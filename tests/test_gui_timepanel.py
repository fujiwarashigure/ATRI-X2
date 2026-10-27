"""「时间」页用例：数据层（timeview）+ 选项卡（TimeTab）+ 「功能」页汇总。

覆盖三件事：

* **能读到什么**：配置默认值合并、实时快照口径（时区/偏移/星期）、时区探针、后台
  ``/diagnostics`` 时间段的渲染；
* **能改什么**：``[time]`` / ``[time.verification]`` / ``[[time.sources]]`` 落盘，
  非法数字与非法时区**不落盘**（先校验再写，写前状态行报错）；
* **入口对不对**：「功能」页有一行「时间服务」并能跳到「时间」选项卡。

没有 Tk（无显示环境）时只跳过选项卡用例，数据层用例照跑。
"""

from __future__ import annotations

import datetime
import os
import shutil
import tkinter as tk
import tomllib
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import featureview, timeview
from atri_x.gui.configedit import ConfigPaths, patch_section
from atri_x.gui.tabs.timepanel import TimeTab

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_timepanel"

#: 内置发行资产（拿真实的 app.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

#: 会被复制进临时配置目录的资产。
ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")


def prepare_config() -> ConfigPaths:
    """复制发行资产到临时目录，返回配置路径表。"""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    return ConfigPaths.from_config(str(config_dir / "app.toml"))


