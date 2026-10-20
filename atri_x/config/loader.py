"""配置文件的查找、读取与合并（``app.toml`` 为入口）。

对应 Go：``internal/config/config.go`` 的 ``ResolvePath`` / ``Load`` /
``EnsurePlatformDefaults`` 的调用侧 / ``ConfigEnv`` / ``LoadDotEnv`` / ``LoadEnvFile`` /
``PluginConfigDir`` / ``resolveProviderAPIKeys``。

**查找顺序**（``resolve_path``）：

1. 显式传入的路径（Go 的 ``Load(path)``，CLI 的 ``--config``）；
2. 环境变量 :data:`ENV_CONFIG_FILE`（``ATRIX_CONFIG_FILE``）；
3. 平台默认配置目录（Windows ``%AppData%\\ATRI-X\\app.toml``，其他平台
   ``$XDG_CONFIG_HOME/atri-x/app.toml``）；不存在时按内置资产生成整份默认配置。

**``services.toml`` 缺失降级**：``[config_files].services`` 指向的文件不存在时，
若旧 ``providers.toml`` 仍然存在，则退回旧文件并往
:attr:`atri_x.config.app.AppConfig.config_warnings` 记一条警告（Go 此时直接报错；
这里只在「有旧文件可退」时降级，两边都缺仍然报错，不会静默变成"没有 Provider"）。
``state.toml`` / ``atrinis.toml`` / ``tool_tags.toml`` 缺失属于正常运行状态。

**``.env`` 读取**：``config_env`` 先看**系统环境变量**，再退回配置目录 ``.env``；
``.env`` 里同名变量**首次出现者生效**（与 Go 一致）。

**相对路径**：``app.toml`` 里的相对路径一律相对**该文件所在目录**解析
（``resolve_relative``），只有 ``storage`` / ``soul`` / ``character_library`` /
``sandbox`` / 生图的 artist_library 与 metadata_store 会被解析成绝对/归一化路径。

**平台默认目录**（:func:`platform_default_config_path` / :func:`platform_default_data_dir`）
也放在这里：它们是查找顺序的第 3 步，Windows 用 ``%AppData%\\ATRI-X``，其他平台用
``$XDG_CONFIG_HOME/atri-x``（缺省 ``~/.config``）。:mod:`atri_x.config.app` 需要数据目录
来填默认 SQLite/sandbox 路径，那里用**函数内延迟导入**取用，避免两模块循环导入。
"""

from __future__ import annotations

import os
from pathlib import Path

from atri_x.config import assets as _assets
from atri_x.config.app import (
    APP_DIR_NAME,
    XDG_APP_DIR_NAME,
    AppConfig,
    resolve_relative,
)
from atri_x.config.app_sections import (
    AtrinisConfig,
    ConfigDecodeError,
    MissingConfigFileError,
    load_toml,
)
from atri_x.config.services import ServicesConfig
from atri_x.config.state import load_state
from atri_x.config.tool_tags import load_tool_tags

__all__ = [
    "ENV_CONFIG_FILE",
    "PLUGIN_CONFIG_DIR_NAME",
    "config_env",
    "ensure_platform_defaults",
    "load",
    "load_dot_env",
    "load_env_file",
    "load_from_dir",
    "load_providers_config",
    "platform_default_config_path",
    "platform_default_data_dir",
    "plugin_config_dir",
    "resolve_path",
    "resolve_provider_api_keys",
    "resolve_relative",
]

#: 指定配置文件的环境变量名。对应 Go ``EnvConfigFile``。
ENV_CONFIG_FILE = "ATRIX_CONFIG_FILE"
#: 插件专属配置目录名。对应 Go ``PluginConfigDirName``。
PLUGIN_CONFIG_DIR_NAME = "plugins"


def platform_default_config_path() -> str | None:
    """平台默认 ``app.toml`` 路径；取不到用户配置目录时返回 ``None``。

    对应 Go ``platformDefaultConfigPath``。
    """
    directory = _user_config_dir()
    if not directory:
        return None
    name = APP_DIR_NAME if os.name == "nt" else XDG_APP_DIR_NAME
    return os.path.join(directory, name, "app.toml")


def platform_default_data_dir() -> str:
    """平台默认数据目录。对应 Go ``platformDefaultDataDir``。"""
    if os.name == "nt":
        directory = _user_config_dir()
        if directory:
            return os.path.join(directory, APP_DIR_NAME, "data")
        return os.path.join(APP_DIR_NAME, "data")
    directory = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if directory:
        return os.path.join(directory, XDG_APP_DIR_NAME)
    home = _user_home_dir()
    if home:
        return os.path.join(home, ".local", "share", XDG_APP_DIR_NAME)
    return "data"


