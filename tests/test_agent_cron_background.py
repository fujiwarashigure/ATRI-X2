"""后台工具预载路径（``atri_x.agent.cron``）的测试。

这条路径此前**完全没有测试覆盖**：一位同僚在此处一次修掉三处 ``NameError``
（``ToolAvailabilityReason(...)`` / ``InfoAvailableInContext(...)`` 用了不存在的
camelCase 名字、``BackgroundKind(str)`` 把常量容器当类调用）。三处都在
``run_background`` → ``preload_background_resources`` → ``preload_background_tool``
这条链上，而链上任何一处抛异常都不会被上层吞掉：后台任务会直接失败。

对应 Go：``internal/agent/cron.go`` 的 ``preloadBackgroundResources`` /
``preloadBackgroundTool`` / ``rememberPreloadedDiscovery`` 与
``internal/agent/tool_directive.go`` 的同名 helper。本文件覆盖：

* 可用 / 不可用两条路，以及每个不可用原因的**文案**与 ``result``
  （``rejected`` vs ``skipped`` —— 混用会让拒绝率统计失真）；
* ``info_available_in_context`` 的两个判据（后台上下文里的 ``foreground_only``、
  无视觉能力时的 ``vision_required``）；
* Skill 分支：详情经**协程**加载（``LazyDetailProvider``）、activate 出来的包装工具注入、
  以及 ``empty_skill_detail`` / ``tool_hidden`` 的拒绝路径；
* ``/cron`` 的 kind 分支（含自定义 kind）与沙盒上下文；
* 端到端：真实 ``run_background`` + 假 store/session + 真实 ``Registry``，
  断言预载出的工具清单、审计字段、会话 metadata 与提示词。
"""

from __future__ import annotations

import asyncio
import json
import unittest

from atri_x.agent import cron as cron_module
from atri_x.background import KindAtrinis, KindCron, RunRequest
from atri_x.llm import ToolFunctionSchema, ToolSchema
from atri_x.security import Actor, Policy, RoleSuperadmin, RoleUser, actor_from_context
from atri_x.session import Scope, promote_to_foreground, was_promoted
from atri_x.storage import Message, Session
from atri_x.tool import (
    Capabilities,
    Info,
    RiskCritical,
    RiskLow,
    ToolReasonContextUnavailable,
    ToolReasonHidden,
    ToolReasonNotFound,
    ToolReasonRiskDenied,
    ToolReasonSuperadminOnly,
    new_registry,
    with_capabilities,
    with_sandbox_context,
)
from atri_x.tool.detail import DetailBlock
from atri_x.tool.sandbox import BackgroundKind, SandboxContext
from tests.agent_fakes import FakeAgent


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 假工具 / 假 Skill
# ---------------------------------------------------------------------------
class ControlledTool:
    """可控 ``Info`` 的工具（``Registry`` 只要求 name/info/schema/call）。"""

    def __init__(self, info: Info) -> None:
        self._info = info

    def name(self) -> str:
        return self._info.name

    def info(self) -> Info:
        return self._info

    def schema(self) -> ToolSchema:
        return ToolSchema(function=ToolFunctionSchema(name=self._info.name))

    async def call(self, ctx: object, req: object) -> object:  # pragma: no cover - 不执行
        raise AssertionError("background preload must not execute the tool")


