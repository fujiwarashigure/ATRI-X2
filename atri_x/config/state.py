"""``state.toml`` 运行态：结构、读写、以及**热加载**（mtime 判定 + 分区合并）。

对应 Go：``internal/config/config.go`` 的 ``StateConfig`` / ``StateModelSnapshot`` /
``GroupPolicyConfig`` / ``GroupKnowledgeEntry`` / ``StateGroupServicesConfig`` /
``StateBudgetConfig`` / ``LoadState`` / ``SaveState``，以及 ``internal/agent`` 的
``state_watch.go`` + ``model.go``（``refreshRuntimeState`` / ``applyRuntimeState`` /
``runtimeStateDigest`` / ``changedRuntimeStateSections`` / ``mergeExternalRuntimeState`` /
``saveRuntimeState``）——热加载的运行时部分按配置层职责收进 :class:`RuntimeStateStore`。

**为什么必须与只读配置分开**：``state.toml`` 由运行中的进程回写；与 app.toml /
services.toml 共用文件会让一次回写把只读配置整份覆盖掉（Go ``Load`` 里对此有
"dedicated writable file" 检查，见 :meth:`atri_x.config.app.AppConfig.validate_image_generation`）。

**热加载语义**（不能简化）：

- 只有 mtime **比本进程已应用的版本更新**时才重读（``force=True`` 时跳过该门限）。
- 覆盖的分区固定为 :data:`RUNTIME_STATE_SECTIONS`：模型选择（``mode_models`` /
  ``compact_model`` / ``naming_model`` / ``model_snapshots``）、``context_overflow``、
  ``group_policy``、``group_knowledge``、``group_services``、``group_runtime``。
- ``[budget]`` 账本由运行中进程**独占**：只在启动恢复时读一次，热加载永不覆盖它
  （用磁盘副本替换内存预留会丢掉在途预留）。
- 回写前**先合并一次外部修改**（:meth:`RuntimeStateStore.save`），否则会覆盖操作员
  刚做的编辑。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from atri_x.config.app_sections import (
    OMITEMPTY,
    ModelSelection,
    StrictConfig,
    load_toml,
    normalize_string_list,
)
from atri_x.net import tomlw

__all__ = [
    "ALLOWED_LEARNING_MODERATION_ACTIONS",
    "ContextOverflowConfig",
    "GroupKnowledgeEntry",
    "GroupPolicyConfig",
    "GroupPollConfig",
    "GroupReminderConfig",
    "GroupRuntimeConfig",
    "GroupSignupConfig",
    "LEARNING_MODERATION_ACTIONS",
    "LEARNING_MODERATION_DECIDE",
    "LEARNING_MODERATION_DELETE",
    "LEARNING_MODERATION_EXPORT",
    "LEARNING_MODERATION_MINE",
    "LEARNING_MODERATION_POLICY",
    "LEARNING_MODERATION_VIEW",
    "MAX_GROUP_MERGE_WINDOW_MS",
    "RUNTIME_STATE_SECTIONS",
    "RuntimeStateReload",
    "RuntimeStateStatus",
    "RuntimeStateStore",
    "STATE_BACKUP_SUFFIX",
    "StateBudgetConfig",
    "StateConfig",
    "StateGroupServicesConfig",
    "StateModelSnapshot",
    "StateSessionConfig",
    "THREAD_MODE_GROUP",
    "canonical_json",
    "changed_runtime_state_sections",
    "load_state",
    "normalize_learning_moderation_actions",
    "runtime_state_digest",
    "save_state",
]

#: 群内共享 Session 的线程模式（零值表示历史遗留的 per_user）。对应 Go ``ThreadModeGroup``。
THREAD_MODE_GROUP = "group"
#: ``merge_window_ms`` 上限。对应 Go ``maxGroupMergeWindowMS``。
MAX_GROUP_MERGE_WINDOW_MS = 10000
#: ``state.toml`` 的崩溃恢复副本后缀。对应 Go ``SaveState`` 的 ``path + ".bak"``。
STATE_BACKUP_SUFFIX = ".bak"

LEARNING_MODERATION_VIEW = "view"
LEARNING_MODERATION_DECIDE = "decide"
LEARNING_MODERATION_MINE = "mine"
LEARNING_MODERATION_DELETE = "delete"
LEARNING_MODERATION_EXPORT = "export"
LEARNING_MODERATION_POLICY = "policy"

LEARNING_MODERATION_ACTIONS: tuple[str, ...] = (
    LEARNING_MODERATION_VIEW,
    LEARNING_MODERATION_DECIDE,
    LEARNING_MODERATION_MINE,
    LEARNING_MODERATION_DELETE,
    LEARNING_MODERATION_EXPORT,
    LEARNING_MODERATION_POLICY,
)
ALLOWED_LEARNING_MODERATION_ACTIONS = frozenset(LEARNING_MODERATION_ACTIONS)

#: 参与热加载的分区（顺序固定，供变更报告使用）。``budget`` 被刻意排除。
RUNTIME_STATE_SECTIONS: tuple[str, ...] = (
    "mode_models",
    "compact_model",
    "naming_model",
    "model_snapshots",
    "context_overflow",
    "group_policy",
    "group_knowledge",
    "group_services",
    "group_runtime",
)


# ---------------------------------------------------------------------------
# 运行态结构
# ---------------------------------------------------------------------------
@dataclass
class StateSessionConfig(StrictConfig):
    default_mode: str = ""


@dataclass
class StateModelSnapshot(StrictConfig):
    """一组命名保存的模型选择（``/model --save``）。

    存在 ``state.toml`` 而不是 app.toml / services.toml：后两者是多个进程共享的只读
    配置，运行中的进程不应改写别人的配置文件。
    """

    mode_models: dict[str, ModelSelection] = field(default_factory=dict, metadata=OMITEMPTY)
    compact_model: ModelSelection = field(default_factory=ModelSelection, metadata=OMITEMPTY)
    naming_model: ModelSelection = field(default_factory=ModelSelection, metadata=OMITEMPTY)


@dataclass
class GroupRuntimeConfig(StrictConfig):
    """单个群 scope 的事件派生运行态；不授予任何权限，只在自身不可用时停付费调用。"""

    state: str = field(default="", metadata=OMITEMPTY)
    reason: str = field(default="", metadata=OMITEMPTY)
    updated_at: str = field(default="", metadata=OMITEMPTY)


@dataclass
class ContextOverflowConfig(StrictConfig):
    """群级长消息保护策略覆盖（``chat`` / ``work`` 两种模式）。"""

    chat: str = field(default="", metadata=OMITEMPTY)
    work: str = field(default="", metadata=OMITEMPTY)


@dataclass
class GroupKnowledgeEntry(StrictConfig):
    """一条确定性 FAQ。``match`` 取 exact（默认）/ contains / keywords。"""

    id: str = ""
    question: str = ""
    answer: str = ""
    aliases: list[str] = field(default_factory=list, metadata=OMITEMPTY)
    keywords: list[str] = field(default_factory=list, metadata=OMITEMPTY)
    match: str = field(default="", metadata=OMITEMPTY)
    enabled: bool | None = field(default=None, metadata=OMITEMPTY)
    updated_at: str = field(default="", metadata=OMITEMPTY)

    def is_enabled(self) -> bool:
        return self.enabled is None or self.enabled


@dataclass
class GroupReminderConfig(StrictConfig):
    id: str = ""
    text: str = ""
    due_at: str = ""
    created_by: str = field(default="", metadata=OMITEMPTY)
    created_by_name: str = field(default="", metadata=OMITEMPTY)
    created_at: str = field(default="", metadata=OMITEMPTY)
    status: str = field(default="", metadata=OMITEMPTY)


@dataclass
class GroupPollConfig(StrictConfig):
    id: str = ""
    question: str = ""
    options: list[str] = field(default_factory=list, metadata=OMITEMPTY)
    votes: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)
    voters: dict[str, str] = field(default_factory=dict, metadata=OMITEMPTY)
    created_by: str = field(default="", metadata=OMITEMPTY)
    created_by_name: str = field(default="", metadata=OMITEMPTY)
    created_at: str = field(default="", metadata=OMITEMPTY)
    status: str = field(default="", metadata=OMITEMPTY)


@dataclass
class GroupSignupConfig(StrictConfig):
    id: str = ""
    title: str = ""
    capacity: int = field(default=0, metadata=OMITEMPTY)
    participants: dict[str, str] = field(default_factory=dict, metadata=OMITEMPTY)
    created_by: str = field(default="", metadata=OMITEMPTY)
    created_by_name: str = field(default="", metadata=OMITEMPTY)
    created_at: str = field(default="", metadata=OMITEMPTY)
    status: str = field(default="", metadata=OMITEMPTY)


@dataclass
class StateGroupServicesConfig(StrictConfig):
    """确定性的群提醒 / 投票 / 报名；各 map 的键是 ``platform:scope``。"""

    reminders: dict[str, list[GroupReminderConfig]] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    polls: dict[str, list[GroupPollConfig]] = field(default_factory=dict, metadata=OMITEMPTY)
    signups: dict[str, list[GroupSignupConfig]] = field(default_factory=dict, metadata=OMITEMPTY)


@dataclass
class StateBudgetConfig(StrictConfig):
    """每日计量账本。

    预留按 日期/种类/scope/actor/call-id 作键，重复的 tool call id 不会重复计数；
    值是 Unix 时间戳，只保留当天与前一天。
    """

    reservations: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)
    digests: dict[str, str] = field(default_factory=dict, metadata=OMITEMPTY)
    tokens: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)
    costs: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)
    retries: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)
    executions: dict[str, str] = field(default_factory=dict, metadata=OMITEMPTY)
    #: 无法用真实 usage 结算的硬预算预留（预占金额保留）。
    uncertain: dict[str, int] = field(default_factory=dict, metadata=OMITEMPTY)


def normalize_learning_moderation_actions(values: Sequence[str]) -> list[str]:
    """只保留白名单动作并按首次出现去重。对应 Go ``normalizeLearningModerationActions``。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = value.strip().lower()
        if text not in ALLOWED_LEARNING_MODERATION_ACTIONS or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


