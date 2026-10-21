"""平台无关的输出意图、发送凭据与投递规则。

对应 Go：``internal/delivery/delivery.go``。

本模块是"把一条回答变成平台动作"的唯一契约层：``Output`` 描述一条输出意图，
``Receipt``/``SentMessage`` 描述平台真正接受了什么，``Manager`` 负责校验并把意图
交给适配器。工具调用进度预览的识别（``TOOL_PREVIEW_PREFIX`` /
``Output.is_tool_preview`` / ``is_tool_preview_notice`` /
``should_drop_group_tool_preview``）也集中在这里，平台适配器不再各写一份。

与 Go 的差异（仅形式）：Go 的 ``(Receipt, error)`` 双返回在 Python 里改为返回
``Receipt`` / 抛异常；发送失败但已投递部分内容时，抛出的 :class:`DeliveryError`
上带着 ``receipt``，调用方不会丢失"哪些已经发出去了"这件事。``context.Context``
→ 鸭子类型 ``ctx``：优先用 ctx 自带的 ``with_value`` / ``value`` / ``error``
（:class:`atri_x.safecontext.CancelToken` 即这套接口），否则退回 ``Mapping``
（``dict``）；``None`` 视为空上下文。
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlparse

from atri_x.errors import AtriXError, InvalidError


class Kind:
    """一条输出意图的种类。对应 Go ``Kind``。"""

    TEXT = "text"
    EMOTICON = "emoticon"
    IMAGE = "image"
    FILE = "file"
    RECORD = "record"
    AT = "at"
    REPLY = "reply"

    ALL = (TEXT, EMOTICON, IMAGE, FILE, RECORD, AT, REPLY)


KindText = Kind.TEXT
KindEmoticon = Kind.EMOTICON
KindImage = Kind.IMAGE
KindFile = Kind.FILE
KindRecord = Kind.RECORD
KindAt = Kind.AT
KindReply = Kind.REPLY

META_HOOK_POINT = "hook.point"
META_HOOK_NAME = "hook.name"
META_HOOK_MODE = "hook.mode"
META_DELIVERY_TIMING = "delivery.timing"

DELIVERY_IMMEDIATE = "immediate"
DELIVERY_AFTER_ASSISTANT = "after_assistant"

# ToolPreviewPrefix 是工具调用进度预览的固定前缀。Agent 在组织预览正文时写入它
# （formatToolPreview），平台适配器与历史写入用它识别"这条通知不是用户可见回答"。
# 这条规则此前在 OneBot、QQ 官方、hook 出站记录三处各写一遍，改一处会漏掉其余两处，
# 因此把前缀与判定集中在交付层，只留"是否群聊目标"由各平台自己决定。
TOOL_PREVIEW_PREFIX = "[tool] "


class DeliveryError(AtriXError):
    """一次发送没有全部成功；``receipt`` 保留已经成功投递的部分。

    对应 Go ``(Receipt, error)`` 双返回：用异常携带 receipt，失败路径不丢信息。
    """

    def __init__(self, message: str, receipt: "Receipt | None" = None) -> None:
        super().__init__(message)
        self.receipt = receipt if receipt is not None else Receipt()


_TEMPORARY_CONNECTION_KEY = object()


def ctx_with_value(ctx: Any, key: Any, value: Any) -> Any:
    """对应 Go ``context.WithValue``。"""
    setter = getattr(ctx, "with_value", None)
    if callable(setter):
        return setter(key, value)
    out: dict[Any, Any] = dict(ctx) if isinstance(ctx, Mapping) else {}
    out[key] = value
    return out


def ctx_value(ctx: Any, key: Any, default: Any = None) -> Any:
    if ctx is None:
        return default
    getter = getattr(ctx, "value", None)
    if callable(getter):
        return getter(key, default)
    if isinstance(ctx, Mapping):
        return ctx.get(key, default)
    return default


def ctx_error(ctx: Any) -> BaseException | None:
    """对应 Go ``ctx.Err()``：未取消时返回 ``None``。"""
    if ctx is None:
        return None
    getter = getattr(ctx, "error", None) or getattr(ctx, "err", None)
    if callable(getter):
        return getter()
    return AtriXError("context cancelled") if getattr(ctx, "cancelled", False) else None


def with_temporary_connection(ctx: Any) -> Any:
    """对应 Go ``WithTemporaryConnection``。返回新的 ctx。"""
    return ctx_with_value(ctx, _TEMPORARY_CONNECTION_KEY, True)


def use_temporary_connection(ctx: Any) -> bool:
    return ctx_value(ctx, _TEMPORARY_CONNECTION_KEY) is True


@dataclass
class Target:
    """一条通知的落点。对应 Go ``Target``。空 Target 表示"发给当前会话"。"""

    platform: str = ""
    scope_id: str = ""
    private_user_id: str = ""
    group_id: str = ""
    superadmins: bool = False

    def empty(self) -> bool:
        """对应 Go ``Target.Empty``。"""
        return (
            not self.platform.strip()
            and not self.scope_id.strip()
            and not self.private_user_id.strip()
            and not self.group_id.strip()
            and not self.superadmins
        )

    def to_json(self) -> dict[str, Any]:
        """对应 Go ``Target`` 的 json tag（全字段 omitempty）。"""
        out: dict[str, Any] = {
            "platform": self.platform,
            "scope_id": self.scope_id,
            "private_user_id": self.private_user_id,
            "group_id": self.group_id,
        }
        if self.superadmins:
            out["superadmins"] = True
        return {key: value for key, value in out.items() if value}


@dataclass
class Notice:
    """一条通知。对应 Go ``Notice``。``level`` 为 ``logging`` 级别整数。"""

    target: Target = field(default_factory=Target)
    outputs: list["Output"] = field(default_factory=list)
    level: int = 0


@dataclass
class Source:
    """一条媒体来源。对应 Go ``Source``。

    ``path`` / ``media_id`` / ``url`` / ``data`` 必须**恰好有一个**非空（由
    :func:`validate_outputs` 检查）；``to_json`` 对齐 Go 的 ``media`` 标签。
    """

    url: str = ""
    path: str = ""
    media_id: str = ""
    mime_type: str = ""
    data: bytes = b""

    def to_json(self) -> dict[str, Any]:
        return {"media": self.media_id} if self.media_id else {}


def is_http_media_source(value: str) -> bool:
    value = (value or "").strip().lower()
    return value.startswith("http://") or value.startswith("https://")


@dataclass
class Output:
    """一条输出意图。对应 Go ``Output``。"""

    kind: str = ""
    text: str = ""
    name: str = ""
    emoticon_id: str = ""
    alt_text: str = ""
    reply_to_platform_message_id: str = ""
    source: Source = field(default_factory=Source)
    target: Target = field(default_factory=Target)
    meta: dict[str, Any] | None = None

    def is_tool_preview(self) -> bool:
        """报告这条输出是否是工具调用进度预览。对应 Go ``Output.IsToolPreview``。

        判定使用完整前缀（含分隔空格），因此只有前缀、没有正文的 ``"[tool]"``
        不算预览；Agent 的 ``formatToolPreview`` 一定写出"前缀 + 正文"。
        """
        return self.kind == Kind.TEXT and self.text.strip().startswith(TOOL_PREVIEW_PREFIX)


def is_tool_preview_notice(outputs: Sequence[Output] | None) -> bool:
    """报告这条通知是否是"单条工具调用进度预览"。对应 Go ``IsToolPreviewNotice``。"""
    return bool(outputs) and len(outputs) == 1 and outputs[0].is_tool_preview()


def should_drop_group_tool_preview(
    target: Target,
    outputs: Sequence[Output] | None,
    group_context: bool,
    show_in_group: bool = False,
) -> bool:
    """"群聊里不投递工具调用进度预览"这条平台跳过规则的唯一入口。

    对应 Go ``ShouldDropGroupToolPreview``。规则由三个条件组成：通知没有显式目标
    （发给当前会话）、当前上下文是群聊、且这条通知就是工具调用进度预览——群聊里的
    进度预览是噪声，丢弃时也不算失败。唯一按平台变化的部分是"当前上下文是不是
    群聊"，由调用方传入；Telegram 与本地 CLI 不跳过预览。

    ``show_in_group`` 是 Python 侧新增的开关（``[platform.qqonebot]
    .show_tool_preview_in_group`` / ``[platform.qqofficial]`` 同名字段）：打开后
    群聊也投递进度预览，群成员能看到"正在调用哪个工具"。
    """
    if show_in_group:
        return False
    return target.empty() and bool(group_context) and is_tool_preview_notice(outputs)


def with_delivery_timing(out: Output, timing: str) -> Output:
    timing = (timing or "").strip()
    if timing == "" or timing == DELIVERY_IMMEDIATE:
        return out
    meta = dict(out.meta) if out.meta else {}
    meta[META_DELIVERY_TIMING] = timing
    return dataclasses.replace(out, meta=meta)


def delivery_timing(out: Output) -> str:
    return _output_meta_string(out, META_DELIVERY_TIMING) or DELIVERY_IMMEDIATE


def validate_delivery_timing(timing: str) -> None:
    """对应 Go ``ValidateDeliveryTiming``；不合法时抛 :class:`InvalidError`。"""
    if (timing or "").strip() in ("", DELIVERY_IMMEDIATE, DELIVERY_AFTER_ASSISTANT):
        return
    raise InvalidError(f"unsupported timing {timing!r}")


def split_by_delivery_timing(outputs: Sequence[Output] | None) -> tuple[list[Output], list[Output]]:
    if not outputs:
        return [], []
    immediate: list[Output] = []
    deferred: list[Output] = []
    for out in outputs:
        if delivery_timing(out) == DELIVERY_AFTER_ASSISTANT:
            deferred.append(out)
        else:
            immediate.append(out)
    return immediate, deferred


def text(value: str) -> Output:
    return Output(kind=Kind.TEXT, text=value)


def emoticon(emoticon_id: str, name: str, value: str = "") -> Output:
    emoticon_id = (emoticon_id or "").strip()
    name = (name or "").strip()
    out = Output(kind=Kind.EMOTICON, emoticon_id=emoticon_id, name=name, text=(value or "").strip())
    if name:
        out.alt_text = "[表情: " + name + "]"
    elif out.text:
        out.alt_text = out.text
    return out


def image_path(path: str) -> Output:
    return Output(kind=Kind.IMAGE, source=Source(path=path))


def file_path(path: str) -> Output:
    return Output(kind=Kind.FILE, source=Source(path=path))


def record_path(path: str) -> Output:
    return Output(kind=Kind.RECORD, source=Source(path=path))


def at(user_id: str) -> Output:
    user_id = (user_id or "").strip()
    out = Output(kind=Kind.AT, name=user_id)
    if user_id:
        out.alt_text = "@" + user_id
    return out


def reply(platform_message_id: str, value: str) -> Output:
    return Output(
        kind=Kind.REPLY,
        text=value,
        reply_to_platform_message_id=(platform_message_id or "").strip(),
    )


@dataclass
class SentMessage:
    """一次发送真正产生的平台消息。对应 Go ``SentMessage``。"""

    platform_message_id: str = ""
    platform: str = ""
    scope_id: str = ""
    output_indexes: list[int] = field(default_factory=list)


@dataclass
class Receipt:
    """一次发送的结果凭据。对应 Go ``Receipt``。"""

    platform_message_ids: list[str] = field(default_factory=list)
    sent_messages: list[SentMessage] = field(default_factory=list)
    # failed 表示这次发送在"每一页/每一段都被平台接受"之前就停了；
    # platform_message_ids / sent_messages 仍描述已成功的投递，调用方可据此避免重发。
    # failure 是 failed 的简短、非敏感原因，刻意不裹成异常对象以保持可序列化。
    failed: bool = False
    failure: str = ""

    def _copy(self) -> "Receipt":
        return dataclasses.replace(
            self,
            platform_message_ids=list(self.platform_message_ids),
            sent_messages=list(self.sent_messages),
        )

    def mark_partial_failure(self, err: BaseException | None) -> "Receipt":
        """对应 Go ``MarkPartialFailure``：给已有成功投递的凭据补上失败标记。"""
        out = self._copy()
        if err is None:
            return out
        out.failed = True
        if not out.failure:
            out.failure = str(err)
        return out

    def merge(self, other: "Receipt") -> "Receipt":
        """对应 Go ``Merge``：并入另一次发送的成功投递与部分失败标记。"""
        out = dataclasses.replace(
            self,
            platform_message_ids=[*self.platform_message_ids, *other.platform_message_ids],
            sent_messages=[*self.sent_messages, *other.sent_messages],
        )
        if other.failed:
            out.failed = True
            if not out.failure:
                out.failure = other.failure
        return out


@runtime_checkable
class MessageStream(Protocol):
    """一条可增量追加、最后可整体替换的助手消息。对应 Go ``MessageStream``。"""

    async def append(self, ctx: Any, value: str) -> None: ...
    async def replace(self, ctx: Any, value: str) -> Receipt: ...
    async def finish(self, ctx: Any) -> Receipt: ...


@runtime_checkable
class StreamingMessageSender(Protocol):
    """可选能力：可编辑的流式投递。对应 Go ``StreamingMessageSender``。

    ``start_stream`` 是**同步工厂**（与 Go 的 ``StartStream(ctx) (MessageStream, error)``
    一致）：它只做"能不能流式"的判断并返回一个流对象，不产生 I/O 等待。
    调用点 :func:`atri_x.agent.turn_output.start_message_stream` 也是同步的，失败即
    退回整条发送。三个实现（CLI / 远程 CLI / Telegram）都必须是同步的 ——
    历史事故：实现写成了 ``async def``，调用点拿到的协程被当成 ``MessageStream``，
    首个增量 ``append`` 抛 ``AttributeError``，本地 CLI 每轮对话失败。

    ``MessageStream`` 的三个方法则是 ``async``（Python 侧 I/O 走 asyncio），
    调用点必须 ``await``。
    """

    def start_stream(self, ctx: Any) -> MessageStream: ...


@runtime_checkable
class MessageSender(Protocol):
    """一次逻辑消息（由有序输出段组成）的发送者。对应 Go ``MessageSender``。

    适配器失败时抛异常；已投递部分内容时抛 :class:`DeliveryError` 并带上
    ``receipt``（对应 Go 的 ``(Receipt, error)`` 双返回）。
    """

    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt: ...
    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt: ...


# ContextSender 能用 ctx 里携带的路由信息回发；Sender 是 MessageSender 的别名。
ContextSender = MessageSender
Sender = MessageSender


@dataclass
class Manager:
    """带校验与部分失败标记的发送管理。对应 Go ``Manager``。"""

    sender: Any = None
    logger: logging.Logger | None = None

    async def send_notices(self, ctx: Any, outputs: Sequence[Output]) -> None:
        await self.send_notices_with_receipt(ctx, outputs)

    async def send_notices_with_receipt(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        return await self.send_notice(ctx, Notice(outputs=list(outputs)))

    async def send_chat(self, ctx: Any, outputs: Sequence[Output]) -> Receipt:
        _raise_if_cancelled(ctx)
        if self.sender is None:
            raise InvalidError("output sender is not configured")
        validate_outputs(outputs)
        try:
            return await self.sender.send_chat(ctx, outputs)
        except BaseException as exc:
            # 适配器已经会标记分页发送；这里防御性地再标一次，避免"平台只累积 ID"
            # 的适配器被报成干净失败。
            raise _partial_failure(exc) from exc

    async def send_notice(self, ctx: Any, notice: Notice) -> Receipt:
        _raise_if_cancelled(ctx)
        if self.sender is None or not notice.outputs:
            return Receipt()
        validate_outputs(notice.outputs)
        configured_target = validate_outputs_target(notice.outputs)
        if notice.target.empty():
            notice.target = configured_target
        try:
            return await self.sender.send_notice(ctx, notice)
        except BaseException as exc:
            if self.logger is not None:
                attrs = _output_log_attrs(
                    notice.outputs[0], "platform", notice.target.platform, "error", str(exc)
                )
                self.logger.warning("notice output failed %s", attrs)
            wrapped = _wrap_output_source_error(notice.outputs[0], exc)
            raise DeliveryError(str(wrapped), _receipt_of(exc).mark_partial_failure(exc)) from exc


def new_manager(sender: Any = None, logger: logging.Logger | None = None) -> Manager:
    return Manager(sender=sender, logger=logger)


def _receipt_of(err: BaseException) -> Receipt:
    """取适配器随异常带出的凭据。"""
    return err.receipt if isinstance(err, DeliveryError) else Receipt()


def _partial_failure(err: BaseException) -> DeliveryError:
    """对应 Go 失败路径上的 ``receipt.MarkPartialFailure(err), err``。

    适配器用 :class:`DeliveryError` 报"已投递部分 + 失败"时沿用它的 receipt，
    其余异常按空 receipt 处理。
    """
    return DeliveryError(str(err), _receipt_of(err).mark_partial_failure(err))


def _raise_if_cancelled(ctx: Any) -> None:
    err = ctx_error(ctx)
    if err is not None:
        raise err


def _output_meta_string(out: Output, key: str) -> str:
    if not out.meta:
        return ""
    value = out.meta.get(key)
    return value.strip() if isinstance(value, str) else ""


def _output_log_attrs(out: Output, *attrs: Any) -> tuple[Any, ...]:
    """对应 Go ``outputLogAttrs``：把来源元信息补进日志属性。"""
    out_attrs = list(attrs)
    for key, label in (
        (META_HOOK_NAME, "hook"),
        (META_HOOK_POINT, "hook_point"),
        (META_HOOK_MODE, "hook_mode"),
    ):
        value = _output_meta_string(out, key)
        if value:
            out_attrs.extend((label, value))
    return tuple(out_attrs)


def _wrap_output_source_error(out: Output, err: BaseException | None) -> BaseException | None:
    """对应 Go ``wrapOutputSourceError``：指出失败输出来自哪个 hook。"""
    if err is None:
        return None
    for key in (META_HOOK_NAME, META_HOOK_POINT):
        value = _output_meta_string(out, key)
        if value:
            return InvalidError(f"hook {value} output: {err}")
    return err


def fallback_output(outputs: Sequence[Output] | None) -> Output:
    """对应 Go ``FallbackOutput``：把多条输出压成一条纯文本。"""
    parts: list[str] = []
    for out in outputs or []:
        value = fallback_text(out).strip()
        if value:
            parts.append(value)
    return text("\n".join(parts))


def validate_outputs_target(outputs: Sequence[Output] | None) -> Target:
    """对应 Go ``ValidateOutputsTarget``：一批输出只能有一个显式目标。"""
    target = Target()
    for out in outputs or []:
        if out.target.empty():
            continue
        if target.empty():
            target = out.target
            continue
        if target != out.target:
            raise InvalidError("outputs in one batch must use the same target")
    return target


def validate_outputs(outputs: Sequence[Output] | None) -> None:
    """对应 Go ``ValidateOutputs``。不合法时抛 :class:`InvalidError`。"""
    for i, out in enumerate(outputs or []):
        try:
            validate_delivery_timing(delivery_timing(out))
        except InvalidError as exc:
            raise InvalidError(f"outputs[{i}]: {exc}") from exc
        source_count = (
            bool((out.source.path or "").strip())
            + bool((out.source.media_id or "").strip())
            + bool((out.source.url or "").strip())
            + bool(out.source.data)
        )
        if out.kind == Kind.TEXT:
            if source_count != 0:
                raise InvalidError(f"outputs[{i}]: text output cannot have a media source")
        elif out.kind in (Kind.IMAGE, Kind.FILE, Kind.RECORD):
            if source_count != 1:
                raise InvalidError(
                    f"outputs[{i}]: image/file/record output must have exactly one media source"
                )
            if "://" in (out.source.path or "").strip():
                raise InvalidError(
                    f"outputs[{i}]: media path must be a filesystem path, not a URI"
                )
            url = (out.source.url or "").strip()
            if url and not _is_absolute_http_url(url):
                raise InvalidError(f"outputs[{i}]: media URL must be an absolute HTTP(S) URL")
        elif out.kind == Kind.EMOTICON:
            if source_count != 0:
                raise InvalidError(f"outputs[{i}]: emoticon output cannot have a media source")
            if not (out.emoticon_id or "").strip():
                raise InvalidError(f"outputs[{i}]: emoticon output requires an emoticon ID")
        elif out.kind == Kind.AT:
            if not (out.name or "").strip():
                raise InvalidError(f"outputs[{i}]: at output requires a user ID")
        elif out.kind == Kind.REPLY:
            if not (out.reply_to_platform_message_id or "").strip():
                raise InvalidError(f"outputs[{i}]: reply output requires a message ID")
        else:
            raise InvalidError(f"outputs[{i}]: unsupported output kind {out.kind!r}")
    validate_outputs_target(outputs)


def _is_absolute_http_url(value: str) -> bool:
    """Go 侧 ``url.Parse`` + scheme/host 检查的等价物。"""
    try:
        u = urlparse(value)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.hostname)


def fallback_text(out: Output) -> str:
    """对应 Go ``FallbackText``：一条输出在纯文本平台上的降级表示。"""
    if out.alt_text:
        return out.alt_text
    if out.kind == Kind.TEXT:
        return out.text
    if out.kind == Kind.EMOTICON:
        name = (out.name or "").strip() or (out.text or "").strip()
        return f"[表情: {name}]" if name else ""
    if out.kind == Kind.AT:
        name = (out.name or "").strip() or (out.text or "").strip()
        return f"@{name}" if name else ""
    if out.kind == Kind.REPLY:
        reply_id = (out.reply_to_platform_message_id or "").strip()
        if not reply_id:
            return out.text
        return f"[引用消息 {reply_id}]\n{out.text}"
    if out.kind in (Kind.IMAGE, Kind.FILE, Kind.RECORD):
        label = first_non_empty(out.name, out.source.url, out.source.path, out.text)
        fallback_label = {Kind.IMAGE: "图片", Kind.FILE: "文件", Kind.RECORD: "语音"}[out.kind]
        return f"[{fallback_label}: {label}]" if label else f"[{fallback_label}]"
    return first_non_empty(out.text, out.name, out.alt_text)


def first_non_empty(*values: str) -> str:
    for value in values:
        if (value or "").strip():
            return value
    return ""


__all__ = [name for name in dir() if not name.startswith("_")]
