"""「性能 / 并发」页用例：预设与读写、``/metrics`` 解析，以及真的建一次 Tk 页。

分两层：

* :class:`ConfigCase` 的子类（无 Tk）：预设的键集合与大小关系、``read_perf_config`` 的
  出厂默认值、``save_perf_config`` 写盘后三个小节都对且**不破坏** app.toml 的其它键与
  注释、写 ``[platform.qqonebot]`` 不带 ``enabled``、越界值抛错、``/metrics`` 解析容错、
  ``fetch_perf_metrics`` 的中文错误路径；
* :class:`TabCase`（需要 Tk，无显示环境整组 ``skipTest``）：页面建得起来且子控件都布局了、
  点预设只填输入框不落盘、保存真的写文件、``reload`` 能回读、后台刷新回调不炸。

临时目录固定用 ``tests/_tmp_gui_perf``：仓库规定不用 ``tempfile.mkdtemp()``（本机它按
``0o700`` 建目录，清理钩子会抛 ``PermissionError`` 并留下删不掉的残留）。
"""

from __future__ import annotations

import io
import shutil
import time
import tkinter as tk
import tomllib
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import configedit, perfconfig
from atri_x.gui.tabs.performance import PerformanceTab

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_perf"

#: 内置发行资产：拿真实的 app.toml / services.toml 测"出厂默认值"。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"
ASSET_FILES = ("app.toml", "services.toml", "state.toml")

#: ``[ops]`` 里本页**不**负责、保存后必须原样保留的键。
UNTOUCHED_OPS_KEYS = (
    "tool_timeout_seconds", "hook_timeout_seconds", "compress_timeout_seconds",
    "queue_max_per_user", "queue_max_per_scope", "provider_queue_max_size",
    "provider_wait_timeout_seconds",
)

#: 单调（越大越"放开"）的键；生图两个键 0 = 不限，单独判。
_MONOTONIC_KEYS = (
    "max_concurrent_turns", "max_concurrent_tools", "max_concurrent_hooks",
    "provider_max_concurrent", "queue_max_size", "queue_wait_timeout_seconds",
    "preprocess_workers", "preprocess_queue_size", "high_priority_workers",
)


def _prepare_config() -> configedit.ConfigPaths:
    """复制发行资产到临时目录（两个 SQLite 也指到临时目录，别碰用户真实数据）。"""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    app_path = config_dir / "app.toml"
    configedit.patch_section(
        str(app_path),
        "storage",
        {
            "sessions_sqlite_path": str(TEMP_ROOT / "data" / "atrix_sessions.db"),
            "chat_history_sqlite_path": str(TEMP_ROOT / "data" / "atrix_chat_history.db"),
        },
    )
    return configedit.ConfigPaths.from_config(str(app_path))


def _payload() -> dict:
    """假 ``/metrics``：字段名逐字照抄 :class:`atri_x.app.ops_health.OpsMetrics` 的 JSON。"""
    now = datetime.now(timezone.utc)

    def stamp(seconds: float) -> str:
        return (now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "version": "1.1.8",
        "collected_at": stamp(0),
        "uptime_seconds": 3725,
        "tasks": {
            "active": [
                {"id": "0123456789abcdef", "session_id": "qqonebot:g1", "kind": "turn",
                 "fair_key": "qqonebot:u1", "started_at": stamp(12)},
                {"id": "fedcba9876543210", "session_id": "cli:local", "kind": "tool",
                 "fair_key": "cli:local", "started_at": stamp(3)},
            ],
            "count_by_kind": {"turn": 1, "tool": 1},
            "queued_by_kind": {"turn": 2},
            "queue_busy_rejected": 3,
            "queue_full_rejected": 1,
            "queue_timeout_rejected": 4,
            "queue_cancel_rejected": 2,
            "queue_admitted": 7,
            "queue_average_wait_ms": 120,
            "oldest_queued_wait": 1.5,
            "oldest_age": 12.0,
        },
        "rate_limit": {
            "allowed": 100, "rejected": 3, "configured": False,
            "disabled_reason": "inbound rate limiting is not configured",
        },
        "image_limit": {"active": 2, "waiting": 1},
        "dsh": {
            "enabled": True, "available": True, "credentialUsable": True,
            "credentialProtocol": "anthropic", "credentialProvider": "deepseek",
            "running": True, "prewarmed": True, "active": 1, "maxConcurrent": 2,
            "busyWaitSeconds": 2.0, "stallTimeoutSeconds": 90.0,
            "idleTimeoutSeconds": 900.0, "affinityEnabled": True,
            "stats": {
                "admitted": 5, "busy_rejected": 2, "completed": 3, "failed": 1,
                "stalled": 0, "cold_starts": 1, "idle_closes": 0,
            },
            "affinity": {"entries": 1, "maxEntries": 4, "hits": 3},
        },
        "platforms": ["qqonebot", "cli"],
        "models": ["deepseek-v4", "gpt-4o"],
    }


