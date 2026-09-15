"""多专业协同核查数据模型。

在单专业建筑核查（:mod:`ifc_audit.model`）之外，本模块描述跨专业比对的
数据结构：

* :class:`CoordElement` —— 参与协同核查的构件（建筑 / 结构 / 机电 / 预留洞口），
  只携带协同检测所需的包围盒、轴线段与分类信息；
* :class:`CoordIssue` —— 一条协同问题（硬碰撞 / 预留洞口缺失 / 洞口规格不符 /
  洞口未使用），含责任专业、责任人与「派单 → 整改 → 复核」状态流转；
* :class:`CoordinationLedger` —— 项目级协同台账（跨批次持久化，按指纹合单）；
* :class:`CoordinationResult` —— 一次协同核查的批次结论，与批次门禁联动。

长度单位统一为米，与 :mod:`ifc_audit.model` 保持一致。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------- 专业 ----

DISC_ARCH = "arch"        # 建筑
DISC_STRUCT = "struct"    # 结构
DISC_MEP = "mep"          # 机电（水暖电）
DISCIPLINES = (DISC_ARCH, DISC_STRUCT, DISC_MEP)

DISC_CN = {
    DISC_ARCH: "建筑",
    DISC_STRUCT: "结构",
    DISC_MEP: "机电",
}

# IFC 实体类型 -> 专业（取最具体的标准类别时用 startswith 匹配）
IFC_DISCIPLINE_MAP = {
    # 建筑
    "IfcWall": DISC_ARCH, "IfcDoor": DISC_ARCH, "IfcWindow": DISC_ARCH,
    "IfcSlab": DISC_ARCH, "IfcRoof": DISC_ARCH, "IfcCovering": DISC_ARCH,
    "IfcSpace": DISC_ARCH,
    # 结构
    "IfcBeam": DISC_STRUCT, "IfcColumn": DISC_STRUCT,
    "IfcPile": DISC_STRUCT, "IfcFooting": DISC_STRUCT,
    # 机电
    "IfcPipeSegment": DISC_MEP, "IfcPipeFitting": DISC_MEP,
    "IfcDuctSegment": DISC_MEP, "IfcDuctFitting": DISC_MEP,
    "IfcCableCarrierSegment": DISC_MEP,
    "IfcCableCarrierFitting": DISC_MEP,
    "IfcCableSegment": DISC_MEP,
    "IfcFlowTerminal": DISC_MEP,
    "IfcFlowSegment": DISC_MEP, "IfcFlowFitting": DISC_MEP,
    "IfcFlowController": DISC_MEP, "IfcFlowMovingDevice": DISC_MEP,
    "IfcFlowStorageDevice": DISC_MEP, "IfcFlowTreatmentDevice": DISC_MEP,
    "IfcDistributionPort": DISC_MEP,
}

# 预留洞口（建筑/结构侧为管线预留）
OPENING_TYPE = "IfcOpeningElement"

# 文件名关键词 -> 专业（用于判断整份 IFC 的主导专业）
FILENAME_DISCIPLINE_HINTS = (
    (DISC_STRUCT, ("结构", "struct", "st-", "s-", "_s", "-s")),
    (DISC_MEP, ("机电", "mep", "水电", "暖通", "给排水", "电气",
                "piping", "plumbing", "hvac", "mech", "电气",
                "m-", "_m", "-m")),
    (DISC_ARCH, ("建筑", "arch", "architectural", "a-", "_a", "-a")),
)


def discipline_of_ifc_type(ifc_type: str) -> Optional[str]:
    """按 IFC 实体类型推断专业；未知类型返回 None。"""
    return IFC_DISCIPLINE_MAP.get(ifc_type)


def discipline_from_filename(path: str) -> Optional[str]:
    """按文件名关键词推断该 IFC 的主导专业。"""
    name = os.path.basename(path).lower()
    best = None
    for disc, hints in FILENAME_DISCIPLINE_HINTS:
        if any(h in name for h in hints):
            # 结构/机电关键词优先于建筑（建筑关键词最泛）
            if disc != DISC_ARCH:
                return disc
            best = disc
    return best


# ---------------------------------------------------------------- 问题 ----

KIND_HARD_CLASH = "coord_hard_clash"               # 硬碰撞
KIND_OPENING_MISSING = "coord_opening_missing"    # 该留洞未留
KIND_OPENING_MISMATCH = "coord_opening_mismatch"  # 预留洞口规格/位置不符
KIND_OPENING_UNUSED = "coord_opening_unused"      # 预留洞口无管线使用

COORD_KINDS = (
    KIND_HARD_CLASH,
    KIND_OPENING_MISSING,
    KIND_OPENING_MISMATCH,
    KIND_OPENING_UNUSED,
)

COORD_KIND_CN = {
    KIND_HARD_CLASH: "专业间硬碰撞",
    KIND_OPENING_MISSING: "预留洞口缺失",
    KIND_OPENING_MISMATCH: "预留洞口规格/位置不符",
    KIND_OPENING_UNUSED: "预留洞口未被使用",
}

# 问题类型默认严重程度
KIND_SEVERITY = {
    KIND_HARD_CLASH: "error",
    KIND_OPENING_MISSING: "error",
    KIND_OPENING_MISMATCH: "warning",
    KIND_OPENING_UNUSED: "warning",
}

# 问题类型默认责任专业（可被责任人配置覆盖）
KIND_OWNER_DISCIPLINE = {
    KIND_HARD_CLASH: DISC_MEP,          # 碰撞默认让机电改路由
    KIND_OPENING_MISSING: DISC_STRUCT,  # 该留洞没留，结构/建筑补洞
    KIND_OPENING_MISMATCH: DISC_STRUCT,
    KIND_OPENING_UNUSED: DISC_MEP,      # 留了洞没管，机电确认或建筑封洞
}

# 问题状态
STATUS_OPEN = "open"            # 已派单，待整改
STATUS_FIXED = "fixed"          # 责任专业已整改（自检），待复核
STATUS_VERIFIED = "verified"    # 发起专业复核通过
STATUS_REJECTED = "rejected"    # 复核驳回，退回整改
STATUS_CLEARED = "cleared"      # 重新核查时冲突已自然消失

STATUS_CN = {
    STATUS_OPEN: "待整改",
    STATUS_FIXED: "待复核",
    STATUS_VERIFIED: "复核通过",
    STATUS_REJECTED: "复核驳回",
    STATUS_CLEARED: "已消除",
}

# 未闭环状态（门禁统计口径）
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_FIXED, STATUS_REJECTED)
# 已闭环状态
CLOSED_STATUSES = (STATUS_VERIFIED, STATUS_CLEARED)
# 仍在「整改」环节、受整改时限约束的状态（派单 → 复核 这条线）
SLA_TRACKED_STATUSES = (STATUS_OPEN, STATUS_REJECTED)

# 自动升级级别（逐级升级）
ESCALATION_DISCIPLINE_LEAD = 1   # 升级到责任专业负责人
ESCALATION_PROJECT_MANAGER = 2   # 升级到项目协调 / 项目经理

ESCALATION_CN = {
    ESCALATION_DISCIPLINE_LEAD: "已升级（责任专业负责人）",
    ESCALATION_PROJECT_MANAGER: "已升级（项目协调/项目经理）",
}


def parse_dt(value: str) -> Optional[datetime]:
    """解析台账中 ISO 格式时间；空 / 非法值返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def add_hours(value: str, hours: float) -> str:
    """在 ISO 时间上追加小时数，返回 ISO 字符串。"""
    base = parse_dt(value)
    if base is None:
        return ""
    from datetime import timedelta
    return (base + timedelta(hours=float(hours))).isoformat(timespec="seconds")


