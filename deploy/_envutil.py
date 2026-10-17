"""时间、环境变量、文本/JSON、状态小文件。

对应上游：各 shell 脚本里的 ``date`` 调用、``${VAR:-default}`` 读环境变量、
``read_env_value`` / ``write_int`` / ``read_int``，以及 ``mktemp`` 之外那些
纯文本读写。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。
``_lib.py`` 仍然 re-export 这里的一切。

时间约定（``PORTING.md`` §1.7）：所有返回的时间都是 **aware** 的
``datetime``；``now_iso()`` 生成 ``date -Is`` 等价物，带本地时区偏移。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

__all__ = [
    "decode_env_value",
    "env_flag",
    "env_int",
    "env_value",
    "list_env_sources",
    "now_epoch",
    "now_iso",
    "now_local",
    "now_stamp",
    "now_utc",
    "parse_env_file",
    "parse_env_lines",
    "pretty_json",
    "read_env_file",
    "read_int_file",
    "read_text",
    "tail_lines",
    "wc_lines",
    "write_env_file",
    "write_int_file",
    "write_text",
]


# ============================================================
# 时间（一律带时区）
# ============================================================
def now_utc() -> _dt.datetime:
    """当前 UTC 时间（aware）。"""
    return _dt.datetime.now(_dt.timezone.utc)


def now_local() -> _dt.datetime:
    """当前本地时间（aware，按 ``TZ`` 环境变量解析）。"""
    return _dt.datetime.now().astimezone()


def now_iso() -> str:
    """``date -Is`` 的等价物：本地时区 aware 的 ISO8601（秒精度）。"""
    return now_local().replace(microsecond=0).isoformat()


def now_stamp() -> str:
    """``date +%Y%m%d-%H%M%S`` 的等价物，用于备份 / 诊断目录名。"""
    return now_local().strftime("%Y%m%d-%H%M%S")


def now_epoch() -> int:
    """``date +%s`` 的等价物。"""
    return int(now_utc().timestamp())


# ============================================================
# 环境变量
# ============================================================
def env_value(name: str, default: str = "") -> str:
    """读环境变量；空串与未设置等价（对齐 bash ``${VAR:-default}``）。"""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def env_int(name: str, default: int) -> int:
    """读整数环境变量；为空或解析失败返回 ``default``（不抛异常）。"""
    raw = env_value(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def env_flag(name: str, default: bool = False) -> bool:
    """读布尔环境变量：``1/true/yes/on`` 为真，``0/false/no/off`` 为假。

    无法识别的取值退回 ``default``（上游用 ``= "1"`` 精确比较，这里更宽松，
    但 ``0`` 与未设置语义一致）。
    """
    raw = env_value(name).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def decode_env_value(raw: str) -> str:
    """剥掉 dotenv 值两侧的配对引号（对齐 bash ``read_env_value`` 的引号处理）。"""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_env_lines(text: str) -> dict[str, str]:
    """解析 dotenv 文本为字典；``#`` 注释与空行跳过，取第一个出现的键。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in values:
            continue
        values[key] = decode_env_value(raw)
    return values


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """读一个 .env 文件；文件不存在时返回空字典（不抛异常）。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_env_lines(text)


def read_env_file(path: str | os.PathLike[str], key: str, default: str = "") -> str:
    """从 .env 文件读单个键（对应 bash ``read_env_value``）。

    只读文件、不读进程环境：``rollback.py`` 用它读 ``rollback.env`` 时必须拿到
    文件里的值，否则会被同名进程变量污染。
    """
    return parse_env_file(path).get(key, default)


def write_env_file(path: str | os.PathLike[str], values: dict[str, str]) -> None:
    """写 dotenv 文件（``KEY=value`` 逐行，UTF-8 + LF）。"""
    lines = [f"{key}={value}" for key, value in values.items()]
    write_text(path, "\n".join(lines) + "\n")


def list_env_sources() -> dict[str, str]:
    """当前进程环境（供“已知凭据值检测”遍历，对齐 bash 的 ``env``）。"""
    return dict(os.environ)


# ============================================================
# 文本 / JSON
# ============================================================
def read_text(path: str | os.PathLike[str], default: str = "") -> str:
    """读 UTF-8 文本；解码失败用 U+FFFD 替换，文件不存在返回 ``default``。"""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def write_text(path: str | os.PathLike[str], text: str, mode: int | None = None) -> None:
    """写 UTF-8 + LF 文本，必要时先建父目录。

    ``newline="\\n"`` 是硬要求：Windows 上默认会把 ``\\n`` 写成 ``\\r\\n``，
    而离线包里的 ``*.sh`` / ``*.service`` 一旦带 CRLF，Linux 上会报
    ``bad interpreter: /usr/bin/env bash^M``，systemd 也可能解析失败。

    ``PORTING.md`` §7.1：这是本仓**唯一**允许的写文件方式之一（另一个是编辑
    工具）——绝不用 PowerShell 写文件（会写 BOM 并经 GBK 二次编码毁掉中文）。
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    if mode is not None:
        try:
            p.chmod(mode)
        except OSError:  # pragma: no cover - Windows 上 chmod 语义有限
            pass


def pretty_json(value: object) -> str:
    """``json.dumps(..., ensure_ascii=False, indent=2)``；键名逐字照抄不重排。"""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def tail_lines(text: str, limit: int) -> str:
    """取末尾 ``limit`` 行（对应 ``tail -n``）；``limit<=0`` 返回全部。"""
    if limit <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:]) + "\n"


def wc_lines(path: str | os.PathLike[str]) -> int:
    """数行数（对应 ``wc -l``）：空文件 / 不存在都算 0 行。"""
    text = read_text(path)
    if not text:
        return 0
    return len(text.splitlines())


# ============================================================
# 状态文件（watchdog 的 state dir 用的整数小文件）
# ============================================================
def read_int_file(path: str | os.PathLike[str], default: int = 0) -> int:
    """读整数状态文件；不存在或没有数字时返回 ``default``。

    对应 bash ``read_int``：它用 ``tr -dc '0-9'`` 过滤，所以 ``"12\\n"`` 与
    ``"1x2"`` 都会得到数字。这里保持同样的宽容度。
    """
    raw = read_text(path)
    if not raw:
        return default
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return default
    return int(digits)


def write_int_file(path: str | os.PathLike[str], value: int) -> None:
    """写整数状态文件（一行，UTF-8 + LF）。"""
    write_text(path, f"{value}\n")
