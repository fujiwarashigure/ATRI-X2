"""``/sandbox``：用户安全域执行环境的状态、重置与自检入口。

修改清单 §14 / §15 要求区分「重启 / 保留文件重建 / 清空全部文件」三档，普通盒内
命令不需要逐条确认，**清空文件需要用户确认**；重置不清流量与审计账本。

命令形态：

```text
/sandbox                 查看当前用户的安全域、后端/隔离状态与配额
/sandbox reset           重启环境（文件保留）
/sandbox rebuild         按干净镜像重建（工作区/私有 home 保留）
/sandbox wipe confirm    清空全部文件并重建（必须显式 confirm）
/sandbox probe           重新跑一遍隔离能力探测
```

沙箱未启用（``[sandbox].mode=off``）时明确提示这是不安全的宿主模式，不假装已保护。
"""

from __future__ import annotations

from typing import Any

from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.command import Info, Request, Result, new_func
from atri_x.security import RoleUser

__all__ = ["SandboxModule", "new_sandbox"]

_MODES = {
    "restart": "已重启环境（文件保留）",
    "rebuild": "已按干净镜像重建环境（工作区/私有 home 保留）",
    "wipe": "已清空环境全部文件并重建",
}


def _service() -> Any:
    """当前生效的沙箱服务（未启用时返回 ``None``）。"""
    try:
        from atri_x.sandbox.runtime import service_or_none

        return service_or_none()
    except Exception:  # noqa: BLE001 - 沙箱未装配时按未启用处理
        return None


def _tenant(ctx: Any) -> Any:
    from atri_x.sandbox.tenant import tenant_from_context

    return tenant_from_context(ctx)


def _display(value: Any) -> str:
    """把不可信文本压成单行短文本（避免回复里出现换行注入）。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text[:300]


def _format_status(status: dict, tenant: Any) -> str:
    """状态快照的聊天版。"""
    backend = status.get("backend") or {}
    probe = status.get("probe") or {}
    queue = status.get("queue") or {}
    environments = status.get("environments") or []
    ledger = status.get("ledger") or {}
    record = (ledger.get("tenants") or {}).get(tenant.slug) or {}
    lines = [
        f"沙箱安全域：{tenant.display}",
        "后端：{}（{}）".format(
            _display(backend.get("backend") or "?"),
            "可用" if backend.get("available") else "不可用：" + _display(backend.get("error")),
        ),
        "隔离探测：{}".format("通过" if probe.get("ok") else "未通过/未运行"),
        "准入队列：运行 {}/{}，等待 {}".format(
            queue.get("running", 0), queue.get("capacity", 0), queue.get("waiting", 0)
        ),
        "运行环境：{} 个".format(len(environments)),
        "今日流量：{:.1f} MiB（累计 {:.1f} MiB；重置沙箱不会清零）".format(
            int(record.get("bytes_day", 0) or 0) / 1048576,
            int(record.get("bytes_total", 0) or 0) / 1048576,
        ),
        "用法：`/sandbox reset` 重启；`/sandbox rebuild` 保留文件重建；"
        "`/sandbox wipe confirm` 清空文件。",
    ]
    return "\n".join(lines)


async def _handle_status(ctx: Any, service: Any) -> Result:
    tenant = _tenant(ctx)
    status = await service.status(refresh=False)
    return Result(content=_format_status(status, tenant))


async def _handle_probe(ctx: Any, service: Any) -> Result:
    report = await service.probe(refresh=True)
    failed = ", ".join(item.name for item in report.failed)
    if report.ok:
        return Result(content="沙箱隔离探测通过：只读根 / 无 capability / 网络隔离 / 资源硬顶 / 无 docker.sock。")
    return Result(content=f"沙箱隔离探测未通过（{failed or '未知'}）：沙箱会 fail-closed 拒绝执行。")


async def _handle_reset(ctx: Any, service: Any, args: list[str], deps: Deps) -> Result:
    if not args or args[0] in ("reset", "restart"):
        mode = "restart"
    elif args[0] == "rebuild":
        mode = "rebuild"
    elif args[0] == "wipe":
        if "confirm" not in [item.lower() for item in args[1:]]:
            return Result(
                content="清空沙箱全部文件需要二次确认：请发送 `/sandbox wipe confirm`。"
                "（流量与审计账本不会清零；工作区文件会删除。）"
            )
        mode = "wipe"
    else:
        return Result(content="用法：`/sandbox [status|reset|rebuild|wipe confirm|probe]`。")
    tenant = _tenant(ctx)
    result = await service.reset_tenant(tenant, mode=mode)
    if deps.audit is not None:
        deps.audit(
            "sandbox_reset",
            "tenant",
            tenant.display,
            "mode",
            mode,
            "removed",
            result.get("removed", 0),
        )
    return Result(
        content="{}。流量/滥用账本保留；已发出的消息或外部 API 写入不会被撤回。".format(
            _display(result.get("message") or _MODES.get(mode, "已重置"))
        )
    )


def _new_sandbox_handler(deps: Deps) -> Any:
    async def handle(ctx: Any, req: Request) -> Result | None:
        service = _service()
        if service is None:
            return Result(
                content="沙箱未启用（`[sandbox].mode=off`）：当前是显式宿主 Shell 模式（不安全）。"
                "启用沙箱请把 app.toml 的 `[sandbox].mode` 设为 `container` 并重启后台服务。"
            )
        args = [item.strip() for item in (req.args or "").split() if item.strip()]
        action = args[0].lower() if args else "status"
        if action in ("", "status", "show"):
            return await _handle_status(ctx, service)
        if action in ("probe", "doctor"):
            return await _handle_probe(ctx, service)
        return await _handle_reset(ctx, service, args, deps)

    return new_func(
        Info(
            name="sandbox",
            usage="/sandbox [status|reset|rebuild|wipe confirm|probe]",
            description=(
                "Show or reset the per-user sandboxed execution environment. "
                "Ordinary commands inside the box need no per-command confirmation; "
                "wiping all files requires an explicit `confirm` argument. Traffic and "
                "audit ledgers are never cleared by a reset."
            ),
            aliases=["sb"],
            min_role=RoleUser,
        ),
        handle,
    )


def new_sandbox(deps: Deps) -> Any:
    """``/sandbox`` 命令。"""
    return _new_sandbox_handler(deps)


class SandboxModule:
    """注册 ``/sandbox``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_sandbox)
