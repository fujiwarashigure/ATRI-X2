"""Doctor 的单项检查实现。

对应 Go：``internal/app/doctor.go`` 里 ``RunDoctor`` 调用的那些助手函数
（``checkWritableDirs`` / ``resolveDoctorAPIKey`` / ``doctorHealthURL`` /
``probeApplicationHealth`` / ``resolveDoctorOpsToken`` / ``fetchHealthSnapshot`` /
``enabledPlatformNames`` / ``doctorPlatformConnection`` / ``snapshotHasPlatform`` /
``doctorModelCall`` / ``doctorCLIRoundTrip`` / ``firstLine``）。

拆出来的原因：Go 单文件 575 行，Python 侧要求单文件 ≤600 行且职责单一；
主流程留在 :mod:`atri_x.app.doctor`。

与 Go 的差异：

* ``os.CreateTemp`` 换成 ``O_CREAT|O_EXCL`` 手写随机名 —— PORTING §7.2 禁用
  ``mkdtemp``/``TemporaryDirectory``，这里也顺手避开整个 :mod:`tempfile`；
* ``net/http`` 探测用 :mod:`urllib.request`（超时 5s）；
* ``websocket.Dial`` + ``wsjson`` 用 :func:`atri_x.net.websocket.connect` + ``json``。
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any

from atri_x.config import config_env
from atri_x.errors import ErrHealthzDisabled, InvalidError, NotFoundError
from atri_x.llm import ChatRequest, LLMMessage, Role, SegmentType
from atri_x.llm.segment import MessageSegment
from atri_x.redact import new_error_id

__all__ = [
    "DEFAULT_DOCTOR_TIMEOUT",
    "HEALTH_ADDR_ENV",
    "HEALTH_OPS_TOKEN_ENV",
    "checkWritableDirs",
    "check_writable_dirs",
    "resolveDoctorAPIKey",
    "resolve_doctor_api_key",
    "doctorHealthURL",
    "doctor_health_url",
    "probeApplicationHealth",
    "probe_application_health",
    "resolveDoctorOpsToken",
    "resolve_doctor_ops_token",
    "fetchHealthSnapshot",
    "fetch_health_snapshot",
    "enabledPlatformNames",
    "enabled_platform_names",
    "doctorPlatformConnection",
    "doctor_platform_connection",
    "snapshotHasPlatform",
    "snapshot_has_platform",
    "doctorModelCall",
    "doctor_model_call",
    "doctorCLIRoundTrip",
    "doctor_cli_round_trip",
    "firstLine",
    "first_line",
]

#: 对应 Go ``defaultDoctorTimeout``。
DEFAULT_DOCTOR_TIMEOUT = 60.0

HEALTH_ADDR_ENV = "ATRIX_HEALTH_ADDR"
HEALTH_OPS_TOKEN_ENV = "ATRIX_OPS_TOKEN"

#: 探测请求的超时，逐字对应 Go 的 ``5 * time.Second``。
PROBE_TIMEOUT_SECONDS = 5.0


def first_line(text: str) -> str:
    """对应 Go ``firstLine``。"""
    index = (text or "").find("\n")
    if index >= 0:
        return text[:index].strip()
    return (text or "").strip()


def check_writable_dirs(cfg: Any) -> None:
    """对应 Go ``checkWritableDirs``：在两个存储目录里各写一次再删。"""
    storage = getattr(cfg, "storage", None)
    dirs = [
        os.path.dirname(str(getattr(storage, "sessions_sqlite_path", "") or "")),
        os.path.dirname(str(getattr(storage, "chat_history_sqlite_path", "") or "")),
    ]
    for directory in dirs:
        if not directory.strip():
            raise InvalidError("storage directory is empty")
        try:
            os.makedirs(directory, mode=0o750, exist_ok=True)
        except OSError as exc:
            raise InvalidError(f"create {directory}: {exc}") from exc
        name = os.path.join(directory, f".atri-x-doctor-{secrets.token_hex(6)}")
        try:
            handle = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            raise InvalidError(f"write {directory}: {exc}") from exc
        try:
            os.write(handle, b"ok\n")
        except OSError as exc:
            raise InvalidError(f"write {name}: {exc}") from exc
        finally:
            os.close(handle)
        try:
            os.remove(name)
        except OSError as exc:
            raise InvalidError(f"remove {name}: {exc}") from exc


def resolve_doctor_api_key(provider: Any, config_dir: str) -> str:
    """对应 Go ``resolveDoctorAPIKey``：先 ``api_key_env``，再 ``api_key``。"""
    env_name = str(getattr(provider, "api_key_env", "") or "").strip()
    if env_name:
        value = config_env(env_name, config_dir)
        if value is not None and value.strip():
            return value.strip()
    inline = str(getattr(provider, "api_key", "") or "").strip()
    if inline:
        return inline
    if env_name:
        raise InvalidError(f"environment variable {env_name} is not set")
    raise InvalidError("provider api_key/api_key_env is not configured")


def doctor_health_url(cfg: Any) -> str:
    """把 ``ATRIX_HEALTH_ADDR`` 解析成可访问的 ``http://host:port``。"""
    config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
    addr = (config_env(HEALTH_ADDR_ENV, config_dir) or "").strip()
    if not addr:
        return ""
    host, port, ok = _split_host_port(addr)
    if not ok:
        raise InvalidError(f"parse {HEALTH_ADDR_ENV}={addr!r}: not a host:port address")
    host = host.strip("[]")
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{_join_host_port(host, port)}"


