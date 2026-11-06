"""``/grant`` 与 ``/panic``：授权生命周期的手动入口。

对应本仓《dsh-qq 借鉴落地评估》§B1 / §B2：

* ``/grant``（默认 ``list``）：列出、按 id 撤销当前进程内的放行凭据
  （``/confirmall`` / ``/confirmtool`` 产生的 ``Grant``）；
* ``/panic``：确定性紧急停止——撤销全部放行、停止全部 turn、取消排队请求与
  待确认的确认相位。群里执行需要显式加 ``confirm``，避免群友误触。

两个命令都是**只读配置检查之外**的运维入口，因此 ``Info.min_role`` 留空
（路由按历史行为把空值当"仅超管"）；``/panic`` 不看模型，命令路径直接执行。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.command import Info, Request, Result, new_func

__all__ = ["ApprovalModule", "new_grant", "new_panic"]

_PANIC_GROUP_HINT = (
    "在群里执行 /panic 需要二次确认：请发送 `/panic confirm`。"
    "（私聊与 CLI 直接执行；该命令会撤销全部高风险放行、停止所有进行中的任务。）"
)
_PANIC_DONE = (
    "已执行紧急停止：撤销放行 {grants} 条、停止 turn {turns} 个、"
    "取消排队请求 {requests} 个、丢弃排队消息 {inbox} 条、停止沙箱环境 {envs} 个。"
)


def _split_args(text: str) -> list[str]:
    return [item for item in (text or "").replace("\u3000", " ").split(" ") if item]


def format_grant_list(grants: list[Any], now: float) -> str:
    """把授权登记表渲染成 ``/grant list`` 文本。"""
    if not grants:
        return (
            "当前没有生效中的授权（``/confirmall`` / ``/confirmtool`` 产生的放行都带 TTL，"
            "默认 10 分钟）。"
        )
    lines = ["当前生效的授权："]
    for grant in grants:
        remaining = grant.remaining_seconds(now)
        if remaining < 0:
            ttl_text = "不过期"
        else:
            ttl_text = f"剩余 {int(remaining // 60)} 分 {int(remaining % 60)} 秒"
        target = grant.tool if grant.kind == "tool" else "全部工具"
        lines.append(
            f"- {grant.id}｜{target}｜会话 {grant.session_id or '-'}"
            f"｜作用域 {grant.scope_key or '-'}｜主体 {grant.actor_id or '-'}"
            f"｜已用 {grant.uses} 次｜{ttl_text}"
        )
    lines.append("撤销：`/grant revoke <id>`；全部撤销：`/panic`。")
    return "\n".join(lines)


def _new_grant_handler(deps: Deps) -> Any:
    async def handle(ctx: Any, req: Request) -> Result | None:
        if deps.grants is None:
            return Result(content="授权登记表未启用。")
        args = _split_args(req.args)
        action = args[0].lower() if args else "list"
        if action in ("", "list", "ls"):
            return Result(content=format_grant_list(deps.grants.active(), deps.grants.now()))
        if action in ("revoke", "rm", "cancel"):
            if len(args) < 2:
                return Result(content="用法：`/grant revoke <id>`（id 可用 `/grant list` 查看）。")
            grant = deps.grants.revoke(args[1])
            if deps.audit is not None:
                deps.audit(
                    "grant_revoked",
                    "grant_id",
                    args[1],
                    "result",
                    "revoked" if grant is not None else "not_found",
                )
            if grant is None:
                return Result(content=f"没有找到 id 为 {args[1]} 的授权（可能已过期）。")
            return Result(content=f"已撤销授权 {grant.id}。")
        return Result(content="用法：`/grant list` 或 `/grant revoke <id>`。")

    return new_func(
        Info(
            name="grant",
            usage="/grant [list|revoke <id>]",
            description=(
                "List or revoke high-risk tool grants created by /confirmall or "
                "/confirmtool. Grants are in-memory, scoped to the current "
                "conversation and actor, and expire after 10 minutes by default."
            ),
            aliases=["grants"],
        ),
        handle,
    )


def _new_panic_handler(deps: Deps) -> Any:
    async def handle(ctx: Any, req: Request) -> Result | None:
        args = _split_args(req.args)
        in_group = False
        if deps.scope is not None:
            scope = deps.scope(ctx)
            scope_id = str(getattr(scope, "platform_scope_id", "") or "")
            in_group = scope_id.startswith("group:") or scope_id.startswith(
                "supergroup:"
            ) or scope_id.startswith("channel:")
        if in_group and "confirm" not in [item.lower() for item in args]:
            return Result(content=_PANIC_GROUP_HINT)
        failed: list[str] = []

        def attempt(name: str, fn: Any, fallback: Any = 0) -> Any:
            try:
                return fn()
            except Exception:  # noqa: BLE001 - 各停止步骤独立，错误不泄露服务细节
                failed.append(name)
                return fallback

        grants = 0
        if deps.grants is not None:
            grants = attempt("grants", deps.grants.clear)
        turns = 0
        session_ids: list[str] = []
        if deps.turns is not None:
            session_ids = attempt("snapshot", lambda: [
                str(getattr(item, "session_id", "") or "")
                for item in deps.turns.snapshot_all()
            ], [])
            attempt("turns", deps.turns.stop_all)
            turns = len(session_ids) if "turns" not in failed else 0
        requests = 0
        if deps.requests is not None:
            requests = attempt("requests", lambda: int(deps.requests.cancel_all() or 0))
        inbox = 0
        if deps.cancel_session_inbox is not None:
            for session_id in session_ids:
                if session_id:
                    inbox += attempt("inbox", lambda: int(deps.cancel_session_inbox(session_id) or 0))
        envs = 0
        sandbox = _sandbox_service()
        if sandbox is not None:
            try:
                envs = int(await sandbox.stop_all(reason="panic") or 0)
            except Exception:  # noqa: BLE001 - 停止失败不能阻断紧急停止
                failed.append("sandbox")
        if deps.audit is not None:
            attempt("audit", lambda: deps.audit(
                "panic",
                "grants",
                grants,
                "turns",
                turns,
                "requests",
                requests,
                "inbox",
                inbox,
                "sandbox_envs",
                envs,
                "platform_scope",
                str(getattr(deps.scope(ctx), "platform_scope_id", "") or "")
                if deps.scope is not None
                else "",
                "failed_steps", ",".join(failed),
            ))
        content = _PANIC_DONE.format(
            grants=grants, turns=turns, requests=requests, inbox=inbox, envs=envs
        )
        if failed:
            content = "紧急停止未完成；已尝试其余步骤，失败/未知项：" + ", ".join(failed) + "。\n" + content
        return Result(content=content)

    return new_func(
        Info(
            name="panic",
            usage="/panic [confirm]",
            description=(
                "Deterministic emergency stop: revoke every high-risk grant, stop all "
                "turns, cancel queued requests and drop queued messages. In group chats "
                "it requires an explicit `confirm` argument. Superadmin only."
            ),
        ),
        handle,
    )


def new_grant(deps: Deps) -> Any:
    """``/grant list|revoke``。"""
    return _new_grant_handler(deps)


def _sandbox_service() -> Any:
    """当前生效的沙箱服务（未启用时返回 ``None``）。"""
    try:
        from atri_x.sandbox.runtime import service_or_none

        return service_or_none()
    except Exception:  # noqa: BLE001 - 沙箱未装配时维持旧行为
        return None


def new_panic(deps: Deps) -> Any:
    """``/panic``。"""
    return _new_panic_handler(deps)


class ApprovalModule:
    """注册 ``/grant`` 与 ``/panic``。对应评估 §B1 / §B2。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_grant, new_panic)
