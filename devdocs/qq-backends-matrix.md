# QQ 协议端兼容矩阵（NapCat / SnowLuma）

本页记录「ATRI-X 的 ``qqonebot`` 平台」在两套 OneBot v11 协议端上的**差异、落点与验证状态**。

- 目标：换协议端**不改平台身份**——``platform.name()`` 恒为 ``qqonebot``，权限作用域键
  （``qqonebot:<scope>``）、唤醒词、令牌与 ``ws_url`` 解析都不含后端名。
- 自动化的部分在 ``tests/test_qq_onebot_snowluma.py``、``tests/test_qq_onebot_backend.py``、
  ``tests/test_gui_proto_manager.py``；**标 ``manual`` 的必须真机**，本页就是验收记录载体。
- 真机验收时把下表头三项填上：ATRI-X 版本 / 协议端版本 / QQ 版本。

| ATRI-X | NapCat | SnowLuma | QQ |
|---|---|---|---|
| 1.1.0 | 4.18.38（内置包 `NapCat.52230.Shell`） | 2.2.0（外置接入，**不随包分发**） | 9.9.33-52230（内置包） |

图例：``automated`` = 有进程内用例；``manual`` = 需真机；``blocked`` = 受阻（写明原因）。

> 用户侧安装/接入教程（含 `ATRIX_SNOWLUMA_DIR` 探测规则、WebUI 首启、进程注入与令牌同步）见
> [`docs/snowluma.md`](../docs/snowluma.md)——本文只记录协议差异与真机验收。

> **1.1.0 发行前定稿**：默认协议端最终定为**随包内置的 NapCat**（开箱即用）；SnowLuma 改为
> **可选的备用方案**，只有用户显式选择 ``snowluma`` 才生效。判定规则改为
> "段不存在 = 新装 = NapCat、段存在但键缺失 = 老配置 = NapCat"，详见
> `CHANGELOG.md` 的「修复（本版发行前修正）」。

## 1. 差异与落点

| # | 差异点 | NapCat（默认） | SnowLuma（可选备用） | 代码落点 | 验证 |
|---|---|---|---|---|---|
| 1 | ``protocol_backend`` 取值 | ``napcat``（默认；段不存在/键缺失都解析为它） | ``snowluma``（只有显式写入才生效） | `atri_x/platform/qq_onebot/backend.py`；`gui/proto_manager.configured_backend` | automated |
| 2 | 新装默认 | 新装（`[platform.qqonebot]` 整段不存在）默认 ``napcat``（随包内置、开箱即用） | 不默认；用户显式切换后才写入 ``snowluma`` | `atri_x/gui/proto_manager.py`（`configured_backend`）；`atri_x/gui/tabs/platform.py`（`_display_backend`，与前者同一条判据） | automated |
| 3 | `get_forward_msg` 参数键 | `message_id` | 优先 `id`（res_id 字符串），`message_id` 数字也可 | `transport.get_forward_msg`（主键失败自动换键重试） | automated（参数口径）/ manual（真实转发） |
| 4 | 合并转发节点身份 | 可只给 `content` | 示例带 `user_id`/`nickname`（代码看缺失会回落到机器人 uin） | `output.py`（未改） | **manual** |
| 5 | 群文件下载 | `get_file` 可用；也有 `get_group_file_url` | `get_file` **只覆盖图片/语音**，必须 `get_group_file_url` | `attachment.resolve_chat_file` + `transport.get_chat_file_url` | automated（分派）/ manual（真实下载） |
| 6 | 私聊文件下载 | `get_file`；`get_private_file_url` 只需复合 `file_id` | 需 `get_private_file_url` + 入站 `file_hash`（裸 fileUUID） | `message.file_segment` 保留 `file_hash`；`preprocess.attach_media_scope` 补 group/user；`attachment` 透传 | automated（字段/分派）/ manual（真实下载） |
| 7 | 内联 `file` 段（出站） | 可用 | 群：有 url 自动上传；私聊：内部拆 uploadPrivate + sendC2cFile；**临时会话/合并转发内直接拒绝** | 未改（`output.py` 内联段） | **manual**：临时会话与转发内发文件要显式失败并给可读错误 |
| 8 | 文件上传动作 | `upload_group_file` / `upload_private_file` | 同（`{file_id}`，无 message_id） | `transport.upload_group_file` / `upload_private_file`（薄封装，暂无调用方） | automated（参数口径） |
| 9 | 登录状态 | `get_login_info` | `get_login_info` | `adapter.handshake`（连上后握手，失败只 WARN） → `/healthz` `platforms[].login_ok` | automated（状态机）/ manual（真实登录） |
| 10 | 后端识别 | `get_version_info.app_name` | 同（`SnowLuma`） | `adapter.handshake` + `backend.detect_backend`；界面显示"配置 vs 实际"不一致 | automated（识别）/ manual（真实 app_name） |
| 11 | 令牌错误 | 可能只是连接被拒 | **WS 升级阶段 HTTP 401** | `transport.connect` → `ForbiddenError`；`adapter.run` 固定 30s 慢重试 + 可行动告警 | automated（分类）/ manual（401 真表现） |
| 12 | 入站整数字段类型 | number | 需确认（代码假定 int64） | `types._int64`（**未改**：字符串会整条断线，先真机抓帧） | **manual** |
| 13 | 回执 `message_id = 0` | 少见 | 只上传未发送时合法（0） | `transport.send_message`（0 视为无 id） | manual（转发回执语义） |
| 14 | 管理面 | 内置 Shell + WebUI 6099（带令牌一键打开） | 自带运行时 + WebUI 5099（账号密码；首启同意 EULA + 改密） | `atri_x/gui/proto_manager.py`、`snowluma_proto.py` | automated（探测/托管）/ manual（首启闸门） |
| 15 | 进程托管 | 内置包由本程序启动 | 用户自备，**只读它的配置** | `atri_x/gui/proto_process.py`（PID 归属） | automated（状态机） |
| 16 | 许可 | 随包分发（按 NapCat 许可） | **不内置**：EULA §5.4 需书面授权 | `packaging/qqproto.py`（`backend: napcat`）、文档 | automated（打包元数据） |
| 17 | 遥测 | — | 条款声明默认开启；当前发行版**未含遥测代码** | 文档披露 | manual（升级后复核） |

