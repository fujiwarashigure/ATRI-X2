"""``persona_control``：成员自己开关「专属人设」（按人长期生效）。

对应需求：每个群友可以给自己绑一张人设卡，之后**和他说话时**机器人按这张卡演；
玩腻了就关掉，恢复 ``SOUL.md`` 的默认人设，且完全不影响其他人的对话。

规则：

* 本人可操作自己的绑定（``OwnerScoped`` + ``RiskLow``，普通成员可直接调用）；
* 超级管理员可以带 ``target_user_id`` 替别人设置；
* 绑定只存 ``<配置目录>/gui/member_personas.json``，不触碰 ``SOUL.md``；
* 关闭 = ``enabled=false``，保留绑定；``clear`` 才删除记录；
* 绑定的卡必须对目标成员可见（公开卡，或他自己建的私有卡）。

提示词注入在 :mod:`atri_x.agent.member_persona_prompt`：启用时顶掉 SOUL 段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atri_x.character import Viewer
from atri_x.errors import AtriXError, InvalidError
from atri_x.llm.llm import ToolSchema
from atri_x.memberprofile.persona import (
    MemberPersona,
    MemberPersonaStore,
    PersonaDataError,
)
from atri_x.security import RoleSuperadmin, actor_from_context
from atri_x.tool.builder import Builder, enum, required
from atri_x.tool.builtin.character import (
    CHARACTER_MANAGE_NAME,
    character_not_found_text,
    character_viewer,
)
from atri_x.tool.builtin.common import decode_args
from atri_x.tool.tool import CallRequest, Info, Result, RiskLow

PERSONA_CONTROL_NAME = "persona_control"

__all__ = ["PERSONA_CONTROL_NAME", "PersonaControlArgs", "PersonaControlTool", "new_persona_control_tool"]


@dataclass
class PersonaControlArgs:
    """``persona_control`` 的参数（Go 无对应工具，Python 侧新增）。"""

    operation: str = ""
    character_id: str = ""
    target_user_id: str = ""
    target_platform: str = ""


class PersonaControlTool:
    """查询 / 绑定 / 开关 / 清除成员专属人设。"""

    def __init__(self, config_dir: str = "", characters: Any = None) -> None:
        self.config_dir = config_dir or ""
        self.characters = characters
        self._store: MemberPersonaStore | None = None

    def name(self) -> str:
        return PERSONA_CONTROL_NAME

    def _persona_store(self) -> MemberPersonaStore:
        if self._store is None:
            self._store = MemberPersonaStore(self.config_dir)
        return self._store

    def info_value(self) -> Info:
        return (
            Builder(PERSONA_CONTROL_NAME)
            .description(
                "管理「专属人设」：把自己绑定的角色卡对所有与自己相关的对话长期启用，"
                "或关掉恢复默认系统人设。operation 支持 status、bind、enable、disable、"
                "clear；只有超级管理员可以带 target_user_id 操作别人。关闭只停用、不删卡。"
            )
            .risk(RiskLow)
            .owner_scoped()
            .tags("character")
            .depends_on(CHARACTER_MANAGE_NAME)
            .build_info()
        )

    def info(self) -> Info:
        return self.info_value()

    def schema(self) -> ToolSchema:
        return (
            Builder(PERSONA_CONTROL_NAME)
            .description("管理成员专属人设（按人长期生效，可随时关闭）。")
            .string(
                "operation",
                "status（查看）、bind（绑定到某张卡并启用）、enable（重新启用）、"
                "disable（关闭，恢复默认人设）、clear（删除绑定）。",
                required(),
                enum("status", "bind", "enable", "disable", "clear"),
            )
            .string("character_id", "bind 用：要绑定的角色卡 id（character_list 可查）。")
            .string("target_user_id", "可选：超级管理员替别人操作时的目标用户 id。")
            .string("target_platform", "可选：目标用户所在平台（默认当前平台）。")
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        args = decode_args(req.arguments, PersonaControlArgs, self.name())
        viewer = character_viewer(ctx)
        operation = (args.operation or "").strip().lower() or "status"
        try:
            platform, user_id = _target_of(ctx, args, viewer)
        except InvalidError as exc:
            return Result(content=str(exc))
        if not platform or not user_id:
            return Result(content="无法确定当前用户身份，不能管理专属人设。")
        if self.characters is None:
            return Result(content="角色素材库未配置，无法绑定专属人设。")
        store = self._persona_store()
        try:
            if operation == "status":
                return Result(content=self._describe(store.get(platform=platform, user_id=user_id)))
            if operation == "list":
                return self._list(store, viewer)
            if operation == "bind":
                return Result(content=self._bind(ctx, store, viewer, platform, user_id, args))
            if operation == "enable":
                return Result(content=self._enable(store, platform, user_id, args))
            if operation in ("disable", "off"):
                return Result(content=self._disable(store, platform, user_id))
            if operation == "clear":
                removed = store.remove(platform, user_id)
                if removed is None:
                    return Result(content=f"{user_id} 本来就没有专属人设绑定。")
                return Result(content=f"已删除 {user_id} 的专属人设绑定，恢复默认系统人设。")
        except (PersonaDataError, OSError) as exc:
            return Result(content=f"专属人设保存失败：{exc}")
        return Result(
            content="operation 必须是 status、bind、enable、disable 或 clear。"
        )

    # -- 各操作 -------------------------------------------------------------
    def _describe(self, persona: MemberPersona | None) -> str:
        if persona is None or not persona.character_id:
            return (
                "你还没有绑定专属人设：当前使用默认系统人设（SOUL.md）。\n"
                "想绑定时先用 character_list 找到角色 id，再调用 "
                'persona_control(operation="bind", character_id="<id>")。'
            )
        state = "已启用（你的对话使用该角色）" if persona.enabled else "已关闭（使用默认系统人设）"
        return (
            f"你绑定的人设卡：{persona.character_id}（{state}）。\n"
            "打开：persona_control(operation=\"enable\")；"
            "关闭：persona_control(operation=\"disable\")；"
            "换卡：persona_control(operation=\"bind\", character_id=\"<id>\")。"
        )

    def _list(self, store: MemberPersonaStore, viewer: Viewer) -> Result:
        if not viewer.superadmin:
            return Result(content="只有超级管理员可以列出所有人的专属人设绑定。")
        items = [persona for persona in store.list() if persona.character_id]
        if not items:
            return Result(content="目前没有任何成员绑定专属人设。")
        lines = [f"共 {len(items)} 位成员绑定了专属人设："]
        for persona in items:
            flag = "启用" if persona.enabled else "关闭"
            lines.append(f"- {persona.platform}:{persona.user_id} → {persona.character_id}（{flag}）")
        return Result(content="\n".join(lines))

    def _bind(
        self,
        ctx: Any,
        store: MemberPersonaStore,
        viewer: Viewer,
        platform: str,
        user_id: str,
        args: PersonaControlArgs,
    ) -> str:
        character_id = (args.character_id or "").strip().lower()
        if not character_id:
            return "bind 需要 character_id：先用 character_list 查角色 id。"
        target_viewer = Viewer(
            platform=platform,
            actor_id=f"{platform}:{user_id}",
            superadmin=bool(viewer.superadmin),
        )
        try:
            item = self.characters.get_visible(ctx, character_id, target_viewer)
        except AtriXError:
            return character_not_found_text(character_id) + "（或该卡对这位成员不可见）"
        saved = store.upsert(
            MemberPersona(
                platform=platform,
                user_id=user_id,
                character_id=item.id,
                enabled=True,
            )
        )
        return (
            f"已为 {user_id} 绑定专属人设：{item.name}（{saved.character_id}）。"
            "下一轮与他的对话开始生效，其他人的对话不受影响；"
            "关闭用 persona_control(operation=\"disable\")。"
        )

    def _enable(
        self,
        store: MemberPersonaStore,
        platform: str,
        user_id: str,
        args: PersonaControlArgs,
    ) -> str:
        current = store.get(platform=platform, user_id=user_id)
        if current is None or not current.character_id:
            return (
                "你还没有绑定的角色卡：先用 bind 并指定 character_id，"
                "绑定成功即会启用。"
            )
        updated = store.set_enabled(platform, user_id, True)
        if updated is None:
            return "没有找到绑定记录。"
        return f"已重新启用专属人设 {updated.character_id}，下一轮对话生效。"

    def _disable(self, store: MemberPersonaStore, platform: str, user_id: str) -> str:
        current = store.get(platform=platform, user_id=user_id)
        if current is None or not current.character_id:
            return "你本来就没有专属人设，当前使用默认系统人设（SOUL.md）。"
        updated = store.set_enabled(platform, user_id, False)
        if updated is None:
            return "没有找到绑定记录。"
        return (
            f"已关闭专属人设 {updated.character_id}，恢复默认系统人设（SOUL.md）。"
            "绑定会保留，随时可以 enable 重新打开。"
        )


def new_persona_control_tool(config_dir: str, characters: Any) -> PersonaControlTool:
    """构造工具（注册入口用）。"""
    return PersonaControlTool(config_dir, characters)


# ---------------------------------------------------------------------------
# 目标身份
# ---------------------------------------------------------------------------
def _target_of(
    ctx: Any, args: PersonaControlArgs, viewer: Viewer
) -> tuple[str, str]:
    """解析"要对谁生效"：默认本人；超管可指定 ``target_user_id``。"""
    actor, ok = actor_from_context(ctx)
    platform = (getattr(actor, "platform", "") or "").strip().lower()
    user_id = (getattr(actor, "platform_user_id", "") or "").strip()
    if not user_id:
        raw = str(getattr(actor, "id", "") or "").strip()
        if ":" in raw:
            platform, user_id = raw.split(":", 1)
    target = (args.target_user_id or "").strip()
    if not target:
        return platform, user_id
    if getattr(actor, "role", "") != RoleSuperadmin:
        raise InvalidError("只有超级管理员可以替别人设置专属人设。")
    target_platform = (args.target_platform or "").strip().lower() or platform
    if ":" in target:
        prefix, rest = target.split(":", 1)
        if prefix and prefix.replace("-", "").isalnum():
            target_platform, target = prefix.lower(), rest
    return target_platform, target.strip()
