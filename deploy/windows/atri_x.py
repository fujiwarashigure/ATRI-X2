"""ATRI-X Windows 本地容器部署 / 后台控制（Python 重写版）。

对应上游：``deploy/windows/atri-x.ps1``（822 行 Windows PowerShell）。

上游那个脚本用 PowerShell 实现全部动作，其中 ``backup`` / ``restore-verify`` /
``upgrade`` / ``rollback`` 四个动作需要**再找 Git for Windows 的 Git Bash**去调
``deploy/*.sh``（``Resolve-Bash`` + ``Invoke-BashScript`` + ``Convert-ToBashPath``
一大套路径转换）。Python 版把这一段彻底去掉：那四个 ``.sh`` 已经在本仓重写成
``.py``，直接 :func:`subprocess` 调起同一个解释器即可，Windows 上不再需要 Git Bash。

用法::

    python deploy\\windows\\atri_x.py <动作> [参数...]
    .\\deploy\\windows\\atri-x.cmd <动作> [参数...]        # 极薄引导壳
    .\\deploy\\windows\\atri-x.ps1 <动作> [参数...]        # 极薄引导壳

动作集合与上游 ``ValidateSet`` **逐个对齐**（见 :data:`ACTIONS`）。

有意偏差（Python 版，逐条列在 ``deploy/windows/README.md``）：
1. 不再需要 Git Bash；``backup`` / ``restore-verify`` / ``upgrade`` / ``rollback``
   直接调 ``deploy/*.py``。
2. 上游 ``Assert-BackupVerificationDependencies`` 要求宿主有 ``sqlite3`` 命令与带
   ``tomllib`` 的 python。Python 版两个前提都不成立也不需要：``sqlite3`` 与
   ``tomllib`` 都是标准库，所以这个断言整条删除（比上游更不容易被环境卡住）。
3. 容器内命令从 ``atri-x doctor`` 改成 ``python -m atri_x doctor``（Python 版镜像
   ENTRYPOINT 已经是 ``python -m atri_x``）。
4. 计划任务的动作从 ``powershell.exe -File atri-x.ps1 start -NoBuild`` 改成
   ``python atri_x.py start --no-build``，不再经过 PowerShell。
5. 上游 ``-Json`` / ``-Follow`` / ``-Tail`` / ``-NoBuild`` 这些 PowerShell 开关在
   Python 版里以 ``--json`` / ``--follow`` / ``--tail N`` / ``--no-build`` 形式
   同时接受短横线与下划线两种写法。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_WINDOWS_DIR = _HERE.parent
_DEPLOY = _HERE.parents[1]
_REPO = _HERE.parents[2]
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

#: 动作集合，与上游 ``[ValidateSet(...)]`` 完全一致（``up``/``ps`` 是别名）。
ACTIONS = (
    "help",
    "init",
    "build",
    "start",
    "up",
    "recreate",
    "stop",
    "down",
    "restart",
    "status",
    "ps",
    "health",
    "tasks",
    "metrics",
    "diagnostics",
    "doctor",
    "logs",
    "shell",
    "exec",
    "backup",
    "restore-verify",
    "rollback",
    "upgrade",
    "install-service",
    "uninstall-service",
    "version",
    "compose",
)

ALIASES = {"up": "start", "ps": "status"}
SERVICE = "atri-x"
TASK_NAME = "ATRI-X-Docker"
HEALTH_BASE_DEFAULT = "http://127.0.0.1:32171"

#: 直接转发给 ``deploy/*.py`` 的动作 → 脚本文件名。
DEPLOY_SCRIPTS = {
    "backup": "backup.py",
    "restore-verify": "restore_verify.py",
    "upgrade": "upgrade.py",
    "rollback": "rollback.py",
}


class Context:
    """一次调用需要的全部路径与开关（上游那一堆 ``$script:`` 变量）。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.repo_root = _REPO
        self.deploy_dir = _DEPLOY
        self.compose_file = self.deploy_dir / "docker-compose.yml"
        self.env_file = self.deploy_dir / ".env"
        self.env_example = self.deploy_dir / ".env.example"
        self.data_dir = self.deploy_dir / "data"
        self.backups_dir = self.deploy_dir / "backups"
        self.version_file = self.deploy_dir / "VERSION"
        self.windows_dir = _WINDOWS_DIR
        self.health_base = _lib.env_value("ATRIX_HEALTH_URL", HEALTH_BASE_DEFAULT).rstrip("/")
        self.action = ALIASES.get(args.action, args.action)
        self.rest: list[str] = list(args.rest or [])
        self.follow = bool(args.follow)
        self.tail = int(args.tail)
        self.no_build = bool(args.no_build)
        self.json = bool(args.json)
        self.version = self.read_version()

    def read_version(self) -> str:
        """本地源码版本以 ``deploy/VERSION`` 为准（上游 ``Get-Version``）。"""
        return _lib.read_text(self.version_file).strip() or "dev"

    def ops_token(self) -> str:
        """ops token：进程环境优先，其次 ``.env``（上游 ``Get-OpsToken``）。"""
        token = _lib.env_value("ATRIX_OPS_TOKEN")
        if token:
            return token.strip()
        return _lib.read_env_file(self.env_file, "ATRIX_OPS_TOKEN").strip()


