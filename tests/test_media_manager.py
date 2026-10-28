"""``atri_x.media`` 的 Manager 端到端测试。

对应上游：``internal/media/{manager,generation,platform,history,s3,output}_test.go``。

测试基础设施
------------
``atri_x.storage.sqlite`` 尚在并行开发，**本模块提供内存版 ``storage.Store`` 假实现**
（:class:`MemoryStore`），它忠实复刻 media 相关表与 v12–v16 触发器的语义：

* ``media_initial_orphan`` —— 插入 media 时 ``orphaned_at = created_at``；
* ``media_reference_added`` / ``media_reference_removed`` —— 加引清空、最后一条引用
  释放时记 ``orphaned_at``；
* ``media_reference_guard`` —— media 处于 ``deleting`` 时拒绝新引用；
* ``expire_outputs`` / ``media_output_added`` —— 输出关联与其引用同生共死；
* ``release_session_media_references`` —— 删 Session 释放其全部引用。

其余三个测试文件（resolver / lifecycle / image）从本模块 import 这套假实现，
避免同一份行为在四份文件里各写一遍。集成期 ``atri_x.storage.sqlite`` 落地后，
只需把 :class:`MemoryStore` 换成真的 ``Store``。

临时目录一律用 ``os.makedirs`` 建在仓库根下（本机沙箱会拒绝在新目录里继续写文件，
``tempfile.TemporaryDirectory`` 的 0o700 目录会在清理时抛 ``PermissionError``），
``tearDown`` 里 ``shutil.rmtree(..., ignore_errors=True)``。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import shutil
import struct
import threading
import unittest
import uuid
import zlib
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from atri_x.errors import ConflictError, NotFoundError
from atri_x.media import (
    IDPrefix,
    Config,
    FileDeliveryConfig,
    Input,
    LocalBackend,
    Manager,
    MediaConfig,
    MediaError,
    MediaTooLargeError,
    RecordingGenerationSink,
    Source,
    argument_media_ids,
    new_configured,
    new_s3_backend,
    valid_id,
)
from atri_x.media.imagecodec import Image, encode_png
from atri_x.ops.diskguard import Config as DiskGuardConfig
from atri_x.ops.diskguard import Guard as DiskGuard
from atri_x.safecontext import CancelledError
from atri_x.storage.ids import new_id
from atri_x.storage.models import (
    ChatMessage,
    HistoryMedia,
    Media,
    MediaOutput,
    MediaReference,
    Session,
)
from atri_x.storage.times import now as storage_now

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 所有 async 场景的硬超时：只要挂住就失败，而不是让测试卡死。
HARD_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# 内存版 storage.Store
# ---------------------------------------------------------------------------


class _MemoryMediaRepository:
    """``MediaRepository`` 的内存实现（复刻 media + media_history + media_outputs）。"""

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    # -- media --------------------------------------------------------------
    def get(self, id: str) -> Media:
        row = self._store.rows.get(id)
        if row is None:
            raise NotFoundError(f"media not found: {id}")
        return dataclasses.replace(row)

    def upsert(self, media: Media) -> None:
        self._store.upsert_media(media)

    def touch(self, id: str, now: datetime) -> None:
        self._store.touch(id, now)

    def claim_orphans(self, cutoff: datetime) -> list[Media]:
        return self._store.claim_orphans(cutoff)

    def delete_orphans(self, cutoff: datetime) -> list[Media]:
        return self._store.claim_orphans(cutoff)

    def finish_delete(self, id: str) -> None:
        self._store.finish_delete(id)

    def check_references(self) -> list[str]:
        return self._store.check_references()

    def recover_interrupted(self) -> None:
        self._store.recover_interrupted()

    # -- history ------------------------------------------------------------
    def save_history(self, item: HistoryMedia) -> None:
        self._store.save_history(item)

    def find_history(self, platform: str, scope_id: str, message_id: str) -> list[HistoryMedia]:
        rows = [
            row
            for row in self._store.history
            if row.platform == platform and row.scope_id == scope_id and row.message_id == message_id
        ]
        return sorted((dataclasses.replace(row) for row in rows), key=lambda row: row.media_index)

    def list_history(self, after_owner_id: str, limit: int) -> list[HistoryMedia]:
        rows = sorted(self._store.history, key=lambda row: row.owner_id)
        if after_owner_id:
            rows = [row for row in rows if row.owner_id > after_owner_id]
        return [dataclasses.replace(row) for row in rows[:limit]]

    def delete_history(self, owner_id: str) -> None:
        self._store.delete_history(owner_id)

    # -- outputs ------------------------------------------------------------
    def save_output(self, output: MediaOutput) -> None:
        self._store.save_output(output)

    def find_outputs(
        self, platform: str, scope_id: str, message_id: str, now: datetime
    ) -> list[MediaOutput]:
        rows = [
            row
            for row in self._store.outputs
            if row.platform == platform
            and row.scope_id == scope_id
            and row.message_id == message_id
            and row.expires_at is not None
            and row.expires_at > now
        ]
        return sorted((dataclasses.replace(row) for row in rows), key=lambda row: row.segment_index)

    def expire_outputs(self, now: datetime) -> None:
        self._store.expire_outputs(now)


class _MemoryMediaReferenceRepository:
    """``MediaReferenceRepository`` 的内存实现。"""

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store

    def add(self, reference: MediaReference) -> None:
        self._store.add_references([reference])

    def add_all(self, references: Any) -> None:
        self._store.add_references(list(references))

    def remove(self, reference: MediaReference) -> None:
        self._store.remove_reference(reference)

    def list_by_owner(self, owner_type: str, owner_id: str) -> list[MediaReference]:
        return [
            dataclasses.replace(ref)
            for ref in self._store.refs.values()
            if ref.owner_type == owner_type and ref.owner_id == owner_id
        ]

    def list_media_ids(self, media_id: str) -> list[MediaReference]:
        return [
            dataclasses.replace(ref)
            for ref in self._store.refs.values()
            if ref.media_id == media_id
        ]


class MemoryStore:
    """内存版 ``storage.Store``（只实现 media 相关仓储）。

    ``clock`` 是**触发器**用的时间源（Go 触发器里是 SQLite 的 ``strftime('now')``），
    与 ``Manager.now`` 相互独立 —— 这一点是"宽限期内不被清理 / 再次使用刷新时间"
    两类测试能成立的关键。
    """

    def __init__(self, clock: Any = None) -> None:
        self.rows: dict[str, Media] = {}
        self.orphaned_at: dict[str, datetime | None] = {}
        self.refs: dict[tuple[str, str, str, str], MediaReference] = {}
        self.history: list[HistoryMedia] = []
        self.outputs: list[MediaOutput] = []
        self.session_ids: set[str] = set()
        self.message_ids: set[str] = set()
        self.clock = clock or storage_now
        self._media = _MemoryMediaRepository(self)
        self._references = _MemoryMediaReferenceRepository(self)

    # -- Store 协议 ---------------------------------------------------------
    def media(self) -> _MemoryMediaRepository:
        return self._media

    def media_references(self) -> _MemoryMediaReferenceRepository:
        return self._references

    def sessions(self) -> "MemoryStore":
        return self

    def create_session(self, session: Session | None = None) -> str:
        session = session or Session()
        if not session.id:
            session.id = new_id()
        self.session_ids.add(session.id)
        return session.id

    def delete_session(self, session_id: str) -> None:
        """``release_session_media_references`` 触发器。"""
        self.session_ids.discard(session_id)
        for key in [key for key, ref in self.refs.items() if ref.session_id == session_id]:
            self._drop_reference(key)

    def delete_message(self, message_id: str) -> None:
        """``release_message_media_references`` 触发器。"""
        self.message_ids.discard(message_id)
        for key in [
            key
            for key, ref in self.refs.items()
            if ref.owner_id == message_id and ref.owner_type in ("message", "tool_result")
        ]:
            self._drop_reference(key)

    # -- media --------------------------------------------------------------
    def upsert_media(self, media: Media) -> None:
        existing = self.rows.get(media.id)
        row = dataclasses.replace(media)
        if existing is not None:
            if existing.deleting:
                # Go 的 Upsert 带 WHERE media.deleting=0，正在删除的行不会被覆盖。
                return
            row.created_at = existing.created_at
            if row.last_accessed_at is None:
                row.last_accessed_at = existing.last_accessed_at
            self.rows[media.id] = row
            return
        if row.created_at is None:
            row.created_at = self.clock()
        if row.last_accessed_at is None:
            row.last_accessed_at = row.created_at
        self.rows[media.id] = row
        # media_initial_orphan：orphaned_at = created_at
        self.orphaned_at[media.id] = row.created_at

    def touch(self, id: str, now: datetime) -> None:
        row = self.rows.get(id)
        if row is None or row.deleting:
            raise ConflictError("media unavailable or being deleted")
        row.last_accessed_at = now
        if self.orphaned_at.get(id) is not None:
            self.orphaned_at[id] = now

    def claim_orphans(self, cutoff: datetime) -> list[Media]:
        claimed: list[Media] = []
        for id, row in self.rows.items():
            if row.deleting:
                claimed.append(dataclasses.replace(row))
                continue
            orphaned = self.orphaned_at.get(id)
            if orphaned is None or orphaned > cutoff:
                continue
            if self._refs_for(id):
                continue
            row.deleting = True
            claimed.append(dataclasses.replace(row))
        return claimed

    def finish_delete(self, id: str) -> None:
        row = self.rows.get(id)
        if row is None or not row.deleting or self._refs_for(id):
            return
        del self.rows[id]
        self.orphaned_at.pop(id, None)

    def recover_interrupted(self) -> None:
        for key, ref in list(self.refs.items()):
            if ref.purpose == "temporary" and ref.owner_type in (
                "hook",
                "skill",
                "request",
                "output_send",
            ):
                self._drop_reference(key)

    # -- references ---------------------------------------------------------
    def _refs_for(self, media_id: str) -> list[MediaReference]:
        return [ref for ref in self.refs.values() if ref.media_id == media_id]

    def add_references(self, references: list[MediaReference]) -> None:
        """复刻"外键 + BEFORE INSERT 守卫 + 单事务"的三重语义。"""
        for ref in references:
            if ref.media_id not in self.rows:
                raise NotFoundError(f"media reference to unknown media: {ref.media_id}")
            if self.rows[ref.media_id].deleting:
                raise ConflictError("media is being deleted")
        for ref in references:
            key = (ref.media_id, ref.owner_type, ref.owner_id, ref.purpose)
            if key in self.refs:
                continue
            row = dataclasses.replace(ref)
            if row.created_at is None:
                row.created_at = self.clock()
            self.refs[key] = row
            # media_reference_added
            self.orphaned_at[ref.media_id] = None

    def remove_reference(self, reference: MediaReference) -> None:
        key = (reference.media_id, reference.owner_type, reference.owner_id, reference.purpose)
        self._drop_reference(key)

    def _drop_reference(self, key: tuple[str, str, str, str]) -> None:
        ref = self.refs.pop(key, None)
        if ref is None:
            return
        # media_reference_removed
        if not self._refs_for(ref.media_id):
            self.orphaned_at[ref.media_id] = self.clock()

    # -- history / outputs --------------------------------------------------
    def save_history(self, item: HistoryMedia) -> None:
        row = dataclasses.replace(item)
        if not row.owner_id:
            row.owner_id = new_id()
        self.history = [
            existing
            for existing in self.history
            if not (
                existing.platform == row.platform
                and existing.scope_id == row.scope_id
                and existing.message_id == row.message_id
                and existing.media_index == row.media_index
            )
        ]
        self.history.append(row)
        self.add_references(
            [
                MediaReference(
                    media_id=row.media_id,
                    owner_type="chat_history",
                    owner_id=row.owner_id,
                    purpose="content",
                    created_at=self.clock(),
                )
            ]
        )

    def delete_history(self, owner_id: str) -> None:
        self.history = [row for row in self.history if row.owner_id != owner_id]
        for key in [
            key
            for key, ref in self.refs.items()
            if ref.owner_type == "chat_history" and ref.owner_id == owner_id
        ]:
            self._drop_reference(key)

    def save_output(self, output: MediaOutput) -> None:
        if (
            not output.platform
            or not output.scope_id
            or not output.message_id
            or output.segment_index < 0
            or not output.kind
            or not output.media_id
        ):
            raise MediaError(
                "output media requires platform, scope, message ID, segment index, kind and media ID"
            )
        row = dataclasses.replace(output)
        if not row.owner_id:
            row.owner_id = new_id()
        self.outputs = [
            existing
            for existing in self.outputs
            if not (
                existing.platform == row.platform
                and existing.scope_id == row.scope_id
                and existing.message_id == row.message_id
                and existing.segment_index == row.segment_index
            )
        ]
        self.outputs.append(row)
        self.add_references(
            [
                MediaReference(
                    media_id=row.media_id,
                    owner_type="output",
                    owner_id=row.owner_id,
                    purpose="cache",
                    created_at=self.clock(),
                )
            ]
        )

    def expire_outputs(self, now: datetime) -> None:
        expired = [
            row for row in self.outputs if row.expires_at is not None and row.expires_at <= now
        ]
        for row in expired:
            self.outputs.remove(row)
            for key in [
                key
                for key, ref in self.refs.items()
                if ref.owner_type == "output" and ref.owner_id == row.owner_id
            ]:
                self._drop_reference(key)

    # -- 一致性检查 ---------------------------------------------------------
    def check_references(self) -> list[str]:
        issues: list[str] = []
        for row in self.history:
            if not self._has_ref("chat_history", row.owner_id, row.media_id, "content"):
                issues.append("missing history reference: " + row.owner_id)
        for out in self.outputs:
            if not self._has_ref("output", out.owner_id, out.media_id, ""):
                issues.append("missing output reference: " + out.owner_id)
        for ref in self.refs.values():
            if ref.owner_type in ("message", "tool_result"):
                if ref.owner_id not in self.message_ids:
                    issues.append(f"dangling owner reference: {ref.owner_type}:{ref.owner_id}")
            elif ref.owner_type == "chat_history":
                if not any(
                    row.owner_id == ref.owner_id and row.media_id == ref.media_id
                    for row in self.history
                ):
                    issues.append(f"dangling owner reference: chat_history:{ref.owner_id}")
            elif ref.owner_type == "output":
                if not any(row.owner_id == ref.owner_id for row in self.outputs):
                    issues.append(f"dangling owner reference: output:{ref.owner_id}")
            elif ref.owner_type == "session_tool":
                if (
                    not ref.session_id
                    or ref.session_id != ref.owner_id
                    or ref.owner_id not in self.session_ids
                ):
                    issues.append(f"dangling owner reference: session_tool:{ref.owner_id}")
        return issues

    def _has_ref(self, owner_type: str, owner_id: str, media_id: str, purpose: str) -> bool:
        for ref in self.refs.values():
            if ref.owner_type != owner_type or ref.owner_id != owner_id:
                continue
            if ref.media_id != media_id:
                continue
            if purpose and ref.purpose != purpose:
                continue
            return True
        return False


# ---------------------------------------------------------------------------
# 本地假服务端
# ---------------------------------------------------------------------------


class _OriginHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        server: "OriginServer" = self.server.origin  # type: ignore[attr-defined]
        server.requests.append(self.path)
        if server.stall and self.path.startswith("/stall"):
            # 真实超时：头发完就挂住，只有客户端 socket 超时才会解开。
            self.send_response(200)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.flush()
            server.stalled.set()
            server.release.wait(timeout=30.0)
            self.close_connection = True
            return
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = server.body
        if self.path.startswith("/chunked"):
            # 不带 Content-Length（HTTP/1.0 风格的 EOF 结尾），复刻 Go 里
            # httptest 先 Flush 头再写正文导致 ContentLength = -1 的场景。
            self.send_response(200)
            if server.content_type:
                self.send_header("Content-Type", server.content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        self.send_response(200)
        if server.content_type:
            self.send_header("Content-Type", server.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OriginServer:
    """假的媒体来源 HTTP 服务端（真实 ``ThreadingHTTPServer``，不是 mock）。"""

    def __init__(
        self, body: bytes = b"", content_type: str = "", stall: bool = False
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.stall = stall
        self.requests: list[str] = []
        self.stalled = threading.Event()
        self.release = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
        self.httpd.origin = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5.0)


class _S3Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        return

    def _key(self) -> tuple[str, str]:
        path = self.path.split("?", 1)[0]
        parts = [part for part in path.split("/") if part]
        if not parts:
            return "", ""
        return parts[0], "/".join(parts[1:])

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_PUT(self) -> None:  # noqa: N802
        server: "FakeS3Server" = self.server.s3  # type: ignore[attr-defined]
        bucket, key = self._key()
        body = self._read_body()
        server.objects[(bucket, key)] = body
        server.calls.append(f"PUT {key}")
        self._respond(200)

    def do_GET(self) -> None:  # noqa: N802
        server: "FakeS3Server" = self.server.s3  # type: ignore[attr-defined]
        bucket, key = self._key()
        server.calls.append(f"GET {key}")
        body = server.objects.get((bucket, key))
        if body is None:
            self._respond(404, b"<Error><Code>NoSuchKey</Code></Error>")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        server: "FakeS3Server" = self.server.s3  # type: ignore[attr-defined]
        bucket, key = self._key()
        server.calls.append(f"HEAD {key}")
        body = server.objects.get((bucket, key))
        if body is None:
            self._respond(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_DELETE(self) -> None:  # noqa: N802
        server: "FakeS3Server" = self.server.s3  # type: ignore[attr-defined]
        bucket, key = self._key()
        server.calls.append(f"DELETE {key}")
        server.objects.pop((bucket, key), None)
        self._respond(204)


class FakeS3Server:
    """最小 S3 兼容服务端（path-style，忽略签名）。

    用真实 HTTP 而不是 mock —— 这样 ``S3Client`` 的 SigV4 组装、URL 拼装、
    PUT/GET/DELETE 与预签名下载路径都是真的在跑。
    """

    def __init__(self, bucket: str = "bucket") -> None:
        self.bucket = bucket
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _S3Handler)
        self.httpd.s3 = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# fixture 构造
# ---------------------------------------------------------------------------


def make_image(width: int, height: int, noise: bool = False) -> Image:
    """造一张 RGB 图片（与 Go 测试的 ``testPNG`` 同构）。"""
    data = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            base = (y * width + x) * 3
            if noise:
                data[base] = (x * 7 + y * 13) % 256
                data[base + 1] = (x * 29 + y * 3) % 256
                data[base + 2] = (x * 11 + y * 31) % 256
            else:
                data[base] = (x * 3) % 256
                data[base + 1] = (y * 3) % 256
                data[base + 2] = (x + y) % 256
    return Image(width, height, "RGB", data)


def make_png(width: int, height: int, noise: bool = False) -> bytes:
    """造一张 PNG（对应 Go 测试的 ``testPNG``）。"""
    return encode_png(make_image(width, height, noise))


def png_with_text(payload: bytes, keyword: str = b"parameters", text: bytes = b"steps: 20") -> bytes:
    """在 IHDR 之后插入一个 PNG ``tEXt`` 块。

    生成参数（A1111/NovelAI 等）就放在这种文本块里；重编码成 JPEG 会把它销毁，
    因此它是"采集必须发生在压缩前"这条断言最直接的探针。
    """
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("fixture is not a PNG")
    chunk = keyword + b"\x00" + text
    block = (
        struct.pack(">I", len(chunk))
        + b"tEXt"
        + chunk
        + struct.pack(">I", zlib.crc32(b"tEXt" + chunk) & 0xFFFFFFFF)
    )
    return payload[:33] + block + payload[33:]


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------


class _WorkspaceMixin:
    """在仓库根下建临时目录（不用 tempfile：沙箱会拒绝对新目录继续写入）。"""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self._roots: list[str] = []
        self.work_root = self.make_dir("work")

    def tearDown(self) -> None:
        for path in reversed(self._roots):
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()  # type: ignore[misc]

    def make_dir(self, name: str) -> str:
        path = os.path.join(REPO_ROOT, f".media-test-{name}-{uuid.uuid4().hex}")
        os.makedirs(path, exist_ok=True)
        self._roots.append(path)
        return path

    def make_manager(
        self,
        store: MemoryStore | None = None,
        *,
        config: Config | None = None,
        media: MediaConfig | None = None,
        now: Any = None,
    ) -> tuple[Manager, MemoryStore, str]:
        root = self.make_dir("media")
        store = store or MemoryStore()
        manager = Manager(store, root, LocalBackend(root), config=config, now=now)
        if media is not None:
            manager.media = media
        return manager, store, root


class MediaTestCase(_WorkspaceMixin, unittest.TestCase):
    """同步测试基类。"""


class AsyncMediaTestCase(_WorkspaceMixin, unittest.IsolatedAsyncioTestCase):
    """异步测试基类（``ctx`` 走真实 ``CancelToken``）。"""

    async def guard(self, coro: Any, timeout: float = HARD_TIMEOUT) -> Any:
        """给可能挂住的 await 加硬超时。"""
        return await asyncio.wait_for(coro, timeout)


# ---------------------------------------------------------------------------
# 导入 / 去重 / 读取 / 导出
# ---------------------------------------------------------------------------


class TestImportAndDedup(AsyncMediaTestCase):
    """``manager_test.go`` 的 LocalManagerImportReadExportAndDeduplicatesBinary。"""

    async def test_dedup_read_export(self) -> None:
        manager, store, root = self.make_manager()
        data = bytes([0, 0xFF, 0x80, 0x01, 0x7F])
        first = await self.guard(manager.import_bytes(None, data, Input(name="sample.bin")))
        self.assertTrue(valid_id(first.id))
        self.assertEqual(first.size, len(data))
        self.assertEqual(first.mime_type, "application/octet-stream")
        self.assertEqual(first.backend, "local")

        second = await self.guard(manager.import_bytes(None, data, Input(name="other.bin")))
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.name, "sample.bin", "去重时保留首条记录的名称")

        got, _media = await self.guard(manager.read(None, first.id))
        self.assertEqual(got, data)

        # 同内容只落一个本地文件（不是"每次导入一个副本"）。
        files = [
            os.path.join(base, name)
            for base, _dirs, names in os.walk(root)
            for name in names
        ]
        self.assertEqual(len(files), 1, files)
        self.assertEqual(first.id, IDPrefix + hashlib.sha256(data).hexdigest())

        destination = os.path.join(self.make_dir("export"), "export.bin")
        await self.guard(manager.export(None, first.id, destination))
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), data)

    async def test_import_reader_accepts_unknown_size(self) -> None:
        manager, _store, _root = self.make_manager()
        item = await self.guard(
            manager.import_reader(None, make_png(8, 8), -1, Input(name="x.png"))
        )
        self.assertEqual(item.mime_type, "image/png")

    async def test_import_rejects_over_limit(self) -> None:
        manager, _store, _root = self.make_manager()
        manager.max_import_bytes = 3
        with self.assertRaises(MediaError):
            await self.guard(manager.import_reader(None, b"1234", -1, Input()))


class TestImportWithDiskGuard(AsyncMediaTestCase):
    """磁盘守卫必须接受"只承载值的 ctx"（入站平台路径的真实形状）。

    真机事故（1.0.0 群聊发图全部 ``[媒体不可用；…]``）：媒体中心在
    ``app/runtime.py`` 里**总是**挂一个磁盘守卫；入站平台消息的 ctx 是
    ``ctx_with_value`` 落成的普通 ``dict``（没有 ``raise_if_cancelled``），旧实现
    无条件调用该方法抛 ``AttributeError``，又被失败降级吞成"媒体不可用"。
    """

    def _guarded(self) -> tuple[Manager, MemoryStore, str]:
        manager, store, root = self.make_manager()
        manager.guard = DiskGuard(
            root, DiskGuardConfig(cache_ttl=60.0), lambda _path: (1000, 500)
        )
        return manager, store, root

    async def test_import_with_mapping_context_succeeds(self) -> None:
        manager, _store, _root = self._guarded()
        item = await self.guard(
            manager.import_bytes({"platform": "qqonebot"}, b"guarded", Input(name="a.bin"))
        )
        self.assertTrue(valid_id(item.id))

    async def test_import_with_platform_run_flag_succeeds(self) -> None:
        """``app/platforms.py::_RunFlag`` 也没有 ``raise_if_cancelled``。"""
        manager, _store, _root = self._guarded()

        class RunFlag:
            cancelled = False

            def is_set(self) -> bool:
                return False

        item = await self.guard(
            manager.import_bytes(RunFlag(), b"guarded", Input(name="b.bin"))
        )
        self.assertTrue(valid_id(item.id))

    async def test_cancelled_context_still_blocks_import(self) -> None:
        manager, _store, _root = self._guarded()
        from atri_x.safecontext import with_cancel

        token = with_cancel(None)
        token.cancel("test")
        with self.assertRaises(CancelledError):
            await self.guard(manager.import_bytes(token, b"guarded", Input(name="c.bin")))


class TestValidID(MediaTestCase):
    """``manager_test.go`` 的 TestValidID。"""

    def test_valid_id(self) -> None:
        valid = IDPrefix + "0123456789abcdef" * 4
        for value in (
            valid,
            "",
            "media:short",
            IDPrefix + "0123456789abcdef" * 3 + "0123456789abcdeg",
        ):
            self.assertEqual(valid_id(value), value == valid, value)


class TestReadLimited(AsyncMediaTestCase):
    """``manager_test.go`` 的 TestReadLimited*。"""

    async def test_read_limited_budget(self) -> None:
        manager, _store, _root = self.make_manager()
        item = await self.guard(
            manager.import_bytes(None, b"0123456789abcdef", Input(name="small.bin"))
        )
        with self.assertRaises(MediaTooLargeError):
            await self.guard(manager.read_limited(None, item.id, 8))
        data, _media = await self.guard(manager.read_limited(None, item.id, 1024))
        self.assertEqual(data, b"0123456789abcdef")
        # 恰好等于上限必须通过（不是 off-by-one）。
        data, _media = await self.guard(manager.read_limited(None, item.id, 16))
        self.assertEqual(len(data), 16)

    async def test_stream_guard_rejects_underreported_metadata(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(
            manager.import_bytes(None, b"0123456789abcdef", Input(name="small.bin"))
        )
        stale = store.media().get(item.id)
        stale.size = 1
        store.media().upsert(stale)
        with self.assertRaises(MediaTooLargeError):
            await self.guard(manager.read_limited(None, item.id, 8))


class TestMetadataAndSanitize(AsyncMediaTestCase):
    """``manager_test.go`` 的 TestImportSanitizesMediaMetadataAndReturnedCopies。"""

    CASES = (
        (
            "credential URL",
            dict(
                name="https://user:password@example.com/folder/photo.png?rkey=name-secret#part",
                source=dict(
                    url="https://user:password@example.com/folder/photo.png?rkey=url-secret#part",
                    file_id="https://example.com/photo.png?rkey=id-secret",
                ),
            ),
            "photo.png",
            "",
            "",
        ),
        (
            "signed URL",
            dict(
                name="https://example.com/folder/report.pdf?X-Amz-Signature=name-secret",
                source=dict(
                    url="https://example.com/folder/report.pdf?X-Amz-Signature=url-secret",
                    file_id="C:\\downloads\\report.pdf",
                ),
            ),
            "report.pdf",
            "",
            "",
        ),
        (
            "telegram token path",
            dict(
                name="C:\\private\\image.jpg",
                source=dict(
                    url="https://api.telegram.org/file/bot123456:token/photos/image.jpg",
                    file_id="opaque-platform-id",
                ),
            ),
            "image.jpg",
            "",
            "opaque-platform-id",
        ),
        (
            "safe source",
            dict(
                name="/tmp/archive.bin",
                source=dict(
                    url="https://cdn.example.com/files/archive.bin",
                    file_id="opaque_123-ABC",
                ),
            ),
            "archive.bin",
            "https://cdn.example.com/files/archive.bin",
            "opaque_123-ABC",
        ),
        ("inline name", dict(name="data:image/png;base64,c2VjcmV0"), "file", "", ""),
        ("base64 name", dict(name="base64:c2VjcmV0"), "file", "", ""),
    )

    async def test_sanitized_metadata_and_returned_copies(self) -> None:
        manager, store, _root = self.make_manager()
        for index, (label, raw, want_name, want_url, want_file_id) in enumerate(self.CASES):
            with self.subTest(label):
                source = raw.get("source") or {}
                item = await self.guard(
                    manager.import_bytes(
                        None,
                        bytes([index, 1, 2, 3]),
                        Input(
                            name=raw["name"],
                            source=Source(
                                url=source.get("url", ""), file_id=source.get("file_id", "")
                            ),
                        ),
                    )
                )
                self.assertEqual(item.name, want_name)
                self.assertEqual(item.source_url, want_url)
                self.assertEqual(item.source_file_id, want_file_id)
                stored = store.media().get(item.id)
                self.assertEqual(stored.name, want_name)
                self.assertEqual(stored.source_url, want_url)
                self.assertEqual(stored.source_file_id, want_file_id)

    async def test_legacy_records_are_sanitized_on_read_only(self) -> None:
        manager, store, _root = self.make_manager()
        legacy = await self.guard(manager.import_bytes(None, b"legacy", Input(name="safe.png")))
        row = store.media().get(legacy.id)
        row.name = "https://example.com/private/legacy.png?rkey=legacy-name-secret"
        row.source_url = "https://user:pass@example.com/private/legacy.png#secret"
        row.source_file_id = "https://example.com/private/legacy.png?rkey=legacy-id-secret"
        store.rows[legacy.id] = row

        deduplicated = await self.guard(
            manager.import_bytes(None, b"legacy", Input(name="ignored.png"))
        )
        self.assertEqual(deduplicated.name, "legacy.png")
        self.assertEqual(deduplicated.source_url, "")
        self.assertEqual(deduplicated.source_file_id, "")

        metadata = await self.guard(manager.metadata(None, legacy.id))
        self.assertEqual(metadata.name, "legacy.png")
        self.assertEqual(metadata.source_url, "")
        self.assertEqual(metadata.source_file_id, "")

        # 旧记录按需清洗返回副本，**不**批量回写。
        stored = store.media().get(legacy.id)
        self.assertIn("secret", stored.name + stored.source_url + stored.source_file_id)

    async def test_metadata_rejects_invalid_id(self) -> None:
        manager, _store, _root = self.make_manager()
        with self.assertRaises(MediaError):
            await self.guard(manager.metadata(None, "media:short"))


class TestImportURLSanitizesSourceButDownloadsOriginal(AsyncMediaTestCase):
    """``resolver_test.go`` 的 TestImportURLUnknownSizeAndLimits（来源清洗 + 下载原参）。"""

    async def test_signed_url_download_uses_raw_query(self) -> None:
        server = OriginServer(body=bytes([0, 255, 128, 1]), content_type="image/png")
        self.addCleanup(server.close)
        manager, _store, _root = self.make_manager()
        item = await self.guard(
            manager.import_url(None, server.url + "/a.png?X-Amz-Signature=secret", Input())
        )
        self.assertEqual(item.size, 4)
        self.assertEqual(item.source_url, "", "带 query 的来源 URL 不落库")
        self.assertEqual(item.mime_type, "image/png")
        self.assertEqual(
            server.requests, ["/a.png?X-Amz-Signature=secret"], "下载必须用清洗前的参数"
        )

    async def test_import_url_errors(self) -> None:
        server = OriginServer(body=b"1234", content_type="application/octet-stream")
        self.addCleanup(server.close)
        manager, _store, _root = self.make_manager()
        manager.max_import_bytes = 3
        for source in (server.url + "/a.png", server.url + "/missing", "file:///a"):
            with self.subTest(source):
                with self.assertRaises(MediaError):
                    await self.guard(manager.import_url(None, source, Input()))


class TestImportFile(AsyncMediaTestCase):
    async def test_import_file_defaults_name_and_type(self) -> None:
        directory = self.make_dir("source")
        path = os.path.join(directory, "photo.png")
        data = make_png(8, 8)
        with open(path, "wb") as handle:
            handle.write(data)
        manager, _store, _root = self.make_manager()
        item = await self.guard(manager.import_file(None, path, Input()))
        self.assertEqual(item.name, "photo.png")
        self.assertEqual(item.mime_type, "image/png")
        self.assertEqual(item.size, len(data))

    async def test_import_file_rejects_directory(self) -> None:
        manager, _store, _root = self.make_manager()
        with self.assertRaises(MediaError):
            await self.guard(manager.import_file(None, self.make_dir("dir"), Input()))


# ---------------------------------------------------------------------------
# 生成参数采集
# ---------------------------------------------------------------------------


class TestGenerationCapture(AsyncMediaTestCase):
    """``generation_test.go`` 的六个用例。"""

    def _manager(self, sink: RecordingGenerationSink, cfg: MediaConfig | None = None) -> Manager:
        manager, _store, _root = self.make_manager(media=cfg)
        manager.generation = sink
        return manager

    async def test_captures_original_before_transcode(self) -> None:
        """本接缝存在的理由：JPEG 重编码之后原图元数据就没了。"""
        sink = RecordingGenerationSink()
        manager = self._manager(
            sink, MediaConfig(llm_image_compression_threshold_bytes=1 << 20, llm_image_max_length=128)
        )
        original = png_with_text(make_png(300, 300))
        item = await self.guard(
            manager.import_bytes(None, original, Input(name="big.png", mime_type="image/png"))
        )
        self.assertEqual(len(sink.captures), 1)
        capture = sink.captures[0]
        self.assertTrue(capture.transcoded)
        self.assertEqual(capture.original, original, "采集的必须是压缩前的原始字节（含 tEXt）")
        self.assertEqual(capture.source_mime, "image/png")
        self.assertEqual(capture.stored_mime, "image/jpeg")
        self.assertEqual(capture.source_size, len(original))
        self.assertEqual(capture.stored_size, item.size)
        self.assertEqual(capture.media_id, item.id)
        self.assertEqual(capture.source_hash, hashlib.sha256(original).hexdigest())
        self.assertEqual(capture.source_name, "big.png")

        stored, _media = await self.guard(manager.read(None, item.id))
        self.assertNotEqual(stored, original)
        self.assertNotIn(b"tEXt", stored, "压缩后的对象不能再带着原文块")
        # ID 在哈希之后才产生，且是**已存字节**的哈希。
        self.assertEqual(item.id, IDPrefix + hashlib.sha256(stored).hexdigest())

    async def test_untranscoded_image_is_captured_verbatim(self) -> None:
        sink = RecordingGenerationSink()
        manager = self._manager(sink)
        original = make_png(32, 32)
        item = await self.guard(
            manager.import_bytes(None, original, Input(name="small.png", mime_type="image/png"))
        )
        capture = sink.captures[0]
        self.assertFalse(capture.transcoded)
        self.assertEqual(capture.original, original)
        self.assertEqual(capture.stored_size, capture.source_size)
        stored, _media = await self.guard(manager.read(None, item.id))
        self.assertEqual(stored, original)

    async def test_non_image_skips_capture(self) -> None:
        sink = RecordingGenerationSink()
        manager = self._manager(sink)
        await self.guard(
            manager.import_bytes(None, b"just some text", Input(name="notes.txt", mime_type="text/plain"))
        )
        self.assertEqual(sink.captures, [])

    async def test_deduplicated_reimport_captures_again(self) -> None:
        sink = RecordingGenerationSink()
        manager = self._manager(sink)
        original = make_png(16, 16)
        spec = Input(name="twice.png", mime_type="image/png")
        first = await self.guard(manager.import_bytes(None, original, spec))
        second = await self.guard(manager.import_bytes(None, original, spec))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(sink.captures), 2)
        self.assertEqual(sink.captures[1].media_id, sink.captures[0].media_id)

    async def test_capture_failure_does_not_fail_import(self) -> None:
        sink = RecordingGenerationSink(error=MediaError("record store is corrupt"))
        manager = self._manager(sink)
        item = await self.guard(
            manager.import_bytes(None, make_png(8, 8), Input(name="kept.png", mime_type="image/png"))
        )
        self.assertTrue(valid_id(item.id))

    async def test_nil_sink_is_safe(self) -> None:
        manager, _store, _root = self.make_manager()
        await self.guard(
            manager.import_bytes(None, make_png(8, 8), Input(name="plain.png", mime_type="image/png"))
        )


class TestGenerationArchiveRoundTrip(AsyncMediaTestCase):
    """入库留档 → 转码后回读：配了 ``metadata_store`` 参数就不该丢。

    这是"图转提示词在群里总是取不到参数"的端到端回归：真实媒体管线把超大 PNG
    转码成 JPEG（销毁 tEXt），留档必须在那之前拿到原图参数，并且 ``artist_extract``
    / ``image_to_prompt`` 共用的 :class:`atri_x.artist.Service` 能按媒体 ID 读回来。
    """

    async def test_archived_parameters_survive_transcode(self) -> None:
        from atri_x.artist import (
            MetadataOriginArchive,
            Service as ArtistService,
            new_generation_store,
        )
        from atri_x.artist.metadata import extract_metadata

        store = new_generation_store(
            os.path.join(self.make_dir("archive"), "image_metadata.json")
        )
        manager, _store, _root = self.make_manager(
            media=MediaConfig(
                llm_image_compression_threshold_bytes=1 << 20,
                llm_image_max_length=128,
            )
        )
        manager.generation = store
        original = png_with_text(
            make_png(300, 300),
            text=(
                b"masterpiece, 1girl, solo\n"
                b"Negative prompt: lowres, bad anatomy\n"
                b"Steps: 20, Sampler: Euler a, CFG scale: 7"
            ),
        )
        item = await self.guard(
            manager.import_bytes(
                None, original, Input(name="big.png", mime_type="image/png")
            )
        )
        stored, _media = await self.guard(manager.read(None, item.id))
        self.assertNotEqual(stored, original)
        self.assertNotIn(b"tEXt", stored, "转码后对象里不应再有 PNG 文本块")
        _embedded, found = extract_metadata(stored)
        self.assertFalse(found, "JPEG 本体已经读不到生成参数")
        # 工具侧（artist_extract / image_to_prompt 共用）按媒体 ID 回读留档。
        service = ArtistService(None)
        service.generations = store
        meta = service.extract_image_metadata(item.id, stored)
        self.assertTrue(meta.found)
        self.assertEqual(meta.origin, MetadataOriginArchive)
        self.assertIn("masterpiece", meta.metadata.positive)
        self.assertIn("lowres", meta.metadata.negative)
        self.assertEqual(meta.metadata.parameters.get("steps"), "20")


class TestDeleteForgetsGeneration(AsyncMediaTestCase):
    """``generation_test.go`` 的 TestDeleteForgetsGenerationRecord。"""

    async def test_cleanup_forgets_record(self) -> None:
        sink = RecordingGenerationSink()
        manager, _store, _root = self.make_manager()
        manager.generation = sink
        item = await self.guard(
            manager.import_bytes(None, make_png(8, 8), Input(name="gone.png", mime_type="image/png"))
        )
        real = storage_now()
        manager.now = lambda: real + timedelta(hours=2)
        await self.guard(manager.cleanup(None))
        self.assertEqual(sink.forgot, [item.id])


# ---------------------------------------------------------------------------
# 引用记账
# ---------------------------------------------------------------------------


class TestArgumentMediaIDs(MediaTestCase):
    """``manager_test.go`` 的 TestArgumentMediaIDsUsesExactJSONValues。"""

    def test_exact_json_values(self) -> None:
        first = IDPrefix + "a" * 64
        second = IDPrefix + "b" * 64
        arguments = (
            '{"send_media":"%s","images":["%s","%s"],"%s":"ignored","prompt":"prefix %s"}'
            % (second, first, first, second, first)
        )
        self.assertEqual(argument_media_ids(arguments), [first, second])
        self.assertEqual(argument_media_ids('{"source":"media:short"}'), [])
        self.assertEqual(argument_media_ids('{"source":'), [])


class TestRetainSessionToolArguments(AsyncMediaTestCase):
    """``manager_test.go`` 的 TestRetainSessionToolArgumentsIsAtomicAndIdempotent。"""

    async def test_atomic_idempotent_and_session_scoped(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"tool input", Input(name="input.bin")))
        session_id = store.create_session()

        arguments = '{"source":"%s","again":"%s"}' % (item.id, item.id)
        for _ in range(2):
            manager.retain_session_tool_arguments(session_id, arguments)
        refs = store.media_references().list_by_owner("session_tool", session_id)
        self.assertEqual(len(refs), 1, "同一 Session/媒体幂等")
        self.assertEqual(refs[0].media_id, item.id)
        self.assertEqual(refs[0].session_id, session_id)

        other = store.create_session()
        missing = IDPrefix + "f" * 64
        with self.assertRaises(NotFoundError):
            manager.retain_session_tool_arguments(other, '["%s","%s"]' % (item.id, missing))
        self.assertEqual(
            store.media_references().list_by_owner("session_tool", other), [], "整批回滚"
        )

        store.delete_session(session_id)
        self.assertEqual(store.media_references().list_by_owner("session_tool", session_id), [])

    def test_requires_session_id(self) -> None:
        manager, _store, _root = self.make_manager()
        with self.assertRaises(MediaError):
            manager.retain_session_tool_arguments("", '{"source":"x"}')


class TestHold(AsyncMediaTestCase):
    async def test_hold_releases_and_keeps_object_touchable(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"reading", Input()))
        release = manager.hold(item.id)
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 1)
        self.assertIsNone(store.orphaned_at[item.id], "有引用时 orphaned_at 必须为空")
        release()
        release()  # 幂等
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 0)
        self.assertIsNotNone(store.orphaned_at[item.id])

    async def test_open_holds_until_reader_closes(self) -> None:
        manager, store, _root = self.make_manager()
        item = await self.guard(manager.import_bytes(None, b"reading", Input()))
        reader, _media = await self.guard(manager.open(None, item.id))
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 1)
        reader.close()
        reader.close()
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 0)

    async def test_add_reference_validates_media(self) -> None:
        manager, _store, _root = self.make_manager()
        with self.assertRaises(MediaError):
            manager.add_reference(MediaReference(media_id="media:short"))
        with self.assertRaises(NotFoundError):
            manager.add_reference(MediaReference(media_id=IDPrefix + "0" * 64))


# ---------------------------------------------------------------------------
# 历史关联与对账
# ---------------------------------------------------------------------------


class _HistoryResolver:
    """对应 Go ``historyResolver``：把 PlatformFileID 当字节。"""

    def __init__(self) -> None:
        self.calls = 0

    async def resolve_media(self, ctx: Any, segment: Any, max_bytes: int) -> Any:
        from atri_x.delivery.delivery import Source

        self.calls += 1
        return Source(data=segment.platform_file_id.encode(), mime_type="image/png")


class TestHistoryMedia(AsyncMediaTestCase):
    """``platform_test.go`` 的 TestGetHistoryMediaReusesAssociation。"""

    async def test_history_media_reuses_association(self) -> None:
        manager, _store, _root = self.make_manager()
        from atri_x.platform.media import marshal_chat_segments
        from atri_x.platform.platform import MessageSegment

        row = ChatMessage(
            id="h",
            platform="p",
            platform_scope_id="s",
            platform_message_id="1",
            segments=marshal_chat_segments(
                [
                    MessageSegment(type="text", text="hi"),
                    MessageSegment(type="image", platform_file_id="one"),
                    MessageSegment(type="file", platform_file_id="two"),
                ]
            ),
        )
        resolver = _HistoryResolver()
        ids = await self.guard(manager.history_ids(None, row))
        self.assertEqual(ids, {})
        self.assertEqual(resolver.calls, 0)

        first = await self.guard(manager.get_history_media(None, row, 1, resolver))
        again = await self.guard(manager.get_history_media(None, row, 1, resolver))
        self.assertEqual(first.id, again.id)
        self.assertEqual(resolver.calls, 1, "重复位置必须复用关联，不再下载")

        second = await self.guard(manager.get_history_media(None, row, 2, resolver))
        self.assertNotEqual(second.id, first.id)

        for index in (0, 3):
            with self.assertRaises(MediaError):
                await self.guard(manager.get_history_media(None, row, index, resolver))

        ids = await self.guard(manager.history_ids(None, row))
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[1], first.id)
        self.assertEqual(ids[2], second.id)

        row.platform_scope_id = "other"
        self.assertEqual(await self.guard(manager.history_ids(None, row)), {}, "scope 不能串")


class _FakeHistoryRepository:
    """假 ChatHistoryRepository（内存字典）。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], ChatMessage] = {}
        self.fail = False
        self.appended: list[ChatMessage] = []

    def append(self, message: ChatMessage) -> None:
        if not message.id:
            message.id = new_id()
        if message.created_at is None:
            message.created_at = storage_now()
        self.rows[(message.platform, message.platform_scope_id, message.platform_message_id)] = (
            dataclasses.replace(message)
        )
        self.appended.append(message)

    def get_by_platform_message(
        self, platform: str, scope_id: str, platform_message_id: str
    ) -> ChatMessage:
        if self.fail:
            raise MediaError("history offline")
        row = self.rows.get((platform, scope_id, platform_message_id))
        if row is None:
            raise NotFoundError("history row not found")
        return dataclasses.replace(row)

    def delete_before(self, cutoff: datetime) -> int:
        doomed = [
            key for key, row in self.rows.items() if row.created_at is not None and row.created_at < cutoff
        ]
        for key in doomed:
            del self.rows[key]
        return len(doomed)


