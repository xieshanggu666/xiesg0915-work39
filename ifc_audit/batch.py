"""多模型批量核查：一次纳入多个单体 IFC，按项目 / 单体 / 楼层汇总。

流程：

1. :func:`run_batch` 逐个解析 IFC（复用单模型 :func:`audit_ifc` 流水线），
   汇总每个单体的构件数量、问题分布（按类型 / 严重程度）、房间净面积、
   门窗规格指标，并进一步按 **单体 × 楼层** 聚合；
2. :func:`evaluate_gate` 按 :mod:`ifc_audit.gate` 的放行规则逐单体与项目
   整体判定，任一规则失败即 ``passed=False``，由 CLI 以退出码 3 阻断放行；
3. :mod:`ifc_audit.batch_report` 输出批次 Excel、项目质量看板 PNG 与批次 JSON；
4. :func:`save_batch_snapshot` / :func:`load_project_history` 按批次留存
   精简快照，:func:`build_trend` 与上一批次对比并形成趋势。

核查失败（IFC 无法解析等）的单体不会中断整批，记为失败单体进入汇总，
并受门禁 ``allow_failed_files`` 规则约束。
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Callable

from .model import AuditModel
from .pipeline import audit_ifc
from .thresholds import (
    Thresholds, ThresholdProvenance, resolve as resolve_thresholds,
)
from .gate import (
    QualityGate, GateProvenance, DEFAULT_GATE, META as GATE_META,
    resolve_gate,
)
from .rule_packs import enabled_checks_from_kinds
from .coordination_model import (
    KIND_HARD_CLASH, KIND_OPENING_MISSING, KIND_OPENING_MISMATCH,
    KIND_OPENING_UNUSED, COORD_KINDS,
)

# 支持的 IFC 后缀
IFC_SUFFIXES = (".ifc", ".ifcxml", ".ifczip")


# ---------------------------------------------------------------- 数据结构 ----

@dataclass
class StoreyAgg:
    """单体 × 楼层 聚合行。"""

    unit: str
    storey: str
    walls: int = 0
    doors: int = 0
    windows: int = 0
    rooms: int = 0
    issues: int = 0
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    net_area: float = 0.0
    rooms_open: int = 0        # 围护不闭合
    rooms_unchecked: int = 0   # 无几何未检查
    rooms_zero_area: int = 0
    opening_doors: int = 0
    opening_windows: int = 0
    opening_anomaly: int = 0
    opening_unassigned: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UnitResult:
    """单个单体（一个 IFC 文件）的批量核查结果。"""

    name: str
    file_path: str
    # 跨批次稳定标识（相对模型根的路径，如 A区/楼A），用于工单指纹；
    # name 仅为批次内显示名（跨目录同名时带父目录消歧），不进指纹
    unit_key: str = ""
    ok: bool = True
    error: str = ""

    walls: int = 0
    doors: int = 0
    windows: int = 0
    rooms: int = 0
    issues: int = 0
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)

    total_net_area: float = 0.0
    rooms_open: int = 0
    rooms_unchecked: int = 0
    rooms_zero_area: int = 0
    dup_groups: int = 0

    opening_total: int = 0
    opening_doors: int = 0
    opening_windows: int = 0
    opening_anomaly: int = 0
    opening_unassigned: int = 0

    threshold_describe: str = ""
    rule_pack: Optional[dict] = None
    storeys: list[StoreyAgg] = field(default_factory=list)

    # 核查模型仅在本次运行内保留（供导出单体报告 / 看板取数），不进快照
    model: Optional[AuditModel] = field(default=None, repr=False)

    @property
    def error_density(self) -> float:
        """每 1000 m² 净面积的错误数。"""
        if self.total_net_area <= 1e-9:
            return float(self.errors) * 1e9 if self.errors else 0.0
        return self.errors / (self.total_net_area / 1000.0)

    @property
    def open_room_ratio(self) -> float:
        return self.rooms_open / self.rooms if self.rooms else 0.0

    @property
    def anomaly_ratio(self) -> float:
        return (self.opening_anomaly / self.opening_total
                if self.opening_total else 0.0)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit_key": self.unit_key or stable_unit_key(self.file_path),
            "file_path": self.file_path,
            "ok": self.ok,
            "error": self.error,
            "walls": self.walls,
            "doors": self.doors,
            "windows": self.windows,
            "rooms": self.rooms,
            "issues": self.issues,
            "errors": self.errors,
            "warnings": self.warnings,
            "infos": self.infos,
            "kind_counts": dict(self.kind_counts),
            "total_net_area": self.total_net_area,
            "rooms_open": self.rooms_open,
            "rooms_unchecked": self.rooms_unchecked,
            "rooms_zero_area": self.rooms_zero_area,
            "dup_groups": self.dup_groups,
            "opening_total": self.opening_total,
            "opening_doors": self.opening_doors,
            "opening_windows": self.opening_windows,
            "opening_anomaly": self.opening_anomaly,
            "opening_unassigned": self.opening_unassigned,
            "error_density": round(self.error_density, 3),
            "open_room_ratio": round(self.open_room_ratio, 4),
            "anomaly_ratio": round(self.anomaly_ratio, 4),
            "threshold_describe": self.threshold_describe,
            "rule_pack": self.rule_pack,
            "storeys": [s.to_dict() for s in self.storeys],
        }


@dataclass
class GateRuleResult:
    """一条放行规则的判定结果。"""

    level: str          # unit / project / batch
    scope: str          # 单体名（项目级为项目名）
    key: str
    rule: str
    limit: str
    actual: str
    passed: bool
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchResult:
    """一次批量核查（一个批次）的完整结果。"""

    project: str
    batch_id: str
    label: str
    created_at: str
    units: list[UnitResult]
    storeys: list[StoreyAgg]
    totals: dict
    gate: dict
    gate_passed: bool
    gate_results: list[GateRuleResult]
    trend: dict = field(default_factory=dict)
    history_dir: str = ""
    rule_pack: Optional[dict] = None
    enabled_checks: list[str] = field(default_factory=list)
    # 本批模型根锚点（稳定单体标识相对它计算），随快照持久化以便追溯
    model_root: str = ""

    # 多专业协同核查结果（纳入建筑/结构/机电 ≥2 专业时填充，否则为 None）
    coordination: object = None

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "batch_id": self.batch_id,
            "label": self.label,
            "created_at": self.created_at,
            "n_files": len(self.units),
            "n_units_ok": sum(1 for u in self.units if u.ok),
            "totals": self.totals,
            "gate": self.gate,
            "gate_passed": self.gate_passed,
            "gate_results": [r.to_dict() for r in self.gate_results],
            "trend": self.trend,
            "rule_pack": self.rule_pack,
            "enabled_checks": list(self.enabled_checks),
            "model_root": self.model_root,
            "coordination": (self.coordination.to_dict()
                             if self.coordination is not None else None),
            "units": [u.to_dict() for u in self.units],
            "storeys": [s.to_dict() for s in self.storeys],
        }


# ---------------------------------------------------------------- 工具 ----

def _natural_key(text: str):
    """楼层名等自然排序：1F < 2F < 10F。"""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", text or "")]


def discover_ifc_files(paths: list[str]) -> list[str]:
    """把文件 / 目录参数展开为 IFC 文件列表（目录不递归，按文件名排序）。"""
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for suf in IFC_SUFFIXES:
                files.extend(glob.glob(os.path.join(p, f"*{suf}")))
        elif os.path.isfile(p):
            files.append(p)
        else:
            # 可能是 glob 模式
            matched = glob.glob(p)
            if matched:
                files.extend(f for f in matched if os.path.isfile(f))
            else:
                raise FileNotFoundError(f"找不到 IFC 文件或目录：{p}")
    # 去重并自然排序
    return sorted({os.path.abspath(f) for f in files},
                  key=lambda f: _natural_key(os.path.basename(f)))


def _unit_base_name(file_path: str) -> str:
    """单体默认名：去掉 IFC 后缀的文件名。"""
    return os.path.splitext(os.path.basename(file_path))[0]


def stable_unit_key(file_path: str, model_root: str = "") -> str:
    """单体跨批次稳定标识（见 :mod:`ifc_audit.identity`）。

    等于模型相对模型根锚点的 POSIX 相对路径去后缀，可区分不同目录下的同名
    模型；``name``（:func:`unique_unit_names`）只是批次内显示名，不进指纹。
    """
    from .identity import stable_unit_key as _key
    return _key(file_path, model_root)


def unique_unit_names(files: list[str]) -> dict[str, str]:
    """为一批 IFC 文件生成不重名的单体名。

    不同目录下存在同名文件时，单体名会带父目录消歧
    （如 ``A区/楼A.ifc`` 与 ``B区/楼A.ifc`` -> ``A区-楼A``、``B区-楼A``）。
    同一基名的一组重名文件统一带上相同级数的父目录，保证命名规整；
    若候选名与其它单体（或组内文件）冲突，则整组再向上多带一级；
    到根目录仍冲突时追加序号兜底。返回 ``{绝对路径: 单体名}``。
    """
    result: dict[str, str] = {}
    used: set[str] = set()

    def _take(path, candidate) -> bool:
        if candidate not in used:
            used.add(candidate)
            result[path] = candidate
            return True
        return False

    def _candidate(path: str, base: str, level: int) -> str:
        parts = []
        cur = os.path.dirname(path)
        for _ in range(level):
            parent = os.path.basename(cur)
            if parent:
                parts.append(parent)
            nxt = os.path.dirname(cur)
            if nxt == cur:  # 已到文件系统根
                break
            cur = nxt
        return "-".join(parts[::-1] + [base]) if parts else base

    by_base: dict[str, list[str]] = {}
    for fp in files:
        by_base.setdefault(_unit_base_name(fp), []).append(fp)

    # 不重名的基名先占用文件名
    duplicate_groups = []
    for base, paths in by_base.items():
        if len(paths) == 1:
            _take(paths[0], base)
        else:
            duplicate_groups.append((base, sorted(paths)))

    # 重名组：从 1 级父目录开始整组尝试，冲突则整组加深一级
    for base, paths in duplicate_groups:
        level = 1
        while True:
            candidates = {fp: _candidate(fp, base, level) for fp in paths}
            values = list(candidates.values())
            if len(set(values)) == len(values) and not (set(values) & used):
                for fp, cand in candidates.items():
                    _take(fp, cand)
                break
            # 所有路径都已取到根目录仍无法区分，序号兜底
            if all(os.path.dirname(os.path.dirname(fp))
                   == os.path.dirname(fp) for fp in paths):
                for fp in paths:
                    idx = 2
                    while not _take(fp, f"{base}-{idx}"):
                        idx += 1
                break
            level += 1
    return result


def _storey_label(storey: str) -> str:
    return storey or "(未分层)"


# ---------------------------------------------------------------- 聚合 ----

def _aggregate_unit(name: str, file_path: str, model: AuditModel,
                    unit_key: str = "") -> UnitResult:
    """从单模型核查结果聚合单体指标与单体×楼层指标。"""
    s = model.summary()
    u = UnitResult(name=name, file_path=file_path,
                   unit_key=unit_key or stable_unit_key(file_path), model=model)
    u.walls, u.doors, u.windows, u.rooms = (
        s["walls"], s["doors"], s["windows"], s["rooms"])
    u.issues = s["issues"]
    u.errors = s["errors"]
    u.warnings = s["warnings"]
    u.infos = u.issues - u.errors - u.warnings
    u.dup_groups = s["duplicate_groups"]
    u.total_net_area = s["total_net_area"]
    u.opening_unassigned = s["openings_unassigned"]
    u.opening_anomaly = s["openings_size_anomaly"]
    prov = getattr(model, "threshold_provenance", None)
    u.threshold_describe = prov.describe() if prov else ""
    pack_ref = getattr(model, "rule_pack", None)
    u.rule_pack = pack_ref.to_dict() if pack_ref is not None else None

    # 楼层行
    rows: dict[str, StoreyAgg] = {}

    def row(storey) -> StoreyAgg:
        label = _storey_label(storey)
        if label not in rows:
            rows[label] = StoreyAgg(unit=name, storey=label)
        return rows[label]

    for e in model.elements.values():
        r = row(e.storey)
        if e.ifc_type == "IfcWall":
            r.walls += 1
        elif e.ifc_type == "IfcDoor":
            r.doors += 1
        elif e.ifc_type == "IfcWindow":
            r.windows += 1
        elif e.ifc_type == "IfcSpace":
            r.rooms += 1

    for room in model.rooms:
        r = row(room.storey)
        r.net_area = round(r.net_area + room.net_area, 3)
        if room.enclosure_status == "open":
            r.rooms_open += 1
        elif room.enclosure_status == "unchecked":
            r.rooms_unchecked += 1
        if room.net_area <= 1e-9:
            r.rooms_zero_area += 1

    for o in model.opening_items:
        r = row(o.storey)
        if o.kind == "door":
            r.opening_doors += 1
        else:
            r.opening_windows += 1
        if o.anomalous:
            r.opening_anomaly += 1
        if o.unassigned:
            r.opening_unassigned += 1

    for issue in model.issues:
        r = row(issue.storey)
        r.issues += 1
        r.kind_counts[issue.kind] = r.kind_counts.get(issue.kind, 0) + 1
        if issue.severity == "error":
            r.errors += 1
        elif issue.severity == "warning":
            r.warnings += 1
        else:
            r.infos += 1
        u.kind_counts[issue.kind] = u.kind_counts.get(issue.kind, 0) + 1

    for r in rows.values():
        r.net_area = round(r.net_area, 3)
    u.storeys = sorted(rows.values(), key=lambda r: _natural_key(r.storey))

    u.rooms_open = sum(r.rooms_open for r in u.storeys)
    u.rooms_unchecked = sum(r.rooms_unchecked for r in u.storeys)
    u.rooms_zero_area = sum(r.rooms_zero_area for r in u.storeys)
    u.opening_doors = sum(r.opening_doors for r in u.storeys)
    u.opening_windows = sum(r.opening_windows for r in u.storeys)
    u.opening_total = u.opening_doors + u.opening_windows
    return u


# ---------------------------------------------------------------- 门禁 ----

def _fmt_limit(spec, value):
    if spec.unit == _PCT:
        return "不限制" if value < 0 else f"≤ {value * 100:g}%"
    if value == -1:
        return "不限制"
    if spec.unit == "density":
        return f"≤ {value:g}"
    if spec.unit == "bool":
        return "不允许" if not value else "允许"
    return f"≤ {value:g}"


_PCT = "%"


def evaluate_gate(units: list[UnitResult], gate: QualityGate,
                  project: str,
                  disabled_keys: Optional[set[str]] = None
                  ) -> list[GateRuleResult]:
    """逐单体 + 项目 + 批次完整性评估全部启用的门禁规则。

    disabled_keys 中的规则键不参与判定（企业规则包关闭对应核查项时，
    相关放行条件失去统计意义，由调用方传入）。
    """
    disabled_keys = disabled_keys or set()
    results: list[GateRuleResult] = []

    def add(level, scope, key, actual, passed, message, actual_shown=None):
        spec = GATE_META[key]
        limit = getattr(gate, spec.attr)
        results.append(GateRuleResult(
            level=level, scope=scope, key=key, rule=spec.label,
            limit=_fmt_limit(spec, limit),
            actual=actual_shown if actual_shown is not None else str(actual),
            passed=passed, message=message))

    ok_units = [u for u in units if u.ok]
    failed_units = [u for u in units if not u.ok]

    # ---- 单体级 ----
    for u in ok_units:
        checks = [
            ("unit_max_errors", u.errors,
             f"错误 {u.errors} 条"),
            ("unit_max_warnings", u.warnings,
             f"警告 {u.warnings} 条"),
            ("unit_max_errors_per_1000m2", u.error_density,
             f"错误密度 {u.error_density:.2f} 条/千m²"),
            ("unit_max_open_rooms_pct", u.open_room_ratio,
             f"不闭合房间占比 {u.open_room_ratio * 100:.1f}%"
             f"（{u.rooms_open}/{u.rooms}）"),
            ("unit_max_open_rooms", u.rooms_open,
             f"不闭合房间 {u.rooms_open} 间"),
            ("unit_max_unassigned_openings", u.opening_unassigned,
             f"未归属门窗 {u.opening_unassigned} 樘"),
            ("unit_max_size_anomaly_pct", u.anomaly_ratio,
             f"尺寸异常门窗占比 {u.anomaly_ratio * 100:.1f}%"
             f"（{u.opening_anomaly}/{u.opening_total}）"),
            ("unit_max_size_anomaly", u.opening_anomaly,
             f"尺寸异常门窗 {u.opening_anomaly} 樘"),
            ("unit_max_dup_groups", u.dup_groups,
             f"重复构件组 {u.dup_groups} 组"),
        ]
        for key, actual, shown in checks:
            spec = GATE_META[key]
            if key in disabled_keys:
                continue  # 对应核查项已被规则包关闭
            limit = getattr(gate, spec.attr)
            if limit < 0:
                continue  # 规则关闭
            if spec.unit == _PCT:
                passed = actual <= limit + 1e-12
            else:
                passed = actual <= limit + 1e-12
            add("unit", u.name, key, actual, passed,
                ("" if passed else f"{spec.label}超限：{shown}，"
                                   f"门禁要求 {_fmt_limit(spec, limit)}"),
                actual_shown=shown)

    # ---- 项目级汇总 ----
    totals = _project_totals(units)
    proj_checks = [
        ("project_max_errors", totals["errors"],
         f"项目错误合计 {totals['errors']} 条"),
        ("project_max_dup_groups", totals["duplicate_groups"],
         f"项目重复构件组合计 {totals['duplicate_groups']} 组"),
        ("project_max_zero_area_rooms", totals["rooms_zero_area"],
         f"净面积为0的房间 {totals['rooms_zero_area']} 间"),
        ("min_units", len(units),
         f"纳入单体 {len(units)} 个"),
    ]
    for key, actual, shown in proj_checks:
        spec = GATE_META[key]
        if key in disabled_keys:
            continue  # 对应核查项已被规则包关闭
        limit = getattr(gate, spec.attr)
        if key == "min_units":
            passed = actual >= limit
            msg = ("" if passed else
                   f"纳入单体仅 {actual} 个，少于最少要求 {limit} 个（可能漏传文件）")
        else:
            if limit < 0:
                continue
            passed = actual <= limit
            msg = ("" if passed
                   else f"{spec.label}超限：{shown}，门禁要求 {_fmt_limit(spec, limit)}")
        add("project", project, key, actual, passed, msg,
            actual_shown=shown)

    # ---- 批次完整性：失败文件 ----
    spec = GATE_META["allow_failed_files"]
    if failed_units:
        passed = gate.allow_failed_files
        names = "、".join(u.name for u in failed_units)
        add("batch", project, "allow_failed_files", len(failed_units), passed,
            ("" if passed else
             f"{len(failed_units)} 个单体核查失败：{names}；门禁不允许失败文件"),
            actual_shown=f"失败 {len(failed_units)} 个（{names}）")
    return results


def _project_totals(units: list[UnitResult]) -> dict:
    """项目汇总指标（失败单体按 0 计入构件指标，单独记 failed_files）。"""
    ok = [u for u in units if u.ok]
    totals = {
        "units": len(units),
        "units_failed": sum(1 for u in units if not u.ok),
        "walls": sum(u.walls for u in ok),
        "doors": sum(u.doors for u in ok),
        "windows": sum(u.windows for u in ok),
        "rooms": sum(u.rooms for u in ok),
        "issues": sum(u.issues for u in ok),
        "errors": sum(u.errors for u in ok),
        "warnings": sum(u.warnings for u in ok),
        "infos": sum(u.infos for u in ok),
        "duplicate_groups": sum(u.dup_groups for u in ok),
        "total_net_area": round(sum(u.total_net_area for u in ok), 3),
        "rooms_open": sum(u.rooms_open for u in ok),
        "rooms_unchecked": sum(u.rooms_unchecked for u in ok),
        "rooms_zero_area": sum(u.rooms_zero_area for u in ok),
        "opening_total": sum(u.opening_total for u in ok),
        "opening_doors": sum(u.opening_doors for u in ok),
        "opening_windows": sum(u.opening_windows for u in ok),
        "opening_anomaly": sum(u.opening_anomaly for u in ok),
        "opening_unassigned": sum(u.opening_unassigned for u in ok),
    }
    kind_counts: dict[str, int] = {}
    for u in ok:
        for k, v in u.kind_counts.items():
            kind_counts[k] = kind_counts.get(k, 0) + v
    totals["kind_counts"] = kind_counts
    return totals


def _new_batch_id() -> str:
    """批次编号：秒级时间戳 + 毫秒后缀，避免同秒连续运行时相互覆盖。"""
    now = datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


# ---------------------------------------------------------------- 批量入口 ----

def run_batch(paths: list[str],
              project: str = "未命名项目",
              label: str = "",
              gate: Optional[QualityGate] = None,
              gate_provenance: Optional[GateProvenance] = None,
              thresholds: Optional[Thresholds] = None,
              threshold_provenance: Optional[ThresholdProvenance] = None,
              enabled_kinds: Optional[set[str]] = None,
              rule_pack=None,
              disabled_gate_keys: Optional[set[str]] = None,
              progress: Optional[Callable[[int, str], None]] = None,
              run_coordination_check: Optional[bool] = None,
              coord_owners: Optional[dict[str, str]] = None,
              coord_settings: Optional[object] = None,
              coord_gate_profile: str = "default",
              coord_gate_overrides: Optional[dict[str, object]] = None,
              coord_discipline_map: Optional[dict[str, str]] = None,
              history_dir: str = "",
              model_root: str = "",
              ) -> BatchResult:
    """批量核查多个 IFC 文件并完成聚合与门禁判定。

    Args:
        paths: IFC 文件 / 目录列表（目录取其中 ``.ifc/.ifcxml/.ifczip``）。
        project: 项目名（看板 / 历史留存按项目归档）。
        label: 批次标签（如 "v1 提模"、"竣工审查"），可空。
        gate: 放行规则；默认 :data:`~ifc_audit.gate.DEFAULT_GATE`。
        gate_provenance: 放行规则来源说明。
        thresholds / threshold_provenance: 单模型核查阈值（默认 default 预设）。
        enabled_kinds: 启用的问题种类集合（规则包关闭部分核查项时收窄）。
        rule_pack: 企业规则包引用（RulePackRef），随批次结果进报告以便追溯。
        disabled_gate_keys: 因核查项关闭而不参与判定的门禁规则键。
        progress: 进度回调 ``progress(percent, message)``。
        run_coordination_check: 是否做多专业协同核查；None=自动（文件名识别出
            机电 + 其它专业时执行），True=强制执行（无多专业时报错），False=关闭。
        coord_owners: 专业责任人映射 ``{arch/struct/mep: 姓名}``。
        coord_settings: :class:`ifc_audit.coordination.CoordSettings`。
        coord_gate_profile / coord_gate_overrides: 协同门禁预设与单项覆盖。
        coord_discipline_map: 显式指定单体专业（单体名或绝对路径 -> 专业）。
        history_dir: 批次历史目录，提供时协同台账按项目归档其中。
        model_root: 模型根锚点（显式指定）；不给定时按项目历史配置 /
            本批文件公共父目录确定，用于生成跨批次稳定单体标识。
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    from .identity import resolve_model_root, build_unit_keys

    files = discover_ifc_files(paths)
    if not files:
        raise FileNotFoundError("指定路径下没有找到 IFC 文件")
    # 模型根锚点：显式 > 项目历史固化 > 本批公共父目录（首次自动持久化）
    model_root_dir, root_reused = resolve_model_root(
        files, project=project, history_dir=history_dir,
        explicit=model_root)
    # 不同目录下的同名文件：显示名消歧 + 稳定键相对模型根，二者各司其职
    unit_names = unique_unit_names(files)
    unit_keys = build_unit_keys(files, model_root_dir)
    batch_id = _new_batch_id()

    if gate is None:
        gate, gate_provenance = DEFAULT_GATE, GateProvenance()
    elif gate_provenance is None:
        gate_provenance = GateProvenance(profile="custom")
    if thresholds is None:
        thresholds, threshold_provenance = resolve_thresholds("default")

    units: list[UnitResult] = []
    n = len(files)
    for idx, fp in enumerate(files):
        name = unit_names[fp]
        key = unit_keys[os.path.abspath(fp)]
        base_pct = int(idx / n * 100)
        end_pct = int((idx + 1) / n * 100)
        report(base_pct, f"[{idx + 1}/{n}] 正在核查单体 {name} …")
        try:
            model = audit_ifc(
                fp,
                progress=(lambda p, m, _b=base_pct, _e=end_pct:
                          progress(_b + int(p / 100 * (_e - _b)), m))
                if progress else None,
                thresholds=thresholds,
                provenance=threshold_provenance,
                enabled_kinds=enabled_kinds,
                rule_pack=rule_pack)
            units.append(_aggregate_unit(name, fp, model, unit_key=key))
        except Exception as exc:  # 单体失败不拖垮整批
            units.append(UnitResult(
                name=name, file_path=fp,
                unit_key=key, ok=False,
                error=f"{type(exc).__name__}: {exc}"))

    all_storeys = [s for u in units for s in u.storeys]
    totals = _project_totals(units)
    gate_results = evaluate_gate(units, gate, project,
                                 disabled_keys=disabled_gate_keys)

    # ---- 多专业协同核查（建筑/结构/机电 ≥2 专业且含机电时自动执行）----
    coordination = _maybe_run_coordination(
        files=files,
        unit_names=unit_names,
        project=project,
        label=label or "",
        batch_id=batch_id,
        run=run_coordination_check,
        owners=coord_owners,
        settings=coord_settings,
        gate_profile=coord_gate_profile,
        gate_overrides=coord_gate_overrides,
        discipline_map=coord_discipline_map,
        enabled_kinds=enabled_kinds,
        history_dir=history_dir,
        gate_results=gate_results,
        model_root=model_root_dir,
        progress=progress)

    # 门禁关闭（none 预设）时不阻断；启用时全部规则通过才放行
    passed = (not gate.enabled) or all(r.passed for r in gate_results)
    if coordination is not None and not coordination.gate_passed:
        passed = False

    report(100, "批量核查完成。")
    batch = BatchResult(
        project=project,
        batch_id=batch_id,
        label=label or "",
        created_at=datetime.now().isoformat(timespec="seconds"),
        units=units,
        storeys=all_storeys,
        totals=totals,
        gate={
            "values": asdict(gate),
            "provenance": gate_provenance.to_dict() if gate_provenance else None,
            "description": gate_provenance.describe() if gate_provenance else "",
            "enabled": gate.enabled,
            "disabled_keys": sorted(disabled_gate_keys or []),
        },
        gate_passed=passed,
        gate_results=gate_results,
        rule_pack=rule_pack.to_dict() if rule_pack is not None else None,
        enabled_checks=enabled_checks_from_kinds(enabled_kinds),
        coordination=coordination,
        model_root=model_root_dir,
    )
    return batch


