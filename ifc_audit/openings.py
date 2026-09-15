"""门窗规格清单：按楼层和房间统计每类门/窗的类型、宽×高与数量。

输入为已完成房间归属（``model.rooms`` 中的 ``global_ids_doors/windows``）的
核查模型，产出两部分：

* ``model.opening_items``：逐门/窗实例的明细，含归属房间、尺寸来源、异常标记；
* ``model.opening_schedule``：按 楼层 + 房间 + 门/窗 + 类型 + 宽×高 归并的门窗表。

两类问题会进入 ``model.issues``（均可在 GUI / 平面图中定位）：

* ``opening_unassigned``：门/窗没有归到任何房间（warning）；
* ``opening_size_anomaly``：宽/高缺失或小于阈值下限（warning）。

门位于两个房间边界时会计入两侧（与房间净面积清单的归属规则一致），
因此门窗表的数量按“房间行”统计，同一樘门可出现在两个房间行中。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from .model import (
    AuditModel, Issue, OpeningItem, OpeningScheduleRow, DOOR, WINDOW,
)
from .thresholds import DEFAULT_THRESHOLDS, Thresholds

KIND_CN = {"door": "门", "window": "窗"}

# IFC PredefinedType -> 中文类型（IFC4 IfcDoorTypeEnum / IfcWindowTypeEnum）
_PREDEFINED_CN = {
    "door": {
        "door": "门", "gate": "大门", "trapdoor": "翻转门",
        "double_door": "双扇门", "folding_door": "折叠门",
        "sliding_door": "推拉门", "swing_door": "平开门",
        "revolving_door": "旋转门", "rollupdoor": "卷帘门",
        "userdefined": "自定义门", "notdefined": "门",
    },
    "window": {
        "window": "窗", "skylight": "天窗", "lighthouse": "采光顶",
        "userdefined": "自定义窗", "notdefined": "窗",
    },
}

DIM_SOURCE_CN = {"overall": "IFC属性", "bbox": "几何推断", "none": "未知"}


def _type_name(elem, kind: str) -> str:
    """门/窗类型名：优先 ObjectType，其次 PredefinedType 映射，缺省为 门/窗。"""
    ot = (elem.object_type or "").strip()
    if ot:
        return ot
    pt = (elem.predefined_type or "").strip().lower()
    if pt:
        return _PREDEFINED_CN[kind].get(pt, pt)
    return KIND_CN[kind]


def _ifctype_to_kind(ifc_type: str) -> str:
    return "door" if ifc_type == DOOR else "window"


def size_label(width: float, height: float) -> str:
    """门窗表中的规格标注，如 ``900×2100``；尺寸未知标“尺寸缺失”。"""
    if width <= 1e-9 or height <= 1e-9:
        return "尺寸缺失"
    return f"{round(width * 1000):g}×{round(height * 1000):g}"


def _size_check(kind: str, width: float, height: float, th: Thresholds):
    """返回 (是否异常, 原因列表)。"""
    notes = []
    cn = KIND_CN[kind]
    if width <= 1e-9 or height <= 1e-9:
        notes.append(f"{cn}宽/高尺寸缺失")
        return True, notes
    min_w = th.door_min_width if kind == "door" else th.win_min_width
    min_h = th.door_min_height if kind == "door" else th.win_min_height
    if width < min_w - 1e-9:
        notes.append(f"宽 {width * 1000:.0f}mm < 下限 {min_w * 1000:g}mm")
    if height < min_h - 1e-9:
        notes.append(f"高 {height * 1000:.0f}mm < 下限 {min_h * 1000:g}mm")
    return (bool(notes), notes)


def build_opening_schedule(model: AuditModel,
                           th: Thresholds = DEFAULT_THRESHOLDS,
                           enabled_kinds: Optional[set[str]] = None
                           ) -> AuditModel:
    """按本次阈值生成门窗明细、门窗表与异常问题。

    门窗明细 / 门窗表始终生成；``enabled_kinds`` 给定时，被关闭核查项的
    问题不产生，明细上的异常 / 未归属标记也不计（与门禁口径一致）。
    """
    emit_unassigned = enabled_kinds is None \
        or "opening_unassigned" in enabled_kinds
    emit_anomaly = enabled_kinds is None \
        or "opening_size_anomaly" in enabled_kinds
    # 房间 GlobalId -> RoomArea（归属信息在房间清单阶段已生成）
    room_map = {r.global_id: r for r in model.rooms}

    def assigned_rooms(elem):
        """门/窗实例归属到的房间 (global_id, name) 列表。"""
        out = []
        for r in model.rooms:
            ids = (r.global_ids_doors if elem.ifc_type == DOOR
                   else r.global_ids_windows)
            if elem.global_id in ids:
                out.append((r.global_id, r.name or "(未命名房间)"))
        return out

    items: list[OpeningItem] = []
    elements = sorted(model.by_type(DOOR) + model.by_type(WINDOW),
                      key=lambda e: (e.storey, e.ifc_type, e.name or "",
                                     e.global_id))

    unassigned_seq = 1
    anomaly_seq = 1
    for e in elements:
        kind = _ifctype_to_kind(e.ifc_type)
        assigned = assigned_rooms(e)
        room_ids = [g for g, _ in assigned]
        room_names = [n for _, n in assigned]
        unassigned = (not assigned) and emit_unassigned
        anomalous, notes = _size_check(
            kind, e.width, e.dim_height, th)
        anomalous = anomalous and emit_anomaly

        item = OpeningItem(
            global_id=e.global_id,
            kind=kind,
            type_name=_type_name(e, kind),
            name=e.name,
            storey=e.storey,
            width=round(e.width, 3),
            height=round(e.dim_height, 3),
            dim_source=e.dim_source,
            rooms=room_ids,
            room_names=room_names,
            anomalous=anomalous,
            unassigned=unassigned,
            size_notes=notes,
            centroid=(float(e.centroid[0]), float(e.centroid[1])),
        )
        items.append(item)
        cn = KIND_CN[kind]

        if unassigned:
            model.issues.append(Issue(
                issue_id=f"OPN-{unassigned_seq:03d}",
                severity="warning",
                kind="opening_unassigned",
                title=f"{cn}“{e.name or e.global_id[:8]}”没有归到任何房间",
                detail=(
                    f"该{cn}的形心不在任何房间轮廓"
                    f"（归属距离 {th.assign_distance * 1000:g}mm）内，"
                    "可能位于房间外、房间缺失或归属容差过小。"
                ),
                global_ids=[e.global_id],
                location=item.centroid,
                storey=e.storey,
            ))
            unassigned_seq += 1

        if anomalous:
            spec = size_label(e.width, e.dim_height)
            model.issues.append(Issue(
                issue_id=f"OPS-{anomaly_seq:03d}",
                severity="warning",
                kind="opening_size_anomaly",
                title=f"{cn}“{e.name or e.global_id[:8]}”尺寸异常（{spec}）",
                detail=(
                    f"规格 {spec}（宽×高，来源："
                    f"{DIM_SOURCE_CN.get(e.dim_source, e.dim_source)}）；"
                    + "；".join(notes) + "。"
                    + ("" if e.dim_source != "none"
                       else "IFC 未提供 OverallWidth/OverallHeight，"
                            "且几何包围盒无法推断。")
                ),
                global_ids=[e.global_id],
                location=item.centroid,
                storey=e.storey,
                measure=min(
                    v for v in (e.width, e.dim_height) if v > 1e-9)
                if (e.width > 1e-9 or e.dim_height > 1e-9) else 0.0,
            ))
            anomaly_seq += 1

    model.opening_items = items

    # 按 楼层 + 房间 + 门/窗 + 类型 + 宽×高 归并；未归属的归入“（未归属房间）”
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    order = 0
    for item in items:
        if item.unassigned:
            room_keys = [("", "（未归属房间）")]
        else:
            room_keys = list(zip(item.rooms, item.room_names))
        for room_gid, room_name in room_keys:
            key = (item.storey, room_gid, room_name, item.kind,
                   item.type_name, item.width, item.height, item.dim_source)
            if key not in groups:
                groups[key] = {
                    "count": 0, "n_anomalous": 0, "n_unassigned": 0,
                    "gids": [], "order": order,
                }
                order += 1
            g = groups[key]
            g["count"] += 1
            g["n_anomalous"] += int(item.anomalous)
            g["n_unassigned"] += int(item.unassigned)
            g["gids"].append(item.global_id)

    rows = []
    for (storey, room_gid, room_name, kind, type_name, width, height,
         dim_src), g in groups.items():
        flags = []
        if g["n_anomalous"]:
            flags.append(f"尺寸异常×{g['n_anomalous']}")
        if g["n_unassigned"]:
            flags.append(f"未归属×{g['n_unassigned']}")
        rows.append(OpeningScheduleRow(
            kind=kind,
            storey=storey,
            room_name=room_name,
            room_global_id=room_gid,
            type_name=type_name,
            width=width,
            height=height,
            dim_source=dim_src,
            count=g["count"],
            n_anomalous=g["n_anomalous"],
            n_unassigned=g["n_unassigned"],
            notes="；".join(flags),
            global_ids=g["gids"],
        ))

    # 楼层、房间、门/窗、类型、宽高排序；未归属房间排到最后
    rows.sort(key=lambda r: (
        r.storey,
        1 if r.room_global_id == "" else 0,
        r.room_name,
        0 if r.kind == "door" else 1,
        r.type_name,
        r.width, r.height,
    ))
    model.opening_schedule = rows

    model.issues.sort(key=lambda i: (i.storey, i.kind, i.issue_id))
    return model
