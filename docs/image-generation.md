# 生图服务（image_generate）

ATRI-X 内置 `image_generate` 工具，用于对接 OpenAI 兼容的 `/images/generations` 端点，例如中转站提供的 GPT Image 2.5 服务。它会在发送请求前自动拼接**预设提示词**，并把结果导入 Media Center、作为图片段返回给模型，可选发到当前聊天、写回角色素材库。

## 配置

生图的 `[image_generation]` 位于共享的 `services.toml`（旧部署仍可放在 `app.toml`）。本节后面的 `[image_generation]` 片段都指同一个 section：

```toml
[image_generation]
enabled = true
base_url = "https://your-relay.example.com/v1"  # 会自动拼 /images/generations
# endpoint = "https://your-relay.example.com/v1/images/generations"  # 端点不同时直接写全
api_key_env = "IMAGE_API_KEY"
# api_key = ""                                     # 不推荐，优先用环境变量
model = "gpt-image-2.5"
size = "1024x1024"
quality = "high"          # 具体取值以中转站为准（常见 low/medium/high）
output_format = "png"     # png / jpeg / webp
# response_format = "b64_json"  # 只有中转站要求时才填
timeout_seconds = 180

preset_prompt = ""        # 全局预设提示词
# negative_prompt = ""
max_prompt_runes = 4000

superadmin_only = true    # 只有超级管理员能调用
save_to_character = true  # 出图写回当前角色的 images/
send_by_default = false   # 生成后是否默认发到当前聊天

supports_reference = false  # 中转站是否支持参考图
reference_field = "image"   # 参考图透传字段名

# 提示词方言：决定画师串怎么渲染。不填按 model_family 推断，再不行用 natural。
# prompt_dialect = "natural"   # natural / sd_weighted / nai / tags
# model_family = "gpt_image"

# 负向提示词默认独立发送，不再折进正向提示词。
# negative_prompt_field = "negative_prompt"
# negative_prompt_mode = "field"   # field / inline / off

# 画师串库（可选）。配置 root 即启用。
# [image_generation.artist_library]
# enabled = true
# root = "artist_library.json"     # 相对 app.toml/services.toml 所在目录
# artist_default_preset = ""

# extra_payload = { }
# extra_headers = { }
# proxy = ""
```

API Key 从 `api_key_env` 指定的环境变量读取；Docker 部署时写在 `deploy/.env`（由 `deploy/.env.example` 复制而来）：

```bash
IMAGE_API_KEY=sk-xxxxxxxx
```

## 提示词是怎么拼的

最终提示词按顺序拼成：

```text
最终 prompt = 全局预设 preset_prompt
            + 每个角色预设（image_prompt.md 和 character.toml 的 [image] preset_prompt）
            + 参考图一致性说明（用了参考图时）
            + 场景描述（prompt 参数，可能已被规则库/语义改写处理过）
            + 画师串（本次传了 artist_preset_id / artist_string 时）
            + 多角色同框约束（索引了 2 个及以上角色时）

negative_prompt = 本次 negative_prompt + 规则库补的负面词 + 全局 negative_prompt + 各角色 negative_prompt
```

- 角色预设只在"指定了 `character_id` / `character_ids`"或"本轮用 `@char:<id>` 启用了角色"时加入；
- 多角色时每个角色块会带 `角色 名称（id）：` 标签；
- 全局预设用来放统一画风/画质/安全词，角色预设用来放人物外貌、发色、服装、画风一致性关键词。

**负向提示词是独立字段，不是拼在正向提示词里的。** 加权标签模型会把 `avoid: blurry` 当成"要画模糊"的请求，所以默认由 `negative_prompt_mode = "field"` 单独发送。需要旧行为（把负面词折成 `avoid: ...` 追加到正向提示词末尾）时设成 `inline`；后端完全不接受负面词时设成 `off`，此时负面词会被丢弃并在工具结果里给出警告，而不是让它看起来像生效了。

**超长时不会硬截断。** 超过 `max_prompt_runes`（默认 4000）时按优先级**整体丢弃**片段：先丢参考图一致性说明，再丢全局预设和角色预设，最后才动场景描述；画师串与多角色同框约束受保护，永不丢弃。原子标签和权重表达式永远不会被切成半截，所以不会出现发给上游的 `(artist_a:1.` 这种残缺表达式。连受保护片段都放不下时，工具会返回一条可读错误，提示你缩短描述或调大上限。

