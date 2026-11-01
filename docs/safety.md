# 内容安全（道德限制 + 敏感信息过滤 + 远程审核）

ATRI-X 内置的**内容安全闸门**：用词表拦"道德 / 合规"类内容，用正则拦**敏感信息**
（凭据、PII、内网地址、本机路径），可选地把文本/图片送到**云厂商内容审核**做语义判断，
并按 `block` / `review` / `pass` 三档决定"拦 / 进人工队列 / 放行"。

它和 `[security]`（工具风险与权限）是两件事：内容安全管**内容**，`[security]` 管**能力**。

> 定位：这类过滤只能**降低风险**，不替代平台侧审核与人工运营。面向公众服务时，
> 生成式 AI 标识、举报入口、未成年人保护等要求仍需按当地法规单独实现。
>
> ⚠️ **远程审核会把你送审的文本/图片发给云厂商**。默认关闭；开启前请确认合规与
> 数据出境要求。

## 两层配置：`app.toml` 的 `[safety]` 与独立的 `safety.toml`

| 用法 | 读哪里 | 说明 |
| --- | --- | --- |
| **旧路径（1.0.3 ~ 1.1.7）** | `app.toml` 的 `[safety]` / `[safety.chat]` / `[safety.work]` | 只有本地词表 + 正则；**没有 `safety.toml` 时行为与 1.1.7 完全一致** |
| **新路径（1.1.8+）** | `<配置目录>/safety.toml` + `safety/` 数据目录 | 本地层可编辑 + 热加载 + 远程审核 + 三档 + 复核队列 |

`safety.toml` **存在**时，本地层由它统一提供（旧的按模式覆盖不再参与）；不存在时完全走旧
路径。删掉 `safety.toml` 即可一键回到 1.1.7 行为（热加载生效，不需要重启）。

## 五层结构

```
本地规则层（词表 / 正则 / 归一化 / 白名单）
        │  命中即结案（零成本）
        ▼
远程审核层（腾讯云 / 阿里云；双供应商仲裁、缓存、熔断、预算）
        │  归一化成 verdict
        ▼
裁决层（block / review / pass；按群、按工具覆盖；fail-open / fail-close）
        │
        ├─▶ 处置（拦截 / 脱敏 / 丢弃 / 先压住）
        └─▶ 人工复核队列（sqlite）
                    │
                    ▼
             留痕（safety-audit.jsonl，v2）
```

设计原则：**默认关闭**、**fail-open 不阻断对话**、**审计不存原文**、**纯标准库**、
**本地优先**（本地命中即结案，不花钱）。

## 数据目录

```
<配置目录>/                       # 例：%APPDATA%\ATRI-X
├── safety.toml                   # 主策略（用户可编辑，热加载）
├── safety/
│   ├── wordlists/<类别>.txt      # 用户词表：同名与内置合并，新名字自动成为新类别
│   ├── sensitive/<名字>.regex    # 一行一条：名字 = /正则/flags
│   ├── scopes/<任意名>.toml      # 按群 / 按用户覆盖
│   ├── review.db                 # 人工复核队列（自动建）
│   └── quarantine/               # 异步待审媒体的暂存位置（P4 使用）
└── safety-audit.jsonl            # 审计（v2）
```

**热加载**：按 `(mtime_ns, size)` 指纹检测；`safety.toml`、词表、正则、scope 文件任一变化
→ 重建规则集（1 秒节流）。配置写坏了（TOML 语法错 / 正则非法）**保留上一份可用策略**并
写 WARN，不会让内容安全静默失效。

## `safety.toml` 参考

