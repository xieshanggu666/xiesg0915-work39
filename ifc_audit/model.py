"""核查数据模型。

所有几何量均投影到建筑水平面（IfcProject 的 XY 平面，通常为 Z 轴法向），
单位统一换算成米。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString

# 支持的 IFC 构件类别
WALL = "IfcWall"
DOOR = "IfcDoor"
WINDOW = "IfcWindow"
SPACE = "IfcSpace"


@dataclass
class Element:
    """提取出的单个构件。"""

    global_id: str
    ifc_type: str
    name: str
    object_type: str = ""
    predefined_type: str = ""
    storey: str = ""

    # 包围盒（世界坐标，米），(minx, miny, minz, maxx, maxy, maxz)
    bounds: tuple[float, float, float, float, float, float] = (0,) * 6
    centroid: np.ndarray = field(default_factory=lambda: np.zeros(3))
    volume: float = 0.0

    # 水平面轮廓（shapely，单位米）；墙/房间均有，门窗可为 None
    footprint: Optional[Polygon | MultiPolygon] = None
    # 封闭洞口的凸包轮廓（重复构件检测用）
    hull: Optional[Polygon] = None

    # 墙体专用：中轴线（含门洞口的墙体会是 None）
    axis: Optional[LineString] = None
    base_elevation: float = 0.0
    height: float = 0.0
    thickness: float = 0.0

    # 墙的厚度方向（水平单位向量），用于自由端判定
    thick_dir: Optional[np.ndarray] = None

    # 围护关系（ifopenshell inverse attr）
    hosted_by_wall: Optional[str] = None  # 门窗所属墙的 GlobalId

    # 门窗专用：标称宽/高（米），优先取 IFC OverallWidth/OverallHeight，
    # 缺失时按几何包围盒推断；均无法得到时为 0
    width: float = 0.0
    dim_height: float = 0.0
    # 尺寸来源：overall=IFC 属性 / bbox=包围盒推断 / none=未知
    dim_source: str = "none"

    # 原始 IFC 实体（交互定位时用，不参与序列化）
    raw: object = field(default=None, repr=False)

    @property
    def key(self) -> str:
        return f"{self.ifc_type}:{self.global_id}"

    @property
    def label(self) -> str:
        """界面显示名。"""
        cn = {"IfcWall": "墙", "IfcDoor": "门", "IfcWindow": "窗",
              "IfcSpace": "房间"}.get(self.ifc_type, self.ifc_type)
        return f"{cn} | {self.name or '(未命名)'} | {self.global_id[:8]}"


@dataclass
class Issue:
    """一条核查问题。"""

    issue_id: str
    severity: str            # error / warning / info
    kind: str                # 问题类型标识
    title: str               # 一句话描述
    detail: str              # 详细说明
    global_ids: list[str]    # 关联构件（点击定位用）
    location: tuple[float, float]          # 标注图上的位置 (x, y) 米
    storey: str = ""
    measure: float = 0.0     # 量化指标（缺口长度 / 重合体积比等）

    @property
    def label(self) -> str:
        return f"[{self.severity.upper()}] {self.title}"


@dataclass
class RoomArea:
    """房间净面积清单项。"""

    global_id: str
    name: str
    storey: str
    long_name: str
    net_area: float            # 采用的净面积 m²
    declared_area: float       # IFC 中声明的 NetFloorArea（无则 0）
    computed_area: float       # 由几何计算的面积 m²
    area_source: str           # declared / geometry
    deviation: float           # 声明值与计算值相对偏差（无则 0）
    bounds: tuple[float, float, float, float, float, float]
    centroid: tuple[float, float]
    doors: int
    windows: int
    enclosure_status: str      # closed=闭合 / open=不闭合 / unchecked=未检查(无几何)
    perimeter: float
    global_ids_doors: list[str] = field(default_factory=list)
    global_ids_windows: list[str] = field(default_factory=list)

    @property
    def enclosed(self) -> bool:
        """围护闭合（未检查按不闭合处理，仅供布尔判断用）。"""
        return self.enclosure_status == "closed"

    @property
    def enclosure_label(self) -> str:
        return {"closed": "是", "open": "否", "unchecked": "未检查"}.get(
            self.enclosure_status, "未检查")


@dataclass
class OpeningItem:
    """门窗明细中的单个门/窗实例。"""

    global_id: str
    kind: str                # door / window
    type_name: str           # 类型（ObjectType / PredefinedType 映射，缺省为 门/窗）
    name: str
    storey: str
    width: float             # m，0 表示尺寸未知
    height: float            # m
    dim_source: str          # overall / bbox / none
    rooms: list[str] = field(default_factory=list)    # 归属房间 GlobalId
    room_names: list[str] = field(default_factory=list)
    anomalous: bool = False  # 尺寸异常或缺失
    unassigned: bool = False  # 没有归到任何房间
    size_notes: list[str] = field(default_factory=list)  # 异常原因
    centroid: tuple[float, float] = (0.0, 0.0)


@dataclass
class OpeningScheduleRow:
    """门窗表中的一行：楼层 + 房间 + 类型 + 宽×高 归并后的数量统计。"""

    kind: str
    storey: str
    room_name: str          # 未归属时为“（未归属房间）”
    room_global_id: str
    type_name: str
    width: float
    height: float
    dim_source: str
    count: int
    n_anomalous: int
    n_unassigned: int
    notes: str
    global_ids: list[str] = field(default_factory=list)


@dataclass
class AuditModel:
    """一次核查的完整结果。"""

    file_path: str
    unit_scale: float
    elements: dict[str, Element] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    rooms: list[RoomArea] = field(default_factory=list)

    # 分析中间数据：房间 -> 围护缺口线段列表
    room_gaps: dict[str, list] = field(default_factory=dict)
    # 重复构件分组
    duplicate_groups: list[list[str]] = field(default_factory=list)

    # 门窗规格清单：逐实例明细 + 按楼层/房间归并的门窗表
    opening_items: list[OpeningItem] = field(default_factory=list)
    opening_schedule: list[OpeningScheduleRow] = field(default_factory=list)

    # 本次核查使用的判定阈值及来源（报告中注明）
    thresholds: object = None
    threshold_provenance: object = None

    # 本次核查使用的企业规则包引用（RulePackRef；未使用规则包时为 None）
    rule_pack: object = None
    # 本次启用的问题种类集合（规则包关闭部分核查项时收窄；None=全部启用）
    enabled_kinds: object = None

    def by_type(self, ifc_type: str) -> list[Element]:
        return [e for e in self.elements.values() if e.ifc_type == ifc_type]

    def wall_ids(self) -> list[str]:
        return [e.global_id for e in self.by_type(WALL)]

    def summary(self) -> dict:
        n_err = sum(1 for i in self.issues if i.severity == "error")
        n_warn = sum(1 for i in self.issues if i.severity == "warning")
        return {
            "file": self.file_path,
            "walls": len(self.by_type(WALL)),
            "doors": len(self.by_type(DOOR)),
            "windows": len(self.by_type(WINDOW)),
            "rooms": len(self.by_type(SPACE)),
            "issues": len(self.issues),
            "errors": n_err,
            "warnings": n_warn,
            "duplicate_groups": len(self.duplicate_groups),
            "total_net_area": round(sum(r.net_area for r in self.rooms), 3),
            "openings_unassigned": sum(1 for o in self.opening_items
                                       if o.unassigned),
            "openings_size_anomaly": sum(1 for o in self.opening_items
                                         if o.anomalous),
        }
