"""从 IFC 参数化表示直接解析水平轮廓。

三角网格在顶盖/底板附近受三角化方式影响会出现斜面瑕疵，而 IFC 的
``IfcExtrudedAreaSolid`` 携带干净的二维轮廓（矩形 / 任意闭合曲线 /
圆形），沿挤出方向（通常是 Z）投影即可得到精确平面轮廓。

解析失败时（BRep、曲面等）回退到网格切片法。
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon, Point

from . import geometry


def _place_xy(xy, position, scale: float):
    """把轮廓局部 XY 坐标按 IfcAxis2Placement3D/2D 变换到世界坐标。"""
    arr = np.asarray(xy, dtype=float)
    if position is None:
        return arr * scale

    loc = np.asarray(position.Location.Coordinates, dtype=float)[:2]
    # RefDirection 给出局部 X 轴方向；Y 轴由 X 逆时针旋转 90°
    ref = getattr(position, "RefDirection", None)
    if ref is not None:
        xdir = np.asarray(ref.DirectionRatios, dtype=float)[:2]
        xdir = xdir / np.linalg.norm(xdir)
        ydir = np.array([-xdir[1], xdir[0]])
        basis = np.column_stack([xdir, ydir])
        world = arr @ basis.T + loc
    else:
        world = arr + loc
    return world * scale


def _profile_polygon(profile, solid_position, scale: float):
    """IfcProfileDef -> shapely Polygon（世界坐标）。"""
    ptype = profile.is_a()

    if ptype == "IfcRectangleProfileDef":
        xdim = float(profile.XDim)
        ydim = float(profile.YDim)
        cx = cy = 0.0
        if profile.Position is not None and profile.Position.Location is not None:
            cx, cy = profile.Position.Location.Coordinates[:2]
        local = [
            (cx - xdim / 2, cy - ydim / 2),
            (cx + xdim / 2, cy - ydim / 2),
            (cx + xdim / 2, cy + ydim / 2),
            (cx - xdim / 2, cy + ydim / 2),
        ]
        world = _place_xy(local, solid_position, scale)
        return Polygon(world)

    if ptype in ("IfcArbitraryClosedProfileDef",
                 "IfcArbitraryProfileDefWithVoids"):
        outer = profile.OuterCurve
        if outer.is_a("IfcPolyline"):
            local = [p.Coordinates[:2] for p in outer.Points]
        elif outer.is_a("IfcIndexedPolyCurve"):
            seg_idx = outer.Points
            # IfcCartesianPointList2D
            pts2d = np.asarray(seg_idx.CoordList, dtype=float)
            local = pts2d.tolist()
            if local and local[0] != local[-1]:
                local.append(local[0])
        else:
            return None
        world = _place_xy(local, solid_position, scale)
        if len(world) < 4:
            return None
        poly = Polygon(world)
        # 内孔（IfcArbitraryProfileDefWithVoids）
        if ptype == "IfcArbitraryProfileDefWithVoids":
            holes = []
            for inner in profile.InnerCurves:
                if inner.is_a("IfcPolyline"):
                    hw = _place_xy([p.Coordinates[:2] for p in inner.Points],
                                   solid_position, scale)
                    holes.append(hw)
            if holes:
                poly = Polygon(world, holes)
        return poly if poly.is_valid else poly.buffer(0)

    if ptype == "IfcCircleProfileDef":
        r = float(profile.Radius)
        loc = (0.0, 0.0)
        if profile.Position is not None and profile.Position.Location is not None:
            loc = profile.Position.Location.Coordinates[:2]
        center = _place_xy([loc], solid_position, scale)[0]
        return Point(center).buffer(r * scale)

    return None


def _is_vertical_extrusion(item, scale: float) -> bool:
    d = getattr(item, "ExtrudedDirection", None)
    if d is None:
        return False
    ratios = np.asarray(d.DirectionRatios, dtype=float)
    return abs(ratios[0]) < 1e-6 and abs(ratios[1]) < 1e-6 and abs(ratios[2]) > 0.99


def analytic_footprint(element, scale: float, mode: str = "largest"):
    """尝试从参数化表示提取平面轮廓；不适用时返回 None。"""
    try:
        reps = element.Representation
        if reps is None:
            return None
        polys = []
        for rep in reps.Representations:
            for item in rep.Items:
                if not item.is_a("IfcExtrudedAreaSolid"):
                    continue
                if not _is_vertical_extrusion(item, scale):
                    continue
                poly = _profile_polygon(item.SweptArea, item.Position, scale)
                if poly is not None and not poly.is_empty and poly.area > 1e-9:
                    polys.append(poly)
        if not polys:
            return None
        from shapely.ops import unary_union
        union = unary_union(polys).buffer(0)
        if union.is_empty:
            return None
        if union.geom_type == "MultiPolygon" and mode == "largest":
            return max(union.geoms, key=lambda g: g.area)
        return union
    except Exception:
        return None


def footprint_for(element, verts, faces, minz, maxz, scale: float,
                  mode: str = "largest"):
    """优先参数化解析，失败时回退网格切片。"""
    analytic = analytic_footprint(element, scale, mode=mode)
    if analytic is not None:
        return analytic
    return geometry.triangles_to_footprint(verts, faces, minz, maxz, mode=mode)