def _coord_gate_disabled_keys(enabled_kinds: Optional[set[str]]) -> set[str]:
    """规则包关闭协同核查项时，对应协同门禁不参与判定。"""
    if enabled_kinds is None:
        return set()
    disabled = set()
    if not ({KIND_HARD_CLASH, KIND_OPENING_MISSING} & enabled_kinds):
        disabled.add("coord_max_clash_active")
    if KIND_OPENING_MISMATCH not in enabled_kinds:
        disabled.add("coord_max_mismatch_active")
    if KIND_OPENING_UNUSED not in enabled_kinds:
        disabled.add("coord_max_unused_active")
    # 整改时限/超期升级门禁覆盖全部协同工单：两类协同核查项都关闭时才跳过
    if not (enabled_kinds & set(COORD_KINDS)):
        disabled.add("coord_max_overdue_active")
    return disabled


def _maybe_run_coordination(files: list[str], unit_names: dict[str, str],
                            project: str, label: str, batch_id,
                            run: Optional[bool],
                            owners, settings, gate_profile, gate_overrides,
                            discipline_map, enabled_kinds, history_dir,
                            gate_results: list["GateRuleResult"],
                            progress, model_root: str = "") -> object:
    """按开关 / 文件专业组成决定并执行多专业协同核查。

    协同门禁的逐条判定同时镜像进批次 ``gate_results``（级别 ``coordination``），
    使批次「放行判定」表与退出码 3 联动。返回 CoordinationResult 或 None。
    """
    from .coordination import (
        classify_files, has_multiple_disciplines, run_coordination,
        default_ledger_path,
    )
    from .coordination_model import DISC_CN

    _, _, file_discs = classify_files(files, discipline_map)
    multi = has_multiple_disciplines(file_discs)
    if run is False:
        return None
    if not multi:
        if run is True:
            discs = sorted({d for d in file_discs.values() if d})
            raise FileNotFoundError(
                "要求执行多专业协同核查，但文件未能识别出机电 + 其它专业"
                f"（当前识别：{', '.join(DISC_CN.get(d, d) for d in discs) or '无'}）；"
                "请在文件名中加入 建筑/结构/机电 关键词，或用专业映射显式指定")
        return None

    ledger_path = (default_ledger_path(history_dir, project)
                   if history_dir else None)
    # 规则包关闭协同核查项时，协同门禁同步跳过
    disabled = _coord_gate_disabled_keys(enabled_kinds)
    result = run_coordination(
        files, project=project, label=label, batch_id=batch_id,
        discipline_map=discipline_map, owners=owners, settings=settings,
        gate_profile=gate_profile, gate_overrides=gate_overrides,
        ledger_path=ledger_path, disabled_gate_keys=disabled,
        model_root=model_root,
        progress=(lambda p, m: progress(p, f"多专业协同：{m}")
                  if progress else None))

    # 协同门禁结果镜像为批次门禁规则（级别 coordination）
    for r in result.gate_rules:
        gate_results.append(GateRuleResult(
            level="coordination", scope=project, key=r["key"],
            rule="[协同] " + r["rule"], limit=r["limit"],
            actual=r["actual"], passed=r["passed"], message=r["message"]))
    return result


