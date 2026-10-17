"""``atri_x.platform.builtin`` 的单元测试（对应 Go ``internal/platform/builtin``）。

覆盖三层：

1. **Go ``builtin_test.go`` 的四个用例**逐条照抄（禁用/启用 qqonebot、
   ``ModeCLIOnly`` / ``ModeService`` 的主平台与运行时装列表）；
2. **并行移植期的缺件语义**：平台包缺失、或包在但没有装配入口时，跳过而不是
   整体炸（用 ``sys.modules`` 注入假模块 / ``None`` 占位做确定性模拟）；
3. **真实兄弟模块的接线**：用真的 ``atri_x.platform.telegram`` 与
   ``atri_x.platform.qq_onebot`` 各装配一次，验证参数顺序与 ``Enabled()`` 过滤。

Go 测试传 ``nil`` store / chatHistory / logger，这里同样：
:func:`atri_x.platform.builtin.new` 不触碰它们，只做透传。
"""

from __future__ import annotations

import logging
import os
import sys
import types
import unittest
from unittest import mock

from atri_x.errors import InvalidError
from atri_x.platform.builtin import (
    MODE_CLI_ONLY,
    MODE_FULL,
    MODE_SERVICE,
    Bundle,
    Mode,
    ModeCLIOnly,
    ModeFull,
    ModeService,
    New,
    Options,
    new,
    platform_module_names,
)


class FakeAdapter:
    """最小适配器替身：Go 侧只用到 ``Name()`` 与 ``Enabled()``。"""

    def __init__(self, name: str, enabled: bool = True, **kwargs) -> None:
        self._name = name
        self._enabled = enabled
        self.kwargs = kwargs

    def name(self) -> str:
        return self._name

    def enabled(self) -> bool:
        return self._enabled


class FakeConfig:
    """``atri_x.config.AppConfig`` 的最小替身（只带 builtin 用到的字段）。"""

    def __init__(
        self,
        platform: dict | None = None,
        config_path: str = "/cfg/app.toml",
        superadmins: dict | None = None,
        prefixes: list | None = None,
        sandbox_root: str = "/sandbox",
    ) -> None:
        self.platform = dict(platform or {})
        self.config_path = config_path
        self.security = types.SimpleNamespace(superadmins=dict(superadmins or {}))
        self.commands = types.SimpleNamespace(prefixes=list(prefixes or ["/"]))
        self.sandbox = types.SimpleNamespace(root=sandbox_root)
        self.platform_files = types.SimpleNamespace(
            max_receive_file_bytes=1024, download_timeout_secs=7
        )


class RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def warn(self, message: str, **fields) -> None:
        self.records.append((message, fields))


# ---------------------------------------------------------------------------
# 假平台模块
# ---------------------------------------------------------------------------


def fake_platform_module(module_name: str, adapter_name: str, enabled, calls: list):
    """造一个只导出 ``new_from_platform_config`` 的假平台包。

    ``enabled`` 给 ``None`` 时按真实适配器的行为**从原始小节读 ``enabled`` 键**；
    给 ``True``/``False`` 时固定返回值。
    """
    module = types.ModuleType(module_name)

    def new_from_platform_config(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raw = args[0] if args else None
        if enabled is None:
            flag = bool((raw or {}).get("enabled", False))
        else:
            flag = bool(enabled)
        return FakeAdapter(adapter_name, flag)

    module.new_from_platform_config = new_from_platform_config
    return module


def fake_cli_module(
    enabled: bool = True,
    server_enabled: bool = False,
    calls: list | None = None,
    with_remote_server: bool = True,
):
    """造一个假 ``atri_x.platform.cli``（只提供 builtin 真正会消费的入口）。"""
    calls = [] if calls is None else calls
    module = types.ModuleType("atri_x.platform.cli")
    module.PLATFORM_NAME = "cli"

    def new_adapter(**kwargs):
        calls.append(("new_adapter", kwargs))
        return FakeAdapter("cli")

    def new_config_from_platform_config(raw):
        calls.append(("new_config_from_platform_config", raw))
        server = types.SimpleNamespace(enabled=server_enabled)
        return types.SimpleNamespace(enabled=enabled, server=server)

    module.new_adapter = new_adapter
    module.new_config_from_platform_config = new_config_from_platform_config
    if with_remote_server:
        module.RemoteServerOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)

        def new_remote_server(opts):
            calls.append(("new_remote_server", opts))
            return FakeAdapter("cli-remote")

        module.new_remote_server = new_remote_server
    return module