@dataclass
class GroupPolicyConfig(StrictConfig):
    """单群交互与授权策略。是本地服务端策略：提示词与角色**不**决定群管权限。"""

    wake_keywords: list[str] = field(default_factory=list, metadata=OMITEMPTY)
    response_mode: str = field(default="", metadata=OMITEMPTY)
    default_mode: str = field(default="", metadata=OMITEMPTY)
    default_model: str = field(default="", metadata=OMITEMPTY)
    allowed_models: list[str] = field(default_factory=list, metadata=OMITEMPTY)
    #: ``None`` = 未声明（继承全局工具）；``[]`` = 显式空列表（禁用全部群工具）。
    #: 写回时两者都被 omitempty 略去，所以"显式策略"必须靠 ``tool_allowlist_set`` 保留 ——
    #: 这与 Go 的 nil/空切片 + ``,omitempty`` + ``ToolAllowlistSet`` 完全一致。
    tool_allowlist: list[str] | None = field(default=None, metadata=OMITEMPTY)
    tool_allowlist_set: bool = field(default=False, metadata=OMITEMPTY)
    #: 分模式白名单（本仓扩展，上游没有）：``None`` = 该模式没有单独配置、回退到上面的
    #: 通用 ``tool_allowlist``；显式空列表 = 该模式禁止全部工具。``_set`` 用来在
    #: omitempty 写回后仍然记得"这是显式策略"（与 ``tool_allowlist_set`` 同理）。
    tool_allowlist_chat: list[str] | None = field(default=None, metadata=OMITEMPTY)
    tool_allowlist_chat_set: bool = field(default=False, metadata=OMITEMPTY)
    tool_allowlist_work: list[str] | None = field(default=None, metadata=OMITEMPTY)
    tool_allowlist_work_set: bool = field(default=False, metadata=OMITEMPTY)
    image_quota: int = field(default=0, metadata=OMITEMPTY)
    vision_quota: int = field(default=0, metadata=OMITEMPTY)
    asr_quota: int = field(default=0, metadata=OMITEMPTY)
    user_image_quota: int = field(default=0, metadata=OMITEMPTY)
    user_vision_quota: int = field(default=0, metadata=OMITEMPTY)
    user_asr_quota: int = field(default=0, metadata=OMITEMPTY)
    chat_tokens_quota: int = field(default=0, metadata=OMITEMPTY)
    chat_cost_quota: float = field(default=0.0, metadata=OMITEMPTY)
    thread_mode: str = field(default="", metadata=OMITEMPTY)
    merge_window_ms: int = field(default=0, metadata=OMITEMPTY)
    quiet_hours: str = field(default="", metadata=OMITEMPTY)
    group_analysis: bool | None = field(default=None, metadata=OMITEMPTY)
    learning: bool | None = field(default=None, metadata=OMITEMPTY)
    history: bool | None = field(default=None, metadata=OMITEMPTY)
    knowledge: bool | None = field(default=None, metadata=OMITEMPTY)
    services: bool | None = field(default=None, metadata=OMITEMPTY)
    asr: bool | None = field(default=None, metadata=OMITEMPTY)
    learning_moderation: bool = field(default=False, metadata=OMITEMPTY)
    learning_moderation_actions: list[str] = field(
        default_factory=list, metadata=OMITEMPTY
    )

    def normalize(self) -> GroupPolicyConfig:
        """补齐默认值并清掉不安全/空值。对应 Go ``Normalize``。"""
        out = self.copy()
        out.wake_keywords = normalize_string_list(self.wake_keywords or [])
        out.allowed_models = normalize_string_list(self.allowed_models or [])
        # ``tool_allowlist`` 是 nil / 空切片二态字段，**必须在归一化后仍然保留这个区分**：
        #   * ``None``（没有这个键）＝继承全局工具策略，不受群白名单限制；
        #   * 显式列表（含 ``[]``）＝当前群有工具策略，空列表表示禁止全部群工具。
        # 若把 ``None`` 归一成 ``[]``，下一次 :meth:`tool_allowlist_restricted` 会把
        # "空列表非 None" 当成显式策略 → 所有没配过群策略的群都会被判成"禁止全部工具"
        # （真机事故：QQ 群里所有工具调用都回 tool is not in this group's allowlist）。
        if self.tool_allowlist is None:
            out.tool_allowlist = None
        else:
            out.tool_allowlist = normalize_string_list(self.tool_allowlist)
            out.tool_allowlist_set = True
        # 分模式白名单与通用字段同一套 nil / 空切片二态语义。
        for mode_name in ("chat", "work"):
            value = getattr(self, "tool_allowlist_" + mode_name)
            if value is None:
                setattr(out, "tool_allowlist_" + mode_name, None)
                continue
            setattr(
                out,
                "tool_allowlist_" + mode_name,
                normalize_string_list(value),
            )
            setattr(out, "tool_allowlist_" + mode_name + "_set", True)
        out.learning_moderation_actions = normalize_learning_moderation_actions(
            self.learning_moderation_actions or []
        )
        if not out.learning_moderation_actions and out.learning_moderation:
            out.learning_moderation_actions = [
                LEARNING_MODERATION_VIEW,
                LEARNING_MODERATION_DECIDE,
            ]
        mode = self.response_mode.strip().lower()
        if mode == "all":
            out.response_mode = "all"
        elif mode in ("keyword", "keywords"):
            out.response_mode = "keyword"
        elif mode in ("reply", "replies"):
            out.response_mode = "reply"
        elif mode in ("off", "none", "disabled"):
            out.response_mode = "off"
        else:
            out.response_mode = "mention"
        default_mode = self.default_mode.strip().lower()
        out.default_mode = default_mode if default_mode in ("chat", "work") else ""
        thread_mode = self.thread_mode.strip().lower()
        if thread_mode in ("group", "shared", "multi", "multi_user", "multi-user"):
            out.thread_mode = THREAD_MODE_GROUP
        else:
            out.thread_mode = ""
        window = self.merge_window_ms
        out.merge_window_ms = 0 if window < 0 else min(window, MAX_GROUP_MERGE_WINDOW_MS)
        out.default_model = self.default_model.strip()
        out.quiet_hours = self.quiet_hours.strip()
        for name in (
            "image_quota",
            "vision_quota",
            "asr_quota",
            "user_image_quota",
            "user_vision_quota",
            "user_asr_quota",
            "chat_tokens_quota",
            "chat_cost_quota",
        ):
            if getattr(out, name) < 0:
                setattr(out, name, 0)
        return out

    def response_mode_value(self) -> str:
        return self.normalize().response_mode

    def thread_mode_value(self) -> str:
        """零值按向后兼容的 ``per_user`` 处理。对应 Go ``ThreadModeValue``。"""
        return THREAD_MODE_GROUP if self.normalize().thread_mode == THREAD_MODE_GROUP else "per_user"

    def wake_keywords_value(self) -> list[str]:
        return self.normalize().wake_keywords

    def is_group_analysis_enabled(self) -> bool:
        return self.group_analysis is None or self.group_analysis

    def is_learning_enabled(self) -> bool:
        return self.learning is None or self.learning

    def is_history_enabled(self) -> bool:
        return self.history is None or self.history

    def is_knowledge_enabled(self) -> bool:
        return self.knowledge is None or self.knowledge

    def is_services_enabled(self) -> bool:
        return self.services is None or self.services

    def is_asr_enabled(self) -> bool:
        return self.asr is None or self.asr

    def is_zero(self) -> bool:
        """该策略是否等价于「未配置」。对应 Go ``IsZero``。"""
        norm = self.normalize()
        return (
            not norm.wake_keywords
            and norm.response_mode == "mention"
            and norm.default_mode == ""
            and norm.default_model == ""
            and not norm.allowed_models
            and not norm.tool_allowlist
            and not norm.tool_allowlist_set
            and not norm.tool_allowlist_chat
            and not norm.tool_allowlist_chat_set
            and not norm.tool_allowlist_work
            and not norm.tool_allowlist_work_set
            and norm.image_quota == 0
            and norm.vision_quota == 0
            and norm.asr_quota == 0
            and norm.user_image_quota == 0
            and norm.user_vision_quota == 0
            and norm.user_asr_quota == 0
            and norm.chat_tokens_quota == 0
            and norm.chat_cost_quota == 0
            and norm.thread_mode == ""
            and norm.merge_window_ms == 0
            and norm.quiet_hours == ""
            and self.group_analysis is None
            and self.learning is None
            and self.history is None
            and self.knowledge is None
            and self.services is None
            and self.asr is None
            and not self.learning_moderation
            and not norm.learning_moderation_actions
        )

    def tool_allowlist_restricted(self) -> bool:
        """显式工具策略（空列表表示**禁用全部**群工具）。对应 Go ``ToolAllowlistRestricted``。"""
        norm = self.normalize()
        return norm.tool_allowlist_set or bool(norm.tool_allowlist)

    def mode_tool_allowlist(self, mode: str) -> tuple[bool, list[str] | None]:
        """某个会话模式生效的工具白名单：``(是否显式限制, 名单)``。

        优先级：该模式的分模式名单 → 通用 ``tool_allowlist`` → 不限制（返回 ``False, None``）。
        显式空名单表示"该模式禁止全部工具"，与通用字段的 nil/空列表二态一致。
        """
        norm = self.normalize()
        mode_name = (mode or "").strip().lower()
        if mode_name in ("chat", "work"):
            value = getattr(norm, "tool_allowlist_" + mode_name)
            explicit = getattr(norm, "tool_allowlist_" + mode_name + "_set")
            if value is not None or explicit:
                return True, list(value or [])
        if norm.tool_allowlist_set or norm.tool_allowlist:
            return True, list(norm.tool_allowlist or [])
        return False, None

    def mode_tool_allowlist_configured(self, mode: str) -> bool:
        """该模式是否**单独**配置过白名单（不看通用兜底）。"""
        norm = self.normalize()
        mode_name = (mode or "").strip().lower()
        if mode_name not in ("chat", "work"):
            return False
        value = getattr(norm, "tool_allowlist_" + mode_name)
        explicit = getattr(norm, "tool_allowlist_" + mode_name + "_set")
        return value is not None or explicit

    def learning_moderation_actions_value(self) -> list[str]:
        """受权群管的生效动作；老 state 文件默认 view+decide。"""
        norm = self.normalize()
        if not norm.learning_moderation_actions and norm.learning_moderation:
            return [LEARNING_MODERATION_VIEW, LEARNING_MODERATION_DECIDE]
        return list(norm.learning_moderation_actions)


