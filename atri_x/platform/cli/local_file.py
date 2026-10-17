"""TUI 的 ``#文件引用`` 补全与展开。

对应 Go：``internal/platform/cli/tui_file_reference.go``。

职责有两半，彼此独立：

* **补全**：识别光标处的 ``#path`` / ``#"path with space"`` 片段，把工作目录下的
  相对路径按模糊评分排序后转成 :class:`atri_x.completion.completion.Item`。
* **展开**：提交输入前把整条文本里的 ``#引用`` 就地替换成
  ``[file: path]`` + 围栏代码块；引用不存在时**保留原文**（与 Go 的
  ``errors.Is(err, fs.ErrNotExist)`` 分支一致），其余失败抛
  :class:`atri_x.errors.InvalidError`。

与 Go 的差异（仅形式）：

* Go 按 **UTF-8 字节**索引（``text[i]`` 取的是字节），Python ``str`` 按**码点**索引。
  本模块统一按码点实现，并保证 :class:`atri_x.completion.completion.Item` 的
  ``replace_start`` / ``replace_end`` 是**码点偏移** —— 这与
  ``atri_x.completion.completion.Request.cursor``（「``cursor`` 为字符偏移」）同口径。
  ASCII 输入下两者完全一致；含非 ASCII 路径时 Python 侧的行为更正确。
* Go 的 ``Complete`` 是同步方法；Python 侧为了能直接当
  :class:`atri_x.completion.completion.Source` 用，:meth:`LocalFileResolver.complete`
  是协程，另提供同步的 :meth:`LocalFileResolver.complete_sync` 给 Elm 式的同步
  ``update()`` 路径使用。
* ``filepath.ToSlash`` → :func:`to_slash`（仅在 Windows 上把 ``\\`` 换成 ``/``）。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Any

from atri_x.completion.completion import Item, Request
from atri_x.errors import InvalidError

#: 补全条目类型。对应 Go ``kindLocalFileReference``。
KIND_LOCAL_FILE_REFERENCE = "local_file"

#: 补全条目描述。对应 Go ``localFileReferenceDescription``。
LOCAL_FILE_REFERENCE_DESCRIPTION = "local file"

#: 补全候选上限。对应 Go ``maxLocalFileCompletionItems``。
MAX_LOCAL_FILE_COMPLETION_ITEMS = 50

#: 扫描文件数上限。对应 Go ``maxLocalFileCompletionScan``。
MAX_LOCAL_FILE_COMPLETION_SCAN = 5000

#: 单个引用文件大小上限（字节）。对应 Go ``defaultLocalFileMaxSize``。
DEFAULT_LOCAL_FILE_MAX_SIZE = 256 * 1024

#: 一次展开的引用总大小上限（字节）。对应 Go ``defaultLocalFileMaxTotalSize``。
DEFAULT_LOCAL_FILE_MAX_TOTAL_SIZE = 1024 * 1024

#: 补全扫描跳过的目录名。对应 Go ``localFileIgnoredDirs``。
LOCAL_FILE_IGNORED_DIRS = frozenset({".git", "node_modules", "vendor", "dist", "build"})

#: 引用边界字符。对应 Go ``isReferenceBoundary``。
_REFERENCE_BOUNDARIES = frozenset({" ", "\t", "\r", "\n"})

__all__ = [
    "DEFAULT_LOCAL_FILE_MAX_SIZE",
    "DEFAULT_LOCAL_FILE_MAX_TOTAL_SIZE",
    "KIND_LOCAL_FILE_REFERENCE",
    "LOCAL_FILE_IGNORED_DIRS",
    "LOCAL_FILE_REFERENCE_DESCRIPTION",
    "MAX_LOCAL_FILE_COMPLETION_ITEMS",
    "MAX_LOCAL_FILE_COMPLETION_SCAN",
    "LocalFileCandidate",
    "LocalFileReferenceToken",
    "LocalFileResolver",
    "format_local_file_expansion",
    "format_local_file_reference",
    "is_reference_boundary",
    "local_file_fuzzy_score",
    "new_local_file_resolver",
    "parse_local_file_completion_token",
    "parse_local_file_reference_at",
    "segment_prefix_match",
    "subsequence_score",
    "to_slash",
]


def to_slash(value: str) -> str:
    """对应 Go ``filepath.ToSlash``。"""
    if os.sep == "/":
        return value
    return value.replace(os.sep, "/")


def is_reference_boundary(char: str) -> bool:
    """对应 Go ``isReferenceBoundary``。"""
    return char in _REFERENCE_BOUNDARIES


@dataclass
class LocalFileReferenceToken:
    """光标处识别到的一段引用片段。对应 Go ``localFileReferenceToken``。"""

    start: int = 0
    end: int = 0
    query: str = ""
    quoted: bool = False


@dataclass
class LocalFileCandidate:
    """一个补全候选（仓库相对路径 + 模糊分）。对应 Go ``localFileCandidate``。"""

    path: str = ""
    score: int = 0


@dataclass
class LocalFileResolver:
    """把工作目录下的文件解析成 ``#引用`` 的补全与展开器。

    对应 Go ``localFileResolver``。
    """

    root: str = ""
    max_file_size: int = DEFAULT_LOCAL_FILE_MAX_SIZE
    max_total: int = DEFAULT_LOCAL_FILE_MAX_TOTAL_SIZE

    # -- 补全 ---------------------------------------------------------------
    async def complete(self, ctx: Any, req: Request) -> list[Item]:
        """协程版补全（可直接当 :class:`atri_x.completion.completion.Source`）。"""
        return self.complete_sync(ctx, req)

    def complete_sync(self, ctx: Any, req: Request) -> list[Item]:
        """对应 Go ``localFileResolver.Complete``。"""
        if not (self.root or "").strip():
            return []
        token, ok = parse_local_file_completion_token(req.text, req.cursor_or_end())
        if not ok:
            return []
        candidates = self.matching_files(token.query)
        if not candidates:
            return []
        if len(candidates) > MAX_LOCAL_FILE_COMPLETION_ITEMS:
            candidates = candidates[:MAX_LOCAL_FILE_COMPLETION_ITEMS]
        return [
            Item(
                text=format_local_file_reference(candidate.path, token.quoted),
                label=candidate.path,
                description=LOCAL_FILE_REFERENCE_DESCRIPTION,
                kind=KIND_LOCAL_FILE_REFERENCE,
                replace_start=token.start,
                replace_end=token.end,
            )
            for candidate in candidates
        ]

    def matching_files(self, query: str) -> list[LocalFileCandidate]:
        """扫描工作目录并按模糊分排序。对应 Go ``localFileResolver.matchingFiles``。"""
        candidates: list[LocalFileCandidate] = []
        scanned = 0
        stopped = False
        for dir_path, dir_names, file_names in os.walk(self.root, topdown=True):
            if stopped:
                break
            dir_names[:] = [name for name in dir_names if name not in LOCAL_FILE_IGNORED_DIRS]
            dir_names.sort()
            for name in sorted(file_names):
                path = os.path.join(dir_path, name)
                if scanned >= MAX_LOCAL_FILE_COMPLETION_SCAN:
                    stopped = True
                    break
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                scanned += 1
                try:
                    rel = os.path.relpath(path, self.root)
                except ValueError:  # pragma: no cover - 跨盘符（Windows）才会发生
                    continue
                display_path = to_slash(rel)
                score, ok = local_file_fuzzy_score(display_path, query)
                if not ok:
                    continue
                candidates.append(LocalFileCandidate(path=display_path, score=score))
        candidates.sort(key=lambda item: (-item.score, len(item.path), item.path))
        return candidates

    # -- 展开 ---------------------------------------------------------------
    def expand_references(self, text: str) -> str:
        """把文本里的 ``#引用`` 展开成文件内容块。对应 Go ``expandReferences``。

        引用指向不存在的文件时保留原文（对应 Go 的 ``fs.ErrNotExist`` 分支）；
        引用非法、文件过大或疑似二进制时抛 :class:`atri_x.errors.InvalidError`。
        """
        out: list[str] = []
        total = 0
        changed = False
        index = 0
        length = len(text)
        while index < length:
            if text[index] != "#":
                out.append(text[index])
                index += 1
                continue
            ref_path, end, ok = parse_local_file_reference_at(text, index)
            if not ok:
                out.append(text[index])
                index += 1
                continue
            try:
                expansion, total = self._expand_reference(ref_path, total)
            except FileNotFoundError:
                out.append(text[index:end])
                index = end
                continue
            out.append(expansion)
            index = end
            changed = True
        if not changed:
            return text
        return "".join(out)

    def _expand_reference(self, ref_path: str, total: int) -> tuple[str, int]:
        """展开单个引用，返回 ``(展开文本, 新的累计字节数)``。对应 Go ``expandReference``。"""
        display_path = to_slash(ref_path)
        clean = os.path.normpath(ref_path)
        if (
            clean in (".", os.sep)
            or os.path.isabs(clean)
            or clean == ".."
            or clean.startswith(".." + os.sep)
        ):
            raise InvalidError(
                f'local file reference "{ref_path}" is outside the current directory'
            )
        absolute = os.path.join(self.root, clean)
        rel = os.path.relpath(absolute, self.root)
        if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
            raise InvalidError(
                f'local file reference "{ref_path}" is outside the current directory'
            )
        try:
            info = os.stat(absolute)
        except FileNotFoundError:
            raise FileNotFoundError(f'read local file reference "{ref_path}"') from None
        except OSError as exc:
            raise InvalidError(f'read local file reference "{ref_path}": {exc}') from exc
        if stat.S_ISDIR(info.st_mode):
            raise InvalidError(f'local file reference "{ref_path}" is a directory')
        if not stat.S_ISREG(info.st_mode):
            raise InvalidError(f'local file reference "{ref_path}" is not a regular file')
        max_file_size = self.max_file_size if self.max_file_size > 0 else DEFAULT_LOCAL_FILE_MAX_SIZE
        if info.st_size > max_file_size:
            raise InvalidError(
                f'local file reference "{ref_path}" is too large '
                f"({info.st_size} bytes > {max_file_size} bytes)"
            )
        max_total = self.max_total if self.max_total > 0 else DEFAULT_LOCAL_FILE_MAX_TOTAL_SIZE
        if total + info.st_size > max_total:
            raise InvalidError(
                "local file references are too large "
                f"({total + info.st_size} bytes > {max_total} bytes)"
            )
        try:
            with open(absolute, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise InvalidError(f'read local file reference "{ref_path}": {exc}') from exc
        if b"\x00" in data:
            raise InvalidError(f'local file reference "{ref_path}" appears to be binary')
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidError(
                f'local file reference "{ref_path}" appears to be binary'
            ) from exc
        return format_local_file_expansion(display_path, content), total + len(data)


def new_local_file_resolver(root: str) -> LocalFileResolver:
    """Go ``newLocalFileResolver`` 的等价物（空 root 取当前工作目录并转绝对路径）。"""
    if not (root or "").strip():
        root = os.getcwd()
    return LocalFileResolver(root=os.path.abspath(root))


# ---------------------------------------------------------------------------
# 片段解析
# ---------------------------------------------------------------------------

def parse_local_file_completion_token(text: str, cursor: int) -> tuple[LocalFileReferenceToken, bool]:
    """识别光标处的 ``#引用`` 片段。对应 Go ``parseLocalFileCompletionToken``。

    先找 ``#"`` 开头的引号形式（从光标往前扫），找不到再找同一段（以空白为界）
    里以 ``#`` 开头的裸形式。返回 ``(token, ok)``。
    """
    if cursor < 0 or cursor > len(text):
        cursor = len(text)
    for start in range(cursor - 1, -1, -1):
        if text[start] != "#" or start + 1 >= len(text) or text[start + 1] != '"':
            continue
        if start > 0 and not is_reference_boundary(text[start - 1]):
            continue
        if '"' in text[start + 2 : cursor]:
            continue
        return (
            LocalFileReferenceToken(start=start, end=cursor, query=text[start + 2 : cursor], quoted=True),
            True,
        )
    start = cursor
    while start > 0 and not is_reference_boundary(text[start - 1]):
        start -= 1
    if start >= cursor or text[start] != "#":
        return LocalFileReferenceToken(), False
    if start + 1 < len(text) and text[start + 1] == '"':
        return LocalFileReferenceToken(), False
    return (
        LocalFileReferenceToken(start=start, end=cursor, query=text[start + 1 : cursor]),
        True,
    )


def parse_local_file_reference_at(text: str, start: int) -> tuple[str, int, bool]:
    """从 ``text[start]`` 处的 ``#`` 解析一个完整引用。对应 Go ``parseLocalFileReferenceAt``。

    返回 ``(引用路径, 结束偏移, 是否命中)``；引号形式未闭合时抛
    :class:`atri_x.errors.InvalidError`（对应 Go 的 ``unterminated local file reference``）。
    """
    if start > 0 and not is_reference_boundary(text[start - 1]):
        return "", 0, False
    if start + 1 < len(text) and text[start + 1] == '"':
        end = start + 2
        while end < len(text) and text[end] != '"':
            end += 1
        if end >= len(text):
            raise InvalidError("unterminated local file reference")
        ref_path = text[start + 2 : end]
        if not ref_path.strip():
            return "", 0, False
        return ref_path, end + 1, True
    end = start + 1
    while end < len(text) and not text[end].isspace():
        end += 1
    if end == start + 1:
        return "", 0, False
    return text[start + 1 : end], end, True


# ---------------------------------------------------------------------------
# 模糊评分
# ---------------------------------------------------------------------------

def local_file_fuzzy_score(path: str, query: str) -> tuple[int, bool]:
    """路径模糊评分。对应 Go ``localFileFuzzyScore``。返回 ``(score, ok)``。"""
    query = to_slash(query).strip().lower()
    path_lower = path.lower()
    base_lower = os.path.basename(path_lower)
    if query == "":
        return 1000 - len(path_lower), True
    if path_lower == query or base_lower == query:
        return 10000 - len(path_lower), True
    if path_lower.startswith(query):
        return 9000 - len(path_lower), True
    if base_lower.startswith(query):
        return 8800 - len(path_lower), True
    if segment_prefix_match(path_lower, query):
        return 8500 - len(path_lower), True
    index = path_lower.find(query)
    if index >= 0:
        return 7600 - index - len(path_lower), True
    index = base_lower.find(query)
    if index >= 0:
        return 7400 - index - len(path_lower), True
    score, ok = subsequence_score(path_lower, query)
    if not ok:
        return 0, False
    return score - len(path_lower), True


def segment_prefix_match(path: str, query: str) -> bool:
    """任一路径段以 query 开头。对应 Go ``segmentPrefixMatch``。"""
    return any(segment.startswith(query) for segment in path.split("/"))


def subsequence_score(value: str, query: str) -> tuple[int, bool]:
    """子序列匹配评分。对应 Go ``subsequenceScore``。返回 ``(score, ok)``。"""
    query_index = 0
    last_match = -1
    adjacent = 0
    gaps = 0
    index = 0
    while index < len(value) and query_index < len(query):
        if value[index] != query[query_index]:
            index += 1
            continue
        if last_match >= 0:
            if index == last_match + 1:
                adjacent += 1
            else:
                gaps += index - last_match - 1
        last_match = index
        query_index += 1
        index += 1
    if query_index != len(query):
        return 0, False
    return 5000 + adjacent * 20 - gaps * 2, True


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def format_local_file_reference(path: str, force_quoted: bool) -> str:
    """把路径渲染成可插入输入框的引用文本。对应 Go ``formatLocalFileReference``。"""
    if force_quoted or any(char in path for char in " \t\r\n"):
        return '#"' + path + '"'
    return "#" + path


def format_local_file_expansion(path: str, content: str) -> str:
    """把文件内容渲染成引用展开块。对应 Go ``formatLocalFileExpansion``。

    围栏长度随正文里已有的反引号增长，避免内容提前闭合代码块。
    """
    fence = "```"
    while fence in content:
        fence += "`"
    if content and not content.endswith("\n"):
        content += "\n"
    return f"[file: {path}]\n{fence}text\n{content}{fence}"
