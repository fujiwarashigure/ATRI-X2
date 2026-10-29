"""「在线」选项卡的数据层与界面用例。

分两层：

- **纯逻辑**（无 Tk）：``/healthz`` 解析、健康接口客户端（真起一个本机 HTTP 服务）、
  协议端 TCP 探测、``app.toml`` 的平台开关读写、人话格式化；
- **界面**（有 Tk 才跑）：选项卡构建、按钮文案随配置变化、渲染（不阻塞 Tk 的部分）、
  连接开关真的落盘、复制连接信息。

这台机器上 ``ATRIX_HEALTH_ADDR`` / ``ATRIX_OPS_TOKEN`` 是桌面端自己写进 ``.env`` 的，
所以 :func:`atri_x.gui.service.ensure_health_endpoint` 的用例重点验证三件事：
**幂等**、**已有值不动**、**端口被占时顺延**。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import tkinter as tk
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import ttk
from typing import Any
from unittest import mock

from atri_x.gui import linkstate, service
from atri_x.gui.configedit import read_toml
from atri_x.gui.tabs.link import LinkTab

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_link"

#: 一份最小的 app.toml（``[platform.qqonebot]`` 默认是注释掉的，和发行资产一致）。
SAMPLE_APP_TOML = """\
# ATRI-X 配置示例（测试用）
[platform]
platforms = ["cli"]

[platform.cli]
enabled = true
default_client = "local"
default_url = "ws://127.0.0.1:32172/cli/v1/ws"

[platform.cli.server]
enabled = false
listen = "127.0.0.1:32172"

[platform.cli.server.tokens]
local = ["ATRIX_CLI_LOCAL_TOKEN"]

