"""``atri_x.agent.commands`` 的测试替身。

命令包对 ``atri_x.agent`` 顶层模块（core / model / tools）**零 import**：所有服务
都从 :class:`atri_x.agent.commands.deps.Deps` 以鸭子类型注入。因此这里给出各服务的
最小替身，只实现命令真正会调用的方法，并记录调用轨迹，便于断言"命令确实把参数交给了
服务"。

会话 / 存储 / turn / request 这几项用**真实实现**（``tests.test_session.new_test_service``
提供内存 SQLite store），只有外部服务（模型、日志、hook 等）用替身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from atri_x.agent.commands import (
    Deps,
    ModelListResult,
    ModelOption,
    ModelProfile,
    ModelSnapshot,
    ModelSnapshotSlot,
    RuntimeStateReloadReport,
    RuntimeStateStatus,
    new_default_router,
    new_session_command_state,
)
from atri_x.angelmemory import LegacySourceBackfill, Memory
from atri_x.character import Character, Visibility
from atri_x.command import Router
from atri_x.request import Manager as RequestManager
from atri_x.security import Actor, RoleSuperadmin, RoleUser, with_actor
from atri_x.session import Scope
from atri_x.turn import Manager as TurnManager
from atri_x.utils.fileops import RollbackInfo
from tests.test_session import new_test_service

GROUP_SCOPE_ID = "group:42"

#: ``SessionModule`` 在 ``deps.session_state`` 缺失时用的默认配置。
DEFAULT_SESSION_LIST_PAGE_SIZE = 10
DEFAULT_RETENTION_DAYS = 30


# ---------------------------------------------------------------------------
# 简单替身类型
# ---------------------------------------------------------------------------


@dataclass
class ToolInfo:
    """``ToolService.list()`` 的一项（对应 Go ``runtime.Info``）。"""

    name: str = ""
    source: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class StatefulHookInfo:
    """有状态 hook 的视图（对应 Go ``hook.RuntimeInfo``）。"""

    id: str = ""
    mode: str = ""
    status: str = ""
    description: str = ""
    detail: str = ""
    active: int = 0
    waiting: int = 0


@dataclass
class LearningCandidate:
    """``SelfLearning.review`` 的一条候选。"""

    id: str = ""
    pattern: str = ""
    status: str = "pending"
    count: int = 0


@dataclass
class LearningHistoryRecord:
    """``SelfLearning.history`` 的一条记录。"""

    from_status: str = ""
    to_status: str = ""
    reviewer: str = ""


@dataclass
class MineStats:
    """``SelfLearning.mine`` 的结果。"""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    scanned: int = 0
    total_chars: int = 0
    truncated: bool = False
    timed_out: bool = False


@dataclass
class LogEntryStub:
    """日志替身里的一条记录（真实类型是 ``atri_x.logging.reader.LogEntry``）。"""

    level: str = "info"
    message: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@dataclass
class ScanResultStub:
    """``LogService.query_logs`` 的返回（对应 ``atri_x.logging.QueryResult``）。"""

    entries: list[Any] = field(default_factory=list)
    damaged_count: int = 0
    damaged_lines: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 各服务替身
# ---------------------------------------------------------------------------


class FakeModels:
    """``ModelService`` 替身。"""

    def __init__(self) -> None:
        self.provider = "openai"
        self.model = "gpt-4o"
        self.options: list[ModelOption] = [
            ModelOption(
                index=1,
                provider="openai",
                model="gpt-4o",
                current=True,
                chat_current=True,
                work_current=True,
            ),
            ModelOption(index=2, provider="anthropic", model="claude-3-5-sonnet"),
        ]
        self.profiles: list[ModelProfile] = [
            ModelProfile(name="fast", provider="openai", model="gpt-4o", available=True),
        ]
        self.snapshots: list[ModelSnapshot] = [
            ModelSnapshot(
                name="cheap",
                slots=[ModelSnapshotSlot(label="chat", provider="openai", model="gpt-4o-mini")],
            )
        ]
        self.calls: list[str] = []

    # -- 读取 ---------------------------------------------------------------
    def current_model(self) -> str:
        return self.model

    def current_provider(self) -> str:
        return self.provider

    def current_mode_model(self) -> ModelOption:
        return ModelOption(provider=self.provider, model=self.model, current=True)

    def current_model_for_mode(self, mode: str) -> ModelOption:
        return ModelOption(provider=self.provider, model=self.model, mode_marks=[str(mode)])

    def current_compact_model(self) -> ModelOption:
        return ModelOption(provider="openai", model="gpt-4o-mini", compact=True)

    def current_naming_model(self) -> ModelOption:
        return ModelOption(provider="openai", model="gpt-4o-mini", naming=True)

    def models(self, query: str) -> list[ModelOption]:
        return list(self.options)

    def model_list(self, query: str, opts: Any = None) -> ModelListResult:
        return ModelListResult(options=list(self.options))

    def model_profiles(self) -> list[ModelProfile]:
        return list(self.profiles)

    def model_snapshots(self) -> list[ModelSnapshot]:
        return list(self.snapshots)

    # -- 写入 ---------------------------------------------------------------
    async def select_model(self, ctx: Any, arg: str) -> ModelOption:
        self.calls.append(f"select_model:{arg}")
        return ModelOption(provider="openai", model=arg or self.model)

    async def select_model_for_mode(self, ctx: Any, mode: str, arg: str) -> ModelOption:
        self.calls.append(f"select_model_for_mode:{mode}:{arg}")
        return ModelOption(provider="openai", model=arg or self.model)

    def select_compact_model(self, arg: str) -> ModelOption:
        self.calls.append(f"select_compact_model:{arg}")
        return ModelOption(provider="openai", model=arg or "gpt-4o-mini")

    def select_naming_model(self, arg: str) -> ModelOption:
        self.calls.append(f"select_naming_model:{arg}")
        return ModelOption(provider="openai", model=arg or "gpt-4o-mini")

    def save_model_snapshot(self, name: str) -> None:
        self.calls.append(f"save:{name}")

    async def apply_model_snapshot(self, ctx: Any, name: str) -> ModelSnapshot:
        self.calls.append(f"apply:{name}")
        return self.snapshots[0]

    def delete_model_snapshot(self, name: str) -> None:
        self.calls.append(f"delete:{name}")


class FakeContextStatus:
    """``ContextStatusService`` 替身。"""

    def __init__(self) -> None:
        self.text = "  context: 0 / 1000 tokens"

    def context_status(self, ctx: Any, session: Any) -> str:
        return self.text


class FakeCompact:
    """``CompactService`` 替身。"""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def compact_current(self, ctx: Any, trigger_reason: str) -> str:
        self.reasons.append(trigger_reason)
        return "context compacted"


class FakeContextPolicy:
    """``ContextPolicyService`` 替身。"""

    def __init__(self) -> None:
        self.value = "reject"
        self.calls: list[str] = []

    def context_policy_status(self, ctx: Any) -> str:
        return f"overflow policy: {self.value}"

    async def set_context_policy(self, ctx: Any, target: str, value: str) -> str:
        self.calls.append(f"set:{target}:{value}")
        self.value = value
        return f"已设置 {target} 溢出策略为 {value}"

    async def reset_context_policy(self, ctx: Any, target: str) -> str:
        self.calls.append(f"reset:{target}")
        return f"已重置 {target} 溢出策略"


class FakeGroupPolicy:
    """``GroupPolicyService`` 替身。"""

    def __init__(self, learning: bool = True, authorized: bool = True) -> None:
        self.learning = learning
        self.authorized = authorized
        self.calls: list[str] = []
        self.status_text = "群级策略\n修改：/grouppolicy <field> <value>"

    def group_policy_status(self, ctx: Any) -> str:
        return self.status_text

    async def set_group_policy(self, ctx: Any, field: str, value: str) -> str:
        self.calls.append(f"set:{field}:{value}")
        return f"群策略已更新：{field} = {value}"

    async def reset_group_policy(self, ctx: Any, field: str) -> str:
        self.calls.append(f"reset:{field}")
        return f"群策略字段已重置：{field}"

    def authorize_learning_action(self, ctx: Any, action: str) -> bool:
        return self.authorized

    def group_learning_enabled(self, ctx: Any) -> bool:
        return self.learning


class FakeGroupKnowledge:
    """``GroupKnowledgeService`` 替身。"""

    def __init__(self) -> None:
        self.entries: list[Any] = []
        self.calls: list[str] = []

    async def group_knowledge_list(self, ctx: Any) -> list[Any]:
        return list(self.entries)

    async def group_knowledge_add(
        self,
        ctx: Any,
        question: str,
        answer: str,
        match: str,
        aliases: list[str],
        keywords: list[str],
    ) -> Any:
        entry = KnowledgeEntry(
            id=f"k{len(self.entries) + 1}",
            question=question,
            answer=answer,
            match=match,
            aliases=list(aliases),
            keywords=list(keywords),
        )
        self.entries.append(entry)
        self.calls.append(f"add:{question}")
        return entry

    async def group_knowledge_remove(self, ctx: Any, id: str) -> bool:
        for index, entry in enumerate(self.entries):
            if entry.id == id:
                del self.entries[index]
                return True
        return False

    async def group_knowledge_clear(self, ctx: Any) -> int:
        count = len(self.entries)
        self.entries.clear()
        return count

    async def group_knowledge_test(self, ctx: Any, text: str) -> tuple[Any, bool]:
        for entry in self.entries:
            if text == entry.question:
                return entry, True
        return None, False


@dataclass
class KnowledgeEntry:
    """知识库条目替身（真实类型见 ``atri_x.config.GroupKnowledgeEntry``）。"""

    id: str = ""
    question: str = ""
    answer: str = ""
    match: str = "exact"
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    enabled: bool = True


class FakeMemberPanel:
    """``MemberPanelService`` 替身。"""

    def __init__(self) -> None:
        self.views: list[str] = []

    async def member_panel(self, ctx: Any, view: str) -> str:
        self.views.append(view)
        return f"my panel ({view or 'all'})"


class FakeGroupServices:
    """``GroupServicesService`` 替身。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reminder_create(self, ctx: Any, when_text: str, text: str) -> str:
        self.calls.append(f"reminder_create:{when_text}:{text}")
        return f"提醒已创建：{when_text} {text}"

    async def reminder_list(self, ctx: Any) -> str:
        return "当前群提醒（0 条）："

    async def reminder_remove(self, ctx: Any, id: str) -> str:
        self.calls.append(f"reminder_remove:{id}")
        return f"已删除提醒 {id}"

    async def poll_create(self, ctx: Any, question: str, options: list[str]) -> str:
        self.calls.append(f"poll_create:{question}:{'|'.join(options)}")
        return f"[p1] {question}（open）"

    async def poll_list(self, ctx: Any) -> str:
        return "当前群投票（0 条）："

    async def poll_show(self, ctx: Any, id: str) -> str:
        return f"[{id}] 问题（open）"

    async def poll_vote(self, ctx: Any, id: str, option: str) -> str:
        self.calls.append(f"poll_vote:{id}:{option}")
        return f"已投票：{id} 选项 {option}"

    async def poll_close(self, ctx: Any, id: str) -> str:
        self.calls.append(f"poll_close:{id}")
        return f"[{id}] 问题（closed）"

    async def signup_create(self, ctx: Any, title: str, capacity: int) -> str:
        self.calls.append(f"signup_create:{title}:{capacity}")
        return f"[s1] {title}（open，0/{capacity}）"

    async def signup_list(self, ctx: Any) -> str:
        return "当前群报名（0 条）："

    async def signup_show(self, ctx: Any, id: str) -> str:
        return f"[{id}] 报名（open）"

    async def signup_join(self, ctx: Any, id: str) -> str:
        self.calls.append(f"signup_join:{id}")
        return f"已加入报名 {id}"

    async def signup_leave(self, ctx: Any, id: str) -> str:
        self.calls.append(f"signup_leave:{id}")
        return f"已退出报名 {id}"

    async def signup_close(self, ctx: Any, id: str) -> str:
        self.calls.append(f"signup_close:{id}")
        return f"[{id}] 报名（closed）"