class LazySkill:
    """懒加载 Skill：同步入口给不出详情，只有 ``await load_detail(ctx)`` 才有。

    与真实实现一致（``atri_x/tool/skill/descriptor.py`` 的 ``Descriptor`` 就是
    ``detail()`` + ``detail_block()`` + **async** ``load_detail()`` + ``activate_tools()``）。
    """

    def __init__(self, name: str, detail: str = "", wrappers: list[str] | None = None, *,
                 hidden: bool = False) -> None:
        self._name = name
        self._detail = detail
        self._wrappers = list(wrappers or [])
        self._hidden = hidden
        self.loaded = 0

    def name(self) -> str:
        return self._name

    def info(self) -> Info:
        return Info(
            name=self._name,
            description="skill " + self._name,
            source="skill_agent",
            risk=RiskLow,
            hidden=self._hidden,
        )

    def schema(self) -> ToolSchema:
        # Skill 本体没有可调用的 function schema（与 Go 的 Descriptor.Schema 一致）。
        return ToolSchema()

    # -- DetailProvider + LazyDetailProvider ---------------------------------
    def detail(self) -> str:
        """同步入口：真实 Skill 的同步详情为空，详情要经 ``load_detail``。"""
        return ""

    def detail_block(self) -> DetailBlock:
        return DetailBlock()

    async def load_detail(self, ctx: object) -> DetailBlock:
        del ctx
        self.loaded += 1
        return DetailBlock(content=self._detail)

    def activate_tools(self) -> list[str]:
        return list(self._wrappers)


def background_ctx(kind: str = KindCron, *, vision: bool | None = None):
    ctx = with_sandbox_context(
        {},
        SandboxContext(
            root="data/sandbox",
            dir="data/sandbox/%s" % kind,
            background=True,
            background_kind=kind,
        ),
    )
    if vision is not None:
        ctx = with_capabilities(ctx, Capabilities(vision=vision))
    return ctx


def audit_fields(agent: FakeAgent, event: str) -> list[dict]:
    """把 ``agent.audit_calls`` 里某个事件的 key/value 拉平成字典列表。"""
    out = []
    for call in agent.audit_calls:
        if not call or call[0] != event:
            continue
        out.append(dict(zip(call[1::2], call[2::2])))
    return out


def skipped_reasons(agent: FakeAgent, name: str) -> list[tuple[str, str]]:
    return [
        (item.get("reason"), item.get("result"))
        for item in audit_fields(agent, "background_preload_skipped")
        if item.get("name") == name
    ]


def make_registry() -> tuple[object, dict[str, ControlledTool]]:
    registry = new_registry()
    tools = {
        "ok_tool": Info(name="ok_tool", description="fine", risk=RiskLow),
        "owner_scoped": Info(
            name="owner_scoped", description="scoped", risk=RiskCritical, owner_scoped=True
        ),
        "hidden_tool": Info(name="hidden_tool", description="hidden", hidden=True),
        "fg_only": Info(name="fg_only", description="foreground", foreground_only=True),
        "vision_tool": Info(name="vision_tool", description="vision", vision_required=True),
        "risky": Info(name="risky", description="risky", risk=RiskCritical),
        "admin_tool": Info(name="admin_tool", description="admin", superadmin_only=True),
        "helper": Info(name="helper", description="wrapper", hidden=True),
    }
    objects = {name: ControlledTool(info) for name, info in tools.items()}
    for tool in objects.values():
        registry.register(tool)
    return registry, objects


def make_agent(registry, *, policy: Policy | None = None, actor_role: str = RoleUser) -> FakeAgent:
    agent = FakeAgent(registry=registry)
    agent.security_policy = policy if policy is not None else Policy(user_max_tool_risk=RiskLow)
    agent.actor_role = actor_role
    return agent


