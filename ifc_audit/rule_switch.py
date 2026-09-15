"""规则版本切换：同一批模型按新旧规则包重算、试算对比与确认联动。

企业规则包发布新版本后，项目不宜直接改口径：先用本模块对**上一批次
同一批模型**分别按旧规则包与新规则包重算（试算），得到**纯规则调整**
引起的问题变化（新增 / 消除 / 保留），并预览放行门禁结论变化；
确认切换后四处联动：

1. **项目规则选择**——``<history>/<项目>/project_rule_binding.json``
   记录项目当前确认使用的规则包版本，后续 ``batch`` 自动按该版本核查
   （优先级高于规则库自动选择、低于显式 ``--rule-pack``）；
2. **放行门禁**——门禁规则随绑定规则包物化，下一批次即按新口径判定；
3. **趋势统计**——新规则重算结果作为「规则切换基线」快照写入批次历史，
   其在时间线中的位置取**确认时刻**（切换生效点；重算时刻保留在
   ``rule_switch.recheck_created_at``），下一批次（模型整改后）与之
   对比的增量即**模型整改**带来的变化，与本次试算得到的**规则调整**
   影响区分开；延迟确认前产生的旧口径批次全部保留在时间线中，
   但后续趋势锚点只取同口径基线（见
   :func:`ifc_audit.batch.select_trend_anchor`），不混入旧口径变化；
4. **追溯**——原始批次快照与原规则包版本全部保留，试算记录（含两次
   重算的完整快照与逐条问题差异）归档在
   ``<history>/<项目>/rule_switches/``。
"""

from __future__ import annotations

import copy
import glob
import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Optional, Callable

from .batch import (
    BatchResult, run_batch_with_rule_pack, project_history_dir,
    save_snapshot_dict, load_project_history, _TREND_METRICS, _new_batch_id,
)
from .rule_packs import (
    MaterializedRulePack, CHECKS, CHECK_CN, CHECK_ISSUE_KINDS,
)


class RuleSwitchError(ValueError):
    """规则版本切换流程错误（缺批次 / 缺文件 / 状态不允许等）。"""


# 问题类型 -> 核查项（试算报告按核查项归组规则调整影响）
_KIND_TO_CHECK: dict[str, str] = {
    kind: check for check, kinds in CHECK_ISSUE_KINDS.items() for kind in kinds
}


# ------------------------------------------------------------- 问题差异 ----

def _issue_fp(issue) -> tuple:
    """问题指纹：同一批模型两次重算间用于配对的稳定标识。"""
    ids = tuple(sorted(issue.global_ids))
    if ids:
        return (issue.kind, issue.storey or "", ids)
    return (issue.kind, issue.storey or "", ("", issue.title))


def _issue_brief(issue) -> dict:
    return {
        "kind": issue.kind,
        "check": _KIND_TO_CHECK.get(issue.kind, ""),
        "severity": issue.severity,
        "title": issue.title,
        "storey": issue.storey or "",
        "global_ids": list(issue.global_ids),
    }


def _diff_issue_lists(old_issues: list, new_issues: list) -> dict:
    """多重集差异：同一指纹两侧各取一次计为保留，余下为新增 / 消除。"""
    new_by_fp: dict[tuple, list] = {}
    for i in new_issues:
        new_by_fp.setdefault(_issue_fp(i), []).append(i)
    added, removed, kept_old = [], [], []
    for i in old_issues:
        lst = new_by_fp.get(_issue_fp(i))
        if lst:
            lst.pop(0)
            kept_old.append(i)
        else:
            removed.append(i)
    for lst in new_by_fp.values():
        added.extend(lst)
    return {"added": added, "removed": removed, "kept_old": kept_old}


