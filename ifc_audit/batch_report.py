"""批量核查结果导出：批次 Excel、项目质量看板 PNG、批次 JSON。"""

from __future__ import annotations

import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .fonts_util import configure as configure_font
from .report import KIND_CN
from .gate import gate_rows_for_report
from .batch import BatchResult, _natural_key
from .coordination_model import DISC_CN as DISC_CN_LOCAL

configure_font()

# ----------------------------------------------------------------- Excel ----

_ERR_FILL = "F8CBAD"
_WARN_FILL = "FFE699"
_OK_FILL = "C6EFCE"
_GREY_FILL = "D9D9D9"
_OVERDUE_FILL = "FF7C80"


def _autosize(ws):
    for col in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)


def _style_header(ws, ncols, row=1, fill="305496"):
    from openpyxl.styles import Font, PatternFill, Alignment
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=fill)
        cell.alignment = Alignment(horizontal="center")
    if row == 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions


def result_sla_hours(coord) -> float:
    """协同结果采用的整改时限（小时），优先取设置，其次从工单推断。"""
    h = coord.settings.get("fix_sla_hours") if coord.settings else None
    if h:
        return float(h)
    slas = {i.sla_hours for i in coord.issues if i.sla_hours}
    return next(iter(slas), 0.0)


def _add_coordination_sheets(wb, batch) -> None:
    """批次 Excel 增加「协同工单」「协同结论与门禁」两张表。"""
    from openpyxl.styles import Font, PatternFill
    from .coordination_model import (
        COORD_KIND_CN, STATUS_CN, DISC_CN,
    )
    coord = batch.coordination
    s = coord.summary()

    # 协同结论与门禁
    ws = wb.create_sheet("协同结论与门禁")
    ws.append(["项目", coord.project])
    ws.append(["协同批次", coord.batch_id])
    ws.append(["纳入专业", "、".join(DISC_CN.get(d, d) for d in s["disciplines"])])
    ws.append(["参与构件 / 预留洞口", f"{s['n_elements']} / {s['n_openings']}"])
    ws.append(["问题总数（含已闭环）", s["issues_total"]])
    ws.append(["未闭环问题", s["issues_active"]])
    for k, cn in COORD_KIND_CN.items():
        ws.append([f"未闭环·{cn}", s["active_by_kind"][k]])
    ws.append(["整改时限（派单→整改，小时）",
               result_sla_hours(coord)])
    ws.append(["超期未整改 / 已自动升级",
               f"{s.get('issues_overdue', 0)} / {s.get('issues_escalated', 0)}"])
    ws.append([])
    ws.append(["回写建筑侧结论",
               coord.arch_writeback.get("verdict", "-") if coord.arch_writeback else "-"])
    for row in coord.arch_writeback.get("per_arch_unit", []):
        ws.append([f"建筑单体 {row['unit']}",
                   f"未闭环 {row['active']}（错误 {row['errors']} / "
                   f"警告 {row['warnings']}；超期 {row.get('overdue', 0)} / "
                   f"升级 {row.get('escalated', 0)}）"])
    ws.append([])
    ws.append(["协同门禁规则", "限值", "实际值", "判定", "说明"])
    hr = ws.max_row
    for r in coord.gate_rules:
        ws.append([r["rule"], r["limit"], r["actual"],
                   "通过" if r["passed"] else "不通过", r["message"] or "-"])
    ws.append([])
    ws.append(["协同放行结论",
               "✅ 协同核查通过" if coord.gate_passed
               else "⛔ 协同门禁未通过（阻断批次放行）"])
    for c in (1, 2):
        ws.cell(row=1, column=c).font = Font(bold=True)
    for c in range(1, 6):
        cell = ws.cell(row=hr, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C00000")
    for r in range(hr + 1, ws.max_row):
        if ws.cell(row=r, column=4).value == "不通过":
            for c in range(1, 6):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor=_ERR_FILL)
    last = ws.max_row
    ws.cell(row=last, column=1).font = Font(bold=True)
    ws.cell(row=last, column=2).fill = PatternFill(
        "solid", fgColor=_OK_FILL if coord.gate_passed else _ERR_FILL)
    _autosize(ws)

    # 协同工单
    ws = wb.create_sheet("协同工单")
    headers = ["工单编号", "状态", "时限状态", "严重程度", "问题类型", "标题",
               "责任专业", "责任人", "楼层", "位置(x,y,z)", "量化指标",
               "涉及构件", "涉及单体",
               "整改时限(h)", "整改截止", "剩余/超期(h)", "升级",
               "创建批次", "整改人", "复核人", "详细说明"]
    ws.append(headers)
    sev_cn = {"error": "错误", "warning": "警告", "info": "提示"}
    for i in coord.issues:
        elems = "；".join(
            f"[{DISC_CN.get(e['discipline'], e['discipline'])}]"
            f"{e['name'] or e['global_id'][:8]}"
            for e in i.elements)
        rem = i.sla_remaining_hours()
        ws.append([
            i.issue_id, STATUS_CN.get(i.status, i.status), i.sla_status_cn(),
            sev_cn.get(i.severity, i.severity),
            COORD_KIND_CN.get(i.kind, i.kind), i.title,
            DISC_CN.get(i.owner_discipline, i.owner_discipline),
            i.owner or "（未指派）", i.storey or "-",
            f"({i.location[0]:.2f},{i.location[1]:.2f},{i.location[2]:.2f})",
            f"{i.measure:g} {i.measure_label}".strip(),
            elems,
            "、".join(sorted({e["unit"] for e in i.elements if e.get("unit")})),
            f"{i.sla_hours:g}" if i.sla_hours else "-",
            (i.due_at or "-").replace("T", " ")[:16],
            ("-" if rem is None else (f"超期 {-rem:g}" if rem < 0 else f"{rem:g}")),
            "L" + str(i.escalation_level) if i.escalation_level else "-",
            i.created_batch, i.fixed_by or "-", i.verified_by or "-",
            i.detail,
        ])
    _style_header(ws, len(headers), fill="C00000")
    status_fills = {
        "open": _ERR_FILL, "rejected": _ERR_FILL,
        "fixed": _WARN_FILL, "verified": _OK_FILL, "cleared": _GREY_FILL,
    }
    sla_col = headers.index("时限状态") + 1
    for r in range(2, ws.max_row + 1):
        st = ws.cell(row=r, column=2).value
        fill = next((f for s, f in status_fills.items()
                     if STATUS_CN.get(s) == st), None)
        if fill:
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill("solid", fgColor=fill)
        if str(ws.cell(row=r, column=sla_col).value or "").startswith("超期"):
            for c in range(1, len(headers) + 1):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor=_OVERDUE_FILL)
    _autosize(ws)


