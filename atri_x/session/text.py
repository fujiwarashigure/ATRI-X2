"""标题/预览文本 helper。对应 Go：``internal/session/service.go`` 末尾的 ``forkTitle`` / ``defaultTitle`` /
``preview``。**拆分说明**：这三个 helper 在 Go 里与 ``Service`` 同文件，但 Python 的
``naming.py`` / ``query.py`` 都要用它们，而 ``service.py`` 又要 import 这两个模块的
mixin —— 留在 ``service.py`` 会形成模块循环依赖，因此单独成文件并在
``service.py`` 里再导出，调用点仍可写 ``atri_x.session.service.default_title``。
"""

from __future__ import annotations

#: 标题最大长度（Go ``defaultTitle`` 里的 ``maxRunes``）。
MAX_TITLE_RUNES = 40

#: 预览最大长度（Go ``preview`` 里的 ``maxRunes``）。
MAX_PREVIEW_RUNES = 80

#: 占位标题。Go 里是散落的字面量 ``"New session"``。
PLACEHOLDER_TITLE = "New session"

__all__ = [
    "MAX_PREVIEW_RUNES",
    "MAX_TITLE_RUNES",
    "PLACEHOLDER_TITLE",
    "default_title",
    "fork_title",
    "preview",
]


def default_title(text: str) -> str:
    """由首条消息生成默认标题（超过 40 个字符截断加省略号）。对应 Go ``defaultTitle``。"""
    text = (text or "").strip()
    if not text:
        return PLACEHOLDER_TITLE
    if len(text) <= MAX_TITLE_RUNES:
        return text
    return text[:MAX_TITLE_RUNES] + "..."


def fork_title(title: str) -> str:
    """Fork 出来的会话标题。对应 Go ``forkTitle``。"""
    title = (title or "").strip()
    if not title:
        title = PLACEHOLDER_TITLE
    return default_title("Fork: " + title)


def preview(text: str) -> str:
    """单行预览（换行折叠成空格，超过 80 个字符截断加省略号）。对应 Go ``preview``。"""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= MAX_PREVIEW_RUNES:
        return text
    return text[:MAX_PREVIEW_RUNES] + "..."
