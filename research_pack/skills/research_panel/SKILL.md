---
name: research_panel
description: 评审聚合：按五维评分（创新/严谨/清晰/重要/伦理）计算加权总分、分歧与决定，并对同模型评审显式降级声明；不传 reviews 时返回空白评审模板。
---

# research_panel

把多份评审意见变成可解释的结论。**决定由规则算出，不由模型口算。**

## 参数

| 参数 | 说明 |
| --- | --- |
| `run` | 运行 id（必填） |
| `reviews` | 评审数据对象；不传时返回空白模板并写入 `reviews.json` |

## 评审数据结构

```json
{"reviews": [{
  "reviewer": "R1",
  "independence": "same-model",
  "confidence": 3,
  "scores": {"originality": 4, "rigor": 3, "clarity": 4, "significance": 4, "ethics": 5},
  "comments": {"rigor": "缺少与基线方法的对照"},
  "required_fixes": ["补充对照实验"],
  "recommendation": "major_revision"
}]}
```

`independence` 只能填 `independent` 或 `same-model`。模型生成的评审一律填 `same-model`。

## 返回

- `weighted_total`（5 分制加权）、`decision`（accept/minor_revision/major_revision/reject）；
- `dimensions`：每维均值、极差、是否需要讨论（极差 ≥2）；
- `agreement.flag`：HIGH 表示评审分歧大；
- `degraded`：`NOT_INDEPENDENT`（同一模型）、`INSUFFICIENT_REVIEWERS`（<2 份）、
  `MISSING_DIMENSIONS`、`ACCEPT_DOWNGRADED`（不独立时 acceptance 被降级）。

## 使用规则

1. 先按 `academic_review` 技能产出结构化评审；
2. 再调本工具聚合；
3. 给用户结论时**必须**转述 `degraded`：
   "以上为同一模型生成的模拟评审，不构成独立同行评审，accept 已被降级为 minor_revision"。
4. 分歧维度（`needs_discussion`）要单列，说明双方理由，不要强行和稀泥。

## 注意

- 不传 `reviews` 时工具只写模板，不会报错：这是让用户填表后再聚合的正常路径；
- 阈值来自 `settings.json` 的 `panel.thresholds`，改阈值要在报告里说明；
- 不要用本工具给真实投稿生成"审稿意见"冒充他人评审。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`panel` · 技能：`research_panel`
- 风险：`low`（普通成员可调用）
- 超时：90 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `reviews` | 对象(JSON) | 否 | 评审数据：{reviews:[{reviewer,independence:'independent'\|'same-model',confidence,scores:{originality,rigor,clarity,significance,ethics},comments,required_fixes,recommendation}]}。 |

引擎强制执行的规则（不是建议）：

- 五维加权总分与接受阈值是固定算式，不是模型感觉。
- independence=same-model 的评审会被显式降级声明，不能冒充独立评审。
- 分歧超过阈值的维度会列进 needs_discussion。

<!-- hardtool-contract:end -->
