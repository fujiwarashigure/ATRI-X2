"""把随包 draw.io 技能装进用户配置目录（首次启动自动执行，幂等、可升级、失败不阻断主程序）。

装什么
------

``<配置目录>/ATRI-X/skills/agent/drawio_skill/``
    上游 drawio-skill 3.4.2 的运行文件（``SKILL.md`` + ``atrix_cli.py`` + ``scripts/`` +
    ``references/`` + ``data/`` + ``styles/``）+ 本模块按运行形态生成的
    ``ATRIX_SKILL.toml``。工具名 ``drawio``，**仅超级管理员可见**。
``<配置目录>/ATRI-X/drawio/install.json``
    安装清单（schema ``atrix-drawio/install/1``，``mode="bundled"``）：记录包摘要、
    技能版本、命令形态、渲染器路径。幂等判断与升级都以它为准。

三条硬约束
----------

1. **只走冻结产物自动安装**：源码树默认不自动装（源码用户按 ``docs/diagram.md`` 的
   说明显式执行 ``python -m atri_x.diagram.bootstrap --source``），避免测试/开发时往
   真实配置目录写东西；
2. **渲染器只读、不复制**：draw.io 桌面版解包目录随产物放在 ``_internal/drawio_runtime``，
   本模块只把它的绝对路径记进清单和命令行，不往用户目录复制第二份（约 400 MB）；
3. **失败只报告不抛**：打包资源缺失、磁盘写失败都返回 ``state="failed"``，调用方记一条
   WARNING 即可——图表能力不可用不应该让机器人起不来。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from atri_x.diagram.pack import (
    PACK_META_FILE,
    SKILL_DIR_NAME,
    SKILL_INSTALL_NAME,
    TOOL_FILE,
    is_frozen,
    load_pack_meta,
    load_tool_meta,
    pack_root,
    renderer_exe,
    skill_source_dir,
)
from atri_x.diagram.paths import default_config_dir, drawio_home

__all__ = [
    "BootstrapResult",
    "INSTALL_SCHEMA",
    "MODE_BUNDLED",
    "ensure_installed",
    "main",
    "pack_status",
    "render_skill_manifest",
]

logger = logging.getLogger(__name__)

#: 安装清单 schema。
INSTALL_SCHEMA = "atrix-drawio/install/1"
#: 本模块写出的清单 ``mode``。
MODE_BUNDLED = "bundled"
#: 生成文件的首行标记：升级时只清理带这个标记的旧文件。
GENERATED_MARKER = "# 由 atri_x.diagram.bootstrap 生成"
#: 安装锁文件名（GUI 与后台服务可能同时首次启动）。
LOCK_NAME = ".install.lock"
#: 等锁上限（秒）：超时后仍继续装，靠"先写临时目录再替换"保证结果可用。
LOCK_TIMEOUT = 10.0

_SKILL_DOC = "SKILL.md"
_SKILL_TOML = "ATRIX_SKILL.toml"
_SKILL_ADAPTER = "atrix_cli.py"

#: 技能里必须存在的运行文件（就绪判断用）。
_REQUIRED_FILES = (_SKILL_DOC, _SKILL_ADAPTER, _SKILL_TOML)


@dataclass(frozen=True)
class BootstrapResult:
    """一次 ``ensure_installed`` 的结果（调用方只读，不抛异常）。"""

    #: ``installed`` / ``up-to-date`` / ``skipped`` / ``external`` / ``unavailable`` / ``failed``
    state: str
    home: str = ""
    skill_dir: str = ""
    skill_version: str = ""
    message: str = ""
    #: 工具命令行（调试/清单用），形如 ``{"drawio": [...]}``
    commands: dict[str, list[str]] = field(default_factory=dict)
    renderer: str = ""

    @property
    def ok(self) -> bool:
        """是否处于"draw.io 技能可用"的状态（跳过源码安装不算失败，但也不算已装）。"""
        return self.state in ("installed", "up-to-date", "external")


# ---------------------------------------------------------------------------
# 命令形态
# ---------------------------------------------------------------------------


def _console_executable() -> str:
    """冻结包里"控制台版" exe 的路径。

    GUI 进程的 ``sys.executable`` 是 ``atri-x-gui.exe``（窗口版没有 stdout），
    技能子进程必须用同目录的 ``atri-x.exe``，否则 ``CommandTool`` 读不到 JSON。
    """
    executable = getattr(sys, "executable", "") or ""
    if not executable:
        return "atri-x"
    if is_frozen():
        name = os.path.basename(executable).lower()
        if "gui" in name:
            sibling = os.path.join(os.path.dirname(executable), "atri-x.exe")
            if os.path.isfile(sibling):
                return sibling
    return executable


def skill_base_command(skill_dir: str, *, frozen: bool | None = None) -> list[str]:
    """技能命令前缀（不含动作参数与业务参数）。"""
    use_frozen = is_frozen() if frozen is None else bool(frozen)
    drawio = renderer_exe()
    if use_frozen:
        parts = [
            _console_executable(),
            "-m",
            "atri_x.diagram.runner",
            "--skill-dir",
            skill_dir,
        ]
    else:
        parts = [sys.executable, os.path.join(skill_dir, _SKILL_ADAPTER)]
    if drawio:
        parts.extend(["--drawio-exe", drawio])
    return parts


def toml_command(parts: list[str]) -> str:
    """把命令渲染成 TOML 字符串数组（JSON 转义与 TOML 基本字符串兼容）。"""
    return "[" + ", ".join(json.dumps(part, ensure_ascii=False) for part in parts) + "]"


def render_skill_manifest(tool: dict[str, Any], command: list[str]) -> str:
    """渲染 ``ATRIX_SKILL.toml``（字段集合与 ``agent_manifest`` 允许的 8 个键一致）。"""
    tags = [str(tag) for tag in tool.get("tags") or []]
    parameters = tool.get("parameters") or {}
    lines = [
        f"{GENERATED_MARKER}（来源 drawio_pack/{TOOL_FILE}，升级随包资源时重写）",
        f'risk = "{tool.get("risk") or "low"}"',
        "superadmin_only = " + ("true" if tool.get("superadmin_only") else "false"),
        "tags = [" + ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags) + "]",
        "command = " + toml_command(command),
        f"timeout_seconds = {int(tool.get('timeout_seconds') or 0)}",
        "expose_root = " + ("true" if tool.get("expose_root") else "false"),
        "",
        "parameters = '''",
        json.dumps(parameters, ensure_ascii=False, indent=2),
        "'''",
        "",
        "[args]",
    ]
    for name, flag in (tool.get("args") or {}).items():
        lines.append(f"{name} = {json.dumps(str(flag), ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 文件操作（先写临时副本再替换，两个进程并发时也不会留下半个文件）
# ---------------------------------------------------------------------------


def _write_text(path: str, text: str) -> None:
    """原子写文本（UTF-8、LF、无 BOM）。"""
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _replace_dir(source: Path, target: Path) -> None:
    """用 ``source`` 整体替换 ``target``（Windows 上目录不能原地覆盖，先改名再换）。"""
    staging = target.with_name(f"{target.name}.new-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging)
    if target.exists():
        retired = target.with_name(f"{target.name}.old-{os.getpid()}")
        shutil.rmtree(retired, ignore_errors=True)
        os.replace(target, retired)
        os.replace(staging, target)
        shutil.rmtree(retired, ignore_errors=True)
    else:
        os.replace(staging, target)


@contextlib.contextmanager
def _install_lock(home: str) -> Iterator[bool]:
    """跨进程安装锁；拿不到锁（超时/IO 失败）也继续，返回是否真的持锁。"""
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        yield False
        return
    lock_path = os.path.join(home, LOCK_NAME)
    handle: int | None = None
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode("ascii", "replace"))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                logger.warning("drawio bootstrap: 等安装锁超时，继续执行（结果仍可自愈）")
                break
            time.sleep(0.1)
        except OSError as exc:  # 只读目录等：不阻断，后续写入会给出真实错误
            logger.warning("drawio bootstrap: 无法创建安装锁（%s）", exc)
            break
    try:
        yield handle is not None
    finally:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:  # pragma: no cover - 关闭失败无补救动作
                pass
            try:
                os.unlink(lock_path)
            except OSError:  # pragma: no cover - 已被别的进程清理
                pass


# ---------------------------------------------------------------------------
# 清单与就绪判断
# ---------------------------------------------------------------------------


def _read_install_manifest(home: str) -> dict[str, Any] | None:
    path = os.path.join(home, "install.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _manifest_is_ours(manifest: dict[str, Any] | None) -> bool:
    return bool(manifest) and manifest.get("schema") == INSTALL_SCHEMA and manifest.get(
        "mode"
    ) == MODE_BUNDLED


def _installation_ready(
    skill_dir: str, meta: dict[str, Any], manifest: dict[str, Any] | None
) -> bool:
    """清单摘要一致 + 技能运行文件齐 → 视为已就绪。"""
    if not _manifest_is_ours(manifest):
        return False
    assert manifest is not None  # for type checkers; _manifest_is_ours 已保证
    if manifest.get("pack_digest") != meta.get("digest"):
        return False
    for name in _REQUIRED_FILES:
        if not os.path.isfile(os.path.join(skill_dir, name)):
            return False
    return True


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def _install(
    pack: Path,
    meta: dict[str, Any],
    tool: dict[str, Any],
    skill_source: Path,
    home: str,
    config_dir: str,
    *,
    frozen: bool,
) -> BootstrapResult:
    skill_dir = os.path.join(config_dir, "skills", "agent", SKILL_INSTALL_NAME)
    os.makedirs(os.path.dirname(skill_dir), exist_ok=True)
    # 1) 技能：整体替换，避免升级后残留旧脚本
    _replace_dir(skill_source, Path(skill_dir))
    # 2) 清单：按运行形态生成命令
    tool_name = str(tool.get("tool") or SKILL_INSTALL_NAME)
    command = skill_base_command(skill_dir, frozen=frozen)
    _write_text(os.path.join(skill_dir, _SKILL_TOML), render_skill_manifest(tool, command))

    # 3) 安装清单（最后写：它是"装完了"的判定依据）
    drawio = renderer_exe()
    os.makedirs(home, exist_ok=True)
    manifest = {
        "schema": INSTALL_SCHEMA,
        "mode": MODE_BUNDLED,
        "skill_version": str(meta.get("skill_version") or ""),
        "pack_digest": str(meta.get("digest") or ""),
        "pack_schema": str(meta.get("schema") or ""),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_dir": config_dir,
        "home": home,
        "skill_dir": skill_dir,
        "tool": tool_name,
        "command_mode": "frozen" if frozen else "source",
        "executable": _console_executable(),
        "python": sys.executable,
        "renderer": drawio,
        "commands": {tool_name: command},
    }
    _write_text(
        os.path.join(home, "install.json"),
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    logger.info(
        "drawio bootstrap: 已安装随包 draw.io 技能 %s（工具 %s，命令形态 %s，渲染器 %s）",
        manifest["skill_version"],
        tool_name,
        manifest["command_mode"],
        drawio or "<缺失>",
    )
    return BootstrapResult(
        state="installed",
        home=home,
        skill_dir=skill_dir,
        skill_version=manifest["skill_version"],
        message="随包 draw.io 技能已安装",
        commands={tool_name: command},
        renderer=drawio,
    )


def ensure_installed(
    config_dir: str = "",
    *,
    frozen: bool | None = None,
    allow_source: bool = False,
    force: bool = False,
) -> BootstrapResult:
    """确保 draw.io 技能已装进 ``config_dir``（默认取平台配置目录）。

    ``frozen=None`` 时按当前进程判定；源码树默认返回 ``state="skipped"``，
    需要显式 ``allow_source=True`` 才装（测试与排障用）。
    """
    use_frozen = is_frozen() if frozen is None else bool(frozen)
    base_dir = (config_dir or "").strip() or default_config_dir()
    if not base_dir:
        return BootstrapResult(state="failed", message="无法确定 ATRI-X 配置目录")
    home = drawio_home(base_dir)
    skill_dir = os.path.join(base_dir, "skills", "agent", SKILL_INSTALL_NAME)
    if not use_frozen and not allow_source:
        return BootstrapResult(
            state="skipped",
            home=home,
            skill_dir=skill_dir,
            message="源码模式不自动安装 draw.io 技能（用 python -m atri_x.diagram.bootstrap --source）",
        )
    pack = pack_root()
    meta = load_pack_meta(pack)
    tool = load_tool_meta(pack)
    skill_source = skill_source_dir(pack)
    if pack is None or meta is None or tool is None or skill_source is None:
        return BootstrapResult(
            state="unavailable",
            home=home,
            skill_dir=skill_dir,
            message=f"随包 draw.io 技能缺失（找不到 {PACK_META_FILE} / {TOOL_FILE} / skills/{SKILL_DIR_NAME}）",
        )
    current = _read_install_manifest(home)
    if current is not None and not _manifest_is_ours(current):
        return BootstrapResult(
            state="external",
            home=home,
            skill_dir=str(current.get("skill_dir") or skill_dir),
            skill_version=str(current.get("skill_version") or ""),
            message="已有外部安装的 draw.io 技能，保持不动",
        )
    if not force and _installation_ready(skill_dir, meta, current):
        return BootstrapResult(
            state="up-to-date",
            home=home,
            skill_dir=skill_dir,
            skill_version=str(meta.get("skill_version") or ""),
            message="随包 draw.io 技能已就绪",
            commands={
                k: [str(x) for x in v]
                for k, v in (current or {}).get("commands", {}).items()
            },
            renderer=str((current or {}).get("renderer") or ""),
        )
    try:
        with _install_lock(home) as locked:
            if locked and not force and _installation_ready(
                skill_dir, meta, _read_install_manifest(home)
            ):
                current = _read_install_manifest(home) or {}
                return BootstrapResult(
                    state="up-to-date",
                    home=home,
                    skill_dir=skill_dir,
                    skill_version=str(meta.get("skill_version") or ""),
                    message="随包 draw.io 技能已就绪（另一进程刚装好）",
                    commands={
                        k: [str(x) for x in v]
                        for k, v in (current.get("commands") or {}).items()
                    },
                    renderer=str(current.get("renderer") or ""),
                )
            return _install(
                pack,
                meta,
                tool,
                skill_source,
                home,
                base_dir,
                frozen=use_frozen,
            )
    except OSError as exc:
        logger.warning("drawio bootstrap: 安装失败（%s）", exc)
        return BootstrapResult(state="failed", home=home, message=f"安装失败：{exc}")
    except (ValueError, TypeError) as exc:  # 清单/资源损坏
        logger.warning("drawio bootstrap: 资源不完整（%s）", exc)
        return BootstrapResult(state="failed", home=home, message=f"随包资源不完整：{exc}")


def pack_status(config_dir: str = "") -> dict[str, Any]:
    """随包资源与安装状态快照（诊断用；永不抛异常）。"""
    base_dir = (config_dir or "").strip() or default_config_dir()
    home = drawio_home(base_dir) if base_dir else ""
    pack = pack_root()
    meta = load_pack_meta(pack) or {}
    tool = load_tool_meta(pack) or {}
    current = _read_install_manifest(home) if home else None
    skill_dir = ""
    if _manifest_is_ours(current):
        skill_dir = str((current or {}).get("skill_dir") or "")
    if not skill_dir and base_dir:
        skill_dir = os.path.join(base_dir, "skills", "agent", SKILL_INSTALL_NAME)
    drawio = renderer_exe()
    return {
        "pack_available": pack is not None,
        "pack_dir": str(pack) if pack is not None else "",
        "pack_skill_version": str(meta.get("skill_version") or ""),
        "pack_digest": str(meta.get("digest") or ""),
        "tool": str(tool.get("tool") or SKILL_INSTALL_NAME),
        "home": home,
        "skill_dir": skill_dir,
        "installed": _manifest_is_ours(current),
        "install_mode": str((current or {}).get("mode") or ""),
        "install_schema": str((current or {}).get("schema") or ""),
        "skill_version": str((current or {}).get("skill_version") or ""),
        "up_to_date": bool(current)
        and _manifest_is_ours(current)
        and (current or {}).get("pack_digest") == meta.get("digest"),
        "renderer": drawio,
        "renderer_available": bool(drawio),
        "frozen": is_frozen(),
    }


# ---------------------------------------------------------------------------
# 命令行（冻结产物的构建期核验用：atri-x.exe -m atri_x.diagram.bootstrap --check）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m atri_x.diagram.bootstrap``：安装并打印 JSON 结果。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.diagram.bootstrap",
        description="安装/核对随包 draw.io 技能（ATRI-X 首次启动会自动执行同一逻辑）",
    )
    parser.add_argument("--config-dir", default="", help="ATRI-X 配置目录（默认按平台解析）")
    parser.add_argument("--check", action="store_true", help="只核对并打印 JSON，不强制重装")
    parser.add_argument("--force", action="store_true", help="已就绪也重装（排障用）")
    parser.add_argument("--source", action="store_true", help="源码树也安装（测试/排障用）")
    parser.add_argument("--status", action="store_true", help="只打印状态快照")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(pack_status(args.config_dir), ensure_ascii=False, indent=2))
        return 0
    result = ensure_installed(
        args.config_dir,
        force=args.force,
        allow_source=bool(args.source) or not args.check,
    )
    payload = {
        "ok": result.ok,
        "state": result.state,
        "home": result.home,
        "skill_dir": result.skill_dir,
        "skill_version": result.skill_version,
        "message": result.message,
        "commands": result.commands,
        "renderer": result.renderer,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - 入口在子进程里跑
    raise SystemExit(main())
