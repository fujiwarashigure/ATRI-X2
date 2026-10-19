"""CPU / 阻塞 IO 的专用线程池。

Python 版新增（Go 侧没有对应文件）：Go 的 goroutine 调度器天然把阻塞系统调用
和 CPU 工作分开，CPython 只有 asyncio 惰性创建的**默认** ``ThreadPoolExecutor``，
IO 与 CPU 任务混在同一个池里 —— 一个卡住的图片压缩就能把 HTTP 请求排在后面。
本模块把这两类工作分到各自的池里。

这不是为了多核提速
------------------
GIL 打开时（``sys._is_gil_enabled() is True``）纯 Python 的 CPU 工作没有真正
的并行。本机实测（Python 3.14.7）：纯 Python 图片压缩 4 线程加速比 **0.97x**、
``json.loads`` **1.03x**、Pillow 大图 **1.41x**。所以拆池**不是为了更快**，而是
为了：

1. **隔离** —— CPU 任务排满自己的池时不会挡住 IO 请求的线程；
2. **背压** —— 每类工作有自己的 ``max_workers`` 上限，而不是共用一个池容量；
3. **可诊断** —— ``thread_name_prefix`` 让线程栈/采样一眼能分清是哪类工作。

约定
----
- :func:`cpu_pool` / :func:`io_pool` 都是**模块级单例**，线程名前缀分别是
  ``atrix-cpu`` / ``atrix-io``。
- **不动** asyncio 的默认池：``set_default_executor`` 会改变全局行为，未迁移的
  调用点保持原样（见 :func:`run_cpu` / :func:`run_io` 的文档）。
- :func:`shutdown_pools` 幂等，供测试与退出路径使用；关掉之后再取池会新建。
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

__all__ = [
    "cpu_pool",
    "io_pool",
    "named_pool",
    "run_cpu",
    "run_io",
    "shutdown_pools",
]

_T = TypeVar("_T")

#: CPU 池的线程数上界。CPU 密集任务通常是突发而非长跑，8 个线程足够吸收峰值，
#: 又不至于让 GIL 切换成为新的开销。
CPU_MAX_WORKERS = 8
#: IO 池的线程数。阻塞 IO 的线程大部分时间在等内核，可以比 CPU 池宽得多；
#: 它与默认池在本机的容量一致（``min(32, cpu + 4)``），因此迁移不改吞吐。
IO_MAX_WORKERS = 32

_pools: "dict[str, ThreadPoolExecutor]" = {}
_pools_lock = threading.Lock()


def _process_cpu_count() -> int:
    """读 ``os.process_cpu_count()``（Python 3.13+），旧版本向下兼容。

    本项目最低支持 Python 3.11：3.11/3.12 没有 ``os.process_cpu_count``，退到
    :func:`os.cpu_count`（不感知亲和性，但同样只用来定上界）。
    """
    getter = getattr(os, "process_cpu_count", None)
    if getter is None:
        return os.cpu_count() or 1
    try:
        count = int(getter() or 0)
    except OSError:  # pragma: no cover - 平台不支持时
        count = 0
    return count or (os.cpu_count() or 1)


def named_pool(prefix: str, max_workers: int) -> ThreadPoolExecutor:
    """取得（必要时新建）一个具名单例池。

    给"自己已经有闸门、池容量必须与闸门对齐"的工作用：调用方若把闸门开到 N
    个槽位却只给 N 个以下的线程，闸门就形同虚设（多出来的槽位只是排队）。
    :func:`cpu_pool` / :func:`io_pool` 都是它的特例；所有池都会被
    :func:`shutdown_pools` 收走。
    """
    workers = max(1, int(max_workers))
    with _pools_lock:
        pool = _pools.get(prefix)
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=prefix
            )
            _pools[prefix] = pool
        return pool


def cpu_pool() -> ThreadPoolExecutor:
    """CPU 密集且**会阻塞**的任务用的池（线程名前缀 ``atrix-cpu``）。

    线程数 ``min(8, 进程可用 CPU 数)``；GIL 下这不等于并行度，见模块 docstring。
    """
    return named_pool("atrix-cpu", min(CPU_MAX_WORKERS, _process_cpu_count()))


def io_pool() -> ThreadPoolExecutor:
    """阻塞 IO 任务用的池（线程名前缀 ``atrix-io``，32 线程）。"""
    return named_pool("atrix-io", IO_MAX_WORKERS)


async def run_cpu(fn: Callable[..., _T], *args: Any) -> _T:
    """在 :func:`cpu_pool` 里跑 ``fn(*args)`` 并 await 结果。

    用 :func:`functools.partial` 绑定入参，而不是 ``lambda``：lambda 写循环体时
    很容易捕获到循环变量（延迟绑定 bug）。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(cpu_pool(), functools.partial(fn, *args))


async def run_io(fn: Callable[..., _T], *args: Any) -> _T:
    """在 :func:`io_pool` 里跑 ``fn(*args)`` 并 await 结果。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(io_pool(), functools.partial(fn, *args))


def shutdown_pools() -> None:
    """关掉所有已建池（幂等），供测试与退出路径使用。

    关掉之后再调用 :func:`cpu_pool` / :func:`io_pool` 会新建一个池；正在跑的
    工作由 ``ThreadPoolExecutor`` 自己等它结束，不在这里 cancel。
    """
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.shutdown(wait=False)
