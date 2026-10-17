"""``discover_tool``：把可用工具/技能的简介或完整 schema 交给模型。

对应 Go：``internal/tool/discover.go``。

职责：

* :func:`new_discover_tool` —— 构造 ``discover_tool``；可选 ``before`` 钩子
  （Go 里是 ``skill.Manager.EnsureLoaded``）在每次调用前先跑。
* :class:`DiscoverFilter` + :func:`with_discover_filter` —— 让 Agent 能按
  群/scope 可见性再过滤一层，而 ``tool`` 包不必依赖 Agent 策略。
* :func:`discovery_content` —— 把发现结果渲染成回灌给 LLM 的文本。
* :func:`expand_discovery_tag_names` —— 把 tag 展开成工具名，使隐藏工具
  （例如 chat 组的 ``get_media``）也能被 ``discover_tool(name="<tag>")`` 发现。

与 Go 的差异：

* Go 的 ``context.Context`` → 鸭子类型 ``ctx``；``Registry.DiscoverDetails``
  在 Python 侧是 async，因此 ``Call`` 与 :func:`discovery_content` 都是 async。
* ``Result.data`` 在 Python 里是 ``str``（Go 是 ``[]byte``）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from atri_x.errors import InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.security import Actor, Policy, RoleUser, actor_from_context, default_policy, policy_from_context
from atri_x.tool.builder import Builder
from atri_x.tool.capability import ctx_value, ctx_with_value
from atri_x.tool.detail import (
    DetailBlock,
    DetailProvider,
    DiscoveryResult,
    DiscoveredTool,
    load_discovery_content,
    new_rule_card_formats_from_context,
    public_info,
    render_detail_blocks_with_context,
)
from atri_x.tool.sandbox import info_available_in_context
from atri_x.tool.tool import (
    METADATA_ACTIVATE_TOOLS,
    METADATA_SHOWN_RULE_CARD_FORMATS,
    CallRequest,
    Info,
    Result,
    normalize_names,
    can_access_tool,
)

#: ``discover_tool`` 的名字（Go 里是硬编码字面量）。
DISCOVER_TOOL_NAME = "discover_tool"

_DESCRIPTION = (
    "发现可用工具和技能。未知工具schema时使用此工具获取，已知不再次使用。"
    "不传 name/names 时列出工具简介，传 name/names 时返回指定工具及 schema或者skill描述。"
)

_NAME_DESCRIPTION = "可选，要查询详情的tool/skill名称。不传 name/names 则列出全部可见工具简介。"
_NAMES_DESCRIPTION = "可选，要批量查询详情的tool/skill名称列表。"

_DISCOVER_FILTER_KEY = object()

#: 对应 Go ``DiscoverFilter``：按当前 Agent 策略判断某个工具是否可见。
DiscoverFilter = Callable[[Any, Info], bool]


@dataclass
class DiscoverArgs:
    """对应 Go ``discoverArgs``。"""

    name: str = ""
    names: list[str] = field(default_factory=list)

    @staticmethod
    def parse(arguments: str) -> "DiscoverArgs":
        if not arguments:
            return DiscoverArgs()
        try:
            raw = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise InvalidError(f"parse discover_tool arguments: {exc}") from exc
        if raw is None:
            return DiscoverArgs()
        if not isinstance(raw, dict):
            raise InvalidError("parse discover_tool arguments: expected object")
        names = raw.get("names") or []
        if not isinstance(names, list):
            raise InvalidError("parse discover_tool arguments: names must be an array")
        return DiscoverArgs(
            name=raw.get("name") or "",
            names=[item for item in names if isinstance(item, str)],
        )


def with_discover_filter(ctx: Any, filter: DiscoverFilter | None) -> Any:
    """把群/scope 可见性过滤器挂到 ctx 上。对应 Go ``WithDiscoverFilter``。"""
    if filter is None:
        return ctx
    return ctx_with_value(ctx, _DISCOVER_FILTER_KEY, filter)


def discover_filter_allowed(ctx: Any, info: Info) -> bool:
    """对应 Go ``discoverFilterAllowed``。"""
    filter = ctx_value(ctx, _DISCOVER_FILTER_KEY)
    if not callable(filter):
        return True
    return bool(filter(ctx, info))


@dataclass
class DiscoverTool:
    """对应 Go ``discoverTool``。

    注意：工具名走 :meth:`name`（对应 Go 的 ``Name()`` 方法），因此这里**不能**
    再有一个叫 ``name`` 的字段——``dataclass`` 字段会遮蔽方法。
    """

    registry: Any = None
    before_discover: Callable[[Any], Any] | None = None

    def name(self) -> str:
        """``Tool`` 协议方法。对应 Go ``discoverTool.Name``。"""
        return DISCOVER_TOOL_NAME

    def info_value(self) -> Info:
        """对应 Go ``discoverTool.Info``（避开 ``Tool.info`` 协议方法名）。"""
        return (
            Builder(self.name())
            .description(_DESCRIPTION)
            .risk("safe")
            .build_info()
        )

    def info(self) -> Info:
        """``Tool`` 协议方法。对应 Go ``discoverTool.Info``。"""
        return self.info_value()

    def schema(self) -> ToolSchema:
        """对应 Go ``discoverTool.Schema``。"""
        return (
            Builder(self.name())
            .description(_DESCRIPTION)
            .string("name", _NAME_DESCRIPTION)
            .string_array("names", _NAMES_DESCRIPTION)
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        """对应 Go ``discoverTool.Call``。"""
        if self.before_discover is not None:
            result = self.before_discover(ctx)
            if hasattr(result, "__await__"):
                await result
        args = DiscoverArgs.parse(req.arguments or "")
        names = list(args.names)
        if args.name:
            names = [args.name] + names
        names = normalize_names(names)
        actor, policy = discovery_security(ctx)
        activate_tools: list[str] = []
        if not names:
            out: list[DiscoveredTool] = []
            for info_value in self.registry.list():
                if (
                    not info_value.hidden
                    and info_available_in_context(ctx, info_value)
                    and (
                        can_access_tool(actor, policy, info_value)
                        or info_value.name == DISCOVER_TOOL_NAME
                    )
                    and discover_filter_allowed(ctx, info_value)
                ):
                    out.append(DiscoveredTool(info=public_info(info_value)))
            result_value = DiscoveryResult(tools=out)
        else:
            names = expand_discovery_tag_names(
                self.registry, names, _allowed_predicate(ctx, actor, policy)
            )
            details, errors = await self.registry.discover_details(
                ctx, names, _allowed_predicate(ctx, actor, policy)
            )
            for discovered in details:
                if not discovered.detail:
                    continue
                target, ok = self.registry.get(discovered.info.name)
                if ok and target is not None and isinstance(target, DetailProvider):
                    activate_tools.extend(target.activate_tools())
            result_value = DiscoveryResult(tools=details, errors=errors)
            if not details and errors and len(names) == 1:
                raise InvalidError(
                    f"discover tool {errors[0].name!r}: {errors[0].reason}"
                )
        data = marshal_json_no_escape(result_value.to_json())
        metadata: dict[str, Any] = {}
        activate_tools = normalize_names(activate_tools)
        if activate_tools:
            metadata[METADATA_ACTIVATE_TOOLS] = activate_tools
        content = await discovery_content(ctx, self.registry, result_value)
        formats = new_rule_card_formats_from_context(ctx)
        if formats:
            metadata[METADATA_SHOWN_RULE_CARD_FORMATS] = formats
        # 普通工具的完整 schema 通过 data 交给 Agent 注入 top-level tools。
        # tool message 文本只返回简短「已发现工具」，避免上下文膨胀，
        # 也让后续请求保持稳定的工具注入顺序。
        return Result(content=content, data=data, metadata=metadata)


def new_discover_tool(
    registry: Any, before: Callable[[Any], Any] | None = None
) -> DiscoverTool:
    """对应 Go ``NewDiscoverTool``。"""
    return DiscoverTool(registry=registry, before_discover=before)


def _allowed_predicate(ctx: Any, actor: Actor, policy: Policy | None) -> Callable[[Any], bool]:
    def allowed(candidate: Any) -> bool:
        info_value = candidate.info()
        return (
            info_available_in_context(ctx, info_value)
            and can_access_tool(actor, policy, info_value)
            and discover_filter_allowed(ctx, info_value)
        )

    return allowed


def marshal_json_no_escape(value: Any) -> str:
    """对应 Go ``marshalJSONNoEscape``：不转义 HTML、紧凑输出。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


