# draw.io 图表使用说明（实操篇）

> 面向**使用者**：怎么让 ATRI-X 画出能改的架构图、怎么导出图片、出问题怎么排。
> 设计取舍、隔离原理、体积与构建细节见 [draw.io 图表能力](diagram.md)（概览篇）。

一句话：**对 ATRI-X 说你要什么图，它会生成可编辑的 `.drawio`，并顺手导出 PNG/SVG/PDF
发到聊天里**——不需要装 draw.io，也不需要 Python 环境（安装版 / 便携版已内置渲染器）。

## 1. 能做什么

| 你想做的事 | 工具动作 | 需要的输入 |
| --- | --- | --- |
| 把**代码目录**画成模块依赖图 / 类图 | `build` | 目录路径 + 语言（`python`/`javascript`/`go`/`rust`/`pyclasses`） |
| 把 **Terraform** 画成资源/部署图 | `build` | `.tf` 或目录，`source_type=terraform` |
| 把**建表 SQL** 画成 ERD | `build` | SQL 文本或 `.sql` 文件，`source_type=sql` |
| 把 **OpenAPI/Swagger** 画成接口图 | `build` | JSON/YAML，`source_type=openapi` |
| 把 **AsyncAPI / Proto / GraphQL / K8s / Compose / CI** 画成图 | `build` | 对应文件或文本，见 §5 源类型表 |
| 按你的**描述**手绘一张架构图 / 流程图 / 时序图 / UML | `create` | Diagram IR JSON 或 draw.io XML（模型会自己写） |
| 把已有 `.drawio` 导出成图片 | `export` | `.drawio` 路径 + `format` |
| 检查图有没有重叠/断线/连线穿节点 | `validate` | `.drawio` |
| 看图的节点数、边数、页面、无障碍文本 | `inspect` | `.drawio` 或 IR JSON |
| 查某类节点、某个 owner 的节点、两节点间有没有通路 | `query` | `.drawio` + 过滤条件 |
| 查 draw.io 官方形状 style（避免模型瞎编形状名） | `shapes` | 关键词，如 `aws lambda` |
| 读技能内置的 20 篇参考文档（图类型、XML 规范、IR、布局…） | `docs` | 文档名（省略则列出全部） |
| 确认图表能力是否就绪（渲染器版本等） | `doctor` | — |

**产物永远是两份**：可编辑的 `.drawio` 源 + 你要的图片（默认 PNG）。只想要源文件时
把 `format` 设成 `drawio`。

## 2. 使用前提

1. **版本**：ATRI-X 1.1.5 及以上。
2. **身份**：`drawio` 是**超管专属工具**（清单 `superadmin_only = true`）。普通成员
   **连工具都看不到**，群聊里配 `tool_allowlist` 也不会放开。
3. **就绪状态**：安装版 / 便携版首次启动会自动把技能装进配置目录，并直接使用随包的
   draw.io 32.6.1 渲染器；源码树需要手动启用（见 §11）。
4. 想知道当前能不能出图，直接对 ATRI-X 说：

   > 跑一下 drawio 的 doctor，看看渲染器正常吗。

   `render_available: true` 且 `drawio_version: 32.6.1` 就说明能导出图片；否则仍能生成
   `.drawio`，只是不出图片（见 §10）。

## 3. 快速上手：直接说需求

不用记参数，用日常说法即可，模型自己决定调哪个动作、传什么参数。

### 例 1：模块依赖图（最常用）

> 把当前项目画成模块依赖图，只要架构层，按目录分组，横向排版，导出 PNG。

模型会 `build` + `source_type=python`（或按语言）+ `group=true` + `direction=LR`，
返回 `.drawio` 和一张 PNG。

### 例 2：Terraform 部署图

> 把 `D:\myproj\infra` 里的 Terraform 画成部署图，导出 PNG，标题叫「生产环境」。

### 例 3：贴一段 SQL 画 ERD

> 这是我贴的建表 SQL：`CREATE TABLE users (...); CREATE TABLE orders (...);`
> 画成 ERD，要看到外键关系。

### 例 4：OpenAPI 接口图

> 把 `D:\myproj\docs\openapi.yaml` 画成接口调用关系图，按服务分组。

### 例 5：已有图导成矢量图 / 高清图

> 把 `docs/arch.drawio` 导出成 SVG，所有页都要。
>
> 把 `docs/arch.drawio` 导出成 2 倍分辨率 PNG，透明背景。

### 例 6：检查一张图

> 校验 `docs/arch.drawio`，告诉我有没有重叠、断线、连线穿节点。

### 例 7：查依赖通路

> 在 `docs/arch.drawio` 里查一下从 `internet` 到 `orders-db` 有没有通路，中间经过谁。

### 例 8：手绘一张（不给源码）

