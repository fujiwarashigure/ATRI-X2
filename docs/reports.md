# 定时报告（生图量 / Token / 费用 / 磁盘 / 内存）与每日会话总结

ATRI-X 内置一个定时资源报告任务，默认每天 **9:00 和 21:00** 各发一次，内容包含：

- 生图调用量（成功 / 失败）
- DeepSeek（或指定 provider）的 Token 消耗量
- 按模型单价换算的消费金额
- 数据目录占用 + 文件系统已用/剩余
- 进程内存（RSS / 解释器内存 / asyncio task）

报告以文本消息发送给配置的超级管理员（`[security.superadmins]`）。

## 配置

`app.toml`：

```toml
[maintenance.daily_report]
enabled = true
schedule = "0 9,21 * * *"    # 每天 09:00 和 21:00
window_hours = 12            # 每次统计最近 12 小时
provider = "deepseek"        # 统计哪个 provider 的 llm_usage；留空统计全部
# platform = ""              # 指定发送平台；留空用主平台
currency = "CNY"
image_price_per_image = 0.05 # 生图每张价格（只统计成功出图）
peak_pricing = true          # 启用高峰/空闲双档
# holidays = ["2026-10-01", "2026-10-02", "2026-10-03"]
# data_root = ""             # 默认取 SQLite 所在数据目录（含 logs/）

# DeepSeek 官方价（元 / 百万 tokens）。基准字段=高峰价，offpeak_*=空闲价。
[maintenance.daily_report.prices."deepseek-v4-pro"]
input_per_million = 9.0
cache_input_per_million = 0.30
output_per_million = 27.0
offpeak_input_per_million = 4.5
offpeak_cache_input_per_million = 0.15
offpeak_output_per_million = 13.5

[maintenance.daily_report.prices."deepseek-flash"]
input_per_million = 2.0
cache_input_per_million = 0.04
output_per_million = 8.0
offpeak_input_per_million = 1.0
offpeak_cache_input_per_million = 0.02
offpeak_output_per_million = 4.0

[maintenance.daily_report.prices."deepseek-v4-flash"]
input_per_million = 2.0
cache_input_per_million = 0.04
output_per_million = 8.0
offpeak_input_per_million = 1.0
offpeak_cache_input_per_million = 0.02
offpeak_output_per_million = 4.0
```

- `enabled = false`（默认）时不注册该任务；设为 `true` 后重启生效。
- `schedule` 是 5 字段 cron 表达式：`0 9,21 * * *` = 每天 9 点和 21 点；想改成 8 点和 20 点就写 `0 8,20 * * *`。
- `window_hours` 是每次报告的统计窗口，默认 12 小时，和每天两次刚好无缝衔接。
- `provider` 为空时统计所有 provider；填 `deepseek` 时只看 DeepSeek 的 `llm_usage`。
- 未配置单价的模型只统计 token，不计金额，报告里会列出"未配置单价"。

## 单价与计价

`prices."<模型名>"` 六个字段（元 / 百万 tokens）：

| 字段 | 含义 |
| --- | --- |
| `input_per_million` | 输入 · 缓存未命中（高峰价） |
| `cache_input_per_million` | 输入 · 缓存命中（高峰价），省略则按输入价 |
| `output_per_million` | 输出（高峰价） |
| `offpeak_input_per_million` | 输入 · 缓存未命中（空闲价） |
| `offpeak_cache_input_per_million` | 输入 · 缓存命中（空闲价） |
| `offpeak_output_per_million` | 输出（空闲价） |

单次调用费用：

```text
费用 = (prompt_tokens - cache_hit_tokens)/1e6 × 输入价
     +  cache_hit_tokens              /1e6 × 缓存命中价
     +  completion_tokens             /1e6 × 输出价
```

**高峰/空闲双档**（`peak_pricing = true`，DeepSeek 官方规则）：

