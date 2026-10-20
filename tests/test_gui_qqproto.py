"""内置 QQ 协议端环境（``atri_x.gui.qqproto``）的用例。

覆盖三块：

1. **未内置环境**：一个文件都不写（外部 NapCat 用户的配置绝不能被碰）；
2. **首次运行准备**：令牌 / 端口 / ``onebot11.json`` / ``webui.json`` / ``app.toml``
   的生成规则，幂等性，已有值优先，端口冲突顺延；
3. **界面与 CLI**：``LinkTab`` 的「启动协议端」按钮状态、``-m`` 入口的 JSON 摘要
   （摘要里不出现令牌明文）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from typing import Any
from unittest import mock

from atri_x.gui import configedit, qqproto, service

#: 测试临时目录（仓库规定：临时目录放仓库内 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_qqproto"

APP_TOML = """\
# ATRI-X 测试用配置
[platform]
platforms = ["cli"]

[platform.cli]
enabled = true
default_client = "local"

[platform.cli.server]
enabled = false
listen = "127.0.0.1:32172"
"""


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _make_bundle(root: Path) -> Path:
    """造一个「发行包里的 qqproto 目录 + NapCat Shell」结构（文件都是桩）。"""
    shell = root / "bundle" / "qqproto" / "NapCat.52230.Shell"
    app = shell / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat"
    _write(shell / "NapCatWinBootMain.exe", "stub")
    _write(shell / "QQ.exe", "stub")
    _write(
        app / "napcat.mjs",
        'const sQ = {}, Vu = typeof sQ < "u" && "4.18.38" || "1.0.0-dev";\n',
    )
    (app / "node_modules").mkdir(parents=True, exist_ok=True)
    (app / "native").mkdir(parents=True, exist_ok=True)
    (app / "config").mkdir(parents=True, exist_ok=True)
    return root / "bundle"


def _make_config(root: Path, text: str = APP_TOML) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return _write(config_dir / "app.toml", text)


def _read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _env(**values: str) -> Any:
    """临时设置环境变量；值为空串表示删掉。"""

    class _Ctx:
        def __enter__(self) -> None:
            self.saved = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)

        def __exit__(self, *exc: Any) -> None:
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Ctx()


#: 这些环境变量会被 ``prepare_settings`` / ``ensure_*`` 写进进程环境，用例要自己还原，
#: 否则后面的用例会误以为"已经有值"而不再写 ``.env``。
_PROCESS_ENV_KEYS = (
    "APPDATA",
    "XDG_CONFIG_HOME",
    "ATRIX_CONFIG_FILE",
    "ATRIX_CLI_LOCAL_TOKEN",
    "ATRIX_HEALTH_ADDR",
    "ATRIX_OPS_TOKEN",
    "QQONEBOT_ACCESS_TOKEN",
    "ATRIX_QQPROTO_DIR",
)


def _snapshot_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in _PROCESS_ENV_KEYS}


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _napcat_config_dir(shell: str) -> Path:
    return Path(shell) / "versions" / "9.9.33-52230" / "resources" / "app" / "napcat" / "config"


class QqProtoLogicCase(unittest.TestCase):
    """纯逻辑用例：不建 Tk 窗口。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEMP_ROOT / "case"
        self.bundle = _make_bundle(self.root)
        self.config_path = _make_config(self.root)
        self.config_dir = self.config_path.parent

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    # ---------------------------------------------------------------- 未内置

    def test_no_bundle_writes_nothing(self) -> None:
        before = self.config_path.read_text(encoding="utf-8")
        with _env(**{qqproto.BUNDLE_ENV_VAR: ""}):
            setup = qqproto.ensure_qqproto(
                str(self.config_dir),
                bundle_dir=str(self.root / "no-such-bundle"),
                app_toml=str(self.config_path),
            )
        self.assertFalse(setup.available)
        self.assertIn("未内置", setup.reason)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertFalse((self.config_dir / ".env").exists())

    def test_incomplete_shell_reports_reason(self) -> None:
        broken = self.root / "broken" / "qqproto" / "NapCat.Broken.Shell"
        _write(broken / "NapCatWinBootMain.exe", "stub")
        (broken / "versions").mkdir(parents=True)
        setup = qqproto.ensure_qqproto(
            str(self.config_dir),
            bundle_dir=str(self.root / "broken"),
            app_toml=str(self.config_path),
        )
        self.assertFalse(setup.available)
        self.assertIn("不完整", setup.reason)

    # ---------------------------------------------------------------- 首次准备

    def _ensure(self, **kwargs: Any) -> qqproto.QqProtoSetup:
        params: dict[str, Any] = {
            "bundle_dir": str(self.bundle),
            "app_toml": str(self.config_path),
        }
        params.update(kwargs)
        return qqproto.ensure_qqproto(str(self.config_dir), **params)

    def test_first_run_generates_configs(self) -> None:
        setup = self._ensure()
        self.assertTrue(setup.available, setup.reason)
        self.assertTrue(setup.changed)
        self.assertEqual(setup.onebot_port, qqproto.DEFAULT_ONEBOT_PORT)
        self.assertGreater(setup.webui_port, 0)

        env_values = configedit.read_env(str(self.config_dir / ".env"))
        token = env_values[qqproto.ONEBOT_TOKEN_ENV]
        self.assertEqual(token, setup.token)
        self.assertEqual(len(token), 32)

        napcat = _napcat_config_dir(setup.shell_dir)
        onebot = _read(napcat / "onebot11.json")
        servers = onebot["network"]["websocketServers"]
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(server["name"], qqproto.SERVER_NAME)
        self.assertEqual(server["host"], "127.0.0.1")
        self.assertEqual(server["port"], setup.onebot_port)
        self.assertEqual(server["token"], token)
        self.assertTrue(server["enable"])
        self.assertEqual(server["messagePostFormat"], "array")

        webui = _read(napcat / "webui.json")
        self.assertEqual(webui["host"], "127.0.0.1")
        self.assertEqual(webui["port"], setup.webui_port)
        self.assertEqual(len(str(webui["token"])), 16)
        self.assertIn("token=", setup.webui_url)

        data = configedit.read_toml(str(self.config_path))
        section = data["platform"]["qqonebot"]
        self.assertFalse(section["enabled"])
        self.assertEqual(section["ws_url"], f"ws://127.0.0.1:{setup.onebot_port}/")
        self.assertEqual(section["access_token_env"], qqproto.ONEBOT_TOKEN_ENV)
        self.assertEqual(section["trigger_keywords"], ["atri"])

    def test_second_run_is_idempotent(self) -> None:
        first = self._ensure()
        napcat = _napcat_config_dir(first.shell_dir)
        snapshot = {
            path.name: path.read_text(encoding="utf-8")
            for path in napcat.iterdir()
            if path.is_file()
        }
        app_before = self.config_path.read_text(encoding="utf-8")
        second = self._ensure()
        self.assertFalse(second.changed)
        self.assertEqual(second.onebot_port, first.onebot_port)
        self.assertEqual(second.token, first.token)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), app_before)
        for name, text in snapshot.items():
            self.assertEqual((napcat / name).read_text(encoding="utf-8"), text)

    def test_configured_local_port_wins(self) -> None:
        text = APP_TOML + (
            "\n[platform.qqonebot]\n"
            'ws_url = "ws://127.0.0.1:3456/"\n'
            'access_token_env = "QQONEBOT_ACCESS_TOKEN"\n'
        )
        _write(self.config_path, text)
        setup = self._ensure()
        self.assertEqual(setup.onebot_port, 3456)
        onebot = _read(_napcat_config_dir(setup.shell_dir) / "onebot11.json")
        self.assertEqual(onebot["network"]["websocketServers"][0]["port"], 3456)

    def test_non_loopback_endpoint_is_left_alone(self) -> None:
        text = APP_TOML + (
            "\n[platform.qqonebot]\n"
            'ws_url = "ws://10.1.2.3:6700/"\n'
            'access_token_env = "QQONEBOT_ACCESS_TOKEN"\n'
        )
        _write(self.config_path, text)
        setup = self._ensure()
        self.assertNotEqual(setup.onebot_port, 6700)
        data = configedit.read_toml(str(self.config_path))
        self.assertEqual(data["platform"]["qqonebot"]["ws_url"], "ws://10.1.2.3:6700/")

    def test_existing_token_is_reused(self) -> None:
        _write(self.config_dir / ".env", "QQONEBOT_ACCESS_TOKEN=user-provided-token\n")
        with _env(**{qqproto.ONEBOT_TOKEN_ENV: ""}):
            setup = self._ensure()
        self.assertEqual(setup.token, "user-provided-token")
        onebot = _read(_napcat_config_dir(setup.shell_dir) / "onebot11.json")
        self.assertEqual(onebot["network"]["websocketServers"][0]["token"], "user-provided-token")

    def test_env_var_token_is_used(self) -> None:
        with _env(**{qqproto.ONEBOT_TOKEN_ENV: "from-process-env"}):
            setup = self._ensure()
        self.assertEqual(setup.token, "from-process-env")
        self.assertFalse((self.config_dir / ".env").exists())

    def test_user_section_keys_are_preserved(self) -> None:
        text = APP_TOML + (
            "\n[platform.qqonebot]\n"
            "enabled = true\n"
            'trigger_keywords = ["bot", "atri"]\n'
        )
        _write(self.config_path, text)
        setup = self._ensure()
        data = configedit.read_toml(str(self.config_path))
        section = data["platform"]["qqonebot"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["trigger_keywords"], ["bot", "atri"])
        self.assertEqual(section["ws_url"], f"ws://127.0.0.1:{setup.onebot_port}/")
        self.assertEqual(section["access_token_env"], qqproto.ONEBOT_TOKEN_ENV)

    def test_existing_webui_is_preserved(self) -> None:
        napcat = _napcat_config_dir(str(self.bundle / "qqproto" / "NapCat.52230.Shell"))
        _write(
            napcat / "webui.json",
            json.dumps({"host": "127.0.0.1", "port": 7099, "token": "keep-me"}),
        )
        setup = self._ensure()
        self.assertEqual(setup.webui_port, 7099)
        webui = _read(napcat / "webui.json")
        self.assertEqual(webui["token"], "keep-me")

    def test_bundle_env_var_override(self) -> None:
        with _env(**{qqproto.BUNDLE_ENV_VAR: str(self.bundle / qqproto.ENV_DIR_NAME)}):
            setup = qqproto.ensure_qqproto(
                str(self.config_dir), app_toml=str(self.config_path)
            )
        self.assertTrue(setup.available, setup.reason)
        self.assertTrue(setup.env_dir.endswith(qqproto.ENV_DIR_NAME))

    # ---------------------------------------------------------------- 小工具

    def test_pick_free_port_skips_busy(self) -> None:
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = int(busy.getsockname()[1])
        try:
            picked = qqproto.pick_free_port("127.0.0.1", port)
        finally:
            busy.close()
        self.assertNotEqual(picked, port)
        self.assertGreater(picked, port)

    def test_summary_never_contains_token(self) -> None:
        setup = self._ensure()
        summary = qqproto.setup_summary(setup)
        self.assertNotIn("token", summary)
        self.assertTrue(summary["tokenSet"])
        self.assertNotIn(setup.token, json.dumps(summary, ensure_ascii=False))

    def test_launch_without_bundle_fails(self) -> None:
        setup = qqproto.QqProtoSetup(available=False, reason="没有内置环境")
        ok, detail = qqproto.launch_background(setup)
        self.assertFalse(ok)
        self.assertIn("没有内置环境", detail)

    def test_launch_missing_script_fails(self) -> None:
        setup = qqproto.QqProtoSetup(
            available=True, env_dir=str(self.root), shell_dir=str(self.root)
        )
        ok, detail = qqproto.launch_background(setup)
        self.assertFalse(ok)
        self.assertIn("启动脚本", detail)

    def test_cli_json(self) -> None:
        appdata = self.root / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO()
        with _env(
            APPDATA=str(appdata),
            XDG_CONFIG_HOME=str(appdata),
            ATRIX_CONFIG_FILE=str(self.config_path),
        ), contextlib.redirect_stdout(buffer):
            code = qqproto.main(["--json", "--bundle-dir", str(self.bundle)])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
        self.assertTrue(payload["available"])
        self.assertTrue(payload["tokenSet"])
        self.assertNotIn("token=", payload["webuiUrl"])


