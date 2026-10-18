"""GUI 协议层用例：帧 → 界面状态（聊天记录、状态栏、补全）。

这一层是纯函数/状态机，不碰 Tk、不碰网络，因此可以精确断言「服务端发什么 →
界面上出现什么」。
"""

from __future__ import annotations

import unittest

from atri_x.gui.protocol import (
    ROLE_ASSISTANT,
    ROLE_ERROR,
    ROLE_NOTICE,
    ROLE_REASONING,
    ROLE_USER,
    Transcript,
    apply_completion,
    apply_frame,
    describe_status,
    format_notice,
    iter_messages,
    phase_label,
    status_from_frame,
)
from atri_x.llm.llm import Usage
from atri_x.platform.cli.protocol import (
    REMOTE_MSG_CHAT,
    REMOTE_MSG_COMPLETE_RESULT,
    REMOTE_MSG_ERROR,
    REMOTE_MSG_HELLO_OK,
    REMOTE_MSG_NOTICE,
    REMOTE_MSG_REASONING,
    REMOTE_MSG_STATUS,
    REMOTE_MSG_STREAM_APPEND,
    REMOTE_MSG_STREAM_FINISH,
    REMOTE_MSG_STREAM_REPLACE,
    RemoteMessage,
)
from atri_x.runtime.status import Phase, Snapshot


def message(kind: str, text: str = "", **extra: object) -> RemoteMessage:
    """按协议类型造一帧（``extra`` 覆盖其余字段）。"""
    frame = RemoteMessage(type=kind, text=text)
    for key, value in extra.items():
        setattr(frame, key, value)
    return frame


class TranscriptTestCase(unittest.TestCase):
    """聊天记录与流式累积。"""

    def test_user_message_finishes_open_stream(self) -> None:
        transcript = Transcript()
        transcript.stream_append("上一轮没收到 finish")
        line = transcript.add_user("你好")
        self.assertEqual(line.role, ROLE_USER)
        self.assertFalse(transcript.streaming)
        self.assertEqual(transcript.lines[0].role, ROLE_ASSISTANT)
        self.assertEqual(transcript.lines[1].role, ROLE_USER)

    def test_stream_append_accumulates_into_single_line(self) -> None:
        transcript = Transcript()
        transcript.stream_append("你")
        transcript.stream_append("好")
        self.assertEqual(len(transcript.lines), 1)
        self.assertEqual(transcript.lines[0].text, "你好")
        self.assertTrue(transcript.lines[0].streaming)
        transcript.stream_finish()
        self.assertFalse(transcript.lines[0].streaming)

    def test_stream_replace_overrides_accumulated_text(self) -> None:
        transcript = Transcript()
        transcript.stream_append("原始")
        transcript.stream_replace("Hook 改写后的最终文本")
        self.assertEqual(transcript.lines[0].text, "Hook 改写后的最终文本")

    def test_chat_replaces_streaming_line_instead_of_duplicating(self) -> None:
        transcript = Transcript()
        transcript.stream_append("流式片段")
        line = transcript.add_chat("完整回复")
        self.assertEqual(len(transcript.lines), 1)
        self.assertEqual(line.text, "完整回复")
        self.assertFalse(line.streaming)

    def test_chat_without_stream_appends_new_line(self) -> None:
        transcript = Transcript()
        line = transcript.add_chat("直接回复")
        self.assertEqual(line.role, ROLE_ASSISTANT)
        self.assertEqual(len(transcript.lines), 1)

    def test_reasoning_chunks_merge_into_one_line(self) -> None:
        transcript = Transcript()
        transcript.add_reasoning("先看")
        transcript.add_reasoning("再看")
        self.assertEqual(len(transcript.lines), 1)
        self.assertEqual(transcript.lines[0].role, ROLE_REASONING)
        self.assertEqual(transcript.lines[0].text, "先看再看")

    def test_empty_stream_is_dropped_on_finish(self) -> None:
        transcript = Transcript()
        transcript.stream_append("")
        transcript.stream_finish()
        self.assertEqual(transcript.lines, [])

    def test_clear_resets_stream_state(self) -> None:
        transcript = Transcript()
        transcript.stream_append("x")
        transcript.clear()
        self.assertEqual(transcript.lines, [])
        self.assertFalse(transcript.streaming)

    def test_text_renders_roles_for_copy(self) -> None:
        transcript = Transcript()
        transcript.add_user("问", at="12:00:00")
        transcript.add_chat("答", at="12:00:01")
        rendered = transcript.text()
        self.assertIn("你: 问", rendered)
        self.assertIn("ATRI: 答", rendered)
        self.assertIn("[12:00:00]", rendered)


