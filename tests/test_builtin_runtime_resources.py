"""``runtime_resources`` 工具与 ``/resources`` 命令的回归测试。

覆盖四层：

1. **声明**：``superadmin_only`` / ``risk == safe`` / schema 参数与默认值；
2. **采集**：:func:`atri_x.sysinfo.collect_resources` 的换算逻辑（用可注入的假采集器，
   不依赖真实 Windows API 与真实计时）、失败降级、卷标签（**不带路径**）、以及旧接口
   ``process_rss`` 在 Windows 上不再恒为 0；
3. **硬件/显卡**：``nvidia-smi`` CSV 解析、PDH 计数器换算、型号分类与采集降级；
4. **入口**：工具的 ``call`` 与 ``/resources`` 命令（可注入、失败不炸流水线、仅超管），
   以及 1.1.4 修订的**隐私边界**：输出里不出现真实路径 / 用户名 / 主机名。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import sys
import time
import unittest
from unittest import mock

from atri_x.agent.commands.deps import Deps
from atri_x.agent.commands.resources import new_resources, render_resources
from atri_x.command import Request
from atri_x.errors import InvalidError
from atri_x.gpu_usage import (
    GpuInfo,
    generic_gpu_usage,
    parse_gpu_engine_usage,
    parse_gpu_memory_usage,
    parse_nvidia_smi_csv,
)
from atri_x.hardware import (
    HardwareInfo,
    arch_name,
    collect_hardware,
    gpu_vendor,
    is_integrated_gpu,
    is_virtual_gpu,
    nvidia_smi_path,
    os_description,
)
from atri_x.sysinfo import (
    DiskUsage,
    ProcessSample,
    SystemSample,
    collect_resources,
    format_bytes,
    format_resources,
    format_uptime,
    process_rss,
    resources_to_dict,
)
from atri_x.sysinfo_format import format_resources as format_resources_from_report
from atri_x.sysinfo_platform import default_collector
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

_PARENT = os.path.dirname(os.getcwd())


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


def _fixed_hardware() -> HardwareInfo:
    return HardwareInfo(
        cpu_model="Test CPU 9000",
        cpu_threads=16,
        os_name="TestOS（build 1）",
        arch="x64",
        gpus=[
            GpuInfo(
                name="NVIDIA GeForce RTX 4090",
                vendor="NVIDIA",
                memory_total_bytes=24 * 1024**3,
                memory_used_bytes=6 * 1024**3,
                utilization_percent=25.0,
                temperature_c=60.0,
                power_watts=200.0,
                power_limit_watts=450.0,
                source="nvidia-smi",
            )
        ],
        gpu_source="nvidia-smi",
    )


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
            uptime_seconds=3661,
        ),
        system=SystemResources(
            cpu_percent=12.5,
            cpu_count=8,
            mem_total_bytes=32 * 1024**3,
            mem_avail_bytes=10 * 1024**3,
            mem_percent=68.75,
            uptime_seconds=2 * 86400 + 3 * 3600,
        ),
        disks=[DiskUsage(label="系统盘", total_bytes=500 * 1024**3,
                         free_bytes=250 * 1024**3)],
        hardware=_fixed_hardware(),
        sample_interval=0.5,
    )


def _no_hardware() -> HardwareInfo:
    return HardwareInfo()


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

    def test_description_says_no_local_paths(self) -> None:
        text = runtime_resources_builder().build_schema().function.description
        self.assertIn("不返回本机文件路径", text)

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
            [], sample_interval=0.001, platform_impl=collector,
            hardware_provider=_no_hardware,
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

    def test_process_uptime_comes_from_start_timestamp(self) -> None:
        started = time.time() - 125
        collector = FakeCollector(
            process_samples=[
                ProcessSample(start_unix_seconds=started),
                ProcessSample(start_unix_seconds=started),
            ]
        )
        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=collector,
            hardware_provider=_no_hardware,
        )
        self.assertGreaterEqual(snapshot.process.uptime_seconds, 120)
        self.assertLess(snapshot.process.uptime_seconds, 3600)

    def test_disks_use_labels_never_paths(self) -> None:
        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=FakeCollector(),
            hardware_provider=_no_hardware,
        )
        # 自动枚举本机固定磁盘：受限环境下可能一个都拿不到。关键是不带路径。
        for disk in snapshot.disks:
            self.assertIsInstance(disk, DiskUsage)
            self.assertTrue(disk.label.startswith(("系统盘", "数据盘")))
            self.assertNotIn(os.getcwd(), disk.label)
            self.assertNotIn(os.sep, disk.label)

    def test_explicit_path_becomes_target_label(self) -> None:
        snapshot = collect_resources(
            [os.getcwd()], sample_interval=0.001, platform_impl=FakeCollector(),
            hardware_provider=_no_hardware,
        )
        self.assertEqual(len(snapshot.disks), 1)
        disk = snapshot.disks[0]
        self.assertIn(disk.label, {"系统盘", "指定卷 1"})
        self.assertGreater(disk.total_bytes, 0)
        self.assertLessEqual(disk.free_bytes, disk.total_bytes)
        self.assertNotIn(os.getcwd(), repr(snapshot))

    def test_dir_size_only_when_paths_are_explicit(self) -> None:
        snapshot = collect_resources(
            [os.getcwd()], sample_interval=0.001, include_dir_size=True,
            platform_impl=FakeCollector(), hardware_provider=_no_hardware,
        )
        self.assertGreaterEqual(snapshot.dir_bytes, 0)

    def test_bad_disk_path_degrades_without_leaking_path(self) -> None:
        bad = os.path.join(os.getcwd(), "no-such-dir-for-resources-test")
        snapshot = collect_resources(
            [bad], sample_interval=0.001, platform_impl=FakeCollector(),
            hardware_provider=_no_hardware,
        )
        self.assertEqual(snapshot.disks, [])
        self.assertTrue(snapshot.errors, "采集失败必须落在 errors 里，不能抛")
        for item in snapshot.errors:
            self.assertNotIn(bad, item)
            self.assertNotIn("no-such-dir", item)

    def test_platform_collector_selection(self) -> None:
        collector = default_collector()
        self.assertTrue(hasattr(collector, "sample_process"))
        self.assertTrue(hasattr(collector, "sample_system"))
        sample = collector.sample_process()
        self.assertEqual(sample.rss_bytes, sample.rss_bytes)  # 不抛即通过

    def test_process_sampling_failure_degrades(self) -> None:
        collector = FakeCollector(process_error=OSError("boom"))
        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=collector,
            hardware_provider=_no_hardware,
        )
        self.assertTrue(any("进程" in item for item in snapshot.errors))
        self.assertEqual(snapshot.process.rss_bytes, 0)

    def test_hardware_is_collected_and_merged(self) -> None:
        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=FakeCollector(),
            hardware_provider=_fixed_hardware,
        )
        self.assertEqual(snapshot.hardware.cpu_model, "Test CPU 9000")
        self.assertEqual(snapshot.hardware.gpus[0].name, "NVIDIA GeForce RTX 4090")
        self.assertEqual(snapshot.hardware.gpus[0].utilization_percent, 25.0)

    def test_hardware_failure_degrades(self) -> None:
        def broken() -> HardwareInfo:
            raise RuntimeError("/secret/path/should-not-leak")

        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=FakeCollector(),
            hardware_provider=broken,
        )
        self.assertTrue(any("硬件采集失败" in item for item in snapshot.errors))
        self.assertNotIn("/secret/path/should-not-leak", " ".join(snapshot.errors))
        self.assertEqual(snapshot.hardware.cpu_model, "")

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

        snapshot = collect_resources(
            [], sample_interval=0.001, platform_impl=OneShotCollector(),
            hardware_provider=_no_hardware,
        )
        self.assertEqual(snapshot.process.rss_bytes, 42)
        self.assertEqual(snapshot.process.cpu_percent, 0.0)
        self.assertEqual(snapshot.system.mem_percent, 60.0)
        self.assertTrue(any("二次采样" in item for item in snapshot.errors))

    def test_real_platform_smoke(self) -> None:
        """不注入采集器：在 Windows 上必须拿到真实的非零 RSS 与系统内存。"""
        snapshot = collect_resources([], sample_interval=0.001)
        self.assertGreaterEqual(snapshot.process.rss_bytes, 0)
        self.assertGreater(snapshot.system.mem_total_bytes, 0)
        self.assertGreater(snapshot.system.cpu_count, 0)
        self.assertTrue(snapshot.hardware.cpu_model, "CPU 型号不应为空")
        if sys.platform == "win32":
            self.assertGreater(snapshot.process.rss_bytes, 0, "Windows RSS 不应为 0")
            self.assertGreater(snapshot.process.peak_rss_bytes, 0)
            self.assertGreater(snapshot.process.uptime_seconds, 0, "进程运行时长应可测")
            # 旧接口（/metrics、日报、diskguard 用）也必须拿到真实值。
            self.assertGreater(process_rss(), 0, "process_rss 在 Windows 不应恒为 0")


class HardwareParsingTests(unittest.TestCase):
    def test_parse_nvidia_smi_csv(self) -> None:
        text = (
            "NVIDIA GeForce RTX 4070 Ti, 37, 12282, 3245, 61, 145.32, 285.00\n"
            "NVIDIA GeForce RTX 3060, [N/A], 12288, 100, 45, [N/A], 170\n"
        )
        gpus = parse_nvidia_smi_csv(text)
        self.assertEqual(len(gpus), 2)
        first = gpus[0]
        self.assertEqual(first.name, "NVIDIA GeForce RTX 4070 Ti")
        self.assertAlmostEqual(first.utilization_percent, 37.0)
        self.assertEqual(first.memory_total_bytes, 12282 * 1024 * 1024)
        self.assertEqual(first.memory_used_bytes, 3245 * 1024 * 1024)
        self.assertAlmostEqual(first.temperature_c, 61.0)
        self.assertAlmostEqual(first.power_watts, 145.32)
        self.assertAlmostEqual(first.power_limit_watts, 285.0)
        self.assertEqual(first.source, "nvidia-smi")
        second = gpus[1]
        self.assertIsNone(second.utilization_percent)
        self.assertIsNone(second.power_watts)

    def test_parse_empty_nvidia_smi_output(self) -> None:
        self.assertEqual(parse_nvidia_smi_csv(""), [])
        self.assertEqual(parse_nvidia_smi_csv("\n\n"), [])

    def test_parse_gpu_engine_usage_sums_3d_only(self) -> None:
        items = [
            ("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 12.5),
            ("pid_2_luid_0x0_0x1_phys_0_eng_1_engtype_3D", 7.5),
            ("pid_1_luid_0x0_0x1_phys_0_eng_2_engtype_VideoDecode", 90.0),
            ("garbage", 50.0),
        ]
        self.assertAlmostEqual(parse_gpu_engine_usage(items), 20.0)

    def test_parse_gpu_engine_usage_caps_at_100(self) -> None:
        items = [("p_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 80.0),
                 ("q_luid_0x0_0x1_phys_0_eng_1_engtype_3D", 80.0)]
        self.assertEqual(parse_gpu_engine_usage(items), 100.0)

    def test_parse_gpu_engine_usage_none_when_no_3d(self) -> None:
        self.assertIsNone(parse_gpu_engine_usage([("x_engtype_Copy", 10.0)]))
        self.assertIsNone(parse_gpu_engine_usage([]))

    def test_parse_gpu_memory_usage_sums_bytes(self) -> None:
        items = [("luid_0x0_0x1_phys_0", 1024.0), ("luid_0x0_0x2_phys_0", 512.0)]
        self.assertEqual(parse_gpu_memory_usage(items), 1536)

    def test_gpu_classification_helpers(self) -> None:
        self.assertEqual(gpu_vendor("NVIDIA GeForce RTX 4090"), "NVIDIA")
        self.assertEqual(gpu_vendor("AMD Radeon RX 640"), "AMD")
        self.assertEqual(gpu_vendor("Intel(R) UHD Graphics 770"), "Intel")
        self.assertEqual(gpu_vendor("OrayIddDriver Device"), "其它")
        self.assertTrue(is_integrated_gpu("Intel(R) UHD Graphics 770"))
        self.assertFalse(is_integrated_gpu("NVIDIA GeForce RTX 4090"))
        self.assertTrue(is_virtual_gpu("OrayIddDriver Device"))
        self.assertTrue(is_virtual_gpu("Microsoft Basic Display Adapter"))
        self.assertFalse(is_virtual_gpu("AMD Radeon RX 640"))

    def test_nvidia_smi_path_honours_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"ATRIX_NVIDIA_SMI": __file__}):
            self.assertEqual(nvidia_smi_path(), __file__)

    def test_arch_and_os_description_are_plain_strings(self) -> None:
        self.assertIn(arch_name(), {"x64", "arm64", "x86", ""})
        description = os_description()
        self.assertIsInstance(description, str)
        host = os.environ.get("COMPUTERNAME", "")
        if host:
            self.assertNotIn(host, description)

    def test_collect_hardware_real_smoke(self) -> None:
        info = collect_hardware()
        self.assertIsInstance(info, HardwareInfo)
        self.assertGreater(info.cpu_threads, 0)
        self.assertGreater(len(info.os_name), 0)
        if sys.platform == "win32":
            self.assertTrue(info.cpu_model, "Windows 上 CPU 型号应可读")
            self.assertTrue(info.os_name.startswith("Windows"))
        # 显卡型号只应是型号名，不带任何路径分隔符。
        for gpu in info.gpus:
            self.assertNotIn("\\", gpu.name)
            self.assertNotIn("/", gpu.name)

    def test_generic_gpu_usage_never_raises(self) -> None:
        usage = generic_gpu_usage()
        if usage is not None:
            percent, memory_used = usage
            self.assertTrue(percent is None or 0.0 <= percent <= 100.0)
            self.assertGreaterEqual(memory_used, 0)


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
        self.assertIn("【硬件】", text)
        self.assertIn("【系统】", text)
        self.assertIn("【显卡】", text)
        self.assertIn("【磁盘】", text)
        self.assertIn("【软件】", text)
        self.assertIn("单核基准", text)
        self.assertIn("Test CPU 9000", text)
        self.assertIn("NVIDIA GeForce RTX 4090", text)
        self.assertIn("GPU 25.0%", text)
        self.assertIn("温度 60°C", text)
        self.assertIn("已运行 1小时1分", text)
        self.assertIn("系统盘", text)
        self.assertIn(format_bytes(191 * 1024 * 1024), text)
        self.assertIn("3.2%", text)  # 进程 CPU 两位小数 → 一位展示

    def test_aggregate_gpu_usage_line(self) -> None:
        snapshot = _fixed_snapshot()
        snapshot.hardware = HardwareInfo(
            gpus=[GpuInfo(name="AMD Radeon RX 640", vendor="AMD",
                          memory_total_bytes=4 * 1024**3)],
            gpu_utilization_percent=32.5,
            gpu_memory_used_bytes=2 * 1024**3,
            gpu_source="perf-counters",
        )
        text = format_resources(snapshot)
        self.assertIn("整机 GPU 3D 占用 32.5%", text)
        self.assertIn("显存占用 2.0 GB", text)

    def test_gpu_usage_unavailable_is_explicit(self) -> None:
        snapshot = _fixed_snapshot()
        snapshot.hardware = HardwareInfo(
            gpus=[GpuInfo(name="AMD Radeon RX 640", vendor="AMD")]
        )
        self.assertIn("占用不可得", format_resources(snapshot))

    def test_no_gpu_reported_explicitly(self) -> None:
        snapshot = _fixed_snapshot()
        snapshot.hardware = HardwareInfo(cpu_model="Test CPU")
        self.assertIn("未识别到可用显卡", format_resources(snapshot))

    def test_resources_to_dict_is_json_serializable_and_labelled(self) -> None:
        payload = resources_to_dict(_fixed_snapshot())
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("rss_bytes", encoded)
        self.assertEqual(payload["system"]["cpu_count"], 8)
        self.assertEqual(payload["disks"][0]["label"], "系统盘")
        self.assertNotIn("path", payload["disks"][0])
        self.assertEqual(payload["hardware"]["gpus"][0]["vendor"], "NVIDIA")
        self.assertIn("uptime_seconds", payload["process"])

    def test_report_module_is_the_single_render_source(self) -> None:
        snapshot = _fixed_snapshot()
        self.assertEqual(
            format_resources(snapshot), format_resources_from_report(snapshot)
        )


class PrivacyTests(unittest.TestCase):
    """1.1.4 修订：命令与工具输出都不得出现本机真实路径 / 用户名 / 主机名。"""

    def _assert_clean(self, text: str) -> None:
        self.assertNotIn(os.getcwd(), text)
        self.assertNotIn(_PARENT, text)
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]", text), text)
        self.assertIsNone(re.search(r"[/\\](Users|home)[/\\]", text), text)
        home = os.path.expanduser("~")
        if home and home not in {"/", "\\"}:
            self.assertNotIn(home, text)
        host = os.environ.get("COMPUTERNAME") or platform.node()
        if host:
            self.assertNotIn(host, text)

    def test_rendered_text_has_no_local_paths(self) -> None:
        self._assert_clean(format_resources(_fixed_snapshot()))

    def test_rendered_text_has_no_paths_even_with_errors(self) -> None:
        snapshot = _fixed_snapshot()
        snapshot.errors = [
            "磁盘用量采集失败（FileNotFoundError）",
            "进程采样失败（OSError）",
        ]
        text = format_resources(snapshot)
        self.assertIn("【降级】", text)
        self._assert_clean(text)

    def test_json_payload_has_no_paths(self) -> None:
        payload = resources_to_dict(_fixed_snapshot())
        self._assert_clean(json.dumps(payload, ensure_ascii=False))

    def test_real_render_has_no_local_paths(self) -> None:
        self._assert_clean(render_resources())


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
        self.assertIn("【软件】", result.content)
        self.assertIn("rss_bytes", result.data)
        self.assertNotIn(os.getcwd(), result.content)
        self.assertNotIn(os.getcwd(), result.data)

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

    def test_call_without_paths_auto_enumerates_volumes(self) -> None:
        snapshot = _fixed_snapshot()
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            return_value=snapshot,
        ) as patched:
            self._call()
        args, _kwargs = patched.call_args
        self.assertIsNone(args[0], "默认应自动枚举固定磁盘，而不是指定配置目录")

    def test_collector_crash_returns_text_not_exception(self) -> None:
        with mock.patch(
            "atri_x.tool.builtin.runtime_resources.collect_resources",
            side_effect=RuntimeError("boom"),
        ):
            result = self._call()
        self.assertIn("资源采集失败", result.content)
        self.assertIn("RuntimeError", result.content)
        self.assertNotIn("boom", result.content)


class ResourcesCommandTests(unittest.TestCase):
    def test_render_resources_uses_real_collector(self) -> None:
        text = render_resources()
        self.assertIn("ATRI-X 资源占用", text)
        self.assertIn("【磁盘】", text)

    def test_render_resources_accepts_explicit_paths(self) -> None:
        text = render_resources([os.getcwd()])
        self.assertIn("【磁盘】", text)
        self.assertNotIn(os.getcwd(), text)

    def test_handler_uses_injected_renderer(self) -> None:
        handler = new_resources(Deps(resources=lambda: "FAKE"))
        result = asyncio.run(handler.handle(None, Request(name="resources")))
        self.assertEqual(result.content, "FAKE")

    def test_handler_reports_failure_without_exception_text(self) -> None:
        def broken() -> str:
            raise RuntimeError("/leaky/path")

        handler = new_resources(Deps(resources=broken))
        result = asyncio.run(handler.handle(None, Request(name="resources")))
        self.assertIn("资源采集失败", result.content)
        self.assertIn("RuntimeError", result.content)
        self.assertNotIn("/leaky/path", result.content)

    def test_command_is_superadmin_only(self) -> None:
        """``Info.min_role`` 留空 = 路由层按"仅超管"处理（与 /doctor / /rollback 一致）。"""
        info = new_resources(Deps()).info()
        self.assertEqual(info.name, "resources")
        self.assertEqual(info.min_role, "")


if __name__ == "__main__":
    unittest.main()
