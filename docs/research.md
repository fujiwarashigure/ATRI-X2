# 科研研究助理（1.1.3）

把「查文献 → 筛文献 → 核验引文 → 写稿 → 评审 → 导出」做成 **ATRI-X 的一个原生子功能**：
12 个确定性硬工具 + 3 个协议技能 + 1 个只读命令 + 1 个桌面产物面板 + 一个随包分发的
Python 引擎。目标是**换 LLM 之后输出质量与流程不变**——凡是能算的（去重、计数、核验、
格式、结构、评分）都由引擎代码做，模型只负责"判断与表达"。

> 引擎与技能都是本项目的原创实现，没有包含任何上游 `academic-research-skills` 的文本或代码。
> 原创性核查方法与结果见 [LEGAL.md](../LEGAL.md) 与发行说明。

---

## 1. 怎么就算"就绪"

安装版 / 便携版**随包自带**研究引擎（`_internal/research_pack/`），
**首次启动自动安装**到用户配置目录，不需要系统 Python、不需要另跑安装器：

```text
%APPDATA%\ATRI-X\research\            （便携版：<解压目录>\data\appdata\ATRI-X\research）
├── engine\atrix_research\            引擎源码（38 个模块，约 8.7k 行）
├── settings.json                     引擎设置（检索源、阈值、评审权重…）
├── install.json                      安装清单（随包摘要 / 引擎版本 / 命令形态）
├── runs\<运行id>\                    每次研究的全部产物
└── venv\                             仅源码树安装器（atri-research）创建；发行版不需要
```

