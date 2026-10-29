"""``runtime_resources`` 工具与 ``/resources`` 命令的回归测试。

覆盖三层：

1. **声明**：``superadmin_only`` / ``risk == safe`` / schema 参数与默认值；
2. **采集**：:func:`atri_x.sysinfo.collect_resources` 的换算逻辑（用可注入的假采集器，
   不依赖真实 Windows API 与真实计时）、失败降级、以及旧接口 ``process_rss`` 在
   Windows 上不再恒为 0；
3. **入口**：工具的 ``call``（含未知参数拒绝、采集失败回文本）与 ``/resources``
   命令（``Deps.resources`` 可注入、失败不炸流水线、``MinRole`` 留空 = 仅超管）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest import mock

from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.resources import (
    default_resource_paths,
    new_resources,
    render_resources,
)
from atri_x.command import Request
from atri_x.errors import InvalidError
from atri_x.sysinfo import (
    ProcessSample,
    SystemSample,
    collect_resources,
    format_bytes,
    format_resources,
    format_uptime,
    process_rss,
    resources_to_dict,
)
from atri_x.tool.builtin.runtime_resources import (
    DEFAULT_SAMPLE_MS,
    MAX_SAMPLE_MS,
    MIN_SAMPLE_MS,
    RUNTIME_RESOURCES_NAME,
    new_runtime_resources_tool,
    parse_runtime_resources_args,
    runtime_resources_builder,
)
from atri_x.tool.tool import CallRequest, RiskSafe


class FakeCollector:
    """可编程的假平台采集器：只负责"读两次计数器"。"""

    def __init__(
        self,
        process_samples: list[ProcessSample] | None = None,
        system_samples: list[SystemSample] | None = None,
        process_error: Exception | None = None,
    ) -> None:
        self.process_samples = list(process_samples or [])
        self.system_samples = list(system_samples or [])
        self.process_error = process_error
        self.process_calls = 0

    def sample_process(self) -> ProcessSample:
        self.process_calls += 1
        if self.process_error is not None:
            raise self.process_error
        if self.process_samples:
            return self.process_samples.pop(0)
        return ProcessSample()

    def sample_system(self) -> SystemSample:
        if self.system_samples:
            return self.system_samples.pop(0)
        return SystemSample()


def _fixed_snapshot() -> object:
    from atri_x.sysinfo import ProcessResources, ResourceSnapshot, SystemResources

    return ResourceSnapshot(
        process=ProcessResources(
            rss_bytes=191 * 1024 * 1024,
            peak_rss_bytes=221 * 1024 * 1024,
            private_bytes=101 * 1024 * 1024,
            cpu_percent=3.25,
            threads=41,
            handles=812,
        ),
        system=SystemResources(
            cpu_percent=12.5,
            cpu_count=8,
            mem_total_bytes=32 * 1024**3,
            mem_avail_bytes=10 * 1024**3,
            mem_percent=68.75,
            uptime_seconds=2 * 86400 + 3 * 3600,
        ),
        disks=[(os.getcwd(), 500 * 1024**3, 250 * 1024**3)],
        sample_interval=0.5,
    )


class BuilderTests(unittest.TestCase):
    def test_info_is_superadmin_only_and_safe(self) -> None:
        info = runtime_resources_builder().build_info()
        self.assertEqual(info.name, RUNTIME_RESOURCES_NAME)
        self.assertTrue(info.superadmin_only, "非超管连 schema 都不该拿到")
        self.assertEqual(info.risk, RiskSafe)
        # 不能是 owner_scoped：那会让普通用户也放行（Policy 对 owner_scoped 一律放行）。
        self.assertFalse(info.owner_scoped)
        self.assertIn("agent", info.tags)

    def test_schema_declares_three_parameters_without_required(self) -> None:
        schema = runtime_resources_builder().build_schema()
        parameters = schema.function.parameters
        self.assertEqual(
            set(parameters["properties"]),
            {"sample_ms", "paths", "include_dir_size"},
        )
        self.assertNotIn("required", parameters)
        self.assertEqual(
            parameters["properties"]["paths"]["items"]["type"], "string"
        )
        self.assertEqual(parameters["properties"]["sample_ms"]["type"], "integer")

    def test_tool_exposes_name_info_and_schema(self) -> None:
        tool = new_runtime_resources_tool()
        self.assertEqual(tool.name(), RUNTIME_RESOURCES_NAME)
        self.assertTrue(tool.info().superadmin_only)
        self.assertEqual(tool.schema().function.name, RUNTIME_RESOURCES_NAME)


class ArgumentParsingTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = parse_runtime_resources_args("")
        self.assertEqual(args.sample_ms, DEFAULT_SAMPLE_MS)
        self.assertIsNone(args.paths)
        self.assertFalse(args.include_dir_size)

    def test_parses_all_fields(self) -> None:
        args = parse_runtime_resources_args(
            json.dumps({"sample_ms": 800, "paths": ["C:/x", " C:/x ", ""],
                        "include_dir_size": True})
        )
        self.assertEqual(args.sample_ms, 800)
        self.assertEqual(args.paths, ["C:/x"])
        self.assertTrue(args.include_dir_size)

    def test_sample_ms_is_clamped(self) -> None:
        self.assertEqual(
            parse_runtime_resources_args('{"sample_ms": 1}').sample_ms, MIN_SAMPLE_MS
        )
        self.assertEqual(
            parse_runtime_resources_args('{"sample_ms": 99999}').sample_ms, MAX_SAMPLE_MS
        )

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(InvalidError):
            parse_runtime_resources_args('{"nope": 1}')

    def test_bad_payloads_are_rejected(self) -> None:
        for raw in ("[]", '{"paths": 1}', '{"paths": [1]}', '{"include_dir_size": 1}',
                    '{"sample_ms": "x"}', "not json"):
            with self.assertRaises(InvalidError, msg=raw):
                parse_runtime_resources_args(raw)


class CollectResourcesTests(unittest.TestCase):
    def test_cpu_and_memory_are_derived_from_counter_deltas(self) -> None:
        collector = FakeCollector(
            process_samples=[
                ProcessSample(cpu_user_seconds=1.0, cpu_sys_seconds=0.5),
                ProcessSample(
                    cpu_user_seconds=1.025,
                    cpu_sys_seconds=0.525,
                    rss_bytes=100,
                    peak_rss_bytes=120,
                    private_bytes=80,
                    handles=7,
                ),
            ],
            system_samples=[
                SystemSample(cpu_total_seconds=100.0, cpu_idle_seconds=40.0),
                SystemSample(
                    cpu_total_seconds=110.0,
                    cpu_idle_seconds=45.0,
                    mem_total_bytes=1000,
                    mem_avail_bytes=250,
                    uptime_seconds=90,
                ),
            ],
        )
        # 采样窗口改成最小值，避免测试真的等 500ms。
        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=collector
        )
        self.assertEqual(snapshot.errors, [])
        self.assertEqual(snapshot.sample_interval, 0.05)
        self.assertEqual(snapshot.process.rss_bytes, 100)
        self.assertEqual(snapshot.process.peak_rss_bytes, 120)
        self.assertEqual(snapshot.process.private_bytes, 80)
        self.assertEqual(snapshot.process.handles, 7)
        # 0.1s CPU / 0.1s 墙钟 → 100%（单核基准）。
        self.assertAlmostEqual(snapshot.process.cpu_percent, 100.0, delta=15.0)
        # 系统：Δbusy=5s / Δtotal=10s → 50%。
        self.assertAlmostEqual(snapshot.system.cpu_percent, 50.0, delta=0.1)
        self.assertEqual(snapshot.system.mem_percent, 75.0)
        self.assertEqual(snapshot.system.uptime_seconds, 90)

    def test_disks_and_dir_size_are_collected_for_existing_paths(self) -> None:
        collector = FakeCollector()
        snapshot = collect_resources(
            [os.getcwd()], sample_interval=0.001, include_dir_size=True,
            platform_impl=collector,
        )
        self.assertEqual(len(snapshot.disks), 1)
        path, total, free = snapshot.disks[0]
        self.assertEqual(path, os.getcwd())
        self.assertGreater(total, 0)
        self.assertLessEqual(free, total)
        self.assertGreaterEqual(snapshot.dir_bytes, 0)

    def test_bad_disk_path_degrades_into_errors(self) -> None:
        collector = FakeCollector()
        bad = os.path.join(os.getcwd(), "no-such-dir-for-resources-test")
        snapshot = collect_resources([bad], sample_interval=0.001, platform_impl=collector)
        self.assertEqual(snapshot.disks, [])
        self.assertTrue(snapshot.errors, "采集失败必须落在 errors 里，不能抛")

    def test_process_sampling_failure_degrades(self) -> None:
        collector = FakeCollector(process_error=OSError("boom"))
        snapshot = collect_resources([], sample_interval=0.001, platform_impl=collector)
        self.assertTrue(any("进程" in item for item in snapshot.errors))
        self.assertEqual(snapshot.process.rss_bytes, 0)

    def test_one_sample_still_reports_memory(self) -> None:
        """第二次采样失败时，第一次拿到的内存值仍然要报出来（CPU% 留 0）。"""

        class OneShotCollector:
            def __init__(self) -> None:
                self.process_calls = 0
                self.system_calls = 0

            def sample_process(self) -> ProcessSample:
                self.process_calls += 1
                if self.process_calls > 1:
                    raise OSError("gone")
                return ProcessSample(rss_bytes=42)

            def sample_system(self) -> SystemSample:
                self.system_calls += 1
                if self.system_calls > 1:
                    raise OSError("gone")
                return SystemSample(mem_total_bytes=100, mem_avail_bytes=40)

        snapshot = collect_resources([], sample_interval=0.001,
                                     platform_impl=OneShotCollector())
        self.assertEqual(snapshot.process.rss_bytes, 42)
        self.assertEqual(snapshot.process.cpu_percent, 0.0)
        self.assertEqual(snapshot.system.mem_percent, 60.0)
        self.assertTrue(any("二次采样" in item for item in snapshot.errors))

    def test_real_platform_smoke(self) -> None:
        """不注入采集器：在 Windows 上必须拿到真实的非零 RSS 与系统内存。"""
        snapshot = collect_resources([os.getcwd()], sample_interval=0.001)
        self.assertGreaterEqual(snapshot.process.rss_bytes, 0)
        self.assertGreater(snapshot.system.mem_total_bytes, 0)
        self.assertGreater(snapshot.system.cpu_count, 0)
        if sys.platform == "win32":
            self.assertGreater(snapshot.process.rss_bytes, 0, "Windows RSS 不应为 0")
            self.assertGreater(snapshot.process.peak_rss_bytes, 0)
            # 旧接口（/metrics、日报、diskguard 用）也必须拿到真实值。
            self.assertGreater(process_rss(), 0, "process_rss 在 Windows 不应恒为 0")


class FormattingTests(unittest.TestCase):
    def test_format_uptime(self) -> None:
        self.assertEqual(format_uptime(0), "0秒")
        self.assertEqual(format_uptime(59), "59秒")
        self.assertEqual(format_uptime(60), "1分0秒")
        self.assertEqual(format_uptime(3661), "1小时1分")
        self.assertEqual(format_uptime(2 * 86400 + 3 * 3600 + 5), "2天3小时")

    def test_format_resources_has_all_sections(self) -> None:
        text = format_resources(_fixed_snapshot())
        self.assertIn("采样 500ms", text)
        self.assertIn("【进程】", text)
        self.assertIn("【系统】", text)
        self.assertIn("【磁盘】", text)
        self.assertIn("单核基准", text)
        self.assertIn(format_bytes(191 * 1024 * 1024), text)
        self.assertIn("3.2%", text)  # 进程 CPU 两位小数 → 一位展示

    def test_resources_to_dict_is_json_serializable(self) -> None:
        payload = resources_to_dict(_fixed_snapshot())
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("rss_bytes", encoded)
        self.assertEqual(payload["system"]["cpu_count"], 8)
        self.assertEqual(payload["disks"][0]["total_bytes"], 500 * 1024**3)


class ToolCallTests(unittest.TestCase):
    class _RuntimeInfo:
        config_dir = os.getcwd()

    def _call(self, arguments: str = "") -> object:
        tool = new_runtime_resources_tool(self._RuntimeInfo())
        return asyncio.run(tool.call(None, CallRequest(name=RUNTIME_RESOURCES_NAME,
                                                      arguments=arguments)))

    def test_call_renders_text_and_json(self) -> None:
        snapshot = _fixed_snapshot()
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            return_value=snapshot,
        ):
            result = self._call('{"sample_ms": 100}')
        self.assertIn("【进程】", result.content)
        self.assertIn("rss_bytes", result.data)

    def test_call_passes_args_through(self) -> None:
        snapshot = _fixed_snapshot()
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            return_value=snapshot,
        ) as patched:
            self._call('{"include_dir_size": true, "paths": ["C:/x"]}')
        _args, kwargs = patched.call_args
        self.assertEqual(kwargs["include_dir_size"], True)
        self.assertEqual(kwargs["sample_interval"], 0.5)
        self.assertEqual(_args[0], ["C:/x"])

    def test_call_defaults_to_config_dir(self) -> None:
        snapshot = _fixed_snapshot()
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            return_value=snapshot,
        ) as patched:
            self._call()
        self.assertEqual(patched.call_args[0][0], [os.getcwd()])

    def test_collector_crash_returns_text_not_exception(self) -> None:
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            side_effect=RuntimeError("boom"),
        ):
            result = self._call()
        self.assertIn("资源采集失败", result.content)


class ResourcesCommandTests(unittest.TestCase):
    def test_render_resources_uses_real_collector(self) -> None:
        text = render_resources([])
        self.assertIn("ATRI-X 资源占用", text)

    def test_default_paths_are_absolute_or_empty(self) -> None:
        paths = default_resource_paths()
        for path in paths:
            self.assertTrue(os.path.isabs(path), path)

    def test_handler_uses_injected_renderer(self) -> None:
        handler = new_resources(Deps(resources=lambda: "FAKE"))
        result = asyncio.run(handler.handle(None, Request(name="resources")))
        self.assertEqual(result.content, "FAKE")

    def test_handler_reports_failure_as_text(self) -> None:
        def broken() -> str:
            raise RuntimeError("nope")

        handler = new_resources(Deps(resources=broken))
        result = asyncio.run(handler.handle(None, Request(name="resources")))
        self.assertIn("资源采集失败", result.content)

    def test_command_is_superadmin_only(self) -> None:
        """``Info.min_role`` 留空 = 路由层按"仅超管"处理（与 /doctor / /rollback 一致）。"""
        info = new_resources(Deps()).info()
        self.assertEqual(info.name, "resources")
        self.assertEqual(info.min_role, "")


if __name__ == "__main__":
    unittest.main()
