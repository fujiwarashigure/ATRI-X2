"""``atri_x.agent.commands`` 的模块清单、导出与"真实 Agent 端到端"测试。

对应 Go：``internal/agent/commands``（29 个生产文件）。

与既有三个测试文件的分工：

* ``test_agent_commands.py`` —— 注册表、别名、帮助、补全、参数错误、权限（用替身服务）；
* ``test_agent_commands_session.py`` / ``test_agent_commands_services.py`` —— 各命令的
  业务行为（替身服务）；
* **本文件** —— ① 31 个文件逐个导入、模块职责与 Go 文件对照；② ``Deps`` 的协议槽位与
  :mod:`atri_x.agent.bootstrap` 装出来的门面**真的对得上**（``runtime_checkable`` 结构判定）；
  ③ 命令跑在**真实 Agent + 真实门面**上不炸（替身测不出接线缺失）。
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import unittest

from atri_x.agent import commands as commands_package

#: Go 生产文件 → Python 模块。``help_util.go`` 并入 ``help.py``，
#: ``session_mode.go`` 并入 ``session_core.py``，``log.go`` 拆成三个模块。
#: 值里的 ``None`` 表示该 Go 文件没有独立 Python 文件（已合并）。
GO_TO_PYTHON: dict[str, str | None] = {
    "character.go": "character",
    "compact.go": "compact",
    "completion_helpers.go": "completion",
    "context_policy.go": "context_policy",
    "doctor.go": "doctor",
    "group_knowledge.go": "group_knowledge",
    "group_policy.go": "group_policy",
    "group_services.go": "group_services",
    "help.go": "help",
    "help_util.go": None,
    "hook.go": "hook",
    "log.go": "log",
    "member_panel.go": "member_panel",
    "memory_common.go": "memory_common",
    "memory_forget.go": "memory_forget",
    "memory_learning.go": "memory_learning",
    "model.go": "model",
    "register.go": "deps",
    "request.go": "request",
    "rollback.go": "rollback",
    "session_core.go": "session_core",
    "session_format.go": "session_format",
    "session_lifecycle.go": "session_lifecycle",
    "session_mode.go": None,
    "session_module.go": "session_module",
    "session_navigation.go": "session_navigation",
    "state.go": "state",
    "tool.go": "tool",
    "usage.go": "usage",
}

#: 每个模块必须给出的公开名（``__all__`` 的非空断言之外的定点检查）。
EXPECTED_EXPORTS: dict[str, tuple[str, ...]] = {
    "character": ("CharacterModule", "new_characters"),
    "compact": ("CompactModule", "new_compact"),
    "completion": ("complete_session_ids", "complete_tool_names"),
    "context_policy": ("ContextPolicyModule", "new_context_policy"),
    "deps": ("Deps", "register_default_modules"),
    "doctor": ("DoctorModule", "new_doctor"),
    "group_knowledge": ("KnowledgeModule", "new_knowledge"),
    "group_policy": ("GroupPolicyModule", "new_group_policy"),
    "group_services": ("GroupServicesModule", "new_poll", "new_remind", "new_signup"),
    "help": ("HelpModule", "new_help"),
    "hook": ("HookModule", "new_hooks"),
    "log": ("LogModule", "new_atriwisp", "new_audit", "new_log"),
    "log_args": ("parse_log_args", "parse_log_time"),
    "log_format": ("format_audit_entries", "format_runtime_log_entries"),
    "member_panel": ("MemberPanelModule", "new_member_panel"),
    "memory_common": ("memory_entry_line", "resolve_scoped_memory"),
    "memory_forget": ("ForgetCommand", "new_forget"),
    "memory_learning": ("LearningCommand", "MemoryCommand", "new_learning"),
    "model": ("ModelModule", "new_check_model", "new_model"),
    "model_types": ("ModelOption", "ModelProfile", "ModelService"),
    "request": ("RequestModule", "new_requests", "new_stop", "new_stop_all"),
    "rollback": ("FileModule", "new_rollback"),
    "session_core": ("new_chat", "new_new", "new_status", "new_work"),
    "session_format": ("format_time", "list_session_page"),
    "session_lifecycle": ("new_archive", "new_clean", "new_delete", "new_rename"),
    "session_module": ("SessionCommandState", "SessionModule"),
    "session_navigation": ("ResumeCommand", "new_fork", "new_resume"),
    "state": ("StateModule", "new_state_command"),
    "tool": ("ToolModule", "new_tools"),
    "usage": ("UsageBucket", "new_usage", "parse_usage_query"),
}


def superadmin_ctx():
    from atri_x.security import Actor, RoleSuperadmin, with_actor
    from tests.test_agent_core import make_ctx

    return with_actor(make_ctx(), Actor(id="root", role=RoleSuperadmin))


def booted_agent(**overrides):
    from atri_x.agent.bootstrap import install_agent_services
    from tests.test_agent_core import make_agent

    agent = make_agent(**overrides)
    install_agent_services(agent)
    return agent


def dispatch(agent, text: str, ctx=None):
    return asyncio.run(agent.commands.dispatch(ctx or superadmin_ctx(), text))


class ModuleInventoryTests(unittest.TestCase):
    """31 个文件都在、都能导入、都给出了自己的公开名。"""

    def test_every_go_file_has_a_python_home(self) -> None:
        present = {info.name for info in pkgutil.iter_modules(commands_package.__path__)}
        for go_file, module in GO_TO_PYTHON.items():
            if module is None:
                continue
            self.assertIn(module, present, "%s → %s 缺失" % (go_file, module))

    def test_python_files_are_all_covered_by_the_mapping(self) -> None:
        """反向检查：不能有"没人认领"的 Python 文件。"""
        mapped = {module for module in GO_TO_PYTHON.values() if module}
        mapped.update({"__init__", "log_args", "log_format", "model_types"})
        present = {info.name for info in pkgutil.iter_modules(commands_package.__path__)}
        self.assertEqual(present - mapped, set())

    def test_every_module_imports_and_exports(self) -> None:
        for name, exports in EXPECTED_EXPORTS.items():
            with self.subTest(module=name):
                module = importlib.import_module("atri_x.agent.commands." + name)
                for attribute in exports:
                    self.assertTrue(
                        hasattr(module, attribute), "%s.%s 缺失" % (name, attribute)
                    )

    def test_package_reexports_every_module_symbol(self) -> None:
        for name, exports in EXPECTED_EXPORTS.items():
            for attribute in exports:
                if name in ("model_types",):
                    continue  # 由 §类型唯一出处 单独校验
                with self.subTest(symbol=attribute):
                    self.assertTrue(
                        hasattr(commands_package, attribute),
                        "commands 未再导出 %s" % attribute,
                    )

    def test_all_declared_exports_exist(self) -> None:
        """``__all__`` 名不副实会让 ``from ... import *`` 静默缺名。"""
        self.assertTrue(commands_package.__all__)
        missing = [
            name for name in commands_package.__all__ if not hasattr(commands_package, name)
        ]
        self.assertEqual(missing, [])

    def test_help_util_and_session_mode_are_merged_not_lost(self) -> None:
        """两个"没有独立文件"的 Go 文件：符号必须还在。"""
        help_module = importlib.import_module("atri_x.agent.commands.help")
        for attribute in (
            "canonical_help_text",
            "command_usage",
            "format_command_help",
            "rewrite_help_prefix",
            "wants_command_help",
        ):
            self.assertTrue(hasattr(help_module, attribute), attribute)
        session_core = importlib.import_module("atri_x.agent.commands.session_core")
        for attribute in ("new_work", "new_chat", "format_tool_usage"):
            self.assertTrue(hasattr(session_core, attribute), attribute)

    def test_log_go_file_is_split_into_three_modules(self) -> None:
        args_module = importlib.import_module("atri_x.agent.commands.log_args")
        format_module = importlib.import_module("atri_x.agent.commands.log_format")
        log_module = importlib.import_module("atri_x.agent.commands.log")
        self.assertTrue(hasattr(args_module, "parse_log_args"))
        self.assertTrue(hasattr(format_module, "format_runtime_log_entries"))
        self.assertTrue(hasattr(log_module, "LogModule"))

    def test_register_go_file_landed_in_deps_and_init(self) -> None:
        """``register.go`` 的机制在 ``deps.py``，注册入口在 ``commands/__init__.py``。"""
        deps_module = importlib.import_module("atri_x.agent.commands.deps")
        for attribute in ("Deps", "Registrar", "Module", "register_modules"):
            self.assertTrue(hasattr(deps_module, attribute), attribute)
        self.assertTrue(callable(commands_package.new_default_router))
        self.assertTrue(callable(commands_package.register_default_modules))


class FacadeProtocolConformanceTests(unittest.TestCase):
    """装配出来的门面必须真的满足 ``Deps`` 声明的那几个 ``Protocol``。

    ``runtime_checkable`` 只做结构判定，正好用来抓"装了个壳、方法名对不上"。
    """

    def test_tools_facade_matches_tool_service(self) -> None:
        from atri_x.agent.commands.deps import ToolService

        agent = booted_agent()
        self.assertIsInstance(agent.services["tools"], ToolService)

    def test_member_panel_facade_matches(self) -> None:
        from atri_x.agent.commands.deps import MemberPanelService

        agent = booted_agent()
        self.assertIsInstance(agent.services["member_panel"], MemberPanelService)

    def test_context_runtime_facade_matches_three_protocols(self) -> None:
        from atri_x.agent.commands.deps import (
            CompactService,
            ContextPolicyService,
            ContextStatusService,
        )

        agent = booted_agent()
        runtime = agent.services["context_runtime"]
        self.assertIsInstance(runtime, ContextStatusService)
        self.assertIsInstance(runtime, ContextPolicyService)
        self.assertIsInstance(runtime, CompactService)

    def test_model_facade_matches_model_service(self) -> None:
        from atri_x.agent.commands.model_types import ModelService as ModelServiceProtocol

        agent = booted_agent()
        self.assertIsInstance(agent.services["models"], ModelServiceProtocol)

    def test_group_facades_match_their_protocols(self) -> None:
        from atri_x.agent.commands.deps import (
            GroupKnowledgeService,
            GroupPolicyService,
        )

        agent = booted_agent()
        # Agent 自己继承了这两组 mixin，``deps`` 直接接宿主（与 Go 的 ``Deps{GroupPolicy: a}`` 一致）。
        self.assertIs(agent.command_deps.group_policy, agent)
        self.assertIs(agent.command_deps.group_knowledge, agent)
        self.assertIsInstance(agent.command_deps.group_policy, GroupPolicyService)
        self.assertIsInstance(
            agent.command_deps.group_knowledge, GroupKnowledgeService
        )

    def test_group_services_facade_matches(self) -> None:
        from atri_x.agent.commands.deps import GroupServicesService

        agent = booted_agent()
        facade = agent.command_deps.group_services
        self.assertIsNotNone(facade)
        self.assertIsInstance(facade, GroupServicesService)


class RealAgentCommandSmokeTests(unittest.TestCase):
    """命令跑在真实 Agent 上：服务缺失时给出提示，服务在时真的返回内容。"""

    def test_help_and_status(self) -> None:
        agent = booted_agent()
        for text in ("/help", "/status", "/new", "/work", "/chat"):
            with self.subTest(command=text):
                result = dispatch(agent, text)
                self.assertIsNotNone(result)
                self.assertTrue(result.content)

    def test_arguments_are_still_validated_on_a_real_agent(self) -> None:
        """参数缺失要抛错，而不是被"接线缺失"掩盖成别的异常。"""
        from atri_x.errors import InvalidError

        agent = booted_agent()
        with self.assertRaises((InvalidError, ValueError)):
            dispatch(agent, "/vote")
        with self.assertRaises((InvalidError, ValueError)):
            dispatch(agent, "/fork")

    def test_model_family(self) -> None:
        from atri_x.config import ProviderConfig

        agent = booted_agent(providers={"default": ProviderConfig(models=["m1", "m2"])})
        for text in ("/model --profiles", "/model --snapshots", "/checkmodel"):
            with self.subTest(command=text):
                result = dispatch(agent, text)
                self.assertIsNotNone(result)
                self.assertTrue(result.content)

    def test_tools_command_without_registry(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/tools")
        self.assertIn("tools: none", result.content)

    def test_doctor_without_service(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/doctor")
        self.assertIn("未启用", result.content)

    def test_hooks_without_service(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/hooks")
        self.assertIn("not configured", result.content)

    def test_hooks_use_the_injected_hook_service(self) -> None:
        from atri_x.agent.bootstrap import install_agent_services
        from tests.test_agent_core import make_agent

        class _HookService:
            def hook_list(self):
                return []

            def stateful_hooks(self):
                return []

        agent = make_agent()
        install_agent_services(agent, opts={"hook_service": _HookService()})
        self.assertIs(agent.services["hooks"], agent.command_deps.hooks)
        self.assertIn("hooks: none", dispatch(agent, "/hooks").content)

    def test_log_without_reader(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/log")
        self.assertTrue(result.content)

    def test_rollback_without_backups(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/rollback")
        self.assertTrue(result.content)

    def test_me_panel(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/me")
        self.assertTrue(result.content)

    def test_chars_without_store(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/chars")
        self.assertTrue(result.content)

    def test_state_command_via_facade(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/state")
        self.assertTrue(result.content)

    def test_compact_without_session(self) -> None:
        from atri_x.errors import NotFoundError

        agent = booted_agent()
        # 没有当前会话时压缩无法进行；这里只要求"接线到位"（不是 AttributeError/
        # "服务未配置"这类接线缺失的表现）。
        with self.assertRaises(NotFoundError):
            dispatch(agent, "/compact")

    def test_group_commands_on_a_group_scope(self) -> None:
        from atri_x.platform import MessageContext, with_message_context
        from atri_x.security import Actor, RoleSuperadmin, with_actor

        ctx = with_message_context(
            None,
            MessageContext(
                platform="qq",
                actor_id="u1",
                platform_user_id="10001",
                nickname="nick",
                scope_id="group:42",
                conversation_kind="group",
            ),
        )
        ctx = with_actor(ctx, Actor(id="root", role=RoleSuperadmin))
        agent = booted_agent()
        # 这些命令都要参数；只验证"服务门面到位、参数校验生效"。
        for text in ("/grouppolicy", "/faq", "/remind", "/poll", "/signup"):
            with self.subTest(command=text):
                result = dispatch(agent, text, ctx)
                self.assertIsNotNone(result)
                self.assertTrue(result.content)

    def test_overflow_command(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/overflow")
        self.assertTrue(result.content)

    def test_memory_and_forget(self) -> None:
        agent = booted_agent()
        for text in ("/memory", "/forget"):
            with self.subTest(command=text):
                result = dispatch(agent, text)
                self.assertTrue(result.content)

    def test_learning(self) -> None:
        agent = booted_agent()
        result = dispatch(agent, "/learning")
        self.assertTrue(result.content)

    def test_requests_and_stopall(self) -> None:
        agent = booted_agent()
        self.assertTrue(dispatch(agent, "/requests").content)
        self.assertTrue(dispatch(agent, "/stopall").content)

    def test_usage(self) -> None:
        agent = booted_agent()
        self.assertTrue(dispatch(agent, "/usage").content)

    def test_audit_and_atriwisp(self) -> None:
        agent = booted_agent()
        self.assertTrue(dispatch(agent, "/audit").content)
        self.assertTrue(dispatch(agent, "/atriwisp").content)

    def test_completion_commands_are_async(self) -> None:
        """每个命令的补全实现都要能被 await（Go 的 Completer 是异步契约）。"""
        import inspect

        from atri_x.command import Request

        agent = booted_agent()
        for info in agent.commands.commands():
            handler, ok = agent.commands.handler(info.name)
            self.assertTrue(ok, info.name)
            complete = getattr(handler, "complete", None)
            if complete is None:
                continue
            with self.subTest(command=info.name):
                self.assertTrue(inspect.iscoroutinefunction(complete))
                outcome = complete(
                    superadmin_ctx(),
                    Request(raw="/" + info.name, prefix="/", name=info.name, args=""),
                )
                self.assertTrue(hasattr(outcome, "__await__"))


class DepsSlotCoverageTests(unittest.TestCase):
    """``Deps`` 的每个服务槽位都要有人填（否则命令静默降级）。"""

    def test_service_slots_are_filled_by_bootstrap(self) -> None:
        from atri_x.agent.commands.deps import Deps

        agent = booted_agent()
        deps = agent.command_deps
        for field_name in Deps.__dataclass_fields__:
            if field_name in ("router", "audit", "logs", "hooks", "doctor"):
                continue  # 由 app 层注入或本就不必填
            value = getattr(deps, field_name)
            if field_name in (
                "characters",
                "angel_memory",
                "resident_memory",
                "self_learning",
                "file_backups",
                "group_services",
            ):
                continue  # 这些由 app 层的业务存储提供
            with self.subTest(slot=field_name):
                self.assertIsNotNone(value, "Deps.%s 没有被装配" % field_name)

    def test_router_is_the_agent_router(self) -> None:
        agent = booted_agent()
        self.assertIs(agent.command_deps.router, agent.commands)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