def _split_host_port(addr: str) -> tuple[str, str, bool]:
    if addr.startswith("["):
        end = addr.find("]")
        if end < 0 or not addr[end + 1 :].startswith(":"):
            return "", "", False
        port = addr[end + 2 :]
        return addr[: end + 1], port, port != ""
    if addr.count(":") != 1:
        return "", "", False
    host, port = addr.split(":", 1)
    return host, port, port != ""


def _join_host_port(host: str, port: str) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """GET 一次；HTTP 错误码也当结果返回（不抛），只有连接层错误才抛。"""
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""


def probe_application_health(ctx: Any, base_url: str) -> None:
    """对应 Go ``probeApplicationHealth``：``GET /live`` 必须 200。"""
    url = (base_url or "").rstrip("/") + "/live"
    try:
        status, _body = _http_get(url)
    except Exception as exc:  # noqa: BLE001 - 统一成可读错误
        raise InvalidError(f"probe {url}: {exc}") from exc
    if status != 200:
        raise InvalidError(f"/live returned HTTP {status}")


def resolve_doctor_ops_token(cfg: Any) -> str:
    """对应 Go ``resolveDoctorOpsToken``。"""
    if cfg is None:
        return ""
    config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
    try:
        return (config_env(HEALTH_OPS_TOKEN_ENV, config_dir) or "").strip()
    except Exception:  # noqa: BLE001 - 解析失败等同未配置
        return ""


