"""协同问题闭环引擎：分派 / 流转 / 整改回写 / 通知 / 权限 / 门禁。

本模块在 :mod:`ifc_audit.collab_model` 之上提供面向设计、结构、机电团队的
完整闭环能力：

1. **多来源纳管** :func:`ingest_batch` / :func:`ingest_coordination`：
   把批量审查（单模型问题）、多专业协同（碰撞 / 洞口）、规则校验问题统一
   合入项目闭环台账，跨批次按指纹合单、自动闭环 / 回归重开，人工登记的问题
   不被自动扫描消除（:func:`open_manual_ticket`）；
2. **问题分派** :func:`assign_ticket`：按专业 / 责任人派单、改派，
   责任人缺失时回落到专业负责人；
3. **状态流转**：``待整改 → 待复核 → 复核通过 / 驳回 → 已关闭``，
   重新核查消失自动消除（:func:`sweep_absent_tickets`）；
3.1. **局部复查** :class:`ScanScope` / :func:`ingest_batch`：
   支持按**单体 / 专业 / 核查项**圈定复查范围，记录模型版本（文件名 + 大小 +
   内容指纹）与实际扫描范围（:class:`~ifc_audit.collab_model.ScanRun`）；
   **仅对成功覆盖的工单自动销项**，不在范围内（out_of_scope）或扫描失败
   （scan_failed）的工单保留原状态，门禁与通知随覆盖结果联动；
4. **整改回写** :func:`writeback_fix`：责任专业回填整改说明与整改后构件，
   随 :func:`build_writeback` 回写到单体 / 批次结论；
5. **权限** :func:`require_permission`：项目协调 / 三专业负责人 / 责任人 /
   复核人 / 只读 六类角色的 RBAC 校验（见 :data:`ACTION_PERMISSIONS`）；
6. **通知** :func:`notify`：派单、临期、超期、升级、整改、驳回、闭环、
   门禁阻断等事件按角色 / 专业 / 责任人精准投递到台账通知中心；
7. **报告汇总 / 门禁** :func:`collab_summary` / :func:`evaluate_collab_gate`：
   按来源 / 专业 / 状态 / 楼层汇总闭环情况，并与批次放行联动。

所有写操作都要求传 ``actor``（操作人姓名），并在工单 ``history`` 与通知中
留痕；``actor`` 为系统账号（:data:`SYSTEM_ACTOR`）时跳过权限校验。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Callable

from .batch import _slug
from .coordination_model import (
    DISCIPLINES, DISC_CN, COORD_KIND_CN, KIND_OWNER_DISCIPLINE,
    STATUS_OPEN, STATUS_FIXED, STATUS_VERIFIED, STATUS_REJECTED,
    STATUS_CLEARED, ESCALATION_DISCIPLINE_LEAD, ESCALATION_PROJECT_MANAGER,
    ESCALATION_CN,
)
from .collab_model import (
    CollabTicket, ModelRef, RosterUser, Notification, CollabLedger,
    ScanRun, make_ticket_fingerprint, make_audit_fingerprint,
    make_legacy_audit_fingerprint,
    SOURCE_AUDIT, SOURCE_COORD, SOURCE_RULE, SOURCE_MANUAL, SOURCES, SOURCE_CN,
    SEVERITIES,
    STATUS_CLOSED, STATUS_CN,
    COLLAB_ACTIVE_STATUSES, COLLAB_CLOSED_STATUSES,
    ROLE_COORDINATOR, ROLE_DESIGN_LEAD, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD,
    ROLE_RESPONSIBLE, ROLE_REVIEWER, ROLE_VIEWER,
    ROLES, ROLE_CN, LEAD_DISCIPLINE, DISCIPLINE_LEAD,
    EVENT_CREATED, EVENT_ASSIGNED, EVENT_DUE_SOON, EVENT_OVERDUE,
    EVENT_ESCALATED, EVENT_FIXED, EVENT_REJECTED, EVENT_VERIFIED,
    EVENT_CLOSED, EVENT_REOPENED, EVENT_GATE_BLOCKED, EVENT_SCAN_INCOMPLETE,
    EVENT_CN, EVENT_LEVEL,
    COVER_COVERED, COVER_OUT_OF_SCOPE, COVER_SCAN_FAILED, COVER_PRESENT,
    COVER_CN,
)

# 系统账号：批量扫描 / 时限扫描等自动动作的操作者，绕过 RBAC
SYSTEM_ACTOR = "系统"

# 单模型审查问题类型 -> 默认责任专业 / 中文名
# 建筑侧单体问题默认派设计（建筑）专业；多专业问题沿用协同台账的责任归属。
AUDIT_KIND_OWNER = {
    "wall_free_end": "arch",
    "wall_end_gap": "arch",
    "room_enclosure_gap": "arch",
    "room_no_geometry": "arch",
    "duplicate_element": "arch",
    "opening_size_anomaly": "arch",
    "opening_unassigned": "arch",
}

AUDIT_KIND_CN = {
    "wall_free_end": "墙自由端",
    "wall_end_gap": "墙段缺口",
    "room_enclosure_gap": "房间围护缺口",
    "room_no_geometry": "房间无几何未检",
    "duplicate_element": "重复构件",
    "opening_size_anomaly": "门窗尺寸异常",
    "opening_unassigned": "门窗未归属",
}

# audit 工单可选核查项标识（--recheck-kind 校验 / 中文名）
AUDIT_KINDS = tuple(AUDIT_KIND_OWNER)

AUDIT_KIND_SCOPE_CN = {
    "wall": "墙体闭合",
    "duplicate": "重复构件",
    "room": "房间净面积",
    "opening": "门窗规格",
}

# 核查项别名（CLI 友好）-> 展开为具体 kind
KIND_ALIASES = {
    "wall": ("wall_free_end", "wall_end_gap", "room_enclosure_gap",
             "room_no_geometry"),
    "duplicate": ("duplicate_element",),
    "room": ("room_enclosure_gap", "room_no_geometry"),
    "opening": ("opening_size_anomaly", "opening_unassigned"),
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============================================================ 局部复查 ----

@dataclass(frozen=True)
class ScanScope:
    """一次复查的申请范围（空集合表示该维度不限）。

    * ``units``：单体名集合（= 模型文件名去后缀，与 ``UnitResult.name`` 一致）；
    * ``disciplines``：专业集合（arch/struct/mep）；
    * ``kinds``：核查项集合（audit 的 kind，如 ``wall_free_end``；
      coord 工单按其涉及专业匹配，不按 kind 过滤）。
    """

    units: frozenset[str] = frozenset()
    disciplines: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()

    @classmethod
    def make(cls, units=(), disciplines=(), kinds=()) -> "ScanScope":
        kind_set: set[str] = set()
        for k in kinds or ():
            kind_set.update(KIND_ALIASES.get(k, (k,)))
        return cls(frozenset(units or ()), frozenset(disciplines or ()),
                   frozenset(kind_set))

    @property
    def partial(self) -> bool:
        return bool(self.units or self.disciplines or self.kinds)

    def describe(self) -> str:
        parts = []
        if self.units:
            parts.append("单体=" + "/".join(sorted(self.units)))
        if self.disciplines:
            parts.append("专业=" + "/".join(
                DISC_CN.get(d, d) for d in sorted(self.disciplines)))
        if self.kinds:
            names = [AUDIT_KIND_CN.get(k, k) for k in sorted(self.kinds)]
            parts.append("核查项=" + "/".join(names))
        return "；".join(parts) or "全量复查"

    def covers_kind(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds

    def covers_disciplines(self, discs) -> bool:
        return (not self.disciplines
                or bool(set(discs or []) & set(self.disciplines)))


def model_file_version(path: str) -> str:
    """计算模型文件版本标识：``大小-内容指纹``。

    常规模型（≤256MB）整文件流式 SHA-1，任何字节变化都会换版；更大的模型
    退化为「头/尾 1MB + 每 16MB 抽样 32KB」抽样哈希。文件不可读时退化为
    大小 / mtime。
    """
    try:
        size = os.path.getsize(path)
        h = hashlib.sha1()
        h.update(str(size).encode("utf-8"))
        head_len = 1 << 20
        full_hash_limit = 1 << 28      # 256MB 以内整文件哈希
        with open(path, "rb") as f:
            if size <= full_hash_limit:
                for block in iter(lambda: f.read(1 << 22), b""):
                    h.update(block)
            else:
                h.update(f.read(head_len))
                step = 1 << 24          # 16MB
                chunk = 1 << 15         # 32KB
                tail_start = size - head_len
                pos = step
                while pos < tail_start:
                    f.seek(pos)
                    h.update(f.read(chunk))
                    mid = pos + step // 2
                    if mid < tail_start:
                        f.seek(mid)
                        h.update(f.read(chunk))
                    pos += step
                f.seek(tail_start)
                h.update(f.read(head_len))
        return f"{size}-{h.hexdigest()[:12]}"
    except OSError:
        try:
            return f"mtime-{int(os.path.getmtime(path))}"
        except OSError:
            return "unknown"


@dataclass
class _ScanIndices:
    """一次扫描按 (稳定单体, 专业) 汇总的成功 / 失败索引，供覆盖判定。"""

    ok_pairs: set[tuple[str, str]] = field(default_factory=set)
    failed_pairs: set[tuple[str, str]] = field(default_factory=set)
    ok_units: set[str] = field(default_factory=set)
    failed_units: set[str] = field(default_factory=set)
    # 批次内显示名 -> 稳定单体标识（历史工单只有显示名时换算用）
    name_to_key: dict[str, str] = field(default_factory=dict)
    pair_versions: dict[tuple[str, str], str] = field(default_factory=dict)
    pair_files: dict[tuple[str, str], str] = field(default_factory=dict)
    files_info: list[dict] = field(default_factory=list)
    failed_files: list[dict] = field(default_factory=list)
    scanned_units: set[str] = field(default_factory=set)


def _guess_discipline_from_name(name: str) -> str:
    """按文件名 / 单体名关键词推断专业（协同未运行时的回落）。"""
    low = (name or "").lower()
    if any(k in low for k in ("结构", "struct")):
        return "struct"
    if any(k in low for k in ("机电", "mep", "暖通", "给排水", "电气",
                             "hvac", "piping", "水暖")):
        return "mep"
    if any(k in low for k in ("建筑", "arch")):
        return "arch"
    return "arch"


def _unit_key_of_unit(unit) -> str:
    """从批次单体结果取稳定单体标识（缺失时由文件名推导）。"""
    uk = getattr(unit, "unit_key", "") or ""
    if uk:
        return uk
    fp = getattr(unit, "file_path", "") or ""
    return os.path.splitext(os.path.basename(fp))[0] if fp else unit.name


def _ticket_unit_keys(t: CollabTicket) -> list[str]:
    """工单涉及的稳定单体标识集合（含历史显示名回落）。"""
    keys: set[str] = set()
    if getattr(t, "unit_key", ""):
        keys.add(t.unit_key)
    for r in t.refs:
        uk = getattr(r, "unit_key", "") or ""
        if uk:
            keys.add(uk)
    # 旧台账没有 unit_key 字段：退回显示名（与当时扫描索引的键一致）
    if not keys:
        if t.unit:
            keys.add(t.unit)
        for r in t.refs:
            if r.unit:
                keys.add(r.unit)
    return sorted(keys)


def _build_scan_indices(batch, coord, *, hash_files: bool) -> _ScanIndices:
    """从批次结果构建实际扫描成功 / 失败索引（audit 单体 + coord 专业模型）。

    配对主键统一为 **(稳定单体标识 unit_key, 专业)**；同时登记显示名到稳定
    标识的映射，供历史工单（无 unit_key、只有消歧显示名）回落匹配。
    """
    idx = _ScanIndices()

    def _add_pair(uk, disc, display, fp, ok, error=""):
        pair = (uk, disc or "")
        if display:
            idx.name_to_key[display] = uk
        if ok:
            idx.ok_pairs.add(pair)
            idx.ok_units.add(uk)
            if uk:
                idx.scanned_units.add(uk)
            ver = model_file_version(fp) if hash_files and fp else ""
            if ver:
                idx.pair_versions[pair] = ver
                idx.pair_files[pair] = fp
                idx.files_info.append({"unit": uk, "display": display,
                                       "discipline": disc or "",
                                       "file": fp, "version": ver})
        else:
            idx.failed_pairs.add(pair)
            if uk:
                idx.failed_units.add(uk)
            idx.ok_pairs.discard(pair)
            if not any(f["unit"] == uk and f["discipline"] == (disc or "")
                       for f in idx.failed_files):
                idx.failed_files.append({
                    "unit": uk, "display": display,
                    "discipline": disc or "", "file": fp,
                    "error": error or "模型核查失败"})

    # 协同专业模型
    if coord is not None:
        for f in getattr(coord, "files", []):
            uk = getattr(f, "unit_key", "") or os.path.splitext(
                os.path.basename(getattr(f, "file_path", "")))[0]
            _add_pair(uk, getattr(f, "discipline", ""),
                      getattr(f, "unit", ""), getattr(f, "file_path", ""),
                      getattr(f, "ok", True), getattr(f, "error", ""))

    # 批量单体结果
    for unit in batch.units:
        uk = _unit_key_of_unit(unit)
        fp = getattr(unit, "file_path", "")
        # 专业优先取协同侧；否则按文件名推断
        disc = next((d for (u2, d) in idx.ok_pairs | idx.failed_pairs
                     if u2 == uk and d), "") or \
            _guess_discipline_from_name(f"{unit.name} {fp}")
        ok = getattr(unit, "model", None) is not None
        _add_pair(uk, disc, unit.name, fp, ok,
                  getattr(unit, "error", "模型核查失败"))
    return idx


def _resolve_ticket_units(t: CollabTicket, idx: _ScanIndices) -> list[str]:
    """工单稳定单体标识；旧工单只有显示名时用本批 name->key 映射换算。"""
    keys = set(_ticket_unit_keys(t))
    resolved: set[str] = set()
    for k in keys:
        resolved.add(idx.name_to_key.get(k, k))
    return sorted(resolved)


def _ticket_scan_pairs(t: CollabTicket, idx: Optional[_ScanIndices] = None
                       ) -> list[tuple[str, str]]:
    """工单依赖的 (稳定单体, 专业) 配对（覆盖判定用）。"""
    pairs: set[tuple[str, str]] = set()
    if t.source == SOURCE_AUDIT:
        units = set(_ticket_unit_keys(t))
        if idx is not None:
            units = set(_resolve_ticket_units(t, idx))
        disc = t.owner_discipline or "arch"
        pairs.update((u, disc) for u in units)
    else:
        for r in t.refs:
            uk = getattr(r, "unit_key", "") or r.unit
            if uk:
                pairs.add((uk, r.discipline or t.owner_discipline or ""))
        if not pairs and (t.unit_key or t.unit):
            pairs.add((t.unit_key or t.unit, t.owner_discipline))
    return sorted(pairs)


def ticket_cover_result(t: CollabTicket, scope: ScanScope,
                        idx: _ScanIndices) -> str:
    """判定工单相对本次复查范围 / 实际扫描结果的覆盖结论。

    * 不在范围（单体 / 专业 / 核查项任一不匹配）-> ``out_of_scope``；
    * 在范围内但任一依赖 (单体, 专业) 模型扫描失败 -> ``scan_failed``；
    * 范围外单体与失败单体同时存在时，``scan_failed`` 优先（提醒重扫）；
    * 其余 -> ``covered``（无论本次是否仍检出；仍检出由纳管流程保活）。
    """
    if t.source not in (SOURCE_AUDIT, SOURCE_COORD):
        return COVER_OUT_OF_SCOPE
    if scope.units:
        # 申请范围用显示名（CLI 传入）；同时用本批映射与稳定标识比较，
        # 兼容“上一批带目录前缀、本批不带”等单体显示名漂移
        units = set(_resolve_ticket_units(t, idx))
        names = {t.unit} | {r.unit for r in t.refs if r.unit}
        wanted = set(scope.units)
        if not (units & wanted) and not (names & wanted):
            return COVER_OUT_OF_SCOPE
    if scope.disciplines and not scope.covers_disciplines(
            t.disciplines or [t.owner_discipline]):
        return COVER_OUT_OF_SCOPE
    if scope.kinds and not scope.covers_kind(t.kind):
        return COVER_OUT_OF_SCOPE
    pairs = _ticket_scan_pairs(t, idx)
    pair_units = {p[0] for p in pairs}
    if pair_units & idx.failed_units:
        return COVER_SCAN_FAILED
    for p in pairs:
        if p in idx.failed_pairs:
            return COVER_SCAN_FAILED
    # 依赖的 (单体, 专业) 模型本次没有成功扫描 -> 未实际覆盖（范围外）
    if pairs and not all(p in idx.ok_pairs for p in pairs):
        return COVER_OUT_OF_SCOPE
    return COVER_COVERED


class CollabError(ValueError):
    """协同闭环模块的配置 / 流转 / 权限错误。"""


# ============================================================ 权限 RBAC ----

# 动作 -> 允许执行的角色
ACTION_PERMISSIONS = {
    "view":             ROLES,
    "create":           (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD, ROLE_REVIEWER),
    "assign":           (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD),
    "fix":              (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD, ROLE_RESPONSIBLE),
    "verify":           (ROLE_COORDINATOR, ROLE_REVIEWER,
                         ROLE_DESIGN_LEAD, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD),
    "reject":           (ROLE_COORDINATOR, ROLE_REVIEWER,
                         ROLE_DESIGN_LEAD, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD),
    "close":            (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD),
    "writeback":        (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD, ROLE_RESPONSIBLE),
    "manage_roster":    (ROLE_COORDINATOR,),
    "manage_gate":      (ROLE_COORDINATOR,),
}

ACTION_CN = {
    "view": "查看", "create": "登记问题", "assign": "派单/改派",
    "fix": "报整改", "verify": "复核通过", "reject": "复核驳回",
    "close": "关闭工单", "writeback": "整改回写",
    "manage_roster": "维护名册", "manage_gate": "管理门禁",
}


def resolve_role(ledger: CollabLedger, actor: str,
                 default_role: str = ROLE_COORDINATOR) -> str:
    """解析操作人角色：名册查不到时回落 ``default_role``（向后兼容）。"""
    if actor == SYSTEM_ACTOR:
        return ROLE_COORDINATOR
    user = ledger.find_user(actor)
    return user.role if user else default_role


def _is_lead_for(role: str, discipline: str) -> bool:
    return LEAD_DISCIPLINE.get(role) == discipline


def require_permission(ledger: CollabLedger, action: str, actor: str,
                       ticket: Optional[CollabTicket] = None,
                       default_role: str = ROLE_COORDINATOR) -> str:
    """校验操作人是否有权执行 ``action``，返回其角色；无权抛 :class:`CollabError`。

    规则：

    * 系统账号放行全部动作；
    * 角色必须在 :data:`ACTION_PERMISSIONS` 允许列表内；
    * 责任人（responsible）只能整改 / 回写**本人名下**工单；
    * 专业负责人只能派单 / 整改 / 复核 / 关闭**本专业**工单；
    * 只读成员（viewer）仅可查看。
    """
    if actor == SYSTEM_ACTOR:
        return ROLE_COORDINATOR
    if action not in ACTION_PERMISSIONS:
        raise CollabError(f"未知操作“{action}”")
    role = resolve_role(ledger, actor, default_role)
    if role not in ACTION_PERMISSIONS[action]:
        raise CollabError(
            f"“{actor}”的角色为{ROLE_CN.get(role, role)}，"
            f"无权执行「{ACTION_CN.get(action, action)}」"
            f"（允许角色：{'、'.join(ROLE_CN[r] for r in ACTION_PERMISSIONS[action])}）")
    if ticket is not None and role != ROLE_COORDINATOR:
        # 责任人：仅限本人名下工单
        if role == ROLE_RESPONSIBLE and action in ("fix", "writeback"):
            if ticket.owner and ticket.owner != actor:
                raise CollabError(
                    f"责任人“{actor}”只能整改 / 回写本人名下工单"
                    f"（{ticket.ticket_id} 当前责任人：{ticket.owner or '未指派'}）")
        # 专业负责人：仅限本专业
        if role in (ROLE_DESIGN_LEAD, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD):
            disc = LEAD_DISCIPLINE[role]
            if ticket.owner_discipline and ticket.owner_discipline != disc:
                raise CollabError(
                    f"{ROLE_CN[role]}“{actor}”只能处理{DISC_CN.get(disc, disc)}"
                    f"专业工单（{ticket.ticket_id} 归属"
                    f"{DISC_CN.get(ticket.owner_discipline, ticket.owner_discipline)}专业）")
    return role


# ============================================================ 通知中心 ----

def notify(ledger: CollabLedger, ticket: Optional[CollabTicket], event: str,
           *, body: str = "", recipients: Optional[list[str]] = None,
           role: str = "", level: str = "", at: Optional[str] = None,
           title: str = "") -> Notification:
    """写入一条通知。``recipients`` 给姓名时逐人投递；否则按角色广播一条。"""
    at = at or _now()
    if recipients:
        # 逐人投递，仅投递给名册中未退订的成员；未入名册也投递（显式点名）
        first = None
        for name in recipients:
            user = ledger.find_user(name)
            if user is not None and (not user.active or not user.notify):
                continue
            n = Notification(
                notif_id=f"N-{ledger.next_notif_seq():05d}",
                ticket_id=ticket.ticket_id if ticket else "",
                event=event, recipient=name,
                title=title or EVENT_CN.get(event, event),
                body=body, level=level or EVENT_LEVEL.get(event, "info"),
                created_at=at)
            ledger.add_notification(n)
            first = first or n
        if first is None:
            # 全部退订时仍记录一条广播（保留留痕，不影响统计）
            first = _broadcast(ledger, ticket, event, body, role, level, at, title)
        return first
    return _broadcast(ledger, ticket, event, body, role, level, at, title)


def _broadcast(ledger, ticket, event, body, role, level, at, title) -> Notification:
    n = Notification(
        notif_id=f"N-{ledger.next_notif_seq():05d}",
        ticket_id=ticket.ticket_id if ticket else "",
        event=event, recipient="", recipient_role=role,
        title=title or EVENT_CN.get(event, event), body=body,
        level=level or EVENT_LEVEL.get(event, "info"), created_at=at)
    ledger.add_notification(n)
    return n


def _owner_and_leads(ledger: CollabLedger, ticket: CollabTicket
                     ) -> list[str]:
    """通知收件人：责任人 + 责任专业负责人（去重、保序）。"""
    out: list[str] = []
    if ticket.owner:
        out.append(ticket.owner)
    lead_role = DISCIPLINE_LEAD.get(ticket.owner_discipline)
    if lead_role:
        for u in ledger.users_by_role(lead_role):
            if u.name not in out:
                out.append(u.name)
    return out


def mark_notification_read(ledger: CollabLedger, notif_id: str,
                           actor: str) -> Notification:
    """标记通知已读；只能读自己的（广播通知角色成员均可读，按读取人复制已读）。"""
    for n in ledger.notifications:
        if n.notif_id == notif_id:
            if n.recipient and n.recipient != actor and actor != SYSTEM_ACTOR:
                raise CollabError(f"通知 {notif_id} 不属于“{actor}”")
            n.read = True
            n.read_at = _now()
            return n
    raise CollabError(f"通知不存在：{notif_id}")


def mark_all_read(ledger: CollabLedger, actor: str) -> int:
    """把某人的全部未读通知标记已读，返回条数。"""
    n = 0
    for x in ledger.notifications_for(actor, unread_only=True):
        x.read = True
        x.read_at = _now()
        n += 1
    return n


def notification_digest(ledger: CollabLedger, actor: str) -> dict:
    """某人的通知摘要（未读 / 各事件计数），用于报告与控制台。"""
    items = ledger.notifications_for(actor)
    by_event: dict[str, int] = {}
    n_unread = 0
    for n in items:
        by_event[n.event] = by_event.get(n.event, 0) + 1
        if not n.read:
            n_unread += 1
    return {"name": actor, "total": len(items), "unread": n_unread,
            "by_event": by_event}


# ============================================================ 整改时限 ----

def set_sla(ticket: CollabTicket, sla_hours: float, start_at: str = "",
            reset_clock: bool = True) -> None:
    """设置整改时限（小时）并计算截止时间。"""
    start_at = start_at or _now()
    ticket.sla_hours = float(sla_hours)
    if reset_clock or not ticket.due_at:
        base = datetime.fromisoformat(start_at)
        ticket.due_at = (base + timedelta(hours=float(sla_hours))
                         ).isoformat(timespec="seconds")


def _record(ticket: CollabTicket, action: str, from_status: str, to_status: str,
            actor: str, note: str, batch_id: str, extra: Optional[dict] = None,
            at: Optional[str] = None) -> dict:
    entry = {
        "batch_id": batch_id, "at": at or _now(), "action": action,
        "from": from_status, "to": to_status,
        "by": actor or "", "note": note or "",
    }
    if extra:
        entry.update(extra)
    ticket.history.append(entry)
    ticket.updated_at = entry["at"]
    return entry


def apply_sla_sweep(ledger: CollabLedger, sla_hours: float,
                    batch_id: str = "", now: Optional[datetime] = None
                    ) -> list[CollabTicket]:
    """整改时限扫描：临期提醒 + 超期自动升级（幂等）。返回发生状态变化的工单。"""
    now = now or datetime.now()
    now_s = now.isoformat(timespec="seconds")
    changed: list[CollabTicket] = []
    for t in ledger.tickets.values():
        if not t.sla_tracked or not t.due_at:
            # 历史活动工单补时限（不追溯，自当前时刻起算）
            if sla_hours > 0 and t.active and not t.due_at:
                set_sla(t, sla_hours, start_at=now_s)
                _record(t, "sla_backfill", t.status, t.status, SYSTEM_ACTOR,
                        f"历史工单补录整改时限 {sla_hours:g}h", batch_id, at=now_s)
            else:
                continue
        due = datetime.fromisoformat(t.due_at)
        if now <= due:
            # 24h 内临期且尚未提醒过
            reminded = any(h.get("action") == "due_soon" for h in t.history)
            if 0 <= (due - now).total_seconds() / 3600 <= 24 and not reminded:
                _record(t, "due_soon", t.status, t.status, SYSTEM_ACTOR,
                        f"整改临期：剩余 {t.sla_remaining_hours(now):g}h",
                        batch_id, at=now_s)
                notify(ledger, t, EVENT_DUE_SOON,
                       body=f"工单 {t.ticket_id}「{t.title}」整改临期，"
                            f"剩余 {t.sla_remaining_hours(now):g}h",
                       recipients=_owner_and_leads(ledger, t), at=now_s)
                changed.append(t)
            continue
        overdue_h = (now - due).total_seconds() / 3600
        target = (ESCALATION_PROJECT_MANAGER if overdue_h > t.sla_hours
                  else ESCALATION_DISCIPLINE_LEAD)
        if t.escalation_level >= target:
            if not t.escalated:
                t.escalated = True
                t.escalated_at = t.escalated_at or now_s
            continue
        prev = t.escalation_level
        t.escalation_level = target
        t.escalated = True
        t.escalated_at = now_s
        cn = ESCALATION_CN[target]
        _record(t, "escalate", t.status, t.status, SYSTEM_ACTOR,
                f"超过整改时限 {t.sla_hours:g}h 未完成，自动升级：{cn}"
                f"（已超期 {overdue_h:.1f}h）", batch_id,
                extra={"level": target, "prev_level": prev}, at=now_s)
        # 通知：超期/升级 -> 责任人、专业负责人；升到项目级时额外通知协调人
        recips = _owner_and_leads(ledger, t)
        if target == ESCALATION_PROJECT_MANAGER:
            recips += [u.name for u in ledger.users_by_role(ROLE_COORDINATOR)
                       if u.name not in recips]
        notify(ledger, t,
               EVENT_OVERDUE if prev == 0 else EVENT_ESCALATED,
               body=f"工单 {t.ticket_id}「{t.title}」已超期 {overdue_h:.1f}h，{cn}",
               recipients=recips, at=now_s)
        changed.append(t)
    return changed


# ============================================================ 多源纳管 ----

def _refs_from_coord_elements(elements: list[dict],
                              file_by_unit: Optional[dict[str, str]] = None
                              ) -> list[ModelRef]:
    file_by_unit = file_by_unit or {}
    refs = []
    for e in elements:
        fp = e.get("file_path", "") or file_by_unit.get(e.get("unit", ""), "")
        uk = e.get("unit_key", "") or os.path.splitext(
            os.path.basename(fp))[0] if fp else e.get("unit", "")
        refs.append(ModelRef(
            global_id=e.get("global_id", ""),
            ifc_type=e.get("ifc_type", ""),
            name=e.get("name", ""),
            discipline=e.get("discipline", ""),
            unit=e.get("unit", ""),
            unit_key=uk,
            storey=e.get("storey", ""),
            file_path=fp))
    return refs


def _coord_owner(kind: str, owners: dict[str, str], fallback_lead: bool
                 ) -> tuple[str, str]:
    disc = KIND_OWNER_DISCIPLINE.get(kind, "")
    name = owners.get(disc, "")
    return disc, name


def ingest_coordination(ledger: CollabLedger, coord_result,
                        batch_id: str,
                        owners: Optional[dict[str, str]] = None,
                        sla_hours: float = 72.0,
                        sweep: bool = True,
                        scope: Optional[ScanScope] = None,
                        idx: Optional[_ScanIndices] = None,
                        now: Optional[str] = None,
                        progress: Optional[Callable[[str], None]] = None
                        ) -> tuple[list[CollabTicket], set[str]]:
    """把一次多专业协同核查结果合入闭环台账。

    协同工单（COORD-xxxx）映射为闭环工单（COLL-xxxx），指纹沿用协同指纹，
    已在台账中的工单保留状态 / 责任人 / 整改记录。

    Args:
        scope: 局部复查范围；范围外的协同问题不纳管 / 不刷新。
        idx: 批次扫描索引，用于在工单上盖实际模型版本戳。
        sweep: 是否执行自动闭环与时限扫描（:func:`ingest_batch` 统一扫描时
            传 ``False``）。

    Returns:
        (本批协同工单, 本批检出且在范围内的协同工单指纹集合)。
    """
    scope = scope or ScanScope()
    now = now or _now()
    owners = owners or dict(getattr(coord_result, "owners", {}) or {})
    file_by_unit = {f.unit: f.file_path for f in getattr(coord_result, "files", [])
                    if getattr(f, "file_path", "")}
    seq = ledger.next_ticket_seq()
    out: list[CollabTicket] = []
    present: set[str] = set()

    for ci in coord_result.issues:
        # 局部复查：专业 / 核查项范围外的协同问题不参与本次纳管
        if not scope.covers_disciplines(ci.disciplines):
            continue
        if scope.kinds and not scope.covers_kind(ci.kind):
            continue
        # 闭环台账以 (来源 + 协同指纹) 作主键，避免与其它来源撞键
        key = f"fp:{ci.fingerprint}"
        present.add(key)
        refs = _refs_from_coord_elements(ci.elements, file_by_unit)
        ref_units = sorted({r.unit for r in refs if r.unit})
        ref_keys = sorted({r.unit_key for r in refs if r.unit_key})
        main_unit = ref_units[0] if len(ref_units) == 1 else ""
        main_key = ref_keys[0] if len(ref_keys) == 1 else ""
        old = ledger.get(key)
        if old is None:
            t = CollabTicket(
                ticket_id=f"COLL-{seq:04d}",
                fingerprint=key,
                source=SOURCE_COORD,
                kind=ci.kind,
                severity=ci.severity,
                title=ci.title, detail=ci.detail,
                refs=refs,
                disciplines=list(ci.disciplines),
                location=tuple(ci.location),
                storey=ci.storey,
                unit=main_unit, unit_key=main_key,
                measure=ci.measure, measure_label=ci.measure_label,
                owner_discipline=ci.owner_discipline,
                owner=ci.owner or owners.get(ci.owner_discipline, ""),
                status=ci.status if ci.status in COLLAB_ACTIVE_STATUSES
                else STATUS_OPEN,
                created_batch=batch_id, created_at=now, updated_at=now,
                created_by=SYSTEM_ACTOR,
                sla_hours=getattr(ci, "sla_hours", 0.0) or 0.0,
                due_at=getattr(ci, "due_at", "") or "",
                source_ref=ci.issue_id,
                present_in_scan=True,
            )
            if not t.owner:
                t.owner = _default_owner(ledger, t.owner_discipline)
            if t.sla_hours <= 0 and sla_hours > 0:
                set_sla(t, sla_hours, start_at=now)
            _record(t, "created", "", STATUS_OPEN, SYSTEM_ACTOR,
                    f"多专业协同核查发现并派单（{COORD_KIND_CN.get(ci.kind, ci.kind)}）"
                    + (f"，整改时限 {sla_hours:g}h" if sla_hours > 0 else ""),
                    batch_id, at=now)
            _stamp_scan(t, idx, batch_id, now, result=COVER_PRESENT)
            ledger.tickets[key] = t
            seq += 1
            notify(ledger, t, EVENT_CREATED,
                   body=f"新协同问题派单：{t.title}",
                   recipients=_owner_and_leads(ledger, t), at=now)
            out.append(t)
        else:
            old.present_in_scan = True
            reopened = _refresh_from_scan(ledger, old, ci, refs, now, owners)
            _stamp_scan(old, idx, batch_id, now, result=COVER_PRESENT)
            if reopened:
                notify(ledger, old, EVENT_REOPENED,
                       body=f"协同问题回归重开：{old.title}",
                       recipients=_owner_and_leads(ledger, old), at=now)
            out.append(old)

    if sweep:
        if idx is None:
            idx = _indices_from_coord(coord_result)
        stats = _sweep_with_coverage(ledger, present, {SOURCE_COORD},
                                     batch_id, scope=scope, idx=idx, now=now)
        # 用空批次对象补登记复查记录（独立协同核查无 BatchResult）
        _record_scan_run(ledger, batch_id, now, scope=scope, idx=idx,
                         present=present, stats=stats)
        apply_sla_sweep(ledger, sla_hours, batch_id,
                        now=datetime.fromisoformat(now))
    if progress:
        progress(f"协同问题纳管完成：本批 {len(out)} 项")
    return out, present


def _stamp_scan(t: CollabTicket, idx: Optional[_ScanIndices], batch_id: str,
                now: str, *, result: str,
                versions: Optional[dict[str, str]] = None) -> None:
    """在工单上记录最近一次扫描 / 覆盖留痕（模型版本按 “单体|文件” 存）。"""
    if versions is None:
        versions = {}
    if idx is not None:
        for unit, disc in _ticket_scan_pairs(t, idx):
            ver = idx.pair_versions.get((unit, disc))
            if ver:
                versions[f"{unit}|{idx.pair_files.get((unit, disc), '')}"] = ver
    # 只在确有扫描留痕时更新批次 / 时刻 / 版本，避免范围外工单被误记
    if result == COVER_PRESENT or versions:
        t.last_scan_batch = batch_id
        t.last_scan_at = now
        if versions:
            t.last_scan_versions = versions
    if result:
        t.last_cover_result = result
        t.last_cover_batch = batch_id
        t.last_cover_at = now


def _refresh_from_scan(ledger: CollabLedger, t: CollabTicket, ci, refs, now,
                       owners) -> bool:
    """用最新扫描刷新描述 / 几何，处理回归重开，保留责任与整改记录。

    返回该工单是否本次发生了回归重开。
    """
    reopened = False
    if t.closed:
        t.status = STATUS_OPEN
        t.escalated = False
        t.escalation_level = 0
        t.escalated_at = ""
        reopened = True
        _record(t, "reopen", t.status, STATUS_OPEN, SYSTEM_ACTOR,
                "已闭环问题在新模型中再次出现（回归），自动重开",
                t.created_batch, at=now)
    if not t.owner and owners.get(ci.owner_discipline):
        t.owner = owners[ci.owner_discipline]
    t.kind, t.severity = ci.kind, ci.severity
    t.title, t.detail = ci.title, ci.detail
    t.refs = refs
    t.disciplines = list(ci.disciplines)
    t.location = tuple(ci.location)
    t.storey = ci.storey
    ref_units = sorted({r.unit for r in refs if r.unit})
    ref_keys = sorted({r.unit_key for r in refs if r.unit_key})
    if len(ref_units) == 1:
        t.unit = ref_units[0]
    if len(ref_keys) == 1:
        t.unit_key = ref_keys[0]
    t.measure, t.measure_label = ci.measure, ci.measure_label
    t.updated_at = now
    return reopened


def _legacy_exact_audit_key(iss, unit_name: str) -> str:
    """旧口径精确指纹（issue_id + 消歧显示名），带 ``fp:`` 前缀。"""
    return "fp:" + make_legacy_audit_fingerprint(
        iss.kind, iss.global_ids, unit_name, iss.issue_id, iss.storey)


def _weak_audit_key(kind: str, gids: list[str], unit_key: str,
                    storey: str, location, measure: float = 0.0) -> tuple:
    """物理弱键：kind + 稳定单体 + 楼层 + 构件集 + 位置网格 + 量化指标。

    用于旧台账中“指纹带漂移 issue_id”的工单兼容匹配；不含扫描序号，
    只依赖模型物理要素。同一构件同楼层的多个问题靠位置网格与量化指标区分。
    """
    from .collab_model import quantize_loc
    gid_part = ",".join(sorted(g for g in (gids or []) if g))
    loc = tuple(quantize_loc(v) for v in tuple(location or ())[:2])
    meas = int(round(float(measure or 0.0) * 1000.0))
    return (kind, unit_key, storey or "", gid_part, loc, meas)


def _ticket_weak_audit_key(t: CollabTicket) -> tuple:
    uk = t.unit_key or os.path.splitext(
        os.path.basename(t.refs[0].file_path if t.refs else ""))[0]
    gids = [r.global_id for r in t.refs if r.global_id]
    return _weak_audit_key(t.kind, gids, uk, t.storey, t.location, t.measure)


def _find_legacy_audit_ticket(ledger: CollabLedger, iss,
                              unit_name: str, unit_key: str
                              ) -> tuple[Optional[str], Optional[CollabTicket]]:
    """在历史台账中回查与本次 audit 问题对应的旧工单。

    匹配优先级：

    1. 旧口径精确指纹（``issue_id|storey`` + 旧显示名 / 稳定单体名）；
    2. 物理弱键（kind+单体+楼层+构件集+位置网格），仅接受**唯一**候选，
       避免把同一单体上多个不同问题误并。

    返回 (旧主键, 工单)；找不到时 (None, None)。
    """
    gids = list(iss.global_ids or [])
    # 1) 旧精确指纹：同时尝试历史显示名与稳定单体名两种 unit 取值
    for uname in dict.fromkeys([unit_name, unit_key, ""]):
        old_fp = _legacy_exact_audit_key(iss, uname)
        hit = ledger.get(old_fp)
        if hit is not None and hit.source == SOURCE_AUDIT:
            return old_fp, hit
        k2, hit2 = ledger.find_by_fp_or_alias(old_fp)
        if hit2 is not None and hit2.source == SOURCE_AUDIT:
            return k2, hit2

    # 2) 物理弱键：在 audit 活动 / 已闭环工单中找唯一匹配
    target = _weak_audit_key(iss.kind, gids, unit_key, iss.storey,
                             iss.location, iss.measure)
    candidates = [
        (k, t) for k, t in ledger.tickets.items()
        if t.source == SOURCE_AUDIT and t.kind == iss.kind
        and _weak_key_compatible(t, target)]
    if len(candidates) == 1:
        return candidates[0]
    return None, None


def _weak_key_compatible(t: CollabTicket, target: tuple) -> bool:
    """工单弱键与目标是否一致（兼容旧工单缺失的稳定单体 / 楼层字段）。"""
    kind, unit_key, storey, gid_part, loc, meas = target
    tkind, tuk, tstorey, tgid, tloc, tmeas = _ticket_weak_audit_key(t)
    if tkind != kind:
        return False
    # 旧工单无稳定单体标识时不比对单体（由构件 GlobalId + 位置锁定）
    if tuk and unit_key and tuk != unit_key:
        return False
    if gid_part != tgid:
        return False
    # 楼层：旧工单可能未填顶层 storey，退而取构件引用楼层
    if tstorey != storey:
        ref_storeys = {r.storey for r in t.refs if r.storey}
        if tstorey or (storey and storey not in ref_storeys):
            return False
    # 至少有构件或位置强一致要素
    if not gid_part and loc != tloc:
        return False
    if gid_part and loc and tloc != loc:
        return False
    if gid_part and tloc == loc and tmeas != meas:
        return False
    return True


def ingest_batch(ledger: CollabLedger, batch,
                 sla_hours: float = 72.0,
                 include_infos: bool = False,
                 only_kinds: Optional[set[str]] = None,
                 scope: Optional[ScanScope] = None,
                 record_versions: bool = True,
                 progress: Optional[Callable[[str], None]] = None
                 ) -> list[CollabTicket]:
    """把一次批量审查（:class:`~ifc_audit.batch.BatchResult`）合入闭环台账。

    纳管每个成功单体 ``unit.model.issues`` 中的单模型问题，以及
    ``batch.coordination`` 的协同问题（若有）。规则校验 / 门禁阻断问题可由
    :func:`ingest_rule_violations` 另行纳管。返回本批新增 / 现存的工单。

    Args:
        scope: 局部复查范围（单体 / 专业 / 核查项）。范围外工单不纳管、不刷新、
            不参与自动销项，状态原样保留；范围内但对应模型扫描失败的工单同样保留。
        record_versions: 是否记录模型文件版本（大小 + 内容指纹）到工单与复查记录。

    每次合入都会在台账登记一条 :class:`~ifc_audit.collab_model.ScanRun`，
    记录申请范围、实际成功 / 失败扫描的单体、模型版本与各类覆盖结果计数。
    """
    if scope is None:
        scope = ScanScope.make(kinds=only_kinds)
    batch_id = batch.batch_id
    now = _now()
    seq = ledger.next_ticket_seq()
    out: list[CollabTicket] = []
    present: set[str] = set()
    owners = {}

    idx = _build_scan_indices(
        batch, getattr(batch, "coordination", None),
        hash_files=record_versions)

    coord = getattr(batch, "coordination", None)
    if coord is not None:
        owners = dict(getattr(coord, "owners", {}) or {})
        coord_tickets, coord_present = ingest_coordination(
            ledger, coord, batch_id, owners=owners, sla_hours=sla_hours,
            sweep=False, scope=scope, idx=idx, now=now)
        out.extend(coord_tickets)
        present.update(coord_present)

    # 单模型问题均归属建筑专业；专业范围不含建筑时整体跳过
    audit_in_disc = scope.covers_disciplines(["arch"])
    for unit in batch.units:
        if scope.units and unit.name not in scope.units:
            continue
        model = getattr(unit, "model", None)
        if model is None:
            # 扫描失败的单体在 _sweep_with_coverage 中按 scan_failed 保留
            continue
        unit_name = unit.name
        unit_key = getattr(unit, "unit_key", "") or \
            os.path.splitext(os.path.basename(
                getattr(unit, "file_path", "")))[0]
        file_path = getattr(unit, "file_path", "")
        for iss in model.issues:
            if iss.severity == "info" and not include_infos:
                continue
            if not scope.covers_kind(iss.kind) or not audit_in_disc:
                continue
            disc = AUDIT_KIND_OWNER.get(iss.kind, "arch")
            # 稳定指纹：不含每批重排的 issue_id，单体用跨批次稳定标识
            key = "fp:" + make_audit_fingerprint(
                iss.kind, iss.global_ids, unit_key,
                storey=iss.storey, location=iss.location,
                measure=iss.measure)
            refs = [ModelRef(global_id=gid, discipline=disc, unit=unit_name,
                             unit_key=unit_key, storey=iss.storey,
                             file_path=file_path)
                    for gid in iss.global_ids]
            loc3 = (float(iss.location[0]), float(iss.location[1]), 0.0) \
                if iss.location else (0.0, 0.0, 0.0)
            # 兼容历史台账：新指纹未命中时，用旧口径（issue_id + 显示名）与
            # 物理弱键（kind + 单体 + 楼层 + 构件）回查旧工单并迁移
            old_key, old = ledger.find_by_fp_or_alias(key)
            migrated = False
            if old is None:
                old_key, old = _find_legacy_audit_ticket(
                    ledger, iss, unit_name, unit_key)
            if old is not None and old_key is not None and old_key != key:
                ledger.rekey(old_key, key)
                if old_key not in old.fingerprint_aliases:
                    old.fingerprint_aliases.append(old_key)
                migrated = True
                _record(old, "fp_migrate", old.status, old.status, SYSTEM_ACTOR,
                        "单体稳定标识 / 指纹口径升级，历史工单指纹迁移，"
                        "状态与整改记录保留", batch_id, at=now,
                        extra={"from": old_key, "to": key})
            present.add(key)
            if old is None:
                t = CollabTicket(
                    ticket_id=f"COLL-{seq:04d}",
                    fingerprint=key, source=SOURCE_AUDIT, kind=iss.kind,
                    severity=iss.severity, title=iss.title, detail=iss.detail,
                    refs=refs, disciplines=[disc], location=loc3,
                    storey=iss.storey, unit=unit_name, unit_key=unit_key,
                    measure=iss.measure,
                    owner_discipline=disc,
                    owner=owners.get(disc, ""),
                    created_batch=batch_id, created_at=now, updated_at=now,
                    created_by=SYSTEM_ACTOR, source_ref=iss.issue_id,
                    present_in_scan=True)
                t.owner = t.owner or _default_owner(ledger, disc)
                if sla_hours > 0:
                    set_sla(t, sla_hours, start_at=now)
                _record(t, "created", "", STATUS_OPEN, SYSTEM_ACTOR,
                        f"批量审查发现并派单（{AUDIT_KIND_CN.get(iss.kind, iss.kind)}）"
                        f"，整改时限 {sla_hours:g}h", batch_id, at=now)
                _stamp_scan(t, idx, batch_id, now, result=COVER_PRESENT)
                ledger.tickets[key] = t
                seq += 1
                notify(ledger, t, EVENT_CREATED,
                       body=f"新审查问题派单：[{unit_name}] {t.title}",
                       recipients=_owner_and_leads(ledger, t), at=now)
                out.append(t)
            else:
                old.present_in_scan = True
                old.title, old.detail = iss.title, iss.detail
                old.severity = iss.severity
                old.measure = iss.measure
                old.refs = refs
                old.location = loc3
                old.unit = unit_name
                old.unit_key = unit_key
                old.source_ref = iss.issue_id
                old.updated_at = now
                _stamp_scan(old, idx, batch_id, now, result=COVER_PRESENT)
                if old.closed:
                    old.status = STATUS_OPEN
                    old.escalated = False
                    old.escalation_level = 0
                    _record(old, "reopen", old.status, STATUS_OPEN,
                            SYSTEM_ACTOR,
                            "已闭环审查问题在新模型中再次出现（回归），自动重开",
                            batch_id, at=now)
                    notify(ledger, old, EVENT_REOPENED,
                           body=f"回归问题重开：[{unit_name}] {old.title}",
                           recipients=_owner_and_leads(ledger, old), at=now)
                out.append(old)

    # 仅对“范围内 + 扫描成功且未再检出”的工单自动销项；
    # 范围外（out_of_scope）/ 扫描失败（scan_failed）工单状态保留
    stats = _sweep_with_coverage(
        ledger, present, {SOURCE_AUDIT, SOURCE_COORD}, batch_id,
        scope=scope, idx=idx, now=now)
    run = _record_scan_run(ledger, batch_id, now, scope=scope, idx=idx,
                           present=present, stats=stats)
    apply_sla_sweep(ledger, sla_hours, batch_id)
    if progress:
        if scope.partial or run.incomplete:
            label = (f"局部复查（{scope.describe()}）" if scope.partial
                     else "全量复查（部分模型扫描失败 / 缺失）")
            progress(f"{label}：自动销项 "
                     f"{run.n_auto_verified + run.n_auto_cleared} 项，"
                     f"范围外保留 {run.n_out_of_scope} 项，"
                     f"扫描失败保留 {run.n_scan_failed} 项，"
                     f"失败模型 {len(run.failed_files)} 个")
        progress(f"批量审查问题纳管完成：台账共 {len(ledger.tickets)} 项")
    return out


def _indices_from_coord(coord, *, hash_files: bool = True) -> _ScanIndices:
    idx = _ScanIndices()
    for f in getattr(coord, "files", []):
        display = getattr(f, "unit", "")
        fp = getattr(f, "file_path", "")
        unit = getattr(f, "unit_key", "") or \
            os.path.splitext(os.path.basename(fp))[0]
        disc = getattr(f, "discipline", "") or ""
        pair = (unit, disc)
        if display:
            idx.name_to_key[display] = unit
        if getattr(f, "ok", True):
            idx.ok_pairs.add(pair)
            idx.ok_units.add(unit)
            if unit:
                idx.scanned_units.add(unit)
            ver = model_file_version(fp) if hash_files and fp else ""
            if ver:
                idx.pair_versions[pair] = ver
                idx.pair_files[pair] = fp
                idx.files_info.append({"unit": unit, "display": display,
                                       "discipline": disc,
                                       "file": fp, "version": ver})
        else:
            idx.failed_pairs.add(pair)
            if unit:
                idx.failed_units.add(unit)
            idx.failed_files.append({"unit": unit, "display": display,
                                     "discipline": disc, "file": fp,
                                     "error": getattr(f, "error", "")})
    return idx


def _ticket_version_map(t: CollabTicket, idx: _ScanIndices) -> dict[str, str]:
    ver_map: dict[str, str] = {}
    for unit, disc in _ticket_scan_pairs(t, idx):
        ver = idx.pair_versions.get((unit, disc))
        if ver:
            ver_map[f"{unit}|{idx.pair_files.get((unit, disc), '')}"] = ver
    return ver_map


def _sweep_with_coverage(ledger: CollabLedger, present: set[str],
                         sources: set[str], batch_id: str, *,
                         scope: ScanScope, idx: _ScanIndices,
                         now: str) -> dict:
    """覆盖感知的自动销项，返回分类统计（changed/auto_verified/...）。

    只处理 sources 内、活动、本批未检出的工单，按覆盖结论分流：

    * covered：范围内且依赖模型全部扫描成功 -> 待复核自动通过 / 其余标已消除；
    * scan_failed：范围内但模型扫描失败 -> 保留，记 ``scan_failed`` 流转；
    * out_of_scope：不在本次复查范围 -> 保留，仅更新覆盖留痕字段。
    """
    stats = {"changed": [], "auto_verified": [], "auto_cleared": [],
             "scan_failed": [], "out_of_scope": []}
    scope_note = f"（复查范围：{scope.describe()}）" if scope.partial else ""
    for key, t in ledger.tickets.items():
        if t.source not in sources or t.source == SOURCE_MANUAL:
            continue
        if not t.active:
            continue
        # 本批仍检出：新指纹命中，或迁移前的旧指纹别名命中
        if key in present:
            continue
        result = ticket_cover_result(t, scope, idx)
        if result == COVER_OUT_OF_SCOPE:
            t.last_cover_result = COVER_OUT_OF_SCOPE
            t.last_cover_batch = batch_id
            t.last_cover_at = now
            stats["out_of_scope"].append(t)
            continue
        if result == COVER_SCAN_FAILED:
            t.last_scan_batch = batch_id
            t.last_scan_at = now
            t.last_cover_result = COVER_SCAN_FAILED
            t.last_cover_batch = batch_id
            t.last_cover_at = now
            already = any(h.get("action") == "scan_failed"
                          and h.get("batch_id") == batch_id for h in t.history)
            if not already:
                pairs = _ticket_scan_pairs(t, idx)
                pair_units = {u for u, _ in pairs}
                failed = "、".join(
                    f"{f.get('display') or f['unit']}（{f['error'] or '扫描失败'}）"
                    for f in idx.failed_files
                    if (f["unit"], f["discipline"]) in pairs
                    or f["unit"] in pair_units)
                _record(t, "scan_failed", t.status, t.status, SYSTEM_ACTOR,
                        "范围内模型扫描失败，不予自动销项，工单状态保留"
                        + (f"：{failed}" if failed else "") + scope_note,
                        batch_id, at=now)
            stats["scan_failed"].append(t)
            continue
        # covered：成功覆盖且未再检出 -> 自动销项
        versions = _ticket_version_map(t, idx)
        if versions:
            t.last_scan_versions = versions
            t.cleared_versions = {**t.cleared_versions, **versions}
        t.last_scan_batch = batch_id
        t.last_scan_at = now
        t.last_cover_result = COVER_COVERED
        t.last_cover_batch = batch_id
        t.last_cover_at = now
        if t.status == STATUS_FIXED:
            _record(t, "auto_verify", STATUS_FIXED, STATUS_VERIFIED,
                    SYSTEM_ACTOR,
                    "重新核查未再检出，自动复核通过" + scope_note,
                    batch_id, at=now,
                    extra={"versions": versions} if versions else None)
            t.status = STATUS_VERIFIED
            t.verified_by = t.verified_by or SYSTEM_ACTOR
            t.verified_at = now
            notify(ledger, t, EVENT_VERIFIED,
                   body=f"工单 {t.ticket_id} 整改后重新核查未检出，自动闭环",
                   recipients=_owner_and_leads(ledger, t), at=now)
            stats["auto_verified"].append(t)
        else:
            _record(t, "auto_clear", t.status, STATUS_CLEARED, SYSTEM_ACTOR,
                    "重新核查未再检出，问题在模型中已消失" + scope_note,
                    batch_id, at=now,
                    extra={"versions": versions} if versions else None)
            t.status = STATUS_CLEARED
            stats["auto_cleared"].append(t)
        stats["changed"].append(t)
    return stats


def _record_scan_run(ledger: CollabLedger, batch_id: str, now: str, *,
                     scope: ScanScope, idx: _ScanIndices, present: set[str],
                     stats: dict) -> ScanRun:
    """汇总本次复查的实际范围 / 版本 / 覆盖结果，登记 ScanRun 并发通知。"""
    uncovered = [t.ticket_id for t in (stats["out_of_scope"]
                                       + stats["scan_failed"])]
    run = ScanRun(
        batch_id=batch_id, at=now,
        units=sorted(scope.units), disciplines=sorted(scope.disciplines),
        kinds=sorted(scope.kinds),
        scoped=scope.partial,
        partial=scope.partial or bool(idx.failed_files)
        or bool(stats["out_of_scope"]),
        scanned_units=sorted(idx.scanned_units),
        failed_files=list(idx.failed_files),
        model_versions=list(idx.files_info),
        n_present=len(present),
        n_covered=len(stats["auto_verified"]) + len(stats["auto_cleared"]),
        n_auto_verified=len(stats["auto_verified"]),
        n_auto_cleared=len(stats["auto_cleared"]),
        n_out_of_scope=len(stats["out_of_scope"]),
        n_scan_failed=len(stats["scan_failed"]),
        uncovered_ticket_ids=uncovered,
    )
    # note 记录申请范围（展示用）；partial 反映实际覆盖是否完整
    if scope.partial:
        run.note = scope.describe()
    elif run.partial:
        run.note = "全量复查（含扫描失败 / 缺失单体）"
    else:
        run.note = "全量复查"
    ledger.add_run(run)
    if run.incomplete:
        _notify_scan_incomplete(ledger, run)
    return run


def _notify_scan_incomplete(ledger: CollabLedger, run: ScanRun) -> None:
    """局部复查存在未覆盖工单 / 扫描失败模型时，通知协调与各专业负责人。"""
    scope_label = "局部复查" if run.scoped else "全量复查"
    parts = [f"批次 {run.batch_id} {scope_label}覆盖不完整（{run.note}）"]
    if run.failed_files:
        names = "、".join(
            f"{f['unit'] or f['file']}" for f in run.failed_files[:10])
        parts.append(f"扫描失败模型 {len(run.failed_files)} 个（{names}），"
                     f"相关 {run.n_scan_failed} 张工单保留状态")
    if run.n_out_of_scope:
        parts.append(f"{run.n_out_of_scope} 张工单不在本次复查范围，状态保留")
    message = "；".join(parts) + "。仅成功覆盖且未再检出的问题已自动销项。"
    # 同一批次同消息不重复通知
    if any(n.event == EVENT_SCAN_INCOMPLETE and not n.ticket_id
           and n.body == message for n in ledger.notifications[-200:]):
        return
    leads = []
    for role in (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                 ROLE_STRUCT_LEAD, ROLE_MEP_LEAD):
        leads += [u.name for u in ledger.users_by_role(role)]
    notify(ledger, None, EVENT_SCAN_INCOMPLETE, body=message,
           recipients=sorted(set(leads)) or None, role=ROLE_COORDINATOR,
           at=run.at)


def sweep_absent_tickets(ledger: CollabLedger, present: set[str],
                         sources: set[str], batch_id: str,
                         now: Optional[str] = None) -> list[CollabTicket]:
    """本批重新核查未检出的活动工单自动闭环；人工登记问题不自动消除。

    全量复查语义（无范围、无失败模型）的便捷封装；带范围 / 失败模型的场景由
    :func:`ingest_batch` 内部的覆盖感知销项处理。
    """
    now = now or _now()
    stats = _sweep_with_coverage(
        ledger, present, sources, batch_id,
        scope=ScanScope(), idx=_ScanIndices(), now=now)
    return stats["changed"]


def ingest_rule_violations(ledger: CollabLedger, violations: list[dict],
                           batch_id: str,
                           owners: Optional[dict[str, str]] = None,
                           sla_hours: float = 72.0) -> list[CollabTicket]:
    """纳管规则校验 / 门禁阻断问题。

    每条 violation：``{kind, title, detail, severity, discipline, unit,
    storey, global_ids?, measure?}``。规则问题默认**只在不存在时建单**，
    已存在则刷新，不自动关闭（规则口径问题需人工确认）。
    """
    owners = owners or {}
    now = _now()
    seq = ledger.next_ticket_seq()
    out: list[CollabTicket] = []
    for v in violations:
        kind = v.get("kind", "rule_violation")
        disc = v.get("discipline", "arch")
        gids = v.get("global_ids", [])
        unit_name = v.get("unit", "")
        unit_key = v.get("unit_key", "") or unit_name
        key = "fp:" + make_ticket_fingerprint(
            SOURCE_RULE, kind, gids,
            unit=unit_key, extra=v.get("title", ""))
        refs = [ModelRef(global_id=g, discipline=disc, unit=unit_name,
                         unit_key=unit_key,
                         storey=v.get("storey", "")) for g in gids]
        old = ledger.get(key)
        if old is not None:
            old.detail = v.get("detail", old.detail)
            old.updated_at = now
            out.append(old)
            continue
        t = CollabTicket(
            ticket_id=f"COLL-{seq:04d}", fingerprint=key,
            source=SOURCE_RULE, kind=kind,
            severity=v.get("severity", "warning"),
            title=v.get("title", "规则校验问题"),
            detail=v.get("detail", ""), refs=refs, disciplines=[disc],
            storey=v.get("storey", ""), unit=unit_name, unit_key=unit_key,
            measure=float(v.get("measure", 0.0)),
            measure_label=v.get("measure_label", ""),
            owner_discipline=disc, owner=owners.get(disc, ""),
            created_batch=batch_id, created_at=now, updated_at=now,
            created_by=v.get("by", SYSTEM_ACTOR), present_in_scan=True)
        t.owner = t.owner or _default_owner(ledger, disc)
        if sla_hours > 0:
            set_sla(t, sla_hours, start_at=now)
        _record(t, "created", "", STATUS_OPEN, t.created_by,
                "规则校验 / 门禁判定发现并派单", batch_id, at=now)
        ledger.tickets[key] = t
        seq += 1
        notify(ledger, t, EVENT_GATE_BLOCKED,
               body=f"规则校验问题：{t.title}",
               recipients=_owner_and_leads(ledger, t), at=now)
        out.append(t)
    return out


def _default_owner(ledger: CollabLedger, discipline: str) -> str:
    """责任人缺失时回落到责任专业负责人。"""
    lead_role = DISCIPLINE_LEAD.get(discipline)
    if lead_role:
        leads = ledger.users_by_role(lead_role)
        if leads:
            return leads[0].name
    return ""


def open_manual_ticket(ledger: CollabLedger, *, title: str, detail: str,
                       actor: str, owner_discipline: str = "",
                       owner: str = "", severity: str = "warning",
                       unit: str = "", storey: str = "",
                       refs: Optional[list[ModelRef]] = None,
                       sla_hours: float = 0.0,
                       batch_id: str = "", note: str = "") -> CollabTicket:
    """人工登记会审 / 现场问题（需要 create 权限）。人工问题不被扫描自动消除。"""
    require_permission(ledger, "create", actor)
    if not title:
        raise CollabError("人工登记问题必须填写标题")
    if severity not in SEVERITIES:
        raise CollabError(f"未知严重程度“{severity}”，可选：{', '.join(SEVERITIES)}")
    if owner_discipline and owner_discipline not in DISCIPLINES:
        raise CollabError(f"未知专业“{owner_discipline}”")
    refs = refs or []
    now = _now()
    seq = ledger.next_ticket_seq()
    fp = "fp:" + make_ticket_fingerprint(
        SOURCE_MANUAL, f"manual-{seq}",
        [r.global_id for r in refs if r.global_id] or f"manual-{now}-{seq}",
        unit=unit, extra=title)
    t = CollabTicket(
        ticket_id=f"COLL-{seq:04d}", fingerprint=fp,
        source=SOURCE_MANUAL, kind="manual_review",
        severity=severity, title=title, detail=detail,
        refs=refs, disciplines=sorted({r.discipline for r in refs if r.discipline}),
        storey=storey, unit=unit,
        unit_key=(unit or next((r.unit_key for r in refs if r.unit_key), "")),
        owner_discipline=owner_discipline,
        owner=owner or _default_owner(ledger, owner_discipline),
        status=STATUS_OPEN, created_batch=batch_id,
        created_at=now, updated_at=now, created_by=actor,
        source_ref="", present_in_scan=True)
    if sla_hours > 0:
        set_sla(t, sla_hours, start_at=now)
    _record(t, "created", "", STATUS_OPEN, actor,
            note or "会审 / 现场人工登记", batch_id, at=now)
    ledger.tickets[fp] = t
    notify(ledger, t, EVENT_CREATED,
           body=f"人工登记问题派单：{t.title}",
           recipients=_owner_and_leads(ledger, t) or [actor], at=now)
    return t


# ============================================================ 工单流转 ----

def _find(ledger: CollabLedger, ticket_id: str) -> CollabTicket:
    try:
        return ledger.find(ticket_id)
    except KeyError:
        raise CollabError(f"台账中找不到工单：{ticket_id}")


def _transition(ledger: CollabLedger, t: CollabTicket, action: str,
                to_status: str, actor: str, note: str, batch_id: str,
                allowed_from: tuple[str, ...], extra: Optional[dict] = None,
                event: str = "") -> CollabTicket:
    if t.status not in allowed_from:
        raise CollabError(
            f"工单 {t.ticket_id} 当前状态为{STATUS_CN.get(t.status, t.status)}，"
            f"不能执行「{ACTION_CN.get(action, action)}」"
            f"（仅 {'、'.join(STATUS_CN[s] for s in allowed_from)} 状态可操作）")
    _record(t, action, t.status, to_status, actor, note, batch_id, extra)
    t.status = to_status
    if event:
        notify(ledger, t, event,
               body=f"工单 {t.ticket_id}「{t.title}」{EVENT_CN.get(event, '')}"
                    + (f"：{note}" if note else ""),
               recipients=_owner_and_leads(ledger, t)
               + ([actor] if actor not in _owner_and_leads(ledger, t) else []))
    return t


def assign_ticket(ledger: CollabLedger, ticket_id: str, actor: str,
                  owner: str = "", owner_discipline: str = "",
                  note: str = "", batch_id: str = "") -> CollabTicket:
    """派单 / 改派（需要 assign 权限）。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "assign", actor, t)
    if owner_discipline:
        if owner_discipline not in DISCIPLINES:
            raise CollabError(
                f"未知专业“{owner_discipline}”，可选：{', '.join(DISCIPLINES)}")
        t.owner_discipline = owner_discipline
    if owner:
        t.owner = owner
        # 被指派人若在名册中是只读，自动升为责任人角色，确保其能整改回写
        user = ledger.find_user(owner)
        if user is not None and user.role == ROLE_VIEWER:
            user.role = ROLE_RESPONSIBLE
            if not user.discipline:
                user.discipline = t.owner_discipline
    _record(t, "assign", t.status, t.status, actor, note, batch_id,
            extra={"owner": t.owner,
                   "owner_discipline": t.owner_discipline})
    notify(ledger, t, EVENT_ASSIGNED,
           body=f"工单 {t.ticket_id} 改派给 {t.owner or '未指派'}"
                f"（{DISC_CN.get(t.owner_discipline, t.owner_discipline)}专业）"
                + (f"：{note}" if note else ""),
           recipients=([t.owner] if t.owner else _owner_and_leads(ledger, t)))
    return t