# ------------------------------------------------------------- 构件 ----

@dataclass
class CoordElement:
    """参与协同核查的构件（轻量几何）。"""

    global_id: str
    ifc_type: str
    name: str
    discipline: str
    unit: str = ""                 # 所属单体（文件名）
    file_path: str = ""
    storey: str = ""
    object_type: str = ""

    # 世界坐标包围盒（米）
    bounds: tuple[float, float, float, float, float, float] = (0.0,) * 6
    cx: float = 0.0
    cy: float = 0.0
    cz: float = 0.0
    length: float = 0.0            # 管线/梁类构件的长度（包围盒长轴）
    axis_kind: str = "none"        # x / y / z / none：长轴方向
    section: tuple[float, float] = (0.0, 0.0)  # 垂直长轴截面尺寸（m）

    # 预留洞口专用
    is_opening: bool = False
    host_id: Optional[str] = None       # 洞口所属墙/板的 GlobalId
    host_type: str = ""
    host_discipline: str = ""

    raw: object = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.ifc_type}:{self.global_id}"

    @property
    def label(self) -> str:
        cn = self.name or self.ifc_type
        return f"{DISC_CN.get(self.discipline, self.discipline)}·{cn}"

    def to_dict(self) -> dict:
        # 手工序列化：raw 持有 IFC 实体（SwigPyObject），不能用 asdict 深拷贝
        return {
            "global_id": self.global_id,
            "ifc_type": self.ifc_type,
            "name": self.name,
            "discipline": self.discipline,
            "unit": self.unit,
            "file_path": self.file_path,
            "storey": self.storey,
            "object_type": self.object_type,
            "bounds": list(self.bounds),
            "cx": self.cx, "cy": self.cy, "cz": self.cz,
            "length": self.length,
            "axis_kind": self.axis_kind,
            "section": list(self.section),
            "is_opening": self.is_opening,
            "host_id": self.host_id,
            "host_type": self.host_type,
            "host_discipline": self.host_discipline,
        }


