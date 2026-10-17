# 第二轮对抗性验证：`atri_x/tool/builtin/**`

- 验证对象：`atri_x/tool/builtin/**`（46 个 .py）←→ 上游 `internal/tool/builtin/**`（38 个生产 .go）
- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)；本仓：本仓库工作树（Python 3.14.7）

## 覆盖表

工具：`python tmp_verify2_cover.py internal/tool/builtin atri_x/tool/builtin`

```
Go 生产文件 38 个；未在 docstring 里声明对应的: 3 / 38
   - internal/tool/builtin/angel_forget.go
   - internal/tool/builtin/shell_process_unix.go
   - internal/tool/builtin/shell_process_windows.go
```

三条「未声明」经核对**功能有对应物**，是 docstring 漏写而非缺件：

| Go 文件 | 实际落点 | 证据 |
|---|---|---|
| `angel_forget.go`(213) | `atri_x/tool/builtin/angel_memory.py` | docstring 只声明了 `angel_memory.go`；`angel_forget` 作为**可选注册**的工具在 `register.py:117` 的 `UNPORTED_TOOLS` 里明确登记为「`AngelMemoryAllowToolForget` 打开时才有」，`angel_memory.py` 里有 `AngelForgetTool` |
| `shell_process_unix.go`(16) + `shell_process_windows.go`(14) | `atri_x/tool/builtin/shell_env.py`(103) | Go 侧是 `//go:build` 分平台两文件，Python 用 `sys.platform` 单文件合并（`shell_env.py` 里 `os.killpg` / `process.kill` 两条分支）—— 属已公开的「平台文件合并」形式差异 |
| （Python 侧多出） | `common.py`、`file_tools_read.py`、`file_tools_detail.py`、`image_generate_compose.py`、`long_memory_model.py`、`long_memory_store.py`、`shell_syntax.py`、`runtimeinfo.py`、`__init__.py` | 均为单文件拆分 / 包级再导出 |

单文件拆分对照（PORTING §2 允许，docstring 已注明）：

| Go | Python |
|---|---|
| `long_memory.go`(1181) | `long_memory.py`(244) + `long_memory_model.py`(392) + `long_memory_store.py`(803) |
| `image_generate.go`(902) | `image_generate.py`(614) + `image_generate_compose.py`(472) |
| `file_tools.go`(542) + `file_tools_ast.go`(349) | `file_tools.py`(528) + `file_tools_ast.py`(560) + `file_tools_read.py`(192) + `file_tools_detail.py`(99) |
| `character.go`(600) | `character.py`(665) |
| `chat_history.go`(407) | `chat_history.py`(498) |

**结论：文件级覆盖无缺口。**

## 符号级核对

### 工具名（模型可见面）逐个核对

工具：`tmp_verify2_tools.py` 抽 `register.py` 的 `REGISTERED_TOOLS` / `UNPORTED_TOOLS`；`tmp_verify2_toolnames.py` 反向在**全部 Go 源码**里找这个名字的字面量。

```
Python REGISTERED_TOOLS: 42
['discover_tool','workspace','resident_memory','resident_memory_read','resident_memory_normal',
 'resident_memory_core','angel_remember','angel_recall','self_learning_review','long_memory',
 'long_memory_search','long_memory_write','cron','cron_query','cron_write','send_file','get_media',
 'view_image','search_chat_history','get_chat_history_around','reply_to_chat_history_message',
 'group_analysis','web_search','web_extract','character_list','character_read','character_search',
 'character_manage','character_delete','image_generate','artist_extract','artist_library_search',
 'artist_library_read','artist_library_save','artist_library_delete','image_to_prompt',
 'prompt_library_search','read_file','edit_file','rollback_file','shell','atriwisp_creator']

Python UNPORTED_TOOLS: ('angel_forget（需 AngelMemoryAllowToolForget 打开）',
                        'skill agent/go runner → create/read/modify/finalize_atrix_skill')

== REGISTERED_TOOLS 逐名在 Go 里找 ==
找不到的: 0 / 42
```

Go 侧 `internal/tool/builtin/**` 里 `Register(` 调用中出现、Python 未注册的名字只有 `atrix_skill` —— 那是 `file_guard.go` 的 **FileGuard 规则名**（`NewAtrixSkillFileGuardRule`），不是工具。**42 个工具名与 Go 逐字一致，无遗漏、无拼错。**

