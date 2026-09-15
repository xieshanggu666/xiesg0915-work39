"""从 IFC 文件提取墙体、门窗、房间的几何与属性。"""

from __future__ import annotations

import numpy as np
import ifcopenshell
import ifcopenshell.geom as ifc_geom
import ifcopenshell.util.element as ifc_element

from .model import Element, AuditModel, WALL, DOOR, WINDOW, SPACE
from . import geometry, analytic
from .units import project_length_scale

_INTEREST = (WALL, DOOR, WINDOW, SPACE)


def _mesh_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """三角网格有向体积绝对值。"""
    v0 = verts[faces[:, 0]]
    v1 = verts[faces[:, 1]]
    v2 = verts[faces[:, 2]]
    cross = np.cross(v1, v2)
    vol = np.einsum("ij,ij->i", v0, cross).sum() / 6.0
    return abs(float(vol))


def _get_host_wall(elem) -> object | None:
    """门/窗通过 IfcOpeningElement 找到所属墙。"""
    fills = getattr(elem, "FillsVoids", None)
    if not fills:
        return None
    for rel in fills:
        opening = rel.RelatingOpeningElement
        voids = getattr(opening, "VoidsElements", None)
        if voids:
            return voids[0].RelatingBuildingElement
    return None


def _container_name(elem) -> str:
    """获取构件所在楼层名（兼容 ContainedInStructure 与 Decomposes 两种归属）。"""
    container = ifc_element.get_container(elem)
    if container is not None:
        return container.Name or ""
    # IfcSpace 常通过 IfcRelAggregates(Decomposes) 归入楼层
    decomposes = getattr(elem, "Decomposes", None)
    if decomposes:
        relating = getattr(decomposes[0], "RelatingObject", None)
        if relating is not None and relating.is_a("IfcSpatialStructureElement"):
            return relating.Name or ""
    return ""


def _declared_net_area(elem) -> float:
    """从 Qto_SpaceBaseQuantities 读取声明的净楼面面积。"""
    try:
        qsets = ifc_element.get_psets(elem, qtos_only=True)
    except Exception:
        return 0.0
    for qset in qsets.values():
        for key, val in qset.items():
            if key.lower() == "netfloorarea" and isinstance(val, (int, float)):
                return float(val)
    return 0.0


def _overall_dimensions(elem, scale: float):
    """读取门/窗标称宽高 (m)。

    优先 IFC4 属性 OverallWidth / OverallHeight（IFC2x3 无，回退包围盒）；
    属性缺失或非正数时按水平包围盒取宽、竖直包围盒取高。
    返回 (width, height, source)，source 为 overall / bbox / none。
    """
    width = float(getattr(elem, "OverallWidth", 0.0) or 0.0) * scale
    height = float(getattr(elem, "OverallHeight", 0.0) or 0.0) * scale
    if width > 1e-9 and height > 1e-9:
        return width, height, "overall"
    return 0.0, 0.0, "none"


def extract(file_path: str) -> AuditModel:
    """读取并解析整个 IFC 文件，返回 AuditModel（几何单位：米）。"""
    ifc_file = ifcopenshell.open(file_path)
    scale = project_length_scale(ifc_file)

    model = AuditModel(file_path=file_path, unit_scale=scale)

    settings = ifc_geom.settings()
    # 只取几何，不需要材质/层信息，速度更快
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except Exception:
        pass

    elements = ifc_file.by_type("IfcProduct")
    for elem in elements:
        ifc_type = elem.is_a()
        # 取最具体的标准类别（如 IfcWallStandardCase -> IfcWall）
        for base in _INTEREST:
            if elem.is_a(base):
                ifc_type = base
                break
        else:
            continue

        shape = None
        try:
            shape = ifc_geom.create_shape(settings, elem)
        except Exception:
            shape = None

        if shape is None:
            # 没有几何的构件也登记，便于后续报告“无几何构件”
            geom_ok = False
            verts = np.zeros((0, 3))
            faces = np.zeros((0, 3), dtype=int)
        else:
            geom_ok = True
            g = shape.geometry
            verts = np.asarray(g.verts, dtype=float).reshape(-1, 3) * scale
            faces = np.asarray(g.faces, dtype=int).reshape(-1, 3)

        if geom_ok and len(verts):
            mn, mx = verts.min(axis=0), verts.max(axis=0)
            bounds = (float(mn[0]), float(mn[1]), float(mn[2]),
                      float(mx[0]), float(mx[1]), float(mx[2]))
            centroid = verts.mean(axis=0)
            volume = _mesh_volume(verts, faces)
            # 房间取全截面并集；墙/门/窗取最大截面
            mode = "union" if ifc_type == SPACE else "largest"
            sliced = analytic.footprint_for(
                elem, verts, faces, float(mn[2]), float(mx[2]),
                scale, mode=mode)
            hull = geometry.convex_hull_footprint(verts)
        else:
            bounds = (0.0,) * 6
            centroid = np.zeros(3)
            volume = 0.0
            sliced = hull = None

        # 围护分析语义：墙=门楣上方完整墙身；门扇用凸包覆盖门洞；
        # 窗不参与围护（窗台以上采光）。
        if ifc_type == DOOR:
            footprint = hull
        else:
            footprint = sliced

        try:
            storey = _container_name(elem)
        except Exception:
            storey = ""

        e = Element(
            global_id=elem.GlobalId,
            ifc_type=ifc_type,
            name=elem.Name or "",
            object_type=getattr(elem, "ObjectType", "") or "",
            predefined_type=getattr(elem, "PredefinedType", "") or "",
            storey=storey,
            bounds=bounds,
            centroid=centroid,
            volume=volume,
            footprint=footprint,
            hull=hull,
            raw=elem,
        )

        if ifc_type == WALL and footprint is not None:
            axis, thick_dir, thickness = geometry.axis_from_footprint(footprint)
            e.axis = axis
            e.thick_dir = thick_dir
            e.thickness = thickness
            e.base_elevation = bounds[2]
            e.height = bounds[5] - bounds[2]

        if ifc_type in (DOOR, WINDOW):
            host = _get_host_wall(elem)
            e.hosted_by_wall = host.GlobalId if host is not None else None
            width, height, dim_src = _overall_dimensions(elem, scale)
            if (width <= 1e-9 or height <= 1e-9) and geom_ok:
                # IFC 属性缺失：按包围盒推断。门窗沿墙方向的洞口尺寸通常
                # 大于墙厚方向，故水平方向取长边为宽，竖直方向为高。
                bx = bounds[3] - bounds[0]
                by = bounds[4] - bounds[1]
                bz = bounds[5] - bounds[2]
                width, height = max(bx, by), bz
                dim_src = "bbox" if width > 1e-9 and height > 1e-9 else "none"
            e.width, e.dim_height, e.dim_source = width, height, dim_src

        model.elements[e.global_id] = e

    model._declared_areas = {
        e.global_id: _declared_net_area(e.raw)
        for e in model.by_type(SPACE)
        if e.raw is not None
    }
    return model
