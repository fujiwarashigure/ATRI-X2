"""工具调用进度预览的默认文案与渲染（Python 侧新增，无 Go 对应）。

对应 Go ``internal/toolrun/runner.go`` 里 ``"正在调用 <tool>：<args>"`` 这段硬编码
文案。Python 版把它收成一个纯数据对象 :class:`PreviewTexts`：

* 由 ``[tool_preview]`` 配置构造（见 :meth:`PreviewTexts.from_config`）；
* 由 :mod:`atri_x.agent.chat_tools` 放进 ``RunRequest`` 传给工具运行器；
* 运行器（:mod:`atri_x.toolrun.runner`）只认这个数据对象，不依赖 Agent/配置层，
  因此 ``runner`` 的单测仍然可以脱离宿主跑。

两种风格：

* ``persona``（默认）—— 角色化短句，**不显示工具参数**（默认不泄露查询词/路径）；
* ``system`` —— 旧行为「正在调用 工具名：参数」，参数先紧凑化再按
  :data:`MAX_PREVIEW_RUNES` 截断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "DEFAULT_FAILURE_TEMPLATE",
    "DEFAULT_PREVIEW_TEXT",
    "DEFAULT_SEARCH_TEXT",
    "DEFAULT_WEB_EXTRACT_TEXT",
    "EXTRACT_TOOLS",
    "MAX_PREVIEW_RUNES",
    "SEARCH_TOOLS",
    "SYSTEM_TEMPLATE",
    "PreviewTexts",
    "default_failure_text",
    "default_preview_text",
]

#: 单条预览里展示工具参数的最大字符数（与原 ``runner.MAX_PREVIEW_RUNES`` 一致）。
MAX_PREVIEW_RUNES = 160

#: 会被换成「检索」文案的工具名（大小写不敏感）。
SEARCH_TOOLS = frozenset({"web_search", "search_chat_history"})

#: 会被换成「读取网页」文案的工具名。
EXTRACT_TOOLS = frozenset({"web_extract"})

DEFAULT_SEARCH_TEXT = "检索中……请稍候。"
DEFAULT_WEB_EXTRACT_TEXT = "正在读取页面内容。"
DEFAULT_PREVIEW_TEXT = "正在调用 {tool}……"
SYSTEM_TEMPLATE = "正在调用 {tool}：{args}"
DEFAULT_FAILURE_TEMPLATE = "{tool} 调用失败：{error}"


@dataclass(frozen=True)
class PreviewTexts:
    """一组可配置的预览文案（由 ``[tool_preview]`` 构造）。"""

    enabled: bool = True
    style: str = "persona"
    search_text: str = DEFAULT_SEARCH_TEXT
    extract_text: str = DEFAULT_WEB_EXTRACT_TEXT
    default_text: str = DEFAULT_PREVIEW_TEXT
    system_template: str = SYSTEM_TEMPLATE
    failure_template: str = DEFAULT_FAILURE_TEMPLATE

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Any) -> "PreviewTexts":
        """从 ``ToolPreviewConfig``（或等价映射/``None``）构造，缺项用默认值。"""
        data = _section_dict(config)
        return cls(
            enabled=bool(data.get("enabled", True)),
            style=_text(data.get("style"), "persona"),
            search_text=_text(data.get("search_text"), DEFAULT_SEARCH_TEXT),
            extract_text=_text(data.get("extract_text"), DEFAULT_WEB_EXTRACT_TEXT),
            default_text=_text(data.get("default_text"), DEFAULT_PREVIEW_TEXT),
            system_template=_text(data.get("system_template"), SYSTEM_TEMPLATE),
            failure_template=_text(
                data.get("failure_template"), DEFAULT_FAILURE_TEMPLATE
            ),
        ).normalized()

    def normalized(self) -> "PreviewTexts":
        style = (self.style or "").strip().lower()
        if style not in ("persona", "system"):
            style = "persona"
        return PreviewTexts(
            enabled=bool(self.enabled),
            style=style,
            search_text=(self.search_text or "").strip() or DEFAULT_SEARCH_TEXT,
            extract_text=(self.extract_text or "").strip()
            or DEFAULT_WEB_EXTRACT_TEXT,
            default_text=(self.default_text or "").strip() or DEFAULT_PREVIEW_TEXT,
            system_template=(self.system_template or "").strip() or SYSTEM_TEMPLATE,
            failure_template=(self.failure_template or "").strip()
            or DEFAULT_FAILURE_TEMPLATE,
        )

    # -- 渲染 ---------------------------------------------------------------
    def render(self, tool_name: str, arguments: str) -> str:
        """渲染一条工具调用预览文案。"""
        name = (tool_name or "").strip() or "tool"
        if self.style == "system":
            return _format(self.system_template, tool=name, args=arguments or "{}")
        lowered = name.casefold()
        if self.search_text and lowered in SEARCH_TOOLS:
            return _format(self.search_text, tool=name)
        if self.extract_text and lowered in EXTRACT_TOOLS:
            return _format(self.extract_text, tool=name)
        return _format(self.default_text, tool=name)

    def failure(self, tool_name: str, error: str) -> str:
        """渲染一条工具失败预览文案。"""
        name = (tool_name or "").strip() or "tool"
        return _format(self.failure_template, tool=name, error=str(error or "").strip())


def default_preview_text(tool_name: str, arguments: str) -> str:
    """内置默认预览文案（没有配置时与旧版逐字一致）。"""
    return PreviewTexts(style="system").render(tool_name, arguments)


def default_failure_text(tool_name: str, error: str) -> str:
    """内置默认失败预览文案。"""
    return PreviewTexts().failure(tool_name, error)


#: ``normalized()`` 链式展开的最大层数。见 :func:`_section_dict`。
_MAX_NORMALIZE_DEPTH = 3


def _section_dict(config: Any, _depth: int = 0) -> Mapping[str, Any]:
    """把 dataclass / 映射 / ``None`` 统一成键值映射。

    **不能**按 ``normalized()`` 的返回值无条件递归：``ToolPreviewConfig`` /
    ``PreviewTexts`` 这类 dataclass 的 ``normalized()`` 每次调用都返回**新实例**，
    ``value is not config`` 恒为真，递归会一路打到 ``RecursionError``——这正是
    1.0.5 后台服务启动即崩（``atri-x: maximum recursion depth exceeded``）、
    ``ws://127.0.0.1:32172/cli/v1/ws`` 从未监听的根因。

    因此只在两种情况下采用 ``normalized()`` 的结果：

    * 返回的是映射——直接采用（包装对象的常见形态）；
    * 返回的是**另一种类型**的对象——有界展开（最多 :data:`_MAX_NORMALIZE_DEPTH`
      层），同类型对象不递归，因为所需字段已经从 ``config`` 上取到了。
    """
    if config is None:
        return {}
    if isinstance(config, Mapping):
        return config
    out: dict[str, Any] = {}
    for name in (
        "enabled",
        "style",
        "search_text",
        "extract_text",
        "default_text",
        "system_template",
        "failure_template",
    ):
        if hasattr(config, name):
            out[name] = getattr(config, name)
    normalized = getattr(config, "normalized", None)
    if callable(normalized) and _depth < _MAX_NORMALIZE_DEPTH:
        try:
            value = normalized()
        except Exception:  # noqa: BLE001 - 配置对象实现有问题时退回原始字段
            value = None
        if isinstance(value, Mapping):
            return value
        if value is not None and value is not config and type(value) is not type(config):
            nested = _section_dict(value, _depth + 1)
            if nested:
                return nested
    return out


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _format(template: str, **values: str) -> str:
    """安全格式化：模板里出现未知占位符时原样返回，不让预览影响工具执行。"""
    try:
        return template.format(**values).strip()
    except (KeyError, IndexError, ValueError):
        return template.strip()
