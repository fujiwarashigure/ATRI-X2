"""「性能 / 并发」选项卡：一键切并发模式，外加一块后台并发实况。

这一页回答两个问得最多的问题：

* **"能不能调大一点？"** —— 三个预设（省资源 / 标准 / 高并发）一键把 11 个上限填进
  输入框，改完点「应用并保存」才落盘；
* **"现在到底卡在哪？"** —— 「并发实况」直接读后台 ``/metrics``：几个 turn/tool/hook
  在跑、每个 kind 排了几条、拒绝了多少、最老的请求等了多久、生图闸门占了几槽、
  Provider 限速有没有配。后台没起就只在这一行写中文提示，**不弹窗**（那是常态）。

口径说明（写在页面顶部，避免把它当成"多核加速开关"）：ATRI-X 的并发是 asyncio 协程，
不是多进程。不同群 / 私聊的回合本来就在同一个事件循环里并行；真正串行的是**同一个群
的会话**（thread-mode group，按回合排队）。这里调的是"同时在跑多少"和"满了排多久"，
既不会用上更多 CPU 核心，也不会让单个回合变快。

约束：配置读写走 :mod:`atri_x.gui.perfconfig`（行级补丁 + 回读校验），网络请求走
:func:`atri_x.gui.widgets.run_async`（后台线程 + 回主线程），Tk 只在主线程碰。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import perfconfig, widgets
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.tabs.base import ConfigTab, confirm

__all__ = ["PerformanceTab", "REFRESH_INTERVAL_MS", "REQUEST_COLUMNS"]

#: 自动刷新间隔（毫秒）。
REFRESH_INTERVAL_MS = 5000

#: 「并发实况」表格的列：``(字段, 表头, 宽度, 是否吸收剩余宽度)``。
REQUEST_COLUMNS: tuple[tuple[str, str, int, bool], ...] = (
    ("kind", "类型", 80, False),
    ("session_id", "会话", 220, True),
    ("fair_key", "公平键", 170, True),
    ("running", "已运行", 90, False),
    ("id", "请求 id", 90, False),
)

#: 还没拉过数据时的状态行文案。
LIVE_IDLE_TEXT = (
    "点「刷新」读取后台当前并发（后台服务没启动时只在这里提示，不弹窗）；"
    "勾「自动刷新」后每 5 秒拉一次，切走本页即停。"
)

#: 每个键在界面上的标题与右解释。
_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "max_concurrent_turns": ("同时运行的回合（turn）上限", "0 = 不限"),
    "max_concurrent_tools": ("同时运行的工具（tool）上限", "0 = 不限"),
    "max_concurrent_hooks": ("同时运行的 Hook 上限", "0 = 不限"),
    "provider_max_concurrent": ("同一 Provider 的调用并发上限", "0 = 不限"),
    "queue_max_size": ("并发满时允许排队的请求数", "0 = 不排队，直接拒绝"),
    "queue_wait_timeout_seconds": ("排队最长等待（秒）", "0 = 不限时"),
    "image_max_concurrent": ("同时生成的图片数上限", "0 = 不限"),
    "image_queue_size": ("生图超限时的排队数", "0 = 不排队"),
    "preprocess_workers": ("入站预处理工作线程", "0 = 内置默认（4）"),
    "preprocess_queue_size": ("入站预处理队列长度", "0 = 内置默认（256）"),
    "high_priority_workers": ("高优先线程（撤回 / 入群 / 管理）", "0 = 内置默认（1）"),
}

#: 顶部的口径说明。
_INTRO_TEXT = (
    "ATRI-X 的并发是 asyncio 协程，不是多进程：不同群 / 私聊的回合本来就在同一个事件循环里"
    "并行；真正会串行的是同一个群的会话（thread-mode group）按回合排队。\n"
    "下面调的是「同时在跑多少」和「并发满了以后排多久」——调大不会让单个回合变快，"
    "也不会用上更多 CPU 核心；调过 Provider 上限反而会把并发压力换成 429。\n"
    "这些键都是启动期配置：保存后点「重启后台服务」（或菜单「服务 → 重启后台服务」）才生效。"
)


class PerformanceTab(ConfigTab):
    """并发模式 + 并发上限 + 后台并发实况。"""

    hint = "改完需要重启后台服务才生效"
    scrollable = True

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._cfg = perfconfig.PerfConfig()
        self._vars: dict[str, tk.StringVar] = {}
        self._qq_group: Any = None
        self._image_group: Any = None
        self._inflight = False
        self._job: str | None = None
        self._visible = False
        try:
            self.frame.bind("<Destroy>", self._on_destroy, add="+")
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass
        self._build()
        self.reload()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        ttk.Label(
            self.body,
            text=_INTRO_TEXT,
            foreground="#555555",
            wraplength=880,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))
        self._build_presets()
        self._build_forms()
        self._build_live()
        self.button("保存并应用", self._save)
        self.button("重新载入", self.reload)

    def _build_presets(self) -> None:
        row = ttk.Frame(self.body)
        row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(row, text="并发模式：").pack(side=tk.LEFT)
        self.preset_buttons: dict[str, ttk.Button] = {}
        for name in (perfconfig.PRESET_LOW, perfconfig.PRESET_NORMAL, perfconfig.PRESET_HIGH):
            button = ttk.Button(row, text=name, command=lambda item=name: self.apply_preset(item))
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.preset_buttons[name] = button
        ttk.Button(row, text="应用并保存", command=self._save).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(
            row, text="（点预设只填下面的输入框，不写盘）", foreground="#8a8a8a"
        ).pack(side=tk.LEFT, padx=(8, 0))

    def _build_forms(self) -> None:
        # 提示行先建、后 pack：``_render_notices`` 用 ``before=<对应组>`` 把它插到该组
        # 表单正上方；不需要时整行 pack_forget，不占位置。
        self._qq_notice = ttk.Label(
            self.body, text="", foreground="#8a8a8a", wraplength=880, justify=tk.LEFT
        )
        self._image_notice = ttk.Label(
            self.body, text="", foreground="#8a8a8a", wraplength=880, justify=tk.LEFT
        )
        for index, (title, keys) in enumerate(perfconfig.GROUPS):
            group = ttk.LabelFrame(self.body, text=title, padding=8)
            group.pack(fill=tk.X, pady=(0, 8))
            if index == 1:
                self._image_group = group
            elif index == 2:
                self._qq_group = group
            for row, key in enumerate(keys):
                self._form_row(group, row, key)

    def _form_row(self, parent: Any, row: int, key: str) -> None:
        label, hint = _FIELD_LABELS.get(key, (key, ""))
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2, padx=(0, 10))
        variable = tk.StringVar(value="0")
        self._vars[key] = variable
        ttk.Spinbox(
            parent,
            from_=perfconfig.VALUE_MIN,
            to=perfconfig.VALUE_MAX,
            textvariable=variable,
            width=8,
            increment=1,
        ).grid(row=row, column=1, sticky="w", pady=2)
        ttk.Label(parent, text=f"{hint}　{key}", foreground="#8a8a8a").grid(
            row=row, column=2, sticky="w", padx=(10, 0), pady=2
        )
        parent.columnconfigure(2, weight=1)

    def _build_live(self) -> None:
        box = ttk.LabelFrame(self.body, text="并发实况（读后台 /metrics）", padding=8)
        box.pack(fill=tk.BOTH, expand=True)
        bar = ttk.Frame(box)
        bar.pack(fill=tk.X)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar,
            text=f"自动刷新（{REFRESH_INTERVAL_MS // 1000} 秒）",
            variable=self.auto_var,
            command=self._toggle_auto,
        ).pack(side=tk.LEFT)
        ttk.Button(bar, text="刷新", command=self.refresh_now).pack(side=tk.RIGHT)
        self._live_status = ttk.Label(
            box, text=LIVE_IDLE_TEXT, foreground="#555555", wraplength=880, justify=tk.LEFT
        )
        self._live_status.pack(anchor="w", pady=(4, 0))
        self.summary_text = tk.Text(box, height=8, width=1, wrap=tk.WORD)
        self.summary_text.pack(fill=tk.X, pady=(6, 0))
        self.summary_text.configure(state=tk.DISABLED)
        table = ttk.Frame(box)
        table.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.request_tree = ttk.Treeview(
            table,
            columns=[name for name, _title, _width, _stretch in REQUEST_COLUMNS],
            show="headings",
            height=4,
        )
        for name, title, width, stretch in REQUEST_COLUMNS:
            self.request_tree.heading(name, text=title)
            self.request_tree.column(name, width=width, stretch=stretch)
        scrollbar = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.request_tree.yview)
        self.request_tree.configure(yscrollcommand=scrollbar.set)
        self.request_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)

    # ---------------------------------------------------------------- 预设
    def apply_preset(self, name: str) -> None:
        """把预设值填进输入框（**不落盘**；只有 :meth:`_save` 会写文件）。"""
        preset = perfconfig.PRESETS.get(name)
        if preset is None:
            self.fail(f"未知的并发模式：{name}")
            return
        for key, value in preset.items():
            variable = self._vars.get(key)
            if variable is not None:
                variable.set(str(value))
        self.notify(f"已填入「{name}」；点「应用并保存」才会写盘")

    # ---------------------------------------------------------------- 载入
    def reload(self) -> None:
        """从配置文件回填所有输入框。"""
        if not self.is_ready():
            return
        try:
            config = perfconfig.read_perf_config(self.require_paths())
        except perfconfig.PerfConfigError as exc:
            self.fail(exc)
            return
        self._cfg = config
        for key, variable in self._vars.items():
            variable.set(str(getattr(config, key)))
        self._render_notices()
        self.notify("已载入并发配置")

    def _render_notices(self) -> None:
        """``[platform.qqonebot]`` / ``[image_generation]`` 原本不存在时，在对应组上方给一行灰字。"""
        self._toggle_notice(
            self._qq_notice,
            self._qq_group,
            ""
            if self._cfg.qq_section_present
            else "保存后会新建该小节（只写三个池参数，不会因此启用 QQ 平台）。",
        )
        self._toggle_notice(
            self._image_notice,
            self._image_group,
            ""
            if self._cfg.image_section_present
            else "这份配置里没有 [image_generation] 小节，保存后会新建（容量按生图端点的实际并发填）。",
        )

    def _toggle_notice(self, label: Any, group: Any, text: str) -> None:
        try:
            label.configure(text=text)
            if text:
                label.pack(fill=tk.X, pady=(0, 4), before=group)
            else:
                label.pack_forget()
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    # ---------------------------------------------------------------- 保存
    def _collect(self) -> dict[str, int]:
        """读输入框；不是整数就抛中文错误（不静默改成 0）。"""
        values: dict[str, int] = {}
        for key, variable in self._vars.items():
            text = str(variable.get()).strip()
            try:
                values[key] = int(text)
            except ValueError:
                raise perfconfig.PerfConfigError(
                    f"「{_FIELD_LABELS.get(key, (key, ''))[0]}」必须是整数（现在是 {text!r}）"
                ) from None
        return values

    def _save(self) -> None:
        try:
            paths = self.require_paths()
            values = self._collect()
        except (perfconfig.PerfConfigError, ConfigEditError) as exc:
            self.fail(exc)
            return
        lines = [
            f"{_FIELD_LABELS.get(key, (key, ''))[0]} = {values[key]}"
            for key in perfconfig.FIELD_KEYS
        ]
        if not confirm(
            self.frame,
            "保存并发配置",
            "写入这些并发上限？\n\n" + "\n".join(lines) + "\n\n保存后需要重启后台服务才生效。",
        ):
            self.notify("已取消保存（输入框里的值还在，可以改完再存）")
            return
        try:
            perfconfig.save_perf_config(paths, values)
        except (perfconfig.PerfConfigError, ConfigEditError) as exc:
            self.fail(exc)
            return
        self.journal(
            "保存并发配置（[ops] 并发/排队 + [platform.qqonebot] 预处理池 + [image_generation] 生图闸门）"
        )
        self.reload()
        self.notify("已保存；重启后台服务后生效")

    # ---------------------------------------------------------------- 实况
    def refresh_now(self) -> None:
        """拉一次后台 ``/metrics``（同一次只跑一个，重复点会忽略）。"""
        if self._inflight:
            return
        if not self.is_ready():
            self._live_status.configure(text="找不到配置文件，读不到状态接口地址")
            return
        self._inflight = True
        self._live_status.configure(text="正在读取后台并发实况…")
        paths = self.paths

        def work() -> dict[str, Any]:
            return perfconfig.fetch_perf_metrics(paths)

        widgets.run_async(self.frame, work, self._refresh_done)

    def _refresh_done(self, value: Any, error: BaseException | None) -> None:
        self._inflight = False
        if error is not None:
            # 后台服务没启动是常态：只写状态行，不弹窗。
            self._live_status.configure(text=f"读取失败：{error}")
            return
        payload = value if isinstance(value, dict) else {}
        rows = self._render_live(payload)
        if not payload:
            self._live_status.configure(text="后台没有返回并发数据（旧版本 /metrics？）")
            return
        self._live_status.configure(text=f"已读取后台并发实况：{rows} 个请求在跑")

    def _render_live(self, payload: dict[str, Any]) -> int:
        """把 ``/metrics`` 画进 summary 与表格，返回在跑的请求数。"""
        lines = perfconfig.metrics_summary(payload)
        self.summary_text.configure(state=tk.NORMAL)
        # 高度跟着行数走：内容装得下就不用内部滚动，滚轮全部交给外层页面
        # （Text 自己有滚轮绑定，内容超高时指针停在它上面会"卡"在这一块）。
        self.summary_text.configure(height=max(3, min(len(lines), 26)))
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert("1.0", "\n".join(f"{label}：{value}" for label, value in lines))
        self.summary_text.configure(state=tk.DISABLED)
        self.request_tree.delete(*self.request_tree.get_children(""))
        rows = perfconfig.active_request_rows(payload)
        for index, row in enumerate(rows):
            self.request_tree.insert(
                "",
                tk.END,
                iid=f"perf-request-{index}",
                values=(
                    row.get("kind") or "—",
                    row.get("session_id") or "—",
                    row.get("fair_key") or "—",
                    _seconds_text(row.get("running")),
                    row.get("id") or "—",
                ),
            )
        return len(rows)

    # ---------------------------------------------------------------- 轮询
    def on_show(self) -> None:
        """切到本页：勾了自动刷新就恢复轮询（不重新载入，免得冲掉没保存的输入）。"""
        self._visible = True
        if self.auto_var.get():
            self._schedule(0)

    def on_hide(self) -> None:
        """切走本页：停掉轮询（"切走页面不算"）。"""
        self._visible = False
        self._cancel()

    def _toggle_auto(self) -> None:
        if self.auto_var.get():
            self._schedule(0)
        else:
            self._cancel()
            self.notify("已停止自动刷新")

    def _schedule(self, delay: int | None = None) -> None:
        self._cancel()
        if not self.auto_var.get() or not self._visible:
            return
        interval = REFRESH_INTERVAL_MS if delay is None else max(0, int(delay))
        try:
            self._job = self.frame.after(interval, self._tick)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            self._job = None

    def _cancel(self) -> None:
        if self._job is None:
            return
        try:
            self.frame.after_cancel(self._job)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        self._job = None

    def _tick(self) -> None:
        self._job = None
        if not self.auto_var.get() or not self._visible:
            return
        self.refresh_now()
        self._schedule()

    def _on_destroy(self, event: Any = None) -> None:
        """页面销毁时撤掉 ``after`` 任务（否则回调会打到一个已经没了的控件上）。"""
        widget = getattr(event, "widget", None)
        if widget is not None and widget is not self.frame:
            return
        self._visible = False
        self._cancel()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _seconds_text(value: Any) -> str:
    """已运行秒数 → ``"12.3 秒"``（拿不到时间戳时是 ``"—"``）。"""
    if value is None:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{seconds:.1f} 秒"
