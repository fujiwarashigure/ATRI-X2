# ADR 0004：结构收敛的顺序（先有红灯，再搬家）

- 状态：接受（1.1.4 起）
- 上下文：本仓是平铺布局（[ADR 0001](0001-flat-layout.md)），实测 80 个文件超 600 行
  （[ADR 0002](0002-six-hundred-lines.md)），跨一级域 import 边占四成，锐减模块数/拆文件都会
  触碰大量调用点。**顺序错了会把"可维护性改造"变成"一次性大爆炸重构"**。

## 决定（按优先级，且必须按顺序）

### 第 0 步：先把漂移变成红灯（已完成，1.1.4）

- `scripts/check_docs.py`（6 条断言 + 两个 baseline 棘轮）
- `scripts/gen_index.py --check`（生成索引漂移）
- `scripts/check_complexity.py` + `scripts/complexity_budget.json`（结构指标预算）
- `.github/workflows/quality.yml`（7 道代码门禁 + 文档门禁 + 生成索引 + 复杂度 + 全量测试进 CI）

**没有这一步，后面每一步都在移动靶子上改文档。**

### 第 1 步：枢纽 facade + import 边界（已在 1.1.4 冻结基线）

- `atri_x/devtools/boundaries.py` 声明 7 个枢纽的公开面；
- 规则：**跨域**模块 import 枢纽的**子模块**（而不是枢纽本身）算一次"绕开 facade"；
  同域内部不受限；
- 现状 **26 处**（全部在 `atri_x.config` 的子模块上）冻结成预算，只降不升。

下降的方式有两条：把调用方改成 `from atri_x.config import X`（让 `atri_x/config/__init__.py`
再导出），或把该子模块登记进 `PUBLIC_SUBMODULES`（等于宣布它是公开面的一部分）。

### 第 2 步：合并微型模块（减导航面，不改行为）

- 现状：44 个文件 < 50 行，其中一部分 ≤80 行且 fan-in ≤1；
- 按域合并进邻接模块，目标 **784 → ~650**；每合一个更新预算、跑生成器；
- 不接受"为了指标而合并"：先看是否真的没有独立职责。

### 第 3 步：拆超线文件（按职责，不按行数）

优先级（fan-in / 域影响 / 是否枢纽）：

1. `atri_x/config/app_sections.py`（1225 行，配置段聚集 → 按域拆）
2. `atri_x/tool/builtin/character.py`（1117）、`atri_x/net/websocket.py`（1034）、
   `atri_x/net/httpclient.py`（999）
3. `atri_x/agent/core.py`（995）、`atri_x/agent/toolrun_adapter.py`（980）、
   `atri_x/tool/builtin/long_memory_store.py`（980）
4. 其余 ≥600 行文件（完整清单见 `scripts/line_budget_baseline.json`）

**拆法固定**：抽出职责 → 新模块 → 原模块 re-export（调用方零改动）→ 更新基线 → 跑生成器 → 单独提交。

### 第 4 步：目录渐进分层（最后做，且一次一个域）

`atri_x/` 保持平铺作为 facade，内部按 `core/`（纯逻辑）、`adapters/`（平台/IO）、
`features/`（业务域）迁移。首批试点：`platform/`（4 个适配器，边界天然清晰）与
`hook/`（已有 rules/runtime/protocol 三层）。

## 为什么这个顺序

- 先有红灯：否则每搬一次家都要手工追一遍文档与索引，越改越乱；
- 先加边界再减文件：边界决定"哪些文件是别人的 API"，没有边界就不知道能不能动；
- 最后动目录：目录平移对 git blame 与所有并行 PR 的伤害最大，收益最小（平铺本身不是主要矛盾）。

## 度量（每版写进发行说明）

| 指标 | 1.1.4 实测 | 收敛目标 |
| --- | --- | --- |
| 模块数 | 784 | ~650 |
| 超 600 行文件 | 80 | ≤ 10 |
| 最长文件 | 1225 | ≤ 600 |
| 跨一级域 import 边 | 1468 | 只降不升 |
| 最大 fan-in | 250（`atri_x.errors`） | 只降不升 |
| 无测试触达的模块 | 307 | 只降不升 |
| 跨域绕开 facade | 26 | → 0 |
| 文档漂移门禁 | 6 条断言 + 索引 + 预算 | 保持 |

复算：`python scripts/check_complexity.py --list` 与 `python -m atri_x.devtools metrics`。

## 相关

- 索引与门禁：`atri_x/devtools/`、`scripts/gen_index.py`、`scripts/check_docs.py`
- 预算：`scripts/complexity_budget.json`、`scripts/line_budget_baseline.json`
- 人的入口：[`docs/dev/START-HERE.md`](../../docs/dev/START-HERE.md)、
  [`docs/dev/find-it.md`](../../docs/dev/find-it.md)
