"""``python -m atri_x.agent.dsh.smoke``：DSH ACP 最小闭环冒烟。

流程：``dsh --profile acp`` → ``initialize`` → ``session/new`` →
（可选）``session/set_config_option`` → ``session/prompt``（流式消费
``session/update``）→ 打印 ``stopReason`` 与真实 token 用量 → ``session/close``。

启动命令的解析顺序（**故意让内置内核优先**，见 :mod:`atri_x.sidecar`）：

1. ``--command`` 显式指定；
2. ``DSH_ACP_COMMAND`` 环境变量；
3. 发行包内置的 DSH 内核（``runtime/dsh-kernel``，随包安装，不依赖 PATH 上的 ``dsh``）；
4. 最后才回落到 PATH 上的 ``dsh --profile acp``。

用法::

    python -m atri_x.agent.dsh.smoke
    python -m atri_x.agent.dsh.smoke --prompt "用一句话解释 ACP" \\
        --model deepseek-v4-flash --show-events
    python -m atri_x.agent.dsh.smoke --command "node /path/bin.js --profile acp"

退出码：0 成功（``stopReason=end_turn``）；1 失败（协议/进程/超时/其他停止原因）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from atri_x.agent.dsh.client import AcpClient
from atri_x.agent.dsh.command import DEFAULT_COMMAND_FALLBACK, format_command, resolve_command
from atri_x.agent.dsh.types import AcpError, AcpEvent
from atri_x.redact import secrets

__all__ = ["main"]

_DEFAULT_PROMPT = "用一句话回答：1+1 等于几？只输出答案。"
_DEFAULT_COMMAND = DEFAULT_COMMAND_FALLBACK


def _write(line: str) -> None:
    """按当前控制台编码安全输出（Windows GBK 控制台不吞异常）。"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _resolve_cwd(explicit: str | None) -> str:
    """工作目录：显式传入 > ``DSH_WORKSPACE`` > 镜像约定的 ``/data/dsh-ws`` > 当前目录。"""
    for candidate in (explicit, os.environ.get("DSH_WORKSPACE")):
        if candidate:
            return os.path.abspath(candidate)
    if os.path.isdir("/data/dsh-ws"):
        return "/data/dsh-ws"
    return os.getcwd()


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析命令行。"""
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.agent.dsh.smoke",
        description="DSH ACP 最小闭环冒烟：initialize → session/new → prompt → close",
    )
    parser.add_argument(
        "--command",
        default="",
        help=(
            "启动命令；为空时依次取 DSH_ACP_COMMAND → 内置 DSH 内核 → "
            f"{_DEFAULT_COMMAND!r}"
        ),
    )
    parser.add_argument("--cwd", default=None, help="会话工作目录（必须是绝对路径；默认见 --help 说明）")
    parser.add_argument("--prompt", default=_DEFAULT_PROMPT, help="提示词")
    parser.add_argument("--model", default=None, help="会话模型 id（来自 initialize 公布的可配置项）")
    parser.add_argument("--reasoning-effort", default=None, help="推理强度（模型支持时）")
    parser.add_argument("--timeout", type=float, default=180.0, help="单轮超时秒数（默认 180）")
    parser.add_argument("--show-events", action="store_true", help="逐条打印 session/update 事件")
    parser.add_argument("--json", action="store_true", help="额外输出一行 JSON 摘要")
    return parser.parse_args(argv)


def _event_printer(enabled: bool):
    """构造事件打印回调；未开启时返回 ``None``。"""
    if not enabled:
        return None

    def _on_event(event: AcpEvent) -> None:
        if event.kind == "usage_update":
            _write(f"[dsh-acp] event=usage_update used={event.used_tokens} size={event.context_size}")
            return
        if event.kind in {"tool_call", "tool_call_update"}:
            _write(f"[dsh-acp] event={event.kind} status={event.tool_status} title={event.tool_title}")
            return
        if event.text:
            _write(f"[dsh-acp] event={event.kind} text={event.text!r}")

    return _on_event


def _summary(info: Any, result: Any, session_id: str) -> dict[str, Any]:
    """组装 JSON 摘要（不依赖服务端对象，全部来自已收敛的数据）。"""
    return {
        "ok": result.stop_reason == "end_turn",
        "sessionId": session_id,
        "protocolVersion": info.protocol_version,
        "agent": f"{info.agent_name}/{info.agent_version}".strip("/"),
        "stopReason": result.stop_reason,
        "usage": result.usage.to_dict(),
        "usedTokens": result.used_tokens,
        "contextSize": result.context_size,
        "eventCount": len(result.events),
        "elapsedSeconds": round(result.elapsed_seconds, 3),
        "answer": result.text,
    }


def _resolve_command(explicit: str) -> Sequence[str] | str:
    """解析启动命令：显式 > 环境变量 > 内置内核 > PATH 上的 dsh。

    实现在 :mod:`atri_x.agent.dsh.command`（路由与冒烟共用**唯一出处**）；这里
    只是把"都找不到"回退成默认命令字符串，让失败信息更可读。
    """
    return resolve_command(explicit) or _DEFAULT_COMMAND


def _format_command(command: Sequence[str] | str) -> str:
    """把命令显示成一行（内置内核路径带空格时加引号）。"""
    return format_command(command)


def main(argv: Sequence[str] | None = None) -> int:
    """跑一次完整闭环；返回进程退出码。"""
    args = _parse_args(argv)
    cwd = _resolve_cwd(args.cwd)
    command = _resolve_command(args.command)
    _write(f"[dsh-acp] command: {secrets(_format_command(command))}")
    _write(f"[dsh-acp] cwd: {secrets(cwd)}")
    client = AcpClient(
        command,
        prompt_timeout=args.timeout,
        on_event=_event_printer(args.show_events),
    )
    try:
        info = client.start()
        _write(f"[dsh-acp] initialize: protocol={info.protocol_version} agent={info.agent_name}/{info.agent_version}")
        session = client.new_session(cwd)
        _write(f"[dsh-acp] session: {session.session_id} cwd={session.cwd}")
        model = session.current_value("model")
        if args.model and args.model != model:
            client.set_config_option(session.session_id, "model", args.model)
            model = args.model
        effort = session.current_value("reasoning_effort")
        if args.reasoning_effort and args.reasoning_effort != effort:
            client.set_config_option(session.session_id, "reasoning_effort", args.reasoning_effort)
            effort = args.reasoning_effort
        _write(f"[dsh-acp] config: model={model or '-'} reasoning_effort={effort or '-'}")
        _write(f"[dsh-acp] prompt: {secrets(args.prompt)}")
        result = client.prompt(session.session_id, args.prompt)
        _write(f"[dsh-acp] stopReason={result.stop_reason} elapsed={result.elapsed_seconds:.2f}s")
        usage = result.usage
        _write(
            "[dsh-acp] tokens: "
            f"input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens} "
            f"cachedRead={usage.cached_read_tokens} cachedWrite={usage.cached_write_tokens} "
            f"thought={usage.thought_tokens}"
        )
        if result.used_tokens or result.context_size:
            _write(f"[dsh-acp] context: used={result.used_tokens} size={result.context_size}")
        if result.thought:
            _write(f"[dsh-acp] thought({len(result.thought)} chars)")
        _write(f"[dsh-acp] answer: {result.text}")
        summary = _summary(info, result, session.session_id)
        if args.json:
            _write("[dsh-acp] summary: " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
        if result.stop_reason != "end_turn":
            _write(f"[dsh-acp] failed: unexpected stopReason={result.stop_reason!r}")
            return 1
        _write("[dsh-acp] ok")
        return 0
    except AcpError as exc:
        _write(f"[dsh-acp] error: {secrets(str(exc))}")
        tail = client.stderr_tail()
        if tail:
            _write("[dsh-acp] stderr tail:")
            for line in tail.splitlines()[-20:]:
                _write("  " + secrets(line))
        return 1
    finally:
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
