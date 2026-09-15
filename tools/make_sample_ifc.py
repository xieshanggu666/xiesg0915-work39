"""生成带已知问题的样例 IFC 文件，用于端到端测试。

建筑平面（单位：米，墙厚 0.2m，层高 3m）::

    0        5       10
    +--------+--------+  y=8   W2 外墙（顶）
    |        |        |
    |  RoomA |  RoomB |
    |  40m²  |  40m²  |        W6 为室内自由墙垛（两端悬空）
    |  (40)  | (38.5) |        W5 隔墙含门 D1（另有重复门 D1b）
    |        |        |
    +--------+--------+  y=0   W1 外墙（底），D3 外门、WN1 窗
    （RoomC 在主体下方 y=-6..-3，顶部墙留 0.15m 缺口）

注入的问题：
  * W3 与 W3b 重复墙；D1 与 D1b 重复门
  * W6 两个自由端
  * W11a/W11b 端头缺口 0.15m，RoomC 围护不闭合
  * RoomB 声明面积 38.5 与几何 40 偏差；RoomC 无声明面积
  * WN3 宽 0.3m 小于默认窗宽下限 0.4m（尺寸异常，归入 RoomB）
  * D5、WN2 游离在所有房间之外（未归属门窗）
"""

from __future__ import annotations

import sys
import uuid

import ifcopenshell

T = 0.2   # 墙厚
H = 3.0   # 墙高


def guid() -> str:
    return ifcopenshell.guid.compress(uuid.uuid4().hex)


