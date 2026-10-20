"""内置 QQ 协议端环境（NapCat + 便携 QQ）的首次运行准备。

发行包（便携版 / 安装版）在打包时可以把一套 NapCat Shell 环境放进程序目录的
``qqproto\\``（见 ``packaging/qqproto.py``）。本模块负责把那份**不带任何令牌**的
环境变成一台可用的本机协议端：

1. 生成或复用 OneBot 令牌，写进配置目录 ``.env`` 的 ``QQONEBOT_ACCESS_TOKEN``
   （后台服务据此连协议端，所以它必须和 NapCat 的 ``onebot11.json`` 一致）；
2. 挑端口：优先沿用 ``app.toml`` 已配的**本机** ``ws_url`` 端口与上一次的
   ``onebot11.json`` / ``webui.json``；都没有才挑空闲端口（默认 3001 / 6099）；
3. 写 NapCat 的 ``onebot11.json``（正向 WebSocket 服务端 127.0.0.1）与
   ``webui.json``（扫码登录页）；
4. ``app.toml`` 缺 ``[platform.qqonebot]`` 时补一段指向本机协议端的默认值
   （``enabled = false``）。**已有值一律不动**：用户接外部 NapCat 的配置不会被覆盖。

安全边界：不下载、不启动进程、不碰 QQ 的用户数据目录；只在发行包自带环境存在时
动作。没内置环境的构建（例如 ``--no-gui`` 或用户自备 NapCat）调用本模块会得到
``available=False``，且**一个文件都不写**。

也可以单独跑（打包自检与排障用，冻结后在 exe 里同样可用）::

    atri-x.exe -m atri_x.gui.qqproto --json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atri_x.gui import configedit

__all__ = [
    "BUNDLE_ENV_VAR",
    "DEFAULT_ONEBOT_PORT",
    "DEFAULT_WEBUI_PORT",
    "ENV_DIR_NAME",
    "ONEBOT_TOKEN_ENV",
    "PLATFORM_SECTION",
    "QqProtoSetup",
    "bundle_env_root",
    "ensure_qqproto",
    "find_shell",
    "launch_background",
    "main",
    "pick_free_port",
    "setup_summary",
]

#: 发行包里内置协议端环境的目录名（相对程序目录）。
ENV_DIR_NAME = "qqproto"

#: 开发/排障时用环境变量显式指定协议端环境目录（优先于程序目录下的 ``qqproto``）。
BUNDLE_ENV_VAR = "ATRIX_QQPROTO_DIR"

#: NapCat Shell 目录名（一键安装器解出来的那个）与它里面的引导程序。
SHELL_GLOB = "NapCat*.Shell"
SHELL_EXE = "NapCatWinBootMain.exe"

#: OneBot 令牌的环境变量名（``.env`` 里的键，也是 ``[platform.qqonebot].access_token_env``）。
ONEBOT_TOKEN_ENV = "QQONEBOT_ACCESS_TOKEN"

#: 协议端默认端口（OneBot 正向 WS / NapCat WebUI）。
DEFAULT_ONEBOT_PORT = 3001
DEFAULT_WEBUI_PORT = 6099

#: ``app.toml`` 里 QQ OneBot 平台的小节名。
PLATFORM_SECTION = "platform.qqonebot"

#: 默认唤醒词（与 ``packaging/README.md`` 的说明一致；用户可改）。
DEFAULT_TRIGGER_KEYWORDS = ("atri",)

#: NapCat 的配置文件（相对 ``resources\\app\\napcat\\config``）。
ONEBOT_CONFIG_NAME = "onebot11.json"
WEBUI_CONFIG_NAME = "webui.json"

#: WebSocket 服务端条目名：换个名字会多出一条服务端配置，所以固定用这个。
SERVER_NAME = "atri-x"

#: ``launch_background`` 用的 Windows ``CREATE_NEW_CONSOLE``：协议端要有自己的控制台窗口，
#: 关掉它就等于下线，这一点必须让用户看得见。
_CREATE_NEW_CONSOLE = 0x00000010


@dataclass(frozen=True)
class QqProtoSetup:
    """内置协议端环境的准备结果（纯数据，不依赖 Tk）。"""

    available: bool = False
    reason: str = ""
    env_dir: str = ""
    shell_dir: str = ""
    config_dir: str = ""
    start_script: str = ""
    stop_script: str = ""
    onebot_port: int = 0
    webui_port: int = 0
    webui_url: str = ""
    #: OneBot 令牌；``repr=False`` 避免被日志/异常顺手打出来。
    token: str = field(default="", repr=False)
    changed: bool = False

    @property
    def ws_url(self) -> str:
        """后台服务该连的地址（协议端没准备出来时为空串）。"""
        return f"ws://127.0.0.1:{self.onebot_port}/" if self.onebot_port else ""

    @property
    def label(self) -> str:
        """一句话描述，给「在线」页用。"""
        if not self.available:
            return self.reason or "未内置协议端环境"
        return f"内置 NapCat（端口 {self.onebot_port}）"


# ---------------------------------------------------------------------------
# 目录定位
# ---------------------------------------------------------------------------


def _is_shell(path: str) -> bool:
    """``path`` 是不是一个 NapCat Shell 根目录。"""
    return os.path.isfile(os.path.join(path, SHELL_EXE)) and os.path.isdir(
        os.path.join(path, "versions")
    )


def find_shell(env_root: str) -> str:
    """在 ``env_root``（``qqproto`` 目录）里找 NapCat Shell 目录；找不到返回空串。"""
    root = os.path.abspath(os.path.expanduser(env_root)) if env_root else ""
    if not root or not os.path.isdir(root):
        return ""
    if _is_shell(root):
        return root
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ""
    for name in names:
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate) and _is_shell(candidate):
            return candidate
    return ""


def bundle_env_root(bundle_dir: str = "") -> str:
    """解析内置协议端环境目录（``ATRIX_QQPROTO_DIR`` 优先，其次程序目录下 ``qqproto``）。

    返回空串表示"这个程序没有内置协议端环境"，调用方什么都不该写。
    """
    candidates: list[str] = []
    explicit = (os.environ.get(BUNDLE_ENV_VAR) or "").strip()
    if explicit:
        candidates.append(explicit)
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, ENV_DIR_NAME))
    else:
        from atri_x.gui.process import app_dir  # 延迟导入：纯逻辑用例不必带 GUI 层

        candidates.append(os.path.join(app_dir(), ENV_DIR_NAME))
    for candidate in candidates:
        try:
            shell = find_shell(candidate)
        except OSError:
            shell = ""
        if shell:
            return os.path.abspath(os.path.expanduser(candidate))
    return ""


def napcat_app_dir(shell_dir: str) -> str:
    """``<shell>\\versions\\<QQ 版本>\\resources\\app\\napcat``；找不到返回空串。"""
    versions = os.path.join(shell_dir, "versions")
    try:
        names = sorted(os.listdir(versions))
    except OSError:
        return ""
    for name in names:
        app = os.path.join(versions, name, "resources", "app", "napcat")
        if os.path.isfile(os.path.join(app, "napcat.mjs")):
            return app
    return ""


# ---------------------------------------------------------------------------
# 端口与 JSON 小工具
# ---------------------------------------------------------------------------


def _port_bindable(host: str, port: int) -> bool:
    """本机回环上这个端口能不能绑（只探测，不监听）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, int(port)))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def pick_free_port(host: str, preferred: int, span: int = 20) -> int:
    """从 ``preferred`` 起挑一个能被绑定的端口（全被占则返回 ``preferred`` 本身）。"""
    for offset in range(max(1, span)):
        candidate = int(preferred) + offset
        if candidate > 65535:
            break
        if _port_bindable(host, candidate):
            return candidate
    return int(preferred)


