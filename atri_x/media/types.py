"""媒体中心的核心类型、常量与 ctx 适配。

对应 Go：``internal/media/types.go``。

Go 的 ``types.go`` 同时声明了 ``Manager`` struct；本仓把 ``Manager`` 放在
:mod:`atri_x.media.manager`（Go ``manager.go`` 才是它的主体实现），本模块只保留
**跨模块共享**的词汇：媒体 ID 规则、``Source``/``Input``/``ImportLimits``、
``Backend`` 协议与 ctx helper，避免实现模块之间互相 import。

``context.Context`` 适配
------------------------
Go 的方法都收 ``ctx context.Context``；本仓保留同名 ``ctx`` 参数，做**鸭子类型**
处理（与 ``PORTING.md`` §1.6 一致）：

* ``ctx is None`` —— 不取消。
* ``raise_if_cancelled(ctx)`` —— 优先调用 ``ctx.raise_if_cancelled()``
  （:class:`atri_x.safecontext.CancelToken` 就是这个形状），否则退回读
  ``ctx.cancelled`` 属性。
* ``is_cancelled(ctx)`` —— 非抛出式判定。
* ``wait_cancelled(ctx)`` —— 返回一个在 ctx 被取消时完成的 awaitable，
  供 ``asyncio.wait`` 与锁竞争使用；ctx 没有 ``wait`` 时返回 ``None``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Protocol, runtime_checkable

__all__ = [
    "Backend",
    "IDPrefix",
    "ID_PREFIX",
    "ID_HASH_LENGTH",
    "ImportLimits",
    "Input",
    "MediaError",
    "Source",
    "is_cancelled",
    "raise_if_cancelled",
    "valid_id",
    "ValidID",
    "wait_cancelled",
]

#: 媒体 ID 前缀。对应 Go ``media.IDPrefix``。
IDPrefix = "media:"
#: ``snake_case`` 别名（PORTING §1.4：常量也允许 snake_case 形态）。
ID_PREFIX = IDPrefix
#: SHA-256 十六进制长度。对应 Go ``len(IDPrefix)+64`` 里的 64。
ID_HASH_LENGTH = 64


class MediaError(Exception):
    """媒体中心领域错误（Go 里的 ``fmt.Errorf`` 返回值）。

    不是 ``atri_x.errors`` 的哨兵之一：Go 侧 media 包除 ``ErrMediaTooLarge``
    外没有 sentinel，调用方按消息判定失败原因。
    """


def valid_id(value: Any) -> bool:
    """``media:<64 位小写十六进制>`` 判定。对应 Go ``media.ValidID``。"""
    if not isinstance(value, str) or not value.startswith(IDPrefix):
        return False
    if len(value) != len(IDPrefix) + ID_HASH_LENGTH:
        return False
    return all("0" <= ch <= "9" or "a" <= ch <= "f" for ch in value[len(IDPrefix):])


#: Go 导出名 ``ValidID`` 的 PascalCase 别名（``atri_x.background.report`` 等
#: 兄弟模块按 Go 名引用它，见 PORTING §1.4 的交叉检索要求）。
ValidID = valid_id


@dataclass
class Source:
    """媒体来源。对应 Go ``media.Source``。

    ``url``/``file_id`` 是**清洗后**的值（见 :mod:`atri_x.media.sanitize`），
    只保留无凭据风险的形态。
    """

    platform: str = ""
    url: str = ""
    file_id: str = ""


@dataclass
class ImportLimits:
    """单次物化的导入上限覆盖值。对应 Go ``media.ImportLimits``。

    非正值保留 Manager 默认值；``max_import_bytes`` 只能收紧 Manager 的上限。
    """

    max_import_bytes: int = 0
    download_timeout: float = 0.0


@dataclass
class Input:
    """一次导入的输入描述。对应 Go ``media.Input``。"""

    name: str = ""
    mime_type: str = ""
    source: Source = field(default_factory=Source)


@dataclass
class ImportRequest:
    """``Input`` + 字节预算的内部载体（Go 用位置参数传 ``maxBytes``）。"""

    spec: Input = field(default_factory=Input)
    max_bytes: int = 0


@runtime_checkable
class Backend(Protocol):
    """媒体存储后端。对应 Go ``media.Backend``。

    Go 的 ``Put`` 收 ``io.Reader`` 以便流式上传；本仓统一收 ``bytes``
    （intake 上游的 ``import_reader`` 本来就已经把全部字节读进内存做哈希），
    ``size`` 仍按 Go 的语义传入。``open`` 返回**同步**可读流：S3 后端把对象读成
    ``io.BytesIO``，本地后端返回 ``open(path, "rb")``，调用方统一用
    ``read()``/``close()`` 消费（``Manager.open`` 会用
    :class:`atri_x.media.reference.ReferencedReader` 包住它）。
    """

    async def put(
        self, ctx: Any, id: str, data: bytes, size: int, content_type: str
    ) -> str:
        """写入对象，返回记录用位置（本地路径 / 对象键）。"""
        ...

    async def open(self, ctx: Any, media: Any) -> BinaryIO:
        """打开已存对象。"""
        ...

    async def remove(self, ctx: Any, media: Any) -> None:
        """删除对象；对象不存在必须视为成功。"""
        ...

    async def presign_get(self, ctx: Any, media: Any, expiry: float) -> str:
        """生成限时下载 URL；不支持的后端抛 :class:`MediaError`。"""
        ...


# ---------------------------------------------------------------------------
# context.Context 适配
# ---------------------------------------------------------------------------


def is_cancelled(ctx: Any) -> bool:
    """``ctx`` 是否已取消（``None`` 视为未取消）。"""
    if ctx is None:
        return False
    checker = getattr(ctx, "is_cancelled", None)
    if checker is None:
        checker = getattr(ctx, "cancelled", None)
    if checker is None:
        return False
    try:
        return bool(checker() if callable(checker) else checker)
    except Exception:  # noqa: BLE001 - 判定失败按"已取消"保守处理
        return True


def raise_if_cancelled(ctx: Any) -> None:
    """ctx 已取消时抛出对应异常（Go 的 ``ctx.Err()``）。

    :class:`atri_x.safecontext.CancelToken` 会抛出它自己的
    ``CancelledError``/``DeadlineExceededError``；其他 ctx 形状退回
    ``asyncio.CancelledError``。
    """
    if ctx is None:
        return
    raiser = getattr(ctx, "raise_if_cancelled", None)
    if callable(raiser):
        raiser()
        return
    if not is_cancelled(ctx):
        return
    error = getattr(ctx, "error", None)
    if callable(error):
        error = error()
    if isinstance(error, BaseException):
        raise error
    raise asyncio.CancelledError("media context cancelled")


def wait_cancelled(ctx: Any) -> Any:
    """返回 ctx 被取消时完成的 awaitable；ctx 不支持等待时返回 ``None``。

    只用于 ``asyncio.wait`` 竞争（锁获取、清理派发），拿到的协程必须交给
    ``asyncio.ensure_future`` 转成 Task。
    """
    if ctx is None:
        return None
    waiter = getattr(ctx, "wait", None)
    if not callable(waiter):
        return None
    return waiter()
