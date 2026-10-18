"""每日会话总结（:mod:`atri_x.maintenance.summary`）的测试。

覆盖：确定性摘要的内容/窗口/条数上限、落盘与读取（含损坏文件容错）、
``Service.run_daily_summary`` 的开关、LLM 二次摘要与失败回退、通知投递。
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.config import DailySummaryConfig, shanghai_tz
from atri_x.maintenance import Service
from atri_x.maintenance.summary import (
    DailySummary,
    collect_digest,
    list_summaries,
    load_summary,
    save_summary,
    window,
)
from atri_x.storage import Message, RoleAssistant, RoleUser, Session
from atri_x.storage.sqlite import new as new_store

from tests._maintenance_support import _AsyncCaseWithTmp, _CaseWithTmp


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day() -> str:
    """与实现同口径的日期标签（Asia/Shanghai）。"""
    return _now().astimezone(shanghai_tz()).strftime("%Y-%m-%d")


def _session(store, session_id: str, *, title: str, hours_ago: float, archived: bool = False):
    moment = _now() - timedelta(hours=hours_ago)
    row = Session(
        id=session_id,
        owner_id="u1",
        platform="cli",
        platform_scope_id="local",
        title=title,
        created_at=moment,
        updated_at=moment,
        archived_at=moment if archived else None,
    )
    store.sessions().create(row)
    return row


def _messages(store, session_id: str, *contents: str) -> None:
    for index, content in enumerate(contents):
        role = RoleUser if index % 2 == 0 else RoleAssistant
        store.messages().append(Message(session_id=session_id, role=role, content=content))


class TestCollectDigest(_CaseWithTmp):
    def setUp(self) -> None:
        super().setUp()
        self.store = new_store(":memory:")
        self.addCleanup(self.store.close)

    def test_window_and_content(self) -> None:
        _session(self.store, "s1", title="项目讨论", hours_ago=2)
        _messages(self.store, "s1", "帮我看下这段代码", "看起来是空指针")
        _session(self.store, "s2", title="旧会话", hours_ago=72)
        _messages(self.store, "s2", "三天前说的话", "好的")
        _session(self.store, "s3", title="归档会话", hours_ago=1, archived=True)
        _messages(self.store, "s3", "归档里的内容", "嗯")

        since, until = window(1)
        text, sessions, messages = collect_digest(self.store, since, until, max_sessions=20)

        self.assertEqual(sessions, 2)  # s2 在窗口外
        self.assertEqual(messages, 4)
        self.assertIn("会话 2 个 / 消息 4 条", text)
        self.assertIn("《项目讨论》", text)
        self.assertIn("帮我看下这段代码", text)
        self.assertIn("看起来是空指针", text)
        self.assertIn("（已归档）", text)
        self.assertNotIn("旧会话", text)

    def test_max_sessions_caps_rows(self) -> None:
        for index in range(5):
            session_id = "s%d" % index
            _session(self.store, session_id, title="会话%d" % index, hours_ago=index + 1)
            _messages(self.store, session_id, "问题%d" % index, "回答%d" % index)

        since, until = window(1)
        text, sessions, messages = collect_digest(self.store, since, until, max_sessions=2)

        self.assertEqual(sessions, 2)
        self.assertEqual(messages, 4)
        # 按 updated_at 倒序：最近的 s0/s1 入选。
        self.assertIn("《会话0》", text)
        self.assertIn("《会话1》", text)
        self.assertNotIn("《会话4》", text)

    def test_empty_window_renders_placeholder(self) -> None:
        since, until = window(1)
        text, sessions, messages = collect_digest(self.store, since, until)
        self.assertEqual((sessions, messages), (0, 0))
        self.assertIn("没有会话活动", text)

    def test_max_chars_truncates(self) -> None:
        _session(self.store, "s1", title="很长的会话", hours_ago=1)
        _messages(self.store, "s1", "长" * 500, "答" * 500)
        since, until = window(1)
        text, _, _ = collect_digest(self.store, since, until, max_chars=120)
        self.assertLessEqual(len(text), 120)
        self.assertIn("已截断", text)


class TestSummaryFiles(_CaseWithTmp):
    def test_save_load_list_roundtrip(self) -> None:
        directory = self.path("daily_summaries")
        first = DailySummary(day="2026-10-17", created_at="a", sessions=1, messages=2, text="A")
        second = DailySummary(day="2026-10-18", created_at="b", sessions=3, messages=4, text="B")
        for item in (first, second):
            save_summary(directory, item)

        self.assertEqual(load_summary(directory, "2026-10-18").text, "B")
        self.assertIsNone(load_summary(directory, "2026-01-01"))
        self.assertEqual(
            [item.day for item in list_summaries(directory)], ["2026-10-18", "2026-10-17"]
        )
        # 原子写：临时文件不残留。
        self.assertEqual(
            [name for name in os.listdir(directory) if name.endswith(".tmp")], []
        )
        with open(os.path.join(directory, "2026-10-18.json"), encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["sessions"], 3)
        self.assertEqual(payload["source"], "digest")

    def test_corrupt_file_is_ignored(self) -> None:
        directory = self.path("daily_summaries")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "2026-10-18.json"), "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertIsNone(load_summary(directory, "2026-10-18"))
        self.assertEqual(list_summaries(directory), [])

    def test_empty_directory_and_limit(self) -> None:
        self.assertEqual(list_summaries(""), [])
        self.assertEqual(list_summaries(self.path("nope")), [])
        directory = self.path("daily_summaries")
        for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
            save_summary(directory, DailySummary(day=day, text=day))
        self.assertEqual(len(list_summaries(directory, limit=2)), 2)


class TestRunDailySummary(_AsyncCaseWithTmp):
    def setUp(self) -> None:
        super().setUp()
        self.store = new_store(":memory:")
        self.addCleanup(self.store.close)
        _session(self.store, "s1", title="项目讨论", hours_ago=2)
        _messages(self.store, "s1", "帮我看下这段代码", "看起来是空指针")
        self.service = Service(store=self.store)
        self.service.daily_summary = DailySummaryConfig(enabled=True, days=1, max_sessions=20)
        self.service.summary_dir = self.path("daily_summaries")

    async def test_writes_summary_file(self) -> None:
        await self.service.run_daily_summary(None)

        files = os.listdir(self.service.summary_dir)
        self.assertEqual(len(files), 1)
        summary = load_summary(self.service.summary_dir, files[0][: -len(".json")])
        self.assertIsNotNone(summary)
        self.assertEqual(summary.sessions, 1)
        self.assertEqual(summary.messages, 2)
        self.assertIn("《项目讨论》", summary.text)
        self.assertEqual(summary.source, "digest")

    async def test_disabled_is_noop(self) -> None:
        self.service.daily_summary.enabled = False
        await self.service.run_daily_summary(None)
        self.assertFalse(os.path.exists(self.service.summary_dir))

    async def test_use_llm_success(self) -> None:
        self.service.daily_summary.use_llm = True

        async def generator(ctx, text):  # noqa: ANN001
            self.assertIn("《项目讨论》", text)
            return "LLM 版本摘要"

        self.service.summary_generator = generator
        await self.service.run_daily_summary(None)

        day = _day()
        snapshot = load_summary(self.service.summary_dir, day)
        self.assertEqual(snapshot.text, "LLM 版本摘要")
        self.assertEqual(snapshot.source, "llm")

    async def test_use_llm_failure_falls_back_to_digest(self) -> None:
        self.service.daily_summary.use_llm = True

        async def broken(ctx, text):  # noqa: ANN001
            raise RuntimeError("model down")

        self.service.summary_generator = broken
        await self.service.run_daily_summary(None)

        day = _day()
        snapshot = load_summary(self.service.summary_dir, day)
        self.assertEqual(snapshot.source, "digest")
        self.assertIn("ATRI-X 会话总结", snapshot.text)

    async def test_notify_uses_report_sender(self) -> None:
        self.service.daily_summary.notify = True
        sent: list[str] = []

        async def sender(ctx, text):  # noqa: ANN001
            sent.append(text)

        self.service.report_sender = sender
        await self.service.run_daily_summary(None)
        self.assertEqual(len(sent), 1)
        self.assertIn("会话总结", sent[0])

    async def test_empty_store_still_writes_placeholder(self) -> None:
        empty = new_store(":memory:")
        self.addCleanup(empty.close)
        self.service.store = empty
        await self.service.run_daily_summary(None)
        files = os.listdir(self.service.summary_dir)
        self.assertEqual(len(files), 1)


if __name__ == "__main__":  # pragma: no cover - 兼容直接运行
    unittest.main()
