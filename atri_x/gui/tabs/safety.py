"""「内容安全」选项卡：总览 + 策略 + 词表 + 复核 + 审计 + 原始 TOML。

为什么单独一页：1.1.8 把内容安全做成了独立数据目录（``safety.toml`` + ``safety/``
词表/正则/复核库 + ``safety-audit.jsonl``），此前这些只能手改文件；用户反馈
"不知道现在到底是开是关、命中了什么、去哪改词表"。本页把这四件事收在一处：

| 子页 | 内容 | 写入 |
|---|---|---|
| 总览 | 当前开关/模式、复核待处理数、审计条数、词表规模 | 只读（可一键创建默认配置） |
| 策略 | 本地层 / 远程层 / 三档裁决 / 媒体 / 成本 / 云厂商凭据变量 | ``safety.toml`` |
| 词表 | ``safety/wordlists/*.txt`` 与 ``safety/sensitive/*.regex`` 的增删改 + 离线试扫 | 词表文件 |
| 复核 | ``safety/review.db`` 的待处理条目，通过 / 驳回 / 过期 | 复核库 |
| 审计 | ``safety-audit.jsonl`` 尾部（只含脱敏预览与指纹） | 只读 |
| 原始 TOML | 整份 ``safety.toml`` 文本编辑（校验后保存） | ``safety.toml`` |

生效时机：``safety.toml`` 与词表都是**热加载**（后台按文件指纹跟随，≤1 秒），
所以策略保存后不需要重启；只有"启用/停用整个审计写入"这类装配期行为才需要重启。
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

from atri_x.gui import safetyconfig
from atri_x.gui import process as gui_process
from atri_x.gui import widgets
from atri_x.gui.tabs.base import ConfigTab, confirm
from atri_x.gui.safetyviews import AuditPanel, RawTomlPanel, ReviewPanel
from atri_x.gui.safetywordedit import WordlistPanel

__all__ = ["SafetyTab"]


# ---------------------------------------------------------------------------
# 表单字段声明（策略页由这张表驱动，避免 60 个控件写 60 遍）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """一个策略字段：写进 ``safety.toml`` 的哪个小节、什么类型、怎么校验。"""

    section: str
    key: str
    label: str
    kind: str  # bool | choice | int | float | text | list
    choices: tuple[tuple[str, str], ...] = ()
    hint: str = ""
    minimum: int = 0
    maximum: int = 0


_MODE_CHOICES = (
    ("off", "off（关闭）"),
    ("audit", "audit（只记录）"),
    ("enforce", "enforce（拦截）"),
)
_TIER_CHOICES = (
    ("block", "block（拦截）"),
    ("review", "review（进复核）"),
    ("pass", "pass（放行）"),
)
_SENSITIVE_ACTION_CHOICES = (
    ("flag", "flag（只标记）"),
    ("redact", "redact（脱敏）"),
    ("block", "block（拦截）"),
)
_INBOUND_REVIEW_CHOICES = (
    ("pass_and_queue", "照发 + 入复核队列"),
    ("hold", "先压住等复核"),
)
_OUTBOUND_REVIEW_CHOICES = (
    ("send_and_queue", "照发 + 入复核队列"),
    ("hold", "先压住等复核"),
)
_MEDIA_CHOICES = (
    ("sync", "sync（同步送审）"),
    ("async", "async（异步任务）"),
    ("off", "off（不送审）"),
)
_BUDGET_EXCEEDED_CHOICES = (
    ("local_only", "local_only（退化为只跑本地规则）"),
    ("pass_open", "pass_open（放行 + 告警）"),
    ("fail_close", "fail_close（拦截）"),
)

#: 策略表单字段（顺序即界面顺序；按 section 分组渲染）。
FIELD_SPECS: tuple[FieldSpec, ...] = (
    # -- 本地规则层 ---------------------------------------------------------
    FieldSpec("local", "enabled", "启用本地规则层", "bool"),
    FieldSpec("local", "mode", "本地模式", "choice", _MODE_CHOICES),
    FieldSpec("local", "normalize", "归一化后再匹配（防全角/零宽绕过）", "bool"),
    FieldSpec("local", "max_scan_chars", "单次扫描预算（字符）", "int", minimum=500, maximum=20000),
    FieldSpec("local", "categories", "启用的词表类别（留空 = 全部内置）", "list"),
    FieldSpec("local", "sensitive", "启用的敏感信息类型（留空 = 全部内置）", "list"),
    FieldSpec("local", "allowlist", "豁免词（命中即放过）", "list"),
    FieldSpec("local", "audit_preview_chars", "审计预览字符数", "int", minimum=20, maximum=300),
    FieldSpec("local", "block_image_output", "输出图片默认拦截", "bool"),
    FieldSpec("local", "block_file_output", "输出文件默认拦截", "bool"),
    FieldSpec("local", "strip_tool_images", "剥离工具结果里的图片", "bool"),
    # -- 远程审核层 ---------------------------------------------------------
    FieldSpec("remote", "enabled", "启用远程审核（需云厂商凭据）", "bool"),
    FieldSpec("remote", "providers", "供应商顺序", "list", hint="tencent, aliyun"),
    FieldSpec(
        "remote",
        "quorum",
        "多供应商仲裁",
        "choice",
        (
            ("any_block", "any_block（任一家拦就拦）"),
            ("both_pass", "both_pass（两家都放行才放行）"),
            ("primary_only", "primary_only（只看第一家）"),
        ),
    ),
    FieldSpec("remote", "fail_mode", "失败策略", "choice", (
        ("open", "open（放行 + 告警）"),
        ("closed", "closed（拦截）"),
    )),
    FieldSpec("remote", "fail_close_scopes", "强制失败关闭的群（平台:群号）", "list"),
    FieldSpec("remote", "timeout_ms", "单次同步超时（毫秒）", "int", minimum=100, maximum=5000),
    FieldSpec("remote", "retries", "失败重试次数", "int", minimum=0, maximum=5),
    FieldSpec("remote", "cache_ttl_seconds", "内容缓存时长（秒）", "int", minimum=0, maximum=86400),
    FieldSpec("remote", "redact_before_submit", "提交前先脱敏敏感信息", "bool"),
    FieldSpec("remote", "degraded_alert", "降级时写告警", "bool"),
    # -- 三档裁决 -----------------------------------------------------------
    FieldSpec("tiers", "local_wordlist", "本地词表命中", "choice", (
        ("block", "block（拦截）"),
        ("review", "review（进复核）"),
    )),
    FieldSpec("tiers", "local_sensitive_redact", "敏感信息默认动作", "choice", _SENSITIVE_ACTION_CHOICES),
    FieldSpec("tiers", "remote_block", "远程 block 档", "choice", _TIER_CHOICES),
    FieldSpec("tiers", "remote_review", "远程 review 档", "choice", _TIER_CHOICES),
    FieldSpec("tiers", "inbound_review", "入站 review 处置", "choice", _INBOUND_REVIEW_CHOICES),
    FieldSpec("tiers", "outbound_review", "出站 review 处置", "choice", _OUTBOUND_REVIEW_CHOICES),
    FieldSpec("tiers", "high_risk_scopes_hold", "高敏群（review 时压住）", "list"),
    # -- 多模态分路 ---------------------------------------------------------
    FieldSpec("media", "images", "图片", "choice", _MEDIA_CHOICES),
    FieldSpec("media", "audio", "语音", "choice", _MEDIA_CHOICES),
    FieldSpec("media", "video", "视频", "choice", _MEDIA_CHOICES),
    FieldSpec("media", "image_max_bytes", "单图上限（字节）", "int", minimum=1024, maximum=52428800),
    FieldSpec("media", "image_base64_max_bytes", "内联 base64 上限（字节）", "int", minimum=1024, maximum=52428800),
    FieldSpec("media", "object_store", "对象存储地址（留空 = 不走 URL）", "text"),
    FieldSpec("media", "skip_actors", "图片免审用户（用户 id）", "list"),
    # -- 成本 ---------------------------------------------------------------
    FieldSpec("cost", "daily_budget_cny", "每天预算（元，0 = 不限）", "float"),
    FieldSpec("cost", "on_budget_exceeded", "超预算后的行为", "choice", _BUDGET_EXCEEDED_CHOICES),
    FieldSpec("cost", "text_price_cny_per_10k", "文本单价（元/万条）", "float"),
    # -- 云厂商（存的是环境变量名，不是密钥） -------------------------------
    FieldSpec("remote.tencent", "enabled", "启用腾讯云", "bool"),
    FieldSpec("remote.tencent", "region", "地域", "text", hint="如 ap-guangzhou"),
    FieldSpec("remote.tencent", "secret_id_env", "SecretId 环境变量名", "text"),
    FieldSpec("remote.tencent", "secret_key_env", "SecretKey 环境变量名", "text"),
    FieldSpec("remote.aliyun", "enabled", "启用阿里云", "bool"),
    FieldSpec("remote.aliyun", "region", "地域", "text", hint="如 cn-shanghai"),
    FieldSpec("remote.aliyun", "access_key_id_env", "AccessKeyId 环境变量名", "text"),
    FieldSpec("remote.aliyun", "access_key_secret_env", "AccessKeySecret 环境变量名", "text"),
)

#: 小节标题（按出现顺序渲染）。
_SECTION_TITLES: dict[str, str] = {
    "local": "本地规则层 [local]",
    "remote": "远程审核层 [remote]（默认关闭）",
    "tiers": "三档裁决 [tiers]",
    "media": "多模态分路 [media]",
    "cost": "成本与预算 [cost]",
    "remote.tencent": "腾讯云天御 [remote.tencent]",
    "remote.aliyun": "阿里云内容安全 [remote.aliyun]",
}

#: 阶段级远程策略（``[remote.stages]``）：单独渲染成一行一个下拉。
_STAGE_SPECS: tuple[FieldSpec, ...] = tuple(
    FieldSpec(
        "remote.stages",
        name,
        safetyconfig.STAGE_LABELS.get(name, name),
        "choice",
        (
            ("sync", "sync（同步送审）"),
            ("cache_only", "cache_only（只查缓存）"),
            ("local_only", "local_only（只走本地）"),
            ("off", "off（不送审）"),
        ),
    )
    for name in safetyconfig.KNOWN_STAGES
)


class SafetyTab(ConfigTab):
    """内容安全总页：六个子页共享同一个 ``ConfigPaths``。"""

    hint = "safety.toml 与词表保存后 ≤1 秒热加载，不需要重启"

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._panels: dict[str, Any] = {}
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        ttk.Label(
            self.body,
            text=(
                "内容安全 = 本地词表/正则 + （可选）云厂商审核 + 三档裁决 + 人工复核队列 + 审计留痕。"
                "没开远程审核时不需要任何云账号，纯本地规则即可工作；删除 safety.toml 等于整体回退。"
            ),
            foreground="#555555",
            wraplength=1000,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        notebook = ttk.Notebook(self.body)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook = notebook
        pages = (
            (" 总览 ", OverviewPanel),
            (" 策略 ", PolicyPanel),
            (" 词表 ", WordlistPanel),
            (" 复核 ", ReviewPanel),
            (" 审计 ", AuditPanel),
            (" 原始 TOML ", RawTomlPanel),
        )
        for title, panel_class in pages:
            # 子页高度随内容变化（词表/复核在 720p 下会超出可视区），统一放进
            # 可滚动宿主：装得下时没有滚动条，装不下时能滚到底。
            frame = widgets.scrollable_host(notebook)
            notebook.add(frame, text=title)
            panel = panel_class(frame.inner, self.paths, self)
            panel.frame.pack(fill=tk.BOTH, expand=True)
            self._panels[title.strip()] = panel

        bar = ttk.Frame(self.body)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="全部重新载入", command=self.reload).pack(side=tk.LEFT)
        ttk.Label(
            bar,
            text="策略保存后后台自动热加载；词表/正则改完即生效。",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(12, 0))

    # -- 生命周期 -----------------------------------------------------------
    def reload(self) -> None:
        for panel in self._panels.values():
            try:
                panel.reload()
            except Exception as exc:  # noqa: BLE001 - 单个子页失败不影响其它页
                self.fail(exc)
                return

    def on_show(self) -> None:
        # 只刷新只读的总览（其它页可能有没保存的输入）。
        overview = self._panels.get("总览")
        if overview is not None:
            try:
                overview.reload()
            except Exception as exc:  # noqa: BLE001 - 状态刷新失败不该弹窗
                self.fail(exc)

    # -- 面板回调 -----------------------------------------------------------
    def confirm(self, text: str) -> bool:
        """面板的确认弹窗（测试里可 mock）。"""
        return confirm(self.frame, "确认", text)


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------
class OverviewPanel:
    """只读状态摘要 + 一键创建默认配置。"""

    _ROWS = (
        ("file", "策略文件"),
        ("local", "本地规则层"),
        ("remote", "远程审核层"),
        ("review", "复核队列"),
        ("audit", "审计日志"),
        ("words", "用户词表"),
        ("errors", "配置问题"),
    )

    def __init__(self, parent: Any, paths: Any, tab: SafetyTab) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._vars = {key: tk.StringVar(value="—") for key, _ in self._ROWS}
        grid = ttk.Frame(self.frame)
        grid.pack(fill=tk.X)
        for row, (key, label) in enumerate(self._ROWS):
            ttk.Label(grid, text=label, width=14).grid(row=row, column=0, sticky="w", pady=1)
            ttk.Label(grid, textvariable=self._vars[key], wraplength=760).grid(
                row=row, column=1, sticky="w", pady=1
            )
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="刷新", command=self.reload).pack(side=tk.LEFT)
        self._create_button = ttk.Button(
            buttons, text="创建默认配置（audit，只记不拦）", command=self._create
        )
        self._create_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="打开配置目录", command=self._open_config).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="打开审计文件", command=self._open_audit).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="打开词表目录", command=self._open_words).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(
            self.frame,
            text=(
                "提效顺序：先「创建默认配置」→ 保存后只记不拦（audit）跑一天 → 看「审计」页确认命中是否符合预期 "
                "→ 再把「策略 → 本地模式」改成 enforce。"
            ),
            foreground="#8a8a8a",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(8, 0))

    def reload(self) -> None:
        if self.paths is None:
            return
        data = safetyconfig.overview(self.paths)
        self._vars["file"].set(
            safetyconfig.safety_path(self.paths) if data.file_exists else "未创建（= 内容安全未启用）"
        )
        self._vars["local"].set(
            f"已启用 · 模式 {data.local_mode}" if data.local_enabled else "未启用（off）"
        )
        if data.remote_enabled:
            self._vars["remote"].set(
                f"已启用 · 供应商 {', '.join(data.providers) or '未配置'} · 仲裁 {data.quorum} · 失败 {data.fail_mode}"
            )
        else:
            self._vars["remote"].set("未启用（只跑本地规则，不产生云费用）")
        self._vars["review"].set(
            f"待复核 {data.review_pending} 条 / 累计 {data.review_total} 条"
        )
        last = f"，最近一条 {data.last_audit_at}" if data.last_audit_at else ""
        self._vars["audit"].set(f"{data.audit_records} 条{last}")
        self._vars["words"].set(f"{data.wordlist_files} 个文件 / {data.wordlist_terms} 个词条")
        self._vars["errors"].set("；".join(data.errors) if data.errors else "无")
        self._create_button.configure(
            state=tk.DISABLED if data.file_exists else tk.NORMAL
        )

    # -- 按钮 ---------------------------------------------------------------
    def _create(self) -> None:
        try:
            safetyconfig.create_default_safety_file(self.paths)
        except Exception as exc:  # noqa: BLE001 - 直接展示给用户
            self.tab.fail(exc)
            return
        self.tab.journal("内容安全：创建默认 safety.toml（audit 模式）")
        self.tab.reload()
        self.tab.notify("已创建默认配置：当前只记录不拦截；确认命中符合预期后改成 enforce")

    def _open_config(self) -> None:
        self._open(safetyconfig.safety_path(self.paths))

    def _open_audit(self) -> None:
        settings, _errors = safetyconfig.load_settings(self.paths)
        self._open(safetyconfig.audit_path(self.paths, settings))

    def _open_words(self) -> None:
        settings, _errors = safetyconfig.load_settings(self.paths)
        self._open(safetyconfig.wordlists_dir(self.paths, settings))

    def _open(self, path: str) -> None:
        if not path or not gui_process.open_path(path):
            self.tab.notify(f"打不开：{path or '路径为空'}")


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
class PolicyPanel:
    """``safety.toml`` 的表单编辑（字段由 :data:`FIELD_SPECS` 驱动）。"""

    def __init__(self, parent: Any, paths: Any, tab: SafetyTab) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._vars: dict[tuple[str, str], Any] = {}
        self._specs: dict[tuple[str, str], FieldSpec] = {}
        scroller_holder = ttk.Frame(self.frame)
        scroller_holder.pack(fill=tk.BOTH, expand=True)
        from atri_x.gui import widgets

        self.scroller = widgets.ScrollFrame(scroller_holder)
        self.scroller.pack(fill=tk.BOTH, expand=True)
        self.body = self.scroller.inner
        self._build_form()
        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="保存策略", command=self._save).pack(side=tk.LEFT)
        ttk.Button(bar, text="重新载入", command=self.reload).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            bar,
            text="云厂商两项填的是「环境变量名」，密钥放 .env 或系统环境变量，不要写进 safety.toml。",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(12, 0))

    # -- 构建 ---------------------------------------------------------------
    def _build_form(self) -> None:
        specs = FIELD_SPECS + _STAGE_SPECS
        grouped: dict[str, list[FieldSpec]] = {}
        for spec in specs:
            grouped.setdefault(spec.section, []).append(spec)
        for section, items in grouped.items():
            box = ttk.LabelFrame(
                self.body, text=_SECTION_TITLES.get(section, section), padding=6
            )
            box.pack(fill=tk.X, pady=(0, 8))
            box.columnconfigure(1, weight=1)
            for row, spec in enumerate(items):
                ttk.Label(box, text=spec.label, width=30).grid(
                    row=row, column=0, sticky="w", pady=1
                )
                self._add_control(box, row, spec)

    def _add_control(self, parent: Any, row: int, spec: FieldSpec) -> None:
        key = (spec.section, spec.key)
        self._specs[key] = spec
        if spec.kind == "bool":
            variable: Any = tk.BooleanVar(value=False)
            ttk.Checkbutton(parent, variable=variable).grid(
                row=row, column=1, sticky="w", pady=1
            )
        elif spec.kind == "choice":
            variable = tk.StringVar(value=spec.choices[0][0] if spec.choices else "")
            values = [f"{value}  {label}" for value, label in spec.choices]
            combo = ttk.Combobox(
                parent, textvariable=variable, values=values, state="readonly", width=34
            )
            combo.grid(row=row, column=1, sticky="w", pady=1)
            combo.bind("<<ComboboxSelected>>", lambda _e, c=combo, v=variable: v.set(
                str(v.get()).split("  ")[0]
            ))
        elif spec.kind in ("int", "float"):
            variable = tk.StringVar(value="0")
            width = 10 if spec.kind == "int" else 14
            ttk.Entry(parent, textvariable=variable, width=width).grid(
                row=row, column=1, sticky="w", pady=1
            )
            if spec.minimum or spec.maximum:
                ttk.Label(
                    parent,
                    text=f"{spec.minimum} ~ {spec.maximum}",
                    foreground="#8a8a8a",
                ).grid(row=row, column=2, sticky="w", padx=(6, 0))
        else:
            variable = tk.StringVar(value="")
            entry = ttk.Entry(parent, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=1)
            if spec.hint:
                ttk.Label(parent, text=spec.hint, foreground="#8a8a8a").grid(
                    row=row, column=2, sticky="w", padx=(6, 0)
                )
        self._vars[key] = variable

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if self.paths is None:
            return
        settings, errors = safetyconfig.load_settings(self.paths)
        for (section, key), spec in self._specs.items():
            value = _section_value(settings, section, key)
            variable = self._vars[(section, key)]
            if spec.kind == "bool":
                variable.set(bool(value))
            elif spec.kind == "choice":
                text = str(value or (spec.choices[0][0] if spec.choices else ""))
                _set_choice(variable, spec, text)
            elif spec.kind in ("int", "float"):
                variable.set("" if value is None else str(value))
            elif spec.kind == "list":
                items = value if isinstance(value, (list, tuple)) else []
                variable.set(", ".join(str(item) for item in items))
            else:
                variable.set(str(value or ""))
        state = "；".join(errors) if errors else "已载入当前策略（保存后 ≤1 秒热加载）"
        self.tab.notify(state)

    def _save(self) -> None:
        try:
            paths = self.tab.require_paths()
            sections: dict[str, dict[str, Any]] = {}
            for (section, key), spec in self._specs.items():
                if section.endswith(".stages") and not str(
                    self._vars[(section, key)].get()
                ).strip():
                    continue  # 阶段策略允许留空 = 用默认
                sections.setdefault(section, {})[key] = _field_value(
                    spec, self._vars[(section, key)].get()
                )
            safetyconfig.save_safety_sections(paths, sections)
        except Exception as exc:  # noqa: BLE001 - 表单错误直接展示
            self.tab.fail(exc)
            return
        self.tab.journal("内容安全：保存 safety.toml 策略")
        self.tab.notify("策略已保存；后台 ≤1 秒热加载")
        self.reload()


def _section_value(settings: Any, section: str, key: str) -> Any:
    """从 ``SafetySettings`` 里取某小节某字段（嵌套小节逐级下钻）。"""
    current: Any = settings
    for name in section.split("."):
        current = getattr(current, name, None)
        if current is None:
            return None
    return getattr(current, key, None)


def _set_choice(variable: tk.StringVar, spec: FieldSpec, value: str) -> None:
    for choice, label in spec.choices:
        if choice == value:
            variable.set(f"{choice}  {label}")
            return
    variable.set("")


def _field_value(spec: FieldSpec, raw: Any) -> Any:
    """把界面值转换成 TOML 值；非法输入抛 ``ConfigEditError``。"""
    from atri_x.gui.configedit import ConfigEditError

    if spec.kind == "bool":
        return bool(raw)
    if spec.kind == "choice":
        text = str(raw).split("  ")[0].strip()
        allowed = {choice for choice, _ in spec.choices}
        if text not in allowed:
            raise ConfigEditError(f"「{spec.label}」的取值非法：{text or '空'}")
        return text
    if spec.kind in ("int", "float"):
        text = str(raw).strip()
        try:
            number = int(text) if spec.kind == "int" else float(text)
        except ValueError:
            raise ConfigEditError(f"「{spec.label}」需要数字，得到 {text!r}") from None
        if spec.minimum and number < spec.minimum:
            raise ConfigEditError(f"「{spec.label}」不能小于 {spec.minimum}")
        if spec.maximum and number > spec.maximum:
            raise ConfigEditError(f"「{spec.label}」不能大于 {spec.maximum}")
        return number
    if spec.kind == "list":
        return [
            item.strip()
            for item in str(raw).replace("，", ",").split(",")
            if item.strip()
        ]
    return str(raw or "")
