"""镜像冒烟测试：一条命令，退出码即结论。

对应上游：无（上游只有在真机上手工执行的 ``deploy/tests/*.sh``；容器镜像的
冒烟与稳定性测试是 Python 版新增的）。验收依据是
:file:`deploy/README.md` 第 12 节「部署验收清单」里**能在无人值守下自动判定**的那几条：
容器能起来、健康探测三层至少一层通过、非 root、无第三方依赖、
``--help`` / ``--health-probe`` 退出码为 0、数据卷目录结构正确、没有反复重启。

用法::

    python deploy/smoke.py                     # 构建 + 起容器 + 全部断言 + 自动清理
    python deploy/smoke.py --image atri-x:0.9.0   # 跳过构建，测已有镜像
    python deploy/smoke.py --timeout 300 --keep   # 延长等待；保留容器/镜像/卷以便排查
    python deploy/smoke.py --json out/smoke.json  # 指定报告路径

逐条断言 ↔ 验收项
-----------------
======================  ==========================================================
步骤名                   对应验收项
======================  ==========================================================
``docker_available``    前置条件：Docker 守护进程可用（不可用则一切无从谈起）
``image_ready``         第 2 节构建命令（``-f deploy/Dockerfile`` + 仓库根上下文）
``seed_config``         第 1 节「首次启动生成配置」的等价物（fixtures 最小配置）
``container_start``     第 1 节 ``docker compose up -d`` 的等价物（``--rm -d`` + ``/data`` 卷）
``healthy``             第 12 节第 1 条（容器 healthy）+ 第 3.1 节三层探测顺序
``user_not_root``       第 2 节「非 root 运行（10001:10001）」
``no_third_party_deps`` 第 2 节「不装任何第三方依赖」（Pillow 允许缺失）
``import_atri_x``       第 2 节「代码直接 COPY 进镜像」
``help_exit_zero``      入口可用（``python -m atri_x --help``）
``health_probe_exit_zero`` 第 3.1 节第 1 层（应用内置 ``--health-probe``）
``data_volume_layout``  第 1 节配置文件落位 + 第 5 节「配置与数据同卷」
``container_running``   第 12 节隐含项：没有因为启动错误退出 / 反复重启
``cleanup``             默认自动清理（``--keep`` 保留）
======================  ==========================================================

Docker 不可用时的行为
---------------------
**不会崩**，也不会假装通过：第一步 ``docker_available`` 记 FAIL，其余步骤记
``skipped:``，退出码 1，报告照常写盘。本机 Docker Desktop 未运行时看到的就是这种结果
（原因与恢复步骤见 :file:`deploy/TESTING.md`）。

报告
----
固定键名，CI 可直接读::

    {"ok": bool,
     "steps": [{"name","ok","detail","seconds"}, ...],
     "image": str, "container": str, "docker_version": str}

``detail`` 以 ``skipped:`` 开头 = 该步未执行（不计入失败）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402
import _smokelib as _sl  # noqa: E402
from _docker import DockerCLI  # noqa: E402

#: 默认构建产物的 tag 前缀（带版本号，便于排查）。
DEFAULT_TAG_PREFIX = "atri-x"
DEFAULT_TIMEOUT = 180.0
DEFAULT_BUILD_TIMEOUT = 3600.0
#: 构建失败时打印的日志行数（验收要求：最后 40 行）——与 _smokelib 同一个常量。
BUILD_TAIL_LINES = _sl.BUILD_TAIL_LINES
DEFAULT_FIXTURE_DIR = _DEPLOY / "fixtures" / "smoke-config"
DEFAULT_REPORT_DIR = _DEPLOY / "reports"
#: 写最小配置用的一次性容器名。与稳定性测试不同名，两者可以同时跑。
SEED_CONTAINER = "atri-x-smoke-seed"

#: 步骤顺序（用于把「前置失败后未执行的步骤」统一记成 skipped）。
PIPELINE = (
    "docker_available",
    "image_ready",
    "seed_config",
    "container_start",
    "healthy",
    "user_not_root",
    "no_third_party_deps",
    "import_atri_x",
    "help_exit_zero",
    "health_probe_exit_zero",
    "data_volume_layout",
    "container_running",
)


class SmokeTest:
    """一次镜像冒烟测试。``docker`` 可注入（测试用假实现驱动全部分支）。"""

    def __init__(
        self,
        *,
        image: str = "",
        tag: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        keep: bool = False,
        json_path: str = "",
        write_json: bool = True,
        fixture_dir: str = "",
        volume: str = "atri_x_smoke_data",
        container: str = _sl.CONTAINER_NAME_SMOKE,
        docker: DockerCLI | None = None,
        dockerfile: str = "deploy/Dockerfile",
        context: str = "",
        health_interval: float = 2.0,
        sleep=time.sleep,
    ) -> None:
        self.docker = docker if docker is not None else DockerCLI()
        self.image = image
        self.tag = tag
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.keep = keep
        self.json_path = json_path
        self.write_json = write_json
        self.fixture_dir = Path(fixture_dir) if fixture_dir else DEFAULT_FIXTURE_DIR
        self.volume = volume
        self.container = container
        self.dockerfile = dockerfile
        self.context = context or str(_REPO)
        self.health_interval = health_interval
        self.sleep = sleep

        self.steps = _sl.StepLog()
        self.report: dict[str, object] = {}
        self.docker_version = ""
        self.built = False
        self.started = False

    # ------------------------------------------------------------------
    # 流程
    # ------------------------------------------------------------------
    def run(self) -> int:
        """跑完整流程并返回进程退出码（0 = 全绿）。

        清理放在 ``finally`` 里：无论中途在哪一步失败，容器 / 镜像 / 卷都不会留在
        机器上（``--keep`` 除外），这也是它能被 CI 反复调用的前提。
        """
        try:
            self._pipeline()
        finally:
            self._cleanup()
        return self._finish()

    def _pipeline(self) -> None:
        """按顺序执行前置步骤与断言；前置失败则把后续步骤记成 skipped 并返回。"""
        if not self._docker_available():
            self._skip_rest("Docker 不可用")
            return
        if not self._image_ready():
            self._skip_rest("镜像不可用")
            return
        if not self._seed_config():
            self._skip_rest("最小配置未能写入数据卷")
            return
        if not self._container_start():
            self._skip_rest("容器未能启动")
            return

        self._healthy()
        self._assert_user_not_root()
        self._assert_no_third_party()
        self._assert_import()
        self._assert_help()
        self._assert_health_probe()
        self._assert_layout()
        self._assert_running()

    def _skip_rest(self, reason: str) -> None:
        """把尚未记录的步骤标成 skipped（保持步骤集合稳定，便于 CI 对比）。"""
        done = {step.name for step in self.steps.steps}
        for name in PIPELINE:
            if name not in done:
                self.steps.skipped(name, reason)

    def _finish(self) -> int:
        """写报告、打印摘要、返回退出码。"""
        self.report = {
            "ok": self.steps.ok,
            "steps": self.steps.to_list(),
            "image": self._image_ref(),
            "container": self.container,
            "docker_version": self.docker_version,
        }
        written = ""
        if self.write_json and self.json_path:
            _sl.write_report(self.json_path, self.report)
            written = str(self.json_path)
        print("")
        _lib.log(f"冒烟测试摘要（镜像 {self.report['image']}，容器 {self.container}）")
        _sl.print_steps(self.steps.steps)
        failed = self.steps.failed()
        if failed:
            _lib.warn(f"未通过 {len(failed)} 项：" + ", ".join(step.name for step in failed))
        conclusion = "通过" if self.steps.ok else "失败"
        _lib.log(f"结论：{conclusion}" + (f"；报告：{written}" if written else ""))
        return 0 if self.steps.ok else 1

    # ------------------------------------------------------------------
    # 前置（第 1~4 步的实现放在 _smokelib，稳定性测试复用同一套）
    # ------------------------------------------------------------------
    def _docker_available(self) -> bool:
        ok, version = _sl.check_docker(self.docker, self.steps)
        self.docker_version = version
        return ok

    def _image_ready(self) -> bool:
        prep = _sl.prepare_image(
            self.docker,
            self.steps,
            image=self.image,
            tag=self.tag,
            version=_lib.read_text(_DEPLOY / "VERSION").strip() or "dev",
            dockerfile=self.dockerfile,
            context=self.context,
            build_timeout=self.build_timeout,
            tag_prefix=DEFAULT_TAG_PREFIX,
            tag_suffix="-smoke",
        )
        self.tag = prep.tag or self.tag
        self.built = prep.built
        return prep.ok

    def _seed_config(self) -> bool:
        return _sl.seed_volume(
            self.docker,
            self.steps,
            image=self._image_ref(),
            volume=self.volume,
            fixture_dir=self.fixture_dir,
            container_name=SEED_CONTAINER,
        )

    def _container_start(self) -> bool:
        spec = _sl.ContainerSpec(
            name=self.container,
            image=self._image_ref(),
            volume=self.volume,
            env={"ATRIX_OPS_TOKEN": "smoke-test-token"},
            auto_remove=not self.keep,
        )
        ok = _sl.start_container(self.docker, self.steps, spec)
        self.started = ok
        return ok

    # ------------------------------------------------------------------
    # 断言
    # ------------------------------------------------------------------
    def _healthy(self) -> None:
        self.steps.start("healthy")
        outcome = _sl.wait_healthy(
            self.docker,
            self.container,
            timeout=self.timeout,
            interval=self.health_interval,
            sleep=self.sleep,
        )
        step = _sl.apply_health_outcome(
            self.steps, "healthy", outcome, container=self.container, keep=self.keep
        )
        if not step.ok and step.detail:
            _lib.warn("healthy 判定失败：" + step.detail.splitlines()[0])

    def _assert_user_not_root(self) -> None:
        self.steps.start("user_not_root")
        value = self.docker.inspect_text(self.container, "{{.Config.User}}")
        user = _sl.parse_container_user(value)
        ok = user not in ("", "0", "root")
        detail = f"Config.User={value!r}（解析出的用户 {user or '空=root'}）"
        if not ok:
            detail += "；容器以 root 运行，违反 deploy/README.md 第 2 节的非 root 要求"
        self.steps.finish("user_not_root", ok, detail)

    def _assert_no_third_party(self) -> None:
        self.steps.start("no_third_party_deps")
        done = self.docker.exec(
            self.container, ["python", "-m", "pip", "list", "--format=freeze"], timeout=120.0
        )
        if done.returncode != 0:
            self.steps.finish(
                "no_third_party_deps",
                False,
                f"pip list 失败（退出码 {done.returncode}）：{(done.stderr or '').strip()}",
            )
            return
        names = _sl.parse_pip_freeze(done.stdout or "")
        extras = _sl.third_party_packages(names)
        tolerated = sorted(_sl.tolerated_packages(names))
        detail = f"包清单：{', '.join(names) if names else '（空）'}"
        if tolerated:
            detail += f"；容忍项（一方代码/可选后端）：{', '.join(tolerated)}"
        ok = not extras
        if not ok:
            detail += f"；出现第三方依赖：{', '.join(extras)}"
        self.steps.finish("no_third_party_deps", ok, detail)

    def _assert_import(self) -> None:
        self.steps.start("import_atri_x")
        done = self.docker.exec(self.container, list(_sl.IMPORT_PROBE), timeout=60.0)
        path = (done.stdout or "").strip().splitlines()[-1].strip() if done.stdout else ""
        ok = done.returncode == 0 and path.endswith("atri_x/__init__.py")
        detail = f"atri_x.__file__={path!r}" if ok else (
            f"import atri_x 失败（退出码 {done.returncode}）：{(done.stderr or '').strip()}"
        )
        self.steps.finish("import_atri_x", ok, detail)

    def _assert_help(self) -> None:
        self.steps.start("help_exit_zero")
        done = self.docker.exec(self.container, list(_sl.HELP_PROBE), timeout=60.0)
        ok = done.returncode == 0
        detail = (
            "python -m atri_x --help 退出码 0"
            if ok
            else f"--help 退出码 {done.returncode}：{(done.stderr or '').strip()}"
        )
        self.steps.finish("help_exit_zero", ok, detail)

    def _assert_health_probe(self) -> None:
        self.steps.start("health_probe_exit_zero")
        done = self.docker.exec(self.container, list(_sl.HEALTH_PROBE), timeout=60.0)
        ok = done.returncode == 0
        detail = (
            "python -m atri_x --health-probe 退出码 0"
            if ok
            else (
                f"--health-probe 退出码 {done.returncode}（三层探测里的首选层）；"
                f"{(done.stderr or '').strip()}"
            )
        )
        self.steps.finish("health_probe_exit_zero", ok, detail)

    def _assert_layout(self) -> None:
        self.steps.start("data_volume_layout")
        paths = [*_sl.EXPECTED_CONFIG_PATHS, _sl.CONTAINER_DATA_DIR]
        parsed = _sl.missing_paths(self.docker, self.container, paths)
        if parsed is None:
            self.steps.finish(
                "data_volume_layout", False, "容器内目录结构探针执行失败（docker exec 非 0）"
            )
            return
        missing = [str(item) for item in parsed.get("missing", [])]
        entries = [str(item) for item in parsed.get("data_entries", [])]
        has_db = any(entry.endswith(".db") for entry in entries)
        ok = not missing and has_db
        detail = (
            f"{_sl.CONTAINER_DATA_DIR} 内容：{', '.join(entries) if entries else '（空）'}"
        )
        if missing:
            detail += f"；缺失：{', '.join(missing)}"
        if not has_db:
            detail += "；数据目录里没有 .db（SQLite 未落盘）"
        self.steps.finish("data_volume_layout", ok, detail)

    def _assert_running(self) -> None:
        self.steps.start("container_running")
        state = _sl.container_state(self.docker, self.container)
        if state is None:
            self.steps.finish(
                "container_running",
                False,
                "容器已不存在（--rm 删除或启动即崩）；用 --keep 重跑可保留现场",
            )
            return
        restarts = state.get("RestartCount", 0)
        running = bool(state.get("Running", False))
        ok = running and int(restarts or 0) == 0
        detail = (
            f"State.Running={running} RestartCount={restarts} Status={state.get('Status')!r}"
        )
        if not ok:
            detail += "；容器未在运行或发生过重启，说明启动期出错（见 healthy 步骤的日志）"
        self.steps.finish("container_running", ok, detail)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        if self.keep:
            self.steps.skipped(
                "cleanup",
                f"--keep：保留容器 {self.container}、卷 {self.volume}"
                + (f"、镜像 {self.tag}" if self.built else ""),
            )
            return
        if not self.docker_version:
            self.steps.skipped("cleanup", "Docker 不可用，没有产生任何需要清理的对象")
            return
        self.steps.start("cleanup")
        removed: list[str] = []
        if self.started:
            self.docker.remove_container(self.container)
            removed.append(f"容器 {self.container}")
        if self.built and self.tag:
            self.docker.remove_image(self.tag)
            removed.append(f"镜像 {self.tag}")
        self.docker.remove_volume(self.volume)
        removed.append(f"卷 {self.volume}")
        self.steps.finish("cleanup", True, "已清理：" + "、".join(removed))

    # ------------------------------------------------------------------
    def _image_ref(self) -> str:
        """本次实际使用的镜像引用（``--image`` 优先，否则用构建出来的 tag）。"""
        return self.image or self.tag or ""


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="smoke.py",
        description="ATRI-X 镜像冒烟测试（构建 → 起容器 → 断言 → 报告 → 清理）",
    )
    parser.add_argument("--image", default="", help="跳过构建，直接测这个已有镜像 tag")
    parser.add_argument("--tag", default="", help="构建出的镜像 tag（默认 atri-x:<VERSION>-smoke）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="等 healthy 的秒数，默认 180")
    parser.add_argument("--build-timeout", type=float, default=DEFAULT_BUILD_TIMEOUT, help="docker build 超时秒数")
    parser.add_argument("--keep", action="store_true", help="保留容器 / 镜像 / 卷（同时关掉 --rm）")
    parser.add_argument("--json", dest="json_path", default="", help="JSON 报告路径")
    parser.add_argument("--no-json", action="store_true", help="不写 JSON 报告，只打印摘要")
    parser.add_argument("--volume", default="atri_x_smoke_data", help="数据卷名")
    parser.add_argument("--container", default=_sl.CONTAINER_NAME_SMOKE, help="容器名")
    parser.add_argument("--fixture-dir", default="", help="最小配置目录，默认 deploy/fixtures/smoke-config")
    parser.add_argument("--docker", dest="docker_bin", default="docker", help="docker 可执行文件名")
    return parser


def default_report_path() -> str:
    """默认报告路径：``deploy/reports/smoke-<时间戳>.json``。"""
    return str(DEFAULT_REPORT_DIR / f"smoke-{_lib.now_stamp()}.json")


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码（0 通过 / 1 失败 / 2 参数错误）。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    json_path = args.json_path or ("" if args.no_json else default_report_path())
    test = SmokeTest(
        image=args.image,
        tag=args.tag,
        timeout=args.timeout,
        build_timeout=args.build_timeout,
        keep=args.keep,
        json_path=json_path,
        write_json=not args.no_json,
        fixture_dir=args.fixture_dir,
        volume=args.volume,
        container=args.container,
        docker=DockerCLI(args.docker_bin),
    )
    return test.run()


if __name__ == "__main__":
    raise SystemExit(main())
