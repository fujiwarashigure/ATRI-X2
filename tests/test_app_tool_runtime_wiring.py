"""``atri_x.app.tool_runtime.build_tool_runtime`` 的真实装配回归。

对应 Go：``internal/tool/builtin/runtime.go`` 的 ``NewRuntime`` 与
``internal/tool/builtin/register.go`` 的 ``RegisterAll``。

这一组用例刻意**不走假宿主**（上一轮 P0 正是被假宿主掩盖的）：

* ``build_tool_runtime`` 是真实函数，装配的是真实 :class:`atri_x.tool.tool.Registry`；
* 依赖给的是**真实服务实现** —— angelmemory / selflearning / character / artist /
  imagegen / groupanalysis / cron / vision / media / sqlite 存储都用本仓正式实现，
  临时状态全部落在仓库根 ``tmp_app_tool_runtime_wiring_test/``（禁用
  ``tempfile.mkdtemp``，见 AGENTS.md）；
* 只有「配置段」是构造出来的：真实 config dataclass 的实例（Go 里也是这些结构体）。

三条断言（与 Go 逐条对照）：

1. 依赖给全时，``registry`` 的工具名集合**恰好** == ``set(REGISTERED_TOOLS)``，
   ``missing=0`` / ``extra=0`` —— 少一个就说明某个 ``RegisterOptions`` 字段没接上线，
   多一个就说明某个守卫没生效；
2. 只给 ``config_dir`` 时，只有 ``NewRuntime`` 自己硬编码提供的那几组会注册，
   其余每一个条件组**一个都不注册**（守卫真的生效，没有被"一律注册"掩盖）；
3. skill 6 工具：manager 可用时 6 个全在，manager 缺失时只剩无守卫的
   ``agent_skill`` / ``go_skill_run``（Go ``register.go:229-250``）。
"""

from __future__ import annotations

import os
import shutil
import unittest
from typing import Any
from unittest import mock

from atri_x.app.tool_runtime import (
    ToolRuntime,
    ToolRuntimeOptions,
    build_tool_runtime,
)
from atri_x.config import (
    AngelMemoryConfig,
    ArtistLibraryConfig,
    CharacterLibraryConfig,
    FileDeliveryConfig,
    GroupAnalysisConfig,
    ImageGenerationConfig,
    ImageGenerationProfileConfig,
    SelfLearningConfig,
)
from atri_x.cron import Manager as CronManager
from atri_x.cron import Options as CronOptions
from atri_x.cron import Service as CronService
from atri_x.processenv import Environment
from atri_x.storage.sqlite import store as sqlite_store
from atri_x.storage.sqlite.chat_history_repository import new_chat_history
from atri_x.tool.builtin.register import (
    ALWAYS_REGISTERED_TOOLS,
    CONDITIONAL_TOOLS,
    REGISTERED_TOOLS,
    UNPORTED_TOOLS,
    RegisterOptions,
    new_registry_with_builtin,
    register_all,
)
from atri_x.tool.builtin.runtimeinfo import RuntimeInfo
from atri_x.tool.media_runtime import MediaRuntime
from atri_x.tool.skill import Manager as SkillManager
from atri_x.tool.skill import new_manager
from atri_x.tool.tool import Registry
from atri_x.utils.fileops import new_rollback_store
from atri_x.vision import Options as VisionOptions
from atri_x.vision import new as new_vision

#: 临时目录放仓库根（PORTING §7.2：禁用 tempfile.mkdtemp）。
TMP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tmp_app_tool_runtime_wiring_test",
)

#: Go ``runtime.go`` 里 ``NewRuntime`` **自己**提供、因此生产路径上恒为非空的依赖。
#: 它们对应的条件组不是"可能缺席"，而是"必然出现"：
#: ``residentStore``（runtime.go:98）、``fileManager``（:195）、``skillManager``（:194）、
#: ``LongMemoryDir``（:219）。
ALWAYS_SUPPLIED_OPTIONS: tuple[str, ...] = (
    "resident_memory_store",
    "file_manager",
    "skill_manager",
    "long_memory_dir",
)