def _url_port(url: str) -> tuple[str, int]:
    """``ws://host:port/path`` → ``(host, port)``；解析不出来返回 ``("", 0)``。"""
    text = (url or "").strip()
    if not text:
        return "", 0
    rest = text.split("://", 1)[-1]
    host_port = rest.split("/", 1)[0]
    host, _, port_text = host_port.rpartition(":")
    if not host:
        return "", 0
    try:
        return host, int(port_text)
    except ValueError:
        return "", 0


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _read_json(path: str) -> dict[str, Any]:
    """读 JSON 对象；文件不存在/坏了都当空对象（协议端配置允许重新生成）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _write_json(path: str, data: Mapping[str, Any]) -> bool:
    """整份写 JSON；内容没变返回 ``False``（幂等，不刷文件时间）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except OSError:
        pass
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return True


# ---------------------------------------------------------------------------
# 配置准备
# ---------------------------------------------------------------------------


def _ensure_token(env_path: str) -> tuple[str, bool]:
    """取（或生成）OneBot 令牌；返回 ``(令牌, 是否新写入)``。

    优先级：进程环境变量 → 配置目录 ``.env`` → 生成 32 位十六进制写进 ``.env``。
    用户已经为**外部** NapCat 配过令牌时会沿用同一个，不覆盖。
    """
    value = (os.environ.get(ONEBOT_TOKEN_ENV) or "").strip()
    if value:
        return value, False
    if env_path:
        existing = (configedit.read_env(env_path).get(ONEBOT_TOKEN_ENV) or "").strip()
        if existing:
            return existing, False
    token = secrets.token_hex(16)
    if env_path:
        configedit.set_env_value(env_path, ONEBOT_TOKEN_ENV, token)
    return token, True


