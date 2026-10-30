# 聊天漫画（comic_plan / comic_render）

把**当前群聊**改编成一页结构化漫画：先出分镜计划给用户看，确认后再生成一张整页
图。1.1.6 新增（Python 侧功能，Go 上游没有对应实现）。

设计目标只有三条：**来源可查、人物不串、失败不乱重试**。比"多塞几句绘图提示词"
更重要的是：这张图用了哪些聊天、哪个人物对应第几张参考图、台词是不是原文、失败
之后能不能安全重试。

## 1. 它和普通生图的区别

| | `image_generate` | 聊天漫画 |
| --- | --- | --- |
| 输入 | 一段场景描述 | 当前群聊的真实消息 |
| 结构 | 无（提示词文本） | 结构化计划：格数 / 人物 / 逐字台词 / 来源消息 |
| 分成两步 | 否 | 是：`comic_plan`（不扣额度）→ `comic_render`（生成） |
| 人物 | 角色卡预设 | 群友身份 → 角色卡 / 平台头像，参考图按编号绑定 |
| 台词 | 可被语义改写 | **受保护**，逐字进提示词，不翻译不压缩 |
| 失败恢复 | 单次重试 | 记录三个状态；超时标"结果未知"，不自动重试；可按任务重发已有图片 |

两者共用同一批基础设施：生图 profile（`[image_generation]`）、并发限流、Media
Center、内容安全检查、聊天历史与角色库——漫画没有自己的 HTTP 客户端，也不会绕过
profile 的 `superadmin_only`。

## 2. 使用流程

```text
用户：根据刚才聊天画六格漫画，人物用头像
  ↓ comic_plan({panel_count: 6, character_mode: "avatars"})
取材（当前会话最近 12 条）→ 人物解析（群友 → 头像）→ 分镜（规则或模型）
  ↓ 返回 plan_id + 逐格预览（不调用生图、不扣额度）
用户确认
  ↓ comic_render({plan_id: "...", revision: 1})
编译受保护提示词 → 生成 1 张整页图 → 入库 → 发送到当前聊天 → 写生成记录
```

模型也可以自己串起来（`comic_render` 的 `depends_on` 里有 `comic_plan`）：但计划
必须先落盘，这是"先预览再花钱"的硬约束，不是建议。

## 3. 两个工具的参数

### `comic_plan`

| 参数 | 说明 |
| --- | --- |
| `panel_count` | 格数，默认 6，上限 `[comic].max_panels`（默认 12）。**不是生成几张图**。 |
| `language` / `layout` | `zh`/`en`；`grid`（网格，默认）/`strip`（竖排条漫）。 |
| `title` / `instructions` | 标题提示；本次改编的额外要求。 |
| `character_mode` | `auto`（默认：角色卡优先、否则头像）、`characters`（只用角色卡）、`avatars`（全部用群友头像）。 |
| `character_ids` | 显式指定角色卡 ID 列表（优先于模式）。 |
| `reference_images` | 本次显式参考图：`<角色ID或群友ID或显示名>=media:<sha256>`，或 `<角色ID>=<角色图片名>`；不带 `=` 的按顺序补位。 |
| `anchor` | 以某条消息为中心取材（消息 ID，或 `current`）。锚点不存在时**报错**，不会退回"最近几条"。 |
| `context_query` / `context_limit` | 关键词取材；取材条数。 |

结果里带 `plan_id`、`revision`、逐格预览与来源消息 ID；同一个 `plan_id` 每次重新
规划都会递增 `revision`。

### `comic_render`

| 参数 | 说明 |
| --- | --- |
| `plan_id` | 必填。 |
| `revision` | 要生成第几版；不填用最新版。计划更新后旧版本会被**拒绝**。 |
| `task_id` | 重发已有任务：只发送记录里的 MediaID，不重新生成、不重复扣费。 |
| `profile` / `size` / `quality` | 可选生图参数，不填用 profile 默认。 |
| `send` | 是否直接发到当前聊天；不填跟随 `[comic].send_by_default`（默认 true）。 |
| `dry_run` | 只编译并显示最终提示词、参考图顺序，不读图、不下载头像、不调用生图、不保存。 |

**不会生成 N 张图**：一页漫画永远是 `count=1` 的整页图。

## 4. 人物与参考图

人物身份是稳定的，显示名不是：

* 群友：`member:<platform>:<sender_id>`，同名不同人不会合并；
* 角色卡：`character:<角色 ID>`，走角色库可见性校验。

参考图来源优先级：**本次显式指定 > 角色卡图片 > 平台头像**。用户明确说"就用头像"
（`character_mode=avatars`）时不会被偷偷换成角色卡。