**触发角色预设的方式：**

1. 模型显式传单个角色：

```json
{"prompt": "在雨里的霓虹街道回眸", "character_id": "catgirl"}
```

2. 模型显式传多个角色：

```json
{"prompt": "两人在雨夜街道同框", "character_ids": ["catgirl", "foxgirl"]}
```

3. 消息里先启用一个或多个角色（推荐，确定性自动带上）：

```text
@char:catgirl @char:foxgirl 画一张两人站在雨里的霓虹街道同框
```

`@char:` 激活的角色会随本轮上下文传给 `image_generate`。默认情况下，所有被索引的角色都会写进**同一张图**；只有当工具参数 `count > 1` 时，才会生成多张图，并且每张图仍然包含全部被索引角色，不会把角色拆到不同图片里。

参考图还支持多张：

```json
{
  "prompt": "两人同框",
  "character_ids": ["catgirl", "foxgirl"],
  "reference_images": ["catgirl:avatar.png", "foxgirl:avatar.png"]
}
```

`reference_images` 每项可以是 `media:<sha256>`、角色图片名，或多角色时的 `<character_id>:<图片名>`。是否支持多张参考图取决于中转站；开启 `supports_reference = true` 后，单张会按原格式发送，多张会以数组发送到 `reference_field` 指定的字段。

## 内置提示词优化（rules）

`image_generate` 内置了从 `GPT_Image_Prompts_大全.xlsx` 转换来的规则库（16 个大类、226 条场景提示词、15 个万能模板、关键词词库、10 组负面提示词、10 个用途比例方案、50 条进阶技巧），会在调用生图服务前对场景描述做**针对性补全**。

它做四件事：

1. **识别用途**：从「参数速查」匹配头像 / 壁纸 / 海报 / 电商主图 / 封面 / 详情页等，补推荐比例 + 用途补充句；
2. **识别大类与场景**：从「全部提示词汇总」匹配最接近的一条，提取它的英文固定短语作为画风/镜头/光线锚点；
3. **补负面词**：按「负面提示词」类别选择性追加 `Avoid: ...`（有人物 → 人体结构/面部/重复；有文字 → 文字与标识；产品/海报 → 构图；始终包含画质与安全合规）；
4. **编辑保护**：识别"换背景 / 局部修改 / 扩图 / 修图"等意图时追加 `Keep the subject, pose, composition, lighting, and background unchanged; do not redraw the whole image`；使用参考图时追加角色一致性要求。

配置：

```toml
[image_generation]
optimize = "rules"          # off 关闭，rules 使用内置规则库
optimize_term_mode = "phrase"  # phrase：短语锚点；tag：单个单词 tag 串
optimize_max_anchors = 4
optimize_max_negatives = 10
optimize_max_added_runes = 400
optimize_max_tags = 12
```

工具参数 `optimize` 可以按次覆盖。优化器**只追加、不重写**用户原句，原句永远在最前面；总追加长度受 `optimize_max_added_runes` 限制。

示例：

```text
输入：给猫娘画一个头像
输出：给猫娘画一个头像 1:1 square composition, Square avatar, centered face, clean background, ultra-detailed
负面：low quality, blurry, out of focus, bad anatomy, extra fingers, ...

输入：赛博朋克城市夜景，16:9
输出：赛博朋克城市夜景，16:9 Cyberpunk city street, neon signs, holograms, flying cars
（已有比例时不会再补比例）
```

### 两种输出模式

`optimize_term_mode` 控制追加内容的形态，工具参数 `term_mode` 可按次覆盖：

| 模式 | 追加内容 | 示例 |
| --- | --- | --- |
| `phrase`（默认） | 用途补充句 + 短语锚点 | `Square avatar, centered face, clean background, Watercolor illustration, loose brush strokes` |
| `tag` | 比例 + 单个单词 tag 串 | `1:1, centered, face, avatar, square, ultra-detailed` |

tag 模式的选词顺序：

1. 命中场景提示词的**单词表**（按词频 + 相关性排序，如 `neon / city / rain / holograms`）；
2. 用途补充句里的有效单词（如 `square / avatar / centered / face`）；
3. `画质` 词库里 1-2 个风格中性的词（如 `ultra-detailed`、`8k`）；
4. 比例（如 `1:1`、`16:9`）。

