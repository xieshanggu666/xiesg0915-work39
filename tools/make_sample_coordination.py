"""生成多专业协同核查样例 IFC（建筑 / 结构 / 机电三份，共用同一坐标系）。

平面（单位：米，层高 3m）::

    0        5        10
    +-----------------+  y=6  建筑外墙 W-north
    |                 |
    |   [B1 梁 x:2..8]|      y=4.5，梁高 0.5m（z:2.6..3.1 穿层）
    |   ║P2           |      P2 竖向风管穿梁（硬碰撞）
    |   P1  P3   P4   |
    +-----------------+  y=0  建筑外墙 W-south
    楼板 slab z:0..0.15

注入的协同问题：

* P1 水管（y=1.5）穿南墙：墙有洞口且尺寸合格 → 无问题；
* P2 风管（x=3）穿过结构梁 B1 → **硬碰撞**；
* P3 水管（x=6）穿楼板：洞口预留过小（Ø100 vs Ø300）→ **洞口规格不符**；
* P4 风管（x=8.5）穿南墙：墙上无对应洞口 → **洞口缺失**；
* 南墙另有一处预留洞口 OP-UNUSED 无任何管线 → **洞口未使用**。

用法::

    python tools/make_sample_coordination.py output/coord_sample
"""

from __future__ import annotations

import os
import sys
import uuid

import ifcopenshell


def _guid_for(name: str):
    """按构件名生成稳定 GUID（测试跨批次合单时同名构件 GlobalId 一致）。"""
    return ifcopenshell.guid.compress(
        __import__("hashlib").sha1(
            f"coord-sample:{name}".encode("utf-8")).hexdigest())


# 测试可注入的固定 GUID 表：{构件名: guid}；为空时按构件名生成稳定 GUID。
# 协同台账按“问题类型+构件 GlobalId”合单，因此同批构件跨批次必须同名同 GUID。
PINNED_GUIDS: dict[str, str] = {}


def guid() -> str:
    return ifcopenshell.guid.compress(uuid.uuid4().hex)


def _id_for(name: str) -> str:
    return PINNED_GUIDS.get(name) or _guid_for(name)


def _new_file(project_name: str, storey_name: str = "1F"):
    """创建最小 IFC4 文件（项目/场地/建筑/楼层），返回 (file, ctx, storey)。"""
    f = ifcopenshell.file(schema="IFC4")
    origin = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    axis = f.create_entity("IfcAxis2Placement3D", Location=origin)
    ctx = f.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model", ContextIdentifier="Model",
        CoordinateSpaceDimension=3, Precision=1.0e-5,
        WorldCoordinateSystem=axis,
        TrueNorth=f.create_entity("IfcDirection", DirectionRatios=(0.0, 1.0)),
    )
    metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = f.create_entity("IfcUnitAssignment", Units=(metre,))
    project = f.create_entity(
        "IfcProject", GlobalId=guid(), Name=project_name,
        RepresentationContexts=(ctx,), UnitsInContext=units)
    site = f.create_entity("IfcSite", GlobalId=guid(), Name="样例场地")
    building = f.create_entity("IfcBuilding", GlobalId=guid(), Name="样例楼")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=guid(),
                             Name=storey_name)
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=project, RelatedObjects=(site,))
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=site, RelatedObjects=(building,))
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=building, RelatedObjects=(storey,))
    return f, ctx, storey


def _identity(f):
    p = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    return f.create_entity("IfcLocalPlacement",
                           RelativePlacement=f.create_entity(
                               "IfcAxis2Placement3D", Location=p))