### 方法与常量符号差

```
$ python tmp_verify2_symdiff.py  （全部 builtin 文件对）
!! file_tools.go      missing=3   NewEditFileTool / NewReadFileTool / UnmarshalJSON
!! character.go       missing=5   Character{Delete,List,Manage,Read,Search}Name
!! resident_memory.go missing=4   ResidentMemory{Core,Normal,Read,}ToolName
!! artist_tools.go    missing=5   Artist{Extract,LibraryDelete,LibraryRead,LibrarySave,LibrarySearch}Name
!! image_generate.go  missing=2   ImageGenerateName / NewImageGenerateTool
!! view_image.go      missing=1   ViewImageName
!! shell.go           missing=2   String / Write
!! file_rollback.go / image_generate_context.go / image_to_prompt.go / prompt_library.go / atriwisp_creator.go 各 missing=1
```

逐条核实**全部不是缺件**：
- `XxxName` 常量在 Python 侧改成了 SCREAMING_SNAKE + `_NAME`/`_TOOL_NAME` 后缀（`character.py:57 CHARACTER_DELETE_NAME`、`artist_tools.py:21 ARTIST_EXTRACT_NAME`、`resident_memory.py:38 RESIDENT_MEMORY_CORE_TOOL_NAME`、`__init__.py:118/175/543` 已再导出）—— **命名风格差异，按任务要求不计为发现**。
- `NewEditFileTool` / `NewReadFileTool` → Python 直接注册类（`register.py:258 registry.register(EditFileTool(file_guard, opts.file_backups))`）。
- `UnmarshalJSON` → 自定义 JSON 解码，Python 走 `common.py:171 decode_args`。
- `String` / `Write` → Go 的 `shellOutputBuffer`（`shell.go:349-368`）限长缓冲；`shell.py:10` 的 docstring 已注明「Go 在写入时截断但仍排空管道，Python 用…」，是**有意偏差**。

**除命名与已注明的形式差异外，符号级无缺口。**

## 发现

### P0-1 `cron_query` / `cron_write` 两个工具全部失效（漏 await → 协程被丢），`disable`/`delete` 还会**假报成功**

**Go 位置**（`Service` 全部是**同步**方法，工具直接调用）
- `internal/cron/service.go:143` `func (s *Service) Create(ctx, req UpsertRequest) (*storage.CronJob, error)`
- `internal/cron/service.go:166` `Update(...)`、`:234 Disable(...)`、`:251 Delete(...)`、`:268 Get(...) (JobView, error)`、`:288 List(...) ([]JobView, error)`
- 工具调用点：`internal/tool/builtin/cron.go:116 view, err := t.service.Get(...)`、`:122 views, err := t.service.List(...)`、`:165 if err := t.service.Disable(...)`、`:170 if err := t.service.Delete(...)`、`:194 job, err := t.service.Create(...)`

**Python 位置**（`Service` 被移植成了 `async def`，工具侧却没跟着 await）
- `atri_x/cron/service.py:239 create` / `:284 update` / `:336 disable` / `:355 delete` / `:391 list` / `get` —— **全部 `async def`**（`tmp_verify2_cron.py` 逐条打印 `async=True`）
- 工具侧：
  - `atri_x/tool/builtin/cron.py:234` `return json_result(_job_view_json(service.get(ctx, args.name, actor)))` —— 未 await
  - `atri_x/tool/builtin/cron.py:235-240` `views = service.list(ctx, ...)`，`:241 for view in views` —— 未 await
  - `atri_x/tool/builtin/cron.py:351` `service.disable(ctx, args.name, default_actor(ctx))` —— 未 await
  - `atri_x/tool/builtin/cron.py:354` `service.delete(ctx, args.name, default_actor(ctx))` —— 未 await
  - `atri_x/tool/builtin/cron.py:389` `job = service.create(ctx, UpsertRequest(...))` —— 未 await（`_create` 是**同步**函数，`:363 def _create`）
  - `atri_x/tool/builtin/cron.py:438` `job = service.update(ctx, patch)` —— 未 await（`:409 def _update`）

同一模块内其它同步/异步混用是**正确**的对照：`CronTool`（`cron.py:154`）与 `CronQueryTool.__init__` / `CronWriteTool.__init__` 都是同步构造，说明这是工具侧漏改，而不是 service 侧多改。