def _configured_onebot_port(app_toml: str) -> int:
    """``app.toml`` 里已配的**本机** ``ws_url`` 端口（非回环地址视为没配）。"""
    if not app_toml or not os.path.isfile(app_toml):
        return 0
    try:
        data = configedit.read_toml(app_toml)
    except configedit.ConfigEditError:
        return 0
    platform = data.get("platform")
    section = platform.get("qqonebot") if isinstance(platform, Mapping) else None
    if not isinstance(section, Mapping):
        return 0
    host, port = _url_port(str(section.get("ws_url") or ""))
    return port if port and _is_loopback(host) else 0


def _ensure_onebot(config_dir: str, token: str, port_hint: int) -> tuple[int, bool]:
    """写 NapCat 的 ``onebot11.json`` 正向 WS 服务端；返回 ``(端口, 是否改动)``。"""
    path = os.path.join(config_dir, ONEBOT_CONFIG_NAME)
    data = _read_json(path)
    network = data.get("network") if isinstance(data.get("network"), Mapping) else {}
    servers = network.get("websocketServers")
    servers = list(servers) if isinstance(servers, list) else []

    index = -1
    current: Mapping[str, Any] = {}
    for position, item in enumerate(servers):
        if isinstance(item, Mapping) and str(item.get("name") or "") == SERVER_NAME:
            index, current = position, item
            break
    try:
        existing_port = int(current.get("port") or 0)
    except (TypeError, ValueError):
        existing_port = 0
    port = int(port_hint) or existing_port or pick_free_port("127.0.0.1", DEFAULT_ONEBOT_PORT)

    desired = {
        "name": SERVER_NAME,
        "enable": True,
        "host": "127.0.0.1",
        "port": port,
        "messagePostFormat": "array",
        "reportSelfMessage": False,
        "token": token,
        "enableForcePushEvent": True,
        "debug": False,
        "heartInterval": 30000,
    }
    merged = dict(current)
    merged.update(desired)
    if index >= 0:
        servers[index] = merged
    else:
        servers.append(merged)
    network = dict(network)
    network["websocketServers"] = servers
    data["network"] = network
    return port, _write_json(path, data)


