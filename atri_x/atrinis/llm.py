"""Atrinis ``llm`` 模式：后台跑一次任务、解析 JSON 结果、准备并触发汇报投递。

对应 Go：``internal/atrinis/llm.go``。

一次 ``llm`` 事件的完整生命周期（状态机与 ``atriena`` 的状态常量一一对应）::

    queued → running →(需要汇报) result_ready → delivering → completed
                     └(不需要汇报) completed
                     └(失败)      failed

关键边界语义（Go 测试逐条钉住，这里逐条复刻）：

* **前台接管**（``result.taken_over``）按**完成**收尾，而不是失败：用户已经在前台
  看见了进度，后台再去解析 JSON、准备汇报、重复投递都是错的。
* JSON 解析失败会**先在同 session 重试一次**（:meth:`LLMMixin.retry_llm_result_format`），
  仍失败才写失败状态并把错误发给目标。
* 有汇报时**不立刻置 completed**：状态停在 ``result_ready``/``delivering``，只有
  :meth:`atri_x.atrinis.outbox.OutboxMixin.deliver_report` 成功后才推进到 completed。
  所以"事件 completed"确实意味着"汇报已投递"。
"""

from __future__ import annotations

import json
from typing import Any

from atri_x.atriena import Event
from atri_x.atrinis.background import (
    atrinis_actor,
    atrinis_sandbox_subdir,
    first_non_empty,
    first_platform,
    maybe_await,
)
from atri_x.atrinis.types import StatusCompleted, StatusFailed, StatusRunning
from atri_x.background import (
    DefaultJSONRetryPrompt,
    JSONResult,
    KindAtrinis,
    ParseJSONResult,
    RunRequest,
)
from atri_x.errors import InvalidError

__all__ = ["LLMMixin"]


