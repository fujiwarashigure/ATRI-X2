"""``atriwisp_creator``：给出一份创建 Atriwisp 监听器的精简任务卡。

对应 Go：``internal/tool/builtin/atriwisp_creator.go``。

工具本身**不写任何文件**：它只读当前 ATRI-X / Atrinis 配置摘要，拼出
Atriyph 写法的任务卡（``#task create_atriwisp``），真正的读/写/测试交给
``read_file`` / ``edit_file`` / ``shell``（也是它 ``depends_on`` 的三件）。

与 Go 的差异：

* ``os.UserConfigDir`` → :func:`user_config_dir`（Windows 用 ``%APPDATA%``，
  其余平台按 ``XDG_CONFIG_HOME`` / ``~/.config``）。
* ``config.Load`` → :func:`atri_x.config.load`（``strict=False``：读不到文件时仍要
  能给出带 warning 的任务卡，而不是让工具直接失败）。
* ``runtimeinfo.AtriyphRuleCard`` → :func:`atri_x.tool.runtimeinfo.atriyph_rule_card`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from atri_x.config import APP_DIR_NAME, ENV_CONFIG_FILE, XDG_APP_DIR_NAME, load
from atri_x.llm.llm import ToolSchema
from atri_x.tool.builder import Builder
from atri_x.tool.runtimeinfo import atriyph_rule_card
from atri_x.tool.tool import (
    CallRequest,
    Info,
    Result,
    RiskLow,
    SourceBuiltin,
)

from .runtimeinfo import RuntimeInfo, first as first_runtime_info

#: 工具名。对应 Go ``AtriwispCreatorName``。
ATRIWISP_CREATOR_NAME = "atriwisp_creator"

_NOT_FOUND_WARNING = (
    "未找到 ATRI-X app.toml，无法确认 Atrinis endpoint 和 token 配置。"
    "请提示用户先配置 Atrinis，或让用户要求 Agent 修改 app.toml / atrinis.toml / .env。"
)


@dataclass
class AtriwispCreatorConfigSnapshot:
    """配置摘要。对应 Go ``atriwispCreatorConfigSnapshot``。"""

    app_config_path: str = ""
    atrinis_config_path: str = ""
    enabled: bool = False
    addr: str = ""
    token_names: list[str] = field(default_factory=list)
    token_env_names: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def new_atriwisp_creator_tool(*infos: RuntimeInfo) -> "AtriwispCreatorTool":
    """对应 Go ``NewAtriwispCreatorTool``。"""
    return AtriwispCreatorTool(first_runtime_info(*infos))


class AtriwispCreatorTool:
    """对应 Go ``AtriwispCreatorTool``。"""

    def __init__(self, info: RuntimeInfo | None = None) -> None:
        self.runtime_info = first_runtime_info(*([info] if info is not None else []))

    def name(self) -> str:
        return ATRIWISP_CREATOR_NAME

    def info(self) -> Info:
        return (
            Builder(ATRIWISP_CREATOR_NAME)
            .description(
                "获取创建 Atriwisp 监听器所需的精简 Atrinis/Atriena/Atriyph 说明，"
                "并附带当前 Atrinis 配置摘要。"
            )
            .source(SourceBuiltin)
            .risk(RiskLow)
            .superadmin_only()
            .depends_on("read_file", "edit_file", "shell")
            .build_info()
        )

    def schema(self) -> ToolSchema:
        return (
            Builder(ATRIWISP_CREATOR_NAME)
            .description(
                "无参数。调用后返回精简的 Atriwisp 创建任务卡，包含当前 Atrinis 配置摘要、"
                "Atriena 协议、Atriyph 写法和安全注意事项。"
            )
            .build_schema()
        )

    async def call(self, ctx: Any, req: CallRequest) -> Result:
        return Result(content=build_atriwisp_creator_guide(
            load_atriwisp_creator_config(self.runtime_info)
        ))


def load_atriwisp_creator_config(info: RuntimeInfo) -> AtriwispCreatorConfigSnapshot:
    """读当前配置并汇总成摘要。对应 Go ``loadAtriwispCreatorConfig``。"""
    app_config_path, ok = find_atriwisp_creator_app_config_path(info)
    if not ok:
        return AtriwispCreatorConfigSnapshot(warnings=[_NOT_FOUND_WARNING])

    try:
        cfg = load(app_config_path, strict=False)
    except Exception as exc:  # noqa: BLE001 - 读配置失败要变成可见 warning
        return AtriwispCreatorConfigSnapshot(
            app_config_path=app_config_path,
            warnings=[
                f"读取 ATRI-X 配置失败：{exc}。请提示用户修复配置后再创建 Atriwisp。"
            ],
        )

    atrinis = getattr(cfg, "atrinis", None)
    tokens = getattr(atrinis, "tokens", None) or {}
    token_names: list[str] = []
    token_env_set: set[str] = set()
    for name, token in tokens.items():
        token_names.append(str(name))
        for env_name in getattr(token, "token_env", None) or []:
            text = (env_name or "").strip()
            if text != "":
                token_env_set.add(text)
    token_names.sort()
    token_env_names = sorted(token_env_set)

    app_path = getattr(cfg, "config_path", "") or app_config_path
    snapshot = AtriwispCreatorConfigSnapshot(
        app_config_path=app_path,
        atrinis_config_path=getattr(cfg, "atrinis_config_path", "") or "",
        enabled=bool(getattr(atrinis, "enabled", False)),
        addr=(getattr(getattr(atrinis, "http", None), "addr", "") or "").strip(),
        token_names=token_names,
        token_env_names=token_env_names,
        allowed_tools=list(getattr(atrinis, "allowed_tools", None) or []),
    )
    snapshot.allowed_tools.sort()

    if not snapshot.enabled:
        snapshot.warnings.append(
            "当前 Atrinis 未启用。测试 Atriwisp 前，请提示用户设置 atrinis.toml 的 enabled=true。"
        )
    if snapshot.addr == "":
        snapshot.warnings.append(
            "未读取到 Atrinis HTTP addr。请提示用户配置 atrinis.toml 的 [http].addr。"
        )
    if not snapshot.token_env_names:
        snapshot.warnings.append(
            "当前未配置 Atrinis token_env。请提示用户配置 token_env，"
            "并在系统环境变量或配置目录 .env 中保存 token。"
        )
    return snapshot


def find_atriwisp_creator_app_config_path(info: RuntimeInfo) -> tuple[str, bool]:
    """找出 app.toml 的位置。对应 Go ``findAtriwispCreatorAppConfigPath``。"""
    info = info.normalize()
    if (info.config_path or "").strip() != "":
        return os.path.normpath(info.config_path), True
    env_path = (os.environ.get(ENV_CONFIG_FILE, "") or "").strip()
    if env_path != "":
        return os.path.normpath(env_path), True
    directory = user_config_dir()
    if directory:
        name = APP_DIR_NAME if os.name == "nt" else XDG_APP_DIR_NAME
        path = os.path.join(directory, name, "app.toml")
        if file_exists(path):
            return path, True
    return "", False


def user_config_dir() -> str:
    """用户配置目录。对应 Go ``os.UserConfigDir``。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
        return base
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if base:
        return base
    home = os.path.expanduser("~")
    return os.path.join(home, ".config") if home else ""


