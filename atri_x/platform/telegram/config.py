"""Telegram 适配器配置与装配入口。

对应 Go：``internal/platform/telegram/config.go``。

命名差异（Python 里字段与方法不能同名，逐一对应）：

* Go ``Config.Format`` 字段 → 字段 :attr:`Config.format`；
  Go 的 ``Config.format()`` 方法 → :meth:`Config.message_format`。
* Go ``Config.ProxyURL`` 字段 → 字段 :attr:`Config.proxy_url`；
  Go 的 ``Config.proxyURL()`` 方法 → :meth:`Config.parsed_proxy_url`。

其他形式差异：

* ``url.URL`` → :class:`urllib.parse.SplitResult`；``time.Duration`` → 秒（``float``）。
* ``appconfig.ConfigEnv`` → :func:`atri_x.config.loader.config_env`：读 ``.env``
  失败时抛 :class:`atri_x.config.inspect.ConfigDecodeError`（Go 返回 error）。
* ``NewFromPlatformConfig`` 在 Go 里返回 ``*Adapter``；本模块用函数内延迟导入
  :mod:`atri_x.platform.telegram.adapter`，以免 ``config`` ↔ ``adapter`` 形成
  模块级循环导入（Go 无此问题）。
* 代理只声明校验：``httpclient`` 目前实现 ``http://`` 代理，``socks5://`` 会被
  解析并接受但不会被隧道化（见 PORTING 契约缺口报告）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import SplitResult, urlsplit

from atri_x.config.loader import config_env
from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.config import decode_config

#: 平台名。对应 Go ``platformName``。
PLATFORM_NAME = "telegram"

#: 默认端点与超时。对应 Go 的常量块。
DEFAULT_API_BASE_URL = "https://api.telegram.org"
DEFAULT_FILE_BASE_URL = "https://api.telegram.org/file"
DEFAULT_API_TIMEOUT = 15.0
DEFAULT_POLL_TIMEOUT = 30.0
DEFAULT_RECONNECT_INTERVAL = 3.0
DEFAULT_STREAM_EDIT_INTERVAL = 0.25
DEFAULT_FORMAT = "html"

#: 单条文本消息的 rune 上限（Telegram 的 4096）。对应 Go ``telegramTextPageRunes``。
TELEGRAM_TEXT_PAGE_RUNES = 4096
#: Rich Message 的 rune 上限。对应 Go ``telegramRichTextRunes``。
TELEGRAM_RICH_TEXT_RUNES = 32768


@dataclass
class Config:
    """``[platform.telegram]`` 小节。对应 Go ``Config``。"""

    enabled: bool = False
    bot_token: str = ""
    bot_token_env: str = ""
    proxy_url: str = ""
    proxy_url_env: str = ""
    api_base_url: str = ""
    file_base_url: str = ""
    api_timeout_seconds: int = 0
    poll_timeout_seconds: int = 0
    reconnect_interval_seconds: int = 0
    stream_edit_interval_milliseconds: int = 0
    format: str = ""
    trigger_keywords: list[str] = field(default_factory=list)
    # superadmins / command_prefixes / config_env_dir 来自平台外层配置（Go 的 ``toml:"-"``）。
    superadmins: list[str] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=list)
    config_env_dir: str = ""

    # -- 凭据 ---------------------------------------------------------------
    def token(self) -> str:
        """Bot Token；读取失败时返回空串（对应 Go ``token()`` 忽略 error）。"""
        try:
            return self.token_value()
        except AtriXError:
            return ""

    def token_value(self) -> str:
        """对应 Go ``tokenValue``：先看 ``bot_token``，再按 ``bot_token_env`` 取。"""
        value = (self.bot_token or "").strip()
        if value:
            return value
        return self.env_value(self.bot_token_env)

    # -- 代理 ---------------------------------------------------------------
    def parsed_proxy_url(self) -> SplitResult | None:
        """校验后的代理 URL；未配置时返回 ``None``。对应 Go ``proxyURL``。"""
        value = self.proxy_value()
        if value == "":
            return None
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise InvalidError(f"invalid telegram proxy_url: {exc}") from exc
        if not parsed.scheme or not parsed.hostname:
            raise InvalidError("invalid telegram proxy_url: missing scheme or host")
        return parsed

    def proxy_value(self) -> str:
        """对应 Go ``proxyValue``：``proxy_url_env`` 优先于 ``proxy_url``。"""
        value = self.env_value(self.proxy_url_env)
        if value != "":
            return value
        return (self.proxy_url or "").strip()

    def http_proxy(self) -> str:
        """交给 :class:`atri_x.net.httpclient.HTTPClient` 的代理字符串。

        Python 侧新增的取值口（Go 在 ``newHTTPClient`` 里直接消费 ``proxyURL()``）：
        未配置或非法时返回空串，与 Go 忽略 ``proxyURL()`` 错误的后果一致。
        """
        try:
            if self.parsed_proxy_url() is None:
                return ""
        except AtriXError:
            return ""
        return self.proxy_value()

    def env_value(self, name: str) -> str:
        """按环境变量名取值（系统环境变量优先，其次配置目录 ``.env``）。"""
        name = (name or "").strip()
        if name == "":
            return ""
        value = config_env(name, self.config_env_dir)
        if value is None:
            return ""
        return value.strip()

    # -- 时长 ---------------------------------------------------------------
    def api_timeout(self) -> float:
        """对应 Go ``apiTimeout``。"""
        seconds = float(self.api_timeout_seconds or 0)
        if seconds <= 0:
            return DEFAULT_API_TIMEOUT
        return seconds

    def poll_timeout(self) -> float:
        """对应 Go ``pollTimeout``（``getUpdates`` 的 long polling 时长）。"""
        seconds = float(self.poll_timeout_seconds or 0)
        if seconds <= 0:
            return DEFAULT_POLL_TIMEOUT
        return seconds

    def reconnect_interval(self) -> float:
        """对应 Go ``reconnectInterval``。"""
        seconds = float(self.reconnect_interval_seconds or 0)
        if seconds <= 0:
            return DEFAULT_RECONNECT_INTERVAL
        return seconds

    def stream_edit_interval(self) -> float:
        """对应 Go ``streamEditInterval``（流式编辑节流间隔，秒）。"""
        seconds = float(self.stream_edit_interval_milliseconds or 0) / 1000.0
        if seconds <= 0:
            return DEFAULT_STREAM_EDIT_INTERVAL
        return seconds

    # -- 格式 ---------------------------------------------------------------
    def message_format(self) -> str:
        """对应 Go ``Config.format()``。"""
        return normalize_telegram_format(self.format)

    def rich_message_enabled(self) -> bool:
        """对应 Go ``richMessageEnabled``。"""
        return self.message_format() == "rich"


def new_from_platform_config(
    raw: Mapping[str, Any] | None,
    store: Any,
    chat_history: Any,
    logger: Any,
    superadmins: Sequence[str] | None,
    command_prefixes: Sequence[str] | None,
    config_env_dir: str = "",
) -> Any:
    """从 ``[platform.telegram]`` 原始小节装配适配器。对应 Go ``NewFromPlatformConfig``。

    返回 :class:`atri_x.platform.telegram.adapter.Adapter`；配置非法时抛
    :class:`atri_x.errors.InvalidError`。
    """
    # 延迟导入：adapter 依赖本模块的 Config，模块级导入会成环。
    from atri_x.platform.telegram.adapter import new_adapter

    cfg = Config()
    try:
        decode_config(raw, cfg)
    except AtriXError as exc:
        raise InvalidError(f"decode telegram config: {exc}") from exc
    cfg.superadmins = list(superadmins or [])
    cfg.command_prefixes = list(command_prefixes or [])
    cfg.config_env_dir = config_env_dir
    apply_defaults(cfg)
    if cfg.enabled:
        token = cfg.token_value()
        if not token.strip():
            raise InvalidError("telegram bot_token or bot_token_env is required")
        cfg.parsed_proxy_url()
    return new_adapter(cfg, store, chat_history, logger)


def apply_defaults(cfg: Config) -> None:
    """就地补齐默认值。对应 Go ``applyDefaults``。"""
    if not cfg.api_base_url:
        cfg.api_base_url = DEFAULT_API_BASE_URL
    if not cfg.file_base_url:
        cfg.file_base_url = DEFAULT_FILE_BASE_URL
    if (cfg.api_timeout_seconds or 0) <= 0:
        cfg.api_timeout_seconds = int(DEFAULT_API_TIMEOUT)
    if (cfg.poll_timeout_seconds or 0) <= 0:
        cfg.poll_timeout_seconds = int(DEFAULT_POLL_TIMEOUT)
    if (cfg.reconnect_interval_seconds or 0) <= 0:
        cfg.reconnect_interval_seconds = int(DEFAULT_RECONNECT_INTERVAL)
    if (cfg.stream_edit_interval_milliseconds or 0) <= 0:
        cfg.stream_edit_interval_milliseconds = int(DEFAULT_STREAM_EDIT_INTERVAL * 1000)
    cfg.format = normalize_telegram_format(cfg.format)
    if not cfg.command_prefixes:
        cfg.command_prefixes = ["/"]


def normalize_telegram_format(format: str) -> str:
    """把 ``format`` 归一为 ``html`` / ``plain`` / ``rich``。对应 Go ``normalizeTelegramFormat``。"""
    value = (format or "").strip().lower()
    if value in ("", "html"):
        return DEFAULT_FORMAT
    if value in ("rich", "plain"):
        return value
    return DEFAULT_FORMAT


__all__ = [name for name in dir() if not name.startswith("_")]
