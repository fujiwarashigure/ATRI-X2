"""qqofficial 适配器配置与装配入口。

对应 Go：``internal/platform/qqofficial/config.go``。

形式差异：

* ``*bool`` → ``bool | None``：``None`` 表示"没配过"，默认值由 :func:`apply_defaults` 补。
* ``time.Duration`` → 秒（``float``）。
* ``config.ConfigEnv`` → :func:`atri_x.config.loader.config_env`；``.env`` 读取失败时
  抛 :class:`atri_x.config.inspect.ConfigDecodeError`（Go 返回 error）。
* ``NewFromPlatformConfig`` 延迟导入 :class:`atri_x.platform.qqofficial.adapter.Adapter`，
  避免 ``config`` ↔ ``adapter`` 循环导入。
* Go 的 ``Config.secret()`` 还会兜底读 ``os.Getenv``（没有 ``.env`` 目录概念）；
  Python 侧统一走 :func:`atri_x.platform.qqofficial.config.resolve_secret`，
  系统环境变量优先、其次配置目录 ``.env``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atri_x.config.loader import config_env
from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.config import decode_config

#: 平台名。对应 Go ``platformName``。
PLATFORM_NAME = "qqofficial"

DEFAULT_API_BASE_URL = "https://api.sgroup.qq.com"
DEFAULT_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_HTTP_TIMEOUT = 15.0
DEFAULT_RECONNECT_INTERVAL = 3.0
DEFAULT_MAX_RECEIVE_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECS = 60


@dataclass
class Config:
    """``[platform.qqofficial]`` 小节。对应 Go ``Config``。"""

    enabled: bool = False
    app_id: str = ""
    client_secret: str = ""
    client_secret_env: str = ""
    allow_proactive: bool | None = None
    markdown_by_default: bool | None = None
    enable_keyboard: bool | None = None
    enable_ark: bool = False
    http_timeout_seconds: int = 0
    reconnect_interval_seconds: int = 0
    trigger_keywords: list[str] = field(default_factory=list)
    #: 群聊里是否投递工具调用进度预览。默认 ``False``（与上游 Go 一致）。
    show_tool_preview_in_group: bool = False
    # 以下字段来自平台外层配置（Go 的 ``toml:"-"`` / 无 tag）。
    command_prefixes: list[str] = field(default_factory=list)
    superadmins: list[str] = field(default_factory=list)
    attachment_dir: str = ""
    max_receive_file_bytes: int = 0
    download_timeout_secs: int = 0
    #: Python 侧新增：``.env`` 所在目录（Go 由 NewFromPlatformConfig 参数传入）。
    config_env_dir: str = ""

    # -- 取值口 -------------------------------------------------------------
    def secret(self) -> str:
        """对应 Go ``Config.secret()``（系统环境变量优先，其次配置目录 ``.env``）。"""
        value = (self.client_secret or "").strip()
        if value != "":
            return value
        name = (self.client_secret_env or "").strip()
        if name == "":
            return ""
        resolved = config_env(name, self.config_env_dir)
        if resolved is None:
            return ""
        return resolved.strip()

    def http_timeout(self) -> float:
        """对应 Go ``Config.httpTimeout``。"""
        seconds = float(self.http_timeout_seconds or 0)
        if seconds <= 0:
            return DEFAULT_HTTP_TIMEOUT
        return seconds

    def allow_proactive_messages(self) -> bool:
        """对应 Go ``Config.allowProactive``。"""
        return self.allow_proactive is None or bool(self.allow_proactive)

    def markdown_by_default_enabled(self) -> bool:
        """对应 Go ``Config.markdownByDefault``。"""
        return self.markdown_by_default is None or bool(self.markdown_by_default)

    def keyboard_enabled(self) -> bool:
        """对应 Go ``Config.enableKeyboard``。"""
        return self.enable_keyboard is None or bool(self.enable_keyboard)

    def reconnect_interval(self) -> float:
        """对应 Go ``Config.reconnectInterval``。"""
        seconds = float(self.reconnect_interval_seconds or 0)
        if seconds <= 0:
            return DEFAULT_RECONNECT_INTERVAL
        return seconds


def resolve_secret(cfg: Config, config_env_dir: str) -> str:
    """解析 client secret。对应 Go ``resolveSecret``。"""
    value = (cfg.client_secret or "").strip()
    if value != "":
        return value
    env_name = (cfg.client_secret_env or "").strip()
    if env_name == "":
        return ""
    cfg.config_env_dir = config_env_dir
    return cfg.secret()


def new_from_platform_config(
    raw: Mapping[str, Any] | None,
    store: Any,
    chat_history: Any,
    logger: Any,
    superadmins: Sequence[str] | None = None,
    command_prefixes: Sequence[str] | None = None,
    config_env_dir: str = "",
    attachment_dir: str = "",
    max_receive_file_bytes: int = 0,
    download_timeout_secs: int = 0,
) -> Any:
    """从 ``[platform.qqofficial]`` 原始小节装配适配器。对应 Go ``NewFromPlatformConfig``。

    签名与 Go 逐参对应；返回 :class:`atri_x.platform.qqofficial.adapter.Adapter`。
    配置非法时抛 :class:`atri_x.errors.InvalidError`。
    """
    # 延迟导入：adapter 依赖本模块的 Config，模块级导入会成环。
    from atri_x.platform.qqofficial.adapter import new_adapter

    cfg = Config()
    try:
        decode_config(raw, cfg)
    except AtriXError as exc:
        raise InvalidError(f"decode qqofficial config: {exc}") from exc
    cfg.superadmins = list(superadmins or [])
    cfg.command_prefixes = list(command_prefixes or [])
    cfg.attachment_dir = (attachment_dir or "").strip()
    cfg.max_receive_file_bytes = max_receive_file_bytes
    cfg.download_timeout_secs = download_timeout_secs
    cfg.config_env_dir = config_env_dir
    apply_defaults(cfg)
    if cfg.enabled:
        if (cfg.app_id or "").strip() == "":
            raise InvalidError("qqofficial app_id is required")
        try:
            secret = resolve_secret(cfg, config_env_dir)
        except AtriXError as exc:
            raise InvalidError(
                f"resolve qqofficial client secret from {cfg.client_secret_env}: {exc}"
            ) from exc
        if secret == "":
            raise InvalidError("qqofficial client_secret or client_secret_env is required")
        cfg.client_secret = secret
    return new_adapter(cfg, store, chat_history, logger)


def apply_defaults(cfg: Config) -> None:
    """就地补齐默认值。对应 Go ``applyDefaults``。"""
    if (cfg.http_timeout_seconds or 0) <= 0:
        cfg.http_timeout_seconds = int(DEFAULT_HTTP_TIMEOUT)
    if (cfg.reconnect_interval_seconds or 0) <= 0:
        cfg.reconnect_interval_seconds = int(DEFAULT_RECONNECT_INTERVAL)
    if cfg.allow_proactive is None:
        cfg.allow_proactive = True
    if cfg.markdown_by_default is None:
        cfg.markdown_by_default = True
    if cfg.enable_keyboard is None:
        cfg.enable_keyboard = True
    if (cfg.max_receive_file_bytes or 0) <= 0:
        cfg.max_receive_file_bytes = DEFAULT_MAX_RECEIVE_FILE_BYTES
    if (cfg.download_timeout_secs or 0) <= 0:
        cfg.download_timeout_secs = DEFAULT_DOWNLOAD_TIMEOUT_SECS


__all__ = [name for name in dir() if not name.startswith("_")]
