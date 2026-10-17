"""``tool_tags.toml``：标签 → 工具集合 + 提示词，以及按标签取工具名。

对应 Go：``internal/agent/tool_tag_config.go``。

标签配置是**可热加载**的（按 ``(size, mtime)`` 缓存）：改完 ``tool_tags.toml`` 不必重启，
下一次注入就会用新内容。``tags.<name>.prompt`` 只在会话的 metadata 里记录过该 tag 时
才注入 system prompt（``@tool:<tag>`` / ``@skill:<tag>`` 触发），避免把全部标签说明都塞
进每一轮请求。

标签名归一化只允许 ``a-z 0-9 _ - .``：名字会出现在 ``@tool:<tag>`` 指令里，也会作为
``tool_tags.toml`` 的 TOML 键。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from atri_x.agent.context_compact import decode_metadata
from atri_x.agent.model import host_of
from atri_x.agent.system_prompt import SystemPromptPart, SystemPromptRequest
from atri_x.config import ToolTagConfig, ToolTagsConfig, load_tool_tags

__all__ = [
    "ToolTagConfigSource",
    "ToolTagFileState",
    "completion_tool_names_by_tag",
    "completion_tool_tags",
    "current_tool_tag_file_state",
    "names_by_tool_tag",
    "names_by_tool_tag_sync",
    "new_tool_tag_config_source",
    "normalize_tool_tag",
    "normalize_tool_tags_config",
    "same_tool_tag_file_state",
    "sorted_unique",
]


def _shared(module: str, name: str) -> Any:
    import importlib

    try:
        mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


def sorted_unique(values: list[str] | None) -> list[str]:
    """去空去重并排序。对应 Go ``sortedUnique``（定义在 ``session_metadata.go``）。"""
    shared = _shared("atri_x.agent.session_metadata", "sorted_unique")
    if shared is not None and shared is not sorted_unique:
        return shared(values or [])
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    out.sort()
    return out


def normalize_tool_tag(tag: str) -> str:
    """归一化标签名（只允许 ``a-z 0-9 _ - .``）。对应 Go ``normalizeToolTag``。"""
    text = (tag or "").strip().lower()
    for char in text:
        if char.isascii() and (char.islower() or char.isdigit() or char in "_-."):
            continue
        return ""
    return text


def normalize_tool_tags_config(cfg: ToolTagsConfig | None) -> ToolTagsConfig:
    """归一化整份标签配置。对应 Go ``normalizeToolTagsConfig``。"""
    out = ToolTagsConfig(tags={})
    for tag, entry in ((cfg.tags if cfg is not None else {}) or {}).items():
        key = normalize_tool_tag(tag)
        if not key:
            continue
        tools = sorted_unique(list(getattr(entry, "tools", None) or []))
        prompt = (getattr(entry, "prompt", "") or "").strip()
        if not tools and not prompt:
            continue
        out.tags[key] = ToolTagConfig(tools=tools, prompt=prompt)
    return out


@dataclass
class ToolTagFileState:
    """``tool_tags.toml`` 的 ``(size, mtime)`` 指纹。对应 Go ``toolTagFileState``。"""

    size: int = 0
    mod_time: datetime | None = None


@dataclass
class _Cache:
    loaded: bool = False
    config: ToolTagsConfig = field(default_factory=ToolTagsConfig)
    state: ToolTagFileState = field(default_factory=ToolTagFileState)


def current_tool_tag_file_state(path: str) -> ToolTagFileState:
    """对应 Go ``currentToolTagFileState``（文件不存在时返回零值）。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return ToolTagFileState()
    except OSError as exc:
        raise OSError(f'stat tool tags config "{path}": {exc}') from exc
    seconds, remainder = divmod(info.st_mtime_ns, 1_000_000_000)
    return ToolTagFileState(
        size=info.st_size,
        mod_time=datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder // 1000
        ),
    )


def same_tool_tag_file_state(left: ToolTagFileState, right: ToolTagFileState) -> bool:
    """对应 Go ``sameToolTagFileState``。"""
    return left.size == right.size and left.mod_time == right.mod_time


