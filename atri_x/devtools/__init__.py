"""``atri_x.devtools``：把仓库事实**算出来**的开发期索引工具。

为什么在这个包里：项目对"代码"有 7 道可执行门禁，对"文档"只有 ``gen_stats`` 一道
（只校验 README 里的数字）。于是索引是**第二份手写的知识副本**，一旦漂移就只能靠人
记得去改。这里把 `代码 → 索引` 变成单向生成，再由 ``scripts/gen_index.py --check``
在 CI 里判漂移，文档就从"软副本"变成"编译产物"。

约束（与运行时约定一致）：

- **零第三方依赖**，只用标准库（``ast`` / ``pathlib``）；
- **不 import atri_x** —— 生成索引时不该触发整包导入（会拖慢、也可能触发副作用），
  一律用 ``ast`` 静态解析；
- **确定性输出** —— 所有集合都按稳定顺序排序，``--check`` 才能逐字节比对。

子模块：

``astindex``
    AST 扫描：模块清单、docstring 首行、公共符号、内部 import 边、测试反查、
    "对应 Go" 映射、fan-in / 跨域边等复杂度指标。
``render``
    把 :class:`astindex.RepoIndex` 渲染成 Markdown / Mermaid（无模板引擎）。
``cli``
    ``python -m atri_x.devtools <cmd>``：``map`` / ``deps`` / ``entrypoints`` /
    ``test-map`` / ``go-map`` / ``metrics`` / ``all``。

用法::

    python -m atri_x.devtools metrics       # 只打印指标
    python -m atri_x.devtools all --out devdocs/generated
"""

from __future__ import annotations

from atri_x.devtools.astindex import (
    ModuleInfo,
    RepoIndex,
    TestFile,
    collect_metrics,
    scan,
)
from atri_x.devtools.render import (
    ARTIFACTS,
    check_artifacts,
    render_artifacts,
    write_artifacts,
)

__all__ = [
    "ARTIFACTS",
    "ModuleInfo",
    "RepoIndex",
    "TestFile",
    "check_artifacts",
    "collect_metrics",
    "render_artifacts",
    "scan",
    "write_artifacts",
]
