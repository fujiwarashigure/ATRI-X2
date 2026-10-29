"""科研研究助理：随包引擎、技能与装配引导。

本子包负责"研究助理"在 **ATRI-X 自身**里落地所需的**非原生**部分：

* :mod:`atri_x.research.pack` —— 随包资源 ``research_pack/``（引擎源码 + 15 个技能 +
  清单数据）在源码树与冻结产物里的定位；
* :mod:`atri_x.research.bootstrap` —— 首次启动把引擎与技能装进用户配置目录
  （``<配置目录>/ATRI-X/research/engine`` 与 ``<配置目录>/ATRI-X/skills/agent/research_*``），
  幂等、可升级、失败不阻断主程序；
* :mod:`atri_x.research.runner` —— 技能 ``ATRIX_SKILL.toml`` 的执行入口：
  冻结包用 ``atri-x.exe -m atri_x.research.runner`` 起子进程，源码树用当前解释器跑
  ``<配置目录>/research/engine/run.py``；
* :mod:`atri_x.research.paths` —— 研究数据目录（``%APPDATA%/ATRI-X/research``）的
  唯一解析规则，``/research`` 命令与桌面「研究」页都从这里取，不再各写一份。

分层与依赖方向：本子包只依赖标准库；**不 import** ``atri_x.agent`` / ``atri_x.gui``，
也不在模块顶层 import 引擎（引擎来自磁盘上的 ``research_pack/engine``，由
:mod:`atri_x.research.runner` 在子进程里加载）。这样配置层、命令层、GUI 层都能安全引用，
不会把"引擎能否导入"变成主程序启动的前置条件。

本模块**只导出轻量的目录解析**：``bootstrap`` / ``runner`` 由使用方按需显式导入
（``from atri_x.research import bootstrap``），这样 ``python -m atri_x.research.bootstrap``
不会因为包 ``__init__`` 已经 import 过而触发 runpy 的"模块已在 sys.modules"警告。
"""

from __future__ import annotations

from atri_x.research.paths import default_config_dir, research_home

__all__ = ["default_config_dir", "research_home"]
