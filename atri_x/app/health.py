"""健康与运维 HTTP 端点的启动装配。

对应 Go：``internal/app/health.go``。

Go 直接从进程环境读 5 个开关（``ATRIX_HEALTH_ADDR`` 等），本模块逐字照抄这套
环境变量名与默认值，并复用 :func:`atri_x.config.config_env`（它先看进程环境、
再回落到配置目录里的 ``.env``，与 Go 的 ``config.ConfigEnv`` 一致）。

没有配置 ``ATRIX_HEALTH_ADDR`` 时**不启动**服务器并返回 ``(None, None)`` ——
这是 Go 的行为，不要在 Python 侧改成"默认监听 localhost"。
"""

from __future__ import annotations

import ipaddress
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from atri_x.config import config_env
from atri_x.errors import InvalidError
from atri_x.health import DirWritable, Options, ServerOptions, new_server, new_state

__all__ = [
    "DEFAULT_HEALTH_LIVE_STALE_SECONDS",
    "HEALTH_ADDR_ENV",
    "HEALTH_LIVE_STALE_ENV",
    "HEALTH_OPS_TOKEN_ENV",
    "HEALTH_ALLOW_UNAUTHENTICATED_ENV",
    "HEALTH_RESTART_REASON_ENV",
    "HEALTH_RESTART_REASON_VALUE_ENV",
    "read_health_config",
    "startHealthServer",
    "start_health_server",
    "applyRestartReason",
    "apply_restart_reason",
    "isLoopbackHealthAddr",
    "is_loopback_health_addr",
    "closeHealthServer",
    "close_health_server",
]

#: 对应 Go ``defaultHealthLiveStaleSeconds``。
DEFAULT_HEALTH_LIVE_STALE_SECONDS = 90

HEALTH_ADDR_ENV = "ATRIX_HEALTH_ADDR"
HEALTH_LIVE_STALE_ENV = "ATRIX_HEALTH_LIVE_STALE_SECONDS"
HEALTH_OPS_TOKEN_ENV = "ATRIX_OPS_TOKEN"
HEALTH_ALLOW_UNAUTHENTICATED_ENV = "ATRIX_OPS_ALLOW_UNAUTHENTICATED"
HEALTH_RESTART_REASON_ENV = "ATRIX_RESTART_REASON_FILE"
HEALTH_RESTART_REASON_VALUE_ENV = "ATRIX_LAST_RESTART_REASON"


def _warn(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "warn", None)
    if callable(emit):
        emit(message, **fields)


def read_health_config(cfg: Any) -> dict[str, Any]:
    """把 5 个环境变量解析成结构化结果（便于单测，无需真起服务器）。

    返回键：``addr`` / ``enabled`` / ``live_stale`` / ``ops_token`` /
    ``allow_unauthenticated``。
    """
    config_dir = os.path.dirname(getattr(cfg, "config_path", "") or "")
    addr = (config_env(HEALTH_ADDR_ENV, config_dir) or "").strip()
    enabled = bool(addr)

    live_stale = timedelta(seconds=DEFAULT_HEALTH_LIVE_STALE_SECONDS)
    raw = config_env(HEALTH_LIVE_STALE_ENV, config_dir)
    if raw is not None and raw.strip():
        try:
            seconds = int(raw.strip())
        except ValueError:
            seconds = -1
        if seconds <= 0:
            raise InvalidError(
                f"{HEALTH_LIVE_STALE_ENV} must be a positive integer, got {raw!r}"
            )
        live_stale = timedelta(seconds=seconds)

    ops_token = (config_env(HEALTH_OPS_TOKEN_ENV, config_dir) or "").strip()

    allow_unauthenticated = False
    raw = config_env(HEALTH_ALLOW_UNAUTHENTICATED_ENV, config_dir)
    if raw is not None and raw.strip():
        allow_unauthenticated = _parse_bool(raw.strip(), HEALTH_ALLOW_UNAUTHENTICATED_ENV)

    return {
        "addr": addr,
        "enabled": enabled,
        "live_stale": live_stale,
        "ops_token": ops_token,
        "allow_unauthenticated": allow_unauthenticated,
    }


def _parse_bool(raw: str, env_name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "t", "true", "y", "yes", "on"}:
        return True
    if lowered in {"0", "f", "false", "n", "no", "off"}:
        return False
    raise InvalidError(f"{env_name} must be a boolean, got {raw!r}")


