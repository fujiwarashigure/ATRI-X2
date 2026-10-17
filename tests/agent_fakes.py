"""``atri_x.agent`` 测试用的最小 Agent 替身。

放在 ``tests/agent_fakes.py``（不是 ``test_*.py``）故意如此：``unittest discover``
的默认模式是 ``test*.py``，因此它不会被当成测试模块收集，但可以被
``from tests.agent_fakes import FakeAgent`` 复用。

字段名刻意与 ``atri_x.agent.core.Agent``（即 Go 的 ``*Agent``）一致：群策略 / 群知识
/ 群服务 / 工具缓存都直接挂在本对象上，从而验证"状态只有一份"。
"""

from __future__ import annotations

import threading
from typing import Any

from atri_x.config import GroupKnowledgeConfig, GroupPolicyConfig, GroupServicesConfig
from atri_x.security import Actor, GroupRoleAdmin, GroupRoleMember, RoleSuperadmin, RoleUser
from atri_x.session import Scope
from atri_x.storage import Message, Session


class FakeTool:
    """最小工具对象（``Info`` 用匿名对象）。"""

    def __init__(self, name: str, *, hidden: bool = False, foreground_only: bool = False) -> None:
        self._name = name
        self._hidden = hidden
        self._foreground_only = foreground_only

    def info(self) -> Any:
        return type(
            "Info",
            (),
            {
                "name": self._name,
                "description": "desc of " + self._name,
                "source": "builtin",
                "risk": "low",
                "hidden": self._hidden,
                "foreground_only": self._foreground_only,
                "owner_scoped": False,
                "vision_required": False,
                "superadmin_only": False,
                "tags": [],
            },
        )()

    def schema(self) -> Any:
        from atri_x.llm import ToolFunctionSchema, ToolSchema

        return ToolSchema(function=ToolFunctionSchema(name=self._name))


class FakeRegistry:
    """最小注册表（只实现被测路径用到的方法）。"""

    def __init__(self, tools: dict[str, Any] | None = None) -> None:
        self._tools = dict(tools or {})

    def register(self, name: str, tool: Any = None) -> None:
        self._tools[name] = tool or FakeTool(name)

    def get(self, name: str) -> tuple[Any, bool]:
        tool = self._tools.get(name)
        return (tool, True) if tool is not None else (None, False)

    def list(self) -> list[Any]:
        return [tool.info() for tool in self._tools.values()]

    def schemas(self) -> list[Any]:
        return [tool.schema() for tool in self._tools.values()]

    def schemas_for_context(self, allowed: Any = None) -> list[Any]:
        out = []
        for tool in self._tools.values():
            if allowed is not None and not allowed(tool.info()):
                continue
            out.append(tool.schema())
        return out

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def names_by_tag(self, tag: str, allowed: Any = None) -> list[str]:
        del tag, allowed
        return []

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    async def discover_details(self, ctx: Any, names: list[str], allowed: Any = None) -> tuple[list[Any], list[Any]]:
        from atri_x.tool.detail import DiscoveredTool, public_info

        out = []
        for name in names or ():
            tool, ok = self.get(name)
            if not ok:
                continue
            if allowed is not None and not allowed(tool):
                continue
            out.append(DiscoveredTool(info=public_info(tool.info()), schema=tool.schema()))
        return out, []


class FakeToolRuntime:
    """``tool_runtime``（与 core.py 的 ``_ToolRuntime`` 同形）。"""

    def __init__(self, registry: Any = None) -> None:
        self.registry = registry
        self.manager = None
        self.config = None
        self.provider = None
        self.default_provider = True


class FakeMessageRepository:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def append(self, message: Message) -> None:
        self.messages.append(message)

    def list_by_session(self, session_id: str) -> list[Message]:
        return [message for message in self.messages if message.session_id == session_id]


class FakeSessionRepository:
    def __init__(self) -> None:
        self.rows: dict[str, Session] = {}
        self.counter = 0

    def create(self, session: Session) -> None:
        self.counter += 1
        if not session.id:
            session.id = "s%d" % self.counter
        self.rows[session.id] = session

    def get(self, id: str) -> Session:
        if id not in self.rows:
            from atri_x.errors import NotFoundError

            raise NotFoundError(id)
        return self.rows[id]

    def mutate(self, id: str, update: Any) -> Session:
        row = self.get(id)
        update(row)
        return row


class FakeStore:
    def __init__(self) -> None:
        self._sessions = FakeSessionRepository()
        self._messages = FakeMessageRepository()

    def sessions(self) -> FakeSessionRepository:
        return self._sessions

    def messages(self) -> FakeMessageRepository:
        return self._messages

    def media(self) -> None:
        return None


