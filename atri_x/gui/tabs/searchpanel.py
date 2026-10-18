"""「模型 → 搜索服务」子页：``web_search`` 的通道列表、表单、顺序与密钥。

写 ``services.toml`` 的 ``[web_search]``：``engine_order`` 决定尝试顺序，
``[web_search.engines.<name>]`` 存协议 / 密钥环境变量名 / 服务地址 / 可执行文件；
密钥值写配置根的 ``.env``（与 Provider 一致，界面只显示「已设置/未设置 + 掩码」）。

宿主是 :class:`atri_x.gui.tabs.model.ModelTab`：本面板只负责界面与数据，配置路径、
状态行、弹窗和界面日志都通过宿主提供（``tab.require_paths()`` / ``tab.notify`` /
``tab.fail`` / ``tab.journal`` / ``tab.frame``），不复制第二套。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import searchconfig, widgets
from atri_x.gui.configedit import ConfigEditError, pop_env_value, set_env_value
from atri_x.gui.tabs.base import confirm, message
from atri_x.tool.builtin.web_search_engines import (
    BUILTIN_PROTOCOLS,
    CLI_PROTOCOLS,
    DEFAULT_KEY_ENV,
    KEYED_PROTOCOLS,
    KEYLESS_PROTOCOLS,
    PROTOCOL_SEARXNG,
)

__all__ = ["SearchServicePanel"]


def _protocol_label(protocol: str) -> str:
    """协议名 → 界面显示名。"""
    return searchconfig.PROTOCOL_LABELS.get(protocol, protocol)


def _protocol_from_label(label: str) -> str:
    """界面显示名 → 协议名（手打了协议名本身也认）。"""
    text = (label or "").strip()
    for protocol, name in searchconfig.PROTOCOL_LABELS.items():
        if text == name:
            return protocol
    return text if text in BUILTIN_PROTOCOLS else ""


class SearchServicePanel:
    """搜索服务子页：列表（顺序即降级顺序）+ 单条通道表单。"""

    def __init__(self, parent: Any, tab: Any) -> None:
        self.tab = tab
        self.engines: list[searchconfig.SearchEngineView] = []
        self.original = ""
        self._build(parent)

    # -- 构建 ---------------------------------------------------------------
    def _build(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="搜索通道（services.toml 的 [web_search]）", padding=8)
        box.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(box)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.tree = ttk.Treeview(
            left,
            columns=("order", "protocol", "state"),
            show="headings",
            height=8,
            selectmode="browse",
        )
        self.tree.heading("order", text="名称（自上而下尝试）")
        self.tree.heading("protocol", text="协议")
        self.tree.heading("state", text="状态")
        self.tree.column("order", width=160, stretch=False)
        self.tree.column("protocol", width=220)
        self.tree.column("state", width=90, stretch=False)
        scrollbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._row_selected)

        buttons = ttk.Frame(box)
        buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 10))
        for text, command in (
            ("新增自定义", self._new_engine),
            ("删除", self._delete_engine),
            ("上移", lambda: self._move_engine(-1)),
            ("下移", lambda: self._move_engine(1)),
            ("重新载入", self.reload),
        ):
            ttk.Button(buttons, text=text, width=10, command=command).pack(fill=tk.X, pady=(0, 4))

        form = ttk.Frame(box)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        form.columnconfigure(1, weight=1)
        self.name_var = tk.StringVar()
        self.protocol_var = tk.StringVar()
        self.key_env_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.bin_var = tk.StringVar()
        self.key_value_var = tk.StringVar()

        self.name_entry = self._row(form, 0, "通道名", self.name_var)
        protocol_row = ttk.Frame(form)
        protocol_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(protocol_row, text="协议", width=20).pack(side=tk.LEFT)
        self.protocol_box = ttk.Combobox(
            protocol_row,
            textvariable=self.protocol_var,
            values=[_protocol_label(name) for name in BUILTIN_PROTOCOLS],
            state="readonly",
            width=30,
        )
        self.protocol_box.pack(side=tk.LEFT)
        self.protocol_box.bind("<<ComboboxSelected>>", self._protocol_changed)

        secret_row = ttk.Frame(form)
        secret_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(secret_row, text="API Key", width=20).pack(side=tk.LEFT)
        self.key_entry = widgets.SecretEntry(secret_row, self.key_value_var, width=30)
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(secret_row, text="写入 .env", command=self._save_key).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(secret_row, text="清除", command=self._clear_key).pack(side=tk.LEFT, padx=(4, 0))

        self._row(form, 3, "密钥环境变量名", self.key_env_var)
        self.key_state_label = ttk.Label(form, text="", foreground="#555555")
        self.key_state_label.grid(row=4, column=1, sticky="w", pady=(0, 2))
        self._row(form, 5, "服务地址（可选）", self.base_url_var)
        self.base_hint_label = ttk.Label(form, text="", foreground="#8a8a8a")
        self.base_hint_label.grid(row=6, column=1, sticky="w")
        self._row(form, 7, "可执行文件（CLI）", self.bin_var)
        self.hint_label = ttk.Label(form, text="", foreground="#555555", wraplength=420)
        self.hint_label.grid(row=8, column=0, columnspan=2, sticky="w", pady=(4, 0))

        save_row = ttk.Frame(form)
        save_row.grid(row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(save_row, text="保存通道", command=self._save_engine).pack(side=tk.LEFT)
        ttk.Button(save_row, text="恢复默认链", command=self._reset_engines).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(
            save_row,
            text="  写 services.toml + .env；重启后台服务后生效",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)
        ttk.Label(
            form,
            text=(
                "按列表顺序逐条尝试，失败自动换下一条。免注册通道（Firecrawl / Keenable）"
                "不填 key 也能用；Tavily / Exa 需要 key；SearXNG 需要自建实例地址；"
                "agy / grok 需要本机装好并登录对应 CLI。"
            ),
            foreground="#8a8a8a",
            wraplength=520,
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        return entry

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        """从 ``services.toml`` 重新载入通道列表。"""
        if not self.tab.is_ready():
            return
        try:
            self.engines = searchconfig.load_engines(self.tab.require_paths())
        except ConfigEditError as exc:
            self.tab.fail(exc)
            self.engines = searchconfig.default_engines()
        self._refresh_tree()
        preferred = self.original
        if not preferred or not any(view.name == preferred for view in self.engines):
            preferred = self.engines[0].name if self.engines else ""
        if preferred:
            self._select_row(preferred)

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for index, view in enumerate(self.engines, start=1):
            self.tree.insert(
                "",
                tk.END,
                iid=view.name,
                values=(f"{index}. {view.name}", view.display(), self._state_text(view)),
            )

    def _state_text(self, view: searchconfig.SearchEngineView) -> str:
        if not self.tab.is_ready():
            return ""
        try:
            state = searchconfig.engine_key_state(self.tab.require_paths(), view)
        except ConfigEditError:
            return ""
        if state.configured:
            return "已设置"
        if view.protocol in KEYED_PROTOCOLS:
            return "缺密钥"
        if view.protocol in KEYLESS_PROTOCOLS:
            return "免注册"
        if view.protocol == PROTOCOL_SEARXNG:
            return "待填地址" if not view.base_url else "就绪"
        if view.protocol in CLI_PROTOCOLS:
            return "外部 CLI"
        return ""

    def _find(self, name: str) -> searchconfig.SearchEngineView | None:
        for view in self.engines:
            if view.name == name:
                return view
        return None

    def _select_row(self, name: str) -> None:
        if name in self.tree.get_children(""):
            self.tree.selection_set(name)
            self.tree.see(name)
            view = self._find(name)
            if view is not None:
                self._load_form(view)

    def _row_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        view = self._find(str(selection[0]))
        if view is not None:
            self._load_form(view)

    def _load_form(self, view: searchconfig.SearchEngineView) -> None:
        self.original = view.name
        self.name_entry.configure(state=tk.NORMAL)
        self.name_var.set(view.name)
        self.protocol_var.set(_protocol_label(view.protocol))
        self.key_env_var.set(view.api_key_env)
        self.base_url_var.set(view.base_url)
        self.bin_var.set(view.binary)
        self.key_entry.clear()
        self._refresh_key_state()
        self._refresh_hint()

    def _current_view(self) -> searchconfig.SearchEngineView:
        return searchconfig.SearchEngineView(
            name=self.name_var.get().strip() or self.original,
            protocol=_protocol_from_label(self.protocol_var.get()),
            api_key_env=self.key_env_var.get().strip(),
            base_url=self.base_url_var.get().strip(),
            binary=self.bin_var.get().strip(),
        )

    def _refresh_key_state(self) -> None:
        if not self.tab.is_ready():
            return
        view = self._current_view()
        if not view.api_key_env:
            self.key_state_label.configure(text="该通道不需要密钥（留空即可）")
            return
        try:
            state = searchconfig.engine_key_state(self.tab.require_paths(), view)
        except ConfigEditError:
            self.key_state_label.configure(text="")
            return
        self.key_state_label.configure(text=state.describe())

    def _refresh_hint(self) -> None:
        view = self._current_view()
        self.hint_label.configure(text=searchconfig.PROTOCOL_HINTS.get(view.protocol, ""))
        self.base_hint_label.configure(text=view.base_placeholder())

    def _protocol_changed(self, _event: Any = None) -> None:
        view = self._current_view()
        default_env = DEFAULT_KEY_ENV.get(view.protocol, "")
        if default_env and not self.key_env_var.get().strip():
            self.key_env_var.set(default_env)
        self._refresh_key_state()
        self._refresh_hint()

    # -- 动作 ---------------------------------------------------------------
    def _new_engine(self) -> None:
        existing = {view.name for view in self.engines}
        index = 1
        while f"custom-{index}" in existing:
            index += 1
        view = searchconfig.SearchEngineView(
            name=f"custom-{index}", protocol=PROTOCOL_SEARXNG, api_key_env="", base_url=""
        )
        self.engines.append(view)
        self._refresh_tree()
        self._select_row(view.name)
        self.tab.notify("新通道默认 SearXNG（自建实例）；协议也可以换成任意其他通道再填地址/密钥")

    def _delete_engine(self) -> None:
        name = self.original or self.name_var.get().strip()
        if not name:
            message(self.tab.frame, "删除通道", "先选中一条通道。")
            return
        if len(self.engines) <= 1:
            message(self.tab.frame, "删除通道", "至少要保留一条搜索通道。")
            return
        if not confirm(
            self.tab.frame, "删除通道", f"删除搜索通道「{name}」？\n\n这个操作只改 services.toml。"
        ):
            return
        remaining = [view for view in self.engines if view.name != name]
        try:
            searchconfig.save_engines(self.tab.require_paths(), remaining)
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self.original = ""
        self.reload()
        self.tab.journal(f"删除搜索通道：{name}")
        self.tab.notify(f"已删除搜索通道 {name}（重启后台服务后生效）")

    def _move_engine(self, delta: int) -> None:
        name = self.original or self.name_var.get().strip()
        names = [view.name for view in self.engines]
        if name not in names:
            message(self.tab.frame, "调整顺序", "先选中一条通道。")
            return
        index = names.index(name)
        target = index + delta
        if target < 0 or target >= len(names):
            return
        self.engines[index], self.engines[target] = self.engines[target], self.engines[index]
        try:
            searchconfig.save_engines(self.tab.require_paths(), self.engines)
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self.original = name
        self._refresh_tree()
        self._select_row(name)
        self.tab.journal(f"调整搜索通道顺序：{name} → 第 {target + 1} 位")
        self.tab.notify(f"已把 {name} 移到第 {target + 1} 位（重启后台服务后生效）")

    def _save_engine(self) -> None:
        secret = self.key_entry.get()
        try:
            paths = self.tab.require_paths()
            view = self._current_view()
            name = searchconfig.validate_engine_name(view.name)
            if not view.protocol:
                raise ConfigEditError("先选一个搜索协议")
            if view.protocol == PROTOCOL_SEARXNG and not view.base_url:
                raise ConfigEditError(
                    "SearXNG 通道要填服务地址（自建实例根地址，例如 https://searx.example.com）"
                )
            updated: list[searchconfig.SearchEngineView] = []
            replaced = False
            for existing in self.engines:
                if existing.name == self.original:
                    updated.append(view)
                    replaced = True
                else:
                    updated.append(existing)
            if not replaced:
                updated.append(view)
            searchconfig.save_engines(paths, updated)
            key_env = view.api_key_env or DEFAULT_KEY_ENV.get(view.protocol, "")
            if secret:
                if not key_env:
                    raise ConfigEditError("该通道没有密钥环境变量名，不能写入 .env")
                set_env_value(paths.env_path, key_env, secret)
                self.key_entry.clear()
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self.original = view.name
        self.reload()
        self.tab.journal(f"保存搜索通道：{view.name}（protocol={view.protocol}）")
        extra = "，密钥已写入 .env" if secret else ""
        self.tab.notify(f"已保存搜索通道 {view.name}{extra}；重启后台服务后生效")

    def _save_key(self) -> None:
        try:
            paths = self.tab.require_paths()
            name = self.key_env_var.get().strip()
            if not name:
                raise ConfigEditError("先填「密钥环境变量名」（例如 TAVILY_API_KEY）")
            secret = self.key_entry.get()
            if not secret:
                raise ConfigEditError("先在 API Key 输入框里填密钥")
            set_env_value(paths.env_path, name, secret)
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self.key_entry.clear()
        self._refresh_key_state()
        self.tab.journal(f"写入搜索密钥：.env 的 {name}")
        self.tab.notify(f"已写入 .env 的 {name}；重启后台服务后生效")

    def _clear_key(self) -> None:
        try:
            paths = self.tab.require_paths()
            name = self.key_env_var.get().strip()
            if not name:
                raise ConfigEditError("先填「密钥环境变量名」")
            if not confirm(self.tab.frame, "清除密钥", f"从 .env 里删掉 {name}？"):
                return
            removed = pop_env_value(paths.env_path, name)
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self._refresh_key_state()
        self.tab.journal(f"清除搜索密钥：.env 的 {name}（{'已删除' if removed else '本来就没有'}）")
        self.tab.notify(f"已从 .env 删除 {name}" if removed else f".env 里本来就没有 {name}")

    def _reset_engines(self) -> None:
        if not confirm(
            self.tab.frame,
            "恢复默认链",
            "把搜索通道恢复成默认的 Tavily → Firecrawl → Keenable → Exa？",
        ):
            return
        try:
            searchconfig.save_engines(self.tab.require_paths(), searchconfig.default_engines())
        except ConfigEditError as exc:
            self.tab.fail(exc)
            return
        self.original = ""
        self.reload()
        self.tab.journal("恢复默认搜索通道链")
        self.tab.notify("已恢复默认搜索通道链（重启后台服务后生效）")
