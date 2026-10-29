"""把随包研究引擎装进用户配置目录（首次启动自动执行，幂等、可升级、失败不阻断主程序）。

装什么
------

``<配置目录>/ATRI-X/research/engine``
    研究引擎源码（从 ``research_pack/engine`` 复制）；``/research`` 命令与引擎本身
    都按 ``<home>/engine/atrix_research/__init__.py`` 判断"是否已安装"。
``<配置目录>/ATRI-X/research/settings.json``
    引擎默认设置；**已存在时不覆盖**（用户改过的检索源/阈值要保留）。
``<配置目录>/ATRI-X/skills/agent/<skill>/``
    15 个 AgentSkill：``SKILL.md`` + （12 个硬工具的）``ATRIX_SKILL.toml``。
    清单里的 ``command`` 由本模块按运行形态生成——冻结包是
    ``[atri-x.exe, -m, atri_x.research.runner, --home, <home>, <tool>]``，
    源码树是 ``[python, <home>/engine/run.py, --home, <home>, <tool>]``。
``<配置目录>/ATRI-X/research/install.json``
    安装清单（schema ``atrix-research/install/2``，``mode="bundled"``）：记录包摘要、
    引擎版本、命令形态、装过的技能。幂等判断与升级都以它为准。

三条硬约束
----------

1. **只走冻结产物自动安装**：源码树默认不自动装（源码用户按 ``atri-research/install.py``
   的既有流程装 venv 版），避免测试/开发时往真实配置目录写东西；
2. **尊重已有外部安装**：``install.json`` 不是本模块写的（``mode`` 缺失或非
   ``bundled``）时一律不动——用户用安装器装过 venv 版就继续用那份；
3. **失败只报告不抛**：打包资源缺失、磁盘写失败都返回 ``state="failed"``，调用方
   记一条 WARNING 即可；研究助理不可用不应该让机器人起不来。
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

from atri_x.research.pack import (
    PACK_DIR_NAME,
    PACK_META_FILE,
    is_frozen,
    load_pack_meta,
    pack_root,
)
from atri_x.research.paths import default_config_dir, research_home

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

#: 安装清单 schema（与 ``atri-research/install.py`` 的 ``.../install/1`` 区分开）。
INSTALL_SCHEMA = "atrix-research/install/2"
#: 本模块写出的清单 ``mode``；外部安装器写的是别的 schema，用这个值区分归属。
MODE_BUNDLED = "bundled"
#: 生成文件的首行标记：升级时只清理带这个标记的旧技能目录。
GENERATED_MARKER = "# 由 atri_x.research.bootstrap 生成"
#: 安装锁文件名（GUI 与后台服务可能同时首次启动）。
LOCK_NAME = ".install.lock"
#: 等锁上限（秒）：超时后仍继续装，靠"先写临时目录再替换"保证结果可用。
LOCK_TIMEOUT = 10.0

#: 12 个硬工具（有 ``ATRIX_SKILL.toml``）；其余 3 个是纯文档技能。
_MANIFEST_FILES = "manifest.json"
_SKILL_DOC = "SKILL.md"
_SKILL_TOML = "ATRIX_SKILL.toml"


@dataclass(frozen=True)
class BootstrapResult:
    """一次 ``ensure_installed`` 的结果（调用方只读，不抛异常）。"""

    #: ``installed`` / ``up-to-date`` / ``skipped`` / ``external`` / ``unavailable`` / ``failed``
    state: str
    home: str = ""
    engine_version: str = ""
    skills: tuple[str, ...] = ()
    message: str = ""
    #: 12 个硬工具的命令行（调试/清单用），形如 ``{"search": [...]}``
    commands: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否处于"研究引擎可用"的状态（跳过源码安装不算失败，但也不算已装）。"""
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