# ---------------------------------------------------------------------------
# 可用 / 不可用矩阵
# ---------------------------------------------------------------------------
class PreloadAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry, self.tools = make_registry()
        self.agent = make_agent(self.registry)
        self.ctx = background_ctx()
        self.session = Session(id="bg-1", mode="work")

    def _preload(self, names: list[str]):
        return run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, self.session, names
            )
        )

    def test_available_tools_are_preloaded(self) -> None:
        result = self._preload(["ok_tool", "owner_scoped"])
        self.assertEqual(["ok_tool", "owner_scoped"], result.tools)
        self.assertEqual([], result.skills)
        self.assertEqual("", result.skill_prompt)
        self.assertEqual([], audit_fields(self.agent, "background_preload_skipped"))
        # 预载 = 进内存缓存，后台只拿到这一步注入的工具
        self.assertIn("ok_tool", self.agent.discovered_tools["bg-1"])

    def test_missing_tool_reason_is_tool_not_found(self) -> None:
        result = self._preload(["nope"])
        self.assertEqual([], result.tools)
        self.assertEqual([(ToolReasonNotFound, "skipped")], skipped_reasons(self.agent, "nope"))

    def test_foreground_only_in_background_is_context_unavailable(self) -> None:
        result = self._preload(["fg_only"])
        self.assertEqual([], result.tools)
        self.assertEqual(
            [(ToolReasonContextUnavailable, "skipped")],
            skipped_reasons(self.agent, "fg_only"),
        )

    def test_hidden_tool_is_skipped_when_not_a_skill_wrapper(self) -> None:
        result = self._preload(["hidden_tool"])
        self.assertEqual([], result.tools)
        self.assertEqual(
            [(ToolReasonHidden, "skipped")], skipped_reasons(self.agent, "hidden_tool")
        )

    def test_risk_above_policy_is_rejected(self) -> None:
        result = self._preload(["risky"])
        self.assertEqual([], result.tools)
        # 明确裁决用 rejected：与"没轮到执行"的 skipped 必须区分开
        self.assertEqual(
            [(ToolReasonRiskDenied, "rejected")], skipped_reasons(self.agent, "risky")
        )

    def test_superadmin_only_is_rejected_for_normal_actor(self) -> None:
        result = self._preload(["admin_tool"])
        self.assertEqual([], result.tools)
        self.assertEqual(
            [(ToolReasonSuperadminOnly, "rejected")],
            skipped_reasons(self.agent, "admin_tool"),
        )

    def test_superadmin_actor_gets_superadmin_tool(self) -> None:
        agent = make_agent(self.registry, actor_role=RoleSuperadmin)
        result = run(
            cron_module.preload_background_resources(agent, self.ctx, self.session, ["admin_tool"])
        )
        self.assertEqual(["admin_tool"], result.tools)

    def test_owner_scoped_critical_passes_for_normal_actor(self) -> None:
        # owner_scoped 由工具自己按 actor 限定范围，策略对普通用户一律放行
        result = self._preload(["owner_scoped"])
        self.assertEqual(["owner_scoped"], result.tools)

    def test_chat_mode_session_preloads_nothing(self) -> None:
        chat = Session(id="bg-chat", mode="chat")
        result = run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, chat, ["ok_tool"]
            )
        )
        self.assertEqual([], result.tools)
        self.assertEqual([], self.agent.audit_calls)

    def test_registry_missing_preloads_nothing(self) -> None:
        agent = FakeAgent(registry=None)
        result = run(
            cron_module.preload_background_resources(agent, self.ctx, self.session, ["ok_tool"])
        )
        self.assertEqual([], result.tools)
        self.assertEqual([], agent.audit_calls)

    def test_blank_names_and_discover_tool_are_ignored(self) -> None:
        # discover_tool 在名字归一化阶段就被丢掉（后台任务不能自己发现工具）
        self.assertEqual(
            ["ok_tool"],
            cron_module.background_tool_list_names(["", "  ", "discover_tool", "ok_tool"]),
        )
        result = self._preload(cron_module.background_tool_list_names(["", "   ", "discover_tool"]))
        self.assertEqual([], result.tools)
        self.assertEqual([], audit_fields(self.agent, "background_preload_skipped"))

        # 直接把 discover_tool 交给 preloadBackgroundTool：静默返回，连审计都不产生
        self.assertEqual(
            [],
            run(
                cron_module.preload_background_tool(
                    self.agent,
                    self.ctx,
                    self.session,
                    "discover_tool",
                    self.agent.actor(self.ctx),
                    self.agent.security_policy,
                    {},
                    False,
                )
            ),
        )
        self.assertEqual([], audit_fields(self.agent, "background_preload_skipped"))

    def test_info_available_in_context_decides_the_context_reason(self) -> None:
        from atri_x.tool import info_available_in_context

        foreground = Info(name="t", foreground_only=True)
        self.assertFalse(info_available_in_context(self.ctx, foreground))
        self.assertTrue(info_available_in_context({}, foreground))

        vision = Info(name="v", vision_required=True)
        # 没有能力声明的上下文保持历史行为（后台任务与直接调用不受影响）
        self.assertTrue(info_available_in_context(self.ctx, vision))
        self.assertFalse(
            info_available_in_context(background_ctx(vision=False), vision)
        )
        self.assertTrue(info_available_in_context(background_ctx(vision=True), vision))

    def test_vision_tool_skipped_without_vision_capability(self) -> None:
        ctx = background_ctx(vision=False)
        result = run(
            cron_module.preload_background_resources(
                self.agent, ctx, self.session, ["vision_tool"]
            )
        )
        self.assertEqual([], result.tools)
        self.assertEqual(
            [(ToolReasonContextUnavailable, "skipped")],
            skipped_reasons(self.agent, "vision_tool"),
        )

    def test_default_policy_used_when_agent_has_none(self) -> None:
        agent = FakeAgent(registry=self.registry)
        agent.security_policy = None
        result = run(
            cron_module.preload_background_resources(agent, self.ctx, self.session, ["ok_tool"])
        )
        self.assertEqual(["ok_tool"], result.tools)


