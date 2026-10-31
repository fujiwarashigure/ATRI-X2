<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/gui/` — 桌面端（Tk + 托盘）

## 这个域负责什么

设置界面、页签、工具权限目录与托盘；只做展示与配置读写。

**不做什么**：不在 GUI 里实现业务逻辑；它调用与 CLI 相同的那套服务。

**入口**：`atri_x/gui/app.py`（主窗口）、`atri_x/gui/tabs/`（页签）、`atri_x/gui/toolcatalog.py`。

**配置段**：`state.toml` 与各服务的 GUI 段

## 易错点

- `toolcatalog` 必须覆盖 `REGISTERED_TOOLS`（集合相等由测试钉住）。
- 页签是懒构建的；在 `__init__` 里做重活会让首启变慢。
- Tk 只能在主线程碰；后台线程更新界面要走队列/`after`。

## 模块清单（自动生成）

共 **77** 个模块，被测试覆盖 **54** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.gui` | ``atri_x.gui``：Windows 桌面端（Tk 前端 + 托盘 + 后台服务管理）。 | 76 | `test_cli_sessions.py`、`test_gui_client.py`、`test_gui_configedit.py`、`test_gui_diagnostics.py`、`test_gui_extractconfig.py`、`test_gui_icon.py`、`test_gui_imagemount.py`、`test_gui_link.py`、`test_gui_maintenance.py`、`test_gui_members.py`、`test_gui_modelcatalog.py`、`test_gui_promptlib.py`、`test_gui_proto_manager.py`、`test_gui_protocol.py`、`test_gui_qqproto.py`、`test_gui_searchconfig.py`、`test_gui_service.py`、`test_gui_tabs.py`、`test_gui_timepanel.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.__main__` | ``python -m atri_x.gui``：桌面端进程入口。 | 217 | `test_gui_diagnostics.py` |
| `atri_x.gui.about` | 「关于 ATRI-X」窗口：版本、项目简介、仓库地址、推荐语与署名。 | 139 | `test_gui_tabs.py` |
| `atri_x.gui.actions` | 主窗口的「服务」动作：启动 / 重启 / 停止后台服务、打开目录/日志、关于。 | 170 | `test_gui_diagnostics.py` |
| `atri_x.gui.app` | Tk 主窗口：选项卡（对话/模型/人设卡/平台/群友/知识库/进阶/日志/功能）+ 状态栏 + 托盘常驻。 | 860 | `test_gui_diagnostics.py`、`test_gui_tabs.py` |
| `atri_x.gui.autostart` | 开机自启：写用户注册表 ``Run`` 项（``HKCU\…\CurrentVersion\Run``）。 | 104 | — |
| `atri_x.gui.avatars` | 群友头像：拼平台头像 URL、下载、解码缩放成统一的本地 PNG 缓存。 | 180 | `test_gui_members.py`、`test_gui_tabs.py` |
| `atri_x.gui.client` | GUI 的 WS 客户端：连 ``ws://…/cli/v1/ws``、握手、收发帧、断线重连。 | 301 | `test_gui_client.py` |
| `atri_x.gui.configedit` | 桌面端的配置读写底座：路径解析、TOML 行级补丁、``.env``、文本文件。 | 678 | `test_gui_configedit.py`、`test_gui_extractconfig.py`、`test_gui_link.py`、`test_gui_maintenance.py`、`test_gui_modelcatalog.py`、`test_gui_promptlib.py`、`test_gui_proto_manager.py`、`test_gui_qqproto.py`、`test_gui_searchconfig.py`、`test_gui_tabs.py`、`test_gui_timepanel.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.configmodel` | 桌面端「模型」页的数据模型：LLM Provider 与模型分配。 | 482 | `test_gui_configedit.py`、`test_gui_modelcatalog.py` |
| `atri_x.gui.cronschedule` | 界面上的 cron 小工具：「每天 / 每周 / 每月」结构 ↔ 5 段表达式。 | 227 | `test_gui_maintenance.py` |
| `atri_x.gui.dialogs` | 「进阶」页的二级设置窗口（自学习 / HOOK 插件 / 定时维护任务）。 | 55 | `test_gui_maintenance.py`、`test_gui_tabs.py` |
| `atri_x.gui.dialogs.common` | 子设置弹窗的公共骨架：独立窗口 + 底部状态行 + 保存/重载/关闭按钮。 | 122 | — |
| `atri_x.gui.dialogs.hooks` | 「HOOK 插件配置」子窗口：直接编辑 ``plugins/hooks.toml``。 | 162 | — |
| `atri_x.gui.dialogs.maintenance` | 「定时维护任务」子窗口：编辑 ``app.toml`` 的 ``[maintenance.*]``。 | 521 | — |
| `atri_x.gui.dialogs.selflearning` | 「自学习设置」子窗口：编辑 ``app.toml`` 的 ``[self_learning]``。 | 116 | — |
| `atri_x.gui.dialogs.tasks` | 维护任务清单 + 「界面状态 ↔ cron」翻译（无 Tk，便于单独测试）。 | 180 | `test_gui_maintenance.py` |
| `atri_x.gui.extractconfig` | 桌面端「模型 → 抓取服务」页的数据层：读写 ``services.toml`` 的 ``[web_extract]``。 | 222 | `test_gui_extractconfig.py`、`test_gui_tabs.py` |
| `atri_x.gui.featureview` | 「功能」页的数据来源：把配置文件里能看出来的功能开关汇总成一张表。 | 594 | `test_gui_configedit.py`、`test_gui_tabs.py`、`test_gui_timepanel.py` |
| `atri_x.gui.icon` | 品牌图标素材的运行时解析。 | 45 | `test_gui_icon.py` |
| `atri_x.gui.imagemount` | 本地生图服务的「挂载」：把端口映射 / SSL 链接翻译成 OpenAI 兼容 Base URL。 | 357 | `test_gui_imagemount.py`、`test_gui_tabs.py` |
| `atri_x.gui.imageprofileedit` | 「提示词」页的「生图档案」子页：编辑 ``services.toml`` 的命名生图 profile。 | 317 | `test_gui_promptlib.py` |
| `atri_x.gui.journal` | 界面操作日志：把桌面端"点了什么、改了哪个文件"记进 ``<配置目录>/gui/gui.log``。 | 91 | `test_gui_diagnostics.py`、`test_gui_tabs.py` |
| `atri_x.gui.linkstate` | 「在线」选项卡的数据层：平台配置、协议端探测、后台服务健康快照。 | 573 | `test_gui_link.py`、`test_gui_qqproto.py` |
| `atri_x.gui.memberdata` | 群友角色数据：成员档案的 GUI 适配层 + 群友发现。 | 222 | `test_gui_members.py`、`test_gui_promptlib.py`、`test_gui_tabs.py` |
| `atri_x.gui.modelcatalog` | 模型目录：从上游 ``/models`` 拉可用模型，并把「备注名」缓存在本地。 | 437 | `test_gui_modelcatalog.py`、`test_gui_tabs.py` |
| `atri_x.gui.process` | 进程与路径助手：程序目录、状态目录、后台服务的 pid 记录与终止、单实例。 | 363 | `test_gui_diagnostics.py` |
| `atri_x.gui.promptedit` | 「提示词」选项卡的模板编辑面板：左分类列表 + 右正文编辑器 + 只读预览。 | 416 | `test_gui_promptlib.py` |
| `atri_x.gui.proto_manager` | 协议端管理层：按配置选后端（NapCat / SnowLuma），统一 prepare/launch/stop/status。 | 190 | `test_gui_proto_manager.py` |
| `atri_x.gui.proto_process` | 协议端进程托管：启动、停止、PID 归属校验。 | 271 | `test_gui_proto_manager.py` |
| `atri_x.gui.proto_setup` | 协议端环境描述（NapCat / SnowLuma 共用）。 | 76 | `test_gui_proto_manager.py`、`test_gui_qqproto.py` |
| `atri_x.gui.protocol` | GUI 侧的协议解析与对话模型（纯逻辑，不依赖 tkinter，可离线测试）。 | 454 | `test_cli_sessions.py`、`test_gui_diagnostics.py`、`test_gui_protocol.py` |
| `atri_x.gui.qqproto` | 内置 QQ 协议端环境（NapCat + 便携 QQ）的首次运行准备。 | 567 | `test_gui_proto_manager.py`、`test_gui_qqproto.py` |
| `atri_x.gui.qqproto_cli` | ``atri-x.exe -m atri_x.gui.qqproto`` 的命令行入口。 | 86 | — |
| `atri_x.gui.reactionpage` | 「人设卡 → 表情图库」子选项卡（Python 侧新增，无 Go 对应）。 | 553 | — |
| `atri_x.gui.searchconfig` | 桌面端「模型 → 搜索服务」页的数据层：读写 ``services.toml`` 的 ``[web_search]``。 | 249 | `test_gui_searchconfig.py`、`test_gui_tabs.py` |
| `atri_x.gui.selftest` | 桌面端自检：不连服务、不建托盘，只验证 Tk 侧能构建与渲染。 | 115 | — |
| `atri_x.gui.service` | GUI 的「服务侧」准备：配置、token、后台服务的探测与拉起。 | 638 | `test_gui_link.py`、`test_gui_qqproto.py`、`test_gui_service.py`、`test_gui_tabs.py` |
| `atri_x.gui.sessions` | 会话侧边栏：会话列表 + 新建/刷新（GUI 的"会话列表/历史"入口）。 | 156 | `test_cli_sessions.py` |
| `atri_x.gui.snowluma_proto` | SnowLuma 协议端环境的探测与托管（可选的备用协议端，外置，**不内置分发**）。 | 261 | `test_gui_proto_manager.py` |
| `atri_x.gui.tabs` | 桌面端的选项卡集合。 | 58 | `test_gui_link.py`、`test_gui_promptlib.py`、`test_gui_qqproto.py`、`test_gui_tabs.py`、`test_gui_timepanel.py` |
| `atri_x.gui.tabs.advanced` | 「进阶」选项卡：生图/视觉/语音、记忆与群功能开关、日志级别。 | 544 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.base` | 配置选项卡的公共骨架：底部状态行、错误弹窗、"需要重启"提示。 | 136 | — |
| `atri_x.gui.tabs.character` | 「人设卡」选项卡：角色库编辑 + 系统人设（SOUL.md）。 | 775 | `test_gui_promptlib.py`、`test_gui_tabs.py` |
| `atri_x.gui.tabs.chat` | 「对话」选项卡：会话列表 + 聊天记录 + 输入框。 | 165 | — |
| `atri_x.gui.tabs.extractpanel` | 「模型 → 抓取服务」子页：``web_extract`` 的通道列表、表单、顺序与密钥。 | 418 | — |
| `atri_x.gui.tabs.features` | 「功能」选项卡：功能状态一览 + 服务/日志面板。 | 219 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.knowledge` | 「知识库」选项卡：群知识库 / FAQ 的全局配置 + 现有条目查看。 | 296 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.link` | 「在线」选项卡：一条线看住「后台服务 → 协议端 → QQ 平台」三段，并提供上下线开关。 | 822 | `test_gui_link.py`、`test_gui_qqproto.py` |
| `atri_x.gui.tabs.logs` | 「日志」选项卡：把后台服务写的日志和界面操作日志放到一个可筛选的窗口里。 | 478 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.members` | 「群友」选项卡：按群管理群友的角色数据（备注名 / 标签 / 角色数据 / 头像）。 | 803 | `test_gui_promptlib.py`、`test_gui_tabs.py` |
| `atri_x.gui.tabs.model` | 「模型」选项卡：LLM Provider（含 API Key）、模型分配、搜索服务与抓取服务。 | 820 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.mountpanel` | 「进阶」页生图区的本地挂载面板（:class:`MountPanelMixin`）。 | 238 | — |
| `atri_x.gui.tabs.permission` | 「权限」选项卡：超级管理员名单 + 风险等级 / 工具轮次 / 成员建卡开关。 | 227 | — |
| `atri_x.gui.tabs.platform` | 「平台」选项卡：QQ / Telegram / 本地 CLI 服务端 / 超级管理员。 | 500 | `test_gui_tabs.py` |
| `atri_x.gui.tabs.promptlib` | 「提示词」选项卡：自定义提示词模板 / 生图档案 / 工具标签 / 标签库。 | 99 | `test_gui_promptlib.py` |
| `atri_x.gui.tabs.research` | 「研究」选项卡：查看科研助理的运行与产物（只读）。 | 326 | — |
| `atri_x.gui.tabs.searchpanel` | 「模型 → 搜索服务」子页：``web_search`` 的通道列表、表单、顺序与密钥。 | 429 | — |
| `atri_x.gui.tabs.timepanel` | 「时间」选项卡：统一时间服务（``[time]`` / ``[time.verification]`` / ``[[time.sources]]``）。 | 500 | `test_gui_timepanel.py` |
| `atri_x.gui.tabs.toolperm` | 「工具权限」选项卡：按群、按会话模式管理工具授权（``state.toml`` 的 ``[group_policy]``）。 | 583 | `test_gui_tabs.py` |
| `atri_x.gui.taglibedit` | 「提示词」页的「标签库」子页 + 群友/人设页共用的标签选择对话框。 | 318 | `test_gui_promptlib.py` |
| `atri_x.gui.timediagnostics` | 后台 ``/diagnostics`` 时间段的面板（只读，不发网络校验、不改任何配置）。 | 185 | — |
| `atri_x.gui.timesources` | ``[[time.sources]]`` 的表格 + 表单编辑器（只操作内存里的列表，不碰配置文件）。 | 309 | — |
| `atri_x.gui.timeview` | 「时间」页的数据层（无 Tk）：读 ``[time]``、算实时快照、跑时区探针、拉后台 ``/diagnostics``。 | 499 | `test_gui_timepanel.py` |
| `atri_x.gui.toolcatalog` | 桌面端「工具权限」页的工具目录：中文名、一句话功能解释、分组与风险等级。 | 544 | `test_gui_tabs.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.toolmenu` | 「工具权限」页的纵向工具菜单控件：一行一个工具（激活按钮 + 状态 + 功能解释）。 | 242 | — |
| `atri_x.gui.toolmode` | 「工具权限」页的分模式工具编辑器（chat / work 各一个）。 | 321 | — |
| `atri_x.gui.toolparams` | 「工具参数」子选项卡的控件：每个工具一组可展开 / 收起的参数面板。 | 530 | `test_gui_tabs.py` |
| `atri_x.gui.toolpolicy` | 「工具权限」页的策略读写层：群工具白名单（``state.toml`` 的 ``[group_policy]``）。 | 424 | `test_gui_tabs.py` |
| `atri_x.gui.toolpreview` | 「工具权限 → 工具进度预览」子选项卡（Python 侧新增，无 Go 对应）。 | 264 | — |
| `atri_x.gui.toolsettings` | 「工具参数」子选项卡的注册表与读写（无 Tk）。 | 261 | `test_gui_tabs.py`、`test_gui_toolsettings.py` |
| `atri_x.gui.toolsettings_base` | 工具参数的字段类型与「界面值 → TOML 值」转换。 | 255 | `test_gui_tabs.py` |
| `atri_x.gui.toolsettings_general` | 工具参数定义：「文本 / 联网 / 文件 / 记忆 / 群聊」类工具。 | 696 | — |
| `atri_x.gui.toolsettings_media` | 工具参数定义：「图片 / 画师串 / 视觉」类工具。 | 367 | — |
| `atri_x.gui.tooltagedit` | 「提示词」页的「工具标签」子页：编辑独立配置文件 ``tool_tags.toml``。 | 243 | `test_gui_promptlib.py` |
| `atri_x.gui.tray` | Windows 托盘图标（纯 ``ctypes``，零第三方依赖）。 | 523 | — |
| `atri_x.gui.widgets` | 桌面端小部件：密钥输入框、可滚动下拉框、可滚动表单区、后台任务回主线程的小工具。 | 282 | `test_gui_tabs.py` |

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 825 个 / 超线文件 79 个）。