class FakePolicy:
    """只实现被测路径需要的方法。"""

    def can_use_tool(self, actor: Any, risk: str, owner_scoped: bool = False) -> bool:
        del actor, risk, owner_scoped
        return True

    def needs_tool_confirmation(self, actor: Any, risk: str) -> bool:
        del actor, risk
        return False


class FakePlatform:
    def name(self) -> str:
        return "qq"


class FakeAgent:
    """鸭子类型的 Agent 替身。"""

    def __init__(
        self,
        *,
        group: bool = True,
        scope_id: str = "group:42",
        registry: Any = None,
        group_policy: dict[str, GroupPolicyConfig] | None = None,
    ) -> None:
        self.platform = FakePlatform()
        self.security_policy = FakePolicy()
        self._group = group
        self.scope_id = scope_id
        self.group_policy: dict[str, Any] = dict(group_policy or {})
        self.group_knowledge: dict[str, list[Any]] = {}
        self.group_knowledge_cfg = GroupKnowledgeConfig()
        self.group_services_cfg = GroupServicesConfig()
        self.group_reminders: dict[str, list[Any]] = {}
        self.group_polls: dict[str, list[Any]] = {}
        self.group_signups: dict[str, list[Any]] = {}
        self.group_runtime: dict[str, Any] = {}
        self.group_services_started = False
        self.discovered_tools: dict[str, dict[str, Any]] = {}
        self.auto_confirm_mu = threading.Lock()
        self.tool_runtime = FakeToolRuntime(registry)
        self.tools_config = None
        self.store = FakeStore()
        self.services: dict[str, Any] = {}
        self.audit_calls: list[tuple] = []
        self.sent: list[str] = []
        self.notices: list[Any] = []
        self.saved = 0
        self.actor_role = RoleUser
        self.group_role = GroupRoleMember
        self.tool_aliases: dict[str, str] = {}
        self.tool_profiles: dict[str, list[str]] = {}
        self.model_aliases: dict[str, str] = {}
        self.model_profiles: dict[str, Any] = {}
        self.sessions = None
        self.turns = None
        self.requests = None
        self.policy_calls: list[tuple[str, Any]] = []

    # -- scope / actor ------------------------------------------------------
    def scope(self, ctx: Any) -> Scope:  # noqa: ARG002
        return Scope(
            actor_id="u1",
            platform="qq",
            platform_scope_id=self.scope_id if self._group else "private:u1",
        )

    def base_scope(self, ctx: Any) -> Scope:
        return self.scope(ctx)

    def is_group_scope(self, ctx: Any) -> bool:
        return self._group

    def actor(self, ctx: Any) -> Actor:  # noqa: ARG002
        return Actor(
            id="u1",
            platform="qq",
            platform_user_id="10001",
            nickname="nick",
            group_card="card",
            display_name="display",
            role=self.actor_role,
            group_role=self.group_role,
        )

    # -- 侧效 ---------------------------------------------------------------
    def audit(self, *args: Any) -> None:
        self.audit_calls.append(args)

    def audit_warn(self, *args: Any, **kwargs: Any) -> None:
        """与 :meth:`audit` 同形记录，事件名仍在 ``call[0]``。

        真实的 :meth:`atri_x.agent.core.Agent.audit_warn` 也是
        ``audit_warn(event, *attrs)``，事件名就是第一个位置参数；早期这里多塞了一个
        ``"warn"`` 前缀，会让 ``call[0] == "<event>"`` 形式的断言读不到事件名。
        """
        del kwargs
        self.audit_calls.append(args)

    async def send_chat(self, ctx: Any, text: str) -> None:  # noqa: ARG002
        self.sent.append(text)

    async def send_chat_with_receipt(self, ctx: Any, text: str) -> Any:  # noqa: ARG002
        from atri_x.delivery import Receipt

        self.sent.append(text)
        return Receipt()

    def send_notice(self, ctx: Any, notice: Any) -> Any:  # noqa: ARG002
        from atri_x.delivery import Receipt

        self.notices.append(notice)
        return Receipt()

    def save_runtime_state(self) -> None:
        self.saved += 1

    def client_for_provider(self, provider: str) -> Any:  # noqa: ARG002
        return object()

    def resolve_group_model_selection(self, raw: str) -> Any:
        from atri_x.agent.group_policy import resolve_group_model_selection

        return resolve_group_model_selection(self, raw)

    def group_model_selection_allowed(self, policy: Any, selection: Any, raw: str) -> bool:
        from atri_x.agent.group_policy import group_model_selection_allowed

        return group_model_selection_allowed(self, policy, selection, raw)


def superadmin_actor() -> Actor:
    return Actor(id="root", role=RoleSuperadmin, group_role=GroupRoleAdmin)