def fix_ticket(ledger: CollabLedger, ticket_id: str, actor: str,
               note: str, batch_id: str = "") -> CollabTicket:
    """责任专业报整改完成（进入待复核）。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "fix", actor, t)
    overdue = t.is_overdue()
    _transition(ledger, t, "fix", STATUS_FIXED, actor, note, batch_id,
                (STATUS_OPEN, STATUS_REJECTED),
                extra={"overdue": True} if overdue else None,
                event=EVENT_FIXED)
    t.fixed_by, t.fixed_note, t.fixed_at = actor, note, t.updated_at
    # 整改说明同步进整改回写字段
    if note and not t.resolution:
        t.resolution = note
        t.writeback_at = t.updated_at
    return t


def writeback_fix(ledger: CollabLedger, ticket_id: str, actor: str,
                  resolution: str, resolution_gids: Optional[list[str]] = None,
                  batch_id: str = "") -> CollabTicket:
    """整改回写：责任专业回填整改说明与整改后构件（不改变状态，留痕回写）。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "writeback", actor, t)
    if not resolution:
        raise CollabError("整改回写必须填写整改说明")
    now = _now()
    t.resolution = resolution
    t.writeback_at = now
    if resolution_gids:
        t.resolution_refs = [ModelRef(global_id=g, discipline=t.owner_discipline,
                                      unit=t.unit, unit_key=t.unit_key)
                             for g in resolution_gids]
    _record(t, "writeback", t.status, t.status, actor,
            f"整改回写：{resolution}", batch_id,
            extra={"resolution_refs": [g for g in (resolution_gids or [])]},
            at=now)
    return t