def run_batch_with_config(paths: list[str],
                          project: str = "未命名项目",
                          label: str = "",
                          threshold_profile: str = "default",
                          threshold_config: Optional[str] = None,
                          threshold_overrides: Optional[dict] = None,
                          gate_profile: str = "default",
                          gate_config: Optional[str] = None,
                          gate_overrides: Optional[dict] = None,
                          progress: Optional[Callable[[int, str], None]] = None,
                          run_coordination_check: Optional[bool] = None,
                          coord_owners: Optional[dict[str, str]] = None,
                          coord_settings: Optional[object] = None,
                          coord_gate_profile: str = "default",
                          coord_gate_overrides: Optional[dict] = None,
                          coord_discipline_map: Optional[dict[str, str]] = None,
                          history_dir: str = os.path.join("output", "batch_history"),
                          model_root: str = "",
                          ) -> BatchResult:
    """便捷入口：先解析核查阈值与门禁配置，再执行批量核查。"""
    th, th_prov = resolve_thresholds(
        threshold_profile, threshold_config, threshold_overrides)
    gate, gate_prov = resolve_gate(
        gate_profile, gate_config, gate_overrides)
    return run_batch(paths, project=project, label=label,
                     gate=gate, gate_provenance=gate_prov,
                     thresholds=th, threshold_provenance=th_prov,
                     progress=progress,
                     run_coordination_check=run_coordination_check,
                     coord_owners=coord_owners, coord_settings=coord_settings,
                     coord_gate_profile=coord_gate_profile,
                     coord_gate_overrides=coord_gate_overrides,
                     coord_discipline_map=coord_discipline_map,
                     history_dir=history_dir, model_root=model_root)