# ------------------------------------------------------------- 问题 ----

@dataclass
class CoordIssue:
    """一条多专业协同问题（同时是台账中的一条工单）。"""

    issue_id: str = ""
    fingerprint: str = ""          # 跨批次合单指纹
    kind: str = ""
    severity: str = "error"
    title: str = ""
    detail: str = ""

    # 参与构件：[(global_id, ifc_type, discipline, unit, name)]
    elements: list[dict] = field(default_factory=list)
    # 涉及专业（去重）
    disciplines: list[str] = field(default_factory=list)
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    storey: str = ""

    # 量化指标
    measure: float = 0.0           # 碰撞体积(m³) / 洞口尺寸偏差(mm)
    measure_label: str = ""

    # 责任分派
    owner_discipline: str = ""
    owner: str = ""

    # 工单流转
    status: str = STATUS_OPEN
    created_batch: str = ""
    created_at: str = ""
    updated_at: str = ""
    fixed_by: str = ""
    fixed_note: str = ""
    fixed_at: str = ""
    verified_by: str = ""
    verified_at: str = ""
    review_note: str = ""
    history: list[dict] = field(default_factory=list)

    # 整改时限（派单 → 复核 这条线）
    sla_hours: float = 0.0          # 本工单适用的整改时限（小时，0=未设置）
    due_at: str = ""                # 当前整改环节的截止时间（ISO；驳回/重开时重置）
    escalated: bool = False         # 是否已自动升级
    escalation_level: int = 0       # 升级级别（0=未升级，1=专业负责人，2=项目协调）
    escalated_at: str = ""          # 最近一次升级时间

    # 重新核查时标记本批是否仍检测到（不持久化为 True 的瞬态字段）
    present_in_scan: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def closed(self) -> bool:
        return self.status in CLOSED_STATUSES

    @property
    def sla_tracked(self) -> bool:
        """是否仍处于受整改时限约束的环节（待整改 / 驳回）。"""
        return self.status in SLA_TRACKED_STATUSES

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        """是否已超过整改时限（仅待整改 / 驳回状态计算）。"""
        if not self.sla_tracked or not self.due_at:
            return False
        due = parse_dt(self.due_at)
        if due is None:
            return False
        return (now or datetime.now()) > due

    def sla_remaining_hours(self, now: Optional[datetime] = None
                            ) -> Optional[float]:
        """距整改时限剩余小时数（负数=已超期；不适用时限返回 None）。"""
        if not self.sla_tracked or not self.due_at:
            return None
        due = parse_dt(self.due_at)
        if due is None:
            return None
        return round((due - (now or datetime.now())).total_seconds() / 3600.0, 1)

    def sla_status_cn(self, now: Optional[datetime] = None) -> str:
        """面向报告的时限状态中文描述。"""
        if self.status == STATUS_FIXED:
            return "待复核"
        if self.closed:
            return "已闭环"
        rem = self.sla_remaining_hours(now)
        if rem is None:
            return "未设时限"
        if rem < 0:
            return f"超期 {-rem:g}h"
        return f"剩余 {rem:g}h"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CoordIssue":
        from dataclasses import fields
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ------------------------------------------------------------- 台账 ----