def _ensure_webui(config_dir: str) -> tuple[int, str, bool]:
    """写 NapCat 的 ``webui.json``；返回 ``(端口, 令牌, 是否改动)``。"""
    path = os.path.join(config_dir, WEBUI_CONFIG_NAME)
    data = _read_json(path)
    host = str(data.get("host") or "127.0.0.1")
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    port = port or pick_free_port("127.0.0.1", DEFAULT_WEBUI_PORT)
    token = str(data.get("token") or "") or secrets.token_hex(8)

    defaults: dict[str, Any] = {
        "host": host,
        "port": port,
        "token": token,
        "loginRate": 10,
        "autoLoginAccount": "",
        "disableWebUI": False,
        "accessControlMode": "none",
        "ipWhitelist": [],
        "ipBlacklist": [],
        "enableXForwardedFor": False,
        "enable2FA": False,
        "totpSecret": "",
    }
    merged = dict(defaults)
    merged.update(data)
    merged["host"] = host
    merged["port"] = port
    merged["token"] = token
    return port, token, _write_json(path, merged)


def _ensure_platform_section(app_toml: str, port: int) -> bool:
    """``app.toml`` 缺 ``[platform.qqonebot]`` / 键时补默认值；返回是否改动。"""
    if not app_toml or not os.path.isfile(app_toml) or not port:
        return False
    try:
        data = configedit.read_toml(app_toml)
    except configedit.ConfigEditError:
        return False
    platform = data.get("platform")
    section = platform.get("qqonebot") if isinstance(platform, Mapping) else None
    desired: dict[str, Any] = {
        "enabled": False,
        "ws_url": f"ws://127.0.0.1:{int(port)}/",
        "access_token_env": ONEBOT_TOKEN_ENV,
        "api_timeout_seconds": 15,
        "trigger_keywords": list(DEFAULT_TRIGGER_KEYWORDS),
        "send_file_mode": "base64",
        "inbound_dedup_enabled": True,
    }
    if not isinstance(section, Mapping):
        return configedit.patch_section(
            app_toml,
            PLATFORM_SECTION,
            desired,
            header_comment=(
                "# QQ 群机器人（本程序包内置 NapCat；换外部协议端时只改 ws_url）"
            ),
        )
    missing: dict[str, Any] = {}
    for key, value in desired.items():
        current = section.get(key)
        if key == "ws_url":
            if not str(current or "").strip():
                missing[key] = value
        elif current is None:
            missing[key] = value
    if not missing:
        return False
    return configedit.patch_section(app_toml, PLATFORM_SECTION, missing)


def ensure_qqproto(
    config_dir: str, *, bundle_dir: str = "", app_toml: str = ""
) -> QqProtoSetup:
    """确保内置协议端可用；幂等，**没有内置环境时一个文件都不写**。

    参数：

    * ``config_dir``：用户配置目录（``.env`` 所在；空串表示不落令牌文件）；
    * ``bundle_dir``：程序目录（默认按 :func:`atri_x.gui.process.app_dir` 推）；
    * ``app_toml``：``app.toml`` 路径（缺省不补平台小节）。
    """
    env_root = bundle_env_root(bundle_dir)
    if not env_root:
        return QqProtoSetup(
            available=False,
            reason="本程序未内置 QQ 协议端环境（qqproto）；外部 NapCat 请照旧手动配置",
        )
    shell = find_shell(env_root)
    app_dir = napcat_app_dir(shell) if shell else ""
    if not shell or not app_dir:
        return QqProtoSetup(
            available=False,
            env_dir=env_root,
            reason="内置 qqproto 目录不完整：缺 NapCat 引导程序或 versions\\...\\resources\\app\\napcat",
        )
    napcat_config = os.path.join(app_dir, "config")
    try:
        os.makedirs(napcat_config, exist_ok=True)
    except OSError as exc:
        return QqProtoSetup(
            available=False,
            env_dir=env_root,
            shell_dir=shell,
            reason=f"协议端配置目录不可写：{exc}",
        )

    changed = False
    env_path = os.path.join(config_dir, ".env") if config_dir else ""
    token, token_changed = _ensure_token(env_path)
    changed = changed or token_changed
    port_hint = _configured_onebot_port(app_toml)
    onebot_port, onebot_changed = _ensure_onebot(napcat_config, token, port_hint)
    changed = changed or onebot_changed
    webui_port, webui_token, webui_changed = _ensure_webui(napcat_config)
    changed = changed or webui_changed
    changed = _ensure_platform_section(app_toml, onebot_port) or changed

    return QqProtoSetup(
        available=True,
        env_dir=env_root,
        shell_dir=shell,
        config_dir=napcat_config,
        start_script=os.path.join(env_root, "Start-NapCat.bat"),
        stop_script=os.path.join(env_root, "Stop-NapCat.bat"),
        onebot_port=onebot_port,
        webui_port=webui_port,
        webui_url=f"http://127.0.0.1:{webui_port}/webui?token={webui_token}",
        token=token,
        changed=changed,
    )


