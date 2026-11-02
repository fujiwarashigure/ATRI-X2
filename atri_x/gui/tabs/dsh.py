"""「DSH」选项卡：内置 DSH 内核的深度路由配置 + 状态自检。

为什么需要这一页：内置 DSH 只在"内核可用"时才接管复杂任务，而"可用"取决于两件事——

1. **凭据**：用哪个 ATRI-X Provider 的密钥喂内核，以及该 Provider 的 ``base_url``
   是不是 Anthropic Messages 端点（内核只往 ``<base_url>/v1/messages`` 发请求；
   OpenAI 兼容网关必然 404，这是"深度路由从没生效"的最常见原因）；
2. **内核模型目录**：内核只认它目录里列出的模型 id，而 Anthropic 兼容网关（如
   OpenCode Go）实际提供的 id 往往不在内核自带目录里，id 对不上时请求直接 400。

这两处过去只能手改 ``app.toml`` / ``cordis.patch.yml``，桌面端没有入口，换机器或
重装后就容易静默失效。本页把它们做成可保存的表单，并提供「一键配置（推荐）」和
「端到端自检」（真的拉起内核跑一轮 prompt）。

拆分：本模块只放路由 / 凭据表单与保存；模型目录、状态与自检区块在
:mod:`atri_x.gui.tabs.dshpanels`（单文件行数预算）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import dshcatalog, dshconfig, widgets
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.tabs.base import ConfigTab
from atri_x.gui.tabs.dshpanels import DshPanelsMixin

__all__ = ["DshTab"]

#: 协议下拉的显示名（顺序与 :data:`dshconfig.PROTOCOLS` 一致）。
_PROTOCOL_LABELS: list[str] = [dshconfig.PROTOCOL_LABELS[key] for key in dshconfig.PROTOCOLS]


class DshTab(DshPanelsMixin, ConfigTab):
    """内置 DSH：路由参数 / 凭据诊断 / 内核模型目录 / 状态与自检。"""

    hint = "改完需要重启后台服务才生效"
    scrollable = True

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self.settings = dshconfig.DshSettings()
        self.catalog = dshcatalog.KernelCatalog()
        self._providers: dict[str, Any] = {}
        self._status_text: Any = None
        self._status_note = ""
        self._smoke_running = False
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.button("保存", self._save)
        self.button("保存并重启后台服务", self._save_and_restart)
        self.button("一键配置（推荐）", self._apply_recommended)
        self._smoke_button = self.button("端到端自检", self._run_smoke)
        self.button("刷新状态", self._refresh_status)
        self.button("重新载入", self.reload)

        self._build_router(self.body)
        self._build_credential(self.body)
        self._build_catalog(self.body)
        self._build_status(self.body)

    def _build_router(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="路由（写 app.toml 的 [agent.dsh]）", padding=8)
        box.pack(fill=tk.X, pady=(0, 8))
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box,
            text="启用内置 DSH：任务够复杂时交给内核处理一轮（普通聊天仍走原生链路）",
            variable=self.enabled_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        self.prewarm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box, text="启动时预热内核（约 170 MB 内存；关掉可以省内存）", variable=self.prewarm_var
        ).grid(row=1, column=0, columnspan=4, sticky="w")
        self.affinity_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box, text="同一聊天复用 DSH 会话（只发增量，省 token）", variable=self.affinity_var
        ).grid(row=2, column=0, columnspan=4, sticky="w")
        self.min_score_var = tk.StringVar()
        self.concurrency_var = tk.StringVar()
        self.idle_var = tk.StringVar()
        self.timeout_var = tk.StringVar()
        self._entry(box, 3, 0, "路由阈值 min_score", self.min_score_var, width=10)
        ttk.Label(
            box,
            text="0~1，默认 0.40；低于阈值的消息不交给 DSH（「在吗」这类闲聊就是 0 分）",
            foreground="#8a8a8a",
        ).grid(row=3, column=2, columnspan=2, sticky="w", padx=(6, 0))
        self._entry(box, 4, 0, "并发上限", self.concurrency_var, width=10)
        self._entry(box, 4, 2, "空闲回收（秒）", self.idle_var, width=10)
        self._entry(box, 5, 0, "单轮超时（秒）", self.timeout_var, width=10)
        ttk.Label(
            box,
            text="并发上限 = 同一条内核连接上同时跑的 prompt 数；空闲回收 0 = 不回收",
            foreground="#8a8a8a",
        ).grid(row=5, column=2, columnspan=2, sticky="w", padx=(6, 0))

    def _build_credential(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="凭据（内核用哪个 Provider 的密钥）", padding=8)
        box.pack(fill=tk.X, pady=(0, 8))
        self.provider_var = tk.StringVar()
        self.protocol_var = tk.StringVar()
        ttk.Label(box, text="Provider", width=12).grid(row=0, column=0, sticky="w")
        self.provider_box = widgets.dropdown(
            box,
            self.provider_var,
            width=26,
            height=12,
            on_open=self._refresh_provider_values,
        )
        self.provider_box.grid(row=0, column=1, sticky="w")
        self.provider_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_hint())
        ttk.Label(box, text="协议", width=8).grid(row=0, column=2, sticky="e")
        self.protocol_box = ttk.Combobox(
            box,
            textvariable=self.protocol_var,
            values=_PROTOCOL_LABELS,
            state="readonly",
            width=44,
        )
        self.protocol_box.grid(row=0, column=3, sticky="w")
        self.protocol_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_hint())
        self.hint_var = tk.StringVar()
        ttk.Label(
            box, textvariable=self.hint_var, foreground="#555555", wraplength=940, justify="left"
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(box)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="复制建议的 Anthropic 地址", command=self._copy_suggestion).pack(
            side=tk.LEFT
        )
        ttk.Label(
            buttons,
            text="  base_url 要改成同一网关的 Anthropic Messages 路径（例如 …/anthropic），"
            "改完回这里点「刷新状态」",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

    # -- 小工具 -------------------------------------------------------------
    def _entry(
        self, parent: Any, row: int, column: int, label: str, var: tk.StringVar, *, width: int = 12
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, width=16).grid(row=row, column=column, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=column + 1, sticky="w", pady=2)
        return entry

    def _selected_provider(self) -> str:
        text = (self.provider_var.get() or "").strip()
        if not text or text == dshconfig.AUTO_PROVIDER_LABEL:
            return ""
        return text

    def _selected_protocol(self) -> str:
        label = (self.protocol_var.get() or "").strip()
        for key, text in dshconfig.PROTOCOL_LABELS.items():
            if text == label:
                return key
        return "auto"

    def _set_status(self, text: str) -> None:
        if self._status_text is None:
            return
        self._status_text.configure(state=tk.NORMAL)
        self._status_text.delete("1.0", tk.END)
        self._status_text.insert("1.0", text)
        self._status_text.configure(state=tk.DISABLED)

    def _append_status(self, text: str) -> None:
        if self._status_text is None:
            return
        self._status_text.configure(state=tk.NORMAL)
        self._status_text.insert(tk.END, ("\n" if text else "") + text)
        self._status_text.see(tk.END)
        self._status_text.configure(state=tk.DISABLED)

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        """从配置文件重读表单（不联网、不起子进程）。"""
        if not self.is_ready():
            return
        paths = self.require_paths()
        try:
            self.settings = dshconfig.load_settings(paths)
            self._providers = dshconfig.available_providers(paths)
            self.catalog = dshcatalog.load_kernel_catalog(paths)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        settings = self.settings
        self.enabled_var.set(settings.enabled)
        self.prewarm_var.set(settings.prewarm)
        self.affinity_var.set(settings.session_affinity)
        self.min_score_var.set(f"{settings.min_score:g}")
        self.concurrency_var.set(str(settings.max_concurrent_prompts))
        self.idle_var.set(f"{settings.idle_timeout_seconds:g}")
        self.timeout_var.set(f"{settings.timeout_seconds:g}")
        self._fill_provider_box()
        self.protocol_var.set(dshconfig.PROTOCOL_LABELS[settings.credential_protocol])
        self._fill_catalog_widgets()
        self._refresh_hint()

    def _fill_provider_box(self) -> None:
        values = [dshconfig.AUTO_PROVIDER_LABEL] + list(self._providers)
        self.provider_box.configure(values=values)
        current = self.settings.credential_provider or dshconfig.AUTO_PROVIDER_LABEL
        self.provider_var.set(current if current in values else dshconfig.AUTO_PROVIDER_LABEL)

    def _refresh_provider_values(self) -> None:
        """点开下拉前重读 provider 表（用户可能刚在「模型」页加/删过）。"""
        if not self.is_ready():
            return
        try:
            self._providers = dshconfig.available_providers(self.require_paths())
        except ConfigEditError:
            return
        self._fill_provider_box()

    def _refresh_hint(self, _event: Any = None) -> None:
        if not self.is_ready():
            return
        provider = self._selected_provider()
        parts = []
        try:
            parts.append(dshconfig.hint_for_provider(self.require_paths(), provider))
        except ConfigEditError as exc:
            parts.append(f"读 Provider 表失败：{exc}")
        protocol = self._selected_protocol()
        if protocol == "anthropic":
            parts.append("协议已强制 anthropic：只在该网关确实支持 Messages 协议时才对")
        elif protocol == "openai":
            parts.append("协议=openai：内核只说 Messages 协议，深度路由不会生效")
        elif protocol == "off":
            parts.append("协议=off：深度路由关闭，所有消息都走原生链路")
        model_id = (self.model_var.get() or "").strip()
        if model_id and model_id not in self.catalog.ids():
            parts.append(f"默认模型 {model_id} 不在内核目录里：内核会拒绝该覆盖并退回默认模型")
        self.hint_var.set("；".join(part for part in parts if part))

    # -- 保存 ---------------------------------------------------------------
    def _read_form(self) -> dshconfig.DshSettings:
        """把界面控件读成一份设置对象（含校验）。"""

        def number(var: tk.StringVar, label: str, cast: Any = float) -> Any:
            text = (var.get() or "").strip()
            try:
                return cast(float(text)) if cast is int else float(text)
            except ValueError as exc:
                raise ConfigEditError(f"{label} 需要是数字（当前 {text!r}）") from exc

        model_id = (self.model_var.get() or "").strip()
        model = (
            dshconfig.make_model_value(dshconfig.DEFAULT_KERNEL_PROVIDER, model_id)
            if model_id
            else ""
        )
        settings = dshconfig.DshSettings(
            enabled=bool(self.enabled_var.get()),
            credential_provider=self._selected_provider(),
            credential_protocol=self._selected_protocol(),
            model=model,
            min_score=number(self.min_score_var, "路由阈值"),
            prewarm=bool(self.prewarm_var.get()),
            idle_timeout_seconds=number(self.idle_var, "空闲回收"),
            timeout_seconds=number(self.timeout_var, "单轮超时"),
            session_affinity=bool(self.affinity_var.get()),
            max_concurrent_prompts=number(self.concurrency_var, "并发上限", int),
        )
        return dshconfig.validate_settings(settings)

    def _save(self) -> bool:
        """保存 ``[agent.dsh]``；返回是否成功（失败已在状态行/弹窗说明）。"""
        if not self.is_ready():
            self.fail("找不到 app.toml，无法保存")
            return False
        try:
            settings = self._read_form()
            changed = dshconfig.save_settings(self.require_paths(), settings)
        except ConfigEditError as exc:
            self.fail(exc)
            return False
        self.settings = settings
        self.journal(
            "保存内置 DSH：enabled=%s provider=%r protocol=%s model=%r"
            % (
                settings.enabled,
                settings.credential_provider,
                settings.credential_protocol,
                settings.model_id(),
            )
        )
        if changed:
            note = "已保存 [agent.dsh]；重启后台服务后生效"
        else:
            note = "配置没有变化"
        self.notify(note)
        self._refresh_hint()
        self._refresh_status(note=note)
        return True

    def _save_and_restart(self) -> None:
        if self._save() and self.on_restart is not None:
            self.on_restart()
