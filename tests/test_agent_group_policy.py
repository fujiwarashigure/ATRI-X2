"""``atri_x.agent`` 群策略 / 群知识 / 群服务 / 群运行态的测试。

覆盖：字段规范化与校验、策略读写与视图、静默时段、工具白名单联动、
知识库增删改查、提醒 / 投票 / 报名三条命令链路、群运行态状态机。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.agent import group_knowledge as gk
from atri_x.agent import group_policy as gp
from atri_x.agent import group_policy_rules as rules
from atri_x.agent import group_runtime as gr
from atri_x.agent import group_services_polls as polls
from atri_x.agent import group_services_reminders as reminders
from atri_x.agent import group_services_signups as signups
from atri_x.agent import group_services_state as services_state
from atri_x.agent import tool_auth
from atri_x.config import GroupPolicyConfig, StateConfig
from atri_x.errors import InvalidError
from atri_x.security import RoleSuperadmin
from tests.agent_fakes import FakeAgent

GROUP_KEY = "qq:group:42"


class FieldRulesTests(unittest.TestCase):
    def test_canonical_field_aliases(self) -> None:
        self.assertEqual(rules.canonical_group_policy_field("Thread_Mode"), "thread-mode")
        self.assertEqual(rules.canonical_group_policy_field("model"), "default-model")
        self.assertEqual(rules.canonical_group_policy_field("kb"), "knowledge")
        self.assertEqual(rules.canonical_group_policy_field("polls"), "services")
        self.assertEqual(rules.canonical_group_policy_field("all"), "")
        self.assertEqual(rules.canonical_group_policy_field("nope"), "")

    def test_split_group_list(self) -> None:
        self.assertEqual(rules.split_group_list("a，b,c;d、e"), ["a", "b", "c", "d", "e"])
        self.assertEqual(rules.split_group_list("clear"), [])
        self.assertEqual(rules.split_group_list("  "), [])

    def test_parse_enabled(self) -> None:
        for value in ("on", "TRUE", "yes", "1", "enabled"):
            self.assertTrue(rules.parse_enabled(value))
        for value in ("off", "false", "no", "0", "disabled"):
            self.assertFalse(rules.parse_enabled(value))
        with self.assertRaises(InvalidError):
            rules.parse_enabled("maybe")

    def test_quiet_hours(self) -> None:
        self.assertTrue(rules.valid_quiet_hours("23:00-07:30"))
        self.assertFalse(rules.valid_quiet_hours("25:00-07:30"))
        self.assertFalse(rules.valid_quiet_hours("2300-0730"))
        night = datetime(2026, 1, 1, 23, 30)
        self.assertTrue(rules.quiet_hours_active("23:00-07:30", night))
        self.assertFalse(rules.quiet_hours_active("23:00-07:30", night.replace(hour=12)))
        self.assertTrue(rules.quiet_hours_active("08:00-08:00", night))

    def test_apply_and_reset_each_field(self) -> None:
        policy = GroupPolicyConfig()
        rules.apply_policy_field(None, policy, "wake", "atri,小助手")
        self.assertEqual(policy.wake_keywords, ["atri", "小助手"])
        rules.apply_policy_field(None, policy, "response", "all")
        self.assertEqual(policy.response_mode_value(), "all")
        rules.apply_policy_field(None, policy, "merge-window", "1500")
        self.assertEqual(policy.merge_window_ms, 1500)
        rules.apply_policy_field(None, policy, "image-quota", "5")
        self.assertEqual(policy.image_quota, 5)
        rules.apply_policy_field(None, policy, "chat-cost-quota", "1.5")
        self.assertAlmostEqual(policy.chat_cost_quota, 1.5)
        rules.apply_policy_field(None, policy, "quiet", "23:00-07:30")
        self.assertEqual(policy.quiet_hours, "23:00-07:30")
        rules.apply_policy_field(None, policy, "history", "off")
        self.assertFalse(policy.is_history_enabled())
        rules.apply_policy_field(None, policy, "tool-allow", "none")
        self.assertTrue(policy.tool_allowlist_restricted())
        self.assertEqual(policy.tool_allowlist, [])
        rules.apply_policy_field(None, policy, "tool-allow", "clear")
        self.assertFalse(policy.tool_allowlist_set)
        for field in (
            "wake",
            "response",
            "merge-window",
            "image-quota",
            "chat-cost-quota",
            "quiet",
            "history",
            "tool-allow",
        ):
            rules.reset_policy_field(policy, field)
        self.assertEqual(policy.wake_keywords, [])
        self.assertEqual(policy.merge_window_ms, 0)
        self.assertEqual(policy.image_quota, 0)
        self.assertEqual(policy.quiet_hours, "")
        self.assertIsNone(policy.history)

    def test_apply_rejects_bad_values(self) -> None:
        policy = GroupPolicyConfig()
        for field, value in (
            ("response", "sometimes"),
            ("merge-window", "99999"),
            ("image-quota", "-1"),
            ("quiet", "25:00-07:00"),
            ("history", "maybe"),
            ("learning-moderation-actions", "bogus"),
        ):
            with self.assertRaises(InvalidError):
                rules.apply_policy_field(None, policy, field, value)

    def test_audit_value(self) -> None:
        policy = GroupPolicyConfig(wake_keywords=["a"], image_quota=3).normalize()
        self.assertEqual(rules.group_policy_field_audit_value(policy, "wake"), "a")
        self.assertEqual(rules.group_policy_field_audit_value(policy, "image-quota"), "3")
        self.assertEqual(rules.group_policy_field_audit_value(policy, "tool-allow"), "inherit")
        restricted = GroupPolicyConfig(tool_allowlist=[], tool_allowlist_set=True).normalize()
        self.assertEqual(
            rules.group_policy_field_audit_value(restricted, "tool-allow"), "none"
        )
        allowed = GroupPolicyConfig(
            tool_allowlist=["web_search"], tool_allowlist_set=True
        ).normalize()
        self.assertEqual(
            rules.group_policy_field_audit_value(allowed, "tool-allow"), "web_search"
        )


class GroupPolicyTests(unittest.TestCase):
    def test_scope_key_matches_context_policy(self) -> None:
        from atri_x.agent.context_policy import context_overflow_key

        agent = FakeAgent()
        scope = agent.scope(None)
        self.assertEqual(gp.context_overflow_key(scope), context_overflow_key(scope))
        self.assertEqual(gp.context_overflow_key(scope), GROUP_KEY)

    def test_is_group_scope_and_management(self) -> None:
        agent = FakeAgent()
        self.assertTrue(gp.is_group_scope(agent, None))
        self.assertFalse(gp.can_manage_current_group_policy(agent, None))
        agent.group_role = "admin"
        self.assertTrue(gp.can_manage_current_group_policy(agent, None))
        self.assertFalse(gp.is_group_scope(FakeAgent(group=False), None))

    def test_set_and_reset_group_policy(self) -> None:
        agent = FakeAgent()

        async def _run() -> None:
            agent.group_role = "owner"
            message = await gp.set_group_policy(agent, None, "wake", "atri,助手")
            self.assertEqual(message, "群策略已更新：wake = atri,助手")
            policy = gp.group_policy_for_scope(agent, agent.scope(None))
            self.assertEqual(policy.wake_keywords, ["atri", "助手"])
            self.assertEqual(agent.saved, 1)
            self.assertIn(GROUP_KEY, agent.group_policy)

            reset = await gp.reset_group_policy(agent, None, "wake")
            self.assertEqual(reset, "群策略字段已重置：wake")
            self.assertEqual(gp.group_policy_for_scope(agent, agent.scope(None)).wake_keywords, [])

            all_reset = await gp.reset_group_policy(agent, None, "all")
            self.assertEqual(all_reset, "当前群策略已重置为全局默认。")
            # 整表重置写回的是"零策略"：归一化后仍是零，所以条目被删掉，
            # 该群回到"继承全局"（工具不受群白名单限制）。
            self.assertNotIn(GROUP_KEY, agent.group_policy)
            policy_after = gp.group_policy_for_scope(agent, agent.scope(None))
            self.assertEqual(policy_after.wake_keywords, [])
            self.assertIsNone(policy_after.tool_allowlist)
            self.assertFalse(policy_after.tool_allowlist_restricted())

        import asyncio

        asyncio.run(_run())

    def test_set_rejects_unknown_and_non_manager(self) -> None:
        agent = FakeAgent()

        async def _run() -> None:
            with self.assertRaises(InvalidError) as caught:
                await gp.set_group_policy(agent, None, "wake", "x")
            self.assertIn("需要当前群群主/管理员", str(caught.exception))
            agent.group_role = "admin"
            with self.assertRaises(InvalidError) as bad:
                await gp.set_group_policy(agent, None, "nope", "x")
            self.assertEqual(str(bad.exception), "未知的群策略字段")
            with self.assertRaises(InvalidError) as denied:
                await gp.set_group_policy(agent, None, "allowed-models", "*")
            self.assertIn("只能由机器人超级管理员配置", str(denied.exception))

        import asyncio

        asyncio.run(_run())

    def test_non_group_scope_message(self) -> None:
        agent = FakeAgent(group=False)
        self.assertEqual(
            gp.group_policy_status(agent, None), "群级策略只适用于群聊；当前不是群聊作用域。"
        )

    def test_status_view_contains_sections(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            wake_keywords=["atri"], image_quota=4, quiet_hours="23:00-07:30", history=False
        ).normalize()
        # 视图按 Go 的 groupPolicyForScope 语义再归一化一次，因此这里显式关掉工具白名单受限。
        text = gp.group_policy_status(agent, None)
        self.assertIn("群级策略", text)
        self.assertIn("作用域：%s" % GROUP_KEY, text)
        self.assertIn("唤醒词：atri", text)
        self.assertIn("生图日额度：已用 0 / 4（剩余 4）", text)
        self.assertIn("静默时段：23:00-07:30", text)
        self.assertIn("修改：/grouppolicy <field> <value>", text)

    def test_budget_text_helpers(self) -> None:
        self.assertEqual(gp.budget_text(0, 7), "已用 7，未设置上限")
        self.assertEqual(gp.budget_text(10, 12), "已用 12 / 10（剩余 0）")
        self.assertEqual(gp.cost_budget_text(0, 1_500_000), "已用 1.500000，未设置上限")
        self.assertEqual(gp.int64_budget_text(1000, 250), "已用 250 / 1000（剩余 750）")

    def test_tool_allowlist_text(self) -> None:
        agent = FakeAgent()
        text = gp.group_tool_allowlist_text(agent, None)
        self.assertIn("chat：继承通用", text)
        self.assertIn("work：继承通用", text)
        self.assertIn("继承全局（不限制）", text)

        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            tool_allowlist=[], tool_allowlist_set=True
        ).normalize()
        self.assertIn("禁止全部", gp.group_tool_allowlist_text(agent, None))

        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            tool_allowlist=["web_search", "web_extract"], tool_allowlist_set=True
        ).normalize()
        text = gp.group_tool_allowlist_text(agent, None)
        self.assertIn("web_search", text)
        self.assertIn("web_extract", text)

        # 分模式白名单优先于通用兜底（本仓扩展）。
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            tool_allowlist=["web_extract"],
            tool_allowlist_set=True,
            tool_allowlist_chat=["web_search"],
            tool_allowlist_chat_set=True,
        ).normalize()
        text = gp.group_tool_allowlist_text(agent, None)
        self.assertIn("chat：仅白名单（1）：web_search", text)
        self.assertIn("work：继承通用", text)
        self.assertIn("通用（/tools allow 写入的兜底）：仅白名单（1）：web_extract", text)

        self.assertEqual(
            gp.group_tool_allowlist_text(FakeAgent(group=False), None),
            "非群聊，工具不受群白名单限制",
        )

    def test_mode_tool_allowlist_falls_back_to_generic(self) -> None:
        plain = GroupPolicyConfig().normalize()
        self.assertEqual(plain.mode_tool_allowlist("chat"), (False, None))
        self.assertFalse(plain.mode_tool_allowlist_configured("chat"))

        generic = GroupPolicyConfig(
            tool_allowlist=["web_search"], tool_allowlist_set=True
        ).normalize()
        self.assertEqual(generic.mode_tool_allowlist("chat"), (True, ["web_search"]))
        self.assertEqual(generic.mode_tool_allowlist("work"), (True, ["web_search"]))
        self.assertFalse(generic.mode_tool_allowlist_configured("chat"))

        split = GroupPolicyConfig(
            tool_allowlist=["web_extract"],
            tool_allowlist_set=True,
            tool_allowlist_chat=[],
            tool_allowlist_chat_set=True,
            tool_allowlist_work=["shell"],
            tool_allowlist_work_set=True,
        ).normalize()
        # chat：显式禁止全部；work：显式白名单；两者都不再看通用。
        self.assertEqual(split.mode_tool_allowlist("chat"), (True, []))
        self.assertEqual(split.mode_tool_allowlist("work"), (True, ["shell"]))
        self.assertTrue(split.mode_tool_allowlist_configured("chat"))
        self.assertTrue(split.mode_tool_allowlist_configured("work"))

    def test_request_keys(self) -> None:
        agent = FakeAgent()
        self.assertEqual(gp.request_scope_key(agent, None), "qq:group:42")
        self.assertEqual(gp.request_fair_key(agent, None), "qq:group:42:u1")

    def test_group_merge_window_is_capped(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(merge_window_ms=99999)
        self.assertEqual(gp.group_merge_window(agent, None), 10.0)

    def test_quiet_hours_via_agent(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(quiet_hours="00:00-23:59")
        self.assertTrue(gp.group_quiet_hours_active(agent, None))
        self.assertFalse(
            gp.group_quiet_hours_active(agent, None, datetime(2026, 1, 1, 23, 59, 30))
        )

    def test_scope_flag_readers(self) -> None:
        agent = FakeAgent()
        self.assertTrue(gp.group_history_enabled_for_scope(agent, "qq", "group:42"))
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(history=False).normalize()
        self.assertFalse(gp.group_history_enabled_for_scope(agent, "qq", "group:42"))
        self.assertTrue(gp.group_history_enabled_for_scope(agent, "qq", "private:1"))
        self.assertFalse(gp.group_history_enabled_for_scope(agent, "", "group:42"))

    def test_model_catalog_gate(self) -> None:
        from atri_x.config import ModelSelection

        agent = FakeAgent()
        agent.model_aliases = {"fast": "work"}
        agent.model_profiles = {"work": ModelSelection(provider="p", model="m")}
        selection = gp.resolve_group_model_selection(agent, "fast")
        self.assertEqual(selection, ModelSelection(provider="p", model="m"))
        self.assertEqual(
            gp.resolve_group_model_selection(agent, "p/m"),
            ModelSelection(provider="p", model="m"),
        )
        self.assertIsNone(gp.resolve_group_model_selection(agent, "nope"))
        policy = GroupPolicyConfig().normalize()
        self.assertTrue(gp.group_model_selection_allowed(agent, policy, selection, "fast"))
        self.assertFalse(gp.group_model_selection_allowed(agent, policy, selection, "p/other"))
        catalog = GroupPolicyConfig(allowed_models=["fast"]).normalize()
        self.assertTrue(gp.group_model_selection_allowed(agent, catalog, selection, "fast"))
        star = GroupPolicyConfig(allowed_models=["*"]).normalize()
        self.assertTrue(gp.group_model_selection_allowed(agent, star, selection, "p/other"))

    def test_learning_gates(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(learning=False).normalize()
        self.assertFalse(gp.group_learning_enabled(agent, None))
        self.assertFalse(gp.authorize_learning_action(agent, None, "view"))
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            learning_moderation=True, learning_moderation_actions=["view"]
        ).normalize()
        agent.group_role = "admin"
        self.assertTrue(gp.authorize_learning_action(agent, None, "view"))
        self.assertFalse(gp.authorize_learning_action(agent, None, "delete"))
        agent.actor_role = RoleSuperadmin
        self.assertTrue(gp.authorize_learning_action(agent, None, "delete"))
        self.assertTrue(gp.group_admin_command_grant(agent, None, "learning"))
        self.assertFalse(gp.group_admin_command_grant(agent, None, "policy"))

    def test_tool_allowlist_flows_into_tool_auth(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(
            tool_allowlist=["read_file"], tool_allowlist_set=True
        ).normalize()
        allowlist = tool_auth.group_tool_allowlist(agent, None)
        self.assertEqual(allowlist, {"read_file": True})


class GroupKnowledgeTests(unittest.TestCase):
    def test_knowledge_crud_and_match(self) -> None:
        import asyncio

        agent = FakeAgent()

        async def _run() -> None:
            entry = await gk.group_knowledge_add(
                agent, None, "几点了", "不知道", "exact", ["现在几点"], []
            )
            self.assertEqual(entry.id, "k1")
            self.assertEqual(entry.match, "exact")
            self.assertTrue(entry.updated_at.endswith("Z"))
            entries = await gk.group_knowledge_list(agent, None)
            self.assertEqual(len(entries), 1)

            matched, ok = await gk.group_knowledge_test(agent, None, "现在几点")
            self.assertTrue(ok)
            self.assertEqual(matched.id, "k1")
            missed, ok = await gk.group_knowledge_test(agent, None, "别的")
            self.assertFalse(ok)
            self.assertIsNone(missed)

            self.assertTrue(await gk.group_knowledge_remove(agent, None, "K1"))
            self.assertFalse(await gk.group_knowledge_remove(agent, None, "k1"))
            await gk.group_knowledge_add(agent, None, "q", "a", "exact")
            self.assertEqual(await gk.group_knowledge_clear(agent, None), 1)
            self.assertEqual(await gk.group_knowledge_clear(agent, None), 0)

        asyncio.run(_run())

    def test_add_validates(self) -> None:
        import asyncio

        agent = FakeAgent()

        async def _run() -> None:
            with self.assertRaises(InvalidError):
                await gk.group_knowledge_add(agent, None, "", "a", "exact")
            with self.assertRaises(InvalidError) as caught:
                await gk.group_knowledge_add(agent, None, "q", "a", "keywords")
            self.assertIn("keywords 模式至少需要一个关键词", str(caught.exception))

        asyncio.run(_run())

    def test_try_answer_schedules_send(self) -> None:
        import asyncio

        agent = FakeAgent()

        async def _run() -> None:
            await gk.group_knowledge_add(agent, None, "问题", "答案", "exact")
            self.assertTrue(gk.try_group_knowledge_answer(agent, None, "问题"))
            await asyncio.sleep(0)
            self.assertEqual(agent.sent, ["答案"])
            self.assertFalse(gk.try_group_knowledge_answer(agent, None, "无关"))

        asyncio.run(_run())

    def test_disabled_globally(self) -> None:
        from atri_x.config import GroupKnowledgeConfig

        agent = FakeAgent()
        agent.group_knowledge_cfg = GroupKnowledgeConfig(enabled=False)
        self.assertFalse(gk.try_group_knowledge_answer(agent, None, "x"))

    def test_snapshot_normalization(self) -> None:
        from atri_x.config import GroupKnowledgeConfig, GroupKnowledgeEntry

        snapshot = {
            "qq:group:42": [
                GroupKnowledgeEntry(id="", question="q", answer="a"),
                GroupKnowledgeEntry(id="bad", question="", answer="a"),
                GroupKnowledgeEntry(
                    id="kw", question="q2", answer="a2", match="keywords", keywords=[]
                ),
            ]
        }
        clean = gk.normalize_group_knowledge_snapshot(snapshot, GroupKnowledgeConfig())
        entries = clean["qq:group:42"]
        self.assertEqual([item.id for item in entries], ["k1"])
        self.assertEqual(entries[0].match, "exact")

    def test_next_knowledge_id_skips_used(self) -> None:
        from atri_x.config import GroupKnowledgeEntry

        used = [GroupKnowledgeEntry(id="k1"), GroupKnowledgeEntry(id="k2")]
        self.assertEqual(gk.next_knowledge_id(used), "k3")


class GroupServicesTests(unittest.TestCase):
    def _create(self, agent: FakeAgent, when: str = "10m") -> str:
        return reminders.reminder_create(agent, None, when, "开会")

    def test_reminder_create_list_remove(self) -> None:
        agent = FakeAgent()
        message = self._create(agent)
        self.assertIn("提醒已创建：r1", message)
        listing = reminders.reminder_list(agent, None)
        self.assertIn("当前群提醒（1 条）：", listing)
        self.assertIn("- r1", listing)
        self.assertEqual(agent.saved, 1)
        removed = reminders.reminder_remove(agent, None, "R1")
        self.assertEqual(removed, "已删除提醒 R1")
        self.assertIsNone(agent.group_reminders.get(GROUP_KEY))

    def test_reminder_validation(self) -> None:
        agent = FakeAgent()
        with self.assertRaises(InvalidError):
            reminders.reminder_create(agent, None, "1s", "开会")
        with self.assertRaises(InvalidError):
            reminders.reminder_create(agent, None, "10m", "  ")
        with self.assertRaises(InvalidError):
            reminders.reminder_create(agent, None, "bogus", "开会")

    def test_reminder_remove_denied_for_others(self) -> None:
        agent = FakeAgent()
        self._create(agent)
        agent.group_reminders[GROUP_KEY][0].created_by = "other"
        with self.assertRaises(InvalidError) as caught:
            reminders.reminder_remove(agent, None, "r1")
        self.assertIn("只有提醒创建者", str(caught.exception))

    def test_reminder_times(self) -> None:
        now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            services_state.parse_reminder_when("10m", now), now + timedelta(minutes=10)
        )
        self.assertEqual(
            services_state.parse_reminder_when("2d", now), now + timedelta(days=2)
        )
        self.assertEqual(
            services_state.parse_reminder_when("1h30m", now),
            now + timedelta(hours=1, minutes=30),
        )
        self.assertEqual(
            services_state.parse_reminder_when("2d3h", now),
            now + timedelta(days=2, hours=3),
        )
        future = services_state.parse_reminder_when("15:04", now)
        self.assertEqual((future.hour, future.minute), (15, 4))
        self.assertEqual(
            services_state.group_service_duration_text(timedelta(seconds=90)), "1m30s"
        )

    def test_reminder_dispatch(self) -> None:
        import asyncio

        agent = FakeAgent()
        entry = {
            "id": "r1",
            "text": "开会",
            "due_at": services_state.format_group_service_time(
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ),
            "created_by": "u1",
            "created_by_name": "card",
            "created_at": "",
            "status": "pending",
        }
        from atri_x.config import GroupReminderConfig

        agent.group_reminders[GROUP_KEY] = [GroupReminderConfig(**entry)]

        async def _run() -> None:
            await reminders.dispatch_due_reminders(agent)
            await asyncio.sleep(0)

        asyncio.run(_run())
        self.assertEqual(agent.group_reminders[GROUP_KEY][0].status, "sent")
        self.assertEqual(len(agent.notices), 1)
        self.assertEqual(agent.notices[0].outputs[0].text, "提醒：开会（由 card 创建）")

    def test_reminder_dispatch_skips_removed_group(self) -> None:
        import asyncio

        from atri_x.config import GroupReminderConfig

        agent = FakeAgent()
        agent.group_runtime[GROUP_KEY] = gr.GroupRuntimeConfig(state="removed")
        agent.group_reminders[GROUP_KEY] = [
            GroupReminderConfig(
                id="r1",
                text="x",
                due_at=services_state.format_group_service_time(
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ),
                status="pending",
            )
        ]

        async def _run() -> None:
            await reminders.dispatch_due_reminders(agent)

        asyncio.run(_run())
        self.assertEqual(agent.group_reminders[GROUP_KEY][0].status, "skipped")

    def test_poll_flow(self) -> None:
        agent = FakeAgent()
        message = polls.poll_create(agent, None, "吃啥", ["面", "饭", "面"])
        self.assertIn("[p1] 吃啥（open）", message)
        self.assertEqual(len(agent.group_polls[GROUP_KEY][0].options), 2)

        voted = polls.poll_vote(agent, None, "P1", "2")
        self.assertEqual(voted, "已投票：P1 选项 2 饭")
        shown = polls.poll_show(agent, None, "p1")
        self.assertIn("你的选择：2 饭", shown)
        self.assertIn("  2. 饭 - 1 票", shown)
        self.assertIn("共 1 票", shown)
        listing = polls.poll_list(agent, None)
        self.assertIn("- p1  吃啥（1 票）", listing)
        closed = polls.poll_close(agent, None, "p1")
        self.assertIn("（closed）", closed)

    def test_poll_validation(self) -> None:
        agent = FakeAgent()
        with self.assertRaises(InvalidError):
            polls.poll_create(agent, None, "", ["a", "b"])
        with self.assertRaises(InvalidError):
            polls.poll_create(agent, None, "q", ["a"])
        with self.assertRaises(InvalidError) as caught:
            polls.poll_vote(agent, None, "p1", "0")
        self.assertEqual(str(caught.exception), "选项序号必须是正整数")

    def test_poll_limits(self) -> None:
        from atri_x.config import GroupServicesConfig

        agent = FakeAgent()
        agent.group_services_cfg = GroupServicesConfig(max_polls_per_scope=1)
        polls.poll_create(agent, None, "q", ["a", "b"])
        with self.assertRaises(InvalidError) as caught:
            polls.poll_create(agent, None, "q2", ["a", "b"])
        self.assertIn("进行中的投票已达到 1 个上限", str(caught.exception))

    def test_signup_flow(self) -> None:
        agent = FakeAgent()
        message = signups.signup_create(agent, None, "团建", 2)
        self.assertIn("[s1] 团建（open，0/2）", message)
        joined = signups.signup_join(agent, None, "s1")
        self.assertEqual(joined, "已加入报名 s1：团建（1/2）")
        self.assertIn("你已加入", signups.signup_show(agent, None, "s1"))
        left = signups.signup_leave(agent, None, "s1")
        self.assertEqual(left, "已退出报名 s1：团建（0/2）")
        closed = signups.signup_close(agent, None, "s1")
        self.assertIn("（closed，0/2）", closed)

    def test_signup_capacity(self) -> None:
        agent = FakeAgent()
        signups.signup_create(agent, None, "团建", 1)
        signups.signup_join(agent, None, "s1")
        agent.actor = lambda ctx: type(  # noqa: ARG005
            "A", (), {"id": "u2", "nickname": "b", "group_card": "", "display_name": ""}
        )()
        with self.assertRaises(InvalidError) as caught:
            signups.signup_join(agent, None, "s1")
        self.assertIn("已满（1/1）", str(caught.exception))

    def test_signup_validation(self) -> None:
        agent = FakeAgent()
        with self.assertRaises(InvalidError):
            signups.signup_create(agent, None, "", 1)
        with self.assertRaises(InvalidError):
            signups.signup_create(agent, None, "x", -1)
        with self.assertRaises(InvalidError):
            signups.signup_create(agent, None, "x", 10**9)

    def test_services_require_group(self) -> None:
        agent = FakeAgent(group=False)
        with self.assertRaises(InvalidError) as caught:
            polls.poll_create(agent, None, "q", ["a", "b"])
        self.assertEqual(str(caught.exception), "该功能只适用于群聊")

    def test_services_disabled_by_policy(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(services=False).normalize()
        with self.assertRaises(InvalidError) as caught:
            polls.poll_create(agent, None, "q", ["a", "b"])
        self.assertEqual(str(caught.exception), "当前群已关闭提醒/投票/报名")

    def test_snapshot_roundtrip_and_normalization(self) -> None:
        from atri_x.config import (
            GroupPollConfig,
            GroupReminderConfig,
            GroupServicesConfig,
            GroupSignupConfig,
            StateGroupServicesConfig,
        )

        stale = services_state.format_group_service_time(
            datetime.now(timezone.utc) - timedelta(days=30)
        )
        snapshot = StateGroupServicesConfig(
            reminders={
                "qq:group:42": [
                    GroupReminderConfig(id="r1", text="x", due_at=stale, status="sent"),
                    GroupReminderConfig(id="r2", text="y", due_at=stale, status="pending"),
                ]
            },
            polls={
                "qq:group:42": [
                    GroupPollConfig(
                        id="p1", question="q", options=["a", "b"], status="closed", created_at=stale
                    )
                ]
            },
            signups={
                "qq:group:42": [
                    GroupSignupConfig(id="s1", title="t", status="closed", created_at=stale)
                ]
            },
        )
        clean = services_state.normalize_group_services_snapshot(
            snapshot, GroupServicesConfig()
        )
        self.assertEqual([item.id for item in clean.reminders["qq:group:42"]], ["r2"])
        self.assertEqual(clean.polls, {})
        self.assertEqual(clean.signups, {})

        agent = FakeAgent()
        services_state.set_group_services_snapshot(agent, clean)
        self.assertEqual(
            services_state.group_services_snapshot(agent).reminders["qq:group:42"][0].id, "r2"
        )

    def test_clone_is_deep(self) -> None:
        from atri_x.config import GroupPollConfig

        source = {"k": [GroupPollConfig(id="p1", question="q", options=["a"], votes={"u": 1})]}
        copied = services_state.clone_group_polls(source)
        copied["k"][0].options.append("b")
        copied["k"][0].votes["x"] = 2
        self.assertEqual(source["k"][0].options, ["a"])
        self.assertEqual(source["k"][0].votes, {"u": 1})


class GroupRuntimeTests(unittest.TestCase):
    def test_normalize_and_blocked(self) -> None:
        self.assertEqual(gr.normalize_group_runtime_state(" MUTED "), "muted")
        self.assertEqual(gr.normalize_group_runtime_state("bogus"), "")
        self.assertTrue(gr.group_runtime_blocked_state("removed"))
        self.assertFalse(gr.group_runtime_blocked_state("active"))
        self.assertFalse(gr.group_runtime_blocked_state(""))

    def test_transition_is_idempotent(self) -> None:
        agent = FakeAgent()
        scope = agent.scope(None)
        previous, changed = gr.transition_group_runtime_for_scope(
            agent, scope, gr.GroupRuntimeConfig(state="muted", reason="self_muted")
        )
        self.assertEqual(previous, "")
        self.assertTrue(changed)
        previous, changed = gr.transition_group_runtime_for_scope(
            agent, scope, gr.GroupRuntimeConfig(state="muted")
        )
        self.assertEqual(previous, "muted")
        self.assertFalse(changed)
        previous, changed = gr.transition_group_runtime_for_scope(
            agent, scope, gr.GroupRuntimeConfig(state="active")
        )
        self.assertEqual(previous, "muted")
        self.assertTrue(changed)
        self.assertNotIn(GROUP_KEY, agent.group_runtime)

    def test_blocked_scope_and_notice_target(self) -> None:
        agent = FakeAgent()
        agent.group_runtime[GROUP_KEY] = gr.GroupRuntimeConfig(state="muted")
        self.assertTrue(gr.group_runtime_blocked_scope(agent, agent.scope(None)))
        from atri_x.delivery import Target

        self.assertTrue(gr.notice_target_blocked(agent, Target(platform="qq", scope_id="group:42")))
        self.assertTrue(
            gr.notice_target_blocked(agent, Target(platform="qq", group_id="42"))
        )
        self.assertFalse(
            gr.notice_target_blocked(agent, Target(platform="qq", scope_id="private:1"))
        )

    def test_apply_state_cancels_and_notifies(self) -> None:
        agent = FakeAgent()
        cancelled: list[str] = []

        class Requests:
            def scope_ids(self, scope_key: str) -> list[str]:
                cancelled.append("ids:" + scope_key)
                return ["r1"]

            def cancel_scope(self, scope_key: str) -> int:
                cancelled.append("cancel:" + scope_key)
                return 2

        agent.requests = Requests()
        agent.mark_turn_canceled = lambda request_id: cancelled.append("mark:" + request_id)
        agent.cancel_inbox_scope = lambda scope_key: 1
        applied = gr.apply_group_runtime_state(agent, agent.scope(None), "muted", "self_muted")
        self.assertTrue(applied)
        self.assertIn("mark:r1", cancelled)
        self.assertIn("cancel:qq:group:42", cancelled)
        self.assertEqual(len(agent.notices), 1)
        self.assertEqual(agent.notices[0].level, "warning")
        self.assertTrue(agent.notices[0].target.superadmins)
        self.assertEqual(agent.saved, 1)
        self.assertFalse(
            gr.apply_group_runtime_state(agent, agent.scope(None), "muted", "self_muted")
        )

    def test_notice_bypass_roundtrip(self) -> None:
        ctx = gr.with_group_runtime_notice_bypass({})
        self.assertTrue(gr.group_runtime_notice_bypass(ctx))
        self.assertFalse(gr.group_runtime_notice_bypass({}))

    def test_update_from_events(self) -> None:
        from atri_x.platform import EventNotice, PlatformEvent

        agent = FakeAgent()
        muted = PlatformEvent(
            platform="qq",
            kind=EventNotice,
            type="notice",
            scope_id="group:42",
            user_id="10001",
            meta={
                "qq_onebot.self_id": "10001",
                "qq_onebot.notice_type": "group_ban",
                "qq_onebot.sub_type": "ban",
            },
        )
        self.assertTrue(gr.update_group_runtime_from_event(agent, muted))
        self.assertEqual(agent.group_runtime[GROUP_KEY].state, "muted")
        lift = PlatformEvent(
            platform="qq",
            scope_id="group:42",
            user_id="10001",
            meta={
                "qq_onebot.self_id": "10001",
                "qq_onebot.notice_type": "group_ban",
                "qq_onebot.sub_type": "lift_ban",
            },
        )
        self.assertTrue(gr.update_group_runtime_from_event(agent, lift))
        self.assertNotIn(GROUP_KEY, agent.group_runtime)
        other = PlatformEvent(
            platform="qq",
            scope_id="group:42",
            user_id="20002",
            meta={"qq_onebot.self_id": "10001", "qq_onebot.notice_type": "group_ban"},
        )
        self.assertFalse(gr.update_group_runtime_from_event(agent, other))
        self.assertFalse(gr.update_group_runtime_from_event(agent, PlatformEvent(scope_id="private:1")))

    def test_runtime_sections_roundtrip(self) -> None:
        agent = FakeAgent()
        agent.group_policy[GROUP_KEY] = GroupPolicyConfig(wake_keywords=["a"]).normalize()
        agent.group_runtime[GROUP_KEY] = gr.GroupRuntimeConfig(state="muted")
        state = StateConfig()
        gr.RuntimeSectionsMixin.publish_extra_runtime_sections(agent, state)
        # 快照经 cloneGroupPolicy → Normalize，因此只比关键字段（见下一条测试）。
        self.assertEqual(
            state.group_policy[GROUP_KEY].wake_keywords, ["a"]
        )
        self.assertEqual(state.group_runtime, agent.group_runtime)
        self.assertIn("group_services", state.__dataclass_fields__)

        other = FakeAgent()
        gr.RuntimeSectionsMixin.apply_extra_runtime_sections(other, state)
        self.assertEqual(
            other.group_policy[GROUP_KEY].wake_keywords, agent.group_policy[GROUP_KEY].wake_keywords
        )
        self.assertEqual(other.group_runtime[GROUP_KEY].state, "muted")

    def test_snapshot_drops_empty_states(self) -> None:
        agent = FakeAgent()
        gr.set_group_runtime_snapshot(
            agent, {"a": gr.GroupRuntimeConfig(state=""), "b": gr.GroupRuntimeConfig(state="muted")}
        )
        self.assertEqual(list(agent.group_runtime.keys()), ["b"])

    def test_is_group_scope_id(self) -> None:
        self.assertTrue(gr.is_group_scope_id("group:1"))
        self.assertTrue(gr.is_group_scope_id(" supergroup:1 "))
        self.assertFalse(gr.is_group_scope_id("private:1"))
        self.assertEqual(gr.group_runtime_snapshot(FakeAgent()), {})


class JsonKeyTests(unittest.TestCase):
    def test_state_group_services_field_names(self) -> None:
        """落库字段名与 Go 的 tag 对齐（供跨仓读取）。"""
        import dataclasses

        from atri_x.config import (
            GroupPollConfig,
            GroupReminderConfig,
            GroupSignupConfig,
        )

        self.assertEqual(
            [field.name for field in dataclasses.fields(GroupReminderConfig)],
            ["id", "text", "due_at", "created_by", "created_by_name", "created_at", "status"],
        )
        self.assertEqual(
            [field.name for field in dataclasses.fields(GroupPollConfig)],
            [
                "id",
                "question",
                "options",
                "votes",
                "voters",
                "created_by",
                "created_by_name",
                "created_at",
                "status",
            ],
        )
        self.assertEqual(
            [field.name for field in dataclasses.fields(GroupSignupConfig)],
            [
                "id",
                "title",
                "capacity",
                "participants",
                "created_by",
                "created_by_name",
                "created_at",
                "status",
            ],
        )
