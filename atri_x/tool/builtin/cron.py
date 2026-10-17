"""``cron`` / ``cron_query`` / ``cron_write`` 三个定时任务工具。

对应 Go：``internal/tool/builtin/cron.go``。

与 Go 的差异：

* ``Call`` / ``DiscoveryContent`` 的调用形态：Python 的 ``call`` 是 async；
  ``discovery_content()`` 保持无 ctx 形态（:func:`atri_x.tool.detail.load_discovery_content`
  会按签名自动选择）。
* Go 的 ``Service`` 六个 CRUD 方法都是同步的（``internal/cron/service.go``），本仓
  ``atri_x.cron.Service`` 改成了 ``async``（投递/runner 是 I/O），因此工具侧必须
  ``await``。统一经 :func:`atri_x.cron.maybe_await`：服务是 ``async`` 就真 await
  （**不允许丢掉协程**），注入同步实现时也能工作；操作抛错时异常原样上抛，
  绝不返回 ``ok:true``。
* Go 的 ``*elcron.Service`` → :class:`atri_x.cron.Service`；``UpsertRequest`` /
  ``PatchRequest`` 同名对应。``cron.ScheduleMode`` / ``cron.TriggerMode`` 在 Python
  侧是常量类（``ScheduleMode.ONCE``），不能像 Go 那样做字符串转换，因此这里直接把
  JSON 字符串原样交给 service（校验口径与 Go 的 ``validateUpsert`` 一致）。
* ``stringPtrIfNotEmpty`` → :func:`atri_x.tool.builtin.common.string_ptr_if_not_empty`
  （Python 里 ``None`` 即 Go 的 ``nil`` 指针）。
* 回给模型的 JSON 形态（``JobView`` / ``storage.CronJob``）统一走
  :mod:`atri_x.cron.jsonview`：Go 的 ``storage.CronJob`` 没有 json tag，键就是字段名。
* Go ``time.AddDate(0, months, weeks*7+days)`` 的"日历月 + 日"语义用
  :func:`_add_months` + :class:`datetime.timedelta` 复刻（日溢出按 Go 的
  "规范化到下一月" 行为）。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from atri_x.cron import (
    PatchRequest,
    Service,
    UpsertRequest,
    maybe_await,
)
from atri_x.cron.jsonview import cron_job_json, job_view_json
from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder, required
from atri_x.tool.builtin.common import (
    decode_args,
    default_actor,
    json_result,
    string_ptr_if_not_empty,
    text_result,
)
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo, first as first_runtime_info
from atri_x.tool.tool import CallRequest, Info, Result, RiskHigh, RiskMedium

#: Go ``time`` 的 ``2006-01-02 15:04:05`` 布局。
RUN_AT_LAYOUT = "%Y-%m-%d %H:%M:%S"

CRON_TOOL_NAME = "cron"
CRON_QUERY_TOOL_NAME = "cron_query"
CRON_WRITE_TOOL_NAME = "cron_write"


@dataclass
class CronCreateArgs:
    """对应 Go ``cronCreateArgs``（JSON 键名照抄 struct tag）。"""

    name: str = ""
    title: str = ""
    schedule_mode: str = ""
    run_at: str = ""
    cron_expr: str = ""
    run_after_minutes: int = 0
    run_after_hours: int = 0
    run_after_days: int = 0
    run_after_weeks: int = 0
    run_after_months: int = 0
    trigger_mode: str = ""
    message: str = ""
    tool_list_names: list[str] = field(default_factory=list)
    session_mode: str = ""
    all_enabled_platforms: bool = False
    enabled: bool | None = None


@dataclass
class CronQueryArgs:
    """对应 Go ``cronQueryArgs``。"""

    name: str = ""
    include_disabled: bool = False
    include_completed: bool = False


@dataclass
class CronWriteArgs:
    """对应 Go ``cronWriteArgs``。两个 ``*bool`` 用 ``None`` 表示未传。"""

    operation: str = ""
    name: str = ""
    title: str = ""
    schedule_mode: str = ""
    run_at: str = ""
    cron_expr: str = ""
    run_after_minutes: int = 0
    run_after_hours: int = 0
    run_after_days: int = 0
    run_after_weeks: int = 0
    run_after_months: int = 0
    trigger_mode: str = ""
    message: str = ""
    tool_list_names: list[str] | None = None
    session_mode: str = ""
    all_enabled_platforms: bool | None = None
    enabled: bool | None = None


@dataclass
class RelativeOffset:
    """相对时间偏移。对应 Go ``relativeOffset``。"""

    minutes: int = 0
    hours: int = 0
    days: int = 0
    weeks: int = 0
    months: int = 0

    def has_any(self) -> bool:
        """对应 Go ``relativeOffset.hasAny``。"""
        return (
            self.minutes != 0
            or self.hours != 0
            or self.days != 0
            or self.weeks != 0
            or self.months != 0
        )


def new_cron_tools(service: Service | None, *infos: RuntimeInfo) -> list[Any]:
    """对应 Go ``NewCronTools``。"""
    info = first_runtime_info(*infos)
    return [
        CronTool(service, info),
        CronQueryTool(service, info),
        CronWriteTool(service, info),
    ]


def cron_builder(name: str, description: str) -> Builder:
    """对应 Go ``cronBuilder``。"""
    return (
        Builder(name)
        .description(description)
        .risk(RiskMedium)
        .superadmin_only()
    )


def hidden_cron_info(name: str, description: str, risk: str) -> Info:
    """对应 Go ``hiddenCronInfo``。"""
    return cron_builder(name, description).risk(risk).hidden().superadmin_only().build_info()


class CronTool:
    """入口工具：只报告当前本地时间。对应 Go ``CronTool``。"""

    def __init__(self, service: Service | None = None, info: RuntimeInfo | None = None) -> None:
        self.service = service
        # Go 的私有字段 ``info runtimeinfo.Info``；Python 里 ``info`` 已被
        # ``Tool.info()`` 协议方法占用，故改名为 ``runtime_info``。
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return CRON_TOOL_NAME

    def info_value(self) -> Info:
        return (
            Builder(self.name())
            .description("管理系统 Cron 定时任务。本工具仅为入口")
            .risk(RiskMedium)
            .superadmin_only()
            .depends_on(CRON_QUERY_TOOL_NAME, CRON_WRITE_TOOL_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return cron_builder(self.name(), self.info().description).build_schema()

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        now = self.runtime_info.current_time().strftime(RUN_AT_LAYOUT)
        return text_result(
            "cron 是定时任务管理入口。当前本地时间："
            + now
            + "。请调用 cron_query 或 cron_write。cron_query 不传 name 时列出 cron，"
            "传 name 时查询单个 cron。"
        )

    def discovery_content(self) -> tuple[str, bool]:
        """把当前本地时间附到 discover_tool 文本后面。对应 Go ``DiscoveryContent``。"""
        return (
            "当前本地时间：" + self.runtime_info.current_time().strftime(RUN_AT_LAYOUT),
            False,
        )


class CronQueryTool:
    """查询工具。对应 Go ``CronQueryTool``。"""

    def __init__(self, service: Service | None = None, info: RuntimeInfo | None = None) -> None:
        self.service = service
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return CRON_QUERY_TOOL_NAME

    def info(self) -> Info:
        return hidden_cron_info(
            self.name(),
            "查询单个 cron 或列出 cron。不传 name 时列出；传 name 时查询单个。",
            RiskMedium,
        )

    def schema(self) -> ToolSchema:
        return (
            cron_builder(self.name(), "查询单个 cron 或列出 cron。 ")
            .string("name", "任务短名。留空则列出 cron。")
            .boolean("include_disabled", "列出 cron 时是否包含已停用 cron。默认 false。 ")
            .boolean(
                "include_completed",
                "列出 cron 时是否显示已完成的 cron。默认 false；"
                "想查看历史一次性任务时设为 true。 ",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, CronQueryArgs, self.name())
        service = self._service()
        actor = default_actor(ctx)
        if (args.name or "").strip() != "":
            view = await maybe_await(service.get(ctx, args.name, actor))
            return json_result(job_view_json(view))
        views = await maybe_await(
            service.list(
                ctx,
                bool(args.include_disabled),
                bool(args.include_completed),
                actor,
            )
        )
        return json_result([job_view_json(view) for view in views])

    def _service(self) -> Service:
        if self.service is None:
            raise InvalidError("cron service is not configured")
        return self.service


class CronWriteTool:
    """写入工具（create / update / disable / delete）。对应 Go ``CronWriteTool``。"""

    def __init__(self, service: Service | None = None, info: RuntimeInfo | None = None) -> None:
        self.service = service
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return CRON_WRITE_TOOL_NAME

    def info(self) -> Info:
        return hidden_cron_info(
            self.name(),
            "创建、Patch 更新、停用或硬删除 cron。"
            "operation 为 create、update、disable 或 delete。",
            RiskHigh,
        )

    def schema(self) -> ToolSchema:
        return (
            cron_builder(self.name(), cron_write_description())
            .string("operation", "写操作：create、update、disable、delete。", required())
            .string(
                "name",
                "任务短名，只允许字母、数字、下划线、点和横线。",
                required(),
            )
            .string("title", "create 需要；update 可选。cron session 标题。为空时使用 name。")
            .string(
                "schedule_mode",
                "create 需要；update 可选。触发计划类型：once 或 cron。 ",
            )
            .string(
                "run_at",
                "一次性任务绝对时间，格式 YYYY-MM-DD HH:MM:SS。"
                "schedule_mode=once 且未使用 run_after_* 时填写。\n"
                "~ 任意 run_after_* 同时传",
            )
            .string("cron_expr", "5 字段 cron 表达式。schedule_mode=cron 时填写。 ")
            .integer(
                "run_after_minutes",
                "一次性任务相对当前时间的分钟偏移。\n** 用户说几分钟后时优先使用\n"
                "~ run_at 同时传",
            )
            .integer(
                "run_after_hours",
                "一次性任务相对当前时间的小时偏移。\n** 用户说几小时后时优先使用\n"
                "~ run_at 同时传",
            )
            .integer(
                "run_after_days",
                "一次性任务相对当前时间的天数偏移。\n** 用户说几天后时优先使用\n"
                "~ run_at 同时传",
            )
            .integer(
                "run_after_weeks",
                "一次性任务相对当前时间的周数偏移，1周按7天。\n** 用户说几周后时优先使用\n"
                "~ run_at 同时传",
            )
            .integer(
                "run_after_months",
                "一次性任务相对当前时间的日历月偏移。\n** 用户说几个月后时优先使用\n"
                "~ run_at 同时传",
            )
            .string(
                "trigger_mode",
                "create 需要；update 可选。触发模式：direct 或 llm。"
                "direct 直接发消息；llm 后台运行 LLM 处理复杂任务。 ",
            )
            .string(
                "message",
                "create 需要；update 可选。trigger_mode=direct：使用普通自然语言通知文本。"
                "trigger_mode=llm：使用 Atriyph #task <name> - 描述 任务文本。",
            )
            .string_array(
                "tool_list_names",
                "trigger_mode=llm 时预注入的工具名或 Skill 名列表；普通工具会注入 schema，"
                "Skill 会注入任务说明并自动注入对应 runner。update 传空数组表示清空。 ",
            )
            .string(
                "session_mode",
                "trigger_mode=llm 时后台 Session 模式：work 或 chat；默认 work。"
                "不需要工具时选chat",
            )
            .boolean("all_enabled_platforms", "是否发送/广播到所有 enabled 平台超级管理员。 ")
            .boolean(
                "enabled",
                "create：创建后是否启用，默认 true；update：是否启用。"
                "false 表示停用但保留记录。 ",
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, CronWriteArgs, self.name())
        service = self._service()
        operation = (args.operation or "").strip().lower()
        if operation == "create":
            return await self._create(ctx, args, service)
        if operation == "update":
            return await self._update(ctx, args, service)
        if operation == "disable":
            await maybe_await(service.disable(ctx, args.name, default_actor(ctx)))
            return json_result({"ok": True})
        if operation == "delete":
            await maybe_await(service.delete(ctx, args.name, default_actor(ctx)))
            return json_result({"ok": True})
        raise InvalidError("operation must be create, update, disable, or delete")

    def _service(self) -> Service:
        if self.service is None:
            raise InvalidError("cron service is not configured")
        return self.service

    async def _create(self, ctx: Any, args: CronWriteArgs, service: Service) -> Result:
        """对应 Go ``CronWriteTool.create``。"""
        enabled = True if args.enabled is None else bool(args.enabled)
        all_enabled_platforms = (
            False if args.all_enabled_platforms is None else bool(args.all_enabled_platforms)
        )
        actor = default_actor(ctx)
        create_args = CronCreateArgs(
            name=args.name,
            title=args.title,
            schedule_mode=args.schedule_mode,
            run_at=args.run_at,
            cron_expr=args.cron_expr,
            run_after_minutes=_as_int(args.run_after_minutes),
            run_after_hours=_as_int(args.run_after_hours),
            run_after_days=_as_int(args.run_after_days),
            run_after_weeks=_as_int(args.run_after_weeks),
            run_after_months=_as_int(args.run_after_months),
            trigger_mode=args.trigger_mode,
            message=args.message,
            tool_list_names=list(args.tool_list_names or []),
            session_mode=args.session_mode,
            all_enabled_platforms=all_enabled_platforms,
            enabled=args.enabled,
        )
        run_at = resolve_create_run_at_at(create_args, self.runtime_info.current_time())
        job = await maybe_await(
            service.create(
                ctx,
                UpsertRequest(
                    name=args.name,
                    title=args.title,
                    schedule_mode=args.schedule_mode,
                    run_at=run_at,
                    cron_expr=args.cron_expr,
                    trigger_mode=args.trigger_mode,
                    message=args.message,
                    tool_list_names=list(args.tool_list_names or []),
                    session_mode=args.session_mode,
                    all_enabled_platforms=all_enabled_platforms,
                    enabled=enabled,
                    actor=actor,
                    source_platform=actor.platform,
                ),
            )
        )
        return json_result({"ok": True, "job": cron_job_json(job)})

    async def _update(self, ctx: Any, args: CronWriteArgs, service: Service) -> Result:
        """对应 Go ``CronWriteTool.update``。"""
        offset = RelativeOffset(
            minutes=_as_int(args.run_after_minutes),
            hours=_as_int(args.run_after_hours),
            days=_as_int(args.run_after_days),
            weeks=_as_int(args.run_after_weeks),
            months=_as_int(args.run_after_months),
        )
        run_at = resolve_update_run_at_at(
            string_ptr_if_not_empty(args.run_at), offset, self.runtime_info.current_time()
        )
        patch = PatchRequest(
            name=args.name,
            title=string_ptr_if_not_empty(args.title),
            run_at=run_at,
            cron_expr=string_ptr_if_not_empty(args.cron_expr),
            message=string_ptr_if_not_empty(args.message),
            session_mode=string_ptr_if_not_empty(args.session_mode),
            all_enabled_platforms=args.all_enabled_platforms,
            enabled=args.enabled,
            actor=default_actor(ctx),
        )
        if args.schedule_mode != "":
            patch.schedule_mode = args.schedule_mode
        if args.trigger_mode != "":
            patch.trigger_mode = args.trigger_mode
        if args.tool_list_names is not None:
            patch.tool_list_names = list(args.tool_list_names)
        job = await maybe_await(service.update(ctx, patch))
        return json_result({"ok": True, "job": cron_job_json(job)})


# ---------------------------------------------------------------------------
# run_at 解析
# ---------------------------------------------------------------------------
def resolve_create_run_at(args: CronCreateArgs, now: datetime | None = None) -> str:
    """对应 Go ``resolveCreateRunAt``。"""
    return resolve_create_run_at_at(args, now or _local_now())


def resolve_create_run_at_at(args: CronCreateArgs, now: datetime) -> str:
    """对应 Go ``resolveCreateRunAtAt``。"""
    return resolve_run_at_at(
        args.run_at,
        RelativeOffset(
            minutes=_as_int(args.run_after_minutes),
            hours=_as_int(args.run_after_hours),
            days=_as_int(args.run_after_days),
            weeks=_as_int(args.run_after_weeks),
            months=_as_int(args.run_after_months),
        ),
        now,
    )


def resolve_update_run_at(run_at: str | None, offset: RelativeOffset) -> str | None:
    """对应 Go ``resolveUpdateRunAt``。"""
    return resolve_update_run_at_at(run_at, offset, _local_now())


def resolve_update_run_at_at(
    run_at: str | None, offset: RelativeOffset, now: datetime
) -> str | None:
    """对应 Go ``resolveUpdateRunAtAt``。"""
    if not offset.has_any():
        return run_at
    resolved = resolve_run_at_at(run_at or "", offset, now)
    return resolved


def resolve_run_at(run_at: str, offset: RelativeOffset) -> str:
    """对应 Go ``resolveRunAt``。"""
    return resolve_run_at_at(run_at, offset, _local_now())


def resolve_run_at_at(run_at: str, offset: RelativeOffset, now: datetime) -> str:
    """对应 Go ``resolveRunAtAt``（绝对时间与相对偏移互斥）。"""
    run_at = (run_at or "").strip()
    if not offset.has_any():
        return run_at
    if run_at != "":
        raise InvalidError(
            "run_at 不能和 run_after_* 同时传；请只使用绝对时间或相对偏移之一"
        )
    if (
        offset.minutes < 0
        or offset.hours < 0
        or offset.days < 0
        or offset.weeks < 0
        or offset.months < 0
    ):
        raise InvalidError(
            f"run_after_* 必须是正数；当前本地时间：{now.strftime(RUN_AT_LAYOUT)}"
        )
    if (
        offset.months == 0
        and offset.weeks == 0
        and offset.days == 0
        and offset.hours == 0
        and offset.minutes == 0
    ):
        raise InvalidError(
            f"run_after_* 至少需要一个正数；当前本地时间：{now.strftime(RUN_AT_LAYOUT)}"
        )
    run = _add_months(now, offset.months) + timedelta(
        days=offset.weeks * 7 + offset.days,
        hours=offset.hours,
        minutes=offset.minutes,
    )
    return run.strftime(RUN_AT_LAYOUT)


def _add_months(value: datetime, months: int) -> datetime:
    """对应 Go ``time.Time.AddDate(0, months, 0)``。

    Go 的语义：月份先加，日超出目标月长度时**规范化到下一月**（例如 1/31 + 1 月
    = 3/3）。这里用同样的做法，而不是把日夹到月末。
    """
    if months == 0:
        return value
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = value.day
    days_in_month = calendar.monthrange(year, month)[1]
    result = value.replace(year=year, month=month, day=1)
    if day <= days_in_month:
        return result.replace(day=day)
    overflow = day - days_in_month
    result = result.replace(day=days_in_month)
    return result + timedelta(days=overflow)


def cron_write_description() -> str:
    """对应 Go ``cronWriteDescription``。"""
    from atri_x.tool.runtimeinfo import atriyph_rule_card

    return (
        "创建、Patch 更新、停用或硬删除 cron。\n\n"
        + atriyph_rule_card()
        + """

#task cron_write_desc - cron_write 总规则
** operation=create 时创建一次性或周期 cron；title、schedule_mode、trigger_mode、message 通常需要填写
** operation=update 时只修改传入字段
** operation=disable 时停用 cron 但保留记录
** operation=delete 时硬删除 cron，不保留调度记录；用户只想暂停时优先用 disable
** trigger_mode=direct 时，message 使用普通自然语言通知文本
** trigger_mode=llm 时，message 使用 Atriyph #task <name> - 描述 任务文本
** 用户说几分钟/几小时/几天/几周/几个月后时，优先使用对应 run_after_*
** create 时 enabled 缺省为 true
~ 自己计算用户相对时间对应的 run_at
~ 同时传 run_at 和任意 run_after_*"""
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _local_now() -> datetime:
    return datetime.now().astimezone()


__all__ = [name for name in dir() if not name.startswith("_")]
