"""``media.*`` RPC：把宿主 MediaAPI 暴露给进程 Hook。

对应 Go：``internal/hook/runtime/media.go``。

只有下面这个投影会过管道：**从不**把 ``storage.Media`` 本体发出去。租约
（``MediaReference``）带 TTL，由 :meth:`MediaBridge.prune` 定时回收。
"""

from __future__ import annotations

import base64 as _base64
import json
import os
import shutil
import secrets
import threading
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from atri_x.errors import AtriXError, ClosedError, ErrClosed
from atri_x.hook.output.jsonstrict import (
    JSONDecodeError,
    strict_object,
)
from atri_x.hook.output.output import MAX_BASE64_BYTES
from atri_x.hook.runtime.routes import random_id
from atri_x.media.types import Input
from atri_x.storage.models import Media, MediaReference

#: 小于这个大小（1 MiB）的 ``media.read`` 直接内联 base64。
MEDIA_INLINE_BYTES = 1024 * 1024
#: 媒体租约 TTL（1 小时）。
MEDIA_LEASE_TTL = 3600.0

__all__ = [
    "MEDIA_INLINE_BYTES",
    "MEDIA_LEASE_TTL",
    "MediaBridgeError",
    "MediaResult",
    "MediaLease",
    "MediaBridge",
    "new_media_bridge",
]


class MediaBridgeError(AtriXError):
    """``media.*`` 请求失败。对应 Go 里这一层的 ``fmt.Errorf``。"""


@dataclass
class MediaResult:
    """过管道发给插件的媒体投影。对应 Go ``mediaResult``。"""

    media_id: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0
    base64: str = ""
    path: str = ""
    expires_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        from atri_x.hook.protocol.eventjson import time_to_json

        out: dict[str, Any] = {
            "media": self.media_id,
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
        }
        if self.base64:
            out["base64"] = self.base64
        if self.path:
            out["path"] = self.path
        out["expires_at"] = time_to_json(self.expires_at)
        return out


@dataclass
class MediaLease:
    """一条媒体租约。对应 Go ``mediaLease``。"""

    reference: MediaReference = field(default_factory=MediaReference)
    expires: datetime | None = None
    dir: str = ""
    path: str = ""


class MediaBridge:
    """把 ``hook.MediaAPI`` 适配成 ``media.*`` RPC。对应 Go ``mediaBridge``。"""

    def __init__(self, api: Any) -> None:
        self._mu = threading.RLock()
        self._api = api
        self._owner = random_id("hook-media")
        self._leases: dict[str, MediaLease] = {}
        self._closed = False

    @property
    def api(self) -> Any:
        return self._api

    @property
    def closed(self) -> bool:
        return self._closed

    async def request(
        self, ctx: Any, base_dir: str, method: str, raw: Any
    ) -> MediaResult:
        """处理一次 ``media.*`` 请求。对应 Go ``mediaBridge.request``。"""
        with self._mu:
            if self._closed:
                raise ClosedError("hook runtime manager is closed")
        if method == "media.import":
            metadata = await self._import(ctx, base_dir, raw)
        elif method in ("media.read", "media.export", "media.metadata"):
            params = _decode(
                raw,
                {"media": _require_string},
                "media.params",
            )
            try:
                metadata = await self._api.metadata(ctx, params.get("media") or "")
            except Exception as exc:  # noqa: BLE001 - 统一包装成桥接错误
                raise MediaBridgeError(f"hook media operation failed: {exc}") from exc
        else:
            raise MediaBridgeError(f"unsupported hook media method {method!r}")
        return await self._lease(ctx, metadata, method)

    # -- 内部 ---------------------------------------------------------------
    async def _import(self, ctx: Any, base_dir: str, raw: Any) -> Media:
        params = _decode(
            raw,
            {
                "url": _require_string,
                "path": _require_string,
                "base64": _optional_string,
                "name": _require_string,
                "mime_type": _require_string,
            },
            "media.import",
        )
        provided = 0
        if params.get("url"):
            provided += 1
        if params.get("path"):
            provided += 1
        if "base64" in params and params["base64"] is not None:
            provided += 1
        if provided != 1:
            raise MediaBridgeError(
                "media.import requires exactly one of url, path or base64"
            )
        spec = Input(
            name=params.get("name") or "",
            mime_type=params.get("mime_type") or "",
        )
        try:
            if params.get("url"):
                return await self._api.import_url(ctx, params["url"], spec)
            if params.get("path"):
                return await self._import_path(ctx, base_dir, params["path"], spec)
            encoded = params.get("base64") or ""
            if _decoded_len(len(encoded)) > MAX_BASE64_BYTES:
                raise MediaBridgeError(
                    "media.import base64 exceeds 10 MiB decoded limit"
                )
            try:
                data = _base64.b64decode(_strip_ws(encoded), validate=True)
            except (ValueError, _base64.binascii.Error) as exc:
                raise MediaBridgeError(f"decode media.import base64: {exc}") from exc
            return await self._api.import_bytes(ctx, data, spec)
        except MediaBridgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MediaBridgeError(f"hook media operation failed: {exc}") from exc

    async def _import_path(
        self, ctx: Any, base_dir: str, rel: str, spec: Input
    ) -> Media:
        if not is_local_path(rel):
            raise MediaBridgeError("media.import path must be plugin-relative")
        target = _resolve_within(base_dir, rel)
        if target is None:
            raise MediaBridgeError("open plugin media: path escapes plugin directory")
        try:
            handle = open(target, "rb")
        except OSError as exc:
            raise MediaBridgeError(f"open plugin media: {exc}") from exc
        with handle:
            if not os.path.isfile(target):
                raise MediaBridgeError("media.import requires a regular file")
            size = os.path.getsize(target)
            if not spec.name:
                spec.name = os.path.basename(rel)
            return await self._api.import_reader(ctx, handle, size, spec)

    async def _lease(self, ctx: Any, metadata: Media, method: str) -> MediaResult:
        with self._mu:
            lease = self._leases.get(metadata.id)
            if lease is None:
                lease = MediaLease(
                    reference=MediaReference(
                        media_id=metadata.id,
                        owner_type="hook",
                        owner_id=self._owner,
                        purpose="temporary",
                    )
                )
                try:
                    self._api.add_reference(lease.reference)
                except Exception as exc:  # noqa: BLE001
                    raise MediaBridgeError(f"hook media operation failed: {exc}") from exc
                self._leases[metadata.id] = lease
            lease.expires = datetime.now(timezone.utc) + timedelta(
                seconds=MEDIA_LEASE_TTL
            )
            result = MediaResult(
                media_id=metadata.id,
                name=metadata.name,
                mime_type=metadata.mime_type,
                size=metadata.size,
                expires_at=lease.expires,
            )
            if method == "media.read" and metadata.size <= MEDIA_INLINE_BYTES:
                try:
                    data, _meta = await self._api.read(ctx, metadata.id)
                except Exception as exc:  # noqa: BLE001
                    raise MediaBridgeError(f"hook media operation failed: {exc}") from exc
                result.base64 = _base64.b64encode(data).decode("ascii")
            elif method in ("media.export", "media.read"):
                if not lease.path:
                    directory = _new_lease_dir()
                    path = os.path.join(directory, "media")
                    try:
                        await self._api.export(ctx, metadata.id, path)
                    except Exception as exc:  # noqa: BLE001
                        shutil.rmtree(directory, ignore_errors=True)
                        raise MediaBridgeError(
                            f"hook media operation failed: {exc}"
                        ) from exc
                    lease.dir, lease.path = directory, path
                result.path = lease.path
            return result

    async def prune(self, ctx: Any, now: datetime | None = None, close_all: bool = False) -> None:
        """回收到期（或全部）租约。对应 Go ``mediaBridge.prune``。"""
        moment = now or datetime.now(timezone.utc)
        with self._mu:
            if close_all:
                self._closed = True
            failures: list[str] = []
            for media_id, lease in list(self._leases.items()):
                if not close_all and lease.expires is not None and moment < lease.expires:
                    continue
                try:
                    self._api.remove_reference(lease.reference)
                except Exception as exc:  # noqa: BLE001
                    failures.append(str(exc))
                    continue
                if lease.dir:
                    try:
                        shutil.rmtree(lease.dir, ignore_errors=False)
                    except OSError as exc:
                        failures.append(str(exc))
                        continue
                self._leases.pop(media_id, None)
        if failures:
            raise MediaBridgeError("release hook media: " + "; ".join(failures))


