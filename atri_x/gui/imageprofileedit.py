"""「提示词」页的「生图档案」子页：编辑 ``services.toml`` 的命名生图 profile。

背景：``[image_generation.profiles.<名字>]`` 是 ``@image:<名字>`` 用的命名档
（可覆盖模型 / 尺寸 / 预设提示词 / 负向提示词 / 端点等），以前只能手写 TOML。
本面板直接读写 ``services.toml`` 的对应小节，其它键与注释原样保留
（:func:`atri_x.gui.configedit.patch_section` / :func:`...remove_section`）。

生效：生图 profile 在后台启动时装配，改完点「保存」后**需要重启后台服务**。
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import (
    ConfigEditError,
    remove_section,
    patch_section,
    read_toml,
    verify_config,
)

__all__ = ["ImageProfilesPanel"]

#: profile 名允许的字符（写进 TOML 小节名，点号会造成嵌套歧义）。
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

#: 单个 profile 可编辑的字段：``(键, 界面标签, 类型)``。
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("model", "模型（留空继承全局）", "str"),
    ("size", "尺寸（如 1024x1024）", "str"),
    ("quality", "质量（low/medium/high）", "str"),
    ("output_format", "输出格式（png/jpeg/webp）", "str"),
    ("base_url", "Base URL（留空继承）", "str"),
    ("api_key_env", "密钥环境变量名（写在 .env）", "str"),
    ("model_family", "模型族（general/sdxl/nai/flux）", "str"),
    ("prompt_dialect", "提示词方言（natural/sd_weighted/nai/tags）", "str"),
    ("negative_prompt_mode", "负向模式（field/inline/off）", "str"),
    ("aliases", "别名（逗号分隔，支持中文）", "list"),
    ("superadmin_only", "仅超级管理员可用", "tristate"),
)

#: 多行文本字段。
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("preset_prompt", "预设提示词（拼在最终 prompt 最前面）"),
    ("negative_prompt", "负向提示词（留空继承全局）"),
)


class ImageProfilesPanel:
    """命名生图 profile 的增删改（services.toml）。"""

    def __init__(self, parent: Any, paths: Any, controller: Any) -> None:
        self.paths = paths
        self.controller = controller
        self.frame = ttk.Frame(parent, padding=(8, 6))
        self._profiles: dict[str, dict[str, Any]] = {}
        self._current = ""
        self._deleted: set[str] = set()
        self._entries: dict[str, Any] = {}
        self._texts: dict[str, tk.Text] = {}
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(0, weight=1)
        left = ttk.Frame(self.frame)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", height=16)
        self.tree.grid(row=0, column=0, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_select())
        actions = ttk.Frame(left)
        actions.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(actions, text="新增档案", command=self._new_profile).pack(side=tk.LEFT)
        ttk.Button(actions, text="删除档案", command=self._delete_profile).pack(
            side=tk.LEFT, padx=(4, 0)
        )

        right = ttk.Frame(self.frame)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        ttk.Label(
            right,
            text=(
                "激活方式：聊天里发 @image:<名字>（仅本轮生效）。"
                "档案在后台启动时装配，保存后需要重启后台服务。"
            ),
            foreground="#8a8a8a",
            wraplength=780,
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        row = 1
        for key, label, kind in FIELDS:
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", pady=2)
            if kind == "tristate":
                variable = tk.StringVar(value="（默认）")
                ttk.Combobox(
                    right,
                    textvariable=variable,
                    values=["（默认）", "开", "关"],
                    state="readonly",
                    width=12,
                ).grid(row=row, column=1, sticky="w", pady=2)
            else:
                variable = tk.StringVar()
                ttk.Entry(right, textvariable=variable).grid(
                    row=row, column=1, sticky="ew", pady=2
                )
            self._entries[key] = variable
            row += 1
        for key, label in TEXT_FIELDS:
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="nw", pady=2)
            widget = tk.Text(right, wrap=tk.WORD, height=4, font=("Microsoft YaHei UI", 9))
            widget.grid(row=row, column=1, sticky="ew", pady=2)
            self._texts[key] = widget
            row += 1

        bottom = ttk.Frame(self.frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(bottom, text="保存生图档案", command=self.save).pack(side=tk.LEFT)
        ttk.Button(bottom, text="重新载入", command=self.reload).pack(
            side=tk.LEFT, padx=(6, 0)
        )

    # -- 读 ---------------------------------------------------------------
    def reload(self) -> None:
        self._deleted.clear()
        if self.paths is None:
            self._notify("找不到配置文件，无法读取生图档案")
            return
        try:
            data = read_toml(self.paths.services_path)
        except ConfigEditError as exc:
            self._fail(exc)
            return
        section = data.get("image_generation")
        section = section if isinstance(section, dict) else {}
        profiles = section.get("profiles")
        profiles = profiles if isinstance(profiles, dict) else {}
        self._profiles = {
            str(name): dict(value) if isinstance(value, dict) else {}
            for name, value in profiles.items()
        }
        self._refill()
        if self._profiles:
            self._select(next(iter(self._profiles)))
        else:
            self._clear_form()

    def _refill(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        for name in self._profiles:
            self.tree.insert("", tk.END, iid=name, text=name)

    def _on_select(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        self._capture()
        self._select(selected[0])

    def _select(self, name: str) -> None:
        if name not in self._profiles:
            return
        self._current = name
        profile = self._profiles[name]
        for key, label, kind in FIELDS:
            variable = self._entries[key]
            value = profile.get(key)
            if kind == "tristate":
                variable.set("开" if value is True else "关" if value is False else "（默认）")
            elif kind == "list":
                items = value if isinstance(value, list) else []
                variable.set(", ".join(str(item) for item in items))
            else:
                variable.set("" if value is None else str(value))
        for key, _label in TEXT_FIELDS:
            widget = self._texts[key]
            value = profile.get(key)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", "" if value is None else str(value))

    def _clear_form(self) -> None:
        self._current = ""
        for key, _label, kind in FIELDS:
            self._entries[key].set("（默认）" if kind == "tristate" else "")
        for key, _label in TEXT_FIELDS:
            self._texts[key].delete("1.0", tk.END)

    def _capture(self) -> None:
        name = self._current
        if not name or name in self._deleted:
            return
        profile = self._profiles.get(name)
        if profile is None:
            return
        for key, _label, kind in FIELDS:
            raw = self._entries[key].get().strip()
            if kind == "tristate":
                if raw == "开":
                    profile[key] = True
                elif raw == "关":
                    profile[key] = False
                else:
                    profile.pop(key, None)
            elif kind == "list":
                items = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
                if items:
                    profile[key] = items
                else:
                    profile.pop(key, None)
            elif raw:
                profile[key] = raw
            else:
                profile.pop(key, None)
        for key, _label in TEXT_FIELDS:
            value = self._texts[key].get("1.0", "end-1c")
            if value.strip():
                profile[key] = value
            else:
                profile.pop(key, None)

    # -- 操作 -------------------------------------------------------------
    def _new_profile(self) -> None:
        self._capture()
        index = 1
        while f"profile{index}" in self._profiles:
            index += 1
        name = _ask_text(self.frame, "新增生图档案", "档案名（字母/数字/_/-）：", f"profile{index}")
        if not name:
            return
        if not _NAME_PATTERN.match(name):
            self._fail("档案名只允许字母、数字、下划线和连字符（1–40 个字符）")
            return
        if name in self._profiles and name not in self._deleted:
            self._fail(f"档案已存在：{name}")
            return
        self._profiles[name] = {}
        self._deleted.discard(name)
        self._refill()
        self._select(name)
        self._notify("已新增档案；填好字段后点「保存生图档案」")

    def _delete_profile(self) -> None:
        self._capture()
        name = self._current
        if not name:
            return
        if not self.controller.confirm(f"删除生图档案 {name}？保存后从 services.toml 移除。"):
            return
        self._profiles.pop(name, None)
        self._deleted.add(name)
        self._current = ""
        self._refill()
        if self._profiles:
            self._select(next(iter(self._profiles)))
        else:
            self._clear_form()
        self._notify("已标记删除；点「保存生图档案」后落盘")

    def save(self) -> None:
        if self.paths is None:
            self._fail("找不到配置文件，无法保存")
            return
        self._capture()
        path = self.paths.services_path
        try:
            for name in self._deleted:
                remove_section(path, "image_generation.profiles." + name)
            for name, profile in self._profiles.items():
                if not _NAME_PATTERN.match(name):
                    self._fail(f"档案名不合法：{name!r}")
                    return
                values: dict[str, Any] = {}
                for key, _label, kind in FIELDS:
                    if key not in profile:
                        # 清空字段 = 删掉该键（继承全局），不是写空串。
                        values[key] = None
                        continue
                    value = profile[key]
                    if kind == "list":
                        values[key] = list(value)
                    else:
                        values[key] = value
                for key, _label in TEXT_FIELDS:
                    values[key] = profile.get(key)
                patch_section(path, "image_generation.profiles." + name, values)
            verify_config(self.paths)
        except ConfigEditError as exc:
            self._fail(exc)
            return
        self._deleted.clear()
        self._journal("保存生图档案（services.toml）")
        self._notify("已保存；重启后台服务后生效")

    # -- 控制器转发 --------------------------------------------------------
    def _notify(self, text: str) -> None:
        self.controller.notify(text)

    def _fail(self, exc: Any) -> None:
        self.controller.fail(exc)

    def _journal(self, text: str) -> None:
        self.controller.journal(text)


def _ask_text(parent: Any, title: str, prompt: str, initial: str = "") -> str:
    """简单的单行输入对话框（返回空串表示取消）。"""
    from tkinter import simpledialog

    return simpledialog.askstring(title, prompt, initialvalue=initial, parent=parent) or ""