def run_batch_with_rule_pack(paths: list[str],
                             materialized,
                             project: str = "未命名项目",
                             label: str = "",
                             progress: Optional[Callable[[int, str], None]] = None,
                             run_coordination_check: Optional[bool] = None,
                             coord_owners: Optional[dict[str, str]] = None,
                             coord_settings: Optional[object] = None,
                             coord_gate_profile: str = "default",
                             coord_gate_overrides: Optional[dict] = None,
                             coord_discipline_map: Optional[dict[str, str]] = None,
                             history_dir: str = os.path.join("output", "batch_history"),
                             model_root: str = "",
                             ) -> BatchResult:
    """便捷入口：用已物化的企业规则包执行批量核查。

    Args:
        materialized: :func:`ifc_audit.rule_packs.materialize` 的结果。
    """
    from .rule_packs import disabled_gate_keys
    return run_batch(
        paths, project=project, label=label,
        gate=materialized.gate, gate_provenance=materialized.gate_provenance,
        thresholds=materialized.thresholds,
        threshold_provenance=materialized.threshold_provenance,
        enabled_kinds=materialized.enabled_kinds,
        rule_pack=materialized.ref,
        disabled_gate_keys=disabled_gate_keys(materialized.enabled_checks),
        progress=progress,
        run_coordination_check=run_coordination_check,
        coord_owners=coord_owners, coord_settings=coord_settings,
        coord_gate_profile=coord_gate_profile,
        coord_gate_overrides=coord_gate_overrides,
        coord_discipline_map=coord_discipline_map,
        history_dir=history_dir, model_root=model_root)