@dataclass
class StateConfig(StrictConfig):
    """整个 ``state.toml``。"""

    session: StateSessionConfig = field(default_factory=StateSessionConfig)
    mode_models: dict[str, ModelSelection] = field(default_factory=dict)
    naming_model: ModelSelection = field(default_factory=ModelSelection)
    compact_model: ModelSelection = field(default_factory=ModelSelection)
    model_snapshots: dict[str, StateModelSnapshot] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    context_overflow: dict[str, ContextOverflowConfig] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    group_policy: dict[str, GroupPolicyConfig] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    group_runtime: dict[str, GroupRuntimeConfig] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    group_knowledge: dict[str, list[GroupKnowledgeEntry]] = field(
        default_factory=dict, metadata=OMITEMPTY
    )
    group_services: StateGroupServicesConfig = field(
        default_factory=StateGroupServicesConfig, metadata=OMITEMPTY
    )
    budget: StateBudgetConfig = field(default_factory=StateBudgetConfig, metadata=OMITEMPTY)

    @classmethod
    def runtime_sections(cls) -> tuple[str, ...]:
        return RUNTIME_STATE_SECTIONS


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_state(path: str | os.PathLike[str]) -> StateConfig | None:
    """读取 ``state.toml``；文件与 ``.bak`` 都不存在时返回 ``None``。

    对应 Go ``LoadState``：正常路径读到的是 ``SaveState`` 的原子替换结果；进程在
    "移走旧文件"与"改名新文件"之间崩溃时，从 ``path + ".bak"`` 恢复最后一份完整版本。
    Python 侧 :func:`save_state` 用 ``os.replace`` 原子替换（Windows 同样成立），
    因此不会产生半写文件；读 ``.bak`` 只为兼容 Go 版本留下的副本。
    """
    try:
        state = load_toml(path, StateConfig, strict=True)
    except FileNotFoundError:
        backup = f"{os.fspath(path)}{STATE_BACKUP_SUFFIX}"
        try:
            state = load_toml(backup, StateConfig, strict=True)
        except FileNotFoundError:
            return None
    assert isinstance(state, StateConfig)
    return state


