"""多专业协同核查引擎。

把**建筑 / 结构 / 机电**专业模型放在同一坐标系下比对，完成两类核查：

1. **跨专业硬碰撞**：机电管线（风管 / 水管 / 桥架 / 设备端子）与梁、柱、墙、
   板等围护构件几何相交；
2. **预留洞口核对**：机电管线穿越墙 / 板时，建筑/结构侧是否预留了
   :class:`IfcOpeningElement`，洞口**位置、规格**是否与管线匹配；
   已预留但没有任何管线使用的洞口单独标出。

检测结果按问题类型**自动派给责任专业**（可配置责任人姓名映射），并写入
项目级协同台账（:class:`CoordinationLedger`），随模型版本迭代走
``待整改 → 待复核 → 复核通过 / 驳回`` 流转；重新核查时已消失的问题自动闭环，
复核通过后再次出现则自动重开（回归问题）。批次结论通过
:func:`evaluate_coordination_gate` 与批次放行门禁联动，并回写建筑侧批次结论。

检测以三维轴对齐包围盒（AABB）为基础，适用于规则正交的常规工程模型；
判定参数见 :data:`DEFAULT_COORD_SETTINGS`（毫米/毫米余量，内部换算为米）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Optional, Callable

import numpy as np
import ifcopenshell
import ifcopenshell.geom as ifc_geom
import ifcopenshell.util.element as ifc_element

from .batch import (
    discover_ifc_files, unique_unit_names, _new_batch_id, _slug,
)
from .coordination_model import (
    CoordElement, CoordIssue, CoordinationLedger, CoordinationResult,
    DisciplineFile, make_fingerprint,
    DISC_ARCH, DISC_STRUCT, DISC_MEP, DISCIPLINES, DISC_CN,
    IFC_DISCIPLINE_MAP, OPENING_TYPE,
    KIND_HARD_CLASH, KIND_OPENING_MISSING, KIND_OPENING_MISMATCH,
    KIND_OPENING_UNUSED, COORD_KINDS, KIND_SEVERITY,
    KIND_OWNER_DISCIPLINE,
    STATUS_OPEN, STATUS_FIXED, STATUS_VERIFIED, STATUS_REJECTED,
    STATUS_CLEARED, STATUS_CN,
    ESCALATION_DISCIPLINE_LEAD, ESCALATION_PROJECT_MANAGER, ESCALATION_CN,
    discipline_from_filename,
)
from .units import project_length_scale


# ------------------------------------------------------------- 参数 ----

@dataclass(frozen=True)
class CoordSettings:
    """协同检测判定参数（内部单位：米）。"""

    hard_clash_min_len: float = 0.10     # 嵌入式相交最小长度（小于视为贴邻）
    opening_pos_tol: float = 0.20        # 洞口中心与穿墙点平面偏差上限
    opening_size_tol: float = 0.02       # 洞口尺寸允许负偏差（单边）
    opening_extra_margin: float = 0.05   # 管线每侧要求的安装余量
    clash_clearance: float = 0.0         # 软碰撞净距（0=只报硬碰撞）


# 用户配置键（毫米）-> 内部字段
SETTING_KEYS = {
    "hard_clash_min_len_mm": ("hard_clash_min_len", "硬碰撞最小相交长度", 0.0, 2000.0),
    "opening_pos_tol_mm": ("opening_pos_tol", "洞口位置偏差容差", 0.0, 2000.0),
    "opening_size_tol_mm": ("opening_size_tol", "洞口尺寸负偏差容差", 0.0, 500.0),
    "opening_extra_margin_mm": ("opening_extra_margin", "洞口安装余量", 0.0, 500.0),
    "clash_clearance_mm": ("clash_clearance", "软碰撞净距（0=只报硬碰撞）", 0.0, 1000.0),
}

_DEFAULT_USER = {
    "hard_clash_min_len_mm": 100.0,
    "opening_pos_tol_mm": 200.0,
    "opening_size_tol_mm": 20.0,
    "opening_extra_margin_mm": 50.0,
    "clash_clearance_mm": 0.0,
}


def resolve_settings(overrides: Optional[dict[str, float]] = None
                     ) -> CoordSettings:
    """解析协同检测参数（用户面毫米；非法键 / 越界报错）。"""
    values = dict(_DEFAULT_USER)
    for k, v in (overrides or {}).items():
        if k not in SETTING_KEYS:
            raise CoordinationConfigError(
                f"未知协同检测参数“{k}”，可用键：{', '.join(SETTING_KEYS)}")
        v = float(v)
        attr, _label, lo, hi = SETTING_KEYS[k]
        if not (lo <= v <= hi):
            raise CoordinationConfigError(
                f"协同检测参数 {k}={v:g} 超出允许范围 [{lo:g}, {hi:g}]")
        values[k] = v
    return CoordSettings(**{
        SETTING_KEYS[k][0]: values[k] / 1000.0 for k in SETTING_KEYS})


def settings_user_values(s: CoordSettings) -> dict[str, float]:
    """内部参数 -> 用户面毫米值（报告用）。"""
    return {k: round(getattr(s, SETTING_KEYS[k][0]) * 1000.0, 3)
            for k in SETTING_KEYS}


class CoordinationConfigError(ValueError):
    """协同核查参数 / 文件配置有误。"""


# ------------------------------------------------------------- 门禁 ----

@dataclass(frozen=True)
class CoordinationGate:
    """协同问题放行门禁（仅在执行了多专业协同时参与判定）。

    上限类取 -1 表示不限制该条。
    """

    max_clash_active: int = 0          # 未闭环硬碰撞 + 洞口缺失（错误类）
    max_mismatch_active: int = -1      # 未闭环洞口不符（警告类）
    max_unused_active: int = -1       # 未闭环洞口未使用（警告类）
    max_pending_review: int = -1       # 待复核（责任方已报整改）工单数
    max_overdue_active: int = -1       # 超期未整改（待整改/驳回）工单数
    fix_sla_hours: float = 72.0        # 派单 → 整改完成 的整改时限（小时）
    require_owner: bool = False        # 是否要求每个工单都已指派到人

    @property
    def enabled(self) -> bool:
        return self != for_gate_profile("none")


_COORD_GATE_PROFILES = {
    "default": {
        "fix_sla_hours": 72.0,
        "max_overdue_active": -1,
    },
    "strict": {
        "max_clash_active": 0,
        "max_mismatch_active": 0,
        "max_unused_active": 0,
        "max_pending_review": 0,
        "max_overdue_active": 0,
        "fix_sla_hours": 48.0,
        "require_owner": True,
    },
    "loose": {
        "max_clash_active": 3,
        "max_mismatch_active": -1,
        "max_unused_active": -1,
        "max_pending_review": -1,
        "max_overdue_active": -1,
        "fix_sla_hours": 168.0,
        "require_owner": False,
    },
    "none": {
        "max_clash_active": -1,
        "max_mismatch_active": -1,
        "max_unused_active": -1,
        "max_pending_review": -1,
        "max_overdue_active": -1,
        "fix_sla_hours": 72.0,
        "require_owner": False,
    },
}

COORD_GATE_PROFILES = tuple(_COORD_GATE_PROFILES)
COORD_GATE_PROFILE_CN = {
    "default": "标准协同门禁（错误类零容忍）",
    "strict": "严格协同门禁（全部清零、工单到人、超期零容忍）",
    "loose": "宽松协同门禁（方案阶段）",
    "none": "不设协同门禁",
}

# 门禁键 -> (QualityGate 字段, 中文名, 单位)
COORD_GATE_META = {
    "coord_max_clash_active": (
        "max_clash_active", "未闭环硬碰撞/洞口缺失数", "int"),
    "coord_max_mismatch_active": (
        "max_mismatch_active", "未闭环洞口规格不符数", "int"),
    "coord_max_unused_active": (
        "max_unused_active", "未闭环洞口未使用数", "int"),
    "coord_max_pending_review": (
        "max_pending_review", "待复核工单数", "int"),
    "coord_max_overdue_active": (
        "max_overdue_active", "超期未整改工单数", "int"),
    "coord_fix_sla_hours": (
        "fix_sla_hours", "派单到整改完成时限", "hours"),
    "coord_require_owner": ("require_owner", "工单是否全部指派到人", "bool"),
}


def for_gate_profile(name: str = "default") -> CoordinationGate:
    if name not in _COORD_GATE_PROFILES:
        raise CoordinationConfigError(
            f"未知协同门禁预设“{name}”，可选：{', '.join(_COORD_GATE_PROFILES)}")
    return CoordinationGate(**_COORD_GATE_PROFILES[name])


def resolve_coord_gate(profile: str = "default",
                       overrides: Optional[dict[str, object]] = None
                       ) -> CoordinationGate:
    gate = for_gate_profile(profile)
    if not overrides:
        return gate
    updates = {}
    for key, raw in overrides.items():
        if key not in COORD_GATE_META:
            raise CoordinationConfigError(
                f"未知协同门禁键“{key}”，可用：{', '.join(COORD_GATE_META)}")
        attr, _label, unit = COORD_GATE_META[key]
        if unit == "bool":
            if isinstance(raw, bool):
                v = raw
            elif str(raw).lower() in ("true", "1", "yes", "是"):
                v = True
            elif str(raw).lower() in ("false", "0", "no", "否"):
                v = False
            else:
                raise CoordinationConfigError(f"{key} 应为 true/false")
        elif unit == "hours":
            v = float(raw)
            if not (0.0 <= v <= 24 * 365):
                raise CoordinationConfigError(
                    f"{key}={v:g} 超出允许范围 [0, 8760]（小时，0=不设时限）")
        else:
            v = int(float(raw))
        updates[attr] = v
    return CoordinationGate(**{**asdict(gate), **updates})


def parse_coord_gate_items(items: list[str]) -> dict[str, object]:
    """解析 ``--coord-gate-set key=value``。"""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise CoordinationConfigError(
                f"--coord-gate-set 参数格式应为 key=value：“{item}”")
        key, raw = item.split("=", 1)
        out[key.strip()] = raw.strip()
    return out


def evaluate_coordination_gate(result: CoordinationResult,
                               gate: CoordinationGate,
                               disabled_keys: Optional[set[str]] = None
                               ) -> tuple[bool, list[dict]]:
    """按协同门禁评估批次结论，返回 (是否通过, 逐条判定)。

    判定结果同时写回 ``result.gate_rules`` / ``result.gate_passed``。
    """
    disabled_keys = disabled_keys or set()
    now = datetime.now()
    summ = result.summary()
    n_clash = summ["active_by_kind"][KIND_HARD_CLASH] \
        + summ["active_by_kind"][KIND_OPENING_MISSING]
    n_mismatch = summ["active_by_kind"][KIND_OPENING_MISMATCH]
    n_unused = summ["active_by_kind"][KIND_OPENING_UNUSED]
    n_pending = summ["by_status"][STATUS_FIXED]
    n_overdue = sum(1 for i in result.issues
                    if i.sla_tracked and i.is_overdue(now))
    n_no_owner = sum(1 for i in result.issues if i.active and not i.owner)

    actuals = [
        ("coord_max_clash_active", n_clash, f"未闭环硬碰撞/洞口缺失 {n_clash} 项"),
        ("coord_max_mismatch_active", n_mismatch, f"未闭环洞口不符 {n_mismatch} 项"),
        ("coord_max_unused_active", n_unused, f"未闭环洞口未使用 {n_unused} 项"),
        ("coord_max_pending_review", n_pending, f"待复核工单 {n_pending} 项"),
        ("coord_max_overdue_active", n_overdue,
         f"超期未整改工单 {n_overdue} 项（时限 {gate.fix_sla_hours:g}h）"),
        ("coord_require_owner",
         0 if gate.require_owner else 0,
         f"未指派责任人的未闭环工单 {n_no_owner} 项"),
    ]
    rules = []
    passed_all = True
    for key, actual, shown in actuals:
        if key in disabled_keys:
            continue
        attr, label, unit = COORD_GATE_META[key]
        value = getattr(gate, attr)
        if unit == "bool":
            if not value:
                continue  # 不要求指派到人
            passed = n_no_owner == 0
            limit = "必须全部指派"
        else:
            if value < 0:
                continue  # 规则关闭
            passed = actual <= value
            limit = f"≤ {value:g}"
        passed_all = passed_all and passed
        rules.append({
            "key": key, "rule": label, "limit": limit,
            "actual": shown, "passed": passed,
            "message": ("" if passed else f"{label}超限：{shown}，门禁要求 {limit}"),
        })
    result.gate_rules = rules
    result.gate_passed = (not gate.enabled) or passed_all
    return result.gate_passed, rules


# ------------------------------------------------------------- 提取 ----

_BARRIER_TYPES = {"IfcWall", "IfcSlab", "IfcBeam", "IfcColumn"}


def _opening_host(elem):
    """IfcOpeningElement -> 被开洞的宿主构件。"""
    voids = getattr(elem, "VoidsElements", None)
    if voids:
        return voids[0].RelatingBuildingElement
    return None


def _storey_of(elem) -> str:
    try:
        c = ifc_element.get_container(elem)
        if c is not None:
            return c.Name or ""
    except Exception:
        pass
    return ""


def load_discipline_file(file_path: str, unit: str,
                         discipline: Optional[str] = None
                         ) -> tuple[list[CoordElement], DisciplineFile]:
    """从一份 IFC 提取协同核查所需构件。

    只提取：围护构件（墙/板/梁/柱）、机电构件（管线/端子/设备）、
    预留洞口（IfcOpeningElement）。专业优先取文件内构件类型推断，
    推断不出时用文件名关键词（调用方传入）。
    """
    ifc_file = ifcopenshell.open(file_path)
    scale = project_length_scale(ifc_file)
    df = DisciplineFile(unit=unit, file_path=file_path,
                        discipline=discipline or "")

    settings = ifc_geom.settings()
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except Exception:
        pass

    wanted = _BARRIER_TYPES | {OPENING_TYPE}
    elems_out: list[CoordElement] = []
    disc_votes: dict[str, int] = {}

    for elem in ifc_file.by_type("IfcProduct"):
        ifc_type = elem.is_a()
        is_opening = elem.is_a(OPENING_TYPE)
        base_type = ifc_type
        disc = None
        if is_opening:
            base_type = OPENING_TYPE
        else:
            for base, d in IFC_DISCIPLINE_MAP.items():
                if elem.is_a(base):
                    base_type = base
                    disc = d
                    break
            if base_type not in wanted and disc != DISC_MEP:
                continue

        shape = None
        try:
            shape = ifc_geom.create_shape(settings, elem)
        except Exception:
            shape = None
        if shape is None:
            continue
        g = shape.geometry
        verts = np.asarray(g.verts, dtype=float).reshape(-1, 3) * scale
        if not len(verts):
            continue

        mn, mx = verts.min(axis=0), verts.max(axis=0)
        dims = (mx - mn)
        bounds = (float(mn[0]), float(mn[1]), float(mn[2]),
                  float(mx[0]), float(mx[1]), float(mx[2]))
        long_axis = int(np.argmax(dims))
        section = tuple(float(v) for i, v in enumerate(dims)
                        if i != long_axis)
        if len(section) != 2:
            section = (0.0, 0.0)

        if disc:
            disc_votes[disc] = disc_votes.get(disc, 0) + 1

        host = _opening_host(elem) if is_opening else None
        host_id = host.GlobalId if host is not None else None
        host_type = ""
        host_disc = ""
        if host is not None:
            host_type = host.is_a()
            for base, d in IFC_DISCIPLINE_MAP.items():
                if host.is_a(base):
                    host_disc = d
                    break

        ce = CoordElement(
            global_id=elem.GlobalId,
            ifc_type=base_type,
            name=elem.Name or "",
            discipline=disc or "",
            unit=unit,
            file_path=file_path,
            storey=_storey_of(elem),
            object_type=getattr(elem, "ObjectType", "") or "",
            bounds=bounds,
            cx=float((mn[0] + mx[0]) / 2),
            cy=float((mn[1] + mx[1]) / 2),
            cz=float((mn[2] + mx[2]) / 2),
            length=float(dims[long_axis]),
            axis_kind=("x", "y", "z")[long_axis],
            section=section,
            is_opening=is_opening,
            host_id=host_id,
            host_type=host_type,
            host_discipline=host_disc,
            raw=elem,
        )
        elems_out.append(ce)

    # 文件主导专业：机电构件计 1 票，围护构件按类型计票；文件名推断兜底
    if discipline:
        file_disc = discipline
    elif disc_votes:
        file_disc = max(sorted(disc_votes), key=lambda d: disc_votes[d])
    else:
        file_disc = discipline_from_filename(file_path) or DISC_ARCH
    df.discipline = file_disc
    for e in elems_out:
        if not e.discipline:
            e.discipline = file_disc if not e.is_opening else ""
        if e.is_opening and not e.host_discipline:
            e.host_discipline = file_disc
    df.n_elements = len([e for e in elems_out if not e.is_opening])
    df.n_openings = len([e for e in elems_out if e.is_opening])
    return elems_out, df


# ------------------------------------------------------------- 几何 ----

def _overlap_amounts(b1, b2) -> list[float]:
    """两个 AABB 在 x/y/z 三轴的重叠长度（不重叠为 0）。"""
    out = []
    for ax in range(3):
        lo = max(b1[ax], b2[ax])
        hi = min(b1[ax + 3], b2[ax + 3])
        out.append(max(0.0, hi - lo))
    return out


def _bbox_gap(b1, b2) -> float:
    """两个 AABB 表面最小间距（相交返回负值重叠深度）。"""
    gaps = []
    for ax in range(3):
        if b1[ax + 3] < b2[ax]:
            gaps.append(b2[ax] - b1[ax + 3])
        elif b2[ax + 3] < b1[ax]:
            gaps.append(b1[ax] - b2[ax + 3])
    if not gaps:
        return -min(_overlap_amounts(b1, b2))
    return float(np.linalg.norm(gaps))


def _crosses(svc: CoordElement, barrier: CoordElement, axis: int,
             tol: float = 0.0) -> bool:
    """管线包围盒是否沿 axis 轴贯穿构件（两侧都伸出）。"""
    b, s = barrier.bounds, svc.bounds
    return s[axis] < b[axis] - tol and s[axis + 3] > b[axis + 3] + tol


def _thinnest_axis(e: CoordElement, horizontal_only: bool = False) -> int:
    dims = [e.bounds[3] - e.bounds[0],
            e.bounds[4] - e.bounds[1],
            e.bounds[5] - e.bounds[2]]
    if horizontal_only:
        return 0 if dims[0] <= dims[1] else 1
    return int(np.argmin(dims))


def _center(e: CoordElement) -> tuple[float, float, float]:
    return e.cx, e.cy, e.cz


# ------------------------------------------------------------- 检测 ----

def _element_ref(e: CoordElement) -> dict:
    return {
        "global_id": e.global_id,
        "ifc_type": e.ifc_type,
        "name": e.name,
        "discipline": e.discipline or e.host_discipline,
        "unit": e.unit,
        "storey": e.storey,
    }


def _issue(kind: str, title: str, detail: str, elems: list[CoordElement],
           measure: float, measure_label: str, owner_discipline: str,
           owners: dict[str, str], location, storey: str,
           element_ids: list[str], extra_fp: str = "") -> CoordIssue:
    discs = []
    for e in elems:
        d = e.discipline or e.host_discipline
        if d and d not in discs:
            discs.append(d)
    return CoordIssue(
        fingerprint=make_fingerprint(kind, element_ids, extra_fp),
        kind=kind,
        severity=KIND_SEVERITY[kind],
        title=title,
        detail=detail,
        elements=[_element_ref(e) for e in elems],
        disciplines=discs,
        location=tuple(float(v) for v in location),
        storey=storey,
        measure=round(float(measure), 4),
        measure_label=measure_label,
        owner_discipline=owner_discipline,
        owner=owners.get(owner_discipline, ""),
    )


def _opening_covers(svc: CoordElement, barrier: CoordElement,
                    normal: int, opening: CoordElement,
                    settings: CoordSettings
                    ) -> tuple[bool, float, float]:
    """洞口是否覆盖管线穿越需求。

    Returns:
        (位置是否在容差内, 洞口中心与穿越点的平面距离 m, 最大单边尺寸缺口 m)。
    """
    b = barrier.bounds
    tang = [ax for ax in range(3) if ax != normal]
    # 穿越点：管线中心投影到构件中面（切向坐标取管线中心）
    pen_center = [svc.cx, svc.cy, svc.cz]
    pen_center[normal] = (b[normal] + b[normal + 3]) / 2

    o = opening.bounds
    center_dist_axes = []
    shortfall = 0.0
    inside = True
    for ax in tang:
        oc = (o[ax] + o[ax + 3]) / 2
        # 需要的洞口半宽 = 管线截面半宽 + 每侧安装余量
        svc_half = (svc.bounds[ax + 3] - svc.bounds[ax]) / 2
        need_half = svc_half + settings.opening_extra_margin
        o_half = (o[ax + 3] - o[ax]) / 2
        shortfall = max(shortfall, need_half - o_half)
        center_dist_axes.append(abs(oc - pen_center[ax]))
        if not (o[ax] - settings.opening_pos_tol <= pen_center[ax]
                <= o[ax + 3] + settings.opening_pos_tol):
            inside = False
    center_dist = float(np.linalg.norm(center_dist_axes))
    return inside, center_dist, max(0.0, shortfall)


def detect_coordination(elements: list[CoordElement],
                        settings: CoordSettings,
                        owners: Optional[dict[str, str]] = None
                        ) -> list[CoordIssue]:
    """对全部专业构件执行碰撞与预留洞口检测，返回问题列表。"""
    owners = owners or {}
    services = [e for e in elements
                if e.discipline == DISC_MEP and not e.is_opening]
    barriers = [e for e in elements if e.ifc_type in _BARRIER_TYPES]
    openings = [e for e in elements if e.is_opening and e.host_id]
    openings_by_host: dict[str, list[CoordElement]] = {}
    for o in openings:
        openings_by_host.setdefault(o.host_id, []).append(o)

    host_by_id = {b.global_id: b for b in barriers}
    issues: list[CoordIssue] = []
    used_openings: set[str] = set()
    handled_pairs: set[tuple[str, str, str]] = set()

    def barrier_kind(b: CoordElement) -> str:
        if b.ifc_type in ("IfcBeam", "IfcColumn"):
            return "frame"
        if b.ifc_type == "IfcSlab":
            return "slab"
        return "wall"

    for svc in services:
        for bar in barriers:
            # 只做跨专业（机电 vs 建筑/结构围护）
            if (bar.discipline or bar.host_discipline) == DISC_MEP:
                continue
            gaps = _overlap_amounts(svc.bounds, bar.bounds)
            bbox_intersects = min(gaps) > 0.0
            clearance_gap = _bbox_gap(svc.bounds, bar.bounds)
            if settings.clash_clearance > 0:
                if clearance_gap > settings.clash_clearance:
                    continue
            elif not bbox_intersects:
                continue

            bkind = barrier_kind(bar)
            normal = (2 if bkind == "slab"
                      else _thinnest_axis(bar, horizontal_only=True))
            crosses = _crosses(svc, bar, normal)
            loc = _center(svc)
            storey = svc.storey or bar.storey
            bar_disc = bar.discipline or bar.host_discipline or DISC_STRUCT
            cn_name = {"wall": "墙", "slab": "板", "frame": "梁柱"}[bkind]
            bar_ref = f"{DISC_CN.get(bar_disc, bar_disc)}{cn_name} {bar.name or bar.global_id[:8]}"
            svc_ref = f"机电管线 {svc.name or svc.global_id[:8]}"

            # 仅在包围盒真实相交时判定穿越/硬碰撞；净距不足（软碰撞）暂只跳过，
            # 避免把贴邻管线误报为缺洞
            if bbox_intersects and bkind in ("wall", "slab") and crosses:
                # --- 穿越墙/板：核对预留洞口 ---
                host_openings = openings_by_host.get(bar.global_id, [])
                best = None
                for o in host_openings:
                    inside, cdist, shortfall = _opening_covers(
                        svc, bar, normal, o, settings)
                    if inside and (best is None or cdist < best[1]):
                        best = (o, cdist, shortfall)
                if best is None:
                    # 找最近的洞口（容差外），用于区分“没留洞”与“留偏了”
                    nearest = None
                    for o in host_openings:
                        _, cdist, shortfall = _opening_covers(
                            svc, bar, normal, o, settings)
                        if nearest is None or cdist < nearest[1]:
                            nearest = (o, cdist, shortfall)
                    if nearest and nearest[1] <= settings.opening_pos_tol * 3:
                        o2, cdist, shortfall = nearest
                        used_openings.add(o2.global_id)
                        issues.append(_issue(
                            KIND_OPENING_MISSING,
                            f"管线穿越{cn_name}处洞口位置偏差过大",
                            f"{svc_ref} 穿越{bar_ref}，最近的预留洞口中心偏差 "
                            f"{cdist * 1000:.0f}mm（容差 "
                            f"{settings.opening_pos_tol * 1000:.0f}mm），"
                            "无法覆盖穿越点，需重新定位开洞。",
                            [svc, bar, o2], cdist * 1000, "洞口中心偏差 mm",
                            bar_disc, owners, loc, storey,
                            [svc.global_id, bar.global_id, o2.global_id]))
                    else:
                        issues.append(_issue(
                            KIND_OPENING_MISSING,
                            f"管线穿越{cn_name}未预留洞口",
                            f"{svc_ref} 穿越{bar_ref}，{cn_name}上未找到覆盖该"
                            f"穿越点的预留洞口（安装余量每侧 "
                            f"{settings.opening_extra_margin * 1000:.0f}mm）。",
                            [svc, bar], 1.0, "缺洞 1 处",
                            bar_disc, owners, loc, storey,
                            [svc.global_id, bar.global_id]))
                else:
                    o1, cdist, shortfall = best
                    used_openings.add(o1.global_id)
                    if shortfall > settings.opening_size_tol:
                        issues.append(_issue(
                            KIND_OPENING_MISMATCH,
                            f"预留洞口规格不足（{o1.name or o1.global_id[:8]}）",
                            f"{svc_ref} 穿越{bar_ref}，预留洞口相对管线截面+"
                            f"安装余量每侧缺口最大 {shortfall * 1000:.0f}mm"
                            f"（容差 {settings.opening_size_tol * 1000:.0f}mm），"
                            "洞口需扩大。",
                            [svc, bar, o1], shortfall * 1000,
                            "洞口单边缺口 mm", bar_disc, owners, loc, storey,
                            [svc.global_id, bar.global_id, o1.global_id]))
                handled_pairs.add((svc.global_id, bar.global_id, "penetration"))
                continue

            # --- 硬碰撞（必须真实相交；纯净距不足不在本版报告）---
            if not bbox_intersects:
                continue
            overlap_vol = gaps[0] * gaps[1] * gaps[2]
            # 嵌入式相交：沿构件表面方向要有实质性重叠，过滤贴邻误报
            tang = [a for a in range(3) if a != normal]
            tang_overlap = min(gaps[a] for a in tang)
            if tang_overlap < settings.hard_clash_min_len:
                continue
            if (svc.global_id, bar.global_id, "clash") in handled_pairs:
                continue
            handled_pairs.add((svc.global_id, bar.global_id, "clash"))
            if bkind == "frame":
                title = f"机电管线与{cn_name}硬碰撞"
                detail = (f"{svc_ref} 与{bar_ref}几何相交，"
                          f"包围盒交叠约 {overlap_vol * 1e3:.1f} L，"
                          "原则上不得在梁柱上开洞，请调整路由。")
            else:
                state = "嵌入" if not crosses else "穿越段无有效洞口"
                title = f"机电管线与{cn_name}硬碰撞"
                detail = (f"{svc_ref} {state}{cn_name}（{bar_ref}），"
                          f"包围盒交叠约 {overlap_vol * 1e3:.1f} L，"
                          "且未被合格预留洞口覆盖。")
            issues.append(_issue(
                KIND_HARD_CLASH, title, detail, [svc, bar],
                overlap_vol, "交叠体积 m³",
                KIND_OWNER_DISCIPLINE[KIND_HARD_CLASH], owners,
                loc, storey, [svc.global_id, bar.global_id]))

    # --- 预留洞口未使用 ---
    for o in openings:
        if o.global_id in used_openings:
            continue
        host = host_by_id.get(o.host_id)
        # 洞口未使用：责任是机电确认（管线路由变更）还是建筑封洞，
        # 默认派机电专业核对路由
        elems = [o] + ([host] if host is not None else [])
        issues.append(_issue(
            KIND_OPENING_UNUSED,
            f"预留洞口无管线使用（{o.name or o.global_id[:8]}）",
            f"预留洞口 {o.name or o.global_id[:8]}"
            f"（宿主 {host.name if host else o.host_type}）未检测到任何"
            "机电管线穿越，请机电确认路由或由建筑/结构封堵。",
            elems, 1.0, "闲置洞口 1 处",
            KIND_OWNER_DISCIPLINE[KIND_OPENING_UNUSED], owners,
            ((o.cx, o.cy, o.cz)), o.storey or (host.storey if host else ""),
            [o.global_id]))

    issues.sort(key=lambda i: (i.severity != "error", i.kind,
                               i.location[2], i.location[1], i.location[0]))
    return issues


# ----------------------------------------------------- 台账合并/流转 ----

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_issue_sla(issue: CoordIssue, sla_hours: float,
                  start_at: str = "", reset_clock: bool = True) -> None:
    """为工单设置整改时限（小时）并据此计算截止时间。

    Args:
        start_at: 本轮整改环节起算时间（ISO）；不给则用当前时间。
        reset_clock: 工单已有时限且本次只改时限值时，是否把时钟重置到
            ``start_at``；驳回 / 重开必须重置，普通配置刷新不重置。
    """
    start_at = start_at or _now()
    issue.sla_hours = float(sla_hours)
    if reset_clock or not issue.due_at:
        base = datetime.fromisoformat(start_at)
        issue.due_at = (base + timedelta(hours=float(sla_hours))
                        ).isoformat(timespec="seconds")


def backfill_ledger_sla(ledger: CoordinationLedger, sla_hours: float,
                        batch_id: str = "", now: Optional[datetime] = None
                        ) -> int:
    """为历史台账中缺时限的活动工单补整改时限（向后兼容）。

    历史工单补时限时以**补录时刻**起算完整时限（不追溯派单时间），避免一开启
    时限功能就把全部历史工单判成超期；已闭环工单不补。返回补录工单数。
    """
    if sla_hours <= 0:
        return 0
    now = now or datetime.now()
    now_s = now.isoformat(timespec="seconds")
    n = 0
    for issue in ledger.issues.values():
        if issue.sla_hours > 0 and issue.due_at:
            continue
        if not issue.active:
            continue
        set_issue_sla(issue, sla_hours, start_at=now_s)
        issue.history.append({
            "batch_id": batch_id, "at": now_s, "action": "sla_backfill",
            "from": issue.status, "to": issue.status,
            "note": f"历史工单补录整改时限 {sla_hours:g}h（自补录时刻起算）"})
        issue.updated_at = now_s
        n += 1
    return n


def apply_sla_sweep(ledger: CoordinationLedger, sla_hours: float,
                    batch_id: str = "", now: Optional[datetime] = None
                    ) -> list[CoordIssue]:
    """对台账执行整改时限扫描：补录历史时限 + 超时自动升级。

    仅作用于仍在整改环节（待整改 / 驳回）的工单；待复核与已闭环工单不受时限
    约束。升级幂等：同一张工单每次跨过下一级别只产生一条升级记录。
    返回本次发生升级的工单列表。
    """
    now = now or datetime.now()
    backfill_ledger_sla(ledger, sla_hours, batch_id, now)
    now_s = now.isoformat(timespec="seconds")
    escalated: list[CoordIssue] = []
    for issue in ledger.issues.values():
        if not issue.sla_tracked or not issue.due_at or issue.sla_hours <= 0:
            continue
        due = datetime.fromisoformat(issue.due_at)
        if now <= due:
            continue
        overdue_h = (now - due).total_seconds() / 3600.0
        # 逐级升级：超期 -> 专业负责人；再超一个时限周期 -> 项目协调
        target_level = ESCALATION_PROJECT_MANAGER \
            if overdue_h > issue.sla_hours else ESCALATION_DISCIPLINE_LEAD
        if issue.escalation_level >= target_level:
            # 已升级过：仅补齐状态标记，不重复写流转记录
            if not issue.escalated:
                issue.escalated = True
                issue.escalated_at = issue.escalated_at or now_s
            continue
        prev_level = issue.escalation_level
        issue.escalation_level = target_level
        issue.escalated = True
        issue.escalated_at = now_s
        cn = ESCALATION_CN[target_level]
        issue.history.append({
            "batch_id": batch_id, "at": now_s, "action": "escalate",
            "from": issue.status, "to": issue.status,
            "level": target_level, "prev_level": prev_level,
            "note": f"超过整改时限 {issue.sla_hours:g}h 未完成整改，自动升级：{cn}"
                    f"（已超期 {overdue_h:.1f}h）"})
        issue.updated_at = now_s
        escalated.append(issue)
    return escalated


def merge_with_ledger(scanned: list[CoordIssue],
                      ledger: CoordinationLedger,
                      batch_id: str,
                      owners: dict[str, str],
                      sla_hours: float = 72.0) -> list[CoordIssue]:
    """把本批扫描结果与项目台账合并（按指纹合单），返回本批问题列表。

    流转规则：

    * 新问题：编号入台账，状态 ``open``，按 ``sla_hours`` 设置整改时限；
    * 台账中仍在本批出现：保留工单状态与责任人；``verified`` 再次出现
      视为回归，自动重开为 ``open`` 并重排整改时限、清零升级标记；
    * 台账中本批未出现：``fixed`` 自动复核通过（重新核查确认消失），
      ``open/rejected`` 标记 ``cleared``，``verified`` 保持不变；
    * 合并后执行整改时限扫描：历史工单补录时限，超时未整改自动升级。
    """
    now = _now()
    now_dt = datetime.fromisoformat(now)
    present = set()
    out: list[CoordIssue] = []
    seq = 1 + max(
        (int(i.issue_id.split("-")[1]) for i in ledger.issues.values()
         if i.issue_id.startswith("COORD-")
         and i.issue_id.split("-")[1].isdigit()),
        default=0)

    for issue in scanned:
        present.add(issue.fingerprint)
        old = ledger.get(issue.fingerprint)
        if old is None:
            issue.issue_id = f"COORD-{seq:04d}"
            seq += 1
            issue.created_batch = batch_id
            issue.created_at = now
            issue.updated_at = now
            if sla_hours > 0:
                set_issue_sla(issue, sla_hours, start_at=now)
            issue.history.append({
                "batch_id": batch_id, "at": now, "action": "created",
                "from": "", "to": STATUS_OPEN,
                "note": "协同核查首次发现并派单"
                        + (f"，整改时限 {sla_hours:g}h" if sla_hours > 0 else "")})
            ledger.issues[issue.fingerprint] = issue
            out.append(issue)
            continue

        # 已配置责任人姓名时补到历史工单
        if not old.owner and owners.get(old.owner_discipline):
            old.owner = owners[old.owner_discipline]
        old.present_in_scan = True
        if old.status in (STATUS_VERIFIED, STATUS_CLEARED):
            # 回归：已闭环问题再次出现，自动重开并重排整改时限
            action = "reopen" if old.status == STATUS_VERIFIED else "reopen_cleared"
            note = ("复核通过后问题再次出现（回归），自动重开"
                    if old.status == STATUS_VERIFIED
                    else "已消除的问题在新模型中再次出现，自动重开")
            old.history.append({
                "batch_id": batch_id, "at": now, "action": action,
                "from": old.status, "to": STATUS_OPEN, "note": note})
            old.status = STATUS_OPEN
            old.escalated = False
            old.escalation_level = 0
            old.escalated_at = ""
            if sla_hours > 0:
                set_issue_sla(old, sla_hours, start_at=now)
            old.updated_at = now
        # 用最新扫描刷新几何/描述（责任与状态保留）
        old.kind = issue.kind
        old.severity = issue.severity
        old.title = issue.title
        old.detail = issue.detail
        old.elements = issue.elements
        old.disciplines = issue.disciplines
        old.location = issue.location
        old.storey = issue.storey
        old.measure = issue.measure
        old.measure_label = issue.measure_label
        out.append(old)

    # 本批未出现的历史问题：自动闭环
    for fp, old in ledger.issues.items():
        if fp in present:
            continue
        if old.status == STATUS_FIXED:
            old.history.append({
                "batch_id": batch_id, "at": now, "action": "auto_verify",
                "from": STATUS_FIXED, "to": STATUS_VERIFIED,
                "note": "重新核查未再检出，自动复核通过"})
            old.status = STATUS_VERIFIED
            old.verified_at = now
            old.verified_by = old.verified_by or "重新核查自动复核"
            old.updated_at = now
        elif old.status in (STATUS_OPEN, STATUS_REJECTED):
            old.history.append({
                "batch_id": batch_id, "at": now, "action": "auto_clear",
                "from": old.status, "to": STATUS_CLEARED,
                "note": "重新核查未再检出，冲突在模型中已消失"})
            old.status = STATUS_CLEARED
            old.updated_at = now

    # 整改时限扫描：历史工单补录时限，超时未整改自动升级（幂等）
    apply_sla_sweep(ledger, sla_hours, batch_id, now_dt)

    # 本批问题列表 = 本批扫到的 + 仍在整改/待复核但本批未扫到（定位不到，
    # 但门禁仍需统计），已闭环历史不进本批
    active_missing = [i for fp, i in ledger.issues.items()
                      if fp not in present and i.active]
    out.extend(active_missing)
    out.sort(key=lambda i: (i.issue_id or ""))
    return out


# ------------------------------------------------------------- 主入口 ----

def classify_files(paths: list[str],
                   discipline_map: Optional[dict[str, str]] = None
                   ) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """展开路径并为每个文件确定单体名与专业。

    Returns:
        (files, unit_names, file_disciplines)。
    """
    files = discover_ifc_files(paths)
    unit_names = unique_unit_names(files)
    discipline_map = discipline_map or {}
    file_discs: dict[str, str] = {}
    for fp in files:
        unit = unit_names[fp]
        if unit in discipline_map:
            disc = discipline_map[unit]
        elif os.path.abspath(fp) in discipline_map:
            disc = discipline_map[os.path.abspath(fp)]
        else:
            disc = discipline_from_filename(fp) or ""
        file_discs[fp] = disc
    return files, unit_names, file_discs


def has_multiple_disciplines(file_disciplines: dict[str, str]) -> bool:
    """参与比对的文件是否覆盖了 ≥2 个专业（含机电才有协同意义）。"""
    discs = {d for d in file_disciplines.values() if d}
    return DISC_MEP in discs and len(discs) >= 2


def run_coordination(paths: list[str],
                     project: str = "未命名项目",
                     label: str = "",
                     batch_id: Optional[str] = None,
                     discipline_map: Optional[dict[str, str]] = None,
                     owners: Optional[dict[str, str]] = None,
                     settings: Optional[CoordSettings] = None,
                     gate_profile: str = "default",
                     gate_overrides: Optional[dict[str, object]] = None,
                     ledger_path: Optional[str] = None,
                     disabled_gate_keys: Optional[set[str]] = None,
                     progress: Optional[Callable[[int, str], None]] = None
                     ) -> CoordinationResult:
    """执行一次多专业协同核查并与项目台账合并。

    Args:
        paths: 各专业 IFC 文件 / 目录（文件名用 建筑/结构/机电 等关键词，
            或用 discipline_map 显式指定单体专业）。
        owners: 专业 -> 责任人姓名，如 ``{"struct": "张工", "mep": "李工"}``。
        ledger_path: 台账 JSON 路径；不给则不做跨批次合单（每次全新）。
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    owners = {k: v for k, v in (owners or {}).items()
              if k in DISCIPLINES and v}
    settings = settings or CoordSettings()
    gate = resolve_coord_gate(gate_profile, gate_overrides)
    files, unit_names, file_discs = classify_files(paths, discipline_map)
    if not files:
        raise FileNotFoundError("指定路径下没有找到 IFC 文件")

    report(5, f"发现 {len(files)} 份专业模型，正在提取多专业构件…")
    all_elements: list[CoordElement] = []
    disc_files: list[DisciplineFile] = []
    for idx, fp in enumerate(files):
        unit = unit_names[fp]
        disc = file_discs.get(fp) or None
        try:
            elems, df = load_discipline_file(fp, unit, disc)
            all_elements.extend(elems)
            disc_files.append(df)
        except Exception as exc:
            disc_files.append(DisciplineFile(
                unit=unit, file_path=fp, discipline=disc or "",
                ok=False, error=f"{type(exc).__name__}: {exc}"))
        report(5 + int((idx + 1) / len(files) * 45),
               f"[{idx + 1}/{len(files)}] 已提取 {unit}"
               f"（{DISC_CN.get(disc, disc or '未判定')}）")

    report(55, "正在执行跨专业碰撞与预留洞口核对…")
    scanned = detect_coordination(all_elements, settings, owners)

    batch_id = batch_id or _new_batch_id()
    if ledger_path:
        ledger = CoordinationLedger.load_or_new(ledger_path, project)
    else:
        ledger = CoordinationLedger(project=project)
    issues = merge_with_ledger(scanned, ledger, batch_id, owners,
                               sla_hours=gate.fix_sla_hours)

    result = CoordinationResult(
        project=project,
        batch_id=batch_id,
        label=label or "",
        created_at=_now(),
        files=disc_files,
        elements=all_elements,
        issues=issues,
        owners=dict(owners),
        ledger_path=ledger_path or "",
        settings={
            "values": settings_user_values(settings),
            "gate_profile": gate_profile,
            "fix_sla_hours": gate.fix_sla_hours,
        },
    )

    # 责任人配置变更时同步全部未闭环工单的责任人姓名
    for i in result.issues:
        if i.active and not i.owner and owners.get(i.owner_discipline):
            i.owner = owners[i.owner_discipline]

    evaluate_coordination_gate(result, gate, disabled_gate_keys)

    # 建筑侧回写结论
    result.arch_writeback = build_arch_writeback(result)

    if ledger_path:
        ledger.save(ledger_path)
        ledger.updated_at = _now()

    report(100, f"协同核查完成：{result.summary()['issues_active']} 项未闭环。")
    return result