class TestHistoryReconciliation(AsyncMediaTestCase):
    """``history_test.go`` 的 TestHistoryReconciliationAfterInterruptedDeletion。"""

    async def test_reconciliation_after_interrupted_deletion(self) -> None:
        manager, store, _root = self.make_manager()
        history = _FakeHistoryRepository()
        manager.history = history
        message = ChatMessage(
            platform="p",
            platform_scope_id="s",
            platform_message_id="1",
            sender_id="u",
            created_at=storage_now() - timedelta(hours=24),
        )
        history.append(message)
        item = await self.guard(manager.import_bytes(None, b"media", Input(name="image.png")))
        await self.guard(
            manager.associate_history(None, message, 1, "image", item.id)
        )
        await self.guard(manager.reconcile_history(None))
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 1)

        history.fail = True
        with self.assertRaises(Exception):
            await self.guard(manager.cleanup(None))
        self.assertEqual(
            len(store.media_references().list_media_ids(item.id)), 1, "历史库故障不释放引用"
        )

        history.fail = False
        history.delete_before(storage_now())
        # 模拟"历史事务已提交、主库清理前被打断"。
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 1)
        await self.guard(manager.reconcile_history(None))
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 0)
        await self.guard(manager.cleanup(None))
        self.assertTrue(valid_id((await self.guard(manager.metadata(None, item.id))).id))

        # 复用平台消息 ID 不能让新行继承旧关联。
        message_id = message.platform_message_id
        await self.guard(manager.associate_history(None, message, 1, "image", item.id))
        replacement = ChatMessage(
            platform="p",
            platform_scope_id="s",
            platform_message_id=message_id,
            sender_id="u",
            created_at=storage_now(),
        )
        history.append(replacement)
        await self.guard(manager.reconcile_history(None))
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 0)


