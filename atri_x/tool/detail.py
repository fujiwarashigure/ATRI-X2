"""工具详情的结构化块、发现结果、发现文案提供者与规则卡去重。

对应 Go：``internal/tool/detail.go``，外加 ``internal/tool/tool.go`` 里的
``DetailBlock`` / ``DetailProvider`` / ``StructuredDetailProvider`` /
``LazyDetailProvider`` / ``DiscoveryContentProvider`` /
``ContextDiscoveryContentProvider`` / ``LoadDiscoveryContent`` /
``PublicInfo`` / ``DiscoveredTool`` / ``DiscoveryResult`` / ``DiscoveryError``
/ ``publicInfo``。

拆分说明：Go 侧这些类型都写在 ``tool.go`` 里（该文件 597 行）。Python 侧受
"单文件 ≤ 600 行"约束，把"详情块 + 发现结果"这一职责整体挪到本模块；行为逐条
对齐 Go 源码，调用点仍是 ``Registry.discover`` / ``Registry.discover_details``。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from atri_x.llm.llm import ToolSchema
from atri_x.tool.capability import ctx_value, ctx_with_value

_RULE_CARD_STATE_KEY = object()


@dataclass
class DetailBlock:
    """一次工具发现要渲染的详情块。对应 Go ``DetailBlock``。"""

    content: str = ""
    format: str = ""
    rule_card: str = ""


@runtime_checkable
class DetailProvider(Protocol):
    """自带详情文本的工具（Skill 类工具即以此区分）。对应 Go ``DetailProvider``。"""

    def detail(self) -> str: ...

    def activate_tools(self) -> list[str]: ...


@runtime_checkable
class StructuredDetailProvider(Protocol):
    """用结构化块而不是裸文本提供详情的工具。对应 Go ``StructuredDetailProvider``。"""

    def detail_block(self) -> DetailBlock: ...


@runtime_checkable
class LazyDetailProvider(Protocol):
    """只在被发现/预载时才加载详情的工具。对应 Go ``LazyDetailProvider``。

    ctx 里带着调用者 actor，因为部分详情会追加与角色相关的提示。
    """

    async def load_detail(self, ctx: Any) -> DetailBlock: ...


@runtime_checkable
class DiscoveryContentProvider(Protocol):
    """自定义"被发现时的展示文本"的工具。对应 Go ``DiscoveryContentProvider``。

    override=True：该工具从"已发现工具"行里移出，内容单独成块；
    override=False：仍留在那一行，内容追加在行后单独成块。
    """

    def discovery_content(self) -> tuple[str, bool]: ...


@runtime_checkable
class ContextDiscoveryContentProvider(Protocol):
    """展示文本依赖当前工具执行上下文（例如激活的 workspace）。"""

    async def discovery_content(self, ctx: Any) -> tuple[str, bool]: ...


async def load_discovery_content(ctx: Any, target: Any) -> tuple[str, bool, bool]:
    """对应 Go ``LoadDiscoveryContent``。

    返回 ``(content, override, supported)``；``supported`` 为 False 表示该工具
    不提供自定义发现文案。Go 的第三个返回值是 error，Python 侧改为抛异常。

    Go 用两个接口（带 ctx / 不带 ctx）区分调用形态；``runtime_checkable`` 的
    Protocol 只看方法名，因此这里按方法签名判定是否接收 ctx。
    """
    provider = getattr(target, "discovery_content", None)
    if not callable(provider):
        return "", False, False
    if _accepts_context_arg(provider):
        content, override = await provider(ctx)
        return content, override, True
    content, override = provider()
    return content, override, True


def _accepts_context_arg(fn: Any) -> bool:
    """该可调用对象是否还能接收一个 ctx 参数（Go 的 ctx 版本接口）。

    传进来的是绑定方法，``inspect.signature`` 已经不含 ``self``，因此这里不跳过
    首个参数。
    """
    import inspect

    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - 内建/无法内省
        return True
    return any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.KEYWORD_ONLY,
        )
        for parameter in parameters
    )


# ---------------------------------------------------------------------------
# 发现结果（Go 侧同样定义在 tool.go）
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryError:
    """对应 Go ``DiscoveryError``。"""

    name: str = ""
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "reason": self.reason}


@dataclass
class PublicInfo:
    """对应 Go ``PublicInfo``。"""

    name: str = ""
    description: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)
    foreground_only: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
        }
        if self.tags:
            out["tags"] = list(self.tags)
        if self.foreground_only:
            out["foreground_only"] = True
        return out


@dataclass
class DiscoveredTool:
    """对应 Go ``DiscoveredTool``。``detail_block`` 不参与 JSON（Go ``json:"-"``）。"""

    info: PublicInfo = field(default_factory=PublicInfo)
    schema: ToolSchema | None = None
    detail: str = ""
    detail_block: DetailBlock = field(default_factory=DetailBlock)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"info": self.info.to_json()}
        if self.schema is not None:
            out["schema"] = dataclasses.asdict(self.schema)
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class DiscoveryResult:
    """对应 Go ``DiscoveryResult``。"""

    tools: list[DiscoveredTool] = field(default_factory=list)
    errors: list[DiscoveryError] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.tools:
            out["tools"] = [tool.to_json() for tool in self.tools]
        if self.errors:
            out["errors"] = [err.to_json() for err in self.errors]
        return out


def public_info(info: Any) -> PublicInfo:
    """对应 Go ``publicInfo``。"""
    from atri_x.tool.tool import normalize_tags

    return PublicInfo(
        name=info.name,
        description=info.description,
        source=str(info.source),
        tags=normalize_tags(info.tags),
        foreground_only=bool(info.foreground_only),
    )


# ---------------------------------------------------------------------------
# 发现遍历
#
# Go 侧是 ``(*Registry).DiscoverDetails`` / ``addDiscoveryDetail``（写在 tool.go 里）。
# 这里放成自由函数、由 Registry 委托，既避免 Registry 过长，也不引入循环 import：
# 只需要 registry 暴露 ``get(name) -> (Tool, bool)``。
# ---------------------------------------------------------------------------

def normalize_names(names: Sequence[str] | None) -> list[str]:
    """对应 Go ``normalizeNames``：trim + 去重 + 保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        name = (name or "").strip()
        if name == "" or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