def build_arch_writeback(result: CoordinationResult) -> dict:
    """生成回写建筑侧批次的协同结论（批次 + 按单体/楼层拆分）。"""
    arch_units = {f.unit for f in result.files
                  if f.discipline == DISC_ARCH}
    per_unit: dict[str, dict] = {}
    for issue in result.issues:
        if not issue.active:
            continue
        # 问题涉及的建筑单体（碰撞构件来自建筑模型时）；其余记“跨专业”
        units = sorted({e["unit"] for e in issue.elements
                        if e.get("discipline") in (DISC_ARCH, DISC_STRUCT)})
        targets = [u for u in units if u in arch_units] or units or ["跨专业"]
        for u in targets:
            row = per_unit.setdefault(u, {
                "unit": u, "active": 0, "errors": 0, "warnings": 0,
                "overdue": 0, "escalated": 0,
                "by_kind": {k: 0 for k in COORD_KINDS}})
            row["active"] += 1
            row["errors" if issue.severity == "error" else "warnings"] += 1
            if issue.is_overdue():
                row["overdue"] += 1
            if issue.escalated and issue.sla_tracked:
                row["escalated"] += 1
            row["by_kind"][issue.kind] = row["by_kind"].get(issue.kind, 0) + 1
    summ = result.summary()
    return {
        "project": result.project,
        "batch_id": result.batch_id,
        "written_at": _now(),
        "verdict": ("多专业协同核查通过" if result.gate_passed
                    else "多专业协同核查未通过（协同门禁阻断）"),
        "gate_passed": result.gate_passed,
        "fix_sla_hours": result.settings.get("fix_sla_hours", 0),
        "issues_total": summ["issues_total"],
        "issues_active": summ["issues_active"],
        "issues_overdue": summ["issues_overdue"],
        "issues_escalated": summ["issues_escalated"],
        "active_by_kind": summ["active_by_kind"],
        "by_status": summ["by_status"],
        "owners": dict(result.owners),
        "per_arch_unit": sorted(per_unit.values(), key=lambda r: r["unit"]),
    }