# ---------------------------------------------------------------- 趋势 ----

_TREND_METRICS = (
    ("issues", "问题总数"), ("errors", "错误"), ("warnings", "警告"),
    ("duplicate_groups", "重复构件组"), ("total_net_area", "净面积(m²)"),
    ("rooms_open", "不闭合房间"), ("opening_unassigned", "未归属门窗"),
    ("opening_anomaly", "尺寸异常门窗"),
)


def build_trend(batch: BatchResult, previous: Optional[dict]) -> dict:
    """与上一批次快照对比，生成指标增减、单体变化与历史序列。"""
    if previous is None:
        return {"has_previous": False, "history": [_history_point(batch)]}

    pt = previous.get("totals", {})
    deltas = {}
    for key, label in _TREND_METRICS:
        new = batch.totals.get(key, 0)
        old = pt.get(key, 0)
        deltas[key] = {"label": label, "old": old, "new": new,
                       "delta": round(new - old, 3)}

    # 单体按**稳定标识**（unit_key）匹配，显示名仅对旧快照（无 unit_key）回落；
    # 避免跨目录同名文件消歧前缀变化时被误判成“单体新增 / 缺失”。
    def _prev_key(u: dict) -> str:
        return u.get("unit_key") or u.get("name") or ""

    prev_units = {_prev_key(u): u for u in previous.get("units", [])}
    prev_by_name = {u.get("name"): u for u in previous.get("units", [])
                    if u.get("name")}
    cur_units = {(u.unit_key or u.name): u for u in batch.units}

    def _match_old(key: str, new_u):
        old = prev_units.get(key)
        if old is None and new_u is not None and new_u.name in prev_by_name:
            # 旧快照只有显示名（无稳定键）时回落匹配
            old = prev_by_name[new_u.name]
        return old

    unit_delta = []
    for key in sorted(set(prev_units) | set(cur_units)):
        new = cur_units.get(key)
        old = prev_units.get(key)
        name = (new.name if new else old.get("name", key))
        if old and new:
            unit_delta.append({
                "unit": name, "unit_key": key,
                "errors_old": old.get("errors", 0),
                "errors_new": new.errors,
                "errors_delta": new.errors - old.get("errors", 0),
                "issues_old": old.get("issues", 0),
                "issues_new": new.issues,
                "issues_delta": new.issues - old.get("issues", 0),
                "status": "ok" if new.ok else "failed",
            })
        elif new:
            unit_delta.append({
                "unit": name, "unit_key": key,
                "errors_old": None, "errors_new": new.errors,
                "errors_delta": None, "issues_old": None,
                "issues_new": new.issues, "issues_delta": None,
                "status": "new",
            })
        else:
            unit_delta.append({
                "unit": name, "unit_key": key,
                "errors_old": old.get("errors", 0),
                "errors_new": None, "errors_delta": None,
                "issues_old": old.get("issues", 0), "issues_new": None,
                "issues_delta": None, "status": "missing",
            })

    return {
        "has_previous": True,
        "previous_batch_id": previous.get("batch_id"),
        "previous_label": previous.get("label", ""),
        "previous_created_at": previous.get("created_at"),
        "previous_gate_passed": previous.get("gate_passed"),
        "previous_rule_pack_id": previous.get("rule_pack_id")
        or (previous.get("rule_pack") or {}).get("id", ""),
        # 上一批次为「规则切换基线」（同一批模型 × 新规则包重算）时，
        # 本次增量反映模型整改效果，规则调整影响见切换试算记录
        "previous_is_rule_switch_baseline": bool(previous.get("rule_switch")),
        "previous_rule_switch": previous.get("rule_switch"),
        "deltas": deltas,
        "unit_delta": unit_delta,
        # 用显示名列出新增 / 缺失单体（内部已按稳定键匹配）
        "units_new": [d["unit"] for d in unit_delta if d["status"] == "new"],
        "units_missing": [d["unit"] for d in unit_delta
                          if d["status"] == "missing"],
    }


