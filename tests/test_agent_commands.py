"""``atri_x.agent.commands`` 的注册、查找、帮助、补全与权限测试。

对应 Go：``internal/agent/commands/register.go``、``help.go``、``help_test.go``。

本文件只验证"命令框架"这一层：注册表内容与顺序、别名与大小写归一、命令级准入、
``/help`` 的帮助文案与详细帮助、整行补全、参数解析失败时的异常。命令的业务行为在
``test_agent_commands_session.py`` 与 ``test_agent_commands_services.py`` 里。
"""

from __future__ import annotations

import asyncio
import inspect
import unittest

from atri_x.agent.commands import (
    CommandGroup,
    Deps,
    default_modules,
    new_command_group,
    new_default_router,
    new_router,
    register_default_modules,
    register_factories,
    register_modules,
)
from atri_x.command import (
    Info,
    SessionEffectMutate,
    SessionEffectSwitchCurrent,
)
from atri_x.errors import InvalidError
from atri_x.security import Actor, RoleSuperadmin, RoleUser
from tests.agent_commands_fakes import Harness

# 49 条默认命令（1.0.10 新增 /time；1.1.3 新增 /research），顺序与 Go ``DefaultModules``
# 的注册顺序一致，Python 侧新增的模块排在末尾。
EXPECTED_COMMANDS = [
    "help",
    "grant",
    "panic",
    "model",
    "checkmodel",
    "new",
    "status",
    "sessions",
    "archives",
    "archive",
    "unarchive",
    "pin",
    "unpin",
    "rename",
    "delete",
    "clean",
    "resume",
    "messages",
    "fork",
    "work",
    "chat",
    "compact",
    "overflow",
    "grouppolicy",
    "faq",
    "me",
    "remind",
    "poll",
    "vote",
    "signup",
    "join",
    "requests",
    "stop",
    "stopall",
    "rollback",
    "doctor",
    "log",
    "audit",
    "atriwisp",
    "usage",
    "tools",
    "chars",
    "hooks",
    "state",
    "memory",
    "forget",
    "learning",
    "time",
    "research",
]

# 普通用户（非超管、非群管理员）能看到的命令。
USER_COMMANDS = {
    "archive",
    "archives",
    "chars",
    "chat",
    "compact",
    "delete",
    "forget",
    "fork",
    "help",
    "join",
    "me",
    "messages",
    "new",
    "pin",
    "poll",
    "remind",
    "rename",
    "research",
    "resume",
    "sessions",
    "signup",
    "status",
    "stop",
    "tools",
    "unarchive",
    "unpin",
    "vote",
    "work",
}


def run(coro):
    return asyncio.run(coro)