```toml
version = 1

[local]                      # 本地规则层（语义与旧 [safety] 一致）
enabled = true
mode = "enforce"             # off | audit | enforce
categories = []              # 空 = 全部 8 类内置词表（用户词表的新类别总是启用）
sensitive = []               # 空 = 全部 10 类敏感信息
sensitive_actions = {}       # 逐类 redact / flag
allowlist = []               # 命中豁免（子串匹配，只放过那一处）
max_scan_chars = 8000        # 扫描预算 = 这个值 × 4，分块扫
normalize = true             # NFKC / 去零宽 / 去 CJK 间分隔符 / 大小写折叠
wordlist_dir = "safety/wordlists"
sensitive_dir = "safety/sensitive"
strip_tool_images = false
block_image_output = false
block_file_output = false
audit_path = ""              # 空 = <配置目录>/safety-audit.jsonl

[remote]                     # 远程审核层（默认关闭）
enabled = false
providers = ["tencent", "aliyun"]     # 顺序即优先级
quorum = "any_block"                  # any_block | both_pass | primary_only
timeout_ms = 800
retries = 1
fail_mode = "open"                    # open | closed
fail_close_scopes = []                # 例 ["qq:group:9299***42"]
degraded_alert = true
cache_ttl_seconds = 600
cache_max_entries = 2000
redact_before_submit = true           # 先本地脱敏 PII/凭据，再送云
stages = { inbound = "sync", llm = "sync", send = "sync", tool = "cache_only", image_prompt = "local_only" }

[remote.tencent]
secret_id_env = "TENCENT_SECRET_ID"       # 变量名，不是密钥本身
secret_key_env = "TENCENT_SECRET_KEY"
region = "ap-guangzhou"
service = "tms"                           # 文本走 tms，图片走 ims
biz_type = ""                             # 天御 BizType（可选）

[remote.aliyun]
access_key_id_env = "ALIYUN_ACCESS_KEY_ID"
access_key_secret_env = "ALIYUN_ACCESS_KEY_SECRET"
endpoint = "green-cip.cn-shanghai.aliyuncs.com"
region = "cn-shanghai"
service = "comment_detection"

[tiers]                      # 档位 → 处置
local_wordlist = "block"              # 词表命中（默认拦）
remote_block = "block"
remote_review = "review"
inbound_review = "pass_and_queue"     # 入站 review：送模型 + 入队；改成 hold 则先压住
outbound_review = "send_and_queue"    # 出站 review：照发 + 入队；改成 hold 则先压住
high_risk_scopes_hold = []            # 这些 scope 的 review 一律 hold

[media]
images = "sync"                       # sync | async | off
image_max_bytes = 4194304
image_base64_max_bytes = 1048576      # 超阈值且有对象存储走 URL，否则跳过并告警
object_store = "s3"
audio = "async"
video = "async"
async_poll_seconds = 60
quarantine_ttl_seconds = 3600
skip_actors = []                     # 这些用户 id 的图片不送远程审（成本策略）

[cost]
daily_budget_cny = 0.0                # 0 = 不限
on_budget_exceeded = "local_only"     # local_only | pass_open | fail_close
text_price_cny_per_10k = 22.0

[tools]                      # 某工具的远程策略（覆盖 [remote].stages）
web_search = "local_only"
web_extract = "local_only"
image_generate = "sync"
```

### 阶段策略

| 值 | 含义 |
| --- | --- |
| `sync` | 同步调远程（受 `timeout_ms` 约束，100~300 ms 量级） |
| `cache_only` | 只复用缓存结论；未命中就当 pass（零额外延迟） |
| `local_only` | 完全不调远程，只走本地层 |
| `off` | 该阶段不审核 |

默认：`inbound` / `llm` / `send` 同步；`tool` 缓存优先；`image_prompt` 只走本地；
`web_search` / `web_extract` 只走本地（1.1.7 的误报重灾区）。

### 按群 / 按用户覆盖

```toml
# safety/scopes/qq__group__9299xxxx42.toml
platform = "qq"
scope_id = "9299xxxx42"

fail_mode = "closed"            # 高敏群：审核失败就拦
images = "sync"
tools = ["image_generate"]      # 该群额外强制远程审的工具
allow_tools = ["web_search"]    # 该群豁免远程审核的工具

[tiers]
remote_block = "review"
outbound_review = "hold"
```

匹配：`platform + scope_id` 精确优先，`scope_id = "*"` 是该平台兜底；一个坏文件只跳过自己。

## 本地规则层

- 8 类内置词表（`atri_x/safety/wordlists/*.txt`，共 54 条中文词条）+ 用户词表合并；
  新文件名（如 `my_terms.txt`）自动成为一个类别，默认动作 `block`。
- 10 类敏感信息：`private_key` / `api_key` / `jwt` / `password`（默认 `redact`）、
  `id_card` / `bank_card` / `phone` / `email` / `internal_ip` / `local_path`（默认 `flag`）。
- 自定义正则：`safety/sensitive/*.regex`，格式 `名字 = /正则/flags`，非法行整条跳过。
- **归一化**：词表匹配跑在折叠骨架上（全角→半角、去零宽/变体选择符、去 CJK 之间分隔符、
  大小写折叠），敏感信息仍在**原文**上跑正则（替换位置才对）。
