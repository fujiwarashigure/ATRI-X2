"""聊天漫画测试用的假依赖（历史 / 生图 client / 媒体中心 / 角色库 / 头像）。

放在测试包里的原因与 ``_vision_fakes.py`` 相同：多个测试模块共用，且**不触网、
不建真实数据库**。这些假实现尽量贴着真实接口的行为走：

* ``FakeHistory.search`` 复刻"按 seq 升序返回最近 N 条"（真实仓储是 DESC 再反转）；
* ``FakeHistory.around`` 复刻"锚点不存在就抛 ``NotFoundError``"；
* ``FakeMedia.read`` 返回真正存进去的字节，参考图路径才能被验证；
* ``FakeCharacterStore.read_image`` 返回固定假图片字节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from atri_x.character import Character, Image, ImageSettings
from atri_x.comic import (
    ComicCharacter,
    ComicCharacterReference,
    ComicDialogue,
    ComicPanel,
    ComicPlan,
    ComicSource,
)
from atri_x.errors import NotFoundError
from atri_x.imagegen import Result as ImageResult
from atri_x.platform import MessageContext, with_message_context
from atri_x.storage import ChatMessage

__all__ = [
    "FakeAvatarResolver",
    "FakeCharacterStore",
    "FakeHistory",
    "FakeImageClient",
    "FakeMediaManager",
    "build_context",
    "make_character",
    "make_character_model",
    "make_plan",
    "make_rows",
]


def build_context(
    platform: str = "qqonebot",
    scope_id: str = "group-1",
    platform_message_id: str = "msg-999",
):
    """构造带平台路由的 ctx（漫画取材必须有它）。"""
    return with_message_context(
        None,
        MessageContext(
            platform=platform,
            scope_id=scope_id,
            platform_message_id=platform_message_id,
            conversation_kind="group",
        ),
    )


def make_rows(
    texts,
    *,
    platform: str = "qqonebot",
    scope_id: str = "group-1",
    senders=None,
    start_seq: int = 1,
) -> list[ChatMessage]:
    """按顺序造聊天历史行。``senders`` 缺省是 ``(用户1, user1)`` 之类的交替发言人。"""
    base = datetime(2024, 5, 1, 9, 0, tzinfo=timezone.utc)
    rows: list[ChatMessage] = []
    for index, text in enumerate(texts):
        sender_name, sender_id = ("小明", "1001") if index % 2 == 0 else ("小红", "1002")
        if senders is not None:
            sender_name, sender_id = senders[index]
        rows.append(
            ChatMessage(
                seq=start_seq + index,
                id=f"row-{start_seq + index}",
                platform=platform,
                platform_scope_id=scope_id,
                scope_type="group",
                platform_message_id=f"msg-{start_seq + index}",
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                created_at=base + timedelta(minutes=index),
            )
        )
    return rows


class FakeHistory:
    """聊天历史仓储的假实现（只实现漫画用到的三个读操作）。"""

    def __init__(self, rows: list[ChatMessage] | None = None) -> None:
        self.rows = list(rows or [])
        self.requests: list[object] = []

    def _scoped(self, platform: str, scope_id: str) -> list[ChatMessage]:
        return [
            row
            for row in self.rows
            if row.platform == platform and row.platform_scope_id == scope_id
        ]

    def search(self, req) -> list[ChatMessage]:
        self.requests.append(req)
        rows = self._scoped(req.platform, req.platform_scope_id)
        terms = [term for term in getattr(req, "query_terms", []) or [] if term]
        if terms:
            lowered = [(term or "").lower() for term in terms]
            rows = [
                row
                for row in rows
                if any(term in (row.text or "").lower() for term in lowered)
            ]
        limit = int(getattr(req, "limit", 0) or 0) or 10
        rows.sort(key=lambda row: row.seq)
        return rows[-limit:]

    def around(self, req) -> list[ChatMessage]:
        self.requests.append(req)
        rows = self._scoped(req.platform, req.platform_scope_id)
        rows.sort(key=lambda row: row.seq)
        target = None
        for index, row in enumerate(rows):
            if row.platform_message_id == req.platform_message_id:
                target = index
                break
        if target is None:
            raise NotFoundError("anchor not found")
        start = max(0, target - max(0, int(req.before or 0)))
        end = target + 1 + max(0, int(req.after or 0))
        return rows[start:end]


class FakeImageClient:
    """永远成功的生图 client；``error`` 非空时抛它。"""

    def __init__(self, model: str = "fake-image", error: BaseException | None = None) -> None:
        self.model = model
        self.error = error
        self.requests: list[object] = []

    def enabled(self) -> bool:
        return True

    async def generate(self, ctx, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ImageResult(
            data=b"comic-png-bytes",
            mime_type="image/png",
            model=getattr(request, "model", "") or self.model,
            revised_prompt="six panels comic",
        )


@dataclass
class FakeMedia:
    id: str = ""
    name: str = ""
    mime_type: str = "image/png"


@dataclass
class FakeMediaManager:
    """最小可用的媒体中心：入库 / 读取 / 头像 URL 导入 / 引用登记。

    ``imported`` 只记 ``import_reader``（生成结果入库）；头像下载记在 ``url_imports``，
    这样测试能分开断言"生成了几张图"与"下载了几张头像"。
    """

    import_limit: int = 0
    blobs: dict[str, bytes] = field(default_factory=dict)
    imported: list[FakeMedia] = field(default_factory=list)
    url_imports: list[FakeMedia] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    references: list[object] = field(default_factory=list)
    import_error: BaseException | None = None

    def _next_id(self) -> str:
        return f"media:{len(self.blobs) + 1:064x}"

    async def import_reader(self, ctx, data, size, spec):
        if self.import_error is not None:
            raise self.import_error
        if self.import_limit and size > self.import_limit:
            raise ValueError("too large")
        media = FakeMedia(
            id=self._next_id(),
            name=getattr(spec, "name", "x.png"),
            mime_type=getattr(spec, "mime_type", "image/png") or "image/png",
        )
        self.blobs[media.id] = bytes(data)
        self.imported.append(media)
        return media

    async def import_url(self, ctx, url, spec):
        self.urls.append(url)
        media = FakeMedia(
            id=self._next_id(),
            name=getattr(spec, "name", "avatar.png"),
            mime_type="image/png",
        )
        self.blobs[media.id] = b"avatar-bytes"
        self.url_imports.append(media)
        return media

    async def read(self, ctx, id_value):
        if id_value not in self.blobs:
            raise NotFoundError(f"media {id_value} not found")
        return self.blobs[id_value], FakeMedia(id=id_value, name=id_value + ".png")

    def add_reference(self, reference) -> None:
        self.references.append(reference)


class FakeCharacterStore:
    """角色库的假实现（只读面）。"""

    def __init__(self, cards: list[Character] | None = None) -> None:
        self.cards = {card.id: card for card in cards or []}
        self.read_calls: list[tuple[str, str]] = []

    def enabled(self) -> bool:
        return True

    def list(self, ctx=None, viewer=None):
        return list(self.cards.values())

    def get_visible(self, ctx, id_value, viewer=None):
        card = self.cards.get(id_value)
        if card is None:
            raise NotFoundError(f"character {id_value} not found")
        return card

    def read_image(self, ctx, id_value, name, viewer=None):
        self.read_calls.append((id_value, name))
        return b"card-image-bytes", "image/png"


class FakeAvatarResolver:
    """按 (platform, user_id) 返回头像 URL；没有映射就返回空串。"""

    def __init__(self, mapping: dict[tuple[str, str], str] | None = None) -> None:
        self.mapping = dict(mapping or {})
        self.calls: list[tuple[str, str]] = []

    async def avatar_url(self, ctx, platform: str, user_id: str) -> str:
        self.calls.append((platform, user_id))
        return self.mapping.get((platform, user_id), "")


def make_character(
    character_id: str,
    name: str,
    *,
    aliases: list[str] | None = None,
    images: list[Image] | None = None,
    image_settings: ImageSettings | None = None,
) -> Character:
    """造一张角色卡（不落盘）。"""
    return Character(
        id=character_id,
        name=name,
        aliases=list(aliases or []),
        description=f"{name} 的设定",
        images=list(images or []),
        image=image_settings or ImageSettings(),
    )


def make_character_model(
    character_id: str = "member:qqonebot:1001",
    name: str = "小明",
    *,
    reference_kind: str = "avatar",
    reference_source: str = "https://example.invalid/avatar.png",
    character_id_value: str = "",
) -> ComicCharacter:
    """造计划里的人物的值对象。"""
    return ComicCharacter(
        id=character_id,
        display_name=name,
        source="avatar" if reference_kind == "avatar" else "character",
        character_id=character_id_value,
        reference=ComicCharacterReference(kind=reference_kind, source=reference_source),
    )


def make_plan(
    *,
    panel_count: int = 2,
    characters: list[ComicCharacter] | None = None,
    plan_id: str = "plan-0001",
    revision: int = 1,
) -> ComicPlan:
    """造一份通过校验的最小计划（每格一句原文台词）。"""
    cast = characters or [make_character_model()]
    panels: list[ComicPanel] = []
    for index in range(panel_count):
        message_id = f"msg-{index + 1}"
        panels.append(
            ComicPanel(
                index=index + 1,
                scene=f"第 {index + 1} 格的画面：两人在群里聊天",
                character_ids=[cast[0].id],
                dialogue=[
                    ComicDialogue(
                        speaker_id=cast[0].id,
                        text=f"这是第 {index + 1} 句原话",
                        kind="original",
                        source_message_ids=[message_id],
                    )
                ],
                source_message_ids=[message_id],
            )
        )
    return ComicPlan(
        plan_id=plan_id,
        revision=revision,
        title="群聊漫画：测试",
        language="zh",
        layout="grid",
        panel_count=panel_count,
        characters=cast,
        panels=panels,
        source=ComicSource(
            platform="qqonebot",
            scope_id="group-1",
            message_ids=[f"msg-{index + 1}" for index in range(panel_count)],
            message_count=panel_count,
        ),
        planner="rules",
    )
