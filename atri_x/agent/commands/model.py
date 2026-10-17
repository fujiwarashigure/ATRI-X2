"""``/model`` 与 ``/checkmodel``（别名 ``/models``）。

对应 Go：``internal/agent/commands/model.go``（504 行）。

``ModelTarget`` 是 Go 里 ``modelTarget``（字符串枚举）；Python 用同名常量类 +
字符串值，保持 "``--chat`` → ``"chat"``" 的落点一致（``SelectModelForMode`` 直接
拿 ``atriwisp1`` / ``atriwisp2`` / ``atriwisp3`` 当模式名）。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Completion, Info, Request, Result, new_func
from atri_x.logging import quote_go
from atri_x.storage import SessionModeChat, SessionModeWork
from atri_x.agent.commands.completion import (
    CompletionOption,
    current_completion_token,
)
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.agent.commands.help import format_command_help, wants_command_help
from atri_x.agent.commands.model_types import (
    ModelListOptions,
    ModelListResult,
    ModelOption,
    ModelProfile,
    ModelProviderError,
    ModelSnapshot,
)

__all__ = [
    "ModelModule",
    "ModelTarget",
    "append_model_provider_errors",
    "complete_model_options",
    "format_model_profiles",
    "format_model_snapshot_slots",
    "format_model_snapshots",
    "fuzzy_subsequence_match",
    "model_command_info",
    "model_suffix",
    "new_check_model",
    "new_model",
    "option_only_model_args",
    "parse_model_args",
    "parse_model_list_args",
    "previous_completion_token",
]


class ModelTarget:
    """``/model`` 的目标槽位。对应 Go ``modelTarget`` 及其常量。"""

    CURRENT = "current"
    PROFILES = "profiles"
    SNAPSHOTS = "snapshots"
    SAVE = "save"
    APPLY = "apply"
    DELETE = "delete"
    CHAT = "chat"
    WORK = "work"
    ATRIWISP1 = "atriwisp1"
    ATRIWISP2 = "atriwisp2"
    ATRIWISP3 = "atriwisp3"
    COMPACT = "compact"
    NAMING = "naming"


_USAGE = (
    "/model [--profiles|--snapshots|--save <名字>|--apply <名字>|--delete <名字>|"
    "--chat|--work|--atriwisp1|--atriwisp2|--atriwisp3|--compact|--naming] "
    "<name or number>"
)

_HELP = """Options:
  --profiles           List named model selections (@model:<name>).
  --snapshots          List saved named model snapshots.
  --save <name>        Save the current model selections under <name>.
  --apply <name>       Switch every slot back to the saved snapshot.
  --delete <name>      Delete a saved snapshot.
  --chat <model>       Switch chat mode model.
  --work <model>       Switch work mode model.
  --atriwisp1 <model>    Switch Atrinis atriwisp1 model slot.
  --atriwisp2 <model>    Switch Atrinis atriwisp2 model slot.
  --atriwisp3 <model>    Switch Atrinis atriwisp3 model slot.
  --compact <model>    Switch context compact model.
  --naming <model>     Switch session naming model.

Without a target option, /model switches the current session mode model.
Model can be a list number, model name, or provider/model.