**证据（实跑，`tmp_verify2_cron.py`；真实 `atri_x.cron.Service` + 最小假 store + 真实 `CronWriteTool`/`CronQueryTool`）**
```
cron.Service.create   async=True
cron.Service.update   async=True
cron.Service.disable  async=True
cron.Service.delete   async=True
cron.Service.list     async=True
cron.Service.get      async=True

== CronWriteTool(create).call(...) ==
   !! TypeError: Object of type coroutine is not JSON serializable
   服务侧真实创建的 job 数 = 0

== CronQueryTool.call(...) ==
   !! TypeError: 'coroutine' object is not iterable

== CronWriteTool(delete).call(...) ==
   返回 content = '{"ok":true}'
   服务侧真实剩下的 job 数 = 0
```
stderr（关键，直接指出源码行）：
```
RuntimeWarning: coroutine 'Service.create' was never awaited
  File "atri_x\tool\builtin\cron.py", line 389
RuntimeWarning: coroutine 'Service.list' was never awaited
  File "atri_x\tool\builtin\cron.py", line 235
RuntimeWarning: coroutine 'Service.delete' was never awaited
  File "atri_x\tool\builtin\cron.py", line 354
```

**影响**（按严重度从高到低）：
1. **`operation=delete` 假报成功**：模型被告知 `{"ok":true}`，而任务**没有被删除**，cron 会继续按期触发（用户以为已经取消）。同理 `disable`。
2. `operation=create` / `update`：抛 `TypeError: Object of type coroutine is not JSON serializable`（工具彻底不可用），且**服务侧一个 job 都没建**。
3. `cron_query`：传 name 时拿 coroutine 去解 JSON，不传 name 时 `for view in coroutine` 直接 `TypeError` —— **查不了任何任务**。

也就是说：模型侧**唯一**的定时任务创建/查询/停用/删除入口整体不可用，其中两条路径还会给出与事实相反的答复。

**为什么测试全绿**：`tests/test_cron*` 面向的是 `atri_x/cron/**` 的 Service 本身（上一轮已单独验证过），`atri_x/tool/builtin/cron.py` 的两个工具类没有「用真实 `Service` 走一遍 `call()`」的用例 —— 与服务侧用例都全绿，接缝无人覆盖。

**修复建议**：把 `_create` / `_update` 改成 `async def` 并在内部 `await service.create(...)` / `await service.update(...)`；`CronWriteTool.call` 的 disable/delete 分支改成 `await service.disable(...)` / `await service.delete(...)` 后返回 `json_result({"ok": True})`；`CronQueryTool.call` 改成 `view = await service.get(...)` / `views = await service.list(...)`。回归用例请用**真实 `atri_x.cron.Service`**（配假 store）跑一遍 `CronWriteTool(service=svc).call(...)`，至少断言 `service.list(...)`（await 后）能看到新建/已删的 job。

**建议 Lead 立刻修：是。**

### P3-2 包级再导出文件 `__init__.py` 未声明任何 Go 出处
- `atri_x/tool/builtin/__init__.py`(687) 是包级再导出总表，`__all__` 里含 40+ 个符号；文件顶部 docstring 未指向具体 Go 文件
- 影响：反向按 Go 文件检索 Python 落点时，常量类符号（`*_NAME`）只在 `__init__.py` 里出现，容易漏判为缺件
- 建议：在 docstring 里写明「本文件是包级再导出，对应 Go `internal/tool/builtin/` 各文件的 `XxxName` 常量」；**不必现在修**

### P3-3（一行一条）
- `internal/tool/builtin/shell_process_{unix,windows}.go` 与 `angel_forget.go` 在 `atri_x/tool/builtin/**` 里没有 docstring 反向声明（实现分别在 `shell_env.py` 与 `angel_memory.py`）。
- `atri_x/tool/builtin/long_memory_store.py` 的 docstring 同时声明 `long_memory.go`，与其他三个 `long_memory*` 模块的声明重叠，反向检索无法唯一定位。

## 已核对无问题的高风险点

