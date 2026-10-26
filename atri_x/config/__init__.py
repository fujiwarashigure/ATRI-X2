"""配置层：``app.toml`` / ``services.toml``（旧 ``providers.toml``）/ ``state.toml`` /
``atrinis.toml`` / ``tool_tags.toml`` / ``.env`` 的读取、严格解码、默认值与运行态回写。

对应 Go：``internal/config``（``config.go`` / ``assets.go`` / ``inspect.go``）。
本包是**枢纽模块**：其他模块只消费这里的公开名，不要各自再解析 TOML。

模块分工：

- :mod:`atri_x.config.app`：``AppConfig`` 根结构（Go ``Config``）+ 默认值/校验/合并。
- :mod:`atri_x.config.app_sections`：app.toml 各静态段 dataclass + **严格解码框架**。
- :mod:`atri_x.config.services`：``[providers.*]`` / ``services.toml`` / 生图 / 视觉 / ASR。
- :mod:`atri_x.config.state`：``state.toml`` 运行态 + 热加载（mtime 门限 + 分区合并）。
- :mod:`atri_x.config.tool_tags`：``tool_tags.toml``。
- :mod:`atri_x.config.loader`：查找顺序、``.env``、相对路径、整体 ``load()``。
- :mod:`atri_x.config.inspect`：未知键与内置 Skill 漂移。
- :mod:`atri_x.config.assets`：内置默认资产（由 ``scripts/extract_assets.py`` 生成）。

名字冲突说明：``ensure_platform_defaults`` 在 Go 里有两个形态（``assets.go`` 的
"在指定目录补齐资产"与 ``config.go`` 调用侧的"确保平台默认目录并返回 app.toml 路径"）。
包级导出的是**后者**（:func:`atri_x.config.loader.ensure_platform_defaults`）；
前者以 :data:`ensure_platform_defaults_in_dir` 导出，也可以直接走
``atri_x.config.assets.ensure_platform_defaults``。
"""

from __future__ import annotations

from atri_x.config import (
    app as app,
    app_sections as app_sections,
    assets as assets,
    inspect as inspect,
    loader as loader,
    services as services,
    state as state,
    time_sections as time_sections,
    tool_tags as tool_tags,
)
from atri_x.config.app import *  # noqa: F403
from atri_x.config.app_sections import *  # noqa: F403
from atri_x.config.assets import (
    DATA_DIR,
    DEFAULT_CONFIG_ASSETS,
    DEFAULT_CONFIG_DIRS,
    DefaultAsset,
    copy_asset_tree,
    default_config_asset,
)
from atri_x.config.assets import ensure_platform_defaults as ensure_platform_defaults_in_dir
from atri_x.config.inspect import *  # noqa: F403
from atri_x.config.loader import *  # noqa: F403
from atri_x.config.services import *  # noqa: F403
from atri_x.config.state import *  # noqa: F403
from atri_x.config.time_sections import *  # noqa: F403
from atri_x.config.tool_tags import *  # noqa: F403

__all__ = sorted(
    {
        "DATA_DIR",
        "DEFAULT_CONFIG_ASSETS",
        "DEFAULT_CONFIG_DIRS",
        "DefaultAsset",
        "app",
        "app_sections",
        "assets",
        "copy_asset_tree",
        "default_config_asset",
        "ensure_platform_defaults_in_dir",
        "inspect",
        "loader",
        "services",
        "state",
        "time_sections",
        "tool_tags",
    }
    | set(app.__all__)
    | set(app_sections.__all__)
    | set(inspect.__all__)
    | set(loader.__all__)
    | set(services.__all__)
    | set(state.__all__)
    | set(time_sections.__all__)
    | set(tool_tags.__all__)
)