class ConfigCase(unittest.TestCase):
    """每个用例一套独立的临时配置目录。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def read_toml(self, name: str) -> dict:
        with open(Path(self.paths.config_dir) / name, "rb") as handle:
            return tomllib.load(handle)

    def read_text(self, name: str) -> str:
        with open(Path(self.paths.config_dir) / name, encoding="utf-8") as handle:
            return handle.read()


# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------
class PresetTest(unittest.TestCase):
    def test_presets_share_keys_and_are_ordered(self) -> None:
        expected = set(perfconfig.FIELD_KEYS)
        self.assertEqual(len(expected), 11)
        self.assertEqual(len(perfconfig.PRESETS), 3)
        for name, preset in perfconfig.PRESETS.items():
            self.assertEqual(set(preset), expected, f"{name} 的键集合不一致")
            for key, value in preset.items():
                self.assertIsInstance(value, int, f"{name}.{key}")
                self.assertGreaterEqual(value, perfconfig.VALUE_MIN, f"{name}.{key}")
                self.assertLessEqual(value, perfconfig.VALUE_MAX, f"{name}.{key}")
        low = perfconfig.PRESETS[perfconfig.PRESET_LOW]
        normal = perfconfig.PRESETS[perfconfig.PRESET_NORMAL]
        high = perfconfig.PRESETS[perfconfig.PRESET_HIGH]
        for key in _MONOTONIC_KEYS:
            self.assertLessEqual(low[key], normal[key], key)
            self.assertLessEqual(normal[key], high[key], key)
        # 生图 0 = 不限：省资源收紧到 1 槽、标准沿用出厂值、高并发给 8 槽 + 排队。
        self.assertEqual(low["image_max_concurrent"], 1)
        self.assertEqual(normal["image_max_concurrent"], 0)
        self.assertEqual(high["image_max_concurrent"], 8)
        self.assertGreater(high["image_queue_size"], low["image_queue_size"])

    def test_standard_equals_factory_defaults(self) -> None:
        self.assertEqual(
            perfconfig.PRESETS[perfconfig.PRESET_NORMAL], perfconfig.PerfConfig().values()
        )
        fields = [
            name
            for name in perfconfig.PerfConfig.__dataclass_fields__
            if not name.endswith("_present")
        ]
        self.assertEqual(tuple(fields), perfconfig.FIELD_KEYS)


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------
class ReadWriteTest(ConfigCase):
    def test_factory_config_reads_as_standard(self) -> None:
        config = perfconfig.read_perf_config(self.paths)
        self.assertEqual(config.values(), perfconfig.PRESETS[perfconfig.PRESET_NORMAL])
        # 出厂 services.toml 有 [image_generation]；出厂 app.toml 的 QQ 段整段是注释。
        self.assertTrue(config.image_section_present)
        self.assertFalse(config.qq_section_present)
        self.assertEqual(
            perfconfig.read_perf_config(None).values(),
            perfconfig.PRESETS[perfconfig.PRESET_NORMAL],
        )

    def test_invalid_input_falls_back_per_key(self) -> None:
        configedit.patch_section(
            self.paths.config_path,
            "ops",
            {"max_concurrent_turns": -3, "queue_max_size": 999, "max_concurrent_tools": 8},
        )
        config = perfconfig.read_perf_config(self.paths)
        self.assertEqual(config.max_concurrent_turns, 10)  # 负数 → 标准
        self.assertEqual(config.queue_max_size, 20)  # >512 → 标准
        self.assertEqual(config.max_concurrent_tools, 8)  # 合法值保留

    def test_broken_toml_falls_back_to_defaults(self) -> None:
        with open(self.paths.config_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("[ops\nmax_concurrent_turns = = 3\n")
        self.assertEqual(
            perfconfig.read_perf_config(self.paths).values(),
            perfconfig.PRESETS[perfconfig.PRESET_NORMAL],
        )

    def test_save_writes_three_sections_and_keeps_rest(self) -> None:
        high = perfconfig.PRESETS[perfconfig.PRESET_HIGH]
        perfconfig.save_perf_config(self.paths, high)
        app, services = self.read_toml("app.toml"), self.read_toml("services.toml")

        self.assertEqual(app["ops"]["max_concurrent_turns"], high["max_concurrent_turns"])
        self.assertEqual(app["ops"]["provider_max_concurrent"], high["provider_max_concurrent"])
        self.assertEqual(app["ops"]["queue_max_size"], high["queue_max_size"])
        self.assertEqual(app["platform"]["qqonebot"]["preprocess_workers"], 8)
        self.assertEqual(app["platform"]["qqonebot"]["preprocess_queue_size"], 512)
        self.assertEqual(app["platform"]["qqonebot"]["high_priority_workers"], 2)
        self.assertEqual(services["image_generation"]["max_concurrent"], 8)
        self.assertEqual(services["image_generation"]["queue_size"], 16)

        # 别的小节、同小节的别的键、queue_wait_kinds 都不能被顺手改掉。
        self.assertIn("safety", app)
        self.assertEqual(app["ops"]["queue_wait_kinds"], ["turn", "tool", "hook", "compress"])
        self.assertEqual(app["ops"]["tool_timeout_seconds"], 600)
        for key in UNTOUCHED_OPS_KEYS:
            self.assertIn(key, app["ops"], key)
        # 注释也还在（行级补丁不重写整份文件）。
        self.assertIn("# 可选：超过并发上限时允许短暂排队", self.read_text("app.toml"))
        self.assertIn("# 生图独立并发限制", self.read_text("services.toml"))

    def test_qq_section_never_writes_enabled(self) -> None:
        perfconfig.save_perf_config(self.paths, perfconfig.PRESETS[perfconfig.PRESET_NORMAL])
        section = self.read_toml("app.toml")["platform"]["qqonebot"]
        self.assertNotIn("enabled", section)
        self.assertEqual(set(section), set(perfconfig.FIELD_KEYS[8:]))
        # 小节现在存在了，回读能看出来（界面据此收起那行提示）。
        self.assertTrue(perfconfig.read_perf_config(self.paths).qq_section_present)

    def test_saved_values_read_back(self) -> None:
        for name in (perfconfig.PRESET_LOW, perfconfig.PRESET_HIGH):
            perfconfig.save_perf_config(self.paths, perfconfig.PRESETS[name])
            self.assertEqual(
                perfconfig.read_perf_config(self.paths).values(), perfconfig.PRESETS[name]
            )

    def test_out_of_range_and_missing_key_raise(self) -> None:
        normal = perfconfig.PRESETS[perfconfig.PRESET_NORMAL]
        for bad in ({**normal, "max_concurrent_turns": -1}, {**normal, "queue_max_size": 513}):
            with self.assertRaises(perfconfig.PerfConfigError) as caught:
                perfconfig.save_perf_config(self.paths, bad)
            self.assertIn("必须落在", str(caught.exception))

        partial = dict(normal)
        del partial["image_queue_size"]
        with self.assertRaises(perfconfig.PerfConfigError) as caught:
            perfconfig.save_perf_config(self.paths, partial)
        self.assertIn("缺少这些并发参数", str(caught.exception))

        with self.assertRaises(perfconfig.PerfConfigError):
            perfconfig.save_perf_config(
                self.paths, {**normal, "preprocess_workers": "8"}  # type: ignore[dict-item]
            )


# ---------------------------------------------------------------------------
# ``/metrics`` 解析
# ---------------------------------------------------------------------------
class MetricsTest(unittest.TestCase):
    def test_summary_renders_payload(self) -> None:
        rows = dict(perfconfig.metrics_summary(_payload()))
        self.assertEqual(rows["版本"], "1.1.8")
        self.assertEqual(rows["运行时长"], "1 小时 2 分")
        self.assertEqual(rows["活跃 turn"], "1")
        self.assertEqual(rows["活跃 hook"], "0")  # 零值被 omitempty 吞了，仍要显示 0
        self.assertEqual(rows["排队 turn"], "2")
        self.assertEqual(rows["并发满被拒"], "3")
        self.assertEqual(rows["队满被拒"], "1")
        self.assertEqual(rows["排队超时"], "4")
        self.assertEqual(rows["排队取消"], "2")
        self.assertEqual(rows["平均排队等待"], "120 ms")
        self.assertEqual(rows["最老排队等待"], "1 秒")
        self.assertEqual(rows["最老运行中请求"], "12 秒")
        self.assertEqual(rows["生图闸门"], "active 2 / waiting 1")
        self.assertIn("放行 100 / 拒绝 3", rows["Provider 限速"])
        self.assertIn("未启用限流", rows["Provider 限速"])
        self.assertIn("active 1 / max 2", rows["DSH 并发"])
        self.assertEqual(rows["DSH 忙拒 / 卡死"], "2 / 0")
        self.assertEqual(rows["DSH 完成 / 失败"], "3 / 1")
        self.assertEqual(rows["DSH 冷启动 / 空闲回收"], "1 / 0")
        self.assertEqual(rows["DSH 会话亲和"], "1 / 4 个（命中 3）")
        self.assertEqual(rows["DSH 预热"], "已预热")
        self.assertIn("deepseek", rows["DSH 凭据协议"])
        self.assertEqual(rows["平台"], "qqonebot、cli")
        self.assertEqual(rows["模型"], "2 个")

    def test_summary_tolerates_missing_and_wrong_types(self) -> None:
        rows = dict(perfconfig.metrics_summary({}))
        self.assertEqual(rows["版本"], "未知")
        self.assertEqual(rows["运行时长"], "—")
        self.assertEqual(rows["活跃 turn"], "0")
        self.assertEqual(rows["生图闸门"], "active 0 / waiting 0")
        self.assertFalse(any(label.startswith("DSH") for label in rows))
        self.assertEqual(dict(perfconfig.metrics_summary(None))["版本"], "未知")
        rows = dict(
            perfconfig.metrics_summary(
                {
                    "version": 5, "uptime_seconds": "soon", "tasks": "oops",
                    "rate_limit": 7, "image_limit": None, "platforms": 3,
                    "models": {}, "collected_at": ["x"], "dsh": "oops",
                }
            )
        )
        self.assertEqual(rows["版本"], "未知")
        self.assertEqual(rows["模型"], "0 个")
        self.assertEqual(rows["平台"], "—")
        self.assertFalse(any(label.startswith("DSH") for label in rows))

    def test_active_rows(self) -> None:
        rows = perfconfig.active_request_rows(_payload())
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["kind"], "turn")
        self.assertEqual(first["session_id"], "qqonebot:g1")
        self.assertEqual(first["fair_key"], "qqonebot:u1")
        self.assertEqual(first["id"], "01234567")  # 只留前 8 位
        self.assertGreaterEqual(first["running"], 10.0)
        self.assertLess(first["running"], 60.0)
        self.assertEqual(rows[1]["kind"], "tool")

    def test_active_rows_tolerate_missing_fields(self) -> None:
        self.assertEqual(perfconfig.active_request_rows({}), [])
        self.assertEqual(perfconfig.active_request_rows({"tasks": {"active": "no"}}), [])
        rows = perfconfig.active_request_rows({"tasks": {"active": [{}]}})
        self.assertEqual((rows[0]["kind"], rows[0]["running"]), ("", None))


# ---------------------------------------------------------------------------
# ``/metrics`` 错误路径
# ---------------------------------------------------------------------------
class _RaisingOpener:
    """把 ``build_opener`` 换成它，让请求按指定状态码失败。"""

    def __init__(self, code: int) -> None:
        self.code = code

    def open(self, request, timeout=None):  # noqa: D401 - 仿 urllib 的 opener
        raise urllib.error.HTTPError(request.full_url, self.code, "err", None, io.BytesIO(b"{}"))


class FetchErrorTest(ConfigCase):
    def _credentials(self, addr: str, token: str):
        return mock.patch.object(
            perfconfig.timeview, "diagnostics_credentials", return_value=(addr, token)
        )

    def _fetch_error(self, code: int) -> str:
        with self._credentials("127.0.0.1:32171", "token"):
            with mock.patch.object(
                perfconfig.urllib.request, "build_opener", return_value=_RaisingOpener(code)
            ):
                with self.assertRaises(perfconfig.PerfConfigError) as caught:
                    perfconfig.fetch_perf_metrics(self.paths)
        return str(caught.exception)

    def test_missing_address_and_token(self) -> None:
        with self._credentials("", "token"):
            with self.assertRaises(perfconfig.PerfConfigError) as caught:
                perfconfig.fetch_perf_metrics(self.paths)
        self.assertIn("未配置后台状态接口地址", str(caught.exception))
        with self._credentials("127.0.0.1:32171", ""):
            with self.assertRaises(perfconfig.PerfConfigError) as caught:
                perfconfig.fetch_perf_metrics(self.paths)
        self.assertIn("缺少后台状态接口令牌", str(caught.exception))

    def test_http_errors_are_chinese(self) -> None:
        self.assertIn("鉴权失败", self._fetch_error(401))
        message = self._fetch_error(404)
        self.assertIn("/metrics", message)
        self.assertIn("旧版本", message)
        self.assertIn("ATRIX_OPS_TOKEN", message)

    def test_unreachable_backend_and_endpoint_mapping(self) -> None:
        with self._credentials("127.0.0.1:1", "token"):
            with self.assertRaises(perfconfig.PerfConfigError) as caught:
                perfconfig.fetch_perf_metrics(self.paths, timeout=0.5)
        self.assertIn("不可达", str(caught.exception))
        self.assertEqual(
            perfconfig.metrics_endpoint("0.0.0.0:32171"), "http://127.0.0.1:32171/metrics"
        )
        with self.assertRaises(perfconfig.PerfConfigError):
            perfconfig.metrics_endpoint("")


# ---------------------------------------------------------------------------
# Tk 页
# ---------------------------------------------------------------------------
class TabCase(unittest.TestCase):
    """一个隐藏的 Tk 根窗口 + 一套临时配置；没有 Tk 就跳过整组。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()
        # 失败路径会弹 messagebox，模态框在无人值守的测试里会一直等点击。
        self._dialogs = [
            mock.patch("tkinter.messagebox.showerror", return_value=None),
            mock.patch("tkinter.messagebox.showinfo", return_value=None),
            mock.patch("tkinter.messagebox.askyesno", return_value=False),
        ]
        for item in self._dialogs:
            item.start()

    def tearDown(self) -> None:
        for item in self._dialogs:
            item.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def build(self) -> PerformanceTab:
        frame = ttk.Frame(self.root)
        frame.pack()
        return PerformanceTab(frame, self.paths)

    def read_toml(self, name: str) -> dict:
        with open(Path(self.paths.config_dir) / name, "rb") as handle:
            return tomllib.load(handle)

    def read_text(self, name: str) -> str:
        with open(Path(self.paths.config_dir) / name, encoding="utf-8") as handle:
            return handle.read()

    def wait(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_builds_and_lays_out(self) -> None:
        tab = self.build()
        self.root.update()
        children = tab.body.winfo_children()
        self.assertGreater(len(children), 5)
        notices = (tab._qq_notice, tab._image_notice)  # 这两行按需要才 pack
        for child in children:
            if child in notices:
                continue
            self.assertTrue(child.winfo_manager(), f"{child} 没有被布局")
        self.assertEqual(set(tab._vars), set(perfconfig.FIELD_KEYS))
        for name in perfconfig.PRESETS:
            self.assertTrue(tab.preset_buttons[name].winfo_manager())
        self.assertEqual(tab.summary_text.winfo_manager(), "pack")
        self.assertEqual(tab.request_tree.winfo_manager(), "pack")
        # 出厂没有 [platform.qqonebot]：提示行应该已经插在 QQ 组上方；生图小节存在，不提示。
        self.assertTrue(tab._qq_notice.winfo_manager())
        self.assertIn("不会因此启用 QQ 平台", tab._qq_notice.cget("text"))
        self.assertEqual(tab._image_notice.winfo_manager(), "")

    def test_preset_only_fills_boxes(self) -> None:
        tab = self.build()
        before = self.read_text("app.toml")
        tab.preset_buttons[perfconfig.PRESET_HIGH].invoke()
        self.root.update()
        for key, value in perfconfig.PRESETS[perfconfig.PRESET_HIGH].items():
            self.assertEqual(tab._vars[key].get(), str(value), key)
        self.assertEqual(self.read_text("app.toml"), before)  # 没落盘

    def test_save_writes_files_then_reload_reads_back(self) -> None:
        tab = self.build()
        tab.apply_preset(perfconfig.PRESET_HIGH)
        with mock.patch("atri_x.gui.tabs.performance.confirm", return_value=True):
            tab._save()
        app = self.read_toml("app.toml")
        self.assertEqual(app["ops"]["max_concurrent_turns"], 32)
        self.assertEqual(app["ops"]["queue_wait_kinds"], ["turn", "tool", "hook", "compress"])
        self.assertEqual(app["platform"]["qqonebot"]["preprocess_workers"], 8)
        self.assertNotIn("enabled", app["platform"]["qqonebot"])
        self.assertEqual(self.read_toml("services.toml")["image_generation"]["queue_size"], 16)
        self.assertIn("已保存；重启后台服务后生效", tab._status.get())
        self.assertEqual(tab._qq_notice.cget("text"), "")  # 小节已存在，提示收起

        # 文件被别处改过（这里用数据层改），reload 应该把输入框拉回文件里的值。
        perfconfig.save_perf_config(self.paths, perfconfig.PRESETS[perfconfig.PRESET_LOW])
        tab.reload()
        self.root.update()
        for key, value in perfconfig.PRESETS[perfconfig.PRESET_LOW].items():
            self.assertEqual(tab._vars[key].get(), str(value), key)

    def test_save_cancelled_and_bad_input_keep_file(self) -> None:
        tab = self.build()
        tab.apply_preset(perfconfig.PRESET_HIGH)
        before = self.read_text("app.toml")
        with mock.patch("atri_x.gui.tabs.performance.confirm", return_value=False):
            tab._save()
        self.assertEqual(self.read_text("app.toml"), before)
        self.assertIn("已取消保存", tab._status.get())

        tab._vars["max_concurrent_turns"].set("很多")
        with mock.patch("atri_x.gui.tabs.performance.confirm", return_value=True):
            tab._save()
        self.assertEqual(self.read_text("app.toml"), before)
        self.assertIn("必须是整数", tab._status.get())

    def test_refresh_renders_backend_payload(self) -> None:
        tab = self.build()
        with mock.patch.object(perfconfig, "fetch_perf_metrics", return_value=_payload()):
            tab.refresh_now()
            self.assertTrue(self.wait(lambda: not tab._inflight), "后台回调没在超时内回到主线程")
        self.root.update()
        text = tab.summary_text.get("1.0", tk.END)
        self.assertIn("版本：1.1.8", text)
        self.assertIn("排队 turn：2", text)
        self.assertEqual(len(tab.request_tree.get_children("")), 2)
        self.assertIn("已读取后台并发实况", tab._live_status.cget("text"))
        running = tab.request_tree.item("perf-request-0", "values")[3]
        self.assertTrue(running.endswith(" 秒"), running)
        self.assertGreaterEqual(float(str(running).split()[0]), 10.0)

    def test_refresh_error_lands_in_status_line(self) -> None:
        tab = self.build()
        error = perfconfig.PerfConfigError("后台状态接口不可达：refused（后台服务没启动？）")
        with mock.patch.object(perfconfig, "fetch_perf_metrics", side_effect=error):
            tab.refresh_now()
            self.assertTrue(self.wait(lambda: not tab._inflight))
        status = tab._live_status.cget("text")
        self.assertIn("读取失败", status)
        self.assertIn("不可达", status)

    def test_auto_refresh_respects_page_visibility(self) -> None:
        tab = self.build()
        with mock.patch.object(perfconfig, "fetch_perf_metrics", return_value=_payload()):
            tab.auto_var.set(True)
            tab.on_show()
            self.assertTrue(self.wait(lambda: tab._live_status.cget("text").startswith("已读取")))
            tab.on_hide()  # 切走 = 停轮询
            self.assertIsNone(tab._job)
            self.assertFalse(tab._visible)
            tab.auto_var.set(False)
            tab.on_show()  # 关了自动刷新时切回来也不该自己排任务
            self.assertIsNone(tab._job)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
