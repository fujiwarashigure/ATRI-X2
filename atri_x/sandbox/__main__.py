"""``python -m atri_x.sandbox``：沙箱运维命令（安装版用 ``-m`` 同样可跑）。

子命令：

```text
status [--json]                 后端/探测/队列/环境/账本快照
list [--json]                   列出运行中的环境
probe [--json]                  隔离能力实测（不启动租户环境）
reset --platform P --bot B --scope S --user U [--mode restart|rebuild|wipe]
gc                              立刻跑一轮空闲回收/过期清理
```

所有输出默认 ASCII 安全（GBK 控制台不会因为中文崩）；``status --json`` 供 GUI/脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import json

from atri_x.sandbox.doctor import check as doctor_check
from atri_x.sandbox.errors import SandboxError
from atri_x.sandbox.runtime import build_service, clear_service
from atri_x.sandbox.tenant import tenant_from_ids

__all__ = ["main"]


def _print_json(payload: dict) -> None:
    """输出 JSON（``ensure_ascii`` 避免 GBK 控制台编码错误）。"""
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _service(section: object = None):
    """按默认配置构造服务；不可用时抛错。"""
    if section is None:
        from atri_x.config import default as default_config

        section = getattr(default_config(), "sandbox", None)
    service = build_service(section)
    if service is None:
        raise SystemExit("沙箱未启用（[sandbox].mode=off）")
    return service


async def _status(service: object, *, json_output: bool) -> int:
    status = await service.status(refresh=True)  # type: ignore[attr-defined]
    if json_output:
        _print_json(status)
    else:
        backend = status.get("backend") or {}
        print(f"sandbox backend : {backend.get('backend', '?')} "
              f"{'available' if backend.get('available') else 'UNAVAILABLE'}")
        probe = status.get("probe") or {}
        print(f"isolation probe : {'ok' if probe.get('ok') else 'not run/failed'}")
        queue = status.get("queue") or {}
        print(f"queue           : running={queue.get('running')} waiting={queue.get('waiting')} "
              f"capacity={queue.get('capacity')}")
        print(f"environments    : {len(status.get('environments') or [])}")
        for item in status.get("environments") or []:
            print(f"  - {item.get('tenant')} running={item.get('running')} "
                  f"idle={item.get('idle_seconds')}s net={item.get('network')}")
    return 0


async def _list(service: object, *, json_output: bool) -> int:
    status = await service.status(refresh=True)  # type: ignore[attr-defined]
    envs = status.get("environments") or []
    if json_output:
        _print_json({"environments": envs})
    else:
        for item in envs:
            print(f"{item.get('container')} tenant={item.get('tenant')} "
                  f"running={item.get('running')} idle={item.get('idle_seconds')}s")
        if not envs:
            print("(no environments)")
    return 0


async def _probe(*, json_output: bool) -> int:
    report = await doctor_check(probe=True)
    if json_output:
        _print_json(report)
    else:
        print("sandbox ready" if report.get("ok") else "sandbox NOT ready")
        for item in report.get("checks") or []:
            print(f"  [{'OK' if item['ok'] else 'FAIL'}] {item['name']} — {item.get('detail', '')}")
    return 0 if report.get("ok") else 1


async def _reset(args: argparse.Namespace) -> int:
    service = _service()
    tenant = tenant_from_ids(args.platform, args.bot, args.scope, args.user)
    result = await service.reset_tenant(tenant, mode=args.mode)  # type: ignore[attr-defined]
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


async def _gc(service: object) -> int:
    await service._gc_once()  # noqa: SLF001 - 运维子命令
    print("gc done")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(prog="atri_x.sandbox")
    sub = parser.add_subparsers(dest="command")
    for name in ("status", "list", "probe"):
        item = sub.add_parser(name)
        item.add_argument("--json", action="store_true")
    reset = sub.add_parser("reset")
    reset.add_argument("--platform", default="local")
    reset.add_argument("--bot", default="local")
    reset.add_argument("--scope", default="local")
    reset.add_argument("--user", default="local")
    reset.add_argument("--mode", default="rebuild", choices=("restart", "rebuild", "wipe"))
    sub.add_parser("gc")
    args = parser.parse_args(argv)
    command = args.command or "status"
    try:
        if command == "probe":
            return asyncio.run(_probe(json_output=bool(args.json)))
        if command == "reset":
            return asyncio.run(_reset(args))
        service = _service()
        try:
            if command == "list":
                return asyncio.run(_list(service, json_output=bool(args.json)))
            if command == "gc":
                return asyncio.run(_gc(service))
            return asyncio.run(_status(service, json_output=bool(args.json)))
        finally:
            clear_service()
    except SandboxError as exc:
        print(f"sandbox error: {exc}")
        return 1
    except SystemExit as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
