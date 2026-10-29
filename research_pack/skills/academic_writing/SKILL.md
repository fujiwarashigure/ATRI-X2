---
name: academic_writing
description: 科研写作协议：只产出结构化内容（小节标题 + 段落 + cite_key），由 research_compose 负责校验、排序、引用标记、预算与写盘。
---

# 科研写作协议

写作时你只负责**内容**：观点、证据选择、论证与限定。格式（章节顺序、`[@key]` 标记、
引用预算、字数统计、占位符）全部交给 `research_compose`——它是纯函数，换模型不会变。

## 1. 开工前必读

1. `research_flow`（run=<id>）→ 确认下一步是 `research_compose`；
2. `research_plan action=status` → 确认 `outline.json`、`references.json` 已存在；
3. 读 `outline.json` 的 `sections`：每节有 `title`（必须原样用）、`target_words`、
   `citation_budget`、`must_cover`；
4. 读 `references.json` 的 `by_cite_key`：这是**唯一可用**的引用键集合。

## 2. 提交格式（结构化，不是 Markdown）

```json
{"sections": [
  {"heading": "引言",
   "paragraphs": [
     {"text": "检索增强把外部证据引入生成过程，降低了无依据陈述的比例。",
      "cites": ["zhang2023rag", "liu2022survey"]}
   ]}
]}
```

- `heading` 必须等于 `outline.json` 里的 `title`（否则 `E_UNKNOWN_HEADING`）；
- `text` 是完整句子，不要自己写 `[@key]`（写了也会被抽取，不会重复）；
- `cites` 只用 `by_cite_key` 里的键（编造会 `E_UNKNOWN_CITE`，直接拒绝写入）；
- 每段 1-3 个引用；一段只放一个论断，别把三个结论塞进一句。

## 3. 执行顺序

1. `research_compose action=validate` —— 先校验不写盘；
2. 有 `errors` → 按 `errors[].fix` 修正后重提（`errors` 是硬缺陷）；
3. `action=append` 写入 1-3 节；工具会自动跑质检并返回 `qa` 与 `remaining_sections`；
4. 按 `remaining_sections` 继续下一批；
5. 全部写完 → `research_write action=check` 复核 → `research_audit`。

**每轮 1-3 节**。`append` 会拒绝重复小节（`E_ALREADY_WRITTEN`），不要靠"再写一遍"覆盖。

## 4. 每节写什么

| 章节 | 要写什么 | 常见错误 |
| --- | --- | --- |
| 摘要 | 问题、方法（检索与筛选口径）、主要发现、结论、局限一句 | 写成引言、出现未定义缩写 |
| 引言 | 领域现状 → 已有综述不足 → 本文问题与范围 → 贡献 | 只堆背景不给缺口 |
| 方法 | 检索源、检索式、时间范围、纳排标准、PRISMA 数字、质量评估方式 | 数字与 `screening.json` 不一致 |
| 现状 | 按技术路线/问题类型分组，每组给代表工作与关键结论 | 按论文逐篇流水账 |
| 综合讨论 | 一致结论、冲突证据、方法差异如何解释分歧、证据强度 | 只复述不比较 |
| 缺口展望 | 缺口 → 为什么重要 → 可验证的研究设计建议 | 空泛的"未来可以继续研究" |
| 局限 | 检索覆盖、语言偏倚、筛选主观性、证据等级、适用范围 | 写成免责声明 |

## 5. 语言与风格

- 中文：术语首次出现给英文原词；数字与单位之间留空格；不要"笔者认为"连堆。
- 英文：方法过去时、结论现在时；避免绝对化（proves / revolutionary / first ever）。
- 任何"首次证明""彻底解决""100%"式表述都违反契约精神，会被质检标 `OVERCLAIM_PHRASE`。
- 转述他人观点要带限定（"该研究报告""在其样本内"），不要写成普适结论。

## 6. 硬约束（引擎执行，不是建议）

- `E_UNKNOWN_CITE` / `E_UNKNOWN_HEADING` / `E_PLACEHOLDER` → 拒绝写入；
- 引用预算不足、字数偏离、顺序被重排 → `warnings`，会被质检与评审看到；
- 写盘后立即跑 `qa.py`，`verdict/score/issues` 来自规则，**不接受模型自评**。

## 7. 禁止事项

- 禁止编造引文、DOI、页码、数据；
- 禁止把 `TODO`/`TBD`/`[EVIDENCE]` 留在正文；
- 禁止直接往 `draft.md` 拼 Markdown（绕过契约与自动质检，等于放弃质量门禁）；
- 禁止替用户虚构实验结果：没有数据就写"本文为综述，不含原始实验"。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`academic_writing` · 技能：`academic_writing`
- 形态：协议技能（无命令行，只规定必须调用哪些硬工具、哪些判断不许自己做）

引擎强制执行的规则（不是建议）：

- 正文必须经 research_compose 写盘，禁止直接拼 Markdown 写 draft.md（否则绕过契约与自动质检）。
- 引用只能来自 references.json 的 by_cite_key；编造 key 会被 E_UNKNOWN_CITE 拒绝。
- 残留占位符、绝对化措辞会被契约与质检同时拦下。

<!-- hardtool-contract:end -->