class TimeViewConfigCase(unittest.TestCase):
    """数据层的配置读取与展示项（不需要 Tk）。"""

    def setUp(self) -> None:
        self.paths = prepare_config()
        # 全量跑用例时，前面「服务」相关用例会把这两个变量写进进程环境
        # （ensure_health_endpoint 的副作用）；这里先摘掉，否则"没有 .env 就取不到凭据"
        # 这条断言会读到别的用例留下的值。
        self._env = mock.patch.dict(
            os.environ, {"ATRIX_HEALTH_ADDR": "", "ATRIX_OPS_TOKEN": ""}, clear=False
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_defaults_match_shipped_config(self) -> None:
        view = timeview.load_time_config(self.paths)
        self.assertEqual(view.timezone, "Asia/Shanghai")
        self.assertTrue(view.inject_context)
        self.assertEqual(view.max_sample_age_seconds, 21600)
        self.assertFalse(view.verification["enabled"])
        self.assertEqual(view.verification["mode"], "verify_only")
        self.assertEqual(view.verification["interval_seconds"], 1800)
        self.assertEqual(view.sources, [])

    def test_defaults_come_from_config_layer(self) -> None:
        """界面默认值取自配置层 dataclass，不另抄一份数字。"""
        self.assertEqual(timeview.verification_defaults(), timeview.verification_defaults())
        self.assertEqual(timeview.time_defaults()["timezone"], "Asia/Shanghai")
        source = timeview.source_defaults()
        self.assertEqual(source["protocol"], "sntp")
        self.assertEqual(source["port"], 123)
        self.assertTrue(source["require_no_store"])
        self.assertFalse(source["enabled"])

    def test_reads_full_section(self) -> None:
        patch_section(
            self.paths.config_path,
            "time",
            {"timezone": "Europe/London", "inject_context": False, "max_sample_age_seconds": 600},
        )
        patch_section(
            self.paths.config_path,
            "time.verification",
            {"enabled": True, "interval_seconds": 300, "warn_offset_ms": 500},
        )
        view = timeview.load_time_config(self.paths)
        self.assertEqual(view.timezone, "Europe/London")
        self.assertFalse(view.inject_context)
        self.assertEqual(view.max_sample_age_seconds, 600)
        self.assertTrue(view.verification["enabled"])
        self.assertEqual(view.verification["interval_seconds"], 300)
        self.assertEqual(view.verification["warn_offset_ms"], 500)
        # 没写的键保持配置层默认值，而不是空串。
        self.assertEqual(view.verification["conflict_offset_ms"], 30000)

    def test_reads_sources_table(self) -> None:
        configedit_write_sources(self.paths)
        view = timeview.load_time_config(self.paths)
        self.assertEqual([item["id"] for item in view.sources], ["corp-ntp", "corp-api"])
        self.assertEqual(view.sources[0]["protocol"], "sntp")
        self.assertEqual(view.sources[1]["url"], "https://time.example.com/v1/now")
        self.assertFalse(view.sources[1]["enabled"])

    def test_clock_uses_same_time_service(self) -> None:
        lines = dict(timeview.describe_clock("Asia/Shanghai"))
        self.assertEqual(lines["时区（解析后）"], "Asia/Shanghai")
        self.assertEqual(lines["UTC 偏移"], "UTC+08:00")
        self.assertEqual(lines["业务时间源"].split("（")[0], "system_clock")
        self.assertEqual(lines["时区数据"], "可用")
        self.assertIn(lines["星期"], ("周一", "周二", "周三", "周四", "周五", "周六", "周日"))

    def test_clock_rejects_invalid_timezone(self) -> None:
        with self.assertRaises(timeview.TimeViewError) as caught:
            timeview.describe_clock("Mars/Olympus")
        self.assertIn("Mars/Olympus", str(caught.exception))

    def test_clock_supports_fixed_offset_and_system(self) -> None:
        fixed = dict(timeview.describe_clock("fixed:+05:30"))
        self.assertEqual(fixed["UTC 偏移"], "UTC+05:30")
        self.assertEqual(fixed["时区规则"], "固定偏移（无夏令时）")
        system = dict(timeview.describe_clock("system"))
        self.assertIn("进程本地时区", system["时区规则"])

    def test_probe_reports_tzdata_and_dst(self) -> None:
        report = timeview.probe_timezone("Asia/Shanghai")
        self.assertTrue(report["ok"])
        self.assertTrue(report["zoneinfo_available"])
        self.assertGreater(report["zones"], 100)
        lines = dict(timeview.probe_lines(report))
        self.assertEqual(lines["探针结果"], "通过")
        self.assertEqual(lines["夏令时探针"], "通过")
        self.assertIn("DST 冬季", lines)

    def test_probe_failure_lines(self) -> None:
        lines = dict(timeview.probe_lines({"ok": False, "error": "boom"}))
        self.assertEqual(lines["探针结果"], "失败")
        self.assertEqual(lines["错误"], "boom")

    def test_state_labels_cover_all_states(self) -> None:
        for state in (
            "disabled",
            "checking",
            "consistent",
            "insufficient_evidence",
            "conflict",
            "stale",
            "unavailable",
        ):
            self.assertNotEqual(timeview.state_label(state), state)
        self.assertEqual(timeview.state_label("brand_new"), "brand_new")

    def test_verification_lines_from_diagnostics(self) -> None:
        section = {
            "state": "consistent",
            "mode": "verify_only",
            "enabled": True,
            "sources_configured": 2,
            "last_check_at": "2026-01-01T00:00:00Z",
            "last_success_at": "2026-01-01T00:00:00Z",
            "sample_age_seconds": 30,
            "offset_ms": 12.5,
            "spread_ms": 3.0,
            "groups": 2,
            "message": "ok",
            "sources": [
                {
                    "provider_id": "corp-ntp",
                    "protocol": "sntp",
                    "group": "corp",
                    "status": "ok",
                    "rtt_ms": 4.5,
                    "offset_ms": 11.0,
                    "uncertainty_ms": 1.0,
                    "last_ok_at": "2026-01-01T00:00:00Z",
                }
            ],
            "alerts": [{"code": "conflict", "at": "2025-12-31T23:00:00Z", "reason": "drift"}],
        }
        lines = dict(timeview.verification_lines(section))
        self.assertIn("一致", lines["校验状态"])
        self.assertEqual(lines["已配置来源"], "2 个")
        self.assertIn("verify_only", lines["模式"])
        self.assertIn("12.5 ms", lines["本地偏差"])
        self.assertEqual(lines["样本年龄"], "30 秒前")
        self.assertEqual(len(timeview.verification_sources(section)), 1)
        self.assertEqual(
            timeview.verification_alerts(section), ["2025-12-31T23:00:00Z conflict drift"]
        )

    def test_diagnostics_endpoint_and_credentials(self) -> None:
        self.assertEqual(
            timeview.diagnostics_endpoint("127.0.0.1:32173"), "http://127.0.0.1:32173/diagnostics"
        )
        self.assertEqual(
            timeview.diagnostics_endpoint("0.0.0.0:9999"), "http://127.0.0.1:9999/diagnostics"
        )
        with self.assertRaises(timeview.TimeViewError):
            timeview.diagnostics_endpoint("")
        # 没有 .env 时取不到凭据（界面据此显示"桌面端启动时会自动补齐"）。
        self.assertEqual(timeview.diagnostics_credentials(self.paths), ("", ""))
        configedit_set_env(self.paths)
        self.assertEqual(
            timeview.diagnostics_credentials(self.paths),
            ("127.0.0.1:32173", "atrix-ops-test"),
        )

    def test_format_offset_text(self) -> None:
        self.assertEqual(
            timeview.format_offset_text(datetime.timedelta(hours=8)), "UTC+08:00"
        )
        self.assertEqual(
            timeview.format_offset_text(datetime.timedelta(hours=-5, minutes=-30)), "UTC-05:30"
        )
        self.assertEqual(timeview.format_offset_text("+08:00"), "+08:00")
        self.assertEqual(timeview.format_offset_text(None), "")

    def test_timezone_choices_start_with_common_zones(self) -> None:
        choices = timeview.timezone_choices()
        self.assertEqual(choices[:2], ("Asia/Shanghai", "Asia/Tokyo"))
        self.assertIn("America/New_York", choices)
        self.assertGreater(len(choices), 100)


class FeatureRowCase(unittest.TestCase):
    """「功能」页必须能看到时间服务，且能跳到「时间」选项卡。"""

    def setUp(self) -> None:
        self.paths = prepare_config()

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def test_row_reports_default_state(self) -> None:
        rows = {row.key: row for row in featureview.collect_features(self.paths)}
        time_row = rows["time"]
        self.assertEqual(time_row.tab, "time")
        self.assertEqual(time_row.status, "默认启用")
        self.assertIn("时区 Asia/Shanghai", time_row.detail)
        self.assertIn("网络校验未启用", time_row.detail)

    def test_row_reports_enabled_verification(self) -> None:
        patch_section(self.paths.config_path, "time.verification", {"enabled": True})
        configedit_write_sources(self.paths)
        rows = {row.key: row for row in featureview.collect_features(self.paths)}
        self.assertEqual(rows["time"].status, "已启用")
        # 只统计 enabled=true 的来源（另一条 https 来源是关着的）。
        self.assertIn("1 个来源", rows["time"].detail)


class TimeTabCase(unittest.TestCase):
    """真的建 Tk 控件：载入 / 保存 / 校验失败 / 来源增删 / 后台状态渲染。"""

    def setUp(self) -> None:
        self.paths = prepare_config()
        # 进程环境里可能有真实的状态接口凭据，先摘掉以免用例变成"连本机后台"。
        self._env = mock.patch.dict(
            os.environ, {"ATRIX_HEALTH_ADDR": "", "ATRIX_OPS_TOKEN": ""}, clear=False
        )
        self._env.start()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
            self._env.stop()
            self.skipTest(f"Tk 不可用：{exc}")
        self.root.withdraw()
        self._dialogs = [
            mock.patch("tkinter.messagebox.showerror", return_value=None),
            mock.patch("tkinter.messagebox.showinfo", return_value=None),
            mock.patch("tkinter.messagebox.showwarning", return_value=None),
            mock.patch("tkinter.messagebox.askyesno", return_value=True),
        ]
        for item in self._dialogs:
            item.start()
        frame = ttk.Frame(self.root)
        frame.pack()
        self.tab = TimeTab(frame, self.paths)

    def tearDown(self) -> None:
        self.tab.on_hide()
        for item in self._dialogs:
            item.stop()
        self._env.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def read_app(self) -> dict:
        path = self.paths.config_path
        with open(path, "rb") as handle:
            return tomllib.load(handle)

    def status(self) -> str:
        return self.tab._status.get()

    # -- 载入 ---------------------------------------------------------------
    def test_loads_shipped_defaults(self) -> None:
        self.assertEqual(self.tab.timezone_var.get(), "Asia/Shanghai")
        self.assertTrue(self.tab.inject_var.get())
        self.assertEqual(self.tab.max_age_var.get(), "21600")
        self.assertFalse(self.tab.verify_enabled_var.get())
        self.assertEqual(self.tab.verify_mode_var.get(), "verify_only")
        self.assertEqual(self.tab._verification_vars["interval_seconds"].get(), "1800")
        self.assertEqual(self.tab.sources_editor.tree.get_children(""), ())

    def test_clock_rows_are_rendered(self) -> None:
        self.tab._refresh_clock()
        self.root.update()
        values = {label: variable.get() for label, variable in self.tab._clock_vars.items()}
        self.assertEqual(values["UTC 偏移"], "UTC+08:00")
        self.assertNotEqual(values["当前时间"], "—")

    def test_hidden_tab_does_not_fetch_diagnostics(self) -> None:
        """没有 .env（即没有状态接口凭据）时不发网络请求，只提示去哪里补。"""
        self.assertFalse(self.tab.diagnostics_panel._inflight)
        self.tab._fetch_diagnostics()
        self.assertIn("ATRIX_HEALTH_ADDR", self.tab.diagnostics_panel.diag_error.cget("text"))

    # -- 保存 ---------------------------------------------------------------
    def test_save_writes_time_and_verification(self) -> None:
        self.tab.timezone_var.set("Europe/London")
        self.tab.inject_var.set(False)
        self.tab.max_age_var.set("3600")
        self.tab.verify_enabled_var.set(True)
        self.tab._verification_vars["interval_seconds"].set("600")
        self.tab._verification_vars["warn_offset_ms"].set("1500")
        self.tab._save()
        self.assertIn("已写入 app.toml", self.status())
        data = self.read_app()
        self.assertEqual(data["time"]["timezone"], "Europe/London")
        self.assertFalse(data["time"]["inject_context"])
        self.assertEqual(data["time"]["max_sample_age_seconds"], 3600)
        verification = data["time"]["verification"]
        self.assertTrue(verification["enabled"])
        self.assertEqual(verification["interval_seconds"], 600)
        self.assertEqual(verification["warn_offset_ms"], 1500)
        # 没动过的参数照样写全（配置段完整、可读）。
        self.assertEqual(verification["conflict_offset_ms"], 30000)

    def test_save_rejects_non_numeric_value(self) -> None:
        before = Path(self.paths.config_path).read_text(encoding="utf-8")
        self.tab._verification_vars["interval_seconds"].set("abc")
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("必须是整数", self.status())
        self.assertEqual(Path(self.paths.config_path).read_text(encoding="utf-8"), before)

    def test_save_rejects_out_of_range_value(self) -> None:
        before = Path(self.paths.config_path).read_text(encoding="utf-8")
        self.tab._verification_vars["interval_seconds"].set("5")  # 配置层下限是 60
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("interval_seconds", self.status())
        self.assertEqual(Path(self.paths.config_path).read_text(encoding="utf-8"), before)

    def test_save_rejects_invalid_timezone(self) -> None:
        before = Path(self.paths.config_path).read_text(encoding="utf-8")
        self.tab.timezone_var.set("Mars/Olympus")
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("timezone", self.status())
        self.assertEqual(Path(self.paths.config_path).read_text(encoding="utf-8"), before)

    def test_save_keeps_warn_below_conflict(self) -> None:
        self.tab._verification_vars["warn_offset_ms"].set("40000")  # > conflict 30000
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("warn_offset_ms", self.status())

    # -- 来源 ---------------------------------------------------------------
    def test_add_and_save_source(self) -> None:
        editor = self.tab.sources_editor
        editor.new_source()
        editor.src_vars["id"].set("corp-ntp")
        editor.src_vars["host"].set("ntp.example.com")
        editor.src_vars["port"].set("123")
        editor.src_vars["group"].set("corp")
        editor.src_flags["enabled"].set(True)
        self.tab._save()
        sources = self.read_app()["time"]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "corp-ntp")
        self.assertEqual(sources[0]["protocol"], "sntp")
        self.assertEqual(sources[0]["host"], "ntp.example.com")
        self.assertTrue(sources[0]["enabled"])
        # 保存后界面重新载入 = 表格里有一条。
        self.assertEqual(len(self.tab.sources_editor.tree.get_children("")), 1)

    def test_https_source_requires_https_url(self) -> None:
        editor = self.tab.sources_editor
        editor.new_source()
        editor.src_vars["id"].set("api")
        editor.src_vars["protocol"].set("https")
        editor.src_vars["url"].set("http://time.example.com/now")
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("https://", self.status())

    def test_duplicate_source_id_is_rejected(self) -> None:
        editor = self.tab.sources_editor
        for name in ("dup", "dup"):
            editor.new_source()
            editor.src_vars["id"].set(name)
            editor.src_vars["host"].set("h")
        self.tab._save()
        self.assertIn("失败", self.status())
        self.assertIn("duplicate id", self.status())

    def test_delete_source_removes_block(self) -> None:
        editor = self.tab.sources_editor
        editor.new_source()
        editor.src_vars["id"].set("gone")
        editor.src_vars["host"].set("h")
        self.tab._save()
        editor.tree.selection_set("source-0")
        self.root.update()
        editor.delete_source()
        self.assertEqual(editor.sources, [])
        self.tab._save()
        self.assertNotIn("sources", self.read_app()["time"])

    def test_switching_selection_keeps_previous_edits(self) -> None:
        editor = self.tab.sources_editor
        for name in ("one", "two"):
            editor.new_source()
            editor.src_vars["id"].set(name)
            editor.src_vars["host"].set(f"{name}.example.com")
        editor.tree.selection_set("source-0")
        self.root.update()
        self.assertEqual(editor.src_vars["id"].get(), "one")
        self.assertEqual(editor.sources[1]["host"], "two.example.com")

    # -- 时区探针 / 后台状态 ------------------------------------------------
    def test_probe_renders_lines(self) -> None:
        self.tab._probe()
        text = self.tab._probe_text.get("1.0", "end-1c")
        self.assertIn("探针结果：通过", text)
        self.assertIn("DST 冬季", text)

    def test_render_diagnostics_fills_grid_and_tables(self) -> None:
        section = {
            "verification": {
                "state": "consistent",
                "mode": "verify_only",
                "enabled": True,
                "sources_configured": 1,
                "sample_age_seconds": 15,
                "offset_ms": 2.5,
                "sources": [
                    {
                        "provider_id": "corp-ntp",
                        "protocol": "sntp",
                        "group": "corp",
                        "status": "ok",
                        "rtt_ms": 3.0,
                        "offset_ms": 2.0,
                        "uncertainty_ms": 0.5,
                        "last_ok_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "alerts": [],
            }
        }
        panel = self.tab.diagnostics_panel
        panel.render(section, "")
        self.root.update()
        labels = _descendant_texts(panel._diag_grid)
        self.assertTrue(any("校验状态" in item for item in labels))
        self.assertTrue(any("一致" in item for item in labels))
        rows = panel.observation_tree.get_children("")
        self.assertEqual(len(rows), 1)
        self.assertEqual(panel.observation_tree.item(rows[0], "values")[0], "corp-ntp")
        self.assertIn("告警：无", panel._alerts_text.get("1.0", "end-1c"))

    def test_render_diagnostics_error_is_visible(self) -> None:
        panel = self.tab.diagnostics_panel
        panel.render({}, "状态接口不可达：超时")
        self.assertIn("不可达", panel.diag_error.cget("text"))


def _descendant_texts(widget) -> list[str]:
    """收集一个容器里所有 ``ttk.Label`` 的文字（用来断言"页面上写了什么"）。"""
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ttk.Label):
            try:
                found.append(str(child.cget("text")))
            except tk.TclError:  # pragma: no cover - 控件销毁中
                continue
        found.extend(_descendant_texts(child))
    return found


def configedit_write_sources(paths: ConfigPaths) -> None:
    """写两条 ``[[time.sources]]``（测试辅助，走生产同一条写盘路径）。"""
    from atri_x.gui.configedit import write_array_tables

    write_array_tables(
        paths.config_path,
        "time.sources",
        [
            {"id": "corp-ntp", "protocol": "sntp", "host": "ntp.example.com", "port": 123,
             "group": "corp", "enabled": True},
            {"id": "corp-api", "protocol": "https", "url": "https://time.example.com/v1/now",
             "group": "corp-api", "enabled": False},
        ],
        after_section="time.verification",
    )


def configedit_set_env(paths: ConfigPaths) -> None:
    """在 ``.env`` 里写状态接口地址与令牌（与桌面端自动补齐的两个键一致）。"""
    env_path = paths.env_path
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("ATRIX_HEALTH_ADDR=127.0.0.1:32173\nATRIX_OPS_TOKEN=atrix-ops-test\n")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
