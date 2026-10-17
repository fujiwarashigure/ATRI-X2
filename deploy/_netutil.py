"""HTTP 探针与 TCP 兜底：标准库版 ``curl -fsS --max-time N``。

对应上游：各脚本里的 ``curl -fsS --max-time 5 <url>``（watchdog 的 /live、
/ready、/healthz、/tasks、/metrics 探活，backup/rollback/upgrade 的
``compose exec ... curl ... /ready``，以及 restore-verify 的隔离实例探活）。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。

约定的失败表示：**状态码 0 = 连不上**（DNS / 拒绝 / 超时），错误文本放在
body 里。这样调用方统一按 ``http_ok()`` 判断即可，不需要区分异常类型——
与 ``curl ... || true`` 之后只看到空输出的行为一致。
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

__all__ = [
    "http_get",
    "http_ok",
    "http_post_json",
    "http_status",
    "post_json",
    "probe_health",
    "tcp_open",
]


def http_post_json(
    url: str,
    payload: object,
    *,
    timeout: float = 5.0,
    token: str = "",
) -> tuple[int, str]:
    """``curl -fsS -X POST -H 'Content-Type: application/json' -d <json>`` 的等价物。

    用于 watchdog 的 webhook 通知。同样遵守「失败 = 状态码 0」的约定，
    调用方只需判断 :func:`http_ok` 的语义。
    """
    import json as _json

    body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), exc.read(65536).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 错误体读不出来
            return int(exc.code), ""
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一降级为 0
        return 0, f"{type(exc).__name__}: {exc}"


#: 语义更直白的别名（调用点用它更像 curl -X POST）。
post_json = http_post_json


def http_get(
    url: str,
    *,
    timeout: float = 5.0,
    token: str = "",
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str]:
    """``curl -sS --max-time N`` 的等价物：返回 ``(状态码, 正文)``。

    ``max_bytes`` 是防御性的：诊断包会抓 ``/metrics`` 与 ``/tasks``，不能因为
    对端吐了一个巨大响应就把运维机的内存吃光。

    HTTP 错误（4xx/5xx）**算成功拿到响应**，返回真实状态码与错误正文——
    这样 ``/healthz`` 的 503 能被诊断包完整记录下来。
    """
    request = urllib.request.Request(url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"
            return int(response.status), body.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 错误体读不出来
            body = ""
        return int(exc.code), body
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一降级为 0
        return 0, f"{type(exc).__name__}: {exc}"


def http_status(url: str, *, timeout: float = 5.0, token: str = "") -> int:
    """只取状态码；连不上返回 0。"""
    status, _ = http_get(url, timeout=timeout, token=token)
    return status


def http_ok(url: str, *, timeout: float = 5.0, token: str = "") -> bool:
    """``curl -fsS`` 的等价物：2xx 才算成功。"""
    status = http_status(url, timeout=timeout, token=token)
    return 200 <= status < 300


def probe_health(url: str, *, timeout: float = 5.0, token: str = "") -> dict:
    """探健康接口并返回结构化结果（``--health-probe`` 与兼容开关共用）。

    返回键：``url`` / ``reachable`` / ``status`` / ``body`` / ``error``。
    **键名一旦写进 JSON 就不要再动**（探针输出可能被编排系统解析）。
    """
    status, body = http_get(url, timeout=timeout, token=token)
    return {
        "url": url,
        "reachable": status != 0,
        "status": status,
        "body": body,
        "error": "" if status != 0 else body,
    }


def tcp_open(host: str, port: int, *, timeout: float = 3.0) -> bool:
    """TCP 端口是否可连（健康探测的最后一层兜底）。"""
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()