- 高峰：北京时间周一至周五 **09:00–12:00、14:00–18:00**；
- 空闲：其余时段（含周末），空闲价 = 高峰价的一半；
- 中国法定节假日：官方按空闲价，但 ATRI-X 没有内置节假日表，用 `holidays = ["YYYY-MM-DD", ...]` 手工列出；不列就按"工作日/周末"规则算。
- 时间戳缺失时按高峰价计（宁高不低）。

**DeepSeek 官方价（元 / 百万 tokens）**：

| 项目 | deepseek-flash | deepseek-v4-pro |
| --- | --- | --- |
| 输入 · 缓存命中 · 高峰 | 0.04 | 0.30 |
| 输入 · 缓存命中 · 空闲 | 0.02 | 0.15 |
| 输入 · 缓存未命中 · 高峰 | 2.0 | 9.0 |
| 输入 · 缓存未命中 · 空闲 | 1.0 | 4.5 |
| 输出 · 高峰 | 8.0 | 27.0 |
| 输出 · 空闲 | 4.0 | 13.5 |

> 旧模型名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 仍可调用，按 Flash 价格计费，所以配置里也复制了一份。价格以官方账单为准，页面更新后改这里即可。

**生图费用**：`image_price_per_image = 0.05`，按**成功出图张数**计费（失败不计）。费用区会显示：

```text
- 生图：12 张 × ¥0.0500 = ¥0.6000
```

> 计价来源：<https://api-docs.deepseek.com/zh-cn/quick_start/pricing/>（若官方调价，按新价改配置）。

## 报告示例

```text
ATRI-X 定时报告
周期：2026-10-01 09:00 ~ 2026-10-01 21:00
窗口：最近 12 小时

【生图】
- 成功：12 次
- 失败：1 次
- 合计：13 次

【Token 消耗】
- deepseek-v4-pro：调用 86 次｜输入 1,234,567｜输出 234,567｜缓存命中 800,000｜合计 1,469,134
- deepseek-v4-flash：调用 42 次｜输入 120,000｜输出 30,000｜缓存命中 60,000｜合计 150,000
- 合计：调用 128 次｜输入 1,354,567｜输出 264,567｜缓存命中 860,000｜合计 1,619,134

【费用】
- deepseek-v4-pro：¥12.3456
- deepseek-v4-flash：¥0.3456
- 生图：12 张 × ¥0.0500 = ¥0.6000
- 合计：¥13.2912
- 计价：高峰/空闲双档（北京时间周一至周五 9-12、14-18 为高峰；周末及配置的节假日为空闲）

【磁盘】
- 数据目录：1.2 GB
- 文件系统：已用 12.3 GB / 50.0 GB（剩余 37.7 GB，24.6%）

【内存】
- 进程 RSS：128.4 MB
- 解释器内存：32.1 MB / 系统 96.0 MB
- asyncio task：42
```

## 数据来源

| 指标 | 来源 |
| --- | --- |
| 生图量 | audit 日志 `event=tool_call tool=image_generate` 的 `success` 字段 |
| Token | audit 日志 `event=llm_usage` 的 `prompt_tokens/completion_tokens/total_tokens/cache_hit_tokens`；`cache_hit_reported=false` 表示上游这次没返回缓存字段（此时 `cache_hit_tokens=0` 只是"未上报"，不是"没命中"） |
| 费用 | 上面 token × `prices.<model>` 单价；缓存命中按 `cache_input_per_million` 单独计价 |
| 磁盘 | `data_root` 目录递归大小 + `statfs` 文件系统总量/可用量 |
| 内存 | `/proc/self/statm` RSS + `runtime.MemStats` |

## 注意事项

