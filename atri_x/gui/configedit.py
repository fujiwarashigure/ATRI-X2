"""桌面端的配置读写底座：路径解析、TOML 行级补丁、``.env``、文本文件。

分工（见 ``docs/windows-app.md``）：GUI 不跑 Agent，但**配置是本地文件**，所以
配置界面直接读写配置文件，不经过后台服务。本模块只管"怎么安全地读写"，
具体编辑哪些键在 :mod:`atri_x.gui.configmodel`（provider / 模型分配）和
各选项卡里。

安全写盘的三条约定：

- 用**行级补丁**改 TOML：保留注释、缩进和别的段落，只替换 / 追加 / 删除目标键
  （小节不存在时在文件末尾补一个）；
- 每次写盘前留一份 ``<文件名>.gui.bak``（上一次保存前的内容，可手工回滚）；
- 先写 ``.tmp`` 再 ``os.replace``，写前用 ``tomllib`` 解析整份新文本，解析不过就
  **不落盘**（避免把配置写坏导致服务起不来）。

编码统一 UTF-8 无 BOM + LF（仓库规则）。

哪些改动什么时候生效：

| 文件 | 生效时机 |
|---|---|
| ``state.toml``（模型分配 / 默认模型快照 / 群策略等运行分区） | 运行期热加载，保存后几秒内生效 |
| ``state.toml`` 的 ``[session].default_mode``（默认会话模式） | **后台服务启动时读取**：改完需重启后台服务，之后新建的会话才用新模式（运行中的会话不受影响） |
| ``services.toml``（Provider）/ ``.env``（密钥） | 重启后台服务 |
| ``app.toml``（平台、人设库、功能开关） | 重启后台服务 |
| ``characters/`` + ``SOUL.md`` | 角色库 3 秒内自动重扫；SOUL 下一轮对话生效 |
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from atri_x.config.loader import load_env_file

__all__ = [
    "ConfigEditError",
    "ConfigPaths",
    "default_config_path",
    "load_paths",
    "patch_section",
    "patch_top_level",
    "pop_env_value",
    "read_env",
    "read_toml",
    "remove_section",
    "set_env_value",
    "toml_value",
    "verify_config",
    "write_array_tables",
    "write_text_file",
]

#: 备份后缀：每次写盘前把旧内容存成它（只留最近一份）。
BACKUP_SUFFIX = ".gui.bak"


class ConfigEditError(Exception):
    """配置读写的可预期失败（消息直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigPaths:
    """一个配置目录里各个文件的绝对路径（由 ``app.toml`` 的 ``[config_files]`` 推出来）。

    后加的四项给「日志」「群友」两个只读页用：它们要找到后台服务写的日志目录与
    SQLite 库，但又不该自己去解析 ``app.toml``。取不到时为空串（页面上给出提示）。
    """

    config_path: str
    config_dir: str
    services_path: str
    state_path: str
    atrinis_path: str
    env_path: str
    soul_path: str
    character_root: str
    plugins_dir: str
    skills_dir: str
    data_dir: str = ""
    sessions_db_path: str = ""
    chat_history_db_path: str = ""
    log_dir: str = ""
    #: ``tool_tags.toml`` 的绝对路径（「提示词 → 工具标签」页读写它）。
    tool_tags_path: str = ""

    @property
    def hooks_path(self) -> str:
        return os.path.join(self.plugins_dir, "hooks.toml")

    @property
    def gui_dir(self) -> str:
        """GUI 自己的数据目录（``<配置目录>/gui``：成员档案、头像缓存）。"""
        return os.path.join(self.config_dir, "gui")

    @property
    def services_is_legacy_providers(self) -> bool:
        """当前用到的 provider 文件是不是旧的 ``providers.toml``（没有 services.toml）。"""
        return os.path.basename(self.services_path) == "providers.toml"

    @classmethod
    def from_config(cls, config_path: str) -> ConfigPaths:
        """从 ``app.toml`` 路径推其它文件路径；文件不存在也能推（按默认名）。"""
        resolved = os.path.abspath(os.path.expanduser(config_path or ""))
        config_dir = os.path.dirname(resolved)
        data = read_toml(resolved)
        files = data.get("config_files")
        files = files if isinstance(files, Mapping) else {}
        services = _resolve_relative(resolved, _text(files.get("services")) or "services.toml")
        legacy = _resolve_relative(resolved, _text(files.get("providers")) or "providers.toml")
        if not os.path.exists(services) and os.path.exists(legacy):
            services = legacy
        soul = data.get("soul")
        soul_path = _text(soul.get("path")) if isinstance(soul, Mapping) else ""
        library = data.get("character_library")
        root = _text(library.get("root")) if isinstance(library, Mapping) else ""
        data_dir, sessions_db, history_db, log_dir = _storage_paths(resolved, data)
        return cls(
            config_path=resolved,
            config_dir=config_dir,
            services_path=services,
            state_path=_resolve_relative(resolved, _text(files.get("state")) or "state.toml"),
            atrinis_path=_resolve_relative(
                resolved, _text(files.get("atrinis")) or "atrinis.toml"
            ),
            env_path=os.path.join(config_dir, ".env"),
            soul_path=_resolve_relative(resolved, soul_path or "SOUL.md"),
            character_root=_resolve_relative(resolved, root or "characters"),
            plugins_dir=os.path.join(config_dir, "plugins"),
            skills_dir=os.path.join(config_dir, "skills"),
            data_dir=data_dir,
            sessions_db_path=sessions_db,
            chat_history_db_path=history_db,
            log_dir=log_dir,
            tool_tags_path=_resolve_relative(
                resolved, _text(files.get("tool_tags")) or "tool_tags.toml"
            ),
        )


