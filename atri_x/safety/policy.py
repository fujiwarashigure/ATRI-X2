"""安全策略：``[safety]`` 段 → :class:`SafetyPolicy`，以及内置类别的默认值。

对应 1.0.2 的外挂 PoC ``ATRI-X-safety-gate/plugin/safety-gate/policy.json``：
本模块是那份 JSON 的 Python 原生替代品，但**不读文件、不 import 配置模块**——
宿主（``atri_x.config.app_sections.SafetyConfig``）把 TOML 段解成一个鸭子类型的对象，
:func:`policy_from_section` 用 ``getattr`` / ``Mapping`` 取值，因此 ``atri_x.safety``
与 ``atri_x.config`` 之间没有 import 环。

设计要点（与 PoC 一致，见 ``docs/safety.md``）：

* ``mode = off | audit | enforce``。``enabled = false`` 等价于 ``off``：
  不配置就完全不改变 1.0.2 的任何行为。
* 词表类（道德 / 合规）命中即 ``block``；敏感信息按类别配 ``flag``（只记审计）
  或 ``redact``（替换），默认值见 :data:`SENSITIVE_DEFAULT_ACTIONS`。
* 时间一律带时区（审计记录用 UTC）。
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field, fields
from typing import Any, Iterable, Mapping

from atri_x.safety.rules import CATEGORY_IDS, SENSITIVE_DEFAULT_ACTIONS, SENSITIVE_IDS

__all__ = [
    "ACTION_BLOCK",
    "ACTION_FLAG",
    "ACTION_PASS",
    "ACTION_REDACT",
    "DEFAULT_AUDIT_PREVIEW_CHARS",
    "DEFAULT_AUDIT_FILE_NAME",
    "DEFAULT_BLOCK_REPLY",
    "DEFAULT_MAX_SCAN_CHARS",
    "SENSITIVE_DEFAULT_ACTIONS",
    "SafetyMode",
    "SafetyPolicy",
    "OVERRIDE_FIELDS",
    "merge_policy",
    "policies_from_section",
    "policy_from_section",
]


class SafetyMode:
    """过滤模式。``off`` 关闭、``audit`` 只记审计、``enforce`` 真正拦 / 替换。"""

    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"
    ALL = (OFF, AUDIT, ENFORCE)


# 决策动作（``SafetyDecision.action`` 的取值）。
ACTION_PASS = "pass"
ACTION_FLAG = "flag"
ACTION_REDACT = "redact"
ACTION_BLOCK = "block"

#: 入站消息被拦截时回复用户的默认文本（可由 ``[safety].block_reply`` 覆盖）。
DEFAULT_BLOCK_REPLY = "你的消息包含违反内容安全策略的内容，已被拦截。请调整表述后重试。"

#: 审计文件名；``[safety].audit_path`` 为空时落在 ``<配置目录>/`` 下。
DEFAULT_AUDIT_FILE_NAME = "safety-audit.jsonl"

DEFAULT_AUDIT_PREVIEW_CHARS = 120
DEFAULT_MAX_SCAN_CHARS = 8000
DEFAULT_CATEGORIES: tuple[str, ...] = CATEGORY_IDS
DEFAULT_SENSITIVE: tuple[str, ...] = SENSITIVE_IDS


def _field(section: Any, name: str, default: Any) -> Any:
    """鸭子类型取字段：先 ``Mapping`` 再 ``getattr``。"""
    if section is None:
        return default
    if isinstance(section, Mapping):
        return section.get(name, default)
    return getattr(section, name, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_names(value: Any) -> tuple[str, ...]:
    """把配置里的名单归一成小写、去重、保序的元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        return ()
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _as_actions(value: Any) -> dict[str, str]:
    """``category -> flag|redact``；非法动作直接丢（保持内置默认）。"""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key or "").strip().lower()
        action = str(raw or "").strip().lower()
        if name and action in (ACTION_FLAG, ACTION_REDACT, ACTION_BLOCK):
            out[name] = action
    return out