class FakeTools:
    """``ToolService`` 替身。"""

    def __init__(self) -> None:
        self.infos: list[ToolInfo] = [
            ToolInfo(name="read_file", source="builtin", description="读文件", tags=["fs"])
        ]
        self.calls: list[str] = []

    def list(self) -> list[ToolInfo]:
        return list(self.infos)

    def unregister(self, name: str) -> None:
        self.calls.append(f"unregister:{name}")

    async def remove(self, ctx: Any, name: str) -> None:
        self.calls.append(f"remove:{name}")

    async def reload(self, ctx: Any) -> None:
        self.calls.append("reload")


class FakeHooks:
    """``HookService`` 替身。"""

    def __init__(self) -> None:
        from atri_x.hook.manager import Info as HookInfo

        self.infos: list[Any] = [
            HookInfo(
                name="audit-log",
                plugin_id="",
                description="记录审计事件",
                point="tool_call_prepared",
                priority=10,
                active=3,
            ),
            HookInfo(
                name="rule-a",
                plugin_id="plug",
                description="插件规则",
                point="llm_request_prepared",
                priority=5,
            ),
        ]
        self.stateful: list[StatefulHookInfo] = [
            StatefulHookInfo(
                id="plug",
                mode="oneshot",
                status="running",
                description="有状态插件",
                active=1,
                waiting=2,
            )
        ]
        self.notices: list[str] = []
        self.calls: list[str] = []

    def hook_list(self) -> list[Any]:
        return list(self.infos)

    def hook_reload(self) -> Any:
        from atri_x.hook.manager import ReloadReport

        return ReloadReport(notices=list(self.notices))

    def stateful_hooks(self) -> list[StatefulHookInfo]:
        return list(self.stateful)

    def start_stateful_hook(self, id: str) -> None:
        self.calls.append(f"start:{id}")

    async def stop_hook(self, ctx: Any, id: str) -> bool:
        self.calls.append(f"stop:{id}")
        return True

    async def restart_stateful_hook(self, ctx: Any, id: str) -> None:
        self.calls.append(f"restart:{id}")


