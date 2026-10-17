"""内置默认配置资产。**本文件由 ``scripts/extract_assets.py`` 生成，不要手改。**

对应 Go：``internal/config/assets.go``。

资产正文放在同目录 :file:`assets_data/` 下，**相对路径与运行期落盘路径完全一致**
（因为 ``BuiltinAssetDrift`` 会逐字节比较内置资产与磁盘文件）。修改上游后重新运行::

    python scripts/extract_assets.py

目录布局来源：Go 的 ``defaultConfigAssets`` / ``defaultConfigDirs``。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "assets_data"

#: 上游 Go 包名，保留以便交叉检索。
UPSTREAM_GO_CONST = "internal/config/assets.go"


@dataclass(frozen=True)
class DefaultAsset:
    """一条内置资产。对应 Go ``defaultAsset``。"""

    #: 相对配置目录的落盘路径（POSIX 分隔符）
    path: str
    #: 对应的 Go 常量名（仅用于交叉检索）
    go_const: str

    def content(self) -> str:
        """读取资产正文（UTF-8，LF）。"""
        return (DATA_DIR / self.path).read_text(encoding="utf-8")


#: 默认配置目录。对应 Go ``defaultConfigDirs``。
DEFAULT_CONFIG_DIRS: tuple[str, ...] = (
    "skills",
    "skills/agent",
    "skills/go",
    "plugins",
    "long_memory",
)


#: 默认配置资产清单。对应 Go ``defaultConfigAssets``。
DEFAULT_CONFIG_ASSETS: tuple[DefaultAsset, ...] = (
    DefaultAsset(path="app.toml", go_const="defaultAppTOML"),
    DefaultAsset(path="services.toml", go_const="defaultServicesTOML"),
    DefaultAsset(path="state.toml", go_const="defaultStateTOML"),
    DefaultAsset(path="SOUL.md", go_const="defaultSoulMD"),
    DefaultAsset(path="memories.toml", go_const="defaultMemoriesTOML"),
    DefaultAsset(path="atrinis.toml", go_const="defaultAtrinisTOML"),
    DefaultAsset(path="tool_tags.toml", go_const="defaultToolTagsTOML"),
    DefaultAsset(path="plugins/hooks.toml", go_const="defaultHooksTOML"),
    DefaultAsset(path="plugins/.env", go_const="defaultHookEnv"),
    DefaultAsset(path="skills/agent/agent_skill_creator/SKILL.md", go_const="defaultAgentSkillCreatorSkillMD"),
    DefaultAsset(path="skills/agent/agent_skill_creator/ATRIX_SKILL.toml", go_const="defaultAgentSkillCreatorSkillTOML"),
    DefaultAsset(path="skills/agent/write_atrix_hook/SKILL.md", go_const="defaultWriteAtrixHookSkillMD"),
    DefaultAsset(path="skills/agent/write_atrix_hook/ATRIX_SKILL.toml", go_const="defaultWriteAtrixHookSkillTOML"),
    DefaultAsset(path=".env.example", go_const="defaultEnvExample"),
)


def default_config_asset(path: str) -> DefaultAsset | None:
    """按落盘路径查一条内置资产。"""
    for asset in DEFAULT_CONFIG_ASSETS:
        if asset.path == path:
            return asset
    return None


def ensure_platform_defaults(config_dir: Path) -> None:
    """在 ``config_dir`` 下创建默认目录与缺失的默认资产。

    对应 Go ``EnsurePlatformDefaults``（Go 侧还负责解析平台默认配置路径，
    那部分在 ``atri_x.config.loader`` 里）。
    """
    for rel in DEFAULT_CONFIG_DIRS:
        (config_dir / rel).mkdir(parents=True, exist_ok=True)
    for asset in DEFAULT_CONFIG_ASSETS:
        target = config_dir / asset.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(asset.content(), encoding="utf-8", newline="\n")


def copy_asset_tree(dest: Path) -> None:
    """把整个资产树复制到 ``dest``（用于离线打包/备份脚本）。"""
    shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)
