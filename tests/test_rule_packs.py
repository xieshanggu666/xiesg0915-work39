"""企业审查规则库（规则包）测试（不依赖 pytest，可直接运行）。

用法::

    python tests/test_rule_packs.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_sample_ifc import make_sample  # noqa: E402
from ifc_audit.pipeline import audit_ifc  # noqa: E402
from ifc_audit.batch import run_batch_with_rule_pack  # noqa: E402
from ifc_audit.rule_packs import (  # noqa: E402
    RulePackLibrary, RulePackError, RulePackContent, new_draft, materialize,
    load_published_file, write_draft_template, parse_version,
    disabled_gate_keys, enabled_checks_from_kinds,
    CHECKS, CHECK_DUPLICATE, CHECK_ROOM_AREA,
    CHECK_OPENING_ASSIGN, CHECK_ISSUE_KINDS,
)
from ifc_audit.cli import main as cli_main  # noqa: E402


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        lib_dir = os.path.join(td, "lib")
        ifc_dir = os.path.join(td, "ifc")
        os.makedirs(ifc_dir)
        make_sample(os.path.join(ifc_dir, "1号楼.ifc"))
        make_sample(os.path.join(ifc_dir, "2号楼.ifc"))

        lib = RulePackLibrary(lib_dir)
        lib.init()
        check(os.path.isdir(os.path.join(lib_dir, "drafts")), "规则库目录初始化")

        # ---- 1) 草稿 / 发布 / 版本管理 ----
        c = new_draft("住宅施工图审查", description="施工图阶段标准包",
                      projects=["花园小区一期"],
                      stages=["construction_drawing"])
        c.thresholds = {"gap_min_len_mm": 50}
        c.gate_rules = {"unit_max_warnings": 20}
        lib.save_draft(c)
        pack = lib.publish("住宅施工图审查", "1.0.0", published_by="admin")
        check(pack.id == "住宅施工图审查@1.0.0", "发布后完整标识 名称@版本")
        check(len(pack.content_hash) == 16, "发布快照带内容指纹")
        check(os.path.exists(pack.path), "发布快照落盘且不可变存放")

        # 草稿改动不影响已发布版本
        c2 = lib.load_draft("住宅施工图审查")
        c2.thresholds["gap_min_len_mm"] = 90
        lib.save_draft(c2)
        pack_reloaded = lib.load_published("住宅施工图审查", "1.0.0")
        check(pack_reloaded.thresholds["gap_min_len_mm"] == 50,
              "草稿再编辑不改变已发布版本（不可变）")

        # 同名同版本拒绝重复发布
        try:
            lib.publish("住宅施工图审查", "1.0.0")
            check(False, "同名同版本重复发布应报错")
        except RulePackError:
            check(True, "同名同版本拒绝重复发布")

        # 新版本
        v2 = lib.publish("住宅施工图审查", "1.1.0", published_by="admin")
        check(lib.load_published("住宅施工图审查").version == "1.1.0",
              "不指定版本时取最新版本")
        check(parse_version("1.10.0") > parse_version("1.2.0"),
              "语义化版本按数字比较（1.10 > 1.2）")

        # 指纹防篡改：手改发布快照 -> 载入报错（检查后恢复）
        with open(v2.path, "r", encoding="utf-8") as f:
            original_text = f.read()
        tampered = json.loads(original_text)
        tampered["thresholds"]["gap_min_len_mm"] = 1
        with open(v2.path, "w", encoding="utf-8") as f:
            json.dump(tampered, f, ensure_ascii=False)
        try:
            lib.load_published("住宅施工图审查", "1.1.0")
            check(False, "被篡改的快照应报错")
        except RulePackError:
            check(True, "内容指纹不一致时拒绝载入（防篡改）")
        with open(v2.path, "w", encoding="utf-8") as f:
            f.write(original_text)

        # ---- 2) 适用范围与自动选择 ----
        c_strict = new_draft("竣工审查包", projects=["花园小区一期"],
                             stages=["completion"],
                             threshold_profile="strict",
                             gate_profile="strict")
        lib.save_draft(c_strict)
        lib.publish("竣工审查包", "1.0.0")
        c_any = new_draft("公司通用包")   # 适用全部项目 / 全部阶段
        lib.save_draft(c_any)
        lib.publish("公司通用包", "1.0.0")

        sel = lib.select_for("花园小区一期", "completion")
        check(sel.name == "竣工审查包",
              "项目+阶段精确匹配优先于全包（选竣工包）")
        sel2 = lib.select_for("花园小区一期", "scheme")
        check(sel2.name == "公司通用包",
              "阶段不命中任何专用包时回退全阶段包")
        sel2b = lib.select_for("花园小区一期", "construction_drawing")
        check(sel2b.name == "住宅施工图审查",
              "项目+阶段双命中专用包（施工图包）")
        sel3 = lib.select_for("其它项目", "")
        check(sel3.name == "公司通用包",
              "无项目信息时回退全项目全阶段包")

        # 废止版本不参与自动选择
        lib.set_deprecated("竣工审查包", "1.0.0", True)
        sel4 = lib.select_for("花园小区一期", "completion")
        check(sel4.name == "公司通用包", "废止版本不参与自动选择")
        lib.set_deprecated("竣工审查包", "1.0.0", False)

        # 限定项目/阶段的场景包：完全对不上时才应报错
        c_narrow = new_draft("窄范围包", projects=["只此一家"], stages=["scheme"])
        lib.save_draft(c_narrow)
        lib.publish("窄范围包", "1.0.0")
        # 临时把全包移开：直接用全新空库验证无适用包
        empty_lib = RulePackLibrary(os.path.join(td, "empty_lib"))
        empty_lib.init()
        try:
            empty_lib.select_for("任意项目", "completion")
            check(False, "无适用包应报错")
        except RulePackError:
            check(True, "无适用规则包时明确报错")
        # 全项目但限定阶段：未指定阶段时不命中（独立小库验证）
        stage_lib = RulePackLibrary(os.path.join(td, "stage_lib"))
        stage_lib.init()
        sc = new_draft("仅方案包", stages=["scheme"])
        stage_lib.save_draft(sc)
        stage_lib.publish("仅方案包", "1.0.0")
        try:
            stage_lib.select_for("任意项目")
            check(False, "全是阶段限定包、未指定阶段时应报错")
        except RulePackError:
            check(True, "未指定阶段时不匹配阶段限定包")
        # 有公司通用包（全项目全阶段）兜底时，任意项目/阶段都能选到
        check(lib.select_for("不存在的项目", "scheme").name
              in ("公司通用包", "方案专用包", "窄范围包"),
              "查询可被多个包满足时按相关度选择，不报错")

        # ---- 3) 物化：阈值 / 门禁 / 核查项 ----
        pack = lib.load_published("竣工审查包", "1.0.0")
        mat = materialize(pack)
        check(abs(mat.thresholds.gap_min_len - 0.05) < 1e-9,
              "规则包物化 strict 阈值（围护下限 50mm）")
        check(mat.threshold_provenance.rule_pack_id == "竣工审查包@1.0.0",
              "阈值来源记录规则包版本")
        check(mat.gate_provenance.rule_pack_id == "竣工审查包@1.0.0",
              "门禁来源记录规则包版本")
        check(mat.ref.id == "竣工审查包@1.0.0", "RulePackRef 可追溯")
        check(mat.enabled_kinds == set().union(*CHECK_ISSUE_KINDS.values()),
              "核查项全开时启用全部问题种类")

        # 关闭核查项
        c_off = new_draft("精简包", stages=["scheme"],
                          threshold_profile="loose", gate_profile="loose")
        c_off.checks[CHECK_DUPLICATE] = False
        c_off.checks[CHECK_ROOM_AREA] = False
        c_off.checks[CHECK_OPENING_ASSIGN] = False
        lib.save_draft(c_off)
        lib.publish("精简包", "2.3.1")
        mat_off = materialize(lib.load_published("精简包", "2.3.1"))
        check("duplicate_element" not in mat_off.enabled_kinds,
              "关闭重复构件核查后问题种类收窄")
        check(not mat_off.enabled_checks.__contains__(CHECK_DUPLICATE),
              "enabled_checks 反映关闭项")
        dg = disabled_gate_keys(mat_off.enabled_checks)
        check({"unit_max_dup_groups", "project_max_dup_groups"} <= dg,
              "重复构件关闭 -> 重复组门禁不参与")
        check("unit_max_unassigned_openings" in dg,
              "未归属核查关闭 -> 未归属门禁不参与")
        check({"unit_max_errors", "unit_max_warnings"} <= dg,
              "任一核查项关闭 -> 错误/警告总数门禁不参与（口径不完整）")
        check("unit_max_open_rooms_pct" not in dg,
              "未关闭的核查项对应门禁照常参与")
        check(enabled_checks_from_kinds(None) == list(CHECKS),
              "enabled_kinds=None 视为全部启用")

        # ---- 4) 端到端：单模型核查使用规则包 ----
        ifc_path = os.path.join(ifc_dir, "1号楼.ifc")
        model = audit_ifc(ifc_path, thresholds=mat_off.thresholds,
                          provenance=mat_off.threshold_provenance,
                          enabled_kinds=mat_off.enabled_kinds,
                          rule_pack=mat_off.ref)
        kinds = {i.kind for i in model.issues}
        check("duplicate_element" not in kinds, "单模型不再报重复构件")
        check("area_mismatch" not in kinds
              and "area_missing_declared" not in kinds,
              "单模型不再报面积类问题")
        check("opening_unassigned" not in kinds, "单模型不再报未归属门窗")
        check(model.rule_pack.id == "精简包@2.3.1", "模型携带规则包引用")
        # 门窗表仍生成（清单不依赖核查开关）
        check(len(model.opening_schedule) > 0, "关闭核查项不影响门窗表清单")
        # 房间净面积清单仍生成
        check(len(model.rooms) == 3, "关闭面积核查不影响房间净面积清单")
        check(model.duplicate_groups == [], "关闭重复核查时分组为空")

        # 默认核查（无规则包）行为不变
        model_default = audit_ifc(ifc_path)
        kinds_d = {i.kind for i in model_default.issues}
        check("duplicate_element" in kinds_d,
              "不传 enabled_kinds 时重复构件照常核查（向后兼容）")
        check(model_default.rule_pack is None, "默认核查无规则包引用")

        # ---- 5) 端到端：批量核查 + 门禁联动 ----
        batch = run_batch_with_rule_pack([ifc_dir], mat_off,
                                         project="花园小区一期", label="v1")
        check(batch.rule_pack["id"] == "精简包@2.3.1", "批次结果记录规则包版本")
        evaluated = {r.key for r in batch.gate_results}
        check("unit_max_dup_groups" not in evaluated
              and "unit_max_errors" not in evaluated,
              "关闭核查项对应门禁规则不判定")
        check(all(u.rule_pack["id"] == "精简包@2.3.1" for u in batch.units),
              "每个单体结果都带规则包版本（可追溯到单体）")
        d = batch.to_dict()
        check(d["rule_pack"]["content_hash"] == mat_off.ref.content_hash,
              "批次 JSON 含规则包指纹")
        check(CHECK_DUPLICATE not in d["enabled_checks"],
              "批次 JSON 记录启用核查项")

        # 全开规则包时全部门禁照常判定
        batch_full = run_batch_with_rule_pack([ifc_dir], mat,
                                              project="花园小区一期")
        keys_full = {r.key for r in batch_full.gate_results}
        check("unit_max_errors" in keys_full
              and "unit_max_dup_groups" in keys_full,
              "核查全开时全部门禁规则参与判定")

        # ---- 6) 草稿校验 ----
        bad = new_draft("坏包")
        bad.checks = {c: False for c in CHECKS}
        try:
            lib.save_draft(bad)
            check(False, "全部核查项关闭应拒绝保存")
        except RulePackError:
            check(True, "至少启用一个核查项")

        bad2 = new_draft("坏包2")
        bad2.thresholds = {"not_a_key": 1}
        try:
            lib.save_draft(bad2)
            check(False, "未知阈值键应拒绝保存")
        except RulePackError:
            check(True, "草稿保存时校验阈值键与取值")

        bad3 = new_draft("坏包3")
        bad3.stages = ["not_a_stage"]
        try:
            RulePackContent.from_dict(bad3.to_dict())
            check(False, "未知阶段应拒绝")
        except RulePackError:
            check(True, "未知阶段标识报错")

        # 名称合法性
        for bad_name in ("", "a/b", ".."):
            try:
                RulePackLibrary._validate_name(bad_name)
                check(False, f"非法名称应报错：{bad_name!r}")
            except RulePackError:
                check(True, f"规则包名称校验：{bad_name!r}")

        # ---- 7) 库外快照文件载入（分发场景）----
        ext_path = os.path.join(td, "外部快照.json")
        with open(pack_reloaded.path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        with open(ext_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        ext_pack = load_published_file(ext_path)
        check(ext_pack.id == "住宅施工图审查@1.0.0", "可从库外快照文件载入")
        check(materialize(ext_pack).ref.source_path == os.path.abspath(ext_path),
              "库外快照记录来源路径")

        # 草稿模板不能当发布快照载入
        tpl_path = os.path.join(td, "tpl.json")
        write_draft_template(tpl_path, new_draft("模板包"))
        try:
            load_published_file(tpl_path)
            check(False, "草稿模板应被拒绝")
        except RulePackError:
            check(True, "拒绝把未发布草稿当快照使用")

        # ---- 8) CLI 端到端 ----
        out_dir = os.path.join(td, "out")
        rc = cli_main(["batch", ifc_dir, "--project", "花园小区一期",
                       "--stage", "completion",
                       "--rule-lib", lib_dir, "-o", out_dir,
                       "--history", os.path.join(td, "hist"),
                       "-q", "--no-unit-reports"])
        # strict 竣工包：样例有错误 -> 3
        check(rc == 3, "CLI 自动选择规则包并按其门禁阻断（退出码 3）")

        # 显式指定名称@版本
        rc2 = cli_main(["batch", ifc_dir, "--project", "任意项目",
                        "--rule-pack", "精简包@2.3.1", "--rule-lib", lib_dir,
                        "-o", os.path.join(td, "out2"),
                        "--history", os.path.join(td, "hist2"),
                        "-q", "--no-unit-reports"])
        check(rc2 in (0, 3), "CLI 显式名称@版本可执行")

        # 显式指定库外快照文件
        rc3 = cli_main(["batch", ifc_dir, "--project", "任意项目",
                        "--rule-pack", ext_path,
                        "-o", os.path.join(td, "out3"),
                        "--history", os.path.join(td, "hist3"),
                        "-q", "--no-unit-reports"])
        check(rc3 in (0, 3), "CLI 支持快照文件路径作为规则包")

        # 互斥校验
        rc4 = cli_main(["batch", ifc_dir, "--project", "花园小区一期",
                        "--rule-pack", "竣工审查包", "--rule-lib", lib_dir,
                        "--profile", "strict",
                        "-o", os.path.join(td, "out4"),
                        "--history", os.path.join(td, "hist4"), "-q"])
        check(rc4 == 2, "规则包与 --profile 同用退出码 2")

        # ---- 8b) 自动选包也必须拦住冲突参数（不得静默忽略）----
        def _n(k):
            return os.path.join(td, k)

        auto_base = ["batch", ifc_dir, "--project", "花园小区一期",
                     "--stage", "completion", "--rule-lib", lib_dir,
                     "-q", "--no-unit-reports"]

        def _auto_rc(extra, tag):
            return cli_main(auto_base + extra + ["-o", _n(f"o_{tag}"),
                                                 "--history", _n(f"h_{tag}")])

        # 修复点：以下参数在自动选包命中时都应退出码 2，且不得产出批次报告
        check(_auto_rc(["--no-gate"], "nogate") == 2,
              "自动选包 + --no-gate 被拦截（修复静默忽略）")
        check(_auto_rc(["--profile", "strict"], "prof") == 2,
              "自动选包 + --profile 被拦截")
        check(_auto_rc(["--gate-profile", "loose"], "gp") == 2,
              "自动选包 + --gate-profile 被拦截")
        check(_auto_rc(["--gate-config", "/nonexistent/g.json"], "gc") == 2,
              "自动选包 + --gate-config 被拦截")
        check(_auto_rc(["--gate-set", "unit_max_warnings=99"], "gs") == 2,
              "自动选包 + --gate-set 被拦截")
        check(_auto_rc(["--config", "/nonexistent/t.json"], "cfg") == 2,
              "自动选包 + --config 被拦截")
        check(_auto_rc(["--set", "gap_min_len_mm=50"], "set") == 2,
              "自动选包 + --set 被拦截")
        check(not os.path.exists(_n("o_nogate")),
              "冲突时不创建输出目录、不执行核查、不产出报告")

        # 逃生口：--no-rule-pack 后普通参数照常生效
        rc_esc = cli_main(auto_base + [
            "--no-rule-pack", "--no-gate",
            "-o", _n("o_esc"), "--history", _n("h_esc")])
        check(rc_esc == 0, "--no-rule-pack + --no-gate 临时口径放行（退出码 0）")
        bj_esc = [f for f in os.listdir(_n("o_esc")) if f.endswith(".json")]
        esc_data = json.load(open(os.path.join(_n("o_esc"), bj_esc[0]),
                                  encoding="utf-8"))
        check(esc_data["rule_pack"] is None,
              "临时口径批次不标注规则包（可追溯：本次未走企业规则包）")

        # --rule-pack 与 --no-rule-pack 互斥
        rc_mutex = cli_main(auto_base + [
            "--rule-pack", "竣工审查包@1.0.0", "--no-rule-pack",
            "-o", _n("o_mutex"), "--history", _n("h_mutex")])
        check(rc_mutex == 2, "--rule-pack 与 --no-rule-pack 同用退出码 2")

        # 自动选包无适用包时回退普通模式：手动参数必须照常生效（不算冲突）。
        # 用独立空库，保证确实没有任何适用规则包。
        fb_lib_dir = os.path.join(td, "fb_lib")
        RulePackLibrary(fb_lib_dir).init()
        rc_fb = cli_main(["batch", ifc_dir, "--project", "完全不相关项目",
                          "--stage", "scheme", "--rule-lib", fb_lib_dir,
                          "--no-gate", "-q", "--no-unit-reports",
                          "-o", _n("o_fb"), "--history", _n("h_fb")])
        check(rc_fb == 0, "无适用规则包自动回退后 --no-gate 照常生效（退出码 0）")
        fb_jsons = [f for f in os.listdir(_n("o_fb")) if f.endswith(".json")]
        fb_data = json.load(open(os.path.join(_n("o_fb"), fb_jsons[0]),
                                 encoding="utf-8"))
        check(fb_data["rule_pack"] is None, "回退批次不标注规则包")

        # audit 子命令：显式规则包 + 阈值参数 -> 2
        ifc_one = os.path.join(ifc_dir, "1号楼.ifc")
        rc_a1 = cli_main(["audit", ifc_one,
                          "--rule-pack", "竣工审查包@1.0.0", "--rule-lib", lib_dir,
                          "--profile", "loose", "-o", _n("a1"), "-q"])
        check(rc_a1 == 2, "audit 规则包与 --profile 同用退出码 2")
        # audit 自动选择（--use-rule-pack）+ --set -> 2
        rc_a2 = cli_main(["audit", ifc_one, "--use-rule-pack",
                          "--project", "花园小区一期", "--stage", "completion",
                          "--rule-lib", lib_dir,
                          "--set", "gap_min_len_mm=50", "-o", _n("a2"), "-q"])
        check(rc_a2 == 2, "audit 自动选包 + --set 被拦截（与批量共用同一套判定）")
        # audit 默认不自动选包：--profile 正常生效（无规则包不冲突）
        rc_a3 = cli_main(["audit", ifc_one, "--profile", "loose",
                          "-o", _n("a3"), "-q"])
        check(rc_a3 in (0, 1), "audit 不用规则包时 --profile 正常（向后兼容）")

        # rulepack 子命令
        rc5 = cli_main(["rulepack", "list", "--rule-lib", lib_dir])
        check(rc5 == 0, "rulepack list 正常")
        init_draft = os.path.join(td, "newpack.json")
        rc6 = cli_main(["rulepack", "template", init_draft,
                        "--name", "新包", "--stage", "scheme"])
        check(rc6 == 0 and os.path.exists(init_draft),
              "rulepack template 生成草稿模板")
        rc7 = cli_main(["rulepack", "publish", init_draft, "3.0.0",
                        "--as-name", "新包", "--rule-lib", lib_dir])
        check(rc7 == 0, "rulepack publish 库外草稿 + --as-name")
        rc8 = cli_main(["rulepack", "show", "新包@3.0.0",
                        "--rule-lib", lib_dir])
        check(rc8 == 0, "rulepack show 名称@版本")
        rc9 = cli_main(["rulepack", "deprecate", "新包", "3.0.0",
                        "--rule-lib", lib_dir])
        check(rc9 == 0, "rulepack deprecate 标记废止")

        # 导出的批次 JSON / 单体 Excel 含版本标注
        bj = [f for f in os.listdir(out_dir) if f.endswith(".json")]
        with open(os.path.join(out_dir, bj[0]), encoding="utf-8") as f:
            exported = json.load(f)
        check(exported["rule_pack"]["id"] == "竣工审查包@1.0.0",
              "导出批次 JSON 标注规则包版本")
        check(exported["rule_pack"]["content_hash"],
              "导出批次 JSON 含规则包指纹")

        # audit 子命令显式规则包：单体 JSON 含 rule_pack
        audit_out = os.path.join(td, "audit_out")
        rc10 = cli_main(["audit", os.path.join(ifc_dir, "1号楼.ifc"),
                         "--rule-pack", "竣工审查包@1.0.0",
                         "--rule-lib", lib_dir, "-o", audit_out, "-q"])
        check(rc10 in (0, 1), "audit 显式规则包可执行")
        aj = json.load(open(os.path.join(audit_out, "1号楼_结果.json"),
                            encoding="utf-8"))
        check(aj["rule_pack"]["id"] == "竣工审查包@1.0.0",
              "单体结果 JSON 标注规则包版本")
        check(aj["thresholds"]["provenance"]["rule_pack_id"]
              == "竣工审查包@1.0.0", "单体阈值来源标注规则包版本")

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
