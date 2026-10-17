"""看门狗与 Windows 启动器的部署自测。

对应上游：``deploy/tests/watchdog_redaction_test.sh``（121 行）+
``deploy/tests/windows_script_test.sh``（75 行）。

上游 ``watchdog_redaction_test.sh`` 的五个判据逐条保留：

1. 已知秘密必须消失（``sk-`` key / Bearer / JSON 凭据 / Telegram bot token / URL userinfo）；
2. 非敏感内容（``plain=keepme``）必须保留；
3. **重复脱敏必须幂等**（第二次跑内容字节不变）；
4. 独立凭据检测必须发现通用规则覆盖不到的原始 JWT；
5. 已知凭据值检测必须发现环境变量里的秘密。

**有意偏差（重要）**：上游第 5 条只要求“检测到并报警”（``--redact-dir`` 返回
非零），Python 版在检测之外还**真的把它从文件里清掉**（见
``deploy/_libfs.py`` 的 ``redact_dir(extra_secrets=...)``）——因为“报警了但没清”
仍然会让诊断包带着凭据被外发。所以对应断言从“必须返回非零”改成
“秘密必须不在文件里”，判据更强而不是更弱。

Windows 部分用 ``atri_x.py`` 的 ``main()`` 直接调用（不经过 ``.cmd``/``.ps1``
引导壳），只断言参数解析、动作分发与退出码——真的调 Docker 需要 Docker Desktop，
本机不保证在跑。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "deploy"
TMP_ROOT = REPO_ROOT / "tmp_deploy_watchdog_test"
LIB_MODULES = ("_lib.py", "_libfs.py", "_envutil.py", "_proc.py", "_netutil.py", "_console.py")

FIXTURE_LOG = """2026-10-01T00:00:00Z starting atri-x
provider api_key=sk-abcdefghijklmnopqrstuvwxyz ok
authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadpayload
{"refresh_token":"json-secret-value","password": "pw-123456"}
https://api.telegram.org/bot123456789:AAFakeTelegramToken/getMe failed
https://user:secret-pass@example.com/hook
plain=keepme
"""

LEAKABLE_SECRETS = (
    "sk-abcdefghijklmnopqrstuvwxyz",
    "eyJhbGciOiJIUzI1NiJ9.payloadpayload",
    "json-secret-value",
    "pw-123456",
    "AAFakeTelegramToken",
    "secret-pass",
)
RAW_JWT = "eyJhbGciOiJIUzI1NiJ9.header1234567890.signature1234567890"
KNOWN_SECRET = "known-secret-value-abcdef123456"


class WatchdogFixture(unittest.TestCase):
    """公共夹具：``<tmp>/deploy/`` 里放一份可独立运行的 watchdog。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        self.tmp = TMP_ROOT
        self.deploy = self.tmp / "deploy"
        self.deploy.mkdir(parents=True, exist_ok=True)
        for name in ("atri_x_watchdog.py", *LIB_MODULES):
            shutil.copy2(DEPLOY_SRC / name, self.deploy / name)
        (self.deploy / "docker-compose.yml").write_text(
            "services:\n  atri-x:\n    image: atri-x:0.9.0\n", encoding="utf-8", newline="\n"
        )

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def diag_dir(self, name: str, files: dict[str, str]) -> Path:
        """建一个诊断目录并写入给定文件。"""
        directory = self.tmp / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename, text in files.items():
            (directory / filename).write_text(text, encoding="utf-8", newline="\n")
        return directory

    def run_watchdog(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """跑 ``python deploy/atri_x_watchdog.py ...``。"""
        merged = dict(os.environ)
        merged["PYTHONIOENCODING"] = "utf-8"
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(self.deploy / "atri_x_watchdog.py"), *args],
            cwd=str(self.tmp),
            env=merged,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )


