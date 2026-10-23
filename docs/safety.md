# 内容安全（道德限制 + 敏感信息过滤）

ATRI-X 内置的**内容安全闸门**：用词表拦"道德 / 合规"类内容，用正则拦**敏感信息**
（凭据、PII、内网地址、本机路径），并在**发送边界**按策略丢弃图片 / 文件出口。

它是 1.0.2 外挂 PoC（`ATRI-X-safety-gate`，Node Hook 插件 + 每次调用拉起一个
`node.exe` 子进程）的**原生 Python 内置化**：不再需要 Node 运行时、不再有
50~150 ms 的进程冷启动，配置落在 `app.toml` 的 `[safety]` 段。

> 定位：这类过滤只能**降低风险**，不替代平台侧审核与人工运营。面向公众服务时，
> 生成式 AI 标识、举报入口、未成年人保护等要求仍需按当地法规单独实现。

## 快速开始

`app.toml`：

```toml
[safety]
enabled = false      # 总开关：默认关，不配置就完全不改变既有行为
mode = "audit"       # off | audit | enforce
```

上线分两步走（和 PoC 的建议一致）：

1. `enabled = true` + `mode = "audit"`：**只记审计、不改任何输出**。观察一到两天，
   看 `safety-audit.jsonl` 里的 `labels` 与 `preview` 有多少误杀。
2. 误杀可以接受后改成 `mode = "enforce"`：词表命中整体拦截、敏感信息脱敏、
   按策略丢弃图片 / 文件出口。

改完配置重启服务（或在 GUI 里重载配置）即可生效；策略在启动时编译一次，
运行期不重新读盘。

## 桌面端（GUI）设置

内容安全**不在「进阶」页**，而在「工具权限」页里：

1. 主窗口「工具权限」选项卡 → 第三个子选项卡「**工具参数（全局）**」；
2. 「**全局参数（所有工具）**」面板默认展开，往下翻到「内容安全」这一段（全局默认的 8 项）；
3. 同一个子选项卡里还有两个覆盖面板：「**内容安全：chat 模式覆盖**」「**内容安全：work 模式覆盖**」；
4. **chat / work 两个子选项卡里也各有一份同样的覆盖面板**——切到该模式就能直接改，不用再绕到工具参数页；
5. 改完点面板里的「保存本组」或页面顶部的「保存全部改动」，再点右下角「重启后台服务」生效。

覆盖面板里的「（默认）」= **继承全局**；「启用内容安全」是三态，因此可以在全局关闭时只给某个模式单独打开。
审计路径只保留在全局默认面板（审计文件全局唯一，记录里的 `mode` 字段写该模式实际生效的模式）。

「功能」页里的「内容安全（道德限制 + 敏感信息过滤）」一行是只读状态（显示全局 `enabled` / `mode`
与 chat/work 覆盖摘要），双击它会跳到工具参数页并展开全局面板。词表类别 `categories`、
敏感信息类别 `sensitive`、`sensitive_actions`、`allowlist`、`audit_preview_chars` 这些
**逐类细调项仍只在 `app.toml` 里配**，GUI 没有为它们做控件。

## 按会话模式覆盖（chat / work）

1.0.3 热修起，同一份内容安全策略可以按**会话模式**各覆盖一份：`[safety.chat]` 管 chat 模式
（闲聊 / 低成本模型槽位），`[safety.work]` 管 work 模式（工作 / 工具模型槽位）。

```toml
[safety]
enabled = true
mode = "audit"            # 全局默认

[safety.chat]
mode = "off"              # chat 模式不跑过滤（其余字段继续继承全局）

[safety.work]
mode = "enforce"          # work 模式真正拦截 / 脱敏
block_image_output = true
```

- **没写的键继承全局默认**：不写 `[safety.chat]` / `[safety.work]` 时行为与 1.0.3 之前完全一致。
- 可覆盖：`enabled` / `mode` / `categories` / `sensitive` / `sensitive_actions` / `allowlist` /
  `block_reply` / `strip_tool_images` / `block_image_output` / `block_file_output` / `max_scan_chars`。
  字符串/列表类的**空值也算继承**（例如覆盖段里 `block_reply = ""` 仍然用全局的拦截文案）。
- **不可覆盖**：`audit_path` / `audit_preview_chars`（审计文件全局唯一）。
- 全局 `enabled = false` 时，覆盖段里写 `enabled = true` 可以让**只有该模式**跑过滤；
  两个覆盖段都没打开时 guard 根本不会装配，行为与不配置一致。
- 运行时按当前会话的 `session.mode` 选策略。入站消息在会话创建前按「群默认模式 → 服务默认模式
  （默认 `work`）」逐级推断，因此首条消息与后续轮次用的是同一套覆盖。
- 工具链路（`image_generate` 提示词、`send_file`、工具结果剥离/脱敏）也按当前会话模式生效。

## 模式语义

