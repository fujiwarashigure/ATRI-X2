"""「在线」选项卡的数据层：平台配置、协议端探测、后台服务健康快照。

本模块**不依赖 Tk**，因此无显示环境也能逐条测：

- :func:`read_platform_config` / :func:`set_platform_enabled`：读写 ``app.toml`` 的
  ``[platform.qqonebot]``（走 :mod:`atri_x.gui.configedit`，保留注释与行序）；
- :func:`probe_endpoint`：对 OneBot 协议端（``ws://host:port/``）做 TCP 连通性探测；
- :func:`fetch_health` / :func:`parse_health`：拉后台服务 ``/healthz`` 并转成结构化视图；
- :func:`describe_*`：把状态转成人话（界面直接显示）。

后台服务的状态接口默认**不启用**：桌面端在
:func:`atri_x.gui.service.prepare_settings` 里往 ``.env`` 补
``ATRIX_HEALTH_ADDR`` 与 ``ATRIX_OPS_TOKEN``（幂等，已有值不动），后端读到后才会在
``http://127.0.0.1:32171`` 上起 ``/live`` / ``/ready`` / ``/healthz``。

为什么状态从 ``/healthz`` 拿而不是自己观察日志：平台连接状态是**后端进程**的事实
（``atri_x.health.state.PlatformStatus``），由 ``atri_x.app.platforms`` 在连接/断开实际
发生时写入；GUI 只是另一个进程，只能通过公开接口读，不该去猜日志。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from atri_x.gui import configedit
from atri_x.gui.configedit import ConfigEditError

__all__ = [
    "DEFAULT_PLATFORM",
    "HEALTH_PATH",
    "EndpointProbe",
    "HealthReport",
    "LinkError",
    "ModelEntry",
    "PlatformConfig",
    "PlatformEntry",
    "connect_blocker",
    "describe_model",
    "describe_platform",
    "fetch_health",
    "format_clock",
    "format_uptime",
    "health_endpoint",
    "parse_health",
    "platform_of",
    "probe_endpoint",
    "read_platform_config",
    "secret_state",
    "set_platform_enabled",
]

#: 本选项卡针对的平台（QQ OneBot v11）。
DEFAULT_PLATFORM = "qqonebot"

#: 后台服务健康快照的路径（见 :mod:`atri_x.health.server`）。
HEALTH_PATH = "/healthz"

#: TCP 探测 / HTTP 请求的默认超时（秒）。界面每 2 秒刷新一次，不能拖住 Tk。
DEFAULT_TIMEOUT = 1.5

#: Go 侧 ``time.Time`` 零值的 RFC3339 形状：表示"从未发生"。
GO_ZERO_TIME = "0001-01-01T00:00:00Z"


class LinkError(Exception):
    """状态采集的可预期失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 健康快照
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    return bool(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


@dataclass(frozen=True)
class PlatformEntry:
    """``/healthz`` 里 ``platforms[]`` 的一项（对应 ``health.state.PlatformStatus``）。

    ``connected`` 只表示"传输层连上了"；``login_ok`` 才是"QQ 已登录"。两者都为真
    才等于"能用 QQ 收发消息"。
    """

    name: str = ""
    connected: bool = False
    connect_count: int = 0
    last_connected_at: str = ""
    last_disconnected_at: str = ""
    last_error: str = ""
    backend: str = ""
    login_ok: bool = False
    login_checked: bool = False
    login_checked_at: str = ""
    self_id: str = ""
    nickname: str = ""
    app_name: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PlatformEntry":
        data = _mapping(data)
        return cls(
            name=_text(data.get("name")),
            connected=_bool(data.get("connected")),
            connect_count=_int(data.get("connect_count")),
            last_connected_at=_text(data.get("last_connected_at")),
            last_disconnected_at=_text(data.get("last_disconnected_at")),
            last_error=_text(data.get("last_error")),
            backend=_text(data.get("backend")),
            login_ok=_bool(data.get("login_ok")),
            login_checked="login_checked_at" in data,
            login_checked_at=_text(data.get("login_checked_at")),
            self_id=_text(data.get("self_id")),
            nickname=_text(data.get("nickname")),
            app_name=_text(data.get("app_name")),
        )


@dataclass(frozen=True)
class ModelEntry:
    """``/healthz`` 里 ``models[]`` 的一项（provider 级健康/熔断状态）。"""

    provider: str = ""
    status: str = ""
    circuit: str = ""
    last_error: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ModelEntry":
        data = _mapping(data)
        return cls(
            provider=_text(data.get("provider")),
            status=_text(data.get("status")),
            circuit=_text(data.get("circuit")),
            last_error=_text(data.get("last_error")),
        )


@dataclass(frozen=True)
class HealthReport:
    """后台服务的一份只读健康视图（字段与 ``marshal_snapshot`` 对应）。"""

    http_status: int = 0
    status: str = ""
    live: bool = False
    ready: bool = False
    degraded: bool = False
    version: str = ""
    uptime_seconds: int = 0
    started_at: str = ""
    messages_ok: int = 0
    messages_failed: int = 0
    last_message_at: str = ""
    checks: Mapping[str, str] = field(default_factory=dict)
    models: tuple[ModelEntry, ...] = ()
    platforms: tuple[PlatformEntry, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    def platform(self, name: str = DEFAULT_PLATFORM) -> PlatformEntry | None:
        return platform_of(self, name)


def parse_health(body: Any, *, http_status: int = 0) -> HealthReport:
    """把 ``/healthz`` 的 JSON 体转成 :class:`HealthReport`（缺字段按默认值）。"""
    data = _mapping(body)
    if not data:
        raise LinkError("状态接口返回的不是 JSON 对象")
    checks = {
        str(key): _text(value) for key, value in _mapping(data.get("checks")).items()
    }
    models = tuple(
        ModelEntry.from_mapping(item) for item in _items(data.get("models"))
    )
    platforms = tuple(
        PlatformEntry.from_mapping(item) for item in _items(data.get("platforms"))
    )
    return HealthReport(
        http_status=int(http_status),
        status=_text(data.get("status")),
        live=_bool(data.get("live")),
        ready=_bool(data.get("ready")),
        degraded=_bool(data.get("degraded")),
        version=_text(data.get("version")),
        uptime_seconds=_int(data.get("uptime_seconds")),
        started_at=_text(data.get("started_at")),
        messages_ok=_int(data.get("messages_ok")),
        messages_failed=_int(data.get("messages_failed")),
        last_message_at=_text(data.get("last_message_at")),
        checks=checks,
        models=models,
        platforms=platforms,
        raw=data,
    )


def health_endpoint(addr: str) -> str:
    """``127.0.0.1:32171`` → ``http://127.0.0.1:32171/healthz``。"""
    text = _text(addr)
    if not text:
        raise LinkError("未配置后台服务状态接口地址")
    host, _, port = text.rpartition(":")
    if not host:
        host = text
        port = "80"
    if host in ("0.0.0.0", "::", "[::]", "*"):
        host = "127.0.0.1"
    if not port:
        raise LinkError(f"状态接口地址缺少端口：{text}")
    return f"http://{host}:{port}{HEALTH_PATH}"


def fetch_health(
    addr: str, token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> HealthReport:
    """拉一次 ``/healthz``；``503``（not_live / not_ready）仍会解析，其余非 2xx 抛错。

    显式禁用代理：要访问的是本机回环地址，走系统代理只会超时或 407。
    """
    url = health_endpoint(addr)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(0.2, float(timeout))) as response:
            status = int(getattr(response, "status", 200) or 200)
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            payload = exc.read()
        except OSError:  # 读错误体失败不该盖住鉴权/服务错误
            payload = b""
        finally:
            exc.close()
        if status in (401, 403):
            raise LinkError(
                "状态接口鉴权失败：请检查 .env 的 ATRIX_OPS_TOKEN 与桌面端是否一致"
            ) from exc
        if status not in (200, 503):
            raise LinkError(f"状态接口返回 HTTP {status}") from exc
    except urllib.error.URLError as exc:
        raise LinkError(f"状态接口不可达：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise LinkError(f"状态接口不可达：{exc}") from exc
    if not payload:
        return parse_health({}, http_status=status)
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LinkError(f"状态接口返回了无法解析的内容：{exc}") from exc
    return parse_health(body, http_status=status)


def platform_of(report: HealthReport, name: str = DEFAULT_PLATFORM) -> PlatformEntry | None:
    """取某个平台的状态项；``/healthz`` 只列已登记的平台。"""
    for item in report.platforms:
        if item.name == name:
            return item
    return None


# ---------------------------------------------------------------------------
# 协议端探测
# ---------------------------------------------------------------------------
def parse_ws_url(url: str) -> tuple[str, int] | None:
    """解析 ``ws://host:port/path`` → ``(host, port)``；解析不出来返回 ``None``。"""
    text = _text(url)
    if not text:
        return None
    if "://" not in text:
        text = "ws://" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    host = parts.hostname or ""
    if not host:
        return None
    port = parts.port
    if port is None:
        port = 443 if (parts.scheme or "").lower() in ("wss", "https") else 80
    return host, int(port)


@dataclass(frozen=True)
class EndpointProbe:
    """协议端端口探测结果。"""

    reachable: bool = False
    host: str = ""
    port: int = 0
    detail: str = ""


def probe_endpoint(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> EndpointProbe:
    """TCP 连一次协议端端口：能建连就说明协议端（NapCat）在监听。

    只做 TCP 握手，不跑 OneBot 协议：连接/发送/监听的完整验证用
    ``atri-x.exe -m atri_x.platform.qq_onebot.smoke``（见 ``docs/windows-app.md``）。
    """
    parsed = parse_ws_url(url)
    if parsed is None:
        return EndpointProbe(reachable=False, detail="协议端地址无效（应形如 ws://127.0.0.1:3001/）")
    host, port = parsed
    try:
        with socket.create_connection((host, port), timeout=max(0.2, float(timeout))):
            pass
    except OSError as exc:
        return EndpointProbe(
            reachable=False,
            host=host,
            port=port,
            detail=f"{host}:{port} 未监听（{exc}）",
        )
    return EndpointProbe(
        reachable=True,
        host=host,
        port=port,
        detail=f"{host}:{port} 已监听",
    )


# ---------------------------------------------------------------------------
# 平台配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlatformConfig:
    """``app.toml`` 里 ``[platform.qqonebot]`` 的只读视图。"""

    section_exists: bool = False
    enabled: bool = False
    ws_url: str = ""
    token_env: str = ""
    trigger_keywords: tuple[str, ...] = ()
    send_file_mode: str = ""

    @property
    def has_endpoint(self) -> bool:
        return bool(self.ws_url.strip())

    @property
    def endpoint(self) -> tuple[str, int] | None:
        return parse_ws_url(self.ws_url)


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def read_platform_config(config_path: str) -> PlatformConfig:
    """读 ``[platform.qqonebot]``；文件不存在时返回"未配置"（不抛错）。"""
    data = configedit.read_toml(config_path)
    platform = _mapping(data.get("platform"))
    entry = platform.get(DEFAULT_PLATFORM)
    entry = entry if isinstance(entry, Mapping) else {}
    return PlatformConfig(
        section_exists=isinstance(platform.get(DEFAULT_PLATFORM), Mapping),
        enabled=_bool(entry.get("enabled")),
        ws_url=_text(entry.get("ws_url") or entry.get("url")),
        token_env=_text(entry.get("access_token_env")),
        trigger_keywords=_string_list(entry.get("trigger_keywords")),
        send_file_mode=_text(entry.get("send_file_mode")),
    )


def set_platform_enabled(config_path: str, enabled: bool) -> bool:
    """把 ``[platform.qqonebot].enabled`` 置为 ``enabled``；返回是否有改动。"""
    return configedit.patch_section(
        config_path, f"platform.{DEFAULT_PLATFORM}", {"enabled": bool(enabled)}
    )


def secret_state(platform: PlatformConfig, env_path: str) -> str:
    """token 的可用性：``"none"``（协议端不要鉴权）/ ``"set"`` / ``"missing"``。

    与后端一致：环境变量优先，其次配置目录 ``.env``（见 ``atri_x.config.loader.config_env``）。
    """
    name = platform.token_env.strip()
    if not name:
        return "none"
    if (os.environ.get(name) or "").strip():
        return "set"
    try:
        values = configedit.read_env(env_path)
    except ConfigEditError:
        return "missing"
    return "set" if (values.get(name) or "").strip() else "missing"


def connect_blocker(platform: PlatformConfig, env_path: str) -> str:
    """「连接上线」前的配置检查；返回空串表示可以连，否则返回给用户看的原因。"""
    if not platform.has_endpoint:
        return (
            "还没配协议端地址：先到「平台」页填 [platform.qqonebot].ws_url"
            "（例如 ws://127.0.0.1:3001/）并保存。"
        )
    if secret_state(platform, env_path) == "missing":
        return (
            f"协议端令牌还没写入 .env：到「平台」页填 token 值并保存"
            f"（变量名 {platform.token_env}）；协议端不鉴权时清空该变量名即可。"
        )
    return ""


# ---------------------------------------------------------------------------
# 人话
# ---------------------------------------------------------------------------
def format_uptime(seconds: int) -> str:
    """把秒数写成「2 天 3 小时」/「1 小时 5 分」/「3 分 20 秒」。"""
    total = max(0, _int(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


def format_clock(iso: str) -> str:
    """RFC3339（UTC）→ 本地 ``MM-DD HH:MM:SS``；空值/Go 零值返回空串。"""
    text = _text(iso)
    if not text or text == GO_ZERO_TIME:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone().strftime("%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return text


def describe_platform(
    entry: PlatformEntry | None,
    *,
    enabled: bool,
    service_known: bool,
) -> str:
    """QQ 平台一句话状态（界面主状态行）。"""
    if not enabled:
        return "未启用（点「连接上线」写入配置并重启后台服务）"
    if not service_known:
        return "后台服务未运行（状态未知）"
    if entry is None:
        return "已启用，等待后端登记平台状态…"
    if entry.connected:
        return "已连接（QQ 在线）"
    if entry.last_error:
        return f"未连接：{entry.last_error}"
    return "未连接（协议端未启动或 ws 地址/令牌不对）"


def describe_model(entry: ModelEntry) -> str:
    """模型 provider 一句话状态。"""
    name = entry.provider or "unknown"
    if entry.circuit:
        return f"{name}：熔断（{entry.circuit}）"
    if entry.status == "up":
        return f"{name}：正常"
    if entry.status == "degraded":
        reason = f"：{entry.last_error}" if entry.last_error else ""
        return f"{name}：异常{reason}"
    return f"{name}：{entry.status or '未知'}"
