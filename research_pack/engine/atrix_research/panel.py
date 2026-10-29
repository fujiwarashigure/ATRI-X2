"""评审意见聚合：把多位评审的分数变成可解释的结论。

只做确定性的算术与规则判定，不做"模型替评审打分"：

* 逐维度均值/极差，暴露分歧最大的维度；
* 按权重算总分，按阈值给决定；
* 独立性标注：如果所有评审都声明 ``same-model``，结论降级并显式声明——
  同一模型的多份意见不构成独立评审。
"""

from __future__ import annotations

import statistics
from typing import Any

__all__ = ["DEFAULT_RUBRIC", "aggregate", "panel_markdown", "review_template"]

DEFAULT_RUBRIC: dict[str, Any] = {
    "schema": "atrix-research/rubric/1",
    "scale": 5,
    "dimensions": {
        "originality": {"label": "创新性", "weight": 0.22},
        "rigor": {"label": "严谨性", "weight": 0.26},
        "clarity": {"label": "清晰度", "weight": 0.16},
        "significance": {"label": "重要性", "weight": 0.22},
        "ethics": {"label": "伦理与合规", "weight": 0.14},
    },
    "thresholds": {
        # 加权总分（5 分制）→ 决定
        "accept": 4.20,
        "minor_revision": 3.50,
        "major_revision": 2.60,
        # 低于最低阈值为 reject
    },
    "disagreement_warn": 1.0,
}


def review_template(scale: int = 5) -> dict[str, Any]:
    """给写作/评审 skill 的空白评审模板，模型照着填。"""
    return {
        "schema": "atrix-research/reviews/1",
        "scale": scale,
        "reviews": [
            {
                "reviewer": "R1",
                "independence": "independent",
                "confidence": 4,
                "scores": {dim: 4 for dim in DEFAULT_RUBRIC["dimensions"]},
                "comments": {dim: "" for dim in DEFAULT_RUBRIC["dimensions"]},
                "strengths": [],
                "weaknesses": [],
                "required_fixes": [],
                "recommendation": "minor_revision",
            }
        ],
        "notes": "independence 取 independent | same-model；same-model 时引擎会把结论标为降级。",
    }


