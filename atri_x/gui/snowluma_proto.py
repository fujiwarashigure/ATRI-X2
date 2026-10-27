"""SnowLuma 协议端环境的探测与托管（可选的备用协议端，外置，**不内置分发**）。

ATRI-X 1.1.0 的**默认协议端是随包内置的 NapCat**；SnowLuma 只在用户显式到
「平台」页选择 ``snowluma`` 之后才会被使用，属于备用方案。

与 NapCat 的托管方向正好相反：

* NapCat：ATRI-X **写**它的 ``onebot11.json`` / ``webui.json``（内置包不带令牌）；
* SnowLuma：ATRI-X **只读**它的 ``config/onebot_<QQ号>.json``，把端口与令牌取回来
  填进自己的配置——绝不改第三方文件。

许可边界（本版本不变）：SnowLuma 是非商业源码可见许可 + 专有原生组件，EULA §5.4
明确"并入第三方安装包 / 镜像 / 自动化脚本部署须事先书面授权"。因此发行包**不内置**
SnowLuma，本模块只处理"用户自己装好了 SnowLuma、ATRI-X 去接入它"这一种形态。

首启闸门：SnowLuma 第一次启动要先同意 EULA/隐私、再设 WebUI 密码，初始密码只打印在
它自己的控制台里。ATRI-X **不代用户同意**（那是替用户接受第三方协议），只在界面上
提示"先去它的控制台完成首启"，``ProtoSetup.needs_consent`` 用来表达这一点。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from atri_x.gui import proto_process
from atri_x.gui.proto_setup import ProtoSetup
from atri_x.platform.qq_onebot.backend import PROTOCOL_BACKEND_SNOWLUMA

__all__ = [
    "CONSENT_NAME",
    "DEFAULT_WEBUI_PORT",
    "ENV_VAR",
    "ONEBOT_CONFIG_PREFIX",
    "find_dir",
    "is_snowluma_dir",
    "launch",
    "prepare",
    "read_endpoint",
    "read_webui_port",
    "stop",
]

#: 用户可显式指定 SnowLuma 目录（优先于自动探测）。
ENV_VAR = "ATRIX_SNOWLUMA_DIR"

#: 每个 QQ 账号一份配置：``config/onebot_<uin>.json``；全局回落在 ``config/onebot.json``。
ONEBOT_CONFIG_PREFIX = "onebot_"
DEFAULT_ONEBOT_CONFIG = "onebot.json"
WEBUI_CONFIG = "webui.json"
#: EULA/隐私同意状态文件（存在且为接受态时视为已同意）。
CONSENT_NAME = "consent.json"

#: SnowLuma WebUI 默认端口。
DEFAULT_WEBUI_PORT = 5099
#: SnowLuma 默认 OneBot 正向 WS 端口（读不到配置时用它提示）。
DEFAULT_ONEBOT_PORT = 3001

#: 自动探测的常见安装位置（用户自备，不随发行包分发）。
_CANDIDATE_DIRS = (
    "%LOCALAPPDATA%/SnowLuma",
    "%LOCALAPPDATA%/Programs/SnowLuma",
    "%USERPROFILE%/SnowLuma",
    "C:/SnowLuma",
)


def find_dir(explicit: str = "") -> str:
    """定位 SnowLuma 安装目录；``""`` 表示没找到。

    顺序：显式参数 → ``ATRIX_SNOWLUMA_DIR`` → 常见安装位置。
    """
    for candidate in (
        explicit,
        os.environ.get(ENV_VAR, ""),
        *(os.path.expandvars(item) for item in _CANDIDATE_DIRS),
    ):
        path = (candidate or "").strip()
        if path and is_snowluma_dir(path):
            return os.path.abspath(os.path.expanduser(path))
    return ""


def is_snowluma_dir(path: str) -> bool:
    """``path`` 是不是一个 SnowLuma 运行时目录（``index.mjs`` + ``package.json``）。"""
    root = (path or "").strip()
    if not root or not os.path.isdir(root):
        return False
    return os.path.isfile(os.path.join(root, "index.mjs")) and os.path.isfile(
        os.path.join(root, "package.json")
    )


def read_endpoint(env_dir: str) -> tuple[int, str]:
    """读 SnowLuma 的配置，返回 ``(正向 WS 端口, access token)``；读不到返回 ``(0, "")``。

    每账号一份 ``config/onebot_<uin>.json``，**覆盖**全局 ``config/onebot.json``；
    取修改时间最新的那份。只读，不改。
    """
    config_dir = os.path.join(env_dir or "", "config")
    data = _load_latest_config(config_dir)
    if not data:
        return 0, ""
    networks = data.get("networks")
    servers = networks.get("wsServers") if isinstance(networks, dict) else None
    if not isinstance(servers, list):
        return 0, ""
    for server in servers:
        if not isinstance(server, dict):
            continue
        port = _as_int(server.get("port"))
        if port <= 0:
            continue
        return port, str(server.get("accessToken") or "").strip()
    return 0, ""


def read_webui_port(env_dir: str) -> int:
    """WebUI 端口（``config/webui.json`` 的 ``port``，缺省 5099）。"""
    path = os.path.join(env_dir or "", "config", WEBUI_CONFIG)
    data = _load_json(path)
    port = _as_int(data.get("port"))
    return port if port > 0 else DEFAULT_WEBUI_PORT


def prepare(env_dir: str = "", app_toml: str = "") -> ProtoSetup:
    """把"能连上用户自备的 SnowLuma"这件事描述出来；**不写任何第三方文件**。

    ``app_toml`` 只用于判断"这台机器上有没有历史配置"（给 ``changed`` 用），
    这里不改配置：地址与令牌由调用方（``proto_manager`` / ``service``）按
    "只填空值"的规则补，绝不覆盖用户已经写好的值。
    """
    root = find_dir(env_dir)
    if not root:
        return ProtoSetup(
            available=False,
            backend=PROTOCOL_BACKEND_SNOWLUMA,
            reason=(
                "未找到 SnowLuma：请自行安装 SnowLuma 并设环境变量 "
                f"{ENV_VAR} 指向它的安装目录（发行包不内置 SnowLuma）"
            ),
        )
    port, token = read_endpoint(root)
    config_dir = os.path.join(root, "config")
    webui_port = read_webui_port(root)
    consent = _consent_state(root)
    reason = ""
    if port <= 0:
        reason = (
            "SnowLuma 还没有可用的 OneBot 正向 WS 配置：先在它的 WebUI 里启用一个 "
            "WebSocket 服务端（默认端口 3001），再回本页"
        )
    return ProtoSetup(
        available=port > 0,
        reason=reason,
        backend=PROTOCOL_BACKEND_SNOWLUMA,
        env_dir=root,
        config_dir=config_dir,
        onebot_port=port or DEFAULT_ONEBOT_PORT,
        webui_port=webui_port,
        webui_url=f"http://127.0.0.1:{webui_port}/",
        token=token,
        changed=False,
        needs_consent=not consent,
        external=True,
    )


def launch(setup: ProtoSetup) -> tuple[bool, str]:
    """托管启动 SnowLuma：``<dir>/node.exe index.mjs``，**工作目录必须是安装目录**。

    为什么不用 ``launcher.bat``：它没有 ``cd /d %~dp0``（用裸 ``node``），且结尾
    ``pause`` 会挡住脚本化；配置与日志都相对 CWD，所以这里显式指定 ``cwd``。
    """
    if not setup.available and not setup.env_dir:
        return False, setup.reason or "未找到 SnowLuma 安装目录"
    node = os.path.join(setup.env_dir, "node.exe")
    if not os.path.isfile(node):
        node = "node"
    index = os.path.join(setup.env_dir, "index.mjs")
    if not os.path.isfile(index):
        return False, f"SnowLuma 目录里没有 index.mjs：{setup.env_dir}"
    ok, detail = proto_process.launch_console(
        setup.env_dir,
        PROTOCOL_BACKEND_SNOWLUMA,
        [node, "index.mjs"],
        cwd=setup.env_dir,
        detail=(
            "已启动 SnowLuma（新的控制台窗口）；首次启动要在那个窗口里同意 EULA/隐私"
            " 并设置 WebUI 密码，然后加载注入 QQ"
        ),
    )
    if not ok:
        return ok, detail
    if setup.needs_consent:
        return ok, detail + "。注意：还没完成首启同意流程，SnowLuma 可能直接退出"
    return ok, detail


def stop(setup: ProtoSetup) -> tuple[bool, str]:
    """只停本程序启动的 SnowLuma 进程树。"""
    return proto_process.stop_managed(setup.env_dir)


def _load_latest_config(config_dir: str) -> dict[str, Any]:
    """取"最新的那份" onebot 配置：``onebot_<uin>.json`` 优先于 ``onebot.json``。"""
    candidates: list[Path] = []
    try:
        for entry in Path(config_dir).glob(ONEBOT_CONFIG_PREFIX + "*.json"):
            if entry.is_file():
                candidates.append(entry)
    except OSError:
        return {}
    global_path = Path(config_dir) / DEFAULT_ONEBOT_CONFIG
    candidates.sort(key=lambda path: _mtime(path), reverse=True)
    for path in candidates:
        data = _load_json(str(path))
        if data:
            return data
    if global_path.is_file():
        return _load_json(str(global_path))
    return {}


def _consent_state(env_dir: str) -> bool:
    """读 EULA/隐私同意状态；文件缺失或格式不认识一律当作"还没同意"。"""
    data = _load_json(os.path.join(env_dir, "config", CONSENT_NAME))
    if not data:
        return False
    for key in ("accepted", "eulaAccepted", "privacyAccepted", "agreed"):
        value = data.get(key)
        if value is True:
            return True
        if isinstance(value, dict) and any(item is True for item in value.values()):
            return True
    return False


def _load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