def diff_batch_issues(old_batch: BatchResult, new_batch: BatchResult) -> dict:
    """对同一批模型的两次重算结果做逐条问题差异（纯规则调整影响）。

    按单体名配对；返回每个单体的新增 / 消除 / 保留问题清单，
    并按问题类型与核查项归组统计。
    """
    old_units = {u.name: u for u in old_batch.units if u.ok}
    new_units = {u.name: u for u in new_batch.units if u.ok}
    per_unit: dict[str, dict] = {}
    by_kind: dict[str, dict] = {}
    by_check: dict[str, dict] = {}
    totals = {"added": 0, "removed": 0, "kept": 0}

    def bump(bucket: dict, key: str, field: str) -> None:
        row = bucket.setdefault(key, {"added": 0, "removed": 0, "kept": 0})
        row[field] += 1

    for name in sorted(set(old_units) | set(new_units)):
        old_u, new_u = old_units.get(name), new_units.get(name)
        old_issues = list(old_u.model.issues) if old_u and old_u.model else []
        new_issues = list(new_u.model.issues) if new_u and new_u.model else []
        d = _diff_issue_lists(old_issues, new_issues)
        per_unit[name] = {
            "added": [_issue_brief(i) for i in d["added"]],
            "removed": [_issue_brief(i) for i in d["removed"]],
            "kept": len(d["kept_old"]),
        }
        totals["added"] += len(d["added"])
        totals["removed"] += len(d["removed"])
        totals["kept"] += len(d["kept_old"])
        for i in d["added"]:
            bump(by_kind, i.kind, "added")
            bump(by_check, _KIND_TO_CHECK.get(i.kind, "(未知)"), "added")
        for i in d["removed"]:
            bump(by_kind, i.kind, "removed")
            bump(by_check, _KIND_TO_CHECK.get(i.kind, "(未知)"), "removed")
        for i in d["kept_old"]:
            bump(by_kind, i.kind, "kept")
            bump(by_check, _KIND_TO_CHECK.get(i.kind, "(未知)"), "kept")

    return {"totals": totals, "by_kind": by_kind,
            "by_check": by_check, "per_unit": per_unit}


# --------------------------------------------------------- 规则包内容差异 ----

def compare_materialized(old: MaterializedRulePack,
                         new: MaterializedRulePack) -> dict:
    """新旧规则包物化口径差异：核查项开关 / 阈值 / 门禁规则逐项对比。"""
    from .thresholds import META as TH_META, _ATTR_TO_KEY as TH_ATTR_TO_KEY
    checks = []
    for c in CHECKS:
        old_on = c in old.enabled_checks
        new_on = c in new.enabled_checks
        if old_on != new_on:
            checks.append({"check": c, "name": CHECK_CN[c],
                           "old": old_on, "new": new_on})

    def _dict_diff(old_d: dict, new_d: dict) -> list[dict]:
        rows = []
        for k in sorted(set(old_d) | set(new_d)):
            ov, nv = old_d.get(k), new_d.get(k)
            if ov != nv:
                rows.append({"key": k, "old": ov, "new": nv})
        return rows

    # 阈值差异换算为用户面键名与单位（mm / %），与配置文件口径一致
    old_th = asdict(old.thresholds)
    new_th = asdict(new.thresholds)
    th_rows = []
    for attr in sorted(set(old_th) | set(new_th)):
        ov, nv = old_th.get(attr), new_th.get(attr)
        if ov == nv:
            continue
        user_key = TH_ATTR_TO_KEY.get(attr, attr)
        spec = TH_META.get(user_key)
        if spec is not None and ov is not None and nv is not None:
            th_rows.append({
                "key": user_key, "attr": attr, "label": spec.label,
                "unit": spec.unit,
                "old": round(spec.to_user(ov), 6),
                "new": round(spec.to_user(nv), 6)})
        else:
            th_rows.append({"key": user_key, "attr": attr, "label": attr,
                            "unit": "", "old": ov, "new": nv})

    return {
        "checks_toggled": checks,
        "thresholds": th_rows,
        "gate_rules": _dict_diff(asdict(old.gate), asdict(new.gate)),
    }


