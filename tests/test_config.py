"""``atri_x.config`` 顶层行为测试（app.toml / services.toml / 严格解码 / 校验）。

对应上游 ``internal/config/config_test.go``、``metadata_store_test.go``、
``ops_defaults_test.go``、``dialect_alias_test.go`` 里的核心断言。

临时目录用仓库根下的 ``tmp_cfg_test/``（本机沙箱下 ``tempfile.TemporaryDirectory()``
的清理会抛 PermissionError），``tearDown`` 里 ``shutil.rmtree(ignore_errors=True)``。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import unittest
from unittest import mock

from atri_x.config import AppConfig
from atri_x.config import ModelSelection
from atri_x.config import assets as A
from atri_x.config import inspect as I
from atri_x.config import loader
from atri_x.config.app import default, default_app_config
from atri_x.config.app_sections import (
    ConfigDecodeError,
    ContextConfig,
    MissingConfigFileError,
    UnknownConfigKeysError,
    key_path,
)
from atri_x.config.services import (
    APIProtocol,
    AudioSupport,
    VisionSupport,
    normalize_reasoning_effort,
    parse_api_protocol,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tmp_cfg_test"


def _cleanup_case_root(class_name: str) -> None:
    """收掉本测试类的目录与（空掉的）根目录。

    ``tmp_cfg_test/`` 目前并不在 ``.gitignore`` 里（``tests/_tmp*/`` 才是被忽略的模式），
    所以测试结束时必须自己扫干净，别在仓库根留下空目录。
    """
    shutil.rmtree(ROOT / class_name, ignore_errors=True)
    try:
        ROOT.rmdir()
    except OSError:
        pass

PROVIDERS_TOML = """
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "ATRIX_TEST_UNUSED_KEY"

