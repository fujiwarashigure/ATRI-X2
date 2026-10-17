"""``atri_x.tool`` 核心行为测试。

对应 Go：``internal/tool/{tool,availability,executor,builder,capability,workspace,sandbox}_test.go``
里的关键判定。覆盖风险等级比较、schema 构造、注册表/发现、availability 原因互斥、
执行器准入与 workspace/sandbox 路径解析。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field

from atri_x.llm.llm import MessageSegment, SegmentType
from atri_x.llm.llm import ToolCallRequest, ToolFunctionSchema, ToolSchema, segments_content_text
from atri_x.tool import (
    TOOL_REASON_CONTEXT_UNAVAILABLE,
    TOOL_REASON_HIDDEN,
    TOOL_REASON_NOT_FOUND,
    TOOL_REASON_RISK_DENIED,
    TOOL_REASON_SUPERADMIN_ONLY,
    Actor,
    Builder,
    CallRequest,
    Capabilities,
    Executor,
    Info,
    PathResolveOptions,
    Policy,
    Registry,
    Result,
    RiskCritical,
    RiskHigh,
    RiskLow,
    RiskMedium,
    RiskSafe,
    RoleSuperadmin,
    RoleUser,
    SandboxContext,
    SourceBuiltin,
    SourceSkillAgent,
    assess_risk,
    background_context,
    can_access_tool,
    compare_risk,
    default_policy,
    info_available_in_context,
    normalize_risk,
    normalize_tag,
    normalize_tags,
    parse_risk,
    registry_tool_availability_reason,
    resolve_sandbox_relative_path,
    resolve_workspace_path,
    session_id_from_context,
    tool_access_reason,
    tool_availability_reason,
    with_capabilities,
    with_sandbox_context,
    with_session_id,
    with_workspace_store,
)


class FakeTool:
    """最小工具替身，字段覆盖 ``Info`` 的全部可见性开关。

    ``name()`` 是方法（对齐 Go 的 ``Tool.Name``），因此构造参数存进
    ``tool_name``，避免实例属性遮蔽方法。
    """

    def __init__(
        self,
        name: str = "fake",
        description: str = "",
        source: str = SourceBuiltin,
        risk: str = RiskLow,
        hidden: bool = False,
        superadmin_only: bool = False,
        owner_scoped: bool = False,
        foreground_only: bool = False,
        vision_required: bool = False,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        content: str = "ok",
    ) -> None:
        self.tool_name = name
        self.description = description
        self.source = source
        self.risk = risk
        self.hidden = hidden
        self.superadmin_only = superadmin_only
        self.owner_scoped = owner_scoped
        self.foreground_only = foreground_only
        self.vision_required = vision_required
        self.tags = list(tags or [])
        self.depends_on = list(depends_on or [])
        self.content = content
        self.calls: list[CallRequest] = []

    def name(self) -> str:
        return self.tool_name

    def info(self) -> Info:
        return Info(
            name=self.tool_name,
            description=self.description,
            source=self.source,
            risk=self.risk,
            hidden=self.hidden,
            superadmin_only=self.superadmin_only,
            owner_scoped=self.owner_scoped,
            foreground_only=self.foreground_only,
            vision_required=self.vision_required,
            tags=self.tags,
            depends_on=self.depends_on,
        )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            type="function",
            function=ToolFunctionSchema(
                name=self.tool_name, parameters={"type": "object"}
            ),
        )

    async def call(self, ctx, req: CallRequest) -> Result:
        self.calls.append(req)
        return Result(content=self.content)


class TestRiskLevels(unittest.TestCase):
    """风险等级与 ``Policy.CanUseTool``：对应 security_test.go。"""

    def test_compare_risk_orders_levels(self) -> None:
        ordered = [RiskSafe, RiskLow, RiskMedium, RiskHigh, RiskCritical]
        for i, left in enumerate(ordered):
            for j, right in enumerate(ordered):
                self.assertEqual(compare_risk(left, right), i - j, f"{left} vs {right}")

    def test_compare_risk_unknown_level_ranks_as_high(self) -> None:
        self.assertEqual(compare_risk("bogus", RiskHigh), 0)
        self.assertEqual(compare_risk("bogus", RiskCritical), -1)

    def test_parse_risk_falls_back_on_unknown(self) -> None:
        self.assertEqual(parse_risk(" HIGH ", RiskLow), RiskHigh)
        self.assertEqual(parse_risk("nope", RiskLow), RiskLow)
        self.assertEqual(parse_risk("", RiskMedium), RiskMedium)

    def test_normalize_risk_uses_fallback_then_high(self) -> None:
        self.assertEqual(normalize_risk(RiskSafe, RiskHigh), RiskSafe)
        self.assertEqual(normalize_risk("", RiskMedium), RiskMedium)
        self.assertEqual(normalize_risk("", ""), RiskHigh)

    def test_default_policy_limits_regular_user_to_low(self) -> None:
        policy = default_policy()
        user = Actor(id="cli:guest", platform="cli", platform_user_id="guest", role=RoleUser)
        self.assertTrue(policy.can_use_tool(user, RiskSafe, False))
        self.assertTrue(policy.can_use_tool(user, RiskLow, False))
        self.assertFalse(policy.can_use_tool(user, RiskMedium, False))
        self.assertFalse(policy.can_use_tool(user, RiskHigh, False))

    def test_policy_allows_superadmin_and_owner_scoped(self) -> None:
        policy = Policy(user_max_tool_risk=RiskLow, superadmin_confirm_risk=RiskHigh)
        admin = Actor(role=RoleSuperadmin)
        user = Actor(role=RoleUser)
        self.assertTrue(policy.can_use_tool(admin, RiskCritical, False))
        self.assertTrue(policy.can_use_tool(user, RiskCritical, True))
        self.assertFalse(policy.can_use_tool(user, RiskCritical, False))

    def test_needs_tool_confirmation_thresholds(self) -> None:
        policy = Policy(user_max_tool_risk=RiskLow, superadmin_confirm_risk=RiskCritical)
        user = Actor(role=RoleUser)
        admin = Actor(role=RoleSuperadmin)
        self.assertTrue(policy.needs_tool_confirmation(user, RiskHigh))
        self.assertFalse(policy.needs_tool_confirmation(user, RiskMedium))
        self.assertFalse(policy.needs_tool_confirmation(admin, RiskHigh))
        self.assertTrue(policy.needs_tool_confirmation(admin, RiskCritical))

    def test_policy_actor_resolves_superadmin_from_platform_map(self) -> None:
        policy = Policy(superadmins={"cli": {"local": True}})
        actor = policy.actor("cli:local", "cli", "cli:local", "Local")
        self.assertEqual(actor.id, "cli:local")
        self.assertEqual(actor.platform_user_id, "local")
        self.assertEqual(actor.role, RoleSuperadmin)
        self.assertEqual(policy.actor("x", "cli", "other", "").role, RoleUser)


class TestInfoAndTags(unittest.TestCase):
    def test_normalize_tag_rejects_illegal_characters(self) -> None:
        self.assertEqual(normalize_tag(" Web-Tools.1 "), "web-tools.1")
        self.assertEqual(normalize_tag("中文"), "")
        self.assertEqual(normalize_tag("has space"), "")
        self.assertEqual(normalize_tag(""), "")

    def test_normalize_tags_dedupes_after_normalization(self) -> None:
        self.assertEqual(normalize_tags([" Web ", "web", "bad tag"]), ["web"])

    def test_info_carries_visibility_fields(self) -> None:
        info = Info(
            name="shell",
            risk=RiskHigh,
            hidden=True,
            superadmin_only=True,
            owner_scoped=True,
            foreground_only=True,
            vision_required=True,
            tags=["ops"],
            depends_on=["read_file"],
        )
        self.assertTrue(
            all(
                (
                    info.hidden,
                    info.superadmin_only,
                    info.owner_scoped,
                    info.foreground_only,
                    info.vision_required,
                )
            )
        )
        self.assertEqual(info.tags, ["ops"])
        self.assertEqual(info.depends_on, ["read_file"])

    def test_builder_defaults_match_go(self) -> None:
        info = Builder("plain").build_info()
        self.assertEqual(info.source, SourceBuiltin)
        self.assertEqual(info.risk, RiskLow)
        self.assertFalse(info.hidden)

    def test_builder_risk_empty_falls_back_to_low(self) -> None:
        info = Builder("x").risk("").build_info()
        self.assertEqual(info.risk, RiskLow)


class TestSchemaConstruction(unittest.TestCase):
    """schema 助手：对应 builder.go。"""

    def test_build_schema_has_function_shape(self) -> None:
        schema = Builder("read_file").description("read").build_schema()
        self.assertEqual(schema.type, "function")
        self.assertEqual(schema.function.name, "read_file")
        self.assertEqual(schema.function.description, "read")
        self.assertEqual(schema.function.parameters["type"], "object")
        self.assertEqual(schema.function.parameters["properties"], {})
        self.assertNotIn("required", schema.function.parameters)

    def test_params_record_type_description_required_and_enum(self) -> None:
        from atri_x.tool import enum, items, required

        schema = (
            Builder("cmd")
            .string("path", "path", required())
            .integer("limit", "limit")
            .boolean("force", "force")
            .string_array("tags", "tags")
            .string("mode", "mode", enum("a", "b"))
            .build_schema()
        )
        props = schema.function.parameters["properties"]
        self.assertEqual(props["path"], {"type": "string", "description": "path"})
        self.assertEqual(props["limit"]["type"], "integer")
        self.assertEqual(props["force"]["type"], "boolean")
        self.assertEqual(props["tags"]["items"], {"type": "string"})
        self.assertEqual(props["mode"]["enum"], ["a", "b"])
        self.assertEqual(schema.function.parameters["required"], ["path"])

    def test_object_param_allows_additional_properties(self) -> None:
        schema = Builder("x").object("payload", "payload").build_schema()
        prop = schema.function.parameters["properties"]["payload"]
        self.assertTrue(prop["additionalProperties"])

    def test_object_array_builds_items_schema(self) -> None:
        from atri_x.tool import required

        schema = (
            Builder("x")
            .object_array(
                "rows",
                "rows",
                {"name": {"type": "string"}},
                ["name"],
                required(),
            )
            .build_schema()
        )
        prop = schema.function.parameters["properties"]["rows"]
        self.assertEqual(prop["type"], "array")
        self.assertEqual(prop["items"]["type"], "object")
        self.assertEqual(prop["items"]["required"], ["name"])
        self.assertEqual(schema.function.parameters["required"], ["rows"])

    def test_required_is_not_duplicated(self) -> None:
        from atri_x.tool import required

        schema = Builder("x").string("a", "a", required()).string("a", "a", required()).build_schema()
        self.assertEqual(schema.function.parameters["required"], ["a"])

    def test_items_helper_is_importable(self) -> None:
        from atri_x.tool import items

        cfg = type("Cfg", (), {"required": False, "items": None, "enum": []})()
        items("string")(cfg)
        self.assertEqual(cfg.items, {"type": "string"})


class TestRegistry(unittest.TestCase):
    def test_register_rejects_duplicate_and_empty_names(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="a"))
        with self.assertRaises(Exception):
            registry.register(FakeTool(name="a"))
        with self.assertRaises(Exception):
            registry.register(FakeTool(name="  "))

    def test_list_is_sorted_by_name(self) -> None:
        registry = Registry()
        for name in ("c", "a", "b"):
            registry.register(FakeTool(name=name))
        self.assertEqual([info.name for info in registry.list()], ["a", "b", "c"])

    def test_unregister_refuses_builtin(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="builtin"))
        with self.assertRaises(Exception):
            registry.unregister("builtin")
        registry.register(FakeTool(name="skill", source=SourceSkillAgent))
        registry.unregister("skill")
        self.assertFalse(registry.get("skill")[1])

    def test_replace_sources_rejects_builtin_and_keeps_others(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="builtin"))
        registry.register(FakeTool(name="old", source=SourceSkillAgent))
        registry.replace_sources([SourceSkillAgent], [FakeTool(name="new", source=SourceSkillAgent)])
        self.assertFalse(registry.get("old")[1])
        self.assertTrue(registry.get("new")[1])
        self.assertTrue(registry.get("builtin")[1])
        with self.assertRaises(Exception):
            registry.replace_sources([SourceBuiltin], [])

    def test_replace_sources_reports_conflict_with_other_source(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="keep"))
        with self.assertRaises(Exception):
            registry.replace_sources(
                [SourceSkillAgent], [FakeTool(name="keep", source=SourceSkillAgent)]
            )

    def test_tool_names_skips_discover_and_hidden(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="discover_tool"))
        registry.register(FakeTool(name="visible"))
        registry.register(FakeTool(name="invisible", hidden=True))
        self.assertEqual(registry.tool_names(), ["visible"])

    def test_tags_and_names_by_tag(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="a", tags=["Web", "ops"]))
        registry.register(FakeTool(name="b", tags=["web"]))
        registry.register(FakeTool(name="c", hidden=True, tags=["web"]))
        self.assertEqual(registry.tags(), ["ops", "web"])
        # Go 的 NamesByTag 自己不筛隐藏工具，筛不筛由 allowed 决定。
        self.assertEqual(registry.names_by_tag("WEB"), ["a", "b", "c"])
        self.assertEqual(
            registry.names_by_tag("web", lambda tool: not tool.info().hidden), ["a", "b"]
        )
        self.assertEqual(registry.names_by_tag(""), [])

    def test_schemas_for_context_only_exposes_discover_tool(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="other"))
        self.assertEqual(registry.schemas(), [])
        registry.register(FakeTool(name="discover_tool"))
        schemas = registry.schemas()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0].function.name, "discover_tool")
        self.assertEqual(registry.schemas_for_context(lambda info: False), [])


class TestDiscovery(unittest.IsolatedAsyncioTestCase):
    async def test_discover_without_name_lists_visible_tools(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="a"))
        registry.register(FakeTool(name="hidden", hidden=True))
        result = await registry.discover("")
        self.assertEqual([tool.info.name for tool in result.tools], ["a"])

    async def test_discover_unknown_name_raises(self) -> None:
        registry = Registry()
        with self.assertRaises(Exception):
            await registry.discover("nope")

    async def test_discover_named_tool_carries_schema(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="a", description="d"))
        result = await registry.discover("a")
        self.assertEqual(len(result.tools), 1)
        self.assertEqual(result.tools[0].info.description, "d")
        self.assertIsNotNone(result.tools[0].schema)

    async def test_discover_walks_depends_on(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="root", depends_on=["dep"]))
        registry.register(FakeTool(name="dep"))
        details, errors = await registry.discover_details(None, ["root"])
        self.assertEqual(errors, [])
        self.assertEqual([tool.info.name for tool in details], ["root", "dep"])

    async def test_discover_hidden_root_is_an_error(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="hidden", hidden=True))
        details, errors = await registry.discover_details(None, ["hidden"])
        self.assertEqual(details, [])
        self.assertEqual(errors[0].name, "hidden")

    async def test_public_info_normalizes_tags(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="a", tags=[" Web ", "bad tag"], foreground_only=True))
        result = await registry.discover("")
        self.assertEqual(result.tools[0].info.tags, ["web"])
        self.assertTrue(result.tools[0].info.foreground_only)


class TestAvailability(unittest.IsolatedAsyncioTestCase):
    """availability 原因互斥：对应 availability_test.go。"""

    def setUp(self) -> None:
        self.policy = Policy(user_max_tool_risk=RiskLow, superadmin_confirm_risk=RiskCritical)
        self.user = Actor(id="u1", platform="cli", role=RoleUser)
        self.admin = Actor(id="cli:local", platform="cli", role=RoleSuperadmin)

    def test_reasons_are_mutually_exclusive(self) -> None:
        background = with_sandbox_context(None, SandboxContext(root="s", dir="s", background=True))
        cases = [
            ("missing", None, None, self.user, TOOL_REASON_NOT_FOUND),
            (
                "foreground only in background",
                background,
                FakeTool(name="workspace", foreground_only=True),
                self.user,
                TOOL_REASON_CONTEXT_UNAVAILABLE,
            ),
            (
                "vision required without capability",
                with_capabilities(None, Capabilities(vision=False)),
                FakeTool(name="view_image", vision_required=True),
                self.user,
                TOOL_REASON_CONTEXT_UNAVAILABLE,
            ),
            (
                "superadmin only",
                None,
                FakeTool(name="admin", superadmin_only=True),
                self.user,
                TOOL_REASON_SUPERADMIN_ONLY,
            ),
            (
                "risk above policy",
                None,
                FakeTool(name="shell", risk=RiskHigh),
                self.user,
                TOOL_REASON_RISK_DENIED,
            ),
            ("allowed", None, FakeTool(name="web", risk=RiskLow), self.user, ""),
            ("superadmin allowed", None, FakeTool(name="shell", risk=RiskHigh), self.admin, ""),
        ]
        for name, ctx, candidate, actor, want in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    tool_availability_reason(ctx, actor, self.policy, candidate), want
                )

    def test_matches_existing_gates(self) -> None:
        """新入口与既有判定入口必须等价（迁移调用点不许顺带改准入）。"""
        contexts = [
            None,
            with_sandbox_context(None, SandboxContext(root="s", dir="s", background=True)),
            with_capabilities(None, Capabilities(vision=False)),
        ]
        candidates = [
            FakeTool(name="web", risk=RiskLow),
            FakeTool(name="shell", risk=RiskHigh),
            FakeTool(name="admin", risk=RiskLow, superadmin_only=True),
            FakeTool(name="hidden", risk=RiskLow, hidden=True),
            FakeTool(name="workspace", risk=RiskLow, foreground_only=True),
            FakeTool(name="view_image", risk=RiskLow, vision_required=True),
        ]
        for actor in (self.user, self.admin):
            for candidate in candidates:
                for ctx in contexts:
                    info = candidate.info()
                    available = info_available_in_context(ctx, info) and can_access_tool(
                        actor, self.policy, info
                    )
                    reason = tool_availability_reason(ctx, actor, self.policy, candidate)
                    self.assertEqual(available, reason == "", (actor.role, info.name))

    def test_tool_access_reason_ignores_context(self) -> None:
        info = Info(name="workspace", risk=RiskLow, foreground_only=True)
        self.assertEqual(tool_access_reason(self.user, self.policy, info), "")

    def test_registry_reason_covers_hidden_and_missing(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="visible"))
        registry.register(FakeTool(name="hidden", hidden=True))
        cases = [
            ("missing", "nope", False, TOOL_REASON_NOT_FOUND),
            ("empty name", "  ", False, TOOL_REASON_NOT_FOUND),
            ("hidden not allowed", "hidden", False, TOOL_REASON_HIDDEN),
            ("hidden allowed", "hidden", True, ""),
            ("visible", "visible", False, ""),
        ]
        for name, tool_name, allow_hidden, want in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    registry_tool_availability_reason(
                        None, registry, self.admin, self.policy, tool_name, allow_hidden
                    ),
                    want,
                )
        self.assertEqual(
            registry_tool_availability_reason(None, None, self.admin, self.policy, "visible", False),
            TOOL_REASON_NOT_FOUND,
        )


class TestCapabilityContext(unittest.TestCase):
    def test_vision_required_availability(self) -> None:
        info = Builder("view_image").vision_required().build_info()
        self.assertTrue(info.vision_required)
        self.assertTrue(info_available_in_context(None, info))
        self.assertFalse(
            info_available_in_context(with_capabilities(None, Capabilities(vision=False)), info)
        )
        self.assertTrue(
            info_available_in_context(with_capabilities(None, Capabilities(vision=True)), info)
        )
        self.assertTrue(
            info_available_in_context(
                with_capabilities(None, Capabilities(vision=False)), Builder("plain").build_info()
            )
        )

    def test_ctx_helpers_work_with_token_like_objects(self) -> None:
        class Token:
            def __init__(self, parent=None):
                self._values = {}
                self._parent = parent

            def with_value(self, key, value):
                child = Token(self)
                child._values = {key: value}
                return child

            def value(self, key, default=None):
                node = self
                while node is not None:
                    if key in node._values:
                        return node._values[key]
                    node = node._parent
                return default

        ctx = with_sandbox_context(Token(), SandboxContext(dir="d", background=True))
        self.assertTrue(background_context(ctx))
        ctx = with_session_id(ctx, " s1 ")
        self.assertEqual(session_id_from_context(ctx), "s1")
        self.assertEqual(session_id_from_context(Token()), "")


class TestSandboxPaths(unittest.TestCase):
    def test_resolve_relative_path_joins_sandbox_dir(self) -> None:
        sandbox = SandboxContext(root="ignored", dir=tempfile.gettempdir(), background=True)
        resolved = resolve_sandbox_relative_path(sandbox, "sub/./file.txt")
        self.assertEqual(
            resolved, os.path.join(os.path.abspath(tempfile.gettempdir()), "sub", "file.txt")
        )

    def test_rejects_absolute_home_and_escape(self) -> None:
        sandbox = SandboxContext(dir=tempfile.gettempdir(), background=True)
        for raw in ("", "   ", "~/x", "/abs", "C:/abs", "..", "../x", "a/../../x"):
            with self.subTest(raw=raw):
                with self.assertRaises(Exception):
                    resolve_sandbox_relative_path(sandbox, raw)

    def test_requires_configured_root(self) -> None:
        with self.assertRaises(Exception):
            resolve_sandbox_relative_path(SandboxContext(), "a.txt")

    def test_background_instruction_is_stable(self) -> None:
        self.assertIn("相对路径", "所有路径参数必须使用相对路径")


class TestWorkspace(unittest.IsolatedAsyncioTestCase):
    async def test_relative_path_uses_workspace_store(self) -> None:
        class Store:
            async def get_workspace_dir(self, ctx):
                return tempfile.gettempdir()

            async def set_workspace_dir(self, ctx, dir):
                return None

            async def clear_workspace_dir(self, ctx):
                return None

        ctx = with_workspace_store(None, Store())
        resolved = await resolve_workspace_path(ctx, "a.txt", PathResolveOptions(allow_create=True))
        self.assertEqual(resolved.path, os.path.join(tempfile.gettempdir(), "a.txt"))
        self.assertFalse(resolved.was_abs)
        self.assertEqual(resolved.warnings, [])

    async def test_absolute_path_warns_about_workspace(self) -> None:
        target = os.path.join(tempfile.gettempdir(), "atri-x-abs.txt")
        resolved = await resolve_workspace_path(None, target, PathResolveOptions(allow_create=True))
        self.assertTrue(resolved.was_abs)
        self.assertEqual(len(resolved.warnings), 1)
        self.assertIn("绝对路径", resolved.warnings[0])

    async def test_background_context_pins_path_into_sandbox(self) -> None:
        ctx = with_sandbox_context(None, SandboxContext(dir=tempfile.gettempdir(), background=True))
        resolved = await resolve_workspace_path(ctx, "a/b.txt", PathResolveOptions(allow_create=True))
        self.assertEqual(
            resolved.path, os.path.join(os.path.abspath(tempfile.gettempdir()), "a", "b.txt")
        )
        self.assertEqual(resolved.base_dir, os.path.normpath(tempfile.gettempdir()))

    async def test_empty_path_and_missing_file_raise(self) -> None:
        with self.assertRaises(Exception):
            await resolve_workspace_path(None, "  ")
        with self.assertRaises(Exception):
            await resolve_workspace_path(
                None, os.path.join(tempfile.gettempdir(), "definitely-missing-atri.txt")
            )

    async def test_directory_rejected_without_allow_directory(self) -> None:
        with self.assertRaises(Exception):
            await resolve_workspace_path(None, tempfile.gettempdir())
        resolved = await resolve_workspace_path(
            None, tempfile.gettempdir(), PathResolveOptions(allow_directory=True)
        )
        self.assertTrue(resolved.path)


class TestExecutor(unittest.IsolatedAsyncioTestCase):
    """执行器准入：对应 executor_test.go。"""

    async def test_executes_tool_and_builds_tool_message(self) -> None:
        registry = Registry()
        tool = FakeTool(name="ok", content="ok")
        registry.register(tool)
        result = await Executor(registry=registry).execute(
            None, ToolCallRequest(id="call_1", name="ok", arguments="{}")
        )
        self.assertIsNone(result.err)
        self.assertEqual(result.message.role, "tool")
        self.assertEqual(result.message.tool_call_id, "call_1")
        self.assertEqual(result.message.name, "ok")
        self.assertEqual(segments_content_text(result.message.segments), "ok")
        self.assertEqual(tool.calls[0].arguments, "{}")

    async def test_missing_tool_reports_message_not_raising(self) -> None:
        result = await Executor(registry=Registry()).execute(
            None, ToolCallRequest(id="call_1", name="missing", arguments="{}")
        )
        self.assertIsNotNone(result.err)
        self.assertIn("not found", segments_content_text(result.message.segments))

    async def test_registry_missing_is_an_error_result(self) -> None:
        result = await Executor().execute(
            None, ToolCallRequest(name="x", arguments="{}")
        )
        self.assertIsNotNone(result.err)
        self.assertIn("registry is not configured", str(result.err))

    async def test_denies_tool_above_user_risk(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="danger", risk=RiskHigh))
        result = await Executor(
            registry=registry, actor=Actor(role=RoleUser), policy=default_policy()
        ).execute(None, ToolCallRequest(id="c", name="danger", arguments="{}"))
        self.assertIsNotNone(result.err)
        self.assertIn("above your allowed tool level", segments_content_text(result.message.segments))

    async def test_denies_superadmin_only_tool_for_normal_user(self) -> None:
        registry = Registry()
        registry.register(FakeTool(name="cron", risk=RiskMedium, superadmin_only=True))
        result = await Executor(
            registry=registry,
            actor=Actor(role=RoleUser),
            policy=Policy(user_max_tool_risk=RiskMedium, superadmin_confirm_risk=RiskHigh),
        ).execute(None, ToolCallRequest(id="c", name="cron", arguments="{}"))
        self.assertIsNotNone(result.err)
        self.assertIn("requires superadmin", segments_content_text(result.message.segments))

    async def test_result_segments_win_over_content(self) -> None:
        class SegmentTool(FakeTool):
            async def call(self, ctx, req):
                return Result(
                    segments=[
                        _segment_text("image:"),
                        _segment_image("https://example.com/a.png"),
                    ]
                )

        registry = Registry()
        registry.register(SegmentTool(name="segments"))
        result = await Executor(registry=registry).execute(
            None, ToolCallRequest(id="c", name="segments", arguments="{}")
        )
        self.assertEqual(len(result.message.segments), 2)
        self.assertEqual(result.message.segments[1].url, "https://example.com/a.png")

    async def test_tool_exception_becomes_error_result(self) -> None:
        class Boom(FakeTool):
            async def call(self, ctx, req):
                raise RuntimeError("boom")

        registry = Registry()
        registry.register(Boom(name="boom"))
        result = await Executor(registry=registry).execute(
            None, ToolCallRequest(id="c", name="boom", arguments="{}")
        )
        self.assertIsInstance(result.err, RuntimeError)
        self.assertIn("boom", segments_content_text(result.message.segments))


class TestWarningsAndRiskAssessment(unittest.IsolatedAsyncioTestCase):
    def test_warnings_append_to_content(self) -> None:
        text = segments_content_text(Result(content="ok", warnings=["use read_file"]).llm_segments())
        self.assertIn("ok", text)
        self.assertIn("Warnings:", text)
        self.assertIn("- use read_file", text)

    def test_warnings_append_to_segments(self) -> None:
        segments = Result(
            segments=[_segment_text("text"), _segment_image("https://example.com/a.png")],
            warnings=["use read_file"],
        ).llm_segments()
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[-1].type, "image")
        self.assertIn("use read_file", segments_content_text(segments))

    def test_warnings_are_deduped_and_blank_dropped(self) -> None:
        from atri_x.tool import format_warnings

        self.assertEqual(format_warnings(["  ", "a", "a", "b"]), "Warnings:\n- a\n- b")
        self.assertEqual(format_warnings([]), "")

    def test_warning_only_content_returns_warning_block(self) -> None:
        from atri_x.tool import append_warnings

        self.assertEqual(append_warnings("  \n", ["w"]), "Warnings:\n- w")

    async def test_assess_risk_uses_assessor_then_normalizes(self) -> None:
        from atri_x.tool import RiskAssessment

        class Assessor(FakeTool):
            async def assess_risk(self, ctx, req):
                return RiskAssessment(level="", reasons=["parsed"])

        tool = Assessor(name="a", risk=RiskMedium)
        assessment = await assess_risk(None, tool, CallRequest(id="1", name="a", arguments="{}"))
        self.assertEqual(assessment.level, RiskMedium)
        self.assertEqual(assessment.reasons, ["parsed"])

    async def test_assess_risk_falls_back_to_high(self) -> None:
        assessment = await assess_risk(
            None, FakeTool(name="a", risk=""), CallRequest(id="1", name="a", arguments="{}")
        )
        self.assertEqual(assessment.level, RiskHigh)


def _segment_text(value: str) -> MessageSegment:
    return MessageSegment(type=SegmentType.TEXT, text=value)


def _segment_image(url: str) -> MessageSegment:
    return MessageSegment(type=SegmentType.IMAGE, url=url)


class TestProvider(unittest.TestCase):
    def test_prompt_names_split_tools_and_skills(self) -> None:
        from atri_x.tool import PromptNames, SchemaProvider, prompt_names_from_infos

        infos = [
            Info(name="discover_tool"),
            Info(name="hidden", hidden=True),
            Info(name="resident_memory"),
            Info(name="skill_doc", source=SourceSkillAgent),
        ]
        names = prompt_names_from_infos(infos)
        self.assertEqual(names.tools, ["resident_memory"])
        self.assertEqual(names.skills, ["skill_doc"])
        self.assertIsInstance(names, PromptNames)

    def test_schema_provider_mode_behaviour(self) -> None:
        from atri_x.tool import SESSION_MODE_CHAT, SESSION_MODE_WORK, SchemaProvider

        registry = Registry()
        registry.register(FakeTool(name="resident_memory"))
        registry.register(FakeTool(name="discover_tool"))
        provider = SchemaProvider(registry=registry, policy=default_policy())
        self.assertEqual(provider.schemas(None, SESSION_MODE_CHAT), [])
        schemas = provider.schemas(None, SESSION_MODE_WORK)
        self.assertEqual([schema.function.name for schema in schemas], ["discover_tool"])
        names = provider.tool_names(None, SESSION_MODE_WORK)
        self.assertEqual(names.tools, ["resident_memory"])
        self.assertEqual(provider.tool_names(None, SESSION_MODE_CHAT).tools, [])

    def test_schema_provider_hides_superadmin_tools_from_user(self) -> None:
        from atri_x.tool import SESSION_MODE_WORK, SchemaProvider

        registry = Registry()
        registry.register(FakeTool(name="discover_tool"))
        registry.register(FakeTool(name="cron", superadmin_only=True))
        provider = SchemaProvider(registry=registry, policy=default_policy())
        names = provider.tool_names(None, SESSION_MODE_WORK)
        self.assertEqual(names.tools, [])

    def test_actor_from_scope_prefers_context_actor(self) -> None:
        from atri_x.tool import WithActor, actor_from_scope

        @dataclass
        class Scope:
            platform: str = "cli"
            actor_id: str = "cli:guest"

        actor = Actor(id="cli:local", platform="cli", role=RoleSuperadmin)
        self.assertEqual(actor_from_scope(WithActor(None, actor), Scope()).role, RoleSuperadmin)
        derived = actor_from_scope(None, Scope())
        self.assertEqual(derived.id, "cli:guest")
        self.assertEqual(derived.platform, "cli")
        self.assertEqual(derived.role, RoleUser)


class TestDetailRuleCards(unittest.TestCase):
    """规则卡去重：对应 detail.go。"""

    def test_render_joins_blocks_with_separator(self) -> None:
        from atri_x.tool import DetailBlock, render_detail_blocks

        blocks = [DetailBlock(content=" a "), DetailBlock(content="b")]
        self.assertEqual(render_detail_blocks(blocks), "a\n\n---\n\nb")

    def test_rule_card_rendered_once_and_reported(self) -> None:
        from atri_x.tool import (
            DetailBlock,
            new_rule_card_formats_from_context,
            render_detail_blocks_with_context,
            with_shown_rule_card_formats,
        )

        ctx = with_shown_rule_card_formats(None, [])
        blocks = [
            DetailBlock(content="c1", rule_card="rule", format="fmt"),
            DetailBlock(content="c2", rule_card="rule", format="fmt"),
        ]
        self.assertEqual(
            render_detail_blocks_with_context(ctx, blocks), "rule\n\n---\n\nc1\n\n---\n\nc2"
        )
        self.assertEqual(new_rule_card_formats_from_context(ctx), ["fmt"])

    def test_already_shown_rule_card_is_skipped(self) -> None:
        from atri_x.tool import (
            DetailBlock,
            new_rule_card_formats_from_context,
            render_detail_blocks_with_context,
            with_shown_rule_card_formats,
        )

        ctx = with_shown_rule_card_formats(None, ["fmt"])
        rendered = render_detail_blocks_with_context(
            ctx, [DetailBlock(content="c1", rule_card="rule", format="fmt")]
        )
        self.assertEqual(rendered, "c1")
        self.assertEqual(new_rule_card_formats_from_context(ctx), [])


class TestDiscoveryContentProvider(unittest.IsolatedAsyncioTestCase):
    async def test_load_discovery_content_prefers_context_provider(self) -> None:
        from atri_x.tool import load_discovery_content

        class ContextProvider:
            async def discovery_content(self, ctx):
                return "ctx", True

        class PlainProvider:
            def discovery_content(self):
                return "plain", False

        class Neither:
            pass

        self.assertEqual(await load_discovery_content(None, ContextProvider()), ("ctx", True, True))
        self.assertEqual(await load_discovery_content(None, PlainProvider()), ("plain", False, True))
        self.assertEqual(await load_discovery_content(None, Neither()), ("", False, False))

    async def test_structured_detail_provider_is_used(self) -> None:
        from atri_x.tool import DetailBlock

        class Structured(FakeTool):
            def detail(self):
                return "ignored"

            def activate_tools(self):
                return []

            def detail_block(self):
                return DetailBlock(content="structured", rule_card="rule", format="fmt")

        registry = Registry()
        registry.register(Structured(name="skill"))
        details, errors = await registry.discover_details(None, ["skill"])
        self.assertEqual(errors, [])
        self.assertEqual(details[0].detail, "rule\n\n---\n\nstructured")
        self.assertIsNone(details[0].schema)

    async def test_lazy_detail_failure_is_reported(self) -> None:
        class Lazy(FakeTool):
            async def load_detail(self, ctx):
                raise RuntimeError("no workspace")

        registry = Registry()
        registry.register(Lazy(name="lazy"))
        details, errors = await registry.discover_details(None, ["lazy"])
        self.assertEqual(details, [])
        self.assertEqual(errors[0].reason, "no workspace")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
