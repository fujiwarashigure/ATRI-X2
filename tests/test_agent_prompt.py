"""提示词组（prompt / system_prompt / system_prompt_sources / tool_tag_config）的测试。

对应 Go：``internal/agent/prompt_test.go`` / ``system_prompt_test.go`` /
``tool_tag_config_test.go`` 的核心路径。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.agent.prompt import (
    AssistantMetadata,
    FileSoulProvider,
    NoopToolSchemaProvider,
    PromptBuildRequest,
    PromptBuilder,
    assistant_message_metadata,
    message_segments,
    message_segments_from_storage,
    new_prompt_builder,
    storage_message_to_llm,
    summary_user_prefix,
    tool_name_from_metadata,
    tool_names_text,
)
from atri_x.agent.system_prompt import (
    ConversationMeta,
    SystemPromptManager,
    SystemPromptPart,
    SystemPromptRequest,
    new_system_prompt_manager,
)
from atri_x.agent.system_prompt_sources import (
    ConversationMetaSystemPromptSource,
    ResidentMemorySystemPromptSource,
    SoulSystemPromptSource,
    ToolNamesSystemPromptSource,
    resident_memory_prompt_header,
    wrap_resident_memory_prompt,
)
from atri_x.agent.tool_tag_config import (
    ToolTagConfigSource,
    normalize_tool_tag,
    normalize_tool_tags_config,
    sorted_unique,
    tool_tag_source_of,
)
from atri_x.config import ToolTagConfig, ToolTagsConfig
from atri_x.errors import NotFoundError
from atri_x.llm.llm import LLM, Role, RoleAssistant, RoleSystem, RoleUser, ToolCallRequest, ToolSchema
from atri_x.storage.models import ContextSummary, Message, Session
from atri_x.tool import PromptNames, SandboxContext, with_sandbox_context

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_agent_prompt_test")


class _Tools:
    """假的工具名提供者。"""

    def __init__(self, names: PromptNames) -> None:
        self.names = names
        self.calls = 0

    async def tool_names(self, ctx, mode, session, scope) -> PromptNames:
        self.calls += 1
        return self.names

    async def schemas(self, ctx, mode, session, scope) -> list[ToolSchema]:
        return [ToolSchema()]


class _ResidentStore:
    def __init__(self, text: str = "", missing: bool = False) -> None:
        self.text_value = text
        self.missing = missing

    def read(self, scope) -> object:
        if self.missing:
            raise NotFoundError("no memory")
        store = self

        class _Memory:
            def text(self) -> str:
                return store.text_value

        return _Memory()


class _Soul:
    def __init__(self, text: str = "soul", fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    async def system_prompt(self, ctx, mode: str) -> str:
        if self.fail:
            raise RuntimeError("boom")
        return self.text


class _SyncSoul:
    def system_prompt(self, ctx, mode: str) -> str:
        return "sync-soul"


class TestPromptHelpers(unittest.TestCase):
    def test_tool_names_text_exact(self) -> None:
        self.assertEqual(tool_names_text(PromptNames()), "")
        text = tool_names_text(PromptNames(tools=["a", "b"], skills=["s"]))
        self.assertTrue(text.startswith("tools: a,b. skills: s."))
        self.assertIn("discover_tool(names)", text)

    def test_summary_user_prefix(self) -> None:
        self.assertEqual(summary_user_prefix(" s "), "s\n\n当前用户输入：\n")

    def test_assistant_metadata_parsing(self) -> None:
        metadata = assistant_message_metadata(
            '{"tool_calls":[{"id":"1","name":"n","arguments":"{}"}],"raw_text":"raw"}'
        )
        self.assertEqual(metadata.raw_text, "raw")
        self.assertEqual(metadata.tool_calls[0].name, "n")
        # 返回类型可能来自 agent-tools 的 tool_transcript（唯一出处），因此按鸭子类型断言
        for raw in ("not json", ""):
            empty = assistant_message_metadata(raw)
            self.assertEqual(list(empty.tool_calls), [])
            self.assertEqual(empty.raw_text, "")

    def test_tool_name_from_metadata(self) -> None:
        self.assertEqual(tool_name_from_metadata('{"name":"t"}'), "t")
        self.assertEqual(tool_name_from_metadata("bad"), "")
        self.assertEqual(tool_name_from_metadata(""), "")

    def test_message_segments_prefers_segments_column(self) -> None:
        message = Message(content="text", segments='[{"type":"image","url":"http://x/y.png"}]')
        segments = message_segments("text", message)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].type, "image")
        plain = message_segments("text", Message(content="text"))
        self.assertEqual(plain[0].text, "text")
        self.assertEqual(message_segments_from_storage("[bad json"), [])

    def test_storage_message_to_llm(self) -> None:
        tool_message = Message(role="tool", content="c", tool_call_id="tc", metadata='{"name":"t"}')
        out = storage_message_to_llm(Role.TOOL, [], tool_message)
        self.assertEqual(out.name, "t")
        self.assertEqual(out.tool_call_id, "tc")

        assistant = Message(
            role="assistant",
            metadata='{"tool_calls":[{"id":"1","name":"n","arguments":"{}"}]}',
        )
        out = storage_message_to_llm(Role.ASSISTANT, [], assistant)
        self.assertEqual(len(out.tool_calls), 1)
        self.assertEqual(out.tool_calls[0].id, "1")


class TestPromptBuilder(unittest.IsolatedAsyncioTestCase):
    async def test_requires_system_prompt(self) -> None:
        builder = PromptBuilder(SystemPromptManager([]))
        with self.assertRaises(ValueError):
            await builder.build(None, PromptBuildRequest())

    async def test_builds_system_then_history(self) -> None:
        template = _Tools(PromptNames(tools=["t"]))
        manager = new_system_prompt_manager(ToolNamesSystemPromptSource(template))
        builder = PromptBuilder(manager)
        session = Session(id="s1", mode="work")
        request = PromptBuildRequest(
            session=session,
            messages=[
                Message(role="system", content="old-system"),
                Message(role="user", content="u1"),
                Message(role="assistant", content="a1", metadata='{"raw_text":"raw"}'),
                Message(role="weird", content="skip"),
            ],
        )
        out = await builder.build(None, request)
        self.assertEqual(out[0].role, RoleSystem)
        self.assertIn("tools: t.", out[0].segments[0].text)
        self.assertEqual([m.role for m in out[1:]], [RoleSystem, RoleUser, RoleAssistant])

    async def test_summary_injected_only_into_first_user(self) -> None:
        builder = PromptBuilder(SystemPromptManager([SoulSystemPromptSource(_Soul())]))
        summary = ContextSummary(summary="S")
        request = PromptBuildRequest(
            session=Session(id="s1", mode="work"),
            messages=[
                Message(role="user", content="first"),
                Message(role="user", content="second"),
            ],
            summary=summary,
        )
        out = await builder.build(None, request)
        self.assertTrue(out[1].segments[0].text.startswith(summary_user_prefix("S")))
        self.assertEqual(out[2].segments[0].text, "second")

    async def test_summary_keeps_image_segments(self) -> None:
        builder = PromptBuilder(SystemPromptManager([SoulSystemPromptSource(_Soul())]))
        request = PromptBuildRequest(
            session=Session(id="s1", mode="work"),
            messages=[
                Message(
                    role="user",
                    content="hi",
                    segments='[{"type":"image","url":"http://x/1.png"}]',
                )
            ],
            summary=ContextSummary(summary="S"),
        )
        out = await builder.build(None, request)
        kinds = [segment.type for segment in out[1].segments]
        self.assertIn("image", kinds)
        self.assertEqual(out[1].segments[0].type, "text")


class TestSoulProvider(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    async def test_file_soul_provider_caches_by_state(self) -> None:
        path = os.path.join(TMP_DIR, "SOUL.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("v1")
        provider = FileSoulProvider(path)
        self.assertEqual(await provider.system_prompt(None, "work"), "v1")
        stamp = os.stat(path)
        # 同长度改写 + 把 mtime 还原：状态不变 -> 命中缓存（与 Go 的 (size,mtime) 判定一致）
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("v2")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(await provider.system_prompt(None, "work"), "v1")
        # 长度变化 -> 状态变化 -> 重读
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("v3-longer")
        self.assertEqual(await provider.system_prompt(None, "work"), "v3-longer")

    async def test_missing_soul_file_raises(self) -> None:
        provider = FileSoulProvider(os.path.join(TMP_DIR, "missing.md"))
        with self.assertRaises(OSError):
            await provider.system_prompt(None, "work")

    async def test_noop_tool_provider(self) -> None:
        provider = NoopToolSchemaProvider()
        self.assertEqual(await provider.schemas(None, "work", None, None), [])
        names = await provider.tool_names(None, "work", None, None)
        self.assertEqual(names.tools, [])


class TestSystemPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_manager_trims_and_joins(self) -> None:
        class _Source:
            def __init__(self, parts) -> None:
                self.parts_value = parts

            async def parts(self, ctx, req):
                return self.parts_value

        manager = SystemPromptManager(
            [
                _Source([SystemPromptPart("a", "  one  "), SystemPromptPart("b", "   ")]),
                _Source([SystemPromptPart("c", "two")]),
            ]
        )
        self.assertEqual(await manager.build(None, SystemPromptRequest()), "one\n\ntwo")
        manager.add_source(None)
        self.assertEqual(len(manager.sources), 2)

    async def test_conversation_meta_source(self) -> None:
        source = ConversationMetaSystemPromptSource()
        request = SystemPromptRequest(
            meta=ConversationMeta(
                platform="qq",
                kind="group",
                id="g1",
                user_id="u1",
                display_name="Ali  ce",
            ),
            session=Session(
                id="s1", created_at=datetime(2026, 5, 20, 3, 4, 5, tzinfo=timezone.utc)
            ),
        )
        parts = await source.parts(None, request)
        self.assertEqual(
            parts[0].content,
            'meta: platform=qq, conversation=group(id:g1), '
            'display_name="Ali ce"(id:u1), session_created_at=2026-05-20T03:04:05.',
        )
        self.assertEqual(await source.parts(None, SystemPromptRequest()), [])

    async def test_conversation_meta_private_has_no_id(self) -> None:
        source = ConversationMetaSystemPromptSource()
        parts = await source.parts(
            None, SystemPromptRequest(meta=ConversationMeta(kind="private", id="x"))
        )
        self.assertEqual(parts[0].content, "meta: conversation=private.")

    async def test_soul_source_accepts_sync_and_async(self) -> None:
        self.assertEqual(
            (await SoulSystemPromptSource(_Soul("x")).parts(None, SystemPromptRequest()))[0].content,
            "x",
        )
        self.assertEqual(
            (
                await SoulSystemPromptSource(_SyncSoul()).parts(
                    None, SystemPromptRequest(mode="chat")
                )
            )[0].content,
            "sync-soul",
        )
        self.assertEqual(await SoulSystemPromptSource(None).parts(None, SystemPromptRequest()), [])

    async def test_resident_memory_source(self) -> None:
        source = ResidentMemorySystemPromptSource(_ResidentStore("<b>hi</b>"))
        parts = await source.parts(None, SystemPromptRequest())
        self.assertIn(resident_memory_prompt_header, parts[0].content)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", parts[0].content)
        self.assertEqual(
            await ResidentMemorySystemPromptSource(_ResidentStore(missing=True)).parts(
                None, SystemPromptRequest()
            ),
            [],
        )
        self.assertEqual(
            await ResidentMemorySystemPromptSource(None).parts(None, SystemPromptRequest()),
            [],
        )
        self.assertEqual(wrap_resident_memory_prompt(" x ").count("<resident_memory>"), 1)

    async def test_tool_names_source_skips_background(self) -> None:
        tools = _Tools(PromptNames(tools=["t"]))
        source = ToolNamesSystemPromptSource(tools)
        session = Session(id="s1")
        parts = await source.parts(None, SystemPromptRequest(session=session))
        self.assertTrue(parts[0].content.startswith("tools: t."))
        ctx = with_sandbox_context({}, SandboxContext(background=True))
        self.assertEqual(await source.parts(ctx, SystemPromptRequest(session=session)), [])
        self.assertEqual(
            await ToolNamesSystemPromptSource(None).parts(None, SystemPromptRequest(session=session)),
            [],
        )


class TestToolTagConfig(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_normalize(self) -> None:
        self.assertEqual(normalize_tool_tag(" Media "), "media")
        self.assertEqual(normalize_tool_tag("bad tag"), "")
        self.assertEqual(normalize_tool_tag("a.b-c_1"), "a.b-c_1")
        cfg = ToolTagsConfig(
            tags={
                " Media ": ToolTagConfig(tools=["b", "a", "a"], prompt=" p "),
                "EMPTY": ToolTagConfig(),
                "bad tag": ToolTagConfig(tools=["x"]),
            }
        )
        normalized = normalize_tool_tags_config(cfg)
        self.assertEqual(list(normalized.tags), ["media"])
        self.assertEqual(normalized.tags["media"].tools, ["a", "b"])
        self.assertEqual(normalized.tags["media"].prompt, "p")
        self.assertEqual(sorted_unique(["b", "", "a", "b"]), ["a", "b"])

    async def test_source_without_path_uses_initial(self) -> None:
        source = ToolTagConfigSource(
            "", ToolTagsConfig(tags={"media": ToolTagConfig(tools=["t"], prompt="P")})
        )
        self.assertEqual(await source.configured_tags(None), ["media"])
        self.assertEqual(await source.configured_tool_names_by_tag(None, "media"), ["t"])
        self.assertEqual(await source.configured_tags_for_tool(None, "t"), ["media"])
        self.assertEqual(await source.configured_tags_for_tool(None, ""), [])

        session = Session(id="s1", metadata='{"tool_tags":["media","unknown"]}')
        parts = await source.parts(None, SystemPromptRequest(session=session))
        self.assertEqual([p.name for p in parts], ["tool_tag_prompt:media"])
        self.assertEqual(parts[0].content, "P")
        self.assertEqual(await source.parts(None, SystemPromptRequest()), [])

    async def test_source_hot_reloads_file(self) -> None:
        path = os.path.join(TMP_DIR, "tool_tags.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[tags.media]\ntools = ["t"]\nprompt = "P1"\n')
        source = ToolTagConfigSource(path)
        self.assertEqual(await source.configured_tags(None), ["media"])
        stamp = os.stat(path)
        # 同长度改写 + 还原 mtime：状态不变则命中缓存（与 Go 的 (size,mtime) 判定一致）
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[tags.media]\ntools = ["t"]\nprompt = "P2"\n')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        cached = await source.load(None)
        self.assertEqual(cached.tags["media"].prompt, "P1")
        # 改变长度后重读
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[tags.media]\ntools = ["t"]\nprompt = "P2-long"\n')
        reloaded = await source.load(None)
        self.assertEqual(reloaded.tags["media"].prompt, "P2-long")

    async def test_source_missing_file_is_empty(self) -> None:
        source = ToolTagConfigSource(os.path.join(TMP_DIR, "nope.toml"))
        self.assertEqual((await source.load(None)).tags, {})
        self.assertEqual(await source.configured_tool_names_by_tag(None, "x"), [])

    def test_tool_tag_source_of(self) -> None:
        cfg = tool_tag_source_of({"media": {"tools": ["t"], "prompt": "p"}})
        self.assertEqual(cfg.tags["media"].tools, ["t"])
        self.assertEqual(tool_tag_source_of(None).tags, {})
        original = ToolTagsConfig(tags={"a": ToolTagConfig()})
        self.assertIs(tool_tag_source_of(original), original)


class TestNewPromptBuilder(unittest.IsolatedAsyncioTestCase):
    async def test_builds_manager_from_options(self) -> None:
        class _Opts:
            soul_provider = _SyncSoul()
            soul_path = ""
            resident_memory_store = _ResidentStore("mem")
            tool_provider = _Tools(PromptNames(tools=["t"]))
            tool_tags_path = ""
            tool_tags = {"media": {"tools": ["t"], "prompt": "P"}}

        builder = new_prompt_builder(_Opts())
        session = Session(id="s1", mode="work", metadata='{"tool_tags":["media"]}')
        out = await builder.build(
            None,
            PromptBuildRequest(
                session=session,
                messages=[Message(role="user", content="hi")],
                meta=ConversationMeta(platform="cli", kind="private"),
            ),
        )
        system = out[0].segments[0].text
        self.assertIn("sync-soul", system)
        self.assertIn("<resident_memory>", system)
        self.assertIn("tools: t.", system)
        self.assertIn("P", system)
        self.assertIn("meta: platform=cli, conversation=private.", system)

    async def test_llm_protocol_shape_is_available(self) -> None:
        self.assertTrue(hasattr(LLM, "chat_stream"))
        self.assertEqual(RoleUser, "user")
        self.assertEqual(RoleAssistant, "assistant")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
