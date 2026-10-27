"""``python -m atri_x.platform.qq_onebot.smoke``：OneBot 协议端连通性冒烟。

为什么需要它
------------
QQ 链路的故障九成出在"协议端没开 / 端口不对 / token 不对 / 事件推不过来"，这四类
问题**都不需要模型**就能判断，却往往被误当成"消息没有回复"去查 Agent。本命令只做
四件事，把一个 OneBot v11 协议端逐层验完：

1. **连**：按 ``ws_url`` 建 WebSocket（带 ``access_token`` 时用 ``Bearer`` 头）；
2. **调**：发 ``get_login_info``，验证动作通道并拿到机器人 QQ 号与昵称；
3. **发**（可选）：``--group`` / ``--private`` 发一条文本，验证发送通道；
4. **收**（可选）：``--listen N`` 监听 N 秒，打印上报事件，验证事件推送方向。

与 ``atri-x doctor`` 的分工：doctor 站在**装配层**看平台是否配起来；本命令站在
**协议层**看这条 WebSocket 到底通不通，不读 ``app.toml``、不装配 Agent、不调模型
（与 ``--health-probe`` 同样的取舍）。因此它也能对着测试桩跑，见
``tests/test_qq_onebot_smoke.py``。

用法::

    python -m atri_x.platform.qq_onebot.smoke
    python -m atri_x.platform.qq_onebot.smoke --url ws://127.0.0.1:3001/ --json
    python -m atri_x.platform.qq_onebot.smoke --group 123456 --text "连通性测试"
    python -m atri_x.platform.qq_onebot.smoke --listen 20 --json

冻结后的 exe 没有解释器，用入口内置的 ``-m`` 转发跑同名模块::

    atri-x.exe -m atri_x.platform.qq_onebot.smoke --json

退出码：``0`` 连接 + ``get_login_info`` 都成功（且可选步骤没失败）；否则 ``1``。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from typing import Any, Sequence, TextIO

from atri_x.platform.qq_onebot.backend import (
    PROTOCOL_BACKENDS,
    detect_backend,
    resolve_protocol_backend,
)
from atri_x.platform.qq_onebot.config import DEFAULT_WS_URL
from atri_x.platform.qq_onebot.transport import Transport
from atri_x.platform.qq_onebot.types import Event

#: 事件正文的截断长度（冒烟输出用，不需要全文）。
TEXT_LIMIT = 80
#: 默认访问令牌环境变量名，与 ``[platform.qqonebot].access_token_env`` 的示例一致。
DEFAULT_TOKEN_ENV = "QQONEBOT_ACCESS_TOKEN"
#: 默认 URL 环境变量名。
DEFAULT_URL_ENV = "QQONEBOT_WS_URL"


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回退出码。"""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = asyncio.run(run(args))
    except KeyboardInterrupt:  # Ctrl+C 视为正常中断
        return 1
    except Exception as exc:  # noqa: BLE001 - 冒烟工具，任何异常都归为“不通”
        _emit(args, {"ok": False, "url": args.url, "error": _error_text(exc)})
        _err("[qqbot] %s\n" % _error_text(exc))
        return 1
    _emit(args, report)
    return 0 if report.get("ok") else 1


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.platform.qq_onebot.smoke",
        description="OneBot v11 protocol endpoint smoke test (connect / call / send / listen).",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get(DEFAULT_URL_ENV, "") or DEFAULT_WS_URL,
        help="protocol endpoint ws url (default: $%s or %s)" % (DEFAULT_URL_ENV, DEFAULT_WS_URL),
    )
    parser.add_argument("--access-token", default="", help="literal access token")
    parser.add_argument(
        "--access-token-env",
        default=DEFAULT_TOKEN_ENV,
        help="env var holding the access token (default: %s)" % DEFAULT_TOKEN_ENV,
    )
    parser.add_argument("--group", type=int, default=0, help="send a text to this group id")
    parser.add_argument("--private", type=int, default=0, help="send a text to this user id")
    parser.add_argument("--text", default="ATRI-X 连通性测试", help="text to send")
    parser.add_argument(
        "--backend",
        default="auto",
        choices=("auto", *PROTOCOL_BACKENDS),
        help=(
            "protocol backend: auto (detect from get_version_info) / "
            + " / ".join(PROTOCOL_BACKENDS)
        ),
    )
    parser.add_argument(
        "--listen", type=float, default=0.0, help="seconds to listen for reported events"
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="per-action timeout seconds")
    parser.add_argument("--json", action="store_true", help="print one JSON object as result")
    return parser.parse_args(list(argv))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """跑一遍握手 / 动作 / 发送 / 监听，返回结构化结果。

    报告是**逐项**的 ``checks: [{name, ok, detail}]``，方便一眼看出卡在哪一层；
    同时保留 ``ok`` / ``self_id`` / ``nickname`` / ``sent`` / ``events`` 这些老字段。
    """
    url = (args.url or "").strip()
    token = resolve_access_token(args.access_token, args.access_token_env)
    configured_backend = _resolve_backend(args.backend)
    transport = Transport(
        url=url,
        access_token=token,
        timeout=args.timeout,
        backend=configured_backend,
    )
    report: dict[str, Any] = {
        "ok": False,
        "url": url,
        "auth": bool(token),
        "backend": configured_backend or "auto",
        "checks": [],
    }

    def check(name: str, ok: bool, detail: str = "") -> bool:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    _out("[qqbot] connecting: %s (token=%s)\n" % (url, "yes" if token else "no"))
    await transport.connect(None)
    check("connect", True, url)
    # 读循环必须与 call 并发跑：动作响应靠它 dispatch 回等待方；上报事件靠它进队列。
    events: asyncio.Queue = asyncio.Queue()
    pump = asyncio.ensure_future(_pump(transport, events))
    try:
        login = await transport.get_login_info(None)
        report["self_id"] = login.user_id
        report["nickname"] = login.nickname
        report["login"] = login.logged_in()
        check(
            "login",
            login.logged_in(),
            "self_id=%s nickname=%s" % (login.user_id, login.nickname or "?"),
        )
        _out(
            "[qqbot] connected: self_id=%s nickname=%s\n"
            % (report["self_id"], report["nickname"] or "?")
        )

        # 协议端自报家门：识别"配置的后端 vs 实际连上的后端"是否一致。
        version = await _probe(transport.get_version_info(None))
        if version is not None:
            report["app_name"] = version.app_name
            report["app_version"] = version.app_version
            report["detected_backend"] = detect_backend(version.app_name)
            check(
                "version",
                True,
                "%s %s" % (version.app_name or "?", version.app_version or ""),
            )
            check(
                "backend_match",
                not configured_backend
                or not report["detected_backend"]
                or report["detected_backend"] == configured_backend,
                "configured=%s connected=%s"
                % (configured_backend or "auto", report["detected_backend"] or "unknown"),
            )
        for action, label in (("can_send_image", "image"), ("can_send_record", "record")):
            probe = getattr(transport, action)
            allowed = await _probe(probe(None))
            check("can_send_" + label, bool(allowed), "yes" if allowed else "no")

        sent: list[dict[str, Any]] = []
        if args.group:
            message_id = await transport.send_group_message(None, args.group, args.text)
            sent.append({"target": "group", "id": args.group, "message_id": message_id})
            check("send_group", bool(message_id), "message_id=" + (message_id or "-"))
            _out("[qqbot] sent group %d message_id=%s\n" % (args.group, message_id))
        if args.private:
            message_id = await transport.send_private_message(None, args.private, args.text)
            sent.append({"target": "private", "id": args.private, "message_id": message_id})
            check("send_private", bool(message_id), "message_id=" + (message_id or "-"))
            _out("[qqbot] sent private %d message_id=%s\n" % (args.private, message_id))
        report["sent"] = sent

        collected = await _drain(events, args.listen)
        report["events"] = collected
        report["event_count"] = len(collected)
        if args.listen > 0:
            check("listen", True, "events=%d" % len(collected))
        report["ok"] = bool(report["login"])
        _out("[qqbot] ok (events=%d)\n" % len(collected))
        return report
    finally:
        pump.cancel()
        with contextlib.suppress(BaseException):
            await pump
        await transport.close()


