"""Direct / LLM 执行、报告生成与 JSON 格式重试。对应 Go：``internal/cron/execution.go``。

一次触发分两步：**准备投递状态**（:meth:`ExecutionMixin.prepare_delivery`）与
**投递**（``delivery.py``）。本模块只负责第一步：

* 用 CAS 开启新一轮（``once``）或每轮刷新（``cron``）投递状态，令牌是 RunID；
* ``direct`` 触发把任务正文当报告（无需 LLM）；
* ``llm`` 触发经注入的 runner 跑 Agent 后台任务，解析严格 JSON 报告；
* 解析失败时**复用同一 Session** 做一次 JSON 格式重试（正常触发则一律新建 Session，
  对应 architecture ``locator:cron`` 的硬约定）；
* 被前台接管的 Session 记成"已完成但无汇报"（``ReportReady=True`` / ``Report=""``），
  既不投递任何文本，也不算失败。
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

from atri_x.atriyph import rule_card
from atri_x.background import (
    DefaultJSONRetryPrompt,
    JSONResult,
    KindCron,
    ParseJSONResult,
    RunRequest,
    RunResult,
)
from atri_x.cron.errors import join_errors
from atri_x.cron.model import (
    CronDeliveryState,
    Metadata,
    ScheduleMode,
    TriggerMode,
    cron_sandbox_subdir,
    cron_scope_id,
    decode_delivery_state,
    encode_delivery_state,
    first_non_empty,
)
from atri_x.errors import InvalidError
from atri_x.security import Actor, RoleSuperadmin, actor_id
from atri_x.storage.models import CronJob
from atri_x.storage.ids import new_id

__all__ = [
    "ExecutionMixin",
    "cron_format_retry_prompt",
    "cron_parse_failed_message",
    "cron_prompt",
    "maybe_await",
    "parse_llm_result",
]


async def maybe_await(value: Any) -> Any:
    """兼容同步/异步注入回调：是 awaitable 就 await。

    注入点（``send_target`` / ``runner.run_background``）在 Go 里都是阻塞调用；
    本仓允许实现方给同步函数，所以统一过这一层。
    """
    if inspect.isawaitable(value):
        return await value
    return value


class ExecutionMixin:
    """``internal/cron/execution.go`` 的方法集合（由 :class:`~atri_x.cron.service.Service` 组合）。"""

    async def run_direct(self, ctx: Any, job: CronJob, meta: Metadata) -> BaseException | None:
        """Direct 触发。对应 Go ``runDirect``。"""
        job, state, prepare_err = await self.prepare_delivery(ctx, job, meta)
        if not state.report_ready:
            return prepare_err
        deliver_err = await self.deliver_prepared(ctx, job, meta, state, "", False)
        return join_errors(prepare_err, deliver_err)

    async def run_llm(self, ctx: Any, job: CronJob, meta: Metadata) -> BaseException | None:
        """LLM 触发。对应 Go ``runLLM``。"""
        job, state, prepare_err = await self.prepare_delivery(ctx, job, meta)
        if not state.report_ready:
            return prepare_err
        deliver_err = await self.deliver_prepared(ctx, job, meta, state, "", False)
        return join_errors(prepare_err, deliver_err)

    async def prepare_delivery(
        self, ctx: Any, job: CronJob, meta: Metadata
    ) -> tuple[CronJob, CronDeliveryState, BaseException | None]:
        """准备本轮投递状态。对应 Go ``prepareDelivery``。

        返回 ``(job, state, err)``：``state.report_ready`` 为假表示"这一轮不该由我投递"
        （例如 CAS 输给了并发的另一个触发），调用方应直接返回 ``err``。
        """
        _ = ctx
        try:
            state = decode_delivery_state(job.delivery_state)
        except Exception as exc:  # noqa: BLE001
            return job, CronDeliveryState(), exc
        # 周期性任务每轮都开新状态；一次性任务只在还没有当前轮次时开。
        if (
            meta.schedule.mode == ScheduleMode.CRON
            or not (job.delivery_token or "").strip()
            or not (state.run_id or "").strip()
        ):
            state = CronDeliveryState(run_id=new_id())
            try:
                encoded = encode_delivery_state(state)
            except Exception as exc:  # noqa: BLE001
                return job, CronDeliveryState(), exc
            try:
                swapped = self.cron_jobs().compare_and_swap_delivery(
                    job.id, job.delivery_token, state.run_id, encoded
                )
            except Exception as exc:  # noqa: BLE001
                return job, CronDeliveryState(), exc
            if not swapped:
                # 令牌已被别人换掉：本轮不由我负责，安静退出。
                return job, CronDeliveryState(), None
            job = _with_delivery(job, state.run_id, encoded)
        if state.report_ready:
            return job, state, None
        err: BaseException | None = None
        if meta.trigger.mode == TriggerMode.DIRECT:
            state.report_ready = True
            state.task_completed = True
            state.report = (meta.trigger.message or "").strip()
        elif meta.trigger.mode == TriggerMode.LLM:
            state, _, err = await self.run_llm_report(ctx, job, meta, state)
        else:
            return job, state, InvalidError(f'unsupported trigger mode "{meta.trigger.mode}"')
        if meta.trigger.mode == TriggerMode.LLM and not state.report_ready:
            return job, state, err
        next_token = first_non_empty(state.report_session_id, state.run_id)
        try:
            encoded = encode_delivery_state(state)
        except Exception as exc:  # noqa: BLE001
            return job, state, join_errors(err, exc)
        try:
            swapped = self.cron_jobs().compare_and_swap_delivery(
                job.id, job.delivery_token, next_token, encoded
            )
        except Exception as exc:  # noqa: BLE001
            return job, state, join_errors(err, exc)
        if not swapped:
            return job, CronDeliveryState(), err
        job = _with_delivery(job, next_token, encoded)
        return job, state, err

    async def run_llm_report(
        self, ctx: Any, job: CronJob, meta: Metadata, state: CronDeliveryState
    ) -> tuple[CronDeliveryState, str, BaseException | None]:
        """跑一次 LLM 后台任务并生成报告。对应 Go ``runLLMReport``。

        每次实际调度触发都新建 Session（``session_id`` 为空）；只有同一轮的 JSON 格式
        重试才带上第一次的 Session ID。
        """
        if self.runner is None:
            return state, "", InvalidError("cron llm runner is not configured")
        actor = Actor(
            id=actor_id(meta.created_by.platform, meta.created_by.platform_user_id),
            platform=meta.created_by.platform,
            platform_user_id=meta.created_by.platform_user_id,
            display_name=meta.created_by.display_name,
            role=RoleSuperadmin,
        )
        request = RunRequest(
            kind=KindCron,
            name=job.name,
            title=meta.title,
            platform=meta.target.source_platform,
            actor=actor,
            scope_id=cron_scope_id(job.name),
            prompt=cron_prompt(meta.trigger.message),
            tool_list_names=list(meta.llm.tool_list_names),
            session_mode=meta.llm.session_mode,
            sandbox_subdir=cron_sandbox_subdir(job.name),
            metadata={"cron_job_name": job.name},
        )
        try:
            result = await _run_background(self.runner, ctx, request)
        except Exception as exc:  # noqa: BLE001
            return state, "", exc
        try:
            parsed: JSONResult | None = parse_llm_result(result.text)
            err: BaseException | None = None
        except ValueError as exc:
            parsed, err = None, exc
        if err is not None:
            result, parsed, err = await self.retry_llm_result_format(
                ctx, job, meta, actor, result.session_id
            )
        if result.taken_over:
            # 前台已经接管这个 Session：用户能在前台直接看到进度，本次后台运行不再
            # 解析 JSON、不发汇报、也不重复投递。
            return self.taken_over_delivery_state(job, meta, state, result.session_id), "", None
        if err is not None or parsed is None:
            message = cron_parse_failed_message(
                self.command_prefix_value(), meta.title, result.session_id, err
            )
            state.report_ready = True
            state.report = message
            state.report_session_id = result.session_id
            state.report_message_id = result.message_id
            return state, message, err
        if meta.target.all_enabled_platforms:
            try:
                await self.copy_session_to_broadcast_targets(
                    ctx, result.session_id, meta, job.name
                )
            except Exception as exc:  # noqa: BLE001 - 复制失败不影响本轮报告
                self.log_warn("copy cron session failed", job=job.name, error=str(exc))
        state.report_ready = True
        state.task_completed = parsed.completed
        state.report_segments = list(parsed.report_segments)
        state.report_session_id = result.session_id
        state.report_message_id = result.message_id
        report = (parsed.report or "").strip()
        if parsed.completed:
            state.report = report
            return state, report, None
        if not report:
            report = "任务未完成。"
        report += (
            f"\n可 {self.command_prefix_value()}resume 到 cron session 查看详情。"
            f"\nsession: {result.session_id}"
        )
        state.report = report
        return state, report, None

    async def retry_llm_result_format(
        self, ctx: Any, job: CronJob, meta: Metadata, actor: Actor, session_id: str
    ) -> tuple[RunResult, JSONResult, BaseException | None]:
        """同一 Session 的 JSON 格式重试。对应 Go ``retryLLMResultFormat``。"""
        request = RunRequest(
            kind=KindCron,
            name=job.name,
            title=meta.title,
            platform=meta.target.source_platform,
            actor=actor,
            scope_id=cron_scope_id(job.name),
            session_id=session_id,
            prompt=cron_format_retry_prompt(),
            tool_list_names=list(meta.llm.tool_list_names),
            session_mode=meta.llm.session_mode,
            sandbox_subdir=cron_sandbox_subdir(job.name),
            metadata={"cron_job_name": job.name},
        )
        try:
            result = await _run_background(self.runner, ctx, request)
        except Exception as exc:  # noqa: BLE001
            return RunResult(), JSONResult(), exc
        if not result.session_id:
            result.session_id = session_id
        try:
            parsed = parse_llm_result(result.text)
        except ValueError as exc:
            return result, JSONResult(), exc
        return result, parsed, None

    def taken_over_delivery_state(
        self, job: CronJob, meta: Metadata, state: CronDeliveryState, session_id: str
    ) -> CronDeliveryState:
        """被前台接管：记成"已完成但无汇报"。对应 Go ``takenOverDeliveryState``。

        报告为空，因此 ``deliver_prepared`` 不会发出任何文本，job 也不会被重复投递或判失败。
        """
        state.report_ready = True
        state.task_completed = True
        state.report = ""
        state.report_segments = []
        state.report_session_id = session_id
        state.report_message_id = ""
        self.audit_event(
            "cron.background_taken_over",
            **self.cron_audit_attrs(job.name, meta, session_id=session_id),
        )
        self.log_info(
            "cron background task taken over by foreground",
            **self.cron_audit_attrs(job.name, meta, session_id=session_id),
        )
        return state

    def command_prefix_value(self) -> str:
        """主命令前缀，未配置时回退 ``/``。对应 Go ``commandPrefixValue``。"""
        prefix = (self.command_prefix or "").strip()
        return prefix or "/"


def cron_parse_failed_message(prefix: str, title: str, session_id: str, err: Any) -> str:
    """JSON 解析失败时发给超管的文案。对应 Go ``cronParseFailedMessage``。"""
    if not (prefix or "").strip():
        prefix = "/"
    return (
        f"cron 任务 {title} 解析格式失败，请 {prefix}resume 到 cron session 查看详情。"
        f"\nsession: {session_id}\n错误：{err}"
    )


def cron_prompt(message: str) -> str:
    """LLM cron 的系统提示词（含 Atriyph 规则卡）。对应 Go ``cronPrompt``。"""
    return (
        "[系统 Cron 后台任务]\n\n"
        + rule_card()
        + "\n\n"
        + "** “Cron 任务内容”必须是 Atriyph #task <name> - 描述 任务文本\n"
        + "** 按“Cron 任务内容”中的 Atriyph 任务自主执行\n"
        + "** Cron 任务内容不需要包含最终 JSON 格式或汇报字段要求\n"
        + "** 信息不足时，在最终 JSON 的 report 填写失败或阻塞原因\n"
        + "** 需要使用工具时直接使用工具\n"
        + "** 所有路径参数必须使用相对路径，基于当前任务工作目录解析；不要使用绝对路径、~、.. 或 cd。\n"
        + "** 有投递目标、任务要求通知或产生需要目标知道的结果/失败/阻塞原因时，"
        + "应设置 need_report=true 并在 report 写自然语言汇报\n"
        + "** 最终回复必须是严格 JSON\n"
        + '** JSON 格式：{"completed":true,"need_report":false,"report":"","report_segments":[]}\n'
        + '** report_segments 可选数组，元素为 {"type":"image|file","url":"相对路径或 HTTP(S) URL"}，'
        + "用于附带图片或文件。本地图片/文件须先保存在当前任务工作目录内；"
        + "远程 URL 会原样交给平台，不会下载\n"
        + "** completed 表示是否完成任务\n"
        + "** need_report 表示是否需要向目标平台汇报；成功、失败或阻塞都可以请求汇报\n"
        + "** report 为需要发给用户的汇报，可填写处理结果、失败原因或阻塞原因\n"
        + "~ 把任务当作前台用户对话\n"
        + "~ 闲聊\n"
        + "~ 向用户提问\n"
        + "~ 输出 Markdown 代码块\n"
        + "~ 输出 JSON 外的任何文字\n"
        + "\nCron 任务内容：\n"
        + (message or "").strip()
    )


def cron_format_retry_prompt() -> str:
    """格式重试提示词。对应 Go ``cronFormatRetryPrompt``。"""
    return DefaultJSONRetryPrompt()


def parse_llm_result(text: str) -> JSONResult:
    """解析 LLM 的 JSON 报告。对应 Go ``parseLLMResult``（失败抛 ``ValueError``）。"""
    return ParseJSONResult(text)


async def _run_background(runner: Any, ctx: Any, request: RunRequest) -> RunResult:
    """调用注入的 runner 并归一化返回值（同步实现也接受）。"""
    result = await maybe_await(runner.run_background(ctx, request))
    if isinstance(result, RunResult):
        return result
    return _coerce_run_result(result)


def _coerce_run_result(result: Any) -> RunResult:
    """把鸭子类型的后台结果收敛成 :class:`RunResult`（集成期由 agent 直接返回该类型）。"""
    if result is None:
        return RunResult()
    if isinstance(result, dict):
        return RunResult(
            session_id=str(result.get("session_id", "") or ""),
            message_id=str(result.get("message_id", "") or ""),
            text=str(result.get("text", "") or ""),
            taken_over=bool(result.get("taken_over", False)),
            outcome=str(result.get("outcome", "") or ""),
        )
    return RunResult(
        session_id=str(getattr(result, "session_id", "") or ""),
        message_id=str(getattr(result, "message_id", "") or ""),
        text=str(getattr(result, "text", "") or ""),
        taken_over=bool(getattr(result, "taken_over", False)),
        outcome=str(getattr(result, "outcome", "") or ""),
    )


def _with_delivery(job: CronJob, token: str, encoded: str) -> CronJob:
    """返回带新投递令牌/状态的 job（Go 的 job 是值类型，这里显式复制）。"""
    return replace(job, delivery_token=token, delivery_state=encoded)
