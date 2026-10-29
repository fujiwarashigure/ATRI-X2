# ADR 0001：平铺布局（`atri_x/` 不下沉、不做 src-layout）

- 状态：接受（自 0.9.0 起）
- 上下文：本仓库是 Go 项目 [`ATRI-X`](https://github.com/fujiwarashigure/ATRI-X) 的逐包重写。

## 决定

仓库根下直接放包目录 `atri_x/`，**不安装、不做 `src/` 布局、不拆多包**。包里按"域"分目录
（`agent/`、`tool/`、`platform/` …），但域内部**基本平铺**（一个关注点一个模块，不追求更深的层级）。
运行方式是仓库根目录下的 `python -m atri_x`。

## 为什么

1. **与上游一一对应**：`PORTING.md` §4 要求 `internal/<pkg>/…` ↔ `atri_x/<pkg>/…` 可逐包对照，
   Go 的包边界就是 Python 的目录边界。一旦重排目录，映射表、移植状态、逐包核对全部失效。
2. **零安装**：`python -m atri_x` 直接跑，没有 `pip install -e .`、没有构建产物，方便在目标机上
   复现问题（出问题时整包拷走就能跑）。
3. **冻结产物简单**：PyInstaller 用 `collect_submodules("atri_x")` 一次收干净，不需要维护包清单。

## 代价（实测，来自 `atri_x/devtools`）

- 模块数 **784**，任何一个模块都能 import 任何别的模块：**跨一级域的 import 边占四成**；
- 7 个枢纽被大量依赖：`atri_x.errors` fan-in **250**、`atri_x.llm.llm` 88、`atri_x.security` 73、
  `atri_x.tool.tool` 68、`atri_x.config` 64、`atri_x.storage.models` 61；
- "我只改这一块"在类型层面不成立，所以**导航必须靠生成索引**（见 ADR 0004）。

## 边界（什么时候该收回这个决定）

不改目录层级，而是**加边界**：给枢纽定义公开面（`atri_x/devtools/boundaries.py`），跨域只允许
`from atri_x.<hub> import <公开符号>`。本版把现状的 26 处"绕开 facade"冻结成只降不升的预算，
而不是一次性搬家——一次性重排 784 个文件会让 git blame 断代、生成物失效、所有 PR 冲突。

## 相关

- 目录不是知识来源：[`docs/dev/START-HERE.md`](../../docs/dev/START-HERE.md)
- 预算与收敛顺序：[ADR 0004](0004-structural-convergence.md)
- 上游映射：[`PORTING.md`](../../PORTING.md) §4、`devdocs/generated/go-map.md`
