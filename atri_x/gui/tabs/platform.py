"""「平台」选项卡：QQ / Telegram / 本地 CLI 服务端 / 超级管理员。

平台配置在 ``app.toml`` 的 ``[platform.*]`` 里，属于**启动期配置**：改完要点底部的
「重启后台服务」（或用菜单「服务 → 重启后台服务」）。字段与
:mod:`atri_x.platform.qq_onebot.config` / ``telegram.config`` / ``qqofficial.config``
的 TOML 键一一对应；密钥同样只写配置根 ``.env``，界面只显示掩码。

这里只覆盖接机器人最常改的键（开关、地址、token 变量、触发词）；更细的参数
（去重、并发、文件发送模式等）仍写在配置文件里，界面不猜也不覆盖它们。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import widgets
from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml, set_env_value
from atri_x.gui.configmodel import secret_state
from atri_x.gui.tabs.base import ConfigTab

__all__ = ["PlatformTab"]

#: 平台 id → 显示名。
_LABELS = {
    "qqonebot": "QQ（OneBot v11，NapCat 等协议端）",
    "qqofficial": "QQ 官方机器人（开放平台）",
    "telegram": "Telegram Bot",
}

#: 超级管理员按平台配置：``[security.superadmins]``。
_SUPERADMIN_KEYS = ("cli", "qqonebot", "qqofficial", "telegram")


class PlatformTab(ConfigTab):
    """四个平台小节 + 超级管理员列表。"""

    hint = ""

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self._build()
        self.reload()

    # -- 构建 ---------------------------------------------------------------
    def _build(self) -> None:
        grid = ttk.Frame(self.body)
        grid.pack(fill=tk.BOTH, expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        self.qqonebot = _OneBotBox(grid)
        self.qqonebot.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self.qqofficial = _OfficialBox(grid)
        self.qqofficial.frame.grid(row=0, column=1, sticky="nsew", pady=(0, 6))
        self.telegram = _TelegramBox(grid)
        self.telegram.frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.cli = _CliBox(grid)
        self.cli.frame.grid(row=1, column=1, sticky="nsew")
        self.superadmins = _SuperadminBox(grid)
        self.superadmins.frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        self.button("保存平台配置", self._save)
        self.button("重新载入", self.reload)

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            data = read_toml(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        platform = data.get("platform")
        platform = platform if isinstance(platform, dict) else {}
        self._load_block(self.qqonebot, platform.get("qqonebot"))
        self._load_block(self.qqofficial, platform.get("qqofficial"))
        self._load_block(self.telegram, platform.get("telegram"))
        cli = platform.get("cli")
        cli = cli if isinstance(cli, dict) else {}
        server = cli.get("server")
        server = server if isinstance(server, dict) else {}
        self._load_block(self.cli, server)
        security = data.get("security")
        security = security if isinstance(security, dict) else {}
        superadmins = security.get("superadmins")
        self.superadmins.load(superadmins if isinstance(superadmins, dict) else {})
        for block in (self.qqonebot, self.qqofficial, self.telegram):
            block.refresh_key_state(self.require_paths())
        self.notify("已载入平台配置（改完需要重启后台服务）")

    def _load_block(self, block: Any, entry: Any) -> None:
        block.load(entry if isinstance(entry, dict) else {})

    # -- 保存 ---------------------------------------------------------------
    def _save(self) -> None:
        try:
            paths = self.require_paths()
            secrets = [
                (self.qqonebot.secret_env(), self.qqonebot.secret_value()),
                (self.qqofficial.secret_env(), self.qqofficial.secret_value()),
                (self.telegram.secret_env(), self.telegram.secret_value()),
            ]
            for section, values in (
                ("platform.qqonebot", self.qqonebot.values()),
                ("platform.qqofficial", self.qqofficial.values()),
                ("platform.telegram", self.telegram.values()),
                ("platform.cli.server", self.cli.values()),
            ):
                patch_section(paths.config_path, section, values)
            superadmins = self.superadmins.values()
            if superadmins:
                patch_section(paths.config_path, "security.superadmins", superadmins)
            for name, value in secrets:
                if name and value:
                    set_env_value(paths.env_path, name, value)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        for block in (self.qqonebot, self.qqofficial, self.telegram):
            block.clear_secret()
            block.refresh_key_state(paths)
        self.journal("保存平台配置（app.toml [platform.*] + .env）")
        self.notify("已保存 app.toml（令牌写入 .env）；重启后台服务后生效")


# ---------------------------------------------------------------------------
# 各平台表单
# ---------------------------------------------------------------------------
class _OneBotBox:
    """``[platform.qqonebot]``。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text=_LABELS["qqonebot"], padding=8)
        self.enabled = tk.BooleanVar()
        self.url = tk.StringVar()
        self.token_env = tk.StringVar()
        self.token_value = tk.StringVar()
        self.keywords = tk.StringVar()
        ttk.Checkbutton(self.frame, text="启用", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._row(1, "协议端 WS 地址", self.url)
        self._row(2, "access_token 环境变量", self.token_env)
        ttk.Label(self.frame, text="token 值").grid(row=3, column=0, sticky="w", pady=2)
        self.token_entry = widgets.SecretEntry(self.frame, self.token_value, width=26)
        self.token_entry.grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=2)
        self._row(4, "唤醒词（逗号分隔）", self.keywords)
        ttk.Label(
            self.frame,
            text="群里 @机器人、或消息以唤醒词开头（如「atri 你好」）都会唤醒；"
            "留空 = 只认 @ 与回复。改完重启后台服务生效（也可在「在线」页直接改）。",
            foreground="#8a8a8a",
            wraplength=380,
            justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=2, sticky="w")
        self.state_label = ttk.Label(self.frame, text="", foreground="#555555")
        self.state_label.grid(row=6, column=0, columnspan=2, sticky="w")
        self.frame.columnconfigure(1, weight=1)

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.frame, textvariable=variable, width=26).grid(
            row=row, column=1, sticky="ew", padx=(6, 0), pady=2
        )

    def load(self, entry: dict[str, Any]) -> None:
        self.enabled.set(bool(entry.get("enabled")))
        self.url.set(_text(entry.get("ws_url")) or _text(entry.get("url")))
        self.token_env.set(_text(entry.get("access_token_env")))
        self.keywords.set(_join(entry.get("trigger_keywords")))
        self.token_entry.clear()

    def values(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled.get()),
            "ws_url": self.url.get().strip() or None,
            "access_token_env": self.token_env.get().strip() or None,
            "trigger_keywords": _split(self.keywords.get()) or None,
        }

    def secret_env(self) -> str:
        return self.token_env.get().strip()

    def secret_value(self) -> str:
        return self.token_entry.get()

    def clear_secret(self) -> None:
        self.token_entry.clear()

    def refresh_key_state(self, paths: Any) -> None:
        state = secret_state(paths, self.secret_env())
        self.state_label.configure(text="token：" + _state_text(state))


