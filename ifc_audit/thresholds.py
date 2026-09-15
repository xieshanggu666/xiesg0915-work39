"""可配置判定阈值。

四类核查规则的判定阈值集中在此，支持三种方式调整（优先级从低到高）：

1. 内置预设 ``default`` / ``strict`` / ``loose``；
2. JSON 配置文件（``--config``，可用 ``init-config`` 生成带注释模板）；
3. 命令行单项覆盖（``--set key=value``，可重复）。

内部计算一律使用米 / 比例；配置文件与命令行面向用户，长度用 **毫米**、
面积偏差用 **百分比**，字段元数据见 :data:`META`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, asdict
from typing import Optional


# ---------------------------------------------------------------- 预设 ----

PROFILES = ("default", "strict", "loose")
PROFILE_CN = {
    "default": "标准（内置默认）",
    "strict": "严格（高精度模型 / 竣工审查）",
    "loose": "宽松（方案阶段 / 粗糙模型）",
    "custom": "自定义（配置文件或命令行覆盖）",
}


@dataclass(frozen=True)
class Thresholds:
    """一次核查使用的全部判定阈值。

    长度单位均为米；比例 / IoU / 面积偏差为 0~1 的小数。
    """

    # 自由端 / 墙段缺口
    free_end_tol: float = 0.05        # 墙端头伸入其它墙体的判定容差
    endpoint_merge_tol: float = 0.30  # 邻近自由端聚类为“墙段缺口”的容差
    # 房间围护缺口
    gap_min_len: float = 0.10         # 围护缺口最小上报长度
    barrier_buffer: float = 0.005     # 围护构件覆盖边界的外扩容差（毫米级，吸收建模误差）
    # 重复构件
    dup_centroid_tol: float = 0.08    # 形心距离
    dup_vol_ratio: float = 0.85       # 体积相似度（小/大）
    dup_iou: float = 0.70             # 平面轮廓 IoU
    # 房间面积 / 归属
    area_dev_warn: float = 0.02       # 声明面积与几何面积偏差警告线
    # 注意：这里是外扩容差“之外”的归属余量，不含 barrier_buffer；
    # 实际归属距离 = barrier_buffer + assign_tol，默认 5mm + 50mm = 55mm。
    assign_tol: float = 0.05          # 门窗归入房间的归属余量（buffer 另算，勿重复叠加）
    # 门窗规格：小于下限时判尺寸异常（米）；尺寸缺失无论取值如何都会标出
    door_min_width: float = 0.60      # 门最小宽
    door_min_height: float = 1.80     # 门最小高
    win_min_width: float = 0.40       # 窗最小宽
    win_min_height: float = 0.40      # 窗最小高

    @property
    def assign_distance(self) -> float:
        """门窗归入房间的实际距离（外扩容差 + 归属容差）。"""
        return self.barrier_buffer + self.assign_tol


# 内置预设：在默认值基础上覆盖
_PROFILE_VALUES: dict[str, dict] = {
    "default": {},
    # 严格：模型精度高，小问题也应暴露
    "strict": {
        "free_end_tol": 0.03,
        "endpoint_merge_tol": 0.20,
        "gap_min_len": 0.05,
        "barrier_buffer": 0.002,
        "dup_centroid_tol": 0.05,
        "dup_vol_ratio": 0.90,
        "dup_iou": 0.80,
        "area_dev_warn": 0.01,
        "door_min_width": 0.70,
        "door_min_height": 2.00,
        "win_min_width": 0.50,
        "win_min_height": 0.50,
    },
    # 宽松：方案阶段模型较粗，只报明显问题
    "loose": {
        "free_end_tol": 0.10,
        "endpoint_merge_tol": 0.50,
        "gap_min_len": 0.20,
        "barrier_buffer": 0.01,
        "dup_centroid_tol": 0.15,
        "dup_vol_ratio": 0.80,
        "dup_iou": 0.60,
        "area_dev_warn": 0.05,
        "door_min_width": 0.50,
        "door_min_height": 1.50,
        "win_min_width": 0.30,
        "win_min_height": 0.30,
    },
}


def for_profile(name: str = "default") -> Thresholds:
    """按内置预设名构造阈值。"""
    if name not in PROFILES:
        raise ValueError(
            f"未知阈值预设“{name}”，可选：{', '.join(PROFILES)}")
    return Thresholds(**_PROFILE_VALUES[name])


DEFAULT_THRESHOLDS = for_profile("default")


# ------------------------------------------------------------ 字段元数据 ----

GROUP_CN = {
    "free_end": "自由端 / 墙段缺口",
    "envelope": "房间围护缺口",
    "duplicate": "重复构件",
    "area": "房间净面积",
    "openings": "门窗规格",
}


@dataclass(frozen=True)
class ThresholdSpec:
    """单个阈值的用户面描述（配置文件 / GUI / 报告用）。"""

    key: str            # 用户配置键（带单位后缀）
    attr: str           # Thresholds 字段名
    label: str          # 中文名
    unit: str           # 用户面单位：mm / % / ratio
    group: str          # GROUP_CN 的键
    minv: float
    maxv: float
    hint: str

    def to_user(self, value: float) -> float:
        """内部值（米/比例）→ 用户面数值（mm/%）。"""
        if self.unit == "mm":
            return value * 1000.0
        if self.unit == "%":
            return value * 100.0
        return value

    def from_user(self, value: float) -> float:
        """用户面数值 → 内部值。"""
        if self.unit == "mm":
            return value / 1000.0
        if self.unit == "%":
            return value / 100.0
        return value


META: dict[str, ThresholdSpec] = {
    "free_end_tol_mm": ThresholdSpec(
        "free_end_tol_mm", "free_end_tol", "自由端判定容差", "mm",
        "free_end", 0.0, 500.0,
        "墙端头在该范围内没有其它墙体即判为自由端"),
    "endpoint_merge_tol_mm": ThresholdSpec(
        "endpoint_merge_tol_mm", "endpoint_merge_tol",
        "墙段缺口聚类容差", "mm", "free_end", 0.0, 2000.0,
        "两个不同墙的端头间距小于该值时判为墙段缺口，否则各自判为自由端"),
    "gap_min_len_mm": ThresholdSpec(
        "gap_min_len_mm", "gap_min_len", "围护缺口最小长度", "mm",
        "envelope", 0.0, 2000.0,
        "房间边界未被墙/门覆盖的长度超过该值才上报"),
    "barrier_buffer_mm": ThresholdSpec(
        "barrier_buffer_mm", "barrier_buffer", "围护覆盖外扩容差", "mm",
        "envelope", 0.0, 100.0,
        "毫米级建模误差吸收量，过大会掩盖真实缺口"),
    "dup_centroid_tol_mm": ThresholdSpec(
        "dup_centroid_tol_mm", "dup_centroid_tol", "重复构件形心距离", "mm",
        "duplicate", 0.0, 1000.0,
        "同类构件形心距小于该值才进一步比较几何"),
    "dup_vol_ratio": ThresholdSpec(
        "dup_vol_ratio", "dup_vol_ratio", "重复构件体积相似度", "ratio",
        "duplicate", 0.0, 1.0,
        "较小体积 / 较大体积，低于该值不判重（0~1）"),
    "dup_iou": ThresholdSpec(
        "dup_iou", "dup_iou", "重复构件轮廓 IoU", "ratio",
        "duplicate", 0.0, 1.0,
        "平面轮廓交并比阈值，低于该值不判重（0~1）"),
    "area_dev_warn_pct": ThresholdSpec(
        "area_dev_warn_pct", "area_dev_warn", "面积偏差警告线", "%",
        "area", 0.0, 100.0,
        "声明净面积与几何计算值偏差超过该百分比时警告"),
    "door_min_width_mm": ThresholdSpec(
        "door_min_width_mm", "door_min_width", "门最小宽度", "mm",
        "openings", 0.0, 2000.0,
        "门的标称宽度小于该值时在门窗表中标为尺寸异常"),
    "door_min_height_mm": ThresholdSpec(
        "door_min_height_mm", "door_min_height", "门最小高度", "mm",
        "openings", 0.0, 3000.0,
        "门的标称高度小于该值时在门窗表中标为尺寸异常"),
    "win_min_width_mm": ThresholdSpec(
        "win_min_width_mm", "win_min_width", "窗最小宽度", "mm",
        "openings", 0.0, 2000.0,
        "窗的标称宽度小于该值时在门窗表中标为尺寸异常"),
    "win_min_height_mm": ThresholdSpec(
        "win_min_height_mm", "win_min_height", "窗最小高度", "mm",
        "openings", 0.0, 3000.0,
        "窗的标称高度小于该值时在门窗表中标为尺寸异常；宽高均缺失也标异常"),
}

# 内部字段名 -> 用户键（库 API 直接传内部名时也接受）
_ATTR_TO_KEY = {spec.attr: key for key, spec in META.items()}
_VALID_ATTRS = {f.name for f in fields(Thresholds)}

# 配置文件中以 _ 开头的键为注释/元信息，解析时忽略
_RESERVED_PREFIX = "_"


# ---------------------------------------------------------------- 来源 ----

@dataclass
class ThresholdProvenance:
    """本次核查阈值的来源信息，供报告注明。"""

    profile: str = "default"          # 基准预设；被覆盖时记为 "custom"
    base_profile: str = "default"     # 覆盖前的基准预设名
    config_path: Optional[str] = None
    overrides: dict[str, float] = field(default_factory=dict)  # 用户键 -> 用户值
    rule_pack_id: Optional[str] = None  # 来自企业规则包时记 “名称@版本”

    def describe(self) -> str:
        """一句话说明本次用的是哪套阈值（只列相对预设真正改动的项）。"""
        base = for_profile(self.base_profile)
        changed = []
        for key, val in sorted(self.overrides.items()):
            spec = META[key]
            if abs(spec.from_user(val) - getattr(base, spec.attr)) > 1e-12:
                changed.append(f"{key}={_format_user(key, val)}")
        parts = [PROFILE_CN.get(self.profile, self.profile)]
        if self.rule_pack_id:
            parts.insert(0, f"规则包 {self.rule_pack_id}")
        if self.config_path:
            parts.append(f"配置文件 {self.config_path}")
        if changed:
            parts.append(f"调整项：{', '.join(changed)}")
        return "；".join(parts)

    def is_default(self) -> bool:
        return self.profile == "default" and not self.config_path \
            and not self.overrides and not self.rule_pack_id

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "profile_name": PROFILE_CN.get(self.profile, self.profile),
            "base_profile": self.base_profile,
            "config_path": self.config_path,
            "overrides": self.overrides,
            "rule_pack_id": self.rule_pack_id,
            "description": self.describe(),
        }


def _format_user(key: str, value: float) -> str:
    spec = META[key]
    if spec.unit == "mm":
        return f"{value:g}mm"
    if spec.unit == "%":
        return f"{value:g}%"
    return f"{value:g}"


# ---------------------------------------------------------------- 解析 ----

class ThresholdConfigError(ValueError):
    """配置文件 / 命令行阈值参数有误。"""


def _coerce(key: str, raw) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ThresholdConfigError(f"阈值 {key} 的值“{raw}”不是数字")
    spec = META[key]
    if not (spec.minv <= v <= spec.maxv):
        raise ThresholdConfigError(
            f"阈值 {key}={v} 超出允许范围 "
            f"[{spec.minv:g}, {spec.maxv:g}]（{spec.label}）")
    return v


def _apply_user_values(base: Thresholds, values: dict[str, float]) -> Thresholds:
    updates = {}
    for key, val in values.items():
        spec = META[key]
        updates[spec.attr] = spec.from_user(val)
    return Thresholds(**{**asdict(base), **updates})


def resolve(profile: str = "default",
            config_path: Optional[str] = None,
            overrides: Optional[dict[str, float]] = None
            ) -> tuple[Thresholds, ThresholdProvenance]:
    """按“预设 → 配置文件 → 单项覆盖”的优先级解析阈值。

    Args:
        profile: 内置预设名（default/strict/loose）。
        config_path: JSON 配置文件路径；文件内可用 ``profile`` 指定预设，
            其余键为 :data:`META` 中的用户键。
        overrides: 最终单项覆盖，键为用户键（如 ``free_end_tol_mm``），
            值为用户单位数值（mm / %）；库调用也可传内部字段名
            （如 ``free_end_tol``，此时按米/比例解释）。

    Returns:
        (阈值对象, 来源记录)。
    """
    # 1) 命令行/调用方指定的预设为起点；配置文件可再指定
    if profile not in PROFILES:
        raise ThresholdConfigError(
            f"未知阈值预设“{profile}”，可选：{', '.join(PROFILES)}")
    base = for_profile(profile)
    used_profile = profile
    cfg_overrides: dict[str, float] = {}

    # 2) 配置文件
    if config_path:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            raise ThresholdConfigError(f"阈值配置文件不存在：{config_path}")
        except json.JSONDecodeError as exc:
            raise ThresholdConfigError(
                f"阈值配置文件不是合法 JSON：{config_path}（{exc}）")
        if not isinstance(cfg, dict):
            raise ThresholdConfigError("阈值配置文件顶层必须是 JSON 对象")

        cfg_profile = cfg.get("profile")
        if cfg_profile is not None:
            if cfg_profile not in PROFILES:
                raise ThresholdConfigError(
                    f"配置文件中的 profile“{cfg_profile}”无效，"
                    f"可选：{', '.join(PROFILES)}")
            if cfg_profile != used_profile:
                # 显式 --profile 优先于配置文件
                if profile == "default":
                    base = for_profile(cfg_profile)
                    used_profile = cfg_profile
        for key, raw in cfg.items():
            if key == "profile" or key.startswith(_RESERVED_PREFIX):
                continue
            if key not in META:
                raise ThresholdConfigError(
                    f"配置文件中存在未知阈值键“{key}”，"
                    f"可用键：{', '.join(META)}")
            cfg_overrides[key] = _coerce(key, raw)
        if cfg_overrides:
            base = _apply_user_values(base, cfg_overrides)

    # 3) 单项覆盖（命令行 --set 用用户键；库调用也可传内部字段名）
    final_overrides: dict[str, float] = {}
    internal_updates: dict[str, float] = {}
    if overrides:
        for k, raw in overrides.items():
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise ThresholdConfigError(f"阈值 {k} 的值“{raw}”不是数字")
            if k in META:
                final_overrides[k] = _coerce(k, raw)
            elif k in _ATTR_TO_KEY:
                # 内部名（米/比例）-> 对应用户键，便于 provenance 统一记录
                ukey = _ATTR_TO_KEY[k]
                final_overrides[ukey] = META[ukey].to_user(v)
            elif k == "assign_tol":
                # 仅库 API 可用的内部字段（米），无用户配置键
                internal_updates[k] = v
            else:
                raise ThresholdConfigError(
                    f"未知阈值键“{k}”，可用键：{', '.join(META)}")
        if final_overrides:
            base = _apply_user_values(base, final_overrides)
        if internal_updates:
            base = _apply_internal(base, internal_updates)

    all_user_overrides = {**cfg_overrides, **final_overrides}
    # 相对预设做过任何覆盖（含仅库 API 的内部字段），即记为自定义方案
    eff_profile = "custom" if (all_user_overrides or internal_updates) \
        else used_profile

    prov = ThresholdProvenance(
        profile=eff_profile,
        base_profile=used_profile,
        config_path=config_path,
        overrides={**cfg_overrides, **final_overrides},
    )
    return base, prov


def _apply_internal(base: Thresholds, updates: dict[str, float]) -> Thresholds:
    return Thresholds(**{**asdict(base), **updates})


def parse_set_items(items: list[str]) -> dict[str, float]:
    """解析命令行 ``--set key=value``（可重复）。"""
    out: dict[str, float] = {}
    for item in items:
        if "=" not in item:
            raise ThresholdConfigError(
                f"--set 参数格式应为 key=value：“{item}”")
        key, raw = item.split("=", 1)
        key, raw = key.strip(), raw.strip()
        if key not in META:
            raise ThresholdConfigError(
                f"--set 未知阈值键“{key}”，可用键：{', '.join(META)}")
        out[key] = _coerce(key, raw)
    return out


# ------------------------------------------------------------ 模板导出 ----

def config_template(profile: str = "default") -> dict:
    """生成带说明字段的配置模板（可直接写 JSON）。"""
    t = for_profile(profile)
    doc: dict = {
        "_说明": ("阈值配置：长度单位为毫米(mm)，面积偏差为百分比(%)，"
                "比例/IoU 为 0~1 的小数；profile 可选 default/strict/loose。"
                "修改后用 `python -m ifc_audit.cli audit model.ifc "
                "--config 本文件.json` 生效。"),
        "profile": profile,
    }
    groups_seen = []
    for spec in META.values():
        if spec.group not in groups_seen:
            groups_seen.append(spec.group)
            doc[f"_{GROUP_CN[spec.group]}"] = ""
        v = getattr(t, spec.attr)
        doc[spec.key] = round(spec.to_user(v), 6)
        doc[f"_{spec.key}_说明"] = spec.hint
    return doc


def write_config_template(path: str, profile: str = "default") -> str:
    """把带注释的配置模板写到 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config_template(profile), f, ensure_ascii=False, indent=2)
    return path


def rows_for_report(t: Thresholds) -> list[tuple[str, str, str, str]]:
    """报告用的阈值行：(分组, 中文名, 显示值, 配置键)。"""
    rows = []
    for spec in META.values():
        v = getattr(t, spec.attr)
        uv = spec.to_user(v)
        if spec.unit == "mm":
            shown = f"{uv:g} mm"
        elif spec.unit == "%":
            shown = f"{uv:g}%"
        else:
            shown = f"{uv:g}"
        rows.append((GROUP_CN[spec.group], spec.label, shown, spec.key))
    # assign_tol 仅内部使用，也在报告中注明（= 外扩容差 + 归属容差）
    rows.append((GROUP_CN["area"], "门窗归入房间距离",
                 f"{t.assign_distance * 1000:g} mm（自动含外扩容差）",
                 "barrier_buffer_mm + assign_tol"))
    return rows
