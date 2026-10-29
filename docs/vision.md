# 识图（vision）能力

ATRI-X 的识图能力只有**一个描述引擎**（`atri_x.vision.Service`），内置工具
`image_to_prompt` 与聊天里的图片处理共用它、同一份缓存与同一套超时/凭据规则。
本文描述当前实现的行为与配置，不描述内部调用链（那是 `devdocs/` 的事）。

> 移植状态：Go 版有描述引擎与聊天兜底，没有两段式、`detail`、以图搜图、隐私开关
> 与失败分类；五者都是 Python 侧新增，见 [`PORTING.md`](../PORTING.md) 的差异说明。

<!-- locator:vision -->
## 1. 两段式：先描述属性，再让主模型下结论

视觉模型只回答**画面里看得到什么**，不下任何身份/作品结论：

1. 第一段用「画面属性描述」模板（`atri_x.vision.image_attributes_prompt`）要求
   视觉模型输出一个紧凑 JSON：`hair_color` 发色、`eye_color` 瞳色、
   `hairstyle` 发型、`outfit_colors` 服装配色、`trim` 滚边纹样、`badges` 徽章
   标志、`accessories` 配件、`visible_text` 可见文字、`art_style` 画风倾向、
   `ai_generated` 是否疑似 AI 生成、`scene` 构图与背景、`character_count` 人物
   数量、`identity_confidence`（`low`/`medium`/`high`，表示「能否仅凭画面确定
   具体角色」）、`description` 自然语言描述。
2. 第二段由**主模型**拿着这段文字，结合人设与上下文回答。

提示词硬约束：只描述可观察特征；不猜作品、角色与画师；不补充画面里不存在的
细节；支持中文与英文（`language = zh|en`）。

属性解析是**宽容**的：某个字段缺失或类型不符只按空值处理，全部字段都空才算
「没读出东西」（记 `parse_failed`，并退回把模型原文当描述）。以图搜图兜底见 §3。

## 2. 图片以原图细节发出（`detail=original`）

图片段可以带一个 `detail` 字段（`MessageSegment.detail`），非空时才会写进请求：

* chat-completions：`{"type":"image_url","image_url":{"url":...,"detail":"original"}}`
* Responses：`{"type":"input_image","image_url":...,"detail":"original"}`

`view_image` 与视觉服务**默认** `detail = "original"`。刻意不默认 `low`：
被上游降采样后，发色、纹样、徽章、画面文字这些第一段要靠的证据会先丢在第一步。
`detail` 为空串表示**不写这个键**，因此旧数据与 Go 落库数据保持逐字节兼容。

单次请求可以覆盖服务的默认档位（`VisionRequest.detail`；agent 侧的
`vision_detail` 会填进这个字段），所以「聊天用 original、工具用 low」这类组合
不需要两个服务实例。注意 `detail` **不进缓存键**：同一张图 + 同一个模板只会
缓存一份描述，改档位不会自动让旧条目失效（要强制刷新就改 prompt 版本）。

## 3. 以图搜图兜底（SauceNAO，默认关闭）

只有**同时**满足下列条件才会把图发到 SauceNAO 查一次：

* `identity_enabled = true`；
* 有 API key（按 `identity_api_key_env` 读进程环境变量，或配置目录 `.env` 里的同名变量，
  默认 `SAUCENAO_API_KEY`；**不要把 key 写进 `services.toml`**）；
* 当日配额未用尽（UTC 日切，进程内计数 + 可选 JSON 持久化）；
* 用户**本轮**文本明确在问身份（如「这是谁」「什么角色」「出自哪」「who is this」
  「which character」「source of this image」）；
* 第一段给出的 `identity_confidence = low`（画面本身定不了是谁）。

结果只作为参考追加，格式固定为
`[以图搜图参考] 相似度 …；标题；作者；来源：（外部服务，仅供参考）`。
失败（未开启 / 超限 / 上游错误）**只记原因**，不编造作品名或角色名。API key 只出现
在 multipart 请求体里，不进日志、不进审计字段、不进任何返回文本。

> 未联网实测：本仓没有 SauceNAO 的真实 key，因此只用离线 fake 覆盖了请求构造、
> 响应解析、每日配额与失败分类；真实网络行为需在有 key 的环境里复核。

## 4. 超时降级

