"""``atri_x.agent.tools`` / ``tool_cache`` / ``tool_transcript`` / 提示词提供者的测试。"""

from __future__ import annotations

import asyncio
import json
import unittest

from atri_x.agent import tool_cache as tc
from atri_x.agent import tool_transcript as tt
from atri_x.agent import toolrun_prompt_provider as provider
from atri_x.agent.tools import (
    ToolRuntimeMixin,
    compact_arguments,
    first_non_empty,
    is_background_session,
    is_group_scope_id,
    min_int,
    normalize_string_list,
    preview_arguments,
    preview_runes,
    risk_reasons_text,
    rune_len,
    service_host,
    short_hash,
    sorted_unique,
    tool_runtime_state,
    truncate_runes,
)
from atri_x.config import GroupPolicyConfig
from atri_x.llm import LLMMessage, ToolCallRequest, text_segments
from atri_x.storage import Message, Session
from atri_x.toolrun import CachedTool
from tests.agent_fakes import FakeAgent, FakeRegistry, FakeTool


class HelperTests(unittest.TestCase):
    def test_first_non_empty(self) -> None:
        self.assertEqual(first_non_empty("", "  ", "x"), "x")
        self.assertEqual(first_non_empty(None, 3), "3")
        self.assertEqual(first_non_empty(), "")

    def test_short_hash_matches_fnv1a(self) -> None:
        self.assertEqual(short_hash(""), "811c9dc5")
        self.assertEqual(short_hash("hello"), "4f9f2cab")

    def test_sorted_unique(self) -> None:
        self.assertEqual(sorted_unique(["b", "a", "b", ""]), ["a", "b"])
        self.assertEqual(sorted_unique(None), [])

    def test_normalize_string_list_keeps_order(self) -> None:
        self.assertEqual(normalize_string_list([" b ", "a", "b"]), ["b", "a"])

    def test_min_and_rune_helpers(self) -> None:
        self.assertEqual(min_int(3, 5), 3)
        self.assertEqual(rune_len("abc"), 3)
        self.assertEqual(truncate_runes("abcdef", 3), "abc")
        self.assertEqual(truncate_runes("abc", 0), "abc")
        self.assertEqual(preview_runes("abcdef", 3), "abc...")
        self.assertEqual(preview_runes("   ", 3), "")

    def test_arguments_helpers(self) -> None:
        self.assertEqual(compact_arguments(" { 'a' : 1 } ".replace("'", '"')), '{"a":1}')
        self.assertEqual(compact_arguments(""), "{}")
        self.assertEqual(compact_arguments("[1,2]"), "[1,2]")
        self.assertTrue(preview_arguments('{"a":"%s"}' % ("x" * 300)).endswith("..."))

    def test_risk_reasons_text(self) -> None:
        self.assertEqual(risk_reasons_text([]), "")
        self.assertEqual(risk_reasons_text(["a", " b "]), "\n风险原因：\n- a\n- b")

    def test_is_group_scope_id(self) -> None:
        self.assertTrue(is_group_scope_id("group:1"))
        self.assertFalse(is_group_scope_id("private:1"))

    def test_is_background_session(self) -> None:
        self.assertFalse(is_background_session(None))
        self.assertFalse(is_background_session(Session(id="s", metadata="")))
        self.assertTrue(
            is_background_session(Session(id="s", metadata='{"background_kind":"cron"}'))
        )
        self.assertFalse(
            is_background_session(Session(id="s", metadata='{"background_kind":""}'))
        )
        self.assertFalse(is_background_session(Session(id="s", metadata="{bad")))

    def test_service_host(self) -> None:
        from atri_x.agent.tools import AgentService

        agent = FakeAgent()
        service = AgentService(agent)
        self.assertIs(service_host(service), agent)
        self.assertIs(service_host(agent), agent)
        # 未知属性转发给宿主。
        self.assertIs(service.store, agent.store)


