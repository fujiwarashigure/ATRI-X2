"""桌面端「模型」页的数据模型：LLM Provider 与模型分配。

两件事：

1. **Provider**（``services.toml`` 的 ``[providers.<name>]``，旧部署退回
   ``providers.toml``）：``base_url`` / ``api_key_env`` / ``models`` / ``api_mode``。
   密钥值不写 TOML，只写配置根 ``.env``；这里只负责"读状态 + 掩码展示"。
2. **模型分配**（``state.toml`` 的 ``[mode_models.*]`` / ``[naming_model]`` /
   ``[compact_model]`` / ``[session].default_mode``）：运行期回写的文件，后台服务
   会热加载，保存后几秒生效。

读写原语在 :mod:`atri_x.gui.configedit`（行级补丁 + 备份 + 校验）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_env,
    read_toml,
    remove_section,
)

__all__ = [
    "KeyState",
    "MODEL_SLOTS",
    "ModelSlot",
    "ProviderView",
    "delete_provider",
    "load_model_selection",
    "load_providers",
    "mask_secret",
    "provider_key_state",
    "save_model_selection",
    "save_provider",
    "secret_state",
    "secret_value",
    "validate_provider_name",
]

#: Provider 名字允许的字符（同时要能直接写进 ``[providers.<name>]``）。
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: provider 所在小节前缀。
_PROVIDERS_SECTION = "providers"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
@dataclass
class ProviderView:
    """``[providers.<name>]`` 在界面上需要编辑的字段。"""

    name: str
    base_url: str = ""
    api_key_env: str = ""
    models: list[str] = field(default_factory=list)
    api_mode: str = ""


@dataclass(frozen=True)
class KeyState:
    """某个 provider 密钥的配置状态（不回显明文）。"""

    name: str = ""
    in_process_env: bool = False
    in_dotenv: bool = False
    masked: str = ""

    @property
    def configured(self) -> bool:
        return self.in_process_env or self.in_dotenv

    def describe(self) -> str:
        if not self.name:
            return "未配置 api_key_env"
        suffix = f"（{self.masked}）" if self.masked else ""
        if self.in_process_env:
            return f"已设置：来自当前进程环境变量{suffix}"
        if self.in_dotenv:
            return f"已设置：来自 .env{suffix}"
        return "未设置（在下方填密钥，保存时写入 .env）"


def validate_provider_name(name: str) -> str:
    """校验 provider 名字；返回去空白后的名字，非法时抛 :class:`ConfigEditError`。"""
    value = (name or "").strip()
    if not _PROVIDER_NAME.match(value):
        raise ConfigEditError(
            "provider 名字只能用字母/数字/下划线/短横线/点，且以字母或数字开头（最长 64）"
        )
    return value


def load_providers(paths: ConfigPaths) -> dict[str, ProviderView]:
    """读 provider 列表（``services.toml`` 优先，退回旧的 ``providers.toml``）。"""
    for candidate in (paths.services_path, os.path.join(paths.config_dir, "providers.toml")):
        if not candidate or not os.path.exists(candidate):
            continue
        data = read_toml(candidate)
        providers = data.get(_PROVIDERS_SECTION)
        if isinstance(providers, Mapping) and providers:
            return _providers_from(providers)
    return {}


def _providers_from(raw: Mapping[str, Any]) -> dict[str, ProviderView]:
    result: dict[str, ProviderView] = {}
    for name, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        models = entry.get("models")
        model_list = [str(item) for item in models] if isinstance(models, (list, tuple)) else []
        result[str(name)] = ProviderView(
            name=str(name),
            base_url=_text(entry.get("base_url")),
            api_key_env=_text(entry.get("api_key_env")),
            models=model_list,
            api_mode=_text(entry.get("api_mode")),
        )
    return result


def provider_key_state(paths: ConfigPaths, view: ProviderView) -> KeyState:
    """算某个 provider 的密钥状态（进程环境变量优先，其次 ``.env``）。"""
    return secret_state(paths, view.api_key_env)


def secret_state(paths: ConfigPaths, env_name: str) -> KeyState:
    """算任意一个"环境变量名 → 密钥"的状态（平台 token、client secret 也用）。"""
    name = (env_name or "").strip()
    if not name:
        return KeyState()
    in_process = bool((os.environ.get(name) or "").strip())
    try:
        dotenv = read_env(paths.env_path)
    except ConfigEditError:
        dotenv = {}
    plain = (os.environ.get(name) or "").strip() or (dotenv.get(name) or "").strip()
    return KeyState(
        name=name,
        in_process_env=in_process,
        in_dotenv=bool((dotenv.get(name) or "").strip()),
        masked=mask_secret(plain),
    )


def secret_value(paths: ConfigPaths, env_name: str) -> str:
    """取一个环境变量名的**明文**值（进程环境优先，其次 ``.env``）。

    只给「获取可用模型目录 / 下载头像」这类本机即时调用用：它们要真的带上密钥去请求
    上游，所以不能只拿掩码。界面上仍然不回显明文。
    """
    name = (env_name or "").strip()
    if not name:
        return ""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        dotenv = read_env(paths.env_path)
    except ConfigEditError:
        return ""
    return (dotenv.get(name) or "").strip()


def save_provider(paths: ConfigPaths, view: ProviderView, *, original: str = "") -> str:
    """保存一个 provider，返回实际写入的文件路径。

    ``original`` 非空且与新名字不同表示**改名**：先删旧小节再写新的。
    """
    name = validate_provider_name(view.name)
    target = _providers_path(paths)
    values: dict[str, Any] = {
        "base_url": view.base_url.strip() or None,
        "api_key_env": view.api_key_env.strip() or None,
        "models": list(view.models) or None,
        "api_mode": view.api_mode.strip() or None,
    }
    old = (original or "").strip()
    if old and old != name:
        remove_section(target, f"{_PROVIDERS_SECTION}.{old}")
    patch_section(
        target,
        f"{_PROVIDERS_SECTION}.{name}",
        values,
        header_comment=f"桌面端添加的 LLM Provider：{name}",
    )
    return target


def delete_provider(paths: ConfigPaths, name: str) -> str:
    """删除一个 provider 小节（含 ``.model_configs`` 之类子表），返回文件路径。"""
    target = _providers_path(paths)
    section = f"{_PROVIDERS_SECTION}.{validate_provider_name(name)}"
    if not remove_section(target, section):
        raise ConfigEditError(f"{os.path.basename(target)} 里没有 [providers.{name}]")
    return target


def _providers_path(paths: ConfigPaths) -> str:
    """provider 写回目标：优先 ``services.toml``；只有旧 ``providers.toml`` 时写旧的。"""
    services = paths.services_path
    if os.path.exists(services):
        return services
    legacy = os.path.join(paths.config_dir, "providers.toml")
    if paths.services_is_legacy_providers or os.path.exists(legacy):
        return legacy
    return services


# ---------------------------------------------------------------------------
# 模型分配（state.toml）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSlot:
    """一个模型槽位：``state.toml`` 里的哪一段、界面上叫什么。"""

    key: str
    label: str
    section: str


#: 界面上可编辑的模型槽位（顺序即展示顺序）。
MODEL_SLOTS: tuple[ModelSlot, ...] = (
    ModelSlot("work", "工作模式（工具/复杂任务）", "mode_models.work"),
    ModelSlot("chat", "聊天模式（日常对话）", "mode_models.chat"),
    ModelSlot("naming", "命名模型（会话标题）", "naming_model"),
    ModelSlot("compact", "压缩模型（上下文压缩）", "compact_model"),
)


def load_model_selection(paths: ConfigPaths) -> tuple[str, dict[str, tuple[str, str]]]:
    """读默认模式与各槽位的模型分配，返回 ``(default_mode, {slot: (provider, model)})``。"""
    data = read_toml(paths.state_path) if os.path.exists(paths.state_path) else {}
    session = data.get("session")
    default_mode = _text(session.get("default_mode")) if isinstance(session, Mapping) else ""
    values: dict[str, tuple[str, str]] = {}
    for slot in MODEL_SLOTS:
        head, _, tail = slot.section.partition(".")
        entry = data.get(head)
        if tail and isinstance(entry, Mapping):
            entry = entry.get(tail)
        if isinstance(entry, Mapping):
            values[slot.key] = (_text(entry.get("provider")), _text(entry.get("model")))
        else:
            values[slot.key] = ("", "")
    return default_mode, values


def save_model_selection(
    paths: ConfigPaths,
    default_mode: str,
    values: Mapping[str, tuple[str, str]],
) -> str:
    """写回默认模式与各槽位（只写非空的 provider/model），返回 ``state.toml`` 路径。"""
    target = paths.state_path
    if (default_mode or "").strip():
        patch_section(target, "session", {"default_mode": default_mode.strip()})
    for slot in MODEL_SLOTS:
        if slot.key not in values:
            continue
        provider, model = values[slot.key]
        provider = (provider or "").strip()
        model = (model or "").strip()
        if not provider and not model:
            continue
        payload: dict[str, Any] = {}
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        patch_section(target, slot.section, payload, header_comment="桌面端设置的模型分配")
    return target


def mask_secret(value: str) -> str:
    """把密钥渲染成掩码（只留头尾各 4 个字符）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "****"
    return f"{text[:4]}…{text[-4:]}"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
