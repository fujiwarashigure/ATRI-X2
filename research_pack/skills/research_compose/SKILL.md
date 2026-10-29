---
name: research_compose
description: 结构化成稿：提交小节标题 + 段落 + cite_key，引擎负责契约校验、按大纲排序、渲染引用标记、统计预算并在写盘后自动质检。
---

# 结构化成稿（research_compose）

写作时**不要让模型拼 Markdown**：格式是质量最容易在换模后崩掉的部分。
把内容交成结构化 JSON，格式由引擎确定性渲染。

## 输入格式

```json
{"sections": [
  {"heading": "引言",
   "paragraphs": [
     {"text": "检索增强把外部证据引入生成过程，降低了无依据陈述的比例。",
      "cites": ["zhang2023rag", "liu2022survey"]}
   ]}
]}
```

- `heading` 必须来自 `outline.json` 的 `sections[].title`；
- `cites` 必须来自 `references.json` 的 `by_cite_key`；
- `text` 写完整句子；不要自己加 `[@key]`（加了会被抽取，不会重复）。

## 动作

| action | 作用 |
| --- | --- |
| `validate` | 只校验不写盘；返回 `errors`/`warnings`/`markdown_preview`/`stats` |
| `append` | 追加**尚未写过**的小节；重复小节报 `E_ALREADY_WRITTEN` |
| `save` | 整体覆盖；必须覆盖全部大纲小节，否则 `E_INCOMPLETE_SAVE` |

## 硬缺陷（拒绝写入）

`E_UNKNOWN_HEADING` / `E_UNKNOWN_CITE` / `E_PLACEHOLDER` / `E_EMPTY_TEXT` /
`E_ALREADY_WRITTEN` / `E_INCOMPLETE_SAVE`。

## 写盘后自动发生什么

- 用 `qa.py` 跑质检，返回 `qa.verdict/score/issues`；
- 返回 `remaining_sections`，按它继续 append；
- `degraded=QA_FAILED` 表示错误必须修完再交付。

## 建议节奏

每轮 1-3 节（宿主每轮最多 10 步工具调用）；先 `validate` 再 `append`。
全部写完交给 `research_write action=check` 复核、`research_audit` 评分。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`compose` · 技能：`research_compose`
- 风险：`low`（普通成员可调用）
- 超时：120 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。 |
| `action` | 枚举(validate / append / save) | 是 | validate=只校验不写盘；append=追加未写过的小节；save=整体覆盖（须覆盖全部小节）。 |
| `sections` | 数组<对象(JSON)> | 否 | 结构化小节：[{heading: 大纲里的节标题, paragraphs: [{text: 正文段落, cites: [cite_key, ...]}]}]；也可传保存该 JSON 的文件路径。 |
| `allow_extra` | 枚举(true / false) | 否 | 是否允许大纲之外的子节标题，默认 false（会报 E_UNKNOWN_HEADING）。 |

结果里的关键字段：

- `ok`：false 表示契约拒绝写入，按 errors[].fix 修正后重提。
- `errors`：[{code, path, message, fix}]，硬缺陷，必须修。
- `warnings`：[{code, path, message, fix}]，写入但会被质检/评审看到的风险。
- `qa`：写盘后自动质检结果（verdict/score/errors/issues）。
- `remaining_sections`：还没写入正文的大纲小节。

引擎强制执行的规则（不是建议）：

- 小节标题必须来自 outline.json；引用键必须来自 references.json，否则拒绝写盘（E_UNKNOWN_HEADING / E_UNKNOWN_CITE）。
- 渲染是纯函数：同一份结构化输入在任何模型下产生逐字节相同的 Markdown。
- 小节顺序按大纲重排；重复提交已写过的小节会被 E_ALREADY_WRITTEN 拒绝，避免重试写重。
- 写盘后自动跑 qa.py，verdict/score/issues 来自规则，不采纳模型自评。

<!-- hardtool-contract:end -->
