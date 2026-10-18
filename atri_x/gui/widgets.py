"""桌面端小部件：密钥输入框、可滚动下拉框、可滚动表单区、后台任务回主线程的小工具。

这里的几个东西都是被多个选项卡重复用到的交互原语，所以单独收一层：

- :class:`SecretEntry` —— ``show="*"`` 的输入框 + 「显示 / 隐藏」按钮。密钥类输入
  （LLM API Key、生图密钥、平台 token）都用它，**默认掩码**，用户点一下才明文；
- :func:`dropdown` —— 带下拉箭头、可滚动的组合框。模型名动辄几十个，用输入框手打
  既容易打错也看不到有哪些，统一换成"点箭头 → 列表 → 滚轮/方向键选"；
- :class:`ScrollFrame` —— 竖向滚动容器（``.inner`` 放内容）。表单比窗口高时用它，
  否则多出来的控件会被挤到窗口外面，鼠标点不到；
- :func:`run_async` —— 把"获取模型目录 / 下载头像"这类网络 IO 放到后台线程，结果
  经 ``widget.after`` 回到 Tk 主线程。Tk 不是线程安全的，所以绝不在子线程里碰控件。

约定：本模块只依赖 Tk 与标准库，不读配置文件、不联网（网络在调用方那层）。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

__all__ = ["MASK_CHAR", "ScrollFrame", "SecretEntry", "dropdown", "run_async"]

#: 密钥掩码字符。
MASK_CHAR = "*"

#: 后台任务轮询间隔（毫秒）。
ASYNC_POLL_MS = 120


class SecretEntry(ttk.Frame):
    """密钥输入框 + 显示/隐藏切换（默认掩码，点一下看明文）。"""

    def __init__(
        self,
        parent: Any,
        textvariable: tk.StringVar | None = None,
        *,
        width: int = 34,
        button_width: int = 6,
    ) -> None:
        super().__init__(parent)
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self._visible = False
        self.entry = ttk.Entry(self, textvariable=self.var, show=MASK_CHAR, width=width)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._toggle = ttk.Button(
            self, text="显示", width=button_width, command=self.toggle_visibility
        )
        self._toggle.pack(side=tk.LEFT, padx=(4, 0))

    # -- 显示状态 -------------------------------------------------------------
    @property
    def visible(self) -> bool:
        """当前是否明文显示。"""
        return self._visible

    def toggle_visibility(self) -> None:
        """在「掩码 / 明文」之间切换（按钮文案跟着变）。"""
        self.set_visible(not self._visible)

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        self.entry.configure(show="" if self._visible else MASK_CHAR)
        self._toggle.configure(text="隐藏" if self._visible else "显示")

    # -- 值 -------------------------------------------------------------------
    def get(self) -> str:
        """去空白后的输入值。"""
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value or "")

    def clear(self) -> None:
        """清空输入并恢复掩码（保存成功后调用，别把明文留在界面上）。"""
        self.var.set("")
        self.set_visible(False)

    def focus_set(self) -> None:  # noqa: D401 - 与 tk 同名语义
        self.entry.focus_set()

    def configure_state(self, state: str) -> None:
        """启用/禁用输入框（``tk.NORMAL`` / ``tk.DISABLED``）。"""
        self.entry.configure(state=state)


class ScrollFrame(ttk.Frame):
    """竖向滚动容器：内容放进 ``.inner``，比可视区高时可以滚轮/拖动滚动条。

    用于"表单比窗口高"的页面：不加滚动时 Tk 会把超出的控件排到窗口外面，
    用户既点不到也看不到（「进阶」页展开本地生图挂载时就踩过）。

    行为约定：

    - ``inner`` 的宽度始终等于画布宽度，所以内容仍然按横向 ``fill`` 铺开；
    - 内容没超出可视高度时自动隐藏滚动条，不占位置；
    - 只有指针落在本区域内时滚轮才滚这一块，不抢其它页面的滚轮。
    """

    def __init__(self, parent: Any, *, height: int = 240) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, height=height, takefocus=False
        )
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # 滚轮用全局绑定 + 「指针是否在本区域内」判断，而不是 <Enter>/<Leave>：
        # 指针移到子控件（输入框、下拉框）上时容器会收到 <Leave>，那种写法会失效。
        for sequence, handler in (
            ("<MouseWheel>", self._on_wheel),
            ("<Button-4>", self._on_wheel_x11),
            ("<Button-5>", self._on_wheel_x11),
        ):
            try:
                self.bind_all(sequence, handler, add="+")
            except tk.TclError:  # pragma: no cover - 窗口销毁
                pass

    # -- 滚动条 / 滚动范围 -----------------------------------------------------
    def _on_inner_configure(self, _event: Any = None) -> None:
        self._sync_scrollregion()
        self._sync_scrollbar()

    def _on_canvas_configure(self, event: Any) -> None:
        """让内容宽度跟随画布（等价于横向 fill=X）。"""
        try:
            self.canvas.itemconfigure(self._window, width=event.width)
        except tk.TclError:  # pragma: no cover - 窗口销毁
            return
        self._sync_scrollbar()

    def _sync_scrollregion(self) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:  # pragma: no cover - 窗口销毁
            pass

    def _sync_scrollbar(self) -> None:
        """内容装得下就收起滚动条，装不下再显示。"""
        try:
            needed = self.inner.winfo_reqheight() > self.canvas.winfo_height()
            shown = bool(self.scrollbar.winfo_ismapped())
            if needed and not shown:
                self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            elif not needed and shown:
                self.scrollbar.pack_forget()
        except tk.TclError:  # pragma: no cover - 窗口销毁
            pass

    # -- 滚轮 -----------------------------------------------------------------
    def _pointer_inside(self) -> bool:
        """指针当前是否落在本区域内（含所有子控件）。"""
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
        except tk.TclError:  # pragma: no cover - 窗口销毁
            return False
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_wheel(self, event: Any) -> str | None:
        """Windows / macOS 的滚轮事件（``delta`` 以 120 为一格）。"""
        if not self._scrollable() or not self._pointer_inside():
            return None
        step = -1 if int(event.delta) > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")
        return "break"

    def _on_wheel_x11(self, event: Any) -> str | None:
        """X11 的滚轮事件（Button-4 向上、Button-5 向下）。"""
        if not self._scrollable() or not self._pointer_inside():
            return None
        self.canvas.yview_scroll(-3 if int(event.num) == 4 else 3, "units")
        return "break"

    def _scrollable(self) -> bool:
        try:
            return self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        except tk.TclError:  # pragma: no cover - 窗口销毁
            return False

    # -- 对外 ---------------------------------------------------------------
    def scroll_to_top(self) -> None:
        """回到顶部（切换任务/重新载入后调用，别让用户莫名停在中间）。"""
        try:
            self.canvas.yview_moveto(0.0)
        except tk.TclError:  # pragma: no cover - 窗口销毁
            pass

    def yview(self) -> tuple[float, float]:
        """当前可视区间 ``(首, 尾)``，0~1；用来断言"确实能滚"。"""
        return tuple(self.canvas.yview())  # type: ignore[return-value]


def dropdown(
    parent: Any,
    textvariable: tk.StringVar,
    *,
    values: Any = (),
    width: int = 28,
    height: int = 12,
    state: str = "normal",
    on_open: Callable[[], None] | None = None,
) -> ttk.Combobox:
    """带下拉箭头、可滚动的组合框。

    ``height`` 是一次展开能看到几行，超过的部分 Tk 自己会加滚动条（滚轮/方向键都能选）。
    ``on_open`` 会在每次点箭头展开前调用，用来即时刷新候选（比如刚获取到的模型目录）。
    """
    box = ttk.Combobox(
        parent,
        textvariable=textvariable,
        values=list(values),
        width=width,
        height=height,
        state=state,
    )
    if on_open is not None:
        box.configure(postcommand=on_open)
    return box


def run_async(
    widget: Any,
    work: Callable[[], Any],
    on_done: Callable[[Any, BaseException | None], None],
) -> threading.Thread:
    """在后台线程跑 ``work()``，完成后回 Tk 主线程调 ``on_done(结果, 异常)``。

    ``on_done`` 一定在 Tk 主线程里执行（用 ``widget.after`` 轮询结果队列），因此回调
    里可以安全地改控件。窗口已销毁时静默丢弃结果。
    """
    box: queue.Queue[tuple[Any, BaseException | None]] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            box.put((work(), None))
        except BaseException as exc:  # noqa: BLE001 - 交给界面显示
            box.put((None, exc))

    thread = threading.Thread(target=runner, name="atri-x-gui-async", daemon=True)
    thread.start()

    def poll() -> None:
        try:
            value, error = box.get_nowait()
        except queue.Empty:
            try:
                widget.after(ASYNC_POLL_MS, poll)
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                return
            return
        on_done(value, error)

    try:
        widget.after(ASYNC_POLL_MS, poll)
    except tk.TclError:  # pragma: no cover - 窗口已销毁
        pass
    return thread