## 2. 真机验收清单（9 项，来自接入分析 §5）

| # | 项 | 为什么必须真机 | 结果 |
|---|---|---|---|
| 1 | `send_group_forward_msg` 只带 `content`（无 `user_id`/`nickname`）是否被接受、转发卡片显示是否正常 | SnowLuma 在缺身份时会回落到机器人 uin，但显示效果需实测 | 待验收 |
| 2 | `base64://` 大图/大文件（MB 级）的上限与超时 | 媒体中转有 maxBytes/落盘策略，阈值需实测 | 待验收 |
| 3 | 私聊文件：只给 `file_id`（无 `file_hash`）能否下载 | SnowLuma 的 `file_id` 是裸 fileUUID，注释说依赖 `file_hash` | 待验收 |
| 4 | 引用消息 `reply` 段 id 语义、`get_msg` 的负 message_id | SnowLuma 明确"转发 id 与本地 message_id 不是同一契约" | 待验收 |
| 5 | 断线重连后的事件重放/去重（message_id 稳定性） | 去重键含 message_id | 待验收 |
| 6 | "只登录 QQ、未 Load 注入"时的行为（进程在跑、端口可能不监听） | 四维状态里的"进程/连通/登录"分界 | 待验收 |
| 7 | 配 `napcat` 实际连到 SnowLuma 时能否发现 | 依赖 `get_version_info` 真值 | 待验收 |
| 8 | 错 token 时的 401 表现与重试节奏 | 决定"连不上"还是"令牌错"的用户可读性 | 待验收 |
| 9 | 首帧 3 个 meta 帧（connect/enable/heartbeat）是否产生副作用 | 已有用例假定不会丢帧 | 待验收 |

## 3. 已确认不需要改的假设（有代码证据）

- **WebSocket 心跳**：SnowLuma 服务端 30s ping / ~90s 无活动 terminate；ATRI-X 的 WS
  客户端在 `recv()` 里自动回 PONG（`atri_x/net/websocket.py`），不会误杀。
- **meta 帧**：`adapter.read_loop` 对 `post_type == "meta_event"` 走
  `handle_platform_event`，不会被当未知事件丢弃。
- **纯文本段数组**：SnowLuma 的 `message` 既接受段数组也接受 JSON 字符串，与
  `normalize_message_with_limits` 的宽容解析一致。