# ============================================================
# 前置断言
# ============================================================
def assert_env_file(ctx: Context) -> None:
    """``.env`` 不存在就直接报错并给出下一步命令。"""
    if not ctx.env_file.is_file():
        _lib.die(
            f"未找到 {ctx.env_file}。请先运行 python deploy\\windows\\atri_x.py init，"
            "然后编辑 .env 填写 API Key / token。"
        )


def docker_compose(ctx: Context) -> list[str]:
    """探测 Compose 前端；找不到就报错（上游 ``Get-DockerCompose``）。"""
    found = _lib.compose_command()
    if not found:
        _lib.die(
            "未找到 docker compose / docker-compose：请安装并启动 Docker Desktop，"
            "且切换到 Linux containers。"
        )
    return found


def assert_docker_running(ctx: Context) -> None:
    """确认 ``docker info`` 能通（上游 ``Assert-DockerRunning``）。"""
    if not _lib.has_command("docker"):
        _lib.die("未找到 docker.exe：请安装 Docker Desktop，并在设置中使用 WSL2 后端 + Linux containers。")
    if _lib.run_capture(["docker", "info"], timeout=60.0).returncode != 0:
        _lib.die("Docker 守护进程不可用：请先启动 Docker Desktop，等待托盘状态变为 Running。")


def wait_docker_running(ctx: Context, timeout_seconds: int = 120) -> None:
    """等 Docker Desktop 起来（最多 ``timeout_seconds`` 秒）。"""
    import time

    _lib.log(f"等待 Docker Desktop 守护进程（最长 {timeout_seconds}s）")
    deadline = _lib.now_epoch() + timeout_seconds
    while _lib.now_epoch() < deadline:
        if _lib.has_command("docker") and _lib.run_capture(["docker", "info"], timeout=30.0).returncode == 0:
            return
        time.sleep(3)
    _lib.die(f"Docker 守护进程在 {timeout_seconds}s 内未就绪：请确认 Docker Desktop 已启动并使用 Linux containers。")