class FakeLogs:
    """``LogService`` 替身（同步方法，与 Go 的 ``QueryLogs`` 一致）。"""

    def __init__(self) -> None:
        self.entries: list[LogEntryStub] = []
        self.queries: list[Any] = []

    def query_logs(self, ctx: Any, query: Any) -> ScanResultStub:
        self.queries.append(query)
        return ScanResultStub(entries=list(self.entries))


class FakeRuntimeState:
    """``RuntimeStateService`` 替身。"""

    def __init__(self) -> None:
        self.status = RuntimeStateStatus(
            path="data/config/atri-x/state.toml",
            loaded_mod_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            file_mod_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            pending=False,
        )
        self.reloads = 0

    async def reload_runtime_state(self, ctx: Any) -> RuntimeStateReloadReport:
        self.reloads += 1
        return RuntimeStateReloadReport(
            path=self.status.path,
            applied=True,
            changed=["mode_models"],
            mod_time=datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc),
        )

    def runtime_state_status(self) -> RuntimeStateStatus:
        return self.status


class FakeDoctor:
    """``DoctorService`` 替身。"""

    def __init__(self) -> None:
        self.text = "配置检查：一切正常"

    async def doctor(self, ctx: Any) -> str:
        return self.text


class FakeCharacters:
    """``atri_x.character.Store`` 替身（只实现命令用到的方法）。"""

    def __init__(self) -> None:
        self.items: list[Character] = [
            Character(
                id="alice",
                name="Alice",
                aliases=["小爱"],
                description="测试角色",
                tags=["demo"],
                visibility=Visibility.PUBLIC,
            )
        ]
        self.refreshes = 0

    def enabled(self) -> bool:
        return True

    def refresh(self, ctx: Any = None) -> None:
        self.refreshes += 1

    def list(self, ctx: Any = None, viewer: Any = None) -> list[Character]:
        return list(self.items)


