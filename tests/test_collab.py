"""协同问题闭环模块测试（不依赖 pytest，可直接运行）。

用法::

    python tests/test_collab.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ifc_audit import collab                                          # noqa: E402
from ifc_audit.collab import (                                       # noqa: E402
    CollabError, SYSTEM_ACTOR, set_sla, apply_sla_sweep,
    make_ticket_fingerprint, gate_violations_from_batch,
    ingest_batch, ingest_coordination, ingest_rule_violations,
    open_manual_ticket, assign_ticket, fix_ticket, verify_ticket,
    reject_ticket, close_ticket, writeback_fix, locate_ticket,
    collab_summary, evaluate_collab_gate, for_gate_profile,
    upsert_user, build_writeback, ScanScope, ticket_cover_result,
    model_file_version,
)
from ifc_audit.collab_model import (                                 # noqa: E402
    CollabLedger, ModelRef,
    ROLE_COORDINATOR, ROLE_STRUCT_LEAD, ROLE_MEP_LEAD, ROLE_DESIGN_LEAD,
    ROLE_RESPONSIBLE, ROLE_REVIEWER, ROLE_VIEWER,
    SOURCE_AUDIT, SOURCE_COORD, SOURCE_RULE, SOURCE_MANUAL,
    STATUS_OPEN, STATUS_FIXED, STATUS_VERIFIED, STATUS_REJECTED,
    STATUS_CLEARED, STATUS_CLOSED,
)
from ifc_audit.coordination_model import DISC_MEP, DISC_STRUCT, DISC_ARCH  # noqa: E402


def _ledger() -> CollabLedger:
    led = CollabLedger(project="测试项目")
    upsert_user(led, "协调", "协调", ROLE_COORDINATOR)
    upsert_user(led, "协调", "张结", ROLE_STRUCT_LEAD, discipline="struct")
    upsert_user(led, "协调", "李机", ROLE_MEP_LEAD, discipline="mep")
    upsert_user(led, "协调", "王设", ROLE_DESIGN_LEAD, discipline="arch")
    upsert_user(led, "协调", "王复", ROLE_REVIEWER)
    upsert_user(led, "协调", "小赵", ROLE_RESPONSIBLE, discipline="mep")
    upsert_user(led, "协调", "路人", ROLE_VIEWER)
    return led


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    # -------------------------------------------------- 分派与权限 ----
    led = _ledger()
    t = open_manual_ticket(
        led, title="梁与风管碰撞", detail="1F 走廊", actor="张结",
        owner_discipline=DISC_STRUCT, sla_hours=48)
    check(t.ticket_id == "COLL-0001", f"工单编号 COLL-0001（实际 {t.ticket_id}）")
    check(t.owner == "张结", "未指派人时责任自动落到专业负责人")

    # 只读成员不能派单
    try:
        assign_ticket(led, t.ticket_id, "路人", owner="李机")
        check(False, "只读成员派单应被拒绝")
    except CollabError:
        check(True, "只读成员派单被 RBAC 拒绝")

    # 机电负责人不能处理结构工单
    try:
        assign_ticket(led, t.ticket_id, "李机", owner_discipline=DISC_MEP)
        check(False, "跨专业负责人改派应被拒绝")
    except CollabError:
        check(True, "专业负责人只能处理本专业工单")

    # 责任人只能整改本人名下工单
    t2 = open_manual_ticket(led, title="另一问题", detail="", actor="协调",
                            owner_discipline=DISC_MEP, owner="李机")
    try:
        fix_ticket(led, t2.ticket_id, "小赵", "我改好了")
        check(False, "责任人整改他人工单应被拒绝")
    except CollabError:
        check(True, "责任人只能整改本人名下工单")

    # 协调人改派后，被指派人自动获得责任人角色
    assign_ticket(led, t.ticket_id, "协调", owner="小赵",
                  owner_discipline=DISC_MEP, note="机电改路由")
    check(led.find_user("小赵").role == ROLE_RESPONSIBLE, "改派目标已是责任人角色")
    check(t.owner == "小赵" and t.owner_discipline == DISC_MEP, "改派成功")

    # --------------------------------------------- 状态流转与回写 ----
    writeback_fix(led, t.ticket_id, "小赵", "风管上翻 300mm",
                  resolution_gids=["DUCT-1"])
    check(bool(t.resolution) and t.resolution_refs[0].global_id == "DUCT-1",
          "整改回写记录整改说明与整改构件")
    check(t.active, "整改回写不改变工单状态")

    fix_ticket(led, t.ticket_id, "小赵", "路由整改完成")
    check(led.find(t.ticket_id).status == STATUS_FIXED, "报整改后进入待复核")

    # 待整改工单不能直接复核
    try:
        verify_ticket(led, t2.ticket_id, "王复")
        check(False, "待整改工单直接复核应报错")
    except CollabError:
        check(True, "仅待复核状态可复核")

    verify_ticket(led, t.ticket_id, "王复", "现场确认通过")
    check(led.find(t.ticket_id).status == STATUS_VERIFIED, "复核通过闭环")
    check(led.find(t.ticket_id).verified_by == "王复", "记录复核人")

    # 驳回必须填原因，并重排时限
    fix_ticket(led, t2.ticket_id, "李机", "改了")
    try:
        reject_ticket(led, t2.ticket_id, "王复", "")
        check(False, "空原因驳回应报错")
    except CollabError:
        check(True, "驳回必须填写原因")
    reject_ticket(led, t2.ticket_id, "王复", "仍有净距不足", sla_hours=24)
    check(led.find(t2.ticket_id).status == STATUS_REJECTED, "驳回退回整改")
    check(led.find(t2.ticket_id).escalation_level == 0, "驳回归零升级标记")

    # 人工关闭需要原因
    t3 = open_manual_ticket(led, title="会审问题", detail="", actor="协调",
                            owner_discipline=DISC_ARCH)
    close_ticket(led, t3.ticket_id, "协调", "设计豁免，会审纪要#3")
    check(led.find(t3.ticket_id).status == STATUS_CLOSED, "人工关闭")

    # 通知：派单 / 整改 / 驳回 / 闭环都投递给责任人
    dig = collab.notification_digest(led, "小赵")
    check(dig["unread"] >= 3, f"责任人收到多条未读通知（实际 {dig['unread']}）")
    n0 = led.notifications_for("小赵", unread_only=True)[0]
    collab.mark_notification_read(led, n0.notif_id, "小赵")
    check(collab.notification_digest(led, "小赵")["unread"] == dig["unread"] - 1,
          "通知标记已读")
    # 不能读他人通知
    try:
        collab.mark_notification_read(led, n0.notif_id, "路人")
        check(False, "读取他人通知应被拒绝")
    except CollabError:
        check(True, "通知按人隔离")

    # -------------------------------------------------- SLA / 升级 ----
    led2 = _ledger()
    sla_t = open_manual_ticket(led2, title="超期问题", detail="", actor="协调",
                               owner_discipline=DISC_MEP, owner="李机",
                               sla_hours=48)
    # 回溯起算时间到 60 小时前 -> 超期升级到专业负责人
    set_sla(sla_t, 48,
            start_at=(datetime.now() - timedelta(hours=60)
                      ).isoformat(timespec="seconds"))
    escalated = apply_sla_sweep(led2, 48, "B1")
    check(sla_t in escalated and sla_t.escalation_level == 1,
          "超期未整改自动升级到专业负责人")
    # 再超一个周期 -> 项目协调
    set_sla(sla_t, 48,
            start_at=(datetime.now() - timedelta(hours=120)
                      ).isoformat(timespec="seconds"))
    apply_sla_sweep(led2, 48, "B1")
    check(sla_t.escalation_level == 2, "再超一个周期升级到项目协调")
    check(sla_t.is_overdue(), "工单判定为超期")
    # 幂等：再扫一次不重复升级
    n_hist = len(sla_t.history)
    apply_sla_sweep(led2, 48, "B1")
    check(len(sla_t.history) == n_hist, "时限扫描幂等，不重复升级")

    # --------------------------------------- 自动闭环 / 回归重开 ----
    led3 = _ledger()
    k_open = "fp:" + make_ticket_fingerprint(
        SOURCE_AUDIT, "wall_free_end", ["GA"], unit="U1", extra="GAP-1|1F")
    k_fix = "fp:" + make_ticket_fingerprint(
        SOURCE_AUDIT, "wall_free_end", ["GB"], unit="U1", extra="GAP-2|1F")
    led3.tickets[k_open] = collab.CollabTicket(
        ticket_id="COLL-1001", fingerprint=k_open, source=SOURCE_AUDIT,
        kind="wall_free_end", title="敞开问题", owner_discipline="arch",
        status=STATUS_OPEN, created_batch="B1")
    led3.tickets[k_fix] = collab.CollabTicket(
        ticket_id="COLL-1002", fingerprint=k_fix, source=SOURCE_AUDIT,
        kind="wall_free_end", title="已整改待复核", owner_discipline="arch",
        status=STATUS_FIXED, created_batch="B1")
    manual = open_manual_ticket(led3, title="人工问题不自动消除", detail="",
                                actor="协调", owner_discipline="arch")
    # 本批只检出 GA：GB(待复核) 消失 -> 自动通过；GA 保留；manual 保留
    collab.sweep_absent_tickets(led3, {k_open},
                                {SOURCE_AUDIT, SOURCE_COORD}, "B2")
    check(led3.get(k_fix).status == STATUS_VERIFIED, "待复核问题消失自动复核通过")
    check(led3.get(k_open).status == STATUS_OPEN, "仍检出的问题保持待整改")
    check(manual.active, "人工登记问题不被自动扫描消除")

    # 已闭环工单再次出现 -> 回归重开
    from types import SimpleNamespace
    ci = SimpleNamespace(kind="wall_free_end", severity="error",
                         title="回归", detail="", disciplines=["arch"],
                         location=(0, 0, 0), storey="1F", measure=0,
                         measure_label="", owner_discipline="arch")
    reopened = collab._refresh_from_scan(
        led3, led3.get(k_fix), ci,
        [ModelRef(global_id="GB", discipline="arch", unit="U1")],
        collab._now(), {})
    check(reopened and led3.get(k_fix).status == STATUS_OPEN,
          "已闭环问题再现自动重开（回归）")

    # -------------------------------------------------- 规则校验 ----
    led4 = _ledger()
    v = ingest_rule_violations(led4, [{
        "kind": "gate_unit_max_errors",
        "title": "[unit] 错误数超限",
        "detail": "1F 有 3 个错误", "severity": "error",
        "discipline": "arch", "unit": "U1"}], "B1")
    check(len(v) == 1 and v[0].source == SOURCE_RULE, "规则校验问题纳管")
    # 同口径重复纳管不重复建单
    v2 = ingest_rule_violations(led4, [{
        "kind": "gate_unit_max_errors", "title": "[unit] 错误数超限",
        "detail": "刷新", "severity": "error",
        "discipline": "arch", "unit": "U1"}], "B2")
    check(len(led4.tickets) == 1 and v2[0].detail == "刷新",
          "同指纹规则问题合单刷新而非重复建单")

    # -------------------------------------------------- 跨模型定位 ----
    led5 = _ledger()
    tt = open_manual_ticket(
        led5, title="管线穿梁", detail="", actor="协调",
        owner_discipline=DISC_MEP,
        refs=[ModelRef(global_id="D1", discipline="mep", unit="机电模型",
                       file_path="/m.ifc", storey="1F"),
              ModelRef(global_id="B1", discipline="struct", unit="结构模型",
                       file_path="/s.ifc", storey="1F")])
    loc = locate_ticket(led5, tt.ticket_id)
    check(len(loc["models"]) == 2, "一条工单跨机电/结构两个模型定位")
    check({m["discipline"] for m in loc["models"]} == {"mep", "struct"},
          "定位按模型文件分组并带 GlobalId")

    # -------------------------------------------------- 门禁与汇总 ----
    led6 = _ledger()
    # 1 个错误未闭环 + 1 个警告已闭环
    te = open_manual_ticket(led6, title="错误问题", detail="", actor="协调",
                            owner_discipline=DISC_STRUCT, severity="error")
    tw = open_manual_ticket(led6, title="警告问题", detail="", actor="协调",
                            owner_discipline=DISC_ARCH, severity="warning")
    fix_ticket(led6, tw.ticket_id, "王设", "ok")
    verify_ticket(led6, tw.ticket_id, "王复")
    s = collab_summary(led6)
    check(s["tickets_total"] == 2 and s["tickets_active"] == 1
          and s["tickets_closed"] == 1, "汇总：总数/未闭环/已闭环")
    check(abs(s["close_rate"] - 0.5) < 1e-6, "闭环率 50%")

    # 默认闭环门禁只管流程治理（超期），不再就原始问题数量重复执法：
    # 有未闭环错误但未超期 -> 通过（数量由质量/协同门禁判）
    ok_default, rules_d = evaluate_collab_gate(
        led6, for_gate_profile("default"), "B1", notify_block=False)
    check(ok_default, "默认闭环门禁不因未闭环错误数量重复阻断（数量交专业门禁）")
    ok_loose, _ = evaluate_collab_gate(
        led6, for_gate_profile("loose"), "B1", notify_block=False)
    check(ok_loose, "宽松门禁放行")
    ok_strict, rules_s = evaluate_collab_gate(
        led6, for_gate_profile("strict"), "B1", notify_block=False)
    check(not ok_strict and any(r["key"] == "max_active" for r in rules_s),
          "严格门禁：有未闭环即阻断（竣工阶段全量清零）")

    # 默认门禁在出现“超期未整改”时仍阻断（闭环流程治理职责）
    set_sla(te, 72,
            start_at=(datetime.now() - timedelta(hours=80)
                      ).isoformat(timespec="seconds"))
    ok_overdue, rules_o = evaluate_collab_gate(
        led6, for_gate_profile("default"), "B1", notify_block=False)
    check(not ok_overdue and any(r["key"] == "max_overdue" for r in rules_o),
          "默认闭环门禁：超期未整改仍阻断")

    # 回写结构
    wb = build_writeback(led6, "B1")
    check(wb["tickets_active"] == 1 and len(wb["per_unit"]) >= 1,
          "整改回写：批次 + 按单体拆分结论")

    # -------------------------------------------------- 持久化 ----
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "collab.json")
        led6.save(path)
        back = CollabLedger.load(path)
        check(len(back.tickets) == len(led6.tickets)
              and len(back.users) == len(led6.users),
              "台账 / 名册 / 通知持久化往返一致")
        check(all(isinstance(t.location, tuple) for t in back.tickets.values()),
              "坐标反序列化为 tuple")
        check(all(isinstance(r, ModelRef)
                  for t in back.tickets.values() for r in t.refs),
              "构件引用反序列化为 ModelRef")

    # --------------------------------------- 局部复查：范围 / 版本 / 销项 ----
    from types import SimpleNamespace

    def _issue(kind, gid, iid, storey="1F"):
        return SimpleNamespace(kind=kind, severity="error",
                               title=f"{kind}-{gid}", detail="",
                               global_ids=[gid], storey=storey,
                               location=(0.0, 0.0), measure=0, issue_id=iid)

    def _unit(name, issues, ok=True, error="", path=None):
        model = SimpleNamespace(issues=issues) if ok else None
        return SimpleNamespace(name=name, model=model,
                               file_path=path or f"/{name}.ifc",
                               ok=ok, error=error)

    def _batch(units, bid, coord=None):
        return SimpleNamespace(batch_id=bid, units=units, coordination=coord)

    ledr = _ledger()
    # B1 全量：U1 墙端 GA、U2 墙端 GB、U2 重复 GC
    ingest_batch(ledr, _batch([
        _unit("U1", [_issue("wall_free_end", "GA", "GAP-1")]),
        _unit("U2", [_issue("wall_free_end", "GB", "GAP-2"),
                     _issue("duplicate_element", "GC", "DUP-1")]),
    ], "B1"), sla_hours=0)
    by_gid = {r.global_id: t for t in ledr.tickets.values()
              for r in t.refs}

    # 1) 范围描述与别名展开
    sc = ScanScope.make(units=["U1"], kinds=["wall"])
    check(sc.partial and "wall_end_gap" in sc.kinds
          and "room_no_geometry" in sc.kinds,
          "复查范围：wall 别名展开为墙体/围护类核查项")
    check("U1" in sc.describe(), "范围描述包含单体")

    # 2) 只复查 U1（成功且已无问题）：GA 自动消除；GB/GC 范围外保留
    ingest_batch(ledr, _batch([_unit("U1", [])], "B2"),
                 sla_hours=0, scope=ScanScope.make(units=["U1"]))
    check(by_gid["GA"].status == STATUS_CLEARED
          and by_gid["GA"].last_cover_result == "covered",
          "范围内成功覆盖且未检出 -> 自动消除并记 covered")
    check(by_gid["GB"].status == STATUS_OPEN
          and by_gid["GB"].last_cover_result == "out_of_scope",
          "范围外活动工单保留状态，标记 out_of_scope")
    run = ledr.last_run()
    check(run.partial and run.n_auto_cleared == 1
          and run.n_out_of_scope == 2 and run.n_scan_failed == 0
          and not run.failed_files,
          "复查记录：局部范围 + 自动销项/范围外计数正确")
    check(all(v["version"] for v in run.model_versions)
          and run.model_versions[0]["unit"] == "U1",
          "复查记录模型版本（大小-内容指纹）已留痕")
    check(by_gid["GA"].last_scan_versions
          and by_gid["GA"].cleared_versions,
          "销项工单记录复查模型版本（可追溯销项依据）")
    check(by_gid["GB"].last_scan_batch == "B1"
          and by_gid["GB"].last_cover_batch == "B2",
          "范围外工单保留首次扫描批次戳，复查只记覆盖结论不盖新戳")

    # 3) 范围含 U2 但 U2 扫描失败：GB/GC 保留并标 scan_failed
    ingest_batch(ledr, _batch([_unit("U2", [], ok=False,
                                    error="IFC 解析失败")], "B3"),
                 sla_hours=0, scope=ScanScope.make(units=["U2"]))
    check(by_gid["GB"].status == STATUS_OPEN
          and by_gid["GB"].last_cover_result == "scan_failed",
          "范围内模型扫描失败 -> 工单保留并标 scan_failed")
    check(any(h["action"] == "scan_failed" for h in by_gid["GB"].history),
          "扫描失败写入工单流转记录")
    run3 = ledr.last_run()
    check(len(run3.failed_files) == 1
          and run3.failed_files[0]["error"] == "IFC 解析失败"
          and run3.n_scan_failed == 2,
          "复查记录登记失败文件（单体/专业/文件/错误）")
    # 同一批次重复扫描失败不重复写流转记录
    ingest_batch(ledr, _batch([_unit("U2", [], ok=False,
                                    error="IFC 解析失败")], "B3"),
                 sla_hours=0, scope=ScanScope.make(units=["U2"]))
    n_fail_hist = sum(1 for h in by_gid["GB"].history
                      if h["action"] == "scan_failed")
    check(n_fail_hist == 1, "同批次扫描失败流转记录幂等，不重复留痕")

    # 4) 按核查项局部复查：只扫 duplicate -> GC 消除；GB 属 wall 保留
    ingest_batch(ledr, _batch([_unit("U2", [])], "B4"), sla_hours=0,
                 scope=ScanScope.make(units=["U2"], kinds=["duplicate"]))
    check(by_gid["GC"].status == STATUS_CLEARED,
          "核查项范围内问题未检出 -> 自动消除")
    check(by_gid["GB"].status == STATUS_OPEN
          and by_gid["GB"].last_cover_result == "out_of_scope",
          "核查项范围外（wall）问题保留")

    # 5) 全量复查全部成功且无问题 -> GB 自动消除，覆盖完整
    ingest_batch(ledr, _batch([_unit("U1", []), _unit("U2", [])], "B5"),
                 sla_hours=0)
    check(by_gid["GB"].status == STATUS_CLEARED
          and by_gid["GB"].last_cover_result == "covered",
          "全量复查成功覆盖 -> 剩余工单自动销项")
    check(not ledr.last_run().incomplete
          and ledr.last_run().n_out_of_scope == 0
          and ledr.last_run().n_scan_failed == 0,
          "全量复查覆盖完整：无范围外/失败保留")

    # 6) 通知：局部复查覆盖不完整时通知协调 / 专业负责人
    ledn = _ledger()
    ingest_batch(ledn, _batch([
        _unit("U1", [_issue("wall_free_end", "NA", "GAP-9")])], "N1"),
        sla_hours=0)
    ingest_batch(ledn, _batch([_unit("U2", [])], "N2"), sla_hours=0,
                 scope=ScanScope.make(units=["U2"]))
    from ifc_audit.collab_model import EVENT_SCAN_INCOMPLETE
    check(any(n.event == EVENT_SCAN_INCOMPLETE
              for n in ledn.notifications),
          "局部复查存在未覆盖工单 -> 投递复查覆盖不完整通知")

    # 7) 门禁联动：default 告警不阻断；strict 未覆盖/失败阻断
    ledg = _ledger()
    ingest_batch(ledg, _batch([
        _unit("U1", [_issue("wall_free_end", "GA", "GAP-1")])], "G1"),
        sla_hours=0)
    ingest_batch(ledg, _batch([_unit("U2", [])], "G2"), sla_hours=0,
                 scope=ScanScope.make(units=["U2"]))
    ok_d, rules_d = evaluate_collab_gate(
        ledg, for_gate_profile("default"), "G2", notify_block=False)
    adv = [r for r in rules_d if r["key"] == "max_uncovered_active"]
    check(ok_d and adv and adv[0]["passed"] and adv[0].get("advisory"),
          "default 门禁：局部复查未覆盖只告警、不阻断放行")
    ok_s, rules_s = evaluate_collab_gate(
        ledg, for_gate_profile("strict"), "G2", notify_block=False)
    check(not ok_s
          and any(r["key"] == "max_uncovered_active" and not r["passed"]
                  for r in rules_s),
          "strict 门禁：存在未覆盖活动工单即阻断")
    ledf = _ledger()
    ingest_batch(ledf, _batch([
        _unit("U1", [_issue("wall_free_end", "FA", "GAP-7")])], "F1"),
        sla_hours=0)
    ingest_batch(ledf, _batch(
        [_unit("U1", [], ok=False, error="损坏")], "F2"), sla_hours=0,
        scope=ScanScope.make(units=["U1"]))
    ok_sf, rules_sf = evaluate_collab_gate(
        ledf, for_gate_profile("strict"), "F2", notify_block=False)
    check(not ok_sf
          and any(r["key"] == "block_on_scan_failed" and not r["passed"]
                  for r in rules_sf),
          "strict 门禁：扫描失败模型阻断（防止“没扫到”被当成“已整改”）")

    # 8) 待复核工单在局部成功覆盖后自动复核通过
    ledv = _ledger()
    ingest_batch(ledv, _batch([
        _unit("U1", [_issue("wall_free_end", "VA", "GAP-3")])], "V1"),
        sla_hours=0)
    tv = next(iter(ledv.tickets.values()))
    fix_ticket(ledv, tv.ticket_id, "王设", "已整改")
    ingest_batch(ledv, _batch([_unit("U1", [])], "V2"), sla_hours=0,
                 scope=ScanScope.make(units=["U1"]))
    check(ledv.find(tv.ticket_id).status == STATUS_VERIFIED,
          "待复核工单局部复查成功覆盖且未检出 -> 自动复核通过")

    # 9) 历史台账兼容：旧 v1 台账（无复查字段）可加载，汇总不报错
    with tempfile.TemporaryDirectory() as td:
        old = {
            "schema_version": 1, "project": "老项目", "updated_at": "",
            "tickets": [{
                "ticket_id": "COLL-0001",
                "fingerprint": "fp:old1", "source": "audit",
                "kind": "wall_free_end", "severity": "error",
                "title": "老工单", "status": "open",
                "owner_discipline": "arch", "created_batch": "OLD",
                "refs": [{"global_id": "G", "discipline": "arch",
                          "unit": "U1"}],
                "unit": "U1", "history": []}],
            "users": [], "notifications": [],
        }
        p = os.path.join(td, "old.json")
        with open(p, "w", encoding="utf-8") as f:
            import json as _json
            _json.dump(old, f, ensure_ascii=False)
        old_led = CollabLedger.load(p)
        check(old_led.runs == [] and old_led.SCHEMA_VERSION == 2,
              "旧 v1 台账加载为空复查记录（兼容历史台账）")
        check(collab_summary(old_led)["last_scan"] is None,
              "旧台账汇总 last_scan=None 不报错")
        # 一次新扫描后自动升级为 v2 并登记复查记录
        ingest_batch(old_led, _batch([_unit("U1", [])], "NEW"), sla_hours=0)
        old_led.save(p)
        upgraded = CollabLedger.load(p)
        check(upgraded.runs and upgraded.runs[0].batch_id == "NEW",
              "旧台账重新扫描后升级 schema 并补登记复查记录")
        check(upgraded.find("COLL-0001").status == STATUS_CLEARED,
              "旧台账中覆盖后消失的老工单正常自动销项")

    # 10) 模型版本：文件变化版本随之变化
    with tempfile.TemporaryDirectory() as td:
        f1 = os.path.join(td, "m.ifc")
        with open(f1, "wb") as f:
            f.write(b"IFC v1 content")
        v1 = model_file_version(f1)
        with open(f1, "wb") as f:
            f.write(b"IFC v2 content changed")
        v2 = model_file_version(f1)
        check(v1 != v2, "模型文件内容变化 -> 版本指纹变化")

    # ---------------- 单体身份 / 工单指纹跨批次稳定性（防漂移回归）----
    def _issue2(kind, gid, iid, loc=(0.0, 0.0), storey="1F", measure=0.0):
        return SimpleNamespace(kind=kind, severity="warning",
                               title=f"{kind}-{gid}", detail="",
                               global_ids=[gid], storey=storey,
                               location=loc, measure=measure, issue_id=iid)

    def _unit2(name, issues, key=None, path=None, ok=True):
        return SimpleNamespace(
            name=name, unit_key=key or name,
            file_path=path or f"/proj/{key or name}.ifc",
            model=SimpleNamespace(issues=issues) if ok else None,
            ok=ok, error="" if ok else "坏")

    # 11) issue_id 每批重排不改变工单身份
    ledk = _ledger()
    ingest_batch(ledk, _batch([_unit2("楼A", [
        _issue2("room_enclosure_gap", "R1", "GAP-001", (1.0, 2.0), measure=0.30),
        _issue2("room_enclosure_gap", "R1", "GAP-002", (5.0, 6.0), measure=0.45),
    ])], "K1"), sla_hours=0)
    check(len(ledk.tickets) == 2, "同构件不同位置/指标的两个问题建两张工单")
    t_first = next(t for t in ledk.tickets.values()
                   if round(t.location[0], 1) == 1.0)
    fix_ticket(ledk, t_first.ticket_id, "王设", "已补墙")
    # 第二个批次：第一个缺口修复，残留问题的 issue_id 由 GAP-002 重排成 GAP-001
    ingest_batch(ledk, _batch([_unit2("楼A", [
        _issue2("room_enclosure_gap", "R1", "GAP-001", (5.0, 6.0), measure=0.45),
    ])], "K2"), sla_hours=0)
    check(len(ledk.tickets) == 2,
          "issue_id 序号重排不产生重复工单（指纹不含扫描序号）")
    t_first2 = ledk.find(t_first.ticket_id)
    t_second = next(t for t in ledk.tickets.values()
                    if round(t.location[0], 1) == 5.0)
    check(t_first2.status == STATUS_VERIFIED
          and t_first2.fixed_note == "已补墙",
          "已报整改且成功覆盖消失 -> 自动复核，整改记录保留")
    check(t_second.status == STATUS_OPEN,
          "残留问题即便 issue_id 重排仍是原工单（保持待整改）")

    # 12) 单体消歧显示名漂移（跨目录同名文件）不改变身份
    ledn = _ledger()
    ingest_batch(ledn, _batch([
        _unit2("A区-楼A", [_issue2("wall_free_end", "N1", "END-001")],
               key="楼A", path="/p/A区/楼A.ifc"),
        _unit2("B区-楼A", [_issue2("wall_free_end", "N2", "END-001")],
               key="楼A2", path="/p/B区/楼A.ifc"),
    ], "N1"), sla_hours=0)
    n_before = len(ledn.tickets)
    # 第二批次只扫 A 区文件 -> 不再有同名冲突，显示名退化为“楼A”
    ingest_batch(ledn, _batch([
        _unit2("楼A", [_issue2("wall_free_end", "N1", "END-001")],
               key="楼A", path="/p/A区/楼A.ifc"),
    ], "N2"), sla_hours=0, scope=ScanScope.make(units=["楼A"]))
    check(len(ledn.tickets) == n_before,
          "单体显示名（消歧前缀）变化不重复建单")
    tn = next(t for t in ledn.tickets.values()
              if any(r.global_id == "N1" for r in t.refs))
    check(tn.unit_key == "楼A", "工单稳定单体标识 unit_key 正确")

    # 13) 批次组成变化（少扫单体）不误销、重纳入后正常销项
    ledc = _ledger()
    ingest_batch(ledc, _batch([
        _unit2("1号楼", [_issue2("wall_end_gap", "C1", "GAP-001", (1, 1))]),
        _unit2("2号楼", [_issue2("wall_end_gap", "C2", "GAP-001", (2, 2))]),
        _unit2("3号楼", [_issue2("wall_end_gap", "C3", "GAP-001", (3, 3))]),
    ], "C1"), sla_hours=0)
    n_c = len(ledc.tickets)
    # 只复查 1/2 号楼；1 号楼已整改，2 号楼问题 issue_id 变成 GAP-009
    ingest_batch(ledc, _batch([
        _unit2("1号楼", []),
        _unit2("2号楼", [_issue2("wall_end_gap", "C2", "GAP-009", (2, 2))]),
    ], "C2"), sla_hours=0,
        scope=ScanScope.make(units=["1号楼", "2号楼"]))
    by_gid = {r.global_id: t for t in ledc.tickets.values()
              for r in t.refs}
    check(len(ledc.tickets) == n_c, "批次少扫单体不增 / 不减工单")
    check(by_gid["C1"].status == STATUS_CLEARED, "复查范围内消失 -> 已消除")
    check(by_gid["C2"].status == STATUS_OPEN
          and by_gid["C2"].last_cover_result == "present",
          "issue_id 重排但物理要素不变 -> 识别为同一仍检出问题")
    check(by_gid["C3"].status == STATUS_OPEN
          and by_gid["C3"].last_cover_result == "out_of_scope",
          "未复查单体工单保留（不误销）")
    # 3 号楼重新纳入且已整改
    ingest_batch(ledc, _batch([
        _unit2("1号楼", []), _unit2("2号楼", []), _unit2("3号楼", []),
    ], "C3"), sla_hours=0)
    check(by_gid["C3"].status == STATUS_CLEARED and len(ledc.tickets) == n_c,
          "缺扫单体重新纳入成功覆盖后正常销项，不重复建单")

    # 14) 历史台账（旧指纹=issue_id+消歧显示名）自动迁移且保留整改记录
    import json as _json
    from ifc_audit.collab_model import make_legacy_audit_fingerprint
    legacy_fp = "fp:" + make_legacy_audit_fingerprint(
        "wall_free_end", ["OLDG"], unit_name="A区-楼A",
        issue_id="END-003", storey="2F")
    legacy_ticket = {
        "ticket_id": "COLL-0001", "fingerprint": legacy_fp,
        "source": "audit", "kind": "wall_free_end", "severity": "warning",
        "title": "历史工单", "detail": "",
        "refs": [{"global_id": "OLDG", "discipline": "arch",
                  "unit": "A区-楼A", "file_path": "/p/A区/楼A.ifc",
                  "storey": "2F"}],
        "disciplines": ["arch"], "location": [3.0, 4.0, 0.0],
        "storey": "2F", "unit": "A区-楼A",
        "owner_discipline": "arch", "owner": "王设",
        "status": "fixed", "created_batch": "OLD",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-02T00:00:00", "created_by": "系统",
        "fixed_by": "王设", "fixed_note": "历史整改",
        "fixed_at": "2026-01-02T00:00:00", "sla_hours": 0,
        "history": [
            {"batch_id": "OLD", "at": "2026-01-01T00:00:00", "action": "created",
             "from": "", "to": "open", "by": "系统", "note": ""},
            {"batch_id": "OLD", "at": "2026-01-02T00:00:00", "action": "fix",
             "from": "open", "to": "fixed", "by": "王设",
             "note": "历史整改"}],
    }
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "legacy.json")
        with open(lp, "w", encoding="utf-8") as f:
            _json.dump({"schema_version": 1, "project": "历史项目",
                        "tickets": [legacy_ticket], "users": [],
                        "notifications": []}, f, ensure_ascii=False)
        ledl = CollabLedger.load(lp)
        # 新批次显示名退化为“楼A”、issue_id 重排，问题仍检出
        liss = _issue2("wall_free_end", "OLDG", "END-001", (3.0, 4.0),
                       storey="2F")
        lunit = _unit2("楼A", [liss], key="楼A", path="/p/A区/楼A.ifc")
        ingest_batch(ledl, _batch([lunit], "NEW"), sla_hours=0)
        tl = ledl.find("COLL-0001")
        check(len(ledl.tickets) == 1, "历史旧指纹工单迁移而非新建")
        check(tl.fingerprint != legacy_fp
              and legacy_fp in tl.fingerprint_aliases,
              "旧指纹记入别名，主键迁移到稳定指纹")
        check(tl.status == "fixed" and tl.fixed_note == "历史整改",
              "迁移保留原状态与整改记录")
        check(any(h["action"] == "fp_migrate" for h in tl.history),
              "指纹迁移写 fp_migrate 留痕")
        # 再纳管一次，主键稳定不重复迁移
        ingest_batch(ledl, _batch([lunit], "NEW"), sla_hours=0)
        check(len(ledl.tickets) == 1, "迁移后再次纳管主键稳定")

    # --------------------------------------- 批量审查端到端纳管 ----
    try:
        from tools.make_sample_coordination import make_coordination_sample
        from ifc_audit.batch import run_batch_with_config
        with tempfile.TemporaryDirectory() as td:
            sample = os.path.join(td, "models")
            make_coordination_sample(sample)
            history = os.path.join(td, "history")
            batch = run_batch_with_config(
                [sample], project="协同测试", gate_profile="none",
                run_coordination_check=True,
                coord_owners={"struct": "张结", "mep": "李机", "arch": "王设"},
                history_dir=history, progress=None)
            ledger = CollabLedger(project="协同测试")
            tickets = ingest_batch(ledger, batch, sla_hours=72.0)
            coord_n = sum(1 for t in tickets if t.source == SOURCE_COORD)
            check(coord_n == 4, f"4 条协同问题纳入闭环台账（实际 {coord_n}）")
            # 协同工单指纹合单：再来一次不新增
            ledger_path = collab.default_ledger_path(history, "协同测试")
            ledger.save(ledger_path)
            ledger2 = CollabLedger.load_or_new(ledger_path, "协同测试")
            before = len(ledger2.tickets)
            ingest_batch(ledger2, batch, sla_hours=72.0)
            check(len(ledger2.tickets) == before,
                  "同一批模型重复纳管按指纹合单，不新增工单")
            check(all(r.file_path
                      for t in ledger2.tickets.values()
                      if t.source == SOURCE_COORD
                      for r in t.refs if r.unit),
                  "协同工单跨模型定位带文件路径")
    except Exception as exc:  # noqa: BLE001
        check(False, f"批量审查端到端纳管异常：{type(exc).__name__}: {exc}")

    print()
    if failures:
        print(f"{len(failures)} 项测试失败：")
        for m in failures:
            print("  - " + m)
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