def _normalize_reviews(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        reviews = payload.get("reviews")
    else:
        reviews = payload
    if not isinstance(reviews, list):
        return []
    out = []
    for index, raw in enumerate(reviews):
        if not isinstance(raw, dict):
            continue
        scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
        cleaned: dict[str, float] = {}
        for dimension, value in scores.items():
            try:
                cleaned[str(dimension)] = float(value)
            except (TypeError, ValueError):
                continue
        if not cleaned:
            continue
        out.append(
            {
                "reviewer": str(raw.get("reviewer") or f"R{index + 1}"),
                "independence": str(raw.get("independence") or "unknown"),
                "confidence": raw.get("confidence"),
                "scores": cleaned,
                "comments": raw.get("comments") if isinstance(raw.get("comments"), dict) else {},
                "required_fixes": [str(item) for item in raw.get("required_fixes") or []],
                "recommendation": str(raw.get("recommendation") or ""),
            }
        )
    return out


def aggregate(payload: Any, *, rubric: dict[str, Any] | None = None) -> dict[str, Any]:
    """聚合成 :class:`dict`：逐维统计、加权总分、决定、分歧与降级声明。"""
    rubric = rubric or DEFAULT_RUBRIC
    scale = float(rubric.get("scale") or 5)
    dims: dict[str, Any] = rubric.get("dimensions") or DEFAULT_RUBRIC["dimensions"]
    thresholds = rubric.get("thresholds") or DEFAULT_RUBRIC["thresholds"]
    disagreement_warn = float(rubric.get("disagreement_warn", 1.0))

    reviews = _normalize_reviews(payload)
    degraded: list[str] = []
    if len(reviews) < 2:
        degraded.append("INSUFFICIENT_REVIEWERS")
    independence = {review["independence"] for review in reviews}
    if reviews and independence <= {"same-model", "unknown"}:
        degraded.append("NOT_INDEPENDENT")

    per_dim: dict[str, Any] = {}
    weighted_total = 0.0
    missing_dims: list[str] = []
    for dimension, meta in dims.items():
        weight = float(meta.get("weight") or 0)
        values = [review["scores"][dimension] for review in reviews if dimension in review["scores"]]
        if not values:
            missing_dims.append(dimension)
            per_dim[dimension] = {"label": meta.get("label", dimension), "weight": weight, "n": 0}
            continue
        mean = sum(values) / len(values)
        spread = max(values) - min(values)
        per_dim[dimension] = {
            "label": meta.get("label", dimension),
            "weight": weight,
            "n": len(values),
            "mean": round(mean, 3),
            "min": min(values),
            "max": max(values),
            "spread": round(spread, 3),
            "stdev": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0,
            "needs_discussion": spread >= 2,
        }
        weighted_total += mean * weight
    if missing_dims:
        degraded.append("MISSING_DIMENSIONS")

    ordinary = sum(
        1 for review in reviews if review["independence"] == "independent"
    )
    decision = "reject"
    if weighted_total >= float(thresholds.get("accept", 4.2)):
        decision = "accept"
    elif weighted_total >= float(thresholds.get("minor_revision", 3.5)):
        decision = "minor_revision"
    elif weighted_total >= float(thresholds.get("major_revision", 2.6)):
        decision = "major_revision"
    if degraded and decision in ("accept",):
        decision = "minor_revision"
        degraded.append("ACCEPT_DOWNGRADED")

    required_fixes: list[str] = []
    for review in reviews:
        for fix in review["required_fixes"]:
            if fix not in required_fixes:
                required_fixes.append(fix)

    return {
        "schema": "atrix-research/panel/1",
        "scale": scale,
        "reviewers": len(reviews),
        "independent_reviewers": ordinary,
        "degraded": degraded,
        "weighted_total": round(weighted_total, 3),
        "decision": decision,
        "thresholds": thresholds,
        "dimensions": per_dim,
        "needs_discussion": [
            name for name, item in per_dim.items() if item.get("needs_discussion")
        ],
        "agreement": {
            "mean_spread": round(
                sum(item.get("spread", 0) for item in per_dim.values()) / max(1, len(per_dim)), 3
            ),
            "disagreement_warn": disagreement_warn,
            "flag": "HIGH" if any(item.get("spread", 0) >= disagreement_warn for item in per_dim.values()) else "OK",
        },
        "required_fixes": required_fixes,
        "recommendations": {
            review["reviewer"]: review["recommendation"] for review in reviews if review["recommendation"]
        },
    }


def panel_markdown(panel: dict[str, Any]) -> str:
    """评审聚合结果的人类可读版。"""
    labels = {
        "accept": "接受",
        "minor_revision": "小修",
        "major_revision": "大修",
        "reject": "拒稿",
    }
    lines = [
        "## 评审聚合",
        "",
        f"- 决定：**{labels.get(panel.get('decision'), panel.get('decision'))}**"
        f"（加权总分 {panel.get('weighted_total')}/{panel.get('scale')}）",
        f"- 评审人数：{panel.get('reviewers')}（独立：{panel.get('independent_reviewers')}）",
        f"- 一致度：{panel.get('agreement', {}).get('flag')}",
    ]
    if panel.get("degraded"):
        lines.append(f"- 降级声明：{', '.join(panel['degraded'])}")
        if "NOT_INDEPENDENT" in panel["degraded"]:
            lines.append(
                "  - 所有评审意见由同一模型在同一上下文中生成，不构成独立同行评审；"
                "该结论只能用于自查，不能作为真实评审意见。"
            )
    lines += ["", "| 维度 | 权重 | 均值 | 极差 | 需讨论 |", "| --- | --- | --- | --- | --- |"]
    for name, item in (panel.get("dimensions") or {}).items():
        lines.append(
            f"| {item.get('label', name)} | {item.get('weight')} | {item.get('mean', '-')} | "
            f"{item.get('spread', '-')} | {'是' if item.get('needs_discussion') else '否'} |"
        )
    if panel.get("required_fixes"):
        lines += ["", "### 必须修改", ""]
        for index, fix in enumerate(panel["required_fixes"], start=1):
            lines.append(f"{index}. {fix}")
    return "\n".join(lines).rstrip() + "\n"