async def start_health_server(
    cfg: Any,
    logger: Any,
    version: str,
    extra_handlers: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """对应 Go ``startHealthServer``，返回 ``(state, server)``。

    ``cfg`` 为 ``None`` 或未配置地址时返回 ``(None, None)``。
    """
    if cfg is None:
        return None, None
    parsed = read_health_config(cfg)
    if not parsed["enabled"]:
        return None, None
    addr = parsed["addr"]
    live_stale = parsed["live_stale"]
    ops_token = parsed["ops_token"]
    allow_unauthenticated = parsed["allow_unauthenticated"]

    if not ops_token:
        if allow_unauthenticated:
            _warn(
                logger,
                "ATRIX_OPS_ALLOW_UNAUTHENTICATED is enabled; sensitive ops handlers are "
                "exposed without authentication",
                addr=addr,
                loopback=is_loopback_health_addr(addr),
            )
        else:
            _warn(
                logger,
                "ATRIX_OPS_TOKEN is not set; /healthz and sensitive ops handlers are disabled",
                addr=addr,
                loopback=is_loopback_health_addr(addr),
            )

    state = new_state(
        Options(version=version, live_stale=live_stale.total_seconds())
    )
    storage = getattr(cfg, "storage", None)
    checkers = [
        DirWritable(
            dir=os.path.dirname(getattr(storage, "sessions_sqlite_path", "") or ""),
            check_name="sessions_sqlite_dir",
        ),
        DirWritable(
            dir=os.path.dirname(getattr(storage, "chat_history_sqlite_path", "") or ""),
            check_name="chat_history_sqlite_dir",
        ),
    ]
    try:
        server = new_server(
            ServerOptions(
                addr=addr,
                state=state,
                checkers=checkers,
                extra_handlers=dict(extra_handlers or {}),
                extra_handler_token=ops_token,
                allow_unauthenticated_extra_handlers=allow_unauthenticated,
                logger=logger,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 统一成可读错误
        raise InvalidError(f"create health server: {exc}") from exc
    try:
        server.start()
    except (OSError, RuntimeError) as exc:
        raise InvalidError(f"start health server: {exc}") from exc
    _log_info(
        logger,
        "health endpoints started",
        addr=addr,
        live_stale=_format_duration(live_stale),
    )
    return state, server


def apply_restart_reason(state: Any) -> None:
    """把上次重启原因写进健康快照。对应 Go ``applyRestartReason``。"""
    if state is None:
        return
    value = (os.environ.get(HEALTH_RESTART_REASON_VALUE_ENV) or "").strip()
    if value:
        state.set_last_restart_reason(value)
        return
    path = (os.environ.get(HEALTH_RESTART_REASON_ENV) or "").strip()
    if not path:
        runtime_dir = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
        if runtime_dir:
            path = os.path.join(runtime_dir, "atri-x", "last_restart_reason")
    if not path:
        return
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    text = data.strip()
    if text:
        state.set_last_restart_reason(text)


def is_loopback_health_addr(addr: str) -> bool:
    """对应 Go ``isLoopbackHealthAddr``。"""
    text = (addr or "").strip()
    host, port, ok = _split_host_port(text)
    if not ok:
        return False
    host = host.strip().strip("[]")
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_host_port(addr: str) -> tuple[str, str, bool]:
    """等价 Go ``net.SplitHostPort``：必须同时有 host 和 port。"""
    if addr.startswith("["):
        end = addr.find("]")
        if end < 0:
            return "", "", False
        host = addr[: end + 1]
        rest = addr[end + 1 :]
        if not rest.startswith(":"):
            return "", "", False
        port = rest[1:]
        return host, port, port != ""
    if addr.count(":") != 1:
        return "", "", False
    host, port = addr.split(":", 1)
    if port == "":
        return "", "", False
    return host, port, True


async def close_health_server(ctx: Any, server: Any) -> None:
    """对应 Go ``closeHealthServer``。"""
    if server is None:
        return
    result = server.close(ctx)
    if hasattr(result, "__await__"):
        await result


def _log_info(logger: Any, message: str, **fields: Any) -> None:
    emit = getattr(logger, "info", None)
    if callable(emit):
        emit(message, **fields)


def _format_duration(delta: timedelta) -> str:
    total = delta.total_seconds()
    if total < 1:
        return f"{int(total * 1000)}ms"
    return f"{total:g}s"


# Go 命名别名。
startHealthServer = start_health_server
applyRestartReason = apply_restart_reason
isLoopbackHealthAddr = is_loopback_health_addr
closeHealthServer = close_health_server