| mode | 词表命中 | 敏感信息 | 图片 / 文件出口 | 审计 |
| --- | --- | --- | --- | --- |
| `off`（含 `enabled = false`） | 不判定 | 不判定 | 不判定 | 不写 |
| `audit` | 判定，但**不改文本** | 判定，但**不脱敏** | 不丢弃，只记录"本会拦" | 写 |
| `enforce` | 整段换成拦截文案 | `redact` 类替换、`flag` 类只记 | 丢弃 + 补文本说明 | 写 |

`enabled = false` 与 `mode = "off"` 等价；非法 `mode` 值回落到 `audit`
（宁可只记录，不静默变成拦截）。

**硬约束**：`enabled = false` 时所有集成点都走"完全无行为变化"的短路，
不读词表命中、不写审计、不动文本。

## 配置项

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关 |
| `mode` | `"audit"` | `off` / `audit` / `enforce` |
| `categories` | 空 = 全部 8 类 | 启用的词表类别，见下表 |
| `sensitive` | 空 = 全部 10 类 | 启用的敏感信息类别 |
| `sensitive_actions` | 空 = 内置默认 | 覆盖单个敏感类别的动作：`flag` / `redact` |
| `allowlist` | 空 | 命中豁免：命中片段**包含**其中任一条就放过**这一处** |
| `block_reply` | 内置文案 | 入站消息被拦截时回复用户的话（可改成"安抚 + 求助热线"等） |
| `audit_path` | 空 = `<配置目录>/safety-audit.jsonl` | 审计文件路径 |
| `audit_preview_chars` | `120` | 审计预览保留的字符数 |
| `strip_tool_images` | `false` | 把工具结果里的图片段剥离成文字（模型看不到图） |
| `block_image_output` | `false` | 出站图片丢弃并补文本说明 |
| `block_file_output` | `false` | 出站文件丢弃并补文本说明 |
| `max_scan_chars` | `8000` | 单次扫描上限；超出部分不扫描、原样保留 |
| `chat` / `work` | 空 | 按会话模式覆盖的**子段**（`[safety.chat]` / `[safety.work]`），见下节 |

### 词表类别（`categories`）

词表是包内的数据文件（`atri_x/safety/wordlists/<类别>.txt`，UTF-8、一行一条、
`#` 开头为注释）。改词表不需要改代码。

| 类别 id | 含义 | 动作 |
| --- | --- | --- |
| `minor_sexual` | 涉及未成年人的性内容 | block |
| `sexual_explicit` | 露骨色情 | block |
| `violence_gore` | 暴力血腥与虐待 | block |
| `terror_extremism` | 恐怖主义与极端主义 | block |
| `illegal_method` | 违法犯罪方法（制毒 / 制枪 / 爆炸物 / 诈骗话术） | block |
| `self_harm` | 自伤自杀 | block |
| `hate_speech` | 仇恨与歧视 | block |
| `privacy_violation` | 人肉开盒与隐私侵害 | block |

内置词表刻意保守（`hate_speech` / `sexual_explicit` 只放了明确短语）。**这类过滤一定
会误杀**，请先把你们已有的合规词表 / 第三方审核类别导进去，再用 `audit` 灰度。

### 敏感信息类别（`sensitive` / `sensitive_actions`）

| 类别 id | 匹配对象 | 默认动作 |
| --- | --- | --- |
| `private_key` | PEM 私钥块（多行） | `redact` |
| `api_key` | `sk-…` / `oc_sk_…` / `ghp_…` / `github_pat_…` / `xox?-…` / `AKIA…` / `Bearer …` | `redact` |
| `jwt` | 三段式 JWT | `redact` |
| `password` | `password=` / `api_key:` / `client_secret=` 这类赋值 | `redact` |
| `id_card` | 18 位身份证号 | `flag` |
| `bank_card` | 16~19 位卡号 | `flag` |
| `phone` | 中国大陆手机号 | `flag` |
| `email` | 邮箱地址 | `flag` |
| `internal_ip` | `10.` / `192.168.` / `172.16~31.` 内网地址 | `flag` |
| `local_path` | `C:\…` 这类本机绝对路径 | `flag` |

PII 默认只 `flag`（记审计、不改文本）：工程群、客服场景里手机号 / 内网 IP / 本机路径
常常是**正常内容**，直接替换会大面积误杀。确认要改：

```toml
[safety.sensitive_actions]
phone = "redact"
email = "redact"
```

**脱敏是幂等的**：同一段文本过两遍不会继续啃掉尾巴（替换文本里不含会被自身正则
再次命中的形态）。发送边界会再扫一遍，因此二次扫描必须安全——这条有专门的回归测试。

### 白名单（`allowlist`）

只豁免**这一处命中**，不是整段文本。常用写法：示例密钥、`127.0.0.1`、
`[已脱敏`（避免把自己产出占位符再扫一遍）。

