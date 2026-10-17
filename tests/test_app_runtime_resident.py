"""``[resident_memory]`` 写入策略的回归：秒 → ``timedelta`` 必须真的换算。

真实事故（第三轮对抗性验证在**真实启动日志**里抓到，单测全绿也照样存在）：

- `atri_x/app/runtime.py` 以前用 ``float(...)`` 把配置里的"秒"直接填进
  :class:`atri_x.memory.resident.NormalWritePolicy`，而该 dataclass 的
  ``min_interval`` / ``window`` 是 :class:`datetime.timedelta`；
- 于是 ``NormalWritePolicy.normalized()`` 抛
  ``TypeError: '>' not supported between instances of 'float' and 'datetime.timedelta'``；
- :func:`atri_x.app.tool_runtime._build_resident_store` 把异常吞成一条 WARN 并返回 ``None``，
  启动日志只留一行 ``WARN builtin runtime degraded: open resident memory store: ...``；
- 后果是 **4 个 ``resident_memory*`` 工具在生产路径永远注册不上**（真实冒烟配置只注册 38 个）。

这里不搭假宿主：策略用生产构造函数 ``runtime.resident_normal_write_policy``，
存储用生产入口 ``tool_runtime._build_resident_store``。
"""

from __future__ import annotations

import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from atri_x.app.runtime import resident_normal_write_policy
from atri_x.app.tool_runtime import ToolRuntimeOptions, _build_resident_store
from atri_x.memory.resident import Limits as ResidentLimits
from atri_x.memory.resident.store import NormalWritePolicy

TMP_DIR = Path(__file__).resolve().parent.parent / "tmp_app_runtime_resident_test"


def _resident_section(**overrides: object) -> SimpleNamespace:
    """生产配置节的最小等价物：暴露 ``*_seconds_value`` 访问器（**方法**，不是字段）。"""

    values: dict[str, object] = {
        "normal_write_min_interval_seconds_value": 5,
        "normal_write_window_seconds_value": 60,
        "normal_write_max_per_window_value": 3,
        "normal_max_lines_value": 200,
        "normal_max_units_per_entry_value": 40,
        "is_normal_block_instruction_patterns": True,
    }
    values.update(overrides)
    return SimpleNamespace(**{name: (lambda value=value: value) for name, value in values.items()})


class ResidentWritePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_seconds_are_converted_to_timedelta(self) -> None:
        policy = resident_normal_write_policy(_resident_section())

        self.assertIsInstance(policy.min_interval, timedelta)
        self.assertIsInstance(policy.window, timedelta)
        self.assertEqual(policy.min_interval, timedelta(seconds=5))
        self.assertEqual(policy.window, timedelta(seconds=60))
        self.assertEqual(policy.max_writes, 3)
        self.assertEqual(policy.max_lines, 200)
        self.assertEqual(policy.max_units_per_entry, 40)
        self.assertTrue(policy.block_instruction_patterns)

    def test_normalized_does_not_raise(self) -> None:
        """修复前：``float > timedelta`` → TypeError（这里就是最小复现）。"""
        policy = resident_normal_write_policy(_resident_section())
        normalized = policy.normalized()
        self.assertEqual(normalized.min_interval, timedelta(seconds=5))
        self.assertEqual(normalized.window, timedelta(seconds=60))

    def test_float_shaped_policy_still_raises(self) -> None:
        """反证：把旧形状（float 秒）喂回去，必须复现同一处 TypeError。"""
        legacy = NormalWritePolicy(min_interval=5.0, window=60.0, max_writes=3)
        with self.assertRaises(TypeError):
            legacy.normalized()

    def test_zero_seconds_stay_disabled(self) -> None:
        policy = resident_normal_write_policy(
            _resident_section(
                normal_write_min_interval_seconds_value=0,
                normal_write_window_seconds_value=0,
            )
        )
        normalized = policy.normalized()
        self.assertEqual(normalized.min_interval, timedelta(0))
        self.assertEqual(normalized.window, timedelta(0))
        self.assertEqual(normalized.max_writes, 0)

    def test_resident_store_opens_without_warning(self) -> None:
        """生产入口：策略形状对了，存储就建得起来、且不留 warning。"""
        warnings: list[str] = []
        options = ToolRuntimeOptions(
            config_dir=str(TMP_DIR),
            resident_memory_max_units=ResidentLimits(core=200, normal=300),
            resident_memory_policy=resident_normal_write_policy(_resident_section()),
        )

        store = _build_resident_store(options, warnings)

        self.assertIsNotNone(store, warnings)
        self.assertEqual(warnings, [])
        # 存储拿到的是归一化后的策略（timedelta 语义生效，而不是"没人用过"）
        self.assertEqual(store.normal_policy.min_interval, timedelta(seconds=5))
        self.assertEqual(store.normal_policy.window, timedelta(seconds=60))

    def test_legacy_float_policy_is_swallowed_into_warning(self) -> None:
        """反证：旧形状走到生产入口时，异常被吞成 warning、存储为 None。

        这条钉住的是"为什么当时没人发现"：异常不抛，只留一行 WARN。
        """
        warnings: list[str] = []
        options = ToolRuntimeOptions(
            config_dir=str(TMP_DIR),
            resident_memory_max_units=ResidentLimits(core=200, normal=300),
            resident_memory_policy=NormalWritePolicy(
                min_interval=5.0, window=60.0, max_writes=3  # type: ignore[arg-type]
            ),
        )

        store = _build_resident_store(options, warnings)

        self.assertIsNone(store)
        self.assertEqual(len(warnings), 1)
        self.assertIn("open resident memory store", warnings[0])
        self.assertIn("timedelta", warnings[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