def default_config_path() -> str:
    """没有显式配置时按查找顺序推 ``app.toml`` 路径（**只算路径，不创建文件**）。"""
    from atri_x.config import loader

    explicit = (os.environ.get(loader.ENV_CONFIG_FILE) or "").strip()
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return loader.platform_default_config_path() or ""


def load_paths(config_path: str) -> ConfigPaths | None:
    """建配置路径表；``app.toml`` 解析失败时退回"同目录默认文件名"，界面仍可打开。"""
    if not config_path:
        return None
    try:
        return ConfigPaths.from_config(config_path)
    except ConfigEditError:
        directory = os.path.dirname(os.path.abspath(os.path.expanduser(config_path)))
        data_dir, sessions_db, history_db, log_dir = _storage_paths(config_path, {})
        return ConfigPaths(
            config_path=config_path,
            config_dir=directory,
            services_path=os.path.join(directory, "services.toml"),
            state_path=os.path.join(directory, "state.toml"),
            atrinis_path=os.path.join(directory, "atrinis.toml"),
            env_path=os.path.join(directory, ".env"),
            soul_path=os.path.join(directory, "SOUL.md"),
            character_root=os.path.join(directory, "characters"),
            plugins_dir=os.path.join(directory, "plugins"),
            skills_dir=os.path.join(directory, "skills"),
            data_dir=data_dir,
            sessions_db_path=sessions_db,
            chat_history_db_path=history_db,
            log_dir=log_dir,
            tool_tags_path=os.path.join(directory, "tool_tags.toml"),
        )


def _resolve_relative(base_file: str, path: str) -> str:
    """相对路径按 ``base_file`` 所在目录解析（与 :mod:`atri_x.config.loader` 一致）。"""
    value = (path or "").strip()
    if not value:
        return ""
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(os.path.dirname(base_file), value))


def _storage_paths(
    config_path: str, data: Mapping[str, Any]
) -> tuple[str, str, str, str]:
    """从 ``[storage]`` 推 ``(数据目录, 会话库, 聊天历史库, 日志目录)``。

    与 :meth:`atri_x.config.app.AppConfig.apply_app_defaults` 的口径一致：``app.toml``
    里留空时落在平台默认数据目录；日志目录固定是会话库旁边的 ``logs/``（见
    :func:`atri_x.logging.logging.log_dir_for`）。
    """
    from atri_x.config.loader import platform_default_data_dir

    storage = data.get("storage")
    storage = storage if isinstance(storage, Mapping) else {}
    default_dir = platform_default_data_dir()
    sessions = _resolve_relative(config_path, _text(storage.get("sessions_sqlite_path")))
    if not sessions:
        sessions = os.path.join(default_dir, "atrix_sessions.db")
    history = _resolve_relative(config_path, _text(storage.get("chat_history_sqlite_path")))
    if not history:
        history = os.path.join(default_dir, "atrix_chat_history.db")
    data_dir = os.path.dirname(sessions) or default_dir
    return data_dir, sessions, history, os.path.join(data_dir, "logs")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# TOML 读
