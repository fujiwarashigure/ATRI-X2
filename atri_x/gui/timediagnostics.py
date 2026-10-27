"""后台 ``/diagnostics`` 时间段的面板（只读，不发网络校验、不改任何配置）。

从「时间」选项卡里拆出来：网络校验的**运行状态**只存在于后台进程里（"看到几条证据、
最近一次成功是什么时候、每条来源的偏移是多少、有没有告警"），所以这一段必须去读后台
的只读诊断接口。拆成独立控件后，"怎么拉 / 怎么渲染"可以单独测，不必建整个选项卡。

约束（与 :mod:`atri_x.gui.timeview` 一致）：

* 只读、只访问本机回环地址；地址与令牌来自 ``.env`` 的 ``ATRIX_HEALTH_ADDR`` /
  ``ATRIX_OPS_TOKEN``（桌面端启动时会自动补齐）；
* 拿不到就**只显示提示**，不弹窗、不影响同一页的其它区块；
* 网络请求走 :func:`atri_x.gui.widgets.run_async`（后台线程 + 回主线程），不卡界面。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from atri_x.gui import timeview, widgets

__all__ = ["DiagnosticsPanel", "OBSERVATION_COLUMNS"]

#: 读后台 ``/diagnostics`` 的超时（秒）：本机回环，慢就是有问题。
DIAGNOSTICS_TIMEOUT = 2.5

#: 只读表格：后台每条来源最近一次的观测。
OBSERVATION_COLUMNS = (
    ("provider_id", "来源", 140),
    ("protocol", "协议", 60),
    ("group", "分组", 90),
    ("status", "状态", 120),
    ("rtt_ms", "RTT(ms)", 80),
    ("offset_ms", "偏移(ms)", 90),
    ("uncertainty_ms", "不确定度(ms)", 100),
    ("last_ok_at", "最近成功", 160),
)


class DiagnosticsPanel(ttk.Frame):
    """「后台运行状态」面板：拉取 + 渲染 ``/diagnostics`` 的 ``time`` 段。"""

    def __init__(
        self,
        parent: Any,
        paths: Any,
        *,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self._on_notice = on_notice
        self._inflight = False
        self._build()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X)
        self.diag_target = ttk.Label(bar, text="", foreground="#555555")
        self.diag_target.pack(side=tk.LEFT)
        ttk.Button(bar, text="重新读取", command=self.fetch).pack(side=tk.RIGHT)
        self.diag_error = ttk.Label(
            self, text="", foreground="#b42318", wraplength=960, justify=tk.LEFT
        )
        self.diag_error.pack(anchor="w", pady=(4, 0))
        self._diag_grid = ttk.Frame(self)
        self._diag_grid.pack(fill=tk.X, pady=(4, 0))

        table = ttk.Frame(self)
        table.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.observation_tree = ttk.Treeview(
            table,
            columns=[name for name, _t, _w in OBSERVATION_COLUMNS],
            show="headings",
            height=4,
        )
        for name, title, width in OBSERVATION_COLUMNS:
            self.observation_tree.heading(name, text=title)
            self.observation_tree.column(name, width=width, stretch=name in ("status", "last_ok_at"))
        scrollbar = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.observation_tree.yview)
        self.observation_tree.configure(yscrollcommand=scrollbar.set)
        self.observation_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)

        self._alerts_text = tk.Text(self, height=3, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self._alerts_text.pack(fill=tk.X, pady=(6, 0))
        self._alerts_text.configure(state=tk.DISABLED)
        self._set_alerts(["告警：—"])

    # ---------------------------------------------------------------- 拉取
    def fetch(self) -> None:
        """拉一次后台诊断（同一次只跑一个，重复点会忽略）。"""
        if self._inflight:
            return
        if self.paths is None:
            self.render({}, "找不到配置文件，读不到状态接口地址")
            return
        addr, token = timeview.diagnostics_credentials(self.paths)
        self.diag_target.configure(
            text=f"状态接口：{addr or '未配置'}（{timeview.HEALTH_ADDR_ENV}）"
        )
        if not addr:
            self.render(
                {},
                f"未配置状态接口地址（.env 的 {timeview.HEALTH_ADDR_ENV}）；"
                "桌面端启动时会自动补齐",
            )
            return
        if not token:
            self.render(
                {},
                f"缺少状态接口令牌（.env 的 {timeview.HEALTH_TOKEN_ENV}）；"
                "桌面端启动时会自动补齐",
            )
            return
        self._inflight = True
        self.diag_error.configure(text="正在读取…")

        def work() -> dict[str, Any]:
            return timeview.fetch_time_diagnostics(addr, token, timeout=DIAGNOSTICS_TIMEOUT)

        widgets.run_async(self, work, self._done)

    def _done(self, value: Any, error: BaseException | None) -> None:
        self._inflight = False
        if error is not None:
            self.render({}, str(error))
            return
        self.render(value if isinstance(value, dict) else {}, "")

    # ---------------------------------------------------------------- 渲染
    def render(self, section: dict[str, Any], error: str) -> None:
        """把 ``/diagnostics`` 的 ``time`` 段画出来（``section`` 可为空）。"""
        self.diag_error.configure(text=error)
        for child in self._diag_grid.winfo_children():
            child.destroy()
        verification = section.get("verification")
        verification = verification if isinstance(verification, dict) else {}
        lines = timeview.verification_lines(verification) if verification else []
        if not lines and not error:
            lines = [("状态", "后台没有返回时间段（后台是旧版本，或校验未装配）")]
        for index, (label, text) in enumerate(lines):
            row, column = divmod(index, 2)
            cell = ttk.Frame(self._diag_grid)
            cell.grid(row=row, column=column, sticky="w", padx=(0, 26), pady=1)
            ttk.Label(cell, text=label + "：", foreground="#555555").pack(side=tk.LEFT)
            ttk.Label(cell, text=text).pack(side=tk.LEFT)
        self.observation_tree.delete(*self.observation_tree.get_children(""))
        for index, item in enumerate(timeview.verification_sources(verification)):
            self.observation_tree.insert(
                "",
                tk.END,
                iid=f"observation-{index}",
                values=(
                    str(item.get("provider_id") or "?"),
                    str(item.get("protocol") or ""),
                    str(item.get("group") or ""),
                    str(item.get("status") or ""),
                    _number_text(item.get("rtt_ms")),
                    _number_text(item.get("offset_ms")),
                    _number_text(item.get("uncertainty_ms")),
                    str(item.get("last_ok_at") or ""),
                ),
            )
        alerts = timeview.verification_alerts(verification)
        self._set_alerts(["告警：" + alerts[0]] + ["　　　" + item for item in alerts[1:]] if alerts else ["告警：无"])
        if not error and self._on_notice is not None:
            self._on_notice("已读取后台时间诊断（校验状态与每条来源的最近观测）")

    def _set_alerts(self, lines: list[str]) -> None:
        self._alerts_text.configure(state=tk.NORMAL)
        self._alerts_text.delete("1.0", tk.END)
        self._alerts_text.insert("1.0", "\n".join(lines))
        self._alerts_text.configure(state=tk.DISABLED)


def _number_text(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)
