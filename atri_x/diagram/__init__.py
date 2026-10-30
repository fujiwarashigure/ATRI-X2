"""draw.io 图表能力：随包技能、安装引导与渲染入口。

本子包负责「draw.io 架构图」在 ATRI-X 自身里落地所需的**非原生**部分：

* :mod:`atri_x.diagram.pack` —— 随包资源 ``drawio_pack/``（上游 drawio-skill 3.4.2
  的运行文件 + 工具清单）在源码树与冻结产物里的定位，以及**内置 draw.io 无头渲染器**
  ［``drawio_runtime/``］的查找规则；
* :mod:`atri_x.diagram.bootstrap` —— 首次启动把技能装进用户配置目录
  （``<配置目录>/ATRI-X/skills/agent/drawio_skill``），幂等、可升级、失败不阻断主程序；
* :mod:`atri_x.diagram.runner` —— 技能 ``ATRIX_SKILL.toml`` 的执行入口：冻结包用
  ``atri-x.exe -m atri_x.diagram.runner --skill-dir <dir> …`` 起子进程，源码树用当前
  解释器直接跑技能目录里的 ``atrix_cli.py``。

**渲染器不复制、不安装**：draw.io 桌面版解包目录随产物放在 ``_internal/drawio_runtime``
（源码树放 ``packaging/vendor/drawio``），是只读运行时；每次导出用独立环境变量把
``APPDATA`` / ``LOCALAPPDATA`` / ``TEMP`` / ``TMP`` 收容到技能目录下的私有 profile，
所以既不读写宿主机上用户自己的 draw.io 配置，也不需要外部安装 draw.io。

分层与依赖方向：本子包只依赖标准库；**不 import** ``atri_x.agent`` / ``atri_x.gui``，
也不在模块顶层执行渲染子进程。这样配置层、命令层、GUI 层都能安全引用，不会把
「draw.io 能否启动」变成主程序启动的前置条件。
"""

from __future__ import annotations

from atri_x.diagram.pack import (
    PACK_DIR_NAME,
    PACK_META_FILE,
    SKILL_INSTALL_NAME,
    load_pack_meta,
    pack_root,
    renderer_exe,
)

__all__ = [
    "PACK_DIR_NAME",
    "PACK_META_FILE",
    "SKILL_INSTALL_NAME",
    "load_pack_meta",
    "pack_root",
    "renderer_exe",
]
