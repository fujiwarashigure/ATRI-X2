"""桌面端「模型 → 搜索服务」页的数据层：读写 ``services.toml`` 的 ``[web_search]``。

配置形状（与 :class:`atri_x.config.services.WebSearchConfig` 一一对应）::

    [web_search]
    engine_order = ["tavily", "firecrawl", "keenable", "exa"]

    [web_search.engines.tavily]
    protocol = "tavily"
    api_key_env = "TAVILY_API_KEY"
    base_url = ""

这里只做四件事：列出现有通道、给单条通道读写字段、保存顺序、算密钥状态。
密钥**值**不进 ``services.toml``——和 provider 一样只写环境变量名，明文写配置根的
``.env``（由 :mod:`atri_x.gui.configedit` 负责），界面只显示「已设置/未设置 + 掩码」。
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
from atri_x.tool.builtin.web_search_engines import (
    BUILTIN_PROTOCOLS,
    DEFAULT_KEY_ENV,
    PROTOCOL_ANTIGRAVITY,
    PROTOCOL_EXA,
    PROTOCOL_FIRECRAWL,
    PROTOCOL_GROK,
    PROTOCOL_KEENABLE,
    PROTOCOL_SEARXNG,
    PROTOCOL_TAVILY,
)

__all__ = [
    "DEFAULT_SEARCH_ENGINES",
    "PROTOCOL_HINTS",
    "PROTOCOL_LABELS",
    "SearchEngineView",
    "engine_key_state",
    "load_engines",
    "save_engines",
    "validate_engine_name",
]

#: 协议在界面上的显示名。
PROTOCOL_LABELS: dict[str, str] = {
    PROTOCOL_TAVILY: "Tavily（要 key，带答案摘要）",
    PROTOCOL_FIRECRAWL: "Firecrawl（免注册 / 可填 key 提额）",
    PROTOCOL_KEENABLE: "Keenable（免注册 / 可填 key 提额）",
    PROTOCOL_EXA: "Exa（要 key）",
    PROTOCOL_SEARXNG: "SearXNG（自建实例）",
    PROTOCOL_ANTIGRAVITY: "Antigravity CLI（agy，需登录）",
    PROTOCOL_GROK: "Grok Build（grok，X 搜索）",
}

#: 每个协议的一句话说明（改协议时显示在表单下方）。
PROTOCOL_HINTS: dict[str, str] = {
    PROTOCOL_TAVILY: "官方端点 api.tavily.com，每月 1000 credits；返回 Answer 摘要。",
    PROTOCOL_FIRECRAWL: "不带 key 走免注册额度（按出口 IP 每月 1000 credits）；填 key 换成自己的。",
    PROTOCOL_KEENABLE: "不带 key 走免注册额度（按出口 IP 每小时 1000 次）；填 key 每月 10 万次。",
    PROTOCOL_EXA: "官方端点 api.exa.ai，需要 EXA_API_KEY（每月 $10 免费额度）。",
    PROTOCOL_SEARXNG: "自建实例地址，例如 https://searx.example.com；程序会请求 /search?format=json。",
    PROTOCOL_ANTIGRAVITY: "需要本机装好 agy 并浏览器登录一次；可执行文件留空表示用 PATH 上的 agy。",
    PROTOCOL_GROK: "需要本机装好 grok 并用 SuperGrok / X Premium 登录；只搜 X。",
}

#: 服务地址输入框的占位提示（留空 = 用该协议的官方端点）。
_PROTOCOL_BASE_PLACEHOLDER: dict[str, str] = {
    PROTOCOL_TAVILY: "留空 = https://api.tavily.com/search",
    PROTOCOL_FIRECRAWL: "留空 = https://api.firecrawl.dev/v2/search",
    PROTOCOL_KEENABLE: "留空 = https://api.keenable.ai/v1/search(/public)",
    PROTOCOL_EXA: "留空 = https://api.exa.ai/search",
    PROTOCOL_SEARXNG: "必填：https://searx.example.com",
    PROTOCOL_ANTIGRAVITY: "不适用（CLI 通道）",
    PROTOCOL_GROK: "不适用（CLI 通道）",
}

#: 引擎名字允许的字符（必须能直接写进 ``[web_search.engines.<name>]``，所以不许带点）。
_ENGINE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: ``[web_search]`` 小节名。
_SECTION = "web_search"


@dataclass
class SearchEngineView:
    """``[web_search.engines.<name>]`` 在界面上需要编辑的字段。"""

    name: str
    protocol: str
    api_key_env: str = ""
    base_url: str = ""
    binary: str = ""

    def display(self) -> str:
        return PROTOCOL_LABELS.get(self.protocol, self.protocol)

    def base_placeholder(self) -> str:
        return _PROTOCOL_BASE_PLACEHOLDER.get(self.protocol, "")


def default_engines() -> list[SearchEngineView]:
    """默认通道链：与 ``web_search`` 的环境变量默认链一致。"""
    return [
        SearchEngineView(name=name, protocol=name, api_key_env=DEFAULT_KEY_ENV.get(name, ""))
        for name in (PROTOCOL_TAVILY, PROTOCOL_FIRECRAWL, PROTOCOL_KEENABLE, PROTOCOL_EXA)
    ]


DEFAULT_SEARCH_ENGINES = tuple(view.name for view in default_engines())


def validate_engine_name(name: str) -> str:
    """校验通道名字；返回去空白后的名字，非法时抛 :class:`ConfigEditError`。"""
    value = (name or "").strip()
    if not _ENGINE_NAME.match(value):
        raise ConfigEditError(
            "通道名只能用字母/数字/下划线/短横线，且以字母或数字开头（最长 64，不能带点）"
        )
    return value


def load_engines(paths: ConfigPaths) -> list[SearchEngineView]:
    """读 ``[web_search]``；段缺席或顺序为空时返回默认链。"""
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
    views: list[SearchEngineView] = []
    for name in names:
        entry = engines.get(name)
        entry = entry if isinstance(entry, Mapping) else {}
        protocol = _text(entry.get("protocol")) or (
            name.lower() if name.lower() in BUILTIN_PROTOCOLS else ""
        )
        api_key_env = _text(entry.get("api_key_env")) or DEFAULT_KEY_ENV.get(protocol, "")
        views.append(
            SearchEngineView(
                name=name,
                protocol=protocol,
                api_key_env=api_key_env,
                base_url=_text(entry.get("base_url")),
                binary=_text(entry.get("bin")),
            )
        )
    return views


def save_engines(paths: ConfigPaths, engines: list[SearchEngineView]) -> str:
    """把通道列表与顺序写回 ``services.toml``，返回实际写入的文件路径。

    已删除的通道对应小节会一并删掉；``engine_order`` 为空时页面本身不会走到这里
    （表单要求至少保留一条通道）。
    """
    target = paths.services_path
    if not target:
        raise ConfigEditError("找不到 services.toml，无法保存搜索服务")
    if not engines:
        raise ConfigEditError("至少要保留一条搜索通道")
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
                "api_key_env": view.api_key_env.strip() or DEFAULT_KEY_ENV.get(view.protocol, ""),
                "base_url": view.base_url.strip() or None,
                "bin": view.binary.strip() or None,
            },
            header_comment=f"搜索通道：{name}（桌面端「模型 → 搜索服务」页维护）",
        )
    return target


def engine_key_state(paths: ConfigPaths, view: SearchEngineView) -> KeyState:
    """算一条通道的密钥状态（进程环境变量优先，其次 ``.env``）。"""
    return secret_state(paths, view.api_key_env)


def _existing_engine_names(target: str) -> list[str]:
    """``[web_search.engines.*]`` 里现存的通道名。"""
    data = read_toml(target)
    section = data.get(_SECTION)
    section = section if isinstance(section, Mapping) else {}
    engines = section.get("engines")
    if not isinstance(engines, Mapping):
        return []
    return [str(name) for name, entry in engines.items() if isinstance(entry, Mapping)]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