def ensure_data_dirs(ctx: Context) -> None:
    """建好数据目录（上游 ``New-AtrixDataDirs``）。"""
    for path in (
        ctx.data_dir,
        ctx.data_dir / "config",
        ctx.data_dir / "run",
        ctx.data_dir / "logs",
        ctx.data_dir / "cache",
        ctx.backups_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


# ============================================================
# Compose 调用
# ============================================================
def compose_argv(ctx: Context) -> list[str]:
    """``<compose> --env-file <env> -f <compose file>``（上游 ``Invoke-Compose`` 的头部）。

    始终显式带 ``--env-file``：本脚本可能从任意工作目录被调用，
    否则 compose 会去找**当前目录**的 ``.env`` 而不是 ``deploy/.env``。
    """
    compose = docker_compose(ctx)
    return [*compose, "--env-file", str(ctx.env_file), "-f", str(ctx.compose_file)]


def invoke_compose(ctx: Context, args: list[str], *, inherit: bool = True, timeout: float = 1800.0) -> int:
    """跑一条 compose 命令；``inherit=True`` 时输出直连终端。

    始终补上 ``ATRIX_VERSION`` / ``ATRIX_IMAGE``（上游 ``Set-LocalComposeEnv``），
    用 ``setdefault`` 而不是直接赋值：用户显式设置的环境变量优先。
    """
    env = dict(os.environ)
    env.setdefault("ATRIX_VERSION", ctx.version)
    env.setdefault("ATRIX_IMAGE", f"atri-x:{ctx.version}")
    argv = compose_argv(ctx) + args
    if inherit:
        return _lib.run_inherit(argv, cwd=ctx.deploy_dir, env=env)
    result = _lib.run_capture(argv, cwd=ctx.deploy_dir, env=env, timeout=timeout)
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    return result.returncode


# ============================================================
# 健康与状态
# ============================================================
def get_health(ctx: Context, path: str) -> tuple[bool, object]:
    """取一个健康接口的值，返回 ``(是否成功, 解析后的值或 None)``。

    对齐上游 ``Get-Health`` 的两个关键语义：
    - **401 必须明确报认证失败**，不能把 JSON 错误体当普通健康结果；
    - ``/ready`` 在 not_ready 时返回 503 + JSON，要把响应体解析出来，
      这样 status 才不会把“进程活着但未就绪”误报成“接口不可达”。
    """
    token = ctx.ops_token()
    status, body = _lib.http_get(f"{ctx.health_base}{path}", timeout=5.0, token=token)
    if status == 401:
        _lib.die(f"{path} 认证失败（HTTP 401）：请检查 ATRIX_OPS_TOKEN 与服务端是否一致。")
    if status == 0:
        return False, None
    text = (body or "").strip()
    if not text:
        return True, None
    try:
        return True, json.loads(text)
    except ValueError:
        return True, text


def health_ready(value: object) -> bool:
    """判断一个健康响应是否表示 ready（上游 ``Test-HealthReady``）。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() == "ready"
    if isinstance(value, dict):
        if "ready" in value:
            return bool(value["ready"])
        if "status" in value:
            return str(value["status"]).strip().lower() == "ready"
    return False


def format_health(value: object) -> str:
    """把健康响应压成一行（上游 ``Format-HealthValue``）。"""
    if value is None:
        return "unreachable"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "status" in value:
        text = str(value["status"])
        if value.get("version"):
            text += f" version={value['version']}"
        if "uptime_seconds" in value:
            text += f" uptime={value['uptime_seconds']}s"
        return text
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def container_info(ctx: Context) -> dict | None:
    """读容器的 ``State.Status|Health|Image``（上游 ``Get-ContainerInfo``）。"""
    result = _lib.run_capture(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.Config.Image}}",
            SERVICE,
        ],
        timeout=30.0,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    parts = (result.stdout or "").strip().split("|", 2)
    while len(parts) < 3:
        parts.append("")
    return {"state": parts[0], "health": parts[1], "image": parts[2]}


def show_status(ctx: Context) -> int:
    """打印状态汇总（上游 ``Show-Status``），``--json`` 时输出 JSON。"""
    live_ok, live = get_health(ctx, "/live")
    _, ready = get_health(ctx, "/ready")
    _, healthz = get_health(ctx, "/healthz")
    container = container_info(ctx)
    if ctx.json:
        sys.stdout.write(
            _lib.pretty_json(
                {
                    "version": ctx.version,
                    "compose_file": str(ctx.compose_file),
                    "env_file": str(ctx.env_file),
                    "data_dir": str(ctx.data_dir),
                    "docker": _lib.has_command("docker"),
                    "compose": _lib.compose_command() is not None,
                    "container": container,
                    "task_registered": is_task_registered(),
                    "live": live,
                    "ready": ready,
                    "healthz": healthz,
                    "ops_token_configured": bool(ctx.ops_token()),
                }
            )
            + "\n"
        )
    else:
        compose_available = _lib.compose_command() is not None
        sys.stdout.write(
            "\n".join(
                [
                    "",
                    "ATRI-X Windows 本地容器状态",
                    f"  版本        : {ctx.version}",
                    f"  Compose     : {ctx.compose_file}",
                    f"  .env        : {ctx.env_file}",
                    f"  数据目录    : {ctx.data_dir}",
                    f"  Docker      : {'available' if _lib.has_command('docker') else 'missing'}",
                    f"  Compose     : {'available' if compose_available else 'missing'}",
                    f"  容器        : {container if container else 'not found'}",
                    f"  计划任务    : {TASK_NAME if is_task_registered() else '未注册'}",
                    f"  /live       : {format_health(live)}",
                    f"  /ready      : {format_health(ready)}",
                    f"  /healthz    : {format_health(healthz)}",
                    f"  Ops token   : {'已配置' if ctx.ops_token() else '未配置（仅回环可访问）'}",
                    "",
                    "常用命令：",
                    "  .\\deploy\\windows\\atri-x.cmd start",
                    "  .\\deploy\\windows\\atri-x.cmd health",
                    "  .\\deploy\\windows\\atri-x.cmd doctor --no-model",
                    "  .\\deploy\\windows\\atri-x.cmd logs --follow --tail 200",
                    "  .\\deploy\\windows\\atri-x.cmd tasks",
                    "  .\\deploy\\windows\\atri-x.cmd metrics",
                    "  .\\deploy\\windows\\atri-x.cmd diagnostics",
                    "",
                ]
            )
        )
    if not live_ok or not health_ready(ready):
        return 1
    return 0


def wait_ready(ctx: Context, timeout_seconds: int = 90) -> bool:
    """轮询 ``/ready``（上游 ``Wait-AtrixReady``）。"""
    import time

    deadline = _lib.now_epoch() + timeout_seconds
    while _lib.now_epoch() < deadline:
        _, ready = get_health(ctx, "/ready")
        if health_ready(ready):
            return True
        time.sleep(2)
    return False


def show_logs_hint(ctx: Context) -> None:
    """就绪失败时提示怎么排查（上游 ``Show-LogsHint``）。"""
    _lib.warn("查看日志：.\\deploy\\windows\\atri-x.cmd logs --follow --tail 200")
    _lib.warn("聚合诊断：.\\deploy\\windows\\atri-x.cmd diagnostics")


def invoke_ops_endpoint(ctx: Context, path: str) -> int:
    """取 ``/tasks`` / ``/metrics`` / ``/diagnostics`` 并以 JSON 打印。"""
    ok, value = get_health(ctx, path)
    if not ok:
        _lib.die(
            f"{path} 不可达。确认容器正在运行、32171 端口已映射，"
            "且 ATRIX_OPS_TOKEN 与服务端一致。"
        )
    sys.stdout.write(_lib.pretty_json(value) + "\n")
    return 0


# ============================================================
# 计划任务（上游用 ScheduledTask，Python 版用 schtasks）
# ============================================================
def scheduled_task_command() -> list[str]:
    """注册计划任务要执行的命令：直接调 Python，不再经过 PowerShell。"""
    return [
        sys.executable,
        str(_HERE),
        "start",
        "--no-build",
    ]


def is_task_registered() -> bool:
    """计划任务是否存在（``schtasks /query``）。"""
    if not _lib.has_command("schtasks"):
        return False
    result = _lib.run_capture(["schtasks", "/query", "/tn", TASK_NAME], timeout=30.0)
    return result.returncode == 0


def install_service(ctx: Context) -> int:
    """注册「登录后自动 start --no-build」的计划任务。"""
    if not _lib.has_command("schtasks"):
        _lib.die("未找到 schtasks：请在 Windows 上运行本动作。")
    command = " ".join(f'"{part}"' if " " in part else part for part in scheduled_task_command())
    result = _lib.run_capture(
        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", command, "/sc", "onlogon", "/f"],
        timeout=60.0,
    )
    if result.returncode != 0:
        sys.stderr.write((result.stdout or "") + (result.stderr or ""))
        _lib.die(f"注册计划任务 {TASK_NAME} 失败")
    _lib.log(f"已注册计划任务 {TASK_NAME}：登录 Windows 后自动执行 start --no-build")
    sys.stdout.write(f"  查看：schtasks /query /tn {TASK_NAME} /v /fo LIST\n")
    sys.stdout.write("  移除：.\\deploy\\windows\\atri-x.cmd uninstall-service\n")
    return 0


def uninstall_service(ctx: Context) -> int:
    """移除计划任务。"""
    if not is_task_registered():
        _lib.log(f"计划任务 {TASK_NAME} 不存在")
        return 0
    result = _lib.run_capture(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], timeout=60.0)
    if result.returncode != 0:
        sys.stderr.write((result.stdout or "") + (result.stderr or ""))
        _lib.die(f"移除计划任务 {TASK_NAME} 失败")
    _lib.log(f"已移除计划任务 {TASK_NAME}")
    return 0


# ============================================================
# 各动作
# ============================================================
def do_init(ctx: Context) -> int:
    """建数据目录 + 生成 ``.env``（不覆盖已有 ``.env``）。"""
    ensure_data_dirs(ctx)
    if not ctx.env_file.is_file():
        if not ctx.env_example.is_file():
            _lib.die(f"缺少 {ctx.env_example}，无法生成 .env")
        _lib.write_text(ctx.env_file, _lib.read_text(ctx.env_example))
        _lib.log(f"已生成 {ctx.env_file}")
    else:
        _lib.log(f"{ctx.env_file} 已存在，保持不变")
    sys.stdout.write(
        "\n".join(
            [
                "",
                "下一步：",
                f"  1. 编辑 {ctx.env_file}，至少填写 LLM API Key、ATRIX_CLI_LOCAL_TOKEN、ATRINIS_HOME_TOKEN（如启用）。",
                "  2. 运行 .\\deploy\\windows\\atri-x.cmd start 构建并后台启动容器。",
                "  3. 运行 .\\deploy\\windows\\atri-x.cmd status 查看容器和 /live /ready /healthz。",
                "  4. 首次启动后按 deploy/windows/README.md 检查 data/config/atri-x/*.toml。",
                "",
            ]
        )
    )
    return 0


def do_build(ctx: Context) -> int:
    """构建镜像。"""
    assert_env_file(ctx)
    assert_docker_running(ctx)
    ensure_data_dirs(ctx)
    if invoke_compose(ctx, ["build", *ctx.rest]) != 0:
        _lib.die("docker compose build 失败")
    _lib.log("镜像构建完成")
    return 0


def do_start(ctx: Context) -> int:
    """后台启动容器（默认 ``--build``；``--no-build`` 跳过构建）。"""
    assert_env_file(ctx)
    wait_docker_running(ctx, 180)
    ensure_data_dirs(ctx)
    args = ["up", "-d", "--remove-orphans"]
    if not ctx.no_build:
        args.append("--build")
    if invoke_compose(ctx, args + ctx.rest) != 0:
        _lib.die("docker compose up 失败")
    if not wait_ready(ctx):
        _lib.warn("容器已启动，但 /ready 在 90 秒内未就绪。")
        show_logs_hint(ctx)
        return 1
    _lib.log("ATRI-X 已就绪")
    return show_status(ctx)


def do_recreate(ctx: Context) -> int:
    """强制重建容器并重新注入 ``.env``。"""
    assert_env_file(ctx)
    wait_docker_running(ctx, 180)
    ensure_data_dirs(ctx)
    args = ["up", "-d", "--remove-orphans", "--force-recreate", "--build"]
    if invoke_compose(ctx, args + ctx.rest) != 0:
        _lib.die("docker compose up --force-recreate 失败")
    if not wait_ready(ctx):
        _lib.warn("容器已重建，但 /ready 在 90 秒内未就绪。")
        show_logs_hint(ctx)
        return 1
    _lib.log("ATRI-X 已重建并就绪")
    return show_status(ctx)


def do_stop(ctx: Context) -> int:
    """停止容器（保留容器与数据）。"""
    assert_env_file(ctx)
    assert_docker_running(ctx)
    if invoke_compose(ctx, ["stop", *ctx.rest]) != 0:
        _lib.die("docker compose stop 失败")
    _lib.log("ATRI-X 容器已停止（数据保留在 deploy/data）")
    return 0


def do_down(ctx: Context) -> int:
    """停止并移除容器（保留 ``deploy/data``）。"""
    assert_env_file(ctx)
    assert_docker_running(ctx)
    if invoke_compose(ctx, ["down", *ctx.rest]) != 0:
        _lib.die("docker compose down 失败")
    _lib.log("ATRI-X 容器已移除（数据保留在 deploy/data）")
    return 0


def do_restart(ctx: Context) -> int:
    """重启容器（不重新注入 ``.env``）。"""
    assert_env_file(ctx)
    wait_docker_running(ctx, 120)
    if invoke_compose(ctx, ["restart", *ctx.rest]) != 0:
        _lib.die("docker compose restart 失败")
    if not wait_ready(ctx, 60):
        _lib.warn("容器已重启，但 /ready 在 60 秒内未就绪。")
        show_logs_hint(ctx)
        return 1
    _lib.log("ATRI-X 已重启并就绪")
    return show_status(ctx)


def do_health(ctx: Context) -> int:
    """查看 ``/live``、``/ready``、``/healthz``。"""
    assert_docker_running(ctx)
    live_ok, live = get_health(ctx, "/live")
    _, ready = get_health(ctx, "/ready")
    _, healthz = get_health(ctx, "/healthz")
    if ctx.json:
        sys.stdout.write(_lib.pretty_json({"live": live, "ready": ready, "healthz": healthz}) + "\n")
    else:
        sys.stdout.write(f"/live    : {format_health(live)}\n")
        sys.stdout.write(f"/ready   : {format_health(ready)}\n")
        sys.stdout.write(f"/healthz : {format_health(healthz)}\n")
    if not live_ok or not health_ready(ready):
        return 1
    return 0


def do_doctor(ctx: Context) -> int:
    """在容器内跑 ``python -m atri_x doctor``。"""
    assert_env_file(ctx)
    assert_docker_running(ctx)
    args = ["exec", "-T", SERVICE, "python", "-m", "atri_x", "doctor"]
    if ctx.json:
        args.append("--json")
    args += ctx.rest
    return 0 if invoke_compose(ctx, args) == 0 else 1


def do_logs(ctx: Context) -> int:
    """看容器日志（``--follow`` / ``--tail N``）。"""
    assert_env_file(ctx)
    assert_docker_running(ctx)
    args = ["logs", "--tail", str(ctx.tail)]
    if ctx.follow:
        args.append("-f")
    args += [SERVICE, *ctx.rest]
    return 0 if invoke_compose(ctx, args) == 0 else 1


def do_shell(ctx: Context) -> int:
    """进入容器 shell。

    上游进的是 ``bash``。Python 版镜像基于 ``python:*-slim``（Debian），
    里面有 ``bash``，因此行为保持一致；没有 bash 时退回 ``sh``。
    """
    assert_env_file(ctx)
    assert_docker_running(ctx)
    shell = ctx.rest[0] if ctx.rest else "bash"
    return 0 if invoke_compose(ctx, ["exec", SERVICE, shell]) == 0 else 1


def do_exec(ctx: Context) -> int:
    """在容器内执行一条命令。

    先校验有没有命令再碰 Docker：没有参数是**用法错误**，报出来的应该是用法，
    而不是“Docker 没起来”（上游 ``exec`` 分支同样是先判参数个数）。
    """
    if not ctx.rest:
        _lib.die("用法：.\\deploy\\windows\\atri-x.cmd exec <命令...>")
    assert_env_file(ctx)
    assert_docker_running(ctx)
    return 0 if invoke_compose(ctx, ["exec", "-T", SERVICE, *ctx.rest]) == 0 else 1


def do_deploy_script(ctx: Context, script_name: str) -> int:
    """直接调 ``deploy/*.py``（替代上游的 ``Invoke-BashScript``）。

    上游这里要先把 Windows 路径转成 ``/m/...`` 再交给 Git Bash；Python 版
    用同一个解释器执行同目录的脚本，路径原样传递，不再需要任何转换。

    参数个数先校验：``restore-verify`` 缺备份包是用法错误，不该被 Docker
    前置检查掩盖掉。
    """
    if not ctx.rest:
        _lib.die(f"用法：.\\deploy\\windows\\atri-x.cmd {ctx.action} <参数...>")
    assert_env_file(ctx)
    assert_docker_running(ctx)
    script = ctx.deploy_dir / script_name
    if not script.is_file():
        _lib.die(f"未找到 {script}")
    _lib.log(f"调用 {script_name}")
    code = _lib.run_inherit([sys.executable, str(script), *ctx.rest], cwd=ctx.deploy_dir)
    if code != 0:
        _lib.die(f"{script_name} 执行失败（exit={code}）")
    return 0


def do_version(ctx: Context) -> int:
    """看源码版本与容器内版本。"""
    sys.stdout.write(f"源码版本（deploy/VERSION）: {ctx.version}\n")
    container = container_info(ctx)
    if container is None:
        sys.stdout.write("容器内版本: 容器未运行\n")
        return 0
    result = _lib.run_capture(
        ["docker", "exec", SERVICE, "python", "-m", "atri_x", "--version"], timeout=60.0
    )
    text = (result.stdout or result.stderr or "").strip()
    sys.stdout.write(f"容器内版本: {text or 'unknown'}\n")
    sys.stdout.write(f"容器镜像  : {container['image']}\n")
    return 0


def do_compose_passthrough(ctx: Context) -> int:
    """把参数原样透传给 ``docker compose``。"""
    if not ctx.rest:
        _lib.die("用法：.\\deploy\\windows\\atri-x.cmd compose <参数...>")
    return 0 if invoke_compose(ctx, ctx.rest) == 0 else 1


def do_help(ctx: Context) -> int:
    """打印帮助（内容与上游 ``Show-Help`` 对齐，命令名改成 Python 版）。"""
    sys.stdout.write(
        f"""ATRI-X Windows 本地容器部署 / 后台控制（Python 版）

用法：
  python deploy\\windows\\atri_x.py <动作> [参数...]

部署生命周期：
  init                 创建 deploy/data 和 .env（不覆盖已有 .env）
  build                构建当前源码版本的镜像
  start / up           后台启动容器（默认 --build；可加 --no-build）
  recreate             强制重建容器并重新注入 .env
  stop                 停止容器（保留容器和数据）
  down                 停止并移除容器（保留 deploy/data）
  restart              重启容器，不重新注入 .env
  status / ps          查看容器状态 + /live /ready /healthz；--json 输出 JSON
  version              查看源码版本和容器内版本

运行状态 / 诊断（复用项目内置接口与 python -m atri_x doctor）：
  health               查看 /live /ready /healthz
  tasks                查看 /tasks：活跃任务、阶段、排队积压
  metrics              查看 /metrics：任务/资源/模型/限速/生图队列
  diagnostics          查看 /diagnostics：聚合诊断
  doctor [选项]        容器内运行 python -m atri_x doctor；可追加 --e2e、--no-model、--require-platform、--json
  logs                 查看容器日志；支持 --follow、--tail N

运维 / 数据安全（deploy/*.py，纯标准库，不再需要 Git Bash）：
  backup [目录]        调用 deploy/backup.py 做一致性备份
  restore-verify <包>  调用 deploy/restore_verify.py 做隔离恢复验证
  upgrade              调用 deploy/upgrade.py
  rollback             调用 deploy/rollback.py

后台与高级用法：
  install-service      注册 Windows 计划任务：登录后自动 start --no-build
  uninstall-service    移除计划任务
  shell [shell]        进入容器 shell（默认 bash）
  exec <命令...>       在容器内执行命令（docker compose exec -T）
  compose <参数...>    直接透传给 docker compose
  help                 显示本帮助

说明：
  - 使用 Docker Desktop + Linux containers；不要切换到 Windows containers。
  - 状态接口默认 http://127.0.0.1:32171；可用 ATRIX_HEALTH_URL 覆盖。
  - /tasks /metrics /diagnostics 在设置 ATRIX_OPS_TOKEN 后会自动带 token。
  - 本地源码版本以 deploy/VERSION 为准；脚本会设置 ATRIX_VERSION/ATRIX_IMAGE 后再调用 compose。
  - 与上游 atri-x.ps1 的差异见 deploy/windows/README.md。
"""
    )
    return 0


DISPATCH = {
    "help": do_help,
    "init": do_init,
    "build": do_build,
    "start": do_start,
    "recreate": do_recreate,
    "stop": do_stop,
    "down": do_down,
    "restart": do_restart,
    "status": show_status,
    "health": do_health,
    "doctor": do_doctor,
    "logs": do_logs,
    "shell": do_shell,
    "exec": do_exec,
    "version": do_version,
    "compose": do_compose_passthrough,
    "install-service": install_service,
    "uninstall-service": uninstall_service,
}


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（开关同时接受 ``--kebab`` 与 ``--snake`` 写法）。"""
    parser = argparse.ArgumentParser(
        prog="atri_x.py",
        description="ATRI-X Windows 本地容器部署 / 后台控制（Python 版）",
        add_help=False,
    )
    parser.add_argument("action", nargs="?", default="status", help=f"动作，可用：{', '.join(ACTIONS)}")
    parser.add_argument("rest", nargs="*", help="透传给动作的剩余参数")
    parser.add_argument("-f", "--follow", action="store_true", help="logs 动作跟随输出")
    parser.add_argument("-t", "--tail", type=int, default=200, help="logs 动作取多少行，默认 200")
    parser.add_argument("--no-build", "--no_build", action="store_true", help="start 动作跳过构建")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("-h", "--help", action="store_true", help="显示帮助")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：解析动作并分发；返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    if args.help:
        args.action = "help"
    ctx = Context(args)
    if ctx.action not in ACTIONS:
        _lib.warn(f"未知动作：{ctx.action}")
        sys.stderr.write(f"可用动作：{', '.join(ACTIONS)}\n")
        return 2
    if ctx.action in DEPLOY_SCRIPTS:
        # 参数个数与 Docker 前置检查都在 do_deploy_script 里（先判用法再碰 Docker）。
        return do_deploy_script(ctx, DEPLOY_SCRIPTS[ctx.action])
    if ctx.action in {"tasks", "metrics", "diagnostics"}:
        return invoke_ops_endpoint(ctx, f"/{ctx.action}")
    handler = DISPATCH.get(ctx.action)
    if handler is None:  # pragma: no cover - ACTIONS 与 DISPATCH 已对齐
        _lib.die(f"动作 {ctx.action} 尚未实现")
    return handler(ctx)


if __name__ == "__main__":
    raise SystemExit(main())