async def discovery_content(ctx: Any, registry: Any, result: DiscoveryResult | None) -> str:
    """对应 Go ``discoveryContent``。"""
    if result is None:
        return ""
    parts: list[str] = []
    found_tools: list[str] = []
    appended_extras: list[str] = []
    detail_blocks: list[DetailBlock] = []
    for discovered in result.tools:
        name = (discovered.info.name or "").strip()
        if discovered.detail:
            block = discovered.detail_block
            if not (block.content or "").strip():
                block = DetailBlock(content=discovered.detail)
            detail_blocks.append(block)
            continue
        if discovered.schema is not None and name:
            target, ok = registry.get(name)
            if ok and target is not None:
                content, override, supported = await load_discovery_content(ctx, target)
                if supported:
                    if (content or "").strip():
                        if override:
                            parts.append(content)
                        else:
                            found_tools.append(name)
                            appended_extras.append(content)
                        continue
            found_tools.append(name)
            continue
        if name:
            parts.append(f"{name}：{discovered.info.description}")
    if found_tools:
        parts.insert(0, f"已发现工具：{', '.join(found_tools)}。后续可直接调用。")
    parts.extend(appended_extras)
    detail_text = render_detail_blocks_with_context(ctx, detail_blocks)
    if detail_text:
        parts.append(detail_text)
    if result.errors:
        rendered: list[str] = []
        for item in result.errors:
            if not item.name:
                continue
            reason = (item.reason or "").strip() or "not found or not allowed"
            rendered.append(f"{item.name}：{reason}")
        if rendered:
            parts.append("未发现工具：" + ", ".join(rendered))
    return "\n\n".join(parts)


