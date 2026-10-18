"""``python -m atri_x.sidecar``：查看/核验随包安装的运行时。

用法::

    python -m atri_x.sidecar list                 # 已发现的运行时
    python -m atri_x.sidecar status               # 内置 DSH 内核状态
    python -m atri_x.sidecar dsh-command          # 打印启动命令（没有则退出码 1）
    python -m atri_x.sidecar verify               # 清单 + 关键文件 + node --version
    python -m atri_x.sidecar verify --acp         # 再真跑一次 ACP initialize 握手

输出默认是中文短句；``--json`` 给一行 JSON（给脚本/测试用）。所有输出走 ASCII
兜底包装，GBK 控制台不会因为字符挂掉（见 ``atri_x/__main__.py`` 同一套做法）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Sequence

from atri_x.sidecar.kernel import describe, dsh_command, find_kernel
from atri_x.sidecar.registry import RUNTIME_DIR_ENV, discover, runtime_dirs

__all__ = ["main", "command_status", "command_verify"]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口，返回退出码。"""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command == "list":
        return command_list(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "dsh-command":
        return command_dsh_command(args)
    return command_verify(args)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m atri_x.sidecar", description="运行时（sidecar）查看与核验")
    parser.add_argument("--json", action="store_true", help="输出一行 JSON")
    common = argparse.ArgumentParser(add_help=False)
    # 允许 ``--json`` 写在子命令后面（argparse 的全局选项默认只能写在前面）。
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出一行 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="列出已发现的运行时")
    sub.add_parser("status", parents=[common], help="内置 DSH 内核状态")
    command = sub.add_parser("dsh-command", parents=[common], help="打印内置内核的启动命令")
    command.add_argument("--profile", default="", help="ACP profile 名（默认取清单里的 acp）")
    verify_parser = sub.add_parser("verify", parents=[common], help="核验内置 DSH 内核")
    verify_parser.add_argument("--acp", action="store_true", help="真跑一次 ACP initialize 握手")
    verify_parser.add_argument("--timeout", type=float, default=120.0, help="握手超时（秒）")
    return parser.parse_args(argv)


def command_list(args: argparse.Namespace) -> int:
    """``list``：扫描运行时根目录。"""
    runtimes = [
        {
            "id": runtime.id,
            "kind": runtime.kind,
            "version": runtime.version,
            "root": runtime.root,
            "source": runtime.source,
        }
        for runtime in discover()
    ]
    if args.json:
        _write(json.dumps({"runtimes": runtimes, "dirs": runtime_dirs()}, ensure_ascii=False) + "\n")
        return 0
    if not runtimes:
        _write("没有发现运行时。\n")
        _write(f"搜索路径：{_join(runtime_dirs())}\n")
        _write(f"（可用 {RUNTIME_DIR_ENV} 指定运行时根目录）\n")
        return 1
    for item in runtimes:
        _write(f"{item['id']:<12} {item['version']:<12} {item['root']}\n")
    return 0


def command_status(args: argparse.Namespace) -> int:
    """``status``：内置 DSH 内核状态。"""
    info = describe()
    if args.json:
        _write(json.dumps(info, ensure_ascii=False) + "\n")
        return 0 if info.get("found") else 1
    if not info.get("found"):
        _write("内置 DSH 内核：未找到\n")
        _write(f"搜索路径：{_join(info.get('searched') or [])}\n")
        return 1
    _write(f"内置 DSH 内核：{info['root']}\n")
    _write(f"  dsh      {info['dshVersion']}（entry={info['entry']}）\n")
    _write(f"  node     {info['nodeVersion']}（{info['node']}）\n")
    _write(f"  核验      {'通过' if info['verified'] else '未核验'}\n")
    if info.get("missing"):
        _write(f"  缺失      {_join(info['missing'])}\n")
    return 0 if not info.get("missing") else 1


