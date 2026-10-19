"""图像后端诊断载荷：``/diagnostics`` 的 ``image_backend`` 段 + 探测函数本身。

审计背景：“装没装 Pillow”过去只能从 ``atri_x.media.imagecodec.backend()`` 猜，
启动诊断（``doctor`` 与 ``/diagnostics``）不显示实际生效的后端，产物核验也拿不到
机器可读的结论。这里钉住三件事：

1. ``collect_ops_diagnostics`` 一定带上 ``image_backend``（``pillow`` / ``stdlib``
   + 版本号），且与 :mod:`atri_x.media.imagecodec.backend_probe` 的结论一致；
2. ``marshal`` 走 Go ``map`` 语义：字典键不被 ``omitempty`` 吞掉；
3. 探测的唯一出处就是 ``backend_probe``：Pillow 装不上时降级成 ``stdlib``
   并把原因记在 ``reason``，不让第二处 ``try/except ImportError`` 给出不同答案。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from atri_x.app.ops_health import OpsDiagnostics, collect_ops_diagnostics, marshal
from atri_x.media.imagecodec import backend_probe
from atri_x.media.imagecodec import image as image_module


class DiagnosticsImageBackendTest(unittest.TestCase):
    """``/diagnostics`` 载荷里的后端段。"""

    def tearDown(self) -> None:
        # 探测缓存是进程级全局；被本用例动过就复位，避免污染其它用例。
        backend_probe.reset_probe()
        image_module.set_backend("auto")

    def test_diagnostics_expose_the_live_backend(self) -> None:
        report = collect_ops_diagnostics(None, None)
        payload = report.image_backend
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload, backend_probe.image_backend_info())
        self.assertIn(payload["backend"], ("pillow", "stdlib"))
        if payload["backend"] == "pillow":
            self.assertTrue(payload["pillow_installed"])
            self.assertTrue(payload["version"], "生效 pillow 时必须带版本号")
        else:
            # stdlib 也能带版本号（装了 Pillow 但被强制关掉），此时必须能看出"装了"。
            self.assertEqual(bool(payload["version"]), payload["pillow_installed"])

    def test_rendered_json_keeps_every_key(self) -> None:
        """Go ``map`` 语义：字典键不做 omitempty，空字符串/False 照常输出。"""
        rendered = marshal(collect_ops_diagnostics(None, None))["image_backend"]
        self.assertEqual(
            set(rendered),
            {"backend", "version", "pillow_installed", "reason"},
        )
        self.assertIn("reason", rendered)

    def test_default_field_is_absent_until_collected(self) -> None:
        self.assertIsNone(OpsDiagnostics().image_backend)
        self.assertNotIn("image_backend", marshal(OpsDiagnostics()))

    def test_forced_pure_backend_is_distinguishable(self) -> None:
        image_module.set_backend("pure")
        payload = backend_probe.image_backend_info()
        self.assertEqual(payload["backend"], "stdlib")
        self.assertTrue(payload["pillow_installed"])
        self.assertTrue(payload["version"])

    def test_probe_failure_degrades_to_stdlib_with_reason(self) -> None:
        """模拟 Pillow 缺失（``sys.modules`` 里置 None 会让 import 直接失败）。"""
        backend_probe.reset_probe()
        with mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None}):
            payload = backend_probe.image_backend_info()
        self.assertEqual(payload["backend"], "stdlib")
        self.assertFalse(payload["pillow_installed"])
        self.assertEqual(payload["version"], "")
        # ModuleNotFoundError 是 ImportError 的子类，两种都算"import 失败"。
        self.assertRegex(payload["reason"], r"ImportError|ModuleNotFoundError")

    def test_forced_pure_reports_stdlib(self) -> None:
        image_module.set_backend("pure")
        self.assertEqual(backend_probe.image_backend(), "stdlib")
        self.assertEqual(image_module.backend(), "pure")


if __name__ == "__main__":
    unittest.main()
