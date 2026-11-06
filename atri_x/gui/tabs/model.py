"""「模型」选项卡：LLM Provider（含 API Key）、上下文窗口、模型分配、搜索与抓取服务。

这是桌面端最常用的配置页，拆成四块（二级页签）：

1. **LLM Provider**（写 ``services.toml`` 的 ``[providers.<name>]``）：
   配置端点、模型、协议、请求头与推理强度（可按模型覆盖）。
   密钥单独写入 .env，默认掩码且不回显既有明文。
   另有**模型分配**（写 ``state.toml``）：工作/聊天/命名/压缩四个槽位 + 默认模式。
   ``state.toml`` 是运行期回写的文件，后台服务会热加载，保存后几秒生效，不需要重启。
2. **上下文窗口**（写 ``services.toml`` 的 ``[model_metadata].default_context_window``
   与 ``[providers.<name>.model_configs.<model>].context_window``）：chat / work
   解析窗口时，provider 上报的元数据优先，其次是这里的按模型覆盖、默认值，
   最后是内置 256000。页面上的「模型窗口总览」表会把**扫描到的上游模型**和
   已配置/已覆盖的模型列在一起：上游列展示 ``/models`` 上报的理论窗口，自定义
   列可点击单元格直接改（写进 ``model_configs.<模型>.context_window``）。
   这是启动期配置，改完需要「重启后台服务」。
3. **搜索服务**（写 ``services.toml`` 的 ``[web_search]``）：``web_search`` 工具按列表
   自上而下逐条尝试通道——DeepSeek 官方搜索 / Tavily / Firecrawl / Keenable / Exa /
   SearXNG / Antigravity CLI / Grok Build，外加任意「自定义」通道（选一个协议 + 服务地址
   + 密钥）。免注册通道不填 key 也能用；密钥值和 provider 一样写进 ``.env``，这里只存
   环境变量名。
4. **抓取服务**（写 ``services.toml`` 的 ``[web_extract]``）：``web_extract`` 工具按列表
   逐条尝试通道——DeepSeek 官方抓取 / Jina Reader / 直接抓取，失败自动降级；默认链是
   Jina（有 key 才参与）→ 直接抓取。

模型名不用手打：点「获取可用模型」会 ``GET {base_url}/models`` 把上游模型列出来，之后
在下拉里点选；每个模型还能起一个**本地备注名**（``gui/model_catalog.json``，不影响上游
模型名），下拉里显示成 ``备注（模型）``，保存时再还原成真模型名。

Provider、``.env``、搜索服务与抓取服务都属于启动期配置，改完要点底部的「重启后台服务」。
"""

from __future__ import annotations

import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
    BUILTIN_CONTEXT_WINDOW,
    MODEL_SLOTS,
    ProviderView,
    delete_provider,
    load_context_windows,
    load_model_selection,
    load_providers,
    parse_context_window,
    provider_key_state,
    remove_model_context_window,
    resolve_fetch_key,
    save_default_context_window,
    save_model_context_window,
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

