# ATRI-X2-Py 对抗性验证报告（四个冻结集群）

本目录是**独立对抗性验证**的产出：验证者不是任何被验证模块的实现者，任务是拿上游 Go 源码当唯一真相，
逐条核对 Python 实现，找出**真实的行为分歧、静默失效、边界错误**。

- 上游（只读真相）：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（Go）
- 本仓（只读验证对象）：本仓库工作树（Python 3.14.7，纯标准库）
- 覆盖集群：`contextmgr`、`toolrun`、`session`、`storage`（含 `sqlite/`）
- 契约出处：`PORTING.md`（§1 硬约束 / §2 风格 / §3 共享词汇表 / §4 包映射 / §7 环境陷阱 / §8 验收工具）

## 报告索引

| 报告 | 集群 | 一句话结论 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|---|
| [contextmgr.md](contextmgr.md) | `atri_x/contextmgr/**` ←→ `internal/contextmgr/**` | 移植忠实度最高：两个系统提示词与 Go **逐字节一致**，预算/分片/压缩流程逐行对齐；唯一实质分歧是 `WindowResolver` 的 provider 元数据分支在实际调用链上不可达（冷缓存时窗口取值与 Go 不同）。 | 0 | 0 | 1 | 5 |
| [toolrun.md](toolrun.md) | `atri_x/toolrun/**` ←→ `internal/toolrun/**` | 解析/白名单/编排顺序全对，但**确认阶段被拒绝的工具调用被记成成功**（`call_err=None` → `success=1`），并污染"成功过的工具调用"判定。 | 0 | 1 | 1 | 3 |
| [session.md](session.md) | `atri_x/session/**` ←→ `internal/session/**` | 生命周期/过期/模式/查询/Fork 逐行一致，但**自动会话命名完全失效**：`TitleGenerator` 的 Python 实现是协程且要 ctx，而调用点只传 1 个参数 → 每次命名都是 TypeError，被记成 `llm_error` 静默降级。 | 1 | 0 | 0 | 2 |
| [storage.md](storage.md) | `atri_x/storage/**` ←→ `internal/storage/**` | **17 条 migration 逐字节一致**、全部 SQL 语句文本一致、10 个事务点一一对应、13 个 interface 的 83 个方法全有对应物；只找到 3 处 P3 级的解析严格度/文档差异，没有数据损坏类问题。 | 0 | 0 | 0 | 3 |
| **合计** | | | **1** | **1** | **2** | **13** |

## P0 / P1 速查

| 级别 | 标题 | Python 位置 | Go 位置 | 一句话影响 |
|---|---|---|---|---|
| P0 | 自动会话命名完全失效（协程 + 参数个数双重不匹配） | `atri_x/session/naming.py:136`、`atri_x/agent/title.py:48`、`atri_x/session/types.py:112-118`、装配点 `atri_x/agent/core.py:218-224` | `internal/session/naming.go:81`、`internal/session/types.go:57-59`、`internal/agent/title.go:27` | 模型生成的会话标题永远不会落库，每次尝试都以 `TypeError` 计一次"LLM 失败"，累计 3 次后落到首条消息的 fallback 标题。 |
| P1 | 确认被拒绝的工具调用被记成 `success=1` | `atri_x/toolrun/runner.py:283-306` + `atri_x/toolrun/toolrun.py:458-519`（拒绝分支不返回错误） | `internal/toolrun/runner.go:140-155`、`internal/toolrun/toolrun.go:251-283` | 超管门槛/风险门槛拒绝的调用在审计表里记为成功、`error=NULL`，并被 `SuccessfulIDs` 当成"成功过的工具调用"进入压缩上下文。 |

## 方法论（每个集群都照此执行）

1. **文件级覆盖表**：用 `glob`/目录遍历列出两边**全部生产文件**（排除 `_test.go`），一 Go 文件 → 一 Python 模块；
   不对齐的必须解释（已公开拆分 / Python 额外再导出文件 / 整体缺失）。
2. **符号级核对**：Go 的导出类型/函数/方法/常量 → Python 是否有对应物、名字可否交叉检索、签名是否等价。
   工具：`python scripts/list_api.py <module>`、`scripts/check_imports.py`，以及本次自写的
   `tmp_verify_symbols.py`（自动把 Go `func` 名转 snake_case 后与 Python `def` 名求差）与
   `tmp_verify_storage_ifaces.py`（逐个 interface 方法求差）。**结论以读源码为准，自动比对只用来定位。**
3. **语义核对**（重点）：
   - `(T, error)` → 抛异常 vs 返回错误结果对象（本仓有意的例外：`tool.Executor.execute` 返回带 `err` 的结果对象）；
   - `omitempty` 逐字段复刻（指针 nil 省略、非指针零值仍输出、`time.Time` 零值序列化成 `"0001-01-01T00:00:00Z"`）；
   - `sync.Mutex` → `threading.Lock` 是否在 async 路径上持锁阻塞事件循环；
   - 时间是否带时区、精度是否与 Go 对齐（本仓微秒 / Go 纳秒；**已公开的有意偏差**）；
   - 分页/游标边界（`limit <= 0`、空列表、`offset` 越界）；
   - 排序键是否与 Go 逐字一致、是否稳定；
   - 哨兵错误：`except ErrX`（实例）会 TypeError —— 全仓只读扫了一遍，四个集群内**没有**这种写法
     （`rg -n "except (Err|ErrNotFound|...)" atri_x` 只命中注释，`scripts/check_integration.py` 已有守卫）。