def file_exists(path: str) -> bool:
    """对应 Go ``fileExists``。"""
    try:
        os.stat(path)
    except OSError:
        return False
    return True


def build_atriwisp_creator_guide(cfg: AtriwispCreatorConfigSnapshot) -> str:
    """拼出精简任务卡。对应 Go ``buildAtriwispCreatorGuide``。"""
    b: list[str] = []
    b.append(
        "#task create_atriwisp - 帮用户设计或创建 Atriwisp 监听器\n"
        "<- $user_goal:str!\n"
        "<- $atrinis_config:object?\n"
        "-> $design:object\n"
        "-> $files:list\n"
        "-> $test_commands:list\n"
        "-> $security_notes:list\n"
        "// brief: Atrinis 是 ATRI-X 内部事件监听枢纽；Atriwisp 是外部监听器；"
        "Atriena v3 是 Atriwisp 向 Atrinis 投递事件的 JSON over HTTP 协议。\n"
        "// tool_scope: 这个工具只提供创建指引；读文件用 read_file，写文件用 edit_file，"
        "测试命令用 shell。\n"
        "// roles: Atrinis 负责鉴权、协议校验、去重、记录、目标裁决，并按 record/direct/llm 分发。\n"
        "// roles: Atriwisp 负责观察服务器、RSS、Webhook、日志、脚本、游戏或设备状态，"
        "并上报 Atriena 事件。\n"
        "// roles: Atriena 统一描述来源、事件 id、内容、处理模式、目标请求和可选工具声明。\n"
    )
    _write_config(b, cfg)
    b.append("// atriena_v1: endpoint\n")
    if cfg.addr != "":
        b.append("// POST " + endpoint_url(cfg.addr, "/atriena/v3/events") + "\n")
        b.append("// GET  " + endpoint_url(cfg.addr, "/healthz") + "\n")
    else:
        b.append("// endpoint: unknown; ask user to configure atrinis.toml [http].addr\n")
    b.append(
        "// auth: Authorization: Bearer $TOKEN\n"
        "// auth: X-Atrinis-Token: $TOKEN\n"
        "// token_setup: token_env 只声明变量名，token 值请设置到系统环境变量或配置目录 .env。\n"
        '// required_fields: version="atriena.v3", atriwisp.name, source, id, mode\n'
        "// direct_record_fields: content/segments/calls 至少提供一个；llm 模式仍需要 content\n"
        "// optional_fields: atriwisp.tags, created_at, title, format, model_slot, "
        "tool_list_names, tools, segments, calls, meta\n"
        "// modes: record 只记录；direct 直接通知；llm 进入后台 LLM Session。\n"
        "// media: image/file segment 使用 url，值为 HTTP(S)、data base64 或完整 "
        "media:<sha256>，不新增独立 media 字段。direct 实际发送才进入中心，不保留 sandbox "
        "下载副本；已有扩展名不重复追加。\n"
        "// media_boundary: Atriwisp 只管理自身文件，不提供媒体 RPC；"
        "其远端工具不接收 ATRI-X 媒体文件或导出路径。\n"
        "// report_media: report_segments 的 url 使用 workspace 相对路径或完整 "
        "media:<sha256>；宿主导入后 outbox 保存稳定 ID。\n"
        "// session_mode: 可选：\"work/chat\"。默认work，不需要工具时设为chat\n"
        "// targets: 必填数组；{\"platform\":\"cli\"} 表示投递到 cli 超级管理员；"
        "{\"platform\":\"telegram\",\"type\":\"private\",\"id\":\"123\"} 表示指定私聊；"
        "{\"platform\":\"all\"} 表示所有已启用平台超级管理员。\n"
        "// model_slot: atriwisp1 | atriwisp2 | atriwisp3；未配置时由 Atrinis fallback。\n"
        "// minimal_direct_payload: {\"version\":\"atriena.v3\","
        "\"atriwisp\":{\"name\":\"example-atriwisp\",\"tags\":[\"example\"]},"
        "\"source\":\"example-source\",\"id\":\"stable-event-id\",\"mode\":\"direct\","
        "\"title\":\"事件标题\",\"content\":\"可直接通知给管理员的文本。\","
        "\"targets\":[{\"platform\":\"cli\"}]}\n"
        "// minimal_direct_calls_payload: {\"version\":\"atriena.v3\","
        "\"atriwisp\":{\"name\":\"example-atriwisp\"},\"source\":\"example-source\","
        "\"id\":\"stable-event-id\",\"mode\":\"direct\","
        "\"targets\":[{\"platform\":\"qqonebot\",\"type\":\"group\",\"id\":\"987654321\"}],"
        "\"calls\":[{\"kind\":\"capability\",\"name\":\"message.recall\","
        "\"platform\":\"qqonebot\","
        "\"target\":{\"platform\":\"qqonebot\",\"type\":\"group\",\"id\":\"987654321\"},"
        "\"params\":{\"message_id\":1024}}]}\n"
        "// minimal_llm_payload: {\"version\":\"atriena.v3\","
        "\"atriwisp\":{\"name\":\"example-atriwisp\",\"tags\":[\"example\"]},"
        "\"source\":\"example-source\",\"id\":\"stable-event-id\",\"mode\":\"llm\","
        "\"title\":\"需要分析的事件\",\"format\":\"atriyph\","
        "\"content\":\"#task review_event - 判断事件是否需要通知\\n"
        "<- $event:object!\\n-> $report:str\\n** 基于事件内容、meta 和工具结果判断\\n"
        "~ 编造日志、指标或结论\\n> 如果需要通知，给出原因和建议；否则说明无需打扰。\","
        "\"tool_list_names\":[],\"targets\":[{\"platform\":\"cli\"}],\"meta\":{}}\n"
    )
    b.append(_comment_block("atriyph_rule_card", atriyph_rule_card()))
    b.append(
        "** 如果 current_atrinis 有 warning，先提醒用户处理。\n"
        "** event id 在同一 source 内稳定且唯一。\n"
        "** Atriwisp 只上报事件，由 Atrinis/ATRI-X 决定记录、通知、分析或调用工具。\n"
        "** 真实文件修改使用 edit_file；本地检查、curl 或脚本测试使用 shell。\n"
        "** token 值由用户设置到系统环境变量或配置目录 .env；Atriwisp 代码只读取环境变量。\n"
        "** targets 必须由事件明确声明；用户目标不明确时，先询问用户要推送到哪些平台或会话。\n"
        "** tool_list_names 请求 ATRI-X 内部工具或 Skill，最终可用性由 atrinis.toml "
        "allowed_tools、ToolRun 和 Security Policy 裁决。\n"
        "** tools 是 Atriwisp 随事件声明的外部工具，适合查询 Atriwisp 所在环境的状态、"
        "日志或详情。\n"
        "** Atriwisp 外部工具 endpoint 默认绑定 127.0.0.1，除非用户明确需要远程访问。\n"
        "~ 硬编码 token\n"
        "~ 记录 token\n"
        "~ 直接读取系统环境变量或配置目录 .env 中的 token/key 值\n"
        "~ 编造 endpoint/token\n"
        "~ 让 Atriwisp 直接向聊天平台发消息\n"
        "> 创建或修改 Atriwisp 后，简要说明设计、文件路径、配置项、环境变量、测试命令和"
        "安全注意事项。\n"
    )
    return "".join(b)