def save_state(path: str | os.PathLike[str], state: StateConfig) -> None:
    """把运行态写回 ``state.toml``（原子写 + 确定性输出）。对应 Go ``SaveState``。"""
    tomlw.dump(state.to_dict(), path)


def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 热加载
# ---------------------------------------------------------------------------
def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、紧凑分隔符。对应 Go ``runtimeStateDigestOf``（json.Marshal）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def runtime_state_digest(state: StateConfig) -> dict[str, str]:
    """按分区给出内存快照的指纹，用于判断一次热加载到底改了什么。"""
    return {
        "mode_models": canonical_json(
            {key: value.to_dict() for key, value in state.mode_models.items()}
        ),
        "compact_model": canonical_json(state.compact_model.to_dict()),
        "naming_model": canonical_json(state.naming_model.to_dict()),
        "model_snapshots": canonical_json(
            {key: value.to_dict() for key, value in state.model_snapshots.items()}
        ),
        "context_overflow": canonical_json(
            {key: value.to_dict() for key, value in state.context_overflow.items()}
        ),
        "group_policy": canonical_json(
            {key: value.to_dict() for key, value in state.group_policy.items()}
        ),
        "group_knowledge": canonical_json(
            {
                key: [entry.to_dict() for entry in entries]
                for key, entries in state.group_knowledge.items()
            }
        ),
        "group_services": canonical_json(state.group_services.to_dict()),
        "group_runtime": canonical_json(
            {key: value.to_dict() for key, value in state.group_runtime.items()}
        ),
    }


