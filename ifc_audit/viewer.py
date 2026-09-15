"""三维查看与定位：PyVista 交互窗口 / 离屏截图。

在带显示器的机器上运行 :class:`Viewer3D` 可弹出交互窗口，点击问题列表时
相机构件会高亮定位；无头环境下 :meth:`screenshot` 仍可出图（自动降级到
matplotlib 三维渲染，保证批处理一定能产出标注图）。
"""

from __future__ import annotations

import os
import numpy as np

import ifcopenshell.geom as ifc_geom

from .model import AuditModel, WALL, DOOR, WINDOW, SPACE
from .units import project_length_scale

TYPE_COLOR = {
    WALL: (0.55, 0.55, 0.58),
    DOOR: (0.30, 0.75, 0.35),
    WINDOW: (0.30, 0.75, 0.90),
    SPACE: (0.80, 0.87, 0.97),
}
HIGHLIGHT = (0.90, 0.10, 0.10)


def issue_xyz(model, issue) -> tuple[float, float, float]:
    """问题标记的三维位置：XY 取问题位置，Z 取关联构件顶高 + 偏移。"""
    z = 1.2
    tops = []
    for gid in issue.global_ids:
        e = model.elements.get(gid)
        if e is not None and e.bounds[5] > 0:
            tops.append(e.bounds[5])
    if tops:
        z = max(tops) + 0.3
    return issue.location[0], issue.location[1], z


def _product_meshes(file_path: str):
    """从 IFC 直接读取每个构件的三角网格（米）。"""
    import ifcopenshell

    ifc_file = ifcopenshell.open(file_path)
    scale = project_length_scale(ifc_file)
    settings = ifc_geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    out = {}
    for elem in ifc_file.by_type("IfcProduct"):
        base = next((b for b in TYPE_COLOR if elem.is_a(b)), None)
        if base is None:
            continue
        try:
            shape = ifc_geom.create_shape(settings, elem)
        except Exception:
            continue
        g = shape.geometry
        verts = np.asarray(g.verts, dtype=float).reshape(-1, 3) * scale
        faces = np.asarray(g.faces, dtype=int).reshape(-1, 3)
        if len(verts) == 0:
            continue
        out[elem.GlobalId] = (verts, faces, base)
    return out


class Viewer3D:
    """PyVista 交互查看器（仅在有图形环境时可用）。"""

    def __init__(self, model: AuditModel):
        import pyvista as pv

        self.model = model
        self.pv = pv
        self.mesh_data = _product_meshes(model.file_path)
        self.actors = {}
        self.base_actors = {}

        try:
            pv.OFF_SCREEN = not bool(os.environ.get("IFC_AUDIT_SHOW"))
        except Exception:
            pass

        self.plotter = pv.Plotter(title="IFC 模型核查 - 三维定位",
                                  window_size=(1280, 860))
        try:
            self.plotter.set_background("white")
        except Exception:
            pass

        for gid, (verts, faces, ifc_type) in self.mesh_data.items():
            n = len(faces)
            face_arr = np.column_stack([np.full(n, 3), faces]).ravel()
            mesh = pv.PolyData(verts, face_arr)
            opacity = 0.35 if ifc_type == SPACE else 1.0
            actor = self.plotter.add_mesh(
                mesh, color=TYPE_COLOR[ifc_type], opacity=opacity,
                show_edges=False, name=gid,
            )
            self.actors[gid] = actor
            self.base_actors[gid] = actor

        # 问题位置红点
        if model.issues:
            coords = np.array([issue_xyz(model, i) for i in model.issues])
            pcloud = pv.PolyData(coords)
            self.plotter.add_mesh(pcloud, color="red", point_size=10,
                                  render_points_as_spheres=True)
            self.plotter.add_point_labels(
                pcloud, [str(n) for n in range(1, len(model.issues) + 1)],
                font_size=14, text_color="darkred", always_visible=True,
            )

        self.plotter.add_axes()
        self.clear_highlight()

    # ------------------------------------------------------------ 定位 ----

    def locate(self, global_ids: list[str], labels: list[str] | None = None):
        """高亮指定构件并把相机对准它们。"""
        pv = self.pv
        targets, centers = [], []
        wanted = set(global_ids)
        for gid, (verts, faces, ifc_type) in self.mesh_data.items():
            if gid in wanted:
                n = len(faces)
                face_arr = np.column_stack([np.full(n, 3), faces]).ravel()
                mesh = pv.PolyData(verts, face_arr)
                self.plotter.remove_actor(self.actors.get(gid), render=False)
                actor = self.plotter.add_mesh(
                    mesh, color=HIGHLIGHT, opacity=1.0,
                    show_edges=True, edge_color="white", name=f"hl_{gid}",
                )
                self.actors[gid] = actor
                targets.append(mesh)
                centers.append(verts.mean(axis=0))
        if centers:
            focus = np.mean(centers, axis=0)
            self.plotter.set_focus(focus)
            self.plotter.camera.zoom(1.6)
        if labels:
            self.plotter.add_text(" | ".join(labels)[:120], font_size=9,
                                  color="darkred", name="locate_text")
        self.plotter.render()

    def clear_highlight(self):
        """恢复原色。"""
        for gid, (verts, faces, ifc_type) in self.mesh_data.items():
            current = self.actors.get(gid)
            if current is not None and current is not self.base_actors.get(gid):
                self.plotter.remove_actor(current, render=False)
                pv = self.pv
                n = len(faces)
                face_arr = np.column_stack([np.full(n, 3), faces]).ravel()
                mesh = pv.PolyData(verts, face_arr)
                opacity = 0.35 if ifc_type == SPACE else 1.0
                actor = self.plotter.add_mesh(
                    mesh, color=TYPE_COLOR[ifc_type], opacity=opacity,
                    name=gid,
                )
                self.actors[gid] = actor
                self.base_actors[gid] = actor
        self.plotter.render()

    def run(self):
        self.plotter.show()

    def screenshot(self, out_path: str) -> str:
        try:
            img = self.plotter.screenshot(return_img=True)
            from PIL import Image
            Image.fromarray(img).save(out_path)
            return out_path
        except Exception as exc:  # 无头环境没有 GL 上下文
            print(f"[viewer] PyVista 离屏渲染失败（{exc}），改用 matplotlib 三维视图。")
            return matplotlib_screenshot(self.model, out_path)


