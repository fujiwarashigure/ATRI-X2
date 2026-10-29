"""「权限」选项卡：超级管理员名单 + 风险等级 / 工具轮次 / 成员建卡开关。

这个页是 1.1.3 修订新增的：此前只有「平台」页底部一小块「超级管理员」输入框，
用户反馈"不知道超管在谁手里、QQ 号往哪填"，而且普通成员建卡、``max_rounds_per_turn``
都要手改 ``app.toml``。这里把它们收成一张可读的表：

* **超级管理员**（``[security.superadmins]``）：每个平台一行，逗号分隔的**平台用户
  id**。QQ 填 QQ 号（如 ``10001``），Telegram 填数字 user id，本地 CLI 是
  ``local``。群主/群管理员**不会**自动成为超管，必须写在这里。
* **权限与限制**：
  - ``[security].user_max_tool_risk``：普通成员能直接用的最高工具风险；
  - ``[security].superadmin_confirm_risk``：超管也要先确认的风险阈值；
  - ``[tools].max_rounds_per_turn``：单轮最多工具轮次，撞上限时模型只能总结、不能继续；
  - ``[character_library].allow_user_create``：普通成员是否允许建人设卡。

全部是启动期 / 构造期配置：保存后要点「重启后台服务」或菜单「服务 → 重启后台服务」。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml, verify_config
from atri_x.gui.tabs.base import ConfigTab

__all__ = ["PermissionTab"]

#: 平台 id → 界面显示名。
_PLATFORM_LABELS = {
    "cli": "本地 CLI（桌面端「对话」/ 本地 TUI；用户 id 固定是 local）",
    "qqonebot": "QQ（OneBot v11；填 QQ 号，如 10001）",
    "qqofficial": "QQ 官方机器人（开放平台用户 id）",
    "telegram": "Telegram（数字 user id）",
}

#: 风险等级下拉项。
_RISKS = ("safe", "low", "medium", "high", "critical")

#: 单轮工具轮次的合理范围。
_MAX_ROUNDS_MIN = 1
_MAX_ROUNDS_MAX = 50

#: 缺省值（与 :mod:`atri_x.config.app` 的默认一致）。
_DEFAULT_MAX_ROUNDS = 2


class PermissionTab(ConfigTab):
    """超级管理员 + 权限/限制编辑页。"""

    hint = "改完需要重启后台服务"

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        ttk.Label(
            self.body,
            text=(
                "超级管理员可以建/改公开人设卡、把角色设为全局人设、使用高风险工具、"
                "替成员设置专属人设。QQ 群主/群管理员不会自动成为超管。"
            ),
            foreground="#555555",
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        admins = ttk.LabelFrame(self.body, text="超级管理员（[security.superadmins]）", padding=8)
        admins.pack(fill=tk.X)
        self._admin_vars: dict[str, tk.StringVar] = {}
        for row, platform in enumerate(_PLATFORM_LABELS):
            ttk.Label(admins, text=_PLATFORM_LABELS[platform], width=34).grid(
                row=row, column=0, sticky="w", pady=2
            )
            variable = tk.StringVar()
            entry = ttk.Entry(admins, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            self._admin_vars[platform] = variable
            ttk.Label(admins, text="逗号分隔", foreground="#8a8a8a").grid(
                row=row, column=2, sticky="w", padx=(6, 0)
            )
        admins.columnconfigure(1, weight=1)

        limits = ttk.LabelFrame(self.body, text="权限与工具限制", padding=8)
        limits.pack(fill=tk.X, pady=(8, 0))
        limits.columnconfigure(1, weight=1)
        self.user_risk_var = tk.StringVar()
        self.super_risk_var = tk.StringVar()
        self.max_rounds_var = tk.StringVar()
        self.allow_create_var = tk.BooleanVar(value=True)
        self._combo_row(limits, 0, "普通成员最高工具风险", self.user_risk_var)
        self._combo_row(limits, 1, "超管确认风险阈值", self.super_risk_var)
        ttk.Label(limits, text="单轮最多工具轮次", width=22).grid(
            row=2, column=0, sticky="w", pady=2
        )
        ttk.Spinbox(
            limits,
            from_=_MAX_ROUNDS_MIN,
            to=_MAX_ROUNDS_MAX,
            textvariable=self.max_rounds_var,
            width=8,
        ).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Checkbutton(
            limits,
            text="允许普通成员建人设卡（关闭后只有超级管理员能建新卡）",
            variable=self.allow_create_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(
            self.body,
            text=(
                "说明：\n"
                "· 超级管理员名单按平台分桶；“本地 CLI”里的 local 就是桌面端自己的对话身份，"
                "所以桌面端对话天然是超管。\n"
                "· 「单轮最多工具轮次」撞上限时，这一轮会强制结束并让模型总结，不再执行后续"
                "工具调用。导入人设这类一次工具就能完成的操作不会再撞上限。\n"
                "· 关掉「允许普通成员建卡」后，成员已建好的卡仍可自己修改；不能再新建或导入。"
            ),
            foreground="#8a8a8a",
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(8, 0))

        self.button("保存权限配置", self._save)
        self.button("重新载入", self.reload)

    def _combo_row(self, parent: Any, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, width=22).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(
            parent, textvariable=variable, values=list(_RISKS), state="readonly", width=12
        ).grid(row=row, column=1, sticky="w", pady=2)

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            data = read_toml(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        security = data.get("security")
        security = security if isinstance(security, dict) else {}
        superadmins = security.get("superadmins")
        superadmins = superadmins if isinstance(superadmins, dict) else {}
        for platform, variable in self._admin_vars.items():
            variable.set(_join(superadmins.get(platform)))
        self.user_risk_var.set(_text(security.get("user_max_tool_risk")) or "low")
        self.super_risk_var.set(_text(security.get("superadmin_confirm_risk")) or "high")
        tools = data.get("tools")
        tools = tools if isinstance(tools, dict) else {}
        rounds = tools.get("max_rounds_per_turn")
        self.max_rounds_var.set(str(rounds if isinstance(rounds, int) else _DEFAULT_MAX_ROUNDS))
        library = data.get("character_library")
        library = library if isinstance(library, dict) else {}
        self.allow_create_var.set(bool(library.get("allow_user_create", True)))
        self.notify("已载入权限配置（改完需要重启后台服务）")

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            paths = self.require_paths()
            # 空行写 None = 删掉该平台的键；否则写当前名单。
            superadmins: dict[str, Any] = {}
            for platform, variable in self._admin_vars.items():
                items = _split(variable.get())
                superadmins[platform] = items or None
            patch_section(paths.config_path, "security.superadmins", superadmins)
            patch_section(
                paths.config_path,
                "security",
                {
                    "user_max_tool_risk": _risk(self.user_risk_var.get(), "low"),
                    "superadmin_confirm_risk": _risk(self.super_risk_var.get(), "high"),
                },
            )
            patch_section(
                paths.config_path,
                "tools",
                {"max_rounds_per_turn": _rounds(self.max_rounds_var.get())},
            )
            patch_section(
                paths.config_path,
                "character_library",
                {"allow_user_create": bool(self.allow_create_var.get())},
            )
            verify_config(paths)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal("保存权限配置（[security.superadmins] / [security] / [tools] / [character_library]）")
        self.notify("已保存；重启后台服务后生效")
        self.reload()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _join(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return _text(value)


def _split(text: str) -> list[str]:
    return [item.strip() for item in str(text or "").replace("，", ",").split(",") if item.strip()]


def _risk(value: str, fallback: str) -> str:
    text = _text(value).lower()
    return text if text in _RISKS else fallback


def _rounds(value: str) -> int:
    try:
        number = int(str(value or "").strip())
    except ValueError:
        return _DEFAULT_MAX_ROUNDS
    return max(_MAX_ROUNDS_MIN, min(_MAX_ROUNDS_MAX, number))