# ---------------------------------------------------------------------------
# Skill 分支
# ---------------------------------------------------------------------------
class PreloadSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry, self.tools = make_registry()
        self.agent = make_agent(self.registry)
        self.ctx = background_ctx()
        self.session = Session(id="bg-skill", mode="work")

    def test_lazy_skill_detail_is_loaded_and_prompt_is_built(self) -> None:
        skill = LazySkill("doc_skill", detail="SKILL-BODY", wrappers=["helper"])
        self.registry.register(skill)
        result = run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, self.session, ["doc_skill"]
            )
        )
        self.assertEqual(1, skill.loaded, "懒加载 Skill 的详情必须真的被 await 取到")
        self.assertEqual(["doc_skill"], result.skills)
        self.assertEqual("## Skill: doc_skill\n\nSKILL-BODY", result.skill_prompt)
        # activate 出来的隐藏包装工具走 allow_hidden=True 分支
        self.assertEqual(["helper"], result.tools)

        prompt = cron_module.background_prompt_with_skills("干活", result.skill_prompt)
        self.assertIn("SKILL-BODY", prompt)
        self.assertTrue(prompt.endswith("[后台任务]\n\n干活"))

    def test_skill_without_detail_is_skipped_as_empty(self) -> None:
        self.registry.register(LazySkill("empty_skill", detail=""))
        result = run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, self.session, ["empty_skill"]
            )
        )
        self.assertEqual([], result.skills)
        self.assertEqual("", result.skill_prompt)
        self.assertEqual(
            [("empty_skill_detail", "skipped")],
            skipped_reasons(self.agent, "empty_skill"),
        )

    def test_hidden_skill_is_skipped_before_loading_detail(self) -> None:
        skill = LazySkill("hidden_skill", detail="X", hidden=True)
        self.registry.register(skill)
        result = run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, self.session, ["hidden_skill"]
            )
        )
        self.assertEqual([], result.skills)
        self.assertEqual(0, skill.loaded, "隐藏 Skill 不该触发详情加载")
        self.assertEqual(
            [(ToolReasonHidden, "skipped")], skipped_reasons(self.agent, "hidden_skill")
        )

    def test_skill_wrapper_is_not_treated_as_tool_schema(self) -> None:
        # 直接把 Skill 本体交给 preloadBackgroundTool（activate 列表写错名字时会发生）
        self.registry.register(LazySkill("doc_skill", detail="X"))
        out = run(
            cron_module.preload_background_tool(
                self.agent,
                self.ctx,
                self.session,
                "doc_skill",
                self.agent.actor(self.ctx),
                self.agent.security_policy,
                {},
                True,
            )
        )
        self.assertEqual([], out)
        self.assertEqual(
            [("skill_has_no_schema", "skipped")], skipped_reasons(self.agent, "doc_skill")
        )

    def test_duplicate_skill_names_load_detail_once(self) -> None:
        skill = LazySkill("doc_skill", detail="BODY")
        self.registry.register(skill)
        result = run(
            cron_module.preload_background_resources(
                self.agent, self.ctx, self.session, ["doc_skill", "doc_skill"]
            )
        )
        self.assertEqual(["doc_skill"], result.skills)
        self.assertEqual(1, len(result.skill_prompt.split("## Skill: ")) - 1)


