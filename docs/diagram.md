# draw.io 架构图（超管专属）

ATRI-X 1.1.5 起内置**图表能力**：把代码目录、Terraform、SQL、OpenAPI、AsyncAPI、
Protobuf、GraphQL、K8s、Compose、CI 配置，或模型手写的 Diagram IR / draw.io XML
转成**可编辑**的 `.drawio`，并用**随包内置的 draw.io 32.6.1** 无头渲染成
PNG / SVG / PDF 直接发到聊天里。

和「科研研究助理」一样，这也是一个**随包 AgentSkill**（上游
[drawio-skill 3.4.2](https://github.com/Agents365-ai/drawio-skill)，MIT）：装进
配置目录后注册成一个工具 `drawio`，**只有超级管理员可见/可调用**。

## 能力一览

| 动作 | 用途 | 关键参数 |
| --- | --- | --- |
| `build` | 从代码/基础设施/接口定义生成图 | `source`（文件或目录）或 `content`（内联文本）+ `source_type`；`group`、`direction`、`views` |
| `create` | 用内联 Diagram IR / graph JSON / 手写 XML 生成图 | `content`（或 `source`） |
| `export` | 已有 `.drawio` 导出图片 | `source`、`format`（png/svg/pdf）、`scale`、`pages` |
| `validate` | 结构校验（error / warning / score） | `source` 或 `content` |
| `inspect` | 结构摘要（节点/边/页面/无障碍文本） | `source` |
| `query` | 按 kind/owner 过滤，或查两节点间有向路径 | `source` + `query_kind`/`query_owner`/`from_node`/`to_node` |
| `shapes` | 检索 draw.io 官方形状 style（避免模型臆造） | `keyword` |
| `docs` | 列/读技能内置参考文档 | `doc`（省略则列出 20 篇） |
| `doctor` | 自检：渲染器版本、Graphviz、技能目录 | — |

一期的导入器（`source_type`）覆盖：`terraform`、`sql`、`openapi`、`asyncapi`、
`proto`、`graphql`、`kubernetes`、`compose`、`ci`，以及代码目录
`python` / `javascript` / `go` / `rust` / `pyclasses`。`auto` 会按扩展名/文件头猜。

## 在聊天里怎么用

不需要用户敲命令，直接说需求即可（工具由模型决定是否调用）：

> 帮我画一张这个项目的模块依赖图，只要架构层。
>
> 把 `D:\myproj\infra` 里的 Terraform 画成部署图，导出 PNG。
>
> 这是我贴的建表 SQL：…… 画成 ERD。

产物**同时**返回可编辑源与图片：模型会拿到 `out/*.drawio` 与 `out/*.png` 的媒体
引用（PNG 走媒体中心发到聊天，模型自己也能看到图并做视觉检查）。SVG / PDF /
`.drawio` 以文件形式返回。需要把文件落到指定目录时，由超管另行处理——技能只写自己
目录下的 `out/`。

## 权限与安全边界

- **仅超级管理员**：清单里 `superadmin_only = true`，非超管**连工具 schema 都拿不到**；
  群聊 `tool_allowlist` 配过也不会对普通成员放开。
- 图的内容会随 PNG/`.drawio` 外发，**不要把密钥、内网地址、真实人名等敏感信息写进
  节点标签或 properties**；导入器会把源码里的字符串带进图里，导入前确认仓库可外发。
- 所有产物落在 `<配置目录>/ATRI-X/skills/agent/drawio_skill/out/`，只保留最近
  60 个文件；渲染 profile（`out/.drawio-profile`）用于把 draw.io 的用户数据目录
  收容到技能目录内，不会读写宿主机上用户自己的 draw.io 配置。

## 内置渲染器（无需安装 draw.io）

便携版 / 安装版**随包**带一份 draw.io 桌面版 32.6.1 解包目录（`_internal/drawio_runtime/`），
渲染时以无窗口模式调用：

```text
draw.io.exe -x -f png --disable-update --disable-acceleration -o out.png diagram.drawio
```

实测（32.6.1，Windows 10 / Python 3.14.7）：

- 单页 PNG 约 3 秒，**零可见窗口**，退出码 0；
- 不写宿主用户目录；`APPDATA` / `LOCALAPPDATA` / `TEMP` / `TMP` 重定向到技能私有
  profile 后照常工作（隔离成立）；
- 中文/空格路径、并发 3 路、非法输入快速报错（不会卡住弹窗）均实测通过。

体积：官方解包 **449.9 MB**；随包时删掉 53 个语言包（只留 `en-US` / `zh-CN`），
渲染器约 **382 MB**。`ffmpeg.dll` 必须保留——缺了渲染进程会直接崩，所以是整目录
随包而不是挑文件。这个体积会体现在安装包与便携 zip 上，详见发行说明。

源码树不内置渲染器：把解包目录放到 `packaging/vendor/`（子目录名固定为 drawio，不入库），或设置
环境变量 `ATRIX_DRAWIO` 指向 `draw.io.exe` / 解包目录。

## 源码树怎么启用

冻结产物（安装版 / 便携版）首次启动会自动把技能装进配置目录；**源码树默认不自动
安装**（避免开发/测试往真实配置目录写东西），需要显式执行：

```bash
# 装进默认配置目录（Windows：%APPDATA%\ATRI-X），并生成 ATRIX_SKILL.toml
python -m atri_x.diagram.bootstrap --source

# 只看状态 / 指定配置目录 / 强制重装
python -m atri_x.diagram.bootstrap --status
python -m atri_x.diagram.bootstrap --source --config-dir tmp_cfg_test
python -m atri_x.diagram.bootstrap --source --force
```

装好后工具出现在超管的工具列表里（`/tools` 或 `@tool:drawio` 预载；工具名
`drawio`）。源码树的命令形态是 `python <技能目录>/atrix_cli.py …`，冻结产物是
`atri-x.exe -m atri_x.diagram.runner --skill-dir <技能目录> …`——由安装清单按运行形态
生成，不需要手改。

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 工具列表里没有 `drawio` | 非超管（发现阶段就不可见）；或技能未安装（跑上面 `--status`） |
| `doctor` 报 `render_available=false` | 内置渲染器缺失或路径失效：安装版重新安装；源码树设 `ATRIX_DRAWIO` |
| 导出报「未找到 draw.io 渲染器」 | 同上；技能仍会返回 `.drawio` 源文件 |
| 导出报「renderer process gone (crashed)」 | 渲染器目录不完整（最常见是删了 `ffmpeg.dll`）；用官方解包目录重做 |
| 渲染超时 | 超大图/多页图；一次最多 300 秒（技能清单 `timeout_seconds`），可拆页导出 |
| 图里中文变方框 | 渲染用系统字体；Windows 正常。Linux 容器内不提供渲染（镜像不带 Electron） |
| 装了 drawio 桌面版但没被使用 | 内置渲染器优先；清空 `ATRIX_DRAWIO` 并确认 `_internal/drawio_runtime` 完整 |
| 无窗口渲染失败、服务在 Session 0 | Electron 需要交互式 Windows 会话；服务方式部署时图表渲染不可用，其余动作不受影响 |

## 维护与构建

随包资源在仓库根的 `drawio_pack/`（**上游布局原样**：`skills/drawio-skill/{scripts,references,data,styles}`），
上游 247 个回归测试也在 `drawio_pack/tests/`，由 `tests/test_drawio_pack_upstream.py`
作为一致性门禁跑。我们只对上游脚本打了两处最小补丁：

1. `drawio_pack/skills/drawio-skill/scripts/diagramctl.py`：起导入器/transform 子进程时，冻结形态改走
   `atri-x.exe -m atri_x.diagram.runner --script <绝对路径>`（源码树行为与上游完全一致）；
2. 无（测试套件用 `PYTHONUTF8=1` 跑，不改文件）。

修改 `drawio_pack/skills/drawio-skill/` 后必须：

```bash
python scripts/gen_drawio_pack.py           # 重算 pack.json 摘要（否则 bootstrap 会判过期并重装）
python scripts/gen_drawio_pack.py --check   # CI 门禁：摘要不漂移
python -m unittest tests.test_diagram_pack tests.test_diagram_skill tests.test_drawio_pack_upstream
```

构建带内置渲染器的产物（缺渲染器会直接失败）：

```bash
python packaging/build.py --with-kernel --with-drawio D:\path\to\draw.io-32.6.1-windows --release
# 或把解包目录放到 packaging/vendor/drawio/ 后省略 --with-drawio
```

产物级核验会自动做四步：装技能 → 检查 `_internal/drawio_runtime/draw.io.exe` →
冻结进程 `doctor` 读到版本号 → 用一张两节点图真实导出 PNG。见
[packaging/README.md](../packaging/README.md)。

## 与上游的差异

- 上游把命令写进 SKILL.md 让 Agent 自己跑；ATRI-X 侧改成**结构化工具参数**，由
  `atrix_cli.py` 翻译成上游 argv，并把 stdout 统一成媒体信封。
- 一期只暴露上表的 9 个动作；上游的 `sync` / `reconcile` / `views` / `test` /
  `review` / `whatif` / `story` / `transform`（PPTX、Mermaid、动画等）脚本**都随包**，
  但暂未注册为动作——需要时由超管在服务器上直接跑（或在后续版本补动作）。
- 视觉自检闭环保留：返回的 PNG 模型可见，可对重叠/截断/连线穿节点做至多两轮修复。
