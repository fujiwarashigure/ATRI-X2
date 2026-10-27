"""``atri-x.exe -m atri_x.gui.qqproto`` 的命令行入口。

单独成模块的原因：``atri_x/gui/qqproto.py`` 已经接近单文件行数上限（准备 NapCat 环境
本身就是一大摊事），命令行的参数解析与 ``--stop`` / ``--status`` 分支放这里更清楚。
入口仍然挂在 ``atri_x.gui.qqproto.main``，``-m atri_x.gui.qqproto`` 的调用方式不变。

``--stop`` / ``--status`` 是打包期生成的 ``Stop-NapCat.bat`` 的落点：那个脚本只知道
协议端目录，令牌/端口都不重要，重要的是**只停本程序启动的那个进程树**（按 PID 归属）。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from atri_x.gui import configedit, proto_process
from atri_x.gui.proto_setup import ProtoSetup
from atri_x.gui.qqproto import (
    bundle_env_root,
    ensure_qqproto,
    setup_summary,
    stop_background,
)

__all__ = ["main"]


def _log(message: str) -> None:
    """ASCII 兜底输出（GBK 控制台不因中文抛异常）。"""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """准备 / 停止 / 查询内置协议端；返回进程退出码。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.gui.qqproto",
        description="准备/停止内置 NapCat 协议端环境（令牌、端口、NapCat 与 app.toml 配置）",
    )
    parser.add_argument("--config", default="", help="app.toml 路径（默认按查找顺序）")
    parser.add_argument("--bundle-dir", default="", help="程序目录（默认自动推断）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument(
        "--stop", action="store_true", help="停止本程序启动的协议端（按 PID 归属，不按进程名）"
    )
    parser.add_argument("--status", action="store_true", help="只读查询托管状态")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.stop or args.status:
        env_root = bundle_env_root(args.bundle_dir)
        if not env_root:
            _log("内置协议端：不可用（本程序未内置协议端环境）")
            return 1
        if args.status:
            info = proto_process.managed_status(env_root)
            if args.json:
                _log(json.dumps({"envDir": env_root, **info}, ensure_ascii=False, sort_keys=True))
            else:
                state = "运行中（本程序托管）" if info.get("managed") else "未托管/未运行"
                _log(f"协议端进程：{state}（{info.get('reason') or ''}）".rstrip())
            return 0
        ok, detail = stop_background(ProtoSetup(available=True, env_dir=env_root))
        _log(detail)
        return 0 if ok else 1

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


if __name__ == "__main__":  # pragma: no cover - 由 qqproto.main 转发
    raise SystemExit(main())
