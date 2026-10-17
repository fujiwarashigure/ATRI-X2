"""媒体中心配置。

对应 Go：``internal/media/config.go``。

Go 的 ``media/config.go`` 只有 1.1KB，它把 ``config.FileDeliveryConfig`` 与
``config.MediaConfig`` 映射进 ``Manager`` 并装配按需初始化的 S3 后端
（``NewConfigured``，见 :func:`atri_x.media.manager.new_configured`）。本模块
承载其中的**配置映射**部分：

* :class:`MediaConfig` / :class:`FileDeliveryConfig` / :class:`PlatformFilesConfig`
  / :class:`SandboxConfig` —— 与 Go ``config`` 包里同名 struct 的字段一一对应
  （默认值取 ``config.Default()`` 的实际值，与 ``assets_data/app.toml`` 一致）。
* :class:`Config` —— ``atri_x.media`` 的聚合配置（Go 侧没有这个类型，Manager 直接
  持有两个 section）。
* :func:`config_from_app` —— **鸭子类型**读取 ``atri_x.app`` 的 ``AppConfig``。

集成期由 ``atri_x.app`` 注入
---------------------------
``atri_x.config`` 与 ``atri_x.media`` 是并行开发的兄弟模块，**本模块不 import
``atri_x.config``**：:func:`config_from_app` 只按属性名 ``getattr`` 逐字段取，
缺段/缺字段一律用默认值。集成期由 ``atri_x.app`` 在装配 media center 时把
``AppConfig`` 传进来（等价于 Go ``internal/app/runtime.go`` 里的
``media.NewConfigured(ctx, foundation.Store, ..., cfg.FileDelivery, creds)``）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

__all__ = [
    "Config",
    "FileDeliveryConfig",
    "MediaConfig",
    "PlatformFilesConfig",
    "SandboxConfig",
    "config_from_app",
    "default_config",
    "section_from",
]


@dataclass
class MediaConfig:
    """对应 Go ``config.MediaConfig``（``[media]`` 段）。"""

    llm_image_compression_threshold_bytes: int = 4 * 1024 * 1024
    llm_image_max_length: int = 4096


@dataclass
class FileDeliveryConfig:
    """对应 Go ``config.FileDeliveryConfig``（``[file_delivery]`` 段）。

    ``s3_access_key`` / ``s3_secret_key`` / ``s3_session_token`` 是 Python 侧追加的
    直填字段：Go 只认 ``S3AccessKeyEnv``/``S3SecretKeyEnv`` 指向的环境变量
    （凭据由 aws-sdk 的 provider 链提供）。本仓没有 SDK，凭据来源解析顺序见
    :func:`atri_x.media.s3.new_s3_backend`。
    """

    max_direct_base64_bytes: int = 8 * 1024 * 1024
    backend: str = "base64"
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    s3_access_key_env: str = ""
    s3_secret_key_env: str = ""
    s3_public_base_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_session_token: str = ""
    s3_path_style: bool = True


@dataclass
class PlatformFilesConfig:
    """对应 Go ``config.PlatformFilesConfig``（``[platform_files]`` 段）。"""

    max_receive_file_bytes: int = 100 * 1024 * 1024
    download_timeout_secs: int = 60


@dataclass
class SandboxConfig:
    """对应 Go ``config.SandboxConfig``（``[sandbox]`` 段）。

    媒体中心不用它做 IO；它的作用是让 ``atri_x.app`` 能按 Go 的口径
    ``filepath.Dir(cfg.Sandbox.Root)`` 推出媒体根目录（见 :func:`media_root`），
    并让维护任务把媒体清理挂到 sandbox 的清理时间表上。
    """

    root: str = ""


@dataclass
class Config:
    """``atri_x.media`` 的聚合配置（Python 侧类型；Go 无对应物）。

    ``root`` 为空时表示"由调用方决定"；:func:`media_root` 给出与 Go
    ``internal/app/runtime.go`` 相同的默认推导。
    """

    root: str = ""
    media: MediaConfig = field(default_factory=MediaConfig)
    file_delivery: FileDeliveryConfig = field(default_factory=FileDeliveryConfig)
    platform_files: PlatformFilesConfig = field(default_factory=PlatformFilesConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    @property
    def max_import_bytes(self) -> int:
        """默认导入硬上限（Go ``Manager.MaxImportBytes`` 的初始值）。"""
        return int(self.platform_files.max_receive_file_bytes)

    @property
    def download_timeout(self) -> float:
        """默认下载超时秒数（Go ``Manager.DownloadTimeout`` 的初始值）。"""
        return float(self.platform_files.download_timeout_secs)


def default_config() -> Config:
    """与 Go ``config.Default()`` 对齐的媒体相关默认值。"""
    return Config()


def media_root(cfg: Config) -> str:
    """按 Go 的口径推导媒体根目录：``filepath.Dir(cfg.Sandbox.Root)/media``。

    ``sandbox.root`` 为空时返回空串，由调用方决定（测试与嵌入式使用直接传 root）。
    """
    root = (cfg.root or "").strip()
    if root:
        return root
    sandbox_root = (cfg.sandbox.root or "").strip()
    if not sandbox_root:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(sandbox_root)), "media")


def section_from(source: Any, name: str, target: type) -> Any:
    """从 ``source`` 上按名字取一个配置段并映射成 ``target``（鸭子类型）。

    段缺失时返回 ``target()``；段是 Mapping 时按 ``to_<字段名>`` 取键；段是对象时
    按同名 snake_case 属性取。字段缺失或类型不符一律回落到默认值——配置映射**不该**
    成为启动失败点（与 Go 的 ``apply*Defaults`` 同精神）。
    """
    instance = target()
    section = getattr(source, name, None) if source is not None else None
    if section is None and isinstance(source, Mapping):
        section = source.get(name)
    if section is None:
        return instance
    for item in fields(instance):
        raw: Any = None
        found = False
        if isinstance(section, Mapping):
            if item.name in section:
                raw, found = section[item.name], True
        else:
            if hasattr(section, item.name):
                raw, found = getattr(section, item.name), True
        if not found or raw is None:
            continue
        try:
            setattr(instance, item.name, _coerce(raw, item.type, item.default))
        except (TypeError, ValueError):
            continue
    return instance


def _coerce(value: Any, declared: Any, default: Any) -> Any:
    """把配置值收敛到字段声明的形态（bool/int/float/str）。"""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return value if isinstance(value, str) else str(value)
    return value


def config_from_app(app_config: Any) -> Config:
    """把 ``atri_x.app`` 的 ``AppConfig`` 映射成 :class:`Config`（鸭子类型）。

    读取 ``[media]`` / ``[file_delivery]`` / ``[platform_files]`` / ``[sandbox]``
    四段；任一缺失用默认值。``app_config`` 既可以是对象（``AppConfig``），也可以是
    嵌套 Mapping（``{"media": {...}}``），便于测试与脚本直接喂 dict。
    """
    cfg = Config()
    cfg.media = section_from(app_config, "media", MediaConfig)
    cfg.file_delivery = section_from(app_config, "file_delivery", FileDeliveryConfig)
    cfg.platform_files = section_from(app_config, "platform_files", PlatformFilesConfig)
    cfg.sandbox = section_from(app_config, "sandbox", SandboxConfig)
    return cfg