def _user_config_dir() -> str:
    """``os.UserConfigDir()`` 的等价实现。"""
    if os.name == "nt":
        return (os.environ.get("AppData") or os.environ.get("APPDATA") or "").strip()
    directory = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if directory:
        return directory
    home = _user_home_dir()
    return os.path.join(home, ".config") if home else ""


def _user_home_dir() -> str:
    if os.name == "nt":
        return (os.environ.get("USERPROFILE") or "").strip()
    return (os.environ.get("HOME") or "").strip()


def resolve_path(path: str | os.PathLike[str] | None = "") -> str:
    """按「显式路径 → 环境变量 → 平台默认目录」解析出 ``app.toml`` 路径。"""
    explicit = os.fspath(path).strip() if path is not None else ""
    if explicit:
        return os.path.normpath(explicit)
    env_path = (os.environ.get(ENV_CONFIG_FILE) or "").strip()
    if env_path:
        return os.path.normpath(env_path)
    default_path = platform_default_config_path()
    if default_path:
        try:
            os.stat(default_path)
            return default_path
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ConfigDecodeError(f"stat default config {default_path!r}: {exc}") from None
    if default_path:
        return ensure_platform_defaults()
    raise ConfigDecodeError("platform config dir is unavailable")


def ensure_platform_defaults() -> str:
    """确保平台默认配置目录存在（缺什么补什么），返回 ``app.toml`` 路径。"""
    config_path = platform_default_config_path()
    if not config_path:
        raise ConfigDecodeError("platform config dir is unavailable")
    _assets.ensure_platform_defaults(Path(config_path).parent)
    return config_path


def load(path: str | os.PathLike[str] | None = "", *, strict: bool = True) -> AppConfig:
    """加载完整配置。对应 Go ``Load``。

    ``strict=False`` 时未知键不报错（保留给需要宽容旧配置的调用方）。
    """
    config_path = resolve_path(path)
    config: AppConfig = load_toml(config_path, AppConfig, strict=strict)
    config.apply_app_defaults()

    providers_path = resolve_relative(config_path, config.config_files.providers) if (
        config.config_files.providers
    ) else ""
    services_path = resolve_relative(config_path, config.config_files.services) if (
        config.config_files.services
    ) else ""

    if providers_path and not os.path.exists(providers_path):
        if services_path and os.path.exists(services_path):
            config.config_warnings.append(
                f"[config_files].providers 指向的 {providers_path!r} 不存在，已降级为 "
                f"services 配置 {services_path!r}"
            )
            providers_path = ""
        else:
            raise MissingConfigFileError(
                f"read config {providers_path!r}: 文件不存在（由 [config_files].providers 指定）"
            )
    if providers_path:
        config.merge_providers(load_providers_config(providers_path, strict=strict))

    if services_path and not os.path.exists(services_path):
        if providers_path and os.path.exists(providers_path):
            config.config_warnings.append(
                f"[config_files].services 指向的 {services_path!r} 不存在，已降级为旧的 "
                f"providers 配置 {providers_path!r}"
            )
            services_path = ""
        else:
            raise MissingConfigFileError(
                f"read config {services_path!r}: 文件不存在（由 [config_files].services 指定）"
            )
    if services_path:
        services: ServicesConfig = load_toml(services_path, ServicesConfig, strict=strict)
        config.merge_services(services)

    config.apply_provider_defaults()
    # 第一遍默认值跑在可选 services 文件合并之前，这里再跑一遍，让 services 提供的
    # [image_generation] 等段也拿到标准默认值（所有默认值都是幂等的）。
    config.apply_app_defaults()
    resolve_provider_api_keys(config, os.path.dirname(config_path))

    state_path = resolve_relative(config_path, config.config_files.state)
    state = load_state(state_path)
    if state is not None:
        config.apply_state(state)

    atrinis_path = resolve_relative(config_path, config.config_files.atrinis)
    if os.path.exists(atrinis_path):
        config.atrinis = load_toml(atrinis_path, AtrinisConfig, strict=strict)
    config.apply_atrinis_defaults()

    tool_tags_path = resolve_relative(config_path, config.config_files.tool_tags)
    tags = load_tool_tags(tool_tags_path, strict=strict)
    if tags is not None:
        config.tool_tags = tags

    # state.toml 由运行期回写：与任何只读配置文件共用都会把那份文件覆盖成只剩运行态段。
    for name, shared in (
        ("app", config_path),
        ("providers", providers_path),
        ("services", services_path),
        ("atrinis", atrinis_path),
        ("tool_tags", tool_tags_path),
    ):
        if shared and state_path == shared:
            raise ConfigDecodeError(
                f"config_files.state must point to a dedicated writable file, but "
                f"{state_path!r} is also used by {name} config"
            )

    config.validate_provider_protocols()
    config.validate_provider_headers()
    config.validate_mode_models()
    config.validate_image_generation(
        config_path,
        {
            "app config": config_path,
            "providers config": providers_path,
            "services config": services_path,
            "atrinis config": atrinis_path,
            "tool_tags config": tool_tags_path,
        },
    )
    config.storage.sessions_sqlite_path = resolve_relative(
        config_path, config.storage.sessions_sqlite_path
    )
    config.storage.chat_history_sqlite_path = resolve_relative(
        config_path, config.storage.chat_history_sqlite_path
    )
    config.soul.path = resolve_relative(config_path, config.soul.path)
    config.character_library.root = resolve_relative(config_path, config.character_library.root)
    if config.image_generation.artist_library.root.strip():
        config.image_generation.artist_library.root = resolve_relative(
            config_path, config.image_generation.artist_library.root
        )
    if config.image_generation.metadata_store.strip():
        config.image_generation.metadata_store = resolve_relative(
            config_path, config.image_generation.metadata_store
        )
    config.sandbox.root = resolve_relative(config_path, config.sandbox.root)
    config.config_path = config_path
    config.providers_config_path = providers_path
    config.services_config_path = services_path
    config.state_config_path = state_path
    config.atrinis_config_path = atrinis_path
    config.tool_tags_config_path = tool_tags_path
    return config