# [platform.qqonebot]
# enabled = false
# ws_url = "ws://127.0.0.1:3001/"
# access_token_env = "QQONEBOT_ACCESS_TOKEN"
"""

#: 一份健康快照（形状与 ``atri_x.health.server.marshal_snapshot`` 一致）。
HEALTH_BODY: dict[str, Any] = {
    "status": "ok",
    "live": True,
    "ready": True,
    "degraded": False,
    "version": "0.9.9",
    "started_at": "2026-10-20T06:00:00Z",
    "uptime_seconds": 3725,
    "messages_ok": 12,
    "messages_failed": 1,
    "last_message_at": "2026-10-20T07:00:00Z",
    "checks": {"process": "ready", "scheduler": "ok"},
    "models": [{"provider": "deepseek", "status": "up"}],
    "platforms": [
        {
            "name": "qqonebot",
            "connected": True,
            "connect_count": 2,
            "last_connected_at": "2026-10-20T06:30:00Z",
        }
    ],
}


def _tmp_root() -> Path:
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TEMP_ROOT


def _write_config() -> Path:
    root = _tmp_root()
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "app.toml"
    config_path.write_text(SAMPLE_APP_TOML, encoding="utf-8", newline="\n")
    return config_path


class _HealthHandler(BaseHTTPRequestHandler):
    """最小 ``/healthz`` 服务端：校验 Bearer，按类属性回状态码与 JSON。"""

    token = "ops-token"
    status = 200
    body = b"{}"
    seen_authorization = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        _HealthHandler.seen_authorization = self.headers.get("Authorization", "")
        if self.path != linkstate.HEALTH_PATH:
            self._reply(404, {"error": "not found"})
            return
        if _HealthHandler.seen_authorization != f"Bearer {_HealthHandler.token}":
            self._reply(401, {"error": "unauthorized"})
            return
        self._reply(_HealthHandler.status, None, raw=_HealthHandler.body)

    def _reply(self, status: int, body: Any, *, raw: bytes | None = None) -> None:
        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # 静音
        return


class HealthServerCase(unittest.TestCase):
    """起一个真的本机 HTTP 服务，验证客户端的行为（含 401 / 503 / 连不上）。"""

    def setUp(self) -> None:
        _HealthHandler.token = "ops-token"
        _HealthHandler.status = 200
        _HealthHandler.body = json.dumps(HEALTH_BODY).encode("utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addr = f"127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5.0)
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_fetch_ok_and_header(self) -> None:
        report = linkstate.fetch_health(self.addr, "ops-token", timeout=3.0)
        self.assertEqual(_HealthHandler.seen_authorization, "Bearer ops-token")
        self.assertTrue(report.ready)
        self.assertEqual(report.version, "0.9.9")
        self.assertEqual(report.messages_ok, 12)
        entry = report.platform("qqonebot")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.connected)
        self.assertEqual(entry.connect_count, 2)
        self.assertEqual(report.models[0].provider, "deepseek")

    def test_wrong_token_reports_auth_failure(self) -> None:
        with self.assertRaises(linkstate.LinkError) as caught:
            linkstate.fetch_health(self.addr, "wrong", timeout=3.0)
        self.assertIn("鉴权", str(caught.exception))

    def test_503_body_is_still_parsed(self) -> None:
        _HealthHandler.status = 503
        _HealthHandler.body = json.dumps(
            {"status": "not_ready", "live": True, "ready": False}
        ).encode("utf-8")
        report = linkstate.fetch_health(self.addr, "ops-token", timeout=3.0)
        self.assertEqual(report.http_status, 503)
        self.assertEqual(report.status, "not_ready")
        self.assertFalse(report.ready)

    def test_unreachable_address(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(linkstate.LinkError) as caught:
            linkstate.fetch_health(f"127.0.0.1:{port}", "ops-token", timeout=1.0)
        self.assertIn("不可达", str(caught.exception))


class ParseCase(unittest.TestCase):
    """纯函数：解析 / 地址换算 / 格式化。"""

    def test_health_endpoint(self) -> None:
        self.assertEqual(
            linkstate.health_endpoint("127.0.0.1:32173"),
            "http://127.0.0.1:32173/healthz",
        )
        self.assertEqual(
            linkstate.health_endpoint("0.0.0.0:32173"),
            "http://127.0.0.1:32173/healthz",
        )
        with self.assertRaises(linkstate.LinkError):
            linkstate.health_endpoint("")

    def test_parse_health_defaults(self) -> None:
        report = linkstate.parse_health({"live": True})
        self.assertFalse(report.platforms)
        self.assertFalse(report.models)
        self.assertIsNone(report.platform("qqonebot"))
        with self.assertRaises(linkstate.LinkError):
            linkstate.parse_health({})

    def test_parse_platform_error_field(self) -> None:
        report = linkstate.parse_health(
            {
                "platforms": [
                    {
                        "name": "qqonebot",
                        "connected": False,
                        "last_error": "connect onebot websocket: refused",
                    }
                ]
            }
        )
        entry = report.platform("qqonebot")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.last_error, "connect onebot websocket: refused")

    def test_format_uptime(self) -> None:
        self.assertEqual(linkstate.format_uptime(0), "0 秒")
        self.assertEqual(linkstate.format_uptime(59), "59 秒")
        self.assertEqual(linkstate.format_uptime(65), "1 分 5 秒")
        self.assertEqual(linkstate.format_uptime(3725), "1 小时 2 分")
        self.assertEqual(linkstate.format_uptime(90000), "1 天 1 小时")

    def test_format_clock(self) -> None:
        self.assertEqual(linkstate.format_clock(""), "")
        self.assertEqual(linkstate.format_clock(linkstate.GO_ZERO_TIME), "")
        rendered = linkstate.format_clock("2026-10-20T05:22:35Z")
        self.assertRegex(rendered, r"^\d\d-\d\d \d\d:\d\d:\d\d$")
        # 解析不了的输入原样返回，界面不会因为脏数据崩掉。
        self.assertEqual(linkstate.format_clock("not-a-time"), "not-a-time")

    def test_describe_platform_states(self) -> None:
        disabled = linkstate.describe_platform(None, enabled=False, service_known=True)
        self.assertIn("未启用", disabled)
        unknown = linkstate.describe_platform(None, enabled=True, service_known=False)
        self.assertIn("后台服务未运行", unknown)
        waiting = linkstate.describe_platform(None, enabled=True, service_known=True)
        self.assertIn("等待", waiting)
        connected = linkstate.describe_platform(
            linkstate.PlatformEntry(name="qqonebot", connected=True),
            enabled=True,
            service_known=True,
        )
        self.assertIn("已连接", connected)
        failed = linkstate.describe_platform(
            linkstate.PlatformEntry(name="qqonebot", last_error="refused"),
            enabled=True,
            service_known=True,
        )
        self.assertIn("refused", failed)

    def test_describe_model_states(self) -> None:
        self.assertIn("正常", linkstate.describe_model(linkstate.ModelEntry(provider="p", status="up")))
        self.assertIn(
            "熔断",
            linkstate.describe_model(
                linkstate.ModelEntry(provider="p", status="up", circuit="open")
            ),
        )
        degraded = linkstate.describe_model(
            linkstate.ModelEntry(provider="p", status="degraded", last_error="boom")
        )
        self.assertIn("异常", degraded)
        self.assertIn("boom", degraded)


class EndpointCase(unittest.TestCase):
    """协议端地址解析与 TCP 探测。"""

    def setUp(self) -> None:
        _tmp_root()

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_parse_ws_url(self) -> None:
        self.assertEqual(linkstate.parse_ws_url("ws://127.0.0.1:3001/"), ("127.0.0.1", 3001))
        self.assertEqual(linkstate.parse_ws_url("127.0.0.1:3001"), ("127.0.0.1", 3001))
        self.assertEqual(linkstate.parse_ws_url("wss://example.com/ws"), ("example.com", 443))
        self.assertEqual(linkstate.parse_ws_url("ws://example.com/ws"), ("example.com", 80))
        self.assertIsNone(linkstate.parse_ws_url(""))
        self.assertIsNone(linkstate.parse_ws_url("ws:///nohost"))

    def test_probe_listening_socket(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            probe = linkstate.probe_endpoint(f"ws://127.0.0.1:{port}/", timeout=1.0)
            self.assertTrue(probe.reachable, probe.detail)
            self.assertEqual(probe.port, port)
            self.assertIn("已监听", probe.detail)
        finally:
            server.close()

    def test_probe_closed_port(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result = linkstate.probe_endpoint(f"ws://127.0.0.1:{port}/", timeout=1.0)
        self.assertFalse(result.reachable)
        self.assertIn("未监听", result.detail)

    def test_probe_bad_url(self) -> None:
        result = linkstate.probe_endpoint("", timeout=1.0)
        self.assertFalse(result.reachable)
        self.assertIn("无效", result.detail)


class PlatformConfigCase(unittest.TestCase):
    """``[platform.qqonebot]`` 的读写（保留注释、幂等）。"""

    def setUp(self) -> None:
        self.config_path = _write_config()
        self.env_path = self.config_path.parent / ".env"
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("QQONEBOT_ACCESS_TOKEN", None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_read_unconfigured_section(self) -> None:
        cfg = linkstate.read_platform_config(str(self.config_path))
        self.assertFalse(cfg.section_exists)
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.has_endpoint)

    def test_enable_then_read_and_disable(self) -> None:
        changed = linkstate.set_platform_enabled(str(self.config_path), True)
        self.assertTrue(changed)
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("# [platform.qqonebot]", text)  # 原注释还在
        self.assertEqual(read_toml(str(self.config_path))["platform"]["qqonebot"], {"enabled": True})
        cfg = linkstate.read_platform_config(str(self.config_path))
        self.assertTrue(cfg.section_exists)
        self.assertTrue(cfg.enabled)

        self.assertTrue(linkstate.set_platform_enabled(str(self.config_path), False))
        self.assertFalse(linkstate.read_platform_config(str(self.config_path)).enabled)
        # 幂等：已经是 false 时不再写文件
        self.assertFalse(linkstate.set_platform_enabled(str(self.config_path), False))

    def test_read_configured_section(self) -> None:
        config_path = self.config_path
        text = config_path.read_text(encoding="utf-8")
        text += (
            '\n[platform.qqonebot]\nenabled = true\nws_url = "ws://127.0.0.1:3001/"\n'
            'access_token_env = "QQONEBOT_ACCESS_TOKEN"\n'
            'trigger_keywords = ["atri", "阿特"]\nsend_file_mode = "base64"\n'
        )
        config_path.write_text(text, encoding="utf-8", newline="\n")
        cfg = linkstate.read_platform_config(str(config_path))
        self.assertTrue(cfg.enabled)
        self.assertTrue(cfg.has_endpoint)
        self.assertEqual(cfg.endpoint, ("127.0.0.1", 3001))
        self.assertEqual(cfg.trigger_keywords, ("atri", "阿特"))
        self.assertEqual(cfg.send_file_mode, "base64")

    def test_connect_notice_defaults_and_roundtrip(self) -> None:
        # 没有 [connect_notice] 段时默认两个开关都开（与内置 Hook 的默认一致），群列表为空。
        cfg = linkstate.read_platform_config(str(self.config_path))
        self.assertTrue(cfg.connect_private)
        self.assertTrue(cfg.connect_group)
        self.assertEqual(cfg.connect_groups, ())

        changed = linkstate.set_connect_notice(
            str(self.config_path),
            private=False,
            group=True,
            groups=["123456789", "qqonebot:group:123456789", " group:2 "],
        )
        self.assertTrue(changed)
        cfg = linkstate.read_platform_config(str(self.config_path))
        self.assertFalse(cfg.connect_private)
        self.assertTrue(cfg.connect_group)
        self.assertEqual(cfg.connect_groups, ("123456789", "2"))
        data = read_toml(str(self.config_path))
        self.assertFalse(data["connect_notice"]["private"])
        self.assertTrue(data["connect_notice"]["group"])

    def test_set_connect_notice_keeps_custom_text(self) -> None:
        text = self.config_path.read_text(encoding="utf-8")
        text += '\n[connect_notice]\nprivate = true\ntext = "我醒了"\n'
        self.config_path.write_text(text, encoding="utf-8", newline="\n")
        linkstate.set_connect_notice(
            str(self.config_path), private=True, group=False, groups=[]
        )
        data = read_toml(str(self.config_path))
        self.assertEqual(data["connect_notice"]["text"], "我醒了")

    def test_secret_state_and_blocker(self) -> None:
        config_path = self.config_path
        text = config_path.read_text(encoding="utf-8")
        text += '\n[platform.qqonebot]\nenabled = true\n'
        config_path.write_text(text, encoding="utf-8", newline="\n")
        cfg = linkstate.read_platform_config(str(config_path))
        # 还没填 ws 地址：先被地址挡住。
        self.assertIn("ws_url", linkstate.connect_blocker(cfg, str(self.env_path)))

        config_path.write_text(
            text + 'ws_url = "ws://127.0.0.1:3001/"\n'
            'access_token_env = "QQONEBOT_ACCESS_TOKEN"\n',
            encoding="utf-8",
            newline="\n",
        )
        cfg = linkstate.read_platform_config(str(config_path))

        self.assertEqual(linkstate.secret_state(cfg, str(self.env_path)), "missing")
        self.assertIn("令牌", linkstate.connect_blocker(cfg, str(self.env_path)))

        self.env_path.write_text(
            "QQONEBOT_ACCESS_TOKEN=secret-token\n", encoding="utf-8", newline="\n"
        )
        self.assertEqual(linkstate.secret_state(cfg, str(self.env_path)), "set")
        self.assertEqual(linkstate.connect_blocker(cfg, str(self.env_path)), "")

    def test_secret_state_without_token_env(self) -> None:
        config_path = self.config_path
        text = config_path.read_text(encoding="utf-8")
        text += '\n[platform.qqonebot]\nenabled = true\nws_url = "ws://127.0.0.1:3001/"\n'
        config_path.write_text(text, encoding="utf-8", newline="\n")
        cfg = linkstate.read_platform_config(str(config_path))
        # 协议端不要求鉴权：没有 access_token_env 就不该拦。
        self.assertEqual(linkstate.secret_state(cfg, str(self.env_path)), "none")
        self.assertEqual(linkstate.connect_blocker(cfg, str(self.env_path)), "")


class HealthEndpointConfigCase(unittest.TestCase):
    """桌面端自动补 ``ATRIX_HEALTH_ADDR`` / ``ATRIX_OPS_TOKEN``（幂等、已有值不动）。"""

    def setUp(self) -> None:
        root = _tmp_root()
        self.config_dir = root / "config"
        self.config_dir.mkdir(parents=True)
        self.env_path = self.config_dir / ".env"
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for name in (service.HEALTH_ADDR_ENV, service.HEALTH_TOKEN_ENV):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_writes_addr_and_token_once(self) -> None:
        addr, token, changed = service.ensure_health_endpoint(str(self.config_dir))
        self.assertTrue(changed)
        self.assertTrue(token)
        self.assertRegex(addr, r"^127\.0\.0\.1:\d+$")
        values = read_env_simple(self.env_path)
        self.assertEqual(values[service.HEALTH_ADDR_ENV], addr)
        self.assertEqual(values[service.HEALTH_TOKEN_ENV], token)

        addr2, token2, changed2 = service.ensure_health_endpoint(str(self.config_dir))
        self.assertFalse(changed2)
        self.assertEqual((addr2, token2), (addr, token))

    def test_keeps_existing_values(self) -> None:
        self.env_path.write_text(
            f"{service.HEALTH_ADDR_ENV}=127.0.0.1:18080\n"
            f"{service.HEALTH_TOKEN_ENV}=user-token\n",
            encoding="utf-8",
            newline="\n",
        )
        addr, token, changed = service.ensure_health_endpoint(str(self.config_dir))
        self.assertFalse(changed)
        self.assertEqual(addr, "127.0.0.1:18080")
        self.assertEqual(token, "user-token")

    def test_port_falls_through_when_busy(self) -> None:
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        try:
            addr = service._pick_health_addr(f"127.0.0.1:{port}")
            self.assertNotEqual(addr, f"127.0.0.1:{port}")
            self.assertTrue(addr.startswith("127.0.0.1:"))
        finally:
            busy.close()

    def test_settings_defaults(self) -> None:
        settings = service.GuiSettings(
            config_path="app.toml",
            config_dir=".",
            url="ws://127.0.0.1:32172/cli/v1/ws",
            client_id="local",
            token="t",
            token_env="ATRIX_CLI_LOCAL_TOKEN",
        )
        self.assertEqual(settings.health_addr, "")
        self.assertEqual(settings.health_token, "")


def read_env_simple(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip()
    return values


class LinkTabCase(unittest.TestCase):
    """界面用例：没有 Tk 就跳过整组。"""

    def setUp(self) -> None:
        self.config_path = _write_config()
        (self.config_path.parent / ".env").write_text(
            "QQONEBOT_ACCESS_TOKEN=secret-token\n", encoding="utf-8", newline="\n"
        )
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()
        self._dialogs = [
            mock.patch("tkinter.messagebox.askyesno", return_value=True),
            mock.patch("tkinter.messagebox.showinfo", return_value=None),
            mock.patch("tkinter.messagebox.showerror", return_value=None),
        ]
        for item in self._dialogs:
            item.start()
        frame = ttk.Frame(self.root)
        frame.pack()
        self.tab = LinkTab(frame, self._paths(), settings=None, can_control_service=False)

    def tearDown(self) -> None:
        for item in self._dialogs:
            item.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def _paths(self) -> Any:
        from atri_x.gui import configedit

        return configedit.ConfigPaths.from_config(str(self.config_path))

    def _set_platform(self, *, enabled: bool) -> None:
        text = self.config_path.read_text(encoding="utf-8")
        text += (
            f"\n[platform.qqonebot]\nenabled = {str(enabled).lower()}\n"
            'ws_url = "ws://127.0.0.1:3001/"\n'
            'access_token_env = "QQONEBOT_ACCESS_TOKEN"\n'
            'trigger_keywords = ["atri"]\n'
        )
        self.config_path.write_text(text, encoding="utf-8", newline="\n")
        self.tab.reload()

    def test_build_and_toggle_text(self) -> None:
        self.assertEqual(str(self.tab._connect_button.cget("text")), "连接（上线）")
        self._set_platform(enabled=True)
        self.assertEqual(str(self.tab._connect_button.cget("text")), "断开（下线）")

    def test_render_connected_state(self) -> None:
        self._set_platform(enabled=True)
        self.tab._report = linkstate.parse_health(HEALTH_BODY)
        self.tab._render_platform()
        self.assertIn("已连接", self.tab._vars["platform_state"].get())
        self.assertIn("连接次数 2", self.tab._vars["platform_detail"].get())

    def test_render_service_offline(self) -> None:
        self.tab._render_service()
        self.assertEqual(self.tab._vars["service_state"].get(), "未运行")
        self.assertIn("状态接口未配置", self.tab._vars["service_detail"].get())

    def test_render_service_running_without_pid_file(self) -> None:
        # 服务是别处启动的（没有 pid 记录），但状态接口通 —— 不能显示「未运行」。
        self.tab._report = linkstate.parse_health(HEALTH_BODY)
        self.tab._render_service()
        self.assertIn("运行中", self.tab._vars["service_state"].get())
        self.assertIn("0.9.9", self.tab._vars["service_detail"].get())

    def test_render_models(self) -> None:
        self.tab._report = linkstate.parse_health(HEALTH_BODY)
        self.tab._render_models()
        self.assertIn("deepseek", self.tab._vars["models"].get())
        self.assertIn("就绪", self.tab._vars["models"].get())

    def test_render_models_without_records_shows_readiness(self) -> None:
        body = dict(HEALTH_BODY)
        body.pop("models")
        self.tab._report = linkstate.parse_health(body)
        self.tab._render_models()
        text = self.tab._vars["models"].get()
        self.assertIn("provider", text)
        self.assertIn("就绪", text)

    def test_toggle_writes_config_without_service(self) -> None:
        self._set_platform(enabled=False)
        self.tab._toggle_online()
        data = read_toml(str(self.config_path))
        self.assertTrue(data["platform"]["qqonebot"]["enabled"])
        self.assertIn("已写入配置", self.tab._status.get())

        self.tab._toggle_online()
        data = read_toml(str(self.config_path))
        self.assertFalse(data["platform"]["qqonebot"]["enabled"])

    def test_toggle_blocks_when_endpoint_missing(self) -> None:
        # 初始配置里 qqonebot 是注释状态，没有 ws 地址 → 应被拦住、不落盘。
        self.tab._toggle_online()
        data = read_toml(str(self.config_path))
        self.assertNotIn("qqonebot", data.get("platform", {}))
        self.assertIn("失败", self.tab._status.get())

    def test_save_connect_notice_writes_config(self) -> None:
        self.tab._notice_private_var.set(False)
        self.tab._notice_group_var.set(True)
        self.tab._notice_groups_var.set("123456789，group:123")
        self.tab._save_connect_notice()
        data = read_toml(str(self.config_path))
        self.assertFalse(data["connect_notice"]["private"])
        self.assertTrue(data["connect_notice"]["group"])
        self.assertEqual(data["connect_notice"]["groups"], ["123456789", "123"])
        self.assertEqual(self.tab._cfg.connect_groups, ("123456789", "123"))
        self.assertIn("已保存上线通知设置", self.tab._status.get())

    def test_reload_syncs_connect_notice_controls(self) -> None:
        linkstate.set_connect_notice(
            str(self.config_path),
            private=False,
            group=True,
            groups=["123456789"],
        )
        self.tab.reload()
        self.assertFalse(self.tab._notice_private_var.get())
        self.assertTrue(self.tab._notice_group_var.get())
        self.assertEqual(self.tab._notice_groups_var.get(), "123456789")

    def test_save_wake_words_roundtrip(self) -> None:
        self._set_platform(enabled=True)
        self.tab._wake_var.set("atri，小助手")
        self.tab._save_wake_words()
        data = read_toml(str(self.config_path))
        self.assertEqual(data["platform"]["qqonebot"]["trigger_keywords"], ["atri", "小助手"])
        self.assertEqual(self.tab._cfg.trigger_keywords, ("atri", "小助手"))
        self.assertEqual(self.tab._wake_var.get(), "atri、小助手")
        self.assertIn("已保存唤醒词", self.tab._status.get())

    def test_clear_wake_words_removes_key(self) -> None:
        self._set_platform(enabled=True)
        self.tab._wake_var.set("")
        self.tab._save_wake_words()
        data = read_toml(str(self.config_path))
        self.assertNotIn("trigger_keywords", data["platform"]["qqonebot"])
        self.assertEqual(self.tab._cfg.trigger_keywords, ())

    def test_copy_info(self) -> None:
        self._set_platform(enabled=True)
        self.tab._copy_info()
        text = self.root.clipboard_get()
        self.assertIn("OneBot", text)
        self.assertIn("ws://127.0.0.1:3001/", text)


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