Examples:
  /model 2
  /model --profiles
  /model --snapshots
  /model --save cheap
  /model --apply cheap
  /model --delete cheap
  /model --chat gpt-4o
  /model --work openai/gpt-4.1
  /model --atriwisp2 openai/gpt-4.1
  /model --compact claude-3-5-haiku
  /model --naming gpt-4o-mini"""

_OPTION_TO_TARGET = {
    "--profiles": ModelTarget.PROFILES,
    "--snapshots": ModelTarget.SNAPSHOTS,
    "--save": ModelTarget.SAVE,
    "--apply": ModelTarget.APPLY,
    "--delete": ModelTarget.DELETE,
    "--chat": ModelTarget.CHAT,
    "--work": ModelTarget.WORK,
    "--atriwisp1": ModelTarget.ATRIWISP1,
    "--atriwisp2": ModelTarget.ATRIWISP2,
    "--atriwisp3": ModelTarget.ATRIWISP3,
    "--compact": ModelTarget.COMPACT,
    "-c": ModelTarget.COMPACT,
    "--naming": ModelTarget.NAMING,
    "-n": ModelTarget.NAMING,
}


def model_command_info() -> Info:
    """``/model`` 的 ``Info``。对应 Go ``modelCommandInfo``。"""
    return Info(
        name="model",
        usage=_USAGE,
        description="Switch model for current or specified mode.",
        help=_HELP,
    )


def parse_model_args(prefix: str, args: str) -> tuple[str, str]:
    """拆出模型参数与目标槽位。对应 Go ``parseModelArgs``。

    返回 ``(arg, target)``；同时给出两个目标选项时抛 :class:`ValueError`。
    """
    fields = (args or "").split()
    out: list[str] = []
    target = ModelTarget.CURRENT
    for field in fields:
        next_target = _OPTION_TO_TARGET.get(field, "")
        if next_target != "":
            if target != ModelTarget.CURRENT:
                raise ValueError(f"usage: {prefix}model {_USAGE[len('/model '):]}")
            target = next_target
            continue
        out.append(field)
    return " ".join(out), target


def format_model_profiles(profiles: list[ModelProfile]) -> Result:
    """列出命名模型选择。对应 Go ``formatModelProfiles``。

    失效的 profile 仍然列出但标注 unavailable：名字写对却指向当前进程缺少客户端的
    provider，直接隐藏会让操作者以为是自己名字写错。
    """
    if not profiles:
        return Result(
            content=(
                "no named model profiles configured "
                "(services.toml: model_profiles / model_aliases)"
            )
        )
    out = ["named model profiles (@model:<name>):\n"]
    for profile in profiles:
        status = "available" if profile.available else "unavailable"
        out.append(f"  {profile.name} -> {profile.provider}/{profile.model} ({status})\n")
    return Result(content="".join(out).rstrip("\n"))


def format_model_snapshots(snapshots: list[ModelSnapshot]) -> Result:
    """列出已保存的命名快照。对应 Go ``formatModelSnapshots``。

    空列表也要说明怎么创建，否则用户看到一个空回复不知道下一步做什么。
    """
    if not snapshots:
        return Result(
            content=(
                "no saved model snapshots (use `/model --save <name>` to save the "
                "current selections)"
            )
        )
    out = ["saved model snapshots:\n"]
    for snapshot in snapshots:
        out.append(f"  {snapshot.name}\n")
        for line in format_model_snapshot_slots(snapshot).split("\n"):
            out.append("    " + line + "\n")
    return Result(content="".join(out).rstrip("\n"))


def format_model_snapshot_slots(snapshot: ModelSnapshot) -> str:
    """把一个快照的槽位渲染成多行。对应 Go ``formatModelSnapshotSlots``。"""
    if not snapshot.slots:
        return "(empty snapshot)"
    return "\n".join(
        f"{slot.label} -> {slot.provider}/{slot.model}" for slot in snapshot.slots
    )


def previous_completion_token(args: str, query: str) -> str:
    """返回当前 token 之前那个 token。对应 Go ``previousCompletionToken``。"""
    trimmed = args
    if query and trimmed.endswith(query):
        trimmed = trimmed[: len(trimmed) - len(query)]
    fields = trimmed.split()
    if not fields:
        return ""
    return fields[-1]


def fuzzy_subsequence_match(value: str, query: str) -> bool:
    """子序列模糊匹配。对应 Go ``fuzzySubsequenceMatch``。"""
    if query == "":
        return True
    value = value.lower()
    query = query.lower()
    cursor = 0
    for ch in value:
        if cursor >= len(query):
            break
        if ch == query[cursor]:
            cursor += 1
    return cursor == len(query)


def complete_model_options(query: str, start: int, end: int) -> list[Completion]:
    """补全 ``/model`` 的选项。对应 Go ``completeModelOptions``。"""
    options = [
        CompletionOption("--profiles", description="List named model selections"),
        CompletionOption("--snapshots", description="List saved model snapshots"),
        CompletionOption("--save", description="Save current model selections as a snapshot"),
        CompletionOption("--apply", description="Apply a saved model snapshot"),
        CompletionOption("--delete", description="Delete a saved model snapshot"),
        CompletionOption("--chat", description="Switch chat mode model"),
        CompletionOption("--work", description="Switch work mode model"),
        CompletionOption("--atriwisp1", description="Switch Atrinis atriwisp1 model slot"),
        CompletionOption("--atriwisp2", description="Switch Atrinis atriwisp2 model slot"),
        CompletionOption("--atriwisp3", description="Switch Atrinis atriwisp3 model slot"),
        CompletionOption("--compact", description="Switch context compact model"),
        CompletionOption("--naming", description="Switch session naming model"),
        CompletionOption("-c", description="Switch context compact model"),
        CompletionOption("-n", description="Switch session naming model"),
    ]
    out: list[Completion] = []
    for option in options:
        if option.text.startswith(query):
            out.append(
                Completion(
                    text=option.text,
                    label=option.text,
                    description=option.description,
                    kind="model_option",
                    replace_start=start,
                    replace_end=end,
                )
            )
    return out


def option_only_model_args(args: str) -> bool:
    """最后一个参数是不是"需要值但还没给"的选项。对应 Go ``optionOnlyModelArgs``。"""
    fields = (args or "").split()
    if not fields:
        return False
    return fields[-1] in _OPTION_TO_TARGET


def parse_model_list_args(args: str) -> tuple[str, bool]:
    """``/checkmodel`` 的参数：去掉 ``--fresh`` / ``--refresh``。对应 Go ``parseModelListArgs``。"""
    out: list[str] = []
    fresh = False
    for field in (args or "").split():
        if field in ("--fresh", "--refresh"):
            fresh = True
            continue
        out.append(field)
    return " ".join(out), fresh


def append_model_provider_errors(
    out: list[str], errors: list[ModelProviderError]
) -> None:
    """把 provider 错误追加到输出块。对应 Go ``appendModelProviderErrors``。"""
    has_error = False
    for provider_error in errors:
        if provider_error.err is None:
            continue
        if not has_error:
            out.append("\nmodel provider errors:")
            has_error = True
        out.append(f"\n  - {provider_error.provider}: {provider_error.err}")


def model_suffix(option: ModelOption) -> str:
    """模型行尾的模式/用途标记。对应 Go ``modelSuffix``。"""
    marks = list(option.mode_marks or [])
    if not marks:
        if option.chat_current:
            marks.append("chat")
        if option.work_current:
            marks.append("work")
    if option.compact:
        marks.append("compact")
    if option.naming:
        marks.append("naming")
    if not marks:
        return ""
    return " (" + ", ".join(marks) + ")"


class _ModelCommand:
    """对应 Go ``modelCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return model_command_info()

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        info = self.info()
        deps = self._deps
        if wants_command_help(req.args):
            return format_command_help(req.prefix, info)
        arg, target = parse_model_args(req.prefix, req.args)
        if target == ModelTarget.PROFILES:
            if deps.models is None:
                raise ValueError("model service unavailable")
            return format_model_profiles(deps.models.model_profiles())
        if target == ModelTarget.SNAPSHOTS:
            if deps.models is None:
                raise ValueError("model service unavailable")
            return format_model_snapshots(deps.models.model_snapshots())
        if target == ModelTarget.SAVE:
            if deps.models is None:
                raise ValueError("model service unavailable")
            deps.models.save_model_snapshot(arg)
            return Result(content=f"saved model snapshot: {arg}")
        if target == ModelTarget.APPLY:
            if deps.models is None:
                raise ValueError("model service unavailable")
            applied = await _maybe_await(deps.models.apply_model_snapshot(ctx, arg))
            return Result(
                content="applied model snapshot:\n" + format_model_snapshot_slots(applied)
            )
        if target == ModelTarget.DELETE:
            if deps.models is None:
                raise ValueError("model service unavailable")
            deps.models.delete_model_snapshot(arg)
            return Result(content=f"deleted model snapshot: {arg}")

        if target == ModelTarget.CHAT:
            selected = await _maybe_await(
                deps.models.select_model_for_mode(ctx, SessionModeChat, arg)
            )
        elif target == ModelTarget.WORK:
            selected = await _maybe_await(
                deps.models.select_model_for_mode(ctx, SessionModeWork, arg)
            )
        elif target in (
            ModelTarget.ATRIWISP1,
            ModelTarget.ATRIWISP2,
            ModelTarget.ATRIWISP3,
        ):
            selected = await _maybe_await(
                deps.models.select_model_for_mode(ctx, target, arg)
            )
        elif target == ModelTarget.COMPACT:
            selected = await _maybe_await(deps.models.select_compact_model(arg))
        elif target == ModelTarget.NAMING:
            selected = await _maybe_await(deps.models.select_naming_model(arg))
        else:
            selected = await _maybe_await(deps.models.select_model(ctx, arg))

        if target == ModelTarget.CHAT:
            return Result(
                content=f"switched chat model: {selected.provider}/{selected.model}"
            )
        if target == ModelTarget.WORK:
            return Result(
                content=f"switched work model: {selected.provider}/{selected.model}"
            )
        if target in (
            ModelTarget.ATRIWISP1,
            ModelTarget.ATRIWISP2,
            ModelTarget.ATRIWISP3,
        ):
            return Result(
                content=f"switched {target} model: {selected.provider}/{selected.model}"
            )
        if target == ModelTarget.COMPACT:
            return Result(
                content=f"switched compact model: {selected.provider}/{selected.model}"
            )
        if target == ModelTarget.NAMING:
            return Result(
                content=f"switched naming model: {selected.provider}/{selected.model}"
            )
        return Result(
            content=f"switched to model: {selected.provider}/{selected.model}"
        )

    async def complete(self, ctx: Any, req: Any) -> list[Completion]:
        token = current_completion_token(req)
        query = req.raw[token.start : token.end]
        if query.startswith("-"):
            return complete_model_options(query, token.start, token.end)
        # --apply / --delete 后面跟的是快照名而不是模型名：这里补全已保存的快照，
        # 免得用户必须先去 --snapshots 里抄名字。
        previous = previous_completion_token(req.args, query)
        if previous in ("--apply", "--delete"):
            return self._complete_snapshot_names(query, token.start, token.end)
        if option_only_model_args(req.args) or self._deps.models is None:
            return []
        result = self._deps.models.model_list(query, ModelListOptions())
        items = result.options
        if not items:
            items = self._fuzzy_model_options(query)
        out: list[Completion] = []
        seen: set[str] = set()
        for model in items:
            text = model.provider + "/" + model.model
            if text in seen:
                continue
            seen.add(text)
            out.append(
                Completion(
                    text=text,
                    label=model.model,
                    description=model.provider,
                    kind="model",
                    replace_start=token.start,
                    replace_end=token.end,
                )
            )
        return out

    def _fuzzy_model_options(self, query: str) -> list[ModelOption]:
        """精确前缀没命中时退到子序列模糊匹配。对应 Go ``fuzzyModelOptions``。"""
        query = (query or "").strip().lower()
        if query == "" or self._deps.models is None:
            return []
        items = self._deps.models.model_list("", ModelListOptions()).options
        out: list[ModelOption] = []
        for model in items:
            provider_model = (model.provider + "/" + model.model).lower()
            if (
                fuzzy_subsequence_match(provider_model, query)
                or fuzzy_subsequence_match(model.model.lower(), query)
                or fuzzy_subsequence_match(model.provider.lower(), query)
            ):
                out.append(model)
        return out

    def _complete_snapshot_names(
        self, query: str, start: int, end: int
    ) -> list[Completion]:
        """补全快照名（描述里带上各槽位）。对应 Go ``completeModelSnapshotNames``。"""
        if self._deps.models is None:
            return []
        out: list[Completion] = []
        for snapshot in self._deps.models.model_snapshots():
            if not snapshot.name.startswith(query):
                continue
            parts = [
                f"{slot.label}={slot.provider}/{slot.model}" for slot in snapshot.slots
            ]
            out.append(
                Completion(
                    text=snapshot.name,
                    label=snapshot.name,
                    description=", ".join(parts),
                    kind="model_snapshot",
                    replace_start=start,
                    replace_end=end,
                )
            )
        return out