tag 模式**不会**从风格/光线/色彩/构图词库里乱挑词，避免出现"水彩 + photorealistic"这种自相矛盾；命中不了场景时只加画质词 + 编辑保护句。原句仍然保持在最前面。

实际示例：

```text
输入：a cyberpunk street at night
tag ：a cyberpunk street at night neon, city, rain, signs, crowded, holograms, ultra-detailed

输入：给猫娘画一个头像
tag ：给猫娘画一个头像 1:1, centered, face, avatar, square, ultra-detailed

输入：水彩风格的森林小路
tag ：水彩风格的森林小路 background, illustration, white, brush, watercolor, loose, ultra-detailed

输入：局部修改：把背景换成沙滩
tag ：局部修改：把背景换成沙滩 Keep the subject, pose, composition, lighting, and background unchanged; do not redraw the whole image
```

> 规则库只能做关键词/短句匹配，不做翻译和意图理解。中文口语化、复合意图（例如"猫娘在海边吃冰淇淋"）主要靠匹配到的类别锚点兜底；如果你需要更强的改写，可以在此基础上再加一层 LLM 优化（见下方"后续可扩展"）。

### 数据拆分层级

转换脚本对原表格做了三层拆分：

| 层级 | 说明 | 示例 |
| --- | --- | --- |
| 条目级 | 按逗号把同格的多个标签拆成独立条目 | `low quality, blurry, out of focus` → 3 条负面词 |
| 短语级 | 英文提示词按逗号拆成静态短语锚点 | `soft natural light`、`shallow depth of field` |
| 单词级 | 每条提示词/关键词/负面词再分词，去停用词后建单词索引 | `soft`、`natural`、`light`；`keyword_terms`、`entries[].terms`、`vocabulary` |

- `keywords` / `negatives[].items` 是条目级，可直接当标签用；
- `entries[].anchors` 是短语级，优化时优先使用（语义完整）；
- `entries[].terms`、`keyword_terms`、`vocabulary` 是单词级，用于匹配用户输入和补齐锚点空位；
- 单词级只作为补充：优先放短语，单词最多再补 2 个，并且会过滤 `photo/light/style` 这类通用词，避免把语义拆碎后堆砌。

### 更新规则库

规则库是预生成的 JSON，产物在 `atri_x/imagegen/prompts/library.json`。它的源数据（`.xlsx` 提示词大全）与生成脚本属于上游 Go 仓库的 `scripts/` 目录，本仓库只消费生成物：

```bash
# 生成脚本在上游 Go 仓库（ATRI-X）的 scripts/convert_image_prompts.py，本仓库不重复实现；
# 需要重建时在上游仓库运行它，把产物写回 atri_x/imagegen/prompts/library.json。
```

改完下次启动即生效（规则库 JSON 由 `atri_x/imagegen/library.py` 在运行时读取，不再打进二进制）。

### 后续可扩展

当前是纯本地规则，不调用模型。如果需要"语义级改写"（长句 → 结构化英文提示词、中文 → 英文、复合意图拆解），可以在 `OptimizeResult` 之后再加一次 LLM 调用：用匹配到的模板/关键词作为参考，让配置的模型重写，然后与全局/角色预设合并。这一步需要指定一个模型槽位（建议用 compact/naming 这类低成本模型），需要时再加。

## 画师串（artist string）

"画师串"指一组画师标签，例如 `(artist_a:1.1), (artist_b:0.8)`。ATRI-X 把它当**结构化数据**处理，而不是一段可以随便改写的文本。

### 三种来源不能混为一谈

| 来源 | 怎么得到 | 能说什么 |
| --- | --- | --- |
| `text` | 用户直接给出标签串 | 「你给的串是……」，确定性解析，不调用模型 |
| `metadata` | 图片自己记录的生成参数 | 「图片元数据记录的画师串是……」，这是原始证据 |
| `visual_recommendation` | 视觉模型看画面 + 本地串库检索 | 「风格相近的组合是……」，**不是**原始画师串还原 |

同一画风可能来自不同模型、LoRA、混合标签或后期处理，所以第三种来源永远不会被写成"原始画师串"。工具返回的 `source` 和 JSON 里的 `original_artist_string`（没有证据时为 `null`）用来区分它们。

### 用法一：从图片里读出原始画师串

