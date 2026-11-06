"""主窗口外壳：左侧导航栏 + 玻璃页头 + 选项卡容器（Frutiger Aero 外观）。

为什么要单独一层：主窗口原来把「外壳布局」和「页面装配」写在同一个类里，
:mod:`atri_x.gui.app` 已经很长，行数棘轮只许变短。这里只负责：

- 左侧导航（分组 + 页 + 页内子面板，全部合并进同一棵树；不再有第二套标签栏）；
- 玻璃页头（标题 / 连接状态 / 重新连接 / 背景插画）；
- 选项卡容器（``self.notebook``）与页内 Notebook 的"收编"（:meth:`organize`）。

对外契约与旧实现保持一致（``notebook`` / ``navigation`` / ``buttons`` /
``group_buttons`` / ``title`` / ``status`` / ``runtime`` / ``activate`` /
``organize`` / ``_books`` / ``_book_nodes`` / ``_section_pages`` / ``_active_index``），
所以 :mod:`atri_x.gui.app` 不需要改动装配逻辑。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x import __version__
from atri_x.gui import aero
from atri_x.gui.taborder import TABS

__all__ = ["DesktopShell", "UI_GROUPS"]

#: 六个一级入口；后面是归到该入口下的页索引（与旧版一致，测试按名字取）。
UI_GROUPS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("对话", (0,)),
    ("研究与知识", (12, 6)),
    ("角色与记忆", (2, 14, 5)),
    ("模型与工具", (1, 11, 17)),
    ("连接与平台", (4, 3)),
    ("系统设置", (13, 15, 8, 16, 7, 9, 10)),
)

#: 页头高度（像素）。
HEADER_HEIGHT = 122

#: 左侧导航栏宽度（像素）。
RAIL_WIDTH = 252


class DesktopShell:
    """保留原 Notebook 索引与懒加载契约的玻璃桌面布局。"""

    def __init__(self, root: tk.Tk, select: Callable[[int], None],
                 reconnect: Callable[[], None]) -> None:
        self._select = select
        self._tabs = TABS
        self._last = {name: pages[0] for name, pages in UI_GROUPS}
        self._images = aero.install(root)
        self._configure_notebook_style(root)
        self.root = root
        outer = tk.Frame(root, bg=aero.SKY)
        outer.pack(fill=tk.BOTH, expand=True)
        self._build_rail(outer)
        content = tk.Frame(outer, bg=aero.SKY)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        card = tk.Frame(content, bg=aero.GLASS_EDGE)
        card.pack(fill=tk.BOTH, expand=True, padx=14, pady=(10, 10))
        inner = tk.Frame(card, bg=aero.GLASS)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        self._build_header(inner, reconnect)
        tk.Frame(inner, bg=aero.HEADER_LINE, height=1).pack(fill=tk.X)
        footer = tk.Frame(inner, bg=aero.GLASS)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=(4, 3))
        self.runtime = ttk.Label(footer, text="", style="Muted.TLabel")
        self.runtime.pack(side=tk.RIGHT)
        ttk.Label(footer, text="ATRI-X · SEASIDE", style="Hint.TLabel").pack(side=tk.LEFT)
        self._build_workspace(inner)
        self.activate(0)

    # ---------------------------------------------------------------- 样式
    @staticmethod
    def _configure_notebook_style(root: tk.Tk) -> None:
        """页内 Notebook 一律去掉标签栏：子页合并进左侧导航树。"""
        style = ttk.Style(root)
        style.configure("Workspace.TNotebook", borderwidth=0, tabmargins=0,
                        background=aero.GLASS)
        style.layout("Workspace.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
        style.layout("Workspace.TNotebook.Tab", [])

    # ---------------------------------------------------------------- 左侧导航
    def _build_rail(self, outer: tk.Frame) -> None:
        rail = tk.Frame(outer, bg=aero.RAIL, width=RAIL_WIDTH)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        rail.pack_propagate(False)
        self._logo = tk.Canvas(rail, height=164, bg=aero.RAIL_TOP,
                               highlightthickness=0, borderwidth=0)
        self._logo.pack(fill=tk.X)
        self._logo.bind("<Configure>", self._paint_logo)
        tk.Frame(rail, bg=aero.ACCENT_LIGHT, height=2).pack(fill=tk.X, padx=18)
        nav = tk.Frame(rail, bg=aero.RAIL)
        nav.pack(fill=tk.BOTH, expand=True, padx=(10, 6), pady=(10, 6))
        self.navigation = ttk.Treeview(nav, show="tree", selectmode="browse",
                                       style="Navigation.Treeview")
        nav_scroll = ttk.Scrollbar(nav, orient=tk.VERTICAL, command=self.navigation.yview)
        nav_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.navigation.configure(yscrollcommand=nav_scroll.set)
        self.navigation.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.navigation.column("#0", width=212, minwidth=150)
        self.navigation.tag_configure("group", font=(aero.FONT, 10, "bold"),
                                      foreground="#eaf9ff")
        self.navigation.tag_configure("leaf", font=(aero.FONT, 10),
                                      foreground=aero.RAIL_INK_SOFT)
        bottom = tk.Frame(rail, bg=aero.RAIL_DEEP)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(bottom, text=f"AERO GLASS · v{__version__}", bg=aero.RAIL_DEEP,
                 fg=aero.RAIL_INK_SOFT, font=("Segoe UI", 8)).pack(pady=8)
        self.group_buttons: dict[str, str] = {}
        self.buttons = {page: f"page:{page}" for _, pages in UI_GROUPS for page in pages}
        self._book_nodes: dict[str, tuple[Any, Any]] = {}
        self._books: list[Any] = []
        for number, (name, pages) in enumerate(UI_GROUPS):
            group = f"page:{pages[0]}" if len(pages) == 1 else f"group:{number}"
            self.group_buttons[name] = group
            self.navigation.insert("", "end", iid=group, text=name,
                                   open=number == 0, tags=("group",))
            if len(pages) > 1:
                for page in pages:
                    self.navigation.insert(group, "end", iid=f"page:{page}",
                                           text=TABS[page], tags=("leaf",))
        self.navigation.bind("<<TreeviewSelect>>", self._choose)

    def _paint_logo(self, _event: Any = None) -> None:
        """重画导航栏顶部渐变与品牌字（窗口尺寸变化时也要重画）。"""
        canvas = self._logo
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.delete("all")
        aero.draw_vertical_gradient(canvas, 0, 0, width, height,
                                    ((0.0, "#1b7599"), (0.32, aero.RAIL_TOP),
                                     (1.0, aero.RAIL)))
        canvas.create_rectangle(0, 0, width, 1, fill="#cdeefb", outline="#cdeefb")
        canvas.create_rectangle(0, 1, width, 2, fill="#7fc4dc", outline="#7fc4dc")
        canvas.create_text(22, 40, anchor="w", text="ATRI / X", fill="#ffffff",
                           font=("Segoe UI", 23, "bold"))
        canvas.create_text(23, 74, anchor="w", text="星海 · 智能工作空间",
                           fill=aero.RAIL_INK_SOFT, font=(aero.FONT, 9))
        canvas.create_rectangle(22, 96, 88, 118, fill="#12567a", outline="#3fb6dc")
        canvas.create_text(55, 107, text="AERO", fill="#bfe9fb",
                           font=("Segoe UI", 8, "bold"))

    # ---------------------------------------------------------------- 页头
    def _build_header(self, parent: tk.Frame, reconnect: Callable[[], None]) -> None:
        """页头用 Canvas 现画：渐变 / 高光 / 气泡 / 角色图都不会出现拼接接缝。"""
        header = tk.Canvas(parent, height=HEADER_HEIGHT, bg=aero.HEADER_BG,
                           highlightthickness=0, borderwidth=0)
        header.pack(fill=tk.X)
        self._header = header
        self._mascot = aero.load_brand_image(self.root, "mascot-header.png")
        self._wallpaper = self._mascot
        self._title_plate = tk.Frame(header, bg="#e8f7fe", highlightthickness=1,
                                     highlightbackground="#d3ecf8")
        self.title = tk.Label(self._title_plate, text="", bg="#e8f7fe", fg=aero.INK,
                              anchor="w", font=(aero.FONT, 20, "bold"))
        self.title.pack(padx=14, pady=5)
        self._title_window = header.create_window(16, 28, anchor="nw",
                                                  window=self._title_plate)
        self._connection = tk.Frame(header, bg="#e8f7fe", highlightthickness=1,
                                    highlightbackground="#d3ecf8")
        self.status = tk.Label(self._connection, text="● 未连接", bg="#e8f7fe",
                               fg=aero.INK_MUTED, font=(aero.FONT, 9))
        self.status.pack(side=tk.LEFT, padx=(14, 12), pady=8)
        ttk.Button(self._connection, text="重新连接", command=reconnect).pack(
            side=tk.LEFT, padx=(0, 12))
        self._connection_window = header.create_window(430, 40, anchor="nw",
                                                       window=self._connection)
        header.bind("<Configure>", self._paint_header)

    def _paint_header(self, _event: Any = None) -> None:
        """重画页头背景（渐变 / 高光 / 气泡 / 角色），窗口缩放时也会被调用。"""
        canvas = self._header
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 40 or height < 40:
            return
        canvas.delete("deco")
        rows = aero.gradient_colors(
            ((0.0, "#c6e9f8"), (0.42, "#e2f6fd"), (1.0, "#effbff")), height)
        for row, color in enumerate(rows):
            canvas.create_rectangle(0, row, width, row + 1, fill=color,
                                    outline=color, tags="deco")
        for row in range(min(30, height)):
            ratio = 1.0 - row / 30
            canvas.create_rectangle(
                0, row, width, row + 1,
                fill=aero.mix(rows[row], "#ffffff", 0.25 + 0.35 * ratio),
                outline="", tags="deco")
        for relx, rely, radius in ((0.30, 0.34, 22), (0.40, 0.64, 12),
                                   (0.52, 0.22, 16), (0.63, 0.70, 9),
                                   (0.24, 0.72, 8), (0.70, 0.30, 7)):
            x, y = width * relx, height * rely
            ring = aero.mix(rows[min(height - 1, int(y))], "#ffffff", 0.55)
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius,
                               outline=ring, width=2, tags="deco")
            canvas.create_oval(x - radius * 0.45, y - radius * 0.6,
                               x - radius * 0.1, y - radius * 0.2,
                               fill=aero.mix(ring, "#ffffff", 0.7), outline="",
                               tags="deco")
        if self._mascot is not None:
            mx = width - 22
            base = rows[height // 2]
            for radius, strength in ((86, 0.45), (58, 0.65), (36, 0.85)):
                glow = aero.mix(base, "#ffffff", strength)
                canvas.create_oval(mx - 68 - radius, height * 0.52 - radius,
                                   mx - 68 + radius, height * 0.52 + radius,
                                   fill=glow, outline=glow, tags="deco")
            canvas.create_image(mx, -4, anchor="ne", image=self._mascot,
                                tags="deco")
        canvas.create_text(24, 15, anchor="w", text="A T R I   /   S E A S I D E",
                           fill="#3d7f99", font=("Segoe UI", 9), tags="deco")
        canvas.create_rectangle(24, 96, 106, 99, fill=aero.ACCENT_LIGHT,
                                outline="", tags="deco")
        canvas.create_rectangle(24, 99, 106, 100, fill="#ffffff", outline="",
                                tags="deco")
        canvas.create_rectangle(0, height - 1, width, height, fill="#bfe4f2",
                                outline="", tags="deco")

    # ---------------------------------------------------------------- 工作区
    def _build_workspace(self, parent: tk.Frame) -> None:
        workspace = tk.Frame(parent, bg=aero.GLASS)
        workspace.pack(fill=tk.BOTH, expand=True)
        self._page_wallpaper = aero.load_brand_image(self.root, "aero-wallpaper.png",
                                                     "wallpaper.png")
        self.backdrop: tk.Label | None = None
        if self._page_wallpaper is not None:
            self.backdrop = tk.Label(workspace, image=self._page_wallpaper,
                                     borderwidth=0, bg=aero.GLASS, anchor="se")
            self.backdrop.place(relx=1, rely=1, anchor="se")
        self.notebook = ttk.Notebook(workspace, style="Workspace.TNotebook")
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=(12, 12), pady=(10, 6))
        self._background_frames: set[str] = set()
        self._check_images = [image for image in self._images if image.width() == 24]

    # ---------------------------------------------------------------- 导航逻辑
    def _choose(self, _event: Any = None) -> None:
        selection = self.navigation.selection()
        if not selection:
            return
        node = selection[0]
        if node.startswith("page:"):
            self._select(int(node.split(":")[1]))
        elif node in self._book_nodes:
            chain = []
            current = node
            while current in self._book_nodes:
                chain.append(self._book_nodes[current])
                current = self.navigation.parent(current)
            for book, tab in reversed(chain):
                book.select(tab)
        elif node.startswith("group:"):
            name, _ = UI_GROUPS[int(node.split(":")[1])]
            self.navigation.item(node, open=True)
            self._select(self._last[name])

    def activate(self, index: int) -> None:
        """切页：更新页头标题、导航展开与选中（页内子面板也在这里可见）。"""
        name, pages = next((n, p) for n, p in UI_GROUPS if index in p)
        self._last[name] = index
        self._section_pages = pages
        self._active_index = index
        self.title.configure(text=f"{name} / {self._tabs[index]}" if len(pages) > 1 else name)
        group = self.group_buttons[name]
        for item in self.group_buttons.values():
            self.navigation.item(item, open=item == group)
        node = f"page:{index}"
        selected = self.navigation.selection()
        if not selected or not (selected[0] == node or selected[0].startswith(f"sub:{index}:")):
            self.navigation.selection_set(node)
        self.navigation.see(node)

    def organize(self, parent: Any, index: int) -> None:
        """页内 Notebook 也纳入同一棵导航树，不再放第二套下拉入口。"""
        def walk(widget: Any, node: str) -> None:
            if isinstance(widget, ttk.Frame) and self._page_wallpaper is not None:
                widget.configure(style="TFrame")
                self._background_frames.add(str(widget))
            if isinstance(widget, ttk.LabelFrame) and self._page_wallpaper is not None:
                widget.configure(style="TLabelframe")
            for child in widget.winfo_children():
                if isinstance(child, ttk.Notebook):
                    child.configure(style="Workspace.TNotebook", padding=0)
                    if child not in self._books:
                        self._books.append(child)
                        for position, tab in enumerate(child.tabs()):
                            key = f"sub:{index}:{len(self._books)}:{position}"
                            self.navigation.insert(
                                node, "end", iid=key,
                                text=str(child.tab(tab, "text")).strip(),
                                tags=("leaf",))
                            self._book_nodes[key] = (child, tab)
                            walk(child.nametowidget(tab), key)
                        child.bind("<<NotebookTabChanged>>",
                                   lambda _e: self._sync_books(), add="+")
                else:
                    walk(child, node)
        walk(parent, f"page:{index}")
        self.navigation.item(f"page:{index}", open=True)

    def _sync_books(self) -> None:
        for node, (book, tab) in self._book_nodes.items():
            if (node.startswith(f"sub:{self._active_index}:") and book.winfo_ismapped()
                    and book.select() == tab):
                self.navigation.item(self.navigation.parent(node), open=True)
                self.navigation.selection_set(node)
                self.navigation.see(node)
