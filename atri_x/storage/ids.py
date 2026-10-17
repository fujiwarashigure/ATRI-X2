"""通用 ID helper。对应 Go：``internal/storage/id.go``。

Go 的 ``NewID`` 取 16 个随机字节、置版本位与变体位，再用
``fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])``
格式化 —— 也就是**带连字符的小写 UUID v4**（``8-4-4-4-12``），
等价于 Python 的 ``str(uuid.uuid4())``。

> 历史注记：``PORTING.md`` §1.8 最初写成 ``uuid4().hex``（无连字符）是错的，
> 已按 Go 源码更正；本模块与测试都按更正后的口径（带连字符）实现。
> ID 在库里是不透明 TEXT 主键，两种形态都能用，但既然要求逐包对齐，就按上游形态来。
"""

from __future__ import annotations

import uuid


def new_id() -> str:
    """生成一个新的实体 ID（小写 UUID v4，带连字符）。

    对应 Go：``storage.NewID()``。
    """
    return str(uuid.uuid4())


def parse_id(value: str) -> uuid.UUID:
    """把 ID 解析成 :class:`uuid.UUID`；非法值抛 ``ValueError``。

    上游没有对应函数（Go 里 ID 始终是字符串）。提供它是为了让调用方能显式
    校验而不是把畸形 ID 悄悄带进数据库。
    """
    return uuid.UUID(value)


def is_valid_id(value: str) -> bool:
    """ID 是否是可解析的 UUID（不限制版本）。"""
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True
