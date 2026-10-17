"""冒烟 / 稳定性测试的共享内核：步骤记账、容器规格、健康探测、指标解析、报告。

对应上游：无（上游部署套件只有 7 个 ``deploy/tests/*.sh``，没有容器冒烟/长跑测试）。

拆出来的理由
------------
``smoke.py``（镜像冒烟）与 ``stability.py``（长跑稳定性）要用同一套东西：

* 容器怎么起（:class:`ContainerSpec`，与 ``docker-compose.yml`` 的挂卷/端口方式一致）；
* 最小配置怎么塞进数据卷（:func:`seed_config`）；
* 「healthy」怎么判（:func:`probe_health_layers`，三层顺序与 ``Dockerfile`` 的
  ``HEALTHCHECK`` 逐字一致，见 :data:`HEALTH_LAYERS`）；
* 指标怎么解析（``docker stats`` / ``docker inspect`` 的输出）；
* 报告怎么写（键名固定，CI 直接读）。

这个模块**不碰命令行参数**，也不 ``print`` 结论（只有 :func:`print_steps` 这种
纯展示函数）；两个脚本各自负责 argparse 与退出码，于是两者都能被 unittest
直接驱动——这是「不依赖 Docker 也能验证」的前提。

``docker stats`` / ``pip list`` 输出的解析与判定边界（恰好 1%、恰好 1.5 倍这类）
拆到了 :mod:`_metrics`：那一层是纯函数，本模块仍然 re-export 它们，
调用方只写 ``_smokelib.xxx`` 一种形式即可（与 :mod:`_lib` 对四个子模块的做法一致）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for _candidate in (str(_REPO), str(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402
from _docker import DockerCLI  # noqa: E402
from _metrics import (  # noqa: E402
    BASE_PACKAGES,
    STATS_FORMAT,
    TOLERATED_PACKAGES,
    ascii_bar,
    failures_at_or_below,
    format_bytes,
    growth_ratio_ok,
    parse_container_user,
    parse_cpu_percent,
    parse_mem_usage,
    parse_pip_freeze,
    parse_stats_line,
    third_party_packages,
    thirds_means,
    tolerated_packages,
)

__all__ = [
    "BASE_PACKAGES",
    "BUILD_TAIL_LINES",
    "CONTAINER_CONFIG_DIR",
    "CONTAINER_DATA_DIR",
    "CONTAINER_NAME_STABILITY",
    "CONTAINER_NAME_SMOKE",
    "DEFAULT_TAG_PREFIX",
    "EXPECTED_CONFIG_PATHS",
    "HEALTH_LAYERS",
    "HEALTH_PORT",
    "HELP_PROBE",
    "IMPORT_PROBE",
    "ImagePrep",
    "LAYOUT_PROBE",
    "SEED_CONTAINER_NAME",
    "SEED_SCRIPT",
    "STATS_FORMAT",
    "TARGET_GID",
    "TARGET_UID",
    "TOLERATED_PACKAGES",
    "ContainerSpec",
    "Step",
    "StepLog",
    "apply_health_outcome",
    "ascii_bar",
    "check_docker",
    "container_state",
    "failures_at_or_below",
    "format_bytes",
    "growth_ratio_ok",
    "missing_paths",
    "parse_container_user",
    "parse_cpu_percent",
    "parse_mem_usage",
    "parse_pip_freeze",
    "parse_stats_line",
    "prepare_image",
    "print_steps",
    "probe_health_layers",
    "seed_config",
    "seed_volume",
    "start_container",
    "third_party_packages",
    "thirds_means",
    "tolerated_packages",
    "wait_healthy",
    "write_report",
]

# ---------------------------------------------------------------------------
# 常量（改动前先确认与 Dockerfile / compose 同步）
# ---------------------------------------------------------------------------
#: 独立健康接口端口（``ATRIX_HEALTH_ADDR=0.0.0.0:32171``）。
HEALTH_PORT = 32171
#: 容器内身份（与 ``Dockerfile`` 的 ``useradd -u 10001`` / compose ``user:`` 一致）。
TARGET_UID = 10001
TARGET_GID = 10001
#: 容器内配置目录（镜像 ``XDG_CONFIG_HOME=/data/config`` + 应用目录名 ``atri-x``）。
CONTAINER_CONFIG_DIR = "/data/config/atri-x"
#: 容器内数据目录（fixtures 把两个 SQLite 显式指到这里，便于断言目录结构）。
CONTAINER_DATA_DIR = "/data/data"
CONTAINER_NAME_SMOKE = "atri-x-smoke"
CONTAINER_NAME_STABILITY = "atri-x-stability"

#: 三层健康探测，**顺序与命令与 ``deploy/Dockerfile`` 的 ``HEALTHCHECK`` 逐字一致**
#: （``tests/test_deploy_harness.py`` 有断言把这两处钉在一起，防止漂移）：
#:   1. ``python -m atri_x --health-probe``（应用内置开关，首选）；
#:   2. 退回 HTTP ``GET /live``（只看进程是否还能响应）；
#:   3. 再退回 TCP 连通 32171。
HEALTH_LAYERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("health_probe", ("python", "-m", "atri_x", "--health-probe")),
    (
        "http_live",
        (
            "python",
            "-c",
            "import urllib.request,sys;sys.exit(0 if "
            "urllib.request.urlopen('http://127.0.0.1:32171/live',timeout=3).status==200 else 1)",
        ),
    ),
    (
        "tcp_32171",
        (
            "python",
            "-c",
            "import socket,sys;s=socket.socket();s.settimeout(3);sys.exit(0 if "
            "s.connect_ex(('127.0.0.1',32171))==0 else 1)",
        ),
    ),
)

#: 「容器内能不能 import atri_x」探针。
IMPORT_PROBE: tuple[str, ...] = ("python", "-c", "import atri_x; print(atri_x.__file__)")
#: ``--help`` 必须退出 0（入口参数解析没被镜像布局破坏）。
HELP_PROBE: tuple[str, ...] = ("python", "-m", "atri_x", "--help")
#: ``--health-probe`` 必须退出 0（三层里的第一层单独再验一次）。
HEALTH_PROBE: tuple[str, ...] = ("python", "-m", "atri_x", "--health-probe")
#: 数据卷目录结构探针：打印 ``{"missing":[...],"data_dir":...,"data_entries":[...]}``。
LAYOUT_PROBE: tuple[str, ...] = (
    "python",
    "-c",
    "import json,os,sys;"
    "paths=json.loads(sys.argv[1]);"
    "data=sys.argv[2];"
    "files=sorted(os.listdir(data)) if os.path.isdir(data) else [];"
    "print(json.dumps({'missing':[p for p in paths if not os.path.exists(p)],"
    "'data_dir':data,'data_entries':files}))",
)
#: 首次启动必然出现的配置（由 fixtures 塞入，应用读它）。
EXPECTED_CONFIG_PATHS: tuple[str, ...] = (
    f"{CONTAINER_CONFIG_DIR}/app.toml",
    f"{CONTAINER_CONFIG_DIR}/services.toml",
    f"{CONTAINER_CONFIG_DIR}/state.toml",
)

#: 初始化数据卷的脚本：把 fixtures 拷进配置目录，再把属主交还给容器用户。
#: 用 ``--user 0:0`` 跑一次性容器，因为宿主机 bind mount 的属主不一定是 10001；
#: 但**必须 chown 回 10001**，否则应用（非 root）回写不了 ``state.toml``。
SEED_SCRIPT = (
    "set -e; mkdir -p " + CONTAINER_CONFIG_DIR + "; "
    "cp -a /seed/. " + CONTAINER_CONFIG_DIR + "/; "
    f"chown -R {TARGET_UID}:{TARGET_GID} /data"
)
#: 默认构建 tag 前缀（``atri-x:<VERSION>-smoke``）。
DEFAULT_TAG_PREFIX = "atri-x"
#: 构建失败时打印/记录的最后若干行（验收要求 40 行）。
BUILD_TAIL_LINES = 40
#: 往数据卷里写最小配置的一次性容器名。
SEED_CONTAINER_NAME = "atri-x-seed"


# ---------------------------------------------------------------------------
# 步骤记账（JSON 报告的 steps 数组）
# ---------------------------------------------------------------------------
@dataclass
class Step:
    """一条断言。对应 JSON 报告 ``steps[]`` 里的四个键。"""

    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """固定键名：``name`` / ``ok`` / ``detail`` / ``seconds``（CI 直接读）。"""
        return {
            "name": self.name,
            "ok": bool(self.ok),
            "detail": self.detail,
            "seconds": round(float(self.seconds), 3),
        }

    @property
    def skipped(self) -> bool:
        """``detail`` 以 ``skipped:`` 开头 = 未执行（约定，见 TESTING.md）。"""
        return self.detail.startswith("skipped:")


class StepLog:
    """按顺序收集步骤；``ok`` 为「所有步骤都通过」。"""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self._started: dict[str, float] = {}

    def start(self, name: str) -> None:
        """记录某一步的开始时刻（配合 :meth:`finish` 计耗时）。"""
        self._started[name] = time.monotonic()

    def record(self, name: str, ok: bool, detail: str = "", seconds: float = 0.0) -> Step:
        """直接记一条步骤。"""
        step = Step(name=name, ok=ok, detail=detail, seconds=seconds)
        self.steps.append(step)
        return step

    def finish(self, name: str, ok: bool, detail: str = "") -> Step:
        """按 :meth:`start` 记下的起点自动算耗时。"""
        started = self._started.pop(name, None)
        seconds = 0.0 if started is None else time.monotonic() - started
        return self.record(name, ok, detail, seconds)

    def skipped(self, name: str, detail: str) -> Step:
        """记一条「跳过」：``ok=True`` + ``detail`` 前缀 ``skipped:``。"""
        return self.record(name, True, f"skipped: {detail}", 0.0)

    @property
    def ok(self) -> bool:
        """是否全部通过（跳过不算失败）。"""
        return all(step.ok for step in self.steps)

    def failed(self) -> list[Step]:
        """未通过的步骤。"""
        return [step for step in self.steps if not step.ok]

    def to_list(self) -> list[dict[str, object]]:
        return [step.to_dict() for step in self.steps]


def print_steps(steps: Sequence[Step], *, stream=None) -> None:
    """打印人类可读摘要；失败步骤的完整 detail 缩进另起一段。"""
    target = stream if stream is not None else sys.stdout
    for step in steps:
        mark = " OK " if step.ok else "FAIL"
        head = step.detail.splitlines()[0] if step.detail else ""
        _lib.safe_write(
            target, f"  [{mark}] {step.name:<24} {step.seconds:7.2f}s  {head}"
        )
        extra = step.detail.splitlines()[1:]
        if not step.ok and extra:
            for line in extra:
                _lib.safe_write(target, f"         | {line}")


# ---------------------------------------------------------------------------
# 容器规格
# ---------------------------------------------------------------------------
@dataclass
class ImagePrep:
    """镜像准备结果（``--image`` 直接用，否则构建）。"""

    ok: bool
    #: 本次实际使用的镜像引用。
    ref: str
    #: 是否由本脚本构建（决定清理时要不要 ``docker rmi``）。
    built: bool
    tag: str
    version: str


@dataclass
class ContainerSpec:
    """一次 ``docker run`` 的规格；字段与 ``docker-compose.yml`` 的用法对齐。"""
    name: str
    image: str
    volume: str
    env: dict[str, str] = field(default_factory=dict)
    #: 把容器内健康端口发布到宿主机回环的**随机**端口（0 = 不发布）。
    #: 用 ``-p 127.0.0.1::32171`` 形式，避免与已有实例抢 32171（compose 用的是
    #: ``127.0.0.1:32171:32171``；冒烟测试必须能在生产实例同时在跑时执行）。
    publish_health_port: int = HEALTH_PORT
    #: ``--rm``：容器退出即自动删除（``--keep`` 时关掉，否则没法排查）。
    auto_remove: bool = True
    #: 覆盖镜像 ``CMD``；空表示用镜像自己的 ``CMD``（即 ``run``）。
    command: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()

    def argv(self) -> list[str]:
        """``docker run`` 之后的参数（不含开头的 ``run``）。"""
        args: list[str] = []
        if self.auto_remove:
            args.append("--rm")
        args += ["-d", "--name", self.name]
        # 与 compose 一致：单一数据卷挂 /data，**不给 /data 的子路径再挂卷**
        # （子路径会覆盖父挂载，见 deploy/README.md 第 5 节）。
        args += ["-v", f"{self.volume}:/data"]
        if self.publish_health_port:
            args += ["-p", f"127.0.0.1::{self.publish_health_port}"]
        for key, value in self.env.items():
            args += ["-e", f"{key}={value}"]
        args += list(self.extra_args)
        args.append(self.image)
        args += list(self.command)
        return args


def seed_config(
    docker: DockerCLI,
    *,
    image: str,
    volume: str,
    fixture_dir: str | os.PathLike[str],
    container_name: str,
) -> subprocess.CompletedProcess:
    """把最小配置塞进数据卷（一次性容器 + 只读 bind mount）。

    为什么要额外起一个容器，而不是 ``docker cp``：命名卷只有在被容器挂载时
    才会从镜像里预填充（``/data`` 的属主是 10001），先 ``docker cp`` 到没挂载的
    容器上会让属主/内容都不可预期。这里用同镜像的 ``/bin/sh`` 做一次
    ``cp -a`` 再 ``chown``，结果与「首次启动生成配置」的目录结构一致。
    """
    argv = [
        "--rm",
        "--name",
        container_name,
        "--user",
        "0:0",
        "--entrypoint",
        "/bin/sh",
        "-v",
        f"{volume}:/data",
        "-v",
        f"{_lib.host_path(fixture_dir)}:/seed:ro",
        image,
        "-c",
        SEED_SCRIPT,
    ]
    return docker.run(argv)


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------
def probe_health_layers(
    docker: DockerCLI, container: str, *, timeout: float = 30.0
) -> dict[str, object]:
    """按 :data:`HEALTH_LAYERS` 的顺序做**一轮**三层探测。

    返回 ``{"ok","layer","attempts","detail"}``；``attempts`` 是这一轮里
    每层 ``docker exec`` 的退出码，失败时用于解释「卡在哪一层」。
    """
    attempts: list[dict[str, object]] = []
    for layer, command in HEALTH_LAYERS:
        done = docker.exec(container, list(command), timeout=timeout)
        attempts.append({"layer": layer, "code": done.returncode})
        if done.returncode == 0:
            return {
                "ok": True,
                "layer": layer,
                "attempts": attempts,
                "detail": f"第 {len(attempts)} 层 {layer} 通过",
            }
    codes = ", ".join(f"{item['layer']}={item['code']}" for item in attempts)
    return {
        "ok": False,
        "layer": "",
        "attempts": attempts,
        "detail": f"三层探测全部失败（{codes}）",
    }


def container_state(docker: DockerCLI, container: str) -> dict[str, object] | None:
    """``docker inspect --format '{{json .State}}'`` 的解析结果；容器不存在返回 ``None``。"""
    state = docker.inspect_json(container, "{{json .State}}")
    return state if isinstance(state, dict) else None


def wait_healthy(
    docker: DockerCLI,
    container: str,
    *,
    timeout: float = 180.0,
    interval: float = 2.0,
    log_tail: int = 40,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict[str, object]:
    """轮询到健康或超时，返回 ``{"ok","detail","layer","attempts","logs","rounds"}``。

    每一轮都先看 ``State.Running``：容器已经退出时**立刻**失败并附上
    ``docker logs`` 尾巴——这正是「应用启动即崩」这类问题最需要的证据
    （``--rm`` 会把退出的容器删掉，所以这里抢在下一轮之前抓）。
    """
    deadline = clock() + max(timeout, 0.0)
    rounds = 0
    last: dict[str, object] = {"ok": False, "detail": "未开始探测", "attempts": []}
    while True:
        rounds += 1
        state = container_state(docker, container)
        if state is None:
            return {
                "ok": False,
                "layer": "",
                "attempts": last.get("attempts", []),
                "rounds": rounds,
                "logs": docker.logs(container, tail=log_tail),
                "detail": (
                    "容器已不存在：要么已退出（--rm 自动删除），要么名字冲突被顶掉；"
                    "用 --keep 重跑可保留容器与日志"
                ),
            }
        if not state.get("Running", False):
            return {
                "ok": False,
                "layer": "",
                "attempts": last.get("attempts", []),
                "rounds": rounds,
                "logs": docker.logs(container, tail=log_tail),
                "detail": (
                    f"容器已退出（Status={state.get('Status')!r}, "
                    f"ExitCode={state.get('ExitCode')!r}）"
                ),
            }
        last = probe_health_layers(docker, container)
        if last["ok"]:
            last.update({"rounds": rounds, "logs": ""})
            return last
        if clock() >= deadline:
            return {
                "ok": False,
                "layer": "",
                "attempts": last.get("attempts", []),
                "rounds": rounds,
                "logs": docker.logs(container, tail=log_tail),
                "detail": f"等待 {timeout:g}s 仍未 healthy；{last['detail']}",
            }
        sleep(interval)


def apply_health_outcome(
    step_log: StepLog,
    step_name: str,
    outcome: dict[str, object],
    *,
    container: str,
    keep: bool,
) -> Step:
    """把 :func:`wait_healthy` 的结果记成一步；失败时把容器日志尾巴带进 detail。"""
    detail = str(outcome.get("detail", ""))
    layer = str(outcome.get("layer", ""))
    if outcome.get("ok"):
        detail = f"{detail}（第 {outcome.get('rounds')} 轮）"
        if layer:
            detail += f"；命中层 {layer}"
    logs = str(outcome.get("logs") or "")
    if logs:
        detail += "\n---- docker logs (tail) ----\n" + logs.rstrip("\n")
    if not outcome.get("ok") and keep:
        detail += f"\n---- 容器已保留：{container}，可用 docker logs {container} 继续看 ----"
    # 用 finish 而不是 record：调用方在这之前已经 start(step_name)，耗时自动算。
    return step_log.finish(step_name, bool(outcome.get("ok")), detail)


def missing_paths(
    docker: DockerCLI, container: str, paths: Sequence[str], *, data_dir: str = CONTAINER_DATA_DIR
) -> dict[str, object] | None:
    """在容器内检查 ``paths`` 是否存在，并列出数据目录内容；探针失败返回 ``None``。"""
    done = docker.exec(
        container,
        [*LAYOUT_PROBE, json.dumps(list(paths)), data_dir],
        timeout=60.0,
    )
    if done.returncode != 0:
        return None
    try:
        parsed = json.loads(done.stdout or "")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# 生命周期（冒烟与稳定性测试共用的前置四步）
# ---------------------------------------------------------------------------
def check_docker(docker: DockerCLI, steps: StepLog) -> tuple[bool, str]:
    """第 1 步 ``docker_available``：返回 ``(是否可用, 服务端版本)``。

    刻意问「服务端版本」：``docker --version`` 在守护进程没起来时照样成功，
    用它判断可用性会得到假阳性（本机就是这种状态）。
    """
    steps.start("docker_available")
    version = docker.version()
    if version:
        steps.finish("docker_available", True, f"Docker 服务端 {version}")
        return True, version
    steps.finish(
        "docker_available",
        False,
        "Docker 守护进程不可用：" + docker.probe_error() + "\n"
        "（本机需先启动 Docker Desktop；恢复后的完整执行步骤见 deploy/TESTING.md）",
    )
    return False, ""


def prepare_image(
    docker: DockerCLI,
    steps: StepLog,
    *,
    image: str = "",
    tag: str = "",
    version: str = "dev",
    dockerfile: str = "deploy/Dockerfile",
    context: str = ".",
    build_timeout: float = 3600.0,
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    tag_suffix: str = "-smoke",
) -> ImagePrep:
    """第 2 步 ``image_ready``：``--image`` 就用现成的，否则按 README 第 2 节构建。

    构建失败时**打印并附上最后 :data:`BUILD_TAIL_LINES` 行输出**，这是排查
    「镜像根本建不出来」时最需要的证据。
    """
    steps.start("image_ready")
    if image:
        if docker.image_present(image):
            steps.finish("image_ready", True, f"使用已有镜像 {image}（未构建）")
            return ImagePrep(ok=True, ref=image, built=False, tag="", version=version)
        steps.finish("image_ready", False, f"--image 指定的镜像不存在：{image}")
        return ImagePrep(ok=False, ref=image, built=False, tag="", version=version)

    tag = tag or f"{tag_prefix}:{version}{tag_suffix}"
    _lib.log(f"构建镜像 {tag}（-f {dockerfile}，上下文 {context}）；构建输出只在失败时打印")
    done = docker.build(
        tag,
        dockerfile=dockerfile,
        context=context,
        build_args=("--build-arg", f"VERSION={version}"),
        timeout=build_timeout,
        cwd=context,
    )
    if done.returncode == 0:
        steps.finish("image_ready", True, f"构建成功：{tag}")
        return ImagePrep(ok=True, ref=tag, built=True, tag=tag, version=version)
    tail = _lib.tail_lines((done.stdout or "") + (done.stderr or ""), BUILD_TAIL_LINES).rstrip("\n")
    _lib.warn(f"docker build 失败（退出码 {done.returncode}），最后 {BUILD_TAIL_LINES} 行：")
    _lib.safe_write(sys.stdout, tail or "(构建没有任何输出)")
    steps.finish(
        "image_ready",
        False,
        f"docker build 失败（退出码 {done.returncode}）；最后 {BUILD_TAIL_LINES} 行：\n{tail}",
    )
    return ImagePrep(ok=False, ref=tag, built=False, tag=tag, version=version)


def seed_volume(
    docker: DockerCLI,
    steps: StepLog,
    *,
    image: str,
    volume: str,
    fixture_dir: str | os.PathLike[str],
    container_name: str = SEED_CONTAINER_NAME,
) -> bool:
    """第 3 步 ``seed_config``：建卷并把最小配置写进去。"""
    steps.start("seed_config")
    fixture = Path(fixture_dir)
    if not fixture.is_dir():
        steps.finish("seed_config", False, f"最小配置目录不存在：{fixture}")
        return False
    created = docker.create_volume(volume)
    if created.returncode != 0:
        steps.finish(
            "seed_config", False, f"docker volume create 失败：{(created.stderr or '').strip()}"
        )
        return False
    done = seed_config(
        docker,
        image=image,
        volume=volume,
        fixture_dir=fixture,
        container_name=container_name,
    )
    if done.returncode == 0:
        steps.finish(
            "seed_config", True, f"把 {fixture} 写入卷 {volume} 的 {CONTAINER_CONFIG_DIR}"
        )
        return True
    steps.finish(
        "seed_config",
        False,
        f"写入最小配置失败（退出码 {done.returncode}）：{(done.stderr or '').strip()}",
    )
    return False


def start_container(docker: DockerCLI, steps: StepLog, spec: ContainerSpec) -> bool:
    """第 4 步 ``container_start``：先清同名残留容器，再 ``docker run -d``。"""
    steps.start("container_start")
    # 同名残留容器会让 docker run 直接失败；这里只清这个名字，不碰别的容器。
    docker.remove_container(spec.name)
    done = docker.run(spec.argv())
    if done.returncode != 0:
        steps.finish(
            "container_start",
            False,
            f"docker run 失败（退出码 {done.returncode}）：{(done.stderr or '').strip()}",
        )
        return False
    steps.finish(
        "container_start", True, f"已启动 {spec.name}（docker run {' '.join(spec.argv())}）"
    )
    return True


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def write_report(path: str | os.PathLike[str] | None, payload: dict[str, object]) -> str:
    """把报告写成 UTF-8 + LF 的 JSON；``path`` 为空时只返回正文。"""
    text = _lib.pretty_json(payload)
    if path is None or str(path) == "":
        return text
    _lib.write_text(path, text + "\n")
    return text
