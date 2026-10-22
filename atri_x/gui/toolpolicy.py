"""「工具权限」页的策略读写层：群工具白名单（``state.toml`` 的 ``[group_policy]``）。

把"读 / 写 / 预设"和 Tk 界面拆开，页面只负责画菜单和调这几个函数：

- :func:`load_tool_choices` —— 内置工具名清单（静态常量，不需要后台服务在跑）；
- :func:`load_scope_policies` —— 每个群的工具授权 ``{scope: (mode, names)}``；
- :func:`save_scope_policy` —— 写某个群的授权（``inherit`` 删键、``none`` 写空数组、
  ``whitelist`` 写名单并自动补 :data:`DISCOVER_TOOL`）；
- :func:`discover_group_scopes` —— 从会话库 / 聊天历史库读出真实出现过的群作用域，
  给界面做下拉候选，避免用户手打；
- :func:`normalize_scope` / :func:`scope_is_canonical` —— 作用域归一与校验。

授权语义（0.9.12 真机事故的教训）：``tool_allowlist`` 是 **nil / 空列表二态**——
没配过（``None``）表示"继承全局"，显式空列表表示"禁止全部"；读的时候必须保留这个
区分，写的时候 ``inherit`` 走 ``None`` 而不是 ``[]``。

作用域写法（0.9.13 真机事故的教训）：运行时查表键是
``context_overflow_key(scope) = platform + ":" + platform_scope_id``，也就是
``qqonebot:group:123456789`` 这种**半角冒号**形式。界面曾把从「群友」页复制来的
``qqonebot · group:123456789``（间隔号 ``U+00B7``）原样落盘，导致整份白名单写在一个
永远匹配不上的键上、静默失效。所以这里统一做两件事：

1. 写之前把常见的"像冒号"的字符（``·`` / ``・`` / ``•`` / 全角 ``：`` 等）和空白
   归一成半角 ``:``，并按 ``platform:scope_id`` 校验；
2. 顺手把文件里已经存在的旧写法小节迁移到规范键上（:func:`migrate_legacy_scope_keys`）。

同一份策略也能在群里改（``/tools allow <工具,工具|all|none>``），本模块不管命令侧，
只保证桌面端写进去的格式与命令侧一致。
"""

from __future__ import annotations

import os
import sqlite3

from atri_x.gui.configedit import (
    ConfigEditError,
    patch_section,
    read_toml,
    remove_section,
)

__all__ = [
    "DISCOVER_TOOL",
    "FALLBACK_TOOLS",
    "MODE_ALLOWLIST_KEYS",
    "PRESET_COMMON",
    "PRESET_READONLY",
    "canonical_scope",
    "discover_group_scopes",
    "load_scope_mode_policies",
    "load_scope_policies",
    "load_tool_choices",
    "migrate_legacy_scope_keys",
    "normalize_scope",
    "save_scope_mode_policy",
    "save_scope_policy",
    "scope_is_canonical",
    "scope_problem",
]

#: 作用域里经常被误用的"像冒号"的字符（中文输入法 / 界面展示复制）。
_SCOPE_SEPARATOR_TRANSLATION = str.maketrans(
    {
        "\u00b7": ":",  # · 间隔号（「群友」页展示用）
        "\u30fb": ":",  # ・ 片假名中点
        "\u2022": ":",  # • 项目符号
        "\u2219": ":",  # ∙ 点运算符
        "\u2027": ":",  # ‧ 连字点
        "\uff1a": ":",  # ： 全角冒号
        "\ufe55": ":",  # ﹕ 小冒号
        "\u2236": ":",  # ∶ 比例号
    }
)

#: 白名单里必须一起放行的工具：没有它，模型连"还有哪些工具可用"都发现不了。
DISCOVER_TOOL = "discover_tool"

#: 常用工具预设（勾一下就够日常问答用）。
PRESET_COMMON = (
    DISCOVER_TOOL,
    "web_search",
    "web_extract",
    "search_chat_history",
    "angel_recall",
    "angel_remember",
    "resident_memory",
    "view_image",
    "prompt_library_search",
)

#: 只读安全预设（不写文件、不跑 shell、不发消息）。
PRESET_READONLY = (
    DISCOVER_TOOL,
    "web_search",
    "web_extract",
    "search_chat_history",
    "angel_recall",
    "resident_memory",
    "prompt_library_search",
    "view_image",
)