# ---------------------------------------------------------------------------
# remember_preloaded_discovery：新注入 vs 已存在
# ---------------------------------------------------------------------------
class RememberPreloadedDiscoveryTests(unittest.TestCase):
    def _discovery(self, *names: str, allow_empty_schema: bool = False):
        from atri_x.tool import DiscoveredTool, DiscoveryResult
        from atri_x.tool.detail import public_info

        tools = []
        for name in names:
            tool = ControlledTool(Info(name=name, description=name))
            tools.append(
                DiscoveredTool(
                    info=public_info(tool.info()),
                    schema=None if allow_empty_schema else tool.schema(),
                )
            )
        return DiscoveryResult(tools=tools)

    def test_first_call_injects_second_call_reports_existing(self) -> None:
        agent = make_agent(*[new_registry()][0:1]) if False else FakeAgent()
        session = Session(id="bg-recall", mode="work")
        discovery = self._discovery("a", "b")

        injected, existing = run(
            cron_module.remember_preloaded_discovery(agent, background_ctx(), session, discovery, {})
        )
        self.assertEqual(["a", "b"], injected)
        self.assertEqual([], existing)

        # 第二次（模拟同一 Session 再来一次后台任务）：不算"这次预载出来的"
        injected2, existing2 = run(
            cron_module.remember_preloaded_discovery(agent, background_ctx(), session, discovery, {})
        )
        self.assertEqual([], injected2)
        self.assertEqual(["a", "b"], existing2)

    def test_schema_less_entries_are_ignored(self) -> None:
        agent = FakeAgent()
        session = Session(id="bg-noschema", mode="work")
        discovery = self._discovery("a", allow_empty_schema=True)
        injected, existing = run(
            cron_module.remember_preloaded_discovery(agent, background_ctx(), session, discovery, {})
        )
        self.assertEqual([], injected, "没有 schema 的发现结果不能算注入")
        self.assertEqual([], existing)

    def test_seen_map_prevents_duplicates(self) -> None:
        agent = FakeAgent()
        session = Session(id="bg-seen", mode="work")
        seen: dict[str, bool] = {}
        discovery = self._discovery("a")
        run(cron_module.remember_preloaded_discovery(agent, background_ctx(), session, discovery, seen))
        injected, _ = run(
            cron_module.remember_preloaded_discovery(agent, background_ctx(), session, discovery, seen)
        )
        self.assertEqual([], injected)
        self.assertTrue(seen["a"])


