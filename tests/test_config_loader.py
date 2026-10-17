"""``atri_x.config.loader`` 测试：查找顺序、``.env`` 优先级、相对路径、旧部署兼容。

对应上游 ``internal/config/config_test.go`` 的
``TestLoadDotEnv*`` / ``TestLoadProviderAPIKeyEnv*`` / ``TestLoadMissingProvidersConfig`` /
``TestResolvePath`` 语义。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import unittest
from unittest import mock

from atri_x.config import (
    ENV_CONFIG_FILE,
    AppConfig,
    ConfigDecodeError,
    UnknownConfigKeysError,
    config_env,
    load,
    load_dot_env,
    load_env_file,
    load_from_dir,
    new_load_config,
    plugin_config_dir,
    resolve_path,
    resolve_relative,
)
from atri_x.config import loader
from atri_x.config.app import APP_DIR_NAME, XDG_APP_DIR_NAME
from atri_x.config.services import ImageGenerationProfileConfig

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tmp_cfg_test"


def _cleanup_case_root(class_name: str) -> None:
    """收掉本测试类的目录与（空掉的）根目录（``tmp_cfg_test/`` 不在 .gitignore 里）。"""
    shutil.rmtree(ROOT / class_name, ignore_errors=True)
    try:
        ROOT.rmdir()
    except OSError:
        pass

PROVIDERS_TOML = """
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "ATRIX_TEST_LOADER_KEY"
"""

STATE_TOML = """
[mode_models.work]
provider = "deepseek"
model = "deepseek-chat"

