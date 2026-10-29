---
name: research_export
description: 交付物生成：装配研究报告（方法/PRISMA/证据表/局限），或把正文与报告导出为 Markdown/HTML/DOCX/PDF；缺第三方库时降级并明确说明。
---

# research_export

最后一步：把落盘产物变成用户能打开的文件。

## 参数

| 参数 | 说明 |
| --- | --- |
| `action` | `report` 只装配报告 / `export` 导出已有稿件 / `pdf` 抽取 PDF 全文 / `all` 报告+导出 |
| `run` | 运行 id（必填） |
| `formats` | 导出格式子集，默认 `["md","html","docx","pdf"]` |
| `pdf_dir` | `pdf` 动作的源目录（超管把 PDF 放进该目录后使用） |

## 两种交付物

1. **report.md（研究报告）**：研究问题、检索执行表、纳排标准、PRISMA 计数、
   纳入证据表（年份/作者/标题/来源/被引/核验状态）、证据质量、写作骨架、局限、参考文献。
   数字全部来自产物，适合给导师/评审看方法是否站得住。
2. **exports/draft.\***（论文正文）：由 `draft.md` 导出，适合直接投递或继续排版。

## 格式与降级

| 格式 | 依赖 | 缺失时 |
| --- | --- | --- |
| md / html | 无（标准库） | 总能产出 |
| docx | 无（内置零依赖 OOXML 写入器；装了 python-docx 则排版增强） | 总能产出 |
| pdf | reportlab（内置中文字体） | 跳过并写进 `degraded` |

`degraded` 必须原样转述给用户，例如"缺 reportlab，只导出 docx/html；装了 venv 后可补导 PDF"。

## 返回

`report`（行数/字符数）、`exports`（每个文件的状态与字节数）、`artifacts`。
给用户列出实际生成的文件名与大小，不要把"降级跳过"说成"已导出"。

## 注意

- 报告里的数字是审计依据，**不要手工改动**；要改就重跑对应阶段；
- 导出的 docx 使用 Times New Roman + 宋体，表格带边框，参考文献不自动加悬挂缩进（需排版软件处理）；
- PDF 用 reportlab 的 CID 中文字体，避免乱码；若版式要求高，建议导出 docx 后用 Word 排版。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`export` · 技能：`research_export`
- 风险：`low`（普通成员可调用）
- 超时：240 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | 枚举(report / export / pdf / all) | 是 | report=只装配报告；export=导出已有稿件；pdf=抽取 PDF 全文；all=报告+导出。 |
| `run` | 字符串 | 否 | 运行 id；'latest' 表示最近一次。缺省时多数工具要求显式给出。 |
| `formats` | 数组<字符串> | 否 | 导出格式子集，默认 ['md','html','docx','pdf']。 |
| `pdf_dir` | 字符串 | 否 | pdf 动作的源目录。 |

引擎强制执行的规则（不是建议）：

- 缺 docx/pdf 依赖时降级并在 degraded 里逐项说明，不会假装成功。
- 报告里的方法/PRISMA/证据表数字直接取自产物，不允许模型重写数字。
- 产物只写运行目录的 exports/ 与固定文件名。

<!-- hardtool-contract:end -->
