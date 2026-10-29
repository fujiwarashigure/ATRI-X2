"""成员专属人设（``atri_x.memberprofile.persona``）与相关工具的单元测试。

覆盖：

* JSON 存储的新增 / 查询 / 开关 / 删除与跨实例热更新；
* 渲染（启用时给角色块、关闭 / 卡不存在时回退空串）；
* 系统提示词来源：启用时顶掉 ``soul`` 段，关闭后恢复；
* ``character_import`` 一步建卡（含按成员激活）与 ``persona_control`` 开关；
* ``character_activate`` 的超级管理员限制与 SOUL 写入。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from types import SimpleNamespace

from atri_x.agent.member_persona_prompt import MemberPersonaSystemPromptSource
from atri_x.agent.system_prompt import (
    ConversationMeta,
    SystemPromptManager,
    SystemPromptPart,
    SystemPromptRequest,
)
from atri_x.character import NewStore, Viewer, WriteRequest
from atri_x.memberprofile.persona import (
    PERSONA_PROMPT_HEADER,
    MemberPersona,
    MemberPersonaStore,
    normalize_persona,
    personas_path,
    render_member_persona,
)
from atri_x.tool.builtin.character import CharacterTools
from atri_x.tool.builtin.character import CharacterActivateTool, CharacterImportTool
from atri_x.tool.builtin.persona import PersonaControlTool
from atri_x.tool.tool import Actor, CallRequest, RoleSuperadmin, RoleUser, WithActor

TMP_ROOT = os.path.join(os.getcwd(), "tmp_member_persona_test")

ADMIN = Viewer(platform="test", actor_id="admin", superadmin=True)
USER = Viewer(platform="qqonebot", actor_id="qqonebot:1001", superadmin=False)


def _store() -> NewStore:
    return NewStore(os.path.join(TMP_ROOT, "characters"))


def _text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _seed_public(store, character_id: str = "bigfish") -> None:
    store.write(
        None,
        WriteRequest(
            id=character_id,
            name="蓝色大青鱼",
            visibility="public",
            docs={"profile": "# 大青鱼\n\n说话软软的。"},
        ),
        ADMIN,
    )


def _ctx(actor: Actor):
    return WithActor(None, actor)


def _req(payload: dict) -> CallRequest:
    return CallRequest(arguments=json.dumps(payload, ensure_ascii=False))


def _run(coro):
    return asyncio.run(coro)


class _SoulSource:
    """假的 SOUL 来源：固定返回 ``SOUL-ONLY``，用来验证专属人设是否顶掉它。"""

    async def parts(self, ctx, req):
        return [SystemPromptPart(name="soul", content="SOUL-ONLY")]


class PersonaStoreCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = MemberPersonaStore(TMP_ROOT)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_upsert_and_get(self) -> None:
        saved = self.store.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        self.assertTrue(saved.active)
        found = self.store.get(platform="qqonebot", user_id="1001")
        self.assertIsNotNone(found)
        self.assertEqual(found.character_id, "bigfish")
        self.assertTrue(os.path.exists(personas_path(TMP_ROOT)))

    def test_second_instance_sees_file_change(self) -> None:
        self.store.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        other = MemberPersonaStore(TMP_ROOT)
        self.assertEqual(other.get(platform="qqonebot", user_id="1001").character_id, "bigfish")

    def test_set_enabled_keeps_binding(self) -> None:
        self.store.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        updated = self.store.set_enabled("qqonebot", "1001", False)
        self.assertIsNotNone(updated)
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.character_id, "bigfish")
        self.assertIsNone(self.store.set_enabled("qqonebot", "nobody", False))

    def test_remove(self) -> None:
        self.store.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        removed = self.store.remove("qqonebot", "1001")
        self.assertIsNotNone(removed)
        self.assertIsNone(self.store.get(platform="qqonebot", user_id="1001"))

    def test_normalize_requires_key_and_clears_enabled_without_card(self) -> None:
        with self.assertRaises(Exception):
            normalize_persona(MemberPersona(platform="", user_id="1"))
        normalized = normalize_persona(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="", enabled=True)
        )
        self.assertFalse(normalized.enabled)


class RenderCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.characters = _store()
        _seed_public(self.characters)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_render_enabled_persona(self) -> None:
        persona = MemberPersona(
            platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True
        )
        text = render_member_persona(persona, self.characters)
        self.assertIn(PERSONA_PROMPT_HEADER, text)
        self.assertIn("说话软软的", text)

    def test_render_disabled_or_missing_returns_empty(self) -> None:
        disabled = MemberPersona(
            platform="qqonebot", user_id="1001", character_id="bigfish", enabled=False
        )
        self.assertEqual(render_member_persona(disabled, self.characters), "")
        missing = MemberPersona(
            platform="qqonebot", user_id="1001", character_id="nope", enabled=True
        )
        self.assertEqual(render_member_persona(missing, self.characters), "")


class PromptSourceCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.characters = _store()
        _seed_public(self.characters)
        self.personas = MemberPersonaStore(TMP_ROOT)
        self.source = MemberPersonaSystemPromptSource(self.personas, self.characters)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _request(self) -> SystemPromptRequest:
        scope = SimpleNamespace(platform="qqonebot", platform_scope_id="group:g1")
        meta = ConversationMeta(
            platform="qqonebot", kind="group", id="g1", user_id="1001", display_name="甲"
        )
        return SystemPromptRequest(mode="chat", scope=scope, meta=meta)

    def test_enabled_persona_replaces_soul(self) -> None:
        self.personas.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        manager = SystemPromptManager([_SoulSource(), self.source])
        text = _run(manager.build(None, self._request()))
        self.assertIn(PERSONA_PROMPT_HEADER, text)
        self.assertIn("说话软软的", text)
        self.assertNotIn("SOUL-ONLY", text)

    def test_disabled_persona_keeps_soul(self) -> None:
        self.personas.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=False)
        )
        manager = SystemPromptManager([_SoulSource(), self.source])
        text = _run(manager.build(None, self._request()))
        self.assertEqual(text, "SOUL-ONLY")

    def test_other_member_keeps_soul(self) -> None:
        self.personas.upsert(
            MemberPersona(platform="qqonebot", user_id="1001", character_id="bigfish", enabled=True)
        )
        request = self._request()
        request.meta = ConversationMeta(
            platform="qqonebot", kind="group", id="g1", user_id="2002", display_name="乙"
        )
        manager = SystemPromptManager([_SoulSource(), self.source])
        self.assertEqual(_run(manager.build(None, request)), "SOUL-ONLY")


class PersonaToolsCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.characters = _store()
        _seed_public(self.characters)
        self.soul_path = os.path.join(TMP_ROOT, "SOUL.md")
        with open(self.soul_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("旧人设\n")
        self.user_ctx = _ctx(Actor(id="qqonebot:1001", platform="qqonebot", role=RoleUser))
        self.admin_ctx = _ctx(Actor(id="cli:local", platform="cli", role=RoleSuperadmin))

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _tools(self, *, allow_user_create: bool = True) -> CharacterTools:
        return CharacterTools(
            store=self.characters,
            media=None,
            soul_path=self.soul_path,
            config_dir=TMP_ROOT,
            allow_user_create=allow_user_create,
        )

    def _persona_tool(self) -> PersonaControlTool:
        return PersonaControlTool(TMP_ROOT, self.characters)

    def test_import_tool_creates_and_binds_member_persona(self) -> None:
        tool = CharacterImportTool(self._tools())
        result = _run(
            tool.call(
                self.user_ctx,
                _req({"text": "# 大青鱼\n\n蓝色。", "id": "myfish", "activate": "member"}),
            )
        )
        self.assertIn("角色卡已创建", result.content)
        self.assertIn("专属人设", result.content)
        persona = MemberPersonaStore(TMP_ROOT).get(platform="qqonebot", user_id="1001")
        self.assertIsNotNone(persona)
        self.assertTrue(persona.enabled)
        self.assertEqual(persona.character_id, "myfish")

    def test_import_tool_soul_requires_superadmin(self) -> None:
        tool = CharacterImportTool(self._tools())
        result = _run(
            tool.call(
                self.user_ctx,
                _req({"text": "# 大青鱼\n\n蓝色。", "id": "soulcard", "activate": "soul"}),
            )
        )
        self.assertIn("需要超级管理员权限", result.content)
        self.assertIn("旧人设", _text(self.soul_path))

    def test_import_tool_soul_for_superadmin(self) -> None:
        tool = CharacterImportTool(self._tools())
        result = _run(
            tool.call(
                self.admin_ctx,
                _req({"text": "# 大青鱼\n\n蓝色。", "id": "soulcard", "activate": "soul"}),
            )
        )
        self.assertIn("全局系统人设", result.content)
        soul = _text(self.soul_path)
        self.assertIn("大青鱼", soul)

    def test_import_tool_blocked_when_user_create_disabled(self) -> None:
        tool = CharacterImportTool(self._tools(allow_user_create=False))
        result = _run(tool.call(self.user_ctx, _req({"text": "# x\n\n正文", "id": "newcard"})))
        self.assertIn("已关闭", result.content)

    def test_persona_control_bind_status_disable_enable_clear(self) -> None:
        tool = self._persona_tool()
        result = _run(
            tool.call(self.user_ctx, _req({"operation": "bind", "character_id": "bigfish"}))
        )
        self.assertIn("已为 1001 绑定专属人设", result.content)
        status = _run(tool.call(self.user_ctx, _req({"operation": "status"})))
        self.assertIn("bigfish", status.content)
        self.assertIn("已启用", status.content)
        disabled = _run(tool.call(self.user_ctx, _req({"operation": "disable"})))
        self.assertIn("恢复默认系统人设", disabled.content)
        self.assertFalse(MemberPersonaStore(TMP_ROOT).get(platform="qqonebot", user_id="1001").enabled)
        enabled = _run(tool.call(self.user_ctx, _req({"operation": "enable"})))
        self.assertIn("已重新启用", enabled.content)
        cleared = _run(tool.call(self.user_ctx, _req({"operation": "clear"})))
        self.assertIn("已删除", cleared.content)
        self.assertIsNone(MemberPersonaStore(TMP_ROOT).get(platform="qqonebot", user_id="1001"))

    def test_persona_control_target_requires_superadmin(self) -> None:
        tool = self._persona_tool()
        result = _run(
            tool.call(
                self.user_ctx,
                _req({"operation": "bind", "character_id": "bigfish", "target_user_id": "2002"}),
            )
        )
        self.assertIn("只有超级管理员", result.content)

    def test_character_activate_tool_requires_superadmin(self) -> None:
        tool = CharacterActivateTool(self._tools())
        result = _run(
            tool.call(self.user_ctx, _req({"operation": "activate", "id": "bigfish"}))
        )
        self.assertIn("只有超级管理员", result.content)

    def test_character_activate_tool_writes_and_restores(self) -> None:
        tool = CharacterActivateTool(self._tools())
        activated = _run(
            tool.call(self.admin_ctx, _req({"operation": "activate", "id": "bigfish"}))
        )
        self.assertIn("全局系统人设", activated.content)
        self.assertIn("大青鱼", _text(self.soul_path))
        restored = _run(tool.call(self.admin_ctx, _req({"operation": "restore"})))
        self.assertIn("回滚", restored.content)
        self.assertEqual(_text(self.soul_path), "旧人设\n")
        status = _run(tool.call(self.admin_ctx, _req({"operation": "status"})))
        self.assertIn("SOUL.md", status.content)


if __name__ == "__main__":
    unittest.main()
