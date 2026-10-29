# 角色素材库

ATRI-X 内置一个按文件夹管理的角色素材库，用来保存角色设定文本和角色图片，并提供工具与命令做索引检索和一键启用。

## 目录结构

角色库存放在 `[character_library].root` 指向的目录，默认是 `app.toml` 同级的 `characters/`：

```text
characters/
  index 由进程内存维护，不需要手工创建
  catgirl/
    character.toml        # 元数据 + 图片索引
    profile.md            # 人设（@char 注入的就是它）
    world.md              # 世界观、背景（可选）
    greeting.md           # 开场白（可选）
    examples.md           # few-shot 示例（可选）
    image_prompt.md       # 生图预设（可选，供 image_generate 自动拼接）
    notes/
      招式.md             # 任意补充素材
      口头禅.md
    images/
      avatar.png          # 角色图片原图
  assistant/
    character.toml
    profile.md
```

每个角色一个文件夹，文件夹名就是角色 id：只允许小写字母、数字、`-`、`_`、`.`，以字母或数字开头，最长 64 位。

## character.toml

`character.toml` 由 ATRI-X 自动维护，也可以手工编辑；进程会在最多 3 秒内自动重新扫描：

```toml
[character]
id = "catgirl"
name = "猫娘"
aliases = ["小喵", "Neko"]
description = "一只黏人的猫娘助手"
tags = ["anime", "assistant"]
owner_platform = "qqonebot"   # 空表示 system（超级管理员维护的公共角色）
owner_id = "qqonebot:10001"
visibility = "private"        # public 或 private
version = "3"                 # 素材版本，更新时自动递增，可显式指定
source = "import"             # 素材来源，如 import / local / qqonebot
created_at = "2026-10-01T12:00:00Z"
updated_at = "2026-10-01T12:00:00Z"

[[images]]
name = "avatar.png"
mime_type = "image/png"
media_id = "media:<64 位小写 sha256>"
path = "images/avatar.png"
size = 123456
version = "1"
source = "qqonebot"
created_at = "2026-10-01T12:00:00Z"
```

文档文件直接写 Markdown 即可，ATRI-X 会读取 `profile.md`、`world.md`、`greeting.md`、`examples.md`、`image_prompt.md` 和 `notes/*.md`。

角色的生图预设还可以写在 `character.toml` 的 `[image]`：

```toml
[image]
preset_prompt = "银白色短发，猫耳，琥珀色眼睛"
negative_prompt = "extra fingers, blurry"
size = "1024x1536"
quality = "high"
references = ["avatar.png"]
```

配合 [生图服务](image-generation.md)，`@char:<id>` 启用角色后调用 `image_generate` 会自动带上这些预设。

## 可见性与所有权

| 角色 | 谁能看 | 谁能改 / 删 |
| --- | --- | --- |
| `visibility = "public"`（或目录由超级管理员创建且无 owner） | 所有人 | 超级管理员 |
| `visibility = "private"` 且 owner 是自己 | 自己 + 超级管理员 | 自己 + 超级管理员 |
| `visibility = "private"` 且 owner 是别人 | 超级管理员 | 拥有者 + 超级管理员 |

- 普通用户创建的角色默认是 `private`，并且只能创建 / 保留 `private`。
- 超级管理员可以创建属于 system 的公共角色，也可以指定 `owner_platform` / `owner_id`。
- 查看者身份由当前平台用户决定，无法通过提示词绕过。

## 工具

| 工具 | 风险 | 作用 |
| --- | --- | --- |
| `character_list` | low | 列出可见角色（公开 + 自己的私有），支持按 tag / 关键词过滤 |
| `character_read` | low | 读取 `profile` / `world` / `greeting` / `examples` / `notes/<name>`，可选返回角色图片 |
| `character_search` | low | 按名称、别名、tags 和正文检索，返回命中的角色及片段 |
| `character_manage` | low（OwnerScoped） | 创建、修改自己的角色；写文本、加图、删图。普通用户只能管自己的私有角色 |
| `character_delete` | high（OwnerScoped） | 永久删除自己的角色；普通用户会走高风险确认流程 |
| `character_import` | low（OwnerScoped） | **一步导入**：把一段人设文本直接保存成角色卡（写 `profile.md`），可顺带启用（`activate=member` 绑定为当前成员的专属人设；`activate=soul` 设为全局人设，需超管） |
| `character_activate` | high | 把某张角色卡写成**全局系统人设** `SOUL.md`（`activate` / `restore` / `status`，仅超级管理员；写前自动备份到 `SOUL.md.bak`） |
| `persona_control` | low（OwnerScoped） | 成员自己管理**专属人设**：`status` / `bind` / `enable` / `disable` / `clear`；关闭后恢复默认 `SOUL.md`，只影响他本人 |

