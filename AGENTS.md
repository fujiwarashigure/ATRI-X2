# ATRI-X（Python 版）Agent 操作手册

本文件只写 **Agent 在本仓库里「怎么做」**（How）。**事实与索引不写在这里**：

- 模块职责 / 测试反查 → [`devdocs/generated/module-map.md`](devdocs/generated/module-map.md)、
  [`devdocs/generated/test-map.md`](devdocs/generated/test-map.md)（生成物，禁止手改）
- 按任务找入口 → [`docs/dev/find-it.md`](docs/dev/find-it.md)；30 分钟上手 →
  [`docs/dev/START-HERE.md`](docs/dev/START-HERE.md)
- 架构为什么这样分 → [`devdocs/architecture.md`](devdocs/architecture.md) 与
  [`devdocs/adr/`](devdocs/adr/)（0001 平铺 / 0002 600 行 / 0003 零依赖 / 0004 收敛顺序）
- 用户侧功能 → [`docs/README.md`](docs/README.md)；移植契约 → [`PORTING.md`](PORTING.md) §4
- 哪些还没核对 → [`devdocs/porting-status.md`](devdocs/porting-status.md)

## 基本原则

- 默认简体中文。当前项目是 **Python 项目**（包名 `atri_x`，目标 3.14，最低 3.11）。
- **纯标准库**：运行时不得 import 第三方包；可选加速后端必须 `try/except ImportError` 降级。
- 布局**平铺**：仓库根下直接是 `atri_x/`，不安装；入口是 `python -m atri_x`。
- 先读上下文与相关文档，再查代码；不要凭空猜。只读与任务相关的文件。
- 优先复用已有接口与模式，不为一次性需求做抽象。
- 单文件目标 ≤ 600 行，按**职责**拆；超线文件在 `scripts/line_budget_baseline.json` 里只降不升。
- 新增/删除/移动模块后**必须**跑 `python scripts/gen_index.py` 并提交生成物。

## 导航流程（替代旧的 locator 手工表）

1. **判任务类型** → 查 [`docs/dev/find-it.md`](docs/dev/find-it.md) 的对应条目（路径都已校验存在）。
2. **找模块** → `rg -n "atri_x\." devdocs/generated/module-map.md`，或直接读
   `devdocs/generated/module-map.md`（模块 → 一句话职责 → 公共符号 → 行数 → 测试）。
3. **找入口** → `devdocs/generated/entrypoints.md`（进程入口 / 装配 / 命令 / 工具 / 平台 /
   Hook / GUI / 任务入口表）。
4. **找上游对照** → `devdocs/generated/go-map.md`；不确定就回到 `PORTING.md` §4。
5. **看导出符号** → `python scripts/list_api.py <module>`。
6. 文档里的 `<!-- locator:NAME -->` 锚点仍然有效，用 `rg -n "locator:NAME" .` 跳章节；
   全量锚点清单在 `devdocs/generated/entrypoints.md` 末尾（自动生成）。

搜索用 `rg`（**不要**用 `python -c` 探接口：本机控制台是 GBK，容易把排查变成二次损坏）。

## 验证规则

改代码后至少跑：

```bash
python scripts/check_syntax.py        # 语法 + BOM + CRLF + U+FFFD + mojibake
python scripts/check_imports.py       # 逐模块导入 + __all__ 校验
python scripts/check_integration.py   # 跨模块唯一出处 / ctx 键 / 脱敏与 ID 契约
```

改结构（新增文件、大改）再跑：

```bash
python scripts/gen_index.py           # 重新生成索引（手改生成物会被 --check 抓）
python scripts/check_docs.py          # 文档门禁：链接 / 内联路径 / 索引覆盖 / 锚点 / 行数 / 数字
python scripts/check_complexity.py    # 结构预算：模块数 / 超线文件 / 跨域边 / fan-in / 边界
```

改行为跑相关测试；公共接口变化跑全量：

```bash
python -m unittest discover -s tests -t .     # 全量
python -m unittest tests.test_xxx -v          # 单个模块
```

可选棘轮：`check_secrets.py`（密钥扫描）、`check_except.py`（`except BaseException` 位置基线）、
`check_coverage.py --summary`（PORTING §4 文件存在性覆盖）。

全量 CI 见 [`.github/workflows/quality.yml`](.github/workflows/quality.yml)。

## 移植期特别注意

- 上游 Go 仓库是**只读参考**；`internal/<pkg>/…` ↔ `atri_x/<pkg>/…` 只以 `PORTING.md` §4 为准。
- Go 的 `(T, error)` → Python 抛异常，哨兵异常统一在 `atri_x/errors.py`。
- `context.Context` → ctx + `atri_x/safecontext.py`；goroutine/channel → `asyncio` task/Queue；
  `sync.Mutex` → `asyncio.Lock`（异步）或 `threading.Lock`（同步 SQLite）。
- 时间统一 `datetime`（带 `timezone.utc`，落库 RFC3339）；ID 是带连字符的小写 UUIDv4。
- 跨模块共享类型只 import `PORTING.md` §3 指定的唯一出处，不要在别的模块复制定义。

## 注意（踩过的坑）

- **不要用 PowerShell 5.1 写文件**（会写 UTF-8 BOM 或按 GBK 二次编码毁掉中文）；用编辑工具，
  或 Python 里 `encoding="utf-8", newline="\n"`。
- **不要用 `tempfile.mkdtemp()`**：本机 `mkdtemp` 以 `0o700` 建目录，清理钩子会抛 `PermissionError`
  并留下删不掉的残留；临时目录统一用 `tests/_tmp_*/` 或仓库根 `tmp_*_test/`。
- 不要在仓库根留调试产物。控制台是 GBK：脚本输出要经 ASCII 兜底（`scripts/` 里的 `_safe()`）。
- **生成物不要手改**：`devdocs/generated/`、`atri_x/<域>/README.md`、`docs/dev/glossary.md`
  都由 `python scripts/gen_index.py` 生成；要改就改 `atri_x/devtools/render.py` / `domaindocs.py`。
- 严格区分"红"和"没验证"：`skipTest` 要写原因；"没法验证"不能当成"通过"。
- 改文档后跑 `check_docs.py`；文档里写的路径必须真实存在（`CHANGELOG.md` 是历史记录，例外）。