class BuiltinTestBase(unittest.TestCase):
    """公共装置：默认把三个未交付/易变的平台包替换成假模块。"""

    def setUp(self):
        self.calls: list = []
        self.modules = {
            "atri_x.platform.cli": fake_cli_module(calls=self.calls),
            "atri_x.platform.qqofficial": fake_platform_module(
                "atri_x.platform.qqofficial", "qqofficial", None, self.calls
            ),
            "atri_x.platform.qq_onebot": fake_platform_module(
                "atri_x.platform.qq_onebot", "qqonebot", None, self.calls
            ),
            "atri_x.platform.telegram": fake_platform_module(
                "atri_x.platform.telegram", "telegram", None, self.calls
            ),
        }
        patcher = mock.patch.dict(sys.modules, self.modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self, bundle: Bundle) -> list[str]:
        return [adapter.name() for adapter in bundle.runtimes]


# ---------------------------------------------------------------------------
# 1) Go builtin_test.go 的用例
# ---------------------------------------------------------------------------


class TestGoParityCases(BuiltinTestBase):
    def test_new_registers_enabled_qqonebot_only(self):
        """``TestNewRegistersEnabledQQOneBotOnly``。"""
        cfg = FakeConfig(platform={"qqonebot": {"enabled": False}})
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(len(bundle.runtimes), 1, self.names(bundle))

        cfg.platform["qqonebot"]["enabled"] = True
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(len(bundle.runtimes), 2, self.names(bundle))
        self.assertEqual(bundle.runtimes[1].name(), "qqonebot")

    def test_new_modes(self):
        """``TestNewModes``：cli-only 与 service 的主平台/运行时差异。"""
        cfg = FakeConfig(platform={"qqonebot": {"enabled": True}})

        cli_only = new(Options(mode=MODE_CLI_ONLY), cfg, None, None, None)
        self.assertEqual(cli_only.primary.name(), "cli")
        self.assertEqual(self.names(cli_only), ["cli"])

        service = new(Options(mode=MODE_SERVICE), cfg, None, None, None)
        self.assertEqual(service.primary.name(), "service")
        self.assertEqual(self.names(service), ["qqonebot"])

    def test_qq_official_and_telegram_are_appended_in_go_order(self):
        """Go 的装配顺序是 qqofficial → qqonebot → telegram。"""
        cfg = FakeConfig(
            platform={
                "qqofficial": {"enabled": True},
                "qqonebot": {"enabled": True},
                "telegram": {"enabled": True},
            }
        )
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli", "qqofficial", "qqonebot", "telegram"])

    def test_disabled_platforms_are_filtered_by_enabled(self):
        cfg = FakeConfig(
            platform={
                "qqofficial": {"enabled": False},
                "qqonebot": {"enabled": False},
                "telegram": {"enabled": False},
            }
        )
        with mock.patch.dict(
            sys.modules,
            {
                "atri_x.platform.qqofficial": fake_platform_module(
                    "atri_x.platform.qqofficial", "qqofficial", False, self.calls
                ),
                "atri_x.platform.qq_onebot": fake_platform_module(
                    "atri_x.platform.qq_onebot", "qqonebot", False, self.calls
                ),
                "atri_x.platform.telegram": fake_platform_module(
                    "atri_x.platform.telegram", "telegram", False, self.calls
                ),
            },
        ):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli"])

    def test_none_config_returns_primary_only(self):
        bundle = new(Options(mode=MODE_FULL), None, None, None, None)
        self.assertEqual(bundle.primary.name(), "cli")
        self.assertEqual(self.names(bundle), ["cli"])
        self.assertEqual(bundle.skipped, [])

    def test_empty_mode_is_normalized_to_full(self):
        cfg = FakeConfig(platform={"telegram": {"enabled": True}})
        bundle = new(Options(mode=Mode("")), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli", "telegram"])

    def test_primary_also_appears_in_runtimes_for_cli(self):
        bundle = new(Options(), FakeConfig(), None, None, None)
        self.assertIs(bundle.runtimes[0], bundle.primary)

    def test_platform_key_absent_vs_present_but_none(self):
        """Go 判定的是键存在性：``{"telegram": None}`` 仍会去调装配入口。"""
        module = fake_platform_module(
            "atri_x.platform.telegram", "telegram", True, self.calls
        )
        cfg = FakeConfig(platform={"telegram": None})
        with mock.patch.dict(sys.modules, {"atri_x.platform.telegram": module}):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli", "telegram"])
        self.assertEqual(bundle.skipped, [])
        # 装配入口确实被调用了（键在就调，值是不是空表不影响）。
        self.assertEqual(len([call for call in self.calls if isinstance(call, dict)]), 1)

    def test_platform_key_truly_absent_skips_factory(self):
        cfg = FakeConfig(platform={})
        new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual([call for call in self.calls if isinstance(call, dict)], [])


# ---------------------------------------------------------------------------
# 2) 装配参数（逐参对照 Go 的调用）
# ---------------------------------------------------------------------------


class TestArgumentShape(BuiltinTestBase):
    def test_qq_platform_arguments(self):
        cfg = FakeConfig(
            platform={"qqofficial": {"enabled": True}},
            config_path="/etc/atri-x/app.toml",
            superadmins={"qqofficial": ["u1"]},
            prefixes=["/", "!"],
            sandbox_root="/data/sandbox",
        )
        new(Options(mode=MODE_FULL), cfg, "STORE", "HISTORY", "LOGGER")
        call = self.calls[-1]
        self.assertEqual(
            list(call["args"]),
            [
                {"enabled": True},
                "STORE",
                "HISTORY",
                "LOGGER",
                ["u1"],
                ["/", "!"],
                "/etc/atri-x",
                os.path.join("/data/sandbox", "platform", "qqofficial"),
                1024,
                7,
            ],
        )

    def test_telegram_arguments_have_no_attachment_dir(self):
        """Go 的 telegram 装配入口只有 7 个参数（没有 attachmentDir / 限额）。"""
        cfg = FakeConfig(
            platform={"telegram": {"enabled": True}},
            config_path="/etc/atri-x/app.toml",
            superadmins={"telegram": ["t1"]},
            prefixes=["/"],
        )
        new(Options(mode=MODE_FULL), cfg, "STORE", "HISTORY", "LOGGER")
        call = self.calls[-1]
        self.assertEqual(
            list(call["args"]),
            [
                {"enabled": True},
                "STORE",
                "HISTORY",
                "LOGGER",
                ["t1"],
                ["/"],
                "/etc/atri-x",
            ],
        )

    def test_missing_config_dir_falls_back_to_dot_like_go(self):
        """Go ``filepath.Dir("")`` 是 ``"."``，Python ``os.path.dirname("")`` 不是。"""
        cfg = FakeConfig(platform={"telegram": {"enabled": True}}, config_path="")
        new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.calls[-1]["args"][6], ".")


