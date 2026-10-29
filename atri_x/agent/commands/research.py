"""``/research``：科研研究助理的只读操作面板命令。

对应功能（全部只读，不起子进程、不写文件）：

* ``/research`` 或 ``/research status``：列出全部研究运行（阶段/题目/更新时间）；
* ``/research <run-id>``：看某次运行的产物清单与计数；
* ``/research env``：引擎/venv/依赖/数据目录体检；
* ``/research open``：打开研究数据目录（仅超级管理员，避免普通成员在服务器上弹窗）。

命令为什么不做"一键跑完整流程"：研究流程的每一步都要模型判断（检索式、
纳排标准、写作），命令层只负责**看状态**；真正的执行由 AgentSkill 工具完成，
这样命令可以给普通群成员用，而不会绕过工具的风险与审计。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from atri_x.command import Info, Request, Result, new_func
from atri_x.agent.commands.deps import Deps, Registrar, register_factories
from atri_x.security import RoleSuperadmin, RoleUser, actor_from_context

__all__ = ["ResearchModule", "new_research", "research_home"]


def research_home() -> str:
    """研究数据目录：``ATRIX_RESEARCH_HOME`` → 配置目录下的 ``research``。

    解析规则唯一出处在 :func:`atri_x.research.paths.research_home`（命令层、桌面「研究」
    页、技能子进程三处必须看到同一个目录）。这里刻意不 import 引擎包：命令层要能在
    引擎未安装时也正常回答，否则 ``/research env`` 在"还没装"的时候会先崩掉。
    """
    from atri_x.research.paths import research_home as resolve_home

    return resolve_home()


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _runs(home: str) -> list[dict[str, Any]]:
    runs_dir = os.path.join(home, "runs")
    if not os.path.isdir(runs_dir):
        return []
    out: list[dict[str, Any]] = []
    for name in os.listdir(runs_dir):
        state = _load_json(os.path.join(runs_dir, name, "run.json"))
        if not isinstance(state, dict):
            continue
        out.append(
            {
                "id": state.get("id") or name,
                "title": str(state.get("title") or ""),
                "stage": str(state.get("stage") or ""),
                "updated_at": str(state.get("updated_at") or ""),
                "dir": os.path.join(runs_dir, name),
            }
        )
    out.sort(key=lambda item: item["updated_at"], reverse=True)
    return out


#: 阶段的中文说明（报告里直接给用户看）。
_STAGES = {
    "init": "已立项",
    "search": "已检索",
    "screen": "已筛选",
    "verify": "已核验",
    "cite": "已生成引用",
    "outline": "已出大纲",
    "writing": "写作中",
    "check": "已质检",
    "review": "已评审",
    "report": "已出报告",
    "export": "已导出",
}

_ARTIFACT_LABELS = {
    "library.json": "文献库",
    "screening.json": "筛选结果",
    "verification.json": "引文核验",
    "references.json": "参考文献",
    "outline.json": "写作大纲",
    "draft.md": "论文正文",
    "qa.json": "质检结果",
    "panel.json": "评审聚合",
    "report.md": "研究报告",
    "exports": "导出目录",
}


def _format_status(home: str) -> str:
    runs = _runs(home)
    lines = [f"研究数据目录：{home}"]
    if not runs:
        lines.append("还没有研究运行。")
        lines.append("在对话里让助理开始一次研究（会创建运行），或检查科研技能是否已发现。")
        return "\n".join(lines)
    lines.append(f"共 {len(runs)} 次运行（最近在前）：")
    for run in runs[:10]:
        stage = _STAGES.get(run["stage"], run["stage"] or "未知")
        title = run["title"][:40] or "（无题目）"
        lines.append(f"- {run['id']}  [{stage}]  {title}")
    latest = runs[0]
    lines.append("")
    lines.append(f"最近一次：{latest['id']}")
    lines.extend(_format_run_detail(latest["dir"]))
    lines.append("")
    lines.append("提示：让助理调用科研工具继续推进；输入 /research <运行id> 看产物明细。")
    return "\n".join(lines)


def _format_run_detail(run_dir: str) -> list[str]:
    state = _load_json(os.path.join(run_dir, "run.json")) or {}
    lines = [
        f"题目：{state.get('title', '')}",
        f"研究问题：{state.get('question', '')}",
        f"阶段：{_STAGES.get(str(state.get('stage') or ''), state.get('stage') or '')}"
        f" · 语言：{state.get('language', '')} · 引用样式：{state.get('style', '')}",
    ]
    counts = state.get("counts") or {}
    if counts:
        pairs = []
        for key, label in (
            ("identified", "检索命中"),
            ("deduped", "去重后"),
            ("included", "纳入"),
            ("verified_ratio", "核验通过率"),
            ("qa_score", "质检得分"),
        ):
            if key in counts:
                pairs.append(f"{label} {counts[key]}")
        if pairs:
            lines.append("计数：" + "，".join(pairs))
    present = []
    for name, label in _ARTIFACT_LABELS.items():
        path = os.path.join(run_dir, name)
        if os.path.isdir(path):
            files = [
                item
                for item in os.listdir(path)
                if os.path.isfile(os.path.join(path, item))
            ]
            present.append(f"{label}（{len(files)} 个文件）")
        elif os.path.isfile(path):
            size = os.path.getsize(path)
            present.append(f"{label}（{_human_size(size)}）")
    if present:
        lines.append("产物：" + "、".join(present))
    else:
        lines.append("产物：（暂无）")
    return lines


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _format_pack_line() -> str:
    """随包引擎一行（冻结版随包自带，首次启动自动装到研究目录）。"""
    try:
        from atri_x.research.bootstrap import pack_status
    except Exception:  # noqa: BLE001 - 展示失败不应让命令失败
        return ""
    try:
        status = pack_status()
    except Exception:  # noqa: BLE001
        return ""
    if not status.get("pack_available"):
        return ""
    version = status.get("pack_engine_version") or "?"
    if status.get("installed"):
        state = "已安装" if status.get("up_to_date") else "已安装（有更新，下次启动自动升级）"
    else:
        state = "随包自带，首次启动自动安装"
    return f"- 随包引擎：v{version}（{state}）"


def _format_env(home: str) -> str:
    try:
        from atri_x import __version__ as atri_version  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 版本号只用于展示
        atri_version = "unknown"

    lines = [f"科研助理环境：{home}"]
    lines.append(f"- ATRI-X 版本：{atri_version}")
    lines.append(f"- Python：{sys.version.split()[0]}")
    engine = os.path.join(home, "engine", "atrix_research", "__init__.py")
    venv_python = os.path.join(
        home, "venv", "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python"
    )
    lines.append(f"- 引擎：{'已安装' if os.path.isfile(engine) else '未安装'}")
    pack_line = _format_pack_line()
    if pack_line:
        lines.append(pack_line)
    lines.append(f"- 独立 venv：{'已创建' if os.path.isfile(venv_python) else '未创建'}")
    settings = _load_json(os.path.join(home, "settings.json"))
    lines.append(f"- settings.json：{'已就绪' if isinstance(settings, dict) else '缺失（用默认值）'}")
    if os.path.isfile(venv_python):
        site_dirs = [
            os.path.join(home, "venv", "Lib", "site-packages"),
            os.path.join(home, "venv", "lib"),
        ]
        found = []
        for name, module in (("python-docx", "docx"), ("reportlab", "reportlab"), ("pypdf", "pypdf")):
            ok = any(
                os.path.isdir(os.path.join(site, module)) or os.path.isdir(os.path.join(site, module + "-"))
                for site in site_dirs
                if os.path.isdir(site)
            )
            if not ok:
                # venv 的 site-packages 目录结构随版本变化，退一步用通配目录名判断
                for site in site_dirs:
                    if not os.path.isdir(site):
                        continue
                    for entry in os.listdir(site):
                        if entry.lower().startswith(module.lower()):
                            ok = True
                            break
            found.append(f"{name}={'有' if ok else '无'}")
        lines.append("- 可选依赖：" + "，".join(found))
    lines.append("- 工具入口：research_plan / research_search / research_screen / research_verify")
    lines.append("            research_cite / research_write / research_panel / research_export / research_doctor")
    lines.append("")
    lines.append("完整体检：让助理调用 research_doctor（action=env），它会实测网络与缓存。")
    return "\n".join(lines)


def _format_run_by_id(home: str, run_id: str) -> str:
    target = run_id.strip()
    for run in _runs(home):
        if run["id"] == target or run["id"].startswith(target):
            lines = [f"运行：{run['id']}"]
            lines.extend(_format_run_detail(run["dir"]))
            return "\n".join(lines)
    return f"找不到运行：{run_id}。输入 /research 查看全部运行 id。"


def new_research(deps: Deps) -> Any:
    """构造 ``/research`` 命令。"""

    async def handle(ctx: Any, req: Request) -> Result | None:
        home = research_home()
        argument = (req.args or "").strip()
        lowered = argument.lower()
        actor, actor_ok = actor_from_context(ctx)
        is_superadmin = actor_ok and getattr(actor, "role", "") == RoleSuperadmin
        if lowered in ("", "status", "list"):
            return Result(content=_format_status(home))
        if lowered in ("env", "doctor", "check"):
            return Result(content=_format_env(home))
        if lowered in ("open", "dir"):
            if not is_superadmin:
                return Result(content="只有超级管理员可以用 /research open（避免在服务器上弹窗）。")
            if not os.path.isdir(home):
                return Result(content=f"研究目录还不存在：{home}")
            try:
                if os.name == "nt":
                    os.startfile(home)  # type: ignore[attr-defined]
                else:
                    import subprocess

                    subprocess.Popen(["xdg-open", home])
            except Exception as exc:  # noqa: BLE001 - 打不开目录不该让命令失败
                return Result(content=f"打开目录失败：{type(exc).__name__}: {exc}\n目录：{home}")
            return Result(content=f"已打开：{home}")
        if lowered in ("help", "?"):
            return Result(content=(
                "/research            列出研究运行与最近一次产物\n"
                "/research <运行id>   查看指定运行的产物与计数\n"
                "/research env        引擎/venv/依赖体检\n"
                "/research open       打开研究数据目录（仅超管）"
            ))
        return Result(content=_format_run_by_id(home, argument))

    return new_func(
        Info(
            name="research",
            usage="/research [status|env|open|<运行id>]",
            description=(
                "Academic research assistant: list research runs, inspect artifacts of one run, "
                "check engine/venv environment, or open the research data directory (superadmin). "
                "Read-only; the actual research steps run through the research_* AgentSkill tools."
            ),
            # 普通群成员即可查看研究状态：这里没有写操作，也没有跨用户数据
            min_role=RoleUser,
        ),
        handle,
    )


class ResearchModule:
    """把 ``/research`` 注册进命令路由（对应 ``default_modules()`` 里的一项）。"""

    def register_commands(self, registrar: Registrar, deps: Deps) -> None:
        register_factories(registrar, deps, new_research)
