<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->
<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；模块清单与测试映射由代码算出。 -->

# `atri_x/tool/` — 工具与工具运行时

## 这个域负责什么

内置工具的声明（Builder/Info/Result）、执行闸门（风险确认 / 超管 / 白名单）与注册表。

**不做什么**：不在工具里做平台适配；工具只吃 `ctx` 与参数，结果回给模型。

**入口**：`atri_x/tool/builtin/register.py`（注册表）、`atri_x/toolrun/runner.py`（执行闸门）。

**配置段**：`state.toml` 的 `tool_allowlist`、`[tools]`

## 易错点

- 新增工具必须补 `atri_x/gui/toolcatalog.py` 一行，否则 `test_gui_tabs` 直接红。
- 注册表计数断言（`tests/test_app_tool_runtime_wiring.py` 的 54→55）是刻意的，改注册表就要同步改。
- 只读工具用 `RiskSafe`，不要习惯性给 `RiskLow`+owner_scoped（那等于对所有人放开）。
- 超管专属用 `Builder.superadmin_only()`：发现与执行两道闸门都会拦。

## 模块清单（自动生成）

共 **83** 个模块，被测试覆盖 **61** 个；全仓口径见 `devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。

| 模块 | 职责 | 行数 | 测试 |
|---|---|---:|---|
| `atri_x.tool` | Tool Runtime：工具契约、注册表、发现、风险准入与执行适配。 | 191 | `agent_fakes.py`、`test_agent_assembly.py`、`test_agent_chat.py`、`test_agent_cron.py`、`test_agent_cron_background.py`、`test_agent_directives.py`、`test_agent_prompt.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_background.py`、`test_builtin_character_artist.py`、`test_builtin_comic.py`、`test_builtin_cron.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_command_router.py`、`test_completion.py`、`test_diagram_pack.py`、`test_hook_runtime.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_qq_slowfetch.py`、`test_research_bootstrap.py`、`test_safety_integration.py`、`test_tool_core.py`、`test_tool_discover.py`、`test_tool_media_runtime.py`、`test_tool_runtimeinfo.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_toolrun.py`、`test_usage_attribution.py`、`test_vision_privacy.py`、`test_vision_threading.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.availability` | 工具"为什么没被采用"的机器可读原因，以及产出它的唯一判定入口。 | 114 | — |
| `atri_x.tool.builder` | 工具 schema 与元信息的声明式构造器。 | 249 | `test_tool_discover.py` |
| `atri_x.tool.builtin` | 内置工具包：ATRI-X 自带的全部工具实现与注册入口。 | 748 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_character_artist.py`、`test_builtin_comic.py`、`test_builtin_cron.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools2.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_member_profile.py`、`test_memory_notes.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_toolrun.py`、`test_vision_privacy.py`、`test_vision_threading.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.builtin.angel_memory` | Angel 长期记忆工具族：写入、召回，以及可选的删除（angel_forget）。 | 457 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.artist_extract` | ``artist_extract``：从提示词文本或图片自身的生成参数里提取画师串。 | 456 | `test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_library` | ``artist_library_*``：画师串组合的检索、读取、保存与删除。 | 817 | `test_builtin_character_artist.py` |
| `atri_x.tool.builtin.artist_tools` | 画师串工具的共享依赖、身份视图与错误文案。 | 206 | `test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_builtin_tools4.py` |
| `atri_x.tool.builtin.atriwisp_creator` | ``atriwisp_creator``：给出一份创建 Atriwisp 监听器的精简任务卡。 | 343 | `test_builtin_tools4.py` |
| `atri_x.tool.builtin.character` | ``character_*``：角色素材库的列表、读取、检索、管理与删除。 | 1117 | `test_builtin_character_artist.py`、`test_builtin_imagegen.py`、`test_member_persona.py` |
| `atri_x.tool.builtin.chat_history` | 聊天历史工具族：关键词检索、上下文窗口、引用回复。 | 598 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.comic` | 聊天漫画工具：``comic_plan``（只规划、预览）与 ``comic_render``（真正生成）。 | 435 | `test_builtin_comic.py` |
| `atri_x.tool.builtin.common` | 内置工具的公共环境读取与共享小助手。 | 249 | `test_builtin_character_artist.py`、`test_builtin_files.py`、`test_builtin_imagegen.py`、`test_builtin_tools3.py` |
| `atri_x.tool.builtin.cron` | ``cron`` / ``cron_query`` / ``cron_write`` 三个定时任务工具。 | 594 | `test_builtin_cron.py`、`test_builtin_tools3.py` |
| `atri_x.tool.builtin.fetch_message` | ``fetch_message`` 工具：按消息 id 取回引用 / 转发的完整正文。**Python 版新增**。 | 109 | `test_qq_slowfetch.py` |
| `atri_x.tool.builtin.file_guard` | 文件读写导流守卫：按路径把 read_file / edit_file 引向专用工具。 | 158 | `test_builtin_files.py`、`test_builtin_shell.py` |
| `atri_x.tool.builtin.file_manager` | 出站文件交付的准备与体积校验。 | 105 | `test_app_runtime_agent.py`、`test_builtin_tools4.py`、`test_member_profile.py` |
| `atri_x.tool.builtin.file_rollback` | ``rollback_file``：撤销 ATRI-X 最近一次 ``edit_file`` 对某个文件的修改。 | 175 | `test_builtin_files.py` |
| `atri_x.tool.builtin.file_tools` | ``read_file`` 与 ``edit_file``：带 revision 校验的文本读写。 | 605 | `test_builtin_files.py` |
| `atri_x.tool.builtin.file_tools_ast` | ``read_file`` 的 AST 搜索模式：Go / Shell 源码的标识符与函数名检索。 | 633 | — |
| `atri_x.tool.builtin.file_tools_detail` | ``edit_file`` 的人读确认详情渲染。 | 123 | — |
| `atri_x.tool.builtin.file_tools_directory` | ``read_file`` 的目录搜索模式：递归 grep（走 ripgrep）与递归 AST 检索。 | 507 | — |
| `atri_x.tool.builtin.file_tools_read` | ``read_file`` 的声明与结果渲染。 | 216 | — |
| `atri_x.tool.builtin.get_media` | ``get_media``：按当前聊天的消息 ID 与媒体序号取媒体 ID（不返回内容）。 | 226 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.group_analysis` | ``group_analysis``：统计当前群聊的消息量、活跃成员与活跃时段。 | 145 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.image_generate` | ``image_generate``：调用 OpenAI 兼容的生图端点并组装最终提示词。 | 754 | `test_builtin_imagegen.py`、`test_builtin_tools4.py`、`test_safety_integration.py` |
| `atri_x.tool.builtin.image_generate_compose` | ``image_generate`` 的提示词编译与参考图收集。 | 554 | `test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_generate_context` | ``image_generate`` 的上下文与角色解析部分。 | 464 | `test_builtin_imagegen.py` |
| `atri_x.tool.builtin.image_to_prompt` | ``image_to_prompt``：把参考图片反推成可直接用于绘图的提示词。 | 498 | `test_builtin_imagegen.py`、`test_vision_privacy.py` |
| `atri_x.tool.builtin.long_memory` | ``long_memory`` / ``long_memory_search`` / ``long_memory_write`` 三个工具。 | 315 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_model` | 长期记忆的记录模型、front matter 读写与文本处理助手。 | 528 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_scope` | Long Memory 的会话作用域：归属记录、检索隔离与跨会话操作授权。 | 148 | `test_long_memory_scope.py` |
| `atri_x.tool.builtin.long_memory_store` | 长期记忆的 SQLite 索引、文件回填与检索。 | 980 | `test_builtin_long_memory.py`、`test_long_memory_scope.py` |
| `atri_x.tool.builtin.persona` | ``persona_control``：成员自己开关「专属人设」（按人长期生效）。 | 264 | `test_member_persona.py` |
| `atri_x.tool.builtin.prompt_library` | ``prompt_library_search``：检索内置的 GPT Image 提示词库。 | 132 | `test_builtin_tools2.py` |
| `atri_x.tool.builtin.register` | 把所有已落地的内置工具装进 :class:`atri_x.tool.Registry`。 | 469 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_comic.py`、`test_builtin_imagegen.py`、`test_builtin_tools2.py`、`test_builtin_tools4.py`、`test_member_profile.py`、`test_qq_slowfetch.py` |
| `atri_x.tool.builtin.resident_memory` | 常驻记忆工具族：入口 + 读取 + 普通记忆 + 核心记忆。 | 388 | `test_builtin_tools2.py`、`test_memory_notes.py` |
| `atri_x.tool.builtin.runtime` | ``runtime.go`` 对应的入口名与配置换算。 | 151 | `test_builtin_tools4.py` |
| `atri_x.tool.builtin.runtime_resources` | ``runtime_resources``：超管专属的只读硬件资源占用查询。 | 196 | `test_builtin_runtime_resources.py` |
| `atri_x.tool.builtin.runtimeinfo` | 内置工具共享的运行时信息 —— 现在只是 :mod:`atri_x.tool.runtimeinfo` 的再导出。 | 32 | `test_app_runtime_agent.py`、`test_app_tool_runtime_wiring.py`、`test_member_profile.py` |
| `atri_x.tool.builtin.self_learning` | ``self_learning_review``：管理 clean-room 表达/黑话候选。 | 218 | `test_builtin_tools3.py` |
| `atri_x.tool.builtin.send_file` | ``send_file``：把本地文件 / file URI / HTTP(S) URL / 媒体 ID 作为产物发出。 | 264 | `test_builtin_tools3.py`、`test_safety_integration.py` |
| `atri_x.tool.builtin.shell` | ``shell`` 工具：带风险分级、目录约束与后台沙盒的执行器。 | 468 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_env` | Shell 解释器探测、子进程配置与进程树清理。 | 139 | — |
| `atri_x.tool.builtin.shell_risk` | Shell 命令的静态风险分级。 | 566 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_sandbox` | 后台沙盒下的 shell 命令风险加固。 | 484 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_syntax` | Shell 命令的字面词解析与参数判定助手。 | 138 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.shell_warnings` | Shell 命令的执行建议：把「该用专用工具」的纠偏提示回喂给模型。 | 191 | `test_builtin_shell.py` |
| `atri_x.tool.builtin.time_tool` | ``get_current_time`` 工具：给模型一个**只读**的当前时间来源。 | 200 | — |
| `atri_x.tool.builtin.view_image` | ``view_image``：把一张图片作为真实图片段交给模型。 | 424 | `test_builtin_tools3.py`、`test_vision_privacy.py`、`test_vision_threading.py` |
| `atri_x.tool.builtin.web_extract` | ``web_extract``：网页正文提取（可配置的抓取通道链）。 | 483 | `test_builtin_tools2.py`、`test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_extract_engines` | ``web_extract`` 的抓取通道。 | 402 | `test_web_extract_engines.py` |
| `atri_x.tool.builtin.web_search` | ``web_search``：网页搜索（可配置的通道链）。 | 610 | `test_builtin_tools2.py`、`test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_cli` | ``web_search`` 的外部 CLI 通道：Antigravity（``agy``）与 Grok Build（``grok``）。 | 376 | `test_web_search_engines.py` |
| `atri_x.tool.builtin.web_search_engines` | ``web_search`` 的引擎通道。 | 556 | `test_web_search_engines.py` |
| `atri_x.tool.builtin.workspace` | ``workspace`` 工具：设置当前 Session 的共享工作目录。 | 238 | `test_builtin_files.py`、`test_builtin_tools2.py`、`test_toolrun.py` |
| `atri_x.tool.capability` | 当前请求所用模型的能力声明，并充当本包共享的零依赖底座。 | 170 | `test_builtin_tools3.py`、`test_command_router.py` |
| `atri_x.tool.detail` | 工具详情的结构化块、发现结果、发现文案提供者与规则卡去重。 | 395 | `agent_fakes.py`、`test_agent_cron_background.py`、`test_tool_discover.py` |
| `atri_x.tool.discover` | ``discover_tool``：把可用工具/技能的简介或完整 schema 交给模型。 | 346 | `test_tool_discover.py` |
| `atri_x.tool.executor` | Tool Runtime 的执行适配：风险裁决、策略准入与结果回灌消息。 | 124 | — |
| `atri_x.tool.media_runtime` | 宿主侧媒体访问：显式输入声明、临时引用与沙盒内导出入。 | 389 | `test_app_tool_runtime_wiring.py`、`test_builtin_tools4.py`、`test_tool_media_runtime.py` |
| `atri_x.tool.provider` | Agent 提示用的工具/skill 名单与 schema 提供者。 | 145 | — |
| `atri_x.tool.runtimeinfo` | ``internal/tool/runtimeinfo`` 的 Python 移植。 | 105 | `test_builtin_cron.py`、`test_tool_runtimeinfo.py` |
| `atri_x.tool.sandbox` | 工具执行的轻量沙盒运行态、后台上下文与相对路径解析。 | 150 | `test_agent_cron.py`、`test_agent_cron_background.py`、`test_background.py`、`test_builtin_files.py`、`test_builtin_shell.py`、`test_builtin_tools3.py`、`test_toolrun.py`、`test_usage_attribution.py` |
| `atri_x.tool.skill` | Skill 工具包：AgentSkill / Go skill 的发现、运行与创建。 | 186 | `test_app_tool_runtime_wiring.py`、`test_builtin_tools4.py`、`test_diagram_pack.py`、`test_research_bootstrap.py`、`test_tool_skill.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py` |
| `atri_x.tool.skill.agent_manifest` | AgentSkill 清单（``ATRIX_SKILL.toml``）的读取、校验与 schema 投影。 | 349 | `test_diagram_pack.py`、`test_research_bootstrap.py` |
| `atri_x.tool.skill.agent_skill_tool` | ``agent_skill`` 工具：读写 AgentSkill 的 ``ATRIX_SKILL.toml``。 | 274 | — |
| `atri_x.tool.skill.atriyph_warnings` | 把 Atriyph 诊断拼进工具输出。 | 23 | — |
| `atri_x.tool.skill.catalog` | Skill 目录：种类、记录与线程安全目录。 | 128 | — |
| `atri_x.tool.skill.command_tool` | 可调用的 AgentSkill 工具（执行清单里的 command）。 | 207 | — |
| `atri_x.tool.skill.creator` | ``create_atrix_skill``：创建 ATRI-X 原生 Atriyph skill。 | 339 | `test_tool_skill_editor.py`、`test_tool_skill_tools.py` |
| `atri_x.tool.skill.descriptor` | 文档型 skill 的工具描述符。 | 165 | — |
| `atri_x.tool.skill.finalize_atrix_skill` | ``finalize_atrix_skill``：校验、gofmt、编译并重载。 | 204 | — |
| `atri_x.tool.skill.go_source` | Go skill 源码的规范化、校验与编译。 | 294 | `test_tool_skill.py` |
| `atri_x.tool.skill.manager` | Skill 管理器：装载、重载、删除与 AgentSkill 清单写入。 | 294 | — |
| `atri_x.tool.skill.media` | Skill 子进程的媒体交接。 | 320 | — |
| `atri_x.tool.skill.modify_atrix_skill` | ``read_atrix_skill`` / ``modify_atrix_skill``：按行读、按编辑协议改原生 skill。 | 508 | — |
| `atri_x.tool.skill.parser` | SKILL.md 的 front matter 解析与风险等级解析。 | 175 | — |
| `atri_x.tool.skill.process` | 子进程启动、超时/取消与进程树终止。 | 244 | — |
| `atri_x.tool.skill.runner` | Go skill 运行器：把 payload 写进子进程 stdin 并解析 stdout。 | 333 | — |
| `atri_x.tool.skill.scanner` | 文件系统 skill 扫描器。 | 399 | — |
| `atri_x.tool.tool` | Tool Runtime 核心类型：工具契约、风险等级、注册表与工具发现。 | 502 | `test_agent_assembly.py`、`test_agent_cron.py`、`test_agent_risk.py`、`test_agent_tools.py`、`test_app_tool_runtime_wiring.py`、`test_builtin_character_artist.py`、`test_builtin_comic.py`、`test_builtin_cron.py`、`test_builtin_imagegen.py`、`test_builtin_long_memory.py`、`test_builtin_runtime_resources.py`、`test_builtin_shell.py`、`test_builtin_tools3.py`、`test_builtin_tools4.py`、`test_hook_runtime.py`、`test_long_memory_scope.py`、`test_member_persona.py`、`test_qq_slowfetch.py`、`test_safety_integration.py`、`test_tool_discover.py`、`test_tool_skill_editor.py`、`test_tool_skill_tools.py`、`test_toolrun.py`、`test_vision_privacy.py`、`test_web_extract_engines.py`、`test_web_search_engines.py` |
| `atri_x.tool.workspace` | 工具 workspace 上下文与路径解析。 | 229 | `test_builtin_files.py`、`test_toolrun.py` |

## 对应上游 Go（自动生成）

`internal/security/security.go`、`internal/tool/availability.go`、`internal/tool/builder.go`、`internal/tool/builtin/angel_memory.go`、`internal/tool/builtin/artist_extract.go`、`internal/tool/builtin/artist_library.go`（完整反查见 `devdocs/generated/go-map.md`）

## 改这个域之前

- 读 `docs/dev/find-it.md` 里对应的任务条目；
- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），再跑 `python scripts/check_docs.py`；
- 全仓结构指标见 `scripts/complexity_budget.json`（当前模块 825 个 / 超线文件 79 个）。
