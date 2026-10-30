# 按任务找入口（find-it）

> 用法：找到你的任务 → 按顺序改那几处 → 看"易错点" → 跑"验证"。
> 每个路径都是仓库里真实存在的文件/目录（`python scripts/check_docs.py` 会验）。
> 不熟悉仓库先读 [START-HERE.md](START-HERE.md)。

## 加一个内置工具

1. 新建 `atri_x/tool/builtin/<name>.py`（模板抄 `atri_x/tool/builtin/runtime_resources.py`：
   `Builder` 声明 + `Tool` 类 + `new_<name>_tool()`）；模块 docstring 写
   `Python 侧新增：…`（没有上游对应物时）或 `对应 Go：internal/tool/builtin/<name>.go`。
2. 注册：`atri_x/tool/builtin/register.py` —— import、`register_all()` 里的
   `registry.register(...)`、以及 `ALWAYS_REGISTERED_TOOLS`（无守卫）或 `CONDITIONAL_TOOLS`（按依赖）。
3. 包级再导出：`atri_x/tool/builtin/__init__.py` 的 import 块与"模块 ↔ Go"表。
4. **GUI 目录必须补**：`atri_x/gui/toolcatalog.py` 加一行 `ToolMeta`。
5. 测试：`tests/test_builtin_<name>.py`（照 `tests/test_builtin_runtime_resources.py`）。
6. 计数断言：`tests/test_app_tool_runtime_wiring.py` 的 `REGISTERED_TOOLS` / `ALWAYS_REGISTERED_TOOLS`
   数字、`:349` 一类的 `len(...)` 断言。
7. 生成物：`python scripts/gen_index.py`（module-map / entrypoints 自动更新）。
8. 文档：`docs/dev/find-it.md`（如需）、`docs/commands.md`（若带命令）、`CHANGELOG.md`。

**易错点**：忘了 GUI 目录 → `tests/test_gui_tabs.py` 直接红；想给所有人用就别加
`superadmin_only()`，但只读的机器信息通常应该加；群聊里若配过 `tool_allowlist` 要手加。
**验证**：`python -m unittest tests.test_builtin_<name> tests.test_app_tool_runtime_wiring tests.test_gui_tabs -v`

## 加一个平台适配器

1. 新建 `atri_x/platform/<platform>/`（包），实现 `atri_x/platform/platform.py` 的 `PlatformAdapter`
   契约（`start` / `send` / 能力位等），入口照 `atri_x/platform/qq_onebot/` 或 `headless/`。
2. 装配：`atri_x/app/platforms.py` 里注册该平台；配置段加进 `atri_x/config/app_sections.py`。
3. 平台角色/权限：`atri_x/security/` 的 `Actor` 映射写在适配器里，别在业务层判断平台。
4. 测试：`tests/test_platform_<name>.py`（用进程内假连接，不要真连服务端）。
5. 生成物：`python scripts/gen_index.py`（entrypoints 的"平台适配器"段自动收录）。
6. 文档：`docs/configuration.md` 的平台段、`devdocs/qq-backends-matrix.md`（QQ 生态时）。

**易错点**：不支持的能力要用能力位声明，不要静默降级；适配器异常必须被隔离（一个平台挂掉不能带走进程）。
**验证**：`python -m unittest tests.test_platform_<name> -v` + `check_integration.py`。

## 加一条 slash 命令

1. 新建 `atri_x/agent/commands/<name>.py`：`new_<name>(deps)` + `<Name>Module.register_commands()`，
   照 `atri_x/agent/commands/doctor.py`（仅超管）或 `rollback.py`。
2. `atri_x/agent/commands/deps.py`：`Deps` 加所需字段；`default_modules()` 列表加模块。
3. 需要服务时在 `atri_x/agent/core.py` 的 `_build_deps()` 注入。
4. 测试：`tests/test_agent_commands.py`（`EXPECTED_COMMANDS`、模块顺序断言）、
   `tests/test_agent_bootstrap.py`（命令数）。
5. 文档：`docs/commands.md` 加一节；`docs/dev/find-it.md` 无需改。

**易错点**：`Info.min_role` 留空 = **仅超管**（历史行为），要给普通用户显式写 `RoleUser`；
命令 handler 必须 `async`（有测试检查）。
**验证**：`python -m unittest tests.test_agent_commands tests.test_agent_bootstrap -v`