def changed_runtime_state_sections(
    before: Mapping[str, str], after: Mapping[str, str]
) -> list[str]:
    """列出有效值发生变化的分区（固定顺序）。对应 Go ``changedRuntimeStateSections``。"""
    return [
        section
        for section in RUNTIME_STATE_SECTIONS
        if before.get(section) != after.get(section)
    ]


@dataclass
class RuntimeStateReload:
    """一次 ``state.toml`` 刷新的结果。对应 Go ``runtimeStateReload``。"""

    path: str = ""
    #: 是否重读了文件并合并进内存。
    applied: bool = False
    #: 有效值与刷新前不同的分区（固定顺序）。
    changed: list[str] = field(default_factory=list)
    #: 本次应用的文件 mtime。
    mod_time: _dt.datetime | None = None


@dataclass
class RuntimeStateStatus:
    """``state.toml`` 的加载状态。对应 Go ``RuntimeStateStatus``。"""

    path: str = ""
    loaded_mod_time: _dt.datetime | None = None
    file_mod_time: _dt.datetime | None = None
    #: 磁盘上的文件比本进程已应用的版本更新。
    pending: bool = False


def _mod_time_from_ns(ns: int) -> _dt.datetime:
    """把 ns 时间戳转成 UTC aware ``datetime``（整微秒精度，不做浮点除法）。"""
    seconds, remainder = divmod(ns, 1_000_000_000)
    return _dt.datetime.fromtimestamp(seconds, tz=_dt.timezone.utc).replace(
        microsecond=remainder // 1000
    )


