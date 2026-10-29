---
name: research_plan
description: 研究立项/状态/大纲：新建一次研究运行（题目、研究问题、文体、语言、引用样式），查看运行与产物，或按文体生成逐节写作契约与证据池。
---

# research_plan

科研流程的入口与骨架生成器。**没有它就没有 run，后续工具全部无法工作。**

## 参数

| 参数 | 说明 |
| --- | --- |
| `action` | `init` 立项 / `status` 查状态（含运行列表）/ `outline` 生成大纲 |
| `run` | 运行 id；`latest` 表示最近一次 |
| `title` `question` | init 必填：题目、一句话研究问题 |
| `language` | `zh`（默认）/ `en` |
| `style` | `gbt7714`（中文刊默认）/ `apa7` |
| `doc_type` | `review` 综述 / `empirical` 实证 / `proposal` 开题 |
| `target_words` | 目标篇幅，默认 6000 |

## 何时调用

- 用户提出一个新的研究任务 → `action=init`（先问清文体与引用样式）；
- 不知道进行到哪一步 / 想确认产物 → `action=status`（不传 run 列出全部运行）；
- 检索与筛选完成后 → `action=outline` 生成逐节契约与证据池。

## 返回与下一步

- init 返回 `run`、`dir`、`next`；把 run id 记下来，后续都要用。
- outline 返回每节的目标字数与引用预算、`evidence_count`、`citation_total_budget`；
  下一步用 `research_write` action=append 逐节写作。
- `status` 的 `artifacts` 是已生成产物清单，缺什么一眼能看出来。

## 注意

- 一次研究只建一个 run；换题目要新建，不要在旧 run 上改题目。
- 大纲的引用预算之和是质检的判据，写作时按节满足，不要全局摊平。
- 语言与引用样式可以在 init 后通过 `research_cite` 的 `style` 参数临时覆盖，但正文语言不会自动翻译。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`plan` · 技能：`research_plan`
- 风险：`low`（普通成员可调用）
- 超时：120 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | 枚举(init / status / outline) | 是 | init=新建运行；status=查看运行/产物；outline=生成大纲与写作契约。 |
| `run` | 字符串 | 否 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `title` | 字符串 | 否 | 题目（init 必填）。 |
| `question` | 字符串 | 否 | 一句话研究问题（init 必填）。 |
| `language` | 枚举(zh / en) | 否 | 写作语言，默认 zh。 |
| `style` | 枚举(gbt7714 / apa7) | 否 | 引用样式：gbt7714=GB/T 7714-2015，apa7=APA 第 7 版。 |
| `doc_type` | 枚举(review / empirical / proposal) | 否 | 文体：综述 / 实证 / 开题（默认 review）。 |
| `target_words` | 整数 | 否 | 目标篇幅（字/词），默认 6000。 |
| `owner` | 字符串 | 否 | 发起人标记（可选，写入运行记录）。 |

引擎强制执行的规则（不是建议）：

- 大纲与逐节引用预算是模板 + 文献数计算的，不接受模型改写结构。
- 运行 id 只允许 a-z/0-9/连字符（白名单校验，防止路径穿越）。
- init 之后的阶段推进以 research_flow 的 next 为准。

<!-- hardtool-contract:end -->
