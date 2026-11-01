# 性能与并发（多人生图 / 多会话 / 多请求）

先回答最常见的问题：**ATRI-X 不是"一个线程一次只干一件事"**。服务进程是一个
`asyncio` 事件循环，所有网络调用（LLM、生图、平台收发）都是协程并发；真正耗 CPU
的图片压缩、哈希、编解码走独立线程池。所以"多人同时说话 / 同时出图 / 同时收发"
本来就是并行处理的——需要调的是**并发上限、排队策略和可观测性**，而不是把程序
改成多线程。桌面端 1.1.8 新增的「性能」选项卡就是干这个的。

> GIL 说明：CPython 的线程不能把纯 Python 计算真正跑满多核；本项目的瓶颈在
> `await`（等上游 API、等平台），所以协程并发才是有效手段。线程池只用来隔离
> 阻塞 IO / 图片编解码，不是"多线程加速"。

## 哪些事情真的并行

| 事情 | 并行方式 | 默认上限 | 配置位置 |
| --- | --- | --- | --- |
| 不同群 / 不同私聊的回合 | 每条入站消息一个 `asyncio.Task` | `[ops] max_concurrent_turns = 10` | `app.toml` |
| 同一回合里的工具调用 | 请求管理器按 `tool` 限流 | `max_concurrent_tools = 10` | `app.toml` |
| 同一回合里一次生多张图 | `asyncio.gather` + 最多 4 路 | 4 张/批 | 代码内置 |
| 跨回合同时生图 | 全局生图限流器 | `[image_generation] max_concurrent = 0`（不限） | `services.toml` |
| LLM 真实请求 | provider 级闸门 | `provider_max_concurrent = 10` | `app.toml` |
| QQ 入站预处理（@ / 引用 / 转发取数） | 有界 worker 池 | `preprocess_workers = 4` | `app.toml` |
| 图片压缩/解码 | 每 loop 10 槽 + `atrix-media` 线程池 10 | 10 | 代码内置 |
| 视觉兜底识图 | 独立 runner | 10（1.1.8 起可配） | `services.toml` `[vision] max_concurrent_jobs` |
| 语音转写 | 独立 runner | `[asr] max_concurrent = 2`（示例值） | `services.toml` |
| DSH 深度路由 | 一条 ACP 连接上多会话并发 | `[agent.dsh] max_concurrent_prompts = 2` | `app.toml` |

**同一群默认每人独立会话**（`thread-mode per-user`），所以群里两个人同时提问是
两个并行回合。只有显式 `/grouppolicy thread-mode group` 或打开连续消息合并窗口
（`merge_window_ms > 0`）时，同一个 Session 才**按回合串行**——这是语义要求，
不是性能 bug。

## 桌面端「性能」选项卡

「性能」页（`atri_x/gui/tabs/performance.py`）三块：

1. **并发模式**：`省资源（单用户）` / `标准（默认）` / `高并发（多人/多群）` 三个预设。
   点预设只填值，点「保存」才写盘；保存后需要**重启后台服务**（这些是启动期读取的）。
2. **并发上限**：逐个调 turn / tool / hook / provider / 排队 / 生图 / QQ 预处理 worker。
3. **并发实况**：从后台 `/metrics` 读活跃回合数、各 kind 排队数、并发拒绝/队满/超时计数、
   生图闸门占用、provider 限速统计，以及 **DSH 深度路由**的并发占用/忙拒/卡死/冷启动/
   亲和命中；可自动刷新（5 秒）。后台服务没起时只显示提示，不弹窗。

预设只是"一组数字"，没有隐藏开关；每档具体值见下表。

| 旋钮 | 省资源 | 标准（出厂默认） | 高并发 |
| --- | ---: | ---: | ---: |
| `max_concurrent_turns` | 2 | 10 | 32 |
| `max_concurrent_tools` | 2 | 10 | 32 |
| `max_concurrent_hooks` | 2 | 10 | 16 |
| `provider_max_concurrent` | 2 | 10 | 16 |
| `queue_max_size` | 8 | 20 | 64 |
| `queue_wait_timeout_seconds` | 20 | 30 | 60 |
| `[image_generation] max_concurrent` | 1 | 0（不限） | 8 |
| `[image_generation] queue_size` | 2 | 0 | 16 |
| `preprocess_workers` | 2 | 4 | 8 |
| `preprocess_queue_size` | 64 | 256 | 512 |
| `high_priority_workers` | 1 | 1 | 2 |

选择建议：

- 只有自己用：`省资源`。同时占用的上游连接最少，429 也最少。
- 十人级群 / 多群混用：`标准`。出厂默认就是按这个量级定的。
- 几十人同时用、且上游额度充足：`高并发`。**必须**同步把 `provider_max_concurrent`
  提到上游允许的并发，否则压力会变成 HTTP 429（provider 闸门就是为此存在的）。
- 上游有严格速率限制时，反而该用「省资源」并把 `queue_max_size` 调大：让请求排队，
  而不是同时打过去再一起被 429。

## 排队与拒绝的语义