class LLMMixin:
    """``llm`` 模式的执行路径。对应 Go ``llm.go`` 里的 ``Service`` 方法。"""

    async def run_llm_event(self, ctx: Any, event: Event, event_id: str) -> None:
        """执行一次后台 LLM 事件。对应 Go ``RunLLMEvent``（失败时抛异常）。"""
        attrs = self.event_attrs(event)
        if self.runner is None:
            err = InvalidError("atrinis background runner is not configured")
            self.complete_event(
                ctx, event_id, event.resolved_targets, StatusFailed, "", str(err)
            )
            self.log_warn(
                "atrinis llm failed", *attrs, "event_id", event_id, "error", str(err)
            )
            raise err
        self.complete_event(ctx, event_id, event.resolved_targets, StatusRunning, "", "")
        self.audit_event("atrinis.llm_started", *attrs, "event_id", event_id)
        self.log_info("atrinis llm started", *attrs, "event_id", event_id)

        segments = await self.materialize_segments(ctx, event.request.segments, event_id)
        model = self.model_for_event(event)
        result = await maybe_await(
            self.runner.run_background(
                ctx,
                RunRequest(
                    kind=KindAtrinis,
                    name=event.event_key,
                    title=first_non_empty(
                        event.request.title, "Atrinis: " + event.request.source
                    ),
                    platform=first_platform(event.resolved_targets),
                    actor=atrinis_actor(event),
                    scope_id="atrinis:" + event.event_key,
                    model_provider=model.provider,
                    model=model.model,
                    session_mode=event.request.session_mode,
                    prompt_segments=segments,
                    prompt=self.llm_prompt(event),
                    tool_list_names=list(event.request.tool_list_names or []),
                    cached_tools=self.atriwisp_cached_tools(event),
                    sandbox_subdir=atrinis_sandbox_subdir(event.request.atriwisp.name),
                    metadata={
                        "atrinis_event_key": event.event_key,
                        "atriwisp_name": event.request.atriwisp.name,
                        "atrinis_source": event.request.source,
                        "atrinis_source_id": event.request.id,
                    },
                ),
            )
        )
        # Go 这里**忽略** runner 自带的 Parsed/ParseErr，一律重新解析 result.Text
        # （runner 的解析结果可能来自不同的口径），保持同一行为。
        from atri_x.agent.admission import ADMISSION_SKIP_OUTCOME

        if str(getattr(result, "outcome", "") or "") == ADMISSION_SKIP_OUTCOME:
            # 静默期被准入层跳过：事件按完成收尾、不生成汇报（审计由 run_background 写）。
            self.complete_event_with_session(
                ctx, event_id, event.resolved_targets, StatusCompleted, "", "", ""
            )
            return
        try:
            parsed = ParseJSONResult(result.text)
            parse_error: BaseException | None = None
        except ValueError as exc:
            parse_error = exc
        if parse_error is not None:
            result, parsed, parse_error = await self.retry_llm_result_format(
                ctx, event, result.session_id, model
            )
        if result.taken_over:
            # 前台已经接管这个 Session：不再解析 JSON、不准备汇报、不重复投递；
            # 事件按完成收尾而不是失败。
            self.complete_event_with_session(
                ctx, event_id, event.resolved_targets, StatusCompleted, result.session_id, "", ""
            )
            self.audit_event(
                "atrinis.background_taken_over",
                *attrs,
                "event_id",
                event_id,
                "session_id",
                result.session_id,
            )
            self.log_info(
                "atrinis background task taken over by foreground",
                *attrs,
                "event_id",
                event_id,
                "session_id",
                result.session_id,
            )
            return None
        if parse_error is not None:
            message = (
                f"Atrinis 事件 {event.event_key} 解析格式失败，请查看后台 session。\n"
                f"session: {result.session_id}\n错误：{parse_error}"
            )
            self.complete_event_with_session(
                ctx,
                event_id,
                event.resolved_targets,
                StatusFailed,
                result.session_id,
                message,
                str(parse_error),
            )
            self.audit_event(
                "atrinis.llm_failed",
                *attrs,
                "event_id",
                event_id,
                "session_id",
                result.session_id,
                "error",
                str(parse_error),
            )
            self.log_warn(
                "atrinis llm format failed",
                *attrs,
                "event_id",
                event_id,
                "session_id",
                result.session_id,
                "error",
                str(parse_error),
            )
            raise parse_error

        try:
            result_json = _marshal(parsed.to_json())
        except (TypeError, ValueError):
            # 与 Go 的 ``resultJSON, _ := json.Marshal(parsed)`` 一致：序列化失败
            # 不改变控制流，只是结果列为空。
            result_json = ""
        if parsed.need_report and ((parsed.report or "").strip() or parsed.report_segments):
            prepared = await self.prepare_report(
                ctx,
                event,
                event_id,
                result_json,
                result.session_id,
                result.message_id,
                parsed,
            )
            if not prepared:
                self.audit_event(
                    "atrinis.llm_completed",
                    *attrs,
                    "event_id",
                    event_id,
                    "session_id",
                    result.session_id,
                )
                self.log_info(
                    "atrinis llm completed",
                    *attrs,
                    "event_id",
                    event_id,
                    "session_id",
                    result.session_id,
                )
                return None
            await self.deliver_report(ctx, event_id)
            return None
        self.complete_event_with_session(
            ctx,
            event_id,
            event.resolved_targets,
            StatusCompleted,
            result.session_id,
            result_json,
            "",
        )
        self.audit_event(
            "atrinis.llm_completed",
            *attrs,
            "event_id",
            event_id,
            "session_id",
            result.session_id,
        )
        self.log_info(
            "atrinis llm completed",
            *attrs,
            "event_id",
            event_id,
            "session_id",
            result.session_id,
        )
        return None

    async def retry_llm_result_format(
        self, ctx: Any, event: Event, session_id: str, model: Any
    ) -> tuple[Any, JSONResult, BaseException | None]:
        """在同一 session 上用固定提示词重试一次 JSON 格式。对应 Go ``retryLLMResultFormat``。

        返回 ``(run_result, parsed, parse_error)``；调用方按 Go 的双返回语义处理
        "解析仍失败"（不在这里抛）。
        """
        result = await maybe_await(
            self.runner.run_background(
                ctx,
                RunRequest(
                    kind=KindAtrinis,
                    name=event.event_key,
                    title=first_non_empty(
                        event.request.title, "Atrinis: " + event.request.source
                    ),
                    platform=first_platform(event.resolved_targets),
                    actor=atrinis_actor(event),
                    scope_id="atrinis:" + event.event_key,
                    session_id=session_id,
                    model_provider=model.provider,
                    model=model.model,
                    session_mode=event.request.session_mode,
                    prompt=DefaultJSONRetryPrompt(),
                    sandbox_subdir=atrinis_sandbox_subdir(event.request.atriwisp.name),
                    metadata={
                        "atrinis_event_key": event.event_key,
                        "atriwisp_name": event.request.atriwisp.name,
                    },
                ),
            )
        )
        try:
            parsed = ParseJSONResult(result.text)
        except ValueError as exc:
            return result, JSONResult(), exc
        return result, parsed, None

    def llm_prompt(self, event: Event) -> str:
        """后台任务的提示词。对应 Go ``llmPrompt``（逐行照抄，含 ``**`` / ``~`` 标记）。"""
        fmt = (event.request.format or "").strip() or "text"
        parts = ["[系统 Atrinis 后台事件任务]", ""]
        if fmt == "atriyph":
            from atri_x.atriyph import rule_card

            parts.extend([rule_card(), ""])
        parts.extend(
            [
                "** 按事件内容自主处理，当前无人值守",
                "** 信息不足时，在最终 JSON 的 report 填写失败或阻塞原因",
                "** 需要使用工具时直接使用工具",
                "** 所有路径参数必须使用相对路径",
                "** 有投递目标、任务要求通知或产生需要目标知道的结果/失败/阻塞原因时，"
                "应设置 need_report=true 并在 report 写自然语言汇报",
                "** 最终回复必须是严格 JSON",
                '** JSON 格式：{"completed":true,"need_report":false,"report":"",'
                '"report_segments":[]}',
                "** report_segments 可选数组，元素为 {\"type\":\"image|file\","
                '"url":"相对路径"} 或 {"type":"image|file","url":"media:<sha256>"}，'
                "来源二选一。文件路径必须位于当前任务工作目录内",
                "** completed 表示是否完成任务",
                "** need_report 表示是否需要向目标平台汇报；成功、失败或阻塞都可以请求汇报",
                "** report 为需要发给目标平台的汇报，可填写处理结果、失败原因或阻塞原因",
                "~ 使用绝对路径、~、.. 或 cd。",
                "~ 闲聊",
                "~ 向用户提问",
                "~ 输出 Markdown 代码块",
                "~ 输出 JSON 外的任何文字",
                "",
                "事件标题：" + (event.request.title or "").strip(),
                "事件来源：" + event.request.source,
                "事件 ID：" + event.request.id,
                "事件格式：" + fmt,
            ]
        )
        if event.request.meta:
            try:
                parts.append("事件 metadata：" + _marshal(dict(event.request.meta)))
            except (TypeError, ValueError):
                pass
        parts.extend(["", "事件内容：", (event.request.content or "").strip()])
        return "\n".join(parts)

    def model_for_event(self, event: Event) -> Any:
        """选模型：``model_slot`` 缺省为 ``work``，取不到再回落 ``work``。对应 Go ``modelForEvent``。"""
        from atri_x.config import ModelSelection

        slot = (event.request.model_slot or "").strip() or "work"
        if self.resolve_model is None:
            return ModelSelection()
        selected = self.resolve_model(slot)
        if (not selected.provider or not selected.model) and slot != "work":
            selected = self.resolve_model("work")
        return selected


def _marshal(value: Any) -> str:
    """按 Go ``json.Marshal`` 的紧凑口径序列化。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
