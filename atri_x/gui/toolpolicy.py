"""「工具权限」页的策略读写层：群工具白名单（``state.toml`` 的 ``[group_policy]``）。

把"读 / 写 / 预设"和 Tk 界面拆开，页面只负责画菜单和调这几个函数：

- :func:`load_tool_choices` —— 内置工具名清单（静态常量，不需要后台服务在跑）；
- :func:`load_scope_policies` —— 每个群的工具授权 ``{scope: (mode, names)}``；
- :func:`save_scope_policy` —— 写某个群的授权（``inherit`` 删键、``none`` 写空数组、
  ``whitelist`` 写名单并自动补 :data:`DISCOVER_TOOL`）。

授权语义（0.9.12 真机事故的教训）：``tool_allowlist`` 是 **nil / 空列表二态**——
没配过（``None``）表示"继承全局"，显式空列表表示"禁止全部"；读的时候必须保留这个
区分，写的时候 ``inherit`` 走 ``None`` 而不是 ``[]``。

同一份策略也能在群里改（``/tools allow <工具,工具|all|none>``），本模块不管命令侧，
只保证桌面端写进去的格式与命令侧一致。
"""

from __future__ import annotations

import os

from atri_x.gui.configedit import (
    ConfigEditError,
    patch_section,
    read_toml,
    remove_section,
)

__all__ = [
    "DISCOVER_TOOL",
    "FALLBACK_TOOLS",
    "PRESET_COMMON",
    "PRESET_READONLY",
    "load_scope_policies",
    "load_tool_choices",
    "save_scope_policy",
]

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
    """
    scope = (scope or "").strip()
    if not scope:
        raise ConfigEditError("群作用域不能为空（形如 qqonebot:group:123456）")
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