def _mod_time(path: str) -> _dt.datetime | None:
    stamp = _mtime_ns(path)
    return None if stamp is None else _mod_time_from_ns(stamp)


class RuntimeStateStore:
    """持有运行态并在外部编辑后热加载。

    典型用法（与 Go 一致，顺序不能颠倒）::

        store = RuntimeStateStore.open(cfg.state_config_path)   # 启动恢复（含 [budget]）
        ...
        store.merge_external()        # 1. 先吸收外部编辑
        store.state.mode_models[...] = ...   # 2. 再改内存
        store.save()                  # 3. 落盘（save 内部还会再合并一次）
    """

    def __init__(self, path: str | os.PathLike[str], state: StateConfig | None = None) -> None:
        self.path = os.fspath(path)
        self._state = state if state is not None else StateConfig()
        #: 已应用的文件 mtime（ns）；None 表示本进程还没从文件读过任何版本。
        self._applied_mtime_ns: int | None = None
        #: 串行化「合并外部修改 → 取快照 → 落盘」全过程（对应 Go ``stateWriteMu``）。
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> RuntimeStateStore:
        """启动恢复：读一次 ``state.toml``（**含 [budget] 账本**）并记下 mtime。"""
        store = cls(path)
        state = load_state(store.path)
        if state is not None:
            store._state = state
            store._applied_mtime_ns = _mtime_ns(store.path)
        return store

    @property
    def state(self) -> StateConfig:
        """内存中的运行态（可变；改动后调 :meth:`save` 落盘）。"""
        return self._state

    @state.setter
    def state(self, value: StateConfig) -> None:
        self._state = value

    def digest(self) -> dict[str, str]:
        return runtime_state_digest(self._state)

    def merge_external(self) -> RuntimeStateReload:
        """吸收外部对 ``state.toml`` 的编辑（不强制，仍受 mtime 门限约束）。"""
        return self.refresh(force=False)

    def refresh(self, force: bool = False) -> RuntimeStateReload:
        """重读 ``state.toml`` 并合并运行态分区；``force=True`` 跳过 mtime 门限。"""
        report = RuntimeStateReload(path=self.path)
        if not self.path:
            return report
        with self._lock:
            mtime_ns = _mtime_ns(self.path)
            if mtime_ns is None:
                return report
            if (
                not force
                and self._applied_mtime_ns is not None
                and mtime_ns <= self._applied_mtime_ns
            ):
                return report
            state = load_state(self.path)
            if state is None:
                return report
            before = self.digest()
            self._apply(state)
            report.applied = True
            report.mod_time = _mod_time(self.path)
            report.changed = changed_runtime_state_sections(before, self.digest())
            self._applied_mtime_ns = mtime_ns
            return report

    def status(self) -> RuntimeStateStatus:
        """报告已加载/磁盘 mtime 与是否存在待应用的编辑。"""
        file_mtime_ns = _mtime_ns(self.path)
        loaded = (
            _mod_time_from_ns(self._applied_mtime_ns)
            if self._applied_mtime_ns is not None
            else None
        )
        pending = self._applied_mtime_ns is None or (
            file_mtime_ns is not None and file_mtime_ns > self._applied_mtime_ns
        )
        return RuntimeStateStatus(
            path=self.path,
            loaded_mod_time=loaded,
            file_mod_time=_mod_time(self.path),
            pending=pending,
        )

    def save(self) -> None:
        """写回 ``state.toml``：**先合并一次外部修改**，再落盘整份运行态。

        与 Go ``saveRuntimeState`` 一致，``[budget]`` 账本随内存版本写出（它就是内存
        独占的那一份），不会被磁盘副本替换。
        """
        if not self.path:
            return
        with self._lock:
            self.merge_external()
            save_state(self.path, self._state)
            self._applied_mtime_ns = _mtime_ns(self.path)

    def replace_runtime_sections(self, state: StateConfig) -> None:
        """把 ``state`` 的运行态分区合并进内存（不动 ``[budget]``）。"""
        with self._lock:
            self._apply(state)

    def _apply(self, state: StateConfig) -> None:
        """合并运行态分区；``[budget]`` 刻意不动（见模块 docstring）。"""
        current = self._state
        current.mode_models = dict(state.mode_models)
        current.compact_model = state.compact_model.copy()
        current.naming_model = state.naming_model.copy()
        current.model_snapshots = dict(state.model_snapshots)
        current.context_overflow = dict(state.context_overflow)
        current.group_policy = {
            key.strip(): value
            for key, value in state.group_policy.items()
            if key.strip()
        }
        current.group_knowledge = {key: list(value) for key, value in state.group_knowledge.items()}
        current.group_services = state.group_services.copy()
        current.group_runtime = dict(state.group_runtime)