#: 上一轮 P0 漏掉的 8 组条件工具（缺字段 → 整组静默消失）。
GAP_TOOL_GROUPS: tuple[str, ...] = (
    "self_learning",
    "cron_service",
    "group_analysis_service",
    "character_store",
    "image_profiles",
    "artists",
    "image_prompt_service",
)


def _make_full_options(root: str) -> tuple[ToolRuntimeOptions, list[Any]]:
    """全依赖的 ``ToolRuntimeOptions``；返回 (options, 需要关闭的句柄)。"""
    for name in ("sandbox", "characters", "skills", "media"):
        os.makedirs(os.path.join(root, name), exist_ok=True)

    history_store = new_chat_history(os.path.join(root, "history.db"))
    storage = sqlite_store.new(os.path.join(root, "sessions.db"))
    media_center = new_media_manager(
        storage, os.path.join(root, "media"), FileDeliveryConfig(), None
    )
    image_generation = ImageGenerationConfig(
        enabled=True,
        base_url="https://image.invalid/v1",
        api_key="wiring-test-key",
        model="wiring-base-model",
        default_profile="main",
        profiles={
            "main": ImageGenerationProfileConfig(
                enabled=True, model="wiring-profile-model"
            )
        },
    )
    options = ToolRuntimeOptions(
        config_dir=root,
        # 生产形态：``app/runtime.py`` 的 ``_tool_options`` 就是不带 ``config_dir``
        # （由 ``_normalize_info`` 从 ``opts.config_dir`` 补），这里刻意保持一致。
        runtime_info=RuntimeInfo(
            config_path=os.path.join(root, "app.toml"),
            sandbox_root=os.path.join(root, "sandbox"),
            file_delivery=FileDeliveryConfig(),
        ),
        cron_service=CronService(CronOptions(manager=CronManager(repo=storage.cron_jobs()))),
        chat_history=history_store.repository(),
        outbound_messages=history_store.outbound(),
        group_analysis=GroupAnalysisConfig(max_messages=120),
        angel_memory=AngelMemoryConfig(allow_tool_forget=True),
        self_learning=SelfLearningConfig(),
        store=storage,
        media=media_center,
        character_enabled=True,
        character_root=os.path.join(root, "characters"),
        artist_library_enabled=True,
        artist_library_root=os.path.join(root, "artists.json"),
        artist_metadata_store=new_generation_store(os.path.join(root, "generations.json")),
        image_generation=image_generation,
        image_prompt_service=new_vision(VisionOptions()),
        process_env=Environment(),
        child_process_env=Environment(),
        file_backups=new_rollback_store(),
    )
    return options, [history_store, storage]


def new_media_manager(*args: Any, **kwargs: Any) -> Any:
    from atri_x.media import new_configured

    return new_configured(*args, **kwargs)


def new_generation_store(path: str) -> Any:
    from atri_x.artist import new_generation_store as factory

    return factory(path)


def _names(runtime: ToolRuntime) -> set[str]:
    return {info.name for info in runtime.registry.list()}


def _tool(runtime: ToolRuntime, name: str) -> Any:
    tool, ok = runtime.registry.get(name)
    if not ok:
        raise AssertionError(f"tool {name!r} is not registered")
    return tool


class _TmpDirTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        # LIFO：先关句柄（下面注册），最后删目录。
        self.addCleanup(shutil.rmtree, TMP_DIR, ignore_errors=True)
        self.options: ToolRuntimeOptions | None = None

    def build_full(self) -> ToolRuntime:
        options, closers = _make_full_options(TMP_DIR)
        self.options = options
        for closer in closers:
            self.addCleanup(closer.close)
        runtime = build_tool_runtime(options)
        # 服务自己开的 SQLite 句柄由 ToolRuntime 持有，测试负责关掉（Windows 上
        # 未关的连接会让 tmp 目录删不干净，也会在 GC 时报 ResourceWarning）。
        for store in (runtime.angel_memory_store, runtime.self_learning_store):
            if store is not None:
                self.addCleanup(store.close)
        return runtime


