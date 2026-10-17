"""Tool Runtime 核心类型：工具契约、风险等级、注册表与工具发现。

对应 Go：``internal/tool/tool.go``。

本模块是 PORTING.md §3 指定的跨模块契约：
``Tool`` / ``Info`` / ``Result`` / ``RiskLevel`` / schema 助手。

与 Go 的差异（仅形式）：

* Go 的 ``(T, error)`` → 抛异常；``Registry`` 是纯内存结构，保留同步接口
  （对应 Go 的 ``sync.RWMutex`` → ``threading.Lock``）。
* ``context.Context`` → 鸭子类型 ``ctx``：本包统一使用 ``Mapping``（``dict``），
  ``None`` 视为空上下文；键是模块级私有哨兵对象，不同来源的 ctx 可安全合并。
* ``json.RawMessage`` → ``str``（JSON 文本）。
* 风险等级与访问策略原属 ``internal/security/security.go``（PORTING.md §3 把
  ``RiskLevel`` 归到本模块）；定义见 :mod:`atri_x.tool.capability`，本模块原样
  再导出，``atri_x.security`` 落地后由它接管。
* 发现遍历与发现结果类型在 :mod:`atri_x.tool.detail`（单文件 ≤ 600 行的拆分），
  ``Registry.discover*`` 只是委托。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from atri_x.delivery import Output
from atri_x.errors import InvalidError, NotFoundError
from atri_x.llm.llm import (
    MessageSegment,
    ToolSchema,
    append_segment_text,
    text_segments,
)
from atri_x.tool.capability import (  # noqa: F401  (再导出：契约要求的 RiskLevel 等)
    Actor,
    ActorFromContext,
    ActorID,
    Capabilities,
    CompareRisk,
    DefaultPolicy,
    GroupRole,
    GroupRoleAdmin,
    GroupRoleMember,
    GroupRoleOwner,
    GroupRoleUnknown,
    ParseGroupRole,
    ParseRisk,
    Policy,
    PolicyFromContext,
    RiskCritical,
    RiskHigh,
    RiskLevel,
    RiskLow,
    RiskMedium,
    RiskSafe,
    Role,
    RoleSuperadmin,
    RoleUser,
    WithActor,
    WithPolicy,
    capabilities_from_context,
    compare_risk,
    default_policy,
    parse_risk,
    vision_available,
    with_capabilities,
)
from atri_x.tool.detail import (
    DetailBlock,
    DetailProvider,
    DiscoveredTool,
    DiscoveryError,
    DiscoveryResult,
    LazyDetailProvider,
    PublicInfo,
    StructuredDetailProvider,
    discover_details as _discover_details,
    public_info,
)


class Source:
    """工具来源。对应 Go ``Source``。"""

    BUILTIN = "builtin"
    SKILL_AGENT = "skill_agent"
    SKILL_GO = "skill_go"

    ALL = (BUILTIN, SKILL_AGENT, SKILL_GO)


SourceBuiltin = Source.BUILTIN
SourceSkillAgent = Source.SKILL_AGENT
SourceSkillGo = Source.SKILL_GO


@dataclass
class Info:
    """一个工具的元信息。对应 Go ``Info``。

    ``hidden`` 只控制 prompt/列表暴露，**不是**安全边界；
    ``owner_scoped`` 为真时 ``Policy.can_use_tool`` 对普通用户一律放行，由工具
    自己按 actor 限定数据范围。
    """

    name: str = ""
    description: str = ""
    source: str = Source.BUILTIN
    risk: str = RiskLow
    superadmin_only: bool = False
    hidden: bool = False
    owner_scoped: bool = False
    foreground_only: bool = False
    vision_required: bool = False
    # tags 是面向用户的补全/手动预载分组标签，不是安全边界，也不经
    # discover_tool 暴露。
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@runtime_checkable
class Tool(Protocol):
    """一个可被模型调用的工具。对应 Go ``Tool``。"""

    def name(self) -> str: ...

    def info(self) -> Info: ...

    def schema(self) -> ToolSchema: ...

    async def call(self, ctx: Any, req: "CallRequest") -> "Result": ...


@dataclass
class RiskAssessment:
    """一次工具调用的风险评估。对应 Go ``RiskAssessment``。"""

    level: str = ""
    reasons: list[str] = field(default_factory=list)


@runtime_checkable
class RiskAssessor(Protocol):
    """能按参数自行评估风险的工具。对应 Go ``RiskAssessor``。"""

    async def assess_risk(self, ctx: Any, req: "CallRequest") -> RiskAssessment: ...


@runtime_checkable
class RiskDetailProvider(Protocol):
    """为风险确认渲染人类可读参数的工具。对应 Go ``RiskDetailProvider``。

    只影响确认详情文本，不影响工具执行结果。
    """

    async def risk_detail(self, ctx: Any, req: "CallRequest") -> str: ...


@runtime_checkable
class ConfirmationPreflightProvider(Protocol):
    """在用户确认前校验参数与目标状态。对应 Go ``ConfirmationPreflightProvider``。

    抛异常即同时阻止该次确认与执行。
    """

    async def preflight_confirmation(self, ctx: Any, req: "CallRequest") -> None: ...


async def assess_risk(ctx: Any, tool: Tool, req: "CallRequest") -> RiskAssessment:
    """对应 Go ``AssessRisk``。"""
    if isinstance(tool, RiskAssessor):
        assessment = await tool.assess_risk(ctx, req)
        assessment.level = normalize_risk(assessment.level, tool.info().risk)
        return assessment
    return RiskAssessment(level=normalize_risk(tool.info().risk, RiskHigh))


@dataclass
class CallRequest:
    """一次工具调用的入参。对应 Go ``CallRequest``。"""

    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class Result:
    """一次工具调用的结果。对应 Go ``Result``。"""

    # content 是正常工具调用回灌给 LLM 的文本。
    content: str = ""
    # segments 是回灌给 LLM 的多模态结果。图片/文件必须走带类型的段，
    # 不允许靠猜 URL。
    segments: list[MessageSegment] = field(default_factory=list)
    # warnings 会追加到 LLM 可见的工具结果里，用来纠偏后续工具使用。
    warnings: list[str] = field(default_factory=list)
    # data 预留给 discover_tool 的 DiscoveryResult（JSON 文本），Agent 用它把
    # 发现的 schema 注入为顶层工具。普通工具结果必须走 content / segments。
    data: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    outputs: list[Output] = field(default_factory=list)

    def llm_segments(self) -> list[MessageSegment]:
        """对应 Go ``Result.LLMSegments``。"""
        if len(self.segments) > 0:
            return append_segment_text(self.segments, warnings_suffix(self.warnings))
        return text_segments(append_warnings(self.content, self.warnings))


def append_warnings(content: str, warnings: Sequence[str] | None) -> str:
    """对应 Go ``AppendWarnings``。"""
    warning_text = format_warnings(warnings)
    if warning_text == "":
        return content
    content = content.rstrip("\n")
    if content.strip() == "":
        return warning_text
    return content + "\n\n" + warning_text


def format_warnings(warnings: Sequence[str] | None) -> str:
    """对应 Go ``FormatWarnings``。"""
    normalized = normalize_warnings(warnings)
    if not normalized:
        return ""
    out = "Warnings:\n"
    for warning in normalized:
        out += "- " + warning.replace("\n", "\n  ") + "\n"
    return out.rstrip("\n")


def warnings_suffix(warnings: Sequence[str] | None) -> str:
    """对应 Go ``warningsSuffix``。"""
    warning_text = format_warnings(warnings)
    if warning_text == "":
        return ""
    return "\n\n" + warning_text


def normalize_warnings(warnings: Sequence[str] | None) -> list[str]:
    """对应 Go ``normalizeWarnings``：去空白、去重、丢空。"""
    out: list[str] = []
    seen: set[str] = set()
    for warning in warnings or []:
        warning = (warning or "").strip()
        if warning == "" or warning in seen:
            continue
        seen.add(warning)
        out.append(warning)
    return out


METADATA_ACTIVATE_TOOLS = "activate_tools"
METADATA_SHOWN_RULE_CARD_FORMATS = "shown_rule_card_formats"


class Registry:
    """工具注册表。对应 Go ``Registry``。

    纯内存结构，保持同步接口；对应 Go 的 ``sync.RWMutex`` 用 ``threading.Lock``。
    """

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool | None) -> None:
        """对应 Go ``Registry.Register``。"""
        if tool is None:
            raise InvalidError("tool is nil")
        name = (tool.name() or "").strip()
        if name == "":
            raise InvalidError("tool name is required")
        with self._mu:
            if name in self._tools:
                raise InvalidError(f"tool {name!r} already registered")
            self._tools[name] = tool

    def replace_sources(self, sources: Sequence[str], replacements: Sequence[Tool]) -> None:
        """对应 Go ``Registry.ReplaceSources``：原子替换若干来源的全部工具。"""
        source_set: set[str] = set()
        for source in sources or []:
            if source == Source.BUILTIN:
                raise InvalidError("builtin tools cannot be replaced")
            source_set.add(source)
        if not source_set:
            raise InvalidError("replacement sources are required")

        candidates: list[tuple[str, Tool]] = []
        seen: set[str] = set()
        problems: list[str] = []
        for i, replacement in enumerate(replacements or []):
            if replacement is None:
                problems.append(f"replacement[{i}] is nil")
                continue
            name = (replacement.name() or "").strip()
            if name == "":
                problems.append(f"replacement[{i}] has no name")
                continue
            info = replacement.info()
            if info.source not in source_set:
                problems.append(
                    f"tool {name!r} has source {info.source!r} outside replacement set"
                )
            if name in seen:
                problems.append(f"tool {name!r} appears more than once")
            else:
                seen.add(name)
            candidates.append((name, replacement))

        with self._mu:
            for name, _ in candidates:
                existing = self._tools.get(name)
                if existing is not None and existing.info().source not in source_set:
                    problems.append(
                        f"tool {name!r} conflicts with existing source "
                        f"{existing.info().source!r}"
                    )
            if problems:
                raise InvalidError("replace tool sources: " + "; ".join(sorted(problems)))

            nxt: dict[str, Tool] = {}
            for name, existing in self._tools.items():
                if existing.info().source not in source_set:
                    nxt[name] = existing
            for name, candidate in candidates:
                nxt[name] = candidate
            self._tools = nxt

    def unregister(self, name: str) -> None:
        """对应 Go ``Registry.Unregister``。"""
        name = (name or "").strip()
        with self._mu:
            tool = self._tools.get(name)
            if tool is None:
                raise NotFoundError(f"tool {name!r} not found")
            if tool.info().source == Source.BUILTIN:
                raise InvalidError(f"cannot uninstall builtin tool {name!r}")
            del self._tools[name]

    def get(self, name: str) -> tuple[Tool | None, bool]:
        """对应 Go ``Registry.Get``。"""
        with self._mu:
            tool = self._tools.get((name or "").strip())
            return tool, tool is not None

    def list(self) -> list[Info]:
        """对应 Go ``Registry.List``：按名字排序。"""
        with self._mu:
            infos = [tool.info() for tool in self._tools.values()]
        infos.sort(key=lambda info: info.name)
        return infos

    def schemas(self) -> list[ToolSchema]:
        """对应 Go ``Registry.Schemas``。"""
        return self.schemas_for_context(None)

    def schemas_for_context(self, allowed: Any = None) -> list[ToolSchema]:
        """对应 Go ``Registry.SchemasForContext``：当前只暴露 ``discover_tool``。"""
        tool, ok = self.get("discover_tool")
        if ok and tool is not None:
            if allowed is None or allowed(tool.info()):
                return [tool.schema()]
        return []

    def tool_names(self) -> list[str]:
        """对应 Go ``Registry.ToolNames``。"""
        names: list[str] = []
        for info in self.list():
            if info.name != "discover_tool" and not info.hidden:
                names.append(info.name)
        return names

    def tags(self) -> list[str]:
        """对应 Go ``Registry.Tags``：所有规范化后的 tag，去重且排序。"""
        seen: set[str] = set()
        for info in self.list():
            for tag in info.tags:
                tag = normalize_tag(tag)
                if tag != "":
                    seen.add(tag)
        return sorted(seen)

    def names_by_tag(self, tag: str, allowed: Any = None) -> list[str]:
        """对应 Go ``Registry.NamesByTag``。"""
        tag = normalize_tag(tag)
        if tag == "":
            return []
        if allowed is None:
            allowed = _allow_all_tools
        names: list[str] = []
        for info in self.list():
            candidate, ok = self.get(info.name)
            if not ok or candidate is None or not allowed(candidate):
                continue
            if not has_tag(info.tags, tag):
                continue
            names.append(info.name)
        return names

    async def discover(self, name: str, ctx: Any = None) -> DiscoveryResult:
        """对应 Go ``Registry.Discover``：无名列出全部可见工具，有名拉详情。

        Go 用 ``context.Background()`` 调 ``DiscoverDetails``；Python 里详情来源
        可能是异步的，因此这里多一个可选 ``ctx`` 并走同一条 ``discover_details``。
        """
        name = (name or "").strip()
        if name == "":
            out = [
                DiscoveredTool(info=public_info(info))
                for info in self.list()
                if not info.hidden
            ]
            return DiscoveryResult(tools=out)
        details, errors = await self.discover_details(ctx, [name], _allow_all_tools)
        if errors:
            raise NotFoundError(f"tool {name!r} not found")
        return DiscoveryResult(tools=details)

    async def discover_details(
        self, ctx: Any, names: Sequence[str] | None, allowed: Any = None
    ) -> tuple[list[DiscoveredTool], list[DiscoveryError]]:
        """对应 Go ``Registry.DiscoverDetails``。"""
        return await _discover_details(self, ctx, names, allowed)


def _allow_all_tools(_tool: Tool) -> bool:
    return True


def new_registry() -> Registry:
    """对应 Go ``NewRegistry``。"""
    return Registry()


def can_access_tool(actor: Actor, policy: Policy | None, info: Info) -> bool:
    """对应 Go ``CanAccessTool``。"""
    if info.superadmin_only and getattr(actor, "role", None) != RoleSuperadmin:
        return False
    if policy is None:
        policy = DefaultPolicy()
    return policy.can_use_tool(
        actor, normalize_risk(info.risk, RiskHigh), bool(info.owner_scoped)
    )


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


def normalize_tags(tags: Sequence[str] | None) -> list[str]:
    """对应 Go ``normalizeTags``：逐个规范化后去重保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        tag = normalize_tag(tag)
        if tag == "" or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def normalize_tag(tag: str) -> str:
    """对应 Go ``normalizeTag``：小写化，只允许 ``[a-z0-9_.-]``，否则丢弃。"""
    tag = (tag or "").strip().lower()
    for ch in tag:
        if not (ch.isascii() and (ch.islower() or ch.isdigit() or ch in "_-.")):
            return ""
    return tag


def has_tag(tags: Sequence[str] | None, want: str) -> bool:
    """对应 Go ``hasTag``。"""
    want = normalize_tag(want)
    if want == "":
        return False
    return any(normalize_tag(tag) == want for tag in tags or [])


def normalize_risk(value: str, fallback: str) -> str:
    """对应 Go ``normalizeRisk``：空值回退，最终兜底 ``high``。"""
    if value:
        return value
    if fallback:
        return fallback
    return RiskHigh


__all__ = [name for name in dir() if not name.startswith("_")]
