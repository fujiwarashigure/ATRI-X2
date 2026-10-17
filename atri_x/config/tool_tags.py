"""``tool_tags.toml`` 解码。

对应 Go：``internal/config/config.go`` 的 ``ToolTagsConfig`` / ``ToolTagConfig``，
以及 ``Load`` 里读取 ``[config_files].tool_tags`` 的那一步。

``tool_tags.toml`` 把「一组工具 + 一段提示词」打包成 tag，只有被 ``@tool:<tag>``
激活后才追加到 system prompt（见上游 ``docs/configuration.md``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from atri_x.config.app_sections import StrictConfig, load_toml

__all__ = ["ToolTagConfig", "ToolTagsConfig", "load_tool_tags"]


@dataclass
class ToolTagConfig(StrictConfig):
    """``[tags.<name>]``：激活后注入的工具与提示词。"""

    tools: list[str] = field(default_factory=list)
    prompt: str = ""


@dataclass
class ToolTagsConfig(StrictConfig):
    """整个 ``tool_tags.toml``。"""

    tags: dict[str, ToolTagConfig] = field(default_factory=dict)

    def tag(self, name: str) -> ToolTagConfig | None:
        """按名字取 tag（大小写敏感，与 Go 的 map 索引一致）。"""
        return self.tags.get(name)

    def names(self) -> list[str]:
        return sorted(self.tags)


def load_tool_tags(path: str | os.PathLike[str], *, strict: bool = True) -> ToolTagsConfig | None:
    """读取 ``tool_tags.toml``；文件不存在时返回 ``None``（对应 Go 的 ``os.ErrNotExist`` 容忍）。"""
    try:
        config = load_toml(path, ToolTagsConfig, strict=strict)
    except FileNotFoundError:
        return None
    assert isinstance(config, ToolTagsConfig)
    return config
