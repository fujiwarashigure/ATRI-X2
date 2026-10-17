"""宿主侧媒体访问：显式输入声明、临时引用与沙盒内导出入。

对应 Go：``internal/tool/media_runtime.go``。

职责：

* :class:`MediaInput` —— 一次显式输入声明，**绝不**从命令文本里推断。
* :class:`MediaRuntime` —— 给可执行工具（shell / Go runner）提供宿主拥有的媒体访问。
* :class:`MediaCall` —— 一次调用的引用与临时文件所有权；``close`` 负责回收。

与 Go 的差异：

* Go 用 ``os.OpenRoot`` / ``os.Root`` 做符号链接安全的目录内访问；Python 标准库
  没有对应物，本模块改为"显式校验路径 + 目录内拼接"，并在导入侧拒绝
  绝对路径、盘符、``..`` 与符号链接逃逸（见 :func:`_resolve_in_root`）。
* Go 的 ``context.Context`` → 本仓的 ``ctx`` 鸭子类型（可为 ``None``）。
* Go 的 ``rand.Text()`` → :func:`_rand_text`（26 字符 base32 大写，等价形态）。
* ``Close`` 的 5 秒超时用 :func:`asyncio.wait_for` 复刻。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x.errors import InvalidError

#: 媒体 ID 前缀。对应 Go ``media.IDPrefix``（常量，避免模块级 import 造成循环）。
#:
#: 说明：``atri_x.media`` 的 import 链会经过 ``atri_x.platform.platform`` →
#: ``atri_x.tool.capability``，也就是会先执行 ``atri_x/tool/__init__.py``。本模块
#: 正被该 ``__init__`` 再导出，所以**不能**在模块级 import ``atri_x.media`` /
#: ``atri_x.storage``；下面这些都在函数内延迟 import（PORTING.md 的并行期约定）。
_ID_PREFIX = "media:"

#: ``MediaCall.Close`` 移除引用时的超时（对应 Go 的 5*time.Second）。
CLOSE_TIMEOUT_SECONDS = 5.0

#: 临时工作目录前缀，与 Go 的 ``.media-call-*`` 一致。
MEDIA_CALL_DIR_PREFIX = ".media-call-"

#: ``CachedExport`` 在沙盒内的相对子目录。
MEDIA_INPUTS_DIR = "media-inputs"

_PREFIX = _ID_PREFIX


def _valid_media_id(value: Any) -> bool:
    """延迟 import :func:`atri_x.media.valid_id`（见文件头循环 import 说明）。"""
    from atri_x.media import valid_id

    return valid_id(value)


def _rand_text() -> str:
    """对应 Go 1.24 ``rand.Text()``：26 字符的 base32（大写、无填充）。"""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]


@dataclass
class MediaInput:
    """显式的媒体输入声明。对应 Go ``MediaInput``。

    Go 的 ``UnmarshalJSON`` 用 ``DisallowUnknownFields`` 并校验 media ID；
    Python 侧改为 :meth:`from_dict`，未知字段与非法 ID 都抛
    :class:`atri_x.errors.InvalidError`。
    """

    media_id: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"media": self.media_id}

    @staticmethod
    def from_dict(data: Any) -> "MediaInput":
        if data is None:
            value: Any = {}
        elif isinstance(data, (bytes, bytearray)):
            value = _decode_json_object(bytes(data))
        elif isinstance(data, str):
            value = _decode_json_object(data.encode("utf-8"))
        else:
            value = data
        if not isinstance(value, dict):
            raise InvalidError("invalid media input")
        unknown = [key for key in value if key != "media"]
        if unknown:
            raise InvalidError(
                f'json: unknown field "{sorted(unknown)[0]}"'
            )
        media_id = value.get("media", "")
        if not isinstance(media_id, str) or not _valid_media_id(media_id):
            raise InvalidError("invalid media")
        return MediaInput(media_id=media_id)


def _decode_json_object(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidError(f"invalid media input: {exc}") from exc


@dataclass
class MediaRuntime:
    """宿主拥有的媒体访问入口。对应 Go ``MediaRuntime``。"""

    center: "Any" = None
    sandbox_root: str = ""

    def new_call(self) -> "MediaCall":
        """对应 Go ``MediaRuntime.NewCall``。"""
        return MediaCall(runtime=self, owner=_rand_text())


@dataclass
class MediaCall:
    """一次调用的引用与临时文件所有权。对应 Go ``MediaCall``。"""

    runtime: MediaRuntime | None = None
    owner: str = ""
    refs: "dict[str, Any]" = field(default_factory=dict)
    dir: str = ""
    #: ``close`` 期间的非致命错误（Go 的 ``errors.Join`` 聚合值）。
    failures: list[BaseException] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 引用
    # ------------------------------------------------------------------
    async def retain(self, ctx: Any, id: str) -> "Media":
        """保留一份媒体引用并返回其元数据。对应 Go ``MediaCall.Retain``。"""
        from atri_x.storage import MediaReference

        center = self._center()
        if not _valid_media_id(id):
            raise InvalidError("invalid media")
        metadata = await center.metadata(ctx, id)
        if id not in self.refs:
            ref = MediaReference(
                media_id=id,
                owner_type="skill",
                owner_id=self.owner,
                purpose="temporary",
            )
            center.add_reference(ref)
            self.refs[id] = ref
        return metadata

    async def close(self) -> None:
        """回收临时目录与全部引用。对应 Go ``MediaCall.Close``。

        Go 返回 ``errors.Join`` 聚合后的 error；Python 里清理失败不致命，
        只记录在 ``failures`` 里（避免 close 把调用方的主错误盖掉）。
        """
        failures: list[BaseException] = self.failures
        if self.dir:
            try:
                shutil.rmtree(self.dir, ignore_errors=False)
            except OSError as exc:  # pragma: no cover - 平台相关
                failures.append(exc)
            self.dir = ""
        center = self.runtime.center if self.runtime is not None else None
        if center is not None and self.refs:
            try:
                await asyncio.wait_for(
                    self._release_refs(center), timeout=CLOSE_TIMEOUT_SECONDS
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:  # pragma: no cover
                failures.append(exc)

    async def _release_refs(self, center: Any) -> None:
        for id in list(self.refs):
            ref = self.refs[id]
            try:
                center.remove_reference(ref)
            except BaseException as exc:  # pragma: no cover - 存储层错误
                self.failures.append(exc)
                continue
            self.refs.pop(id, None)

    # ------------------------------------------------------------------
    # 导入导出
    # ------------------------------------------------------------------
    def workspace(self, base: str) -> str:
        """懒建本次调用的私有目录，返回相对 ``base`` 的路径。

        对应 Go ``MediaCall.Workspace``。相对 cwd 是刻意的：让既有 Skill
        脚本里的相对路径继续有效。
        """
        if not self.dir:
            os.makedirs(base, exist_ok=True)
            self.dir = os.path.abspath(
                _make_temp_dir(base, MEDIA_CALL_DIR_PREFIX)
            )
        return os.path.relpath(self.dir, base)

    async def export(self, ctx: Any, id: str, base: str) -> tuple[str, "Media"]:
        """导出一次私有输入，返回 ``(相对 cwd 的路径, 元数据)``。

        对应 Go ``MediaCall.Export``。
        """
        metadata = await self.retain(ctx, id)
        relative = self.workspace(base)
        name = media_filename(metadata)
        target = _resolve_in_root(self.dir, name)
        await self._write(ctx, target, id)
        return os.path.join(relative, name), metadata

    async def cached_export(self, ctx: Any, id: str) -> str:
        """导出到沙盒内的共享输入缓存并刷新 mtime。对应 Go ``MediaCall.CachedExport``。

        复用已存在的文件时也刷新 ``mtime``；删除由常规沙盒保留任务负责，
        这里不持有活动文件锁。
        """
        metadata = await self.retain(ctx, id)
        runtime = self.runtime
        if runtime is None or not runtime.sandbox_root:
            raise InvalidError("media sandbox is not configured")
        base = os.path.abspath(runtime.sandbox_root)
        os.makedirs(base, mode=0o700, exist_ok=True)
        inputs = _resolve_in_root(base, MEDIA_INPUTS_DIR)
        os.makedirs(inputs, mode=0o700, exist_ok=True)
        name = os.path.join(MEDIA_INPUTS_DIR, media_filename(metadata))
        target = _resolve_in_root(base, name)
        if os.path.exists(target) or os.path.islink(target):
            if not os.path.isfile(target):
                raise InvalidError("media cache is not a regular file")
        else:
            await self._write(ctx, target, id)
        now = time.time()
        os.utime(target, (now, now))
        return os.path.join(base, name).replace("\\", "/")

    async def import_(
        self, ctx: Any, base: str, path: str, input: "Input | None" = None
    ) -> "Media":
        """导入 Skill cwd 内的常规产物。对应 Go ``MediaCall.Import``。

        ``path`` 必须是相对路径、不含 ``..``、不含 ``:``、不以 ``/`` 开头，
        且解析后仍落在 ``base`` 内。
        """
        from atri_x.media import Input as MediaSpec

        center = self._center()
        slash = (path or "").replace("\\", "/")
        if slash == "" or slash.startswith("/") or ":" in slash or not _is_local(slash):
            raise InvalidError("skill media path must be relative")
        if any(part == ".." for part in slash.split("/")):
            raise InvalidError("skill media path must not contain parent traversal")
        target = _resolve_in_root(base, slash)
        if not os.path.isfile(target):
            raise InvalidError(f"skill media must be a regular file: {path}")
        spec = input if input is not None else MediaSpec()
        if not spec.name:
            spec = MediaSpec(
                name=os.path.basename(path),
                mime_type=spec.mime_type,
                source=spec.source,
            )
        size = os.path.getsize(target)
        with open(target, "rb") as reader:
            return await center.import_reader(ctx, reader, size, spec)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _center(self) -> Any:
        runtime = self.runtime
        if runtime is None or runtime.center is None:
            raise InvalidError("media center is not configured")
        return runtime.center

    async def _write(self, ctx: Any, target: str, id: str) -> None:
        """把媒体内容原子写进 ``target``。对应 Go ``MediaCall.write``。"""
        center = self._center()
        reader, _metadata = await center.open(ctx, id)
        temporary = target + "." + _rand_text() + ".tmp"
        try:
            with open(temporary, "wb") as out:
                while True:
                    chunk = reader.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except BaseException:
            _remove_quietly(temporary)
            raise
        finally:
            closer = getattr(reader, "close", None)
            if callable(closer):
                closer()
        try:
            os.replace(temporary, target)
        except BaseException:
            _remove_quietly(temporary)
            raise


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _make_temp_dir(base: str, prefix: str) -> str:
    """在 ``base`` 下建一个私有临时目录（代替 ``os.MkdirTemp``）。"""
    for _ in range(100):
        candidate = os.path.join(base, prefix + _rand_text())
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise InvalidError("cannot create media call workspace")  # pragma: no cover


def _is_local(path: str) -> bool:
    """近似 Go ``filepath.IsLocal``：相对、不逃逸、不是保留名。"""
    if path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _resolve_in_root(root: str, name: str) -> str:
    """把 ``name`` 拼到 ``root`` 内并校验没有逃逸（代替 ``os.OpenRoot``）。

    与 ``os.Root`` 的差距：这里用"规范化后前缀比较"取代真正的目录句柄，
    因此**不能**抵御"校验与打开之间被换成符号链接"的竞态（TOCTOU）。
    实际使用场景是同进程内的受控目录，风险可接受；文档已注明。
    """
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, name))
    if not _within(root_abs, target):
        raise InvalidError(f"media path escapes root: {name}")
    # 逐段检查已有的符号链接：不跟随，直接拒绝。
    current = root_abs
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise InvalidError(f"media path must not contain symlinks: {name}")
        if not os.path.exists(current):
            break
    return target


def _within(root: str, target: str) -> bool:
    root_slash = root.replace("\\", "/").rstrip("/").lower()
    target_slash = target.replace("\\", "/").lower()
    return target_slash == root_slash or target_slash.startswith(root_slash + "/")


def media_filename(metadata: "Media") -> str:
    """``<media id 去掉前缀><安全扩展名>``。对应 Go ``mediaFilename``。

    扩展名只保留 ``[A-Za-z0-9]`` 字符，否则整体丢弃（防目录穿越与怪字符）。
    """
    name = getattr(metadata, "name", "") or ""
    ext = os.path.splitext(name)[1]
    if ext:
        body = ext[1:]
        if body == "" or any(
            not (ch.isascii() and (ch.isalnum())) for ch in body
        ):
            ext = ""
    id = getattr(metadata, "id", "") or ""
    if _PREFIX and id.startswith(_PREFIX):
        id = id[len(_PREFIX) :]
    return id + ext


__all__ = [
    "CLOSE_TIMEOUT_SECONDS",
    "MEDIA_CALL_DIR_PREFIX",
    "MEDIA_INPUTS_DIR",
    "MediaCall",
    "MediaInput",
    "MediaRuntime",
    "media_filename",
]