def default_ledger_path(history_dir: str, project: str) -> str:
    """项目协同台账默认路径：<history>/<项目>/coordination_ledger.json。"""
    return os.path.join(history_dir, _slug(project), "coordination_ledger.json")


# ------------------------------------------------------------- 工单流转 ----

class CoordWorkflowError(ValueError):
    """工单状态流转不合法。"""


def _transition(issue: CoordIssue, action: str, to_status: str,
                batch_id: str, note: str, actor: str,
                allowed_from: tuple[str, ...], extra: Optional[dict] = None
                ) -> CoordIssue:
    if issue.status not in allowed_from:
        raise CoordWorkflowError(
            f"工单 {issue.issue_id} 当前状态为"
            f"{STATUS_CN.get(issue.status, issue.status)}，不能执行「{action}」"
            f"（仅 { '、'.join(STATUS_CN[s] for s in allowed_from) } 状态可操作）")
    entry = {
        "batch_id": batch_id, "at": _now(), "action": action,
        "from": issue.status, "to": to_status,
        "by": actor or "", "note": note or "",
    }
    if extra:
        entry.update(extra)
    issue.history.append(entry)
    issue.status = to_status
    issue.updated_at = entry["at"]
    if actor:
        issue.review_note = note or issue.review_note
    return issue


