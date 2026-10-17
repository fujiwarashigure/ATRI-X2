"""控制台与日志：各部署脚本共用的 ``log`` / ``warn`` / ``die``。

对应上游：``deploy/`` 下 7 个 shell 脚本里各自复制的那一份
（``log()`` / ``warn()`` / ``die()`` + ``printf '\\033[1;3Xm==>\\033[0m %s\\n'``）。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。
``_lib.py`` 仍然 re-export 这里的一切，调用方 ``import _lib`` 即可。
"""

from __future__ import annotations

import sys

__all__ = ["ANSI", "console", "die", "log", "safe_write", "warn"]

ANSI = {
    "red": "\033[1;31m",
    "green": "\033[1;32m",
    "yellow": "\033[1;33m",
    "reset": "\033[0m",
}


def _ansi_enabled() -> bool:
    """是否给输出上色：只有 TTY 且未显式关闭时上色（cron / systemd 里是无色的）。"""
    import os

    if os.environ.get("NO_COLOR") or os.environ.get("ATRIX_NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:  # pragma: no cover - stdout 被替换成非 TTY 对象
        return False


def console() -> None:
    """把 stdout/stderr 切到 UTF-8，并把 GBK 控制台表达不了的字符转义。

    ``PORTING.md`` §7.4：控制台码页是 GBK，直接 print 含 ``€`` 的字符串会抛
    ``UnicodeEncodeError``，把“报告一次编码损坏”变成“报告器自己崩掉”。
    这里在进程入口统一兜底：能 utf-8 就 utf-8，不能就 ``backslashreplace``。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # pragma: no cover - 某些被重定向的流不支持 reconfigure
            continue


def safe_write(stream, text: str) -> None:
    """写一行到 ``stream``；编码不可表示时退到 ASCII 转义而不是抛异常。"""
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:  # pragma: no cover - console() 之后不应再发生
        stream.write(text.encode("ascii", "backslashreplace").decode() + "\n")
    try:
        stream.flush()
    except Exception:  # pragma: no cover - 已关闭的流
        pass


def _emit(prefix: str, message: str, stream) -> None:
    safe_write(stream, f"{prefix}{message}")


def log(message: str) -> None:
    """``==> 消息``（对应各 shell 脚本的 ``log()``，绿色，走 stdout）。"""
    color = ANSI["green"] if _ansi_enabled() else ""
    reset = ANSI["reset"] if color else ""
    _emit(f"{color}==>{reset} ", message, sys.stdout)


def warn(message: str) -> None:
    """``[!] 消息``（对应 ``warn()``，黄色，走 stderr）。"""
    color = ANSI["yellow"] if _ansi_enabled() else ""
    reset = ANSI["reset"] if color else ""
    _emit(f"{color}[!]{reset} ", message, sys.stderr)


def die(message: str, code: int = 1) -> None:
    """``[x] 消息`` 后退出（对应 ``die()``，红色，走 stderr）。

    抛 ``SystemExit`` 而不是直接 ``sys.exit``，便于测试断言退出码。
    """
    color = ANSI["red"] if _ansi_enabled() else ""
    reset = ANSI["reset"] if color else ""
    _emit(f"{color}[x]{reset} ", message, sys.stderr)
    raise SystemExit(code)
