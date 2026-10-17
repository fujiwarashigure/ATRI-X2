"""``docker stats`` / ``docker inspect`` / ``pip list`` 输出的解析与判定边界。

对应上游：无（冒烟 / 稳定性测试是 Python 版新增的部署套件）。

从 :mod:`_smokelib` 拆出来（原文件 780+ 行，超过 PORTING.md §2 的 600 行上限）：
这一层是**纯函数**——把 docker 的文本输出变成数字、把数字变成布尔判定，
不碰 docker 也不碰文件，因此可以被直接单测；``_smokelib`` 仍然 re-export 这里的一切，
``smoke.py`` / ``stability.py`` 只写 ``_smokelib.xxx`` 一种调用形式。

判定边界（**边界值算通过**，与 stability.py 的 docstring 一致）：
``failures_at_or_below(1, 100, 0.01) is True``（恰好 1%）、
``growth_ratio_ok(1.5, 1.5) is True``（恰好 1.5 倍）。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "BASE_PACKAGES",
    "STATS_FORMAT",
    "TOLERATED_PACKAGES",
    "ascii_bar",
    "failures_at_or_below",
    "format_bytes",
    "growth_ratio_ok",
    "parse_container_user",
    "parse_cpu_percent",
    "parse_mem_usage",
    "parse_pip_freeze",
    "parse_stats_line",
    "third_party_packages",
    "thirds_means",
    "tolerated_packages",
]

#: 允许出现在镜像里的**基础**包（第三方依赖必须一个都没有）。
BASE_PACKAGES: frozenset[str] = frozenset({"pip", "setuptools", "wheel"})
#: 允许存在但要写进 detail 的包：``atri_x`` 是一方代码，Pillow 是 PORTING §1 允许的
#: 可选图像后端（缺失才是常态，Dockerfile 不装它）。
TOLERATED_PACKAGES: frozenset[str] = frozenset({"atri_x", "pillow"})
#: ``docker stats --no-stream`` 的单行格式：``CPU%|内存用量``（如 ``0.15%|42.3MiB / 1.5GiB``）。
STATS_FORMAT = "{{.CPUPerc}}|{{.MemUsage}}"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_container_user(value: str) -> str:
    """``Config.User`` 里的用户名部分（``10001:10001`` → ``10001``）。"""
    return (value or "").split(":", 1)[0].strip()


def parse_pip_freeze(text: str) -> list[str]:
    """``pip list --format=freeze`` 的包名列表（``name==version`` → ``name``）。"""
    names: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==", 1)[0].split(" @ ", 1)[0].strip()
        if name:
            names.append(name)
    return names


def third_party_packages(names: Sequence[str]) -> list[str]:
    """既不在 :data:`BASE_PACKAGES` 也不在 :data:`TOLERATED_PACKAGES` 里的包。

    比较时大小写与 ``-``/``_`` 归一（``Pillow``/``pillow``、
    ``importlib-metadata``/``importlib_metadata`` 视为同一个）。
    """
    allowed = {_normalize_package(name) for name in (BASE_PACKAGES | TOLERATED_PACKAGES)}
    return [name for name in names if _normalize_package(name) not in allowed]


def tolerated_packages(names: Sequence[str]) -> list[str]:
    """包清单里属于「容忍项」（一方代码 / 可选图像后端）的那些，仅用于展示。"""
    allowed = {_normalize_package(name) for name in TOLERATED_PACKAGES}
    return [name for name in names if _normalize_package(name) in allowed]


def _normalize_package(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_")


def parse_mem_usage(text: str) -> float | None:
    """``"42.3MiB / 1.5GiB"`` → 字节数（取 ``/`` 前那一段）；解析不了返回 ``None``。"""
    used = (text or "").split("/", 1)[0].strip()
    if not used:
        return None
    number = ""
    unit = ""
    for char in used:
        if char.isdigit() or char == ".":
            if unit:
                break
            number += char
        else:
            unit += char
    if not number:
        return None
    try:
        value = float(number)
    except ValueError:  # pragma: no cover - 上面的过滤已保证是数字
        return None
    factors = {
        "": 1.0,
        "b": 1.0,
        "k": 1000.0,
        "kb": 1000.0,
        "kib": 1024.0,
        "m": 1000.0**2,
        "mb": 1000.0**2,
        "mib": 1024.0**2,
        "g": 1000.0**3,
        "gb": 1000.0**3,
        "gib": 1024.0**3,
        "t": 1000.0**4,
        "tb": 1000.0**4,
        "tib": 1024.0**4,
    }
    factor = factors.get(unit.strip().lower())
    return None if factor is None else value * factor


def parse_cpu_percent(text: str) -> float | None:
    """``"0.15%"`` → ``0.15``；解析不了返回 ``None``。"""
    raw = (text or "").strip().rstrip("%").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_stats_line(text: str) -> tuple[float | None, float | None]:
    """解析 :data:`STATS_FORMAT` 的输出 → ``(cpu_percent, rss_bytes)``。"""
    raw = (text or "").strip()
    if not raw:
        return None, None
    cpu_text, _, mem_text = raw.partition("|")
    if not mem_text:
        return parse_cpu_percent(cpu_text), None
    return parse_cpu_percent(cpu_text), parse_mem_usage(mem_text)


def format_bytes(value: float | None) -> str:
    """字节数 → ``42.3MiB`` 这样的短文本。"""
    if value is None:
        return "n/a"
    size = float(value)
    for unit, factor in (("GiB", 1024.0**3), ("MiB", 1024.0**2), ("KiB", 1024.0)):
        if size >= factor:
            return f"{size / factor:.1f}{unit}"
    return f"{size:.0f}B"


# ---------------------------------------------------------------------------
# 判定边界（边界值本身是**通过**）
# ---------------------------------------------------------------------------
def failures_at_or_below(failures: int, samples: int, rate: float) -> bool:
    """失败率是否 ``<= rate``（恰好等于算通过）。

    ``samples <= 0`` 时返回 ``False``：一次都没采样成功不能算通过。
    """
    if samples <= 0:
        return False
    return (failures / samples) <= rate + 1e-12


def growth_ratio_ok(ratio: float, limit: float) -> bool:
    """内存增长比是否 ``<= limit``（恰好等于算通过）。"""
    return ratio <= limit + 1e-12


def thirds_means(values: Sequence[float]) -> tuple[float | None, float | None]:
    """前 1/3 与后 1/3 的均值；样本不足 3 个时返回 ``(None, None)``。

    两段长度都取 ``len // 3``（向下取整），保证「前段」与「后段」不重叠。
    """
    size = len(values) // 3
    if size < 1:
        return None, None
    head = values[:size]
    tail = values[-size:]
    return sum(head) / len(head), sum(tail) / len(tail)


def ascii_bar(value: float | None, maximum: float | None, width: int = 28) -> str:
    """按最大值缩放的 ASCII 条形（``None``/0 → 全空）。"""
    if value is None or maximum is None or maximum <= 0 or value <= 0:
        return " " * width
    filled = int(round(width * min(value / maximum, 1.0)))
    return "#" * max(filled, 1) + " " * max(width - filled, 0)
