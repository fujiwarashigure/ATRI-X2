"""模型目录用例：``/models`` 拉取、备注名、本地缓存文件、密钥取值。

这里测的是「模型」页与「进阶」页生图区的**数据层**（:mod:`atri_x.gui.modelcatalog` 与
:mod:`atri_x.gui.configmodel`），不建 Tk 窗口：拉取用本机临时 HTTP 服务，走真实
``urllib`` 路径，不访问外网。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from atri_x.gui import configedit, configmodel, modelcatalog

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_modelcatalog"


class _Handler(BaseHTTPRequestHandler):
    """按路由表返回 JSON；顺手记下请求路径与 Authorization 头。"""

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
    """在线程里跑一个临时 HTTP 服务。"""

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


def _models_payload(*models: str) -> bytes:
    return json.dumps({"data": [{"id": item} for item in models]}).encode("utf-8")


class CatalogCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


class StorageCase(CatalogCase):
    """``gui/model_catalog.json`` 的读写与备注名。"""

    def test_missing_file_is_empty_catalog(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        self.assertEqual(catalog.models("providers.deepseek"), [])
        self.assertEqual(catalog.labels("providers.deepseek"), [])

    def test_record_and_roundtrip(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        added = catalog.record(
            "providers.deepseek", ["deepseek-chat", "deepseek-reasoner"], source="https://x"
        )
        self.assertEqual(added, ["deepseek-chat", "deepseek-reasoner"])
        path = catalog.save(str(self.config_dir))
        self.assertEqual(path, modelcatalog.catalog_path(str(self.config_dir)))
        reloaded = modelcatalog.load(str(self.config_dir))
        self.assertEqual(
            reloaded.models("providers.deepseek"), ["deepseek-chat", "deepseek-reasoner"]
        )
        self.assertTrue(reloaded.entry("providers.deepseek").fetched_at)

    def test_merge_keeps_order_and_dedups(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        key = "providers.relay"
        catalog.record(key, ["m2", "m1"])
        added = catalog.record(key, ["m1", "m3", "m3"])
        self.assertEqual(added, ["m3"])
        self.assertEqual(catalog.models(key), ["m2", "m1", "m3"])

    def test_alias_roundtrip_and_labels(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        key = "providers.deepseek"
        catalog.record(key, ["deepseek-chat"])
        catalog.set_alias(key, "deepseek-chat", "主力")
        catalog.save(str(self.config_dir))
        reloaded = modelcatalog.load(str(self.config_dir))
        self.assertEqual(reloaded.alias_of(key, "deepseek-chat"), "主力")
        labels = reloaded.labels(key)
        self.assertEqual(labels, ["主力（deepseek-chat）"])
        self.assertEqual(
            modelcatalog.model_of_label(labels[0], reloaded.names(key)), "deepseek-chat"
        )
        # 清空备注名 = 删除该条
        reloaded.set_alias(key, "deepseek-chat", "")
        self.assertEqual(reloaded.alias_of(key, "deepseek-chat"), "")
        self.assertEqual(reloaded.labels(key), ["deepseek-chat"])

    def test_labels_include_configured_models(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        catalog.record("providers.deepseek", ["a"])
        labels = catalog.labels("providers.deepseek", ["a", "hand-written"])
        self.assertEqual(labels, ["a", "hand-written"])

    def test_alias_requires_model(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        with self.assertRaises(modelcatalog.CatalogError):
            catalog.set_alias("providers.deepseek", "", "x")

    def test_corrupt_file_degrades(self) -> None:
        path = Path(modelcatalog.catalog_path(str(self.config_dir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(modelcatalog.load(str(self.config_dir)).entries, {})

    def test_alias_is_truncated(self) -> None:
        catalog = modelcatalog.load(str(self.config_dir))
        catalog.set_alias("providers.x", "m", "备" * 100)
        self.assertEqual(len(catalog.alias_of("providers.x", "m")), modelcatalog.MAX_ALIAS_RUNES)


class ParseCase(unittest.TestCase):
    """``parse_models`` 吃三种常见返回结构。"""

    def test_openai_data_list(self) -> None:
        self.assertEqual(
            modelcatalog.parse_models({"data": [{"id": "a"}, {"id": "b"}]}), ["a", "b"]
        )

    def test_models_list_and_plain_items(self) -> None:
        self.assertEqual(modelcatalog.parse_models({"models": ["m1", {"name": "m2"}]}), ["m1", "m2"])

    def test_bare_list_and_dedup(self) -> None:
        self.assertEqual(modelcatalog.parse_models(["a", "a", " b ", {"model": "c"}]), ["a", "b", "c"])

    def test_garbage_is_empty(self) -> None:
        self.assertEqual(modelcatalog.parse_models({"foo": 1}), [])
        self.assertEqual(modelcatalog.parse_models("nope"), [])

    def test_limit(self) -> None:
        models = modelcatalog.parse_models({"data": [{"id": f"m{index}"} for index in range(900)]})
        self.assertEqual(len(models), modelcatalog.MAX_MODELS)


class FetchCase(CatalogCase):
    """``fetch_models`` 走真实 HTTP（本机临时服务）。"""

    def test_fetches_models_and_sends_bearer(self) -> None:
        with _Server({"/models": (200, _models_payload("deepseek-chat", "deepseek-reasoner"))}) as server:
            models = modelcatalog.fetch_models(server.base_url, "sk-test", timeout=5)
            seen = list(server.seen)
        self.assertEqual(models, ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(seen[0][0], "/models")
        self.assertEqual(seen[0][1], "Bearer sk-test")

    def test_falls_back_to_v1_path(self) -> None:
        routes = {
            "/v1/models": (404, b"{}"),
            "/models": (200, _models_payload("m-v1-missing")),
        }
        with _Server(routes) as server:
            models = modelcatalog.fetch_models(server.base_url + "/v1", timeout=5)
        # base 以 /v1 结尾时先试 {base}/models，再回退到去掉 /v1 的 /models
        self.assertEqual(models, ["m-v1-missing"])

    def test_auth_error_is_explained(self) -> None:
        with _Server({"/models": (401, b"{}")}) as server:
            with self.assertRaises(modelcatalog.CatalogError) as caught:
                modelcatalog.fetch_models(server.base_url, "bad", timeout=5)
        text = str(caught.exception)
        self.assertIn("鉴权失败", text)

    def test_404_mentions_v1(self) -> None:
        with _Server({"/models": (404, b"{}")}) as server:
            with self.assertRaises(modelcatalog.CatalogError) as caught:
                modelcatalog.fetch_models(server.base_url, timeout=5)
        self.assertIn("/v1", str(caught.exception))

    def test_unreachable_host_is_explained(self) -> None:
        with _Server({"/models": (200, _models_payload("x"))}) as server:
            base = server.base_url
        # 服务已关闭：连接应失败而不是抛原始异常
        with self.assertRaises(modelcatalog.CatalogError):
            modelcatalog.fetch_models(base, timeout=2)

    def test_empty_base_url_rejected(self) -> None:
        with self.assertRaises(modelcatalog.CatalogError):
            modelcatalog.fetch_models("   ")


class SecretValueCase(CatalogCase):
    """``secret_value``：进程环境优先，其次 ``.env``（拉模型要真密钥）。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.config_dir / "app.toml"
        self.app.write_text("[config_files]\n", encoding="utf-8")
        self.paths = configedit.ConfigPaths.from_config(str(self.app))

    def test_reads_dotenv(self) -> None:
        configedit.set_env_value(self.paths.env_path, "MY_KEY", "sk-dotenv")
        self.assertEqual(configmodel.secret_value(self.paths, "MY_KEY"), "sk-dotenv")

    def test_process_env_wins(self) -> None:
        configedit.set_env_value(self.paths.env_path, "MY_KEY", "sk-dotenv")
        old = os.environ.get("MY_KEY")
        os.environ["MY_KEY"] = "sk-process"
        try:
            self.assertEqual(configmodel.secret_value(self.paths, "MY_KEY"), "sk-process")
        finally:
            if old is None:
                os.environ.pop("MY_KEY", None)
            else:
                os.environ["MY_KEY"] = old

    def test_missing_returns_empty(self) -> None:
        self.assertEqual(configmodel.secret_value(self.paths, ""), "")
        self.assertEqual(configmodel.secret_value(self.paths, "NOPE"), "")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