class _OfficialBox:
    """``[platform.qqofficial]``。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text=_LABELS["qqofficial"], padding=8)
        self.enabled = tk.BooleanVar()
        self.app_id = tk.StringVar()
        self.secret_env_var = tk.StringVar()
        self.secret_text = tk.StringVar()
        ttk.Checkbutton(self.frame, text="启用", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._row(1, "AppID", self.app_id)
        self._row(2, "client_secret 环境变量", self.secret_env_var)
        ttk.Label(self.frame, text="secret 值").grid(row=3, column=0, sticky="w", pady=2)
        self.secret_entry = widgets.SecretEntry(self.frame, self.secret_text, width=26)
        self.secret_entry.grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=2)
        self.state_label = ttk.Label(self.frame, text="", foreground="#555555")
        self.state_label.grid(row=4, column=0, columnspan=2, sticky="w")
        self.frame.columnconfigure(1, weight=1)

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.frame, textvariable=variable, width=26).grid(
            row=row, column=1, sticky="ew", padx=(6, 0), pady=2
        )

    def load(self, entry: dict[str, Any]) -> None:
        self.enabled.set(bool(entry.get("enabled")))
        self.app_id.set(_text(entry.get("app_id")))
        self.secret_env_var.set(_text(entry.get("client_secret_env")))
        self.secret_entry.clear()

    def values(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled.get()),
            "app_id": self.app_id.get().strip() or None,
            "client_secret_env": self.secret_env_var.get().strip() or None,
        }

    def secret_env(self) -> str:
        return self.secret_env_var.get().strip()

    def secret_value(self) -> str:
        return self.secret_entry.get()

    def clear_secret(self) -> None:
        self.secret_entry.clear()

    def refresh_key_state(self, paths: Any) -> None:
        state = secret_state(paths, self.secret_env())
        self.state_label.configure(text="secret：" + _state_text(state))


class _TelegramBox:
    """``[platform.telegram]``。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text=_LABELS["telegram"], padding=8)
        self.enabled = tk.BooleanVar()
        self.token_env = tk.StringVar()
        self.token_value = tk.StringVar()
        self.proxy_env = tk.StringVar()
        ttk.Checkbutton(self.frame, text="启用", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._row(1, "bot_token 环境变量", self.token_env)
        ttk.Label(self.frame, text="token 值").grid(row=2, column=0, sticky="w", pady=2)
        self.token_entry = widgets.SecretEntry(self.frame, self.token_value, width=26)
        self.token_entry.grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=2)
        self._row(3, "代理环境变量（可选）", self.proxy_env)
        self.state_label = ttk.Label(self.frame, text="", foreground="#555555")
        self.state_label.grid(row=4, column=0, columnspan=2, sticky="w")
        self.frame.columnconfigure(1, weight=1)

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.frame, textvariable=variable, width=26).grid(
            row=row, column=1, sticky="ew", padx=(6, 0), pady=2
        )

    def load(self, entry: dict[str, Any]) -> None:
        self.enabled.set(bool(entry.get("enabled")))
        self.token_env.set(_text(entry.get("bot_token_env")))
        self.proxy_env.set(_text(entry.get("proxy_url_env")))
        self.token_entry.clear()

    def values(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled.get()),
            "bot_token_env": self.token_env.get().strip() or None,
            "proxy_url_env": self.proxy_env.get().strip() or None,
        }

    def secret_env(self) -> str:
        return self.token_env.get().strip()

    def secret_value(self) -> str:
        return self.token_entry.get()

    def clear_secret(self) -> None:
        self.token_entry.clear()

    def refresh_key_state(self, paths: Any) -> None:
        state = secret_state(paths, self.secret_env())
        self.state_label.configure(text="token：" + _state_text(state))


