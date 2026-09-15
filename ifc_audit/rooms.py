"""房间净面积清单：优先采用 IFC 声明值，缺失时由几何计算。"""

from __future__ import annotations

from typing import Optional

from shapely.geometry import Point

from .model import AuditModel, RoomArea, Issue, DOOR, WINDOW
from .thresholds import DEFAULT_THRESHOLDS, Thresholds


def _distance_to_room(point_xy, room_elem) -> float:
    """门窗形心到房间轮廓的最近距离（轮廓内部为 0）。"""
    if room_elem.footprint is None:
        return 1e9
    return float(room_elem.footprint.distance(Point(point_xy)))


def build_room_areas(model: AuditModel,
                     th: Thresholds = DEFAULT_THRESHOLDS,
                     enabled_kinds: Optional[set[str]] = None) -> AuditModel:
    """按本次阈值统计房间净面积、门窗归属与面积偏差。

    房间清单与门窗归属始终计算（门窗表依赖）；``enabled_kinds`` 给定时
    只抑制面积类问题的产生（缺声明 / 偏差）。
    """
    emit_missing = enabled_kinds is None or "area_missing_declared" in enabled_kinds
    emit_mismatch = enabled_kinds is None or "area_mismatch" in enabled_kinds
    declared = getattr(model, "_declared_areas", {})
    doors = model.by_type(DOOR)
    windows = model.by_type(WINDOW)
    assign_tol = th.assign_distance

    for room in model.by_type("IfcSpace"):
        gid = room.global_id
        fp = room.footprint
        computed = float(fp.area) if fp is not None else 0.0
        declared_area = float(declared.get(gid, 0.0))

        if declared_area > 0:
            source, net = "declared", declared_area
            deviation = (abs(declared_area - computed) / computed
                         if computed > 1e-9 else 0.0)
        else:
            source, net, deviation = "geometry", computed, 0.0

        # 门位于两个房间边界上：形心落在轮廓内或距边界足够近即归入
        room_doors = [d.global_id for d in doors
                      if _distance_to_room(d.centroid[:2], room) <= assign_tol]
        room_windows = [w.global_id for w in windows
                        if _distance_to_room(w.centroid[:2], room) <= assign_tol]

        perimeter = float(fp.length) if fp is not None else 0.0
        # 三态：无几何的房间无法检查围护，标记为 unchecked，不得给出闭合结论
        if fp is None:
            enclosure_status = "unchecked"
        elif model.room_gaps.get(gid):
            enclosure_status = "open"
        else:
            enclosure_status = "closed"

        model.rooms.append(RoomArea(
            global_id=gid,
            name=room.name,
            long_name=getattr(room.raw, "LongName", "") or "" if room.raw else "",
            storey=room.storey,
            net_area=round(net, 3),
            declared_area=round(declared_area, 3),
            computed_area=round(computed, 3),
            area_source=source,
            deviation=round(deviation, 4),
            bounds=room.bounds,
            centroid=(float(room.centroid[0]), float(room.centroid[1])),
            doors=len(room_doors),
            windows=len(room_windows),
            enclosure_status=enclosure_status,
            perimeter=round(perimeter, 3),
            global_ids_doors=room_doors,
            global_ids_windows=room_windows,
        ))

    model.rooms.sort(key=lambda r: (r.storey, r.name))

    seq = 1
    for r in model.rooms:
        if not r.declared_area:
            if not emit_missing:
                continue
            model.issues.append(Issue(
                issue_id=f"AREA-{seq:03d}",
                severity="info",
                kind="area_missing_declared",
                title=f"房间“{r.name}”无声明净面积，已按几何计算 {r.net_area:.2f} m²",
                detail=(
                    "IFC 中未找到 Qto_SpaceBaseQuantities.NetFloorArea，"
                    "净面积由房间平面轮廓几何计算得到。"
                ),
                global_ids=[r.global_id],
                location=r.centroid,
                storey=r.storey,
                measure=r.net_area,
            ))
            seq += 1
        elif emit_mismatch and r.deviation > th.area_dev_warn:
            model.issues.append(Issue(
                issue_id=f"AREA-{seq:03d}",
                severity="warning",
                kind="area_mismatch",
                title=f"房间“{r.name}”声明净面积与几何值偏差 {r.deviation*100:.1f}%",
                detail=(
                    f"声明净面积 {r.declared_area:.2f} m²，"
                    f"几何计算 {r.computed_area:.2f} m²，"
                    f"偏差超过警告线 {th.area_dev_warn*100:g}%。"
                ),
                global_ids=[r.global_id],
                location=r.centroid,
                storey=r.storey,
                measure=r.deviation,
            ))
            seq += 1

    model.issues.sort(key=lambda i: (i.storey, i.kind, i.issue_id))
    return model