# ---------------------------------------------------------------------------
# S3 后端
# ---------------------------------------------------------------------------


class TestS3Backend(AsyncMediaTestCase):
    """``s3_test.go`` 的三个用例 + 一条真实往返。"""

    def test_requires_credentials(self) -> None:
        with self.assertRaises(MediaError):
            new_s3_backend(FileDeliveryConfig())
        with self.assertRaises(MediaError):
            new_s3_backend(FileDeliveryConfig(), {"access_key": "", "secret_key": ""})

    async def test_new_configured_defers_remote_errors(self) -> None:
        store = MemoryStore()
        root = self.make_dir("media")
        new_configured(store, root, FileDeliveryConfig(backend="base64"), None)
        for mode in ("s3", "hybrid"):
            with self.subTest(mode):
                manager = new_configured(store, root, FileDeliveryConfig(backend=mode), None)
                self.assertIsNone(manager.remote, "远端后端不能在启动时初始化")
                with self.assertRaises(MediaError):
                    await self.guard(manager.remote_backend(None))

    def test_rejects_unknown_backend(self) -> None:
        store = MemoryStore()
        with self.assertRaises(MediaError):
            new_configured(store, self.make_dir("media"), FileDeliveryConfig(backend="ftp"), None)

    async def test_uses_compatible_endpoint_path(self) -> None:
        server = FakeS3Server()
        self.addCleanup(server.close)
        backend = new_s3_backend(
            FileDeliveryConfig(
                s3_endpoint=server.url,
                s3_region="auto",
                s3_bucket="bucket",
            ),
            {"access_key": "access", "secret_key": "secret"},
        )
        id = IDPrefix + "0123456789abcdef" * 4
        await self.guard(backend.put(None, id, b"", 0, "application/octet-stream"))
        self.assertIn(
            "PUT media/0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            server.calls,
        )

    async def test_round_trip_put_open_remove_presign(self) -> None:
        server = FakeS3Server()
        self.addCleanup(server.close)
        backend = new_s3_backend(
            FileDeliveryConfig(s3_endpoint=server.url, s3_bucket="bucket"),
            {"access_key": "access", "secret_key": "secret"},
        )
        item = Media(id=IDPrefix + "ab" * 32, mime_type="text/plain", backend="s3")
        key = await self.guard(backend.put(None, item.id, b"hello s3", 8, "text/plain"))
        self.assertEqual(key, "media/" + item.id[len(IDPrefix):])
        item.object_key = key
        stream = await self.guard(backend.open(None, item))
        self.assertEqual(stream.read(), b"hello s3")
        url = await self.guard(backend.presign_get(None, item, 3600))
        self.assertTrue(url.startswith(server.url + "/bucket/media/"), url)
        await self.guard(backend.remove(None, item))
        with self.assertRaises(MediaError):
            await self.guard(backend.open(None, item))


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------


