"""按 scope（群 / 用户）覆盖策略：``<config_dir>/safety/scopes/*.toml``。

一个文件描述"某个平台 + 某个 scope 的策略差异"，例如::

    # safety/scopes/qq__group__9299xxxx42.toml
    platform = "qq"
    scope_id = "9299xxxx42"
    fail_mode = "closed"          # 高敏群：审核失败就拦
    images = "sync"
    tools = ["image_generate"]    # 该群额外强制远程审的工具
    allow_tools = ["web_search"]  # 该群豁免远程审的工具

    [tiers]
    remote_review = "hold"        # review 档不直接发，进队列

匹配规则：``platform`` + ``scope_id`` 精确匹配优先；``scope_id = "*"`` 表示该平台兜底。
文件缺失/解析失败**整份跳过**并告警——一个坏文件不能影响其它群。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.config import StrictConfig, load_toml

__all__ = ["ScopePolicies", "ScopeSettings", "scope_file_key"]


@dataclass
class ScopeSettings(StrictConfig):
    """单个 scope 的覆盖项；``None`` 表示"继承全局"。"""

    platform: str = ""
    scope_id: str = ""
    #: 该 scope 是否启用内容安全（默认继承）。
    enabled: bool | None = None
    #: 本地规则层模式（off / audit / enforce）。
    mode: str | None = None
    #: 远程失败策略（open / closed）。
    fail_mode: str | None = None
    #: 图片审核模式（sync / async / off）。
    images: str | None = None
    #: 强制走远程审核的工具名。
    tools: list[str] = field(default_factory=list)
    #: 豁免远程审核的工具名（优先于全局默认）。
    allow_tools: list[str] = field(default_factory=list)
    #: 档位覆盖（``remote_block`` / ``remote_review`` / ``inbound_review`` / ``outbound_review``）。
    tiers: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        platform = (self.platform or "").strip().lower()
        scope = (self.scope_id or "").strip()
        return f"{platform}:{scope}" if platform else scope

    def tool_policy(self, tool: str) -> str:
        """返回该 scope 对某工具的强制远程策略（空串 = 没有特别规定）。"""
        name = (tool or "").strip().lower()
        if not name:
            return ""
        if name in {(item or "").strip().lower() for item in self.allow_tools}:
            return "local_only"
        if name in {(item or "").strip().lower() for item in self.tools}:
            return "sync"
        return ""


def scope_file_key(platform: str, scope_id: str) -> str:
    """生成匹配用的键（与 :attr:`ScopeSettings.key` 同构）。"""
    p = (platform or "").strip().lower()
    s = (scope_id or "").strip()
    return f"{p}:{s}" if p else s


class ScopePolicies:
    """一组 scope 覆盖（构造一次，按 scope 查询）。"""

    def __init__(self, entries: list[ScopeSettings] | None = None) -> None:
        self.entries: dict[str, ScopeSettings] = {}
        self.wildcards: dict[str, ScopeSettings] = {}
        self.errors: list[str] = []
        for item in entries or ():
            self._register(item)

    def _register(self, item: ScopeSettings) -> None:
        platform = (item.platform or "").strip().lower()
        scope = (item.scope_id or "").strip()
        if not platform and not scope:
            self.errors.append("scope 文件缺少 platform/scope_id，已跳过")
            return
        if scope == "*":
            self.wildcards[platform] = item
            return
        self.entries[scope_file_key(platform, scope)] = item

    @classmethod
    def load(cls, directory: str, *, logger: Any = None) -> "ScopePolicies":
        """读目录下的 ``*.toml``；坏文件跳过并记错误。"""
        base = (directory or "").strip()
        out = cls()
        if not base or not os.path.isdir(base):
            return out
        try:
            names = sorted(os.listdir(base))
        except OSError as exc:
            out.errors.append(f"读取 scope 目录失败：{exc}")
            return out
        for filename in names:
            if not filename.lower().endswith(".toml"):
                continue
            path = os.path.join(base, filename)
            try:
                item = load_toml(path, ScopeSettings, strict=False)
            except Exception as exc:  # noqa: BLE001 - 坏文件只跳过自己
                out.errors.append(f"{filename}: {exc}")
                if logger is not None:
                    try:
                        logger.warn("scope 策略文件解析失败，已跳过", file=filename, error=str(exc))
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if isinstance(item, ScopeSettings):
                out._register(item)
        return out

    def match(self, platform: str, scope_id: str) -> ScopeSettings | None:
        """精确匹配 > 平台通配；都没有返回 ``None``。"""
        key = scope_file_key(platform, scope_id)
        found = self.entries.get(key)
        if found is not None:
            return found
        wild = self.wildcards.get((platform or "").strip().lower())
        return wild

    def signature(self, directory: str) -> tuple[tuple[str, int, int], ...]:
        """目录内所有文件的 ``(名字, mtime_ns, size)``（热加载指纹用）。"""
        base = (directory or "").strip()
        if not base or not os.path.isdir(base):
            return ()
        out: list[tuple[str, int, int]] = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return ()
        for filename in names:
            if not filename.lower().endswith(".toml"):
                continue
            path = os.path.join(base, filename)
            try:
                info = os.stat(path)
            except OSError:
                continue
            out.append((filename, int(info.st_mtime_ns), int(info.st_size)))
        return tuple(out)

    def __len__(self) -> int:
        return len(self.entries) + len(self.wildcards)
