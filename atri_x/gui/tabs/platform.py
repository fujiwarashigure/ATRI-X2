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

from atri_x.errors import InvalidError
from atri_x.gui import widgets
from atri_x.gui.configedit import (
    ConfigEditError,
    patch_section,
    read_toml,
    set_env_value,
    verify_config,
)
from atri_x.gui.configmodel import secret_state
from atri_x.gui.tabs.base import ConfigTab, confirm
from atri_x.platform.qq_onebot.backend import (
    LEGACY_DEFAULT_BACKEND,
    NEW_INSTALL_BACKEND,
    backend_choices,
    backend_label,
    resolve_protocol_backend,
)

__all__ = ["PlatformTab"]

#: 平台 id → 显示名。
_LABELS = {
    "qqonebot": "QQ（OneBot v11，SnowLuma / NapCat 等协议端）",
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
        # qqonebot 的「协议端」下拉要知道"这段是不是本次新装出来的"：
        # 段不存在 = 新装，预选 SnowLuma；段存在 = 老配置，显示解析后的生效值。
        raw_onebot = platform.get("qqonebot")
        self.qqonebot.load(
            raw_onebot if isinstance(raw_onebot, dict) else {},
            section_exists=isinstance(raw_onebot, dict),
        )
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
        previous_backend = self.qqonebot.loaded_backend()
        backend = self.qqonebot.selected_backend()
        if backend != previous_backend and not confirm(
            self.frame,
            "切换协议端",
            f"将把协议端从 {backend_label(previous_backend)} 切换到 {backend_label(backend)}。\n\n"
            "切换只改 app.toml 里的 protocol_backend，不会改 WS 地址与令牌，也不会启动/停止任何进程。\n"
            "正确顺序：先在「在线」页断开 → 停止旧协议端（两套不能同时接管同一个 QQ）→ "
            "改下拉并保存 → 重启桌面端（协议端环境在启动时准备，内置协议端尤其如此）→ "
            "回「在线」页启动新协议端。\n\n继续保存吗？",
        ):
            return
        try:
            paths = self.require_paths()
            backend = self.qqonebot.selected_backend()
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
            # 回读两道：整份配置严格解码能过 + 刚写的协议端值确实是写进去的那个。
            verify_config(paths)
            self._verify_backend(paths, backend)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        for block in (self.qqonebot, self.qqofficial, self.telegram):
            block.clear_secret()
            block.refresh_key_state(paths)
        self.journal("保存平台配置（app.toml [platform.*] + .env）")
        self.notify("已保存 app.toml（令牌写入 .env）；重启后台服务后生效")

    @staticmethod
    def _verify_backend(paths: Any, expected: str) -> None:
        """回读断言：``[platform.qqonebot].protocol_backend`` 必须等于刚选的值。

        ``patch_section`` 只保证"写进去的是合法 TOML"，不保证"写对了值和位置"；
        这条断言是切换协议端时的最后一道保险（写错键名不会报错、只会静默丢键）。
        """
        try:
            data = read_toml(paths.config_path)
        except ConfigEditError as exc:
            raise ConfigEditError(f"回读协议端失败：{exc}") from exc
        platform = data.get("platform")
        section = platform.get("qqonebot") if isinstance(platform, dict) else None
        actual = None
        if isinstance(section, dict):
            actual = section.get("protocol_backend")
        if actual != expected:
            raise ConfigEditError(
                "[platform.qqonebot].protocol_backend 回读不一致："
                f"写入 {expected!r}，读到 {actual!r}"
            )


# ---------------------------------------------------------------------------
# 各平台表单
# ---------------------------------------------------------------------------
class _OneBotBox:
    """``[platform.qqonebot]``。"""

    def __init__(self, parent: Any) -> None:
        self.frame = ttk.LabelFrame(parent, text=_LABELS["qqonebot"], padding=8)
        self.enabled = tk.BooleanVar()
        self.backend = tk.StringVar()
        self.url = tk.StringVar()
        self.token_env = tk.StringVar()
        self.token_value = tk.StringVar()
        self.keywords = tk.StringVar()
        self._choices = backend_choices()
        self._label_to_value = {label: value for value, label in self._choices}
        #: 载入时的生效后端：保存时用它判断"用户是不是真的要切换协议端"。
        self._loaded_backend = resolve_protocol_backend("")
        ttk.Checkbutton(self.frame, text="启用", variable=self.enabled).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(self.frame, text="协议端").grid(row=1, column=0, sticky="w", pady=2)
        self.backend_combo = ttk.Combobox(
            self.frame,
            textvariable=self.backend,
            values=[label for _value, label in self._choices],
            state="readonly",
            width=24,
        )
        self.backend_combo.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=2)
        self._row(2, "协议端 WS 地址", self.url)
        self._row(3, "access_token 环境变量", self.token_env)
        ttk.Label(self.frame, text="token 值").grid(row=4, column=0, sticky="w", pady=2)
        self.token_entry = widgets.SecretEntry(self.frame, self.token_value, width=26)
        self.token_entry.grid(row=4, column=1, sticky="ew", padx=(6, 0), pady=2)
        self._row(5, "唤醒词（逗号分隔）", self.keywords)
        ttk.Label(
            self.frame,
            text="新装默认 SnowLuma（外置接入：需自行安装 SnowLuma，发行包不内置）；"
            "本程序包若内置 NapCat，可在上面直接切到 NapCat 使用。"
            "协议端只决定由哪一套接管 QQ：换它不会改上面的 WS 地址与令牌；"
            "两套不能同时接管同一个 QQ（先停旧的再起新的）。改完重启桌面端生效。",
            foreground="#8a8a8a",
            wraplength=380,
            justify=tk.LEFT,
        ).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(
            self.frame,
            text="群里 @机器人、或消息以唤醒词开头（如「atri 你好」）都会唤醒；"
            "留空 = 只认 @ 与回复。改完重启后台服务生效（也可在「在线」页直接改）。",
            foreground="#8a8a8a",
            wraplength=380,
            justify=tk.LEFT,
        ).grid(row=7, column=0, columnspan=2, sticky="w")
        self.state_label = ttk.Label(self.frame, text="", foreground="#555555")
        self.state_label.grid(row=8, column=0, columnspan=2, sticky="w")
        self.frame.columnconfigure(1, weight=1)

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self.frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(self.frame, textvariable=variable, width=26).grid(
            row=row, column=1, sticky="ew", padx=(6, 0), pady=2
        )

    def load(self, entry: dict[str, Any], *, section_exists: bool = True) -> None:
        self.enabled.set(bool(entry.get("enabled")))
        effective = _display_backend(entry.get("protocol_backend"), section_exists)
        self._loaded_backend = resolve_protocol_backend(effective)
        self.backend.set(backend_label(effective))
        self.url.set(_text(entry.get("ws_url")) or _text(entry.get("url")))
        self.token_env.set(_text(entry.get("access_token_env")))
        self.keywords.set(_join(entry.get("trigger_keywords")))
        self.token_entry.clear()

    def loaded_backend(self) -> str:
        """载入时生效的后端（判断是否真的发生了切换）。"""
        return self._loaded_backend

    def selected_backend(self) -> str:
        """下拉框当前选中的后端取值（永远是显式字符串，空值按旧配置解析）。"""
        value = self._label_to_value.get(self.backend.get().strip(), "")
        return value or resolve_protocol_backend("")

    def values(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled.get()),
            # 永远写显式字符串：patch_section 里 None = 删键，"未配置"会在下次读取时
            # 被重新解释成默认值，用户的选择就丢了。
            "protocol_backend": self.selected_backend(),
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


def _display_backend(raw: Any, section_exists: bool) -> str:
    """协议端下拉该显示的**生效值**。

    * 段原本不存在 = 新装：预选 :data:`NEW_INSTALL_BACKEND`（默认 NapCat，随包内置）；
      SnowLuma 只有用户在这里显式选过之后才会生效；
    * 段存在 = 老配置：显示解析后的生效值（键缺失 → ``napcat``）；
    * 手改出来的非法值不该让整页打不开：按默认值显示，保存时覆盖成合法值。
    """
    if not section_exists:
        return NEW_INSTALL_BACKEND
    try:
        return resolve_protocol_backend(raw)
    except InvalidError:
        return LEGACY_DEFAULT_BACKEND


def _state_text(state: Any) -> str:
    """密钥状态文案；没填环境变量名时别说成"未配置 api_key_env"。"""
    return state.describe() if getattr(state, "name", "") else "未设置环境变量名"


def _join(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return _text(value)


def _split(text: str) -> list[str]:
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]
