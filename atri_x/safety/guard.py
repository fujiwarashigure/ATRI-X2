"""安全闸门：文本审核 / 脱敏、媒体出口判定与审计的唯一入口。

对应 1.0.2 外挂 PoC ``guard.mjs``，但这里是**原生 Python**、进程内同步调用，
不再有子进程冷启动（PoC 每次 exec 约 50~150 ms）。

三段式语义（与 ``[safety].mode`` 一致）：

* ``off``（含 ``enabled = false``）：所有方法都返回 ``pass``、不改文本、不写审计。
  **这是"不配置就完全无行为变化"的硬约束**，所有集成点只判断 ``guard is None``，
  模式判断全部收敛在本类里。
* ``audit``：照常判定（:meth:`SafetyGuard.evaluate_text` 能拿到"本会怎样"），
  但 :meth:`SafetyGuard.filter_text` 原样返回文本，只写审计。
* ``enforce``：``redact`` 替换敏感信息、``block`` 换成拦截文案、媒体出口丢弃。

两条额外约束：

1. **幂等**：对同一段文本重复 :meth:`filter_text` 不会继续啃掉文本尾巴
   （替换文本不含可再次命中的形态，见 :mod:`atri_x.safety.rules`）。
2. **审计不存原文**：只写 sha256 + 脱敏预览 + 标签 + 阶段 + 平台/scope/actor + 时间。
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from atri_x.safety.audit import AuditLog, utc_now
from atri_x.safety.policy import (
    ACTION_BLOCK,
    ACTION_FLAG,
    ACTION_PASS,
    ACTION_REDACT,
    SafetyMode,
    SafetyPolicy,
    merge_policy,
    policy_from_section,
)
from atri_x.safety.rules import RuleHit, RuleSet
from atri_x.safety.mode_ctx import (
    SESSION_MODES,
    ModeBoundGuard,
    SafetyScope,
    _coerce_scope,
)

__all__ = [
    "DEFAULT_BLOCK_REPLACEMENT",
    "MEDIA_BLOCK_TEXT",
    "SafetyDecision",
    "SafetyGuard",
    "TOOL_IMAGE_STRIPPED_TEXT",
]

#: ``enforce`` 下文本被整体拦截时代入的文案（入站走 ``policy.block_reply``）。
DEFAULT_BLOCK_REPLACEMENT = "（这条内容因内容安全策略被拦截，已替换。）"

#: 媒体出口被拦截时补的文本说明（**不做图片/文件内容识别**，只按策略在出口丢弃）。
MEDIA_BLOCK_TEXT: dict[str, str] = {
    "image": "（这张图片因内容安全策略被拦截，未发送。）",
    "file": "（这个文件因内容安全策略被拦截，未发送。）",
}

#: 工具结果里的图片段被剥离后代的占位文本。
TOOL_IMAGE_STRIPPED_TEXT = "[图片已被内容安全策略拦截]"

_STAT_KEYS = (
    "evaluated",
    "pass",
    "flag",
    "redact",
    "block",
    "media_blocked",
    "audit_written",
    "audit_errors",
)

@dataclass(frozen=True)
class SafetyDecision:
    """一次判定的结果。``text`` 是"按策略处理后"的文本（audit 模式下不会被采用）。"""

    allowed: bool = True
    text: str = ""
    action: str = ACTION_PASS
    labels: tuple[str, ...] = ()
    reason: str = ""


class SafetyGuard:
    """把一份 :class:`SafetyPolicy` 变成可调用的判定器（宿主挂在 ``agent.safety_guard``）。

    支持**按会话模式覆盖**：``chat`` / ``work`` 传进来的是 ``[safety.chat]`` /
    ``[safety.work]`` 覆盖段（鸭子类型对象或 ``Mapping``），内部用
    :func:`atri_x.safety.policy.merge_policy` 合并成每个模式一份策略。所有判定方法
    都接受 ``mode`` 参数；不传时用全局默认策略（行为与 1.0.3 完全一致）。
    """

    def __init__(
        self,
        policy: SafetyPolicy | Any,
        logger: Any = None,
        now: Callable[[], datetime] | None = None,
        config_dir: str = "",
        *,
        chat: Any = None,
        work: Any = None,
    ) -> None:
        self.policy: SafetyPolicy = (
            policy if isinstance(policy, SafetyPolicy) else policy_from_section(policy)
        )
        self.logger = logger
        self.config_dir = config_dir or ""
        self._now = now if callable(now) else utc_now
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {key: 0 for key in _STAT_KEYS}
        self.rules = RuleSet(self.policy)
        self.audit = AuditLog(
            self.policy.resolved_audit_path(self.config_dir),
            preview_chars=self.policy.audit_preview_chars,
            logger=logger,
            now=self._now,
        )
        # 每个会话模式一份策略："" = 全局默认；没有覆盖段的模式复用全局那份。
        self._variants: dict[str, tuple[SafetyPolicy, RuleSet]] = {
            "": (self.policy, self.rules)
        }
        for name, section in (("chat", chat), ("work", work)):
            if section is None:
                continue
            merged = (
                section
                if isinstance(section, SafetyPolicy)
                else merge_policy(self.policy, section)
            )
            if merged == self.policy:
                self._variants[name] = (self.policy, self.rules)
            else:
                self._variants[name] = (merged, RuleSet(merged))
        for warning in self.rules.warnings:
            self._warn(warning)
        for name, (_variant_policy, rules) in self._variants.items():
            if rules is self.rules:
                continue
            for warning in rules.warnings:
                self._warn(f"[{name}] {warning}")

    # -- 模式 ---------------------------------------------------------------
    @staticmethod
    def normalize_mode(mode: Any) -> str:
        """把任意 ``mode`` 归一成 ``""`` / ``"chat"`` / ``"work"``。"""
        text = str(mode or "").strip().lower()
        return text if text in SESSION_MODES else ""

    def _variant(self, mode: Any = None) -> tuple[SafetyPolicy, RuleSet]:
        key = self.normalize_mode(mode)
        return self._variants.get(key) or self._variants[""]

    def policy_for(self, mode: Any = None) -> SafetyPolicy:
        """某个会话模式生效的策略（``mode = None`` 时是全局默认）。"""
        return self._variant(mode)[0]

    def rules_for(self, mode: Any = None) -> RuleSet:
        """某个会话模式生效的规则集（没有覆盖时就是全局那份）。"""
        return self._variant(mode)[1]

    def for_mode(self, mode: Any) -> "ModeBoundGuard":
        """返回绑定到某个会话模式的视图（工具 ctx 里存的就是它）。"""
        return ModeBoundGuard(self, mode)

    # -- 派生属性（全局默认；按模式请用 ``*_for(mode)``） --------------------
    @property
    def mode(self) -> str:
        """生效模式（``enabled = false`` 时为 ``off``）。"""
        return self.policy.effective_mode()

    @property
    def active(self) -> bool:
        return self.policy.active

    @property
    def enforcing(self) -> bool:
        return self.policy.enforcing

    @property
    def block_reply(self) -> str:
        """入站拦截时回复用户的文本。"""
        return self.policy.block_reply_text()

    def mode_for(self, mode: Any = None) -> str:
        """某个会话模式的生效模式。"""
        return self.policy_for(mode).effective_mode()

    def active_for(self, mode: Any = None) -> bool:
        """某个会话模式下策略是否生效。"""
        return self.policy_for(mode).active

    def enforcing_for(self, mode: Any = None) -> bool:
        """某个会话模式是否处于 ``enforce``。"""
        return self.policy_for(mode).enforcing

    def block_reply_for(self, mode: Any = None) -> str:
        """某个会话模式的入站拦截文案。"""
        return self.policy_for(mode).block_reply_text()

    def should_strip_tool_images(self, mode: Any = None) -> bool:
        """是否剥离工具结果里的图片段（只有该模式的 ``enforce`` 才改行为）。"""
        policy = self.policy_for(mode)
        return bool(policy.strip_tool_images) and policy.enforcing

    # -- 文本 ---------------------------------------------------------------
    def evaluate_text(
        self,
        text: str,
        *,
        stage: str,
        scope: Any = None,
        block_text: str = "",
        mode: Any = None,
    ) -> SafetyDecision:
        """只判定、不改文本、**不写审计**。

        用于"想知道本会怎样"的场景（灰度前观察、测试）。审计由
        :meth:`filter_text` / :meth:`allow_media_output` 负责，避免同一条消息写两遍。
        ``mode`` 为 ``chat`` / ``work`` 时用该模式的覆盖策略。
        """
        original = text or ""
        policy, rules = self._variant(mode)
        if not policy.active:
            return SafetyDecision(allowed=True, text=original, action=ACTION_PASS)
        inspected, hits = rules.inspect(original)
        return self._decide(original, inspected, hits, block_text=block_text)

    def filter_text(
        self,
        text: str,
        *,
        stage: str,
        scope: Any = None,
        block_text: str = "",
        meta: Mapping[str, Any] | None = None,
        mode: Any = None,
    ) -> tuple[str, SafetyDecision]:
        """返回 ``(实际采用的文本, 判定)``。

        ``enforce`` 下 ``redact`` 采用脱敏文本、``block`` 采用 ``block_text``
        （缺省 :data:`DEFAULT_BLOCK_REPLACEMENT`）；``audit`` / ``off`` 下原样返回。
        ``mode`` 为 ``chat`` / ``work`` 时用该模式的覆盖策略。
        """
        original = text or ""
        policy, rules = self._variant(mode)
        if not policy.active:
            return original, SafetyDecision(allowed=True, text=original, action=ACTION_PASS)
        inspected, hits = rules.inspect(original)
        decision = self._decide(original, inspected, hits, block_text=block_text)
        self._record(decision, original, stage=stage, scope=scope, meta=meta, policy=policy)
        self._tally(decision.action)
        if policy.enforcing and decision.action in (ACTION_BLOCK, ACTION_REDACT):
            return decision.text, decision
        return original, decision

    # -- 媒体出口 -----------------------------------------------------------
    def allow_media_output(
        self,
        kind: str,
        *,
        stage: str = "send",
        scope: Any = None,
        meta: Mapping[str, Any] | None = None,
        mode: Any = None,
    ) -> SafetyDecision:
        """图片 / 文件出口判定。

        ``kind`` 为 ``"image"`` / ``"file"``。**不做内容识别**：只看
        ``block_image_output`` / ``block_file_output``，命中就在发送边界丢弃该输出。
        ``enforce`` 下返回 ``allowed = False``；``audit`` 下返回 ``allowed = True``
        但 ``action = flag``（调用方据此知道"本会被拦"）。``mode`` 为 ``chat`` /
        ``work`` 时用该模式的覆盖策略。
        """
        name = (kind or "").strip().lower()
        if name not in ("image", "file"):
            return SafetyDecision(allowed=True, text="", action=ACTION_PASS)
        policy = self.policy_for(mode)
        if not policy.active:
            return SafetyDecision(allowed=True, text="", action=ACTION_PASS)
        configured = (
            policy.block_image_output if name == "image" else policy.block_file_output
        )
        if not configured:
            return SafetyDecision(allowed=True, text="", action=ACTION_PASS)
        labels = ("media." + name,)
        note = MEDIA_BLOCK_TEXT[name]
        if policy.enforcing:
            decision = SafetyDecision(
                allowed=False,
                text=note,
                action=ACTION_BLOCK,
                labels=labels,
                reason=name + " 输出被内容安全策略拦截",
            )
        else:
            decision = SafetyDecision(
                allowed=True,
                text=note,
                action=ACTION_FLAG,
                labels=labels,
                reason=name + " 输出会被内容安全策略拦截（audit：只记录）",
            )
        payload = {"kind": name}
        for key, value in (meta or {}).items():
            if value not in (None, ""):
                payload[str(key)] = str(value)
        self._record(decision, "", stage=stage, scope=scope, meta=payload, policy=policy)
        self._tally(decision.action, media=True)
        return decision

    # -- 统计 ---------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        """累计计数（进程内、加锁读取）。"""
        with self._lock:
            out = dict(self._counts)
        out["audit_written"] = self.audit.written
        out["audit_errors"] = self.audit.write_errors
        return out

    # -- 内部 ---------------------------------------------------------------
    def _decide(
        self,
        original: str,
        inspected: str,
        hits: list[RuleHit],
        *,
        block_text: str = "",
    ) -> SafetyDecision:
        actions = {hit.action for hit in hits}
        if ACTION_BLOCK in actions:
            action = ACTION_BLOCK
        elif ACTION_REDACT in actions:
            action = ACTION_REDACT
        elif ACTION_FLAG in actions:
            action = ACTION_FLAG
        else:
            action = ACTION_PASS
        labels: list[str] = []
        for hit in hits:
            if hit.category not in labels:
                labels.append(hit.category)
        if action == ACTION_BLOCK:
            text = (block_text or "").strip() or DEFAULT_BLOCK_REPLACEMENT
        elif action == ACTION_REDACT:
            text = inspected
        else:
            text = original
        reason = ""
        if hits:
            reason = "命中：" + "、".join(
                f"{hit.label}({hit.category})" for hit in hits[:4]
            )
        return SafetyDecision(
            allowed=action != ACTION_BLOCK,
            text=text,
            action=action,
            labels=tuple(labels),
            reason=reason,
        )

    def _record(
        self,
        decision: SafetyDecision,
        original: str,
        *,
        stage: str,
        scope: Any,
        meta: Mapping[str, Any] | None = None,
        policy: SafetyPolicy | None = None,
    ) -> None:
        """写一条审计（``pass`` 不记；**只写指纹与脱敏预览**）。"""
        if decision.action == ACTION_PASS:
            return
        effective = policy or self.policy
        info = _coerce_scope(scope)
        preview = self.rules.mask(decision.text or "")[: effective.audit_preview_chars]
        record: dict[str, Any] = {
            "stage": stage or "",
            "action": decision.action,
            "mode": effective.effective_mode(),
            "labels": list(decision.labels),
            "reason": decision.reason,
            "chars": len(original),
            "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest() if original else "",
            "preview": preview,
            "platform": info.platform,
            "scope": info.scope_id,
            "actor": info.actor,
        }
        if meta:
            record["meta"] = {str(k): str(v) for k, v in meta.items() if v not in (None, "")}
        self.audit.append(record)

    def _tally(self, action: str, *, media: bool = False) -> None:
        key = {
            ACTION_BLOCK: "block",
            ACTION_REDACT: "redact",
            ACTION_FLAG: "flag",
        }.get(action, "pass")
        with self._lock:
            self._counts["evaluated"] += 1
            self._counts[key] = self._counts.get(key, 0) + 1
            if media and action == ACTION_BLOCK:
                self._counts["media_blocked"] += 1

    def _warn(self, message: str) -> None:
        logger = self.logger
        if logger is None:
            return
        with contextlib.suppress(Exception):
            warn = getattr(logger, "warning", None) or getattr(logger, "warn", None)
            if callable(warn):
                warn(message)
