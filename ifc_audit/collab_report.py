"""协同问题闭环报告导出。

输出：

* 闭环汇总 Excel：闭环概览 / 工单台账（按状态着色）/ 按专业楼层汇总 /
  通知与权限（名册）四张表；
* 工单 CSV；
* 闭环台账 JSON（含工单 / 名册 / 汇总 / 门禁判定）；
* 整改回写 JSON（回写单体 / 批次结论）。
"""

from __future__ import annotations

import csv
import json
import os

from .collab_model import (
    CollabLedger, SOURCE_CN, SEVERITY_CN, STATUS_CN, STATUS_CLOSED,
    DISCIPLINES, DISC_CN, ROLE_CN, COVER_CN,
    COLLAB_CLOSED_STATUSES,
)
from . import collab

_ERR_FILL = "F8CBAD"
_WARN_FILL = "FFE699"
_OK_FILL = "C6EFCE"
_GREY_FILL = "D9D9D9"
_BLUE_FILL = "DDEBF7"
_OVERDUE_FILL = "FF7C80"

_STATUS_FILL = {
    "open": _ERR_FILL, "rejected": _ERR_FILL,
    "fixed": _WARN_FILL, "verified": _OK_FILL,
    "cleared": _GREY_FILL, "closed": _GREY_FILL,
}


def _fmt_dt(iso: str) -> str:
    return iso.replace("T", " ")[:16] if iso else "-"


def _autosize(ws):
    for col in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)


def _style_header(ws, ncols, fill="305496"):
    from openpyxl.styles import Font, PatternFill, Alignment
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def _ordered_tickets(ledger: CollabLedger):
    return sorted(ledger.tickets.values(),
                  key=lambda t: (not t.active, t.owner_discipline,
                                 t.ticket_id))