```json
{"image": "media:<sha256>"}
```

默认 `visual_fallback = false`：只读图片自身记录的生成参数，**不消耗视觉额度**。找到就返回原文，找不到就明确说明原因。

> **入库转码这件事**：超过 4 MiB 或长边达到 4096 像素的图片在入库时会被转码成 JPEG（`[media].llm_image_compression_threshold_bytes` / `llm_image_max_length`），那一步会永久销毁 PNG 文本块与 EXIF/XMP。因此生成参数的采集发生在**转码之前**——开启下面的留档后，转码过的图仍然取得到参数。常见的 A1111 / NovelAI / ComfyUI 出图本来就在阈值以内，不开留档也能直接读到。

支持的参数格式：PNG `tEXt` / `iTXt` / `zTXt`、A1111 `parameters`、NovelAI、ComfyUI（按 KSampler 的正负向链接归属）、InvokeAI，以及 JPEG 的 EXIF `UserComment` / XMP 和 WebP 的 `EXIF` / `XMP `。

NovelAI 的方言判定同时看 `Comment` 的键集、`Software` 和 `Source`：第三方客户端会把 `Software` 改写成自己的名字（实测有写成 `Plana` 的），只看 `Software` 会漏掉这类图，而 `Source` 在 `Comment` 丢失或损坏时仍然可用。模型身份从 `Comment` 的 `model_name` / `model_hash` 取出，报告为 `model` / `model_hash`。

> **`signed_hash` 不是可信度证明**：NovelAI 的 `Comment` 里带 `signed_hash` 字段，但**字段存在不等于验签通过**——重编码过的图（含第三方客户端改写后的图）签名必然对不上。ATRI-X 不做验签，只把这段原文留在 `text` 里，也不把它当生成参数。所以读到的提示词与参数可以用，但它们与画面的一致性没有密码学背书。

### 用法二：把串存成组合，下次直接引用

```json
{"name": "柔和粉彩", "scope": "group",
 "artists": [{"tag": "artist_a", "weight": 1.1}, {"tag": "artist_b", "weight": 0.8}],
 "raw_string": "(artist_a:1.1), (artist_b:0.8)",
 "style_description": "纤细线条、粉彩配色"}
```

存好之后生图时直接引用：

```json
{"prompt": "在雨里的霓虹街道回眸", "artist_preset_id": "soft-pastel-2f1a"}
```

- 顺序就是最终画师串的顺序，不会重新排序，也不会去重；
- `raw_string` 按你给的样子保存；
- 更新必须带上次读取拿到的 `expected_revision`，否则拒绝而不是覆盖别人的修改；
- `scope=user` 只写自己的个人库，`scope=group` 写当前群，`scope=global` 仅超级管理员；这三个身份由服务端从调用上下文决定，模型传什么 `scope_id` 都不作数；
- `status=pending` 的组合只是候选，不会自动用于生图，也不会被推荐检索命中。

### 用法三：没有参数时按画风推荐

```json
{"image": "media:<sha256>", "visual_fallback": true}
```

这会用结构化分析模板额外调用一次视觉模型（**计入视觉额度**），拿到线条/上色/光影等风格特征后在**本地串库里检索**，返回相近的组合。模型被明确要求不得猜测画师名或作者身份——猜测没有证据价值，只会污染串库。推荐结果可以存成 `status=pending` 的候选，人工确认后再改成 `approved`。

### 方言：同一组标签，不同后端写法不同

`prompt_dialect` 决定画师串怎么渲染：

| 方言 | 渲染结果 | 说明 |
| --- | --- | --- |
| `natural`（默认） | `画风参考：artist_a、artist_b` | 自然语言目标不能表达权重，权重会转成文字强调并给出警告 |
| `sd_weighted` | `(artist_a:1.1), (artist_b:0.8)` | SD 加权标签 |
| `nai` | `{artist_a}, [artist_b]` | 按 NovelAI 的括号阶梯；落在阶梯外或超过 5 层时改用 `(tag:weight)` 并提示按版本核对 |
| `tags` | `artist_a, artist_b` | 纯标签，权重被忽略并给出警告 |

方言不匹配时**不静默丢权重**：要么给出警告，要么返回错误，所以你总能区分"权重生效了"和"权重被忽略了"。

### 串库存在哪

