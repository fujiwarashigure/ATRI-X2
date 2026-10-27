"""协议端管理层：按配置选后端（NapCat / SnowLuma），统一 prepare/launch/stop/status。

分工：

* :mod:`atri_x.gui.qqproto`：**NapCat 专用**（内置包探测、令牌、onebot11/webui 配置）；
* :mod:`atri_x.gui.snowluma_proto`：**SnowLuma 专用**（只读它的配置、按其目录启动）；
* 本模块：把"用户配的是哪一套"解析出来，分派给对应实现，并负责把它的实际监听地址
  与令牌**补**进 ATRI-X 自己的配置（只填空值，绝不覆盖用户已写好的值）。

约定：协议端类型只影响"怎么接入"，不影响平台身份——``platform.name()`` 恒为
``qqonebot``，权限作用域键里不出现后端名。
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Mapping

from atri_x.gui import configedit, qqproto, snowluma_proto
from atri_x.gui.proto_process import managed_status
from atri_x.gui.proto_setup import ProtoSetup
from atri_x.platform.qq_onebot.backend import (
    LEGACY_DEFAULT_BACKEND,
    NEW_INSTALL_BACKEND,
    PROTOCOL_BACKEND_SNOWLUMA,
    resolve_protocol_backend,
)

__all__ = [
    "apply_connection",
    "configured_backend",
    "launch",
    "prepare",
    "status",
    "stop",
    "webui_url",
]

#: ``[platform.qqonebot]`` 的小节名。
PLATFORM_SECTION = qqproto.PLATFORM_SECTION


def configured_backend(app_toml: str = "") -> str:
    """读 ``[platform.qqonebot].protocol_backend``，并按"这份配置算不算新装"解析。

    三态与 :func:`atri_x.platform.qq_onebot.backend.resolve_protocol_backend` 一致，
    但**整个小节都不存在**时按"新装"处理，返回 :data:`NEW_INSTALL_BACKEND`
    （默认 NapCat，随包内置、开箱即用）。为什么要和"键缺失"分开：

    * 键缺失、小节存在 = 1.0.x 老配置（当年只有 NapCat）→ 升级**不自动切换**；
    * 整个小节不存在 = 这台机器还没配过 QQ 接入 = 新装 → 与「平台」页的预选规则
      （:func:`atri_x.gui.tabs.platform._display_backend`）保持同一个默认值。

    SnowLuma 是可选的备用协议端：只有用户在「平台」页显式选择 ``snowluma``
    并保存后，这里才会返回 SnowLuma。1.1.0 的发行包只内置 NapCat，因此默认路径
    不需要用户额外安装任何东西。
    """
    if not app_toml or not os.path.isfile(app_toml):
        return NEW_INSTALL_BACKEND
    try:
        data = configedit.read_toml(app_toml)
    except configedit.ConfigEditError:
        return NEW_INSTALL_BACKEND
    platform = data.get("platform")
    section = platform.get("qqonebot") if isinstance(platform, Mapping) else None
    if not isinstance(section, Mapping):
        return NEW_INSTALL_BACKEND
    raw = section.get("protocol_backend")
    try:
        return resolve_protocol_backend(raw)
    except Exception:  # noqa: BLE001 - 配置非法时按旧行为兜底，报错留给装配期
        return LEGACY_DEFAULT_BACKEND


def prepare(
    config_dir: str = "",
    *,
    bundle_dir: str = "",
    app_toml: str = "",
    snowluma_dir: str = "",
) -> ProtoSetup:
    """准备当前配置的协议端环境（幂等；没有内置环境时一个文件都不写）。"""
    if configured_backend(app_toml) == PROTOCOL_BACKEND_SNOWLUMA:
        return _with_bundled_napcat_hint(snowluma_proto.prepare(snowluma_dir or "", app_toml))
    return qqproto.ensure_qqproto(config_dir, bundle_dir=bundle_dir, app_toml=app_toml)


def _with_bundled_napcat_hint(setup: ProtoSetup) -> ProtoSetup:
    """SnowLuma 不可用、但本程序包**内置**了 NapCat 时，在原因里补一句可切换提示。

    SnowLuma 是**可选的备用协议端**（外置接入，发行包不内置），而默认协议端是同一个
    包里内置的 NapCat：用户显式选了 SnowLuma 却没装好时，「在线」页只会说"未找到
    SnowLuma"。补这句文案，提示"其实包里那套默认协议端就能直接用"。只改 ``reason``
    文案，不改变可用性判定，也不自动改用户的协议端选择。
    """
    if setup.available or setup.backend != PROTOCOL_BACKEND_SNOWLUMA:
        return setup
    try:
        bundled = qqproto.bundle_env_root("")
    except Exception:  # noqa: BLE001 - 提示失败不能影响桌面端启动
        bundled = ""
    if not bundled:
        return setup
    hint = "；本程序包内置 NapCat：到「平台」页把协议端切成 NapCat 并重启桌面端即可使用"
    return replace(setup, reason=f"{setup.reason}{hint}" if setup.reason else hint)


def launch(setup: ProtoSetup) -> tuple[bool, str]:
    """启动协议端（按后端分派）。"""
    if setup.backend == PROTOCOL_BACKEND_SNOWLUMA:
        return snowluma_proto.launch(setup)
    return qqproto.launch_background(setup)


def stop(setup: ProtoSetup) -> tuple[bool, str]:
    """停止**本程序启动的**协议端（按 PID 归属，绝不按进程名）。"""
    if setup.backend == PROTOCOL_BACKEND_SNOWLUMA:
        return snowluma_proto.stop(setup)
    return qqproto.stop_background(setup)


def status(setup: ProtoSetup) -> dict[str, Any]:
    """托管状态（进程是否还在、是不是我们起的）。"""
    info = managed_status(setup.env_dir)
    info["backend"] = setup.backend
    return info


def webui_url(setup: ProtoSetup) -> str:
    """打开协议端 WebUI 的地址。

    NapCat 的 ``webui_url`` 里带一次性 token；SnowLuma 的 WebUI 是账号密码登录
    （首启密码只打印在它自己的控制台里），所以不能像 NapCat 那样"带令牌一键打开"。
    """
    return setup.webui_url or ""


def apply_connection(
    setup: ProtoSetup, app_toml: str = "", env_path: str = ""
) -> bool:
    """把协议端的实际监听地址/令牌**补**进 ATRI-X 的配置；返回是否有改动。

    只填**空值**：用户已经写好的 ``ws_url`` / 令牌一律不动——"切换协议端不覆盖地址
    令牌"这条是硬约束。改动统一走 :func:`configedit.patch_section`（原子写 + 备份）。
    """
    if not setup.available or not app_toml or not os.path.isfile(app_toml):
        return False
    try:
        data = configedit.read_toml(app_toml)
    except configedit.ConfigEditError:
        return False
    platform = data.get("platform")
    section = platform.get("qqonebot") if isinstance(platform, Mapping) else None
    section_exists = isinstance(section, Mapping)
    section = section if section_exists else {}
    values: dict[str, Any] = {}
    if not section_exists:
        # 整段新建：必须把"自家默认值"一次写全，尤其 ``enabled = false``。
        # doctor 对"有段但缺 enabled"视为**启用**；只补 ws_url 会造出一个"看起来
        # 已配置、实际没启用"的半成品段，后台服务会莫名其妙去连它。
        values.update(
            {
                "enabled": False,
                "protocol_backend": setup.backend,
                "api_timeout_seconds": 15,
                "trigger_keywords": list(qqproto.DEFAULT_TRIGGER_KEYWORDS),
                "send_file_mode": "base64",
                "inbound_dedup_enabled": True,
            }
        )
    if not str(section.get("ws_url") or section.get("url") or "").strip() and setup.ws_url:
        values["ws_url"] = setup.ws_url
    token_env = str(section.get("access_token_env") or "").strip()
    token_value = str(section.get("access_token") or "").strip()
    if setup.token and not token_env and not token_value:
        # 协议端要令牌、ATRI-X 侧还没配：用它的令牌名 + 写进 .env（不写明文进 app.toml）。
        values["access_token_env"] = qqproto.ONEBOT_TOKEN_ENV
    if not values:
        return False
    try:
        changed = configedit.patch_section(app_toml, PLATFORM_SECTION, values)
    except configedit.ConfigEditError:
        return False
    if changed and "access_token_env" in values and env_path:
        try:
            configedit.set_env_value(env_path, qqproto.ONEBOT_TOKEN_ENV, setup.token)
        except configedit.ConfigEditError:
            pass
    return changed
