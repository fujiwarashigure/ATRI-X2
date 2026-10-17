"""逐目标逐输出的投递、状态持久化、降级与 receipt 映射。对应 Go：``internal/cron/delivery.go``。

核心语义（对应 architecture ``locator:cron``）：

* 投递状态按**实际收件目标**（``key``）与**实际输出**（``id``，``text`` / ``segment:<i>``）
  两级记录；某个目标成功不影响另一个目标，某个输出成功不影响另一个输出。
* 正常触发与平台连接**补发共用**这套逐目标逐输出状态，差别只有两个：补发按平台过滤、
  且正文加"补发："前缀、附件失败降级为路径/URL 文字。
* 每次状态写入都走 ``compare_and_swap_delivery`` **条件更新**：只写仍启用的当前轮次，
  绝不覆盖配置、也绝不把已禁用的任务重新启用。
* 只有 ``once`` 任务且**所有目标的所有输出**都完成后才条件禁用任务；未完整投递的任务
  保持启用，等下次平台连接继续补发（不重跑 LLM）。
* 降级文字本身也失败时状态停在 ``fallback_pending``，等下次平台连接重发**降级文字**
  （不重新解析附件、不做周期重试）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from atri_x.background import BuildReportSegmentOutput
from atri_x.cron.errors import join_errors
from atri_x.cron.execution import maybe_await
from atri_x.cron.model import (
    CronDeliveryOutputState,
    CronDeliveryState,
    CronDeliveryTargetState,
    DeliveryDelivered,
    DeliveryFallbackDelivered,
    DeliveryFallbackPending,
    DeliveryPending,
    Metadata,
    ScheduleMode,
    cron_sandbox_subdir,
    cron_scope_id,
    cron_session_metadata,
    encode_delivery_state,
    unique_strings,
)
from atri_x.delivery.delivery import KindText, Output, Receipt, Target, is_http_media_source
from atri_x.errors import AtriXError, InvalidError
from atri_x.llm.llm import MessageSegment
from atri_x.security import actor_id
from atri_x.storage.ids import new_id
from atri_x.storage.models import (
    CronJob,
    Message,
    PlatformMessageMap,
    Session,
    SessionStatusActive,
)
from atri_x.tool.sandbox import BackgroundKindCron, SandboxContext

__all__ = [
    "DeliveryMixin",
    "ResolvedCronTarget",
    "cron_sandbox",
    "cron_target_label",
    "delivery_output_ids",
    "delivery_status_done",
    "ensure_delivery_output_state",
    "ensure_delivery_target_state",
    "find_delivery_output_status",
    "find_delivery_target_state",
    "missed_once_report_text",
    "private_scope_id",
    "report_segment_fallback_text",
]


@dataclass
class ResolvedCronTarget:
    """解析后的一个实际收件目标。对应 Go ``resolvedCronTarget``。"""

    key: str = ""
    platform: str = ""
    target: Target | None = None
    map_scope_id: str = ""


class DeliveryMixin:
    """``internal/cron/delivery.go`` 的方法集合（由 :class:`~atri_x.cron.service.Service` 组合）。"""

    async def deliver_prepared(
        self,
        ctx: Any,
        job: CronJob,
        meta: Metadata,
        state: CronDeliveryState,
        platform_filter: str,
        recovery: bool,
    ) -> BaseException | None:
        """投递已经准备好的报告。对应 Go ``deliverPrepared``。

        ``platform_filter`` 非空时只投递给该平台（补发用）；``recovery=True`` 时正文加
        "补发："前缀，附件失败降级为文字。
        """
        if not state.report_ready:
            return None
        for resolved in self.resolve_delivery_targets(meta, job.name):
            if platform_filter and resolved.platform != platform_filter:
                continue
            target_state = ensure_delivery_target_state(state, resolved.key)
            report = (state.report or "").strip()
            if report:
                output_state = ensure_delivery_output_state(target_state, "text")
                if not delivery_status_done(output_state.status):
                    text = report
                    if recovery:
                        text = missed_once_report_text(meta.title, report)
                    err = await self.send_delivery_output(
                        ctx, job.name, resolved, Output(kind=KindText, text=text), state
                    )
                    if err is not None:
                        return err
                    output_state.status = DeliveryDelivered
                    active, persist_err = await self.persist_delivery_state(ctx, job, state)
                    if persist_err is not None or not active:
                        return persist_err
            for index, segment in enumerate(state.report_segments):
                output_state = ensure_delivery_output_state(target_state, f"segment:{index}")
                if delivery_status_done(output_state.status):
                    continue
                if output_state.status == DeliveryFallbackPending:
                    # 上一次补发已把附件降级成文字，但降级文字本身没发出去：
                    # 这次连接只重发降级文字，不重新解析附件。
                    err = await self.send_delivery_output(
                        ctx,
                        job.name,
                        resolved,
                        Output(kind=KindText, text=output_state.fallback_text),
                        state,
                    )
                    if err is not None:
                        return err
                    output_state.status = DeliveryFallbackDelivered
                    active, persist_err = await self.persist_delivery_state(ctx, job, state)
                    if persist_err is not None or not active:
                        return persist_err
                    continue

                out: Output | None = None
                build_err: BaseException | None = None
                try:
                    out = BuildReportSegmentOutput(segment, self.cron_sandbox(job.name))
                except Exception as exc:  # noqa: BLE001
                    build_err = exc
                if build_err is None and out is not None:
                    build_err = await self.send_delivery_output(ctx, job.name, resolved, out, state)
                if build_err is None:
                    output_state.status = DeliveryDelivered
                    active, persist_err = await self.persist_delivery_state(ctx, job, state)
                    if persist_err is not None or not active:
                        return persist_err
                    continue
                if not recovery:
                    return build_err

                # 补发：附件失败在同次补发里降级为路径/URL 文字。
                output_state.status = DeliveryFallbackPending
                output_state.fallback_text = report_segment_fallback_text(segment)
                active, persist_err = await self.persist_delivery_state(ctx, job, state)
                if persist_err is not None or not active:
                    return join_errors(build_err, persist_err)
                err = await self.send_delivery_output(
                    ctx,
                    job.name,
                    resolved,
                    Output(kind=KindText, text=output_state.fallback_text),
                    state,
                )
                if err is not None:
                    return err
                output_state.status = DeliveryFallbackDelivered
                active, persist_err = await self.persist_delivery_state(ctx, job, state)
                if persist_err is not None or not active:
                    return persist_err

        if meta.schedule.mode == ScheduleMode.ONCE and self.delivery_complete(meta, state):
            return await self.disable_completed_delivery(ctx, job.name, job.delivery_token)
        return None

    async def send_delivery_output(
        self,
        ctx: Any,
        job_name: str,
        resolved: ResolvedCronTarget,
        out: Output,
        state: CronDeliveryState,
    ) -> BaseException | None:
        """往一个目标发一条输出。对应 Go ``sendDeliveryOutput``。"""
        return await self.send_outputs_to_platform_target(
            ctx,
            job_name,
            resolved.platform,
            resolved.target,
            [out],
            state.report_session_id,
            state.report_message_id,
            resolved.map_scope_id,
        )

    async def persist_delivery_state(
        self, ctx: Any, job: CronJob, state: CronDeliveryState
    ) -> tuple[bool, BaseException | None]:
        """条件写回投递状态。对应 Go ``persistDeliveryState``。

        令牌不变（``expected == next``），所以这次写入**不会**推进轮次；仓库只在任务
        仍启用且令牌匹配时才写，返回 ``False`` 表示"这一轮已经不属于我"。
        """
        _ = ctx
        try:
            encoded = encode_delivery_state(state)
        except Exception as exc:  # noqa: BLE001
            return False, exc
        try:
            swapped = self.cron_jobs().compare_and_swap_delivery(
                job.id, job.delivery_token, job.delivery_token, encoded
            )
        except Exception as exc:  # noqa: BLE001
            return False, exc
        if not swapped:
            return False, None
        job.delivery_state = encoded
        return True, None

    async def disable_completed_delivery(
        self, ctx: Any, job_name: str, delivery_token: str
    ) -> BaseException | None:
        """条件禁用已完整投递的一次性任务。对应 Go ``disableCompletedDelivery``。"""
        _ = ctx
        try:
            if self.manager is not None:
                await self.manager.disable_job_if_delivery_token(job_name, delivery_token)
            else:
                self.cron_jobs().disable_by_name_if_delivery_token(job_name, delivery_token)
        except Exception as exc:  # noqa: BLE001
            return exc
        return None

    def resolve_delivery_targets(self, meta: Metadata, job_name: str) -> list[ResolvedCronTarget]:
        """把 Metadata 解析成实际收件目标列表。对应 Go ``resolveDeliveryTargets``。

        ``all_enabled_platforms`` 为真时覆盖全部启用平台；否则只投给
        ``source_platform``，收件人是创建者的平台用户 ID。
        """
        pairs: list[tuple[str, list[str]]] = []
        if meta.target.all_enabled_platforms:
            for target in self.enabled_platforms:
                pairs.append((target.name, list(target.superadmin_ids)))
        else:
            creator = (meta.created_by.platform_user_id or "").strip()
            pairs.append((meta.target.source_platform, [creator] if creator else []))
        resolved: list[ResolvedCronTarget] = []
        for raw_name, raw_ids in pairs:
            platform_name = (raw_name or "").strip()
            if not platform_name:
                continue
            ids = unique_strings(raw_ids)
            if not ids:
                resolved.append(
                    ResolvedCronTarget(
                        key=platform_name + "|superadmins",
                        platform=platform_name,
                        target=Target(platform=platform_name, superadmins=True),
                        map_scope_id=cron_scope_id(job_name),
                    )
                )
                continue
            for raw in ids:
                target_id = (raw or "").strip()
                if not target_id:
                    continue
                resolved.append(
                    ResolvedCronTarget(
                        key=f"{platform_name}|private|{target_id}",
                        platform=platform_name,
                        target=Target(platform=platform_name, private_user_id=target_id),
                        map_scope_id=private_scope_id(platform_name, target_id),
                    )
                )
        return resolved

    def delivery_complete(self, meta: Metadata, state: CronDeliveryState) -> bool:
        """本轮是否所有目标的所有输出都已完成。对应 Go ``deliveryComplete``。"""
        if not state.report_ready:
            return False
        output_ids = delivery_output_ids(state)
        for target in self.resolve_delivery_targets(meta, ""):
            target_state = find_delivery_target_state(state, target.key)
            for output_id in output_ids:
                if target_state is None or not delivery_status_done(
                    find_delivery_output_status(target_state, output_id)
                ):
                    return False
        return True

    # -- 无状态发送入口（维护任务 / 通知复用） --------------------------------
    async def send_to_targets(
        self, ctx: Any, job_name: str, meta: Metadata, text: str
    ) -> BaseException | None:
        """按 Metadata 的目标发一条文本。对应 Go ``sendToTargets``。"""
        if meta.target.all_enabled_platforms:
            return await self.send_to_platforms(
                ctx, job_name, self.enabled_platform_names(), text
            )
        return await self.send_to_platforms(ctx, job_name, [meta.target.source_platform], text)

    async def send_to_platforms(
        self, ctx: Any, job_name: str, platforms: list[str], text: str
    ) -> BaseException | None:
        """按平台名列表发一条文本（收件人固定为超管）。对应 Go ``sendToPlatforms``。"""
        return await self.send_outputs_to_platforms(
            ctx, job_name, platforms, [Output(kind=KindText, text=text)]
        )

    async def send_outputs_to_platforms(
        self, ctx: Any, job_name: str, platforms: list[str], outputs: list[Output]
    ) -> BaseException | None:
        """按平台名列表发输出。对应 Go ``sendOutputsToPlatforms``。"""
        return await self.send_outputs_to_platforms_mapped(
            ctx, job_name, platforms, outputs, "", ""
        )

    async def send_outputs_to_platform_targets(
        self,
        ctx: Any,
        job_name: str,
        platforms: list[Any],
        outputs: list[Output],
        session_id: str,
        message_id: str,
    ) -> BaseException | None:
        """按平台目标表发输出。对应 Go ``sendOutputsToPlatformTargets``。"""
        if self.send_target is None:
            return InvalidError("cron target sender is not configured")
        errors: list[BaseException | None] = []
        for platform in platforms:
            platform_name = (platform.name or "").strip()
            if not platform_name:
                continue
            ids = unique_strings(platform.superadmin_ids)
            if not ids:
                errors.append(
                    await self.send_outputs_to_platform_target(
                        ctx,
                        job_name,
                        platform_name,
                        Target(platform=platform_name, superadmins=True),
                        outputs,
                        session_id,
                        message_id,
                        cron_scope_id(job_name),
                    )
                )
                continue
            for raw in ids:
                target_id = (raw or "").strip()
                if not target_id:
                    continue
                errors.append(
                    await self.send_outputs_to_platform_target(
                        ctx,
                        job_name,
                        platform_name,
                        Target(platform=platform_name, private_user_id=target_id),
                        outputs,
                        session_id,
                        message_id,
                        private_scope_id(platform_name, target_id),
                    )
                )
        return join_errors(*errors)

    async def send_outputs_to_platforms_mapped(
        self,
        ctx: Any,
        job_name: str,
        platforms: list[str],
        outputs: list[Output],
        session_id: str,
        message_id: str,
    ) -> BaseException | None:
        """按平台名列表逐输出发送并处理回执。对应 Go ``sendOutputsToPlatformsMapped``。"""
        if self.send_target is None:
            return InvalidError("cron target sender is not configured")
        errors: list[BaseException | None] = []
        for platform_name in unique_strings(platforms):
            if not platform_name:
                continue
            for out in outputs:
                err = await self._send_one(
                    ctx,
                    job_name,
                    platform_name,
                    Target(platform=platform_name, superadmins=True),
                    out,
                    session_id,
                    message_id,
                    cron_scope_id(job_name),
                )
                if err is not None:
                    errors.append(err)
        return join_errors(*errors)

    async def send_outputs_to_platform_target(
        self,
        ctx: Any,
        job_name: str,
        platform_name: str,
        target: Target,
        outputs: list[Output],
        session_id: str,
        message_id: str,
        map_scope_id: str,
    ) -> BaseException | None:
        """往单个具体目标逐输出发送。对应 Go ``sendOutputsToPlatformTarget``。"""
        errors: list[BaseException | None] = []
        for out in outputs:
            err = await self._send_one(
                ctx, job_name, platform_name, target, out, session_id, message_id, map_scope_id
            )
            if err is not None:
                errors.append(err)
        return join_errors(*errors)

    async def _send_one(
        self,
        ctx: Any,
        job_name: str,
        platform_name: str,
        target: Target,
        out: Output,
        session_id: str,
        message_id: str,
        map_scope_id: str,
    ) -> BaseException | None:
        """发出一条输出并处理审计/日志/回执映射。对应 Go ``sendOutputsToPlatformTarget`` 的循环体。"""
        attrs = {
            "job": job_name,
            "platform": platform_name,
            "target": cron_target_label(target),
            "kind": out.kind,
        }
        self.audit_event("cron.send_started", **attrs)
        self.log_info("cron send started", **attrs)
        try:
            receipt = await maybe_await(self.send_target(ctx, target, [out]))
        except Exception as exc:  # noqa: BLE001 - 单个输出失败不该中断其它输出
            err = AtriXError(f"send {platform_name}: {exc}")
            self.audit_event("cron.send_failed", **attrs, error=str(err))
            self.log_warn("cron send failed", **attrs, error=str(err))
            return err
        await self.map_report_receipt(
            ctx, job_name, platform_name, map_scope_id, session_id, message_id, receipt
        )
        self.audit_event("cron.send_completed", **attrs)
        self.log_info("cron send completed", **attrs)
        return None

    async def map_report_receipt(
        self,
        ctx: Any,
        job_name: str,
        platform_name: str,
        scope_id: str,
        session_id: str,
        message_id: str,
        receipt: Receipt | None,
    ) -> None:
        """把发送回执里的平台消息映射回报告消息。对应 Go ``mapReportReceipt``。"""
        _ = ctx
        if not session_id or not message_id or self.store is None:
            return
        messages = self.store.messages()
        if messages is None:
            return
        resolved_scope = (scope_id or "").strip() or cron_scope_id(job_name)
        raw_ids = getattr(receipt, "platform_message_ids", None) or []
        for raw_id in raw_ids:
            platform_message_id = (raw_id or "").strip()
            if not platform_message_id:
                continue
            mapping = PlatformMessageMap(
                platform=platform_name,
                platform_scope_id=resolved_scope,
                platform_message_id=platform_message_id,
                session_id=session_id,
                message_id=message_id,
            )
            try:
                messages.map_platform_message(mapping)
            except Exception as exc:  # noqa: BLE001 - 映射失败只记审计
                self.audit_event(
                    "cron.report_map_failed",
                    job=job_name,
                    platform=platform_name,
                    scope_id=resolved_scope,
                    platform_message_id=platform_message_id,
                    session_id=session_id,
                    message_id=message_id,
                    error=str(exc),
                )
                self.log_warn(
                    "map cron report message failed",
                    job=job_name,
                    platform=platform_name,
                    scope_id=resolved_scope,
                    platform_message_id=platform_message_id,
                    session_id=session_id,
                    message_id=message_id,
                    error=str(exc),
                )

    async def copy_session_to_broadcast_targets(
        self, ctx: Any, source_session_id: str, meta: Metadata, job_name: str
    ) -> None:
        """把报告 Session 复制到其它广播平台。对应 Go ``copySessionToBroadcastTargets``。

        复制出来的 Session 属于各平台的超管，scope 仍是 cron scope；消息**不带**
        parent/reply 关系（它们是另一条会话线）。源平台自己那条不复制。
        """
        _ = ctx
        if source_session_id == "" or self.store is None:
            return
        sessions = self.store.sessions()
        messages_repo = self.store.messages()
        if sessions is None or messages_repo is None:
            return
        source = sessions.get(source_session_id)
        messages = messages_repo.list_by_session(source_session_id)
        for target in self.enabled_platforms:
            if not target.name or target.name == source.platform:
                continue
            owner = _target_owner_id(target)
            if not owner:
                continue
            copy_session = Session(
                id=new_id(),
                owner_id=owner,
                platform=target.name,
                platform_scope_id=cron_scope_id(job_name),
                mode=source.mode,
                title=meta.title,
                status=SessionStatusActive,
                metadata=cron_session_metadata(job_name, source_session_id, True),
            )
            sessions.create(copy_session)
            copy_id = copy_session.id
            for message in messages:
                duplicate = Message(
                    id=new_id(),
                    session_id=copy_id,
                    role=message.role,
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    segments=message.segments,
                    metadata=message.metadata,
                    created_at=message.created_at,
                )
                messages_repo.append(duplicate)
            self.audit_event(
                "cron.session_copied",
                job=job_name,
                source_session_id=source_session_id,
                target_session_id=copy_id,
                platform=target.name,
            )

    def cron_sandbox(self, job_name: str) -> SandboxContext:
        """任务的沙盒上下文。对应 Go ``cronSandbox``。"""
        return cron_sandbox(self.sandbox_root, job_name)


# ---------------------------------------------------------------------------
# 纯函数 helper（对应 delivery.go 的包级函数）
# ---------------------------------------------------------------------------
def delivery_output_ids(state: CronDeliveryState) -> list[str]:
    """本轮的输出 ID 列表（``text`` 与 ``segment:<i>``）。对应 Go ``deliveryOutputIDs``。"""
    ids: list[str] = []
    if (state.report or "").strip():
        ids.append("text")
    for index in range(len(state.report_segments)):
        ids.append(f"segment:{index}")
    return ids


def ensure_delivery_target_state(state: CronDeliveryState, key: str) -> CronDeliveryTargetState:
    """取（或新建）某个目标的投递状态。对应 Go ``ensureDeliveryTargetState``。"""
    for item in state.targets:
        if item.key == key:
            return item
    created = CronDeliveryTargetState(key=key)
    state.targets.append(created)
    return created


def ensure_delivery_output_state(
    state: CronDeliveryTargetState, id: str
) -> CronDeliveryOutputState:
    """取（或新建）某个输出的投递状态。对应 Go ``ensureDeliveryOutputState``。"""
    for item in state.outputs:
        if item.id == id:
            return item
    created = CronDeliveryOutputState(id=id, status=DeliveryPending)
    state.outputs.append(created)
    return created


def find_delivery_target_state(
    state: CronDeliveryState, key: str
) -> CronDeliveryTargetState | None:
    """按 key 查目标状态。对应 Go ``findDeliveryTargetState``。"""
    for item in state.targets:
        if item.key == key:
            return item
    return None


def find_delivery_output_status(state: CronDeliveryTargetState, id: str) -> str:
    """按 ID 查输出状态；查不到按"未投递"处理。对应 Go ``findDeliveryOutputStatus``。"""
    for item in state.outputs:
        if item.id == id:
            return item.status
    return DeliveryPending


def delivery_status_done(status: str) -> bool:
    """该输出是否已经送达（含降级文字送达）。对应 Go ``deliveryStatusDone``。"""
    return status in (DeliveryDelivered, DeliveryFallbackDelivered)


def report_segment_fallback_text(segment: MessageSegment) -> str:
    """附件发送失败时的降级文字。对应 Go ``reportSegmentFallbackText``。"""
    value = (segment.url or "").strip()
    if is_http_media_source(value):
        return f"url {value} 发送失败"
    return f"路径 {value} 附件发送失败"


def missed_once_report_text(title: str, report: str) -> str:
    """补发正文前缀。对应 Go ``missedOnceReportText``。"""
    prefix = (title or "").strip() + "补发："
    body = (report or "").strip()
    if not body:
        return prefix
    return prefix + "\n\n" + body


def cron_target_label(target: Target) -> str:
    """目标的可读标签（审计用）。对应 Go ``cronTargetLabel``。"""
    if target.superadmins:
        return "superadmins"
    if target.private_user_id:
        return "private"
    if target.group_id:
        return "group"
    return "unknown"


def private_scope_id(platform_name: str, id: str) -> str:
    """私聊 scope ID（QQ 官方是 ``c2c:``）。对应 Go ``privateScopeID``。"""
    value = (id or "").strip()
    if not value:
        return ""
    if (platform_name or "").strip() == "qqofficial":
        return "c2c:" + value
    return "private:" + value


def cron_sandbox(sandbox_root: str, job_name: str) -> SandboxContext:
    """任务的沙盒上下文。对应 Go ``cronSandbox``。"""
    return SandboxContext(
        root=sandbox_root,
        dir=os.path.join(sandbox_root, *cron_sandbox_subdir(job_name).split("/")),
        background=True,
        background_kind=BackgroundKindCron,
    )


def _target_owner_id(target: Any) -> str:
    """平台的超管 owner ID。对应 Go ``targetOwnerID``。"""
    ids = unique_strings(getattr(target, "superadmin_ids", None))
    if not ids:
        return ""
    return actor_id(target.name, ids[0])