def command_dsh_command(args: argparse.Namespace) -> int:
    """``dsh-command``：打印可直接执行的命令（shell 引用按 cmd 规则）。"""
    command = dsh_command(getattr(args, "profile", "") or "")
    if command is None:
        _write("未找到可用的内置 DSH 内核。\n")
        return 1
    text = args.json and json.dumps({"command": list(command)}, ensure_ascii=False) + "\n"
    _write(text or " ".join(_quote(part) for part in command) + "\n")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    """``verify``：清单/文件核验，``--acp`` 时再真跑一次握手。"""
    kernel = find_kernel()
    if kernel is None:
        payload: dict[str, Any] = {"found": False, "ok": False}
        _write(json.dumps(payload, ensure_ascii=False) + "\n" if args.json else "未找到内置 DSH 内核。\n")
        return 1
    missing = kernel.missing()
    payload: dict[str, Any] = {
        "found": True,
        "ok": not missing,
        "root": kernel.root,
        "dshVersion": kernel.dsh_version,
        "nodeVersion": kernel.node_version,
        "verified": kernel.verified,
        "missing": missing,
    }
    if missing:
        _write(json.dumps(payload, ensure_ascii=False) + "\n" if args.json else "缺失：" + _join(missing) + "\n")
        return 1
    node_ok, node_text = _node_version(kernel.node)
    payload["nodeRuns"] = node_ok
    payload["nodeReport"] = node_text
    if not node_ok:
        _write(json.dumps(payload, ensure_ascii=False) + "\n" if args.json else "node 起不来：" + node_text + "\n")
        return 1
    if getattr(args, "acp", False):
        handshake = _acp_handshake(kernel, float(getattr(args, "timeout", 120.0)))
        payload.update(handshake)
        if not handshake.get("acpOk"):
            _write(json.dumps(payload, ensure_ascii=False) + "\n" if args.json else "ACP 握手失败：" + str(handshake.get("acpError") or "") + "\n")
            return 1
    if args.json:
        _write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        _write("内置 DSH 内核核验通过：" + kernel.root + "\n")
        if payload.get("acpOk"):
            _write(f"  ACP 握手成功（{payload.get('acpAgent') or 'unknown'}）\n")
    return 0


def _node_version(node_exe: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [node_exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    text = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return completed.returncode == 0, text


def _acp_handshake(kernel: Any, timeout: float) -> dict[str, Any]:
    """用仓库里现成的 ACP 客户端真连一次内置内核。

    用独立的 ``DSH_HOME`` / ``DSH_WORKSPACE``（放在用户数据目录下）：
    核验只证明「内核能起来并握手」，不该往用户真正的 DSH 会话目录里写东西。
    """
    from atri_x.agent.dsh import AcpClient  # noqa: PLC0415 - 只在核验路径导入

    home = os.path.join(_verify_root(), "dsh-home")
    workspace = os.path.join(_verify_root(), "ws")
    try:
        os.makedirs(home, exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
    except OSError as exc:
        return {"acpOk": False, "acpError": f"无法创建核验目录：{exc}"}
    env = {"DSH_HOME": home, "DSH_WORKSPACE": workspace}
    try:
        client = AcpClient(kernel.command(), env=env, prompt_timeout=timeout)
        try:
            info = client.start()
        finally:
            client.stop()
    except Exception as exc:  # noqa: BLE001 - 核验失败要把原因带出去
        return {"acpOk": False, "acpError": f"{type(exc).__name__}: {exc}"}
    return {
        "acpOk": True,
        "acpAgent": f"{info.agent_name}/{info.agent_version}",
        "acpProtocol": info.protocol_version,
    }


def _verify_root() -> str:
    """核验用的工作目录：按可写性挑第一个候选。

    候选顺序：``ATRIX_SIDECAR_VERIFY_DIR`` → ``<发行包>/build/sidecar-verify``
    → ``%LOCALAPPDATA%\\ATRI-X\\sidecar-verify``。

    为什么程序目录优先：DSH 自己带 ACL 沙箱，把 ``DSH_HOME`` 放到「工作目录之外的
    另一个盘/被策略限制的目录」时会在建 profile 时收到 ``EPERM``；放在同一棵树里最稳。
    数据目录只读的环境仍会退到 ``%LOCALAPPDATA%``。
    """
    candidates: list[str] = []
    explicit = (os.environ.get("ATRIX_SIDECAR_VERIFY_DIR") or "").strip()
    if explicit:
        candidates.append(os.path.abspath(os.path.expanduser(explicit)))
    from atri_x.sidecar.registry import bundle_root  # noqa: PLC0415 - 只在这里用

    bundle = bundle_root()
    if getattr(sys, "frozen", False):
        # 冻结的 onedir 里 ``bundle_root()`` 是 ``<app>/_internal``；核验产物必须放在
        # onedir **外面**（``<app>/../sidecar-verify``），否则会被下一次 PyInstaller
        # 打包/安装包带进发行物 —— 实测过 ``_internal/build/sidecar-verify`` 混进 dist。
        parent = os.path.dirname(os.path.abspath(bundle))
        candidates.append(os.path.join(parent, "sidecar-verify"))
    else:
        candidates.append(os.path.join(bundle, "build", "sidecar-verify"))
    base = (
        (os.environ.get("LOCALAPPDATA") or os.environ.get("LocalAppData") or "").strip()
        or (os.environ.get("APPDATA") or os.environ.get("AppData") or "").strip()
    )
    if base:
        candidates.append(os.path.join(base, "ATRI-X", "sidecar-verify"))
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError:
            continue
        return candidate
    return candidates[-1]


def _quote(part: str) -> str:
    text = str(part)
    return '"' + text + '"' if (" " in text or "\t" in text) else text


def _join(values: Any) -> str:
    return ", ".join(str(item) for item in (values or []))


def _write(text: str) -> None:
    """按 GBK 兜底写 stdout。"""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "backslashreplace").decode("ascii"))
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
