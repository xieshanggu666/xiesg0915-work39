"""企业审查规则库（Rule Pack Library）。

管理员把**核查项开关、判定阈值、放行条件**组合成可命名、可版本化的
**规则包（Rule Pack）**，指定适用项目与阶段；发布（publish）后各项目
核查时按项目名 / 阶段自动匹配规则包版本（也可显式指定），
报告标注所用规则包名称与版本以便追溯。

核心概念：

* :class:`RulePackContent` —— 规则包可编辑内容（核查项 + 阈值 + 门禁 +
  适用范围），对应一份草稿 JSON；
* :class:`PublishedRulePack` —— 发布后的不可变快照，带版本号、内容指纹
  （sha256）、发布时间与适用范围；
* :class:`RulePackLibrary` —— 规则库（磁盘目录），管理草稿与全部已发布
  版本，并按项目 / 阶段选择适用规则包；
* :func:`materialize` —— 把已发布规则包物化为本次核查实际使用的
  阈值 / 门禁 / 核查项集合，以及供报告追溯的 :class:`RulePackRef`。

规则库目录结构::

    <library>/
      index.json                 # 库索引（名称 -> 草稿/版本清单）
      drafts/<名称>.json          # 可编辑草稿
      published/<名称>/<版本>.json  # 不可变发布快照

版本号采用语义化版本 ``主.次.修``（如 ``1.0.0``、``2.1.3``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .thresholds import (
    Thresholds, ThresholdProvenance, PROFILES as TH_PROFILES,
    resolve as resolve_thresholds,
)
from .gate import (
    QualityGate, GateProvenance, GATE_PROFILES, resolve_gate,
)


# ------------------------------------------------------------ 核查项定义 ----

# 核查项标识（规则包内使用，稳定英文标识）
CHECK_WALL_CLOSURE = "wall_closure"          # 未闭合的墙（自由端 / 墙段缺口）
CHECK_ROOM_ENVELOPE = "room_envelope"        # 房间围护缺口 / 房间无几何
CHECK_DUPLICATE = "duplicate_element"        # 重复构件
CHECK_ROOM_AREA = "room_area"                # 房间净面积（缺声明 / 偏差）
CHECK_OPENING_SIZE = "opening_size"          # 门窗尺寸异常
CHECK_OPENING_ASSIGN = "opening_assignment"  # 门窗未归属房间
CHECK_MEP_CLASH = "mep_clash"                # 多专业硬碰撞
CHECK_RESERVED_OPENING = "reserved_opening"  # 预留洞口核对（缺失/不符/未使用）

CHECKS = (
    CHECK_WALL_CLOSURE,
    CHECK_ROOM_ENVELOPE,
    CHECK_DUPLICATE,
    CHECK_ROOM_AREA,
    CHECK_OPENING_SIZE,
    CHECK_OPENING_ASSIGN,
    CHECK_MEP_CLASH,
    CHECK_RESERVED_OPENING,
)

CHECK_CN = {
    CHECK_WALL_CLOSURE: "未闭合的墙（自由端 / 墙段缺口）",
    CHECK_ROOM_ENVELOPE: "房间围护缺口（含无几何房间）",
    CHECK_DUPLICATE: "重复构件",
    CHECK_ROOM_AREA: "房间净面积（缺声明 / 面积偏差）",
    CHECK_OPENING_SIZE: "门窗尺寸异常",
    CHECK_OPENING_ASSIGN: "门窗未归属房间",
    CHECK_MEP_CLASH: "多专业协同：专业间硬碰撞",
    CHECK_RESERVED_OPENING: "多专业协同：预留洞口核对",
}

# 核查项 -> 该核查项产生的问题 kind（model.Issue.kind）
CHECK_ISSUE_KINDS: dict[str, tuple[str, ...]] = {
    CHECK_WALL_CLOSURE: ("wall_free_end", "wall_end_gap"),
    CHECK_ROOM_ENVELOPE: ("room_enclosure_gap", "room_no_geometry"),
    CHECK_DUPLICATE: ("duplicate_element",),
    CHECK_ROOM_AREA: ("area_missing_declared", "area_mismatch"),
    CHECK_OPENING_SIZE: ("opening_size_anomaly",),
    CHECK_OPENING_ASSIGN: ("opening_unassigned",),
    CHECK_MEP_CLASH: ("coord_hard_clash", "coord_opening_missing"),
    CHECK_RESERVED_OPENING: (
        "coord_opening_mismatch", "coord_opening_unused"),
}

# 核查项 -> 受其控制的门禁规则键（关闭核查项时对应门禁规则自动不参与判定，
# 避免「不核查却仍按 0 阻断」）。错误 / 警告总数类门禁要求全部核查项启用
# 才有意义，因此也在此映射内。
CHECK_GATE_KEYS: dict[str, tuple[str, ...]] = {
    CHECK_WALL_CLOSURE: (),
    CHECK_ROOM_ENVELOPE: (
        "unit_max_open_rooms_pct", "unit_max_open_rooms",
    ),
    CHECK_DUPLICATE: (
        "unit_max_dup_groups", "project_max_dup_groups",
    ),
    CHECK_ROOM_AREA: ("project_max_zero_area_rooms",),
    CHECK_OPENING_SIZE: (
        "unit_max_size_anomaly_pct", "unit_max_size_anomaly",
    ),
    CHECK_OPENING_ASSIGN: ("unit_max_unassigned_openings",),
    # 协同核查项对应协同门禁（批次级，仅多专业协同时生效）
    CHECK_MEP_CLASH: ("coord_max_clash_active",),
    CHECK_RESERVED_OPENING: (
        "coord_max_mismatch_active", "coord_max_unused_active",
    ),
}
# 错误 / 警告 / 密度类汇总门禁：任何核查项关闭时统计口径都不完整
AGGREGATE_GATE_KEYS = (
    "unit_max_errors", "unit_max_warnings",
    "unit_max_errors_per_1000m2", "project_max_errors",
)

# 规则包适用阶段（按由粗到细排序）
STAGES = ("scheme", "construction_drawing", "submission", "completion")
STAGE_CN = {
    "scheme": "方案阶段",
    "construction_drawing": "施工图阶段",
    "submission": "提模审查阶段",
    "completion": "竣工阶段",
}

DEFAULT_RULE_LIBRARY = os.path.join("output", "rule_library")

_NAME_RE = re.compile(r"^[0-9A-Za-z一-鿿][0-9A-Za-z一-鿿._ -]{0,63}$")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class RulePackError(ValueError):
    """规则包 / 规则库内容有误。"""


# ------------------------------------------------------------- 版本比较 ----

def parse_version(v: str) -> tuple[int, int, int]:
    """解析语义化版本 ``主.次.修``。"""
    m = _VERSION_RE.match((v or "").strip())
    if not m:
        raise RulePackError(
            f"版本号“{v}”不是语义化版本（应为 主.次.修，如 1.0.0）")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


# ------------------------------------------------------------- 数据结构 ----

@dataclass
class RulePackContent:
    """规则包的可编辑内容（草稿 / 发布前内容）。"""

    name: str
    description: str = ""
    threshold_profile: str = "default"
    gate_profile: str = "default"
    checks: dict[str, bool] = field(
        default_factory=lambda: {c: True for c in CHECKS})
    thresholds: dict[str, object] = field(default_factory=dict)
    gate_rules: dict[str, object] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)   # 适用项目；空=全部项目
    stages: list[str] = field(default_factory=list)     # 适用阶段；空=全部阶段

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "name": self.name,
            "description": self.description,
            "applicability": {
                "projects": list(self.projects),
                "stages": list(self.stages),
            },
            "checks": {c: bool(self.checks.get(c, True)) for c in CHECKS},
            "threshold_profile": self.threshold_profile,
            "thresholds": dict(self.thresholds),
            "gate_profile": self.gate_profile,
            "gate_rules": dict(self.gate_rules),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RulePackContent":
        if not isinstance(data, dict):
            raise RulePackError("规则包文件顶层必须是 JSON 对象")
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise RulePackError("规则包缺少 name（规则包名称）")
        c = cls(name=name)
        c.description = str(data.get("description", "") or "")
        c.threshold_profile = data.get("threshold_profile", "default")
        c.gate_profile = data.get("gate_profile", "default")
        if c.threshold_profile not in TH_PROFILES:
            raise RulePackError(
                f"规则包 {name} 的 threshold_profile“{c.threshold_profile}”"
                f"无效，可选：{', '.join(TH_PROFILES)}")
        if c.gate_profile not in GATE_PROFILES:
            raise RulePackError(
                f"规则包 {name} 的 gate_profile“{c.gate_profile}”无效，"
                f"可选：{', '.join(GATE_PROFILES)}")

        checks = data.get("checks")
        if checks is None:
            c.checks = {k: True for k in CHECKS}
        elif isinstance(checks, dict):
            unknown = [k for k in checks if k not in CHECKS]
            if unknown:
                raise RulePackError(
                    f"规则包 {name} 存在未知核查项：{', '.join(unknown)}；"
                    f"可用：{', '.join(CHECKS)}")
            if not all(isinstance(v, bool) for v in checks.values()):
                raise RulePackError(f"规则包 {name} 的 checks 取值必须为 true/false")
            c.checks = {k: bool(checks.get(k, True)) for k in CHECKS}
        else:
            raise RulePackError(f"规则包 {name} 的 checks 必须是对象")
        if not any(c.checks.values()):
            raise RulePackError(f"规则包 {name} 至少要启用一个核查项")

        c.thresholds = dict(data.get("thresholds") or {})
        c.gate_rules = dict(data.get("gate_rules") or {})
        if not isinstance(c.thresholds, dict) or not isinstance(c.gate_rules, dict):
            raise RulePackError(f"规则包 {name} 的 thresholds/gate_rules 必须是对象")

        appl = data.get("applicability") or {}
        if not isinstance(appl, dict):
            raise RulePackError(f"规则包 {name} 的 applicability 必须是对象")
        c.projects = [str(p) for p in appl.get("projects", []) or []]
        c.stages = [str(s) for s in appl.get("stages", []) or []]
        bad_stages = [s for s in c.stages if s not in STAGES]
        if bad_stages:
            raise RulePackError(
                f"规则包 {name} 存在未知阶段：{', '.join(bad_stages)}；"
                f"可选：{', '.join(STAGES)}（{', '.join(STAGE_CN.values())}）")
        return c

    def enabled_checks(self) -> list[str]:
        return [c for c in CHECKS if self.checks.get(c, True)]

    def validate_settings(self) -> None:
        """提前用阈值 / 门禁解析器校验取值（发布前给出明确报错）。"""
        if not any(self.checks.values()):
            raise RulePackError(
                f"规则包 {self.name} 至少要启用一个核查项")
        unknown = [k for k in self.checks if k not in CHECKS]
        if unknown:
            raise RulePackError(
                f"规则包 {self.name} 存在未知核查项：{', '.join(unknown)}")
        if not all(isinstance(v, bool) for v in self.checks.values()):
            raise RulePackError(
                f"规则包 {self.name} 的 checks 取值必须为 true/false")
        from .thresholds import ThresholdConfigError
        from .gate import GateConfigError
        try:
            resolve_thresholds(self.threshold_profile, None,
                               self.thresholds or None)
            resolve_gate(self.gate_profile, None, self.gate_rules or None)
        except (ThresholdConfigError, GateConfigError) as exc:
            raise RulePackError(f"规则包 {self.name} 内容无效：{exc}") from exc


@dataclass
class PublishedRulePack:
    """已发布的不可变规则包快照。"""

    name: str
    version: str
    description: str
    projects: list[str]
    stages: list[str]
    checks: dict[str, bool]
    threshold_profile: str
    thresholds: dict[str, object]
    gate_profile: str
    gate_rules: dict[str, object]
    published_at: str
    published_by: str = ""
    deprecated: bool = False
    content_hash: str = ""
    path: str = ""

    @property
    def id(self) -> str:
        """完整标识：``名称@版本``。"""
        return f"{self.name}@{self.version}"

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return parse_version(self.version)

    def applies_to(self, project: str, stage: str) -> bool:
        if self.projects and project not in self.projects:
            return False
        if self.stages and stage not in self.stages:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "applicability": {
                "projects": list(self.projects),
                "stages": list(self.stages),
            },
            "checks": dict(self.checks),
            "threshold_profile": self.threshold_profile,
            "thresholds": dict(self.thresholds),
            "gate_profile": self.gate_profile,
            "gate_rules": dict(self.gate_rules),
            "published_at": self.published_at,
            "published_by": self.published_by,
            "deprecated": self.deprecated,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict, path: str = "") -> "PublishedRulePack":
        appl = data.get("applicability") or {}
        return cls(
            name=data["name"], version=data["version"],
            description=data.get("description", ""),
            projects=list(appl.get("projects", []) or []),
            stages=list(appl.get("stages", []) or []),
            checks=dict(data.get("checks") or {}),
            threshold_profile=data.get("threshold_profile", "default"),
            thresholds=dict(data.get("thresholds") or {}),
            gate_profile=data.get("gate_profile", "default"),
            gate_rules=dict(data.get("gate_rules") or {}),
            published_at=data.get("published_at", ""),
            published_by=data.get("published_by", ""),
            deprecated=bool(data.get("deprecated", False)),
            content_hash=data.get("content_hash", ""),
            path=path,
        )


@dataclass
class RulePackRef:
    """本次核查实际使用的规则包引用（写入报告 / JSON 以便追溯）。"""

    name: str
    version: str
    content_hash: str
    projects: list[str]
    stages: list[str]
    published_at: str = ""
    source_path: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def describe(self) -> str:
        scope = []
        scope.append("适用项目：" + ("、".join(self.projects) if self.projects else "全部"))
        scope.append("阶段：" + ("、".join(STAGE_CN.get(s, s) for s in self.stages)
                                if self.stages else "全部"))
        return f"{self.id}（{ '；'.join(scope)}）"

    def short(self) -> str:
        return self.id

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "id": self.id,
            "content_hash": self.content_hash,
            "projects": list(self.projects),
            "stages": list(self.stages),
            "stage_names": [STAGE_CN.get(s, s) for s in self.stages],
            "published_at": self.published_at,
            "source_path": self.source_path,
        }


@dataclass
class MaterializedRulePack:
    """规则包物化结果：直接喂给核查流水线的阈值 / 门禁 / 核查项。"""

    thresholds: Thresholds
    threshold_provenance: ThresholdProvenance
    gate: QualityGate
    gate_provenance: GateProvenance
    enabled_kinds: set[str]
    enabled_checks: list[str]
    ref: RulePackRef


# ------------------------------------------------------------- 内容指纹 ----

def _fingerprint(content: RulePackContent) -> str:
    """对规则包可编辑内容计算 sha256（不含版本 / 发布时间等元信息）。"""
    payload = json.dumps(content.to_dict(), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def disabled_gate_keys(enabled_checks: list[str]) -> set[str]:
    """根据启用的核查项推导不参与判定的门禁规则键。"""
    disabled: set[str] = set()
    enabled = set(enabled_checks)
    if enabled != set(CHECKS):
        disabled.update(AGGREGATE_GATE_KEYS)
    for check, keys in CHECK_GATE_KEYS.items():
        if check not in enabled:
            disabled.update(keys)
    return disabled


def enabled_checks_from_kinds(enabled_kinds: Optional[set[str]]) -> list[str]:
    """从启用的问题种类反推启用的核查项（None=全部启用）。"""
    if enabled_kinds is None:
        return list(CHECKS)
    return [c for c in CHECKS
            if any(k in enabled_kinds for k in CHECK_ISSUE_KINDS[c])]


def materialize(pack: PublishedRulePack) -> MaterializedRulePack:
    """把已发布规则包物化为阈值 / 门禁 / 启用问题种类与追溯引用。"""
    content = RulePackContent(
        name=pack.name, description=pack.description,
        threshold_profile=pack.threshold_profile,
        gate_profile=pack.gate_profile,
        checks=dict(pack.checks),
        thresholds=dict(pack.thresholds),
        gate_rules=dict(pack.gate_rules),
        projects=list(pack.projects), stages=list(pack.stages))
    content.validate_settings()

    th, th_prov = resolve_thresholds(
        pack.threshold_profile, None, pack.thresholds or None)
    gate, gate_prov = resolve_gate(
        pack.gate_profile, None, pack.gate_rules or None)
    # 规则包是阈值 / 门禁的来源，报告中据此标注
    th_prov.rule_pack_id = pack.id
    gate_prov.rule_pack_id = pack.id

    enabled = content.enabled_checks()
    kinds: set[str] = set()
    for check in enabled:
        kinds.update(CHECK_ISSUE_KINDS[check])

    ref = RulePackRef(
        name=pack.name, version=pack.version,
        content_hash=pack.content_hash, projects=list(pack.projects),
        stages=list(pack.stages), published_at=pack.published_at,
        source_path=pack.path)
    return MaterializedRulePack(
        thresholds=th, threshold_provenance=th_prov,
        gate=gate, gate_provenance=gate_prov,
        enabled_kinds=kinds, enabled_checks=enabled, ref=ref)


# --------------------------------------------------------------- 规则库 ----

class RulePackLibrary:
    """磁盘上的企业审查规则库。"""

    def __init__(self, root: str = DEFAULT_RULE_LIBRARY):
        self.root = os.path.abspath(root)
        self.drafts_dir = os.path.join(self.root, "drafts")
        self.published_dir = os.path.join(self.root, "published")
        self.index_path = os.path.join(self.root, "index.json")

    # ---- 初始化 / 索引 ----
    def init(self) -> None:
        os.makedirs(self.drafts_dir, exist_ok=True)
        os.makedirs(self.published_dir, exist_ok=True)
        if not os.path.exists(self.index_path):
            self._write_index({"schema_version": 1, "packs": {}})

    def _read_index(self) -> dict:
        if not os.path.exists(self.index_path):
            return {"schema_version": 1, "packs": {}}
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
        except json.JSONDecodeError as exc:
            raise RulePackError(f"规则库索引损坏：{self.index_path}（{exc}）")
        idx.setdefault("packs", {})
        return idx

    def _write_index(self, idx: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def list_packs(self) -> list[dict]:
        """列出库内全部规则包（名称、最新版本、版本数、适用范围、状态）。"""
        idx = self._read_index()
        out = []
        for name, entry in sorted(idx.get("packs", {}).items()):
            versions = entry.get("versions", [])
            latest = self.load_published(name, entry["latest"]) \
                if entry.get("latest") else None
            out.append({
                "name": name,
                "description": entry.get("description", ""),
                "latest": entry.get("latest", ""),
                "n_versions": len(versions),
                "versions": versions,
                "has_draft": bool(entry.get("draft")),
                "projects": latest.projects if latest else [],
                "stages": latest.stages if latest else [],
                "deprecated": latest.deprecated if latest else False,
            })
        return out

    # ---- 草稿 ----
    def draft_path(self, name: str) -> str:
        self._validate_name(name)
        return os.path.join(self.drafts_dir, f"{name}.json")

    def save_draft(self, content: RulePackContent) -> str:
        """新建或覆盖规则包草稿，返回草稿路径。"""
        self._validate_name(content.name)
        content.validate_settings()
        self.init()
        path = self.draft_path(content.name)
        doc = {
            "_说明": (
                "企业审查规则包草稿：checks 开关核查项；threshold_profile 可选 "
                "default/strict/loose；thresholds 为阈值单项覆盖（长度 mm、"
                "面积偏差 %）；gate_profile 可选 default/strict/loose/none；"
                "gate_rules 为放行条件单项覆盖（-1=不限制该条）；"
                "applicability 指定适用项目与阶段（空数组=全部；"
                f"阶段可选 {', '.join(STAGES)}）。"
                "用 `rulepack publish 本草稿.json <版本号>` 发布。"),
            **content.to_dict(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        idx = self._read_index()
        entry = idx["packs"].setdefault(content.name, {"versions": []})
        entry["draft"] = os.path.relpath(path, self.root)
        entry["description"] = content.description
        self._write_index(idx)
        return path

    def load_draft(self, name_or_path: str) -> RulePackContent:
        """按规则包名或草稿文件路径载入草稿。"""
        path = (name_or_path if os.path.sep in name_or_path
                or name_or_path.endswith(".json")
                else self.draft_path(name_or_path))
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return RulePackContent.from_dict(data)

    # ---- 发布 ----
    def publish(self, name_or_path: str, version: str,
                published_by: str = "",
                as_name: Optional[str] = None) -> PublishedRulePack:
        """把草稿发布为不可变版本快照。

        Args:
            name_or_path: 库内规则包名（取其草稿）或草稿 JSON 文件路径。
            version: 语义化版本号；同名版本已存在则报错（不可变）。
            published_by: 发布人（记录在快照中）。
            as_name: 从库外文件发布时指定规则包名称。
        """
        parse_version(version)
        if os.path.isfile(name_or_path):
            content = self.load_draft(name_or_path)
            if as_name:
                content.name = as_name
        else:
            if as_name:
                raise RulePackError("--as-name 仅在从草稿文件发布时使用")
            content = self.load_draft(name_or_path)
        self._validate_name(content.name)
        content.validate_settings()

        self.init()
        out_dir = os.path.join(self.published_dir, content.name)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{version}.json")
        if os.path.exists(out_path):
            raise RulePackError(
                f"规则包 {content.name} 版本 {version} 已发布，"
                "已发布版本不可修改，请换用更高版本号")

        fingerprint = _fingerprint(content)
        pack = PublishedRulePack(
            name=content.name, version=version,
            description=content.description,
            projects=list(content.projects), stages=list(content.stages),
            checks={c: bool(content.checks.get(c, True)) for c in CHECKS},
            threshold_profile=content.threshold_profile,
            thresholds=dict(content.thresholds),
            gate_profile=content.gate_profile,
            gate_rules=dict(content.gate_rules),
            published_at=datetime.now().isoformat(timespec="seconds"),
            published_by=published_by, deprecated=False,
            content_hash=fingerprint,
            path=out_path)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(pack.to_dict(), f, ensure_ascii=False, indent=2)

        idx = self._read_index()
        entry = idx["packs"].setdefault(
            content.name, {"versions": [], "draft": ""})
        versions = entry.setdefault("versions", [])
        if version not in versions:
            versions.append(version)
        versions.sort(key=parse_version)
        entry["latest"] = self._latest_version(versions)
        entry["description"] = content.description
        self._write_index(idx)
        return pack

    @staticmethod
    def _latest_version(versions: list[str]) -> str:
        return max(versions, key=parse_version)

    def load_published(self, name: str, version: Optional[str] = None
                       ) -> PublishedRulePack:
        """载入已发布规则包；version 为 None 时取最新版本。"""
        idx = self._read_index()
        entry = idx.get("packs", {}).get(name)
        if entry is None:
            raise RulePackError(
                f"规则库（{self.root}）中没有规则包“{name}”，"
                "可用 rulepack list 查看")
        versions = entry.get("versions", [])
        if not versions:
            raise RulePackError(f"规则包“{name}”尚无已发布版本")
        version = version or entry.get("latest") or self._latest_version(versions)
        if version not in versions:
            raise RulePackError(
                f"规则包“{name}”没有版本 {version}，"
                f"已发布：{', '.join(versions)}")
        path = os.path.join(self.published_dir, name, f"{version}.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        stored_hash = data.get("content_hash", "")
        pack = PublishedRulePack.from_dict(data, path=path)
        # 指纹校验：发布快照被人改动时明确报错（版本不可变）
        content = RulePackContent(
            name=pack.name, description=pack.description,
            threshold_profile=pack.threshold_profile,
            gate_profile=pack.gate_profile, checks=dict(pack.checks),
            thresholds=dict(pack.thresholds), gate_rules=dict(pack.gate_rules),
            projects=list(pack.projects), stages=list(pack.stages))
        if stored_hash and _fingerprint(content) != stored_hash:
            raise RulePackError(
                f"规则包 {pack.id} 的内容指纹与发布记录不一致，"
                "快照可能已被改动；请重新发布新版本")
        return pack

    def versions(self, name: str) -> list[str]:
        idx = self._read_index()
        entry = idx.get("packs", {}).get(name)
        return sorted(entry.get("versions", []), key=parse_version) if entry else []

    def set_deprecated(self, name: str, version: str,
                       deprecated: bool) -> PublishedRulePack:
        """标记 / 取消标记某版本废止（废止版本不参与自动选择）。

        废止标记属于管理状态而非规则内容，写在快照中但不改变内容指纹。
        """
        pack = self.load_published(name, version)
        pack.deprecated = deprecated
        with open(pack.path, "w", encoding="utf-8") as f:
            json.dump(pack.to_dict(), f, ensure_ascii=False, indent=2)
        return pack

    # ---- 适用规则包选择 ----
    def select_for(self, project: str, stage: str = "") -> PublishedRulePack:
        """按项目与阶段选择适用的已发布规则包。

        匹配规则（取分数最高者；分数相同取版本高、发布时间新者）：

        1. 规则包的适用项目命中得 2 分（适用项目为空表示全部项目，得 1 分）；
        2. 查询指定了阶段时，适用阶段命中得 2 分、全阶段包得 1 分；
           查询未指定阶段时，只有全阶段包可匹配（得 1 分），阶段限定包排除；
        3. 已废止版本不参与选择。

        没有任何规则包时抛出 :class:`RulePackError`。
        """
        idx = self._read_index()
        candidates: list[tuple[tuple, PublishedRulePack]] = []
        for name, entry in idx.get("packs", {}).items():
            for version in entry.get("versions", []):
                pack = self.load_published(name, version)
                if pack.deprecated:
                    continue
                if pack.projects and project not in pack.projects:
                    continue
                if stage:
                    if pack.stages and stage not in pack.stages:
                        continue
                elif pack.stages:
                    continue
                score_project = 2 if pack.projects else 1
                score_stage = 2 if (stage and pack.stages) else 1
                # 全项目/全阶段的“兜底包”排在精确范围包之后
                if not pack.projects:
                    score_project = 1
                candidates.append(((score_project, score_stage), pack))
        if not candidates:
            scope = f"项目“{project}”" + (f" / 阶段“{stage}”" if stage else "")
            raise RulePackError(
                f"规则库中没有适用于{scope}的已发布规则包；"
                "可用 --rule-pack 显式指定名称@版本，或用 rulepack 子命令发布")
        candidates.sort(key=lambda kv: (
            kv[0], kv[1].version_tuple, kv[1].published_at), reverse=True)
        return candidates[0][1]

    # ---- 校验 ----
    @staticmethod
    def _validate_name(name: str) -> None:
        if not name or not _NAME_RE.match(name):
            raise RulePackError(
                f"规则包名称“{name}”不合法：限中英文/数字/空格及 ._ -，"
                "长度 1~64，且不以特殊符号开头")


# ------------------------------------------------------ 库外快照载入 ----

def load_published_file(path: str) -> PublishedRulePack:
    """直接从一个已发布快照 JSON 文件载入规则包（库外分发场景）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RulePackError(f"规则包文件不存在：{path}")
    except json.JSONDecodeError as exc:
        raise RulePackError(f"规则包文件不是合法 JSON：{path}（{exc}）")
    if "version" not in data or "published_at" not in data:
        raise RulePackError(
            f"{path} 不是已发布的规则包快照（缺少 version/published_at），"
            "请先用 rulepack publish 发布")
    pack = PublishedRulePack.from_dict(data, path=os.path.abspath(path))
    # 库外文件同样做指纹校验
    content = RulePackContent(
        name=pack.name, description=pack.description,
        threshold_profile=pack.threshold_profile,
        gate_profile=pack.gate_profile, checks=dict(pack.checks),
        thresholds=dict(pack.thresholds), gate_rules=dict(pack.gate_rules),
        projects=list(pack.projects), stages=list(pack.stages))
    if pack.content_hash and _fingerprint(content) != pack.content_hash:
        raise RulePackError(
            f"规则包 {pack.id} 的内容指纹与发布记录不一致，快照可能已被改动")
    return pack


