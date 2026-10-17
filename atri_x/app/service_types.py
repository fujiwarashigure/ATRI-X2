"""app 内部使用的服务侧数据类型。

对应 Go：``internal/app`` 里跨文件用到的两类值对象 ——

* ``agent.AudioTranscriptionRequest`` / ``agent.AudioTranscriptionResult``
  （``asr.go`` 用到，定义在 ``internal/agent``）；
* ``builtin.ImagePromptRewriteRequest``（``image_rewriter.go`` 用到，定义在
  ``internal/tool/builtin``）。

Python 侧的 ``atri_x.agent`` 与 ``atri_x.tool.builtin`` 尚未导出同名的类型，
而 app 层又不允许反过来要求它们改名，因此这里给出**字段完全一致**的本地
dataclass，并在两处都用鸭子类型读取：Agent 传进来的对象只要有同名属性即可，
app 返回的对象 Agent 也能按属性读。

这是**有意的过渡形状**，不是复制契约类型（契约类型见 PORTABLE.md §3，
本文件不属于其中任何一个）。Lead 在做最终对接时，若 ``atri_x.agent`` /
``atri_x.tool.builtin`` 补上了同名类型，把这里的 import 换过去即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AudioTranscriptionRequest",
    "AudioTranscriptionResult",
    "ImagePromptRewriteRequest",
]


@dataclass
class AudioTranscriptionRequest:
    """对应 Go ``agent.AudioTranscriptionRequest``。"""

    media_id: str = ""
    data: bytes = b""
    name: str = ""
    mime_type: str = ""
    language: str = ""


@dataclass
class AudioTranscriptionResult:
    """对应 Go ``agent.AudioTranscriptionResult``。"""

    text: str = ""
    language: str = ""
    provider: str = ""
    model: str = ""


@dataclass
class ImagePromptRewriteRequest:
    """对应 Go ``builtin.ImagePromptRewriteRequest``。

    字段名与 Go 一一对应；``protected_blocks`` 是"已由程序单独处理、模型不要输出
    也不要改写"的片段。
    """

    scene: str = ""
    character_name: str = ""
    character_prompt: str = ""
    context_text: str = ""
    library_hints: str = ""
    target_model: str = ""
    target_dialect: str = ""
    model_family: str = ""
    max_runes: int = 0
    protected_blocks: list[str] = field(default_factory=list)

    @classmethod
    def from_any(cls, value: Any) -> "ImagePromptRewriteRequest":
        """从任意同形对象构造（Agent / 工具侧传进来的对象）。"""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        return cls(
            scene=_text(value, "scene"),
            character_name=_text(value, "character_name"),
            character_prompt=_text(value, "character_prompt"),
            context_text=_text(value, "context_text"),
            library_hints=_text(value, "library_hints"),
            target_model=_text(value, "target_model"),
            target_dialect=_text(value, "target_dialect"),
            model_family=_text(value, "model_family"),
            max_runes=int(getattr(value, "max_runes", 0) or 0),
            protected_blocks=list(getattr(value, "protected_blocks", None) or []),
        )


def _text(obj: Any, name: str) -> str:
    return str(getattr(obj, name, "") or "")
