"""``atri_x.config.state`` 测试：state.toml 读写、热加载、分区合并、[budget] 隔离。

对应上游 ``internal/config/config_test.go`` 的 state 部分与
``internal/agent/state_watch.go`` + ``model.go`` 的 ``refreshRuntimeState`` /
``applyRuntimeState`` / ``runtimeStateDigest`` / ``saveRuntimeState`` 语义。

借助 ``os.utime`` 显式推进 mtime，避免依赖文件系统时间戳精度。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import tomllib
import unittest

from atri_x.config import ModelSelection
from atri_x.config.app_sections import ConfigDecodeError
from atri_x.config.state import (
    RUNTIME_STATE_SECTIONS,
    ContextOverflowConfig,
    GroupKnowledgeEntry,
    GroupPolicyConfig,
    GroupRuntimeConfig,
    RuntimeStateStore,
    StateBudgetConfig,
    StateConfig,
    StateGroupServicesConfig,
    StateModelSnapshot,
    StateSessionConfig,
    canonical_json,
    changed_runtime_state_sections,
    load_state,
    runtime_state_digest,
    save_state,
)
from atri_x.net import tomlw

ROOT = pathlib.Path(__file__).resolve().parent.parent / "tmp_cfg_test"
#: 显式 mtime 基准（2030 年后），保证测试设的 mtime 一定大于 ``save_state`` 的"现在"。
MTIME_BASE = 1_900_000_000_000_000_000
TICK_NS = 1_000_000_000


def _cleanup_case_root(class_name: str) -> None:
    """收掉本测试类的目录与（空掉的）根目录（``tmp_cfg_test/`` 不在 .gitignore 里）。"""
    shutil.rmtree(ROOT / class_name, ignore_errors=True)
    try:
        ROOT.rmdir()
    except OSError:
        pass


def sample_state() -> StateConfig:
    """一份覆盖全部运行态分区的状态。"""
    return StateConfig(
        session=StateSessionConfig(default_mode="chat"),
        mode_models={
            "work": ModelSelection(provider="deepseek", model="deepseek-v4-pro"),
            "chat": ModelSelection(provider="deepseek", model="deepseek-flash"),
        },
        naming_model=ModelSelection(provider="deepseek", model="deepseek-flash"),
        compact_model=ModelSelection(provider="deepseek", model="deepseek-flash"),
        model_snapshots={
            "evening": StateModelSnapshot(
                mode_models={"work": ModelSelection(provider="p", model="m")},
                compact_model=ModelSelection(provider="p", model="c"),
            )
        },
        context_overflow={"cli:local": ContextOverflowConfig(chat="truncate", work="reject")},
        group_policy={
            "qqonebot:group:1": GroupPolicyConfig(
                wake_keywords=["atri", "  ", "atri"],
                response_mode="ALL",
                thread_mode="multi-user",
                merge_window_ms=99999,
                # tool_allowlist 是 Go 的 nil/空切片二态字段：这里用显式 set 标志表达
                # "显式策略"；空列表语义在 TestGroupPolicyNormalization 里单独钉住。
                tool_allowlist_set=True,
                learning_moderation=True,
                image_quota=-5,
            )
        },
        group_runtime={
            "qqonebot:group:1": GroupRuntimeConfig(
                state="muted", reason="kicked", updated_at="2026-01-01T00:00:00Z"
            )
        },
        group_knowledge={
            "qqonebot:group:1": [
                GroupKnowledgeEntry(id="k1", question="怎么用", answer="看文档"),
                GroupKnowledgeEntry(id="k2", question="q", answer="a", enabled=False),
            ]
        },
        group_services=StateGroupServicesConfig(reminders={"qqonebot:group:1": []}),
        budget=StateBudgetConfig(
            reservations={"2026-01-01/image/u1/call1": 1_767_225_600},
            digests={"2026-01-01/chat/u1/call1": "abc"},
            tokens={"2026-01-01/chat/u1": 1234},
            costs={"2026-01-01/chat/u1": 5678},
            retries={"2026-01-01/chat/u1": 1},
            executions={"2026-01-01/image/u1/call1": "done"},
            uncertain={"2026-01-01/chat/u1/call9": 1_767_225_700},
        ),
    )


class StateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = ROOT / type(self).__name__ / self._testMethodName
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.toml"
        self.tick = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        _cleanup_case_root(type(self).__name__)

    # -- 夹具 ---------------------------------------------------------------
    def write_state(self, state: StateConfig | str) -> None:
        """写入 state.toml，并把 mtime 推进到显式基准 + tick 秒。"""
        if isinstance(state, str):
            self.path.write_text(state, encoding="utf-8", newline="\n")
        else:
            tomlw.dump(state.to_dict(), self.path)
        self.bump()

    def bump(self) -> None:
        self.tick += 1
        stamp = MTIME_BASE + self.tick * TICK_NS
        os.utime(self.path, ns=(stamp, stamp))


class TestStateRoundTrip(StateCase):
    def test_to_dict_survives_tomlw_and_tomllib(self) -> None:
        state = sample_state()
        text = tomlw.dumps(state.to_dict())
        parsed = tomllib.loads(text)
        self.assertEqual(parsed, state.to_dict())
        self.assertEqual(StateConfig.from_dict(parsed), state)

    def test_dump_and_load_state_are_equal(self) -> None:
        state = sample_state()
        save_state(self.path, state)
        self.assertEqual(load_state(self.path), state)

    def test_optional_fields_never_write_null(self) -> None:
        state = StateConfig(group_policy={"g": GroupPolicyConfig()})
        text = tomlw.dumps(state.to_dict())
        self.assertNotIn("null", text)
        parsed = tomllib.loads(text)
        self.assertEqual(StateConfig.from_dict(parsed), state)

    def test_group_policy_pointer_defaults_survive_round_trip(self) -> None:
        state = StateConfig(
            group_policy={
                "g": GroupPolicyConfig(learning=False, asr=True, history=False, merge_window_ms=5)
            }
        )
        save_state(self.path, state)
        loaded = load_state(self.path)
        policy = loaded.group_policy["g"]
        self.assertIs(policy.learning, False)
        self.assertIs(policy.asr, True)
        self.assertIs(policy.history, False)
        self.assertEqual(policy.merge_window_ms, 5)
        self.assertIsNone(policy.knowledge)

    def test_tool_allowlist_nil_and_empty_are_distinct(self) -> None:
        """``tool_allowlist = []`` 与"没有这个键"在 Go 里是两回事（nil vs 空切片）。"""
        self.write_state("[group_policy.g]\ntool_allowlist = []\n")
        loaded = load_state(self.path)
        policy = loaded.group_policy["g"]
        self.assertEqual(policy.tool_allowlist, [])
        self.assertTrue(policy.tool_allowlist_restricted())

        self.write_state('[group_policy.g]\nresponse_mode = "all"\n')
        loaded = load_state(self.path)
        self.assertIsNone(loaded.group_policy["g"].tool_allowlist)
        self.assertFalse(loaded.group_policy["g"].tool_allowlist_restricted())

    def test_mode_tool_allowlist_nil_and_empty_survive_round_trip(self) -> None:
        """分模式白名单（本仓扩展）同样要保留 nil / 空列表二态。"""
        state = StateConfig(
            group_policy={
                "g": GroupPolicyConfig(
                    tool_allowlist_chat=[],  # 显式禁止 chat 工具
                    tool_allowlist_chat_set=True,
                    tool_allowlist_work=["web_search"],
                    tool_allowlist_work_set=True,
                ).normalize()
            }
        )
        save_state(self.path, state)
        loaded = load_state(self.path)
        policy = loaded.group_policy["g"]
        # omitempty 会把空列表省略，二态由 ``_set`` 标记保住（与通用字段同一套规则）。
        self.assertIsNone(policy.tool_allowlist_chat)
        self.assertTrue(policy.tool_allowlist_chat_set)
        self.assertEqual(policy.tool_allowlist_work, ["web_search"])
        self.assertEqual(policy.mode_tool_allowlist("chat"), (True, []))
        self.assertEqual(policy.mode_tool_allowlist("work"), (True, ["web_search"]))

        # 没配过的模式：键缺失 → None，回退通用策略。
        self.write_state('[group_policy.g]\nresponse_mode = "all"\n')
        loaded = load_state(self.path)
        self.assertIsNone(loaded.group_policy["g"].tool_allowlist_chat)
        self.assertEqual(loaded.group_policy["g"].mode_tool_allowlist("chat"), (False, None))

    def test_empty_state_round_trip(self) -> None:
        state = StateConfig()
        save_state(self.path, state)
        self.assertEqual(load_state(self.path), state)

    def test_backup_is_used_when_primary_is_missing(self) -> None:
        state = sample_state()
        save_state(self.path, state)
        backup = pathlib.Path(str(self.path) + ".bak")
        shutil.copy(self.path, backup)
        self.path.unlink()
        self.assertEqual(load_state(self.path), state)

    def test_missing_state_returns_none(self) -> None:
        self.assertIsNone(load_state(self.path))

    def test_malformed_state_raises(self) -> None:
        self.write_state("[mode_models.work\nprovider = 1\n")
        with self.assertRaises(ConfigDecodeError):
            load_state(self.path)

    def test_unknown_key_raises(self) -> None:
        self.write_state('[mode_models.work]\nprovider = "p"\nmodel = "m"\ntypo = 1\n')
        with self.assertRaises(ConfigDecodeError) as ctx:
            load_state(self.path)
        self.assertIn("mode_models.work.typo", str(ctx.exception))

    def test_group_knowledge_entry_enabled_defaults_to_true(self) -> None:
        self.write_state('[[group_knowledge.g]]\nid = "k1"\nquestion = "q"\nanswer = "a"\n')
        entry = load_state(self.path).group_knowledge["g"][0]
        self.assertTrue(entry.is_enabled())
        self.assertEqual(entry.id, "k1")


class TestGroupPolicyNormalization(StateCase):
    def test_normalize_is_faithful_to_go(self) -> None:
        policy = GroupPolicyConfig(
            wake_keywords=["a", " a ", "", "b"],
            allowed_models=[" m1 ", "m1"],
            tool_allowlist=[],
            response_mode=" Keywords ",
            default_mode="GROUP",
            thread_mode="shared",
            merge_window_ms=-5,
            image_quota=-3,
            learning_moderation=True,
        ).normalize()
        self.assertEqual(policy.wake_keywords, ["a", "b"])
        self.assertEqual(policy.allowed_models, ["m1"])
        self.assertEqual(policy.tool_allowlist, [])
        self.assertTrue(policy.tool_allowlist_set)
        self.assertEqual(policy.response_mode, "keyword")
        self.assertEqual(policy.default_mode, "")
        self.assertEqual(policy.thread_mode, "group")
        self.assertEqual(policy.merge_window_ms, 0)
        self.assertEqual(policy.image_quota, 0)
        self.assertEqual(policy.learning_moderation_actions, ["view", "decide"])

    def test_merge_window_is_clamped(self) -> None:
        self.assertEqual(
            GroupPolicyConfig(merge_window_ms=99999).normalize().merge_window_ms, 10000
        )
        self.assertEqual(GroupPolicyConfig(merge_window_ms=-1).normalize().merge_window_ms, 0)

    def test_learning_moderation_actions_filtered(self) -> None:
        policy = GroupPolicyConfig(
            learning_moderation=True,
            learning_moderation_actions=["VIEW", "bogus", "delete", "view"],
        ).normalize()
        self.assertEqual(policy.learning_moderation_actions, ["view", "delete"])

    def test_learning_moderation_actions_value_defaults(self) -> None:
        self.assertEqual(
            GroupPolicyConfig(learning_moderation=True).learning_moderation_actions_value(),
            ["view", "decide"],
        )
        self.assertEqual(GroupPolicyConfig().learning_moderation_actions_value(), [])

    def test_response_mode_values(self) -> None:
        for raw, expected in (
            ("all", "all"),
            ("keywords", "keyword"),
            ("replies", "reply"),
            ("none", "off"),
            ("disabled", "off"),
            ("bogus", "mention"),
            ("", "mention"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    GroupPolicyConfig(response_mode=raw).response_mode_value(), expected
                )

    def test_thread_mode_value(self) -> None:
        self.assertEqual(GroupPolicyConfig().thread_mode_value(), "per_user")
        self.assertEqual(GroupPolicyConfig(thread_mode="multi_user").thread_mode_value(), "group")

    def test_is_zero(self) -> None:
        self.assertTrue(GroupPolicyConfig().is_zero())
        self.assertTrue(GroupPolicyConfig(response_mode="mention").is_zero())
        self.assertFalse(GroupPolicyConfig(response_mode="all").is_zero())
        self.assertFalse(GroupPolicyConfig(tool_allowlist=[]).normalize().is_zero())
        self.assertFalse(
            GroupPolicyConfig(tool_allowlist_chat=[], tool_allowlist_chat_set=True)
            .normalize()
            .is_zero()
        )
        self.assertFalse(
            GroupPolicyConfig(tool_allowlist_work=["shell"]).normalize().is_zero()
        )
        self.assertFalse(GroupPolicyConfig(learning=False).is_zero())

    def test_tool_allowlist_restricted(self) -> None:
        self.assertFalse(GroupPolicyConfig().tool_allowlist_restricted())
        self.assertTrue(GroupPolicyConfig(tool_allowlist=[]).tool_allowlist_restricted())
        self.assertTrue(GroupPolicyConfig(tool_allowlist=["shell"]).tool_allowlist_restricted())
        self.assertTrue(
            GroupPolicyConfig(tool_allowlist=None, tool_allowlist_set=True).tool_allowlist_restricted()
        )

    def test_feature_switches_default_to_enabled(self) -> None:
        policy = GroupPolicyConfig()
        for method in (
            policy.is_group_analysis_enabled,
            policy.is_learning_enabled,
            policy.is_history_enabled,
            policy.is_knowledge_enabled,
            policy.is_services_enabled,
            policy.is_asr_enabled,
        ):
            self.assertTrue(method())
        self.assertFalse(GroupPolicyConfig(asr=False).is_asr_enabled())


class TestDigest(StateCase):
    def test_digest_covers_runtime_sections_and_not_budget(self) -> None:
        digest = runtime_state_digest(sample_state())
        self.assertEqual(sorted(digest), sorted(RUNTIME_STATE_SECTIONS))
        self.assertNotIn("budget", digest)
        self.assertNotIn("session", digest)

    def test_changed_sections_only_lists_real_changes(self) -> None:
        state = sample_state()
        before = runtime_state_digest(state)
        self.assertEqual(changed_runtime_state_sections(before, before), [])
        after = dict(before)
        after["group_policy"] = canonical_json({})
        self.assertEqual(changed_runtime_state_sections(before, after), ["group_policy"])

    def test_changed_sections_order_is_stable(self) -> None:
        before = {section: "a" for section in RUNTIME_STATE_SECTIONS}
        after = {section: "b" for section in RUNTIME_STATE_SECTIONS}
        self.assertEqual(changed_runtime_state_sections(before, after), list(RUNTIME_STATE_SECTIONS))

    def test_canonical_json_is_key_order_independent(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), canonical_json({"a": 2, "b": 1}))


class TestHotReload(StateCase):
    def test_unchanged_file_is_not_reapplied(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        report = store.refresh()
        self.assertFalse(report.applied)
        self.assertEqual(report.changed, [])
        self.assertEqual(report.path, str(self.path))

    def test_force_ignores_the_mtime_gate(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        report = store.refresh(force=True)
        self.assertTrue(report.applied)
        self.assertEqual(report.changed, [])  # 内容没变
        self.assertIsNotNone(report.mod_time)

    def test_external_edit_applies_only_runtime_sections(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        store.state.session.default_mode = "work"
        store.state.budget.tokens = {"2026-01-01/chat/u1": 10}

        changed_state = sample_state()
        changed_state.session.default_mode = "chat"  # 非热加载分区：不得生效
        changed_state.mode_models["work"] = ModelSelection(provider="p2", model="m2")
        changed_state.group_runtime = {}
        self.write_state(changed_state)
        report = store.refresh()
        self.assertTrue(report.applied)
        self.assertEqual(report.changed, ["mode_models", "group_runtime"])
        self.assertEqual(store.state.mode_models["work"].model, "m2")
        self.assertEqual(store.state.group_runtime, {})
        # session 与 [budget] 属于进程自己（budget 只在启动时恢复）。
        self.assertEqual(store.state.session.default_mode, "work")
        self.assertEqual(store.state.budget.tokens, {"2026-01-01/chat/u1": 10})

    def test_budget_ledger_is_never_hot_reloaded(self) -> None:
        state = sample_state()
        state.budget = StateBudgetConfig(tokens={"day/chat/u1": 1})
        self.write_state(state)
        store = RuntimeStateStore.open(self.path)
        self.assertEqual(store.state.budget.tokens, {"day/chat/u1": 1})

        store.state.budget.tokens = {"day/chat/u1": 99}
        store.state.budget.reservations = {"day/image/u1/call": 42}
        external = sample_state()
        external.budget = StateBudgetConfig(tokens={"day/chat/u1": 7}, costs={"day/chat/u1": 3})
        self.write_state(external)
        report = store.refresh()
        self.assertTrue(report.applied)
        self.assertNotIn("budget", report.changed)
        self.assertEqual(store.state.budget.tokens, {"day/chat/u1": 99})
        self.assertEqual(store.state.budget.reservations, {"day/image/u1/call": 42})
        self.assertEqual(store.state.budget.costs, {})

    def test_merge_external_is_alias_of_refresh(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        external = sample_state()
        external.context_overflow = {"g": ContextOverflowConfig(chat="summarize")}
        self.write_state(external)
        report = store.merge_external()
        self.assertTrue(report.applied)
        self.assertEqual(report.changed, ["context_overflow"])
        self.assertEqual(store.state.context_overflow["g"].chat, "summarize")

    def test_missing_file_is_a_no_op(self) -> None:
        store = RuntimeStateStore.open(self.path)
        report = store.refresh()
        self.assertFalse(report.applied)
        self.assertEqual(report.path, str(self.path))

    def test_unknown_provider_selection_still_merges(self) -> None:
        # provider 校验属于 app 层（Go 在 agent.applyRuntimeState 里做）；配置层只做合并。
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        external = sample_state()
        external.mode_models["work"] = ModelSelection(provider="ghost", model="m")
        self.write_state(external)
        self.assertTrue(store.refresh().applied)
        self.assertEqual(store.state.mode_models["work"].provider, "ghost")

    def test_apply_replaces_partitions_wholesale(self) -> None:
        """分区是整体替换（Go ``setGroupXxxSnapshot``），不是逐键合并。"""
        state = sample_state()
        state.group_knowledge = {"g1": [GroupKnowledgeEntry(id="a", question="q", answer="a")]}
        self.write_state(state)
        store = RuntimeStateStore.open(self.path)
        external = sample_state()
        external.group_knowledge = {"g2": [GroupKnowledgeEntry(id="b", question="q", answer="a")]}
        self.write_state(external)
        store.refresh()
        self.assertEqual(sorted(store.state.group_knowledge), ["g2"])


class TestWriteBack(StateCase):
    def test_save_merges_external_edit_before_writing(self) -> None:
        state = sample_state()
        state.group_policy = {}
        self.write_state(state)
        store = RuntimeStateStore.open(self.path)

        external = sample_state()
        external.group_policy = {"g": GroupPolicyConfig(response_mode="all")}
        external.mode_models["work"] = ModelSelection(provider="deepseek", model="deepseek-v4-pro")
        self.write_state(external)

        # 契约：先合并外部编辑，再改内存，最后落盘。
        store.merge_external()
        store.state.model_snapshots["night"] = StateModelSnapshot(
            mode_models={"chat": ModelSelection(provider="p", model="m")}
        )
        store.save()

        reloaded = load_state(self.path)
        self.assertIn("night", reloaded.model_snapshots)
        self.assertEqual(reloaded.group_policy["g"].response_mode, "all")
        self.assertEqual(reloaded.mode_models["work"].model, "deepseek-v4-pro")

    def test_save_alone_merges_pending_external_edit(self) -> None:
        # save() 里也必须先合并：预算是内存独占的那一份，不先改内存也不能丢外部编辑。
        store = RuntimeStateStore(self.path)
        store.state.budget.tokens = {"day/chat/u1": 5}
        self.write_state(sample_state())
        store.save()
        reloaded = load_state(self.path)
        self.assertEqual(reloaded.group_policy["qqonebot:group:1"].response_mode, "ALL")
        self.assertEqual(reloaded.budget.tokens, {"day/chat/u1": 5})

    def test_local_mutation_before_save_is_overwritten_by_pending_edit(self) -> None:
        """固定 Go 的既有契约：调用方必须**先合并、再改内存**。

        先改内存再 save()，会被 save() 内部的合并用文件分片覆盖（Go 的
        ``mergeExternalRuntimeState`` 注释明确写了这一点：命令报成功却丢掉改动）。
        这个用例是防止有人「顺手」把 save() 里的合并去掉。
        """
        state = sample_state()
        state.mode_models["work"] = ModelSelection(provider="deepseek", model="old")
        self.write_state(state)
        store = RuntimeStateStore.open(self.path)

        store.state.mode_models["work"] = ModelSelection(provider="deepseek", model="new")
        external = sample_state()
        external.mode_models["work"] = ModelSelection(provider="deepseek", model="external")
        self.write_state(external)

        store.save()  # 未先 merge_external
        self.assertEqual(load_state(self.path).mode_models["work"].model, "external")

    def test_memory_wins_when_file_is_not_newer(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        store.state.mode_models["work"] = ModelSelection(provider="deepseek", model="local")
        store.save()
        self.assertEqual(load_state(self.path).mode_models["work"].model, "local")

    def test_save_creates_missing_directories(self) -> None:
        nested = self.dir / "nested" / "state.toml"
        store = RuntimeStateStore(nested)
        store.state.mode_models["work"] = ModelSelection(provider="p", model="m")
        store.save()
        self.assertTrue(nested.is_file())
        self.assertEqual(load_state(nested).mode_models["work"].model, "m")

    def test_open_restores_budget_from_file(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        self.assertEqual(store.state.budget.tokens, {"2026-01-01/chat/u1": 1234})
        self.assertEqual(
            store.state.budget.reservations, {"2026-01-01/image/u1/call1": 1_767_225_600}
        )

    def test_replace_runtime_sections_leaves_budget(self) -> None:
        store = RuntimeStateStore(self.path)
        store.state.budget.costs = {"day/chat": 9}
        store.replace_runtime_sections(sample_state())
        self.assertEqual(store.state.mode_models["work"].model, "deepseek-v4-pro")
        self.assertEqual(store.state.budget.costs, {"day/chat": 9})


class TestStatus(StateCase):
    def test_pending_before_first_load_and_after_external_edit(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore(self.path)
        self.assertTrue(store.status().pending)
        store.refresh()
        self.assertFalse(store.status().pending)

        self.write_state(sample_state())
        self.assertTrue(store.status().pending)
        store.refresh()
        self.assertFalse(store.status().pending)

    def test_status_reports_mtimes(self) -> None:
        self.write_state(sample_state())
        store = RuntimeStateStore.open(self.path)
        status = store.status()
        self.assertEqual(status.path, str(self.path))
        self.assertIsNotNone(status.file_mod_time)
        self.assertEqual(status.loaded_mod_time, status.file_mod_time)
        self.assertEqual(status.file_mod_time.tzinfo, dt.timezone.utc)

    def test_status_without_file(self) -> None:
        store = RuntimeStateStore(self.path)
        status = store.status()
        self.assertIsNone(status.file_mod_time)
        self.assertIsNone(status.loaded_mod_time)
        self.assertTrue(status.pending)


if __name__ == "__main__":
    unittest.main()