def assign_issue(ledger: CoordinationLedger, issue_id: str,
                 owner: str, discipline: str = "",
                 by: str = "", note: str = "",
                 batch_id: str = "") -> CoordIssue:
    """派单 / 改派：指定责任人（可同时改责任专业）。"""
    issue = find_issue(ledger, issue_id)
    if discipline:
        if discipline not in DISCIPLINES:
            raise CoordWorkflowError(
                f"未知专业“{discipline}”，可选：{', '.join(DISCIPLINES)}")
        issue.owner_discipline = discipline
    if owner:
        issue.owner = owner
    elif discipline and not issue.owner:
        issue.owner = ""
    issue.history.append({
        "batch_id": batch_id, "at": _now(), "action": "assign",
        "from": issue.status, "to": issue.status,
        "by": by or "", "note": note or "",
        "owner": issue.owner,
        "owner_discipline": issue.owner_discipline,
    })
    issue.updated_at = _now()
    return issue


def fix_issue(ledger: CoordinationLedger, issue_id: str,
              by: str, note: str = "", batch_id: str = "") -> CoordIssue:
    """责任专业报整改完成（待复核）。"""
    issue = find_issue(ledger, issue_id)
    overdue = issue.is_overdue()
    _transition(issue, "fix", STATUS_FIXED, batch_id, note, by,
                (STATUS_OPEN, STATUS_REJECTED),
                extra=({"overdue": True} if overdue else None))
    issue.fixed_by = by
    issue.fixed_note = note
    issue.fixed_at = issue.updated_at
    return issue


