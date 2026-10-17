"""子进程环境的分层快照、PATH 补充与可执行文件解析。

对应 Go：``internal/processenv/environment.go``。

:class:`Environment` 是不可变的环境快照：``fill`` / ``overlay`` /
``without_sensitive_keys`` 都返回**新对象**。零值（``Environment()``，即没有
显式条目）保留 Go ``os/exec`` 的"继承当前进程环境"行为。

**凭据过滤**：Shell / Skill 子进程必须用
:meth:`Environment.without_sensitive_keys`，不要把 API key、token、
password 这类变量顺手传下去。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

__all__ = [
    "Command",
    "Environment",
    "SensitiveEnvName",
    "executable_candidates",
    "find_executable",
    "merge_path_lists",
    "new",
    "sensitive_env_name",
]

_IS_WINDOWS = os.name == "nt"
_SENSITIVE_PARTS = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "PRIVATE",
    }
)
_SENSITIVE_MARKERS = (
    "APIKEY",
    "ACCESSKEY",
    "SECRETKEY",
    "PRIVATEKEY",
    "CLIENTSECRET",
    "BOTTOKEN",
)
_DEFAULT_PATH_EXT = (".com", ".exe", ".bat", ".cmd")


def environment_key(name: str) -> str:
    """环境变量名的比较键：Windows 大小写不敏感，其它平台敏感。"""
    return name.upper() if _IS_WINDOWS else name


def sensitive_env_name(name: str) -> bool:
    """变量名是否像凭据、令牌、密钥或私钥。"""
    name = (name or "").strip().upper()
    parts = [part for part in _split_env_name(name) if part]
    if any(part in _SENSITIVE_PARTS for part in parts):
        return True
    compact = name.replace("_", "").replace("-", "").replace(".", "")
    return any(marker in compact for marker in _SENSITIVE_MARKERS)


def _split_env_name(name: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for ch in name:
        if ch in "_-.":
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    out.append("".join(current))
    return out


def merge_path_lists(primary: str, extra: str) -> str:
    """把两段 PATH 去重合并（``primary`` 在前，Windows 忽略大小写去重）。"""
    seen: set[str] = set()
    merged: list[str] = []
    for value in (primary, extra):
        for entry in (value or "").split(os.pathsep):
            if not entry:
                continue
            key = entry.upper() if _IS_WINDOWS else entry
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
    return os.pathsep.join(merged)


def executable_candidates(path: str, path_ext: str) -> list[str]:
    """返回一个候选路径在该平台上可能的可执行文件名。"""
    if not _IS_WINDOWS:
        return [path]
    extensions = [ext for ext in (path_ext or "").split(os.pathsep) if ext]
    if not extensions:
        extensions = list(_DEFAULT_PATH_EXT)
    current = os.path.splitext(path)[1].lower()
    for candidate_ext in extensions:
        if current == candidate_ext.lower():
            return [path]
    return [path + ext for ext in extensions if ext]


def find_executable(name: str, path_value: str, path_ext: str) -> str | None:
    """在显式 PATH 里找可执行文件（不读进程环境）。"""
    if not name or os.path.isabs(name) or os.path.splitdrive(name)[0]:
        return None
    if "/" in name or "\\" in name:
        return None
    for directory in (path_value or "").split(os.pathsep):
        if not directory or not os.path.isabs(directory):
            continue
        base = os.path.join(directory, name)
        for candidate in executable_candidates(base, path_ext):
            try:
                info = os.stat(candidate)
            except OSError:
                continue
            if os.path.isdir(candidate):
                continue
            if not _IS_WINDOWS and info.st_mode & 0o111 == 0:
                continue
            return candidate
    return None


@dataclass
class Command:
    """Go ``*exec.Cmd`` 的 Python 对应物。

    ``executable`` 是解析后的绝对路径（解析不到时等于 ``args[0]``），
    ``args[0]`` 保留调用方给的原名字，``env`` 是 ``NAME=value`` 列表。
    """

    args: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    executable: str = ""

    def env_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in self.env:
            name, _, value = entry.partition("=")
            if name:
                out[name] = value
        return out

    def popen(self, **kwargs) -> subprocess.Popen:
        """按 Go ``exec.Cmd`` 的语义起进程（env 显式传入）。"""
        kwargs.setdefault("env", self.env_dict())
        return subprocess.Popen(self.args, executable=self.executable, **kwargs)


class Environment:
    """不可变的环境快照。对应 Go ``processenv.Environment``。"""

    __slots__ = ("_configured", "_environ", "_path", "_path_ext")

    def __init__(self, entries: list[str] | None = None) -> None:
        if entries is None:
            self._configured = False
            self._environ: list[str] = []
            self._path = ""
            self._path_ext = ""
            return
        self._configured = True
        self._environ = []
        self._path = ""
        self._path_ext = ""
        indexes: dict[str, int] = {}
        for entry in entries:
            name, sep, value = entry.partition("=")
            if not sep or not name.strip():
                continue
            key = environment_key(name)
            if key in indexes:
                continue
            indexes[key] = len(self._environ)
            self._environ.append(name + "=" + value)
            self._set_cached(key, value)

    # -- 访问 ---------------------------------------------------------------

    def configured(self) -> bool:
        """是否是显式快照（否则继承当前进程环境）。"""
        return self._configured

    def environ(self) -> list[str]:
        """返回有效环境的 ``NAME=value`` 列表副本。"""
        if not self._configured:
            return [f"{key}={value}" for key, value in os.environ.items()]
        return list(self._environ)

    def lookup(self, name: str) -> tuple[str, bool]:
        """按名查值（对应 Go ``Environment.Lookup``）。"""
        name = (name or "").strip()
        if not name:
            return "", False
        key = environment_key(name)
        for entry in self.environ():
            entry_name, sep, value = entry.partition("=")
            if sep and environment_key(entry_name) == key:
                return value, True
        return "", False

    def look_path(self, name: str) -> str:
        """用**有效环境**的 PATH 解析可执行文件。找不到抛 ``FileNotFoundError``。"""
        if self._configured:
            found = find_executable(name, self._path, self._path_ext)
            if found is None:
                raise FileNotFoundError(f'executable file not found in $PATH: "{name}"')
            return found
        found = shutil.which(name)
        if found is None:
            raise FileNotFoundError(f'executable file not found in $PATH: "{name}"')
        return found

    # -- 分层 ---------------------------------------------------------------

    def fill(self, values: dict[str, str]) -> "Environment":
        """补上缺失的值，保留已有条目（PATH 始终追加）。"""
        return self._merge(values, False)

    def overlay(self, values: dict[str, str]) -> "Environment":
        """用新层替换已有值（PATH 仍保持追加语义）。"""
        return self._merge(values, True)

    def without_sensitive_keys(self) -> "Environment":
        """返回移除了常见凭据变量的副本。"""
        filtered: list[str] = []
        for entry in self.environ():
            name, sep, _value = entry.partition("=")
            if not sep or not sensitive_env_name(name):
                filtered.append(entry)
        return Environment(filtered)

    def _merge(self, values: dict[str, str], replace: bool) -> "Environment":
        if not self._configured:
            merged = Environment([f"{key}={value}" for key, value in os.environ.items()])
        else:
            merged = Environment(list(self._environ))
        merged._configured = True

        indexes: dict[str, int] = {}
        current: dict[str, str] = {}
        for index, entry in enumerate(merged._environ):
            name, sep, value = entry.partition("=")
            if not sep or not name:
                continue
            key = environment_key(name)
            indexes[key] = index
            current[key] = value

        path_key = environment_key("PATH")
        for name in sorted(values):
            if not name.strip():
                continue
            key = environment_key(name)
            value = values[name]
            if key == path_key:
                value = merge_path_lists(current.get(key, ""), value)
                index = indexes.get(key)
                if index is None:
                    indexes[key] = len(merged._environ)
                    merged._environ.append(name + "=" + value)
                else:
                    original_name = merged._environ[index].partition("=")[0]
                    merged._environ[index] = original_name + "=" + value
                current[key] = value
                merged._set_cached(key, value)
                continue
            index = indexes.get(key)
            if index is not None:
                if not replace:
                    continue
                original_name = merged._environ[index].partition("=")[0]
                merged._environ[index] = original_name + "=" + value
            else:
                indexes[key] = len(merged._environ)
                merged._environ.append(name + "=" + value)
            current[key] = value
            merged._set_cached(key, value)
        return merged

    # -- 子进程 -------------------------------------------------------------

    def command(self, name: str, *args: str) -> Command:
        """构造一个子进程命令（对应 Go ``Environment.Command``）。"""
        return self.command_context(None, name, *args)

    def command_context(self, ctx, name: str, *args: str) -> Command:
        """同 :meth:`command`，但 `ctx` 已取消时直接抛错。

        ``ctx`` 允许为 ``None`` 或任何有 ``is_cancelled()`` / ``error()`` 的
        取消令牌（:class:`atri_x.safecontext.CancelToken`）。
        """
        if ctx is not None and ctx.is_cancelled():
            raise ctx.error() if ctx.error() is not None else RuntimeError("canceled")
        resolved = name
        try:
            resolved = self.look_path(name)
        except FileNotFoundError:
            resolved = name
        return Command(
            args=[name, *args],
            env=self.environ(),
            executable=resolved,
        )

    # -- 内部 ---------------------------------------------------------------

    def _set_cached(self, key: str, value: str) -> None:
        if key == environment_key("PATH"):
            self._path = value
        elif key == environment_key("PATHEXT"):
            self._path_ext = value

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Environment configured={self._configured} entries={len(self._environ)}>"


def new(entries: list[str] | None = None) -> Environment:
    """对应 Go ``processenv.New``。"""
    return Environment(entries)


# 便于交叉检索的 PascalCase 别名（Go 导出名）。
SensitiveEnvName = sensitive_env_name
