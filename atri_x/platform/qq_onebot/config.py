"""qq-onebot 适配器配置与装配入口。

对应 Go：``internal/platform/qq-onebot/adapter.go`` 的 ``Config`` /
``applyDefaults`` / ``validateSendFileMode`` / ``NewFromPlatformConfig`` / ``New``。

命名差异（Python 里字段名必须能跟 TOML 键对上）：

* Go ``Config.URL``（toml ``ws_url``）→ 字段 :attr:`Config.url`；:func:`new_from_platform_config`
  在解码前把 ``ws_url`` 别名成 ``url``（``atri_x.platform.config.decode_config`` 按属性名匹配，
  不认 struct tag）。两个键都接受。
* ``*bool`` → ``bool | None``：``None`` 表示"没配过"，默认值由 :func:`apply_defaults` 补。

其它形式差异：``time.Duration`` → 秒（``float``）；``config.ConfigEnv`` →
:func:`atri_x.config.loader.config_env`（读 ``.env`` 失败时抛
:class:`atri_x.config.inspect.ConfigDecodeError`）；``NewFromPlatformConfig`` 里延迟导入
:class:`atri_x.platform.qq_onebot.adapter.Adapter`，避免 ``config`` ↔ ``adapter`` 循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atri_x.config.loader import config_env
from atri_x.errors import AtriXError, InvalidError
from atri_x.platform.config import decode_config
from atri_x.platform.qq_onebot.output import SEND_FILE_MODE_BASE64, SEND_FILE_MODE_FILE_URI
from atri_x.platform.qq_onebot.types import (
    DEFAULT_FORWARD_FETCH_TIMEOUT,
    DEFAULT_FORWARD_MAX_DEPTH,
    DEFAULT_FORWARD_MAX_FETCHES,
    DEFAULT_FORWARD_MAX_NODES,
    DEFAULT_FORWARD_MAX_NON_TEXT,
    DEFAULT_FORWARD_MAX_RESULT_BYTES,
    DEFAULT_FORWARD_MAX_RUNES,
)

#: 平台名。对应 Go ``(*Adapter).Name()``。
PLATFORM_NAME = "qqonebot"

#: 默认正向 WebSocket 地址。对应 Go ``applyDefaults`` 里的字面量。
DEFAULT_WS_URL = "ws://127.0.0.1:6700/"
DEFAULT_RECONNECT_INTERVAL_SECONDS = 3
DEFAULT_API_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RECEIVE_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECS = 60
DEFAULT_INBOUND_DEDUP_TTL_SECONDS = 1800
DEFAULT_INBOUND_DEDUP_MAX_ENTRIES = 4096
DEFAULT_PREPROCESS_WORKERS = 4
DEFAULT_PREPROCESS_QUEUE_SIZE = 256
DEFAULT_HIGH_PRIORITY_WORKERS = 1
DEFAULT_HIGH_PRIORITY_QUEUE_SIZE = 128


@dataclass
class Config:
    """``[platform.qqonebot]`` 小节。对应 Go ``Config``。"""

    enabled: bool = False
    url: str = ""
    access_token: str = ""
    access_token_env: str = ""
    reconnect_interval_seconds: int = 0
    api_timeout_seconds: int = 0
    trigger_keywords: list[str] = field(default_factory=list)
    send_file_mode: str = ""
    forward_max_nodes: int = 0
    forward_max_runes: int = 0
    forward_max_depth: int = 0
    forward_max_fetches: int = 0
    forward_max_result_bytes: int = 0
    forward_max_non_text: int = 0
    forward_fetch_timeout_seconds: int = 0
    #: 未配置时为 ``None`` → 默认开启。见 Go 的同名字段注释。
    inbound_dedup_enabled: bool | None = None
    #: 群聊里是否投递工具调用进度预览（"正在调用 web_search"这类提示）。
    #: 默认 ``False``，与上游 Go 一致（群里的进度预览是噪声）。
    show_tool_preview_in_group: bool = False
    inbound_dedup_ttl_seconds: int = 0
    inbound_dedup_max_entries: int = 0
    preprocess_workers: int = 0
    preprocess_queue_size: int = 0
    high_priority_workers: int = 0
    high_priority_queue_size: int = 0
    # 以下字段来自平台外层配置（Go 的 ``toml:"-"``）。
    attachment_dir: str = ""
    max_receive_file_bytes: int = 0
    download_timeout_secs: int = 0
    superadmins: list[str] = field(default_factory=list)
    command_prefixes: list[str] = field(default_factory=list)
    #: Python 侧新增：``.env`` 所在目录（Go 由 NewFromPlatformConfig 参数传入）。
    config_env_dir: str = ""


#: TOML 键 → 属性名的别名（``decode_config`` 不认 struct tag）。
CONFIG_KEY_ALIASES = {"ws_url": "url"}


def resolve_access_token(cfg: Config) -> str:
    """取访问令牌：``access_token`` 优先，其次按 ``access_token_env`` 查。"""
    value = (cfg.access_token or "").strip()
    if value != "":
        return value
    name = (cfg.access_token_env or "").strip()
    if name == "":
        return ""
    resolved = config_env(name, cfg.config_env_dir)
    if resolved is None:
        return ""
    return resolved.strip()


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
    """从 ``[platform.qqonebot]`` 原始小节装配适配器。对应 Go ``NewFromPlatformConfig``。

    签名与 Go 逐参对应（``raw, store, chatHistory, logger, superadmins,
    commandPrefixes, configEnvDir, attachmentDir, maxReceiveFileBytes,
    downloadTimeoutSecs``）；返回 :class:`atri_x.platform.qq_onebot.adapter.Adapter`。
    配置非法时抛 :class:`atri_x.errors.InvalidError`。
    """
    # 延迟导入：adapter 依赖本模块的 Config，模块级导入会成环。
    from atri_x.platform.qq_onebot.adapter import new_adapter

    cfg = Config()
    try:
        decode_config(_aliased(raw), cfg)
    except AtriXError as exc:
        raise InvalidError(f"decode qqonebot config: {exc}") from exc
    cfg.superadmins = list(superadmins or [])
    cfg.command_prefixes = list(command_prefixes or [])
    cfg.attachment_dir = (attachment_dir or "").strip()
    cfg.max_receive_file_bytes = max_receive_file_bytes
    cfg.download_timeout_secs = download_timeout_secs
    cfg.config_env_dir = config_env_dir
    apply_defaults(cfg)
    validate_send_file_mode(cfg.send_file_mode)
    if (cfg.access_token or "").strip() == "" and (cfg.access_token_env or "").strip() != "":
        try:
            cfg.access_token = resolve_access_token(cfg)
        except AtriXError as exc:
            raise InvalidError(
                f"resolve qqonebot access token from {cfg.access_token_env}: {exc}"
            ) from exc
    return new_adapter(cfg, store, chat_history, logger)


def _aliased(raw: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """把 TOML 键别名（``ws_url``）换成属性名。"""
    if not isinstance(raw, Mapping):
        return raw
    out: dict[str, Any] = {}
    for key, value in raw.items():
        out[CONFIG_KEY_ALIASES.get(str(key), str(key))] = value
    return out


def apply_defaults(cfg: Config) -> None:
    """就地补齐默认值。对应 Go ``applyDefaults``。"""
    if not cfg.url:
        cfg.url = DEFAULT_WS_URL
    if (cfg.reconnect_interval_seconds or 0) <= 0:
        cfg.reconnect_interval_seconds = DEFAULT_RECONNECT_INTERVAL_SECONDS
    if (cfg.api_timeout_seconds or 0) <= 0:
        cfg.api_timeout_seconds = DEFAULT_API_TIMEOUT_SECONDS
    cfg.send_file_mode = (cfg.send_file_mode or "").strip().lower()
    if cfg.send_file_mode == "":
        cfg.send_file_mode = SEND_FILE_MODE_BASE64
    if not cfg.command_prefixes:
        cfg.command_prefixes = ["/"]
    if (cfg.max_receive_file_bytes or 0) <= 0:
        cfg.max_receive_file_bytes = DEFAULT_MAX_RECEIVE_FILE_BYTES
    if (cfg.download_timeout_secs or 0) <= 0:
        cfg.download_timeout_secs = DEFAULT_DOWNLOAD_TIMEOUT_SECS
    if (cfg.forward_max_nodes or 0) <= 0:
        cfg.forward_max_nodes = DEFAULT_FORWARD_MAX_NODES
    if (cfg.forward_max_runes or 0) <= 0:
        cfg.forward_max_runes = DEFAULT_FORWARD_MAX_RUNES
    if (cfg.forward_max_depth or 0) <= 0:
        cfg.forward_max_depth = DEFAULT_FORWARD_MAX_DEPTH
    if (cfg.forward_max_fetches or 0) <= 0:
        cfg.forward_max_fetches = DEFAULT_FORWARD_MAX_FETCHES
    if (cfg.forward_max_result_bytes or 0) <= 0:
        cfg.forward_max_result_bytes = DEFAULT_FORWARD_MAX_RESULT_BYTES
    if (cfg.forward_max_non_text or 0) <= 0:
        cfg.forward_max_non_text = DEFAULT_FORWARD_MAX_NON_TEXT
    if (cfg.forward_fetch_timeout_seconds or 0) <= 0:
        cfg.forward_fetch_timeout_seconds = int(DEFAULT_FORWARD_FETCH_TIMEOUT)
    if cfg.inbound_dedup_enabled is None:
        cfg.inbound_dedup_enabled = True
    if (cfg.inbound_dedup_ttl_seconds or 0) <= 0:
        cfg.inbound_dedup_ttl_seconds = DEFAULT_INBOUND_DEDUP_TTL_SECONDS
    if (cfg.inbound_dedup_max_entries or 0) <= 0:
        cfg.inbound_dedup_max_entries = DEFAULT_INBOUND_DEDUP_MAX_ENTRIES
    if (cfg.preprocess_workers or 0) <= 0:
        cfg.preprocess_workers = DEFAULT_PREPROCESS_WORKERS
    if (cfg.preprocess_queue_size or 0) <= 0:
        cfg.preprocess_queue_size = DEFAULT_PREPROCESS_QUEUE_SIZE
    if (cfg.high_priority_workers or 0) <= 0:
        cfg.high_priority_workers = DEFAULT_HIGH_PRIORITY_WORKERS
    if (cfg.high_priority_queue_size or 0) <= 0:
        cfg.high_priority_queue_size = DEFAULT_HIGH_PRIORITY_QUEUE_SIZE


def validate_send_file_mode(mode: str) -> None:
    """对应 Go ``validateSendFileMode``；非法时抛 :class:`atri_x.errors.InvalidError`。"""
    if mode in (SEND_FILE_MODE_BASE64, SEND_FILE_MODE_FILE_URI):
        return
    raise InvalidError(
        "qqonebot send_file_mode must be "
        f"{SEND_FILE_MODE_BASE64!r} or {SEND_FILE_MODE_FILE_URI!r}, got {mode!r}"
    )


__all__ = [name for name in dir() if not name.startswith("_")]