def compare_gate_outcome(old_batch: BatchResult,
                         new_batch: BatchResult) -> dict:
    """放行门禁结论对比（同一批模型、仅规则口径不同）。"""

    def failed(batch: BatchResult) -> dict[tuple, str]:
        return {(r.level, r.scope, r.key): r.message
                for r in batch.gate_results if not r.passed}

    old_f, new_f = failed(old_batch), failed(new_batch)
    return {
        "old_passed": old_batch.gate_passed,
        "new_passed": new_batch.gate_passed,
        "old_failed_count": len(old_f),
        "new_failed_count": len(new_f),
        "newly_failed": [{"level": k[0], "scope": k[1], "key": k[2],
                          "message": new_f[k]}
                         for k in sorted(new_f - old_f.keys())],
        "newly_passed": [{"level": k[0], "scope": k[1], "key": k[2],
                          "message": old_f[k]}
                         for k in sorted(old_f - new_f.keys())],
    }


# --------------------------------------------------------- 重算一致性 ----

_CONSISTENCY_KEYS = (
    "issues", "errors", "warnings", "duplicate_groups",
    "rooms_open", "opening_anomaly", "opening_unassigned",
)


def check_consistency(original: dict, old_recheck: BatchResult) -> dict:
    """原始批次快照 vs 旧规则重算：校验模型文件是否仍是原批次那一批。

    两者口径相同（同一旧规则包），指标应完全一致；不一致说明磁盘上的
    模型文件相对原批次已被改动，此时试算差异会混入模型变化因素。
    """
    mismatches = []
    ot = original.get("totals", {})
    nt = old_recheck.totals
    for key in _CONSISTENCY_KEYS:
        if int(ot.get(key, -1)) != int(nt.get(key, -2)):
            mismatches.append({
                "scope": "项目合计", "key": key,
                "original": ot.get(key), "recheck": nt.get(key)})
    if abs(float(ot.get("total_net_area", -1))
           - float(nt.get("total_net_area", -2))) > 1e-6:
        mismatches.append({
            "scope": "项目合计", "key": "total_net_area",
            "original": ot.get("total_net_area"),
            "recheck": nt.get("total_net_area")})
    orig_units = {u["name"]: u for u in original.get("units", [])}
    for u in old_recheck.units:
        ou = orig_units.get(u.name)
        if not ou:
            continue
        for key in ("issues", "errors", "warnings"):
            if int(ou.get(key, -1)) != int(getattr(u, key)):
                mismatches.append({
                    "scope": u.name, "key": key,
                    "original": ou.get(key), "recheck": getattr(u, key)})
    return {"consistent": not mismatches, "mismatches": mismatches}


# ------------------------------------------------------------- 试算 ----