def verify_issue(ledger: CoordinationLedger, issue_id: str,
                 by: str, note: str = "复核通过",
                 batch_id: str = "") -> CoordIssue:
    """发起专业复核通过。"""
    issue = find_issue(ledger, issue_id)
    _transition(issue, "verify", STATUS_VERIFIED, batch_id, note, by,
                (STATUS_FIXED,))
    issue.verified_by = by
    issue.verified_at = _now()
    return issue


def reject_issue(ledger: CoordinationLedger, issue_id: str,
                 by: str, note: str, batch_id: str = "",
                 sla_hours: float = 72.0) -> CoordIssue:
    """复核驳回，退回责任专业整改（重排整改时限、清零自动升级标记）。"""
    issue = find_issue(ledger, issue_id)
    if not note:
        raise CoordWorkflowError("驳回必须填写原因（--note）")
    _transition(issue, "reject", STATUS_REJECTED, batch_id, note, by,
                (STATUS_FIXED,))
    # 驳回后新一轮整改：重排时限，升级标记清零（历史升级记录仍保留）
    if sla_hours > 0:
        set_issue_sla(issue, sla_hours, start_at=issue.updated_at)
    issue.escalated = False
    issue.escalation_level = 0
    issue.escalated_at = ""
    issue.history.append({
        "batch_id": batch_id, "at": issue.updated_at,
        "action": "sla_reset", "from": STATUS_REJECTED,
        "to": STATUS_REJECTED,
        "note": (f"驳回后重排整改时限 {sla_hours:g}h"
                 if sla_hours > 0 else "驳回后进入新一轮整改")})
    return issue


def find_issue(ledger: CoordinationLedger, issue_id: str) -> CoordIssue:
    """按工单编号或指纹查找问题。"""
    if issue_id in ledger.issues:
        return ledger.issues[issue_id]
    for i in ledger.issues.values():
        if i.issue_id == issue_id:
            return i
    raise CoordWorkflowError(f"台账中找不到工单：{issue_id}")


def parse_owner_items(items: list[str]) -> dict[str, str]:
    """解析 ``--owner struct=张工``（可重复）为 {专业: 责任人}。"""
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise CoordinationConfigError(
                f"--owner 参数格式应为 专业=姓名：“{item}”")
        disc, name = item.split("=", 1)
        disc, name = disc.strip(), name.strip()
        if disc not in DISCIPLINES:
            raise CoordinationConfigError(
                f"未知专业“{disc}”，可选：{', '.join(DISCIPLINES)}"
                f"（{', '.join(DISC_CN[d] for d in DISCIPLINES)}）")
        out[disc] = name
    return out
