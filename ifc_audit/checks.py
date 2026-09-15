"""核查规则：重复构件与未闭合墙检测。

判定阈值集中在 :mod:`ifc_audit.thresholds`，由流程层传入；
本模块顶部的常量仅为向后兼容保留的默认值。
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Optional

import numpy as np
from shapely.geometry import Point
from shapely.ops import unary_union

from .model import AuditModel, Issue, WALL, DOOR, WINDOW, SPACE
from .thresholds import DEFAULT_THRESHOLDS, Thresholds

# 默认判定阈值（单位：米）——阈值现可通过配置文件 / 命令行调整，
# 这些常量仅保留给直接 import 的旧代码，等价于 default 预设。
FREE_END_TOL = DEFAULT_THRESHOLDS.free_end_tol        # 墙端头伸入其它墙体的判定容差
ENDPOINT_MERGE_TOL = DEFAULT_THRESHOLDS.endpoint_merge_tol  # 自由端聚类为“墙间缺口”的容差
GAP_MIN_LEN = DEFAULT_THRESHOLDS.gap_min_len          # 房间围护缺口最小长度
BARRIER_BUFFER = DEFAULT_THRESHOLDS.barrier_buffer    # 围护构件覆盖边界的外扩容差（毫米级）
DUP_CENTROID_TOL = DEFAULT_THRESHOLDS.dup_centroid_tol  # 重复构件形心距离
DUP_VOL_RATIO = DEFAULT_THRESHOLDS.dup_vol_ratio      # 重复构件体积相似度
DUP_IOU = DEFAULT_THRESHOLDS.dup_iou                  # 重复构件平面轮廓 IoU


def _union_footprints(elements, include_doors=True, include_windows=False):
    geoms = [e.footprint for e in elements if e.footprint is not None]
    if not geoms:
        return None
    return unary_union(geoms)


def _iou(a, b) -> float:
    if a is None or b is None:
        return 0.0
    inter = a.intersection(b).area
    union = a.union(b).area
    return inter / union if union > 0 else 0.0


def find_duplicates(model: AuditModel,
                    th: Thresholds = DEFAULT_THRESHOLDS,
                    emit: bool = True) -> None:
    """检测重复构件：同类型、形心重合且几何（体积/轮廓）基本相同。

    emit=False 时只清空历史分组、不做检测也不产生问题（规则包关闭该核查项）。
    """
    if not emit:
        model.duplicate_groups = []
        return
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups_of_type = defaultdict(list)
    for e in model.elements.values():
        groups_of_type[e.ifc_type].append(e)

    pair_records = []
    for ifc_type, elems in groups_of_type.items():
        if len(elems) < 2:
            continue
        for a, b in itertools.combinations(elems, 2):
            dist = float(np.linalg.norm(a.centroid - b.centroid))
            if dist > th.dup_centroid_tol:
                continue
            va, vb = a.volume, b.volume
            if va > 1e-6 and vb > 1e-6:
                ratio = min(va, vb) / max(va, vb)
                if ratio < th.dup_vol_ratio:
                    continue
            score = _iou(a.hull or a.footprint, b.hull or b.footprint)
            # 体积极小或无轮廓的构件（如纯占位件），只要形心重合也判重
            if score < th.dup_iou and va * vb > 1e-6:
                continue
            union(a.global_id, b.global_id)
            pair_records.append((a, b, dist, score))

    clusters = defaultdict(list)
    for gid in parent:
        clusters[find(gid)].append(gid)
    dup_groups = [sorted(g) for g in clusters.values() if len(g) > 1]
    dup_groups.sort(key=lambda g: g[0])
    model.duplicate_groups = dup_groups

    type_cn = {WALL: "墙体", DOOR: "门", WINDOW: "窗", SPACE: "房间"}
    for idx, gids in enumerate(dup_groups, start=1):
        elems = [model.elements[g] for g in gids]
        loc = np.mean([e.centroid[:2] for e in elems], axis=0)
        tname = type_cn.get(elems[0].ifc_type, elems[0].ifc_type)
        names = sorted({e.name for e in elems if e.name})
        title = f"重复{tname}构件 ×{len(elems)}"
        detail = (
            f"{len(elems)} 个{tname}形心距离 < {th.dup_centroid_tol*1000:.0f}mm "
            f"且几何一致（体积比 ≥ {th.dup_vol_ratio:g}，"
            f"轮廓 IoU ≥ {th.dup_iou:g}）。"
            f"构件：{', '.join(e.label for e in elems)}"
        )
        if names:
            title += f"（{names[0]}）"
        model.issues.append(Issue(
            issue_id=f"DUP-{idx:03d}",
            severity="error",
            kind="duplicate_element",
            title=title,
            detail=detail,
            global_ids=list(gids),
            location=(float(loc[0]), float(loc[1])),
            storey=elems[0].storey,
            measure=len(elems),
        ))


def _wall_barrier(model: AuditModel, exclude_id: str | None = None):
    """所有墙身（高位切片，已跨越门洞）的平面联合。"""
    walls = [e for e in model.by_type(WALL)
             if e.footprint is not None and e.global_id != exclude_id]
    return unary_union([e.footprint for e in walls]) if walls else None


def find_wall_closures(model: AuditModel,
                       th: Thresholds = DEFAULT_THRESHOLDS,
                       emit: bool = True) -> None:
    """检测自由墙端与墙间缺口。"""
    if not emit:
        return
    walls = [e for e in model.by_type(WALL) if e.axis is not None]
    # 每条墙用“其它墙”的联合做端点测试
    all_wall_fp = unary_union([e.footprint for e in walls if e.footprint is not None]) \
        if walls else None
    if all_wall_fp is None:
        return

    # 自由端：(墙id, 端点坐标)
    free_ends: list[tuple[str, np.ndarray]] = []
    for w in walls:
        # 用其它墙的缓冲联合测试端头，避免自身轮廓影响
        others_fp = unary_union([
            o.footprint for o in walls
            if o.global_id != w.global_id and o.footprint is not None
        ])
        probe = others_fp.buffer(th.free_end_tol) if others_fp is not None else None
        for coord in w.axis.coords:
            p = Point(coord[:2])
            if probe is None or not p.intersects(probe):
                free_ends.append((w.global_id, np.array(coord[:2])))

    if not free_ends:
        return

    # 对自由端聚类：多个端头聚到一起 => 墙间缺口；单个 => 悬空端头
    clusters: list[list[tuple[str, np.ndarray]]] = []
    for item in free_ends:
        gid, pt = item
        placed = False
        for cl in clusters:
            if np.linalg.norm(pt - cl[0][1]) <= th.endpoint_merge_tol:
                cl.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    seq = 1
    for cl in clusters:
        ids = [g for g, _ in cl]
        center = np.mean([p for _, p in cl], axis=0)
        storey = next((model.elements[g].storey for g in ids if g in model.elements), "")
        if len({g for g, _ in cl}) >= 2:
            gap = float(max(
                np.linalg.norm(p1 - p2)
                for (_, p1), (_, p2) in itertools.combinations(cl, 2)
            )) if len(cl) >= 2 else 0.0
            model.issues.append(Issue(
                issue_id=f"GAP-{seq:03d}",
                severity="error",
                kind="wall_end_gap",
                title=f"墙段端头未闭合，缺口约 {gap*1000:.0f} mm",
                detail=(
                    "两段墙的端头没有搭接，平面上形成开口"
                    f"（端头聚类容差 {th.endpoint_merge_tol*1000:.0f}mm）。"
                    f"涉及构件：{', '.join(model.elements[g].label for g in ids)}。"
                ),
                global_ids=ids,
                location=(float(center[0]), float(center[1])),
                storey=storey,
                measure=gap,
            ))
        else:
            w = model.elements[ids[0]]
            model.issues.append(Issue(
                issue_id=f"END-{seq:03d}",
                severity="warning",
                kind="wall_free_end",
                title="墙体自由端（端头未与任何墙体连接）",
                detail=(
                    f"墙体“{w.name or w.global_id[:8]}”的端头在 "
                    f"{th.free_end_tol*1000:.0f}mm 范围内没有其它墙体与之相接。"
                ),
                global_ids=ids,
                location=(float(center[0]), float(center[1])),
                storey=storey,
            ))
        seq += 1


def find_room_enclosure(model: AuditModel,
                        th: Thresholds = DEFAULT_THRESHOLDS,
                        emit: bool = True) -> None:
    """逐房间检查围护边界：墙身与门扇覆盖不到的边界段即围护缺口。"""
    if not emit:
        return
    barriers = [e.footprint for e in model.by_type(WALL) if e.footprint is not None]
    # 门用凸包（完整门扇）覆盖门洞；窗不参与围护
    barriers += [e.footprint for e in model.by_type(DOOR) if e.footprint is not None]
    barrier = unary_union(barriers).buffer(th.barrier_buffer) if barriers else None

    seq = 1
    for room in model.by_type(SPACE):
        if room.footprint is None:
            model.issues.append(Issue(
                issue_id=f"ENC-{seq:03d}",
                severity="warning",
                kind="room_no_geometry",
                title=f"房间“{room.name}”缺少可用几何",
                detail="无法从几何生成房间平面轮廓，无法检查围护闭合性。",
                global_ids=[room.global_id],
                location=tuple(room.centroid[:2].tolist()),
                storey=room.storey,
            ))
            seq += 1
            continue

        boundary = room.footprint.boundary
        if barrier is not None:
            gaps = boundary.difference(barrier)
        else:
            gaps = boundary

        gap_lines = []
        if gaps.is_empty:
            pass
        elif gaps.geom_type == "LineString":
            gap_lines = [gaps]
        elif gaps.geom_type == "MultiLineString":
            gap_lines = list(gaps.geoms)
        elif hasattr(gaps, "geoms"):
            gap_lines = [g for g in gaps.geoms if g.geom_type == "LineString"]

        # 保留长度超阈值的缺口；房间外轮廓上的缺口最值得报
        big = [g for g in gap_lines if g.length > th.gap_min_len]
        model.room_gaps[room.global_id] = big

        for g in big:
            mid = g.interpolate(0.5, normalized=True)
            model.issues.append(Issue(
                issue_id=f"ENC-{seq:03d}",
                severity="error",
                kind="room_enclosure_gap",
                title=f"房间“{room.name}”围护缺口 {g.length*1000:.0f} mm",
                detail=(
                    f"房间边界有 {g.length:.2f}m 没有墙或门覆盖"
                    f"（上报下限 {th.gap_min_len*1000:.0f}mm），"
                    "该房间未完全闭合（可能漏画墙或墙段未对齐）。"
                ),
                global_ids=[room.global_id],
                location=(float(mid.x), float(mid.y)),
                storey=room.storey,
                measure=float(g.length),
            ))
            seq += 1


def run_all_checks(model: AuditModel,
                   th: Thresholds = DEFAULT_THRESHOLDS,
                   enabled_kinds: Optional[set[str]] = None) -> AuditModel:
    """执行几何类核查；enabled_kinds 给定时只启用问题种类在集合内的核查项。

    核查项与问题种类的映射见 :data:`ifc_audit.rule_packs.CHECK_ISSUE_KINDS`。
    """
    from .rule_packs import (
        CHECK_DUPLICATE, CHECK_WALL_CLOSURE, CHECK_ROOM_ENVELOPE,
        CHECK_ISSUE_KINDS,
    )

    def _enabled(check: str) -> bool:
        if enabled_kinds is None:
            return True
        return any(k in enabled_kinds for k in CHECK_ISSUE_KINDS[check])

    find_duplicates(model, th, emit=_enabled(CHECK_DUPLICATE))
    find_wall_closures(model, th, emit=_enabled(CHECK_WALL_CLOSURE))
    find_room_enclosure(model, th, emit=_enabled(CHECK_ROOM_ENVELOPE))
    # 按楼层、位置排序，报告更稳定
    model.issues.sort(key=lambda i: (i.storey, i.kind, i.issue_id))
    return model