def verify_ticket(ledger: CollabLedger, ticket_id: str, actor: str,
                  note: str = "复核通过", batch_id: str = "") -> CollabTicket:
    """复核通过，闭环工单。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "verify", actor, t)
    _transition(ledger, t, "verify", STATUS_VERIFIED, actor, note, batch_id,
                (STATUS_FIXED,), event=EVENT_VERIFIED)
    t.verified_by, t.verified_at = actor, t.updated_at
    t.review_note = note
    return t


def reject_ticket(ledger: CollabLedger, ticket_id: str, actor: str,
                  note: str, batch_id: str = "",
                  sla_hours: float = 72.0) -> CollabTicket:
    """复核驳回，退回责任专业整改（重排时限、清零升级标记）。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "reject", actor, t)
    if not note:
        raise CollabError("驳回必须填写原因（note）")
    _transition(ledger, t, "reject", STATUS_REJECTED, actor, note, batch_id,
                (STATUS_FIXED,), event=EVENT_REJECTED)
    t.review_note = note
    if sla_hours > 0:
        set_sla(t, sla_hours, start_at=t.updated_at)
    t.escalated = False
    t.escalation_level = 0
    t.escalated_at = ""
    _record(t, "sla_reset", STATUS_REJECTED, STATUS_REJECTED, SYSTEM_ACTOR,
            f"驳回后重排整改时限 {sla_hours:g}h", batch_id)
    return t