async def _maybe_await(value: Any) -> Any:
    """接口实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def new_model(deps: Deps) -> Any:
    """对应 Go ``NewModel``。"""
    return _ModelCommand(deps)


def new_check_model(deps: Deps) -> Any:
    """``/checkmodel``（别名 ``/models``）。对应 Go ``NewCheckModel``。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        args, fresh = parse_model_list_args(req.args)
        result: ModelListResult = await _maybe_await(
            deps.models.model_list(args, ModelListOptions(fresh=fresh))
        )
        models = result.options
        out: list[str] = []
        if not models:
            if (args or "").strip() != "":
                out.append(f"no models matching {quote_go(args.strip())}")
            else:
                out.append("no models available")
            append_model_provider_errors(out, result.errors)
            return Result(content="".join(out).rstrip("\n"))

        out.append("available models:\n")
        current_provider = ""
        for model in models:
            if model.provider != current_provider:
                if current_provider != "":
                    out.append("\n")
                current_provider = model.provider
                out.append(f"{current_provider}:\n")
            marker = " "
            if (
                model.current
                or model.chat_current
                or model.work_current
                or model.compact
                or model.naming
            ):
                marker = "*"
            suffix = model_suffix(model)
            if not "".join(out).endswith("\n"):
                out.append("\n")
            out.append(f"  {marker} [{model.index}] {model.model}{suffix}")
        append_model_provider_errors(out, result.errors)
        return Result(content="".join(out).rstrip("\n"))

    return new_func(
        Info(
            name="checkmodel",
            usage="/checkmodel [--fresh|--refresh] [query]",
            description="List or search available models.",
            aliases=["models"],
            help="""Options:
  --fresh, --refresh    Refresh provider model lists before showing results.

Examples:
  /models
  /models claude
  /models --fresh
  /models --refresh""",
        ),
        handle,
    )


class ModelModule:
    """对应 Go ``ModelModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_model, new_check_model)
