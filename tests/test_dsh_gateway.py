"""``atri_x.agent.dsh.gateway`` 的离线测试：本地头代理 + 凭据计划 + 运行时接线。

覆盖四层：

1. :class:`atri_x.agent.dsh.gateway.HeaderProxy` —— 路径/请求头/请求体转发、流式
   （chunked）透传、上游错误状态透传、上游不可达转 502、重复启动幂等、关停后端口释放；
2. :func:`atri_x.agent.dsh.gateway.sanitize_headers` 与 :class:`GatewaySpec` 校验；
3. :func:`atri_x.agent.dsh.credentials.resolve_credential_plan` —— provider 声明了
   ``extra_headers`` 时产出网关规格（值不外显）；
4. :class:`atri_x.agent.dsh.runtime.DshRuntime` —— 内核 ``DEEPSEEK_BASE_URL`` 被替换成
   本地代理地址，且该地址真能把请求（带注入头）送到上游。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atri_x.agent.dsh.credentials import resolve_credential_plan
from atri_x.agent.dsh.gateway import (
    DEFAULT_ANTHROPIC_BASE_URL,
    GatewaySpec,
    HeaderProxy,
    sanitize_headers,
)
from atri_x.agent.dsh.router import DshRouteConfig
from atri_x.agent.dsh.runtime import DshRuntime
from atri_x.agent.dsh.workspace import WorkspacePaths
from atri_x.storage import SessionModeWork

TMP_ROOT = Path(__file__).resolve().parent / "_tmp_dsh_gateway"


def _port_of(url: str) -> int:
    """从 ``http://127.0.0.1:<port>`` 取端口。"""
    return int(url.rsplit(":", 1)[1])