def make_fingerprint(kind: str, element_ids: list[str],
                     extra: str = "") -> str:
    """由问题类型 + 排序后的构件 ID 生成稳定指纹（跨批次合单用）。"""
    payload = kind + "|" + ",".join(sorted(element_ids))
    if extra:
        payload += "|" + extra
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class CoordinationLedger:
    """项目级协同问题台账（跨批次持久化）。"""

    project: str
    issues: dict[str, CoordIssue] = field(default_factory=dict)  # fingerprint -> issue
    updated_at: str = ""

    # ---- 持久化 ----
    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        doc = {
            "schema_version": 2,
            "project": self.project,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "issues": [i.to_dict() for i in
                       sorted(self.issues.values(), key=lambda x: x.issue_id)],
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "CoordinationLedger":
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        ledger = cls(project=doc.get("project", ""))
        ledger.updated_at = doc.get("updated_at", "")
        for d in doc.get("issues", []):
            issue = CoordIssue.from_dict(d)
            if issue.fingerprint:
                ledger.issues[issue.fingerprint] = issue
        return ledger

    @classmethod
    def load_or_new(cls, path: str, project: str) -> "CoordinationLedger":
        if os.path.exists(path):
            return cls.load(path)
        return cls(project=project)

    def get(self, fingerprint: str) -> Optional[CoordIssue]:
        return self.issues.get(fingerprint)


# ------------------------------------------------------------- 批次结论 ----

@dataclass
class DisciplineFile:
    """协同核查纳入的一份专业模型。"""

    unit: str
    file_path: str
    discipline: str
    ok: bool = True
    error: str = ""
    n_elements: int = 0
    n_openings: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CoordinationResult:
    """一次多专业协同核查的完整结果。"""

    project: str
    batch_id: str
    label: str
    created_at: str
    files: list[DisciplineFile] = field(default_factory=list)
    elements: list[CoordElement] = field(default_factory=list)
    issues: list[CoordIssue] = field(default_factory=list)

    # 责任人配置（专业 -> 姓名）
    owners: dict[str, str] = field(default_factory=dict)
    # 各门禁规则的实际生效状态与值
    gate_rules: list[dict] = field(default_factory=list)
    gate_passed: bool = True
    # 建筑侧回写结论
    arch_writeback: dict = field(default_factory=dict)
    ledger_path: str = ""

    # 检测参数（写入报告以便追溯）
    settings: dict = field(default_factory=dict)

    # ---- 统计 ----
    def issues_of_status(self, status: str) -> list[CoordIssue]:
        return [i for i in self.issues if i.status == status]

    def count(self, kinds: Optional[list[str]] = None,
              statuses: Optional[list[str]] = None,
              owner_disciplines: Optional[list[str]] = None) -> int:
        return len(self.filter_issues(kinds, statuses, owner_disciplines))

    def filter_issues(self, kinds: Optional[list[str]] = None,
                      statuses: Optional[list[str]] = None,
                      owner_disciplines: Optional[list[str]] = None
                      ) -> list[CoordIssue]:
        out = []
        for i in self.issues:
            if kinds and i.kind not in kinds:
                continue
            if statuses and i.status not in statuses:
                continue
            if owner_disciplines and i.owner_discipline not in owner_disciplines:
                continue
            out.append(i)
        return out

    def summary(self) -> dict:
        by_kind = {k: 0 for k in COORD_KINDS}
        active_by_kind = {k: 0 for k in COORD_KINDS}
        for i in self.issues:
            by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
            if i.active:
                active_by_kind[i.kind] = active_by_kind.get(i.kind, 0) + 1
        by_status = {s: 0 for s in (*ACTIVE_STATUSES, *CLOSED_STATUSES)}
        for i in self.issues:
            by_status[i.status] = by_status.get(i.status, 0) + 1
        by_owner_disc = {d: 0 for d in DISCIPLINES}
        now = datetime.now()
        for i in self.issues:
            if i.active:
                by_owner_disc[i.owner_discipline] = \
                    by_owner_disc.get(i.owner_discipline, 0) + 1
        n_overdue = sum(1 for i in self.issues
                        if i.sla_tracked and i.is_overdue(now))
        n_escalated = sum(1 for i in self.issues
                          if i.sla_tracked and i.escalated)
        return {
            "disciplines": sorted({f.discipline for f in self.files}),
            "n_files": len(self.files),
            "n_elements": len(self.elements),
            "n_openings": sum(f.n_openings for f in self.files),
            "issues_total": len(self.issues),
            "issues_active": sum(by_status[s] for s in ACTIVE_STATUSES),
            "issues_overdue": n_overdue,
            "issues_escalated": n_escalated,
            "by_kind": by_kind,
            "active_by_kind": active_by_kind,
            "by_status": by_status,
            "active_by_owner_discipline": by_owner_disc,
            "gate_passed": self.gate_passed,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "project": self.project,
            "batch_id": self.batch_id,
            "label": self.label,
            "created_at": self.created_at,
            "files": [f.to_dict() for f in self.files],
            "elements": [e.to_dict() for e in self.elements],
            "issues": [i.to_dict() for i in self.issues],
            "owners": dict(self.owners),
            "gate_rules": list(self.gate_rules),
            "gate_passed": self.gate_passed,
            "arch_writeback": dict(self.arch_writeback),
            "ledger_path": self.ledger_path,
            "settings": dict(self.settings),
            "summary": self.summary(),
        }