async def discover_details(
    registry: Any, ctx: Any, names: Sequence[str] | None, allowed: Any = None
) -> tuple[list[DiscoveredTool], list[DiscoveryError]]:
    """对应 Go ``Registry.DiscoverDetails``。"""
    if allowed is None:
        allowed = _allow_all_tools
    seen: dict[str, bool] = {}
    visiting: dict[str, bool] = {}
    details: list[DiscoveredTool] = []
    errors: list[DiscoveryError] = []
    for name in normalize_names(names):
        details, errors = await add_discovery_detail(
            registry, ctx, name, True, allowed, seen, visiting, details, errors
        )
    return details, errors


async def add_discovery_detail(
    registry: Any,
    ctx: Any,
    name: str,
    root: bool,
    allowed: Any,
    seen: dict[str, bool],
    visiting: dict[str, bool],
    details: list[DiscoveredTool],
    errors: list[DiscoveryError],
) -> tuple[list[DiscoveredTool], list[DiscoveryError]]:
    """对应 Go ``Registry.addDiscoveryDetail``（含 ``DependsOn`` 递归）。

    ``visiting`` 只用于切断环；``seen`` 一旦写入就不再重试——包括详情加载失败的
    工具，与 Go 的行为一致。
    """
    name = (name or "").strip()
    if name == "" or seen.get(name) or visiting.get(name):
        return details, errors
    tool, ok = registry.get(name)
    if not ok or tool is None or not allowed(tool) or (root and tool.info().hidden):
        if root:
            errors.append(DiscoveryError(name=name, reason="not found or not allowed"))
        return details, errors
    visiting[name] = True
    seen[name] = True
    info = tool.info()
    discovered = DiscoveredTool(info=public_info(info))
    if isinstance(tool, LazyDetailProvider):
        try:
            block = await tool.load_detail(ctx)
        except BaseException as exc:
            errors.append(DiscoveryError(name=name, reason=str(exc)))
            visiting.pop(name, None)
            return details, errors
        discovered.detail_block = block
        discovered.detail = render_detail_blocks_with_context(ctx, [block])
    elif isinstance(tool, DetailProvider):
        if isinstance(tool, StructuredDetailProvider):
            discovered.detail_block = tool.detail_block()
            discovered.detail = render_detail_blocks_with_context(
                ctx, [discovered.detail_block]
            )
        else:
            discovered.detail = tool.detail()
            discovered.detail_block = DetailBlock(content=discovered.detail)
    else:
        discovered.schema = tool.schema()
    details.append(discovered)
    for dep in info.depends_on:
        details, errors = await add_discovery_detail(
            registry, ctx, dep, False, allowed, seen, visiting, details, errors
        )
    visiting.pop(name, None)
    return details, errors