> 画一张「用户 → API 网关 → 订单服务 → 数据库」的架构图，订单服务下面标出 Redis 缓存，
> 用 draw.io 的官方 AWS 形状。

这种情况模型会先用 `shapes` 查官方 style，再用 `create` 写 Diagram IR 或 XML。

## 4. 动作与参数速查

工具只有一个：`drawio`；必填 `action`，其余按动作取用。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `action` | 枚举 | `create` / `build` / `export` / `validate` / `inspect` / `query` / `shapes` / `docs` / `doctor` |
| `source` | 字符串 | 文件或**目录**路径（绝对路径，或相对技能目录）。代码目录导入必须用它 |
| `content` | 字符串 | 内联内容：SQL / OpenAPI / Terraform / AsyncAPI / Proto / GraphQL / IR JSON / draw.io XML |
| `source_type` | 枚举 | `auto`（默认，按扩展名/文件头猜）、`ir`、`graph`、`drawio`、`terraform`、`sql`、`openapi`、`asyncapi`、`proto`、`graphql`、`kubernetes`、`compose`、`ci`、`python`、`javascript`、`js`、`go`、`rust`、`pyclasses` |
| `format` | 枚举 | `png`（默认，同时给 `.drawio`）、`svg`、`pdf`、`drawio`（只要可编辑源） |
| `output` | 字符串 | 输出文件名（不含扩展名），默认 `diagram`；写在技能目录 `out/` 下 |
| `title` | 字符串 | 图标题，写进页面名/元数据 |
| `group` | 布尔 | `build` 时按目录/模块把节点聚合成分组（大仓库强烈建议开） |
| `direction` | 枚举 | `TB` 纵向（默认）/ `LR` 横向 |
| `views` | 字符串 | 一次出多视图：`executive,system,deployment,dataflow,security` 的逗号子集 |
| `scale` | 数字 | 导出缩放，如 `2` 表示 2x（PNG 更清晰） |
| `width` / `height` | 整数 | 导出像素尺寸（**不要和 `scale` 同时给**） |
| `border` | 整数 | 图四周留白（像素） |
| `transparent` | 布尔 | PNG 透明背景 |
| `pages` | 字符串 | 多页图：`all` 全部 / `3` 第 3 页 / `1-3` 范围 |
| `keyword` | 字符串 | `shapes` 的检索词 |
| `doc` | 字符串 | `docs` 要读的参考文档文件名（可省略 `.md`） |
| `query_kind` | 字符串 | `query`：按节点 kind 过滤 |
| `query_owner` | 字符串 | `query`：按 owner 过滤 |
| `from_node` / `to_node` | 字符串 | `query`：查两节点间的有向路径 |

约定与边界：

- **`source` 与 `content` 二选一**：`source` 引用磁盘上已有的内容；`content` 用于聊天里
  直接贴的文本。都给了时以 `content` 为准（`build` 的代码目录导入除外，必须用 `source`）。
- **`content` 只适合文本源**（SQL/OpenAPI/Terraform/IR/XML）；代码目录必须 `source`。
- `export` 只支持 `png` / `svg` / `pdf`；`format=drawio` 表示不渲染、只交付源文件。
- **单次调用上限 300 秒**（技能清单 `timeout_seconds`），其中单张图渲染最长 180 秒。
  超大图/多页图请拆页导出。
- 可选依赖 Graphviz（`dot`）只影响自动布局兜底；安装版/便携版不依赖它。

## 5. 输入源类型与导入器

`source_type=auto` 时按扩展名/文件头猜；**源有歧义时显式指定**更稳。

| 输入 | 推荐参数 | 说明 |
| --- | --- | --- |
| Python 目录 | `source=<目录>`、`source_type=python` | 模块/库/入口点关系；大仓库加 `group=true` |
| JavaScript / TypeScript 目录 | `source_type=javascript`（或 `js`） | 同上 |
| Go 目录 | `source_type=go` | package 依赖 |
| Rust 目录 | `source_type=rust` | crate/module 依赖 |
| Python 类图 | `source_type=pyclasses` | 类、继承、组合关系 |
| Terraform | `source_type=terraform`，可给目录 | 资源与引用关系自动成图 |
| SQL DDL | `content=<DDL>`、`source_type=sql` | 表/列/外键 → ERD |
| OpenAPI / Swagger | `source=_/content`、`source_type=openapi` | 路径、服务、依赖 |
| AsyncAPI | `source_type=asyncapi` | 消息通道 |
| Protobuf | `source_type=proto` | service/message |
| GraphQL | `source_type=graphql` | type/query |
| Kubernetes YAML | `source_type=kubernetes` | 工作负载/服务 |
| Docker Compose | `source_type=compose` | 服务/依赖 |
| CI 配置 | `source_type=ci` | GitHub Actions / GitLab CI 步骤 |
| Diagram IR / graph JSON | `source_type=ir` / `graph` | 语义模型，见 §6 |
| 手写 draw.io XML | `source_type=drawio` | 原样收下并导出 |

