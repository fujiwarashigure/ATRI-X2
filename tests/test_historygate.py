"""``atri_x.historygate`` 的单元测试。

全部用鸭子类型的假仓库（记录调用、可选地不具备范围读取能力），不依赖 SQLite。
覆盖：默认放行、策略拒绝时静默丢弃、读操作永远委托、可选能力探测与哨兵异常。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.errors import AtriXError, RangeUnsupportedError
from atri_x.historygate import (
    ChatRepository,
    OutboundRepository,
    Policy,
    new_policy,
    policy_allowed,
    policy_set,
    wrap_chat,
    wrap_outbound,
)
from atri_x.storage import ChatMessage, OutboundMessage


class _ChatMessage:
    """最小的入站历史行（只带策略需要的两个字段）。"""

    def __init__(self, platform: str = "cli", scope_id: str = "private:1") -> None:
        self.platform = platform
        self.platform_scope_id = scope_id
        self.id = "m1"


class _OutboundMessage:
    def __init__(self, platform: str = "cli", scope_id: str = "private:1") -> None:
        self.platform = platform
        self.platform_scope_id = scope_id
        self.id = "o1"


class _FakeChatRepo:
    """基础假仓库：只有必选能力，没有 ``list_range``。"""

    def __init__(self) -> None:
        self.appended: list = []
        self.calls: list[tuple] = []

    def append(self, message) -> None:  # noqa: ANN001
        self.appended.append(message)

    def get_by_platform_message(self, platform, scope_id, platform_message_id):  # noqa: ANN001
        self.calls.append(("get", platform, scope_id, platform_message_id))
        return "row:" + platform_message_id

    def search(self, req):  # noqa: ANN001
        self.calls.append(("search", req))
        return ["s"]

    def around(self, req):  # noqa: ANN001
        self.calls.append(("around", req))
        return ["a"]

    def delete_before(self, cutoff):  # noqa: ANN001
        self.calls.append(("delete_before", cutoff))
        return 3


class _RangeChatRepo(_FakeChatRepo):
    """额外具备 ``list_range`` 的假仓库。"""

    def list_range(self, req):  # noqa: ANN001
        self.calls.append(("list_range", req))
        return ["r"]


class _FakeOutboundRepo:
    def __init__(self, with_counter: bool = False) -> None:
        self.appended: list = []
        self.calls: list[tuple] = []
        self._with_counter = with_counter

    def append(self, message) -> None:  # noqa: ANN001
        self.appended.append(message)

    def list_range(self, req):  # noqa: ANN001
        self.calls.append(("list_range", req))
        return ["r"]

    def delete_before(self, cutoff):  # noqa: ANN001
        self.calls.append(("delete_before", cutoff))
        return 2


class _CountingOutboundRepo(_FakeOutboundRepo):
    def count_range(self, req):  # noqa: ANN001
        self.calls.append(("count_range", req))
        return 7


class TestPolicy(unittest.TestCase):
    def test_default_allows_everything(self) -> None:
        policy = new_policy()
        self.assertTrue(policy.allowed("cli", "private:1"))
        self.assertIsNone(policy.callback())

    def test_set_installs_callback(self) -> None:
        policy = Policy()
        seen: list[tuple[str, str]] = []

        def _fn(platform: str, scope_id: str) -> bool:
            seen.append((platform, scope_id))
            return platform == "cli"

        policy.set(_fn)
        self.assertTrue(policy.allowed("cli", "private:1"))
        self.assertFalse(policy.allowed("telegram", "group:9"))
        # 回调收到的是 trim 过的值。
        self.assertEqual(seen[0], ("cli", "private:1"))
        policy.allowed("  cli  ", "  private:1  ")
        self.assertEqual(seen[-1], ("cli", "private:1"))

    def test_set_none_restores_allow_all(self) -> None:
        policy = Policy()
        policy.set(lambda _p, _s: False)
        self.assertFalse(policy.allowed("cli", "x"))
        policy.set(None)
        self.assertTrue(policy.allowed("cli", "x"))

    def test_none_receiver_helpers(self) -> None:
        # Go 的 nil-receiver 语义：None 策略允许一切，Set 是 no-op。
        self.assertTrue(policy_allowed(None, "cli", "private:1"))
        policy_set(None, lambda _p, _s: False)
        policy = new_policy()
        policy_set(policy, lambda _p, _s: False)
        self.assertFalse(policy_allowed(policy, "cli", "x"))

    def test_concurrent_write_and_read_are_safe(self) -> None:
        policy = new_policy()
        stop = threading.Event()

        def _writer() -> None:
            allowed = True
            while not stop.is_set():
                policy.set((lambda flag: (lambda _p, _s: flag))(allowed))
                allowed = not allowed

        writer = threading.Thread(target=_writer)
        writer.start()
        try:
            for _ in range(500):
                policy.allowed("cli", "private:1")
        finally:
            stop.set()
            writer.join(timeout=5)
        self.assertFalse(writer.is_alive())


class TestWrapChat(unittest.TestCase):
    def test_wrap_none_repo_returns_none(self) -> None:
        self.assertIsNone(wrap_chat(None, new_policy()))

    def test_wrap_none_policy_uses_default(self) -> None:
        repo = _FakeChatRepo()
        wrapped = wrap_chat(repo, None)
        self.assertIsInstance(wrapped, ChatRepository)
        wrapped.append(_ChatMessage())
        self.assertEqual(len(repo.appended), 1)

    def test_append_delegates_when_allowed(self) -> None:
        repo = _FakeChatRepo()
        wrapped = wrap_chat(repo, new_policy())
        wrapped.append(_ChatMessage("cli", "private:1"))
        self.assertEqual(len(repo.appended), 1)

    def test_append_dropped_when_denied(self) -> None:
        repo = _FakeChatRepo()
        policy = new_policy()
        policy.set(lambda platform, _scope: platform != "cli")
        wrapped = wrap_chat(repo, policy)
        wrapped.append(_ChatMessage("cli", "private:1"))
        # 静默丢弃：不写底层，也不抛错。
        self.assertEqual(repo.appended, [])

    def test_append_none_message_is_noop(self) -> None:
        repo = _FakeChatRepo()
        wrapped = wrap_chat(repo, new_policy())
        wrapped.append(None)
        self.assertEqual(repo.appended, [])

    def test_reads_delegate_even_when_denied(self) -> None:
        repo = _FakeChatRepo()
        policy = new_policy()
        policy.set(lambda _p, _s: False)
        wrapped = wrap_chat(repo, policy)
        self.assertEqual(wrapped.get_by_platform_message("cli", "private:1", "pm1"), "row:pm1")
        self.assertEqual(wrapped.search("req"), ["s"])
        self.assertEqual(wrapped.around("req"), ["a"])
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(wrapped.delete_before(cutoff), 3)
        self.assertEqual(len(repo.calls), 4)

    def test_list_range_delegates_when_supported(self) -> None:
        repo = _RangeChatRepo()
        wrapped = wrap_chat(repo, new_policy())
        self.assertEqual(wrapped.list_range("req"), ["r"])

    def test_list_range_raises_when_unsupported(self) -> None:
        repo = _FakeChatRepo()
        wrapped = wrap_chat(repo, new_policy())
        with self.assertRaises(RangeUnsupportedError) as caught:
            wrapped.list_range("req")
        self.assertIn("history range access is not supported", str(caught.exception))
        self.assertIsInstance(caught.exception, AtriXError)

    def test_real_sqlite_chat_history_supports_list_range(self) -> None:
        # 集成口径：真实仓储应当具备可选能力，否则闸门会误报"不支持"。
        from atri_x.storage import sqlite

        import os
        import shutil

        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tmp_historygate_test",
            self._testMethodName,
        )
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root, exist_ok=True)
        store = None
        try:
            store = sqlite.new_chat_history(os.path.join(root, "chat.db"))
            chat = wrap_chat(store.repository(), new_policy())
            outbound = wrap_outbound(store.outbound(), new_policy())
            self.assertEqual(chat.list_range(_empty_range_request()), [])
            self.assertEqual(outbound.list_range(_empty_outbound_request()), [])
            self.assertEqual(outbound.count_range(_empty_outbound_request()), 0)
        finally:
            # Windows 上必须先关连接，否则 rmtree 删不掉仍被占用的 chat.db。
            if store is not None:
                store.close()
            shutil.rmtree(root, ignore_errors=True)


class TestWrapOutbound(unittest.TestCase):
    def test_wrap_none_repo_returns_none(self) -> None:
        self.assertIsNone(wrap_outbound(None, new_policy()))

    def test_wrap_none_policy_uses_default(self) -> None:
        repo = _FakeOutboundRepo()
        wrapped = wrap_outbound(repo, None)
        self.assertIsInstance(wrapped, OutboundRepository)
        wrapped.append(_OutboundMessage())
        self.assertEqual(len(repo.appended), 1)

    def test_append_policy_gate(self) -> None:
        repo = _FakeOutboundRepo()
        policy = new_policy()
        policy.set(lambda _p, scope: scope != "group:9")
        wrapped = wrap_outbound(repo, policy)
        wrapped.append(_OutboundMessage("cli", "private:1"))
        wrapped.append(_OutboundMessage("cli", "group:9"))
        self.assertEqual(len(repo.appended), 1)
        self.assertEqual(repo.appended[0].platform_scope_id, "private:1")

    def test_append_none_message_is_noop(self) -> None:
        repo = _FakeOutboundRepo()
        wrapped = wrap_outbound(repo, new_policy())
        wrapped.append(None)
        self.assertEqual(repo.appended, [])

    def test_reads_delegate(self) -> None:
        repo = _FakeOutboundRepo()
        wrapped = wrap_outbound(repo, new_policy())
        self.assertEqual(wrapped.list_range("req"), ["r"])
        self.assertEqual(
            wrapped.delete_before(datetime(2026, 1, 1, tzinfo=timezone.utc)), 2
        )

    def test_count_range_raises_when_unsupported(self) -> None:
        repo = _FakeOutboundRepo(with_counter=False)
        wrapped = wrap_outbound(repo, new_policy())
        with self.assertRaises(RangeUnsupportedError):
            wrapped.count_range("req")

    def test_count_range_delegates_when_supported(self) -> None:
        repo = _CountingOutboundRepo()
        wrapped = wrap_outbound(repo, new_policy())
        self.assertEqual(wrapped.count_range("req"), 7)


class TestProtocolConformance(unittest.TestCase):
    """包装类必须满足 storage 的 Protocol（否则调用方的 isinstance 会失效）。"""

    def test_chat_wrapper_matches_chat_history_protocol(self) -> None:
        from atri_x.storage import ChatHistoryRepository
        from atri_x.storage.repositories import ChatHistoryRangeRepository

        wrapped = wrap_chat(_RangeChatRepo(), new_policy())
        self.assertIsInstance(wrapped, ChatHistoryRepository)
        self.assertIsInstance(wrapped, ChatHistoryRangeRepository)

    def test_outbound_wrapper_matches_protocols(self) -> None:
        from atri_x.storage import OutboundMessageCounter, OutboundMessageRepository

        wrapped = wrap_outbound(_CountingOutboundRepo(), new_policy())
        self.assertIsInstance(wrapped, OutboundMessageRepository)
        self.assertIsInstance(wrapped, OutboundMessageCounter)


def _empty_range_request():
    from atri_x.storage import ChatHistoryRangeRequest

    return ChatHistoryRangeRequest(
        platform="cli",
        platform_scope_id="private:1",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 1, 2, tzinfo=timezone.utc),
        limit=10,
    )


def _empty_outbound_request():
    from atri_x.storage import OutboundMessageRangeRequest

    return OutboundMessageRangeRequest(
        platform="cli",
        platform_scope_id="private:1",
        since=datetime(2026, 1, 1, tzinfo=timezone.utc),
        until=datetime(2026, 1, 2, tzinfo=timezone.utc),
        limit=10,
    )


class TestRealStorageTypes(unittest.TestCase):
    """确认用的是 storage 的唯一出处类型，而不是本包自造的替身。"""

    def test_messages_are_storage_dataclasses(self) -> None:
        message = ChatMessage(id="m1", platform="cli", platform_scope_id="private:1")
        outbound = OutboundMessage(id="o1", platform="cli", platform_scope_id="private:1")
        repo = _FakeChatRepo()
        outbound_repo = _FakeOutboundRepo()
        wrap_chat(repo, new_policy()).append(message)
        wrap_outbound(outbound_repo, new_policy()).append(outbound)
        self.assertEqual(len(repo.appended), 1)
        self.assertEqual(len(outbound_repo.appended), 1)

    def test_delete_before_receives_aware_datetime(self) -> None:
        repo = _FakeChatRepo()
        wrapped = wrap_chat(repo, new_policy())
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        wrapped.delete_before(cutoff)
        self.assertIs(repo.calls[0][1], cutoff)


if __name__ == "__main__":
    unittest.main()