def make_sample(path: str) -> str:
    f = ifcopenshell.file(schema="IFC4")

    # 单位 / 上下文
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

    def identity_placement():
        p = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
        a = f.create_entity("IfcAxis2Placement3D", Location=p)
        return f.create_entity("IfcLocalPlacement", RelativePlacement=a)

    project = f.create_entity("IfcProject", GlobalId=guid(),
                              Name="样例项目", RepresentationContexts=(ctx,),
                              UnitsInContext=units)
    site = f.create_entity("IfcSite", GlobalId=guid(), Name="样例场地",
                           ObjectPlacement=identity_placement())
    building = f.create_entity("IfcBuilding", GlobalId=guid(), Name="样例楼",
                               ObjectPlacement=identity_placement())
    storey = f.create_entity("IfcBuildingStorey", GlobalId=guid(), Name="1F",
                             ObjectPlacement=identity_placement())
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=project, RelatedObjects=(site,))
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=site, RelatedObjects=(building,))
    f.create_entity("IfcRelAggregates", GlobalId=guid(),
                    RelatingObject=building, RelatedObjects=(storey,))

    # ---------- 几何辅助 ----------
    def identity_placement():
        p = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
        a = f.create_entity("IfcAxis2Placement3D", Location=p)
        return f.create_entity("IfcLocalPlacement", RelativePlacement=a)

    def placement(x, y, z=0.0):
        p = f.create_entity("IfcCartesianPoint",
                            Coordinates=(float(x), float(y), float(z)))
        a = f.create_entity("IfcAxis2Placement3D", Location=p)
        return f.create_entity("IfcLocalPlacement", RelativePlacement=a)

    def box_solid(x, y, z, lx, ly, lz):
        """从 (x,y,z) 起、沿 X/Y/Z 正方向的长方体挤出实体。"""
        p0 = f.create_entity("IfcCartesianPoint",
                             Coordinates=(float(lx) / 2, float(ly) / 2))
        prof_pos = f.create_entity("IfcAxis2Placement2D", Location=p0)
        profile = f.create_entity(
            "IfcRectangleProfileDef", ProfileType="AREA",
            XDim=float(lx), YDim=float(ly), Position=prof_pos)
        loc = f.create_entity("IfcCartesianPoint",
                              Coordinates=(float(x), float(y), float(z)))
        pos3 = f.create_entity("IfcAxis2Placement3D", Location=loc)
        return f.create_entity(
            "IfcExtrudedAreaSolid", SweptArea=profile, Position=pos3,
            ExtrudedDirection=f.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
            Depth=float(lz))

    def box_product(ifc_class, name, x, y, z, lx, ly, lz,
                    object_type=None, predefined_type=None,
                    overall_width=None, overall_height=None):
        # 实体位置携带世界坐标；构件本身使用单位放置，避免二次平移
        solid = box_solid(x, y, z, lx, ly, lz)
        rep = f.create_entity(
            "IfcShapeRepresentation", ContextOfItems=ctx,
            RepresentationIdentifier="Body", RepresentationType="SweptSolid",
            Items=(solid,))
        pds = f.create_entity("IfcProductDefinitionShape",
                              Representations=(rep,))
        kwargs = {}
        if predefined_type is not None:
            kwargs["PredefinedType"] = predefined_type
        if overall_width is not None:
            kwargs["OverallWidth"] = float(overall_width)
        if overall_height is not None:
            kwargs["OverallHeight"] = float(overall_height)
        prod = f.create_entity(
            ifc_class, GlobalId=guid(), Name=name,
            ObjectPlacement=identity_placement(), Representation=pds,
            ObjectType=object_type, **kwargs)
        return prod

    def wall_h(name, x0, y0, length):
        """沿 X 向墙：中心线从 (x0, y0) 起，长度 length，半厚 T/2。"""
        return box_product("IfcWall", name,
                           x0, y0 - T / 2, 0, length, T, H)

    def wall_v(name, x0, y0, length):
        """沿 Y 向墙：中心线从 (x0, y0) 起，长度 length，半厚 T/2。"""
        return box_product("IfcWall", name,
                           x0 - T / 2, y0, 0, T, length, H)

    def add_opening(host, x, y, z, lx, ly, lz, filler_class, filler_name,
                    object_type=None, predefined_type=None,
                    overall_width=None, overall_height=None):
        opening = box_product("IfcOpeningElement",
                              f"Opening-{filler_name}", x, y, z, lx, ly, lz)
        f.create_entity("IfcRelVoidsElement", GlobalId=guid(),
                        RelatingBuildingElement=host,
                        RelatedOpeningElement=opening)
        filler = box_product(filler_class, filler_name, x, y, z, lx, ly, lz,
                             object_type=object_type,
                             predefined_type=predefined_type,
                             overall_width=overall_width,
                             overall_height=overall_height)
        f.create_entity("IfcRelFillsElement", GlobalId=guid(),
                        RelatingOpeningElement=opening,
                        RelatedBuildingElement=filler)
        return filler

    products = []

    # ---------- 主体外墙 ----------
    w1 = wall_h("W1-南外墙", 0, 0, 10)
    w2 = wall_h("W2-北外墙", 0, 8, 10)
    w3 = wall_v("W3-西外墙", 0, 0, 8)
    w4 = wall_v("W4-东外墙", 10, 0, 8)
    w5 = wall_v("W5-隔墙", 5, 0, 8)
    w6 = wall_h("W6-自由墙垛", 2, 4, 2)          # 两个自由端
    w3b = wall_v("W3b-重复西外墙", 0.02, 0, 8)    # 与 W3 重复（偏移 20mm）
    products += [w1, w2, w3, w4, w5, w6, w3b]

    # ---------- RoomC（围护留缺口），墙中线对齐房间边界 ----------
    w8 = wall_v("W8", 1, -6, 3)              # 中线 x=1，y: -6..-3
    w9 = wall_v("W9", 4, -6, 3)              # 中线 x=4
    w10 = wall_h("W10", 1, -6, 3)            # 中线 y=-6，x: 1..4
    w11a = wall_h("W11a", 1, -3, 1.2)        # 中线 y=-3，x: 1.0..2.2
    w11b = wall_h("W11b", 2.35, -3, 1.65)    # x: 2.35..4.0 -> 0.15m 缺口
    products += [w8, w9, w10, w11a, w11b]

    # ---------- 门 / 窗 ----------
    # 门扇/窗扇完全填充洞口（垂直墙：x 向满墙厚；水平墙：y 向满墙厚）
    d1 = add_opening(w5, 4.9, 3.0, 0.0, 0.2, 0.9, 2.1,
                     "IfcDoor", "D1-隔墙上的门",
                     object_type="实木门", predefined_type="DOOR")
    d1b = add_opening(w5, 4.9, 3.02, 0.0, 0.2, 0.9, 2.1,
                      "IfcDoor", "D1b-重复的门")
    d3 = add_opening(w1, 2.0, -0.1, 0.0, 0.9, 0.2, 2.1,
                     "IfcDoor", "D3-外门",
                     object_type="防盗门", predefined_type="DOOR",
                     overall_width=0.9, overall_height=2.1)
    wn1 = add_opening(w1, 6.4, -0.1, 0.9, 1.2, 0.2, 1.2,
                      "IfcWindow", "WN1-外窗",
                      object_type="铝合金窗", predefined_type="WINDOW")

    # WN3-尺寸异常的高窗（宽 0.3m 小于默认下限 400mm），归入 RoomB
    wn3 = add_opening(w2, 8.0, 7.9, 1.5, 0.3, 0.2, 0.9,
                      "IfcWindow", "WN3-异常小窗",
                      object_type="高窗",
                      overall_width=0.3, overall_height=0.9)

    # D5/WN2：未挂到任何墙、也不在任何房间内的门窗（游离构件）
    d5 = box_product("IfcDoor", "D5-无归属的门", 11.0, -1.5, 0.0,
                     3.2, 0.2, 2.1,
                     object_type="超大门", predefined_type="DOOR",
                     overall_width=3.2, overall_height=2.1)
    wn2 = box_product("IfcWindow", "WN2-无归属的窗", 11.0, 4.0, 0.9,
                      1.5, 0.2, 1.5,
                      object_type="固定窗", predefined_type="WINDOW",
                      overall_width=1.5, overall_height=1.5)
    products += [d1, d1b, d3, wn1, wn3, d5, wn2]

    # ---------- 房间 ----------
    spaces = []

    def space(name, long_name, corners, declared_area=None):
        pts = [f.create_entity("IfcCartesianPoint",
                               Coordinates=(float(x), float(y)))
               for x, y in corners + corners[:1]]
        curve = f.create_entity("IfcPolyline", Points=pts)
        profile = f.create_entity(
            "IfcArbitraryClosedProfileDef", ProfileType="AREA",
            OuterCurve=curve)
        loc = f.create_entity("IfcCartesianPoint",
                              Coordinates=(0.0, 0.0, 0.0))
        solid = f.create_entity(
            "IfcExtrudedAreaSolid", SweptArea=profile,
            Position=f.create_entity("IfcAxis2Placement3D", Location=loc),
            ExtrudedDirection=f.create_entity(
                "IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
            Depth=2.8)
        rep = f.create_entity(
            "IfcShapeRepresentation", ContextOfItems=ctx,
            RepresentationIdentifier="Body", RepresentationType="SweptSolid",
            Items=(solid,))
        pds = f.create_entity("IfcProductDefinitionShape",
                              Representations=(rep,))
        sp = f.create_entity(
            "IfcSpace", GlobalId=guid(), Name=name, LongName=long_name,
            ObjectPlacement=placement(0, 0), Representation=pds)
        spaces.append(sp)
        # 房间通过 IfcRelAggregates 归入楼层（IFC 空间结构标准做法）
        f.create_entity("IfcRelAggregates", GlobalId=guid(),
                        RelatingObject=storey, RelatedObjects=(sp,))
        if declared_area is not None:
            qty = f.create_entity(
                "IfcQuantityArea", Name="NetFloorArea",
                AreaValue=float(declared_area))
            qto = f.create_entity(
                "IfcElementQuantity", GlobalId=guid(),
                Name="Qto_SpaceBaseQuantities",
                MethodOfMeasurement="BaseQuantities", Quantities=(qty,))
            f.create_entity(
                "IfcRelDefinesByProperties", GlobalId=guid(),
                RelatedObjects=(sp,), RelatingPropertyDefinition=qto)
        return sp

    space("A-101", "办公室A", [(0, 0), (5, 0), (5, 8), (0, 8)], 40.0)
    space("B-102", "办公室B", [(5, 0), (10, 0), (10, 8), (5, 8)], 38.5)
    space("C-103", "设备间", [(1, -6), (4, -6), (4, -3), (1, -3)], None)

    # 物理构件归属楼层；房间已通过 IfcRelAggregates 归入
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=guid(),
                    RelatingStructure=storey,
                    RelatedElements=tuple(products))

    f.write(path)
    return path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "output/sample.ifc"
    make_sample(out)
    print(f"样例 IFC 已生成：{out}")