def offscreen_render_available() -> bool:
    """探测当前环境是否支持 VTK/OpenGL 离屏渲染（EGL/OSMesa/X）。

    VTK 在完全无显示环境有时会在 C 层直接退出（无法 try/except），
    因此先按环境变量快速判断；仍不确定时用隔离子进程探测，
    绝不影响主流程。
    """
    import sys
    import subprocess

    # 显式要求展示（GUI 场景）时不拦截
    if os.environ.get("IFC_AUDIT_SHOW"):
        return True
    # 有显示服务器才可能交互/离屏
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            or sys.platform == "win32" or sys.platform == "darwin"):
        return False

    probe = (
        "import pyvista as pv; pv.OFF_SCREEN=True;"
        "p=pv.Plotter(off_screen=True, window_size=(64,64));"
        "p.add_mesh(pv.Cube());"
        "img=p.screenshot(return_img=True); p.close();"
        "raise SystemExit(0 if img is not None else 1)"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def matplotlib_screenshot(model: AuditModel, out_path: str,
                          highlight: list[str] | None = None) -> str:
    """无 GPU / 无显示环境下的三维标注图（matplotlib Poly3DCollection）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    from .fonts_util import configure as configure_font
    configure_font()

    meshes = _product_meshes(model.file_path)
    wanted = set(highlight or [])

    fig = plt.figure(figsize=(14, 9))
    ax = fig.add_subplot(111, projection="3d")

    for gid, (verts, faces, ifc_type) in meshes.items():
        color = HIGHLIGHT if gid in wanted else TYPE_COLOR[ifc_type]
        alpha = 0.15 if (ifc_type == SPACE and gid not in wanted) else 0.9
        tris = verts[faces]
        coll = Poly3DCollection(tris, facecolor=color, edgecolor="none",
                                alpha=alpha, linewidths=0)
        ax.add_collection3d(coll)

    if model.issues:
        coords = np.array([issue_xyz(model, i) for i in model.issues])
        ax.scatter(coords[:, 0], coords[:, 1], coords[:, 2],
                   c="red", s=40, depthshade=False)
        for n, (x, y, z) in enumerate(coords, start=1):
            ax.text(x, y, z, str(n), color="darkred", fontsize=8)

    # 自动设置坐标范围
    all_v = np.vstack([m[0] for m in meshes.values()])
    mn, mx = all_v.min(0), all_v.max(0)
    ax.set_xlim(mn[0], mx[0]); ax.set_ylim(mn[1], mx[1]); ax.set_zlim(0, mx[2])
    ax.set_box_aspect((mx[0] - mn[0], mx[1] - mn[1], max(mx[2], 0.1)))
    ax.set_xlabel("X (m)"); ax.set_ylabel("Y (m)"); ax.set_zlabel("Z (m)")
    ax.set_title("IFC 模型三维核查视图（红点=问题位置，编号对应问题清单）")
    ax.view_init(elev=35, azim=-60)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path