class FullWiringTest(_TmpDirTest):
    """依赖给全：``register_all`` 的每一条都必须出现。"""

    def test_registry_equals_registered_tools(self) -> None:
        runtime = self.build_full()
        names = _names(runtime)
        expected = set(REGISTERED_TOOLS)
        # missing / extra 分开断言，失败时能立刻看出方向。
        self.assertEqual(sorted(expected - names), [], "missing tools")
        self.assertEqual(sorted(names - expected), [], "extra tools")
        self.assertEqual(names, expected)
        # Go ``register.go`` 注册的全部工具数 + Python 新增（``fetch_message`` /
        # ``get_current_time`` / ``character_import`` / ``character_activate`` /
        # ``persona_control``）。1.1.3 修订：角色库一组新增 3 个工具。
        self.assertEqual(len(REGISTERED_TOOLS), 54)
        self.assertEqual(len(names), len(REGISTERED_TOOLS))

    def test_no_dependency_degraded_silently(self) -> None:
        """任何 warning 都意味着某个依赖没建起来 → 对应工具组会静默缺席。"""
        runtime = self.build_full()
        self.assertEqual(runtime.warnings, [])

    def test_previous_gap_groups_are_all_present(self) -> None:
        """上一轮 P0 的 8 组条件工具：每一组都必须真的注册上。"""
        runtime = self.build_full()
        names = _names(runtime)
        for option in GAP_TOOL_GROUPS:
            group = CONDITIONAL_TOOLS[option]
            self.assertTrue(group, option)
            for name in group:
                self.assertIn(name, names, f"{name}（守卫 {option}）")
        for name in (
            "self_learning_review",
            "cron",
            "cron_query",
            "cron_write",
            "group_analysis",
            "character_list",
            "image_generate",
            "artist_library_search",
            "image_to_prompt",
            "long_memory",
            "long_memory_search",
            "long_memory_write",
        ):
            self.assertIn(name, names, name)

    def test_dependencies_reach_the_image_generate_tool(self) -> None:
        """``image_generate`` 一次性消费 7 个 ``RegisterOptions`` 字段（含 artists）。"""
        runtime = self.build_full()
        tool = _tool(runtime, "image_generate")
        self.assertEqual(runtime.default_image_profile, "main")
        self.assertIs(tool.client, runtime.image_profiles["main"].client)
        self.assertIs(tool.cfg, runtime.image_profiles["main"].config)
        self.assertIs(tool.artists, runtime.artists)
        self.assertIs(tool.limiter, runtime.image_limiter)
        self.assertIs(tool.characters, runtime.character_store)
        self.assertIs(tool.history, self.options.chat_history)

    def test_image_profiles_are_resolved_profiles_not_raw_clients(self) -> None:
        """``ImageProfiles`` 的元素必须是 ``ImageProfile``，且配置来自 profile 段。"""
        runtime = self.build_full()
        self.assertEqual(sorted(runtime.image_profiles), ["", "main"])
        base = runtime.image_profiles[""]
        named = runtime.image_profiles["main"]
        self.assertEqual(base.name, "")
        self.assertEqual(base.client.config.model, "wiring-base-model")
        self.assertIs(base.config, base.client.config)
        self.assertEqual(named.name, "main")
        self.assertEqual(named.config.model, "wiring-profile-model")

    def test_media_runtime_reaches_shell_and_skill_runner(self) -> None:
        """Go ``register.go:54-59`` / ``:218`` / ``:233``：同一个 mediaRuntime。"""
        runtime = self.build_full()
        shell = _tool(runtime, "shell")
        runner = _tool(runtime, "go_skill_run")
        self.assertIsInstance(shell.media, MediaRuntime)
        self.assertIsInstance(runner.media, MediaRuntime)
        self.assertIs(shell.media, runner.media)
        self.assertIs(runtime.skill_manager.scanner.media, runner.media)
        self.assertIs(shell.media.center, runtime.file_manager.media)

    def test_artists_use_the_real_library_and_vision_service(self) -> None:
        runtime = self.build_full()
        self.assertTrue(runtime.artists.enabled())
        self.assertIs(runtime.artists.media, runtime.file_manager.media)
        self.assertIs(runtime.artists.vision, runtime.registry.get("image_to_prompt")[0].service)
        self.assertIsNotNone(runtime.artists.service.generations)

    def test_services_are_the_ones_handed_to_register_options(self) -> None:
        runtime = self.build_full()
        self.assertIs(_tool(runtime, "self_learning_review").service, runtime.self_learning)
        self.assertIs(_tool(runtime, "cron").service, self.options.cron_service)
        self.assertIs(_tool(runtime, "group_analysis").service, runtime.group_analysis)
        self.assertIs(_tool(runtime, "angel_remember").service, runtime.angel_memory)
        self.assertEqual(_tool(runtime, "group_analysis").max_messages, 120)
        self.assertIs(_tool(runtime, "send_file").files, runtime.file_manager)

    def test_production_runtime_info_is_normalized_not_mutated(self) -> None:
        """``Info`` 是 frozen 的：``_normalize_info`` 必须造新实例（否则启动即崩）。"""
        runtime = self.build_full()
        info = _tool(runtime, "atriwisp_creator").runtime_info
        self.assertEqual(info.config_dir, TMP_DIR)
        self.assertEqual(info.sandbox_root, os.path.join(TMP_DIR, "sandbox"))

    def test_skill_file_guard_rule_is_installed(self) -> None:
        """Go ``register.go:193-195``：skill 根目录也要进 FileGuard。"""
        runtime = self.build_full()
        guard = _tool(runtime, "read_file").file_guard
        names = {rule.name for rule in guard.rules}
        self.assertIn("atrix_skill", names)
        self.assertIn("resident_memory", names)
        self.assertIn("long_memory", names)
        by_name = {rule.name: rule for rule in guard.rules}
        self.assertEqual(by_name["atrix_skill"].root, runtime.skill_manager.root)