## 加一个配置项

1. 结构：`atri_x/config/app_sections.py`（`[app]` 下的段）或 `services.toml` 对应模块。
2. 默认值：`atri_x/config/assets.py` / `assets_data/`（首启生成的模板，用户看得到）。
3. 读取：`atri_x/config/loader.py` 的加载路径；运行期消费者在对应域。
4. 校验与提示：`atri_x/app/config_check.py` / `doctor` 检查项。
5. 测试：`tests/test_config_loader.py`、`tests/test_config_assets.py`。
6. 文档：`docs/configuration.md` 加键说明（文档里的键必须与实现一致）。

**易错点**：新增键必须进默认资产，否则老用户升级后看不到；`app.toml` 改动需要重启，
只有 `state.toml` 支持热加载。
**验证**：`python -m unittest tests.test_config_loader tests.test_config_assets -v` + `python -m atri_x config check`

## 加一个 Hook

1. 事件定义：`atri_x/hook/` 里找对应事件点（`atri_x/hook/event.py` / `atri_x/hook/manager.py`）。
2. 规则格式：`hooks.toml`，示例在 `atri_x/config/assets_data/`。
3. 测试：`tests/test_hook*.py`（规则匹配、超时、失败策略）。
4. 文档：`docs/hooks.md`。

**易错点**：Hook 失败默认不阻断主流程；要阻断必须显式声明；`hooks.toml` 的热加载边界与
`state.toml` 不同。
**验证**：`python -m unittest tests.test_hook -v`

## 加一次数据库迁移

1. 迁移实现：`atri_x/storage/sqlite/`（照现有 migration 的命名与注册方式）。
2. 模型：`atri_x/storage/models.py`；仓储：`atri_x/storage/repositories.py`。
3. 测试：`tests/test_sqlite_store.py`（含"老库原地升级"用例）。
4. 文档：`CHANGELOG.md` 注明是否破坏兼容。

**易错点**：用户不会删库——迁移必须能在已有库上原地升级；ID 是带连字符的小写 UUIDv4，
时间统一 `datetime` + RFC3339 落库。
**验证**：`python -m unittest tests.test_sqlite_store -v`

## 改系统提示词 / 人设装配

1. 来源装配：`atri_x/agent/` 的提示词模块（`prompt*` / `member_profile_prompt.py`）。
2. 优先级：群友人设 > 角色卡 > `SOUL.md`；改之前先确认覆盖顺序（1.1.2 修过装配期覆盖的 bug）。
3. 测试：`tests/test_agent_prompt*.py`、`tests/test_member_persona.py`。
4. 文档：`docs/concepts.md` / `docs/character-library.md`。

**易错点**：提示词里不要塞易变的数字（时间走时间服务注入）；群友人设按"平台 + 群 + 用户"匹配。
**验证**：`python -m unittest tests.test_agent_prompt tests.test_member_persona -v`

## 改内置提示词模板 / 标签词表

1. 默认文本：`atri_x/promptlib/defaults.py`（改它等于改"用户没覆盖时"的默认；用户覆盖在
   `<配置目录>/gui/prompts.json`，由桌面端「提示词」页维护）。
2. 渲染：`atri_x/promptlib/render.py`（只替换登记过的 `{变量}`；"整行只有一个空变量"的行丢弃）。
3. 接入：生图改写 `atri_x/app/image_rewriter.py`、识图模板 `atri_x/vision/prompt.py`、
   群友档案 `atri_x/memberprofile/prompt.py`、专属人设 `atri_x/memberprofile/persona.py`、
   表情提示 `atri_x/agent/system_prompt_sources.py`、人设卡指引 `atri_x/agent/character_guide_prompt.py`。
4. 测试：`tests/test_promptlib.py`（默认文本逐字一致、覆盖/热加载/转义）、
   `tests/test_gui_promptlib.py`（GUI 读写）。
5. 文档：`docs/configuration.md` 的「自定义提示词库与标签库」。

**易错点**：默认文本有"契约"性质（识图模板参与缓存指纹、群友档案要保证 `<member_profile>`
边界唯一），改之前先跑 `tests/test_promptlib.py`；用户可编辑的是模板，**转义与信任声明
留在代码里**，不能因为模板可配置就把边界交给用户文本。
**验证**：`python -m unittest tests.test_promptlib tests.test_gui_promptlib -v`