def close_ticket(ledger: CollabLedger, ticket_id: str, actor: str,
                 reason: str, batch_id: str = "") -> CollabTicket:
    """人工关闭（会审销项 / 设计豁免 / 不做处理），需 close 权限与原因。"""
    t = _find(ledger, ticket_id)
    require_permission(ledger, "close", actor, t)
    if not reason:
        raise CollabError("人工关闭必须填写原因（reason）")
    _transition(ledger, t, "close", STATUS_CLOSED, actor, reason, batch_id,
                (STATUS_OPEN, STATUS_FIXED, STATUS_REJECTED),
                event=EVENT_CLOSED)
    t.closed_by, t.closed_reason = actor, reason
    return t


# ============================================================ 跨模型定位 ----

def locate_ticket(ledger: CollabLedger, ticket_id: str) -> dict:
    """返回工单的跨模型定位信息：按专业 / 单体 / 文件分组的构件引用。

    供 GUI 点击高亮与 CLI 输出使用；同一工单可定位到建筑 / 结构 / 机电
    多份模型中的多个构件。
    """
    t = _find(ledger, ticket_id)
    by_file: dict[str, dict] = {}
    for r in t.refs:
        key = r.file_path or f"unit:{r.unit}"
        row = by_file.setdefault(key, {
            "file_path": r.file_path, "unit": r.unit,
            "discipline": r.discipline, "global_ids": []})
        if r.global_id and r.global_id not in row["global_ids"]:
            row["global_ids"].append(r.global_id)
    return {
        "ticket_id": t.ticket_id,
        "title": t.title,
        "source": t.source,
        "source_cn": SOURCE_CN.get(t.source, t.source),
        "storey": t.storey,
        "location": list(t.location),
        "disciplines": list(t.disciplines),
        "models": sorted(by_file.values(), key=lambda x: (x["discipline"],
                                                          x["unit"])),
        "resolution_refs": [r.to_dict() for r in t.resolution_refs],
    }


