# -*- coding: utf-8 -*-
"""
ui_visuals.py

现代简洁风格背景
- 纯色浅灰背景，干净清爽
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Dict

import ttkbootstrap as ttk


# ========================================
# 现代简洁配色方案
# ========================================
class ModernColors:
    """现代简洁风格配色"""
    BG_MAIN = "#f5f5f7"
    BG_CARD = "#ffffff"


# 向后兼容
COLORS = {
    "bg_main": ModernColors.BG_MAIN,
    "bg_card": ModernColors.BG_CARD,
}


class GlassBackground:
    """
    现代简洁风格背景：纯色浅灰
    """

    def __init__(self, master: tk.Widget, **kwargs: Any) -> None:
        self.master = master
        self.bg_color = kwargs.get("bg_color", COLORS["bg_main"])
        self.canvas = ttk.Canvas(master, highlightthickness=0, bd=0, bg=self.bg_color)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

    def lower(self) -> None:
        """将背景 Canvas 置于最低层。"""
        try:
            self.canvas.lower()
        except Exception:
            pass