def _new_switch_id() -> str:
    now = datetime.now()
    return "RS" + now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def run_rule_switch_compare(project: str,
                            original: dict,
                            old_mat: MaterializedRulePack,
                            new_mat: MaterializedRulePack,
                            history_dir: str,
                            by: str = "",
                            progress: Optional[Callable[[int, str], None]] = None
                            ) -> dict:
    """规则切换试算：对原始批次同一批模型按新旧规则包各重算一次并对比。

    Args:
        project: 项目名。
        original: 原始批次快照（``load_project_history`` 的元素）。
        old_mat / new_mat: 旧 / 新规则包的物化结果。
        history_dir: 批次历史目录（试算记录归档到其 ``rule_switches/``）。
        by: 试算操作人。

    Returns:
        试算记录（status=pending），已写入 ``rule_switches/<switch_id>.json``。
    """
    files = [u.get("file_path", "") for u in original.get("units", [])]
    files = [f for f in files if f]
    if not files:
        raise RuleSwitchError(
            f"原始批次 {original.get('batch_id')} 的快照中没有单体文件路径，"
            "无法按同一批模型重算")
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise RuleSwitchError(
            "原始批次的以下模型文件已不存在，无法按同一批模型重算：\n  "
            + "\n  ".join(missing))

    old_id = old_mat.ref.id
    new_id = new_mat.ref.id
    base_label = original.get("label") or ""

    def _run(mat, tag, lo, hi):
        def prog(p, m):
            if progress:
                progress(lo + int(p / 100 * (hi - lo)), m)
        return run_batch_with_rule_pack(
            files, mat, project=project,
            label=f"{base_label}·{tag}".strip("·"),
            progress=prog if progress else None,
            history_dir="")  # 试算不写协同台账 / 批次历史

    old_batch = _run(old_mat, f"旧规则重算[{old_id}]", 0, 50)
    new_batch = _run(new_mat, f"新规则试算[{new_id}]", 50, 100)

    consistency = check_consistency(original, old_batch)
    issue_diff = diff_batch_issues(old_batch, new_batch)
    metric_deltas = {}
    for key, label in _TREND_METRICS:
        old_v = old_batch.totals.get(key, 0)
        new_v = new_batch.totals.get(key, 0)
        metric_deltas[key] = {"label": label, "old": old_v, "new": new_v,
                              "delta": round(new_v - old_v, 3)}

    record = {
        "schema_version": 1,
        "switch_id": _new_switch_id(),
        "project": project,
        "status": "pending",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_by": by,
        "original_batch_id": original.get("batch_id", ""),
        "original_label": original.get("label", ""),
        "original_created_at": original.get("created_at", ""),
        "original_rule_pack_id": (original.get("rule_pack") or {}).get("id", ""),
        "old_rule_pack_id": old_id,
        "new_rule_pack_id": new_id,
        "files": files,
        "recheck": {"old_batch_id": old_batch.batch_id,
                    "new_batch_id": new_batch.batch_id},
        "consistency": consistency,
        "issue_diff": issue_diff,
        "metric_deltas": metric_deltas,
        "gate_compare": compare_gate_outcome(old_batch, new_batch),
        "pack_compare": compare_materialized(old_mat, new_mat),
        # 两次重算的完整快照随记录归档，确认时取新规则快照作为趋势基线
        "snapshots": {"old": old_batch.to_dict(), "new": new_batch.to_dict()},
        "confirmed_at": "",
        "confirmed_by": "",
        "baseline_batch_id": "",
    }
    path = save_switch_record(record, history_dir)
    record["record_path"] = path
    return record


# --------------------------------------------------------- 记录归档 ----

def switches_dir(history_dir: str, project: str) -> str:
    return os.path.join(project_history_dir(history_dir, project),
                        "rule_switches")


def save_switch_record(record: dict, history_dir: str) -> str:
    sdir = switches_dir(history_dir, record["project"])
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, f"{record['switch_id']}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
    return path


def list_switch_records(history_dir: str, project: str) -> list[dict]:
    """读取项目全部切换试算记录（按时间升序）。"""
    sdir = switches_dir(history_dir, project)
    out = []
    for fn in sorted(glob.glob(os.path.join(sdir, "*.json"))):
        try:
            with open(fn, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda r: (r.get("created_at", ""), r.get("switch_id", "")))
    return out


def load_switch_record(history_dir: str, project: str,
                       switch_id: str) -> dict:
    path = os.path.join(switches_dir(history_dir, project),
                        f"{switch_id}.json")
    if not os.path.isfile(path):
        raise RuleSwitchError(
            f"项目“{project}”没有切换试算记录 {switch_id}；"
            "可用 ruleswitch list 查看")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------- 项目规则绑定 ----

def _binding_path(history_dir: str, project: str) -> str:
    return os.path.join(project_history_dir(history_dir, project),
                        "project_rule_binding.json")


def load_project_rule_binding(history_dir: str, project: str) -> Optional[dict]:
    """读取项目已确认切换的规则包绑定；未绑定返回 None。"""
    path = _binding_path(history_dir, project)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not data.get("rule_pack_id"):
        return None
    return data


# --------------------------------------------------------- 确认切换 ----

