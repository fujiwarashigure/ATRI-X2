"""进程 Hook 发来的请求：``tool.call`` / ``hooks.reload`` / ``media.*`` / ``shared.*``。

对应 Go：``internal/hook/runtime/tool_bridge.go``。

这里的函数接收 worker 实例（Go 里这些是 ``*worker`` 的方法）。它们刻意**不**
import :mod:`atri_x.hook.runtime.worker`，避免与 worker 的模块级 import 成环。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from atri_x import security as security_module
from atri_x.delivery.delivery import Receipt, Target
from atri_x.errors import AtriXError
from atri_x.hook.protocol.eventjson import dumps
from atri_x.hook.protocol.frames import Frame
from atri_x.logging.contract import ModuleHook, ResultFailed, ResultSucceeded
from atri_x.security import Actor, Role
from atri_x.tool.tool import CallRequest

__all__ = ["ToolBridgeError", "plugin_request", "call_tool", "schemas"]

#: ``hooks.reload`` 不接受任何参数（对应 Go 的空 map 判定）。
_EMPTY_OBJECTS = (None, "", "{}", "null")


class ToolBridgeError(AtriXError):
    """插件请求失败。对应 Go 里这一层的 ``fmt.Errorf``。"""


@dataclass
class _ToolCallParams:
    name: str = ""
    arguments: Any = None
    tool_context: str = ""
    origin: str = ""
    background: bool = False
    target: Target = field(default_factory=Target)


async def plugin_request(worker: Any, value: Frame) -> tuple[Any, BaseException | None]:
    """分发一个插件请求。对应 Go ``worker.pluginRequest``。"""
    method = value.method or ""
    try:
        if method.startswith("media."):
            token = _with_timeout(worker, float(worker.config.event_timeout_seconds))
            try:
                result = await worker.manager.media_request(
                    token, worker.config.dir, method, value.params
                )
            finally:
                _cancel(token)
            return (result.to_json() if hasattr(result, "to_json") else result), None
        if method.startswith("shared."):
            return worker.manager.shared.handle_request(method, value.params), None
        if method == "hooks.reload":
            if (value.params or "").strip() not in _EMPTY_OBJECTS:
                raise ToolBridgeError("hooks.reload does not accept parameters")
            return await worker.prepare_self_reload(), None
        if method == "tool.call":
            return await call_tool(worker, value.params), None
        raise ToolBridgeError(f"unsupported hook request method {method!r}")
    except BaseException as exc:  # noqa: BLE001 - 统一转成协议错误帧
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None, exc


async def call_tool(worker: Any, raw: Any) -> Any:
    """替插件调用一个已注册工具。对应 Go ``worker.callTool``。"""
    params = _decode_tool_call(raw)
    name = params.name.strip()
    if not name:
        raise ToolBridgeError("tool.call requires name")
    allowed = _contains(worker.config.tools.allow, name)
    if params.background:
        allowed = _contains(worker.config.tools.background_allow, name)
    if not allowed:
        raise ToolBridgeError(
            f"tool {name!r} is not allowed for hook {worker.config.id!r}"
        )
    registry = worker.manager.opts.registry
    if registry is None:
        raise ToolBridgeError("tool registry is not configured")
    registered, ok = registry.get(name)
    if not ok or registered is None:
        raise ToolBridgeError(f"tool {name!r} not found")
    info = registered.info()
    if getattr(info, "foreground_only", False) and params.background:
        raise ToolBridgeError(f"tool {name!r} is foreground-only")

    actor = Actor(id="hook:" + worker.config.id, role=Role.SUPERADMIN)
    own_token: Any = None
    if not params.background or (params.origin or "").strip():
        token = params.tool_context
        if params.background:
            token = params.origin
        from atri_x.hook.runtime.routes import take_tool_context

        context_value = take_tool_context(worker.manager, token, worker.config.id)
        if context_value is None:
            raise ToolBridgeError("invalid, expired or exhausted hook context")
        call_ctx = context_value.context
        event = context_value.event
        actor = Actor(
            id=event.actor.id,
            platform=event.platform.name,
            platform_user_id=event.actor.user_id,
            nickname=event.actor.nickname,
            group_card=event.actor.group_card,
            role=event.actor.role,
            group_role=event.actor.group_role,
            display_name=event.actor.display_name,
        )
    elif params.target.empty():
        raise ToolBridgeError("background tool output requires an explicit target")
    else:
        own_token = _with_timeout(worker, float(worker.config.event_timeout_seconds))
        call_ctx = own_token
    call_ctx = security_module.with_actor(call_ctx, actor)

    started = time.monotonic()
    result: Any = None
    error: BaseException | None = None
    try:
        result = await registered.call(
            call_ctx,
            CallRequest(id=_random_id("plugin"), name=name, arguments=params.arguments),
        )
    except BaseException as exc:  # noqa: BLE001 - 审计后原样上抛
        error = exc
    finally:
        _audit(worker, params, name, actor, started, failed=error is not None)
        _cancel(own_token)
    if error is not None:
        raise error

    if result is None:
        return {
            "content": "",
            "segments": [],
            "warnings": [],
            "receipts": [],
        }
    send = worker.manager.opts.send
    if send is None:
        raise ToolBridgeError("hook output sender is not configured")
    if not params.target.empty():
        for output in result.outputs:
            output.target = params.target
    receipt = await send(call_ctx, params.target, result.outputs)
    return {
        "content": result.content,
        "segments": [segment.to_json() for segment in result.segments],
        "warnings": list(result.warnings),
        "receipts": [_receipt_to_json(receipt)],
    }


def schemas(worker: Any) -> list[Any]:
    """允许清单里的工具 schema。对应 Go ``worker.schemas``。"""
    registry = worker.manager.opts.registry
    if registry is None:
        return []
    out: list[Any] = []
    for name in worker.config.tools.allow:
        registered, ok = registry.get(name)
        if ok and registered is not None:
            out.append(registered.schema())
    return out


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _decode_tool_call(raw: Any) -> _ToolCallParams:
    text = raw
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    if isinstance(text, str):
        if not text.strip():
            raise ToolBridgeError("tool.call requires a JSON object")
        try:
            text = json.loads(text)
        except ValueError as exc:
            raise ToolBridgeError(f"decode tool.call params: {exc}") from exc
    if not isinstance(text, dict):
        raise ToolBridgeError("tool.call params must be a JSON object")
    target_raw = text.get("target")
    target = Target()
    if isinstance(target_raw, dict):
        target = Target(
            platform=str(target_raw.get("platform") or target_raw.get("Platform") or ""),
            scope_id=str(target_raw.get("scope_id") or target_raw.get("ScopeID") or ""),
            private_user_id=str(
                target_raw.get("private_user_id")
                or target_raw.get("PrivateUserID")
                or ""
            ),
            group_id=str(target_raw.get("group_id") or target_raw.get("GroupID") or ""),
            superadmins=bool(
                target_raw.get("superadmins") or target_raw.get("Superadmins")
            ),
        )
    arguments = text.get("arguments")
    if arguments is None:
        arguments = ""
    elif not isinstance(arguments, str):
        arguments = dumps(arguments)
    return _ToolCallParams(
        name=str(text.get("name") or ""),
        arguments=arguments,
        tool_context=str(text.get("tool_context") or ""),
        origin=str(text.get("origin") or ""),
        background=bool(text.get("background")),
        target=target,
    )


def _audit(
    worker: Any,
    params: _ToolCallParams,
    name: str,
    actor: Actor,
    started: float,
    failed: bool,
) -> None:
    audit = worker.manager.opts.audit
    if audit is None:
        return
    invocation = params.tool_context
    if params.background:
        invocation = params.origin
    elapsed_ms = int((time.monotonic() - started) * 1000)
    audit(
        "hook_tool_call",
        "module",
        ModuleHook,
        "hook",
        worker.config.id,
        "invocation",
        invocation,
        "tool",
        name,
        "status",
        "error" if failed else "ok",
        "result",
        ResultFailed if failed else ResultSucceeded,
        "elapsed_ms",
        elapsed_ms,
        "platform",
        actor.platform,
        "user_id",
        actor.platform_user_id,
    )


def _receipt_to_json(receipt: Receipt) -> dict[str, Any]:
    """``delivery.Receipt`` 没有 json tag，Go 输出全部字段。"""
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


def _contains(values: Any, target: str) -> bool:
    for value in values or ():
        if (value or "").strip() == target:
            return True
    return False


def _random_id(prefix: str) -> str:
    from atri_x.hook.runtime.routes import random_id

    return random_id(prefix)


def _with_timeout(worker: Any, timeout: float) -> Any:
    token = worker.manager.root_token
    if timeout <= 0:
        return token
    from atri_x.safecontext import with_timeout

    return with_timeout(token, timeout)


def _cancel(token: Any) -> None:
    if token is None:
        return
    cancel = getattr(token, "cancel", None)
    if callable(cancel):
        cancel()
