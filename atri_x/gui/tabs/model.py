"""「模型」选项卡：LLM Provider（含 API Key）、模型分配、搜索服务与抓取服务。

这是桌面端最常用的配置页，拆成三块（二级页签）：

1. **LLM Provider**（写 ``services.toml`` 的 ``[providers.<name>]``）：
   ``base_url`` / ``api_key_env`` / ``models`` / ``api_mode`` / ``extra_headers``
   （网关要求的自定义请求头，例如 OpenCode Go 的 ``x-opencode-session``）/
   ``reasoning_effort``（推理思考强度：none / minimal / low / medium / high，
   chat 协议写进请求 JSON 的顶层 ``reasoning_effort``，response 协议自动折成
   Responses 原生的 ``reasoning.effort``；留空 = 不发送，只对支持该参数的
   模型生效；也可在 ``model_configs`` 里按模型覆盖）；
   密钥值单独写进配置根 ``.env``（界面只显示"已设置/未设置 + 掩码"，不回显明文；
   输入框默认掩码，点「显示」才明文）；
   另有**模型分配**（写 ``state.toml``）：工作/聊天/命名/压缩四个槽位 + 默认模式。
   ``state.toml`` 是运行期回写的文件，后台服务会热加载，保存后几秒生效，不需要重启。
2. **搜索服务**（写 ``services.toml`` 的 ``[web_search]``）：``web_search`` 工具按列表
   自上而下逐条尝试通道——DeepSeek 官方搜索 / Tavily / Firecrawl / Keenable / Exa /
   SearXNG / Antigravity CLI / Grok Build，外加任意「自定义」通道（选一个协议 + 服务地址
   + 密钥）。免注册通道不填 key 也能用；密钥值和 provider 一样写进 ``.env``，这里只存
   环境变量名。
3. **抓取服务**（写 ``services.toml`` 的 ``[web_extract]``）：``web_extract`` 工具按列表
   逐条尝试通道——DeepSeek 官方抓取 / Jina Reader / 直接抓取，失败自动降级；默认链是
   Jina（有 key 才参与）→ 直接抓取。

模型名不用手打：点「获取可用模型」会 ``GET {base_url}/models`` 把上游模型列出来，之后
在下拉里点选；每个模型还能起一个**本地备注名**（``gui/model_catalog.json``，不影响上游
模型名），下拉里显示成 ``备注（模型）``，保存时再还原成真模型名。

Provider、``.env``、搜索服务与抓取服务都属于启动期配置，改完要点底部的「重启后台服务」。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.config.inspect import ConfigDecodeError
from atri_x.config.services import normalize_reasoning_effort, validate_provider_extra_headers
from atri_x.gui import modelcatalog, widgets
from atri_x.gui.configedit import (
    ConfigEditError,
    pop_env_value,
    read_toml,
    set_env_value,
)
from atri_x.gui.configmodel import (
    MODEL_SLOTS,
    ProviderView,
    delete_provider,
    load_model_selection,
    load_providers,
    provider_key_state,
    resolve_fetch_key,
    save_model_selection,
    save_provider,
)
from atri_x.gui.tabs.base import ConfigTab, confirm, message
from atri_x.gui.tabs.extractpanel import ExtractServicePanel
from atri_x.gui.tabs.searchpanel import SearchServicePanel

__all__ = ["ModelTab"]

#: API 协议下拉项（空串 = 沿用 Provider 默认，即 chat）。
_API_MODES = ("", "chat", "response")

#: 默认模式下拉项。
_DEFAULT_MODES = ("work", "chat")

#: 推理思考强度下拉项：``(显示文案, 写进 services.toml 的值)``；空串 = 不发送。
_REASONING_EFFORT_CHOICES: tuple[tuple[str, str], ...] = (
    ("（不设置）", ""),
    ("none（关闭思考）", "none"),
    ("minimal", "minimal"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
)

#: 模型下拉一次展开多少行（超过滚动）。
_DROPDOWN_HEIGHT = 14


def _reasoning_effort_label(value: str) -> str:
    """配置值 → 下拉显示文案（未识别的值原样显示，不静默吞掉）。"""
    normalized, _ok = normalize_reasoning_effort(value)
    for label, raw in _REASONING_EFFORT_CHOICES:
        if raw == normalized:
            return label
    return (value or "").strip()


def _reasoning_effort_value(label: str) -> str:
    """下拉显示文案 → 要写进配置的值（手打的非标准值原样返回，由保存期校验拒绝）。"""
    text = (label or "").strip()
    for display, raw in _REASONING_EFFORT_CHOICES:
        if display == text:
            return raw
    return text


class ModelTab(ConfigTab):
    """Provider 列表 + 表单 + 模型分配 + 搜索服务 + 抓取服务。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._providers: dict[str, ProviderView] = {}
        self._catalog = modelcatalog.Catalog()
        self._original = ""
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.body)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        llm_page = ttk.Frame(self.notebook)
        search_page = ttk.Frame(self.notebook)
        extract_page = ttk.Frame(self.notebook)
        self.notebook.add(llm_page, text="LLM Provider / 模型分配")
        self.notebook.add(search_page, text="搜索服务（web_search）")
        self.notebook.add(extract_page, text="抓取服务（web_extract）")

        provider_box = ttk.LabelFrame(llm_page, text="LLM Provider（services.toml）", padding=8)
        provider_box.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(provider_box)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.tree = ttk.Treeview(
            left, columns=("name", "url"), show="headings", height=8, selectmode="browse"
        )
        self.tree.heading("name", text="名称")
        self.tree.heading("url", text="Base URL")
        self.tree.column("name", width=110, stretch=False)
        self.tree.column("url", width=190)
        scrollbar = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._provider_selected)

        left_buttons = ttk.Frame(provider_box)
        left_buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        ttk.Button(left_buttons, text="新增", width=8, command=self._new_provider).pack(
            fill=tk.X, pady=(0, 4)
        )
        ttk.Button(left_buttons, text="删除", width=8, command=self._delete_provider).pack(
            fill=tk.X, pady=(0, 4)
        )
        ttk.Button(left_buttons, text="重新载入", width=8, command=self.reload).pack(fill=tk.X)

        form = ttk.Frame(provider_box)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        form.columnconfigure(1, weight=1)
        self.name_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.key_env_var = tk.StringVar()
        self.models_var = tk.StringVar()
        self.api_mode_var = tk.StringVar()
        self.reasoning_var = tk.StringVar()
        self.key_value_var = tk.StringVar()
        self.picker_var = tk.StringVar()
        self.alias_model_var = tk.StringVar()
        self.alias_value_var = tk.StringVar()

        self.name_entry = self._row(form, 0, "名称", self.name_var)
        self._row(form, 1, "Base URL", self.base_url_var)
        self._row(form, 2, "密钥环境变量名", self.key_env_var)

        secret_row = ttk.Frame(form)
        secret_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(secret_row, text="API Key", width=20).pack(side=tk.LEFT)
        self.key_entry = widgets.SecretEntry(secret_row, self.key_value_var, width=34)
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(secret_row, text="写入 .env", command=self._save_key_only).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(secret_row, text="清除", command=self._clear_key).pack(side=tk.LEFT, padx=(4, 0))
        self.key_state_label = ttk.Label(form, text="", foreground="#555555")
        self.key_state_label.grid(row=4, column=1, sticky="w", pady=(0, 2))

        self._row(form, 5, "已启用模型（逗号分隔）", self.models_var)
        picker_row = ttk.Frame(form)
        picker_row.grid(row=6, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(picker_row, text="可用模型（点箭头选择）", width=20).pack(side=tk.LEFT)
        self.picker_box = widgets.dropdown(
            picker_row,
            self.picker_var,
            width=34,
            height=_DROPDOWN_HEIGHT,
            on_open=self._refresh_catalog_widgets,
        )
        self.picker_box.pack(side=tk.LEFT)
        ttk.Button(picker_row, text="获取可用模型", command=self._fetch_models).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(picker_row, text="加入列表", command=self._append_selected_model).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        mode_row = ttk.Frame(form)
        mode_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(mode_row, text="API 协议", width=20).pack(side=tk.LEFT)
        ttk.Combobox(
            mode_row,
            textvariable=self.api_mode_var,
            values=list(_API_MODES),
            state="readonly",
            width=12,
        ).pack(side=tk.LEFT)
        ttk.Label(
            mode_row,
            text="  留空 = chat（/chat/completions）；response = /responses",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

        effort_row = ttk.Frame(form)
        effort_row.grid(row=8, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(effort_row, text="推理思考强度", width=20).pack(side=tk.LEFT)
        self.reasoning_box = widgets.dropdown(
            effort_row,
            self.reasoning_var,
            values=[label for label, _raw in _REASONING_EFFORT_CHOICES],
            width=16,
            height=len(_REASONING_EFFORT_CHOICES),
            state="readonly",
        )
        self.reasoning_box.pack(side=tk.LEFT)
        ttk.Label(
            effort_row,
            text="  留空 = 不发送；chat → reasoning_effort，response → reasoning.effort；只对支持该参数的模型/网关生效",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

        headers_box = ttk.LabelFrame(
            form, text="请求头（可选，写 [providers.<名称>.extra_headers]）", padding=6
        )
        headers_box.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.headers_text = tk.Text(
            headers_box, height=3, width=40, wrap="none", font=("Consolas", 9)
        )
        self.headers_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            headers_box,
            text="每行一条 Name: value。网关强制要求时填，\n"
            "例如 OpenCode Go 必须带 x-opencode-session；\n"
            "Authorization / Content-Type 等保留头不能覆盖。",
            foreground="#8a8a8a",
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(8, 0))

        alias_box = ttk.LabelFrame(form, text="模型备注名（本地，不写回 services.toml）", padding=6)
        alias_box.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(alias_box, text="模型").pack(side=tk.LEFT)
        self.alias_model_box = widgets.dropdown(
            alias_box,
            self.alias_model_var,
            width=30,
            height=_DROPDOWN_HEIGHT,
            on_open=self._refresh_catalog_widgets,
        )
        self.alias_model_box.pack(side=tk.LEFT, padx=(4, 6))
        self.alias_model_box.bind("<<ComboboxSelected>>", self._alias_model_picked)
        ttk.Label(alias_box, text="备注名").pack(side=tk.LEFT)
        ttk.Entry(alias_box, textvariable=self.alias_value_var, width=16).pack(
            side=tk.LEFT, padx=(4, 6)
        )
        ttk.Button(alias_box, text="保存备注", command=self._save_alias).pack(side=tk.LEFT)
        ttk.Button(alias_box, text="清除备注", command=self._clear_alias).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            form,
            text="备注名只用于下拉里显示（形如「主力（deepseek-chat）」），保存时自动还原真模型名。",
            foreground="#8a8a8a",
        ).grid(row=11, column=0, columnspan=2, sticky="w")

        save_row = ttk.Frame(form)
        save_row.grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(save_row, text="保存 Provider", command=self._save_provider).pack(side=tk.LEFT)
        ttk.Label(
            save_row,
            text="  写 services.toml + .env；对已有对话生效需重启后台服务",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

        slot_box = ttk.LabelFrame(
            llm_page, text="模型分配（state.toml，保存后几秒内热加载）", padding=8
        )
        slot_box.pack(fill=tk.X, pady=(8, 0))
        slot_box.columnconfigure(1, weight=1)
        slot_box.columnconfigure(2, weight=1)
        default_row = ttk.Frame(slot_box)
        default_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        ttk.Label(default_row, text="默认会话模式", width=16).pack(side=tk.LEFT)
        self.default_mode_var = tk.StringVar()
        ttk.Combobox(
            default_row,
            textvariable=self.default_mode_var,
            values=list(_DEFAULT_MODES),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT)
        ttk.Label(
            default_row,
            text="  新会话用哪个模式（work / chat 都支持工具，区别在模型槽位）",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

        self.slot_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        self.slot_provider_boxes: list[ttk.Combobox] = []
        self.slot_model_boxes: dict[str, ttk.Combobox] = {}
        for offset, slot in enumerate(MODEL_SLOTS, start=1):
            ttk.Label(slot_box, text=slot.label, width=24).grid(
                row=offset, column=0, sticky="w", pady=1
            )
            provider_var = tk.StringVar()
            model_var = tk.StringVar()
            provider_box = widgets.dropdown(
                slot_box,
                provider_var,
                width=16,
                height=_DROPDOWN_HEIGHT,
                state="readonly",
                on_open=self._refresh_slot_providers,
            )
            provider_box.grid(row=offset, column=1, sticky="w", padx=(0, 6))
            model_box = widgets.dropdown(
                slot_box,
                model_var,
                width=30,
                height=_DROPDOWN_HEIGHT,
                on_open=lambda key=slot.key: self._fill_model_suggestions(key),
            )
            model_box.grid(row=offset, column=2, sticky="ew")
            provider_box.bind(
                "<<ComboboxSelected>>",
                lambda _event, key=slot.key: self._fill_model_suggestions(key),
            )
            self.slot_vars[slot.key] = (provider_var, model_var)
            self.slot_provider_boxes.append(provider_box)
            self.slot_model_boxes[slot.key] = model_box
        slot_actions = ttk.Frame(slot_box)
        slot_actions.grid(row=len(MODEL_SLOTS) + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(slot_actions, text="保存模型分配", command=self._save_slots).pack(side=tk.LEFT)
        ttk.Button(slot_actions, text="刷新模型列表", command=self._refresh_slots_from_disk).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(slot_actions, text="重新载入", command=self.reload).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            slot_actions,
            text="  下拉里可以点箭头翻；模型名也允许手打",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

        self.search_panel = SearchServicePanel(search_page, self)
        self.extract_panel = ExtractServicePanel(extract_page, self)

    def _row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        return entry

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        """从配置文件重新载入 provider 列表、密钥状态和模型分配。"""
        if not self.is_ready():
            return
        paths = self.require_paths()
        try:
            self._providers = load_providers(paths)
        except ConfigEditError as exc:
            self.fail(exc)
            self._providers = {}
        self._catalog = modelcatalog.load(paths.config_dir)
        self._refresh_tree()
        if self._original and self._original in self._providers:
            self._select_row(self._original)
        elif self._providers and not self._original:
            first = sorted(self._providers)[0]
            self._select_row(first)
        self._refresh_slot_providers()
        self._load_model_selection()
        self.search_panel.reload()
        self.extract_panel.reload()
        self.notify(
            f"已载入 {len(self._providers)} 个 provider、{len(self.search_panel.engines)} 条搜索通道、"
            f"{len(self.extract_panel.engines)} 条抓取通道"
        )

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for name in sorted(self._providers):
            view = self._providers[name]
            self.tree.insert("", tk.END, iid=name, values=(name, view.base_url))

    def _select_row(self, name: str) -> None:
        if name in self.tree.get_children(""):
            self.tree.selection_set(name)
            self.tree.see(name)
            self._load_form(self._providers[name])

    def _provider_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        view = self._providers.get(str(selection[0]))
        if view is not None:
            self._load_form(view)

    def _load_form(self, view: ProviderView) -> None:
        self._original = view.name
        self.name_var.set(view.name)
        self.name_entry.configure(state=tk.DISABLED)
        self.base_url_var.set(view.base_url)
        self.key_env_var.set(view.api_key_env)
        self.models_var.set(", ".join(view.models))
        self.api_mode_var.set(view.api_mode if view.api_mode in _API_MODES else "")
        self.reasoning_var.set(_reasoning_effort_label(view.reasoning_effort))
        self._set_headers(view.extra_headers)
        self.key_entry.clear()
        self.picker_var.set("")
        self.alias_model_var.set("")
        self.alias_value_var.set("")
        self._refresh_key_state()
        self._refresh_catalog_widgets()

    def _set_headers(self, headers: dict[str, str]) -> None:
        """把请求头字典铺进文本框（每行 ``Name: value``）。"""
        self.headers_text.delete("1.0", tk.END)
        lines = [f"{name}: {value}" if value else f"{name}:" for name, value in headers.items()]
        if lines:
            self.headers_text.insert("1.0", "\n".join(lines))

    def _refresh_key_state(self) -> None:
        if not self.is_ready():
            return
        name = self.key_env_var.get().strip()
        if not name:
            self.key_state_label.configure(text="未配置 api_key_env")
            return
        try:
            state = provider_key_state(
                self.require_paths(),
                ProviderView(name=self.name_var.get().strip(), api_key_env=name),
            )
        except ConfigEditError as exc:
            self.key_state_label.configure(text=f"读取密钥状态失败：{exc}")
            return
        self.key_state_label.configure(text=state.describe())

    # -- 模型目录 -----------------------------------------------------------
    def _catalog_key(self, name: str = "") -> str:
        return modelcatalog.provider_key(name or self.name_var.get().strip() or self._original)

    def _configured_models(self) -> list[str]:
        return _split_models(self.models_var.get())

    def _refresh_catalog_widgets(self) -> None:
        """刷新「可用模型」与「模型备注」两个下拉的候选（点箭头时也会调一次）。"""
        if not self.is_ready():
            return
        labels = self._catalog.labels(self._catalog_key(), self._configured_models())
        try:
            self.picker_box.configure(values=labels)
            self.alias_model_box.configure(values=labels)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    def _fetch_models(self) -> None:
        """后台拉取 ``{base_url}/models`` 并落进本地模型目录。"""
        if not self.is_ready():
            return
        label = self.name_var.get().strip() or self._original
        if not label:
            self.fail("先填 Provider 名称（模型目录要按 Provider 分开存）")
            return
        base_url = self.base_url_var.get().strip()
        if not base_url:
            self.fail("先填 Base URL（例如 https://api.deepseek.com）")
            return
        env_name = self.key_env_var.get().strip()
        api_key, key_source = resolve_fetch_key(self.key_entry.get(), self.require_paths(), env_name)
        self.notify(f"正在获取 {label} 的可用模型…")
        self.journal(f"获取模型目录：{label} ← {base_url}（密钥：{key_source or '未配置'}）")
        widgets.run_async(
            self.frame,
            lambda: modelcatalog.fetch_models(base_url, api_key),
            self._models_fetched,
        )

    def _models_fetched(self, models: Any, error: BaseException | None) -> None:
        if error is not None:
            self.journal(f"获取模型目录失败：{error}")
            self.fail(f"获取模型目录失败：{error}")
            return
        found = [str(item) for item in (models or [])]
        if not found:
            self.notify("上游没有返回模型清单")
            return
        key = self._catalog_key()
        added = self._catalog.record(key, found, source=self.base_url_var.get().strip())
        try:
            self._catalog.save(self.require_paths().config_dir)
        except modelcatalog.CatalogError as exc:
            self.fail(exc)
            return
        self._refresh_catalog_widgets()
        if not self._configured_models():
            # 已启用模型还空着：把第一个模型填进去，省得用户再点一次。
            self.models_var.set(found[0])
            self._refresh_catalog_widgets()
        self.journal(f"获取模型目录成功：{key} 共 {len(found)} 个（新增 {len(added)}）")
        self.notify(
            f"已获取 {len(found)} 个模型（新增 {len(added)}）：在下拉里选一个点「加入列表」"
        )

    def _append_selected_model(self) -> None:
        model = self._picked_model(self.picker_var.get())
        if not model:
            self.notify("先点「获取可用模型」或直接在下方输入模型名")
            return
        models = self._configured_models()
        if model in models:
            self.notify(f"{model} 已经在列表里了")
            return
        models.append(model)
        self.models_var.set(", ".join(models))
        self.picker_var.set("")
        self._refresh_catalog_widgets()
        self.notify(f"已加入 {model}（点「保存 Provider」落盘）")

    def _picked_model(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        names = self._catalog.names(self._catalog_key(), self._configured_models())
        return modelcatalog.model_of_label(text, names)

    def _alias_model_picked(self, _event: Any = None) -> None:
        model = self._picked_model(self.alias_model_var.get())
        self.alias_value_var.set(self._catalog.alias_of(self._catalog_key(), model))

    def _save_alias(self) -> None:
        model = self._picked_model(self.alias_model_var.get())
        if not model:
            self.notify("先在「模型」下拉里选一个模型")
            return
        try:
            self._catalog.set_alias(self._catalog_key(), model, self.alias_value_var.get())
            self._catalog.save(self.require_paths().config_dir)
        except modelcatalog.CatalogError as exc:
            self.fail(exc)
            return
        alias = self._catalog.alias_of(self._catalog_key(), model)
        self._refresh_catalog_widgets()
        self.journal(f"设置模型备注名：{model} → {alias or '（清除）'}")
        self.notify(f"{model} 的备注名：{alias}" if alias else f"已清除 {model} 的备注名")

    def _clear_alias(self) -> None:
        self.alias_value_var.set("")
        self._save_alias()

    # -- 模型分配 -----------------------------------------------------------
    def _refresh_slot_providers(self) -> None:
        names = sorted(self._providers)
        for box in self.slot_provider_boxes:
            box.configure(values=names)

    def _fill_model_suggestions(self, slot_key: str) -> None:
        provider_var, _model_var = self.slot_vars[slot_key]
        provider = provider_var.get().strip()
        view = self._providers.get(provider)
        box = self.slot_model_boxes.get(slot_key)
        if box is None:
            return
        configured = list(view.models) if view is not None else []
        labels = self._catalog.labels(modelcatalog.provider_key(provider), configured)
        box.configure(values=labels)

    def _refresh_slots_from_disk(self) -> None:
        """重新读 ``services.toml`` 与模型目录，刷新模型分配的候选（不改已选的值）。"""
        try:
            self._providers = load_providers(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._catalog = modelcatalog.load(self.require_paths().config_dir)
        self._refresh_tree()
        self._refresh_slot_providers()
        for slot in MODEL_SLOTS:
            self._fill_model_suggestions(slot.key)
        self._refresh_catalog_widgets()
        self.journal("刷新模型分配候选（重读 services.toml + 模型目录）")
        self.notify("已刷新模型列表（重读 services.toml 与模型目录；无需重启）")

    def _load_model_selection(self) -> None:
        try:
            default_mode, values = load_model_selection(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.default_mode_var.set(default_mode or "work")
        for slot in MODEL_SLOTS:
            provider_var, model_var = self.slot_vars[slot.key]
            provider, model = values.get(slot.key, ("", ""))
            provider_var.set(provider)
            self._fill_model_suggestions(slot.key)
            model_var.set(self._model_label(provider, model))

    def _model_label(self, provider: str, model: str) -> str:
        """模型名 → 下拉里显示的标签（有备注名时带上）。"""
        model = (model or "").strip()
        if not model:
            return ""
        key = modelcatalog.provider_key(provider)
        return modelcatalog.display_label(model, self._catalog.alias_of(key, model))

    # -- 动作 ---------------------------------------------------------------
    def _new_provider(self) -> None:
        self._original = ""
        self.name_entry.configure(state=tk.NORMAL)
        self.name_var.set("")
        self.base_url_var.set("")
        self.key_env_var.set("")
        self.models_var.set("")
        self.api_mode_var.set("")
        self.reasoning_var.set("")
        self._set_headers({})
        self.picker_var.set("")
        self.alias_model_var.set("")
        self.alias_value_var.set("")
        self.key_entry.clear()
        self.tree.selection_remove(*self.tree.selection())
        self.name_entry.focus_set()
        self._refresh_catalog_widgets()
        self.notify("填好新 provider 后点「保存 Provider」")

    def _delete_provider(self) -> None:
        name = self._original or self.name_var.get().strip()
        if not name:
            message(self.frame, "删除 Provider", "先选中一个 provider。")
            return
        if not confirm(self.frame, "删除 Provider", f"删除 [providers.{name}]？\n\n这个操作只改配置文件，可回滚 .bak。"):
            return
        try:
            delete_provider(self.require_paths(), name)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal(f"删除 LLM Provider：{name}")
        self._original = ""
        self.reload()
        self.notify(f"已删除 provider {name}（重启后台服务后生效）")

    def _collect_form(self) -> ProviderView:
        name = self.name_var.get().strip() or self._original
        if not name:
            raise ConfigEditError("provider 名字不能为空")
        headers = _parse_headers(self.headers_text.get("1.0", tk.END))
        try:
            validate_provider_extra_headers(name, headers)
        except ConfigDecodeError as exc:
            raise ConfigEditError(str(exc)) from exc
        return ProviderView(
            name=name,
            base_url=self.base_url_var.get().strip(),
            api_key_env=self.key_env_var.get().strip(),
            models=_split_models(self.models_var.get()),
            api_mode=self.api_mode_var.get().strip(),
            reasoning_effort=_reasoning_effort_value(self.reasoning_var.get()),
            extra_headers=headers,
        )

    def _save_provider(self) -> None:
        try:
            paths = self.require_paths()
            view = self._collect_form()
            if not view.base_url:
                raise ConfigEditError("base_url 不能为空（例如 https://api.deepseek.com）")
            if not view.api_key_env:
                raise ConfigEditError("api_key_env 不能为空（例如 DEEPSEEK_API_KEY）")
            target = save_provider(paths, view, original=self._original)
            secret = self.key_entry.get()
            if secret:
                set_env_value(paths.env_path, view.api_key_env, secret)
                self.key_entry.clear()
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._original = view.name
        header_note = f"，请求头 {len(view.extra_headers)} 条" if view.extra_headers else ""
        effort_note = f"，推理强度 {view.reasoning_effort}" if view.reasoning_effort else ""
        self.journal(
            f"保存 LLM Provider：{view.name}"
            f"（models={','.join(view.models) or '-'}{header_note}{effort_note}）"
        )
        self.reload()
        self._select_row(view.name)
        where = target.replace(paths.config_dir + "\\", "").replace(paths.config_dir + "/", "")
        extra = "，密钥已写入 .env" if secret else ""
        self.notify(f"已保存并回读校验 {where}{extra}；重启后台服务后生效")

    def _save_key_only(self) -> None:
        try:
            paths = self.require_paths()
            name = self.key_env_var.get().strip()
            if not name:
                raise ConfigEditError("先填「密钥环境变量名」（例如 DEEPSEEK_API_KEY）")
            secret = self.key_entry.get()
            if not secret:
                raise ConfigEditError("先在 API Key 输入框里填密钥")
            set_env_value(paths.env_path, name, secret)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.key_entry.clear()
        self._refresh_key_state()
        self.journal(f"写入密钥：.env 的 {name}")
        self.notify(f"已写入 .env 的 {name}；重启后台服务后生效")

    def _clear_key(self) -> None:
        try:
            paths = self.require_paths()
            name = self.key_env_var.get().strip()
            if not name:
                raise ConfigEditError("先填「密钥环境变量名」")
            if not confirm(self.frame, "清除密钥", f"从 .env 里删掉 {name}？"):
                return
            removed = pop_env_value(paths.env_path, name)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._refresh_key_state()
        self.journal(f"清除密钥：.env 的 {name}（{'已删除' if removed else '本来就没有'}）")
        self.notify(f"已从 .env 删除 {name}" if removed else f".env 里本来就没有 {name}")

    def _save_slots(self) -> None:
        try:
            paths = self.require_paths()
            previous_mode = ""
            try:
                session_section = read_toml(paths.state_path).get("session")
                if isinstance(session_section, dict):
                    previous_mode = str(session_section.get("default_mode") or "").strip()
            except ConfigEditError:
                previous_mode = ""
            values: dict[str, tuple[str, str]] = {}
            for slot in MODEL_SLOTS:
                provider_var, model_var = self.slot_vars[slot.key]
                provider = provider_var.get().strip()
                model = self._picked_model_for_slot(slot.key, model_var.get())
                values[slot.key] = (provider, model)
            default_mode = self.default_mode_var.get().strip()
            # 校验（半填 / 必填槽位 / 非法值）与落盘后回读校验都在 configmodel 里，
            # 这里不再重复写一套，避免界面规则和文件规则各说各话。
            save_model_selection(paths, default_mode, values)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal(
            "保存模型分配："
            + "；".join(f"{slot.key}={values[slot.key][0]}/{values[slot.key][1]}" for slot in MODEL_SLOTS)
        )
        if default_mode and default_mode != previous_mode:
            # [session].default_mode 只在后台服务启动时读取，不在 state.toml 热加载分区里。
            self.notify(
                f"模型槽位几秒内热加载；默认会话模式已改为 {default_mode}，"
                "需重启后台服务后才对新会话生效"
            )
        else:
            self.notify("已保存并回读校验 state.toml，几秒内热加载生效（无需重启）")

    def _picked_model_for_slot(self, slot_key: str, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return ""
        provider_var, _model_var = self.slot_vars[slot_key]
        provider = provider_var.get().strip()
        view = self._providers.get(provider)
        configured = list(view.models) if view is not None else []
        names = self._catalog.names(modelcatalog.provider_key(provider), configured)
        return modelcatalog.model_of_label(text, names)


def _split_models(text: str) -> list[str]:
    """逗号/空白分隔的模型列表（中英文逗号都接受）。"""
    return [
        item
        for chunk in (text or "").replace("，", ",").split(",")
        for item in chunk.split()
        if item.strip()
    ]


def _parse_headers(text: str) -> dict[str, str]:
    """把「每行 ``Name: value``」的文本框内容解析成请求头字典。

    空行与 ``#`` 开头视为注释；值里允许再出现冒号（切在第一个冒号处）。
    """
    headers: dict[str, str] = {}
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition(":")
        name = name.strip()
        if not separator:
            raise ConfigEditError(
                f"请求头第 {lineno} 行格式不对：写成「名称: 值」（用英文冒号分隔）"
            )
        if not name:
            raise ConfigEditError(f"请求头第 {lineno} 行缺少名称")
        if name in headers:
            raise ConfigEditError(f"请求头 {name} 重复")
        headers[name] = value.strip()
    return headers
