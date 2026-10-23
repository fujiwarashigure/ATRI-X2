r"""``atri_x.safety`` 单元测试：策略装载、词表命中、脱敏与幂等、审计、模式语义。

刻意**只用标准库**（``unittest`` + 仓库根的 ``tmp_safety_test/`` 临时目录），
避免引入第三方面试框架；也和本仓其它测试一样不碰 ``tempfile.mkdtemp``
（本机以 0o700 建目录，清理钩子会抛 ``PermissionError``）。

覆盖的目标（对应 ``docs/safety.md`` 的"验证"一节）：

* ``policy_from_section`` 的默认值 / 归一化 / 鸭子类型读取；
* 8 类词表可加载、命中即 ``block``；
* 敏感信息 ``redact`` 与 ``flag`` 的默认动作、``allowlist`` 豁免；
* **幂等**：二次脱敏不会继续啃掉文本尾巴；
* ``audit`` 只记审计不改输出，且审计里**没有原文**；
* ``enforce`` 的 block / redact；
* ``mode = off`` / ``enabled = false`` 完全无行为变化；
* 图片 / 文件出口判定；
* 审计写失败只记日志、不抛异常。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import types
import unittest

from atri_x.safety import (
    CATEGORY_IDS,
    SENSITIVE_IDS,
    SafetyGuard,
    SafetyMode,
    SafetyPolicy,
    available_wordlists,
    load_all_wordlists,
    load_wordlist,
    merge_policy,
    mode_from_host,
    policies_from_section,
    policy_from_section,
    wordlist_path,
)

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp_safety_test")


class TempCase(unittest.TestCase):
    """每个用例一个独立子目录，退出时整体清掉。"""

    case_name = "case"

    def setUp(self) -> None:
        self.dir = os.path.join(TMP_ROOT, self.__class__.__name__, self.case_name)
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)
        self.audit_path = os.path.join(self.dir, "safety-audit.jsonl")

    def guard(self, **overrides) -> SafetyGuard:
        payload = {
            "enabled": True,
            "mode": SafetyMode.ENFORCE,
            "audit_path": self.audit_path,
        }
        payload.update(overrides)
        return SafetyGuard(SafetyPolicy(**payload))

    def audit_records(self, path: str | None = None) -> list[dict]:
        target = path or self.audit_path
        if not os.path.exists(target):
            return []
        with open(target, "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle.read().splitlines() if line.strip()]


class TestPolicyFromSection(TempCase):
    def test_defaults_from_empty_section(self) -> None:
        policy = policy_from_section(types.SimpleNamespace())
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.mode, SafetyMode.AUDIT)
        self.assertFalse(policy.active)
        self.assertEqual(policy.effective_mode(), SafetyMode.OFF)
        self.assertEqual(policy.enabled_categories(), CATEGORY_IDS)
        self.assertEqual(policy.enabled_sensitive(), SENSITIVE_IDS)
        self.assertEqual(policy.audit_preview_chars, 120)
        self.assertEqual(policy.max_scan_chars, 8000)
        self.assertEqual(policy.block_reply_text(), policy.block_reply_text().strip())

    def test_none_section_is_off(self) -> None:
        self.assertFalse(policy_from_section(None).active)

    def test_invalid_mode_falls_back_to_audit(self) -> None:
        policy = policy_from_section(types.SimpleNamespace(enabled=True, mode="bogus"))
        self.assertEqual(policy.mode, SafetyMode.AUDIT)
        self.assertTrue(policy.active)

    def test_off_mode_is_inactive(self) -> None:
        policy = policy_from_section(types.SimpleNamespace(enabled=True, mode="OFF"))
        self.assertEqual(policy.effective_mode(), SafetyMode.OFF)
        self.assertFalse(policy.active)

    def test_category_subset_keeps_builtin_order(self) -> None:
        policy = policy_from_section(
            types.SimpleNamespace(
                enabled=True,
                categories=["violence_gore", "unknown_category", "illegal_method"],
            )
        )
        self.assertEqual(policy.enabled_categories(), ("illegal_method", "violence_gore"))

    def test_sensitive_actions_override(self) -> None:
        policy = policy_from_section(
            types.SimpleNamespace(
                enabled=True,
                sensitive_actions={"phone": "redact", "bogus": "explode"},
            )
        )
        self.assertEqual(policy.action_for("phone"), "redact")
        self.assertEqual(policy.action_for("email"), "flag")
        self.assertEqual(policy.action_for("api_key"), "redact")
        self.assertNotIn("bogus", dict(policy.sensitive_actions))

    def test_mapping_section_supported(self) -> None:
        policy = policy_from_section(
            {
                "enabled": True,
                "mode": "enforce",
                "categories": ["self_harm"],
                "allowlist": ["示例"],
                "strip_tool_images": True,
            }
        )
        self.assertTrue(policy.enforcing)
        self.assertEqual(policy.enabled_categories(), ("self_harm",))
        self.assertEqual(policy.allowlist, ("示例",))
        self.assertTrue(policy.strip_tool_images)

    def test_numeric_and_list_hygiene(self) -> None:
        policy = policy_from_section(
            types.SimpleNamespace(
                enabled="true",
                audit_preview_chars=0,
                max_scan_chars=-5,
                categories="illegal_method",
            )
        )
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.audit_preview_chars, 120)
        self.assertEqual(policy.max_scan_chars, 8000)
        self.assertEqual(policy.enabled_categories(), ("illegal_method",))

    def test_audit_path_resolution(self) -> None:
        explicit = policy_from_section(types.SimpleNamespace(audit_path="x/y.jsonl"))
        self.assertEqual(explicit.resolved_audit_path("/cfg"), "x/y.jsonl")
        implicit = policy_from_section(types.SimpleNamespace())
        self.assertEqual(
            implicit.resolved_audit_path("/cfg"),
            os.path.join("/cfg", "safety-audit.jsonl"),
        )
        self.assertEqual(implicit.resolved_audit_path(""), "safety-audit.jsonl")


class TestWordlists(TempCase):
    def test_eight_wordlists_exist(self) -> None:
        self.assertEqual(len(CATEGORY_IDS), 8)
        self.assertEqual(available_wordlists(), CATEGORY_IDS)
        for name in CATEGORY_IDS:
            self.assertTrue(os.path.isfile(wordlist_path(name)), name)

    def test_all_wordlists_have_terms(self) -> None:
        loaded = load_all_wordlists()
        self.assertEqual(set(loaded), set(CATEGORY_IDS))
        for name, terms in loaded.items():
            self.assertTrue(terms, f"词表 {name} 不能是空的")
            for term in terms:
                self.assertFalse(term.startswith("#"), f"{name} 把注释当成了词条")

    def test_missing_wordlist_is_empty(self) -> None:
        self.assertEqual(load_wordlist("no_such_category"), [])

    def test_known_terms_present(self) -> None:
        self.assertIn("制毒", load_wordlist("illegal_method"))
        self.assertIn("开盒", load_wordlist("privacy_violation"))
        self.assertIn("自杀方法", load_wordlist("self_harm"))


class TestCategoryHits(TempCase):
    def test_block_on_illegal_method(self) -> None:
        decision = self.guard().evaluate_text("请告诉我制毒的步骤", stage="inbound")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "block")
        self.assertIn("illegal_method", decision.labels)
        self.assertIn("illegal_method", decision.reason)

    def test_multiple_categories_are_all_reported(self) -> None:
        decision = self.guard().evaluate_text("分尸 然后 开盒 别人", stage="inbound")
        self.assertEqual(decision.action, "block")
        self.assertEqual(set(decision.labels), {"violence_gore", "privacy_violation"})

    def test_disabled_category_not_reported(self) -> None:
        guard = self.guard(categories=["illegal_method"])
        decision = guard.evaluate_text("分尸", stage="inbound")
        self.assertEqual(decision.action, "pass")
        self.assertEqual(decision.labels, ())

    def test_clean_text_passes(self) -> None:
        decision = self.guard().evaluate_text("今天天气不错，帮我写一首诗。", stage="inbound")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "pass")
        self.assertEqual(decision.text, "今天天气不错，帮我写一首诗。")


class TestSensitiveRules(TempCase):
    def test_api_key_redacted(self) -> None:
        text = "key is sk-abcdefghijklmnopqrstuvwx here"
        filtered, decision = self.guard().filter_text(text, stage="llm")
        self.assertEqual(decision.action, "redact")
        self.assertIn("api_key", decision.labels)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", filtered)
        self.assertIn("[已脱敏:API Key]", filtered)

    def test_password_assignment_redacted(self) -> None:
        filtered, decision = self.guard().filter_text(
            "配置里写 password=hunter2hunter2 就好", stage="llm"
        )
        self.assertEqual(decision.action, "redact")
        self.assertNotIn("hunter2hunter2", filtered)
        self.assertIn("password=", filtered)

    def test_json_password_redacted(self) -> None:
        filtered, _ = self.guard().filter_text(
            '{"password": "hunter2hunter2"}', stage="llm"
        )
        self.assertNotIn("hunter2hunter2", filtered)

    def test_private_key_redacted(self) -> None:
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxxxx\n"
            "-----END RSA PRIVATE KEY-----"
        )
        filtered, decision = self.guard().filter_text("前 " + block + " 后", stage="tool")
        self.assertEqual(decision.action, "redact")
        self.assertIn("private_key", decision.labels)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", filtered)
        self.assertTrue(filtered.startswith("前 "))
        self.assertTrue(filtered.endswith(" 后"))

    def test_jwt_redacted(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
        filtered, decision = self.guard().filter_text("token " + token, stage="llm")
        self.assertEqual(decision.action, "redact")
        self.assertIn("jwt", decision.labels)
        self.assertNotIn(token, filtered)

    def test_flagged_categories_do_not_change_text(self) -> None:
        text = "联系 13812345678 或 a.b@example.com，机器 192.168.1.10，路径 C:\\Users\\x\\y.txt"
        filtered, decision = self.guard().filter_text(text, stage="send")
        self.assertEqual(decision.action, "flag")
        self.assertEqual(filtered, text)
        self.assertTrue({"phone", "email", "internal_ip", "local_path"} <= set(decision.labels))

    def test_id_card_and_bank_card_are_flag_by_default(self) -> None:
        text = "身份证 110101199003071234 卡号 6222021234567890123"
        filtered, decision = self.guard().filter_text(text, stage="send")
        self.assertEqual(filtered, text)
        self.assertIn("id_card", decision.labels)
        self.assertIn("bank_card", decision.labels)

    def test_override_flag_to_redact(self) -> None:
        guard = self.guard(sensitive_actions={"phone": "redact"})
        filtered, decision = guard.filter_text("电话 13812345678", stage="send")
        self.assertEqual(decision.action, "redact")
        self.assertNotIn("13812345678", filtered)
        self.assertIn("[已脱敏:手机号]", filtered)

    def test_sensitive_subset_only(self) -> None:
        guard = self.guard(sensitive=["api_key"])
        _, decision = guard.filter_text("电话 13812345678 key sk-abcdefghijklmnopqrstuvwx", stage="send")
        self.assertEqual(decision.labels, ("api_key",))


class TestIdempotency(TempCase):
    SAMPLES = (
        "key sk-abcdefghijklmnopqrstuvwx 结束",
        "password=hunter2hunter2 尾巴",
        "身份证 110101199003071234 卡号 6222021234567890123 结束",
        "邮箱 a.b@example.com 内网 192.168.1.10 路径 D:\\work\\secret.txt",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
        "私钥 -----BEGIN PRIVATE KEY-----MIIBVAIBADANBg-----END PRIVATE KEY----- 结束",
        "混合 sk-abcdefghijklmnopqrstuvwx 与 13812345678 以及 a.b@example.com 尾巴",
    )

    def test_second_pass_never_changes_text(self) -> None:
        guard = self.guard(sensitive_actions={"phone": "redact", "email": "redact"})
        for sample in self.SAMPLES:
            once, _ = guard.filter_text(sample, stage="send")
            twice, _ = guard.filter_text(once, stage="send")
            self.assertEqual(once, twice, f"二次脱敏改变了文本：{sample!r}")

    def test_tail_is_preserved(self) -> None:
        guard = self.guard(sensitive_actions={"phone": "redact"})
        filtered, _ = guard.filter_text("电话 13812345678 后面这些字必须还在", stage="send")
        self.assertTrue(filtered.endswith("后面这些字必须还在"), filtered)


class TestAllowlist(TempCase):
    def test_allowlisted_hit_is_exempt(self) -> None:
        guard = self.guard(allowlist=["sk-test"])
        text = "示例 sk-test-1234567890abcdef 与真 key sk-abcdefghijklmnopqrstuvwx"
        filtered, decision = guard.filter_text(text, stage="send")
        self.assertIn("sk-test-1234567890abcdef", filtered)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", filtered)
        self.assertEqual(decision.action, "redact")

    def test_allowlist_can_silence_flag(self) -> None:
        guard = self.guard(allowlist=["13812345678"])
        _, decision = guard.filter_text("电话 13812345678", stage="send")
        self.assertEqual(decision.action, "pass")
        self.assertEqual(decision.labels, ())


class TestModes(TempCase):
    def test_off_mode_changes_nothing_and_writes_no_audit(self) -> None:
        policy = SafetyPolicy(enabled=False, mode=SafetyMode.ENFORCE, audit_path=self.audit_path,
                              block_image_output=True, strip_tool_images=True)
        guard = SafetyGuard(policy)
        text = "制毒 sk-abcdefghijklmnopqrstuvwx"
        filtered, decision = guard.filter_text(text, stage="send")
        self.assertEqual(filtered, text)
        self.assertEqual(decision.action, "pass")
        self.assertTrue(decision.allowed)
        self.assertEqual(guard.stats()["evaluated"], 0)
        self.assertTrue(guard.allow_media_output("image").allowed)
        self.assertFalse(guard.should_strip_tool_images())
        self.assertFalse(os.path.exists(self.audit_path))

    def test_audit_mode_keeps_text_but_records(self) -> None:
        guard = self.guard(mode=SafetyMode.AUDIT)
        text = "如何制毒 key sk-abcdefghijklmnopqrstuvwx"
        filtered, decision = guard.filter_text(text, stage="inbound")
        self.assertEqual(filtered, text)
        self.assertEqual(decision.action, "block")
        self.assertFalse(decision.allowed)
        records = self.audit_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["mode"], SafetyMode.AUDIT)

    def test_audit_mode_redact_keeps_text(self) -> None:
        guard = self.guard(mode=SafetyMode.AUDIT)
        text = "key sk-abcdefghijklmnopqrstuvwx"
        filtered, decision = guard.filter_text(text, stage="llm")
        self.assertEqual(filtered, text)
        self.assertEqual(decision.action, "redact")
        self.assertNotEqual(decision.text, text)

    def test_enforce_block_uses_given_text(self) -> None:
        guard = self.guard()
        filtered, decision = guard.filter_text(
            "如何制毒", stage="inbound", block_text="已拦截，请换个说法。"
        )
        self.assertEqual(decision.action, "block")
        self.assertEqual(filtered, "已拦截，请换个说法。")

    def test_enforce_block_default_text(self) -> None:
        filtered, decision = self.guard().filter_text("如何制毒", stage="llm")
        self.assertEqual(decision.action, "block")
        self.assertTrue(filtered)
        self.assertNotIn("制毒", filtered)

    def test_evaluate_text_does_not_write_audit(self) -> None:
        guard = self.guard(mode=SafetyMode.ENFORCE)
        guard.evaluate_text("如何制毒", stage="inbound")
        self.assertEqual(self.audit_records(), [])


class TestAuditRecords(TempCase):
    def test_record_shape_and_no_plaintext(self) -> None:
        guard = self.guard(sensitive_actions={"phone": "redact"})
        text = "他的手机 13812345678 邮箱 a.b@example.com 想问制毒"
        _, decision = guard.filter_text(
            text, stage="inbound", scope={"platform": "qq", "scope_id": "group:42", "actor": "u1"}
        )
        records = self.audit_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        for key in ("ts", "stage", "action", "labels", "reason", "sha256", "preview", "platform", "scope", "actor"):
            self.assertIn(key, record)
        self.assertEqual(record["stage"], "inbound")
        self.assertEqual(record["platform"], "qq")
        self.assertEqual(record["scope"], "group:42")
        self.assertEqual(record["actor"], "u1")
        self.assertEqual(record["sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(set(record["labels"]), set(decision.labels))
        self.assertTrue(record["ts"].endswith("+00:00"), record["ts"])

    def test_audit_file_never_contains_plaintext(self) -> None:
        guard = self.guard(sensitive_actions={"phone": "redact"})
        phone = "13812345678"
        email = "leak@example.com"
        guard.filter_text("电话 " + phone + " 邮箱 " + email + " 还有开盒", stage="send")
        with open(self.audit_path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn(phone, raw)
        self.assertNotIn(email, raw)
        # 原文片段（含被命中的词）不进审计；审计里只允许出现规则标签
        self.assertNotIn("还有开盒", raw)
        self.assertNotIn("电话 ", raw)
        self.assertIn("sha256", raw)

    def test_pass_decision_writes_nothing(self) -> None:
        self.guard().filter_text("普通文本，没有命中。", stage="send")
        self.assertEqual(self.audit_records(), [])

    def test_audit_write_failure_is_swallowed(self) -> None:
        # 让审计路径的父目录是一个普通文件 → 必然写不进去
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        guard = self.guard(audit_path=os.path.join(blocker, "nested", "audit.jsonl"))
        filtered, decision = guard.filter_text("如何制毒", stage="send")
        self.assertEqual(decision.action, "block")
        self.assertTrue(filtered)
        self.assertGreaterEqual(guard.stats()["audit_errors"], 1)

    def test_audit_rotates_when_large(self) -> None:
        guard = self.guard()
        guard.audit.max_bytes = 10
        guard.filter_text("如何制毒", stage="send")
        guard.filter_text("如何制毒", stage="send")
        self.assertTrue(os.path.exists(self.audit_path + ".1"))


class TestMediaOutput(TempCase):
    def test_image_blocked_in_enforce(self) -> None:
        guard = self.guard(block_image_output=True)
        decision = guard.allow_media_output(
            "image", stage="send", scope={"platform": "qq"}, meta={"name": "a.png"}
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "block")
        self.assertIn("media.image", decision.labels)
        self.assertTrue(decision.text)
        records = self.audit_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["meta"]["kind"], "image")

    def test_file_not_configured_passes(self) -> None:
        guard = self.guard(block_image_output=True)
        decision = guard.allow_media_output("file")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "pass")

    def test_file_blocked_in_enforce(self) -> None:
        guard = self.guard(block_file_output=True)
        decision = guard.allow_media_output("file")
        self.assertFalse(decision.allowed)
        self.assertIn("media.file", decision.labels)

    def test_media_audit_mode_still_allows(self) -> None:
        guard = self.guard(mode=SafetyMode.AUDIT, block_image_output=True)
        decision = guard.allow_media_output("image")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "flag")
        self.assertEqual(len(self.audit_records()), 1)

    def test_unknown_kind_passes(self) -> None:
        decision = self.guard(block_file_output=True).allow_media_output("record")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "pass")

    def test_strip_tool_images_requires_enforce(self) -> None:
        self.assertTrue(self.guard(strip_tool_images=True).should_strip_tool_images())
        audit_guard = self.guard(strip_tool_images=True, mode=SafetyMode.AUDIT)
        self.assertFalse(audit_guard.should_strip_tool_images())


class TestStats(TempCase):
    def test_counters(self) -> None:
        guard = self.guard()
        guard.filter_text("干净文本", stage="send")
        guard.filter_text("key sk-abcdefghijklmnopqrstuvwx", stage="send")
        guard.filter_text("如何制毒", stage="send")
        guard.allow_media_output("image")  # 未配置 block_image_output → 完全空操作
        stats = guard.stats()
        self.assertEqual(stats["evaluated"], 3)
        self.assertEqual(stats["pass"], 1)
        self.assertEqual(stats["redact"], 1)
        self.assertEqual(stats["block"], 1)
        self.assertEqual(stats["media_blocked"], 0)
        self.assertEqual(stats["audit_written"], 2)

    def test_image_blocked_counter(self) -> None:
        guard = self.guard(block_image_output=True)
        guard.allow_media_output("image")
        self.assertEqual(guard.stats()["media_blocked"], 1)


class TestMaxScanChars(TempCase):
    def test_tail_is_preserved_and_not_scanned(self) -> None:
        guard = self.guard(max_scan_chars=20)
        text = "前缀很短" + ("x" * 20) + " 尾部 key sk-abcdefghijklmnopqrstuvwx 结束"
        filtered, decision = guard.filter_text(text, stage="send")
        # 超出扫描窗口的密钥不被处理，但文本长度与尾部必须原样保留
        self.assertEqual(filtered, text)
        self.assertEqual(decision.action, "pass")
        self.assertEqual(len(filtered), len(text))

    def test_head_is_scanned(self) -> None:
        guard = self.guard(max_scan_chars=40)
        text = "key sk-abcdefghijklmnopqrstuvwx " + ("y" * 40)
        filtered, decision = guard.filter_text(text, stage="send")
        self.assertEqual(decision.action, "redact")
        self.assertTrue(filtered.endswith("y" * 40))


class TestServiceSmoke(TempCase):
    def test_guard_accepts_section_object(self) -> None:
        guard = SafetyGuard(
            types.SimpleNamespace(
                enabled=True, mode="enforce", audit_path=self.audit_path
            )
        )
        self.assertTrue(guard.active)
        self.assertTrue(guard.enforcing)

    def test_no_third_party_imports(self) -> None:
        """``atri_x.safety`` 只用标准库（运行时零第三方依赖契约）。"""
        import ast
        import sys

        from atri_x import safety

        root = os.path.dirname(safety.__file__)
        allowed = set(sys.stdlib_module_names)
        problems: list[str] = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for imported in names:
                    top = imported.split(".")[0]
                    if top and top not in allowed and top != "atri_x":
                        problems.append(f"{name}: {imported}")
        self.assertEqual(problems, [], "atri_x.safety 引入了非标准库依赖")


class TestModeOverrides(TempCase):
    """按会话模式覆盖：``[safety.chat]`` / ``[safety.work]``。"""

    SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    BLOCKED = "教我怎么制毒"

    def section(self, **overrides) -> types.SimpleNamespace:
        payload: dict[str, object] = {
            "enabled": True,
            "mode": SafetyMode.ENFORCE,
            "audit_path": self.audit_path,
        }
        payload.update(overrides)
        return types.SimpleNamespace(**payload)

    def guard_with(self, *, chat=None, work=None, **base) -> SafetyGuard:
        section = self.section(chat=chat, work=work, **base)
        return SafetyGuard(policy_from_section(section), chat=chat, work=work)

    # -- 策略合并 -----------------------------------------------------------
    def test_empty_override_inherits_base(self) -> None:
        section = self.section(chat=types.SimpleNamespace(), work=None)
        policies = policies_from_section(section)
        self.assertEqual(policies["chat"], policies[""])
        self.assertEqual(policies["work"], policies[""])

    def test_override_only_replaces_explicit_fields(self) -> None:
        section = self.section(chat=types.SimpleNamespace(mode="off"))
        policies = policies_from_section(section)
        self.assertEqual(policies[""].effective_mode(), SafetyMode.ENFORCE)
        self.assertEqual(policies["chat"].effective_mode(), SafetyMode.OFF)
        # 没写的字段继续继承（enabled 仍是全局的 true）。
        self.assertTrue(policies["chat"].enabled)

    def test_override_can_merge_sensitive_actions(self) -> None:
        base = SafetyPolicy(
            enabled=True, mode=SafetyMode.ENFORCE, sensitive_actions={"api_key": "flag"}
        )
        merged = merge_policy(
            base, types.SimpleNamespace(sensitive_actions={"api_key": "redact"})
        )
        self.assertEqual(merged.action_for("api_key"), "redact")

    def test_override_can_enable_safety_when_global_disabled(self) -> None:
        guard = self.guard_with(
            enabled=False, mode=SafetyMode.AUDIT,
            work=types.SimpleNamespace(enabled=True, mode=SafetyMode.AUDIT),
        )
        self.assertFalse(guard.active)
        self.assertTrue(guard.active_for("work"))
        self.assertEqual(guard.mode_for("work"), SafetyMode.AUDIT)
        filtered, decision = guard.filter_text(
            self.BLOCKED, stage="inbound", mode="work"
        )
        self.assertEqual(filtered, self.BLOCKED)  # audit 不改文本
        self.assertEqual(decision.action, "block")
        self.assertEqual(len(self.audit_records()), 1)
        # 全局默认（不传 mode）仍然按 off 短路，不写审计。
        guard.filter_text(self.BLOCKED, stage="inbound")
        self.assertEqual(len(self.audit_records()), 1)

    # -- 判定 / 出口 --------------------------------------------------------
    def test_chat_override_off_keeps_text_without_audit(self) -> None:
        guard = self.guard_with(chat=types.SimpleNamespace(mode="off"))
        filtered, decision = guard.filter_text(self.SECRET, stage="send", mode="chat")
        self.assertEqual(filtered, self.SECRET)
        self.assertEqual(decision.action, "pass")
        self.assertEqual(self.audit_records(), [])
        # work 没覆盖，继续按全局 enforce 脱敏并写审计。
        filtered, _ = guard.filter_text(self.SECRET, stage="send", mode="work")
        self.assertNotIn(self.SECRET, filtered)
        self.assertEqual(len(self.audit_records()), 1)

    def test_media_override_is_per_mode(self) -> None:
        guard = self.guard_with(
            work=types.SimpleNamespace(block_image_output=True)
        )
        self.assertFalse(guard.allow_media_output("image", mode="work").allowed)
        self.assertTrue(guard.allow_media_output("image", mode="chat").allowed)
        # 全局默认没配 block_image_output。
        self.assertTrue(guard.allow_media_output("image").allowed)

    def test_override_block_reply_and_strip_images(self) -> None:
        guard = self.guard_with(
            chat=types.SimpleNamespace(
                block_reply="（chat 模式拦截）", strip_tool_images=True
            )
        )
        self.assertEqual(guard.block_reply_for("chat"), "（chat 模式拦截）")
        self.assertEqual(guard.block_reply_for("work"), guard.policy.block_reply_text())
        self.assertTrue(guard.should_strip_tool_images("chat"))
        self.assertFalse(guard.should_strip_tool_images("work"))

    def test_bound_guard_uses_mode(self) -> None:
        guard = self.guard_with(chat=types.SimpleNamespace(mode="off"))
        bound = guard.for_mode("chat")
        self.assertEqual(bound.mode_name, "chat")
        self.assertFalse(bound.enforcing)
        self.assertEqual(bound.filter_text(self.SECRET, stage="send")[0], self.SECRET)
        work_bound = bound.for_mode("work")
        self.assertTrue(work_bound.enforcing)
        self.assertNotIn(
            self.SECRET, work_bound.filter_text(self.SECRET, stage="send")[0]
        )

    # -- mode_from_host -----------------------------------------------------
    def _host(self, *, current=None, group_default="", default_mode="", raises=False):
        audit = self

        class _Sessions:
            def current(self, scope):
                if raises:
                    raise RuntimeError("no current session")
                return current

            def default_mode(self):
                return default_mode

        class _Host:
            sessions = _Sessions()

            def scope(self, ctx):
                return object()

            def group_default_session_mode(self, ctx):
                return group_default

        return _Host()

    def test_mode_from_host_prefers_current_session(self) -> None:
        host = self._host(current=types.SimpleNamespace(mode="chat"))
        self.assertEqual(mode_from_host(host, {}), "chat")

    def test_mode_from_host_falls_back_to_group_default(self) -> None:
        host = self._host(raises=True, group_default="chat")
        self.assertEqual(mode_from_host(host, {}), "chat")

    def test_mode_from_host_falls_back_to_service_default(self) -> None:
        host = self._host(raises=True, default_mode="chat")
        self.assertEqual(mode_from_host(host, {}), "chat")

    def test_mode_from_host_defaults_to_work(self) -> None:
        self.assertEqual(mode_from_host(None, {}), "work")
        self.assertEqual(mode_from_host(self._host(raises=True), {}), "work")

    def test_normalize_mode(self) -> None:
        guard = self.guard_with()
        self.assertEqual(guard.normalize_mode("CHAT"), "chat")
        self.assertEqual(guard.normalize_mode(" work "), "work")
        self.assertEqual(guard.normalize_mode("unknown"), "")
        self.assertEqual(guard.normalize_mode(None), "")

    # -- 配置解码 -----------------------------------------------------------
    def test_config_sections_decode_and_normalize(self) -> None:
        from atri_x.config.app_sections import SafetyConfig

        config = SafetyConfig.from_dict(
            {
                "enabled": False,
                "mode": "audit",
                "chat": {"enabled": True, "mode": "ENFORCE", "max_scan_chars": 0},
                "work": {"block_image_output": True},
            }
        )
        self.assertTrue(config.chat.enabled)
        self.assertEqual(config.chat.mode, "ENFORCE")
        self.assertIsNone(config.work.enabled)
        self.assertTrue(config.work.block_image_output)
        normalized = config.normalized()
        self.assertEqual(normalized.chat.mode, "enforce")
        self.assertIsNone(normalized.chat.max_scan_chars)
        self.assertIsNone(normalized.work.enabled)
        # to_dict / from_dict 往返幂等（空覆盖段也一样）。
        payload = normalized.to_dict()
        self.assertEqual(SafetyConfig.from_dict(payload).to_dict(), payload)


if __name__ == "__main__":
    unittest.main()
