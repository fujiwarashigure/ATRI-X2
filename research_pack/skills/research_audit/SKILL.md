---
name: research_audit
description: 确定性评审：按固定公式给六项可度量维度打分并列出必修缺陷；原创性与伦理如实标为未度量，交给 research_panel。
---

# 确定性评审（research_audit）

"评审"里可度量的部分不该由模型自由裁量。本工具从运行产物算分，换模型不会变。

## 怎么用

1. 稿件质检通过后调 `research_audit --run <id>`；
2. 读结果：
   - `weighted_total`：0-100；85+ accept，70-85 minor，55-70 major，<55 reject；
   - `dimensions`：六维得分、权重、算法出处；
   - `unmeasured`：不可度量维度（原创性/伦理）——必须由 `research_panel` 覆盖；
   - `blocks`：必须处理的缺陷（每条带 `fix`）；
3. `gate=FAIL` → 按 `blocks[].fix` 修完再重跑；`gate=WARN` → 修主要项后可用于交付，
   但要在回答里说明；

## 六维算法（固定，不可改判）

| 维度 | 权重 | 出处 |
| --- | --- | --- |
| 证据覆盖 | 0.20 | 引用数 ÷ 大纲预算 + 参考文献被引用率 + 证据池 |
| 引文可靠性 | 0.22 | DOI 核验通过率 + 非错配比例 |
| 方法可复现性 | 0.15 | 检索步骤成功率 + 筛选有无待判定 + PRISMA 自洽 |
| 结构完整度 | 0.18 | 小节覆盖率 + 字数区间 + 摘要 |
| 表述克制 | 0.13 | 占位符 + 绝对化措辞 |
| 交付完整度 | 0.12 | 报告 + md/html/docx/pdf 覆盖 |

## 边界

- 不重新检索、不改正文、不改参考文献；
- 缺失维度不按 0 分计入（权重归一化），并在 `unmeasured` 如实列出；
- 原创性/伦理的判断请走 `research_panel`，不要伪装成引擎结论。

## 输出

写 `audit.json` / `audit.md` 到运行目录；这是可审计的评审留痕，
报告与 PRISMA 一并构成交付证据链。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`audit` · 技能：`research_audit`
- 风险：`low`（普通成员可调用）
- 超时：90 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `run` | 字符串 | 是 | 运行 id；'latest' 表示最近一次。 |

结果里的关键字段：

- `weighted_total`：0-100 加权总分；85+ accept，70-85 minor，55-70 major，<55 reject。
- `dimensions`：六项维度的得分、权重、算法出处。
- `unmeasured`：无法由产物度量的维度（原创性/伦理），必须由 panel 覆盖。
- `blocks`：[{code, message, fix}]，必须处理的缺陷清单。

引擎强制执行的规则（不是建议）：

- 分数来自产物（qa.json / verification.json / screening.json / search.json / exports）的固定公式，与模型无关。
- 缺失维度不按 0 分计入（权重归一化），并在 unmeasured 里如实列出。
- qa.json 有 error、筛选有待判定、核验全 FAIL 时 gate 直接 FAIL。

<!-- hardtool-contract:end -->
