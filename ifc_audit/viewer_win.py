"""独立进程的 PyVista 交互窗口。

GUI 通过子进程启动本模块，避免 VTK 与 Tk 主循环冲突::

    python -m ifc_audit.viewer_win model.ifc 2Xk... 9Qp...
"""

from __future__ import annotations

import sys

from .pipeline import audit_ifc
from .viewer import Viewer3D


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("用法: python -m ifc_audit.viewer_win <file.ifc> [GlobalId ...]")
        return 2
    file_path = argv[0]
    gids = argv[1:]
    model = audit_ifc(file_path)
    viewer = Viewer3D(model)
    if gids:
        elems = [model.elements[g] for g in gids if g in model.elements]
        viewer.locate(gids, [e.label for e in elems])
    viewer.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
