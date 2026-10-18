"""``python -m atri_x.agent.dsh.status``：DSH 内核与深度路由的状态自检。

面向运维与排障（不启动 Agent、不起子进程）：

- 读 ``app.toml``（``--config`` 可指定）里的 ``[agent.dsh]``；
- 报告内核是否可用、解析出的启动命令、路由参数与熔断状态；
- ``--score TEXT`` 可以对任意一段话跑打分/判定，用来标定 ``min_score``；
- ``--json`` 输出机器可读结果（脚本/打包验证用）。

退出码：0 表示"内核可用且路由已启用"；1 表示不可用/未启用/配置加载失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from atri_x.agent.dsh.router import DshRouteConfig, DshRouter, score_task

__all__ = ["main", "collect_status"]


def _write(line: str) -> None:
    """按当前控制台编码安全输出（GBK 控制台不抛 UnicodeEncodeError）。"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def collect_status(config_path: str = "", scores: Sequence[str] = ()) -> dict[str, Any]:
    """收集状态快照（异常不抛出，转成 ``error`` 字段）。"""
    report: dict[str, Any] = {
        "configPath": config_path,
        "loaded": False,
        "enabled": False,
        "available": False,
        "command": "",
        "kernel": {},
        "route": {},
        "scores": [],
    }
    try:
        from atri_x.config import load as load_config

        cfg = load_config(config_path) if config_path else load_config()
    except Exception as exc:  # noqa: BLE001 - 状态自检不因为配置坏了就崩
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["loaded"] = True
    try:
        report["configPath"] = str(getattr(cfg, "config_path", "") or config_path)
    except Exception:  # noqa: BLE001
        pass
    section = getattr(getattr(cfg, "agent", None), "dsh", None)
    config = DshRouteConfig.from_section(section)
    router = DshRouter(config)
    report["enabled"] = config.enabled
    report["available"] = router.available()
    report["command"] = router.describe()["command"]
    report["config"] = {
        "minScore": config.min_score,
        "lambdaPenalty": config.lambda_penalty,
        "costRatio": config.cost_ratio,
        "model": config.model,
        "timeoutSeconds": config.timeout_seconds,
        "workspace": config.workspace,
        "home": config.home,
    }
    try:
        from atri_x.sidecar import describe as describe_kernel

        report["kernel"] = describe_kernel()
    except Exception as exc:  # noqa: BLE001 - sidecar 缺失只表示没有内置内核
        report["kernel"] = {"found": False, "error": f"{type(exc).__name__}: {exc}"}
    probe = DshRouter(config)
    report["route"] = probe.describe()
    for text in scores:
        features, score = score_task(text)
        decision = probe.decide(text, estimated_tokens=0)
        report["scores"].append(
            {
                "text": text,
                "score": round(score, 4),
                "features": features.to_dict(),
                "decision": decision.to_dict(),
            }
        )
    return report


def _print_human(report: dict[str, Any]) -> None:
    _write("== DSH 内核 ==")
    kernel = report.get("kernel") or {}
    if kernel.get("found"):
        _write(
            "kernel: found id=%s version=%s node=%s verified=%s"
            % (
                kernel.get("id", ""),
                kernel.get("dshVersion", ""),
                kernel.get("nodeVersion", ""),
                str(bool(kernel.get("verified"))).lower(),
            )
        )
        _write("kernel root: " + str(kernel.get("root", "")))
    else:
        _write("kernel: not found（回退 PATH 上的 dsh）")
    _write("")
    _write("== 深度路由（[agent.dsh]）==")
    if report.get("error"):
        _write("config: ERROR " + str(report["error"]))
        return
    _write("config: " + str(report.get("configPath", "")))
    _write("enabled: " + str(bool(report.get("enabled"))).lower())
    _write("available: " + str(bool(report.get("available"))).lower())
    _write("command: " + str(report.get("command") or "<none>"))
    route = report.get("route") or {}
    _write(
        "route: minScore=%s lambda=%s costRatio=%s model=%r"
        % (
            route.get("minScore", 0),
            route.get("lambdaPenalty", 0),
            route.get("costRatio", 0),
            route.get("model", ""),
        )
    )
    breaker = route.get("breaker") or {}
    _write(
        "breaker: failures=%s open=%s"
        % (breaker.get("failures", 0), str(bool(breaker.get("open"))).lower())
    )
    for item in report.get("scores") or []:
        decision = item.get("decision") or {}
        _write(
            "score %.3f use=%s reason=%s  %s"
            % (
                item.get("score", 0.0),
                str(bool(decision.get("useDsh"))).lower(),
                decision.get("reason", ""),
                item.get("text", ""),
            )
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m atri_x.agent.dsh.status",
        description="报告内置 DSH 内核与 [agent.dsh] 深度路由状态",
    )
    parser.add_argument("--config", default="", help="app.toml 路径（默认按标准顺序查找）")
    parser.add_argument(
        "--score", action="append", default=[], help="对一段文本跑打分（可重复）"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口；返回进程退出码。"""
    args = _parse_args(argv)
    report = collect_status(args.config, args.score)
    if args.json:
        _write(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report.get("available") and report.get("enabled") else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    sys.exit(main())