class QqProtoServiceCase(unittest.TestCase):
    """``prepare_settings`` 要能接上内置协议端，且没有内置环境时不新增文件。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEMP_ROOT / "service"
        self.bundle = _make_bundle(self.root)
        self.config_path = _make_config(self.root)
        self._saved_env = _snapshot_env()
        self._env_ctx = _env(**{qqproto.ONEBOT_TOKEN_ENV: "", "ATRIX_HEALTH_ADDR": "", "ATRIX_OPS_TOKEN": ""})
        self._env_ctx.__enter__()

    def tearDown(self) -> None:
        self._env_ctx.__exit__(None, None, None)
        _restore_env(self._saved_env)
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_settings_carry_qqproto(self) -> None:
        with _env(**{qqproto.BUNDLE_ENV_VAR: str(self.bundle / qqproto.ENV_DIR_NAME)}):
            settings = service.prepare_settings(str(self.config_path))
        self.assertIsNotNone(settings.qqproto)
        assert settings.qqproto is not None
        self.assertTrue(settings.qqproto.available, settings.qqproto.reason)
        self.assertEqual(settings.qqproto.onebot_port, qqproto.DEFAULT_ONEBOT_PORT)

    def test_settings_without_bundle(self) -> None:
        with _env(**{qqproto.BUNDLE_ENV_VAR: ""}):
            settings = service.prepare_settings(str(self.config_path))
        self.assertIsNotNone(settings.qqproto)
        assert settings.qqproto is not None
        self.assertFalse(settings.qqproto.available)


class LinkTabCase(unittest.TestCase):
    """界面用例：没有 Tk 就跳过整组。"""

    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.config_path = _make_config(TEMP_ROOT / "tab")
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def _tab(self, setup: qqproto.QqProtoSetup | None) -> Any:
        from atri_x.gui.tabs.link import LinkTab

        frame = ttk.Frame(self.root)
        frame.pack()
        settings = service.GuiSettings(
            config_path=str(self.config_path),
            config_dir=str(self.config_path.parent),
            url="ws://127.0.0.1:32172/cli/v1/ws",
            client_id="local",
            token="t",
            token_env="ATRIX_CLI_LOCAL_TOKEN",
            qqproto=setup,
        )
        return LinkTab(frame, configedit.ConfigPaths.from_config(str(self.config_path)), settings=settings)

    def test_start_button_enabled_for_bundled_env(self) -> None:
        setup = qqproto.QqProtoSetup(
            available=True,
            env_dir="C:/x/qqproto",
            shell_dir="C:/x/qqproto/NapCat.52230.Shell",
            onebot_port=3001,
            webui_port=6099,
            webui_url="http://127.0.0.1:6099/webui?token=abc",
        )
        tab = self._tab(setup)
        self.assertEqual(str(tab._start_button.cget("state")), "normal")
        self.assertIn("内置协议端", tab._vars["endpoint_env"].get())

    def test_start_button_disabled_without_env(self) -> None:
        tab = self._tab(qqproto.QqProtoSetup(available=False, reason="未内置"))
        self.assertEqual(str(tab._start_button.cget("state")), "disabled")
        self.assertIn("不可用", tab._vars["endpoint_env"].get())


if __name__ == "__main__":
    unittest.main()
