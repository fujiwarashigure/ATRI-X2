# ATRI-X 代码地图（读的顺序）

> **本文件只写"怎么读"和"为什么这样分"**（判断类知识）。逐模块职责、依赖、入口、测试反查
> **全部由代码生成**，不要在这里维护第二份：
>
> - 模块 → 职责 / 公共符号 / 行数 / 测试：`devdocs/generated/module-map.md`
> - 域依赖图 + 依赖矩阵：`devdocs/generated/deps.mmd`
> - 进程入口 / 命令 / 工具 / 平台 / Hook / GUI / 任务入口表：`devdocs/generated/entrypoints.md`
> - 模块 ↔ 测试双向表（含"无覆盖"清单）：`devdocs/generated/test-map.md`
> - 模块 → 上游 Go 文件：`devdocs/generated/go-map.md`
> - 域入口（人读）：`atri_x/<域>/README.md`（10 个高价值域，生成 + 人工补）
>
> 改代码后跑 `python scripts/gen_index.py`；手改生成物会被 `python scripts/gen_index.py --check` 抓到。

## 1. 先读这四条主线

1. **启动 → 装配**：`python -m atri_x` → `atri_x/__main__.py` → `atri_x/launcher/cli.py`（参数）
   → `atri_x/app/app.py`（运行模式与 `Run`）→ `atri_x/app/runtime.py`（分阶段装配：
   Environment → Foundation → Models → Platforms → Runtime → Integrations）。
   **装配顺序有依赖**，改顺序容易拿到半成品。
2. **一条消息 → 一轮对话**：平台适配器（`atri_x/platform/`）→ `atri_x/agent/`（取上下文、
   调模型、跑工具、落库）→ `atri_x/delivery/`（输出与发送）。中间的类型契约在
   `atri_x/platform/platform.py`、`atri_x/delivery/delivery.py`。
3. **一次工具调用**：模型发起 → `atri_x/toolrun/`（工具视图、命名解析、风险确认、执行前授权复核）
   → `atri_x/tool/builtin/` 的实现 → 结果回灌。**权限判定看两处**：发现阶段
   （`atri_x/tool/discover.py`）与执行阶段（`atri_x/toolrun/runner.py`），两道都过才真的执行。
4. **配置 → 行为**：`atri_x/config/loader.py` 加载 → `atri_x/config/app_sections.py` 分段结构与
   校验 → 各域消费。`app.toml` 改动需要重启；`state.toml` 支持热加载。

## 2. 契约层（唯一出处，先认这 7 个）

| 契约 | 出处 | 约束 |
| --- | --- | --- |
| 异常哨兵 | `atri_x/errors.py` | 全仓唯一 |
| 工具契约（Tool / Info / Result / Registry） | `atri_x/tool/tool.py` | 全仓唯一 |
| LLM 契约（消息 / schema / 流式分片） | `atri_x/llm/llm.py` | 全仓唯一 |
| 身份与权限（Actor / Policy / Role / RiskLevel） | `atri_x/security/` | 全仓唯一 |
| 存储模型（Session / Message / …） | `atri_x/storage/models.py` | 全仓唯一 |
| 投递契约（Output / Receipt / Target） | `atri_x/delivery/delivery.py` | 全仓唯一 |
| 配置结构 | `atri_x/config/` | 全仓唯一 |

这 7 个是"枢纽"：被几十到两百多个模块依赖（实测最大 fan-in 250）。跨域使用它们时**只从枢纽本身
import**，不要深 import 它的子模块（`atri_x/devtools/boundaries.py` 会把"绕开 facade"计入预算）。
详见 [`devdocs/adr/0004-structural-convergence.md`](adr/0004-structural-convergence.md)。

## 3. 域切片（改之前先看所在域）

| 域 | 一句话 | 域入口 |
| --- | --- | --- |
| `agent/` | 一轮对话的编排与 slash 命令 | `atri_x/agent/README.md` |
| `tool/` + `toolrun/` | 工具契约、内置工具、执行闸门 | `atri_x/tool/README.md` |
| `platform/` | 外部协议 → 内部事件的翻译层 | `atri_x/platform/README.md` |
| `gui/` | 桌面端界面与托盘 | `atri_x/gui/README.md` |
| `storage/` | 领域模型 + SQLite 实现与迁移 | `atri_x/storage/README.md` |
| `config/` | 配置加载、校验、默认资产与热加载 | `atri_x/config/README.md` |
| `hook/` | 事件钩子与插件协议 | `atri_x/hook/README.md` |
| `llm/` | 模型协议适配与流式 | `atri_x/llm/README.md` |
| `app/` | 分阶段装配与生命周期 | `atri_x/app/README.md` |
| `media/` + `imagegen/` + `vision/` | 媒体、生图、识图 | `atri_x/media/README.md` |

其余域（`session/`、`contextmgr/`、`memory/`、`cron/`、`maintenance/`、`background/`、`atrinis/`、
`atriena/`、`atriyph/`、`character/`、`artist/`、`asr/`、`selflearning/`、`groupanalysis/`、
`groupkb/`、`research/`、`health/`、`ops/`、`redact/`、`logging/`、`signal/`、`net/`、
`shellparse/`、`utils/`）直接在 `devdocs/generated/module-map.md` 里按前缀查。

## 4. 跨切面（改任何域都可能碰到）

| 主题 | 位置 | 注意 |
| --- | --- | --- |
| 时间 | `atri_x/time_service.py`、`atri_x/timezones.py`、`atri_x/timesync/` | 业务时间只有一个来源与一个时区口径；网络校验只做观测，**绝不改系统时钟** |
| 资源采样 | `atri_x/sysinfo.py`、`atri_x/sysinfo_platform.py`、`atri_x/hardware.py`、`atri_x/gpu_usage.py` | 采集/换算在 `sysinfo`（平台采集器在 `sysinfo_platform`），硬件型号与显卡占用在外围模块；**输出只允许经 `atri_x/sysinfo_format.py`**：磁盘只出卷标签，绝不输出本机路径 |
| 安全策略 | `atri_x/security/`、`atri_x/safety/` | 风险分级与角色权限在核心；内容安全是独立可关的模块 |
| 脱敏 | `atri_x/redact/` | 日志与审计出口统一走它 |
| 日志与信号 | `atri_x/logging/`、`atri_x/signal/` | 运行 / 审计 / Atriwisp 日志分离 |
| 健康与运维 | `atri_x/health/`、`atri_x/app/ops_health.py` | `/live` 只看进程；`/ready` 还看初始化与数据目录 |
| 开发期工具 | `atri_x/devtools/` | AST 索引、渲染、复杂度与边界；**零第三方依赖**，只被 `scripts/` 使用 |

## 5. 读代码的三个建议

1. **先看域 README，再看代码**：域 README 有"职责 / 不做啥 / 易错点 / 模块清单 / 对应测试"，
   比从头翻目录快。
2. **找测试当文档**：`devdocs/generated/test-map.md` 给出每个模块被哪些测试覆盖；没覆盖的模块
   说明你只能靠调用方测试与人工推理。
3. **遇到"上游应该有"的东西**：先查 `devdocs/generated/go-map.md` 确认 Python 侧对应物，
   再查 `devdocs/porting-status.md` 确认是否"已落地但未核对"。