```toml
[safety]
allowlist = ["sk-test", "AKIAIOSFODNN7EXAMPLE", "example.com"]
```

## 处理链路

| # | 位置 | 行为 |
| --- | --- | --- |
| 1 | 入站用户消息（`platform.message.received` Hook 之后） | 词表命中且 `enforce` → 用 `block_reply` 回复并**结束本轮**（不进模型、不落库成正常对话）；只命中可脱敏项时把脱敏后的文本写回消息段 |
| 2 | 模型输出文本（`llm.response.received` Hook 之后） | 结果写回 `event.llm.text`，后续落库 / 展示 / 发送看到同一份文本 |
| 3 | 工具结果（`tool.call.completed`） | 文本段逐段脱敏；`strip_tool_images = true` 时图片段换成文字占位 |
| 4 | 发送边界（`agent.output.prepared` / `send_outputs` / `send_notice`） | 正文与通知文本再过一遍（幂等）；图片 / 文件按 `block_*_output` 丢弃并补一条文本说明 |
| 5 | 生图提示词（`image_generate` 请求 provider 之前） | 拼接后的正向 / 负向提示词命中词表且 `enforce` → 直接返回失败，不发起请求 |
| 6 | 文件出口（`send_file`） | 按 `block_file_output` / `block_image_output` 在出口拦截 |

图片 / 文件**不做内容识别**：只按策略在出口丢弃（换来的是零第三方依赖、零额外延迟）。
要"看图片内容是否违规"，属于另一件事（本地 NSFW 模型或云审核），不在本功能范围内。

## 审计日志

默认 `<配置目录>/safety-audit.jsonl`，一行一条 JSON：

```json
{"stage":"inbound","action":"block","mode":"enforce","labels":["illegal_method"],
 "reason":"命中：违法犯罪方法(illegal_method)","chars":42,
 "sha256":"…","preview":"…","platform":"qq","scope":"group:42","actor":"10001",
 "ts":"2026-01-01T00:00:00+00:00"}
```

- **不存原文**：只有 `sha256`（原文指纹）+ **脱敏预览**。预览里连"只 flag"的手机号、
  邮箱、内网地址也已被替换，词表命中替换成 `[已过滤]`——避免"做了内容安全反而把
  敏感内容二次落盘"。
- `pass` 判定不写记录；`audit` / `enforce` 两种模式写的是同一种结构（用 `mode` 区分）。
- 单文件超过 1 MiB 自动滚动为 `.1`（只保留一份历史）。
- 写入用 `threading.Lock` 串行；**写失败只记日志 + 计数，不阻断消息发送**
  （磁盘满、目录不可写都不会让对话失败）。

## 灰度与误杀处理

1. `mode = "audit"` 跑一两天。
2. 看审计里的 `labels` / `preview`，确认哪些是误杀。
3. 误杀的处理顺序：先加 `allowlist`（只豁免那一处），再把不合适的词条从
   `wordlists/*.txt` 删掉，最后才是整类 `categories` 关闭。
4. 确认可以接受后切 `mode = "enforce"`。

## 已知限制

- **词表一定误杀**：`hate_speech` / `sexual_explicit` 这类边界高度依赖语境，
  内置词表只覆盖"明确"的短语；请按运营规范补充或接第三方审核。
- **没有二级语义审核**：PoC 里可选的 `reviewer`（调一个 OpenAI 兼容模型做
  PASS/REJECT 分类）**未内置**。确实需要时建议走外挂 Hook，不要塞进主链路
  （会把每条外发消息都变成一次模型请求）。
- **不做图片 / 文件内容识别**：出口只按策略整体丢弃。
- **`max_scan_chars` 之外的文本不扫描**（原样保留），超长文档里的敏感信息可能漏过；
  需要时调大这个值，代价是延迟。
- **流式预览 / 思维链**（`send_preview` / `send_reasoning`）不在 guard 链路上：
  它们在发送边界之前就到平台了。`send_notice` 与最终正文已覆盖。
- **入站拦截只作用于文本**：入站图片 / 文件不审核。
- 词表 / 正则是在启动时编译的；改词表文件后需要重启（或重载配置）。

## 验证

```bash
python -m unittest tests.test_safety_core tests.test_safety_integration -v
```

- `tests/test_safety_core.py`：策略装载、词表命中、脱敏与**幂等**、`audit` 不改输出、
  `enforce` 拦截、白名单、审计不存原文、`mode = off` 无变化、媒体出口、审计写失败不抛。
- `tests/test_safety_integration.py`：逐个集成点的假宿主验证（入站 / 模型输出 /
  工具结果 / 发送边界 / 通知 / 文件出口 / 生图提示词）。

## 回滚

把 `[safety]` 的 `enabled` 改回 `false`（或把整段删掉）即恢复 1.0.2 行为；
审计文件可以保留也可以删除（`<配置目录>/safety-audit.jsonl`）。
