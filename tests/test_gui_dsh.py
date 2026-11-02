"""「DSH」页与 :mod:`atri_x.gui.dshconfig` / :mod:`atri_x.gui.dshcatalog` 的回归测试。

覆盖三块：

- ``[agent.dsh]`` 的读写往返、模型 ACP 取值格式、字段校验；
- 内核模型目录补丁的渲染/解析往返、按 Provider 生成、清空；
- 「DSH」页的构建与三个按钮路径（保存 / 生成模型目录 / 一键配置）。
"""

from __future__ import annotations

import os
import shutil
import tkinter as tk
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from tkinter import ttk
from unittest import mock

from atri_x.gui import dshcatalog, dshconfig, dshsmoke
from atri_x.gui.configedit import ConfigEditError, ConfigPaths, patch_section, set_env_value
from atri_x.gui.tabs import dshpanels
from atri_x.gui.tabs.dsh import DshTab

#: 测试临时目录（仓库规定：临时目录放 ``tests/_tmp_*``）。
TEMP_ROOT = Path(__file__).resolve().parent / "_tmp_gui_dsh"

#: 内置发行资产（拿真实的 app.toml/services.toml 来测）。
ASSETS = Path(__file__).resolve().parent.parent / "atri_x" / "config" / "assets_data"

#: 会被复制进临时配置目录的资产。
ASSET_FILES = ("app.toml", "services.toml", "state.toml", "atrinis.toml", "SOUL.md")

#: 模拟"Anthropic 兼容网关 + 需要自定义请求头"的 provider 名。
GATEWAY = "demo-dsh-gateway"

#: 模拟"OpenAI 兼容网关"的 provider 名。
OPENAI_GATEWAY = "demo-openai-gateway"


def _prepare_config() -> ConfigPaths:
    """复制发行资产到临时目录，并加两个模拟 provider（Anthropic / OpenAI 路径各一）。"""
    shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    config_dir = TEMP_ROOT / "config"
    config_dir.mkdir(parents=True)
    for name in ASSET_FILES:
        shutil.copyfile(ASSETS / name, config_dir / name)
    app_path = config_dir / "app.toml"
    patch_section(
        str(app_path),
        "storage",
        {
            "sessions_sqlite_path": str(TEMP_ROOT / "data" / "atrix_sessions.db"),
            "chat_history_sqlite_path": str(TEMP_ROOT / "data" / "atrix_chat_history.db"),
        },
    )
    services_path = str(config_dir / "services.toml")
    patch_section(
        services_path,
        f"providers.{GATEWAY}",
        {
            "base_url": "https://gateway.example.com/anthropic",
            "api_key_env": "DEMO_DSH_KEY",
            "models": ["demo-model-1", "demo-model-2"],
            "api_mode": "response",
        },
    )
    patch_section(
        services_path,
        f"providers.{GATEWAY}.extra_headers",
        {"x-demo-session": "demo-session"},
    )
    patch_section(
        services_path,
        f"providers.{GATEWAY}.model_configs.demo-model-1",
        {"context_window": 512000},
    )
    patch_section(
        services_path,
        f"providers.{OPENAI_GATEWAY}",
        {
            "base_url": "https://gateway.example.com/openai/v1",
            "api_key_env": "DEMO_OPENAI_KEY",
            "models": ["demo-model-1"],
        },
    )
    paths = ConfigPaths.from_config(str(app_path))
    set_env_value(paths.env_path, "DEMO_DSH_KEY", "demo-key-not-a-real-secret")
    return paths


