"""组装内置 DSH 内核（``runtime/dsh-kernel/``），供发行包与本地开发使用。

背景：ATRI-X 通过 ACP 调用 DSH（默认命令 ``dsh --profile acp``）。外部 ``dsh`` 要另装
Node 与全局包；本脚本把「node.exe + DSH 的 ACP 依赖闭包」拷成自包含目录，运行时由
``atri_x.sidecar.kernel`` 解析，不再依赖 PATH 上的 ``dsh``。

流程（产物布局与实测数字见 ``packaging/README.md`` §3）：

1. 从 ``--modules``（已装好 DSH 的 ``node_modules``）按 ``--dump-config`` 的插件名 +
   ``dependencies`` 求闭包，剪掉 ACP 用不到的 Web/桌面前端包；
2. 拷进 ``--out``（默认 ``runtime/dsh-kernel``）并写 ``kernel.json``；
3. **核验**：用产物里的 node 起 ACP 服务端、发 ``initialize`` 帧、读回 ``result``；
   缺包就从源目录自动补拷（``--heal-rounds``）后重试。

脚本只读源目录，不跑 npm、不联网。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 默认产物目录。
DEFAULT_OUT = os.path.join(REPO_ROOT, "runtime", "dsh-kernel")

#: 默认核验用的 ACP profile。
DEFAULT_PROFILE = "acp"

#: DSH 主包（它自己的 bin 就是 ACP 服务端入口）。
DSH_PACKAGE = "@deepseek-ai/dsh"

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # 让「python packaging/assemble_kernel.py」能 import 同目录模块
    sys.path.insert(0, _HERE)

from kernel_manifest import (  # noqa: E402 - 上面刚把同目录塞进 sys.path
    MANIFEST_NAME,
    MANIFEST_FORMAT,
    mb,
    node_version,
    now,
    read_json,
    tree_bytes,
    write_json,
    write_manifest,
)

#: 默认排除：Web/桌面端前端包对 ACP 无用，去掉能省一大截体积。
#: 真正需要时由 ``--heal`` 自动补回（核验会证明）。
DEFAULT_PRUNE = (
    "@deepseek-ai/dsh-client-*",
    "@deepseek-ai/dsh-web*",
    "@deepseek-ai/dsh-host-frontend-static*",
)

#: 启动期缺包时 node 的报错样式（ESM 与 CJS 各一种）。只认包名，不认路径。
_MISSING_PATTERNS = (
    re.compile(r"Cannot find package '([^']+)'"),
    re.compile(r"Cannot find module '([^']+)'"),
)

#: 合法包名（防止把 ``build\x.napi.node`` 这类路径当成包）。
_PACKAGE_NAME = re.compile(r"^(?:@[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)$")

#: ACP ``initialize`` 帧：拿到 result 就说明内核真的起来了。
_INITIALIZE_FRAME = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": 1,
        "clientCapabilities": {},
        "clientInfo": {"name": "atri-x-kernel-verify", "version": "0"},
    },
}


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = _parse_args(argv)
    try:
        return _run(args)
    except _Fail as exc:
        print(f"[kernel] 失败：{exc}", file=sys.stderr)
        return 1


class _Fail(Exception):
    """可预期的失败（打印一句话就退出，不打 traceback）。"""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="assemble_kernel.py",
        description="组装内置 DSH 内核（Node + ACP 依赖闭包 + kernel.json）",
    )
    parser.add_argument(
        "--modules",
        default=os.environ.get("DSH_KERNEL_MODULES", ""),
        help="已安装的 node_modules 目录（必填；也可用 DSH_KERNEL_MODULES）",
    )
    parser.add_argument(
        "--node-exe",
        default=os.environ.get("DSH_KERNEL_NODE", ""),
        help="node.exe 路径（默认从 PATH 找 node）",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"产物目录（默认 {DEFAULT_OUT}）")
    parser.add_argument(
        "--prune",
        default=",".join(DEFAULT_PRUNE),
        help="逗号分隔的通配符：这些包不拷（root 包永不剪枝；传空串表示不剪枝）",
    )
    parser.add_argument("--no-verify", action="store_true", help="跳过 ACP 启动核验")
    parser.add_argument(
        "--heal-rounds", type=int, default=12, help="缺包自动补拷的最大轮数（默认 12）"
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="核验等待秒数")
    parser.add_argument("--force", action="store_true", help="产物目录已存在时先删除")
    parser.add_argument("--json", action="store_true", help="只输出一行 JSON 结果")
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    modules = _require_modules(args.modules)
    node_exe = _find_node(args.node_exe)
    out = os.path.abspath(args.out)
    prune = tuple(item.strip() for item in (args.prune or "").split(",") if item.strip())

    print(f"[kernel] 源 node_modules：{modules}")
    print(f"[kernel] node：{node_exe}")
    print(f"[kernel] 产物：{out}")

    dsh_dir = os.path.join(modules, *DSH_PACKAGE.split("/"))
    if not os.path.isdir(dsh_dir):
        raise _Fail(f"{modules} 里没有 {DSH_PACKAGE}，请指向装好 DSH 的 node_modules")

    roots = _acp_roots(node_exe, modules, timeout=args.timeout)
    print(f"[kernel] acp profile 引用 {len(roots)} 个包")

    if os.path.isdir(out):
        if not args.force:
            raise _Fail(f"产物目录已存在：{out}（加 --force 覆盖）")
        shutil.rmtree(out, ignore_errors=False)
    os.makedirs(out, exist_ok=True)

    # 1) Node 运行时
    node_target = os.path.join(out, "node")
    os.makedirs(node_target, exist_ok=True)
    shutil.copy2(node_exe, os.path.join(node_target, "node.exe"))
    print(f"[kernel] node 已复制：{os.path.getsize(os.path.join(node_target, 'node.exe'))} 字节")

    # 2) 依赖闭包
    closure = _closure(modules, roots + [DSH_PACKAGE], prune=prune)
    copied = _copy_closure(modules, closure, os.path.join(out, "node_modules"), skip=set())
    print(f"[kernel] 复制 {len(copied)} 个包 / {mb(sum(copied.values()))}")

    verified: dict[str, Any] | None = None
    if not args.no_verify:
        print("[kernel] 核验：启动 ACP 服务端并握手 …")
        verified = _verify_with_heal(args, out, modules, prune, copied)
        if not verified["ok"]:
            raise _Fail("核验失败：" + "; ".join(verified["errors"]))
        print(f"[kernel] 核验通过：{verified['detail']}")

    manifest = write_manifest(
        out,
        _kernel_entry(modules),
        _dsh_version(modules),
        copied,
        prune,
        profile=DEFAULT_PROFILE,
        verified=verified if verified else None,
    )
    print(f"[kernel] kernel.json 已写入：{manifest['packages']} 包 / {mb(manifest['bytes'])}")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "out": out,
                    "dshVersion": manifest.get("dshVersion", ""),
                    "nodeVersion": manifest.get("nodeVersion", ""),
                    "packages": manifest.get("packages", 0),
                    "bytes": manifest.get("bytes", 0),
                    "verified": bool(manifest.get("verified")),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"[kernel] 完成：{out}")
    return 0


# ---------------------------------------------------------------------------
# 源目录探查
# ---------------------------------------------------------------------------


def _require_modules(value: str) -> str:
    path = os.path.abspath(os.path.expanduser((value or "").strip())) if value else ""
    if not path or not os.path.isdir(path):
        raise _Fail("需要 --modules <已装好 DSH 的 node_modules 目录>")
    return path


def _find_node(explicit: str) -> str:
    candidate = (explicit or "").strip() or shutil.which("node") or ""
    if not candidate or not os.path.isfile(candidate):
        raise _Fail("找不到 node.exe（用 --node-exe 指定）")
    return os.path.abspath(candidate)


def _dsh_version(modules: str) -> str:
    package = read_json(os.path.join(modules, *DSH_PACKAGE.split("/"), "package.json"))
    return str((package or {}).get("version") or "")


def _dsh_entry(modules: str) -> str:
    package = read_json(os.path.join(modules, *DSH_PACKAGE.split("/"), "package.json"))
    bin_field = (package or {}).get("bin")
    if isinstance(bin_field, str) and bin_field:
        return bin_field
    if isinstance(bin_field, Mapping):
        for value in bin_field.values():
            if isinstance(value, str) and value:
                return value
    return "lib/bin.js"


def _kernel_entry(modules: str) -> str:
    """manifest 里的 ``entry``：相对于产物根的正斜杠路径。"""
    return "node_modules/" + "/".join([*DSH_PACKAGE.split("/"), *_dsh_entry(modules).split("/")])


def _acp_roots(node_exe: str, modules: str, *, timeout: float) -> list[str]:
    """跑 ``--dump-config`` 拿到 acp profile 真正引用的包名（启动失败时退回空表）。"""
    entry = os.path.join(modules, *DSH_PACKAGE.split("/"), *_dsh_entry(modules).split("/"))
    env = _clean_env(modules)
    env.update(
        {
            "DSH_HOME": os.path.join(REPO_ROOT, "build", "kernel-dump", "dsh-home"),
            "DSH_WORKSPACE": os.path.join(REPO_ROOT, "build", "kernel-dump", "ws"),
        }
    )
    os.makedirs(env["DSH_HOME"], exist_ok=True)
    os.makedirs(env["DSH_WORKSPACE"], exist_ok=True)
    command = [node_exe, entry, "--profile", DEFAULT_PROFILE, "--dump-config"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            cwd=modules,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[kernel] dump-config 跑不起来（退化为只按 {DSH_PACKAGE} 求闭包）：{exc}")
        return []
    if completed.returncode != 0:
        print(
            "[kernel] dump-config 失败（退化为只按 %s 求闭包）：%s"
            % (DSH_PACKAGE, (completed.stderr or "").strip()[-300:])
        )
        return []
    names = re.findall(r"name:\s*'(@?[^']+)'", completed.stdout or "")
    roots: list[str] = []
    for name in names:
        package = _package_root(name)
        if package and package not in roots:
            roots.append(package)
    return roots


def _package_root(name: str) -> str:
    """``@scope/pkg/sub`` / ``pkg/sub`` → 包名。"""
    parts = [part for part in name.strip().split("/") if part]
    if not parts:
        return ""
    if parts[0].startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    return parts[0]


# ---------------------------------------------------------------------------
# 闭包
# ---------------------------------------------------------------------------


def _closure(modules: str, roots: Iterable[str], *, prune: tuple[str, ...]) -> dict[str, str]:
    """按 ``dependencies`` 求依赖闭包；返回 包名 → 源目录绝对路径。

    ``prune`` 只作用于非 root 包（root 是 dump-config 直接点名的插件）。
    """
    root_set = {name for name in roots if name}
    seen: dict[str, str] = {}
    stack = sorted(root_set)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        directory = _resolve_package(modules, name)
        if directory is None:
            continue
        if name not in root_set and _matches_any(name, prune):
            continue
        seen[name] = directory
        package = read_json(os.path.join(directory, "package.json")) or {}
        for dep in (package.get("dependencies") or {}):
            if dep not in seen:
                stack.append(str(dep))
    return seen


def _resolve_package(modules: str, name: str) -> str | None:
    directory = os.path.join(modules, *name.split("/"))
    return directory if os.path.isdir(directory) else None


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatchcase

    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _copy_closure(
    modules: str,
    closure: Mapping[str, str],
    target: str,
    *,
    skip: set[str],
) -> dict[str, int]:
    """把闭包里的包拷到 ``target``；返回 包名 → 字节数。"""
    copied: dict[str, int] = {}
    for name, source in sorted(closure.items()):
        if name in skip:
            continue
        destination = os.path.join(target, *name.split("/"))
        if os.path.isdir(destination):
            shutil.rmtree(destination)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
        copied[name] = tree_bytes(destination)
    return copied


def tree_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# 核验（真启动 ACP 服务端）
# ---------------------------------------------------------------------------


def _verify_with_heal(
    args: argparse.Namespace,
    out: str,
    modules: str,
    prune: tuple[str, ...],
    copied: dict[str, int],
) -> dict[str, Any]:
    """启动产物里的 ACP 服务端并握手；缺包就补拷后重试。

    「缺包」从两处取：进程 stderr，以及 DSH 自己写的启动诊断日志（聚合错误会把
    ``Cannot find package`` 藏在 ``errors`` 数组里，只看 stderr 会漏）。
    """
    node_target = os.path.join(out, "node", "node.exe")
    entry = os.path.join(
        out, "node_modules", *DSH_PACKAGE.split("/"), *_dsh_entry(modules).split("/")
    )
    errors: list[str] = []
    for round_index in range(max(0, args.heal_rounds) + 1):
        home = os.path.join(REPO_ROOT, "build", "kernel-verify")
        ok, missing, detail = _verify_once(node_target, entry, out, home, timeout=args.timeout)
        if ok:
            return {
                "ok": True,
                "detail": detail,
                "errors": errors,
                "missing": sorted(missing),
                "rounds": round_index,
            }
        heal = sorted(name for name in missing if name not in copied)
        if not heal:
            errors.append(detail or "启动失败且报告里没有可定位的缺失包")
            return {"ok": False, "detail": "", "errors": errors, "missing": sorted(missing)}
        print(f"[kernel] 第 {round_index + 1} 轮补拷 {len(heal)} 个包：{', '.join(heal)}")
        extra = _closure(modules, heal, prune=())
        added = _copy_closure(modules, extra, os.path.join(out, "node_modules"), skip=set(copied))
        unresolved = [name for name in heal if name not in added]
        if unresolved:
            errors.append("源目录里没有：" + ", ".join(unresolved))
            return {"ok": False, "detail": "", "errors": errors, "missing": sorted(missing)}
        copied.update(added)
    errors.append(f"补拷超过 {args.heal_rounds} 轮仍未通过")
    return {"ok": False, "detail": "", "errors": errors, "missing": sorted(missing)}


def _verify_once(
    node_exe: str, entry: str, out: str, home: str, *, timeout: float
) -> tuple[bool, set[str], str]:
    """起 ACP 服务端 → 发 initialize → 读回 result；返回 (是否通过, 缺包名, 细节)。"""
    workspace = os.path.join(home, "ws")
    home = os.path.join(home, "dsh-home")
    for path in (home, workspace):
        os.makedirs(path, exist_ok=True)
    env = _clean_env(out)
    env.update({"DSH_HOME": home, "DSH_WORKSPACE": workspace})
    frame = json.dumps(_INITIALIZE_FRAME, ensure_ascii=False) + "\n"
    try:
        process = subprocess.Popen(
            [node_exe, entry, "--profile", DEFAULT_PROFILE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=workspace,
        )
    except OSError as exc:
        return False, set(), f"启动失败：{exc}"
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(frame)
        process.stdin.flush()
        result = _read_result_line(process, timeout)
        if result is not None:
            return True, set(), f"initialize 返回 {json.dumps(result, ensure_ascii=False)[:200]}"
        stderr = _drain(process)
        evidence = stderr + "\n" + _startup_log_text(home)
        return False, _missing_packages(evidence), (stderr or "无输出").strip()[-600:]
    finally:
        _terminate(process)


def _startup_log_text(home: str) -> str:
    """把最新一份 DSH 启动诊断日志读成文本（拿聚合错误里的缺包名）。"""
    logs = os.path.join(home, "logs")
    try:
        names = [
            os.path.join(logs, name) for name in os.listdir(logs) if name.startswith("startup-")
        ]
    except OSError:
        return ""
    if not names:
        return ""
    latest = max(names, key=os.path.getmtime)
    try:
        with open(latest, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_result_line(process: subprocess.Popen[str], timeout: float) -> Any:
    """读 stdout 直到出现带 ``result`` 的 JSON-RPC 帧，或超时/进程退出。"""
    import threading

    holder: dict[str, Any] = {}

    def _reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            text = line.strip()
            if not text or not text.startswith("{"):
                continue
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if isinstance(message, dict) and ("result" in message or "error" in message):
                holder["result"] = message.get("result", message.get("error"))
                return

    thread = threading.Thread(target=_reader, name="kernel-verify-reader", daemon=True)
    thread.start()
    thread.join(timeout)
    return holder.get("result")


def _drain(process: subprocess.Popen[str]) -> str:
    """拿进程退出后的 stderr 尾部。"""
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        assert process.stderr is not None
        return process.stderr.read() or ""
    except (OSError, ValueError):
        return ""


def _missing_packages(evidence: str) -> set[str]:
    """从 stderr / 启动日志里抠出缺失的包名（只留合法包名，丢掉文件路径）。"""
    found: set[str] = set()
    for pattern in _MISSING_PATTERNS:
        for match in pattern.finditer(evidence or ""):
            name = match.group(1).strip()
            if not _PACKAGE_NAME.match(name):
                continue
            package = _package_root(name)
            if package and _PACKAGE_NAME.match(package):
                found.add(package)
    return found


def _terminate(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _clean_env(dsh_modules: str) -> dict[str, str]:
    """核验子进程环境：去掉会把外部 DSH 装回链接的变量，避免“验的不是产物”。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("DSH_")}
    env.pop("NODE_PATH", None)
    env["NODE_OPTIONS"] = "--no-warnings"
    return env


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
