---
name: research_screen
description: 按 PRISMA 做纳排筛选：用固定规则与原因码筛选标题摘要或全文阶段，支持人工判定覆盖，输出每条判定明细与 PRISMA 计数（可复算、可审计）。
---

# research_screen

筛选是**可审计**环节：每条排除都有原因码，重跑得到同一结果。

## 参数

| 参数 | 说明 |
| --- | --- |
| `run` | 运行 id（必填） |
| `stage` | `title_abstract`（默认）或 `full_text` |
| `criteria` | 纳排标准对象；不传则用运行内 `criteria.json` |
| `decisions` | 人工判定：`{文献key: {decision: "include"/"exclude", reason: "..."}}` |
| `pdf_dir` | 仅 full_text：存放 PDF 的目录（需要 venv 里的 pypdf） |
| `fulltext_keywords` | 仅 full_text：`{include: [...], exclude: [...]}` 关键词自动初筛 |

## 纳排标准结构

```json
{"include": {"year_from": 2018, "year_to": 2026, "languages": ["en", "zh"],
             "types": ["article", "review", "preprint", "conference"],
             "require_abstract": true, "title_keywords": []},
 "exclude": {"title_keywords": ["retracted", "corrigendum"], "abstract_keywords": [],
             "venues": [], "retracted": true}}
```

原因码：`E-YEAR` `E-TYPE` `E-LANG` `E-NOABS` `E-RETRACTED` `E-KEYWORD` `E-VENUE` `E-MANUAL` `U-PENDING`。

## 两阶段

1. `title_abstract`：规则自动判定，通常一轮完成；
2. `full_text`：需要逐条人工/模型判断（写进 `decisions`）。没有判断的条目保持 `pending`，
   `gate=FAIL`——这是刻意的，避免"未读全文却声称全文筛选"。

## 返回

`counts`（identified/duplicates_removed/screened/excluded/included/excluded_by_reason）、`gate`、`pending`。
计数会同时写入 `prisma.md`，可直接贴进论文方法节。

## 注意

- 不要为了让数字好看放宽标准；标准改动应写进 `criteria` 并在报告里说明；
- 自动关键词全文筛选只作初筛建议，工具会在 `degraded` 里声明；
- 下一步：`research_verify`。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`screen` · 技能：`research_screen`
- 风险：`low`（普通成员可调用）
- 超时：180 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `stage` | 枚举(title_abstract / full_text) | 是 | 筛选阶段；full_text 需要 decisions 给出逐条判断。 |
| `criteria` | 对象(JSON) | 否 | 纳排标准；不传则用运行内 criteria.json（首次自动生成模板）。结构：{include:{year_from,year_to,languages,types,require_abstract,title_keywords},exclude:{title_keywords,abstract_keywords,venues,retracted}} |
| `decisions` | 对象(JSON) | 否 | 人工判定覆盖：{文献key: {decision:'include'\|'exclude', reason:'...'}}。 |
| `pdf_dir` | 字符串 | 否 | 全文 PDF 所在目录（仅 full_text 阶段；需要 venv 里的 pypdf）。 |
| `fulltext_keywords` | 对象(JSON) | 否 | 全文自动判定关键词：{include:[...],exclude:[...]}，仅作初筛建议。 |

引擎强制执行的规则（不是建议）：

- 纳排判定使用固定原因码（I-OK / E-YEAR / E-TYPE / …），每条排除都有据可查。
- 全文阶段没给 decisions 的条目记为 pending，gate=FAIL，不允许带着未决条目进入写作。
- 人工覆盖（decisions）优先于规则，并标记 manual=true。

<!-- hardtool-contract:end -->