聊天前置描述受一条**短超时**约束（`probe_timeout_seconds`，默认 4 秒，夹到
1~30 秒）。超时或失败时图片段被替换成一句只写原因的文字：

```
图没取到（原因：超时 / 下载失败 / 格式不支持 / 超限 / 识别失败）
```

绝不编造画面内容——「没取到」比「看起来像什么」更诚实。

## 5. 隐私开关

两个独立开关，**都不改默认行为**（默认都允许外发）：

| 开关 | 作用 |
|---|---|
| `allow_external_images` | 总开关：关掉后任何图片都不发往上游 |
| `allow_group_images` | 群聊开关：关掉后群聊作用域里的图片不外发 |

`false` 时：

* **服务层**在打开任何上游连接之前就抛 `PrivacyBlockedError`
  （不建连、不解码、不消耗额度、不消耗以图搜图配额）；
* 聊天前置路径把图片段替换成 `[图片未外发：外部识图已关闭]`；
* 工具路径（`view_image` / `image_to_prompt`）返回可读错误而不是静默失败。

工具路径不经过视觉服务（`view_image` 直接把图片段交给主模型），所以它们查同一个
进程级配置（`atri_x.vision.configure_privacy`），装配层在启动时设置一次即可。

## 6. 失败可观测

`Service.stats()` 增加 `failures` 计数（JSON 里的 `failures` 键），取值是有界枚举：

`download_failed`、`unsupported_format`、`too_large`、`timeout`、`cancelled`、
`provider_error`、`privacy_blocked`、`empty_result`、`parse_failed`、
`identity_disabled`、`identity_quota`、`identity_error`。

分类只看异常类型与 `ErrorCategory`，不看人类可读的 message；键里没有 media ID、
图像字节、URL 或凭据，因此可以安全地对外暴露。下载/读字节失败发生在服务之外，
由聊天路径按同一套类别回填。传了 `logger` 时每次失败记一行
（`kind=… error=<异常类名>`，不记 message，避免把凭据带出去）。

## 7. 配置字段

`[vision]` / `[image_to_prompt]` 段新增（由装配层从 `services.toml` 读入
`VisionOptions`，全部带默认值，旧配置不需要改）。`[image_to_prompt]` 只用到其中的
`detail` 与 `allow_external_images`（它是工具路径，没有聊天触发/以图搜图的概念）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `detail` | `"original"` | 发图片段的细节档位；空串表示不带该键 |
| `probe_timeout_seconds` | `4.0` | 聊天前置描述的短超时（1~30 秒） |
| `allow_external_images` | `true` | 外部识图总开关 |
| `allow_group_images` | `true` | 群聊识图开关 |
| `identity_enabled` | `false` | 以图搜图兜底开关 |
| `identity_api_key_env` | `"SAUCENAO_API_KEY"` | 读取 key 的环境变量名（也可写进配置目录 `.env`） |
| `identity_daily_limit` | `100` | 每日上限（UTC 日切，`0` 表示不限额） |
| `identity_timeout_seconds` | `8.0` | 单次检索超时 |
| `identity_min_similarity` | `0.7` | 候选相似度下限 |
| `identity_state_path` | `""` | 每日计数的 JSON 持久化路径；留空只记进程内 |

Agent 侧装配（由装配层通过 `getattr` 读取，缺省完全不改变行为）：
`vision_trigger`（`"on_error"` 默认 / `"always"` 两段式前置描述）、
`vision_probe_timeout_seconds`、`vision_allow_group_images`、`vision_detail`、
以及可选的 `vision_language`。

工具路径（`view_image` / `image_to_prompt`）不经过视觉服务的配置，它们读进程级
开关，因此装配层启动时应调用一次
`atri_x.vision.configure_privacy(allow_external_images=…, allow_group_images=…)`；
不调用时两个开关都是 `true`（与改造前一致）。

## 8. 相关测试

```bash
python -m unittest tests.test_vision tests.test_vision_threading \
    tests.test_vision_attributes tests.test_vision_identity \
    tests.test_vision_privacy tests.test_vision_prechat -v
```

`tests/test_vision_prechat.py` 是两段式的行为级用例（默认 `on_error` 不动消息、
超时/下载失败降级、群聊与外发开关、以图搜图接入点）；
`tests/test_vision_identity.py` 全程离线（HTTP 用 fake 替代）。
