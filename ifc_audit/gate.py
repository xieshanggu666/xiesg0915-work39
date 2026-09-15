"""项目放行质量门禁（Quality Gate）。

批量核查完成后，按可配置规则判定每个单体与整个项目是否达到放行标准；
不达标时由 CLI 以退出码 3 **阻断放行**。

门禁规则分两级：

* **单体级**：对每个单体逐条评估（错误数、警告数、错误密度、围护不闭合
  房间数 / 占比、未归属门窗数、尺寸异常门窗数 / 占比等），任一单体失败
  即项目不放行；
* **项目级**：对项目汇总指标评估（错误总数、重复构件组总数、净面积为 0
  的房间数、核查失败文件数），并要求纳入的单体数不少于最小数量。

规则取值有三种调整方式（与判定阈值一致，优先级从低到高）：
内置预设（default/strict/loose/none）→ JSON 配置文件 → 命令行单项覆盖。
配置文件可用 ``python -m ifc_audit.cli init-gate gate.json`` 生成带说明模板。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, asdict
from typing import Optional


# ---------------------------------------------------------------- 预设 ----

GATE_PROFILES = ("default", "strict", "loose", "none")
GATE_PROFILE_CN = {
    "default": "标准放行门禁",
    "strict": "严格放行门禁（竣工审查）",
    "loose": "宽松放行门禁（方案阶段）",
    "none": "不设门禁（仅统计，不阻断）",
    "custom": "自定义门禁（配置文件或命令行覆盖）",
}


@dataclass(frozen=True)
class QualityGate:
    """一次批量核查使用的全部放行规则。

    “上限”类规则取值 ``-1`` 表示该条规则不启用；占比字段为 0~1 小数
    （用户面用百分比）。
    """

    # ---- 单体级（逐单体评估，全部满足才算单体合格）----
    unit_max_errors: int = 0          # 错误级问题数量上限
    unit_max_warnings: int = -1       # 警告级问题数量上限（-1=不限制）
    unit_max_errors_per_1000m2: float = -1.0   # 每 1000m² 净面积错误数上限
    unit_max_open_rooms_pct: float = 0.05      # 围护不闭合房间占比上限
    unit_max_open_rooms: int = 2      # 围护不闭合房间绝对数量上限
    unit_max_unassigned_openings: int = 5      # 未归属门窗数量上限
    unit_max_size_anomaly_pct: float = 0.05    # 尺寸异常门窗占比上限
    unit_max_size_anomaly: int = -1   # 尺寸异常门窗绝对数量上限
    unit_max_dup_groups: int = 0      # 重复构件组数量上限

    # ---- 项目级（对汇总结果评估）----
    project_max_errors: int = -1      # 项目错误总数上限（-1=不限制）
    project_max_dup_groups: int = -1  # 项目重复构件组总数上限
    project_max_zero_area_rooms: int = -1  # 净面积为 0 的房间数上限
    min_units: int = 1                # 至少纳入的单体数（防漏传文件）

    # ---- 批次完整性 ----
    allow_failed_files: bool = False  # 是否允许存在核查失败（无法解析）的 IFC

    @property
    def enabled(self) -> bool:
        """门禁是否实际启用（none 预设下所有规则都关闭）。"""
        return self != for_gate_profile("none")


# 内置预设：在默认值基础上覆盖
_PROFILE_VALUES: dict[str, dict] = {
    "default": {},
    # 严格：竣工审查，警告与异常占比也收紧
    "strict": {
        "unit_max_errors": 0,
        "unit_max_warnings": 10,
        "unit_max_errors_per_1000m2": 2.0,
        "unit_max_open_rooms_pct": 0.02,
        "unit_max_open_rooms": 1,
        "unit_max_unassigned_openings": 2,
        "unit_max_size_anomaly_pct": 0.02,
        "unit_max_size_anomaly": 5,
        "unit_max_dup_groups": 0,
        "project_max_errors": 0,
        "project_max_dup_groups": 0,
        "project_max_zero_area_rooms": 0,
        "min_units": 1,
        "allow_failed_files": False,
    },
    # 宽松：方案阶段粗模，允许少量错误存在
    "loose": {
        "unit_max_errors": 5,
        "unit_max_warnings": -1,
        "unit_max_errors_per_1000m2": -1.0,
        "unit_max_open_rooms_pct": 0.10,
        "unit_max_open_rooms": -1,
        "unit_max_unassigned_openings": -1,
        "unit_max_size_anomaly_pct": 0.10,
        "unit_max_size_anomaly": -1,
        "unit_max_dup_groups": 3,
        "project_max_errors": -1,
        "project_max_dup_groups": -1,
        "project_max_zero_area_rooms": -1,
        "min_units": 1,
        "allow_failed_files": False,
    },
    # 不设门禁：所有上限关闭，但 min_units 仍保留 1 防误用
    "none": {
        "unit_max_errors": -1,
        "unit_max_warnings": -1,
        "unit_max_errors_per_1000m2": -1.0,
        "unit_max_open_rooms_pct": -1.0,
        "unit_max_open_rooms": -1,
        "unit_max_unassigned_openings": -1,
        "unit_max_size_anomaly_pct": -1.0,
        "unit_max_size_anomaly": -1,
        "unit_max_dup_groups": -1,
        "project_max_errors": -1,
        "project_max_dup_groups": -1,
        "project_max_zero_area_rooms": -1,
        "min_units": 1,
        "allow_failed_files": True,
    },
}


def for_gate_profile(name: str = "default") -> QualityGate:
    """按内置预设名构造门禁规则。"""
    if name not in GATE_PROFILES:
        raise ValueError(
            f"未知门禁预设“{name}”，可选：{', '.join(GATE_PROFILES)}")
    return QualityGate(**_PROFILE_VALUES[name])


DEFAULT_GATE = for_gate_profile("default")


# ------------------------------------------------------------ 字段元数据 ----

GATE_GROUP_CN = {
    "unit": "单体级规则",
    "project": "项目级规则",
    "batch": "批次完整性",
}


@dataclass(frozen=True)
class GateSpec:
    """单条门禁规则的用户面描述（配置文件 / 报告用）。"""

    key: str
    attr: str
    label: str
    unit: str          # int / % / density / bool
    group: str
    hint: str

    def to_user(self, value):
        if self.unit == "%":
            return value * 100.0
        return value

    def from_user(self, value):
        if self.unit == "%":
            return value / 100.0
        return value


_INT = "int"
_PCT = "%"
_DENSITY = "density"
_BOOL = "bool"

META: dict[str, GateSpec] = {
    # ---- 单体级 ----
    "unit_max_errors": GateSpec(
        "unit_max_errors", "unit_max_errors", "单体错误数上限",
        _INT, "unit",
        "任一单体错误级问题数超过该值即阻断（默认 0：零容忍）；-1 表示不限制"),
    "unit_max_warnings": GateSpec(
        "unit_max_warnings", "unit_max_warnings", "单体警告数上限",
        _INT, "unit",
        "任一单体警告级问题数超过该值即阻断；-1 表示不限制"),
    "unit_max_errors_per_1000m2": GateSpec(
        "unit_max_errors_per_1000m2", "unit_max_errors_per_1000m2",
        "单体每千平方米错误数上限", _DENSITY, "unit",
        "错误数 / 净面积(千m²) 超过该值即阻断，用于大体量单体的密度控制；-1 表示不限制"),
    "unit_max_open_rooms_pct": GateSpec(
        "unit_max_open_rooms_pct", "unit_max_open_rooms_pct",
        "围护不闭合房间占比上限", _PCT, "unit",
        "围护不闭合房间 / 房间总数的占比上限（%%）；-1 表示不限制"),
    "unit_max_open_rooms": GateSpec(
        "unit_max_open_rooms", "unit_max_open_rooms",
        "围护不闭合房间数上限", _INT, "unit",
        "单体围护不闭合房间绝对数量上限，与占比规则同时生效（任一超限即阻断）；-1 表示不限制"),
    "unit_max_unassigned_openings": GateSpec(
        "unit_max_unassigned_openings", "unit_max_unassigned_openings",
        "未归属门窗数上限", _INT, "unit",
        "单体内没有归到任何房间的门/窗数量上限；-1 表示不限制"),
    "unit_max_size_anomaly_pct": GateSpec(
        "unit_max_size_anomaly_pct", "unit_max_size_anomaly_pct",
        "尺寸异常门窗占比上限", _PCT, "unit",
        "尺寸异常或缺失的门窗 / 门窗总数的占比上限（%%）；-1 表示不限制"),
    "unit_max_size_anomaly": GateSpec(
        "unit_max_size_anomaly", "unit_max_size_anomaly",
        "尺寸异常门窗数上限", _INT, "unit",
        "单体内尺寸异常门窗的绝对数量上限，与占比规则同时生效；-1 表示不限制"),
    "unit_max_dup_groups": GateSpec(
        "unit_max_dup_groups", "unit_max_dup_groups",
        "单体重复构件组数上限", _INT, "unit",
        "单体内重复构件聚类组数上限（默认 0：不允许重复构件）；-1 表示不限制"),
    # ---- 项目级 ----
    "project_max_errors": GateSpec(
        "project_max_errors", "project_max_errors", "项目错误总数上限",
        _INT, "project",
        "项目全部单体错误级问题合计上限；-1 表示不限制"),
    "project_max_dup_groups": GateSpec(
        "project_max_dup_groups", "project_max_dup_groups",
        "项目重复构件组总数上限", _INT, "project",
        "项目全部单体重复构件组合计上限；-1 表示不限制"),
    "project_max_zero_area_rooms": GateSpec(
        "project_max_zero_area_rooms", "project_max_zero_area_rooms",
        "净面积为0的房间数上限", _INT, "project",
        "项目内净面积为 0（无几何 / 无声明值）的房间总数上限；-1 表示不限制"),
    "min_units": GateSpec(
        "min_units", "min_units", "批次最少单体数", _INT, "project",
        "批次纳入的 IFC 文件数少于该值时阻断（防止漏传单体）"),
    # ---- 批次完整性 ----
    "allow_failed_files": GateSpec(
        "allow_failed_files", "allow_failed_files", "允许存在核查失败的文件",
        _BOOL, "batch",
        "true=某个 IFC 无法解析时只记错误继续；false=存在失败文件即阻断放行"),
}

_ATTR_TO_KEY = {spec.attr: key for key, spec in META.items()}
_VALID_ATTRS = {f.name for f in fields(QualityGate)}


# ---------------------------------------------------------------- 来源 ----

@dataclass
class GateProvenance:
    """本次门禁规则的来源信息，供报告注明。"""

    profile: str = "default"
    base_profile: str = "default"
    config_path: Optional[str] = None
    overrides: dict[str, object] = field(default_factory=dict)
    rule_pack_id: Optional[str] = None  # 来自企业规则包时记 “名称@版本”

    def describe(self) -> str:
        parts = []
        if self.rule_pack_id:
            parts.append(f"规则包 {self.rule_pack_id}")
        parts.append(GATE_PROFILE_CN.get(self.profile, self.profile))
        if self.config_path:
            parts.append(f"配置文件 {self.config_path}")
        if self.overrides:
            parts.append("调整项：" + ", ".join(
                f"{k}={_format_user(k, v)}"
                for k, v in sorted(self.overrides.items())
                if not _is_disabled(k, v)))
        return "；".join(parts)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "profile_name": GATE_PROFILE_CN.get(self.profile, self.profile),
            "base_profile": self.base_profile,
            "config_path": self.config_path,
            "overrides": self.overrides,
            "rule_pack_id": self.rule_pack_id,
            "description": self.describe(),
        }


def _format_user(key: str, value) -> str:
    spec = META[key]
    if spec.unit == _PCT:
        return f"{value:g}%"
    if spec.unit == _BOOL:
        return "true" if value else "false"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _is_disabled(key: str, value) -> bool:
    """覆盖值是否表示“关闭该规则”（-1 上限），来源描述中不再列出。"""
    spec = META[key]
    return spec.unit in (_INT, _PCT, _DENSITY) and isinstance(value, (int, float)) \
        and value < 0


# ---------------------------------------------------------------- 解析 ----

class GateConfigError(ValueError):
    """门禁配置文件 / 命令行参数有误。"""


def _coerce(key: str, raw):
    spec = META[key]
    if spec.unit == _BOOL:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "1", "yes", "是"):
            return True
        if isinstance(raw, str) and raw.strip().lower() in ("false", "0", "no", "否"):
            return False
        raise GateConfigError(f"门禁 {key} 的值“{raw}”应为 true/false")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise GateConfigError(f"门禁 {key} 的值“{raw}”不是数字")
    if spec.unit == _INT:
        if v != int(v):
            raise GateConfigError(f"门禁 {key} 的值“{raw}”应为整数")
        return int(v)
    return v


def _apply(base: QualityGate, values: dict[str, object]) -> QualityGate:
    updates = {}
    for key, val in values.items():
        spec = META[key]
        v = spec.from_user(val)
        if spec.unit == _INT:
            v = int(v)
        updates[spec.attr] = v
    return QualityGate(**{**asdict(base), **updates})


def resolve_gate(profile: str = "default",
                 config_path: Optional[str] = None,
                 overrides: Optional[dict[str, object]] = None
                 ) -> tuple[QualityGate, GateProvenance]:
    """按“预设 → 配置文件 → 单项覆盖”的优先级解析门禁规则。"""
    if profile not in GATE_PROFILES:
        raise GateConfigError(
            f"未知门禁预设“{profile}”，可选：{', '.join(GATE_PROFILES)}")
    base = for_gate_profile(profile)
    used_profile = profile
    cfg_overrides: dict[str, object] = {}

    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            raise GateConfigError(f"门禁配置文件不存在：{config_path}")
        except json.JSONDecodeError as exc:
            raise GateConfigError(
                f"门禁配置文件不是合法 JSON：{config_path}（{exc}）")
        if not isinstance(cfg, dict):
            raise GateConfigError("门禁配置文件顶层必须是 JSON 对象")

        cfg_profile = cfg.get("profile")
        if cfg_profile is not None:
            if cfg_profile not in GATE_PROFILES:
                raise GateConfigError(
                    f"配置文件中的 profile“{cfg_profile}”无效，"
                    f"可选：{', '.join(GATE_PROFILES)}")
            if cfg_profile != used_profile and profile == "default":
                base = for_gate_profile(cfg_profile)
                used_profile = cfg_profile
        for key, raw in cfg.items():
            if key == "profile" or key.startswith("_"):
                continue
            if key not in META:
                raise GateConfigError(
                    f"配置文件中存在未知门禁键“{key}”，"
                    f"可用键：{', '.join(META)}")
            cfg_overrides[key] = _coerce(key, raw)
        if cfg_overrides:
            base = _apply(base, cfg_overrides)

    final_overrides: dict[str, object] = {}
    if overrides:
        for k, raw in overrides.items():
            key = k if k in META else _ATTR_TO_KEY.get(k)
            if key is None:
                raise GateConfigError(
                    f"未知门禁键“{k}”，可用键：{', '.join(META)}")
            final_overrides[key] = _coerce(key, raw)
        if final_overrides:
            base = _apply(base, final_overrides)

    all_overrides = {**cfg_overrides, **final_overrides}
    eff_profile = "custom" if all_overrides else used_profile
    return base, GateProvenance(
        profile=eff_profile,
        base_profile=used_profile,
        config_path=config_path,
        overrides=all_overrides,
    )


def parse_gate_set_items(items: list[str]) -> dict[str, object]:
    """解析命令行 ``--gate-set key=value``（可重复）。"""
    out: dict[str, object] = {}
    for item in items:
        if "=" not in item:
            raise GateConfigError(
                f"--gate-set 参数格式应为 key=value：“{item}”")
        key, raw = item.split("=", 1)
        key, raw = key.strip(), raw.strip()
        if key not in META:
            raise GateConfigError(
                f"--gate-set 未知门禁键“{key}”，可用键：{', '.join(META)}")
        out[key] = _coerce(key, raw)
    return out


# ------------------------------------------------------------ 模板导出 ----

def gate_config_template(profile: str = "default") -> dict:
    """生成带说明字段的门禁配置模板。"""
    g = for_gate_profile(profile)
    doc: dict[str, object] = {
        "_说明": (
            "项目放行门禁配置：占比字段使用百分比(%)，数量字段为整数，"
            "布尔字段为 true/false；上限类字段取 -1 表示不启用该条规则。"
            "profile 可选 default/strict/loose/none。"
            "修改后用 `python -m ifc_audit.cli batch ifc目录/ "
            "--gate-config 本文件.json` 生效；不达标时命令以退出码 3 阻断放行。"),
        "profile": profile,
    }
    groups_seen = []
    for spec in META.values():
        if spec.group not in groups_seen:
            groups_seen.append(spec.group)
            doc[f"_{GATE_GROUP_CN[spec.group]}"] = ""
        v = getattr(g, spec.attr)
        doc[spec.key] = spec.to_user(v)
        doc[f"_{spec.key}_说明"] = spec.hint
    return doc


def write_gate_config_template(path: str, profile: str = "default") -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gate_config_template(profile), f, ensure_ascii=False,
                  indent=2)
    return path


def gate_rows_for_report(g: QualityGate) -> list[tuple[str, str, str, str]]:
    """报告用的门禁规则行：(分组, 中文名, 显示值, 配置键)。"""
    rows = []
    for spec in META.values():
        v = getattr(g, spec.attr)
        if spec.unit == _PCT:
            shown = "不限制" if v < 0 else f"{v * 100:g}%"
        elif spec.unit == _BOOL:
            shown = "是" if v else "否"
        elif spec.unit in (_INT, _DENSITY):
            shown = "不限制" if v < 0 else f"{v:g}"
        else:
            shown = str(v)
        rows.append((GATE_GROUP_CN[spec.group], spec.label, shown, spec.key))
    return rows
