"""文件系统 skill 扫描器。

对应 Go：``internal/tool/skill/scanner.go``。

目录约定（与 Go 一致）::

    <root>/agent/<name>/SKILL.atriyph | SKILL.md   # AgentSkill
    <root>/go/<name>/SKILL.atriyph                 # Go skill（+ 同名可执行文件）

扫描是**全量**的：任何一条记录读失败都会让整次扫描失败（而不是静默跳过），
重名会报错并列出两个目录——配置错误必须立刻可见。
"""

from __future__ import annotations

import os
import shutil
import stat as stat_module
from typing import Any

from atri_x.atriyph import FORMAT as ATRIYPH_FORMAT
from atri_x.atriyph import SKILL_FILE_NAME, parse_header
from atri_x.tool.skill.agent_manifest import load_agent_skill_manifest
from atri_x.tool.skill.catalog import Catalog, Kind, Record, new_catalog
from atri_x.tool.skill.command_tool import CommandTool, new_command_tool
from atri_x.tool.skill.descriptor import new_descriptor
from atri_x.tool.skill.parser import parse_risk, parse_skill_markdown
from atri_x.tool.skill.process import IS_WINDOWS
from atri_x.tool.tool import RiskHigh, RiskSafe, SourceSkillAgent, SourceSkillGo

__all__ = [
    "FilesystemScanner",
    "atriyph_risk",
    "default_root",
    "dir_name_for_kind",
    "executable_file",
    "find_go_binary",
    "new_filesystem_scanner",
    "tool_for_record",
    "user_config_dir",
]

#: Windows 下的应用目录名。对应 Go ``windowsAppDirName``。
WINDOWS_APP_DIR_NAME = "ATRI-X"
#: XDG 下的应用目录名。对应 Go ``xdgAppDirName``。
XDG_APP_DIR_NAME = "atri-x"


def user_config_dir() -> str:
    """``os.UserConfigDir()`` 的对应物。取不到时抛 ``OSError``。"""
    if IS_WINDOWS:
        appdata = (os.environ.get("APPDATA") or "").strip()
        if appdata:
            return appdata
        raise OSError("neither $APPDATA nor $HOME are defined")
    home = os.path.expanduser("~")
    if os.name == "posix" and os.uname().sysname == "Darwin":
        if home:
            return os.path.join(home, "Library", "Application Support")
        raise OSError("neither $HOME nor $XDG_CONFIG_HOME are defined")
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return xdg
    if home:
        return os.path.join(home, ".config")
    raise OSError("neither $HOME nor $XDG_CONFIG_HOME are defined")


def default_root() -> str:
    """skill 根目录的默认位置。对应 Go ``DefaultRoot``。

    Windows：``%AppData%/ATRI-X/skills``；其它平台：``$XDG_DATA_HOME/atri-x/skills``，
    再退回 ``~/.local/share/atri-x/skills``，最后是相对路径 ``skills``。
    """
    if IS_WINDOWS:
        try:
            directory = user_config_dir()
        except OSError:
            directory = ""
        if directory.strip() != "":
            return os.path.join(directory, WINDOWS_APP_DIR_NAME, "skills")
    xdg_data = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg_data != "":
        return os.path.join(xdg_data, XDG_APP_DIR_NAME, "skills")
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".local", "share", XDG_APP_DIR_NAME, "skills")
    return "skills"


def dir_name_for_kind(kind: str) -> str:
    """种类 → 子目录名。对应 Go ``dirNameForKind``。"""
    return "go" if kind == Kind.GO else "agent"


