"""``tests/test_deploy_harness.py`` 的假 docker 与公共夹具。

约定与仓库其它测试辅助模块一致（``tests/agent_fakes.py``、``tests/_vision_fakes.py``、
``tests/_atrinis_support.py``），用 ``from tests._deploy_harness_fakes import ...`` 复用。

为什么要有「假 docker」
----------------------
本机 Docker Desktop 守护进程随时可能没在跑（``docker version`` 报
``npipe:////./pipe/dockerDesktopLinuxEngine`` 找不到），而「等 Docker 恢复了再验证」
等于没验证。``deploy/smoke.py`` / ``deploy/stability.py`` 把「调 docker CLI」
全部收敛到 :class:`deploy._docker.DockerCLI`，所以这里用 :class:`FakeDocker`
覆写它的 :meth:`DockerCLI._run`：**记录每次调用的 argv、按预置 stdout 返回**，
于是构建成功/失败、三层健康探测的每一种回落、非 root 断言、``pip list`` 出现第三方包、
``RestartCount != 0``、失败率与内存增长的边界值……都能在没有 Docker 的机器上跑到。

本模块**不含** ``Test*`` 类（unittest 不会收集它），只有假实现与夹具基类。

严格模式（2026 加固）
--------------------
``FakeDocker`` 不再对未识别的命令返回 0：``_route`` / ``_route_exec`` 的兜底分支
抛 ``AssertionError``（消息含全 argv）。否则被测脚本新增一条 docker 调用时，
假实现会把它当成功，测试全绿却什么也没验证。每次路由命中同时记账
（``hits`` / ``rule_hits()`` / ``hit_log``），用例可以断言命中了哪条规则几次。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "deploy"
TMP_ROOT = REPO_ROOT / "tmp_deploy_harness_test"

if str(DEPLOY_SRC) not in sys.path:
    sys.path.insert(0, str(DEPLOY_SRC))

import smoke as smoke_module  # noqa: E402
import stability as stability_module  # noqa: E402
from _docker import DockerCLI  # noqa: E402

#: ``pip list --format=freeze`` 在 python:*-slim 里的正常输出。
BASE_FREEZE = "pip==24.2\nsetuptools==75.1.0\nwheel==0.44.0\n"
#: 数据卷目录结构探针的正常输出。
LAYOUT_OK = json.dumps(
    {
        "missing": [],
        "data_dir": "/data/data",
        "data_entries": ["chat_history.db", "logs", "sessions.db"],
    }
)
#: 守护进程没起来时 docker 的原话（本机真实输出）。
NPIPE_ERROR = (
    "failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; "
    "check if the path is correct and if the daemon is running"
)


def completed(args, code=0, stdout="", stderr=""):
    """构造一个 :class:`subprocess.CompletedProcess`（与 ``_proc.run_capture`` 同形）。"""
    return subprocess.CompletedProcess(list(args), code, stdout, stderr)


class FakeClock:
    """每次调用自增的单调时钟（让等待循环在测试里按确定步长推进）。"""

    def __init__(self, step: float = 0.1) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class FakeDocker(DockerCLI):
    """假 docker：记录 argv，按配置返回预置输出。

    只覆写 :meth:`DockerCLI._run`——真实类里所有「拼参数 / 拆输出」的逻辑都照跑，
    假的只是 docker 本身，不是被测代码。

    **未识别的命令一律 ``AssertionError``**（``_route`` / ``_route_exec`` 的兜底
    分支不再返回 0）：假实现不认识的新子命令过去会被当成「成功」，于是被测脚本
    新增一条 docker 调用时测试照样全绿——那正是假实现最危险的地方。现在它会响亮
    失败并把全 argv 打出来，提示补路由。

    每次命中还会记账（``self.hits`` / :meth:`rule_hits` / ``self.hit_log``），
    用例可以断言「这次调用命中了哪条规则、几次」，而不是只看调用过没有。
    """

    DEFAULTS = {
        "version": "27.3.1",
        "build_code": 0,
        "build_stdout": "Successfully built deadbeef\n",
        "build_stderr": "",
        "images": ("atri-x:0.9.0",),
        "volume_create_code": 0,
        "seed_code": 0,
        "seed_stderr": "",
        "run_code": 0,
        "run_stdout": "c0ffee123456\n",
        "run_stderr": "",
        "user": "atri-x:atri-x",
        "state_exists": True,
        "state_running": True,
        "restart_count": 0,
        "probe_codes": (0,),
        "layer_http_code": 0,
        "layer_tcp_code": 0,
        "help_code": 0,
        "import_code": 0,
        "import_stdout": "/app/atri_x/__init__.py\n",
        "pip_code": 0,
        "pip_stdout": BASE_FREEZE,
        "layout_code": 0,
        "layout_stdout": LAYOUT_OK,
        "stats_stdout": "0.12%|42.3MiB / 1.5GiB\n",
        "stats_sequence": (),
        "logs_stdout": "2026-10-01T00:00:00Z atri-x starting\n",
        "port_stdout": "127.0.0.1:49153\n",
        "remove_code": 0,
        "remove_image_code": 0,
        "remove_volume_code": 0,
    }

    def __init__(self, **overrides) -> None:
        unknown = set(overrides) - set(self.DEFAULTS)
        if unknown:
            raise AssertionError(f"FakeDocker 未知配置：{sorted(unknown)}")
        super().__init__("docker", capture=self._forbid_real)
        self.options = dict(self.DEFAULTS)
        self.options.update(overrides)
        self.calls: list[list[str]] = []
        self._probe_index = 0
        self._stats_index = 0
        #: 规则名 → 命中次数（``_route`` / ``_route_exec`` 的每个分支各有一个名字）。
        self.hits: dict[str, int] = {}
        #: 命中顺序（同一个规则名会按调用顺序重复出现）。
        self.hit_log: list[str] = []

    # -- 基础设施 ---------------------------------------------------------
    @staticmethod
    def _forbid_real(*_args, **_kwargs):
        raise AssertionError("FakeDocker 不应触到真实子进程（capture 被误用）")

    def _hit(self, name: str) -> None:
        """记一次规则命中。"""
        self.hits[name] = self.hits.get(name, 0) + 1
        self.hit_log.append(name)

    def rule_hits(self, name: str | None = None):
        """命中计数：给名字返回次数，不给名字返回 ``{规则名: 次数}`` 副本。"""
        if name is None:
            return dict(self.hits)
        return self.hits.get(name, 0)

    @staticmethod
    def _unknown(args: list[str], where: str) -> AssertionError:
        return AssertionError(
            f"FakeDocker.{where} 未识别的命令：{list(args)}；"
            "请在 tests/_deploy_harness_fakes.py 里补路由，不要让它静默成功"
        )

    def _run(self, args, *, timeout=None, cwd=None):
        self.calls.append(list(args))
        return self._route(list(args))

    def argv_of(self, *needles: str) -> list[list[str]]:
        """返回所有「包含这些**独立参数**」的调用 argv。

        用**完全相等**而不是子串匹配：``-f``/``rm`` 这类短词做子串匹配会误命中
        ``--format`` 之类的长参数（踩过）。
        """
        return [call for call in self.calls if all(needle in call for needle in needles)]

    def ran(self, *needles: str) -> bool:
        """是否发生过包含这些独立参数的调用。"""
        return bool(self.argv_of(*needles))

    # -- 路由 -------------------------------------------------------------
    def _route(self, args):
        head = args[0] if args else ""
        if head == "version":
            self._hit("version")
            if not self.options["version"]:
                return completed(args, 1, "", NPIPE_ERROR)
            return completed(args, 0, self.options["version"] + "\n")
        if head == "build":
            self._hit("build")
            return completed(
                args,
                self.options["build_code"],
                self.options["build_stdout"],
                self.options["build_stderr"],
            )
        if head == "image":
            if args[1:2] != ["inspect"]:
                raise self._unknown(args, "_route(image)")
            self._hit("image.inspect")
            code = 0 if args[-1] in tuple(self.options["images"]) else 1
            return completed(args, code, "", "")
        if head == "volume":
            sub = args[1] if len(args) > 1 else ""
            if sub == "create":
                self._hit("volume.create")
                return completed(args, self.options["volume_create_code"], "", "")
            if sub == "rm":
                self._hit("volume.rm")
                return completed(args, self.options["remove_volume_code"], "", "")
            raise self._unknown(args, "_route(volume)")
        if head == "run":
            if "--entrypoint" in args:
                self._hit("run.seed")
                return completed(args, self.options["seed_code"], "", self.options["seed_stderr"])
            self._hit("run.detached")
            return completed(
                args,
                self.options["run_code"],
                self.options["run_stdout"],
                self.options["run_stderr"],
            )
        if head == "exec":
            return self._route_exec(args)
        if head == "inspect":
            return self._route_inspect(args)
        if head == "logs":
            self._hit("logs")
            return completed(args, 0, self.options["logs_stdout"], "")
        if head == "stats":
            self._hit("stats")
            return completed(args, 0, self._next_stats(), "")
        if head == "port":
            self._hit("port")
            return completed(args, 0, self.options["port_stdout"], "")
        if head == "rm":
            self._hit("rm")
            return completed(args, self.options["remove_code"], "", "")
        if head == "rmi":
            self._hit("rmi")
            return completed(args, self.options["remove_image_code"], "", "")
        raise self._unknown(args, "_route")

    def _route_inspect(self, args):
        if "{{.Config.User}}" in args:
            self._hit("inspect.user")
        else:
            self._hit("inspect.state")
        if not self.options["state_exists"]:
            return completed(args, 1, "", "Error: No such object")
        if "{{.Config.User}}" in args:
            return completed(args, 0, self.options["user"] + "\n", "")
        state = {
            "Status": "running" if self.options["state_running"] else "exited",
            "Running": bool(self.options["state_running"]),
            "RestartCount": int(self.options["restart_count"]),
            "ExitCode": 0 if self.options["state_running"] else 1,
        }
        return completed(args, 0, json.dumps(state), "")

    def _route_exec(self, args):
        # docker exec [--user <u>] <container> <command...>
        index = 1
        while index < len(args) and args[index].startswith("-"):
            index += 2 if args[index] in ("--user", "-u") else 1
        command = args[index + 1 :] if index < len(args) else []
        if command[:3] == ["python", "-m", "atri_x"] and command[3:4] == ["--health-probe"]:
            self._hit("exec.health-probe")
            codes = tuple(self.options["probe_codes"])
            code = codes[min(self._probe_index, len(codes) - 1)]
            self._probe_index += 1
            return completed(args, code, "ok\n" if code == 0 else "", "" if code == 0 else "no\n")
        if command[:3] == ["python", "-m", "atri_x"] and command[3:4] == ["--help"]:
            self._hit("exec.help")
            return completed(args, self.options["help_code"], "Usage:\n", "")
        if command[:3] == ["python", "-m", "pip"]:
            self._hit("exec.pip")
            return completed(args, self.options["pip_code"], self.options["pip_stdout"], "")
        script = command[2] if len(command) > 2 and command[1] == "-c" else ""
        if "import atri_x" in script:
            self._hit("exec.import")
            return completed(args, self.options["import_code"], self.options["import_stdout"], "")
        if "urlopen" in script:
            self._hit("exec.http-live")
            return completed(args, self.options["layer_http_code"], "", "")
        if "socket" in script:
            self._hit("exec.tcp")
            return completed(args, self.options["layer_tcp_code"], "", "")
        if "os.listdir" in script:
            self._hit("exec.layout")
            return completed(args, self.options["layout_code"], self.options["layout_stdout"], "")
        raise self._unknown(args, "_route_exec")

    def _next_stats(self) -> str:
        sequence = tuple(self.options["stats_sequence"])
        if not sequence:
            return self.options["stats_stdout"]
        value = sequence[min(self._stats_index, len(sequence) - 1)]
        self._stats_index += 1
        return value


class HarnessCase(unittest.TestCase):
    """公共夹具：仓库根下的临时目录 + 便捷构造器（``tearDown`` 里 rmtree）。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = TMP_ROOT

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def report_path(self, name: str = "report.json") -> Path:
        return self.tmp / name

    def run_smoke(self, docker: FakeDocker, **options):
        """跑一次冒烟测试，返回 ``(退出码, SmokeTest)``；报告默认写进临时目录。"""
        options.setdefault("json_path", str(self.report_path()))
        options.setdefault("timeout", 0.0)
        options.setdefault("sleep", lambda _seconds: None)
        test = smoke_module.SmokeTest(docker=docker, **options)
        return test.run(), test

    def run_stability(self, docker: FakeDocker, **options):
        """跑一次稳定性测试，返回 ``(退出码, StabilityTest)``。"""
        options.setdefault("json_path", str(self.report_path("stability.json")))
        options.setdefault("duration", 0.0)
        options.setdefault("interval", 0.0)
        options.setdefault("sleep", lambda _seconds: None)
        test = stability_module.StabilityTest(docker=docker, **options)
        return test.run(), test

    def steps(self, test) -> dict[str, dict]:
        """步骤名 → ``to_dict()`` 后的字典。"""
        return {step.name: step.to_dict() for step in test.steps.steps}
