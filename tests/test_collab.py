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
    upsert_user, build_writeback,
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
