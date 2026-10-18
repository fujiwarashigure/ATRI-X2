"""「定时维护任务」的调度翻译与二级窗口：结构化选项 → app.toml 的 cron / run_at。

分两层：

- :class:`ScheduleCronCase` —— 纯函数（不建窗口）：每天/每周/每月 ↔ cron、人话、
  下次触发、一次性时间格式、表单 ↔ cron 的校验与还原；
- :class:`MaintenanceDialogCase` —— 真的建 Tk 窗口，把四种周期写法 + 一次性各点一遍，
  断言 ``app.toml`` 落盘内容与列表里的实时摘要。

需要 Tk 的用例在无显示环境自动跳过（见 :class:`tests.test_gui_tabs.TkCase`）。
"""

from __future__ import annotations

import datetime
import tkinter as tk
import unittest

from atri_x.gui import configedit
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.cronschedule import (
    KIND_CUSTOM,
    KIND_DAILY,
    KIND_MONTHLY,
    KIND_WEEKLY,
    build_cron,
    describe_cron,
    kind_of_label,
    label_of_kind,
    next_run_text,
    parse_cron,
    parse_once_at,
    split_run_at,
    weekday_label,
    weekday_of_label,
)
from atri_x.gui.dialogs import MaintenanceDialog
from atri_x.gui.dialogs.tasks import (
    MAINTENANCE_TASKS,
    TASK_BY_KEY,
    TaskForm,
    clock_text,
    form_cron,
    load_schedule,
    schedule_of,
    schedule_text,
    validate_schedule,
)
from tests.test_gui_tabs import TkCase


class ScheduleCronCase(unittest.TestCase):
    """结构化调度 ↔ cron 的纯逻辑。"""

    def test_build_cron_daily_weekly_monthly(self) -> None:
        self.assertEqual(build_cron(KIND_DAILY, hour=3, minute=0), "0 3 * * *")
        self.assertEqual(build_cron(KIND_WEEKLY, hour=4, minute=30, weekday=1), "30 4 * * 1")
        self.assertEqual(build_cron(KIND_MONTHLY, hour=2, minute=5, day=15), "5 2 15 * *")

    def test_build_cron_normalizes_sunday_7_to_0(self) -> None:
        self.assertEqual(build_cron(KIND_WEEKLY, hour=0, minute=0, weekday=7), "0 0 * * 0")

    def test_build_cron_rejects_out_of_range(self) -> None:
        for kwargs in (
            {"hour": 24, "minute": 0},
            {"hour": 0, "minute": 60},
        ):
            with self.assertRaises(ConfigEditError):
                build_cron(KIND_DAILY, **kwargs)
        with self.assertRaises(ConfigEditError):
            build_cron(KIND_WEEKLY, hour=0, minute=0, weekday=8)
        with self.assertRaises(ConfigEditError):
            build_cron(KIND_MONTHLY, hour=0, minute=0, day=0)
        with self.assertRaises(ConfigEditError):
            build_cron("hourly", hour=0, minute=0)

    def test_parse_cron_roundtrip(self) -> None:
        for cron in ("0 3 * * *", "30 4 * * 3", "5 2 15 * *", "0 0 * * 0"):
            parsed = parse_cron(cron)
            self.assertIsNotNone(parsed, cron)
            kind = parsed["kind"]
            weekdays = {"kind": KIND_WEEKLY, "hour": parsed["hour"], "minute": parsed["minute"],
                        "weekday": parsed.get("weekday", 0)}
            kwargs = dict(hour=parsed["hour"], minute=parsed["minute"])
            if kind == KIND_WEEKLY:
                kwargs["weekday"] = weekdays["weekday"]
            if kind == KIND_MONTHLY:
                kwargs["day"] = parsed["day"]
            self.assertEqual(build_cron(kind, **kwargs), cron)

    def test_parse_cron_returns_none_for_unstructured(self) -> None:
        for cron in (
            "0 9,21 * * *",  # 一天两个时间点
            "0 */6 * * *",  # 步长
            "0 3 1 6 *",  # 限定了月份
            "0 3 1 * 1",  # 日与周同时受限
            "@daily",
            "not-a-cron",
            "",
        ):
            self.assertIsNone(parse_cron(cron), cron)

    def test_describe_cron_is_human_readable(self) -> None:
        self.assertEqual(describe_cron("0 3 * * *"), "每天 03:00")
        self.assertEqual(describe_cron("30 4 * * 3"), "每周三 04:30")
        self.assertEqual(describe_cron("5 2 15 * *"), "每月 15 日 02:05")
        self.assertEqual(describe_cron("0 9,21 * * *"), "自定义：0 9,21 * * *")
        self.assertEqual(describe_cron(""), "未设置")

    def test_next_run_text_uses_given_now(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, 0)
        self.assertEqual(next_run_text("0 3 * * *", now), "2026-10-19 03:00:00")
        self.assertEqual(next_run_text("30 4 * * 1", now), "2026-10-19 04:30:00")

    def test_labels_roundtrip(self) -> None:
        self.assertEqual(kind_of_label("每周"), KIND_WEEKLY)
        self.assertEqual(label_of_kind(KIND_MONTHLY), "每月")
        self.assertEqual(kind_of_label("叫什么都不会出现"), KIND_CUSTOM)
        self.assertEqual(weekday_of_label("周日"), 0)
        self.assertEqual(weekday_of_label("周三"), 3)
        self.assertEqual(weekday_label(0), "周日")
        self.assertEqual(weekday_label("6"), "周六")

    def test_parse_once_at(self) -> None:
        self.assertEqual(parse_once_at("2026-03-01", "03:30"), "2026-03-01 03:30:00")
        self.assertEqual(parse_once_at(" 2026-03-01 ", " 03:30:45 "), "2026-03-01 03:30:45")
        with self.assertRaises(ConfigEditError):
            parse_once_at("", "03:30")
        with self.assertRaises(ConfigEditError):
            parse_once_at("2026-13-01", "03:30")
        with self.assertRaises(ConfigEditError):
            parse_once_at("2026-03-01", "25:00")

    def test_split_run_at(self) -> None:
        self.assertEqual(split_run_at("2026-03-01 03:30:00"), ("2026-03-01", "03:30"))
        self.assertEqual(split_run_at("2026-03-01 03:30"), ("2026-03-01", "03:30"))