#: 取不到静态目录时的兜底清单（保证界面仍可用，不因为导入失败而空白）。
FALLBACK_TOOLS = (
    DISCOVER_TOOL,
    "web_search",
    "web_extract",
    "search_chat_history",
    "angel_recall",
    "angel_remember",
    "resident_memory",
    "view_image",
    "prompt_library_search",
    "group_analysis",
    "read_file",
    "write_file",
    "shell",
)


def load_tool_choices() -> list[str]:
    """取内置工具名清单（静态常量，不需要后台服务在跑）。"""
    try:
        from atri_x.tool.builtin.register import REGISTERED_TOOLS

        names = {str(name).strip() for name in REGISTERED_TOOLS if str(name).strip()}
        if names:
            return sorted(names)
    except Exception:  # noqa: BLE001 - 取不到目录时用兜底清单，界面不空着
        pass
    return sorted(FALLBACK_TOOLS)


def load_scope_policies(state_path: str) -> dict[str, tuple[str, list[str]]]:
    """读 ``state.toml`` 里每个群的工具授权：``{scope: (mode, names)}``。"""
    if not state_path or not os.path.exists(state_path):
        return {}
    data = read_toml(state_path)
    section = data.get("group_policy")
    if not isinstance(section, dict):
        return {}
    out: dict[str, tuple[str, list[str]]] = {}
    for scope, entry in section.items():
        scope_key = str(scope).strip()
        if not scope_key:
            continue
        values = entry if isinstance(entry, dict) else {}
        raw = values.get("tool_allowlist")
        explicit = values.get("tool_allowlist_set")
        if raw is None and not explicit:
            out[scope_key] = ("inherit", [])
            continue
        names = [str(item).strip() for item in raw] if isinstance(raw, list) else []
        names = [item for item in names if item]
        if not names:
            out[scope_key] = ("none", [])
        else:
            out[scope_key] = ("whitelist", names)
    return out


def save_scope_policy(state_path: str, scope: str, mode: str, names: list[str]) -> None:
    """把某个群的工具授权写进 ``state.toml``。

    ``inherit`` 时删掉这两个键（回到"继承全局"），``none`` 写空数组，
    ``whitelist`` 写工具名并自动补上 ``discover_tool``。

    作用域先归一化（``·`` / 全角冒号 / 空白 → ``:``）并校验；写之前把同一作用域的
    旧写法小节迁移过来（否则旧键会一直留在文件里、界面看起来又是另一个群）。
    """
    scope = normalize_scope(scope)
    migrate_legacy_scope_keys(state_path)
    section = f'group_policy."{scope}"'
    if mode == "inherit":
        patch_section(
            state_path, section, {"tool_allowlist": None, "tool_allowlist_set": None}
        )
        # 只在这个小节确实空了的时候才删小节：其它群策略字段（唤醒词、额度……）
        # 可能也写在同一段里，不能一删了之。
        raw = read_toml(state_path).get("group_policy")
        entry = raw.get(scope) if isinstance(raw, dict) else None
        if not entry:
            remove_section(state_path, section)
        return
    if mode == "none":
        patch_section(state_path, section, {"tool_allowlist": []})
        return
    cleaned: list[str] = []
    for name in names:
        text = str(name).strip()
        if text and text.lower() not in ("*", "all") and text not in cleaned:
            cleaned.append(text)
    if DISCOVER_TOOL not in cleaned:
        cleaned.insert(0, DISCOVER_TOOL)
    patch_section(state_path, section, {"tool_allowlist": cleaned})


#: 分模式白名单对应的 ``state.toml`` 字段后缀（与 ``GroupPolicyConfig`` 一致）。
MODE_ALLOWLIST_KEYS: tuple[str, ...] = ("chat", "work")


