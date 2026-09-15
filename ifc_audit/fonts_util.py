"""中文字体配置：让 matplotlib / 报告图能正确显示中文。

优先使用随项目字体目录 ``fonts/`` 中的 Noto Sans SC，其次尝试系统中常见
的中文字体；都没有时返回 None（图中中文可能显示为方框，不影响核查逻辑）。
"""

from __future__ import annotations

import os

import matplotlib
from matplotlib import font_manager

_FONT_CANDIDATES = [
    "Noto Sans SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
    "Microsoft YaHei",
    "SimHei",
    "PingFang SC",
    "Arial Unicode MS",
]

_configured = False


def configure() -> str | None:
    """注册可用中文字体并设置 matplotlib 全局参数，返回字体名。"""
    global _configured
    if _configured:
        return matplotlib.rcParams.get("font.family", [None])[0] \
            if matplotlib.rcParams.get("font.family") else None

    # 1) 项目自带字体
    here = os.path.dirname(os.path.abspath(__file__))
    local_fonts = []
    for d in (os.path.join(here, "..", "fonts"),
              os.path.join(here, "..", "..", "fonts")):
        d = os.path.abspath(d)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.lower().endswith((".ttf", ".otf")):
                    local_fonts.append(os.path.join(d, fn))
    for path in local_fonts:
        try:
            font_manager.fontManager.addfont(path)
        except Exception:
            pass

    # 2) 已安装字体中匹配
    installed = {f.name for f in font_manager.fontManager.ttflist}
    chosen = next((c for c in _FONT_CANDIDATES if c in installed), None)

    if chosen:
        # 英文/数字也用同一族，保证混排一致；负号正常显示
        matplotlib.rcParams["font.sans-serif"] = [chosen, "DejaVu Sans"]
        matplotlib.rcParams["font.monospace"] = [chosen, "DejaVu Sans Mono"]
        matplotlib.rcParams["font.family"] = "sans-serif"
        matplotlib.rcParams["axes.unicode_minus"] = False

    _configured = True
    return chosen