# ---------------------------------------------------------------------------
def read_toml(path: str) -> dict[str, Any]:
    """读 TOML；文件不存在返回空表，解析失败抛 :class:`ConfigEditError`。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigEditError(f"读取 {path} 失败：{exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"{os.path.basename(path)} 不是合法的 TOML：{exc}") from exc
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# TOML 写（行级补丁）
# ---------------------------------------------------------------------------
def toml_value(value: Any) -> str:
    """把 Python 值渲染成 TOML 标量/数组字面量（只支持界面用到的类型）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            raise ConfigEditError("不支持 inf/nan")
        return repr(value)
    if isinstance(value, str):
        return '"' + _escape_basic(value) + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise ConfigEditError(f"不支持写入的类型：{type(value).__name__}")


def _escape_basic(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\b":
            out.append("\\b")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\f":
            out.append("\\f")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return "".join(out)


def patch_section(
    path: str,
    section: str,
    values: Mapping[str, Any],
    *,
    header_comment: str = "",
) -> bool:
    """写 ``[section]`` 里的若干键，返回是否改动。

    ``values`` 里值为 ``None`` 表示删除该键。小节不存在时在文件末尾补上，
    可选的 ``header_comment`` 会写在小节名上方（只在新建小节时使用）。
    已有键的行内注释会随被替换的那一行一起消失（值变了，注释通常也不再准确）。
    """
    if not section.strip():
        raise ConfigEditError("小节名不能为空")
    text = _read_text(path)
    lines = text.splitlines()
    header = _section_header_index(lines, section)
    if header is None:
        lines = _append_section(lines, section, values, header_comment)
    else:
        end = _section_end(lines, header)
        cursor = end
        for key, value in values.items():
            key = _check_key(key)
            found = _key_line_index(lines, header, end, key)
            if value is None:
                if found is not None:
                    del lines[found]
                    end -= 1
                    cursor -= 1
                continue
            rendered = f"{key} = {toml_value(value)}"
            if found is not None:
                lines[found] = rendered
            else:
                cursor = _insert_index(lines, header, cursor)
                lines.insert(cursor, rendered)
                end += 1
                cursor += 1
    return _commit(path, text, "\n".join(lines) + "\n")


def patch_top_level(path: str, values: Mapping[str, Any]) -> bool:
    """写文件**第一个小节之前**的顶层键（``atrinis.toml`` 的 ``enabled`` 这种），返回是否改动。

    值为 ``None`` 表示删除该键；键不存在时插到第一个小节之前。
    """
    text = _read_text(path)
    lines = text.splitlines()
    end = len(lines)
    for index, line in enumerate(lines):
        if _header_name(line) is not None:
            end = index
            break
    cursor = end
    for key, value in values.items():
        key = _check_key(key)
        found = _key_line_index(lines, -1, end, key)
        if value is None:
            if found is not None:
                del lines[found]
                end -= 1
                cursor -= 1
            continue
        rendered = f"{key} = {toml_value(value)}"
        if found is not None:
            lines[found] = rendered
        else:
            cursor = _insert_index(lines, -1, cursor)
            lines.insert(cursor, rendered)
            end += 1
            cursor += 1
    return _commit(path, text, "\n".join(lines) + "\n")


def remove_section(path: str, section: str) -> bool:
    """删除 ``[section]`` 及其子表（``[section.xxx]``），返回是否改动。"""
    if not section.strip():
        raise ConfigEditError("小节名不能为空")
    text = _read_text(path)
    lines = text.splitlines()
    doomed: set[int] = set()
    index = 0
    while index < len(lines):
        name = _header_name(lines[index])
        if name is None:
            index += 1
            continue
        end = _section_end(lines, index)
        if name == section or name.startswith(section + "."):
            doomed.update(range(index, end))
        index = end
    if not doomed:
        return False
    kept = [line for position, line in enumerate(lines) if position not in doomed]
    return _commit(path, text, "\n".join(kept) + "\n")


def write_array_tables(
    path: str,
    header: str,
    blocks: Iterable[Mapping[str, Any]],
    *,
    after_section: str = "",
) -> bool:
    """整份重写 ``[[header]]`` 数组表（例如 ``[[time.sources]]``），返回是否改动。

    :func:`patch_section` 只能改 ``[section]`` 里的标量键：数组表的条数会变，没法用
    "替换某个键"表达，所以这里走"摘掉全部旧块 → 写入全部新块"：旧块整段移除
    （文件里的注释与其它小节原样保留），新块插回它原来的位置；原本没有旧块时插到
    ``after_section`` 指定的小节之后，小节也不存在就追加到文件末尾。
    ``blocks`` 为空表示清空这个数组表（旧块一并删除）。

    每个块按 ``blocks`` 的顺序渲染成一行 ``[[header]]`` + 若干 ``key = value``；
    值为 ``None`` 的键跳过（与 :func:`patch_section` 的约定一致）。
    """
    if not header.strip():
        raise ConfigEditError("数组表名不能为空")
    text = _read_text(path)
    lines = text.splitlines()

    # 1) 摘掉所有 [[header]] 块，记下第一块原来的位置（新块写回这里）。
    kept: list[str] = []
    anchor: int | None = None
    index = 0
    while index < len(lines):
        name = _array_header_name(lines[index])
        if name is None:
            kept.append(lines[index])
            index += 1
            continue
        end = _block_end(lines, index)
        if name == header:
            if anchor is None:
                anchor = len(kept)
            index = end
            continue
        kept.extend(lines[index:end])
        index = end

    # 2) 渲染新块。
    rendered: list[str] = []
    for block in blocks:
        if rendered:
            rendered.append("")
        rendered.append(f"[[{header}]]")
        for key, value in block.items():
            if value is None:
                continue
            rendered.append(f"{_check_key(key)} = {toml_value(value)}")

    # 3) 决定插入位置，并把两侧多余空行收掉（保持"块与块之间一个空行"）。
    if anchor is not None:
        position = anchor
    elif after_section:
        position = _section_insert_index(kept, after_section)
    else:
        position = len(kept)
    before, after = kept[:position], kept[position:]
    while before and not before[-1].strip():
        before.pop()
    while after and not after[0].strip():
        after.pop(0)
    merged = list(before)
    if merged and rendered:
        merged.append("")
    merged.extend(rendered)
    if merged and after:
        merged.append("")
    merged.extend(after)
    body = "\n".join(merged).rstrip("\n")
    return _commit(path, text, body + "\n" if body else "")


def write_text_file(path: str, text: str) -> bool:
    """整份写一个文本文件（SOUL.md 这类），带备份与原子替换；返回是否改动。"""
    old = _read_text(path)
    new = text if text.endswith("\n") or not text else text + "\n"
    if new == old:
        return False
    _backup(path, old)
    _atomic_write(path, new)
    return True


def verify_config(paths: Any) -> None:
    """保存 ``app.toml`` 后回读整份配置：严格解码能过，后台服务才起得来。

    与 :meth:`patch_section` 的分工：那条只保证"写进去的是合法 TOML"，这条保证
    "整份配置还能被后台服务按当前代码解码"。失败统一转成
    :class:`ConfigEditError`（GUI 可展示），因为写盘已经发生。
    """
    from atri_x.config.loader import load

    try:
        load(paths.config_path, strict=True)
    except Exception as exc:  # noqa: BLE001 - 统一转成 GUI 可展示的错误
        raise ConfigEditError(f"回读校验失败（配置会被后台服务拒绝）：{exc}") from exc


def _check_key(key: str) -> str:
    if not re.match(r"^[A-Za-z0-9_-]+$", key or ""):
        raise ConfigEditError(f"非法键名：{key!r}")
    return key


def _section_header_index(lines: list[str], section: str) -> int | None:
    for index, line in enumerate(lines):
        if _header_name(line) == section:
            return index
    return None


def _header_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    if stripped.startswith("[["):
        return None
    return stripped[1:-1].strip()


def _section_end(lines: list[str], header: int) -> int:
    for index in range(header + 1, len(lines)):
        if _header_name(lines[index]) is not None:
            return index
    return len(lines)


def _array_header_name(line: str) -> str | None:
    """``[[name]]`` → ``name``；不是数组表返回 ``None``。"""
    stripped = line.strip()
    if not (stripped.startswith("[[") and stripped.endswith("]]")):
        return None
    return stripped[2:-2].strip() or None


def _block_end(lines: list[str], header: int) -> int:
    """一个表块的结束行：下一个任意表头（``[x]`` 或 ``[[x]]``）之前。"""
    for index in range(header + 1, len(lines)):
        if _header_name(lines[index]) is not None or _array_header_name(lines[index]) is not None:
            return index
    return len(lines)


def _section_insert_index(lines: list[str], section: str) -> int:
    """``[section]`` 最后一个键之后的位置（小节不存在时返回文件末尾）。"""
    header = _section_header_index(lines, section)
    if header is None:
        return len(lines)
    end = _block_end(lines, header)
    while end > header + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _key_line_index(lines: list[str], header: int, end: int, key: str) -> int | None:
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for index in range(header + 1, min(end, len(lines))):
        if pattern.match(lines[index]):
            return index
    return None


def _insert_index(lines: list[str], header: int, preferred: int) -> int:
    """新键插到小节末尾（跳过尾部空行，让新增键紧跟最后一个键）。"""
    index = max(min(preferred, len(lines)), header + 1)
    while index > header + 1 and not lines[index - 1].strip():
        index -= 1
    return index


def _append_section(
    lines: list[str],
    section: str,
    values: Mapping[str, Any],
    header_comment: str,
) -> list[str]:
    body = [line for line in lines]
    if body and body[-1].strip():
        body.append("")
    if header_comment:
        for line in header_comment.splitlines():
            body.append("# " + line.strip())
    body.append(f"[{section}]")
    for key, value in values.items():
        if value is None:
            continue
        body.append(f"{_check_key(key)} = {toml_value(value)}")
    return body


def _read_text(path: str) -> str:
    path = os.fspath(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigEditError(f"读取 {path} 失败：{exc}") from exc


def _commit(path: str, old_text: str, new_text: str, *, validate_toml: bool = True) -> bool:
    """校验并原子写回；内容没变时不动文件。"""
    if new_text == old_text:
        return False
    if validate_toml:
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError as exc:  # 理论不可达，兜底防写坏
            raise ConfigEditError(
                f"准备写入 {os.path.basename(path)} 的内容不是合法 TOML：{exc}"
            ) from exc
    _backup(path, old_text)
    _atomic_write(path, new_text)
    return True


def _atomic_write(path: str, text: str) -> None:
    path = os.fspath(path)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise ConfigEditError(f"写入 {path} 失败：{exc}") from exc


def _backup(path: str, text: str) -> None:
    if not text:
        return
    path = os.fspath(path)
    try:
        with open(path + BACKUP_SUFFIX, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def read_env(path: str) -> dict[str, str]:
    """读配置根 ``.env``（同名变量首次出现者生效）。"""
    try:
        return load_env_file(path)
    except Exception as exc:  # noqa: BLE001 - 读不到就当没配
        raise ConfigEditError(f"读取 .env 失败：{exc}") from exc


def set_env_value(path: str, name: str, value: str) -> bool:
    """写 / 改 ``.env`` 里的一行；保留其他行与注释，返回是否改动。"""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ConfigEditError(f"非法的环境变量名：{name!r}")
    # dotenv 是一行一个键，值里不能带换行（误粘贴时静默折成一行，避免写坏文件）。
    value = str(value).replace("\r", "").replace("\n", "").strip()
    text = _read_text(path)
    lines = text.splitlines()
    entry = f"{name}={value}"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == name:
            lines[index] = entry
            break
    else:
        lines.append(entry)
    return _commit(path, text, "\n".join(lines) + "\n", validate_toml=False)


def pop_env_value(path: str, name: str) -> bool:
    """删掉 ``.env`` 里的 ``name`` 那一行（清空密钥用），返回是否改动。"""
    text = _read_text(path)
    kept: list[str] = []
    removed = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            if stripped.partition("=")[0].strip() == name:
                removed = True
                continue
        kept.append(line)
    if not removed:
        return False
    return _commit(path, text, "\n".join(kept) + "\n", validate_toml=False)
