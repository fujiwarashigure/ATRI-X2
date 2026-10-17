"""独立健康探针：给容器 HEALTHCHECK / 编排系统 / 运维脚本用。

对应上游：Go 版 Dockerfile 里的
``HEALTHCHECK CMD ["curl","-fsS","--max-time","3","http://127.0.0.1:32171/live"]``。

Python 版 slim 基础镜像里**没有 curl**，所以探针改成标准库实现：

    0 = 健康（HTTP 2xx）
    1 = 不健康（非 2xx、连不上、超时、参数非法）

探到的原文可以直接打到 stdout，也可以按 ``--json`` 输出结构化结果
（键名见 :func:`_lib.probe_health`）。

与 ``atri_x.health.state`` 的 ``/live`` 语义对齐：**只看进程是否还能响应**，
不判断调度心跳、模型 API、QQ OneBot / Telegram 连通性。要判断“就绪”请用
``--ready``（打 ``/ready``）。

用法::

    python deploy/health_probe.py                      # /live，默认 127.0.0.1:32171
    python deploy/health_probe.py --ready              # /ready，调度/数据目录就绪
    python deploy/health_probe.py --url http://host:32171/live --timeout 2
    python deploy/health_probe.py --json               # 输出 {"url":..,"status":..}
    python deploy/health_probe.py --quiet              # 只给退出码

⚠️ 与 ``python -m atri_x --health-probe`` 的关系（待对接）：
容器 HEALTHCHECK 首选应用内置的 ``--health-probe`` 开关（由 ``atri_x.launcher``
同僚实现），本脚本是**不依赖该开关**的独立等价物——它只用标准库发一个 HTTP
请求，因此即使应用没实现这个开关，容器健康检查依然成立。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 32171
LIVE_PATH = "/live"
READY_PATH = "/ready"


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（``--url`` 优先于 ``--host``/``--port``）。"""
    parser = argparse.ArgumentParser(
        prog="health_probe.py",
        description="ATRI-X 独立健康探针（标准库实现，等价于 HEALTHCHECK 里的 curl）",
    )
    parser.add_argument("--url", default="", help="完整 URL；给了它就忽略 --host/--port/--ready")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"默认 {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"默认 {DEFAULT_PORT}")
    parser.add_argument("--ready", action="store_true", help="探 /ready 而不是 /live")
    parser.add_argument("--healthz", action="store_true", help="探 /healthz（需要 ops token）")
    parser.add_argument("--token", default="", help="ops token，用于 /healthz 等受保护接口")
    parser.add_argument("--timeout", type=float, default=3.0, help="单次请求超时秒数，默认 3")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON（键名固定）")
    parser.add_argument("--quiet", action="store_true", help="不输出，只给退出码")
    return parser


def resolve_url(args: argparse.Namespace) -> str:
    """决定要探的 URL：``--url`` > ``--healthz`` > ``--ready`` > ``/live``。"""
    if args.url:
        return args.url
    path = LIVE_PATH
    if args.healthz:
        path = "/healthz"
    elif args.ready:
        path = READY_PATH
    return f"http://{args.host}:{args.port}{path}"


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码（0 健康 / 1 不健康 / 2 参数错误由 argparse 处理）。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    url = resolve_url(args)
    token = args.token or _lib.env_value("ATRIX_OPS_TOKEN")
    probe = _lib.probe_health(url, timeout=args.timeout, token=token)
    healthy = 200 <= probe["status"] < 300

    if not args.quiet:
        if args.json:
            sys.stdout.write(_lib.pretty_json({**probe, "healthy": healthy}) + "\n")
        elif healthy:
            sys.stdout.write(probe["body"].rstrip("\n") + "\n")
        else:
            detail = probe["error"] or f"HTTP {probe['status']}"
            sys.stderr.write(f"health_probe: not healthy: {url}: {detail}\n")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
