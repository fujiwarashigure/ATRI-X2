"""``safety.toml`` 数据目录 + 热加载 + 用户词表/正则（1.1.8）。"""

from __future__ import annotations

import os
import shutil
import unittest

from atri_x.safety.engine import SafetyEngine, build_engine, safety_dirs
from atri_x.safety.rules import load_custom_patterns, load_user_wordlists
from atri_x.safety.settings import (
    default_settings,
    fail_mode_for,
    load_safety_settings,
    local_policy,
    stage_policy,
)

TMP_ROOT = os.path.join(os.path.dirname(__file__), "_tmp_safety_settings")

TOML = """
version = 1

[local]
enabled = true
mode = "enforce"
normalize = true
wordlist_dir = "safety/wordlists"
sensitive_dir = "safety/sensitive"

[remote]
enabled = true
providers = ["tencent", "aliyun"]
quorum = "any_block"
fail_mode = "open"
fail_close_scopes = ["qq:group:high"]

[remote.tencent]
secret_id_env = "T_SECRET_ID"
secret_key_env = "T_SECRET_KEY"
region = "ap-guangzhou"

[media]
images = "sync"

[cost]
daily_budget_cny = 5.0
text_price_cny_per_10k = 22.0

[tools]
web_search = "local_only"
image_generate = "sync"

[scopes."qq:group:high"]
fail_mode = "closed"
"""


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = os.path.join(TMP_ROOT, self._testMethodName)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path


class TestLoad(SettingsCase):
    def test_missing_file_means_defaults(self) -> None:
        settings, errors = load_safety_settings(os.path.join(self.dir, "safety.toml"))
        self.assertEqual(errors, [])
        self.assertFalse(settings.remote.enabled)
        self.assertEqual(settings.local.mode, "enforce")
        self.assertFalse(default_settings().remote.enabled)

    def test_load_and_sanitize(self) -> None:
        path = self.write("safety.toml", TOML)
        settings, errors = load_safety_settings(path)
        self.assertEqual(errors, [])
        self.assertTrue(settings.remote.enabled)
        self.assertEqual(settings.remote.providers, ["tencent", "aliyun"])
        self.assertEqual(settings.tools["web_search"], "local_only")
        # 默认值被补齐
        self.assertEqual(settings.remote.stages["tool"], "cache_only")
        self.assertEqual(settings.remote.stages["inbound"], "sync")

    def test_bad_toml_falls_back_to_defaults(self) -> None:
        path = self.write("safety.toml", "this is not toml [[[")
        settings, errors = load_safety_settings(path)
        self.assertTrue(errors)
        self.assertFalse(settings.remote.enabled)

    def test_invalid_enums_are_corrected(self) -> None:
        path = self.write(
            "safety.toml",
            '[local]\nmode = "banana"\n[remote]\nfail_mode = "sometimes"\nquorum = "nope"\n',
        )
        settings, errors = load_safety_settings(path)
        self.assertEqual(errors, [])
        self.assertEqual(settings.local.mode, "audit")
        self.assertEqual(settings.remote.fail_mode, "open")
        self.assertEqual(settings.remote.quorum, "any_block")

    def test_stage_and_fail_mode_lookup(self) -> None:
        settings, _ = load_safety_settings(self.write("safety.toml", TOML))
        self.assertEqual(stage_policy(settings, "web_search"), "local_only")
        self.assertEqual(stage_policy(settings, "inbound"), "sync")
        self.assertEqual(stage_policy(settings, "image_generate"), "sync")
        self.assertEqual(fail_mode_for(settings, "qq:group:high"), "closed")
        self.assertEqual(fail_mode_for(settings, "qq:group:normal"), "open")

    def test_local_policy_maps_to_safety_policy(self) -> None:
        settings, _ = load_safety_settings(self.write("safety.toml", TOML))
        policy = local_policy(settings)
        self.assertTrue(policy.active)
        self.assertTrue(policy.enforcing)
        self.assertEqual(policy.max_scan_chars, 8000)


