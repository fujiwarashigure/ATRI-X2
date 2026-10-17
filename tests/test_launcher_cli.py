"""``atri_x.launcher.cli`` 的单元测试（对应 Go ``internal/launcher/cli_test.go``）。

覆盖：

* ``TestParseArgs`` 的**全部 17 条**表驱动用例（逐条照抄 ``cli_test.go``）；
* ``TestWriteCompletionShells``（7 个 shell 的输出都要提到 ``atri-x``）；
* ``TestParseDoctorArgs``；
* Python 版新增项：``--health-probe`` 解析、``health_probe_address`` 优先级、
  ``USAGE_TEXT`` 内容、``detect_completion_shell`` 的环境判定顺序。
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from atri_x import launcher
from atri_x.errors import InvalidError
from atri_x.launcher.cli import (
    COMMAND_COMPLETION,
    COMMAND_CONFIG_CHECK,
    COMMAND_DOCTOR,
    COMMAND_RUN,
    MODE_AUTO,
    MODE_CLI_ONLY,
    MODE_FULL,
    MODE_SERVICE,
    detect_completion_shell,
    health_probe_address,
    parse_args,
    supported_completion_shell,
    write_completion,
    write_usage,
)

#: 直接照抄 ``cli_test.go`` 的 ``tests`` 表（字段名换成 Python 侧拼写）。
PARSE_CASES: list[dict[str, object]] = [
    {"name": "default auto", "args": [], "mode": MODE_AUTO, "command": COMMAND_RUN},
    {"name": "run", "args": ["run"], "mode": MODE_FULL, "command": COMMAND_RUN},
    {"name": "cli", "args": ["cli"], "mode": MODE_CLI_ONLY, "command": COMMAND_RUN},
    {
        "name": "service run",
        "args": ["service", "run"],
        "mode": MODE_SERVICE,
        "command": COMMAND_RUN,
    },
    {
        "name": "completion fish",
        "args": ["completion", "fish"],
        "mode": MODE_AUTO,
        "command": COMMAND_COMPLETION,
    },
    {
        "name": "completion bash",
        "args": ["completion", "bash"],
        "mode": MODE_AUTO,
        "command": COMMAND_COMPLETION,
    },
    {
        "name": "completion nushell",
        "args": ["completion", "nushell"],
        "mode": MODE_AUTO,
        "command": COMMAND_COMPLETION,
    },
    {
        "name": "go run separator",
        "args": ["--", "completion", "fish"],
        "mode": MODE_AUTO,
        "command": COMMAND_COMPLETION,
    },
    {
        "name": "config before command",
        "args": ["--config", "custom/app.toml", "run"],
        "mode": MODE_FULL,
        "command": COMMAND_RUN,
        "config": "custom/app.toml",
    },
    {
        "name": "config after command",
        "args": ["cli", "--config=custom/app.toml"],
        "mode": MODE_CLI_ONLY,
        "command": COMMAND_RUN,
        "config": "custom/app.toml",
    },
    {
        "name": "client before command",
        "args": ["-c", "local", "cli"],
        "mode": MODE_CLI_ONLY,
        "command": COMMAND_RUN,
        "client": "local",
    },
    {
        "name": "client equals",
        "args": ["--client=windows"],
        "mode": MODE_AUTO,
        "command": COMMAND_RUN,
        "client": "windows",
    },
    {"name": "name unsupported", "args": ["--name", "local"], "error": True},
    {"name": "unknown command", "args": ["wat"], "error": True},
    {"name": "unknown service action", "args": ["service", "stop"], "error": True},
    {"name": "unknown completion shell", "args": ["completion", "csh"], "error": True},
    {"name": "missing config", "args": ["--config"], "error": True},
]


class TestParseArgs(unittest.TestCase):
    """``TestParseArgs``：逐条对照 Go 的表驱动用例。"""

    def test_table(self):
        for case in PARSE_CASES:
            with self.subTest(case=case["name"]):
                args = list(case["args"])  # type: ignore[arg-type]
                if case.get("error"):
                    with self.assertRaises(InvalidError):
                        parse_args(args)
                    continue
                opts = parse_args(args)
                self.assertEqual(str(opts.mode), str(case["mode"]))
                self.assertEqual(str(opts.command), str(case["command"]))
                self.assertEqual(opts.config_path, case.get("config", ""))
                self.assertEqual(opts.client_name, case.get("client", ""))

    def test_extra_flags(self):
        """Go 表里没写、但源码里存在的分支。"""
        self.assertTrue(parse_args(["--help"]).help)
        self.assertTrue(parse_args(["-h"]).help)
        self.assertTrue(parse_args(["--version"]).version)
        # 单独一个 ``--`` 只是被跳过，不影响默认模式。
        self.assertEqual(str(parse_args(["--"]).mode), MODE_AUTO)
        # 单横线 ``-config`` 合法（Go 就是这么写的）……
        self.assertEqual(parse_args(["-config", "a.toml"]).config_path, "a.toml")
        # ……但 ``-config=x`` 不合法（Go 只对 ``--config=`` 做前缀匹配）。
        with self.assertRaises(InvalidError):
            parse_args(["-config=a.toml"])
        # ``--help`` / ``--version`` 提前返回，忽略未知位置参数。
        self.assertTrue(parse_args(["--help", "wat"]).help)
        self.assertTrue(parse_args(["--version", "wat"]).version)
        self.assertEqual(parse_args(["config", "check"]).command, COMMAND_CONFIG_CHECK)
        self.assertEqual(parse_args(["doctor"]).command, COMMAND_DOCTOR)

    def test_help_ignores_bad_positionals(self):
        opts = parse_args(["--help"])
        self.assertTrue(opts.help)
        self.assertFalse(opts.version)


class TestParseDoctorArgs(unittest.TestCase):
    """``TestParseDoctorArgs``。"""

    def test_all_doctor_flags(self):
        opts = parse_args(
            [
                "doctor",
                "--e2e",
                "--json",
                "--no-model",
                "--require-platform",
                "--config",
                "custom/app.toml",
            ]
        )
        self.assertEqual(opts.command, COMMAND_DOCTOR)
        self.assertTrue(opts.doctor_e2e)
        self.assertTrue(opts.doctor_json)
        self.assertTrue(opts.doctor_no_model)
        self.assertTrue(opts.doctor_require_platform)
        self.assertEqual(opts.config_path, "custom/app.toml")


class TestCompletion(unittest.TestCase):
    """``TestWriteCompletionShells`` + 补全输出契约。"""

    SHELLS = ["bash", "zsh", "fish", "nu", "nushell", "powershell", "pwsh"]

    def test_supported_shells(self):
        for shell in self.SHELLS + ["auto"]:
            self.assertTrue(supported_completion_shell(shell), shell)
        for shell in ("csh", "cmd", "sh", ""):
            self.assertFalse(supported_completion_shell(shell), shell)

    def test_write_completion_mentions_atri_x(self):
        for shell in self.SHELLS:
            with self.subTest(shell=shell):
                buffer = io.StringIO()
                write_completion(buffer, shell)
                self.assertIn("atri-x", buffer.getvalue())

    def test_write_completion_rejects_unknown_shell(self):
        with self.assertRaises(InvalidError) as caught:
            write_completion(io.StringIO(), "csh")
        self.assertIn("cannot detect shell", str(caught.exception))

    def test_auto_falls_back_to_error_without_shell_env(self):
        with mock.patch.dict(os.environ, {"SHELL": "", "PSModulePath": ""}, clear=True):
            with self.assertRaises(InvalidError):
                write_completion(io.StringIO(), "auto")

    def test_detect_completion_shell_order(self):
        cases = [
            ({"SHELL": "/usr/bin/fish"}, "fish"),
            ({"SHELL": "/usr/bin/nushell"}, "nushell"),
            ({"SHELL": "/bin/zsh"}, "zsh"),
            ({"SHELL": "/bin/bash"}, "bash"),
            ({"SHELL": "", "PSModulePath": r"C:\Windows\System32\WindowsPowerShell"}, "powershell"),
            ({"SHELL": "/bin/sh"}, ""),
        ]
        for env, want in cases:
            with self.subTest(env=env):
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(detect_completion_shell(), want)

    def test_nu_wins_over_zsh(self):
        """判定顺序与 Go 一致：``nu`` 在 ``zsh``/``bash`` 之前。"""
        with mock.patch.dict(os.environ, {"SHELL": "/opt/nushell/bin/zsh"}, clear=True):
            self.assertEqual(detect_completion_shell(), "nushell")


class TestUsage(unittest.TestCase):
    def test_usage_lists_every_documented_command(self):
        buffer = io.StringIO()
        write_usage(buffer)
        text = buffer.getvalue()
        for token in (
            "atri-x run",
            "atri-x cli",
            "atri-x service run",
            "atri-x config check",
            "atri-x doctor",
            "atri-x completion",
            "--help",
            "--version",
        ):
            self.assertIn(token, text)
        # 用法文本必须是纯 ASCII：GBK 控制台/管道下不能因为编码炸掉。
        buffer.getvalue().encode("ascii")

    def test_usage_python_alias_identical(self):
        self.assertIs(launcher.WriteUsage, launcher.write_usage)
        self.assertIs(launcher.ParseArgs, launcher.parse_args)
        self.assertIs(launcher.Options, launcher.cli.Options)


class TestHealthProbeOptions(unittest.TestCase):
    """Python 版新增的 ``--health-probe`` 解析（Go 上游无对应物）。"""

    def test_flag_alone_is_not_a_run(self):
        opts = parse_args(["--health-probe"])
        self.assertTrue(opts.health_probe)
        self.assertEqual(opts.health_probe_url, "")
        self.assertEqual(str(opts.command), str(COMMAND_RUN))
        self.assertEqual(str(opts.mode), MODE_AUTO)

    def test_flag_with_value(self):
        opts = parse_args(["--health-probe=http://127.0.0.1:9/live"])
        self.assertTrue(opts.health_probe)
        self.assertEqual(opts.health_probe_url, "http://127.0.0.1:9/live")

    def test_flag_does_not_break_subcommands(self):
        opts = parse_args(["doctor", "--health-probe"])
        self.assertTrue(opts.health_probe)
        self.assertEqual(opts.command, COMMAND_DOCTOR)

    def test_address_priority(self):
        opts = parse_args(["--health-probe=10.0.0.1:1234"])
        self.assertEqual(health_probe_address(opts, {"ATRIX_HEALTH_ADDR": "1.2.3.4:5"}), "10.0.0.1:1234")
        plain = parse_args(["--health-probe"])
        self.assertEqual(health_probe_address(plain, {"ATRIX_HEALTH_ADDR": "1.2.3.4:5"}), "1.2.3.4:5")
        self.assertEqual(health_probe_address(plain, {}), "127.0.0.1:32171")
        self.assertEqual(health_probe_address(plain, {"ATRIX_HEALTH_ADDR": "  "}), "127.0.0.1:32171")

    def test_path_only_value_is_not_an_address(self):
        """``--health-probe=/ready`` 是路径：地址回落到环境变量 / 默认值。"""
        opts = parse_args(["--health-probe=/ready"])
        self.assertEqual(health_probe_address(opts, {}), "127.0.0.1:32171")
        self.assertEqual(
            health_probe_address(opts, {"ATRIX_HEALTH_ADDR": "10.0.0.9:9999"}),
            "10.0.0.9:9999",
        )
        self.assertEqual(
            launcher.health_probe_default_address({"ATRIX_HEALTH_ADDR": "10.0.0.9:9999"}),
            "10.0.0.9:9999",
        )
        self.assertEqual(launcher.health_probe_default_address({}), "127.0.0.1:32171")

    def test_address_does_not_read_config(self):
        """``health_probe_address`` 只看 flag/环境变量，签名里没有 config。"""
        opts = parse_args(["--health-probe", "--config", "does/not/exist.toml"])
        self.assertEqual(health_probe_address(opts, {}), "127.0.0.1:32171")


if __name__ == "__main__":
    unittest.main()