[model_metadata]
default_context_window = 128000
"""

STATE_TOML = """
[mode_models.work]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.chat]
provider = "deepseek"
model = "deepseek-chat"
"""


class ConfigCase(unittest.TestCase):
    """每个用例一个独立临时目录（位于仓库根 ``tmp_cfg_test/`` 下）。"""

    def setUp(self) -> None:
        self.dir = ROOT / type(self).__name__ / self._testMethodName
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        _cleanup_case_root(type(self).__name__)

    # -- 夹具 ---------------------------------------------------------------
    def write(self, name: str, body: str) -> pathlib.Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        return path

    def write_minimal(self, app_body: str = "") -> pathlib.Path:
        """写入 Go 测试同款「最小可加载配置集」，返回 app.toml 路径。

        app.toml 不写 ``[config_files]`` 时按旧部署默认读 ``providers.toml``；
        ``mode_models`` 必须存在（``validate_mode_models`` 要求 work/chat 都有值）。
        """
        app = self.write("app.toml", app_body)
        self.write("providers.toml", PROVIDERS_TOML)
        self.write("state.toml", STATE_TOML)
        return app


class TestDefaultAssetsEndToEnd(ConfigCase):
    """最强端到端自检：加载**真实默认资产**必须完整且零未知键。"""

    def setUp(self) -> None:
        super().setUp()
        A.ensure_platform_defaults(self.dir)
        self.cfg = loader.load_from_dir(self.dir)

    def test_no_unknown_keys_in_default_app_toml(self) -> None:
        self.assertEqual(I.unknown_app_config_keys(self.dir / "app.toml"), [])

    def test_no_degradation_warnings(self) -> None:
        self.assertEqual(self.cfg.config_warnings, [])

    def test_config_paths_are_siblings(self) -> None:
        self.assertEqual(pathlib.Path(self.cfg.config_path), self.dir / "app.toml")
        for path, name in (
            (self.cfg.services_config_path, "services.toml"),
            (self.cfg.state_config_path, "state.toml"),
            (self.cfg.atrinis_config_path, "atrinis.toml"),
            (self.cfg.tool_tags_config_path, "tool_tags.toml"),
        ):
            self.assertEqual(pathlib.Path(path), self.dir / name)
        # services.toml 存在时不得再读旧 providers.toml。
        self.assertEqual(self.cfg.providers_config_path, "")

    def test_services_supply_providers_and_image_generation(self) -> None:
        self.assertEqual(sorted(self.cfg.providers), ["deepseek", "openai"])
        self.assertEqual(self.cfg.providers["deepseek"].base_url, "https://api.deepseek.com")
        self.assertEqual(self.cfg.model_metadata.default_context_window, 256000)
        self.assertFalse(self.cfg.image_generation.enabled)
        self.assertEqual(self.cfg.image_generation.model, "gpt-image-2.5")
        self.assertEqual(self.cfg.image_generation.queue_timeout_seconds, 0)

    def test_state_supplies_model_selection(self) -> None:
        self.assertEqual(self.cfg.mode_models["work"].model, "deepseek-v4-pro")
        self.assertEqual(self.cfg.mode_models["chat"].model, "deepseek-v4-flash")
        self.assertEqual(self.cfg.session.default_mode, "work")
        self.assertEqual(self.cfg.default_model_selection().provider, "deepseek")

    def test_ops_and_llm_request_values_come_from_asset(self) -> None:
        self.assertEqual(self.cfg.ops.max_concurrent_turns, 10)
        self.assertEqual(self.cfg.ops.queue_max_size, 20)
        self.assertEqual(self.cfg.ops.queue_wait_kinds, ["turn", "tool", "hook", "compress"])
        self.assertEqual(self.cfg.ops.tool_timeout_seconds, 600)
        self.assertEqual(self.cfg.tools.max_rounds_per_turn, 10)
        self.assertEqual(self.cfg.llm_request.first_chunk_timeout_seconds, 180)
        self.assertEqual(self.cfg.llm_request.response_timeout_seconds, 0)
        self.assertEqual(self.cfg.context.overflow_mode, "reject")
        self.assertEqual(self.cfg.platform["cli"]["default_client"], "local")
        self.assertEqual(self.cfg.security.superadmins, {"cli": ["local"]})

    def test_tool_tags_and_atrinis_loaded(self) -> None:
        self.assertEqual(self.cfg.tool_tags.names(), ["agent"])
        self.assertIn("edit_file", self.cfg.tool_tags.tag("agent").tools)
        self.assertFalse(self.cfg.atrinis.enabled)
        self.assertEqual(self.cfg.atrinis.http.addr, "127.0.0.1:32170")
        self.assertIn("home", self.cfg.atrinis.tokens)

    def test_platform_dependent_paths_are_absolute(self) -> None:
        # 默认资产里留空 → 落到平台数据目录（绝对路径）。
        for path in (
            self.cfg.storage.sessions_sqlite_path,
            self.cfg.storage.chat_history_sqlite_path,
            self.cfg.sandbox.root,
        ):
            self.assertTrue(os.path.isabs(path), path)
        self.assertEqual(pathlib.Path(self.cfg.soul.path), self.dir / "SOUL.md")
        self.assertEqual(pathlib.Path(self.cfg.character_library.root), self.dir / "characters")

    def test_to_dict_round_trip_is_stable(self) -> None:
        again = AppConfig.from_dict(self.cfg.to_dict(), source="roundtrip")
        self.assertEqual(again.to_dict(), self.cfg.to_dict())

    def test_builtin_asset_drift_is_empty_right_after_install(self) -> None:
        self.assertEqual(I.builtin_asset_drift(str(self.dir)), [])


class TestStrictDecoding(ConfigCase):
    def test_unknown_key_reports_key_path_and_file(self) -> None:
        app = self.write_minimal("[ops]\nbogus_key = 1\ntool_timeout_seconds = 5\n")
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            loader.load(app)
        message = str(ctx.exception)
        self.assertIn("ops.bogus_key", message)
        self.assertIn(str(app), message)
        self.assertEqual(ctx.exception.keys, ["ops.bogus_key"])

    def test_all_unknown_keys_are_collected_and_sorted(self) -> None:
        app = self.write_minimal("[zzz]\na = 1\n\n[aaa]\nb = 2\n")
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            loader.load(app)
        self.assertEqual(ctx.exception.keys, ["aaa", "zzz"])

    def test_unknown_key_in_nested_table(self) -> None:
        app = self.write_minimal(
            '[maintenance.log_cleanup]\nschedule = "0 3 * * *"\ntypo = true\n'
        )
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            loader.load(app)
        self.assertEqual(ctx.exception.keys, ["maintenance.log_cleanup.typo"])

    def test_strict_false_ignores_unknown_keys(self) -> None:
        app = self.write_minimal("[ops]\nbogus_key = 1\n")
        cfg = loader.load(app, strict=False)
        self.assertEqual(cfg.ops.tool_timeout_seconds, 0)

    def test_type_error_reports_key_path_and_file(self) -> None:
        app = self.write_minimal('[tools]\nmax_rounds_per_turn = "ten"\n')
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        message = str(ctx.exception)
        self.assertIn("[tools].max_rounds_per_turn", message)
        self.assertIn(str(app), message)

    def test_int_field_rejects_float_and_bool(self) -> None:
        for literal in ("1.5", "true"):
            with self.subTest(literal=literal):
                app = self.write_minimal("[media]\nllm_image_max_length = " + literal + "\n")
                with self.assertRaises(ConfigDecodeError) as ctx:
                    loader.load(app)
                self.assertIn("[media].llm_image_max_length", str(ctx.exception))

    def test_list_of_tables_type_error_reports_index(self) -> None:
        app = self.write_minimal("[atrinis.delivery_disabled]\ntargets = [1]\n")
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("[atrinis.delivery_disabled].targets[0]", str(ctx.exception))

    def test_str_field_rejects_int(self) -> None:
        app = self.write_minimal("[soul]\npath = 3\n")
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("[soul].path", str(ctx.exception))

    def test_key_path_rendering(self) -> None:
        self.assertEqual(key_path(()), "<root>")
        self.assertEqual(key_path(("a",)), "[a]")
        self.assertEqual(key_path(("a", "b", "c")), "[a.b].c")
        self.assertEqual(key_path(("a", "b", "#0")), "[a].b[0]")
        self.assertEqual(key_path(("0",)), "[0]")


class TestMissingFiles(ConfigCase):
    def test_missing_app_config(self) -> None:
        with self.assertRaises(FileNotFoundError):
            loader.load(self.dir / "missing.toml")

    def test_missing_providers_config_names_key_path(self) -> None:
        app = self.write("app.toml", '[config_files]\nproviders = "missing.toml"\n')
        with self.assertRaises(MissingConfigFileError) as ctx:
            loader.load(app)
        self.assertIn("[config_files].providers", str(ctx.exception))
        self.assertIn("missing.toml", str(ctx.exception))
        self.assertIsInstance(ctx.exception, FileNotFoundError)

    def test_missing_services_degrades_to_legacy_providers(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nservices = "services.toml"\nproviders = "providers.toml"\n',
        )
        self.write("providers.toml", PROVIDERS_TOML)
        self.write("state.toml", STATE_TOML)
        cfg = loader.load(app)
        self.assertIn("deepseek", cfg.providers)
        self.assertEqual(cfg.services_config_path, "")
        self.assertTrue(cfg.config_warnings)
        self.assertIn("services.toml", cfg.config_warnings[0])

    def test_missing_services_without_legacy_file_fails(self) -> None:
        app = self.write("app.toml", '[config_files]\nservices = "services.toml"\n')
        with self.assertRaises(MissingConfigFileError) as ctx:
            loader.load(app)
        self.assertIn("[config_files].services", str(ctx.exception))

    def test_missing_state_and_tool_tags_are_tolerated(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nproviders = "providers.toml"\nstate = "state.toml"\n'
            'tool_tags = "tool_tags.toml"\n\n'
            '[mode_models.work]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n\n'
            '[mode_models.chat]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n',
        )
        self.write("providers.toml", PROVIDERS_TOML)
        cfg = loader.load(app)  # state.toml / tool_tags.toml 都不存在
        self.assertEqual(cfg.tool_tags.tags, {})
        self.assertEqual(cfg.mode_models["work"].model, "deepseek-chat")
        self.assertEqual(pathlib.Path(cfg.state_config_path), self.dir / "state.toml")

    def test_malformed_state_fails_loudly(self) -> None:
        app = self.write_minimal()
        self.write("state.toml", "this is not toml = = =\n")
        with self.assertRaises(ConfigDecodeError):
            loader.load(app)

    def test_state_must_be_a_dedicated_file(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nproviders = "providers.toml"\nstate = "tool_tags.toml"\n',
        )
        self.write("providers.toml", PROVIDERS_TOML)
        self.write("state.toml", STATE_TOML)
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("dedicated writable file", str(ctx.exception))
        self.assertIn("tool_tags", str(ctx.exception))

    def test_legacy_providers_default_is_used_without_config_files(self) -> None:
        app = self.write_minimal()
        cfg = loader.load(app)
        self.assertEqual(pathlib.Path(cfg.providers_config_path), self.dir / "providers.toml")
        self.assertEqual(cfg.model_metadata.default_context_window, 128000)
        self.assertIn("deepseek", cfg.providers)


class TestProviderSemantics(ConfigCase):
    def test_api_mode_parsing(self) -> None:
        for raw, expected in (
            ("", APIProtocol.CHAT),
            ("chat", APIProtocol.CHAT),
            ("chat_completions", APIProtocol.CHAT),
            ("response", APIProtocol.RESPONSES),
            ("responses_api", APIProtocol.RESPONSES),
            ("  RESPONSES  ", APIProtocol.RESPONSES),
        ):
            with self.subTest(raw=raw):
                protocol, ok = parse_api_protocol(raw)
                self.assertTrue(ok)
                self.assertIs(protocol, expected)
        protocol, ok = parse_api_protocol("typo")
        self.assertFalse(ok)
        self.assertIs(protocol, APIProtocol.CHAT)

    def test_invalid_api_mode_fails_at_load(self) -> None:
        app = self.write_minimal()
        self.write("providers.toml", '[providers.deepseek]\napi_mode = "typo"\n')
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("api_mode must be chat or response", str(ctx.exception))

    def test_per_model_api_mode_override(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\napi_mode = "chat"\n'
            '[providers.a.model_configs."m1"]\napi_mode = "response"\n',
        )
        cfg = loader.load(app)
        provider = cfg.providers["a"]
        self.assertIs(provider.api_protocol_for("m1"), APIProtocol.RESPONSES)
        self.assertIs(provider.api_protocol_for("other"), APIProtocol.CHAT)
        self.assertTrue(provider.uses_responses_api())
        self.assertIs(cfg.api_protocol_for("a", "m1"), APIProtocol.RESPONSES)
        self.assertIs(cfg.api_protocol_for("ghost", "m1"), APIProtocol.CHAT)

    def test_reasoning_effort_parsing(self) -> None:
        for raw, expected, ok_expected in (
            ("", "", True),
            ("   ", "", True),
            ("high", "high", True),
            ("  HIGH  ", "high", True),
            ("minimal", "minimal", True),
            ("none", "none", True),
            ("typo", "typo", False),
            ("xhigh", "xhigh", False),
        ):
            with self.subTest(raw=raw):
                value, ok = normalize_reasoning_effort(raw)
                self.assertEqual(value, expected)
                self.assertIs(ok, ok_expected)

    def test_reasoning_effort_provider_and_model_override(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\nreasoning_effort = "LOW"\n'
            '[providers.a.model_configs."m1"]\nreasoning_effort = "high"\n'
            '[providers.a.model_configs."m2"]\nreasoning_effort = ""\n',
        )
        cfg = loader.load(app)
        provider = cfg.providers["a"]
        # 模型级覆盖 Provider 级；空串的模型级配置回落到 Provider 级。
        self.assertEqual(provider.reasoning_effort_for("m1"), "high")
        self.assertEqual(provider.reasoning_effort_for("m2"), "low")
        self.assertEqual(provider.reasoning_effort_for("other"), "low")

    def test_invalid_reasoning_effort_fails_at_load(self) -> None:
        app = self.write_minimal()
        self.write("providers.toml", '[providers.a]\nreasoning_effort = "ultra"\n')
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("reasoning_effort must be", str(ctx.exception))

    def test_invalid_model_reasoning_effort_fails_at_load(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\n[providers.a.model_configs."m1"]\nreasoning_effort = "hihg"\n',
        )
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("model_configs", str(ctx.exception))
        self.assertIn("reasoning_effort", str(ctx.exception))

    def test_vision_and_audio_tristate(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            "[providers.a]\nvision = true\naudio = false\n"
            '[providers.a.model_configs."m1"]\nvision = false\n',
        )
        cfg = loader.load(app)
        self.assertIs(cfg.vision_support_for("a", "m1"), VisionSupport.UNSUPPORTED)
        self.assertIs(cfg.vision_support_for("a", "m2"), VisionSupport.SUPPORTED)
        self.assertIs(cfg.vision_support_for("ghost", "m1"), VisionSupport.UNKNOWN)
        self.assertIs(cfg.audio_support_for("a", "m2"), AudioSupport.UNSUPPORTED)
        self.assertIs(cfg.audio_support_for("ghost", "m1"), AudioSupport.UNKNOWN)

    def test_fallback_mode_shorthands(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\nfallback_mode = "on_error"\n'
            '[providers.b]\nfallback_mode = "First_Error"\n'
            '[providers.c]\nfallback_mode = "circuit"\n'
            "[providers.d]\nfallback_on_error = true\n",
        )
        cfg = loader.load(app)
        self.assertTrue(cfg.providers["a"].uses_fallback_on_error())
        self.assertTrue(cfg.providers["b"].uses_fallback_on_error())
        self.assertFalse(cfg.providers["c"].uses_fallback_on_error())
        self.assertTrue(cfg.providers["d"].uses_fallback_on_error())

    def test_api_key_env_prefers_os_env_over_dotenv(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\napi_key_env = "ATRIX_TEST_KEY_A"\n'
            '[providers.b]\napi_key_env = "ATRIX_TEST_KEY_B"\n',
        )
        self.write(".env", "ATRIX_TEST_KEY_A=from-dotenv\nATRIX_TEST_KEY_B=from-dotenv\n")
        with mock.patch.dict(os.environ, {"ATRIX_TEST_KEY_A": "from-os"}):
            cfg = loader.load(app)
        self.assertEqual(cfg.providers["a"].api_key, "from-os")
        self.assertEqual(cfg.providers["b"].api_key, "from-dotenv")

    def test_explicit_api_key_wins_and_missing_env_is_not_fatal(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            '[providers.a]\napi_key = "literal"\napi_key_env = "ATRIX_TEST_UNSET_XYZ"\n'
            '[providers.b]\napi_key_env = "ATRIX_TEST_UNSET_XYZ"\n',
        )
        cfg = loader.load(app)
        self.assertEqual(cfg.providers["a"].api_key, "literal")
        self.assertEqual(cfg.providers["b"].api_key, "")


class TestExtraPayloadPassthrough(ConfigCase):
    """``extra_payload`` / ``extra_headers`` 必须原样透传（中转站特有字段）。"""

    def test_provider_and_model_extra_payload(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            "[providers.deepseek]\n"
            'extra_payload = { provider_field = "both", thinking = { type = "disabled" } }\n'
            '[providers.deepseek.model_configs."deepseek-chat"]\n'
            "context_window = 64000\n"
            'extra_payload = { thinking = { type = "disabled" }, provider_field = "model" }\n',
        )
        cfg = loader.load(app)
        provider = cfg.providers["deepseek"]
        self.assertEqual(provider.extra_payload["provider_field"], "both")
        self.assertEqual(provider.extra_payload["thinking"], {"type": "disabled"})
        model_config = provider.model_configs["deepseek-chat"]
        self.assertEqual(model_config.context_window, 64000)
        self.assertEqual(model_config.extra_payload["provider_field"], "model")

    def test_image_generation_extra_headers(self) -> None:
        app = self.write_minimal(
            '[image_generation]\n'
            'extra_headers = { "X-Trace" = "on" }\n'
            'extra_payload = { n = 1 }\n'
        )
        cfg = loader.load(app)
        self.assertEqual(cfg.image_generation.extra_headers, {"X-Trace": "on"})
        self.assertEqual(cfg.image_generation.extra_payload, {"n": 1})

    def test_provider_extra_headers(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            "[providers.go]\n"
            'base_url = "https://opencode.ai/inference/go/openai/v1"\n'
            'extra_headers = { "x-opencode-session" = "atri-x-local" }\n',
        )
        cfg = loader.load(app)
        provider = cfg.providers["go"]
        self.assertEqual(provider.extra_headers, {"x-opencode-session": "atri-x-local"})

    def test_provider_extra_headers_reject_reserved(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            "[providers.go]\n"
            'extra_headers = { Authorization = "Bearer forge" }\n',
        )
        with self.assertRaises(ConfigDecodeError):
            loader.load(app)

    def test_provider_extra_headers_reject_line_break(self) -> None:
        app = self.write_minimal()
        self.write(
            "providers.toml",
            "[providers.go]\n"
            'extra_headers = { "x-session" = "line1\\nline2" }\n',
        )
        with self.assertRaises(ConfigDecodeError):
            loader.load(app)


class TestAppDefaults(ConfigCase):
    def test_default_config_is_complete(self) -> None:
        cfg = default()
        self.assertEqual(cfg.config_files.providers, "providers.toml")
        self.assertEqual(cfg.config_files.state, "state.toml")
        self.assertEqual(cfg.config_files.tool_tags, "tool_tags.toml")
        self.assertEqual(cfg.config_files.services, "")
        self.assertEqual(cfg.tools.max_rounds_per_turn, 2)
        self.assertEqual(cfg.session.naming.trigger_step, 1)
        self.assertEqual(cfg.session.idle_expiration.private_superadmin_ttl_minutes, 0)
        self.assertEqual(cfg.maintenance.daily_report.schedule, "0 9,21 * * *")
        self.assertTrue(cfg.maintenance.daily_report.is_peak_pricing())
        self.assertEqual(cfg.maintenance.session_cleanup.retention_days, 30)
        self.assertEqual(cfg.maintenance.sandbox_cleanup.retention_days, 7)
        self.assertEqual(cfg.maintenance.chat_history_cleanup.retention_days, 180)
        self.assertEqual(cfg.maintenance.privacy_cleanup.schedule, "")
        self.assertEqual(cfg.security.superadmins, {"cli": ["local"]})
        self.assertEqual(cfg.group_analysis.report_schedule, "0 9 * * *")
        self.assertEqual(cfg.angel_memory.retention_days, 365)
        self.assertEqual(cfg.self_learning.retention_days, 365)
        self.assertEqual(cfg.ops.circuit_breaker_half_open_max, 1)
        self.assertEqual(cfg.context.overflow_mode, "reject")
        self.assertEqual(cfg.atrinis.http.workers, 2)
        self.assertEqual(cfg.image_generation.model, "gpt-image-2.5")
        self.assertEqual(cfg.image_to_prompt.max_tokens, 400)
        self.assertEqual(cfg.vision.temperature_value(), 0.2)
        self.assertEqual(cfg.vision.language_value(), "zh")
        self.assertEqual(cfg.asr.max_retries, 2)
        self.assertEqual(cfg.turn_directives.prefixes, ["@", "#"])
        self.assertEqual(cfg.model_metadata.default_context_window, 256000)

    def test_apply_defaults_is_idempotent(self) -> None:
        cfg = default_app_config()
        first = cfg.to_dict()
        cfg.apply_app_defaults()
        cfg.apply_app_defaults()
        self.assertEqual(cfg.to_dict(), first)

    def test_negative_values_are_clamped(self) -> None:
        cfg = AppConfig.from_dict(
            {
                "ops": {"max_concurrent_turns": -3, "circuit_breaker_half_open_max": -1},
                "budget_limits": {
                    "global_image_daily": -5,
                    "chat_hard_limit_reserve_output_tokens": -1,
                },
                "context": {"compact_trigger_ratio": 1.5, "user_original_max_runes": -1},
                "session": {"idle_expiration": {"group_user_ttl_minutes": -7}},
                "resident_memory": {"normal_max_lines": 0},
            }
        )
        cfg.apply_app_defaults()
        self.assertEqual(cfg.ops.max_concurrent_turns, 0)
        self.assertEqual(cfg.ops.circuit_breaker_half_open_max, 1)
        self.assertEqual(cfg.budget_limits.global_image_daily, 0)
        self.assertEqual(cfg.budget_limits.chat_hard_limit_reserve_output_tokens, 0)
        self.assertEqual(cfg.context.compact_trigger_ratio, 0.8)
        self.assertEqual(cfg.context.user_original_max_runes, 4000)
        self.assertEqual(cfg.session.idle_expiration.group_user_ttl_minutes, 0)
        # 显式 0 的指针字段保留 0，取值方法回落到内置默认之外的值。
        self.assertEqual(cfg.resident_memory.normal_max_lines_value(), 0)

    def test_resident_memory_pointer_defaults(self) -> None:
        empty = AppConfig()
        self.assertEqual(empty.resident_memory.normal_write_min_interval_seconds_value(), 5)
        self.assertEqual(empty.resident_memory.normal_write_window_seconds_value(), 60)
        self.assertEqual(empty.resident_memory.normal_max_units_per_entry_value(), 80)
        self.assertTrue(empty.resident_memory.is_normal_block_instruction_patterns())
        self.assertEqual(empty.resident_memory.core_max_units, 200)

    def test_context_overflow_mode_normalization(self) -> None:
        self.assertEqual(
            ContextConfig(overflow_mode=" TRUNCATE ").normalized().overflow_mode, "truncate"
        )
        self.assertEqual(ContextConfig(overflow_mode="bogus").normalized().overflow_mode, "reject")

    def test_mode_models_validation(self) -> None:
        cfg = AppConfig()
        with self.assertRaises(ConfigDecodeError) as ctx:
            cfg.validate_mode_models()
        self.assertIn("mode_models.work", str(ctx.exception))
        cfg.mode_models["work"] = ModelSelection(provider="p", model="m")
        cfg.mode_models["chat"] = ModelSelection(provider="p", model="m")
        cfg.validate_mode_models()
        self.assertEqual(cfg.session.default_mode, "work")
        cfg.session.default_mode = "nope"
        with self.assertRaises(ConfigDecodeError) as ctx:
            cfg.validate_mode_models()
        self.assertIn("session.default_mode", str(ctx.exception))

    def test_default_model_selection_fallbacks(self) -> None:
        cfg = AppConfig()
        self.assertEqual(cfg.default_model_selection(), ModelSelection())
        cfg.session.default_mode = "chat"
        cfg.mode_models["work"] = ModelSelection(provider="p", model="work-model")
        self.assertEqual(cfg.default_model_selection().model, "work-model")
        cfg.mode_models["chat"] = ModelSelection(provider="p", model="chat-model")
        self.assertEqual(cfg.default_model_selection().model, "chat-model")

    def test_image_generation_dialect_and_negative_mode_validation(self) -> None:
        cfg = AppConfig()
        cfg.image_generation.negative_prompt_mode = "fold"
        with self.assertRaises(ConfigDecodeError) as ctx:
            cfg.validate_image_generation("app.toml", {})
        self.assertIn("negative_prompt_mode", str(ctx.exception))

        cfg = AppConfig()
        cfg.image_generation.prompt_dialect = "sd_weighted"
        cfg.validate_image_generation("app.toml", {})
        cfg.image_generation.prompt_dialect = "klingon"
        with self.assertRaises(ConfigDecodeError) as ctx:
            cfg.validate_image_generation("app.toml", {})
        self.assertIn("prompt_dialect", str(ctx.exception))

    def test_metadata_store_must_be_dedicated(self) -> None:
        app = self.write_minimal('[image_generation]\nmetadata_store = "app.toml"\n')
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("image_generation.metadata_store", str(ctx.exception))

    def test_artist_library_store_must_be_dedicated(self) -> None:
        app = self.write_minimal(
            '[image_generation]\nmetadata_store = "image_metadata.json"\n\n'
            '[image_generation.artist_library]\nroot = "image_metadata.json"\n'
        )
        with self.assertRaises(ConfigDecodeError) as ctx:
            loader.load(app)
        self.assertIn("artist_library.root", str(ctx.exception))

    def test_daily_report_prices_and_peak_tiers(self) -> None:
        app = self.write_minimal(
            '[maintenance.daily_report.prices."deepseek-flash"]\n'
            "input_per_million = 2.0\ncache_input_per_million = 0.04\n"
            "output_per_million = 8.0\noffpeak_input_per_million = 1.0\n"
            "offpeak_output_per_million = 4.0\n"
            "offpeak_cache_input_per_million = 0.02\n"
        )
        cfg = loader.load(app)
        report = cfg.maintenance.daily_report
        shanghai = dt.timezone(dt.timedelta(hours=8))
        peak = dt.datetime(2026, 10, 12, 10, 0, tzinfo=shanghai)  # 周一 10:00 → 高峰
        off = dt.datetime(2026, 10, 12, 20, 0, tzinfo=shanghai)  # 周一 20:00 → 空闲
        weekend = dt.datetime(2026, 10, 17, 10, 0, tzinfo=shanghai)  # 周六 → 空闲
        self.assertEqual(peak.weekday(), 0)
        self.assertEqual(weekend.weekday(), 5)
        self.assertEqual(report.price_for("deepseek-flash", peak).input_per_million, 2.0)
        self.assertEqual(report.price_for("deepseek-flash", off).input_per_million, 1.0)
        self.assertIsNone(report.price_for("ghost", peak))
        self.assertEqual(report.price_for("deepseek-flash", weekend).input_per_million, 1.0)
        report.holidays = ["2026-10-12"]
        self.assertEqual(report.price_for("deepseek-flash", peak).input_per_million, 1.0)
        cost = report.price_for("deepseek-flash", off).compute_cost(1_000_000, 1_000_000, 200_000)
        self.assertAlmostEqual(cost, 0.8 * 1.0 + 0.2 * 0.02 + 1.0 * 4.0, places=9)


class TestInspect(ConfigCase):
    def test_unknown_keys_lists_typo_section(self) -> None:
        app = self.write_minimal('[provders.openai]\nbase_url = "x"\n')
        self.assertEqual(I.unknown_app_config_keys(app), ["provders"])

    def test_unknown_keys_empty_for_minimal_config(self) -> None:
        app = self.write_minimal()
        self.assertEqual(I.unknown_app_config_keys(app), [])

    def test_unknown_keys_reports_type_error(self) -> None:
        app = self.write_minimal('[tools]\nmax_rounds_per_turn = "ten"\n')
        with self.assertRaises(ConfigDecodeError):
            I.unknown_app_config_keys(app)

    def test_unknown_keys_missing_file(self) -> None:
        with self.assertRaises(MissingConfigFileError):
            I.unknown_app_config_keys(self.dir / "nope.toml")

    def test_builtin_asset_drift_detects_modified_and_missing_skills(self) -> None:
        A.ensure_platform_defaults(self.dir)
        self.assertEqual(I.builtin_asset_drift(str(self.dir)), [])
        skill = self.dir / "skills/agent/write_atrix_hook/SKILL.md"
        skill.write_text("hand edited\n", encoding="utf-8")
        (self.dir / "skills/agent/agent_skill_creator/SKILL.md").unlink()
        drift = I.builtin_asset_drift(str(self.dir))
        self.assertEqual(len(drift), 2)
        self.assertTrue(any("write_atrix_hook" in item for item in drift))
        self.assertTrue(any("缺失内置 Skill 文件" in item for item in drift))
        self.assertEqual(I.builtin_asset_drift(""), [])

    def test_app_toml_edits_do_not_count_as_drift(self) -> None:
        A.ensure_platform_defaults(self.dir)
        (self.dir / "app.toml").write_text("[tools]\nmax_rounds_per_turn = 3\n", encoding="utf-8")
        self.assertEqual(I.builtin_asset_drift(str(self.dir)), [])


class TestToolTags(ConfigCase):
    def test_load_tool_tags(self) -> None:
        path = self.write(
            "tool_tags.toml",
            '[tags.agent]\ntools = ["read_file", "shell"]\nprompt = """ROLE: be careful."""\n',
        )
        tags = loader.load_tool_tags(path)
        self.assertEqual(tags.names(), ["agent"])
        self.assertEqual(tags.tag("agent").tools, ["read_file", "shell"])
        self.assertIn("ROLE", tags.tag("agent").prompt)
        self.assertIsNone(tags.tag("nope"))

    def test_missing_tool_tags_returns_none(self) -> None:
        self.assertIsNone(loader.load_tool_tags(self.dir / "tool_tags.toml"))

    def test_unknown_tag_key_reports_path(self) -> None:
        path = self.write("tool_tags.toml", "[tags.agent]\ntoolz = []\n")
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            loader.load_tool_tags(path)
        self.assertIn("tags.agent.toolz", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