`character_manage` 的 `operation` 取值：

- `create` / `update`：写元数据和 `docs`（key 为 `profile`、`world`、`greeting`、`examples` 或 `notes/<name>`，空字符串表示删除该文档），配合 `remove_docs` 删除整篇。
- `add_image`：`image_source` 支持工作区相对路径、`http(s)` URL 或 `media:<sha256>`；图片会同时写入 `images/` 并导入 Media Center。
- `remove_image`：按 `image_name` 删除。

## 临时启用角色：`@char:<id>`

在消息里写 `@char:<id>`（简写 `@c:<id>`，冒号可用全角 `：`）即可把该角色的 `profile`（以及 `world`）作为当轮的角色设定注入消息，仅影响这一轮：

```text
@char:catgirl 你好呀
```

- 指令会被剥离，不会发给模型；ATRI-X 会回复"已启用角色：catgirl"。
- 作用范围严格限定为**当前一轮**：不切换 Session、不写入会话元数据，回复结束即失效，不会串到其他会话或下一轮。
- `@char` 没有跨会话 TTL 配置，因为生命期本来就是一次 turn。
- 在 chat 模式同样可用（因为只是注入文本，不依赖工具）。
- 角色不存在或无权访问时会提示"未找到或不可用的角色：xxx"。
- 一条消息可以同时写多个 `@char:<id>`；`image_generate` 会把所有已启用角色的图片预设合并到同一张图。传 `count > 1` 时也是每张图都包含全部角色，不会把角色拆到不同图片里。
- 注入内容上限约 12000 字符；超出会截断。

## 一步导入人设卡（1.1.3 修订）

用户把一整段人设贴进对话说"加入人设卡并启用"时，模型应直接调用
`character_import`，**一次调用**完成建卡，不需要翻配置文件：

```text
character_import(
  text = "<整段人设正文>",
  id = "catgirl",              # 可省略：按名称/时间自动生成
  name = "猫娘",               # 可省略：取正文第一个标题
  activate = "member"          # none（默认）/ member / soul
)
```

- `activate = "member"`：建卡后立即绑定为**当前成员**的专属人设（见下一节）。
- `activate = "soul"`：建卡后立即设为**全局系统人设**（仅超级管理员；桌面端 / 本机
  CLI 对话的用户默认就是超管）。
- 结果里会返回角色 id 与可见性；普通成员建的卡是私有（只有本人 + 超管可见可改）。
- 桌面端「人设卡」页也提供**导入文件**（ZIP / JSON / TOML / Markdown）与**粘贴导入**
  按钮：粘贴正文、可选"同时设为全局系统人设"，点一次就完成建卡；不需要手工创建目录。

### 导入 / 导出的文件格式

| 格式 | 能带什么 | 说明 |
| --- | --- | --- |
| `.zip` | `character.toml` + 全部文档 + `images/` 图片字节 | 推荐。换机器、备份都用它 |
| `.json` | 元数据 + 文档全文 + 图片元数据 | 不含图片字节（导入时会提示）；`format = "atri-x-character"` |
| `.toml` | 单个 `character.toml` | 同目录的 `*.md` / `notes/` 会一起读 |
| `.md` / `.txt` | 整段人设正文 | 作为 `profile.md` 建卡，文件名当角色名 |
| 目录 | `characters/<id>/` 结构 | 桌面端"导入文件"选目录内的 `character.toml` 即可 |

导入都走 `Store.write`，因此版本号递增、owner / visibility 规则与聊天工具完全一致；
ZIP 只解压到 `characters/.imports/<uuid>/` 临时目录（角色库扫描会跳过），并拒绝
`..` / 绝对路径 / 超大压缩包。

## 成员专属人设：按人长期生效（1.1.3 修订）

全局只有一份 `SOUL.md`。如果一群人共用同一个机器人，又想让"和某个人说话时用某张
卡"，就使用**成员专属人设**：

- 绑定键是 **平台 + 用户 id**（不含群）：同一个 QQ 换群、私聊都跟着他；只影响他和
  机器人的对话，不会改变其他成员的对话风格，也不动全局 `SOUL.md`。