1. **发送目标**：报告按 `[security.superadmins]` 发送。如果没配超管 ID，发送会失败并记录 warning；请在 `[security.superadmins]` 里给对应平台填上用户 ID。
2. **日志保留期**：Token 统计依赖 audit 日志，`[runtime].log_retention_days`（默认 30 天）会清理旧日志，报告窗口不要超过保留期。
3. **价格准确性**：单价以你的官方/中转账单为准，ATRI-X 只做乘法；缓存命中 token 会按 `cache_input_per_million` 计价（未配置则按输入价）。
4. **磁盘指标**：数据目录默认是 SQLite 所在目录（含 `logs/`、`media/`、`sandbox/` 等）；文件系统使用量需要 Linux（`statfs`），其他平台只报数据目录大小。
5. **服务器时区**：高峰判定按北京时间；部署脚本已设置 `TZ=Asia/Shanghai`，如果自己改过时区，记得保持 Asia/Shanghai，否则高低峰会算错。
6. **手动补发**：可以让机器人调用 `cron` 工具触发同名维护任务（`maintenance.daily_report`），或临时创建一个只跑一次的任务。注意 `cron` 是超级管理员专用的**内置工具**而不是 slash 命令：需要先切到 `work` 模式，并让模型发现该工具（也可用 `@tool:cron` 预载）。

---

# 每日会话总结（`[maintenance.daily_summary]`）

资源报告看的是"花了多少、占了多少"，**会话总结看的是"今天聊了什么"**：它把一段时间内
ATRI-X 自己的会话（SQLite `sessions` + `messages`）汇总成一段可读文本，默认每天 8:00
生成一次，**不调用模型、不花 token**。

## 产物

```text
<数据目录>/daily_summaries/<YYYY-MM-DD>.json
```

```json
{
  "day": "2026-10-18",
  "createdAt": "2026-10-18T00:00:12.345678+00:00",
  "sessions": 3,
  "messages": 42,
  "source": "digest",
  "text": "ATRI-X 会话总结 2026-10-18\n窗口：2026-10-17 08:00 ~ 2026-10-18 08:00\n会话 3 个 / 消息 42 条\n\n1) 10-17 21:12 · cli · 《项目讨论》 · 12 条\n   用户：…\n   助手：…"
}
```

## 配置

```toml
[maintenance.daily_summary]
enabled = true                # 默认由配置资产打开
schedule = "0 8 * * *"        # 每天 8:00
days = 1                      # 统计最近 1 天（24 小时口径，不是自然日）
max_sessions = 20             # 最多汇总多少个会话（按最近更新时间倒序）
use_llm = false               # true 时把摘要再交给压缩模型改写一遍（会花 token）
notify = false                # true 时走资源报告的投递通道发给超管
```

## 查看与手动生成

```bash
# 查看最近 3 份
python -m atri_x.maintenance.summary_cli

# 现在生成一份并落盘（会覆盖当天的文件）
python -m atri_x.maintenance.summary_cli --build

# 机器可读
python -m atri_x.maintenance.summary_cli --json --limit 7

# 打包后的程序同样可用
atri-x.exe -m atri_x.maintenance.summary_cli --build
```

## 说明

1. **窗口口径**：`days` 是"最近 N×24 小时"，与资源报告的 `window_hours` 一致；日期标签按
   Asia/Shanghai 计算。
2. **`use_llm`**：打开后复用 `/compact` 的压缩模型（provider/model 与压缩完全一致）；
   模型没配、调用失败或返回空串时**自动回退**到确定性摘要，不会丢当天的记录。
   摘要不会写回任何会话，也不会改 `messages` / `context_summaries`。
3. **只读本地库**：不访问平台接口、不上传任何内容；`notify = true` 才把正文按资源报告
   通道发给超管。
4. **与清理的关系**：`[maintenance.session_cleanup]`（默认开启）会在保留期后删除会话行；
   需要长期留着每日总结就把 `daily_summaries/` 单独备份，不要依赖库里还留着原文。
5. **默认不含 DSH 内核侧会话**：内置 DSH 内核的一次性会话历史由
   `[maintenance.dsh_cleanup]` 清理，其内容不进本总结（DSH 用自己的存储格式）。