class TaskFormCase(unittest.TestCase):
    """表单状态 ↔ cron：还原、校验、摘要与一次性兜底。"""

    def test_every_task_default_schedule_is_valid(self) -> None:
        for task in MAINTENANCE_TASKS:
            form = TaskForm()
            load_schedule(form, task.default_schedule)
            self.assertEqual(validate_schedule(form, task), task.default_schedule)

    def test_load_schedule_splits_kind(self) -> None:
        form = TaskForm()
        load_schedule(form, "30 4 * * 2")
        self.assertEqual(form.kind, KIND_WEEKLY)
        self.assertEqual(form.weekday, "2")
        self.assertEqual(form.hour, "04")
        self.assertEqual(form.minute, "30")

        custom = TaskForm()
        load_schedule(custom, "0 9,21 * * *")
        self.assertEqual(custom.kind, KIND_CUSTOM)
        self.assertEqual(custom.custom, "0 9,21 * * *")

    def test_form_cron_and_invalid_custom(self) -> None:
        form = TaskForm(kind=KIND_MONTHLY, hour="2", minute="5", day="15")
        self.assertEqual(form_cron(form), "5 2 15 * *")
        bad = TaskForm(kind=KIND_CUSTOM, custom="nope")
        task = TASK_BY_KEY["log_cleanup"]
        with self.assertRaises(ConfigEditError):
            validate_schedule(bad, task)

    def test_one_off_keeps_default_cron_as_fallback(self) -> None:
        task = TASK_BY_KEY["log_cleanup"]
        form = TaskForm(once=True, kind=KIND_CUSTOM, custom="nope")
        self.assertEqual(schedule_of(form, task), task.default_schedule)
        self.assertEqual(validate_schedule(form, task), task.default_schedule)

    def test_schedule_text(self) -> None:
        task = TASK_BY_KEY["log_cleanup"]
        self.assertEqual(schedule_text(TaskForm(kind=KIND_DAILY, hour="3", minute="0"), task), "每天 03:00")
        self.assertEqual(
            schedule_text(TaskForm(once=True, date="2026-03-01", time="03:30"), task),
            "一次性：2026-03-01 03:30",
        )
        self.assertEqual(schedule_text(TaskForm(kind=KIND_CUSTOM, custom="nope"), task), "自定义：nope")

    def test_clock_text(self) -> None:
        self.assertEqual(clock_text("3", "03"), "03")
        self.assertEqual(clock_text("12", "00"), "12")
        self.assertEqual(clock_text("abc", "00"), "00")