class RegistryTests(unittest.TestCase):
    def test_default_modules_register_every_command(self) -> None:
        harness = Harness()
        names = [info.name for info in harness.router.commands()]
        self.assertEqual(names, EXPECTED_COMMANDS)
        self.assertEqual(len(names), len(set(names)))

    def test_default_modules_order_matches_go(self) -> None:
        modules = default_modules()
        self.assertEqual(
            [type(module).__name__ for module in modules],
            [
                "HelpModule",
                "ApprovalModule",
                "ModelModule",
                "SessionModule",
                "CompactModule",
                "ContextPolicyModule",
                "GroupPolicyModule",
                "KnowledgeModule",
                "MemberPanelModule",
                "GroupServicesModule",
                "RequestModule",
                "FileModule",
                "DoctorModule",
                "LogModule",
                "ToolModule",
                "CharacterModule",
                "HookModule",
                "StateModule",
                "MemoryLearningModule",
                "TimeModule",
                "ResearchModule",
            ],
        )

    def test_alias_lookup_is_case_insensitive(self) -> None:
        harness = Harness()
        for alias, expected in (
            ("models", "checkmodel"),
            ("angel_memory", "memory"),
            ("selflearning", "learning"),
            ("mytasks", "me"),
            ("ctxpolicy", "overflow"),
            ("gpolicy", "grouppolicy"),
            ("kb", "faq"),
            ("forget_memory", "forget"),
            ("delete_memory", "forget"),
            ("groupkb", "faq"),
        ):
            info, ok = harness.router.command_info(alias)
            self.assertTrue(ok, alias)
            self.assertEqual(info.name, expected, alias)
        info, ok = harness.router.command_info("/MODELS")
        self.assertTrue(ok)
        self.assertEqual(info.name, "checkmodel")

    def test_command_info_returns_a_copy(self) -> None:
        harness = Harness()
        info, ok = harness.router.command_info("help")
        self.assertTrue(ok)
        info.name = "mutated"
        again, _ = harness.router.command_info("help")
        self.assertEqual(again.name, "help")

    def test_unknown_command_is_empty_string(self) -> None:
        harness = Harness()
        info, ok = harness.router.command_info("nope")
        self.assertFalse(ok)
        self.assertIsNone(info)

    def test_duplicate_registration_raises(self) -> None:
        router = new_router()
        router.register(harness_help_handler())
        with self.assertRaises(InvalidError):
            router.register(harness_help_handler())

    def test_duplicate_alias_raises(self) -> None:
        router = new_router()
        first = harness_help_handler()
        router.register(first)
        clash = harness_help_handler(name="other", aliases=["help"])
        with self.assertRaises(InvalidError):
            router.register(clash)

    def test_empty_name_raises(self) -> None:
        router = new_router()
        with self.assertRaises(InvalidError):
            router.register(harness_help_handler(name=""))

    def test_register_helpers(self) -> None:
        harness = Harness()
        router = new_router()
        register_modules(router, harness.deps, new_command_group())
        self.assertEqual(len(router.commands()), 0)

        group = new_command_group(
            lambda deps: harness.router.handler("help")[0],
        )
        self.assertIsInstance(group, CommandGroup)
        register_modules(router, harness.deps, group)
        self.assertEqual([info.name for info in router.commands()], ["help"])

        router2 = new_router()
        register_factories(
            router2, harness.deps, lambda deps: harness.router.handler("help")[0]
        )
        self.assertEqual([info.name for info in router2.commands()], ["help"])

    def test_register_default_modules_can_take_extra(self) -> None:
        harness = Harness()
        router = new_router()
        register_default_modules(router, harness.deps, new_command_group())
        self.assertEqual(len(router.commands()), len(EXPECTED_COMMANDS))
        self.assertEqual(
            [info.name for info in router.commands()], EXPECTED_COMMANDS
        )

    def test_session_effect_flags(self) -> None:
        harness = Harness()
        for name, effect in (
            ("new", SessionEffectSwitchCurrent),
            ("resume", SessionEffectSwitchCurrent),
            ("fork", SessionEffectSwitchCurrent),
            ("work", SessionEffectSwitchCurrent),
            ("chat", SessionEffectSwitchCurrent),
            ("compact", SessionEffectMutate),
        ):
            info, ok = harness.router.command_info(name)
            self.assertTrue(ok)
            self.assertEqual(info.session_effect, effect, name)
        info, _ = harness.router.command_info("status")
        self.assertEqual(info.session_effect, 0)

    def test_every_completer_is_async(self) -> None:
        """``atri_x.completion.router`` 会 ``await handler.complete(...)``。

        因此凡是提供 ``complete`` 的 handler，``complete`` 必须是协程函数，
        否则补全链路会在运行期 TypeError。
        """
        harness = Harness()
        checked = 0
        for info in harness.router.commands():
            handler, ok = harness.router.handler(info.name)
            self.assertTrue(ok)
            complete = getattr(handler, "complete", None)
            if complete is None:
                continue
            checked += 1
            self.assertTrue(
                inspect.iscoroutinefunction(complete),
                f"{info.name}.complete 必须是 async def",
            )
        self.assertGreater(checked, 8)

    def test_every_handler_has_async_handle(self) -> None:
        harness = Harness()
        for info in harness.router.commands():
            handler, _ = harness.router.handler(info.name)
            self.assertTrue(inspect.iscoroutinefunction(handler.handle), info.name)


