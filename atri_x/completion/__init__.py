"""平台补全服务：命令名/参数、``@tool:``、``@skill:``、``@char:``、风险确认、Fork。

对应 Go 包：``internal/completion``（``completion.go`` / ``router.go`` /
``tool.go`` / ``character.go`` / ``risk.go`` / ``fork.go``）。
"""

from __future__ import annotations

from atri_x.completion.character import (  # noqa: F401
    KIND_CHARACTER_DIRECTIVE,
    CharacterDirectiveSource,
    CharacterLister,
    CharacterOption,
    KindCharacterDirective,
)
from atri_x.completion.completion import (  # noqa: F401
    Item,
    Request,
    Service,
    Source,
    new_service,
    texts,
)
from atri_x.completion.fork import (  # noqa: F401
    KIND_FORK_MESSAGE,
    ForkMessageSource,
    KindForkMessage,
    LoaderFactory,
    ScopeFunc,
    has_fork_args,
)
from atri_x.completion.risk import (  # noqa: F401
    KIND_RISK_CONFIRMATION,
    PHASE_AWAIT_RISK_CONFIRM,
    KindRiskConfirmation,
    RiskConfirmationSource,
    has_any_command_args,
)
from atri_x.completion.router import (  # noqa: F401
    KIND_COMMAND,
    KindCommand,
    RouterSource,
)
from atri_x.completion.tool import (  # noqa: F401
    KIND_SKILL_DIRECTIVE,
    KIND_TOOL_DIRECTIVE,
    KIND_TOOL_TAG_DIRECTIVE,
    ActorFunc,
    KindSkillDirective,
    KindToolDirective,
    KindToolTagDirective,
    PolicyFunc,
    ToolDirectiveSource,
    ToolNamesByTagFunc,
    ToolRegistryFunc,
    ToolTagsFunc,
    fuzzy_match,
    is_allowed_skill,
    is_plain_allowed_tool,
    match_infos,
    match_strings,
)

__all__ = [name for name in dir() if not name.startswith("_")]