class TestUserData(SettingsCase):
    def test_user_wordlists_merge_and_add_category(self) -> None:
        self.write("safety/wordlists/illegal_method.txt", "自定义词甲\n自定义词乙\n")
        self.write("safety/wordlists/my_category.txt", "只在这个类别里\n")
        found = load_user_wordlists(os.path.join(self.dir, "safety", "wordlists"))
        self.assertEqual(found["illegal_method"], ["自定义词甲", "自定义词乙"])
        self.assertIn("my_category", found)

    def test_custom_regex_is_loaded_and_invalid_skipped(self) -> None:
        self.write(
            "safety/sensitive/extra.regex",
            "# 注释\n"
            "工号 = /EMP-\\d{6}/\n"
            "坏正则 = /([a-z/\n",
        )
        patterns = load_custom_patterns(os.path.join(self.dir, "safety", "sensitive"))
        names = [item[0] for item in patterns]
        self.assertIn("工号", names)
        self.assertNotIn("坏正则", names)

    def test_engine_uses_user_wordlist_after_reload(self) -> None:
        self.write("safety.toml", '[local]\nenabled = true\nmode = "enforce"\n')
        engine = SafetyEngine(config_dir=self.dir)
        self.assertTrue(engine.active)
        # 初始没有该词
        _text, hits = engine.rules.inspect("这是自定义词甲的内容")
        self.assertFalse([hit for hit in hits if hit.category == "illegal_method"])
        # 加词表 + 改文件（大小变化）→ refresh 必须重建
        self.write("safety/wordlists/illegal_method.txt", "自定义词甲\n")
        self.assertTrue(engine.refresh(force=True))
        _text, hits = engine.rules.inspect("这是自定义词甲的内容")
        self.assertIn("illegal_method", [hit.category for hit in hits])

    def test_engine_disabled_without_safety_toml(self) -> None:
        self.assertIsNone(build_engine(config_dir=self.dir))
        engine = build_engine(config_dir=self.write_dir_with_toml())
        self.assertIsNotNone(engine)
        self.assertTrue(engine.active)

    def write_dir_with_toml(self) -> str:
        self.write("safety.toml", "[local]\nenabled = true\n")
        return self.dir


class TestHotReload(SettingsCase):
    def test_refresh_detects_new_scope_file(self) -> None:
        self.write("safety.toml", "[local]\nenabled = true\n")
        engine = SafetyEngine(config_dir=self.dir)
        self.assertIsNone(engine.scope_for("qq", "g1"))
        self.write(
            "safety/scopes/qq__group__g1.toml",
            'platform = "qq"\nscope_id = "g1"\nfail_mode = "closed"\n',
        )
        engine.refresh(force=True)
        scope = engine.scope_for("qq", "g1")
        self.assertIsNotNone(scope)
        self.assertEqual(engine.fail_mode("qq:g1"), "closed")

    def test_broken_reload_keeps_previous_settings(self) -> None:
        self.write("safety.toml", '[local]\nenabled = true\nmode = "enforce"\n[tools]\nweb_search = "local_only"\n')
        engine = SafetyEngine(config_dir=self.dir)
        before = engine.settings.tools["web_search"]
        self.write("safety.toml", "broken [[[")
        engine.refresh(force=True)
        self.assertEqual(engine.settings.tools["web_search"], before)
        self.assertTrue(engine.reload_errors)

    def test_safety_dirs_resolve_relative_paths(self) -> None:
        self.write("safety.toml", "[local]\nenabled = true\n")
        dirs = safety_dirs(self.dir)
        self.assertTrue(dirs["config"].endswith("safety.toml"))
        self.assertTrue(dirs["wordlists"].endswith(os.path.join("safety", "wordlists")))
        self.assertTrue(dirs["review_db"].endswith(os.path.join("safety", "review.db")))
