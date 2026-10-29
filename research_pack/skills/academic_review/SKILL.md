---
name: academic_review
description: 科研评审协议：先取 research_audit 的确定性分数与缺陷清单，再补不可度量的原创性/伦理判断，分数不靠模型自评。
---

# 科研评审协议

评审分两半：**可度量的六维**由 `research_audit` 用固定公式算（证据覆盖、引文可靠性、
方法可复现性、结构完整度、表述克制、交付完整度）；**不可度量的**（原创性、伦理）
只能由你判断，并且必须标注成人工判断。这样换模型时，评审结论的硬指标部分完全一致。

## 1. 先跑确定性评审

`research_audit`（run=<id>）返回：

- `weighted_total`：0-100；85+ accept，70-85 minor_revision，55-70 major_revision，<55 reject；
- `dimensions`：六维得分与算法出处；
- `unmeasured`：不可度量的维度（通常是 originality / ethics）；
- `blocks`：必须处理的缺陷，每条带 `fix`。

**六维分数直接引用，不要重算、不要改判。**

## 2. 再补主观评审

`research_panel` 接收评审列表（不传则返回模板）：

```json
{"reviews": [
  {"reviewer": "方法视角",
   "independence": "same-model",
   "confidence": 3,
   "scores": {"originality": 3, "rigor": 4, "clarity": 4, "significance": 3, "ethics": 5},
   "comments": "……",
   "required_fixes": ["……"],
   "recommendation": "minor_revision"}
]}
```

- `independence` 如实填：同一模型产出的多份意见**必须**写 `same-model`，
  引擎会在结果里降级声明，不能当独立评审对外呈现；
- `scores` 只填你真正判断得动的维度；其余以 `audit.dimensions` 为准；
- `required_fixes` 必须可执行（"第 3 节缺 2 处引用"而不是"加强论证"）。

## 3. 评审报告的写法

1. 结论先行：`decision` + `weighted_total` + 一句理由；
2. 硬指标表：六维分数与出处（来自 `audit.dimensions`）；
3. 必须处理的缺陷：逐条 `blocks[].code + message + fix`；
4. 主观判断：原创性/伦理，明确写"以下为模型判断，非引擎结论"；
5. 给作者的最小修改路径：按影响从大到小列 3-5 条。

## 4. 诚实与边界

- 不得把 `same-model` 的多份评审说成"多位评审一致"；
- 不得用"结构完整、证据充分"这类空话替代 `dimensions` 里的具体分数；
- `audit.gate=FAIL` 时不要给 accept 结论；
- 核验率低于闸门、导出缺格式等降级项要在评审里单列。

## 5. 禁止事项

- 禁止替 `research_audit` 重新打分或调整权重；
- 禁止编造评审人身份/机构；
- 禁止在没有正文或产物缺失时凭题目给出"通过"结论。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`academic_review` · 技能：`academic_review`
- 形态：协议技能（无命令行，只规定必须调用哪些硬工具、哪些判断不许自己做）

引擎强制执行的规则（不是建议）：

- 六项可度量维度以 research_audit 的结果为准，不重新打分。
- 原创性/伦理两项必须显式标注为人工判断，不得伪装成引擎结论。
- 同一模型产出的多份评审在 research_panel 里会被声明为 same-model，不能当独立评审。

<!-- hardtool-contract:end -->