def discovery_security(ctx: Any) -> tuple[Actor, Policy]:
    """取本次发现使用的 actor 与 policy。对应 Go ``discoverySecurity``。"""
    policy, ok = policy_from_context(ctx)
    if not ok or policy is None:
        policy = default_policy()
    actor, ok = actor_from_context(ctx)
    if not ok or actor is None:
        actor = Actor(role=RoleUser)
    return actor, policy


def expand_discovery_tag_names(
    registry: Any, names: Sequence[str], allowed: Callable[[Any], bool]
) -> list[str]:
    """把不是工具名的条目当 tag 展开。对应 Go ``expandDiscoveryTagNames``。"""
    if registry is None or not names:
        return list(names)
    out: list[str] = []
    seen: dict[str, bool] = {}
    for name in names:
        name = (name or "").strip()
        if name == "" or seen.get(name):
            continue
        target, ok = registry.get(name)
        if ok and target is not None:
            seen[name] = True
            out.append(name)
            continue
        matches = registry.names_by_tag(name, allowed)
        if not matches:
            seen[name] = True
            out.append(name)
            continue
        for match in matches:
            if seen.get(match):
                continue
            seen[match] = True
            out.append(match)
    return out


__all__ = [
    "DISCOVER_TOOL_NAME",
    "DiscoverArgs",
    "DiscoverFilter",
    "DiscoverTool",
    "discover_filter_allowed",
    "discovery_content",
    "discovery_security",
    "expand_discovery_tag_names",
    "marshal_json_no_escape",
    "new_discover_tool",
    "with_discover_filter",
]