# ---------------------------------------------------------------------------
# 3) 并行移植期的缺件语义
# ---------------------------------------------------------------------------


class TestMissingModules(BuiltinTestBase):
    def test_missing_platform_module_is_skipped(self):
        cfg = FakeConfig(platform={"qqofficial": {"enabled": True}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.qqofficial": None}):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli"])
        self.assertEqual(len(bundle.skipped), 1)
        self.assertIn("qqofficial", bundle.skipped[0])
        self.assertIn("unavailable", bundle.skipped[0])

    def test_module_without_entry_point_is_skipped(self):
        cfg = FakeConfig(platform={"qqonebot": {"enabled": True}})
        empty = types.ModuleType("atri_x.platform.qq_onebot")
        with mock.patch.dict(sys.modules, {"atri_x.platform.qq_onebot": empty}):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli"])
        self.assertIn("does not export", bundle.skipped[0])

    def test_all_platforms_missing_still_returns_cli_primary(self):
        cfg = FakeConfig(
            platform={
                "qqofficial": {"enabled": True},
                "qqonebot": {"enabled": True},
                "telegram": {"enabled": True},
            }
        )
        with mock.patch.dict(
            sys.modules,
            {
                "atri_x.platform.qqofficial": None,
                "atri_x.platform.qq_onebot": None,
                "atri_x.platform.telegram": None,
            },
        ):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli"])
        self.assertEqual(len(bundle.skipped), 3)

    def test_missing_cli_module_falls_back_to_headless_primary(self):
        cfg = FakeConfig()
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": None}):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(bundle.primary.name(), "service")
        self.assertEqual(bundle.runtimes, [])
        self.assertIn("cli", bundle.skipped[0])

    def test_missing_headless_in_service_mode_raises(self):
        with mock.patch.dict(sys.modules, {"atri_x.platform.headless": None}):
            with self.assertRaises(InvalidError) as caught:
                new(Options(mode=MODE_SERVICE), FakeConfig(), None, None, None)
        self.assertIn("atri_x.platform.headless", str(caught.exception))

    def test_both_primary_modules_missing_raises(self):
        with mock.patch.dict(
            sys.modules,
            {"atri_x.platform.cli": None, "atri_x.platform.headless": None},
        ):
            with self.assertRaises(InvalidError):
                new(Options(mode=MODE_FULL), FakeConfig(), None, None, None)

    def test_skipped_reason_is_logged_as_warning(self):
        logger = RecordingLogger()
        cfg = FakeConfig(platform={"telegram": {"enabled": True}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.telegram": None}):
            new(Options(mode=MODE_FULL), cfg, None, None, logger)
        self.assertEqual(logger.records[0][0], "platform skipped")
        self.assertEqual(logger.records[0][1]["platform"], "telegram")

    def test_platform_module_names_listing(self):
        self.assertEqual(
            platform_module_names(),
            (
                "atri_x.platform.qqofficial",
                "atri_x.platform.qq_onebot",
                "atri_x.platform.telegram",
            ),
        )


# ---------------------------------------------------------------------------
# 4) service 模式下的 cli 远程服务端
# ---------------------------------------------------------------------------


class TestServiceRemoteServer(BuiltinTestBase):
    def test_remote_server_added_when_enabled(self):
        module = fake_cli_module(enabled=True, server_enabled=True, calls=self.calls)
        cfg = FakeConfig(
            platform={"cli": {"server": {"enabled": True}}},
            config_path="/etc/atri-x/app.toml",
            superadmins={"cli": ["local"]},
        )
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": module}):
            bundle = new(Options(mode=MODE_SERVICE), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli-remote"])
        self.assertEqual(bundle.primary.name(), "service")
        options = self.calls[-1][1]
        self.assertEqual(options.config_dir, "/etc/atri-x")
        self.assertEqual(options.superadmins, ["local"])

    def test_remote_server_skipped_when_server_disabled(self):
        module = fake_cli_module(enabled=True, server_enabled=False, calls=self.calls)
        cfg = FakeConfig(platform={"cli": {}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": module}):
            bundle = new(Options(mode=MODE_SERVICE), cfg, None, None, None)
        self.assertEqual(bundle.runtimes, [])

    def test_remote_server_not_built_outside_service_mode(self):
        module = fake_cli_module(enabled=True, server_enabled=True, calls=self.calls)
        cfg = FakeConfig(platform={"cli": {}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": module}):
            bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(self.names(bundle), ["cli"])
        self.assertFalse([call for call in self.calls if call[0] == "new_remote_server"])

    def test_remote_server_without_factory_is_skipped(self):
        module = fake_cli_module(
            enabled=True, server_enabled=True, calls=self.calls, with_remote_server=False
        )
        cfg = FakeConfig(platform={"cli": {}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": module}):
            bundle = new(Options(mode=MODE_SERVICE), cfg, None, None, None)
        self.assertEqual(bundle.runtimes, [])
        self.assertIn("cli.server", bundle.skipped[0])

    def test_missing_cli_module_in_service_mode_is_skipped(self):
        cfg = FakeConfig(platform={"cli": {"server": {"enabled": True}}})
        with mock.patch.dict(sys.modules, {"atri_x.platform.cli": None}):
            bundle = new(Options(mode=MODE_SERVICE), cfg, None, None, None)
        self.assertEqual(bundle.primary.name(), "service")
        self.assertIn("cli.server", bundle.skipped[0])


# ---------------------------------------------------------------------------
# 5) 真实兄弟模块接线（不打桩）
# ---------------------------------------------------------------------------


class TestRealSiblingModules(unittest.TestCase):
    """用真的 telegram / qq_onebot 包各装配一次，验证签名与 ``Enabled()`` 过滤。"""

    def test_real_telegram_adapter(self):
        cfg = FakeConfig(platform={"telegram": {"enabled": True, "bot_token": "123:abc"}})
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual([a.name() for a in bundle.runtimes], ["cli", "telegram"])
        self.assertEqual(bundle.skipped, [])

    def test_real_telegram_disabled(self):
        cfg = FakeConfig(platform={"telegram": {"enabled": False}})
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual([a.name() for a in bundle.runtimes], ["cli"])

    def test_real_qq_onebot_adapter(self):
        cfg = FakeConfig(platform={"qqonebot": {"enabled": True, "access_token": "tok"}})
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual([a.name() for a in bundle.runtimes], ["cli", "qqonebot"])

    def test_real_qq_official_adapter(self):
        cfg = FakeConfig(
            platform={
                "qqofficial": {
                    "enabled": True,
                    "app_id": "12345",
                    "client_secret": "sec",
                }
            }
        )
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual([a.name() for a in bundle.runtimes], ["cli", "qqofficial"])
        self.assertEqual(bundle.skipped, [])

    def test_real_all_three_platforms_at_once(self):
        cfg = FakeConfig(
            platform={
                "qqofficial": {
                    "enabled": True,
                    "app_id": "12345",
                    "client_secret": "sec",
                },
                "qqonebot": {"enabled": True, "access_token": "tok"},
                "telegram": {"enabled": True, "bot_token": "123:abc"},
            }
        )
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(
            [a.name() for a in bundle.runtimes],
            ["cli", "qqofficial", "qqonebot", "telegram"],
        )
        self.assertEqual(bundle.skipped, [])

    def test_real_cli_module_primary(self):
        cfg = FakeConfig()
        bundle = new(Options(mode=MODE_FULL), cfg, None, None, None)
        self.assertEqual(bundle.primary.name(), "cli")
        self.assertEqual(bundle.skipped, [])


class TestTypesAndAliases(unittest.TestCase):
    def test_mode_constants(self):
        self.assertEqual(str(ModeFull), "full")
        self.assertEqual(str(ModeCLIOnly), "cli")
        self.assertEqual(str(ModeService), "service")
        self.assertIs(ModeFull, MODE_FULL)
        self.assertIs(ModeCLIOnly, MODE_CLI_ONLY)
        self.assertIs(ModeService, MODE_SERVICE)
        self.assertIsInstance(ModeFull, str)

    def test_pascal_aliases(self):
        self.assertIs(New, new)

    def test_bundle_defaults(self):
        bundle = Bundle()
        self.assertIsNone(bundle.primary)
        self.assertEqual(bundle.runtimes, [])
        self.assertEqual(bundle.skipped, [])

    def test_logger_warning_level_name_matches_repo_logger(self):
        """仓库 logger 的告警方法叫 ``warn``；这里确认鸭子类型认它。"""
        logger = logging.getLogger("test")
        self.assertTrue(callable(getattr(logger, "warning", None)))


if __name__ == "__main__":
    unittest.main()
