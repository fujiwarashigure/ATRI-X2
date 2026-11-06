"""Frutiger Aero（玻璃 / 水光 / 天空）主题：调色板、ttk 样式与启动期生成的图片。

为什么单独一层：界面里"好看"的部分一半是配色、一半是渐变/高光。配色是常量，渐变
与指示器图片是启动时用 :class:`tkinter.PhotoImage` 逐行填色生成的（运行时不引入任何
第三方图像库）。把两者收在这个模块，选项卡与主窗口只引用常量与 :func:`install`，
就不会各写一套颜色、也不会重复写渐变代码。

约定：

- 只通过 ``ttk.Style`` 与 ``option_add`` 改样式，**不改控件结构**，其它模块不需要
  知道主题存在；控件按原有 ``ttk`` 类名建出来就自动是新外观。
- 随包 PNG（``brand/aero-wallpaper.png`` / ``brand/aero-header.png``）优先，缺失时
  回退到旧素材，素材全缺也只是"没背景"，不影响启动。
- :func:`install` 返回的图片必须由调用方保活（``PhotoImage`` 被回收后样式会花掉）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Sequence

from atri_x.gui.icon import BRAND_DIR

__all__ = [
    "ACCENT",
    "ACCENT_DEEP",
    "ACCENT_LIGHT",
    "FONT",
    "GLASS",
    "GLASS_EDGE",
    "GLASS_EDGE_SOFT",
    "GLASS_TINT",
    "HEADER_BG",
    "HEADER_LINE",
    "INK",
    "INK_MUTED",
    "MINT",
    "RAIL",
    "RAIL_DEEP",
    "RAIL_INK",
    "RAIL_INK_SOFT",
    "RAIL_SELECT",
    "RAIL_TOP",
    "SKY",
    "SKY_DEEP",
    "install",
    "draw_vertical_gradient",
    "gradient_colors",
    "load_brand_image",
    "mix",
]

# -- 调色板 -----------------------------------------------------------------
#: 窗口底色（玻璃幕墙外的那层天空）。
SKY = "#d9f0fb"
#: 更深的天空色，用于卡片外框。
SKY_DEEP = "#bfe6f6"
#: 页面 / 卡片底色（近白玻璃）。
GLASS = "#f7fdff"
#: 略带回青的次级面板色。
GLASS_TINT = "#f0fafe"
#: 玻璃边框。
GLASS_EDGE = "#a9d9ec"
#: 更淡的分隔线。
GLASS_EDGE_SOFT = "#d3ecf7"
#: 页头底色（与 ``brand/aero-header.png`` 左侧同色）。
HEADER_BG = "#e7f6fd"
#: 页头下沿的玻璃亮线。
HEADER_LINE = "#bfe4f2"
#: 正文墨色（深青，比纯黑柔和）。
INK = "#0c3b50"
#: 次要文字。
INK_MUTED = "#4f7f95"
#: 主强调色（青蓝）。
ACCENT = "#2f9cc4"
#: 深强调色（标题 / 图标）。
ACCENT_DEEP = "#0e6d92"
#: 浅强调色（高光 / 选中边框）。
ACCENT_LIGHT = "#8fd6ee"
#: 成功 / 在线色。
MINT = "#3faf74"
#: 左侧导航栏顶部渐变起色。
RAIL_TOP = "#0d5b7c"
#: 左侧导航栏主色。
RAIL = "#0a4463"
#: 左侧导航栏底部渐变终色。
RAIL_DEEP = "#073349"
#: 导航栏文字。
RAIL_INK = "#d9f2ff"
#: 导航栏次要文字。
RAIL_INK_SOFT = "#9fc6da"
#: 导航栏选中色。
RAIL_SELECT = "#2f9cc4"
#: 界面统一字体族。
FONT = "Microsoft YaHei UI"


def load_brand_image(master: tk.Misc, *names: str) -> Any:
    """按顺序尝试加载 ``brand/`` 下的图片，全部失败返回 ``None``。"""
    for name in names:
        try:
            return tk.PhotoImage(master=master, file=str(BRAND_DIR / name))
        except tk.TclError:
            continue
    return None


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mix(first: str, second: str, ratio: float) -> str:
    a, b = _rgb(first), _rgb(second)
    return "#%02x%02x%02x" % tuple(
        round(a[index] + (b[index] - a[index]) * ratio) for index in range(3)
    )


def mix(first: str, second: str, ratio: float) -> str:
    """把两个 ``#rrggbb`` 按 ``ratio`` 混合（0 = first，1 = second）。"""
    a, b = _rgb(first), _rgb(second)
    return "#%02x%02x%02x" % tuple(
        round(a[index] + (b[index] - a[index]) * ratio) for index in range(3)
    )


