"""CLI 远程客户端入口：把 CLI 平台配置成"连远端服务"的瘦客户端。

对应 Go：``internal/app/cli_client.go``。

**延迟导入**：``atri_x.platform.cli`` 由同僚并行移植，可能在 import ``atri_x.app``
时尚未存在，因此本模块顶层不 import 它 —— 只在真正要建客户端时才导入。

与 Go 的差异：

* Go 的 ``ErrCLIClientFallback`` 是哨兵 ``error``；Python 用
  :data:`atri_x.errors.ErrCLIClientFallback`（模块级单例异常），调用方用
  ``is`` 判定。
* ``isLocalCLIURL`` 用 :func:`urllib.parse.urlsplit` 复刻 Go ``url.Parse``。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from atri_x.app.support import maybe_await
from atri_x.config import load as load_config
from atri_x.errors import ErrCLIClientFallback, InvalidError

__all__ = [
    "CLIClientOptions",
    "RunCLIClient",
    "run_cli_client",
    "TryRunCLIClient",
    "try_run_cli_client",
    "newCLIClient",
    "new_cli_client",
    "isLocalCLIURL",
    "is_local_cli_url",
]


@dataclass
class CLIClientOptions:
    """对应 Go ``CLIClientOptions``。"""

    config_path: str = ""
    client_name: str = ""


def _cli_module() -> Any:
    """延迟导入 CLI 平台模块；缺件时给出可读错误。"""
    import importlib

    try:
        return importlib.import_module("atri_x.platform.cli")
    except Exception as exc:  # noqa: BLE001 - 并行移植期允许缺件
        raise InvalidError(f"cli platform module is unavailable: {exc}") from exc


def new_cli_client(opts: CLIClientOptions) -> Any:
    """构造远程 CLI 客户端。对应 Go ``newCLIClient``。"""
    cli = _cli_module()
    cfg = load_config(opts.config_path)
    raw = (getattr(cfg, "platform", None) or {}).get("cli")
    # 命名按 PORTING.md §2：函数用 snake_case（`NewConfigFromPlatformConfig` 是类型/
    # 构造函数名的大小写惯例，函数一律 snake_case）。
    cli_cfg = cli.new_config_from_platform_config(raw)
    if not bool(getattr(cli_cfg, "enabled", False)):
        raise InvalidError("cli platform is disabled")
    import os

    return cli.new_remote_client(
        cli.RemoteClientOptions(
            config=cli_cfg,
            config_dir=os.path.dirname(str(cfg.config_path or "")),
            client_name=opts.client_name,
        )
    )


async def run_cli_client(ctx: Any, opts: CLIClientOptions) -> None:
    """对应 Go ``RunCLIClient``。"""
    client = new_cli_client(opts)
    return await maybe_await(client.run(ctx))


async def try_run_cli_client(ctx: Any, opts: CLIClientOptions) -> None:
    """对应 Go ``TryRunCLIClient``。

    构造失败且未点名 client 时抛 :data:`atri_x.errors.ErrCLIClientFallback`，
    让调用方回落到"本机全前台模式"。运行失败但 URL 是本机回环地址时同样回落。
    """
    try:
        client = new_cli_client(opts)
    except Exception:
        if not opts.client_name:
            raise ErrCLIClientFallback from None
        raise
    try:
        return await maybe_await(client.run(ctx))
    except Exception:
        if not opts.client_name and is_local_cli_url(_client_url(client)):
            raise ErrCLIClientFallback from None
        raise


def _client_url(client: Any) -> str:
    url = getattr(client, "url", None)
    if callable(url):
        return str(url() or "")
    return str(url or "")


def is_local_cli_url(raw: str) -> bool:
    """对应 Go ``isLocalCLIURL``：``localhost`` 或回环 IP。"""
    try:
        parsed = urlsplit((raw or "").strip())
    except ValueError:
        return False
    host = parsed.hostname or ""
    if host == "localhost" or host == "":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


RunCLIClient = run_cli_client
TryRunCLIClient = try_run_cli_client
newCLIClient = new_cli_client
isLocalCLIURL = is_local_cli_url