def load_scope_mode_policies(
    state_path: str,
) -> dict[str, dict[str, tuple[str, list[str]]]]:
    """读每个群的**分模式**工具授权。

    返回 ``{scope: {mode: (授权模式, 工具名), "generic": (...)}}``，其中 ``mode`` 取
    ``"chat"`` / ``"work"``，授权模式是 ``inherit`` / ``whitelist`` / ``none``。
    分模式字段只看该模式自己的键（``tool_allowlist_chat`` / ``tool_allowlist_work``）；
    某个模式没配置就是 ``inherit``，运行时再回退到通用 ``tool_allowlist``（同一份策略里
    的 ``generic`` 项，界面上单独展示）。
    """
    if not state_path or not os.path.exists(state_path):
        return {}
    data = read_toml(state_path)
    section = data.get("group_policy")
    if not isinstance(section, dict):
        return {}
    out: dict[str, dict[str, tuple[str, list[str]]]] = {}
    for scope, entry in section.items():
        scope_key = str(scope).strip()
        if not scope_key:
            continue
        values = entry if isinstance(entry, dict) else {}
        policies: dict[str, tuple[str, list[str]]] = {}
        for mode_key in MODE_ALLOWLIST_KEYS:
            policies[mode_key] = _read_allowlist(
                values.get("tool_allowlist_" + mode_key),
                values.get("tool_allowlist_" + mode_key + "_set"),
            )
        policies["generic"] = _read_allowlist(
            values.get("tool_allowlist"), values.get("tool_allowlist_set")
        )
        out[scope_key] = policies
    return out


def _read_allowlist(raw: Any, explicit: Any) -> tuple[str, list[str]]:
    """``(tool_allowlist*, *_set)`` → ``(授权模式, 工具名)``（nil / 空列表二态）。"""
    if raw is None and not explicit:
        return "inherit", []
    names = [str(item).strip() for item in raw] if isinstance(raw, list) else []
    names = [item for item in names if item]
    if not names:
        return "none", []
    return "whitelist", names


def save_scope_mode_policy(
    state_path: str, scope: str, mode_key: str, mode: str, names: list[str]
) -> None:
    """把某个群、某个会话模式（chat / work）的工具授权写进 ``state.toml``。

    写的是 ``tool_allowlist_<mode_key>`` / ``tool_allowlist_<mode_key>_set``；通用
    ``tool_allowlist`` 不动（它是群内 ``/tools allow`` 的写入目标，作为两模式的兜底）。
    ``inherit`` 删除该模式的两个键；``none`` 写显式空列表（禁止全部）；``whitelist``
    写名单并自动补 ``discover_tool``。
    """
    scope = normalize_scope(scope)
    mode_key = str(mode_key or "").strip().lower()
    if mode_key not in MODE_ALLOWLIST_KEYS:
        raise ConfigEditError(f"未知的会话模式 {mode_key!r}（只能是 chat / work）")
    migrate_legacy_scope_keys(state_path)
    section = f'group_policy."{scope}"'
    key = f"tool_allowlist_{mode_key}"
    set_key = key + "_set"
    if mode == "inherit":
        patch_section(state_path, section, {key: None, set_key: None})
        raw = read_toml(state_path).get("group_policy")
        entry = raw.get(scope) if isinstance(raw, dict) else None
        if not entry:
            remove_section(state_path, section)
        return
    if mode == "none":
        patch_section(state_path, section, {key: [], set_key: True})
        return
    cleaned: list[str] = []
    for name in names:
        text = str(name).strip()
        if text and text.lower() not in ("*", "all") and text not in cleaned:
            cleaned.append(text)
    if DISCOVER_TOOL not in cleaned:
        cleaned.insert(0, DISCOVER_TOOL)
    patch_section(state_path, section, {key: cleaned, set_key: True})


# ---------------------------------------------------------------------------
# 作用域归一 / 校验 / 发现 / 迁移
# ---------------------------------------------------------------------------
def canonical_scope(raw: str) -> str:
    """把作用域归一成半角冒号的 ``platform:scope_id`` 写法（不做合法性校验）。"""
    text = str(raw or "").translate(_SCOPE_SEPARATOR_TRANSLATION)
    # 作用域里不允许空白：从「群友」页复制时常带 "qqonebot · group:123" 里的空格。
    return "".join(text.split())


def scope_problem(scope: str) -> str:
    """校验归一后的作用域；合法返回空串，否则返回可直接展示的错误文本。"""
    text = str(scope or "").strip()
    if not text:
        return "群作用域不能为空（形如 qqonebot:group:123456）"
    if " " in text or "\t" in text:
        return "作用域里不能有空白：%r" % text
    parts = text.split(":")
    if len(parts) < 2:
        return "作用域应形如 qqonebot:group:123456（平台 + 会话作用域 id）"
    if any(not part for part in parts):
        return "作用域里有空段：%r" % text
    return ""