def _resolve_backend(value: str) -> str:
    """``--backend``：``auto`` 交给 ``get_version_info`` 识别，其余按显式后端。"""
    text = (value or "").strip().lower()
    if text in ("", "auto"):
        return ""
    return resolve_protocol_backend(text)


async def _probe(coro: Any) -> Any:
    """跑一个可选动作；``None`` 表示协议端不支持（不当作失败）。"""
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 可选能力探测，失败就是"没有"
        return None


async def _pump(transport: Transport, events: "asyncio.Queue[Event]") -> None:
    """后台读循环：动作响应就地分发，上报事件塞进队列。"""
    while True:
        try:
            event = await transport.read(None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 连接断开即结束读循环
            return
        if event is not None:
            events.put_nowait(event)


async def _drain(events: "asyncio.Queue[Event]", seconds: float) -> list[dict[str, Any]]:
    """收集 ``seconds`` 秒内到达的上报事件（``0`` 表示不监听）。"""
    if seconds <= 0:
        return []
    deadline = time.monotonic() + seconds
    collected: list[dict[str, Any]] = []
    _out("[qqbot] listening %.1fs for events...\n" % seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(events.get(), timeout=remaining)
        except TimeoutError:
            break
        item = summarize_event(event)
        collected.append(item)
        _out("[qqbot] event: %s\n" % format_event(item))
    return collected


def summarize_event(event: Event) -> dict[str, Any]:
    """把一条上报事件压成冒烟输出需要的字段。"""
    return {
        "post_type": event.post_type,
        "message_type": event.message_type,
        "notice_type": event.notice_type,
        "self_id": event.self_id,
        "user_id": event.user_id,
        "group_id": event.group_id,
        "message_id": event.message_id,
        "raw_message": _clip(event.raw_message),
    }


def format_event(item: dict[str, Any]) -> str:
    """单行渲染事件摘要（ASCII 友好）。"""
    parts = ["post_type=%s" % (item.get("post_type") or "?")]
    if item.get("message_type"):
        parts.append("type=%s" % item["message_type"])
    if item.get("notice_type"):
        parts.append("notice=%s" % item["notice_type"])
    if item.get("group_id"):
        parts.append("group=%s" % item["group_id"])
    if item.get("user_id"):
        parts.append("user=%s" % item["user_id"])
    if item.get("raw_message"):
        parts.append("text=%s" % item["raw_message"])
    return " ".join(parts)


def resolve_access_token(explicit: str, env_name: str) -> str:
    """取访问令牌：``--access-token`` 优先，其次读环境变量。"""
    value = (explicit or "").strip()
    if value:
        return value
    name = (env_name or "").strip()
    if not name:
        return ""
    return (os.environ.get(name) or "").strip()


def _emit(args: argparse.Namespace, report: dict[str, Any]) -> None:
    """``--json`` 时只打一行 JSON，否则打人类可读摘要。"""
    if not args.json:
        if not report.get("ok"):
            error = report.get("error") or "not ready"
            _err("[qqbot] failed: %s\n" % error)
        return
    _out(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clip(text: str) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= TEXT_LIMIT:
        return text
    return text[:TEXT_LIMIT] + "..."


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _out(text: str) -> None:
    _write(sys.stdout, text)


def _err(text: str) -> None:
    _write(sys.stderr, text)


def _write(stream: TextIO | None, text: str) -> None:
    """写文本，GBK 控制台写不了时退 ASCII 转义（PORTING §7.4）。"""
    target = stream if stream is not None else sys.stdout
    try:
        target.write(text)
    except UnicodeEncodeError:
        target.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    flush = getattr(target, "flush", None)
    if callable(flush):
        flush()


if __name__ == "__main__":  # pragma: no cover - 进程引导
    raise SystemExit(main())
