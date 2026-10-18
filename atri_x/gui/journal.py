"""界面操作日志：把桌面端"点了什么、改了哪个文件"记进 ``<配置目录>/gui/gui.log``。

后台服务的日志（``atri-x-*.log`` / ``audit-*.log``）只覆盖服务进程；用户在桌面端点了
「保存 Provider」「获取模型目录」「下载头像」之后，服务日志里看不到任何痕迹。排障时
最常问的正是"我刚改的那一步到底落了没落盘"，所以这里补一条极简的追加日志：

* 一行一条：``YYYY-MM-DD HH:MM:SS  动作说明``；
* 只追加、不阻塞：写失败静默（操作日志不该影响正在做的事）；
* 超过 :data:`MAX_JOURNAL_BYTES` 时轮转一份 ``gui.log.1``（只留上一份）。

读取侧由「日志」选项卡负责（通道选「界面操作」）。
"""

from __future__ import annotations

import os
from datetime import datetime

__all__ = [
    "JOURNAL_NAME",
    "MAX_JOURNAL_BYTES",
    "journal_path",
    "read_lines",
    "read_tail",
    "record",
]

#: 文件名（相对 ``<配置目录>/gui``）。
JOURNAL_NAME = "gui.log"

#: 轮转阈值（字节）。
MAX_JOURNAL_BYTES = 512 * 1024


def journal_path(config_dir: str) -> str:
    """操作日志路径（``<配置目录>/gui/gui.log``）。"""
    return os.path.join(config_dir or "", "gui", JOURNAL_NAME)


def record(config_dir: str, message: str) -> bool:
    """追加一条操作记录；返回是否写入成功。"""
    text = (message or "").strip()
    if not config_dir or not text:
        return False
    path = journal_path(config_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate(path)
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}\n"
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
        return True
    except OSError:
        return False


def _rotate(path: str) -> None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < MAX_JOURNAL_BYTES:
        return
    try:
        os.replace(path, path + ".1")
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def read_lines(config_dir: str, *, limit: int = 500) -> list[str]:
    """读最后 ``limit`` 行（不把整个文件读进内存）。"""
    return [line for line in read_tail(config_dir, limit=limit).splitlines()]


def read_tail(config_dir: str, *, limit: int = 500, max_bytes: int = 256 * 1024) -> str:
    """读文件末尾（最多 ``max_bytes`` 字节），返回文本。"""
    path = journal_path(config_dir)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    rows = text.splitlines()
    return "\n".join(rows[-max(limit, 1) :])