#: 上下文窗口页 Provider 下拉里的「全部」项（筛选/批量扫描用）。
_ALL_PROVIDERS = "（全部 Provider）"


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
    """Provider 列表 + 表单 + 上下文窗口 + 模型分配 + 搜索服务 + 抓取服务。"""

    scrollable = True  # 表单比 1080p 窗口高（实测 req≈942 > 可视≈900），必须能整页滚动。

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._providers: dict[str, ProviderView] = {}
        self._catalog = modelcatalog.Catalog()
        self._original = ""
        #: 已保存的「按模型上下文窗口」覆盖：``(provider, model) -> tokens``。
        self._context_overrides: dict[tuple[str, str], int] = {}
        #: 模型窗口表当前行：``iid -> (provider, model)``（表刷新时重建）。
        self._ctx_rows: dict[str, tuple[str, str]] = {}
        #: 正在编辑的单元格输入框（``None`` 表示没在编辑）。
        self._ctx_editor: Any = None
        self._ctx_edit_iid = ""
        #: 扫描上游窗口的后台任务是否在跑（防重复点击）。
        self._ctx_scanning = False
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.notebook = ttk.Notebook(self.body)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        llm_page = ttk.Frame(self.notebook)
        context_page = ttk.Frame(self.notebook)
        search_page = ttk.Frame(self.notebook)
        extract_page = ttk.Frame(self.notebook)
        self.notebook.add(llm_page, text="LLM Provider / 模型分配")
        self.notebook.add(context_page, text="上下文窗口")
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

        self._build_context_page(context_page)
        self.search_panel = SearchServicePanel(search_page, self)
        self.extract_panel = ExtractServicePanel(extract_page, self)

    def _row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        return entry

    def _build_context_page(self, page: Any) -> None:
        """「上下文窗口」页：默认窗口 + 按模型覆盖（都写 ``services.toml``）。"""
        default_box = ttk.LabelFrame(
            page, text="默认上下文窗口（services.toml [model_metadata]）", padding=8
        )
        default_box.pack(fill=tk.X)
        default_row = ttk.Frame(default_box)
        default_row.pack(fill=tk.X)
        ttk.Label(default_row, text="默认窗口（tokens）", width=20).pack(side=tk.LEFT)
        self.default_window_var = tk.StringVar()
        ttk.Entry(default_row, textvariable=self.default_window_var, width=16).pack(
            side=tk.LEFT
        )
        ttk.Button(default_row, text="保存默认值", command=self._save_default_window).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(default_row, text="恢复内置默认", command=self._reset_default_window).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            default_box,
            text=(
                "没有单独覆盖的 provider/model 都用它；留空 = 内置 "
                f"{BUILTIN_CONTEXT_WINDOW}。\n"
                "支持 1000000 / 1_000_000 / 1m / 512k；上游 /models 若上报了窗口，"
                "以它为准（本页只在没上报时兜底）。"
            ),
            foreground="#8a8a8a",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

        table_box = ttk.LabelFrame(
            page,
            text=(
                "模型窗口总览（扫描上游 /models 上报窗口；按模型覆盖写 services.toml "
                "[providers.<名称>.model_configs.<模型>].context_window）"
            ),
            padding=8,
        )
        table_box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        toolbar = ttk.Frame(table_box)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Provider", width=10).pack(side=tk.LEFT)
        self.ctx_provider_var = tk.StringVar(value=_ALL_PROVIDERS)
        self.ctx_provider_box = widgets.dropdown(
            toolbar,
            self.ctx_provider_var,
            width=20,
            height=_DROPDOWN_HEIGHT,
            state="readonly",
            on_open=self._refresh_ctx_providers,
        )
        self.ctx_provider_box.pack(side=tk.LEFT)
        self.ctx_provider_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_context_tree()
        )
        self.ctx_scan_button = ttk.Button(
            toolbar, text="扫描上游窗口", command=self._scan_context_windows
        )
        self.ctx_scan_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            toolbar, text="删除所选覆盖", command=self._delete_selected_context_override
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="重新载入", command=self._load_context_windows).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.ctx_status_var = tk.StringVar(
            value="还没扫描过；点「扫描上游窗口」拉取模型清单与上报窗口"
        )
        ttk.Label(toolbar, textvariable=self.ctx_status_var, foreground="#8a8a8a").pack(
            side=tk.LEFT, padx=(10, 0)
        )

        tree_frame = ttk.Frame(table_box)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.ctx_tree = ttk.Treeview(
            tree_frame,
            columns=("provider", "model", "upstream", "custom"),
            show="headings",
            height=12,
            selectmode="browse",
        )
        self.ctx_tree.heading("provider", text="Provider")
        self.ctx_tree.heading("model", text="模型")
        self.ctx_tree.heading("upstream", text="上游上报窗口（tokens）")
        self.ctx_tree.heading("custom", text="自定义覆盖（点击修改，tokens）")
        self.ctx_tree.column("provider", width=150, stretch=False)
        self.ctx_tree.column("model", width=300)
        self.ctx_tree.column("upstream", width=180, stretch=False)
        self.ctx_tree.column("custom", width=210, stretch=False)
        tree_scroll = ttk.Scrollbar(
            tree_frame, orient=tk.VERTICAL, command=self.ctx_tree.yview
        )
        self.ctx_tree.configure(yscrollcommand=tree_scroll.set)
        self.ctx_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.ctx_tree.bind("<ButtonRelease-1>", self._context_tree_clicked)
        self.ctx_tree.bind("<Double-1>", self._context_tree_double_clicked)

        manual = ttk.Frame(table_box)
        manual.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(manual, text="手动添加覆盖：模型名").pack(side=tk.LEFT)
        self.ctx_manual_model_var = tk.StringVar()
        ttk.Entry(manual, textvariable=self.ctx_manual_model_var, width=30).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(manual, text="  窗口（tokens）").pack(side=tk.LEFT)
        self.ctx_manual_window_var = tk.StringVar()
        ttk.Entry(manual, textvariable=self.ctx_manual_window_var, width=12).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Button(
            manual, text="保存覆盖", command=self._save_manual_context_override
        ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(
            table_box,
            text=(
                "「上游上报」是 /models 的原话：多数网关的模型清单不带窗口，也可能挂在服务根、"
                "后台服务读不到；要让某个模型稳定用某个窗口，就点「自定义覆盖」单元格写入。\n"
                "点击自定义列（或双击任意列）直接改：回车保存、Esc 取消；清空覆盖请用"
                "「删除所选覆盖」。只影响上下文预算 / 压缩阈值与用量百分比，超过上游限长仍会被"
                "上游拒绝；保存后需「重启后台服务」生效。"
            ),
            foreground="#8a8a8a",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

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
        self._load_context_windows()
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
        identity = (self._catalog_key(), base_url)
        widgets.run_async(
            self.frame,
            lambda: modelcatalog.fetch_models(base_url, api_key),
            lambda models, error: self._models_result(identity, models, error),
        )

    def _models_result(self, identity: tuple[str, str], models: Any, error: Any) -> None:
        if identity != (self._catalog_key(), self.base_url_var.get().strip()):
            return  # 请求期间已切换 Provider 或地址，不能污染当前目录。
        self._models_fetched(models, error)

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

    # -- 上下文窗口 ---------------------------------------------------------
    def _load_context_windows(self) -> None:
        """从 ``services.toml`` 读默认窗口与按模型覆盖，刷新本页控件。"""
        if not self.is_ready():
            return
        try:
            data = load_context_windows(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.default_window_var.set(str(data.default) if data.default > 0 else "")
        self._context_overrides = dict(data.overrides)
        self._refresh_ctx_providers()
        self._refresh_context_tree()

    def _refresh_ctx_providers(self) -> None:
        """Provider 下拉 = 「全部」+ 现有 provider（既做筛选也做批量扫描目标）。"""
        box = getattr(self, "ctx_provider_box", None)
        if box is None:
            return
        values = [_ALL_PROVIDERS] + sorted(self._providers)
        try:
            box.configure(values=values)
            if self.ctx_provider_var.get() not in values:
                self.ctx_provider_var.set(_ALL_PROVIDERS)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    def _context_provider_names(self) -> list[str]:
        """当前要展示/扫描的 provider（「全部」= 所有配置过的 provider）。"""
        chosen = self.ctx_provider_var.get().strip()
        if chosen and chosen in self._providers:
            return [chosen]
        return sorted(self._providers)

    def _refresh_context_tree(self) -> None:
        """重建模型窗口表：扫描到的模型 ∪ 已配置模型 ∪ 已有覆盖。"""
        tree = getattr(self, "ctx_tree", None)
        if tree is None:
            return
        selected = tree.selection()[0] if tree.selection() else ""
        rows: dict[str, tuple[str, str]] = {}
        try:
            tree.delete(*tree.get_children())
            for provider in self._context_provider_names():
                key = modelcatalog.provider_key(provider)
                view = self._providers.get(provider)
                configured = list(view.models) if view is not None else []
                models = self._catalog.names(key, configured)
                seen = set(models)
                for override_provider, override_model in sorted(self._context_overrides):
                    if override_provider == provider and override_model not in seen:
                        seen.add(override_model)
                        models.append(override_model)
                for model in models:
                    iid = f"r{len(rows)}"
                    rows[iid] = (provider, model)
                    upstream = self._catalog.window_of(key, model)
                    custom = self._context_overrides.get((provider, model), 0)
                    tree.insert(
                        "",
                        tk.END,
                        iid=iid,
                        values=(
                            provider,
                            model,
                            _format_tokens(upstream),
                            str(custom) if custom else "",
                        ),
                    )
            self._ctx_rows = rows
            if selected in rows:
                tree.selection_set(selected)
                tree.see(selected)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    def _context_tree_clicked(self, event: Any) -> None:
        """点「自定义覆盖」单元格 → 就地把这一格变成输入框。"""
        tree = getattr(self, "ctx_tree", None)
        if tree is None or self._ctx_editor is not None:
            return
        if tree.identify_region(event.x, event.y) != "cell":
            return
        if tree.identify_column(event.x) != "#4":
            return
        iid = tree.identify_row(event.y)
        if iid and iid in self._ctx_rows:
            self._start_context_edit(iid)

    def _context_tree_double_clicked(self, event: Any) -> None:
        """双击其它列也进编辑（自定义列已被单击路径处理）。"""
        tree = getattr(self, "ctx_tree", None)
        if tree is None or self._ctx_editor is not None:
            return
        if tree.identify_region(event.x, event.y) != "cell":
            return
        iid = tree.identify_row(event.y)
        if not iid or iid not in self._ctx_rows:
            return
        if tree.identify_column(event.x) != "#4":
            self._start_context_edit(iid)

    def _start_context_edit(self, iid: str) -> None:
        """在单元格上放一个输入框（回车保存 / Esc 取消 / 失焦保存）。"""
        tree = self.ctx_tree
        provider, model = self._ctx_rows.get(iid, ("", ""))
        if not provider:
            return
        self._cancel_context_edit()
        bbox = tree.bbox(iid, "#4")
        if not bbox:
            return
        current = self._context_overrides.get((provider, model), 0)
        editor = ttk.Entry(tree)
        if current > 0:
            editor.insert(0, str(current))
        editor.select_range(0, tk.END)
        editor.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        editor.focus_set()
        self._ctx_editor = editor
        self._ctx_edit_iid = iid
        editor.bind("<Return>", lambda _event: self._commit_context_edit())
        editor.bind("<Escape>", lambda _event: self._cancel_context_edit())
        editor.bind("<FocusOut>", lambda _event: self._commit_context_edit())
        tree.selection_set(iid)

    def _commit_context_edit(self) -> None:
        """保存单元格：写 ``model_configs.<模型>.context_window`` 并回读校验。"""
        editor = self._ctx_editor
        if editor is None:
            return
        iid = self._ctx_edit_iid
        text = editor.get()
        self._ctx_editor = None
        self._ctx_edit_iid = ""
        try:
            editor.destroy()
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        provider, model = self._ctx_rows.get(iid, ("", ""))
        if not provider or not model:
            return
        if not text.strip():
            self.notify("留空没有保存；要删除覆盖请选中该行点「删除所选覆盖」")
            return
        try:
            window = parse_context_window(text)
            save_model_context_window(self.require_paths(), provider, model, window)
        except ConfigEditError as exc:
            self.notify(f"没有保存：{exc}")
            self._refresh_context_tree()
            return
        self._load_context_windows()
        self.journal(f"保存模型上下文窗口：{provider}/{model} = {window}")
        self.notify(
            f"已保存 {provider}/{model} 的上下文窗口为 {window} tokens；重启后台服务后生效"
        )

    def _cancel_context_edit(self) -> None:
        editor = self._ctx_editor
        self._ctx_editor = None
        self._ctx_edit_iid = ""
        if editor is None:
            return
        try:
            editor.destroy()
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    def _delete_selected_context_override(self) -> None:
        tree = getattr(self, "ctx_tree", None)
        selection = tree.selection() if tree is not None else ()
        if not selection or selection[0] not in self._ctx_rows:
            self.notify("先在表里选中一行（要删除的是它的自定义覆盖）")
            return
        provider, model = self._ctx_rows[selection[0]]
        window = self._context_overrides.get((provider, model), 0)
        if window <= 0:
            self.notify(f"{provider}/{model} 没有自定义覆盖，无需删除")
            return
        if not confirm(
            self.frame,
            "删除覆盖",
            f"删除 {provider}/{model} 的上下文窗口覆盖（{window} tokens）？\n\n"
            "删除后按「上游上报 → 默认窗口 → 内置 256000」解析，可回滚 .gui.bak。",
        ):
            return
        try:
            remove_model_context_window(self.require_paths(), provider, model)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._load_context_windows()
        self.journal(f"删除模型上下文窗口：{provider}/{model}")
        self.notify(f"已删除 {provider}/{model} 的上下文窗口覆盖；重启后台服务后生效")

    def _save_manual_context_override(self) -> None:
        """手打模型名保存覆盖（表里没扫到的模型也能设）。"""
        provider = self.ctx_provider_var.get().strip()
        model = ""
        window = 0
        try:
            if provider not in self._providers:
                raise ConfigEditError(
                    "先把 Provider 下拉选成一个具体 Provider（不能是「全部 Provider」）"
                )
            model = self.ctx_manual_model_var.get().strip()
            if not model:
                raise ConfigEditError("先填模型名（例如 deepseek-v4.1-flash）")
            window = parse_context_window(self.ctx_manual_window_var.get())
            save_model_context_window(self.require_paths(), provider, model, window)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.ctx_manual_window_var.set("")
        self._load_context_windows()
        self.journal(f"保存模型上下文窗口：{provider}/{model} = {window}")
        self.notify(
            f"已保存 {provider}/{model} 的上下文窗口为 {window} tokens；重启后台服务后生效"
        )

    def _scan_context_windows(self) -> None:
        """后台扫描上游 ``/models``：模型清单 + 上报窗口写进本地模型目录。"""
        if not self.is_ready() or self._ctx_scanning:
            return
        targets = self._context_provider_names()
        if not targets:
            self.fail("先在上面「LLM Provider」页添加一个 Provider")
            return
        paths = self.require_paths()
        jobs: list[tuple[str, str, str, dict[str, str], str]] = []
        for name in targets:
            view = self._providers.get(name)
            if view is None:
                continue
            api_key, key_source = resolve_fetch_key("", paths, view.api_key_env)
            jobs.append((name, view.base_url, api_key, dict(view.extra_headers), key_source))
        if not jobs:
            self.fail("没有可扫描的 Provider")
            return
        self._ctx_scanning = True
        try:
            self.ctx_scan_button.configure(state=tk.DISABLED)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        self.notify("正在扫描上游模型窗口…")
        self.journal(
            "扫描上游模型窗口："
            + "、".join(
                f"{name}（密钥：{source or '未配置'}）" for name, _b, _k, _h, source in jobs
            )
        )
        widgets.run_async(
            self.frame, lambda: _scan_context_windows_worker(jobs), self._context_scan_done
        )

    def _context_scan_done(self, result: Any, error: BaseException | None) -> None:
        self._ctx_scanning = False
        try:
            self.ctx_scan_button.configure(state=tk.NORMAL)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        if error is not None:
            self.journal(f"扫描上游模型窗口失败：{error}")
            self.fail(f"扫描上游模型窗口失败：{error}")
            return
        results, errors = (
            result if isinstance(result, tuple) and len(result) == 2 else ([], [])
        )
        total = 0
        for name, windows, source in results:
            key = modelcatalog.provider_key(name)
            self._catalog.record(key, list(windows), source=source)
            self._catalog.set_windows(key, windows)
            total += len(windows)
        try:
            self._catalog.save(self.require_paths().config_dir)
        except modelcatalog.CatalogError as exc:
            self.fail(exc)
            return
        self._refresh_catalog_widgets()
        self._refresh_ctx_providers()
        self._refresh_context_tree()
        stamp = datetime.now().strftime("%H:%M:%S")
        summary = f"成功 {len(results)} 个 Provider / {total} 个模型"
        if errors:
            summary += f"，失败 {len(errors)} 个（详情见日志页「界面操作」）"
        self.ctx_status_var.set(f"上次扫描 {stamp}：{summary}")
        detail = "；".join(errors)
        self.journal(f"扫描上游模型窗口：{summary}" + (f"；{detail}" if detail else ""))
        self.notify(f"扫描完成：{summary}")

    def _save_default_window(self) -> None:
        try:
            paths = self.require_paths()
            text = self.default_window_var.get().strip()
            window = parse_context_window(text) if text else None
            save_default_context_window(paths, window)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._load_context_windows()
        shown = f"{window} tokens" if window else f"内置默认 {BUILTIN_CONTEXT_WINDOW}"
        self.journal(f"保存默认上下文窗口：{shown}")
        self.notify(f"已保存默认上下文窗口为 {shown}；重启后台服务后生效")

    def _reset_default_window(self) -> None:
        self.default_window_var.set("")
        self._save_default_window()

def _format_tokens(value: int) -> str:
    """表格里的窗口数字：有值加千分位，没上报显示「—」。"""
    number = int(value or 0)
    return f"{number:,}" if number > 0 else "—"


def _scan_context_windows_worker(
    jobs: list[tuple[str, str, str, dict[str, str], str]],
) -> tuple[list[tuple[str, dict[str, int], str]], list[str]]:
    """后台线程：并发拉各 Provider 的 ``/models`` 窗口（不碰 Tk 控件）。

    逐个串行扫时，一个连不上的 Provider 会拖住整次扫描（每个候选 URL 等满超时）；
    这里按 Provider 并发，最坏只等一个超时（``timeout=10``）。
    """

    def fetch(job: tuple[str, str, str, dict[str, str], str]) -> tuple:
        name, base_url, api_key, extra_headers, _key_source = job
        try:
            windows = modelcatalog.fetch_model_windows(
                base_url, api_key, extra_headers=extra_headers, timeout=10.0
            )
        except modelcatalog.CatalogError as exc:
            return name, base_url, None, str(exc)
        except Exception as exc:  # noqa: BLE001 - 单个 Provider 失败不该拖垮整次扫描
            return name, base_url, None, str(exc)
        return name, base_url, windows, ""

    results: list[tuple[str, dict[str, int], str]] = []
    errors: list[str] = []
    workers = min(4, max(1, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, base_url, windows, message in pool.map(fetch, jobs):
            if windows is None:
                errors.append(f"{name}：{message}")
            else:
                results.append((name, windows, base_url))
    return results, errors


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