# ============================================================ 报告汇总 ----

def collab_summary(ledger: CollabLedger, now: Optional[datetime] = None) -> dict:
    """闭环台账汇总：按来源 / 状态 / 责任专业 / 楼层统计。"""
    now = now or datetime.now()
    by_source = {s: {"total": 0, "active": 0} for s in SOURCES}
    by_status = {s: 0 for s in (*COLLAB_ACTIVE_STATUSES,
                                *COLLAB_CLOSED_STATUSES)}
    by_owner_disc = {d: 0 for d in DISCIPLINES}
    by_storey: dict[str, int] = {}
    n_overdue = n_escalated = n_no_owner = 0
    fix_durations: list[float] = []
    close_counts = {s: 0 for s in COLLAB_CLOSED_STATUSES}

    for t in ledger.tickets.values():
        by_source.setdefault(t.source, {"total": 0, "active": 0})
        by_source[t.source]["total"] += 1
        if t.active:
            by_source[t.source]["active"] += 1
        by_status[t.status] = by_status.get(t.status, 0) + 1
        if t.active:
            by_owner_disc[t.owner_discipline] = \
                by_owner_disc.get(t.owner_discipline, 0) + 1
            # 规则校验类是批次级治理事件（通知协调人），不要求指派专业责任人
            if not t.owner and t.source != SOURCE_RULE:
                n_no_owner += 1
            if t.sla_tracked and t.is_overdue(now):
                n_overdue += 1
            if t.sla_tracked and t.escalated:
                n_escalated += 1
            if t.storey:
                by_storey[t.storey] = by_storey.get(t.storey, 0) + 1
        if t.status in close_counts:
            close_counts[t.status] += 1
        if t.status == STATUS_VERIFIED and t.created_at and t.verified_at:
            c = datetime.fromisoformat(t.created_at)
            v = datetime.fromisoformat(t.verified_at)
            fix_durations.append(round((v - c).total_seconds() / 3600.0, 1))

    total = len(ledger.tickets)
    active = sum(by_status[s] for s in COLLAB_ACTIVE_STATUSES)
    closed = total - active
    last = ledger.last_run()
    last_scan = None
    if last is not None:
        live_uncovered = [tid for tid in last.uncovered_ticket_ids
                          if _active_ticket(ledger, tid)]
        last_scan = {
            "batch_id": last.batch_id,
            "at": last.at,
            "scoped": last.scoped,
            "partial": last.partial,
            "scope": last.note,
            "scanned_units": list(last.scanned_units),
            "failed_files": list(last.failed_files),
            "n_auto_verified": last.n_auto_verified,
            "n_auto_cleared": last.n_auto_cleared,
            "n_out_of_scope": last.n_out_of_scope,
            "n_scan_failed": last.n_scan_failed,
            "uncovered_active": live_uncovered,
            "model_versions": list(last.model_versions),
        }
    return {
        "project": ledger.project,
        "updated_at": ledger.updated_at,
        "tickets_total": total,
        "tickets_active": active,
        "tickets_closed": closed,
        "close_rate": round(closed / total, 4) if total else 0.0,
        "by_source": by_source,
        "by_status": by_status,
        "active_by_owner_discipline": by_owner_disc,
        "active_by_storey": dict(sorted(by_storey.items())),
        "overdue": n_overdue,
        "escalated": n_escalated,
        "no_owner": n_no_owner,
        "closed_breakdown": close_counts,
        "avg_fix_hours": round(sum(fix_durations) / len(fix_durations), 1)
        if fix_durations else None,
        "last_scan": last_scan,
    }


