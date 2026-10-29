---
name: research_verify
description: 引文核验：逐条用 DOI 反查 Crossref/DataCite，比对标题/年份/第一作者，标记撤稿与疑似错配；缺 DOI 时按标题+年份+作者反查给出候选（不自动写入）。输出通过率与闸门。
---

# research_verify

把"模型说这篇文献存在"变成"注册机构证明它存在"。

## 参数

| 参数 | 说明 |
| --- | --- |
| `run` | 运行 id（必填） |
| `limit` | 最多核验条数（默认全部纳入文献） |
| `min_ratio` | 通过率闸门，默认 0.9 |
| `suggest_doi` | 是否对缺 DOI 条目做标题反查（默认 true） |

## 返回与判读

`summary` 六个计数：`verified` / `partial` / `mismatch` / `not_found` / `no_doi` / `not_run`，
以及 `verified_ratio` 与 `gate`。

| 状态 | 含义 | 写作时怎么用 |
| --- | --- | --- |
| VERIFIED | DOI 可解析且标题/年份/作者一致 | 可放心引用 |
| PARTIAL | DOI 可解析但部分元数据不一致（如年份差 1） | 引用但需人工确认版本 |
| MISMATCH | DOI 指向的元数据明显不同 | **不得引用**，先修正条目 |
| NOT_FOUND | DOI 无法解析 | **不得引用**，可能是错号或已失效 |
| NO_DOI | 条目本身没有 DOI | 可引用但要在局限里说明可核查性弱 |
| NOT_RUN | 网络/数据源问题未核验 | 网络恢复后重跑 |

`gate=WARN` 不代表失败，代表"通过率低于阈值"：继续流程，但结论要更保守，
并把 `warnings` 里的原因原样告诉用户。

## 关于候选 DOI

`no_doi` 条目会带 `suggested_doi` 与 `suggested_similarity`，这是**建议**不是事实。
要采用必须由用户确认；不要自行把候选 DOI 当成已核验引用。

## 注意

- 一次核验几十条要 1-2 分钟（有磁盘缓存，重跑很快），不要因为慢而跳过；
- 撤稿标记来自 OpenAlex/Semantic Scholar，命中 `retracted=true` 的条目必须处理；
- 下一步：`research_cite`，然后 `research_plan` action=outline。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`verify` · 技能：`research_verify`
- 风险：`low`（普通成员可调用）
- 超时：300 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `limit` | 整数 | 否 | 最多核验多少条（默认全部纳入文献）。 |
| `min_ratio` | 数字 | 否 | 核验通过率闸门，默认 0.9；低于它闸门为 WARN。 |
| `suggest_doi` | 枚举(true / false) | 否 | 缺 DOI 时是否按标题反查候选，默认 true。 |

引擎强制执行的规则（不是建议）：

- 只按 DOI 反查 Crossref/DataCite，不自动改写文献元数据。
- 通过率低于 min_ratio 时 gate=WARN（如实报警，不粉饰）。
- 缺 DOI 时只给 ≥0.92 相似度的候选，仍需人工确认。

<!-- hardtool-contract:end -->
