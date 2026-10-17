"""``atri_x.atrinis`` 的 HTTP 入口与 wire 编码测试。

两段：

* **纯函数**：``configured_timeout`` / ``bearer_token`` / ``decode_single_json`` /
  ``response_status``，以及落库用的 ``delivery.Output`` / ``Receipt`` wire 编解码
  （Go 这两个结构没有 json tag，marshal 出来是 PascalCase）。
* **端到端**：真的用 :class:`atri_x.atrinis.http.Runtime` 绑定 ``127.0.0.1:0``，
  再用 :class:`atri_x.net.httpclient.HTTPClient` 发请求，覆盖
  ``internal/atrinis/http_test.go`` 的路由 / 鉴权 / 体积上限 / 尾部 JSON 契约。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import unittest

from atri_x.atriena import Atriwisp, ModeRecord, Request, Response, StatusFailed, Target
from atri_x.atrinis import (
    Options,
    Runtime,
    Service,
    UNAUTHORIZED_MESSAGE,
    bearer_token,
    configured_timeout,
    decode_single_json,
    output_from_json,
    output_to_json,
    receipt_from_json,
    receipt_to_json,
    response_status,
)
from atri_x.config import AtrinisConfig, AtrinisHTTPConfig
from atri_x.delivery import Kind, Output, Receipt, SentMessage, Source, Target as DeliveryTarget
from atri_x.net.httpclient import HTTPClient
from atri_x.storage import sqlite

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_atrinis_test")
TOKEN = "secret"


def valid_http_event(identifier: str) -> dict:
    """与 Go ``validHTTPEvent`` 等价的请求体。"""
    return {
        "version": "atriena.v2",
        "atriwisp": {"name": "watcher", "tags": ["test"]},
        "source": "http-test",
        "id": identifier,
        "mode": "record",
        "format": "text",
        "content": "hello",
        "targets": [{"platform": "cli"}],
    }


class TestPureHelpers(unittest.TestCase):
    def test_configured_timeout(self) -> None:
        self.assertEqual(configured_timeout(0, 5), 5)
        self.assertEqual(configured_timeout(-3, 5), 5)
        self.assertEqual(configured_timeout(7, 5), 7)

    def test_bearer_token(self) -> None:
        self.assertEqual(bearer_token({"Authorization": "Bearer abc"}), "abc")
        self.assertEqual(bearer_token({"Authorization": "  Bearer  abc  "}), "abc")
        # Authorization 不是 Bearer 前缀时回落到 X-Atrinis-Token（Go 的 fallthrough）。
        self.assertEqual(
            bearer_token({"Authorization": "Basic x", "X-Atrinis-Token": "tok"}), "tok"
        )
        self.assertEqual(bearer_token({"X-Atrinis-Token": " tok "}), "tok")
        self.assertEqual(bearer_token({}), "")

    def test_decode_single_json(self) -> None:
        self.assertEqual(decode_single_json('{"a":1}\n \t'), {"a": 1})
        self.assertEqual(decode_single_json("[]"), [])
        self.assertEqual(decode_single_json("true"), True)
        for trailing in (" {}", " []", " true", " not-json", "{}{"):
            with self.assertRaises(ValueError) as caught:
                decode_single_json('{"a":1}' + trailing)
            self.assertIn("unexpected trailing JSON value", str(caught.exception))
        with self.assertRaises(ValueError):
            decode_single_json("")
        with self.assertRaises(ValueError):
            decode_single_json("{oops")

    def test_response_status(self) -> None:
        self.assertEqual(response_status(Response(accepted=True)), 202)
        self.assertEqual(
            response_status(Response(accepted=False, status=StatusFailed, error=UNAUTHORIZED_MESSAGE)),
            401,
        )
        self.assertEqual(
            response_status(Response(accepted=False, status=StatusFailed, error="bad")), 400
        )
        # direct 模式"已接受但发送失败"仍是 202（与 Go 的 `!resp.Accepted` 判定一致）。
        self.assertEqual(
            response_status(Response(accepted=True, status=StatusFailed, error="boom")), 202
        )


class TestDeliveryWire(unittest.TestCase):
    def test_output_uses_pascal_case_keys(self) -> None:
        out = Output(
            kind=Kind.IMAGE,
            name="图",
            alt_text="alt",
            emoticon_id="e1",
            reply_to_platform_message_id="m1",
            source=Source(media_id="media:" + "a" * 64, mime_type="image/png"),
            target=DeliveryTarget(platform="cli"),
        )
        payload = output_to_json(out)
        self.assertEqual(
            sorted(payload),
            [
                "AltText",
                "EmoticonID",
                "Kind",
                "Meta",
                "Name",
                "ReplyToPlatformMessageID",
                "Source",
                "Target",
                "Text",
            ],
        )
        self.assertEqual(payload["Kind"], "image")
        self.assertEqual(payload["Source"]["media"], "media:" + "a" * 64)
        self.assertIsNone(payload["Source"]["Data"])
        self.assertEqual(payload["Target"], {"platform": "cli"})

    def test_output_round_trip(self) -> None:
        out = Output(
            kind=Kind.FILE,
            text="",
            name="报告.pdf",
            source=Source(path="/sandbox/x.pdf", mime_type="application/pdf", data=b"\x00\x01"),
            target=DeliveryTarget(platform="telegram", group_id="9"),
            meta={"delivery.timing": "immediate"},
        )
        restored = output_from_json(json.dumps(output_to_json(out), ensure_ascii=False))
        self.assertEqual(restored.kind, out.kind)
        self.assertEqual(restored.name, out.name)
        self.assertEqual(restored.source.path, "/sandbox/x.pdf")
        self.assertEqual(restored.source.data, b"\x00\x01")
        self.assertEqual(restored.target.group_id, "9")
        self.assertEqual(restored.meta, {"delivery.timing": "immediate"})

    def test_source_omits_media_when_empty(self) -> None:
        payload = output_to_json(Output(kind=Kind.TEXT, text="hi"))
        self.assertNotIn("media", payload["Source"])
        self.assertIsNone(payload["Meta"])

    def test_receipt_round_trip(self) -> None:
        receipt = Receipt(
            platform_message_ids=["a", "b"],
            sent_messages=[
                SentMessage(
                    platform_message_id="a", platform="cli", scope_id="private:1", output_indexes=[0]
                )
            ],
            failed=True,
            failure="partial",
        )
        payload = receipt_to_json(receipt)
        self.assertEqual(payload["PlatformMessageIDs"], ["a", "b"])
        self.assertTrue(payload["Failed"])
        restored = receipt_from_json(json.dumps(payload))
        self.assertEqual(restored.platform_message_ids, ["a", "b"])
        self.assertEqual(restored.sent_messages[0].scope_id, "private:1")
        self.assertTrue(restored.failed)
        self.assertEqual(restored.failure, "partial")

    def test_receipt_none_becomes_nil_slices(self) -> None:
        payload = receipt_to_json(None)
        self.assertIsNone(payload["PlatformMessageIDs"])
        self.assertIsNone(payload["SentMessages"])


class TestRuntimeConfig(unittest.TestCase):
    def _runtime(self, cfg: AtrinisHTTPConfig) -> Runtime:
        os.makedirs(TMP_ROOT, exist_ok=True)
        case_dir = os.path.join(TMP_ROOT, "runtime_cfg_" + str(id(self)))
        shutil.rmtree(case_dir, ignore_errors=True)
        os.makedirs(case_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, case_dir, True)
        store = sqlite.new(os.path.join(case_dir, "test.db"))
        self.addCleanup(store.close)
        service = Service(
            Options(
                config=AtrinisConfig(enabled=True),
                store=store,
                tokens={"home": TOKEN},
                sandbox_root=os.path.join(case_dir, "sandbox"),
            )
        )
        return Runtime(cfg, service)

    def test_defaults(self) -> None:
        runtime = self._runtime(AtrinisHTTPConfig())
        self.assertEqual(runtime.addr, "127.0.0.1:32170")
        self.assertEqual(runtime.queue.maxsize, 128)
        # 本仓 AtrinisHTTPConfig() 的默认 workers 是 2（Go 的零值是 0）；
        # 只有 <= 0 才回落到 1。
        self.assertEqual(runtime.workers, 2)
        self.assertEqual(self._runtime(AtrinisHTTPConfig(workers=0)).workers, 1)
        self.assertEqual(runtime.max_body_bytes, 1024 * 1024)
        self.assertEqual(runtime.read_header_timeout, 5)
        self.assertEqual(runtime.read_timeout, 30)
        self.assertEqual(runtime.write_timeout, 300)
        self.assertEqual(runtime.idle_timeout, 60)
        self.assertFalse(runtime.is_running())

    def test_configured_values(self) -> None:
        runtime = self._runtime(
            AtrinisHTTPConfig(
                addr="127.0.0.1:0",
                queue_size=4,
                workers=3,
                max_body_bytes=16,
                read_header_timeout_seconds=7,
                read_timeout_seconds=11,
                write_timeout_seconds=13,
                idle_timeout_seconds=17,
            )
        )
        self.assertEqual(runtime.address(), ("127.0.0.1", 0))
        self.assertEqual(runtime.queue.maxsize, 4)
        self.assertEqual(runtime.workers, 3)
        self.assertEqual(runtime.max_body_bytes, 16)
        self.assertEqual(
            (
                runtime.read_header_timeout,
                runtime.read_timeout,
                runtime.write_timeout,
                runtime.idle_timeout,
            ),
            (7, 11, 13, 17),
        )

    def test_new_runtime_installs_enqueuer(self) -> None:
        runtime = self._runtime(AtrinisHTTPConfig())
        enqueuer = runtime.service.enqueue_llm
        self.assertIs(getattr(enqueuer, "__self__", None), runtime)
        self.assertIs(getattr(enqueuer, "__func__", None), Runtime.enqueue_llm)

    def test_enqueue_full_raises(self) -> None:
        runtime = self._runtime(AtrinisHTTPConfig(queue_size=1))
        runtime.enqueue_llm(None, "first")
        from atri_x.errors import QueueFullError

        with self.assertRaises(QueueFullError):
            runtime.enqueue_llm(None, "second")

    def test_enqueue_respects_cancelled_ctx(self) -> None:
        from atri_x.safecontext import CancelledError, with_cancel

        runtime = self._runtime(AtrinisHTTPConfig(queue_size=4))
        token = with_cancel(None)
        token.cancel("test")
        with self.assertRaises(CancelledError):
            runtime.enqueue_llm(token, "x")


class TestHTTPEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.case_dir = os.path.join(TMP_ROOT, "http_" + self._testMethodName)
        shutil.rmtree(self.case_dir, ignore_errors=True)
        os.makedirs(os.path.join(self.case_dir, "sandbox"), exist_ok=True)
        self.store = sqlite.new(os.path.join(self.case_dir, "test.db"))

        async def _send(ctx, target, outputs):  # noqa: ANN001
            return Receipt()

        self.service = Service(
            Options(
                config=AtrinisConfig(
                    enabled=True,
                    atriwisps={},
                ),
                store=self.store,
                tokens={"home": TOKEN},
                sandbox_root=os.path.join(self.case_dir, "sandbox"),
                enabled_platforms=["cli"],
                send=_send,
            )
        )
        self.runtime = Runtime(
            AtrinisHTTPConfig(addr="127.0.0.1:0", queue_size=8, workers=1), self.service
        )
        self.runner_task = asyncio.create_task(self.runtime.run())
        deadline = time.monotonic() + 10
        while not self.runtime.is_running():
            if self.runner_task.done():
                # 启动失败（例如端口占用）时立刻把真实异常暴露出来，不要傻等。
                await self.runner_task
                raise AssertionError("atrinis runtime stopped before binding")
            if time.monotonic() > deadline:
                raise AssertionError("atrinis runtime did not start")
            await asyncio.sleep(0.01)
        self.base = f"http://127.0.0.1:{self.runtime.port()}"
        self.client = HTTPClient(base_url=self.base, timeout=10)

    async def asyncTearDown(self) -> None:
        self.runner_task.cancel()
        try:
            await self.runner_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self.store.close()
        shutil.rmtree(self.case_dir, ignore_errors=True)

    async def test_healthz(self) -> None:
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"ok": True})
        head = await self.client.request("POST", "/healthz")
        self.assertEqual(head.status, 200)

    async def test_accepts_single_json_and_ignores_unknown_fields(self) -> None:
        body = valid_http_event("unknown-field")
        body["future_field"] = {"enabled": True}
        raw = json.dumps(body).encode("utf-8") + b"\n \t"
        response = await self.client.post(
            "/atriena/v2/events",
            body=raw,
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 202, response.text())
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "completed")

    async def test_v3_route_and_token_header_fallback(self) -> None:
        response = await self.client.post(
            "/atriena/v3/events",
            json=valid_http_event("v3-event"),
            headers={"X-Atrinis-Token": TOKEN},
        )
        self.assertEqual(response.status, 202, response.text())

    async def test_unauthorized(self) -> None:
        response = await self.client.post("/atriena/v2/events", json=valid_http_event("no-token"))
        self.assertEqual(response.status, 401)
        payload = response.json()
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["error"], UNAUTHORIZED_MESSAGE)

    async def test_method_not_allowed(self) -> None:
        response = await self.client.get("/atriena/v2/events")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.json()["error"], "method not allowed")

    async def test_unknown_path(self) -> None:
        response = await self.client.get("/nope")
        self.assertEqual(response.status, 404)

    async def test_body_limit(self) -> None:
        self.runtime.max_body_bytes = 16
        response = await self.client.post(
            "/atriena/v2/events",
            body=json.dumps(valid_http_event("oversized")).encode("utf-8"),
            headers={"Authorization": "Bearer " + TOKEN},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("request body too large", response.text())

    async def test_trailing_json_rejected(self) -> None:
        raw = json.dumps(valid_http_event("trailing")).encode("utf-8") + b" {}"
        response = await self.client.post(
            "/atriena/v2/events", body=raw, headers={"Authorization": "Bearer " + TOKEN}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("invalid json", response.text())

    async def test_invalid_json_body(self) -> None:
        response = await self.client.post(
            "/atriena/v2/events", body=b"{oops", headers={"Authorization": "Bearer " + TOKEN}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("invalid json", response.text())

    async def test_bad_request_reports_validation_error(self) -> None:
        body = valid_http_event("bad-mode")
        body["mode"] = "telepathy"
        response = await self.client.post(
            "/atriena/v2/events", json=body, headers={"Authorization": "Bearer " + TOKEN}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("unsupported mode", response.json()["error"])

    async def test_duplicate_returns_202_and_duplicate_flag(self) -> None:
        for _ in range(2):
            response = await self.client.post(
                "/atriena/v2/events",
                json=valid_http_event("dup-event"),
                headers={"Authorization": "Bearer " + TOKEN},
            )
            self.assertEqual(response.status, 202, response.text())
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(response.json()["status"], "duplicate")

    async def test_llm_event_is_queued_and_executed(self) -> None:
        body = valid_http_event("llm-event")
        body["mode"] = "llm"
        body["model_slot"] = "atriwisp1"
        response = await self.client.post(
            "/atriena/v2/events", json=body, headers={"Authorization": "Bearer " + TOKEN}
        )
        self.assertEqual(response.status, 202, response.text())
        self.assertEqual(response.json()["status"], "queued")
        # worker 会立即取走并尝试执行；runner 未配置 → 事件被写成 failed。
        deadline = time.monotonic() + 10
        record = self.store.atrinis_events().get_by_key("watcher", "http-test", "llm-event")
        while time.monotonic() < deadline:
            record = self.store.atrinis_events().get_by_key("watcher", "http-test", "llm-event")
            if record.status == "failed":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(record.status, "failed")
        self.assertIn("background runner is not configured", record.error)


class TestRequestFromBody(unittest.TestCase):
    def test_request_from_http_body(self) -> None:
        request = Request.from_json(valid_http_event("x"))
        self.assertEqual(request.atriwisp.name, "watcher")
        self.assertEqual(request.mode, ModeRecord)
        self.assertEqual([t.platform for t in request.targets or []], ["cli"])
        # 反向：to_json 仍是 Go 的 tag 形状。
        payload = request.to_json()
        self.assertEqual(payload["atriwisp"], {"name": "watcher", "tags": ["test"]})
        self.assertEqual(payload["targets"], [{"platform": "cli"}])

    def test_request_equality_of_aliases(self) -> None:
        # atrinis.types 只是 atriena 的再导出：必须是同一个类。
        from atri_x.atrinis.types import Request as AtrienisRequest

        self.assertIs(AtrienisRequest, Request)
        self.assertIsInstance(Request(atriwisp=Atriwisp(name="w")), Request)
        self.assertIsInstance(Target(platform="cli"), Target)


if __name__ == "__main__":
    unittest.main()
