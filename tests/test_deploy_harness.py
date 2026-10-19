"""``deploy/smoke.py`` / ``deploy/stability.py`` 的自测：用**假 docker** 驱动全部分支。

对应上游：无（上游只有 ``deploy/tests/*.sh``；容器冒烟/稳定性测试是 Python 版新增的）。

假 docker 在 :mod:`tests._deploy_harness_fakes` 里（与 ``tests/agent_fakes.py``
同一约定）：:class:`FakeDocker` 覆写 ``deploy/_docker.py`` 的 ``DockerCLI._run``，
记录每次调用的 argv、按预置 stdout 返回，于是构建成功/失败、三层健康探测的每一种回落、
非 root 断言、``pip list`` 出现第三方包、``RestartCount != 0``、失败率与内存增长的
**边界值**（恰好 1%、恰好 1.5 倍）全都能在没有 Docker 的机器上跑到。

进程级端到端（真跑 ``python deploy/smoke.py``）与 fixtures 的加载验证在
:mod:`tests.test_deploy_harness_cli`（拆文件是为了不超过 PORTING.md §2 的行数上限）。

本文件**不**触碰真实 Docker，也不联网。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests._deploy_harness_fakes import (
    BASE_FREEZE,
    DEPLOY_SRC,
    FakeClock,
    FakeDocker,
    HarnessCase,
)

import _smokelib as _sl  # noqa: E402  （sys.path 由 fakes 模块补好）
import stability as _stability  # noqa: E402

# ---------------------------------------------------------------------------
# 记账 / 解析 / 规格
# ---------------------------------------------------------------------------
class TestStepLog(HarnessCase):
    """步骤记账：JSON 报告的 steps[] 契约。"""

    def test_step_dict_has_fixed_keys(self) -> None:
        step = _sl.Step(name="x", ok=True, detail="d", seconds=1.23456)
        self.assertEqual(set(step.to_dict()), {"name", "ok", "detail", "seconds"})
        self.assertEqual(step.to_dict()["seconds"], 1.235)

    def test_ok_is_all_steps(self) -> None:
        log = _sl.StepLog()
        log.record("a", True)
        self.assertTrue(log.ok)
        log.record("b", False)
        self.assertFalse(log.ok)
        self.assertEqual([step.name for step in log.failed()], ["b"])

    def test_skipped_counts_as_ok_with_prefix(self) -> None:
        log = _sl.StepLog()
        step = log.skipped("build", "--image 已给出")
        self.assertTrue(step.ok and step.skipped)
        self.assertTrue(step.detail.startswith("skipped:"))
        self.assertTrue(log.ok)

    def test_finish_measures_elapsed(self) -> None:
        log = _sl.StepLog()
        log.start("a")
        step = log.finish("a", True, "done")
        self.assertGreaterEqual(step.seconds, 0.0)
        self.assertEqual(step.detail, "done")

    def test_print_steps_marks_failures(self) -> None:
        import io

        log = _sl.StepLog()
        log.record("a", True, "fine")
        log.record("b", False, "bad\n第二行")
        buffer = io.StringIO()
        _sl.print_steps(log.steps, stream=buffer)
        text = buffer.getvalue()
        self.assertIn("[ OK ] a", text)
        self.assertIn("[FAIL] b", text)
        self.assertIn("| 第二行", text)


class TestParsers(HarnessCase):
    """docker 输出解析：容器用户、pip 清单、stats、内存增长分位。"""

    def test_parse_container_user(self) -> None:
        self.assertEqual(_sl.parse_container_user("10001:10001"), "10001")
        self.assertEqual(_sl.parse_container_user("atri-x"), "atri-x")
        self.assertEqual(_sl.parse_container_user(""), "")

    def test_parse_pip_freeze_skips_noise(self) -> None:
        text = "# comment\n\npip==24.2\nwheel==0.44.0\nlocal @ file:///x\n"
        self.assertEqual(_sl.parse_pip_freeze(text), ["pip", "wheel", "local"])

    def test_third_party_flags_unknown_package(self) -> None:
        self.assertEqual(_sl.third_party_packages(["pip", "setuptools", "wheel", "requests"]),
                         ["requests"])

    def test_third_party_tolerates_pillow_and_first_party(self) -> None:
        self.assertEqual(_sl.third_party_packages(["pip", "Pillow", "atri_x"]), [])
        self.assertEqual(_sl.tolerated_packages(["pip", "Pillow"]), ["Pillow"])

    def test_third_party_normalizes_dash_case(self) -> None:
        self.assertEqual(_sl.third_party_packages(["PyYAML"]), ["PyYAML"])
        self.assertEqual(_sl.third_party_packages(["ATRI_X", "pillow"]), [])

    def test_parse_mem_usage_units(self) -> None:
        self.assertAlmostEqual(_sl.parse_mem_usage("42.3MiB / 1.5GiB"), 42.3 * 1024**2)
        self.assertAlmostEqual(_sl.parse_mem_usage("1GiB / 2GiB"), float(1024**3))
        self.assertAlmostEqual(_sl.parse_mem_usage("512KiB"), 512 * 1024.0)
        self.assertEqual(_sl.parse_mem_usage("0B / 1GiB"), 0.0)
        self.assertIsNone(_sl.parse_mem_usage("nonsense"))

    def test_parse_cpu_percent(self) -> None:
        self.assertEqual(_sl.parse_cpu_percent("0.15%"), 0.15)
        self.assertEqual(_sl.parse_cpu_percent("12%"), 12.0)
        self.assertIsNone(_sl.parse_cpu_percent("--"))

    def test_parse_stats_line(self) -> None:
        cpu, rss = _sl.parse_stats_line("0.12%|42.3MiB / 1.5GiB\n")
        self.assertEqual(cpu, 0.12)
        self.assertAlmostEqual(rss, 42.3 * 1024**2)
        self.assertEqual(_sl.parse_stats_line(""), (None, None))

    def test_thirds_means_needs_three_samples(self) -> None:
        self.assertEqual(_sl.thirds_means([1.0, 2.0]), (None, None))
        # 6 个样本 → 每段取 2 个：前段 [10,20]、后段 [50,60]。
        first, last = _sl.thirds_means([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
        self.assertEqual(first, 15.0)
        self.assertEqual(last, 55.0)

    def test_failure_rate_boundary_exactly_one_percent_passes(self) -> None:
        self.assertTrue(_sl.failures_at_or_below(1, 100, 0.01))
        self.assertFalse(_sl.failures_at_or_below(2, 100, 0.01))
        self.assertFalse(_sl.failures_at_or_below(0, 0, 0.01))

    def test_growth_ratio_boundary_exactly_1_5_passes(self) -> None:
        self.assertTrue(_sl.growth_ratio_ok(1.5, 1.5))
        self.assertFalse(_sl.growth_ratio_ok(1.5001, 1.5))

    def test_ascii_bar_and_format_bytes(self) -> None:
        self.assertEqual(_sl.ascii_bar(None, 10.0, width=4), " " * 4)
        self.assertEqual(_sl.ascii_bar(10.0, 10.0, width=4), "####")
        self.assertEqual(len(_sl.ascii_bar(5.0, 10.0, width=10)), 10)
        self.assertEqual(_sl.format_bytes(1024.0**3), "1.0GiB")
        self.assertEqual(_sl.format_bytes(None), "n/a")

    def test_container_spec_argv_matches_compose_style(self) -> None:
        spec = _sl.ContainerSpec(name="c", image="img:1", volume="vol", env={"A": "1"})
        argv = spec.argv()
        self.assertEqual(argv[0], "--rm")
        self.assertIn("-d", argv)
        self.assertIn("vol:/data", argv)
        self.assertIn("127.0.0.1::32171", argv)
        self.assertIn("A=1", argv)
        self.assertEqual(argv[-1], "img:1")

    def test_container_spec_keep_drops_rm(self) -> None:
        spec = _sl.ContainerSpec(name="c", image="i", volume="v", auto_remove=False)
        self.assertNotIn("--rm", spec.argv())


class TestHealthLayerConsistency(HarnessCase):
    """三层探测必须与 Dockerfile / compose 的 HEALTHCHECK 同源（防漂移）。"""

    @staticmethod
    def _normalized(text: str) -> str:
        return text.replace('"', "").replace("\\\n", " ")

    def _assert_layers_in(self, path: Path) -> None:
        text = self._normalized(path.read_text(encoding="utf-8"))
        for _layer, command in _sl.HEALTH_LAYERS:
            joined = " ".join(command)
            self.assertIn(joined, text, f"{path.name} 里缺少探测命令：{joined}")

    def test_dockerfile_healthcheck_has_all_three_layers(self) -> None:
        self._assert_layers_in(DEPLOY_SRC / "Dockerfile")

    def test_compose_healthcheck_has_all_three_layers(self) -> None:
        self._assert_layers_in(DEPLOY_SRC / "docker-compose.yml")

    def test_layer_order_is_probe_then_http_then_tcp(self) -> None:
        self.assertEqual([name for name, _cmd in _sl.HEALTH_LAYERS],
                         ["health_probe", "http_live", "tcp_32171"])


class TestImageDefaultCommand(HarnessCase):
    """镜像默认命令必须是长驻的 ``service run``（防漂移）。

    真实事故（Docker 冒烟抓到）：两个 Dockerfile 的 ``CMD`` 曾被写成 ``["run"]``。
    ``run`` 是完整前台模式，按 Go ``runPlatforms`` 的语义，所有平台运行时退出后进程
    就跟着退出；冒烟用的最小配置一个平台都没启用，于是容器启动完成 0.5 秒后**以退出码 0
    正常退出**，表现为"容器起来又没了"。上游镜像的 CMD 是 ``service run``。
    """

    @staticmethod
    def _cmd_line(path: Path) -> str:
        """取镜像默认命令那一行（exec 形式 ``CMD [...]``）。

        注意不能只找 ``CMD``：``HEALTHCHECK`` 里也有一行续行的 ``CMD python -m ...``。
        """
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("CMD ["):
                return stripped
        return ""

    def test_main_dockerfile_defaults_to_service_run(self) -> None:
        self.assertEqual(self._cmd_line(DEPLOY_SRC / "Dockerfile"), 'CMD ["service", "run"]')

    def test_prebuilt_dockerfile_defaults_to_service_run(self) -> None:
        self.assertEqual(
            self._cmd_line(DEPLOY_SRC / "pack" / "Dockerfile.prebuilt"),
            'CMD ["service", "run"]',
        )

    def test_service_mode_keeps_running_without_platforms(self) -> None:
        """服务模式在零平台时保持存活；完整前台模式则退出（Go ``platforms.go:280-284``）。"""
        import inspect

        from atri_x.app import platforms as app_platforms

        source = inspect.getsource(app_platforms.run_platforms)
        self.assertIn("RUN_MODE_SERVICE", source)
        self.assertIn("no platform is enabled", source)


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------
class TestHealthProbing(HarnessCase):
    """三层回落的每一种组合，以及容器退出/消失时的行为。"""

    def test_first_layer_wins(self) -> None:
        result = _sl.probe_health_layers(FakeDocker(), "c")
        self.assertTrue(result["ok"])
        self.assertEqual(result["layer"], "health_probe")
        self.assertEqual(len(result["attempts"]), 1)

    def test_falls_back_to_http_live(self) -> None:
        result = _sl.probe_health_layers(FakeDocker(probe_codes=(1,)), "c")
        self.assertTrue(result["ok"])
        self.assertEqual(result["layer"], "http_live")
        self.assertEqual([item["layer"] for item in result["attempts"]],
                         ["health_probe", "http_live"])

    def test_falls_back_to_tcp_last(self) -> None:
        result = _sl.probe_health_layers(
            FakeDocker(probe_codes=(1,), layer_http_code=1), "c"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["layer"], "tcp_32171")
        self.assertEqual(len(result["attempts"]), 3)

    def test_all_layers_failing_reports_each_code(self) -> None:
        result = _sl.probe_health_layers(
            FakeDocker(probe_codes=(1,), layer_http_code=1, layer_tcp_code=1), "c"
        )
        self.assertFalse(result["ok"])
        self.assertIn("health_probe=1", result["detail"])
        self.assertIn("tcp_32171=1", result["detail"])

    def test_wait_healthy_retries_until_probe_passes(self) -> None:
        # 第一轮三层全失败，第二轮第一层成功 → 只睡一次。
        docker = FakeDocker(probe_codes=(1, 0), layer_http_code=1, layer_tcp_code=1)
        slept: list[float] = []
        result = _sl.wait_healthy(docker, "c", timeout=60.0, interval=2.0,
                                  sleep=slept.append, clock=FakeClock(0.1))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rounds"], 2)
        self.assertEqual(slept, [2.0])
        self.assertEqual(result["layer"], "health_probe")

    def test_wait_healthy_times_out_with_attempts(self) -> None:
        docker = FakeDocker(probe_codes=(1,), layer_http_code=1, layer_tcp_code=1)
        result = _sl.wait_healthy(docker, "c", timeout=0.0, sleep=lambda _s: None)
        self.assertFalse(result["ok"])
        self.assertIn("仍未 healthy", result["detail"])

    def test_wait_healthy_stops_when_container_exited_and_keeps_logs(self) -> None:
        docker = FakeDocker(state_running=False, logs_stdout="boom: startup failed\n")
        result = _sl.wait_healthy(docker, "c", timeout=60.0, sleep=lambda _s: None)
        self.assertFalse(result["ok"])
        self.assertIn("容器已退出", result["detail"])
        self.assertIn("boom: startup failed", result["logs"])

    def test_wait_healthy_reports_removed_container(self) -> None:
        result = _sl.wait_healthy(FakeDocker(state_exists=False), "c", timeout=60.0,
                                  sleep=lambda _s: None)
        self.assertFalse(result["ok"])
        self.assertIn("--rm", result["detail"])


# ---------------------------------------------------------------------------
# 冒烟测试
# ---------------------------------------------------------------------------
class TestSmokeHappyPath(HarnessCase):
    """全绿路径：步骤集合、命令形状、报告、清理。"""

    def test_full_run_is_green_and_cleans_up(self) -> None:
        docker = FakeDocker()
        code, test = self.run_smoke(docker)
        self.assertEqual(code, 0, test.report)
        self.assertTrue(test.report["ok"])
        self.assertTrue(docker.ran("volume", "rm"))
        self.assertTrue(docker.ran("rm", "-f"))
        self.assertTrue(docker.ran("rmi", "-f"))

    def test_report_has_exact_required_keys(self) -> None:
        _code, test = self.run_smoke(FakeDocker())
        self.assertEqual(set(test.report),
                         {"ok", "steps", "image", "container", "docker_version"})
        self.assertEqual(test.report["docker_version"], "27.3.1")
        self.assertEqual(test.report["container"], "atri-x-smoke")
        self.assertTrue(test.report["image"].startswith("atri-x:"))
        for step in test.report["steps"]:
            self.assertEqual(set(step), {"name", "ok", "detail", "seconds"})

    def test_report_written_to_json_file(self) -> None:
        path = self.report_path("smoke-ok.json")
        code, _test = self.run_smoke(FakeDocker(), json_path=str(path))
        self.assertEqual(code, 0)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual([step["name"] for step in payload["steps"]][:4],
                         ["docker_available", "image_ready", "seed_config", "container_start"])

    def test_build_uses_readme_command_shape(self) -> None:
        docker = FakeDocker()
        self.run_smoke(docker)
        builds = docker.argv_of("build", "-f")
        self.assertEqual(len(builds), 1)
        build = builds[0]
        self.assertIn("deploy/Dockerfile", build)
        self.assertIn("--build-arg", build)
        self.assertTrue(any(str(part).startswith("VERSION=") for part in build))

    def test_container_run_mounts_single_data_volume(self) -> None:
        docker = FakeDocker()
        self.run_smoke(docker)
        runs = [call for call in docker.argv_of("run", "-d") if "--entrypoint" not in call]
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        self.assertIn("--rm", argv)
        self.assertIn("atri_x_smoke_data:/data", argv)
        # 不允许给 /data 的子路径再挂卷（会遮掉父挂载）。
        self.assertFalse(any(str(part).startswith("atri_x_smoke_data:/data/") for part in argv))

    def test_seed_uses_helper_container_and_chowns_back(self) -> None:
        docker = FakeDocker()
        self.run_smoke(docker)
        seeds = docker.argv_of("--entrypoint")
        self.assertEqual(len(seeds), 1)
        argv = seeds[0]
        self.assertIn("0:0", argv)
        self.assertIn("--rm", argv)
        self.assertTrue(any(str(part).endswith(":/seed:ro") for part in argv),
                        f"缺少只读挂载：{argv}")
        self.assertIn("chown -R 10001:10001 /data", argv[-1])

    def test_cleanup_detail_lists_removed_objects(self) -> None:
        _code, test = self.run_smoke(FakeDocker())
        detail = self.steps(test)["cleanup"]["detail"]
        self.assertIn("容器 atri-x-smoke", detail)
        self.assertIn("卷 atri_x_smoke_data", detail)

    def test_keep_skips_cleanup_and_rm(self) -> None:
        docker = FakeDocker()
        code, test = self.run_smoke(docker, keep=True)
        self.assertEqual(code, 0)
        step = self.steps(test)["cleanup"]
        self.assertTrue(step["ok"])
        self.assertTrue(step["detail"].startswith("skipped:"))
        self.assertFalse(docker.ran("rmi", "-f"))
        runs = [call for call in docker.argv_of("run", "-d") if "--entrypoint" not in call]
        self.assertNotIn("--rm", runs[0])


class TestSmokeWithExistingImage(HarnessCase):
    """``--image``：跳过构建、不动用户给的镜像。"""

    def test_image_skips_build(self) -> None:
        docker = FakeDocker()
        code, test = self.run_smoke(docker, image="atri-x:0.9.0")
        self.assertEqual(code, 0)
        self.assertIn("未构建", self.steps(test)["image_ready"]["detail"])
        self.assertFalse(docker.ran("build"))
        self.assertFalse(docker.ran("rmi"))
        self.assertEqual(test.report["image"], "atri-x:0.9.0")

    def test_missing_image_fails_and_skips_rest(self) -> None:
        docker = FakeDocker()
        code, test = self.run_smoke(docker, image="atri-x:absent")
        self.assertEqual(code, 1)
        steps = self.steps(test)
        self.assertIn("不存在", steps["image_ready"]["detail"])
        self.assertTrue(steps["container_start"]["detail"].startswith("skipped:"))
        self.assertFalse(docker.ran("run", "-d"))


class TestSmokeFailures(HarnessCase):
    """逐条断言各自的失败路径。"""

    def test_docker_unavailable_marks_everything_skipped(self) -> None:
        code, test = self.run_smoke(FakeDocker(version=""))
        self.assertEqual(code, 1)
        steps = self.steps(test)
        self.assertFalse(steps["docker_available"]["ok"])
        self.assertIn("dockerDesktopLinuxEngine", steps["docker_available"]["detail"])
        self.assertTrue(steps["healthy"]["detail"].startswith("skipped:"))
        self.assertEqual(test.report["docker_version"], "")

    def test_build_failure_prints_last_40_lines(self) -> None:
        noise = "\n".join(f"step {index}" for index in range(1, 61))
        code, test = self.run_smoke(FakeDocker(build_code=1, build_stderr=noise))
        self.assertEqual(code, 1)
        detail = self.steps(test)["image_ready"]["detail"]
        self.assertIn("step 21", detail)      # 更早的行被 tail 掉了
        self.assertNotIn("step 20\n", detail)
        self.assertIn("step 60", detail)

    def test_seed_failure_skips_container_start(self) -> None:
        docker = FakeDocker(seed_code=1, seed_stderr="cp: cannot create regular file")
        code, test = self.run_smoke(docker)
        self.assertEqual(code, 1)
        steps = self.steps(test)
        self.assertIn("cp: cannot create", steps["seed_config"]["detail"])
        self.assertTrue(steps["container_start"]["detail"].startswith("skipped:"))
        self.assertTrue(docker.ran("rmi", "-f"))  # 已经构建出来的镜像仍要清掉

    def test_run_failure_reports_docker_error(self) -> None:
        docker = FakeDocker(run_code=125, run_stderr="docker: Error response from daemon: port")
        code, test = self.run_smoke(docker)
        self.assertEqual(code, 1)
        detail = self.steps(test)["container_start"]["detail"]
        self.assertIn("退出码 125", detail)
        self.assertIn("port", detail)

    def test_health_timeout_fails_with_logs(self) -> None:
        docker = FakeDocker(
            probe_codes=(1,),
            layer_http_code=1,
            layer_tcp_code=1,
            logs_stdout="atri-x: DefaultRuntimeFactory.build() missing 1 required argument\n",
        )
        code, test = self.run_smoke(docker, timeout=0.0)
        self.assertEqual(code, 1)
        detail = self.steps(test)["healthy"]["detail"]
        self.assertIn("docker logs", detail)
        self.assertIn("DefaultRuntimeFactory", detail)

    def test_root_user_fails_non_root_assertion(self) -> None:
        for user in ("0", "", "root", "0:0"):
            with self.subTest(user=user):
                code, test = self.run_smoke(FakeDocker(user=user))
                self.assertEqual(code, 1)
                step = self.steps(test)["user_not_root"]
                self.assertFalse(step["ok"])
                self.assertIn("root", step["detail"])

    def test_named_user_passes(self) -> None:
        code, test = self.run_smoke(FakeDocker(user="10001:10001"))
        self.assertEqual(code, 0)
        self.assertTrue(self.steps(test)["user_not_root"]["ok"])

    def test_third_party_package_fails(self) -> None:
        docker = FakeDocker(pip_stdout=BASE_FREEZE + "requests==2.32.3\n")
        code, test = self.run_smoke(docker)
        self.assertEqual(code, 1)
        step = self.steps(test)["no_third_party_deps"]
        self.assertFalse(step["ok"])
        self.assertIn("requests", step["detail"])

    def test_pip_missing_is_reported(self) -> None:
        _code, test = self.run_smoke(FakeDocker(pip_code=1))
        step = self.steps(test)["no_third_party_deps"]
        self.assertFalse(step["ok"])
        self.assertIn("pip list 失败", step["detail"])

    def test_import_probe_failures(self) -> None:
        for options, why in (
            ({"import_code": 1}, "import 非 0"),
            ({"import_stdout": "/tmp/atri_x.py\n"}, "路径不在包里"),
        ):
            with self.subTest(why=why):
                _code, test = self.run_smoke(FakeDocker(**options))
                self.assertFalse(self.steps(test)["import_atri_x"]["ok"])

    def test_help_nonzero_fails(self) -> None:
        _code, test = self.run_smoke(FakeDocker(help_code=2))
        step = self.steps(test)["help_exit_zero"]
        self.assertFalse(step["ok"])
        self.assertIn("退出码 2", step["detail"])

    def test_health_probe_step_fails_when_layer_one_broken(self) -> None:
        docker = FakeDocker(probe_codes=(1,), layer_http_code=0)
        code, test = self.run_smoke(docker)
        self.assertEqual(code, 1)
        self.assertTrue(self.steps(test)["healthy"]["ok"])          # 第二层兜住了
        self.assertFalse(self.steps(test)["health_probe_exit_zero"]["ok"])

    def test_layout_problems_fail(self) -> None:
        cases = (
            ({"missing": ["/data/config/atri-x/state.toml"],
              "data_dir": "/data/data", "data_entries": ["sessions.db"]}, "state.toml"),
            ({"missing": [], "data_dir": "/data/data", "data_entries": ["logs"]}, "没有 .db"),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                docker = FakeDocker(layout_stdout=json.dumps(payload))
                code, test = self.run_smoke(docker)
                self.assertEqual(code, 1)
                step = self.steps(test)["data_volume_layout"]
                self.assertFalse(step["ok"])
                self.assertIn(expected, step["detail"])

    def test_restart_count_nonzero_fails(self) -> None:
        code, test = self.run_smoke(FakeDocker(restart_count=2))
        self.assertEqual(code, 1)
        step = self.steps(test)["container_running"]
        self.assertFalse(step["ok"])
        self.assertIn("RestartCount=2", step["detail"])

    def test_container_not_running_fails(self) -> None:
        _code, test = self.run_smoke(FakeDocker(state_running=False))
        self.assertFalse(self.steps(test)["container_running"]["ok"])

    def test_removed_container_fails_running_step(self) -> None:
        _code, test = self.run_smoke(FakeDocker(state_exists=False))
        step = self.steps(test)["container_running"]
        self.assertFalse(step["ok"])
        self.assertIn("--rm", step["detail"])


# ---------------------------------------------------------------------------
# 稳定性测试
# ---------------------------------------------------------------------------
class TestStabilityRun(HarnessCase):
    """长跑流程、采样记录、报告契约、四条判定的边界。"""

    def test_sample_loop_collects_one_sample_per_iteration(self) -> None:
        code, test = self.run_stability(FakeDocker())
        self.assertEqual(test.samples, 1)
        self.assertEqual(len(test.sample_detail), 1)
        self.assertEqual(code, 1)  # 样本 <3，内存判定不通过（见模块 docstring）
        self.assertFalse(self.steps(test)["sample"]["detail"].startswith("skipped:"))

    def test_report_has_all_required_keys(self) -> None:
        _code, test = self.run_stability(FakeDocker())
        required = {
            "ok", "duration_seconds", "samples", "failures", "restart_count",
            "rss_first_third_mean", "rss_last_third_mean", "rss_growth_ratio",
            "log_tracebacks", "summary",
        }
        self.assertTrue(required.issubset(set(test.report)), sorted(required - set(test.report)))
        self.assertIsInstance(test.report["summary"], str)

    def test_report_written_to_json_file(self) -> None:
        path = self.report_path("stability.json")
        self.run_stability(FakeDocker(), json_path=str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("sample_detail", payload)
        self.assertEqual(payload["samples"], 1)

    def test_samples_use_published_port(self) -> None:
        docker = FakeDocker(port_stdout="127.0.0.1:49153\n")
        _code, test = self.run_stability(docker)
        self.assertEqual(test.base_url, "http://127.0.0.1:49153")
        self.assertTrue(docker.ran("port", "32171/tcp"))


class TestStabilityReadyGate(HarnessCase):
    """采样窗口之前要先等 ``/ready``：冷启动瞬态不该算进稳定性失败率。

    真实事故（2026-10-17 20:44 的 300s 长跑）：容器 ``healthy`` 的三层探测最后一层只是
    TCP 连通；冷启动数据卷首次初始化时 ``/ready`` 在 start 后约 8~14 秒仍是 503，
    第 1 次采样撞上它 → 50 次采样的失败率 1/50=2% > 1% 阈值，稳定性结论被判红。
    """

    def _make(self, probe, *, startup_timeout: float = 30.0):
        test = _stability.StabilityTest(
            docker=FakeDocker(),
            duration=1.0,
            interval=0.0,
            sleep=lambda _seconds: None,
            startup_timeout=startup_timeout,
            health_interval=0.0,
            probe=probe,
        )
        test.base_url = "http://127.0.0.1:49153"
        return test

    def test_waits_until_ready_before_sampling(self) -> None:
        calls: list[str] = []

        def probe(url: str):
            calls.append(url)
            return (200, "{}") if len(calls) >= 3 else (503, "not ready")

        test = self._make(probe)
        test._wait_ready()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(url.endswith("/ready") for url in calls))

    def test_gives_up_after_startup_budget(self) -> None:
        calls: list[str] = []

        def probe(url: str):
            calls.append(url)
            return (503, "not ready")

        test = self._make(probe, startup_timeout=1.0)
        test._wait_ready()  # 不允许无限等下去
        self.assertGreater(len(calls), 0)
        self.assertLess(len(calls), 200)

    def test_skipped_when_sampling_window_is_zero(self) -> None:
        """干跑/单测（duration=0）没有采样窗口，不该对假端口做真实 HTTP 轮询。"""
        calls: list[str] = []

        def probe(url: str):
            calls.append(url)
            return (503, "not ready")

        test = self._make(probe)
        test.duration = 0.0
        test._wait_ready()
        self.assertEqual(calls, [])

    def test_endpoint_failures_recorded_with_timestamp(self) -> None:
        _code, test = self.run_stability(FakeDocker(), probe=lambda url: (503, "not ready"))
        self.assertEqual(test.failures, 1)
        self.assertEqual(len(test.failure_detail), 3)
        first = test.failure_detail[0]
        self.assertEqual(set(first), {"timestamp", "endpoint", "status", "error", "counted"})
        self.assertEqual(first["endpoint"], "/live")
        self.assertEqual(first["status"], 503)

    def test_missing_published_port_fails_sampling(self) -> None:
        code, test = self.run_stability(FakeDocker(port_stdout=""))
        self.assertEqual(code, 1)
        self.assertFalse(self.steps(test)["sample"]["ok"])
        self.assertIn("宿主机端口", self.steps(test)["sample"]["detail"])

    def test_stats_parsed_into_rss_and_cpu(self) -> None:
        _code, test = self.run_stability(FakeDocker(stats_stdout="3.50%|64.0MiB / 1.5GiB\n"))
        sample = test.sample_detail[0]
        self.assertEqual(sample["cpu_percent"], 3.5)
        self.assertAlmostEqual(sample["rss_bytes"], 64.0 * 1024**2)

    def test_restart_check_fails_on_restart(self) -> None:
        _code, test = self.run_stability(FakeDocker(restart_count=1))
        self.assertFalse(self.steps(test)["restart_check"]["ok"])
        self.assertEqual(test.report["restart_count"], 1)
        self.assertFalse(test.report["ok"])

    def test_log_check_counts_tracebacks(self) -> None:
        docker = FakeDocker(
            logs_stdout="ok\nTraceback (most recent call last):\n  File x\nValueError\n"
        )
        _code, test = self.run_stability(docker)
        step = self.steps(test)["log_check"]
        self.assertFalse(step["ok"])
        self.assertEqual(test.report["log_tracebacks"], 1)
        self.assertIn("Traceback", step["detail"])

    def test_allow_missing_health_tolerates_healthz_404(self) -> None:
        def probe(url: str):
            return (404, "not found") if url.endswith("/healthz") else (200, "ok")

        _code, test = self.run_stability(FakeDocker(), probe=probe, allow_missing_health=True)
        self.assertEqual(test.failures, 0)
        self.assertEqual(test.failure_detail[0]["counted"], False)

    def test_strict_mode_counts_healthz_404(self) -> None:
        def probe(url: str):
            return (404, "not found") if url.endswith("/healthz") else (200, "ok")

        _code, test = self.run_stability(FakeDocker(), probe=probe)
        self.assertEqual(test.failures, 1)
        self.assertTrue(test.failure_detail[0]["counted"])

    def test_docker_unavailable_still_writes_full_report(self) -> None:
        code, test = self.run_stability(FakeDocker(version=""))
        self.assertEqual(code, 1)
        self.assertFalse(test.report["ok"])
        self.assertEqual(test.report["samples"], 0)
        self.assertEqual(test.report["failures"], 0)
        self.assertIsNone(test.report["rss_growth_ratio"])
        self.assertTrue(self.steps(test)["sample"]["detail"].startswith("skipped:"))

    def test_keep_keeps_container_and_skips_cleanup(self) -> None:
        docker = FakeDocker()
        _code, test = self.run_stability(docker, keep=True)
        self.assertTrue(self.steps(test)["cleanup"]["detail"].startswith("skipped:"))
        # --keep 下唯一的 rm 是启动前的同名残留清理；卷与镜像都不动。
        self.assertEqual(len(docker.argv_of("rm", "-f")), 1)
        self.assertFalse(docker.ran("volume", "rm"))
        self.assertFalse(docker.ran("rmi"))

    def _verdict_fixture(self, samples: int, failures: int, rss: list[float]):
        _code, test = self.run_stability(FakeDocker())
        test.samples, test.failures = samples, failures
        test.rss_values = rss
        test.restart_count, test.running, test.log_tracebacks = 0, True, 0
        return test

    def test_failure_rate_boundary_exactly_one_percent_passes(self) -> None:
        test = self._verdict_fixture(100, 1, [100.0] * 100)
        summary, verdicts = test._verdicts()
        self.assertTrue(verdicts["健康检查失败率"], summary)
        test.failures = 2
        _summary, verdicts = test._verdicts()
        self.assertFalse(verdicts["健康检查失败率"])

    def test_rss_growth_boundary_exactly_1_5_passes(self) -> None:
        test = self._verdict_fixture(30, 0, [100.0] * 15 + [150.0] * 15)
        _summary, verdicts = test._verdicts()
        self.assertTrue(verdicts["内存增长比"])
        self.assertAlmostEqual(test.report["rss_growth_ratio"], 1.5, places=3)
        test.rss_values = [100.0] * 15 + [151.0] * 15
        _summary, verdicts = test._verdicts()
        self.assertFalse(verdicts["内存增长比"])

    def test_insufficient_samples_fail_memory_verdict(self) -> None:
        test = self._verdict_fixture(2, 0, [100.0, 101.0])
        summary, verdicts = test._verdicts()
        self.assertFalse(verdicts["内存增长比"])
        self.assertIn("样本不足", summary)
        self.assertIsNone(test.report["rss_growth_ratio"])

    def test_trend_rendering_does_not_crash_on_empty(self) -> None:
        _code, test = self.run_stability(FakeDocker())
        test.sample_detail = []
        test._print_trend()  # 只是不能抛异常


class TestFakeDockerStrictness(HarnessCase):
    """假 docker 的 fail-closed 契约与命中计数。

    过去 ``_route`` / ``_route_exec`` 的兜底分支返回退出码 0：假实现不认识的命令
    会被当成"成功"，于是被测脚本新增一条 docker 调用时测试照样全绿。现在未识别
    命令一律 ``AssertionError``（消息带全 argv），并且每次命中都记账。
    """

    def test_unknown_head_is_assertion_error_with_argv(self) -> None:
        docker = FakeDocker()
        with self.assertRaises(AssertionError) as caught:
            docker._run(["volume", "prune", "-f"])
        message = str(caught.exception)
        for part in ("volume", "prune", "-f"):
            self.assertIn(part, message)
        self.assertIn("_route", message)

    def test_unknown_image_subcommand_is_not_green(self) -> None:
        docker = FakeDocker()
        with self.assertRaises(AssertionError) as caught:
            docker._run(["image", "ls"])
        self.assertIn("'ls'", str(caught.exception))

    def test_unknown_volume_subcommand_is_not_green(self) -> None:
        docker = FakeDocker()
        with self.assertRaises(AssertionError):
            docker._run(["volume", "inspect", "v"])

    def test_unknown_exec_command_is_not_green(self) -> None:
        docker = FakeDocker()
        with self.assertRaises(AssertionError) as caught:
            docker._run(["exec", "c", "python", "-m", "atri_x", "doctor", "--no-model"])
        message = str(caught.exception)
        self.assertIn("_route_exec", message)
        self.assertIn("doctor", message)

    def test_unknown_command_counts_no_hit(self) -> None:
        docker = FakeDocker()
        with self.assertRaises(AssertionError):
            docker._run(["system", "df"])
        self.assertEqual(docker.rule_hits(), {})

    def test_rule_hits_record_each_route(self) -> None:
        """命中计数能回答"这次调用命中了哪条规则、几次"。"""
        docker = FakeDocker()
        code, _test = self.run_smoke(docker)
        self.assertEqual(code, 0)
        self.assertEqual(docker.rule_hits("version"), 1)
        self.assertEqual(docker.rule_hits("build"), 1)
        self.assertEqual(docker.rule_hits("volume.create"), 1)
        self.assertEqual(docker.rule_hits("run.seed"), 1)
        self.assertEqual(docker.rule_hits("run.detached"), 1)
        self.assertEqual(docker.rule_hits("exec.pip"), 1)
        # 健康探测会跑两处：wait_healthy 的层探测 + 断言步骤
        self.assertGreaterEqual(docker.rule_hits("exec.health-probe"), 1)
        self.assertEqual(docker.rule_hits("inspect.user"), 1)
        # 自建镜像路径不做 docker image inspect；那条路由由 --image 用例覆盖
        self.assertEqual(docker.rule_hits("image.inspect"), 0)
        # rm 两次：启动前清同名残留 + 收尾清理
        self.assertEqual(docker.rule_hits("rm"), 2)
        self.assertEqual(docker.rule_hits("rmi"), 1)
        self.assertEqual(docker.rule_hits("volume.rm"), 1)
        # 未命中的规则名读出来是 0（而不是 KeyError），全部命中次数等于调用次数
        self.assertEqual(docker.rule_hits("logs"), 0)
        self.assertEqual(sum(docker.rule_hits().values()), len(docker.calls))
        self.assertEqual(docker.hit_log[0], "version")

    def test_hit_counting_exposes_retry_loops(self) -> None:
        """探测回落会按层命中：第一层失败后第二层命中，计数看得见。"""
        docker = FakeDocker(probe_codes=(1,), layer_http_code=0)
        self.run_smoke(docker)
        self.assertGreaterEqual(docker.rule_hits("exec.health-probe"), 1)
        self.assertEqual(docker.rule_hits("exec.http-live"), 1)
        self.assertEqual(docker.rule_hits("exec.tcp"), 0)

    def test_rule_hits_returns_a_copy(self) -> None:
        docker = FakeDocker()
        docker._run(["version"])
        snapshot = docker.rule_hits()
        snapshot["version"] = 99
        self.assertEqual(docker.rule_hits("version"), 1)

    def test_image_inspect_route_is_counted_in_the_image_path(self) -> None:
        docker = FakeDocker()
        code, _test = self.run_smoke(docker, image="atri-x:0.9.0")
        self.assertEqual(code, 0)
        self.assertEqual(docker.rule_hits("image.inspect"), 1)
        self.assertEqual(docker.rule_hits("build"), 0)

    def test_exec_user_option_does_not_shift_command_parsing(self) -> None:
        """``docker exec --user X <container> <cmd>`` 里的 --user 不能被当成命令。"""
        docker = FakeDocker()
        done = docker._run(["exec", "--user", "10001:10001", "c", "python", "-m", "pip", "list"])
        self.assertEqual(done.returncode, 0)
        self.assertEqual(docker.rule_hits("exec.pip"), 1)

    def test_real_capture_is_still_forbidden(self) -> None:
        """FakeDocker 的 capture 注入点被误用时必须报错（不碰真实子进程）。"""
        docker = FakeDocker()
        with self.assertRaises(AssertionError) as caught:
            docker._capture(["docker", "ps"])
        self.assertIn("不应触到真实子进程", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover - 手工单跑
    unittest.main()
