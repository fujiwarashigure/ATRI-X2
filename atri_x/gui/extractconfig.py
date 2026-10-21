"""桌面端「模型 → 抓取服务」页的数据层：读写 ``services.toml`` 的 ``[web_extract]``。

配置形状（与 :class:`atri_x.config.services.WebExtractConfig` 一一对应）::

    [web_extract]
    engine_order = ["jina", "direct"]

    [web_extract.engines.deepseek]
    protocol = "deepseek"
    api_key_env = "DEEPSEEK_SEARCH_KEY"
    # base_url = ""      # 留空 = https://api.deepseek.com/v1/tools/fetch

    [web_extract.engines.jina]
    protocol = "jina"
    api_key_env = "JINA_API_KEY"

    [web_extract.engines.direct]
    protocol = "direct"

这里只做四件事：列出现有通道、给单条通道读写字段、保存顺序、算密钥状态。
密钥**值**不进 ``services.toml``——只写环境变量名，明文写配置根的 ``.env``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_toml,
    remove_section,
)
from atri_x.gui.configmodel import KeyState, secret_state
from atri_x.tool.builtin.web_extract_engines import (
    BUILTIN_PROTOCOLS,
    DEFAULT_KEY_ENV,
    KEYED_PROTOCOLS,
    PROTOCOL_DEEPSEEK,
    PROTOCOL_DIRECT,
    PROTOCOL_JINA,
    key_env_names,
)

__all__ = [
    "DEFAULT_EXTRACT_ENGINES",
    "PROTOCOL_HINTS",
    "PROTOCOL_LABELS",
    "ExtractEngineView",
    "engine_key_state",
    "load_engines",
    "save_engines",
    "validate_engine_name",
]

#: 协议在界面上的显示名。
PROTOCOL_LABELS: dict[str, str] = {
    PROTOCOL_DEEPSEEK: "DeepSeek 官方抓取（要 key，按次计费）",
    PROTOCOL_JINA: "Jina Reader（要 key）",
    PROTOCOL_DIRECT: "直接抓取（无需 key）",
}

#: 每个协议的一句话说明（改协议时显示在表单下方）。
PROTOCOL_HINTS: dict[str, str] = {
    PROTOCOL_DEEPSEEK: (
        "官方端点 api.deepseek.com/v1/tools/fetch，返回页面标题 + Markdown 正文；"
        "按成功请求计费 5 元 / 1000 次。密钥优先 DEEPSEEK_SEARCH_KEY，回落 DEEPSEEK_API_KEY。"
    ),
    PROTOCOL_JINA: "r.jina.ai 的 Reader 接口，需要 JINA_API_KEY；默认首选通道。",
    PROTOCOL_DIRECT: "直接 GET 目标网页并用标准库剥标签；不需要 key，但遇到反爬会拿不到正文。",
}

#: 服务地址输入框的占位提示（留空 = 用该协议的官方端点）。
_PROTOCOL_BASE_PLACEHOLDER: dict[str, str] = {
    PROTOCOL_DEEPSEEK: "留空 = https://api.deepseek.com/v1/tools/fetch",
    PROTOCOL_JINA: "留空 = https://r.jina.ai/",
    PROTOCOL_DIRECT: "不适用（直接 GET 目标网页）",
}

#: 引擎名字允许的字符（必须能直接写进 ``[web_extract.engines.<name>]``，所以不许带点）。
_ENGINE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: ``[web_extract]`` 小节名。
_SECTION = "web_extract"


@dataclass
class ExtractEngineView:
    """``[web_extract.engines.<name>]`` 在界面上需要编辑的字段。"""

    name: str
    protocol: str
    api_key_env: str = ""
    base_url: str = ""

    def display(self) -> str:
        return PROTOCOL_LABELS.get(self.protocol, self.protocol)

    def base_placeholder(self) -> str:
        return _PROTOCOL_BASE_PLACEHOLDER.get(self.protocol, "")


def default_engines() -> list[ExtractEngineView]:
    """默认通道链：Jina（有 key 才参与）→ 直接抓取，与工具层默认链一致。"""
    return [
        ExtractEngineView(name=name, protocol=name, api_key_env=DEFAULT_KEY_ENV.get(name, ""))
        for name in (PROTOCOL_JINA, PROTOCOL_DIRECT)
    ]


DEFAULT_EXTRACT_ENGINES = tuple(view.name for view in default_engines())


def validate_engine_name(name: str) -> str:
    """校验通道名字；返回去空白后的名字，非法时抛 :class:`ConfigEditError`。"""
    value = (name or "").strip()
    if not _ENGINE_NAME.match(value):
        raise ConfigEditError(
            "通道名只能用字母/数字/下划线/短横线，且以字母或数字开头（最长 64，不能带点）"
        )
    return value


def load_engines(paths: ConfigPaths) -> list[ExtractEngineView]:
    """读 ``[web_extract]``；段缺席或顺序为空时返回默认链。"""
    data = read_toml(paths.services_path) if paths.services_path else {}
    section = data.get(_SECTION)
    section = section if isinstance(section, Mapping) else {}
    order = section.get("engine_order")
    names = [str(item).strip() for item in order] if isinstance(order, (list, tuple)) else []
    names = [name for name in names if name]
    if not names:
        return default_engines()
    engines = section.get("engines")
    engines = engines if isinstance(engines, Mapping) else {}
    views: list[ExtractEngineView] = []
    for name in names:
        entry = engines.get(name)
        entry = entry if isinstance(entry, Mapping) else {}
        protocol = _text(entry.get("protocol")) or (
            name.lower() if name.lower() in BUILTIN_PROTOCOLS else ""
        )
        api_key_env = _text(entry.get("api_key_env")) or DEFAULT_KEY_ENV.get(protocol, "")
        views.append(
            ExtractEngineView(
                name=name,
                protocol=protocol,
                api_key_env=api_key_env,
                base_url=_text(entry.get("base_url")),
            )
        )
    return views


def save_engines(paths: ConfigPaths, engines: list[ExtractEngineView]) -> str:
    """把通道列表与顺序写回 ``services.toml``，返回实际写入的文件路径。"""
    target = paths.services_path
    if not target:
        raise ConfigEditError("找不到 services.toml，无法保存抓取服务")
    if not engines:
        raise ConfigEditError("至少要保留一条抓取通道")
    seen: set[str] = set()
    names: list[str] = []
    for view in engines:
        name = validate_engine_name(view.name)
        if name in seen:
            raise ConfigEditError(f"通道名重复：{name}")
        if view.protocol not in BUILTIN_PROTOCOLS:
            raise ConfigEditError(f"未知协议：{view.protocol}")
        seen.add(name)
        names.append(name)
    for name in _existing_engine_names(target):
        if name not in seen:
            remove_section(target, f"{_SECTION}.engines.{name}")
    patch_section(target, _SECTION, {"engine_order": names})
    for view in engines:
        name = validate_engine_name(view.name)
        patch_section(
            target,
            f"{_SECTION}.engines.{name}",
            {
                "protocol": view.protocol,
                "api_key_env": view.api_key_env.strip() or DEFAULT_KEY_ENV.get(view.protocol, "") or None,
                "base_url": view.base_url.strip() or None,
            },
            header_comment=f"抓取通道：{name}（桌面端「模型 → 抓取服务」页维护）",
        )
    return target


def engine_key_state(paths: ConfigPaths, view: ExtractEngineView) -> KeyState:
    """算一条通道的密钥状态（进程环境变量优先，其次 ``.env``）。

    按 :func:`key_env_names` 的优先级逐个看：任一已配置就算可用。
    """
    names = key_env_names(view.protocol, view.api_key_env)
    if not names:
        return secret_state(paths, "")
    first = secret_state(paths, names[0])
    for name in names:
        state = secret_state(paths, name)
        if state.configured:
            return state
    return first


def _existing_engine_names(target: str) -> list[str]:
    """``[web_extract.engines.*]`` 里现存的通道名。"""
    data = read_toml(target)
    section = data.get(_SECTION)
    section = section if isinstance(section, Mapping) else {}
    engines = section.get("engines")
    if not isinstance(engines, Mapping):
        return []
    return [str(name) for name, entry in engines.items() if isinstance(entry, Mapping)]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
