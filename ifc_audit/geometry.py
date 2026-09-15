"""几何工具：三角网格切片与平面轮廓提取。

核查以二维水平面（平面图）为主，因此把每个构件的三维网格在指定高度切片，
得到若干线段，再用 shapely.polygonize 拼合成多边形轮廓。
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import (
    Polygon, MultiPolygon, LineString, MultiLineString, MultiPoint)
from shapely.ops import polygonize_full, unary_union, linemerge


def _lines_to_polys(segments: list[np.ndarray], snap: float = 1e-6):
    """切片线段 -> 多边形。

    三角形交线的端点存在浮点级错位：先把坐标吸附到细网格，再 linemerge
    拼成完整边界，最后 polygonize。
    """
    if snap:
        segments = [np.round(sg / snap) * snap for sg in segments]
    lines = [LineString(sg) for sg in segments]
    merged = linemerge(unary_union(lines))
    if merged.is_empty:
        return []
    if merged.geom_type == "LineString":
        merged = MultiLineString([merged])
    result, _dangles, _cuts, _invalids = polygonize_full(merged)
    return list(result.geoms) if hasattr(result, "geoms") else []


def slice_triangles_at_z(
    verts: np.ndarray, faces: np.ndarray, z: float, tol: float = 1e-9
) -> list[np.ndarray]:
    """三角形网格在水平面 z 处切片，返回交线线段数组，每条形如 ((x1,y1),(x2,y2))。

    只处理严格跨越 z 的三角形（顶点恰好在切面上的退化情形会被两侧切片
    重复覆盖，因此不选含顶点在平面上的三角形），保证交面轮廓干净。
    """
    if faces is None or len(faces) == 0:
        return []
    v0 = verts[faces[:, 0]]
    v1 = verts[faces[:, 1]]
    v2 = verts[faces[:, 2]]

    z0, z1, z2 = v0[:, 2], v1[:, 2], v2[:, 2]
    d0, d1, d2 = z0 - z, z1 - z, z2 - z
    # 严格跨越：同时存在平面上方与下方的顶点
    above = (d0 > tol).astype(int) + (d1 > tol).astype(int) + (d2 > tol).astype(int)
    below = (d0 < -tol).astype(int) + (d1 < -tol).astype(int) + (d2 < -tol).astype(int)
    idx = np.where((above > 0) & (below > 0))[0]

    def intersect(p, q):
        t = (z - p[2]) / (q[2] - p[2])
        return p[:2] + t * (q[:2] - p[:2])

    segments: list[np.ndarray] = []
    for i in idx:
        tri = (v0[i], v1[i], v2[i])
        pts = []
        for a in range(3):
            p, q = tri[a], tri[(a + 1) % 3]
            if (p[2] - z) * (q[2] - z) < -tol * tol:  # 两端分居平面两侧
                pts.append(intersect(p, q))
        if len(pts) >= 2:
            segments.append(np.array([pts[0], pts[-1]]))
    return segments


def triangles_to_footprint(
    verts: np.ndarray, faces: np.ndarray, minz: float, maxz: float,
    mode: str = "largest",
) -> Polygon | MultiPolygon | None:
    """提取构件水平轮廓。

    在构件高度内密集取水平切片并多边形化。

    Args:
        mode:
            * ``"largest"``（墙/门/窗）：取面积最大的截面。带门墙在门楣
              上方恢复完整墙身，面积最大，同时自动避开顶盖/底板三角化出
              的斜面区域，截面是轴对齐的干净矩形；
            * ``"union"``（房间）：取全部截面并集。
    """
    height = maxz - minz
    if height < 1e-9 or len(verts) == 0:
        return None

    # 21 个切片，跨越门窗洞口；避开 0/1 端面
    polys_all = []
    for r in np.linspace(0.05, 0.95, 21):
        z = minz + float(r) * height
        segs = slice_triangles_at_z(verts, faces, z)
        if not segs:
            continue
        polys = _lines_to_polys(segs)
        if polys:
            polys_all.append(unary_union(polys).buffer(0))

    if polys_all:
        if mode == "largest":
            footprint = max(polys_all, key=lambda g: g.area)
        else:
            footprint = unary_union(polys_all).buffer(0)
    else:
        footprint = MultiPoint(verts[:, :2]).convex_hull

    if footprint.is_empty or footprint.area <= 1e-6:
        return None
    if footprint.geom_type == "Polygon":
        return footprint
    kept = [g for g in footprint.geoms if g.area > 1e-6]
    if not kept:
        return None
    return kept[0] if len(kept) == 1 else MultiPolygon(kept)


def convex_hull_footprint(verts: np.ndarray) -> Polygon | None:
    """构件全部顶点投影到 XY 平面的凸包（用于重复构件检测，洞口封闭）。"""
    if len(verts) == 0:
        return None
    hull = MultiPoint(verts[:, :2]).convex_hull
    return hull if hull.area > 1e-6 else None


def axis_from_footprint(
    footprint: Polygon | MultiPolygon,
) -> tuple[LineString | None, np.ndarray | None, float]:
    """从长条形墙轮廓提取中轴线、厚度方向、厚度（PCA 法）。

    返回 (axis, thick_dir, thickness)。非长条形返回 (None, None, 0)。
    """
    if footprint is None:
        return None, None, 0.0
    if footprint.geom_type == "MultiPolygon":
        geom = max(footprint.geoms, key=lambda g: g.area)
    else:
        geom = footprint

    coords = np.asarray(geom.minimum_rotated_rectangle.exterior.coords)
    if len(coords) < 5:
        rect = coords
    else:
        rect = coords
    # minimum_rotated_rectangle 为闭合四边形，边长排序得到长边/短边
    edges = np.diff(rect, axis=0)
    lengths = np.linalg.norm(edges, axis=1)
    order = np.argsort(lengths)
    long_edge = edges[order[-1]]
    axis_dir = long_edge[:2] / np.linalg.norm(long_edge[:2])
    thickness = float(lengths[order[0]])
    length = float(lengths[order[-1]])

    if length < thickness * 1.2:
        return None, None, 0.0  # 不像墙（长宽比太小）

    thick_dir = np.array([-axis_dir[1], axis_dir[0]])
    c = np.asarray(geom.centroid.coords[0])
    axis = LineString([
        c - axis_dir * length / 2,
        c + axis_dir * length / 2,
    ])
    return axis, thick_dir, thickness
