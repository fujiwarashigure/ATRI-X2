"""内容安全的公共值对象：处置档位、审核结果、阶段名与标签归一化。

1.1.8 起"档位"（tier）是一等公民：本地词表 / 远程审核都产出 ``block`` / ``review``
``/ ``pass`` 三档之一，由 :mod:`atri_x.safety.engine` 统一裁决。**不做什么**：
不读配置、不做判定、不写审计——只有常量与纯函数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "TIER_BLOCK",
    "TIER_PASS",
    "TIER_REVIEW",
    "TIERS",
    "STAGE_INBOUND",
    "STAGE_LLM",
    "STAGE_SEND",
    "STAGE_TOOL",
    "STAGE_IMAGE_PROMPT",
    "STAGE_IMAGE",
    "STAGE_AUDIO",
    "STAGE_VIDEO",
    "STAGES",
    "STAGE_POLICY_SYNC",
    "STAGE_POLICY_CACHE_ONLY",
    "STAGE_POLICY_LOCAL_ONLY",
    "STAGE_POLICY_OFF",
    "STAGE_POLICIES",
    "FAIL_OPEN",
    "FAIL_CLOSED",
    "LABEL_MAP",
    "Verdict",
    "normalize_label",
    "normalize_labels",
    "tier_of",
]


# -- 处置档位 ---------------------------------------------------------------
TIER_BLOCK = "block"
TIER_REVIEW = "review"
TIER_PASS = "pass"
TIERS: tuple[str, ...] = (TIER_BLOCK, TIER_REVIEW, TIER_PASS)

#: 档位"严重度"排序：数值越大越严重（用于多来源合并时取最严重）。
_TIER_RANK: dict[str, int] = {TIER_PASS: 0, TIER_REVIEW: 1, TIER_BLOCK: 2}

# -- 处理阶段 ---------------------------------------------------------------
STAGE_INBOUND = "inbound"
STAGE_LLM = "llm"
STAGE_SEND = "send"
STAGE_TOOL = "tool"
STAGE_IMAGE_PROMPT = "image_prompt"
STAGE_IMAGE = "image"
STAGE_AUDIO = "audio"
STAGE_VIDEO = "video"
STAGES: tuple[str, ...] = (
    STAGE_INBOUND,
    STAGE_LLM,
    STAGE_SEND,
    STAGE_TOOL,
    STAGE_IMAGE_PROMPT,
    STAGE_IMAGE,
    STAGE_AUDIO,
    STAGE_VIDEO,
)

# -- 每个阶段的远程策略 -----------------------------------------------------
#: 同步调远程审核（100~300 ms；受 ``[remote].timeout_ms`` 约束）。
STAGE_POLICY_SYNC = "sync"
#: 只在缓存命中时用远程结果，未命中就当 pass（零额外延迟，适合工具结果这种大段第三方文本）。
STAGE_POLICY_CACHE_ONLY = "cache_only"
#: 完全不调远程，只走本地规则层。
STAGE_POLICY_LOCAL_ONLY = "local_only"
#: 该阶段不审核（等同关闭）。
STAGE_POLICY_OFF = "off"
STAGE_POLICIES: tuple[str, ...] = (
    STAGE_POLICY_SYNC,
    STAGE_POLICY_CACHE_ONLY,
    STAGE_POLICY_LOCAL_ONLY,
    STAGE_POLICY_OFF,
)

# -- 失败策略 ---------------------------------------------------------------
FAIL_OPEN = "open"
FAIL_CLOSED = "closed"
FAIL_MODES: tuple[str, ...] = (FAIL_OPEN, FAIL_CLOSED)


# -- 标签归一化（两家供应商映射到同一组类别） --------------------------------
#: 归一化类别：``porn`` / ``politics`` / ``abuse`` / ``ad`` / ``terror`` / ``other``。
LABEL_MAP: dict[str, str] = {
    # 腾讯云天御
    "porn": "porn",
    "sexy": "porn",
    "abuse": "abuse",
    "illegal": "abuse",
    "ad": "ad",
    "politics": "politics",
    "terror": "terror",
    "terrorism": "terror",
    "polity": "politics",
    "custom": "other",
    # 阿里云内容安全
    "sexual": "porn",
    "sexual_content": "porn",
    "contraband": "abuse",
    "contraband_political": "politics",
    "political_entertainment": "politics",
    "abuse_content": "abuse",
    "ad_content": "ad",
    "terrorism_content": "terror",
    "violent": "abuse",
    "gore": "abuse",
}


def normalize_label(label: str) -> str:
    """把供应商的原始标签归一到本项目的一组类别（未知标签保留小写原值）。"""
    text = (label or "").strip().lower()
    if not text:
        return "other"
    return LABEL_MAP.get(text, text)


def normalize_labels(labels: object) -> tuple[str, ...]:
    """把标签序列归一成去重保序的元组；容忍 ``None`` / 字符串 / 任意可迭代。"""
    if labels is None:
        return ()
    if isinstance(labels, str):
        raw: object = (labels,)
    else:
        raw = labels
    out: list[str] = []
    try:
        iterator = iter(raw)  # type: ignore[arg-type]
    except TypeError:
        return ()
    for item in iterator:
        name = normalize_label(str(item or ""))
        if name and name not in out:
            out.append(name)
    return tuple(out)


def tier_of(suggestion: str) -> str:
    """供应商的 ``suggestion``（pass/review/block，大小写不敏感）→ 档位。"""
    text = (suggestion or "").strip().lower()
    return text if text in TIERS else TIER_PASS


def most_severe(tiers: object) -> str:
    """一组档位里最严重的那个（空集合 → pass）。"""
    worst = TIER_PASS
    try:
        iterator = iter(tiers)  # type: ignore[arg-type]
    except TypeError:
        return worst
    for item in iterator:
        name = str(item or "").strip().lower()
        if _TIER_RANK.get(name, -1) > _TIER_RANK[worst]:
            worst = name
    return worst


@dataclass(frozen=True)
class Verdict:
    """一次审核（本地或远程）的结果。

    ``degraded`` 表示"这不是一次干净的主供应商同步结果"：来自备供应商、缓存、
    或超时兜底。留痕时要能区分，否则事后无法判断某个 block 是否可信。
    """

    provider: str = "local"
    suggestion: str = TIER_PASS
    labels: tuple[str, ...] = ()
    score: float | None = None
    request_id: str = ""
    latency_ms: int = 0
    degraded: bool = False
    raw_code: str = ""
    #: 供排障的原始明细（**不含原文**，只有标签/分数/关键词长度这类）。
    detail: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def tier(self) -> str:
        return tier_of(self.suggestion)

    def to_record(self) -> dict[str, object]:
        """审计/队列用的扁平投影（不含原文）。"""
        return {
            "provider": self.provider,
            "suggestion": self.suggestion,
            "labels": list(self.labels),
            "score": self.score,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "degraded": self.degraded,
            "raw_code": self.raw_code,
        }