导入器会把源码里的**字符串标签**带进图里（服务名、表名、路径、注释等）。导入前请确认
这些内容可以外发，见 §8。

## 6. 让模型手绘：Diagram IR 最小示例

`create` 的推荐输入是 **Diagram IR v1**（语义与坐标分离，布局交给工具）：

```json
{
  "schema": "drawio-skill/diagram-ir/v1",
  "metadata": {"title": "Checkout"},
  "nodes": [
    {"id": "api", "label": "API 网关", "kind": "service", "properties": {}},
    {"id": "orders", "label": "订单服务", "kind": "service",
     "properties": {"owner": "orders-team"}},
    {"id": "db", "label": "订单库", "kind": "database", "properties": {}}
  ],
  "edges": [
    {"id": "e1", "source": "api", "target": "orders", "label": "HTTPS",
     "kind": "sync", "properties": {}},
    {"id": "e2", "source": "orders", "target": "db", "label": "SQL",
     "kind": "sync", "properties": {}}
  ],
  "views": []
}
```

要点：

- `id` 是稳定语义身份，**不要用 draw.io 保留 id `0` / `1`**；
- `kind` 决定形状（`service` / `database` / `queue` / `module` 等），`properties`
  里的 `owner` / `environment` / `trust_boundary` 会被多视图和 `query` 使用；
- `provenance` 可记录来源（文件/行号），生成后仍保留为单元格属性；
- 多视图用 `views` 参数一次生成（`executive,system,deployment,dataflow,security`）。
  元数据不足时某视图会**回退**到全量模型，并在结果里给出 `fallback` 与 `hint`，不会静默糊弄。

更完整的 IR、多视图与增量同步（`sync` / `reconcile`）见 `docs` 动作里的
`diagram-ir.md`；XML 手写规范见 `xml-authoring.md`。

## 7. 产物在哪、长什么样

- **位置**：`<配置目录>/ATRI-X/skills/agent/drawio_skill/out/`。
  Windows 默认即 `%APPDATA%\ATRI-X\skills\agent\drawio_skill\out\`。
- **返回**：图片（PNG）走媒体中心，聊天里能直接看到，模型自己也会看图做一次视觉自检；
  `.drawio` / SVG / PDF 作为文件段返回。路径都是相对技能目录的 `out/...`。
- **命名**：默认 `diagram`，同名不覆盖（自动加时间后缀）；可用 `output` 指定。
- **清理**：`out/` 只保留最近 **60** 个文件，旧的按修改时间自动删除。
- **私有渲染目录**：`out/.tmp/`、`out/.drawio-profile/` 是临时输入与 draw.io 隔离配置，
  不要手工往里放东西。
- 技能**只写自己目录下的 `out/`**。需要把产物落到别的位置时，由超管在服务器上另存。

## 8. 权限与安全

- **仅超管**：非超管看不到工具；不要在群聊里试图给普通成员开放。
- **图会外发**：PNG / `.drawio` 都随消息发出去，图里的节点标签、属性、代码路径都算内容。
  不要把密钥、token、内网地址、真实人名、客户数据写进节点标签或 `properties`；
  从源码/配置导入前，先确认这份仓库可以外发。
- **隔离**：渲染时只把 `APPDATA` / `LOCALAPPDATA` / `TEMP` / `TMP` 收容到技能私有
  profile，不读写宿主机上用户自己的 draw.io 配置；不联网、不自动更新。
- **失败不阻断**：渲染器缺失或渲染失败时，工具仍返回 `.drawio` 并说明原因，不会让
  机器人报错退出。

## 9. 常见需求 → 一句话对照

| 需求 | 可以直接说 |
| --- | --- |
| 模块依赖 | 「把这个目录画成模块依赖图，按目录分组」 |
| 部署图 | 「把 infra 下的 Terraform 画成部署图」 |
| 数据库 | 「这段建表 SQL 画成 ERD，标出外键」 |
| 接口图 | 「openapi.yaml 画成接口关系图」 |
| K8s / CI | 「把这个 deployment.yaml / GitHub Actions 画成图」 |
| 手绘架构 | 「画一张用户→网关→服务→库的架构图，用官方 AWS 形状」 |
| 高清图 | 「导出 2 倍 PNG，透明背景」 |
| 矢量图 | 「导出 SVG / PDF，所有页」 |
| 只想要源文件 | 「只给 .drawio，不用出图」 |
| 质检 | 「校验这张图并修复重叠/断线」 |
| 查结构 | 「列一下图里有哪些服务、哪些边」 |
| 查通路 | 「查 internet 到 db 的路径」 |
| 找形状 | 「查 draw.io 官方 aws lambda 形状」 |
| 读文档 | 「读技能里的 diagram-types.md」 |

## 10. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 工具列表里没有 `drawio` | 不是超管（发现阶段就不可见）；或技能未安装：安装版重装，源码树跑 §11 的 bootstrap |
| `doctor` 显示 `render_available: false` | 内置渲染器缺失/路径失效：安装版重新安装；源码树设 `ATRIX_DRAWIO` 或放 `packaging/vendor/drawio/` |
| 只返回 `.drawio`，没有图片 | 渲染器不可用（同上），或渲染失败——结果文本里会写原因；源文件仍可用 |
| `未找到 draw.io 渲染器` | 同上；`.drawio` 可拿桌面版 draw.io 打开 |
| `renderer process gone (crashed)` | 渲染器目录不完整（最常见是删了 `ffmpeg.dll`），用官方解包目录重做 |
| 渲染超时（180 秒） | 图太大/页太多：拆页（`pages`）、减少节点、加 `group=true` 聚合 |
| 图里中文变方框 | 渲染用系统字体；Windows 正常。Linux 容器不提供渲染 |
| 图很乱、线穿节点 | 让模型用 `validate` 检查并修复（最多两轮）；或开 `group` / 换 `direction` |
| 无窗口渲染失败、服务跑在 Session 0 | Electron 需要交互式 Windows 会话；服务方式部署时只能出 `.drawio`，其余动作不受影响 |
| 导入后图里出现敏感字符串 | 立即删除产物并按安全流程处理；下次导入前先脱敏源 |
| 同名产物找不到 | `out/` 最多留 60 个，旧文件会被清理；需要留档请另存 |

## 11. 进阶：源码树 / 手工调用

### 11.1 源码树启用技能

冻结产物（安装版/便携版）首次启动自动安装；**源码树默认不装**（避免开发测试写真实配置）：

```bash
# 装进默认配置目录（Windows：%APPDATA%\ATRI-X），生成 ATRIX_SKILL.toml
python -m atri_x.diagram.bootstrap --source

