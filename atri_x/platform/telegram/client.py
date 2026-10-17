"""Telegram Bot API 客户端（long polling、发送、媒体上传与下载）。

对应 Go：``internal/platform/telegram/client.go``。

形式差异：

* Go 的 ``net/http`` 换成已完成的 :class:`atri_x.net.httpclient.HTTPClient`
  （纯标准库封装）。未显式配置代理时，Go 的 ``http.DefaultTransport`` 会走
  ``ProxyFromEnvironment``，``HTTPClient`` 同样回落到 ``HTTPS_PROXY``，行为一致。
* ``callTelegram[T]`` 的泛型 → ``parse`` 回调；``json.RawMessage`` 结果用重新
  序列化的 JSON 文本表示。
* ``context.WithTimeout`` → 每次请求的 ``timeout`` 参数 + 把 ctx 作为 ``cancel``
  传给 HTTPClient（ctx 需提供 ``is_cancelled()``，见 :class:`atri_x.safecontext.CancelToken`）。
* 传输层错误同样做 token 脱敏：:class:`RedactedTelegramError` 的 ``__cause__``
  保留原异常（对应 Go ``redactedTelegramError.Unwrap``）。
* Python 侧新增 :class:`TelegramAPIError`：Go 只返回带 ``http %d code %d`` 文案的
  error，这里额外把 ``status`` / ``error_code`` / ``description`` 暴露成字段，
  便于调用方识别 409（同 token 多实例）等状态；文案与 Go 逐字一致。
* 下载用缓冲响应后校验长度（Go 用 ``io.LimitReader`` 边读边截断），因此超大文件
  会先整体进内存再报错 —— ``atri_x/net/httpclient.py`` 未提供读取上限口子。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable

from atri_x.errors import AtriXError, InvalidError, MediaTooLargeError
from atri_x.net.httpclient import HTTPClient, HTTPError, build_multipart
from atri_x.platform.telegram.config import Config
from atri_x.platform.telegram.types import (
    AnswerCallbackQueryRequest,
    ApiResponse,
    BotCommand,
    ChatMember,
    EditMessageTextRequest,
    FileInfo,
    GetChatMemberRequest,
    GetFileRequest,
    GetUpdatesRequest,
    SendMessageRequest,
    SendRichMessageDraftRequest,
    SendRichMessageRequest,
    SentMessage,
    SetMyCommandsRequest,
    Update,
    User,
    api_response_from_json,
)

#: 允许的更新类型。对应 Go ``getUpdates`` 里的 ``AllowedUpdates``。
ALLOWED_UPDATES = ["message", "callback_query"]


class TelegramAPIError(AtriXError):
    """Bot API 返回 ``ok=false`` 或非 2xx。对应 Go ``decodeTelegramResponse`` 的 error。

    ``message`` 与 Go 的文案逐字一致（``telegram api %s failed: http %d code %d: %s``）。
    """

    def __init__(
        self, message: str, *, status: int = 0, error_code: int = 0, description: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.description = description


class RedactedTelegramError(AtriXError):
    """脱敏后的传输层错误。对应 Go ``redactedTelegramError``。

    ``__cause__`` 保留原始异常（Go 的 ``Unwrap``），``err`` 是同一对象。
    """

    def __init__(self, text: str, err: BaseException) -> None:
        super().__init__(text)
        self.err = err
        self.__cause__ = err


@dataclass
class MediaSource:
    """一次出站媒体上传的来源。对应 Go ``mediaSource``（**不是** ``delivery.Source``）。"""

    url: str = ""
    path: str = ""
    data: bytes = b""
    name: str = ""


def new_api_client(cfg: Config, http: HTTPClient | None = None) -> "ApiClient":
    """对应 Go ``newAPIClient``。"""
    return ApiClient(cfg, http)


def new_http_client(cfg: Config) -> HTTPClient:
    """按配置构造 HTTP 客户端。对应 Go ``newHTTPClient``。"""
    return HTTPClient(timeout=cfg.api_timeout(), proxy=cfg.http_proxy())


class ApiClient:
    """Bot API 调用封装。对应 Go ``apiClient``。"""

    def __init__(self, cfg: Config, http: HTTPClient | None = None) -> None:
        self.cfg = cfg
        self.http = http if http is not None else new_http_client(cfg)
        self.token = cfg.token()

    # -- URL ---------------------------------------------------------------
    def api_url(self, method: str) -> str:
        """对应 Go ``apiURL``。"""
        return (self.cfg.api_base_url or "").rstrip("/") + "/bot" + self.token + "/" + (method or "").lstrip("/")

    def file_url(self, file_path: str) -> str:
        """对应 Go ``fileURL``（下载路径同样带 bot token，日志里会被脱敏）。"""
        return (
            (self.cfg.file_base_url or "").rstrip("/")
            + "/bot"
            + self.token
            + "/"
            + (file_path or "").lstrip("/")
        )

    # -- 基础调用 -----------------------------------------------------------
    async def call_raw(self, ctx: Any, method: str, params: dict[str, Any] | None) -> str:
        """原样返回 ``result`` 的 JSON 文本。对应 Go ``callRaw``。"""
        return await self._call(
            ctx,
            method,
            params,
            self.cfg.api_timeout(),
            lambda result: json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )

    async def get_me(self, ctx: Any) -> User:
        """对应 Go ``getMe``。"""
        return await self._call(ctx, "getMe", None, self.cfg.api_timeout(), User.from_json)

    async def get_updates(self, ctx: Any, offset: int) -> list[Update]:
        """长轮询取更新。对应 Go ``getUpdates``。"""
        request = GetUpdatesRequest(
            offset=offset,
            timeout=int(self.cfg.poll_timeout()),
            allowed_updates=list(ALLOWED_UPDATES),
        )
        timeout = self.cfg.poll_timeout() + self.cfg.api_timeout()
        return await self._call(
            ctx,
            "getUpdates",
            request,
            timeout,
            lambda result: [Update.from_json(item) for item in result] if isinstance(result, list) else [],
        )

    async def send_message(self, ctx: Any, request: SendMessageRequest) -> SentMessage:
        """发送文本；``parse_mode`` 导致的解析失败会去掉 ``parse_mode`` 重试一次。

        对应 Go ``sendMessage``。
        """
        try:
            return await self._call(
                ctx, "sendMessage", request, self.cfg.api_timeout(), SentMessage.from_json
            )
        except Exception as exc:  # noqa: BLE001 - 与 Go 一致：任何错误都先看是否实体解析失败
            if not request.parse_mode or not is_telegram_parse_error(exc):
                raise
            request.parse_mode = ""
            return await self._call(
                ctx, "sendMessage", request, self.cfg.api_timeout(), SentMessage.from_json
            )

    async def edit_message_text(self, ctx: Any, request: EditMessageTextRequest) -> SentMessage:
        """编辑文本；解析失败时同样去掉 ``parse_mode`` 重试一次。对应 Go ``editMessageText``。"""
        try:
            return await self._call(
                ctx, "editMessageText", request, self.cfg.api_timeout(), SentMessage.from_json
            )
        except Exception as exc:  # noqa: BLE001 - 同 sendMessage：解析失败才降级重试
            if not request.parse_mode or not is_telegram_parse_error(exc):
                raise
            request.parse_mode = ""
            return await self._call(
                ctx, "editMessageText", request, self.cfg.api_timeout(), SentMessage.from_json
            )

    async def set_my_commands(self, ctx: Any, commands: list[BotCommand]) -> None:
        """同步 bot 命令菜单。对应 Go ``setMyCommands``。"""
        await self._call(
            ctx,
            "setMyCommands",
            SetMyCommandsRequest(commands=list(commands)),
            self.cfg.api_timeout(),
            lambda result: result,
        )

    async def send_rich_message(self, ctx: Any, request: SendRichMessageRequest) -> SentMessage:
        """对应 Go ``sendRichMessage``。"""
        return await self._call(
            ctx, "sendRichMessage", request, self.cfg.api_timeout(), SentMessage.from_json
        )

    async def send_rich_message_draft(self, ctx: Any, request: SendRichMessageDraftRequest) -> None:
        """对应 Go ``sendRichMessageDraft``。"""
        await self._call(
            ctx, "sendRichMessageDraft", request, self.cfg.api_timeout(), lambda result: result
        )

    async def answer_callback_query(self, ctx: Any, callback_id: str, text: str) -> None:
        """对应 Go ``answerCallbackQuery``。"""
        await self._call(
            ctx,
            "answerCallbackQuery",
            AnswerCallbackQueryRequest(callback_query_id=callback_id, text=text),
            self.cfg.api_timeout(),
            lambda result: result,
        )

    async def get_chat_member(self, ctx: Any, chat_id: int, user_id: int) -> ChatMember:
        """对应 Go ``getChatMember``。"""
        return await self._call(
            ctx,
            "getChatMember",
            GetChatMemberRequest(chat_id=chat_id, user_id=user_id),
            self.cfg.api_timeout(),
            ChatMember.from_json,
        )

    async def get_file(self, ctx: Any, file_id: str) -> FileInfo:
        """对应 Go ``getFile``。"""
        return await self._call(
            ctx, "getFile", GetFileRequest(file_id=file_id), self.cfg.api_timeout(), FileInfo.from_json
        )

    # -- 媒体 ---------------------------------------------------------------
    async def download_file(self, ctx: Any, file_path: str, max_bytes: int) -> bytes:
        """按 ``file_path`` 下载文件并校验上限。对应 Go ``downloadFile``。"""
        try:
            response = await self.http.request(
                "GET",
                self.file_url(file_path),
                timeout=self.cfg.api_timeout(),
                cancel=_cancel_of(ctx),
            )
        except HTTPError as exc:
            raise_redacted(exc, self.token)
        data = response.body
        if not response.ok:
            raise TelegramAPIError(
                f"telegram file download failed: http {response.status}", status=response.status
            )
        if len(data) > max_bytes:
            raise MediaTooLargeError(f"telegram media exceeds import limit of {max_bytes} bytes")
        return data

    async def send_photo(
        self, ctx: Any, chat_id: int, source: MediaSource, reply_to: int
    ) -> SentMessage:
        """对应 Go ``sendPhoto``。"""
        return await self.send_media(ctx, "sendPhoto", chat_id, "photo", source, reply_to)

    async def send_document(
        self, ctx: Any, chat_id: int, source: MediaSource, reply_to: int
    ) -> SentMessage:
        """对应 Go ``sendDocument``。"""
        return await self.send_media(ctx, "sendDocument", chat_id, "document", source, reply_to)

    async def send_sticker(
        self, ctx: Any, chat_id: int, sticker_id: str, reply_to: int
    ) -> SentMessage:
        """对应 Go ``sendSticker``（原生 sticker file id，不做上传）。"""
        return await self.send_media_by_reference(
            ctx, "sendSticker", chat_id, "sticker", (sticker_id or "").strip(), reply_to
        )

    async def send_media(
        self,
        ctx: Any,
        method: str,
        chat_id: int,
        field: str,
        source: MediaSource,
        reply_to: int,
    ) -> SentMessage:
        """URL 走引用，其余走 multipart 上传。对应 Go ``sendMedia``。"""
        value = (source.url or "").strip()
        if value != "":
            return await self.send_media_by_reference(ctx, method, chat_id, field, value, reply_to)
        body, content_type = multipart_media_body(chat_id, field, source, reply_to)
        return await self.do_multipart(ctx, method, body, content_type)

    async def send_media_by_reference(
        self, ctx: Any, method: str, chat_id: int, field: str, value: str, reply_to: int
    ) -> SentMessage:
        """按 file_id / URL 引用发送。对应 Go ``sendMediaByReference``。"""
        body: dict[str, Any] = {"chat_id": chat_id, field: value}
        if reply_to != 0:
            body["reply_to_message_id"] = reply_to
        return await self._call(ctx, method, body, self.cfg.api_timeout(), SentMessage.from_json)

    async def do_multipart(
        self, ctx: Any, method: str, body: bytes, content_type: str
    ) -> SentMessage:
        """发送 multipart 请求体。对应 Go ``doMultipart``。"""
        try:
            response = await self.http.request(
                "POST",
                self.api_url(method),
                headers={"Content-Type": content_type},
                body=body,
                timeout=self.cfg.api_timeout(),
                cancel=_cancel_of(ctx),
            )
        except HTTPError as exc:
            raise_redacted(exc, self.token)
        return decode_telegram_response(method, response.status, response.body, SentMessage.from_json)

    # -- 内部 ---------------------------------------------------------------
    async def _call(
        self,
        ctx: Any,
        method: str,
        payload: Any,
        timeout: float,
        parse: Callable[[Any], Any],
    ) -> Any:
        """对应 Go ``callTelegram``。"""
        headers: dict[str, str] = {}
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(_payload_json(payload), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            response = await self.http.request(
                "POST",
                self.api_url(method),
                headers=headers or None,
                body=body,
                timeout=timeout,
                cancel=_cancel_of(ctx),
            )
        except HTTPError as exc:
            raise_redacted(exc, self.token)
        return decode_telegram_response(method, response.status, response.body, parse)


def decode_telegram_response(
    method: str, status: int, data: bytes, parse: Callable[[Any], Any] | None = None
) -> Any:
    """解码统一响应包。对应 Go ``decodeTelegramResponse``。"""
    text = data.decode("utf-8", errors="replace") if data else ""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise AtriXError(f"decode telegram {method} response: {exc}") from exc
    try:
        out: ApiResponse = api_response_from_json(raw, parse)
    except ValueError as exc:
        raise AtriXError(f"decode telegram {method} response: {exc}") from exc
    if not (200 <= status < 300) or not out.ok:
        description = (out.description or "").strip()
        if description == "":
            description = text.strip()
        raise TelegramAPIError(
            f"telegram api {method} failed: http {status} code {out.error_code}: {description}",
            status=status,
            error_code=out.error_code,
            description=description,
        )
    return out.result


def is_telegram_parse_error(err: BaseException | None) -> bool:
    """是否是 ``parse_mode`` 实体解析失败。对应 Go ``isTelegramParseError``。"""
    if err is None:
        return False
    text = str(err).lower()
    return "parse" in text or "can't parse" in text or "entities" in text


def redact_telegram_error(err: BaseException | None, token: str) -> BaseException | None:
    """把错误文本里的 bot token 换成 ``<redacted>``。对应 Go ``redactTelegramError``。"""
    if err is None:
        return None
    token = (token or "").strip()
    if token == "":
        return err
    original = str(err)
    text = original.replace("/bot" + token + "/", "/bot<redacted>/").replace(token, "<redacted>")
    if text == original:
        return err
    return RedactedTelegramError(text, err)


def raise_redacted(err: BaseException, token: str) -> None:
    """按 Go 的错误包装语义抛脱敏后的异常（未命中脱敏时原样抛）。"""
    wrapped = redact_telegram_error(err, token)
    if wrapped is err or wrapped is None:
        raise err
    raise wrapped from err


def multipart_media_body(
    chat_id: int, field: str, source: MediaSource, reply_to: int
) -> tuple[bytes, str]:
    """构造 multipart 上传体。对应 Go ``multipartMediaBody``。"""
    fields: dict[str, str] = {"chat_id": str(chat_id)}
    if reply_to != 0:
        fields["reply_to_message_id"] = str(reply_to)
    name = (source.name or "").strip()
    if name == "" and (source.path or "").strip():
        name = _path_base(source.path)
    if name == "":
        name = "file"
    if source.data:
        content = source.data
    else:
        path = (source.path or "").strip()
        if path == "":
            raise InvalidError("telegram media source is empty")
        try:
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            raise InvalidError(f"open media source {path!r}: {exc}") from exc
    boundary = "----atriXTelegram%s" % os.urandom(12).hex()
    payload = build_multipart(
        fields, [(field, name, content, "application/octet-stream")], boundary
    )
    return payload, f"multipart/form-data; boundary={boundary}"


def _payload_json(payload: Any) -> Any:
    """请求体 → JSON 可序列化结构（dataclass 走 ``to_json``）。"""
    to_json = getattr(payload, "to_json", None)
    if callable(to_json):
        return to_json()
    return payload


def _cancel_of(ctx: Any) -> Any:
    """从鸭子类型 ctx 里取出 HTTPClient 认识的取消句柄。"""
    if ctx is None:
        return None
    if isinstance(ctx, asyncio.Event):
        return ctx
    if callable(getattr(ctx, "is_cancelled", None)):
        return ctx
    return None


def _path_base(value: str) -> str:
    """对应 Go ``filepath.Base``（Windows 与 POSIX 分隔符都认）。"""
    value = (value or "").replace("\\", "/").rstrip("/")
    if "/" in value:
        value = value[value.rindex("/") + 1 :]
    return value or "/"


__all__ = [name for name in dir() if not name.startswith("_")]
