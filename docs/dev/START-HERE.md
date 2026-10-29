# 30 分钟上手（维护者入口）

> 目标：让第一次接触本仓库的人在 **30 分钟内**跑起来、看懂"东西放哪"、并完成一次真实的小改动。
> 读完之后按任务找入口去 [find-it.md](find-it.md)；术语不通查 [glossary.md](glossary.md)。

## 0–5 分钟：跑起来

```bash
python -m atri_x --version      # 需要 Python ≥ 3.11（开发验证在 3.14.7）
python -m atri_x                # 首启会在平台配置目录生成 app.toml / services.toml / SOUL.md
python -m unittest tests.test_builtin_runtime_resources -v   # 跑一个最小测试文件
```

不需要装任何第三方包，也没有构建步骤：本仓库**在仓库根目录直接跑**。配置目录在 Windows 是
`%APPDATA%\ATRI-X`，Linux 是 XDG 配置目录。

跑一次全量门禁（CI 跑的就是这些，几分钟）：

```bash
python scripts/check_syntax.py
python scripts/check_imports.py
python scripts/check_docs.py
python scripts/gen_index.py --check
python -m unittest discover -s tests -t .
```

## 5–15 分钟：心智模型

仓库是**平铺**的（`atri_x/` 下 55+ 个目录、几百个模块，没有 src-layout）。别背目录，记四层：

| 层 | 是什么 | 入口 |
| --- | --- | --- |
| **契约层** | 全仓共享的类型与协议，**唯一出处** | `atri_x/errors.py`、`atri_x/tool/tool.py`、`atri_x/llm/llm.py`、`atri_x/security/`、`atri_x/storage/models.py`、`atri_x/delivery/delivery.py`、`atri_x/config/` |
| **域层** | 一个业务域的全部实现（agent / tool / platform / gui / storage / config / hook / llm / app / media …） | 每个域有自己的 `README.md`：域职责、易错点、模块清单 |
| **装配层** | 把域接起来并管生命周期 | `atri_x/app/app.py`、`atri_x/app/tool_runtime.py` |
| **文档层** | 人读的判断类文档；**事实类索引由代码生成** | `docs/`（用户）、`docs/dev/`（维护者）、`devdocs/`（架构与生成物） |

三个**互相独立**的导航支点（任何一支坏了，另外两支仍能定位代码）：

1. **生成索引** —— `devdocs/generated/`：模块地图、域依赖图、入口清单、测试地图、Go 映射。
   改代码后跑 `python scripts/gen_index.py`；手改生成物会被 `--check` 抓到。
2. **测试地图** —— `devdocs/generated/test-map.md` 明确列出"**没有被测试直接覆盖**的模块"。
   改到那些模块时，你只能靠调用方测试，别指望同名测试。
3. **结构预算** —— `scripts/complexity_budget.json` 与 `atri_x/devtools/boundaries.py`
   把"模块数 / 超线文件 / 跨域边 / fan-in / 绕开 facade"冻结成**只降不升**的指标。

一句话：**事实看生成物，判断看 ADR，任务看 find-it。** 手写路径索引不再是知识来源。

## 15–30 分钟：做一次真实的小改动

### 练习 A（3 分钟）：感受"文档是编译产物"

1. 打开 `atri_x/devtools/domaindocs.py`，把 `DOMAIN_NOTES["tool"]` 的某条 `pitfalls` 改一句；
2. 跑 `python scripts/gen_index.py`，看 `atri_x/tool/README.md` 里那句话变了；
3. 手改 `atri_x/tool/README.md` 任意一行，再跑 `python scripts/gen_index.py --check` → **红**；
4. 跑 `python scripts/gen_index.py` 复原，再 `--check` → 绿。

这就闭环了：生成物不能手改，漂移会变红灯。

### 练习 B（10 分钟）：改一个测试并跑门禁

1. 打开 `tests/test_builtin_runtime_resources.py`，找到 `test_format_uptime`，加一条断言；
2. 跑 `python -m unittest tests.test_builtin_runtime_resources -v`；
3. 跑 `python scripts/check_docs.py`（文档门禁；如果你在注释里写了不存在的路径，这里会红）；
4. 跑 `python scripts/check_syntax.py`（确认没有 BOM / CRLF / 编码损坏）。

### 练习 C（15 分钟，可选）：加一个内置工具

按 [find-it.md 的「加一个内置工具」](find-it.md#加一个内置工具) 走一遍。你会碰到三个必须同步的地方：
注册表、GUI 工具目录、注册表计数断言——这三处是刻意的，防止"加了工具但界面上没有/测试不承认"。

## 提交前检查单

- [ ] `python scripts/check_syntax.py`、`check_imports.py`、`check_integration.py` 通过
- [ ] 动了模块结构 → `python scripts/gen_index.py` 并提交生成物
- [ ] 改了文档 → `python scripts/check_docs.py` 通过
- [ ] 改了结构（新增文件/大改）→ `python scripts/check_complexity.py` 通过
- [ ] 相关 `python -m unittest tests.test_xxx` 通过；公共接口有变化再跑全量
- [ ] 单文件不要超过 600 行（超了就按职责拆，别按行数切）

## 本机（Windows）特别提醒

- 写文件**不要用 PowerShell 5.1**（会写 UTF-8 BOM 或按 GBK 二次编码毁掉中文）；用编辑工具，或
  Python 里 `encoding="utf-8", newline="\n"`。
- 不要用 `tempfile.mkdtemp()`；临时目录统一用 `tests/_tmp_*/` 或仓库根 `tmp_*_test/`。
- 控制台是 GBK：脚本输出要经 ASCII 兜底（`scripts/` 里已有 `_safe()` 模式）。

## 接下来读什么

| 想知道 | 去读 |
| --- | --- |
| 某个任务要改哪些文件 | [find-it.md](find-it.md) |
| 为什么布局平铺 / 为什么 ≤600 行 / 为什么零依赖 | `devdocs/adr/`（0001–0004） |
| 一段代码对应上游哪个 Go 文件 | `devdocs/generated/go-map.md` |
| 这个模块的职责与测试 | `devdocs/generated/module-map.md` |
| 架构链路与设计取舍 | `devdocs/architecture.md`、`devdocs/code-map.md` |
| 仓库内操作规则（AI 与人都适用） | [AGENTS.md](../../AGENTS.md) |
| 移植契约与 Go 包映射 | [PORTING.md](../../PORTING.md) §4 |