# ---------------------------------------------------------------------------
# kind 分支
# ---------------------------------------------------------------------------
class BackgroundKindTests(unittest.TestCase):
    def test_known_kinds_map_to_tool_background_kind(self) -> None:
        self.assertEqual(BackgroundKind.CRON, cron_module.tool_background_kind(KindCron))
        self.assertEqual(BackgroundKind.ATRINIS, cron_module.tool_background_kind(KindAtrinis))

    def test_custom_kind_is_passed_through_trimmed(self) -> None:
        # Go 是 ``tool.BackgroundKind(strings.TrimSpace(string(kind)))``；
        # Python 的 BackgroundKind 只是常量容器，不能当类调用（曾是 NameError）。
        self.assertEqual("custom", cron_module.tool_background_kind("  custom "))
        self.assertEqual("", cron_module.tool_background_kind(""))
        self.assertEqual("", cron_module.tool_background_kind(None))

    def _run_with_kind(self, kind: str, sandbox_subdir: str = ""):
        from atri_x.tool.sandbox import sandbox_context_from_context

        agent = FakeAgent(registry=make_registry()[0])
        seen: dict[str, object] = {}

        async def _start_background_chat(ctx, session, prompt):
            sandbox, ok = sandbox_context_from_context(ctx)
            seen["sandbox"] = sandbox
            seen["sandbox_ok"] = ok
            seen["prompt"] = prompt
            seen["actor"] = actor_from_context(ctx)[0]
            agent.store.messages().append(
                Message(session_id=session.id, role="assistant", content="done")
            )

        agent.start_background_chat = _start_background_chat
        req = RunRequest(
            kind=kind, name="job", prompt="p", sandbox_subdir=sandbox_subdir, tool_list_names=[]
        )
        run(cron_module.run_background(agent, None, req))
        return agent, seen

    def test_cron_kind_reaches_start_chat_with_cron_sandbox(self) -> None:
        _agent, seen = self._run_with_kind(KindCron)
        sandbox = seen["sandbox"]
        self.assertTrue(seen["sandbox_ok"])
        self.assertTrue(sandbox.background)
        self.assertEqual(BackgroundKind.CRON, sandbox.background_kind)
        self.assertEqual("cron", sandbox.dir.replace("\\", "/").split("/")[-1])

    def test_custom_kind_uses_kind_as_subdir_and_background_kind(self) -> None:
        _agent, seen = self._run_with_kind("nightly-custom")
        sandbox = seen["sandbox"]
        self.assertTrue(sandbox.background)
        self.assertEqual("nightly-custom", sandbox.background_kind)
        self.assertEqual("nightly-custom", sandbox.dir.replace("\\", "/").split("/")[-1])

    def test_atrinis_kind(self) -> None:
        _agent, seen = self._run_with_kind(KindAtrinis)
        self.assertEqual(BackgroundKind.ATRINIS, seen["sandbox"].background_kind)

    def test_explicit_sandbox_subdir_wins(self) -> None:
        _agent, seen = self._run_with_kind("nightly-custom", sandbox_subdir="my/dir")
        self.assertEqual(
            "my/dir", "/".join(seen["sandbox"].dir.replace("\\", "/").split("/")[-2:])
        )