class FakeAngelMemory:
    """``atri_x.angelmemory.Service`` 替身（同步方法）。"""

    def __init__(self, memories: list[Memory] | None = None) -> None:
        self.memories = list(memories or [])
        self.deleted: list[str] = []

    def ready(self) -> bool:
        return True

    def get(self, platform: str, scope_id: str, id: str) -> Memory:
        for memory in self.memories:
            if memory.id == id:
                return memory
        from atri_x.errors import NotFoundError

        raise NotFoundError("not found")

    def list(self, platform: str, scope_id: str, filter: Any, limit: int) -> list[Memory]:
        return [m for m in self.memories if _filter_match(filter, m)][:limit]

    def resolve_id(self, platform: str, scope_id: str, prefix: str, filter: Any) -> str:
        hits = [m.id for m in self.memories if m.id.startswith(prefix)]
        from atri_x.errors import AmbiguousIDError, NotFoundError

        if not hits:
            raise NotFoundError("not found")
        if len(hits) > 1:
            raise AmbiguousIDError("ambiguous id")
        return hits[0]

    def recall(self, platform: str, scope_id: str, text: str, limit: int) -> list[Memory]:
        return self.memories[:limit]

    def count(self, platform: str, scope_id: str) -> int:
        return len(self.memories)

    def delete(self, platform: str, scope_id: str, id: str) -> bool:
        self.deleted.append(id)
        for index, memory in enumerate(self.memories):
            if memory.id == id:
                del self.memories[index]
                return True
        return False

    def delete_by_source(self, platform: str, scope_id: str, filter: Any) -> int:
        kept = [m for m in self.memories if not _filter_match(filter, m)]
        count = len(self.memories) - len(kept)
        self.memories = kept
        return count

    def legacy_source_backfill_stats(self) -> LegacySourceBackfill:
        return LegacySourceBackfill(total=10, linked=4, backfillable=3)

    def backfill_legacy_source_kind(self) -> int:
        return 3