class ToolTagConfigSource:
    """``tool_tags.toml`` 的来源（可热加载）+ system prompt 的一段来源。

    对应 Go ``toolTagConfigSource``。
    """

    def __init__(self, path: str = "", initial: ToolTagsConfig | None = None) -> None:
        self.path = (path or "").strip()
        self.mu = threading.RLock()
        # 路径为空时配置固定为初始值（对应 Go 构造里的 ``loaded: path == ""``）。
        self.cache = _Cache(
            loaded=self.path == "",
            config=normalize_tool_tags_config(initial),
        )

    # -- SystemPromptSource -------------------------------------------------
    async def parts(
        self, ctx: Any, req: SystemPromptRequest
    ) -> list[SystemPromptPart]:
        """注入已被会话激活的标签提示词。对应 Go ``Parts``。"""
        session = req.session
        if session is None:
            return []
        metadata = decode_metadata(getattr(session, "metadata", "") or "")
        tags = list(getattr(metadata, "tool_tags", None) or []) if metadata else []
        if not tags:
            return []
        cfg = await self.load(ctx)
        parts: list[SystemPromptPart] = []
        for tag in tags:
            entry = cfg.tags.get(normalize_tool_tag(tag))
            if entry is None or not (entry.prompt or "").strip():
                continue
            parts.append(
                SystemPromptPart(name="tool_tag_prompt:" + str(tag), content=entry.prompt)
            )
        return parts

    # -- 查询 ---------------------------------------------------------------
    async def configured_tags(self, ctx: Any) -> list[str]:
        """配置里的全部标签（排序）。对应 Go ``configuredTags``。"""
        cfg = await self.load(ctx)
        return sorted(cfg.tags.keys())

    async def configured_tags_for_tool(self, ctx: Any, name: str) -> list[str]:
        """包含该工具的标签（排序）。对应 Go ``configuredTagsForTool``。"""
        name = (name or "").strip()
        if not name:
            return []
        cfg = await self.load(ctx)
        out: list[str] = []
        for tag, entry in cfg.tags.items():
            if name in list(entry.tools or []):
                out.append(tag)
        return sorted_unique(out)

    async def configured_tool_names_by_tag(self, ctx: Any, tag: str) -> list[str]:
        """某个标签声明的工具名。对应 Go ``configuredToolNamesByTag``。"""
        key = normalize_tool_tag(tag)
        if not key:
            return []
        cfg = await self.load(ctx)
        entry = cfg.tags.get(key)
        return list(entry.tools or []) if entry is not None else []

    # -- 加载 ---------------------------------------------------------------
    async def load(self, ctx: Any) -> ToolTagsConfig:
        """读配置（带 ``(size, mtime)`` 缓存）。对应 Go ``load``。"""
        return self.load_sync()

    def load_sync(self) -> ToolTagsConfig:
        """:meth:`load` 的同步版（文件读取本来就是同步的）。"""
        if not self.path:
            with self.mu:
                return self.cache.config
        with self.mu:
            state = current_tool_tag_file_state(self.path)
            if self.cache.loaded and same_tool_tag_file_state(self.cache.state, state):
                return self.cache.config
            cfg = load_tool_tags(self.path, strict=True)
            if cfg is None:
                # 文件不存在：按空配置处理（对应 Go 的 os.IsNotExist 分支）。
                empty = ToolTagsConfig(tags={})
                self.cache = _Cache(loaded=True, config=empty, state=state)
                return empty
            normalized = normalize_tool_tags_config(cfg)
            self.cache = _Cache(loaded=True, config=normalized, state=state)
            return normalized


def new_tool_tag_config_source(
    path: str = "", initial: ToolTagsConfig | None = None
) -> ToolTagConfigSource:
    """构造来源。对应 Go ``newToolTagConfigSource``。"""
    return ToolTagConfigSource(path, initial)


# ---------------------------------------------------------------------------
# 按标签取工具名（挂在 Agent 上）
# ---------------------------------------------------------------------------
def _source(agent: Any) -> ToolTagConfigSource | None:
    runtime = getattr(agent, "tool_runtime", None)
    source = getattr(runtime, "tool_tags", None) if runtime is not None else None
    return source if isinstance(source, ToolTagConfigSource) else None


def _registry(agent: Any) -> Any:
    runtime = getattr(agent, "tool_runtime", None)
    return getattr(runtime, "registry", None) if runtime is not None else None


