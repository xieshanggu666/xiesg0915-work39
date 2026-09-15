"""多模型批量核查 / 门禁 / 趋势的端到端测试（不依赖 pytest，可直接运行）。

用法::

    python tests/test_batch.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_sample_ifc import make_sample  # noqa: E402
from ifc_audit.batch import (  # noqa: E402
    run_batch, run_batch_with_config, attach_trend, save_batch_snapshot,
    load_project_history, discover_ifc_files, unique_unit_names,
)
from ifc_audit.gate import (  # noqa: E402
    for_gate_profile, resolve_gate, parse_gate_set_items, GateConfigError,
    gate_config_template,
)
from ifc_audit import batch_report  # noqa: E402


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        # ---- 准备两个单体文件（同目录批量纳入）----
        d1 = os.path.join(td, "1号楼.ifc")
        d2 = os.path.join(td, "2号楼.ifc")
        make_sample(d1)
        make_sample(d2)

        files = discover_ifc_files([td])
        check(len(files) == 2, f"目录展开为 2 个 IFC（实际 {len(files)}）")

        # ---- 默认门禁：样例含错误级问题，应阻断 ----
        batch = run_batch_with_config([td], project="测试项目", label="v1",
                                      gate_profile="default")
        check(len(batch.units) == 2, f"聚合 2 个单体（实际 {len(batch.units)}）")
        check(all(u.ok for u in batch.units), "两个单体均核查成功")
        t = batch.totals
        check(t["errors"] >= 2, f"项目错误数 ≥2（实际 {t['errors']}）")
        check(abs(t["total_net_area"] - 175.0) < 1e-6,
              f"项目净面积=175 m²（实际 {t['total_net_area']}）")
        check(not batch.gate_passed, "默认门禁下错误问题阻断放行")
        failed_rules = [r for r in batch.gate_results if not r.passed]
        check(any(r.key == "unit_max_errors" for r in failed_rules),
              "失败项含“单体错误数上限”规则")
        check(any(r.scope == "1号楼" for r in failed_rules),
              "失败规则定位到具体单体")

        # ---- 楼层聚合：样例只有 1F ----
        storeys = {(s.unit, s.storey) for s in batch.storeys}
        check(storeys == {("1号楼", "1F"), ("2号楼", "1F")},
              f"单体×楼层聚合正确（实际 {storeys}）")
        s1 = next(s for s in batch.storeys if s.unit == "1号楼")
        check(s1.rooms == 3 and s1.net_area == 87.5,
              f"1号楼/1F 房间3、净面积87.5（实际 {s1.rooms}/{s1.net_area}）")
        check(s1.opening_doors == 4 and s1.opening_windows == 3,
              f"楼层门窗数 4门3窗（实际 {s1.opening_doors}/{s1.opening_windows}）")
        check(s1.opening_unassigned == 2 and s1.opening_anomaly == 1,
              "楼层未归属2、尺寸异常1")

        # ---- 宽松门禁：小样本占比/密度规则偏严，关闭占比与密度规则后放行 ----
        loose = run_batch_with_config(
            [td], project="测试项目", gate_profile="loose",
            gate_overrides={
                "unit_max_open_rooms_pct": -1,
                "unit_max_size_anomaly_pct": -1,
                "unit_max_errors_per_1000m2": -1,
            })
        check(loose.gate_passed, "宽松预设下样例准予放行")

        # ---- none 预设：不阻断 ----
        none_b = run_batch_with_config([td], project="测试项目",
                                       gate_profile="none")
        check(none_b.gate_passed and none_b.gate["enabled"] is False,
              "none 预设门禁关闭且不阻断")

        # ---- 命令行单项覆盖门禁：放宽错误/占比上限后放行 ----
        b2 = run_batch_with_config(
            [td], project="测试项目",
            gate_overrides=parse_gate_set_items(
                ["unit_max_errors=99", "unit_max_dup_groups=99",
                 "project_max_errors=99",
                 "unit_max_open_rooms_pct=-1",
                 "unit_max_size_anomaly_pct=-1"]))
        check(b2.gate_passed, "单项覆盖门禁后放行")

        # ---- 严格预设：警告也受控 ----
        strict = run_batch_with_config([td], project="测试项目",
                                       gate_profile="strict")
        check(not strict.gate_passed, "严格预设阻断")
        strict_fail_keys = {r.key for r in strict.gate_results if not r.passed}
        check({"unit_max_errors", "unit_max_open_rooms",
               "unit_max_unassigned_openings"} & strict_fail_keys,
              f"严格预设命中错误/不闭合/未归属规则（{strict_fail_keys}）")

        # ---- 批次导出：Excel / JSON / 看板 PNG ----
        xlsx = batch_report.export_batch_excel(
            batch, os.path.join(td, "b.xlsx"))
        jpath = batch_report.export_batch_json(
            batch, os.path.join(td, "b.json"))
        png = batch_report.export_dashboard(
            batch, os.path.join(td, "dash.png"))
        check(all(os.path.getsize(p) > 100 for p in (xlsx, jpath, png)),
              "批次 Excel/JSON/看板 PNG 非空")
        with open(jpath, encoding="utf-8") as f:
            dump = json.load(f)
        check(dump["gate_passed"] is False and dump["n_files"] == 2,
              "批次 JSON 结构正确")
        check({u["name"] for u in dump["units"]} == {"1号楼", "2号楼"},
              "JSON 含两个单体结果")
        check(any(s["storey"] == "1F" for s in dump["storeys"]),
              "JSON 含楼层聚合行")

        # 验证 Excel 工作表
        from openpyxl import load_workbook
        wb = load_workbook(xlsx)
        expected_sheets = {
            "批次概览", "放行判定", "单体汇总", "楼层汇总",
            "问题分布", "门窗规格汇总", "趋势对比", "阈值与门禁"}
        check(expected_sheets <= set(wb.sheetnames),
              f"Excel 8 张表齐全（实际 {wb.sheetnames}）")
        ws = wb["问题分布"]
        check(ws.max_row == 4,  # 表头 + 2 单体 + 合计
              f"问题分布表 4 行（实际 {ws.max_row}）")

        # ---- 历史留存与趋势：连续两批 ----
        hist = os.path.join(td, "history")
        attach_trend(batch, hist)
        save_batch_snapshot(batch, hist)
        first = load_project_history(hist, "测试项目")
        check(len(first) == 1, "首批留存 1 条快照")
        check(batch.trend["has_previous"] is False, "首批无趋势对比")

        # 第二批：放宽占比/密度规则后放行，指标与首批一致
        _loose_small = {
            "unit_max_open_rooms_pct": -1,
            "unit_max_size_anomaly_pct": -1,
            "unit_max_errors_per_1000m2": -1,
        }
        batch2 = run_batch_with_config([td], project="测试项目",
                                       label="v2", gate_profile="loose",
                                       gate_overrides=_loose_small)
        attach_trend(batch2, hist)
        check(batch2.trend["has_previous"] is True, "第二批识别到上一批次")
        check(batch2.trend["previous_batch_id"] == batch.batch_id,
              "趋势对比锚定首批")
        d = batch2.trend["deltas"]["errors"]
        check(d["old"] == d["new"] and d["delta"] == 0,
              "相同模型错误数变化为 0")
        names = {x["unit"] for x in batch2.trend["unit_delta"]}
        check(names == {"1号楼", "2号楼"}, "趋势含两个单体的对比行")
        save_batch_snapshot(batch2, hist)
        check(len(load_project_history(hist, "测试项目")) == 2,
              "两批快照均留存")
        check(len(batch2.trend["history"]) == 2, "看板趋势序列含 2 个点")

        # ---- 第三批：移除一个单体 -> 趋势应报缺失 ----
        only1 = os.path.join(td, "only")
        os.makedirs(only1)
        os.rename(d2, os.path.join(only1, "2号楼.ifc"))
        batch3 = run_batch_with_config([d1], project="测试项目",
                                       label="v3", gate_profile="loose",
                                       gate_overrides=_loose_small)
        attach_trend(batch3, hist)
        check("2号楼" in batch3.trend["units_missing"],
              "缺失单体进入趋势对比")
        png3 = batch_report.export_dashboard(
            batch3, os.path.join(td, "dash3.png"))
        check(os.path.getsize(png3) > 100, "3 批趋势下看板可渲染")
        trend_png = batch_report.export_trend_chart(
            batch3, os.path.join(td, "trend.png"))
        check(os.path.getsize(trend_png) > 100, "趋势图可渲染")

        # ---- 核查失败文件：不拖垮整批，默认门禁阻断 ----
        bad = os.path.join(td, "bad.ifc")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("这不是合法IFC")
        fb = run_batch_with_config([d1, bad], project="测试项目",
                                   gate_profile="loose")
        bad_unit = next(u for u in fb.units if u.name == "bad")
        check(not bad_unit.ok and bad_unit.error,
              f"坏文件标记为失败单体（{bad_unit.error[:40]}）")
        check(fb.totals["units_failed"] == 1, "项目汇总记录 1 个失败文件")
        check(not fb.gate_passed, "loose 预设下失败文件仍阻断（allow_failed_files=False）")
        fail_rules = [r for r in fb.gate_results if not r.passed]
        check(any(r.key == "allow_failed_files" for r in fail_rules),
              "失败文件命中 allow_failed_files 规则")
        # 显式允许失败文件
        fb2 = run_batch_with_config(
            [d1, bad], project="测试项目",
            gate_overrides={"allow_failed_files": True})
        # d1 样例在 default 下有错误，规则会因错误数阻断——只验证失败文件规则不再出现
        check(all(r.key != "allow_failed_files" for r in fb2.gate_results
                  if not r.passed),
              "allow_failed_files=true 时不再因失败文件阻断")

        # ---- min_units：只传 1 个文件时若要求 2 个单体则阻断 ----
        gb = run_batch_with_config(
            [d1], project="测试项目",
            gate_overrides={"min_units": 2})
        check(not gb.gate_passed
              and any(not r.passed and r.key == "min_units"
                      for r in gb.gate_results),
              "纳入单体不足 min_units 时阻断（防漏传）")

        # ---- 门禁配置解析 ----
        ok_gate, _ = resolve_gate("default", overrides={
            "unit_max_warnings": 20, "unit_max_open_rooms_pct": 3})
        check(ok_gate.unit_max_warnings == 20
              and abs(ok_gate.unit_max_open_rooms_pct - 0.03) < 1e-12,
              "门禁单项覆盖解析（整数/百分比）")
        try:
            resolve_gate("default", overrides={"unit_max_warnings": "abc"})
            check(False, "非法门禁值应报错")
        except GateConfigError:
            check(True, "非法门禁值报 GateConfigError")
        try:
            parse_gate_set_items(["no_such_key=1"])
            check(False, "未知门禁键应报错")
        except GateConfigError:
            check(True, "未知门禁键报 GateConfigError")
        tpl = gate_config_template("strict")
        check(tpl["profile"] == "strict" and "_说明" in tpl,
              "门禁配置模板带中文说明")

        # ---- 错误密度门禁（每千 m²）----
        gd = run_batch_with_config(
            [d1], project="测试项目",
            gate_overrides={"unit_max_errors_per_1000m2": 1.0})
        # 单体约 44.5 m² 有 ≥3 错误 -> 密度远超 1.0/千m²
        dens_fails = [r for r in gd.gate_results
                      if not r.passed and r.key == "unit_max_errors_per_1000m2"]
        check(bool(dens_fails), "错误密度规则按每千平方米评估")

        # ---- 不同目录下的同名 IFC：单体名消歧，结论不串、报告不覆盖 ----
        dup_dir_a = os.path.join(td, "A区")
        dup_dir_b = os.path.join(td, "B区")
        os.makedirs(dup_dir_a)
        os.makedirs(dup_dir_b)
        same_a = os.path.join(dup_dir_a, "楼A.ifc")
        same_b = os.path.join(dup_dir_b, "楼A.ifc")
        make_sample(same_a)
        make_sample(same_b)

        dup_batch = run_batch_with_config(
            [dup_dir_a, dup_dir_b], project="重名测试", gate_profile="default")
        dup_names = [u.name for u in dup_batch.units]
        check(len(dup_names) == 2 and len(set(dup_names)) == 2,
              f"同名文件单体名唯一（实际 {dup_names}）")
        check(set(dup_names) == {"A区-楼A", "B区-楼A"},
              f"单体名带父目录消歧（实际 {sorted(dup_names)}）")
        # 文件路径各自保留，没有互相覆盖
        paths = {u.name: u.file_path for u in dup_batch.units}
        check(os.path.dirname(paths["A区-楼A"]).endswith("A区")
              and os.path.dirname(paths["B区-楼A"]).endswith("B区"),
              "两个单体仍分别指向各自目录的文件")
        # 门禁判定按消歧后的单体名分别成行，不互相合并
        unit_scopes = {r.scope for r in dup_batch.gate_results
                       if r.level == "unit"}
        check(unit_scopes == {"A区-楼A", "B区-楼A"},
              f"门禁逐条判定的单体范围互不相同（实际 {sorted(unit_scopes)}）")
        # 单体汇总每行的结论只能命中自己那一行（旧 bug 会两行都取到同组判定）
        from openpyxl import load_workbook
        dup_xlsx = batch_report.export_batch_excel(
            dup_batch, os.path.join(td, "dup.xlsx"))
        wb_dup = load_workbook(dup_xlsx)
        ws_dup = wb_dup["单体汇总"]
        name_col = [(r[0].value, r[20].value) for r in ws_dup.iter_rows(
            min_row=2, max_row=3)]
        check({n for n, _ in name_col} == {"A区-楼A", "B区-楼A"},
              f"单体汇总两行名称独立（实际 {name_col}）")
        check(all(v == "不通过" for _, v in name_col),
              f"两行结论各自独立判定，不串结论（实际 {name_col}）")
        # 导出单体报告：同名文件不能互相覆盖
        from ifc_audit.cli import _export_unit_reports
        dup_out = os.path.join(td, "dup_out")
        _export_unit_reports(dup_batch, dup_out, True, False)
        for fn in ("A区-楼A_核查报告.xlsx", "B区-楼A_核查报告.xlsx",
                   "A区-楼A_标注平面图.png", "B区-楼A_标注平面图.png"):
            check(os.path.exists(os.path.join(dup_out, "单体报告", fn)),
                  f"单体报告未互相覆盖：{fn}")

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
