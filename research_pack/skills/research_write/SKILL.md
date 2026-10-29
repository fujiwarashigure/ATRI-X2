---
name: research_write
description: 稿件写入与质检：分段保存/追加正文（避免超长参数），或对全文跑固定规则检查（引用键闭合、占位符、引用预算、绝对化措辞、长度、参考文献格式）。
---

# research_write

写正文用 `append`，查问题用 `check`。正文**必须**由本工具落盘，否则后续质检与导出看不到。

## 参数

| 参数 | 说明 |
| --- | --- |
| `action` | `append` 追加一节 / `save` 整体覆盖 / `check` 质检 / `status` 查看字数 |
| `run` | 运行 id（必填） |
| `content` | `append`/`save` 的 Markdown 正文；单次 ≤ 20000 字符 |
| `min_section_citations` | 每节最低引用数；缺省按大纲的引用预算 |

## 写作循环（推荐）

1. `research_plan` action=status 确认 `outline.json` 与 `references.json` 就绪；
2. 每次写一节 → `action=append`；
3. 全部写完 → `research_cite` 重排 → `action=check`；
4. 按 `issues` 的 `fix` 修改 → 再 `check`，直到 PASS 或只剩可接受 warning。

## check 会查什么

- `CITATION_UNKNOWN_KEY`：引用了 `references.json` 里没有的键（error）；
- `CITATION_INDEX_OUT_OF_RANGE`：数字编号越界（error）；
- `PLACEHOLDER_REMAINS`：TODO/TBD/[EVIDENCE]/XXX/{{...}}/None 残留（error）；
- `REFERENCE_EMPTY` / `REFERENCE_DIRTY` / `REFERENCE_NUMBERING`：参考文献格式问题（error）；
- `SECTION_CITATION_LOW`：某节引用数低于预算（warn）；
- `REFERENCES_UNUSED`：参考文献未被正文引用（warn）；
- `OVERCLAIM_PHRASE`：出现"首次证明/彻底解决/proves that"等绝对化措辞（warn）；
- `ABSTRACT_LENGTH` / `LENGTH_DEVIATION`：摘要与总篇幅偏离（warn）。

## 返回

`verdict`（PASS/WARN/FAIL）、`score`、`issues`（含 `severity`/`code`/`where`/`message`/`fix`）。
**error 必须全部修掉**；warn 逐条判断，接受的在交付时说明。

## 注意

- 不要一次塞整篇（Windows 命令行参数有长度上限），按节 append；
- 不要在正文里手写参考文献列表——由 `research_cite` 生成后附加；
- 质检规则是可复算的，不接受"我觉得没问题"。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`write` · 技能：`research_write`
- 风险：`low`（普通成员可调用）
- 超时：120 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | 枚举(save / append / check / status) | 是 | save=整体覆盖；append=追加一段；check=质检；status=字数与节数。 |
| `run` | 字符串 | 否 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `content` | 字符串 | 否 | Markdown 正文（save/append 用；单次建议 ≤ 20000 字符）。 |
| `min_section_citations` | 整数 | 否 | 每节最少引用数；缺省按大纲的引用预算。 |

引擎强制执行的规则（不是建议）：

- 只写运行目录内的 draft.md（产物白名单），不接受任意路径。
- 质检规则固定：未知引用键/占位符/数字越界为 error，引用预算与绝对化措辞为 warn。
- 新写作建议用 research_compose，结构、标记与预算由契约强制执行。

<!-- hardtool-contract:end -->
