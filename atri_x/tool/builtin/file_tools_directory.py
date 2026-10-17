"""``read_file`` 的目录搜索模式：递归 grep（走 ripgrep）与递归 AST 检索。

对应 Go：``internal/tool/builtin/file_tools_directory.go``。

与 Go 的差异（仅形式）：

* ``sync.Mutex`` → :class:`threading.Lock`（缓存是同步内存结构）。
* ``exec.CommandContext`` → :func:`asyncio.create_subprocess_exec`；ripgrep 依旧从
  PATH 查找，找不到时给出与 Go 相同的安装提示。
* ``time.Now`` → :func:`datetime.now` （带时区）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.errors import InvalidError
from atri_x.tool.builtin.common import go_quote
from atri_x.tool.builtin.file_tools_ast import (
    READ_FILE_MODE_AST,
    READ_FILE_MODE_AST_FUNCTION,
    READ_FILE_MODE_GREP,
    ASTFunctionMatch,
    ASTMatch,
    detect_ast_language,
    find_go_ast_functions,
    find_go_ast_matches,
    find_shell_ast_functions,
    find_shell_ast_matches,
)
from atri_x.tool.tool import Result
from atri_x.utils.fileops import (
    MAX_FILE_SIZE,
    normalize_grep_context_lines,
    normalize_max_matches,
    read_file_with_limit,
    split_lines,
)

FILE_TOOL_DETAILED_MAX_BYTES = MAX_FILE_SIZE
FILE_TOOL_MAX_BYTES = 100 * 1024 * 1024

DIRECTORY_AST_CACHE_TTL = timedelta(minutes=1)
DIRECTORY_AST_CACHE_MAX_SIZE = 64
_SKIP_DIRS = frozenset({".git", "node_modules", "vendor"})
_LARGE_PREVIEW_LIMIT = 5


@dataclass
class DirectorySearchMatch:
    """目录搜索的一条命中。对应 Go ``directorySearchMatch``。"""

    path: str = ""
    line: int = 0
    column: int = 0
    end_line: int = 0
    kind: str = ""
    label: str = ""
    text: str = ""
    language: str = ""


@dataclass(frozen=True)
class DirectoryASTCacheKey:
    """对应 Go ``directoryASTCacheKey``。"""

    root: str = ""
    mode: str = ""
    query: str = ""


@dataclass(frozen=True)
class DirectoryASTFileState:
    """对应 Go ``directoryASTFileState``。"""

    path: str = ""
    size: int = 0
    mod_time: int = 0


@dataclass
class DirectoryASTCacheEntry:
    """对应 Go ``directoryASTCacheEntry``。"""

    matches: list[DirectorySearchMatch] = field(default_factory=list)
    files: list[DirectoryASTFileState] = field(default_factory=list)
    expires_at: datetime | None = None
    used_at: datetime | None = None


def directory_large_ast_warnings(
    files: list[DirectoryASTFileState], mode: str
) -> list[str]:
    """对应 Go ``directoryLargeASTWarnings``。"""
    large: list[str] = []
    count = 0
    for item in files:
        if item.size <= FILE_TOOL_DETAILED_MAX_BYTES:
            continue
        count += 1
        if len(large) < _LARGE_PREVIEW_LIMIT:
            large.append(f"{item.path} ({item.size} bytes)")
    if count == 0:
        return []
    extra = f"; {count - len(large)} more" if count > len(large) else ""
    return [
        f"{mode} skipped {count} large files: {', '.join(large)}{extra}"
    ]


class DirectoryASTCache:
    """目录 AST 检索结果的短 TTL 缓存。对应 Go ``directoryASTCache``。"""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._entries: dict[DirectoryASTCacheKey, DirectoryASTCacheEntry] = {}

    def load(
        self, root: str, mode: str, query: str
    ) -> tuple[list[DirectorySearchMatch], list[str], bool]:
        """对应 Go ``DirectoryASTCache.load``。"""
        key = DirectoryASTCacheKey(root, mode, query)
        with self._mu:
            entry = self._entries.get(key)
            if entry is None or _now() > (entry.expires_at or _now()):
                self._entries.pop(key, None)
                return [], [], False
            if not directory_ast_files_match(root, entry.files):
                self._entries.pop(key, None)
                return [], [], False
            entry.used_at = _now()
            self._entries[key] = entry
            return (
                list(entry.matches),
                directory_large_ast_warnings(entry.files, mode),
                True,
            )

    def store(
        self,
        root: str,
        mode: str,
        query: str,
        matches: list[DirectorySearchMatch],
        files: list[DirectoryASTFileState],
    ) -> None:
        """对应 Go ``DirectoryASTCache.store``（超出上限时淘汰最久未使用者）。"""
        key = DirectoryASTCacheKey(root, mode, query)
        now = _now()
        with self._mu:
            if key not in self._entries:
                while len(self._entries) >= DIRECTORY_AST_CACHE_MAX_SIZE:
                    oldest_key = None
                    oldest_time = None
                    for candidate_key, entry in self._entries.items():
                        used = entry.used_at or now
                        if oldest_time is None or used < oldest_time:
                            oldest_key, oldest_time = candidate_key, used
                    if oldest_key is None:
                        break
                    del self._entries[oldest_key]
            self._entries[key] = DirectoryASTCacheEntry(
                matches=list(matches),
                files=sorted(files, key=lambda item: item.path),
                expires_at=now + DIRECTORY_AST_CACHE_TTL,
                used_at=now,
            )


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def read_file_directory_search(
    ctx: Any,
    root: str,
    mode: str,
    args: Any,
    warnings: list[str],
    ast_cache: DirectoryASTCache | None,
) -> Result:
    """对应 Go ``readFileDirectorySearch``。"""
    query = (args.query or "").strip()
    if query == "":
        raise InvalidError(f"query is required when mode is {mode}")
    matches: list[DirectorySearchMatch] = []
    if mode == READ_FILE_MODE_GREP:
        matches, grep_warnings = await find_directory_grep_matches(ctx, root, query)
        warnings = [*warnings, *grep_warnings]
    elif mode in (READ_FILE_MODE_AST, READ_FILE_MODE_AST_FUNCTION):
        cached, cached_warnings, ok = (
            ast_cache.load(root, mode, query)
            if ast_cache is not None
            else ([], [], False)
        )
        if ok:
            matches = cached
            warnings = [*warnings, *cached_warnings]
        else:
            matches, files = find_directory_ast_matches(root, query, mode)
            if ast_cache is not None:
                ast_cache.store(root, mode, query, matches, files)
            warnings = [*warnings, *directory_large_ast_warnings(files, mode)]
    else:
        raise InvalidError(f"unsupported read_file mode: {mode}")

    matches.sort(key=lambda item: (item.path, item.line))
    return format_directory_search_matches(
        root, mode, query, matches, args.context_lines, args.max_matches, args.index, warnings
    )


async def find_directory_grep_matches(
    ctx: Any, root: str, query: str
) -> tuple[list[DirectorySearchMatch], list[str]]:
    """对应 Go ``findDirectoryGrepMatches``：走 ``rg --json``。"""
    warnings = directory_large_file_warnings(root, FILE_TOOL_MAX_BYTES, "grep")
    rg = shutil.which("rg")
    if not rg:
        raise InvalidError(
            "directory grep requires ripgrep (rg), but rg was not found in PATH. "
            "Ask the user to install ripgrep and make `rg` available in PATH"
        )
    argv = [
        rg,
        "--json",
        "--fixed-strings",
        "--no-messages",
        "--max-filesize",
        str(FILE_TOOL_MAX_BYTES),
        "--glob",
        "!**/.git/**",
        "--glob",
        "!**/node_modules/**",
        "--glob",
        "!**/vendor/**",
        "--",
        query,
        root,
    ]
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode not in (0, 1):
        raise InvalidError(f"run ripgrep: exit code {process.returncode}")

    matches: list[DirectorySearchMatch] = []
    for raw_line in (stdout or b"").decode("utf-8", "replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path_text = ((data.get("path") or {}).get("text")) or ""
        if not path_text:
            continue
        column = 1
        submatches = data.get("submatches") or []
        if submatches:
            column += int(submatches[0].get("start") or 0)
        try:
            rel = os.path.relpath(path_text, root)
        except ValueError:
            continue
        line_number = int(data.get("line_number") or 0)
        text = ((data.get("lines") or {}).get("text")) or ""
        matches.append(
            DirectorySearchMatch(
                path=rel.replace("\\", "/"),
                line=line_number,
                column=column,
                end_line=line_number,
                kind="grep",
                text=text.rstrip("\n"),
            )
        )
    return matches, warnings


def directory_large_file_warnings(root: str, max_bytes: int, mode: str) -> list[str]:
    """对应 Go ``directoryLargeFileWarnings``。"""
    large: list[str] = []
    count = 0
    for path in _walk_files(root):
        try:
            size = os.stat(path).st_size
        except OSError:
            continue
        if size <= max_bytes:
            continue
        count += 1
        if len(large) < _LARGE_PREVIEW_LIMIT:
            rel = os.path.relpath(path, root).replace("\\", "/")
            large.append(f"{rel} ({size} bytes)")
    if count == 0:
        return []
    extra = f"; {count - len(large)} more" if count > len(large) else ""
    return [f"{mode} skipped {count} large files: {', '.join(large)}{extra}"]


def find_directory_ast_matches(
    root: str, query: str, mode: str
) -> tuple[list[DirectorySearchMatch], list[DirectoryASTFileState]]:
    """对应 Go ``findDirectoryASTMatches``。"""
    matches: list[DirectorySearchMatch] = []
    files: list[DirectoryASTFileState] = []
    for path in _walk_files(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        files.append(
            DirectoryASTFileState(
                path=rel, size=st.st_size, mod_time=st.st_mtime_ns
            )
        )
        if st.st_size > FILE_TOOL_DETAILED_MAX_BYTES:
            continue
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        text = data.decode("utf-8", "replace")
        try:
            language = detect_ast_language(path, text)
        except InvalidError:
            continue
        if mode == READ_FILE_MODE_AST_FUNCTION:
            functions = _ast_functions(language, path, text, query)
            for function in functions:
                matches.append(
                    DirectorySearchMatch(
                        path=rel,
                        line=function.start_line,
                        end_line=function.end_line,
                        kind=function.kind,
                        label=function.name,
                        language=language,
                    )
                )
            continue
        identifiers = _ast_identifiers(language, path, text, query)
        for identifier in identifiers:
            matches.append(
                DirectorySearchMatch(
                    path=rel,
                    line=identifier.line,
                    column=identifier.column,
                    end_line=identifier.line,
                    kind=identifier.kind,
                    label=identifier.container,
                    language=language,
                )
            )
    return matches, files


def _ast_functions(
    language: str, path: str, text: str, query: str
) -> list[ASTFunctionMatch]:
    if language == "go":
        return find_go_ast_functions(path, text, query)
    return find_shell_ast_functions(text, query)


def _ast_identifiers(language: str, path: str, text: str, query: str) -> list[ASTMatch]:
    if language == "go":
        return find_go_ast_matches(path, text, query)
    return find_shell_ast_matches(text, query)


def _walk_files(root: str):
    """对应 Go 的 ``filepath.WalkDir``（跳过 .git / node_modules / vendor）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def directory_ast_files_match(
    root: str, cached: list[DirectoryASTFileState]
) -> bool:
    """对应 Go ``directoryASTFilesMatch``：逐个文件的 size/mtime 比对。"""
    current: list[DirectoryASTFileState] = []
    for path in _walk_files(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        rel = os.path.relpath(path, root).replace("\\", "/")
        current.append(
            DirectoryASTFileState(
                path=rel, size=st.st_size, mod_time=st.st_mtime_ns
            )
        )
    if len(current) != len(cached):
        return False
    current.sort(key=lambda item: item.path)
    return current == list(cached)


def format_directory_search_matches(
    root: str,
    mode: str,
    query: str,
    matches: list[DirectorySearchMatch],
    context_lines: int,
    max_matches: int,
    index: int,
    warnings: list[str],
) -> Result:
    """对应 Go ``formatDirectorySearchMatches``。"""
    max_matches = normalize_max_matches(max_matches)
    if index < 0 or index > len(matches):
        raise InvalidError(f"index {index} is out of range; found {len(matches)} matches")
    truncated = index == 0 and len(matches) > max_matches
    shown = matches
    if index > 0:
        shown = matches[index - 1 : index]
    elif truncated:
        shown = matches[:max_matches]
    out = [
        f"path: {root}\n",
        f"mode: {mode}\n",
        f"query: {go_quote(query)}\n",
        f"matches: {len(matches)}\n",
        f"truncated: {'true' if truncated else 'false'}\n",
    ]
    if not matches:
        out.append("content:\n")
        return Result(content="".join(out), warnings=warnings)
    if index > 0:
        out.append(f"index: {index}\ncontent:\n")
        return format_directory_search_selection(
            out, root, mode, shown[0], context_lines, warnings
        )
    if len(matches) > 1 or mode != READ_FILE_MODE_AST_FUNCTION:
        if mode == READ_FILE_MODE_AST_FUNCTION and len(matches) > 1:
            out.append("selection_required: true\n")
        out.append("content:\n")
        for position, match in enumerate(shown):
            if mode == READ_FILE_MODE_AST_FUNCTION:
                out.append(
                    f"{position + 1}. {match.label} - {match.path}:"
                    f"{match.line}-{match.end_line}\n"
                )
            else:
                out.append(
                    f"{position + 1}. {match.path}:{match.line}:{match.column} "
                    f"[{match.kind}] {match.label}\n"
                )
        return Result(content="".join(out), warnings=warnings)
    out.append("content:\n")
    return format_directory_search_selection(
        out, root, mode, shown[0], context_lines, warnings
    )


def format_directory_search_selection(
    out: list[str],
    root: str,
    mode: str,
    match: DirectorySearchMatch,
    context_lines: int,
    warnings: list[str],
) -> Result:
    """对应 Go ``formatDirectorySearchSelection``。"""
    path = os.path.join(root, match.path.replace("/", os.sep))
    max_bytes = MAX_FILE_SIZE if mode != READ_FILE_MODE_GREP else FILE_TOOL_MAX_BYTES
    file = read_file_with_limit(path, "", max_bytes)
    lines = split_lines(file.text)
    if len(file.bytes) > FILE_TOOL_DETAILED_MAX_BYTES:
        warnings = [
            *warnings,
            f"large file: {len(file.bytes)} bytes; ast and ast_function modes are disabled",
        ]
    start, end = match.line, match.end_line
    if mode != READ_FILE_MODE_AST_FUNCTION:
        context_lines = normalize_grep_context_lines(context_lines)
        start = max(1, match.line - context_lines)
        end = min(len(lines), match.line + context_lines)
    out.append(
        f"match: {match.path}:{match.line}-{match.end_line} "
        f"[{match.kind}] {match.label}\n"
    )
    width = len(str(len(lines)))
    for line in range(start, end + 1):
        out.append(f"  {line:>{width}} | {lines[line - 1]}\n")
    return Result(content="".join(out), warnings=warnings)


__all__ = [name for name in dir() if not name.startswith("_")]