def load_from_dir(
    config_dir: str | os.PathLike[str], *, strict: bool = True
) -> AppConfig:
    """从配置目录加载其中的 ``app.toml``（Python 侧便捷入口，Go 只有 ``Load(path)``）。"""
    return load(os.path.join(os.fspath(config_dir), "app.toml"), strict=strict)


def load_providers_config(path: str | os.PathLike[str], *, strict: bool = True) -> AppConfig:
    """读取旧的 ``providers.toml``。

    Go 把它解码进完整的 ``Config``，但 ``mergeProviders`` 只取 ``providers`` 与
    ``model_metadata``；这里保持一致（也顺带接受旧式 ``[image_generation]`` 段）。
    """
    config: AppConfig = load_toml(path, AppConfig, strict=strict)
    return config


def plugin_config_dir(config_path: str = "") -> str:
    """插件专属配置目录（``<配置目录>/plugins``）。对应 Go ``PluginConfigDir``。

    插件字段不进 ``Config`` 模型：核心配置结构不随插件膨胀，具体字段由插件自行解析。
    """
    if not config_path:
        config_path = platform_default_config_path() or ""
    return os.path.join(os.path.dirname(os.path.normpath(config_path)), PLUGIN_CONFIG_DIR_NAME)


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def config_env(key: str, config_dir: str) -> str | None:
    """按 key 取环境变量：**系统环境变量优先**，其次配置目录的 ``.env``。

    对应 Go ``ConfigEnv``：都取不到时返回 ``None``（Go 的 ``ok=false``）。
    """
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    values = load_env_file(os.path.join(config_dir, ".env"))
    return values.get(key)


def load_dot_env(config_dir: str) -> dict[str, str]:
    """读取配置目录下的 ``.env``。对应 Go ``LoadDotEnv``。"""
    return load_env_file(os.path.join(config_dir, ".env"))


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """读取指定 dotenv 文件；文件不存在返回空映射；同名变量首次出现者生效。"""
    values: dict[str, str] = {}
    target = os.fspath(path)
    if not target.strip():
        return values
    try:
        text = Path(target).read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise ConfigDecodeError(f"open env file {target!r}: {exc}") from None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        if name not in values:
            values[name] = _unquote_env_value(value).strip()
    return values


def _unquote_env_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
        return text[1:-1]
    return text


def resolve_provider_api_keys(config: AppConfig, config_dir: str) -> None:
    """把 ``api_key_env`` 解析成 ``api_key``（显式 ``api_key`` 优先）。

    对应 Go ``(*Config).resolveProviderAPIKeys``；放在 loader 是因为它需要 ``.env``。
    取不到值时**不报错**（密钥可能来自命令行/平台注入），只跳过。
    """
    for name, provider in config.providers.items():
        if provider.api_key.strip() or not provider.api_key_env.strip():
            continue
        try:
            value = config_env(provider.api_key_env, config_dir)
        except ConfigDecodeError as exc:
            raise ConfigDecodeError(f'resolve provider "{name}" api key: {exc}') from None
        if value is None or not value.strip():
            continue
        provider.api_key = value