def resolve_rule_pack(spec: str, library: RulePackLibrary) -> PublishedRulePack:
    """解析命令行 ``--rule-pack`` 取值。

    接受：``名称``（库内最新）、``名称@版本``、已发布快照 JSON 文件路径。
    """
    if os.path.sep in spec or spec.endswith(".json"):
        return load_published_file(spec)
    if "@" in spec:
        name, version = spec.split("@", 1)
        return library.load_published(name.strip(), version.strip())
    return library.load_published(spec.strip())


# ----------------------------------------------------------- 草稿模板 ----

def new_draft(name: str, description: str = "",
              projects: Optional[list[str]] = None,
              stages: Optional[list[str]] = None,
              threshold_profile: str = "default",
              gate_profile: str = "default") -> RulePackContent:
    """以内置预设为基准新建一份草稿（核查项全开，可再逐项调整）。"""
    return RulePackContent(
        name=name, description=description,
        threshold_profile=threshold_profile, gate_profile=gate_profile,
        checks={c: True for c in CHECKS},
        projects=list(projects or []), stages=list(stages or []))


def write_draft_template(path: str, content: RulePackContent) -> str:
    """把带说明的草稿模板写到 path（独立文件，供 rulepack init 使用）。"""
    doc = {
        "_说明": (
            "企业审查规则包草稿：checks 开关核查项；"
            "threshold_profile 可选 default/strict/loose；"
            "thresholds 为阈值单项覆盖（长度 mm、面积偏差 %、比例 0~1）；"
            "gate_profile 可选 default/strict/loose/none；"
            "gate_rules 为放行条件单项覆盖（上限类 -1=不限制）；"
            f"applicability 指定适用项目与阶段（空数组=全部；阶段可选 "
            f"{', '.join(STAGES)}）。"
            "修改后用 `python -m ifc_audit.cli rulepack publish 本文件.json "
            "<版本号> --as-name <名称>` 发布。"),
        "_核查项说明": {CHECK_CN[c]: c for c in CHECKS},
        "_阶段说明": {STAGE_CN[s]: s for s in STAGES},
        **content.to_dict(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return path


def check_rows_for_report(enabled: dict[str, bool]) -> list[tuple[str, bool, str]]:
    """报告用的核查项行：(中文名, 是否启用, 标识)。"""
    return [(CHECK_CN[c], bool(enabled.get(c, True)), c) for c in CHECKS]
