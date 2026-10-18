"""GUI 的「服务侧」准备：配置、token、后台服务的探测与拉起。

架构（见 ``docs/windows-app.md``）：**GUI 不跑 Agent，Agent 跑在 ``atri-x service run``
里**。GUI 只是一个 WS 前端（协议见 ``docs/frontend-api.md``），窗口关掉不影响服务，
QQ 机器人因此可以继续在线。本模块负责这条链路的前半段：

1. 确保配置目录存在（缺文件按发行资产补齐）；
2. 确保 ``[platform.cli.server].enabled = true``（默认是 ``false``，只在 service 模式生效）；
3. 确保本地客户端 token 存在（优先用 ``.env`` / 环境变量，没有就生成一个写进 ``.env``）；
4. 探测 ``ws://…/cli/v1/ws``；连不上就后台拉起 ``atri-x service run``。

**对用户配置的写入只有两处**，都可解释、可回滚：

- ``app.toml``：把 ``[platform.cli.server]`` 的 ``enabled`` 置为 ``true``（先备份 ``.bak``，
  写完用 :mod:`tomllib` 重新解析校验）；
- ``.env``：写入/更新一行 ``ATRIX_CLI_LOCAL_TOKEN=<随机值>``（本机回环服务端的登录 token）。

不做的事：不生成模型配置、不改 ``[platform.cli.server].listen``、不猜用户的 provider。
进程/路径相关助手在 :mod:`atri_x.gui.process`（这里重新导出几个常用的，方便调用方）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from atri_x.gui.process import (  # noqa: F401 - 这些名字属于本模块对外的对外接口
    app_dir,
    application_paths,
    cleanup_service_pid,
    focus_existing_window,
    is_process_alive,
    open_path,
    read_service_pid,
    service_log_path,
    single_instance_lock,
    state_dir,
    stop_service,
    write_service_pid,
)
from atri_x.net.websocket import connect_sync

__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_TOKEN_ENV",
    "GuiConfigError",
    "GuiSettings",
    "app_dir",
    "application_paths",
    "ensure_server_enabled",
    "ensure_token",
    "ensure_tokens_section",
    "focus_existing_window",
    "inspect_tokens",
    "open_path",
    "prepare_settings",
    "probe_server",
    "read_service_pid",
    "restart_service",
    "service_command",
    "service_log_path",
    "single_instance_lock",
    "spawn_service",
    "state_dir",
    "stop_service",
    "wait_until_ready",
]

#: 默认的 CLI 客户端 id（对应 ``[platform.cli.server.tokens]`` 的键）。
DEFAULT_CLIENT_ID = "local"

#: 默认的 token 环境变量名（发行资产里 ``tokens.local`` 指向它）。
DEFAULT_TOKEN_ENV = "ATRIX_CLI_LOCAL_TOKEN"

#: ``app.toml`` 里控制远程 CLI 服务端的键路径。
_SERVER_SECTION = "[platform.cli.server]"
_TOKENS_SECTION = "[platform.cli.server.tokens]"

#: Windows ``subprocess`` 的进程创建标志。
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class GuiConfigError(Exception):
    """配置层可预期的失败（消息直接展示给用户）。"""


@dataclass(frozen=True)
class GuiSettings:
    """GUI 跑起来所需的全部本地信息。"""

    config_path: str
    config_dir: str
    url: str
    client_id: str
    token: str
    token_env: str
    #: 本次启动是否改过配置（界面上提示「已自动开启本地服务端口」）。
    changed: bool = False

    @property
    def data_dir(self) -> str:
        """ATRI-X 的用户数据目录（``%APPDATA%\\ATRI-X``）。"""
        return self.config_dir


# ---------------------------------------------------------------------------
# 配置准备
# ---------------------------------------------------------------------------


def prepare_settings(config_path: str = "") -> GuiSettings:
    """准备配置与 token，返回 :class:`GuiSettings`。

    幂等：配置已经正确时不会写任何文件（``changed=False``）。
    """
    from atri_x.config import loader

    if config_path:
        resolved = os.path.abspath(os.path.expanduser(config_path))
        if not os.path.isfile(resolved):
            raise GuiConfigError(f"配置文件不存在：{resolved}")
    else:
        try:
            resolved = loader.ensure_platform_defaults()
        except Exception as exc:  # noqa: BLE001 - 配置层报错统一转成可展示的失败
            raise GuiConfigError(f"无法创建默认配置：{exc}") from exc
    config_dir = os.path.dirname(os.path.normpath(resolved))

    changed = False
    text = _read_text(resolved)
    patched, server_changed = ensure_server_enabled(text)
    if server_changed:
        _backup_once(resolved)
        _write_text(resolved, patched)
        changed = True

    data = _load_toml(_read_text(resolved), resolved)
    client_id, token_envs = inspect_tokens(data)
    if not client_id:
        patched, tokens_changed = ensure_tokens_section(_read_text(resolved))
        if tokens_changed:
            _backup_once(resolved)
            _write_text(resolved, patched)
            changed = True
        data = _load_toml(_read_text(resolved), resolved)
        client_id, token_envs = inspect_tokens(data)
        if not client_id:
            raise GuiConfigError(
                "配置里没有可用的 [platform.cli.server.tokens]；请在 app.toml 里至少配置一个客户端 id"
            )

    token_env, token, token_changed = ensure_token(config_dir, token_envs)
    changed = changed or token_changed
    url = _server_url(data)
    return GuiSettings(
        config_path=resolved,
        config_dir=config_dir,
        url=url,
        client_id=client_id,
        token=token,
        token_env=token_env,
        changed=changed,
    )


def ensure_server_enabled(text: str) -> tuple[str, bool]:
    """把 ``[platform.cli.server].enabled`` 置为 ``true``；返回 (新文本, 是否改动)。

    只动这一行：保留注释、缩进和其他键；段落不存在时在文件末尾补一个。
    """
    lines = text.splitlines()
    header = _find_header(lines, _SERVER_SECTION)
    if header is None:
        block = ["", _SERVER_SECTION, "enabled = true"]
        return "\n".join(lines + block) + "\n", True
    end = _section_end(lines, header)
    for index in range(header + 1, end):
        line = lines[index]
        if not re.match(r"^\s*enabled\s*=", line):
            continue
        if re.match(r"^\s*enabled\s*=\s*true\b", line):
            return text, False
        lines[index] = re.sub(r"(^\s*enabled\s*=\s*)false\b", r"\g<1>true", line, count=1)
        return "\n".join(lines) + "\n", True
    lines.insert(header + 1, "enabled = true")
    return "\n".join(lines) + "\n", True


def ensure_tokens_section(text: str) -> tuple[str, bool]:
    """确保 ``[platform.cli.server.tokens]`` 里有一个本地客户端；返回 (新文本, 是否改动)。"""
    lines = text.splitlines()
    header = _find_header(lines, _TOKENS_SECTION)
    entry = f'{DEFAULT_CLIENT_ID} = ["{DEFAULT_TOKEN_ENV}"]'
    if header is None:
        return "\n".join(lines + ["", _TOKENS_SECTION, entry]) + "\n", True
    end = _section_end(lines, header)
    for index in range(header + 1, end):
        if re.match(r"^\s*[^\s#].*=", lines[index]):
            return text, False
    lines.insert(header + 1, entry)
    return "\n".join(lines) + "\n", True


def inspect_tokens(data: Mapping[str, Any]) -> tuple[str, list[str]]:
    """从已解析的配置里取 ``(client_id, token 环境变量名列表)``。

    取第一个**配置了非空环境变量名**的客户端；没有则返回 ``("", [])``。
    """
    platform = data.get("platform") if isinstance(data, Mapping) else None
    cli = platform.get("cli") if isinstance(platform, Mapping) else None
    server = cli.get("server") if isinstance(cli, Mapping) else None
    tokens = server.get("tokens") if isinstance(server, Mapping) else None
    if not isinstance(tokens, Mapping):
        return "", []
    for client_id, value in tokens.items():
        names = _string_list(value)
        if names:
            return str(client_id), names
    return "", []


def ensure_token(config_dir: str, env_names: list[str]) -> tuple[str, str, bool]:
    """取（或生成）本地登录 token；返回 ``(环境变量名, token, 是否新写入)``。

    优先级：进程环境变量 → 配置目录 ``.env`` → 生成新值写进 ``.env``。
    """
    for name in env_names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return name, value, False
    env_path = os.path.join(config_dir, ".env")
    values = _read_env_file(env_path)
    for name in env_names:
        value = (values.get(name) or "").strip()
        if value:
            return name, value, False
    name = env_names[0] if env_names else DEFAULT_TOKEN_ENV
    token = "atrix-gui-" + secrets.token_urlsafe(24)
    _upsert_env_line(env_path, name, token)
    os.environ[name] = token
    return name, token, True


# ---------------------------------------------------------------------------
# 服务探测 / 拉起
# ---------------------------------------------------------------------------


def probe_server(url: str, client_id: str, token: str, timeout: float = 2.0) -> tuple[bool, str]:
    """连一次 WS 并握手；成功返回 ``(True, "")``，失败返回 ``(False, 原因)``。"""
    try:
        with connect_sync(url, open_timeout=timeout) as websocket:
            websocket.send_text(
                json.dumps({"type": "hello", "client_id": client_id, "token": token})
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                kind = str(message.get("type") or "")
                if kind == "hello_ok":
                    return True, ""
                if kind == "error":
                    return False, str(message.get("text") or "服务端拒绝握手")
    except Exception as exc:  # noqa: BLE001 - 探测失败只关心原因
        return False, f"{type(exc).__name__}: {exc}"
    return False, "等待 hello_ok 超时"


def service_command(settings: GuiSettings) -> list[str]:
    """后台服务的启动命令（冻结包用同目录的控制台 exe）。"""
    _directory, console = application_paths()
    if console:
        return [console, "service", "run", "--config", settings.config_path]
    return [sys.executable, "-m", "atri_x", "service", "run", "--config", settings.config_path]


def spawn_service(settings: GuiSettings) -> subprocess.Popen[bytes]:
    """后台拉起 ``service run``，输出重定向到 ``gui/service.out.log``。

    用 ``DETACHED_PROCESS`` 让服务不挂在 GUI 的进程组上：GUI 退出后服务继续跑。
    """
    log_path = service_log_path()
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    handle = open(log_path, "ab", buffering=0)
    handle.write(_log_header(settings).encode("utf-8"))
    env = dict(os.environ)
    env.setdefault(settings.token_env, settings.token)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    flags = _DETACHED_PROCESS | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(  # noqa: S603 - 命令行由本模块构造
            service_command(settings),
            cwd=app_dir(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=flags if os.name == "nt" else 0,
        )
    finally:
        handle.close()
    write_service_pid(process.pid)
    return process


def wait_until_ready(settings: GuiSettings, timeout: float = 25.0) -> tuple[bool, str]:
    """轮询探测直到服务就绪（或超时）；返回 ``(就绪, 最后一次失败原因)``。"""
    deadline = time.monotonic() + timeout
    reason = ""
    while time.monotonic() < deadline:
        ok, reason = probe_server(settings.url, settings.client_id, settings.token, timeout=1.5)
        if ok:
            return True, ""
        time.sleep(0.4)
    return False, reason or "服务未在限定时间内就绪"


def restart_service(settings: GuiSettings, timeout: float = 30.0) -> tuple[bool, str]:
    """停止再拉起后台服务，返回 ``(是否就绪, 原因)``。

    用于"改完启动期配置（providers / .env / app.toml）后应用改动"。Windows 上没有
    可靠的"给另一个控制台进程发 Ctrl+C"手段，所以这里和菜单里的停止一样是强制结束；
    差别只是紧接着重新拉起并等它就绪。调用方应当放到后台线程里（会阻塞到就绪/超时）。
    """
    stop_service()
    try:
        spawn_service(settings)
    except Exception as exc:  # noqa: BLE001 - 结果直接展示给用户
        return False, f"启动后台服务失败：{exc}"
    return wait_until_ready(settings, timeout=timeout)


# ---------------------------------------------------------------------------
# 内部：文件读写 / TOML 细节
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise GuiConfigError(f"读配置失败：{exc}") from exc


def _write_text(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _backup_once(path: str) -> str:
    """第一次改动前留一份 ``<配置>.bak``（不覆盖已有的备份）。"""
    backup = path + ".bak"
    if os.path.exists(backup):
        return backup
    try:
        _write_text(backup, _read_text(path))
    except OSError:
        pass
    return backup


def _load_toml(text: str, path: str) -> Mapping[str, Any]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GuiConfigError(f"解析配置失败（{path}）：{exc}") from exc
    return data if isinstance(data, Mapping) else {}


def _server_url(data: Mapping[str, Any]) -> str:
    """按配置推 ws URL。

    优先级：**本机服务端监听地址**（``[platform.cli.server].enabled`` 时以 ``listen`` 为准）
    → 默认客户端 profile 的 ``url`` → ``[platform.cli].default_url`` → 内置默认。

    为什么服务端优先：``default_url`` 的语义是"没有自己 url 的客户端连哪里"，而 GUI 是
    本机前端；用户把 ``server.listen`` 改到别的端口却忘了同步 ``default_url`` 时，按
    ``default_url`` 连会一直"已断开"（实测踩过）。显式客户端 profile 的 ``url`` 仍然优先
    于监听地址（那是"我要连别的机器"的意图）。
    """
    platform = data.get("platform") if isinstance(data, Mapping) else None
    cli = platform.get("cli") if isinstance(platform, Mapping) else None
    if not isinstance(cli, Mapping):
        return "ws://127.0.0.1:32172/cli/v1/ws"
    default_client = str(cli.get("default_client") or "local").strip() or "local"
    clients = cli.get("clients") if isinstance(cli.get("clients"), Mapping) else {}
    profile = clients.get(default_client) if isinstance(clients, Mapping) else None
    if isinstance(profile, Mapping):
        explicit = str(profile.get("url") or "").strip()
        if explicit:
            return explicit
    server = cli.get("server") if isinstance(cli.get("server"), Mapping) else {}
    if bool(server.get("enabled")):
        listen = str(server.get("listen") or "").strip()
        if listen:
            return _url_from_listen(listen)
    url = str(cli.get("default_url") or "").strip()
    if url:
        return url
    listen = str(server.get("listen") or "").strip()
    if listen:
        return _url_from_listen(listen)
    return "ws://127.0.0.1:32172/cli/v1/ws"


def _url_from_listen(listen: str) -> str:
    """``host:port`` → ``ws://host:port/cli/v1/ws``（``0.0.0.0`` 换回回环）。"""
    host, _, port = listen.rpartition(":")
    host = host or "127.0.0.1"
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    return f"ws://{host}:{port or '32172'}/cli/v1/ws"


def _find_header(lines: list[str], header: str) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == header:
            return index
    return None


def _section_end(lines: list[str], header: int) -> int:
    for index in range(header + 1, len(lines)):
        text = lines[index].strip()
        if text.startswith("[") and text.endswith("]"):
            return index
    return len(lines)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _read_env_file(path: str) -> dict[str, str]:
    """读 ``.env``（首次出现者生效，与 :func:`atri_x.config.loader.load_env_file` 一致）。"""
    values: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _upsert_env_line(path: str, name: str, value: str) -> None:
    """写/改 ``.env`` 的一行（保留其他行与注释；UTF-8 无 BOM、LF）。"""
    lines: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    entry = f"{name}={value}"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == name:
            lines[index] = entry
            break
    else:
        lines.append(entry)
    _write_text(path, "\n".join(lines) + "\n")


def _log_header(settings: GuiSettings) -> str:
    moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"\n=== {moment} 启动后台服务 ===\n"
        f"配置：{settings.config_path}\n"
        f"地址：{settings.url}\n"
    )