- `max_concurrent_*` 是**同时活跃数**，不是 QPS。0 表示不限。
- 超限时只有两种结果：该 kind 在 `queue_wait_kinds` 里且 `queue_max_size > 0` → 入队等待；
  否则立刻拒绝（`ErrConcurrencyLimit`）。
- 队列还有三层上限：全局 `queue_max_size`、单用户 `queue_max_per_user`、
  单 `平台:scope` `queue_max_per_scope`；任意一层满都返回 `ErrQueueFull`。
- 排队最长等 `queue_wait_timeout_seconds`，超时返回 `ErrQueueTimeout`。
- provider 闸门（1.1.8 修复）：`provider_queue_max_size > 0` 时是有界队列，**队满立即拒绝**；
  设为 0 走旧的信号量等待语义（等 `provider_wait_timeout_seconds` 再拒绝）。

## 怎么观测

- 桌面端「性能 → 并发实况」：最方便，字段与下面一致。
- `GET /metrics`（需要 `ATRIX_OPS_TOKEN`）：`tasks` 段含
  `active[].kind / session_id / fair_key / started_at`、`count_by_kind`、`queued_by_kind`、
  `queue_busy_rejected / queue_full_rejected / queue_timeout_rejected / queue_cancel_rejected`、
  `oldest_queued_wait`、`queue_average_wait_ms`；`image_limit.active/waiting`；`rate_limit`。
- `GET /tasks`：只返回 `tasks` 段（更轻）。

## DSH 深度路由的并发（1.1.8）

DSH 是"第二个后端"：只有任务够复杂时才接管一轮，普通聊天仍走原生链路。1.1.7 及以前
`DshRuntime` 用一把进程级锁把所有路由轮次串成 **1 并发**，第 2 个复杂任务只能排到
180 秒超时；1.1.8 改成内核本来就支持的"一条连接上多会话并发"：

| 旋钮 | 默认 | 说明 |
| --- | ---: | --- |
| `[agent.dsh] max_concurrent_prompts` | 2 | 同时运行的 DSH prompt 上限；1 = 旧的串行语义 |
| `[agent.dsh] busy_wait_seconds` | 2 | 槽满最多等多久；超时直接回退原生（不排队） |
| `[agent.dsh] stall_timeout_seconds` | 90 | 单轮无任何事件就取消（须小于 `timeout_seconds`） |
| `[agent.dsh] idle_timeout_seconds` | 900 | 空闲时会回收内核进程（省约 170MB） |
| `[agent.dsh] prewarm` | true | 启动后台预热（首轮省 1.7~1.8 秒） |
| `[agent.dsh] session_affinity` | true | 同一聊天连续路由时复用会话、只发增量 |

要点：

- DSH 轮次与原生链路**共用** `[ops].provider_max_concurrent` 闸门（provider 名取凭据
  计划里的 provider），并发化不会偷偷把上游打成 429；
- 忙时的结果不是"排 3 分钟"，而是**立刻回退原生**；审计里是 `dsh_route_busy`，
  不记熔断失败、不动预算；
- 实测（本机内置内核 0.2.0-rc.2）：冷启动 1.7~1.8 秒、`session/new` 10~40ms、
  4 个会话并发新建合计 20ms、4 个会话只多约 2MB 内存；贵的是 prompt 本身，不是进程；
- 指标在 `/metrics` 的 `dsh` 段：`active / maxConcurrent`、`stats`（`admitted` /
  `busy_rejected` / `completed` / `failed` / `stalled` / `cold_starts` /
  `idle_closes` / `prewarmed`）、`affinity`（条目/命中）。

## 已知瓶颈（1.1.8 的边界）

- **同步 SQLite 在事件循环里**：存储层是单连接 + 一把锁、全同步调用，回合越多，
  这条路径越容易互相等。缓解：不要把 `max_concurrent_turns` 调到几百；SQLite 写入
  本身不是并发瓶颈，排队才是。
- **`[budget_limits].chat_hard_limit = true`** 会开全局锁 + 全量扫描 + 落盘，
  高并发下明显变慢。要精确预算就接受这个代价，否则保持 `false`（软限制）。
- **QQ 预处理 4 worker**：引用/转发/艾特密集时，消息会在进入模型前排队。调大
  `preprocess_workers` 能提高入站吞吐，但每个 worker 都会打平台 API，注意别把
  OneBot 侧打限流。
- **媒体线程池 10 槽**：十几个回合同时存大图会互相排队，属预期背压。
- **共享会话（thread-mode group）**：全群严格串行，这是会话语义，不是配置能绕开的；
  要并行就让群保持默认 `per-user`。
- **DSH 并发别贪高**：`max_concurrent_prompts` 调大只提升"同时有多少复杂任务交给
  DSH"，不会让单轮变快；真正约束是上游 provider 的额度（`provider_max_concurrent`）
  与内核单进程事件循环。建议先看 `/metrics` 的 `dsh.stats.busy_rejected` 再决定。

## 验证

```bash
python -m unittest tests.test_gui_perf tests.test_agent_provider_limit \
    tests.test_vision_concurrency tests.test_dsh_scheduler -v
```
