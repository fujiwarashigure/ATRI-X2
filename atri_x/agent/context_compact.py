"""上下文压缩：``/compact`` 的会话切换、摘要 metadata 与状态。

对应 Go：``internal/agent/context_compact.go``，外加 ``session_metadata.go`` 里
``contextCompactState`` 的读写适配。

**顺序语义**（Go 注释强调，不能改）：

* 有在途请求时不许压缩；压缩占用 ``turns`` 的 compact 阶段，并且必须在所有返回路径上
  释放（``closeLifecycle`` 的等价物由 ``try/finally`` 保证）。
* 压缩产物是**一个新会话**：它带上 ``pending`` 的 ``context_compact`` 摘要与已生成的
  标题（``<base> compacted-N``），等待下一轮作为种子消费（见 ``context_seed.py``）。
* ``from_message_id`` 取「已加载上下文的第一个消息」；已有摘要时取摘要自身的
  ``from_message_id``，这样多代压缩不会丢掉最早的锚点。

本模块同时持有会话 metadata 的**读取适配**（:func:`decode_metadata`）：唯一出处是
agent-core 的 ``session_metadata.py``，缺失时用 JSON 视图兜底，让本组模块可以独立跑测试。
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from atri_x.agent.model import audit_event
from atri_x.agent.prompt import assistant_message_metadata, summary_user_prefix
from atri_x.config import ModelSelection
from atri_x.contextmgr import CompactMessage, CompactToolCall, CompactionChoice
from atri_x.errors import InvalidError
from atri_x.request.manager import KindCompress, StartRequest
from atri_x.storage.models import ContextSummary, Message, RoleAssistant, RoleUser

__all__ = [
    "ContextCompactMixin",
    "ContextCompactState",
    "build_compact_messages",
    "compact_active",
    "compact_user_inputs",
    "create_session",
    "decode_metadata",
    "encode_metadata_fields",
    "format_compacted_title",
    "next_compacted_title",
]

#: 压缩会话的标题后缀模板。对应 Go ``formatCompactedTitle``。
COMPACTED_TITLE_SUFFIX = " compacted-"


@dataclass
class ContextCompactState:
    """落盘结构 ``context_compact``。对应 Go ``contextCompactState``。

    字段名与 JSON 键（Go struct tag）逐字对应；唯一出处是 agent-core 的
    ``session_metadata.py``，这里只是缺省兜底。
    """

    pending: bool = False
    summary: str = ""
    source_session_id: str = ""
    from_message_id: str = ""
    to_message_id: str = ""
    provider: str = ""
    model: str = ""
    trigger_reason: str = ""
    source_tokens: int = 0
    summary_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    generation: int = 0
    base_title: str = ""

    def to_json(self) -> dict[str, Any]:
        """按 Go 的 ``omitempty`` 语义输出（空值不落键）。"""
        out: dict[str, Any] = {}
        for key, value in (
            ("pending", self.pending),
            ("summary", self.summary),
            ("source_session_id", self.source_session_id),
            ("from_message_id", self.from_message_id),
            ("to_message_id", self.to_message_id),
            ("provider", self.provider),
            ("model", self.model),
            ("trigger_reason", self.trigger_reason),
            ("source_tokens", self.source_tokens),
            ("summary_tokens", self.summary_tokens),
            ("total_tokens", self.total_tokens),
            ("cache_hit_tokens", self.cache_hit_tokens),
            ("generation", self.generation),
            ("base_title", self.base_title),
        ):
            if value:
                out[key] = value
        return out


# ---------------------------------------------------------------------------
# 会话 metadata 读写适配
# ---------------------------------------------------------------------------
def _session_metadata_module() -> Any:
    try:
        return importlib.import_module("atri_x.agent.session_metadata")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        return None


def _json_object(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _namespace(value: Any) -> Any:
    """把 JSON 结构转成可属性访问的视图（``dict`` 递归转换）。"""
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def decode_metadata(raw: str) -> Any:
    """解析会话 metadata。

    优先用 agent-core 的 ``decode_session_metadata``（唯一出处）；它还没落地时返回
    JSON 属性视图，字段名与 Go 的 struct tag 一致（``context_compact`` /
    ``tool_tags`` / ``workspace_dir`` / ``last_usage`` …）。
    """
    if not raw:
        return None
    module = _session_metadata_module()
    decode = getattr(module, "decode_session_metadata", None) if module else None
    if callable(decode):
        try:
            return decode(raw)
        except Exception:  # noqa: BLE001 - 脏数据按没有 metadata 处理
            return None
    data = _json_object(raw)
    return _namespace(data) if data else None


def encode_metadata_fields(base_raw: str, updates: Mapping[str, Any]) -> str:
    """把若干字段合并进会话 metadata JSON。

    对应 Go ``encodeSessionMetadataInto`` 的字段合并部分（空字符串/``False``/``None``
    表示删除该键，与 ``setMetadataField`` 一致）。``state.toml`` 之外唯一会写会话
    metadata 的地方是 agent-core 与 agent-tools；这里刻意只做「合并」而不重建整份
    metadata，避免把它们写的字段抹掉。
    """
    base = _json_object(base_raw)
    for key, value in updates.items():
        if value is None or value == "" or value is False or value == {} or value == []:
            base.pop(key, None)
            continue
        if hasattr(value, "to_json"):
            value = value.to_json()
        base[key] = value
    if not base:
        return ""
    return json.dumps(base, ensure_ascii=False, separators=(",", ":"))


def compact_state_from_metadata(raw: str) -> ContextCompactState | None:
    """从会话 metadata 里取 ``context_compact``（兼容 dataclass 与 JSON 两种表示）。"""
    metadata = decode_metadata(raw)
    compact = getattr(metadata, "context_compact", None) if metadata is not None else None
    if compact is None:
        return None
    if isinstance(compact, ContextCompactState):
        return compact
    if isinstance(compact, Mapping):
        return ContextCompactState(**{**compact})
    values = {}
    for name in ContextCompactState.__dataclass_fields__:
        values[name] = getattr(compact, name, None)
    values = {
        key: (value if value is not None else ContextCompactState.__dataclass_fields__[key].default)
        for key, value in values.items()
    }
    return ContextCompactState(**values)


# ---------------------------------------------------------------------------
# 标题
# ---------------------------------------------------------------------------
def format_compacted_title(base_title: str, generation: int) -> str:
    """``<base> compacted-N``。对应 Go ``formatCompactedTitle``。"""
    return f"{(base_title or '').strip()}{COMPACTED_TITLE_SUFFIX}{generation}"


def next_compacted_title(source: Any) -> tuple[str, int, str]:
    """算出压缩后新会话的标题与代数。对应 Go ``nextCompactedTitle``。"""
    base_title = str(getattr(source, "title", "") or "").strip()
    generation = 0
    compact = compact_state_from_metadata(str(getattr(source, "metadata", "") or ""))
    if compact is not None and int(compact.generation or 0) > 0:
        generation = int(compact.generation)
        expected = format_compacted_title(compact.base_title, compact.generation)
        if getattr(source, "title", "") == expected:
            base_title = compact.base_title
    if not base_title:
        base_title = "New session"
    generation += 1
    return format_compacted_title(base_title, generation), generation, base_title


# ---------------------------------------------------------------------------
# 压缩输入
# ---------------------------------------------------------------------------
def compact_user_inputs(messages: Sequence[Message]) -> list[str]:
    """用户原话列表。对应 Go ``compactUserInputs``。"""
    inputs: list[str] = []
    for message in messages:
        if message.role == RoleUser and (message.content or "").strip():
            inputs.append(message.content)
    return inputs


async def build_compact_messages(
    ctx: Any, loaded: Any, store: Any
) -> list[CompactMessage]:
    """把已加载上下文压成压缩器的输入。对应 Go ``compactMessages``。

    只保留**成功过**的工具调用（``tool_call_records`` 判成功），否则失败的调用会作为
    "已做过的事"进入摘要。
    """
    call_ids: list[str] = []
    for message in loaded.messages:
        if message.role != RoleAssistant:
            continue
        for call in assistant_message_metadata(message.metadata).tool_calls:
            if call.id:
                call_ids.append(call.id)

    successful: Mapping[str, bool] = {}
    if call_ids:
        repository = store.tool_calls() if store is not None else None
        if repository is None:
            raise InvalidError("tool call repository is not configured")
        successful = repository.successful_ids(call_ids) or {}

    summary = getattr(loaded, "summary", None)
    out: list[CompactMessage] = []
    summary_injected = False
    for message in loaded.messages:
        if message.role == RoleUser:
            content = message.content or ""
            if summary is not None and not summary_injected:
                content = summary_user_prefix(summary.summary) + content
                summary_injected = True
            if content.strip():
                out.append(CompactMessage(role=RoleUser, content=content))
        elif message.role == RoleAssistant:
            metadata = assistant_message_metadata(message.metadata)
            content = metadata.raw_text or (message.content or "")
            calls = [
                CompactToolCall(name=call.name, arguments=call.arguments)
                for call in metadata.tool_calls
                if successful.get(call.id)
            ]
            if content.strip() or calls:
                out.append(
                    CompactMessage(role=RoleAssistant, content=content, tool_calls=calls)
                )
    if (
        summary is not None
        and not summary_injected
        and (summary.summary or "").strip()
    ):
        out.insert(0, CompactMessage(role=RoleUser, content=summary.summary))
    return out


# ---------------------------------------------------------------------------
# 压缩编排
# ---------------------------------------------------------------------------
async def run_session_compaction(
    *,
    runtime: Any,
    requests: Any,
    turns: Any,
    sessions: Any,
    store: Any,
    ctx: Any,
    current: Any,
    scope: Any,
    trigger_reason: str,
    selection: ModelSelection,
) -> tuple[Any, CompactionChoice]:
    """执行一次会话压缩并返回新会话。对应 Go ``contextRuntimeState.compactSession``。"""
    if requests is not None and requests.list_by_session(current.id):
        raise InvalidError("当前会话有正在运行的请求，无法压缩")

    _, token, done = await requests.start(
        ctx,
        StartRequest(
            session_id=current.id,
            kind=KindCompress,
            label="compact",
            scope_key=request_scope_key(scope),
            timeout=getattr(runtime, "compress_timeout", 0.0),
        ),
    )
    lifecycle_closed = False
    turn_started = False

    def close_lifecycle() -> None:
        nonlocal lifecycle_closed
        if lifecycle_closed:
            return
        if turn_started and turns is not None:
            turns.complete_compact(current.id)
        done()
        lifecycle_closed = True

    try:
        token.raise_if_cancelled()
        if turns is None or not turns.start_compact(current.id):
            raise InvalidError("当前会话正在处理其他任务，无法压缩")
        turn_started = True
        token.raise_if_cancelled()

        loaded = await runtime.load(token, current.id)
        if not loaded.messages:
            raise InvalidError("没有可压缩的历史消息")
        raw_messages = await runtime.load_raw_messages(token, current.id)
        compact_messages = await build_compact_messages(token, loaded, store)

        compressor, choice = runtime.compactor_for(
            selection.provider, selection.model
        )
        if compressor is None:
            raise InvalidError("压缩后端未配置")
        from atri_x.contextmgr import CompactRequest

        result = await compressor.compact(
            token,
            CompactRequest(
                provider=selection.provider,
                model=selection.model,
                messages=compact_messages,
                user_inputs=compact_user_inputs(raw_messages),
                user_input_max_runes=runtime.user_original_max_runes(),
            ),
        )

        from_message_id = loaded.messages[0].id
        if loaded.summary is not None and loaded.summary.from_message_id:
            from_message_id = loaded.summary.from_message_id
        title, generation, base_title = next_compacted_title(current)
        compact = ContextCompactState(
            pending=True,
            summary=result.assembled_summary,
            source_session_id=current.id,
            from_message_id=from_message_id,
            to_message_id=loaded.messages[-1].id,
            provider=selection.provider,
            model=selection.model,
            trigger_reason=trigger_reason,
            generation=generation,
            base_title=base_title,
        )
        if result.usage is not None:
            compact.source_tokens = int(result.usage.prompt_tokens or 0)
            compact.summary_tokens = int(result.usage.completion_tokens or 0)
            compact.total_tokens = int(result.usage.total_tokens or 0)
            compact.cache_hit_tokens = int(result.usage.cache_hit_tokens or 0)
        metadata = encode_metadata_fields(
            "",
            {
                "context_compact": compact,
                "title_renamed": True,
                "title_source": "compact",
            },
        )
        from atri_x.session.types import CreateRequest

        next_session = create_session(
            sessions,
            token,
            scope,
            CreateRequest(title=title, mode=current.mode, metadata=metadata),
        )
        close_lifecycle()
        return next_session, choice
    finally:
        close_lifecycle()


def create_session(sessions: Any, ctx: Any, scope: Any, request: Any) -> Any:
    """建新会话。

    ``atri_x.session.Service.create`` 的签名是 ``(scope, req)``（Go 带 ctx），
    因此按可调用对象的参数个数自适应；两种形状都能接。
    """
    import inspect

    create = sessions.create
    try:
        parameters = inspect.signature(create).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/无签名可调用
        parameters = {"scope": None, "req": None}
    if len(parameters) >= 3:
        return create(ctx, scope, request)
    return create(scope, request)


def request_scope_key(scope: Any) -> str:
    """请求队列的 scope 桶。对应 Go ``requestScopeKeyFromScope``（``group_policy.go``）。

    唯一出处是 agent-tools 的 ``group_policy.py``；缺失时用等价实现兜底。
    """
    module = None
    try:
        module = importlib.import_module("atri_x.agent.group_policy")
    except Exception:  # noqa: BLE001 - 同僚文件可能尚未落地
        module = None
    fn = getattr(module, "request_scope_key_from_scope", None) if module else None
    if callable(fn):
        return fn(scope)
    platform = str(getattr(scope, "platform", "") or "").strip() or "unknown"
    scope_id = str(getattr(scope, "platform_scope_id", "") or "").strip() or "unknown"
    return platform + ":" + scope_id


def compact_active(turns: Any, requests: Any, session_id: str) -> bool:
    """该会话是否正在压缩。对应 Go ``contextRuntimeState.compactActive``。"""
    from atri_x.turn.manager import PhaseCompact

    if turns is not None:
        snapshot = turns.snapshot(session_id)
        if getattr(snapshot, "phase", "") == PhaseCompact:
            return True
    if requests is not None:
        for active in requests.list_by_session(session_id):
            if getattr(active, "kind", "") == KindCompress:
                return True
    return False


class ContextCompactMixin:
    """``internal/agent/context_compact.go`` 里挂在 ``*Agent`` 上的方法。"""

    async def compact_current(self, ctx: Any, trigger_reason: str) -> str:
        """压缩当前会话。对应 Go ``CompactCurrent``。"""
        if not self.history_enabled(ctx):
            raise InvalidError("本群已关闭历史记录，不能压缩或生成新的会话摘要")
        current = self.sessions.current(self.scope(ctx))
        _, content = await self.compact_session(
            ctx, current, trigger_reason, self.model_selection_for_turn(ctx, current)
        )
        return content

    async def compact_session(
        self,
        ctx: Any,
        current: Any,
        trigger_reason: str,
        fallback: ModelSelection,
    ) -> tuple[Any, str]:
        """压缩指定会话并返回 ``(新会话, 提示文本)``。对应 Go ``compactSession``。"""
        from atri_x.agent.context_runtime import ensure_context_runtime

        runtime = ensure_context_runtime(self)
        selection = runtime.compact_selection(fallback)
        self.authorize_execution_model_selection(ctx, selection)
        next_session, choice = await run_session_compaction(
            runtime=runtime,
            requests=getattr(self, "requests", None),
            turns=getattr(self, "turns", None),
            sessions=self.sessions,
            store=getattr(self, "store", None),
            ctx=ctx,
            current=current,
            scope=self.scope(ctx),
            trigger_reason=trigger_reason,
            selection=selection,
        )
        # 记录这次压缩实际走的后端：压缩很少发生，而"到底是谁压的、有没有回退"事后
        # 无法从结果反推；没有回退时也记一条，用于确认服务端压缩有没有接上。
        audit_event(
            self,
            "context_compaction_backend",
            session_id=next_session.id,
            backend=choice.backend,
            reason=choice.fallback_reason,
            provider=selection.provider,
            model=selection.model,
            trigger=trigger_reason,
            result="succeeded",
        )
        return next_session, f"上下文压缩完成。\nnew session: {next_session.id}"

    def compact_active(self, session_id: str) -> bool:
        """该会话是否正在压缩。对应 Go ``Agent.compactActive``。"""
        return compact_active(
            getattr(self, "turns", None), getattr(self, "requests", None), session_id
        )


def compact_summary_of(session: Any) -> ContextSummary | None:
    """会话 metadata 里的 pending 压缩摘要（``context_seed`` 的读入口）。"""
    compact = compact_state_from_metadata(str(getattr(session, "metadata", "") or ""))
    if compact is None or not compact.pending or not compact.summary:
        return None
    return ContextSummary(
        session_id=getattr(session, "id", "") or "",
        from_message_id=compact.from_message_id,
        to_message_id=compact.to_message_id,
        summary=compact.summary,
        provider=compact.provider,
        model=compact.model,
        source_tokens=compact.source_tokens,
        summary_tokens=compact.summary_tokens,
        total_tokens=compact.total_tokens,
        cache_hit_tokens=compact.cache_hit_tokens,
        trigger_reason=compact.trigger_reason,
    )