class MaintenanceDialogCase(TkCase):
    """定时维护任务窗口：每天/每周/每月/自定义 + 一次性，都写进 app.toml。"""

    def setUp(self) -> None:
        super().setUp()
        self.dialog = MaintenanceDialog(self.root, self.paths)

    def tearDown(self) -> None:
        try:
            self.dialog.close()
        except tk.TclError:  # pragma: no cover - 已销毁
            pass
        super().tearDown()

    def select(self, key: str) -> None:
        """切到某个任务（等价于点列表里那一行）。"""
        self.dialog._commit_form()
        self.dialog.tree.selection_set(key)
        self.dialog._load_form(key)

    def maintenance(self) -> dict:
        return self.read_toml("app.toml")["maintenance"]

    def test_daily_schedule_written(self) -> None:
        self.select("log_cleanup")
        self.dialog.enabled_var.set(True)
        self.dialog.kind_var.set("每天")
        self.dialog._refresh_fields()
        self.dialog.hour_var.set("05")
        self.dialog.minute_var.set("30")
        self.dialog._save()
        log_cleanup = self.maintenance()["log_cleanup"]
        self.assertTrue(log_cleanup["enabled"])
        self.assertEqual(log_cleanup["schedule"], "30 5 * * *")
        self.assertNotIn("run_at", log_cleanup)

    def test_weekly_schedule_written(self) -> None:
        self.select("session_cleanup")
        self.dialog.enabled_var.set(True)
        self.dialog.kind_var.set("每周")
        self.dialog._refresh_fields()
        self.dialog.weekday_var.set("周三")
        self.dialog.hour_var.set("04")
        self.dialog.minute_var.set("15")
        self.dialog._save()
        self.assertEqual(self.maintenance()["session_cleanup"]["schedule"], "15 4 * * 3")

    def test_monthly_schedule_written(self) -> None:
        self.select("sandbox_cleanup")
        self.dialog.kind_var.set("每月")
        self.dialog._refresh_fields()
        self.dialog.day_var.set("15")
        self.dialog.hour_var.set("02")
        self.dialog.minute_var.set("05")
        self.dialog._save()
        self.assertEqual(self.maintenance()["sandbox_cleanup"]["schedule"], "5 2 15 * *")

    def test_custom_cron_written_from_preset(self) -> None:
        self.select("daily_report")
        self.dialog.kind_var.set("自定义 cron")
        self.dialog._refresh_fields()
        self.dialog.preset_box.current(3)
        self.dialog._apply_preset()
        self.assertEqual(self.dialog.custom_var.get(), "0 3 * * 1")
        self.dialog._save()
        self.assertEqual(self.maintenance()["daily_report"]["schedule"], "0 3 * * 1")

    def test_one_off_writes_run_at(self) -> None:
        self.select("session_cleanup")
        self.dialog.enabled_var.set(True)
        self.dialog.mode_var.set("once")
        self.dialog._refresh_fields()
        self.dialog.date_var.set("2026-03-01")
        self.dialog.time_var.set("03:30")
        self.dialog._save()
        session = self.maintenance()["session_cleanup"]
        self.assertEqual(session["run_at"], "2026-03-01 03:30:00")
        self.assertTrue(session["enabled"])
        # 一次性任务仍留下合法的周期表达式做兜底。
        self.assertEqual(session["schedule"], "15 3 * * *")

    def test_invalid_custom_cron_rejected(self) -> None:
        self.select("log_cleanup")
        self.dialog.kind_var.set("自定义 cron")
        self.dialog._refresh_fields()
        self.dialog.custom_var.set("not-a-cron")
        self.dialog._save()
        self.assertIn("失败", self.dialog._status.get())
        self.assertEqual(self.maintenance()["log_cleanup"]["schedule"], "0 3 * * *")

    def test_preview_shows_generated_cron_and_next_run(self) -> None:
        self.select("log_cleanup")
        self.dialog.kind_var.set("每周")
        self.dialog._refresh_fields()
        self.dialog.weekday_var.set("周五")
        self.dialog.hour_var.set("06")
        self.dialog.minute_var.set("07")
        preview = self.dialog.preview_label.cget("text")
        self.assertIn("每周五 06:07", preview)
        self.assertIn("cron：7 6 * * 5", preview)
        self.assertIn("下次触发", preview)

    def test_row_updates_before_save(self) -> None:
        self.select("log_cleanup")
        self.dialog.enabled_var.set(True)
        self.dialog.kind_var.set("每月")
        self.dialog._refresh_fields()
        self.dialog.day_var.set("9")
        values = self.dialog.tree.item("log_cleanup", "values")
        self.assertEqual(values[1], "已启用")
        self.assertEqual(values[2], "每月 9 日 03:00")

    def test_existing_cron_loads_into_structured_form(self) -> None:
        configedit.patch_section(
            self.paths.config_path, "maintenance.log_cleanup", {"schedule": "0 6 * * 2"}
        )
        self.dialog.reload()
        self.select("log_cleanup")
        self.assertEqual(self.dialog.kind_var.get(), "每周")
        self.assertEqual(self.dialog.weekday_var.get(), "周二")
        self.assertEqual(self.dialog.hour_var.get(), "06")
        self.assertEqual(self.dialog.minute_var.get(), "00")

    def test_unstructured_cron_falls_back_to_custom(self) -> None:
        # 默认配置里「资源/用量报告」是 0 9,21 * * *，结构化选项表达不了。
        self.select("daily_report")
        self.assertEqual(self.dialog.kind_var.get(), "自定义 cron")
        self.assertEqual(self.dialog.custom_var.get(), "0 9,21 * * *")


if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    unittest.main()