class ToolRuntimeStateTests(unittest.TestCase):
    def test_prefers_host_tool_runtime(self) -> None:
        agent = FakeAgent()
        state = tool_runtime_state(agent)
        self.assertIs(state.registry, agent.tool_runtime.registry)
        registry = FakeRegistry({"shell": FakeTool("shell")})
        state.registry = registry
        self.assertIs(agent.tool_runtime.registry, registry)

    def test_skills_fall_back_to_owner_slot(self) -> None:
        agent = FakeAgent()
        state = tool_runtime_state(agent)

        class Skills:
            async def reload(self, ctx: object) -> None:
                return None

            async def remove(self, ctx: object, name: str) -> None:
                return None

        state.skills = Skills()
        self.assertIs(agent.tool_runtime.skills, state.skills)

    def test_set_tool_runtime_builds_manager_and_provider(self) -> None:
        agent = FakeAgent()
        registry = FakeRegistry({"shell": FakeTool("shell")})
        ToolRuntimeMixin.set_tool_runtime(agent, registry, None)
        state = tool_runtime_state(agent)
        self.assertIs(state.registry, registry)
        self.assertIsNotNone(state.manager)
        self.assertTrue(state.default_provider)
        self.assertIsInstance(state.provider, provider.ToolRunPromptProvider)
        self.assertEqual([info.name for info in ToolRuntimeMixin.list(agent)], ["shell"])

    def test_set_tool_provider_and_config_and_tags(self) -> None:
        agent = FakeAgent()

        class Custom:
            async def schemas(self, ctx: object, mode: str, session: object, scope: object) -> list:
                return []

        ToolRuntimeMixin.set_tool_provider(agent, Custom())
        state = tool_runtime_state(agent)
        self.assertIsInstance(state.provider, Custom)
        self.assertFalse(state.default_provider)
        ToolRuntimeMixin.set_tool_provider(agent, None)
        self.assertFalse(tool_runtime_state(agent).default_provider)

        from atri_x.config import ToolsConfig

        ToolRuntimeMixin.set_tool_config(agent, ToolsConfig(max_rounds_per_turn=0))
        self.assertEqual(tool_runtime_state(agent).config.max_rounds_per_turn, 2)
        self.assertIs(agent.tools_config.max_rounds_per_turn, 2)
        self.assertIs(agent.tool_runtime.config, agent.tools_config)

        ToolRuntimeMixin.set_tool_tag_config(agent, "tags.toml", None)
        self.assertIsNotNone(tool_runtime_state(agent).tool_tags)

    def test_unregister(self) -> None:
        agent = FakeAgent(registry=FakeRegistry({"shell": FakeTool("shell")}))
        ToolRuntimeMixin.unregister(agent, "shell")
        self.assertEqual(ToolRuntimeMixin.list(agent), [])

    def test_remove_and_reload_without_skills(self) -> None:
        agent = FakeAgent(registry=FakeRegistry({"shell": FakeTool("shell")}))
        asyncio.run(ToolRuntimeMixin.remove(agent, None, "shell"))
        asyncio.run(ToolRuntimeMixin.reload(agent, None))
        self.assertEqual(ToolRuntimeMixin.list(agent), [])


