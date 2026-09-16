"""命令行入口。

用法::

    python -m ifc_audit.cli audit model.ifc -o output/
    python -m ifc_audit.cli batch ifc目录/ --project XX项目 -o output/batch/
    python -m ifc_audit.cli coord run 各专业ifc目录/ --project XX项目
    python -m ifc_audit.cli trend --project XX项目
    python -m ifc_audit.cli gui              # 图形界面
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from .pipeline import audit_ifc, audit_ifc_with_config
from . import report
from .report import KIND_CN, SEV_CN
from .thresholds import (
    PROFILES, PROFILE_CN, parse_set_items, ThresholdConfigError,
    write_config_template,
)
from .gate import (
    GATE_PROFILES, GATE_PROFILE_CN, parse_gate_set_items, GateConfigError,
    write_gate_config_template,
)
from .batch import (
    run_batch_with_config, run_batch_with_rule_pack, attach_trend,
    save_batch_snapshot, load_project_history,
)
from . import batch_report
from .rule_packs import (
    RulePackLibrary, RulePackError, CHECKS, CHECK_CN,
    STAGES, STAGE_CN, DEFAULT_RULE_LIBRARY, new_draft, write_draft_template,
    resolve_rule_pack, materialize,
)


# ------------------------------------------------------- 规则库子命令 ----

def _rule_lib(args) -> RulePackLibrary:
    return RulePackLibrary(getattr(args, "rule_lib", None)
                           or DEFAULT_RULE_LIBRARY)


def _add_rule_pack_args(parser, auto_flag: bool = False) -> None:
    """audit / batch 共用的规则包参数。"""
    parser.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                        help=f"企业规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    parser.add_argument("--rule-pack", default=None, metavar="名称[@版本]|快照.json",
                        help="显式指定企业规则包：库内名称（最新版）、名称@版本，"
                             "或已发布快照 JSON 文件路径")
    if auto_flag:
        parser.add_argument("--use-rule-pack", dest="use_rule_pack",
                            action="store_true",
                            help="不指定 --rule-pack 时，按项目/阶段从规则库"
                                 "自动选择适用的已发布规则包")
    parser.add_argument("--stage", choices=STAGES, default="",
                        help="项目阶段（自动选择规则包用）："
                             + " / ".join(f"{k}={v}" for k, v in STAGE_CN.items()))


def _print_pack_row(item: dict) -> None:
    scope_p = "、".join(item["projects"]) if item["projects"] else "全部项目"
    scope_s = "、".join(STAGE_CN.get(s, s) for s in item["stages"]) \
        if item["stages"] else "全部阶段"
    flags = []
    if item["deprecated"]:
        flags.append("最新版已废止")
    if item["has_draft"]:
        flags.append("有未发布草稿")
    print(f"  {item['name']:<20} v{item['latest'] or '-':<10} "
          f"{item['n_versions']} 个版本  [{scope_p} / {scope_s}]"
          f"{'  （' + '，'.join(flags) + '）' if flags else ''}")
    if item["description"]:
        print(f"    └ {item['description']}")


def _cmd_rulepack_list(args) -> int:
    lib = _rule_lib(args)
    items = lib.list_packs()
    if not items:
        print(f"规则库（{lib.root}）中还没有规则包。"
              "可用 `rulepack init` 创建第一份草稿。")
        return 0
    print(f"规则库：{lib.root}（共 {len(items)} 个规则包）")
    for item in items:
        _print_pack_row(item)
    return 0


def _cmd_rulepack_init(args) -> int:
    lib = _rule_lib(args)
    lib.init()
    content = new_draft(
        args.name, description=args.description or "",
        projects=args.project or [], stages=args.stage or [],
        threshold_profile=args.profile,
        gate_profile=("loose" if args.profile == "loose" else "default"))
    try:
        path = lib.save_draft(content)
    except RulePackError as exc:
        print(f"规则包错误：{exc}", file=sys.stderr)
        return 2
    print(f"规则包草稿已创建：{path}")
    print(f"核查项全开；阈值预设 {args.profile}。编辑草稿后发布：")
    print(f"  python -m ifc_audit.cli rulepack publish {args.name} "
          f"1.0.0 --rule-lib {lib.root}")
    return 0


def _cmd_rulepack_show(args) -> int:
    lib = _rule_lib(args)
    try:
        pack = resolve_rule_pack(args.spec, lib)
    except RulePackError as exc:
        print(f"规则包错误：{exc}", file=sys.stderr)
        return 2
    print(f"规则包        : {pack.id}")
    print(f"说明          : {pack.description or '-'}")
    print(f"适用项目      : {'、'.join(pack.projects) if pack.projects else '全部项目'}")
    print(f"适用阶段      : "
          f"{'、'.join(STAGE_CN.get(s, s) for s in pack.stages) if pack.stages else '全部阶段'}")
    print(f"发布时间 / 人 : {pack.published_at} / {pack.published_by or '-'}")
    print(f"内容指纹      : {pack.content_hash}")
    if pack.deprecated:
        print("状态          : 已废止（不参与自动选择）")
    print("核查项：")
    for c in CHECKS:
        on = pack.checks.get(c, True)
        print(f"  [{'x' if on else ' '}] {CHECK_CN[c]}（{c}）")
    print(f"判定阈值      : 预设 {pack.threshold_profile}"
          + (f"，覆盖 {len(pack.thresholds)} 项" if pack.thresholds else ""))
    for k, v in sorted(pack.thresholds.items()):
        print(f"    {k} = {v}")
    print(f"放行门禁      : 预设 {pack.gate_profile}"
          + (f"，覆盖 {len(pack.gate_rules)} 项" if pack.gate_rules else ""))
    for k, v in sorted(pack.gate_rules.items()):
        print(f"    {k} = {v}")
    return 0


def _cmd_rulepack_publish(args) -> int:
    lib = _rule_lib(args)
    try:
        pack = lib.publish(args.source, args.version,
                           published_by=args.by or "",
                           as_name=args.as_name)
    except RulePackError as exc:
        print(f"规则包错误：{exc}", file=sys.stderr)
        return 2
    print(f"已发布规则包 {pack.id}（不可变）")
    print(f"快照：{pack.path}")
    print(f"适用：{'、'.join(pack.projects) or '全部项目'} / "
          f"{'、'.join(STAGE_CN.get(s, s) for s in pack.stages) or '全部阶段'}")
    print(f"指纹：{pack.content_hash}")
    return 0


def _cmd_rulepack_template(args) -> int:
    if os.path.exists(args.path):
        print(f"已存在同名文件，未覆盖：{args.path}", file=sys.stderr)
        return 2
    content = new_draft(
        args.name, projects=args.project or [], stages=args.stage or [],
        threshold_profile=args.profile)
    write_draft_template(args.path, content)
    print(f"规则包草稿模板已生成：{args.path}")
    print("编辑后发布：python -m ifc_audit.cli rulepack publish "
          f"{args.path} 1.0.0 --as-name {args.name}")
    return 0


def _cmd_rulepack_deprecate(args) -> int:
    lib = _rule_lib(args)
    try:
        lib.set_deprecated(args.name, args.version, not args.undo)
    except RulePackError as exc:
        print(f"规则包错误：{exc}", file=sys.stderr)
        return 2
    action = "废止" if not args.undo else "取消废止"
    print(f"已{action}规则包 {args.name}@{args.version}")
    return 0


# ------------------------------------------------------- 规则版本切换 ----

def _print_switch_diff(record: dict) -> None:
    """打印试算对比：规则调整影响（同模型重算差异）与门禁结论变化。"""
    from .report import KIND_CN, SEV_CN
    print(f"\n试算记录    : {record['switch_id']}（{record['created_at']}）")
    print(f"原始批次    : {record['original_batch_id']} "
          f"{record.get('original_label') or ''}"
          f"（{record.get('original_created_at')}，规则包 "
          f"{record.get('original_rule_pack_id') or '未使用'}）")
    print(f"规则包切换  : {record['old_rule_pack_id']} → "
          f"{record['new_rule_pack_id']}")
    print(f"重算模型    : {len(record.get('files', []))} 个单体（与原始批次同一批）")

    cons = record.get("consistency", {})
    if cons.get("consistent"):
        print("重算一致性  : ✅ 旧规则重算与原始批次完全一致"
              "（模型文件未变，差异全部来自规则调整）")
    else:
        print("重算一致性  : ⚠ 旧规则重算与原始批次不一致，"
              "模型文件相对原批次可能已被改动，下列差异可能混入模型变化：")
        for m in cons.get("mismatches", [])[:10]:
            print(f"    · {m['scope']} {m['key']}: "
                  f"原批次 {m['original']} → 重算 {m['recheck']}")

    pc = record.get("pack_compare", {})
    toggled = pc.get("checks_toggled", [])
    if toggled:
        print("\n核查项开关变化：")
        for c in toggled:
            print(f"  · {c['name']}（{c['check']}）："
                  f"{'启用' if c['old'] else '关闭'} → "
                  f"{'启用' if c['new'] else '关闭'}")
    th_rows = pc.get("thresholds", [])
    if th_rows:
        print("判定阈值变化：")
        for r in th_rows:
            unit = {"mm": "mm", "%": "%", "ratio": ""}.get(r.get("unit"), "")
            label = r.get("label") or r["key"]
            print(f"  · {label}（{r['key']}）: {r['old']:g}{unit} → "
                  f"{r['new']:g}{unit}")
    gate_rows = pc.get("gate_rules", [])
    if gate_rows:
        print("放行门禁变化：")
        for r in gate_rows:
            print(f"  · {r['key']}: {r['old']} → {r['new']}")

    diff = record.get("issue_diff", {})
    t = diff.get("totals", {})
    print("\n规则调整影响（同一批模型，仅规则口径不同）：")
    print(f"  问题新增 {t.get('added', 0)} 条 / 消除 {t.get('removed', 0)} 条 "
          f"/ 保留 {t.get('kept', 0)} 条")
    for check, row in sorted(diff.get("by_check", {}).items()):
        if row["added"] or row["removed"]:
            print(f"  · {check}：新增 {row['added']} / 消除 {row['removed']}"
                  f"（保留 {row['kept']}）")
    md = record.get("metric_deltas", {})
    changed = [(d["label"], d) for d in md.values() if d["delta"]]
    if changed:
        print("  指标变化：")
        for label, d in changed:
            print(f"    {label:<12} {d['old']:g} → {d['new']:g}"
                  f"（{'+' if d['delta'] > 0 else ''}{d['delta']:g}）")

    gc = record.get("gate_compare", {})
    verdict = {"放行": "✅ 放行", "阻断": "⛔ 阻断"}
    old_v = verdict["放行" if gc.get("old_passed") else "阻断"]
    new_v = verdict["放行" if gc.get("new_passed") else "阻断"]
    print(f"\n放行门禁联动：{old_v} → {new_v}"
          f"（未通过规则 {gc.get('old_failed_count', 0)} → "
          f"{gc.get('new_failed_count', 0)} 条）")
    for r in gc.get("newly_failed", []):
        print(f"  ✗ 新增未通过：[{r['scope']}] {r['message']}")
    for r in gc.get("newly_passed", []):
        print(f"  ✓ 转为通过：  [{r['scope']}] {r['message']}")

    # 逐条问题示例（新增 / 消除各列前 5 条）
    for tag, key in (("新增问题", "added"), ("消除问题", "removed")):
        shown = 0
        for unit, row in sorted(diff.get("per_unit", {}).items()):
            for i in row.get(key, []):
                if shown == 0:
                    print(f"\n{tag}（前 5 条示例）：")
                if shown >= 5:
                    break
                print(f"  · [{unit}] [{SEV_CN.get(i['severity'], i['severity'])}] "
                      f"{KIND_CN.get(i['kind'], i['kind'])} | {i['title']}")
                shown += 1
            if shown >= 5:
                break


def _cmd_ruleswitch_compare(args) -> int:
    from .rule_switch import run_rule_switch_compare, RuleSwitchError
    history_dir = args.history or os.path.join("output", "batch_history")
    lib = _rule_lib(args)
    history = load_project_history(history_dir, args.project)
    if not history:
        print(f"项目“{args.project}”没有历史批次，无法做规则切换试算；"
              "请先用 batch 完成一次按规则包的核查。", file=sys.stderr)
        return 2
    if args.from_batch:
        original = next((h for h in history
                         if h.get("batch_id") == args.from_batch), None)
        if original is None:
            print(f"项目“{args.project}”没有批次 {args.from_batch}；"
                  "可用 trend --project 查看批次号。", file=sys.stderr)
            return 2
    else:
        original = history[-1]

    old_spec = args.from_pack or (original.get("rule_pack") or {}).get("id")
    if not old_spec:
        print(f"原始批次 {original.get('batch_id')} 未使用企业规则包，"
              "请用 --from-pack 显式指定旧规则包（名称@版本）。", file=sys.stderr)
        return 2

    def progress(pct, msg):
        if not args.quiet:
            print(f"[{pct:3d}%] {msg}", flush=True)

    try:
        old_pack = resolve_rule_pack(old_spec, lib)
        new_pack = resolve_rule_pack(args.to, lib)
        if old_pack.id == new_pack.id:
            print(f"新旧规则包相同（{old_pack.id}），无需切换。", file=sys.stderr)
            return 2
        record = run_rule_switch_compare(
            args.project, original,
            materialize(old_pack), materialize(new_pack),
            history_dir=history_dir, by=args.by or "",
            progress=progress if not args.quiet else None)
    except (RulePackError, RuleSwitchError) as exc:
        print(f"规则切换试算失败：{exc}", file=sys.stderr)
        return 2

    os.makedirs(args.output, exist_ok=True)
    jpath = os.path.join(
        args.output, f"{args.project}_规则切换试算_{record['switch_id']}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    print("\n============== 规则版本切换试算 ==============")
    _print_switch_diff(record)
    print(f"\n试算记录已归档：{record.get('record_path', '')}")
    print(f"对比明细已导出：{jpath}")
    print("\n本试算不影响项目趋势与放行；确认切换后联动生效：")
    print(f"  python -m ifc_audit.cli ruleswitch confirm "
          f"--project {args.project} --switch {record['switch_id']}")
    return 0


def _cmd_ruleswitch_confirm(args) -> int:
    from .rule_switch import confirm_rule_switch, RuleSwitchError
    history_dir = args.history or os.path.join("output", "batch_history")
    try:
        record = confirm_rule_switch(
            history_dir, args.project,
            switch_id=args.switch or None, by=args.by or "")
    except RuleSwitchError as exc:
        print(f"确认切换失败：{exc}", file=sys.stderr)
        return 2
    print("\n============== 规则版本切换已确认 ==============")
    print(f"试算记录    : {record['switch_id']}（确认人："
          f"{record['confirmed_by'] or '-'}，{record['confirmed_at']}）")
    print(f"规则包切换  : {record['old_rule_pack_id']} → "
          f"{record['new_rule_pack_id']}")
    intervening = record.get("intervening_batches") or []
    if intervening:
        print(f"延迟确认    : 原批次之后已有 {len(intervening)} 个批次"
              f"（{', '.join(intervening[:3])}{'…' if len(intervening) > 3 else ''}），"
              "基线按确认时刻插入时间线，这些历史批次保留不变；"
              "后续批次趋势只与同口径基线对比，不混入旧口径变化")
    print("联动更新：")
    print(f"  · 项目规则选择：后续 batch 自动按 "
          f"{record['new_rule_pack_id']} 核查（--rule-pack 可显式覆盖）")
    print("  · 放行门禁    ：按新规则包门禁口径判定")
    print(f"  · 趋势统计    ：新规则重算基线已写入批次历史"
          f"（批次 {record['baseline_batch_id']}），"
          "下一批次增量反映模型整改效果")
    print(f"  · 追溯保留    ：原始批次 {record['original_batch_id']} 及规则包 "
          f"{record['old_rule_pack_id']} 快照均保留不变")
    return 0


def _cmd_ruleswitch_list(args) -> int:
    from .rule_switch import list_switch_records
    history_dir = args.history or os.path.join("output", "batch_history")
    records = list_switch_records(history_dir, args.project)
    if not records:
        print(f"项目“{args.project}”没有规则切换试算记录。")
        return 0
    print(f"项目“{args.project}”共 {len(records)} 条规则切换记录：\n")
    print(f"{'试算编号':<22}{'状态':<8}{'规则包切换':<44}{'时间':<22}确认人")
    for r in records:
        status = {"pending": "待确认", "confirmed": "已确认"}.get(
            r.get("status"), r.get("status", ""))
        switch = f"{r.get('old_rule_pack_id', '')} → {r.get('new_rule_pack_id', '')}"
        print(f"{r.get('switch_id', ''):<22}{status:<8}{switch:<44}"
              f"{r.get('created_at', ''):<22}{r.get('confirmed_by') or '-'}")
    return 0


def _cmd_ruleswitch_show(args) -> int:
    from .rule_switch import load_switch_record, RuleSwitchError
    history_dir = args.history or os.path.join("output", "batch_history")
    try:
        record = load_switch_record(history_dir, args.project, args.switch)
    except RuleSwitchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    status = {"pending": "待确认", "confirmed": "已确认"}.get(
        record.get("status"), record.get("status"))
    print(f"状态        : {status}"
          + (f"（{record.get('confirmed_at')} 确认，"
             f"基线批次 {record.get('baseline_batch_id')}）"
             if record.get("status") == "confirmed" else ""))
    _print_switch_diff(record)
    return 0


def _resolve_materialized(args, optional: bool = False) -> tuple[object, int]:
    """按命令行参数解析并物化规则包；失败返回 (None, 退出码)。

    optional=True 时（批量自动选择），库不存在 / 无适用规则包等“未配置”
    情况返回 (None, 0) 由调用方回退普通模式；显式指定的错误仍然报错。
    """
    lib = _rule_lib(args)
    try:
        if getattr(args, "rule_pack", None):
            pack = resolve_rule_pack(args.rule_pack, lib)
        else:
            pack = lib.select_for(args.project, getattr(args, "stage", "") or "")
        return materialize(pack), 0
    except RulePackError as exc:
        if optional and not getattr(args, "rule_pack", None):
            if not args.quiet:
                print(f"[info] 未使用企业规则包（{exc}），改用内置预设。")
            return None, 0
        print(f"规则包错误：{exc}", file=sys.stderr)
        return None, 2


def _rule_pack_conflict_items(args, include_gate: bool = True) -> list[str]:
    """与规则包口径互斥的手动参数（保证“按哪个版本核查”可追溯）。

    include_gate=False 用于没有门禁参数的 audit 子命令。
    """
    items = []
    if getattr(args, "profile", "default") != "default":
        items.append(f"--profile {args.profile}")
    if getattr(args, "config", None):
        items.append(f"--config {args.config}")
    if getattr(args, "set_threshold", None):
        items.append("--set")
    if include_gate:
        if getattr(args, "no_gate", False):
            items.append("--no-gate")
        if getattr(args, "gate_profile", "default") != "default":
            items.append(f"--gate-profile {args.gate_profile}")
        if getattr(args, "gate_config", None):
            items.append("--gate-config")
        if getattr(args, "gate_set", None):
            items.append("--gate-set")
    return items


def _resolve_rule_pack_for_run(args, auto: bool,
                               include_gate: bool = True,
                               escape_hint: str = "") -> tuple[object, int]:
    """audit / batch 共用的规则包选择 + 冲突判定。

    流程：先判断是否要用规则包（显式 ``--rule-pack`` 优先；否则按
    ``auto`` 决定是否按项目/阶段自动选择），**确认实际选到规则包后**再
    检查手动阈值 / 门禁参数冲突——自动选包没有适用包而回退普通模式时，
    普通参数照常生效，不算冲突。

    Args:
        escape_hint: 自动选包冲突时给出的“改用临时口径”操作提示
            （batch 为 ``--no-rule-pack``，audit 为去掉 ``--use-rule-pack``）。

    Returns:
        (mat, rc)：``(None, 0)`` 表示本次不用规则包（调用方走普通模式）；
        ``(mat, 0)`` 为已物化规则包；``(None, 2)`` 表示冲突或规则包错误。
    """
    explicit = bool(getattr(args, "rule_pack", None))
    if not (explicit or auto):
        return None, 0
    if explicit and getattr(args, "no_rule_pack", False):
        print("配置冲突：--rule-pack 与 --no-rule-pack 不能同时使用。",
              file=sys.stderr)
        return None, 2
    # 自动选择允许“无适用包”回退；显式指定时任何错误都退出码 2
    mat, rc = _resolve_materialized(args, optional=not explicit)
    if rc or mat is None:
        return mat, rc
    # 已选到规则包：手动阈值/门禁参数不得再覆盖口径，否则报告虽标注版本、
    # 实际口径却不是该版本，追溯链断裂
    conflicts = _rule_pack_conflict_items(args, include_gate=include_gate)
    if conflicts:
        esc = f"\n  · {escape_hint}；" if (auto and escape_hint) else ""
        print(
            f"配置冲突：本次已{'显式指定' if explicit else '按项目/阶段自动选用'}"
            f"企业规则包 {mat.ref.id}，不允许同时指定 "
            f"{'、'.join(conflicts)}。\n"
            "  · 要按规则包口径核查/放行：去掉上述冲突参数；"
            f"{esc}"
            "\n  · 需要不同的阈值或放行条件（含本次不阻断放行）："
            "请调整并发布新版本的规则包（版本化留痕，可追溯）。",
            file=sys.stderr)
        return None, 2
    return mat, 0


def _cmd_audit(args) -> int:
    out_dir = args.output
    base = os.path.splitext(os.path.basename(args.ifc))[0]

    def progress(pct, msg):
        if not args.quiet:
            print(f"[{pct:3d}%] {msg}", flush=True)

    try:
        overrides = parse_set_items(args.set_threshold)
        auto = bool(getattr(args, "use_rule_pack", False))
        mat, rc = _resolve_rule_pack_for_run(
            args, auto=auto, include_gate=False,
            escape_hint=("本次确实要改用命令行临时口径：去掉 --use-rule-pack"
                         "（该次核查不标注规则包版本，不作为企业口径留痕）"))
        if rc:
            return rc
        # 口径确定且无冲突后才创建输出目录
        os.makedirs(out_dir, exist_ok=True)
        if mat is not None:
            model = audit_ifc(
                args.ifc, progress=progress if not args.quiet else None,
                thresholds=mat.thresholds,
                provenance=mat.threshold_provenance,
                enabled_kinds=mat.enabled_kinds, rule_pack=mat.ref)
            pack_info = mat.ref
        else:
            model = audit_ifc_with_config(
                args.ifc, progress=progress if not args.quiet else None,
                profile=args.profile, config_path=args.config,
                overrides=overrides or None)
            pack_info = None
    except ThresholdConfigError as exc:
        print(f"阈值配置错误：{exc}", file=sys.stderr)
        return 2
    s = model.summary()

    print("\n================ 核查汇总 ================")
    print(f"文件        : {s['file']}")
    if pack_info is not None:
        print(f"规则包      : {pack_info.describe()}")
        print(f"规则包指纹  : {pack_info.content_hash}")
    print(f"墙体/门/窗  : {s['walls']} / {s['doors']} / {s['windows']}")
    print(f"房间        : {s['rooms']}    净面积合计: {s['total_net_area']} m²")
    print(f"问题        : {s['issues']} 条 (错误 {s['errors']} / 警告 {s['warnings']})")
    print(f"重复构件组  : {s['duplicate_groups']}")
    print(f"阈值方案    : {model.threshold_provenance.describe()}")

    if model.issues:
        print("\n---------------- 问题清单 ----------------")
        for n, i in enumerate(model.issues, start=1):
            print(f"{n:>3}. {i.issue_id} [{SEV_CN.get(i.severity, i.severity)}] "
                  f"{KIND_CN.get(i.kind, i.kind)} | {i.title}"
                  f"{f'  ({i.storey})' if i.storey else ''}")

    print("\n---------------- 房间净面积 --------------")
    print(f"{'房间':<14}{'楼层':<10}{'净面积m²':>10}{'来源':>8}"
          f"{'门':>4}{'窗':>4}  围护状态")
    for r in model.rooms:
        print(f"{(r.name or '')[:14]:<14}{(r.storey or '')[:10]:<10}"
              f"{r.net_area:>10.2f}{('声明' if r.area_source == 'declared' else '几何'):>8}"
              f"{r.doors:>4}{r.windows:>4}  {r.enclosure_label}")

    print("\n---------------- 门窗表 ------------------")
    from .openings import size_label
    print(f"{'楼层':<8}{'房间':<14}{'类':<4}{'类型':<10}"
          f"{'规格(mm)':<12}{'数量':>4}  备注")
    for r in model.opening_schedule:
        print(f"{(r.storey or '-')[:8]:<8}{r.room_name[:14]:<14}"
              f"{('门' if r.kind == 'door' else '窗'):<4}"
              f"{(r.type_name or '')[:10]:<10}{size_label(r.width, r.height):<12}"
              f"{r.count:>4}  {r.notes}")

    outputs = {}
    xlsx = os.path.join(out_dir, f"{base}_核查报告.xlsx")
    report.export_excel(model, xlsx)
    outputs["excel"] = xlsx

    report.export_issues_csv(model, os.path.join(out_dir, f"{base}_问题清单.csv"))
    report.export_rooms_csv(model, os.path.join(out_dir, f"{base}_房间净面积.csv"))
    outputs["issues_csv"] = os.path.join(out_dir, f"{base}_问题清单.csv")
    outputs["rooms_csv"] = os.path.join(out_dir, f"{base}_房间净面积.csv")

    report.export_openings_csv(model, os.path.join(out_dir, f"{base}_门窗表.csv"))
    outputs["openings_csv"] = os.path.join(out_dir, f"{base}_门窗表.csv")

    plan = os.path.join(out_dir, f"{base}_标注平面图.png")
    report.export_annotated_plan(model, plan)
    outputs["annotated_plan"] = plan

    # 三维图：优先 pyvista 离屏渲染；无 GL/显示环境自动降级 matplotlib
    view3d = os.path.join(out_dir, f"{base}_三维标注.png")
    try:
        from .viewer import Viewer3D, offscreen_render_available, matplotlib_screenshot
        if offscreen_render_available():
            Viewer3D(model).screenshot(view3d)
        else:
            if not args.quiet:
                print("[info] 当前环境无 GPU/显示，三维图改用 matplotlib 渲染；"
                      "在桌面环境运行 `python -m ifc_audit.cli gui` 可使用 PyVista 交互定位。")
            matplotlib_screenshot(model, view3d)
    except Exception as exc:
        if not args.quiet:
            print(f"[warn] 三维渲染失败（{exc}），改用 matplotlib。")
        from .viewer import matplotlib_screenshot
        matplotlib_screenshot(model, view3d)
    outputs["view3d"] = view3d

    # 机器可读 JSON（GUI / 后续流水线使用）
    from dataclasses import asdict
    dump = {
        "summary": s,
        "rule_pack": pack_info.to_dict() if pack_info is not None else None,
        "thresholds": {
            "values": asdict(model.thresholds) if model.thresholds else None,
            "provenance": (model.threshold_provenance.to_dict()
                           if model.threshold_provenance else None),
        },
        "issues": [
            {
                "id": i.issue_id, "severity": i.severity, "kind": i.kind,
                "title": i.title, "detail": i.detail,
                "global_ids": i.global_ids,
                "location": list(i.location), "storey": i.storey,
                "measure": i.measure,
            } for i in model.issues
        ],
        "rooms": [vars(r) for r in model.rooms],
        "openings": {
            "items": [vars(o) for o in model.opening_items],
            "schedule": [vars(r) for r in model.opening_schedule],
        },
        "outputs": outputs,
    }
    json_path = os.path.join(out_dir, f"{base}_结果.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2, default=str)

    print("\n---------------- 导出文件 ----------------")
    for k, v in outputs.items():
        print(f"{k:<16}: {v}")
    print(f"{'json':<16}: {json_path}")

    return 1 if (s["errors"] > 0 and args.fail_on_error) else 0


def _print_batch_summary(batch) -> None:
    t = batch.totals
    print("\n============== 项目批量核查汇总 ==============")
    print(f"项目        : {batch.project}")
    print(f"批次        : {batch.batch_id} {batch.label or ''}（{batch.created_at}）")
    print(f"纳入单体    : {t['units']} 个（成功 {t['units'] - t['units_failed']}"
          f" / 失败 {t['units_failed']}）")
    print(f"构件(墙门窗房间): {t['walls']} / {t['doors']} / "
          f"{t['windows']} / {t['rooms']}")
    print(f"问题        : {t['issues']} 条 (错误 {t['errors']} / "
          f"警告 {t['warnings']} / 提示 {t['infos']})")
    print(f"重复构件组  : {t['duplicate_groups']}")
    print(f"净面积合计  : {t['total_net_area']} m2；不闭合房间 {t['rooms_open']} 间")
    print(f"门窗        : 共 {t['opening_total']} 樘，"
          f"尺寸异常 {t['opening_anomaly']}，未归属 {t['opening_unassigned']}")
    if batch.rule_pack:
        print(f"规则包      : {batch.rule_pack['id']}（指纹 {batch.rule_pack['content_hash']}）")
    print(f"核查阈值    : {next((u.threshold_describe for u in batch.units if u.ok), '-')}")
    print(f"门禁方案    : {batch.gate.get('description') or '-'}")

    print("\n---------------- 单体汇总 ----------------")
    print(f"{'单体':<16}{'状态':<6}{'错误':>5}{'警告':>5}"
          f"{'重复组':>7}{'净面积m²':>11}{'不闭合':>7}{'异常门窗':>8}")
    for u in batch.units:
        if not u.ok:
            print(f"{u.name[:16]:<16}{'失败':<6}  {u.error}")
            continue
        print(f"{u.name[:16]:<16}{'成功':<6}{u.errors:>5}{u.warnings:>5}"
              f"{u.dup_groups:>7}{u.total_net_area:>11.2f}"
              f"{u.rooms_open:>7}{u.opening_anomaly:>8}")

    # 门禁未通过项与最终放行结论统一在命令末尾给出（质量/协同/闭环合并为一条），
    # 这里不再分别打印，避免同一批次出现多份阻断结论。

    tr = batch.trend or {}
    if tr.get("has_previous"):
        print("\n---------------- 趋势对比（相对上一批次）----------------")
        print(f"上一批次：{tr.get('previous_batch_id')} "
              f"{tr.get('previous_label') or ''}（{tr.get('previous_created_at')}）")
        if tr.get("previous_is_rule_switch_baseline"):
            rs = tr.get("previous_rule_switch") or {}
            print(f"对比基线：规则切换重算基线（同一批模型 × "
                  f"{rs.get('new_rule_pack_id') or '新规则包'}），"
                  "下列增量反映模型整改效果；"
                  "规则调整影响见切换试算记录（ruleswitch show）")
        prev_pack = tr.get("previous_rule_pack_id") or ""
        cur_pack = (batch.rule_pack or {}).get("id", "")
        if cur_pack or prev_pack:
            if cur_pack == prev_pack:
                print(f"规则包    : {cur_pack or '未使用'}（与上一批次一致）")
            else:
                print(f"规则包    : {prev_pack or '未使用（内置预设）'} → "
                      f"{cur_pack or '未使用（内置预设）'}（版本已切换，指标口径可能变化）")
        for key, d in tr.get("deltas", {}).items():
            delta = d["delta"]
            if delta == 0:
                arrow = "持平"
            else:
                arrow = f"{'增加' if delta > 0 else '减少'} {abs(delta):g}"
            print(f"  {d['label']:<12} {d['old']:g} → {d['new']:g}  （{arrow}）")
        if tr.get("units_new") or tr.get("units_missing"):
            print(f"  新增单体：{', '.join(tr['units_new']) or '无'}；"
                  f"本批缺失：{', '.join(tr['units_missing']) or '无'}")


def _safe_unit_filename(name: str) -> str:
    """单体名 -> 可安全作为文件名的字符串（去掉路径分隔符等非法字符）。"""
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip("_") or "unit"


def _parse_disciplines(items: list[str] | None) -> dict[str, str]:
    """解析 ``--discipline 单体=专业``（可重复）。"""
    from .coordination_model import DISCIPLINES
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--discipline 参数格式应为 单体=专业：“{item}”")
        unit, disc = item.split("=", 1)
        unit, disc = unit.strip(), disc.strip()
        if disc not in DISCIPLINES:
            raise ValueError(
                f"--discipline 未知专业“{disc}”，可选：{', '.join(DISCIPLINES)}")
        out[unit] = disc
    return out


def _export_unit_reports(batch, out_dir, quiet, with_3d) -> None:
    """为每个成功核查的单体导出单模型 Excel/CSV/平面图（可选三维图）。"""
    sub = os.path.join(out_dir, "单体报告")
    os.makedirs(sub, exist_ok=True)
    used_names: set[str] = set()
    for u in batch.units:
        if u.model is None:
            continue
        # 不同目录同名单体已在批量入口消歧；这里再做一次文件名防御，绝不覆盖
        base = _safe_unit_filename(u.name)
        if base in used_names:
            i = 2
            while f"{base}-{i}" in used_names:
                i += 1
            base = f"{base}-{i}"
        used_names.add(base)
        report.export_excel(u.model, os.path.join(sub, f"{base}_核查报告.xlsx"))
        report.export_issues_csv(u.model, os.path.join(sub, f"{base}_问题清单.csv"))
        report.export_rooms_csv(u.model, os.path.join(sub, f"{base}_房间净面积.csv"))
        report.export_openings_csv(u.model, os.path.join(sub, f"{base}_门窗表.csv"))
        report.export_annotated_plan(
            u.model, os.path.join(sub, f"{base}_标注平面图.png"))
        if with_3d:
            view3d = os.path.join(sub, f"{base}_三维标注.png")
            try:
                from .viewer import (
                    Viewer3D, offscreen_render_available, matplotlib_screenshot)
                if offscreen_render_available():
                    Viewer3D(u.model).screenshot(view3d)
                else:
                    matplotlib_screenshot(u.model, view3d)
            except Exception as exc:
                if not quiet:
                    print(f"[warn] {u.name} 三维渲染失败（{exc}），跳过。")
        if not quiet:
            print(f"  已导出单体报告：{base}")


def _cmd_batch(args) -> int:
    out_dir = args.output
    history_dir = args.history or os.path.join("output", "batch_history")

    def progress(pct, msg):
        if not args.quiet:
            print(f"[{pct:3d}%] {msg}", flush=True)

    try:
        overrides = parse_set_items(args.set_threshold)
        gate_overrides = parse_gate_set_items(args.gate_set)
        # 多专业协同核查参数
        from .coordination import (
            resolve_settings, parse_coord_gate_items,
            parse_owner_items as _parse_owners, CoordinationConfigError,
        )
        coord_owners = _parse_owners(getattr(args, "owner", None))
        coord_settings = resolve_settings(
            {kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
             for kv in getattr(args, "coord_set", [])}
            if getattr(args, "coord_set", None) else None)
        coord_gate_overrides = parse_coord_gate_items(
            getattr(args, "coord_gate_set", None))
        coord_discipline_map = _parse_disciplines(
            getattr(args, "discipline", None))
        coord_mode = getattr(args, "coord_mode", "auto")
        run_coord = {"auto": None, "off": False, "required": True}[coord_mode]

        # ---- 局部复查范围（单体 / 专业 / 核查项）----
        from . import collab as collab_mod
        scan_paths = list(args.paths)
        recheck_units = list(getattr(args, "recheck_unit", []) or [])
        recheck_disciplines = list(getattr(args, "recheck_discipline", []) or [])
        recheck_kinds = list(getattr(args, "recheck_kind", []) or [])
        valid_disc = {"arch", "struct", "mep"}
        bad_disc = [d for d in recheck_disciplines if d not in valid_disc]
        if bad_disc:
            print(f"配置错误：未知复查专业“{'、'.join(bad_disc)}”，"
                  f"可选 arch/struct/mep", file=sys.stderr)
            return 2
        valid_kinds = set(collab_mod.AUDIT_KINDS) | set(collab_mod.KIND_ALIASES)
        bad_kinds = [k for k in recheck_kinds if k not in valid_kinds]
        if bad_kinds:
            print(f"配置错误：未知复查核查项“{'、'.join(bad_kinds)}”，"
                  f"可选：{'、'.join(sorted(valid_kinds))}", file=sys.stderr)
            return 2
        if recheck_units:
            # 展开输入并同时按 显示名 / 稳定单体键（相对模型根路径）过滤
            from .batch import discover_ifc_files, unique_unit_names
            from .identity import resolve_model_root, build_unit_keys
            all_files = discover_ifc_files(args.paths)
            name_of = unique_unit_names(all_files)
            _root, _ = resolve_model_root(
                all_files, project=args.project, history_dir=history_dir,
                explicit=getattr(args, "model_root", "") or "")
            key_of = build_unit_keys(all_files, _root)  # 绝对路径 -> 稳定键
            wanted = set(recheck_units)

            def _matches(fp: str) -> bool:
                afp = os.path.abspath(fp)
                return (name_of.get(fp) in wanted
                        or key_of.get(afp) in wanted
                        or key_of.get(afp, "").replace("/", os.sep) in wanted)

            kept = [f for f in all_files if _matches(f)]
            matched_names = {name_of[f] for f in kept}
            matched_keys = {key_of.get(os.path.abspath(f), "") for f in kept}
            available = set(name_of.values()) | set(key_of.values())
            missing = sorted(w for w in wanted
                             if w not in matched_names and w not in matched_keys)
            if not kept:
                print(f"配置错误：复查单体 {'、'.join(sorted(wanted))} "
                      f"在输入中没有匹配的 IFC 文件；可用单体："
                      f"{'、'.join(sorted(available))}", file=sys.stderr)
                return 2
            if missing:
                print(f"[warn] 复查单体 {'、'.join(missing)} 未在输入中找到，"
                      "已忽略", file=sys.stderr)
            scan_paths = kept
            # 复查范围统一用命中文件的稳定键（覆盖判定按稳定键）
            scope_units = sorted({key_of.get(os.path.abspath(f))
                                  or name_of[f] for f in kept})
        else:
            scope_units = []
        scan_scope = collab_mod.ScanScope.make(
            units=scope_units, disciplines=recheck_disciplines,
            kinds=recheck_kinds)
        if scan_scope.partial and not args.quiet:
            print(f"[复查] 本次为局部复查：{scan_scope.describe()}；"
                  "范围外 / 扫描失败的工单保留状态，不自动销项。")

        # 批量默认按项目/阶段自动选包；--no-rule-pack 显式关闭自动选择。
        # 显式 --rule-pack 与自动选包共用同一套冲突判定（含 --no-gate）。
        auto = getattr(args, "use_rule_pack", True) \
            and not getattr(args, "no_rule_pack", False)
        # 项目已确认规则切换（ruleswitch confirm）时，绑定版本优先于库内自动选择
        if auto and not getattr(args, "rule_pack", None):
            from .rule_switch import load_project_rule_binding
            binding = load_project_rule_binding(history_dir, args.project)
            if binding:
                args.rule_pack = binding["rule_pack_id"]
                if not args.quiet:
                    print(f"[info] 项目已确认切换至规则包 {args.rule_pack}"
                          f"（{binding.get('confirmed_at', '')} 确认），"
                          "本次按该版本核查与放行；"
                          "更换口径请重新走 ruleswitch 试算确认。")
        mat, rc = _resolve_rule_pack_for_run(
            args, auto=auto, include_gate=True,
            escape_hint=("本次确实要改用命令行临时口径：显式加 --no-rule-pack"
                         "（该次报告不标注规则包版本，不作为企业口径留痕）"))
        if rc:
            return rc
        # 口径已确定（规则包或普通参数）且无冲突，才创建输出目录并执行
        os.makedirs(out_dir, exist_ok=True)
        # --no-gate 为整体逃生口：质量、协同、闭环三类门禁都不阻断
        coord_gate_profile = getattr(args, "coord_gate_profile", "default")
        if getattr(args, "no_gate", False):
            coord_gate_profile = "none"
        common_kw = dict(
            run_coordination_check=run_coord,
            coord_owners=coord_owners,
            coord_settings=coord_settings,
            coord_gate_profile=coord_gate_profile,
            coord_gate_overrides=coord_gate_overrides or None,
            coord_discipline_map=coord_discipline_map or None,
            history_dir=history_dir,
            model_root=getattr(args, "model_root", "") or "",
        )
        if mat is not None:
            batch = run_batch_with_rule_pack(
                scan_paths, mat,
                project=args.project, label=args.label or "",
                progress=progress if not args.quiet else None,
                **common_kw)
        else:
            batch = run_batch_with_config(
                scan_paths,
                project=args.project,
                label=args.label or "",
                threshold_profile=args.profile,
                threshold_config=args.config,
                threshold_overrides=overrides or None,
                gate_profile=("none" if args.no_gate else args.gate_profile),
                gate_config=(None if args.no_gate else args.gate_config),
                gate_overrides=(None if args.no_gate else (gate_overrides or None)),
                progress=progress if not args.quiet else None,
                **common_kw)
        attach_trend(batch, history_dir)
    except (ThresholdConfigError, GateConfigError, RulePackError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"批量核查失败：{exc}", file=sys.stderr)
        return 2
    except CoordinationConfigError as exc:
        print(f"协同核查配置错误：{exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        _print_batch_summary(batch)
        _print_coordination_summary(batch)

    # 导出：批次 Excel / 看板 PNG / JSON + 单体报告
    xlsx = batch_report.export_batch_excel(
        batch, os.path.join(out_dir, f"{args.project}_批次核查报告_{batch.batch_id}.xlsx"))
    png = batch_report.export_dashboard(
        batch, os.path.join(out_dir, f"{args.project}_质量看板_{batch.batch_id}.png"))
    jpath = batch_report.export_batch_json(
        batch, os.path.join(out_dir, f"{args.project}_批次结果_{batch.batch_id}.json"))

    if not args.no_unit_reports:
        if not args.quiet:
            print("\n---------------- 导出单体报告 ----------------")
        _export_unit_reports(batch, out_dir, args.quiet, args.with_3d)

    # 多专业协同核查产物（独立 Excel/CSV/JSON + 建筑侧回写 + 台账）
    coord_paths = _export_coordination(batch, out_dir, args.quiet)

    # 协同问题闭环：统一纳管批量审查 + 多专业协同问题（门禁失败项不重复建单）
    collab_paths = {}
    collab_failed: list[dict] = []
    if getattr(args, "collab_mode", "on") == "on":
        # --no-gate 为整体逃生口：闭环纳管 / 报告照常，但闭环门禁不阻断放行
        if getattr(args, "no_gate", False) \
                and getattr(args, "collab_gate_profile", "default") == "default":
            args.collab_gate_profile = "none"
        collab_paths, _collab_blocked, collab_failed = _run_collab_for_batch(
            batch, args, out_dir, history_dir, scope=scan_scope)

    # 快照留存（批次 JSON 导出后再留存，供后续批次趋势对比）
    snap = save_batch_snapshot(batch, history_dir)

    print("\n---------------- 导出文件 ----------------")
    for label, p in (("批次Excel", xlsx), ("质量看板", png),
                     ("批次JSON", jpath), ("批次快照", snap)):
        print(f"{label:<10}: {p}")
    for label, p in coord_paths.items():
        print(f"{label:<10}: {p}")
    for label, p in collab_paths.items():
        print(f"{label:<10}: {p}")

    # ---- 统一放行结论：质量 / 多专业协同 / 闭环三类门禁合并为一个结论 ----
    quality_failed = [r for r in batch.gate_results
                      if r.level in ("unit", "project", "batch") and not r.passed]
    coord_blocked = (batch.coordination is not None
                     and not batch.coordination.gate_passed)
    collab_blocked = bool(collab_failed)
    blocking = []
    if quality_failed:
        blocking.append("质量门禁")
    if coord_blocked:
        blocking.append("多专业协同门禁")
    if collab_blocked:
        blocking.append("协同闭环门禁")

    if blocking:
        print(f"\n⛔ 不予放行：{'、'.join(blocking)}未通过（同一批次统一结论，退出码 3）。",
              file=sys.stderr)
        for r in quality_failed:
            level_cn = {"unit": "单体", "project": "项目", "batch": "批次"}
            print(f"  ✗ [质量·{level_cn.get(r.level, r.level)}] "
                  f"{r.scope}：{r.message}", file=sys.stderr)
        if coord_blocked:
            for r in batch.coordination.gate_rules:
                if not r["passed"]:
                    print(f"  ✗ [协同] {r['message']}", file=sys.stderr)
        for r in collab_failed:
            print(f"  ✗ [闭环] {r['message']}", file=sys.stderr)
        print("\n请调整模型或按工单整改、复核闭环后重新核查；"
              "确需临时放行可用 --gate-profile loose / --no-gate（同时不阻断协同与闭环门禁）。",
              file=sys.stderr)
        return 3

    print("\n✅ 准予放行：质量、多专业协同、协同闭环门禁均通过。")
    return 0


def _print_coordination_summary(batch) -> None:
    coord = batch.coordination
    if coord is None:
        return
    from .coordination_model import COORD_KIND_CN, STATUS_CN, DISC_CN
    s = coord.summary()
    print("\n-------------- 多专业协同核查 --------------")
    print("专业模型    : "
          + "、".join(f"{DISC_CN.get(d, d)}({sum(1 for f in coord.files if f.discipline == d)})"
                     for d in s["disciplines"]))
    print(f"参与构件    : {s['n_elements']}；预留洞口 {s['n_openings']}")
    print("未闭环问题  :")
    for k in COORD_KIND_CN:
        n = s["active_by_kind"][k]
        if n:
            print(f"  - {COORD_KIND_CN[k]}: {n}")
    if not any(s["active_by_kind"].values()):
        print("  （无）")
    print("工单状态    : " + "，".join(
        f"{STATUS_CN[st]} {s['by_status'][st]}"
        for st in ("open", "fixed", "rejected", "verified", "cleared")
        if s["by_status"].get(st)))
    if coord.owners:
        print("责任人      : " + "，".join(
            f"{DISC_CN[d]}={coord.owners[d]}" for d in coord.owners))
    # 协同门禁未通过项与最终放行结论统一在命令末尾给出，避免重复结论。


def _export_coordination(batch, out_dir, quiet) -> dict[str, str]:
    """导出多专业协同核查产物并把回写结论复制到建筑单体目录。"""
    coord = batch.coordination
    if coord is None:
        return {}
    from . import coordination_report
    base = f"{batch.project}_多专业协同_{batch.batch_id}"
    paths = {
        "协同Excel": coordination_report.export_coordination_excel(
            coord, os.path.join(out_dir, f"{base}.xlsx")),
        "协同工单CSV": coordination_report.export_issues_csv(
            coord, os.path.join(out_dir, f"{base}_工单清单.csv")),
        "协同JSON": coordination_report.export_coordination_json(
            coord, os.path.join(out_dir, f"{base}.json")),
        "建筑侧回写": coordination_report.export_arch_writeback(
            coord, os.path.join(out_dir, f"{base}_建筑侧结论.json")),
    }
    # 回写建筑侧：按建筑单体拆分的结论复制进单体报告目录
    arch_units = {f.unit for f in coord.files if f.discipline == "arch"}
    sub = os.path.join(out_dir, "单体报告")
    if arch_units and os.path.isdir(sub):
        for unit in arch_units:
            safe = _safe_unit_filename(unit)
            wb_path = os.path.join(sub, f"{safe}_多专业协同结论_{batch.batch_id}.json")
            row = next((r for r in coord.arch_writeback.get("per_arch_unit", [])
                        if r["unit"] == unit), None)
            import json as _json
            with open(wb_path, "w", encoding="utf-8") as f:
                _json.dump({"batch": coord.arch_writeback, "unit": row},
                           f, ensure_ascii=False, indent=2, default=str)
            paths[f"回写-{unit}"] = wb_path
    if coord.ledger_path:
        paths["协同台账"] = coord.ledger_path
    return paths


def _run_collab_for_batch(batch, args, out_dir, history_dir, scope=None):
    """把批量核查结果纳入协同问题闭环台账，评估闭环门禁并导出闭环产物。

    返回 (导出路径字典, 闭环门禁是否阻断, 失败规则列表)。只纳管实际检出的
    审查/协同问题；**不**把门禁失败项再建成规则工单（否则会被门禁重复执法，
    同一批次出现循环阻断与重复结论）。规则校验问题可由调用方通过
    :func:`ifc_audit.collab.ingest_rule_violations` 显式纳管。

    ``scope`` 为局部复查范围（单体 / 专业 / 核查项）；范围外与扫描失败的
    工单保留状态，不自动销项。
    """
    from . import collab, collab_report
    from .collab_model import CollabLedger

    profile = getattr(args, "collab_gate_profile", "default")
    gate = collab.for_gate_profile(profile)
    sla = getattr(args, "collab_sla", None)
    sla_hours = gate.fix_sla_hours if sla is None else float(sla)

    ledger_path = collab.default_ledger_path(history_dir, args.project)
    ledger = CollabLedger.load_or_new(ledger_path, args.project)

    def progress(msg):
        if not getattr(args, "quiet", False):
            print(f"[闭环] {msg}", flush=True)

    collab.ingest_batch(
        ledger, batch, sla_hours=sla_hours, scope=scope,
        progress=(progress if not getattr(args, "quiet", False) else None))

    gate_passed, gate_rules = collab.evaluate_collab_gate(
        ledger, gate, batch.batch_id)
    ledger.save(ledger_path)

    if not getattr(args, "quiet", False):
        _print_collab_summary(ledger, gate_passed, gate_rules)

    base = f"{args.project}_协同闭环_{batch.batch_id}"
    paths = {
        "闭环Excel": collab_report.export_collab_excel(
            ledger, os.path.join(out_dir, f"{base}.xlsx"),
            gate_rules=gate_rules, gate_passed=gate_passed,
            batch_id=batch.batch_id),
        "闭环工单CSV": collab_report.export_tickets_csv(
            ledger, os.path.join(out_dir, f"{base}_工单台账.csv")),
        "闭环JSON": collab_report.export_collab_json(
            ledger, os.path.join(out_dir, f"{base}.json"),
            gate_rules=gate_rules, gate_passed=gate_passed,
            batch_id=batch.batch_id),
        "整改回写": collab_report.export_writeback_json(
            ledger, os.path.join(out_dir, f"{base}_整改回写.json"),
            batch_id=batch.batch_id),
        "闭环台账": ledger_path,
    }
    failed = [r for r in gate_rules if not r["passed"]]
    return paths, (not gate_passed), failed


def _print_collab_summary(ledger, gate_passed, gate_rules) -> None:
    from . import collab as _c
    from .collab_model import SOURCE_CN, STATUS_CN, DISC_CN
    s = _c.collab_summary(ledger)
    print("\n-------------- 协同问题闭环 --------------")
    print(f"工单总数    : {s['tickets_total']}（未闭环 {s['tickets_active']} / "
          f"已闭环 {s['tickets_closed']}，闭环率 {s['close_rate'] * 100:.0f}%）")
    src = "；".join(f"{SOURCE_CN[k]} {v['active']}"
                    for k, v in s["by_source"].items() if v["active"])
    print("未闭环按来源: " + (src or "无"))
    disc = "、".join(f"{DISC_CN[d]} {n}" for d, n in
                    s["active_by_owner_discipline"].items() if n)
    print("未闭环按专业: " + (disc or "无"))
    st = "，".join(f"{STATUS_CN[k]} {v}" for k, v in s["by_status"].items() if v)
    print("状态分布    : " + (st or "无"))
    print(f"超期 {s['overdue']} / 升级 {s['escalated']} / "
          f"未指派 {s['no_owner']}；名册成员 {len(ledger.users)} 人")
    ls = s.get("last_scan")
    if ls:
        kind = "局部复查" if ls["scoped"] else "全量复查"
        print(f"最近复查    : {kind}（批次 {ls['batch_id']}，{ls['at']}）"
              f" 范围：{ls['scope']}")
        print(f"  实际扫描单体 {len(ls['scanned_units'])} 个"
              f"（{('、'.join(ls['scanned_units'])) or '无'}）；"
              f"模型版本 {len(ls['model_versions'])} 份已留痕")
        if ls["n_auto_verified"] or ls["n_auto_cleared"]:
            print(f"  自动销项：复核通过 {ls['n_auto_verified']} / "
                  f"已消除 {ls['n_auto_cleared']}（均为成功覆盖且未再检出）")
        if ls["failed_files"]:
            names = "、".join(f"{f['unit'] or f['file']}"
                             for f in ls["failed_files"])
            print(f"  ⚠ 扫描失败模型 {len(ls['failed_files'])} 个（{names}）；"
                  f"相关 {ls['n_scan_failed']} 张工单保留状态")
        if ls["n_out_of_scope"]:
            print(f"  ⚠ 不在复查范围 {ls['n_out_of_scope']} 张，状态保留")
        if not ls["failed_files"] and not ls["n_out_of_scope"]:
            print("  覆盖完整：无范围外 / 失败保留工单")
    # 只列闭环流程治理类失败项；告警项（局部复查未覆盖，不阻断）单独提示
    for r in gate_rules:
        if r.get("advisory"):
            print(f"  ⚠ [闭环·告警] {r['actual']}（{r['limit']}）")
        elif not r["passed"]:
            print(f"  ✗ [闭环] {r['message']}")


def _cmd_trend(args) -> int:
    history = load_project_history(
        args.history or os.path.join("output", "batch_history"), args.project)
    if not history:
        print(f"项目“{args.project}”没有历史批次记录。", file=sys.stderr)
        return 2
    print(f"项目“{args.project}”共 {len(history)} 个批次：\n")
    print(f"{'批次':<18}{'标签':<12}{'规则包':<22}{'时间':<22}"
          f"{'单体':>4}{'问题':>5}{'错误':>5}{'警告':>5}  放行")
    for h in history:
        pack_id = h.get("rule_pack_id") or (h.get("rule_pack") or {}).get("id") \
            or "(内置预设)"
        print(f"{h.get('batch_id', ''):<20}{(h.get('label') or '-'):<12}"
              f"{pack_id:<24}{h.get('created_at', ''):<22}"
              f"{h.get('n_files', h.get('n_units', 0)):>4}"
              f"{h.get('totals', {}).get('issues', h.get('issues', 0)):>5}"
              f"{h.get('totals', {}).get('errors', h.get('errors', 0)):>5}"
              f"{h.get('totals', {}).get('warnings', h.get('warnings', 0)):>5}  "
              f"{'✅' if h.get('gate_passed') else '⛔阻断'}")

    if len(history) >= 2:
        first, last = history[0], history[-1]
        ft, lt = first.get("totals", {}), last.get("totals", {})
        print("\n首末批次对比：")
        for key, label in (("issues", "问题总数"), ("errors", "错误"),
                           ("warnings", "警告"), ("duplicate_groups", "重复构件组"),
                           ("total_net_area", "净面积(m²)"),
                           ("rooms_open", "不闭合房间"),
                           ("opening_anomaly", "尺寸异常门窗"),
                           ("opening_unassigned", "未归属门窗")):
            o, n = ft.get(key, 0), lt.get(key, 0)
            d = n - o
            arrow = "持平" if d == 0 else (f"{'+' if d > 0 else ''}{d:g}")
            print(f"  {label:<12} {o:g} → {n:g}  （{arrow}）")

    if args.output:
        # 用历史快照的精简点复用趋势图渲染
        from types import SimpleNamespace
        fake = SimpleNamespace(
            project=args.project, trend={"history": [
                {k: h.get(k) for k in (
                    "batch_id", "label", "created_at", "gate_passed",
                    "issues", "errors", "warnings", "total_net_area")}
                | {"rule_pack_id": h.get("rule_pack_id")
                   or (h.get("rule_pack") or {}).get("id", "")}
                for h in history]})
        p = batch_report.export_trend_chart(fake, args.output)
        print(f"\n趋势图已导出：{p}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ifc_audit",
        description="IFC 建筑模型核查工具：未闭合墙 / 重复构件 / 房间净面积 / 门窗表",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="核查 IFC 文件并导出报告")
    p_audit.add_argument("ifc", help="IFC 文件路径 (.ifc/.ifcxml/.ifczip)")
    p_audit.add_argument("-o", "--output", default="output", help="输出目录")
    p_audit.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p_audit.add_argument("--fail-on-error", action="store_true",
                         help="存在错误级问题时以退出码 1 返回（便于 CI 集成）")
    p_audit.add_argument(
        "--profile", choices=PROFILES, default="default",
        help="判定阈值预设：default=标准（默认）/ strict=严格 / loose=宽松；"
             "会被配置文件与 --set 覆盖")
    p_audit.add_argument("--config",
                         help="阈值配置 JSON 文件（可用 init-config 生成模板）")
    p_audit.add_argument(
        "--set", dest="set_threshold", action="append", default=[],
        metavar="KEY=VALUE",
                         help="单项覆盖阈值，可重复，长度 mm / 偏差 %%。"
                              "例如 --set gap_min_len_mm=50 --set area_dev_warn_pct=1")
    _add_rule_pack_args(p_audit, auto_flag=True)
    # 单模型自动选择规则包时用项目名匹配；帮助中不突出（主要场景是批量）
    p_audit.add_argument("--project", default="未命名项目", help=argparse.SUPPRESS)
    p_audit.set_defaults(func=_cmd_audit)

    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_gui.set_defaults(func=lambda a: _launch_gui())

    # ---- 多模型批量核查 + 项目质量看板 ----
    p_batch = sub.add_parser(
        "batch",
        help="批量核查多个单体 IFC，按项目/单体/楼层汇总并生成质量看板；"
             "门禁不达标时退出码 3 阻断放行")
    p_batch.add_argument("paths", nargs="+",
                         help="IFC 文件或目录（目录取其中 .ifc/.ifcxml/.ifczip），"
                              "可混合传入多个")
    p_batch.add_argument("--project", default="未命名项目", help="项目名（看板与历史归档用）")
    p_batch.add_argument("--label", default="", help="批次标签，如 “v1提模” / “竣工审查”")
    p_batch.add_argument("-o", "--output", default=os.path.join("output", "batch"),
                         help="批次报告输出目录（默认 output/batch/）")
    p_batch.add_argument("--history", default=None,
                         help="批次历史留存目录（默认 output/batch_history/）")
    p_batch.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p_batch.add_argument("--with-3d", action="store_true",
                         help="同时导出每个单体的三维标注图（默认仅平面图，批量更快）")
    p_batch.add_argument("--no-unit-reports", action="store_true",
                         help="不导出单体 Excel/CSV/平面图，只出批次报告与看板")
    # 核查阈值（与 audit 子命令一致）
    p_batch.add_argument("--profile", choices=PROFILES, default="default",
                         help="单模型核查阈值预设：default/strict/loose")
    p_batch.add_argument("--config", help="核查阈值配置 JSON（init-config 生成）")
    p_batch.add_argument("--set", dest="set_threshold", action="append",
                         default=[], metavar="KEY=VALUE",
                         help="单项覆盖核查阈值，可重复")
    # 放行门禁
    p_batch.add_argument("--gate-profile", choices=GATE_PROFILES, default="default",
                         help="放行门禁预设：default=标准（默认）/ strict=严格 / "
                              "loose=宽松 / none=不设门禁不阻断")
    p_batch.add_argument("--gate-config", help="门禁配置 JSON（init-gate 生成）")
    p_batch.add_argument("--gate-set", action="append", default=[],
                         metavar="KEY=VALUE",
                         help="单项覆盖门禁规则，可重复，如 "
                              "--gate-set unit_max_warnings=20")
    p_batch.add_argument("--no-gate", action="store_true",
                         help="本次不启用门禁（等同 --gate-profile none，仅统计不阻断）")
    # 企业审查规则包（发布后的规则包按项目/阶段自动匹配，也可显式指定）
    p_batch.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                         help=f"企业规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_batch.add_argument("--rule-pack", default=None,
                         metavar="名称[@版本]|快照.json",
                         help="显式指定企业规则包；不给定时按 --project/--stage "
                              "从规则库自动选择适用的已发布规则包")
    p_batch.add_argument("--stage", choices=STAGES, default="",
                         help="项目阶段（自动选择规则包用）："
                              + " / ".join(f"{k}={v}" for k, v in STAGE_CN.items()))
    p_batch.add_argument("--no-rule-pack", dest="no_rule_pack",
                         action="store_true",
                         help="即使规则库中存在适用规则包也不使用，"
                              "改用 --profile/--gate-profile 等普通参数")
    # 多专业协同核查
    p_batch.add_argument("--coord", dest="coord_mode", action="store_const",
                         const="auto", default="auto",
                         help="多专业协同核查（默认）：文件名识别出机电+建筑/结构"
                              "专业模型时自动执行碰撞与预留洞口核对")
    p_batch.add_argument("--no-coord", dest="coord_mode",
                         action="store_const", const="off",
                         help="关闭多专业协同核查")
    p_batch.add_argument("--require-coord", dest="coord_mode",
                         action="store_const", const="required",
                         help="强制多专业协同核查；识别不出多专业时直接报错退出")
    p_batch.add_argument("--owner", action="append", default=[],
                         metavar="专业=姓名",
                         help="协同问题责任人，可重复：--owner struct=张工 "
                              "--owner mep=李工 --owner arch=王工")
    p_batch.add_argument("--discipline", action="append", default=[],
                         metavar="单体=专业",
                         help="显式指定单体专业，可重复，如 "
                              "--discipline 1号楼结构=struct；专业取值 "
                              "arch/struct/mep")
    p_batch.add_argument("--coord-gate-profile",
                         choices=("default", "strict", "loose", "none"),
                         default="default",
                         help="协同门禁预设：default=错误类零容忍、时限 72h（默认）/ "
                              "strict=全部清零且工单到人、时限 48h、超期零容忍 / "
                              "loose=时限 168h / none")
    p_batch.add_argument("--coord-gate-set", action="append", default=[],
                         metavar="KEY=VALUE",
                         help="单项覆盖协同门禁，可重复，如 "
                              "--coord-gate-set coord_max_mismatch_active=5；"
                              "整改时限 coord_fix_sla_hours=24、"
                              "超期工单数 coord_max_overdue_active=0")
    p_batch.add_argument("--coord-set", action="append", default=[],
                         metavar="KEY=VALUE",
                         help="单项覆盖协同检测参数（毫米），可重复，如 "
                              "--coord-set opening_pos_tol_mm=150")
    p_batch.add_argument("--model-root", default="", metavar="目录",
                         help="模型根锚点目录：稳定单体标识取相对该根的路径"
                              "（去后缀），用于区分不同目录下的同名模型"
                              "（如 A区/楼A、B区/楼A）。不给定时首次按本批"
                              "文件公共父目录自动确定并固化到项目历史，之后"
                              "跨批次沿用，保证单体身份一致")
    # 协同问题闭环（统一纳管批量审查 + 协同 + 规则校验问题）
    p_batch.add_argument("--collab", dest="collab_mode", action="store_const",
                         const="on", default="on",
                         help="协同问题闭环（默认）：把批量审查/多专业协同/规则"
                              "校验问题统一纳管，分派、流转、整改回写、通知与门禁联动")
    p_batch.add_argument("--no-collab", dest="collab_mode",
                         action="store_const", const="off",
                         help="关闭协同问题闭环纳管（仅出原核查/协同报告）")
    p_batch.add_argument("--collab-gate-profile",
                         choices=("default", "strict", "loose", "none"),
                         default="default",
                         help="闭环门禁只管流程治理：default=仅卡超期（默认，"
                              "问题数量交质量/协同门禁不重复阻断）/ "
                              "strict=全部清零、工单到人、回写齐全、超期零容忍 / "
                              "loose=方案阶段 / none=不阻断")
    p_batch.add_argument("--collab-sla", type=float, default=None,
                         metavar="小时",
                         help="闭环工单整改时限（小时），默认取闭环门禁预设值")
    # 局部复查：按单体 / 专业 / 核查项圈定本次复查范围
    p_batch.add_argument("--recheck-unit", action="append", default=[],
                         metavar="单体名",
                         help="局部复查：仅复查指定单体（可重复）；范围外工单"
                              "保留状态不自动销项。不给=全部输入单体")
    p_batch.add_argument("--recheck-discipline", action="append", default=[],
                         metavar="专业",
                         help="局部复查：仅复查指定专业 arch/struct/mep（可重复）")
    p_batch.add_argument("--recheck-kind", action="append", default=[],
                         metavar="核查项",
                         help="局部复查：仅复查指定核查项（可重复）："
                              "wall=墙体闭合 / duplicate=重复构件 / "
                              "room=房间净面积 / opening=门窗规格，"
                              "也可用具体 kind 如 wall_free_end")
    p_batch.set_defaults(use_rule_pack=True, func=_cmd_batch)

    # ---- 批次历史趋势 ----
    p_trend = sub.add_parser(
        "trend", help="查看项目历史批次趋势对比，可选导出趋势图 PNG")
    p_trend.add_argument("--project", required=True, help="项目名")
    p_trend.add_argument("--history", default=None,
                         help="批次历史目录（默认 output/batch_history/）")
    p_trend.add_argument("-o", "--output", default=None,
                         help="趋势图 PNG 输出路径（不给则只在控制台列出）")
    p_trend.set_defaults(func=_cmd_trend)

    # ---- 多专业协同核查（独立运行 / 工单流转）----
    p_coord = sub.add_parser(
        "coord",
        help="多专业协同核查：建筑/结构/机电碰撞与预留洞口核对、派单整改复核")
    coord_sub = p_coord.add_subparsers(dest="coord_command", required=True)

    def _add_common_coord_inputs(p, with_gate=True):
        p.add_argument("paths", nargs="+",
                       help="各专业 IFC 文件 / 目录（文件名含 建筑/结构/机电 "
                            "关键词，或用 --discipline 指定）")
        p.add_argument("--project", default="未命名项目", help="项目名（台账按项目归档）")
        p.add_argument("--label", default="", help="批次标签")
        p.add_argument("--discipline", action="append", default=[],
                       metavar="单体=专业",
                       help="显式指定单体专业，可重复（arch/struct/mep）")
        p.add_argument("--owner", action="append", default=[],
                       metavar="专业=姓名", help="责任人，可重复")
        p.add_argument("--coord-set", action="append", default=[],
                       metavar="KEY=VALUE", help="检测参数覆盖（毫米），可重复")
        if with_gate:
            p.add_argument("--coord-gate-profile",
                           choices=("default", "strict", "loose", "none"),
                           default="default", help="协同门禁预设")
            p.add_argument("--coord-gate-set", action="append", default=[],
                           metavar="KEY=VALUE",
                           help="协同门禁单项覆盖，可重复；整改时限 "
                                "coord_fix_sla_hours=72，超期工单数 "
                                "coord_max_overdue_active=0")

    p_cr = coord_sub.add_parser("run", help="执行多专业协同核查并导出报告")
    _add_common_coord_inputs(p_cr)
    p_cr.add_argument("-o", "--output", default=os.path.join("output", "coord"),
                      help="输出目录（默认 output/coord/）")
    p_cr.add_argument("--ledger", default=None,
                      help="协同台账 JSON 路径（默认按项目归档在批次历史目录）")
    p_cr.add_argument("--history", default=None,
                      help="台账归档根目录（默认 output/batch_history/）")
    p_cr.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p_cr.set_defaults(func=_cmd_coord_run)

    p_cl = coord_sub.add_parser("list", help="列出项目协同台账工单")
    p_cl.add_argument("--project", required=True, help="项目名")
    p_cl.add_argument("--ledger", default=None, help="台账 JSON 路径")
    p_cl.add_argument("--history", default=None,
                      help="台账归档根目录（默认 output/batch_history/）")
    p_cl.add_argument("--status", default="",
                      help="按状态过滤：open/fixed/rejected/verified/cleared/active")
    p_cl.add_argument("--discipline", default="",
                      help="按责任专业过滤：arch/struct/mep")
    p_cl.set_defaults(func=_cmd_coord_list)

    def _add_ticket_args(p, need_note=False):
        p.add_argument("issue_id", help="工单编号（COORD-xxxx）或指纹")
        p.add_argument("--project", required=True, help="项目名")
        p.add_argument("--by", default="", help="操作人")
        p.add_argument("--note", default="", required=need_note,
                       help="说明 / 整改记录 / 驳回原因")
        p.add_argument("--ledger", default=None, help="台账 JSON 路径")
        p.add_argument("--history", default=None,
                       help="台账归档根目录（默认 output/batch_history/）")

    p_ca = coord_sub.add_parser("assign", help="派单 / 改派责任人")
    _add_ticket_args(p_ca)
    p_ca.add_argument("--owner", default="", help="责任人姓名")
    p_ca.add_argument("--owner-discipline", default="",
                      choices=("arch", "struct", "mep"),
                      help="改派责任专业（不改则省略）")
    p_ca.set_defaults(func=_cmd_coord_assign)

    p_cf = coord_sub.add_parser("fix", help="责任专业报整改完成（进入待复核）")
    _add_ticket_args(p_cf, need_note=True)
    p_cf.set_defaults(func=_cmd_coord_fix)

    p_cv = coord_sub.add_parser("verify", help="发起专业复核通过")
    _add_ticket_args(p_cv)
    p_cv.set_defaults(func=_cmd_coord_verify)

    p_cx = coord_sub.add_parser("reject", help="复核驳回，退回整改（必须 --note）")
    _add_ticket_args(p_cx, need_note=True)
    p_cx.set_defaults(func=_cmd_coord_reject)

    # ---- 协同问题闭环（统一纳管审查/协同/规则/人工问题）----
    p_cb = sub.add_parser(
        "collab",
        help="协同问题闭环：跨专业问题分派、定位、流转、整改回写、"
             "报告汇总、通知与角色权限")
    cb_sub = p_cb.add_subparsers(dest="collab_command", required=True)

    def _add_cb_project(p):
        p.add_argument("--project", required=True, help="项目名（闭环台账按项目归档）")
        p.add_argument("--ledger", default=None, help="闭环台账 JSON 路径")
        p.add_argument("--history", default=None,
                       help="台账归档根目录（默认 output/batch_history/）")

    p_cb_ls = cb_sub.add_parser("list", help="列出闭环工单（可按来源/状态/专业过滤）")
    _add_cb_project(p_cb_ls)
    p_cb_ls.add_argument("--status", default="",
                         help="open/fixed/rejected/verified/cleared/closed/active")
    p_cb_ls.add_argument("--discipline", default="", help="arch/struct/mep")
    p_cb_ls.add_argument("--source", default="",
                         help="audit/coord/rule/manual")
    p_cb_ls.add_argument("--mine", default="", metavar="姓名",
                         help="只看某责任人名下工单")
    p_cb_ls.set_defaults(func=_cmd_collab_list)

    p_cb_show = cb_sub.add_parser("show", help="查看工单详情与完整流转记录")
    _add_cb_project(p_cb_show)
    p_cb_show.add_argument("ticket_id", help="工单编号（COLL-xxxx）或指纹")
    p_cb_show.set_defaults(func=_cmd_collab_show)

    p_cb_loc = cb_sub.add_parser("locate", help="跨模型定位：输出工单涉及的模型文件与构件 GlobalId")
    _add_cb_project(p_cb_loc)
    p_cb_loc.add_argument("ticket_id", help="工单编号（COLL-xxxx）或指纹")
    p_cb_loc.set_defaults(func=_cmd_collab_locate)

    p_cb_open = cb_sub.add_parser("open", help="人工登记会审/现场问题")
    _add_cb_project(p_cb_open)
    p_cb_open.add_argument("title", help="问题标题")
    p_cb_open.add_argument("--detail", default="", help="问题详细说明")
    p_cb_open.add_argument("--by", required=True, help="登记人（需名册中有相应权限）")
    p_cb_open.add_argument("--owner-discipline", default="",
                          choices=("arch", "struct", "mep"), help="责任专业")
    p_cb_open.add_argument("--owner", default="", help="责任人姓名")
    p_cb_open.add_argument("--severity", default="warning",
                          choices=("error", "warning", "info"))
    p_cb_open.add_argument("--unit", default="", help="单体名")
    p_cb_open.add_argument("--storey", default="", help="楼层")
    p_cb_open.add_argument("--gid", action="append", default=[],
                          help="关联构件 GlobalId，可重复")
    p_cb_open.add_argument("--sla", type=float, default=0.0, help="整改时限（小时）")
    p_cb_open.add_argument("--note", default="", help="备注")
    p_cb_open.set_defaults(func=_cmd_collab_open)

    def _add_cb_ticket(p, need_note=False, note_help="说明"):
        p.add_argument("ticket_id", help="工单编号（COLL-xxxx）或指纹")
        _add_cb_project(p)
        p.add_argument("--by", required=True, help="操作人")
        p.add_argument("--note", default="", required=need_note, help=note_help)

    p_cb_as = cb_sub.add_parser("assign", help="派单 / 改派责任人")
    _add_cb_ticket(p_cb_as)
    p_cb_as.add_argument("--owner", default="", help="责任人姓名")
    p_cb_as.add_argument("--owner-discipline", default="",
                         choices=("arch", "struct", "mep"), help="改派责任专业")
    p_cb_as.set_defaults(func=_cmd_collab_assign)

    p_cb_fix = cb_sub.add_parser("fix", help="报整改完成（进入待复核）")
    _add_cb_ticket(p_cb_fix, need_note=True, note_help="整改说明")
    p_cb_fix.set_defaults(func=_cmd_collab_fix)

    p_cb_wb = cb_sub.add_parser("writeback", help="整改回写（回填整改说明/整改构件，不改状态）")
    _add_cb_ticket(p_cb_wb, need_note=True, note_help="整改回写说明")
    p_cb_wb.add_argument("--gid", action="append", default=[],
                         help="整改后构件 GlobalId，可重复")
    p_cb_wb.set_defaults(func=_cmd_collab_writeback)

    p_cb_vf = cb_sub.add_parser("verify", help="复核通过，闭环工单")
    _add_cb_ticket(p_cb_vf)
    p_cb_vf.set_defaults(func=_cmd_collab_verify)

    p_cb_rj = cb_sub.add_parser("reject", help="复核驳回（必须 --note 填写原因）")
    _add_cb_ticket(p_cb_rj, need_note=True, note_help="驳回原因")
    p_cb_rj.add_argument("--sla", type=float, default=72.0,
                         help="驳回后重排的整改时限（小时，默认 72）")
    p_cb_rj.set_defaults(func=_cmd_collab_reject)

    p_cb_cl = cb_sub.add_parser("close", help="人工关闭（会审销项/设计豁免，必须 --reason）")
    _add_cb_project(p_cb_cl)
    p_cb_cl.add_argument("ticket_id", help="工单编号（COLL-xxxx）或指纹")
    p_cb_cl.add_argument("--by", required=True, help="操作人")
    p_cb_cl.add_argument("--reason", required=True, help="关闭原因")
    p_cb_cl.set_defaults(func=_cmd_collab_close)

    # 名册与权限
    p_cb_usr = cb_sub.add_parser("user", help="名册：新增/修改项目成员与角色权限")
    _add_cb_project(p_cb_usr)
    p_cb_usr.add_argument("name", help="成员姓名")
    p_cb_usr.add_argument("--by", required=True, help="操作人（需项目协调角色）")
    p_cb_usr.add_argument("--role", required=True,
                          choices=("coordinator", "design_lead", "struct_lead",
                                   "mep_lead", "responsible", "reviewer", "viewer"),
                          help="角色：coordinator=项目协调 / *_lead=专业负责人 / "
                               "responsible=责任人 / reviewer=复核人 / viewer=只读")
    p_cb_usr.add_argument("--discipline", default="",
                          choices=("arch", "struct", "mep"), help="所属专业")
    p_cb_usr.add_argument("--remove", action="store_true", help="从名册移除")
    p_cb_usr.add_argument("--no-notify", action="store_true", help="该成员退订通知")
    p_cb_usr.set_defaults(func=_cmd_collab_user)

    p_cb_users = cb_sub.add_parser("users", help="列出名册成员与角色")
    _add_cb_project(p_cb_users)
    p_cb_users.set_defaults(func=_cmd_collab_users)

    # 通知
    p_cb_note = cb_sub.add_parser("notifications", help="查看某人的通知（默认只看未读）")
    _add_cb_project(p_cb_note)
    p_cb_note.add_argument("name", help="成员姓名")
    p_cb_note.add_argument("--all", action="store_true", help="包含已读")
    p_cb_note.add_argument("--mark-read", default="", metavar="通知号",
                           help="把指定通知标记已读（N-xxxxx）")
    p_cb_note.add_argument("--read-all", action="store_true", help="全部标记已读")
    p_cb_note.set_defaults(func=_cmd_collab_notifications)

    # 报告
    p_cb_rep = cb_sub.add_parser("report", help="导出闭环报告（Excel/CSV/JSON/回写）")
    _add_cb_project(p_cb_rep)
    p_cb_rep.add_argument("-o", "--output",
                          default=os.path.join("output", "collab"),
                          help="输出目录（默认 output/collab/）")
    p_cb_rep.add_argument("--gate-profile",
                          choices=("default", "strict", "loose", "none"),
                          default="default", help="导出时评估的闭环门禁预设")
    p_cb_rep.set_defaults(func=_cmd_collab_report)

    p_init = sub.add_parser(
        "init-config", help="生成带说明的阈值配置文件模板（JSON）")
    p_init.add_argument("path", help="配置文件输出路径，如 thresholds.json")
    p_init.add_argument("--profile", choices=PROFILES, default="default",
                        help="模板以哪套预设值为初始值（默认 default）")
    p_init.set_defaults(func=_cmd_init_config)

    p_init_gate = sub.add_parser(
        "init-gate", help="生成带说明的项目放行门禁配置模板（JSON）")
    p_init_gate.add_argument("path", help="配置文件输出路径，如 gate.json")
    p_init_gate.add_argument("--profile", choices=GATE_PROFILES, default="default",
                             help="模板以哪套门禁预设为初始值（默认 default）")
    p_init_gate.set_defaults(func=_cmd_init_gate)

    # ---- 企业审查规则库 ----
    p_rp = sub.add_parser(
        "rulepack", help="企业审查规则库：规则包草稿 / 发布 / 版本 / 适用范围")
    rp_sub = p_rp.add_subparsers(dest="rulepack_command", required=True)

    p_rp_list = rp_sub.add_parser("list", help="列出规则库内全部规则包与版本")
    p_rp_list.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                           help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rp_list.set_defaults(func=_cmd_rulepack_list)

    p_rp_init = rp_sub.add_parser(
        "init", help="在规则库中创建规则包草稿（核查项全开，可再编辑）")
    p_rp_init.add_argument("name", help="规则包名称，如 住宅施工图审查规则")
    p_rp_init.add_argument("--description", default="", help="规则包用途说明")
    p_rp_init.add_argument("--project", action="append", default=[],
                           help="适用项目名，可重复；不给则适用全部项目")
    p_rp_init.add_argument("--stage", choices=STAGES, action="append", default=[],
                           help="适用阶段，可重复；不给则适用全部阶段")
    p_rp_init.add_argument("--profile", choices=PROFILES, default="default",
                           help="以哪套阈值预设为草稿初始值（默认 default）")
    p_rp_init.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                           help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rp_init.set_defaults(func=_cmd_rulepack_init)

    p_rp_pub = rp_sub.add_parser(
        "publish", help="发布草稿为不可变版本快照（同名同版本不可重复发布）")
    p_rp_pub.add_argument("source",
                          help="库内规则包名（取其草稿）或草稿 JSON 文件路径")
    p_rp_pub.add_argument("version", help="语义化版本号，如 1.0.0")
    p_rp_pub.add_argument("--as-name", default=None,
                          help="从库外草稿文件发布时指定规则包名称")
    p_rp_pub.add_argument("--by", default="", help="发布人（记录在快照中）")
    p_rp_pub.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                          help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rp_pub.set_defaults(func=_cmd_rulepack_publish)

    p_rp_show = rp_sub.add_parser("show", help="查看规则包内容（核查项/阈值/门禁）")
    p_rp_show.add_argument("spec", help="规则包名称、名称@版本或快照 JSON 路径")
    p_rp_show.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                           help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rp_show.set_defaults(func=_cmd_rulepack_show)

    p_rp_dep = rp_sub.add_parser(
        "deprecate", help="标记某版本废止（不参与自动选择；快照不删除）")
    p_rp_dep.add_argument("name", help="规则包名称")
    p_rp_dep.add_argument("version", help="版本号")
    p_rp_dep.add_argument("--undo", action="store_true", help="取消废止标记")
    p_rp_dep.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                          help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rp_dep.set_defaults(func=_cmd_rulepack_deprecate)

    p_rp_tpl = rp_sub.add_parser(
        "template", help="在任意目录生成带说明的规则包草稿模板 JSON")
    p_rp_tpl.add_argument("path", help="模板输出路径，如 rules.json")
    p_rp_tpl.add_argument("--name", default="企业审查规则包", help="规则包名称")
    p_rp_tpl.add_argument("--project", action="append", default=[],
                          help="适用项目名，可重复；不给则全部项目")
    p_rp_tpl.add_argument("--stage", choices=STAGES, action="append", default=[],
                          help="适用阶段，可重复；不给则全部阶段")
    p_rp_tpl.add_argument("--profile", choices=PROFILES, default="default",
                          help="以哪套阈值预设为初始值（默认 default）")
    p_rp_tpl.set_defaults(func=_cmd_rulepack_template)

    # ---- 规则版本切换（试算 → 确认 → 联动）----
    p_rs = sub.add_parser(
        "ruleswitch",
        help="规则版本切换：对同一批模型按新旧规则包重算试算，"
             "区分规则调整与模型整改影响；确认后联动项目规则选择、"
             "放行门禁与趋势统计")
    rs_sub = p_rs.add_subparsers(dest="ruleswitch_command", required=True)

    def _add_rs_common(p):
        p.add_argument("--project", required=True, help="项目名")
        p.add_argument("--history", default=None,
                       help="批次历史目录（默认 output/batch_history/）")

    p_rs_cmp = rs_sub.add_parser(
        "compare",
        help="试算：对上一批次同一批模型按新旧规则包各重算一次，"
             "输出纯规则调整引起的问题变化与门禁结论变化（不影响趋势）")
    _add_rs_common(p_rs_cmp)
    p_rs_cmp.add_argument("--to", required=True,
                          metavar="名称[@版本]|快照.json",
                          help="新规则包（切换目标版本）")
    p_rs_cmp.add_argument("--from-pack", default=None,
                          metavar="名称[@版本]|快照.json",
                          help="旧规则包；默认取原始批次快照记录的版本")
    p_rs_cmp.add_argument("--from-batch", default=None,
                          help="原始批次号（默认项目最新批次）")
    p_rs_cmp.add_argument("--by", default="", help="试算操作人")
    p_rs_cmp.add_argument("--rule-lib", default=DEFAULT_RULE_LIBRARY,
                          help=f"规则库目录（默认 {DEFAULT_RULE_LIBRARY}）")
    p_rs_cmp.add_argument("-o", "--output",
                          default=os.path.join("output", "ruleswitch"),
                          help="对比明细 JSON 输出目录（默认 output/ruleswitch/）")
    p_rs_cmp.add_argument("-q", "--quiet", action="store_true", help="精简输出")
    p_rs_cmp.set_defaults(func=_cmd_ruleswitch_compare)

    p_rs_ok = rs_sub.add_parser(
        "confirm",
        help="确认切换：联动更新项目规则选择 / 放行门禁 / 趋势基线，"
             "原始批次与规则版本保留可追溯")
    _add_rs_common(p_rs_ok)
    p_rs_ok.add_argument("--switch", default=None,
                         help="试算记录编号（默认最新的待确认记录）")
    p_rs_ok.add_argument("--by", default="", help="确认人")
    p_rs_ok.set_defaults(func=_cmd_ruleswitch_confirm)

    p_rs_ls = rs_sub.add_parser("list", help="列出项目的规则切换试算记录")
    _add_rs_common(p_rs_ls)
    p_rs_ls.set_defaults(func=_cmd_ruleswitch_list)

    p_rs_show = rs_sub.add_parser("show", help="查看某次试算的完整对比明细")
    _add_rs_common(p_rs_show)
    p_rs_show.add_argument("switch", help="试算记录编号（RS…）")
    p_rs_show.set_defaults(func=_cmd_ruleswitch_show)

    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------- 协同核查 CLI ----

def _coord_ledger_path(args) -> str:
    from .coordination import default_ledger_path
    if getattr(args, "ledger", None):
        return args.ledger
    history = getattr(args, "history", None) \
        or os.path.join("output", "batch_history")
    return default_ledger_path(history, args.project)


def _load_coord_ledger(args):
    from .coordination import CoordWorkflowError
    from .coordination_model import CoordinationLedger
    path = _coord_ledger_path(args)
    if not os.path.exists(path):
        raise CoordWorkflowError(
            f"项目“{args.project}”还没有协同台账：{path}；"
            "请先运行 `coord run` 完成一次多专业协同核查")
    return CoordinationLedger.load(path), path


def _save_ledger_and_exit(ledger, path, message) -> int:
    ledger.save(path)
    print(message)
    print(f"台账已更新：{path}")
    return 0


def _cmd_coord_run(args) -> int:
    from .coordination import (
        run_coordination, resolve_settings, parse_coord_gate_items,
        parse_owner_items, CoordinationConfigError,
    )
    from .coordination_model import COORD_KIND_CN, STATUS_CN, DISC_CN
    from . import coordination_report

    out_dir = args.output
    history_dir = args.history or os.path.join("output", "batch_history")
    try:
        owners = parse_owner_items(args.owner)
        discipline_map = _parse_disciplines(args.discipline)
        settings = resolve_settings(
            {kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip()
             for kv in args.coord_set} if args.coord_set else None)
        gate_overrides = parse_coord_gate_items(args.coord_gate_set)
        ledger_path = args.ledger
        if ledger_path is None:
            from .coordination import default_ledger_path
            ledger_path = default_ledger_path(history_dir, args.project)
        result = run_coordination(
            args.paths,
            project=args.project, label=args.label or "",
            discipline_map=discipline_map or None,
            owners=owners, settings=settings,
            gate_profile=args.coord_gate_profile,
            gate_overrides=gate_overrides or None,
            ledger_path=ledger_path,
            progress=(None if args.quiet else
                      lambda p, m: print(f"[{p:3d}%] {m}", flush=True)))
    except CoordinationConfigError as exc:
        print(f"协同核查配置错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"协同核查失败：{exc}", file=sys.stderr)
        return 2

    os.makedirs(out_dir, exist_ok=True)
    base = f"{args.project}_多专业协同_{result.batch_id}"
    xlsx = coordination_report.export_coordination_excel(
        result, os.path.join(out_dir, f"{base}.xlsx"))
    csvp = coordination_report.export_issues_csv(
        result, os.path.join(out_dir, f"{base}_工单清单.csv"))
    jpath = coordination_report.export_coordination_json(
        result, os.path.join(out_dir, f"{base}.json"))
    wb = coordination_report.export_arch_writeback(
        result, os.path.join(out_dir, f"{base}_建筑侧结论.json"))

    if not args.quiet:
        s = result.summary()
        print("\n============== 多专业协同核查 ==============")
        print(f"项目 / 批次: {result.project} / {result.batch_id}")
        print("专业模型    : " + "、".join(
            f"{DISC_CN.get(d, d)}({sum(1 for f in result.files if f.discipline == d)})"
            for d in s["disciplines"]))
        print(f"问题总数    : {s['issues_total']}，未闭环 {s['issues_active']}")
        for k, cn in COORD_KIND_CN.items():
            print(f"  {cn}: 未闭环 {s['active_by_kind'][k]}"
                  f" / 全部 {s['by_kind'][k]}")
        print("工单状态    : " + "，".join(
            f"{STATUS_CN[st]} {s['by_status'][st]}"
            for st in ("open", "fixed", "rejected", "verified", "cleared")
            if s["by_status"].get(st)))
        sla_hours = result.settings.get("fix_sla_hours", 0)
        print(f"整改时限    : {sla_hours:g}h（派单→整改），"
              f"超期未整改 {s.get('issues_overdue', 0)}，"
              f"已自动升级 {s.get('issues_escalated', 0)}")
        print("\n---------------- 未闭环工单 ----------------")
        for i in result.issues:
            if i.active:
                esc = "　⛔已升级" if (i.escalated and i.sla_tracked) else ""
                print(f"{i.issue_id} [{STATUS_CN[i.status]}|{i.sla_status_cn()}"
                      f"{esc}] "
                      f"{COORD_KIND_CN.get(i.kind, i.kind)} | {i.title}"
                      f"  → 责任：{DISC_CN.get(i.owner_discipline, i.owner_discipline)}"
                      f"/{i.owner or '未指派'}")
        for r in result.gate_rules:
            if not r["passed"]:
                print(f"✗ {r['message']}")

    print("\n---------------- 导出文件 ----------------")
    for label, p in (("协同Excel", xlsx), ("工单CSV", csvp),
                     ("协同JSON", jpath), ("建筑侧回写", wb),
                     ("协同台账", result.ledger_path)):
        print(f"{label:<10}: {p}")
    print("\n协同结论：" + ("✅ 协同门禁通过"
                            if result.gate_passed
                            else "⛔ 协同门禁未通过，阻断批次放行"))
    return 3 if not result.gate_passed else 0


def _cmd_coord_list(args) -> int:
    from .coordination import CoordWorkflowError
    from .coordination_model import COORD_KIND_CN, STATUS_CN, DISC_CN
    try:
        ledger, path = _load_coord_ledger(args)
    except CoordWorkflowError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    issues = list(ledger.issues.values())
    filt = args.status
    if filt == "active":
        issues = [i for i in issues if i.active]
    elif filt:
        issues = [i for i in issues if i.status == filt]
    if args.discipline:
        issues = [i for i in issues if i.owner_discipline == args.discipline]
    issues.sort(key=lambda i: i.issue_id)
    print(f"项目“{args.project}”协同台账：{path}（共 {len(issues)} 条）\n")
    print(f"{'工单':<12}{'状态':<7}{'时限/升级':<15}{'类型':<15}"
          f"{'责任':<8}{'责任人':<7}标题")
    for i in issues:
        due = (i.due_at or "").replace("T", " ")[:16]
        sla = i.sla_status_cn()
        if i.escalated and i.sla_tracked:
            sla += "/升级"
        print(f"{i.issue_id:<12}{STATUS_CN.get(i.status, i.status):<7}"
              f"{sla:<15}{COORD_KIND_CN.get(i.kind, i.kind):<15}"
              f"{DISC_CN.get(i.owner_discipline, i.owner_discipline):<8}"
              f"{i.owner or '-':<7}{i.title}"
              + (f"（截止 {due}）" if due and i.sla_tracked else ""))
    return 0


def _cmd_coord_assign(args) -> int:
    from .coordination import assign_issue, CoordWorkflowError, CoordinationConfigError
    try:
        ledger, path = _load_coord_ledger(args)
        issue = assign_issue(ledger, args.issue_id, args.owner,
                             discipline=args.owner_discipline,
                             by=args.by, note=args.note)
    except (CoordWorkflowError, CoordinationConfigError) as exc:
        print(f"派单失败：{exc}", file=sys.stderr)
        return 2
    from .coordination_model import DISC_CN
    return _save_ledger_and_exit(
        ledger, path,
        f"已派单 {issue.issue_id} → {DISC_CN.get(issue.owner_discipline, issue.owner_discipline)}"
        f"/{issue.owner or '未指派'}（{issue.title}）")


def _cmd_coord_fix(args) -> int:
    from .coordination import fix_issue, CoordWorkflowError
    try:
        ledger, path = _load_coord_ledger(args)
        issue = fix_issue(ledger, args.issue_id, by=args.by or "责任专业",
                          note=args.note)
    except CoordWorkflowError as exc:
        print(f"整改上报失败：{exc}", file=sys.stderr)
        return 2
    return _save_ledger_and_exit(
        ledger, path,
        f"工单 {issue.issue_id} 已报整改完成，进入待复核：{args.note}")


def _cmd_coord_verify(args) -> int:
    from .coordination import verify_issue, CoordWorkflowError
    try:
        ledger, path = _load_coord_ledger(args)
        issue = verify_issue(ledger, args.issue_id, by=args.by or "复核人",
                             note=args.note or "复核通过")
    except CoordWorkflowError as exc:
        print(f"复核失败：{exc}", file=sys.stderr)
        return 2
    return _save_ledger_and_exit(
        ledger, path, f"工单 {issue.issue_id} 复核通过，已闭环。")


def _cmd_coord_reject(args) -> int:
    from .coordination import reject_issue, CoordWorkflowError
    try:
        ledger, path = _load_coord_ledger(args)
        issue = reject_issue(ledger, args.issue_id, by=args.by or "复核人",
                             note=args.note)
    except CoordWorkflowError as exc:
        print(f"驳回失败：{exc}", file=sys.stderr)
        return 2
    return _save_ledger_and_exit(
        ledger, path,
        f"工单 {issue.issue_id} 已驳回并退回整改：{args.note}")


# ---------------------------------------------------- 协同闭环 CLI ----

def _collab_ledger_path(args) -> str:
    from .collab import default_ledger_path
    if getattr(args, "ledger", None):
        return args.ledger
    history = getattr(args, "history", None) \
        or os.path.join("output", "batch_history")
    return default_ledger_path(history, args.project)


def _load_collab_ledger(args, create: bool = False):
    from .collab_model import CollabLedger
    path = _collab_ledger_path(args)
    if not os.path.exists(path):
        if create:
            return CollabLedger(project=args.project), path
        from .collab import CollabError
        raise CollabError(
            f"项目“{args.project}”还没有协同闭环台账：{path}；"
            "请先运行一次 `batch`（默认自动纳管）或 `collab open` 登记问题")
    return CollabLedger.load(path), path


def _save_collab(ledger, path, message) -> int:
    ledger.save(path)
    print(message)
    print(f"闭环台账已更新：{path}")
    return 0


def _cmd_collab_list(args) -> int:
    from . import collab
    from .collab_model import SOURCE_CN, STATUS_CN, DISC_CN
    try:
        ledger, path = _load_collab_ledger(args)
    except collab.CollabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    tickets = list(ledger.tickets.values())
    if args.status == "active":
        tickets = [t for t in tickets if t.active]
    elif args.status:
        tickets = [t for t in tickets if t.status == args.status]
    if args.discipline:
        tickets = [t for t in tickets if t.owner_discipline == args.discipline]
    if args.source:
        tickets = [t for t in tickets if t.source == args.source]
    if args.mine:
        tickets = [t for t in tickets if t.owner == args.mine]
    tickets.sort(key=lambda t: (not t.active, t.ticket_id))
    print(f"项目“{args.project}”闭环台账：{path}（筛选出 {len(tickets)} 条）\n")
    print(f"{'工单':<11}{'来源':<8}{'状态':<7}{'时限':<10}{'严重':<6}"
          f"{'责任专业':<6}{'责任人':<8}标题")
    for t in tickets:
        print(f"{t.ticket_id:<11}{SOURCE_CN.get(t.source, t.source):<8}"
              f"{STATUS_CN.get(t.status, t.status):<7}{t.sla_status_cn():<10}"
              f"{t.severity:<6}"
              f"{DISC_CN.get(t.owner_discipline, t.owner_discipline or '-'):<6}"
              f"{t.owner or '-':<8}{t.title}")
    return 0


def _cmd_collab_show(args) -> int:
    from . import collab
    from .collab_model import SOURCE_CN, STATUS_CN, SEVERITY_CN, DISC_CN
    try:
        ledger, _ = _load_collab_ledger(args)
        t = ledger.find(args.ticket_id)
    except (collab.CollabError, KeyError):
        print(f"台账中找不到工单：{args.ticket_id}", file=sys.stderr)
        return 2
    print(f"工单 {t.ticket_id}（指纹 {t.fingerprint}）")
    print(f"  来源    : {SOURCE_CN.get(t.source, t.source)}"
          f"（类型 {t.kind}，来源单号 {t.source_ref or '-'}）")
    print(f"  标题    : {t.title}")
    print(f"  状态    : {STATUS_CN.get(t.status, t.status)} / "
          f"{SEVERITY_CN.get(t.severity, t.severity)} / {t.sla_status_cn()}")
    print(f"  责任    : {DISC_CN.get(t.owner_discipline, t.owner_discipline)}"
          f"专业 / {t.owner or '未指派'}")
    print(f"  位置    : {t.unit or '-'} {t.storey or ''} "
          f"({t.location[0]:.2f},{t.location[1]:.2f},{t.location[2]:.2f})")
    print(f"  量化    : {t.measure:g} {t.measure_label}".rstrip())
    print(f"  说明    : {t.detail or '-'}")
    print("  跨模型构件:")
    for r in t.refs:
        print(f"    - [{DISC_CN.get(r.discipline, r.discipline or '?')}]"
              f"{r.unit} {r.ifc_type} {r.name or r.global_id[:8]} "
              f"({r.global_id}) {r.file_path}")
    if t.resolution:
        print(f"  整改回写: {t.resolution}（{t.writeback_at}）")
    print("  流转记录:")
    for h in t.history:
        print(f"    {h.get('at', '')} {h.get('by', '')} "
              f"{h.get('action', '')}: {h.get('note', '')}".rstrip())
    return 0


def _cmd_collab_locate(args) -> int:
    from . import collab
    from .collab_model import DISC_CN
    try:
        ledger, _ = _load_collab_ledger(args)
        loc = collab.locate_ticket(ledger, args.ticket_id)
    except collab.CollabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"工单 {loc['ticket_id']}：{loc['title']}"
          f"（{loc['source_cn']}，楼层 {loc['storey'] or '-'}）")
    print(f"坐标：({loc['location'][0]:.3f}, {loc['location'][1]:.3f}, "
          f"{loc['location'][2]:.3f})")
    print("跨模型定位：")
    for m in loc["models"]:
        print(f"  [{DISC_CN.get(m['discipline'], m['discipline'] or '?')}] "
              f"单体 {m['unit'] or '-'}  文件 {m['file_path'] or '(未知)'}")
        for gid in m["global_ids"]:
            print(f"      - {gid}")
    return 0


def _cmd_collab_open(args) -> int:
    from . import collab
    from .collab_model import ModelRef
    try:
        ledger, path = _load_collab_ledger(args, create=True)
        refs = [ModelRef(global_id=g, discipline=args.owner_discipline,
                         unit=args.unit, storey=args.storey)
                for g in args.gid]
        t = collab.open_manual_ticket(
            ledger, title=args.title, detail=args.detail, actor=args.by,
            owner_discipline=args.owner_discipline, owner=args.owner,
            severity=args.severity, unit=args.unit, storey=args.storey,
            refs=refs, sla_hours=args.sla, note=args.note)
    except collab.CollabError as exc:
        print(f"登记失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path,
                        f"已登记 {t.ticket_id} → "
                        f"{args.owner_discipline or '未派专业'}/{t.owner or '未指派'}："
                        f"{t.title}")


def _cmd_collab_assign(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.assign_ticket(ledger, args.ticket_id, actor=args.by,
                                 owner=args.owner,
                                 owner_discipline=args.owner_discipline,
                                 note=args.note)
    except collab.CollabError as exc:
        print(f"派单失败：{exc}", file=sys.stderr)
        return 2
    from .collab_model import DISC_CN
    return _save_collab(
        ledger, path,
        f"已派单 {t.ticket_id} → {DISC_CN.get(t.owner_discipline, t.owner_discipline)}"
        f"/{t.owner or '未指派'}：{t.title}")


def _cmd_collab_fix(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.fix_ticket(ledger, args.ticket_id, actor=args.by,
                              note=args.note)
    except collab.CollabError as exc:
        print(f"整改上报失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path,
                        f"工单 {t.ticket_id} 已报整改完成，进入待复核：{args.note}")


def _cmd_collab_writeback(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.writeback_fix(ledger, args.ticket_id, actor=args.by,
                                 resolution=args.note,
                                 resolution_gids=args.gid or None)
    except collab.CollabError as exc:
        print(f"整改回写失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path,
                        f"工单 {t.ticket_id} 整改回写已记录：{t.resolution}")


def _cmd_collab_verify(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.verify_ticket(ledger, args.ticket_id, actor=args.by,
                                 note=args.note or "复核通过")
    except collab.CollabError as exc:
        print(f"复核失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path, f"工单 {t.ticket_id} 复核通过，已闭环。")


def _cmd_collab_reject(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.reject_ticket(ledger, args.ticket_id, actor=args.by,
                                 note=args.note, sla_hours=args.sla)
    except collab.CollabError as exc:
        print(f"驳回失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path,
                        f"工单 {t.ticket_id} 已驳回退回整改（重排时限 {args.sla:g}h）"
                        f"：{args.note}")


def _cmd_collab_close(args) -> int:
    from . import collab
    try:
        ledger, path = _load_collab_ledger(args)
        t = collab.close_ticket(ledger, args.ticket_id, actor=args.by,
                                reason=args.reason)
    except collab.CollabError as exc:
        print(f"关闭失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path,
                        f"工单 {t.ticket_id} 已人工关闭：{args.reason}")


def _cmd_collab_user(args) -> int:
    from . import collab
    from .collab_model import DISC_CN, ROLE_CN
    try:
        ledger, path = _load_collab_ledger(args, create=True)
        if args.remove:
            collab.remove_user(ledger, args.by, args.name)
            msg = f"已从名册移除：{args.name}"
        else:
            u = collab.upsert_user(ledger, args.by, args.name, args.role,
                                   discipline=args.discipline)
            if args.no_notify:
                u.notify = False
            msg = (f"已维护成员 {u.name}：{ROLE_CN.get(u.role, u.role)} / "
                   f"{DISC_CN.get(u.discipline, u.discipline or '无专业')}"
                   f" / 通知{'开' if u.notify else '退订'}")
    except collab.CollabError as exc:
        print(f"名册操作失败：{exc}", file=sys.stderr)
        return 2
    return _save_collab(ledger, path, msg)


def _cmd_collab_users(args) -> int:
    from . import collab
    from .collab_model import DISC_CN, ROLE_CN
    try:
        ledger, path = _load_collab_ledger(args)
    except collab.CollabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"项目“{args.project}”名册：{path}（{len(ledger.users)} 人）\n")
    print(f"{'姓名':<10}{'角色':<12}{'专业':<8}{'启用':<6}通知")
    for u in sorted(ledger.users.values(), key=lambda x: (x.role, x.name)):
        print(f"{u.name:<10}{ROLE_CN.get(u.role, u.role):<12}"
              f"{DISC_CN.get(u.discipline, u.discipline or '-'):<8}"
              f"{'是' if u.active else '否':<6}"
              f"{'是' if u.notify else '退订'}")
    return 0


def _cmd_collab_notifications(args) -> int:
    from . import collab
    from .collab_model import EVENT_CN
    try:
        ledger, path = _load_collab_ledger(args)
        if args.mark_read:
            n = collab.mark_notification_read(ledger, args.mark_read, args.name)
            ledger.save(path)
            print(f"通知 {n.notif_id} 已标记已读：{n.title}")
            return 0
        if args.read_all:
            k = collab.mark_all_read(ledger, args.name)
            ledger.save(path)
            print(f"已把 {args.name} 的 {k} 条通知标记已读：{path}")
            return 0
        items = ledger.notifications_for(args.name, unread_only=not args.all)
    except collab.CollabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    digest = collab.notification_digest(ledger, args.name)
    print(f"{args.name} 的通知：未读 {digest['unread']} / 共 {digest['total']}\n")
    for n in items:
        flag = "  " if n.read else "● "
        print(f"{flag}{n.notif_id} {n.created_at} "
              f"[{EVENT_CN.get(n.event, n.event)}] "
              f"{('工单 ' + n.ticket_id) if n.ticket_id else ''} {n.title}"
              f"{' — ' + n.body if n.body else ''}")
    return 0


def _cmd_collab_report(args) -> int:
    from . import collab, collab_report
    try:
        ledger, _ = _load_collab_ledger(args)
    except collab.CollabError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    gate = collab.for_gate_profile(args.gate_profile)
    passed, rules = collab.evaluate_collab_gate(ledger, gate, "",
                                                notify_block=False)
    paths = collab_report.export_all(
        ledger, args.output, gate_rules=rules, gate_passed=passed)
    print("闭环报告已导出：")
    for label, p in paths.items():
        print(f"  {label:<10}: {p}")
    print("闭环门禁结论：" + ("✅ 通过" if passed else "⛔ 未通过（阻断放行）"))
    return 0 if passed else 3


def _cmd_init_config(args) -> int:
    if os.path.exists(args.path):
        print(f"已存在同名文件，未覆盖：{args.path}", file=sys.stderr)
        return 2
    write_config_template(args.path, args.profile)
    print(f"阈值配置模板已生成：{args.path}（初始预设：{PROFILE_CN[args.profile]}）")
    print(f"修改后使用：python -m ifc_audit.cli audit model.ifc --config {args.path}")
    return 0


def _cmd_init_gate(args) -> int:
    if os.path.exists(args.path):
        print(f"已存在同名文件，未覆盖：{args.path}", file=sys.stderr)
        return 2
    write_gate_config_template(args.path, args.profile)
    print(f"门禁配置模板已生成：{args.path}（初始预设：{GATE_PROFILE_CN[args.profile]}）")
    print(f"修改后使用：python -m ifc_audit.cli batch ifc目录/ --project 项目名 "
          f"--gate-config {args.path}")
    return 0


def _launch_gui() -> int:
    try:
        from .gui import App
    except Exception as exc:
        print(f"无法启动图形界面：{exc}\n"
              "本机 Python 缺少 tkinter，请安装系统的 python3-tk，"
              "或直接使用 `python -m ifc_audit.cli audit`。", file=sys.stderr)
        return 2
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