# ---------------------------------------------------------------------------
# 端到端：run_background
# ---------------------------------------------------------------------------
class RunBackgroundPreloadEndToEndTests(unittest.TestCase):
    def _agent(self):
        registry, tools = make_registry()
        registry.register(LazySkill("doc_skill", detail="BODY", wrappers=["helper"]))
        agent = make_agent(registry)
        captured: dict[str, object] = {}

        async def _start_background_chat(ctx, session, prompt):
            captured["prompt"] = prompt
            captured["session_id"] = session.id
            captured["actor"] = actor_from_context(ctx)[0]
            agent.store.messages().append(
                Message(
                    session_id=session.id,
                    role="assistant",
                    content="report",
                    metadata=json.dumps({"raw_text": "raw report"}),
                )
            )

        agent.start_background_chat = _start_background_chat
        return agent, tools, captured

    def test_full_path_lists_tools_and_audits(self) -> None:
        agent, _tools, captured = self._agent()
        req = RunRequest(
            kind=KindCron,
            name="daily",
            title="Daily",
            prompt="do it",
            tool_list_names=["ok_tool", "missing", "admin_tool", "discover_tool"],
            metadata={"cron_job_name": "daily"},
        )
        result = run(cron_module.run_background(agent, None, req))

        # 1) 预载出来的工具清单
        self.assertEqual(["ok_tool"], [t for t in agent.discovered_tools[result.session_id]])
        self.assertEqual(["do it"], [captured["prompt"]])
        self.assertEqual("raw report", result.text)
        self.assertEqual(result.session_id, captured["session_id"])

        # 2) 审计字段
        preloaded = audit_fields(agent, "background_tool_preloaded")
        self.assertEqual(1, len(preloaded))
        self.assertEqual(["ok_tool"], preloaded[0]["tools"])
        self.assertEqual(KindCron, preloaded[0]["kind"])
        self.assertEqual("daily", preloaded[0]["name"])
        self.assertEqual("succeeded", preloaded[0]["result"])

        skipped = audit_fields(agent, "background_preload_skipped")
        self.assertEqual(
            sorted(
                [
                    ("missing", ToolReasonNotFound, "skipped"),
                    ("admin_tool", ToolReasonSuperadminOnly, "rejected"),
                ]
            ),
            sorted((item["name"], item["reason"], item["result"]) for item in skipped),
        )
        # discover_tool 连审计都不该产生（在名字归一化阶段就被丢掉）
        self.assertNotIn("discover_tool", [item["name"] for item in skipped])

        # 3) 会话与 metadata
        session = agent.store.sessions().get(result.session_id)
        self.assertEqual("Daily", session.title)
        metadata = json.loads(session.metadata)
        self.assertEqual("cron", metadata["background_kind"])
        self.assertEqual("daily", metadata["cron_job_name"])

    def test_skill_preload_prefixes_prompt_and_audits_skill(self) -> None:
        agent, _tools, captured = self._agent()
        req = RunRequest(
            kind=KindCron, name="daily", prompt="do it", tool_list_names=["doc_skill"]
        )
        result = run(cron_module.run_background(agent, None, req))

        prompt = captured["prompt"]
        self.assertIn("[系统预加载 Skill]", prompt)
        self.assertIn("BODY", prompt)
        self.assertTrue(prompt.endswith("[后台任务]\n\ndo it"))

        skills = audit_fields(agent, "background_skill_preloaded")
        self.assertEqual(1, len(skills))
        self.assertEqual(["doc_skill"], skills[0]["skills"])
        self.assertEqual("succeeded", skills[0]["result"])

        preloaded = audit_fields(agent, "background_tool_preloaded")
        self.assertEqual([["helper"]], [item["tools"] for item in preloaded])
        # metadata 里的 raw_text 优先于 content（与 Go 一致）
        self.assertEqual("raw report", result.text)

    def test_chat_mode_session_skips_preload(self) -> None:
        agent, _tools, _captured = self._agent()
        req = RunRequest(
            kind=KindCron,
            name="daily",
            prompt="p",
            session_mode="chat",
            tool_list_names=["ok_tool"],
        )
        run(cron_module.run_background(agent, None, req))
        self.assertEqual([], audit_fields(agent, "background_tool_preloaded"))

    def test_external_cached_tools_are_filtered_and_audited(self) -> None:
        from atri_x.toolrun import CachedTool

        agent, _tools, _captured = self._agent()
        req = RunRequest(
            kind=KindCron,
            name="daily",
            prompt="p",
            cached_tools=[
                CachedTool(name="shell"),
                CachedTool(name="discover_tool"),
                CachedTool(name="workspace"),
            ],
        )
        result = run(cron_module.run_background(agent, None, req))
        events = audit_fields(agent, "background_external_tools_preloaded")
        self.assertEqual(1, len(events))
        # 外部声明不能把后台白名单撑大：只留 shell
        self.assertEqual(["shell"], events[0]["tools"])
        self.assertEqual("succeeded", events[0]["result"])
        self.assertEqual(result.session_id, agent.store.sessions().rows[result.session_id].id)

    def test_empty_role_is_promoted_without_touching_caller_actor(self) -> None:
        # Go 的 ``actor := req.Actor`` 是值拷贝：``Actor{}.Role == ""`` 时提升成超管，
        # 但提升只作用于本次后台运行。
        agent, _tools, captured = self._agent()
        actor = Actor(id="u1", platform="qq", platform_user_id="10001", role="")
        req = RunRequest(kind=KindCron, name="daily", prompt="p", actor=actor)
        run(cron_module.run_background(agent, None, req))
        self.assertEqual(RoleSuperadmin, captured["actor"].role)
        self.assertEqual("", actor.role, "不能把调用方手里的 Actor 改成超管")

    def test_default_actor_keeps_its_role(self) -> None:
        # Python 的 ``Actor()`` 默认是 role="user"（Go 零值是 ""），所以默认 Actor
        # 不会被提升 —— 与 Go 的差异记在 Actor 的默认值上，不在本模块。
        agent, _tools, captured = self._agent()
        req = RunRequest(kind=KindCron, name="daily", prompt="p")
        run(cron_module.run_background(agent, None, req))
        self.assertEqual(RoleUser, captured["actor"].role)
        self.assertEqual(RoleUser, req.actor.role)

    def test_explicit_role_is_kept(self) -> None:
        agent, _tools, captured = self._agent()
        actor = Actor(id="u1", role=RoleUser)
        run(
            cron_module.run_background(
                agent, None, RunRequest(kind=KindCron, name="daily", prompt="p", actor=actor)
            )
        )
        self.assertEqual(RoleUser, captured["actor"].role)

    def test_missing_assistant_message_is_an_error(self) -> None:
        agent, _tools, _captured = self._agent()

        async def _start_background_chat(ctx, session, prompt):
            del ctx, prompt
            agent.store.messages().append(
                Message(session_id=session.id, role="user", content="?")
            )

        agent.start_background_chat = _start_background_chat
        with self.assertRaises(ValueError):
            run(
                cron_module.run_background(
                    agent, None, RunRequest(kind=KindCron, name="daily", prompt="p")
                )
            )

    def test_taken_over_session_returns_taken_over_result(self) -> None:
        agent, _tools, _captured = self._agent()
        promoted = Session(
            id="bg-promoted",
            platform_scope_id="cron:daily",
            metadata=json.dumps({"background_kind": "cron"}),
        )
        promote_to_foreground(promoted, Scope(actor_id="u1", platform="qq", platform_scope_id="private:u1"))
        self.assertTrue(was_promoted(promoted), "真接管标记必须被识别")
        agent.store.sessions().create(promoted)
        result = run(
            cron_module.run_background(
                agent, None, RunRequest(kind=KindCron, name="daily", session_id="bg-promoted")
            )
        )
        self.assertTrue(result.taken_over)
        self.assertEqual("bg-promoted", result.session_id)

    def test_reused_background_session_keeps_untouched_metadata(self) -> None:
        agent, _tools, _captured = self._agent()
        existing = Session(id="bg-reuse", metadata=json.dumps({"keep": 1}), mode="chat")
        agent.store.sessions().create(existing)
        result = run(
            cron_module.run_background(
                agent,
                None,
                RunRequest(
                    kind=KindCron,
                    name="daily",
                    session_id="bg-reuse",
                    session_mode="work",
                    tool_list_names=["ok_tool"],
                ),
            )
        )
        self.assertEqual("bg-reuse", result.session_id)
        metadata = json.loads(agent.store.sessions().get("bg-reuse").metadata)
        self.assertEqual(1, metadata["keep"])
        self.assertEqual("cron", metadata["background_kind"])
        self.assertEqual(["ok_tool"], [t for t in agent.discovered_tools["bg-reuse"]])


if __name__ == "__main__":
    unittest.main()