def new_media_bridge(api: Any) -> MediaBridge:
    """对应 Go ``newMediaBridge``。"""
    return MediaBridge(api)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def is_local_path(path: str) -> bool:
    """对应 Go ``filepath.IsLocal``：相对、不逃出、不是绝对/盘符/UNC。"""
    path = path or ""
    if not path or os.path.isabs(path) or os.path.splitdrive(path)[0]:
        return False
    if path.startswith(("/", "\\")):
        return False
    parts = path.replace("\\", "/").split("/")
    return all(part not in ("..",) for part in parts) and parts[-1] != ""


def _resolve_within(base_dir: str, rel: str) -> str | None:
    """把插件目录内的相对路径解析成真实路径（拒绝符号链接逃逸）。"""
    base = os.path.realpath(base_dir or ".")
    target = os.path.realpath(os.path.join(base, rel))
    if target == base or target.startswith(base + os.sep):
        return target
    return None


def _new_lease_dir() -> str:
    """为 ``media.read``/``media.export`` 建一个临时目录。

    刻意不用 ``tempfile.mkdtemp``：本机它以 ``0o700`` 建目录，后续清理会撞
    ACL（见 PORTING.md §7.2）。这里自己建、由 :meth:`MediaBridge.prune` 删。
    """
    base = os.path.join(tempfile.gettempdir(), "atri-x-hook-media")
    os.makedirs(base, exist_ok=True)
    for _attempt in range(8):
        candidate = os.path.join(base, secrets.token_hex(8))
        try:
            os.makedirs(candidate, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise MediaBridgeError("create hook media temp directory: too many collisions")


def _strip_ws(value: str) -> str:
    if "\n" in value or "\r" in value or "\t" in value:
        return value.replace("\n", "").replace("\r", "").replace("\t", "")
    return value


def _decoded_len(encoded_len: int) -> int:
    return encoded_len // 4 * 3


def _require_string(value: Any, path: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise JSONDecodeError(f"{path}: cannot unmarshal non-string into string")
    return value


def _optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, path)


def _decode(raw: Any, fields: dict[str, Any], path: str) -> dict[str, Any]:
    if raw is None:
        raise JSONDecodeError(f"{path}: unexpected end of JSON input")
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if isinstance(raw, str):
        if not raw.strip():
            raise JSONDecodeError(f"{path}: unexpected end of JSON input")
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise JSONDecodeError(f"{path}: invalid JSON: {exc}") from exc
    return strict_object(raw, path, fields)
