---
name: research_search
description: 多源文献检索与去重：在 OpenAlex/Crossref/arXiv/PubMed/Semantic Scholar 上按关键词检索，按 DOI/标题合并重复条目，写入本次运行的文献库并回报每个源的成败。
---

# research_search

一次调用完成"检索 + 去重 + 落库"。**这是文献的唯一来源**，之后所有引用都来自它写出的 `library.json`。

## 参数

| 参数 | 说明 |
| --- | --- |
| `run` | 运行 id（必填） |
| `queries` | 字符串数组：2-5 组关键词。中英文各给一组往往能显著提高覆盖 |
| `sources` | 数据源子集：`openalex`（默认，覆盖最广）、`crossref`、`arxiv`、`pubmed`、`semanticscholar` |
| `limit` | 每源每检索式条数上限（默认 20，最大 100） |
| `year_from` `year_to` | 年份范围；不传则沿用 `criteria.json` 里的范围 |

## 检索式写法

- 用领域术语而不是整句：`"retrieval augmented generation"`、`"RAG survey"`、`"检索增强生成"`；
- 同义词/缩写分开各给一组：`["LLM agent", "large language model agent"]`；
- 太宽的词（如 `"AI"`）会带来大量噪声，先窄后宽，不够再补检。

## 返回

`identified`（原始命中）、`deduped`（去重后）、`duplicates_removed`、`by_source`、`steps`（每源每检索式的成败）。
`degraded` 里有失败源时，要在给用户的回答里说明覆盖缺口。

## 下一步

`research_screen` stage=title_abstract。先看 `library.json` 的 `dedupe.possible_duplicates`，
必要时用 `decisions` 人工确认可疑合并（不合并的条目会各自留在库里）。

## 注意

- 本工具不判断文献质量，只负责"找全、去重、留痕"；
- 公共 API 有速率限制，一次调用 30 秒左右是正常的；不要并发重复调用；
- 命中为 0 时不要空跑筛选：换更宽的关键词或去掉年份限制重试，并向用户说明。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`search` · 技能：`research_search`
- 风险：`low`（普通成员可调用）
- 超时：240 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `queries` | 数组<字符串> | 是 | 检索式列表（从研究问题拆出的 2-5 组关键词，中英文都可）。 |
| `sources` | 数组<字符串> | 否 | 数据源子集，默认 openalex/crossref/arxiv；可选 pubmed、semanticscholar。 |
| `limit` | 整数 | 否 | 每个源每个检索式的条数上限，默认 20。 |
| `year_from` | 整数 | 否 | 最早年份（可选）。 |
| `year_to` | 整数 | 否 | 最晚年份（可选）。 |

引擎强制执行的规则（不是建议）：

- 每个源的成败都记录在 search.json 的 steps 里，失败不会静默吞掉。
- 去重按 DOI 与归一化标题合并，结果可复算；每源条数上限 100。
- 检索式原样落盘，评审可据此复现检索过程。

<!-- hardtool-contract:end -->