| 高风险点 | 怎么验的 | 结论 |
|---|---|---|
| **工具集是否有静默缺失** | `tmp_verify2_toolnames.py`（42 个注册名逐个在 Go 全树找字面量，0 处找不到）+ `tmp_verify2_tools.py`（`Register(` 里 Go 有 Python 没有的只有 `atrix_skill`，那是 FileGuard 规则名） | 42/42 一致；`UNPORTED_TOOLS` 明确登记了 `angel_forget`（开关控制）与 skill runner 四个工具，**没有「既没注册也没登记」的暗缺** |
| **上一轮遗留的导入失败** | `python scripts/check_imports.py` | **已修复**：`modules=610 imported=610 import_failures=0 export_failures=0`（上一轮为 `604/605`，缺 `MetadataOriginArchive`）。`artist_extract` 工具因此可正常注册 |
| **`long_memory_store._check_cancelled` 的 9 处「多传一个参数」** | `tmp_verify2_show.py atri_x/tool/builtin/long_memory_store.py:170-240` | 误报：`long_memory_store.py:180-181` 是 `@staticmethod def _check_cancelled(ctx)`，调用点 `self._check_cancelled(ctx)` 正确（探针早期未处理 `@staticmethod`） |
| **`cron.py:351/354` 之外是否还有漏 await** | `tmp_verify2_await.py`（AST，扣 `@staticmethod`/`dict.items`/`re.*` 误报）+ `tmp_verify2_discard.py`（async 函数里的裸调用）+ `tmp_verify2_asynccheck2.py`（逐个判定目标 async 性） | 除 P0-1 外**没有真实漏 await**。逐条排除：`angel_memory.py:223 self.service.delete` → `angelmemory.Service.delete` **sync**；`character.py:765 store.delete` / `character.py:576 remove_image` / `image_generate.py:507 characters.add_image` → `character.Store.delete/add_image` **sync**；`self_learning.py:159/171 service.decide/undo` → **sync**；`resident_memory.py` 的 `append_normal/write_normal/delete_normal/write_core` → 本类方法，sync；`file_guard.py:67` / `file_tools_ast.py:240` / `shell_sandbox.py:152` / `chat_history.py:397` 的 `match(2 pos)` → `re.match`；`long_memory_store.py` 全部 `execute()` → `sqlite3.Connection.execute` |
| **安全性相关工具是否被削弱（路径逃逸 / 风险判定 / 沙箱）** | `tmp_verify2_symdiff.py` 对 `shell_risk.go`、`shell_sandbox.go`、`file_guard.go`、`workspace.go`、`shell_warnings.go`、`file_manager.go` 逐符号比对；读 `shell.py:1-30` 的有意偏差声明 | 六个文件 `missing=0`（`shell.go` 的 `String`/`Write` 是 Go `shellOutputBuffer` 的内部方法，`shell.py:10` 已声明为有意偏差）；未发现安全判定被跳过或降级 |
| **`decode_args` 的入参契约** | `common.py:171-190` + 探针中被 `json.loads(dict)` 打中 | 契约是「JSON 字符串」，与 Go `json.Unmarshal([]byte, ...)` 一致；空入参返回零值对象（Go 同）；**不是缺陷**（本次探针一度误判，已修正后重跑） |
| **`json_result` 的 Go 序列化复刻** | `common.py:193-214`：`ensure_ascii=False` + `separators=(",",":")` + `sort_keys=True` + `<>&` 转义 | 与 Go `json.Marshal` 的紧凑输出/HTML 转义/键排序一致（属已公开的有意偏差范围）；`json_result({'ok': True})` 独立跑出 `Result(content='{"ok":true}')` 正确 |
| **`register_all` 的条件注册分支** | `register.py:200-300` 与 `internal/tool/builtin/register.go:57-200` 逐段对照 | 分支条件一致（`resident_memory_store is not None`、`angel_memory is not None`、`long_memory_dir != ""`、`cron_service is not None`、`file_manager is not None`、`chat_history is not None`、`character_store.enabled()`、`len(image_profiles) > 0`、`artists.enabled()` …）；`file_guard` 规则的三个 `AddRule` 也一一对应 |

## 本次用到的探针（均已删除）

`tmp_verify2_cover.py`、`tmp_verify2_symdiff.py`、`tmp_verify2_tools.py`、`tmp_verify2_toolnames.py`、`tmp_verify2_cron.py`、`tmp_verify2_await.py`、`tmp_verify2_discard.py`、`tmp_verify2_asynccheck2.py`、`tmp_verify2_awaitcheck.py`、`tmp_verify2_show.py`