- **幂等**：同一段文本重复扫描不会继续啃尾巴。
- **扫描预算**：`max_scan_chars × 4` 字符，超出的尾部原样保留。

## 远程审核层

| 供应商 | 文本 | 图片 | 认证 |
| --- | --- | --- | --- |
| 腾讯云天御 | `TextModeration`（`tms.tencentcloudapi.com`） | `ImageModeration` | TC3-HMAC-SHA256 |
| 阿里云内容安全 2.0 | `TextModeration`（`green-cip.*`） | `ImageModeration`（需 URL） | ACS3-HMAC-SHA256 |

- 凭据只认**环境变量名**：系统环境优先，其次 `<配置目录>/.env`；没配凭据的供应商自动跳过。
- **双供应商仲裁**：`any_block`（任一 block 即 block，默认）/ `both_pass` / `primary_only`；
  主供应商失败自动切备，结果标 `degraded`。
- **失败策略**：`open` 放行 + 告警 + 审计 `fail_open`；`closed` 按 block 处置。
- **熔断**：连续 5 次失败冷却 60 秒；认证类错误冷却 10 分钟。
- **缓存**：按内容 `sha256` 缓存结论（TTL 600 秒、LRU 2000 条）。
- **预算**：`daily_budget_cny` 超限后按 `on_budget_exceeded` 降级（默认只跑本地层）。
- **送审前脱敏**：`redact_before_submit = true` 时先本地脱敏 PII/凭据再送云。

> ⚠️ **未联调**：适配器按官方文档实现，签名/解析有离线用例（假传输 + 录制响应）覆盖，
> 但**还没有用真实账号跑过**。联调时先用 `[remote] enabled = true` + `[local] mode = "audit"`
> 观察审计里的 `provider` / `request_id` / `score`，再逐群切 `enforce`。

## 三档与人工复核

| 档位 | 入站 | 模型输出 / 发送边界 | 图片 / 文件 |
| --- | --- | --- | --- |
| `block` | 回复 `block_reply` 并结束本轮 | 整段换拦截文案 | 丢弃 + 补文本说明 |
| `review` | 默认送模型 + 入队；`hold` 时先压住 | 默认照发 + 入队；`hold` 时先压住 | 同左 |
| `pass` | 放行 | 放行 | 放行 |

复核队列：`<配置目录>/safety/review.db`（sqlite），`pending → approved / rejected / expired`；
每条含 `provider` / `request_id` / `labels` / `score` / `sha256` / 脱敏预览 / 处置动作。

## 处理链路（接线点）

| # | 位置 | 行为 |
| --- | --- | --- |
| 1 | 入站文本 | 本地 + 远程；`block` 回复并结束本轮；`review` 入队 / `hold` 压住 |
| 2 | 入站图片 | 取 media 字节 → 远程图片审核；`block` 回复并结束本轮 |
| 3 | 模型输出 | 结果写回 `event.llm.text`，落库/展示/发送看到同一份 |
| 4 | 工具结果 | 默认只走本地层；`strip_tool_images` 时图片换成文字占位 |
| 5 | 发送边界 | 文本再扫一遍（幂等）；图片/文件按策略丢弃或 hold |
| 6 | 生图提示词 | 本地层拦截（不发起请求） |
| 7 | 生图结果 / `send_file` | 图片远程审核后决定发 / 丢 / 压住 |
| 8 | 流式 delta | **远程审核开启时关闭流式**（delta 无法事后收回），退化成整条发送 |

## 审计（v2）

默认 `<配置目录>/safety-audit.jsonl`，一行一条 JSON：

```json
{"schema":2,"ts":"…","stage":"send","action":"review","tier":"review","mode":"enforce",
 "labels":["porn"],"reason":"远程：tencent(porn) 0.93","chars":42,
 "sha256":"…","preview":"…","platform":"qq","scope":"group:…","actor":"…",
 "provider":"tencent","request_id":"…","score":0.93,"latency_ms":187,
 "hold":true,"meta":{"tool":"image_generate"}}
```

- **不存原文**：只有 `sha256` 指纹 + `RuleSet.mask()` 之后的脱敏预览。
- `pass` 不记；`audit` / `enforce` 写同一种结构（用 `mode` 区分）。
- 单文件超 1 MiB 自动滚动成 `.1`；写失败只记日志 + 计数，不阻断发送。