class ToolCacheTests(unittest.TestCase):
    def test_state_uses_host_dicts(self) -> None:
        agent = FakeAgent()
        state = tc.tool_cache_state(agent)
        self.assertIs(state.discovered_tools, agent.discovered_tools)
        self.assertIs(state.lock, agent.auto_confirm_mu)

    def test_remember_and_discovered_schemas(self) -> None:
        agent = FakeAgent(registry=FakeRegistry({"shell": FakeTool("shell")}))
        session = Session(id="s1", metadata="")
        cached = CachedTool(name="shell", description="d")
        asyncio.run(tc.remember_cached_tools(agent, None, session, [cached]))
        self.assertIn("shell", agent.discovered_tools["s1"])
        schemas = tc.discovered_tool_schemas(agent, session)
        self.assertEqual(len(schemas), 1)

    def test_persist_merges_metadata(self) -> None:
        agent = FakeAgent()
        stored = Session(id="s1", metadata='{"keep":1}')
        agent.store.sessions().create(stored)
        session = agent.store.sessions().get("s1")
        asyncio.run(
            tc.persist_cached_tools(
                agent, None, session, [CachedTool(name="shell", description="d")]
            )
        )
        metadata = json.loads(session.metadata)
        self.assertEqual(metadata["keep"], 1)
        self.assertEqual(metadata["discovered_tools"], ["shell"])
        self.assertEqual(metadata["tool_cache"][0]["name"], "shell")

    def test_restore_from_metadata(self) -> None:
        agent = FakeAgent(registry=FakeRegistry({"shell": FakeTool("shell")}))
        session = Session(
            id="s9",
            metadata=json.dumps({"discovered_tools": ["shell"], "tool_cache": [{"name": "x", "source": "native"}]}),
        )
        tc.restore_discovered_tools_from_metadata(agent, session)
        bucket = agent.discovered_tools["s9"]
        self.assertIn("shell", bucket)
        self.assertIn("x", bucket)

    def test_cached_tool_name_set_and_tags(self) -> None:
        agent = FakeAgent()
        session = Session(
            id="s2",
            metadata=json.dumps({"tool_cache": [{"name": "a", "source": "native"}]}),
        )
        agent.store.sessions().create(session)
        names = asyncio.run(tc.cached_tool_name_set(agent, None, session))
        self.assertEqual(sorted(names), ["a"])
        asyncio.run(tc.persist_tool_tags(agent, None, session, ["t1", "t1"]))
        asyncio.run(tc.persist_shown_rule_card_formats(agent, None, session, ["card"]))
        metadata = json.loads(session.metadata)
        self.assertEqual(metadata["tool_tags"], ["t1"])
        self.assertEqual(metadata["shown_rule_card_formats"], ["card"])

    def test_remember_discovered_tools_from_result(self) -> None:
        from atri_x.tool.tool import DiscoveredTool, DiscoveryResult, PublicInfo

        agent = FakeAgent()
        session = Session(id="s3")
        agent.store.sessions().create(session)
        result = DiscoveryResult(
            tools=[
                DiscoveredTool(
                    info=PublicInfo(name="shell", description="d", source="builtin"),
                    schema=FakeTool("shell").schema(),
                )
            ]
        )
        asyncio.run(tc.remember_discovered_tools(agent, None, session, result))
        self.assertEqual(sorted(agent.discovered_tools["s3"]), ["shell"])

    def test_no_session_is_noop(self) -> None:
        agent = FakeAgent()
        asyncio.run(tc.remember_cached_tools(agent, None, None, [CachedTool(name="x")]))
        asyncio.run(tc.persist_cached_tools(agent, None, None, [CachedTool(name="x")]))
        asyncio.run(tc.persist_tool_tags(agent, None, None, ["t"]))
        self.assertEqual(agent.discovered_tools, {})


