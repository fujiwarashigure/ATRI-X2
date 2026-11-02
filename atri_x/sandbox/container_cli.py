"""容器引擎命令行适配：找引擎、跑命令、解析 ``docker/podman`` 的 JSON 输出。

只做「执行 + 解析」，不含沙箱策略；策略在
:mod:`atri_x.sandbox.backend_container` 里。这样单测可以只测解析/参数拼装，
不需要真的装 Docker。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass

__all__ = [
    "EngineCLI",
    "EngineCommandResult",
    "classify_failure",
    "decode_output",
    "parse_duration_seconds",
    "parse_size_bytes",
    "parse_stats",
    "read_capped_output",
    "resolve_engine",
]

#: 单次引擎命令保留的输出上限（防止 ``docker build`` 日志把内存吃满）。
_MAX_CAPTURE = 4 * 1024 * 1024


@dataclass
class EngineCommandResult:
    """一次引擎命令的结果。"""

    args: list[str]
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """退出码为 0。"""
        return self.exit_code == 0

    def error_text(self, limit: int = 400) -> str:
        """给异常用的可读错误（优先 stderr 最后几行）。"""
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        if not text:
            return f"exit code {self.exit_code}"
        lines = [line for line in text.splitlines() if line.strip()]
        tail = "\n".join(lines[-3:])
        return tail[:limit]


class EngineCLI:
    """一个具体引擎（``docker`` 或 ``podman``）的命令行封装。"""

    def __init__(self, kind: str, path: str) -> None:
        self.kind = kind
        self.path = path

    async def run(
        self,
        args: list[str],
        *,
        timeout: float = 120.0,
        input_text: str | None = None,
    ) -> EngineCommandResult:
        """异步执行一条引擎命令（输出有上限，超时取消）。"""
        argv = [self.path, *args]
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except OSError as exc:
            return EngineCommandResult(args=args, exit_code=127, stderr=str(exc))
        try:
            stdout_b, stderr_b, _ = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(process.stdout),
                    _read_capped(process.stderr),
                    _feed_stdin(process, input_text),
                ),
                timeout=timeout,
            )
            code = await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            await _terminate(process)
            return EngineCommandResult(
                args=args,
                exit_code=124,
                stderr=f"engine command timed out after {timeout:g}s",
            )
        except asyncio.CancelledError:
            await _terminate(process)
            raise
        return EngineCommandResult(
            args=args,
            exit_code=int(code or 0),
            stdout=stdout_b.decode("utf-8", "replace"),
            stderr=stderr_b.decode("utf-8", "replace"),
        )

    async def version(self) -> tuple[bool, str]:
        """引擎与守护进程是否可用；返回 ``(ok, version_or_error)``。"""
        result = await self.run(["version", "--format", "{{.Server.Version}}"], timeout=20)
        if self.kind == "podman":
            result = await self.run(["info", "--format", "{{.Version.Version}}"], timeout=20)
        if result.ok:
            return True, (result.stdout or "").strip().splitlines()[0] if result.stdout.strip() else "ok"
        return False, result.error_text()


def resolve_engine(backend: str = "auto", engine_path: str = "") -> EngineCLI | None:
    """按 ``[sandbox].backend`` 找引擎；``auto`` 在 Windows 优先 Docker，Linux 优先 Podman。"""
    explicit = (engine_path or "").strip()
    if explicit:
        kind = "podman" if "podman" in os.path.basename(explicit).lower() else "docker"
        if os.path.isfile(explicit) or shutil.which(explicit):
            return EngineCLI(kind, explicit)
        return None
    order = ("docker", "podman") if sys.platform == "win32" else ("podman", "docker")
    wanted = (backend or "auto").strip().lower()
    if wanted in ("docker", "podman"):
        order = (wanted,)
    for kind in order:
        found = shutil.which(kind)
        if found:
            return EngineCLI(kind, found)
    return None


async def _read_capped(stream: asyncio.StreamReader | None) -> bytes:
    """边读边丢，只保留前 ``_MAX_CAPTURE`` 字节。"""
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if total < _MAX_CAPTURE:
            keep = chunk[: _MAX_CAPTURE - total]
            chunks.append(keep)
            total += len(keep)
    return b"".join(chunks)


async def _feed_stdin(process: asyncio.subprocess.Process, text: str | None) -> None:
    """把 stdin 写进去并关闭（没有输入时返回）。"""
    if text is None or process.stdin is None:
        return
    try:
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
    except (OSError, ConnectionResetError):
        pass


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """超时/取消时结束引擎命令。"""
    if process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except (asyncio.TimeoutError, OSError):
        pass


def parse_size_bytes(text: str) -> int:
    """把 ``12.3MiB`` / ``1.5GB`` / ``0B`` 这类人类可读大小转成字节。"""
    value = (text or "").strip().replace(" ", "")
    if not value:
        return 0
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "KiB": 1024,
        "MB": 1000**2,
        "MiB": 1024**2,
        "GB": 1000**3,
        "GiB": 1024**3,
        "TB": 1000**4,
        "TiB": 1024**4,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if value.endswith(suffix):
            number = value[: -len(suffix)]
            try:
                return int(float(number) * units[suffix])
            except ValueError:
                return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def parse_duration_seconds(text: str) -> int:
    """把 ``1.2s`` / ``150ms`` / ``2h3m`` 这类时长粗略转成秒。"""
    value = (text or "").strip()
    if not value:
        return 0
    total = 0.0
    number = ""
    for char in value:
        if char.isdigit() or char == ".":
            number += char
            continue
        if not number:
            continue
        amount = float(number)
        number = ""
        if char == "h":
            total += amount * 3600
        elif char == "m":
            total += amount * 60
        elif char == "s":
            total += amount
        elif char == "u":
            total += amount / 1_000_000
        elif char == "n":
            total += amount / 1_000_000_000
    return int(total)


def parse_stats(text: str) -> dict:
    """解析 ``docker stats --no-stream --format '{{json .}}'`` 的一行 JSON。"""
    try:
        data = json.loads((text or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return {}
    if not isinstance(data, dict):
        return {}
    memory = str(data.get("MemUsage", "") or "").split("/")
    net = str(data.get("NetIO", "") or "").split("/")
    cpu = str(data.get("CPUPerc", "0%") or "0%").strip().rstrip("%")
    pids = str(data.get("PIDs", "0") or "0").strip()

    def _number(value: str) -> float:
        try:
            return float(value)
        except ValueError:
            return 0.0

    return {
        "name": str(data.get("Name", "") or ""),
        "cpu_percent": _number(cpu),
        "memory_bytes": parse_size_bytes(memory[0]) if memory else 0,
        "memory_limit_bytes": parse_size_bytes(memory[1]) if len(memory) > 1 else 0,
        "net_rx_bytes": parse_size_bytes(net[0]) if net else 0,
        "net_tx_bytes": parse_size_bytes(net[1]) if len(net) > 1 else 0,
        "pids": int(_number(pids)),
    }


async def read_capped_output(
    stream: asyncio.StreamReader | None, limit: int
) -> tuple[bytes, bool]:
    """读取输出流：保留前 ``limit`` 字节，其余排空丢弃（防管道阻塞）。"""
    if stream is None:
        return b"", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if total < limit:
            keep = chunk[: limit - total]
            chunks.append(keep)
            total += len(keep)
            if len(keep) < len(chunk):
                truncated = True
        else:
            truncated = True
    return b"".join(chunks), truncated


def decode_output(raw: tuple[bytes, bool]) -> tuple[str, bool]:
    """字节 → 文本（UTF-8 容错），保留截断标记。"""
    data, truncated = raw
    return data.decode("utf-8", "replace"), truncated


def classify_failure(exit_code: int | None, stderr: str) -> str:
    """按退出码/stderr 粗分类失败原因（审计与工具错误用）。"""
    if exit_code in (0, None):
        return ""
    text = (stderr or "").lower()
    if exit_code == 137 or "out of memory" in text or "oom" in text:
        return "oom"
    if exit_code == 126 or "permission denied" in text:
        return "permission_denied"
    if "no space left" in text or "disk quota" in text:
        return "disk_full"
    return "exit_nonzero"
