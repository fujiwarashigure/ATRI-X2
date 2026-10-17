"""Atrinis ``direct`` 模式：执行平台调用、投递给目标、回填消息映射。

对应 Go：``internal/atrinis/direct.go``。

``direct`` 的语义是"立刻投出去"：先把请求里的平台调用按顺序执行完，再把正文/媒体
发给每一个已解析的目标。顺序不能颠倒——调用（例如撤消息、禁言）往往是"发通知
之前的准备动作"。

两处与 Go 一致但容易忽略的细节：

* 平台调用的平台名允许缺省，缺省时**回落到第一个目标**的平台（Go 的
  ``targets[0].Platform``）；所以 ``calls`` 与 ``targets`` 的先后顺序是有意义的。
* 投递用的是 :func:`atri_x.delivery.with_temporary_connection` 包装过的 ctx：
  目标可能不在当前会话连接上，投递层据此走临时连接而不是复用当前会话。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from atri_x.atriena import (
    Call,
    StatusCompleted,
    Target,
    TargetScopeID,
    resolve_raw_call,
)
from atri_x.atrinis.background import maybe_await
from atri_x.atrinis.targets import decode_resolved_targets
from atri_x.delivery import Output, Receipt, with_temporary_connection
from atri_x.errors import InvalidError
from atri_x.storage import PlatformMessageMap

__all__ = ["DirectMixin"]


class DirectMixin:
    """``direct`` 模式的执行路径。对应 Go ``direct.go`` 里的 ``Service`` 方法。"""

    async def run_direct(self, ctx: Any, event: Any, event_id: str) -> None:
        """执行 direct 事件。对应 Go ``runDirect``（失败时抛异常）。

        平台调用先跑；只有"有目标且确有正文或段"时才真正发送。
        """
        if self.send is None:
            raise InvalidError("atrinis sender is not configured")
        req = event.request
        resolved = decode_resolved_targets(event.resolved_targets)
        await self.execute_calls(ctx, resolved, req.calls)
        if resolved and ((req.content or "").strip() or req.segments):
            outputs = await self.direct_media_outputs(ctx, event, event_id)
            await self.send_outputs_to_targets(ctx, resolved, outputs)
        self.complete_event(ctx, event_id, event.resolved_targets, StatusCompleted, "", "")
        return None

    async def execute_calls(
        self, ctx: Any, targets: Sequence[Target] | None, calls: Sequence[Call] | None
    ) -> None:
        """按顺序执行平台调用。对应 Go ``executeCalls``。

        与 :func:`atri_x.atriena.execute_calls` 的差别：这里在调用未声明平台时
        回落到第一个目标的平台（Go 的 atrinis 版本特有），并且只关心成功/失败，
        不收集 :class:`atri_x.atriena.CallResult`。
        """
        if not calls:
            return None
        if self.platform_callers is None:
            raise InvalidError("platform api callers are not configured")
        for call in calls:
            raw_call = resolve_raw_call(call)
            platform = (raw_call.platform or "").strip()
            if not platform and targets:
                platform = targets[0].platform
            caller, ok = self.platform_callers.platform_caller(platform)
            if not ok or caller is None:
                raise InvalidError(f"platform {platform!r} does not support api calls")
            await maybe_await(caller.call_platform_api(ctx, raw_call.api, raw_call.params))
        return None

    async def send_outputs_to_targets(
        self, ctx: Any, targets: Sequence[Target] | None, outputs: Sequence[Output] | None
    ) -> None:
        """把输出发给每个目标。对应 Go ``sendOutputsToTargets``。"""
        await self.send_outputs_to_targets_mapped(ctx, "", targets, outputs, "", "")
        return None

    async def send_outputs_to_targets_mapped(
        self,
        ctx: Any,
        event_key: str,
        targets: Sequence[Target] | None,
        outputs: Sequence[Output] | None,
        session_id: str,
        message_id: str,
    ) -> None:
        """发送并回填平台消息映射。对应 Go ``sendOutputsToTargetsMapped``。

        任何一个目标发送失败即中断（与 Go 一致，不做部分成功的收集）。
        """
        wrapped = with_temporary_connection(ctx)
        for target in targets or []:
            receipt = await maybe_await(
                self.send(wrapped, target.to_delivery_target(), list(outputs or []))
            )
            self.map_report_receipt(wrapped, event_key, target, session_id, message_id, receipt)
        return None

    def map_report_receipt(
        self,
        ctx: Any,
        event_key: str,
        target: Target,
        session_id: str,
        message_id: str,
        receipt: Receipt | None,
    ) -> None:
        """把发送回执里的平台消息 ID 映射回会话消息。对应 Go ``mapReportReceipt``。

        缺任一前提（无 session、无 message、store 未配置、目标没有 scope）都直接
        返回；单条映射失败只记审计与告警，**不影响**已经完成的投递。
        """
        if (
            not session_id
            or not message_id
            or self.store is None
            or self.store.messages() is None
        ):
            return
        scope_id = TargetScopeID(target)
        if not scope_id:
            return
        for platform_message_id in (receipt.platform_message_ids if receipt else []) or []:
            text = (platform_message_id or "").strip()
            if not text:
                continue
            mapping = PlatformMessageMap(
                platform=target.platform,
                platform_scope_id=scope_id,
                platform_message_id=text,
                session_id=session_id,
                message_id=message_id,
            )
            try:
                self.store.messages().map_platform_message(mapping)
            except Exception as exc:  # noqa: BLE001 - 映射失败不推翻投递结果
                self.audit_event(
                    "atrinis.report_map_failed",
                    "event_key",
                    event_key,
                    "platform",
                    target.platform,
                    "scope_id",
                    scope_id,
                    "platform_message_id",
                    text,
                    "session_id",
                    session_id,
                    "message_id",
                    message_id,
                    "error",
                    str(exc),
                )
                self.log_warn(
                    "map atrinis report message failed",
                    "event_key",
                    event_key,
                    "platform",
                    target.platform,
                    "scope_id",
                    scope_id,
                    "platform_message_id",
                    text,
                    "session_id",
                    session_id,
                    "message_id",
                    message_id,
                    "error",
                    str(exc),
                )