技能装到 `%APPDATA%\ATRI-X\skills\agent\research_*\`：15 个目录，其中 12 个带
`ATRIX_SKILL.toml`（可调用硬工具），3 个是纯协议文档技能。

检查是否就绪：

```text
/research env                                            # 群聊/命令行里看状态
atri-x.exe -m atri_x.research.bootstrap --status         # 打印 JSON（排障用）
```

**源码树运行**（`python -m atri_x`）时引擎不随仓库自动安装：用交付的 `atri-research/install.py`
装一份带独立 venv 的版本（`--offline` 可完全离线），或者只做只读调试时让它保持"未就绪"。

---

## 2. 对话里怎么用

不需要记工具名，用自然语言提要求即可，模型会按需发现 `research_*` 技能并逐步调用：

```text
帮我调研一下「浮式风电平台的系泊系统失效模式」，写一篇综述，目标 6000 字，GB/T 7714 引用格式
```

一次完整研究对应 11 步流水线（阶段名会出现在 `/research` 与「研究」页）：

| 阶段 | 谁在做 | 产出 |
| --- | --- | --- |
| 1 立项 | `research_plan(action=init)` | `run.json`（题目 / 研究问题 / 文体 / 语言 / 样式）、`criteria.json`（纳排标准） |
| 2 检索 | `research_search` | `search.json` + `library.json`（去重统计、每源成败） |
| 3 筛选 | `research_screen(stage=title/abstract)` → `(stage=fulltext)` | `screening.json` + `decisions.json` + `prisma.md` |
| 4 核验 | `research_verify` | `verification.json`（DOI 反查 Crossref / DataCite，`verified_ratio`） |
| 5 大纲 | `research_plan(action=outline)` | `outline.json` + `outline.md`（逐节配额 + 证据池） |
| 6 引用 | `research_cite` | `references.json` + `references.md`（cite_key ↔ 文献映射，APA / GB-T 7714） |
| 7 写作 | `research_write(action=append)` × N | `draft.md`（正文 + `[@key]` 引用标记） |
| 8 质检 | `research_write(action=check)` | `qa.json` + `qa.md`（字数、结构、引用覆盖、编造引用检测） |
| 9 评审 | `research_panel` | `panel.json`（加权总分 / accept·minor·major·reject；同模型评审降级为 `NOT_INDEPENDENT`） |
| 10 确定性评审 | `research_audit` | `audit.json`（六维固定公式评分，不随模型变） |
| 11 导出 | `research_export(action=all)` | `report.md` + `exports/*.md|html|docx|pdf` |

`research_flow`（顺序状态机）会告诉你"下一步该调哪个工具"，`research_compose` 负责把
正文结构化成契约格式（缺节、超引用预算、编造引用都会被拒绝），`research_doctor` 做环境/网络/
缓存体检。**工具的输出统一是一个 JSON 信封**：

```json
{"ok": true, "tool": "search", "run": "r-…", "engine": "0.2.0", "elapsed_ms": 40888,
 "data": {...}, "artifacts": [{"path": "library.json", "bytes": 95464}],
 "degraded": [], "warnings": []}
```

`degraded` 非空 = 某个可选能力降级（例如没装 PDF 库时导出仍给 md/html/docx），
不是失败；`gate`（`PASS` / `WARN` / `FAIL`）才是判定。

---

## 3. 12 个硬工具（全部 `risk="low"`，普通成员可用）

| 工具 | 必填参数 | 做什么 | 闸门 |
| --- | --- | --- | --- |
| `research_plan` | `action` | 立项 / 看状态 / 生成大纲与逐节契约 | 立项校验题目、研究问题、文体、样式 |
| `research_search` | `run` `queries` | 多源检索（OpenAlex / Crossref / arXiv / PubMed / Semantic Scholar）+ 去重落库 | 全源失败才报错；每源成败都记录 |
| `research_screen` | `run` `stage` | 标题/摘要/全文筛选 + PRISMA 计数 + 原因码 | 全文阶段有 pending → `gate=FAIL` |
| `research_verify` | `run` | DOI 反查（Crossref / DataCite），标题+年份+作者比对 | 低于阈值 `WARN`，全失败 `FAIL` |
| `research_cite` | `run` | 生成参考文献（APA / GB/T 7714），无 DOI 回退 arXiv / PubMed / OpenAlex 定位符 | 引用必须来自文献库 |
| `research_write` | `action` | 追加正文 / 质检（字数、结构、引用覆盖、编造检测） | error > 0 → `FAIL` |
| `research_panel` | `run` | 评审聚合：加权总分与决定 | 同一模型评审 → `NOT_INDEPENDENT` 降级 |
| `research_export` | `action` | 导出 md / html / docx / pdf | 全部失败才报错；缺库显式 degraded |
| `research_doctor` | — | 环境、网络、缓存与运行列表体检 | critical 失败 → 非零退出 |
| `research_flow` | `action` | 下一步顺序状态机（该检索还是该写稿） | 违反顺序会被指出 |
| `research_compose` | `run` `action` | 结构化成稿 + 契约校验 + 自动质检 | 缺节 / 超预算 / 编造引用 → 拒绝写入 |
| `research_audit` | `run` | 六维固定公式评分（原创性/严谨性/清晰度/意义/伦理…） | 公式与权重在 `settings.json`，可复算 |

---

## 4. 命令与桌面面板

| 入口 | 用途 |
| --- | --- |
| `/research` | 列出全部研究运行（id / 阶段 / 题目 / 更新时间） |
| `/research <运行id>` | 查看某次运行的摘要与产物清单（支持 id 前缀匹配） |
| `/research env` | 引擎 / venv / 依赖 / 数据目录体检（引擎没装也能回答） |
| `/research open` | 打开研究数据目录（仅超级管理员） |
| 桌面端「研究」选项卡 | 运行列表 + 摘要 + 产物树；双击运行打开目录；「打开报告」「打开导出目录」按钮 |

命令与面板都是**只读**：没有删除/重跑按钮，避免绕过工具的风险与审计口径。

---

## 5. 依赖与降级

- 引擎**零第三方代码依赖**：检索、去重、筛选、核验、引用、QA、评审聚合全部标准库完成。
- 可选依赖只有两个，用于 PDF：
  - `reportlab`：`research_export` 的 PDF 输出；缺它时导出 md / html / docx，并在 `degraded` 里写明。
  - `pypdf`：PDF 全文抽取（全文筛选阶段上传 PDF 时才需要）；缺它时该阶段报 `DEPENDENCY_MISSING`
    并给出安装提示，不会假装成功。
- 发行版（安装包 / 便携版）把这两个包打进冻结包，因此**冻结产物不需要系统 Python 也能导出 PDF**；
  源码树安装器 `atri-research/install.py` 会建独立 venv 装它们（`--offline` 用随包 `wheels/`）。

---

## 6. 权限与数据

- 12 个工具 `risk="low"`、`superadmin_only=false`，低于默认 `user_max_tool_risk="low"`——
  群内普通成员可以调用；`/research` 命令 `min_role=user`，`/research open` 在代码里再判超管。
- 工具参数里**没有"输出路径"字段**：`run_id` 正则白名单 + 运行目录必须在 `runs/` 之下，
  产物名是固定集合，无法读任意文件或写任意路径。
- 研究库当前是**安装级共享**（ATRI-X 的工具执行环境暂不透传 actor，无法按成员隔离）；
  多成员隔离需要 ATRI-X 在 `CommandTool` 执行时注入 actor 环境变量，属于后续工作。
- 网络数据源（OpenAlex / Crossref / DataCite / arXiv / PubMed / Semantic Scholar）各有自己的
  使用条款与速率限制；引擎统一做了最小间隔、缓存、重试与 `contact_mailto` 配置，请按各自
  条款使用。见 [LEGAL.md](../LEGAL.md)。

---

## 7. 排障

| 现象 | 处理 |
| --- | --- |
| `/research env` 显示「引擎：未安装」 | 发行版正常首次启动会自动装；若没有，跑 `atri-x.exe -m atri_x.research.bootstrap --check` 看 JSON 里的 `message`（源码树请用 `atri-research/install.py`） |
| 「随包引擎」行显示"有更新" | 安装包升级后引擎会按包摘要自动升级；下次启动仍显示时可跑 `--check` 或 `--force` 重装 |
| 检索全部失败 | `research_doctor(action=env)` 实测网络与缓存；检查代理 / 防火墙 / DNS |
| 核验比率低 | 无 DOI 的条目只能给候选 DOI，需人工确认（设计如此）；`research_doctor` 会提示 API 状态 |
| 导出缺 PDF | 检查 `degraded` 字段：源码树 venv 里装 `reportlab`，或直接改用发行版（已内置） |
| `/research` 列出为空 | 还没有运行过研究；在对话里提一个调研需求即可创建运行 |
| 找不到运行 | `/research` 看完整 id；`run.json` 损坏的运行不会出现在列表里 |

---

## 8. 开发：新增一个硬工具

引擎的"硬工具"是**声明式规格 + 纯函数**，四步：

1. 在 `research_pack/engine/atrix_research/tools.py` 写纯函数 `tool_xxx(args)`（无 LLM、无随机、无网络）；
2. 在 `capabilities.py` 用 `@register(Capability(...))` 声明参数 JSON Schema、`[args]` 映射、风险、
   超时、不变量与输出说明（渲染进 `ATRIX_SKILL.toml` 与 SKILL.md 附录）；
3. 注册进 `HANDLERS`；
4. `python make_skills.py --write` 刷新技能文档附录，重新生成 `research_pack/`（`pack.json` 摘要随之变化），
   重新打包后首次启动会自动升级。

技能文档里的"工具契约"附录由引擎生成并通过 `make_skills.py --check` / `run.py skills --check`
校验同源——手改文档会在门禁里报漂移。
