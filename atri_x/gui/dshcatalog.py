"""内置 DSH 内核的模型目录补丁（``cordis.patch.yml``）读写。

内核只认**它自己模型目录里**列出的 id；而 Anthropic 兼容网关（OpenCode Go 等）
实际提供的 id 往往和内核自带目录（``deepseek-flash`` / ``deepseek-v4-pro``）对不上，
id 对不上时请求直接 400 —— 用户侧表现为"深度路由从没生效"。

该补丁文件是内核自己的用户覆盖层（``cordis.yml`` 里写着 "Edit cordis.patch.yml,
not this file"），所以桌面端写它是受支持的路径。仓库零第三方依赖，因此这里自己
渲染/解析这一小段固定结构的 YAML（只认本模块写出来的结构，读取容错）。

写盘走 :mod:`atri_x.gui.configedit` 的约定：``.gui.bak`` 备份 + 原子替换。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    read_toml,
    write_text_file,
)
from atri_x.gui.configmodel import (
    load_context_windows,
    load_providers,
    parse_context_window,
)

__all__ = [
    "DEFAULT_KERNEL_MAX_TOKENS",
    "DEFAULT_KERNEL_PROVIDER",
    "DEFAULT_PROFILE",
    "KernelCatalog",
    "KernelModel",
    "catalog_from_provider",
    "clear_kernel_catalog",
    "default_context_window",
    "dsh_home",
    "load_kernel_catalog",
    "parse_kernel_catalog",
    "patch_path",
    "render_kernel_catalog",
    "save_kernel_catalog",
    "validate_catalog",
]

#: 内核 ACP profile 名（与 :data:`atri_x.sidecar.kernel.DEFAULT_PROFILE` 一致）。
DEFAULT_PROFILE = "acp"

#: 内核模型目录里的 provider 名：这是**内核自己的** provider 注册名，不是 ATRI-X 的
#: ``[providers.<name>]``；凭据靠环境变量注入，所以两套名字不必相同。
DEFAULT_KERNEL_PROVIDER = "deepseek-official"

#: 内核模型目录的默认输出上限（``maxTokens``）。与内置默认上下文窗口同口径，
#: 实测网关接受该值；用户可在「DSH」页重新生成时沿用补丁里已有的数字。
DEFAULT_KERNEL_MAX_TOKENS = 256000

#: 内置兜底上下文窗口（``[model_metadata].default_context_window`` 常见值）。
_FALLBACK_CONTEXT_WINDOW = 256000

_TOP_ID = re.compile(r"^- id:\s*(.+?)\s*$")
_FIELD = re.compile(r"^\s+(name|contextWindow|maxTokens|provider|model):\s*(.*?)\s*$")
_MODEL_ID = re.compile(r"^\s+-\s+id:\s*(.+?)\s*$")


@dataclass(frozen=True)
class KernelModel:
    """内核模型目录里的一条（写进 ``llm-deepseek`` 的 ``models`` 列表）。"""

    id: str
    name: str = ""
    context_window: int = _FALLBACK_CONTEXT_WINDOW
    max_tokens: int = DEFAULT_KERNEL_MAX_TOKENS

    def to_dict(self) -> dict[str, Any]:
        """界面/JSON 用的字典。"""
        return {
            "id": self.id,
            "name": self.name or self.id,
            "contextWindow": int(self.context_window),
            "maxTokens": int(self.max_tokens),
        }


@dataclass
class KernelCatalog:
    """内核模型目录补丁的解析结果。"""

    models: list[KernelModel] = field(default_factory=list)
    provider: str = DEFAULT_KERNEL_PROVIDER
    model: str = ""
    #: 补丁文件里是否有实际内容（不存在/``[]`` 都算"没有"）。
    present: bool = False

    def ids(self) -> list[str]:
        """模型 id 列表（保持文件顺序）。"""
        return [item.id for item in self.models]


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def dsh_home(paths: ConfigPaths) -> str:
    """解析 ``DSH_HOME``：``[agent.dsh].home``（相对配置目录）或 ``<配置目录>/dsh/home``。"""
    base = os.path.dirname(os.path.abspath(paths.state_path)) or paths.config_dir
    data = read_toml(paths.config_path)
    agent = data.get("agent")
    section = agent.get("dsh") if isinstance(agent, Mapping) else None
    raw = str((section or {}).get("home") or "").strip() if isinstance(section, Mapping) else ""
    path = raw or os.path.join("dsh", "home")
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def patch_path(paths: ConfigPaths) -> str:
    """内核 ACP profile 的补丁文件路径。"""
    return os.path.join(dsh_home(paths), "profiles", DEFAULT_PROFILE, "cordis.patch.yml")


# ---------------------------------------------------------------------------
# 读 / 生成 / 写
# ---------------------------------------------------------------------------
def load_kernel_catalog(paths: ConfigPaths) -> KernelCatalog:
    """读补丁文件里的模型目录；文件不存在/结构不认识时返回空目录。"""
    path = patch_path(paths)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return KernelCatalog()
    catalog = parse_kernel_catalog(text)
    catalog.present = text.strip() not in ("", "[]")
    return catalog


def default_context_window(paths: ConfigPaths) -> int:
    """``[model_metadata].default_context_window``；没配时用内置兜底值。"""
    try:
        window = load_context_windows(paths).default
    except ConfigEditError:
        window = 0
    return window if window > 0 else _FALLBACK_CONTEXT_WINDOW


def catalog_from_provider(
    paths: ConfigPaths,
    provider: str,
    *,
    existing: KernelCatalog | None = None,
    max_tokens: int = DEFAULT_KERNEL_MAX_TOKENS,
) -> KernelCatalog:
    """按某个 ATRI-X Provider 的模型清单生成内核目录。

    上下文窗口取 ``[providers.<p>.model_configs.<m>].context_window`` → 默认窗口；
    ``maxTokens`` 优先沿用补丁里已有的值（不悄悄改用户调过的数字）。
    """
    view = load_providers(paths).get(provider)
    if view is None:
        raise ConfigEditError(f"没有这个 Provider：{provider}")
    models = [str(item).strip() for item in (view.models or []) if str(item).strip()]
    if not models:
        raise ConfigEditError(
            f"Provider {provider} 没有配置 models（先在「模型」页给它填上模型名，再来生成）"
        )
    context_default = default_context_window(paths)
    windows = load_context_windows(paths).overrides
    keep = {item.id: item for item in ((existing or KernelCatalog()).models or [])}
    out: list[KernelModel] = []
    for model_id in models:
        previous = keep.get(model_id)
        window = windows.get((provider, model_id), 0) or context_default
        out.append(
            KernelModel(
                id=model_id,
                name=(previous.name if previous else model_id) or model_id,
                context_window=window,
                max_tokens=(previous.max_tokens if previous else max_tokens),
            )
        )
    previous_catalog = existing or KernelCatalog()
    return KernelCatalog(
        models=out,
        provider=previous_catalog.provider or DEFAULT_KERNEL_PROVIDER,
        model=previous_catalog.model if previous_catalog.model in models else out[0].id,
        present=True,
    )


def render_kernel_catalog(catalog: KernelCatalog) -> str:
    """把目录渲染成 ``cordis.patch.yml`` 文本（固定结构，UTF-8 + LF）。"""
    validate_catalog(catalog)
    model = catalog.model if catalog.model in catalog.ids() else catalog.models[0].id
    provider = catalog.provider or DEFAULT_KERNEL_PROVIDER
    lines = [
        "# 由 ATRI-X 桌面端「DSH」页生成（同目录 .gui.bak 是上一次写入前的内容）。",
        "# 内置 DSH 内核的模型目录补丁：内核只认这里列出的 id，",
        "# 请求会打到 <base_url>/v1/messages（Anthropic Messages 协议）。",
        "# 想换回内核自带目录（官方 DeepSeek 密钥）时，在「DSH」页点「清空（回内核自带目录）」。",
        "",
        "- id: llm-deepseek",
        "  config:",
        "    models:",
    ]
    for item in catalog.models:
        lines.extend(
            [
                f"      - id: {_yaml_scalar(item.id)}",
                f"        name: {_yaml_scalar(item.name or item.id)}",
                f"        contextWindow: {int(item.context_window)}",
                f"        maxTokens: {int(item.max_tokens)}",
            ]
        )
    for entry in ("agent-default-model", "acp"):
        lines.extend(
            [
                f"- id: {entry}",
                "  config:",
                f"    provider: {_yaml_scalar(provider)}",
                f"    model: {_yaml_scalar(model)}",
            ]
        )
    return "\n".join(lines + [""])


def parse_kernel_catalog(text: str) -> KernelCatalog:
    """解析补丁文本（只认 :func:`render_kernel_catalog` 那种结构，容错读）。"""
    catalog = KernelCatalog()
    top = ""
    current: dict[str, Any] | None = None
    raw_models: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            matched = _TOP_ID.match(line)
            top = _unquote(matched.group(1)) if matched else ""
            current = None
        elif top == "llm-deepseek":
            matched = _MODEL_ID.match(line)
            if matched:
                current = {"id": _unquote(matched.group(1))}
                raw_models.append(current)
            elif current is not None:
                _read_model_field(current, line)
        else:
            matched = _FIELD.match(line)
            if matched and top in ("agent-default-model", "acp"):
                name, value = matched.group(1), _unquote(matched.group(2))
                if name == "provider" and value:
                    catalog.provider = value
                elif name == "model" and value:
                    catalog.model = value
    catalog.models = [
        KernelModel(
            id=item["id"],
            name=_as_text(item.get("name")) or item["id"],
            context_window=int(item.get("contextWindow") or _FALLBACK_CONTEXT_WINDOW),
            max_tokens=int(item.get("maxTokens") or DEFAULT_KERNEL_MAX_TOKENS),
        )
        for item in raw_models
        if _as_text(item.get("id"))
    ]
    return catalog


def _read_model_field(current: dict[str, Any], line: str) -> None:
    """把一行 ``name:`` / ``contextWindow:`` / ``maxTokens:`` 写进解析中的模型字典。"""
    matched = _FIELD.match(line)
    if not matched:
        return
    name, value = matched.group(1), _unquote(matched.group(2))
    if name == "name":
        current["name"] = value
        return
    if name in ("contextWindow", "maxTokens"):
        try:
            current[name] = parse_context_window(value)
        except ConfigEditError:
            return


def validate_catalog(catalog: KernelCatalog) -> None:
    """校验目录：至少一个模型、id 非空且不重复、数字为正。"""
    if not catalog.models:
        raise ConfigEditError("模型目录不能为空；想恢复内核自带目录请用「清空（回内核自带目录）」")
    seen: set[str] = set()
    for item in catalog.models:
        model_id = (item.id or "").strip()
        if not model_id:
            raise ConfigEditError("模型 id 不能为空")
        if model_id in seen:
            raise ConfigEditError(f"模型 id 重复：{model_id}")
        seen.add(model_id)
        if item.context_window <= 0 or item.max_tokens <= 0:
            raise ConfigEditError(f"模型 {model_id} 的上下文窗口/输出上限必须是正数")
    if catalog.model and catalog.model not in seen:
        raise ConfigEditError(f"默认模型 {catalog.model} 不在目录列表里")


def save_kernel_catalog(paths: ConfigPaths, catalog: KernelCatalog) -> bool:
    """写内核模型目录补丁（带 ``.gui.bak`` 与原子替换），返回是否改动。"""
    validate_catalog(catalog)
    text = render_kernel_catalog(catalog)
    if parse_kernel_catalog(text).ids() != catalog.ids():
        raise ConfigEditError("生成的模型目录自检失败（解析回来对不上），已放弃写入")
    path = patch_path(paths)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        raise ConfigEditError(f"创建内核 profile 目录失败：{exc}") from exc
    return write_text_file(path, text)


def clear_kernel_catalog(paths: ConfigPaths) -> bool:
    """清空模型目录（写 ``[]`` = 内核用自带目录；官方 DeepSeek 密钥场景用）。"""
    return write_text_file(patch_path(paths), "[]\n")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _yaml_scalar(value: str) -> str:
    """字符串始终引用，避免 YAML 隐式数字/布尔/null；仅支持单行文本。"""
    text = str(value or "")
    if any(ord(char) < 32 or ord(char) == 127 or char in "\u0085\u2028\u2029" for char in text):
        raise ConfigEditError("模型目录字段不能包含换行或控制字符")
    return "'" + text.replace("'", "''") + "'"


def _unquote(value: str) -> str:
    """去 YAML 引号（单/双引号，容错）。"""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        return inner.replace("''", "'") if text[0] == "'" else inner
    return text


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