def engine_base_command(home: str, *, frozen: bool | None = None) -> list[str]:
    """引擎命令前缀（不含工具名与参数）。"""
    use_frozen = is_frozen() if frozen is None else bool(frozen)
    if use_frozen:
        return [
            _console_executable(),
            "-m",
            "atri_x.research.runner",
            "--home",
            home,
        ]
    engine_run = os.path.join(home, "engine", "run.py")
    return [sys.executable, engine_run, "--home", home]


def toml_command(parts: list[str]) -> str:
    """把命令渲染成 TOML 字符串数组（JSON 转义与 TOML 基本字符串兼容）。"""
    return "[" + ", ".join(json.dumps(part, ensure_ascii=False) for part in parts) + "]"


def render_skill_manifest(manifest: dict[str, Any], command: list[str]) -> str:
    """渲染一个硬工具的 ``ATRIX_SKILL.toml``（字段集合与 ``agent_manifest`` 允许的 8 个键一致）。"""
    skill = str(manifest.get("skill") or manifest.get("tool") or "")
    tags = [str(tag) for tag in manifest.get("tags") or []]
    lines = [
        f"{GENERATED_MARKER}（来源 {PACK_DIR_NAME}/skills/{skill}/manifest.json，升级随包资源时重写）",
        f'risk = "{manifest.get("risk") or "low"}"',
        "superadmin_only = false",
        "tags = [" + ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags) + "]",
        "command = " + toml_command(command),
        f"timeout_seconds = {int(manifest.get('timeout_seconds') or 0)}",
        "expose_root = false",
        "",
        "parameters = '''",
        json.dumps(manifest.get("parameters") or {}, ensure_ascii=False, indent=2),
        "'''",
        "",
        "[args]",
    ]
    for name, flag in (manifest.get("args") or {}).items():
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
                logger.warning("research bootstrap: 等安装锁超时，继续执行（结果仍可自愈）")
                break
            time.sleep(0.1)
        except OSError as exc:  # 只读目录等：不阻断，后续写入会给出真实错误
            logger.warning("research bootstrap: 无法创建安装锁（%s）", exc)
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