class TestResolveForOutput(AsyncMediaTestCase):
    async def test_exports_temp_file_and_holds_reference(self) -> None:
        manager, store, _root = self.make_manager()
        manager.output_root = self.make_dir("outputs")
        item = await self.guard(manager.import_bytes(None, b"send me", Input(name="f.bin")))
        source, cleanup = await self.guard(manager.resolve_for_output(None, item.id))
        try:
            self.assertTrue(source.path.startswith(manager.output_root + os.sep), source.path)
            self.assertTrue(source.path.endswith(os.sep + "media"), source.path)
            self.assertEqual(source.mime_type, item.mime_type)
            self.assertEqual(source.url, "", "输出路径永不产生预签名 URL")
            self.assertEqual(len(store.media_references().list_media_ids(item.id)), 1)
            with open(source.path, "rb") as handle:
                self.assertEqual(handle.read(), b"send me")
        finally:
            cleanup()
            cleanup()  # 幂等
        self.assertEqual(len(store.media_references().list_media_ids(item.id)), 0)
        self.assertFalse(os.path.exists(source.path))


class TestRecoverInterrupted(MediaTestCase):
    def test_recover_drops_temporary_owner_references(self) -> None:
        store = MemoryStore()
        row = Media(id=IDPrefix + "1" * 64, mime_type="text/plain", backend="local")
        store.upsert_media(row)
        temporary = MediaReference(
            media_id=row.id, owner_type="request", owner_id="r1", purpose="temporary"
        )
        durable = MediaReference(
            media_id=row.id, owner_type="message", owner_id="m1", purpose="content"
        )
        store.add_references([temporary, durable])
        store.media().recover_interrupted()
        refs = store.media_references().list_media_ids(row.id)
        self.assertEqual([ref.owner_type for ref in refs], ["message"])


