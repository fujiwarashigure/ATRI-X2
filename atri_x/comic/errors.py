"""漫画业务域的异常与"失败可能已在服务端成功"的分类。

单独成文件，是为了让 ``models``（值对象与校验）与 ``service``（编排）都能引用
同一批异常，而不用互相 import。
"""

from __future__ import annotations

from atri_x.errors import AtriXError

__all__ = [
    "MAYBE_UNKNOWN_ERRORS",
    "ComicCharacterError",
    "ComicContextError",
    "ComicDataError",
    "ComicError",
    "ComicPlanError",
    "ComicPromptError",
    "ComicRenderError",
    "ComicStoreError",
    "is_maybe_unknown_error",
]

#: 这些异常发生时，生图请求可能已经在服务端落地：只能标"结果未知"，不得自动重试。
MAYBE_UNKNOWN_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    OSError,
    ConnectionError,
)


class ComicError(AtriXError):
    """漫画业务域的非哨兵错误基类（消息直接面向用户/模型）。"""


class ComicDataError(ComicError):
    """计划/记录结构不合法。"""


class ComicContextError(ComicError):
    """聊天素材不足或锚点无效。"""


class ComicPlanError(ComicError):
    """分镜规划失败或规划结果未通过校验。"""


class ComicCharacterError(ComicError):
    """人物解析/参考图绑定失败。"""


class ComicPromptError(ComicError):
    """漫画提示词编译失败（超长、缺参考图等）。"""


class ComicRenderError(ComicError):
    """生图/入库阶段失败。"""


class ComicStoreError(ComicError):
    """计划/记录的持久化失败。"""


def is_maybe_unknown_error(exc: BaseException) -> bool:
    """这次失败是否**可能已经在服务端生成成功**（只能记未知、不能自动重试）。"""
    return isinstance(exc, MAYBE_UNKNOWN_ERRORS)