def _read_toml(path: str) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class DshConfigCase(unittest.TestCase):
    """数据层：``[agent.dsh]`` 与内核模型目录补丁。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()

    def tearDown(self) -> None:
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    # -- [agent.dsh] -------------------------------------------------------
    def test_model_value_roundtrip(self) -> None:
        value = dshconfig.make_model_value("deepseek-official", "deepseek-v4.1-flash")
        # 内核公布的可选值是**紧凑 JSON**（逗号后没有空格），必须逐字符一致。
        self.assertEqual(value, '["deepseek-official","deepseek-v4.1-flash"]')
        self.assertEqual(
            dshconfig.parse_model_value(value), ("deepseek-official", "deepseek-v4.1-flash")
        )
        self.assertEqual(dshconfig.parse_model_value("deepseek-chat"), ("", "deepseek-chat"))
        self.assertEqual(dshconfig.parse_model_value(""), ("", ""))
        self.assertEqual(dshconfig.parse_model_value("{oops"), ("", "{oops"))
        # 旧格式（带空格）解析后保存会被归一成紧凑写法。
        normalized = dshconfig.validate_settings(
            dshconfig.DshSettings(model='["deepseek-official", "x"]')
        )
        self.assertEqual(normalized.model, '["deepseek-official","x"]')

    def test_save_and_load_settings_roundtrip(self) -> None:
        original = dshconfig.load_settings(self.paths)
        wanted = replace(
            original.with_model("demo-model-1"),
            credential_provider=GATEWAY,
            credential_protocol="anthropic",
            min_score=0.25,
            prewarm=False,
        )
        changed = dshconfig.save_settings(self.paths, wanted)
        self.assertTrue(changed)
        stored = _read_toml(self.paths.config_path)
        section = stored["agent"]["dsh"]
        self.assertEqual(section["credential_provider"], GATEWAY)
        self.assertEqual(section["credential_protocol"], "anthropic")
        self.assertAlmostEqual(section["min_score"], 0.25)
        self.assertFalse(section["prewarm"])
        self.assertEqual(section["model"], '["deepseek-official","demo-model-1"]')
        loaded = dshconfig.load_settings(self.paths)
        self.assertEqual(loaded.credential_provider, GATEWAY)
        self.assertEqual(loaded.model_id(), "demo-model-1")
        # 再写一次相同内容 = 没有改动
        self.assertFalse(dshconfig.save_settings(self.paths, loaded))

    def test_validate_settings_rejects_bad_input(self) -> None:
        with self.assertRaises(ConfigEditError):
            dshconfig.validate_settings(
                dshconfig.DshSettings(min_score=1.5)
            )
        with self.assertRaises(ConfigEditError):
            dshconfig.validate_settings(dshconfig.DshSettings(timeout_seconds=0))
        with self.assertRaises(ConfigEditError):
            dshconfig.validate_settings(
                dshconfig.DshSettings(model='["demo-provider","demo-model-1"]')
            )

    def test_suggest_anthropic_base_url(self) -> None:
        self.assertEqual(
            dshconfig.suggest_anthropic_base_url("https://opencode.ai/inference/go/openai/v1"),
            "https://opencode.ai/inference/go/anthropic",
        )
        self.assertEqual(
            dshconfig.suggest_anthropic_base_url("https://api.deepseek.com"), ""
        )
        self.assertEqual(
            dshconfig.suggest_anthropic_base_url("https://x.example.com/anthropic"), ""
        )

    def test_provider_hint_and_key_state(self) -> None:
        anthropic_hint = dshconfig.hint_for_provider(self.paths, GATEWAY)
        self.assertIn("✓", anthropic_hint)
        self.assertIn("自定义请求头", anthropic_hint)
        self.assertIn("密钥已配置", anthropic_hint)
        openai_hint = dshconfig.hint_for_provider(self.paths, OPENAI_GATEWAY)
        self.assertIn("✗", openai_hint)
        self.assertIn("建议改成", openai_hint)
        providers = dshconfig.available_providers(self.paths)
        self.assertTrue(
            dshconfig.provider_is_usable_for_dsh(providers[GATEWAY])
        )
        self.assertFalse(
            dshconfig.provider_is_usable_for_dsh(providers[OPENAI_GATEWAY])
        )

    # -- 内核模型目录 -------------------------------------------------------
    def test_catalog_render_parse_roundtrip(self) -> None:
        catalog = dshcatalog.KernelCatalog(
            models=[
                dshcatalog.KernelModel("demo-model-1", "Demo One", 512000, 64000),
                dshcatalog.KernelModel("demo:model two", "演示 2", 128000, 8192),
            ],
            model="demo-model-1",
        )
        text = dshcatalog.render_kernel_catalog(catalog)
        self.assertIn("- id: llm-deepseek", text)
        self.assertIn("contextWindow: 512000", text)
        parsed = dshcatalog.parse_kernel_catalog(text)
        self.assertEqual(parsed.ids(), ["demo-model-1", "demo:model two"])
        self.assertEqual(parsed.models[1].name, "演示 2")
        self.assertEqual(parsed.models[0].context_window, 512000)
        self.assertEqual(parsed.model, "demo-model-1")

    def test_catalog_from_provider_uses_config_window(self) -> None:
        catalog = dshcatalog.catalog_from_provider(self.paths, GATEWAY)
        self.assertEqual(catalog.ids(), ["demo-model-1", "demo-model-2"])
        self.assertEqual(catalog.models[0].context_window, 512000)
        self.assertEqual(catalog.model, "demo-model-1")
        self.assertEqual(catalog.provider, dshcatalog.DEFAULT_KERNEL_PROVIDER)

    def test_catalog_save_backup_and_clear(self) -> None:
        catalog = dshcatalog.catalog_from_provider(self.paths, GATEWAY)
        self.assertTrue(dshcatalog.save_kernel_catalog(self.paths, catalog))
        path = dshcatalog.patch_path(self.paths)
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".gui.bak"))
        # 第二次写（内容不同）应留下 .gui.bak
        second = dshcatalog.catalog_from_provider(
            self.paths,
            GATEWAY,
            existing=dshcatalog.KernelCatalog(
                models=[dshcatalog.KernelModel("demo-model-1", "Demo One", 512000, 64000)],
                model="demo-model-1",
            ),
        )
        self.assertTrue(dshcatalog.save_kernel_catalog(self.paths, second))
        self.assertTrue(os.path.exists(path + ".gui.bak"))
        loaded = dshcatalog.load_kernel_catalog(self.paths)
        self.assertTrue(loaded.present)
        self.assertEqual(loaded.ids(), ["demo-model-1", "demo-model-2"])
        self.assertEqual(loaded.models[0].name, "Demo One")
        self.assertTrue(dshcatalog.clear_kernel_catalog(self.paths))
        cleared = dshcatalog.load_kernel_catalog(self.paths)
        self.assertFalse(cleared.present)
        self.assertEqual(cleared.models, [])


class DshTabCase(unittest.TestCase):
    """「DSH」页：构建 + 保存 / 生成模型目录 / 一键配置。"""

    def setUp(self) -> None:
        self.paths = _prepare_config()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - 无显示环境
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

    def tearDown(self) -> None:
        for item in self._dialogs:
            item.stop()
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - 已经销毁
            pass
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)

    def _build(self) -> DshTab:
        frame = ttk.Frame(self.root)
        frame.pack()
        return DshTab(frame, self.paths)

    def test_builds_and_lists_providers(self) -> None:
        tab = self._build()
        values = list(tab.provider_box.cget("values"))
        self.assertIn(dshconfig.AUTO_PROVIDER_LABEL, values)
        self.assertIn(GATEWAY, values)
        self.assertEqual(tab.min_score_var.get(), "0.4")
        self.assertTrue(tab.hint_var.get().strip())
        self.assertTrue(tab.catalog_state_var.get().strip())

    def test_save_writes_agent_dsh(self) -> None:
        tab = self._build()
        tab.provider_var.set(GATEWAY)
        tab.protocol_var.set(dshconfig.PROTOCOL_LABELS["auto"])
        tab.min_score_var.set("0.3")
        self.assertTrue(tab._save())
        section = _read_toml(self.paths.config_path)["agent"]["dsh"]
        self.assertEqual(section["credential_provider"], GATEWAY)
        self.assertEqual(section["credential_protocol"], "auto")
        self.assertAlmostEqual(section["min_score"], 0.3)
        self.assertEqual(tab.settings.credential_provider, GATEWAY)

    def test_generate_catalog_writes_patch(self) -> None:
        tab = self._build()
        tab.provider_var.set(GATEWAY)
        tab._generate_catalog()
        loaded = dshcatalog.load_kernel_catalog(self.paths)
        self.assertTrue(loaded.present)
        self.assertEqual(loaded.ids(), ["demo-model-1", "demo-model-2"])
        self.assertEqual(tab.model_var.get(), "demo-model-1")
        self.assertNotIn("不在内核目录里", tab.hint_var.get())

    def test_apply_recommended_configures_everything(self) -> None:
        tab = self._build()
        tab._apply_recommended()
        section = _read_toml(self.paths.config_path)["agent"]["dsh"]
        self.assertTrue(section["enabled"])
        self.assertEqual(section["credential_provider"], GATEWAY)
        self.assertEqual(section["credential_protocol"], "auto")
        self.assertEqual(
            dshconfig.parse_model_value(section["model"]),
            ("deepseek-official", "demo-model-1"),
        )
        self.assertTrue(dshcatalog.load_kernel_catalog(self.paths).present)

    def test_run_smoke_reports_credential_unusable(self) -> None:
        """凭据判定不可用时自检要**不拉内核**就返回原因（端到端自检的失败路径）。"""
        set_env_value(self.paths.env_path, "DEMO_OPENAI_KEY", "demo-key-not-a-real-secret")
        patch_section(
            self.paths.config_path,
            "agent.dsh",
            {
                "credential_provider": OPENAI_GATEWAY,
                "credential_protocol": "auto",
                "enabled": True,
            },
        )
        result = dshsmoke.run_smoke(self.paths.config_path)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "credential")
        self.assertIn("Anthropic Messages", str(result["error"]))

    def test_panels_mixin_exposes_sections(self) -> None:
        for name in (
            "_build_catalog",
            "_build_status",
            "_apply_recommended",
            "_generate_catalog",
            "_run_smoke",
            "_refresh_status",
        ):
            self.assertTrue(callable(getattr(dshpanels.DshPanelsMixin, name)), name)



if __name__ == "__main__":  # pragma: no cover - 手工跑单个模块
    unittest.main()