def confirm_rule_switch(history_dir: str, project: str,
                        switch_id: Optional[str] = None,
                        by: str = "") -> dict:
    """确认规则切换：联动项目规则选择 / 放行门禁 / 趋势统计。

    - 把试算中新规则重算的快照标记为「规则切换基线」写入批次历史，
      下一批次趋势与之对比，增量即模型整改效果；
    - 写入项目规则绑定，后续 ``batch`` 自动按新规则包版本核查与放行；
    - 原始批次快照与原规则包版本保持不变，试算记录标记为已确认。
    """
    if switch_id is None:
        pending = [r for r in list_switch_records(history_dir, project)
                   if r.get("status") == "pending"]
        if not pending:
            raise RuleSwitchError(
                f"项目“{project}”没有待确认的规则切换试算；"
                "请先运行 ruleswitch compare")
        record = pending[-1]
    else:
        record = load_switch_record(history_dir, project, switch_id)
    if record.get("status") != "pending":
        raise RuleSwitchError(
            f"切换试算 {record.get('switch_id')} 已处于 "
            f"{record.get('status')} 状态，不能重复确认")

    now = datetime.now().isoformat(timespec="seconds")
    # 延迟确认：原批次之后可能已有其它批次。基线的时间线位置必须是
    # 「切换生效时刻」（确认时刻），否则会排在这些批次之前，
    # 后续批次趋势就会与旧口径批次对比、混入旧口径变化。
    history = load_project_history(history_dir, project)
    # 与时间线排序键一致：(created_at, batch_id)——created_at 为秒级精度，
    # 同一秒内的先后靠毫秒级批次号区分
    orig_key = (record.get("original_created_at", ""),
                record["original_batch_id"])
    intervening = sorted(
        h.get("batch_id", "") for h in history
        if (h.get("created_at", ""), h.get("batch_id", "")) > orig_key)

    # 1) 趋势基线：新规则重算快照写入批次历史（带 rule_switch 标记）。
    #    created_at 与 batch_id 均取确认时刻新值，保证基线排在干扰批次
    #    之后（同秒时批次号也最大）；重算时刻与重算批次号保留在
    #    rule_switch 元数据中供追溯；历史批次全部保留不变。
    baseline = copy.deepcopy(record["snapshots"]["new"])
    recheck_created_at = baseline.get("created_at", "")
    baseline["batch_id"] = _new_batch_id()
    baseline["created_at"] = now
    baseline["label"] = (f"{record.get('original_label') or ''}·规则切换基线"
                         .strip("·"))
    baseline["rule_switch"] = {
        "switch_id": record["switch_id"],
        "original_batch_id": record["original_batch_id"],
        "original_rule_pack_id": record["original_rule_pack_id"],
        "old_rule_pack_id": record["old_rule_pack_id"],
        "new_rule_pack_id": record["new_rule_pack_id"],
        "recheck_batch_id": record["recheck"].get("new_batch_id", ""),
        "recheck_created_at": recheck_created_at,
        "intervening_batches": intervening,
        "confirmed_at": now,
        "confirmed_by": by,
    }
    save_snapshot_dict(baseline, history_dir)

    # 2) 项目规则选择：绑定新规则包版本（batch 自动选择优先采用）
    binding = {
        "project": project,
        "rule_pack_id": record["new_rule_pack_id"],
        "switched_from": record["old_rule_pack_id"],
        "switch_id": record["switch_id"],
        "confirmed_at": now,
        "confirmed_by": by,
    }
    os.makedirs(os.path.dirname(_binding_path(history_dir, project)),
                exist_ok=True)
    tmp = _binding_path(history_dir, project) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(binding, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _binding_path(history_dir, project))

    # 3) 试算记录标记已确认（原始批次快照与原规则包版本不动）
    record["status"] = "confirmed"
    record["confirmed_at"] = now
    record["confirmed_by"] = by
    record["baseline_batch_id"] = baseline["batch_id"]
    record["baseline_created_at"] = now
    record["intervening_batches"] = intervening
    save_switch_record(record, history_dir)
    record["baseline"] = baseline
    record["binding"] = binding
    return record