def _expected_skills(pack: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """返回 ``(技能名列表, 硬工具清单)``（清单按技能名索引）。"""
    skills_dir = pack / "skills"
    names: list[str] = []
    manifests: dict[str, dict[str, Any]] = {}
    if not skills_dir.is_dir():
        return names, manifests
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (child / _SKILL_DOC).is_file():
            continue
        names.append(child.name)
        manifest_path = child / _MANIFEST_FILES
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("schema") == "atrix-research/manifest/1":
            manifests[child.name] = manifest
    return names, manifests


def _installation_ready(
    home: str, config_dir: str, meta: dict[str, Any], manifest: dict[str, Any] | None
) -> bool:
    """清单摘要一致 + 引擎文件在 + 每个技能的 SKILL.md/TOML 齐 → 视为已就绪。"""
    if not _manifest_is_ours(manifest):
        return False
    assert manifest is not None  # for type checkers; _manifest_is_ours 已保证
    if manifest.get("pack_digest") != meta.get("digest"):
        return False
    if not os.path.isfile(os.path.join(home, "engine", "atrix_research", "__init__.py")):
        return False
    pack = pack_root()
    if pack is None:
        return False
    names, manifests = _expected_skills(pack)
    skills_dir = os.path.join(config_dir, "skills", "agent")
    for name in names:
        skill_dir = os.path.join(skills_dir, name)
        if not os.path.isfile(os.path.join(skill_dir, _SKILL_DOC)):
            return False
        if name in manifests and not os.path.isfile(os.path.join(skill_dir, _SKILL_TOML)):
            return False
    return True


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


def _install(pack: Path, meta: dict[str, Any], home: str, config_dir: str, *, frozen: bool) -> BootstrapResult:
    names, manifests = _expected_skills(pack)
    if not names:
        return BootstrapResult(
            state="unavailable",
            home=home,
            message=f"{pack / 'skills'} 下没有技能（随包资源不完整）",
        )

    os.makedirs(home, exist_ok=True)
    # 1) 引擎：整体替换，避免升级后残留旧模块
    _replace_dir(pack / "engine", Path(home) / "engine")

    # 2) 默认设置：只在缺失时落盘（用户改过的不覆盖）
    settings_target = os.path.join(home, "settings.json")
    settings_source = pack / "settings.json"
    if not os.path.isfile(settings_target) and settings_source.is_file():
        _write_text(settings_target, settings_source.read_text(encoding="utf-8"))

    # 3) 技能：SKILL.md 覆盖（随包文档是权威），ATRIX_SKILL.toml 按运行形态生成
    skills_dir = os.path.join(config_dir, "skills", "agent")
    os.makedirs(skills_dir, exist_ok=True)
    base = engine_base_command(home, frozen=frozen)
    commands: dict[str, list[str]] = {}
    for name in names:
        source_dir = pack / "skills" / name
        target_dir = os.path.join(skills_dir, name)
        os.makedirs(target_dir, exist_ok=True)
        _write_text(
            os.path.join(target_dir, _SKILL_DOC),
            (source_dir / _SKILL_DOC).read_text(encoding="utf-8"),
        )
        manifest = manifests.get(name)
        if manifest is None:
            continue
        tool = str(manifest.get("tool") or name)
        command = [*base, tool]
        commands[tool] = command
        _write_text(os.path.join(target_dir, _SKILL_TOML), render_skill_manifest(manifest, command))

    # 4) 清理上一版留下、这一版已不存在的技能（只删带生成标记的 TOML 所在目录）
    previous = _read_install_manifest(home)
    stale = set()
    if _manifest_is_ours(previous) and isinstance(previous, dict):
        stale = set(previous.get("skills") or []) - set(names)
    for name in sorted(stale):
        skill_dir = Path(skills_dir) / name
        toml_path = skill_dir / _SKILL_TOML
        try:
            head = toml_path.read_text(encoding="utf-8")[: len(GENERATED_MARKER)]
        except OSError:
            continue
        if head == GENERATED_MARKER:
            shutil.rmtree(skill_dir, ignore_errors=True)

    # 5) 安装清单（最后写：它是"装完了"的判定依据）
    manifest = {
        "schema": INSTALL_SCHEMA,
        "mode": MODE_BUNDLED,
        "engine_version": str(meta.get("engine_version") or ""),
        "pack_digest": str(meta.get("digest") or ""),
        "pack_schema": str(meta.get("schema") or ""),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_dir": config_dir,
        "home": home,
        "engine_dir": os.path.join(home, "engine"),
        "engine_run": os.path.join(home, "engine", "run.py"),
        "command_mode": "frozen" if frozen else "source",
        "executable": _console_executable(),
        "python": sys.executable,
        "skills": names,
        "commands": commands,
    }
    _write_text(os.path.join(home, "install.json"), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "research bootstrap: 已安装随包引擎 %s（%d 个技能，命令形态 %s）",
        manifest["engine_version"],
        len(names),
        manifest["command_mode"],
    )
    return BootstrapResult(
        state="installed",
        home=home,
        engine_version=manifest["engine_version"],
        skills=tuple(names),
        message="随包研究引擎已安装",
        commands=commands,
    )


def ensure_installed(
    config_dir: str = "",
    *,
    frozen: bool | None = None,
    allow_source: bool = False,
    force: bool = False,
) -> BootstrapResult:
    """确保研究引擎/技能已装进 ``config_dir``（默认取平台配置目录）。

    ``frozen=None`` 时按当前进程判定；源码树默认返回 ``state="skipped"``，
    需要显式 ``allow_source=True`` 才装（测试与排障用）。
    """
    use_frozen = is_frozen() if frozen is None else bool(frozen)
    base_dir = (config_dir or "").strip() or default_config_dir()
    if not base_dir:
        return BootstrapResult(state="failed", message="无法确定 ATRI-X 配置目录")
    home = research_home(base_dir)
    if not use_frozen and not allow_source:
        return BootstrapResult(
            state="skipped",
            home=home,
            message="源码模式不自动安装研究引擎（用 atri-research/install.py）",
        )
    pack = pack_root()
    meta = load_pack_meta(pack)
    if pack is None or meta is None:
        return BootstrapResult(
            state="unavailable",
            home=home,
            message=f"随包研究引擎缺失（找不到 {PACK_DIR_NAME}/{PACK_META_FILE}）",
        )
    current = _read_install_manifest(home)
    if current is not None and not _manifest_is_ours(current):
        return BootstrapResult(
            state="external",
            home=home,
            engine_version=str(current.get("engine_version") or ""),
            message="已有外部安装（atri-research 安装器）的研究引擎，保持不动",
        )
    if not force and _installation_ready(home, base_dir, meta, current):
        return BootstrapResult(
            state="up-to-date",
            home=home,
            engine_version=str(meta.get("engine_version") or ""),
            skills=tuple(current.get("skills") or ()) if current else (),
            message="随包研究引擎已就绪",
            commands={k: [str(x) for x in v] for k, v in (current or {}).get("commands", {}).items()},
        )
    try:
        with _install_lock(home) as locked:
            if locked and not force and _installation_ready(
                home, base_dir, meta, _read_install_manifest(home)
            ):
                current = _read_install_manifest(home) or {}
                return BootstrapResult(
                    state="up-to-date",
                    home=home,
                    engine_version=str(meta.get("engine_version") or ""),
                    skills=tuple(current.get("skills") or ()),
                    message="随包研究引擎已就绪（另一进程刚装好）",
                    commands={
                        k: [str(x) for x in v]
                        for k, v in (current.get("commands") or {}).items()
                    },
                )
            return _install(pack, meta, home, base_dir, frozen=use_frozen)
    except OSError as exc:
        logger.warning("research bootstrap: 安装失败（%s）", exc)
        return BootstrapResult(state="failed", home=home, message=f"安装失败：{exc}")
    except (ValueError, TypeError) as exc:  # 清单/资源损坏
        logger.warning("research bootstrap: 资源不完整（%s）", exc)
        return BootstrapResult(state="failed", home=home, message=f"随包资源不完整：{exc}")


def pack_status(config_dir: str = "") -> dict[str, Any]:
    """随包引擎与安装状态快照（``/research env`` 与诊断用；永不抛异常）。"""
    base_dir = (config_dir or "").strip() or default_config_dir()
    home = research_home(base_dir) if base_dir else ""
    pack = pack_root()
    meta = load_pack_meta(pack) or {}
    current = _read_install_manifest(home) if home else None
    return {
        "pack_available": pack is not None,
        "pack_dir": str(pack) if pack is not None else "",
        "pack_engine_version": str(meta.get("engine_version") or ""),
        "pack_digest": str(meta.get("digest") or ""),
        "home": home,
        "installed": _manifest_is_ours(current),
        "install_mode": str((current or {}).get("mode") or ""),
        "install_schema": str((current or {}).get("schema") or ""),
        "engine_version": str((current or {}).get("engine_version") or ""),
        "engine_present": bool(home)
        and os.path.isfile(os.path.join(home, "engine", "atrix_research", "__init__.py")),
        "up_to_date": bool(current)
        and _manifest_is_ours(current)
        and (current or {}).get("pack_digest") == meta.get("digest"),
        "frozen": is_frozen(),
    }


# ---------------------------------------------------------------------------
# 命令行（冻结产物的构建期核验用：atri-x.exe -m atri_x.research.bootstrap --check）
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m atri_x.research.bootstrap``：安装并打印 JSON 结果。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.research.bootstrap",
        description="安装/核对随包研究引擎（ATRI-X 首次启动会自动执行同一逻辑）",
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
        "engine_version": result.engine_version,
        "skills": list(result.skills),
        "message": result.message,
        "commands": result.commands,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - 入口在子进程里跑
    raise SystemExit(main())
