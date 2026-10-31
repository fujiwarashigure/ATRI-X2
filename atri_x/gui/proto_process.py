"""协议端进程托管：启动、停止、PID 归属校验。

为什么单独一层：``gui/qqproto.py`` 原来只负责"准备配置 + 启动"，启动时把 ``Popen``
的返回值丢掉，所以"这个进程是不是我起的"无法回答；停止只能按镜像名
``taskkill /f /im QQ.exe``——那会**杀掉用户本机所有 QQ**（包括与 ATRI-X 无关的会话）。
这里把 ``PID + 镜像名 + 启动时间`` 写进状态文件（``<协议端目录>/atri-x-proto-process.json``），
停止前逐项校验归属，只停自己启动的那一棵进程树。

状态文件落在协议端目录而不是配置目录：它描述的是"这台机器上那套协议端的托管关系"，
跟着环境目录走才不会被 ``app.toml`` 迁移/复制带错地方。

不做的事：不扫全表找进程、不按名字杀、不接管不是本程序启动的协议端（外置/用户自己
双击起来的进程，界面只显示"外部自备（未托管）"）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import time
from typing import Any

__all__ = [
    "STATE_NAME",
    "clear_state",
    "launch_console",
    "managed_status",
    "read_state",
    "state_path",
    "stop_managed",
    "write_state",
]

#: 托管状态文件名（相对协议端环境目录）。
STATE_NAME = "atri-x-proto-process.json"

#: Windows ``CREATE_NEW_CONSOLE``：协议端要有自己的控制台窗口，关窗即下线，用户看得见。
_CREATE_NEW_CONSOLE = 0x00000010

#: 停止时最多等这么久（秒），超时也算"已发出停止请求"。
STOP_WAIT_SECONDS = 8.0


def state_path(env_dir: str) -> str:
    """状态文件路径；``env_dir`` 为空时返回空串（表示不可托管）。"""
    root = (env_dir or "").strip()
    if not root:
        return ""
    return os.path.join(root, STATE_NAME)


def read_state(env_dir: str) -> dict[str, Any]:
    """读状态文件；没有/损坏都返回空字典（当作"未托管"）。"""
    path = state_path(env_dir)
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def write_state(env_dir: str, record: dict[str, Any]) -> None:
    """写状态文件（尽力而为：写不了只影响"停止"，不影响启动本身）。"""
    path = state_path(env_dir)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError:
        pass


def clear_state(env_dir: str) -> None:
    """删掉状态文件（忽略不存在）。"""
    path = state_path(env_dir)
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def launch_console(
    env_dir: str,
    backend: str,
    argv: list[str],
    *,
    cwd: str = "",
    detail: str = "",
) -> tuple[bool, str]:
    """在新控制台窗口里启动协议端，并记下 PID 归属。返回 ``(成功, 说明)``。"""
    if not argv:
        return False, "协议端启动命令为空"
    if not env_dir:
        return False, "协议端环境目录未知，无法托管启动"
    creationflags = _CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(  # noqa: S603 - 命令来自本模块与调用方的固定参数
            list(argv),
            cwd=cwd or None,
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as exc:
        return False, f"启动协议端失败：{exc}"
    write_state(
        env_dir,
        {
            "pid": int(proc.pid),
            "backend": backend,
            "image": os.path.basename(argv[0]),
            "argv": list(argv),
            "cwd": cwd or "",
            "started_at": time.time(),
        },
    )
    return True, detail or f"已启动协议端（PID {proc.pid}）"


def managed_status(env_dir: str) -> dict[str, Any]:
    """当前托管状态：记录里的 PID 是否还活着、镜像名是否还对得上。"""
    record = read_state(env_dir)
    pid = _as_int(record.get("pid"))
    if pid <= 0:
        return {"managed": False, "reason": "本程序没有启动过协议端"}
    if not _pid_alive(pid):
        return {"managed": False, "pid": pid, "reason": "记录的协议端进程已经退出"}
    expected = _image_name(record.get("image"))
    actual = _pid_image(pid)
    if expected and actual and expected != _image_name(actual):
        return {
            "managed": False,
            "pid": pid,
            "reason": f"PID {pid} 已被别的进程占用（{actual}）",
        }
    return {
        "managed": True,
        "pid": pid,
        "backend": str(record.get("backend") or ""),
        "image": actual or expected,
        "started_at": record.get("started_at") or 0,
        "reason": "",
    }


def stop_managed(env_dir: str) -> tuple[bool, str]:
    """只停本程序启动的那一个协议端进程树；返回 ``(成功, 说明)``。

    校验顺序：有记录 → PID 还活着 → 镜像名对得上。任一项不成立就**不动手**，
    只清理过期记录并说明原因（宁可让用户手动处理，也不误杀别人的 QQ）。
    """
    record = read_state(env_dir)
    pid = _as_int(record.get("pid"))
    if pid <= 0:
        return False, "没有本程序启动的协议端记录（外置协议端请自行关闭）"
    if not _pid_alive(pid):
        clear_state(env_dir)
        return False, "记录的协议端进程已经退出（已清理记录）"
    expected = _image_name(record.get("image"))
    actual = _pid_image(pid)
    if expected and actual and expected != _image_name(actual):
        clear_state(env_dir)
        return False, f"PID {pid} 现在是 {actual}，不是本程序启动的协议端，已放弃停止"
    _terminate_tree(pid)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    alive = _pid_alive(pid)
    clear_state(env_dir)
    if alive:
        return False, f"已请求停止 PID {pid}，但它仍在运行（可能被系统拒绝）"
    return True, f"已停止协议端（PID {pid}）"


# ---------------------------------------------------------------------------
# 进程探测（纯标准库；Windows 用 tasklist，其它平台用 os.kill）
# ---------------------------------------------------------------------------
def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _image_name(value: Any) -> str:
    """镜像名归一化：去路径、转小写、去 ``.exe`` 后缀后再比较。

    启动用的 ``argv[0]`` 与 ``tasklist`` 报回来的名字不一定写法一致：内置 NapCat 用
    ``["cmd", "/c", Start-NapCat.bat]`` 启动，记录里存的是 ``cmd``，而 ``tasklist``
    报 ``cmd.exe``。不归一化就会把"自己启动的进程"误判成"PID 被别的进程占用"
    （状态显示「已被别的进程占用（cmd.exe）」且停止按钮拒绝执行）。
    """
    base = os.path.basename(str(value or "").strip()).lower()
    return base[:-4] if base.endswith(".exe") else base


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    return _tasklist_row(pid) != []


def _pid_image(pid: int) -> str:
    """进程镜像名（如 ``cmd.exe``）；查不到返回空串。"""
    if pid <= 0:
        return ""
    if os.name != "nt":
        return ""
    row = _tasklist_row(pid)
    return row[0] if row else ""


def _tasklist_row(pid: int) -> list[str]:
    """``tasklist /FI "PID eq N" /FO CSV /NH`` 的第一行字段。"""
    try:
        completed = subprocess.run(  # noqa: S603,S607 - 系统自带命令，参数是整数
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except (OSError, subprocess.SubprocessError):
        return []
    for row in csv.reader(io.StringIO(completed.stdout or "")):
        if row and row[0].strip() and _as_int(row[1] if len(row) > 1 else "") == pid:
            return [field.strip() for field in row]
    return []


def _terminate_tree(pid: int) -> None:
    """结束进程树（协议端会拉子进程：命令行 → QQ.exe；Node → 注入进程）。"""
    if os.name == "nt":
        try:
            subprocess.run(  # noqa: S603,S607 - 参数是整数 PID
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True,
                timeout=15,
                creationflags=0x08000000,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