class ToolTranscriptTests(unittest.TestCase):
    def test_tool_call_message_roundtrip(self) -> None:
        calls = [ToolCallRequest(id="c1", name="shell", arguments='{"a":1}')]
        message = tt.tool_call_storage_message("s1", "跑一下", "原始", calls)
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.content, "跑一下")
        metadata = json.loads(message.metadata)
        self.assertEqual(metadata["tool_calls"][0]["name"], "shell")
        self.assertEqual(metadata["raw_text"], "原始")
        parsed = tt.AssistantMetadata.from_json(message.metadata)
        self.assertEqual(parsed.tool_calls[0].id, "c1")
        self.assertEqual(parsed.raw_text, "原始")

    def test_tool_call_message_without_raw_text(self) -> None:
        message = tt.tool_call_storage_message("s1", "same", "same", [])
        self.assertEqual(json.loads(message.metadata), {})

    def test_tool_call_message_persists_reasoning_content(self) -> None:
        """思考模式的思维链要能落库并读回，供后续请求原样回传。"""
        calls = [ToolCallRequest(id="c1", name="shell", arguments="{}")]
        message = tt.tool_call_storage_message(
            "s1", "", "", calls, reasoning_content="先想想再调工具"
        )
        metadata = json.loads(message.metadata)
        self.assertEqual(metadata["reasoning_content"], "先想想再调工具")
        parsed = tt.AssistantMetadata.from_json(message.metadata)
        self.assertEqual(parsed.reasoning_content, "先想想再调工具")
        self.assertEqual(parsed.tool_calls[0].id, "c1")
        # 没有思维链时不写字段：避免把厂商扩展发给不认识的模型。
        plain = tt.tool_call_storage_message("s1", "", "", calls)
        self.assertNotIn("reasoning_content", json.loads(plain.metadata))
        self.assertEqual(tt.AssistantMetadata.from_json(plain.metadata).reasoning_content, "")

    def test_stored_segments_clears_url_for_media(self) -> None:
        from atri_x.llm import MessageSegment

        segments = [
            MessageSegment(type="text", text="hi"),
            MessageSegment(type="image", media="m1", url="http://x/y.png"),
        ]
        raw = tt.stored_message_segments(segments)
        parsed = json.loads(raw)
        self.assertEqual(parsed[1]["media"], "m1")
        self.assertNotIn("url", parsed[1])
        self.assertEqual(tt.stored_message_segments(text_segments("only")), "")
        self.assertEqual(tt.stored_message_segments([]), "")

    def test_tool_result_message(self) -> None:
        message = LLMMessage(role="tool", name="shell", tool_call_id="c1")
        message.segments = text_segments("ok")
        stored = tt.tool_result_storage_message("s1", message)
        self.assertEqual(stored.session_id, "s1")
        self.assertEqual(stored.tool_call_id, "c1")
        self.assertEqual(json.loads(stored.metadata), {"name": "shell"})

    def test_tool_name_metadata(self) -> None:
        self.assertEqual(tt.tool_name_metadata(""), "")
        self.assertEqual(tt.tool_name_metadata("shell"), '{"name":"shell"}')

    def test_assistant_raw_text_metadata(self) -> None:
        self.assertEqual(tt.assistant_raw_text_metadata("a", "a"), "")
        self.assertEqual(tt.assistant_raw_text_metadata("a", ""), "")
        self.assertIn("raw_text", tt.assistant_raw_text_metadata("a", "b"))
        # 只有思维链时也要落 metadata（下一轮请求要回传 reasoning_content）。
        only_reasoning = json.loads(tt.assistant_raw_text_metadata("a", "a", "想了想"))
        self.assertEqual(only_reasoning, {"reasoning_content": "想了想"})
        both = json.loads(tt.assistant_raw_text_metadata("a", "b", "想了想"))
        self.assertEqual(both["raw_text"], "b")
        self.assertEqual(both["reasoning_content"], "想了想")

    def test_persisted_tool_message_summarizes_discovery(self) -> None:
        message = LLMMessage(role="tool", name="discover_tool")
        message.segments = text_segments(
            json.dumps({"tools": [{"info": {"name": "shell"}}, {"info": {"name": "web"}}]})
        )
        out = tt.persisted_tool_message(message)
        self.assertEqual(out.segments[0].text, "discover_tool found tools: shell, web")
        plain = LLMMessage(role="tool", name="discover_tool")
        plain.segments = text_segments("{bad")
        self.assertEqual(tt.persisted_tool_message(plain).segments[0].text, "{bad")
        other = LLMMessage(role="tool", name="shell")
        other.segments = text_segments("x")
        self.assertIs(tt.persisted_tool_message(other), other)

    def test_metadata_tool_names_and_join(self) -> None:
        self.assertEqual(tt.metadata_tool_names(["a", "b"]), ["a", "b"])
        self.assertEqual(tt.metadata_tool_names("a"), ["a"])
        self.assertEqual(tt.metadata_tool_names(1), [])
        self.assertEqual(tt.join_names([]), "none")
        self.assertEqual(tt.join_names(["a", "b"]), "a, b")

    def test_persist_turn_message_respects_history_gate(self) -> None:
        agent = FakeAgent()
        agent.history_enabled = lambda ctx: False  # noqa: ARG005
        message = Message(session_id="s1", role="assistant", content="hi")
        asyncio.run(tt.persist_turn_message(agent, None, message, "turn"))
        self.assertEqual(agent.store.messages().messages, [])

        agent.history_enabled = lambda ctx: True  # noqa: ARG005
        asyncio.run(tt.persist_turn_messages(agent, None, "s1", "turn", [message]))
        self.assertEqual(len(agent.store.messages().messages), 1)
        self.assertEqual(agent.store.messages().messages[0].session_id, "s1")

    def test_remember_discovery_result_parses_data(self) -> None:
        agent = FakeAgent(registry=FakeRegistry({"shell": FakeTool("shell")}))
        session = Session(id="s4")
        agent.store.sessions().create(session)
        result = type(
            "Result",
            (),
            {
                "data": json.dumps(
                    {"tools": [{"info": {"name": "shell", "description": "d"}}]}
                ),
                "metadata": {"activate_tools": ["shell"], "shown_rule_card_formats": ["card"]},
            },
        )()
        asyncio.run(tt.remember_discovery_result(agent, None, session, result))
        self.assertEqual(sorted(agent.discovered_tools["s4"]), ["shell"])
        self.assertIn("card", json.loads(session.metadata).get("shown_rule_card_formats", []))