class _CliBox:
    """``[platform.cli.server]``（桌面端连的就是它）。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text="本地 CLI 服务端（桌面端 / 远程前端）", padding=8)
        self.enabled = tk.BooleanVar()
        self.listen = tk.StringVar()
        ttk.Checkbutton(self.frame, text="启用", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(self.frame, text="监听地址").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(self.frame, textvariable=self.listen, width=26).grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=2
        )
        self.frame.columnconfigure(1, weight=1)
        self.note = ttk.Label(
            self.frame,
            text="本机登录 token 由桌面端自动生成并写进 .env（ATRIX_CLI_LOCAL_TOKEN）。",
            foreground="#8a8a8a",
            wraplength=320,
        )
        self.note.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

    def load(self, entry: dict[str, Any]) -> None:
        self.enabled.set(bool(entry.get("enabled")))
        self.listen.set(_text(entry.get("listen")) or "127.0.0.1:32172")

    def values(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled.get()),
            "listen": self.listen.get().strip() or None,
        }


class _SuperadminBox:
    """``[security.superadmins]``：按平台列超级管理员 id。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text="超级管理员（每个平台一行，逗号分隔）", padding=8)
        self.vars: dict[str, tk.StringVar] = {}
        for row, key in enumerate(_SUPERADMIN_KEYS):
            ttk.Label(self.frame, text=key, width=12).grid(row=row, column=0, sticky="w", pady=1)
            variable = tk.StringVar()
            ttk.Entry(self.frame, textvariable=variable).grid(
                row=row, column=1, sticky="ew", pady=1
            )
            self.vars[key] = variable
        self.frame.columnconfigure(1, weight=1)

    def load(self, entry: dict[str, Any]) -> None:
        for key, variable in self.vars.items():
            variable.set(_join(entry.get(key)))

    def values(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, variable in self.vars.items():
            items = _split(variable.get())
            if items:
                payload[key] = items
        return payload


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _state_text(state: Any) -> str:
    """密钥状态文案；没填环境变量名时别说成"未配置 api_key_env"。"""
    return state.describe() if getattr(state, "name", "") else "未设置环境变量名"


def _join(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return _text(value)


def _split(text: str) -> list[str]:
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