class MinimalWiringTest(_TmpDirTest):
    """只给 ``config_dir``：除了 ``NewRuntime`` 硬编码提供的那几组，其余全部缺席。"""

    def _build(self) -> ToolRuntime:
        return build_tool_runtime(ToolRuntimeOptions(config_dir=TMP_DIR))

    def test_only_hardcoded_dependencies_register(self) -> None:
        runtime = self._build()
        names = _names(runtime)
        expected = set(ALWAYS_REGISTERED_TOOLS)
        for option in ALWAYS_SUPPLIED_OPTIONS:
            expected |= set(CONDITIONAL_TOOLS[option])
        self.assertEqual(sorted(names - expected), [], "extra tools")
        self.assertEqual(sorted(expected - names), [], "missing tools")
        # 计数：14 无条件（含 fetch_message）+ 4 resident + 3 long_memory + 1
        # send_file + 4 skill = 26。
        self.assertEqual(len(names), 27)

    def test_every_config_dependent_guard_stays_closed(self) -> None:
        runtime = self._build()
        names = _names(runtime)
        for option, group in CONDITIONAL_TOOLS.items():
            if option in ALWAYS_SUPPLIED_OPTIONS:
                continue
            for name in group:
                self.assertNotIn(name, names, f"{name}（守卫 {option}）")

    def test_register_all_minimal_is_exactly_the_unconditional_set(self) -> None:
        """``RegisterOptions()`` 一个依赖都不给：恰好是无条件那 14 个。"""
        registry = new_registry_with_builtin(RegisterOptions())
        names = {info.name for info in registry.list()}
        self.assertEqual(names, set(ALWAYS_REGISTERED_TOOLS))
        self.assertEqual(len(ALWAYS_REGISTERED_TOOLS), 15)


