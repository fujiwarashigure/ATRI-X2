"""协议端管理层（``proto_process`` / ``snowluma_proto`` / ``proto_manager``）的用例。

三块重点：

1. **PID 归属**：停止只认本程序写进状态文件的 PID，镜像名对不上就放弃——绝不按
   进程名杀（``Stop-NapCat.bat`` 的 ``taskkill /f /im QQ.exe`` 会杀掉本机所有 QQ）；
2. **SnowLuma 只读**：它的端口与令牌从它自己的 ``config/onebot_<uin>.json`` 读，
   ATRI-X 不改第三方文件；
3. **补配置只填空值**：``ws_url`` / 令牌已经有值就一个都不动（"切换协议端不覆盖地址
   令牌"这条硬约束）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from atri_x.gui import configedit, proto_manager, proto_process, qqproto, snowluma_proto
from atri_x.gui.proto_setup import ProtoSetup

TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_proto"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _make_snowluma(root: Path, *, uin: str = "10001", port: int = 3001) -> Path:
    """造一个"用户自己装好的 SnowLuma"目录（文件都是桩）。"""
    _write(root / "package.json", json.dumps({"name": "snowluma", "version": "2.2.0"}))
    _write(root / "index.mjs", "// stub\n")
    _write(root / "node.exe", "stub")
    _write(
        root / "config" / f"onebot_{uin}.json",
        json.dumps(
            {
                "networks": {
                    "wsServers": [
                        {
                            "name": "ws-default",
                            "host": "127.0.0.1",
                            "port": port,
                            "accessToken": "snow-token-abc",
                        }
                    ]
                }
            }
        ),
    )
    return root


APP_TOML = """\
[platform]
platforms = ["cli"]

