# 变更记录

本文件**只做补丁式写作**：描述从旧状态怎样变化到新状态。其它文档（`README.md`、`docs/`、`devdocs/`）只描述「现在正确的状态」，不写历史。

本仓库 `ATRI-X2-Py` 是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（v0.9.0，781 个 `.go` 文件 / 156,773 行生产代码）的 **Python 全量重写**。因此本文件的「旧状态」有两层含义：一是**跨语言**（Go → Python），二是 Python 仓库内部的后续改动。跨语言那一层集中在下面最早的一节（0.9.1），之后按正常节奏追加。

---

## [1.1.7] - 2026-10-31 — 后台服务断线自愈 + 服务退出诊断（稳定性补丁版）

> 只修稳定性：**不改**配置格式、工具 schema、会话/数据库格式；不新增配置项，1.1.6 的配置与
> 数据原样可用。发行说明（面向用户）见 [`docs/release-notes/1.1.7.md`](docs/release-notes/1.1.7.md)。

旧状态（1.1.6）：

- 桌面端只是 WS 前端，Agent 跑在独立的 `atri-x service run` 里。后台服务一旦消失（崩溃、
  被安全软件/优化工具清理、被脚本误杀），GUI 只会每 2 秒重连一次，状态栏永远停在
  「已断开（… WinError 1225）」，**不会自己把服务拉起来**；QQ 机器人与桌面端一起长期掉线，
  只能手动点「服务 → 启动/确认后台服务」。真实事故：本地跑全量单测的人为腾出
  `127.0.0.1:32171` 先杀了用户正在用的后台服务，测试跑完忘了重启，桌面端就一直「已断开」。
- 服务消失时没有任何线索：`gui/service.out.log` 里通常什么都没有，进程退出码没人记录，
  事后无法判断是崩溃、被 `taskkill` 还是被系统回收。

新状态：

- **新增 `atri_x/gui/supervisor.py`（断线自愈状态机）**：连接断开持续 8 秒后自动探测一次；
  pid 记录里的进程还活着但端口不通 → 先强制结束（挂死的服务重启才能好）→ 拉起
  `atri-x service run` → 等就绪 → GUI 自动重连；恢复期间状态栏显示「正在自动重启后台服务…」。
  失败按 15/30/60/120 秒退避、一次掉线最多 5 次，用尽后提示手动恢复。
- **安全边界**（都有回归用例）：只对 `ws://127.0.0.1` / `localhost` / `[::1]` 的本地服务启用
  （连远端服务器时绝不替对方重启进程）；`--no-service` 不启用；用户显式「停止后台服务」后
  暂停；用户点「重启后台服务」的 1–2 秒断连窗口不触发自愈（避免同时拉起两个服务）。
- **服务退出可诊断**：`atri_x/gui/service.py` 在拉起服务后挂一个看护线程，把启动/退出写进
  `gui.log`，退出记录带 pid、退出码、运行时长（`taskkill /F` 是 1、
  `STATUS_ACCESS_VIOLATION` 是 `0xC0000005`）。
- 桌面端接线：`GuiApp` 新增 `allow_auto_recover`（`--no-service` 传 False）；`_poll` 里驱动
  状态机并消费后台结果；`actions.start_backend` / `restart_backend` / `stop_backend` 与
  `quit_app(stop_service=True)` 按要求暂停/恢复（不与用户意图打架）。
- 新增 `tests/test_gui_supervisor.py`（状态机时机/退避/上限/暂停恢复/回环判定/恢复动作编排）
  与 `tests/test_gui_service.py` 的服务生命周期日志用例。

### 修订：CI 拆片 + 本地测试卫生 + ubuntu 平台断言（并入本版）

- 全量单测拆 6 片并行（`scripts/ci_shard_tests.py` + 实测权重 `scripts/ci_test_weights.json`），
  门禁与单测拆成两个 job；单进程 45 分钟以上会被托管 runner 判失联，此前 10 次 quality 运行
  0 成功。
- 部署探针/启动器用例显式指向空闲端口（`--url` / `ATRIX_HEALTH_URL`），与宿主上是否跑着真实
  后台服务无关——本地跑全量单测**不再需要先停服务**（上一轮就是这么误杀用户服务的）。
- 修掉只在 ubuntu 分片失败的 6 个平台专有断言：`file:///C:/…` 的 POSIX 前导斜杠、协议端记录
  路径用正斜杠、`.iss` 反斜杠路径按 Windows 语义解析、视觉字节预算用 PNG `tEXt` 撑体积
  （不依赖 Pillow 压缩率）、`/diagnostics` 的 `pillow_installed` 只在真装了 Pillow 时要求
  版本号。

### 发行

- 版本号 `1.1.6` → `1.1.7`：`atri_x/__init__.py`（`__version__`）/ `pyproject.toml` /
  `deploy/VERSION` / 仓库 `README.md` 同步；产物名随之变成 `ATRI-X-1.1.7-*`。
- 构建与产物（构建后填写）：尺寸 / SHA-256 / 构建事实见下方。
- 全量单测：`Ran 7,190 tests`，失败 0 / 跳过 3（后台服务保持运行；旧版探针用例要求先停服务，
  本版不需要）。12 项门禁 0 失败。
- 脱敏（构建后填写）：源码树与源码归档扫描 0 命中；便携包/安装包内 qqproto 按白名单剔除
  配置/缓存/日志（`proto-info.json` 标记 `sanitized=True`）；证据日志走脱敏脚本。

---

## [1.1.6] - 2026-10-31 — 提示词库 GUI 化 + 聊天漫画 + 头像下载修复

> 只加能力：**不改**配置格式（新增 `[comic]` 段与三个 GUI 数据文件）、不改既有工具
> schema、不改会话与数据库格式；默认行为与 1.1.5 逐字一致（内置提示词模板原样搬进
> `atri_x/promptlib/defaults.py`）。发行说明（面向用户）见
> [`docs/release-notes/1.1.6.md`](docs/release-notes/1.1.6.md)。

旧状态（1.1.5）：

- 提示词分四层：`SOUL.md` / 人设卡 / 群友档案 / 生图预设这些"用户数据"能改，但
  **模板常量写死在源码里**——生图改写的两套 system 教义、识图四套模板、群友档案与
  专属人设的注入模板、表情提示、人设卡指引，想改只能改 Python 重新打包。
- `[image_generation.profiles.*]`（`@image:<名字>` 命名生图档案）与 `tool_tags.toml`
  的 tag 提示词**没有界面**，只能手写 TOML（多行字符串尤其难写）。
- 群友 / 人设卡的「标签」只是逗号分隔的裸词：注入提示词时只有 `标签：a、b`，没有词表、
  没有说明、不能复用同一份标签。

新状态：

- **新包 `atri_x/promptlib`**：内置模板（`defaults.py`）+ 用户覆盖
  （`<配置目录>/gui/prompts.json`，只存改动）+ `(mtime_ns,size)` 指纹热加载；模板用
  `{变量}` 渲染，"整行只有一个空变量"的行自动丢弃。默认文本与 1.1.5 的常量逐字一致
  （`tests/test_promptlib.py` 断言）。
- **接入六处硬编码提示词**：生图 LLM 改写（natural / tags）、识图四套 system 模板、
  群友档案整段模板、专属人设整段模板、表情提示、人设卡指引。群友档案的自由文本仍由
  代码转义 `<` / `>`，模板不能绕过注入边界。
- **用户自建提示词**可勾选「注入系统提示词」，作为独立片段追加到每轮系统提示词
  （`SystemPromptPart("user_prompt:<id>")`），保存后下一轮生效。
- **桌面端新增第 15 个「提示词」选项卡**，四个子页：提示词模板（分类列表 + 正文编辑 +
  示例变量预览 + 恢复默认 / 导入 / 导出）、生图档案（`[image_generation.profiles.*]`
  增删改，写回 `services.toml` 且保留其它键与注释）、工具标签（`tool_tags.toml`）、
  标签库（`gui/tags.json` 的标签名 / 说明 / 别名 / 分组）。
- **标签词表**：群友档案 / 人设卡页新增「标签库…」多选按钮；群友档案注入时命中词表的
  标签渲染成 `标签（说明）`（未登记标签原样输出，兼容旧档案）。
- 生成物与门禁：`gen_index.py` 重新生成、`complexity_budget.json` 按新增模块/导入边刷新、
  新增 `tests/test_promptlib.py` 与 `tests/test_gui_promptlib.py`。

### 桌面端「下载头像」HTTP 400

旧状态（1.1.5 发行版）：群友页点「下载头像」直接报 `下载头像失败：HTTP 400`。根因是 QQ
头像接口（`q1.qlogo.cn/g?b=qq&nk=...&s=...`）只接受固定边长档位 `40` / `100` / `140` /
`640`，而界面把本地缓存边长 `96` 直接当请求参数发出去，服务端回 `HTTP 400`（响应头
`X-ErrNo: -105`）。

新状态：请求边长先**吸附到接口支持的档位**（向上取整、超过 640 取 640），本地仍按 96
解码缩放缓存；`tests/test_gui_members.py` 钉住档位映射与 401/404/超时/超大体的报错文案。

### 修订：协议端托管状态误报（2026-10-31 重发，版本号仍为 1.1.6）

旧状态（1.1.6 首发）：桌面端「在线」页把本程序刚启动的内置 NapCat 显示成
「进程：未运行或未托管（PID … 已被别的进程占用（cmd.exe））」，点「停止协议端」也会被
拒绝（只清掉状态记录）。根因是 `atri_x/gui/proto_process.py` 把 `argv[0]` 的 basename
（`cmd`）写进状态文件，而 Windows `tasklist` 报回 `cmd.exe`；字符串比较把"自己启动的
进程"误判成"PID 被别的进程复用"。

新状态：新增 `_image_name()` 归一化（去路径 / 转小写 / 去 `.exe`）后再比较，
`managed_status()` 与 `stop_managed()` 共用；旧记录（`cmd`）无需迁移即可对上，真正被
复用的 PID 仍然拒绝终止（安全边界不变）。`tests/test_gui_proto_manager.py` 新增三个
用例（`cmd` vs `cmd.exe`、路径 + 大小写、停止时同样归一化）。

### 修订：协议端端口被 Windows 保留段占用，QQ 登录后依然连不上（2026-10-31 再次重发，版本号仍为 1.1.6）

旧状态（本轮重发版）：NapCat 控制台里 QQ 已经扫码登录成功、WebUI（`6099`）也能打开，
但「在线」页一直停在「协议端未就绪 / OneBot：未连通」，后台日志只有一行
`onebot connect failed: 连接 ws://127.0.0.1:3001/ 失败：[WinError 1225] 远程计算机拒绝网络连接`。
根因是 OneBot 端口 `3001` 落进了 **Windows 的排除端口段**（本机实测
`netsh interface ipv4 show excludedportrange protocol=tcp` 给出 `2942–3041`，来自
Hyper-V / WSL / Docker 的动态端口保留），任何进程绑定该端口都会得到
`WinError 10013`，所以 NapCat 的 OneBot WS 服务端根本没能监听——它能登录 QQ，但 ATRI-X
永远连不上。而 `atri_x/gui/qqproto.py` 挑端口时只判断"端口号非空"，不验证能不能绑；
登录后 NapCat 又优先读账号那份 `onebot11_<QQ号>.json`，只改全局 `onebot11.json` 也不生效。

新状态：

- **先探测再写配置**：`atri_x/gui/proto_process.py` 新增 `port_bindable()` /
  `port_listening()` / `port_usable()` / `pick_free_port()`（顺延 200 个端口 → 高位段
  `30000/31000` → 系统临时端口）。`qqproto` 准备配置时先用它验证端口：空闲能绑才用；
  **已有进程在听也算可用**（多半是上一轮启动的协议端，换端口会把在线的机器人踢下线）。
- **不可用就顺延**：`3001` 被系统保留时自动改用可用端口，并把全局 `onebot11.json` 与
  所有已存在的账号配置 `onebot11_<QQ号>.json` 一起改（令牌同步），WebUI 端口同样校验。
- **回写 `app.toml`**：若 `[platform.qqonebot].ws_url` 是"回环地址 + 旧端口既绑不上也没人
  在听"，一并改成协议端实际端口；**非回环地址（指向别的机器 / 外部协议端）一律不动**。
- **界面说清楚**：`QqProtoSetup.port_note` 记录"端口 3001 不可用，已自动改用 30001"，
  「在线」页四维状态与「启动协议端」提示都会显示；实际端口仍以「在线」页为准。
- 文档补充：`README.md`（端口表下的自动顺延说明）、`docs/windows-app.md` §5.1（保留段
  现象 + 处理）、`docs/snowluma.md` §9.1（保留段排查命令），`packaging/qqproto.py` 生成的
  `NapCat-使用说明.txt` 也加上这条。
- 测试：`tests/test_gui_qqproto.py` 新增 7 个用例（端口优先/保留段顺延/监听中不换端口/
  账号配置同步/`ws_url` 重指/`pick_free_port` 跳出整段保留/WebUI 端口校验），并把
  「显式端口优先」等用例打桩端口探测，避免用例结果随构建机端口保留状态漂移。
- 已按同样流程**重建脱敏发行仓库 / 便携版 / 安装版**（版本号仍为 1.1.6），尺寸与哈希见
  下节「发行」。

### 修订：CI 全量单测拆 6 片并行 + 修掉依赖构建机 draw.io 前置的用例（2026-10-31 第三次刷新，版本号仍为 1.1.6）

旧状态（本轮重发版）：

- `.github/workflows/quality.yml` 只有一个 job：12 道门禁后面接
  `python -m unittest discover -s tests -t . -v`。全量 7,167 个用例在 GitHub 托管 runner 上
  要跑 **45 分钟以上**，最近几次运行都在「全量单测」这一步以
  `The hosted runner lost communication with the server` 结束——步骤永远停在
  `in_progress`、job 却已经 `completed`，门禁因此常年红。这不是用例失败，是单个长任务把
  runner 拖失联。
- 另外 `tests/test_packaging_build.py` 里「PIL 校验必须早于 PyInstaller」那条用例依赖构建机
  放没放 `packaging/vendor/drawio`（400 MB，不入库）：没放时 `build.resolve_drawio_dir`
  会先报 draw.io 缺失，用例等不到 `import PIL` 的报错而误红，CI 上必然复现。

新状态：

- **新增 `scripts/ci_shard_tests.py` + `scripts/ci_test_weights.json`**：按模块**实测耗时**
  把全量用例贪心均分成 N 片（默认 6 片；权重由 `--measure` 在本机逐模块跑一遍生成，缺失项按
  用例数估）。只按用例数分并不均衡——`test_gui_tabs` 这类要反复建 Tk 根的模块单条用例就慢一个
  量级（实测 163s/122 条）。划分是**确定性**的（只看 `tests/test_*.py` 集合、用例数与权重），
  各片自己算出同一份，不需要中心调度或中间产物；`--plan` 打印计划并自检「恰好覆盖全部测试
  模块 + 用例数守恒」，以后加/删测试模块不会悄悄漏跑。
- **`quality.yml` 拆成两个 job**：`gates`（12 道门禁 + 分片计划自检，约 40 秒）与
  `tests`（matrix 6 片，`fail-fast: false`，失败时上传本片日志 artifact）并行；runner 固定
  `ubuntu-24.04`；`concurrency` 取消同分支旧运行；超时 20 / 30 分钟。本机 6 片实测
  27–257s（未加权）/ 预计加权后 76–163s；托管 runner 上单片约 6–10 分钟，
  runner 不再被单个长任务拖失联，红了也能一眼定位到片。
- **修测试隔离**：那条 PIL 用例打桩 `build.resolve_drawio_dir`，只测「PIL 校验早于
  PyInstaller」，不再随构建机是否放渲染器漂移。
- 验证：本地 12 道门禁 0 失败；6 片合计 `Ran 7,167 tests`、失败 0 / 跳过 3（此前那 1 项
  构建机环境失败已消失），与单进程全量口径一致。
- README 数字随新增脚本刷新（`check_syntax` 1,080 个文件、受版本控制 1,459 个文件）。

### 聊天漫画（`comic_plan` / `comic_render`）

> 只加能力：**不改**既有工具 schema、会话与数据库格式；新增 `services.toml` 的
> `[comic]` 段（有默认值，缺段也能用）与 `<配置目录>/comic/` 数据目录。
> 普通生图 `image_generate` 行为不变（同一批 profile / 限流器 / 媒体中心复用）。

旧状态（1.1.5）：

- 想让模型"根据刚才聊天画漫画"只能靠 `image_generate` 加成句提示词：聊天上下文是
  压缩摘要（单条约 200 字符、总量 2400），没有消息 ID、没有结构化分镜、台词可能被
  语义改写翻译或压缩；
- 有人物参考图时只能靠 `character_id:图片名` 手工指定，没有"群友身份 → 漫画人物 →
  第几张参考图"的绑定；
- 生成失败只能整次重来，无法区分"生成失败"与"投递失败"，也无法回答"这张图用了哪些
  聊天、哪版分镜、哪个 profile"。

新状态：

- **新业务域 `atri_x/comic/`**：计划值对象与校验（`models` / `records`）、只从当前会话
  取材并保留消息 ID（`context`）、群友身份 → 角色卡/头像参考图绑定（`characters`）、
  规则规划 + 模型 JSON 结构校验（`planner`）、受保护提示词编译（`prompt`）、参考图
  读取（`references`）、文件持久化（`store`）、编排（`service` / `options`）。
- **两个新工具**：`comic_plan`（取材 + 分镜 + 预览，**不消耗生图额度**）与
  `comic_render`（按 `plan_id + revision` 生成**一张整页**漫画）。没有 `count` 参数，
  格数是计划里的 `panel_count`。
- **台词受保护**：分镜、人物—参考图映射、逐字台词都是 `PromptPriorityProtected`，
  超长时先丢风格片段、仍超长直接报错；漫画不使用普通生图的语义改写通道。
- **人物不串**：群友身份键 `member:<platform>:<sender_id>`、角色卡键
  `character:<id>`；参考图优先级 本次显式指定 > 角色卡图片 > 平台头像；
  `character_mode=avatars` 时不会被换成角色卡，缺图明确告警/报错。
- **失败恢复**：生成/入库/投递三个独立状态落库；超时等"可能已生成"记 `unknown` 且
  不自动重试；`task_id` 重发只发已有 MediaID；计划 `revision + fingerprint` 绑定，
  改台词后旧版本不能误用；重启时卡在渲染中的记录标 unknown。
- **规划模型可选**：`planner=auto` 有模型用模型、没模型用规则规划（规则规划台词逐字
  取原文，开箱可用）；规划 system 提示词在桌面端「提示词 → 漫画」可改（id `comic.plan`）。
- 生成物与门禁：`gen_index.py` 重新生成、复杂度预算按新增模块/边刷新；新增
  `tests/test_comic_*.py`（模型/取材/人物/规划/提示词/服务）与
  `tests/test_builtin_comic.py`。
- 顺带修：`tests/test_app_runtime_agent.py` 补 `tearDownModule` 清进程级旁路用量
  sink——Agent 构造会安装它，测试之间残留会让后续无关测试的旁路调用审计到假
  logger 上（全量跑出 `AttributeError: '_Logger' object has no attribute 'log'`）。

### 修订：本地测试误杀真实后台服务 + ubuntu 分片 6 个平台专有断言（2026-10-31 第四次刷新，版本号仍为 1.1.6）

旧状态（本轮）：`tests/test_deploy_backup.py` / `tests/test_deploy_watchdog.py` 的探针用例
默认访问 `127.0.0.1:32171` 并断言「连不上」。开发机上桌面端的后台服务正常监听时，这些
用例会打到**真服务**（`/healthz` 返回 401、`/live` 可达），4 个用例因此失败——于是跑全量
单测的人只能先把用户正在用的后台服务杀掉；GUI 随后一直显示「已断开（WinError 1225）」，
而桌面端**没有断线自愈**，QQ 机器人与桌面端一起长期掉线。ubuntu 分片另有 6 个只在 POSIX
上失败的断言：Windows 路径语义（`file:///C:/…` 的前导斜杠、协议端记录里的反斜杠路径、
`.iss` 里的反斜杠路径）、视觉字节预算用例依赖 Pillow 编码器的压缩率、`/diagnostics` 把
`pillow_installed` 写死为 True。

新状态：探针与启动器用例显式指向一个空闲端口（`--url` / `ATRIX_HEALTH_URL`），测试与宿主
上跑着什么无关，跑全量**不再需要**停后台服务；6 个平台断言改为与平台无关（POSIX 保留
`/C:/…` 前导斜杠、协议端记录路径用正斜杠、`.iss` 路径统一按 Windows 语义换成 `/` 再解析、
视觉预算用例用 PNG `tEXt` 文本块把体积撑到预算之上、`pillow_installed` 只在真装了 Pillow
时才要求版本号非空）。本轮只改测试，不改产品行为。

### 发行

- 版本号 `1.1.5` → `1.1.6`：`atri_x/__init__.py`（`__version__`）/ `pyproject.toml` /
  `deploy/VERSION` / 仓库 `README.md` 同步；产物名随之变成 `ATRI-X-1.1.6-*`。不新增破坏性
  配置键、不改既有工具 schema、不改会话 / 数据库格式，配置与会话数据**向后兼容**。
- 打包：`packaging/build.py --release --with-kernel --with-qqproto <NapCat Shell> --with-drawio
  <draw.io 解包目录>` 一条命令出 onedir + 便携版 zip + 安装包；产物级核验（`--version` /
  `config check` / tzdata / `-m` 转发 / 随包研究引擎 / 随包 draw.io 无头导出 / sidecar 内核 /
  GUI selftest / `service run` 起服务）全部通过；便携包解压自检与安装包静默安装/卸载自检
  全部通过。构建机 Python 3.14.7 / PyInstaller 6.22.3，含可选 PDF 依赖 reportlab 5.1.0 /
  pypdf 6.21.4（研究助理 PDF 能力不降级）。
- 发行产物（本机从工作树重建；已推送 GitHub 并作为 **GitHub Release v1.1.6**（15 个附件）
  发布——`main` 提交前进到本轮修复提交、tag `v1.1.6` 不动。下表是 **2026-10-31 第二次重发**
  的尺寸与哈希，版本号仍为 1.1.6，覆盖原资产）：
  - `ATRI-X-1.1.6-setup.exe` 592,998,164 字节（565.5 MB）—
    `5e15acec9d651c75ae894a07597a089b6e7d8dbd523976e5305152f8ecaf1888`
  - `ATRI-X-1.1.6-portable-win64.zip` 862,465,140 字节（822.5 MB）—
    `9f55c60f2a508486115ecb0458faff50913c985a0d05f503098f5d157fbd4a3a`
  - `ATRI-X-1.1.6-repo.zip` / `.tar.gz`：源码归档（1,457 条目，不含仓库根 `dist`/`build`/`runtime`
    构建产物与缓存；`atri_x/dist` / `atri_x/hook/runtime` / `atri_x/runtime` 源码包完整保留），
    尺寸与 SHA-256 见发行库 `SHA256SUMS.txt` 与 `ATRI-X-1.1.6-repo-entries.txt`。
- GitHub Release：[`v1.1.6`](https://github.com/fujiwarashigure/ATRI-X2/releases/tag/v1.1.6)，
  15 个附件（安装包 / 便携版 / 源码归档 zip + tar.gz + 条目清单 / 发行说明 / 自检报告 /
  兼容性报告 / 第三方声明 / drawio 指南 / SnowLuma 指南 / `README.md` / `LEGAL.md` / `SHA256SUMS.txt`）。
- 构建事实：onedir 714.7 MB / 24,752 文件；内置 DSH 内核 277.6 MB（384 包 / Node
  v24.19.0）；内置 draw.io 渲染器 381.9 MB / 19 文件（32.6.1）；内置协议端 2,516 文件 /
  1,202.5 MB（NapCat 4.18.38 + QQ 9.9.33-52230，脱敏 = True）；便携包 822.5 MB（压缩比
  43%，27,268 输入文件）；安装包 565.5 MB（ISCC 309s，静默安装 108.5s / 27,271 文件，
  卸载 17.6s）。构建总耗时约 11.7 分钟（onedir + 便携约 3.1 分钟，安装包编译与自检约 8.6 分钟）。
- 门禁 11 项 0 失败：`check_syntax`（1,079 文件）/ `check_imports`（`modules=826
  imported=826`）/ `check_integration`（74 项）/ `check_coverage`（72 包加权 100%）/
  `check_except` / `check_secrets --all`（0 命中）/ `gen_index --check` /
  `gen_stats --check` / `gen_drawio_pack --check` / `check_docs` / `check_complexity`。
- 全量单测（第二次重发版复跑，停掉本机 ATRI-X 后台服务后跑的干净基线）：`Ran 7,167 tests`，
  失败 1 / 跳过 3；唯一失败
  `test_packaging_build.test_build_path_fails_before_pyinstaller_when_pillow_is_missing`
  是**构建机前置条件**（源码树不放 400 MB 的 `packaging/vendor/drawio`，正式构建用
  `--with-drawio` 显式指定），不是产品缺陷。本版定向用例新增 7 个（端口可绑探测 / 保留段
  顺延 / 监听中不换端口 / 账号配置同步 / `ws_url` 重指 / `pick_free_port` 跳出整段保留 /
  WebUI 端口校验），`tests/test_gui_qqproto.py` 37 项全通过；另用**冻结产物**实测
  "app.toml 写 3001 → 自动改 30000 + 同步 `onebot11_<QQ号>.json`"。
- 脱敏：源码树 / 源码归档扫描 0 命中（个人目录、令牌、模型密钥）；便携包与安装包内的
  qqproto 由打包脚本按白名单剔除配置/缓存/日志，`proto-info.json` 标记 `sanitized=True`。

---

## [1.1.5] - 2026-10-30 — 内置 draw.io 图表能力（超管专属）

> 只加功能：**不改**配置格式、既有工具 schema（新增一个工具）、会话与数据库格式；配置与会话数据向后兼容。
> 发行说明（面向用户）见 [`docs/release-notes/1.1.5.md`](docs/release-notes/1.1.5.md)。

旧状态（1.1.4）：

- 没有任何"画图"能力。要架构图只能让模型输出 Mermaid / PlantUML 文本，或者让用户自己
  打开 draw.io 手工画；群里问"画个部署图"没有确定性交付物。
- 即使本机装了 draw.io 桌面版，也没有工具知道怎么调用它；超管想批量出图只能写脚本。
- 上游 drawio-skill（MIT，Agent Skills 格式）是纯 Python 脚本 + 参考文档 + shape 索引，
  但它假设"Agent 能直接跑 python"，而 ATRI-X 的 AgentSkill 契约是**一条命令 + stdout JSON**，
  且发行版没有系统 Python——直接塞进去跑不起来。

新状态：

- **新工具 `drawio`（`superadmin_only = true`，`risk=low`）**：一个工具 + `action` 参数覆盖
  9 个动作——`build`（代码目录 / Terraform / SQL / OpenAPI / AsyncAPI / Proto / GraphQL /
  K8s / Compose / CI 共 15 种来源）、`create`（Diagram IR / graph JSON / 手写 XML）、
  `export`（PNG / SVG / PDF）、`validate`、`inspect`、`query`、`shapes`（官方形状检索）、
  `docs`（20 篇参考文档）、`doctor`（自检）。
- **随包 drawio-skill 3.4.2**（`drawio_pack/`，约 1.2 MB）：上游 47 个纯标准库脚本、
  20 篇 references、shape 索引原样随包，**上游 247 个回归测试**作为一致性门禁
  （`tests/test_drawio_pack_upstream.py`）。只打一处补丁：`diagramctl.py` 起导入器子进程时，
  冻结形态改走 `atri-x.exe -m atri_x.diagram.runner --script <绝对路径>`。
- **内置 draw.io 32.6.1 无头渲染器**（`_internal/drawio_runtime/`，约 382 MB）：
  便携版 / 安装版**不需要用户装 draw.io**；渲染无可见窗口、约 3 秒/页，`APPDATA` /
  `LOCALAPPDATA` / `TEMP` / `TMP` 收容到技能目录私有 profile，不读写宿主用户配置。
  构建缺渲染器时**直接失败**（`--with-drawio` 指定解包目录），产物级核验会真跑一次
  无头导出 PNG（两节点图），而不是只看文件在不在。
- **媒体闭环**：渲染结果以 `out/*.png` 媒体段回到聊天（媒体中心统一入库），模型也能看到
  图做视觉自检；SVG / PDF / `.drawio` 以文件段交付。产物路径全部限定在技能目录内，
  只保留最近 60 个文件。
- **安装引导**：`atri_x/diagram/`（`pack` 定位 / `bootstrap` 安装 / `runner` 冻结入口 /
  `paths` 目录规则）；技能装进 `<配置目录>/ATRI-X/skills/agent/drawio_skill`，
  清单按运行形态生成（冻结 = `atri-x.exe -m atri_x.diagram.runner`，源码 = `python <skill>/atrix_cli.py`）。
  源码树默认不自动装，`python -m atri_x.diagram.bootstrap --source` 显式启用。
- **新增生成/门禁**：`scripts/gen_drawio_pack.py`（`pack.json` 摘要，`--check` 进 CI）、
  `tests/test_diagram_pack.py` / `tests/test_diagram_skill.py` / `tests/test_drawio_pack_upstream.py`；
  文档门禁对 `drawio_pack/`（上游技能文档，路径相对技能根）做整目录豁免。
- **体积影响**：便携 zip 增加约 181 MB、安装包增加约 133 MB（压缩后实测）；
  源码归档只增加约 1.2 MB（渲染器是构建材料，不入库）。

### 兼容性与注意

- 默认配置不变；`drawio` 工具**只对超级管理员可见**，普通成员连 schema 都拿不到。
- 安装版 / 便携版首次启动自动装技能（失败只记 WARN，不影响机器人启动）；源码树需显式
  `--source` 安装，且渲染需要 `packaging/vendor/`（子目录 drawio）或 `ATRIX_DRAWIO`。
- 无窗口渲染依赖交互式 Windows 会话：Session 0 / 无桌面的服务环境里导出会失败并**降级**
  为交付 `.drawio`，其余动作（生成/校验/检索/文档）不受影响。
- 一期未注册 `sync` / `reconcile` / `views` / `test` / `review` / `whatif` / `story` /
  `transform` 等上游动作；脚本随包，可由超管在服务器上直接运行。

### 1.1.5 修订（同日，版本号不变）

旧状态（1.1.5 首次构建）：

- **生图「获取可用模型」返回 HTTP 401**：该按钮只从 `.env` / 进程环境取密钥，界面「密钥值」
  里刚填、**还没保存**的密钥不参与请求；匿名请求被中转站拒绝后，报错文案又写成"检查密钥
  环境变量与 .env"，让人以为是密钥填错。
- **`/status` 的 token 行「命中」恒为 0**：`Usage.cache_hit_tokens` 的 0 既可能是上游明确
  上报 0，也可能是上游响应里根本没有缓存字段，展示层无法区分；同时 `llm_usage` 审计事件
  在**生产装配**里被静默丢弃（`Agent` 只拿到 `Options.logs`，从未调用 `set_log_manager`，
  `agent.logger` / `agent.audit_logger` 全程为 `None`），`/usage` 因此也没有数据可查。

新状态：

- **取密钥优先用界面输入框**：新增 `atri_x.gui.configmodel.resolve_fetch_key()`，生图 / LLM
  Provider / 本地生图服务探测三条「获取可用模型」路径都先取界面里刚填的密钥（标注来源
  `输入框（尚未保存）`），其次才回落 `.env` / 环境变量。
- **401 文案区分"没带密钥"与"密钥不对"**：`modelcatalog` 在 401/403 且本次请求没带密钥时
  追加 `NO_KEY_HINT`（提示"先保存进阶配置再获取"）；界面操作日志同时打印本次用的密钥来源。
- **缓存命中三态**：`Usage` 新增 `cache_hit_reported`（默认 `True`）；HTTP 适配器解析响应时
  只要发现缓存字段（值可以为 0）就标记为已上报，否则标为未上报并随会话 metadata 一起持久化
  （键 `cache_hit_reported`，旧记录按已上报兼容）。`/status` 因此显示 `命中：0`（上游明确
  上报 0）或 `命中：未上报`（上游没给），不再把"未知"说成 0。
- **审计通道接线**：`Agent.__init__` 在给了 `Options.logs` 时自动 `set_log_manager()`，
  `agent.logger` / `agent.audit_logger` / `log_reader` 真正挂上；`/usage`、`/log`、安全审计、
  `llm_usage` 事件从此有数据（`data/logs/audit-*.log` 不再为空）。
- **现场打点**：每轮记一条 `usage recorded`（info，含 prompt/completion/total/cache 与
  `cache_hit_reported`），每次模型调用另有 debug 级 `llm usage`；`llm_usage` 审计事件增加
  `cache_hit_reported` 字段。
- **日志格式 bug**：`RequestLog` 原来把 `%s` 占位符和参数一起当 `key=value` 传给日志器，
  实际输出成 `... %s %s =!BADKEY`；改为标准 `key=value` 后请求摘要与首个 system prompt
  都能正常检索。

## [1.1.4] - 2026-10-30 — 超管资源占用工具 + 可维护性/导航体系

> 只加功能与工程设施，**不改**配置格式、工具 schema、会话与数据库格式；配置与会话数据向后兼容。
> 发行说明（面向用户）见 [`docs/release-notes/1.1.4.md`](docs/release-notes/1.1.4.md)。

旧状态（1.1.3）：

- 超管想知道"现在占多少内存 / CPU / 磁盘还剩多少"没有工具可用：只能让模型用 `shell` 跑
  `Get-Process` / `wmic`——Windows 上 shell 恒判高风险、每次要确认、群里确认还看不到命令，
  且要解析文本。群里成员也无法自证"是不是机器卡"。
- `atri_x/sysinfo.py` 的 `process_rss()` 只实现了 Linux 的 `/proc` 分支，**Windows 上恒返回 0**，
  于是 `/metrics` 的 `resources` 段与定时日报在 Windows 上一直显示进程内存 0；也没有系统级内存 /
  CPU 采样。
- 文档面只有 `gen_stats.py --check` 一道门禁（只验 README 数字）。`devdocs/code-map.md` 635 行、
  257 处手写路径引用，`AGENTS.md` 10.7 KB 的 locator 任务表全靠人维护；`code-map` 覆盖 598/777
  模块（77%），测试只直接 import 了 388/777 模块（49.9%）。**文档是软副本，漂移只能靠人记得改。**
- `AGENTS.md` 写着"单文件 ≤600 行"，但实测 78 个文件超线（16 个 ≥800，最长 1225 行）——**规则没有牙**。
- CI 只有 `release-image.yml`（发镜像），7 道质量门禁**从未在 CI 跑过**。

新状态：

- **新增超管专属只读工具 `runtime_resources`**（`superadmin_only` + `risk=safe`）：进程内存
  （RSS / 峰值 / 私有提交）、进程 CPU（单核基准）、线程 / 句柄 / 读写字节、系统内存与整机 CPU、
  逻辑核数、运行时长、指定卷磁盘剩余；采样窗口默认 500ms（100–2000ms 可调），目录大小递归统计默认关。
  `call` 内走 `asyncio.to_thread`，500ms 采样不会阻塞事件循环。
- **新增 `/resources` 斜杠命令**（仅超管，`Info.min_role` 留空）：与工具共用
  `atri_x/sysinfo.py:format_resources` 的同一份渲染，单一出处。
- **`atri_x/sysinfo.py` 补齐 Windows 采样**（纯标准库 `ctypes`）：`K32GetProcessMemoryInfo` /
  `GetProcessTimes` / `GetProcessHandleCount` / `GetProcessIoCounters` / `GlobalMemoryStatusEx` /
  `GetSystemTimes`；`process_rss()` 在 Windows 不再恒为 0，`/metrics` 与日报的进程内存随之正确。
  新增可注入的 `PlatformCollector` 与 `collect_resources()`，`collect()` / `Snapshot` 语义不变。
- **新增生成式索引体系**：`atri_x/devtools/`（`astindex` / `render` / `entrypoints` / `domaindocs` /
  `boundaries` / `markdown`，零第三方依赖、不 import `atri_x`）+ `scripts/gen_index.py`，产出 16 个
  生成物：`devdocs/generated/{module-map.md,deps.mmd,entrypoints.md,test-map.md,go-map.md}`、
  10 个 `atri_x/<域>/README.md`、`docs/dev/glossary.md`。`--check` 在 CI 判漂移。
- **新增文档门禁 `scripts/check_docs.py`**：相对链接、内联路径、索引覆盖、locator 锚点、行数棘轮、
  数字一致性共 6 条断言，配 `scripts/line_budget_baseline.json` 与 `scripts/doc_link_baseline.json`
  两个棘轮基线；支持 `--list` / `--update`。
- **新增结构预算 `scripts/check_complexity.py` + `scripts/complexity_budget.json`**：模块数、
  超 600 行文件数、最长文件行数、跨一级域 import 边、最大 fan-in、无测试触达模块数、
  跨域绕开枢纽 facade 次数，全部**只降不升**；`atri_x/devtools/boundaries.py` 声明 7 个枢纽的公开面，
  现状 26 处违规（全部在 `atri_x.config` 子模块）冻结成基线。
- **新增 CI `.github/workflows/quality.yml`**：7 道代码门禁 + `gen_index --check` +
  `gen_stats --check` + `check_docs` + `check_complexity` + 全量测试；另加
  `.github/pull_request_template.md` 三条勾选。
- **文档分层收缩**：`README.md` 489 → ~300 行（发行说明历史迁到 `docs/release-notes/`）；
  `AGENTS.md` 10.7 KB → ~6 KB（只留操作规则，locator 任务表被生成物替代）；
  `devdocs/code-map.md` 635 → ~140 行（"读的顺序 + 为什么这样分"，逐文件职责交给 `module-map.md`）；
  新增 `docs/dev/START-HERE.md`（30 分钟上手 + 3 个练习）、`docs/dev/find-it.md`（10 类任务入口）、
  `devdocs/adr/0001-flat-layout.md`、`0002-six-hundred-lines.md`、`0003-zero-runtime-deps.md`、
  `0004-structural-convergence.md`。
- **测试**：新增 `tests/test_builtin_runtime_resources.py`（26 例：声明 / 参数解析 / 换算 / 降级 /
  工具 call / 命令 handler）；注册表与命令计数断言同步（工具 54 → 55、命令 49 → 50、最小装配 27 → 28）。
- **修掉一个存储层真 bug（稳定性测试发现）**：`format_time` 按 Go `RFC3339Nano` 去掉小数秒
  末尾的 0，同一秒内可能同时出现 `…:45.7835Z` 与 `…:45.783545Z`；直接按字符串比较时前者的
  `Z` 大于后者的数字，导致**较早的消息排到后面**（实测：同一会话 user/assistant 次序偶发颠倒，
  约 0.5%）。修复不动存储格式，只在查询侧用 `rtrim(created_at, 'Z')` 归一化比较
  （`atri_x/storage/times.py:order_time_expr`，应用到消息 / 会话预览 / 上下文摘要三处查询），
  并加了一条确定性回归用例。
- **修正口径**：早期文档里的"3,476 条 import 边 / 80% 跨域 / `errors` fan-in 645"是错误算法
  （包前缀当模块名匹配，重复计数）。1.1.4 起以 `atri_x/devtools` 口径为准：3,611 条边、
  跨一级域 40.6%、最大 fan-in 250。

### 1.1.4 修订（同日）— `/resources` 隐私加固 + 硬件/显卡段

旧状态：`/resources`（与 `runtime_resources`）为了报"磁盘剩余"，把**配置目录的真实路径**原样
渲染出来（例如 `C:\Users\<用户名>\AppData\Local\...\ATRI-X`），等于把用户名、安装位置与目录结构
一起发给群聊；输出里也没有显卡占用与硬件型号。

新状态：

- **隐私边界落在渲染层**：磁盘只报**卷标签**（`系统盘` / `数据盘 N` / `指定卷 N`）与容量，
  CPU / GPU 只报型号与占用，**不出现任何本机路径、用户名、主机名**；采集失败只保留异常类型名
  （异常原文可能带路径）。命令与工具共用同一份渲染（`atri_x/sysinfo_format.py`），单一出处。
- **新增硬件 / 显卡 / 软件段**：CPU 型号与线程数、系统版本与架构、GPU 型号与显存；
  显卡占用按 `nvidia-smi`（NVIDIA：利用率 / 显存 / 温度 / 功耗）→ PDH 性能计数器
  （AMD / Intel / 无 nvidia-smi：整机 3D 引擎占用）→ "占用不可得"三级降级；进程段补
  "已运行多久"（`GetProcessTimes` / `/proc/<pid>/stat`），即软件进程状态。
- **结构调整（行数棘轮）**：`atri_x/sysinfo.py` 919 行已到预算上限，因此拆出
  `atri_x/sysinfo_platform.py`（平台采集器）、`atri_x/sysinfo_format.py`（渲染 / JSON）、
  `atri_x/hardware.py`（硬件身份）、`atri_x/gpu_usage.py`（显卡占用）；`sysinfo.py` 降到 667 行。
  复杂度预算显式放宽：模块 787 → 791、跨一级域 1468 → 1473（review 可见），
  无测试触达模块与跨域违规数不变或下降。
- **测试**：`tests/test_builtin_runtime_resources.py` 26 → 52 例（新增隐私断言、卷标签、
  nvidia-smi CSV / PDH 计数器解析、硬件降级、进程运行时长）。
- **兼容**：`runtime_resources` 的入参（`sample_ms` / `paths` / `include_dir_size`）、工具名、
  `/resources` 的权限与 `Deps.resources` 注入点都不变；`paths` 只影响"统计哪个卷"，不再回显路径。
  磁盘快照类型从 `(path, total, free)` 改为 `DiskUsage(label, total_bytes, free_bytes)`。

## [1.1.3] - 2026-10-29（修订）— 人设卡「一步导入 / 启用」与成员专属人设

> 版本号仍为 1.1.3（不发新版）。只加功能与配置键，**不迁移**现有数据：
> `gui/members.json` 不变；新增可选文件 `gui/member_personas.json` 与
> `SOUL.md.bak` 备份；`app.toml` 新增 `[character_library].allow_user_create`
> （缺省 `true`，旧配置不用改）与 `[connect_notice]`（缺省私聊开、群聊开但群列表
> 为空，旧配置不用改——要群提醒需自己填 `groups`）。

旧状态（1.1.3 首次发布）：

- 聊天里说"把人设加入人设卡并启用"没有对应工具路径：模型会去翻 `app.toml` /
  `SOUL.md`，连翻 10 轮撞上 `max_rounds_per_turn` 后整轮无结果；`character_manage`
  只能逐字段建卡，"导入"还得用户自己拆字段。
- 群里能建卡（普通成员强制私有），但"切换 / 启用某个人设"没有机制：`SOUL.md`
  是全局唯一身份，模型会以"没有切换功能"为由拒绝；`@char:<id>` 只对单轮生效。
- 普通成员没有自助开关专属人设的入口；`[security.superadmins]` 只能在「平台」页
  底部一小块里手填，QQ 平台没配时启动日志只有一条 WARN，界面上看不到。
- 上线通知只有 `plugins/hooks.toml` 里的一条私聊规则（`target.superadmins = true`）：
  发不到群里；规则的 `enabled` 只能整体开关，"只关私聊、保留群聊"做不到。

新状态：

- **新增工具 `character_import`**：把一段粘贴的人设文本一步建卡（写 `profile.md`，
  自动生成 id / 名称），可顺带 `activate="member"`（绑为当前成员专属人设）或
  `activate="soul"`（设为全局系统人设，需超管）。系统提示词新增「人设卡操作指引」，
  明确这类请求直接走工具、不要翻文件。
- **新增工具 `persona_control`（OwnerScoped）**：成员自助 `status` / `bind` /
  `enable` / `disable` / `clear` 专属人设；超管可带 `target_user_id` 替别人设置。
- **新增「成员专属人设」**：按「平台 + 用户 id」持久化在
  `<配置目录>/gui/member_personas.json`（文件指纹热更新，下一轮生效、不重启）。
  启用时在系统提示词拼接阶段**替换** `soul` 段（`SystemPromptPart.replaces`），
  因此不再出现"全局 SOUL 与专属人设同时存在、模型拒绝切换"；关闭只停用绑定，
  立即恢复默认 `SOUL.md`，且只影响该成员、不干扰其他人。
- **新增工具 `character_activate`（仅超管）**：`activate` / `restore` / `status`，
  把角色卡渲染成 `SOUL.md`（写前备份 `SOUL.md.bak`，可回滚）；返回的 id 与路径
  可直接展示给用户。
- **新增角色卡导入 / 导出**（`atri_x/character/archive.py`）：ZIP（含文档与图片字节）、
  JSON（文档全文 + 图片元数据）、单个 `character.toml`、Markdown / 纯文本、整个角色
  目录；桌面端「人设卡」页新增 导入文件 / 粘贴导入 / 导出 三个按钮。ZIP 解压限制
  在 `characters/.imports/<uuid>/` 并拒绝路径穿越与超大压缩包。
- **新增「权限」选项卡**（第 14 页，`gui/tabs/permission.py`）：超级管理员名单（每平台
  一行，含 QQ 号示例与"群主/管理员不会自动成为超管"说明）+ 普通成员最高工具风险 +
  超管确认阈值 + **单轮工具轮次**（`[tools].max_rounds_per_turn`）+ **允许普通成员
  建卡**（`[character_library].allow_user_create`）开关；保存后重启后台服务生效。
- **`[character_library].allow_user_create`**（默认 `true`）：关闭后 `character_manage`
  的 `create` 与 `character_import` 都会拒绝普通成员新建/导入（改自己的已有卡不受
  影响），错误文案说明原因与联系对象。
- 桌面端「群友」页新增专属人设下拉 + 启用开关 + 提示词预览（预览会显示专属人设块与
  群友档案两段）；表格新增「专属人设」列。
- 桌面端「人设卡 → 系统人设 SOUL.md」新增「从当前人设卡载入…」与「恢复上次备份」。
- 工具目录（`gui/toolcatalog.py`）补齐 3 个新工具的中文名与说明；`REGISTERED_TOOLS`
  从 51 变 54。文档：`docs/character-library.md`（导入/导出、成员专属人设、SOUL 激活）、
  `docs/configuration.md`（新键与超管名单）、`docs/windows-app.md`（第 14 页与两页改动）、
  `devdocs/code-map.md`。
- **私聊回声保护（1.1.3 修订，无新增配置键，始终开启）**：私聊默认每条都唤醒，
  但两类"非真人输入"不再叫醒模型——①客户端自动应答（消息以 `[自动回复]` /
  `[自動回覆]` / `[Auto-Reply]` 等固定前缀开头）；②同一私聊对象连续发送完全
  相同的文本 3 次（120 秒窗口内）。只静默唤醒，消息照常入库；对方发来不同内容
  立即恢复；CLI / 本地内核不启用复读保护。修复真机事故：上线通知发到超管私聊 →
  对方 QQ「离开自动回复」逐条回应 → 机器人对每条自动回复各回一条 → 4 分钟互相
  刷了 20 轮（每次调用都消耗 token）。
- **上线通知改为 `[connect_notice]`，私聊与群聊各自独立开关（1.1.3 修订）**：新增内置
  Hook `builtin.connect_notice`；`private` 控制给 `[security.superadmins].qqonebot`
  的私聊提醒，`group` + `groups`（QQ 群号列表）控制群提醒，两个开关互不影响。
  旧版 `plugins/hooks.toml` 的 `notify_qqonebot_connected` 规则从默认模板移除并注释
  （保留会重复发送）。文案可用 `text` 覆盖（留空用内置「ATRI-X 已连接 QQ OneBot。」）；
  未配超管时私聊分支自动跳过，不连累群通知；群开关打开但群列表为空时不猜群、不群发。
  桌面端「在线」页 QQ 平台卡片新增「上线通知」一行：私聊 / 群聊两个复选框 + 群号
  输入框 +「保存」（写 `app.toml [connect_notice]`，重启后台服务生效）。同批输出
  若落在不同目标（超管私聊 + 群），发送侧按 target 拆批后再发——一批输出只允许
  一个显式 target，整批直接发会被整批拒绝（这正是两个落点一条都发不出的原因）。
- 验证：`check_syntax`（1012 文件，0 错）/ `check_imports`（`modules=777 imported=777`）/
  `check_integration`（74 项，0 失败）/ `check_except`（与基线一致）/ `gen_stats --check` /
  `check_secrets`（0 命中）全过。全量 `unittest`：`Ran 6888 tests in 1292.8s`，
  `FAILED (failures=2, skipped=4)`——仅 `tests.test_gui_qqproto` 2 项因**本机正在运行
  便携实例**、NapCat 占着 `3001` 使探测拿到 `3002` 而失败（`3002 != 3001`）；此前那批里
  `32171` 一组的 4 项是后台服务在跑导致，本轮开测前已停掉 `atri-x` / `atri-x-gui`，
  故不再出现。这两项在停掉 NapCat 后重跑即可通过（此前已验证），属环境冲突，与本改动无关。针对性 641 项
  （delivery / app_platforms / gui_link / config / hook / safety / agent_core）全过，
  含本次新增的上线通知、target 拆批、GUI 读写用例。冻结产物冒烟：`atri-x.exe --version`、
  `config check`（warnings: none）、`atri-x-gui.exe --selftest` 全部 0 退出。便携版真机
  验证（4 次重启）：连上 OneBot 后**超管私聊与群 123456789 各收到一条上线通知**；
  `private=false` 重启后群仍收到、私聊不再收到；`group=false` 重启后私聊收到、群不再
  收到；恢复两个开关后两者都收到——两个开关确实互不影响。私聊通知之后对方 QQ 的
  「离开自动回复」只入库，**没有任何机器人回复**（旧行为会各回一条并循环 20 轮）。

---

## [1.1.3] - 2026-10-29 — 科研研究助理：随包硬工具引擎、`/research` 命令与「研究」页

> 功能版（1.1.2 → 1.1.3）。**不改现有配置格式、不改内置工具 schema、不改会话/数据库格式**；
> 新增的 `research_pack/` 与 `atri_x/research/` 只在首次启动时往用户配置目录补文件
> （`research/` 与 `skills/agent/research_*`），已有配置不受影响，升级不需要迁移。

旧状态（1.1.2）：ATRI-X 没有任何学术研究能力——没有文献检索/筛选/核验/写作/评审的
结构化流程，模型只能靠通用工具临时拼；也没有一个"进程内不随模型漂移"的研究引擎。

新状态：

- **新增随包研究引擎 `research_pack/`**（仓库内是数据目录，冻结产物在 `_internal/research_pack/`）：
  `engine/` 是 38 个模块、约 8.7k 行的纯标准库实现（多源检索、去重、PRISMA 筛选、DOI 反查
  Crossref / DataCite、APA / GB-T 7714 引用、大纲与正文结构、QA、评审加权、六维评分、报告装配、
  md/html/docx/pdf 导出），`skills/` 是 15 个原创 AgentSkill（12 个硬工具 + 3 个协议技能），
  `pack.json` 记录引擎版本与内容摘要，`settings.json` 是引擎默认设置。
- **新增 `atri_x/research/` 装配层**（`pack.py` / `bootstrap.py` / `runner.py` / `paths.py`）：
  首次启动把引擎与技能装进 `<配置目录>/ATRI-X/research` 与 `skills/agent/research_*`，
  幂等、按 `pack.json` 摘要自动升级、尊重 `atri-research/install.py` 的 venv 版外部安装、
  失败只记 WARN 不阻断主程序。技能的 `command` 按运行形态生成：冻结产物是
  `atri-x.exe -m atri_x.research.runner --home <研究目录> <tool>`（**不需要系统 Python**），
  源码树是当前解释器跑 `<研究目录>/engine/run.py`。
- **新增 `/research` 命令**（`atri_x/agent/commands/research.py`，`min_role=user`，只读）：
  `status`（全部运行）/ `<运行id>`（摘要与产物清单）/ `env`（引擎、venv、依赖、随包引擎状态体检）/
  `open`（仅超管）。命令层刻意不 import 引擎，引擎没装好也能回答。
- **新增桌面端「研究」选项卡**（`atri_x/gui/tabs/research.py`，第 13 页，只读）：
  运行列表（阶段/题目/更新时间）+ 运行摘要 + 产物树（`run.json` / `library.json` / `draft.md` /
  `report.md` / `exports/` 等），双击运行打开目录，可打开报告与导出目录；不依赖引擎包。
- **12 个硬工具全部 `risk="low"`、`superadmin_only=false`**（普通群成员可用，低于默认
  `user_max_tool_risk="low"`）：`research_plan` / `search` / `screen` / `verify` / `cite` /
  `write` / `panel` / `export` / `doctor` / `flow` / `compose` / `audit`。参数 JSON Schema 与
  `[args]` 映射来自随包 `manifest.json`，**技能文档的「工具契约」附录由引擎生成并校验同源**
  （`make_skills.py --check` / `run.py skills --check`），文档与实现不会漂移。
- **确定性优先**：检索去重、PRISMA 计数、DOI 核验、引用格式化、正文结构校验、六维评分全部由
  引擎代码完成，模型只负责"判断与表达"；因此换 LLM 时流程顺序、格式与判定标准不变。
  `research_flow`（顺序状态机）/ `research_compose`（成稿契约 + 自动质检）/ `research_audit`
  （固定公式评分）把最容易随模型漂移的三处也收进代码。
- **依赖与降级**：引擎零第三方代码依赖；PDF 导出用可选 `reportlab`、PDF 全文抽取用可选
  `pypdf`，冻结产物按构建机是否安装把它们收进 PYZ（发行版两个都带），源码树缺库时导出
  降级为 md/html/docx 并在信封 `degraded` 里显式声明，绝不假装成功。
- **原创性**：引擎与技能是本仓库的原创实现，**不包含**上游 `academic-research-skills`
  （CC BY-NC 4.0）的任何文本或代码；相似度核查方法与结果随发行物留档（0 个逐字节重复文件、
  CJK 8-gram 0、正文最长公共片段 9 词、上游内部标识符 0），见 `LEGAL.md` 与发行说明。
- **接线**：`app/runtime_agent.build_agent`（服务进程装配 Agent 前安装引擎，保证 AgentSkill
  扫盘时技能已就位）与 `gui/service.prepare_settings`（GUI 启动时提前安装，保证「研究」页有内容）；
  `/research` 命令注册进 `agent/commands/deps.py` 与桌面端选项卡集合。

### 发行

- 版本号 `1.1.2` → `1.1.3`：`atri_x/__init__.py`（`__version__`）/ `pyproject.toml` /
  `deploy/VERSION` / `deploy/pack/deploy_lighthouse.py` / 仓库 `README.md` 同步；产物名随之
  变成 `ATRI-X-1.1.3-*`。不新增配置键、不改工具 schema、不改会话 / 数据库格式，配置与会话数据
  **向后兼容**。
- 打包：`packaging/atri-x.spec` 把 `research_pack/` 收进 `_internal`（缺失时**构建直接失败**）；
  `packaging/build.py` 产物核验新增「随包引擎安装 + 15 技能 / 12 硬工具 + 引擎可执行」三步，
  便携版核验新增 `research` / `research run` 两步；安装包与便携版都**开箱即用**（无需 Python）。
- 文档：新增 `docs/research.md`（完整使用与排障）、`research_pack/README.md`（随包资源与
  升级流程），`docs/commands.md` / `docs/windows-app.md`（13 个选项卡，§4.9 研究页）/
  `docs/concepts.md` / `devdocs/code-map.md` / `README.md` 同步。
- 发行产物（本机从工作树重建；按发布要求**未提交 git、未推送 GitHub**）：
  - `ATRI-X-1.1.3-setup.exe` 477,667,625 字节（455.5 MB）—
    `2d5ff2193358c6ba0872a2afc13e1574749ea748497c923c8606470c0439fd14`
  - `ATRI-X-1.1.3-portable-win64.zip` 704,208,729 字节（671.6 MB）—
    `ff518c5b9f0d86be1ae867fa931535c094861b8d1021ce65d3ec71a5d6257c94`
  - `ATRI-X-1.1.3-repo.zip` / `ATRI-X-1.1.3-repo.tar.gz`：源码归档（工作树文件清单 1229 条目，
    不含 `runtime`/`build`/`dist`/缓存）。归档自身尺寸与 SHA-256 见发行库
    `SHA256SUMS-1.1.3.txt` 与 `ATRI-X-1.1.3-repo-entries.txt`（归档内文件不写自身哈希，避免自引用）。
- 门禁 7 项 0 失败：`check_syntax`（1012 文件）/ `check_imports`（`modules=777 imported=777`）/
  `check_integration`（74 项）/ `check_coverage`（`packages=72 covered=72 weighted=100.0%`）/
  `check_except`（119 基线）/ `gen_stats --check`（README 数字一致）/ `check_secrets`（0 命中）。
- 全量单测：`Ran 6,888 tests in 1292.8s`，`FAILED (failures=2, skipped=4)`（仅
  `tests.test_gui_qqproto` 2 项因本机 NapCat 占用 `3001` 端口而失败，见上节「验证」）；
  本版定向回归（随包引擎首启安装的
  幂等 / 升级 / 尊重 venv 外部安装、技能清单与实现同源校验、子进程入口分发、`/research` 命令、
  桌面端「研究」页、`build_agent()` 与 GUI 启动期接线）全部 OK；研究引擎另做过真实网络的
  端到端冒烟（多源检索 → PRISMA 筛选 → DOI 核验 → 成稿 → 六维评分 → 导出），留档于发行库
  `证据/1.1.3/research-smoke/`。
- 产物级自检：onedir 24,686 文件 / 364.9 MB，加内置 DSH 内核（`0.2.0-rc.2` / Node v24.19.0，
  277.6 MB）与脱敏后的内置 NapCat（2,516 文件 / 1202.7 MB）后构成便携输入 27,202 文件 /
  1,567.6 MB → 便携包 671.6 MB / 27,207 条目解压自检全过（16.1 s）→ 安装包 455.5 MB
  （ISCC 275.5 s、源文件 27,202；静默安装 65.1 s / 27,205 文件、卸载 8.9 s）；构建总耗时 579.4 s。
- 脱敏：源码树 / 源码归档 / 内置内核 / onedir / 安装包（ASCII + UTF-16LE）/ 便携包文本条目 /
  发行库文本各层扫描 **0 命中**；便携包全量扫描（含二进制）命中的只是 8 个上游二进制
  （`cryptography` Rust 扩展与内置 QQ / NapCat）里的固有构建路径，经 SHA-256 与上游原样文件
  逐字节核对一致，不含本机用户名与令牌。详见发行库 `自检报告.md` §三。
- 原创性与独立复验：与上游 `academic-research-skills` 3.24.0 的相似度核查 **PASS**（逐字节重复
  文件 0、CJK 8-gram 0、散文最长公共片段 9 词、代码最长公共片段 18 词、上游内部标识符 0、
  版权声明头 0、技能名交集 0）；发行库独立复验 54 项检查 0 失败（`independent-verify-1.1.3.json`）。

---

## [1.1.2] - 2026-10-28 — 群友人设真正接入对话（桌面端「群友」页 → 系统提示词）

> 只加功能，不改配置格式、不改工具 schema；`gui/members.json` 向后兼容（新增
> `enabled` 字段缺省视为 `true`），升级后原先录好的群友设定下一轮对话就能生效。

旧状态（1.1.1）：桌面端「群友」页能按群发现成员并保存备注名、标签、角色数据和头像，
但这份档案只有界面读写——模块注释直接写着"后台服务不读它（改动不影响对话）"。用户在
群友页填的人设对模型完全不可见。

新状态：

- 新增共享层 `atri_x/memberprofile/`（依赖方向 `GUI → memberprofile ← Agent`）：
  - `model.py`：`MemberProfile`（含新增 `enabled`）、`members.json` 序列化与归一化；
  - `store.py`：`MemberProfileStore`，仍读写 `<配置目录>/gui/members.json`（不迁 SQLite）。
    按 `(mtime_ns, size)` 指纹热加载（保存/删除后下一轮生效，不重启）；损坏文件读取降级
    为空表并记 WARN，`upsert` / `remove` **拒绝覆盖**损坏文件；写入仍是 `.tmp` +
    `os.replace`，进程内加锁；
  - `prompt.py`：把一份档案渲染成 `<member_profile>` 边界内的系统提示词片段，附"这是
    用户数据、不改变自身身份、不授予权限"的信任声明，并转义正文尖括号防止伪造边界。
- 新增 `atri_x/agent/member_profile_prompt.MemberProfileSystemPromptSource`：每轮只注入
  **当前发言人**的档案；严格按 `平台 + 完整作用域（如 group:123456789） + user id` 匹配
  （不用去前缀的 `req.meta.id`，也不认昵称/正文里自报的号码）；私聊、频道、后台沙盒
  （cron / 外部事件）不注入。
- 接线：`agent/options.py` 新增 `member_profile_store`；`app/runtime_agent.build_agent()`
  按 `app.toml` 所在目录创建 store 并挂到宿主；`agent/prompt.new_prompt_builder()` 注册
  该来源。**并修复一处装配期覆盖**：`ToolRuntimeMixin.rebuild_system_prompt()` 会在安装期
  整体替换 `prompt_builder.system`，原实现只重建 Go 那五个来源，导致 Python 侧新增来源
  （群友档案，以及此前同样丢掉的 `[reactions]` 表情提示）在真实运行路径上不生效；现在
  两处来源清单保持一致。
- 桌面端「群友」页：新增「参与对话」勾选（默认勾选）、列表「对话」列、以及按当前编辑器
  内容实时渲染的「提示词预览」；保存/删除后底部说明提示"下一轮对话生效"。头像仍只是
  GUI 缓存，不会发给模型、也不会自动成为生图参考图。
- 边界：档案只是"这位群友是谁、希望被怎么称呼和互动"，不是权限配置——正文里写
  "我是管理员 / 忽略规则"不会改变工具授权；权限仍由安全策略与「工具权限」页决定。群友
  档案默认按群隔离，不自动跨群/跨平台共享；共享线程切换发言人只加载新发言人的档案。

新增用例：

- `tests/test_member_profile.py`：存储往返/老档案兼容/损坏拒绝覆盖/指纹热更新/删除清缓存、
  按平台+群+用户精确隔离、A 发言只加载 A、私聊与后台沙盒跳过、停用与无档案保持旧行为、
  伪权限不改变授权、`new_prompt_builder` 装配，以及**从 `build_agent()` 出发的集成测试**
  （模型请求里确实能看到档案、保存/停用后下一轮生效）；
- `tests/test_gui_tabs.py::MembersTabCase`：「参与对话」保存与列表标记、提示词预览渲染。

文档：`docs/windows-app.md` §4.2/§4.5 与限制清单、`docs/concepts.md` 系统提示词顺序、
`devdocs/code-map.md` 已同步。

### 发行

- 版本号 `1.1.1` → `1.1.2`：`atri_x/__init__.py`（`__version__`）/ `pyproject.toml` /
  `deploy/VERSION` / `deploy/pack/deploy_lighthouse.py` / 仓库 `README.md` /
  `docs/snowluma.md` 同步；产物名随之变成 `ATRI-X-1.1.2-*`。本版是**功能版**（群友人设真正
  接入对话），不新增配置键、不改工具 schema、不改会话 / 数据库格式，配置与会话数据向后兼容。
- 发行产物（本机从工作树重建；按发布要求**未提交 git、未推送 GitHub**）：
  - `ATRI-X-1.1.2-setup.exe` 461,092,957 字节 —
    `72e83967d35f31c423100a2952ae70d94d930d2a4c2e70777a895d3d5c6ed282`
  - `ATRI-X-1.1.2-portable-win64.zip` 683,825,959 字节 —
    `bcec36235c482e3b0c9cd4176705eec3323ad795f76b44471da8cdda211ac888`
  - `ATRI-X-1.1.2-repo.zip` / `ATRI-X-1.1.2-repo.tar.gz`：源码归档（工作树文件清单 1143 条目，
    不含 `runtime`/`build`/`dist`/缓存）。归档自身尺寸与 SHA-256 见发行库
    `SHA256SUMS-1.1.2.txt` 与 `ATRI-X-1.1.2-repo-entries.txt`（归档内文件不写自身哈希，避免自引用）。
- 门禁 7 项 0 失败：`check_syntax`（995 文件）/ `check_imports`（`modules=763 imported=763`）/
  `check_integration`（74 项）/ `check_coverage`（`packages=72 covered=72 weighted=100.0%`）/
  `check_except`（119 基线）/ `gen_stats --check`（README 数字一致）/ `check_secrets`（0 命中）。
- 全量单测：`Ran 6,814 tests in 520.7s`，`OK (skipped=4)`；本版定向回归 4 个模块 195 项
  （群友档案存储 / 提示词渲染 / 来源装配与隔离 / 共享线程换人 / `build_agent()` 集成 /
  GUI「参与对话」与预览）全部 OK。
- 产物级自检：onedir 24,492 文件 / 333.5 MB，加内置 DSH 内核（277.6 MB）与脱敏后的内置 NapCat
  （2,513 文件 / 1202.3 MB）后构成便携输入 27,005 文件 / 1,535.7 MB → 便携包 652.1 MB /
  27,010 条目解压自检全过 → 安装包 439.7 MB（ISCC 227.0 s、源文件 27,005；静默安装 96.5 s /
  27,008 文件、卸载 18.0 s）；构建总耗时 628.2 s。
- 脱敏：源码树 / 源码归档 / 内置内核 / onedir / 安装包（ASCII + UTF-16LE）/ 便携包文本条目 /
  发行库文本各层扫描 **0 命中**；第三方二进制的上游固有构建路径经 SHA-256 逐字节核对为原样文件，
  不含本机用户名与令牌。详见发行库 `自检报告.md` §三。

---

## [1.1.1] - 2026-10-28 — 修复「图转提示词」入口导流错误与入库生成参数留档失效（补丁版，取代 1.1.0 三产物）

> 本版是 **1.1.0 的补丁版**：只修两处实机问题，不新增功能、不改配置格式、不改工具 schema；
> 配置与会话数据完全向后兼容。`1.1.0` 的安装包 / 便携包 / 源码归档由本版取代。

旧状态（1.1.0 发行版，实机复现）：

- 「功能」页的「图片转提示词（image_to_prompt）」行指向 `advanced`（进阶页），但
  `[image_to_prompt]` 的字段实际注册在「工具权限 → 工具参数（全局）」的工具参数
  注册表里；进阶页只有生图 / 视觉回退 / ASR，双击过去**没有任何可配置项**。该行状态
  只按段内 `enabled` 判定，没配 provider + model（工具根本不会注册）时也读不出「未配置」。
- 群聊里发图后永远取不到图片自身记录的提示词与画师串，只得到"图片已被转码"的结果。
  根因是 `atri_x/app/runtime.py` 的 `_build_artist_metadata()` 从
  `atri_x.artist.metadata` 导入 `GenerationStore`——该模块只有容器解析，没有这个名字，
  导入必然 `ImportError`，又被裸 `except` 吞掉；于是**即使配置了
  `[image_generation].metadata_store`，入库留档库也永远是 None**。媒体管线把超过
  4 MiB / 长边 4096 的图转码成 JPEG 时销毁 PNG 文本块，此后 `artist_extract` /
  `image_to_prompt` 再无任何来源可读回参数。

新状态：

- `_build_artist_metadata()` 改从 `atri_x.artist.metadata_store` 导入 `GenerationStore`；
  装配成功记一条 INFO（`image generation metadata archive enabled`），失败记 WARN，
  不再静默降级。
- 「功能」页的 `image_to_prompt` 行：状态改为按三态语义显示 **未配置 / 已启用 / 已停用**，
  说明文字给出真正入口；双击跳到「工具权限 → 工具参数（全局）」并**展开**对应面板。
  `GuiApp.show_tab_key` 新增 `toolperm_params:<工具名>` 复合键（无冒号时行为不变）。
- 新增「图片生成参数留档（metadata_store）」功能行：显示是否开启、留档文件与"什么情况下
  参数会丢"，双击直达「画师串与生成参数留档（artist_*）」面板。
- 文档：便携版内附 `PATCH-NOTES-1.1.0-image-to-prompt.md`（根因、改动、验证与回滚）。

新增用例：

- `tests/test_app_tool_runtime_wiring.py::ArtistMetadataAssemblyCase`：配置
  `metadata_store` 必须装配成启用的 `GenerationStore`，且 `ArtistTools.Service.Generations`
  与媒体中心用的是同一个对象；
- `tests/test_media_manager.py::TestGenerationArchiveRoundTrip`：含 A1111 参数的大 PNG
  入库转码成 JPEG 后，按媒体 ID 仍能读回正向/负向提示词与采样参数（origin=archive）；
- `tests/test_gui_configedit.py::FeatureViewTestCase` 与
  `tests/test_gui_tabs.py::LazyTabBuildTest`：功能行状态/跳转目标与参数面板自动展开。

### 发行

- 版本号 `1.1.0` → `1.1.1`：`atri_x/__init__.py` / `pyproject.toml` / `deploy/VERSION` /
  `deploy/pack/deploy_lighthouse.py` / 仓库 `README.md` 同步；产物名随之变成 `ATRI-X-1.1.1-*`。
  本版是**补丁版**（图转提示词入口 + 图片生成参数留档），配置与会话数据向后兼容。
- 发行产物（本机从工作树重建；按发布要求**未提交 git、未推送 GitHub**）：
  - `ATRI-X-1.1.1-setup.exe` 461,623,999 字节 —
    `60e02d4f6142912e78ff156574f42c30668b4d20857a629f468613323f687176`
  - `ATRI-X-1.1.1-portable-win64.zip` 684,741,413 字节 —
    `b44051a7ddde2bbb37675b47c18aa179de672574eb6f53cf29e73959fd040eb3`
  - `ATRI-X-1.1.1-repo.zip` / `ATRI-X-1.1.1-repo.tar.gz`：源码归档（工作树文件清单 1136 条目，
    不含 `runtime`/`build`/`dist`/缓存）。归档自身尺寸与 SHA-256 见发行库
    `SHA256SUMS-1.1.1.txt` 与 `ATRI-X-1.1.1-repo-entries.txt`（归档内文件不写自身哈希，避免自引用）。
- 门禁 7 项 0 失败：`check_syntax`（989 文件）/ `check_imports`（`modules=758 imported=758`）/
  `check_integration`（74 项）/ `check_coverage`（`packages=72 covered=72 weighted=100.0%`）/
  `check_except`（119 基线）/ `gen_stats --check`（README 数字一致）/ `check_secrets`（0 命中）。
- 全量单测：`Ran 6789 tests in 1632.5s`，`OK (skipped=4)`；补丁定向回归 4 个模块（留档装配 /
  大图转码后参数回读 / 功能行三态与跳转 / 参数面板展开）全部 OK。
- 产物级自检：onedir 24,534 文件 / 335.5 MB，加内置 DSH 内核（277.6 MB）与脱敏后的内置 NapCat
  （2,513 文件 / 1202.3 MB）后构成便携输入 27,047 文件 / 1,537.8 MB → 便携包 653.0 MB /
  27,052 条目解压自检全过 → 安装包 440.2 MB（ISCC 305.4 s、源文件 27,047；静默安装 64.9 s /
  27,050 文件、卸载 9.0 s）；构建总耗时 705.2 s。
- 脱敏：源码树 / 源码归档 / 内置内核 / onedir / 安装包（ASCII + UTF-16LE）/ 便携包文本条目 /
  发行库文本各层扫描 **0 命中**；第三方二进制的上游固有构建路径经 SHA-256 逐字节核对为原样文件，
  不含本机用户名与令牌。详见发行库 `自检报告.md` §三。

## [1.1.0] - 2026-10-27 — QQ 协议端可切换（默认内置 NapCat，可选外置 SnowLuma）、四维在线状态、协议端按 PID 托管；桌面端新增「时间」选项卡

旧状态（1.0.10）：`qqonebot` 平台只有一条硬绑定 NapCat 的实现路径——配置里没有"协议端
类型"这个概念；运行期**从不调用 `get_login_info`**，所以后台 `/healthz` 的
`platforms[].connected` 只表示"OneBot WebSocket 连上了"，"QQ 是否已登录"这个状态在系统
里不存在，桌面端「在线」页也就无法区分"端口在听"和"真的能收发消息"。协议端进程启动后
`Popen` 返回值被丢掉，因此"停止协议端"只能靠打包期生成的 `Stop-NapCat.bat`，而它的实现是
`taskkill /f /im QQ.exe`——**会杀掉本机所有 QQ**（脚本注释自己也写着这一点）。另外
`get_file` 被当成"所有文件都能取"的通用动作，群/私聊文件下载在 SnowLuma 上会稳定失败。

### 新增

- `[platform.qqonebot].protocol_backend`（`napcat` / `snowluma`）：显式声明协议端类型；
  **默认是随包内置的 `napcat`**（开箱即用）：整个 `[platform.qqonebot]` 段不存在 = 新装 =
  `napcat`，段存在但键缺失/写空 = 1.0.x 旧配置 = `napcat`——升级不会自动切换。
  `snowluma` 是**可选的备用协议端**（外置接入、发行包不内置），只有用户显式选择才生效。
  它只决定"由哪一套接管 QQ"，**不参与** `ws_url`、令牌、唤醒词、平台名与权限作用域的解析
  （`platform.name()` 恒为 `qqonebot`）。非法取值在装配期抛 `InvalidError`，
  `atri-x config check` 也会告警。GUI 的「平台」页与 `proto_manager.configured_backend()`
  用**同一条**"段在不在"判据，默认值与"包里自带、能直接用"的那套一致。
- `atri_x/platform/qq_onebot/backend.py`：协议端差异表（转发 id 参数键、群/私聊文件直链
  动作、`get_file` 是否覆盖文件、私聊是否需要 `file_hash`、上传动作名）+ `detect_backend()`
  按 `get_version_info.app_name` 识别真身。
- 连上后的登录态握手：`adapter.handshake()` 调 `get_login_info` / `get_version_info`，
  失败只 WARN 不阻断；结果经 `set_login_notifier` 写进 `/healthz` 的
  `platforms[].login_ok / self_id / nickname / app_name / login_checked_at`（断线即清零）。
- 桌面端「在线」页把协议端卡片改成**四维状态**：进程运行（只认本程序托管的 PID）/ OneBot
  连通 / QQ 登录 / ATRI 平台接入；端口可达只作线索，并区分「协议端已连上但 QQ 未登录」。
  新增「停止协议端」按钮。
- 协议端管理层：`atri_x/gui/proto_setup.py`、`proto_process.py`、`proto_manager.py`、
  `snowluma_proto.py`。启动时把 `PID + 镜像名 + 启动时间` 记进
  `<协议端目录>/atri-x-proto-process.json`；停止只结束**自己启动的那棵进程树**，PID 被
  别的进程占用（镜像名不符）就放弃。
  SnowLuma 方向相反：**只读**它的 `config/onebot_<uin>.json` 取端口/令牌，不改第三方文件；
  启动用自带 `node.exe index.mjs` 且 `cwd` 固定为安装目录（它的 `launcher.bat` 没有
  `cd /d %~dp0`）；不代用户同意 EULA/隐私。
- 群/私聊文件下载走专有动作：`transport.get_chat_file_url()` 按群/私聊分派
  `get_group_file_url` / `get_private_file_url`（SnowLuma 的 `get_file` 只覆盖图片/语音），
  入站 `file` 段的 `file_hash` 与归属群号/用户号经 `MessageSegment.platform_meta` 传到
  下载点；`get_file` 只作旧后端兜底，SnowLuma 上直接给可行动报错。
- `transport` 新增动作薄封装：`get_login_info` / `get_version_info` / `can_send_image` /
  `can_send_record` / `get_group_file_url` / `get_private_file_url` /
  `upload_group_file` / `upload_private_file`（后两者返回 `file_id`，**不假装有 message_id**）。
- `smoke` 新增 `--backend auto|snowluma|napcat`，报告改成逐项 `checks[]` 并输出
  `detected_backend / app_name / login`。
- `devdocs/qq-backends-matrix.md`：协议端差异矩阵 + 真机验收清单（9 项）。

### 变更

- 令牌错误（HTTP 401/403）现在识别为 `ForbiddenError` 并固定 30s 慢速重试 + 可行动告警，
  不再和"服务没起"一起无限快速退避。
- `proto-info.json` 增加 `backend` / `backendVersion`；`Stop-NapCat.bat` 改为委托
  `atri-x.exe -m atri_x.gui.qqproto --stop`（新增 `--stop` / `--status`），不再按进程名杀。
  发行包声明了非 NapCat 的内置协议端时直接拒绝（许可限制）。
- 桌面端「平台」页新增「协议端」下拉：新装（段原本不存在）预选 **NapCat**（默认、随包内置），
  段存在则显示解析后的生效值；切到 SnowLuma 需二次确认，保存后回读断言；`values()` 永远写
  显式字符串（`patch_section` 的 `None` = 删键，"未配置"会在下次读取时被重新解释成默认值）。
- `service._prepare_qqproto` 改走 `proto_manager`，并把协议端实际监听地址与令牌
  **只补空值**（用户已写好的 `ws_url` / 令牌一律不动）。`configedit.verify_config()` 成为
  平台页与工具预览共用的"保存后回读严格解码"入口。

### 修复（本版发行前修正）

- **默认协议端最终定为内置 NapCat，SnowLuma 改为可选的备用方案**：本版初候选曾把
  SnowLuma 设为新装默认（外置接入、需用户自行安装并完成首启），实机试用后判断"开箱即用"
  更重要。现在 `NEW_INSTALL_BACKEND = napcat`：整个 `[platform.qqonebot]` 段不存在 = 新装 =
  NapCat，段存在但 `protocol_backend` 缺失 = 1.0.x 老配置 = NapCat；用户显式在「平台」页
  切到 `snowluma` 并保存后才走外置接入。内置 NapCat 与默认值一致，装好即可连；选了
  SnowLuma 但本机没装好时，「在线」页仍会提示"本程序包内置 NapCat，可切回去用"。
  新增/更新用例：新装写 `protocol_backend = "napcat"`、段缺失/缺键 → NapCat、显式
  `snowluma` → 走外置、SnowLuma 不可用但包内有内置 NapCat 时原因带切换提示。
  同步把教程与各文档的定位从"默认协议端"改为"可选备用协议端"（`docs/snowluma.md` 仍可
  单独转发，但开头明确"默认是 NapCat，不折腾就不用看"）。

- **「API 协议 = response」时推理思考强度不再 400**：`reasoning_effort` 是协议无关的
  一等配置，实现上折进 `extra_payload` 后由适配器原样写进请求 JSON。chat 协议发顶层
  `reasoning_effort` 没问题，但 Responses 协议里这个字段叫 `reasoning.effort`：顶层写法
  会被 OpenAI 官方与严格校验的网关直接拒掉（实测 opencode.ai：`HTTP 400
  reasoning_effort: requires a native Responses target; portable routing requires the
  full conversation in input`，响应协议本身是通的，去掉该字段或改成 `reasoning.effort`
  就 200）。现在 `ResponsesAdapter.build_body()` 在合并 extra payload 之后调用新增的
  `fold_reasoning_effort()`：把顶层 `reasoning_effort` 折进 `reasoning.effort`；
  如果调用方已用 `extra_payload` 显式给出 `reasoning` 对象则以显式值为准（只在其缺少
  `effort` 时补上），显式值不是对象时保留显式值、丢弃专用字段并记日志；折叠只改本次
  请求体，不回写 provider 配置。chat 适配器行为不变；GUI 提示、`services.toml` 注释、
  `docs/configuration.md` / `docs/windows-app.md` 同步说明"字段形态跟协议走"。
  新增用例：`tests/test_openai_responses.py::TestResponsesReasoningEffort`（9 项）与
  `tests/test_app_llm_adapters.py` 里 response 协议下的 Provider 级 / 模型级 / 显式
  `reasoning` 优先三项。

- 版本号 `1.0.10` → `1.1.0`：`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION` 与仓库 `README.md` 同步。本版是**功能版**（协议端可切换 + 桌面端「时间」选项卡），不是修 bug 版；配置与数据向后兼容（旧配置缺 `protocol_backend` 时按 NapCat 解释）。

### 文档（发行前补录）

- 新增独立教程 `docs/snowluma.md`（**SnowLuma 使用与配置教程**）：从解压、首次手动启动、
  WebUI 初始凭据与强密码规则、进程注入/QQ 登录、OneBot v11 正向 WS 端点（默认 3001 / 令牌）、
  `ATRIX_SNOWLUMA_DIR` 与四个自动探测目录、桌面端「平台 / 在线」两页接入顺序、仅补空值的
  地址/令牌同步规则、冒烟与日志、故障排查、许可与账号风险，到卸载清理与维护者附录。
  该文独立成篇，可脱离其它文档单独转发；`docs/README.md`、`README.md`、`docs/windows-app.md`
  的 SnowLuma 小节与 `devdocs/qq-backends-matrix.md` 均已链接过去，并把 §5.1 里"手工填 ws_url 与
  令牌"的旧步骤改为"设 `ATRIX_SNOWLUMA_DIR` → 重启桌面端 → 自动补全空值"。
- 顺带订正两处与实机 2.2.0 不一致的表述：SnowLuma 的 `consent.json` 实际写
  `{version, acceptedAt}`（ATRI-X 的 `needs_consent` 启发式会误报一行"还没完成首启"，不影响
  使用，已在教程 §7.5 标注）；`SNOWLUMA_TELEMETRY` 在 2.2.0 发行包里未出现，遥测披露改为
  "以官方条款为准、升级后复核"。
- 本轮只改文档，不重打 exe / 便携包（二进制产物与 1.1.0 已验收版本一致）；源码归档
  `ATRI-X-1.1.0-repo.zip/.tar.gz` 与条目清单随之重建。
- 随后默认协议端改回 **NapCat**（见上一节），教程与交叉链接同步改成"SnowLuma 是可选备用
  协议端"的定位，并按新默认重打 exe / 便携包（产品行为变化，不能只改文档）。

#### 桌面端新增「时间」选项卡（统一时间服务的 GUI 入口）

旧状态（1.0.10）：统一时间服务只有三个入口——配置文件（`[time]` / `[time.verification]` /
`[[time.sources]]`）、聊天里的 `/time` 命令（仅超管）、受 token 保护的 `/diagnostics`。
桌面端的 11 个选项卡里**没有时间页**，所以"时区现在是什么、tzdata 到底能不能加载、网络
校验处于什么状态、每条来源最近一次看到什么"都只能手改 TOML 或自己拉诊断接口。

#### 新增

- 主窗口新增「时间」选项卡（界面 `atri_x/gui/tabs/timepanel.py`，数据层
  `atri_x/gui/timeview.py`，来源表格在 `atri_x/gui/timesources.py`，后台状态面板在
  `atri_x/gui/timediagnostics.py`，整页可竖向滚动），把能调整和能阅读的条目一次列全：
  - **现在**（只读，每秒刷新）：业务时间源 / 生效时区 / 解析后时区与规则 / 当前时间 /
    UTC / UTC 偏移 / 星期 / unix 时间戳 / 是否注入提示词 / tzdata 版本 / IANA 时区数 /
    时区数据是否可用；
  - **时区探针**（只读，手动触发）：复用 `python -m atri_x.app.time_check` 的实现，
    看 tzdata 能否真的加载 + DST 冬夏偏移断言，**不触网**；
  - **`[time]`**：时区（候选含常用 IANA 名 / `fixed:` / `system`，也可手输）、是否把当前
    时间注入每轮系统提示、网络样本有效期；
  - **`[time.verification]`**：总开关、`mode`（首版仅 `verify_only`）与 13 个间隔 /
    抖动 / 超时 / 并发 / 重试 / 阈值 / 分组参数，每项带取值范围提示；
  - **`[[time.sources]]`**：表格列出全部来源，右侧表单可增删改（id、协议、SNTP 主机端口
    或 HTTPS URL、独立性分组、私网例外、启用、单来源超时）；
  - **后台运行状态**（只读）：读取后台 `/diagnostics` 的 `time` 段——校验状态、最近一轮 /
    最近成功 / 下次计划、样本年龄、本地偏差与来源间极差、每条来源最近一次观测、告警。
- `atri_x/gui/configedit.py` 新增 `write_array_tables()`：数组表（如 `[[time.sources]]`）
  整份重写——旧块在**原位置**替换、文件里的注释与其它小节不动，空列表 = 清空该数组表。
- `atri_x/gui/featureview.py`：「功能」页新增「时间服务（时区 / 网络校验）」一行，双击跳到
  「时间」选项卡。

#### 变更

- 桌面端选项卡 11 → 12 个（索引 8 = 时间，日志 / 功能 / 工具权限顺延为 9 / 10 / 11）；
  懒加载策略不变（启动只建「对话」+「功能」）。
- 界面校验与后台配置层共用**同一份** `TimeConfig.normalized()`：非法时区、越界数值、
  重复来源 id、非 `https://` 的来源 URL 在保存前就报错并且**不落盘**；保存成功后先回读
  磁盘再写状态行（避免"已载入"把"已写入"盖掉）。

#### 已知限制

- 界面**不触发**网络校验（手动触发仍只有 `/time verify`），也不会修改系统时钟。
- 「后台运行状态」需要后台服务在运行且 `.env` 里有 `ATRIX_OPS_TOKEN`（桌面端启动时会自动
  补齐）；后台未运行或版本较旧时该区块只显示提示，不影响其它区块。

### 已知限制

- **不内置 SnowLuma**：其 EULA §5.4 规定"并入第三方安装包 / Docker 镜像 / 自动化脚本
  部署须事先书面授权"，专有原生组件也不得单独再分发。SnowLuma 只支持"用户自行安装 +
  外置接入"；ATRI-X 的 Apache-2.0 不覆盖它。
- 下列 SnowLuma 行为**待真机验证**（见 `devdocs/qq-backends-matrix.md` §2）：合并转发节点
  只带 `content` 是否被接受、`base64://` 大媒体上限、仅 `file_id` 无 `file_hash` 的私聊
  文件下载、引用 id / 负 message_id 语义、重连后 message_id 稳定性、未注入时行为、
  入站整数字段类型（若发字符串会整条断线）。
- NapCat 与 SnowLuma 都是往 `QQ.exe` 注入的 hook，**同一台机器不能同时接管同一个 QQ**；
  切换需先停旧的一套（界面已在确认框里写明顺序）。

### 发行

- 版本号 `1.0.10` → `1.1.0`：`atri_x/__init__.py` / `pyproject.toml` / `deploy/VERSION` / 仓库
  `README.md` 同步；产物名随之变成 `ATRI-X-1.1.0-*`。本版是**功能版**（协议端可切换 +
  桌面端「时间」选项卡），配置与会话数据向后兼容。
- 发行产物（本机从工作树重建；按发布要求**未提交 git、未推送 GitHub**）：
  - `ATRI-X-1.1.0-setup.exe` 461,624,363 字节 —
    `26282ca2f8188698a5b001743ea2d4935821ed69cc0492915d91729f94f9571b`
  - `ATRI-X-1.1.0-portable-win64.zip` 684,735,116 字节 —
    `34abfda43203014a5bf3d2624e2d7d7e955329419be6eb7de11519783da943d8`
  - `ATRI-X-1.1.0-repo.zip` / `ATRI-X-1.1.0-repo.tar.gz`：源码归档（工作树文件清单 1135 条目，
    不含 `runtime`/`build`/`dist`/缓存）。归档自身尺寸与 SHA-256 见发行库
    `SHA256SUMS-1.1.0.txt` 与 `ATRI-X-1.1.0-repo-entries.txt`（归档内文件不写自身哈希，避免自引用）。
- 门禁 7 项 0 失败：`check_syntax`（989 文件）/ `check_imports`（`modules=758 imported=758`）/
  `check_integration`（74 项）/ `check_coverage`（`packages=72 covered=72 weighted=100.0%`）/
  `check_except`（119 基线）/ `gen_stats --check`（README 数字一致）/ `check_secrets`（0 命中）。
- 全量单测：`Ran 6782 tests in 1161.6s`，`OK (skipped=4)`；定向回归：协议端 134 项、时间服务
  139 项全部 OK。
- 产物级自检：onedir 27,048 文件 / 1,537.8 MB（内置 DSH 内核 384 包、内置 NapCat `4.18.38` +
  便携 QQ `9.9.33-52230`）→ 便携包 653.0 MB / 27,053 条目解压自检全过 → 安装包 440.2 MB
  （ISCC 300.7 s、源文件 27,048；静默安装 66.3 s / 27,051 文件、卸载 9.4 s）；构建总耗时 663.1 s。
- 脱敏：源码树 / 源码归档 / 内置内核 / onedir / 安装包（ASCII + UTF-16LE）/ 便携包文本条目 /
  发行库文本各层扫描 **0 命中**；第三方二进制的上游固有构建路径经 SHA-256 逐字节核对为原样文件，
  不含本机用户名与令牌。详见发行库 `自检报告.md` §三。

---

## [1.0.10] - 2026-10-27 — 内置时间服务 + 多来源校验（verify_only）、tzdata 硬依赖、调度跳时修复

旧状态（1.0.9）：
- **提示词里没有"当前时间"**：模型唯一能看到的时间事实是 `session_created_at=`，跨午夜的
  对话会把会话创建时间当成"现在"；没有任何请求级时间基准。
- **时区语义是隐式的机器本地时区**：`cron.model.system_location()` 直取
  `datetime.now().astimezone()`；`[quiet].timezone` 非法时静默回落本地；`shanghai_tz()`
  缺时区数据时静默退固定 `+08:00`。
- **时区数据是构建机偶然产物**：`pyproject.toml` 的 `dependencies = []`，源码与打包脚本都
  没声明 `tzdata`；Windows 便携包里之所以有 IANA 数据，是因为构建机 site-packages 恰好装了
  它、被 `pyinstaller-hooks-contrib` 顺带收集。换干净构建机会**静默失去**时区数据。
- **消息接收时间没有契约**：用户 `Message.created_at` 由落库时补，平台发送时间只进
  ChatHistory，不进 Agent。
- **没有网络时间校验**：本地时钟错 8 小时只能靠人发现；诊断里没有时间源/时区/校验状态。
- **调度等待不复核目标时间**：cron 用墙钟算一次 `delay` 就 `asyncio.wait_for` 睡到底；
  前跳/回拨/休眠后可能提前或迟到触发。一次性任务存本地墙钟字符串，每次按"当前系统时区"
  重新解释，改时区后旧任务语义漂移。组内提醒用 `datetime.now()`，停机恢复后会集中补发。

### 新增

- `atri_x/timezones.py`：唯一时区解析入口（IANA / `fixed:+08:00` / `system`），非法名与
  数据缺失**分成两个异常硬失败**，不回落。
- `atri_x/time_service.py`：`TimeService` + `TimeSnapshot`（同一 instant 的一致快照）、
  可注入假时钟、墙钟/单调时钟跳变检测、网络校验状态只读暴露。
- `[time]` / `[time.verification]` / `[[time.sources]]` 配置段（`atri_x/config/time_sections.py`）：
  默认 `timezone = "Asia/Shanghai"`、`inject_context = true`、联网校验**默认关闭**且
  只允许 `verify_only`（`takeover` 直接拒绝）；不内置任何公共时间端点。
- 请求级时间注入（`atri_x/agent/time_context.py` + `chat_llm.call_llm`）：在
  `system_append` 之后、DSH 路由与预算之前注入带稳定标记 `[atx-time:v1]` 的一段，
  **先剥旧段再追加**，工具续轮 / 视觉重试 / DSH / 后台任务都只有一份。
- 入站时间基准：平台适配器在 `handle_message` 入口采一次 `message_received_at`
  （QQ / Telegram 还带 `platform_sent_at`），写进 ctx 与 `Message.metadata` 的
  `received_at` / `platform_sent_at`；**不改** `created_at` 语义。
- 内置工具 `get_current_time`（只读、不联网；带来源与校验状态）与超级管理员命令
  `/time`（状态 / `verify` / `zones`）。
- `atri_x/timesync/`：受限 SNTP 客户端（单播、48 字节、逐字段校验、`authenticated` 恒
  False）、HTTPS JSON 时间适配器（TLS 永不关闭、内容类型/新鲜度/大小/可疑偏差校验）、
  出站策略（私网例外、URL 凭据拒绝、脱敏）、间歇校验器（单飞、抖动、退避、挂起检测、
  可疑状态锁存）、只读诊断聚合。
- `/diagnostics` 新增 `time` 段；`doctor` 新增时间能力检查（时区解析 / tzdata / 注入 /
  校验配置），可在**干净机器**上直接验证冻结产物能不能真加载 IANA 数据。
- `python -m atri_x.app.time_check --json`：无网络、无 Agent、无数据库的时区探针（含
  DST 冬夏断言），供构建流水线与部署验收使用。

### 变更

- `tzdata` 成为**硬依赖**：`pyproject.toml` 的 `dependencies` 写入 `tzdata>=2024.1`；
  `packaging/atri-x.spec` 显式收集并断言 `tzdata/zoneinfo/Asia/Shanghai` 存在（缺则构建
  失败）；`packaging/build.py` 的产物级核验新增 payload 检查与冻结探针；
  `deploy/Dockerfile` 显式 `apt-get install tzdata` 并在构建期跑探针；`deploy/README.md`
  补验证命令。安装版 / 便携版因此**开箱即用**，不再依赖构建机碰巧装了什么。
- 旧配置没有 `[time]` 段时按 `Asia/Shanghai` 解释（此前是进程本地时区）。要恢复旧语义请
  显式写 `timezone = "system"`；`config check` / 启动日志 / `/diagnostics` 都会显示生效时区。
- `[quiet].timezone` 留空改为沿用 `[time].timezone`；显式非法值在配置加载期失败，不再
  静默回落机器时区。
- `shanghai_tz()` 缺时区数据时改为抛 `TimezoneDataUnavailableError`（峰谷计价口径），不再
  静默退固定 `+08:00`。
- cron：一次性任务落库新增 `run_at_utc` / `run_at_tz` / `run_at_dst_ambiguous`，读取时
  优先用确定的 UTC 时刻（旧数据仍按本地墙钟解释）；DST 不存在的时刻创建/更新当场拒绝，
  重复时刻取第一次；调度等待改为分段 + 醒来复核目标时间（回拨不提前、前跳/恢复触发一次）；
  组内提醒改用时间服务，停机超过 24h 的待发提醒标记 `skipped`（不集中补发）。
- 时区仍完全可配置：任何 IANA 时区都可写进 `[time].timezone`（全量 tzdata 随包），
  `/time zones` 可列出；`fixed:` 与 `system` 两种限制模式在文档与诊断里明确标注。

### 修复

- `get_current_time` 注册同步点：`tool/builtin/register.py`、`gui/toolcatalog.py`、
  装配测试计数（`REGISTERED_TOOLS` 50→51、`ALWAYS_REGISTERED_TOOLS` 14→15）。
- Windows / Docker 时区数据交付从"构建机偶然产物"改为**可断言、缺失即失败**。

### 已知限制 / 未验证

- 首版**不做**受控接管（`mode = "takeover"` 被拒绝）：网络时间只产生诊断，不修改系统
  时钟、不改变业务时间。
- NTS（加密 NTP）未实现；SNTP 永远是 `authenticated = false`，任何文案都不得声称"已认证"。
- 多来源"独立性"只能由管理员在配置里声明（`group`），无法自动证明不同运营方/不同路径。
- Docker 镜像与冻结产物的时区探针需要在**有 Docker daemon / 干净 Windows** 的机器上跑；
  本机（无 Docker daemon）未验证容器侧，最终报告里按"未验证"记录。

---

---

## [1.0.9] - 2026-10-26 — 修复前台会话工具链路（workspace / shell / 全部路径类工具失败）

旧状态（1.0.8）：
- **前台会话（桌面端、`python -m atri_x run`、CLI 服务端）里工具链路整体不可用**：
  `workspace` 工具设置报 `set workspace: '_SessionWorkspaceStore' object has no attribute
  'set_workspace_dir'`、只查询报 `load workspace: ... has no attribute 'get_workspace_dir'`、
  `reset` 报 `... has no attribute 'clear_workspace_dir'`（用户抓到的原始通知：
  `[tool] workspace 调用失败：set workspace: '_SessionWorkspaceStore' object has no attribute
  'set_workspace_dir'`）。
- 同一根因还打断两类工具：`shell`（前台没有沙盒目录时要用当前 workspace 当 cwd）报
  `load workspace: ... has no attribute 'get_workspace_dir'`；所有路径类工具
  （`read_file` / `edit_file` / `send_file` / `view_image` / `rollback_file`）同样失败——
  连**绝对路径**也中招，因为 `resolve_workspace_path` 会先把 `current_workspace_dir()` 当
  base_dir 取一次。
- 用户可见症状：模型调工具连续失败后**放弃工具、改猜答案**（实测问「现在几点钟」时
  shell 两次失败、workspace 两次失败，最后从会话元数据推算了一个时间并回答"环境工具坏了"）。
- 根因：`atri_x/agent/toolrun_adapter.py` 的 `prepare_tool_context()` 注入的是 Python 移植
  中途的残留桩 `_SessionWorkspaceStore`（只有 `load`/`save`，而它们按名字去调
  `agent.session_workspace` / `agent.persist_session_workspace`——这两个方法**全仓不存在**，
  所以即使被调用也是空操作）。工具层要的是 `atri_x/tool/workspace.py` 的
  `WorkspaceStore` / `WorkspaceAgentNoticeStore` 异步协议（`get_workspace_dir` /
  `set_workspace_dir` / `clear_workspace_dir` + 三个 notice 方法）。正确实现
  `atri_x/agent/workspace.py::SessionWorkspaceStore` 一直存在，`tool_directive.py` 用的就是它，
  **只有真正执行工具的 toolrun 适配层接错**。
- 为什么测试没抓到：`workspace` 工具的所有单测都手写假 store（实现了协议），没有任何用例
  断言 `prepare_tool_context` 注入对象的类型/能力——和 `toolrun/runner.py` 里记录过的
  "假实现让单测全绿" 是同一类事故。

### 修复

- `atri_x/agent/toolrun_adapter.py`：`prepare_tool_context()` 改用
  `atri_x.agent.workspace.SessionWorkspaceStore(agent, session)`，并删除残留桩
  `_SessionWorkspaceStore`。workspace 目录与「AGENT 提示已发」标记恢复写进 session metadata
  （`workspace_dir` / `workspace_agent_notice_dirs`）。
- 回归测试（`tests/test_toolrun.py`）：新增 `WorkspaceStoreWiringTests`——
  - `test_prepare_tool_context_injects_real_workspace_store`：断言注入对象
    `isinstance(store, SessionWorkspaceStore)` 且满足 `WorkspaceStore` 协议、`workspace` 工具
    set 后目录真的落进 session metadata、残留桩已从模块里消失；
  - `test_workspace_tool_accepts_dot_in_session_context`：`{"path": "."}` 在真实会话上下文里成功。
- 顺带修复两个小问题（同一轮里让模型多踩两次的噪声）：

  1. **通知发送失败日志丢字段**（`atri_x/delivery/delivery.py`）：把 `_output_log_attrs()`
     返回的属性元组当成**一个**位置参数传给 slog 风格 logger，格式化器把非字符串键渲染成
     `!BADKEY`，日志变成 `level=WARN msg="notice output failed %s" !BADKEY=!BADKEY`，
     platform / error 全丢。现在按 Go 风格展开成 `key, value` 位置参数，日志恢复
     `msg="notice output failed" platform=... error=...`；外部传标准库 `logging.Logger` 时
     退回 `k=v` 文本（不再 `TypeError`）。回归测试见 `tests/test_delivery.py::TestManager`
     的两条 `test_send_notice_failure_*`。
  2. **`workspace {"path": "."}` 报误导性的 `workspace path is required`**
     （`atri_x/tool/workspace.py::validate_workspace_dir`）：Go 只认绝对路径，`"."` 归一后
     仍等于 `"."`，于是被判成"空路径"。现在相对路径先按**当前工作目录**解析成绝对路径再校验：
     空路径才报 `required`，存在但不是目录报 `workspace path is not a directory`；
     `workspace` 工具描述同步说明"相对路径按当前工作目录解析"。回归测试见
     `tests/test_builtin_files.py::TestWorkspaceTool` 的三条 `test_validate_workspace_dir_*` /
     `test_workspace_tool_accepts_dot`。
- 版本号 `1.0.8` → `1.0.9`：`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION`、
  `README.md`（当前版本与 Release 链接）同步。
- 发行重建：重新 PyInstaller onedir → 便携版 zip → Inno 安装包，并重打全仓库源码归档
  （含内置 DSH 内核与脱敏后的 QQ 协议端）。

### 发行

- 版本号 `1.0.8` → `1.0.9` 随本版提交同步（`atri_x/__init__.py` / `pyproject.toml` /
  `deploy/VERSION` / `README.md`）。
- 产物级自检：全量单测 `Ran 6495 tests` 全部 `OK`（1504.5 s，199 个测试模块逐个执行）；
  门禁 0 失败（`check_syntax` 952 文件 / `check_imports` 734/734 / `check_integration` 74 项 /
  `check_coverage` 100% / `check_except` 119 = 基线 / `check_secrets` 0 命中 / `gen_stats`）；
  便携版解压 27,045 条目、10 项断言全 ok；安装包静默安装 27,043 个文件后卸载清空。
- 脱敏复查（源码树 / 源码归档 / 内置 DSH 内核 / onedir / 安装包 / 发行文本）：全部 **0 命中**；
  便携包文本条目 0 命中。便携包内 7 个第三方二进制（腾讯 QQ / NapCat 的 `libcurl.dll`、
  `librtmp.dll`、`crashpad_handler*.exe`、`ilink2.dll`、`QBar.dll`、`BugReport.dll`）自带的上游
  构建机路径与构建输入 SHA-256 逐字节一致，不含本机用户名 / 用户目录 / 令牌（与 1.0.8 同源）。
- **已推送 GitHub**（`https://github.com/fujiwarashigure/ATRI-X2`）：`main` 提交 `49d116e`
  （`release: ATRI-X 1.0.9 …`），tag `v1.0.9` 指向同一提交；GitHub Release `v1.0.9` 附带七个
  资产（安装包 / 便携版 / 便携版 `.sha256` / 源码归档 zip 与 tar.gz / `SHA256SUMS.txt` /
  `ATRI-X-1.0.9-compatibility-report.md`）。四件产物的尺寸与 SHA-256：
  - `ATRI-X-1.0.9-setup.exe` 461,122,659 字节 —
    `c791410751ac3406dff7c8707ff6429e9da8baf06e4325a5ee917547c0fe2d18`
  - `ATRI-X-1.0.9-portable-win64.zip` 684,255,875 字节 —
    `3f75f9045aa19d94560e96cd288a67a089dffc2b381effef25ae2ec60bf5d8cb`
  - `ATRI-X-1.0.9-repo.zip` 10,408,007 字节 —
    `e7f30c7dfe2952bc95b5415ea390b72e1eb48b01d90f1dd2049c295d7a50641f`
  - `ATRI-X-1.0.9-repo.tar.gz` 9,438,440 字节 —
    `7eeca8f595f104419831d7aabc413c8be0c4f03f2158b1380f995e2918d333af`

## [1.0.8] - 2026-10-29 — 修复「人设卡 → 表情图库」子选项卡整页空白

旧状态（1.0.7）：
- 桌面端打开「人设卡 → 表情图库」子选项卡，**整页空白**：看不到发送策略表单、图库列表
  和全部按钮，因此也没法编辑。这个缺陷从 1.0.4 引入本地表情图库起就存在。
- 根因：`atri_x/gui/reactionpage.py` 的 `ReactionPanel` 把 60 个控件全建在自己的
  `self.frame` 上，而 `CharacterTab._build_reaction_page()` 只把外层 page 加进 notebook，
  **从未 pack 过 `panel.frame`**。Tk 不显示未布局的容器，于是整个子选项卡只剩一个空页。
  `tests/test_gui_tabs.py` 里原有用例只验证数据层（策略落盘、`catalog.json` 读写），
  没有断言布局，所以 1.0.4–1.0.7 一直没被发现。
- 同一处还有第二个缺陷：`CharacterTab.show_reactions_tab()` 用 `notebook.select()` 选的是
  面板内部 frame，而它不是 notebook 的页框；从「功能」页双击「本地表情图库」跳转时会抛
  `TclError: ... is not managed by ...`，跳转失败。

### 修复

- `atri_x/gui/tabs/character.py`：`_build_reaction_page()` 把 `reaction_panel.frame`
  显式 `pack(fill=tk.BOTH, expand=True)` 进子选项卡页框；新增 `self.reaction_page` 指向
  真正的 notebook 页框，`show_reactions_tab()` 改为选它（页面已销毁时静默返回）。
- 验证（源码 GUI 实测）：修复上面板的 `winfo_manager()` 为 `''`、页面里 0 个可见控件；
  修复后为 `pack`、60 个控件全部可见，`show_tab_key("character_reactions")` 不再抛错。
- 回归测试（`tests/test_gui_tabs.py`，新增 4 条）：
  - `CharacterReactionTabCase.test_reaction_sub_tab_is_laid_out`：面板必须被 pack、子选项卡
    页里正好是面板 frame、图库 Treeview 已布局；
  - `CharacterReactionTabCase.test_show_reactions_tab_selects_its_page`：跳转必须选中
    notebook 页框（选面板内部 frame 会 TclError）；
  - `LazyTabBuildTest.test_feature_jump_to_reaction_sub_tab`：「功能」页跳转端到端；
  - `LazyTabBuildTest.test_every_tab_and_sub_tab_page_is_laid_out`：把 11 个顶层页和它们
    的所有子选项卡全扫一遍，要求每页至少有一个已布局子控件——这类「忘了 pack → 整页空白」
    的缺陷以后再出现会直接测试失败。

## [1.0.7] - 2026-10-28 — 桌面端按需构建选项卡（修复窗口拖动明显迟滞）

旧状态（1.0.6）：
- 桌面端窗口拖动有明显迟滞感。实测：主窗口下有 **2434 个 Win32 子窗口**
  （Tk 每个控件都是一个子窗口），每次 `SetWindowPos` 移动约 **6 ms**；而裸 Tk 窗口
  同样移动约 1 ms——移动成本随子窗口数线性上升，鼠标拖动时消息队列堆积，窗口跟手变差。
- 根因：`GuiApp._build_ui()` 启动时**一次性构建全部 11 个选项卡**，其中「工具权限」
  一页就有 1811 个控件（4 个子页：chat 517 / work 517 / 工具参数 722 / 预览 32），
  这些页在启动时全部建成，即使只有「对话」页可见。

### 修复

- `atri_x/gui/app.py`：选项卡改为**按需构建**。启动只真正建「对话」（默认页）与
  「功能」（状态栏轮询要写它），其余 9 页先放占位 `ttk.Frame`，首次切到该页时
  才在占位页里构建（`_ensure_tab()` + 同名属性代理，外部代码 `app.model_tab` 等写法不变）。
  懒加载页构建失败时在聊天区提示并写 journal，不会让窗口卡死。
- 效果（同一台机器实测 `SetWindowPos` 每次移动耗时）：

  | 状态 | 子窗口数 | 每次移动 |
  |---|---|---|
  | 1.0.6（全量预建） | 2434 | ~6.3 ms |
  | 1.0.7 启动（只建对话+功能） | 50 | ~1.0 ms |
  | 1.0.7 打开「模型」…「日志」后 | 206–622 | 1.3–2.1 ms |
  | 1.0.7 打开「工具权限」后 | 2427 | ~6.3 ms（该页自身控件多，见下） |

- 回归测试（`tests/test_gui_tabs.py`）：`LazyTabBuildTest` 断言启动只构建 2 页、
  启动控件数 < 150、notebook 顺序/标题不变、重页首次访问才建且不重复建。

### 已知限制

- 「工具权限」页自身有 1811 个控件，打开后该页仍会回到 ~6 ms/次移动；
  其它页已恢复顺滑。后续可把该页 4 个子页也改成按需构建。
- 机器整体负载高（例如后台跑着 3D 游戏时）拖动本来就会变慢，与本修复无关。

## [1.0.6] - 2026-10-27 — 修复后台服务启动即崩（工具预览配置无限递归）

旧状态（1.0.5）：
- 后台服务（`atri-x service run`，GUI 首次启动也会自动拉起）在 Agent 初始化阶段就抛
  `RecursionError` 退出，stderr 只有一行 `atri-x: maximum recursion depth exceeded`；
  CLI 远程服务端 `ws://127.0.0.1:32172/cli/v1/ws` 因此**从未监听**，GUI 报
  「后台服务未就绪：HandshakeError: 连接 … 失败」，QQ OneBot 平台自然连不上。
- 根因在 `atri_x/toolrun/preview.py` 的 `_section_dict()`：为拿 `[tool_preview]` 的
  配置，它按 `value is not config` 递归展开 `normalized()` 的返回值；而
  `ToolPreviewConfig.normalized()` 每次调用都返回**新实例**，该条件恒为真，递归无上界。
  配置校验（`config check`）、GUI 自检与单测都不走这条构造路径，所以发布前自检没覆盖到。

### 修复

- `_section_dict()` 只在两种情况下采用 `normalized()` 的结果：返回**映射**时直接采用；
  返回**另一种类型**的对象时有界展开（最多 `_MAX_NORMALIZE_DEPTH` 层）；
  同类型对象不再递归——需要的 7 个字段已经从配置对象本身取到了。
- 回归测试（`tests/test_toolrun.py`）：
  `test_preview_texts_from_dataclass_config_does_not_recurse`（真实
  `ToolPreviewConfig` 不再递归）与 `test_preview_texts_from_wrapper_config`
  （包装对象返回映射的旧意图仍然保留）。
- 打包自检补漏（`packaging/build.py`）：新增 `service_smoke`，用默认配置真实起一次
  `service run`，确认 `Agent` 能构造、CLI WebSocket 端口能监听。此前
  `--version` / `config check` / `-m` / GUI `--selftest` **都不构造 Agent**，
  正是这条漏检让 1.0.5 带着「后台服务必崩」的包发了出去。
- 验证：用真实用户配置跑 `python -m atri_x service run`，32172 在 0.5 s 内监听，
  GUI 同款探测（`atri_x.gui.service.probe_server`）WS 握手返回 ok；配置检查与原有
  单测保持通过。

## [1.0.5] - 2026-10-26 — 确认安全边界 + 记忆可见性（借鉴 dsh-qq 第一批）

> 本版落地《dsh-qq 借鉴落地评估》第一批的 P0 两项：**高风险确认的授权边界**与
> **常驻记忆的会话可见性**。两项都是"旧行为是漏洞、新行为是默认安全"的改动，
> 不需要新配置项；已有配置文件原样启动即可。

旧状态（1.0.4）：
- 高风险工具确认只看 Session，不看发言人：共享线程群（`thread_mode = "group"`）
  里任何群成员都能回 `/confirm`、`/confirmall`、`/reject`；确认提示里的参数预览
  直接发到群里，所有成员可见；`/confirmall` 一次放行本会话后续所有高风险工具。
- 常驻记忆按 `(platform, actor_id)` 存整段 core/normal，读取时**不带会话**：
  同一个人在私聊里说过的事，进群后仍会被注入；`resident_memory_normal` 的 `write`
  覆盖整段 normal，写一次群记忆会连带覆盖私聊记忆。

### 安全 — 高风险工具确认（对应评估 §B0）

- `turn.RiskConfirmation` 新增 `actor_id` / `redacted` 两个字段（Python 版扩展）。
  `agent.toolrun_adapter` 创建确认时写入发起人 `actor_id`，群聊 / 频道里把参数与
  工具自定义详情标成 `redacted`。
- 确认类命令（`/detail`、`/confirm`、`/confirmtool`、`/confirmall`、`/reject`、
  `/stop`）**只接受发起人本人**：`agent.input.handle_risk_confirmation_input` 在解析
  出指令后先比对发言人，不匹配就拒绝并记审计 `foreign_actor`；超级管理员是唯一
  例外（运维解锁）。空 `actor_id`（历史来源）按旧行为放行，只记审计。
- 群聊 / 频道里**不展示工具参数**：确认提示给「参数：已隐藏（群聊中不展示）」，
  `/detail` 走 `REDACTED_ARGUMENTS_TEXT`，三处详情渲染器
  （`agent.risk_confirmation`、`agent.input`、`agent.toolrun_adapter`）行为一致。
- **群聊禁用 `/confirmall`**：`/ca` 同样拒绝，提示改用 `/confirmtool` 或 `/confirm`；
  私聊与 CLI 行为不变。等待提示文案由 `confirmation_allows_confirm_all()` 决定是否
  出现 `confirmall`。

### 安全 — 授权生命周期与 `/panic`（对应评估 §B1 / §B2）

- 新模块 `atri_x/security/grants.py`：`Grant`（id / kind(session|tool) / tool /
  session_id / scope_key / actor_id / created_at / expires_at / max_uses / uses）
  与线程安全的进程内 `GrantRegistry`。旧的两个 dict（`auto_confirm_session` /
  `auto_confirm_tools`）退役，`chat_tools.is_session_auto_confirmed` 等旧函数保留为
  薄包装（走登记表，两参 / 三参签名不变）。
- `/confirmall` / `/confirmtool` 现在产生**带 TTL 的凭据**：默认 10 分钟
  （`DEFAULT_GRANT_TTL_SECONDS`，`0` 表示不过期），绑定产生时的
  `scope_key`（`<platform>:<scope_id>`）与 `actor_id`。共享线程群里另一名成员、
  或换个会话都不能复用同一条放行；命中即消耗一次使用次数。
- 失效钩子：群策略写回（`set_group_policy_for_scope`）撤销该作用域凭据；
  `/delete` 删除 Session 时撤销该 Session 凭据；`/panic` 全清。
- 新命令 `/grant list` / `/grant revoke <id>`（支持唯一前缀）与 `/panic`
  （`atri_x/agent/commands/approval.py`，`Info.min_role` 留空 = 仅超管）。
  `/panic` 不走模型：直接撤销全部凭据、`turns.stop_all()`、
  `requests.cancel_all()`、清各 Session 的排队消息；群里执行要求
  `/panic confirm` 二次确认（防误触）。
- 审计新增 `grant_created` / `grant_used` / `grant_revoked` / `panic` 四类事件。

### 记忆 — 会话可见性（对应评估 §A）

- 新模块 `atri_x/memory/notes.py`：`MemoryNote`（带 `scope_id` / `visibility` /
  `share_status`）+ 唯一判定入口 `note_visible_in()`。三档 visibility：
  `none` / `identity`（默认）/ `shared`。两条硬规则：**私聊来源永不进群**
  （`private:` / `c2c:` 来源只能出现在同一个私聊会话）、**合并取更严**
  （`stricter()`）。单轮注入上限 `PROMPT_NOTE_LIMIT = 12` 条 /
  `PROMPT_NOTE_MAX_UNITS = 500` 单位。
- `memories.toml` 新增可选的 `[[memory_notes]]` 数组表（老文件没有这一段时按空表读）；
  `Store` 新增 `read_notes` / `read_visible_notes` / `read_for_prompt` /
  `write_notes` / `append_note` / `delete_notes` / `update_notes`。
- **老数据按决策当 `identity` 档**：老 `core` / `normal` 没有来源信息，同平台任意
  会话仍可见；新写入的条目按来源会话过滤。`core` 维持"认得这个人"的全局可见性。
- `resident_memory_normal` 的 `append` / `write` / `delete` 改为**按会话隔离**：
  `write` 只覆盖**当前会话**的条目（私聊与其它群写下的条目不动），`delete` 只清空
  当前会话；`resident_memory_read` 返回"本会话可见"的记忆（core + 老 normal +
  可见条目）。
- `ResidentMemorySystemPromptSource` 改走 `read_for_prompt`（只提供 `read` 的存储
  替身退回旧行为）；**人物视图注入默认关闭**（`PEOPLE_VIEW_ENABLED = False`），
  先只注入本人条目把过滤跑通。
- 天使记忆（按 `(platform, scope_id)` 存）本来就带 `source_actor_id`，本批不改。

### 长期记忆 — 按会话隔离（对应评估 §A2 / 决策 4）

- `long_memory_*` 从「超管全局知识库」改为**按会话隔离**：新模块
  `atri_x/tool/builtin/long_memory_scope.py` 是唯一判定入口（作用域键、可见性、
  跨会话授权都只在这里判）。
- 记忆文件 front matter 新增可选 `source_scope` / `source_actor`（写成
  `<platform>:<scope_id>` 与 `platform:user_id`）；索引库 `memory_files` 加两列，
  老库在第一次打开时 `ALTER TABLE` 补列（已有行默认空串）。
  **无来源字段的旧记忆按"全局旧数据"处理，仍然到处可见**，升级不失忆。
- 检索 / 分类目录 / 更新 / 删除默认只看「当前会话 + 未归属旧数据」；
  `long_memory_write` 的 `save` 自动给新记忆打上当前会话与写入者。
- 超级管理员可跨会话：`long_memory_search` 传 `scope=all` 看全库、传具体作用域键
  定向看某个会话，结果里多一行「来源会话」；`update` / `delete` 跨会话时必须显式
  传目标记录的 `scope`。非超管即使能调到工具也会被拒绝（纵深防御）。
- 旧文件序列化结果不变：没有来源字段的记录不会写入 `source_scope` / `source_actor`，
  避免升级后整库重写。

### 引用 / 转发 — 慢取（对应评估 §C）

- 新模块 `atri_x/platform/qq_onebot/slowfetch.py`：`FetchCache`（成功 / 失败分开
  TTL、LRU 上限、诊断计数）+ 单飞 + 预算等待。同一个消息 id 的并发拉取合并成一次
  请求；预算内没等到就返回 `deferred`，**任务不取消**，结果由 done callback 写进
  缓存，下一次调用或 `fetch_message` 直接命中。
- `preprocess.fetch_referenced` / `resolve_forward_segments` 改走慢取：入站不再被
  `get_msg`（默认 15s 传输超时）和 `get_forward_msg`（5s × 最多 8 次）串行拖住。
  引用超时按"没有引用上下文"处理；转发超时留「拉取超时，已稍后重试」占位，
  拉取失败仍是「拉取失败」（失败结果按短 TTL 缓存，避免重试风暴）。
- 新增工具 `fetch_message`（`atri_x/tool/builtin/fetch_message.py`）：按消息 / 转发
  id 取回完整正文；先查缓存，未命中再真正拉一次。非 QQ 平台返回"不支持"而不报错。
- `[platform.qqonebot]` 新增 5 个键（全部有内置默认，老配置不写也能启动）：
  `slow_fetch_enabled`（默认 true）、`slow_fetch_budget_ms`（1500）、
  `slow_fetch_success_ttl_seconds`（300）、`slow_fetch_failure_ttl_seconds`（30）、
  `slow_fetch_max_entries`（512）。关掉 `slow_fetch_enabled` 恢复"一直等到平台
  返回"的旧行为（缓存与单飞仍生效）。桌面端暂未暴露这 5 个键（需要手改 app.toml，
  重启生效）。

### 费用归因（对应评估 §F）

- 新模块 `atri_x/agent/call_reason.py`：主聊天六种原因（`direct_message` /
  `ambient_participation` / `cron` / `external_event` / `memory_learning` / `retry`）
  与旁路 `side:<name>` 标签；`classify_call_reason()` 按上下文区分"用户直聊"与
  "后台任务"。
- `llm_usage` 审计事件新增 `reason`（原因）与 `price_known`（价格表里是否有该模型
  的价）；`/usage` 新增 `--by trigger` 按原因下钻。
- 新模块 `atri_x/llm/usage_sink.py`：旁路调用的统一用量出口（进程级 sink，没有
  订阅者时是 no-op）。`Agent.__init__` 订阅它并写成同一种 `llm_usage` 事件
  （`usage_source=side`），**5 条旁路终于进 `/usage` 了**：压缩摘要
  （`side:compaction`）、群分析报告摘要（`side:group_analysis`）、视觉理解
  （`side:vision`）、会话自动命名（`side:title`）、图片提示词改写
  （`side:image_rewrite`）。
- 未知价格不再等于 0 费用：`/usage` 末尾会提示"N 次调用的模型价格未知（费用未计入）
  / N 次旁路调用只统计 token"，两者都明确标注"不等于 0 费用"。

### 计算静默（对应评估 §E）

- `app.toml` 新增 `[quiet]` 段（默认关闭，老配置不写也能启动）：`enabled` /
  `window`（`HH:MM-HH:MM`，跨零点照写）/ `timezone`（空 = 本地）/
  `allow_cron` / `allow_external` / `allow_direct` / `allow_superadmin`。
- 新模块 `atri_x/agent/admission.py`：`admit(agent, ctx, reason)` 是"这次模型调用
  能不能发生"的唯一入口，原因标签沿用 `agent.call_reason`（与 `/usage --by trigger`
  同一套）。静默期规则：cron / 外部事件 / 记忆学习 / 主动参与默认 `skip`，
  用户直聊与超级管理员默认放行；**skip 不补跑**。
- 接线：`agent.cron.run_background`（cron 与 Atrinis 的唯一后台入口）在静默期直接
  返回 `RunResult(outcome="quiet_skipped")` 并写 `cron_skipped_quiet` /
  `external_skipped_quiet` 审计；`cron/execution.py` 与 `atrinis/llm.py` 识别该
  outcome，不当成"解析失败"去重试、不发空报告。
- 群聊唤醒判定改为"全局 `[quiet]` ∨ 群策略 `quiet_hours`"（任一生效即静默），
  超级管理员豁免仍可用 `allow_superadmin` 关掉；行为变化只发生在显式打开
  `[quiet].enabled` 之后。
- 「只压计算，不压数据」：静默期消息照常入库，只是不叫醒模型；群分析这类前台
  turn 内的动作不单独准入（turn 已在唤醒层被压掉）。
- 桌面端暂未提供 `[quiet]` 面板（手改 `app.toml`，重启生效）。

### 群聊参与状态机（对应评估 §D）

- `app.toml` 新增 `[participation]` 段（默认关闭）：`guard` / `cooldown_seconds`
  （默认 120）/ `no_reply_streak_max`（默认 3）。
- 新模块 `atri_x/agent/participation.py`：per-scope `ParticipationState`
  （最近唤醒、最近直接互动、未回应连击、主动参与冷却截止）+ 状态机函数。
  状态只在进程内存里，重启清空。
- 开启后只做**抑制**：机器人刚在某个群接过话的冷却期内，普通闲聊不再唤醒它；
  连续"被叫到没回应"达到上限同样进入冷却。点名 / 回复机器人 / 唤醒词仍然唤醒，
  且直接互动会清零连击。唤醒接线在 `agent.wakeup.message_wakeup`（默认关闭时
  no-op，行为与之前完全一致）。
- **不做**：`readThroughSeq` / 未读浏览 / `set_wake_condition` 工具；也不做
  "追问提示抑制"（需要提示词侧配合，本版未接）。

### 测试与门禁

- 新增 `tests/test_memory_notes.py`（29 个用例：可见性矩阵、存储会话隔离、注入过滤、
  工具读写、老文件兼容）；`tests/test_agent_risk.py` 新增 19 个确认边界用例
  （冒名确认、超管例外、群内藏参数、群内 `/confirmall` 拒绝、私聊 / CLI 放行）；
  `tests/test_qq_slowfetch.py` 31 个用例（缓存 TTL / LRU / 单飞 / 预算延后 / 失败
  短缓存 / 引用与转发复用 / `fetch_message` 工具与注册）；
  `tests/test_usage_attribution.py` 15 个用例（原因归一、旁路 sink、审计字段、
  `--by trigger` 分组、未知费用提示）；`tests/test_long_memory_scope.py` 31 个用例
  （归属字段、跨会话隔离、定向 / 全库检索、跨会话改删授权、老索引迁移、
  旧文件序列化不变）；`tests/test_security_grants.py` 20 个用例（TTL / 次数 /
  scope+actor 绑定 / 各类撤销 / 发放与使用审计 / `/grant` 与 `/panic`）；
  `tests/test_agent_admission.py` 18 个用例（`[quiet]` 配置、静默判定矩阵、
  cron 后台直跳与审计、跳过结果不被当成解析失败）；`tests/test_agent_participation.py`
  12 个用例（配置默认、冷却 / 连击 / 直接互动重置、快照与清空、唤醒接线）。
- `docs/commands.md` 的「高风险工具确认」一节补三条边界与「授权凭据」小节；
  `docs/concepts.md` 补「长期记忆按会话隔离」一段；`docs/configuration.md` 新增
  「静默时段 quiet」与「群聊参与状态机 participation」两节。
- 门禁：`check_syntax` / `check_imports` / `check_integration` / `check_coverage`
  / `check_except` / `check_secrets` / `gen_stats` 全绿；全量 6475 个用例通过
  （跳过 4，较 1.0.4 的 6300 新增 175 个）。

### 尚未落地（下一批）

- 桌面端还没有 `[quiet]` / `[participation]` 面板（手改 `app.toml`，重启生效）。
- 评估 §D 的"追问提示抑制"（需要提示词侧配合）与 `readThroughSeq` / 未读浏览类
  群聊协议扩展没有做。

---

## [1.0.4] - 2026-10-25 — 工具进度提示 + 本地表情图库

> 本版新增两个"拟人感"功能，都做成**配置驱动 + 桌面端可调**，默认不改变旧行为：
> ①**工具进度提示**——模型这一轮没先说话时，程序补一句进度（搜索 / 读取网页 / 其它工具
> 各有文案，默认角色化短句且不展示工具参数）；`[tool_preview]` 段控制文案风格；
> ②**本地表情图库**——模型只用 `[[emo:标签]]` 提出情绪意图，程序按策略决定"这次要不要
> 补图、补哪张"，不会被模型直接指定磁盘路径。版本号 `1.0.3` → `1.0.4`，重建
> Windows 便携版与安装包。

旧状态（1.0.3）：
- 工具进度预览文案硬编码在 `atri_x/toolrun/runner.py`：`正在调用 <工具>：<参数>`，
  参数原样展示（最多 160 rune），群聊里只有一个平台级开关
  （`[platform.qqonebot].show_tool_preview_in_group`），文案本身不可配置，
  代码里也没有"角色口吻"选项。
- 出站内容只有文本/工具产出的媒体，没有任何"按情绪补一张本地表情图"的能力；
  `llm.response.received` 等 Hook 点虽然能追加图片输出，但冷却、去重、场景抑制
  全靠插件作者自己写，产品内没有统一状态。

### 新增 — 工具调用进度提示（`[tool_preview]`）

- `atri_x/toolrun/preview.py`：`PreviewTexts`（纯数据）+ 默认文案。两种风格：
  `persona`（默认，短句、不展示参数）/ `system`（旧的"正在调用 工具名：参数"）。
  搜索类工具（`web_search` / `search_chat_history`）、读取网页（`web_extract`）与
  其它工具分别有独立文案；批量预览按工具**去重**（同一批两个 `web_search` 只显示一行）。
- `RunRequest` 新增 `preview_texts` 字段（默认 `None` = 旧文案），由
  `atri_x/agent/chat_tools.py` 从 `agent.tool_preview_texts` 注入；`runner` 仍不
  依赖 Agent/配置层。`should_send_preview` 增加 `enabled = false` 判断
  （关掉后程序不再主动发进度，模型自己说的中间正文不受影响）。
- `[tool_preview]` 段（严格解码，缺字段回落默认值）：`enabled` / `style` /
  `search_text` / `extract_text` / `default_text` / `system_template` /
  `failure_template`。默认值与 1.0.3 的可见行为对齐（除文案改为角色化短句）。
- 桌面端：「工具权限」页新增**第 4 个子选项卡「工具进度预览」**：群聊投递开关
  （QQ OneBot；`[platform.qqofficial]` 段存在时同写一份）+ 文案风格 + 各文案输入；
  保存后调用 `atri_x.config.load()` 回读校验，避免写出后台服务拒绝的键。
  「功能」页新增一行并支持双击跳转（键 `toolperm_preview`）。

### 新增 — 本地表情图库（`[reactions]` + `reactions/catalog.json`）

- 新包 `atri_x/reaction/`：
  - `marker.py`：`[[emo:无语]]` / `[[表情:无语]]` / 中文冒号 / 大小写都接受；
    标签上限 24 字且不跨行，避免误删正文里的普通方括号；解析结果 = 去标记正文 +
    情绪标签（去重），正文与落库内容都不含标记。
  - `catalog.py`：读取 `<配置目录>/reactions/catalog.json`，条目字段
    `id / emotion / path / name / description / contexts / avoid / weight / enabled`；
    路径必须落在图库目录内（拒绝 `..` / 绝对路径 / 软链逃逸），图片缺失的条目跳过；
    坏 JSON / 缺文件 → 空图库 + warnings，绝不抛到对话链路。
  - `policy.py`：`[reactions]` 的容错解析与夹紧（概率、冷却秒数、去重张数、
    长文阈值、抑制词）。
  - `service.py`：`ReactionService.decide()` 按「启用范围 → 场景抑制 → 文字回复冷却 →
    会话/群时间冷却 → 情绪匹配 → 最近图片去重（全被排除时退回 LRU）→ 权重随机 →
    概率」决策；状态（冷却、最近发过的图）在进程内保存，进程重启即重置。
- 接线：`atri_x/agent/reactions.py` 负责把配置变成 `ReactionSettings`、
  解析"平台 + 群/私聊 + 会话"作用域、把选中的图变成 `timing = after_assistant`
  的输出；`chat_turn` 在**任何可见/落库路径之前**剥标记（工具轮中间正文、assistant
  工具消息、最终回答、会话历史都干净），整轮最多追加一张图，放在空回复判定之后、
  正文发送之前入队 → 正文先发、表情后发；后台回合不发表情。
- 提示词：`[reactions].enabled = true` 时由 `ReactionSystemPromptSource` 把
  "可用标签 + 每轮最多一个 + 严肃场景不配图"注入系统提示词；关闭时不注入，
  但标记仍会被后端剥掉（正文永远干净）。
- 桌面端：「人设卡」页新增**第 4 个子选项卡「表情图库」**：发送策略（开关 / 群聊 /
  私聊 / 三种冷却 / 去重 / 概率 / 长文抑制 / 抑制词 / 可用情绪）+ 图库条目管理
  （Treeview 列表、添加图片并自动拷进 `assets/`、改情绪/名称/权重/启用、删除条目、
  保存 `catalog.json`）；保存策略同样回读校验。「功能」页新增一行并支持双击跳转
  （键 `character_reactions`）。
- 生效方式：`[reactions]` 在 `app.toml` → 重启后台服务；`catalog.json` 由服务按文件
  修改时间自动重载 → 保存即生效。`[reactions].enabled` 默认 **false**（新能力 opt-in）。

### 配置、资产与文档

- `atri_x/config/app_sections.py` 新增 `ToolPreviewConfig` / `ReactionsConfig`；
  `AppConfig` 新增 `tool_preview` / `reactions` 两个字段；`loader.load()` 把
  `gallery_dir` 按 app.toml 所在目录解析成绝对路径（与 `[soul].path` 同口径）。
- 默认资产：`app.toml` 增加 `[tool_preview]` / `[reactions]` 两段（带注释）；新增
  `reactions/catalog.json` 模板与 `reactions/`、`reactions/assets/` 两个默认目录。
  老配置没有这两段时按字段默认值生效（`[tool_preview]` 默认开、`[reactions]` 默认关）。
- 文档：`docs/configuration.md` 新增 `[tool_preview]` 与 `[reactions]` 两节（含
  `catalog.json` 结构）；`docs/windows-app.md` 补两个子选项卡的说明与生效时机；
  `devdocs/code-map.md` 补新模块（`toolrun/preview`、`reaction/`、`agent/reactions`、
  `gui/toolpreview`、`gui/reactionpage`）。

### 用例

- `tests/test_reaction.py`：标记解析（变体 / 重复 / 跨行 / 非标记方括号）、
  图库（缺文件 / 坏 JSON / 路径逃逸 / 重复 ID / 权重启用归一 / 场景过滤）、
  策略（默认值 / 夹紧 / 抑制词 / 配置往返）、决策（启用范围 / 三段冷却 /
  去重与 LRU 回退 / 概率 / 未知情绪 / 快照与重置）。
- `tests/test_agent_reactions.py`：配置→设置转换、作用域判定、回合收尾输出
  （`after_assistant` 图片 / 私聊 / 群关闭 / 未知作用域 / 无情绪计数 /
  抑制场景）、提示词注入只在启用时发生。
- `tests/test_agent_chat.py` 新增 `ReactionTurnTest`：真实回合路径上
  **标记不出现在正文/落库**、图片作为 deferred output 交付、功能关闭时只剥标记。
- `tests/test_toolrun.py` 新增/调整：批量去重、两种文案风格、`preview_texts`
  真正到达 runner。
- `tests/test_gui_tabs.py` 新增：工具进度预览子选项卡读写（含 qqofficial 只在段存在时写）、
  表情图库子选项卡策略往返与 `catalog.json` 增删改往返。

---

## [1.0.3] - 2026-10-23 — 内置内容安全（道德限制 + 敏感信息过滤）、常驻记忆配额 500、识图六项改造

> 本版把 1.0.2 时期的两套外挂能力收进产品内，并重做识图链路：
> ①**内容安全内置化**——「道德限制 + 敏感信息过滤」原生 Python 实现（词表拦截、凭据/PII 脱敏、
> 只存 hash 的审计日志、图片/文件出口策略），不再依赖外挂 Node hook 插件，也把
> 1.0.2 Hook 够不到的图片/文件出口纳入同一层策略；
> ②**常驻记忆配额** core / normal 各从 200 / 300 提到 **500**；
> ③**识图六项改造**——两段式描述、`detail = "original"`、SauceNAO 以图搜图兜底（默认关）、
> 3~5 秒短超时降级、群图外发隐私开关、失败原因可观测。
> 版本号 `1.0.2` → `1.0.3`，重建 Windows 便携版、安装包与全仓库源码归档。

旧状态（1.0.2）：
- 内容过滤只有外挂方案 `ATRI-X-safety-gate`（Node hook 插件 + 5 个 hook 点）：要单独安装、每条命中都要拉一次 `node.exe`（50~150 ms 冷启动），而且 `image_generate` / `send_file` 的 `out.outputs` 不经过 Hook，**图片/文件发出去之前拦不住**。
- 常驻记忆 `core_max_units = 200`、`normal_max_units = 300`；单条 normal 上限 80 units、最多 20 条。
- 识图只有两条：`view_image` 把图片段原样交给主模型（没有任何 `detail` 控制，上游可自行降采样）；`[vision]` 只在主模型**明确报错**“不支持图片”时才兜底。`image_to_prompt` 与聊天兜底共用 `atri_x/vision` 缓存，但提示词模板只有“绘图提示词/通用分析/聊天描述”三种，没有任何“只描述可观察属性、不下结论”的两段式模板，也没有按图搜图、群图外发开关；失败只表现为“降级回文本引用”，看不到是下载失败、格式不支持还是超限。

### 新增 — 内容安全（`atri_x/safety`，默认关闭）

- 新包 `atri_x/safety/`：`policy.py`（`SafetyPolicy` + `policy_from_section()` 鸭子类型读 `[safety]`）、`rules.py`（词表类别 + 敏感信息正则 + 命中豁免 + 幂等脱敏）、`guard.py`（`SafetyGuard.evaluate_text` / `filter_text` / `allow_media_output` / `should_strip_tool_images` / `stats` 与 `SafetyDecision`）、`audit.py`（JSONL 审计：sha256 + 脱敏预览 + 标签 + 阶段 + 平台/scope/actor + 时间，**不存原文**）、`wordlists/*.txt`（hate_speech / illegal_method / minor_sexual / privacy_violation / self_harm / sexual_explicit / terror_extremism / violence_gore 八类，随包分发、可整类启停）。
- 敏感信息默认动作分两档：`private_key` / `api_key` / `jwt` / `password` 默认 `redact`（直接替换）；`id_card` / `bank_card` / `phone` / `email` / `internal_ip` / `local_path` 默认 `flag`（只记审计，避免工程群正常内容被误脱敏），可用 `[safety.sensitive_actions]` 逐类改成 `redact`。
- `mode = off | audit | enforce`：`audit` 只写审计、不改任何输出；`enforce` 才拦截/替换。**`enabled = false`（默认）时所有接入点完全无行为变化**。
- 文本全链路接入：入站用户消息（`atri_x/agent/message.py`，命中即用 `block_reply` 回复并结束本轮、不进模型）、模型输出文本（`atri_x/agent/chat_llm.py`）、工具结果（`atri_x/agent/toolrun_adapter.py`）、发送边界（`atri_x/agent/output.py` 的 `prepare_assistant_output` 与 `send_outputs`，文本幂等重扫、图片/文件按 `block_image_output` / `block_file_output` 丢弃并补文本说明 + 审计）、生图提示词（`atri_x/tool/builtin/image_generate.py`，命中直接失败、不发 provider 请求）、文件出口（`atri_x/tool/builtin/send_file.py`）。
- `strip_tool_images = true` 时把工具结果里的图片段剥离成文字占位（对应 1.0.2 插件“模型看不到图”的能力），默认关。
- 装配：`atri_x/app/runtime_agent.py` 的 `_wire_safety_and_vision()` 构造 `SafetyGuard` 并挂 `agent.safety_guard`；guard 构造失败只记 WARN（fail-open，不让安全层缺失拖垮启动）。
- GUI：「工具权限 → 工具参数（全局）」的全局面板新增 8 个安全开关（启用/模式/审计路径/拦截回复/扫描上限/剥离工具图片/禁出站图片/禁出站文件）；「功能」页新增内容安全一行。

### 新增 — 识图六项改造

1. **两段式描述**：`atri_x/vision/prompt.py` 新增“角色/画面属性描述”模板（新增版本常量使旧缓存 miss），要求输出紧凑 JSON：发色 / 瞳色 / 发型 / 服装配色 / 滚边纹样 / 徽章标志 / 配件 / 可见文字 / 画风倾向 / 是否疑似 AI 生成 / 构图背景 / 人物数量 / `identity_confidence` / 自然语言描述。提示词硬约束“只描述可观察特征、不猜作品与角色身份、不补充画面不存在的细节”，`atri_x/vision/analysis.py` 扩展解析与限幅。`[vision] trigger = "always"` 时由 `atri_x/agent/chat_turn.py` 在首轮 `call_llm` 之前先描述（`atri_x/agent/vision.py` 的前置函数），主模型只拿描述 + 文字回答；默认 `on_error` 保持 1.0.2 行为不变。
2. **`detail = "original"`**：`MessageSegment` 新增 Python 侧字段 `detail`（空串不落盘，保持与 Go 落库键兼容）；`atri_x/llm/openai/wire.py` 的 `image_url` 与 `responses.py` 的 `input_image` 在非空时原样带给上游；`view_image` 的图片段与视觉服务的请求固定 `original`。`[image_to_prompt].detail` / `[vision].detail` 可改，但文档明确不要用 `low`（会先缩到 512×512，认人/认字细节在上传前就没了）。
3. **SauceNAO 以图搜图兜底**（`atri_x/vision/identity.py`，默认关闭）：只有 `identity_enabled = true`、有 key（`identity_api_key_env` 指的环境变量或配置目录 `.env`）、当日未超 `identity_daily_limit`、用户本轮文本明确问身份（这是谁/什么角色/出自哪/who is this/which character…）、且第一段 `identity_confidence = low` 才发一次请求；结果以「[以图搜图参考] 标题/作者/相似度/链接（外部服务，仅供参考）」追加进描述，低于 `identity_min_similarity` 视为没找到。每日计数按 UTC 日切，可选用 `identity_state_path` 持久化；key 绝不进日志/审计/异常文本。
4. **短超时降级**：`[vision].probe_timeout_seconds`（默认 4 秒，范围 1~30）包住前置描述；超时/失败把图片段替换成「图没取到（原因：超时/下载失败/格式不支持/超限）」，**绝不编造**画面内容。
5. **隐私开关**：`[vision].allow_external_images`（总开关）与 `allow_group_images`（群聊开关）。false 时服务层在发上游前直接拒绝（不打开连接），聊天前置路径替换为「[图片未外发：外部识图已关闭]」，工具路径返回可读错误；`[image_to_prompt].allow_external_images` 同理。
6. **失败可观测**：`atri_x/vision/failures.py` + `Service.stats()` 的 `failures` 计数，至少区分 `download_failed` / `unsupported_format` / `too_large` / `timeout` / `cancelled` / `provider_error` / `privacy_blocked` / `empty_result` / `parse_failed` / `identity_disabled` / `identity_quota` / `identity_error`；下载失败在 `atri_x/agent/vision.py` 分类记录。

### 变更 — 常驻记忆配额 200 / 300 → 500 / 500

- `atri_x/config/app_sections.py` 的 `ResidentMemoryConfig` 默认值与 `atri_x/config/app.py` 的补默认逻辑改成 `core_max_units = 500`、`normal_max_units = 500`（两段各自独立计算）；`atri_x/memory/resident/store.py` 的 `DEFAULT_CORE_MAX_UNITS` / `DEFAULT_NORMAL_MAX_UNITS` 同步。
- `atri_x/config/assets_data/app.toml` 模板、GUI 参数默认值、`docs/configuration.md` 同步；已有安装的 `app.toml` 若没显式写这两个键，升级后自动按 500 生效（写死 200/300 的旧配置保持用户显式值）。

### 配置与文档

- `[safety]` 新段进 `app.toml` 严格解码（未知键仍报错）；`[vision]` 新增 `detail` / `trigger` / `probe_timeout_seconds` / `allow_external_images` / `allow_group_images` / `identity_*` 八个键；`[image_to_prompt]` 新增 `detail` / `allow_external_images`。
- 新增 [`docs/safety.md`](docs/safety.md)（词表与敏感信息类别、audit 灰度、误杀调优、审计格式、出口策略、**桌面端在哪配**）与 [`docs/vision.md`](docs/vision.md)（两段式、detail、隐私开关、SauceNaO 配置、失败原因）；`docs/configuration.md` 补 `[safety]` 段与识图新键；`docs/README.md` / `README.md` 补入口；`devdocs/code-map.md` 新增「内容安全（safety）」小节并更新视图片；`AGENTS.md` locator 表补 `locator:safety` / `locator:vision`。

### 用例

- 内容安全：`tests/test_safety_core.py`（策略装载 / 词表命中 / 敏感信息脱敏与幂等 /
  审计不存原文 / `audit` 不改输出 / `enforce` 拦截与替换 / `allowlist` / `mode=off` 完全无变化 /
  `block_*_output` 出口策略 / 审计写失败不抛）与 `tests/test_safety_integration.py`
  （入站、模型输出、工具结果、发送边界、生图提示词、`send_file` 六个接入点，全部用 fake 宿主，
  含「拦截后 provider 一次都没被调用」）。
- 识图：`tests/test_vision.py` / `tests/test_vision_threading.py` 保持原有覆盖；新增
  `tests/test_vision_attributes.py`（属性模板解析与限幅）、`tests/test_vision_identity.py`
  （SauceNAO 请求构造 / 响应解析 / 每日配额 / 失败分类，全程离线 fake）、
  `tests/test_vision_privacy.py`（总开关与群聊开关、Service 前置闸门不建连、工具可读错误）、
  `tests/test_vision_prechat.py`（默认 `on_error` 不动消息、两段式触发、超时/下载失败降级、
  `identity_confidence=low` 才查 SauceNAO）。
- 配置与 GUI：`tests/test_config.py` 断言常驻记忆默认 500/500 与 `[safety]` 严格解码；
  `tests/test_gui_toolsettings.py` / `tests/test_gui_tabs.py` 覆盖新面板字段与「无改动不写盘」；
  `tests/test_resident_memory.py` 同步新的默认上限。
- 全量 unittest：`Ran 6200 tests … OK (skipped=4)`（472.110 s），日志在
  `证据/1.0.3/unittest-1.0.3.log`。

### 热修（同版本 1.0.3 · 重建安装包 / 便携版 / 源码归档）

1.0.3 首发后反馈：内容安全**只能在全局面板改**，chat / work 模式页没有入口，`[safety]` 也不能
按会话模式分别配置。本次按"热修"处理，**版本号保持 `1.0.3`**，四件产物全部重打并替换
GitHub Release `v1.0.3` 资产：

- **配置**：`[safety]` 新增两个覆盖子段 `[safety.chat]` / `[safety.work]`
  （`atri_x/config/app_sections.py` 的 `SafetyOverrideConfig`，字段 `None` = 继承全局）。
  可覆盖 `enabled` / `mode` / `categories` / `sensitive` / `sensitive_actions` / `allowlist` /
  `block_reply` / `strip_tool_images` / `block_image_output` / `block_file_output` /
  `max_scan_chars`；`audit_path` / `audit_preview_chars` 不覆盖（审计文件全局唯一）。
  `SafetyConfig.normalized()` 一并归一化覆盖段；`app.toml` 模板补注释示例。
- **运行时**：`atri_x/safety/policy.py` 新增 `merge_policy()` / `policies_from_section()`；
  `SafetyGuard` 内部按会话模式维护 `_variants`，所有判定方法新增 `mode` 参数与
  `mode_for()` / `active_for()` / `enforcing_for()` / `block_reply_for()` /
  `should_strip_tool_images(mode)` / `for_mode()`（返回 `ModeBoundGuard`）；
  新增 `mode_from_host(agent, ctx)`（当前 `session.mode` → 群默认模式 → 服务默认模式 → `work`）。
  集成点（入站、模型输出、工具结果、发送边界、生图提示词、文件出口）全部按会话模式判定；
  工具 ctx 里挂 `guard.for_mode(mode)`，所以 `image_generate` / `send_file` 不用自己推导模式。
  全局 `enabled = false` 时，覆盖段单独打开也会装配 guard（`_safety_guard_needed()`）。
- **GUI**：`chat` / `work` 两个子选项卡里各内置一份「内容安全：本模式覆盖」面板；
  「工具权限 → 工具参数（全局）」新增「内容安全：chat 模式覆盖」「内容安全：work 模式覆盖」两个面板
  （`toolsettings_general.py` / `toolsettings.py` / `toolparams.py` / `tabs/toolperm.py`）；
  「功能」页「内容安全」行的说明补 chat/work 覆盖摘要，双击直达「工具参数（全局）」并展开全局面板
  （`_TAB_KEYS["toolperm_params"]` + `ToolPermTab.show_global_params()`）。
- **文档**：`docs/safety.md` 新增「桌面端（GUI）设置」「按会话模式覆盖（chat / work）」两节；
  `docs/configuration.md` 补覆盖段说明；`README.md`「安全与可观测」与 1.0.3 说明同步；
  `devdocs/code-map.md` safety 小节补按模式覆盖的代码位置。
- **用例**：`tests/test_safety_core.py` 新增 `TestModeOverrides`（策略合并 / 覆盖开启 /
  分模式出口 / `ModeBoundGuard` / `mode_from_host` 回落链 / 配置解码与往返幂等）；
  `tests/test_safety_integration.py` 新增 `TestModeOverridesIntegration`（按会话模式的
  入站拦截、模型输出脱敏、工具结果、工具 ctx 绑定、只在覆盖段开启时装配 guard）并更新
  工具 ctx 断言（挂的是按模式绑定的视图）。
- **发行**：重打 onedir → 便携版 zip → Inno 安装包 + 全仓库源码归档；四件产物 SHA-256 全部变化，
  `SHA256SUMS.txt` / 发行说明 / 自检报告 / 证据同步重建；GitHub Release `v1.0.3` 的 10 个资产
  按 digest 比对后删除旧文件并重传。

### 发行

- 版本号 `1.0.2` → `1.0.3`（`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION` 与 README / 文档同步）。
- 从工作树重建 Windows x64 三件套（onedir + 便携版 zip + Inno 安装包），每个产物都跑产物级自检：`--version` = `atri-x 1.0.3`、`config check`、`-m` 转发、sidecar 状态、GUI selftest、便携版解压自检、安装包静默安装/卸载自检。
- 全仓库源码归档 `ATRI-X-1.0.3-repo.zip` / `.tar.gz` 与四件产物的尺寸 / SHA-256 见发行库的 `SHA256SUMS.txt` / `发行说明.md` / `自检报告.md` / `证据/1.0.3/`。
- 源码与产物脱敏复查（源码树 / 内置 DSH 内核 / 内置协议端 / 三份产物）0 命中。
- **已推送 GitHub**（`https://github.com/fujiwarashigure/ATRI-X2`）：`main` 分支、tag `v1.0.3`（热修后指向最终提交，`git push --force` 更新该 tag）；GitHub Release `v1.0.3` 附带安装包 / 便携版 / 源码归档 / `RELEASE-NOTES.md` / `SELF-CHECK-REPORT.md` / `THIRD-PARTY-NOTICES.md` / `SHA256SUMS.txt` / `build-info.zip` / `EVIDENCE.zip`（热修后按 digest 比对删除旧资产再重传）。1.0.2 补丁版同批推送（tag `v1.0.2` = `00a6653`，该版本无独立 Release）。

---

## [1.0.2] - 2026-10-23 — 「模型」页新增推理思考强度，修复 GUI 设置未落实到配置文件

> 本版是 **1.0.2 补丁版**：给桌面端「模型」页补上用户一直缺的**推理思考强度
> （`reasoning_effort`）**设置——Provider 表单可选 `none` / `minimal` / `low` /
> `medium` / `high`，保存进 `services.toml` 并真正写进 LLM 请求 JSON（模型级可覆盖）；
> 同时修掉一类「在设置里改了、却没落实到文件，导致配置不生效」的缺陷：模型分配清空
> 命名 / 压缩槽位现在会真的从 `state.toml` 删掉小节，工作 / 聊天槽位与 provider/model
> 半填会明确报错且整份不写，Provider 与模型分配保存后都会**从磁盘回读逐字段校验**。
> 版本号 `1.0.1` → `1.0.2`，重建 Windows 便携版、安装包与源码归档。

旧状态（1.0.1，桌面端真机）：
- 「模型」页的 Provider 表单只有 Base URL / 密钥环境变量 / API Key / 模型列表 /
  API 协议 / 请求头。想调推理强度只能手写 `services.toml` 的
  `extra_payload = { reasoning_effort = "high" }`，界面里没有任何入口，写错也没有校验。
- **模型分配的清空不落盘**：`save_model_selection` 对「provider 和 model 都为空」的
  槽位直接 `continue`，于是界面把命名 / 压缩槽位清空后，`state.toml` 里旧的
  `[naming_model]` / `[compact_model]` 原封不动；重启后台服务后旧模型又生效，用户看到的
  是「设置里改了但没生效」。
- **半填会写出错配**：provider 留空、model 有值时，旧实现只把非空的 `model` 写进文件，
  旧的 `provider` 键保留，形成「旧 provider + 新 model」；provider 有值、model 为空时
  界面报错但提示只覆盖这一种组合。
- 保存成功后只凭"写盘没抛异常"就提示成功，没有回读校验：一旦写入路径、键名或归一化
  与界面显示不一致，用户拿不到任何信号。

### 修复与新增

- `atri_x/config/services.py`：新增 `REASONING_EFFORT_VALUES`（`none` / `minimal` /
  `low` / `medium` / `high`）与 `normalize_reasoning_effort()`（大小写不敏感、空串 =
  未配置）；`ProviderConfig` / `ModelConfig` 各加一个 `reasoning_effort` 字段，
  `ProviderConfig.reasoning_effort_for(model)` 给出「模型级覆盖 Provider 级」的生效值。
- `atri_x/config/app.py` + `atri_x/config/loader.py`：启动期新增
  `validate_provider_reasoning_effort()`，Provider 级与 `model_configs` 级都校验；
  写错档位直接报错（消息里带 `[providers.<name>]` / `model_configs` 路径），不再等
  第一次请求才换一个上游 400。
- `atri_x/app/models.py`：`provider_extra_payload()` / `model_extra_payloads()` 把
  `reasoning_effort` 折进适配器已有的 extra payload（专用字段优先于同名
  `extra_payload` 键），chat 与 responses 两种协议都会把它放进请求 JSON；未配置时不发
  该字段。Hook 的 `llm.extra_body` 仍可做一次性覆盖。
- `atri_x/gui/configmodel.py`：
  - `ProviderView` 增加 `reasoning_effort`，`load_providers` / `save_provider` 读写
    `[providers.<name>].reasoning_effort`（清空 = 删键）；保存前用同一个白名单校验。
  - `save_model_selection()` 改成**先全量校验、后落盘**：provider/model 半填直接拒绝；
    工作 / 聊天槽位禁止清空（缺段会让后台服务启动失败）；命名 / 压缩槽位清空 =
    `remove_section()` 删掉 `[naming_model]` / `[compact_model]` 小节，真正落到文件。
  - 两个保存入口都新增**回读校验**：写盘后用同一套读函数回读并逐字段比对，不一致时抛
    「写入后回读与界面不一致」并列出字段，杜绝"界面改了、文件没变"。
- `atri_x/gui/tabs/model.py`：Provider 表单在「API 协议」下面新增「推理思考强度」下拉
  （`（不设置）` / `none（关闭思考）` / `minimal` / `low` / `medium` / `high`），
  载入时按配置值还原、保存后回读；模型分配页不再自己重复一套校验，统一走
  `configmodel` 的单一口径。保存成功的提示改为「已保存并回读校验」。
- `atri_x/config/assets_data/services.toml`：模板注释补上 Provider 级与模型级
  `reasoning_effort` 示例。

### 用例

- `tests/test_config.py`：`reasoning_effort` 解析（大小写 / 空串 / 非法值）、模型级覆盖
  Provider 级、Provider 级与模型级非法值都在加载期报错。
- `tests/test_app_llm_adapters.py`：Provider 级强度真的出现在 `build_body()` 的请求 JSON；
  模型级覆盖 Provider 级；未配置不发；Hook 的 `extra_body` 仍能一次性覆盖；专用字段
  压过同名 `extra_payload`。
- `tests/test_gui_configedit.py`：Provider 的 `reasoning_effort` 保存 / 归一化 / 清空删键；
  非法值在写盘前被拒；清空命名槽位会删小节；清空工作 / 聊天槽位与半填都被拒绝且文件
  一字不改；保存后回读不一致会报错。
- `tests/test_gui_tabs.py`：模型页下拉选中 `high` → `services.toml` 落盘 → 重新载入还原；
  选「（不设置）」删键；命名槽位清空后 `state.toml` 无该小节；工作槽位清空给出错误且
  文件保持原样。

### 发行

- 版本号 `1.0.1` → `1.0.2`（`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION` 与
  README / 文档同步）。
- 从工作树重建 Windows x64 三件套（onedir + 便携版 zip + Inno 安装包），每个产物都跑
  产物级自检：`--version` = `atri-x 1.0.2`、`config check`、`-m` 转发、sidecar 状态、
  GUI selftest、便携版解压自检、安装包静默安装/卸载自检。
- 全仓库源码归档 `ATRI-X-1.0.2-repo.zip` / `.tar.gz` 与四个产物的尺寸 / SHA-256 见发行
  库的 `SHA256SUMS.txt` / `发行说明.md` / `自检报告.md` / `证据/1.0.2/`。
- 源码与产物脱敏复扫（源码树 / 内置 DSH 内核 / 内置协议端 / 三份产物）0 命中；本轮
  改动**未提交 Git、未推送**。

---

## [1.0.1] - 2026-10-22 — 修复入站图片一律「媒体不可用」、补齐品牌图标，重建便携版与安装包

> 本版是 **1.0.1 补丁版**：修掉 1.0.0 真机上「群里发图后模型只看到
> `[媒体不可用；{文件名}.jpg]`」的入站媒体故障，补上从 exe 资源到 Tk 窗口、托盘、
> 安装向导的一整套品牌图标；并给「工具权限 → 工具参数（全局）」补上跨字段校验，
> 避免「启用反推」开着却填不出 provider / model 时后台服务卡在启动失败。
> 版本号 `1.0.0` → `1.0.1`，重建 Windows 便携版、安装包与源码归档。

旧状态（1.0.0 便携版真机，QQ 群）：

- 群里发图后再引用它问「提取画师串与 prompt」，模型输入里是
  `[媒体不可用；{文件名}.jpg]`；`media` / `media_history` 表里没有这条图。
  但同一轮里模型改用 `get_media(message_id=…)` 又能把**同一张图**下载入库——
  「入站时失败、工具里成功」。
- 桌面端「工具权限 → 工具参数（全局）」里把「启用反推」拨到「开」但没填
  provider / model 时，界面照常写盘；工具本身**不会注册**（群里看不到
  `image_to_prompt`），而**下一次重启后台服务会直接起不来**：
  `atri-x: [image_to_prompt] is enabled but incomplete: provider='' model=''`。

根因（第一条）：

- `atri_x/app/runtime.py` 只要装配媒体中心就总会挂一个磁盘守卫
  （`[storage] disk_*` 那套）。`atri_x/ops/diskguard.py` 的 `Guard.check` 无条件调用
  `ctx.raise_if_cancelled()`；但**入站平台消息的 ctx 不是 `CancelToken`**：
  `app/platforms.py` 用 `_RunFlag` 跑平台循环，`with_message_context` / `with_actor`
  经 `delivery.ctx_with_value` 把它落成"只承载值的普通 `dict`"，没有取消语义。
  于是磁盘守卫在这一步抛 `AttributeError: 'dict' object has no attribute
  'raise_if_cancelled'`，又被 `materialize_platform_segment` 的"失败降级"吞成
  `[媒体不可用；<名字>]`——**每一张入站图片/文件**都进不了媒体库，且日志里一个字都没有。
  工具侧（`get_media` / `view_image`）在回合 ctx（`request.Manager` 造的 `CancelToken`）
  里跑，所以同一个 URL 又能下载成功，形成上面那种"自相矛盾"的现象。

根因（第二条）：

- 「工具参数（全局）」页只做单字段类型 / 范围校验，没有跨字段校验。`[image_to_prompt]`
  的 `enabled` 与 `provider` / `model` 本来就是同一组参数，而 `save_group` 逐键写盘，
  于是能保存出「开但空」的非法组合；后台服务启动时工具装配把它当配置错误直接退出。

### 修复

- `atri_x/ops/diskguard.py`：新增并导出 `raise_if_cancelled(ctx)`，按 Go `ctx.Err()` 的
  语义**鸭子类型**判定——有 `raise_if_cancelled` 就用它，其次是 `is_cancelled` /
  `cancelled` / `error` / `err`；普通值包视为"无取消语义"。`Guard.check` 改用它。
- `atri_x/agent/inbound_media.py`：`materialize_platform_segment` 的失败降级**必须留一条
  WARN**（段类型、段名字、平台消息 ID；不回显来源 URL，避免把带凭据的地址写进日志）。
  之前的静默吞异常就是这次故障长期不可见的原因。
- `atri_x/gui/toolsettings_base.py` / `toolsettings.py`：面板支持跨字段校验
  （`SettingGroup.validate`，入参是"文件现有值 + 本次改动"的合成结果，失败一个字节都不写）。
- `atri_x/gui/toolsettings_media.py`：「图片反推提示词」面板新增校验——「启用反推」选
  「开」却没填 provider / model 时拒绝保存并说明后果（后台服务重启会失败）；
  `metadata_store` 的标签补上配置键名，说明里补上"平台转存过的图、截图、二次编辑本来
  就没有参数，开了也救不回来；只对开启之后新入库的图有效"。

### 新增 — 品牌图标（exe / 窗口 / 托盘 / 安装包）与安装向导宣传图

旧状态：ATRI-X 从桌面快捷方式到右下角托盘**全都没有自己的图标**——`build.py` 不给
`--icon`，`atri-x.spec` 拿到 `icon=None` 后 PyInstaller 嵌的是**它自带的默认图标**，于是
依赖 exe 图标的快捷方式、卸载器、应用列表全继承了那张默认图；Tk 主窗口从没调过
`iconbitmap` / `iconphoto`，标题栏与任务栏按钮显示的是 **Tk 的羽毛图标**；托盘
`TrayIcon` 没传 `icon_path`，退回 `IDI_APPLICATION` 通用图标；安装包 `setup.exe` 用 Inno
默认图标，向导左侧也没有宣传图（而且 Inno 6/7 默认跳过欢迎页，左侧几乎全空）。

- **一份 `atri_x/gui/brand/app.ico` 贯穿四处**：exe 资源图标、Tk 窗口图标、托盘图标、
  安装包图标。10 档尺寸（16/20/24/32/40/48/64/96/128/256），≤32 用脸部特写取景、
  ≥48 用整图；≤128 存 32 位 DIB、256 存 PNG。
- `packaging/make_assets.py`（新增，开发期工具）：从品牌源图生成上面这套图标，以及
  安装向导左侧宣传图（164:314，100%/200%/250% 三档）和右上角小图（58×58 / 116×116）。
- `atri_x/gui/icon.py`（新增）：运行期图标路径解析，源码运行与冻结后走同一套
  `__file__` 相对查找；素材缺失只是「没有图标」，不影响启动。
- `packaging/build.py`：`--icon` 默认指向品牌 ico（显式指错文件直接 exit 2），构建日志
  新增 `icon :` 一行；`atri-x.spec` 把 `atri_x/gui/brand` 打进 `datas`（窗口/托盘要在
  运行期读文件，不能只嵌进 exe 资源）。
- `packaging/atri-x.iss`：`SetupIconFile` + `WizardImageFile` + `WizardSmallImageFile`，
  并显式 `DisableWelcomePage=no`——否则左侧宣传图只在「完成」页出现。
- 修 `atri_x/gui/tray.py` 的托盘图标尺寸：`LoadImageW` 以前传 `0,0` 且不给
  `LR_DEFAULTSIZE`（尺寸未定义，高 DPI 下会糊），改成按 `SM_CXSMICON` / `SM_CYSMICON` 取，
  让 Windows 从多尺寸 ico 里挑正确档位。

### 用例

- `tests/test_ops.py`：值包 ctx（`dict` / 无 `raise_if_cancelled` 的对象）不再抛
  `AttributeError`；自报 `cancelled` 的 ctx 仍会中止。
- `tests/test_media_manager.py::TestImportWithDiskGuard`：带磁盘守卫时，`dict` /
  `_RunFlag` 形状的 ctx 能正常导入；`CancelToken` 取消后仍拒绝写入。
- `tests/test_agent_inbound_media.py`：导入失败降级时留下 WARN 且诊断字段不含 URL。
- `tests/test_gui_toolsettings.py`：「启用反推」缺 provider / model 时拒绝写盘；
  补齐后可以开；文件里已有 provider / model 时只改 `enabled` 也能通过（校验用合成值）。

### 验证

- 冻结产物里用真实 URL 复现（`atri-x.exe -m <probe>`）：修复前入站段变成
  `[媒体不可用；{文件名}.jpg]`，`guard.check` 抛
  `AttributeError: 'dict' object has no attribute 'raise_if_cancelled'`；修复后同一路径
  直接得到 `media:e43d8e46…`，不再降级。
- 用修复后的源码 + 真实配置（`[image_to_prompt]` 已回到"未启用"）跑
  `python -m atri_x service run --config …`：`atri-x startup completed`，而修复前同一条
  命令以 `[image_to_prompt] is enabled but incomplete` 退出 1。

### 发行

- 版本号 `1.0.0` → `1.0.1`（`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION` 与
  README 同步）。
- 从工作树用 `python packaging/build.py --release --with-pillow --with-kernel
  --with-qqproto "<NapCat Shell>"` 重建 Windows x64 三件套，每一件都跑产物级自检：
  - onedir：26,989 个文件 / 1,609,389,884 字节，内置 DSH 内核 0.2.0-rc.2 与脱敏后的协议端
    （2,514 文件 / 1,202.4 MB）；`--version` 输出 `atri-x 1.0.1`，`config check`、`-m` 转发、
    sidecar、GUI selftest 全过。
  - 便携版 `ATRI-X-1.0.1-portable-win64.zip`：682,744,041 字节（651.1 MB），SHA-256
    `af48e3e7…`；解压到短路径后 10 项自检全过（26,994 条目）。
  - 安装包 `ATRI-X-1.0.1-setup.exe`：460,063,237 字节（438.8 MB），SHA-256 `cc05bad3…`；
    静默安装 100.4 s / 26,992 个文件，`--version` = `atri-x 1.0.1`、`install.json` 与内置协议端
    断言通过，静默卸载 17.8 s 且目录清空。
- 全仓库源码归档 `ATRI-X-1.0.1-repo.zip` / `.tar.gz` 取自工作树（不含 `.git`、`dist`、`build`、
  `runtime`、日志、本地 `.env` 与 `__pycache__`）；四个产物的尺寸与 SHA-256 见本机发行库的
  `SHA256SUMS.txt` / `发行说明.md` / `自检报告.md` / `证据/1.0.1/`。
- 品牌图标与安装向导宣传图由 `packaging/make_assets.py` 从 `packaging/brand-src` 重新生成，
  设计未变，仅随 1.0.1 重出一次；`app.ico`（10 档尺寸）同时用于 exe 资源、Tk 窗口、托盘与
  安装包，向导宣传图三档（100% / 200% / 250%）随 `atri-x.iss` 一起进安装包。
- **脱敏**：`scripts/check_secrets.py --all` 0 命中；源码 / 内置 DSH 内核 / 协议端输入三级扫描
  0 命中；三份产物（便携 zip、安装包二进制、源码归档）逐项重扫 0 命中（第三方依赖里
  `@types/node` 的 `C:\Users\...` 文档示例与 AWS 文档示例 key 属已登记假值，不算泄露；平台
  转存过的图与真实群号不入包）。
- 本轮改动仍**未提交 Git、未推送**（本仓库工作副本没有 `.git`，源码归档直接取自工作树）。

---

## [1.0.0] - 2026-10-22 — 首个稳定版：工具权限页新增「工具参数（全局）」子选项卡，每个工具可展开编辑内部参数

> 本版是 **1.0.0 稳定版**：把 0.9.13 之后的两轮改动一起收进正式发行——0.9.14 让 chat 模式也能
> 调用工具、修正群作用域写法并支持 chat / work 分模式工具白名单；1.0.0 给「工具权限」页补上
> 工具内部参数入口，并重建 Windows 便携版、安装包与源码归档（版本号 `0.9.14` → `1.0.0`）。
>
> 旧状态（0.9.14 便携版）：桌面端「工具权限」页只有"工具能不能调用"这一层。用户想把生图
> 工具的 `metadata_store` 打开（让入库转码前先留档生成参数），在群里问助手，得到的答复是
> "这个开关不是工具调用，得你手动改 `services.toml`"——GUI 里没有任何入口能改工具的
> 内部参数，只能手搓 TOML 再重启。

### 变更

- **新增「工具权限 → 工具参数（全局）」子选项卡**（`atri_x/gui/toolperm.py` 的第三个
  子选项卡）：按工具分类列出参数面板，每组参数是一个可展开 / 收起的 ▸/▾ 面板，面板标题
  带影响哪些工具、配置状态（`已配置 N / M 项`）与「保存本组」；顶部有「保存全部改动 /
  全部展开 / 全部收起 / 重新载入」。目前覆盖 15 组面板：全局面板（`[tools]` 每轮工具
  轮数、`[security]` 风险阈值、`[ops]` 超时与并发）、`web_search` / `web_extract` 通道
  顺序、工作区与后台 shell 白名单（`[sandbox]` / `[security]`）、`send_file` 模式、媒体
  与视觉回退（`[media]` / `[vision]`）、常驻记忆 / 长期记忆 / 自学习 / 群聊统计 / 角色库
  的开关与上限、生图基础行为与提示词优化、画师串与 `metadata_store` 留档、图片反推提示词。
  没有可调参数的工具在页面底部统一列出（只受授权 / 风险等级控制）。
- **数据层与界面分层**：参数定义在 `atri_x/gui/toolsettings_general.py`（文本/联网/文件/
  记忆/群聊）与 `toolsettings_media.py`（图片/画师/视觉），字段类型与「界面值 → TOML 值」
  转换在 `toolsettings_base.py`，注册表汇总与读写（`load_all` / `save_group`）在
  `atri_x/gui/toolsettings.py`，控件在 `atri_x/gui/toolparams.py`。导入期自检面板 key
  唯一、字段 key 唯一、引用的工具必须真的在工具目录里、全局面板排第一。
- **只提交改动过的键**：面板保存时按"载入快照 vs 当前值"做差集，只把变化的键交给
  `configedit.patch_section`，所以保存一个参数不会重写同小节其它键、不会丢掉它们的行内
  注释，也不会把用户手写在文件里、界面不认识的取值顺手改掉（例如中转站自定义的
  `quality` 档位）。写盘仍带 `.gui.bak` 备份与写前 `tomllib` 校验。
- **三态字段语义**：`bool | None` 的配置（`superadmin_only`、`auto_character`、
  `forget_on_recall`、`normal_block_instruction_patterns` …）在界面上是「默认 / 开 / 关」
  三态，选「默认」= 删掉该键、回到配置层默认值；列表字段留空 = 删键（回落内置默认链），
  显式填 `none` = 空数组（例如后台 shell 全部拒绝）。
- **工具行新增「参数」按钮**（`atri_x/gui/toolmenu.py` + `toolmode.py`）：有可调参数的
  工具在 chat / work 菜单里多一个「参数」按钮，点了自动切到「工具参数」子选项卡并展开、
  滚到该工具的面板（`image_generate` 会同时展开"基础行为"与"提示词"两组）；
  `atri_x/gui/widgets.py` 的 `ScrollFrame` 增加了 `scroll_to_widget` 供定位。
- **自检与测试**：`atri_x/gui/selftest.py` 新增参数页断言（每组面板的字段控件数量、
  展开 / 收起、"其余工具"说明），打包产物的 `--selftest` 也会卡住；新增
  `tests/test_gui_toolsettings.py`（24 例：注册表自检 + 读写语义 + 非法输入不落盘 +
  注释保留），`tests/test_gui_tabs.py` 新增 `ToolParamsTabCase`（17 例：渲染 / 保存 /
  三态 / 校验 / 从工具行跳转 / 重新载入 / 参数页隐藏群列表）。
- 工具权限页的 `hint` 从"state.toml 热加载"改为空串：这一页现在同时管 `state.toml`
  （授权，15 秒热加载）与 `app.toml` / `services.toml`（参数，需重启），不再用一句话
  概括两种时机。

### 文档

- `docs/windows-app.md`：§4.2 的「工具权限」行与约定补上第三个子选项卡、写入文件与
  生效时机、只提交改动键的语义。
- `docs/configuration.md`「群级策略」：补一条工具内部参数的入口说明（GUI 面板 vs 直接
  编辑 `app.toml` / `services.toml`）。
- `devdocs/code-map.md` 与 `atri_x/gui/__init__.py` / `atri_x/gui/tabs/__init__.py` 的
  模块表：补 `toolmode` / `toolsettings*` / `toolparams`。

### 发行

- 版本号：`0.9.14` → `1.0.0`（`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION` 与
  README 同步）。本次是首个 1.x 稳定版：0.9.x 阶段的真机事故修复、工具授权与工具参数都已
  收进正式发行；后续 1.x 保持配置与命令向后兼容。
- 交付物（Windows x64）：安装包 `ATRI-X-1.0.0-setup.exe`（457,650,891 字节 / 436.4 MB，
  SHA256 `63c626c1…`）、便携版 `ATRI-X-1.0.0-portable-win64.zip`（682,875,603 字节 /
  651.2 MB，SHA256 `e21a9f5a…`）、全仓库源码归档 `ATRI-X-1.0.0-repo.zip` / `.tar.gz`
  （各 1,030 个文件）；逐项 SHA256、尺寸、自检记录与脱敏扫描见本机发行库
  （`SHA256SUMS.txt` / `发行说明.md` / `自检报告.md` / `证据/1.0.0/`）。
- 构建自检：onedir 27,029 个文件 / 1,535.8 MB，内置 DSH 内核与脱敏后的协议端（2,513 文件 /
  1,202.3 MB）；便携包解压 27,034 条目后 10 项自检全过；安装包静默安装 27,032 个文件、
  62.5 s，卸载 9.4 s；全量 `unittest` `Ran 5975 tests … OK (skipped=4)`；门禁全绿。
- 产物内容：内置 DSH 内核（Node + `@deepseek-ai/dsh`）与内置 QQ 协议端（NapCat 4.18.38 +
  便携 QQ 9.9.33-52230）；协议端的 `config` / `cache` / `logs` 与本机令牌不入包，目标机首次
  启动时生成。
- **脱敏**：`scripts/check_secrets.py --all` 0 命中；全仓个人标识扫描（用户名 / 本机路径 /
  真实群号 / 真实消息 ID / 邮箱 / 内网 IP / 密钥样式）逐项清零或替换为占位符；产物级扫描
  （便携包 + 安装包 + 源码归档）0 命中。
- 本轮改动仍**未提交 Git、未推送**（`git status` 里是工作树改动；源码归档直接取自工作树，
  包含未跟踪的新文件）。

---

## [0.9.14] - 2026-10-22 — chat 模式也支持工具调用；工具权限页修正群作用域写法并支持 chat / work 分模式白名单

> 旧状态（0.9.13 便携版真机，QQ 群 `123456789`）：
>
> 1. 用户在桌面端「工具权限」页给群激活了 `web_search` 等 28 个工具并保存，界面上显示"已激活"，
>    但群里怎么问都调用不了搜索；模型自己说"没有任何 function/tool 接口"，`tool_call_records`
>    一直是 0 条。原因是该群会话全部落在 `chat` 模式，而上游 Go 语义里 **chat 会话永不注入/
>    不执行工具**（`tool/provider.py`、`toolrun/`、`tool_directive*` 各自按 `mode == work` 判断）；
>    `state.toml` 的 `[session] default_mode` 又是 `chat`，所以连新会话也没有工具。
> 2. 同一份白名单其实还写在一个**永远匹配不上**的键上：界面把从「群友」页复制来的
>    `qqonebot · group:123456789`（间隔号 `U+00B7`）原样落盘，而运行时查表键是
>    `qqonebot:group:123456789`（半角冒号，`context_overflow_key`），
>    `group_policy_for_scope` 查不到 → 整份白名单静默失效（回落成"不限制"）。
> 3. 桌面端作用域只能手打，写错既不报错也不提示，是上面那个键写错的直接入口。

### 变更

- **chat 模式放开工具调用**（有意偏离上游 Go）：新增唯一判定
  `atri_x/storage/models.py:session_mode_supports_tools()`（`work` / `chat` 都返回真），
  原先散落在 `atri_x/tool/provider.py`（schema / 工具名清单）、`agent/tool_schemas.py`
  （`session_tools_enabled`）、`toolrun/toolrun.py`（`tool_infos` / `base_schemas`）、
  `toolrun/runner.py`（"chat 模式永远不跑工具"早退）、`agent/tool_directive.py` 与
  `agent/tool_directive_skill.py`（`@tool:` / `@skill:` 预载）里的 `mode == work` 判断全部改用它。
  现在两个模式都注入工具 schema、跑工具轮、允许预载；区别只剩 `mode_models.chat` /
  `mode_models.work` 的模型槽位与上下文/溢出策略。要按群禁用工具仍用
  `/tools allow none`（或桌面端「禁止全部」），与模式无关。
- **工具权限页的作用域归一化与校验**（`atri_x/gui/toolpolicy.py`）：保存前把 `·` / `・` /
  `•` / 全角 `：` 等"像冒号"的字符和空白统一转成半角 `:`，并按 `platform:scope_id`
  校验（空值、空段、单段都直接报错，不再静默写盘）。
- **旧写法键自动迁移**：`migrate_legacy_scope_keys()` 在保存/载入时把 `state.toml` 里
  `group_policy` 的非规范键小节搬到规范键（字段能带的带过去，工具白名单两个键留给本次保存
  覆盖），旧的 `qqonebot·group:...` 这类死键会被清掉。
- **作用域候选来自真实会话库**：`discover_group_scopes()` 只读 `atrix_sessions.db` /
  `atrix_chat_history.db`，把 `platform:group:id` 列进「工具权限」页的下拉候选与群列表
  （未配置的群标"未配置"），不用再手打；写法不规范的旧键在列表里标黄提示。
- **工具权限可按会话模式分开配置**（本仓扩展）：`GroupPolicyConfig` 增加
  `tool_allowlist_chat` / `tool_allowlist_work` + 对应 `_set` 标记（nil / 空列表二态），
  `mode_tool_allowlist(mode)` 负责"分模式 → 通用 → 不限制"的回退；运行时
  `tool_auth.group_tool_allowlist` 多收一个 `session`，按**发起调用的会话模式**取白名单，
  注入 schema（`tool_schemas`）、执行前授权（`runner` / `chat_tools._tool_hooks`）、
  `discover_tool` 过滤（`toolrun_adapter`）三处已全部贯通。桌面端「工具权限」页改成
  **chat / work 两个子选项卡**，各自有独立的三态（继承通用 / 仅白名单 / 禁止全部）、
  工具菜单、搜索与预设；顶部显示群内 `/tools allow` 写入的通用白名单作为兜底。
  群内命令不变，`/tools allow` 仍写通用兜底。

### 打包

| 产物 | 大小 / 时间 | 自检 |
| --- | --- | --- |
| `ATRI-X-0.9.14-portable-win64.zip` | 682,837,214 字节（651.2 MB，deflate 42%，打包 37.6 s） | 解压 14.9 s / 27,034 条目后 9 项全过（`--version` / `config check` / 数据目录落包内 / `qqproto` / `-m` 转发 / `sidecar` / GUI `--selftest` / `start-portable.bat` / `debug-console.bat`，另含 APPDATA 重定向断言） |
| `ATRI-X-0.9.14-portable-win64.zip` | SHA256 `dcc6b895f8764b0504e2c6236c10961c32cec2f2763d7a3e2bec91c2dafc6efa` | — |

- 打包输入：内置 DSH 内核 `dshVersion 0.2.0-rc.2`（277.6 MB / 23,812 文件，沿用 0.9.13 已核验的 `runtime/dsh-kernel`）；内置 QQ 协议端 NapCat 4.18.38 / QQ 9.9.33-52230（跳过 `config` / `cache` / `logs` 与 `*.log`，令牌由目标机首次启动生成）。
- **脱敏核验**：打包前 `scripts/check_secrets.py --all` 0 命中，真机群号统一替换为占位符；产物级扫描（onedir 文本文件 + zip 内 `assets_data` / 说明 / 构建元数据）0 命中，zip 内 NapCat `onebot11.json` / `webui.json` 条目为 0，默认 `state.toml` 的 `default_mode` 为 `work`。

### 文档

- `README.md` / `docs/concepts.md` / `docs/commands.md` / `docs/configuration.md` /
  `docs/getting-started.md` / `docs/windows-app.md` 同步"两个模式都支持工具"与新的作用域规则；
  `devdocs/upstream-changelog-cross-check.md` 把"chat 模式不再能传入工具"从**已跟**改成
  **有意相反**，记录本仓的产品决策与落点。

---

## [0.9.13] - 2026-10-22 — 正式版：并入两轮真机热修（思考模式工具链 HTTP 400 / 图片媒体读不到）

> 旧状态（0.9.12 便携版真机，QQ 群 `123456789`）：让 ATRI-X 抽图片里的画师串与 prompt 时，
> 工具确实被调用了，但整轮拿不到任何结果：
>
> 1. 工具轮之后的续轮请求被上游拒收：
>    `HTTP 400: Upstream request failed: [invalid_request_error] The reasoning_content in the
>    thinking mode must be passed back to the API.`；
> 2. `view_image` / `get_media` 始终回「图片获取失败 / 媒体获取失败」，入站引用图显示
>    `[媒体不可用；…]`，`media` / `media_history` 两张表一直为空。
>
> 0.9.12 曾在本机真机部署两轮未单独发版的热修（见下面 `[0.9.12-hotfix.1]` /
> `[0.9.12-hotfix.2]` 两节）；本版把它们并入正式版本，并连同 0.9.10–0.9.12 的开发线
> 一起发版，同时补上 0.9.10 之后未再重建的 Windows 安装包。

### 修复

- **思考模式工具链回传 `reasoning_content`**（对应 `[0.9.12-hotfix.2]`）：DeepSeek 思考模式要求
  携带 `tools` 的请求在后续每一轮把历史 assistant 消息的 `reasoning_content` 原样回传，
  否则部分上游路由直接 400。`atri_x/llm/llm.py` 的 `LLMMessage` / `ChatRequest` 增加对应字段；
  `atri_x/llm/openai/wire.py` 负责序列化（DeepSeek 系目标在 tool_calls 消息上即使思维链为空
  也写 `""`，其它厂商不写）；`atri_x/agent/chat_llm.py` 累积流式思维链，
  `chat_turn.py` / `chat_tools.py` / `toolrun/` 把它随工具调用记录与最终回答一起落库，
  `agent/prompt.py` 在历史重建时读回——同轮续轮与跨回合都满足协议。
- **图片媒体导入的 `Source` 类型错用**（对应 `[0.9.12-hotfix.2]`）：`atri_x/media/platform.py`
  的 `import_platform` 把解析器返回的 `delivery.Source`（带 `mime_type/path/data/media_id`）
  误当成 `media.types.Source`，读 `source.mime_type` 必抛 `AttributeError`，又被失败降级吞成
  `[媒体不可用]`，于是所有入站/历史媒体都进不了媒体库。现按 Go 的
  `delivery.Source` / `media.Source` 分工修正，`url` / `path` / `data` / `media_id` 四种来源分派恢复。
- **同步仓储调用签名**（对应 `[0.9.12-hotfix.1]`）：`view_image` / `get_media` /
  `inbound_media` 不再给同步的 `ChatHistoryRepository.get_by_platform_message` 多传 `ctx`。

### 变更

- 版本 `0.9.12` → `0.9.13`：`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION`、README 同步。
- 重新构建并交付三种产物：便携版 zip、安装版 `setup.exe`、完整仓库源码归档（zip / tar.gz）；
  安装版是 0.9.10 之后首次重建，便携版与安装版都内置 DSH 内核与内置 QQ 协议端（同 0.9.9 起的交付形态）。
- 仓库脱敏后推送 GitHub：`check_secrets.py --all` 零命中、无本地个人目录/令牌/`.env`/构建产物；
  README 的版本号、模块与测试计数、发行物清单同步更新（新增模块计入 699 个 `.py` / 1023 个受控文件）。

### 验证

- 真实聊天历史库副本 + 真实 QQ 图 URL 的修复前/后对照（消息 ID 与媒体 ID 已脱敏）：修复前
  `AttributeError: 'Source' object has no attribute 'mime_type'`；修复后两条历史图片消息
  都下载入库（约 1.7 MB）。
- 真实上游探针（`opencode` 中转 / `deepseek-v4.1-flash`，带 tools）：模型返回
  `reasoning_content`，续轮「不带 / 空串 / 原样」三种写法都符合协议要求。
- 全量 `python -m unittest discover -s tests -t .`：**Ran 5922 tests … OK (skipped=4)**；
  `check_syntax` / `check_imports` / `check_integration` / `check_except` / `check_secrets --all` 全绿。
- 冻结产物 PYZ 核验：新 exe 11 项全部通过，旧 exe 对照 9 项 FAIL；便携版解压自检
  与安装包静默安装/卸载自检通过（细节见发行证据）。

---

## [0.9.12-hotfix.2] - 2026-10-21 — 修复"思考模式工具链 HTTP 400"与"图片媒体仍读不到"

> 旧状态（0.9.12-hotfix.1 之后，同一台便携版真机 / QQ 群 `123456789`）：
>
> 1. 让 ATRI-X 抽图里的画师串与提示词时，工具已经真的跑起来了，但整轮最后只回一条
>    `请求失败：HTTP 400: Upstream request failed: [invalid_request_error] The
>    reasoning_content in the thinking mode must be passed back to the API.`；
>    群里看到的是"工具调了一堆、什么都没返回"。
> 2. 同一时间 `view_image` / `get_media` 仍回 `图片获取失败` /
>    `[媒体获取失败：来源不可用、超限或关联保存失败]`，引用消息里的图显示
>    `[媒体不可用；…]`，`media` / `media_history` 两张表始终为空——即 hotfix.1 只修掉了
>    第一个 `TypeError`，图片仍然进不了媒体库。

### 修复（思考模式续轮回传）

DeepSeek 思考模式的协议要求：**请求带 `tools` 时，历史 assistant 消息里的
`reasoning_content` 必须原样回传**；本轮工具调用之后的续轮尤其不能丢。旧实现只把流式
思维链拿去 CLI 展示，随后就丢弃，因此一旦上游路由执行硬校验就会 400，工具全跑完也拿不到
任何结果。

- `atri_x/llm/llm.py`：`LLMMessage` 增加 `reasoning_content`（Python 侧字段，不落用户可见
  文本）；`ChatRequest` 增加 `provider` 供请求体判断协议；`clone_messages` 一并复制。
- `atri_x/llm/openai/wire.py`：`openai_message` / `to_openai_messages` 回传
  `reasoning_content`。对 DeepSeek 系目标（provider 或 model 名含 `deepseek`），带
  `tool_calls` 的 assistant 消息即使思维链为空也写 `""`——思考模式上游按"字段必须存在"
  校验，缺字段同样 400；其它厂商不写该字段，避免拒收未知扩展。
- `atri_x/llm/openai/openai.py`：非流式响应的 `ChatResponse` 也解析 `reasoning_content`；
  `build_body` 按 `uses_reasoning_protocol(provider, model)` 决定是否写扩展字段。
- `atri_x/agent/chat_llm.py`：`LLMCallResult.reasoning_content`；流式增量无条件完整累积
  （不再受"是否给 CLI 展示"影响）。
- `atri_x/agent/chat_turn.py`：工具轮追加的 assistant 消息带 `reasoning_content`，并把
  本轮思维链交给工具批次。
- `atri_x/agent/chat_tools.py` / `atri_x/toolrun/runner.py` / `atri_x/agent/toolrun_adapter.py`
  / `atri_x/agent/tool_transcript.py`：`RunRequest.assistant_reasoning` → assistant 工具调用
  记录的 metadata（`reasoning_content`）落库；最终回答那一轮的思维链同样写进 assistant
  消息 metadata（`assistant_raw_text_metadata` 新增参数）。
- `atri_x/agent/prompt.py`：从会话历史重建请求时，把 metadata 里的 `reasoning_content`
  读回 `LLMMessage`，因此**跨回合**（不只是同轮续轮）也能按协议回传。

### 修复（媒体导入的 `Source` 类型错用）

- `atri_x/media/platform.py`：`import_platform` 把解析器结果误用成
  `atri_x.media.types.Source`（只有 `platform/url/file_id`），却按
  `delivery.Source`（带 `mime_type/path/data/media_id`）去读 `source.mime_type`，
  于是**每一张入站图/文件**都抛
  `AttributeError: 'Source' object has no attribute 'mime_type'`，又被
  `materialize_platform_segment` / `materialize_with_limits` 的"失败降级"吞成
  `[媒体不可用]`：媒体库永远为空，历史取图只能再走下载、又撞同一个异常。
  现在解析器结果用 `delivery.Source`，`Input.source` 仍用 `media.types.Source`，与
  Go 的 `delivery.Source` / `media.Source` 分工一致；`path` / `data` / `media_id` 分支也
  一并恢复。

### 新增 / 回归用例

- 新增 `tests/test_media_platform.py`（7 例）：URL 段没有 mime_type、解析器补 mime_type、
  path / data / media_id 四种来源分派、已有 media_id 短路、超限拒绝。
- `tests/test_openai_adapter.py`：assistant `reasoning_content` 原样进请求体；DeepSeek 系
  tool_calls 空思维链也写 `""`；非 DeepSeek 不写；`uses_reasoning_protocol` 判定。
- `tests/test_agent_chat.py`：工具轮续轮请求里 assistant 消息带 `reasoning_content`，且
  思维链被交给工具批次落库。
- `tests/test_agent_tools.py` / `tests/test_agent_prompt.py`：metadata 落库/读回与历史重建
  的 `reasoning_content` 往返。

### 验证

- 真实聊天历史库副本 + 真实 QQ 图 URL 的端到端复现（消息 ID 与媒体 ID 已脱敏）：修复前
  `get_history_media` 抛 `AttributeError: 'Source' object has no attribute 'mime_type'`；
  修复后两条历史图片消息都成功下载并入库（约 1.7 MB），`media_history` 正常写入。
- 真实上游探针（`opencode` 中转 / `deepseek-v4.1-flash`，带 tools）：第一轮返回
  `reasoning_content`（158 字符）；续轮带 / 不带 / 空串三种写法该中转都回 200，说明它是
  宽容路由——真机 400 来自会硬校验的上游路由，修复后三种情形都符合协议。
- 专项单测（openai adapter / agent chat / prompt / tools / toolrun / media 系列）全过；
  `check_syntax` / `check_imports` / `check_integration` / `check_except` 全绿；全量套件
  结果与真机冻结产物核验见证据目录。
- 便携版热修重建并替换二进制（Python 3.14.7 / PyInstaller 6.22.3，复用已部署的 DSH 内核
  与内置协议端），重启后台服务后 `/healthz` `ready=true`、`qqonebot connected`。
  版本号仍为 `0.9.12`（不 bump、不重出安装包）。

---

## [0.9.12-hotfix.1] - 2026-10-21 — 修复"群里发图后 `view_image` / `get_media` 读不到图"

> 旧状态（0.9.12 便携版真机，QQ 群 `123456789`）：群里发图让 ATRI-X 抽画师串与提示词时，
> `view_image` / `get_media` 全部失败，工具侧报
> `ChatRepository.get_by_platform_message() takes 4 positional arguments but 5 were given`；
> 模型只能答复"图片媒体拿不到、读不到像素数据"。

### 修复

- `atri_x/tool/builtin/view_image.py` / `atri_x/tool/builtin/get_media.py`：按平台消息 ID
  查历史的调用点不再给**同步**仓储多传 `ctx`。Python 的
  `ChatHistoryRepository.get_by_platform_message` 签名是
  `(platform, scope_id, platform_message_id)`（Go 的 `ctx` 只用于取消，移植时已去掉），
  多传一个参数会直接 `TypeError`，于是两个工具都在查历史这一步就挂了。
- `atri_x/agent/inbound_media.py`：`_history_lookup` 同样去掉 `ctx`，并删掉并不存在的
  `get_by_platform` 名字回退。此前 `TypeError` 被 `associate_inbound_history` 吞成一条
  WARN，入站图片永远关联不到历史消息位序（`media_history` 一直为空）。
- 回归用例：`tests/test_builtin_tools3.py` 的假仓储改成与协议**逐字一致**的 3 参签名
  ——假仓储多收一个 `ctx` 正是这个 bug 一直没被测试抓到的原因；另新增
  `RealHistoryRepositoryTest`（真实 SQLite 仓储 + `historygate.ChatRepository` 包装）与
  `tests/test_agent_inbound_media.py`（入站媒体 → 历史位序关联）。

### 验证

- 修复前同一组用例报 `takes 4 positional arguments but 5 were given`；修复后
  `python -m unittest tests.test_builtin_tools3 tests.test_agent_inbound_media` 全过。
- 全仓体检 `check_syntax` / `check_imports` / `check_integration` / `check_except` 全绿。
- 便携版热修重建（Python 3.14.7 / PyInstaller 6.22.3，复用已部署的 DSH 内核与内置协议端），
  冻结产物 PYZ 核验三个调用点均为 3 参；重启后 `/healthz` `ready=true`、`qqonebot connected`。

---

## [0.9.12] - 2026-10-21 — 修复"所有群工具被误判为未授权"、桌面端命令回执丢失；新增群工具授权机制

> 旧状态（0.9.11 便携版真机，QQ 群 `123456789`）：
>
> 1. 群里问"现在几点 / 查查最新显卡"时，`discover_tool` / `web_search` 全部被拒，
>    工具结果只有 `tool is not in this group's allowlist`，于是模型回答"联网搜索这个群
>    也没开权限"，而 `state.toml` 里**根本没有** `[group_policy]` 段——不是有人配了白名单，
>    是判定把"没配过"读成了"禁止全部"。
> 2. 桌面端切会话（GUI 发 `/resume <id>`）与命令回执报
>    `请求失败：cli target missing`；通知队列把原 ctx 换成裸 `CancelToken`，任务里丢了
>    message context / 远程客户端，CLI 发送者解析不到落点。
> 3. 群里看不到工具调用进度（`should_drop_group_tool_preview` 硬编码丢弃），
>    `/tools` 又是超管专用，普通群成员既看不到"有哪些工具"，也看不到"为什么用不了"，
>    桌面端没有任何工具授权入口。
> 4. 操作者实测还撞上 OneBot 断线窗口（14:45:57–14:1,432:25），期间群消息根本没有进来。

### 修复

- `atri_x/config/state.py`：`GroupPolicyConfig.normalize()` 保留 `tool_allowlist` 的
  **nil / 空列表二态**（`None` = 继承全局；显式列表含 `[]` = 本群有策略、空列表禁止全部）。
  旧实现把 `None` 归一成 `[]`，第二次 `tool_allowlist_restricted()` 就把"没配过群策略"
  判成"显式禁止全部工具"——**任何没配置群策略的群，所有工具调用都会被拒**（真机事故）。
  归一化现在幂等，配套回归用例钉住 nil / 空列表 / 具名列表三种语义。
- `atri_x/agent/notice_queue.py`：队列任务改用 `_run_ctx()`——把原 ctx 的值逐个挂到
  `CancelToken` 上再入队，取消语义与值传递都不丢。修复桌面端（CLI 远程连接）命令回执、
  `/resume`、钩子通知的 `cli target missing`（`请求失败` 落盘到 CLI 通道）。
- `/tools` 的列出与授权状态对普通用户开放：`Info.min_role = RoleUser`，
  `reload` / `uninstall` / `remove` 仍要求超级管理员（命令内再判一次）。

### 新增

- **群工具授权机制（群内）**：`/tools perm` 查看本群工具授权；
  `/tools allow <工具名,工具名|all|none>` 由群主 / 群管理员 / 超级管理员授权
  （与 `/grouppolicy tool-allow` 同一份策略，写入 `state.toml`
  `[group_policy."<平台:scope>"]`，15 秒内热加载）；白名单自动补 `discover_tool`
  （缺了它模型无法发现工具）。`/tools` 列表末尾附带本群授权状态与修改提示。
- **桌面端「工具权限」选项卡**（`atri_x/gui/tabs/toolperm.py`）：按群列出 / 新增作用域，
  三态（继承全局 / 仅白名单 / 禁止全部），工具勾选列表（静态目录 + 自定义工具名），
  「常用」「只读」预设与全选/全不选；下方"群聊里显示工具调用进度"开关。
  写入 `state.toml` 的群策略热加载，进度开关写 `[platform.qqonebot]
  .show_tool_preview_in_group`（需重启后台服务）。
- `[platform.qqonebot].show_tool_preview_in_group` / `[platform.qqofficial]` 同名字段：
  群聊是否投递工具调用进度预览，默认 `false`（与上游 Go 一致）。
  `should_drop_group_tool_preview` 增加 `show_in_group` 参数，两个平台适配器传入配置。
- `atri_x/agent/group_policy.py`：`group_tool_allowlist_text()` 一句话授权视图；
  `Deps.tool_allowlist` 回调由 `_build_deps` 接线给 `/tools`。

### 变更

- 版本 0.9.11 → 0.9.12：`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION`、README 同步。
- 文档：`docs/commands.md`（`/tools` 全量重写 + `tool-allow` 交叉引用）、
  `docs/configuration.md`（nil/空列表语义、`show_tool_preview_in_group`、群策略与 GUI 入口）、
  `docs/windows-app.md`（11 个选项卡 + 工具权限页）、`devdocs/code-map.md`、资产 `app.toml` 注释。

### 修复（发行复验时发现）

- `atri_x/platform/cli/remote_server.py`：远程 CLI 服务端每条 `input` 都新建消息上下文。
  `MessageContext` 是可变对象，入站处理会就地写回 `msg.segments` / `raw_text`
  （`agent.segments.with_inbound_segments`）；此前一条连接只建一份上下文，于是**同一条连接上的
  第二条消息会读到第一条的正文**——聊天表现为"第二条消息被吞掉"，命令表现为**同一条命令被执行两次**
  （回执也发两次）。Go 里 `MessageContext` 是值类型，入站改写只落在副本上，所以这是移植期的语义差异。
  回归用例：`tests/test_platform_cli_remote.py::test_each_input_frame_gets_fresh_message_context`
  （先发 `/status` 再发聊天，断言第二条的段与正文是它自己的）。

### 验证

- 门禁：`check_syntax` / `check_imports` / `check_integration` / `check_coverage` /
  `check_except` / `gen_stats --check` / `check_secrets --all` 全绿（数字见发行证据）。
- 单测：新增/改写 `toolperm` 桌面页 5 例、`/tools` 授权 8 例、
  `GroupPolicyConfig` 归一化幂等回归、通知队列 ctx 保留回归、群聊进度开关回归；
  相关模块（config / agent / commands / delivery / gui / toolrun）全过。
- 真机记录（0.9.11 便携版日志）：DSML 还原正常（`llm_tool_calls_in_text recovered=true`），
  随后两次 `permission_denied reason="tool is not in this group's allowlist"`
  （`discover_tool`、两次 `web_search`），`state.toml` 无 `[group_policy]` → 指向本节的
  归一化 bug；同一时段 `message_error error="cli target missing"` ×5 全部由桌面端
  `/resume` 触发（`session_resume` 审计同刻出现）。
- 冻结产物 e2e（0.9.12 便携版，假上游 + 真实 CLI WebSocket，群作用域 `group:123456`、
  `state.toml` 无 `[group_policy]`）：`/status` 回执 1 次（无 `cli target missing`）、
  随后聊天触发模型轮 → 文本工具调用被还原为 `discover_tool` 并真的执行 →
  最终回答落地，全程无 `allowlist` 拒绝、无协议标记外发。

### 变更（桌面端「工具权限」页改版）

- 旧状态：工具授权区是 4 列复选框矩阵，只有工具英文名——用户看不出某个工具是干什么的，
  也分不清"已放行 / 没放行"；窗口缩到最小高度时下方开关与按钮还会被挤出可视区。
- 新状态：改成**纵向滚动的工具菜单**，一个工具一行：
  「激活状态徽标（已激活 / 未激活 / 继承全局 / 禁止使用）+ 中文名 + 工具名 + 风险等级 +
  一句话功能解释 + 激活 / 停用按钮」；点按钮或点整行都能切换，非白名单模式自动置灰。
  新增搜索框、「只看已激活」、分组标题（带分组内已激活计数）、已激活总数与
  「有未保存改动」提示；群作用域列表与新建输入移到左栏，菜单拿到整页剩余高度。
- 拆分：工具展示信息（49 个内置工具的中文名 / 解释 / 分组 / 风险）进
  `atri_x/gui/toolcatalog.py`；菜单行控件进 `atri_x/gui/toolmenu.py`；群策略读写
  （`load/save_scope_policy`、预设）进 `atri_x/gui/toolpolicy.py`，
  `atri_x/gui/tabs/toolperm.py` 只留页面编排（仍从原位置再导出旧名字，调用方不用改）。
- 测试：`tests/test_gui_tabs.py` 新增工具目录 4 例（目录与 `REGISTERED_TOOLS` 集合相等、
  每条都有标题/解释/分组/风险、目录外工具归「其它工具」、分组顺序）与菜单交互 4 例
  （激活/停用与状态徽标、锁定模式、搜索与「只看已激活」、激活后保存回读）。

## [0.9.11] - 2026-10-21 — 修复上游把工具调用写成正文导致搜索不执行、协议标记被发到用户

> 旧状态：`atri_x` 只认 OpenAI 结构化 `tool_calls`。2026-10-21 线上事故（0.9.10 便携版 +
> `opencode-go` / `deepseek-v4.1-flash`）里，网关没把模型的原生工具调用翻译成 `tool_calls`，
> 而是把 DSML 文本协议 / `<tool-call>` JSON 原样塞进 assistant 正文：`tool_call_records`
> 为空（工具一次都没执行），用户拿不到搜索结果，QQ 群里还收到了
> `<｜｜DSML｜｜ calls> …` 协议原文。
> 新状态：每条模型回复都过一遍「文本工具调用兜底」——正文里有协议标记时先尝试还原成
> `ToolCallRequest` 并**继续工具轮**（还原成功时正文清空，与原生 `tool_calls` 回合一致）；
> 还原不出来、或 chat 会话本来就不吃工具 schema，则把正文换成可读提示，**协议标记绝不外发**；
> 每次处置写一条 WARN 日志 + `llm_tool_calls_in_text` 审计（只记工具名与是否还原，不记正文）。

### 新增

- `atri_x/llm/tooltext.py`：文本工具调用的判定 / 还原 / 清洗（纯函数，无 I/O）。
  - `looks_like_text_tool_call`：按 `` ｜DSML｜ `` 协议分隔符或 `<tool-call>` 标签判定，
    日常文本不会误判；
  - `recover_text_tool_calls` / `parse_text_tool_calls`：还原成 `ToolCallRequest`
    （`arguments` 统一为 JSON 对象字符串），同时给出清洗后的正文；
  - `strip_text_tool_calls`：删掉正文里的协议标记（含 `</` 被打成
    `<｜place▁holder▁no▁73｜>` 的碎片）；
  - 覆盖线上四种破损形态：`parameter name=` 被吞、`invoke` 无 name、外层 `calls` 标签缺失、
    `<｜place▁holder▁no▁73｜>` 替代 `</`；另外支持 `<tool-call>` JSON（单对象 / 数组 /
    逐行 / `arguments` 为 JSON 字符串 / 缺闭合标签）。
- `atri_x/agent/chat_anomaly.py`：`apply_text_tool_recovery` —— 把还原结果接回
  `LLMCallResult`（补 `tool_calls` 与缺失的 call id、清空正文）；已有结构化调用时只清洗
  正文、不重复执行；写 WARN 日志与 `llm_tool_calls_in_text` 审计。
  生产装配只把 `AgentOptions.logs` 存成 `agent.logs`（`set_log_manager` 没有任何调用点），
  因此日志/审计通道按需从 `agent.logs.runtime()` / `agent.logs.audit()` 补挂一次——
  否则这两条证据会静默丢失。
- 测试：`tests/test_llm_tooltext.py`（夹具直接取自线上泄漏原文）、
  `tests/test_agent_chat_anomaly.py`（含"通道未接线时日志/审计仍落盘"的回归用例）。

### 变更

- `atri_x/agent/chat_turn.py`：每次 `call_llm` 返回后统一过一遍
  `apply_text_tool_recovery`，工具轮、最终回答、chat 会话三条路径都受保护。
- 版本 0.9.10 → 0.9.11：`atri_x/__init__.py`、`pyproject.toml`、`deploy/VERSION`、README 同步。
- 0.9.11 产物已重建：**便携版 zip + 全仓库源码归档**（zip / tar.gz）；安装版本轮不重建，
  0.9.10 安装包归档在发行库 `_previous/0.9.10/`。
- `devdocs/code-map.md`、`PORTING.md` §4.1 登记两个新模块。

### 验证

- 门禁：`check_syntax` 893 个文件 0 错（`bom=0`、`crlf=0`、`damaged=0`）；
  `check_imports` `modules=695 imported=695`；`check_integration` 74/0；
  `check_coverage` packages=72 covered=72（weighted=100.0%）；`check_except` 119=基线 119；
  `gen_stats --check` OK；`check_secrets --all` 0 命中。
- 单测：新增 2 个用例文件 28 用例全过；相关模块（chat / agent / toolrun / openai 适配器 /
  装配）共 **547 用例 OK**。
- 端到端（本地假上游复现事故）：
  - 源码层：假上游把 `discover_tool` 的 DSML 原文当正文返回 → 自动还原 → 执行
    `discover_tool` → 继续工具轮并给出答案；
  - **冻结产物层**：对 0.9.11 便携版解压后跑同一复现，CLI 侧看到
    `NOTICE 正在调用 discover_tool`、工具轮真的执行、最终回答正常，日志里有
    `WARN llm returned tool calls as text` 与 `llm_tool_calls_in_text` 审计；
    唯一残留是流式中间帧会先显示被泄漏的协议标记（GUI/CLI 观感问题，QQ 非流式不受影响）。
- 脱敏：构建前源码树 999 个文本文件 0 命中；产物内 onedir 25,613 / 便携 zip 25,618 个
  文本条目 0 命中。

---

## [0.9.10] - 2026-10-21 — 接入 DeepSeek 官方网页搜索与网页抓取、仓库脱敏与 0.9.10 三产物重建

> 旧状态（一）：`web_search` 的通道链只有 Tavily / Firecrawl / Keenable / Exa / SearXNG /
> Antigravity / Grok 七种协议，没有 DeepSeek 官方搜索（`POST /v1/tools/search`）。
> 新状态：新增协议 `deepseek`——`query` + `count`（`need_content` 固定 `false`），
> 用 `summary`（600~800 字，给模型读）填 `snippet`、`date` 非空时前置成
> 「（发布：YYYY-MM-DD）」；`rerank_score` / `authority` 不落库，只在通道说明里提示。
> 计费口径写进通道 `warnings`（5 元 / 1000 次成功请求）。默认链变为
> **DeepSeek（有 key 才参与）→ Tavily → Firecrawl → Keenable → Exa**：没配 key 的部署
> 行为与 0.9.9 完全一致（`KEYED_PROTOCOLS` 的跳过规则）。
>
> 旧状态（二）：`web_extract` 是写死的「Jina（有 `JINA_API_KEY`）→ 直接抓取」两级，
> 没有任何配置段，也没有官方抓取通道。
> 新状态：新增 `[web_extract]` 配置段（`engine_order` + `engines.<name>`，字段与
> `[web_search]` 同形），通道抽象为 `deepseek` / `jina` / `direct` 三种协议，单条失败
> 自动降级；`deepseek` 调 `POST /v1/tools/fetch` 返回标题 + Markdown 正文，按次计费，
> **不在默认链里**（默认仍是 Jina → 直接抓取，旧行为不变）。缓存 key 已按通道
> `source` 区分，同一 URL 走不同通道不会串缓存。
>
> 旧状态（三）：GUI 只有「搜索服务（web_search）」一个子页；密钥只有一个环境变量名。
> 新状态：「模型」页新增第三个子页「抓取服务（web_extract）」；DeepSeek 的密钥口径统一为
> **`DEEPSEEK_SEARCH_KEY` 优先、回落 `DEEPSEEK_API_KEY`**，工具层与界面密钥状态用同一个
> `key_env_names`（避免「界面显示已设置、实际取不到 key」）。

### 新增

- `atri_x/tool/builtin/web_search_engines.py`：协议 `deepseek`（常量、默认端点
  `https://api.deepseek.com/v1/tools/search`、`search_deepseek`、`key_env_names` 与
  `FALLBACK_KEY_ENV`）；`BUILTIN_PROTOCOLS` / `KEYED_PROTOCOLS` / `DEFAULT_KEY_ENV` /
  `default_specs` 同步加入。
- `atri_x/tool/builtin/web_extract_engines.py`：抓取通道（`deepseek` / `jina` / `direct`）
  的请求与解析、`ExtractEngineSpec` / `specs_from_config` / `default_specs` /
  `key_env_names`；纯标准库 HTML 清洗函数从 `web_extract.py` 迁到这里。
- `atri_x/config/services.py`：`WebExtractEngineConfig` / `WebExtractConfig` +
  `ServicesConfig.web_extract`；`atri_x/config/app.py` 的 `AppConfig` 与 `merge_services`
  同步（缺席段保留旧值，与 `[web_search]` 一致）。
- `atri_x/app/tool_runtime.py`、`atri_x/app/runtime.py`、`atri_x/tool/builtin/register.py`：
  把 `web_extract` 配置沿装配链传到工具（`RegisterOptions.web_extract`）。
- `atri_x/gui/extractconfig.py`、`atri_x/gui/tabs/extractpanel.py`：抓取服务的桌面端数据层
  与子页；`atri_x/gui/tabs/model.py` 增加「抓取服务（web_extract）」二级页签。
- `atri_x/gui/searchconfig.py`：DeepSeek 协议的显示名/说明/地址占位，默认链加入
  `deepseek`；`engine_key_state` 改用 `key_env_names` 的回落口径。
- 测试：`tests/test_web_extract_engines.py`（新）、`tests/test_gui_extractconfig.py`（新），
  以及 `tests/test_web_search_engines.py` / `tests/test_config_web_search.py` /
  `tests/test_gui_searchconfig.py` 的 DeepSeek 与 `[web_extract]` 用例。

### 变更

- `atri_x/tool/builtin/web_search.py`：`run_engine` 增加 `deepseek` 分支；`engine_api_key`
  按 `spec.key_envs()` 依次尝试（DeepSeek 回落 `DEEPSEEK_API_KEY`）；默认链、工具说明与
  无可用通道的报错文案同步更新。
- `atri_x/tool/builtin/web_extract.py`：改为按 `[web_extract]` 通道链编排（缓存、分段输出、
  代理与 `jina=false` 语义保留）；`web_extract` 工具说明更新。
- 发行资产 `atri_x/config/assets_data/services.toml` / `.env.example`：补 `[web_search.engines.deepseek]`、
  `[web_extract]` 示例与 `DEEPSEEK_SEARCH_KEY`。
- 文档：`docs/configuration.md`（协议表、默认链、`[web_extract]` 配置与计费/QPS 说明）、
  `devdocs/code-map.md`（搜索/抓取通道与两个 GUI 数据层）。

### 测试

- `python -m unittest tests.test_web_search_engines tests.test_web_extract_engines tests.test_config_web_search tests.test_gui_searchconfig tests.test_gui_extractconfig`：覆盖 DeepSeek 搜索请求/解析/密钥回落/降级、DeepSeek 抓取 404 降级到直接抓取、按通道缓存、
  `[web_extract]` 严格解析与 GUI 存取。
- `python scripts/check_syntax.py`、`python scripts/check_imports.py`、`python scripts/check_integration.py`。

### 脱敏

- 仓库侧复跑 `python scripts/check_secrets.py --all` → **0 处命中**（git 跟踪 + 未跟踪文本：
  真实个人目录、`atrix-gui-*` / `atrix-ops-*` 令牌、`sk-*` 密钥形状、`.env` 真值赋值）。
- 历史 CHANGELOG 里残留的本机构建机绝对路径（构建目录、发行库盘符、工作区盘符、本机 DSH 的
  `node_modules` 路径）统一改成 `<构建目录>` / `<工作区>` / `C:\path\to\node_modules` 占位；
  仓库内不再出现 `C:\Users\<真实用户名>`、主机名、内网 IP 或真实 key（`tests/` 只有固定假值）。
- 代码与配置侧再扫一遍：无本机用户名、无盘符绝对路径写死、无 `DESKTOP-*` 主机名；
  `docs/` 与 `packaging/README.md` 里的 `C:\path\to\node_modules`、`D:\napcat\...` 都是示例占位。
- 发行物侧：内置 QQ 协议端继续由 `packaging/qqproto.py` 跳过
  `resources\app\napcat\{config,cache,logs}` 与 `*.log`；便携包/安装包内的文本条目再按同一套
  规则扫描（结果见下方「发行」与发行库的 `自检报告.md`）。

### 版本

- `atri_x/__init__.py`（`__version__`）、`pyproject.toml`、`deploy/VERSION` 三处同步
  `0.9.9` → `0.9.10`；`README.md` 版本行与性能说明里的版本引用、`docs/windows-app.md` 与
  `packaging/README.md` 的发行实测数字同步。

### 发行（0.9.10 全仓库归档 + 便携版 + 安装版）

构建环境：Windows 10 x64、Python 3.14.7 + PyInstaller 6.22.3 + Pillow 12.3.0（保留可选加速）、
Inno Setup 7；内置 DSH 内核 0.2.0-rc.2（384 包 / 189.1 MB + Node v24.19.0，
ACP `initialize` 握手通过）。构建在**仓库外**的干净树（`<构建目录>\src`，避开 DSH 工作区的
Low 完整性标签）进行，一条命令出两个自检产物：

```powershell
robocopy <仓库> <构建目录>\src /E /XD <仓库>\.git <仓库>\dist <仓库>\build __pycache__ tmp_* /XF *.pyc
cd <构建目录>\src
python packaging\assemble_kernel.py --modules "C:\path\to\node_modules"      # 首次 / 换 DSH 版本时
python packaging\build.py --release --with-pillow --with-kernel --with-qqproto "<NapCat.52230.Shell>"
```

| 产物 | 体积 | 自检 |
|---|---|---|
| onedir（中间产物） | 333.4 MB / 24,516 文件（其中内核 277.6 MB） | `--version`（`atri-x 0.9.10`）、`config check`、`-m` 转发、`sidecar`（内核 `found: true`）、GUI `--selftest` 全过 |
| `ATRI-X-0.9.10-portable-win64.zip` | 682,683,109 字节（651.1 MB，deflate 42%，打包 42.6 s） | 解压 14.4 s / 27,034 条目后 10 项全过（`--version` / `config check` / 数据目录落包内 / `qqproto` / `-m` 转发 / `sidecar` / GUI `--selftest` / `start-portable.bat` / `debug-console.bat` / APPDATA 重定向） |
| `ATRI-X-0.9.10-setup.exe` | 457,387,788 字节（436.2 MB，ISCC lzma2/max 293.6 s） | 静默安装 65.4 s / 27,032 文件 → `--version` 通过 → 静默卸载 8.3 s，安装目录清空 |
| `ATRI-X-0.9.10-repo.zip` / `.tar.gz` | 全仓库源码归档（不含 `.git`、`dist/`、`build/`、`runtime/`；工作树含未提交改动） | zip/tar 条目数与 `git ls-files` + 未跟踪文件清单一致、CRC 校验通过；文件大小与 SHA256 见发行库 `SHA256SUMS.txt` |

包内规模：内置协议端 `qqproto` 2,513 文件 / 1,202.3 MB（跳过 NapCat
`resources\app\napcat\{config,cache,logs}` 与 `*.log`，最长相对路径 128 字符，`sanitized: yes`），
NapCat 4.18.38 + 便携 QQ 9.9.33-52230。

| 文件 | SHA-256 |
|---|---|
| `ATRI-X-0.9.10-portable-win64.zip` | `2370c7b21857ccc36cb0ee046c7e0703b4279baf13bb0d76dda1c14af50a11ef` |
| `ATRI-X-0.9.10-setup.exe` | `8e34fa95d29097002780c2e68e16c95fe361772085f73b2e37663b74779f92dd` |

独立复验（构建脚本之外另跑）：便携包解压 27,034 条目 → `--version` / `config check` /
`-m atri_x.sidecar status` / `-m atri_x.dist.install status` / `-m atri_x.gui.qqproto --json`，
断言 `.env` 令牌 == `onebot11.json` 令牌、`app.toml` 的 `ws_url` 端口 == OneBot 端口（3001）、
WebUI 配置存在、`qqproto` 下没有 `.env`；安装包静默安装 70.0 s / 27,032 文件后做同样断言，
卸载 6.0 s、`leftover=0`、安装目录被删除。两个产物均 `ok=true`。

> 构建注意（本版首次构建踩到并已修正）：清构建树时若用 `robocopy /XD dist build` 这类**裸目录名**，
> 它会排除任意层级的同名目录，连 `atri_x/dist/` 这个 Python 包也一起排掉，冻结产物里
> `-m atri_x.dist.install` 会报 `No module named 'atri_x.dist'`。排除项必须限定到仓库根
> （`/XD <仓库>\dist <仓库>\build`）。修正后重建，独立复验已把
> `-m atri_x.dist.install status --json` 纳入断言。

### 验证

```
python scripts/check_syntax.py      889 个文件，0 错（bom=0 / crlf=0 / damaged=0）
python scripts/check_imports.py     693/693，0 失败
python scripts/check_integration.py 74 项，0 失败
python scripts/check_coverage.py --summary   72/72，weighted=100.0%
python scripts/check_except.py      OK（119 = 基线 119）
python scripts/gen_stats.py --check OK
python scripts/check_secrets.py --all        0 命中
python -m unittest（DeepSeek 搜索/抓取 + 配置 + GUI + 装配相关模块）  203 用例 OK
```

- 发行物脱敏扫描（构建脚本之外）：onedir 25,613 个文本条目、便携 zip 25,618 个文本条目
  → **0 命中**（个人目录 / `atrix-*` 令牌 / `sk-*` 密钥 / `.env` 真值 / 本机构建路径）。
- 完整 `unittest discover` 在本机沙箱里会被 Docker 部署类用例卡住（`tmp_deploy_harness_*`），
  与本次改动无关；相关用例已按模块单独跑过。

---

## [0.9.9-local.5] - 2026-10-20 — 修复 retain 参数错位导致所有工具被拦下、会话命名通知者签名错位与命名 token 上限过小

> 旧状态（一）：从 0.9.8 起，`toolrun.runner` 在"已确认并即将执行"的媒体参数留存一步，
> 按 **Go 版签名**传了 `(ctx, session_id, arguments)`；而本仓移植把
> `media.Manager.retain_session_tool_arguments()` 有意改成了**同步两参**方法（Go 的
> `ctx` 只服务于同步 SQLite 调用，见 `atri_x/media/reference.py` 的「同步 vs 异步」）。
> 真实装配里 `manager.media` 永远非空，于是**每一次**工具调用都在执行前抛
> `TypeError: Manager.retain_session_tool_arguments() takes 3 positional arguments
> but 4 were given`，又被该步的宽泛 `except BaseException` 记成
> `retain media arguments: ...`，返回给模型和用户 —— 连 `discover_tool` 都执行不到，
> 表现为「只能对话、无法调用任何工具」。
>
> 旧状态（二，真机验证时发现）：`app/naming.py` 的 `NamingLogger` 保留了 Go 的
> `(ctx, event)` 签名，而 `session/naming.py` 按本仓 `NamingNotifier` 协议只传
> `event`。第 `trigger_step` 条消息排期命名时抛
> `TypeError: NamingLogger.notify_naming_scheduled() missing 1 required positional
> argument: 'event'`，整个回合被记成「请求失败」，命名还永远停在 in-flight
> （标题再也不生成）。
>
> 旧状态（三，修好前两处后继续真机验证发现）：`agent/title.py` 的命名请求沿用了 Go 的
> `max_tokens=32`。实测 `opencode-go/deepseek-v4.1-flash` 在这个上限下把 32 个 token
> 全花在正文之前（`finish_reason=length`、`delta_content` 为空），自动命名始终以
> `invalid title` 失败，标题停在兜底文本。
>
> 为什么测试没发现（前两处同因）：测试替身照抄了错误的调用/签名 ——
> `tests/test_toolrun.py` 的假 `Media` 是三参调用、`FakeNamingNotifier` 是单参假实现，
> 与生产侧的真实对象正好错位，单测全绿，真实装配必炸。与
> `StartToolRequestContractTests` 记录的 `start_tool_request` 四元组问题属同一类缺陷
> （假实现与真实实现签名不一致）；第三处则属于"Go 的取值只对 Go 时代的模型成立"。
>
> 新状态：工具调用在同样的确认/留存/执行链路上正常执行，参数里的媒体 ID 仍以
> `session_tool` owner 原子关联到 Session；命名排期/完成/失败三个事件都能落到运行
> 日志与审计，命名请求也能拿到标题。三处都补了"用**真实**对象/真实取值跑一遍"的回归
> 用例，今后签名或取值再漂移会直接失败。

### 修复

- `atri_x/toolrun/runner.py`：留存调用改为 `retain(session_id, call.arguments)`（同步
  两参），并保留对 awaitable 返回值的兼容；补注释说明与 Go 的差异。
- `atri_x/app/naming.py`：`NamingLogger` 的三个事件方法去掉 `ctx`，与
  `session/types.py` 的 `NamingNotifier` 协议对齐（Go 里收 ctx，本仓 session 服务不持
  ctx，属有意偏离）；模块 docstring 记录该偏离。
- `atri_x/agent/title.py`：命名请求 `max_tokens` 从 Go 的 32 放宽到 256，注释记录实测
  依据（32 时 finish_reason=length 且正文为空）。

### 测试

- `tests/test_toolrun.py`：新增 `MediaRetainContractTests`，用**真实**
  `atri_x.media.manager.Manager`（内存仓储替身，只走同步引用路径）驱动 runner，断言
  工具真正执行、参数里的媒体 ID 以 `session_tool` 引用留存；修好前该用例复现
  `takes 3 positional arguments but 4 were given`，修好后通过。
- `tests/test_toolrun.py`：`test_media_retain_failure_denies` 的假 `Media` 改成与真实
  实现一致的同步两参签名（旧假实现只是被 TypeError 顺带"通过"）。
- `tests/test_session_lifecycle.py`：`TestServiceNaming` 新增
  `test_real_naming_logger_matches_session_protocol`，用**真实** `NamingLogger` 作通知者
  跑 `maybe_schedule_naming`；修好前复现
  `missing 1 required positional argument: 'event'`，并覆盖 completed/failed 两个事件。
- `tests/test_agent_title.py`（新增）：命名请求的 `max_tokens` 下限（旧值 32 复现
  "自动命名必失败"）、命名模型失败回退主模型、`title_prompt` 角色渲染。

### 发行（本机重建 0.9.9 便携版 + 安装版，均内置 QQ 协议端与 DSH 内核）

产物、校验和与自检报告见本机发行库（`SHA256SUMS.txt` / `发行说明.md` /
`自检报告.md`）：便携 zip 681,260,343 字节（SHA256 `b742b533…`）、Inno 安装包
456,714,636 字节（SHA256 `ff0aa20c…`）。构建命令与上一版相同：
`python packaging/build.py --release --with-pillow --with-kernel --with-qqproto "<NapCat.52230.Shell>"`。
便携包 10 项自检与安装包静默安装/卸载（26384 文件）均通过；真机 `doctor --e2e` 全绿
（含真实 OpenCode Go `chat_call` 与 CLI 远端往返）。另用 CLI 通道实测：`discover_tool`
在 `tool_call_records` 记为 success、助手回复 `TOOL-PROBE-OK`；同一会话第 3 条消息
出现 `session naming scheduled` / `session naming completed title=工具探测与标记验证`，
无「请求失败」。

仓库源码推送到 GitHub `main`（`c8bb8be`），产物发布为 Release `v0.9.9-local.5`；
`v0.9.9-local.4` 的 Release 说明已标注由本版取代。

---

## [0.9.9-local.4] - 2026-10-20 — 修复首次真实对话必然失败的 4 个移植缺口，并支持 Provider 自定义请求头

> 旧状态：0.9.9（含安装版/便携版冻结产物）**从来没有一条消息能走完聊天链路**。四处
> 独立的 Python 移植缺口串在一起，每修好一处就露出下一处；第一处就是用户看到的
> 「请求失败：not enough values to unpack (expected 3, got 2)」。因为错误发生在模型
> 调用之前，看起来很像"Provider / API Key 配错了"，实际与 provider 无关。
>
> 另外，OpenCode Go 这类网关强制要求客户端带 `x-opencode-session` 请求头，旧配置
> 面（`[providers.<name>]` 只有 `base_url` / `api_key_env` / `proxy` / `extra_payload`）
> 无处可配，只能 400。
>
> 新状态：CLI/GUI/QQ 任一路径的入站消息都能走完「Hook 路由 → 媒体本地化 → LLM 调用
> （含工具 schema）→ 按 `MessageContext` 回到正确的输出平台」；`[providers.<name>]`
> 新增 `extra_headers`（保留头 `Authorization`/`Content-Type` 等仍由适配器独占）。
> 已用真实 OpenCode Go 订阅跑通 `atri-x doctor --e2e`（CLI 远端往返）。

### 修复

- `atri_x/hook/runtime/routes.py` + `manager.py`：`route()` 返回 **三元组**
  （第三位与 Go `Manager.Route` 的 `error` 位对齐，恒为 `None`）。旧实现返回二元组，
  而 `agent.hooks.route_hook()` 原样透传、`agent.message._handle_message_inner()` 按
  三元组解包 → **任何入站消息**都在进入聊天流程前抛
  `ValueError: not enough values to unpack (expected 3, got 2)`。
- `atri_x/agent/segments.py` + `chat.py` / `chat_llm.py` / `chat_turn.py`：
  `materialize_media()` 改为 `async`（Go 的 `media.Materialize` 是同步的，Python 要下
  远端媒体），三处调用点补 `await`；并在 `agent/seams.py` 补上 Go 的
  `Agent.materializeMedia` 方法面（`toolrun_adapter` / `tool_transcript` 一直
  `getattr` 它却取不到）。旧实现把 coroutine 当列表用，报
  `'coroutine' object is not iterable`。
- `atri_x/llm/openai/wire.py`：`RequestLog` 的 `tools_hash` 先 `to_openai_tools()` 再
  hash。旧实现对 `list[ToolSchema]` 直接 `json.dumps`，带工具的工作模式每次请求都抛
  `Object of type ToolSchema is not JSON serializable`。
- `atri_x/safecontext.py`（新增 `token_from_ctx`）+ `atri_x/request/manager.py`：
  `requests.start()` 把 Mapping 形式的 ctx 归一成携带原键值的 token。旧实现只认
  `CancelToken`，平台边界（`_RunContext` 经 `ctx_with_value` 变成 dict）传进来的
  `MessageContext` 在 turn 内全部丢失 → 回复退到 headless service 平台，报
  `service platform cannot send chat output without a target platform`。
  `agent/chat.py` 不再把非 token 的 ctx 降级成 `None`。

### 新增

- `atri_x/config/services.py`：`ProviderConfig.extra_headers`（`[providers.<name>]`
  下的 `dict[str, str]`）；`app.py` 启动期校验头名与换行注入，并拒绝覆盖
  `Authorization` / `Content-Type` / `Host` 等保留头；`llm/openai/openai.py` 的
  chat / responses 两个适配器统一从 `_headers()` 合并，`app/models.py` 透传。
  典型用法（OpenCode Go 强制项）：

  ```toml
  [providers.opencode-go]
  base_url = "https://opencode.ai/inference/go/openai/v1"
  api_key_env = "OPENCODE_GO_API_KEY"
  [providers.opencode-go.extra_headers]
  x-opencode-session = "atri-x-local"
  ```

### 桌面端

- `gui/configmodel.py` + `gui/tabs/model.py`：「模型」页 Provider 表单新增**请求头**多行框
  （每行 `Name: value`，写 `[providers.<名称>.extra_headers]`；留空保存即删掉整个子表）。
  保存前与启动期共用同一套校验（见下），保留头、非法头名、换行注入在界面上就被拒绝，
  不用再手改 TOML。
- `config/services.py`：把原来写在 `AppConfig.validate_provider_headers()` 里的规则抽成
  `validate_provider_extra_headers()` + `RESERVED_HEADER_NAMES`，启动期与桌面端保存
  共用一套口径；`AppConfig.validate_provider_headers()` 改为逐 provider 调用它。
- `gui/tabs/link.py`：「在线」页 QQ 平台卡片新增**唤醒词**输入框 +「保存唤醒词」
  （写 `[platform.qqonebot].trigger_keywords`，中英文逗号/顿号分隔；留空需确认，效果是
  只认 @机器人 与回复；保存后提示重启后台服务），并同步显示当前值。
- `gui/tabs/platform.py`：「唤醒词（逗号分隔）」下方补说明：群里 @机器人 或**消息以
  唤醒词开头**（如「atri 你好」）都会唤醒，改完重启后台服务生效，也可在「在线」页改。

### 测试

- `tests/test_hook_runtime.py`：`test_waiting_lease_and_route` 跟随三元组；新增
  `test_route_hook_returns_three_tuple`（`Agent.routeHook` 必须拿到三元组）。
- `tests/test_agent_segments.py`（新增）：异步/同步 `media.materialize` 都要正确 await。
- `tests/test_openai_adapter.py`：新增 `test_debug_log_hashes_tool_schemas`。
- `tests/test_request_manager.py`：新增 `test_start_keeps_mapping_ctx_values`。
- `tests/test_llm_headers.py`（新增）：`clean_extra_headers` 过滤保留头/换行，
  chat 与 responses 适配器都带上 provider 自定义头。
- `tests/test_config.py`：provider `extra_headers` 解码 + 保留头/换行拒绝。
- `tests/test_gui_tabs.py`：「模型」页请求头保存/清除/保留头拒绝/格式错误 4 项。
- `tests/test_gui_link.py`：「在线」页保存唤醒词、清空唤醒词 2 项。
- `tests/test_agent_wakeup.py`（新增）：前缀唤醒词/普通群消息/@机器人/`keyword` 模式/
  私聊/静默时段/命令放行/唤醒词剥离 11 项（此前 `agent/wakeup.py` 没有直接用例）。
- `atri_x/config/assets_data/services.toml`：Provider 段补 `extra_headers` 注释示例
  （发行版首次生成的样板也带提示）。

### 发行（本机重建 0.9.9 便携版 + 安装版，均内置 QQ 协议端与 DSH 内核）

产物、校验和与自检报告见本机发行库（`SHA256SUMS.txt` / `发行说明.md` /
`自检报告.md`）：便携 zip 649.7 MB（SHA256 `97a67ab4…`）、Inno 安装包 435.6 MB
（SHA256 `6dfb5b4f…`）。构建命令
`python packaging/build.py --release --with-pillow --with-kernel --with-qqproto <NapCat.52230.Shell>`；
便携包解压后 10 项自检与安装包静默安装/卸载（26385 文件）均通过，真机
`atri-x doctor --e2e` 全绿（含真实 OpenCode Go `chat_call` 与 CLI 远端往返）。
仓库源码已推送到 GitHub `main`，产物发布为 Release `v0.9.9-local.4`。

---

## [0.9.9-local.3] - 2026-10-20 — 内置 QQ 协议端环境、全仓脱敏与发行库（本机重建，未随上游发布）

> 「换台电脑部署」时最重的一步是另装一套 NapCat + 便携 QQ（约 1.2 GB），且令牌/端口/两边的
> 配置文件都要手工对齐；直接分发的包里也不该夹带本机令牌或登录痕迹。这一版把 NapCat/便携 QQ
> **脱敏后内置**进便携版与安装版，运行期配置（令牌、端口、NapCat 的 `onebot11.json` /
> `webui.json`、`[platform.qqonebot]`）由桌面端在目标机首次启动时生成 —— 发出去的包里不含
> 任何令牌；同时补上「在线」页的「启动协议端」与带令牌的「打开 WebUI」。

### 行为变化

| # | 旧状态 | 新状态 |
|---|---|---|
| 1 | 发行包只有程序本体，接 QQ 要用户自己装 NapCat + 便携 QQ 并手改配置 | `python packaging/build.py --with-qqproto <NapCat.Shell>` 把 **NapCat + 便携 QQ** 脱敏后放进 `<dist>/atri-x/qqproto`；便携 zip 与 Inno 安装包都自带，解压/安装后即可在「在线」页启动协议端 |
| 2 | 内置协议端的令牌/端口需要人工对齐（`.env`、`onebot11.json`、`webui.json`、`app.toml` 四处） | 桌面端启动时由 `gui/qqproto.py` 自动生成/复用令牌，按 `app.toml` 已配端口或空闲端口写齐三处配置；**已有值（含外部 NapCat 配置）一律不动**，没有内置环境时一个文件都不写 |
| 3 | 「在线」页只能「检测端口 / 打开 WebUI（固定 6099 无令牌）」 | 「在线」页新增「启动协议端」（新控制台窗口，关窗即下线），「打开 WebUI」改用内置 `webui.json` 的端口与令牌；协议端卡片显示内置环境路径或"外部自备" |
| 4 | 打包只做 `build.py --release`（便携 + 安装） | 新增 `packaging/qqproto.py`：复制时跳过 `resources\app\napcat\{config,cache,logs}` 与所有 `*.log`，生成纯相对路径的启动脚本/说明/元数据，并核验必需文件、最长相对路径 ≤ 200、文本无盘符路径与令牌 |

### 桌面端（`atri_x/gui/**`）

- 新增 `gui/qqproto.py`（无 Tk）：`bundle_env_root` / `find_shell` / `ensure_qqproto` /
  `launch_background` / `setup_summary`；令牌优先取进程环境变量 → `.env` → 随机生成；
  OneBot 端口优先沿用 `app.toml` 的**回环** `ws_url` 端口，否则沿用旧 `onebot11.json`，
  再否则挑空闲端口；`webui.json` 同理（端口/令牌已有则保留）；`app.toml` 只在
  `[platform.qqonebot]` 缺小节或缺键时补默认值（`enabled = false`、`trigger_keywords = ["atri"]`）。
  另提供 `atri-x.exe -m atri_x.gui.qqproto --json`（打包自检/排障用，摘要不含令牌明文）。
- `gui/service.py`：`GuiSettings` 增加 `qqproto`；`prepare_settings()` 顺带准备内置协议端
  （失败只记原因，不影响启动）；`spawn_service()` 把 OneBot 令牌显式注入子进程环境，
  `service.out.log` 头部记录内置协议端地址。
- `gui/tabs/link.py`：「启动协议端」「打开 WebUI（带令牌）」两个动作 + 内置环境状态行；
  换机器提示改为「程序目录 + 配置目录」两样。
- `gui/__main__.py`：`--url/--token` 覆盖配置时补上 `health_*` / `qqproto` 字段（此前会丢）。
- `gui/__init__.py`、`gui/tabs/__init__.py`：模块职责表补 `qqproto`。

### 打包（`packaging/**`）

- 新增 `packaging/qqproto.py`（独立可跑：`--source/--dest/--check`）。
- `build.py` 新增 `--with-qqproto <Shell 目录>`（只支持 onedir，在核验通过后、打包前落盘）；
  `--release` 未指定时明确打印"本次发行包不含内置 QQ 协议端"。
- `build_portable.py`：`dist_tree()`/`build-info.json`/`README.txt` 记录内置协议端；
  `--portable-verify` 解压后真跑 `-m atri_x.gui.qqproto --json` 并核对
  `.env` 令牌 == `onebot11.json` 令牌 == `app.toml` 的 `ws_url` 端口，自检超时放宽到 1800 s。
- `build_installer.py`：`--verify` 断言 `qqproto\` 的启动脚本与 Shell 目录装进去了；
  自检超时放宽到 1800 s（多了 1.2 GB 解压）。
- `atri-x.iss`：卸载时删除 `{app}\qqproto`（内置协议端的运行期配置不在 [Files] 清单里，
  否则卸载后安装目录会残留 NapCat 的 `onebot11.json` / `webui.json` 等文件）。

### 脱敏

- 发行包**不含**本机令牌/QQ 登录态：来源 Shell 里的 `config`/`cache`/`logs` 与 `*.log` 全部
  跳过；生成的文本核验不含盘符路径、`Users/AppData` 痕迹、令牌形状字符串。
- 新增 `scripts/check_secrets.py`：发布前静态扫描 git 跟踪文件里的真实个人目录、`atrix-*`
  令牌、`sk-*` 密钥与 `.env` 真值赋值（`--all` 连未跟踪文件；`--json` 给流水线用）。
- 仓库侧扫描（个人目录、真实令牌、`sk-` 形状密钥）未发现需要修改的内容；`tests/` 里的
  `sk-...` 均为明显的假值。发行库只放产物、校验和与发行说明。

### 测试与文档

- 新增 `tests/test_gui_qqproto.py`（27 项）：未内置时零写入、首次生成/幂等/已有值优先/
  端口冲突顺延、`prepare_settings` 接线、`LinkTab` 按钮状态、CLI 摘要不含令牌。
- 新增 `tests/test_packaging_qqproto.py`（12 项）：脱敏复制、启动脚本/说明、元数据无绝对路径、
  泄漏 `config`/`*.log`/绝对路径时必须核验失败。
- `docs/windows-app.md`：§4.8 协议端卡片与「启动协议端」、§5.1 内置协议端说明、§7.1/§7.2
  内置负载与「换机器两样」、§10 对比表、§11 排障两条新 FAQ。
- `packaging/README.md`：§2 参数表、§3.5 内置 QQ 协议端、§8 便携包布局与自检。
- `devdocs/code-map.md`、`README.md` 数字随 `gen_stats.py` 同步。

### 发行（本机重建 0.9.9 便携版 + 安装版，均内置协议端）

产物与 SHA256 见本机发行库（`SHA256SUMS.txt` / `发行说明.md`）；
本机构建日志与自检报告见 `自检报告.md`。仓库改动仅本地提交，**未推送**。

---

## [0.9.9-local.2] - 2026-10-20 — QQ 群机器人接入与「在线」页（本机重建，未随上游发布）

> 接 QQ 群当对话机器人时暴露出的可用性问题：QQ 链路是「后台服务 → NapCat 协议端 → OneBot
> 连接」三段，任何一段断掉，界面上都只表现为"群里不回话"；而启用/停用平台要手改
> `app.toml` 再重启服务。这一版把三段状态并排显示，并把上下线做成按钮；同时修正 10 个
> 选项卡后 `Ctrl+10` 这个并不存在的 Tk keysym 会让界面启动失败的问题。

### 行为变化

| # | 旧状态 | 新状态 |
|---|---|---|
| 1 | 只有 9 个选项卡；QQ 是否连上只能看日志/靠群里试探 | 新增第 10 个「在线」页：后台服务、NapCat 协议端、QQ 平台三段状态并排，含连接次数、最近上线/断开、最近错误、收发消息计数，每 2 秒自动刷新（只在切到该页时轮询） |
| 2 | 启用/停用 QQ 平台要手改 `app.toml` 的 `enabled` 再重启后台服务 | 「在线」页提供「连接（上线）/ 断开（下线）」按钮：校验 ws 地址与令牌 → 写一行 `enabled` → 重启后台服务 → 轮询到连接态；不碰 `.env` 里的密钥 |
| 3 | 健康接口默认不启用，界面拿不到平台连接状态（那是后端进程的事实） | 桌面端首次启动时往 `.env` 补 `ATRIX_HEALTH_ADDR=127.0.0.1:32171`（被占则顺延）与随机 `ATRIX_OPS_TOKEN`；**已有值一律不动**。后台服务据此注册 `/healthz`，「在线」页读它 |
| 4 | 10 个选项卡后 `Ctrl+1`…`Ctrl+10` 的绑定会拼出 `<Control-Key-10>`，Tk 抛 TclError，界面死在启动阶段 | `Ctrl+1`…`Ctrl+9` 对应前 9 页，第 10 页 `Ctrl+0` |

### 桌面端（`atri_x/gui/**`）

- 新增 `gui/linkstate.py`（无 Tk）：`read_platform_config` / `set_platform_enabled`（走
  `configedit` 行级补丁，保留注释）、`connect_blocker` / `secret_state`（连之前先查 ws 地址与
  `.env` 令牌）、`probe_endpoint`（协议端 TCP 探测）、`fetch_health` / `parse_health`
  （带 Bearer 拉 `/healthz`，503 也解析；显式禁用系统代理，避免回环请求被代理劫走）、
  `format_uptime` / `format_clock` / `describe_platform` / `describe_model`。
- 新增 `gui/tabs/link.py`（「在线」页）：三张状态卡 + 模型/就绪卡 + 部署提示；
  `on_show` 启动轮询、`on_hide` 停止；连接开关确认后写配置并重启服务，失败原因落状态行与
  `gui/gui.log`；「检测端口」「打开 WebUI」「复制连接信息」三个辅助按钮。
- `gui/service.py`：`GuiSettings` 增加 `health_addr` / `health_token`；新增
  `ensure_health_endpoint()`（幂等、已有值不动、端口被占顺延）与 `_pick_health_addr()`；
  `spawn_service()` 把两个环境变量显式注入子进程，`service.out.log` 头部记录状态接口地址。
- `gui/app.py`：`_TABS` 增加「在线」（平台与群友之间）；`_on_tab_changed` 增加 `on_hide`
  钩子（切走时停轮询）；修正 `Ctrl+0` 绑定。
- `gui/tabs/__init__.py`、`gui/__init__.py`：模块职责表补 `linkstate` 与「在线」页。

### 测试与文档

- 新增 `tests/test_gui_link.py`（31 项）：真起本机 HTTP 服务测 `/healthz` 客户端（200/401/503/
  连不上）、平台配置读写与令牌阻断、协议端探测、健康接口自动配置（幂等/保留已有值/端口顺延）、
  以及「在线」页的按钮文案、三段渲染、连接开关落盘、复制信息。
- `tests/test_gui_tabs.py` 页数断言 9→10；`tests/test_gui_service.py` 的
  `prepare_settings` 用例改为同时断言健康变量被补进 `.env`（CLI token 仍优先环境变量）。
- `docs/windows-app.md`：新增 §4.8「在线页」；§4 选项卡清单与快捷键、§5.4 启动、
  §7.2 换机器要搬的三样东西、§10 运维接口对比、§11 排障顺序与三条 FAQ（在线页未连接 /
  状态接口不可达 / NapCat 登录后仍连不上）。
- `docs/configuration.md` 平台配置补一段：桌面端开关/健康接口自动配置；`docs/README.md`、
  `devdocs/code-map.md` 同步。
- README 静态数字按实测同步（`check_syntax` 876、`check_imports` 689、受控文件 999）。

### 发行（本机重建 0.9.9 便携版）

构建命令与上一版相同，工作区外（`<构建目录>`）构建，内置内核沿用本机 DSH 0.2.0-rc.2：

| 产物 | 体积 | 自检 |
|---|---|---|
| onedir（中间产物） | 321.4 MB / 24510 文件 | `--version`、`config`、`-m` 转发、`sidecar`（内置内核可解析）、GUI `--selftest`（10 页逐页切换）全过 |
| `ATRI-X-0.9.9-portable-win64.zip` | 115.2 MB（120,839,764 字节，deflate 36%） | 解压 24515 条目后：`--version` / `config check` / 数据目录落包内 / `-m` / `sidecar` / GUI selftest / 三个 bat / APPDATA 重定向 全过 |

SHA256：`6a0e81594618d31ebeb761592a1a12088c941671027f6082faf31cab639ff7d7`

独立复验（解压到普通目录、用真实 `%APPDATA%` 配置）：新包 `--version` / GUI selftest 通过；
对着本机假 OneBot v11 协议端跑 `-m atri_x.platform.qq_onebot.smoke --listen 3 --json`
得到 `ok=true, event_count=1`（连接/登录信息/事件上报）；后台服务指向该假协议端后
`GET /healthz` 返回 `platforms[qqonebot].connected=true`，「在线」页显示「已连接（QQ 在线）」
（`<构建目录>\evidence\shot_link_tab.png`）。

> 与官方 v0.9.9 便携包的差异同上一版：本机重建包多 648 个非内核文件（构建解释器用户级
> `tzdata` 被 PyInstaller 收集 + UCRT/API-set DLL），内核文件数与官方一致。

### 验证

```
python scripts/check_syntax.py      876 个文件，0 错
python scripts/check_imports.py     689/689，0 失败
python scripts/check_integration.py 74 项，0 失败
python scripts/check_coverage.py --summary   72/72，weighted=100.0%
python scripts/check_except.py      OK（119 = 基线 119）
python scripts/gen_stats.py --check OK
```

---

## [0.9.9-local.1] - 2026-10-20 — 桌面端诊断可见性（本机重建，未随上游发布）

> 本版是**本机重建**：只改桌面端两处「失败看不见」，并按同一份源码重新打包 Windows 便携版。
> 触发背景：本机把便携版解压到带 **Low 强制完整性标签** 的目录（`<工作区>` 根带
> `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`）后启动——进程以 Low 完整性运行，
> 写 `%APPDATA%\ATRI-X` 被拒（WinError 5）、`Shell_NotifyIconW(NIM_ADD)` 返回
> ACCESS_DENIED（没有托盘图标）。以前这两种失败分别是「点掉就没线索的弹窗」和
> 「静默降级为没有托盘」。

### 行为变化

| # | 旧状态 | 新状态 |
|---|---|---|
| 1 | 托盘 `start()` 失败后静默 `self._tray = None`：用户只看到"没有图标"，关窗口行为也莫名变化 | 聊天区通知 + `<配置目录>/gui/gui.log` 记录 + 一次性弹窗（含失败原因）；`--minimized`（开机自启）且托盘失败时强制把窗口显示出来；「帮助 → 托盘/后台说明」显示真实状态 |
| 2 | GUI 启动失败（尤其配置目录都建不出来时）只弹窗；窗口版没有控制台，点掉即无痕迹 | 先写固定日志再弹窗，弹窗文本附带日志路径；落点优先 `<配置目录>/gui/gui-startup.log`，配置目录不可写时依次退回系统临时目录、程序目录 |

### 桌面端（`atri_x/gui/**`）

- `gui.process`：新增 `STARTUP_LOG_NAME` / `startup_log_candidates()` / `startup_log_path()` /
  `record_startup_failure()`；单条记录含时间、版本、`frozen`、title/detail、exe 路径、配置目录。
- `gui.__main__._show_message()`：先落盘再弹窗；弹窗文本追加「已记录到日志：…」。
- `gui.app`：`_start_tray()` 失败不再静默——记 `_tray_error` 并走 `_report_tray_failure()`
  （聊天区通知、`journal.record`、`root.after(600, …)` 一次性弹窗）；`--minimized` 时
  `deiconify()` 并把 `_minimized` 复位；新增 `_tray_config_dir()` / `_warn_tray_unavailable()`。
- `gui.actions.show_tray_help()`：按 `_tray` / `_tray_error` 显示 正常 / 不可用（原因）/ 未启用。
- 正常路径不变：托盘可用、`--no-tray`、`--selftest` 行为与 0.9.9 一致。

### 测试与文档

- 新增 `tests/test_gui_diagnostics.py`（13 项）：托盘失败三件套、minimized 兜底、帮助文案、
  启动日志候选/兜底/追加/全失败、`_show_message` 无 Tk 时仍落盘。
- `docs/windows-app.md`：§4 关窗口行为、§11 已知限制（Low 完整性标签目录、启动失败日志、
  托盘失败不再静默）、排障顺序（桌面端起不来先看 `gui-startup.log`）。
- README 静态数字按实测同步（`check_syntax` 873、`tests/` 177/168、受控文件 996），
  同时修掉 v0.9.9 遗留的 993 与实际不符（当时新增 2 个受控文件后未同步）。

### 发行（本机重建 0.9.9 便携版）

`python packaging/build.py --with-kernel --with-portable --portable-verify`，在工作区外
（`<构建目录>`，避开 Low 完整性标签）构建；内置内核用
`assemble_kernel.py --modules "C:\path\to\node_modules"`
从本机 DSH 0.2.0-rc.2 重新组装（384 包 / 189.1 MB + node v24.19.0 92.8 MB，ACP
`initialize` 握手通过）：

| 产物 | 体积 | 自检 |
|---|---|---|
| onedir（中间产物） | 321.4 MB / 24510 文件 | `--version`、`config`、`-m` 转发、`sidecar`（内置内核可解析）、GUI `--selftest` 全过 |
| `ATRI-X-0.9.9-portable-win64.zip` | 115.2 MB（120,778,979 字节，deflate 36%） | 解压 24515 条目后：`--version` / `config check` / 数据目录落包内 / `-m` / `sidecar` / GUI selftest / 三个 bat / APPDATA 重定向 全过 |

SHA256：`c9edf624b96a6949a27736a8bf0205085dbedad9707d756b70e5b896af58c4b2`

> 与官方 v0.9.9 便携包的差异：本机重建包多 648 个非内核文件（`_internal\tzdata\zoneinfo\*`、
> UCRT/API-set DLL）——构建解释器的用户级 site-packages 里有 `tzdata` 等包，PyInstaller 的
> `hook-tzdata` 把它们收了进来；内置内核文件数与官方一致（23812 个）。功能自检全过。

### 验证

```
python scripts/check_syntax.py      873 个文件，0 错
python scripts/check_imports.py     687/687，0 失败
python scripts/check_integration.py 74 项，0 失败
python scripts/check_coverage.py --summary   72/72，weighted=100.0%
python scripts/check_except.py      OK（119 = 基线 119）
python scripts/gen_stats.py --check OK
python -m unittest discover -s tests -t .   Ran 5692 tests — OK (skipped=4)
```

---

## [0.9.9] - 2026-10-19 — DSH 凭据与用量记账、并发上限接线、脱敏补齐

> 这一批来自对 v0.9.8 的三项只读审计（内置 DSH 内核能否构建工作区容纳对话、多并发下是否
> 用上 CPU 多核、脱敏是否做好）。三条主线都指向「文档承诺了、代码没做到」：
> **DSH 子进程拿不到 ATRI-X 解析出的密钥**（Windows 图形端密钥只写 `.env`）、
> **`[ops]` 并发上限与排队旋钮只解析没有消费者**（并发实际无上限）、
> **脱敏有绕过路径**（结构化日志属性不脱敏、规则集缺 9 类常见凭据形态）。

### 行为变化（升级前需知悉）

| # | 变化 | 影响面 |
|---|---|---|
| 1 | `[ops] max_concurrent_turns/tools/hooks` 与 `queue_*` **真正生效** | 出厂资产为 10/10/10 + 全局队列 20 + 等待 30s：单进程并发从"无上限"变为"10 并发、超出排队、队满/超时拒绝"。要恢复旧行为把这些旋钮显式配 0；旧 `app.toml` 未写这些键时仍是"无上限且并发满直接拒绝" |
| 2 | DSH 子进程**不再继承整份环境**：先按变量名过滤敏感项，再放回显式注入的模型凭据 | 依赖子进程继承 `*_KEY` / `*_TOKEN` / `*SECRET*` 的自定义 DSH 插件需要显式配置（`[agent.dsh] credential_env`） |
| 3 | `[agent.dsh]` 新增 `credential_provider` / `credential_env` / `credential_base_url_env` | Windows 图形端把密钥写进 `%APPDATA%\ATRI-X\.env` 时，DSH 路由不再是每轮 `MISSING_CREDENTIAL` 后静默回原生 |
| 4 | DSH **没回报 usage 的轮次不再记 0** | 硬额度：保留预占并标 `uncertain`（新增审计 `chat_budget_uncertain`）；软账本：该轮不落 token/费用。`/usage` 与日报里 DSH 轮次不再显示 0 用量，而是不计入 |
| 5 | `[agent.dsh].model` / `reasoning_effort` 被内核拒绝时**一次性降级**为内核默认并记一条审计 warning | 以前每轮 `-32602` 失败、连续 3 次后熔断静默回原生；现在第一轮就能正常跑完（填裸模型 id 不再等于开关失效） |
| 6 | 脱敏规则集扩大；日志里 dict / list 等结构化属性也脱敏 | 之前会明文落盘的 AWS AKID、JWT、`Authorization: Basic`、`Cookie`、PEM 私钥块、DSN userinfo、URL query 签名、裸 `bot<id>:<token>` 现在会被遮；cron 等 `extra=dict` 的结构化属性不再绕过（同一个 error 走审计脱敏、走运行日志不脱敏的不一致消失） |
| 7 | 阻塞 IO 与 CPU 任务分池（`atrix-io` 32 / `atrix-cpu` min(8,CPU) / `atrix-media` 10） | 图片压缩不再与 HTTP 请求抢同一个默认线程池；GIL 下**不增加**多核并行度，只换来隔离与背压 |

### DSH（`atri_x/agent/dsh/**`、`[agent.dsh]` 配置）

- 环境：`runtime.child_environment()` 用 `atri_x.processenv` 过滤继承的敏感变量，再叠加显式注入项；
  `transport` / `client` 新增仅关键字参数 `isolate_env`（默认 `False` 保持"继承 + 叠加"的旧语义）。
- 凭据选择：`route_call.resolve_credential_env()` —— 显式 `credential_provider` > 名字含 `deepseek`
  且能供密钥的 provider > 当前 work provider；密钥取 `api_key`，为空再取 `api_key_env` 指向的宿主
  变量；键为空时不注入空值；`base_url` 非官方 `api.deepseek.com` 时一并注入 base URL 变量。
- 用量：`AcpPromptResult.usage_reported` / `DshRunResult.usage_reported`（响应里真有 `usage` 对象才为真）；
  成功与 stopped 分支据此决定「真实结算」还是「预占 + uncertain」。
- 配置降级：`DshRunResult.warnings` + `DshRuntime._apply_overrides()`（model / effort 各自独立，失败一次后不再重试）。
- 脱敏：`AcpProcessError` 的 stderr 尾部、`smoke` 的 command / cwd / prompt / 错误 / stderr 全部过
  `atri_x.redact.secrets`（内核 startup 横幅自己写着"可能含配置或凭据值"）。
- 文档：`devdocs/dsh-acp.md` §1 / §5 / §6 改写（凭据优先级链、usage 事实、model 取值要求、环境收敛）；
  `docs/windows-app.md` §8.1 同步（三个凭据键、model 取值限制、用量口径）。
- 未做（登记）：`dsh/status.py` 不展示新凭据字段；崩溃分支拿不到 `DshRunResult`，其 `warnings` 不进审计。

### 并发（`atri_x/agent/core.py`、`atri_x/ops/executors.py`）

- `_request_settings_from_ops()` 把 `[ops]` 映射成 `Limits` + `QueueConfig`，接进
  `new_manager_with_limits_and_queue`（此前硬编码 `(0.0, {}, None)`，所有旋钮是死配置）。
- 新模块 `atri_x/ops/executors.py`：`cpu_pool` / `io_pool` / `named_pool` / `run_cpu` / `run_io` /
  `shutdown_pools`；迁移 `vision/service`、`tool/builtin/view_image`、`net/httpclient`、`net/s3`、
  `platform/cli/adapter`、`tool/builtin/web_search_cli`。`media/worker.py` 用与闸门槽位对齐的
  `atrix-media` 池（10 线程，保持"槽位跟底层工作走"的既有语义；用通用 CPU 池会把闸门从 10 降到 8）。
- 文档：`docs/configuration.md`「运维超时与并发上限」与出厂资产对齐（队列 20/0/0、等待 30s），
  写明只有 turn/tool/hook 有上限、`queue_wait_kinds` 里的 `"compress"` 目前不生效、
  `provider_queue_max_size` 仍未消费。

### 脱敏（`atri_x/redact/**`、`atri_x/logging/**`、`atri_x/llm/openai/errors.py`、`atri_x/safecontext.py`、`atri_x/health/server.py`、`deploy/_libfs.py`）

- 规则集：新增 PEM 私钥块、裸 `bot<id>:<token>`、DSN userinfo、AWS AKID、GitHub 细粒度 PAT、JWT、
  `Authorization: Basic`、`Cookie` / `Set-Cookie`、URL query 签名参数；`secrets()` 保持幂等
  （新增用例逐条验证二遍 / 三遍收敛）。
- `atri_x.redact` 包级导出 `redact_secrets` / `redact_text`（PORTING §3 契约名，此前只在子模块里）。
- 日志：非标量属性值（dict / list / tuple / set / 其它对象）先 `str()` 再脱敏；降级兜底
  `redact_fallback.FALLBACK_RULES` 与严格模式同口径（补 URL bot、JSON 引号键等）。
- `llm/openai/errors.response_summary` 复用 `redact.secrets`（上游回显的 `Incorrect API key provided: sk-…`
  不再进用户可见错误 / 审计）；`safecontext` 的回溯输出、健康 checker 的 `str(exc)`、watchdog 的独立
  检测形态（AKID / GitHub PAT / 私钥头 / Basic / DSN）同步补齐。
- 基线：`scripts/except_baseexception_baseline.json` 因 `health/server.py::evaluate_ready` 的处理器正文
  变化做了位置级更新（旧指纹消失、新指纹出现，计数不变：119 处 / 111 个位置）。

### 其他

- README 静态数字与实测同步（687 模块、176 个测试文件 / 167 个 `test_*.py`、993 个受版本控制文件、
  `check_syntax` 872）：其中 982→993 等条目在本次改动前就已漂移，`gen_stats.py --check` 当时即失败。

### 发行（v0.9.9）

Windows 三种产物按 0.9.9 重建（`python packaging/build.py --with-kernel --release`，
内置 DSH 内核 `0.2.0-rc.2` / 384 包 / 277.6 MB，`assemble_kernel.py` 的 ACP `initialize`
握手核验通过）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.8 MB / 23862 文件 | PyInstaller 98.1 s | `--version`（`atri-x 0.9.9`）、`config check`、`-m` 转发、`sidecar`（内置内核可解析）、GUI `--selftest` 全过 |
| `dist/portable/ATRI-X-0.9.9-portable-win64.zip` | 113.9 MB（源 318.8 MB，deflate 36%） | 打包 9.8 s | 解压 19.2 s（23867 条目）后 `--version` / `config check` / 数据目录落在包内 / `-m` / sidecar / GUI selftest / `start-portable.bat` / `debug-console.bat` / APPDATA 重定向全过 |
| `dist/installer/ATRI-X-0.9.9-setup.exe` | 56.5 MB | ISCC 47.4 s（Inno Setup ，自带简体中文界面） | 静默安装 51.8 s / 23865 文件、`--version` 通过、静默卸载 14.4 s 且目录清空 |

| 文件 | SHA256 |
|---|---|
| `ATRI-X-0.9.9-portable-win64.zip` | `206de2405aed0860424dacaa75be3b149f7ee51bb03898415520381f9761b72e` |
| `ATRI-X-0.9.9-setup.exe` | `c1990cc0ebdb0004a95bcada4b72b056fdd083d62daa4e670ec200f710067ac5` |

### 清理与脱敏核对（v0.9.9 发行前）

- **仓库**：对 `git ls-files` 全量扫描两类标记：①凭据形态（`sk-*` / GitHub PAT / AWS AKID /
  Slack token / JWT / PEM 私钥头 / bot token）；②本机与构建机路径（宿主机用户目录、
  工作区根路径、dpx 隔离环境目录、其他构建机工具目录）。→ **0 处真实本机路径**；命中的
  凭据形态全部是 `tests/**` 与文档里刻意构造的假值（`sk-abcdefgh…`、
  `AKIAIOSFODNN7EXAMPLE`、假 JWT / 假私钥块），与 v0.9.8 的登记一致。裸用户名只出现在
  上游 GitHub 地址 `github.com/fujiwarashigure/ATRI-X`（公开身份），不涉及本机用户目录。
- **产物**：onedir 23862 文件、便携版真解压 318.8 MB、`setup.exe` 本体按字节扫描本机路径
  标记 → **0 命中**；仅剩第三方 `node_modules` 文档里的示例值（AWS 文档示例 AKID、
  `jose` 的 `BEGIN PRIVATE KEY` 字符串、`zod` 测试 JWT），与本机无关。
- **构建日志**（`build/`，不入库）里有本机 PyInstaller hooks 路径；`build/`、`dist/`、
  `runtime/` 都在 `.gitignore`，不会进仓库。`packaging/README.md` 的 Inno Setup 示例路径
  改成 `C:\tools` 占位，与“构建机路径一律写占位”的口径一致。

### 验证

```
python scripts/check_syntax.py      872 个文件，0 错
python scripts/check_imports.py     modules=687 imported=687，0 失败
python scripts/check_integration.py 74 项，0 失败
python scripts/check_coverage.py --summary   72/72，weighted=100.0%
python scripts/check_except.py      OK（119 = 基线 119）
python scripts/gen_stats.py --check OK
python -m unittest discover -s tests -t .   Ran 5679 tests — OK (skipped=2)
```

另有一次活体探针（`tests/_tmp_verify_lead/`，跑完已删）：用出厂 `app.toml` 资产验证
`[ops]` → `{turn:10, tool:10, hook:10}` + 队列 20/0/0/30s；用假 provider 验证凭据注入
（`DEEPSEEK_API_KEY`，非官方 `base_url` 一并注入、官方端点不覆盖）；验证环境收敛会把
`ATRIX_*_TOKEN` 之类过滤掉而保留注入项与 `PATH`；并用收敛后的环境真实拉起内置内核
（`initialize` OK、`session/new` OK）。

> 全量跑法说明：本机 PowerShell 在 `... 2>&1 | Tee-Object ...` 这类管道下会把进程退出码
> 报成 1（上面已记录的既知坑），以 unittest 的 `OK` 结论为准；单跑模块时
> `$LASTEXITCODE` 为 0。

---

## [0.9.8] - 2026-10-19

> 本版把「研究报告审计修复」与「二审整改」两批工作一起冻结成可交付发行，并配一次全仓
> 清理与脱敏核对、Windows 安装版与便携版（解压即用）重建。修掉的是**让工具链整条不可用**
> 的 `start_tool_request` 契约缺陷、后台 shell 只收紧评估阶段、媒体工作槽位随等待方释放、
> HTTP 三条请求路径只统一了一条、取消被当成普通工具失败、deploy 伪命令未命中会回落真实
> 执行；另外补上 Pillow 精准打包开关与图像后端探针。
>
> 详细改动按"旧状态 → 新状态"写在下面两节（同为 0.9.8：二审整改、研究报告审计修复）。

### 行为变化（升级前需知悉）

七处运行期行为与 0.9.7 不同，其余 API 与配置格式向后兼容：

| # | 变化 | 影响面 |
|---|---|---|
| 1 | 后台（cron / Atrinis）shell 改为**默认拒绝**，只有"整条命令低风险 + 命令名在只读白名单 + 结构合规"才自动执行；Windows/PowerShell 主机没有可判的 AST，后台一律拒绝 | 旧配置里靠后台跑 `rm`/`git clean` 之类的无人值守任务会被拒；`[security].background_shell_allow_all = true` 可显式恢复旧行为 |
| 2 | `open_stream()` 与 `request_multipart()` 现在**默认跟随 3xx**（与 `request()` 一致） | 依赖"原样返回 302"的调用方需要显式 `follow_redirects=False` |
| 3 | `timeout` 从"每跳各自计时"改为**整条重定向链共享总预算**（含重试退避与每跳 I/O） | 多次重定向 + 重试的总耗时不再随跳数放大 |
| 4 | 301/302/303 改 GET 时**同时丢掉 `Content-Type`**（此前只丢 body） | 目标端不再收到与实际 body 不匹配的类型头 |
| 5 | 流式请求的**建连超时并入 `first_chunk_timeout`**（不再单独一档） | 只影响超时归类，不改成功路径 |
| 6 | 非流式 LLM 请求现在也跟随 3xx、并受 `ctx` 取消令牌约束 | 取消时不再等到整个请求跑完 |
| 7 | `toolrun` 在 `authorize_tool=None` 时 **fail-closed**（拒绝而非放行）；确认通道缺失同样拒绝 | 只有"忘了装授权回调"的自定义装配会受影响，生产路径由 `chat_tools._tool_hooks` 装配 |

打包侧另有一处：`--with-pillow` 不再连带保留 numpy/PyYAML（要一起留用 `--with-all-optional`，
等价于 `ATRIX_PKG_KEEP_OPTIONAL=1`）。

---

## [0.9.8] — 二审整改（执行契约 / 默认拒绝 / 取消传播 / 资源生命周期）

> 第二轮对抗性评审（针对上一轮整改后的工作树）确认的遗留问题：`start_tool_request`
> 的返回元数三方不一致导致**工具链整条不可用**、后台 shell 只在评估阶段收紧、
> 媒体工作槽位随等待方释放、HTTP 三条路径只统一了一条、取消被当成普通工具失败、
> deploy 伪命令未命中会回落真实执行、异常棘轮只看总数、Pillow 打包开关不精准。
> 下面按"旧状态 → 新状态"记录。

### Security

- **后台 shell 的确认阶段改为服务端 fail-closed 裁决**（旧状态：`background.confirm_background_sandbox_shell`
  只看评估阶段传进来的 `risk`，critical 拒绝、其余（含 high）自动确认；评估阶段若没有
  沙盒 ctx 或评估失败，确认阶段就只剩"自动确认"）。新状态：确认阶段从工具参数里取出
  `cmd` 文本**重新**做一遍静态裁决（`shell_command_names` 静态确认 + `classify_shell_command`
  + 沙盒加固 + 后台策略），拿不到命令文本 / 解不出来 / PowerShell 主机 / 结构违规 /
  风险超限 / 白名单未命中一律拒绝，`ShellRiskDecision` 新增 `reason` 字段并写进审计。
- **执行前复核**：`ShellTool.call()` 在启动任何进程/解析工作目录**之前**用执行时的 ctx
  再裁决一次（`background_execution_denial`），拒绝时抛 `ForbiddenError`。
  这样"评估时是 low、排队期间换命令"与"前台本会话自动确认放行"两类绕过都堵在执行点；
  同时确认阶段先于前台 `needs_tool_confirmation` / `confirm_tool_call`（有测试钉住顺序）。
- **白名单不再只比命令名**：新增 `background_shell_structure_violation()`，复用
  `shellparse` 的 AST 与 `shell_risk` 的包装器解包，拒绝命令替换/进程替换/算术展开/
  管道/重定向/后台执行符/复合控制结构/包装器内联脚本/`eval|source|trap`/`-c 脚本文本`/
  参数里的动态词与路径逃逸。
- **取消不再被当成工具失败**：`atri_x/tool/executor.py` 的 `assess_risk` 与 `tool.call`
  两个边界、`atri_x/toolrun/runner.py` 的 7 个异步边界（`prepare_tool_call` /
  `preflight_confirmation` / `confirm` / `start_tool_request` / 媒体参数保留 / 输出投递 /
  `complete_tool_call`）与 `_assess_for_run`、`atri_x/agent/chat_turn.py` 的两个 hook
  包装点、`shell.py` 的 `_media_variables` / `_close_media_call` 全部改为
  `except asyncio.CancelledError: raise` + `except Exception`。旧状态下
  `CancelledError` 被折叠成 `ExecutionResult(err=…)`，`runner` 会记一次"工具失败"并
  继续跑同一批剩下的调用。

### Fixed

- **`start_tool_request` 的三元组契约**（P0，真实接入后所有工具调用都执行不到）：
  `runner.run()` 旧代码按 4 项解包，而协议声明与 `AgentToolRunDeps` 都返回 3 项
  → 每次调用抛 `ValueError: not enough values to unpack (expected 4, got 3)`，被宽泛的
  `except BaseException` 记成"启动失败"，工具永不执行且已登记的 request 因为拿不到
  `done` 而永不结束；测试里的假实现当年返回 4 项，所以单测全绿也发现不了。
  现在 `run()` 按三元组解包、登记失败走异常分支，登记成功后的整段执行包进
  `try/finally` 由 `_release_request()` 保证 `done()` **恰好一次**（成功/执行抛错/
  二次授权拒绝/`before_execute` 拒绝/媒体参数失败/取消/提前 return 全覆盖）。
  判据探针（仓库外本地脚本，不随仓库分发；同一探针跑两棵树）：
  修复前 `tool_executions=0 / requests_closed=0 / message="tool call echo failed: not enough
  values to unpack (expected 4, got 3)"`，修复后 `1 / 1 / "ok:echo"`。
- **媒体隔离压缩的槽位生命周期**（旧状态：`run_isolated` 在 `finally` 里释放槽位，
  超时/取消时等待方结束、线程仍在跑、槽位已释放，于是"最多 10 个槽位"不等于
  "最多 10 个真实存活的工作"；`_acquire_gate` 在"槽位到手但 ctx 已取消"时会永久漏掉
  一个槽位）。新状态：`asyncio.shield` + **内部 future 的 done 回调**里归还槽位，
  超时/取消只记 `orphaned` 不再释放；闸门改为显式记账的 `_Gate`（槽位可直接交接给
  队首等待者，交接与取消的竞态可判定），排队有界
  （`IMAGE_WORKER_QUEUE_LIMIT=64` / `IMAGE_WORKER_QUEUE_TIMEOUT=60s`，分别抛含
  `queue full` / `queue timed out` 的 `MediaError`），新增 `worker_stats()` 诊断快照。
  判据探针（本地临时目录里的脚本，不随仓库分发）：旧树"10 个工作还活着时闸门
  报告 10 个空闲槽位""40 次取消后槽位永久泄漏 0/2"，新树为 `0` 与 `2/2`。
- **HTTP 三条请求路径统一**（旧状态：只有 `request()` 跟随重定向，`request_multipart()`
  与 `open_stream()` 原样返回 3xx）。新状态：抽出共用 `_redirect_step()`
  （相对 `Location`、跳数上限、**循环检测**、301/302/303 改 GET 并丢 body 与
  `Content-Type`、307/308 保留方法与 body、跨源剥离 `Authorization`/`Cookie`/
  `Proxy-Authorization`/`Host`、只允许 http/https、每跳关旧连接）与
  `_RedirectBudget`（**整条链共享总超时预算**，含重试退避与每跳 I/O）；
  `request_multipart` 新增 `follow_redirects`/`max_redirects`；`open_stream` 默认跟随，
  3xx 无 `Location` 原样返回、循环/超限抛 `HTTPError`。
- **流式投递的背压与取消**（旧状态：`_pump` 用 `call_soon_threadsafe(queue.put_nowait, …)`，
  队列（maxsize=2048）满时回调抛 `QueueFull`，行与 EOF 都可能丢，消费端会永久挂在
  `queue.get()`；`open()` 被取消时线程与连接不清理）。新状态：同一个
  `run_coroutine_threadsafe(queue.put(...))` future 上带超时轮询 `result()`（不重复入队），
  `_stop` 置位时取消并取走异常后退出；`close()` 额外补一个 EOF 唤醒阻塞的消费端；
  `open()` 被取消时先关连接/停线程再抛。`read_timeout=0` 现在表示"正文不做 socket 级
  读截止"（并在 `getresponse()` 前留住 socket —— SSE 的 `Connection: close` 会把
  `conn.sock` 清空，旧写法连正数 `read_timeout` 都从未生效）。
- **LLM 适配器切回 `HTTPClient`**：删除为绕开旧缺陷而自建的
  `atri_x/llm/openai/transport.py`（`LineStream` / `buffered_request`），
  `open_stream_request` 改用 `self._http.open_stream(..., read_timeout=0,
  queue_maxsize=2048, follow_redirects=True, cancel=ctx)`，非流式改走 `self._http.request(...)`
  且**不**传 `retry`（避免与 `_do_with_retry` 叠加成双重退避）。
- **deploy 伪命令改为严格模式**：设置 `ATRIX_DEPLOY_FAKE_CAPTURE` 即 fail-closed ——
  规则文件缺失/JSON 损坏/条目非法（未知键、无匹配谓词、`argv` 非字符串列表、
  `real` 与 `code` 冲突）/**没有任何规则命中**一律 `AssertionError`（消息带全 argv 与
  规则文件路径），绝不回落去真跑 docker/git；规则支持结构化 `argv` / `argv_prefix`
  谓词（同格多谓词为"与"），逐条 `"real": true` 或全局
  `ATRIX_DEPLOY_FAKE_ALLOW_REAL=1` 才允许真跑（默认关闭）。`FakeDocker._route` /
  `_route_exec` 的"未识别命令返回 0"兜底删除，改为 `AssertionError`，并新增
  `hits` / `hit_log` 命中记账。
- **`scripts/check_except.py` 基线改为位置级**（旧状态：只比 `except BaseException`
  且不 re-raise 的**总数**，总数不变仍可能是"旧问题被删、新问题被加进来"）。新状态：
  条目 = `file` + `qualname` + 归一化 handler 指纹（行号无关）+ `count`；新增位置或
  次数变多即非零退出，"总数不变但位置换了"同样能抓到；位置消失/减少会提示收紧；
  `--update` 打印 diff 摘要，`--self-test` 覆盖五种判定；v1 基线走兼容路径并提示迁移。
- **JPEG 精度口径可复现**：新增 `tests/test_imagecodec_jpeg_precision.py`（52 个固定
  样本 × 两条解码入口，Pillow 缺失时 skip），把模块头部的"平均 ≤0.01 / 最大 ≤1"
  换成实测表 + 样本位置（4:4:4 ≈0.11/2、4:2:2 ≈0.63/4、4:2:0 ≈1.07/4、极高对比度的
  3×5 上采样边界 ≈11.8/53），并注明与 Go `image/jpeg` **未做**差分验证。
- **Pillow 打包开关精准化**：`--with-pillow` 不再连带保留 numpy/PyYAML，新增
  `--with-extras` / `--with-all-optional`（旧的 `--with-pillow` 语义）/
  `--allow-missing-pillow` / `--check-env`；构建环境 `import PIL` 不可用时 exit 2
  （显式开关才降级为警告）；`packaging/atri-x.spec` 的 `excludes` 改为逐包判定
  （`ATRIX_PKG_KEEP_PILLOW` / `ATRIX_PKG_KEEP_EXTRAS`，`KEEP_OPTIONAL=1` 仍等价三包全留）。
- `atri_x/media/imagecodec/backend_probe.py`（新）：`import PIL` 的唯一出处，
  `image_backend_info()` 提供后端名/版本，`python -m atri_x.media.imagecodec.backend_probe --json`
  用于部署侧（含冻结产物）核验；doctor 新增 `[media] image_backend` 检查，`/diagnostics`
  新增同名段。

### Changed（文档）

- `PORTING.md` §4 重定向说明、`AGENTS.md` 验证规则（棘轮改为位置级）、
  `deploy/TESTING.md` §注入点（严格模式与结构化规则）、`packaging/README.md` 与
  `docs/windows-app.md`（新开关 + 产物侧后端核验命令）、`devdocs/code-map.md`
  （`media/worker.py` 槽位语义、`media/imagecodec/` 与 `backend_probe.py`）已同步到当前实现。

### 发行（v0.9.8）

Windows 三种产物按 0.9.8 重建（`python packaging/build.py --with-kernel --release`，
内置 DSH 内核 `0.2.0-rc.2` / 384 包 / 189.1 MB，`assemble_kernel.py` 的 ACP `initialize`
握手核验通过）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 321.3 MB / 24510 文件 | PyInstaller 88.6 s | `--version`（`atri-x 0.9.8`）、`config check`、`-m` 转发、`sidecar`（内置内核可解析）、GUI `--selftest` 全过 |
| `dist/portable/ATRI-X-0.9.8-portable-win64.zip` | 115.1 MB（源 321.3 MB，deflate 36%） | 打包 9.6 s | 解压 11.1 s（24515 条目）后 `--version` / `config check` / 数据目录落在包内 / `-m` / sidecar / GUI selftest / `start-portable.bat` / `debug-console.bat` / APPDATA 重定向全过 |
| `dist/installer/ATRI-X-0.9.8-setup.exe` | 57.0 MB | ISCC 53.3 s（Inno Setup ，自带简体中文界面） | 静默安装 35.4 s / 24513 文件、`--version` 通过、静默卸载 7.9 s 且目录清空 |

| 文件 | SHA256 |
|---|---|
| `ATRI-X-0.9.8-portable-win64.zip` | `913fa49ad9fcb5f1ab205e89da73b0c2440c98d22f374355f74c90886b9803ec` |
| `ATRI-X-0.9.8-setup.exe` | `44254554f31633fe53f503e7b9715fcf7544c92296da7a9df2b5b6a56c1bc46a` |

便携版是**解压即用**形态：zip 顶层是 `ATRI-X-0.9.8/`，里面是两个 exe、`_internal/`
（含 `runtime/dsh-kernel`）、`start-portable.bat`（数据落在解压目录）、`start-normal.bat`、
`debug-console.bat`、`README.txt` 与 `build-info.json`；**必须先解压到短路径**
（内置内核里有 204 字符的相对路径，解压根目录建议 ≤ 55 字符）。安装版是用户级安装
（`PrivilegesRequired=lowest`，不弹 UAC）到 `%LOCALAPPDATA%\ATRI-X\app`，带开始菜单
快捷方式、卸载项与可选开机自启。

### 清理与脱敏核对（v0.9.8 发行前）

- **垃圾清理**：删掉 89 个目录 + 1 个文件（19.28 MiB）——77 个 `__pycache__/`、
  6 个空的 `tmp_*_test/`、`tests/_tmp_*`（上一轮评审留下的探针与测试残留：cron 运行数据、
  media-worker 复现、net 改动前后对照、packaging 探针、dsh 路由）、根目录
  `tmp_ws_report.txt`。`runtime/`（内核产物）与 `build/`（PyInstaller 工作目录）保留，
  两者都在 `.gitignore` 里；删掉的探针脚本另存到仓库外留档。
- **凭据**：994 个会入库的文件（其中 987 个文本文件）+ 全部 7 个 commit 的 blob，按
  `sk-*` / `ghp_*` / `github_pat_*` / `AKIA*` / `xox[baprs]-*` / `Bearer` / JWT / 私钥头 /
  40+ 位十六进制扫描；命中项全部是 `tests/` 与 `CHANGELOG` 里刻意构造的假值
  （AWS 文档示例 key、`sk-abcdef…`、`xoxb-abcdefghij`、假 JWT）与文档示例 IP
  （`192.168.1.10`、`1.2.3.4`）。`CHANGELOG` 里的长十六进制是**公开发行产物的 SHA256**
  与容器镜像 digest，不属于秘密。
- **个人信息**：没有真实用户目录（`C:\Users\<实际用户名>`）、主机名、内网 IP、邮箱、
  SSH 私钥或代理明文口令入库；git 作者只有公开身份
  `fujiwarashigure <fujiwarashigure@users.noreply.github.com>`。文档里的构建机路径一律写成
  `C:\Users\<用户>` / `C:\path\to\node_modules` 这类占位。
- **文档脱敏**：`CHANGELOG` 中原本写死的仓库外探针路径改成中性描述（探针不随仓库分发，
  写死路径会让读者以为能在仓库里找到）；`tests/test_app_doctor_capabilities.py` 与
  `tests/test_vision_threading.py` 的 docstring 去掉指向仓库外审计文件的路径。
- **发行物复核**：onedir（24510 文件）与便携版 zip（**真解压**后 24515 文件）逐文件按
  宿主机用户目录、其它构建机工具目录、dpx 隔离环境目录等 7 类本机/构建机路径标记扫描
  → **0 命中**；`setup.exe` 本体按字节扫描同样 0 命中。产物里唯一的 `C:\Users\...` 是
  第三方 `node_modules` 文档里的示例路径（`C:\\Users\\x\\y\\z`），与本机无关。
- **未验证**：产物只在本机（Windows 10 19045 / Python 3.14.7）通过自检，无代码签名，
  真实平台/模型 E2E、容器镜像重建不在本次范围。

### 验证（v0.9.8）

```
python scripts/check_syntax.py                  870 个文件，0 错（bom=0 crlf=0 damaged=0）
python scripts/check_imports.py                 modules=686 imported=686，0 失败
python scripts/check_integration.py             74 项，0 失败
python scripts/check_coverage.py --strict --summary  72/72，weighted=100.0%（文件存在性口径）
python scripts/check_except.py                  119 = 基线 119（v2：111 个位置）
python scripts/gen_stats.py --check             OK（README 数字与实测一致）
python -m unittest discover -s tests -t .       Ran 5628 tests in 1011.245s — OK (skipped=4)
```

产物侧：便携版解压自检 9 步全过、安装版静默安装/卸载自检通过、发行物脱敏扫描 0 命中、
`atri-x.exe -m atri_x.sidecar status --json` 断言 `found: true`。

> 打包脚本的退出码注意：`python packaging/build.py --release` 把 PyInstaller 日志写到 stderr，
> 若它是 shell 脚本的最后一条语句，PowerShell 5.1 会把整条命令的退出码报成 1（`$?` 被判 false）。
> 构件与自检实际全部成功；核对时请在末尾追加一条中性语句再读 `$LASTEXITCODE`。

---

## [0.9.8] — 审计修复（研究报告 P0/P1）

> 一轮针对 Python 版新增件的安全与资源审计（图像编解码内存放大、后台 shell 自动放行链、
> toolrun 执行层 fail-open、静默降级、WebSocket 分片复杂度、HTTP 重定向缺失）的整改。
> 下面按"旧状态 → 新状态"记录。

### Security

- **后台（cron / Atrinis）shell 从"非 critical 自动确认"改为默认拒绝 + 只读白名单**。
  旧状态下 `env rm -rf /`、`timeout 5 rm -rf /`、`xargs rm -rf`、`find / -exec rm -rf {} +`、
  `eval 'rm -rf /'`、`trap 'rm -rf /' EXIT`、`git -C / clean -fdx` 等一律判 `low` 并被自动执行；
  现在风险分级会解开 `env` / `command` / `nohup` / `timeout` / `nice` / `setsid` / `stdbuf` /
  `ionice` / `chroot` / `ssh` / `watch` / `busybox` / `xargs` / `time` / `exec` / `coproc`，
  并递归解析 `bash|sh -c '…'`、`eval`、`trap`、`find -exec`、`awk 'system(...)'` 里的脚本文本
  （解析失败按 `high`）。
- 新增 `[security].background_shell_allow`（默认 None = 内置只读白名单）与
  `[security].background_shell_allow_all`（默认 false；true 才恢复旧行为）。
  后台只有在"整条命令低风险 + 真实命令名都在白名单里"时才自动执行，其余直接拒绝；
  PowerShell 主机没有 bash AST 可判，后台默认直接拒绝。
- 后台沙盒的路径检查同样会解开包装器与 `-c` 脚本：`env cat /etc/passwd`、
  `bash -c 'cat /etc/passwd'` 不再绕过绝对路径限制。
- `atri_x/toolrun/runner.py`：`authorize_tool=None` 从"全放行"改为 fail-closed
  （返回 `tool authorization is not configured`）；生产路径由 `chat_tools._tool_hooks`
  装配 `authorize_tool_call`，该处的 `except ImportError: return None, None` 静默兜底已删除。
- `atri_x/toolrun/runner.py::_assess_for_run`：风险评估器抛异常时从 `low` 改为 `high`
  （与 `shell_risk` 的"解析失败即 High"统一）。
- `atri_x/agent/toolrun_adapter.py`：确认通道缺失（`agent.turns` 为空）不再返回
  `allowed=True`，改为拒绝并写入 `error=tool_confirmation_unavailable`，审计记 `rejected`。

### Fixed

- `atri_x/net/websocket.py`：分片重组从"每帧重算 `sum(len(chunk) …)`"（O(F²)）改为增量计数，
  新增单条消息分片数上限 `MAX_MESSAGE_FRAGMENTS=65536`（超限 1009），并每 256 帧让出事件
  循环；实测 16k 分片 3.49 s → 0.02 s、32k 分片 14.11 s → 0.05 s。`apply_mask` ≥64 字节
  改走大整数异或，8 MiB 0.44 s → 0.023 s（约 19 MB/s → 361 MB/s）。握手按 RFC 6455
  §4.2.1 校验 `Sec-WebSocket-Key` 解码后恰 16 字节（否则 400）；64 位长度最高位置 1 时
  回 1002 而不是 1009；`read_timeout` 现在同时约束**单帧读取**与整条消息。
- `atri_x/platform/cli/remote_server.py`：远程 CLI 服务端给 `ws.serve` 传 `read_timeout=60s`；
  hello token 比较改 `secrets.compare_digest`；每个 `input` 帧的并发处理加
  `asyncio.Semaphore(8)`，超限回错误帧并记日志，避免一个客户端无限并发触发 LLM。
- `atri_x/net/httpclient.py`：`request()` 默认跟随 3xx（最多 10 跳，可用
  `follow_redirects` / `max_redirects` 调整）：301/302/303 对非 GET/HEAD 改 GET 并丢 body，
  307/308 保留方法与 body，相对 `Location` 用 `urljoin`，跨源丢弃
  `Authorization`/`Cookie`/`Proxy-Authorization`/`Host`。此前 302 会原样返回空 body，
  与 Go 默认 `http.Client` 不一致。当时 `open_stream` / `request_multipart` 仍不跟随
  （已在 docstring 与 `PORTING.md` 登记），随后在下一节「二审整改」里统一，`PORTING.md`
  与 `CHANGELOG` 的旧表述已随之更正。
- `atri_x/media/imagecodec/jpeg_decode.py`：分配分量平面**之前**先按"每个 8x8 数据单元至少
  2 bit 熵数据"做下界校验，并加解码内存预算；741 字节输入声明 10000x10000 时不再先吃
  286 MiB 内存（现在 0 MiB 内被拒）。同时拒绝多个 SOF 帧头（`sniff` 与解码器口径一致）、
  拒绝 SOS 分量顺序与 SOF 不一致（旧实现解出错色图像）、要求 SOF 分量 ID 唯一。
- `atri_x/media/imagecodec/png.py`：解码改为**按行增量解压**（`_RowFeeder`），峰值内存从
  "2×解压原始 + 输出"降到"输出 + 若干行"（16 位 RGBA 从 20 字节/像素降到约 4 字节/像素，
  3000x3000 实测 171.98 MiB → 34.78 MiB）；关键块 CRC 始终校验，辅助块 CRC 不符记入
  `Image.info["crc_mismatches"]`，`strict_crc=True` 时一律拒绝。
- `atri_x/media/imagecodec/image.py`：默认像素上限 100 MPix → 24 MPix（与视觉预处理对齐），
  新增 `DEFAULT_MAX_DECODE_BYTES`；`max_pixels=0/-1` 从"静默关闭闸门"改为 `ValueError`；
  `decode(strict_crc=True)` 会绕过 Pillow 走纯解码器以保证 CRC 真的被校验。
- `atri_x/media/image.py`：导入期压缩显式传 `max_pixels=MEDIA_IMAGE_MAX_DECODE_PIXELS`（24 MPix），
  不再用编解码器默认值。
- `atri_x/media/imagecodec/resample.py` / `ops.py`：`resize(filter=None)` 与 `to_rgb(img, None)`
  从 `AttributeError` / `TypeError` 改为受控的 `ImageCodecError`。
- `atri_x/media/imagecodec/jpeg_decode.py`：**单分量扫描按 T.81 A.2.2 修正 MCU 网格**。
  旧实现一律按 `hmax/vmax` 划 MCU，把采样因子非 1x1 的灰度 JPEG（Pillow 的
  `subsampling=1/2` + `L`、部分扫描仪）多读 `h*v` 个块，合法文件被误报"熵编码数据截断"
  （1x1 / 1x33 / 3x5 全部复现）；现在单分量帧每个 MCU 只解一个数据单元，126 例
  Pillow 语料从 41 失败降到 8（余下 8 例是色度上采样与 libjpeg 的精度差异，
  已在模块 docstring 标注实测口径：4:2:0 常规尺寸平均差 ≈1/最大 ≈4，极端长宽比
  平均差可达 ~15/最大 ~60，旧文档"平均 ≤0.01、最大 ≤1"已改正）。
- 装配/诊断出口（研究报告 §5.1、§5.5）：`runtime_agent` 启动时把
  `AgentServicesReport.to_dict()` 全量落结构化日志（`skipped` 也出现、每项含原因），
  `/diagnostics` 新增 `agent_services` 与 `redaction` 段，`doctor` 新增
  `[agent] services` / `[security] redaction` / `[ops] inbound_rate_limit` /
  `[agent] hook_service` 四项能力检查（有启动期快照按快照断言，无快照只做静态判定并
  显式 `skipped`，不伪造通过）。
- 入站限流接上（研究报告 §5.4）：`runtime_agent` 按 `[ops] user/group_messages_per_minute`
  (+burst) 构造限流器并随 `opts` 传入，阈值同步进 `RateLimitStatus`（不再恒 0）；
  `/diagnostics.rate_limit` 用 `configured` + `disabled_reason` 区分"未配置"与"0 次拒绝"。
- `/hooks` 控制面接上（研究报告 §5.4）：`runtime_agent` 把已构造的 `hook_service` 放进
  `opts`，`agent.hook_service → services["hooks"] → deps.hooks` 全链路打通，不再恒回
  "hook service is not configured"。
- 脱敏降级不再恒等透传（研究报告 §5.2）：`atri_x.redact` 取不到时改用 6 条正则遮盖
  （sk-xxx / Bearer / api_key=… / URL user:password@ / JWT / Telegram token），启动期发一条
  进程内唯一的 ERROR，`redact_status()` 进 `/diagnostics.redaction` 与 doctor；兜底实现
  拆到新模块 `atri_x/logging/redact_fallback.py`（`logging.py` 591 行，回到 ≤600 目标）。
- 视觉预处理移出事件循环（研究报告 §3.3 问题 6）：`vision/service.py` 与
  `tool/builtin/view_image.py` 的同步压缩/解码改 `await asyncio.to_thread(...)`，
  3 MPix 图不再冻结事件循环十几秒。
- 清理 10 处不可达的 `except ImportError` 静默兜底（研究报告 §5.1）：
  `agent/chat_turn.py`（5 处：context_runtime / prompt / tool_transcript ×2 / tool_cache）、
  `agent/chat.py::summary_user_prefix`、`agent/tool_schemas.py::_toolrun_view`、
  `agent/turn_override.py` 的 `@image:` 分支、`app/platforms.py` 的会话浏览扩展、
  `agent/chat_tools.py::_tool_hooks`。这些模块在 `agent/core.py` 里已是模块级硬导入，
  兜底永远不可达，只会把"模块坏了"变成静默降级；现在缺失即显式失败。
- 删除 `atri_x/media/imagecodec/jpeg_encode.py` 里从未被调用的 `_pad_plane()`。

### Changed

- `atri_x/tool/builtin/shell_risk.py`：`git` 全局选项（`-C` / `--git-dir=` / `-c` …）不再把
  子命令挤出视野；新增 `truncate` / `shred` / `wipe` / `ln` / `find -delete` /
  `find -fprint*` / `awk system()` 与 Windows 管理类命令（`reg` / `schtasks` / `sc` /
  `net` / `wmic` 等）的分级。
- `docs/configuration.md`、`devdocs/architecture.md`、`devdocs/atrinis-atriwisp.md`：
  登记后台 shell 白名单与新的分级口径。

### Added

- `[runtime] fail_fast_agent_services`（默认 `false`）：开启后核心装配器缺失即启动失败，
  不再"启动成功但能力静默缺失"。
- `[security] background_shell_allow` / `background_shell_allow_all`：后台 shell 白名单与
  旧行为开关（见上）。
- `atri_x/logging/redact_fallback.py`：脱敏模块缺失时的正则兜底、一次性告警与自检快照。
- `scripts/check_except.py` + `scripts/except_baseexception_baseline.json`：`except BaseException`
  且不 re-raise 的处理器棘轮。基线是**位置级**的（`file` + `qualname` + 归一化 handler 指纹 +
  `count`），新增位置或次数变多即失败，总数不变但位置被换掉同样会被抓到；`--update` 打印
  diff 摘要，`--self-test` 覆盖五种判定。已登记进 `AGENTS.md` 验证规则。
- `scripts/gen_stats.py`：README 数字的**可复算来源**（只读扫描，口径与 `check_syntax` /
  `check_imports` 共用同一份实现），`--check` 在 README 数字漂移时 exit 1、`--update` 打印可粘贴数字；
  用例数不再硬编码进 README。

### Fixed（测试与文档）

- `tests/test_deploy_upgrade.py`：夹具在 PATH 最前放**空壳** `docker` / `docker-compose` / `git`
  （只为 `shutil.which` 命中；真正执行的命令仍先命中伪造命令表），并修正一条永不匹配的伪造表模式
  （`-f <file>` 夹在 `compose` 与子命令之间）。**没有 Docker 的机器上 13 个失败 → 0**
  （此前 `deploy/_proc.py::compose_command()` 的存在性探测不经过伪造钩子，脚本直接
  `die("未找到 Docker Compose")`）。
- `tests/test_deploy_watchdog.py`：`TestWindowsLauncher.load_module()` 导入与包同名的
  `deploy/windows/atri_x.py` 后把真包**原样放回** `sys.modules`；此前它把真包弹掉不还原，
  使后续任意懒 import 的用例撞上替身报 `'atri_x' is not a package`（discover 顺序下侥幸不炸，
  跑子集必炸）。同进程混跑 docker/部署相关 8 个模块：`Ran 243 tests, OK (skipped=2)`。
- `tests/test_imagegen.py`：上游 Go 仓库不在同级目录时明确 `skipTest`（"无法比对 ≠ 检测通过"），
  漂移临时目录改到 `tests/_tmp_imagegen_drift`。
- `README.md`：651/817/5121（与树漂移）→ 686 个 `.py` 模块 / 866 个扫描文件 /
  "用例数不硬编码，现场跑 `discover`"；`check_coverage` 行补明"**文件存在性**口径"；
  "对抗性验证"段加注：历史"全绿"绑定做者机器（有 Docker、上游在同级目录），现已改成不依赖环境。
- `scripts/check_coverage.py`：只改文案/口径说明（`--summary` 增一行 `scope=file-existence only …`），
  判定逻辑与计数未动。

### Tests

- `tests/test_imagecodec.py`：新增 9 条（分配前熵下界 + tracemalloc 峰值、多 SOF、SOS 顺序、
  PNG 流式峰值、关键/辅助块 CRC、`max_pixels<=0`、非法参数、采样因子非 1x1 的灰度 JPEG）。
- `tests/test_builtin_shell.py`：新增 3 个测试类（20 条绕过用例、包装器解包助手、
  后台白名单 / PowerShell 拒绝 / ShellTool 端到端判级）。
- `tests/test_toolrun.py`、`tests/test_agent_risk.py`：新增"缺授权回调 / 缺确认通道 /
  评估器异常"的 fail-closed 回归，其余用例补上显式放行的授权回调。
- `tests/test_websocket.py`（+307 行）、`tests/test_httpclient.py`（+216）、
  `tests/test_platform_cli_remote.py`（+148）：分片上限/让出事件循环、掩码对照、
  Sec-WebSocket-Key 400、64 位长度 1002、单帧读超时、302/307/相对 Location/跨源丢头、
  input 帧并发上限与 token 比较。
- `tests/test_app_runtime_agent.py`（+308）、`tests/test_app_doctor_capabilities.py`（新，17）、
  `tests/test_vision_threading.py`（新）、`tests/test_app_health.py`（+7）、
  `tests/test_logging.py`（+6）、`tests/test_agent_bootstrap.py`（+3）：装配报告出口、
  限流接线、hook_service、能力检查、脱敏降级、视觉线程化。
- 仓库外验证探针（均为本地脚本，不随仓库分发）：`fix_verify_mem_probe.py`（图像内存 P0）、
  `fix_verify_png_filters.py`（528 例 PNG 滤波器/位深/隔行差分，0 失败）、
  `fix_verify_jpeg_corpus.py`（126 例 Pillow JPEG 语料）、`fix_verify_shell_risk.py`
  （报告 §6.2 绕过表）。
- 测试可移植性：无 Docker、无上游 Go 仓库的本机上
  `tests.test_deploy_upgrade` + `test_deploy_watchdog` + `test_imagegen` 从 14 个失败
  变为 **109 用例 OK（skipped=2，skip 原因写在用例里）**。

---

## [0.9.7] - 2026-10-19

> 本版把 `web_search` 从「只会调 Tavily、没 key 就不可用」改成**可配置的搜索通道链**：
> 桌面端「模型」页新增「搜索服务」子页，可以按顺序排列通道、填密钥/服务地址、加自定义通道。
> 通道覆盖 Tavily / Firecrawl / Keenable / Exa / SearXNG / Antigravity CLI / Grok Build，
> 其中 Firecrawl 与 Keenable 免注册、不填 key 也能搜。工具名、参数、风险等级与 `Result`
> 文本格式都没变，配置里没有 `[web_search]` 段时行为与旧版一致（外加「配了 `EXA_API_KEY`
> 才参与」的 Exa）。

### Added

- `atri_x/tool/builtin/web_search_engines.py`：HTTP 搜索通道的请求与解析——
  Firecrawl（`POST /v2/search`，不带 `Authorization` 即免注册额度）、Keenable
  （`POST /v1/search/public` + `X-Keenable-Title`）、Exa（`POST /search` + `x-api-key`）、
  SearXNG（`GET {base_url}/search?format=json`）；单条通道失败抛 `EngineFailure`，
  由工具层换下一条；另含 `SearchEngineSpec` / `specs_from_config`（把 `[web_search]`
  配置转成通道列表）与 `default_specs`（环境变量默认链）。
- `atri_x/tool/builtin/web_search_cli.py`：外部 CLI 通道——Antigravity（`agy -p … --output-format
  json --json-schema …`）与 Grok Build（`grok -p … --always-approve`，X 搜索）。负责拼提示词、
  跑子进程（`CREATE_NO_WINDOW`，工作目录是临时目录）、解析信封、把「还在检索」的占位结果
  判为失败；CLI 不存在/未登录/超时/非零退出都转成 `EngineFailure` 再降级。
- `atri_x/config/services.py`：`WebSearchConfig`（`engine_order` + `engines`）与
  `WebSearchEngineConfig`（`protocol` / `api_key_env` / `base_url` / `bin`），接进
  `ServicesConfig`，并由 `AppConfig.merge_services` 合并。
- `atri_x/gui/searchconfig.py` + `atri_x/gui/tabs/searchpanel.py`：桌面端「模型 → 搜索服务」
  子页的数据层与界面——通道列表（顺序即降级顺序，上移/下移）、单条通道表单（协议、密钥环境变量名、
  API Key 写 `.env`、服务地址、CLI 可执行文件）、新增自定义/删除/恢复默认链；先删旧小节再写，
  自定义名不允许带点，重复名与未知协议在保存前拒绝。
- `web_search` 新增环境变量 `FIRECRAWL_API_KEY`、`KEENABLE_API_KEY`、`EXA_API_KEY`（可选）
  与 `WEB_SEARCH_ENGINES`（覆盖通道顺序，`[web_search]` 存在时也生效）。
- 默认资产：`services.toml` 里补了注释版的 `[web_search]` 示例，`.env.example` 补了搜索通道密钥位。
- 测试：`tests/test_web_search_engines.py`（42 条，覆盖五条 HTTP 通道、CLI 通道、配置解析、
  通道链与覆盖顺序）、`tests/test_config_web_search.py`（5 条，`[web_search]` 解析/合并/
  未知键拒绝）、`tests/test_gui_searchconfig.py`（11 条，读写/默认链/删除清理/校验/密钥状态）、
  `tests/test_gui_tabs.py` 的「模型」页新增 7 条搜索服务界面用例。

### Changed

- `atri_x/tool/builtin/web_search.py`：`call` 改成「按通道链逐条尝试、失败换下一条、全失败才报错」；
  每条通道归一成 `SearchOutcome` 再统一渲染，因此 Tavily 的 `Answer:` 摘要与免注册通道的
  「标题 + URL + 摘要」共用同一个输出函数（Tavily 文本仍与 Go 版逐字一致）。工具描述改成指向
  「模型 → 搜索服务」页。
- `atri_x/tool/builtin/register.py`、`atri_x/app/tool_runtime.py`、`atri_x/app/runtime.py`：
  `RegisterOptions` / `ToolRuntimeOptions` 增加 `web_search`，把配置里的 `[web_search]`
  传给 `new_web_search_tool(..., specs=...)`。
- `atri_x/gui/tabs/model.py`：「模型」页改成二级页签（LLM Provider / 模型分配、搜索服务），
  搜索服务界面拆到 `tabs/searchpanel.py` 以免单文件过长。
- `atri_x/gui/app.py`：默认窗口从 `1060x740` 放大到 `1280x860`，最小尺寸从 `900x600`
  提到 `1100x700`，给两个左右并排的配置页留出宽度。
- `docs/configuration.md` §内置 Web 工具配置：改成以 `[web_search]` TOML 为主、环境变量默认链为辅；
  `docs/windows-app.md`：更新「模型」页说明（两个子页）与「能联网搜索」的启动步骤；
  `devdocs/code-map.md`：登记新模块。

### Fixed

- `web_search` / `web_extract` 把 `Response.ok` 当方法调用（`response.ok()`）：真实
  `HTTPClient` 返回的是属性，运行到 HTTP 响应判定时必然抛 `TypeError: 'bool' object is
  not callable`。测试里的假响应恰好也写成了方法，所以一直没暴露；现在假响应改成与真实
  `Response` 一致的属性，两处调用改为属性访问。

### 发行（v0.9.7）

Windows 三种产物按 0.9.7 重新构建（`python packaging/build.py --with-kernel --release`，
产物均含内置 DSH 内核 `0.2.0-rc.2`）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.7 MB / 23862 文件 | PyInstaller 105.7 s | `--version`（`atri-x 0.9.7`）、`config check`、`-m` 转发、`sidecar found:true`、GUI `--selftest`（含逐页切换）全过 |
| `dist/portable/ATRI-X-0.9.7-portable-win64.zip` | 113.8 MB（源 318.7 MB，deflate 36%） | 21.7 s | 解压 19.7 s（23867 条目）后 `--version` / `config check` / 数据目录落在包内 / `-m` / sidecar / GUI selftest / 两个启动器 / APPDATA 重定向全过 |
| `dist/installer/ATRI-X-0.9.7-setup.exe` | 56.4 MB | ISCC 42.5 s | 静默安装 39.7 s / 23865 文件、`--version` 通过、静默卸载 13.5 s 且目录清空 |

| 文件 | SHA256 |
|---|---|
| `ATRI-X-0.9.7-portable-win64.zip` | `f6e1040f21eba9b05bb0099f5e1cd245a93f3af01c7b7ef9717505abd72ad2d4` |
| `ATRI-X-0.9.7-setup.exe` | `d67daa499bd28c86df66965fae9f9f5537607f7ea1244db59f97d383d2898ee1` |

两份产物已复制到桌面 `ATRI-X-0.9.7-Windows\` 并核对哈希。容器镜像由 `v0.9.7` tag 推送触发
Actions 重建并推送（`ghcr.io/fujiwarashigure/atri-x2:0.9.7` 与 `:latest`，digest
`sha256:7a31cbd0edef89bd764941e7366300dbe432879e51498b63fbed762a47ef1626`，run 39144389453
success，回拉冒烟 `atri-x 0.9.7`）。

### 清理与脱敏核对（v0.9.7 发行前）

- **凭据**：工作区 964 个文件按 `sk-*` / `tvly-*` / `ghp_*` / `github_pat_*` / `AKIA*` /
  `AIza*` / `xox[baprs]-*` / 私钥头扫描，命中项全部是测试夹具（`sk-abcdef…`、
  `AKIAIOSFODNN7EXAMPLE`、`user:secret-pass@example.com` 等）与文档里 `192.168.0.0/16`、
  `10.0.0.0/8` 这类私有段示例；新增代码与测试里只有 `tvly-123` 这类假值。
- **个人信息**：没有真实用户目录、主机名、邮箱、私钥入库；该公开账号名只出现在公开仓库地址与
  `ghcr.io/fujiwarashigure/atri-x2` 镜像名里。`atri_x/config/assets_data/.env.example`、
  `assets_data/plugins/.env`、`deploy/.env.example` 三个模板都只有占位/注释，没有真实密钥。
- **临时产物**：重建前删掉 `dist/` 里 0.9.6 的旧产物与 `build/pyinstaller` 中间目录；
  测试用 `tests/_tmp_*` / `tmp_*` 目录已清空，工作区只剩本次源码改动。

## [0.9.6] - 2026-10-19

> 本版修 0.9.5 的两个桌面端可用性问题，运行期行为与配置格式**没有变化**：
> 一是「进阶」页在展开本地生图挂载后会把下方的「记忆 / 群功能 / 外部事件」和
> 「定时维护任务」推出窗口、点不到，窗口拉大也救不回来；现在改成两列布局并把整页
> 放进竖向滚动容器。二是「定时维护任务」窗口里只能手写 cron、下拉预设又太死板，
> 现在改成「每天 / 每周 / 每月 / 自定义 cron」的选择式界面，实时显示生成的表达式
> 与下次触发时间。

### 本版摘要（v0.9.6）

- **「进阶」页可滚动、不再丢控件**：布局从「2 行 × 2 列」改成「左列：生图 + 运行与日志，
  右列：视觉与语音 + 记忆/群功能开关 + 定时维护任务」。展开本地生图挂载只是把左列
  撑高，不再增加整页高度；窗口被拖小时整页可以滚轮/滚动条上下滚，窗口够大时滚动条
  自动收起。
- **维护任务调度改成选择题**：周期任务先选「每天 / 每周 / 每月 / 自定义 cron」，再选
  时/分——每周还选周几、每月还选几号；窗口实时显示「生成的 cron + 下次触发时间」，
  列表里当前任务那一行也会立刻跟着变。cron 列表/区间/步长这类表达不了的写法仍可切到
  「自定义 cron」手填（带常用写法下拉）；「指定日期（一次性）」保持不变。
- 已有配置照旧兼容：能还原成结构化写法的 cron（如 `0 3 * * 1`）打开就是「每周一 03:00」，
  还原不了的（如 `0 9,21 * * *`、`0 */6 * * *`）自动落在「自定义 cron」里原样显示。

### Added

- `atri_x/gui/cronschedule.py`：界面用 cron 小工具 —— 「每天 / 每周 / 每月」结构 →
  5 段表达式（`build_cron`）、表达式 → 结构（`parse_cron`，还原不了返回 `None`）、
  中文摘要（`describe_cron`）、下次触发时间（`next_run_text`）、一次性时间解析
  （`parse_once_at` / `split_run_at`）与「周一…周日」标签互转。
- `atri_x/gui/dialogs/tasks.py`：维护任务清单（8 个任务的默认 cron / 保留期 / 提示语）
  与「表单 ↔ cron」翻译（`TaskForm` / `schedule_of` / `validate_schedule` /
  `load_schedule` / `schedule_text`），不含 Tk，便于单独测试。
- `atri_x/gui/widgets.py` 新增 `ScrollFrame`：竖向滚动容器（内容超高才显示滚动条、
  内容宽度跟随画布、只在指针位于本区域时接管滚轮）。
- `tests/test_gui_maintenance.py`（26 条）：结构化 cron 的生成/还原/摘要/下次触发、
  一次性时间与表单兜底，外加 Tk 窗口的每天/每周/每月/自定义/一次性落盘、非法输入
  不落盘、列表实时刷新、已有 cron 载入。
- `tests/test_gui_tabs.py` 新增 4 条：`ScrollFrame` 出现/隐藏滚动条与滚动生效、
  「进阶」页展开挂载后下方两块仍在可视区、窗口压小时能滚到最下面。

### Changed

- `atri_x/gui/tabs/base.py`：新增类属性 `ConfigTab.scrollable`；为 `True` 时
  `self.body` 放进 `ScrollFrame` 内容区，底部按钮/状态行仍是固定的一行。
- `atri_x/gui/tabs/advanced.py`：`scrollable = True`；布局改为左右两列（生图 +
  运行与日志 / 视觉与语音 + 开关 + 维护任务），维护任务入口仍是二级窗口按钮。
- `atri_x/gui/dialogs/maintenance.py`：重写调度表单（重复方式、周几、几号、时分的
  下拉 + 「自定义 cron」输入 + 预设 + 下次触发预览），列表当前行随编辑实时更新；
  校验与兜底逻辑移到 `dialogs/tasks.py`。
- `atri_x/gui/tabs/mountpanel.py`：提示文案缩短并放宽换行宽度，减少挂载面板占高。
- 文档同步：`docs/windows-app.md` §4.2/§4.7（9 页表格里的进阶说明、二级窗口表格、
  滚动说明）、`docs/configuration.md`（维护任务窗口的结构化写法）、`README.md`
  版本与特性行、`devdocs/code-map.md`、`atri_x/gui/__init__.py` 模块表。
- **仓库清理与脱敏**（发行前）：删掉 30 个测试残留的 `.cron-test-*` 目录、`debug.log`
  （输入法写进进程 cwd 的日志）、旧版 `build/`、`dist/` 构建残留与截图调试目录；
  测试临时目录从「仓库根 `.cron-test-*`」改到 `tests/_tmp_cron/`（被 `.gitignore`
  覆盖，中断也不会散到仓库根）；文档里指向构建机/环境的具体路径（工作区路径、内网
  环境名）改成中性占位；确认仓库与发行物不含 API Key、token、个人目录、主机名等
  敏感信息（见下方「清理与脱敏核对」）。

### 发行（v0.9.6）

Windows 两种产物按 0.9.6 重新构建（`python packaging/build.py --with-kernel --release`，
产物均含内置 DSH 内核 `0.2.0-rc.2`）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.6 MB / 23862 文件 | PyInstaller 64.3 s | `--version`（`atri-x 0.9.6`）/ `config check` / `-m` 转发 / `sidecar found:true` / GUI `--selftest`（含逐页切换）全过 |
| `dist/portable/ATRI-X-0.9.6-portable-win64.zip` | 113.7 MB（源 318.6 MB，deflate 36%） | 18.8 s | 解压 16.6 s（23867 条目）后 8 步全过，并断言配置落在解包目录 `data\appdata\ATRI-X` |
| `dist/installer/ATRI-X-0.9.6-setup.exe` | 56.3 MB | ISCC 52.1 s | 静默安装 41.0 s / 23865 文件、`--version` 通过、静默卸载 15.1 s 且目录清空 |

便携版 zip 的 SHA256：`9423cc895d83d42ec162ac9969f9df45cfd16dbd240084021ec3a1c6ec144a8e`；
安装包 SHA256：`89c59c109dbdd6ca3082e0c1badb4d104ebbec5af13fa766b305267ea0ceb172`。
两份产物已复制到桌面 `ATRI-X-0.9.6-Windows\` 并核对哈希。容器镜像由 `v0.9.6` tag 推送触发
Actions 重建并推送（`ghcr.io/fujiwarashigure/atri-x2:0.9.6` 与 `:latest`，digest
`sha256:38c29feab34c5f077eb90cc335e6f098dd7f5b97e458f6785cb402790cacea93`，回拉冒烟
`atri-x 0.9.6`）。

验证：全量测试 **5293 条 OK**（skipped 2，328.8 s）；`check_syntax` 扫描 854 个文件
0 错误 / 0 BOM / 0 CRLF；`check_imports` 681/681；`check_integration` 74/74；
源码与打包产物的 GUI 自检均通过。

### 清理与脱敏核对（v0.9.6 发行前）

- **删掉的本地垃圾**：30 个 `.cron-test-*`（测试中断残留）、`debug.log`（搜狗输入法
  写进进程 cwd 的日志）、`build/`、`dist/`（0.9.2–0.9.6 旧产物与构建日志）、
  `tmp_gui_shot/`（调试截图）、`tests/_tmp_dsh_route/`、76 个 `__pycache__/`。
- **敏感信息扫描**（工作树 + 全部 Git 历史）：API Key / `ghp_` / AKIA / xoxb 等令牌
  只命中 `tests/` 里刻意构造的假值（如 AWS 文档示例 key）；无 `C:\Users\<实际用户名>`、
  主机名、内网 IP、邮箱、SSH 私钥、代理明文口令；`app.toml` / `services.toml` 等随包
  资产只有 `api_key_env` 与占位符。
- **文档脱敏**：`packaging/README.md` 与 `devdocs/upstream-sync-todo.md` 里的构建机
  具体路径 / 环境名换成中性占位；桌面 `使用说明.txt` 同步去掉工作区绝对路径。
- **发行物复核**：解包后的 `_internal` 与内置内核里唯一的 `C:\Users\...` 来自第三方
  预编译产物（`node-pty` 的 PDB 调试信息，GitHub Actions 构建机）与 `@types/node`
  的注释示例，均非本机用户信息；未发现该公开账号名以外的个人标识（它只出现在
  项目公开仓库 URL `github.com/fujiwarashigure/...` 中）。

---

## [0.9.5] - 2026-10-19

> 本版继续打磨桌面端：日志可导出、生图支持**本地服务挂载**（端口映射 / AUTODL 等
> SSL 链接）、新增「知识库」选项卡；自学习 / HOOK 插件 / 定时维护任务改成「进阶」页
> 按钮打开的二级窗口，维护任务支持**指定日期一次性执行**；「关于」窗口补上项目简介、
> 仓库地址、推荐语与署名。运行期行为变化只有一处：维护任务新增可选 `run_at` 配置键
> （不写就完全兼容老配置）。

### 本版摘要（v0.9.5）

- **日志导出**：「日志」页新增「导出…」，把当前筛选视图（通道/日期/级别/关键字）
  存成 `.log`，文件头写明筛选条件与导出时间，UTF-8 + LF。
- **本地生图服务挂载**：「进阶 → 生图」新增「端口映射 / SSL 链接」两种挂载方式，
  自动生成 OpenAI 兼容 Base URL（`http://127.0.0.1:8188/v1`、`https://xxx.gpuhub.com/v1`），
  并有只读「检测服务」：能拉 `/models` 就是 OpenAI 兼容并记入模型目录，只认 ComfyUI
  原生接口（`/system_stats`、`/queue`）时明确提示需要转接。
- **知识库选项卡（第 9 页）**：可改 `[group_knowledge]` 全局开关与上限（每群条数、
  问题/答案/别名/关键词/匹配扫描上限），并只读列出 `state.toml` 里各群的 FAQ 条目
  （关键字筛选、选中看别名/关键词/id/更新时间）；条目增删仍用 `/faq` 命令，避免和
  服务端的 `state.toml` 回写互相覆盖。
- **三个二级设置窗口**（不新增选项卡，从「进阶」页进入）：
  - 自学习设置：保留期、候选出现次数/用户数门槛、含义/观察/上下文的字符与条数上限、挖掘超时；
  - HOOK 插件配置：直接编辑 `plugins/hooks.toml`（等宽字体、保存前 `tomllib` 校验、
    规则/插件计数），保存后 `/hooks reload` 生效；
  - 定时维护任务：8 个任务逐个开关，每个任务可选 **cron 周期**（常用预设）或
    **指定日期一次性**（`run_at`），清理类任务可改保留天数。
- **关于窗口**：项目简述、仓库地址（可点击/可复制）、好评推荐句段与
  `POWERED BY DAFEIYU` 署名。

### Added

- `atri_x/gui/imagemount.py`：本地生图挂载数据层 —— `MountSpec` / `build_base_url`
  （端口映射、SSL 链接、外部 API 三种方式规整 base_url，`/v1` 不重复追加）、
  `probe`（只读探测 `/models`、`/system_stats`、`/queue`，区分 OpenAI 兼容 / 需要鉴权 /
  ComfyUI 原生 / 连不上）。
- `atri_x/gui/tabs/knowledge.py`：「知识库」选项卡，全局配置写 `app.toml`，
  `load_entries` 读 `state.toml` 的 `[group_knowledge]`（兼容数组表与 JSON 字符串值）。
- `atri_x/gui/dialogs/`：二级设置窗口包 —— `common.Dialog`（窗口/状态行/按钮条/
  错误弹窗/操作日志）、`selflearning.SelfLearningDialog`、`hooks.HooksDialog`
  （`plugins/hooks.toml` 编辑器）、`maintenance.MaintenanceDialog`（周期/一次性调度）。
- `atri_x/gui/about.py`：「关于 ATRI-X」窗口（`DESCRIPTION` / `REPO_URL` / `RECOMMEND` /
  `POWERED_BY` 常量 + `about_text()` 纯文本）。
- `tests/test_gui_imagemount.py`（20 条）：地址拼装与非法输入、SSL/端口映射/外部 API、
  本机临时 HTTP 服务验证 OpenAI/鉴权/ComfyUI/未知/连不上五类探测。
- `tests/test_gui_tabs.py` 新增 17 条 Tk 用例：日志导出、本地生图挂载与探测落目录、
  维护任务摘要、知识库页（加载/筛选/详情/保存）、自学习/Hook/维护任务三个二级窗口
  （含非法输入不落盘）、关于文案常量。
- `tests/test_maintenance.py` 新增 1 条：`run_at` 一次性任务要带 `mode=once` 的 metadata。

### Changed

- `atri_x/gui/app.py`：选项卡 8 → 9（新增「知识库」，`Ctrl+1`…`Ctrl+9`、菜单、
  「功能」页跳转同步）；`actions.show_about` 改为打开 `about` 窗口。
- `atri_x/gui/tabs/advanced.py`：生图区新增「本地生图服务挂载」子框；维护任务的 8 个
  复选框换成「N 个任务启用」摘要 + 「定时维护任务…」按钮；新增「自学习设置…」
  与「HOOK 插件配置…」按钮。
- `atri_x/gui/tabs/logs.py`：新增「导出…」与 `export_text` / `_export_to`（纯函数，
  测试可直接断言文件内容）。
- `atri_x/gui/featureview.py` / `tabs/features.py`：「功能」页新增「群知识库（FAQ）」行
  并跳到知识库页；Hook/维护任务的说明改为指向「进阶」页的二级窗口。
- `atri_x/config/app_sections.py` + `atri_x/maintenance/maintenance.py`：维护任务配置
  新增可选 `run_at`（本地墙钟 `YYYY-MM-DD HH:MM:SS`）；`setup_cron` 在该键非空时给
  cron job 带上一次性 metadata（`schedule.mode = "once"`），到点只跑一次、跑完不再
  重复；`schedule` 仍保留做表达式校验与兜底。
- 文档同步：`docs/windows-app.md` §4 表格与 §4.2 更新为 9 页，新增 §4.6 知识库页、
  §4.7 进阶页二级设置窗口，§4.3/§4.4 补本地生图挂载与日志导出；`docs/image-generation.md`
  新增「本地生图服务（自建 / AUTODL）」；`docs/configuration.md` 补 `run_at`；
  `README.md` 版本与特性行、`devdocs/code-map.md`、模块 docstring 同步。

### 发行（v0.9.5）

Windows 两种产物按 0.9.5 重新构建（`python packaging/build.py --with-kernel --release`，
产物均含内置 DSH 内核 `0.2.0-rc.2`）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.5 MB / 23862 文件 | PyInstaller 61.1 s | `--version` / `config check` / `-m` 转发 / `sidecar found:true` / GUI `--selftest`（含逐页切换）全过 |
| `dist/portable/ATRI-X-0.9.5-portable-win64.zip` | 113.7 MB（源 318.5 MB，deflate 36%） | 24.1 s | 解压 15.9 s（23867 条目）后 8 步全过，并断言配置落在解包目录 `data\appdata\ATRI-X` |
| `dist/installer/ATRI-X-0.9.5-setup.exe` | 56.3 MB | ISCC 45.1 s | 静默安装 40.4 s / 23865 文件、`--version` 通过、静默卸载 13.8 s 且目录清空 |

便携版 zip 的 SHA256：`f1e28dd3eae6ec273dcb399f65ab5bda87e301e94a99e0bbaf4b35b6a03137ad`；
安装包 SHA256：`141f9035723e609a38e8f1c80c375d8d33a036393676402682a47ab711dc63b1`。
两份产物已复制到桌面 `ATRI-X-0.9.5-Windows\` 并核对哈希。容器镜像本轮未重建。

---

## [0.9.4] - 2026-10-19

> 本版全部是**桌面端**的可用性补丁：密钥看得见/看不见、日志有地方看、模型不用手打
> （还能起别名）、模型分配手动刷新、群友逐个建档并带头像。运行期（Agent / 后台服务）
> 行为与配置文件格式**没有变化**，老配置直接可用。

### 本版摘要（v0.9.4）

- **选项卡 6 → 8**：新增「群友」（群友角色数据）与「日志」（日志记录与显示），
  `Ctrl+1`…`Ctrl+8`、「视图」菜单、「功能」页双击跳转同步更新。
- **密钥可见性**：LLM API Key、生图密钥、三个平台的 token / client_secret 输入框默认
  掩码，右侧「显示 / 隐藏」按钮可临时看明文；保存成功后自动清空并恢复掩码。
- **模型目录**：模型页与「进阶」页生图区新增 **获取可用模型** —— `GET {base_url}/models`
  拉上游清单（`/v1` 拼法不对手自动补试），下拉点选后「加入列表」/直接切换；每个模型
  还能起**本地备注名**，下拉里显示成 `备注（真模型名）`，保存时自动还原真模型名。
- **模型分配**：新增 **刷新模型列表**（重读 `services.toml` + 模型目录，不动已选值）；
  四个槽位的模型下拉都改成带箭头、可滚动的下拉（模型名仍可手打）。
- **日志页**：运行日志 / 审计日志 / Atrinis 日志 / 后台服务输出 / 界面操作五个通道，
  按日期、级别、关键字过滤，可选条数、勾自动刷新（3 秒）、打开文件/目录、复制；
  `ERROR`/`WARN`/`DEBUG` 分别红/橙/灰着色。界面动作另外追加写 `gui/gui.log`。
- **群友页**：按群（来自聊天历史库，只读）列出群友，带**头像缩略图**；右侧编辑备注名、
  标签、角色数据（最长 4000 字）、头像（QQ 头像下载 / 本地图片 / 移除）。档案写
  `<配置目录>\gui\members.json`，头像缓存写 `<配置目录>\gui\avatars\*.png`。

### Added

- `atri_x/gui/widgets.py`：`SecretEntry`（掩码输入框 + 显示/隐藏切换）、`dropdown`
  （带箭头、可滚动的组合框、`postcommand` 即时刷新候选）、`run_async`（后台线程跑
  网络 IO，结果经 `after` 回 Tk 主线程）。
- `atri_x/gui/modelcatalog.py`：`gui/model_catalog.json` 的读写（provider 键
  `providers.<名字>`、生图键 `image_generation`）、`fetch_models`（`/models` 拉取，
  兼容 `data` / `models` / 裸数组三种返回，401/403/404 给出可操作提示）、
  `display_label` / `model_of_label`（备注名 ↔ 真模型名）、`Catalog.save` 原子写。
- `atri_x/gui/memberdata.py`：群友档案 `gui/members.json`（备注名/标签/角色数据/头像/
  时间戳，单条正文 4000 字、标签最多 12 个）、`list_scopes` / `discover_members`
  （只读打开聊天历史库，按 `platform + scope + sender` 聚合，昵称取最近一条消息）。
- `atri_x/gui/avatars.py`：QQ 头像 URL 规则、下载、**用内置图像编解码把 JPEG 转成
  96×96 PNG**（`atri_x.media.imagecodec`，不依赖 Pillow）、缓存到 `gui/avatars/`。
- `atri_x/gui/journal.py`：界面操作日志 `gui/gui.log`（一行一条"时间 + 动作"，
  512 KiB 轮转为 `.1`，写失败静默）。
- `atri_x/gui/tabs/logs.py`：「日志」选项卡（五通道 + 筛选 + 自动刷新 + 着色）。
- `atri_x/gui/tabs/members.py`：「群友」选项卡（列表 + 头像缩略图 + 档案编辑器；
  头像先缓存、点「保存档案」才写 JSON，避免误写）。
- `tests/test_gui_modelcatalog.py`（22 条）：目录读写/备注名/标签还原、`parse_models`
  三种结构、本机临时 HTTP 服务验证 `/models` 拉取与 401/404/连不上、`secret_value`。
- `tests/test_gui_members.py`（20 条）：档案增删改查与裁剪、真实 SQLite 聚合群友与昵称、
  头像 URL/JPEG→PNG/缓存与删除。
- `tests/test_gui_tabs.py`（25 条）：真建 Tk 控件跑模型目录/备注名/刷新/标签还原、
  日志五通道与筛选、群友发现/保存/头像、生图模型目录、平台密钥显示切换；无 Tk 自动跳过。

### Changed

- `atri_x/gui/app.py`：选项卡从 6 页扩到 8 页（新增「群友」「日志」），`_TAB_KEYS`
  增加 `members` / `logs`；`selftest.py` 增加"逐页切换"（让日志/功能页的 `on_show`
  自检也真跑一次）。
- `atri_x/gui/tabs/model.py`：密钥行改用 `SecretEntry`；新增「可用模型」下拉 +
  「获取可用模型」+「加入列表」+「模型备注名」子框；模型分配区新增「刷新模型列表」，
  四个槽位的下拉改为可滚动下拉，保存时把 `备注（模型）` 还原成真模型名。
- `atri_x/gui/tabs/advanced.py`：生图密钥改用 `SecretEntry`；生图模型改为可滚动下拉，
  新增「获取可用模型」「模型备注名」，保存写回真模型名。
- `atri_x/gui/tabs/platform.py`：三处 token / client_secret 改用 `SecretEntry`（同一套
  显示/隐藏与保存后清空语义），并补「重新载入」按钮。
- `atri_x/gui/configedit.py`：`ConfigPaths` 增加 `data_dir` / `sessions_db_path` /
  `chat_history_db_path` / `log_dir` 与 `gui_dir` 属性（从 `[storage]` 推，空值回落
  平台默认数据目录），供日志页与群友页只读定位文件。
- `atri_x/gui/configmodel.py`：新增 `secret_value`（进程环境优先、其次 `.env`）——
  拉模型目录真的要带密钥，界面上仍不回显明文。
- `atri_x/gui/tabs/base.py`：新增 `ConfigTab.journal()`，保存类动作统一记进 `gui.log`。
- `atri_x/gui/featureview.py` / `tabs/features.py`：「功能」页增加"群友角色数据""日志查看"
  两行并双击跳到新页；「查看日志」按钮直接切到日志页（没有该页时才退回独立窗口）。
- 文档同步：`docs/windows-app.md` §4 表格与 §4.2 更新为 8 页，新增 §4.3 模型目录与备注名、
  §4.4 日志页、§4.5 群友角色数据；`README.md` 版本与特性行同步。

### 发行（v0.9.4）

Windows 两种产物按 0.9.4 重新构建（`python packaging/build.py --with-kernel --release`，
产物均含内置 DSH 内核 `0.2.0-rc.2`）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.4 MB / 23862 文件 | PyInstaller 62.3 s | `--version` / `config check` / `-m` 转发 / `sidecar found:true` / GUI `--selftest`（含逐页切换）全过 |
| `dist/portable/ATRI-X-0.9.4-portable-win64.zip` | 113.5 MB（源 318.4 MB，deflate 36%） | 25.9 s | 解压 15.9 s（23867 条目）后 8 步全过，并断言配置落在解包目录 `data\appdata\ATRI-X` |
| `dist/installer/ATRI-X-0.9.4-setup.exe` | 56.3 MB | ISCC 43.6 s | 静默安装 41.2 s / 23865 文件、`--version` 通过、静默卸载 13.5 s 且目录清空 |

便携版 zip 的 SHA256：`d12c1f82cce5f39035adc1e739a67da089e6bbb8a24e70147e1c63ea3fa4911c`
（同目录 `.zip.sha256` 可校验）；安装包 SHA256：
`da71eddcd1222ed1ed326c7028e1571e18e86765a865056f5535cd864ea1e8df`。
安装包自检第一次被"正在运行的旧版"挡住（Inno Setup `AppMutex=ATRI-X-GUI`，属预期行为），
关掉运行中的 0.9.3 后重跑通过；两份产物已复制到桌面 `ATRI-X-0.9.4-Windows\` 并核对哈希。
容器镜像本轮未重建。

---

## [0.9.3] - 2026-10-19

> 本版把桌面端从"一个聊天窗口"扩成**选项卡式桌面端**：模型 API、人设卡、平台接入、
> 生图/语音/记忆等开关、功能总览第一次都能在界面里改，不用再手搓 TOML。
> 同时把 Windows 交付做成**两条腿**：解压即用的**便携版 zip** 与**安装包**，一条命令同时产出并各自自检。

### 本版摘要（v0.9.3）

- **6 个选项卡**：对话 / 模型 / 人设卡 / 平台 / 进阶 / 功能（`Ctrl+1`…`Ctrl+6`、「视图」菜单）。
- **模型页**：Provider 增删改（Base URL、密钥环境变量、API Key、模型列表、API 协议），
  密钥只写 `.env` 且界面只显示掩码；工作/聊天/命名/压缩四个模型槽位与默认模式写
  `state.toml`，保存后热加载。
- **人设卡页**：角色库增删改 + `profile/world/greeting/examples/image_prompt/notes`
  文档编辑 + 生图预设 + `SOUL.md` 系统人设；写入走 `atri_x.character` 的 `Store.write`，
  版本号自增与 Agent 工具链一致。
- **平台页**：QQ（OneBot v11）/ QQ 官方机器人 / Telegram 的开关、地址、token 变量与值、
  唤醒词；本地 CLI 服务端监听；各平台超级管理员。
- **进阶页**：生图 / 图片转提示词 / 视觉回退 / 语音转写、长期记忆 / 自学习 / 群分析 /
  群服务 / Atrinis、8 个定时维护任务、日志级别与保留天数。
- **功能页**：18 项能力的只读状态表（已启用/已停用/未配置 + 关键配置），双击跳到对应
  选项卡；右侧是连接状态、重启服务、配置目录与后台日志（可只读查看尾部）。
- **保存安全**：所有配置写入都走行级 TOML 补丁（保留注释与其它键）、写前留
  `<文件>.gui.bak`、写前 `tomllib` 全量校验，失败不落盘。
- **两种 Windows 交付形态**：便携版 zip（解压即用，`start-portable.bat` 把数据留在解压目录里，
  整包拷走即可复现问题）与安装包（开始菜单/自启/卸载项）；`python packaging/build.py --release`
  一条命令同时产出并各自做产物级自检。

### Added

- `atri_x/gui/tabs/`：`base.py`（选项卡公共骨架）、`chat.py`（对话，从主窗口拆出）、
  `model.py`、`character.py`、`platform.py`、`advanced.py`、`features.py`。
- `atri_x/gui/actions.py`：主窗口的「服务」动作（启动/重启/停止后台服务、打开配置目录/
  日志、关于），重启在后台线程里跑、结果回主线程展示。
- `atri_x/gui/configedit.py`：配置路径解析（`ConfigPaths`）、TOML 行级补丁
  （`patch_section` / `patch_top_level` / `remove_section`）、文本文件写入、
  `.env` 增删改。
- `atri_x/gui/configmodel.py`：Provider 视图与密钥状态（掩码）、模型分配读写。
- `atri_x/gui/featureview.py`：「功能」页的状态汇总（只读，读配置文件与目录现状）。
- `atri_x/gui/service.py`：`restart_service()`（停止 + 重新拉起 + 等就绪），供保存启动期
  配置后一键生效。
- `tests/test_gui_configedit.py`：27 条用例覆盖行级补丁/备份/校验/`.env`/Provider/
  模型分配/功能汇总。
- `packaging/build_portable.py`：把 onedir 产物打成**解压即用**的便携版 zip。包内附带
  `start-portable.bat`（`APPDATA` 指到解压目录 `data\appdata`）、`start-normal.bat`
  （标准模式）、`debug-console.bat`（doctor + 命令行窗口）、`README.txt`（UTF-8 BOM）
  与 `build-info.json`；同时输出 `.zip.sha256`。自带**解压级核验**：解压到本地短路径，
  跑 `--version` / `config check` / `-m` 转发 / `sidecar status` / GUI `--selftest` /
  `start-portable.bat --selftest`，并断言配置落在解包目录而非真实 `%APPDATA%`。
- `packaging/build.py`：新增 `--with-portable`、`--portable-verify`、`--portable-output`
  与 `--release`；`--release` 一条命令出便携版 zip + 安装包并各自自检。
- `tests/test_packaging_portable.py`：11 条用例覆盖 zip 目录契约、启动器内容、README
  BOM/CRLF、`build-info.json` 元数据、SHA256 旁注、垃圾文件过滤与缺 exe 报错。

### Changed

- `atri_x/gui/app.py`：主窗口改为"状态栏 + `ttk.Notebook` + 菜单/托盘"，聊天界面拆到
  `tabs/chat.py`；新增「视图」菜单、`Ctrl+1…6`、跨线程「重启后台服务」；配置路径在
  没有 `GuiSettings` 时按平台默认目录推算（不再阻断界面构建）。
- `atri_x/gui/tabs` 的配置页保存后会在状态行标注生效方式（热加载 / 重启后台服务），
  每个配置页带「重启后台服务」按钮。
- 文档同步：`docs/windows-app.md` §3 三步接模型改为界面优先、§4 表格补选项卡与
  「重启后台服务」、新增 §4.2 配置选项卡说明（写入哪份文件、何时生效）；本次再补
  §1 交付形态表、§2 `--release` 与新增 §7.2 便携版 zip。
- `packaging/README.md`：文件表补 `build_portable.py`、构建参数表补 4 个新开关、
  新增 §8 便携版 zip（布局、两种数据模式、自检内容、长路径限制），原 §8 排障顺延为 §9。
- `README.md`：发行版表补「Windows 便携版」一行，平台与前端补两种交付形态说明。

### 发行（v0.9.3）

Windows 两种产物按 0.9.3 重新构建（`python packaging/build.py --with-kernel --release`，
产物均含内置 DSH 内核 `0.2.0-rc.2`）：

| 产物 | 体积 | 构建耗时 | 自检 |
|---|---|---|---|
| `dist/windows/atri-x/`（onedir 中间产物） | 318.2 MB / 23862 文件 | PyInstaller 57.7 s | `--version` / `config check` / `-m` 转发 / `sidecar found:true` / GUI `--selftest` 全过 |
| `dist/portable/ATRI-X-0.9.3-portable-win64.zip` | 113.3 MB（源 318.2 MB，deflate 36%） | 22.6 s（首打）/ 10.4 s（复打） | 解压 14.9 s（23867 条目）后 8 步全过，并断言配置落在解包目录 `data\appdata\ATRI-X` |
| `dist/installer/ATRI-X-0.9.3-setup.exe` | 56.2 MB | ISCC 43.5 s | 静默安装 40.4 s / 23865 文件、`--version` 通过、静默卸载 13.9 s 且目录清空 |

便携版 zip 的 SHA256：`c60773f069e7c893649c1a2745263dd9c0dacb29758255914dce02c9050f7a49`
（同目录 `.zip.sha256` 可校验）。解压自检覆盖 `--version` / `config check` / `-m` 转发 /
`sidecar status` / GUI `--selftest` / `start-portable.bat --selftest` / `debug-console.bat`
七步；另用 Windows 自带 `Expand-Archive` 解压（等价资源管理器双击解压）后再跑
`--version` + `config check`，`app.toml` 落在解压目录的 `data\appdata\ATRI-X`。容器镜像本轮未重建。

## [0.9.2] - 2026-10-18

> 本版补齐「定时维护」闭环：会话清理默认打开，并新增**内置 DSH 内核清理**与**每日会话总结**
> 两个 Python 版新增能力。Windows 安装包与 `ghcr.io` 镜像按 0.9.2 重新构建发布。

### 本版摘要（v0.9.2）

- **每日会话总结**：`[maintenance.daily_summary]`（配置资产默认开）每天 8:00 把最近一天的 ATRI-X 会话汇总成一段文本，落盘 `<数据目录>/daily_summaries/<日期>.json`；`python -m atri_x.maintenance.summary_cli` 可查看、立即生成或输出 JSON。
- **内置 DSH 内核清理**：`[maintenance.dsh_cleanup]`（默认开，保留 30 天）只清 `DSH_HOME/sessions` 下过期的一次性会话历史与会话缓存，`profiles/` 与其它 `storages/` 内容保留。
- **会话清理默认开**：`[maintenance.session_cleanup]` 从 `enabled = false` 改为 `true`（保留 30 天），会话表与级联消息按天自动清理。
- **发行**：Windows 安装包（含内置 DSH 内核）与容器镜像随 tag 重建，安装/卸载自检数据见下面的「发行（v0.9.2）」。

### Added

- **每日会话总结**（`[maintenance.daily_summary]`，配置资产默认开启）：cron 任务 `maintenance.daily_summary` 每天 8:00 把最近一天的 ATRI-X 会话（SQLite `sessions` + `messages`）汇总成文本，落盘到 `<数据目录>/daily_summaries/<日期>.json`；`use_llm = true` 时复用 `/compact` 的压缩模型做二次摘要（失败自动回退确定性摘要），`notify = true` 时走资源报告通道投递。新增 `atri_x/maintenance/summary.py`（库）与 `atri_x/maintenance/summary_cli.py`（`python -m atri_x.maintenance.summary_cli`：查看 / `--build` 立即生成 / `--json`）。
- **内置 DSH 内核清理**（`[maintenance.dsh_cleanup]`，配置资产默认开启，保留 30 天）：cron 任务 `maintenance.dsh_cleanup` 只删 `DSH_HOME/sessions/` 下过期的一次性会话目录与会话缓存 `storages/session_projcache/sessions/`；`profiles/`、其它 `storages/` 与顶层文件一律保留。新增 `atri_x/maintenance/dsh.py`。

### Changed

- `[maintenance.session_cleanup]` 默认从 `enabled = false` 改为 `enabled = true`（保留 30 天）：会话表（含级联的消息）按天自动清理，不再需要手动打开。
- 维护任务从 7 个 handler 增加到 9 个；`Service` 新增 `dsh_home` / `daily_summary` / `summary_dir` / `summary_generator` 装配项，`setup_cron` 相应多 upsert/disable 两条任务。
- 文档同步：`docs/configuration.md`（维护段与新增两段）、`docs/reports.md`（新增「每日会话总结」）、`docs/windows-app.md`（DSH 内核清理说明）、`docs/README.md`。

### 发行（v0.9.2）

Windows 安装包按 0.9.2 重新构建（`python packaging/build.py --with-kernel --installer-verify`）：

| 项 | 实测值 |
|---|---|
| 安装包 | `ATRI-X-0.9.2-setup.exe`，**56.1 MB**（58,792,879 字节），SHA256 `6b79d8f0cdf2b20b626898a7f3d82da03bd5fe981cf9d745504cc8328d9f391a` |
| 冻结 onedir | **318.0 MB / 23862 个文件**（其中内置 DSH 内核 277.6 MB） |
| 构建耗时 | PyInstaller 约 59 s；Inno 编译 59.4 s |
| 静默安装 / 卸载 | 安装 40.4 s / 23865 个文件；卸载 13.9 s，目录清空 |
| 产物级核验 | `--version = atri-x 0.9.2`、`config check` ok、`-m` 分发 ok、内置内核 sidecar ok、GUI `--selftest` ok；新增 `-m atri_x.maintenance.summary_cli` 在冻结包内可用 |

容器镜像的 `:0.9.2` tag 由 GitHub Actions 在打 tag / 发 Release 时构建（`git push origin v0.9.2` 触发），本地未推送。

---

## [0.9.1] - 2026-10-18

> 本版是仓库的**首个 Windows 发行版**：Windows 桌面端（Tk GUI + 托盘 + 安装包）、内置 DSH 深度路由、
> GUI 会话浏览、GitHub Actions 容器镜像与发行流程都在这一版落地。与上游 Go 版的整体对应关系见
> [`PORTING.md`](PORTING.md) 与 [`devdocs/porting-status.md`](devdocs/porting-status.md)。

### 本版摘要（v0.9.1）

- **Windows 桌面端**：`atri-x-gui.exe`（Tk 窗口 + 托盘常驻 + 单实例 + 后台服务自动拉起），关窗口不退出，QQ 机器人保持在线；首次启动自动打通 `[platform.cli.server]` 与本地 token。
- **安装/分发**：Inno Setup 单文件安装包（用户级、不弹 UAC、默认开机自启）；`atri_x.dist.install` 安装/卸载/状态；内置 DSH 内核随包分发。
- **DSH 深度路由**：`call_llm` 前按任务打分 + 成本闸门 + 熔断决定是否交给内置 DSH，ACP 用量并入既有预算账本（见 [`devdocs/dsh-acp.md`](devdocs/dsh-acp.md) §6）。
- **GUI 会话浏览**：前端协议新增只读 `sessions` / `history` 帧，左侧会话栏切换复用 `/resume`。
- **发行与 CI**：`.github/workflows/release-image.yml` 在 tag 推送时构建并冒烟 `ghcr.io/fujiwarashigure/atri-x2` 镜像；容器镜像与 Windows 安装包一起随 GitHub Release 发布。
- **仓库清理与脱敏**：修正 `.gitignore` 把 `atri_x/dist/` 误当构建产物忽略的问题；删除测试/构建残留；文档里的本机绝对路径改为通用占位符。

### 从 Go 版到 Python 版（本次重写）

- **逐包重写，不留桩**：上游 71 个生产包各有 Python 对应模块，包级映射表是 [`PORTING.md`](PORTING.md) §4（唯一出处）。`cmd/atri-x` → `atri_x/__main__.py`；`internal/<pkg>/...` → `atri_x/<pkg>/...`；`deploy/*.sh` → `deploy/*.py`。
- **运行方式变了**：不再有 `go build` 产物，也不再有单静态二进制。入口是 `python -m atri_x`，在仓库根目录直接运行，解释执行、无需安装、无需构建。
- **纯标准库**：运行时零第三方依赖。Go 侧那几个第三方库由 Python 侧自研模块顶替：
  | Go 依赖 | Python 对应 |
  |---|---|
  | `github.com/coder/websocket` | `atri_x/net/websocket.py`（标准库 `socket` / `ssl` 上的 RFC 6455） |
  | `pelletier/go-toml/v2` | 读用标准库 `tomllib`，写用 `atri_x/net/tomlw.py`（`tomllib` 只读） |
  | `aws-sdk-go-v2/service/s3` | `atri_x/net/s3.py`（SigV4 最小 REST 客户端） |
  | `robfig/cron/v3` | `atri_x/net/cronspec.py` |
  | `mvdan.cc/sh/v3`（shell AST） | `atri_x/shellparse/`（**clean-room 自研**；`bashlex` 是 GPLv3+ 且已停更，与 Apache-2.0 冲突，不引入） |
  | `bubbletea` + `lipgloss` + `bubbles` | `atri_x/platform/cli/tui/`（ANSI + 原始模式输入） |
  | Go 标准库 `image` / `image/jpeg` / `image/png` | `atri_x/media/imagecodec/`（纯 Python 编解码，可选 `Pillow` 加速后端） |
  | `modernc.org/sqlite` | 标准库 `sqlite3`（随 CPython 分发的 C 扩展） |
  | `mattn/go-runewidth` | `atri_x/utils/runes.py` |
- **并发模型变了**：goroutine + channel → `asyncio` task + `asyncio.Queue`；`sync.Mutex` → `asyncio.Lock`（异步路径）或 `threading.Lock`（同步 SQLite 路径）；`context.Context` → ctx 对象 + `atri_x/safecontext.py` 的取消令牌 + `asyncio.CancelledError`。
- **错误模型变了**：Go 的 `(T, error)` 双返回 → Python 抛异常；哨兵异常（对齐 Go 的 `ErrXxx`）统一在 `atri_x/errors.py`，需要 `is` 判定的用模块级单例异常实例。
- **时间与 ID 口径统一**：`time.Time` → 带 `timezone.utc` 的 `datetime`，落库仍用 RFC3339（与 Go `time.RFC3339Nano` 兼容）；ID 仍是**带连字符的小写 UUIDv4**，与 `internal/storage/id.go` 的输出形态一致。
- **文档全量本地化**：`README.md`、`docs/**`、`devdocs/**`、`AGENTS.md` 从上游对应文件改写而来，一切 Go 专属表述（`go build`、`go test`、`gofmt`、`go.mod`、`.go` 路径、`internal/...`）替换为 Python 对应物；不复制英文镜像（上游 `docs.en/`、`README.en.md`、`CHANGELOG.en.md` 是自动翻译产物）。
- **文档里承诺的命令行接口**（与实现对接的约定，逐条见 [`devdocs/porting-status.md`](devdocs/porting-status.md) §2）：

  ```bash
  python -m atri_x                      # 自动模式
  python -m atri_x run                  # 完整前台
  python -m atri_x config check         # 配置自检
  python -m atri_x doctor               # 部署验收（--no-model / --e2e / --json / --require-platform）
  python -m atri_x --health-probe       # 健康探针（Python 版新增）
  python scripts/check_syntax.py        # 语法 + 编码体检
  python scripts/check_imports.py       # 逐模块导入校验
  python scripts/check_integration.py   # 跨模块集成一致性
  python scripts/check_coverage.py      # 按映射表算加权覆盖率
  ```

### Added

- 新增 Python 侧必需、Go 侧由标准库或第三方库直接提供的模块：`atri_x/net/`（websocket / httpclient / tomlw / s3 / cronspec）、`atri_x/shellparse/`、`atri_x/media/imagecodec/`、`atri_x/platform/cli/tui/`、`atri_x/utils/runes.py`。
- 新增 `atri_x/__main__.py` 的 `--health-probe[=URL|host:port|/path]` 开关与 `deploy/health_probe.py`：Go 上游的健康检查依赖外部 `curl`，Python 版把它做成应用内置开关，容器 `HEALTHCHECK` 与 watchdog 可直接调用。
- 新增 `atri_x/launcher/cli.py`：命令行解析与 shell 补全生成（`bash` / `zsh` / `fish` / `nushell` / `powershell` / `auto`）。
- 新增验收与体检脚本：`scripts/check_syntax.py`（语法 + BOM + CRLF + U+FFFD + mojibake）、`scripts/check_imports.py`、`scripts/check_integration.py`、`scripts/check_coverage.py`、`scripts/extract_assets.py`、`scripts/dump_hang.py`。
- 新增文档：[`devdocs/porting-status.md`](devdocs/porting-status.md)（移植规则速查、命令行接口清单、缺失模块、待核对功能点清单）与 [`AGENTS.md`](AGENTS.md)（Python 版 Agent 操作手册）。
- 新增配置资产漂移比对：`scripts/extract_assets.py --check` 对比内置配置资产与上游 `assets.go` 的差异。

### 内置 DSH（DeepSeek Harness）最小闭环

- 镜像新增 DSH 阶段（`node:24-bookworm-slim` + `@deepseek-ai/dsh@0.2.0-rc.2`）：DSH 以 `dsh --profile acp` 的 stdio 子进程形式内置，不开端口；状态根 `/data/dsh`、会话工作区 `/data/dsh-ws` 与 ATRI-X 数据同卷。运行阶段因此固定安装 `libstdc++6` 与 `ca-certificates`（node 二进制依赖），其余 apt 包仍默认不装。
- 新增 `atri_x/agent/dsh/`（纯标准库、导入无副作用）：`types`（ACP 数据类型与解析）、`transport`（NDJSON JSON-RPC 子进程传输）、`client`（`initialize` / `session/*` / `prompt` / `cancel` / `close`）、`smoke`（`python -m atri_x.agent.dsh.smoke` 闭环冒烟，打印真实 token 用量）。客户端默认**拒绝**一切权限请求——自动放行会绕过 ATRI-X 自己的风险确认。
- 新增构建参数 `NODE_BASE_IMAGE` / `DSH_VERSION` / `DSH_NPM_REGISTRY`，`.env`、compose 与 `deploy/build_push.py` 均可覆盖。
- 新增离线测试 `tests/test_dsh_acp_client.py` + `tests/_dsh_fake_acp_server.py`：不依赖 Node/dsh，覆盖握手、会话生命周期、流式事件、token 用量、权限默认拒绝、未知服务端请求、超时取消、进程崩溃与冒烟入口。
- 本阶段**只落闭环**：路由阈值、上下文交接与预算记账尚未接入对话链路，设计见 [`devdocs/dsh-acp.md`](devdocs/dsh-acp.md)。

### Windows 原生应用与 QQ OneBot 连通性

- 新增 `packaging/`（此前只有 Docker 形态的 [`deploy/windows/`](deploy/windows/README.md)，没有任何冻结/打包脚本）：`atri-x.spec`（收集 `atri_x` 全部子模块、打入 `config/assets_data` 与 `imagegen/prompts`、默认排除 Pillow/numpy/PyYAML/tkinter）、`build.py`（构建 + **产物级核验**）、`entry.py`（冻结入口，转发到 `atri_x.__main__:main`，并支持 `-m <模块>` 复刻 `python -m`）、`README.md`。产物为 onedir 目录（约 27 MB / 44 文件）或单文件 exe，运行期仍是纯标准库。
- 新增 `atri_x/platform/qq_onebot/smoke.py`（`python -m atri_x.platform.qq_onebot.smoke`，冻结后用 `atri-x.exe -m ...`）：一次跑完"连上协议端 → `get_login_info` → 可选发群/私聊 → 可选监听 N 秒上报事件"，把 QQ 链路故障压到不依赖模型的一层。它不读 `app.toml`，`--url` / `--access-token-env` 显式给；`--json` 输出 `ok` / `self_id` / `sent` / `event_count`。
- 新增离线端到端用例 `tests/test_qq_onebot_smoke.py`：用 `atri_x/net/websocket.py` 的服务端实现起一个**真的走 WebSocket** 的假 OneBot v11 协议端，覆盖动作通道、发送通道（`message_id` 回读）、事件监听、Bearer token 透传与连不上时的退出码；不依赖 QQ/NapCat，可进 CI。
- 修复 `pyproject.toml` 的 `[project.scripts]`：`atri-x` 原来指向 `atri_x.launcher.cli:main`，而该模块并没有 `main`（直接 `ImportError`），`pip install` 出来的命令是坏的；现指向真正的入口 `atri_x.__main__:main`。
- 文档新增 [`docs/windows-app.md`](docs/windows-app.md)（原生 exe 的构建、首启、接 QQ、冒烟与排障）与 [`packaging/README.md`](packaging/README.md)（构建细节、冻结限制）；`docs/README.md` 阅读顺序补一条。三种部署形态（Linux/Docker、Docker Desktop、原生 exe）共用同一份配置格式。

### Windows 桌面端（Tk GUI + 托盘 + 安装器 + 内置 DSH 内核）

- 新增 `atri_x/gui/`（纯标准库，Tk 9.0 实测）：`app.py` 主窗口（聊天气泡式记录、流式渲染、状态栏、菜单、补全）、`client.py`（WS 客户端：`hello` 握手、断线重连、鉴权失败单列状态）、`protocol.py`（帧 → 界面状态的纯逻辑，复用 `RemoteMessage`/`format_compact`，不抄第二份字段映射）、`service.py`（准备配置与 token、探测/拉起后台服务）、`process.py`（程序/状态目录、pid 记录、强制停服务、单实例）、`tray.py`（`ctypes` 调 `Shell_NotifyIconW` 的托盘，独立线程 + 隐藏窗口 + 消息循环）、`autostart.py`（`HKCU\…\Run` 开机自启）、`__main__.py`（单实例、`--minimized`、`--selftest`、`--no-service`）。
- **关窗口 ≠ 退出**：默认隐藏到托盘，Agent 继续跑在独立的 `atri-x service run` 进程里（GUI 只是 `docs/frontend-api.md` 那套 WS 协议的前端），QQ 机器人保持在线；要彻底退出用托盘/「文件」菜单，要停服务走「服务 → 停止后台服务（强制）」。
- 首次启动自动打通前后端：`app.toml` 的 `[platform.cli.server].enabled` 置 `true`（只改这一行、写前留 `.bak`、写完用 `tomllib` 重新解析校验），token 写入配置目录 `.env` 的 `ATRIX_CLI_LOCAL_TOKEN`（已有/环境变量优先生效，幂等）。这两处是 GUI 唯一会写的用户配置。
- 新增 `atri_x/dist/`：`shell_link.py`（`ctypes` 走 COM `IShellLinkW` + `IPersistFile` 建/读 `.lnk`）、`install.py`（`python -m atri_x.dist.install install|uninstall|status`）：复制发行包到 `%LOCALAPPDATA%\ATRI-X\app`、建开始菜单/桌面快捷方式、写 `HKCU` 「应用和功能」卸载项、可选 `--autostart`/`--add-path`；卸载默认**保留** `%APPDATA%\ATRI-X` 用户数据，`--purge-data` 才连数据一起删。
- 新增 `atri_x/sidecar/`（内置运行时的通用发现层）：`registry.py`（`ATRIX_RUNTIME_DIR` → 发行包 `runtime/` → `<配置目录>/runtime` 的查找顺序，`kernel.json` 清单解析）、`kernel.py`（DSH 内核定位与 `[node.exe, bin.js, --profile acp]` 命令组装，找不到就回退 PATH 上的 `dsh`）、`__main__.py`（`python -m atri_x.sidecar list|status|dsh-command|verify [--acp]`）。
- 新增 `packaging/assemble_kernel.py`：从**本机已装好的 DSH** 组装自包含内核到 `runtime/dsh-kernel/`（node.exe + 依赖闭包 + `kernel.json`）。做法是先跑 `--dump-config` 取 acp profile 引用的包、按 `dependencies` 求闭包、剪掉 Web/桌面前端包，再**真启动 ACP 服务端发 `initialize`** 核验；启动期报缺包就从源目录补拷后重试（实测 2 轮补拷 23 个包通过）。实测产物 384 包 / 189.1 MB + node.exe 92.8 MB，握手返回 `deepseek-harness-acp/0.0.1`。
- `atri_x/agent/dsh/smoke.py` 启动命令改为「显式 > `DSH_ACP_COMMAND` > 内置内核 > PATH 上的 `dsh`」，内置内核优先，因此不再需要外部 `dsh` 安装。
- `packaging/` 升级：onedir 现在出**两个 exe**（`atri-x.exe` 控制台 + `atri-x-gui.exe` 窗口版，共用 `_internal`），`entry.py` 按 exe 文件名分流；`tkinter` 不再被排除；`--with-kernel/--no-kernel`（默认按 `runtime/dsh-kernel/kernel.json` 自动判断）、`--no-gui`；产物核验新增 `-m atri_x.sidecar status` 与窗口版 `--selftest` 两项。
- 新增离线用例 66 个：`tests/test_gui_protocol.py`（记录/流式/补全/状态栏）、`tests/test_gui_client.py`（真 WebSocket 假 CLI 服务端：握手、收发、鉴权失败、断线）、`tests/test_gui_service.py`（配置定向修改、token 幂等、WS 探测）、`tests/test_sidecar_kernel.py`（发现顺序、清单校验、命令组装）、`tests/test_dist_install.py`（假注册表/假快捷方式的安装/卸载/状态）。
- 文档重写 [`docs/windows-app.md`](docs/windows-app.md)（新增桌面端、托盘/自启/安装、内置 DSH 内核三节，并更新构建产物、命令表与限制）与 [`packaging/README.md`](packaging/README.md)（双 exe、内核组装、安装器、核验清单）；`devdocs/code-map.md`、`devdocs/dsh-acp.md`、`AGENTS.md` locator 表同步。

### Windows 桌面端（第二轮）：会话列表、Inno 安装包、DSH 深度路由

- **会话列表与历史**：前端协议新增两对**只读**帧（Python 版扩展，Go 上游没有）：
  客户端 `sessions` → 服务端 `sessions_result`（id/标题/模式/最近活跃/是否当前）、
  客户端 `history` → 服务端 `history_result`（用户/助手消息 + 时间）。服务端实现复用
  `atri_x.session.Service.list_page` 与 `can_access`（新模块 `atri_x/session/browse.py`），
  不碰 Agent 上下文装载；GUI 侧新增 `atri_x/gui/sessions.py` 会话侧边栏（双击切换 =
  发 `/resume <id>` + 拉历史，`Ctrl+N`/`新建` = `/new`）。切换/鉴权/审计仍走既有命令链路。
  `docs/frontend-api.md` 增补「扩展帧：会话浏览」一节。
- **Inno Setup 安装包**：新增 `packaging/atri-x.iss` + `packaging/build_installer.py`，
  把 onedir 打成单个用户级安装包（`PrivilegesRequired=lowest`，不弹 UAC）：
  `%LOCALAPPDATA%\ATRI-X\app`、开始菜单快捷方式、默认勾选开机自启（HKCU Run `ATRI-X`，
  与 `autostart.py` 同值名）、`install.json` 标记 `"installer":"inno"`、静默安装/卸载参数、
  `--verify` 自检（本地盘 + 短路径，实测 317.9 MB onedir → 56.0 MB 安装包；安装 40.7 s /
  23865 文件、卸载 13.6 s 且清空目录）。`packaging/build.py` 新增
  `--with-installer` / `--installer-verify` / `--installer-output`。
  `atri_x.dist.install` 检测到 Inno 安装箱时会拒绝脚本卸载并提示 `unins000.exe`
  （新增 `--force` 可强删），`status` 输出新增 `installer` 字段。
- **DSH 深度路由（§6 落地）**：新增 `atri_x/agent/dsh/{command,router,handoff,runtime,route_call,status}.py`，
  在 `call_llm` 的原生调用前问一次路由：任务特征打分（代码痕迹/路径/任务动词/多步…）+
  成本闸门（`score − λ·cost_pressure`）+ 连续失败熔断；命中时把「系统设定 + 最近 N 轮 +
  当前消息」拼成一次性 prompt 交给内置内核（每次新开 DSH 会话，无状态交接），流式文本写回
  同一个 `MessageStream`，返回普通 `LLMCallResult`，因此落库/发送/状态发布全部沿用原生路径；
  ACP 真实 `usage` 走既有预算服务（`begin_chat_budget`/`settle_chat_budget`/`record_chat_usage`），
  与原生用量共享按用户/群/日额度。失败时：无输出→静默回退原生，有部分输出→保留并提示。
  新增配置段 `[agent.dsh]`（`enabled`/`min_score`/成本参数/模型/超时/历史预算/权限/熔断，
  默认值写入 `atri_x/config/assets_data/app.toml`）；新增
  `python -m atri_x.agent.dsh.status [--json] [--score 文本]` 查看内核/熔断/打分。
- 新增离线用例 68 个：`tests/test_dsh_route.py`（打分/成本闸门/熔断/交接/运行时/预算/失败回退，
  用假 ACP 服务端真跑子进程）、`tests/test_cli_sessions.py`（新帧协议往返、查询层、服务端分发、
  GUI 解码与侧边栏）；`tests/test_gui_client.py` 扩展会话帧收发；`tests/test_dist_install.py`
  扩展 Inno 标记与拒绝卸载。
- 修复 `SyncWebSocket.close` 的非幂等：重复关闭会对已停事件循环再造一个没人 await 的
  `connection.close()` 协程（`RuntimeWarning`）。
- 修复 `atri_x/sidecar/__main__.py` 的核验目录：冻结时不再写 `_internal/build/sidecar-verify`
  （会被下一次打包/安装包带进发行物），改为写到 onedir 之外的同级目录。

### 发行与持续集成（GitHub Release / ghcr.io）

- 新增 `.github/workflows/release-image.yml`：tag `v*` 推送（也支持 Release 发布与手动触发）时构建
  `deploy/Dockerfile`，推送 `ghcr.io/fujiwarashigure/atri-x2:<版本>` 与 `:latest`，随后**回拉镜像跑
  `python -m atri_x --version` 冒烟**；构建附带 provenance 与 SBOM，并尝试把包可见性设为 public。
- 新增仓库根 `.dockerignore`：排除 `.git`、`tests`、`docs`、`runtime/`（内置 DSH 内核约 277 MB）、
  `dist/`、`build/` 等，构建上下文只保留 Dockerfile 需要的 `atri_x/`、`pyproject.toml`、`README.md`。
- 发行产物：`ATRI-X-<版本>-setup.exe`（Windows 安装包）、`atri-x-<版本>-repo.tar.gz`（完整仓库归档）、
  `atri-x-<版本>-src.tar.gz`（离线源码包）、`SHA256SUMS`；后两者由
  `deploy/pack/prepare_offline.py --skip-images` 与 `git archive` 生成。

### Changed

- `go test ./...` → `python -m unittest discover -s tests -t .`；测试放 `tests/test_<模块>.py`，用标准库 `unittest`（不引入 pytest）。
- `gofmt` / `go vet` → `python scripts/check_syntax.py`（编码与语法体检）；`go build` 这一环整体消失。
- `go.mod` / `go.sum` / `GOPROXY` / `GO_VERSION` 不再存在：本项目零第三方依赖，没有模块代理与版本守卫。
- 部署脚本全部由 shell 改为 Python：`deploy/{backup,restore_verify,upgrade,rollback,init_host,build_push,health_probe,atri_x_watchdog}.py`；Windows 侧不再经过 PowerShell 调度。
- 文档里的「单个静态二进制 / 6 平台交叉编译」口径改为「需要 Python 解释器，仓库根目录直接运行」；`deploy/pack/` 的离线包提供预构建镜像 tar 而不是静态二进制。
- Go Skill（用 Go 编写的原生 Skill）**保留**：仍由 `atri_x/tool/skill/go_source.py` 调用 `gofmt` 与 Go 工具链完成格式化和编译，Skill 的 `skills/go/<name>/SKILL.atriyph` 目录约定不变。上文「不再有 `go build`」指的是**本仓库自身的构建**，不是 Skill 编译。
- `devdocs/` 里三份 Go 时代的工作台账（`upstream-sync-todo.md`、`upstream-changelog-cross-check.md`、`smoke-test-report.md`）保留原文语义与历史结论，但在文首标注「这是 Go 时代台账」，其中的 `internal/**` 包路径、`*_test.go` 用例名、`go build` / `gofmt` 都属于当时的产物。

### 收尾修复（两轮对抗性验证 + 进程级冒烟之后）

跨语言重写完成后又做了两轮对抗性验证（`verification/round2-*.md`、`verification/round3-*.md`）与一次
Python 侧进程级冒烟（真实 `python -m atri_x service run` + 运维端点探测）。下面这批缺陷的共同点是
**单元测试全绿、真实装配或真实启动才炸**，因此逐条记录修法与识别方式。

**装配层（"传了参数但没人读"/"假实现比生产少一个形参"掩盖）**

- `atri_x/app/runner.py`：`integrations.attach(ctx, req)` 与 `runtime.build(ctx, req)` 都补回 `ctx`
  （Go `integrations.go:16` / `runtime.go:42`）。此前真实启动第一行就 `TypeError`；测试里的假工件形参比
  生产少一个，所以全绿。回归用例改为直接使用生产的 `DefaultIntegrationFactory` / `RuntimeFactory` 签名。
- `atri_x/app/runtime_agent.py`：群服务常驻 `Task` 不再被 `await`（Go 是 `StartGroupServices(ctx)` 同步不等待）。
  此前 `build_agent` 会**永久挂起**，任何真实 `Deps` 都起不来。
- `atri_x/app/runtime_agent.py` + `atri_x/agent/core.py`：`Options.tool_registry` / `naming_notifier` /
  `context_overflow` / `group_services` 四个「在 app 侧被赋值、agent 侧从未读取」的字段全部接线；
  工具注册表此前静默丢弃（模型一个工具都拿不到且不报错），会话命名事件永不发出，`[group_services] enabled = false` 无效。
- `atri_x/app/tool_runtime.py`：真实装配此前只把 9 个依赖传给 `RegisterOptions`，**43 个内置工具只注册 23 个**；
  现按 Go `register.go` 逐字段接全，并补 `internal/tool/skill` 一族 6 个工具的接线（Go 顺序：先建 SkillManager 再 RegisterAll）。
  真实装配注册表 == `REGISTERED_TOOLS`（49 个）。
- `atri_x/hook/process.py`：`Environment.new(...)` → 包级 `atri_x.processenv.new(...)`（Go 里 `New` 是包级函数，不是方法）。
- `atri_x/app/runtime_support.py`：补 `ModelSelection` 导入；`_call_bool` 收敛到 `atri_x.app.support.call_bool`
  （此前裸用了一个只定义在同层 `runtime` 模块的私有名，函数体里的未定义名 import 门禁查不出来）。
- `atri_x/app/runtime.py`：`ParseRisk(raw)` → `ParseRisk(raw, fallback)`，两处 fallback 与 Go
  `security.go:50-51`（`RiskLow` / `RiskHigh`）一致。

**Agent 核心**

- `atri_x/agent/character_directive.py` / `turn_override.py`：`@char:` 角色声明与 `@turn:` 单回合覆盖的四个
  Go `Agent` 方法此前只写成模块级函数，从未绑到 `Agent`，每条正常消息都 `AttributeError` 被吞成"请求失败"；
  现以 mixin 形式挂进 `Agent`。
- `atri_x/agent/seams.py` / `core.py`：`group_tool_allowlist` 等三个方法在 `deps.group_policy is agent`
  时自递归（`RecursionError`）；现按 Go 直接实现，并让服务分派显式排除"服务就是宿主自己"。
- `atri_x/agent/chat_llm.py`：`stream.append(ctx, delta)` 补 `await`。此前增量协程被静默丢弃，
  CLI/TUI/Telegram 的流式增量永不显示、只剩终态 `replace`。

**工具与平台**

- `atri_x/tool/builtin/cron.py` + 新增 `atri_x/cron/jsonview.py`：cron CRUD 服务是 `async` 而工具层照抄 Go 的
  同步形状 → 协程被丢弃，`delete`/`disable` 什么都没做却返回 `{"ok":true}`（cron 继续触发）；现全部 `await`，
  并按 Go `json.Marshal` 形态序列化（`storage.CronJob` 没有 json tag，键即字段名）。
- `atri_x/platform/cli/adapter.py` / `remote_server.py`：`start_stream` / `set_runtime_status` 改回**同步**
  （Go 三个平台的实现都是同步的），此前本地 CLI 每轮失败、TUI 状态栏永不更新。
- `atri_x/platform/telegram/stream.py`：`replace` 里补上 `telegram_text_pages` 的延迟导入（原先是未定义名）。

**门禁与测试**

- `scripts/check_integration.py` 新增「async 函数被当同步语句调用」检查（本轮最高产的缺陷形态），
  检查项 73 → 74。
- 新增回归用例覆盖上述每条（`tests/test_agent_assembly.py`、`tests/test_app_runtime_agent.py`、
  `tests/test_app_tool_runtime_wiring.py`、`tests/test_builtin_cron.py` 等），并把多个"假实现比生产少一个形参"
  的测试替身换成生产实现。
- 验收：`check_syntax` 763 文件 0 错、`check_imports` 612/612、`check_integration` 74 项 0 失败、
  `check_coverage --strict` 72/72 包 100%、全量 `unittest` 4894 例 OK。

### 第三轮对抗性验证之后的修复（运维端点与 HTTP 语义）

第三轮验证报告（`verification/round3-summary.md`）在**真实进程 + 真实 HTTP 请求**上抓到一批
"单测全绿、真实路径才炸/才静默失效"的问题，逐条记录改法：

- `atri_x/agent/memory_learning_admin.py`：`angel_memory_count` / `self_learning_stats` 此前照 Go 把
  `ctx` 传给**同步存储**服务，而本仓签名是 `Service.count(platform, scope_id)` →
  `GET /plugins/memory` 与 `GET /plugins/learning` **恒回 500**
  （`Service.count() takes 3 positional arguments but 4 were given`）。现按本仓约定改回 2 参，
  两个端点恢复 200；方法仍保留 `ctx` 形参（调用方按 Go 形状传）。
- `atri_x/health/server.py::dispatch`：此前把查询串丢掉，`r.URL.Query()` 等价物恒空 →
  `/plugins/*` 的 `platform` / `scope_id` 过滤**静默失效**（端点照回 200，但永远是空作用域统计）。
  现解析查询串放进 `_RequestView.query`。
- 同一个 `dispatch`：新增 `_consume_awaitable()`，`extra_handlers` 里直接注册 `async def handler(w, r)`
  时协程会被驱动 —— 此前 Go 风格分支的响应体恒空（`200` + `b""`）、函数式 `async def handler(ctx)`
  分支回 `200` + `null`，进程里只留一条 `RuntimeWarning: coroutine ... was never awaited`
  （生产装配被 `LazyHTTPHandler` 包住，所以真实启动路径看不出来；两个分支的症状由故障注入逐一复现过）。
- `atri_x/health/server.py`：`dispatch` 判定顺序改为**路由 → 鉴权 → 方法**，逐字对齐 Go 的
  `ServeMux` + `requireExtraHandlerToken` + `allowGet`：`POST /nope` 由 405 改回 **404**，
  `POST /healthz`（无 token）由 405 改回 **401**；HTTP 层任意动词都进 `dispatch`
  （`HEAD` / `OPTIONS` 此前回 **501 + HTML**，Go 回 405 JSON + `Allow: GET`），`HEAD` 不写实体，
  `Server` 响应头去掉解释器版本（`atri-x-health Python/3.14.0` → `atri-x-health`）。
- `atri_x/app/ops_health.py::marshal`：**字典**不再按 `omitempty` 丢键 —— Go 的 `map` 没有 omitempty，
  `map[string]any{"count": 0}` 必须输出 `"count":0`。此前 `/plugins/*` 的 `count` / `pending` / `approved`
  在值为 0 时整键消失，与 Go 形状不符（dataclass 分支的 omitempty 语义保持不变）。
- `atri_x/app/runtime.py`：`[resident_memory]` 的 normal 写入策略改由
  `resident_normal_write_policy()` 构造，配置里的**秒**换算成 `timedelta`（Go 是
  `time.Duration(...) * time.Second`，`runtime.go:140-145`）。此前直接填 `float(...)`，而
  `NormalWritePolicy.min_interval` / `window` 是 `timedelta` → `normalized()` 抛
  `TypeError: '>' not supported between instances of 'float' and 'datetime.timedelta'`，被
  `_build_resident_store` 吞成一条 `WARN builtin runtime degraded: open resident memory store` →
  **4 个 `resident_memory*` 工具在生产路径永远注册不上**（真实启动只注册 38 个；修复后 42 个、
  `warnings=[]`，`resident_memory` / `resident_memory_read` / `resident_memory_normal` /
  `resident_memory_core` 全部在列）。回归：`tests/test_app_runtime_resident.py`（6 例，含
  "旧 float 形状必须复现 TypeError" 与"生产入口必须不留 warning"的正反两面）。
- `atri_x/agent/status.py`：排进事件循环的状态推送 Task 现在保留强引用（事件循环只持弱引用，
  官方文档要求调用方保存引用；完成即从集合移除）；同时更正该模块 docstring —— 两个 CLI
  `set_runtime_status` 本轮已改回同步，不再举它们当"async 实现"的例子。
- `atri_x/tool/builtin/register.py`：`REGISTERED_TOOLS` 的注释改准 —— "只注册
  `ALWAYS_REGISTERED_TOOLS`" 只对**全空** `RegisterOptions()` 的 `register_all` 成立（13 个）；
  最小**真实**装配 `build_tool_runtime(ToolRuntimeOptions(config_dir=...))` 是 **25** 个
  （与 Go `builtin.NewRuntime` 同义），依赖喂满才是 49。
- 回归用例：新增 `tests/test_app_ops_plugins.py`（真实 handler + 真实服务 + 真实 `dispatch`，含裸 async
  处理器与零值计数）、`tests/test_app_runtime_resident.py`（写入策略形状 + 生产入口无 warning），
  `tests/test_health.py` 新增 5 条（`POST /nope`→404、token 先于方法判定、`HEAD` 无实体、
  `OPTIONS` 405 JSON、`Server` 头无版本号），`tests/test_app_bootstrap.py` 把
  `auto` 模式用例隔离到独立运行目录并补"已有实例持标记 → CLI-only"一条（此前它会因为同机跑着
  真实 service 而变红，属测试自身依赖全局状态）。
- `deploy/stability.py`：采样窗口开启前多等一次 `/ready`（预算复用 `--startup-timeout`）。
  此前 harness 的 `healthy` 前置与 `Dockerfile` 的 `HEALTHCHECK` 一样只看三层探测（最后一层是
  TCP 连通），而冷启动的数据卷首次初始化期间 `/ready` 在容器启动后 8~14 秒仍是 503 ——
  **启动瞬态被算进了"稳定性失败率"**（真实事故：1/50 = 2% > 1% 阈值，逐条取证后全部来自第 1 次
  采样；加了等待后同一棵树复跑 0/50）。新增
  `tests/test_deploy_harness.py::TestStabilityReadyGate` 三条用例（等到 ready、预算耗尽即放弃、
  `duration=0` 的干跑不做真实 HTTP 轮询）；`deploy/TESTING.md` §3 同步说明。

### 已知差异与未完成（诚实披露）

- `atri_x/platform/qqofficial/`、`atri_x/platform/qq_onebot/`、`atri_x/platform/cli/`（含 TUI 与远程 CLI）
  均已落地；它们**没有连接过真实服务端**，端到端行为只由单元测试与本地进程冒烟覆盖。
- 画师串链路（`atri_x/artist/`）已全量落地（解析、串库、渲染、按方言搜索与 `artist_*` 工具），
  但仍未接真实画师串数据做人工核对。
- TUI 的 cell 级渲染保真度低于上游 `bubbletea`；渐进式 JPEG 不支持（明确抛异常）；`atri_x/shellparse/` 覆盖安全裁决所需的语法面，**不是**完整 POSIX shell。
- `GroupPolicyConfig.normalize()` 与上游一样**非幂等**：群作用域没有显式工具策略时
  `group_tool_allowlist` 返回空映射（等价于禁用该群全部工具）。这是上游 Go 同款行为，已在
  `tests/test_agent_bootstrap.py::test_group_allowlist_empty_policy_matches_go` 里钉住并注明出处。
- 文档中所有「上游有、Python 版待核对」的功能点集中登记在 [`devdocs/porting-status.md`](devdocs/porting-status.md) §4，未删除任何功能描述。

### Security

- 上游 Go 源码不含 copyleft 依赖；本重写同样不引入任何 copyleft 依赖 —— 这正是自研 `atri_x/shellparse/` 而不用 `bashlex`、自研 `atri_x/net/*` 而不用第三方客户端的原因。
- Shell / Go Skill / Hook 子进程的凭据变量过滤（名字含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`PRIVATE` 词段）与上游保持同一套规则，由 `atri_x/processenv/environment.py` 统一实现。