def export_collab_excel(ledger: CollabLedger, out_path: str,
                        gate_rules: list[dict] | None = None,
                        gate_passed: bool = True,
                        batch_id: str = "") -> str:
    """导出协同闭环 Excel（4 张表）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    summ = collab.collab_summary(ledger)

    # 1) 闭环概览
    ws = wb.active
    ws.title = "闭环概览"
    rows = [
        ("项目", ledger.project),
        ("批次编号", batch_id or "-"),
        ("统计时间", _fmt_dt(ledger.updated_at)),
        ("问题总数（含已闭环）", summ["tickets_total"]),
        ("未闭环", summ["tickets_active"]),
        ("已闭环", summ["tickets_closed"]),
        ("闭环率", f"{summ['close_rate'] * 100:.1f}%"),
        ("平均闭环时长(h)", summ.get("avg_fix_hours") or "-"),
        ("超期未整改", summ["overdue"]),
        ("已自动升级", summ["escalated"]),
        ("未指派责任人", summ["no_owner"]),
    ]
    ls = summ.get("last_scan")
    if ls:
        rows.append(None)
        rows.append(("—— 最近一次复查 ——", ""))
        rows.append(("复查类型 / 批次",
                     ("局部复查" if ls["scoped"] else "全量复查")
                     + f"（{ls['batch_id']}，{_fmt_dt(ls['at'])}）"))
        rows.append(("复查范围", ls["scope"]))
        rows.append(("成功扫描单体",
                     f"{len(ls['scanned_units'])} 个："
                     + "、".join(ls["scanned_units"])))
        rows.append(("扫描失败模型",
                     f"{len(ls['failed_files'])} 个"
                     + ("（" + "、".join(f["unit"] or f["file"]
                                        for f in ls["failed_files"]) + "）"
                        if ls["failed_files"] else "")))
        rows.append(("自动销项",
                     f"复核通过 {ls['n_auto_verified']} / "
                     f"已消除 {ls['n_auto_cleared']}"))
        rows.append(("保留：范围外 / 扫描失败",
                     f"{ls['n_out_of_scope']} / {ls['n_scan_failed']}"))
        rows.append(("模型版本留痕", f"{len(ls['model_versions'])} 份"))
    rows.append(None)
    rows.append(("—— 按来源（总数 / 未闭环）——", ""))
    for s, d in SOURCE_CN.items():
        r = summ["by_source"].get(s, {"total": 0, "active": 0})
        rows.append((d, f"{r['total']} / {r['active']}"))
    rows.append(None)
    rows.append(("—— 按状态 ——", ""))
    for st, n in summ["by_status"].items():
        rows.append((STATUS_CN.get(st, st), n))
    rows.append(None)
    rows.append(("—— 未闭环按责任专业 ——", ""))
    for d in DISCIPLINES:
        rows.append((DISC_CN[d] + "专业",
                     summ["active_by_owner_discipline"].get(d, 0)))
    rows.append(None)
    rows.append(("闭环门禁结论",
                 "✅ 通过" if gate_passed else "⛔ 未通过（阻断放行）"))
    for r in rows:
        ws.append(list(r) if r else [])
    _style_header(ws, 2)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[0].font = Font(bold=True)
    concl = ws.cell(row=ws.max_row, column=2)
    concl.fill = PatternFill("solid", fgColor=_OK_FILL if gate_passed
                             else _ERR_FILL)
    concl.font = Font(bold=True)
    _autosize(ws)

    # 2) 工单台账
    ws = wb.create_sheet("工单台账")
    headers = ["工单编号", "来源", "状态", "时限状态", "严重程度", "问题类型",
               "标题", "责任专业", "责任人", "单体", "楼层", "位置(x,y,z)",
               "量化指标", "跨模型构件", "涉及专业",
               "整改时限h", "整改截止", "剩余/超期h", "升级",
               "整改说明(回写)", "关闭原因",
               "最近覆盖", "覆盖批次/时间", "复查模型版本",
               "创建批次", "创建时间", "整改人/时间", "复核人/时间",
               "来源单号", "流转记录"]
    ws.append(headers)
    kind_cn = {**collab.AUDIT_KIND_CN}
    try:
        from .coordination_model import COORD_KIND_CN
        kind_cn.update(COORD_KIND_CN)
    except Exception:
        pass
    for t in _ordered_tickets(ledger):
        refs = "；".join(r.label for r in t.refs)
        history = " ｜ ".join(
            f"{_fmt_dt(h.get('at', ''))} {h.get('by', '')} "
            f"{h.get('action', '')}:{h.get('note', '')}".strip()
            for h in t.history)
        rem = t.sla_remaining_hours()
        ver = "；".join(f"{k.split('|', 1)[0]} {v[:10]}"
                        for k, v in (t.last_scan_versions or {}).items())
        ws.append([
            t.ticket_id, SOURCE_CN.get(t.source, t.source),
            STATUS_CN.get(t.status, t.status), t.sla_status_cn(),
            SEVERITY_CN.get(t.severity, t.severity),
            kind_cn.get(t.kind, t.kind), t.title,
            DISC_CN.get(t.owner_discipline, t.owner_discipline),
            t.owner or "（未指派）", t.unit or "、".join(
                sorted({r.unit for r in t.refs if r.unit})) or "-",
            t.storey or "-",
            f"({t.location[0]:.2f},{t.location[1]:.2f},{t.location[2]:.2f})",
            f"{t.measure:g} {t.measure_label}".strip(),
            refs, "、".join(DISC_CN.get(d, d) for d in t.disciplines),
            f"{t.sla_hours:g}" if t.sla_hours else "-",
            _fmt_dt(t.due_at),
            "-" if rem is None else (f"超期{-rem:g}" if rem < 0 else f"{rem:g}"),
            f"L{t.escalation_level}" if t.escalation_level else "-",
            t.resolution or "-", t.closed_reason or "-",
            COVER_CN.get(t.last_cover_result, "-") if t.last_cover_result else "-",
            (f"{t.last_cover_batch} {_fmt_dt(t.last_cover_at)}".strip()
             if t.last_cover_batch else "-"),
            ver or "-",
            t.created_batch, _fmt_dt(t.created_at),
            (f"{t.fixed_by} {_fmt_dt(t.fixed_at)}".strip()
             if t.fixed_by else "-"),
            (f"{t.verified_by} {_fmt_dt(t.verified_at)}".strip()
             if t.verified_by else "-"),
            t.source_ref or "-", history,
        ])
    _style_header(ws, len(headers), fill="C00000")
    status_col = headers.index("状态") + 1
    sla_col = headers.index("时限状态") + 1
    for r in range(2, ws.max_row + 1):
        st_cn = ws.cell(row=r, column=status_col).value
        fill = next((c for s, c in _STATUS_FILL.items()
                     if STATUS_CN.get(s) == st_cn), None)
        if fill:
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=fill)
        if str(ws.cell(row=r, column=sla_col).value or "").startswith("超期"):
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor=_OVERDUE_FILL)
    _autosize(ws)

    # 3) 专业 × 楼层汇总
    ws = wb.create_sheet("专业楼层汇总")
    ws.append(["责任专业", "楼层", "未闭环工单数"])
    per: dict[tuple[str, str], int] = {}
    for t in ledger.tickets.values():
        if not t.active:
            continue
        key = (DISC_CN.get(t.owner_discipline, t.owner_discipline or "未派"),
               t.storey or "(未分层)")
        per[key] = per.get(key, 0) + 1
    for (d, st), n in sorted(per.items()):
        ws.append([d, st, n])
    _style_header(ws, 3)
    _autosize(ws)

    # 4) 名册与权限 + 门禁
    ws = wb.create_sheet("名册与权限")
    ws.append(["姓名", "角色", "所属专业", "启用", "接收通知"])
    for u in sorted(ledger.users.values(), key=lambda x: x.name):
        ws.append([u.name, ROLE_CN.get(u.role, u.role),
                   DISC_CN.get(u.discipline, u.discipline or "-"),
                   "是" if u.active else "否",
                   "是" if u.notify else "否"])
    _style_header(ws, 5)
    _autosize(ws)

    # 5) 复查记录（范围 / 实际扫描 / 模型版本 / 覆盖结果）
    if ledger.runs:
        ws = wb.create_sheet("复查记录")
        ws.append(["批次", "时间", "类型", "复查范围",
                   "成功扫描单体", "扫描失败模型", "模型版本",
                   "仍检出", "自动复核通过", "自动消除",
                   "范围外保留", "扫描失败保留", "未覆盖工单"])
        for r in ledger.runs:
            failed = "；".join(
                f"{f['unit'] or f['file']}（{f['error'] or '失败'}）"
                for f in r.failed_files)
            vers = "；".join(
                f"{v['unit']}[{DISC_CN.get(v['discipline'], v['discipline'])}]"
                f" {v['version'][:12]}" for v in r.model_versions)
            ws.append([
                r.batch_id, _fmt_dt(r.at),
                "局部" if r.scoped else "全量", r.note,
                "、".join(r.scanned_units), failed, vers,
                r.n_present, r.n_auto_verified, r.n_auto_cleared,
                r.n_out_of_scope, r.n_scan_failed,
                "、".join(r.uncovered_ticket_ids),
            ])
        _style_header(ws, 13, fill="548235")
        _autosize(ws)

    if gate_rules:
        ws.append([])
        ws.append(["闭环门禁规则", "限值", "实际值", "判定", "说明"])
        hr = ws.max_row
        for r in gate_rules:
            ws.append([r["key"], r["limit"], r["actual"],
                       "通过" if r["passed"] else "不通过",
                       r.get("message", "") or "-"])
        for c in range(1, 6):
            cell = ws.cell(row=hr, column=c)
            from openpyxl.styles import Font as _F, PatternFill as _P
            cell.font = _F(bold=True, color="FFFFFF")
            cell.fill = _P("solid", fgColor="C00000")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return out_path


def export_tickets_csv(ledger: CollabLedger, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["工单编号", "来源", "状态", "时限状态", "严重程度",
                    "问题类型", "标题", "责任专业", "责任人", "单体", "楼层",
                    "x", "y", "z", "涉及构件GlobalId", "涉及文件",
                    "整改时限h", "整改截止", "剩余或超期h", "升级级别",
                    "整改说明", "关闭原因",
                    "最近覆盖", "覆盖批次", "覆盖时间", "复查模型版本",
                    "创建批次", "创建时间",
                    "整改人", "整改时间", "复核人", "复核时间",
                    "来源单号", "详细说明"])
        for t in _ordered_tickets(ledger):
            rem = t.sla_remaining_hours()
            w.writerow([
                t.ticket_id, SOURCE_CN.get(t.source, t.source),
                STATUS_CN.get(t.status, t.status), t.sla_status_cn(),
                t.severity, t.kind, t.title,
                DISC_CN.get(t.owner_discipline, t.owner_discipline),
                t.owner, t.unit, t.storey,
                round(t.location[0], 3), round(t.location[1], 3),
                round(t.location[2], 3),
                ";".join(r.global_id for r in t.refs if r.global_id),
                ";".join(sorted({r.file_path for r in t.refs if r.file_path})),
                t.sla_hours or "", _fmt_dt(t.due_at),
                "" if rem is None else round(rem, 1),
                t.escalation_level, t.resolution, t.closed_reason,
                COVER_CN.get(t.last_cover_result, "") if t.last_cover_result else "",
                t.last_cover_batch, _fmt_dt(t.last_cover_at),
                ";".join(f"{k}={v}" for k, v in
                         (t.last_scan_versions or {}).items()),
                t.created_batch, _fmt_dt(t.created_at),
                t.fixed_by, _fmt_dt(t.fixed_at),
                t.verified_by, _fmt_dt(t.verified_at),
                t.source_ref, t.detail,
            ])
    return out_path


def export_collab_json(ledger: CollabLedger, out_path: str,
                       gate_rules: list[dict] | None = None,
                       gate_passed: bool = True, batch_id: str = "") -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc = ledger.to_dict()
    doc["batch_id"] = batch_id
    doc["summary"] = collab.collab_summary(ledger)
    doc["writeback"] = collab.build_writeback(ledger, batch_id)
    doc["gate"] = {"passed": gate_passed, "rules": gate_rules or []}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
    return out_path


def export_writeback_json(ledger: CollabLedger, out_path: str,
                          batch_id: str = "") -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(collab.build_writeback(ledger, batch_id), f,
                  ensure_ascii=False, indent=2, default=str)
    return out_path


def export_all(ledger: CollabLedger, out_dir: str,
               base_name: str = "", gate_rules: list[dict] | None = None,
               gate_passed: bool = True, batch_id: str = "") -> dict[str, str]:
    base = base_name or f"{ledger.project}_协同闭环_{batch_id or 'latest'}"
    return {
        "excel": export_collab_excel(
            ledger, os.path.join(out_dir, f"{base}.xlsx"),
            gate_rules=gate_rules, gate_passed=gate_passed, batch_id=batch_id),
        "csv": export_tickets_csv(
            ledger, os.path.join(out_dir, f"{base}_工单台账.csv")),
        "json": export_collab_json(
            ledger, os.path.join(out_dir, f"{base}.json"),
            gate_rules=gate_rules, gate_passed=gate_passed, batch_id=batch_id),
        "writeback": export_writeback_json(
            ledger, os.path.join(out_dir, f"{base}_整改回写.json"),
            batch_id=batch_id),
    }
