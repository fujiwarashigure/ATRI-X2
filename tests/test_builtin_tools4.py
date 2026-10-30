"""``atriwisp_creator`` / ``builtin.runtime`` 与 ``register_all`` 接线的单元测试。

对应 Go 测试：``internal/tool/builtin/atriwisp_creator_test.go`` 与 ``register_test.go``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest

from atri_x.config import ENV_CONFIG_FILE
from atri_x.tool.builtin.atriwisp_creator import (
    ATRIWISP_CREATOR_NAME,
    AtriwispCreatorConfigSnapshot,
    AtriwispCreatorTool,
    build_atriwisp_creator_guide,
    endpoint_url,
    find_atriwisp_creator_app_config_path,
    load_atriwisp_creator_config,
    new_atriwisp_creator_tool,
    user_config_dir,
)
from atri_x.tool.builtin.register import (
    ALWAYS_REGISTERED_TOOLS,
    CONDITIONAL_TOOLS,
    REGISTERED_TOOLS,
    UNPORTED_TOOLS,
    RegisterOptions,
    new_registry_with_builtin,
    register_all,
)
from atri_x.tool.builtin.runtime import image_config_from
from atri_x.tool.skill import new_manager
from atri_x.tool.tool import CallRequest, Registry

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_tools4_test")


def _run(coro):
    return asyncio.run(coro)


class FakeCronService:
    def create(self, ctx, req):
        return None

    def update(self, ctx, req):
        return None

    def get(self, ctx, name, actor):
        return None

    def list(self, ctx, include_disabled, include_completed, actor):
        return []

    def disable(self, ctx, name, actor):
        return None

    def delete(self, ctx, name, actor):
        return None


class FakeSelfLearning:
    def ready(self) -> bool:
        return True


class FakeGroupAnalysis:
    def analyze(self, req):
        return None

    async def summarize(self, ctx, report):
        return ""


class FakeCenter:
    """够用的 media center 占位（只用于接线，不真正取媒体）。"""

    def metadata(self, ctx, id):
        raise AssertionError("not used")


class FakeResidentStore:
    """只够接线用的常驻记忆 store：FileGuard 读 ``path``，``info()`` 读上限与策略。"""

    def __init__(self) -> None:
        from atri_x.memory.resident import NormalWritePolicy

        self.path = os.path.join(TMP_ROOT, "memories.toml")
        self.normal_policy = NormalWritePolicy()

    def limits_or_default(self):
        from atri_x.memory.resident import Limits

        return Limits(core=200, normal=300)


class FakeCharacterStore:
    """Go 的守卫是 ``CharacterStore != nil && Enabled()``。"""

    def enabled(self) -> bool:
        return True


class FakeArtistService:
    def enabled(self) -> bool:
        return True


class FakeImageClient:
    def enabled(self) -> bool:
        return True


class AtriwispCreatorTest(unittest.TestCase):
    def test_names_and_metadata(self) -> None:
        tool = new_atriwisp_creator_tool()
        self.assertEqual(tool.name(), ATRIWISP_CREATOR_NAME)
        info = tool.info()
        self.assertTrue(info.superadmin_only)
        self.assertEqual(info.risk, "low")
        self.assertEqual(info.depends_on, ["read_file", "edit_file", "shell"])
        self.assertEqual(info.source, "builtin")
        self.assertEqual(tool.schema().function.name, ATRIWISP_CREATOR_NAME)

    def test_endpoint_url(self) -> None:
        self.assertEqual(endpoint_url("", "/x"), "unknown")
        self.assertEqual(endpoint_url("127.0.0.1:1", "/healthz"), "http://127.0.0.1:1/healthz")
        self.assertEqual(
            endpoint_url("https://a.example/", "/atriena/v3/events"),
            "https://a.example/atriena/v3/events",
        )

    def test_guide_contains_task_card_and_config_section(self) -> None:
        snapshot = AtriwispCreatorConfigSnapshot(
            app_config_path="/tmp/app.toml",
            atrinis_config_path="/tmp/atrinis.toml",
            enabled=True,
            addr="127.0.0.1:9000",
            token_names=["home"],
            token_env_names=["ATRI_HOME_TOKEN"],
            allowed_tools=["web_search"],
        )
        guide = build_atriwisp_creator_guide(snapshot)
        self.assertIn("#task create_atriwisp", guide)
        self.assertIn("// current_atrinis", guide)
        self.assertIn("// endpoint: http://127.0.0.1:9000/atriena/v3/events", guide)
        self.assertIn("// token_env: [ATRI_HOME_TOKEN]", guide)
        self.assertIn("// allowed_tools: [web_search]", guide)
        self.assertIn("// atriyph_rule_card", guide)
        # 空列表要写成 ``[]``，不是空串。
        empty = build_atriwisp_creator_guide(AtriwispCreatorConfigSnapshot())
        self.assertIn("// token_names: []", empty)
        # 缺少 endpoint 时明确写 unknown，避免模型编造。
        self.assertIn("// endpoint: unknown", empty)
        self.assertIn("// enabled: false", empty)

    def test_warnings_are_rendered(self) -> None:
        guide = build_atriwisp_creator_guide(
            AtriwispCreatorConfigSnapshot(warnings=["未启用 Atrinis"])
        )
        self.assertIn("// warning: 未启用 Atrinis", guide)

    def test_load_config_falls_back_to_env_var(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        app_path = os.path.join(TMP_ROOT, "env-app.toml")
        with open(app_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("[sandbox]\nroot = \".\"\n")
        saved = os.environ.get(ENV_CONFIG_FILE)
        os.environ[ENV_CONFIG_FILE] = app_path
        try:
            class Info:
                config_path = ""
                config_dir = ""
                sandbox_root = ""

                def normalize(self):
                    return self

            path, ok = find_atriwisp_creator_app_config_path(Info())
            self.assertTrue(ok)
            self.assertEqual(path, os.path.normpath(app_path))
        finally:
            if saved is None:
                os.environ.pop(ENV_CONFIG_FILE, None)
            else:
                os.environ[ENV_CONFIG_FILE] = saved
            shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_user_config_dir_is_non_empty_or_empty_string(self) -> None:
        self.assertIsInstance(user_config_dir(), str)

    def test_call_returns_guide(self) -> None:
        tool = AtriwispCreatorTool()
        result = _run(tool.call(None, CallRequest()))
        self.assertIn("#task create_atriwisp", result.content)

    def test_call_with_configured_app_toml(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        app_path = os.path.join(TMP_ROOT, "app.toml")
        # 加载器（atri_x.config.load）会校验 [config_files] 指向的文件是否存在，
        # 以及 [mode_models.work] / [mode_models.chat] 的 provider/model，这里按
        # tests/test_config_loader.py 的最小可用形状构造。
        with open(app_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                "[sandbox]\nroot = \".\"\n\n"
                "[mode_models.work]\nprovider = \"deepseek\"\nmodel = \"deepseek-chat\"\n\n"
                "[mode_models.chat]\nprovider = \"deepseek\"\nmodel = \"deepseek-chat\"\n"
            )
        with open(
            os.path.join(TMP_ROOT, "providers.toml"), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(
                "[providers.deepseek]\nbase_url = \"https://api.deepseek.com\"\n"
                "api_key_env = \"ATRIX_TEST_ATRIWISP_KEY\"\n"
            )
        # Atrinis 段来自同目录的 atrinis.toml（Go 的 config.Load 会一起读）；
        # 该文件本身就是整个 AtrinisConfig，键在顶层。
        with open(
            os.path.join(TMP_ROOT, "atrinis.toml"), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(
                "enabled = true\nallowed_tools = ['web_search']\n\n"
                "[http]\naddr = \"127.0.0.1:9000\"\n\n"
                "[tokens.home]\ntoken_env = ['ATRI_HOME_TOKEN']\n"
            )
        try:
            tool = AtriwispCreatorTool()
            tool.runtime_info = type(tool.runtime_info)(config_path=app_path)
            snapshot = load_atriwisp_creator_config(tool.runtime_info)
            self.assertEqual(snapshot.app_config_path, app_path)
            self.assertTrue(snapshot.enabled)
            self.assertEqual(snapshot.allowed_tools, ["web_search"])
            self.assertEqual(snapshot.addr, "127.0.0.1:9000")
            self.assertEqual(snapshot.token_names, ["home"])
            self.assertEqual(snapshot.token_env_names, ["ATRI_HOME_TOKEN"])
            self.assertFalse(snapshot.warnings)
        finally:
            shutil.rmtree(TMP_ROOT, ignore_errors=True)


class RuntimeForwardTest(unittest.TestCase):
    def test_image_config_from_normalizes(self) -> None:
        class Section:
            enabled = False
            base_url = "https://example.invalid"
            endpoint = "/v1/images"
            api_key = ""
            api_key_env = "KEY"
            model = "gpt-image-1"
            size = "1024x1024"
            quality = "high"
            output_format = "png"
            response_format = "b64_json"
            timeout_seconds = 30
            preset_prompt = ""
            negative_prompt = ""
            max_prompt_runes = 0
            optimize = ""
            optimize_term_mode = ""
            optimize_max_anchors = 0
            optimize_max_negatives = 0
            optimize_max_added_runes = 0
            optimize_max_tags = 0
            optimize_rewrite = ""
            optimize_rewrite_model = ""
            optimize_rewrite_min_runes = 0
            auto_character = False
            auto_context = False
            context_default_limit = 0
            superadmin_only = False
            save_to_character = False
            send_by_default = False
            supports_reference = False
            reference_field = ""
            model_family = "sdxl"
            prompt_dialect = "sd_weighted"
            negative_prompt_field = ""
            negative_prompt_mode = "inline"
            artist_default_preset = ""
            extra_payload = {"a": "b"}
            extra_headers = {"X": "Y"}
            proxy = ""

        config = image_config_from(Section())
        # Go 的 imageConfigFrom 强制 enabled=true：调用方已经判断过启用。
        self.assertTrue(config.enabled)
        self.assertEqual(config.model, "gpt-image-1")
        self.assertEqual(config.model_family, "sdxl")
        self.assertEqual(config.extra_payload, {"a": "b"})
        self.assertEqual(config.extra_headers, {"X": "Y"})

    def test_image_config_from_uses_is_getters(self) -> None:
        class Section:
            auto_character = False
            auto_context = False
            superadmin_only = False
            save_to_character = False

            def is_auto_character(self) -> bool:
                return True

            def is_auto_context(self) -> bool:
                return True

            def is_superadmin_only(self) -> bool:
                return True

            def is_save_to_character(self) -> bool:
                return True

        config = image_config_from(Section())
        self.assertTrue(config.auto_character)
        self.assertTrue(config.auto_context)
        self.assertTrue(config.superadmin_only)
        self.assertTrue(config.save_to_character)

    def test_app_layer_names_are_reachable(self) -> None:
        import atri_x.tool.builtin.runtime as runtime

        self.assertEqual(runtime.Runtime.__name__, "ToolRuntime")
        self.assertEqual(runtime.RuntimeOptions.__name__, "ToolRuntimeOptions")
        self.assertTrue(callable(runtime.new_runtime))

    def test_unknown_attribute_raises(self) -> None:
        import atri_x.tool.builtin.runtime as runtime

        with self.assertRaises(AttributeError):
            runtime.definitely_not_here


class RegisterWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_no_name_is_both_registered_and_unported(self) -> None:
        unported = " ".join(UNPORTED_TOOLS)
        for name in REGISTERED_TOOLS:
            self.assertNotIn(name, unported, name)

    def test_minimal_registry(self) -> None:
        registry = new_registry_with_builtin(RegisterOptions())
        self.assertIsInstance(registry, Registry)
        names = {info.name for info in registry.list()}
        # 一个依赖都没给时，注册表里**恰好**是 Go ``RegisterAll`` 的无条件那批：
        # register.go:64-224 里带 if 守卫的工具一个都不注册（注册了也只能失败）。
        self.assertEqual(names, set(ALWAYS_REGISTERED_TOOLS))
        for option, group in CONDITIONAL_TOOLS.items():
            for name in group:
                self.assertNotIn(name, names, f"{name}（守卫 {option}）")

    def test_every_wired_tool_registers_when_all_dependencies_exist(self) -> None:
        """把 ``RegisterOptions`` 的依赖全给上，``REGISTERED_TOOLS`` 必须全部出现。

        这是「接线是否完整」的回归：任一条件组因为守卫写错/漏注册都会在这里暴露。
        """
        from atri_x.comic import ComicOptions, ComicService, ComicStore
        from atri_x.imagegen import Config as ImageConfig
        from atri_x.tool.builtin.artist_tools import ArtistTools
        from atri_x.tool.builtin.file_manager import FileManager
        from atri_x.tool.builtin.image_generate import ImageProfile

        registry = Registry()
        register_all(
            registry,
            RegisterOptions(
                resident_memory_store=FakeResidentStore(),
                angel_memory=object(),
                angel_memory_allow_forget=True,
                self_learning=FakeSelfLearning(),
                chat_history=FakeCenter(),
                long_memory_dir=TMP_ROOT,
                cron_service=FakeCronService(),
                group_analysis_service=FakeGroupAnalysis(),
                group_analysis_max_messages=100,
                file_manager=FileManager(sandbox_root=TMP_ROOT, media=FakeCenter()),
                character_store=FakeCharacterStore(),
                # skill 一族（Go ``register.go:237-250``）挂在 ``SkillManager != nil``
                # 上；「依赖给全」自然包括 manager。manager 用真实实现，只是不装载。
                skill_manager=new_manager(os.path.join(TMP_ROOT, "skills"), registry),
                image_profiles={
                    "main": ImageProfile(
                        name="main", client=FakeImageClient(), config=ImageConfig()
                    )
                },
                default_image_profile="main",
                artists=ArtistTools(service=FakeArtistService()),
                image_prompt_service=object(),
                # Python 侧新增（1.1.6）：漫画两个工具挂在 ``comic`` 守卫上；这
                # 里用真实 ComicService（构造不触网、不建数据库）。
                comic=ComicService(
                    ComicOptions(),
                    store=ComicStore(TMP_ROOT),
                    profiles={
                        "main": ImageProfile(
                            name="main", client=FakeImageClient(), config=ImageConfig()
                        )
                    },
                    default_profile="main",
                ),
            ),
        )
        names = {info.name for info in registry.list()}
        for name in REGISTERED_TOOLS:
            self.assertIn(name, names, name)
        self.assertEqual(names, set(REGISTERED_TOOLS))

    def test_conditional_tools_are_registered_when_dependencies_exist(self) -> None:
        from atri_x.tool.builtin.file_manager import FileManager

        registry = Registry()
        register_all(
            registry,
            RegisterOptions(
                self_learning=FakeSelfLearning(),
                cron_service=FakeCronService(),
                chat_history=FakeCenter(),
                long_memory_dir=TMP_ROOT,
                group_analysis_service=FakeGroupAnalysis(),
                group_analysis_max_messages=100,
                file_manager=FileManager(sandbox_root=TMP_ROOT, media=FakeCenter()),
            ),
        )
        names = {info.name for info in registry.list()}
        for name in (
            "cron",
            "cron_query",
            "cron_write",
            "self_learning_review",
            "long_memory",
            "long_memory_search",
            "long_memory_write",
            "get_media",
            "view_image",
            "send_file",
            "group_analysis",
            "search_chat_history",
            "get_chat_history_around",
            "reply_to_chat_history_message",
        ):
            self.assertIn(name, names, name)
        # 这六个依赖没给：其余条件组一个都不能注册。
        given_options = (
            "self_learning",
            "chat_history",
            "long_memory_dir",
            "cron_service",
            "group_analysis_service",
            "file_manager",
        )
        for option, group in CONDITIONAL_TOOLS.items():
            if option in given_options:
                continue
            for name in group:
                self.assertNotIn(name, names, f"{name}（守卫 {option}）")

    def test_shell_gets_a_media_runtime_wrapper(self) -> None:
        from atri_x.tool.builtin.file_manager import FileManager
        from atri_x.tool.media_runtime import MediaRuntime

        registry = Registry()
        register_all(
            registry,
            RegisterOptions(
                file_manager=FileManager(sandbox_root=TMP_ROOT, media=FakeCenter())
            ),
        )
        shell = registry.get("shell")[0]
        # Go：MediaRuntime{Center, SandboxRoot}，不是裸 Manager。
        self.assertIsInstance(shell.media, MediaRuntime)
        self.assertIsInstance(shell.media.center, FakeCenter)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
