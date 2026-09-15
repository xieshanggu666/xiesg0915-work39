"""规则版本切换（试算 → 确认 → 联动）测试（不依赖 pytest，可直接运行）。

用法::

    python tests/test_rule_switch.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_sample_ifc import make_sample  # noqa: E402
from ifc_audit.batch import (  # noqa: E402
    run_batch_with_rule_pack, attach_trend, save_batch_snapshot,
    load_project_history,
)
from ifc_audit.rule_packs import (  # noqa: E402
    RulePackLibrary, new_draft, materialize, CHECK_DUPLICATE,
)
from ifc_audit.rule_switch import (  # noqa: E402
    run_rule_switch_compare, confirm_rule_switch, list_switch_records,
    load_project_rule_binding, RuleSwitchError,
)
from ifc_audit.cli import main as cli_main  # noqa: E402


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        ifc_dir = os.path.join(td, "ifc")
        lib_dir = os.path.join(td, "lib")
        history_dir = os.path.join(td, "history")
        out_dir = os.path.join(td, "out")
        os.makedirs(ifc_dir)
        make_sample(os.path.join(ifc_dir, "1号楼.ifc"))
        make_sample(os.path.join(ifc_dir, "2号楼.ifc"))
        project = "切换测试项目"

        # ---- 规则包 v1.0.0（全核查项，默认口径）----
        lib = RulePackLibrary(lib_dir)
        c1 = new_draft("住宅审查", projects=[project])
        lib.save_draft(c1)
        p1 = lib.publish("住宅审查", "1.0.0", published_by="admin")

        # ---- 原始批次：按 v1.0.0 核查并留存快照 ----
        mat1 = materialize(p1)
        b1 = run_batch_with_rule_pack([ifc_dir], mat1, project=project,
                                      label="v1提模", history_dir="")
        attach_trend(b1, history_dir)
        save_batch_snapshot(b1, history_dir)
        original_batch_id = b1.batch_id
        old_dup_issues = sum(
            1 for u in b1.units
            for i in u.model.issues if i.kind == "duplicate_element")
        check(old_dup_issues >= 2,
              f"样例含重复构件问题（实际 {old_dup_issues} 条）")

        # ---- 规则包 v1.1.0：关闭重复构件核查 + 收紧门宽下限 + 收紧门禁 ----
        c2 = new_draft("住宅审查", projects=[project])
        c2.checks[CHECK_DUPLICATE] = False
        c2.thresholds = {"door_min_width_mm": 1000}
        c2.gate_rules = {"unit_max_unassigned_openings": 0}
        lib.save_draft(c2)
        p2 = lib.publish("住宅审查", "1.1.0", published_by="admin")
        mat2 = materialize(p2)

        # ---- 试算：同一批模型按新旧规则包重算 ----
        history = load_project_history(history_dir, project)
        check(len(history) == 1 and
              history[0]["rule_pack"]["id"] == "住宅审查@1.0.0",
              "原始批次快照留存且记录规则包 v1.0.0")
        record = run_rule_switch_compare(
            project, history[-1], mat1, mat2, history_dir, by="张工")
        check(record["status"] == "pending", "试算记录初始为待确认")
        check(record["old_rule_pack_id"] == "住宅审查@1.0.0" and
              record["new_rule_pack_id"] == "住宅审查@1.1.0",
              "试算记录标注新旧规则包版本")
        check(os.path.isfile(record["record_path"]), "试算记录已归档落盘")

        # 重算一致性：模型未动，旧规则重算应与原批次一致
        check(record["consistency"]["consistent"],
              "旧规则重算与原始批次一致（模型未变）")

        # 规则调整影响：重复构件问题全部消除（核查项关闭）
        diff = record["issue_diff"]
        dup_removed = diff["by_check"].get(CHECK_DUPLICATE, {}).get("removed", 0)
        check(dup_removed == old_dup_issues,
              f"关闭重复构件核查消除 {old_dup_issues} 条问题（实际 {dup_removed}）")
        check(diff["totals"]["removed"] >= old_dup_issues,
              "消除总量覆盖重复构件问题")
        # 门宽下限收紧到 1000mm：900mm 外门变为尺寸异常 → 新增问题
        check(diff["totals"]["added"] >= 1,
              f"收紧门宽下限带来新增问题（实际 {diff['totals']['added']} 条）")
        check(diff["totals"]["kept"] > 0, "其余问题保留")
        check(record["metric_deltas"]["duplicate_groups"]["new"] == 0,
              "新规则下重复构件组统计归零")

        # 规则包内容差异：核查项开关 / 阈值 / 门禁逐项列出
        pc = record["pack_compare"]
        check(any(c["check"] == CHECK_DUPLICATE and c["old"] and not c["new"]
                  for c in pc["checks_toggled"]),
              "内容差异列出重复构件核查项关闭")
        check(any(r["key"] == "door_min_width_mm" and r["new"] == 1000
                  for r in pc["thresholds"]),
              "内容差异列出门宽阈值变化（用户面 mm 口径）")
        check(any(r["key"] == "unit_max_unassigned_openings"
                  for r in pc["gate_rules"]),
              "内容差异列出门禁规则变化")

        # 门禁联动预览：未归属门窗上限收紧为 0 → 新增未通过规则；
        # 重复构件核查关闭 → 相关门禁不再判定（转为通过 / 不参与）
        gc = record["gate_compare"]
        check(not gc["new_passed"], "新规则口径下门禁阻断")
        check(any(r["key"] == "unit_max_unassigned_openings"
                  for r in gc["newly_failed"]),
              "新增未通过项含未归属门窗上限")
        check(any(r["key"] == "unit_max_dup_groups"
                  for r in gc["newly_passed"]),
              "重复构件门禁随核查项关闭不再阻断")

        # 试算不污染批次历史与趋势
        check(len(load_project_history(history_dir, project)) == 1,
              "试算不写入批次历史（仍 1 个批次）")

        # ---- 确认切换：联动规则选择 / 门禁 / 趋势 ----
        rec2 = confirm_rule_switch(history_dir, project, by="李工")
        check(rec2["status"] == "confirmed" and rec2["confirmed_by"] == "李工",
              "确认后记录状态为已确认")
        check(rec2["switch_id"] == record["switch_id"],
              "默认确认最新待确认记录")

        binding = load_project_rule_binding(history_dir, project)
        check(binding and binding["rule_pack_id"] == "住宅审查@1.1.0",
              "项目规则选择联动更新为 v1.1.0")
        check(binding["switched_from"] == "住宅审查@1.0.0",
              "绑定记录保留原规则版本供追溯")

        history2 = load_project_history(history_dir, project)
        check(len(history2) == 2, "确认后批次历史增加规则切换基线")
        baseline = history2[-1]
        check(baseline.get("rule_switch", {}).get("new_rule_pack_id")
              == "住宅审查@1.1.0", "基线快照带规则切换标记")
        check(baseline["rule_pack"]["id"] == "住宅审查@1.1.0",
              "基线批次按新规则包口径")
        # 原始批次与原规则版本保留不变
        orig = history2[0]
        check(orig["batch_id"] == original_batch_id and
              orig["rule_pack"]["id"] == "住宅审查@1.0.0" and
              not orig.get("rule_switch"),
              "原始批次及原规则版本保留不变")
        check(lib.load_published("住宅审查", "1.0.0").content_hash
              == p1.content_hash, "原规则包 v1.0.0 快照仍可载入")

        # 重复确认应报错
        try:
            confirm_rule_switch(history_dir, project,
                                switch_id=record["switch_id"])
            check(False, "重复确认应抛出 RuleSwitchError")
        except RuleSwitchError:
            check(True, "重复确认被拒绝")

        # ---- 趋势联动：下一批次与重算基线对比，增量=模型整改 ----
        b2 = run_batch_with_rule_pack([ifc_dir], mat2, project=project,
                                      label="v2复核", history_dir="")
        attach_trend(b2, history_dir)
        tr = b2.trend
        check(tr.get("previous_is_rule_switch_baseline"),
              "趋势对比基线识别为规则切换基线")
        check(tr["previous_batch_id"] == baseline["batch_id"],
              "趋势与规则切换基线对比")
        check(tr["deltas"]["issues"]["delta"] == 0 and
              tr["deltas"]["errors"]["delta"] == 0,
              "模型未整改时相对基线增量为 0（规则调整影响已被基线吸收）")

        # ---- 延迟确认：试算后、确认前又跑了旧口径批次 ----
        project3 = "延迟确认项目"
        c5 = new_draft("延迟包", projects=[project3])
        lib.save_draft(c5)
        lib.publish("延迟包", "1.0.0")
        c6 = new_draft("延迟包", projects=[project3])
        c6.checks[CHECK_DUPLICATE] = False
        lib.save_draft(c6)
        lib.publish("延迟包", "1.1.0")
        from ifc_audit.rule_packs import load_published_file  # noqa: F401
        m_old = materialize(lib.load_published("延迟包", "1.0.0"))
        m_new = materialize(lib.load_published("延迟包", "1.1.0"))

        b_old1 = run_batch_with_rule_pack([ifc_dir], m_old, project=project3,
                                          label="v1提模", history_dir="")
        attach_trend(b_old1, history_dir)
        save_batch_snapshot(b_old1, history_dir)

        # 试算（此时不重算基线入历史）
        rec3 = run_rule_switch_compare(
            project3, load_project_history(history_dir, project3)[-1],
            m_old, m_new, history_dir)

        # 延迟确认：确认前又跑了两个旧口径批次
        for tag in ("v2提模", "v3提模"):
            b = run_batch_with_rule_pack([ifc_dir], m_old, project=project3,
                                         label=tag, history_dir="")
            attach_trend(b, history_dir)
            save_batch_snapshot(b, history_dir)
        n_hist_before = len(load_project_history(history_dir, project3))
        check(n_hist_before == 3, f"确认前共 3 个旧口径批次（实际 {n_hist_before}）")

        rec3 = confirm_rule_switch(history_dir, project3,
                                   switch_id=rec3["switch_id"], by="张工")
        check(len(rec3.get("intervening_batches", [])) == 2,
              "确认记录识别出 2 个延迟期间的干扰批次")
        hist3 = load_project_history(history_dir, project3)
        check(len(hist3) == 4, "确认后历史含 3 个旧批次 + 1 个基线（均保留）")
        baseline3 = hist3[-1]
        check(baseline3.get("rule_switch", {}).get("new_rule_pack_id")
              == "延迟包@1.1.0", "基线排在时间线最后（确认时刻生效）")
        check(baseline3["created_at"] == rec3["confirmed_at"],
              "基线时间线位置取确认时刻而非重算时刻")
        check(baseline3["rule_switch"].get("recheck_created_at")
              and baseline3["rule_switch"]["recheck_created_at"]
              <= rec3["confirmed_at"],
              "重算时刻保留在基线元数据中供追溯")

        # 延迟确认后：新口径批次趋势锚定基线，而非最后一个旧口径批次
        b_new = run_batch_with_rule_pack([ifc_dir], m_new, project=project3,
                                         label="v4复核", history_dir="")
        attach_trend(b_new, history_dir)
        check(b_new.trend["previous_batch_id"] == baseline3["batch_id"],
              "延迟确认后趋势锚定规则切换基线（而非旧口径批次）")
        check(b_new.trend.get("previous_is_rule_switch_baseline"),
              "锚点标记为规则切换基线")
        check(b_new.trend["deltas"]["issues"]["delta"] == 0,
              "同模型同口径相对基线增量为 0（不混入旧口径变化）")

        # 补录/乱序防护：基线之后又出现一条旧口径快照（更晚时间戳）
        from ifc_audit.batch import save_snapshot_dict, select_trend_anchor
        stale = dict(hist3[0])          # 旧口径（延迟包@1.0.0）批次
        stale["batch_id"] = "99990101-000000-000"
        stale["created_at"] = "9999-01-01T00:00:00"
        stale["label"] = "补录旧批次"
        save_snapshot_dict(stale, history_dir)
        hist4 = load_project_history(history_dir, project3)
        check(hist4[-1]["batch_id"] == "99990101-000000-000",
              "补录的旧口径快照排在时间线最后")
        anchor = select_trend_anchor("延迟包@1.1.0", hist4)
        check(anchor and anchor["batch_id"] == baseline3["batch_id"],
              "锚点选择跳过更晚的旧口径快照，仍锁定同口径基线")
        b_new2 = run_batch_with_rule_pack([ifc_dir], m_new, project=project3,
                                          label="v5复核", history_dir="")
        attach_trend(b_new2, history_dir)
        check(b_new2.trend["previous_batch_id"] == baseline3["batch_id"],
              "补录旧口径快照后趋势仍锚定基线")
        check(len(load_project_history(history_dir, project3)) == 5,
              "全部历史批次（含补录）保留供追溯")
        # 无规则包口径的批次不受锚点逻辑影响（取时间线最后）
        check(select_trend_anchor("", hist4)["batch_id"]
              == "99990101-000000-000",
              "无规则包批次仍取时间线最后批次")

        # ---- CLI 端到端 ----
        # 另起项目走 CLI：compare → confirm → list/show → batch 自动用绑定版本
        project2 = "CLI切换项目"
        c3 = new_draft("CLI包", projects=[project2])
        lib.save_draft(c3)
        lib.publish("CLI包", "1.0.0")
        rc = cli_main(["batch", ifc_dir, "--project", project2,
                       "--rule-lib", lib_dir, "--history", history_dir,
                       "-o", out_dir, "--no-unit-reports", "-q"])
        check(rc == 3, "CLI 原始批次按 v1.0.0 核查（门禁阻断退出码 3）")
        c4 = new_draft("CLI包", projects=[project2])
        c4.checks[CHECK_DUPLICATE] = False
        lib.save_draft(c4)
        lib.publish("CLI包", "1.1.0")

        rc = cli_main(["ruleswitch", "compare", "--project", project2,
                       "--to", "CLI包@1.1.0",
                       "--rule-lib", lib_dir, "--history", history_dir,
                       "-o", out_dir, "-q"])
        check(rc == 0, "CLI ruleswitch compare 成功")
        rc = cli_main(["ruleswitch", "list", "--project", project2,
                       "--history", history_dir])
        check(rc == 0, "CLI ruleswitch list 成功")
        records = list_switch_records(history_dir, project2)
        check(len(records) == 1 and records[0]["status"] == "pending",
              "CLI 试算记录待确认")
        rc = cli_main(["ruleswitch", "show", "--project", project2,
                       "--history", history_dir,
                       records[0]["switch_id"]])
        check(rc == 0, "CLI ruleswitch show 成功")
        rc = cli_main(["ruleswitch", "confirm", "--project", project2,
                       "--history", history_dir, "--by", "王工"])
        check(rc == 0, "CLI ruleswitch confirm 成功")

        # 确认后 batch 自动按绑定的新版本核查（不再自动选最新/其它版本）
        rc = cli_main(["batch", ifc_dir, "--project", project2,
                       "--rule-lib", lib_dir, "--history", history_dir,
                       "-o", out_dir, "--no-unit-reports", "-q"])
        check(rc == 3, "CLI 批次按绑定版本核查（退出码 3）")
        jsons = sorted(glob.glob(os.path.join(
            out_dir, f"{project2}_批次结果_*.json")), key=os.path.getmtime)
        with open(jsons[-1], "r", encoding="utf-8") as f:
            last_batch = json.load(f)
        check(last_batch["rule_pack"]["id"] == "CLI包@1.1.0",
              "确认后 batch 联动使用绑定的新规则包版本")
        check(last_batch["trend"].get("previous_is_rule_switch_baseline"),
              "批次 JSON 趋势标注规则切换基线")

    print()
    if failures:
        print(f"共 {len(failures)} 项失败：")
        for m in failures:
            print(" -", m)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
