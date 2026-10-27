"""qqonebot 协议端差异表（``atri_x.platform.qq_onebot.backend``）的用例。

锁定三条不变量：

1. **默认 = NapCat、旧配置也 = NapCat**：``protocol_backend`` 键缺失时解析成
   ``napcat``，升级不自动切换协议端；SnowLuma 只有显式写入取值才生效；
2. **非法值明确报错**：取值/类型不对抛 :class:`atri_x.errors.InvalidError`，不静默回退；
3. **后端不参与地址与身份解析**：换 ``protocol_backend`` 不改 ``ws_url``、不改令牌、
   不改平台名（``qqonebot``）。
"""

from __future__ import annotations

import unittest

from atri_x.errors import InvalidError
from atri_x.platform.qq_onebot.backend import (
    BACKEND_LABELS,
    LEGACY_DEFAULT_BACKEND,
    NEW_INSTALL_BACKEND,
    PROTOCOL_BACKENDS,
    PROTOCOL_BACKEND_NAPCAT,
    PROTOCOL_BACKEND_SNOWLUMA,
    backend_choices,
    backend_label,
    backend_profile,
    detect_backend,
    normalize_protocol_backend,
    resolve_protocol_backend,
    validate_protocol_backend,
)
from atri_x.platform.qq_onebot.config import (
    DEFAULT_WS_URL,
    Config,
    apply_defaults,
    new_from_platform_config,
)


class NormalizeCase(unittest.TestCase):
    def test_none_and_empty_mean_unset(self) -> None:
        self.assertEqual(normalize_protocol_backend(None), "")
        self.assertEqual(normalize_protocol_backend(""), "")
        self.assertEqual(normalize_protocol_backend("   "), "")

    def test_value_is_stripped_and_lowered(self) -> None:
        self.assertEqual(normalize_protocol_backend("  SnowLuma "), "snowluma")
        self.assertEqual(normalize_protocol_backend("NAPCAT"), "napcat")

    def test_non_string_is_a_type_error(self) -> None:
        for raw in (123, 1.5, True, ["snowluma"]):
            with self.subTest(raw=raw), self.assertRaises(InvalidError):
                normalize_protocol_backend(raw)


class ValidateCase(unittest.TestCase):
    def test_known_values_pass(self) -> None:
        for raw in ("", "napcat", "snowluma", " SnowLuma "):
            with self.subTest(raw=raw):
                validate_protocol_backend(raw)

    def test_unknown_value_raises(self) -> None:
        for raw in ("llonebot", "napcat2", "nap cat"):
            with self.subTest(raw=raw), self.assertRaises(InvalidError) as ctx:
                validate_protocol_backend(raw)
            self.assertIn("protocol_backend", str(ctx.exception))

    def test_resolve_falls_back_to_legacy_default(self) -> None:
        self.assertEqual(resolve_protocol_backend(""), LEGACY_DEFAULT_BACKEND)
        self.assertEqual(resolve_protocol_backend(None), LEGACY_DEFAULT_BACKEND)
        self.assertEqual(resolve_protocol_backend("snowluma"), PROTOCOL_BACKEND_SNOWLUMA)
        self.assertEqual(resolve_protocol_backend(" NAPCAT "), PROTOCOL_BACKEND_NAPCAT)
        with self.assertRaises(InvalidError):
            resolve_protocol_backend("llonebot")


class ConstantsCase(unittest.TestCase):
    def test_legacy_default_is_napcat(self) -> None:
        self.assertEqual(LEGACY_DEFAULT_BACKEND, PROTOCOL_BACKEND_NAPCAT)

    def test_new_install_default_is_napcat(self) -> None:
        """默认协议端是随包内置的 NapCat；SnowLuma 是可选的备用方案。"""
        self.assertEqual(NEW_INSTALL_BACKEND, PROTOCOL_BACKEND_NAPCAT)
        self.assertIn(NEW_INSTALL_BACKEND, PROTOCOL_BACKENDS)

    def test_both_backends_are_declared(self) -> None:
        self.assertEqual(sorted(PROTOCOL_BACKENDS), ["napcat", "snowluma"])
        self.assertEqual(set(BACKEND_LABELS), set(PROTOCOL_BACKENDS))

    def test_choices_cover_backends_in_order(self) -> None:
        choices = backend_choices()
        self.assertEqual(tuple(value for value, _label in choices), PROTOCOL_BACKENDS)
        # 默认项排在第一个（GUI 下拉的默认推荐位）。
        self.assertEqual(choices[0][0], NEW_INSTALL_BACKEND)
        self.assertEqual(choices[0][1], "NapCat")
        self.assertEqual(dict(choices)[PROTOCOL_BACKEND_SNOWLUMA], "SnowLuma")

    def test_label_never_returns_empty(self) -> None:
        self.assertEqual(backend_label(""), "NapCat")
        self.assertEqual(backend_label(None), "NapCat")
        self.assertEqual(backend_label("snowluma"), "SnowLuma")
        self.assertEqual(backend_label("SNOWLUMA"), "SnowLuma")
        # 未知值只做展示，原样回显（展示层不抛异常）。
        self.assertEqual(backend_label("llonebot"), "llonebot")


