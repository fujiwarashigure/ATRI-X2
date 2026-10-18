"""「进阶」选项卡：生图/视觉/语音、记忆与群功能开关、日志级别。

这些配置不在主流程上，但都是"要开一个功能就得改文件"的那一类，所以集中到一页：
界面负责开关和几个关键字段，写回的还是原来的文件——

- ``services.toml``：``[image_generation]`` / ``[image_to_prompt]`` / ``[vision]`` / ``[asr]``；
- ``app.toml``：``[runtime]`` / ``[angel_memory]`` / ``[self_learning]`` /
  ``[group_analysis]`` / ``[group_services]`` / ``[maintenance.*]``；
- ``atrinis.toml``：顶层 ``enabled``（外部事件入口）。

生图区额外提供「本地生图服务挂载」助手：端口映射 / SSL 链接 → OpenAI 兼容 Base URL，
实现见 :mod:`atri_x.gui.tabs.mountpanel`（本页只继承它的界面）。自学习、HOOK 插件、
定时维护任务三块高级设置不占选项卡，由按钮打开二级窗口（:mod:`atri_x.gui.dialogs`）。

本页内容比窗口高（尤其展开本地挂载后），所以整页放在可滚动容器里
（``scrollable = True``，见 :class:`atri_x.gui.tabs.base.ConfigTab`）：
窗口小的时候滚轮/滚动条就能看到下方的开关和维护任务入口，不会被挤到窗口外面。

都属于启动期配置：保存后需要「重启后台服务」。更细的参数（并发、重试、提示词策略等）
仍写在文件里，界面不覆盖它们。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import dialogs, modelcatalog, widgets
from atri_x.gui.configedit import (
    ConfigEditError,
    patch_section,
    patch_top_level,
    read_toml,
    set_env_value,
)
from atri_x.gui.configmodel import secret_state, secret_value
from atri_x.gui.tabs.base import ConfigTab
from atri_x.gui.tabs.mountpanel import MountPanelMixin

__all__ = ["AdvancedTab"]

#: 日志级别下拉项。
_LOG_LEVELS = ("debug", "info", "warn", "error")

#: 维护任务 id → 界面名（顺序即展示顺序）。
_MAINTENANCE_LABELS = (
    ("log_cleanup", "日志清理"),
    ("session_cleanup", "会话表清理"),
    ("sandbox_cleanup", "沙箱目录清理"),
    ("chat_history_cleanup", "聊天历史清理"),
    ("dsh_cleanup", "DSH 内核会话清理"),
    ("privacy_cleanup", "隐私数据清理"),
    ("daily_report", "资源/用量报告"),
    ("daily_summary", "会话日总结"),
)


class AdvancedTab(ConfigTab, MountPanelMixin):
    """进阶配置（生图/生图挂载 + 视觉/ASR + 功能开关 + 维护任务入口 + 日志）。"""

    hint = ""
    #: 本页内容比窗口高（展开本地生图挂载后更高），必须能竖向滚动，否则下方的
    #: 「记忆 / 群功能 / 外部事件」「定时维护任务」会被挤到窗口外面点不到。
    scrollable = True

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._catalog = modelcatalog.Catalog()
        self._stored_image_model = ""
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        # 两列：左边「生图 + 运行与日志」偏高，右边三块开关类放一列。生图区展开
        # 本地挂载后会变高，所以整页还套了可滚动容器（见类属性 ``scrollable``）。
        grid = ttk.Frame(self.body)
        grid.pack(fill=tk.BOTH, expand=True)
        grid.columnconfigure(0, weight=3)
        grid.columnconfigure(1, weight=2)
        left = ttk.Frame(grid)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        right = ttk.Frame(grid)
        right.grid(row=0, column=1, sticky="nsew")
        self._build_image_box(left)
        self._build_runtime_box(left)
        self._build_media_box(right)
        self._build_toggle_box(right)
        self._build_maintenance_box(right)

        self.button("保存进阶配置", self._save)
        self.button("重新载入", self.reload)

    def _build_runtime_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="运行与日志（app.toml [runtime]）", padding=8)
        box.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(box, text="日志级别").pack(side=tk.LEFT)
        self.log_level_var = tk.StringVar()
        ttk.Combobox(
            box, textvariable=self.log_level_var, values=list(_LOG_LEVELS), width=8
        ).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(box, text="日志保留天数").pack(side=tk.LEFT)
        self.log_retention_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.log_retention_var, width=8).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    def _build_image_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="生图（services.toml [image_generation]）", padding=8)
        box.pack(fill=tk.BOTH, expand=True)
        box.columnconfigure(1, weight=1)
        self.image_enabled = tk.BooleanVar()
        ttk.Checkbutton(box, text="启用生图工具 image_generate", variable=self.image_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.image_base_url = tk.StringVar()
        self.image_api_env = tk.StringVar()
        self.image_model = tk.StringVar()
        self.image_alias = tk.StringVar()
        self.image_key_value = tk.StringVar()
        self._text_row(box, 1, "Base URL", self.image_base_url)
        self._text_row(box, 2, "密钥环境变量", self.image_api_env)
        ttk.Label(box, text="密钥值").grid(row=3, column=0, sticky="w", pady=2)
        self.image_key_entry = widgets.SecretEntry(box, self.image_key_value, width=22)
        self.image_key_entry.grid(row=3, column=1, sticky="ew", pady=2)
        model_row = ttk.Frame(box)
        model_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(model_row, text="模型", width=18).pack(side=tk.LEFT)
        self.image_model_box = widgets.dropdown(
            model_row,
            self.image_model,
            width=22,
            height=12,
            on_open=self._refresh_image_models,
        )
        self.image_model_box.pack(side=tk.LEFT)
        self.image_model_box.bind("<<ComboboxSelected>>", self._image_model_picked)
        ttk.Button(model_row, text="获取可用模型", command=self._fetch_image_models).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        alias_row = ttk.Frame(box)
        alias_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Label(alias_row, text="模型备注名", width=18).pack(side=tk.LEFT)
        ttk.Entry(alias_row, textvariable=self.image_alias, width=18).pack(side=tk.LEFT)
        ttk.Button(alias_row, text="保存备注", command=self._save_image_alias).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(alias_row, text="清除", command=self._clear_image_alias).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        self.image_state = ttk.Label(box, text="", foreground="#555555")
        self.image_state.grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(
            box,
            text="备注名只存本机 gui/model_catalog.json，用于下拉显示；模型名照旧写 services.toml。",
            foreground="#8a8a8a",
            wraplength=380,
            justify=tk.LEFT,
        ).grid(row=7, column=0, columnspan=2, sticky="w")
        self._build_mount_box(box, row=8)

    def _build_media_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="视觉与语音（services.toml）", padding=8)
        box.pack(fill=tk.X)
        box.columnconfigure(1, weight=1)
        self.vision_enabled = tk.BooleanVar()
        ttk.Checkbutton(box, text="视觉回退（图片描述）", variable=self.vision_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.vision_provider = tk.StringVar()
        self.vision_model = tk.StringVar()
        self._text_row(box, 1, "视觉 provider", self.vision_provider)
        self._text_row(box, 2, "视觉 model", self.vision_model)
        self.asr_enabled = tk.BooleanVar()
        ttk.Checkbutton(box, text="语音转写（ASR）", variable=self.asr_enabled).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        self.asr_provider = tk.StringVar()
        self.asr_model = tk.StringVar()
        self.asr_language = tk.StringVar()
        self._text_row(box, 4, "ASR provider", self.asr_provider)
        self._text_row(box, 5, "ASR model", self.asr_model)
        self._text_row(box, 6, "ASR 语言", self.asr_language)

    def _build_toggle_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="记忆 / 群功能 / 外部事件", padding=8)
        box.pack(fill=tk.X, pady=(8, 0))
        self._toggle_box = box
        self.angel_memory = tk.BooleanVar()
        self.self_learning = tk.BooleanVar()
        self.group_analysis = tk.BooleanVar()
        self.group_services = tk.BooleanVar()
        self.atrinis_enabled = tk.BooleanVar()
        for row, (text, variable) in enumerate(
            (
                ("长期记忆（angel_memory）", self.angel_memory),
                ("自学习（表达/黑话）", self.self_learning),
                ("群分析（group_analysis）", self.group_analysis),
                ("群提醒/投票/报名（group_services）", self.group_services),
                ("外部事件入口（atrinis.toml）", self.atrinis_enabled),
            )
        ):
            ttk.Checkbutton(box, text=text, variable=variable).grid(
                row=row, column=0, sticky="w", pady=1
            )
        sub = ttk.Frame(box)
        sub.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Button(sub, text="自学习设置…", command=self._open_self_learning).pack(side=tk.LEFT)
        ttk.Button(sub, text="HOOK 插件配置…", command=self._open_hooks).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    def _build_maintenance_box(self, parent: Any) -> None:
        box = ttk.LabelFrame(parent, text="定时维护任务（app.toml [maintenance.*]）", padding=8)
        box.pack(fill=tk.X, pady=(8, 0))
        self._maintenance_box = box
        self.maintenance_summary = ttk.Label(
            box, text="", foreground="#555555", wraplength=300, justify=tk.LEFT
        )
        self.maintenance_summary.pack(anchor="w")
        ttk.Button(box, text="定时维护任务…", command=self._open_maintenance).pack(
            anchor="w", pady=(8, 0)
        )
        ttk.Label(
            box,
            text="开关、cron 周期或指定日期、保留天数都在这个二级窗口里改。",
            foreground="#8a8a8a",
            wraplength=300,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 0))

    def _text_row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, width=24).grid(
            row=row, column=1, sticky="ew", pady=2
        )

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if not self.is_ready():
            return
        paths = self.require_paths()
        try:
            app = read_toml(paths.config_path)
            services = read_toml(paths.services_path) if paths.services_path else {}
            atrinis = read_toml(paths.atrinis_path) if paths.atrinis_path else {}
        except ConfigEditError as exc:
            self.fail(exc)
            return
        runtime = _section(app, "runtime")
        self.log_level_var.set(_text(runtime.get("log_level")) or "info")
        self.log_retention_var.set(str(runtime.get("log_retention_days") or 30))
        self._catalog = modelcatalog.load(paths.config_dir)
        image = _section(services, "image_generation")
        self.image_enabled.set(bool(image.get("enabled")))
        self.image_base_url.set(_text(image.get("base_url")))
        self.image_api_env.set(_text(image.get("api_key_env")))
        self._stored_image_model = _text(image.get("model"))
        self.image_model.set(self._image_label(self._stored_image_model))
        self.image_alias.set(self._catalog.alias_of(modelcatalog.IMAGE_KEY, self._stored_image_model))
        self.image_key_value.set("")
        self.image_key_entry.clear()
        self._refresh_image_models()
        vision = _section(services, "vision")
        self.vision_enabled.set(bool(vision.get("enabled")))
        self.vision_provider.set(_text(vision.get("provider")))
        self.vision_model.set(_text(vision.get("model")))
        asr = _section(services, "asr")
        self.asr_enabled.set(bool(asr.get("enabled")))
        self.asr_provider.set(_text(asr.get("provider")))
        self.asr_model.set(_text(asr.get("model")))
        self.asr_language.set(_text(asr.get("language")))
        self.angel_memory.set(_flag(app, "angel_memory"))
        self.self_learning.set(_flag(app, "self_learning"))
        self.group_analysis.set(_flag(app, "group_analysis"))
        self.group_services.set(_flag(app, "group_services"))
        self.atrinis_enabled.set(bool(atrinis.get("enabled")))
        self._refresh_maintenance_summary(app)
        self._refresh_image_state()
        self.notify("已载入进阶配置（改动需要重启后台服务）")

    def _refresh_image_state(self) -> None:
        if not self.is_ready():
            return
        state = secret_state(self.require_paths(), self.image_api_env.get().strip())
        self.image_state.configure(text="密钥：" + state.describe())

    # -- 生图模型目录 -------------------------------------------------------
    def _image_label(self, model: str) -> str:
        """模型名 → 下拉里显示的标签（有备注名时带上）。"""
        text = (model or "").strip()
        if not text:
            return ""
        return modelcatalog.display_label(
            text, self._catalog.alias_of(modelcatalog.IMAGE_KEY, text)
        )

    def _image_configured(self) -> list[str]:
        """模型下拉的候选来源：配置里存着的模型 + 当前下拉选中的模型。"""
        model = self._picked_image_model()
        return [item for item in (self._stored_image_model, model) if item]

    def _refresh_image_models(self) -> None:
        """刷新生图模型下拉候选（点箭头时也会调一次）。"""
        labels = self._catalog.labels(modelcatalog.IMAGE_KEY, self._image_configured())
        try:
            self.image_model_box.configure(values=labels)
        except (AttributeError, tk.TclError):  # pragma: no cover - 构建期/窗口销毁
            pass

    def _picked_image_model(self) -> str:
        """下拉里的显示名 → 真模型名（只用本地目录里已知的真模型名做参照）。"""
        names = self._catalog.names(modelcatalog.IMAGE_KEY, [self._stored_image_model])
        return modelcatalog.model_of_label(self.image_model.get(), names)

    def _image_model_picked(self, _event: Any = None) -> None:
        """下拉里换了模型：把该模型已有的备注名回填到备注输入框。"""
        model = self._picked_image_model()
        self.image_alias.set(self._catalog.alias_of(modelcatalog.IMAGE_KEY, model))

    def _fetch_image_models(self) -> None:
        """后台拉取生图 API 的 ``/models``（用生图自己的 Base URL 与密钥）。"""
        if not self.is_ready():
            return
        base_url = self.image_base_url.get().strip()
        if not base_url:
            self.fail("先在生图区填 Base URL（例如 https://relay.example.com/v1）")
            return
        api_key = secret_value(self.require_paths(), self.image_api_env.get().strip())
        self.notify("正在获取生图 API 的可用模型…")
        self.journal(f"获取生图模型目录 ← {base_url}")
        widgets.run_async(
            self.frame,
            lambda: modelcatalog.fetch_models(base_url, api_key),
            self._image_models_fetched,
        )

    def _image_models_fetched(self, models: Any, error: BaseException | None) -> None:
        if error is not None:
            self.journal(f"获取生图模型目录失败：{error}")
            self.fail(f"获取生图模型目录失败：{error}")
            return
        found = [str(item) for item in (models or [])]
        if not found:
            self.notify("生图 API 没有返回模型清单")
            return
        added = self._catalog.record(
            modelcatalog.IMAGE_KEY, found, source=self.image_base_url.get().strip()
        )
        try:
            self._catalog.save(self.require_paths().config_dir)
        except modelcatalog.CatalogError as exc:
            self.fail(exc)
            return
        self._refresh_image_models()
        if not self._stored_image_model:
            self._stored_image_model = found[0]
            self.image_model.set(self._image_label(found[0]))
            self._refresh_image_models()
        self.journal(f"获取生图模型目录成功：共 {len(found)} 个（新增 {len(added)}）")
        self.notify(f"已获取 {len(found)} 个生图模型（新增 {len(added)}）；下拉里可自由切换")

    def _save_image_alias(self) -> None:
        model = self._picked_image_model()
        if not model:
            self.notify("先在「模型」下拉里选一个生图模型")
            return
        try:
            self._catalog.set_alias(modelcatalog.IMAGE_KEY, model, self.image_alias.get())
            self._catalog.save(self.require_paths().config_dir)
        except modelcatalog.CatalogError as exc:
            self.fail(exc)
            return
        alias = self._catalog.alias_of(modelcatalog.IMAGE_KEY, model)
        self.image_model.set(self._image_label(model))
        self._refresh_image_models()
        self.journal(f"设置生图模型备注名：{model} → {alias or '（清除）'}")
        self.notify(f"{model} 的备注名：{alias}" if alias else f"已清除 {model} 的备注名")

    def _clear_image_alias(self) -> None:
        self.image_alias.set("")
        self._save_image_alias()

    # -- 二级设置窗口 -------------------------------------------------------
    def _open_self_learning(self) -> None:
        dialogs.open_self_learning(
            self.frame, self.paths, on_saved=self._refresh_self_learning_flag
        )

    def _refresh_self_learning_flag(self) -> None:
        """自学习窗口保存后，把「进阶」页的快捷开关同步成文件里的值。"""
        if not self.is_ready():
            return
        try:
            app = read_toml(self.require_paths().config_path)
        except ConfigEditError:
            return
        self.self_learning.set(_flag(app, "self_learning"))

    def _open_hooks(self) -> None:
        dialogs.open_hooks(self.frame, self.paths)

    def _open_maintenance(self) -> None:
        dialogs.open_maintenance(
            self.frame, self.paths, on_saved=self._refresh_maintenance_summary
        )

    def _refresh_maintenance_summary(self, app: Any = None) -> None:
        """刷新「定时维护任务」按钮上方的摘要。"""
        if app is None:
            if not self.is_ready():
                return
            try:
                app = read_toml(self.require_paths().config_path)
            except ConfigEditError as exc:
                self.maintenance_summary.configure(text=f"读取 app.toml 失败：{exc}")
                return
        maintenance = _section(app, "maintenance")
        enabled = [
            label
            for key, label in _MAINTENANCE_LABELS
            if bool(_section(maintenance, key).get("enabled"))
        ]
        if enabled:
            sample = "、".join(enabled[:4]) + ("…" if len(enabled) > 4 else "")
            self.maintenance_summary.configure(text=f"{len(enabled)} 个任务启用：{sample}")
        else:
            self.maintenance_summary.configure(text="当前没有启用的维护任务")

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            paths = self.require_paths()
            image_model = self._picked_image_model() or self._stored_image_model
            patch_section(
                paths.services_path,
                "image_generation",
                {
                    "enabled": bool(self.image_enabled.get()),
                    "base_url": self.image_base_url.get().strip() or None,
                    "api_key_env": self.image_api_env.get().strip() or None,
                    "model": image_model or None,
                },
            )
            patch_section(
                paths.services_path,
                "vision",
                {
                    "enabled": bool(self.vision_enabled.get()),
                    "provider": self.vision_provider.get().strip() or None,
                    "model": self.vision_model.get().strip() or None,
                },
            )
            patch_section(
                paths.services_path,
                "asr",
                {
                    "enabled": bool(self.asr_enabled.get()),
                    "provider": self.asr_provider.get().strip() or None,
                    "model": self.asr_model.get().strip() or None,
                    "language": self.asr_language.get().strip() or None,
                },
            )
            patch_section(paths.config_path, "runtime", self._runtime_values())
            for section, variable in (
                ("angel_memory", self.angel_memory),
                ("self_learning", self.self_learning),
                ("group_analysis", self.group_analysis),
                ("group_services", self.group_services),
            ):
                patch_section(paths.config_path, section, {"enabled": bool(variable.get())})
            patch_top_level(paths.atrinis_path, {"enabled": bool(self.atrinis_enabled.get())})
            secret_name = self.image_api_env.get().strip()
            secret_text = self.image_key_entry.get()
            if secret_name and secret_text:
                set_env_value(paths.env_path, secret_name, secret_text)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._stored_image_model = image_model
        self.image_key_entry.clear()
        self._refresh_image_state()
        self.journal("保存进阶配置（services.toml / app.toml / atrinis.toml）")
        self.notify("已保存 services.toml / app.toml / atrinis.toml；重启后台服务后生效")

    def _runtime_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        level = self.log_level_var.get().strip()
        if level:
            values["log_level"] = level
        raw = self.log_retention_var.get().strip()
        if raw:
            try:
                values["log_retention_days"] = int(raw)
            except ValueError as exc:
                raise ConfigEditError(f"日志保留天数必须是整数：{raw!r}") from exc
        return values


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _section(data: Any, key: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _flag(data: Any, key: str) -> bool:
    """读取 ``enabled``；未显式配置时按"默认开启"处理（与 featureview 一致）。"""
    value = _section(data, key).get("enabled")
    return True if value is None else bool(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