def _filter_match(filter: Any, memory: Memory) -> bool:
    """``SourceFilter`` 的非空字段之间是 AND（与库层语义一致）。"""
    if filter is None:
        return True
    for attr, value in (
        ("actor_id", memory.source_actor_id),
        ("message_id", memory.source_message_id),
        ("session_id", memory.source_session_id),
    ):
        wanted = getattr(filter, attr, "")
        if wanted != "" and wanted != value:
            return False
    return True


class FakeSelfLearning:
    """``atri_x.selflearning.Service`` 替身（同步方法）。"""

    def __init__(self) -> None:
        self.candidates = [LearningCandidate(id="c1", pattern="yyds", count=4)]
        self.decisions: list[tuple[str, str, str]] = []
        self.mined = 0

    def ready(self) -> bool:
        return True

    def stats(self, platform: str, scope_id: str) -> tuple[int, int]:
        return (2, 5)

    def mine(self, platform: str, scope_id: str, min_count: int, limit: int) -> MineStats:
        self.mined += 1
        return MineStats(created=1, updated=2, skipped=3, scanned=40, total_chars=800)

    def review(self, platform: str, scope_id: str, status: str, limit: int) -> list[Any]:
        return [c for c in self.candidates if c.status == status]

    def decide(
        self,
        platform: str,
        scope_id: str,
        id: str,
        status: str,
        meaning: str,
        reviewer: str,
    ) -> None:
        self.decisions.append((id, status, reviewer))

    def undo(self, platform: str, scope_id: str, id: str, reviewer: str) -> None:
        self.decisions.append((id, "undo", reviewer))

    def history(self, platform: str, scope_id: str, id: str, limit: int) -> list[Any]:
        return [LearningHistoryRecord(from_status="pending", to_status="approved", reviewer="u1")]


class FakeResidentMemory:
    """``atri_x.memory.resident.Store`` 替身。"""

    def __init__(self) -> None:
        self.normal_cleared = 0
        self.core_writes: list[str] = []

    def delete_normal(self, scope: Any) -> None:
        self.normal_cleared += 1

    def write_core(self, scope: Any, text: str) -> None:
        self.core_writes.append(text)