def harness_help_handler(name: str = "help", aliases=None):
    """造一个最小的 ``Info`` + handler，用于注册冲突测试。"""

    async def handle(ctx, req):  # pragma: no cover - 不会被调用
        return None

    from atri_x.command import new_func

    return new_func(Info(name=name, aliases=list(aliases or [])), handle)


class LookupAndAccessTests(unittest.TestCase):
    def test_superadmin_sees_everything(self) -> None:
        harness = Harness()
        actor = Actor(id="u1", platform="qq", platform_user_id="1", role=RoleSuperadmin)
        self.assertEqual(len(harness.router.commands_for_actor(actor)), 49)

    def test_plain_user_sees_only_user_commands(self) -> None:
        harness = Harness()
        user = Actor(id="u2", platform="qq", platform_user_id="2", role=RoleUser)
        names = {info.name for info in harness.router.commands_for_actor(user)}
        self.assertEqual(names, USER_COMMANDS)

    def test_group_admin_gets_group_scoped_commands(self) -> None:
        harness = Harness(group=True)
        admin = Actor(
            id="u2",
            platform="qq",
            platform_user_id="2",
            role=RoleUser,
            group_role="admin",
        )
        names = {info.name for info in harness.router.commands_for_actor(admin)}
        # allow_group_admin 的命令额外放行；普通群成员仍然拿不到。
        for extra in ("grouppolicy", "faq", "overflow", "learning"):
            self.assertIn(extra, names)
        self.assertNotIn("audit", names)

    def test_plain_group_member_is_denied(self) -> None:
        harness = Harness(group=True)
        member = Actor(
            id="u2",
            platform="qq",
            platform_user_id="2",
            role=RoleUser,
            group_role="member",
        )
        names = {info.name for info in harness.router.commands_for_actor(member)}
        self.assertNotIn("grouppolicy", names)
        self.assertNotIn("learning", names)

    def test_command_info_for_actor_denies_superadmin_only(self) -> None:
        harness = Harness()
        user = Actor(id="u2", platform="qq", platform_user_id="2", role=RoleUser)
        info, ok = harness.router.command_info_for_actor("audit", user)
        self.assertFalse(ok)
        self.assertIsNone(info)
        info, ok = harness.router.command_info_for_actor("status", user)
        self.assertTrue(ok)
        self.assertEqual(info.name, "status")

    def test_unknown_command_dispatch_message(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/nope"))
        self.assertEqual(
            result.content,
            "unknown command: /nope\ntype /help for available commands",
        )

    def test_non_command_text_raises(self) -> None:
        harness = Harness()
        with self.assertRaises(InvalidError):
            run(harness.router.dispatch(harness.ctx, "hello"))
        self.assertFalse(harness.router.is_command("hello"))
        self.assertTrue(harness.router.is_command("/help"))

    def test_custom_prefix(self) -> None:
        harness = Harness()
        router = new_default_router(harness.deps, ["!", "／"])
        self.assertIs(harness.deps.router, router)
        self.assertEqual(router.primary_prefix(), "!")
        result = run(router.dispatch(harness.ctx, "!status"))
        self.assertIn("session status", result.content)
        self.assertEqual(
            run(router.dispatch(harness.ctx, "／nope")).content,
            "unknown command: ／nope\ntype ／help for available commands",
        )


class HelpTests(unittest.TestCase):
    def test_help_lists_commands_with_prefix(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/help"))
        self.assertTrue(result.content.startswith("available commands:\n"))
        self.assertIn("  /status                  Show current session status.\n", result.content)
        self.assertIn("\nUse /help <command> for details.", result.content)

    def test_help_hides_commands_the_actor_cannot_use(self) -> None:
        harness = Harness(admin=False)
        result = run(harness.router.dispatch(harness.ctx, "/help"))
        self.assertNotIn("/audit", result.content)
        self.assertNotIn("/log", result.content)
        self.assertIn("/status", result.content)

    def test_detailed_help(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/help audit"))
        self.assertIn("command: audit\n", result.content)
        self.assertIn("usage: /audit [options]\n", result.content)
        self.assertIn("description: Show recent audit events.", result.content)
        self.assertIn("Options:", result.content)
        self.assertFalse(result.content.endswith("\n"))

    def test_detailed_help_unknown_command(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/help nope"))
        self.assertEqual(result.content, "unknown command: nope")

    def test_detailed_help_respects_actor(self) -> None:
        harness = Harness(admin=False)
        result = run(harness.router.dispatch(harness.ctx, "/help audit"))
        self.assertEqual(result.content, "unknown command: audit")

    def test_help_lists_aliases(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/help checkmodel"))
        self.assertIn("aliases: models", result.content)

    def test_wants_command_help_short_circuits(self) -> None:
        # 只有 /model、/log、/usage 检查 --help（与 Go 一致：hook.go 等不检查）。
        harness = Harness()
        for text in ("/model --help", "/audit -h", "/usage --help"):
            result = run(harness.router.dispatch(harness.ctx, text))
            name = text.split()[0].lstrip("/")
            self.assertIn(f"command: {name}\n", result.content, text)

    def test_commands_without_help_flag_treat_it_as_an_argument(self) -> None:
        harness = Harness()
        result = run(harness.router.dispatch(harness.ctx, "/hooks --help"))
        self.assertEqual(
            result.content, 'hook "--help" not found. Use /hooks to list all hooks.'
        )

    def test_help_usage_rewrites_prefix(self) -> None:
        harness = Harness()
        router = new_default_router(harness.deps, ["!"])
        result = run(router.dispatch(harness.ctx, "!help log"))
        self.assertIn("usage: !log [options]", result.content)
        self.assertIn("  !log -w -n 10", result.content)
        self.assertNotIn("/log -w", result.content)

    def test_help_completion(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("help")
        req = make_completion_request("/help au", prefix="/", name="help", args="au")
        items = run(handler.complete(harness.ctx, req))
        self.assertEqual([item.text for item in items], ["audit"])
        self.assertEqual(items[0].kind, "command_arg")
        self.assertEqual(items[0].replace_start, len("/help "))
        self.assertEqual(items[0].replace_end, len("/help au"))

    def test_help_completion_hides_denied_commands(self) -> None:
        harness = Harness(admin=False)
        handler, _ = harness.router.handler("help")
        req = make_completion_request("/help ", prefix="/", name="help", args="")
        items = run(handler.complete(harness.ctx, req))
        texts = [item.text for item in items]
        self.assertIn("status", texts)
        self.assertNotIn("audit", texts)

    def test_help_completion_stops_after_space(self) -> None:
        harness = Harness()
        handler, _ = harness.router.handler("help")
        req = make_completion_request("/help a b", prefix="/", name="help", args="a b")
        self.assertEqual(run(handler.complete(harness.ctx, req)), [])


def make_completion_request(raw: str, prefix: str, name: str, args: str):
    from atri_x.command import CompletionRequest

    return CompletionRequest(
        raw=raw, prefix=prefix, name=name, args=args, cursor=len(raw)
    )


class RouterCompletionTests(unittest.TestCase):
    def test_whole_line_completion(self) -> None:
        harness = Harness()
        actor = harness.actor
        self.assertEqual(harness.router.complete_for_actor("/he", actor), ["/help"])
        # 主名按注册顺序出现，别名紧随其后（Go ``completionNames`` 同样如此）。
        items = harness.router.complete_for_actor("/", actor)
        primaries = [item.lstrip("/") for item in items if item.lstrip("/") in EXPECTED_COMMANDS]
        self.assertEqual(primaries, EXPECTED_COMMANDS)

    def test_completion_includes_aliases_once(self) -> None:
        harness = Harness()
        items = harness.router.complete_for_actor("/model", harness.actor)
        self.assertEqual(items, ["/model", "/models"])

    def test_completion_filters_by_actor(self) -> None:
        harness = Harness()
        user = Actor(id="u2", platform="qq", platform_user_id="2", role=RoleUser)
        items = harness.router.complete_for_actor("/", user)
        self.assertIn("/status", items)
        self.assertNotIn("/audit", items)

    def test_completion_needs_prefix_and_stops_after_space(self) -> None:
        harness = Harness()
        self.assertEqual(harness.router.complete("help"), [])
        self.assertEqual(harness.router.complete("/help "), [])


class ArgumentParsingFailureTests(unittest.TestCase):
    """参数解析失败必须抛异常（Go 的 ``(T, error)`` → Python 异常）。"""

    def test_log_options(self) -> None:
        harness = Harness()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/log --limit abc"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/log --limit 0"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/log --since nonsense"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/log --bogus 1"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, '/log --msg "unterminated'))

    def test_model_option_conflict(self) -> None:
        harness = Harness()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/model --chat --work gpt"))

    def test_usage_option(self) -> None:
        harness = Harness()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/usage --by bogus"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/usage --bogus"))

    def test_session_page_numbers(self) -> None:
        harness = Harness()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/resume --page 0"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/resume --page abc"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/messages 0"))
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/resume 0"))

    def test_fork_requires_message_id(self) -> None:
        harness = Harness()
        harness.create_session()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/fork"))

    def test_faq_unknown_subcommand(self) -> None:
        harness = Harness(group=True)
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/faq bogus"))

    def test_faq_add_requires_separator(self) -> None:
        harness = Harness(group=True)
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/faq add 只有问题"))

    def test_group_services_usage_errors(self) -> None:
        harness = Harness(group=True)
        for text in (
            "/poll 只有问题",
            "/vote",
            "/join",
            "/poll show",
            "/signup join",
            "/remind 只有时间",
        ):
            with self.assertRaises(ValueError, msg=text):
                run(harness.router.dispatch(harness.ctx, text))

    def test_group_services_bare_command_lists(self) -> None:
        # Go：`/remind`、`/poll`、`/signup` 不带参数时等价于 `list`。
        harness = Harness(group=True)
        self.assertIn("当前群提醒", run(harness.router.dispatch(harness.ctx, "/remind")).content)
        self.assertIn("当前群投票", run(harness.router.dispatch(harness.ctx, "/poll")).content)
        self.assertIn("当前群报名", run(harness.router.dispatch(harness.ctx, "/signup")).content)

    def test_delete_without_confirm_asks_for_confirmation(self) -> None:
        harness = Harness()
        session = harness.create_session(title="待删会话")
        result = run(harness.router.dispatch(harness.ctx, f"/delete {session.id}"))
        self.assertIn("delete will modify session:\n", result.content)
        self.assertIn("  title: 待删会话\n", result.content)
        self.assertEqual(
            result.content.splitlines()[-1],
            f"Run /delete {session.id} --confirm to continue.",
        )

    def test_delete_without_target_is_usage_error(self) -> None:
        harness = Harness()
        harness.create_session()
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/delete --confirm"))

    def test_unnumbered_session_target(self) -> None:
        harness = Harness()
        harness.create_session()
        # SessionCommandState 里没有列表时按编号定位失败。
        with self.assertRaises(ValueError):
            run(harness.router.dispatch(harness.ctx, "/archive 3"))


class DepsWiringTests(unittest.TestCase):
    def test_empty_deps_still_registers(self) -> None:
        router = new_default_router(Deps())
        self.assertEqual([info.name for info in router.commands()], EXPECTED_COMMANDS)

    def test_new_default_router_sets_deps_router(self) -> None:
        deps = Deps()
        router = new_default_router(deps)
        self.assertIs(deps.router, router)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
