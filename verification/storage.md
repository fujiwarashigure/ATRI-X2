# storage 集群对抗性验证报告

- 范围：`atri_x/storage/**`（含 `sqlite/`、`repositories.py`、`models.py`、`ids.py`、`times.py`、`storage.py`） ←→ 上游 `internal/storage/**`
- 上游基准：[`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X)（本机**没有 Go 工具链**，Go 侧结论均来自逐行读源码）；Python 侧结论全部来自实跑探针/测试。
- 复现探针（仓库根，跑完已删）：`tmp_verify_sql_diff.py`、`tmp_verify_migrations.py`、`tmp_verify_schema.py`、`tmp_verify_symbols.py`、`tmp_verify_storage_ifaces.py`。

## 覆盖表

| Go 生产文件 | 行数 | Python 对应 | 结论 |
|---|---|---|---|
| `storage/id.go` | 14/18 | `storage/ids.py` | 一一对应（`new_id`）；Python 另加 `parse_id/is_valid_id` |
| `storage/time.go` | 24/29 | `storage/times.py` | 一一对应；Python 另加 `is_zero_time/ensure_utc/ZERO_TIME` |
| `storage/storage.go` | 467/512 | `storage/models.py`（领域模型）+ `storage/repositories.py`（接口）+ `storage/storage.py`（出口） | 按"模型/接口/出口"三分；13 个 interface 的**全部 83 个方法**在 `repositories.py` 都有对应（探针见下） |
| `sqlite/store.go` | 83/101 | `sqlite/store.py` | 一一对应（含 `PRAGMA foreign_keys = ON`、`MkdirAll 0o755`、migration 调用） |
| `sqlite/migrations.go` | 509/540 | `sqlite/migrations.py` | **17 条迁移逐字节一致**（探针），见下 |
| `sqlite/session_repository.go` | 384/415 | `sqlite/session_repository.py` | 一一对应（SQL/参数/排序/limit 默认值全部一致） |
| `sqlite/message_repository.go` | 249/274 | `sqlite/message_repository.py` | 一一对应；见 ST-1 |
| `sqlite/context_summary_repository.go` | 134/148 | `sqlite/context_summary_repository.py` | 一一对应（含 `m.id <= ?` 这个与 message 仓不一致的上游口径，Python 保留并加注释） |
| `sqlite/tool_call_repository.go` | 122/133 | `sqlite/tool_call_repository.py` | 一一对应（500 一批 IN 查询、空列表不查库） |
| `sqlite/cron_job_repository.go` | 310/332 | `sqlite/cron_job_repository.py` | 一一对应（upsert 短路、`optionalTimeEqual`、两条条件 CAS） |
| `sqlite/atrinis_event_repository.go` | 474 | `sqlite/atrinis_event_repository.py` | SQL 逐条一致、3 个事务点一致；控制流**未逐行核对**（见"未能验证"） |
| `sqlite/media_repository.go` | 133 | `sqlite/media_repository.py` | SQL 逐条一致；upsert/媒体引用读写**未逐行核对** |
| `sqlite/media_history.go` | 54 | `sqlite/media_history.py` | SQL 一致、事务点一致 |
| `sqlite/media_lifecycle.go` | 153 | `sqlite/media_lifecycle.py` | 10 条 SQL 逐条一致、2 个事务点一致；控制流**未逐行核对** |
| `sqlite/chat_history_repository.go` | 492/523 | `sqlite/chat_history_repository.py` | `NewChatHistory`/`migrateChatHistory`/`Append`/`GetByPlatformMessage`/`Search`/`Around`/`ListRange`/`DeleteBefore`/`CountRange`/`listAroundSide` 逐行核对一致 |
| （无） | — | `sqlite/__init__.py` | 再导出 |

**文件级无缺失、无多余（除再导出文件）。**

## 符号级核对

1. **Go 的 13 个 storage interface、共 83 个方法**，逐个在 `atri_x/storage/repositories.py` 里查到 snake_case 对应物（探针 `tmp_verify_storage_ifaces.py`）：
   ```
   Store methods=9 OK | SessionRepository=7 OK | MessageRepository=8 OK | MediaRepository=15 OK
   MediaReferenceRepository=5 OK(仅 IDs→i_ds 的大小写伪差) | ToolCallRepository=3 OK(同上)
   CronJobRepository=10 OK | AtrinisEventRepository=14 OK(同上) | ChatHistoryRepository=5 OK
   ChatHistoryRangeRepository=1 OK | OutboundMessageRepository=3 OK | OutboundMessageCounter=1 OK
   ContextSummaryRepository=3 OK
   ```
2. **每个 `sqlite/*.go` 的导出方法名 → `sqlite/*.py`**（探针 `tmp_verify_symbols.py`）：除 `boolInt/nullString/nullTime` 三个包级 helper 统一搬到 `store.py`（docstring 已说明）与 Go 未导出 helper 变成 Python `_xxx` 之外，**没有方法缺失**。
3. **接口方法命名**：`SuccessfulIDs→successful_ids`、`ListMediaIDs→list_media_ids`、`ListResultReadyReportIDs→list_result_ready_report_ids` 等按 `PORTING.md §1.4`（`ID`→`id`）处理，属有意为之。

## 发现

### ST-1（P3）`messages.segments` 的解析严格度与 Go 有两个方向相反的小差异

- 严重度：**P3**（只影响畸形 segments 数据，正常写入路径不会产生）
- Go 证据：`internal/storage/sqlite/message_repository.go:50-57` 把 segments 解成
  `[]struct{ MediaID string \`json:"media"\` }`：
  - 元素是 `null` → Go 的 `null` 反序列化到 struct 是 no-op，**不报错**（继续 append）；
  - `media` 不是字符串（如 `{"media":5}`）→ 类型不匹配 → **报错并回滚整个 append 事务**。
- Python 证据：`atri_x/storage/sqlite/message_repository.py:212-233` `_segment_media_ids`：
  - 元素是 `null` → `isinstance(item, dict)` 为假 → **抛 `InvalidError`**（消息写不进去，而 Go 能写）；
  - `media` 非字符串 → `isinstance(media_id, str)` 为假 → **静默跳过**（Go 会报错）。
- 影响：只有在 segments 被外部（hook/历史脏数据）写成 `[null]` 或 `{"media":数字}` 时才会分叉；本仓自身的写入方 `models.segments_json` 恒产生对象数组，命中概率极低。
- 修复建议：按 Go 语义改判定（`null` 元素跳过而不报错；非字符串 `media` 报错）。
- 是否建议现在修：不建议（P3，且需要先确认是否想保留 Python 更严格的 `null` 行为）。

### ST-2（P3）`chat_history` 的 DB 打开顺序与 Go 略有差异（无后果）

- Go `NewChatHistory`（`chat_history_repository.go:30-55`）在**同一个函数**里：`MkdirAll` → `sql.Open` → `SetMaxOpenConns(1)` → `PRAGMA foreign_keys = ON` → `PingContext` → `migrateChatHistory`。
- Python `new_chat_history`（`chat_history_repository.py:122-143`）复用 `store.open_database()`（内部就是 `MkdirAll` + `sqlite3.connect` + `PRAGMA foreign_keys = ON` + `busy_timeout=0`）再 `migrate_chat_history`。
- 结论：**pragma 仍然在 migration 之前执行**，语义一致；SQL 文本差异扫描里出现的 `[ONLY-GO] PRAGMA foreign_keys = ON` 只是因为它被搬到了 `store.py`。不构成缺陷，记录以免误判。

### ST-3（P3）`times.py` 的两处"更宽松/更严格"的解析边界

- `parse_time` 的小数秒：Go `time.Parse(time.RFC3339Nano, s)` 对超过 9 位的小数秒报错（`fractional second`）；Python `times.py:92-93` `int((fraction + "000000")[:6])` 会**截断**任意长度的小数（如 12 位也不报错）。
- 大小写：Python 正则（`times.py:30-35`）接受小写 `t`/`z`，Go 的 `RFC3339Nano` 布局对字面量大小写敏感（是否接受小写取决于 Go 版本的 RFC3339 宽松解析）；Python 是**超集**。
- 影响：只有解析外部/手工写入的时间文本时才有差异；本仓写入端统一 `format_time`。
- 是否建议现在修：不建议。

## 已核对无问题的高风险点

1. **17 条 migration 与 Go 逐字节一致**（这是本集群最危险的地方：DDL/触发器/视图漂移会静默丢数据）。
   探针 `tmp_verify_migrations.py` 从 Go 源里抽出 `version/name/sql`，与 Python `MIGRATIONS` 逐条做空白归一化后的 `==`：
   ```
   go_migrations=17 py_migrations=17
   diffs=0
   versions equal: True
   names equal: True
   ```
   覆盖 v1..v17（含 v13 的 18 个触发器与 `media_fork_history` 视图、v14 孤儿/守卫触发器、v15 重建 media_outputs、v16 media_history、v17 重建视图）。
2. **运行时 schema 与 Go 的迁移结果自洽**：`sqlite.new(":memory:")` 后统计 `sqlite_master`：
   ```
   objects: [('index', 32), ('table', 13), ('trigger', 20), ('view', 1)]
   migrations: 17 first: 1 last: 17
   triggers: 20
   views: ['media_fork_history']
   second migrate applied: []      # 幂等
   reopen migrate: []              # 文件库重开也幂等
   messages cols: [..., 'metadata', 'created_at', 'segments']   # v11 的 ALTER 加在末尾，与 Go 相同
   ```
   Go 源里 `CREATE TRIGGER`/`CREATE VIEW` 共 24 处，扣掉 v15 重建的 2 个 trigger 与 v17 重建的 1 个 view，最终恰好是 **20 个触发器 + 1 个视图** ✓。
3. **所有 SQL 语句文本一致**（`tmp_verify_sql_diff.py`：按文件抽出反引号/三引号里的 SQL，空白归一化后比对**语句集合**）。除以下三类外**没有任何一条 SQL 只在一边存在**：
   - 仅格式差异（`a = ?` vs `a=?`、逗号后空格）；
   - Python 用 `{COLUMNS}`/`{where}` 之类的 f-string 变量（列清单/where 片段本身与 Go 一致）；
   - Python 多出的 `PRAGMA busy_timeout/journal_mode/synchronous`（默认关闭，见 `store.py` docstring 的"与上游严格一致"说明）与 `schema_migrations` 存在性探测。
   重点核对的语句：`SessionRepository.List` 的 `ORDER BY CASE WHEN s.pinned_at IS NULL THEN 1 ELSE 0 END, s.pinned_at DESC, s.updated_at DESC` / `s.updated_at DESC, s.id DESC`、`LIMIT ? OFFSET ?`、`title LIKE ?`、`(platform_scope_id = ? OR platform_scope_id LIKE 'cron:%')`、`DeleteExpired` 的 `archived_at IS NULL AND pinned_at IS NULL AND updated_at < ?`、message 仓四条 `(created_at </> ? OR (created_at = ? AND rowid ...))`、tool_call 的 `success = 1 AND tool_call_id IN (...)` 全部逐字一致。
4. **事务边界数量与位置对齐**：Go 侧 `BeginTx` 共 10 处（migrations 1、session.mutate 1、message.append 1、media_repository 1、media_history 1、media_lifecycle 2、atrinis_event 3）；Python 侧 `with_transaction()` 恰好也是这 10 处（`rg -n "with_transaction" atri_x/storage/sqlite`）。
   Python 额外支持**内层并入外层事务**（`store.py:225-254`），而 Go 在 `SetMaxOpenConns(1)` 下嵌套 `BeginTx` 会死锁 —— 现实中 Go 不存在嵌套调用，故不影响行为。
5. **分页/边界口径**：`SessionRepository.List` 的 `limit <= 0 → 20`、`offset < 0 → 0`、`archived_only` 优先于 `include_archived`、`exclude_session_id` 用 `s.id <> ?` —— 与 Go 逐行一致（`session_repository.py:151-179` ↔ `session_repository.go:156-190`）。
   `SuccessfulIDs` 的空列表不会拼出 `IN ()`：两边都是"空列表 → 不进循环 → 返回空 map"（`tool_call_repository.py:77` / `tool_call_repository.go:70`）。
   `message_repository` 的四条窗口查询用 `_message_position` 先取锚点，锚点不属于该会话即 `ErrNotFound`（与 Go 的 `messagePosition` 一致）。
6. **写路径的零值填充规则一致**（Go 用 `time.Time.IsZero`，Python 用 `times.is_zero_time`，`None` 等价零值）：session.create/update/mutate、message.append、map_platform_message、context_summary.create、tool_call.create（`CreatedAt=FinishedAt`）、cron_job 的 `now` 取值链，逐条与 Go 相同。
7. **`omitempty`/可空列语义**：`null_string`（空串→NULL）、`null_time`（`None`→NULL）、`bool_int`、`parse_optional_time`、`soft_time`（仅 media 读路径容忍脏数据，对应 Go 里 `m.CreatedAt, _ = storage.ParseTime(...)` 的忽略错误写法）—— 与 Go 的 `sql.NullString`/`*time.Time` 用法一一对应。
8. **时间文本与 SQLite 文本比较**（`PORTING.md` §1.7 的"统一微秒/统一 UTC"是有意偏差，我核对了它的下游后果）：
   - Python 一律写 `...Z`（UTC）且不输出末尾 0 的小数位；Go 的 `storage.Now()` 是 `time.Now()`，在非 UTC 主机上会写成本地偏移（`+08:00`）。
   - 仓库里确实存在按 TEXT 比较时间的 SQL（`sessions.updated_at < ?`、`ORDER BY updated_at`），而 **Go 自己的 SQL 默认值也是 `strftime('%Y-%m-%dT%H:%M:%fZ','now')`（UTC）**，即上游本身就混用 `Z` 与本地偏移。
   - 结论：Python 的"全 `Z`"在**自身运行期完全自洽**（写入与比较都同格式），但把 Go 写过的库交给 Python 读时，同刻不同文本会让 `<`/`ORDER BY` 的字典序判断失真（例如 `2026-10-13T20:00:00+08:00` 字典序大于 cutoff `2026-10-13T12:00:00Z`，过期清理会漏删）。这是**跨实现混用**才出现的问题，单跑本仓不触发，故不计为缺陷，仅记录条件。
9. **ID 契约**：`new_id()` = `str(uuid.uuid4())`（带连字符小写 UUIDv4），与 `id.go` 的 `%08x-%04x-%04x-%04x-%012x` 形态一致（`PORTING.md §1.8`）。
10. **本集群测试全绿**：`python -m unittest tests.test_sqlite_migrations tests.test_sqlite_repositories tests.test_sqlite_store tests.test_sqlite_media tests.test_storage_models`（含在四集群 332 个用例里）→ `OK`。

## 未能验证（诚实声明）

- **未逐行核对控制流**：`atrinis_event_repository.py`（378 行，含 `PrepareReport`/`ClaimReport`/`ReleaseReport`/`MarkReportDeliveryFailed` 的状态机）、`media_lifecycle.py`（claim/recover/expire 的 `RETURNING` 语义）、`media_repository.py`（孤儿媒体扫描）、`chat_history_repository.py` 的后半（`OutboundMessageRepository.ListRange/CountRange/DeleteBefore`）。这些文件的 **SQL 文本、事务点数量、方法集** 都已自动比对一致，但"分支条件/参数顺序是否逐条等价"未逐行读完。
- 迁移的**失败回滚**行为只做了静态核对（`apply_migration` 用 `with_transaction` 包裹，异常 → ROLLBACK），没有真的注入一条会失败的 DDL 去验证 `schema_migrations` 不被写入。
- 触发器/视图的**运行时效果**（如 `media_fork_created` 到底写入了哪些 `media_references` 行）没有构造数据验证，只验证了它们被建出来（20 个）。
