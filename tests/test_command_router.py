"""``atri_x.command`` 测试。

对应 Go：``internal/command/{router_test.go,prefix_test.go,router_group_admin_test.go}``。
覆盖注册/别名冲突、解析（最长前缀）、派发、按 actor 过滤、补全与
``SessionEffect`` 位标志、``Info`` 的授权字段。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.command import (
    Completion,
    CompletionRequest,
    Continuation,
    FuncHandler,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    Info,
    Parsed,
    Request,
    Result,
    RoleSuperadmin,
    RoleUser,
    Router,
    SessionEffect,
    SessionEffectMutate,
    SessionEffectNone,
    SessionEffectSwitchCurrent,
    can_access,
    new_func,
    new_router,
    normalize_name,
    normalize_prefixes,
    primary_prefix,
)
from atri_x.tool.capability import Actor


class Echo:
    """记录调用并按需返回结果的命令。"""

    def __init__(self, info: Info, result: Result | None = None) -> None:
        self._info = info
        self._result = result if result is not None else Result(content="ok")
        self.requests: list[Request] = []

    def info(self) -> Info:
        return self._info

    async def handle(self, ctx, req: Request) -> Result | None:
        self.requests.append(req)
        return self._result


class ArgCompleter(Echo):
    """带参数补全的命令（对应 Go 测试里的 commandArgCompleter）。"""

    async def complete(self, ctx, req: CompletionRequest) -> list[Completion]:
        return [
            Completion(
                text="model",
                label="model",
                kind="command_arg",
                replace_start=len("/help "),
                replace_end=req.cursor,
            )
        ]


class TestPrefixHelpers(unittest.TestCase):
    def test_primary_prefix(self) -> None:
        cases = [
            (None, "/"),
            ([], "/"),
            (["", "   "], "/"),
            (["/"], "/"),
            (["/*"], "/*"),
            (["/", "/*"], "/"),
        ]
        for prefixes, want in cases:
            with self.subTest(prefixes=prefixes):
                self.assertEqual(primary_prefix(prefixes), want)

    def test_normalize_prefixes_dedupes_and_sorts_by_length(self) -> None:
        self.assertEqual(normalize_prefixes(None), ["/"])
        self.assertEqual(normalize_prefixes(["", "  "]), ["/"])
        self.assertEqual(normalize_prefixes(["/", "//", "///"]), ["///", "//", "/"])

    def test_normalize_name_strips_one_slash_then_trims(self) -> None:
        # Go: ToLower(TrimSpace(TrimPrefix(name, "/")))——先剥前导 "/" 再 trim，
        # 因此带前导空格的 "/Audit" 里的斜杠会被保留下来。
        self.assertEqual(normalize_name("/Audit"), "audit")
        self.assertEqual(normalize_name("  /Audit  "), "/audit")
        self.assertEqual(normalize_name(" //Audit"), "//audit")
        self.assertEqual(normalize_name(""), "")

    def test_router_default_prefix(self) -> None:
        router = Router()
        self.assertEqual(router.primary_prefix(), "/")
        self.assertEqual(router.prefixes(), ["/"])
        parsed = router.parse("/help model")
        self.assertEqual(parsed, Parsed(ok=True, prefix="/", name="help", args="model"))
        self.assertTrue(router.is_command("/help"))
        legacy = router.parse("/*help")
        self.assertEqual((legacy.prefix, legacy.name), ("/", "*help"))

    def test_longest_prefix_wins(self) -> None:
        router = Router(["/", "//"])
        parsed = router.parse("//help")
        self.assertEqual((parsed.ok, parsed.prefix, parsed.name), (True, "//", "help"))


class TestRouterRegistration(unittest.TestCase):
    def test_dispatch_and_request_fields(self) -> None:
        router = Router(["/", "-"])
        handler = Echo(
            Info(name="ping", usage="/ping [text]", description="Ping command.", aliases=["p"])
        )
        router.register(handler)
        result = asyncio.run(router.dispatch(None, "-ping hello"))
        self.assertEqual(result.content, "ok")
        self.assertEqual(
            handler.requests[0], Request(raw="-ping hello", prefix="-", name="ping", args="hello")
        )
        self.assertEqual([info.name for info in router.commands()], ["ping"])

    def test_rejects_duplicate_alias(self) -> None:
        router = Router(["/"])
        router.register(new_func(Info(name="one", aliases=["x"])))
        with self.assertRaises(Exception) as ctx:
            router.register(new_func(Info(name="two", aliases=["x"])))
        self.assertIn("already registered", str(ctx.exception))

    def test_rejects_duplicate_alias_inside_same_command(self) -> None:
        router = Router(["/"])
        with self.assertRaises(Exception) as ctx:
            router.register(new_func(Info(name="one", aliases=["one"])))
        self.assertIn("duplicate alias", str(ctx.exception))

    def test_rejects_empty_name_and_none_handler(self) -> None:
        router = Router(["/"])
        with self.assertRaises(Exception):
            router.register(new_func(Info(name="   ")))
        with self.assertRaises(Exception):
            router.register(None)

    def test_register_handlers_stops_on_error(self) -> None:
        router = Router(["/"])
        with self.assertRaises(Exception):
            router.register_handlers(new_func(Info(name="a")), new_func(Info(name="a")))

    def test_command_info_finds_primary_and_alias_without_mutating_handler(self) -> None:
        router = Router(["/"])
        handler = new_func(Info(name="Audit", usage="/audit", aliases=["Aud"], help="details"))
        router.register(handler)
        for name in ("audit", "aud", "/audit", "AUD"):
            with self.subTest(name=name):
                info, ok = router.command_info(name)
                self.assertTrue(ok)
                self.assertEqual(info.name, "audit")
                self.assertEqual(info.help, "details")
        # handler 自己的 Info 不被就地改写（Go 的值拷贝语义）
        self.assertEqual(handler.info().name, "Audit")
        self.assertEqual(router.command_info("nope"), (None, False))

    def test_dispatch_unknown_command_returns_hint(self) -> None:
        router = Router(["/"])
        result = asyncio.run(router.dispatch(None, "/missing"))
        self.assertIn("unknown command: /missing", result.content)
        self.assertIn("type /help for available commands", result.content)

    def test_dispatch_non_command_raises(self) -> None:
        router = Router(["/"])
        with self.assertRaises(Exception):
            asyncio.run(router.dispatch(None, "hello"))

    def test_parse_empty_and_prefix_only(self) -> None:
        router = Router(["/"])
        self.assertEqual(router.parse("   "), Parsed())
        self.assertEqual(router.parse("/"), Parsed(ok=True, prefix="/"))
        self.assertFalse(router.is_command("hello"))

    def test_dispatch_passes_prefix_and_args_through(self) -> None:
        router = Router(["/", "!"])
        handler = Echo(Info(name="echo"))
        router.register(handler)
        asyncio.run(router.dispatch(None, "!echo  a  b "))
        self.assertEqual(handler.requests[0].args, "a  b")


class TestActorFiltering(unittest.TestCase):
    def test_filters_commands_for_actor(self) -> None:
        router = Router(["/"])
        router.register(new_func(Info(name="public", aliases=["p"], min_role=RoleUser)))
        router.register(new_func(Info(name="secret", aliases=["s"])))

        user = Actor(role=RoleUser)
        self.assertEqual([info.name for info in router.commands_for_actor(user)], ["public"])
        info, ok = router.command_info_for_actor("s", user)
        self.assertFalse(ok)
        self.assertIsNone(info)
        self.assertEqual(router.complete_for_actor("/", user), ["/public", "/p"])

        admin = Actor(role=RoleSuperadmin)
        self.assertEqual(len(router.commands_for_actor(admin)), 2)
        info, ok = router.command_info_for_actor("s", admin)
        self.assertTrue(ok)
        self.assertEqual(info.name, "secret")
        self.assertEqual(router.complete_for_actor("/s", admin), ["/secret", "/s"])

    def test_group_admins_allowed_only_when_configured(self) -> None:
        router = Router(["/"])
        router.register(new_func(Info(name="overflow", allow_group_admin=True)))
        member = Actor(role=RoleUser, group_role=GroupRoleMember)
        self.assertFalse(router.command_info_for_actor("overflow", member)[1])
        self.assertTrue(
            router.command_info_for_actor(
                "overflow", Actor(role=RoleUser, group_role=GroupRoleAdmin)
            )[1]
        )
        self.assertTrue(
            router.command_info_for_actor(
                "overflow", Actor(role=RoleUser, group_role=GroupRoleOwner)
            )[1]
        )
        self.assertTrue(
            router.command_info_for_actor(
                "overflow", Actor(role=RoleSuperadmin, group_role=GroupRoleMember)
            )[1]
        )

    def test_group_admin_needs_grant_is_declared_on_info(self) -> None:
        info = Info(name="moderation", allow_group_admin=True, group_admin_needs_grant=True)
        self.assertTrue(info.group_admin_needs_grant)
        self.assertTrue(can_access(info, Actor(role=RoleUser, group_role=GroupRoleAdmin)))

    def test_group_admin_flag_absent_blocks_group_admin(self) -> None:
        info = Info(name="private")
        self.assertFalse(can_access(info, Actor(role=RoleUser, group_role=GroupRoleOwner)))
        self.assertTrue(can_access(info, Actor(role=RoleSuperadmin)))


class TestSessionEffect(unittest.TestCase):
    def test_bit_flags_match_go(self) -> None:
        self.assertEqual(SessionEffect.NONE, 0)
        self.assertEqual(SessionEffect.SWITCH_CURRENT, 1)
        self.assertEqual(SessionEffect.MUTATE, 2)
        self.assertEqual(SessionEffectNone, 0)
        self.assertEqual(SessionEffectSwitchCurrent, 1)
        self.assertEqual(SessionEffectMutate, 2)

    def test_flags_combine_like_a_bitmask(self) -> None:
        combined = SessionEffect.SWITCH_CURRENT | SessionEffect.MUTATE
        self.assertEqual(combined, 3)
        self.assertTrue(combined & SessionEffect.SWITCH_CURRENT)
        self.assertTrue(combined & SessionEffect.MUTATE)
        self.assertFalse(SessionEffectNone & SessionEffect.MUTATE)

    def test_info_carries_session_effect(self) -> None:
        info = Info(name="new", session_effect=SessionEffect.SWITCH_CURRENT)
        self.assertEqual(info.session_effect, 1)


class TestCompletion(unittest.TestCase):
    def test_complete_commands_uses_registration_order(self) -> None:
        router = Router(["/", "-"])
        router.register(new_func(Info(name="model", aliases=["m"])))
        router.register(new_func(Info(name="models")))
        router.register(new_func(Info(name="resume")))
        self.assertEqual(router.complete("/m"), ["/model", "/m", "/models"])
        self.assertEqual(router.complete("-res"), ["-resume"])
        self.assertEqual(router.complete("/model "), [])
        self.assertEqual(router.complete("hello"), [])


class TestHandlerHelpers(unittest.TestCase):
    def test_new_func_wraps_callable(self) -> None:
        seen: list[Request] = []

        async def fn(ctx, req):
            seen.append(req)
            return Result(content="pong", continuation=Continuation(text="t", session_id="s"))

        handler = new_func(Info(name="ping"), fn)
        self.assertIsInstance(handler, FuncHandler)
        result = asyncio.run(handler.handle(None, Request(name="ping")))
        self.assertEqual(result.content, "pong")
        self.assertEqual(result.continuation.session_id, "s")
        self.assertEqual(len(seen), 1)

    def test_func_handler_without_fn_returns_none(self) -> None:
        self.assertIsNone(asyncio.run(new_func(Info(name="x")).handle(None, Request())))

    def test_new_router_helper(self) -> None:
        router = new_router(["-"])
        self.assertEqual(router.primary_prefix(), "-")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
