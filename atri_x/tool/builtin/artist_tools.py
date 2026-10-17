"""画师串工具的共享依赖、身份视图与错误文案。

对应 Go：``internal/tool/builtin/artist_tools.go``。

``ArtistTools`` 用鸭子类型承载 ``atri_x.artist`` 的服务层（``service`` / ``media`` /
``vision`` 三个字段与 Go 一致），这样没有串库的部署（``service=None``）也能加载本包。
错误归类在函数内延迟 import 哨兵，取不到时退化为按异常类型名判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atri_x.platform import message_context_from
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.tool.builder import Builder
from atri_x.tool.tool import Result

# Built-in artist tools.
ARTIST_EXTRACT_NAME = "artist_extract"
ARTIST_LIBRARY_SEARCH_NAME = "artist_library_search"
ARTIST_LIBRARY_READ_NAME = "artist_library_read"
ARTIST_LIBRARY_SAVE_NAME = "artist_library_save"
ARTIST_LIBRARY_DELETE_NAME = "artist_library_delete"

# artistToolTags 把整族归到一个 tag，便于按组一次开关。
ARTIST_TOOL_TAGS = ("image", "artist")

LIBRARY_DISABLED_MESSAGE = (
    "画师串库未启用。请在 services.toml 里配置 "
    "[image_generation.artist_library] 的 root 后重启。"
)


@dataclass
class ArtistTools:
    """画师工具与 image_generate 的画师分支共享的依赖。对应 Go ``ArtistTools``。"""

    service: Any = None
    media: Any = None
    vision: Any = None

    def enabled(self) -> bool:
        """对应 Go ``ArtistTools.Enabled``：可写画师串库是否配置。"""
        service = self.service
        if service is None:
            return False
        checker = getattr(service, "enabled", None)
        if callable(checker):
            return bool(checker())
        return True


def artist_viewer(ctx: Any) -> Any:
    """构造本次调用的身份与 scope。对应 Go ``artistViewer``。

    身份只来自认证过的 ctx，scope 只来自平台消息上下文；二者都**不**从工具参数
    读取，因此模型无法通过编造 id 去访问别的群或别的用户。
    """
    actor, ok = actor_from_context(ctx)
    if not ok:
        actor = None
    platform = getattr(actor, "platform", "") or ""
    scope_id = ""
    msg, ok = message_context_from(ctx)
    if ok:
        if not platform.strip():
            platform = msg.platform
        scope_id = msg.scope_id
    return _artist_viewer_value(
        actor_id=getattr(actor, "id", ""),
        platform=platform,
        superadmin=getattr(actor, "role", "") == RoleSuperadmin,
        scope_id=scope_id,
    )


def _artist_viewer_value(
    actor_id: str, platform: str, superadmin: bool, scope_id: str
) -> Any:
    """延迟 import ``artist.Viewer``；未落地时退化为同名本地数据类。"""
    try:  # pragma: no cover - 依赖并行落地进度
        from atri_x.artist.service import Viewer  # type: ignore

        return Viewer(
            actor_id=actor_id,
            platform=platform,
            superadmin=superadmin,
            scope_id=scope_id,
        )
    except Exception:
        return ArtistViewer(
            actor_id=actor_id,
            platform=platform,
            superadmin=superadmin,
            scope_id=scope_id,
        )


@dataclass
class ArtistViewer:
    """``artist.Viewer`` 未落地时的等价视图（字段名与 Go 一致）。"""

    actor_id: str = ""
    platform: str = ""
    superadmin: bool = False
    scope_id: str = ""


def artist_error_message(err: BaseException | None) -> str:
    """把画师串库错误翻成用户可执行的一句话。对应 Go ``artistErrorMessage``。"""
    if err is None:
        return ""
    kind = _artist_error_kind(err)
    if kind == "disabled":
        return LIBRARY_DISABLED_MESSAGE
    if kind == "not_found":
        return "没有找到这个画师串组合，可先用 artist_library_search 查询当前可见的组合。"
    if kind == "conflict":
        conflict_id = getattr(err, "id", "")
        expected = getattr(err, "expected_revision", None)
        current = getattr(err, "current_revision", None)
        if expected is not None and current is not None:
            return (
                f"画师串组合 {conflict_id} 已被修改：你提交的 revision 是 {expected}，"
                f"当前是 {current}。请先 artist_library_read 读取最新内容，"
                "再用当前 revision 重新保存。"
            )
        return "画师串组合已被他人修改，请重新读取后再保存。"
    if kind == "forbidden":
        return "没有权限对该画师串组合做这个操作：" + str(err)
    if kind == "limit_exceeded":
        return "画师串库容量已满：" + str(err)
    if kind == "invalid":
        return "参数不合法：" + str(err)
    return "画师串库操作失败：" + str(err)


def _artist_error_kind(err: BaseException) -> str:
    """把异常归到 Go 的哨兵语义上；延迟 import，取不到就按类型名判定。"""
    sentinels: dict[str, Any] = {}
    try:  # pragma: no cover - 依赖并行落地进度
        from atri_x import artist as artist_pkg  # type: ignore

        for name, kind in (
            ("ErrDisabled", "disabled"),
            ("ErrNotFound", "not_found"),
            ("ErrConflict", "conflict"),
            ("ErrForbidden", "forbidden"),
            ("ErrLimitExceeded", "limit_exceeded"),
            ("ErrInvalid", "invalid"),
        ):
            sentinel = getattr(artist_pkg, name, None)
            if sentinel is not None:
                sentinels[name] = sentinel
    except Exception:  # pragma: no cover - 依赖并行落地进度
        sentinels = {}

    for name, kind in (
        ("ErrDisabled", "disabled"),
        ("ErrNotFound", "not_found"),
        ("ErrConflict", "conflict"),
        ("ErrForbidden", "forbidden"),
        ("ErrLimitExceeded", "limit_exceeded"),
        ("ErrInvalid", "invalid"),
    ):
        sentinel = sentinels.get(name)
        if sentinel is None:
            continue
        try:
            if isinstance(err, type) or err is sentinel or err == sentinel:
                return kind
        except Exception:
            continue
        if isinstance(sentinel, type) and isinstance(err, sentinel):
            return kind
        if isinstance(sentinel, BaseException) and err is sentinel:
            return kind

    # 退化路径：按类名匹配（``ArtistDisabledError`` / ``ErrDisabled`` …）。
    name = type(err).__name__.lower()
    for token, kind in (
        ("disabled", "disabled"),
        ("notfound", "not_found"),
        ("conflict", "conflict"),
        ("forbidden", "forbidden"),
        ("limitexceeded", "limit_exceeded"),
        ("invalid", "invalid"),
    ):
        if token in name:
            return kind
    return ""


def artist_builder(name: str, description: str) -> Builder:
    """对应 Go ``artistBuilder``。"""
    return Builder(name).description(description).tags(*ARTIST_TOOL_TAGS)


def artist_library_disabled_result() -> Result:
    """对应 Go ``artistLibraryDisabledResult``。"""
    return Result(content=LIBRARY_DISABLED_MESSAGE)


__all__ = [name for name in dir() if not name.startswith("_")]