`[image_generation.artist_library].root` 指定一个 JSON 文件（默认在配置目录下），写入走临时文件 + 原子替换，并用一个整数 revision 仲裁并发编辑，因此不会写出半个文件。文件在配置目录里，`deploy/backup.py` 会随 `data/` 一起备份。不配置 `root`（或 `enabled = false`）时，`artist_extract` 的文本解析与元数据读取照常可用，只是 `artist_library_*` 四个工具不会被注册。

### 生成参数留档

图片是在**入库**时就可能被转码的，而转码会永久销毁参数。要让参数不丢，采集就必须早于转码：

```toml
[image_generation]
metadata_store = "image_metadata.json"   # 相对 app.toml/services.toml 所在目录；留空即关闭
```

开启后：

- 每一张入库图片的原始字节都在转码**之前**交给留档，以媒体 ID（`media:<sha256>`，即现存对象的哈希）为键写入记录。非图片不解析；图片没有生成参数则不写任何记录。存的是**解析出来的参数**，不是原图本身。
- 记录里同时保存原始字节的 SHA-256、原始与存储的 MIME / 大小，以及这张图是否被转码。所以"这份参数是从留档读来的，还是图片自己带的"永远分得清：`artist_extract` 与 `image_to_prompt` 都会给出 `metadata_origin`，`archive` 表示留档、`embedded` 表示图片自身。
- 读取规则是**按需取用**：图片被转码过（本体已不含参数）时用留档；没被转码时先解析图片本身、留档只作兜底——这样旧版本解析器写下的记录不会一直作答。两种情况的来源都会如实标出来。
- 媒体被删除时对应记录同步删除，文件大小跟随存活媒体而不是历史累计；另有 10000 条上限，超出按采集时间淘汰最旧的记录。被淘汰的记录无法恢复，所以上限是安全网而不是常态。
- 重复入库同一张图不会重复写盘。留档文件损坏时不会静默清空重来，而是报错并原样保留文件。
- 写盘失败时**不会**假装成功：记录不会进入可读状态，工具会退回到"读图片本身"并如实报告没找到参数（只留一条警告日志）。因此配置目录只读、磁盘写满这类情况下你看到的是"取不到参数"，而不是"参数已留档"却在下一次重启后消失。
- 留档文件写在配置目录（`deploy/data/config/atri-x/`），因此 `deploy/backup.py` 会随 `data/` 一起备份，不需要改备份清单。里面是明文提示词与采样参数，与图片本体一样按用户数据对待。
- **必须指向独立文件**：与其他持久化文件（`app.toml` / `services.toml` / `state.toml` / SQLite 库 / 常驻记忆 / 角色库）同路径会在启动阶段直接报错，不会等到第一次写入才出事。
- 留档文件每次写入都会整份重写，且记录保留原始文本块（ComfyUI workflow 可能几十 KB）。正常情况下记录随媒体删除而减少，文件规模跟随存活媒体；如果长期累积，值得看一眼它的大小。
- 留档只在**单进程**下安全：没有文件锁，两个 ATRI-X 实例共用一个配置目录会互相覆盖（`python -m atri_x run` 不占用服务单实例锁）。这与"ATRI-X 是单实例、有状态服务"的既有约束一致——两个实例共用同一份数据目录本来就会破坏媒体数据库。
- 默认关闭。关闭时行为与以前一致：只读图片自己的字节；读不到时的提示会指出可以开启留档，而不再断言参数已经无从恢复。
- 开启只对**之后**入库的图片生效：在开启之前就被转码的图已经无从恢复，而当时未被转码的图参数仍原样留在文件里，随时读得到。

## 角色侧配置

在角色文件夹里加图片预设：

```text
characters/catgirl/
  image_prompt.md        # 推荐：自由文本外貌/画风描述
  character.toml         # [image] 结构化预设
  images/avatar.png      # 可作为参考图
```

```toml
[character]
id = "catgirl"
name = "猫娘"
# ...

[image]
preset_prompt = "银白色短发，猫耳，琥珀色眼睛，黑色露肩上衣"
negative_prompt = "extra fingers, blurry"
size = "1024x1536"       # 覆盖全局 size
quality = "high"
references = ["avatar.png"]  # 作为参考图的角色图片名
```

也可以让模型通过 `character_manage` 写入：