- 存储位置：`<配置目录>/gui/member_personas.json`（GUI 与后台服务共用，按文件指纹
  热更新；保存后**下一轮对话生效**，不需要重启）。
- 启用时，系统提示词里的 `soul` 段会被专属人设**替换**（不是叠加），所以模型不会
  再以"我的系统人设是 XXX"为由拒绝切换。
- 关闭（`disable`）只停用绑定、保留记录，立即恢复默认 `SOUL.md` 人设；`clear` 才
  删除记录。绑定的卡被删除 / 对本人不可见时也会自动回退默认人设。

成员的开关方式（聊天里说"切换 / 启用 / 关闭我的人设"即可，模型会调用）：

```text
persona_control(operation = "status")                  # 查看当前绑定
persona_control(operation = "bind", character_id = "catgirl")
persona_control(operation = "disable")                 # 恢复默认 SOUL.md
persona_control(operation = "enable")                  # 重新启用
persona_control(operation = "clear")                   # 删除绑定
```

超级管理员可以带 `target_user_id`（可加 `target_platform`）替别人设置；普通成员只能
操作自己。桌面端「群友」页也提供"专属人设"下拉 + "启用"开关，保存后同样下一轮生效。

> 权限边界：专属人设只是角色扮演身份，不授予任何权限，也不会让成员看到别人的私有卡
> 或绕过工具风控。

## 把人设卡设为全局系统人设（SOUL.md）

`SOUL.md` 是**所有没有专属人设的用户**共用的全局身份。两种改法：

1. 桌面端「人设卡 → 系统人设 SOUL.md」直接编辑，或点「从当前人设卡载入…」把选中
   角色渲染成 SOUL.md（自动备份旧内容到 `SOUL.md.bak`，可点「恢复上次备份」回滚）。
2. 聊天里（超级管理员）：`character_activate(operation="activate", id="catgirl")`；
   回滚用 `operation="restore"`，查看状态用 `operation="status"`。

`character_activate` 写入的是角色卡渲染后的 Markdown（`profile` + `world` /
`greeting` / `examples`）；不是把 `character.toml` 原样复制过去。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/chars` | 列出当前可见角色 |
| `/chars <关键词>` | 按 id / 名称 / 别名 / tags / 简介过滤 |
| `/chars reload` | 超级管理员重建索引 |

## 版本、来源与备份清单

- 角色正文元数据写入 `character.toml` 的 `[character].version` 和 `[character].source`；每次普通更新自动递增 `version`，也可通过 `character_manage` 显式指定。
- 图片索引同样记录 `version` / `source`，默认来源是添加图片时的平台。
- `atri_x/character/manifest.py` 会为角色文档和图片生成 sha256 清单；`deploy/backup.py` 还会对 `characters/` 和 `atri-x/media/` 生成 `*.manifest`，恢复时由 `deploy/restore_verify.py` 做 `sha256sum -c` 校验。

## 图片与 Media Center

- 原图保存在 `characters/<id>/images/`，不会因为 Media Center 回收而丢失。
- 同时会导入 Media Center，记录 `media:<sha256>`，因此 `character_read(include_images=true)` 能把图片直接交给模型看。
- 单张图片上限 10 MiB。
- 媒体中心本身是内容寻址 + 引用计数；角色库的图片条目会让媒体保持有效引用。

## 配置与挂载

`app.toml`：

```toml
[character_library]
enabled = true
root = "characters"       # 相对路径以 app.toml 所在目录为基准
allow_user_create = true  # 普通成员是否允许建卡（默认允许；关闭后只有超管能新建/导入）
```

`allow_user_create = false` 只限制**新建 / 导入**：成员修改自己已有的卡不受影响；
桌面端「权限」页有同名开关，改完重启后台服务生效。

Docker 部署时不需要额外挂载：`data/` 卷已经覆盖 `data/config/atri-x/`，角色目录会持久化在：

```text
宿主机：deploy/data/config/atri-x/characters/
容器内：/data/config/atri-x/characters/
```

如果要把现有角色库直接挂进容器，也可以单独加一个绑定：

```yaml
volumes:
  - ./data:/data
  - /path/to/my-characters:/data/config/atri-x/characters
```

## 索引与刷新

- 角色索引在进程内存中维护，启动后按目录内容构建。
- 每次访问角色库时会在 3 秒冷却后重新扫描目录，手工新增 / 修改文件最多几秒后生效。
- 也可以发送 `/chars reload`（超级管理员）强制重建。