class _Upstream:
    """本地假上游：记录收到的请求，按模式回 JSON / SSE / 错误。"""

    def __init__(self, mode: str = "json") -> None:
        self.records: list[dict[str, Any]] = []
        self.mode = mode
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
                pass

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.records.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if upstream.mode == "error":
                    payload = b'{"type":"error","error":{"type":"boom","message":"upstream"}}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if upstream.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for part in (b"event: a\ndata: 1\n\n", b"event: b\ndata: 2\n\n"):
                        self.wfile.write(part)
                        self.wfile.flush()
                        time.sleep(0.01)
                    self.close_connection = True
                    return
                payload = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def _request(url: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
    """向本地代理发一次请求，返回 ``(status, headers, body)``。"""
    connection = http.client.HTTPConnection("127.0.0.1", _port_of(url), timeout=10)
    try:
        connection.request("POST", path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class GatewayProxyTest(unittest.TestCase):
    """头代理的转发语义（全部在本机回环上跑，不访问外网）。"""

    def setUp(self) -> None:
        self.upstream = _Upstream()
        self.proxy = HeaderProxy(
            GatewaySpec(
                upstream=self.upstream.base + "/anthropic",
                headers={"x-opencode-session": "sess-test", "x-extra": "v1"},
            )
        )
        self.url = self.proxy.start()

    def tearDown(self) -> None:
        self.proxy.close()
        self.upstream.close()

    def test_forwards_path_headers_and_body(self) -> None:
        status, _, body = _request(
            self.url,
            "/v1/messages?beta=1",
            body=b'{"model":"deepseek-v4-pro"}',
            headers={"x-api-key": "key-123", "content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["ok"], True)
        self.assertEqual(len(self.upstream.records), 1)
        record = self.upstream.records[0]
        self.assertEqual(record["path"], "/anthropic/v1/messages?beta=1")
        self.assertEqual(record["body"], b'{"model":"deepseek-v4-pro"}')
        self.assertEqual(record["headers"]["x-opencode-session"], "sess-test")
        self.assertEqual(record["headers"]["x-extra"], "v1")
        self.assertEqual(record["headers"]["x-api-key"], "key-123")
        self.assertEqual(record["headers"]["host"], f"127.0.0.1:{self.upstream.port}")

    def test_streaming_response_is_passed_through(self) -> None:
        self.upstream.mode = "stream"
        status, headers, body = _request(self.url, "/v1/messages")
        self.assertEqual(status, 200)
        self.assertIn("event-stream", headers.get("Content-Type", ""))
        self.assertEqual(body, b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\n")

    def test_upstream_error_status_is_passed_through(self) -> None:
        self.upstream.mode = "error"
        status, headers, body = _request(self.url, "/v1/messages")
        self.assertEqual(status, 400)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["type"], "boom")

    def test_describe_reports_names_not_values(self) -> None:
        snapshot = json.dumps(self.proxy.describe(), ensure_ascii=False)
        self.assertTrue(self.proxy.describe()["running"])
        self.assertIn("x-opencode-session", snapshot)
        self.assertNotIn("sess-test", snapshot)
        self.assertNotIn('"v1"', snapshot)

    def test_start_is_idempotent_and_close_releases_port(self) -> None:
        self.assertEqual(self.proxy.start(), self.url)
        self.proxy.close()
        self.assertFalse(self.proxy.running)
        with self.assertRaises(OSError):
            _request(self.url, "/v1/messages")


class GatewayFailureTest(unittest.TestCase):
    """代理自身故障的降级行为。"""

    def test_unreachable_upstream_returns_502(self) -> None:
        proxy = HeaderProxy(GatewaySpec(upstream="http://127.0.0.1:9", headers={"a": "b"}))
        url = proxy.start()
        try:
            status, _, body = _request(url, "/v1/messages", body=b"{}")
        finally:
            proxy.close()
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"]["type"], "gateway_proxy_error")

    def test_spec_rejects_bad_upstreams(self) -> None:
        for upstream in (
            "ftp://example.com",
            "https://example.com/path?x=1",
            "https://example.com/path#frag",
            "https://user:pw@example.com",
            "",
        ):
            with self.subTest(upstream=upstream):
                with self.assertRaises(ValueError):
                    GatewaySpec(upstream=upstream)

    def test_sanitize_headers_drops_reserved_and_blank(self) -> None:
        cleaned = sanitize_headers(
            {
                "x-opencode-session": " s ",
                "Host": "evil",
                "Content-Length": "1",
                "Connection": "close",
                "empty": "   ",
                "": "x",
            }
        )
        self.assertEqual(cleaned, {"x-opencode-session": "s"})


class CredentialGatewayTest(unittest.TestCase):
    """凭据计划：provider 的 ``extra_headers`` 决定是否走本地代理。"""

    def _agent(self, provider: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            providers={"gw": provider},
            mode_models={SessionModeWork: SimpleNamespace(provider="gw")},
        )

    def test_anthropic_gateway_with_headers_gets_proxy_spec(self) -> None:
        agent = self._agent(
            SimpleNamespace(
                api_key="oc-key",
                base_url="https://opencode.ai/inference/go/anthropic",
                extra_headers={"x-opencode-session": "session-secret"},
            )
        )
        plan = resolve_credential_plan(agent, DshRouteConfig(credential_provider="gw"))
        self.assertTrue(plan.usable)
        self.assertIsNotNone(plan.gateway_spec)
        assert plan.gateway_spec is not None
        self.assertEqual(plan.gateway_spec.upstream, "https://opencode.ai/inference/go/anthropic")
        self.assertEqual(plan.gateway_spec.header_names, ["x-opencode-session"])
        # 环境里仍然下发上游 base_url（运行时再覆盖成代理地址）。
        self.assertEqual(
            plan.env["DEEPSEEK_BASE_URL"], "https://opencode.ai/inference/go/anthropic"
        )
        snapshot = json.dumps(plan.to_dict(), ensure_ascii=False)
        self.assertNotIn("session-secret", snapshot)
        self.assertNotIn("oc-key", snapshot)
        self.assertTrue(plan.to_dict()["gateway"])

    def test_official_deepseek_stays_direct(self) -> None:
        agent = self._agent(
            SimpleNamespace(api_key="sk", base_url="https://api.deepseek.com", extra_headers={})
        )
        plan = resolve_credential_plan(agent, DshRouteConfig(credential_provider="gw"))
        self.assertTrue(plan.usable)
        self.assertIsNone(plan.gateway_spec)
        self.assertFalse(plan.to_dict()["gateway"])

    def test_openai_gateway_with_headers_hints_anthropic_path(self) -> None:
        agent = self._agent(
            SimpleNamespace(
                api_key="oc-key",
                base_url="https://opencode.ai/inference/go/openai/v1",
                extra_headers={"x-opencode-session": "s"},
            )
        )
        plan = resolve_credential_plan(agent, DshRouteConfig(credential_provider="gw"))
        self.assertFalse(plan.usable)
        self.assertIn("extra_headers", plan.reason)
        self.assertIn("anthropic", plan.reason)

    def test_default_upstream_when_provider_has_no_base_url(self) -> None:
        agent = SimpleNamespace(
            providers={
                "deepseek": SimpleNamespace(
                    api_key="sk", base_url="", extra_headers={"x-gw": "1"}
                )
            },
            mode_models={},
        )
        plan = resolve_credential_plan(agent, DshRouteConfig())
        self.assertTrue(plan.usable)
        assert plan.gateway_spec is not None
        self.assertEqual(plan.gateway_spec.upstream, DEFAULT_ANTHROPIC_BASE_URL)


class RuntimeGatewayWiringTest(unittest.TestCase):
    """运行时把内核的 base_url 指到代理，且该地址真能把请求送到上游。"""

    def test_ensure_client_uses_proxy_url(self) -> None:
        upstream = _Upstream()
        proxy = HeaderProxy(
            GatewaySpec(
                upstream=upstream.base + "/anthropic",
                headers={"x-opencode-session": "sess-rt"},
            )
        )
        calls: list[dict[str, Any]] = []

        def factory(command: object, **kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(alive=True, start=lambda: None, stop=lambda: None)

        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        runtime = DshRuntime(
            DshRouteConfig(credential_base_url_env="DEEPSEEK_BASE_URL"),
            command="fake-dsh",
            paths=WorkspacePaths(
                workspace=str(TMP_ROOT / "gw-ws"), home=str(TMP_ROOT / "gw-home")
            ),
            client_factory=factory,
            extra_env={
                "DEEPSEEK_API_KEY": "oc-key",
                "DEEPSEEK_BASE_URL": "https://opencode.ai/inference/go/anthropic",
            },
            gateway=proxy,
        )
        try:
            runtime._ensure_client()
            self.assertEqual(len(calls), 1)
            env = calls[0]["env"]
            assert isinstance(env, dict)
            proxy_url = env["DEEPSEEK_BASE_URL"]
            self.assertTrue(proxy_url.startswith("http://127.0.0.1:"))
            self.assertEqual(env["DEEPSEEK_API_KEY"], "oc-key")
            self.assertTrue(runtime.describe()["gateway"]["running"])
            # 内核真实会打的是 <proxy>/v1/messages：验证代理补头并送到上游。
            status, _, _ = _request(
                proxy_url,
                "/v1/messages",
                body=b'{"model":"deepseek-v4-pro"}',
                headers={"x-api-key": "oc-key", "content-type": "application/json"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(upstream.records[-1]["path"], "/anthropic/v1/messages")
            self.assertEqual(
                upstream.records[-1]["headers"]["x-opencode-session"], "sess-rt"
            )
        finally:
            runtime.close()
            upstream.close()
        self.assertFalse(proxy.running)


if __name__ == "__main__":
    unittest.main()