def gradient_colors(stops: Sequence[tuple[float, str]], rows: int) -> list[str]:
    """把 ``[(位置, 颜色)]`` 展开成 ``rows`` 行颜色（位置 0~1，需覆盖首尾）。"""
    ordered = sorted(stops)
    colors: list[str] = []
    for row in range(rows):
        ratio = row / max(1, rows - 1)
        color = ordered[-1][1]
        for index in range(len(ordered) - 1):
            start, left = ordered[index]
            end, right = ordered[index + 1]
            if start <= ratio <= end:
                color = left if end == start else _mix(left, right, (ratio - start) / (end - start))
                break
        colors.append(color)
    return colors


def draw_vertical_gradient(
    canvas: tk.Canvas,
    x: int,
    y: int,
    width: int,
    height: int,
    stops: Sequence[tuple[float, str]],
) -> None:
    """在画布上画一块竖向渐变（用逐行矩形，行数等于高度，足够便宜）。"""
    if height <= 0 or width <= 0:
        return
    for row, color in enumerate(gradient_colors(stops, height)):
        canvas.create_rectangle(
            x, y + row, x + width, y + row + 1, fill=color, outline=color, tags=("aero-grad",)
        )


# -- 复选框 / 单选框指示器 --------------------------------------------------
def _line_pixels(x0: int, y0: int, x1: int, y1: int, thickness: int) -> set[tuple[int, int]]:
    """整数网格上的粗线段（用于手画勾），返回像素坐标集合。"""
    points: set[tuple[int, int]] = set()
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for step in range(steps + 1):
        x = round(x0 + (x1 - x0) * step / steps)
        y = round(y0 + (y1 - y0) * step / steps)
        for dx in range(thickness):
            for dy in range(thickness):
                points.add((x + dx, y + dy))
    return points


def _indicator_images(master: tk.Misc) -> tuple[Any, Any]:
    """生成未选中 / 已选中的玻璃指示器（24x24，与旧主题尺寸一致）。"""
    unchecked = tk.PhotoImage(master=master, width=24, height=24)
    checked = tk.PhotoImage(master=master, width=24, height=24)
    # 外圈玻璃边（整圈 2px）+ 内部白到浅青的渐变，用逐行填色避免锯齿方块。
    for row in range(24):
        unchecked.put(ACCENT_LIGHT, to=(0, row, 24, row + 1))
        checked.put(ACCENT, to=(0, row, 24, row + 1))
    for row, color in enumerate(gradient_colors(((0.0, "#ffffff"), (1.0, "#dff4fd")), 20), start=2):
        unchecked.put(color, to=(2, row, 22, row + 1))
        checked.put(color, to=(2, row, 22, row + 1))
    # 勾：两条粗线段（左下短、右上长）。
    for x, y in _line_pixels(6, 12, 10, 16, 2):
        checked.put(ACCENT_DEEP, to=(x, y, x + 1, y + 1))
    for x, y in _line_pixels(10, 16, 18, 7, 2):
        checked.put(ACCENT_DEEP, to=(x, y, x + 1, y + 1))
    return unchecked, checked


