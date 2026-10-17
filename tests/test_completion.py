"""``atri_x.completion`` 测试。

对应 Go：``internal/completion/{completion_test.go,source_test.go,character_test.go}``。
覆盖来源合并（首个非空胜出）、命令名/参数补全、``@tool:`` / ``@skill:`` 的
tag 与模糊匹配、``@char:`` 过滤、风险确认与 Fork 来源的前置条件。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from atri_x.command import Completion, CompletionRequest, Info, Result, Router, new_func
from atri_x.completion import (
    KIND_CHARACTER_DIRECTIVE,
    KIND_COMMAND,
    KIND_FORK_MESSAGE,
    KIND_RISK_CONFIRMATION,
    KIND_SKILL_DIRECTIVE,
    KIND_TOOL_DIRECTIVE,
    KIND_TOOL_TAG_DIRECTIVE,
    PHASE_AWAIT_RISK_CONFIRM,
    CharacterDirectiveSource,
    CharacterOption,
    ForkMessageSource,
    Item,
    Request,
    RiskConfirmationSource,
    RouterSource,
    Service,
    ToolDirectiveSource,
    fuzzy_match,
    has_any_command_args,
    has_fork_args,
    is_allowed_skill,
    is_plain_allowed_tool,
    match_infos,
    match_strings,
    new_service,
    texts,
)
from atri_x.llm.llm import ToolFunctionSchema, ToolSchema
from atri_x.tool import (
    Actor,
    DetailBlock,
    Info as ToolInfo,
    Registry,
    RiskLow,
    RoleSuperadmin,
    SourceSkillAgent,
    default_policy,
)


class StaticSource:
    def __init__(self, items):
        self._items = items
        self.calls = 0

    async def complete(self, ctx, req):
        self.calls += 1
        return list(self._items)


class PlainTool:
    """普通工具（非 Skill）。"""

    def __init__(self, name: str, description: str = "", hidden: bool = False, risk=RiskLow,
                 tags=None, superadmin_only: bool = False):
        self._name = name
        self._description = description
        self._hidden = hidden
        self._risk = risk
        self._tags = list(tags or [])
        self._superadmin_only = superadmin_only

    def name(self) -> str:
        return self._name

    def info(self) -> ToolInfo:
        return ToolInfo(
            name=self._name,
            description=self._description,
            risk=self._risk,
            hidden=self._hidden,
            tags=self._tags,
            superadmin_only=self._superadmin_only,
        )

    def schema(self) -> ToolSchema:
        return ToolSchema(function=ToolFunctionSchema(name=self._name))

    async def call(self, ctx, req):
        return None


class SkillTool(PlainTool):
    """Skill 类工具：实现 ``DetailProvider``。"""

    def detail(self) -> str:
        return "detail"

    def activate_tools(self) -> list[str]:
        return []


class CommandArgCompleter:
    def __init__(self, name: str = "help") -> None:
        self._name = name

    def info(self) -> Info:
        return Info(name=self._name)

    async def handle(self, ctx, req):
        return Result(content="")

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


class TestServiceMerging(unittest.IsolatedAsyncioTestCase):
    async def test_first_non_empty_source_wins(self) -> None:
        first = StaticSource([])
        second = StaticSource([Item(text="/model")])
        third = StaticSource([Item(text="/models")])
        service = new_service(first, second, third)
        items = await service.complete(None, Request(text="/m"))
        self.assertEqual([item.text for item in items], ["/model"])
        self.assertEqual((first.calls, second.calls, third.calls), (1, 1, 0))

    async def test_no_source_matches_returns_empty(self) -> None:
        service = new_service(StaticSource([]))
        self.assertEqual(await service.complete(None, Request(text="/x")), [])
        self.assertEqual(Service().sources(), [])

    async def test_nil_sources_are_dropped(self) -> None:
        service = new_service(None, StaticSource([Item(text="a")]))
        self.assertEqual(len(service.sources()), 1)

    def test_texts_drops_empty_items(self) -> None:
        self.assertEqual(texts([Item(text="/a"), Item(), Item(text="/b")]), ["/a", "/b"])

    def test_cursor_or_end(self) -> None:
        self.assertEqual(Request(text="/help mo").cursor_or_end(), 8)
        self.assertEqual(Request(text="/help", cursor=3).cursor_or_end(), 3)
        self.assertEqual(Request(text="/help", cursor=-1).cursor_or_end(), 5)
        self.assertEqual(Request(text="/help", cursor=99).cursor_or_end(), 5)


class TestRouterSource(unittest.IsolatedAsyncioTestCase):
    def _router(self) -> Router:
        router = Router(["/"])
        router.register(new_func(Info(name="model", aliases=["m"], min_role="user")))
        router.register(new_func(Info(name="models", min_role="user")))
        router.register(new_func(Info(name="secret", aliases=["s"])))
        router.register(CommandArgCompleter())
        return router

    async def test_completes_commands_in_registration_order(self) -> None:
        source = RouterSource(router=self._router())
        items = await source.complete(None, Request(text="/m"))
        self.assertEqual([item.text for item in items], ["/model", "/m", "/models"])
        for item in items:
            self.assertEqual(item.kind, KIND_COMMAND)
        self.assertEqual(items[0].label, "model")

    async def test_filters_commands_by_actor(self) -> None:
        router = self._router()
        user = RouterSource(router=router, actor=lambda ctx: Actor(role="user"))
        # "help" 的 Info.MinRole 留空 → 按历史行为要求超管，普通用户看不到。
        self.assertEqual(
            [item.text for item in await user.complete(None, Request(text="/"))],
            ["/model", "/m", "/models"],
        )
        self.assertEqual(await user.complete(None, Request(text="/s")), [])
        self.assertEqual(
            await user.complete(None, Request(text="/help mo", cursor=len("/help mo"))), []
        )

        admin = RouterSource(router=router, actor=lambda ctx: Actor(role=RoleSuperadmin))
        self.assertEqual(
            [item.text for item in await admin.complete(None, Request(text="/s"))],
            ["/secret", "/s"],
        )

    async def test_completes_command_args_for_admin(self) -> None:
        source = RouterSource(
            router=self._router(), actor=lambda ctx: Actor(role=RoleSuperadmin)
        )
        items = await source.complete(None, Request(text="/help mo", cursor=len("/help mo")))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "model")
        self.assertEqual(items[0].kind, "command_arg")
        self.assertEqual(items[0].replace_start, len("/help "))
        self.assertEqual(items[0].replace_end, len("/help mo"))

    async def test_no_router_returns_empty(self) -> None:
        self.assertEqual(await RouterSource().complete(None, Request(text="/")), [])

    async def test_actor_defaults_to_context(self) -> None:
        from atri_x.tool import WithActor

        router = self._router()
        ctx = WithActor(None, Actor(role=RoleSuperadmin))
        items = await RouterSource(router=router).complete(ctx, Request(text="/s"))
        self.assertEqual([item.text for item in items], ["/secret", "/s"])

    async def test_argument_completion_needs_whitespace_after_command(self) -> None:
        source = RouterSource(
            router=self._router(), actor=lambda ctx: Actor(role=RoleSuperadmin)
        )
        items = await source.complete(None, Request(text="/help", cursor=len("/help")))
        self.assertEqual([item.text for item in items], ["/help"])


class TestToolDirectiveSource(unittest.IsolatedAsyncioTestCase):
    def _registry(self) -> Registry:
        registry = Registry()
        registry.register(PlainTool("web_search", "search", tags=["web"]))
        registry.register(PlainTool("web_extract", "extract", tags=["web"]))
        registry.register(PlainTool("shell", "shell"))
        registry.register(PlainTool("hidden_tool", hidden=True))
        registry.register(SkillTool("docx"))
        return registry

    def _source(self, registry: Registry | None = None, **kwargs) -> ToolDirectiveSource:
        registry = registry if registry is not None else self._registry()
        kwargs.setdefault("actor", lambda ctx: Actor(role=RoleSuperadmin))
        return ToolDirectiveSource(registry=lambda: registry, **kwargs)

    async def test_completes_tags_then_plain_tools(self) -> None:
        source = self._source()
        text = "查 @tool:we"
        items = await source.complete(None, Request(text=text, cursor=len(text)))
        self.assertEqual(
            [item.text for item in items], ["@tool:web", "@tool:web_extract", "@tool:web_search"]
        )
        self.assertEqual(items[0].label, "web <tag>")
        self.assertEqual(items[0].kind, KIND_TOOL_TAG_DIRECTIVE)
        self.assertEqual(items[0].description, "2 tools")
        self.assertEqual(items[0].replace_start, len("查 "))
        self.assertEqual(items[0].replace_end, len(text))
        self.assertEqual(items[1].kind, KIND_TOOL_DIRECTIVE)
        self.assertEqual(items[1].description, "extract")

    async def test_prefix_only_completion(self) -> None:
        source = self._source()
        text = "查 @t"
        items = await source.complete(None, Request(text=text, cursor=len(text)))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "@t:")
        self.assertEqual(items[0].kind, KIND_TOOL_DIRECTIVE)
        self.assertEqual(items[0].replace_start, len("查 "))
        self.assertEqual(items[0].replace_end, len(text))

    async def test_fuzzy_and_fullwidth_colon(self) -> None:
        source = self._source()
        for text, want in [
            ("@tool:shl", "@tool:shell"),
            ("@t:shl", "@t:shell"),
            ("@tool：shl", "@tool：shell"),
        ]:
            with self.subTest(text=text):
                items = await source.complete(None, Request(text=text, cursor=len(text)))
                self.assertEqual([item.text for item in items], [want])

    async def test_skills_only_complete_on_skill_prefix(self) -> None:
        source = self._source()
        items = await source.complete(None, Request(text="@tool:d", cursor=len("@tool:d")))
        self.assertEqual(items, [])
        for text, want in [("@skill:do", "@skill:docx"), ("@s:do", "@s:docx"), ("@skill：do", "@skill：docx")]:
            with self.subTest(text=text):
                items = await source.complete(None, Request(text=text, cursor=len(text)))
                self.assertEqual([item.text for item in items], [want])
                self.assertEqual(items[0].kind, KIND_SKILL_DIRECTIVE)
        items = await source.complete(None, Request(text="@skill:doc_", cursor=len("@skill:doc_")))
        self.assertEqual(items, [])

    async def test_configured_tags_use_injected_lookups(self) -> None:
        registry = Registry()
        registry.register(PlainTool("alpha", "alpha"))
        seen_tags: list[str] = []

        def tool_names_by_tag(ctx, reg, tag, allowed):
            seen_tags.append(tag)
            return ["alpha"] if tag == "agent" else []

        source = self._source(
            registry,
            tags=lambda ctx, reg, actor, policy: ["agent"],
            tool_names_by_tag=tool_names_by_tag,
        )
        items = await source.complete(None, Request(text="@tool:ag", cursor=len("@tool:ag")))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "@tool:agent")
        self.assertEqual(items[0].description, "1 tool")
        self.assertIn("agent", seen_tags)

    async def test_no_registry_returns_empty(self) -> None:
        source = ToolDirectiveSource(registry=lambda: None)
        self.assertEqual(await source.complete(None, Request(text="@tool:a")), [])
        self.assertEqual(await ToolDirectiveSource().complete(None, Request(text="@tool:a")), [])

    async def test_hidden_and_non_skill_tools_are_filtered(self) -> None:
        registry = Registry()
        registry.register(PlainTool("visible"))
        registry.register(PlainTool("hidden", hidden=True))
        registry.register(SkillTool("skill"))
        registry.register(PlainTool("admin", superadmin_only=True))
        user_source = self._source(registry, actor=lambda ctx: Actor(role="user"))
        items = await user_source.complete(None, Request(text="@tool:", cursor=7))
        self.assertEqual([item.text for item in items], ["@tool:visible"])
        skills = await user_source.complete(None, Request(text="@skill:", cursor=8))
        self.assertEqual([item.text for item in skills], ["@skill:skill"])

    async def test_empty_prefix_only_token(self) -> None:
        # "@tool:" 自身就是完整前缀（不是某个前缀的前缀），因此按 query="" 列出全部
        # tag 与工具；prefix_only 只处理 "@t" 这种"还差一个冒号"的输入。
        source = self._source()
        items = await source.complete(None, Request(text="@tool:", cursor=6))
        self.assertEqual(
            [item.text for item in items],
            ["@tool:web", "@tool:shell", "@tool:web_extract", "@tool:web_search"],
        )
        self.assertEqual(items[0].kind, KIND_TOOL_TAG_DIRECTIVE)
        self.assertEqual(items[1].kind, KIND_TOOL_DIRECTIVE)


class TestMatchHelpers(unittest.TestCase):
    def test_match_infos_prefers_prefix_then_fuzzy(self) -> None:
        infos = [ToolInfo(name="web_search"), ToolInfo(name="web_extract"), ToolInfo(name="wfs")]
        self.assertEqual([i.name for i in match_infos(infos, "")], ["web_search", "web_extract", "wfs"])
        self.assertEqual(
            [i.name for i in match_infos(infos, "web")], ["web_search", "web_extract"]
        )
        self.assertEqual([i.name for i in match_infos(infos, "wfs")], ["wfs"])
        self.assertEqual([i.name for i in match_infos(infos, "zz")], [])

    def test_match_strings_mirrors_match_infos(self) -> None:
        self.assertEqual(match_strings(["web", "ops"], ""), ["web", "ops"])
        self.assertEqual(match_strings(["web", "ops"], "op"), ["ops"])
        self.assertEqual(match_strings(["web", "ops"], "z"), [])

    def test_fuzzy_match_is_subsequence_case_insensitive(self) -> None:
        self.assertTrue(fuzzy_match("web_search", "wsh"))
        self.assertTrue(fuzzy_match("WebSearch", "ws"))
        self.assertTrue(fuzzy_match("any", ""))
        self.assertFalse(fuzzy_match("web", "bw"))

    def test_plain_and_skill_predicates(self) -> None:
        actor = Actor(role=RoleSuperadmin)
        policy = default_policy()
        self.assertTrue(is_plain_allowed_tool(PlainTool("a"), actor, policy))
        self.assertFalse(is_plain_allowed_tool(SkillTool("s"), actor, policy))
        self.assertFalse(is_plain_allowed_tool(PlainTool("discover_tool"), actor, policy))
        self.assertFalse(is_plain_allowed_tool(PlainTool("h", hidden=True), actor, policy))
        self.assertTrue(is_allowed_skill(SkillTool("s"), actor, policy))
        self.assertFalse(is_allowed_skill(PlainTool("a"), actor, policy))
        self.assertFalse(is_allowed_skill(SkillTool("h", hidden=True), actor, policy))


class TestCharacterSource(unittest.IsolatedAsyncioTestCase):
    async def test_filters_by_id_and_name(self) -> None:
        options = [
            CharacterOption(id="atri", name="亚托莉", description="机器人"),
            CharacterOption(id="kuma", name="熊"),
        ]
        source = CharacterDirectiveSource(characters=lambda ctx: options)
        items = await source.complete(None, Request(text="@char:at", cursor=len("@char:at")))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text, "@char:atri")
        self.assertEqual(items[0].description, "亚托莉 · 机器人")
        self.assertEqual(items[0].kind, KIND_CHARACTER_DIRECTIVE)

        # 查询串只能是 [A-Za-z0-9_.-]（Go 的 IsToolNameByte 限制），
        # 因此按名字匹配只可能命中拉丁字符的名字。
        by_name = await source.complete(None, Request(text="@c:KU", cursor=len("@c:KU")))
        self.assertEqual([item.text for item in by_name], ["@c:kuma"])
        self.assertEqual(by_name[0].description, "熊")

        all_items = await source.complete(None, Request(text="@char:", cursor=len("@char:")))
        self.assertEqual(len(all_items), 2)
        self.assertEqual(all_items[0].kind, KIND_CHARACTER_DIRECTIVE)

    async def test_multibyte_query_is_not_a_token(self) -> None:
        from atri_x.directive import tool as directive

        # Go 的 IsToolNameByte 不收多字节字符，中文查询直接判为"不是补全 token"。
        self.assertFalse(directive.parse_character_completion_token("@c:熊", 4).ok)

    async def test_prefix_only_and_missing_lister(self) -> None:
        source = CharacterDirectiveSource(characters=lambda ctx: [])
        items = await source.complete(None, Request(text="@c", cursor=2))
        self.assertEqual([item.text for item in items], ["@c:"])
        self.assertEqual(await CharacterDirectiveSource().complete(None, Request(text="@c:")), [])
        self.assertEqual(
            await source.complete(None, Request(text="hello", cursor=5)), []
        )


class TestRiskConfirmationSource(unittest.IsolatedAsyncioTestCase):
    def _build(self, phase: str, command_names=("confirm", "cancel"), text="/conf"):
        router = Router(["/"])
        router.register(new_func(Info(name="confirm")))
        router.register(new_func(Info(name="cancel")))
        sessions = SimpleNamespace(
            current=self._current,
            _session=SimpleNamespace(id="s1"),
        )
        turns = SimpleNamespace(snapshot=lambda session_id: SimpleNamespace(phase=phase))
        source = RiskConfirmationSource(
            router=router,
            sessions=sessions,
            turns=turns,
            scope=lambda ctx: "scope",
            command_names=list(command_names),
        )
        return source, text

    async def _current(self, ctx, scope):
        return SimpleNamespace(id="s1")

    async def test_completes_only_while_awaiting_confirmation(self) -> None:
        source, text = self._build(PHASE_AWAIT_RISK_CONFIRM)
        items = await source.complete(None, Request(text=text, cursor=len(text)))
        self.assertEqual([item.text for item in items], ["/confirm"])
        self.assertEqual(items[0].kind, KIND_RISK_CONFIRMATION)

    async def test_returns_empty_in_other_phases_or_with_args(self) -> None:
        source, text = self._build("idle")
        self.assertEqual(await source.complete(None, Request(text=text, cursor=len(text))), [])
        source, _ = self._build(PHASE_AWAIT_RISK_CONFIRM)
        self.assertEqual(await source.complete(None, Request(text="/conf now")), [])
        # 只写了前缀、还没写命令名时（parsed.name == ""）Go 会列出全部候选。
        self.assertEqual(
            [item.text for item in await source.complete(None, Request(text="/"))],
            ["/confirm", "/cancel"],
        )
        self.assertEqual(await source.complete(None, Request(text="/zzz")), [])

    async def test_requires_all_dependencies(self) -> None:
        self.assertEqual(
            await RiskConfirmationSource().complete(None, Request(text="/c")), []
        )
        source, text = self._build(PHASE_AWAIT_RISK_CONFIRM, command_names=())
        self.assertEqual(await source.complete(None, Request(text=text)), [])

    async def test_session_or_turn_failure_is_swallowed(self) -> None:
        async def boom(ctx, scope):
            raise RuntimeError("db down")

        source = RiskConfirmationSource(
            router=Router(["/"]),
            sessions=SimpleNamespace(current=boom),
            turns=SimpleNamespace(snapshot=lambda sid: SimpleNamespace(phase=PHASE_AWAIT_RISK_CONFIRM)),
            scope=lambda ctx: "scope",
            command_names=["confirm"],
        )
        self.assertEqual(await source.complete(None, Request(text="/c")), [])

    def test_has_any_command_args(self) -> None:
        self.assertFalse(has_any_command_args("/confirm", "/"))
        self.assertFalse(has_any_command_args("  /confirm  ", "/"))
        self.assertTrue(has_any_command_args("/confirm now", "/"))
        self.assertFalse(has_any_command_args("confirm now", "/"))
        self.assertFalse(has_any_command_args("/confirm  ", "/"))


class TestForkSource(unittest.IsolatedAsyncioTestCase):
    class Loader:
        def __init__(self, store):
            self.store = store

        async def load(self, ctx, session_id):
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(id="m1", role="assistant", content="hi"),
                    SimpleNamespace(id="m2", role="user", content="hi"),
                    SimpleNamespace(id="m3", role="assistant", content="   "),
                    SimpleNamespace(id="a1", role="assistant", content="ok"),
                ]
            )

    def _source(self, text_router: Router | None = None) -> ForkMessageSource:
        router = text_router or Router(["/"])
        if not router.handler("fork")[1]:
            router.register(new_func(Info(name="fork")))

        async def current(ctx, scope):
            return SimpleNamespace(id="s1")

        return ForkMessageSource(
            router=router,
            sessions=SimpleNamespace(current=current),
            store=object(),
            scope=lambda ctx: "scope",
            loader_factory=self.Loader,
        )

    async def test_lists_assistant_messages_matching_prefix(self) -> None:
        source = self._source()
        items = await source.complete(None, Request(text="/fork m", cursor=len("/fork m")))
        self.assertEqual([item.text for item in items], ["/fork m1"])
        self.assertEqual(items[0].label, "m1")
        self.assertEqual(items[0].kind, KIND_FORK_MESSAGE)

        # 只有 "/fork" 后面出现空格才算进入参数区（Go 的 hasForkArgs），
        # 此时 prefix 为空 → 列出全部非空助手消息。
        self.assertEqual(
            await source.complete(None, Request(text="/fork", cursor=len("/fork"))), []
        )
        all_items = await source.complete(None, Request(text="/fork ", cursor=len("/fork ")))
        self.assertEqual([item.label for item in all_items], ["m1", "a1"])

    async def test_only_matches_fork_command_with_args(self) -> None:
        source = self._source()
        router = source.router
        router.register(new_func(Info(name="help")))
        self.assertEqual(await source.complete(None, Request(text="/help m")), [])
        self.assertEqual(await source.complete(None, Request(text="hello")), [])

    async def test_returns_empty_without_dependencies(self) -> None:
        self.assertEqual(await ForkMessageSource().complete(None, Request(text="/fork m")), [])

    def test_has_fork_args(self) -> None:
        self.assertTrue(has_fork_args("/fork m", "/"))
        self.assertTrue(has_fork_args("  /fork ", "/"))
        self.assertFalse(has_fork_args("/fork", "/"))
        self.assertFalse(has_fork_args("/help m", "/"))
        self.assertFalse(has_fork_args("fork m", "/"))

    def test_kind_constants(self) -> None:
        self.assertEqual(
            (KIND_COMMAND, KIND_TOOL_DIRECTIVE, KIND_TOOL_TAG_DIRECTIVE),
            ("command", "tool_directive", "tool_tag_directive"),
        )
        self.assertEqual((KIND_SKILL_DIRECTIVE, KIND_CHARACTER_DIRECTIVE), ("skill_directive", "character_directive"))
        self.assertEqual((KIND_RISK_CONFIRMATION, KIND_FORK_MESSAGE), ("risk_confirmation", "fork_message"))
        self.assertEqual(PHASE_AWAIT_RISK_CONFIRM, "awaiting_risk_confirm")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
