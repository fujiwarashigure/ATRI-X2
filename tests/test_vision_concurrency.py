"""视觉并发旋钮接线：``[vision]`` 的 max_concurrent_jobs / max_job_queue / max_job_waiters。

回归背景（1.1.8）：``atri_x.vision.Options`` 早就有这三个字段，但 ``[vision]``
配置类里没有对应键、``app/vision.py`` 也从不传，服务永远用内置默认 10——用户
改配置完全无效。这里钉住"配置能解码 + 真的传进服务构造"。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from atri_x.app.vision import build_vision_describer
from atri_x.config.services import VisionConfig


def _cfg(section: VisionConfig) -> SimpleNamespace:
    provider = SimpleNamespace(base_url="https://example.invalid/v1")
    return SimpleNamespace(
        vision=section,
        providers={"demo": provider},
    )


class VisionConcurrencyConfigTest(unittest.TestCase):
    def test_vision_config_decodes_concurrency_fields(self) -> None:
        section = VisionConfig.from_dict(
            {
                "enabled": True,
                "provider": "demo",
                "model": "vision-1",
                "max_concurrent_jobs": 3,
                "max_job_queue": 5,
                "max_job_waiters": 7,
            }
        )
        self.assertEqual(section.max_concurrent_jobs, 3)
        self.assertEqual(section.max_job_queue, 5)
        self.assertEqual(section.max_job_waiters, 7)

    def test_defaults_are_zero_meaning_builtin_default(self) -> None:
        section = VisionConfig.from_dict({"enabled": True, "provider": "demo", "model": "v"})
        self.assertEqual(section.max_concurrent_jobs, 0)
        self.assertEqual(section.max_job_queue, 0)
        self.assertEqual(section.max_job_waiters, 0)

    def test_build_passes_concurrency_into_vision_options(self) -> None:
        section = VisionConfig.from_dict(
            {
                "enabled": True,
                "provider": "demo",
                "model": "vision-1",
                "max_concurrent_jobs": 4,
                "max_job_queue": 6,
                "max_job_waiters": 8,
            }
        )
        models = SimpleNamespace(by_provider={"demo": object()})
        captured: dict[str, object] = {}

        def _fake_service(options: object) -> object:
            captured["options"] = options
            return object()

        with mock.patch("atri_x.app.vision.new_vision_service", _fake_service):
            describer = build_vision_describer(None, _cfg(section), models)
        self.assertIsNotNone(describer)
        options = captured["options"]
        self.assertEqual(options.max_concurrent_jobs, 4)
        self.assertEqual(options.max_job_queue, 6)
        self.assertEqual(options.max_job_waiters, 8)


if __name__ == "__main__":  # pragma: no cover - 手动运行
    unittest.main()