## 改聊天漫画 / 新增漫画工具

1. 业务域：`atri_x/comic/`（Python 侧新增，Go 上游没有）。职责划分见
   `docs/comic.md` 第 8 节；编排入口是 `service.py` 的 `ComicService`。
2. 工具：`atri_x/tool/builtin/comic.py` 的 `comic_plan` / `comic_render`；注册守卫在
   `atri_x/tool/builtin/register.py` 的 `CONDITIONAL_TOOLS["comic"]`，装配在
   `atri_x/app/tool_runtime_comic.py`（复用同一批生图 profile / 限流器 / 媒体中心）。
3. 规划模型与平台头像：`atri_x/app/comic.py`（`LLMComicPlanner` / `PlatformAvatarResolver`），
   由 `atri_x/app/runtime_support.py` 的 `comic_tool_options` 注入。
4. 配置：`atri_x/config/comic.py` 的 `ComicConfig`（`services.toml` 的 `[comic]` 段）。
5. 测试：`tests/test_comic_*.py`（模型/取材/人物/规划/提示词/服务）与
   `tests/test_builtin_comic.py`；测试假实现共用 `tests/_comic_fakes.py`。
6. 文档：`docs/comic.md`；配置键见 `docs/configuration.md` 的「聊天漫画 comic」。
7. 工具目录：`atri_x/gui/toolcatalog.py` 必须同步补 `TOOL_CATALOG`（回归用例钉住两边集合相等）。

**易错点**：台词是受保护块，不能被普通生图的改写器处理，也不能按字符硬截断；计划必须
绑定 `plan_id + revision + fingerprint`；`TimeoutError` 一类"可能已在服务端生成"的失败
只能记 `unknown`，不要自动重试；生图 profile 的 `superadmin_only` 必须在漫画服务里重新
校验（普通用户不能借 `comic_render` 绕过）。
**验证**：`python -m unittest tests.test_comic_service tests.test_builtin_comic -v`

## 加测试 / 修测试

1. 找同名测试：`ls tests/ | grep <module>`；没有就从相近模块的测试文件抄结构。
2. 覆盖不到时先补测试再改代码（本仓库要求"先用测试或最小复现证明问题"）。
3. 临时目录用 `tests/_tmp_*/`（**不要** `tempfile.mkdtemp()`）。
4. 全量：`python -m unittest discover -s tests -t .`。

**易错点**：用例数不硬编码在文档里；`skipTest` 要写清原因，"没法验证"不能当成"通过"。
**验证**：`python -m unittest tests.test_<name> -v`

## 改打包 / 发行

1. 打包脚本：`packaging/build.py`（`--release` 产出便携版 + 安装包并自检）、
   `packaging/atri-x.spec`（PyInstaller）、`packaging/atri-x.iss`（Inno Setup）。
2. 版本号：`pyproject.toml`、`atri_x/` 里的版本常量、`CHANGELOG.md`、`docs/release-notes/`。
3. 发行文档与校验和：`SHA256SUMS-<版本>.txt`、兼容性报告、自检报告。
4. 测试：`tests/test_packaging*.py`、`tests/test_packaging_qqproto.py`。

**易错点**：构建成功 ≠ 产物正确（要解包核验关键符号）；冻结产物里 `atri_x/devtools/` 也会被
`collect_submodules` 收进去，保持它零第三方依赖。
**验证**：`python -m unittest tests.test_packaging tests.test_packaging_qqproto -v`

## 改文档

1. 用户文档：`docs/*.md`；维护者入口：`docs/dev/*.md`；架构与判断：`devdocs/*.md` 与 `devdocs/adr/`。
2. **不要手改生成物**：`devdocs/generated/`、`atri_x/<域>/README.md`、`docs/dev/glossary.md`。
   要改它们就改 `atri_x/devtools/render.py` / `domaindocs.py`，然后 `python scripts/gen_index.py`。
3. 改完必跑 `python scripts/check_docs.py`（链接、内联路径、索引覆盖、锚点、行数棘轮、数字）。
4. 纯补丁式变更记 `CHANGELOG.md`；其他文档只描述**现在**正确的状态。

**易错点**：文档里写的路径必须真实存在（`check_docs` 第 2 条断言）；`CHANGELOG.md` 是历史记录，
它的旧路径不参与校验。
**验证**：`python scripts/check_docs.py` + `python scripts/gen_index.py --check`
