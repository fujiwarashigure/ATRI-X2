"""``/faq``（别名 ``/knowledge``、``/kb``、``/groupkb``）：群确定性知识库。

对应 Go：``internal/agent/commands/group_knowledge.go``（293 行）。

``splitCommandArgs`` 在 Go 里定义在本文件，却被 ``group_services.go`` 复用；
本仓保持同样的落位（:mod:`atri_x.agent.commands.group_services` 从这里 import），
避免出现第二份实现。
"""

from __future__ import annotations

from typing import Any

from atri_x.command import Info, Request, Result
from atri_x.logging import quote_go
from atri_x.security import RoleSuperadmin
from atri_x.agent.commands.deps import Deps, Registrar

__all__ = [
    "KnowledgeCommand",
    "KnowledgeModule",
    "knowledge_entry_contains",
    "new_knowledge",
    "parse_knowledge_add_args",
    "preview_knowledge_text",
    "split_command_args",
    "split_knowledge_aliases",
    "split_knowledge_list",
]

_HELP = """Usage:
  /faq                          # 列出当前群知识库
  /faq list [关键词]            # 搜索
  /faq add <问题> => <答案>      # 精确匹配；左侧可用 | 分隔别名
  /faq add-contains <文本> => <答案>
  /faq add-keyword <词1,词2> => <答案>
  /faq remove <id>
  /faq clear --confirm
  /faq test <文本>
  /faq on|off                   # 开关当前群的知识库回答

说明:
  知识库只按本地确定性规则匹配，命中后直接发送答案，不进入 Session 或 LLM。
  exact 需要整句一致（忽略大小写、全半角和常用标点）；contains 只要求包含；
  keywords 要求所有关键词都出现。答案和触发词由服务端保存，不进入模型提示词。"""


def split_command_args(args: str) -> tuple[str, str]:
    """把 ``子命令 其余`` 拆开。对应 Go ``splitCommandArgs``。"""
    args = (args or "").strip()
    if args == "":
        return "", ""
    for index, char in enumerate(args):
        if char in (" ", "\t", "\n"):
            return args[:index], args[index + 1 :].strip()
    return args, ""


def split_knowledge_aliases(value: str) -> list[str]:
    """按 ``|``（含全角）切成去重后的别名列表。对应 Go ``splitKnowledgeAliases``。"""
    fields: list[str] = []
    current: list[str] = []
    for char in value or "":
        if char in ("|", "\uff5c"):
            fields.append("".join(current))
            current = []
            continue
        current.append(char)
    fields.append("".join(current))
    out: list[str] = []
    seen: set[str] = set()
    for field in fields:
        field = field.strip()
        if field == "":
            continue
        key = field.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(field)
    return out


def split_knowledge_list(value: str) -> list[str]:
    """按逗号/顿号/分号切分关键词。对应 Go ``splitKnowledgeList``。"""
    fields: list[str] = []
    current: list[str] = []
    for char in value or "":
        if char in (",", "，", "、", ";", "；"):
            fields.append("".join(current))
            current = []
            continue
        current.append(char)
    fields.append("".join(current))
    return split_knowledge_aliases("|".join(fields))


def parse_knowledge_add_args(rest: str) -> tuple[str, str]:
    """解析 ``<触发词> => <答案>``。对应 Go ``parseKnowledgeAddArgs``。"""
    rest = (rest or "").strip()
    if rest == "":
        raise ValueError("用法：/faq add <问题> => <答案>")
    parts = rest.split("=>", 1)
    if len(parts) != 2:
        parts = rest.split("＝>", 1)
    if len(parts) != 2:
        raise ValueError("请使用 => 分隔问题和答案")
    trigger = parts[0].strip()
    answer = parts[1].strip()
    if trigger == "" or answer == "":
        raise ValueError("问题和答案都不能为空")
    return trigger, answer


def knowledge_entry_contains(entry: Any, query: str) -> bool:
    """条目是否命中搜索串（大小写不敏感）。对应 Go ``knowledgeEntryContains``。"""
    values = [entry.id, entry.question, entry.answer]
    values.extend(entry.aliases or [])
    values.extend(entry.keywords or [])
    return any(query in (value or "").lower() for value in values)


def preview_knowledge_text(value: str, max_runes: int) -> str:
    """单行预览并截断。对应 Go ``previewKnowledgeText``。"""
    value = " ".join((value or "").split())
    if max_runes > 0 and len(value) > max_runes:
        return value[:max_runes] + "..."
    return value