async def names_by_tool_tag(
    agent: Any, ctx: Any, tag: str, allowed: Callable[[Any], bool] | None = None
) -> list[str]:
    """注册表 + 配置里的同名标签工具。对应 Go ``namesByToolTag``。"""
    host = host_of(agent)
    registry = _registry(host)
    if registry is None:
        return []
    names = list(registry.names_by_tag(tag, allowed))
    source = _source(host)
    if source is not None:
        for name in await source.configured_tool_names_by_tag(ctx, tag):
            candidate = registry.get(name)
            if candidate is None:
                continue
            if allowed is not None and not allowed(candidate):
                continue
            names.append(name)
    return sorted_unique(names)


def names_by_tool_tag_sync(
    agent: Any, ctx: Any, tag: str, allowed: Callable[[Any], bool] | None = None
) -> list[str]:
    """:func:`names_by_tool_tag` 的同步版（配置文件读取本来就是同步的）。"""
    host = host_of(agent)
    registry = _registry(host)
    if registry is None:
        return []
    names = list(registry.names_by_tag(tag, allowed))
    source = _source(host)
    if source is not None:
        key = normalize_tool_tag(tag)
        entry = source.load_sync().tags.get(key) if key else None
        for name in list(entry.tools or []) if entry is not None else []:
            candidate = registry.get(name)
            if candidate is None:
                continue
            if allowed is not None and not allowed(candidate):
                continue
            names.append(name)
    return sorted_unique(names)


async def completion_tool_tags(
    agent: Any, ctx: Any, registry: Any, actor: Any, policy: Any
) -> list[str]:
    """补全用的标签清单（只保留真的有可用工具的标签）。对应 Go ``completionToolTags``。"""
    host = host_of(agent)
    if registry is None:
        return []
    seen: set[str] = set()

    def usable(candidate: Any) -> bool:
        checker = getattr(host, "can_preload_tool_root", None)
        if callable(checker):
            return bool(checker(actor, policy, candidate))
        return True

    tags = list(registry.tags() or [])
    source = _source(host)
    if source is not None:
        tags.extend(await source.configured_tags(ctx))
    for tag in tags:
        names = await completion_tool_names_by_tag(host, ctx, registry, tag, usable)
        if names:
            seen.add(tag)
    return sorted(seen)


async def completion_tool_names_by_tag(
    agent: Any, ctx: Any, registry: Any, tag: str, allowed: Callable[[Any], bool] | None
) -> list[str]:
    """某个标签在补全里可见的工具名。对应 Go ``completionToolNamesByTag``。"""
    if registry is None:
        return []
    host = host_of(agent)
    names = list(registry.names_by_tag(tag, allowed))
    source = _source(host)
    if source is not None:
        for name in await source.configured_tool_names_by_tag(ctx, tag):
            candidate = registry.get(name)
            if candidate is None:
                continue
            if allowed is not None and not allowed(candidate):
                continue
            names.append(name)
    return sorted_unique(names)


class ToolTagConfigMixin:
    """``internal/agent/tool_tag_config.go`` 里挂在 ``*Agent`` 上的方法。"""

    async def names_by_tool_tag(
        self, ctx: Any, tag: str, allowed: Callable[[Any], bool] | None = None
    ) -> list[str]:
        """对应 Go ``namesByToolTag``。"""
        return await names_by_tool_tag(self, ctx, tag, allowed)

    async def completion_tool_tags(
        self, ctx: Any, registry: Any, actor: Any, policy: Any
    ) -> list[str]:
        """对应 Go ``completionToolTags``。"""
        return await completion_tool_tags(self, ctx, registry, actor, policy)

    async def completion_tool_names_by_tag(
        self, ctx: Any, registry: Any, tag: str, allowed: Callable[[Any], bool] | None
    ) -> list[str]:
        """对应 Go ``completionToolNamesByTag``。"""
        return await completion_tool_names_by_tag(self, ctx, registry, tag, allowed)


def tool_tag_source_of(config: Mapping[str, Any] | None) -> ToolTagsConfig:
    """从 ``Options.tool_tags`` 造一份初始配置（dict / ToolTagsConfig 都接受）。"""
    if isinstance(config, ToolTagsConfig):
        return config
    if not config:
        return ToolTagsConfig(tags={})
    tags: dict[str, ToolTagConfig] = {}
    for name, entry in config.items():
        if isinstance(entry, ToolTagConfig):
            tags[str(name)] = entry
            continue
        if isinstance(entry, Mapping):
            tags[str(name)] = ToolTagConfig(
                tools=list(entry.get("tools") or []),
                prompt=str(entry.get("prompt") or ""),
            )
    return ToolTagsConfig(tags=tags)