[platform.cli]
enabled = true
"""


class ProcessStateCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.env_dir = TEMP_ROOT / "proto"

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_state_round_trip(self) -> None:
        self.assertEqual(proto_process.read_state(str(self.env_dir)), {})
        proto_process.write_state(str(self.env_dir), {"pid": 123, "backend": "napcat"})
        self.assertEqual(proto_process.read_state(str(self.env_dir))["pid"], 123)
        proto_process.clear_state(str(self.env_dir))
        self.assertEqual(proto_process.read_state(str(self.env_dir)), {})

    def test_status_without_record(self) -> None:
        info = proto_process.managed_status(str(self.env_dir))
        self.assertFalse(info["managed"])
        self.assertIn("没有启动", info["reason"])

    def test_stop_without_record_does_not_touch_anything(self) -> None:
        with mock.patch.object(proto_process, "_terminate_tree") as killer:
            ok, detail = proto_process.stop_managed(str(self.env_dir))
        self.assertFalse(ok)
        self.assertIn("没有本程序启动", detail)
        killer.assert_not_called()

    def test_stop_clears_record_when_pid_is_gone(self) -> None:
        proto_process.write_state(str(self.env_dir), {"pid": 424242, "image": "cmd.exe"})
        with (
            mock.patch.object(proto_process, "_pid_alive", return_value=False),
            mock.patch.object(proto_process, "_terminate_tree") as killer,
        ):
            ok, _detail = proto_process.stop_managed(str(self.env_dir))
        self.assertFalse(ok)
        killer.assert_not_called()
        self.assertEqual(proto_process.read_state(str(self.env_dir)), {})

    def test_stop_refuses_pid_reuse(self) -> None:
        """PID 被别的进程占用（镜像名不一致）时放弃，而不是把别人的 QQ 杀掉。"""
        proto_process.write_state(str(self.env_dir), {"pid": 777, "image": "cmd.exe"})
        with (
            mock.patch.object(proto_process, "_pid_alive", return_value=True),
            mock.patch.object(proto_process, "_pid_image", return_value="QQ.exe"),
            mock.patch.object(proto_process, "_terminate_tree") as killer,
        ):
            ok, detail = proto_process.stop_managed(str(self.env_dir))
        self.assertFalse(ok)
        self.assertIn("不是本程序启动", detail)
        killer.assert_not_called()

    def test_stop_kills_only_own_tree(self) -> None:
        proto_process.write_state(
            str(self.env_dir), {"pid": 888, "image": "cmd.exe", "backend": "napcat"}
        )
        alive = {"value": True}

        def fake_alive(pid: int) -> bool:
            return alive["value"]

        def fake_terminate(pid: int) -> None:
            alive["value"] = False

        with (
            mock.patch.object(proto_process, "_pid_alive", side_effect=fake_alive),
            mock.patch.object(proto_process, "_pid_image", return_value="cmd.exe"),
            mock.patch.object(proto_process, "_terminate_tree", side_effect=fake_terminate) as killer,
        ):
            ok, detail = proto_process.stop_managed(str(self.env_dir))
        self.assertTrue(ok)
        self.assertIn("888", detail)
        killer.assert_called_once_with(888)
        self.assertEqual(proto_process.read_state(str(self.env_dir)), {})

    def test_status_accepts_recorded_image_without_exe_suffix(self) -> None:
        """记录存的是 ``argv[0]`` 的 basename（``cmd``），tasklist 报的是 ``cmd.exe``。"""
        proto_process.write_state(str(self.env_dir), {"pid": 555, "image": "cmd"})
        with (
            mock.patch.object(proto_process, "_pid_alive", return_value=True),
            mock.patch.object(proto_process, "_pid_image", return_value="cmd.exe"),
        ):
            info = proto_process.managed_status(str(self.env_dir))
        self.assertTrue(info["managed"])
        self.assertEqual(info["pid"], 555)
        self.assertEqual(info["reason"], "")

    def test_status_ignores_image_path_and_case(self) -> None:
        proto_process.write_state(
            str(self.env_dir), {"pid": 556, "image": r"C:\Windows\System32\CMD.EXE"}
        )
        with (
            mock.patch.object(proto_process, "_pid_alive", return_value=True),
            mock.patch.object(proto_process, "_pid_image", return_value="cmd.exe"),
        ):
            info = proto_process.managed_status(str(self.env_dir))
        self.assertTrue(info["managed"])

    def test_stop_accepts_recorded_image_without_exe_suffix(self) -> None:
        """NapCat 是 ``cmd /c`` 起的；不能因为少个 .exe 就放弃停止自己起的进程树。"""
        proto_process.write_state(str(self.env_dir), {"pid": 557, "image": "cmd"})
        alive = {"value": True}

        def fake_alive(pid: int) -> bool:
            return alive["value"]

        def fake_terminate(pid: int) -> None:
            alive["value"] = False

        with (
            mock.patch.object(proto_process, "_pid_alive", side_effect=fake_alive),
            mock.patch.object(proto_process, "_pid_image", return_value="cmd.exe"),
            mock.patch.object(proto_process, "_terminate_tree", side_effect=fake_terminate) as killer,
        ):
            ok, detail = proto_process.stop_managed(str(self.env_dir))
        self.assertTrue(ok)
        self.assertIn("557", detail)
        killer.assert_called_once_with(557)

    def test_launch_records_pid(self) -> None:
        class _Proc:
            pid = 4321

        with mock.patch.object(proto_process.subprocess, "Popen", return_value=_Proc()) as popen:
            ok, detail = proto_process.launch_console(
                str(self.env_dir), "napcat", [sys.executable, "-c", "pass"], cwd=str(TEMP_ROOT)
            )
        self.assertTrue(ok)
        self.assertIn("4321", detail)
        popen.assert_called_once()
        state = proto_process.read_state(str(self.env_dir))
        self.assertEqual(state["pid"], 4321)
        self.assertEqual(state["backend"], "napcat")

    def test_launch_requires_env_dir(self) -> None:
        ok, detail = proto_process.launch_console("", "napcat", [sys.executable])
        self.assertFalse(ok)
        self.assertIn("环境目录", detail)


class SnowLumaCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.dir = _make_snowluma(TEMP_ROOT / "SnowLuma")

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_is_snowluma_dir(self) -> None:
        self.assertTrue(snowluma_proto.is_snowluma_dir(str(self.dir)))
        self.assertFalse(snowluma_proto.is_snowluma_dir(str(TEMP_ROOT / "nope")))

    def test_find_dir_honours_env_var(self) -> None:
        with mock.patch.dict(os.environ, {snowluma_proto.ENV_VAR: str(self.dir)}):
            self.assertEqual(snowluma_proto.find_dir(), str(self.dir))
        self.assertEqual(snowluma_proto.find_dir(str(self.dir)), str(self.dir))

    def test_read_endpoint_reads_per_uin_config(self) -> None:
        port, token = snowluma_proto.read_endpoint(str(self.dir))
        self.assertEqual(port, 3001)
        self.assertEqual(token, "snow-token-abc")

    def test_read_endpoint_prefers_newest_per_uin(self) -> None:
        newer = self.dir / "config" / "onebot_10002.json"
        _write(
            newer,
            json.dumps({"networks": {"wsServers": [{"port": 3999, "accessToken": "new"}]}}),
        )
        os.utime(newer, (2_000_000_000, 2_000_000_000))
        port, token = snowluma_proto.read_endpoint(str(self.dir))
        self.assertEqual((port, token), (3999, "new"))

    def test_read_endpoint_falls_back_to_global_config(self) -> None:
        empty = TEMP_ROOT / "global-only"
        _write(empty / "index.mjs", "// stub\n")
        _write(empty / "package.json", "{}")
        _write(
            empty / "config" / "onebot.json",
            json.dumps({"networks": {"wsServers": [{"port": 3002, "accessToken": "g"}]}}),
        )
        self.assertEqual(snowluma_proto.read_endpoint(str(empty)), (3002, "g"))

    def test_prepare_reports_consent_and_external(self) -> None:
        setup = snowluma_proto.prepare(str(self.dir))
        self.assertTrue(setup.available)
        self.assertEqual(setup.backend, "snowluma")
        self.assertEqual(setup.onebot_port, 3001)
        self.assertEqual(setup.token, "snow-token-abc")
        self.assertTrue(setup.external)
        # 没写 consent.json → 还没同意 EULA
        self.assertTrue(setup.needs_consent)
        self.assertEqual(setup.webui_port, snowluma_proto.DEFAULT_WEBUI_PORT)
        self.assertEqual(setup.ws_url, "ws://127.0.0.1:3001/")
        self.assertEqual(setup.label, "外部 SnowLuma（端口 3001）")

    def test_prepare_marks_consent_when_accepted(self) -> None:
        _write(self.dir / "config" / "consent.json", json.dumps({"accepted": True}))
        self.assertFalse(snowluma_proto.prepare(str(self.dir)).needs_consent)

    def test_prepare_without_dir_explains_licence_boundary(self) -> None:
        setup = snowluma_proto.prepare(str(TEMP_ROOT / "missing"))
        self.assertFalse(setup.available)
        self.assertIn("发行包不内置 SnowLuma", setup.reason)

    def test_prepare_without_ws_server_is_unavailable(self) -> None:
        bare = TEMP_ROOT / "bare"
        _write(bare / "index.mjs", "// stub\n")
        _write(bare / "package.json", "{}")
        setup = snowluma_proto.prepare(str(bare))
        self.assertFalse(setup.available)
        self.assertIn("OneBot 正向 WS", setup.reason)

    def test_launch_uses_bundled_node_and_cwd(self) -> None:
        setup = snowluma_proto.prepare(str(self.dir))
        with mock.patch.object(snowluma_proto.proto_process, "launch_console") as launch:
            launch.return_value = (True, "ok")
            ok, _detail = snowluma_proto.launch(setup)
        self.assertTrue(ok)
        args, kwargs = launch.call_args
        self.assertEqual(args[0], str(self.dir))
        self.assertEqual(args[1], "snowluma")
        self.assertEqual(args[2][1], "index.mjs")
        self.assertEqual(kwargs["cwd"], str(self.dir))

    def test_launch_without_index_fails(self) -> None:
        setup = ProtoSetup(available=False, backend="snowluma", env_dir=str(TEMP_ROOT / "x"))
        ok, detail = snowluma_proto.launch(setup)
        self.assertFalse(ok)
        self.assertIn("index.mjs", detail)


class ManagerCase(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.config_path = _write(TEMP_ROOT / "config" / "app.toml", APP_TOML)
        self.config_dir = self.config_path.parent
        self.env_path = self.config_dir / ".env"

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_configured_backend_new_install_defaults_to_napcat(self) -> None:
        """整个 ``[platform.qqonebot]`` 段不存在 = 这台机器还没配过 QQ = 新装。

        1.1.0 的默认协议端是随包内置的 NapCat（开箱即用）；SnowLuma 是可选的备用
        方案，只有用户显式选择才会生效。
        """
        self.assertEqual(proto_manager.configured_backend(str(self.config_path)), "napcat")
        self.assertEqual(proto_manager.configured_backend(""), "napcat")

    def test_configured_backend_legacy_section_without_key_is_napcat(self) -> None:
        """段存在但键缺失 = 1.0.x 老配置 → 升级不自动切换（仍是 NapCat）。"""
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"enabled": False}
        )
        self.assertEqual(proto_manager.configured_backend(str(self.config_path)), "napcat")

    def test_configured_backend_reads_section(self) -> None:
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"protocol_backend": "snowluma"}
        )
        self.assertEqual(proto_manager.configured_backend(str(self.config_path)), "snowluma")

    def test_prepare_dispatches_by_backend(self) -> None:
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"protocol_backend": "snowluma"}
        )
        with (
            mock.patch.object(proto_manager.snowluma_proto, "prepare") as snow,
            mock.patch.object(proto_manager.qqproto, "ensure_qqproto") as napcat,
        ):
            snow.return_value = ProtoSetup(available=True, backend="snowluma")
            setup = proto_manager.prepare(str(self.config_dir), app_toml=str(self.config_path))
        self.assertEqual(setup.backend, "snowluma")
        snow.assert_called_once()
        napcat.assert_not_called()

    def test_prepare_new_install_defaults_to_napcat(self) -> None:
        """新装（没有 qqonebot 段）走内置 NapCat，默认开箱即用。"""
        with (
            mock.patch.object(proto_manager.snowluma_proto, "prepare") as snow,
            mock.patch.object(proto_manager.qqproto, "ensure_qqproto") as napcat,
        ):
            napcat.return_value = ProtoSetup(available=True, backend="napcat")
            setup = proto_manager.prepare(str(self.config_dir), app_toml=str(self.config_path))
        self.assertEqual(setup.backend, "napcat")
        napcat.assert_called_once()
        snow.assert_not_called()

    def test_prepare_explicit_snowluma_dispatches_to_external(self) -> None:
        """只有显式写入 ``snowluma`` 时才走外置接入；默认不碰 SnowLuma。"""
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"protocol_backend": "snowluma"}
        )
        with (
            mock.patch.object(proto_manager.snowluma_proto, "prepare") as snow,
            mock.patch.object(proto_manager.qqproto, "ensure_qqproto") as napcat,
        ):
            snow.return_value = ProtoSetup(available=False, backend="snowluma")
            setup = proto_manager.prepare(str(self.config_dir), app_toml=str(self.config_path))
        self.assertEqual(setup.backend, "snowluma")
        snow.assert_called_once()
        napcat.assert_not_called()

    def test_prepare_legacy_section_stays_napcat(self) -> None:
        """老配置（段存在、键缺失）继续走内置 NapCat——升级不换协议端。"""
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"enabled": False}
        )
        with (
            mock.patch.object(proto_manager.snowluma_proto, "prepare") as snow,
            mock.patch.object(proto_manager.qqproto, "ensure_qqproto") as napcat,
        ):
            napcat.return_value = ProtoSetup(available=False, backend="napcat")
            setup = proto_manager.prepare(str(self.config_dir), app_toml=str(self.config_path))
        self.assertEqual(setup.backend, "napcat")
        napcat.assert_called_once()
        snow.assert_not_called()

    def test_unavailable_snowluma_mentions_bundled_napcat(self) -> None:
        """SnowLuma 没装、但包里有内置 NapCat 时，原因里要给出可切换提示。"""
        configedit.patch_section(
            str(self.config_path), "platform.qqonebot", {"protocol_backend": "snowluma"}
        )
        with (
            mock.patch.object(proto_manager.snowluma_proto, "prepare") as snow,
            mock.patch.object(proto_manager.qqproto, "bundle_env_root") as bundled,
        ):
            snow.return_value = ProtoSetup(
                available=False, backend="snowluma", reason="未找到 SnowLuma"
            )
            bundled.return_value = str(TEMP_ROOT / "qqproto")
            setup = proto_manager.prepare(str(self.config_dir), app_toml=str(self.config_path))
        self.assertFalse(setup.available)
        self.assertIn("未找到 SnowLuma", setup.reason)
        self.assertIn("内置 NapCat", setup.reason)

    def test_apply_connection_fills_empty_values(self) -> None:
        setup = ProtoSetup(
            available=True,
            backend="snowluma",
            onebot_port=3001,
            token="tok-1",
            env_dir=str(TEMP_ROOT / "SnowLuma"),
        )
        changed = proto_manager.apply_connection(
            setup, str(self.config_path), str(self.env_path)
        )
        self.assertTrue(changed)
        data = configedit.read_toml(str(self.config_path))
        section = data["platform"]["qqonebot"]
        self.assertEqual(section["ws_url"], "ws://127.0.0.1:3001/")
        self.assertEqual(section["access_token_env"], qqproto.ONEBOT_TOKEN_ENV)
        # 整段新建必须写全默认值：doctor 对"有段但缺 enabled"视为启用。
        self.assertIs(section["enabled"], False)
        self.assertEqual(section["protocol_backend"], "snowluma")
        self.assertEqual(section["trigger_keywords"], list(qqproto.DEFAULT_TRIGGER_KEYWORDS))
        self.assertEqual(
            configedit.read_env(str(self.env_path))[qqproto.ONEBOT_TOKEN_ENV], "tok-1"
        )

    def test_apply_connection_never_overwrites_existing_values(self) -> None:
        configedit.patch_section(
            str(self.config_path),
            "platform.qqonebot",
            {
                "ws_url": "ws://127.0.0.1:6700/",
                "access_token_env": "MY_OWN_TOKEN",
            },
        )
        before = self.config_path.read_text(encoding="utf-8")
        setup = ProtoSetup(
            available=True, backend="snowluma", onebot_port=3001, token="tok-1"
        )
        changed = proto_manager.apply_connection(
            setup, str(self.config_path), str(self.env_path)
        )
        self.assertFalse(changed)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertFalse(self.env_path.exists())

    def test_apply_connection_skips_unavailable_setup(self) -> None:
        self.assertFalse(
            proto_manager.apply_connection(
                ProtoSetup(available=False, reason="x"), str(self.config_path), ""
            )
        )

    def test_launch_and_stop_dispatch(self) -> None:
        napcat = ProtoSetup(available=True, backend="napcat", env_dir=str(TEMP_ROOT / "q"))
        snow = ProtoSetup(available=True, backend="snowluma", env_dir=str(TEMP_ROOT / "s"))
        with (
            mock.patch.object(proto_manager.qqproto, "launch_background", return_value=(True, "n")),
            mock.patch.object(proto_manager.snowluma_proto, "launch", return_value=(True, "s")),
        ):
            self.assertEqual(proto_manager.launch(napcat), (True, "n"))
            self.assertEqual(proto_manager.launch(snow), (True, "s"))
        with (
            mock.patch.object(proto_manager.qqproto, "stop_background", return_value=(True, "n")),
            mock.patch.object(proto_manager.snowluma_proto, "stop", return_value=(True, "s")),
        ):
            self.assertEqual(proto_manager.stop(napcat), (True, "n"))
            self.assertEqual(proto_manager.stop(snow), (True, "s"))


if __name__ == "__main__":
    unittest.main()
