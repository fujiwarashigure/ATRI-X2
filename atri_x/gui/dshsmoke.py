"""内置 DSH 内核的端到端自检（真的拉起内核跑一轮 ACP prompt）。

「DSH」页的「端到端自检」按钮走这里：链路与后台服务 ``DshRuntime`` 相同 ——
加载配置 → 解析凭据计划 → 需要时起回环头代理 → ``AcpClient``（内置内核）→
``session/new`` → 可选 ``session/set_config_option(model)`` → ``session/prompt``。

只用于界面按钮与排障脚本：不常驻、不复用后台服务的连接，跑完立即收掉子进程。
任何异常都收敛成 ``{"ok": False, "error": ...}``，界面上直接显示原因。
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

__all__ = ["run_smoke"]

#: 默认提示词：短、确定、不容易被安全策略拦。
DEFAULT_PROMPT = "用一句话回答：2+2 等于几？只输出答案。"


def run_smoke(
    config_path: str,
    *,
    prompt: str = DEFAULT_PROMPT,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """拉起内置内核真跑一轮 prompt，返回可展示的结果字典（绝不抛异常）。"""
    client: Any = None
    proxy: Any = None
    try:
        from atri_x.agent.dsh.client import AcpClient
        from atri_x.agent.dsh.command import format_command, resolve_command
        from atri_x.agent.dsh.credentials import resolve_credential_plan
        from atri_x.agent.dsh.gateway import HeaderProxy
        from atri_x.agent.dsh.router import DshRouteConfig
        from atri_x.agent.dsh.workspace import child_environment, resolve_paths
        from atri_x.config import load as load_config

        cfg = load_config(config_path)
        route = DshRouteConfig.from_section(getattr(getattr(cfg, "agent", None), "dsh", None))
        plan = resolve_credential_plan(cfg, route)
        if not plan.usable:
            return {"ok": False, "stage": "credential", "error": plan.reason}
        stub = SimpleNamespace(
            state_path=os.path.join(os.path.dirname(config_path), "state.toml")
        )
        paths = resolve_paths(stub, route)
        command = resolve_command(route.command)
        if not command:
            return {
                "ok": False,
                "stage": "kernel",
                "error": "找不到可用的 DSH 内核（内置 runtime 与 PATH 都没有）",
            }
        env = child_environment(plan.env)
        for name in [key for key in env if str(key).upper().startswith("DSH_")]:
            env.pop(name, None)
        env["DSH_HOME"] = paths.home
        upstream = ""
        if plan.gateway_spec is not None:
            proxy = HeaderProxy(plan.gateway_spec)
            upstream = proxy.start()
            env[route.credential_base_url_env or "DEEPSEEK_BASE_URL"] = upstream
        client = AcpClient(
            command,
            cwd=paths.workspace,
            env=env,
            isolate_env=True,
            prompt_timeout=timeout,
        )
        client.start()
        session = client.new_session(paths.workspace)
        warnings: list[str] = []
        if route.model:
            # 与生产 `DshRuntime._apply_overrides` 同口径：内核拒绝该覆盖时降级为内核默认、
            # 记一条 warning，不让整轮自检失败（用户要看到的是"能不能跑通"）。
            try:
                client.set_config_option(session.session_id, "model", route.model)
            except Exception as exc:  # noqa: BLE001 - 覆盖被拒只降级
                warnings.append(f"model override rejected: {exc}")
        result = client.prompt(session.session_id, prompt)
        return {
            "ok": result.stop_reason == "end_turn",
            "stage": "prompt",
            "stopReason": result.stop_reason,
            "answer": result.text,
            "model": session.current_value("model"),
            "warnings": warnings,
            "command": format_command(command),
            "gateway": bool(plan.gateway_spec is not None),
            "gatewayUpstream": upstream,
            "elapsedSeconds": round(float(result.elapsed_seconds), 2),
            "usage": result.usage.to_dict(),
        }
    except Exception as exc:  # noqa: BLE001 - 自检要把原因原样带回界面
        tail = ""
        try:
            tail = client.stderr_tail() if client is not None else ""
        except Exception:  # noqa: BLE001
            tail = ""
        return {
            "ok": False,
            "stage": "exception",
            "error": f"{type(exc).__name__}: {exc}",
            "stderrTail": (tail or "")[-3000:],
        }
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001
                pass
        if proxy is not None:
            try:
                proxy.close()
            except Exception:  # noqa: BLE001
                pass
