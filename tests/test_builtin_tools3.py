"""``cron`` / ``send_file`` / ``get_media`` / ``view_image`` /
``group_analysis`` / ``self_learning_review`` 的单元测试。

对应 Go 测试：``internal/tool/builtin`` 下同名 ``*_test.go``。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.delivery import KindFile, KindImage
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import SegmentImage, SegmentText
from atri_x.media import Input
from atri_x.security import Actor, RoleSuperadmin, RoleUser, with_actor
from atri_x.storage import Media
from atri_x.tool.builtin.common import go_json
from atri_x.tool.builtin.cron import (
    CRON_QUERY_TOOL_NAME,
    CRON_TOOL_NAME,
    CRON_WRITE_TOOL_NAME,
    CronTool,
    RelativeOffset,
    new_cron_tools,
    resolve_create_run_at_at,
    resolve_run_at_at,
    resolve_update_run_at_at,
    CronCreateArgs,
)
from atri_x.tool.builtin.get_media import (
    GET_MEDIA_DOWNLOAD_LIMIT,
    GetMediaTool,
    get_media_history_label,
    new_get_media_tool,
)
from atri_x.tool.builtin.group_analysis import (
    GROUP_ANALYSIS_TOOL_NAME,
    GroupAnalysisTool,
    new_group_analysis_tool,
)
from atri_x.tool.builtin.self_learning import (
    SELF_LEARNING_REVIEW_TOOL_NAME,
    SelfLearningReviewTool,
    new_self_learning_review_tool,
    tool_reviewer,
)
from atri_x.tool.builtin.send_file import (
    SEND_FILE_TOOL_NAME,
    SendFileTool,
    file_name_from_url,
    is_image_mime,
    local_source_path,
    mime_type_from_name,
    new_send_file_tool,
    parse_send_file_args,
    output_for_media_type,
)
from atri_x.tool.builtin.view_image import (
    VIEW_IMAGE_NAME,
    ViewImageTool,
    first_image_index,
    image_indexes,
    new_view_image_tool,
    view_image_segment,
)
from atri_x.tool.capability import Capabilities, with_capabilities
from atri_x.tool.tool import CallRequest, RiskHigh, RiskLow, RiskMedium

TMP_ROOT = os.path.join(os.getcwd(), "tmp_builtin_tools3_test")

MEDIA_A = "media:" + "a" * 64


def _run(coro):
    return asyncio.run(coro)


def _req(name: str, arguments: dict | None = None) -> CallRequest:
    return CallRequest(
        id="1",
        name=name,
        arguments=json.dumps(arguments) if arguments is not None else "",
    )


def _chat_ctx(extra: object = None):
    """带平台消息上下文的 ctx（用 ``message_context_from`` 能读到的形态）。"""
    from atri_x.platform import MessageContext, with_message_context

    ctx = with_message_context(extra, MessageContext(platform="qq", scope_id="g1"))
    return ctx


def _vision_ctx(vision: bool = True, extra: object = None):
    """显式声明视觉能力的 ctx（``Capabilities`` 是 dataclass，不能传散参数）。"""
    return with_capabilities(extra, Capabilities(vision=vision))


# ---------------------------------------------------------------------------
# cron
# ---------------------------------------------------------------------------
class FakeCronService:
    def __init__(self) -> None:
        self.created: list[object] = []
        self.updated: list[object] = []
        self.disabled: list[tuple[str, object]] = []
        self.deleted: list[tuple[str, object]] = []

    def create(self, ctx, req):
        self.created.append(req)
        return _CronJob()

    def update(self, ctx, req):
        self.updated.append(req)
        return _CronJob()

    def get(self, ctx, name, actor):
        return _JobView()

    def list(self, ctx, include_disabled, include_completed, actor):
        return [_JobView()]

    def disable(self, ctx, name, actor):
        self.disabled.append((name, actor))

    def delete(self, ctx, name, actor):
        self.deleted.append((name, actor))


class _CronJob:
    def to_json(self):
        return {"id": "j1", "name": "user.cron.x", "enabled": True}


class _Metadata:
    def to_json(self):
        return {"kind": "cron", "version": 1}


class _Delivery:
    def to_json(self):
        return {"run_id": ""}


class _JobView:
    job = _CronJob()
    metadata = _Metadata()
    delivery = _Delivery()


class CronToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeCronService()
        self.tools = new_cron_tools(self.service)
        self.entry, self.query, self.write = self.tools

    def test_names_and_visibility(self) -> None:
        self.assertEqual(self.entry.name(), CRON_TOOL_NAME)
        self.assertEqual(self.query.name(), CRON_QUERY_TOOL_NAME)
        self.assertEqual(self.write.name(), CRON_WRITE_TOOL_NAME)
        self.assertFalse(self.entry.info().hidden)
        self.assertTrue(self.query.info().hidden)
        self.assertEqual(self.write.info().risk, "high")
        self.assertEqual(self.entry.info().depends_on, ["cron_query", "cron_write"])
        self.assertTrue(self.entry.info().superadmin_only)

    def test_entry_reports_time_and_discovery_content(self) -> None:
        fixed = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        entry = CronTool(self.service)
        entry.runtime_info = type(entry.runtime_info)(now=lambda: fixed)
        result = _run(entry.call(None, CallRequest()))
        self.assertIn("2026-03-04 05:06:07", result.content)
        content, override = entry.discovery_content()
        self.assertFalse(override)
        self.assertIn("当前本地时间：", content)
        self.assertEqual(entry.schema().function.name, "cron")

    def test_query_list_and_single(self) -> None:
        self.assertEqual(
            _run(self.query.call(None, CallRequest())).content,
            '[{"delivery":{"run_id":""},"job":{"enabled":true,"id":"j1",'
            '"name":"user.cron.x"},"metadata":{"kind":"cron","version":1}}]',
        )
        single = _run(self.query.call(None, _req(CRON_QUERY_TOOL_NAME, {"name": "x"})))
        self.assertIn('"j1"', single.content)

    def test_write_requires_operation(self) -> None:
        with self.assertRaises(InvalidError):
            _run(self.write.call(None, _req(CRON_WRITE_TOOL_NAME, {"operation": "nope"})))

    def test_write_disable_and_delete(self) -> None:
        _run(
            self.write.call(
                None, _req(CRON_WRITE_TOOL_NAME, {"operation": "disable", "name": "x"})
            )
        )
        _run(
            self.write.call(
                None, _req(CRON_WRITE_TOOL_NAME, {"operation": "delete", "name": "x"})
            )
        )
        self.assertEqual(len(self.service.disabled), 1)
        self.assertEqual(len(self.service.deleted), 1)
        self.assertEqual(self.service.disabled[0][0], "x")

    def test_write_create(self) -> None:
        result = _run(
            self.write.call(
                None,
                _req(
                    CRON_WRITE_TOOL_NAME,
                    {
                        "operation": "create",
                        "name": "x",
                        "schedule_mode": "once",
                        "run_after_minutes": 5,
                        "trigger_mode": "direct",
                        "message": "hi",
                    },
                ),
            )
        )
        self.assertIn('"ok":true', result.content)
        self.assertEqual(len(self.service.created), 1)
        request = self.service.created[0]
        self.assertEqual(request.schedule_mode, "once")
        self.assertTrue(request.enabled)
        self.assertFalse(request.all_enabled_platforms)
        self.assertTrue(request.run_at)

    def test_write_update_sets_optional_fields(self) -> None:
        _run(
            self.write.call(
                None,
                _req(
                    CRON_WRITE_TOOL_NAME,
                    {
                        "operation": "update",
                        "name": "x",
                        "schedule_mode": "cron",
                        "trigger_mode": "llm",
                        "tool_list_names": ["shell"],
                        "enabled": False,
                    },
                ),
            )
        )
        patch = self.service.updated[0]
        self.assertEqual(patch.schedule_mode, "cron")
        self.assertEqual(patch.trigger_mode, "llm")
        self.assertEqual(patch.tool_list_names, ["shell"])
        self.assertFalse(patch.enabled)
        self.assertIsNone(patch.title)

    def test_resolve_run_at_rules(self) -> None:
        now = datetime(2026, 1, 31, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(resolve_run_at_at("", RelativeOffset(), now), "")
        self.assertEqual(
            resolve_run_at_at("2026-02-01 00:00:00", RelativeOffset(), now),
            "2026-02-01 00:00:00",
        )
        with self.assertRaises(InvalidError):
            resolve_run_at_at("2026-02-01 00:00:00", RelativeOffset(minutes=1), now)
        with self.assertRaises(InvalidError):
            resolve_run_at_at("", RelativeOffset(minutes=-1), now)
        self.assertEqual(
            resolve_run_at_at("", RelativeOffset(minutes=30), now),
            "2026-01-31 10:30:00",
        )
        # Go 的 AddDate(0, 1, 0) 在 1/31 上溢出到 3/3。
        self.assertEqual(
            resolve_run_at_at("", RelativeOffset(months=1), now),
            "2026-03-03 10:00:00",
        )
        self.assertEqual(
            resolve_run_at_at("", RelativeOffset(weeks=1, days=1, hours=1), now),
            "2026-02-08 11:00:00",
        )
        self.assertIsNone(resolve_update_run_at_at(None, RelativeOffset(), now))
        self.assertEqual(
            resolve_update_run_at_at(None, RelativeOffset(minutes=1), now),
            "2026-01-31 10:01:00",
        )
        args = CronCreateArgs(run_after_days=2)
        self.assertEqual(
            resolve_create_run_at_at(args, now), "2026-02-02 10:00:00"
        )

    def test_go_json_escapes_html_and_sorts(self) -> None:
        self.assertEqual(go_json({"b": "<x>&", "a": 1}), '{"a":1,"b":"\\u003cx\\u003e\\u0026"}')

    def test_schema_requires_operation(self) -> None:
        parameters = self.write.schema().function.parameters
        self.assertEqual(parameters["required"], ["operation", "name"])
        self.assertIn("run_after_months", parameters["properties"])


# ---------------------------------------------------------------------------
# send_file
# ---------------------------------------------------------------------------
class FakeFileManager:
    def __init__(self) -> None:
        self.media = FakeMediaCenter()
        self.prepared: list[tuple[str, str, str]] = []

    def prepare(self, source_path: str, name: str = "", mime_type: str = ""):
        self.prepared.append((source_path, name, mime_type))
        resolved_name = name or os.path.basename(source_path)
        resolved_mime = mime_type or "application/octet-stream"

        class _Prepared:
            # 类体里 ``x = x or ...`` 会把 ``x`` 变成类命名空间里的局部名，
            # 因此这里只读外层已经算好的 ``resolved_*``。
            path = source_path
            name = resolved_name
            mime_type = resolved_mime

        return _Prepared()


class FakeMediaCenter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def metadata(self, ctx, id):
        self.calls.append(id)
        return Media(id=id, name="pic.PNG", mime_type="image/png")


class SendFileToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_helpers(self) -> None:
        self.assertTrue(is_image_mime(" image/PNG "))
        self.assertFalse(is_image_mime("text/plain"))
        self.assertEqual(mime_type_from_name("a.png"), "image/png")
        self.assertEqual(file_name_from_url("https://x/y/z.txt?q=1"), "z.txt")
        self.assertEqual(local_source_path("file:///C:/a/b.txt").replace("\\", "/"), "C:/a/b.txt")
        with self.assertRaises(InvalidError):
            local_source_path("file://host/x")
        # Go：``file:///`` 的 Host 为空、Path 为 ``/``，只有 Path 为空才报错。
        self.assertEqual(local_source_path("file:///"), os.sep)
        with self.assertRaises(InvalidError):
            local_source_path("file://")
        self.assertEqual(output_for_media_type("image/png", "p").kind, KindImage)
        self.assertEqual(output_for_media_type("text/plain", "p").kind, KindFile)

    def test_parse_args(self) -> None:
        args = parse_send_file_args(_req(SEND_FILE_TOOL_NAME, {"source": " a ", "name": "n"}))
        self.assertEqual(args.source, " a ")
        self.assertEqual(args.name, "n")
        self.assertEqual(SendFileTool().name(), SEND_FILE_TOOL_NAME)

    def test_media_source(self) -> None:
        files = FakeFileManager()
        tool = new_send_file_tool(files)
        result = _run(tool.call(None, _req(SEND_FILE_TOOL_NAME, {"source": MEDIA_A})))
        self.assertIn("已发送文件：pic.PNG", result.content)
        self.assertEqual(result.outputs[0].kind, KindImage)
        self.assertEqual(result.outputs[0].source.media_id, MEDIA_A)

    def test_media_source_requires_center(self) -> None:
        tool = new_send_file_tool(None)
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _req(SEND_FILE_TOOL_NAME, {"source": MEDIA_A})))

    def test_invalid_media_id(self) -> None:
        tool = new_send_file_tool(FakeFileManager())
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _req(SEND_FILE_TOOL_NAME, {"source": "media:bad"})))

    def test_http_source(self) -> None:
        tool = new_send_file_tool(FakeFileManager())
        result = _run(
            tool.call(
                None,
                _req(SEND_FILE_TOOL_NAME, {"source": "https://example.com/a/b.png"}),
            )
        )
        out = result.outputs[0]
        self.assertEqual(out.kind, KindImage)
        self.assertEqual(out.name, "b.png")
        self.assertEqual(out.source.url, "https://example.com/a/b.png")
        self.assertEqual(out.source.mime_type, "image/png")

    def test_local_source_uses_manager(self) -> None:
        target = os.path.join(TMP_ROOT, "note.txt")
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("x")
        files = FakeFileManager()
        tool = new_send_file_tool(files)
        result = _run(tool.call(None, _req(SEND_FILE_TOOL_NAME, {"source": target})))
        self.assertEqual(result.outputs[0].kind, KindFile)
        self.assertEqual(files.prepared[0][0], os.path.abspath(target))

    def test_requires_source(self) -> None:
        tool = new_send_file_tool(FakeFileManager())
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _req(SEND_FILE_TOOL_NAME, {})))

    def test_assess_risk(self) -> None:
        tool = new_send_file_tool(FakeFileManager())
        media = _run(tool.assess_risk(None, _req(SEND_FILE_TOOL_NAME, {"source": MEDIA_A})))
        self.assertEqual(media.level, RiskMedium)
        http = _run(
            tool.assess_risk(
                None, _req(SEND_FILE_TOOL_NAME, {"source": "https://x/a.png"})
            )
        )
        self.assertEqual(http.level, RiskMedium)
        target = os.path.join(TMP_ROOT, "n.txt")
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("x")
        abs_risk = _run(
            tool.assess_risk(None, _req(SEND_FILE_TOOL_NAME, {"source": target}))
        )
        self.assertEqual(abs_risk.level, RiskHigh)
        self.assertEqual(abs_risk.reasons, ["发送外部文件需要确认"])

    def test_assess_risk_missing_local_file(self) -> None:
        # Go 的 ``validateResolvedPath`` 会 ``os.Stat``，不存在的路径直接报错。
        tool = new_send_file_tool(FakeFileManager())
        target = os.path.join(TMP_ROOT, "missing.txt")
        with self.assertRaises(InvalidError):
            _run(tool.assess_risk(None, _req(SEND_FILE_TOOL_NAME, {"source": target})))

    def test_cron_background_targets_superadmins(self) -> None:
        from atri_x.tool.sandbox import (
            BackgroundKindCron,
            SandboxContext,
            with_sandbox_context,
        )

        tool = new_send_file_tool(FakeFileManager())
        ctx = with_sandbox_context(
            _chat_ctx(),
            SandboxContext(root=TMP_ROOT, background=True, background_kind=BackgroundKindCron),
        )
        result = _run(tool.call(ctx, _req(SEND_FILE_TOOL_NAME, {"source": MEDIA_A})))
        self.assertTrue(result.outputs[0].target.superadmins)
        self.assertEqual(result.outputs[0].target.platform, "qq")


# ---------------------------------------------------------------------------
# get_media
# ---------------------------------------------------------------------------
class FakeHistory:
    def __init__(self, rows: dict[str, object] | None = None) -> None:
        self.rows = rows or {}

    def get_by_platform_message(self, ctx, platform, scope_id, id):
        if id not in self.rows:
            raise NotFoundError("not found")
        return self.rows[id]


class FakeRow:
    """``messages`` 行的 ``segments`` 列在库里是 JSON 文本（Go ``Row.Segments``）。"""

    def __init__(self, segments) -> None:
        if isinstance(segments, str):
            self.segments = segments
        else:
            self.segments = json.dumps(
                [
                    item if isinstance(item, dict) else {"type": item.type}
                    for item in segments
                ]
            )


class FakeCenter:
    def __init__(self, ids: dict[int, str] | None = None, fail: bool = False) -> None:
        self.ids = dict(ids or {})
        self.fail = fail
        self.downloads: list[int] = []

    async def history_ids(self, ctx, row):
        return dict(self.ids)

    async def get_history_media(self, ctx, row, index, resolver):
        self.downloads.append(index)
        if self.fail:
            raise RuntimeError("boom")
        return Media(id=MEDIA_A, name="a.png", mime_type="image/png")


class GetMediaToolTest(unittest.TestCase):
    def test_helpers(self) -> None:
        self.assertEqual(get_media_history_label(SegmentImage, MEDIA_A), "[图片 media:" + "a" * 64 + "]")
        self.assertEqual(get_media_history_label(SegmentText, ""), "[文件 media:未下载]")

    def test_names_and_schema(self) -> None:
        tool = new_get_media_tool(None, None)
        self.assertEqual(tool.name(), "get_media")
        self.assertTrue(tool.info().hidden)
        self.assertEqual(tool.info().tags, ["chat"])
        properties = tool.schema().function.parameters["properties"]
        self.assertEqual(properties["media_index"]["items"]["minItems"], 1)

    def test_requires_message_ids(self) -> None:
        tool = new_get_media_tool(FakeHistory(), FakeCenter())
        with self.assertRaises(InvalidError):
            _run(tool.call(None, _req("get_media", {})))

    def test_media_index_length_mismatch(self) -> None:
        tool = new_get_media_tool(FakeHistory(), FakeCenter())
        with self.assertRaises(InvalidError):
            _run(
                tool.call(
                    None,
                    _req("get_media", {"message_id": ["1"], "media_index": []}),
                )
            )

    def test_reports_missing_message_and_out_of_range(self) -> None:
        row = FakeRow([type("S", (), {"type": SegmentImage})()])
        history = FakeHistory({"1": row})
        tool = new_get_media_tool(history, FakeCenter({1: MEDIA_A}))
        result = _run(
            tool.call(
                _chat_ctx(),
                _req(
                    "get_media",
                    {
                        "message_id": ["#1", "2"],
                        "media_index": [[1, 2], [1]],
                    },
                ),
            )
        )
        self.assertIn(f"[#1] 1. [图片 media:{'a' * 64}]", result.content)
        self.assertIn("[#1] 2. [媒体序号越界]", result.content)
        self.assertIn("[#2] 当前聊天没有该消息。", result.content)

    def test_download_limit(self) -> None:
        rows = {}
        segments = [type("S", (), {"type": SegmentImage})() for _ in range(8)]
        for position in range(8):
            rows[str(position)] = FakeRow(segments)
        tool = new_get_media_tool(FakeHistory(rows), FakeCenter())
        message_ids = {"message_id": [str(i) for i in range(8)]}
        result = _run(tool.call(_chat_ctx(), _req("get_media", message_ids)))
        self.assertIn(f"达到本次 {GET_MEDIA_DOWNLOAD_LIMIT} 个下载尝试上限", result.content)
        self.assertEqual(len(tool.center.downloads), GET_MEDIA_DOWNLOAD_LIMIT)

    def test_download_failure_hides_details(self) -> None:
        row = FakeRow([type("S", (), {"type": SegmentImage})()])
        tool = new_get_media_tool(FakeHistory({"1": row}), FakeCenter(fail=True))
        result = _run(
            tool.call(_chat_ctx(), _req("get_media", {"message_id": ["1"]}))
        )
        self.assertIn("媒体获取失败：来源不可用、超限或关联保存失败", result.content)
        self.assertNotIn("boom", result.content)

    def test_requires_context_and_configuration(self) -> None:
        tool = new_get_media_tool(None, None)
        empty = _run(tool.call(None, _req("get_media", {"message_id": ["1"]})))
        self.assertIn("当前上下文没有平台聊天信息", empty.content)
        with self.assertRaises(InvalidError):
            _run(tool.call(_chat_ctx(), _req("get_media", {"message_id": ["1"]})))


# ---------------------------------------------------------------------------
# view_image
# ---------------------------------------------------------------------------
class FakeImageCenter:
    def __init__(self, mime: str = "image/png", data: bytes = b"\x89PNG") -> None:
        self.mime = mime
        self.data = data
        self.imported_urls: list[str] = []
        self.imported_files: list[str] = []

    async def metadata(self, ctx, id):
        return Media(id=id, name="p.png", mime_type=self.mime)

    async def import_url(self, ctx, url, spec):
        self.imported_urls.append(url)
        return Media(id=MEDIA_A, name="u.png", mime_type=self.mime)

    async def import_file(self, ctx, path, spec):
        self.imported_files.append(path)
        return Media(id=MEDIA_A, name="f.png", mime_type=self.mime)

    async def read_limited(self, ctx, id, limit):
        return self.data, Media(id=id, name="p.png", mime_type=self.mime)

    async def history_ids(self, ctx, row):
        return {}

    async def get_history_media(self, ctx, row, index, resolver):
        return Media(id=MEDIA_A, name="h.png", mime_type=self.mime)


class ViewImageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_helpers(self) -> None:
        segments = [
            type("S", (), {"type": SegmentText})(),
            type("S", (), {"type": SegmentImage})(),
        ]
        self.assertEqual(first_image_index(segments), 2)
        self.assertEqual(image_indexes(segments), [2])
        self.assertEqual(first_image_index([]), 0)
        segment = view_image_segment(Media(id=MEDIA_A, name="n", mime_type="image/png"))
        self.assertEqual(segment.type, SegmentImage)
        self.assertEqual(segment.media, MEDIA_A)

    def test_names_and_schema(self) -> None:
        tool = new_view_image_tool(None, None)
        self.assertEqual(tool.name(), VIEW_IMAGE_NAME)
        self.assertTrue(tool.info().vision_required)
        properties = tool.schema().function.parameters["properties"]
        self.assertEqual(properties["message_id"]["minItems"], 1)
        self.assertEqual(properties["media_index"]["items"]["items"]["minimum"], 1)

    def test_parse_requires_exactly_one_source(self) -> None:
        tool = new_view_image_tool(None, None)
        with self.assertRaises(InvalidError):
            tool.parse_args(_req(VIEW_IMAGE_NAME, {}))
        with self.assertRaises(InvalidError):
            tool.parse_args(
                _req(VIEW_IMAGE_NAME, {"source": MEDIA_A, "message_id": ["1"]})
            )
        with self.assertRaises(InvalidError):
            tool.parse_args(
                _req(VIEW_IMAGE_NAME, {"source": MEDIA_A, "media_index": [[1]]})
            )
        with self.assertRaises(InvalidError):
            tool.parse_args(
                _req(VIEW_IMAGE_NAME, {"message_id": ["1"], "media_index": [[0]]})
            )
        args = tool.parse_args(_req(VIEW_IMAGE_NAME, {"source": " x "}))
        self.assertEqual(args.source, "x")

    def test_local_source_requires_superadmin(self) -> None:
        tool = new_view_image_tool(None, FakeImageCenter())
        target = os.path.join(TMP_ROOT, "a.png")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG")
        with self.assertRaises(InvalidError):
            _run(tool.call(with_actor(_chat_ctx(), Actor(role=RoleUser)), _req(VIEW_IMAGE_NAME, {"source": target})))
        ctx = with_actor(_chat_ctx(), Actor(role=RoleSuperadmin, id="root"))
        result = _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": target})))
        self.assertEqual(result.segments[0].media, MEDIA_A)

    def test_vision_required(self) -> None:
        tool = new_view_image_tool(None, FakeImageCenter())
        # 未声明能力时 ``vision_available`` 与 Go 一样返回 true，因此要显式关掉。
        ctx = _vision_ctx(vision=False)
        with self.assertRaises(InvalidError):
            _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": MEDIA_A})))

    def test_media_and_url_sources(self) -> None:
        center = FakeImageCenter()
        tool = new_view_image_tool(None, center)
        ctx = _vision_ctx()
        result = _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": MEDIA_A})))
        self.assertEqual(result.segments[0].type, SegmentImage)
        result = _run(
            tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": "https://x/a.png"}))
        )
        self.assertEqual(center.imported_urls, ["https://x/a.png"])
        self.assertEqual(result.segments[0].media, MEDIA_A)

    def test_non_image_media_rejected(self) -> None:
        tool = new_view_image_tool(None, FakeImageCenter(mime="text/plain"))
        ctx = _vision_ctx()
        with self.assertRaises(InvalidError):
            _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": MEDIA_A})))

    def test_assess_risk_sensitive_file(self) -> None:
        tool = new_view_image_tool(None, FakeImageCenter())
        ctx = with_actor(_chat_ctx(), Actor(role=RoleSuperadmin, id="root"))
        target = os.path.join(TMP_ROOT, ".env")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG")
        assessment = _run(tool.assess_risk(ctx, _req(VIEW_IMAGE_NAME, {"source": target})))
        self.assertEqual(assessment.level, RiskHigh)
        _run(tool.preflight_confirmation(ctx, _req(VIEW_IMAGE_NAME, {"source": target})))

    def test_message_source_reports_non_image_index(self) -> None:
        rows = {"1": FakeRow([type("S", (), {"type": SegmentText})()])}
        tool = new_view_image_tool(FakeHistory(rows), FakeImageCenter())
        tool.history = FakeHistory(rows)
        ctx = _vision_ctx(extra=_chat_ctx())
        result = _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"message_id": ["1"]})))
        self.assertEqual(result.segments[0].type, SegmentText)
        self.assertIn("没有图片", result.segments[0].text)

    def test_message_source_missing_message(self) -> None:
        tool = new_view_image_tool(FakeHistory({}), FakeImageCenter())
        ctx = _vision_ctx(extra=_chat_ctx())
        result = _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"message_id": ["9"]})))
        self.assertIn("当前聊天没有该消息", result.segments[0].text)

    def test_message_source_returns_image(self) -> None:
        rows = {"1": FakeRow([type("S", (), {"type": SegmentImage})()])}
        tool = new_view_image_tool(FakeHistory(rows), FakeImageCenter())
        ctx = _vision_ctx(extra=_chat_ctx())
        result = _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"message_id": ["1"]})))
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[1].type, SegmentImage)

    def test_requires_center(self) -> None:
        tool = new_view_image_tool(None, None)
        ctx = _vision_ctx()
        with self.assertRaises(InvalidError):
            _run(tool.call(ctx, _req(VIEW_IMAGE_NAME, {"source": MEDIA_A})))


# ---------------------------------------------------------------------------
# group_analysis
# ---------------------------------------------------------------------------
class FakeGroupAnalysisService:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.summary = ""

    def analyze(self, req):
        self.requests.append(req)

        class _Report:
            def format_text(self):
                return "报告正文"

        return _Report()

    async def summarize(self, ctx, report):
        return self.summary


class GroupAnalysisToolTest(unittest.TestCase):
    def test_names_and_schema(self) -> None:
        tool = new_group_analysis_tool(None, 0)
        self.assertEqual(tool.name(), GROUP_ANALYSIS_TOOL_NAME)
        self.assertEqual(tool.max_messages, 5000)
        self.assertEqual(tool.info().tags, ["chat", "group"])
        properties = tool.schema().function.parameters["properties"]
        self.assertIn("days", properties)

    def test_unconfigured_and_no_context(self) -> None:
        tool = new_group_analysis_tool(None, 10)
        self.assertIn("未配置", _run(tool.call(None, CallRequest())).content)
        service = FakeGroupAnalysisService()
        tool = new_group_analysis_tool(service, 10)
        self.assertIn("没有平台聊天信息", _run(tool.call(None, CallRequest())).content)

    def test_clamps_and_summarizes(self) -> None:
        service = FakeGroupAnalysisService()
        service.summary = " 摘要 "
        tool = new_group_analysis_tool(service, 7)
        ctx = _chat_ctx()
        fixed = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
        tool.runtime_info = type(tool.runtime_info)(now=lambda: fixed)
        result = _run(
            tool.call(ctx, _req(GROUP_ANALYSIS_TOOL_NAME, {"days": 99, "limit": 999}))
        )
        self.assertEqual(result.content, "摘要：摘要\n\n报告正文")
        request = service.requests[0]
        self.assertEqual(request.limit, 7)
        self.assertEqual(request.since, fixed - timedelta(days=30))
        self.assertEqual(request.until, fixed)
        self.assertEqual(request.platform, "qq")

    def test_defaults_days_and_limit(self) -> None:
        service = FakeGroupAnalysisService()
        tool = new_group_analysis_tool(service, 100)
        fixed = datetime(2026, 5, 6, tzinfo=timezone.utc)
        tool.runtime_info = type(tool.runtime_info)(now=lambda: fixed)
        _run(tool.call(_chat_ctx(), CallRequest()))
        request = service.requests[0]
        self.assertEqual(request.limit, 100)
        self.assertEqual(request.since, fixed - timedelta(days=1))


# ---------------------------------------------------------------------------
# self_learning_review
# ---------------------------------------------------------------------------
class FakeSelfLearningService:
    def __init__(self) -> None:
        self.ready_value = True
        self.decisions: list[tuple] = []
        self.undos: list[tuple] = []

    def ready(self) -> bool:
        return self.ready_value

    def stats(self, platform, scope_id):
        return 2, 3

    def mine(self, platform, scope_id, min_count, limit):
        class _Stats:
            created = 1
            updated = 2
            skipped = 3
            scanned = 4
            total_chars = 5
            truncated = True
            timed_out = True

        return _Stats()

    def review(self, platform, scope_id, status, limit):
        class _Candidate:
            status = "pending"
            id = "c1"
            count = 4
            pattern = "p"

        return [_Candidate()]

    def decide(self, platform, scope_id, id, status, meaning, reviewer):
        self.decisions.append((id, status, meaning, reviewer))

    def undo(self, platform, scope_id, id, reviewer):
        self.undos.append((id, reviewer))

    def history(self, platform, scope_id, candidate_id, limit):
        class _Record:
            from_status = "pending"
            to_status = "approved"
            reviewer = "u1"

        return [_Record()]


class SelfLearningToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeSelfLearningService()
        self.tool = new_self_learning_review_tool(self.service)

    def test_names_and_schema(self) -> None:
        self.assertEqual(self.tool.name(), SELF_LEARNING_REVIEW_TOOL_NAME)
        self.assertEqual(self.tool.info().risk, "high")
        self.assertTrue(self.tool.info().superadmin_only)
        self.assertIn("action", self.tool.schema().function.parameters["properties"])

    def test_unconfigured(self) -> None:
        self.assertIn(
            "未配置", _run(new_self_learning_review_tool(None).call(None, CallRequest())).content
        )
        self.service.ready_value = False
        self.assertIn("未配置", _run(self.tool.call(None, CallRequest())).content)

    def test_no_context(self) -> None:
        self.assertIn("没有平台聊天信息", _run(self.tool.call(None, CallRequest())).content)

    def test_actions(self) -> None:
        ctx = _chat_ctx()
        self.assertIn("待审 2 条，已批准 3 条", _run(self.tool.call(ctx, CallRequest())).content)
        mined = _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "mine"})))
        self.assertIn("新增 1 条", mined.content)
        self.assertIn("挖掘超时", mined.content)
        listed = _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "list"})))
        self.assertIn("1. [pending] id=c1 count=4 pattern=p", listed.content)
        approved = _run(
            self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "approve", "id": "c1", "meaning": "m"}))
        )
        self.assertIn("已标记为 approved", approved.content)
        self.assertEqual(self.service.decisions[0][3], "tool")
        rejected = _run(
            self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "reject", "id": "c1"}))
        )
        self.assertIn("rejected", rejected.content)
        undone = _run(
            self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "undo", "id": "c1"}))
        )
        self.assertIn("已撤回为待审", undone.content)
        history = _run(
            self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "history", "id": "c1"}))
        )
        self.assertIn("1. pending → approved by u1", history.content)

    def test_missing_id_and_unknown_action(self) -> None:
        ctx = _chat_ctx()
        self.assertIn(
            "需要 id",
            _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "approve"}))).content,
        )
        self.assertIn(
            "需要 id",
            _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "undo"}))).content,
        )
        self.assertIn(
            "需要 id",
            _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "history"}))).content,
        )
        self.assertIn(
            "action 只支持",
            _run(self.tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "x"}))).content,
        )

    def test_empty_results(self) -> None:
        ctx = _chat_ctx()

        class Empty(FakeSelfLearningService):
            def review(self, platform, scope_id, status, limit):
                return []

            def history(self, platform, scope_id, candidate_id, limit):
                return []

        tool = new_self_learning_review_tool(Empty())
        self.assertIn(
            "没有符合条件的候选",
            _run(tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "list"}))).content,
        )
        self.assertIn(
            "没有审核记录",
            _run(tool.call(ctx, _req(SELF_LEARNING_REVIEW_TOOL_NAME, {"action": "history", "id": "c"}))).content,
        )

    def test_tool_reviewer_prefers_actor_fields(self) -> None:
        ctx = with_actor(None, Actor(id="", platform_user_id="p1", nickname="n"))
        self.assertEqual(tool_reviewer(ctx), "p1")
        self.assertEqual(tool_reviewer(with_actor(None, Actor(id="u1"))), "u1")
        self.assertEqual(tool_reviewer(None), "tool")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