def _write_config(b: list[str], cfg: AtriwispCreatorConfigSnapshot) -> None:
    """写出 ``// current_atrinis`` 段。对应 Go ``writeAtriwispCreatorConfig``。"""
    b.append("// current_atrinis\n")
    _field(b, "app_config", cfg.app_config_path)
    _field(b, "atrinis_config", cfg.atrinis_config_path)
    _field(b, "enabled", "true" if cfg.enabled else "false")
    if cfg.addr != "":
        _field(b, "endpoint", endpoint_url(cfg.addr, "/atriena/v3/events"))
        _field(b, "healthz", endpoint_url(cfg.addr, "/healthz"))
    else:
        _field(b, "endpoint", "unknown")
    _list_field(b, "token_names", cfg.token_names)
    _list_field(b, "token_env", cfg.token_env_names)
    _list_field(b, "allowed_tools", cfg.allowed_tools)
    for warning in cfg.warnings:
        _field(b, "warning", warning)


def _field(b: list[str], key: str, value: str) -> None:
    text = value if (value or "").strip() != "" else "unknown"
    b.append("// " + key + ": " + text + "\n")


def _comment_block(title: str, text: str) -> str:
    out = ["// " + title + "\n"]
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        out.append("// " + line + "\n")
    return "".join(out)


def _list_field(b: list[str], key: str, values: list[str]) -> None:
    b.append("// " + key + ": ")
    if not values:
        b.append("[]\n")
        return
    b.append("[" + ", ".join(values) + "]\n")


def endpoint_url(addr: str, path: str) -> str:
    """把 addr + path 拼成 URL。对应 Go ``endpointURL``。"""
    addr = (addr or "").strip()
    if addr == "":
        return "unknown"
    if addr.startswith("http://") or addr.startswith("https://"):
        return addr.rstrip("/") + path
    return "http://" + addr.rstrip("/") + path


__all__ = [name for name in dir() if not name.startswith("_")]
