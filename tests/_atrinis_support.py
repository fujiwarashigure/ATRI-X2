"""``atri_x.atrinis`` 测试的共享夹具（非测试模块，不被 unittest 收集）。

从 ``tests/test_atrinis.py`` 拆出：``tmp_atrinis_test/`` 临时目录、与 Go
``service_test.go`` 同构的 ``AtrinisConfig``、假 runner / 假平台调用器，
以及 :class:`_ServiceCase` 基类。
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.atriena import (
    Atriwisp,
    Call,
    CallKindCapability,
    CapabilityMessageRecall,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Origin,
    OriginInternal,
    Request,
    Segment,
    SegmentKindImage,
    SegmentKindText,
    Target,
    VersionV2,
)
from atri_x.atrinis import (
    ATRIWISP_NAME_PATTERN,
    Options,
    Service,
    StatusCompleted,
    StatusDelivering,
    StatusDuplicate,
    StatusFailed,
    StatusQueued,
    StatusResultReady,
    UNAUTHORIZED_MESSAGE,
    atrinis_sandbox_subdir,
    background_tool_names,
    content_hash,
    decode_resolved_targets,
    first_non_empty,
    first_platform,
    hash_text,
    is_atrinis_model_slot,
    is_atrinis_session_mode,
    normalize_targets,
    segments_content_text,
    set_from_strings,
    trim_strings,
    unique_sorted,
    unique_targets,
    validate_media_segment,
    validate_segment_url,
)
from atri_x.background import JSONResult
from atri_x.config import (
    AtrinisAtriwispConfig,
    AtrinisConfig,
    AtrinisTargetConfig,
    ModelSelection,
)
from atri_x.errors import InvalidError
from atri_x.storage import (
    AtrinisReportDeliveryDelivered,
    AtrinisReportDeliveryFailed,
    Message,
    RoleAssistant,
    Session,
    SessionModeWork,
    SessionStatusActive,
    sqlite,
)
from atri_x.toolrun import AtriwispToolDeclaration

__all__ = [
    "TMP_ROOT",
    "_FakePlatformCaller",
    "_FakePlatformCallerResolver",
    "_FakeRunner",
    "_ServiceCase",
    "external_tool",
    "test_request",
]


TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_atrinis_test")


def test_request(mode: str) -> Request:
    """与 Go ``testRequest`` 等价的请求。"""
    return Request(
        version=VersionV2,
        atriwisp=Atriwisp(name="watcher", tags=["test"]),
        source="source",
        id="event-1",
        mode=mode,
        format="text",
        content="hello",
        targets=[Target(platform="cli")],
    )


def external_tool(name: str) -> AtriwispToolDeclaration:
    """与 Go ``testExternalTool`` 等价的外部工具声明。"""
    return AtriwispToolDeclaration(
        name=name,
        description="test external tool",
        endpoint="http://127.0.0.1:32171/tools/" + name,
        timeout_seconds=5,
        schema={"type": "object", "properties": {"detail": {"type": "boolean"}}},
    )


class _FakeRunner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list = []

    async def run_background(self, ctx, req):  # noqa: ANN001
        from atri_x.background import RunResult

        self.requests.append(req)
        return RunResult(session_id="bg-session", message_id="bg-message", text=self.text)


class _FakePlatformCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_platform_api(self, ctx, api, params):  # noqa: ANN001
        self.calls.append((api, dict(params or {})))
        return "{}"


class _FakePlatformCallerResolver:
    def __init__(self, callers: dict) -> None:
        self.callers = callers

    def platform_caller(self, platform: str):
        caller = self.callers.get(platform)
        return caller, caller is not None


class _ServiceCase(unittest.IsolatedAsyncioTestCase):
    """共享夹具：临时目录 + SQLite store + 与 Go 测试同构的 AtrinisConfig。"""

    def setUp(self) -> None:
        super().setUp()
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.case_dir = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.case_dir, ignore_errors=True)
        os.makedirs(os.path.join(self.case_dir, "sandbox"), exist_ok=True)
        self.store = sqlite.new(os.path.join(self.case_dir, "test.db"))
        self.sent: list[tuple[str, list]] = []
        self.service = self._make_service()

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            shutil.rmtree(self.case_dir, ignore_errors=True)
        super().tearDown()

    # -- 夹具 ---------------------------------------------------------------

    async def _default_send(self, ctx, target, outputs):  # noqa: ANN001
        self.sent.append((target.platform, list(outputs)))
        from atri_x.delivery import Receipt

        return Receipt()

    def _make_service(
        self, runner=None, send=None, config: AtrinisConfig | None = None
    ) -> Service:
        cfg = config or AtrinisConfig(
            enabled=True,
            allowed_tools=["web_search"],
            atriwisps={
                "watcher": AtrinisAtriwispConfig(
                    allowed_tokens=["home"],
                    allowed_tools=["shell"],
                    disabled_external_tools=["danger_tool"],
                ),
                "configured": AtrinisAtriwispConfig(),
                "enabled": AtrinisAtriwispConfig(enabled=True),
                "disabled": AtrinisAtriwispConfig(enabled=False),
            },
        )
        return Service(
            Options(
                config=cfg,
                sandbox_root=os.path.join(self.case_dir, "sandbox"),
                tokens={"home": "secret", "alt": "alt-secret"},
                store=self.store,
                send=send or self._default_send,
                runner=runner,
                enabled_platforms=["cli", "qqonebot"],
                resolve_model=lambda slot: ModelSelection(
                    provider="provider-" + slot, model="model-" + slot
                ),
            )
        )

    async def _dispatch(self, req: Request, origin: Origin | None = None):
        return await self.service.dispatch_atriena(
            None, origin or Origin(kind=OriginInternal, name="home"), req
        )

    async def _queue_llm(self, req: Request):
        """跑完 handle 并把入队的事件取出来。"""
        queued: list = []
        self.service.set_llm_enqueuer(lambda ctx, item: queued.append(item))
        response = await self.service.handle(None, "secret", req)
        return response, (queued[0] if queued else None)