class FilesystemScanner:
    """扫描 skill 根目录。对应 Go ``FilesystemScanner``。"""

    def __init__(
        self,
        root: str = "",
        catalog: Catalog | None = None,
        media: Any = None,
    ) -> None:
        if root == "":
            root = default_root()
        self.media = media
        self.root = root
        self.catalog = catalog if catalog is not None else new_catalog()

    # -- 扫描 ---------------------------------------------------------------

    async def scan(self, ctx: Any) -> list[Any]:
        """返回全部 skill 工具。对应 Go ``FilesystemScanner.Scan``。"""
        _raise_if_cancelled(ctx)
        records = await self.scan_records(ctx)
        return [self.tool_for_record(record) for record in records]

    async def reload(self, ctx: Any, registry: Any = None) -> None:
        """重新扫描并原子替换注册表里的 skill 工具。对应 Go ``Reload``。"""
        _raise_if_cancelled(ctx)
        if registry is None:
            return
        records = await self.scan_records(ctx)
        replacements = [self.tool_for_record(record) for record in records]
        registry.replace_sources([SourceSkillAgent, SourceSkillGo], replacements)
        if self.catalog is not None:
            self.catalog.replace(records)

    async def remove(self, ctx: Any, registry: Any, name: str) -> None:
        """删除一个 skill 目录并重扫。对应 Go ``Remove``。"""
        _raise_if_cancelled(ctx)
        if self.catalog is None:
            raise ValueError("skill catalog is not configured")
        record, ok = self.catalog.get(name)
        if not ok or record is None:
            raise ValueError(f"external skill {name!r} not found")
        if record.root == "":
            raise ValueError(f"external skill {name!r} has no root directory")
        try:
            shutil.rmtree(record.root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OSError(f"remove skill directory {record.root!r}: {exc}") from exc
        await self.reload(ctx, registry)

    async def scan_records(self, ctx: Any) -> list[Record]:
        """扫描两个种类并去重。对应 Go ``scanRecords``。"""
        records: list[Record] = []
        for kind in (Kind.AGENT, Kind.GO):
            records.extend(await self.scan_kind(ctx, kind))
        seen: dict[str, Record] = {}
        out: list[Record] = []
        duplicates: list[str] = []
        for record in records:
            if record.name == "":
                continue
            previous = seen.get(record.name)
            if previous is not None:
                duplicates.append(
                    f"{record.name!r} ({previous.root}, {record.root})"
                )
                continue
            seen[record.name] = record
            out.append(record)
        if duplicates:
            duplicates.sort()
            raise ValueError("duplicate skill names: " + "; ".join(duplicates))
        out.sort(key=lambda record: record.name)
        return out

    async def scan_kind(self, ctx: Any, kind: str) -> list[Record]:
        """扫描一个种类目录。对应 Go ``scanKind``。"""
        directory = os.path.join(self.root, dir_name_for_kind(kind))
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise OSError(f"read skill dir {directory!r}: {exc}") from exc
        records: list[Record] = []
        for entry in entries:
            _raise_if_cancelled(ctx)
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if not is_dir:
                continue
            root = os.path.join(directory, entry.name)
            record, ok = self.read_record(root, entry.name, kind)
            if ok:
                records.append(record)
        return records

    def read_record(self, root: str, dir_name: str, kind: str) -> tuple[Record, bool]:
        """读一条记录。对应 Go ``readRecord``。"""
        if kind == Kind.GO:
            return self.read_go_record(root, dir_name)
        return self.read_agent_record(root, dir_name)

    def read_go_record(self, root: str, dir_name: str) -> tuple[Record, bool]:
        """读一个 Go skill。对应 Go ``readGoRecord``。"""
        path = os.path.join(root, SKILL_FILE_NAME)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return Record(), False
        except OSError as exc:
            raise OSError(f"read {SKILL_FILE_NAME} in {root!r}: {exc}") from exc
        text = raw.decode("utf-8", errors="replace")
        try:
            header = parse_header(text)
        except Exception:  # noqa: BLE001 - Go 侧解析失败即「不是 skill」
            return Record(), False
        binary, _ = find_go_binary(root, dir_name, header.name)
        detail = text.strip()
        record = Record(
            name=header.name,
            description=header.description,
            detail=detail,
            format=ATRIYPH_FORMAT,
            risk=atriyph_risk(detail),
            kind=Kind.GO,
            root=root,
            binary_path=binary,
        )
        return record, True

    def read_agent_record(self, root: str, dir_name: str) -> tuple[Record, bool]:
        """读一个 AgentSkill（``SKILL.atriyph`` 优先，其次 ``SKILL.md``）。"""
        path = os.path.join(root, SKILL_FILE_NAME)
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise OSError(f"read {SKILL_FILE_NAME} in {root!r}: {exc}") from exc
        if raw is not None:
            text = raw.decode("utf-8", errors="replace")
            try:
                header = parse_header(text)
            except Exception:  # noqa: BLE001 - Go 侧解析失败即「不是 skill」
                return Record(), False
            detail = text.strip()
            record = Record(
                name=header.name,
                description=header.description,
                detail=detail,
                format=ATRIYPH_FORMAT,
                risk=RiskSafe,
                kind=Kind.AGENT,
                root=root,
            )
            return self.with_agent_manifest(record), True

        detail_path = os.path.join(root, "SKILL.md")
        try:
            with open(detail_path, "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            return Record(), False
        except OSError as exc:
            raise OSError(f"read SKILL.md in {root!r}: {exc}") from exc
        try:
            definition = parse_skill_markdown(data, dir_name)
        except ValueError as exc:
            raise ValueError(f"parse SKILL.md in {root!r}: {exc}") from exc
        record = Record(
            name=definition.name,
            description=definition.description,
            detail_path=detail_path,
            format=definition.format,
            risk=RiskSafe,
            kind=Kind.AGENT,
            root=root,
        )
        return self.with_agent_manifest(record), True

    def with_agent_manifest(self, record: Record) -> Record:
        """读清单并覆盖记录的风险/可见性/标签。对应 Go ``withAgentManifest``。"""
        try:
            manifest, found = load_agent_skill_manifest(record.root)
        except (ValueError, OSError) as exc:
            record.manifest_found = True
            record.manifest_error = str(exc)
            return record
        record.manifest_found = found
        if found:
            record.manifest = manifest
            record.risk = manifest.risk
            record.superadmin_only = manifest.superadmin_only
            record.tags = list(manifest.tags)
        return record

    def tool_for_record(self, record: Record) -> Any:
        """记录 → 工具（可调用则注入媒体运行时）。对应 Go ``toolForRecord``。"""
        target = tool_for_record(record)
        if isinstance(target, CommandTool):
            target.media = self.media
        return target


def tool_for_record(record: Record) -> Any:
    """记录 → 工具。对应 Go 包级 ``toolForRecord``。"""
    if (
        record.kind == Kind.AGENT
        and record.manifest_found
        and record.manifest_error == ""
        and record.manifest.callable
    ):
        return new_command_tool(record)
    return new_descriptor(record)


def atriyph_risk(text: str) -> str:
    """从 ``** risk <level>`` 行取风险等级。对应 Go ``atriyphRisk``。

    找不到合法行时按 ``high`` 处理（保守默认）。
    """
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        trimmed = line.strip()
        if trimmed.startswith("** risk "):
            try:
                return parse_risk(trimmed[len("** risk ") :].strip())
            except ValueError:
                continue
    return RiskHigh


def find_go_binary(root: str, dir_name: str, skill_name: str) -> tuple[str, bool]:
    """在 skill 目录里找可执行文件。对应 Go ``findGoBinary``。

    先看 ``<dir>.exe`` / ``<skill>.exe``（Windows）或同名文件（POSIX），再扫
    整个目录：**恰好一个**候选才算命中（多个候选说明布局有歧义，宁可不注册）。
    """
    candidates: list[str] = []
    if IS_WINDOWS:
        candidates.append(os.path.join(root, dir_name + ".exe"))
        candidates.append(os.path.join(root, skill_name + ".exe"))
    else:
        candidates.append(os.path.join(root, dir_name))
        candidates.append(os.path.join(root, skill_name))
    for candidate in candidates:
        if executable_file(candidate):
            return candidate, True
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError as exc:
        raise OSError(f"read go skill dir {root!r}: {exc}") from exc
    matches: list[str] = []
    for entry in entries:
        try:
            if entry.is_dir():
                continue
        except OSError:
            continue
        path = os.path.join(root, entry.name)
        if IS_WINDOWS:
            if os.path.splitext(entry.name)[1] == ".exe":
                matches.append(path)
        elif executable_file(path):
            matches.append(path)
    if len(matches) == 1:
        return matches[0], True
    return "", False


def executable_file(path: str) -> bool:
    """是否是可执行文件。对应 Go ``executableFile``。"""
    try:
        info = os.stat(path)
    except OSError:
        return False
    if stat_module.S_ISDIR(info.st_mode):
        return False
    if IS_WINDOWS:
        return os.path.splitext(path)[1] == ".exe"
    return info.st_mode & 0o111 != 0


def new_filesystem_scanner(root: str = "", media: Any = None) -> FilesystemScanner:
    """对应 Go ``NewFilesystemScanner``。"""
    return FilesystemScanner(root=root, media=media)


def _raise_if_cancelled(ctx: Any) -> None:
    if ctx is None:
        return
    if ctx.is_cancelled():
        error = ctx.error()
        if error is not None:
            raise error
        from atri_x.safecontext import CancelledError

        raise CancelledError("context canceled")