def build_writeback(ledger: CollabLedger, batch_id: str = "") -> dict:
    """生成回写单体 / 批次的协同闭环结论。"""
    summ = collab_summary(ledger)
    per_unit: dict[str, dict] = {}
    for t in ledger.tickets.values():
        if not t.active:
            continue
        for unit in _ticket_units(t) or ["跨单体"]:
            row = per_unit.setdefault(unit, {
                "unit": unit, "active": 0, "errors": 0, "warnings": 0,
                "overdue": 0, "escalated": 0,
                "by_source": {s: 0 for s in SOURCES}})
            row["active"] += 1
            row["errors" if t.severity == "error" else "warnings"] += 1
            if t.is_overdue():
                row["overdue"] += 1
            if t.escalated and t.sla_tracked:
                row["escalated"] += 1
            row["by_source"][t.source] = \
                row["by_source"].get(t.source, 0) + 1
    return {
        "project": ledger.project,
        "batch_id": batch_id,
        "written_at": _now(),
        "tickets_total": summ["tickets_total"],
        "tickets_active": summ["tickets_active"],
        "tickets_closed": summ["tickets_closed"],
        "close_rate": summ["close_rate"],
        "overdue": summ["overdue"],
        "escalated": summ["escalated"],
        "by_status": summ["by_status"],
        "by_source": summ["by_source"],
        "active_by_owner_discipline": summ["active_by_owner_discipline"],
        "last_scan": summ["last_scan"],
        "per_unit": sorted(per_unit.values(), key=lambda r: r["unit"]),
    }