def normalize_scope(raw: str) -> str:
    """归一化并校验作用域；非法时抛 :class:`ConfigEditError`（消息直接展示）。"""
    scope = canonical_scope(raw)
    problem = scope_problem(scope)
    if problem:
        raise ConfigEditError(problem)
    return scope


def scope_is_canonical(scope: str) -> bool:
    """落盘键本身是否已经是规范写法（没有隔点、空白、全角冒号）。"""
    text = str(scope or "")
    return bool(text) and canonical_scope(text) == text


def discover_group_scopes(
    sessions_db_path: str = "", chat_history_db_path: str = ""
) -> list[str]:
    """从会话库 / 聊天历史库读出真实出现过的群作用域（``platform:group:id``）。

    只读 SQLite；库不存在或表缺失时安静跳过。优先 ``mode=ro``，WAL 库在只读连接下
    打不开时回退到普通连接（服务端本来就在用这两个库，额外读写句柄无害）。
    返回排序后的列表，给「工具权限」页的作用域输入框做候选——比让用户手打靠谱得多。
    """
    queries = (
        ("sessions", "platform", "platform_scope_id"),
        ("chat_messages", "platform", "platform_scope_id"),
        ("outbound_messages", "platform", "platform_scope_id"),
    )
    found: set[str] = set()
    for db_path in (sessions_db_path, chat_history_db_path):
        if not db_path or not os.path.exists(db_path):
            continue
        connection = _connect_sqlite(db_path)
        if connection is None:
            continue
        try:
            for table, platform_column, scope_column in queries:
                try:
                    rows = connection.execute(
                        f"SELECT DISTINCT {platform_column}, {scope_column} FROM {table}"
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for platform, scope_id in rows:
                    platform_text = str(platform or "").strip()
                    scope_text = str(scope_id or "").strip()
                    if not platform_text or not scope_text:
                        continue
                    # 只用群作用域（与 group_policy.is_group_scope 同口径）。
                    if not scope_text.startswith(("group:", "supergroup:")):
                        continue
                    found.add(canonical_scope(platform_text + ":" + scope_text))
        finally:
            connection.close()
    return sorted(found)


def migrate_legacy_scope_keys(state_path: str) -> dict[str, str]:
    """把文件里非规范写法的 ``group_policy`` 小节迁移到规范键，返回 ``{旧键: 新键}``。

    迁移是"先写新键、再删旧键"：新键已存在时以旧小节的字段补齐（新键优先），
    工具白名单两个键留给 :func:`save_scope_policy` 后续覆盖，避免把旧策略带过来。
    """
    if not state_path or not os.path.exists(state_path):
        return {}
    data = read_toml(state_path)
    section = data.get("group_policy")
    if not isinstance(section, dict):
        return {}
    migrated: dict[str, str] = {}
    for raw_key, entry in list(section.items()):
        old_key = str(raw_key).strip()
        if not old_key or scope_is_canonical(old_key):
            continue
        new_key = canonical_scope(old_key)
        if not new_key or new_key == old_key or scope_problem(new_key):
            continue
        fields = entry if isinstance(entry, dict) else {}
        carried = {
            key: value
            for key, value in fields.items()
            if key not in ("tool_allowlist", "tool_allowlist_set")
            and _toml_serializable(value)
        }
        if carried:
            patch_section(state_path, f'group_policy."{new_key}"', carried)
        remove_section(state_path, f'group_policy."{old_key}"')
        migrated[old_key] = new_key
    return migrated


def _toml_serializable(value: object) -> bool:
    """粗略判断值能不能经 :func:`atri_x.gui.configedit.patch_section` 写回。"""
    if isinstance(value, (str, bool, int, float)):
        return value == value  # 排除 NaN
    if isinstance(value, (list, tuple)):
        return all(isinstance(item, (str, bool, int, float)) for item in value)
    return False


def _connect_sqlite(path: str) -> sqlite3.Connection | None:
    """打开会话库读作用域：优先只读，WAL 库只读打不开时回退普通连接。"""
    for target, kwargs in (
        (f"file:{path}?mode=ro", {"uri": True}),
        (path, {}),
    ):
        try:
            return sqlite3.connect(target, timeout=2, **kwargs)
        except sqlite3.Error:
            continue
    return None