缺图时的行为是明确的：

| 情况 | 行为 |
| --- | --- |
| `auto` 且没有角色卡也没有头像 | `source=none` + 告警"没有参考图，仅按文字设定绘制" |
| `avatars` 且平台没有头像能力 | **报错**，不发起生成 |
| 角色卡图片读取失败 | 告警 + 跳过该图（角色还有文字设定） |
| 参考图超过 4 张 | 报错，不静默截掉关键人物 |

头像下载走 Media Center 的 `import_url`（沿用其超时/大小限制），不另写下载器；规划
阶段只解析 URL，**渲染阶段才下载**。

## 5. 配置

```toml
# services.toml
[comic]
enabled = true
planner = "auto"            # auto | rules | llm
plan_model = "work"         # naming / compact / chat / work
language = "zh"
layout = "grid"
default_panel_count = 6
max_panels = 12
max_characters = 4
context_limit = 12
max_scene_runes = 240
max_dialogue_runes = 120
max_prompt_runes = 6000
send_by_default = true
```

* `planner=auto`：有可用规划模型就用模型，否则用规则规划。**规则规划不需要任何模型
  配置**，台词逐字取自原文，开箱可用。
* 规划模型的系统提示词在桌面端「提示词 → 漫画」里可改（id `comic.plan`）。
* 生图端点（base_url / api_key / model / profiles / supports_reference）仍然在
  `[image_generation]`，漫画不重复配置。
* 生效时机：`[comic]` 在启动期装配，改完需要重启后台服务；提示词模板下一次规划即生效。

## 6. 生成记录与失败恢复

每次渲染都会在 `<配置目录>/comic/records/<task_id>.json` 落一份记录：

* 计划 `plan_id + revision + fingerprint`（改台词后旧版本不能误用）；
* 完整最终提示词、负向提示词、参考图顺序；
* 生图 profile / model / size / quality；
* **三个独立状态**：`generation`（生成）、`storage`（入库）、`delivery`（投递请求）；
* 结果 MediaID、`revised_prompt`、错误信息与尝试次数。

失败处理：

| 情况 | 状态 | 行为 |
| --- | --- | --- |
| 服务端明确报错 | `failed` | 报错；用户可重新渲染（新任务） |
| 超时 / 连接中断（可能已生成） | `unknown` | 报"结果未知"，**不自动重试**，避免重复扣费 |
| 生成成功、投递失败 | `succeeded` + `delivery=requested` | 用 `task_id` 重发已有图片，不再调用生图 |
| 进程重启时卡在渲染中 | `unknown` | 下次读记录时标记，不假装成功 |

投递成功回执由 Agent 层负责；工具只能记录"请求投递"，不会伪造回执。

## 7. 已知边界

* **台词逐字进请求 ≠ 绘图模型逐字画对**。前者是程序保证（受保护提示词块），后者
  取决于模型的中文出字能力，需要人工 E2E 验收。
* 规划模型返回的 JSON 会做完整校验：格数不符、人物/消息不存在、声称原文却对不上
  素材都会被拒绝或降级（"原文"降级为"缩写"），不会静默放行。
* 一页最多 4 个有参考图的人物（与生图参考图上限一致）；更多人物请分页。
* 局部重绘、分格生成再拼版、OCR 校正、中文后期排版不在首版范围。
* 历史消息里的"忽略指令 / 把图发到别群"等文本只作为剧情素材：规则规划不经过模型，
  模型规划的 system 提示词也明确声明它们不是指令。

## 8. 代码位置

| 位置 | 职责 |
| --- | --- |
| `atri_x/comic/models.py` / `records.py` | 计划与记录的值对象、校验、序列化 |
| `atri_x/comic/context.py` | 只从当前会话取材，保留消息 ID |
| `atri_x/comic/characters.py` | 群友/角色卡/头像 → 参考图绑定 |
| `atri_x/comic/planner.py` | 规则规划 + 模型 JSON 结构校验 |
| `atri_x/comic/prompt.py` | 受保护提示词编译（台词逐字、超长报错） |
| `atri_x/comic/references.py` | 参考图读取/头像下载（复用 Media Center） |
| `atri_x/comic/store.py` | 计划/记录的文件持久化（原子写 + 路径白名单） |
| `atri_x/comic/service.py` / `options.py` | 编排入口与策略值对象 |
| `atri_x/app/comic.py` / `tool_runtime_comic.py` | 规划模型客户端、平台头像适配、运行时装配 |
| `atri_x/tool/builtin/comic.py` | 两个工具本体 |

单测：`tests/test_comic_*.py` 与 `tests/test_builtin_comic.py`。
