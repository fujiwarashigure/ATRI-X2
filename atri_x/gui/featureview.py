"""「功能」页的数据来源：把配置文件里能看出来的功能开关汇总成一张表。

只做**只读汇总**（改配置去对应的选项卡），所有取值都来自本地文件：

- ``app.toml``：平台、角色库、常驻记忆、自学习、群功能、维护任务、DSH 路由；
- ``services.toml``：生图 / 图片转提示词 / 视觉回退 / 语音转写；
- ``atrinis.toml``：外部事件入口；
- ``plugins/hooks.toml``、``skills/``、``characters/``、``SOUL.md``：目录里的现状。

状态字符串是给界面直接显示的，所以固定成三种：``已启用`` / ``已停用`` / ``默认启用``
（未显式配置但上游默认开启的，会单独标注"默认"）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.gui.configedit import ConfigEditError, ConfigPaths, read_toml
from atri_x.gui.configmodel import load_providers, provider_key_state
from atri_x.timezones import DEFAULT_TIMEZONE

__all__ = ["FeatureRow", "collect_features"]

#: 平台 id → 界面上的名字。
_PLATFORM_LABELS = {
    "qqonebot": "QQ（OneBot v11）",
    "qqofficial": "QQ 官方机器人",
    "telegram": "Telegram",
    "cli": "本地 CLI / 桌面端",
}

#: 未显式配置时默认开启的功能段（对应上游 ``is_enabled`` 的语义）。
_DEFAULT_ON_SECTIONS = ("character_library", "resident_memory", "angel_memory", "self_learning")


@dataclass(frozen=True)
class FeatureRow:
    """功能表的一行。"""

    key: str
    name: str
    status: str
    detail: str
    tab: str = ""


def collect_features(paths: ConfigPaths) -> list[FeatureRow]:
    """汇总当前配置下的功能状态（读取失败降级成"配置读不到"）。"""
    app = _try_read(paths.config_path)
    services = _try_read(paths.services_path)
    atrinis = _try_read(paths.atrinis_path)
    rows = [_llm_row(paths)]
    rows.append(_section_row(
        "image_generation", "生图（image_generate）", services, default_on=False,
        tab="advanced", extra=_image_detail(services),
        fix="services.toml 的 [image_generation]",
    ))
    rows.append(_image_to_prompt_row(services))
    rows.append(_metadata_store_row(services))
    rows.append(_section_row(
        "vision", "视觉回退（图片描述）", services, default_on=False, tab="advanced",
        extra=_kv_detail(services.get("vision"), ("provider", "model")),
    ))
    rows.append(_section_row(
        "safety", "内容安全（道德限制 + 敏感信息过滤）", app, default_on=False,
        tab="toolperm_params", extra=_safety_detail(app.get("safety")),
    ))
    rows.append(_section_row(
        "asr", "语音转写（ASR）", services, default_on=False, tab="advanced",
        extra=_kv_detail(services.get("asr"), ("provider", "model", "language")),
    ))
    rows.append(_character_row(paths, app))
    rows.append(_soul_row(paths))
    rows.append(_members_row(paths))
    rows.append(_knowledge_row(paths, app))
    rows.append(_time_row(app))
    rows.append(_logs_row(paths))
    rows.append(_section_row(
        "resident_memory", "常驻记忆（memories.toml）", app, default_on=True,
        extra=_kv_detail(app.get("resident_memory"), ("core_max_units", "normal_max_units")),
    ))
    rows.append(_section_row(
        "angel_memory", "长期记忆（angel_memory）", app, default_on=True, tab="advanced",
        extra=_kv_detail(app.get("angel_memory"), ("retention_days", "forget_on_recall")),
    ))
    rows.append(_section_row(
        "self_learning", "自学习（表达/黑话）", app, default_on=True, tab="advanced",
        extra=_kv_detail(app.get("self_learning"), ("min_count", "min_users")),
    ))
    rows.append(_section_row(
        "group_analysis", "群分析", app, default_on=True, tab="advanced",
        extra=_kv_detail(app.get("group_analysis"), ("max_messages", "report_enabled")),
    ))
    rows.append(_section_row(
        "group_services", "群提醒/投票/报名", app, default_on=True, tab="advanced",
        extra=_kv_detail(app.get("group_services"), ("max_reminders_per_scope", "max_polls_per_scope")),
    ))
    rows.append(_hooks_row(paths))
    rows.append(_skills_row(paths))
    rows.append(_section_row(
        "tool_preview", "工具进度提示（程序兜底文案）", app, default_on=True,
        tab="toolperm_preview",
        extra=_kv_detail(app.get("tool_preview"), ("style", "enabled")),
    ))
    rows.append(_section_row(
        "reactions", "本地表情图库（情绪标签 + 挑图）", app, default_on=False,
        tab="character_reactions",
        extra=_kv_detail(app.get("reactions"),
                         ("show_in_group", "cooldown_text_replies", "probability")),
    ))
    rows.append(_atrinis_row(atrinis))
    rows.append(_dsh_row(app))
    rows.append(_maintenance_row(app))
    rows.append(_platform_row(app))
    return rows


# ---------------------------------------------------------------------------
# 单行构造
# ---------------------------------------------------------------------------
def _llm_row(paths: ConfigPaths) -> FeatureRow:
    providers = load_providers(paths)
    ready = 0
    for view in providers.values():
        try:
            if provider_key_state(paths, view).configured:
                ready += 1
        except ConfigEditError:
            pass
    if not providers:
        return FeatureRow(
            "llm", "大模型对话", "未配置", "还没有任何 [providers.*]；请到「模型」页添加",
            tab="model",
        )
    status = "已配置" if ready else "缺密钥"
    detail = f"{len(providers)} 个 provider：" + "、".join(sorted(providers)) + f"；{ready} 个已设置密钥"
    return FeatureRow("llm", "大模型对话", status, detail, tab="model")


def _character_row(paths: ConfigPaths, app: Mapping[str, Any]) -> FeatureRow:
    library = _section(app, "character_library")
    enabled = library.get("enabled")
    enabled = True if enabled is None else bool(enabled)
    count = _count_characters(paths.character_root)
    status = _status_of(enabled, explicit=enabled is not None)
    detail = f"目录 {paths.character_root}，共 {count} 个角色"
    return FeatureRow("character_library", "角色库（人设卡）", status, detail, tab="character")


def _soul_row(paths: ConfigPaths) -> FeatureRow:
    size = _file_size(paths.soul_path)
    if size:
        return FeatureRow("soul", "系统人设（SOUL.md）", "已配置", f"{size} 字符：{paths.soul_path}", tab="character")
    return FeatureRow("soul", "系统人设（SOUL.md）", "缺失", f"没有 {paths.soul_path}", tab="character")


def _members_row(paths: ConfigPaths) -> FeatureRow:
    """群友档案（GUI 私有 ``gui/members.json``）。"""
    count = _count_members(paths.config_dir)
    if count:
        detail = f"{count} 位群友有档案：{paths.gui_dir}"
        return FeatureRow("members", "群友角色数据", "已建档", detail, tab="members")
    return FeatureRow(
        "members",
        "群友角色数据",
        "未建档",
        "「群友」页可从聊天历史里选人建档（备注名/标签/角色数据/头像）",
        tab="members",
    )


def _knowledge_row(paths: ConfigPaths, app: Mapping[str, Any]) -> FeatureRow:
    """群知识库：读全局开关 + ``state.toml`` 里的条目数。"""
    section = _section(app, "group_knowledge")
    enabled = section.get("enabled")
    enabled = True if enabled is None else bool(enabled)
    count = _count_knowledge(paths.state_path)
    status = _status_of(enabled, explicit=enabled is not None)
    detail = f"{count} 条知识；每群上限 {section.get('max_entries_per_scope') or 200}"
    return FeatureRow("knowledge", "群知识库（FAQ）", status, detail, tab="knowledge")


def _time_row(app: Mapping[str, Any]) -> FeatureRow:
    """统一时间服务：业务时间口径恒定生效，网络校验是可选的一段（默认关闭）。

    状态口径：网络校验真的能跑（开关打开 + 至少一条启用来源）才是「已启用」；
    否则时间服务本身仍按默认值工作（系统时钟 + ``[time].timezone``），记「默认启用」。
    """
    section = _section(app, "time")
    timezone = str(section.get("timezone") or "").strip() or DEFAULT_TIMEZONE
    verification = _section(section, "verification")
    sources = [
        item
        for item in section.get("sources") or []
        if isinstance(item, Mapping) and bool(item.get("enabled"))
    ]
    verify_on = bool(verification.get("enabled"))
    parts = [f"时区 {timezone}"]
    if section.get("inject_context") is False:
        parts.append("不注入系统提示")
    if verify_on and sources:
        parts.append(f"网络校验已启用（{len(sources)} 个来源）")
    elif verify_on:
        parts.append("网络校验已启用但没有启用的来源 → 运行期不可用")
    else:
        parts.append("网络校验未启用（只读系统时钟，不联网）")
    return FeatureRow(
        "time",
        "时间服务（时区 / 网络校验）",
        "已启用" if verify_on and sources else "默认启用",
        "；".join(parts),
        tab="time",
    )


def _logs_row(paths: ConfigPaths) -> FeatureRow:
    """后台服务日志目录（跑过服务后才有文件）。"""
    directory = paths.log_dir
    files = _count_log_files(directory)
    if files:
        return FeatureRow("logs", "日志查看", "已启用", f"{files} 个日志文件：{directory}", tab="logs")
    return FeatureRow(
        "logs",
        "日志查看",
        "暂无日志",
        f"日志目录 {directory or '（未配置）'}：跑过后台服务后会生成",
        tab="logs",
    )


def _hooks_row(paths: ConfigPaths) -> FeatureRow:
    rules, plugins = _count_hooks(paths.hooks_path)
    if not os.path.exists(paths.hooks_path):
        return FeatureRow(
            "hooks", "Hook 插件", "未配置",
            f"没有 {paths.hooks_path}；在「进阶 → HOOK 插件配置…」里新建",
        )
    detail = f"{rules} 条规则，{plugins} 个插件：{paths.hooks_path}（进阶页可编辑）"
    return FeatureRow("hooks", "Hook 插件", "已启用" if rules or plugins else "空配置", detail)


def _skills_row(paths: ConfigPaths) -> FeatureRow:
    names = _count_skills(paths.skills_dir)
    if not names:
        return FeatureRow("skills", "Skill 技能", "空目录", f"{paths.skills_dir} 里没有可用技能")
    sample = "、".join(names[:4]) + ("…" if len(names) > 4 else "")
    return FeatureRow("skills", "Skill 技能", "已安装", f"{len(names)} 个：{sample}")


def _atrinis_row(atrinis: Mapping[str, Any]) -> FeatureRow:
    enabled = bool(atrinis.get("enabled"))
    http = _section(atrinis, "http")
    detail = _kv_detail(http, ("addr", "workers"))
    return FeatureRow(
        "atrinis", "外部事件入口（Atrinis）", _status_of(enabled, explicit=True), detail,
        tab="advanced",
    )


def _dsh_row(app: Mapping[str, Any]) -> FeatureRow:
    agent = _section(app, "agent")
    dsh = _section(agent, "dsh")
    enabled = bool(dsh.get("enabled"))
    detail = _kv_detail(dsh, ("min_score", "model", "command"))
    if not enabled:
        detail = "未启用（[agent.dsh].enabled 未打开）"
    return FeatureRow("dsh", "内置 DSH 深度路由", _status_of(enabled, explicit=False), detail)


def _maintenance_row(app: Mapping[str, Any]) -> FeatureRow:
    maintenance = _section(app, "maintenance")
    enabled: list[str] = []
    for name, entry in maintenance.items():
        if isinstance(entry, Mapping) and bool(entry.get("enabled")):
            enabled.append(str(name))
    if not enabled:
        return FeatureRow(
            "maintenance", "定时维护任务", "未启用",
            "没有开启的维护任务（进阶 → 定时维护任务…）", tab="advanced",
        )
    detail = f"{len(enabled)} 个已开启：" + "、".join(enabled[:6]) + "（进阶 → 定时维护任务…）"
    return FeatureRow("maintenance", "定时维护任务", "已启用", detail, tab="advanced")


def _platform_row(app: Mapping[str, Any]) -> FeatureRow:
    platform = _section(app, "platform")
    active: list[str] = []
    for key, label in _PLATFORM_LABELS.items():
        entry = _section(platform, key)
        default_on = key == "cli"
        enabled = entry.get("enabled")
        enabled = default_on if enabled is None else bool(enabled)
        if enabled:
            active.append(label)
    if not active:
        return FeatureRow("platforms", "平台接入", "未启用", "没有启用任何平台", tab="platform")
    return FeatureRow(
        "platforms", "平台接入", "已启用", "、".join(active), tab="platform"
    )


def _section_row(
    key: str,
    name: str,
    data: Mapping[str, Any],
    *,
    default_on: bool,
    extra: str = "",
    fix: str = "",
    tab: str = "",
) -> FeatureRow:
    section = _section(data, key)
    enabled = section.get("enabled")
    enabled = default_on if enabled is None else bool(enabled)
    status = _status_of(enabled, explicit=enabled is not None)
    detail = extra or fix
    return FeatureRow(key, name, status, detail, tab=tab)


# ---------------------------------------------------------------------------
# 图片/视觉两行：它们真正的编辑入口在「工具权限 → 工具参数（全局）」
# ---------------------------------------------------------------------------
#: ``tab`` 前缀：``toolperm_params:<工具名>`` 由
#: :meth:`atri_x.gui.app.GuiApp.show_tab_key` 解析成"切到工具权限页 + 展开该工具的面板"。
#: 1.1.0 实机事故：``image_to_prompt`` 行原来指向 ``advanced``，可「进阶」页里根本没有
#: 它的字段（字段在工具参数注册表里），双击过去只能看到一堆不相关的开关。
_TOOL_PARAMS_PREFIX = "toolperm_params:"

#: 这两行共用的编辑位置（写进 detail，用户照着找就能找到）。
_IMAGE_TOOLS_LOCATION = "「工具权限 → 工具参数（全局）」"


def _image_to_prompt_row(services: Mapping[str, Any]) -> FeatureRow:
    """`image_to_prompt` 行：按 ``is_enabled`` 语义判状态，并指向真正的编辑面板。

    ``enabled`` 是三态：显式 false = 强制停用；未显式配置时**填好 provider + model
    就算启用**（与 :meth:`atri_x.config.services.ImageToPromptConfig.is_enabled`
    一致）。没配 provider/model 时工具根本不会注册，所以状态写「未配置」而不是
    「默认停用」——后者会让人以为"默认就该是关的，没救了"。
    """
    section = _section(services, "image_to_prompt")
    provider = str(section.get("provider") or "").strip()
    model = str(section.get("model") or "").strip()
    explicit = section.get("enabled")
    configured = provider != "" and model != ""
    parts: list[str] = []
    if configured:
        parts.append(f"provider={provider}")
        parts.append(f"model={model}")
    else:
        parts.append("没填 provider + model 时工具不会注册（群里看不到 image_to_prompt）")
    if _metadata_store_path(services):
        parts.append("入库参数留档已开启：转码前的原图参数仍可回读")
    else:
        parts.append(
            "入库参数留档未开启：超过入库上限的图会被转码成 JPEG，原图参数会丢（同页可开）"
        )
    location = (
        f"在{_IMAGE_TOOLS_LOCATION}的「图片反推提示词（image_to_prompt）」面板里配置"
    )
    tab = _TOOL_PARAMS_PREFIX + "image_to_prompt"
    if explicit is False:
        return FeatureRow(
            "image_to_prompt",
            "图片转提示词（image_to_prompt）",
            "已停用",
            "；".join(["显式 enabled=false 强制停用", *parts, location]),
            tab=tab,
        )
    if configured:
        return FeatureRow(
            "image_to_prompt",
            "图片转提示词（image_to_prompt）",
            "已启用",
            "；".join([*parts, location]),
            tab=tab,
        )
    return FeatureRow(
        "image_to_prompt",
        "图片转提示词（image_to_prompt）",
        "未配置",
        "；".join([*parts, location]),
        tab=tab,
    )


def _metadata_store_row(services: Mapping[str, Any]) -> FeatureRow:
    """``[image_generation].metadata_store`` 行：它决定"转码前的参数还救不救得回来"。

    单独列出来而不是塞进生图行，是因为它影响的不是"能不能生图"，而是
    ``artist_extract`` / ``image_to_prompt`` 能不能读到原图里记录的画师串与提示词。
    """
    path = _metadata_store_path(services)
    tab = _TOOL_PARAMS_PREFIX + "artist_extract"
    if path:
        return FeatureRow(
            "image_metadata_store",
            "图片生成参数留档（metadata_store）",
            "已启用",
            "；".join(
                [
                    f"留档文件：{path}",
                    "入库转码前先存一份原图参数，之后 artist_extract / image_to_prompt "
                    "仍能取回画师串与提示词",
                    f"编辑位置：{_IMAGE_TOOLS_LOCATION}的「画师串与生成参数留档（artist_*）」面板",
                ]
            ),
            tab=tab,
        )
    return FeatureRow(
        "image_metadata_store",
        "图片生成参数留档（metadata_store）",
        "未启用",
        "；".join(
            [
                "不开时：超过入库上限（默认 4 MiB / 长边 4096）的图会被转码成 JPEG，"
                "PNG 里的画师串与提示词当场丢失",
                f"在{_IMAGE_TOOLS_LOCATION}的「画师串与生成参数留档（artist_*）」面板里"
                "填一个 JSON 文件名（例如 image_metadata.json）即可开启",
            ]
        ),
        tab=tab,
    )


def _metadata_store_path(services: Mapping[str, Any]) -> str:
    """``[image_generation].metadata_store`` 的非空值（未配置 / 留空都算关闭）。"""
    value = _section(services, "image_generation").get("metadata_store")
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------
def _try_read(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        return read_toml(path)
    except ConfigEditError:
        return {}


def _section(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _status_of(enabled: bool, *, explicit: bool) -> str:
    if enabled:
        return "已启用" if explicit else "默认启用"
    return "已停用" if explicit else "默认停用"


def _kv_detail(section: Any, keys: tuple[str, ...], *, sep: str = "；") -> str:
    if not isinstance(section, Mapping):
        return ""
    parts: list[str] = []
    for key in keys:
        value = section.get(key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        parts.append(f"{key}={value}")
    return sep.join(parts)


#: 内容安全的编辑入口提示：这一项**不在「进阶」页**，而在「工具权限」页的参数子选项卡里。
_SAFETY_LOCATION = "在「工具权限 → 工具参数（全局）」的「全局参数」面板里改"

#: 内容安全「按会话模式覆盖」的两个模式键：``app.toml`` 的 ``[safety.chat]`` / ``[safety.work]``。
_SAFETY_MODES = ("chat", "work")


def _safety_override_summary(section: Any, mode: str) -> str:
    """一个模式的覆盖摘要：``chat: mode=enforce`` / ``chat: 继承``。

    覆盖段（``[safety.<mode>]``）没配、或者里面没有 enabled / mode 时算「继承」——
    界面上写的就是"继承全局"；其它覆盖键（block_reply、max_scan_chars、出站开关）
    只影响细节，不进这一行摘要。
    """
    entry = _section(section, mode) if isinstance(section, Mapping) else {}
    detail = _kv_detail(entry, ("enabled", "mode"), sep="、")
    return f"{mode}: {detail}" if detail else f"{mode}: 继承"


def _safety_detail(section: Any) -> str:
    """内容安全行的说明：当前开关 / 模式 + chat / work 覆盖摘要 + 编辑入口。

    入口提示不能省，否则用户会去「进阶」页找；覆盖摘要给的是"两个模式各自有没有单独
    配过"（形如 ``chat: mode=enforce；work: 继承``）。
    """
    parts = [_kv_detail(section, ("enabled", "mode"))]
    parts.extend(_safety_override_summary(section, mode) for mode in _SAFETY_MODES)
    parts.append(_SAFETY_LOCATION)
    return "；".join(part for part in parts if part)


def _image_detail(services: Mapping[str, Any]) -> str:
    section = _section(services, "image_generation")
    parts = [_kv_detail(section, ("base_url", "model", "size", "quality"))]
    return "；".join(part for part in parts if part)


def _count_characters(root: str) -> int:
    if not root or not os.path.isdir(root):
        return 0
    count = 0
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if os.path.exists(os.path.join(entry.path, "character.toml")):
            count += 1
        elif os.path.exists(os.path.join(entry.path, "profile.md")):
            count += 1
    return count


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _count_hooks(path: str) -> tuple[int, int]:
    data = _try_read(path)
    rules = data.get("rules")
    rule_count = len(rules) if isinstance(rules, list) else 0
    plugins = data.get("plugins")
    plugin_count = len(plugins) if isinstance(plugins, list) else 0
    return rule_count, plugin_count


def _count_members(config_dir: str) -> int:
    """群友档案条数（``gui/members.json``；读不到就是 0 位）。"""
    if not config_dir:
        return 0
    try:
        from atri_x.gui.memberdata import load_profiles

        return len(load_profiles(config_dir))
    except Exception:  # noqa: BLE001 - 只读汇总，读不到不该让整页失败
        return 0


def _count_knowledge(state_path: str) -> int:
    """``state.toml`` 里 ``[group_knowledge]`` 的条目总数（读不到就是 0）。"""
    try:
        from atri_x.gui.tabs.knowledge import load_entries

        return len(load_entries(state_path))
    except Exception:  # noqa: BLE001 - 只读汇总，读不到不该让整页失败
        return 0


def _count_log_files(directory: str) -> int:
    """日志目录里 ``*.log`` 的个数。"""
    if not directory or not os.path.isdir(directory):
        return 0
    count = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file() and entry.name.endswith(".log"):
                    count += 1
    except OSError:
        return 0
    return count


def _count_skills(root: str) -> list[str]:
    names: list[str] = []
    if not root or not os.path.isdir(root):
        return names
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(item for item in dirs if not item.startswith("."))
        depth = os.path.relpath(current, root).count(os.sep) + 1
        if depth > 3:
            dirs[:] = []
            continue
        if "SKILL.md" in files or "ATRIX_SKILL.toml" in files:
            names.append(os.path.basename(current))
            dirs[:] = []
    return names
