"""域级文档的生成器：每个高价值域一个 ``README.md``，外加术语表。

为什么要生成：域 README 里最容易漂的是"这个域有哪些模块、对应哪个 Go 包、被哪些
测试覆盖"——这三样都能从 AST 索引算出来，手写必然腐烂。**只有"域职责 / 不做啥 /
易错点"是判断类知识**，那部分写成 :data:`DOMAIN_NOTES` 里的一小段数据，随渲染一起
出门；于是同一份产物既能给人当入口，也能被 ``--check`` 判漂移。

产物：

- ``atri_x/<domain>/README.md``（10 个域）：生成段 = 职责 + 模块清单 + Go 映射 +
  测试覆盖；数据段 = 判断类说明。
- ``docs/dev/glossary.md``：中文功能名 ↔ Python 域 ↔ Go 包 ↔ 测试 ↔ 配置段。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from atri_x.devtools.astindex import ModuleInfo, RepoIndex, collect_metrics

__all__ = [
    "DOMAIN_NOTES",
    "DomainNote",
    "GLOSSARY_PATH",
    "domain_readme_paths",
    "render_domain_readme",
    "render_glossary",
]

GLOSSARY_PATH = "docs/dev/glossary.md"

HEADER = (
    "<!-- 本文件由 `python scripts/gen_index.py` 生成，禁止手改。 -->\n"
    "<!-- 域职责 / 易错点是 `atri_x/devtools/domaindocs.py` 里的数据；"
    "模块清单与测试映射由代码算出。 -->\n"
)


@dataclass(frozen=True)
class DomainNote:
    """一个域的**判断类**说明（生成不了的部分）。"""

    title: str
    duty: str
    not_do: str
    entry: str
    pitfalls: tuple[str, ...] = ()
    config: str = ""


#: 首批 10 个高价值域。``duty`` 一句话说清"这个域负责什么"；
#: ``not_do`` 说清边界（比职责更能防止放错地方）。
DOMAIN_NOTES: dict[str, DomainNote] = {
    "agent": DomainNote(
        title="Agent 对话编排",
        duty="一轮对话的全部编排：取上下文 → 调模型 → 跑工具 → 落库 → 生成输出。",
        not_do="不放平台协议细节（那在 `platform/`）、不直接发消息（那在 `delivery/`）。",
        entry="`atri_x/agent/core.py`（Agent 装配与主流程）、`atri_x/agent/chat.py`。",
        pitfalls=(
            "改系统提示词装配时注意来源覆盖顺序（群友人设 > 角色卡 > SOUL.md），"
            "1.1.2 修过一个「装配期覆盖」的真 bug。",
            "工具轮次用 `limits.max_rounds_per_turn`；调大只会更慢更贵。",
            "命令层（`agent/commands/`）只做参数解析与回执，业务逻辑应在服务层。",
        ),
        config="`[agent]`、`[agent.dsh]`",
    ),
    "tool": DomainNote(
        title="工具与工具运行时",
        duty="内置工具的声明（Builder/Info/Result）、执行闸门（风险确认 / 超管 / 白名单）与注册表。",
        not_do="不在工具里做平台适配；工具只吃 `ctx` 与参数，结果回给模型。",
        entry="`atri_x/tool/builtin/register.py`（注册表）、`atri_x/toolrun/runner.py`（执行闸门）。",
        pitfalls=(
            "新增工具必须补 `atri_x/gui/toolcatalog.py` 一行，否则 `test_gui_tabs` 直接红。",
            "注册表计数断言（`tests/test_app_tool_runtime_wiring.py` 的 54→55）是刻意的，"
            "改注册表就要同步改。",
            "只读工具用 `RiskSafe`，不要习惯性给 `RiskLow`+owner_scoped（那等于对所有人放开）。",
            "超管专属用 `Builder.superadmin_only()`：发现与执行两道闸门都会拦。",
        ),
        config="`state.toml` 的 `tool_allowlist`、`[tools]`",
    ),
    "platform": DomainNote(
        title="平台适配器",
        duty="把 QQ / 官方 / Telegram / CLI 等外部协议翻译成 `platform.PlatformAdapter` 契约。",
        not_do="不写业务逻辑、不做内容决策；适配器只负责传输与消息形态。",
        entry="`atri_x/platform/platform.py`（契约）、`atri_x/platform/qq_onebot/`（NapCat/SnowLuma）。",
        pitfalls=(
            "能力矩阵（capability）要对齐真实协议；不支持就用能力位声明，不要静默降级。",
            "适配器里的失败要隔离：一个平台挂掉不能带走整个进程（见 `app/platforms.py`）。",
            "QQ 官方与 Telegram **没有连过真实服务端**，只有进程内假连接用例（见 porting-status）。",
        ),
        config="`[platform.*]`",
    ),
    "gui": DomainNote(
        title="桌面端（Tk + 托盘）",
        duty="设置界面、页签、工具权限目录与托盘；只做展示与配置读写。",
        not_do="不在 GUI 里实现业务逻辑；它调用与 CLI 相同的那套服务。",
        entry="`atri_x/gui/app.py`（主窗口）、`atri_x/gui/tabs/`（页签）、`atri_x/gui/toolcatalog.py`。",
        pitfalls=(
            "`toolcatalog` 必须覆盖 `REGISTERED_TOOLS`（集合相等由测试钉住）。",
            "页签是懒构建的；在 `__init__` 里做重活会让首启变慢。",
            "Tk 只能在主线程碰；后台线程更新界面要走队列/`after`。",
        ),
        config="`state.toml` 与各服务的 GUI 段",
    ),
    "storage": DomainNote(
        title="存储与 SQLite",
        duty="会话、消息、工具调用记录、各种元数据的持久化与迁移。",
        not_do="不在这里做业务判断；存储层只保证读写与一致性。",
        entry="`atri_x/storage/models.py`（模型）、`atri_x/storage/sqlite/`（实现）、`atri_x/storage/sqlite/migrations.py`。",
        pitfalls=(
            "改表结构必须加迁移，且要能在已有库上原地升级（用户不会删库）。",
            "ID 是带连字符的小写 UUIDv4；时间统一 `datetime` + RFC3339 落库。",
            "SQLite 走同步路径（`threading.Lock`），不要在事件循环里做长事务。",
        ),
        config="`[storage]`",
    ),
    "config": DomainNote(
        title="配置与运行时状态",
        duty="`app.toml` / `services.toml` / `hooks.toml` / `state.toml` 的加载、校验、热加载与默认资产。",
        not_do="不放业务默认值以外的逻辑；配置项本身不产生行为。",
        entry="`atri_x/config/loader.py`（加载）、`atri_x/config/app_sections.py`（分段定义）、`atri_x/config/assets.py`（默认资产）。",
        pitfalls=(
            "新增配置键要同时进默认资产与文档（`docs/configuration.md`），否则用户看不到。",
            "热加载只对 `state.toml` 生效；改 `app.toml` 需要重启。",
            "`app_sections.py` 已经超过 600 行（棘轮冻结），新段优先放独立模块。",
        ),
        config="`[app]` / `[security]` / `[connect_notice]` 等",
    ),
    "hook": DomainNote(
        title="Hook 与插件",
        duty="事件钩子：注册、匹配规则、超时与失败策略。",
        not_do="Hook 不直接改 Agent 状态；它通过返回值影响流程。",
        entry="`atri_x/hook/manager.py`、`atri_x/hook/match.py`、`atri_x/hook/process.py`。",
        pitfalls=(
            "Hook 失败默认不阻断主流程，除非规则显式声明（否则一个坏插件会让机器人静默失聪）。",
            "`hooks.toml` 的热加载边界与 `state.toml` 不同，改完要重启。",
        ),
        config="`hooks.toml`",
    ),
    "llm": DomainNote(
        title="LLM 适配与协议",
        duty="统一的消息/工具 schema、OpenAI 与 Responses 两条协议适配、流式分片与错误分类。",
        not_do="不在这里拼提示词（那在 `agent/prompt*`）、不做重试策略的业务判断（在 `llm/breaker`）。",
        entry="`atri_x/llm/llm.py`（契约）、`atri_x/llm/openai/`（适配器）。",
        pitfalls=(
            "新增模型能力要落到 `ProtocolCapabilities`，不要在各适配器里各自 if。",
            "流式取消要传播到 HTTP 连接；否则取消后还会继续计费。",
            "思考模式（reasoning）与工具链同时用时容易触发 400，本仓已有两轮真机热修。",
        ),
        config="`[llm]` / `[models]`",
    ),
    "app": DomainNote(
        title="装配层",
        duty="把配置、存储、平台、工具、Agent 装到一起，并管生命周期（启动/健康/退出）。",
        not_do="不含业务逻辑；装配层只做「接线」，不替各域做决定。",
        entry="`atri_x/app/app.py`（入口与 Run）、`atri_x/app/runtime.py`、`atri_x/app/tool_runtime.py`。",
        pitfalls=(
            "装配顺序有依赖：工具运行时依赖平台适配器与存储，改顺序容易拿到半成品。",
            "降级要显式：缺依赖时宁可少注册（模型看不到）也不要注册一个只会失败的工具。",
            "`doctor` 是端到端自检入口，新增服务顺手加一条检查。",
        ),
        config="`[app]` 与各服务装配段",
    ),
    "media": DomainNote(
        title="媒体与图片处理",
        duty="图片编解码、媒体中心（落盘/引用）、表情图库与生图结果的存取。",
        not_do="不做内容审核策略（在 `safety/`）、不直接发文件（在 `delivery/`）。",
        entry="`atri_x/media/`、`atri_x/reaction/`、`atri_x/artist/`。",
        pitfalls=(
            "Pillow 是**可选**加速后端：缺它必须降级到纯 Python，不能崩。",
            "大图/大文件走投递后端（base64 / 文件），不要塞进模型上下文。",
            "临时文件名要可回收；本机 `mkdtemp` 有已知清理问题（见 AGENTS.md）。",
        ),
        config="`[image_generation]` / `[media]`",
    ),
}


def _module_table(modules: list[ModuleInfo], index: RepoIndex) -> list[str]:
    lines = ["| 模块 | 职责 | 行数 | 测试 |", "|---|---|---:|---|"]
    for module in modules:
        tests = index.tests_by_module.get(module.name, ())
        test_cell = "、".join(f"`{Path(path).name}`" for path in tests) if tests else "—"
        lines.append(f"| `{module.name}` | {_one_line(module.summary)} | {module.lines} | {test_cell} |")
    return lines


def _one_line(text: str, limit: int = 80) -> str:
    value = " ".join((text or "").replace("|", "\\|").split())
    return (value[: limit - 1] + "…") if len(value) > limit else (value or "—")


def _go_packages(modules: list[ModuleInfo], limit: int = 6) -> list[str]:
    """该域模块声明过的上游 Go 文件（去重、稳定排序）。"""
    paths = sorted({module.go_path for module in modules if module.go_path})
    return paths[:limit]


def render_domain_readme(index: RepoIndex, domain: str) -> str:
    note = DOMAIN_NOTES[domain]
    modules = index.modules_in(domain)
    tested = sum(1 for module in modules if index.tests_by_module.get(module.name))
    metrics = collect_metrics(index)
    lines = [
        HEADER,
        f"# `atri_x/{domain}/` — {note.title}",
        "",
        "## 这个域负责什么",
        "",
        note.duty,
        "",
        f"**不做什么**：{note.not_do}",
        "",
        f"**入口**：{note.entry}",
        "",
    ]
    if note.config:
        lines.extend([f"**配置段**：{note.config}", ""])
    lines.extend(["## 易错点", ""])
    for pitfall in note.pitfalls:
        lines.append(f"- {pitfall}")
    lines.append("")
    lines.extend(
        [
            "## 模块清单（自动生成）",
            "",
            f"共 **{len(modules)}** 个模块，"
            f"被测试覆盖 **{tested}** 个；全仓口径见 "
            "`devdocs/generated/module-map.md` 与 `devdocs/generated/test-map.md`。",
            "",
            *_module_table(modules, index),
            "",
        ]
    )
    go_paths = _go_packages(modules)
    if go_paths:
        lines.extend(
            [
                "## 对应上游 Go（自动生成）",
                "",
                "、".join(f"`{path}`" for path in go_paths)
                + "（完整反查见 `devdocs/generated/go-map.md`）",
                "",
            ]
        )
    lines.extend(
        [
            "## 改这个域之前",
            "",
            "- 读 `docs/dev/find-it.md` 里对应的任务条目；",
            "- 改完跑 `python scripts/gen_index.py`（本文件的模块清单会跟着更新），"
            "再跑 `python scripts/check_docs.py`；",
            f"- 全仓结构指标见 `scripts/complexity_budget.json`"
            f"（当前模块 {metrics['modules']} 个 / 超线文件 {metrics['long_files']} 个）。",
            "",
        ]
    )
    return "\n".join(lines)


def domain_readme_paths(index: RepoIndex) -> dict[str, str]:
    """``atri_x/<domain>/README.md`` → 内容（只覆盖 :data:`DOMAIN_NOTES` 里的域）。"""
    out: dict[str, str] = {}
    for domain in DOMAIN_NOTES:
        if not index.modules_in(domain):
            continue
        out[f"atri_x/{domain}/README.md"] = render_domain_readme(index, domain)
    return out


def _go_package_guess(modules: list[ModuleInfo]) -> str:
    """该域最常出现的 Go 目录前缀；没有映射时给破折号。"""
    counts: dict[str, int] = {}
    for module in modules:
        if not module.go_path:
            continue
        head = module.go_path.rsplit("/", 1)[0]
        counts[head] = counts.get(head, 0) + 1
    if not counts:
        return "—"
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def render_glossary(index: RepoIndex) -> str:
    lines = [
        HEADER,
        "# 术语表（自动生成 + 人工补）",
        "",
        "三套命名的对照：中文功能名（人） ↔ Python 域（代码） ↔ 上游 Go 包（对照）。"
        "查**具体模块**用 `devdocs/generated/module-map.md`；查**按任务入口**用 "
        "`docs/dev/find-it.md`。",
        "",
        "| 中文功能名 | Python 域 | 上游 Go 包 | 模块数 | 测试覆盖 | 配置段 |",
        "|---|---|---|---:|---:|---|",
    ]
    for domain, note in DOMAIN_NOTES.items():
        modules = index.modules_in(domain)
        if not modules:
            continue
        tested = sum(1 for module in modules if index.tests_by_module.get(module.name))
        lines.append(
            f"| {note.title} | `atri_x/{domain}/` | `{_go_package_guess(modules)}` "
            f"| {len(modules)} | {tested}/{len(modules)} | {note.config or '—'} |"
        )
    lines.extend(
        [
            "",
            "## 常用别名",
            "",
            "| 别名 / 旧名 | 指什么 |",
            "|---|---|",
            "| Atriwisp / Atrinis / Atriena | 三条内置角色能力线，见 `docs/atrinis.md` |",
            "| 协议端 / proto | Omni-Bot 协议后端（NapCat / SnowLuma），见 `devdocs/qq-backends-matrix.md` |",
            "| 群友人设 | 按「平台 + 群 + 用户」注入的成员档案，见 `docs/character-library.md` |",
            "| 研究引擎 / research_pack | 随包分发的纯标准库研究流水线，见 `docs/research.md` |",
            "| DSH | 深度路由内核（`runtime/dsh-kernel`），见 `devdocs/dsh-acp.md` |",
            "",
        ]
    )
    return "\n".join(lines)