def _ticket_units(t: CollabTicket) -> list[str]:
    return sorted({r.unit for r in t.refs if r.unit} |
                  ({t.unit} if t.unit else set()))


# ============================================================ 闭环门禁 ----

@dataclass(frozen=True)
class CollabGate:
    """协同闭环放行门禁（-1 表示不限制该条）。

    闭环门禁只管**闭环流程治理**（超期、待复核积压、回写齐全、工单到人）；
    原始问题数量由质量门禁（单体/项目）与多专业协同门禁负责，避免同一批问题
    被多套门禁重复执法。标准口径只卡“超期”（责任人/工单到人在竣工阶段用
    ``strict`` 预设强制）。
    """

    max_active: int = -1             # 未闭环工单总数（默认不限制）
    max_active_errors: int = -1      # 未闭环错误级工单（默认不限制，由专业门禁判）
    max_overdue: int = 0             # 超期未整改
    max_pending_review: int = -1     # 待复核积压
    max_no_owner: int = -1           # 未指派责任人（默认不限制，strict 才强制）
    fix_sla_hours: float = 72.0
    require_writeback: bool = False  # 报整改是否必须有整改回写说明
    # 局部复查：最近一次扫描未覆盖（范围外 / 扫描失败）的活动工单，默认告警不阻断；
    # strict 要求复查覆盖完整（=0）才放行；-2=完全不检查
    max_uncovered_active: int = -1
    block_on_scan_failed: bool = False  # 存在扫描失败模型时是否阻断（strict=True）

    @property
    def enabled(self) -> bool:
        return self != for_gate_profile("none")