[mode_models.chat]
provider = "deepseek"
model = "deepseek-chat"
"""


class LoaderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = ROOT / type(self).__name__ / self._testMethodName
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        _cleanup_case_root(type(self).__name__)

    def write(self, name: str, body: str) -> pathlib.Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
        return path

    def minimal(self, app_body: str = "") -> pathlib.Path:
        app = self.write("app.toml", app_body)
        self.write("providers.toml", PROVIDERS_TOML)
        self.write("state.toml", STATE_TOML)
        return app

    def patch_platform_dirs(self, appdata: pathlib.Path):
        return mock.patch.dict(
            os.environ, {"APPDATA": str(appdata), "XDG_CONFIG_HOME": str(appdata)}
        )

    def platform_config_path(self, appdata: pathlib.Path) -> pathlib.Path:
        name = APP_DIR_NAME if os.name == "nt" else XDG_APP_DIR_NAME
        return appdata / name / "app.toml"


class TestResolvePath(LoaderCase):
    def test_explicit_path_wins_and_is_normalized(self) -> None:
        target = self.minimal()
        self.assertEqual(resolve_path(target), os.path.normpath(str(target)))
        raw = str(self.dir / "sub" / ".." / "app.toml")
        self.assertEqual(resolve_path(raw), os.path.normpath(raw))
        self.assertEqual(resolve_path(target), resolve_path(str(target)))
        self.assertEqual(resolve_path(target), resolve_path(pathlib.Path(target)))

    def test_env_var_is_used_when_no_path_is_given(self) -> None:
        target = self.minimal()
        with mock.patch.dict(os.environ, {ENV_CONFIG_FILE: str(target)}):
            self.assertEqual(resolve_path(), os.path.normpath(str(target)))
            self.assertEqual(resolve_path(""), os.path.normpath(str(target)))
            self.assertEqual(load().config_path, os.path.normpath(str(target)))

    def test_explicit_path_beats_env_var(self) -> None:
        first = self.minimal()
        other = self.write("other.toml", "[tools]\nmax_rounds_per_turn = 1\n")
        with mock.patch.dict(os.environ, {ENV_CONFIG_FILE: str(first)}):
            self.assertEqual(resolve_path(other), os.path.normpath(str(other)))

    def test_platform_default_dir_is_generated_when_missing(self) -> None:
        appdata = self.dir / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)
        expected = self.platform_config_path(appdata)
        with self.patch_platform_dirs(appdata):
            self.assertFalse(expected.exists())
            resolved = resolve_path("")
            self.assertEqual(pathlib.Path(resolved), expected)
            self.assertTrue(expected.is_file())
            # 内置资产一并落盘。
            self.assertTrue(expected.parent.joinpath("services.toml").is_file())
            self.assertTrue(expected.parent.joinpath("skills", "agent").is_dir())
            self.assertEqual(loader.ensure_platform_defaults(), str(expected))
            # 再次解析不会改写（内容与 mtime 都保持）。
            before = expected.stat().st_mtime_ns
            self.assertEqual(resolve_path(""), str(expected))
            self.assertEqual(expected.stat().st_mtime_ns, before)

    def test_existing_platform_default_is_reused(self) -> None:
        appdata = self.dir / "appdata2"
        target = self.platform_config_path(appdata)
        target.parent.mkdir(parents=True)
        target.write_text("[tools]\nmax_rounds_per_turn = 4\n", encoding="utf-8")
        with self.patch_platform_dirs(appdata):
            self.assertEqual(resolve_path(""), str(target))
            # 已存在的 app.toml 不会被内置资产覆盖。
            self.assertIn("max_rounds_per_turn = 4", target.read_text(encoding="utf-8"))


class TestDotEnv(LoaderCase):
    def test_load_env_file_parsing(self) -> None:
        path = self.write(
            ".env",
            "\n".join(
                (
                    "# comment",
                    "",
                    "PLAIN=value",
                    "SPACED =  padded  ",
                    'QUOTED="quoted value"',
                    "SINGLE='single value'",
                    'UNBALANCED="oops',
                    "NO_EQUALS",
                    "=noname",
                    "EMPTY=",
                    "DUP=first",
                    "DUP=second",
                )
            )
            + "\n",
        )
        values = load_env_file(path)
        self.assertEqual(values["PLAIN"], "value")
        self.assertEqual(values["SPACED"], "padded")
        self.assertEqual(values["QUOTED"], "quoted value")
        self.assertEqual(values["SINGLE"], "single value")
        self.assertEqual(values["UNBALANCED"], '"oops')
        self.assertNotIn("NO_EQUALS", values)
        self.assertEqual(values["EMPTY"], "")
        self.assertEqual(values["DUP"], "first")  # 首次出现者生效
        self.assertNotIn("comment", values)

    def test_load_env_file_missing_returns_empty(self) -> None:
        self.assertEqual(load_env_file(self.dir / ".env"), {})
        self.assertEqual(load_env_file(""), {})

    def test_load_dot_env_uses_config_dir(self) -> None:
        self.write(".env", "ATRIX_TEST_DOTENV=1\n")
        self.assertEqual(load_dot_env(str(self.dir)), {"ATRIX_TEST_DOTENV": "1"})

    def test_config_env_prefers_system_environment(self) -> None:
        self.write(".env", "ATRIX_TEST_PRIORITY=from-dotenv\nATRIX_TEST_ONLY_DOTENV=x\n")
        with mock.patch.dict(os.environ, {"ATRIX_TEST_PRIORITY": "from-os"}):
            self.assertEqual(config_env("ATRIX_TEST_PRIORITY", str(self.dir)), "from-os")
            self.assertEqual(config_env("ATRIX_TEST_ONLY_DOTENV", str(self.dir)), "x")
            self.assertIsNone(config_env("ATRIX_TEST_ABSENT", str(self.dir)))

    def test_config_env_blank_system_value_falls_back_to_dotenv(self) -> None:
        self.write(".env", "ATRIX_TEST_BLANK=from-dotenv\n")
        with mock.patch.dict(os.environ, {"ATRIX_TEST_BLANK": "   "}):
            self.assertEqual(config_env("ATRIX_TEST_BLANK", str(self.dir)), "from-dotenv")

    def test_provider_api_key_resolution_uses_config_dir(self) -> None:
        app = self.minimal()
        self.write(".env", "ATRIX_TEST_LOADER_KEY=from-dotenv\n")
        cfg = load_from_dir(self.dir)
        self.assertEqual(cfg.providers["deepseek"].api_key, "from-dotenv")
        self.assertEqual(pathlib.Path(cfg.config_path), app)

    def test_resolve_provider_api_keys_directly(self) -> None:
        from atri_x.config import ProviderConfig

        config = new_load_config()
        config.providers = {"a": ProviderConfig(api_key_env="ATRIX_TEST_DIRECT_KEY")}
        self.write(".env", "ATRIX_TEST_DIRECT_KEY=direct\n")
        loader.resolve_provider_api_keys(config, str(self.dir))
        self.assertEqual(config.providers["a"].api_key, "direct")
        # 已有 api_key 的 provider 不再解析。
        config.providers["b"] = ProviderConfig(
            api_key="keep", api_key_env="ATRIX_TEST_DIRECT_KEY"
        )
        loader.resolve_provider_api_keys(config, str(self.dir))
        self.assertEqual(config.providers["b"].api_key, "keep")


class TestRelativePaths(LoaderCase):
    def test_relative_paths_resolve_against_config_dir(self) -> None:
        app = self.minimal(
            '[storage]\nsessions_sqlite_path = "db/sessions.db"\n'
            'chat_history_sqlite_path = "db/chat.db"\n\n'
            '[soul]\npath = "soul/SOUL.md"\n\n'
            '[character_library]\nroot = "chars"\n\n'
            '[sandbox]\nroot = "box"\n\n'
            '[image_generation]\nmetadata_store = "meta/params.json"\n\n'
            '[image_generation.artist_library]\nroot = "artists.json"\n'
        )
        cfg = load_from_dir(self.dir)
        self.assertEqual(pathlib.Path(cfg.storage.sessions_sqlite_path), self.dir / "db/sessions.db")
        self.assertEqual(pathlib.Path(cfg.storage.chat_history_sqlite_path), self.dir / "db/chat.db")
        self.assertEqual(pathlib.Path(cfg.soul.path), self.dir / "soul/SOUL.md")
        self.assertEqual(pathlib.Path(cfg.character_library.root), self.dir / "chars")
        self.assertEqual(pathlib.Path(cfg.sandbox.root), self.dir / "box")
        self.assertEqual(
            pathlib.Path(cfg.image_generation.metadata_store), self.dir / "meta/params.json"
        )
        self.assertEqual(
            pathlib.Path(cfg.image_generation.artist_library.root), self.dir / "artists.json"
        )
        self.assertEqual(pathlib.Path(cfg.config_path), app)

    def test_absolute_paths_are_untouched(self) -> None:
        absolute = str(self.dir / "absolute.db")
        app = self.minimal(
            f'[storage]\nsessions_sqlite_path = "{pathlib.PurePath(absolute).as_posix()}"\n'
        )
        cfg = load_from_dir(self.dir)
        self.assertEqual(cfg.storage.sessions_sqlite_path, absolute)
        self.assertEqual(pathlib.Path(cfg.config_path), app)

    def test_resolve_relative_helper(self) -> None:
        self.assertEqual(
            resolve_relative("/cfg/app.toml", "a/b.toml"), os.path.normpath("/cfg/a/b.toml")
        )
        self.assertEqual(
            resolve_relative("/cfg/app.toml", "/abs/x.toml"), os.path.normpath("/abs/x.toml")
        )
        self.assertEqual(resolve_relative("/cfg/app.toml", ""), ".")

    def test_plugin_config_dir(self) -> None:
        self.assertEqual(plugin_config_dir(str(self.dir / "app.toml")), str(self.dir / "plugins"))
        # 取不到平台配置目录时退回相对目录 "plugins"（Go 的 filepath.Join(".", "plugins")）。
        with mock.patch.dict(
            os.environ,
            {"APPDATA": "", "XDG_CONFIG_HOME": "", "USERPROFILE": "", "HOME": ""},
        ):
            self.assertEqual(plugin_config_dir(""), "plugins")

    def test_plugin_config_dir_uses_platform_default_when_empty(self) -> None:
        appdata = self.dir / "appdata"
        name = APP_DIR_NAME if os.name == "nt" else XDG_APP_DIR_NAME
        with self.patch_platform_dirs(appdata):
            self.assertEqual(
                plugin_config_dir(""), os.path.join(str(appdata), name, "plugins")
            )


class TestServicesAndLegacyLayout(LoaderCase):
    """``services.toml`` 与 app.toml 的覆盖关系（对应 Go ``mergeServices``）。"""

    def test_services_override_legacy_image_generation(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nservices = "services.toml"\n\n'
            '[image_generation]\nenabled = false\nbase_url = "https://legacy/v1"\n'
            'model = "legacy-image"\n',
        )
        self.write(
            "services.toml",
            '[providers.central]\nbase_url = "https://central/v1"\nmodels = ["a", "b"]\n\n'
            "[model_metadata]\ndefault_context_window = 64000\n\n"
            '[model_profiles.fast]\nprovider = "central"\nmodel = "a"\naliases = ["快"]\n\n'
            '[image_generation]\nenabled = true\nmodel = "central-image"\n\n'
            '[image_generation.profiles.hq]\nmodel = "central-image-hq"\nquality = "high"\n\n'
            '[image_to_prompt]\nprovider = "central"\nmodel = "central-vision"\n'
            "max_tokens = 256\n",
        )
        self.write("state.toml", STATE_TOML)
        cfg = load(app)
        self.assertEqual(cfg.providers["central"].models, ["a", "b"])
        self.assertEqual(cfg.model_metadata.default_context_window, 64000)
        self.assertEqual(cfg.model_profiles["fast"].aliases, ["快"])
        self.assertTrue(cfg.image_generation.enabled)
        self.assertEqual(cfg.image_generation.model, "central-image")
        self.assertEqual(cfg.image_generation.profiles["hq"].quality, "high")
        self.assertTrue(cfg.image_to_prompt.is_enabled())
        self.assertEqual(cfg.image_to_prompt.max_tokens, 256)
        self.assertEqual(cfg.image_to_prompt.temperature_value(), 0.2)

    def test_services_missing_sections_keep_legacy_values(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nservices = "services.toml"\n\n'
            '[image_to_prompt]\nprovider = "legacy"\nmodel = "legacy-vision"\n',
        )
        self.write("services.toml", '[providers.central]\nbase_url = "https://central/v1"\n')
        self.write("state.toml", STATE_TOML)
        cfg = load(app)
        self.assertEqual(cfg.image_to_prompt.provider, "legacy")
        self.assertEqual(cfg.providers["central"].base_url, "https://central/v1")

    def test_image_generation_resolve_profile(self) -> None:
        cfg = AppConfig()
        cfg.image_generation.enabled = True
        cfg.image_generation.model = "base-model"
        cfg.image_generation.profiles["hq"] = ImageGenerationProfileConfig(
            model="hq-model", quality="medium", superadmin_only=False, send_by_default=True
        )
        base = cfg.image_generation.resolve_profile("")
        self.assertEqual(base.model, "base-model")
        # 空名字返回基础配置本体（Go 同样保留 profiles）；命中的 profile 才清空 profiles。
        self.assertEqual(base.profiles, cfg.image_generation.profiles)
        hq = cfg.image_generation.resolve_profile("hq")
        self.assertEqual(hq.model, "hq-model")
        self.assertEqual(hq.profiles, {})
        self.assertFalse(hq.superadmin_only)
        self.assertTrue(hq.send_by_default)
        self.assertEqual(hq.size, "1024x1024")  # 继承基础配置
        self.assertIsNone(cfg.image_generation.resolve_profile("nope"))

    def test_image_to_prompt_enabled_by_provider_model(self) -> None:
        cfg = AppConfig()
        self.assertFalse(cfg.image_to_prompt.is_enabled())
        cfg.image_to_prompt.provider = "openai"
        cfg.image_to_prompt.model = "gpt-4o-mini"
        self.assertTrue(cfg.image_to_prompt.is_enabled())
        cfg.image_to_prompt.enabled = False
        self.assertFalse(cfg.image_to_prompt.is_enabled())


class TestLoadEntryPoints(LoaderCase):
    def test_load_from_dir_accepts_str_and_path(self) -> None:
        self.minimal()
        first = load_from_dir(str(self.dir))
        second = load_from_dir(self.dir)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_load_from_dir_missing_app_toml(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_from_dir(self.dir)

    def test_atrinis_file_overrides_app_section(self) -> None:
        self.minimal('[atrinis]\nenabled = false\nallowed_tools = ["a"]\n')
        self.write(
            "atrinis.toml",
            'enabled = true\nallowed_tools = ["web_search"]\n\n[http]\naddr = "0.0.0.0:32170"\n',
        )
        cfg = load_from_dir(self.dir)
        self.assertTrue(cfg.atrinis.enabled)
        self.assertEqual(cfg.atrinis.allowed_tools, ["web_search"])
        self.assertEqual(cfg.atrinis.http.addr, "0.0.0.0:32170")
        self.assertEqual(cfg.atrinis.http.workers, 2)  # 其余字段仍取默认
        self.assertEqual(pathlib.Path(cfg.atrinis_config_path), self.dir / "atrinis.toml")

    def test_missing_atrinis_file_is_tolerated(self) -> None:
        self.minimal("[atrinis]\nenabled = true\n")
        cfg = load_from_dir(self.dir)
        self.assertTrue(cfg.atrinis.enabled)
        self.assertEqual(cfg.atrinis.http.addr, "127.0.0.1:32170")

    def test_providers_file_unknown_key_is_reported(self) -> None:
        app = self.minimal()
        self.write("providers.toml", '[providers.deepseek]\nbase_url = "x"\nbogus = 1\n')
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            load(app)
        self.assertIn("providers.deepseek.bogus", str(ctx.exception))

    def test_services_file_unknown_key_is_reported(self) -> None:
        self.write("services.toml", '[providers.a]\nbase_url = "x"\ntypo = 1\n')
        self.write("state.toml", STATE_TOML)
        app = self.write("app.toml", '[config_files]\nservices = "services.toml"\n')
        with self.assertRaises(UnknownConfigKeysError) as ctx:
            load(app)
        self.assertIn("providers.a.typo", str(ctx.exception))

    def test_state_must_not_share_the_tool_tags_file(self) -> None:
        app = self.write(
            "app.toml",
            '[config_files]\nproviders = "providers.toml"\nstate = "tool_tags.toml"\n',
        )
        self.write("providers.toml", PROVIDERS_TOML)
        self.write("tool_tags.toml", "")
        with self.assertRaises(ConfigDecodeError) as ctx:
            load(app)
        self.assertIn("dedicated writable file", str(ctx.exception))
        self.assertIn("tool_tags", str(ctx.exception))

    def test_load_reports_resolved_config_paths(self) -> None:
        self.minimal()
        cfg = load_from_dir(self.dir)
        self.assertEqual(pathlib.Path(cfg.providers_config_path), self.dir / "providers.toml")
        self.assertEqual(cfg.services_config_path, "")
        self.assertEqual(pathlib.Path(cfg.state_config_path), self.dir / "state.toml")
        self.assertEqual(pathlib.Path(cfg.tool_tags_config_path), self.dir / "tool_tags.toml")
        self.assertEqual(cfg.config_warnings, [])


if __name__ == "__main__":
    unittest.main()