class SkillWiringTest(_TmpDirTest):
    """Go ``register.go:225-250`` 的 6 个 skill 工具与 ``SkillManager`` 守卫。"""

    SKILL_TOOLS = (
        "agent_skill",
        "go_skill_run",
        "create_atrix_skill",
        "read_atrix_skill",
        "modify_atrix_skill",
        "finalize_atrix_skill",
    )

    def test_manager_present_registers_all_six(self) -> None:
        runtime = self.build_full()
        names = _names(runtime)
        for name in self.SKILL_TOOLS:
            self.assertIn(name, names, name)
        manager = runtime.skill_manager
        self.assertIsInstance(manager, SkillManager)
        self.assertEqual(manager.root, os.path.join(TMP_DIR, "skills"))
        for name in self.SKILL_TOOLS:
            tool = _tool(runtime, name)
            if name == "go_skill_run":
                # Go：``skill.NewGoRunner(catalog, shellEnv)`` 共享同一个 catalog。
                self.assertIs(tool.catalog, manager.catalog, name)
            else:
                self.assertIs(tool.manager, manager, name)

    def test_register_all_without_manager_keeps_only_the_unguarded_two(self) -> None:
        registry = Registry()
        register_all(registry, RegisterOptions())
        names = {info.name for info in registry.list()}
        self.assertIn("agent_skill", names)
        self.assertIn("go_skill_run", names)
        for name in CONDITIONAL_TOOLS["skill_manager"]:
            self.assertNotIn(name, names, name)

    def test_runtime_without_manager_keeps_only_the_unguarded_two(self) -> None:
        """manager 建不起来时，``build_tool_runtime`` 也必须只留无守卫的两个。"""
        import atri_x.app.tool_runtime as module

        with mock.patch.object(module, "_build_skill_manager", return_value=None):
            runtime = build_tool_runtime(ToolRuntimeOptions(config_dir=TMP_DIR))
        names = _names(runtime)
        self.assertIsNone(runtime.skill_manager)
        self.assertIn("agent_skill", names)
        self.assertIn("go_skill_run", names)
        for name in CONDITIONAL_TOOLS["skill_manager"]:
            self.assertNotIn(name, names, name)

    def test_manager_created_before_registration(self) -> None:
        """顺序回归：manager 必须在 ``register_all`` 之前建好。

        ``discover_tool`` 的 ``before_discover`` 钩子就是 manager 的
        ``ensure_loaded``（Go ``register.go:60-63``）；它非空即证明注册时 manager 已存在，
        而 ``Manager.registry`` 又必须是同一个 registry。
        """
        runtime = self.build_full()
        manager = runtime.skill_manager
        self.assertIs(manager.registry, runtime.registry)
        discover = _tool(runtime, "discover_tool")
        self.assertEqual(discover.before_discover, manager.ensure_loaded)
        # manager 建好时就已经指向真正要用的 skill 根目录（扫描期注册要用它）。
        self.assertEqual(manager.scanner.root, os.path.join(TMP_DIR, "skills"))
        self.assertIs(manager.catalog, manager.scanner.catalog)

    def test_unported_list_is_empty_after_wiring(self) -> None:
        self.assertEqual(UNPORTED_TOOLS, ())
        for name in self.SKILL_TOOLS:
            self.assertIn(name, set(REGISTERED_TOOLS), name)


class ArtistMetadataAssemblyCase(_TmpDirTest):
    """``[image_generation].metadata_store`` 必须真的变成入库前的留档库。

    回归背景（1.1.0 实机）：:func:`atri_x.app.runtime._build_artist_metadata` 原来从
    ``atri_x.artist.metadata`` 导入 ``GenerationStore``——那里没有它——``ImportError``
    又被裸 ``except`` 吞掉，于是**配置了 metadata_store 也永远是 None**：入库转码把
    PNG 文本块销毁后，``artist_extract`` / ``image_to_prompt`` 再也取不回画师串与
    提示词，群里只会得到"图片已被转码、没有参数"的答案。
    """

    def _config(self, metadata_store: str) -> Any:
        from atri_x.config import AppConfig

        cfg = AppConfig()
        cfg.image_generation.metadata_store = metadata_store
        return cfg

    def test_store_is_assembled_from_config(self) -> None:
        from atri_x.app.runtime import _build_artist_metadata
        from atri_x.artist import GenerationStore

        path = os.path.join(TMP_DIR, "image_metadata.json")
        store = _build_artist_metadata(self._config(path))
        self.assertIsInstance(store, GenerationStore)
        self.assertTrue(store.enabled())
        self.assertEqual(store.backing_path(), path)

    def test_store_is_none_when_not_configured(self) -> None:
        from atri_x.app.runtime import _build_artist_metadata

        self.assertIsNone(_build_artist_metadata(self._config("")))

    def test_artist_tools_carry_the_store(self) -> None:
        """工具回读走 ``ArtistTools.Service.Generations``，必须与入库侧同一个对象。"""
        from atri_x.app.tool_runtime import _build_artists

        options, closers = _make_full_options(TMP_DIR)
        for closer in closers:
            self.addCleanup(closer.close)
        warnings: list[str] = []
        artists = _build_artists(options, warnings)
        self.assertEqual(warnings, [])
        self.assertIs(artists.service.generations, options.artist_metadata_store)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