# ---------------------------------------------------------------------------
# 启动 / 展示
# ---------------------------------------------------------------------------


def launch_background(setup: QqProtoSetup) -> tuple[bool, str]:
    """在新控制台窗口里启动内置协议端（保留窗口：关窗即下线）。"""
    if not setup.available:
        return False, setup.reason or "本程序未内置 QQ 协议端环境"
    script = setup.start_script
    if not script or not os.path.isfile(script):
        return False, f"缺少协议端启动脚本：{script or '（未知）'}"
    flags = _CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        subprocess.Popen(  # noqa: S603 - 命令行是本模块写死的批处理路径
            ["cmd", "/c", script],
            cwd=setup.shell_dir or setup.env_dir,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        return False, f"启动协议端失败：{exc}"
    return True, "已启动内置 NapCat（新的控制台窗口）；扫码登录后回本页点「连接（上线）」"


def setup_summary(setup: QqProtoSetup) -> dict[str, Any]:
    """给 ``--json`` / 打包自检用的摘要：**不含令牌明文**。"""
    return {
        "available": setup.available,
        "reason": setup.reason,
        "envDir": setup.env_dir,
        "shellDir": setup.shell_dir,
        "configDir": setup.config_dir,
        "onebotPort": setup.onebot_port,
        "webuiPort": setup.webui_port,
        "wsUrl": setup.ws_url,
        "webuiUrl": f"http://127.0.0.1:{setup.webui_port}/webui" if setup.webui_port else "",
        "tokenSet": bool(setup.token),
        "changed": setup.changed,
    }


def _log(message: str) -> None:
    """ASCII 兜底输出（GBK 控制台不因中文抛异常）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """``atri-x.exe -m atri_x.gui.qqproto [--json]`` 的入口；可用返回 0。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.gui.qqproto",
        description="准备内置 NapCat/QQ 协议端环境（令牌、端口、NapCat 与 app.toml 配置）",
    )
    parser.add_argument("--config", default="", help="app.toml 路径（默认按查找顺序）")
    parser.add_argument("--bundle-dir", default="", help="程序目录（默认自动推断）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config_path = (args.config or "").strip() or configedit.default_config_path()
    config_dir = os.path.dirname(os.path.abspath(config_path)) if config_path else ""
    setup = ensure_qqproto(config_dir, bundle_dir=args.bundle_dir, app_toml=config_path)
    summary = setup_summary(setup)
    if args.json:
        _log(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    elif setup.available:
        _log(f"内置协议端：可用（{setup.env_dir}）")
        _log(f"  OneBot：ws://127.0.0.1:{setup.onebot_port}/ · WebUI：{summary['webuiUrl']}")
        _log(f"  配置{'已更新' if setup.changed else '已是最新'}")
    else:
        _log(f"内置协议端：不可用（{setup.reason}）")
    return 0 if setup.available else 1


if __name__ == "__main__":
    raise SystemExit(main())
