"""桌面端「工具权限」页的工具目录：中文名、一句话功能解释、分组与风险等级。

**为什么单独一份静态目录**：工具自己的 ``Info.description`` 是写给模型看的（长、
带参数约定），直接塞进列表会让界面变成大段说明；而要在界面上渲染工具又得先装配
一整套依赖（记忆库 / 生图 / 技能管理器 / 聊天历史……），桌面端不该为了画一行字
把工具运行时搭起来。所以这一份是**面向用户**的静态目录：中文短名 + 一句人话解释
+ 分组 + 风险等级，顺序即"工具菜单"里的显示顺序。

维护约定：

- 键必须覆盖 :data:`atri_x.tool.builtin.register.REGISTERED_TOOLS`（回归用例会钉住
  两边集合相等），新增内置工具时在这里补一行；
- 工具实际的风险等级以工具自己的 ``Info.risk`` 为准，这里的 ``risk`` 只用于在界面上
  提示"这个工具会写东西"，不参与任何授权判断；
- 目录外的名字（技能工具 / 自定义工具）走 :func:`tool_meta` 的兜底，归到「其它工具」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "CATEGORY_ORDER",
    "OTHER_CATEGORY",
    "RISK_COLORS",
    "RISK_LABELS",
    "TOOL_CATALOG",
    "catalog_by_category",
    "catalog_names",
    "ordered_names",
    "tool_meta",
]

#: 风险等级 → 界面短标签。/ 工具自己的 ``Info.risk`` 取值见 :mod:`atri_x.tool.capability`。
RISK_LABELS = {
    "safe": "安全",
    "low": "低风险",
    "medium": "中风险",
    "high": "高风险",
}

#: 风险等级 → 标签颜色（越红越危险，只做视觉提示）。
RISK_COLORS = {
    "safe": "#5f6b7a",
    "low": "#2e7d32",
    "medium": "#a86500",
    "high": "#b3261e",
}

#: 目录外工具（技能工具 / 自定义工具名）的分组名。
OTHER_CATEGORY = "其它工具"


@dataclass(frozen=True)
class ToolMeta:
    """一个工具在「工具权限」页里的一行。"""

    #: 工具名（写进 ``state.toml`` 的 ``tool_allowlist``）。
    name: str
    #: 中文短名。
    title: str
    #: 一句话功能解释（用户视角，尽量不要出现 LLM 术语）。
    summary: str
    #: 分组名，取值见 :data:`CATEGORY_ORDER`。
    category: str
    #: 风险等级，仅用于界面提示。
    risk: str = "low"


#: 内置工具的展示目录；**顺序即菜单顺序**（按分组排列，组内也按这里的顺序）。
TOOL_CATALOG: tuple[ToolMeta, ...] = (
    # -- 工具发现 ----------------------------------------------------------
    ToolMeta(
        "discover_tool",
        "工具发现",
        "让模型知道有哪些工具可用；白名单模式保存时会自动带上它。",
        "工具发现",
        "safe",
    ),
    # -- 联网 --------------------------------------------------------------
    ToolMeta(
        "web_search",
        "网页搜索",
        "联网搜索关键词，返回简洁答案、来源链接和摘要。",
        "联网",
        "low",
    ),
    ToolMeta(
        "web_extract",
        "网页抓取",
        "打开指定网址并提取网页正文，长文可以分段继续读。",
        "联网",
        "low",
    ),
    # -- 文件与执行 --------------------------------------------------------
    ToolMeta(
        "workspace",
        "设置工作区",
        "设置本会话的共享工作目录，其它工具的相对路径都以它为基准。",
        "文件与执行",
        "low",
    ),
    ToolMeta(
        "read_file",
        "读取文件",
        "按行读取文本文件，也能在文件或目录里搜索文本和函数名。",
        "文件与执行",
        "low",
    ),
    ToolMeta(
        "edit_file",
        "编辑文件",
        "批量修改文本文件，一次提交多处改动并返回 diff；失败不写入。",
        "文件与执行",
        "high",
    ),
    ToolMeta(
        "rollback_file",
        "撤销编辑",
        "把某个文件恢复到上一次编辑前的版本（只回滚本工具做过的修改）。",
        "文件与执行",
        "high",
    ),
    ToolMeta(
        "shell",
        "执行命令",
        "在沙盒里执行 shell 命令（bash 语法），风险高，按需放行。",
        "文件与执行",
        "high",
    ),
    ToolMeta(
        "send_file",
        "发送文件",
        "把本机文件作为附件发送到当前会话。",
        "文件与执行",
        "medium",
    ),
    # -- 记忆 --------------------------------------------------------------
    ToolMeta(
        "resident_memory",
        "常驻记忆（入口）",
        "管理当前用户的常驻记忆，包含核心记忆和普通记忆两组工具。",
        "记忆",
        "low",
    ),
    ToolMeta(
        "resident_memory_read",
        "读取常驻记忆",
        "读取当前用户在该平台的常驻记忆内容。",
        "记忆",
        "low",
    ),
    ToolMeta(
        "resident_memory_normal",
        "维护普通记忆",
        "追加、覆盖或删除普通常驻记忆条目（低风险）。",
        "记忆",
        "low",
    ),
    ToolMeta(
        "resident_memory_core",
        "维护核心记忆",
        "整体覆盖核心常驻记忆，写入前会先要求确认。",
        "记忆",
        "high",
    ),
    ToolMeta(
        "angel_remember",
        "记录长期记忆",
        "把用户明确说出的重要偏好、事实或约定存成长期记忆。",
        "记忆",
        "low",
    ),
    ToolMeta(
        "angel_recall",
        "检索长期记忆",
        "按关键词找出以前记下的长期记忆。",
        "记忆",
        "low",
    ),
    ToolMeta(
        "angel_forget",
        "删除长期记忆",
        "删除一条长期记忆，需要二次确认才会真正删除。",
        "记忆",
        "high",
    ),
    ToolMeta(
        "long_memory",
        "长期记忆（入口）",
        "全局长期记忆的入口，用来检索、保存、更新或删除。",
        "记忆",
        "medium",
    ),
    ToolMeta(
        "long_memory_search",
        "检索长期记忆库",
        "按关键词或分类检索全局长期记忆。",
        "记忆",
        "medium",
    ),
    ToolMeta(
        "long_memory_write",
        "写入长期记忆库",
        "保存、局部更新或永久删除全局长期记忆。",
        "记忆",
        "high",
    ),
    # -- 群聊与角色 --------------------------------------------------------
    ToolMeta(
        "search_chat_history",
        "搜索聊天记录",
        "按关键词、用户或时间检索当前群聊 / 私聊的历史消息。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "get_chat_history_around",
        "查看消息上下文",
        "按消息 ID 查看某条聊天消息前后的上下文。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "reply_to_chat_history_message",
        "引用回复消息",
        "引用聊天里某条消息，并把回复发到当前群聊或私聊。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "get_media",
        "获取聊天媒体",
        "按消息 ID 取回聊天里的图片、语音或文件等媒体。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "fetch_message",
        "按 ID 取回消息",
        "引用或转发当时没取到完整内容时，按消息 ID 再取一次（内容通常已在缓存里）。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "view_image",
        "查看图片",
        "把图片内容直接交给模型看，而不只是给一个地址。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "group_analysis",
        "群聊统计",
        "统计指定天数内的消息量、活跃成员和活跃时段。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_list",
        "列出角色",
        "列出角色素材库里公开的角色，以及自己的私有角色。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_read",
        "读取角色设定",
        "读取某个角色的设定文本和图片。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_search",
        "搜索角色",
        "按名称、别名、标签或设定正文检索角色。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_manage",
        "编辑角色",
        "创建或修改自己的角色，维护设定文本和图片。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_delete",
        "删除角色",
        "永久删除自己的角色（公开角色只有超级管理员能删）。",
        "群聊与角色",
        "high",
    ),
    ToolMeta(
        "character_import",
        "导入角色卡",
        "把一段粘贴的人设文本一步保存成角色卡，可同时启用为专属人设或全局人设。",
        "群聊与角色",
        "low",
    ),
    ToolMeta(
        "character_activate",
        "设为全局人设",
        "把某张角色卡写成全局系统人设 SOUL.md（仅超级管理员，自动备份可回滚）。",
        "群聊与角色",
        "high",
    ),
    ToolMeta(
        "persona_control",
        "开关专属人设",
        "成员自己绑定 / 启用 / 关闭 / 清除专属人设，关闭后恢复默认 SOUL.md。",
        "群聊与角色",
        "low",
    ),
    # -- 图片与画师 --------------------------------------------------------
    ToolMeta(
        "image_generate",
        "生成图片",
        "用提示词生成图片，可自动带上角色设定、提示词库和画师串。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "image_to_prompt",
        "图片反推提示词",
        "从参考图片反推出可直接使用的绘图提示词。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "prompt_library_search",
        "提示词库",
        "检索内置的 GPT Image 提示词库，查镜头、光线和画风。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "artist_extract",
        "提取画师串",
        "从提示词文本或图片的生成参数里提取画师串。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "artist_library_search",
        "搜索画师串",
        "搜索画师标签与画师串组合，拿到 id 后可读取或引用。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "artist_library_read",
        "读取画师串",
        "读取一组画师串的完整内容和版本号。",
        "图片与画师",
        "low",
    ),
    ToolMeta(
        "artist_library_save",
        "保存画师串",
        "新建或更新画师串组合，写入个人库或群库。",
        "图片与画师",
        "high",
    ),
    ToolMeta(
        "artist_library_delete",
        "删除画师串",
        "删除一个画师串组合，需要二次确认。",
        "图片与画师",
        "high",
    ),
    # -- 技能与维护 --------------------------------------------------------
    ToolMeta(
        "agent_skill",
        "Agent 技能清单",
        "读写 AgentSkill 的清单文件（ATRIX_SKILL.toml）。",
        "技能与维护",
        "high",
    ),
    ToolMeta(
        "go_skill_run",
        "运行 Go 技能",
        "运行指定的 Go skill，并把参数以 JSON 传进去。",
        "技能与维护",
        "low",
    ),
    ToolMeta(
        "create_atrix_skill",
        "新建技能",
        "创建一个可复用的 ATRI-X 原生 Atriyph skill。",
        "技能与维护",
        "high",
    ),
    ToolMeta(
        "read_atrix_skill",
        "读取技能文件",
        "按行读取 skill 定义或源码，供修改前定位。",
        "技能与维护",
        "medium",
    ),
    ToolMeta(
        "modify_atrix_skill",
        "修改技能",
        "修改 skill 定义或源码，改完再用「完成技能」校验并加载。",
        "技能与维护",
        "high",
    ),
    ToolMeta(
        "finalize_atrix_skill",
        "完成技能",
        "校验并编译技能（gofmt / go build），成功后热加载。",
        "技能与维护",
        "medium",
    ),
    ToolMeta(
        "atriwisp_creator",
        "Atriwisp 说明",
        "获取创建 Atriwisp 监听器需要的说明和当前配置摘要。",
        "技能与维护",
        "low",
    ),
    ToolMeta(
        "self_learning_review",
        "自学习复核",
        "审核 clean-room 表达 / 黑话候选，批准后才会进入上下文。",
        "技能与维护",
        "high",
    ),
    ToolMeta(
        "cron",
        "定时任务（入口）",
        "管理系统定时任务的入口，查询和编辑走子工具。",
        "技能与维护",
        "medium",
    ),
    ToolMeta(
        "cron_query",
        "查询定时任务",
        "列出全部定时任务，或查询某一个任务。",
        "技能与维护",
        "medium",
    ),
    ToolMeta(
        "cron_write",
        "编辑定时任务",
        "创建、更新、停用或删除定时任务。",
        "技能与维护",
        "high",
    ),
    # -- 时间（Python 版新增，1.0.10） --------------------------------------
    ToolMeta(
        "get_current_time",
        "查询当前时间",
        "查询当前时区、本地时间、UTC、星期与 unix 秒，并说明这个时间的来源和网络校验状态；"
        "只读、不联网。",
        "时间与运行",
        "safe",
    ),
)

#: 分组顺序（取目录里第一次出现的顺序；「其它工具」永远最后）。
CATEGORY_ORDER: tuple[str, ...] = tuple(
    dict.fromkeys(item.category for item in TOOL_CATALOG)
) + (OTHER_CATEGORY,)

_BY_NAME = {item.name: item for item in TOOL_CATALOG}


def tool_meta(name: str) -> ToolMeta:
    """取工具的展示元信息；目录外的名字返回「其它工具」兜底行。"""
    key = (name or "").strip()
    found = _BY_NAME.get(key)
    if found is not None:
        return found
    return ToolMeta(
        name=key,
        title=key or "(未命名工具)",
        summary="目录里没有这个工具的说明：多半来自技能或自定义配置（state.toml）。",
        category=OTHER_CATEGORY,
        risk="low",
    )


def catalog_names() -> list[str]:
    """目录里的全部工具名（按菜单顺序）。"""
    return [item.name for item in TOOL_CATALOG]


def ordered_names(names: Iterable[str] | None = None) -> list[str]:
    """把任意工具名集合排成菜单顺序：目录顺序在前，「其它工具」按名字排在后面。"""
    wanted = {str(name).strip() for name in (names or ()) if str(name).strip()}
    if not wanted:
        return []
    ordered = [name for name in catalog_names() if name in wanted]
    extras = sorted(wanted.difference(ordered))
    return ordered + extras


def catalog_by_category(
    names: Sequence[str] | None = None,
) -> list[tuple[str, list[ToolMeta]]]:
    """按分组返回 ``[(分组名, [ToolMeta...]), ...]``，空分组不返回。

    ``names`` 为 ``None`` 时用目录全量；传入集合时只保留其中的工具（顺序仍按目录）。
    """
    selected = ordered_names(names) if names is not None else catalog_names()
    groups: dict[str, list[ToolMeta]] = {}
    for name in selected:
        meta = tool_meta(name)
        groups.setdefault(meta.category, []).append(meta)
    ordered_groups: list[tuple[str, list[ToolMeta]]] = []
    for category in CATEGORY_ORDER:
        metas = groups.pop(category, [])
        if metas:
            ordered_groups.append((category, metas))
    # 目录里没登记的分组（理论上不会出现）按名字兜底，别把工具吞掉。
    for category in sorted(groups):
        if groups[category]:
            ordered_groups.append((category, groups[category]))
    return ordered_groups
