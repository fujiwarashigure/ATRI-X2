"""本地生图服务挂载用例：地址拼装 + 只读探测。

不建 Tk、不访问外网：探测用本机临时 HTTP 服务，走真实 ``urllib`` 路径。
覆盖「端口映射 / SSL 链接 / 外部 API」三种挂载方式与 OpenAI 兼容 / ComfyUI 原生 /
需要鉴权 / 连不上 四类探测结果。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from atri_x.gui import imagemount

# ---------------------------------------------------------------------------
# 临时 HTTP 服务
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        server = self.server
        server.seen.append((self.path, self.headers.get("Authorization", "")))  # type: ignore[attr-defined]
        status, body = server.routes.get(self.path, (404, b'{"error":"not found"}'))  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # pragma: no cover - 静音
        return


class _Server:
    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.routes = routes  # type: ignore[attr-defined]
        self.httpd.seen = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def seen(self) -> list[tuple[str, str]]:
        return self.httpd.seen  # type: ignore[attr-defined]


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# 地址拼装
# ---------------------------------------------------------------------------
class BuildUrlCase(unittest.TestCase):
    def test_port_mapping_defaults(self) -> None:
        spec = imagemount.MountSpec(mode=imagemount.MOUNT_PORT)
        self.assertEqual(
            imagemount.build_base_url(spec), "http://127.0.0.1:8188/v1"
        )

    def test_port_mapping_custom_host_port_and_prefix(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_PORT, host="192.168.1.9", port=7860, prefix="v1/"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "http://192.168.1.9:7860/v1"
        )

    def test_port_mapping_without_prefix(self) -> None:
        spec = imagemount.MountSpec(mode=imagemount.MOUNT_PORT, prefix="")
        self.assertEqual(imagemount.build_base_url(spec), "http://127.0.0.1:8188")

    def test_ssl_link_gets_prefix(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_SSL, url="https://abc-8188.gpuhub.com"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "https://abc-8188.gpuhub.com/v1"
        )

    def test_ssl_link_does_not_duplicate_v1(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_SSL, url="https://abc-8188.gpuhub.com/v1/"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "https://abc-8188.gpuhub.com/v1"
        )

    def test_ssl_link_with_path(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_SSL, url="https://host.example/openai"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "https://host.example/openai/v1"
        )

    def test_external_url_is_kept_and_prefix_added(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_EXTERNAL, url="https://relay.example.com/"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "https://relay.example.com/v1"
        )

    def test_external_url_with_v1_not_duplicated(self) -> None:
        spec = imagemount.MountSpec(
            mode=imagemount.MOUNT_EXTERNAL, url="https://relay.example.com/v1"
        )
        self.assertEqual(
            imagemount.build_base_url(spec), "https://relay.example.com/v1"
        )

    def test_invalid_inputs_raise(self) -> None:
        bad_specs = (
            imagemount.MountSpec(mode=imagemount.MOUNT_EXTERNAL, url=""),
            imagemount.MountSpec(mode=imagemount.MOUNT_PORT, host="http://1.2.3.4"),
            imagemount.MountSpec(mode=imagemount.MOUNT_PORT, port=70000),
            imagemount.MountSpec(mode=imagemount.MOUNT_SSL, url=""),
            imagemount.MountSpec(mode=imagemount.MOUNT_SSL, url="ftp://x/y"),
            imagemount.MountSpec(mode="weird", url="https://x"),
        )
        for spec in bad_specs:
            with self.subTest(spec=spec), self.assertRaises(imagemount.ImageMountError):
                imagemount.build_base_url(spec)

    def test_normalize_prefix(self) -> None:
        self.assertEqual(imagemount.normalize_prefix("v1"), "/v1")
        self.assertEqual(imagemount.normalize_prefix("/v1/"), "/v1")
        self.assertEqual(imagemount.normalize_prefix("  "), "")
        self.assertEqual(imagemount.normalize_prefix("/"), "")

    def test_candidate_roots(self) -> None:
        self.assertEqual(
            imagemount.candidate_roots("http://127.0.0.1:8188/v1/"),
            ["http://127.0.0.1:8188/v1", "http://127.0.0.1:8188"],
        )
        self.assertEqual(
            imagemount.candidate_roots("https://x.example/v1/models"),
            ["https://x.example/v1", "https://x.example"],
        )

    def test_spec_describe(self) -> None:
        self.assertIn(
            "端口映射", imagemount.MountSpec(mode=imagemount.MOUNT_PORT).describe()
        )
        self.assertIn(
            "SSL 链接",
            imagemount.MountSpec(mode=imagemount.MOUNT_SSL, url="https://x").describe(),
        )


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------
class ProbeCase(unittest.TestCase):
    def test_openai_compatible_models(self) -> None:
        routes = {
            "/v1/models": (
                200,
                _json({"object": "list", "data": [{"id": "img-a"}, {"id": "img-b"}]}),
            )
        }
        with _Server(routes) as server:
            result = imagemount.probe(server.base_url + "/v1", "sk-test", timeout=5)
            self.assertTrue(result.ok)
            self.assertEqual(result.kind, imagemount.KIND_OPENAI)
            self.assertEqual(result.models, ("img-a", "img-b"))
            self.assertTrue(result.openai_compatible)
            self.assertEqual(server.seen[0][1], "Bearer sk-test")
        self.assertIn("2 个模型", imagemount.describe(result))

    def test_openai_models_at_root_without_v1(self) -> None:
        routes = {"/models": (200, _json({"data": [{"id": "m1"}]}))}
        with _Server(routes) as server:
            result = imagemount.probe(server.base_url + "/v1", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, imagemount.KIND_OPENAI)

    def test_auth_required(self) -> None:
        routes = {"/v1/models": (401, b'{"error":"unauthorized"}')}
        with _Server(routes) as server:
            result = imagemount.probe(server.base_url + "/v1", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, imagemount.KIND_AUTH)
        self.assertIn("鉴权", result.detail)
        self.assertTrue(result.openai_compatible)

    def test_comfyui_native_detected(self) -> None:
        routes = {
            "/system_stats": (200, _json({"system": {"comfyui_version": "0.3"}, "devices": []}))
        }
        with _Server(routes) as server:
            result = imagemount.probe(server.base_url + "/v1", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, imagemount.KIND_COMFY)
        self.assertFalse(result.openai_compatible)
        self.assertIn("ComfyUI", imagemount.describe(result))
        self.assertIn("OpenAI 兼容", result.detail)

    def test_comfyui_queue_fallback(self) -> None:
        routes = {"/queue": (200, _json({"queue_running": [], "queue_remaining": []}))}
        with _Server(routes) as server:
            result = imagemount.probe(server.base_url, timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, imagemount.KIND_COMFY)

    def test_unknown_service(self) -> None:
        with _Server({}) as server:
            result = imagemount.probe(server.base_url + "/v1", timeout=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, imagemount.KIND_UNKNOWN)
        self.assertIn("没有检测到", imagemount.describe(result))

    def test_unreachable_service(self) -> None:
        with _Server({}) as server:
            dead = server.base_url
        result = imagemount.probe(dead, timeout=2)
        self.assertFalse(result.ok)
        self.assertIn("检测失败", imagemount.describe(result))

    def test_empty_url(self) -> None:
        result = imagemount.probe("")
        self.assertFalse(result.ok)
        self.assertIn("先填", result.detail)


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