# 只看状态 / 指定配置目录 / 强制重装
python -m atri_x.diagram.bootstrap --status
python -m atri_x.diagram.bootstrap --source --config-dir tmp_cfg_test
python -m atri_x.diagram.bootstrap --source --force
```

### 11.2 源码树给渲染器

源码树不内置 382 MB 的渲染器，二选一：

```powershell
# 方式一：环境变量指向 draw.io.exe 或其解包目录
$env:ATRIX_DRAWIO = "D:\tools\draw.io-32.6.1-windows"

# 方式二：把官方解包目录放到仓库的构建材料目录（该目录不入库）
#   packaging\vendor\drawio\draw.io.exe
```

### 11.3 手工跑一次（等价于工具调用）

源码树里直接执行技能适配器，stdout 是 JSON 信封：

```bash
python drawio_pack/skills/drawio-skill/atrix_cli.py --action doctor
python drawio_pack/skills/drawio-skill/atrix_cli.py --action build \
  --source ./infra --source-type terraform --group --format png --output infra-map
python drawio_pack/skills/drawio-skill/atrix_cli.py --action export \
  --source ./docs/arch.drawio --format svg --pages all
python drawio_pack/skills/drawio-skill/atrix_cli.py --action validate \
  --source ./docs/arch.drawio
```

冻结产物里由控制台版 exe 代跑（技能清单里生成的命令就是这一条）：

```powershell
atri-x.exe -m atri_x.diagram.runner --skill-dir "<配置目录>\ATRI-X\skills\agent\drawio_skill" --action doctor
```

### 11.4 一期没注册的上游能力

上游 drawio-skill 3.4.2 的 `sync`（保留手工布局的增量更新）、`views`、`test`、`review`、
`whatif`、`story`、`transform`（PPTX、Mermaid、动画等）脚本**都随包**，但暂未注册成工具
动作。需要这些能力时由超管在服务器上直接跑上游脚本，或等后续版本补动作；不要指望
在聊天里一句「同步一下」就自动走 `sync`。

## 12. 相关文档

- [draw.io 图表能力（概览篇）](diagram.md)：权限、隔离原理、体积、构建与维护。
- `docs` 动作内置参考：`diagram-types.md`（图类选型）、`xml-authoring.md`（手写 XML）、
  `diagram-ir.md`（IR/多视图/同步）、`autolayout.md`（布局）、`shapes.md`（形状与图标）、
  `troubleshooting.md`（上游排障）、`security.md`（安全与脱敏）。
- [发行说明 1.1.5](release-notes/1.1.5.md)：本次新增能力的变更记录。
- [Windows 桌面端与原生应用](windows-app.md)：安装版/便携版的使用与部署方式。
