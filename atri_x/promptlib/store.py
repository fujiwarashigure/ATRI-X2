"""提示词库的 JSON 存储：``<配置目录>/gui/prompts.json``。

口径与 :mod:`atri_x.memberprofile.store` 一致：

1. **只存覆盖**：文件里只放"和内置默认不同"的条目与用户自建条目。内置模板
   升级后，用户没改过的条目自动跟随新默认文本；改过的条目保留用户版本。
2. **按指纹热更新**：``(mtime_ns, size)`` 变了才重读；文件删除后指纹归零，
   下一次读取回到"全部用内置默认"。后台每一轮读一次，保存后下一轮生效。
3. **损坏不静默**：读失败时 ``load_overrides()`` 返回空表（提示词照常工作，
   退回内置默认），但记一条 WARN；``save_overrides()`` 在文件损坏时拒绝写入，
   避免把用户的自定义提示词覆盖掉（GUI 会提示先修复或删除文件）。
4. **写入原子**：先写 ``.tmp`` 再 ``os.replace``，进程内用锁串行化。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Mapping

from atri_x.promptlib.model import (
    PROMPTS_FILE,
    PROMPTS_VERSION,
    PromptDataError,
    PromptEntry,
    normalize_entry,
    prompt_from_dict,
    prompt_to_dict,
)

__all__ = [
    "PromptStore",
    "load_prompt_overrides",
    "prompts_path",
    "save_prompt_overrides",
]

#: 提示词库在配置目录下的位置（与 ``members.json`` 同级的 GUI 数据目录）。
GUI_SUBDIR = "gui"


def prompts_path(config_dir: str) -> str:
    """提示词覆盖文件路径（``<配置目录>/gui/prompts.json``）。"""
    return os.path.join(config_dir or "", GUI_SUBDIR, PROMPTS_FILE)


def _file_signature(path: str) -> tuple[int, int] | None:
    """文件指纹 ``(mtime_ns, size)``；不存在或其他错误返回 ``None``。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def _read_file(path: str) -> tuple[dict[str, PromptEntry], str]:
    """读覆盖文件，返回 ``(entries, error)``；文件不存在时返回空表 + 空错误。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"读取提示词库失败：{exc}"
    if not isinstance(data, dict):
        return {}, "提示词库结构无效：顶层不是对象"
    raw_prompts = data.get("prompts")
    if raw_prompts is None:
        raw_prompts = {}
    if not isinstance(raw_prompts, dict):
        return {}, "提示词库结构无效：prompts 不是对象"
    entries: dict[str, PromptEntry] = {}
    for key, raw in raw_prompts.items():
        entry = prompt_from_dict(raw)
        if entry is None:
            continue
        if not entry.id:
            entry.id = str(key)
        entries[entry.id] = entry
    return entries, ""


class PromptStore:
    """提示词覆盖文件的读写（进程内缓存 + 文件指纹热更新；线程安全）。"""

    def __init__(self, config_dir: str, *, logger: Any = None) -> None:
        self.config_dir = config_dir or ""
        self.logger = logger
        self._lock = threading.RLock()
        self._loaded = False
        self._signature: tuple[int, int] | None = None
        self._entries: dict[str, PromptEntry] = {}
        self._error = ""
        self._logged_error = ""

    @property
    def path(self) -> str:
        """覆盖文件路径。"""
        return prompts_path(self.config_dir)

    @property
    def last_error(self) -> str:
        """最近一次读取失败的原因（成功读取后清空）。"""
        with self._lock:
            return self._error

    # -- 读取 ---------------------------------------------------------------
    def load_overrides(self) -> dict[str, PromptEntry]:
        """读全部覆盖条目（文件不存在/损坏时返回空表）。"""
        with self._lock:
            self._refresh_locked()
            return dict(self._entries)

    def _refresh_locked(self) -> bool:
        signature = _file_signature(self.path)
        if self._loaded and signature == self._signature:
            return False
        entries, error = _read_file(self.path)
        self._loaded = True
        self._entries = entries
        self._error = error
        if error:
            # 读失败不记指纹：下次再试（可能是文件正被替换/占用）。
            self._loaded = False
            self._signature = None
            if error != self._logged_error:
                self._logged_error = error
                self._warn("提示词库读取失败，已按内置默认处理", error)
            return True
        self._logged_error = ""
        self._signature = signature
        return True

    # -- 写入 ---------------------------------------------------------------
    def save_overrides(self, entries: Mapping[str, PromptEntry]) -> str:
        """整表写回覆盖条目；返回文件路径。空表也会写（保留合法空文件）。"""
        normalized: dict[str, PromptEntry] = {}
        for key, entry in (entries or {}).items():
            item = normalize_entry(entry)
            normalized[item.id or str(key)] = item
        with self._lock:
            self._read_for_write_locked()
            return self._save_locked(normalized)

    def _read_for_write_locked(self) -> None:
        """写前先读；读失败时拒绝写，避免用空表覆盖用户的自定义提示词。"""
        self._refresh_locked()
        if self._error:
            raise PromptDataError(
                "提示词库文件损坏或无法读取，已拒绝覆盖；请先修复或删除 "
                f"{self.path}（{self._error}）"
            )

    def _save_locked(self, entries: dict[str, PromptEntry]) -> str:
        path = self.path
        directory = os.path.dirname(path)
        payload = {
            "version": PROMPTS_VERSION,
            "prompts": {
                key: prompt_to_dict(entry) for key, entry in sorted(entries.items())
            },
        }
        temporary = path + ".tmp"
        try:
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise PromptDataError(f"写入提示词库失败：{exc}") from exc
        self._loaded = True
        self._entries = dict(entries)
        self._signature = _file_signature(path)
        self._error = ""
        return path

    def _warn(self, message: str, error: str) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.warn(message, event="prompt_library_unreadable", error=error)
        except Exception:  # noqa: BLE001 - 日志失败不能影响对话
            pass


# ---------------------------------------------------------------------------
# 便捷函数（GUI / 测试用；每次新建临时 store，语义与直接读写文件一致）
# ---------------------------------------------------------------------------
def load_prompt_overrides(config_dir: str) -> dict[str, PromptEntry]:
    """读全部覆盖条目（文件不存在/损坏时返回空表）。"""
    return PromptStore(config_dir).load_overrides()


def save_prompt_overrides(config_dir: str, entries: Mapping[str, PromptEntry]) -> str:
    """整表写回覆盖条目；返回文件路径。"""
    return PromptStore(config_dir).save_overrides(entries)
