"""「工具权限 → 工具进度预览」子选项卡（Python 侧新增，无 Go 对应）。

这一页把"工具调用进度提示"的两类设置放在一起：

1. **群聊投递开关**：``[platform.qqonebot].show_tool_preview_in_group``（QQ 官方
   机器人同名字段在 ``[platform.qqofficial]``，只有该段已存在时才一起写，避免
   ``patch_section`` 新建出一个启用中的平台段）；
2. **程序兜底文案**：``app.toml`` 的 ``[tool_preview]`` —— 文案风格
   （``persona`` 角色化短句 / ``system`` 旧式"正在调用 工具名：参数"）、搜索 / 读取
   网页 / 默认文案、system 模板与失败模板。

写盘统一走 :func:`atri_x.gui.configedit.patch_section`（只改这些键、保留注释），
保存后再用 :func:`atri_x.config.load` 回读一次做 schema 校验：配置层是**严格解码**，
写出一个它不认识的键会让后台服务起不来，界面必须先自己拦住。

生效方式：``app.toml`` 改动需要重启后台服务（页面按钮栏里有「重启后台服务」）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.config.app_sections import ToolPreviewConfig
from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml, verify_config

__all__ = [
    "PREVIEW_TAB_TITLE",
    "PLATFORM_PREVIEW_SECTIONS",
    "ToolPreviewPanel",
    "load_tool_preview_config",
    "read_platform_preview_switch",
    "save_platform_preview_switch",
    "save_tool_preview_config",
]

PREVIEW_TAB_TITLE = " 工具进度预览 "

#: 会写 ``show_tool_preview_in_group`` 的平台段（只写"已存在"的段）。
PLATFORM_PREVIEW_SECTIONS: tuple[str, ...] = ("platform.qqonebot", "platform.qqofficial")

#: ``[tool_preview]`` 的界面字段：``(键, 标签, 说明)``。
_PANEL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("search_text", "检索文案", "web_search / search_chat_history 的兜底提示；不展示查询词。"),
    ("extract_text", "读取网页文案", "web_extract 的兜底提示。"),
    ("default_text", "默认文案", "其它工具的兜底提示；可用 {tool} 占位。"),
    ("system_template", "system 风格模板", "只能 {tool} / {args}；参数默认按 160 字符截断。"),
    ("failure_template", "失败文案模板", "工具执行失败时的一行提示；可用 {tool} / {error}。"),
)


def read_platform_preview_switch(app_path: str, section: str) -> bool:
    """读某个平台段的 ``show_tool_preview_in_group``（缺省 False）。"""
    if not app_path:
        return False
    try:
        data = read_toml(app_path)
    except ConfigEditError:
        return False
    node: Any = data
    for part in section.split("."):
        if not isinstance(node, dict):
            return False
        node = node.get(part)
    return bool(node.get("show_tool_preview_in_group")) if isinstance(node, dict) else False


def load_tool_preview_config(paths: Any) -> dict[str, Any]:
    """读 ``[tool_preview]`` 段（缺字段用 :class:`ToolPreviewConfig` 默认值）。"""
    raw: dict[str, Any] = {}
    try:
        data = read_toml(paths.config_path)
    except ConfigEditError:
        data = {}
    section = data.get("tool_preview")
    if isinstance(section, dict):
        raw = {str(key): value for key, value in section.items()}
    config = ToolPreviewConfig()
    for name in ToolPreviewConfig.__dataclass_fields__:
        if name in raw:
            setattr(config, name, raw[name])
    normalized = config.normalized()
    return {name: getattr(normalized, name) for name in normalized.__dataclass_fields__}


def save_tool_preview_config(paths: Any, values: dict[str, Any]) -> None:
    """把 ``[tool_preview]`` 写回 app.toml，并回读校验。"""
    patch_section(
        paths.config_path,
        "tool_preview",
        dict(values),
        header_comment="工具进度预览（桌面端工具权限 → 工具进度预览）",
    )
    _verify_config(paths)


def save_platform_preview_switch(
    paths: Any, states: bool | dict[str, bool]
) -> list[str]:
    """写群聊进度开关；返回实际写入的平台段名列表。

    ``states`` 可以是单个 bool（两个平台同值，旧调用兼容），也可以是
    ``{section: bool}``（面板上两个复选框各自的值）。
    """
    try:
        data = read_toml(paths.config_path)
    except ConfigEditError as exc:
        raise ConfigEditError(str(exc)) from exc
    if isinstance(states, bool):
        values = {section: states for section in PLATFORM_PREVIEW_SECTIONS}
    else:
        values = dict(states)
    written: list[str] = []
    for section in PLATFORM_PREVIEW_SECTIONS:
        node: Any = data
        exists = True
        for part in section.split("."):
            if not isinstance(node, dict) or not isinstance(node.get(part), dict):
                exists = False
                break
            node = node[part]
        if section == "platform.qqonebot" or exists:
            patch_section(
                paths.config_path,
                section,
                {"show_tool_preview_in_group": bool(values.get(section, False))},
                header_comment="群聊工具进度（桌面端工具权限页）",
            )
            written.append(section)
    _verify_config(paths)
    return written


def _verify_config(paths: Any) -> None:
    """保存后回读整份配置：严格解码能过，后台服务才起得来。"""
    verify_config(paths)


class ToolPreviewPanel:
    """「工具进度预览」子选项卡的内容（由 :class:`ToolPermTab` 持有）。"""

    def __init__(self, page: Any, parent: Any) -> None:
        self.page = page
        self.frame = ttk.Frame(parent, padding=10)
        self.enabled_var = tk.BooleanVar(value=True)
        self.style_var = tk.StringVar(value="persona")
        self.field_vars: dict[str, tk.StringVar] = {
            key: tk.StringVar() for key, _label, _help in _PANEL_FIELDS
        }
        self.switch_vars: dict[str, tk.BooleanVar] = {
            section: tk.BooleanVar(value=False) for section in PLATFORM_PREVIEW_SECTIONS
        }
        self._build()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        intro = ttk.Label(
            self.frame,
            text=(
                "程序兜底提示：只在模型这一轮没有先说话时发送（模型自己说的内容优先）。"
                "群聊里还要打开下面的群聊开关才会投递；改完点保存，重启后台服务后生效。"
            ),
            foreground="#555555",
            wraplength=760,
            justify=tk.LEFT,
        )
        intro.pack(anchor="w", pady=(0, 8))

        switch_box = ttk.LabelFrame(self.frame, text="群聊投递", padding=8)
        switch_box.pack(fill=tk.X)
        qq_row = ttk.Frame(switch_box)
        qq_row.pack(fill=tk.X)
        ttk.Checkbutton(
            qq_row,
            text="QQ OneBot：[platform.qqonebot].show_tool_preview_in_group",
            variable=self.switch_vars["platform.qqonebot"],
        ).pack(side=tk.LEFT)
        official_row = ttk.Frame(switch_box)
        official_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Checkbutton(
            official_row,
            text="QQ 官方机器人：[platform.qqofficial]（该段存在时才写入）",
            variable=self.switch_vars["platform.qqofficial"],
        ).pack(side=tk.LEFT)

        policy_box = ttk.LabelFrame(self.frame, text="兜底文案（app.toml [tool_preview]）", padding=8)
        policy_box.pack(fill=tk.X, pady=(8, 0))
        top = ttk.Frame(policy_box)
        top.pack(fill=tk.X)
        ttk.Checkbutton(top, text="启用程序兜底提示", variable=self.enabled_var).pack(side=tk.LEFT)
        ttk.Label(top, text="   文案风格").pack(side=tk.LEFT)
        ttk.Combobox(
            top,
            textvariable=self.style_var,
            values=("persona", "system"),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            top,
            text="persona = 角色化短句且不显示工具参数；system = 旧式「正在调用 工具名：参数」",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(8, 0))

        grid = ttk.Frame(policy_box)
        grid.pack(fill=tk.X, pady=(6, 0))
        grid.columnconfigure(1, weight=1)
        for row, (key, label, helper) in enumerate(_PANEL_FIELDS):
            ttk.Label(grid, text=label, width=14).grid(row=row * 2, column=0, sticky="w")
            ttk.Entry(grid, textvariable=self.field_vars[key]).grid(
                row=row * 2, column=1, sticky="ew", pady=(2, 0)
            )
            ttk.Label(grid, text=helper, foreground="#8a8a8a").grid(
                row=row * 2 + 1, column=1, sticky="w"
            )

        buttons = ttk.Frame(self.frame)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="保存进度预览设置", command=self.save).pack(side=tk.LEFT)
        ttk.Button(buttons, text="重新载入", command=self.load).pack(side=tk.LEFT, padx=(6, 0))

    # -- 数据 ---------------------------------------------------------------
    def load(self) -> None:
        paths = self.page.require_paths()
        values = load_tool_preview_config(paths)
        self.enabled_var.set(bool(values.get("enabled", True)))
        style = str(values.get("style", "persona") or "persona")
        self.style_var.set(style if style in ("persona", "system") else "persona")
        for key, _label, _help in _PANEL_FIELDS:
            self.field_vars[key].set(str(values.get(key, "") or ""))
        for section in PLATFORM_PREVIEW_SECTIONS:
            self.switch_vars[section].set(
                read_platform_preview_switch(paths.config_path, section)
            )

    def save(self) -> bool:
        values: dict[str, Any] = {
            "enabled": bool(self.enabled_var.get()),
            "style": str(self.style_var.get() or "persona"),
        }
        for key, _label, _help in _PANEL_FIELDS:
            values[key] = str(self.field_vars[key].get() or "").strip()
        try:
            paths = self.page.require_paths()
            save_tool_preview_config(paths, values)
            written = save_platform_preview_switch(
                paths,
                {section: bool(var.get()) for section, var in self.switch_vars.items()},
            )
        except ConfigEditError as exc:
            self.page.fail(exc)
            return False
        self.page.journal(
            "保存工具进度预览：style=%s enabled=%s platforms=%s"
            % (values["style"], values["enabled"], ",".join(written))
        )
        self.page.notify(
            "已保存工具进度预览（[tool_preview] + "
            + "、".join(written)
            + "）；重启后台服务后生效"
        )
        self.load()
        return True
