"""核查流程编排：提取 -> 几何/重复检查 -> 房间面积。"""

from __future__ import annotations

from typing import Optional

from .extract import extract
from .checks import run_all_checks
from .rooms import build_room_areas
from .openings import build_opening_schedule
from .thresholds import (
    Thresholds, ThresholdProvenance, DEFAULT_THRESHOLDS, resolve,
)


def audit_ifc(file_path: str, progress=None,
              thresholds: Optional[Thresholds] = None,
              provenance: Optional[ThresholdProvenance] = None,
              enabled_kinds: Optional[set[str]] = None,
              rule_pack=None) -> "AuditModel":
    """执行完整核查流程。

    Args:
        file_path: IFC 文件路径。
        progress: 可选回调 ``progress(percent: int, message: str)``。
        thresholds: 判定阈值；默认使用 default 预设。
        provenance: 阈值来源（配置文件 / 命令行覆盖 / 规则包），随模型带入
            报告；只给 thresholds 不给 provenance 时记为自定义方案。
        enabled_kinds: 启用的问题种类集合（企业规则包关闭部分核查项时收窄）；
            None 表示全部核查项启用。
        rule_pack: 企业规则包引用（RulePackRef），随模型带入报告以便追溯。
    """
    if thresholds is None:
        thresholds, provenance = DEFAULT_THRESHOLDS, ThresholdProvenance()
    elif provenance is None:
        provenance = ThresholdProvenance(profile="custom")

    def report(pct, msg):
        if progress:
            progress(pct, msg)

    report(5, "正在解析 IFC 几何…")
    model = extract(file_path)
    model.thresholds = thresholds
    model.threshold_provenance = provenance
    model.enabled_kinds = enabled_kinds
    model.rule_pack = rule_pack

    report(55, f"已提取 {len(model.elements)} 个构件，正在执行核查规则…")
    run_all_checks(model, thresholds, enabled_kinds)

    report(75, "正在统计房间净面积…")
    build_room_areas(model, thresholds, enabled_kinds)

    report(90, "正在生成门窗规格清单…")
    build_opening_schedule(model, thresholds, enabled_kinds)

    report(100, "核查完成。")
    return model


def audit_ifc_with_config(file_path: str, progress=None,
                          profile: str = "default",
                          config_path: Optional[str] = None,
                          overrides: Optional[dict] = None) -> "AuditModel":
    """便捷入口：先解析阈值配置再执行核查（CLI / GUI 使用）。"""
    thresholds, provenance = resolve(profile, config_path, overrides)
    return audit_ifc(file_path, progress, thresholds, provenance)