def _box_solid(f, x, y, z, lx, ly, lz):
    p0 = f.create_entity("IfcCartesianPoint",
                         Coordinates=(float(lx) / 2, float(ly) / 2))
    prof = f.create_entity(
        "IfcRectangleProfileDef", ProfileType="AREA",
        XDim=float(lx), YDim=float(ly),
        Position=f.create_entity("IfcAxis2Placement2D", Location=p0))
    loc = f.create_entity("IfcCartesianPoint",
                          Coordinates=(float(x), float(y), float(z)))
    return f.create_entity(
        "IfcExtrudedAreaSolid", SweptArea=prof,
        Position=f.create_entity("IfcAxis2Placement3D", Location=loc),
        ExtrudedDirection=f.create_entity(
            "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=float(lz))


def _product(f, ctx, storey, ifc_class, name, x, y, z, lx, ly, lz,
             object_type=None, predefined=None, contained=True):
    rep = f.create_entity(
        "IfcShapeRepresentation", ContextOfItems=ctx,
        RepresentationIdentifier="Body", RepresentationType="SweptSolid",
        Items=(_box_solid(f, x, y, z, lx, ly, lz),))
    pds = f.create_entity("IfcProductDefinitionShape", Representations=(rep,))
    kwargs = {}
    if predefined is not None:
        kwargs["PredefinedType"] = predefined
    prod = f.create_entity(
        ifc_class, GlobalId=_id_for(name), Name=name,
        ObjectPlacement=_identity(f),
        Representation=pds, ObjectType=object_type, **kwargs)
    if contained:
        f.create_entity(
            "IfcRelContainedInSpatialStructure", GlobalId=guid(),
            RelatingStructure=storey, RelatedElements=(prod,))
    return prod


def _pipe(f, ctx, storey, name, x0, y0, x1, y1, z, diameter,
          object_type="给水管"):
    """水平管段（AABB 盒子近似，直径 diameter）。"""
    xmin, xmax = sorted((x0, x1))
    ymin, ymax = sorted((y0, y1))
    if abs(xmax - xmin) >= abs(ymax - ymin):
        return _product(f, ctx, storey, "IfcPipeSegment", name,
                        xmin, ymin - diameter / 2, z - diameter / 2,
                        xmax - xmin, diameter, diameter,
                        object_type=object_type, predefined="RIGIDSEGMENT")
    return _product(f, ctx, storey, "IfcPipeSegment", name,
                    xmin - diameter / 2, ymin, z - diameter / 2,
                    diameter, ymax - ymin, diameter,
                    object_type=object_type, predefined="RIGIDSEGMENT")


def _duct(f, ctx, storey, name, x0, y0, z0, x1, y1, z1, w, h,
          object_type="送风风管"):
    """风管（矩形截面，沿长轴的 AABB 盒子；w/h 为另外两轴的截面尺寸）。"""
    xmin, xmax = sorted((x0, x1))
    ymin, ymax = sorted((y0, y1))
    zmin, zmax = sorted((z0, z1))
    dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin
    if dx >= max(dy, dz):
        return _product(f, ctx, storey, "IfcDuctSegment", name,
                        xmin, ymin - h / 2, zmin - w / 2,
                        dx, h, w,
                        object_type=object_type, predefined="RIGIDSEGMENT")
    if dy >= dz:
        return _product(f, ctx, storey, "IfcDuctSegment", name,
                        xmin - w / 2, ymin, zmin - h / 2,
                        w, dy, h,
                        object_type=object_type, predefined="RIGIDSEGMENT")
    return _product(f, ctx, storey, "IfcDuctSegment", name,
                    xmin - w / 2, ymin - h / 2, zmin,
                    w, h, dz,
                    object_type=object_type, predefined="RIGIDSEGMENT")


def _wall_with_openings(f, ctx, storey, name, x0, y0, length,
                        horizontal=True, openings=()):
    """带洞口的墙：洞口用 IfcRelVoidsElement 挂接（不实际布尔，检测按 AABB）。"""
    T = 0.2
    H = 3.0
    if horizontal:
        wall = _product(f, ctx, storey, "IfcWall", name,
                        x0, y0 - T / 2, 0, length, T, H)
    else:
        wall = _product(f, ctx, storey, "IfcWall", name,
                        x0 - T / 2, y0, 0, T, length, H)
    for op in openings:
        ox, oy, oz, lx, ly, lz, op_name = op
        opening = _product(f, ctx, storey, "IfcOpeningElement", op_name,
                           ox, oy, oz, lx, ly, lz, contained=False)
        f.create_entity("IfcRelVoidsElement", GlobalId=guid(),
                        RelatingBuildingElement=wall,
                        RelatedOpeningElement=opening)
    return wall


# ---------------------------------------------------------------- 建筑 ----

def make_arch(path: str) -> str:
    f, ctx, storey = _new_file("协同样例-建筑")
    # 南墙（y=0）：P1 合格洞口、P4 无洞口、另有一处闲置洞口
    _wall_with_openings(
        f, ctx, storey, "W-S-南外墙", 0, 0, 10, horizontal=True,
        openings=[
            # P1 水管 y 向穿南墙（x≈1.5），Ø0.15 管 -> 洞口 0.4×0.4 合格
            (1.5 - 0.2, -0.1, 2.6 - 0.2, 0.4, 0.2, 0.4,
             "OP-P1-南墙合格洞口"),
            # 闲置洞口（x≈9.6，管廊外，无管线穿越）
            (9.6 - 0.2, -0.1, 2.2 - 0.2, 0.4, 0.2, 0.4,
             "OP-UNUSED-闲置洞口"),
        ])
    _wall_with_openings(f, ctx, storey, "W-N-北外墙", 0, 6, 10)
    _wall_with_openings(f, ctx, storey, "W-W-西外墙", 0, 0, 6,
                        horizontal=False)
    _wall_with_openings(f, ctx, storey, "W-E-东外墙", 10, 0, 6,
                        horizontal=False)
    # 楼板 z:0..0.15：P3 穿楼板处洞口过小（Ø0.1）
    slab = _product(f, ctx, storey, "IfcSlab", "SLAB-1F楼板",
                    0, 0, -0.15, 10, 6, 0.15, predefined="FLOOR")
    op = _product(f, ctx, storey, "IfcOpeningElement",
                  "OP-P3-楼板小洞", 6 - 0.05, 2.5 - 0.05, -0.15,
                  0.1, 0.1, 0.15, contained=False)
    f.create_entity("IfcRelVoidsElement", GlobalId=guid(),
                    RelatingBuildingElement=slab, RelatedOpeningElement=op)
    f.write(path)
    return path


# ---------------------------------------------------------------- 结构 ----

def make_struct(path: str) -> str:
    f, ctx, storey = _new_file("协同样例-结构")
    # 框架梁 x:2..8，y=4.5，截面 0.3(宽 y) × 0.5(高 z)，梁底 2.6m
    _product(f, ctx, storey, "IfcBeam", "B1-横向框架梁",
             2, 4.5 - 0.15, 2.6, 6, 0.3, 0.5,
             object_type="框架梁300x500")
    # 两根柱（不和任何管线冲突）
    _product(f, ctx, storey, "IfcColumn", "C1-角柱",
             0.1, 0.1, 0, 0.4, 0.4, 3.0)
    _product(f, ctx, storey, "IfcColumn", "C2-角柱",
             9.5, 5.5, 0, 0.4, 0.4, 3.0)
    f.write(path)
    return path


# ---------------------------------------------------------------- 机电 ----

def make_mep(path: str) -> str:
    f, ctx, storey = _new_file("协同样例-机电")
    # P1：给水管 Ø0.15，y 向穿南墙 x≈1.5（墙上洞口合格）-> 无问题
    _pipe(f, ctx, storey, "P1-给水入户管",
          1.5, -0.6, 1.5, 5.5, z=2.6, diameter=0.15)
    # P2：竖向矩形风管 0.4×0.3，穿过梁 B1（梁 z:2.6..3.1，风管 z:2.5..2.9）-> 硬碰撞
    _duct(f, ctx, storey, "P2-排风竖管穿梁",
          3, 4.5, 0.0, 3, 4.5, 3.0, w=0.4, h=0.3,
          object_type="排风风管400x300")
    # P3：排水管 Ø0.3，竖向穿楼板（洞口仅 Ø0.1）-> 洞口规格不符
    _duct(f, ctx, storey, "P3-排水立管",
          6, 2.5, -0.4, 6, 2.5, 2.6, w=0.3, h=0.3,
          object_type="排水管DN300")
    # P4：风管 0.32（宽 x）×0.25（高 z），y 向穿南墙 x≈8.5，墙上无洞口 -> 洞口缺失
    _duct(f, ctx, storey, "P4-新风入户管无洞",
          8.5, -0.5, 2.275, 8.5, 5.0, 2.525, w=0.32, h=0.25,
          object_type="新风风管320x250")
    f.write(path)
    return path


def make_coordination_sample(out_dir: str) -> dict[str, str]:
    """在 out_dir 生成建筑/结构/机电三份样例 IFC，返回路径字典。"""
    os.makedirs(out_dir, exist_ok=True)
    return {
        "arch": make_arch(os.path.join(out_dir, "样例-建筑模型.ifc")),
        "struct": make_struct(os.path.join(out_dir, "样例-结构模型.ifc")),
        "mep": make_mep(os.path.join(out_dir, "样例-机电模型.ifc")),
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "output/coord_sample"
    paths = make_coordination_sample(out)
    for disc, p in paths.items():
        print(f"{disc}: {p}")
