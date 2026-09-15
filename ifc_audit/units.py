"""单位换算工具。

IFC 内部长度通常以项目长度单位（如毫米）存储，几何计算与报告统一使用米。
"""

from __future__ import annotations

import ifcopenshell.util.unit as ifc_unit


def project_length_scale(ifc_file) -> float:
    """返回 1 个 IFC 长度单位对应的米数（例如 mm 项目返回 0.001）。"""
    try:
        scale = ifc_unit.calculate_unit_scale(ifc_file)  # 单位 -> 米
        return float(scale) if scale else 1.0
    except Exception:
        # 没有 IfcUnitAssignment 时按米处理
        return 1.0