class ApplyFrameTestCase(unittest.TestCase):
    """帧分发：哪些动记录、哪些只动状态。"""

    def test_chat_frame_appends(self) -> None:
        transcript = Transcript()
        changed = apply_frame(transcript, message(REMOTE_MSG_CHAT, "你好"))
        self.assertTrue(changed)
        self.assertEqual(transcript.lines[-1].text, "你好")

    def test_stream_frames_update_one_line(self) -> None:
        transcript = Transcript()
        apply_frame(transcript, message(REMOTE_MSG_STREAM_APPEND, "a"))
        apply_frame(transcript, message(REMOTE_MSG_STREAM_APPEND, "b"))
        apply_frame(transcript, message(REMOTE_MSG_STREAM_REPLACE, "ab!"))
        apply_frame(transcript, message(REMOTE_MSG_STREAM_FINISH))
        self.assertEqual(len(transcript.lines), 1)
        self.assertEqual(transcript.lines[0].text, "ab!")
        self.assertFalse(transcript.lines[0].streaming)

    def test_notice_and_error_and_reasoning_have_roles(self) -> None:
        transcript = Transcript()
        apply_frame(transcript, message(REMOTE_MSG_NOTICE, "任务完成", level="INFO"))
        apply_frame(transcript, message(REMOTE_MSG_ERROR, "出错了"))
        apply_frame(transcript, message(REMOTE_MSG_REASONING, "思考中"))
        roles = [line.role for line in transcript.lines]
        self.assertEqual(roles, [ROLE_NOTICE, ROLE_ERROR, ROLE_REASONING])
        self.assertIn("[INFO]", transcript.lines[0].text)

    def test_control_frames_do_not_touch_transcript(self) -> None:
        transcript = Transcript()
        for kind in (REMOTE_MSG_HELLO_OK, REMOTE_MSG_STATUS, REMOTE_MSG_COMPLETE_RESULT):
            self.assertFalse(apply_frame(transcript, message(kind, "")))
        self.assertEqual(transcript.lines, [])

    def test_status_frame_is_parsed_into_snapshot(self) -> None:
        snapshot = Snapshot(phase=Phase.LLM, model="deepseek-v4", provider="deepseek")
        frame = RemoteMessage(type=REMOTE_MSG_STATUS, snapshot=snapshot)
        parsed = status_from_frame(frame)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.phase, Phase.LLM)
        self.assertIsNone(status_from_frame(message(REMOTE_MSG_CHAT, "x")))

    def test_iter_messages_accepts_dicts_and_objects(self) -> None:
        frames = iter_messages([{"type": REMOTE_MSG_CHAT, "text": "hi"}, message(REMOTE_MSG_ERROR, "x")])
        kinds = [frame.type for frame in frames]
        self.assertEqual(kinds, [REMOTE_MSG_CHAT, REMOTE_MSG_ERROR])


class StatusTextTestCase(unittest.TestCase):
    """状态栏与通知文本。"""

    def test_describe_status_uses_shared_formatter(self) -> None:
        snapshot = Snapshot(
            phase=Phase.LLM,
            provider="deepseek",
            model="deepseek-v4",
            usage=Usage(total_tokens=1234),
        )
        text = describe_status(snapshot)
        self.assertIn("llm", text)
        self.assertIn("deepseek/deepseek-v4", text)
        self.assertIn("1,234", text)

    def test_idle_snapshot_renders_empty(self) -> None:
        self.assertEqual(describe_status(Snapshot(phase=Phase.IDLE)), "")
        self.assertEqual(describe_status(None), "")

    def test_phase_label_translates_known_phases(self) -> None:
        self.assertEqual(phase_label("llm"), "生成中")
        self.assertEqual(phase_label("tool"), "工具")
        self.assertEqual(phase_label("weird-phase"), "weird-phase")
        self.assertEqual(phase_label(""), "空闲")

    def test_format_notice_prefixes_level(self) -> None:
        self.assertEqual(format_notice("完成", "warn"), "[WARN] 完成")
        self.assertEqual(format_notice("完成"), "完成")


class CompletionTestCase(unittest.TestCase):
    """补全候选应用（按 UTF-8 字节偏移）。"""

    def test_ascii_replace_range(self) -> None:
        item = {"Text": "/model", "ReplaceStart": 0, "ReplaceEnd": 4}
        self.assertEqual(apply_completion("/mod gpt-4o", item, 4), "/model gpt-4o")

    def test_byte_offset_handles_chinese(self) -> None:
        # "你好世界"：每个汉字 3 字节；替换后两个汉字。
        item = {"Text": "！", "ReplaceStart": 6, "ReplaceEnd": 12}
        self.assertEqual(apply_completion("你好世界", item), "你好！")

    def test_missing_candidate_is_noop(self) -> None:
        self.assertEqual(apply_completion("abc", None), "abc")
        self.assertEqual(apply_completion("abc", {"Text": ""}), "abc")

    def test_invalid_range_inserts_at_cursor(self) -> None:
        item = {"Text": "X", "ReplaceStart": 99, "ReplaceEnd": 100}
        self.assertEqual(apply_completion("ab", item, 1), "aXb")

    def test_dataclass_item_is_supported(self) -> None:
        from atri_x.completion.completion import Item

        item = Item(text="/help", replace_start=0, replace_end=2)
        self.assertEqual(apply_completion("/h", item), "/help")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