def fetch_health_snapshot(ctx: Any, base_url: str, ops_token: str) -> Any:
    """``GET /healthz`` 并解析成 :class:`atri_x.health.Snapshot`。

    ``/healthz`` 未注册（404）时抛 :data:`atri_x.errors.ErrHealthzDisabled`，
    让调用方区分"没开 ops token"与"真的坏了"。
    """
    from atri_x.health import Snapshot

    url = (base_url or "").rstrip("/") + "/healthz"
    headers: dict[str, str] = {}
    token = (ops_token or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        status, body = _http_get(url, headers)
    except Exception as exc:  # noqa: BLE001
        raise InvalidError(f"fetch {url}: {exc}") from exc
    if status != 200:
        if status == 404:
            raise ErrHealthzDisabled
        raise InvalidError(f"/healthz returned HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidError(f"decode /healthz: {exc}") from exc
    return _snapshot_from_json(payload, Snapshot)


def _snapshot_from_json(payload: Any, snapshot_cls: Any) -> Any:
    """按 dataclass 字段名填充快照；未知字段忽略，缺失字段用默认值。"""
    import dataclasses

    if not isinstance(payload, dict):
        raise InvalidError("decode /healthz: not a JSON object")
    kwargs: dict[str, Any] = {}
    for field in dataclasses.fields(snapshot_cls):
        if field.name in payload:
            kwargs[field.name] = payload[field.name]
    try:
        return snapshot_cls(**kwargs)
    except TypeError as exc:
        raise InvalidError(f"decode /healthz: {exc}") from exc


def enabled_platform_names(cfg: Any) -> list[str]:
    """对应 Go ``enabledPlatformNames``：缺 ``enabled`` 视为启用。"""
    names: list[str] = []
    for name, raw in (getattr(cfg, "platform", None) or {}).items():
        enabled = True
        if isinstance(raw, dict) and "enabled" in raw:
            value = raw["enabled"]
            if isinstance(value, bool):
                enabled = value
        if enabled:
            names.append(name)
    return names


def doctor_platform_connection(
    snapshot: Any, enabled: list[str], require: bool
) -> tuple[str, str, BaseException | None]:
    """对应 Go ``doctorPlatformConnection``。"""
    detail = "health=" + str(getattr(snapshot, "status", "") or "")
    platforms = list(getattr(snapshot, "platforms", None) or [])
    parts: list[str] = []
    connected: dict[str, bool] = {}
    for platform in platforms:
        name = str(getattr(platform, "name", "") or "")
        is_connected = bool(getattr(platform, "connected", False))
        parts.append(name + ("=connected" if is_connected else "=disconnected"))
        if is_connected:
            connected[name] = True
    if parts:
        detail = ", ".join(parts)
    if not enabled:
        if require:
            return (
                "failed",
                detail,
                InvalidError("--require-platform was set but no platform is enabled"),
            )
        return "passed", detail + " (no platform is enabled)", None
    missing: list[str] = []
    disconnected: list[str] = []
    for name in enabled:
        if connected.get(name):
            continue
        if snapshot_has_platform(snapshot, name):
            disconnected.append(name)
        else:
            missing.append(name)
    if disconnected:
        return (
            "failed",
            detail,
            InvalidError("platform disconnected: " + ", ".join(disconnected)),
        )
    if require and missing:
        return (
            "failed",
            detail,
            InvalidError("platform status missing for: " + ", ".join(missing)),
        )
    return "passed", detail, None


def snapshot_has_platform(snapshot: Any, name: str) -> bool:
    """对应 Go ``snapshotHasPlatform``。"""
    for platform in getattr(snapshot, "platforms", None) or []:
        if str(getattr(platform, "name", "") or "") == name:
            return True
    return False


async def doctor_model_call(
    ctx: Any,
    cfg: Any,
    provider_name: str,
    provider: Any,
    api_key: str,
    model: str,
) -> None:
    """真实调一次 work 模型，确认"回一个词"。对应 Go ``doctorModelCall``。"""
    from atri_x.app.models import app_llm_request_options, new_provider_llm
    from atri_x.llm.openai import RequestOptions

    if not str(getattr(provider, "base_url", "") or "").strip():
        raise InvalidError("provider base_url is empty")
    if not (model or "").strip():
        raise InvalidError("work model is empty")
    # 密钥由调用方解析（先 api_key 再 api_key_env）；适配器只读 provider.api_key。
    import dataclasses

    try:
        provider = dataclasses.replace(provider, api_key=api_key)
    except TypeError:
        provider.api_key = api_key
    client = new_provider_llm(
        provider_name,
        provider,
        RequestOptions(
            first_chunk_timeout=15.0, stream_idle_timeout=15.0, max_retries=0
        ),
    )
    stream = client.chat_stream(
        ctx,
        ChatRequest(
            model=model,
            messages=[
                LLMMessage(
                    role=Role.USER,
                    segments=[
                        MessageSegment(
                            type=SegmentType.TEXT,
                            text="Reply with exactly one word: pong",
                        )
                    ],
                )
            ],
        ),
    )
    iterator = stream.__aiter__()
    while True:
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            break
        if getattr(chunk, "error", None) is not None:
            raise chunk.error
        if str(getattr(chunk, "delta_content", "") or "").strip():
            return
    raise InvalidError("model stream completed without a text response")


# ---------------------------------------------------------------------------
# CLI 端到端往返
# ---------------------------------------------------------------------------


async def doctor_cli_round_trip(ctx: Any, cfg: Any, timeout: float) -> None:
    """对应 Go ``doctorCLIRoundTrip``：连 CLI 服务端发一条探针消息。

    ``atri_x.platform.cli`` 延迟导入；未交付时抛 :class:`atri_x.errors.InvalidError`，
    由主流程记成 ``e2e`` 失败项。
    """
    import importlib

    raw = (getattr(cfg, "platform", None) or {}).get("cli")
    if not isinstance(raw, dict) or not raw:
        raise InvalidError("cli platform is not configured")
    try:
        cli = importlib.import_module("atri_x.platform.cli")
    except Exception as exc:  # noqa: BLE001 - 并行移植期允许缺件
        raise InvalidError(f"cli platform module is unavailable: {exc}") from exc
    cli_cfg = cli.new_config_from_platform_config(raw)
    if not bool(getattr(cli_cfg, "enabled", False)):
        raise InvalidError("cli platform is disabled")
    _name, client_cfg = cli_cfg.client("")
    url = str(getattr(client_cfg, "url", "") or "").strip()
    if not url:
        raise InvalidError("cli client URL is empty")
    config_dir = os.path.dirname(str(getattr(cfg, "config_path", "") or ""))
    token = ""
    for env_name in getattr(client_cfg, "token_env", None) or []:
        value = config_env(env_name, config_dir)
        if value is not None and value.strip():
            token = value.strip()
            break
    if not token:
        raise InvalidError("cli client token is not configured")

    from atri_x.net.websocket import connect

    client = await connect(url, open_timeout=float(timeout))
    try:
        await client.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "client_id": str(getattr(client_cfg, "id", "") or ""),
                    "token": token,
                },
                ensure_ascii=False,
            )
        )
        hello = _decode(await client.recv())
        if hello.get("type") == "error":
            raise InvalidError(f"cli server rejected connection: {hello.get('text', '')}")
        if hello.get("type") != "hello_ok":
            raise InvalidError(
                f"unexpected cli server hello type {hello.get('type')!r}"
            )
        probe = "doctor-probe-" + str(new_error_id()).replace("-", "")
        await client.send_text(
            json.dumps(
                {
                    "type": "input",
                    "text": "请只回复这个标记，不要添加其他内容：" + probe,
                },
                ensure_ascii=False,
            )
        )
        expected = probe.lower()
        stream_matched = False
        streamed = ""
        while True:
            message = _decode(await client.recv())
            kind = message.get("type", "")
            body = str(message.get("text", "") or "")
            if kind == "chat":
                text = body.strip()
                if not text:
                    raise InvalidError("cli server returned an empty chat message")
                if expected in text.lower():
                    return
                raise InvalidError(
                    f"cli server returned an unexpected reply {text!r}, "
                    f"want probe {probe!r}"
                )
            if kind in ("stream_append", "stream_replace"):
                streamed = streamed + body if kind == "stream_append" else body
                if expected in streamed.lower():
                    stream_matched = True
                continue
            if kind == "stream_finish":
                if stream_matched:
                    return
                raise InvalidError(
                    f"cli stream finished without the expected probe reply {probe!r}"
                )
            if kind == "error":
                raise InvalidError(f"cli round-trip error: {body}")
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结论
            pass


def _decode(frame: Any) -> dict[str, Any]:
    """把一帧解码成 JSON 对象（二进制帧按 UTF-8 解）。"""
    if isinstance(frame, (bytes, bytearray)):
        frame = frame.decode("utf-8", "replace")
    try:
        value = json.loads(frame)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidError(f"decode cli frame: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidError("decode cli frame: not a JSON object")
    return value


# 交叉检索：``NotFoundError`` 供需要区分 404 的调用方复用。
_NOT_FOUND = NotFoundError

# Go 命名别名。
checkWritableDirs = check_writable_dirs
resolveDoctorAPIKey = resolve_doctor_api_key
doctorHealthURL = doctor_health_url
probeApplicationHealth = probe_application_health
resolveDoctorOpsToken = resolve_doctor_ops_token
fetchHealthSnapshot = fetch_health_snapshot
enabledPlatformNames = enabled_platform_names
doctorPlatformConnection = doctor_platform_connection
snapshotHasPlatform = snapshot_has_platform
doctorModelCall = doctor_model_call
doctorCLIRoundTrip = doctor_cli_round_trip
firstLine = first_line
