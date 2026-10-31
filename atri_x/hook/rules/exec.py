"""``exec`` 动作：跑一次 hook.v2 一次性插件。

对应 Go：``internal/hook/rules/exec.go``。

一次性流程：宿主连发 ``system.init`` → 收响应 → 发 ``event.handle`` → 收响应
→ 等进程退出。期间插件可以反过来发 ``media.*`` / ``shared.*`` / ``platform.call``
/ ``output.send`` 等请求，宿主逐条应答。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from atri_x.delivery.delivery import Receipt, Target as DeliveryTarget
from atri_x.hook.event import Event, Point
from atri_x.hook.match import event_match_context
from atri_x.hook.output import (
    BuildOptions,
    Group,
    Segment,
    Target,
    build_group,
    build_message_segments,
    decode_json,
)
from atri_x.hook.protocol import (
    MAX_PROTOCOL_FRAME_BYTES,
    LineReader,
    VERSION,
    decode_frame,
    encode_frame,
    event_to_json,
    match_context_to_json,
    new_request,
    short_protocol_line,
    validate_id,
)
from atri_x.hook.protocol.stream import await_cancelable, detached_process_kwargs
from atri_x.hook.rules.action import (
    ActionResult,
    State,
    first_non_empty,
    render,
    rendered_action_target,
    set_text_field,
)
from atri_x.hook.rules.exec_stderr import (
    ExecError,
    ExecStderrLogger,
    ExecStderrTail,
    exec_context_error,
    exec_cwd,
    exec_process_error,
    kill_process_tree,
    pump_stderr,
    with_exec_stderr,
)
from atri_x.hook.rules.types import Action
from atri_x.llm import llm
from atri_x.logging.contract import ModuleHook, ResultFailed, ResultSucceeded

__all__ = [
    "run_exec",
    "read_protocol_line",
    "write_protocol_frame",
    "write_v2_request",
    "write_v2_response",
    "plugin_stdin_write_error",
    "v2_event_result",
    "byte_size",
]

#: 一次性插件必须使用的协议版本。对应 Go ``hookProtocolVersion``。
HOOK_PROTOCOL_VERSION = VERSION
#: 单帧上限。对应 Go ``maxHookProtocolFrameBytes``。
MAX_HOOK_PROTOCOL_FRAME_BYTES = MAX_PROTOCOL_FRAME_BYTES
#: 内联 base64 上限（10 MiB）。对应 Go ``maxHookOutputBase64Bytes``。
MAX_HOOK_OUTPUT_BASE64_BYTES = 10 * 1024 * 1024
#: 大输出的建议文案。对应 Go ``largeOutputRecommendation``。
LARGE_OUTPUT_RECOMMENDATION = (
    "write large media to a file and return outputs[].path or outputs[].url "
    "instead of inline base64"
)


def byte_size(size: int) -> str:
    """人类可读的字节数。对应 Go ``byteSize``。"""
    if size and size % (1024 * 1024) == 0:
        return f"{size // (1024 * 1024)} MiB"
    if size and size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size} bytes"


async def run_exec(
    module: Any, ctx: Any, event: Event, action: Action, state: State
) -> tuple[Event, ActionResult]:
    """跑一个一次性 hook.v2 插件。对应 Go ``Module.runExec``。"""
    from atri_x.safecontext import CancelToken, with_timeout

    argv = [render(arg, event, state) for arg in action.command]
    if not argv or not (argv[0] or "").strip():
        raise ExecError("command is required")
    parent = ctx if isinstance(ctx, CancelToken) else None
    token = (
        with_timeout(parent, float(action.timeout_seconds))
        if action.timeout_seconds > 0
        else parent
    )
    try:
        cwd = exec_cwd(module, action, event, state)
    except ExecError:
        raise
    process_env = module.opts.process_env
    if action.source.process_env.configured():
        process_env = action.source.process_env
    command = process_env.command(argv[0], *argv[1:])
    try:
        proc = await asyncio.create_subprocess_exec(
            *command.args,
            executable=command.executable or None,
            cwd=cwd,
            env=command.env_dict(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, **detached_process_kwargs(),
        )
    except (OSError, ValueError) as exc:
        raise ExecError(str(exc)) from exc

    tail = ExecStderrTail()
    stderr_logger = ExecStderrLogger(module, action, tail)
    stderr_task = asyncio.ensure_future(pump_stderr(proc.stderr, stderr_logger))
    reader = LineReader(proc.stdout)
    waited = False

    async def wait_exec() -> None:
        nonlocal waited
        if waited:
            return
        waited = True
        try:
            await proc.wait()
        finally:
            # 进程已退出 → stderr 管道会有 EOF；先把它抽干，错误信息里才带得上尾巴。
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), 3)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                stderr_task.cancel()
            stderr_logger.flush()

    async def fail(source: BaseException | None) -> ExecError:
        await kill_process_tree(proc)
        wait_error: BaseException | None = None
        try:
            await wait_exec()
        except BaseException as exc:  # noqa: BLE001 - 等待失败不覆盖原因
            wait_error = exc
        # 与 Go 一致：上下文错误优先，其次是"调用方给的原始来源错误"，
        # 只有来源为空时才用进程退出错误。
        error: BaseException | None = exec_context_error(token, action)
        if error is None:
            error = (
                source
                if source is not None
                else exec_process_error(token, action, wait_error)
            )
        wrapped = with_exec_stderr(error, tail.text())
        return wrapped if isinstance(wrapped, ExecError) else ExecError(str(wrapped))

    runtime = {
        "plugin_name": action.source.plugin_name,
        "plugin_dir": action.source.base_dir,
        "config_path": action.source.config_path,
        "rule_name": first_non_empty(action.source.final_name, action.action_name),
        "cwd": cwd,
    }
    init_id = "host:init"
    event_id = "host:event"
    try:
        try:
            await write_v2_request(
                proc,
                init_id,
                "system.init",
                {"version": HOOK_PROTOCOL_VERSION, "runtime": runtime},
            )
        except ExecError as exc:
            raise plugin_stdin_write_error("system.init", exc) from exc
        await read_v2_response(
            module, token, reader, proc, event, action, state, init_id
        )
        params = {
            "event": event_to_json(event),
            "match": match_context_to_json(event_match_context(event)),
            "runtime": runtime,
        }
        try:
            await write_v2_request(proc, event_id, "event.handle", params)
        except ExecError as exc:
            raise plugin_stdin_write_error("event.handle", exc) from exc
        raw = await read_v2_response(
            module, token, reader, proc, event, action, state, event_id
        )
        result, updated = v2_event_result(event, action, state, raw)
        event = updated
    except BaseException as exc:  # noqa: BLE001 - 统一走 fail（杀进程 + 接 stderr）
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise await fail(exc) from exc
    finally:
        _cancel_task(stderr_task)

    # 与 Go 的差异（有意的健壮性提升）：Go 直到 runExec 返回才关 stdin，
    # 若插件在退出前等 stdin EOF 就会死锁。这里在等进程退出前先关掉。
    await _close_stdin(proc)
    try:
        await wait_exec()
    except BaseException as exc:  # noqa: BLE001
        error = with_exec_stderr(exec_process_error(token, action, exc), tail.text())
        raise error if isinstance(error, ExecError) else ExecError(str(error))
    finally:
        _cancel_task(stderr_task)
        if proc.returncode is None:
            await kill_process_tree(proc)
    return event, result


def _cancel_task(task: Any) -> None:
    if task is not None and not task.done():
        task.cancel()


async def _close_stdin(proc: Any) -> None:
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except (OSError, RuntimeError):
        return
    waiter = getattr(stdin, "wait_closed", None)
    if callable(waiter):
        try:
            await waiter()
        except (OSError, RuntimeError, ConnectionError):
            pass


# ---------------------------------------------------------------------------
# 帧读写
# ---------------------------------------------------------------------------
async def write_v2_request(
    proc: Any, id: str, method: str, params: Any
) -> None:
    """写一个 request 帧。对应 Go ``writeV2Request``。"""
    await write_protocol_frame(proc, new_request(id, method, params))


async def write_v2_response(
    proc: Any, id: str, result: Any, source_error: BaseException | None
) -> None:
    """写一个 response 帧。对应 Go ``writeV2Response``。"""
    frame: dict[str, Any] = {
        "type": "response",
        "id": id,
        "ok": source_error is None,
    }
    if source_error is not None:
        frame["error"] = str(source_error)
    else:
        frame["result"] = result if result is not None else {}
    await write_protocol_frame(proc, frame)


async def write_protocol_frame(proc: Any, frame: Any) -> None:
    """写一帧到插件 stdin。对应 Go ``writeProtocolFrame``。"""
    data = encode_frame(frame)
    if len(data) + 1 > MAX_HOOK_PROTOCOL_FRAME_BYTES:
        raise ExecError(
            f"hook protocol stdin frame exceeds "
            f"{byte_size(MAX_HOOK_PROTOCOL_FRAME_BYTES)} limit"
        )
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        raise ExecError("hook process stdin is closed")
    try:
        stdin.write(data + b"\n")
        await stdin.drain()
    except (OSError, ConnectionError, RuntimeError) as exc:
        raise ExecError(str(exc)) from exc


async def read_protocol_line(
    reader: LineReader, token: Any = None, action: Action | None = None
) -> str:
    """读一行协议帧。对应 Go ``readProtocolLine``。"""
    from atri_x.hook.protocol.stream import ProtocolFrameError

    try:
        line = await await_cancelable(reader.read_line(), token)
    except ProtocolFrameError as exc:
        raise ExecError(
            f"hook protocol stdout frame exceeds "
            f"{byte_size(MAX_HOOK_PROTOCOL_FRAME_BYTES)} limit; "
            f"{LARGE_OUTPUT_RECOMMENDATION}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 管道断开/帧超限
        if isinstance(exc, ExecError):
            raise
        context_error = exec_context_error(token, action) if action is not None else None
        if context_error is not None:
            raise context_error from exc
        raise ExecError(f"read hook plugin stdout: {exc}") from exc
    if line is None:
        raise EOFError("hook.v2 plugin closed stdout")
    return line.decode("utf-8", "replace")


async def read_v2_response(
    module: Any,
    token: Any,
    reader: LineReader,
    proc: Any,
    event: Event,
    action: Action,
    state: State,
    wanted_id: str,
) -> str | None:
    """等一个响应，期间处理插件反向请求。对应 Go ``readV2Response``。"""
    while True:
        try:
            raw_line = await read_protocol_line(reader, token, action)
        except EOFError as exc:
            raise ExecError(
                f'hook.v2 plugin closed stdout while waiting for response "{wanted_id}"'
            ) from exc
        except ExecError:
            raise
        line = raw_line.strip()
        if not line:
            continue
        try:
            frame = decode_frame(line)
        except ValueError as exc:
            raise ExecError(
                f"parse hook.v2 stdout frame: {exc}; line={short_protocol_line(line)}"
            ) from exc
        if frame.type == "response":
            try:
                validate_id(frame.id, "host:")
            except ValueError as exc:
                raise ExecError(f"hook.v2 response: {exc}") from exc
            if frame.id != wanted_id:
                raise ExecError(
                    f"unexpected hook.v2 response id {frame.id!r} while waiting "
                    f"for {wanted_id!r}"
                )
            if frame.ok is not True:
                raise ExecError(
                    f"hook.v2 request {wanted_id!r} failed: "
                    f"{frame.error or 'plugin returned ok=false'}"
                )
            return frame.result
        if frame.type == "request":
            try:
                validate_id(frame.id, "plugin:")
            except ValueError as exc:
                raise ExecError(f"hook.v2 request: {exc}") from exc
            result, request_error = await handle_protocol_request(
                module, token, event, action, state, frame.method, frame.params
            )
            try:
                await write_v2_response(proc, frame.id, result, request_error)
            except ExecError as exc:
                raise plugin_stdin_write_error("response", exc) from exc
            if request_error is not None:
                raise ExecError(str(request_error)) from request_error
            continue
        if frame.type == "event":
            if frame.method == "hook.log" and module.logger is not None:
                module.logger.info(
                    "hook.v2 plugin event",
                    rule=first_non_empty(
                        action.source.final_name, action.action_name
                    ),
                    params=frame.params or "",
                )
            continue
        raise ExecError(f"unsupported hook.v2 frame type {frame.type!r}")


def plugin_stdin_write_error(frame: str, err: BaseException) -> ExecError:
    """写 stdin 失败时的错误文案。对应 Go ``pluginStdinWriteError``。"""
    label = (frame or "").strip() or "protocol"
    return ExecError(
        f"write hook plugin stdin {label} frame failed; plugin may have exited "
        f"early or closed stdin: {err}"
    )


def v2_event_result(
    event: Event, action: Action, state: State, raw: Any
) -> tuple[ActionResult, Event]:
    """解析 ``event.handle`` 的结果。对应 Go ``v2EventResult``。"""
    if not raw:
        return ActionResult(), event
    try:
        payload = decode_json(raw, _decode_event_result)
    except Exception as exc:  # noqa: BLE001
        raise ExecError(f"decode hook.v2 event result: {exc}") from exc
    from atri_x.hook.protocol import apply_llm_result

    event = apply_llm_result(event, payload["llm"])
    status = (payload["status"] or "").strip()
    if status and status != "completed":
        raise ExecError(
            f"one-shot hook.v2 returned unsupported status {payload['status']!r}"
        )
    result = ActionResult(
        result=payload["result"],
        error=payload["error"],
        matched=payload["matched"],
        pass_through=payload["pass_through"],
    )
    if payload["outputs"]:
        outputs = build_group(
            Group(
                outputs=payload["outputs"],
                target=payload["target"],
                timing=payload["timing"],
            ),
            BuildOptions(
                base_dir=_source_base_dir(action),
                default_target=rendered_action_target(action, event, state),
                default_timing=render(action.timing, event, state),
            ),
        )
        event.outputs.extend(outputs)
    message = payload["message"]
    if message is not None and message.segments is not None:
        segments = build_message_segments(message.segments, _source_base_dir(action))
        event.message.segments = segments
        if event.point == Point.TOOL_CALL_COMPLETED:
            event.tool.result = llm.segments_text_only(segments)
    elif message is not None and message.text is not None:
        field = (action.field or "").strip() or "message.text"
        event = set_text_field(event, field, message.text)
    if payload["consume"]:
        event.control.consume = True
    if payload["stop_propagation"]:
        event.control.stop_propagation = True
    return result, event


def _decode_event_result(obj: Any) -> dict[str, Any]:
    """``event.handle`` 结果的严格字段映射（含内嵌 ``EventResultBase``）。"""
    from atri_x.hook.output import JSONDecodeError
    from atri_x.hook.output.jsonstrict import (
        require_bool,
        require_string,
        strict_object,
    )
    from atri_x.hook.protocol.frames import EventResultBase

    base_keys = ("status", "outputs", "target", "timing", "pass_through", "message", "llm")
    extra_keys = ("result", "error", "matched", "consume", "stop_propagation")
    if not isinstance(obj, dict):
        raise JSONDecodeError("event result must be a JSON object")
    allowed = set(base_keys) | set(extra_keys)
    for key in obj:
        if key not in allowed:
            raise JSONDecodeError(f'event result: json: unknown field "{key}"')
    base = EventResultBase.from_object(
        {key: value for key, value in obj.items() if key in base_keys}, "event result"
    )
    extra = strict_object(
        {key: value for key, value in obj.items() if key in extra_keys},
        "event result",
        {
            "result": require_string,
            "error": require_string,
            "matched": lambda value, where: (
                None if value is None else require_bool(value, where)
            ),
            "consume": require_bool,
            "stop_propagation": require_bool,
        },
    )
    return {
        "status": base.status,
        "outputs": base.outputs,
        "target": base.target,
        "timing": base.timing,
        "pass_through": base.pass_through,
        "message": base.message,
        "llm": base.llm,
        "result": extra.get("result", ""),
        "error": extra.get("error", ""),
        "matched": extra.get("matched"),
        "consume": extra.get("consume", False),
        "stop_propagation": extra.get("stop_propagation", False),
    }


# ---------------------------------------------------------------------------
# 插件反向请求
# ---------------------------------------------------------------------------
async def handle_protocol_request(
    module: Any,
    ctx: Any,
    event: Event,
    action: Action,
    state: State,
    method: str,
    params: Any,
) -> tuple[Any, BaseException | None]:
    """处理插件反向请求。对应 Go ``handleProtocolRequest``。"""
    try:
        return await _handle_protocol_request(module, ctx, event, action, state, method, params), None
    except BaseException as exc:  # noqa: BLE001 - 转成错误帧
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, exc


async def _handle_protocol_request(
    module: Any,
    ctx: Any,
    event: Event,
    action: Action,
    state: State,
    method: str,
    params: Any,
) -> Any:
    if method.startswith("media."):
        if module.opts.runtime is None:
            raise ExecError("hook runtime is not configured")
        return await module.opts.runtime.media_request(
            ctx, _source_base_dir(action), method, params
        )
    if method.startswith("shared."):
        if module.opts.runtime is None:
            raise ExecError("hook runtime is not configured")
        return module.opts.runtime.shared_state().handle_request(method, params)
    if method == "platform.call":
        return await _platform_call(module, ctx, event, action, state, params)
    if method == "output.send":
        values = _raw_object(params)
        outputs = _protocol_outputs_from_map(
            values,
            action,
            rendered_action_target(action, event, state),
            render(action.timing, event, state),
            "params.outputs",
        )
        receipt = await _send_protocol_output(module, ctx, outputs)
        return {"sent": len(outputs), "receipts": [_receipt_to_json(receipt)]}
    if method == "message.get_reply":
        message_id = event.platform.reply_to_message_id
        return {"message_id": message_id, "available": bool(message_id)}
    if method == "message.get":
        return {"available": False}
    if method == "hook.log":
        if module.logger is not None:
            module.logger.info(
                "hook plugin log",
                rule=first_non_empty(action.source.final_name, action.action_name),
                params=json.dumps(params, ensure_ascii=False)
                if not isinstance(params, str)
                else params,
            )
        return {"ok": True}
    raise ExecError(f"unsupported hook protocol method {method!r}")


async def _platform_call(
    module: Any,
    ctx: Any,
    event: Event,
    action: Action,
    state: State,
    params: Any,
) -> Any:
    """``platform.call``。对应 Go 里 ``handleProtocolRequest`` 的该分支。"""
    values = _raw_object(params)
    platform_name = str(values.get("platform") or "").strip()
    if not platform_name:
        platform_name = event.platform.name
    if event.platform.name and platform_name != event.platform.name:
        raise ExecError(
            f"platform.call can only call current platform {event.platform.name!r}"
        )
    api = str(values.get("api") or "").strip()
    call_params = values.get("params")
    if not isinstance(call_params, dict):
        call_params = {}
    if module.opts.platform_callers is None:
        raise ExecError("platform callers are not configured")
    found = module.opts.platform_callers.platform_caller(platform_name)
    caller = found[0] if isinstance(found, tuple) else found
    ok = found[1] if isinstance(found, tuple) and len(found) > 1 else caller is not None
    if not ok or caller is None:
        raise ExecError(f"platform {platform_name!r} does not support api calls")
    rule = first_non_empty(action.source.final_name, action.action_name)
    try:
        response = await caller.call_platform_api(ctx, api, call_params)
    except Exception as exc:  # noqa: BLE001
        module.audit(
            "hook_platform_call",
            "module",
            ModuleHook,
            "platform",
            platform_name,
            "api",
            api,
            "rule",
            rule,
            "error",
            str(exc),
            "result",
            ResultFailed,
        )
        raise
    module.audit(
        "hook_platform_call",
        "module",
        ModuleHook,
        "platform",
        platform_name,
        "api",
        api,
        "rule",
        rule,
        "result",
        ResultSucceeded,
    )
    if isinstance(response, (bytes, bytearray)):
        response = bytes(response).decode("utf-8", "replace")
    if isinstance(response, str):
        try:
            return json.loads(response)
        except ValueError:
            return response
    return response


def _protocol_outputs_from_map(
    values: dict[str, Any],
    action: Action,
    default_target: Target,
    default_timing: str,
    field_name: str,
) -> list[Any]:
    """解析 ``params.outputs``。对应 Go ``protocolOutputsFromMap``。"""
    from atri_x.hook.output import JSONDecodeError, require_list, strict_object

    raw = values.get("outputs")
    if raw is None:
        raise ExecError(
            f'missing required field "{field_name}"; expected an array of segment objects'
        )
    try:
        specs = [
            Segment.from_object(item, f"{field_name}[{index}]")
            for index, item in enumerate(require_list(raw, field_name))
        ]
    except JSONDecodeError as exc:
        raise ExecError(f'invalid field "{field_name}": {exc}') from exc
    if not specs:
        raise ExecError(f'field "{field_name}" must contain at least one segment object')
    target = Target()
    if values.get("target") is not None:
        try:
            target = Target.from_object(values["target"], "target")
        except JSONDecodeError as exc:
            raise ExecError(f'invalid field "target": {exc}') from exc
    timing = values.get("timing")
    if not isinstance(timing, str):
        timing = ""
    try:
        return build_group(
            Group(outputs=specs, target=target, timing=timing),
            BuildOptions(
                base_dir=_source_base_dir(action),
                default_target=default_target,
                default_timing=default_timing,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise ExecError(str(exc)) from exc


async def _send_protocol_output(module: Any, ctx: Any, outputs: list[Any]) -> Receipt:
    """发送插件请求的输出。对应 Go ``sendProtocolOutput``。"""
    if module.opts.send is not None:
        return await module.opts.send(ctx, DeliveryTarget(), outputs)
    raise ExecError("output sender is not configured")


def _raw_object(params: Any) -> dict[str, Any]:
    """把参数收敛成一个 dict。对应 Go ``rawObject``。"""
    if params is None:
        raise ExecError("missing request params")
    if isinstance(params, dict):
        return params
    if isinstance(params, (bytes, bytearray)):
        params = bytes(params).decode("utf-8", "replace")
    if isinstance(params, str):
        if not params.strip():
            raise ExecError("missing request params")
        try:
            parsed = json.loads(params)
        except ValueError as exc:
            raise ExecError(f"invalid request params: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ExecError("request params must be a JSON object")
        return parsed
    raise ExecError("request params must be a JSON object")


def _source_base_dir(action: Action) -> str:
    return (action.source.base_dir or "").strip()


def _receipt_to_json(receipt: Receipt) -> dict[str, Any]:
    return {
        "PlatformMessageIDs": list(receipt.platform_message_ids),
        "SentMessages": [
            {
                "PlatformMessageID": item.platform_message_id,
                "Platform": item.platform,
                "ScopeID": item.scope_id,
                "OutputIndexes": list(item.output_indexes),
            }
            for item in receipt.sent_messages
        ],
        "Failed": receipt.failed,
        "Failure": receipt.failure,
    }


# 交叉检索：Go 里用到 ``execContextError`` 的 ctx。
_CONTEXT = exec_context_error