def _allow_all_tools(_tool: Any) -> bool:
    return True


# ---------------------------------------------------------------------------
# 规则卡去重状态：对应 Go 的 ruleCardState + context key
# ---------------------------------------------------------------------------

@dataclass
class RuleCardState:
    """对应 Go ``ruleCardState``。"""

    shown: dict[str, bool] = field(default_factory=dict)
    new: dict[str, bool] = field(default_factory=dict)


def with_shown_rule_card_formats(ctx: Any, formats: Sequence[str] | None) -> Any:
    """对应 Go ``WithShownRuleCardFormats``。返回新的 ctx。"""
    state = RuleCardState()
    for fmt in formats or []:
        fmt = (fmt or "").strip()
        if fmt:
            state.shown[fmt] = True
    return ctx_with_value(ctx, _RULE_CARD_STATE_KEY, state)


def new_rule_card_formats_from_context(ctx: Any) -> list[str]:
    """对应 Go ``NewRuleCardFormatsFromContext``。"""
    state = _rule_card_state(ctx)
    if state is None or not state.new:
        return []
    return sorted(fmt for fmt in state.new if fmt)


def _rule_card_state(ctx: Any) -> RuleCardState | None:
    state = ctx_value(ctx, _RULE_CARD_STATE_KEY)
    return state if isinstance(state, RuleCardState) else None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_detail_blocks(blocks: Sequence[DetailBlock] | None) -> str:
    """对应 Go ``RenderDetailBlocks``（无上下文版本）。"""
    return _render_detail_blocks(None, blocks)


def render_detail_blocks_with_context(ctx: Any, blocks: Sequence[DetailBlock] | None) -> str:
    """对应 Go ``RenderDetailBlocksWithContext``。"""
    return _render_detail_blocks(ctx, blocks)


def _render_detail_blocks(ctx: Any, blocks: Sequence[DetailBlock] | None) -> str:
    parts: list[str] = []
    seen_rules: dict[str, bool] = {}
    for block in blocks or []:
        content = (block.content or "").strip()
        if not content:
            continue
        rule = (block.rule_card or "").strip()
        if rule:
            key = rule_card_key(block, rule)
            if not seen_rules.get(key):
                seen_rules[key] = True
                if _should_render_rule_card(ctx, key):
                    parts.append(rule)
        parts.append(content)
    return "\n\n---\n\n".join(parts)


def rule_card_key(block: DetailBlock, rule: str) -> str:
    """对应 Go ``ruleCardKey``。"""
    key = (block.format or "").strip()
    if not key:
        key = (rule or "").strip()
    return key


def _should_render_rule_card(ctx: Any, key: str) -> bool:
    if ctx is None or not key:
        return True
    state = _rule_card_state(ctx)
    if state is None:
        return True
    if state.shown.get(key):
        return False
    state.shown[key] = True
    state.new[key] = True
    return True


__all__ = [name for name in dir() if not name.startswith("_")]
