"""``deploy/smoke.py`` / ``deploy/stability.py`` 的**进程级**端到端自测。

对应上游：无（容器冒烟/稳定性测试是 Python 版新增的部署套件）。

与 :mod:`tests.test_deploy_harness` 的分工：

* ``test_deploy_harness.py``：用 :class:`tests._deploy_harness_fakes.FakeDocker`
  直接驱动两个脚本的类，覆盖每条断言与判定边界；
* 本文件：用仓库既有的 ``ATRIX_DEPLOY_FAKE_CAPTURE`` 注入点，让**真实的**
  ``deploy._docker.DockerCLI`` 走一遍 ``_lib.run_capture``，再以**子进程**跑
  ``python deploy/smoke.py`` / ``stability.py``，断言退出码、真实的 docker 命令形状
  与 JSON 报告；外加 fixtures 必须能被真实配置加载器接受的防漂移断言。

拆成两个文件是因为单个测试文件超过 PORTING.md §2 的行数上限。
本文件**不**触碰真实 Docker，也不联网。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests._deploy_harness_fakes import (
    BASE_FREEZE,
    DEPLOY_SRC,
    LAYOUT_OK,
    NPIPE_ERROR,
    REPO_ROOT,
    HarnessCase,
)


class TestCliEndToEnd(HarnessCase):
    """以子进程跑真实脚本；docker 通过环境变量注入假输出，全程不碰真 Docker。"""

    def write_capture(self, rules: list[dict]) -> Path:
        path = self.tmp / "fake_capture.json"
        path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8", newline="\n")
        return path

    def base_rules(self, *, build_code: int = 0, probe_code: int = 0) -> list[dict]:
        """按「最先匹配先赢」的顺序给出全部 docker 子命令的假输出。"""
        return [
            {"match": "version --format", "code": 0, "stdout": "27.3.1\n"},
            {"match": "build --build-arg", "code": build_code, "stdout": "Successfully built abc\n"},
            {"match": "--entrypoint", "code": 0, "stdout": ""},
            {"match": "volume create", "code": 0},
            {"match": "run --rm -d", "code": 0, "stdout": "abcdef123456\n"},
            {"match": "inspect --format {{json .State}}", "code": 0,
             "stdout": json.dumps({"Status": "running", "Running": True, "RestartCount": 0})},
            {"match": "inspect --format {{.Config.User}}", "code": 0, "stdout": "atri-x:atri-x\n"},
            {"match": "port atri-x-", "code": 0, "stdout": "127.0.0.1:49153\n"},
            {"match": "--health-probe", "code": probe_code, "stdout": "ok\n"},
            {"match": "--help", "code": 0, "stdout": "Usage:\n"},
            {"match": "pip list", "code": 0, "stdout": BASE_FREEZE},
            {"match": "import atri_x", "code": 0, "stdout": "/app/atri_x/__init__.py\n"},
            {"match": "os.listdir", "code": 0, "stdout": LAYOUT_OK},
            {"match": "stats --no-stream", "code": 0, "stdout": "0.10%|40.0MiB / 1.5GiB\n"},
            {"match": "logs --tail", "code": 0, "stdout": "atri-x starting\n"},
            {"match": "rm -f", "code": 0},
            {"match": "rmi -f", "code": 0},
            {"match": "volume rm", "code": 0},
        ]

    def run_cli(self, script: str, *args: str, rules: list[dict] | None = None):
        """跑 ``python deploy/<script> <args>``，把假 docker 输出经环境变量注入。"""
        capture = self.write_capture(rules if rules is not None else self.base_rules())
        record = self.tmp / f"{Path(script).stem}-calls.txt"
        env = dict(os.environ)
        env.update({
            "ATRIX_DEPLOY_FAKE_CAPTURE": str(capture),
            "ATRIX_DEPLOY_RECORD": str(record),
            "PYTHONIOENCODING": "utf-8",
        })
        done = subprocess.run(
            [sys.executable, str(DEPLOY_SRC / script), *args],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        return done, record

    def test_smoke_cli_green_path(self) -> None:
        report = self.tmp / "smoke.json"
        done, record = self.run_cli(
            "smoke.py", "--json", str(report), "--timeout", "5", "--volume", "v_smoke_e2e"
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["docker_version"], "27.3.1")
        calls = record.read_text(encoding="utf-8")
        self.assertIn("build --build-arg", calls)
        self.assertIn("-f deploy/Dockerfile", calls)
        self.assertIn("v_smoke_e2e:/data", calls)

    def test_smoke_cli_build_failure_is_red(self) -> None:
        report = self.tmp / "smoke-fail.json"
        done, _record = self.run_cli(
            "smoke.py", "--json", str(report), "--timeout", "5", rules=self.base_rules(build_code=1)
        )
        self.assertEqual(done.returncode, 1)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(payload["ok"])
        image_step = next(s for s in payload["steps"] if s["name"] == "image_ready")
        self.assertFalse(image_step["ok"])
        self.assertIn("docker build 失败", image_step["detail"])

    def test_smoke_cli_reports_skips_when_docker_missing(self) -> None:
        rules = [{"match": "version --format", "code": 1, "stderr": NPIPE_ERROR}]
        report = self.tmp / "smoke-nodocker.json"
        done, _record = self.run_cli("smoke.py", "--json", str(report), rules=rules)
        self.assertEqual(done.returncode, 1)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["steps"][0]["name"], "docker_available")
        self.assertFalse(payload["steps"][0]["ok"])
        self.assertIn("dockerDesktopLinuxEngine", payload["steps"][0]["detail"])
        self.assertTrue(payload["steps"][1]["detail"].startswith("skipped:"))

    def test_stability_cli_short_run_writes_full_report(self) -> None:
        report = self.tmp / "stability.json"
        done, _record = self.run_cli(
            "stability.py", "--json", str(report), "--duration", "0", "--interval", "0",
            "--volume", "v_stability_e2e",
        )
        # 长跑只采到 1 个样本 → 内存判定按「样本不足」不通过，退出码为 1（预期行为）。
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["samples"], 1)
        self.assertIsNone(payload["rss_growth_ratio"])
        self.assertEqual(payload["log_tracebacks"], 0)
        self.assertEqual(payload["docker_version"], "27.3.1")
        self.assertIn("container", payload)
        self.assertIn("image", payload)

    def test_scripts_expose_documented_flags(self) -> None:
        expected = {
            "smoke.py": ("--image", "--timeout", "--keep", "--json"),
            "stability.py": ("--duration", "--interval", "--image", "--keep", "--json",
                             "--allow-missing-health"),
        }
        for script, flags in expected.items():
            with self.subTest(script=script):
                done = subprocess.run(
                    [sys.executable, str(DEPLOY_SRC / script), "--help"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120,
                )
                self.assertEqual(done.returncode, 0)
                for flag in flags:
                    self.assertIn(flag, done.stdout)


class TestSmokeFixture(HarnessCase):
    """fixtures 必须能被**真实**配置加载器接受（键名不是臆造的）。"""

    def test_fixture_files_exist(self) -> None:
        fixture = DEPLOY_SRC / "fixtures" / "smoke-config"
        for name in ("app.toml", "services.toml", "state.toml", "SOUL.md"):
            self.assertTrue((fixture / name).is_file(), f"缺少 {name}")

    def test_fixture_loads_through_real_config_loader(self) -> None:
        """把 ``/data`` 重写到临时目录后，用 ``atri_x.config.loader`` 真加载一遍。

        这条断言的价值：``validate_mode_models`` / ``MissingConfigFileError`` 之类的
        解析期强校验会直接报错，所以它证明 fixtures 的键名与结构对得上真实 schema。
        """
        from atri_x.config import loader

        work = self.tmp / "data"
        config_dir = work / "config" / "atri-x"
        config_dir.mkdir(parents=True, exist_ok=True)
        fixture = DEPLOY_SRC / "fixtures" / "smoke-config"
        for name in ("app.toml", "services.toml", "state.toml", "SOUL.md"):
            text = (fixture / name).read_text(encoding="utf-8")
            # 用正斜杠重写：TOML 基本字符串里不需要转义，Windows 也认。
            text = text.replace('"/data/', '"' + work.as_posix() + "/")
            (config_dir / name).write_text(text, encoding="utf-8", newline="\n")

        config = loader.load(str(config_dir / "app.toml"))
        self.assertEqual(Path(config.storage.sessions_sqlite_path), work / "data" / "sessions.db")
        self.assertEqual(config.session.default_mode, "work")
        self.assertEqual(config.mode_models["work"].provider, "smoke")
        self.assertIn("smoke", config.providers)
        self.assertFalse(config.image_generation.enabled)
        # [platform.cli] 是任意平台段（AppConfig.platform 是 dict[str, dict]）。
        self.assertFalse(config.platform.get("cli", {}).get("enabled", True))

    def test_fixture_has_no_platform_and_no_secret_values(self) -> None:
        """冒烟夹具不得声明任何平台，也不得出现像真 key 的值。"""
        fixture = DEPLOY_SRC / "fixtures" / "smoke-config"
        app_text = (fixture / "app.toml").read_text(encoding="utf-8")
        for platform in ("platform.qqonebot", "platform.telegram", "platform.qqofficial"):
            self.assertNotIn(f"[{platform}]", app_text)
        for name in ("app.toml", "services.toml", "state.toml"):
            text = (fixture / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                if "api_key" in key and value.strip().strip('"').startswith("sk-"):
                    self.fail(f"{name} 里出现了看起来像真 key 的值：{stripped}")


if __name__ == "__main__":  # pragma: no cover - 手工单跑
    unittest.main()
