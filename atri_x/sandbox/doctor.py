"""沙箱自检：后端 / 隔离 / 资源限制 / 网络 / 清理能力的实测入口。

用法（源码运行与冻结产物一致）：

```text
python -m atri_x.sandbox.doctor              # 人类可读
python -m atri_x.sandbox.doctor --json       # 机器可读（GUI / 安装校验用）
python -m atri_x.sandbox.doctor --no-probe   # 只查引擎与目录，不跑隔离探测容器
atri-x.exe -m atri_x.sandbox.doctor --json   # 安装版
```

退出码：0 = 可用；1 = 有必需项失败（沙箱会 fail-closed）；2 = 配置无效。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from atri_x.sandbox.backend_container import ContainerBackend
from atri_x.sandbox.errors import SandboxError
from atri_x.sandbox.settings import from_config

__all__ = ["check", "main"]


def _section_from_config() -> Any:
    """从默认配置里取 ``[sandbox]`` 段（不依赖完整 AppConfig 装配）。"""
    try:
        from atri_x.config import default as default_config

        cfg = default_config()
    except Exception:  # noqa: BLE001 - 取不到默认配置时返回空段
        return None
    return getattr(cfg, "sandbox", None)


async def check(
    section: Any = None,
    *,
    probe: bool = True,
    root_override: str = "",
) -> dict:
    """跑一遍沙箱自检，返回结构化结论。"""
    section = _section_from_config() if section is None else section
    try:
        settings = from_config(section, root_override=root_override)
    except SandboxError as exc:
        return {
            "ok": False,
            "mode": "invalid",
            "error": str(exc),
            "checks": [{"name": "config", "ok": False, "detail": str(exc)}],
        }
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})

    add("config", True, f"mode={settings.mode} profile={settings.profile} network={settings.network}")
    add("mode", settings.mode in ("container", "off"), settings.mode)
    if settings.mode == "off":
        add(
            "sandbox_mode",
            False,
            "[sandbox].mode=off：显式宿主 Shell 模式（不安全，不启用沙箱）",
            required=False,
        )
        return {"ok": True, "mode": "off", "settings": settings.dump(), "checks": checks}

    backend = ContainerBackend(settings)
    info = await backend.backend_info()
    add("engine", info.available, info.version if info.available else info.error)
    for path_name, path in (
        ("root", settings.root),
        ("audit", os.path.dirname(settings.audit_path)),
        ("image_context", settings.image_context_dir),
    ):
        try:
            os.makedirs(path, exist_ok=True)
            probe_file = os.path.join(path, ".atri_write_probe")
            with open(probe_file, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.unlink(probe_file)
            add(f"writable_{path_name}", True, path)
        except OSError as exc:
            add(f"writable_{path_name}", False, f"{path}: {exc}")
    if info.available:
        try:
            image = await backend.ensure_image()
            add("image", True, image)
        except SandboxError as exc:
            add("image", False, str(exc))
            image = ""
        if probe and image:
            report = await backend.probe()
            add("isolation_probe", report.ok, f"{len(report.checks)} checks")
            for item in report.checks:
                add(f"probe:{item.name}", item.ok, item.detail, required=item.required)
        elif not probe:
            add("isolation_probe", True, "skipped (--no-probe)", required=False)
    add(
        "queue",
        settings.max_environments >= 1 and settings.queue_max_size >= 1,
        f"max_environments={settings.max_environments} queue={settings.queue_max_size}",
    )
    add(
        "resource_limits",
        settings.resources.memory_mb >= 128 and settings.resources.pids >= 16,
        f"memory={settings.resources.memory_mb}MiB pids={settings.resources.pids} disk={settings.resources.disk_mb}MiB",
    )
    add(
        "network_policy",
        settings.network_policy.tier in ("offline", "public", "restricted"),
        f"tier={settings.network_policy.tier} "
        f"daily={settings.network_policy.daily_bytes // 1048576}MiB",
    )
    failed = [item for item in checks if item["required"] and not item["ok"]]
    return {
        "ok": not failed,
        "mode": settings.mode,
        "backend": info.dump(),
        "image": settings.image,
        "settings": settings.dump(),
        "checks": checks,
        "failed": [item["name"] for item in failed],
    }


def format_report(report: dict) -> str:
    """把自检结论渲染成人类可读文本。"""
    lines = ["ATRI-X sandbox doctor", ""]
    lines.append(f"mode: {report.get('mode', '?')}")
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    backend = report.get("backend") or {}
    if backend:
        lines.append(
            "backend: {backend} {version} {state}".format(
                backend=backend.get("backend", "?"),
                version=backend.get("version", ""),
                state="available" if backend.get("available") else "UNAVAILABLE",
            )
        )
    for item in report.get("checks", []):
        mark = "OK  " if item["ok"] else ("FAIL" if item.get("required", True) else "WARN")
        detail = item.get("detail", "")
        name = item.get("name", "")
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    lines.append("")
    lines.append("sandbox ready" if report.get("ok") else "sandbox NOT ready（将 fail-closed 拒绝执行）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(prog="atri_x.sandbox.doctor")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-probe", action="store_true", help="跳过隔离探测容器")
    parser.add_argument("--root", default="", help="覆盖沙箱数据根（测试用）")
    args = parser.parse_args(argv)
    report = asyncio.run(check(probe=not args.no_probe, root_override=args.root))
    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2)
        _write(text)
    else:
        _write(format_report(report))
    if report.get("mode") == "invalid":
        return 2
    return 0 if report.get("ok") else 1


def _write(text: str) -> None:
    """输出（GBK 控制台兜底：不能编码就替换）。"""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