def export_batch_excel(batch: BatchResult, out_path: str) -> str:
    """导出批次核查 Excel（8 张表）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    t = batch.totals

    # 1) 批次概览
    ws = wb.active
    ws.title = "批次概览"
    rows = [
        ("项目", batch.project),
        ("批次编号", batch.batch_id),
        ("批次标签", batch.label or "-"),
        ("核查时间", batch.created_at),
    ]
    pack = getattr(batch, "rule_pack", None)
    if pack:
        from .rule_packs import STAGE_CN
        pj = "、".join(pack.get("projects") or []) or "全部项目"
        ps = "、".join(STAGE_CN.get(s, s) for s in pack.get("stages") or []) \
            or "全部阶段"
        rows += [
            ("企业规则包", pack["id"]),
            ("规则包指纹", pack.get("content_hash", "")),
            ("规则包适用范围", f"{pj} / {ps}"),
            ("规则包发布时间", pack.get("published_at") or "-"),
        ]
    else:
        rows.append(("企业规则包", "未使用（内置预设 + 命令行参数）"))
    rows += [
        ("纳入单体数", f"{t['units']}（成功 {t['units'] - t['units_failed']} / "
                     f"失败 {t['units_failed']}）"),
        ("构件数量（墙/门/窗/房间）",
         f"{t['walls']} / {t['doors']} / {t['windows']} / {t['rooms']}"),
        ("问题总数", t["issues"]),
        ("错误 / 警告 / 提示", f"{t['errors']} / {t['warnings']} / {t['infos']}"),
        ("重复构件组", t["duplicate_groups"]),
        ("净面积合计 (m²)", t["total_net_area"]),
        ("围护不闭合房间", f"{t['rooms_open']}（另无几何未检查 {t['rooms_unchecked']}）"),
        ("净面积为0房间", t["rooms_zero_area"]),
        ("门窗总数（门/窗）",
         f"{t['opening_total']}（{t['opening_doors']}/{t['opening_windows']}）"),
        ("尺寸异常 / 未归属门窗",
         f"{t['opening_anomaly']} / {t['opening_unassigned']}"),
    ]
    coord = getattr(batch, "coordination", None)
    if coord is not None:
        cs = coord.summary()
        rows.append(("多专业协同",
                     "、".join(DISC_CN_LOCAL.get(d, d) for d in cs["disciplines"])
                     + f"｜构件 {cs['n_elements']} / 预留洞口 {cs['n_openings']}"))
        rows.append(("协同未闭环问题",
                     f"{cs['issues_active']}（碰撞/缺洞 "
                     f"{cs['active_by_kind']['coord_hard_clash'] + cs['active_by_kind']['coord_opening_missing']}，"
                     f"洞口不符 {cs['active_by_kind']['coord_opening_mismatch']}，"
                     f"洞口闲置 {cs['active_by_kind']['coord_opening_unused']}）"))
        rows.append(("协同整改时限/超期/升级",
                     f"{result_sla_hours(coord):g}h / "
                     f"{cs.get('issues_overdue', 0)} 超期 / "
                     f"{cs.get('issues_escalated', 0)} 已升级"))
    rows += [
        ("核查阈值方案",
         next((u.threshold_describe for u in batch.units if u.ok), "-")),
        ("门禁方案", batch.gate.get("description") or "-"),
        ("放行结论", "✅ 准予放行" if batch.gate_passed else "⛔ 不予放行（质量门禁阻断）"),
    ]
    for r in rows:
        ws.append(r)
    _style_header(ws, 2)
    _autosize(ws)
    # 放行结论着色
    concl_cell = ws.cell(row=ws.max_row, column=2)
    concl_cell.fill = PatternFill(
        "solid", fgColor=_OK_FILL if batch.gate_passed else _ERR_FILL)
    concl_cell.font = Font(bold=True)

    # 2) 放行判定
    ws = wb.create_sheet("放行判定")
    ws.append(["级别", "范围（单体/项目）", "规则", "门禁限值",
               "实际值", "判定", "说明"])
    level_cn = {"unit": "单体", "project": "项目", "batch": "批次",
                "coordination": "多专业协同"}
    for r in batch.gate_results:
        ws.append([
            level_cn.get(r.level, r.level), r.scope, r.rule, r.limit,
            r.actual, "通过" if r.passed else "不通过",
            r.message or "-",
        ])
    _style_header(ws, 7, fill="C00000")
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        if ws.cell(row=row, column=6).value == "不通过":
            for c in range(1, 8):
                ws.cell(row=row, column=c).fill = PatternFill(
                    "solid", fgColor=_ERR_FILL)

    # 3) 单体汇总
    ws = wb.create_sheet("单体汇总")
    ws.append([
        "单体", "文件", "状态", "墙", "门", "窗", "房间",
        "问题总数", "错误", "警告", "提示", "重复构件组",
        "净面积(m²)", "错误/千m²", "不闭合房间", "无几何房间",
        "净面积0房间", "门窗总数", "尺寸异常", "未归属门窗", "单体结论",
    ])
    for u in batch.units:
        unit_ok = all(r.passed for r in batch.gate_results
                      if r.level == "unit" and r.scope == u.name)
        ws.append([
            u.name, u.file_path,
            "核查失败" if not u.ok else "成功",
            u.walls, u.doors, u.windows, u.rooms,
            u.issues, u.errors, u.warnings, u.infos, u.dup_groups,
            u.total_net_area,
            round(u.error_density, 2),
            u.rooms_open, u.rooms_unchecked, u.rooms_zero_area,
            u.opening_total, u.opening_anomaly, u.opening_unassigned,
            "-" if not u.ok else ("通过" if unit_ok else "不通过"),
        ])
    _style_header(ws, 21)
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        status = ws.cell(row=row, column=3).value
        verdict = ws.cell(row=row, column=21).value
        if status == "核查失败" or verdict == "不通过":
            for c in range(1, 22):
                ws.cell(row=row, column=c).fill = PatternFill(
                    "solid", fgColor=_ERR_FILL)
        elif ws.cell(row=row, column=9).value:  # 有错数标红
            ws.cell(row=row, column=9).fill = PatternFill(
                "solid", fgColor=_WARN_FILL)

    # 4) 楼层汇总
    ws = wb.create_sheet("楼层汇总")
    ws.append([
        "单体", "楼层", "墙", "门", "窗", "房间",
        "问题总数", "错误", "警告", "提示",
        "净面积(m²)", "不闭合房间", "净面积0房间",
        "门数", "窗数", "尺寸异常", "未归属门窗",
    ])
    for s in sorted(batch.storeys, key=lambda r: (r.unit, _natural_key(r.storey))):
        ws.append([
            s.unit, s.storey, s.walls, s.doors, s.windows, s.rooms,
            s.issues, s.errors, s.warnings, s.infos,
            s.net_area, s.rooms_open, s.rooms_zero_area,
            s.opening_doors, s.opening_windows,
            s.opening_anomaly, s.opening_unassigned,
        ])
    _style_header(ws, 17)
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        if ws.cell(row=row, column=8).value:
            ws.cell(row=row, column=8).fill = PatternFill(
                "solid", fgColor=_ERR_FILL)
        if ws.cell(row=row, column=12).value:
            ws.cell(row=row, column=12).fill = PatternFill(
                "solid", fgColor=_WARN_FILL)

    # 5) 问题分布（单体 × 问题类型矩阵）
    ws = wb.create_sheet("问题分布")
    kinds = sorted({k for u in batch.units for k in u.kind_counts})
    header = ["单体"] + [KIND_CN.get(k, k) for k in kinds] + ["合计"]
    ws.append(header)
    for u in batch.units:
        ws.append([u.name] + [u.kind_counts.get(k, 0) for k in kinds]
                  + [u.issues])
    totals_row = ["项目合计"]
    for k in kinds:
        totals_row.append(batch.totals["kind_counts"].get(k, 0))
    totals_row.append(batch.totals["issues"])
    ws.append(totals_row)
    _style_header(ws, len(header))
    _autosize(ws)
    last = ws.max_row
    for c in range(1, len(header) + 1):
        ws.cell(row=last, column=c).font = Font(bold=True)
        ws.cell(row=last, column=c).fill = PatternFill(
            "solid", fgColor=_GREY_FILL)

    # 6) 门窗规格汇总（跨单体的门窗表明细，按 单体/楼层/房间/规格）
    ws = wb.create_sheet("门窗规格汇总")
    ws.append([
        "单体", "楼层", "房间", "类别", "类型", "规格宽×高(mm)",
        "数量", "尺寸异常", "未归属", "备注",
    ])
    from .openings import size_label, KIND_CN as OPN_CN
    n_open_rows = 0
    for u in sorted(batch.units, key=lambda x: x.name):
        if u.model is None:
            continue
        for r in u.model.opening_schedule:
            ws.append([
                u.name, r.storey, r.room_name,
                OPN_CN.get(r.kind, r.kind), r.type_name,
                size_label(r.width, r.height), r.count,
                r.n_anomalous or "", r.n_unassigned or "", r.notes,
            ])
            n_open_rows += 1
    _style_header(ws, 10)
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        notes = ws.cell(row=row, column=10).value or ""
        if "尺寸异常" in notes:
            fill = _ERR_FILL
        elif "未归属" in notes:
            fill = _GREY_FILL
        else:
            continue
        for c in range(1, 11):
            ws.cell(row=row, column=c).fill = PatternFill("solid", fgColor=fill)

    # 7) 多专业协同核查（建筑/结构/机电碰撞与预留洞口工单）
    if getattr(batch, "coordination", None) is not None:
        _add_coordination_sheets(wb, batch)

    # 8) 趋势对比
    ws = wb.create_sheet("趋势对比")
    tr = batch.trend or {}
    if tr.get("has_previous"):
        cur_pack = (getattr(batch, "rule_pack", None) or {}).get("id", "")
        prev_pack = tr.get("previous_rule_pack_id") or ""
        ws.append([
            f"上一批次：{tr.get('previous_batch_id')} "
            f"{tr.get('previous_label') or ''} "
            f"({tr.get('previous_created_at')})",
        ])
        ws.append([
            "规则包版本",
            prev_pack or "内置预设",
            cur_pack or "内置预设",
            "版本一致" if cur_pack == prev_pack else "版本已切换（口径可能变化）",
        ])
        ws.append(["指标", "上一批次", "本批次", "变化", "方向"])
        for key, d in tr.get("deltas", {}).items():
            delta = d["delta"]
            arrow = "—" if delta == 0 else ("▲ 增加" if delta > 0 else "▼ 减少")
            ws.append([d["label"], d["old"], d["new"],
                       ("" if delta == 0 else f"{'+' if delta > 0 else ''}{delta}"),
                       arrow])
        ws.append([])
        ws.append(["单体", "上次错误", "本次错误", "错误变化",
                   "上次问题", "本次问题", "状态"])
        status_cn = {"ok": "在批", "new": "新增单体",
                     "missing": "本批缺失", "failed": "核查失败"}
        for d in tr.get("unit_delta", []):
            ws.append([
                d["unit"],
                "-" if d["errors_old"] is None else d["errors_old"],
                "-" if d["errors_new"] is None else d["errors_new"],
                "-" if d["errors_delta"] is None else d["errors_delta"],
                "-" if d["issues_old"] is None else d["issues_old"],
                "-" if d["issues_new"] is None else d["issues_new"],
                status_cn.get(d["status"], d["status"]),
            ])
    else:
        ws.append(["本项目首次批次，无上一批次可对比。"])
    _autosize(ws)

    # 8) 阈值与门禁
    ws = wb.create_sheet("阈值与门禁")
    if getattr(batch, "rule_pack", None):
        rp = batch.rule_pack
        from .rule_packs import (
            CHECKS, CHECK_CN, STAGE_CN,
        )
        ws.append(["企业规则包", rp["id"]])
        ws.append(["规则包指纹", rp.get("content_hash", "")])
        ws.append(["发布时间", rp.get("published_at") or "-"])
        ws.append(["适用项目", "、".join(rp.get("projects") or []) or "全部项目"])
        ws.append(["适用阶段",
                   "、".join(STAGE_CN.get(s, s) for s in rp.get("stages") or [])
                   or "全部阶段"])
        ws.append([])
        ws.append(["核查项", "状态", "标识"])
        check_hdr = ws.max_row
        enabled = set(getattr(batch, "enabled_checks", []) or [])
        for c in CHECKS:
            on = c in enabled
            ws.append([CHECK_CN[c], "启用" if on else "关闭", c])
        ws.append([])
    ws.append(["门禁方案", batch.gate.get("description") or "-"])
    gate_title_row = ws.max_row
    disabled = set(batch.gate.get("disabled_keys") or [])
    ws.append(["分组", "规则", "本次取值", "配置键", "状态"])
    header_row = ws.max_row
    # 直接用快照值重建 QualityGate
    from .gate import QualityGate
    qg = QualityGate(**batch.gate["values"])
    for row in gate_rows_for_report(qg):
        ws.append(list(row) + ["不参与（核查项关闭）" if row[3] in disabled else "生效"])
    _style_header(ws, 5, row=header_row, fill="C00000")
    if getattr(batch, "rule_pack", None):
        for c in range(1, 4):
            cell = ws.cell(row=check_hdr, column=c)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
    ws.cell(row=gate_title_row, column=1).font = Font(bold=True)
    _autosize(ws)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    wb.save(out_path)
    return out_path


# ----------------------------------------------------------------- JSON ----

def export_batch_json(batch: BatchResult, out_path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(batch.to_dict(), f, ensure_ascii=False, indent=2,
                  default=str)
    return out_path


# ----------------------------------------------------------- 质量看板 PNG ----

def export_dashboard(batch: BatchResult, out_path: str) -> str:
    """生成项目质量看板 PNG：KPI、问题分布、净面积、门窗指标、趋势。"""
    t = batch.totals
    names = [u.name for u in batch.units]

    has_coord = getattr(batch, "coordination", None) is not None
    fig = plt.figure(figsize=(18, 14.2 if has_coord else 12.5), dpi=130)
    gs = fig.add_gridspec(
        4 if has_coord else 3, 3,
        hspace=0.62, wspace=0.28, left=0.05, right=0.97,
        top=0.76 if has_coord else 0.79, bottom=0.05 if has_coord else 0.06)
    fig.patch.set_facecolor("white")

    # ---- 标题条 + 放行结论 ----
    ax_head = fig.add_axes([0.05, 0.90, 0.92, 0.085])
    ax_head.axis("off")
    verdict_color = "#2e7d32" if batch.gate_passed else "#c62828"
    verdict = "准予放行  PASS" if batch.gate_passed else "不予放行  BLOCKED（质量门禁阻断）"
    ax_head.add_patch(plt.Rectangle(
        (0, 0.05), 1, 0.95, transform=ax_head.transAxes, color=verdict_color,
        alpha=0.10))
    ax_head.text(
        0.01, 0.66, f"项目质量看板 ｜ {batch.project}",
        transform=ax_head.transAxes, fontsize=20, fontweight="bold",
        va="center", color="#1a237e")
    ax_head.text(
        0.01, 0.18,
        f"批次 {batch.batch_id}　{batch.label or ''}　核查时间 {batch.created_at}"
        f"　单体 {t['units']} 个（失败 {t['units_failed']}）",
        transform=ax_head.transAxes, fontsize=10.5, va="center",
        color="#444")
    if getattr(batch, "rule_pack", None):
        rp = batch.rule_pack
        ax_head.text(
            0.01, 0.02,
            f"企业规则包：{rp['id']}（指纹 {rp.get('content_hash', '')}）",
            transform=ax_head.transAxes, fontsize=9, va="center",
            color="#1a237e", fontweight="bold")
    ax_head.text(
        0.99, 0.55, verdict, transform=ax_head.transAxes, fontsize=16,
        fontweight="bold", va="center", ha="right", color=verdict_color)

    # ---- KPI 卡片 ----
    kpis = [
        ("错误", t["errors"], "#c62828"),
        ("警告", t["warnings"], "#ef6c00"),
        ("问题总数", t["issues"], "#37474f"),
        ("重复构件组", t["duplicate_groups"], "#6a1b9a"),
        ("不闭合房间", t["rooms_open"], "#ad1457"),
        ("净面积合计", f"{t['total_net_area']:g} m$^2$", "#0d47a1"),
        ("尺寸异常门窗", t["opening_anomaly"], "#e65100"),
        ("未归属门窗", t["opening_unassigned"], "#5d4037"),
        ("门窗总数", t["opening_total"], "#1b5e20"),
    ]
    n = len(kpis)
    for i, (label, value, color) in enumerate(kpis):
        x0 = i / n
        ax = fig.add_axes([0.05 + x0 * 0.92 + 0.004, 0.825,
                           0.92 / n - 0.008, 0.05])
        ax.axis("off")
        ax.add_patch(plt.Rectangle(
            (0, 0), 1, 1, transform=ax.transAxes, color=color, alpha=0.08,
            ec=color, lw=1.2))
        ax.text(0.5, 0.64, str(value), transform=ax.transAxes,
                fontsize=14, fontweight="bold", ha="center", va="center",
                color=color)
        ax.text(0.5, 0.22, label, transform=ax.transAxes, fontsize=9,
                ha="center", va="center", color="#555")

    # ---- 图1：各单体问题构成（堆叠柱）----
    ax1 = fig.add_subplot(gs[0, 0])
    err = np.array([u.errors for u in batch.units])
    warn = np.array([u.warnings for u in batch.units])
    info = np.array([u.infos for u in batch.units])
    xidx = np.arange(len(names))
    ax1.bar(xidx, err, color="#c62828", label="错误")
    ax1.bar(xidx, warn, bottom=err, color="#ef6c00", label="警告")
    ax1.bar(xidx, info, bottom=err + warn, color="#1f77b4", label="提示")
    for i, u in enumerate(batch.units):
        if not u.ok:
            ax1.text(i, 0.3, "×", ha="center", color="black", fontsize=13,
                     fontweight="bold")
    ax1.set_title("各单体问题分布（按严重程度）", fontsize=11,
                  fontweight="bold")
    ax1.set_xticks(xidx)
    ax1.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
    ax1.legend(fontsize=8)
    ax1.grid(axis="y", ls="--", lw=0.3, alpha=0.5)

    # ---- 图2：项目问题类型分布（横条）----
    ax2 = fig.add_subplot(gs[0, 1])
    kc = t["kind_counts"]
    if kc:
        items = sorted(kc.items(), key=lambda kv: kv[1])
        labels = [KIND_CN.get(k, k) for k, _ in items]
        vals = [v for _, v in items]
        colors = ["#c62828" if v == max(vals) else "#90a4ae" for v in vals]
        ax2.barh(labels, vals, color=colors)
        for i, v in enumerate(vals):
            ax2.text(v + max(vals) * 0.01, i, str(v), va="center",
                     fontsize=8)
        ax2.set_xlim(0, max(vals) * 1.15)
    ax2.set_title("项目问题类型分布", fontsize=11, fontweight="bold")
    ax2.tick_params(labelsize=8)
    ax2.grid(axis="x", ls="--", lw=0.3, alpha=0.5)

    # ---- 图3：各单体净面积 + 不闭合房间 ----
    ax3 = fig.add_subplot(gs[0, 2])
    areas = [u.total_net_area for u in batch.units]
    bars = ax3.bar(xidx, areas, color="#90caf9", edgecolor="#1565c0")
    ax3.set_ylabel("净面积 (m$^2$)", fontsize=9)
    ax3.set_xticks(xidx)
    ax3.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
    ax3b = ax3.twinx()
    ax3b.plot(xidx, [u.rooms_open for u in batch.units], "o-",
              color="#c62828", label="不闭合房间", lw=1.5, markersize=5)
    ax3b.set_ylabel("不闭合房间数", fontsize=9, color="#c62828")
    ax3b.tick_params(axis="y", colors="#c62828", labelsize=8)
    ax3b.set_ylim(bottom=0)
    for b, a in zip(bars, areas):
        ax3.text(b.get_x() + b.get_width() / 2, b.get_height(),
                 f"{a:g}", ha="center", va="bottom", fontsize=7)
    ax3.set_title("各单体净面积与围护不闭合房间", fontsize=11,
                  fontweight="bold")
    ax3.grid(axis="y", ls="--", lw=0.3, alpha=0.5)

    # ---- 图4：楼层问题热力（单体×楼层堆叠取错误数，横条）----
    ax4 = fig.add_subplot(gs[1, 0])
    floor_rows = sorted(
        batch.storeys, key=lambda s: (s.unit, _natural_key(s.storey)))
    # 过多时取错误数 Top 12
    labels4 = [f"{s.unit}/{s.storey}" for s in floor_rows]
    if len(labels4) > 14:
        floor_rows = sorted(floor_rows, key=lambda s: s.issues,
                            reverse=True)[:14]
        floor_rows.reverse()
        labels4 = [f"{s.unit}/{s.storey}" for s in floor_rows]
    e4 = [s.errors for s in floor_rows]
    w4 = [s.warnings for s in floor_rows]
    y4 = np.arange(len(floor_rows))
    ax4.barh(y4, e4, color="#c62828", label="错误")
    ax4.barh(y4, w4, left=e4, color="#ef6c00", label="警告")
    ax4.set_yticks(y4)
    ax4.set_yticklabels(labels4, fontsize=7)
    ax4.set_title("楼层问题分布（单体×楼层）", fontsize=11,
                  fontweight="bold")
    ax4.legend(fontsize=8, loc="lower right")
    ax4.grid(axis="x", ls="--", lw=0.3, alpha=0.5)

    # ---- 图5：门窗指标（异常 / 未归属分组柱）----
    ax5 = fig.add_subplot(gs[1, 1])
    w = 0.38
    anom = [u.opening_anomaly for u in batch.units]
    unas = [u.opening_unassigned for u in batch.units]
    ax5.bar(xidx - w / 2, anom, w, color="#e65100", label="尺寸异常")
    ax5.bar(xidx + w / 2, unas, w, color="#7b1fa2", label="未归属")
    ax5.set_xticks(xidx)
    ax5.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
    ax5.set_title("各单体门窗质量指标（樘）", fontsize=11, fontweight="bold")
    ax5.legend(fontsize=8)
    ax5.grid(axis="y", ls="--", lw=0.3, alpha=0.5)

    # ---- 图6：门窗规格构成（项目级 门/窗 与异常占比）----
    ax6 = fig.add_subplot(gs[1, 2])
    doors = t["opening_doors"]
    wins = t["opening_windows"]
    # 项目级异常占比：正常 vs 尺寸异常 vs 未归属（二者可重叠时正常数取剩余下限）
    normal = max(t["opening_total"]
                 - t["opening_anomaly"] - t["opening_unassigned"], 0)
    sizes = [normal, t["opening_anomaly"], t["opening_unassigned"]]
    pie_labels = [f"正常 {normal}", f"尺寸异常 {t['opening_anomaly']}",
                  f"未归属 {t['opening_unassigned']}"]
    if t["opening_total"] > 0:
        ax6.pie(sizes, labels=pie_labels, autopct="%1.1f%%",
                colors=["#66bb6a", "#ef6c00", "#8e24aa"],
                textprops={"fontsize": 8}, startangle=90,
                wedgeprops=dict(width=0.45, edgecolor="white"))
        ax6.text(0, 0, f"{doors}门\n{wins}窗", ha="center", va="center",
                 fontsize=10, fontweight="bold")
    ax6.set_title("门窗规格构成（门/窗质量占比）", fontsize=11,
                  fontweight="bold")

    # ---- 图7：批次趋势（错误/警告/问题 随批次）----
    ax7 = fig.add_subplot(gs[2, :2])
    history = (batch.trend or {}).get("history", [])
    if len(history) >= 2:
        xs = list(range(len(history)))
        xlabels = [h.get("created_at", "")[5:19] for h in history]
        # 批次使用的规则包版本标注在每个刻度下，口径切换一眼可见
        pack_labels = [
            (h.get("rule_pack_id") or "内置预设").split("@")[-1]
            for h in history
        ]
        ax7.plot(xs, [h.get("issues", 0) for h in history], "o-",
                 color="#37474f", label="问题总数", lw=1.8)
        ax7.plot(xs, [h.get("errors", 0) for h in history], "s-",
                 color="#c62828", label="错误", lw=1.8)
        ax7.plot(xs, [h.get("warnings", 0) for h in history], "^-",
                 color="#ef6c00", label="警告", lw=1.8)
        # 阻断批次红底 + 顶部 BLOCKED 角标
        for i, h in enumerate(history):
            if not h.get("gate_passed", True):
                ax7.axvspan(i - 0.3, i + 0.3, color="#c62828", alpha=0.08)
                ax7.text(i, 0.97, "BLOCKED", transform=ax7.get_xaxis_transform(),
                         fontsize=7, color="#c62828", ha="center", va="top",
                         fontweight="bold")
        ax7.set_xticks(xs)
        ax7.set_xticklabels([f"{t}\n规则包:{p}"
                             for t, p in zip(xlabels, pack_labels)],
                            fontsize=7)
        ax7.margins(y=0.18)
        ax7.legend(fontsize=8, ncol=3, loc="upper left")
    else:
        ax7.text(0.5, 0.5, "首次批次：留存多批次后自动形成趋势对比",
                 ha="center", va="center", fontsize=12, color="#888",
                 transform=ax7.transAxes)
        ax7.set_xticks([])
        ax7.set_yticks([])
    ax7.set_title("批次趋势（问题数随版本变化，红底=门禁阻断批次）",
                  fontsize=11, fontweight="bold", loc="left")
    ax7.grid(ls="--", lw=0.3, alpha=0.5)
    ax7.tick_params(axis="x", labelsize=8, rotation=15)

    # ---- 门禁失败项清单 ----
    ax8 = fig.add_subplot(gs[2, 2])
    ax8.axis("off")
    fails = [r for r in batch.gate_results if not r.passed]
    lines = []
    for r in fails[:10]:
        head = f"[{ {'unit':'单体','project':'项目','batch':'批次','coordination':'协同'}.get(r.level, r.level)} ] {r.scope}"  # noqa
        lines.append(head)
        lines.append(f"  × {r.message or (r.rule + ' 超限')}")
    if len(fails) > 10:
        lines.append(f"……另有 {len(fails) - 10} 项未通过")
    if not fails:
        lines = ["√ 全部门禁规则通过，准予放行"]
    ax8.add_patch(plt.Rectangle(
        (0, 0), 1, 1, transform=ax8.transAxes,
        color=("#c62828" if fails else "#2e7d32"), alpha=0.07))
    ax8.text(0.03, 0.95, f"放行门禁未通过项（{len(fails)}）" if fails
             else "放行门禁", transform=ax8.transAxes,
             fontsize=11, fontweight="bold", va="top",
             color="#c62828" if fails else "#2e7d32")
    ax8.text(0.03, 0.86, "\n".join(lines), transform=ax8.transAxes,
             fontsize=8, va="top", color="#333", linespacing=1.5)

    # ---- 多专业协同面板 ----
    if has_coord:
        from .coordination_model import COORD_KIND_CN, STATUS_CN, DISC_CN
        coord = batch.coordination
        cs = coord.summary()
        axc = fig.add_axes([0.05, 0.005, 0.92, 0.10])
        axc.axis("off")
        ccolor = "#2e7d32" if coord.gate_passed else "#c62828"
        axc.add_patch(plt.Rectangle(
            (0, 0), 1, 1, transform=axc.transAxes, color=ccolor, alpha=0.07))
        axc.text(0.01, 0.88, "多专业协同核查（建筑 × 结构 × 机电）",
                 transform=axc.transAxes, fontsize=12, fontweight="bold",
                 va="top", color="#1a237e")
        axc.text(0.99, 0.88,
                 ("协同核查通过" if coord.gate_passed
                  else "协同门禁阻断：存在未闭环碰撞/洞口问题"),
                 transform=axc.transAxes, fontsize=11, fontweight="bold",
                 va="top", ha="right", color=ccolor)
        sla_hours = result_sla_hours(coord)
        n_overdue = cs.get("issues_overdue", 0)
        n_escalated = cs.get("issues_escalated", 0)
        disc_txt = "、".join(
            f"{DISC_CN.get(d, d)}({sum(1 for f in coord.files if f.discipline == d)})"
            for d in cs["disciplines"])
        axc.text(0.01, 0.60,
                 f"纳入专业：{disc_txt}　构件 {cs['n_elements']} / "
                 f"预留洞口 {cs['n_openings']}　整改时限 {sla_hours:g}h",
                 transform=axc.transAxes, fontsize=9, va="top", color="#444")
        kind_txt = "　".join(
            f"{COORD_KIND_CN[k]} 未闭环 {cs['active_by_kind'][k]}"
            for k in COORD_KIND_CN)
        axc.text(0.01, 0.36, kind_txt, transform=axc.transAxes,
                 fontsize=9.5, va="top", color="#333")
        st_txt = "　".join(
            f"{STATUS_CN[s]} {cs['by_status'].get(s, 0)}"
            for s in ("open", "fixed", "rejected", "verified", "cleared")
            if cs["by_status"].get(s))
        owner_txt = "责任人：" + "，".join(
            f"{DISC_CN[d]}={coord.owners[d]}" for d in coord.owners) \
            if coord.owners else "（未配置责任人）"
        sla_txt = f"超期未整改 {n_overdue}"
        if n_overdue:
            sla_txt += "（已阻断）"
        sla_txt += f"　已自动升级 {n_escalated}"
        axc.text(0.99, 0.36, sla_txt, transform=axc.transAxes,
                 fontsize=9.5, va="top", ha="right",
                 fontweight="bold" if n_overdue else "normal",
                 color="#c62828" if n_overdue else "#2e7d32")
        axc.text(0.01, 0.12, f"{st_txt}　　{owner_txt}",
                 transform=axc.transAxes, fontsize=9, va="top", color="#555")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def export_trend_chart(batch: BatchResult, out_path: str) -> str:
    """仅导出批次趋势对比图（供 trend 子命令）。"""
    history = (batch.trend or {}).get("history", [])
    fig, ax = plt.subplots(figsize=(11, 5), dpi=140)
    if len(history) >= 2:
        xs = [h.get("created_at", "")[5:19] for h in history]
        packs = [(h.get("rule_pack_id") or "内置预设") for h in history]
        ax.plot(xs, [h.get("issues", 0) for h in history], "o-",
                color="#37474f", label="问题总数", lw=1.8)
        ax.plot(xs, [h.get("errors", 0) for h in history], "s-",
                color="#c62828", label="错误", lw=1.8)
        ax.plot(xs, [h.get("warnings", 0) for h in history], "^-",
                color="#ef6c00", label="警告", lw=1.8)
        for i, h in enumerate(history):
            if not h.get("gate_passed", True):
                ax.axvspan(i - 0.3, i + 0.3, color="#c62828", alpha=0.08)
        ax.set_xticks(range(len(xs)))
        ax.set_xticklabels([f"{t}\n{p}" for t, p in zip(xs, packs)],
                           fontsize=7)
        ax.legend()
    else:
        ax.text(0.5, 0.5, "历史批次不足 2 个，暂无趋势可对比",
                ha="center", va="center", fontsize=13, color="#888",
                transform=ax.transAxes)
    ax.set_title(f"{batch.project} ｜ 批次质量趋势", fontsize=13,
                 fontweight="bold")
    ax.grid(ls="--", lw=0.3, alpha=0.5)
    ax.tick_params(axis="x", rotation=15)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path
