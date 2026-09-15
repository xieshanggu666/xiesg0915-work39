"""协同问题闭环数据模型（面向设计 / 结构 / 机电团队）。

在单专业核查（:mod:`ifc_audit.model`）与多专业碰撞协同
（:mod:`ifc_audit.coordination_model`）之上，本模块把**多来源问题**统一成
可分派、可流转、可追溯的协同工单 :class:`CollabTicket`：

* 来源 ``audit`` —— 批量审查的单体问题（未闭合墙 / 重复构件 / 房间净面积 /
  门窗规格等，来自 :mod:`ifc_audit.checks`）；
* 来源 ``coord`` —— 多专业协同问题（硬碰撞 / 预留洞口缺失·不符·闲置）；
* 来源 ``rule``  —— 企业规则包 / 规则校验产生的问题（门禁阻断、口径校验等）；
* 来源 ``manual``—— 会审 / 现场人工登记的问题。

工单状态沿用「派单 → 整改 → 复核 → 闭环」主线，另支持**人工关闭**与
**驳回重整改**；跨批次按稳定指纹合单，重新核查消失自动闭环、闭环后再现
自动重开（回归）。:class:`CollabLedger` 是项目级持久化台账，同时保存：

* **角色与权限**用的项目人员名册（:class:`RosterUser`）；
* **通知中心**消息（:class:`Notification`），按角色 / 专业 / 责任人精准投递。

长度单位与既有模型一致（米）；时间一律 ISO 字符串。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from .coordination_model import (
    DISCIPLINES, DISC_CN,
    STATUS_OPEN, STATUS_FIXED, STATUS_VERIFIED, STATUS_REJECTED,
    STATUS_CLEARED,
    ACTIVE_STATUSES, CLOSED_STATUSES, SLA_TRACKED_STATUSES,
    parse_dt,
)

# re-export，便于其它模块只从本模块取状态常量
__all__ = [
    "DISCIPLINES", "DISC_CN",
    "STATUS_OPEN", "STATUS_FIXED", "STATUS_VERIFIED", "STATUS_REJECTED",
    "STATUS_CLEARED", "STATUS_CLOSED",
    "ACTIVE_STATUSES", "CLOSED_STATUSES", "SLA_TRACKED_STATUSES",
    "SOURCE_AUDIT", "SOURCE_COORD", "SOURCE_RULE", "SOURCE_MANUAL",
    "SOURCES", "SOURCE_CN",
    "SEVERITIES",
    "ROLE_COORDINATOR", "ROLE_DESIGN_LEAD", "ROLE_STRUCT_LEAD",
    "ROLE_MEP_LEAD", "ROLE_RESPONSIBLE", "ROLE_REVIEWER", "ROLE_VIEWER",
    "ROLES", "ROLE_CN", "ROLE_RANK",
    "EVENT_CREATED", "EVENT_ASSIGNED", "EVENT_DUE_SOON", "EVENT_OVERDUE",
    "EVENT_ESCALATED", "EVENT_FIXED", "EVENT_REJECTED", "EVENT_VERIFIED",
    "EVENT_CLOSED", "EVENT_REOPENED", "EVENT_MENTION", "EVENT_GATE_BLOCKED",
    "EVENT_SCAN_INCOMPLETE",
    "EVENT_CN",
    "CollabTicket", "ModelRef", "RosterUser", "Notification",
    "ScanRun", "CollabLedger", "make_ticket_fingerprint",
]

# ------------------------------------------------------------- 问题来源 ----

SOURCE_AUDIT = "audit"
SOURCE_COORD = "coord"
SOURCE_RULE = "rule"
SOURCE_MANUAL = "manual"

SOURCES = (SOURCE_AUDIT, SOURCE_COORD, SOURCE_RULE, SOURCE_MANUAL)

SOURCE_CN = {
    SOURCE_AUDIT: "批量审查",
    SOURCE_COORD: "多专业协同",
    SOURCE_RULE: "规则校验",
    SOURCE_MANUAL: "人工登记",
}

SEVERITIES = ("error", "warning", "info")
SEVERITY_CN = {"error": "错误", "warning": "警告", "info": "提示"}

# 人工关闭：区别于“复核通过”，用于会审销项 / 设计豁免 / 不做处理等场景。
STATUS_CLOSED = "closed"

# 闭环状态在协同五类基础上扩展人工关闭
COLLAB_CLOSED_STATUSES = (STATUS_VERIFIED, STATUS_CLEARED, STATUS_CLOSED)
COLLAB_ACTIVE_STATUSES = (STATUS_OPEN, STATUS_FIXED, STATUS_REJECTED)

STATUS_CN = {
    STATUS_OPEN: "待整改",
    STATUS_FIXED: "待复核",
    STATUS_VERIFIED: "复核通过",
    STATUS_REJECTED: "复核驳回",
    STATUS_CLEARED: "已消除",
    STATUS_CLOSED: "已关闭",
}

# ------------------------------------------------------------- 角色权限 ----

ROLE_COORDINATOR = "coordinator"      # 项目协调 / BIM 负责人：全量操作
ROLE_DESIGN_LEAD = "design_lead"      # 设计（建筑）专业负责人
ROLE_STRUCT_LEAD = "struct_lead"      # 结构专业负责人
ROLE_MEP_LEAD = "mep_lead"            # 机电专业负责人
ROLE_RESPONSIBLE = "responsible"      # 具体责任人：整改本人名下工单
ROLE_REVIEWER = "reviewer"            # 复核人：复核 / 驳回
ROLE_VIEWER = "viewer"                # 只读：查看与订阅通知

ROLES = (
    ROLE_COORDINATOR,
    ROLE_DESIGN_LEAD,
    ROLE_STRUCT_LEAD,
    ROLE_MEP_LEAD,
    ROLE_RESPONSIBLE,
    ROLE_REVIEWER,
    ROLE_VIEWER,
)

ROLE_CN = {
    ROLE_COORDINATOR: "项目协调",
    ROLE_DESIGN_LEAD: "设计负责人",
    ROLE_STRUCT_LEAD: "结构负责人",
    ROLE_MEP_LEAD: "机电负责人",
    ROLE_RESPONSIBLE: "责任人",
    ROLE_REVIEWER: "复核人",
    ROLE_VIEWER: "只读成员",
}

# 角色级别（协调人最高），用于“至少某级别”类判定与展示
ROLE_RANK = {
    ROLE_VIEWER: 0,
    ROLE_RESPONSIBLE: 1,
    ROLE_REVIEWER: 2,
    ROLE_DESIGN_LEAD: 3,
    ROLE_STRUCT_LEAD: 3,
    ROLE_MEP_LEAD: 3,
    ROLE_COORDINATOR: 9,
}

# 专业负责人角色 -> 所辖专业
LEAD_DISCIPLINE = {
    ROLE_DESIGN_LEAD: "arch",
    ROLE_STRUCT_LEAD: "struct",
    ROLE_MEP_LEAD: "mep",
}
DISCIPLINE_LEAD = {d: r for r, d in LEAD_DISCIPLINE.items()}

# ------------------------------------------------------------- 通知事件 ----

EVENT_CREATED = "created"
EVENT_ASSIGNED = "assigned"
EVENT_DUE_SOON = "due_soon"
EVENT_OVERDUE = "overdue"
EVENT_ESCALATED = "escalated"
EVENT_FIXED = "fixed"
EVENT_REJECTED = "rejected"
EVENT_VERIFIED = "verified"
EVENT_CLOSED = "closed"
EVENT_REOPENED = "reopened"
EVENT_MENTION = "mention"
EVENT_GATE_BLOCKED = "gate_blocked"
# 局部复查 / 扫描失败：存在未覆盖工单，自动销项范围受限
EVENT_SCAN_INCOMPLETE = "scan_incomplete"

EVENT_CN = {
    EVENT_CREATED: "新问题派单",
    EVENT_ASSIGNED: "改派",
    EVENT_DUE_SOON: "整改临期",
    EVENT_OVERDUE: "整改超期",
    EVENT_ESCALATED: "自动升级",
    EVENT_FIXED: "提交整改待复核",
    EVENT_REJECTED: "复核驳回",
    EVENT_VERIFIED: "复核通过",
    EVENT_CLOSED: "工单关闭",
    EVENT_REOPENED: "回归重开",
    EVENT_MENTION: "点名提醒",
    EVENT_GATE_BLOCKED: "门禁阻断",
    EVENT_SCAN_INCOMPLETE: "复查覆盖不完整",
}

# 事件默认严重程度（通知中心着色用）
EVENT_LEVEL = {
    EVENT_CREATED: "info",
    EVENT_ASSIGNED: "info",
    EVENT_DUE_SOON: "warning",
    EVENT_OVERDUE: "error",
    EVENT_ESCALATED: "error",
    EVENT_FIXED: "info",
    EVENT_REJECTED: "warning",
    EVENT_VERIFIED: "ok",
    EVENT_CLOSED: "ok",
    EVENT_REOPENED: "error",
    EVENT_MENTION: "info",
    EVENT_GATE_BLOCKED: "error",
    EVENT_SCAN_INCOMPLETE: "warning",
}


# ------------------------------------------------------------- 构件引用 ----

@dataclass
class ModelRef:
    """一条跨模型构件定位引用（工单可含多条，覆盖建筑/结构/机电多个模型）。"""

    global_id: str
    ifc_type: str = ""
    name: str = ""
    discipline: str = ""        # arch / struct / mep
    unit: str = ""              # 单体名
    file_path: str = ""
    storey: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelRef":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})

    @property
    def label(self) -> str:
        disc = DISC_CN.get(self.discipline, self.discipline)
        return (f"[{disc or '?'}]{self.unit or ''}/"
                f"{self.name or self.global_id[:8]}")


# ------------------------------------------------------------- 工单 ----

@dataclass
class CollabTicket:
    """一条跨专业协同问题（同时是台账中的工单）。"""

    ticket_id: str = ""             # COLL-xxxx
    fingerprint: str = ""           # 跨批次合单指纹
    source: str = SOURCE_MANUAL
    kind: str = ""                  # 来源内问题类型标识
    severity: str = "warning"
    title: str = ""
    detail: str = ""

    # 跨模型定位：涉及构件（可跨多份 IFC / 多个专业）
    refs: list[ModelRef] = field(default_factory=list)
    disciplines: list[str] = field(default_factory=list)
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    storey: str = ""
    unit: str = ""                  # 主单体（按问题归属，跨单体可空）

    measure: float = 0.0
    measure_label: str = ""

    # 责任分派
    owner_discipline: str = ""
    owner: str = ""

    # 工单流转
    status: str = STATUS_OPEN
    created_batch: str = ""
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""
    fixed_by: str = ""
    fixed_note: str = ""
    fixed_at: str = ""
    verified_by: str = ""
    verified_at: str = ""
    closed_by: str = ""
    closed_reason: str = ""
    review_note: str = ""
    history: list[dict] = field(default_factory=list)

    # 整改时限（派单 → 复核 这条线，与协同台账一致）
    sla_hours: float = 0.0
    due_at: str = ""
    escalated: bool = False
    escalation_level: int = 0
    escalated_at: str = ""

    # 整改回写：责任专业填写的整改说明 / 整改构件 / 复核材料
    resolution: str = ""
    resolution_refs: list[ModelRef] = field(default_factory=list)
    writeback_at: str = ""

    # 与来源系统的关联（如协同 COORD-0001 / 单体问题 GAP-003）
    source_ref: str = ""

    # 重新核查时本批是否仍检出（瞬态，不持久化为 True）
    present_in_scan: bool = False

    # 局部复查留痕：最近一次扫描的批次 / 时刻 / 模型版本（不区分是否检出）
    last_scan_batch: str = ""
    last_scan_at: str = ""
    # 最近一次确认覆盖该工单的扫描所用模型版本（按 "单体|文件" -> 版本）
    last_scan_versions: dict[str, str] = field(default_factory=dict)
    # 最近一次覆盖结果：covered=成功覆盖；out_of_scope=不在复查范围；
    # scan_failed=范围内但模型扫描失败；manual/rule 类不参与自动覆盖
    last_cover_result: str = ""
    last_cover_batch: str = ""
    last_cover_at: str = ""
    # 自动销项时所依据的模型版本（已消除 / 自动复核通过留痕）
    cleared_versions: dict[str, str] = field(default_factory=dict)

    # ---- 状态判定 ----
    @property
    def active(self) -> bool:
        return self.status in COLLAB_ACTIVE_STATUSES

    @property
    def closed(self) -> bool:
        return self.status in COLLAB_CLOSED_STATUSES

    @property
    def sla_tracked(self) -> bool:
        return self.status in SLA_TRACKED_STATUSES

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        if not self.sla_tracked or not self.due_at:
            return False
        due = parse_dt(self.due_at)
        if due is None:
            return False
        return (now or datetime.now()) > due

    def sla_remaining_hours(self, now: Optional[datetime] = None
                            ) -> Optional[float]:
        if not self.sla_tracked or not self.due_at:
            return None
        due = parse_dt(self.due_at)
        if due is None:
            return None
        return round((due - (now or datetime.now())).total_seconds() / 3600.0, 1)

    def sla_status_cn(self, now: Optional[datetime] = None) -> str:
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

    # ---- 持久化 ----
    def to_dict(self) -> dict:
        d = asdict(self)
        d["refs"] = [r.to_dict() if isinstance(r, ModelRef) else r
                     for r in self.refs]
        d["resolution_refs"] = [
            r.to_dict() if isinstance(r, ModelRef) else r
            for r in self.resolution_refs]
        d["location"] = list(self.location)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CollabTicket":
        from dataclasses import fields
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        kw["refs"] = [ModelRef.from_dict(r) for r in kw.get("refs", [])]
        kw["resolution_refs"] = [ModelRef.from_dict(r)
                                 for r in kw.get("resolution_refs", [])]
        kw["location"] = tuple(kw.get("location", (0.0, 0.0, 0.0)))
        return cls(**kw)


def make_ticket_fingerprint(source: str, kind: str,
                            refs: list | str,
                            unit: str = "", extra: str = "") -> str:
    """生成跨批次稳定指纹。

    Args:
        source: 问题来源（audit/coord/rule/manual）。
        kind: 来源内问题类型。
        refs: 构件引用列表（取排序后的 GlobalId），或直接给定的字符串。
        unit: 单体名（audit 类问题在不同单体中复用同一构件类型时消歧）。
        extra: 追加的区分要素（如门窗规格问题的尺寸串）。
    """
    if isinstance(refs, str):
        ref_part = refs
    else:
        ids = []
        for r in refs:
            gid = r.get("global_id", "") if isinstance(r, dict) \
                else getattr(r, "global_id", "")
            if gid:
                ids.append(gid)
        ref_part = ",".join(sorted(ids))
    payload = "|".join([source, kind, unit, ref_part, extra])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:14]


# ------------------------------------------------------------- 人员名册 ----

@dataclass
class RosterUser:
    """项目协同名册中的一名成员（角色 + 所属专业，用于权限与通知投递）。"""

    name: str
    role: str = ROLE_VIEWER
    discipline: str = ""        # 责任人 / 专业负责人所属专业 arch/struct/mep
    active: bool = True
    notify: bool = True        # 是否接收通知（可退订）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RosterUser":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})


# ------------------------------------------------------------- 通知 ----

@dataclass
class Notification:
    """通知中心的一条消息。"""

    notif_id: str
    ticket_id: str
    event: str
    recipient: str = ""         # 接收人姓名（空=广播给相关角色）
    recipient_role: str = ""
    title: str = ""
    body: str = ""
    level: str = "info"
    created_at: str = ""
    read: bool = False
    read_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Notification":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})


# ------------------------------------------------------- 复查运行记录 ----

# 覆盖判定结果
COVER_COVERED = "covered"             # 在复查范围内且模型扫描成功
COVER_OUT_OF_SCOPE = "out_of_scope"  # 不在本次复查范围（单体 / 专业 / 核查项）
COVER_SCAN_FAILED = "scan_failed"    # 在范围内但对应模型扫描失败
COVER_PRESENT = "present"            # 本次扫描仍检出（未消失，无需销项）

COVER_CN = {
    COVER_COVERED: "成功覆盖",
    COVER_OUT_OF_SCOPE: "不在复查范围",
    COVER_SCAN_FAILED: "扫描失败",
    COVER_PRESENT: "仍检出",
}


@dataclass
class ScanRun:
    """一次（局部）复查的实际扫描范围与覆盖结果留痕。"""

    batch_id: str
    at: str
    # 申请的复查范围（空=全量）
    units: list[str] = field(default_factory=list)
    disciplines: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    # 是否用户显式圈定了复查范围（区别于全量但有模型扫描失败）
    scoped: bool = False
    partial: bool = False
    # 实际扫描结果
    scanned_units: list[str] = field(default_factory=list)   # 成功扫描的单体
    failed_files: list[dict] = field(default_factory=list)  # {unit,discipline,file,error}
    model_versions: list[dict] = field(default_factory=list)  # {unit,discipline,file,version}
    # 覆盖统计（活动 audit/coord 工单）
    n_present: int = 0           # 本次仍检出
    n_covered: int = 0           # 范围覆盖且未检出（含自动销项）
    n_auto_verified: int = 0     # 待复核 -> 自动复核通过
    n_auto_cleared: int = 0      # 待整改/驳回 -> 已消除
    n_out_of_scope: int = 0      # 范围外，状态保留
    n_scan_failed: int = 0       # 范围内但扫描失败，状态保留
    uncovered_ticket_ids: list[str] = field(default_factory=list)  # 未覆盖活动工单
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScanRun":
        return cls(**{k: v for k, v in (d or {}).items()
                      if k in cls.__dataclass_fields__})

    @property
    def incomplete(self) -> bool:
        """存在未覆盖工单或扫描失败文件 -> 本次复查不完整。"""
        return bool(self.failed_files) or bool(self.uncovered_ticket_ids)


# ------------------------------------------------------------- 台账 ----

@dataclass
class CollabLedger:
    """项目级协同问题闭环台账（跨批次持久化）。"""

    project: str
    tickets: dict[str, CollabTicket] = field(default_factory=dict)  # fp -> ticket
    users: dict[str, RosterUser] = field(default_factory=dict)      # 姓名 -> 用户
    notifications: list[Notification] = field(default_factory=list)
    # 复查运行记录（最近在前；局部复查范围 / 模型版本 / 覆盖结果留痕）
    runs: list[ScanRun] = field(default_factory=list)
    updated_at: str = ""

    SCHEMA_VERSION = 2
    # 台账中保留的复查运行记录上限（超出截旧）
    MAX_RUNS = 100

    # ---- 查询 ----
    def get(self, fingerprint: str) -> Optional[CollabTicket]:
        return self.tickets.get(fingerprint)

    def find(self, ticket_id_or_fp: str) -> CollabTicket:
        if ticket_id_or_fp in self.tickets:
            return self.tickets[ticket_id_or_fp]
        for t in self.tickets.values():
            if t.ticket_id == ticket_id_or_fp:
                return t
        raise KeyError(ticket_id_or_fp)

    def find_user(self, name: str) -> Optional[RosterUser]:
        return self.users.get(name)

    def role_of(self, name: str) -> Optional[str]:
        u = self.users.get(name)
        return u.role if u else None

    def next_ticket_seq(self) -> int:
        return 1 + max(
            (int(t.ticket_id.split("-")[1]) for t in self.tickets.values()
             if t.ticket_id.startswith("COLL-")
             and t.ticket_id.split("-")[1].isdigit()),
            default=0)

    def next_notif_seq(self) -> int:
        return 1 + max(
            (int(n.notif_id.split("-")[1]) for n in self.notifications
             if n.notif_id.startswith("N-")
             and n.notif_id.split("-")[1].isdigit()),
            default=0)

    # ---- 名册 ----
    def upsert_user(self, user: RosterUser) -> RosterUser:
        self.users[user.name] = user
        return user

    def remove_user(self, name: str) -> bool:
        return self.users.pop(name, None) is not None

    def users_by_role(self, role: str) -> list[RosterUser]:
        return [u for u in self.users.values() if u.role == role and u.active]

    # ---- 通知 ----
    def add_notification(self, n: Notification) -> Notification:
        self.notifications.append(n)
        return n

    # ---- 复查运行记录 ----
    def add_run(self, run: ScanRun) -> ScanRun:
        """登记一次复查运行（最近在前，超出 :data:`MAX_RUNS` 截旧）。"""
        self.runs.insert(0, run)
        if len(self.runs) > self.MAX_RUNS:
            del self.runs[self.MAX_RUNS:]
        return run

    def last_run(self) -> Optional[ScanRun]:
        return self.runs[0] if self.runs else None

    def notifications_for(self, name: str, unread_only: bool = False
                          ) -> list[Notification]:
        out = []
        for n in self.notifications:
            if n.recipient and n.recipient != name:
                continue
            if unread_only and n.read:
                continue
            out.append(n)
        return out

    # ---- 持久化 ----
    def to_dict(self) -> dict:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "project": self.project,
            "updated_at": self.updated_at,
            "tickets": [t.to_dict() for t in
                        sorted(self.tickets.values(), key=lambda x: x.ticket_id)],
            "users": [u.to_dict() for u in sorted(self.users.values(),
                                                  key=lambda x: x.name)],
            "notifications": [n.to_dict() for n in self.notifications],
            "scan_runs": [r.to_dict() for r in self.runs],
        }

    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.updated_at = datetime.now().isoformat(timespec="seconds")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "CollabLedger":
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        ledger = cls(project=doc.get("project", ""))
        ledger.updated_at = doc.get("updated_at", "")
        for d in doc.get("tickets", []):
            t = CollabTicket.from_dict(d)
            if t.fingerprint:
                ledger.tickets[t.fingerprint] = t
        for d in doc.get("users", []):
            u = RosterUser.from_dict(d)
            if u.name:
                ledger.users[u.name] = u
        for d in doc.get("notifications", []):
            ledger.notifications.append(Notification.from_dict(d))
        # schema v2 起记录复查运行；历史台账（v1 / 无该键）加载为空，照常工作
        for d in doc.get("scan_runs", []):
            run = ScanRun.from_dict(d)
            if run.batch_id:
                ledger.runs.append(run)
        return ledger

    @classmethod
    def load_or_new(cls, path: str, project: str) -> "CollabLedger":
        if os.path.exists(path):
            return cls.load(path)
        return cls(project=project)