def _as_strings(value: Any) -> tuple[str, ...]:
    """原样（不做小写）的字符串名单；``allowlist`` 用，因为它要按原文匹配。"""
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        return ()
    out: list[str] = []
    for item in raw:
        text = str(item if item is not None else "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class SafetyPolicy:
    """一份不可变的安全策略。

    字段与 ``[safety]`` 的 TOML 键一一对应（见 ``docs/safety.md``）。
    ``categories`` / ``sensitive`` 为空表示"全部内置类别"。
    """

    #: 总开关。``False`` 时一切判定结果为 ``pass``，且不写审计。
    enabled: bool = False
    #: ``off`` | ``audit`` | ``enforce``。非法值按 ``audit`` 处理。
    mode: str = SafetyMode.AUDIT
    #: 启用的词表类别 id（空 = 全部）。见 :data:`atri_x.safety.rules.CATEGORY_IDS`。
    categories: tuple[str, ...] = ()
    #: 启用的敏感信息类别 id（空 = 全部）。
    sensitive: tuple[str, ...] = ()
    #: 单个敏感类别 -> ``flag`` / ``redact``；缺省用 :data:`SENSITIVE_DEFAULT_ACTIONS`。
    sensitive_actions: Mapping[str, str] = field(default_factory=dict)
    #: 命中豁免：命中处的**子串**若包含其中任一条，就放过这一处命中。
    allowlist: tuple[str, ...] = ()
    #: 入站拦截时回复用户的文本（空 = :data:`DEFAULT_BLOCK_REPLY`）。
    block_reply: str = ""
    #: 审计日志路径（空 = ``<配置目录>/safety-audit.jsonl``）。
    audit_path: str = ""
    #: 审计预览保留的字符数。
    audit_preview_chars: int = DEFAULT_AUDIT_PREVIEW_CHARS
    #: true：工具结果里的图片段被替换成文字（模型与后续上下文看不到图）。
    strip_tool_images: bool = False
    #: true：出站图片被丢弃并补一条文本说明。
    block_image_output: bool = False
    #: true：出站文件被丢弃并补一条文本说明。
    block_file_output: bool = False
    #: 单次扫描的最大字符数（超出部分不扫描、原样保留）。
    max_scan_chars: int = DEFAULT_MAX_SCAN_CHARS

    # -- 派生视图 -----------------------------------------------------------
    def effective_mode(self) -> str:
        """``enabled = false`` 时等价于 ``off``。"""
        if not self.enabled:
            return SafetyMode.OFF
        return self.mode if self.mode in SafetyMode.ALL else SafetyMode.AUDIT

    @property
    def active(self) -> bool:
        """策略是否生效（``off`` 不算生效）。"""
        return self.effective_mode() != SafetyMode.OFF

    @property
    def enforcing(self) -> bool:
        """是否处于 ``enforce``：只有这个模式才拦 / 替换。"""
        return self.effective_mode() == SafetyMode.ENFORCE

    def enabled_categories(self) -> tuple[str, ...]:
        """实际启用的词表类别（空配置 = 全部内置类别，保持固定顺序）。"""
        if not self.categories:
            return DEFAULT_CATEGORIES
        return tuple(name for name in CATEGORY_IDS if name in self.categories)

    def enabled_sensitive(self) -> tuple[str, ...]:
        """实际启用的敏感信息类别（空配置 = 全部内置类别）。"""
        if not self.sensitive:
            return DEFAULT_SENSITIVE
        return tuple(name for name in SENSITIVE_IDS if name in self.sensitive)

    def action_for(self, category_id: str) -> str:
        """某个敏感信息类别最终生效的动作。"""
        name = (category_id or "").strip().lower()
        override = self.sensitive_actions.get(name, "")
        if override in (ACTION_FLAG, ACTION_REDACT, ACTION_BLOCK):
            return override
        return SENSITIVE_DEFAULT_ACTIONS.get(name, ACTION_REDACT)

    def block_reply_text(self) -> str:
        """入站拦截文案（空配置回落到内置文案）。"""
        return self.block_reply or DEFAULT_BLOCK_REPLY

    def resolved_audit_path(self, config_dir: str = "") -> str:
        """审计日志的最终路径：显式配置 > 配置目录 > 当前目录。"""
        configured = (self.audit_path or "").strip()
        if configured:
            return configured
        base = (config_dir or "").strip()
        if not base:
            return DEFAULT_AUDIT_FILE_NAME
        return os.path.join(base, DEFAULT_AUDIT_FILE_NAME)

    def to_dict(self) -> dict[str, Any]:
        """扁平投影（便于日志 / 测试断言）。"""
        return {
            "enabled": self.enabled,
            "mode": self.effective_mode(),
            "categories": list(self.categories),
            "sensitive": list(self.sensitive),
            "sensitive_actions": dict(self.sensitive_actions),
            "allowlist": list(self.allowlist),
            "block_reply": self.block_reply,
            "audit_path": self.audit_path,
            "audit_preview_chars": self.audit_preview_chars,
            "strip_tool_images": self.strip_tool_images,
            "block_image_output": self.block_image_output,
            "block_file_output": self.block_file_output,
            "max_scan_chars": self.max_scan_chars,
        }

    def replace(self, **changes: Any) -> "SafetyPolicy":
        """不可变替换（测试与宿主小改动用）；未知字段忽略。"""
        known = {item.name for item in fields(type(self))}
        payload = {name: value for name, value in changes.items() if name in known}
        return dataclasses.replace(self, **payload)


#: 按会话模式覆盖（``[safety.chat]`` / ``[safety.work]``）时允许覆盖的字段。
#: 审计文件与预览长度**全局唯一**，所以不在覆盖范围内。
OVERRIDE_FIELDS: tuple[str, ...] = (
    "enabled",
    "mode",
    "categories",
    "sensitive",
    "sensitive_actions",
    "allowlist",
    "block_reply",
    "strip_tool_images",
    "block_image_output",
    "block_file_output",
    "max_scan_chars",
)


def merge_policy(base: SafetyPolicy, override: Any) -> SafetyPolicy:
    """把 ``[safety.chat]`` / ``[safety.work]`` 里**显式写了**的字段盖到 ``base`` 上。

    ``override`` 可以是 ``SafetyOverrideConfig``、``Mapping`` 或普通对象；``None``、
    空段、以及值为 ``None`` 的字段都表示"继承 base"。这正是"不配置就完全无行为变化"
    的保证：没有覆盖段时返回的就是 base 本身（值相等）。
    """
    if override is None:
        return base
    changes: dict[str, Any] = {}
    raw = _field(override, "enabled", None)
    if raw is not None:
        changes["enabled"] = _as_bool(raw, base.enabled)
    raw = _field(override, "mode", None)
    if raw is not None:
        mode = _as_text(raw, "").lower()
        changes["mode"] = mode if mode in SafetyMode.ALL else SafetyMode.AUDIT
    for name in ("categories", "sensitive"):
        raw = _field(override, name, None)
        if raw is not None:
            changes[name] = _as_names(raw)
    raw = _field(override, "sensitive_actions", None)
    if raw is not None:
        merged_actions = dict(base.sensitive_actions)
        merged_actions.update(_as_actions(raw))
        changes["sensitive_actions"] = merged_actions
    raw = _field(override, "allowlist", None)
    if raw is not None:
        changes["allowlist"] = _as_strings(raw)
    raw = _field(override, "block_reply", None)
    if raw is not None:
        # 空串 = 继承（GUI 的「留空 = 继承全局」）；要用内置文案就在全局段留空。
        text = _as_text(raw, "")
        if text:
            changes["block_reply"] = text
    for name in ("strip_tool_images", "block_image_output", "block_file_output"):
        raw = _field(override, name, None)
        if raw is not None:
            changes[name] = _as_bool(raw, getattr(base, name))
    raw = _field(override, "max_scan_chars", None)
    if raw is not None:
        parsed = _as_int(raw, base.max_scan_chars)
        if parsed > 0:
            changes["max_scan_chars"] = parsed
    return base.replace(**changes) if changes else base


def policies_from_section(section: Any) -> dict[str, SafetyPolicy]:
    """``[safety]`` 段 → ``{"": 全局默认, "chat": chat 覆盖, "work": work 覆盖}``。

    ``""`` 键的这份就是 :func:`policy_from_section` 的结果；没有覆盖段的模式直接
    复用同一个对象（值相等），因此不配置时行为完全不变。
    """
    base = policy_from_section(section)
    return {
        "": base,
        "chat": merge_policy(base, _field(section, "chat", None)),
        "work": merge_policy(base, _field(section, "work", None)),
    }


def policy_from_section(section: Any) -> SafetyPolicy:
    """把 ``[safety]`` 段（任意鸭子类型对象）归一成 :class:`SafetyPolicy`。

    ``section`` 可以是 ``atri_x.config.app_sections.SafetyConfig``、普通对象或
    ``Mapping``；缺失字段一律取默认值，非法枚举回落到 ``audit``。
    """
    mode = _as_text(_field(section, "mode", SafetyMode.AUDIT)).lower()
    if mode not in SafetyMode.ALL:
        mode = SafetyMode.AUDIT
    return SafetyPolicy(
        enabled=_as_bool(_field(section, "enabled", False), False),
        mode=mode,
        categories=_as_names(_field(section, "categories", None)),
        sensitive=_as_names(_field(section, "sensitive", None)),
        sensitive_actions=_as_actions(_field(section, "sensitive_actions", None)),
        allowlist=_as_strings(_field(section, "allowlist", None)),
        block_reply=_as_text(_field(section, "block_reply", "")),
        audit_path=_as_text(_field(section, "audit_path", "")),
        audit_preview_chars=_as_int(
            _field(section, "audit_preview_chars", DEFAULT_AUDIT_PREVIEW_CHARS),
            DEFAULT_AUDIT_PREVIEW_CHARS,
        ),
        strip_tool_images=_as_bool(_field(section, "strip_tool_images", False), False),
        block_image_output=_as_bool(_field(section, "block_image_output", False), False),
        block_file_output=_as_bool(_field(section, "block_file_output", False), False),
        max_scan_chars=_as_int(
            _field(section, "max_scan_chars", DEFAULT_MAX_SCAN_CHARS), DEFAULT_MAX_SCAN_CHARS
        ),
    )
