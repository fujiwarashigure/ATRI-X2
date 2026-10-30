---
disable-model-invocation: true
name: drawio_skill
description: 画/改 draw.io 可编辑架构图：把描述、Diagram IR、代码目录、Terraform、SQL、OpenAPI、AsyncAPI、Proto、GraphQL、K8s、Compose、CI 配置转成 .drawio，校验结构并用内置 draw.io 无头渲染 PNG/SVG/PDF。用户要架构图、流程图、UML/ERD/时序图、部署图、数据流图，或要求"可编辑的图/架构图/画个图"时使用；只想要纯文本图（Mermaid/PlantUML）时优先用对应能力。
license: MIT
allowed-tools: [Read, Write]
metadata: {"openclaw":{"requires":{"anyBins":["python3"]},"emoji":"📐","os":["darwin","linux","win32"],"install":[{"id":"brew-drawio","kind":"brew","formula":"drawio","bins":["drawio"],"label":"Install draw.io for native exports","os":["darwin"],"optional":true},{"id":"brew-graphviz","kind":"brew","formula":"graphviz","bins":["dot"],"label":"Install Graphviz for automatic layout","os":["darwin"],"optional":true}]},"hermes":{"tags":["drawio","diagram","architecture","visualization","uml"],"category":"design","requires_tools":["python3"],"related_skills":["mermaid","excalidraw","plantuml"]},"author":"Agents365-ai","version":"3.4.2","homepage":"https://github.com/Agents365-ai/drawio-skill","compatibility":"Core IR, XML, sync, query, test, review, and Story workflows need Python 3 only; native export needs draw.io; Graphviz is optional.","platforms":["macos","linux","windows"]}
---

# draw.io 架构图（ATRI-X 内置）

产出**可编辑**的 `.drawio` 文件，而不是只能看的图片。ATRI-X 已把上游 drawio-skill
3.4.2 封装成一个工具 `drawio`（只有超级管理员可用），并**内置 draw.io 32.6.1 无头渲染器**：
不需要用户装 draw.io，也不需要 Python 环境。所有动作都通过 `drawio` 工具完成，
**不要**在回复里让用户跑 `python scripts/diagramctl.py` 之类的命令。

## 动作一览

| action | 用途 | 关键参数 | 返回 |
| --- | --- | --- | --- |
| `build` | 从代码目录 / IaC / SQL / API schema 生成 | `source` 或 `content` + `source_type`；可选 `format`、`group`、`direction`、`views`、`title` | `.drawio` + 图片/PDF |
| `create` | 用内联 Diagram IR / graph JSON / 手写 XML 生成 | `content`（或 `source`）+ `format` | `.drawio` + 图片 |
| `export` | 已有 `.drawio` 导出图片 | `source`、`format`（png/svg/pdf）、`scale`、`pages` | 图片/文件 |
| `validate` | 结构校验（error/warning/score） | `source` 或 `content` | 文本报告 |
| `inspect` | 结构摘要（节点/边/页面/无障碍文本） | `source` | JSON 摘要 |
| `query` | 按 kind/owner 过滤，或查两个节点间的有向路径 | `source` + `query_kind`/`query_owner`/`from_node`/`to_node` | JSON |
| `shapes` | 检索官方形状 style 字符串 | `keyword` | 命中列表 |
| `docs` | 列/读技能内置参考文档 | `doc`（省略则列出） | 文档正文 |
| `doctor` | 自检渲染器/依赖/版本 | — | 环境报告 |

## 参数约定

- `source`：文件或目录路径；可写绝对路径。用 `source` 引用**磁盘上已有**的内容。
- `content`：内联内容；适合用户直接在聊天里贴的 SQL / OpenAPI / Terraform /
  AsyncAPI / Proto / GraphQL / IR JSON。
- `source_type`：`auto`（默认，按扩展名/文件头猜）或显式指定：
  `terraform`、`sql`、`openapi`、`asyncapi`、`proto`、`graphql`、`kubernetes`、
  `compose`、`ci`、`python`、`javascript`、`go`、`rust`、`pyclasses`、`ir`、`graph`、`drawio`。
  代码目录导入必须给 `source`（目录）+ `python`/`javascript`/`go`/`rust`/`pyclasses`。