class TestRedactDir(WatchdogFixture):
    """``--redact-dir``：脱敏 + 三项复检。"""

    def test_all_known_secrets_are_removed_and_plaintext_kept(self) -> None:
        directory = self.diag_dir("diag", {"docker-logs.txt": FIXTURE_LOG})
        result = self.run_watchdog("--redact-dir", str(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        content = (directory / "docker-logs.txt").read_text(encoding="utf-8")
        for secret in LEAKABLE_SECRETS:
            self.assertNotIn(secret, content, f"脱敏后仍能找到原始秘密：{secret}")
        self.assertIn("plain=keepme", content, "非敏感内容被误删")
        self.assertFalse((directory / "REDACTION-FAILED").exists())

    def test_redaction_is_idempotent(self) -> None:
        directory = self.diag_dir("diag-idem", {"docker-logs.txt": FIXTURE_LOG})
        first = self.run_watchdog("--redact-dir", str(directory))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        after_first = (directory / "docker-logs.txt").read_bytes()

        second = self.run_watchdog("--redact-dir", str(directory))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((directory / "docker-logs.txt").read_bytes(), after_first)

    def test_lone_json_credential_quotes_survive(self) -> None:
        """项目规则的 key=value 分支曾吃掉收尾引号；修复后引号必须还在。"""
        directory = self.diag_dir(
            "diag-json", {"notes.json": '{"api_token":"short1234","ok":true}\n'}
        )
        result = self.run_watchdog("--redact-dir", str(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = (directory / "notes.json").read_text(encoding="utf-8")
        self.assertNotIn("short1234", content)
        self.assertIn("[REDACTED]", content)

    def test_independent_detection_flags_raw_jwt(self) -> None:
        """通用规则刻意不覆盖裸 JWT；独立检测必须报出来并留下失败标记。"""
        directory = self.diag_dir("diag-jwt", {"notes.txt": f"raw={RAW_JWT}\n"})
        result = self.run_watchdog("--redact-dir", str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((directory / "REDACTION-FAILED").exists())
        self.assertIn("疑似未脱敏内容", result.stderr)

    def test_known_secret_from_environment_is_scrubbed(self) -> None:
        """环境变量里的自定义秘密：Python 版直接清除，而不是只报警。"""
        directory = self.diag_dir("diag-known", {"notes.txt": f"plain note: {KNOWN_SECRET}\n"})
        result = self.run_watchdog(
            "--redact-dir", str(directory), env={"WATCHDOG_TEST_SECRET": KNOWN_SECRET}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        content = (directory / "notes.txt").read_text(encoding="utf-8")
        self.assertNotIn(KNOWN_SECRET, content)

    def test_missing_directory_is_usage_error(self) -> None:
        result = self.run_watchdog("--redact-dir", str(self.tmp / "does-not-exist"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("用法", result.stderr)

    def test_binary_file_is_not_corrupted(self) -> None:
        """不带敏感内容的二进制文件不应该因为“读出来又写回去”而被破坏。"""
        directory = self.diag_dir("diag-bin", {})
        payload = bytes(range(256)) * 4
        (directory / "blob.bin").write_bytes(payload)
        result = self.run_watchdog("--redact-dir", str(directory))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((directory / "blob.bin").read_bytes(), payload)


class TestWatchdogRun(WatchdogFixture):
    """一次 watchdog 运行的状态机：失败计数、阈值、dry-run、暂停标记。"""

    def run_env(self, **extra: str) -> dict[str, str]:
        """指向一个不可达的 /live，并把状态目录放进临时夹具。"""
        env = {
            "WATCHDOG_COMPOSE_DIR": str(self.deploy),
            "WATCHDOG_STATE_DIR": str(self.tmp / "state"),
            "WATCHDOG_DIAG_DIR": str(self.tmp / "diagnostics"),
            "WATCHDOG_HEALTH_URL": "http://127.0.0.1:1/live",
            "WATCHDOG_READY_URL": "http://127.0.0.1:1/ready",
            "WATCHDOG_HEALTHZ_URL": "http://127.0.0.1:1/healthz",
            "WATCHDOG_HTTP_TIMEOUT": "1",
        }
        env.update(extra)
        return env

    def test_below_threshold_only_counts(self) -> None:
        result = self.run_watchdog(env=self.run_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = (self.tmp / "state" / "consecutive_failures").read_text(encoding="utf-8")
        self.assertEqual(state.strip(), "1")

    def test_dry_run_does_not_restart(self) -> None:
        state_dir = self.tmp / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "consecutive_failures").write_text("2\n", encoding="utf-8")
        result = self.run_watchdog(env=self.run_env(WATCHDOG_FAILURE_THRESHOLD="3", WATCHDOG_DRY_RUN="1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DRY_RUN=1: would restart", result.stdout)
        # dry-run 不应该消耗重启历史
        self.assertFalse((state_dir / "restart_history").exists())

    def test_paused_marker_skips_everything(self) -> None:
        state_dir = self.tmp / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "paused").write_text("2026-01-01T00:00:00+08:00\n", encoding="utf-8")
        result = self.run_watchdog(env=self.run_env())
        self.assertEqual(result.returncode, 0)
        self.assertIn("watchdog paused by operator", result.stdout)
        self.assertFalse((state_dir / "consecutive_failures").exists())

    def test_max_restarts_pauses_autorestart(self) -> None:
        state_dir = self.tmp / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "consecutive_failures").write_text("5\n", encoding="utf-8")
        import time

        now = int(time.time())
        (state_dir / "restart_history").write_text(
            "\n".join(str(now - 10 * index) for index in range(5)) + "\n", encoding="utf-8"
        )
        result = self.run_watchdog(
            env=self.run_env(WATCHDOG_MAX_RESTARTS="3", WATCHDOG_COOLDOWN_SECONDS="0")
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue((state_dir / "paused").exists())
        self.assertIn("max restarts", result.stderr)

    def test_cooldown_blocks_restart(self) -> None:
        state_dir = self.tmp / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "consecutive_failures").write_text("5\n", encoding="utf-8")
        import time

        (state_dir / "last_restart_epoch").write_text(f"{int(time.time())}\n", encoding="utf-8")
        result = self.run_watchdog(env=self.run_env(WATCHDOG_COOLDOWN_SECONDS="600"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("restart cooldown active", result.stderr)


class TestWindowsLauncher(WatchdogFixture):
    """``deploy/windows/atri_x.py``：动作集合、分发与退出码。"""

    def setUp(self) -> None:
        super().setUp()
        self.windows = self.deploy / "windows"
        self.windows.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEPLOY_SRC / "windows" / "atri_x.py", self.windows / "atri_x.py")

    def load_module(self):
        """导入夹具里的 ``atri_x`` 模块（每次重新加载，避免串味）。"""
        for name in ("atri_x",):
            sys.modules.pop(name, None)
        sys.path.insert(0, str(self.windows))
        try:
            import atri_x as module  # noqa: PLC0415 - 测试里按需导入

            return module
        finally:
            sys.path.remove(str(self.windows))

    def call(self, argv: list[str]) -> tuple[int, str, str]:
        """调 ``main()`` 并捕获输出与退出码。"""
        module = self.load_module()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def test_action_set_matches_upstream_validate_set(self) -> None:
        module = self.load_module()
        expected = {
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
        }
        self.assertEqual(set(module.ACTIONS), expected)

    def test_help_exits_zero_and_documents_python_entrypoint(self) -> None:
        code, out, _ = self.call(["help"])
        self.assertEqual(code, 0)
        self.assertIn("python -m atri_x doctor", out)
        self.assertIn("deploy/backup.py", out)

    def test_unknown_action_exits_two(self) -> None:
        code, _, err = self.call(["definitely-not-an-action"])
        self.assertEqual(code, 2)
        self.assertIn("未知动作", err)

    def test_version_reads_deploy_version(self) -> None:
        (self.deploy / "VERSION").write_text("1.2.3\n", encoding="utf-8", newline="\n")
        code, out, _ = self.call(["version"])
        self.assertEqual(code, 0)
        self.assertIn("1.2.3", out)

    def test_unalias_up_and_ps(self) -> None:
        module = self.load_module()
        args = module.build_parser().parse_args(["up"])
        self.assertEqual(module.Context(args).action, "start")
        args = module.build_parser().parse_args(["ps"])
        self.assertEqual(module.Context(args).action, "status")

    def test_exec_without_command_is_usage_error(self) -> None:
        code, _, err = self.call(["exec"])
        self.assertNotEqual(code, 0)
        self.assertIn("用法", err)

    def test_restore_verify_without_archive_is_usage_error(self) -> None:
        code, _, err = self.call(["restore-verify"])
        self.assertNotEqual(code, 0)
        self.assertIn("用法", err)

    def test_missing_env_file_is_reported_with_next_step(self) -> None:
        code, _, err = self.call(["doctor"])
        self.assertNotEqual(code, 0)
        self.assertIn("init", err)

    def test_ops_endpoints_unreachable_are_reported(self) -> None:
        code, _, err = self.call(["tasks"])
        self.assertNotEqual(code, 0)
        self.assertIn("/tasks 不可达", err)

    def test_json_flag_produces_parseable_status(self) -> None:
        code, out, _ = self.call(["status", "--json"])
        self.assertEqual(code, 1)  # 没有容器，状态一定不是 ready
        payload = json.loads(out)
        for key in (
            "version",
            "compose_file",
            "env_file",
            "data_dir",
            "docker",
            "compose",
            "container",
            "task_registered",
            "live",
            "ready",
            "healthz",
            "ops_token_configured",
        ):
            self.assertIn(key, payload)

    def test_bootstrap_shells_are_thin(self) -> None:
        """``.cmd`` / ``.ps1`` 必须只是极薄引导壳，实现只在 atri_x.py 里。"""
        cmd = (DEPLOY_SRC / "windows" / "atri-x.cmd").read_text(encoding="utf-8")
        ps1 = (DEPLOY_SRC / "windows" / "atri-x.ps1").read_text(encoding="utf-8")
        self.assertLessEqual(len(cmd.splitlines()), 20, "atri-x.cmd 太厚，实现应只在 atri_x.py")
        self.assertLessEqual(len(ps1.splitlines()), 40, "atri-x.ps1 太厚，实现应只在 atri_x.py")
        self.assertIn("atri_x.py", cmd)
        self.assertIn("atri_x.py", ps1)
        # 上游那套 Git Bash 路径转换不应该再出现在引导壳里
        for forbidden in ("cygpath", "Convert-ToBashPath", "Invoke-BashScript"):
            self.assertNotIn(forbidden, cmd)
            self.assertNotIn(forbidden, ps1)


if __name__ == "__main__":
    unittest.main()
