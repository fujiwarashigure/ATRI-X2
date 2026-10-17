"""内置 Hook 插件的注册入口。

对应 Go：``internal/hook/builtin/register.go``。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from atri_x.delivery.delivery import TOOL_PREVIEW_PREFIX, Output, Target
from atri_x.hook.builtin.angel_memory import register_angel_memory
from atri_x.hook.builtin.self_learning import register_self_learning
from atri_x.hook.event import Event, HandlerFunc, Point
from atri_x.hook.manager import Registration
from atri_x.hook.match import always
from atri_x.hook.process import ProcessEnvironment
from atri_x.llm.segment import segments_text_only
from atri_x.storage.models import OutboundMessage
from atri_x.storage.times import now as storage_now

__all__ = [
    "Options",
    "RegisterAll",
    "record_outbound_message",
    "register_module",
    "report_plugin_error",
]


@dataclass
class Options:
    """ATRI-X 自带 Hook 插件共享的依赖。对应 Go ``Options``。

    ``notify`` / ``audit`` 是同步可调用；``send`` 是 async 可调用
    （``await send(ctx, target, outputs) -> Receipt``）。
    """

    config_dir: str = ""
    tools: Any = None
    logger: Any = None
    audit: Callable[..., None] | None = None
    notify: Callable[..., None] | None = None
    send: Callable[..., Any] | None = None
    platform_callers: Any = None
    runtime: Any = None
    process_env: ProcessEnvironment = field(default_factory=ProcessEnvironment)
    outbound_messages: Any = None
    angel_memory: Any = None
    self_learning: Any = None


def RegisterAll(registrar: Any, opts: Options) -> list[Any]:
    """注册全部内置 Hook，返回有状态 Hook 的配置。对应 Go ``RegisterAll``。"""
    if registrar is None:
        return []
    from atri_x.hook.rules import Options as RulesOptions
    from atri_x.hook.rules import rules as rules_module

    try:
        module = rules_module.NewModule(
            RulesOptions(
                config_dir=opts.config_dir,
                tools=opts.tools,
                logger=opts.logger,
                audit=opts.audit,
                notify=opts.notify,
                send=opts.send,
                platform_callers=opts.platform_callers,
                runtime=opts.runtime,
                process_env=opts.process_env,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 规则装载失败即禁用该插件
        report_plugin_error(opts, "rules", exc)
        raise
    register_module(registrar, opts, "rules", module)

    if opts.outbound_messages is not None:
        registrar.register(
            Registration(
                point=Point.PLATFORM_MESSAGE_SENT,
                priority=900,
                plugin_id="builtin",
                name="builtin.outbound_history",
                description="记录实际发送的 assistant 出站消息，供学习和群分析复用。",
                match=always(),
                handler=HandlerFunc(record_outbound_message(opts.outbound_messages)),
            )
        )
    register_angel_memory(registrar, opts.angel_memory)
    register_self_learning(registrar, opts.self_learning)
    return list(module.runtimes)


def record_outbound_message(repo: Any) -> Any:
    """构造"记录出站消息"的 handler。对应 Go ``recordOutboundMessage``。"""

    async def handle(ctx: Any, event: Event) -> Event:
        if repo is None or event.point != Point.PLATFORM_MESSAGE_SENT:
            return event
        text = segments_text_only(event.message.segments).strip()
        # 工具调用进度预览不是用户可见回答，不写入出站历史。前缀规则集中在交付层，
        # 避免与平台的群聊过滤规则各写一份。
        if not text or text.startswith(TOOL_PREVIEW_PREFIX):
            return event
        platform = (event.platform.name or "").strip()
        scope_id = (event.platform.scope_id or "").strip()
        if not platform or not scope_id:
            return event
        message = OutboundMessage(
            platform=platform,
            platform_scope_id=scope_id,
            platform_message_id=(event.platform.message_id or "").strip(),
            text=text,
            created_at=storage_now(),
        )
        try:
            result = repo.append(message)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 包装成可读错误
            raise RuntimeError(f"record outbound message: {exc}") from exc
        return event

    return handle


def register_module(registrar: Any, opts: Options, name: str, module: Any) -> None:
    """注册一个模块；失败时上报并抛出。对应 Go ``registerModule``。"""
    try:
        module.register_hooks(registrar)
    except Exception as exc:  # noqa: BLE001
        report_plugin_error(opts, name, exc)
        raise


def report_plugin_error(opts: Options, name: str, err: BaseException) -> None:
    """插件被禁用时的日志与通知。对应 Go ``reportPluginError``。"""
    if err is None:
        return
    if opts.logger is not None:
        error = getattr(opts.logger, "error", None)
        if callable(error):
            error("hook plugin disabled", plugin=name, error=str(err))
    if opts.notify is not None:
        opts.notify(None, f"Hook 插件 {name} 已禁用：{err}")


# 交叉检索：Go 里 recordOutboundMessage 用到这些类型。
_OUTPUT = Output
_TARGET = Target