- `format`：`png`（默认，同时返回 `.drawio`）、`drawio`（只要可编辑源）、`svg`、`pdf`。
- `output`：输出文件名（不含扩展名），默认 `diagram`；产物写在技能目录 `out/` 下，
  返回的路径都是相对路径，宿主导入后会作为图片/文件发出去。
- `group`：按目录/模块聚合节点（代码/大仓库推荐）；`direction`：`TB` 纵向 / `LR` 横向。
- `views`：`executive,system,deployment,dataflow,security` 的逗号子集，一图多视图。
- `scale` / `width` / `height` / `border` / `transparent` / `pages`：导出细节；
  **不要同时给 `width` 和 `scale`**。多页图用 `pages`：`all`、`3`、`1-3`。

## 推荐工作流

1. **判类型**：用户要"可改的图"→ `.drawio`；只要说明图 → 默认 PNG + `.drawio`。
   输入是代码/配置/表结构/接口定义时，优先 `build`（关系来自源码，不靠猜）；
   输入是描述且需要精确排版时，读 `docs` 里的 `diagram-types.md` +
   `xml-authoring.md`，用 `create` 写 IR/XML。
2. **生成**：`build`/`create` 会自动做结构校验；报告里会带 error/warning/score。
3. **看草稿**：返回的 PNG 模型自己能看到。检查重叠、截断、断线、连线穿节点、
   标签不可读；**最多修两轮**，每轮只改有问题的部分。
4. **交付**：最终按用户需要的格式给（PNG 截图 + `.drawio` 可编辑源；需要矢量给 SVG/PDF）。
   报告里同时给出可编辑源与导出的相对路径，不要只发图片。

## 从来源生成（一期支持）

| 输入 | 做法 |
| --- | --- |
| 代码目录（Py/JS/TS/Go/Rust，类图用 pyclasses） | `build` + `source=<目录>` + `source_type=python` 等；模块多时加 `group=true` |
| Terraform（目录或 .tf） | `build` + `source_type=terraform`；资源/依赖关系自动成图 |
| SQL DDL | `build` + `content=<DDL>` + `source_type=sql`（ERD） |
| OpenAPI / Swagger | `build` + `content`/`source` + `source_type=openapi` |
| AsyncAPI | `source_type=asyncapi` |
| Protobuf / GraphQL | `source_type=proto` / `graphql` |
| Kubernetes / Docker Compose / CI（GitHub Actions、GitLab CI） | `source_type=kubernetes` / `compose` / `ci` |

同一份源改完要更新图时：重新 `build` 到新文件名，再与旧图对比；需要保留人工布局时
先读 `docs` 的 `diagram-ir.md`（`sync`/`reconcile` 工作流，本版暂未暴露为动作，
可让超管用服务端脚本处理）。

## 图纸质量

- 每张图先定**结构**再定**样式**；15 个节点以上优先自动布局（`group`/`direction`），
  不要手摆一堆坐标。
- 云的形状名不要猜：用 `shapes` 查官方 style，再写进 XML。
- 边必须有 geometry；稳定语义 ID；不要复用保留 ID `0`/`1`。
- 关系要可追溯：代码/配置导入的关系标 provenance；推断出来的关系要说明是推断。
- 图里不要放密钥、内网地址、真实人名等敏感信息（`.drawio` 与 PNG 都会外发）。

## 参考文档

`docs` 动作按需读，不要一次读太多：

| 主题 | 文档 |
| --- | --- |
| 图类型与选型 | `diagram-types.md` |
| 手写 XML 规范 | `xml-authoring.md` |
| Diagram IR / 多视图 / sync | `diagram-ir.md` |
| 自动布局 | `autolayout.md` |
| 形状与图标（AWS/Azure/GCP/AI） | `shapes.md`、`databricks.md` |
| 样式预设 | `style-presets.md` |
| 语义工作流（query/test/review/what-if/story） | `semantic-workflows.md` |
| 排障（导出/渲染） | `troubleshooting.md` |
| 安全与脱敏 | `security.md` |

## 注意

- 本工具**仅超级管理员可用**；普通成员触发时会被权限层拒绝，不要教用户绕过。
- 渲染器是随包的 draw.io 32.6.1，不需要联网、不需要用户安装；渲染在无窗口模式下运行。
- 所有产物路径都相对技能目录（`out/...`）；不要请求授予任意目录写权限，
  需要把文件放到指定位置时由超管另行处理。
