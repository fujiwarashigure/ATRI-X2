"""``atri_x.agent`` 的装配与命令层接线测试（bootstrap / mixin / 运行时分区）。

对应 Go：``internal/agent/core.go`` 的 ``NewWithOptions`` 末尾装配段 ——
``SetContextOptions`` / ``attachLLMRetryNotifier`` / ``RegisterDefaultModules``。

本文件覆盖三件事：

1. :func:`atri_x.agent.bootstrap.install_agent_services` 的装配结果、幂等性与失败降级；
2. ``Agent`` 真的继承了各组 mixin（真实实现而不是 :class:`AgentSeams` 的保守降级）；
3. ``state.toml`` 的四个群分区经由 ``Agent`` 覆写的
   ``apply_extra_runtime_sections`` / ``publish_extra_runtime_sections`` 往返；
4. 命令层与模型层共用同一份模型视图类型（``==`` / ``isinstance`` 不能静默失效）。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.config import (
    ContextConfig,
    ModelMetadataConfig,
    ModelSelection,
    ProviderConfig,
    StateConfig,
)
from atri_x.errors import InvalidError
from atri_x.security import Actor, RoleSuperadmin, with_actor
from tests.test_agent_core import make_agent, make_ctx


def superadmin_ctx():
    """``make_ctx`` 的默认 Actor 是普通用户；要看超管命令就换一个。"""
    return with_actor(make_ctx(), Actor(id="root", role=RoleSuperadmin))


class InstallerTests(unittest.TestCase):
    """``install_agent_services`` 的装配结果与幂等。"""

    def test_installs_every_group_facade(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        report = install_agent_services(agent)
        self.assertTrue(report.ok, report.failed)
        names = set(report.installed_names)
        for expected in ("models", "context_runtime", "budget", "ratelimit"):
            self.assertIn(expected, names, report.to_dict())
        self.assertIsNotNone(agent.services.get("models"))
        self.assertIsNotNone(agent.services.get("context_runtime"))
        self.assertIsNotNone(agent.services.get("budget"))
        self.assertIsNotNone(agent.services.get("ratelimit"))

    def test_deps_models_is_the_facade_not_the_agent(self) -> None:
        """``deps.models`` 必须是门面：Agent 自己的同名字段会盖住方法。"""
        from atri_x.agent.bootstrap import install_agent_services
        from atri_x.agent.model_service import ModelService

        agent = make_agent()
        report = install_agent_services(agent)
        self.assertIsInstance(report.models, ModelService)
        self.assertIsNot(report.models, agent)
        self.assertIsInstance(report.deps.models, ModelService)
        # 门面能把 ``model_profiles`` 当方法调用（Agent 本身拿到的是字典）。
        self.assertIsInstance(report.models.model_profiles(), list)
        self.assertIsInstance(agent.model_profiles, dict)

    def test_context_runtime_facade_serves_three_protocols(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        runtime = agent.services["context_runtime"]
        for alias in ("compact", "context_status", "context_policy"):
            self.assertIs(agent.services.get(alias), runtime)
        for attribute in ("compact", "context_status", "context_policy"):
            self.assertIs(getattr(agent.command_deps, attribute), runtime)

    def test_tools_and_member_panel_facades(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        tools = agent.services["tools"]
        panel = agent.services["member_panel"]
        for attribute in ("list", "unregister", "remove", "reload"):
            self.assertTrue(callable(getattr(tools, attribute)), attribute)
        self.assertTrue(callable(panel.member_panel))
        self.assertIs(agent.command_deps.tools, tools)
        self.assertIs(agent.command_deps.member_panel, panel)

    def test_idempotent(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services, is_bootstrapped

        agent = make_agent()
        first = install_agent_services(agent)
        models = agent.services["models"]
        second = install_agent_services(agent)
        self.assertTrue(is_bootstrapped(agent))
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        # 门面对象复用，不是又建了一个。
        self.assertIs(agent.services["models"], models)
        self.assertIs(agent.services["tools"], first.services.get("tools"))
        # 第二次不再重复绑定；台账以 skipped 的形式保留可读性。
        self.assertEqual(second.installed_names, [])
        self.assertIn("models", second.skipped_names)

    def test_missing_installer_is_skipped_with_a_reason(self) -> None:
        """某一项缺失只记 warning 并跳过，其余照装。"""
        from atri_x.agent import bootstrap

        agent = make_agent()
        broken = (
            "ratelimit",
            "atri_x.agent.does_not_exist",
            "install_ratelimit_services",
        )
        original = bootstrap._CORE_INSTALLERS
        bootstrap._CORE_INSTALLERS = tuple(
            broken if item[0] == "ratelimit" else item for item in original
        )
        try:
            report = bootstrap.install_agent_services(agent)
        finally:
            bootstrap._CORE_INSTALLERS = original
        self.assertIn("ratelimit", dict(report.failed))
        self.assertIn("models", report.installed_names)
        self.assertTrue(any("ratelimit" in item for item in report.warnings))
        # 降级后模型服务照装可用。
        self.assertIsNotNone(agent.services.get("models"))

    def test_report_is_machine_readable(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services, previous_report

        agent = make_agent()
        report = install_agent_services(agent)
        payload = report.to_dict()
        self.assertEqual(payload["models"], "ModelService")
        self.assertTrue(payload["ok"])
        self.assertIn("installed", payload)
        self.assertIs(previous_report(agent), report)
        self.assertIn("agent services:", report.summary())

    def test_context_options_are_configured(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(
            agent,
            opts={
                "context_config": ContextConfig(),
                "model_metadata": ModelMetadataConfig(),
                "compact_model": ModelSelection(provider="default", model="small"),
            },
        )
        runtime = agent.context_runtime
        self.assertIsNotNone(runtime.window_resolver)
        self.assertIsNotNone(runtime.compactor)
        self.assertEqual(runtime.configured_compact_model().model, "small")

    def test_report_snapshot_is_published_on_the_host(self) -> None:
        """``to_dict()`` 必须有宿主出口（``/diagnostics`` 与 doctor 共用）。"""
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        report = install_agent_services(agent)
        payload = agent.agent_services_report
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload, report.to_dict())
        self.assertTrue(payload["ok"])
        for key in ("installed", "skipped", "failed", "warnings", "ok"):
            self.assertIn(key, payload)

    def test_rate_limit_options_reach_the_host(self) -> None:
        """``opts`` 里的限流器与阈值必须落到宿主字段（``/diagnostics`` 依赖它们）。"""
        from atri_x.agent.bootstrap import install_agent_services
        from atri_x.ops.ratelimit import Limit, new as new_limiter

        user = new_limiter(Limit(rate_per_second=0.2, burst=3))
        group = new_limiter(Limit(rate_per_second=1.0, burst=10))
        agent = make_agent()
        install_agent_services(
            agent,
            opts={
                "rate_limit_user": user,
                "rate_limit_group": group,
                "rate_limit_user_per_minute": 12,
                "rate_limit_user_burst": 3,
                "rate_limit_group_per_minute": 60,
                "rate_limit_group_burst": 10,
            },
        )
        self.assertIs(agent.rate_limit_user, user)
        self.assertIs(agent.rate_limit_group, group)
        status = agent.rate_limit_status()
        self.assertEqual(status.user_messages_per_minute, 12)
        self.assertEqual(status.user_burst, 3)
        self.assertEqual(status.group_messages_per_minute, 60)
        self.assertEqual(status.group_burst, 10)


class MixinCompositionTests(unittest.TestCase):
    """``Agent`` 必须真的继承各组 mixin（而不是只剩 AgentSeams 的降级答案）。"""

    def test_seams_is_last_in_mro(self) -> None:
        from atri_x.agent.core import Agent
        from atri_x.agent.seams import AgentSeams

        mro = Agent.__mro__
        self.assertIs(mro[-2], AgentSeams)
        self.assertLess(mro.index(AgentSeams), len(mro) - 1)

    def test_expected_mixins_are_present(self) -> None:
        from atri_x.agent.core import Agent

        mro = Agent.__mro__
        for name in (
            "ModelMixin",
            "ModelListMixin",
            "RuntimeStateMixin",
            "BudgetMixin",
            "ContextRuntimeMixin",
            "ContextPolicyMixin",
            "ContextUsageMixin",
            "ContextCompactMixin",
            "ContextSeedMixin",
            "RateLimitMixin",
            "ToolDirectiveMixin",
            "ToolAuthMixin",
            "ToolCacheMixin",
            "ToolRuntimeMixin",
            "GroupPolicyMixin",
            "GroupKnowledgeMixin",
            "GroupRuntimeMixin",
            "CronMixin",
            "MemberPanelMixin",
        ):
            self.assertTrue(
                any(base.__name__ == name for base in mro), "%s 未继承" % name
            )

    def test_real_implementations_win_over_seams(self) -> None:
        """降级实现必须被真实 mixin 覆盖。"""
        agent = make_agent()
        # AgentSeams.history_enabled 恒为 True；群策略版本会读策略。
        self.assertTrue(callable(agent.history_enabled))
        # AgentSeams.raw_group_policy 返回默认 config；群策略版本读快照。
        scope = agent.scope(make_ctx())
        from atri_x.config import GroupPolicyConfig

        self.assertIsInstance(agent.raw_group_policy(scope), GroupPolicyConfig)
        # 工具运行时 / 指令 / 限流状态都能真的取到。
        self.assertTrue(callable(agent.group_tool_allowlist))
        self.assertTrue(callable(agent.apply_tool_directives))
        from atri_x.agent.ratelimit import ensure_rate_limit_state

        self.assertIs(ensure_rate_limit_state(agent), agent.rate_limit_state)

    def test_budget_limits_default_to_zero_when_unset(self) -> None:
        """``Options.budget_limits`` 未给时不能是 None（BudgetMixin 会 normalized()）。"""
        agent = make_agent()
        self.assertIsNotNone(agent.budget_limits)
        self.assertEqual(agent.budget_limits.global_chat_tokens_daily, 0)
        self.assertEqual(agent.chat_budget_limit_specs(make_ctx()), [])

    def test_client_for_provider_prefers_agent_clients(self) -> None:
        """装配期往 ``agent.clients`` 登记的客户端必须能立刻取到。"""
        agent = make_agent()

        class _Late:
            pass

        late = _Late()
        agent.clients["default"] = late
        self.assertIs(agent.client_for_provider("default"), late)

    def test_group_default_model_selection_returns_pair(self) -> None:
        """契约是 ``(selection, bool)``，而 group_policy 的模块函数返回裸值。"""
        agent = make_agent()
        result = agent.group_default_model_selection(make_ctx())
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsNone(result[0])
        self.assertFalse(result[1])

    def test_group_tool_allowlist_is_a_direct_implementation(self) -> None:
        """``groupToolAllowlist`` / ``filterGroupFeatureSchemas`` 在 Go 里是**直接实现**
        （``tool_auth.go:53/138``），不查 ``Deps.GroupPolicy``。

        这里钉住两点：

        1. 非群作用域按 Go 的守卫原样放行（``nil`` / 原 schema 列表）；
        2. ``services["group_policy"]`` 里就算注册了同名方法也不能劫持它们 ——
           ``bootstrap._wire_deps`` 会把 ``deps.group_policy`` 接成 ``Agent`` 自己
           （Go ``core.go:454``），"优先用服务"的分派会当场自递归。
        """
        agent = make_agent()
        agent.services["group_policy"] = _AllowlistPolicy({"read_file": True})
        self.assertIsNone(agent.group_tool_allowlist(make_ctx()))
        schemas = [_schema("read_file"), _schema("write_file")]
        filtered = agent.filter_group_feature_schemas(make_ctx(), schemas)
        self.assertEqual(
            [schema.function.name for schema in filtered], ["read_file", "write_file"]
        )
        self.assertEqual(agent.group_default_model_selection(make_ctx()), (None, False))

    def test_group_tool_allowlist_reads_the_group_policy(self) -> None:
        """群作用域下白名单来自群策略本身（Go ``groupPolicyForScope`` 那条路径）。"""
        from atri_x.config import GroupPolicyConfig

        agent = make_agent()
        agent.services["group_policy"] = _AllowlistPolicy({"read_file": True})
        agent.group_policy["cli:group:42"] = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        )
        self.assertEqual(
            agent.group_tool_allowlist(make_ctx(scope_id="group:42")), {"read_file": True}
        )

    def test_group_seams_do_not_recurse_when_deps_points_at_the_agent(self) -> None:
        """``bootstrap`` 按 Go 把 ``Agent`` 写进 ``deps.group_policy`` 后不能再自递归。

        ``AgentSeams._svc`` 若把宿主自己当成"已装配的服务"，``hasattr`` 恒为真，
        ``service.foo(...)`` 就是 ``self.foo(...)`` → ``RecursionError``（P0-2）。
        """
        from atri_x.config import GroupPolicyConfig

        agent = make_agent()
        agent.command_deps.group_policy = agent
        agent.group_policy["cli:group:42"] = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        )
        ctx = make_ctx(scope_id="group:42")
        self.assertIsNone(agent._svc("group_policy"))
        self.assertEqual(agent.group_tool_allowlist(ctx), {"read_file": True})
        schemas = [_schema("read_file"), _schema("write_file")]
        self.assertEqual(
            [schema.function.name for schema in agent.filter_group_feature_schemas(ctx, schemas)],
            ["read_file", "write_file"],
        )
        self.assertEqual(agent.group_default_model_selection(ctx), (None, False))
        self.assertEqual(agent.group_merge_window(ctx), 0.0)
        self.assertTrue(agent.history_enabled(ctx))
        self.assertIsInstance(agent.raw_group_policy(agent.scope(ctx)), GroupPolicyConfig)

    def test_group_allowlist_without_policy_inherits_global(self) -> None:
        """没配置群策略 = 继承全局：白名单是 ``None``（不限制），不是空名单。

        回归：旧实现把 ``None`` 归一成 ``[]``，第二次受限判定把"没配策略"读成
        "禁止全部工具"，导致所有群的工具调用都被拒（真机事故，0.9.11）。
        """
        agent = make_agent()
        ctx = make_ctx(scope_id="group:42")
        policy = agent.group_policy_for_scope(agent.scope(ctx))
        self.assertFalse(policy.tool_allowlist_set)
        self.assertIsNone(policy.tool_allowlist)
        self.assertFalse(policy.tool_allowlist_restricted())
        self.assertIsNone(agent.group_tool_allowlist(ctx))

    def test_background_takeover_prefers_registered_foreground_service(self) -> None:
        agent = make_agent()
        agent.services["foreground"] = _TakesOver()
        self.assertTrue(agent.background_session_must_stop(make_ctx(), "s1"))


class _AllowlistPolicy:
    def __init__(self, allowlist: dict[str, bool]) -> None:
        self.allowlist = allowlist

    def group_tool_allowlist(self, ctx):
        return self.allowlist

    def filter_group_feature_schemas(self, ctx, schemas):
        return [item for item in schemas if item.function.name in self.allowlist]


class _TakesOver:
    def background_session_must_stop(self, ctx, session_id: str) -> bool:
        del ctx, session_id
        return True


def _schema(name: str):
    from atri_x.llm import ToolFunctionSchema, ToolSchema

    return ToolSchema(function=ToolFunctionSchema(name=name))


class RuntimeSectionsTests(unittest.TestCase):
    """四个群分区经宿主钩子往返 ``state.toml``。"""

    def test_round_trip_through_state(self) -> None:
        from atri_x.config import GroupPolicyConfig

        agent = make_agent()
        state = StateConfig()
        scope = agent.scope(make_ctx())
        agent.set_group_policy_for_scope(
            scope, GroupPolicyConfig(default_model="default/m1", default_mode="work")
        )
        agent.publish_extra_runtime_sections(state)
        self.assertTrue(state.group_policy)

        # 换一个宿主读回同一个 state，两个分区都要搬进去。
        other = make_agent()
        other.apply_extra_runtime_sections(state)
        self.assertEqual(
            other.group_policy_for_scope(other.scope(make_ctx())).default_model,
            "default/m1",
        )

    def test_agent_overrides_are_not_the_empty_mixin(self) -> None:
        from atri_x.agent.core import Agent
        from atri_x.agent.group_runtime import RuntimeSectionsMixin
        from atri_x.agent.model_state import RuntimeStateMixin

        self.assertIsNot(
            Agent.apply_extra_runtime_sections,
            RuntimeStateMixin.apply_extra_runtime_sections,
        )
        self.assertIsNot(
            Agent.publish_extra_runtime_sections,
            RuntimeStateMixin.publish_extra_runtime_sections,
        )
        # 显式委托给 agent-tools 的实现（不依赖 MRO 顺序）。
        agent = make_agent()
        state = StateConfig()
        Agent.apply_extra_runtime_sections(agent, state)
        RuntimeSectionsMixin.publish_extra_runtime_sections(agent, state)
        self.assertIsInstance(state.group_runtime, dict)

    def test_runtime_state_store_is_created_from_state_path(self) -> None:
        import os
        import shutil

        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tmp_agent_bootstrap_test",
        )
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, "state.toml")
        try:
            agent = make_agent(state_path=path)
            agent.save_runtime_state()
            self.assertTrue(os.path.exists(path))
            reread = make_agent(state_path=path)
            reread.load_runtime_state_at_startup()
            self.assertIsInstance(reread.model_runtime.mode_models_snapshot(), dict)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class CommandsOverRealAgentTests(unittest.TestCase):
    """命令层跑在真实 ``Agent`` 上（而不是只跑在 fake harness 上）。"""

    def test_default_modules_register_on_the_agent_router(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        names = [info.name for info in agent.commands.commands()]
        self.assertEqual(len(names), 48)
        self.assertEqual(names[0], "help")
        self.assertIn("state", names)
        self.assertIn("model", names)

    def test_deps_wiring_points_at_the_agent_services(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        deps = agent.command_deps
        self.assertIs(deps.models, agent.services["models"])
        self.assertIs(deps.context_status, agent.services["context_runtime"])
        self.assertIs(deps.compact, agent.services["context_runtime"])
        self.assertIs(deps.context_policy, agent.services["context_runtime"])
        self.assertIs(deps.tools, agent.services["tools"])
        self.assertIs(deps.member_panel, agent.services["member_panel"])
        # 原有接线不受影响。
        self.assertIs(deps.sessions, agent.sessions)
        self.assertIs(deps.runtime_state, agent)

    def test_help_command_runs_end_to_end(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        result = asyncio.run(agent.commands.dispatch(make_ctx(), "/help"))
        self.assertIsNotNone(result)
        self.assertIn("/new", result.content)
        self.assertIn("/stop", result.content)
        # 超管才看得到的命令对普通用户不可见。
        self.assertNotIn("/model", result.content)

        admin = asyncio.run(agent.commands.dispatch(superadmin_ctx(), "/help"))
        self.assertIn("/model", admin.content)
        self.assertIn("/state", admin.content)

    def test_detailed_help_uses_the_router(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        result = asyncio.run(
            agent.commands.dispatch(superadmin_ctx(), "/help model")
        )
        self.assertIn("/model", result.content)

    def test_unknown_command_message(self) -> None:
        agent = make_agent()
        result = asyncio.run(agent.commands.dispatch(make_ctx(), "/nope"))
        self.assertIn("unknown command", result.content)

    def test_model_command_uses_the_facade(self) -> None:
        """``/model --profiles`` 走 ``deps.models`` 门面取命名档。"""
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        result = asyncio.run(
            agent.commands.dispatch(superadmin_ctx(), "/model --profiles")
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.content)

    def test_check_model_lists_configured_models(self) -> None:
        """``/checkmodel`` 经门面列举 provider 配置里的模型。"""
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent(providers={"default": ProviderConfig(models=["m1", "m2"])})
        install_agent_services(agent)
        result = asyncio.run(agent.commands.dispatch(superadmin_ctx(), "/checkmodel"))
        self.assertIsNotNone(result)
        self.assertIn("m1", result.content)

    def test_status_command_runs_with_facade(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        result = asyncio.run(agent.commands.dispatch(make_ctx(), "/status"))
        self.assertIsNotNone(result)
        self.assertIn("session status", result.content)

    def test_state_command_reports_runtime_state(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        result = asyncio.run(agent.commands.dispatch(superadmin_ctx(), "/state"))
        self.assertIsNotNone(result)
        self.assertTrue(result.content)

    def test_command_infos_come_from_the_agent(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        infos = agent.command_infos()
        self.assertEqual(len(infos), 48)
        actor = agent.actor(make_ctx())
        visible = {info.name for info in agent.commands.commands_for_actor(actor)}
        self.assertIn("help", visible)


class ModelViewTypeIdentityTests(unittest.TestCase):
    """模型视图类型只有一份：命令层、模型层、``model_types`` 必须是同一个类。"""

    def test_single_origin(self) -> None:
        from atri_x.agent import commands, model
        from atri_x.agent.commands import model_types

        for name in (
            "ModelListOptions",
            "ModelListResult",
            "ModelOption",
            "ModelProfile",
            "ModelProviderError",
            "ModelSnapshot",
            "ModelSnapshotSlot",
        ):
            first = getattr(model_types, name)
            self.assertIs(getattr(commands, name), first, name)
            self.assertIs(getattr(model, name), first, name)

    def test_instances_compare_across_modules(self) -> None:
        from atri_x.agent import commands, model
        from atri_x.agent.commands import model_types

        left = model.ModelOption(index=1, provider="p", model="m")
        right = commands.ModelOption(index=1, provider="p", model="m")
        self.assertEqual(left, right)
        self.assertIsInstance(right, model_types.ModelOption)
        self.assertIsInstance(left, commands.ModelOption)

    def test_lazy_getattr_still_resolves(self) -> None:
        """``commands.__getattr__`` 兜底路径不能变成死代码。"""
        from atri_x.agent import commands
        from atri_x.agent.commands import model_types

        self.assertIs(
            commands.__getattr__("ModelOption"), model_types.ModelOption
        )
        with self.assertRaises(AttributeError):
            commands.__getattr__("NotARealName")


class PackageExportTests(unittest.TestCase):
    """``atri_x.agent`` 的包级导出。"""

    def test_bootstrap_entry_is_exported(self) -> None:
        import atri_x.agent as package

        self.assertTrue(callable(package.install_agent_services))
        self.assertTrue(callable(package.new_default_router))
        self.assertIs(package.ModelService.__name__, "ModelService")

    def test_every_exported_name_exists(self) -> None:
        import atri_x.agent as package

        self.assertTrue(package.__all__)
        missing = [name for name in package.__all__ if not hasattr(package, name)]
        self.assertEqual(missing, [])

    def test_package_info_lists_modules(self) -> None:
        import atri_x.agent as package

        info = package.agent_package_info()
        self.assertIn("core", info["modules"])
        self.assertIn("bootstrap", info["modules"])
        self.assertGreater(info["exports"], 50)


class ErrorsExportTests(unittest.TestCase):
    def test_sentinel_is_an_instance(self) -> None:
        """``ErrNotFound`` 是**实例**而不是类（PORTING.md §1.5）。"""
        import atri_x.agent as package
        from atri_x import errors as shared

        self.assertIs(package.ErrNotFound, shared.ErrNotFound)
        self.assertFalse(isinstance(shared.ErrNotFound, type))
        self.assertTrue(issubclass(shared.NotFoundError, BaseException))

    def test_isinstance_checks_still_work(self) -> None:
        import atri_x.agent as package

        with self.assertRaises(package.NotFoundError):
            raise package.NotFoundError("x")
        self.assertTrue(callable(package.new_user_error_details))


class CommandErrorPathTests(unittest.TestCase):
    """命令层的参数错误与权限拒绝在真实 Agent 上也要成立。"""

    def test_invalid_usage_raises(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        with self.assertRaises((InvalidError, ValueError)):
            asyncio.run(
                agent.commands.dispatch(superadmin_ctx(), "/model --save --apply")
            )

    def test_non_command_text_is_rejected(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        with self.assertRaises(InvalidError):
            asyncio.run(agent.commands.dispatch(make_ctx(), "hello"))

    def test_completion_is_actor_filtered(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services

        agent = make_agent()
        install_agent_services(agent)
        actor = agent.actor(make_ctx())
        visible = {info.name for info in agent.commands.commands_for_actor(actor)}
        self.assertIn("help", visible)
        self.assertNotIn("doctor", visible)
        self.assertNotIn("model", visible)
        admin = agent.actor(superadmin_ctx())
        self.assertIn("doctor", {i.name for i in agent.commands.commands_for_actor(admin)})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
