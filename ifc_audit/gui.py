"""Tkinter 图形界面：问题列表 + 房间清单 + 点击定位。

定位方式：
1. 在窗口内的三维视图（matplotlib）中高亮并缩放至构件；
2. “在 PyVista 中打开”启动独立交互窗口，可旋转/缩放查看。
"""

from __future__ import annotations

import os
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from .fonts_util import configure as configure_font

configure_font()

from .pipeline import audit_ifc
from . import report
from .report import KIND_CN, SEV_CN
from .viewer import _product_meshes, TYPE_COLOR, HIGHLIGHT, issue_xyz
from .thresholds import (
    PROFILES, PROFILE_CN, META, GROUP_CN, resolve,
    ThresholdConfigError, write_config_template,
)

SEV_TAG = {"error": "err", "warning": "warn", "info": "info"}


class ThresholdDialog(tk.Toplevel):
    """阈值设置：预设 / 配置文件 / 逐项编辑（单位 mm 与 %）。"""

    def __init__(self, master, state: dict):
        super().__init__(master)
        self.title("判定阈值设置")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.result = None
        self._state = state
        self._vars = {}
        self._build()

    def _build(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        top = ttk.Frame(frm)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="阈值预设：").pack(side="left")
        self.var_profile = tk.StringVar(value=self._state.get("profile", "default"))
        ttk.Combobox(top, textvariable=self.var_profile, state="readonly",
                     values=PROFILES, width=10).pack(side="left")
        ttk.Button(top, text="载入配置文件…",
                   command=self._load_config).pack(side="left", padx=(12, 0))
        ttk.Button(top, text="导出当前配置…",
                   command=self._save_config).pack(side="left", padx=4)
        self.var_cfg = tk.StringVar(
            value=self._state.get("config_path") or "未使用配置文件")
        ttk.Label(frm, textvariable=self.var_cfg, foreground="#555").pack(
            anchor="w", pady=(0, 6))

        # 逐项阈值，按分组排列
        grid = ttk.Frame(frm)
        grid.pack(fill="both")
        current_values = self._state.get("values", {})
        t0, _ = resolve(self.var_profile.get())
        row = 0
        last_group = None
        for key, spec in META.items():
            if spec.group != last_group:
                ttk.Label(grid, text=GROUP_CN[spec.group],
                          font=("", 9, "bold")).grid(
                    row=row, column=0, columnspan=3, sticky="w",
                    pady=(6, 2))
                row += 1
                last_group = spec.group
            ttk.Label(grid, text=spec.label).grid(row=row, column=0,
                                                  sticky="w", padx=(12, 8))
            default_v = spec.to_user(getattr(t0, spec.attr))
            v = tk.StringVar(value=str(current_values.get(key, default_v)))
            self._vars[key] = v
            ttk.Entry(grid, textvariable=v, width=9).grid(row=row, column=1,
                                                          sticky="e")
            unit = {"mm": "mm", "%": "%", "ratio": "(0~1)"}[spec.unit]
            ttk.Label(grid, text=unit, foreground="#555").grid(
                row=row, column=2, sticky="w", padx=(4, 0))
            row += 1

        ttk.Label(frm, text="提示：长度单位毫米，面积偏差单位百分比；"
                            "改动在下次核查时生效。",
                  foreground="#555", wraplength=420).pack(anchor="w",
                                                          pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="恢复预设值",
                   command=self._reset_to_profile).pack(side="left")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="确定", command=self._on_ok).pack(side="right",
                                                                padx=6)

    def _reset_to_profile(self):
        t0, _ = resolve(self.var_profile.get())
        for key, spec in META.items():
            self._vars[key].set(
                str(spec.to_user(getattr(t0, spec.attr))))

    def _load_config(self):
        path = filedialog.askopenfilename(
            title="选择阈值配置文件",
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            t, prov = resolve(self.var_profile.get(), path)
        except ThresholdConfigError as exc:
            messagebox.showerror("配置错误", str(exc), parent=self)
            return
        self.var_cfg.set(path)
        for key, spec in META.items():
            self._vars[key].set(
                str(spec.to_user(getattr(t, spec.attr))))

    def _save_config(self):
        path = filedialog.asksaveasfilename(
            title="导出阈值配置", defaultextension=".json",
            filetypes=[("JSON 配置", "*.json")])
        if not path:
            return
        try:
            write_config_template(path, self.var_profile.get())
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("已导出", f"配置模板已保存：\n{path}", parent=self)

    def _on_ok(self):
        values = {}
        for key, var in self._vars.items():
            raw = var.get().strip()
            try:
                v = float(raw)
            except ValueError:
                messagebox.showerror(
                    "格式错误", f"{META[key].label} 不是数字：{raw}",
                    parent=self)
                return
            spec = META[key]
            if not (spec.minv <= v <= spec.maxv):
                messagebox.showerror(
                    "超出范围",
                    f"{spec.label} 需在 [{spec.minv:g}, {spec.maxv:g}] 之间",
                    parent=self)
                return
            values[key] = v
        self.result = {
            "profile": self.var_profile.get(),
            "config_path": None if self.var_cfg.get() == "未使用配置文件"
            else self.var_cfg.get(),
            "values": values,
        }
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IFC 建筑模型核查工具")
        self.geometry("1280x820")
        self.model = None
        self.mesh_data = {}
        # 阈值设置状态：预设 + 可选配置文件 + GUI 逐项覆盖（用户单位）
        self.threshold_state = {"profile": "default",
                                "config_path": None, "values": {}}
        self._build_ui()

    # ------------------------------------------------------------- UI ----
    def _build_ui(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="IFC 文件：").pack(side="left")
        self.var_file = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_file, width=70).pack(
            side="left", padx=4)
        ttk.Button(top, text="浏览…", command=self.pick_file).pack(side="left")
        self.btn_run = ttk.Button(top, text="开始核查", command=self.run_audit)
        self.btn_run.pack(side="left", padx=6)
        ttk.Button(top, text="阈值设置…",
                   command=self.edit_thresholds).pack(side="left")
        ttk.Button(top, text="导出到目录…", command=self.export_all).pack(side="left")
        ttk.Button(top, text="在 PyVista 中打开",
                   command=self.open_pyvista).pack(side="left", padx=6)

        self.var_status = tk.StringVar(value="请选择 IFC 文件后开始核查。")
        ttk.Label(self, textvariable=self.var_status, foreground="#555"
                  ).pack(fill="x", padx=8)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=6)

        # 左侧：问题 / 房间两个标签页
        left = ttk.Frame(body)
        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)

        tab_issue = ttk.Frame(nb)
        nb.add(tab_issue, text="问题清单 (0)")
        cols = ("id", "sev", "kind", "storey", "title")
        self.tree = ttk.Treeview(tab_issue, columns=cols, show="headings",
                                 selectmode="browse")
        for c, t, w in [("id", "编号", 80), ("sev", "级别", 50),
                        ("kind", "类型", 100), ("storey", "楼层", 90),
                        ("title", "描述", 420)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("err", background="#f8cbad")
        self.tree.tag_configure("warn", background="#ffe699")
        self.tree.tag_configure("info", background="#dde9f7")
        vs = ttk.Scrollbar(tab_issue, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_issue)
        self.tab_issue = nb.tab(0)

        tab_room = ttk.Frame(nb)
        nb.add(tab_room, text="房间净面积 (0)")
        rcols = ("name", "storey", "area", "src", "door", "window", "closed")
        self.rtree = ttk.Treeview(tab_room, columns=rcols, show="headings")
        for c, t, w in [("name", "房间", 180), ("storey", "楼层", 100),
                        ("area", "净面积m²", 90), ("src", "来源", 70),
                        ("door", "门", 40), ("window", "窗", 40),
                        ("closed", "围护状态", 75)]:
            self.rtree.heading(c, text=t)
            self.rtree.column(c, width=w, anchor="center")
        self.rtree.tag_configure("notclosed", background="#f8cbad")
        self.rtree.tag_configure("unchecked", background="#d9d9d9")
        rvs = ttk.Scrollbar(tab_room, orient="vertical", command=self.rtree.yview)
        self.rtree.configure(yscrollcommand=rvs.set)
        self.rtree.pack(side="left", fill="both", expand=True)
        rvs.pack(side="right", fill="y")
        self.rtree.bind("<<TreeviewSelect>>", self.on_select_room)
        self.notebook = nb

        tab_open = ttk.Frame(nb)
        nb.add(tab_open, text="门窗表 (0)")
        ocols = ("storey", "room", "kind", "type", "spec", "count",
                 "src", "notes")
        self.otree = ttk.Treeview(tab_open, columns=ocols, show="headings")
        for c, t, w in [("storey", "楼层", 80), ("room", "房间", 150),
                        ("kind", "类别", 50), ("type", "类型", 110),
                        ("spec", "宽×高(mm)", 100), ("count", "数量", 50),
                        ("src", "尺寸来源", 80), ("notes", "备注", 160)]:
            self.otree.heading(c, text=t)
            self.otree.column(c, width=w, anchor="center" if c in
                              ("kind", "count") else "w")
        self.otree.tag_configure("anom", background="#f8cbad")
        self.otree.tag_configure("unassign", background="#d9d9d9")
        ovs = ttk.Scrollbar(tab_open, orient="vertical",
                            command=self.otree.yview)
        self.otree.configure(yscrollcommand=ovs.set)
        self.otree.pack(side="left", fill="both", expand=True)
        ovs.pack(side="right", fill="y")
        self.otree.bind("<<TreeviewSelect>>", self.on_select_opening)
        self._open_rows = {}

        # 问题详情
        detail_box = ttk.LabelFrame(left, text="问题详情", padding=4)
        detail_box.pack(fill="x", pady=(4, 0))
        self.txt_detail = tk.Text(detail_box, height=5, wrap="word")
        self.txt_detail.pack(fill="x")

        body.add(left, weight=1)

        # 右侧：三维视图
        right = ttk.Frame(body)
        self.fig = Figure(figsize=(6, 6))
        self.ax3d = self.fig.add_subplot(111, projection="3d")
        self.ax3d.set_title("三维定位视图")
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, right)
        toolbar.update()
        body.add(right, weight=2)

    # ---------------------------------------------------------- 动作 -----
    def pick_file(self):
        path = filedialog.askopenfilename(
            title="选择 IFC 文件",
            filetypes=[("IFC 文件", "*.ifc *.ifcxml *.ifczip"), ("所有文件", "*.*")])
        if path:
            self.var_file.set(path)

    def edit_thresholds(self):
        dlg = ThresholdDialog(self, self.threshold_state)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.threshold_state = dlg.result
            prov_text = self._threshold_summary()
            self.var_status.set(f"阈值已更新：{prov_text}。开始核查后生效。")

    def _threshold_summary(self) -> str:
        try:
            _, prov = resolve(
                self.threshold_state["profile"],
                self.threshold_state.get("config_path"),
                self.threshold_state.get("values") or None)
            return prov.describe()
        except ThresholdConfigError as exc:
            return f"阈值配置有误（{exc}）"

    def run_audit(self):
        path = self.var_file.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("错误", "请选择有效的 IFC 文件。")
            return
        try:
            thresholds, provenance = resolve(
                self.threshold_state["profile"],
                self.threshold_state.get("config_path"),
                self.threshold_state.get("values") or None)
        except ThresholdConfigError as exc:
            messagebox.showerror("阈值配置错误", str(exc))
            return
        self.btn_run.configure(state="disabled")
        self.var_status.set("正在核查，请稍候…")

        def work():
            try:
                m = audit_ifc(path, thresholds=thresholds,
                              provenance=provenance)
                self.after(0, lambda: self.on_done(m))
            except Exception as run_exc:
                self.after(0, lambda e=run_exc: self.on_fail(e))

        threading.Thread(target=work, daemon=True).start()

    def on_fail(self, exc):
        self.btn_run.configure(state="normal")
        self.var_status.set("核查失败。")
        messagebox.showerror("核查失败", repr(exc))

    def on_done(self, model):
        self.model = model
        self.btn_run.configure(state="normal")
        s = model.summary()
        self.var_status.set(
            f"完成：墙 {s['walls']} / 门 {s['doors']} / 窗 {s['windows']} / "
            f"房间 {s['rooms']}；问题 {s['issues']} 条（错误 {s['errors']}，"
            f"警告 {s['warnings']}）；净面积合计 {s['total_net_area']} m²。"
            f"阈值方案：{model.threshold_provenance.describe()}")

        self.tree.delete(*self.tree.get_children())
        for n, i in enumerate(model.issues, start=1):
            self.tree.insert(
                "", "end", iid=str(n),
                values=(i.issue_id, SEV_CN.get(i.severity, i.severity),
                        KIND_CN.get(i.kind, i.kind), i.storey, i.title),
                tags=(SEV_TAG.get(i.severity, "info"),))
        self.notebook.tab(0, text=f"问题清单 ({len(model.issues)})")

        self.rtree.delete(*self.rtree.get_children())
        for r in model.rooms:
            row_tag = {"open": ("notclosed",),
                       "unchecked": ("unchecked",)}.get(
                r.enclosure_status, ())
            self.rtree.insert(
                "", "end", iid=r.global_id,
                values=(r.name, r.storey, f"{r.net_area:.2f}",
                        "声明" if r.area_source == "declared" else "几何",
                        r.doors, r.windows, r.enclosure_label),
                tags=row_tag)
        self.notebook.tab(1, text=f"房间净面积 ({len(model.rooms)})")

        # 门窗表
        from .openings import size_label, DIM_SOURCE_CN
        self.otree.delete(*self.otree.get_children())
        self._open_rows = {}
        for n, r in enumerate(model.opening_schedule, start=1):
            tag = ""
            if r.n_anomalous:
                tag = "anom"
            elif r.n_unassigned:
                tag = "unassign"
            iid = f"opn-{n}"
            self.otree.insert(
                "", "end", iid=iid,
                values=(r.storey or "-", r.room_name,
                        "门" if r.kind == "door" else "窗",
                        r.type_name, size_label(r.width, r.height),
                        r.count, DIM_SOURCE_CN.get(r.dim_source, r.dim_source),
                        r.notes),
                tags=(tag,) if tag else ())
            self._open_rows[iid] = r.global_ids
        self.notebook.tab(2, text=f"门窗表 ({len(model.opening_schedule)})")

        try:
            self.mesh_data = _product_meshes(model.file_path)
        except Exception:
            self.mesh_data = {}
        self._draw_3d(highlight=set())

    # ------------------------------------------------------- 三维定位 ----
    def _draw_3d(self, highlight: set[str]):
        ax = self.ax3d
        ax.clear()
        for gid, (verts, faces, ifc_type) in self.mesh_data.items():
            color = HIGHLIGHT if gid in highlight else TYPE_COLOR[ifc_type]
            alpha = 0.12 if (ifc_type == "IfcSpace" and gid not in highlight) \
                else 0.9
            ax.add_collection3d(Poly3DCollection(
                verts[faces], facecolor=color, edgecolor="none", alpha=alpha))
        if self.mesh_data:
            import numpy as np
            all_v = np.vstack([m[0] for m in self.mesh_data.values()])
            mn, mx = all_v.min(0), all_v.max(0)
            ax.set_xlim(mn[0], mx[0]); ax.set_ylim(mn[1], mx[1])
            ax.set_zlim(0, mx[2])
            ax.set_box_aspect((mx[0] - mn[0], mx[1] - mn[1], max(mx[2], 0.1)))
        if self.model and self.model.issues:
            import numpy as np
            coords = np.array([issue_xyz(self.model, i)
                               for i in self.model.issues])
            ax.scatter(coords[:, 0], coords[:, 1], coords[:, 2],
                       c="red", s=30, depthshade=False)
        ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")
        ax.set_title("红点=问题位置；选中左侧问题可高亮定位")
        ax.view_init(elev=35, azim=-60)
        self.canvas.draw_idle()

    def on_select_issue(self, _evt=None):
        sel = self.tree.selection()
        if not sel or self.model is None:
            return
        issue = self.model.issues[int(sel[0]) - 1]
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0",
                               f"{issue.issue_id}  {issue.title}\n\n"
                               f"{issue.detail}\n\n"
                               f"关联构件 GlobalId：{', '.join(issue.global_ids)}")
        self._draw_3d(highlight=set(issue.global_ids))
        self._zoom_3d_to(issue.global_ids)

    def on_select_room(self, _evt=None):
        sel = self.rtree.selection()
        if not sel:
            return
        gid = sel[0]
        room = self.model.elements.get(gid)
        if room:
            self._draw_3d(highlight={gid})
            self._zoom_3d_to([gid])

    def on_select_opening(self, _evt=None):
        sel = self.otree.selection()
        if not sel:
            return
        gids = self._open_rows.get(sel[0], [])
        if gids:
            self._draw_3d(highlight=set(gids))
            self._zoom_3d_to(gids)

    def _zoom_3d_to(self, gids):
        """在当前视角下把视图中心移动到构件。"""
        import numpy as np
        centers = []
        for gid in gids:
            if gid in self.mesh_data:
                centers.append(self.mesh_data[gid][0].mean(axis=0))
        if not centers:
            return
        focus = np.mean(centers, axis=0)
        ax = self.ax3d
        # 以构件为中心取 10m 见方视图范围
        d = 5.0
        ax.set_xlim(focus[0] - d, focus[0] + d)
        ax.set_ylim(focus[1] - d, focus[1] + d)
        if self.mesh_data:
            all_v = np.vstack([m[0] for m in self.mesh_data.values()])
            ax.set_zlim(0, max(all_v[:, 2].max(), 0.1))
        self.canvas.draw_idle()

    # ---------------------------------------------------------- 导出 ----
    def export_all(self):
        if self.model is None:
            messagebox.showinfo("提示", "请先执行核查。")
            return
        out_dir = filedialog.askdirectory(title="选择导出目录")
        if not out_dir:
            return
        base = os.path.splitext(os.path.basename(self.model.file_path))[0]
        xlsx = report.export_excel(
            self.model, os.path.join(out_dir, f"{base}_核查报告.xlsx"))
        report.export_issues_csv(
            self.model, os.path.join(out_dir, f"{base}_问题清单.csv"))
        report.export_rooms_csv(
            self.model, os.path.join(out_dir, f"{base}_房间净面积.csv"))
        report.export_openings_csv(
            self.model, os.path.join(out_dir, f"{base}_门窗表.csv"))
        report.export_annotated_plan(
            self.model, os.path.join(out_dir, f"{base}_标注平面图.png"))
        messagebox.showinfo("导出完成", f"报告已导出到：\n{xlsx}")

    def open_pyvista(self):
        if self.model is None:
            messagebox.showinfo("提示", "请先执行核查。")
            return
        gids = []
        sel = self.tree.selection()
        if sel:
            gids = self.model.issues[int(sel[0]) - 1].global_ids
        import sys
        import subprocess
        cmd = [sys.executable, "-m", "ifc_audit.viewer_win",
               self.model.file_path, *gids]
        env = dict(os.environ, IFC_AUDIT_SHOW="1")
        try:
            subprocess.Popen(cmd, env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as exc:
            messagebox.showerror("无法启动 PyVista", repr(exc))


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
