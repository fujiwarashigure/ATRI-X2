"""GUI 服务侧用例：配置/token 准备、WS 探测、服务命令组装。

这些逻辑决定「双击桌面端后能不能连上后台服务」，也是**唯一会改用户配置文件**的地方，
所以逐条断言得很细：只改一行、写前备份、幂等、解析校验。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import tomllib

from atri_x.gui import service
from atri_x.net.websocket import WebSocketConnection, serve

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_service"

#: 一份「发行资产风格」的最小 app.toml：注释与默认值都保留，便于断言「只改一行」。
SAMPLE_APP_TOML = """\
# ATRI-X 配置示例（测试用）
[platform]
# 启用的平台
platforms = ["cli"]

[platform.cli]
enabled = true
default_client = "local"
default_url = "ws://127.0.0.1:32172/cli/v1/ws"

# Used only when this ATRI-X runs as a CLI server.
[platform.cli.server]
enabled = false
listen = "127.0.0.1:32172"

# Client ids allowed to log in to this CLI server.
[platform.cli.server.tokens]
local = ["ATRIX_CLI_LOCAL_TOKEN"]
"""


class EnvTestCase(unittest.TestCase):
    """把 APPDATA / ATRIX_CONFIG_FILE 收进临时目录，避免碰真实配置。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        self.config_dir = TEMP_ROOT / "config"
        self.config_dir.mkdir(parents=True)
        self.appdata = TEMP_ROOT / "appdata"
        self.appdata.mkdir(parents=True)
        self.config_path = self.config_dir / "app.toml"
        self.config_path.write_text(SAMPLE_APP_TOML, encoding="utf-8", newline="\n")
        self._env = mock.patch.dict(
            os.environ,
            {
                "APPDATA": str(self.appdata),
                "ATRIX_CONFIG_FILE": str(self.config_path),
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("ATRIX_CLI_LOCAL_TOKEN", None)

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)


class ConfigPatchTestCase(EnvTestCase):
    """app.toml 定向修改。"""

    def test_ensure_server_enabled_flips_only_the_flag(self) -> None:
        patched, changed = service.ensure_server_enabled(SAMPLE_APP_TOML)
        self.assertTrue(changed)
        self.assertIn("enabled = true", patched)
        self.assertIn("# Used only when this ATRI-X runs as a CLI server.", patched)
        self.assertIn('listen = "127.0.0.1:32172"', patched)
        # 再跑一次应当幂等。
        again, changed_again = service.ensure_server_enabled(patched)
        self.assertFalse(changed_again)
        self.assertEqual(again, patched)
        parsed = tomllib.loads(patched)
        self.assertTrue(parsed["platform"]["cli"]["server"]["enabled"])

    def test_ensure_server_enabled_appends_section_when_missing(self) -> None:
        text = "[platform.cli]\nenabled = true\n"
        patched, changed = service.ensure_server_enabled(text)
        self.assertTrue(changed)
        parsed = tomllib.loads(patched)
        self.assertTrue(parsed["platform"]["cli"]["server"]["enabled"])

    def test_ensure_server_enabled_keeps_trailing_comment(self) -> None:
        text = "[platform.cli.server]\nenabled = false # only for service mode\n"
        patched, changed = service.ensure_server_enabled(text)
        self.assertTrue(changed)
        self.assertIn("enabled = true # only for service mode", patched)

    def test_ensure_tokens_section_fills_empty_table(self) -> None:
        text = SAMPLE_APP_TOML.split("[platform.cli.server.tokens]")[0] + "[platform.cli.server.tokens]\n"
        patched, changed = service.ensure_tokens_section(text)
        self.assertTrue(changed)
        parsed = tomllib.loads(patched)
        tokens = parsed["platform"]["cli"]["server"]["tokens"]
        self.assertEqual(tokens[service.DEFAULT_CLIENT_ID], [service.DEFAULT_TOKEN_ENV])

    def test_ensure_tokens_section_keeps_existing_client(self) -> None:
        _patched, changed = service.ensure_tokens_section(SAMPLE_APP_TOML)
        self.assertFalse(changed)

    def test_inspect_tokens_picks_first_usable_client(self) -> None:
        data = tomllib.loads(SAMPLE_APP_TOML)
        client_id, env_names = service.inspect_tokens(data)
        self.assertEqual(client_id, "local")
        self.assertEqual(env_names, ["ATRIX_CLI_LOCAL_TOKEN"])
        self.assertEqual(service.inspect_tokens({}), ("", []))

    def test_inspect_tokens_skips_client_without_env_names(self) -> None:
        data = {"platform": {"cli": {"server": {"tokens": {"a": [], "b": ["B_TOKEN"]}}}}}
        self.assertEqual(service.inspect_tokens(data), ("b", ["B_TOKEN"]))


class PrepareSettingsTestCase(EnvTestCase):
    """端到端：准备配置 + token。"""

    def test_prepare_writes_token_and_enables_server(self) -> None:
        settings = service.prepare_settings(str(self.config_path))
        self.assertTrue(settings.changed)
        self.assertEqual(settings.client_id, "local")
        self.assertEqual(settings.token_env, service.DEFAULT_TOKEN_ENV)
        self.assertTrue(settings.token.startswith("atrix-gui-"))
        self.assertEqual(settings.url, "ws://127.0.0.1:32172/cli/v1/ws")

        parsed = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(parsed["platform"]["cli"]["server"]["enabled"])
        env_text = (self.config_dir / ".env").read_text(encoding="utf-8")
        self.assertIn(f"{service.DEFAULT_TOKEN_ENV}={settings.token}", env_text)
        # 写前备份
        self.assertTrue((self.config_dir / "app.toml.bak").is_file())

    def test_prepare_is_idempotent(self) -> None:
        first = service.prepare_settings(str(self.config_path))
        second = service.prepare_settings(str(self.config_path))
        self.assertFalse(second.changed)
        self.assertEqual(first.token, second.token)
        env_text = (self.config_dir / ".env").read_text(encoding="utf-8")
        self.assertEqual(env_text.count(service.DEFAULT_TOKEN_ENV), 1)

    def test_prepare_reuses_token_from_env(self) -> None:
        with mock.patch.dict(os.environ, {service.DEFAULT_TOKEN_ENV: "from-env"}, clear=False):
            settings = service.prepare_settings(str(self.config_path))
        self.assertEqual(settings.token, "from-env")
        # CLI token 已经由环境变量提供，不该再写进 .env；但健康接口的地址与令牌
        # 仍会补进 .env（「在线」选项卡要读后台状态，见 ensure_health_endpoint）。
        env_text = (self.config_dir / ".env").read_text(encoding="utf-8")
        self.assertNotIn(service.DEFAULT_TOKEN_ENV, env_text)
        self.assertIn(service.HEALTH_ADDR_ENV, env_text)
        self.assertIn(service.HEALTH_TOKEN_ENV, env_text)
        self.assertTrue(settings.health_addr)
        self.assertTrue(settings.health_token)

    def test_prepare_missing_config_raises(self) -> None:
        with self.assertRaises(service.GuiConfigError):
            service.prepare_settings(str(TEMP_ROOT / "nope" / "app.toml"))

    def test_ensure_token_replaces_existing_empty_line(self) -> None:
        env_path = self.config_dir / ".env"
        env_path.write_text(
            f"OTHER=1\n{service.DEFAULT_TOKEN_ENV}=\n# 注释保留\n", encoding="utf-8", newline="\n"
        )
        _name, token, created = service.ensure_token(
            str(self.config_dir), [service.DEFAULT_TOKEN_ENV]
        )
        self.assertTrue(created)
        text = env_path.read_text(encoding="utf-8")
        self.assertIn(f"{service.DEFAULT_TOKEN_ENV}={token}", text)
        self.assertIn("OTHER=1", text)
        self.assertIn("# 注释保留", text)
        self.assertEqual(text.count(service.DEFAULT_TOKEN_ENV), 1)


class FakeCliServer:
    """最小 CLI WS 服务端：只处理 ``hello`` → ``hello_ok`` / ``error``。"""

    def __init__(self, token: str = "secret") -> None:
        self.token = token
        self.hellos: list[dict[str, Any]] = []

    async def handle(self, conn: WebSocketConnection) -> None:
        raw = await conn.recv()
        hello = json.loads(raw)
        self.hellos.append(hello)
        if hello.get("token") == self.token:
            await conn.send_text(json.dumps({"type": "hello_ok", "client_id": hello.get("client_id")}))
        else:
            await conn.send_text(json.dumps({"type": "error", "text": "bad token"}))
        while True:
            try:
                await conn.recv()
            except Exception:  # noqa: BLE001 - 客户端断开
                return


class ServerThread:
    """在后台事件循环里跑一个 WS 服务端。"""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.server: Any = None

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> str:
        self.thread.start()
        future = asyncio.run_coroutine_threadsafe(
            serve(self.handler, host="127.0.0.1", port=0, path="/cli/v1/ws"), self.loop
        )
        self.server = future.result(timeout=10)
        return "ws://127.0.0.1:%d/cli/v1/ws" % self.server.port

    def stop(self) -> None:
        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(timeout=10)
            self.server = None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        with contextlib.suppress(RuntimeError):
            self.loop.close()


class ProbeServerTestCase(unittest.TestCase):
    """WS 探测：通的/不通的/鉴权失败的。"""

    def setUp(self) -> None:
        self.fake = FakeCliServer(token="secret")
        self.server = ServerThread(self.fake.handle)
        self.url = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def test_probe_succeeds_with_correct_token(self) -> None:
        ok, detail = service.probe_server(self.url, "local", "secret", timeout=5.0)
        self.assertTrue(ok, detail)
        self.assertEqual(self.fake.hellos[0]["client_id"], "local")

    def test_probe_reports_bad_token(self) -> None:
        ok, detail = service.probe_server(self.url, "local", "wrong", timeout=5.0)
        self.assertFalse(ok)
        self.assertIn("bad token", detail)

    def test_probe_reports_connection_failure(self) -> None:
        ok, detail = service.probe_server("ws://127.0.0.1:1/cli/v1/ws", "local", "x", timeout=1.0)
        self.assertFalse(ok)
        self.assertTrue(detail)


class ServiceCommandTestCase(EnvTestCase):
    """后台服务命令组装。"""

    def test_source_mode_uses_python_module(self) -> None:
        settings = service.prepare_settings(str(self.config_path))
        command = service.service_command(settings)
        self.assertEqual(command[1:4], ["-m", "atri_x", "service"])
        self.assertEqual(command[4], "run")
        self.assertIn("--config", command)
        self.assertEqual(command[-1], settings.config_path)

    def test_state_dir_and_log_path_are_next_to_config(self) -> None:
        directory = service.state_dir()
        self.assertTrue(directory.startswith(str(self.config_dir)))
        self.assertTrue(service.service_log_path().endswith("service.out.log"))


class ServerUrlTestCase(unittest.TestCase):
    """``_server_url`` 的优先级：本机监听 > 客户端 profile url > default_url。"""

    def _data(self, *, enabled: bool, listen: str, profile_url: str = "") -> dict:
        profile: dict[str, object] = {}
        if profile_url:
            profile["url"] = profile_url
        return {
            "platform": {
                "cli": {
                    "enabled": True,
                    "default_client": "local",
                    "default_url": "ws://127.0.0.1:32172/cli/v1/ws",
                    "server": {"enabled": enabled, "listen": listen},
                    "clients": {"local": profile},
                }
            }
        }

    def test_listen_wins_when_server_enabled(self) -> None:
        url = service._server_url(self._data(enabled=True, listen="127.0.0.1:32299"))
        self.assertEqual(url, "ws://127.0.0.1:32299/cli/v1/ws")

    def test_wildcard_listen_uses_loopback(self) -> None:
        url = service._server_url(self._data(enabled=True, listen="0.0.0.0:32299"))
        self.assertEqual(url, "ws://127.0.0.1:32299/cli/v1/ws")

    def test_explicit_client_url_wins_over_listen(self) -> None:
        url = service._server_url(
            self._data(
                enabled=True,
                listen="127.0.0.1:32299",
                profile_url="ws://10.0.0.2:32172/cli/v1/ws",
            )
        )
        self.assertEqual(url, "ws://10.0.0.2:32172/cli/v1/ws")

    def test_default_url_when_server_disabled(self) -> None:
        url = service._server_url(self._data(enabled=False, listen="127.0.0.1:32299"))
        self.assertEqual(url, "ws://127.0.0.1:32172/cli/v1/ws")

    def test_fallback_without_platform_section(self) -> None:
        self.assertEqual(
            service._server_url({}), "ws://127.0.0.1:32172/cli/v1/ws"
        )


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