class ProfileCase(unittest.TestCase):
    def test_every_backend_has_a_complete_profile(self) -> None:
        for name in PROTOCOL_BACKENDS:
            with self.subTest(name=name):
                profile = backend_profile(name)
                self.assertEqual(profile.name, name)
                self.assertTrue(profile.label)
                self.assertTrue(profile.forward_id_param)
                self.assertTrue(profile.upload_group_action)
                self.assertTrue(profile.upload_private_action)
                self.assertTrue(profile.group_file_url_action)
                self.assertTrue(profile.private_file_url_action)

    def test_empty_value_uses_legacy_profile(self) -> None:
        self.assertEqual(backend_profile("").name, LEGACY_DEFAULT_BACKEND)

    def test_snowluma_differs_where_it_must(self) -> None:
        snow = backend_profile(PROTOCOL_BACKEND_SNOWLUMA)
        napcat = backend_profile(PROTOCOL_BACKEND_NAPCAT)
        # SnowLuma 的 get_file 只覆盖图片/语音缓存；群/私聊文件必须走专有动作。
        self.assertFalse(snow.get_file_supports_file)
        self.assertTrue(napcat.get_file_supports_file)
        # 私聊文件依赖入站 file 段的 file_hash。
        self.assertTrue(snow.private_file_url_requires_hash)
        self.assertFalse(napcat.private_file_url_requires_hash)
        # 合并转发 id：SnowLuma 优先 res_id 字符串。
        self.assertEqual(snow.forward_id_param, "id")
        self.assertEqual(napcat.forward_id_param, "message_id")

    def test_invalid_backend_has_no_profile(self) -> None:
        with self.assertRaises(InvalidError):
            backend_profile("llonebot")


class DetectCase(unittest.TestCase):
    def test_detects_from_version_info_app_name(self) -> None:
        self.assertEqual(detect_backend("SnowLuma"), PROTOCOL_BACKEND_SNOWLUMA)
        self.assertEqual(detect_backend("snowluma v2.2.0"), PROTOCOL_BACKEND_SNOWLUMA)
        self.assertEqual(detect_backend("NapCat"), PROTOCOL_BACKEND_NAPCAT)
        self.assertEqual(detect_backend("NapCat.Onebot"), PROTOCOL_BACKEND_NAPCAT)

    def test_unknown_or_empty_returns_empty(self) -> None:
        for raw in ("", None, "LLOneBot", "go-cqhttp"):
            with self.subTest(raw=raw):
                self.assertEqual(detect_backend(raw), "")


class ConfigIntegrationCase(unittest.TestCase):
    def test_missing_key_resolves_to_napcat(self) -> None:
        cfg = Config()
        apply_defaults(cfg)
        self.assertEqual(cfg.protocol_backend, PROTOCOL_BACKEND_NAPCAT)

    def test_explicit_value_is_normalized(self) -> None:
        cfg = Config(protocol_backend="  SnowLuma ")
        apply_defaults(cfg)
        self.assertEqual(cfg.protocol_backend, PROTOCOL_BACKEND_SNOWLUMA)

    def test_apply_defaults_is_idempotent(self) -> None:
        cfg = Config(protocol_backend="snowluma")
        apply_defaults(cfg)
        apply_defaults(cfg)
        self.assertEqual(cfg.protocol_backend, PROTOCOL_BACKEND_SNOWLUMA)

    def test_config_object_type_error_is_invalid_error(self) -> None:
        cfg = Config(protocol_backend=123)  # type: ignore[arg-type]
        with self.assertRaises(InvalidError):
            apply_defaults(cfg)

    def test_assembly_rejects_unknown_backend(self) -> None:
        with self.assertRaises(InvalidError):
            new_from_platform_config(
                {"enabled": True, "protocol_backend": "llonebot"}, None, None, None
            )

    def test_assembly_defaults_legacy_section_to_napcat(self) -> None:
        adapter = new_from_platform_config({"enabled": True}, None, None, None)
        self.assertEqual(adapter.cfg.protocol_backend, PROTOCOL_BACKEND_NAPCAT)

    def test_backend_never_touches_ws_url(self) -> None:
        for backend in PROTOCOL_BACKENDS:
            with self.subTest(backend=backend):
                adapter = new_from_platform_config(
                    {"enabled": True, "protocol_backend": backend}, None, None, None
                )
                self.assertEqual(adapter.cfg.url, DEFAULT_WS_URL)

    def test_backend_never_touches_explicit_url_or_token(self) -> None:
        adapter = new_from_platform_config(
            {
                "enabled": True,
                "protocol_backend": "snowluma",
                "ws_url": "ws://127.0.0.1:3001/",
                "access_token": "explicit-token",
            },
            None,
            None,
            None,
        )
        self.assertEqual(adapter.cfg.url, "ws://127.0.0.1:3001/")
        self.assertEqual(adapter.cfg.access_token, "explicit-token")

    def test_platform_identity_ignores_backend(self) -> None:
        """换协议端不改变平台名（权限作用域键不得出现 backend 字样）。"""
        for backend in PROTOCOL_BACKENDS:
            with self.subTest(backend=backend):
                adapter = new_from_platform_config(
                    {"enabled": True, "protocol_backend": backend}, None, None, None
                )
                self.assertEqual(adapter.name(), "qqonebot")
                self.assertNotIn(backend, adapter.name())


if __name__ == "__main__":
    unittest.main()
