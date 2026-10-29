---
name: research_doctor
description: 环境与运行诊断：检查引擎/venv/可选依赖/网络/数据目录，列出全部研究运行，或跑离线自检（去重/筛选/引用/质检/评审的确定性用例）。
---

# research_doctor

出问题时先跑它，不要猜。

## 参数

| 参数 | 说明 |
| --- | --- |
| `action` | `env` 环境体检（默认）/ `status` 运行列表 / `selftest` 离线自检 |
| `network` | env 是否探测数据源可达性，默认 true |

## env 返回

`checks` 列表，每项 `{name, ok, detail, critical}`：

- `python` / `engine` / `home_writable` / `settings`：critical，失败即不可用；
- `optional:docx` / `optional:pdf` / `optional:pdf 抽取`：缺失只影响对应导出与全文抽取；
- `venv`：当前解释器是否在 venv 内、安装目录是否存在；
- `network`：4 个数据源的可达性（有缓存，不会反复打）；
- `cache` / `runs`：缓存规模与运行数。

## status 返回

`runs`（按更新时间倒序，含阶段）、`latest`、`latest_artifacts`。
用于回答"我上次做到哪了""产物还在不在"。

## selftest 返回

6 个离线用例的通过情况：去重合并、年份排除、APA+GB/T 格式化、未知引用键检测、
评审阈值、Markdown 解析。**全绿说明引擎算法没坏**，与网络无关。

## 常见问题对照

| 现象 | 处理 |
| --- | --- |
| `ENV_INVALID` / settings 解析失败 | 按提示修/删 `settings.json` |
| 检索全失败 | `env` 看 network；换 `sources` 单源重试 |
| 导出只有 md/html | `optional:docx`/`optional:pdf` 为 false，用安装器重建 venv |
| 全文抽取报缺 pypdf | 同上；或让用户把 PDF 文本贴进来 |
| 运行记录找不到 | `status` 看 id，注意 `latest` 只在有运行时有效 |

## 注意

- 本工具只读，不会修改任何配置；
- `network` 探测会产生少量外部请求，离线环境可传 `network=false`；
- 汇报时把失败项的 `detail` 原样贴给用户，不要转述成"网络有点问题"。

<!-- hardtool-contract:begin -->
## 工具契约（由引擎自动生成，勿手改）

- 工具：`doctor` · 技能：`research_doctor`
- 风险：`low`（普通成员可调用）
- 超时：120 秒
- 确定性：**是**——判定、排版、计数、门禁全部由引擎代码完成，不依赖模型自评。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | 枚举(env / status / selftest / parity) | 否 | env=环境体检；status=运行列表；selftest=离线自检；parity=模型无关性回归（好/敷衍/乱编三份样本，默认 env）。 |
| `network` | 枚举(true / false) | 否 | env 是否探测网络，默认 true。 |

引擎强制执行的规则（不是建议）：

- selftest 是离线确定性用例（去重/筛选/引用/质检/评审/解析），不联网。
- parity 用三份固定样本分别模拟好/敷衍/乱编模型，断言判定与分数排序不变。
- env 体检会区分 critical 与可选依赖，缺可选依赖不算失败。

<!-- hardtool-contract:end -->