class KnowledgeCommand:
    """对应 Go ``knowledgeCommand``。"""

    def __init__(self, deps: Deps) -> None:
        self._deps = deps

    def info(self) -> Info:
        return Info(
            name="faq",
            aliases=["knowledge", "kb", "groupkb"],
            usage="/faq [list|add|add-contains|add-keyword|remove|clear|test|on|off]",
            description="管理当前群的确定性知识库；命中后本地回答，不调用模型。",
            min_role=RoleSuperadmin,
            allow_group_admin=True,
            help=_HELP,
        )

    async def handle(self, ctx: Any, req: Request) -> Result | None:
        if self._deps.group_knowledge is None:
            raise ValueError("group knowledge service is not configured")
        args = (req.args or "").strip()
        if args == "":
            return await self._list(ctx, "")
        name, rest = split_command_args(args)
        key = name.lower()
        if key in ("list", "show", "search", "ls"):
            return await self._list(ctx, rest)
        if key == "add":
            return await self._add(ctx, rest, "exact")
        if key in ("add-contains", "contains"):
            return await self._add(ctx, rest, "contains")
        if key in ("add-keyword", "add-keywords", "keyword", "keywords"):
            return await self._add(ctx, rest, "keywords")
        if key in ("remove", "delete", "rm"):
            return await self._remove(ctx, rest)
        if key == "clear":
            return await self._clear(ctx, rest)
        if key in ("test", "match"):
            return await self._test(ctx, rest)
        if key in ("on", "enable"):
            return await self._set_enabled(ctx, True)
        if key in ("off", "disable"):
            return await self._set_enabled(ctx, False)
        if key in ("help", "?"):
            return Result(content=self.info().help)
        raise ValueError(f"未知的 /faq 子命令 {quote_go(name)}；发送 /faq help 查看用法")

    async def _list(self, ctx: Any, query: str) -> Result:
        entries = await _maybe_await(self._deps.group_knowledge.group_knowledge_list(ctx))
        query = (query or "").strip().lower()
        out = [f"群知识库（{len(entries)} 条）\n"]
        matched = 0
        for entry in entries:
            if query != "" and not knowledge_entry_contains(entry, query):
                continue
            matched += 1
            mode = entry.match or "exact"
            out.append(f"- {entry.id} [{mode}] {preview_knowledge_text(entry.question, 60)}\n")
            if entry.aliases:
                out.append("  别名：" + "、".join(entry.aliases) + "\n")
            if entry.keywords:
                out.append("  关键词：" + "、".join(entry.keywords) + "\n")
            out.append("  答案：" + preview_knowledge_text(entry.answer, 80) + "\n")
        if matched == 0:
            if query == "":
                return Result(
                    content="当前群还没有知识库条目。使用 /faq add 问题 => 答案 添加。"
                )
            return Result(content=f"没有匹配 {quote_go(query)} 的知识条目。")
        out.append("修改：/faq add、/faq remove <id>、/faq clear --confirm")
        return Result(content="".join(out).rstrip("\n"))

    async def _add(self, ctx: Any, rest: str, mode: str) -> Result:
        trigger, answer = parse_knowledge_add_args(rest)
        question = ""
        aliases: list[str] = []
        keywords: list[str] = []
        if mode == "keywords":
            keywords = split_knowledge_list(trigger)
            if not keywords:
                raise ValueError("关键词不能为空")
            question = "、".join(keywords)
        else:
            parts = split_knowledge_aliases(trigger)
            if not parts:
                raise ValueError("问题不能为空")
            question = parts[0]
            aliases = parts[1:]
        entry = await _maybe_await(
            self._deps.group_knowledge.group_knowledge_add(
                ctx, question, answer, mode, aliases, keywords
            )
        )
        return Result(
            content=(
                f"已添加知识 {entry.id} [{entry.match}]："
                f"{preview_knowledge_text(entry.question, 60)}"
            )
        )

    async def _remove(self, ctx: Any, id_: str) -> Result:
        id_ = (id_ or "").strip()
        if id_ == "":
            raise ValueError("请指定要删除的知识 id")
        removed = await _maybe_await(
            self._deps.group_knowledge.group_knowledge_remove(ctx, id_)
        )
        if not removed:
            return Result(content=f"没有找到知识 {quote_go(id_)}")
        return Result(content=f"已删除知识 {id_}")

    async def _clear(self, ctx: Any, rest: str) -> Result:
        if (rest or "").strip().lower() != "--confirm":
            return Result(content="请使用 /faq clear --confirm 确认清空当前群知识库。")
        count = await _maybe_await(
            self._deps.group_knowledge.group_knowledge_clear(ctx)
        )
        return Result(content=f"已清空 {count} 条知识。")

    async def _test(self, ctx: Any, text: str) -> Result:
        text = (text or "").strip()
        if text == "":
            raise ValueError("请输入要测试的文本")
        entry, ok = await _maybe_await(
            self._deps.group_knowledge.group_knowledge_test(ctx, text)
        )
        if not ok:
            return Result(content="未匹配到任何知识条目。")
        return Result(
            content=(
                f"匹配 {entry.id} [{entry.match}]："
                f"{preview_knowledge_text(entry.question, 60)}\n"
                f"答案：{preview_knowledge_text(entry.answer, 120)}"
            )
        )

    async def _set_enabled(self, ctx: Any, enabled: bool) -> Result:
        if self._deps.group_policy is None:
            raise ValueError("group policy service is not configured")
        value = "on" if enabled else "off"
        content = await _maybe_await(
            self._deps.group_policy.set_group_policy(ctx, "knowledge", value)
        )
        return Result(content=content)


async def _maybe_await(value: Any) -> Any:
    """服务实现可能是协程也可能是同步返回值；两种都接受。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def new_knowledge(deps: Deps) -> Any:
    """对应 Go ``NewKnowledge``。"""
    return KnowledgeCommand(deps)


class KnowledgeModule:
    """对应 Go ``KnowledgeModule``。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        registrar.register(new_knowledge(deps))
