"""多专业协同核查结果导出。

输出：

* 协同核查 Excel：协同概览 / 碰撞与洞口工单（按状态着色、可筛选）/
  专业模型清单 / 判定参数与门禁 四张表；
* 工单 CSV；
* 协同批次 JSON（:meth:`CoordinationResult.to_dict`）；
* 建筑侧回写结论 JSON，并复制到建筑单体目录，供批次结论与门禁联动。
"""

from __future__ import annotations

import csv
import json
import os

from .coordination_model import (
    CoordinationResult, DISCIPLINES, DISC_CN, COORD_KINDS, COORD_KIND_CN,
    STATUS_CN,
)

_ERR_FILL = "F8CBAD"
_WARN_FILL = "FFE699"
_OK_FILL = "C6EFCE"
_GREY_FILL = "D9D9D9"
_BLUE_FILL = "DDEBF7"
_OVERDUE_FILL = "FF7C80"   # 超期未整改（深红）


def _fmt_dt(iso: str) -> str:
    """ISO 时间 -> 报表展示用 yyyy-mm-dd HH:MM。"""
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


def export_coordination_excel(result: CoordinationResult, out_path: str) -> str:
    """导出协同核查 Excel（4 张表）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    summ = result.summary()

    # 1) 协同概览
    ws = wb.active
    ws.title = "协同概览"
    rows = [
        ("项目", result.project),
        ("批次编号", result.batch_id),
        ("批次标签", result.label or "-"),
        ("核查时间", result.created_at),
        ("纳入专业", "、".join(DISC_CN.get(d, d) for d in summ["disciplines"]) or "-"),
        ("专业模型数", summ["n_files"]),
        ("参与构件数", summ["n_elements"]),
        ("预留洞口数", summ["n_openings"]),
    ]
    for d in DISCIPLINES:
        n = sum(1 for f in result.files if f.discipline == d)
        if n:
            rows.append((f"{DISC_CN[d]}专业模型", f"{n} 份"))
    rows.append(None)
    rows.append(("问题总数（含已闭环）", summ["issues_total"]))
    for k in COORD_KINDS:
        rows.append((COORD_KIND_CN[k] + "（未闭环）",
                     summ["active_by_kind"][k]))
    rows.append(None)
    for s, n in summ["by_status"].items():
        rows.append((STATUS_CN.get(s, s), n))
    rows.append(None)
    sla_hours = result.settings.get("fix_sla_hours", 0)
    rows.append(("整改时限（派单→整改）",
                 f"{sla_hours:g} 小时" if sla_hours else "未设置"))
    rows.append(("超期未整改工单", summ.get("issues_overdue", 0)))
    rows.append(("已自动升级工单", summ.get("issues_escalated", 0)))
    rows.append(None)
    rows.append(("责任人配置",
                 "；".join(f"{DISC_CN[d]}={result.owners[d]}"
                           for d in DISCIPLINES if result.owners.get(d)) or "未配置"))
    rows.append(("协同结论",
                 "✅ 协同核查通过" if result.gate_passed
                 else "⛔ 协同门禁未通过（阻断批次放行）"))
    for r in rows:
        ws.append(list(r) if r else [])
    _style_header(ws, 2)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[0].font = Font(bold=True)
    concl = ws.cell(row=ws.max_row, column=2)
    concl.fill = PatternFill("solid",
                             fgColor=_OK_FILL if result.gate_passed else _ERR_FILL)
    concl.font = Font(bold=True)
    _autosize(ws)

    # 2) 碰撞与洞口工单
    ws = wb.create_sheet("碰撞与洞口工单")
    headers = ["工单编号", "状态", "时限状态", "严重程度", "问题类型", "标题",
               "责任专业", "责任人", "楼层", "位置(x,y,z)",
               "量化指标", "涉及构件", "涉及专业", "涉及单体标识",
               "整改时限(h)", "整改截止", "剩余/超期(h)", "升级状态",
               "创建批次", "创建时间", "整改人/整改说明", "复核人/时间",
               "流转记录"]
    ws.append(headers)
    sev_cn = {"error": "错误", "warning": "警告", "info": "提示"}
    for i in result.issues:
        elems = "；".join(
            f"[{DISC_CN.get(e['discipline'], e['discipline'])}]"
            f"{e['name'] or e['global_id'][:8]}（{e['unit']}）"
            for e in i.elements)
        history = " ｜ ".join(
            f"{h.get('at', '')} {h.get('by', '')} "
            f"{h.get('action', '')}: {h.get('note', '')}".strip()
            for h in i.history)
        fixed = (f"{i.fixed_by}：{i.fixed_note}").strip("：") if i.fixed_by else "-"
        verified = (f"{i.verified_by} {_fmt_dt(i.verified_at)}".strip()
                    if i.verified_by else "-")
        rem = i.sla_remaining_hours()
        rem_txt = ("-" if rem is None
                   else (f"超期 {-rem:g}" if rem < 0 else f"{rem:g}"))
        esc_txt = ("已升级 L" + str(i.escalation_level)
                   if i.escalation_level else ("-" if i.sla_tracked else "-"))
        ws.append([
            i.issue_id, STATUS_CN.get(i.status, i.status), i.sla_status_cn(),
            sev_cn.get(i.severity, i.severity),
            COORD_KIND_CN.get(i.kind, i.kind), i.title,
            DISC_CN.get(i.owner_discipline, i.owner_discipline),
            i.owner or "（未指派）",
            i.storey or "-",
            f"({i.location[0]:.2f}, {i.location[1]:.2f}, {i.location[2]:.2f})",
            f"{i.measure:g} {i.measure_label}".strip(),
            elems,
            "、".join(DISC_CN.get(d, d) for d in i.disciplines),
            "、".join(sorted({e.get("unit_key") for e in i.elements
                              if e.get("unit_key")})) or "-",
            f"{i.sla_hours:g}" if i.sla_hours else "-",
            _fmt_dt(i.due_at), rem_txt, esc_txt,
            i.created_batch, _fmt_dt(i.created_at), fixed, verified, history,
        ])
    _style_header(ws, len(headers), fill="C00000")
    status_fills = {
        "open": _ERR_FILL, "rejected": _ERR_FILL,
        "fixed": _WARN_FILL, "verified": _OK_FILL, "cleared": _GREY_FILL,
    }
    status_col = headers.index("状态") + 1
    sla_col = headers.index("时限状态") + 1
    for r in range(2, ws.max_row + 1):
        st = ws.cell(row=r, column=status_col).value
        fill = status_fills.get(next(
            (s for s, cn in STATUS_CN.items() if cn == st), ""))
        if fill:
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=fill)
        # 超期未整改整行深红覆盖（优先于状态底色）
        if str(ws.cell(row=r, column=sla_col).value or "").startswith("超期"):
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor=_OVERDUE_FILL)
    _autosize(ws)

    # 3) 专业模型清单
    ws = wb.create_sheet("专业模型清单")
    ws.append(["单体名", "单体标识", "专业", "文件路径", "状态",
               "参与构件数", "预留洞口数", "错误"])
    for f in result.files:
        ws.append([
            f.unit, getattr(f, "unit_key", "") or f.unit,
            DISC_CN.get(f.discipline, f.discipline or "未判定"),
            f.file_path, "成功" if f.ok else "失败",
            f.n_elements, f.n_openings, f.error or "-"])
        if not f.ok:
            for c in range(1, 9):
                ws.cell(row=ws.max_row, column=c).fill = PatternFill(
                    "solid", fgColor=_ERR_FILL)
    _style_header(ws, 7)
    _autosize(ws)

    # 4) 判定参数与门禁
    ws = wb.create_sheet("判定参数与门禁")
    ws.append(["协同检测参数（用户面 mm）", "本次取值"])
    for k, v in result.settings.get("values", {}).items():
        ws.append([k, v])
    ws.append([])
    ws.append(["协同门禁方案",
               result.settings.get("gate_profile", "default")])
    ws.append(["整改时限（派单→整改，小时）",
               result.settings.get("fix_sla_hours", "-")])
    ws.append(["超期未整改 / 已自动升级",
               f"{summ.get('issues_overdue', 0)} / "
               f"{summ.get('issues_escalated', 0)}"])
    ws.append(["门禁规则", "限值", "实际值", "判定", "说明"])
    hr = ws.max_row
    for r in result.gate_rules:
        ws.append([r["rule"], r["limit"], r["actual"],
                   "通过" if r["passed"] else "不通过", r["message"] or "-"])
    for c in range(1, 3):
        ws.cell(row=1, column=c).font = Font(bold=True)
        ws.cell(row=1, column=c).fill = PatternFill("solid", fgColor=_BLUE_FILL)
    for c in range(1, 6):
        cell = ws.cell(row=hr, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C00000")
    for r in range(hr + 1, ws.max_row + 1):
        if ws.cell(row=r, column=4).value == "不通过":
            for c in range(1, 6):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor=_ERR_FILL)
    _autosize(ws)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return out_path


# ----------------------------------------------------------------- CSV ----

def export_issues_csv(result: CoordinationResult, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["工单编号", "状态", "时限状态", "严重程度", "问题类型", "标题",
                    "责任专业", "责任人", "楼层", "x", "y", "z",
                    "量化指标", "涉及构件GlobalId", "涉及单体", "涉及单体标识",
                    "整改时限h", "整改截止", "剩余或超期h",
                    "升级级别", "升级时间",
                    "创建批次", "创建时间",
                    "整改人", "整改说明", "整改时间",
                    "复核人", "复核时间", "详细说明"])
        for i in result.issues:
            rem = i.sla_remaining_hours()
            w.writerow([
                i.issue_id, STATUS_CN.get(i.status, i.status),
                i.sla_status_cn(), i.severity,
                COORD_KIND_CN.get(i.kind, i.kind), i.title,
                DISC_CN.get(i.owner_discipline, i.owner_discipline),
                i.owner, i.storey,
                round(i.location[0], 3), round(i.location[1], 3),
                round(i.location[2], 3),
                f"{i.measure:g} {i.measure_label}".strip(),
                ";".join(e["global_id"] for e in i.elements),
                ";".join(sorted({e["unit"] for e in i.elements if e.get("unit")})),
                ";".join(sorted({e.get("unit_key") for e in i.elements
                                 if e.get("unit_key")})),
                i.sla_hours or "", _fmt_dt(i.due_at),
                "" if rem is None else round(rem, 1),
                i.escalation_level, _fmt_dt(i.escalated_at),
                i.created_batch, _fmt_dt(i.created_at),
                i.fixed_by, i.fixed_note, _fmt_dt(i.fixed_at),
                i.verified_by, _fmt_dt(i.verified_at), i.detail,
            ])
    return out_path


# ---------------------------------------------------------------- JSON ----

def export_coordination_json(result: CoordinationResult, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2,
                  default=str)
    return out_path


# --------------------------------------------------------- 建筑侧回写 ----

def export_arch_writeback(result: CoordinationResult, out_path: str) -> str:
    """把协同批次结论写成建筑侧回写 JSON。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.arch_writeback, f, ensure_ascii=False, indent=2,
                  default=str)
    return out_path


def export_all(result: CoordinationResult, out_dir: str,
               base_name: str = "") -> dict[str, str]:
    """一次性导出 Excel / CSV / JSON / 建筑侧回写，返回路径字典。"""
    base = base_name or f"{result.project}_多专业协同_{result.batch_id}"
    paths = {
        "excel": export_coordination_excel(
            result, os.path.join(out_dir, f"{base}.xlsx")),
        "issues_csv": export_issues_csv(
            result, os.path.join(out_dir, f"{base}_工单清单.csv")),
        "json": export_coordination_json(
            result, os.path.join(out_dir, f"{base}.json")),
        "arch_writeback": export_arch_writeback(
            result, os.path.join(out_dir, f"{base}_建筑侧结论.json")),
    }
    return paths