4. **每条结论都有可复现证据**：Go 文件:行 + Python 文件:行 + 实际跑过的命令与输出。
   本机**没有 Go 工具链**（`go version` → CommandNotFoundException），所以 Go 侧结论一律是读源码；
   Python 侧结论一律是实跑（探针 / `unittest`），不接受"看起来应该"。
5. **严重度**：`P0` 静默错误/数据损坏/安全问题；`P1` 真实使用会撞到的行为分歧；
   `P2` 边界/文案/性能分歧；`P3` 风格/可读性/文档。每条都给修复建议与"是否建议现在修"。
6. **不重复劳动**：已公开记录的有意偏差（统一微秒、JSON 不转义 `<>&`、Python `re` 替代 RE2、
   `Agent` 用 mixin+installer、单文件拆分、命名 snake_case 化）**不计为发现**，除非证明它造成实际错误。
   "测试覆盖率低"也不计为发现，除非指名某个有真实出错可能的具体分支。

## 本次用到的探针与命令（均已从仓库根删除）

| 探针 | 用途 | 关键输出 |
|---|---|---|
| `tmp_verify_session_naming.py` | 用**真实** `agent.title.TitleGenerator` 走 `maybe_schedule_naming` | `failure ... TypeError: ... missing 1 required positional argument: 'messages'` |
| `tmp_verify_toolrun_denied.py` | 真实 `Manager` + 假 deps，触发超管门槛拒绝 | `denials=[{... 'tool_requires_superadmin'}]` / `records: call_err=None success_would_be=True` |
| `tmp_verify_migrations.py` | 抽 Go `migration{version,name,sql}` 与 Python `MIGRATIONS` 逐条比对 | `go_migrations=17 py_migrations=17 diffs=0` |
| `tmp_verify_sql_diff.py` | 抽两边所有 SQL 语句做集合比对（13 对文件） | 仅剩格式/变量拼接差异 |
| `tmp_verify_schema.py` | 真库建 schema 后统计并查幂等 | `13 tables / 20 triggers / 1 view / 32 indexes`，二次 migrate 为空 |
| `tmp_verify_symbols.py` | 40 对文件的方法级符号差 | 真实缺口只有 `toolrun.new_manager` |
| `tmp_verify_storage_ifaces.py` | 13 个 storage interface 的 83 个方法 | 全部有对应物 |
| `tmp_verify_contextmgr.py` | 提示词逐字节比对 + 预算/估算边界 | `COMPACT/SUMMARIZER prompt equal: True` |

另：四个集群的相关用例一次性全跑
`python -m unittest tests.test_contextmgr tests.test_toolrun tests.test_session tests.test_session_lifecycle tests.test_sqlite_migrations tests.test_sqlite_repositories tests.test_sqlite_store tests.test_sqlite_media tests.test_storage_models`
→ `Ran 332 tests ... OK`（**全绿不等于正确**：本报告的两条 P0/P1 都发生在这些用例覆盖不到的接缝上 ——
session 用同步假 `TitleGenerator`、toolrun 没有断言 `call_err`）。

## 收尾体检（验证者跑过，确认自己没把仓库弄脏）

```
python scripts/check_syntax.py   → scanned=743  syntax_errors=0  encoding_errors=0  bom=0 crlf=0
python scripts/check_imports.py  → modules=605 imported=604 import_failures=1
   IMPORT atri_x.tool.builtin.artist_extract: cannot import name 'MetadataOriginArchive' from 'atri_x.artist'
```

- 本次验证只写了 `verification/*.md`，临时探针一律建在仓库根 `tmp_verify*` 并已删除（删除后复查 `tmp_verify*` 计数为 0）。
- `check_imports.py` 的那 1 个失败**不在本次四个集群内**（`atri_x/artist/**` ←→ `atri_x/tool/builtin/artist_extract.py`），
  且 `atri_x/artist/__init__.py:120` 与 `atri_x/artist/types.py:169` 对同一符号的定义看上去是并行实现中的中间状态 —— 交给 Lead 找该集群的负责人，验证者未越权修改。
  > **已解决（第三轮之后）**：`check_imports.py` 现在报 `modules=612 imported=612 import_failures=0`，
  > `MetadataOriginArchive` 已在 `atri_x.artist.__all__` 里；上面这条登记可以划掉。

## 局限（本次没能验证的部分）

- **没有 Go 工具链**：所有 Go 侧结论都是静态读源码；无法用差分运行证明"同输入同输出"。
- **未逐行核对的控制流**：`storage/sqlite/atrinis_event_repository.py`（报告状态机）、
  `media_lifecycle.py`、`media_repository.py`、`chat_history_repository.py` 的后半
  （这些文件的 SQL 文本、事务点数量、方法集已自动比对一致，但分支条件未逐行读完）。
- **未验证的运行时效果**：20 个触发器的具体行为（只验证了它们被建出来）、migration 失败回滚路径
  （只做了静态核对）。
- **未覆盖的接缝**：四个集群与 `atri_x/agent/**` 的其余耦合点（例如 `Loader`/`WindowResolver` 之外
  的接线）只按调用点抽查，没有全量追踪。