_COLLAB_GATE_PROFILES = {
    # 标准：只卡超期；问题数量交质量/协同门禁，是否到人交 strict，避免重复执法。
    # 局部复查不完整只告警（未覆盖/失败保留的工单不计入自动放行）。
    "default": {"max_active": -1, "max_active_errors": -1,
                "max_overdue": 0, "max_pending_review": -1,
                "max_no_owner": -1, "fix_sla_hours": 72.0,
                "require_writeback": False,
                "max_uncovered_active": -1, "block_on_scan_failed": False},
    "strict": {"max_active": 0, "max_active_errors": 0, "max_overdue": 0,
               "max_pending_review": 0, "max_no_owner": 0,
               "fix_sla_hours": 48.0, "require_writeback": True,
               "max_uncovered_active": 0, "block_on_scan_failed": True},
    "loose": {"max_active": -1, "max_active_errors": -1,
              "max_overdue": -1, "max_pending_review": -1,
              "max_no_owner": -1, "fix_sla_hours": 168.0,
              "require_writeback": False,
              "max_uncovered_active": -2, "block_on_scan_failed": False},
    "none": {"max_active": -1, "max_active_errors": -1, "max_overdue": -1,
             "max_pending_review": -1, "max_no_owner": -1,
             "fix_sla_hours": 72.0, "require_writeback": False,
             "max_uncovered_active": -2, "block_on_scan_failed": False},
}

COLLAB_GATE_PROFILES = tuple(_COLLAB_GATE_PROFILES)
COLLAB_GATE_PROFILE_CN = {
    "default": "标准闭环门禁（超期零容忍）",
    "strict": "严格闭环门禁（全部清零、工单到人、回写齐全）",
    "loose": "宽松闭环门禁（方案阶段）",
    "none": "不设闭环门禁",
}


def for_gate_profile(name: str = "default") -> CollabGate:
    if name not in _COLLAB_GATE_PROFILES:
        raise CollabError(
            f"未知闭环门禁预设“{name}”，可选：{', '.join(_COLLAB_GATE_PROFILES)}")
    return CollabGate(**_COLLAB_GATE_PROFILES[name])


def _active_ticket(ledger: CollabLedger, ticket_id: str) -> Optional[CollabTicket]:
    """按工单号取活动工单（历史记录中的已闭环工单返回 None）。"""
    try:
        t = ledger.find(ticket_id)
    except KeyError:
        return None
    return t if t.active else None


def evaluate_collab_gate(ledger: CollabLedger, gate: CollabGate,
                         batch_id: str = "",
                         notify_block: bool = True) -> tuple[bool, list[dict]]:
    """按闭环门禁评估，返回 (是否通过, 逐条判定)。未通过时投递门禁阻断通知。

    局部复查联动：取台账最近一次复查记录（:class:`ScanRun`）——

    * ``max_uncovered_active``：最近扫描未覆盖（范围外 / 扫描失败）的活动
      工单数；default 下为 **0 阈值但不阻断的告警项**，strict 下必须为 0；
    * ``block_on_scan_failed``：最近扫描存在失败模型，strict 下阻断，
      防止“没扫到”被当成“已整改”放行。
    """
    now = datetime.now()
    summ = collab_summary(ledger, now)
    active = [t for t in ledger.tickets.values() if t.active]
    n_active = summ["tickets_active"]
    n_errors = sum(1 for t in active if t.severity == "error")
    n_overdue = summ["overdue"]
    n_pending = summ["by_status"].get(STATUS_FIXED, 0)
    n_no_owner = summ["no_owner"]
    n_no_writeback = sum(1 for t in active
                         if gate.require_writeback and t.status == STATUS_FIXED
                         and not t.resolution)

    # 最近一次复查（局部复查）覆盖情况；按当前活动工单复核未覆盖集合，
    # 避免上一批次的未覆盖记录干扰本批次全量复查后的结论
    last = ledger.last_run()
    n_uncovered = n_scan_failed_files = 0
    run_batch = ""
    if last is not None:
        run_batch = last.batch_id
        n_scan_failed_files = len(last.failed_files)
        # 按当前活动工单复核未覆盖集合，避免已闭环 / 已重开工单干扰
        n_uncovered = sum(1 for tid in last.uncovered_ticket_ids
                          if _active_ticket(ledger, tid))

    checks = [
        ("max_active", n_active, f"未闭环工单 {n_active} 项", "int"),
        ("max_active_errors", n_errors, f"未闭环错误级工单 {n_errors} 项", "int"),
        ("max_overdue", n_overdue, f"超期未整改工单 {n_overdue} 项", "int"),
        ("max_pending_review", n_pending, f"待复核工单 {n_pending} 项", "int"),
        ("max_no_owner", n_no_owner, f"未指派责任人工单 {n_no_owner} 项", "int"),
        ("require_writeback", n_no_writeback,
         f"待复核但缺少整改回写说明 {n_no_writeback} 项", "bool"),
        ("max_uncovered_active", n_uncovered,
         f"本次复查未覆盖活动工单 {n_uncovered} 项"
         + (f"（批次 {run_batch}）" if run_batch and run_batch != batch_id
            else ""), "int_advisory"),
        ("block_on_scan_failed", n_scan_failed_files,
         f"扫描失败模型 {n_scan_failed_files} 个", "bool"),
    ]
    rules: list[dict] = []
    passed_all = True
    for key, actual, shown, unit in checks:
        if unit == "bool":
            value = getattr(gate, key)
            if not value:
                continue
            ok = actual == 0
            limit = "必须为 0"
        elif unit == "int_advisory":
            # 局部复查覆盖度：-2=不检查；-1=检查但仅告警（default，不阻断）；
            # >=0=数值限值，超限阻断（strict=0）
            value = getattr(gate, key)
            if value < -1:
                continue
            if value == -1:
                # 仅告警：有未覆盖工单时列一条提示规则，但不影响放行
                if actual <= 0:
                    continue
                ok = True
                limit = "仅告警（不阻断放行）"
            else:
                ok = actual <= value
                limit = f"≤ {value:g}"
        else:
            value = getattr(gate, key)
            if value < 0:
                continue
            ok = actual <= value
            limit = f"≤ {value:g}"
        passed_all = passed_all and ok
        rules.append({"key": key, "actual": shown, "limit": limit,
                      "passed": ok, "advisory": unit == "int_advisory"
                      and getattr(gate, key, -2) == -1,
                      "message": ("" if ok else f"{shown}，门禁要求 {limit}")})
    passed = (not gate.enabled) or passed_all
    if not passed and notify_block:
        message = "协同闭环门禁未通过，已阻断批次放行：" + \
            "；".join(r["message"] for r in rules if not r["passed"])
        # 同一批次 / 同一组失败规则不重复通知（批量每次都会重新评估门禁）
        already = any(
            n.event == EVENT_GATE_BLOCKED and not n.ticket_id
            and n.body == message
            for n in ledger.notifications[-200:])
        if not already:
            coord_leads = []
            for role in (ROLE_COORDINATOR, ROLE_DESIGN_LEAD,
                         ROLE_STRUCT_LEAD, ROLE_MEP_LEAD):
                coord_leads += [u.name for u in ledger.users_by_role(role)]
            notify(ledger, None, EVENT_GATE_BLOCKED,
                   body=message,
                   recipients=sorted(set(coord_leads)) or None,
                   role=ROLE_COORDINATOR)
    return passed, rules


# ============================================================ 名册管理 ----

def upsert_user(ledger: CollabLedger, actor: str, name: str, role: str,
                discipline: str = "") -> RosterUser:
    """名册新增 / 修改成员（仅项目协调）。"""
    require_permission(ledger, "manage_roster", actor)
    if not name:
        raise CollabError("成员姓名不能为空")
    if role not in ROLES:
        raise CollabError(f"未知角色“{role}”，可选：{', '.join(ROLES)}")
    if role in (ROLE_DESIGN_LEAD, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD):
        if not discipline:
            discipline = LEAD_DISCIPLINE[role]
    if discipline and discipline not in DISCIPLINES:
        raise CollabError(f"未知专业“{discipline}”")
    user = ledger.find_user(name) or RosterUser(name=name)
    user.role, user.discipline = role, discipline or user.discipline
    user.active = True
    return ledger.upsert_user(user)


def remove_user(ledger: CollabLedger, actor: str, name: str) -> None:
    require_permission(ledger, "manage_roster", actor)
    if not ledger.remove_user(name):
        raise CollabError(f"名册中没有成员：{name}")


# ============================================================ 路径 / 便捷 ----

def default_ledger_path(history_dir: str, project: str) -> str:
    """闭环台账默认路径：<history>/<项目>/collab_ledger.json。"""
    return os.path.join(history_dir, _slug(project), "collab_ledger.json")


def gate_violations_from_batch(batch) -> list[dict]:
    """从批次门禁判定结果提取规则校验 / 门禁阻断问题（供 ingest 用）。"""
    # 门禁 scope 用显示名；建立显示名 -> 稳定单体标识映射
    name_to_key = {u.name: (u.unit_key or u.name) for u in batch.units}
    out: list[dict] = []
    for r in getattr(batch, "gate_results", []):
        if r.passed:
            continue
        unit_name = r.scope if r.level == "unit" else ""
        out.append({
            "kind": f"gate_{r.level}_{r.key}",
            "title": f"[{r.level}] {r.rule} 超限",
            "detail": r.message,
            "severity": "error",
            "discipline": "arch",
            "unit": unit_name,
            "unit_key": name_to_key.get(unit_name, unit_name),
            "measure": 0.0,
        })
    return out