# -- 主题装配 ---------------------------------------------------------------
def install(root: tk.Tk) -> list[Any]:
    """把窗口切换成 Aero 主题；返回需要保活的图片列表。"""
    style = ttk.Style(root)
    style.theme_use("clam")
    images: list[Any] = []

    # 全局字体与底色。
    style.configure(".", background=GLASS, foreground=INK, font=(FONT, 10))
    style.configure("TFrame", background=GLASS)
    style.configure("Footer.TFrame", background="#eaf8fe")
    style.configure("TLabel", background=GLASS, foreground=INK)
    style.configure("Muted.TLabel", foreground=INK_MUTED)
    style.configure("Hint.TLabel", foreground=INK_MUTED, font=(FONT, 9))
    style.configure("Title.TLabel", foreground=INK, font=(FONT, 20, "bold"))
    style.configure("Status.TLabel", background="#e3f6ff", foreground=INK_MUTED,
                    padding=(10, 4), font=(FONT, 9))
    style.configure("TLabelframe", background=GLASS_TINT, bordercolor=GLASS_EDGE,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=GLASS_TINT, foreground=ACCENT_DEEP,
                    font=(FONT, 10, "bold"))
    style.configure("TSeparator", background=GLASS_EDGE_SOFT)

    # 按钮：clam 的 raised 边框 + 白色高光 = 玻璃质感（ttk 图片元素不能做alpha圆角，
    # 所以这里不做图片按钮，避免四角出现黑块）。
    style.configure("TButton", background="#e9f8ff", foreground="#0d4664",
                    bordercolor="#8fd0e8", lightcolor="#ffffff", darkcolor="#bfe4f2",
                    relief="raised", borderwidth=1, padding=(13, 6))
    style.map(
        "TButton",
        background=[("pressed", "#b9e6f6"), ("active", "#d6f1fc")],
        lightcolor=[("pressed", "#bfe4f2")],
        darkcolor=[("pressed", "#ffffff")],
        foreground=[("disabled", "#93a7b0")],
    )
    style.configure("Accent.TButton", background="#3fb6dc", foreground="#ffffff",
                    bordercolor="#2f9cc4", lightcolor="#a8e6f8", darkcolor="#1b7fa6",
                    padding=(15, 7), font=(FONT, 10, "bold"))
    style.map("Accent.TButton",
              background=[("pressed", "#2f9cc4"), ("active", "#57c6e8")])

    # 输入类控件。
    for name in ("TEntry", "TCombobox"):
        style.configure(name, fieldbackground="#ffffff", foreground=INK,
                        bordercolor=GLASS_EDGE, lightcolor="#ffffff",
                        darkcolor="#ffffff", insertcolor=INK, padding=(8, 5))
        style.map(name, bordercolor=[("focus", ACCENT)],
                  fieldbackground=[("readonly", "#f2fbff")])

    # 表格 / 导航。
    style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                    foreground="#123847", bordercolor=GLASS_EDGE_SOFT,
                    lightcolor=GLASS_EDGE_SOFT, darkcolor=GLASS_EDGE_SOFT, rowheight=28)
    style.configure("Treeview.Heading", background="#dff3fc", foreground="#0d5570",
                    relief="flat", padding=(6, 6), font=(FONT, 9, "bold"))
    style.map("Treeview", background=[("selected", "#39a8cf")],
              foreground=[("selected", "#ffffff")])
    style.layout("Navigation.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Navigation.Treeview", background=RAIL, fieldbackground=RAIL,
                    foreground=RAIL_INK_SOFT, rowheight=32, borderwidth=0,
                    font=(FONT, 10))
    style.map("Navigation.Treeview", background=[("selected", RAIL_SELECT)],
              foreground=[("selected", "#ffffff")])

    # 滚动条：天蓝色滑块 + 玻璃槽。
    for orient in ("Vertical", "Horizontal"):
        name = f"{orient}.TScrollbar"
        style.configure(name, background="#bfe7f6", troughcolor="#eaf8fe",
                        bordercolor="#d3eef9", arrowcolor="#2f7f9e", relief="flat",
                        borderwidth=1)
        style.map(name, background=[("active", "#a6ddef")])

    # 进度条（少量页面在用）。
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor="#e6f6fc", bordercolor=GLASS_EDGE,
                    lightcolor=ACCENT_LIGHT, darkcolor=ACCENT)

    # 分隔面板。
    style.configure("TPanedwindow", background=GLASS)
    style.configure("Sash", sashthickness=6, gripcount=0)

    # 复选 / 单选指示器。
    unchecked, checked = _indicator_images(root)
    images.extend([unchecked, checked])
    style.element_create("Aero.Check.indicator", "image", unchecked,
                         ("selected", checked), sticky="w", width=30)
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=GLASS, foreground=INK, padding=(2, 3))
        style.layout(name, [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Aero.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        style.map(name, foreground=[("disabled", "#9aa6b9")],
                  background=[("active", GLASS)])

    # 经典控件（Text / Listbox / Menu）走 option_add，免得每个页面各写一遍。
    root.option_add("*Text.background", "#ffffff")
    root.option_add("*Text.foreground", INK)
    root.option_add("*Text.insertBackground", INK)
    root.option_add("*Text.selectBackground", "#cdeefb")
    root.option_add("*Text.selectForeground", "#0d4664")
    root.option_add("*Canvas.background", GLASS)
    root.option_add("*Listbox.background", "#ffffff")
    root.option_add("*Listbox.foreground", INK)
    root.option_add("*Listbox.selectBackground", "#cdeefb")
    root.option_add("*Listbox.selectForeground", "#0d4664")
    root.option_add("*Menu.background", "#f7fdff")
    root.option_add("*Menu.foreground", INK)
    root.option_add("*Menu.activeBackground", "#d6f1fc")
    root.option_add("*Menu.activeForeground", "#0d4664")
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Menu.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", "#39a8cf")
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    return images