class PromptProviderTests(unittest.TestCase):
    def test_schemas_and_tool_names(self) -> None:
        registry = FakeRegistry({"shell": FakeTool("shell"), "hidden": FakeTool("hidden", hidden=True)})
        agent = FakeAgent(registry=registry)
        provider_instance = provider.ToolRunPromptProvider(agent)
        session = Session(id="s1", mode="work")
        schemas = asyncio.run(
            provider_instance.schemas(None, "work", session, agent.scope(None))
        )
        self.assertEqual([item.function.name for item in schemas], ["shell", "hidden"])
        names = asyncio.run(
            provider_instance.tool_names(None, "work", session, agent.scope(None))
        )
        self.assertEqual(names.tools, ["shell"])
        self.assertEqual(names.skills, [])

    def test_no_session_returns_empty(self) -> None:
        instance = provider.ToolRunPromptProvider(FakeAgent())
        self.assertEqual(asyncio.run(instance.schemas(None, "work", None, None)), [])
        names = asyncio.run(instance.tool_names(None, "work", None, None))
        self.assertEqual(names.tools, [])

    def test_disabled_for_background_session(self) -> None:
        registry = FakeRegistry({"shell": FakeTool("shell")})
        agent = FakeAgent(registry=registry)
        session = Session(id="s1", mode="work", metadata='{"background_kind":"cron"}')
        instance = provider.ToolRunPromptProvider(agent)
        self.assertEqual(
            asyncio.run(instance.schemas(None, "work", session, agent.scope(None))), []
        )

    def test_new_provider_helper(self) -> None:
        self.assertIsInstance(
            provider.new_tool_run_prompt_provider(FakeAgent()), provider.ToolRunPromptProvider
        )


class ToolAuthLinkTests(unittest.TestCase):
    def test_group_policy_allowlist_is_shared(self) -> None:
        from atri_x.agent import tool_auth

        agent = FakeAgent()
        agent.group_policy["qq:group:42"] = GroupPolicyConfig(
            tool_allowlist=["shell"], tool_allowlist_set=True
        ).normalize()
        self.assertEqual(tool_auth.group_tool_allowlist(agent, None), {"shell": True})


if __name__ == "__main__":
    unittest.main()