def _history_point(batch: BatchResult) -> dict:
    """看板趋势图用的精简历史点。"""
    return {
        "batch_id": batch.batch_id,
        "label": batch.label,
        "created_at": batch.created_at,
        "gate_passed": batch.gate_passed,
        "rule_pack_id": (batch.rule_pack or {}).get("id", ""),
        "issues": batch.totals["issues"],
        "errors": batch.totals["errors"],
        "warnings": batch.totals["warnings"],
        "total_net_area": batch.totals["total_net_area"],
    }


# ------------------------------------------------------- 批次快照留存 ----

def _slug(name: str) -> str:
    return re.sub(r"[^\w一-鿿.-]+", "_", name).strip("_") or "project"


def project_history_dir(history_dir: str, project: str) -> str:
    return os.path.join(history_dir, _slug(project))


def save_batch_snapshot(batch: BatchResult, history_dir: str) -> str:
    """把本批次精简结果按项目归档留存，返回快照文件路径。

    目录结构::

        <history_dir>/<项目名>/batches/<batch_id>.json
        <history_dir>/<项目名>/index.json
    """
    return save_snapshot_dict(batch.to_dict(), history_dir)


def save_snapshot_dict(snap: dict, history_dir: str) -> str:
    """把已序列化的批次快照字典按项目归档（规则切换基线等派生快照用）。"""
    import json
    project = snap.get("project") or "未命名项目"
    pdir = project_history_dir(history_dir, project)
    bdir = os.path.join(pdir, "batches")
    os.makedirs(bdir, exist_ok=True)
    path = os.path.join(bdir, f"{snap['batch_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2, default=str)

    # 索引
    index_path = os.path.join(pdir, "index.json")
    index = {"project": project, "batches": []}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    totals = snap.get("totals", {})
    units = snap.get("units", [])
    entry = {
        "batch_id": snap["batch_id"], "label": snap.get("label", ""),
        "created_at": snap.get("created_at", ""),
        "gate_passed": snap.get("gate_passed"),
        "n_files": snap.get("n_files", len(units)),
        "n_units": len(units),
        "errors": totals.get("errors", 0),
        "warnings": totals.get("warnings", 0),
        "issues": totals.get("issues", 0),
        "rule_pack_id": (snap.get("rule_pack") or {}).get("id", ""),
        "snapshot": os.path.relpath(path, pdir),
    }
    if snap.get("rule_switch"):
        entry["rule_switch"] = True   # 规则切换基线（趋势统计口径切换点）
    index["batches"] = [b for b in index.get("batches", [])
                        if b.get("batch_id") != snap["batch_id"]]
    index["batches"].append(entry)
    index["batches"].sort(key=lambda b: (b["created_at"], b["batch_id"]))
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    return path


def load_project_history(history_dir: str, project: str) -> list[dict]:
    """读取项目全部历史批次快照（按时间升序）。"""
    import json
    bdir = os.path.join(project_history_dir(history_dir, project), "batches")
    if not os.path.isdir(bdir):
        return []
    out = []
    for fn in sorted(glob.glob(os.path.join(bdir, "*.json"))):
        try:
            with open(fn, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda b: (b.get("created_at", ""), b.get("batch_id", "")))
    return out


def _snapshot_pack_id(snap: dict) -> str:
    """快照使用的规则包标识（兼容索引条目与全量快照）。"""
    return snap.get("rule_pack_id") or (snap.get("rule_pack") or {}).get("id", "")


def select_trend_anchor(batch_rule_pack_id: str,
                        history: list[dict]) -> Optional[dict]:
    """选择趋势对比的上一批次（基线）。

    常规取时间线最后一个批次；当历史中存在与当前批次**同规则包口径**的
    「规则切换基线」（已确认切换时写入）时，锚定到该基线或其后的
    同口径批次——即使基线之后还混有旧口径批次（延迟确认前跑的批次、
    补录的历史快照等），趋势增量也只相对同口径基线计算，
    避免把规则调整引起的变化误计为模型整改。
    """
    if not history:
        return None
    if batch_rule_pack_id:
        base_idx = None
        for i, h in enumerate(history):
            rs = h.get("rule_switch")
            if rs and rs.get("new_rule_pack_id") == batch_rule_pack_id:
                base_idx = i      # 同包多次切换时取最后一次切换的基线
        if base_idx is not None:
            for h in reversed(history[base_idx:]):
                if _snapshot_pack_id(h) == batch_rule_pack_id:
                    return h      # 基线本身同口径，循环至少命中基线
    return history[-1]


def attach_trend(batch: BatchResult, history_dir: str) -> BatchResult:
    """读取项目历史，为当前批次附加与上一批次的趋势对比与历史序列。"""
    history = load_project_history(history_dir, batch.project)
    previous = select_trend_anchor(
        (batch.rule_pack or {}).get("id", ""), history)
    trend = build_trend(batch, previous)
    points = []
    for h in history:
        ht = h.get("totals", {})
        points.append({
            "batch_id": h.get("batch_id"),
            "label": h.get("label", ""),
            "created_at": h.get("created_at"),
            "gate_passed": h.get("gate_passed"),
            "rule_pack_id": h.get("rule_pack_id")
            or (h.get("rule_pack") or {}).get("id", ""),
            "issues": ht.get("issues"),
            "errors": ht.get("errors"),
            "warnings": ht.get("warnings"),
            "total_net_area": ht.get("total_net_area"),
        })
    # build_trend 的 previous 取最后一个快照，规则包版本同样优先全量快照
    trend["history"] = points + [_history_point(batch)]
    batch.trend = trend
    batch.history_dir = history_dir
    return batch
