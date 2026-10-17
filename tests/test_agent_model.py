"""``atri_x.agent`` 模型管理组（model / model_list / model_snapshot / model_policy）的测试。

对应 Go：``internal/agent/model_test.go`` / ``model_snapshot_test.go`` 的核心路径。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.agent.model import (
    ModelRuntimeState,
    clone_mode_models,
    format_delay,
    model_selection_key,
    new_model_runtime_state,
)
from atri_x.agent.model_agent import ModelMixin
from atri_x.agent.model_service import ModelService, install_model_services
from atri_x.agent.model_list import (
    MODEL_LIST_ERROR_TTL,
    MODEL_LIST_SUCCESS_TTL,
    ModelListCache,
    ModelListMixin,
    merge_model_names,
    model_profile_name_exists,
    model_profiles_of,
)
from atri_x.agent.model_policy import authorize_execution_model_selection
from atri_x.agent.model_snapshot import (
    ModelSnapshotMixin,
    ModelSnapshotStore,
    model_snapshot_view,
    normalize_model_snapshot_name,
    ordered_model_snapshot_modes,
)
from atri_x.config import (
    GroupPolicyConfig,
    ModelSelection,
    ProviderConfig,
    StateModelSnapshot,
)
from atri_x.errors import ErrModelSnapshotNameEmpty, InvalidError, NotFoundError

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_agent_model_test")


class _Session:
    def __init__(self, mode: str = "work", metadata: str = "", session_id: str = "s1") -> None:
        self.id = session_id
        self.mode = mode
        self.metadata = metadata


class _Sessions:
    def __init__(self, current: _Session | None = None) -> None:
        self._current = current

    def current(self, scope: object) -> _Session:
        if self._current is None:
            raise NotFoundError("no current session")
        return self._current

    def default_mode(self) -> str:
        return "work"


class _FakeHost(ModelMixin, ModelListMixin, ModelSnapshotMixin):
    """最小宿主：只有模型组需要的那几个成员。"""

    def __init__(
        self,
        providers: dict[str, ProviderConfig] | None = None,
        clients: dict[str, object] | None = None,
        mode_models: dict[str, ModelSelection] | None = None,
    ) -> None:
        self.providers = dict(providers or {})
        self.clients = dict(clients or {})
        self.model_runtime = new_model_runtime_state(
            None, "", "", None, self.providers, mode_models or {}, self.clients
        )
        self.sessions = _Sessions(None)
        self.model_profiles = {}
        self.model_aliases = {}
        self.audit_events: list[tuple[str, dict]] = []
        self.services: dict[str, object] = {}
        self.saved = 0

    # -- 宿主契约里的少数几个 ------------------------------------------------
    def scope(self, ctx: object) -> object:
        return None

    def command_prefix(self) -> str:
        return "/"

    def client_for_provider(self, provider: str) -> object:
        return self.clients.get(provider)

    def merge_external_runtime_state(self) -> None:
        return None

    def save_runtime_state(self) -> None:
        self.saved += 1

    def current_compact_selection(self) -> ModelSelection:
        return ModelSelection()

    def set_compact_selection(self, selection: ModelSelection) -> None:
        self._compact = selection

    def set_naming_model(self, selection: ModelSelection) -> None:
        self.naming_model = selection

    def configured_naming_model(self) -> ModelSelection:
        return getattr(self, "naming_model", ModelSelection())

    def check_model_switch(self, ctx: object, mode: str, provider: str, model: str) -> None:
        return None

    def apply_mode_model_selection(self, mode: str, provider: str, model: str):
        self.model_runtime.apply_mode_model_selection(mode, provider, model)

    def client_for_provider_or_none(self, provider: str) -> object:
        return self.clients.get(provider)


class TestBudgetKeyFreeHelpers(unittest.TestCase):
    def test_clone_mode_models_is_a_copy(self) -> None:
        source = {"work": ModelSelection(provider="p", model="m")}
        cloned = clone_mode_models(source)
        cloned["work"].model = "other"
        self.assertEqual(source["work"].model, "m")
        self.assertEqual(cloned["chat"] if "chat" in cloned else None, None)
        self.assertEqual(clone_mode_models(None), {})

    def test_model_selection_key(self) -> None:
        self.assertEqual(model_selection_key("p", "m"), "p\u0000m")

    def test_format_delay_matches_go_duration(self) -> None:
        self.assertEqual(format_delay(0), "0s")
        self.assertEqual(format_delay(0.25), "250ms")
        self.assertEqual(format_delay(2), "2s")
        self.assertEqual(format_delay(1.5), "1.5s")


class TestModelRuntimeState(unittest.TestCase):
    def test_model_for_mode_falls_back_to_work(self) -> None:
        state = ModelRuntimeState(
            None, "", "", None, {"p": ProviderConfig()},
            {"work": ModelSelection(provider="p", model="w")}, {},
        )
        self.assertEqual(state.model_for_mode("chat").model, "w")
        self.assertEqual(state.model_for_mode("work").model, "w")

    def test_apply_mode_model_selection_updates_primary(self) -> None:
        providers = {"p": ProviderConfig(), "q": ProviderConfig()}
        state = ModelRuntimeState(None, "", "", None, providers, {}, {"q": object()})
        provider = state.apply_mode_model_selection("chat", "q", "m2")
        self.assertIs(provider, providers["q"])
        self.assertEqual(state.selection()[0], "q")
        self.assertEqual(state.selection()[1], "m2")
        self.assertIsNotNone(state.selection()[2])

    def test_apply_mode_model_selection_rejects_unknown_provider(self) -> None:
        state = ModelRuntimeState()
        with self.assertRaises(NotFoundError):
            state.apply_mode_model_selection("work", "nope", "m")

    def test_compat_aliases(self) -> None:
        state = ModelRuntimeState()
        state.client = "c"
        state.provider_config = "p"
        self.assertEqual(state.llm_client, "c")
        self.assertEqual(state.provider, "p")


class TestModelListCache(unittest.IsolatedAsyncioTestCase):
    async def test_success_is_cached(self) -> None:
        calls = {"n": 0}

        async def fetch(provider: str):
            calls["n"] += 1
            return ["b", "a"], None

        cache = ModelListCache()
        self.assertEqual(await cache.load("p", False, fetch), (["b", "a"], None))
        self.assertEqual(await cache.load("p", False, fetch), (["b", "a"], None))
        self.assertEqual(calls["n"], 1)
        self.assertGreater(cache.cache["p"].expires_at, 0)
        self.assertAlmostEqual(
            cache.cache["p"].expires_at - cache.clock(), MODEL_LIST_SUCCESS_TTL, delta=1
        )

    async def test_fresh_bypasses_cache(self) -> None:
        calls = {"n": 0}

        async def fetch(provider: str):
            calls["n"] += 1
            return [str(calls["n"])], None

        cache = ModelListCache()
        await cache.load("p", False, fetch)
        result = await cache.load("p", True, fetch)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result[0], ["2"])

    async def test_error_keeps_last_good(self) -> None:
        state = {"fail": False}

        async def fetch(provider: str):
            if state["fail"]:
                return [], RuntimeError("boom")
            return ["a", "b"], None

        cache = ModelListCache()
        await cache.load("p", False, fetch)
        state["fail"] = True
        models, err = await cache.load("p", True, fetch)
        self.assertIsInstance(err, RuntimeError)
        # 上次的好结果合并回来，错误也照样返回。
        self.assertEqual(models, ["a", "b"])
        entry = cache.cache["p"]
        self.assertAlmostEqual(
            entry.expires_at - cache.clock(), MODEL_LIST_ERROR_TTL, delta=1
        )
        self.assertEqual(entry.last_good, ["a", "b"])

    async def test_concurrent_loads_share_one_fetch(self) -> None:
        calls = {"n": 0}

        async def fetch(provider: str):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return ["x"], None

        cache = ModelListCache()
        results = await asyncio.gather(
            cache.load("p", True, fetch), cache.load("p", True, fetch)
        )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(results[0], (["x"], None))
        self.assertEqual(results[1], (["x"], None))

    def test_merge_model_names(self) -> None:
        self.assertEqual(merge_model_names(["b"], []), ["b"])
        self.assertEqual(merge_model_names(["b"], ["a", "b", ""]), ["a", "b"])


class TestModelListMixin(unittest.IsolatedAsyncioTestCase):
    async def test_model_options_marks_and_filter(self) -> None:
        providers = {"p": ProviderConfig(models=["m1", "m2"]), "q": ProviderConfig(models=["m3"])}
        host = _FakeHost(providers, {"p": object(), "q": object()}, {"work": ModelSelection("p", "m2")})
        result = await host.model_options("", None)
        self.assertEqual([o.model for o in result.options], ["m1", "m2", "m3"])
        by_name = {o.model: o for o in result.options}
        self.assertTrue(by_name["m2"].current)
        self.assertTrue(by_name["m2"].work_current)
        self.assertFalse(by_name["m1"].current)
        # 序号在过滤前分配（与 Go 一致）
        filtered = await host.model_options("m3", None)
        self.assertEqual([o.index for o in filtered.options], [3])
        self.assertEqual(host.model_runtime.all_models, ["m3"])

    async def test_select_model_option_by_index_and_name(self) -> None:
        providers = {"p": ProviderConfig(models=["m1", "m2"])}
        host = _FakeHost(providers, {"p": object()})
        selected = await host.select_model_option("2")
        self.assertEqual((selected.provider, selected.model), ("p", "m2"))
        selected = await host.select_model_option("p/m1")
        self.assertEqual(selected.model, "m1")
        with self.assertRaises(InvalidError):
            await host.select_model_option("3")
        with self.assertRaises(InvalidError):
            await host.select_model_option("missing")
        with self.assertRaises(InvalidError):
            await host.select_model_option("")

    async def test_select_model_for_mode_persists(self) -> None:
        providers = {"p": ProviderConfig(models=["m1"])}
        host = _FakeHost(providers, {"p": object()})
        option = await host.select_model_for_mode(None, "chat", "m1")
        self.assertTrue(option.chat_current)
        self.assertEqual(host.model_for_mode("chat").model, "m1")
        self.assertEqual(host.saved, 1)

    def test_model_profiles_lists_aliases_and_available(self) -> None:
        host = _FakeHost({"p": ProviderConfig(), "q": ProviderConfig()}, {"p": object()})
        host.model_profiles = {
            "cheap": ModelSelection(provider="p", model="m1"),
            "rich": ModelSelection(provider="q", model="m2"),
        }
        host.model_aliases = {"fast": "cheap"}
        profiles = model_profiles_of(host)
        names = [p.name for p in profiles]
        # 别名以用户实际输入的名字列出，profile 自身的名字也补上。
        self.assertEqual(names, ["cheap", "fast", "rich"])
        by_name = {p.name: p for p in profiles}
        self.assertTrue(by_name["fast"].available)
        self.assertFalse(by_name["rich"].available)
        self.assertTrue(model_profile_name_exists(host, "FAST"))
        self.assertFalse(model_profile_name_exists(host, "nope"))

    def test_service_facade_exposes_model_profiles(self) -> None:
        host = _FakeHost()
        host.model_profiles = {"a": ModelSelection(provider="p", model="m")}
        service = install_model_services(host)
        self.assertIsInstance(service, ModelService)
        self.assertIs(host.services["models"], service)
        self.assertEqual([p.name for p in service.model_profiles()], ["a"])


class TestModelSnapshot(unittest.TestCase):
    def test_normalize_name(self) -> None:
        self.assertEqual(normalize_model_snapshot_name("  cheap "), "cheap")
        self.assertEqual(normalize_model_snapshot_name("廉价-1_2"), "廉价-1_2")
        with self.assertRaises(Exception) as caught:
            normalize_model_snapshot_name("")
        self.assertIs(caught.exception, ErrModelSnapshotNameEmpty)
        with self.assertRaises(InvalidError):
            normalize_model_snapshot_name("a" * 33)
        with self.assertRaises(InvalidError):
            normalize_model_snapshot_name("bad name")

    def test_ordered_modes_puts_work_last(self) -> None:
        modes = {"work": 1, "chat": 1, "zzz": 1, "atriwisp2": 1}
        self.assertEqual(
            ordered_model_snapshot_modes(modes), ["chat", "atriwisp2", "work", "zzz"]
        )

    def test_view_skips_empty_slots(self) -> None:
        snapshot = StateModelSnapshot(
            mode_models={"chat": ModelSelection("p", "m"), "work": ModelSelection()},
            compact_model=ModelSelection("p", "c"),
            naming_model=ModelSelection(),
        )
        view = model_snapshot_view("s", snapshot)
        self.assertEqual([slot.label for slot in view.slots], ["chat", "compact"])

    def test_store_clones_on_read_and_write(self) -> None:
        store = ModelSnapshotStore()
        snapshot = StateModelSnapshot(mode_models={"work": ModelSelection("p", "m")})
        store.put("s", snapshot)
        snapshot.mode_models["work"].model = "changed"
        self.assertEqual(store.get("s").mode_models["work"].model, "m")
        self.assertEqual(store.names(), ["s"])
        self.assertTrue(store.delete("s"))
        self.assertFalse(store.delete("s"))
        self.assertEqual(store.snapshot(), {})


class TestModelPolicy(unittest.TestCase):
    def _host(self, policy: GroupPolicyConfig, group: bool = True):
        class _Host:
            def __init__(self) -> None:
                self.policy = policy
                self.blocked = False

            def group_runtime_blocked(self, ctx: object) -> bool:
                return self.blocked

            def is_group_scope(self, ctx: object) -> bool:
                return group

            def scope(self, ctx: object) -> object:
                return None

            def group_policy_for_scope(self, scope: object) -> GroupPolicyConfig:
                return self.policy

            def resolve_group_model_selection(self, name: str):
                if name == "cheap":
                    return ModelSelection(provider="p", model="m"), True
                return ModelSelection(), False

            def group_model_selection_allowed(self, policy, selection, label) -> bool:
                return label in list(policy.allowed_models or [])

        return _Host()

    def test_no_policy_allows_global_model(self) -> None:
        host = self._host(GroupPolicyConfig())
        authorize_execution_model_selection(host, None, ModelSelection("p", "m"))

    def test_default_model_match_allows(self) -> None:
        host = self._host(GroupPolicyConfig(default_model="cheap"))
        authorize_execution_model_selection(host, None, ModelSelection("p", "m"))

    def test_catalog_rejects_other_model(self) -> None:
        host = self._host(GroupPolicyConfig(allowed_models=["p/other"]))
        with self.assertRaises(InvalidError) as caught:
            authorize_execution_model_selection(host, None, ModelSelection("p", "m"))
        self.assertIn("不在本群模型目录内", str(caught.exception))

    def test_default_only_policy_message(self) -> None:
        host = self._host(GroupPolicyConfig(default_model="cheap"))
        with self.assertRaises(InvalidError) as caught:
            authorize_execution_model_selection(host, None, ModelSelection("p", "other"))
        self.assertIn("不在本群默认模型内", str(caught.exception))

    def test_blocked_group_and_private_scope(self) -> None:
        host = self._host(GroupPolicyConfig())
        host.blocked = True
        with self.assertRaises(InvalidError):
            authorize_execution_model_selection(host, None, ModelSelection("p", "m"))
        # 非群作用域完全沿用全局模型，群策略不参与（对应 Go 的 isGroupScope 早退）。
        host = self._host(GroupPolicyConfig(allowed_models=["p/other"]), group=False)
        authorize_execution_model_selection(host, None, ModelSelection("p", "m"))

    def test_empty_selection_rejected_when_policy_active(self) -> None:
        host = self._host(GroupPolicyConfig(default_model="cheap"))
        with self.assertRaises(InvalidError):
            authorize_execution_model_selection(host, None, ModelSelection())


class TestRuntimeStateFiles(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_initial_state_mod_time_is_aware(self) -> None:
        from atri_x.agent.model_state import initial_state_mod_time

        self.assertIsNone(initial_state_mod_time(""))
        self.assertIsNone(initial_state_mod_time(os.path.join(TMP_DIR, "missing.toml")))
        path = os.path.join(TMP_DIR, "state.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[session]\n")
        stamp = initial_state_mod_time(path)
        self.assertIsNotNone(stamp)
        self.assertIsNotNone(stamp.tzinfo)

    def test_save_and_reload_runtime_state(self) -> None:
        from atri_x.agent.model_state import RuntimeStateMixin
        from atri_x.config import RuntimeStateStore, load_state

        path = os.path.join(TMP_DIR, "state.toml")

        class _Host(RuntimeStateMixin, ModelMixin):
            def __init__(self) -> None:
                self.state_path = path
                self.state_store = RuntimeStateStore.open(path)
                self.providers = {"p": ProviderConfig()}
                self.model_runtime = new_model_runtime_state(
                    None, "", "", None, self.providers, {}, {}
                )
                self.naming_model = ModelSelection()
                self.audit_events = []

            def scope(self, ctx):  # pragma: no cover - 未使用
                return None

            def audit(self, event, **attrs):
                self.audit_events.append((event, attrs))

            def client_for_provider(self, provider):
                return None

            def set_naming_model(self, selection):
                self.naming_model = selection

            def configured_naming_model(self):
                return self.naming_model

        host = _Host()
        host.model_runtime.apply_mode_model_selection("work", "p", "m")
        host.set_naming_model(ModelSelection("p", "n"))
        host.save_runtime_state()
        state = load_state(path)
        self.assertEqual(state.mode_models["work"].model, "m")
        self.assertEqual(state.naming_model.model, "n")

        # 外部编辑后 merge 会把它吸收进内存，并按分区变化记一条审计
        state.mode_models["chat"] = ModelSelection("p", "c")
        from atri_x.config import save_state

        save_state(path, state)
        host.merge_external_runtime_state()
        self.assertEqual(host.model_for_mode("chat").model, "c")
        self.assertTrue(host.audit_events)
        self.assertEqual(host.audit_events[0][0], "runtime_state_reloaded")

        # force 刷新不受 mtime 门限约束
        reload = host.refresh_runtime_state(force=True)
        self.assertTrue(reload.applied)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
