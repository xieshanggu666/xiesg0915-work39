"""结果导出：Excel 清单、CSV、标注平面图。"""

from __future__ import annotations

import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .fonts_util import configure as configure_font

configure_font()
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
from matplotlib.patches import Polygon as MplPolygon

from .model import AuditModel

# 问题严重程度颜色（标注图）
SEV_COLOR = {
    "error": "#d62728",
    "warning": "#ff7f0e",
    "info": "#1f77b4",
}
SEV_CN = {"error": "错误", "warning": "警告", "info": "提示"}

KIND_CN = {
    "duplicate_element": "重复构件",
    "wall_free_end": "自由墙端",
    "wall_end_gap": "墙段缺口",
    "room_enclosure_gap": "房间围护缺口",
    "room_no_geometry": "房间无几何",
    "area_missing_declared": "面积缺声明",
    "area_mismatch": "面积偏差",
    "opening_unassigned": "门窗未归属",
    "opening_size_anomaly": "门窗尺寸异常",
}


# ---------------------------------------------------------------- Excel ----

def _autosize(ws):
    for col in ws.columns:
        width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)


def export_excel(model: AuditModel, out_path: str) -> str:
    """导出 7 张表：汇总 / 判定阈值 / 问题清单 / 房间净面积 / 重复构件 /
    门窗表 / 门窗明细。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="305496")

    def style_header(ws, ncols):
        for c in range(1, ncols + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    # 1) 汇总
    ws = wb.active
    ws.title = "汇总"
    s = model.summary()
    prov = getattr(model, "threshold_provenance", None)
    pack = getattr(model, "rule_pack", None)
    pack_scope = ""
    if pack is not None:
        from .rule_packs import STAGE_CN
        pj = "、".join(pack.projects) if pack.projects else "全部项目"
        ps = "、".join(STAGE_CN.get(s, s) for s in pack.stages) \
            if pack.stages else "全部阶段"
        pack_scope = f"（适用 {pj} / {ps}，发布于 {pack.published_at or '-'}）"
    rows = [
        ("指标", "数值"),
        ("IFC 文件", s["file"]),
        ("企业规则包",
         f"{pack.id}  指纹 {pack.content_hash}{pack_scope}" if pack else "未使用（内置预设）"),
        ("墙体数量", s["walls"]),
        ("门数量", s["doors"]),
        ("窗数量", s["windows"]),
        ("房间数量", s["rooms"]),
        ("问题总数", s["issues"]),
        ("其中-错误", s["errors"]),
        ("其中-警告", s["warnings"]),
        ("重复构件组", s["duplicate_groups"]),
        ("净面积合计 (m²)", s["total_net_area"]),
        ("门窗未归属数量", s["openings_unassigned"]),
        ("门窗尺寸异常数量", s["openings_size_anomaly"]),
        ("判定阈值方案", prov.describe() if prov else "标准（内置默认）"),
    ]
    for r in rows:
        ws.append(r)
    style_header(ws, 2)
    _autosize(ws)

    # 2) 判定阈值（本次核查实际使用的一套，含来源）
    ws = wb.create_sheet("判定阈值")
    if pack is not None:
        ws.append(["企业规则包", pack.id])
        ws.append(["规则包指纹", pack.content_hash])
        ws.append(["适用项目", "、".join(pack.projects) if pack.projects else "全部项目"])
        from .rule_packs import STAGE_CN
        ws.append(["适用阶段",
                   "、".join(STAGE_CN.get(s, s) for s in pack.stages)
                   if pack.stages else "全部阶段"])
        ws.append(["发布时间", pack.published_at or "-"])
    ws.append(["本次判定阈值方案", prov.describe() if prov else "标准（内置默认）"])
    th_title_row = ws.max_row
    ws.append(["分组", "判定项", "本次取值", "配置键"])
    header_row = ws.max_row
    if getattr(model, "thresholds", None) is not None:
        from .thresholds import rows_for_report
        for row in rows_for_report(model.thresholds):
            ws.append(list(row))
    # 规则包核查项开关（追溯本次实际核查了哪些项）
    if pack is not None:
        from .rule_packs import check_rows_for_report, CHECKS
        ws.append([])
        ws.append(["核查项（规则包配置）", "是否启用", "标识", ""])
        checks_title_row = ws.max_row
        enabled = {}
        kinds = getattr(model, "enabled_kinds", None)
        from .rule_packs import CHECK_ISSUE_KINDS
        for c in CHECKS:
            enabled[c] = kinds is None or any(k in kinds for k in CHECK_ISSUE_KINDS[c])
        for label_cn, on, key in check_rows_for_report(enabled):
            ws.append([label_cn, "启用" if on else "关闭", key, ""])
        for c in range(1, 5):
            cell = ws.cell(row=checks_title_row, column=c)
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
    for c in range(1, 5):
        cell = ws.cell(row=header_row, column=c)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")
    ws.cell(row=th_title_row, column=1).font = Font(bold=True)
    _autosize(ws)

    # 2) 问题清单
    ws = wb.create_sheet("问题清单")
    ws.append([
        "编号", "严重程度", "问题类型", "楼层", "位置X(m)", "位置Y(m)",
        "量化值", "问题描述", "详情", "关联构件GlobalId",
    ])
    for i in model.issues:
        ws.append([
            i.issue_id, SEV_CN.get(i.severity, i.severity),
            KIND_CN.get(i.kind, i.kind), i.storey,
            round(i.location[0], 3), round(i.location[1], 3),
            round(i.measure, 4), i.title, i.detail,
            "; ".join(i.global_ids),
        ])
    style_header(ws, 10)
    _autosize(ws)
    # 严重程度着色
    fill_err = PatternFill("solid", fgColor="F8CBAD")
    fill_warn = PatternFill("solid", fgColor="FFE699")
    for row in range(2, ws.max_row + 1):
        sev = ws.cell(row=row, column=2).value
        if sev == "错误":
            ws.cell(row=row, column=2).fill = fill_err
        elif sev == "警告":
            ws.cell(row=row, column=2).fill = fill_warn

    # 3) 房间净面积
    ws = wb.create_sheet("房间净面积")
    ws.append([
        "GlobalId", "楼层", "房间编号", "房间名称", "净面积(m²)", "面积来源",
        "声明面积(m²)", "几何面积(m²)", "偏差", "周长(m)",
        "门数量", "窗数量", "围护状态",
    ])
    for r in model.rooms:
        ws.append([
            r.global_id, r.storey, r.name, r.long_name, r.net_area,
            "IFC声明" if r.area_source == "declared" else "几何计算",
            r.declared_area, r.computed_area,
            f"{r.deviation*100:.1f}%", r.perimeter,
            r.doors, r.windows, r.enclosure_label,
        ])
    style_header(ws, 13)
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        val = ws.cell(row=row, column=13).value
        if val == "否":
            ws.cell(row=row, column=13).fill = fill_err
        elif val == "未检查":
            ws.cell(row=row, column=13).fill = PatternFill(
                "solid", fgColor="D9D9D9")

    # 4) 重复构件
    ws = wb.create_sheet("重复构件")
    ws.append(["组号", "构件类型", "构件GlobalId", "名称", "楼层", "形心X(m)", "形心Y(m)"])
    for n, gids in enumerate(model.duplicate_groups, start=1):
        for gid in gids:
            e = model.elements[gid]
            ws.append([
                n, e.ifc_type.removeprefix("Ifc"), gid, e.name, e.storey,
                round(float(e.centroid[0]), 3), round(float(e.centroid[1]), 3),
            ])
    style_header(ws, 7)
    _autosize(ws)

    fill_anom = PatternFill("solid", fgColor="F8CBAD")   # 尺寸异常
    fill_unassign = PatternFill("solid", fgColor="D9D9D9")  # 未归属

    # 5) 门窗表（按楼层 + 房间归并）
    from .openings import size_label, KIND_CN as OPN_CN, DIM_SOURCE_CN
    ws = wb.create_sheet("门窗表")
    ws.append([
        "楼层", "房间", "类别", "类型", "规格宽×高(mm)",
        "宽(m)", "高(m)", "尺寸来源", "数量", "备注", "GlobalId",
    ])
    for r in model.opening_schedule:
        ws.append([
            r.storey, r.room_name, OPN_CN.get(r.kind, r.kind), r.type_name,
            size_label(r.width, r.height),
            r.width if r.width > 0 else "",
            r.height if r.height > 0 else "",
            DIM_SOURCE_CN.get(r.dim_source, r.dim_source),
            r.count, r.notes, "; ".join(r.global_ids),
        ])
    style_header(ws, 11)
    _autosize(ws)
    # 异常行整行着色：未归属灰、尺寸异常橙红（异常优先）
    for row in range(2, ws.max_row + 1):
        notes = ws.cell(row=row, column=10).value or ""
        fill = None
        if "尺寸异常" in notes:
            fill = fill_anom
        elif "未归属" in notes:
            fill = fill_unassign
        if fill is not None:
            for c in range(1, 12):
                ws.cell(row=row, column=c).fill = fill

    # 6) 门窗明细（逐实例）
    ws = wb.create_sheet("门窗明细")
    ws.append([
        "GlobalId", "楼层", "类别", "类型", "名称",
        "宽(m)", "高(m)", "规格宽×高(mm)", "尺寸来源",
        "归属房间", "尺寸异常", "未归属房间", "异常说明",
        "形心X(m)", "形心Y(m)",
    ])
    for o in model.opening_items:
        ws.append([
            o.global_id, o.storey, OPN_CN.get(o.kind, o.kind), o.type_name,
            o.name, o.width if o.width > 0 else "", o.height if o.height > 0 else "",
            size_label(o.width, o.height),
            DIM_SOURCE_CN.get(o.dim_source, o.dim_source),
            "; ".join(o.room_names),
            "是" if o.anomalous else "",
            "是" if o.unassigned else "",
            "；".join(o.size_notes),
            round(o.centroid[0], 3), round(o.centroid[1], 3),
        ])
    style_header(ws, 15)
    _autosize(ws)
    for row in range(2, ws.max_row + 1):
        if ws.cell(row=row, column=11).value == "是":
            for c in range(1, 16):
                ws.cell(row=row, column=c).fill = fill_anom
        elif ws.cell(row=row, column=12).value == "是":
            for c in range(1, 16):
                ws.cell(row=row, column=c).fill = fill_unassign

    wb.save(out_path)
    return out_path


# ----------------------------------------------------------------- CSV ----

def export_issues_csv(model: AuditModel, out_path: str) -> str:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["编号", "严重程度", "类型", "楼层", "X", "Y", "量化值",
                    "描述", "详情", "关联构件"])
        for i in model.issues:
            w.writerow([
                i.issue_id, i.severity, i.kind, i.storey,
                round(i.location[0], 3), round(i.location[1], 3),
                round(i.measure, 4), i.title, i.detail,
                ";".join(i.global_ids),
            ])
    return out_path


def export_rooms_csv(model: AuditModel, out_path: str) -> str:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["GlobalId", "楼层", "房间编号", "净面积m²", "面积来源",
                    "声明面积", "几何面积", "偏差", "门", "窗", "围护状态"])
        for r in model.rooms:
            w.writerow([
                r.global_id, r.storey, r.name, r.net_area, r.area_source,
                r.declared_area, r.computed_area, f"{r.deviation*100:.1f}%",
                r.doors, r.windows, r.enclosure_label,
            ])
    return out_path


def export_openings_csv(model: AuditModel, out_path: str) -> str:
    """门窗表 CSV（按楼层 + 房间 + 类型 + 宽×高 归并）。"""
    from .openings import size_label, KIND_CN as OPN_CN, DIM_SOURCE_CN
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["楼层", "房间", "类别", "类型", "规格宽×高mm",
                    "宽m", "高m", "尺寸来源", "数量", "备注", "GlobalId"])
        for r in model.opening_schedule:
            w.writerow([
                r.storey, r.room_name, OPN_CN.get(r.kind, r.kind), r.type_name,
                size_label(r.width, r.height),
                r.width if r.width > 0 else "",
                r.height if r.height > 0 else "",
                DIM_SOURCE_CN.get(r.dim_source, r.dim_source),
                r.count, r.notes, ";".join(r.global_ids),
            ])
    return out_path


# ------------------------------------------------------- 标注平面图 -------

def _poly_patches(geom, **style):
    if geom is None:
        return []
    if geom.geom_type == "Polygon":
        return [MplPolygon(list(geom.exterior.coords), closed=True, **style)]
    if geom.geom_type == "MultiPolygon":
        return [MplPolygon(list(g.exterior.coords), closed=True, **style)
                for g in geom.geoms]
    return []


def export_annotated_plan(model: AuditModel, out_path: str,
                          title: str = "IFC 模型核查标注图") -> str:
    """生成带问题编号标注的平面图（PNG）。"""
    fig, ax = plt.subplots(figsize=(14, 10), dpi=150)

    # 房间填充
    for room in model.by_type("IfcSpace"):
        patches = _poly_patches(room.footprint, facecolor="#dceaf7",
                                edgecolor="#3b7bbf", linewidth=1.0, alpha=0.8)
        for p in patches:
            ax.add_patch(p)
        if room.footprint is not None:
            c = room.footprint.centroid
            ax.text(c.x, c.y, room.name or "Room", fontsize=8,
                    ha="center", va="center", color="#1f4e79",
                    bbox=dict(boxstyle="round,pad=0.15", fc="white",
                              ec="#3b7bbf", alpha=0.75), zorder=3)

    # 墙
    wall_patches = []
    for w in model.by_type("IfcWall"):
        wall_patches += _poly_patches(w.footprint, facecolor="#6b6b6b",
                                      edgecolor="#3d3d3d", linewidth=0.3)
    for p in wall_patches:
        ax.add_patch(p)

    # 门/窗平面符号
    for d in model.by_type("IfcDoor"):
        if d.footprint is not None:
            c = d.footprint.centroid
            ax.add_patch(plt.Circle((c.x, c.y), 0.12, color="#2ca02c", zorder=4))
    for win in model.by_type("IfcWindow"):
        if win.footprint is not None:
            c = win.footprint.centroid
            ax.add_patch(plt.Rectangle((c.x - 0.1, c.y - 0.1), 0.2, 0.2,
                                       color="#17becf", zorder=4))

    # 尺寸异常 / 未归属房间的门窗加醒目标记
    for o in model.opening_items:
        if not (o.anomalous or o.unassigned):
            continue
        x, y = o.centroid
        ring = plt.Circle((x, y), 0.35, fill=False,
                          edgecolor="#d62728" if o.anomalous else "#7f3fbf",
                          linewidth=2.0, linestyle="--", zorder=5)
        ax.add_patch(ring)

    # 围护缺口红线
    for gaps in model.room_gaps.values():
        for g in gaps:
            xs, ys = g.xy
            ax.plot(xs, ys, color="#d62728", linewidth=2.5, zorder=5)

    # 问题编号（对重合编号做径向错位，避免互相遮挡）
    placed: list[tuple[float, float]] = []
    min_sep = 0.45
    for n, issue in enumerate(model.issues, start=1):
        x, y = issue.location
        dx, dy, radius = 0.0, 0.0, 0.0
        # 最多迭代寻找不与已放编号重叠的位置
        for _ in range(30):
            px, py = x + dx, y + dy
            if all((px - qx) ** 2 + (py - qy) ** 2 > min_sep ** 2
                   for qx, qy in placed):
                break
            radius += 0.09
            dx, dy = radius, radius * 0.7
        placed.append((x + dx, y + dy))
        if radius > 0:  # 引线指向真实位置
            ax.annotate("", xy=(x, y), xytext=(x + dx, y + dy),
                        arrowprops=dict(arrowstyle="-", color="#555",
                                        lw=0.6), zorder=6)
        color = SEV_COLOR.get(issue.severity, "#333333")
        ax.scatter([x + dx], [y + dy], s=150, marker="o", color=color,
                   edgecolors="white", linewidths=1.2, zorder=7)
        ax.text(x + dx, y + dy, str(n), fontsize=7, color="white",
                ha="center", va="center", zorder=8)

    ax.set_aspect("equal")
    ax.autoscale_view()
    ax.margins(0.10)
    # 游离在房间外的门窗（未归属）可能远离主体，扩边保证标记完整可见
    if model.opening_items:
        xs = [o.centroid[0] for o in model.opening_items]
        ys = [o.centroid[1] for o in model.opening_items]
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        ax.set_xlim(min(x0, min(xs) - 1.0), max(x1, max(xs) + 1.0))
        ax.set_ylim(min(y0, min(ys) - 1.0), max(y1, max(ys) + 1.0))
    ax.grid(True, linestyle="--", linewidth=0.3, alpha=0.5)
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title(title, fontsize=14, pad=12)

    legend_items = [
        Patch(facecolor="#dceaf7", edgecolor="#3b7bbf", label="房间"),
        Patch(facecolor="#6b6b6b", label="墙体"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#2ca02c",
               markersize=9, label="门"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor="#17becf",
               markersize=9, label="窗"),
        Line2D([0], [0], color="#d62728", linewidth=2.5, label="围护缺口"),
        Line2D([0], [0], marker="o", color="w", markeredgecolor="#d62728",
               markeredgewidth=1.6, markersize=11, label="门窗尺寸异常"),
        Line2D([0], [0], marker="o", color="w", markeredgecolor="#7f3fbf",
               markeredgewidth=1.6, markersize=11, label="门窗未归属房间"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#d62728",
               markersize=10, label="错误编号"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#ff7f0e",
               markersize=10, label="警告编号"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#1f77b4",
               markersize=10, label="提示编号"),
    ]
    ax.legend(handles=legend_items, loc="lower right", fontsize=9,
              framealpha=0.9)

    # 问题编号对照表放图下方
    def mt(s):
        # SimHei 无上标 ²，交给 mathtext 渲染
        return s.replace("m²", "m$^2$").replace("²", "$^2$")

    n_lines = 0
    if model.issues:
        lines = [
            mt(f"{n}. [{SEV_CN.get(i.severity, i.severity)}] "
               f"{KIND_CN.get(i.kind, i.kind)}: {i.title}")
            for n, i in enumerate(model.issues, start=1)
        ]
        n_lines = len(lines)
        fig.subplots_adjust(bottom=0.32)
        fig.text(0.02, 0.28, "\n".join(lines[:40]), fontsize=7, va="top",
                 family="sans-serif")
    else:
        fig.subplots_adjust(bottom=0.12)

    # 注明本次使用的阈值方案
    prov = getattr(model, "threshold_provenance", None)
    pack = getattr(model, "rule_pack", None)
    footer_y = 0.015
    if pack is not None:
        fig.text(0.02, 0.045,
                 f"企业规则包：{mt(pack.id)}（版本指纹 {pack.content_hash}）",
                 fontsize=7.5, va="bottom", color="#1a237e",
                 family="sans-serif", fontweight="bold")
    if prov is not None:
        fig.text(0.02, footer_y, f"判定阈值：{mt(prov.describe())}",
                 fontsize=7, va="bottom", color="#555555",
                 family="sans-serif")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path