class FakeFileBackups:
    """``RollbackStore`` 替身。"""

    def __init__(self) -> None:
        self.records = [
            RollbackInfo(
                id=1,
                path="a.txt",
                created=False,
                edited_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
                revision_before="r1",
                revision_after="r2",
                bytes=12,
            )
        ]

    def list(self, session_id: str) -> list[RollbackInfo]:
        return list(self.records)

    def get(self, session_id: str, record_id: int) -> tuple[RollbackInfo, bool]:
        for record in self.records:
            if record.id == record_id:
                return record, True
        return RollbackInfo(), False

    def restore(self, ctx: Any, session_id: str, record_id: int) -> RollbackInfo:
        record, ok = self.get(session_id, record_id)
        if not ok:
            from atri_x.errors import RollbackNotFoundError

            raise RollbackNotFoundError("rollback not found")
        return record


# ---------------------------------------------------------------------------
# 测试台
# ---------------------------------------------------------------------------


class Harness:
    """一套可用的 ``Deps`` + ``Router``。

    默认是 CLI 私聊、超管身份；``group=True`` 切到群作用域，``admin=False`` 降级成
    普通用户（用来验证命令级准入）。
    """

    def __init__(
        self,
        *,
        group: bool = False,
        admin: bool = True,
        group_role: str = "",
        with_services: bool = True,
    ) -> None:
        self.store = None
        self.sessions, self.store = new_test_service()
        self.scope_value = Scope(
            actor_id="u1",
            platform="qq",
            platform_scope_id=GROUP_SCOPE_ID if group else "private:1",
            is_cli=False,
        )
        self.actor = Actor(
            id="u1",
            platform="qq",
            platform_user_id="10001",
            nickname="昵称",
            group_card="群名片",
            display_name="群名片",
            role=RoleSuperadmin if admin else RoleUser,
            group_role=group_role,
        )
        self.ctx = with_actor(None, self.actor)
        self.requests = RequestManager()
        self.turns = TurnManager()
        self.audits: list[tuple[str, tuple[Any, ...]]] = []
        self.cancelled_inbox: list[str] = []

        deps = Deps(
            sessions=self.sessions,
            requests=self.requests,
            turns=self.turns,
            store=self.store,
            scope=lambda ctx: self.scope_value,
            session_state=new_session_command_state(
                DEFAULT_SESSION_LIST_PAGE_SIZE, DEFAULT_RETENTION_DAYS
            ),
            audit=self._audit,
            cancel_session_inbox=self._cancel_inbox,
        )
        if with_services:
            deps.models = self.models = FakeModels()
            deps.compact = self.compact = FakeCompact()
            deps.context_status = self.context_status = FakeContextStatus()
            deps.context_policy = self.context_policy = FakeContextPolicy()
            deps.group_policy = self.group_policy = FakeGroupPolicy()
            deps.group_knowledge = self.group_knowledge = FakeGroupKnowledge()
            deps.member_panel = self.member_panel = FakeMemberPanel()
            deps.group_services = self.group_services = FakeGroupServices()
            deps.tools = self.tools = FakeTools()
            deps.hooks = self.hooks = FakeHooks()
            deps.logs = self.logs = FakeLogs()
            deps.runtime_state = self.runtime_state = FakeRuntimeState()
            deps.doctor = self.doctor = FakeDoctor()
            deps.characters = self.characters = FakeCharacters()
            deps.angel_memory = self.angel_memory = FakeAngelMemory()
            deps.resident_memory = self.resident_memory = FakeResidentMemory()
            deps.self_learning = self.self_learning = FakeSelfLearning()
            deps.file_backups = self.file_backups = FakeFileBackups()
        self.deps = deps
        self.router: Router = new_default_router(deps)

    # -- 辅助 ---------------------------------------------------------------
    def _audit(self, event: str, *fields: Any) -> None:
        self.audits.append((event, fields))

    def _cancel_inbox(self, session_id: str) -> int:
        self.cancelled_inbox.append(session_id)
        return 1

    def create_session(self, title: str = "会话", mode: str = "work") -> Any:
        from atri_x.session import CreateRequest

        return self.sessions.create(
            self.scope_value, CreateRequest(title=title, mode=mode)
        )

    def audit_events(self) -> list[str]:
        return [event for event, _ in self.audits]