```json
{
  "operation": "update",
  "id": "catgirl",
  "docs": {"image_prompt": "银白色短发，猫耳，琥珀色眼睛"},
  "image": {"negative_prompt": "extra fingers", "references": ["avatar.png"]}
}
```

## 自动编排（mode = auto）

`image_generate` 默认 `mode = "auto"`，会按需自动组合四件事：

| 能力 | 触发条件 | 说明 |
| --- | --- | --- |
| **自动选角** | prompt 里出现多个角色名/别名，或传了 `character_id: "auto"` / `character_ids` / `character_query` | 命中后自动带上所有命中角色的图片预设和参考图，合并到同一张图 |
| **自动拉群聊上下文** | prompt 里出现"刚才/上一条/那张图/群里/大家"等指代词 | 从当前群历史检索相关消息，作为参考对话；写进 rewrite 输入，未做改写时追加到 prompt |
| **TAG 库优化** | `optimize = "rules"` | 自动补用途比例、画风锚点/单词 tag、负面词 |
| **LLM 语义改写** | `optimize_rewrite = "auto"` 且 prompt 太短（默认 < 40 字）或拉了群聊上下文 | 用 `optimize_rewrite_model` 指定的低成本模型改写；失败自动回退到规则结果 |

`mode = "manual"` 时只用显式参数，不自动选角/拉上下文/改写。

新增配置：

```toml
[image_generation]
optimize_rewrite = "auto"          # off / auto / always
optimize_rewrite_model = "naming"  # naming / compact / chat / work
optimize_rewrite_min_runes = 40
auto_character = true
auto_context = true
context_default_limit = 6
```

### 让模型显式查 TAG 库

新增了 `prompt_library_search` 工具，可以按关键词查内置词库：

```json
{"query": "cyberpunk", "scope": "entry", "limit": 3}
```

返回命中条目的 `anchors`、`terms`、关键词分组、负面词、用途比例和进阶技巧。适合模型在写 `prompt` 前先查"赛博朋克该配什么镜头/光线"。

## 工具参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `prompt` | 是 | 画面/场景描述；不要重复角色外貌，会由角色预设自动拼接 |
| `character_id` | 否 | 指定单个角色；不填时使用本轮 `@char:<id>` 启用的角色 |
| `character_ids` | 否 | 指定多个角色；默认所有角色画进同一张图，最多 4 个 |
| `size` | 否 | 如 `1024x1024`、`1536x1024`、`1024x1536`；优先级：参数 > 第一个角色 `[image].size` > 全局 |
| `quality` | 否 | `low` / `medium` / `high`；优先级同上 |
| `reference_image` | 否 | 单张参考图：`media:<sha256>`、角色图片名，或多角色时 `<character_id>:<图片名>`；需要 `supports_reference = true` |
| `reference_images` | 否 | 多张参考图；每项同上，多个角色时建议用 `<character_id>:<图片名>` 标明归属 |
| `count` | 否 | 强制生成张数，默认 1，单次最多 4；每张都包含全部已索引角色 |
| `save_to_character` | 否 | 是否写回角色 `images/`；多角色时写回第一个角色；默认跟 `save_to_character` 配置 |
| `send` | 否 | 是否直接发到当前聊天；默认 `false` |
| `mode` | 否 | `auto`（默认）/ `manual` |
| `character_query` | 否 | `character_id: "auto"` 时的检索词；不填用 prompt |
| `context_query` | 否 | 拉群聊上下文的关键词；`auto` 从 prompt 提取，`off` 关闭 |
| `context_limit` | 否 | 拉取条数，默认 6 |
| `rewrite` | 否 | 本次是否做 LLM 语义改写 |
| `artist_preset_id` | 否 | 指定一个画师串组合（来自 `artist_library_search`）；作为受保护片段拼进最终提示词，不会被语义改写改动 |
| `artist_string` | 否 | 本次临时使用的画师串原文，顺序和权重原样保留；比 `artist_preset_id` 优先 |
| `artist_mode` | 否 | `off`（默认，不加画师串）/ `explicit`（用上面两个参数或配置的 `artist_default_preset`）/ `auto`（按场景从串库自动选一个**已确认**的组合） |
| `negative_prompt` | 否 | 本次追加的负面提示词，与规则库、全局和角色的负面词一起按 `negative_prompt_mode` 传递 |
| `dry_run` | 否 | 只编译并显示最终正向/负向提示词、画师串、方言和丢弃的片段；不调用生图接口、不保存、不发送、**不计生图额度** |