## 灰度与回滚

1. **只开本地层**：`[local] mode = "audit"` → 看审计里的误报。
2. **远程只记录**：`[tiers] remote_block = "pass"`、`remote_review = "pass"` → 只留痕不处置。
3. **review 档生效**：`remote_review = "review"`（进队列、不拦）。
4. **逐群放开 block**：scope 文件按群覆盖。
5. **回滚**：`[remote] enabled = false` + `[local] mode = "off"`（热加载生效）；
   最坏情况删掉 `safety.toml` 即回到 1.1.7 行为。

## 成本

按腾讯云套餐价（10–22 元/万条）估算文本量：

| 群消息量 | 月条数 | 月成本（10–22 元/万） |
| --- | --- | --- |
| 500 条/日 | 1.5 万 | 15 – 33 元 |
| 2,000 条/日 | 6 万 | 60 – 132 元 |
| 10,000 条/日 | 30 万 | 300 – 660 元 |
| 50,000 条/日 | 150 万 | 1,500 – 3,300 元 |

省钱杠杆：本地层先判 → 缓存 → 工具结果不上远程 → 图片只审非白名单用户 → 预算熔断。

## 已知限制

- **词表一定误杀**：内置词表只覆盖"明确"写法；请补词表或依赖远程语义审核。
- **远程适配器未联调**：签名/解析有离线用例，真实账号联调是下一步。
- **音视频异步**：配置与接口已留（`[media] audio/video`、`quarantine/`），轮询任务尚未接入。
- **不做文件内容解析**：文件按策略整体放行/拦截。
- **`max_scan_chars × 4` 之外不扫描**（原样保留）。
- **思维链**（`send_reasoning`）只在 CLI 通道，不进平台。
- 词表/正则在重建时编译；改文件后最多 1 秒生效。

## 验证

```bash
python -m unittest tests.test_safety_core tests.test_safety_integration \
    tests.test_safety_settings tests.test_safety_providers \
    tests.test_safety_engine tests.test_safety_async_guard -v
# 桌面端面板（无 Tk 环境自动跳过）
python -m unittest tests.test_gui_safety -v
```

## 桌面端（「内容安全」选项卡）

1.1.8 起桌面端有独立的**「内容安全」**页（`atri_x/gui/tabs/safety.py`），六个子页：

| 子页 | 能做什么 | 写哪个文件 |
| --- | --- | --- |
| 总览 | 看当前开关/模式/远程供应商、复核待处理数、审计条数、词表规模；**一键创建默认配置**（`mode = "audit"`，只记不拦）；打开配置/词表/审计文件 | 只读（创建时写 `safety.toml`） |
| 策略 | 表单编辑 `[local]` / `[remote]` / `[tiers]` / `[media]` / `[cost]` / `[remote.tencent]` / `[remote.aliyun]` / `[remote.stages]`；保存走**行级补丁**，不重写整份文件、不丢注释；写完立刻用后台同一个解码器校验 | `safety.toml` |
| 词表 | 列出 `safety/wordlists/*.txt` 与 `safety/sensitive/*.regex`，就地编辑/新建/删除；**本地试扫**用当前策略跑一段文本，直接看命中类别、动作与脱敏结果（不联网、不写审计） | 词表/正则文件 |
| 复核 | 列 `safety/review.db` 的待处理条目，看脱敏预览/供应商/分数，填备注后**通过 / 驳回 / 标记超时过期** | `safety/review.db` |
| 审计 | `safety-audit.jsonl` 尾部（最多 500 条），按动作/阶段筛选；只显示脱敏预览与指纹 | 只读 |
| 原始 TOML | 整份 `safety.toml` 文本编辑（表单没覆盖的键走这里）；保存前校验 | `safety.toml` |

操作顺序建议：**总览 → 创建默认配置**（audit）→ 跑一天 → **审计**页确认命中符合预期 →
**词表**页补词/加正则并试扫 → **策略**页把本地模式改成 `enforce` → 需要云审核时
再填 `[remote.tencent]` / `[remote.aliyun]` 的环境变量名（密钥放 `.env`）。

生效时机：`safety.toml` 与词表保存后**后台 ≤1 秒热加载**，不需要重启；只有
"整个内容安全从关闭到启用"这类装配期差异才建议重启一次后台服务。
「功能」页仍保留一行只读状态，双击可直接跳到本页。