class TestConfigMapping(MediaTestCase):
    def test_config_from_app_duck_types_sections(self) -> None:
        from atri_x.media import config_from_app

        class Section:
            def __init__(self, **kw: Any) -> None:
                self.__dict__.update(kw)

        class App:
            media = Section(llm_image_max_length=123)
            file_delivery = Section(backend="hybrid", max_direct_base64_bytes=42)
            platform_files = Section(max_receive_file_bytes=7, download_timeout_secs=3)
            sandbox = Section(root="")

        cfg = config_from_app(App())
        self.assertEqual(cfg.media.llm_image_max_length, 123)
        self.assertEqual(cfg.media.llm_image_compression_threshold_bytes, 4 * 1024 * 1024)
        self.assertEqual(cfg.file_delivery.backend, "hybrid")
        self.assertEqual(cfg.file_delivery.max_direct_base64_bytes, 42)
        self.assertEqual(cfg.max_import_bytes, 7)
        self.assertEqual(cfg.download_timeout, 3.0)

    def test_config_from_app_accepts_mapping_and_tolerates_gaps(self) -> None:
        from atri_x.media import config_from_app

        cfg = config_from_app({"media": {"llm_image_max_length": 9}})
        self.assertEqual(cfg.media.llm_image_max_length, 9)
        self.assertEqual(cfg.file_delivery.backend, "base64")
        self.assertEqual(config_from_app(None).media.llm_image_max_length, 4096)