`artist_mode` 默认 `off`：不显式要求就不会给图片加画师串，已有部署的产出不会因此改变。

`count` 通过最多 4 个并发子请求实现，每个上游请求仍是 `n = 1`；这样不依赖中转站是否支持 `n > 1`，每张图也都会包含全部被索引角色。

## 出图之后

- 图片导入 Media Center，返回图片段，模型能直接看到结果；`count > 1` 时会返回多张图片段，每张图都包含全部被索引角色；
- `save_to_character = true` 且解析到角色时，图片写回第一个角色的 `characters/<id>/images/`，并在 `character.toml` 的 `[[images]]` 里登记 `media:<sha256>`；
- `send = true` 或 `send_by_default = true` 时会把图片发到当前会话（群聊可用）。

## 中转站兼容性说明

`image_generate` 发送的是 OpenAI 风格的 JSON：

```json
{
  "model": "gpt-image-2.5",
  "prompt": "...",
  "n": 1,
  "size": "1024x1024",
  "quality": "high",
  "output_format": "png"
}
```

响应支持两种常见形态：

```json
{"data": [{"b64_json": "..."}]}
{"data": [{"url": "https://..."}]}
```

- 返回 `url` 时会自动下载（带超时和 20MB 上限）；
- 单张参考图按 `reference_field` 发送为字符串；`reference_images` 有多张且 `supports_reference=true` 时，同一字段会发送为 data URL 数组；中转站若不支持数组，请每次只传一张参考图。
- 非 2xx 会读取 `error.message` 作为错误信息；
- 中转站如果有额外字段（`aspect_ratio`、`seed`、`watermark` 等），用 `extra_payload` 透传；需要额外请求头用 `extra_headers`；
- GPT Image 系列有的中转不接受 `response_format`，默认不发送；只有中转站明确要求时才配置 `response_format = "b64_json"`；
- `quality` 的可选值以中转站为准，如果报错可先改成 `medium` 或删掉该字段（用 `extra_payload` 覆盖为空）。

## 权限与费用

- `superadmin_only = true`（默认）时，只有 `[security.superadmins]` 里的用户能调用；
- 设为 `false` 后工具风险是 `low`，默认 `[security] user_max_tool_risk = "low"` 下普通用户即可调用；建议配合群级 `image-quota` / `user-image-quota` 一起使用；
- 建议配合中转站侧的额度和限速一起使用；单次调用失败会以工具结果形式返回，不会影响 Session。

## 排错

| 现象 | 原因 |
| --- | --- |
| `生图服务未启用` | `services.toml` 的 `[image_generation] enabled = false`（旧部署为 `app.toml`）或没重启 |
| `缺少 API Key` | `IMAGE_API_KEY` 没设置，或 `.env` 没被加载 |
| `生图服务返回 404` | `base_url` 少/多了 `/v1`；用 `endpoint` 直接写全路径 |
| `生图服务返回 400` | `quality` / `size` / `output_format` 不被中转支持；用 `extra_payload` 调整 |
| `没有返回图片` | 中转返回结构不是 `data[].b64_json` / `data[].url` |
| 结果写回角色失败 | 角色不属于调用者；超级管理员或角色拥有者才能写 |
| `最终提示词 N 个字符，超过 max_prompt_runes` | 自动补充的片段已全部丢弃仍放不下；缩短场景描述或画师串，或调大 `max_prompt_runes`（不会按字符硬截断，以免切坏权重表达式） |
| `没有找到生成参数` | 出图工具没写参数，或图片经过截图/二次编辑/平台转存。超大原图还会在入库时被转码成 JPEG，此时只有**开启 `metadata_store` 后重新发送原始文件**才能留住参数：留档发生在转码之前，而在开启之前入库的图已经无从恢复 |
| `画师串库未启用` | 没有配置 `[image_generation.artist_library].root`（或 `enabled = false`）；`artist_extract` 的文本解析仍可用 |
| `画师串组合 X 已被修改` | 你提交的 `expected_revision` 已过期；先 `artist_library_read` 读最新内容再保存 |
| `方言「自然语言」不能表达……权重` | 正常提示：自然语言/纯标签方言无法表达权重，权重被转写或忽略。换 `prompt_dialect` 或接受这个转换 |
