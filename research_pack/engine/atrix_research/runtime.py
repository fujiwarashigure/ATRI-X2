"""工具层的公共运行时：运行定位、参数归一化、产物读取。

为什么单独一层：每个硬工具（``flow`` / ``compose`` / ``audit`` / 九个既有工具）
都需要同一套"找到运行目录 + 把模型传来的 JSON 参数字符串解析成对象"的逻辑。
把它放在这里，新加一个硬工具就只写"业务纯函数 + 一个 handler"，不会各自
发明一套参数解析（那是模型适配性下降的常见来源）。

约定：本模块**不导入** ``tools.py``，避免与新工具形成循环依赖。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .config import (
    ResearchSettings,
    list_runs,
    load_settings,
    paths,
    resolve_run_id,
    safe_run_id,
)
from .errors import ARG_INVALID, ARTIFACT_MISSING, ResearchError
from .http import HttpClient
from .models import Work
from .store import RunStore

__all__ = [
    "MARKER_RE",
    "as_bool",
    "as_list",
    "draft_path",
    "http_client",
    "included_works",
    "json_arg",
    "library",
    "next_steps",
    "open_run",
    "result",
    "roots",
    "run_id_for",
    "screening",
    "settings",
    "word_count",
    "works",
]

#: 正文引用标记 ``[@cite_key]``；必须与 ``qa.py`` 的正则保持一致。
MARKER_RE = re.compile(r"\[@([A-Za-z0-9_\-\u4e00-\u9fff]+)\]")


# ---------------------------------------------------------------------------
# 结果信封（业务层只返回这四段，CLI 负责包协议）
# ---------------------------------------------------------------------------


def result(
    data: dict[str, Any],
    *,
    artifacts: list[dict[str, Any]] | None = None,
    degraded: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "data": data,
        "artifacts": artifacts or [],
        "degraded": degraded or [],
        "warnings": warnings or [],
    }


# ---------------------------------------------------------------------------
# 设置与网络
# ---------------------------------------------------------------------------


def settings() -> ResearchSettings:
    return load_settings()


def http_client(settings_obj: ResearchSettings | None = None) -> HttpClient:
    return HttpClient(settings_obj or load_settings())


def roots() -> Any:
    return paths().ensure()


# ---------------------------------------------------------------------------
# 运行定位与产物
# ---------------------------------------------------------------------------


def open_run(run_arg: str | None) -> RunStore:
    """按 ``run`` 参数（可为 ``latest``）打开运行目录，不存在时报可操作错误。"""
    root = roots()
    run_id = resolve_run_id(run_arg or "")
    run_dir = root.run_dir(run_id)
    if not os.path.isdir(run_dir):
        raise ResearchError(
            "RUN_NOT_FOUND",
            f"运行不存在: {run_id}",
            hint="用 research_doctor action=status 查看可用运行 id。",
        )
    return RunStore(run_dir, root.runs_dir)


def library(store: RunStore, *, required: bool = True) -> dict[str, Any]:
    data = store.read("library.json", required=required, default={})
    if required and not (data or {}).get("works"):
        raise ResearchError(
            ARTIFACT_MISSING,
            "文献库为空",
            hint="先调用 research_search 完成检索（queries 至少一个）。",
        )
    return data or {}


def works(lib: dict[str, Any]) -> list[Work]:
    return [Work.from_dict(item) for item in lib.get("works") or []]


def screening(store: RunStore) -> dict[str, Any] | None:
    data = store.read("screening.json", required=False, default=None)
    return data if isinstance(data, dict) else None


def included_works(store: RunStore, lib: dict[str, Any]) -> list[Work]:
    """有筛选结果就用纳入集，否则退回全部候选（调用方要在结果里标明）。"""
    all_works = works(lib)
    screen = screening(store)
    keys = set((screen or {}).get("included_keys") or [])
    if not keys:
        return all_works
    return [work for work in all_works if work.key in keys]


def draft_path(store: RunStore) -> str:
    return store.path("draft.md")


def next_steps(state: dict[str, Any], store: RunStore | None = None) -> list[str]:
    """阶段 → 下一步提示（人类可读；机器可读版本见 ``flow`` 工具）。"""
    stage = state.get("stage") or "init"
    mapping = {
        "init": ["research_search 检索文献"],
        "search": ["research_screen stage=title_abstract 筛选"],
        "screen": ["research_verify 核验引文"],
        "verify": ["research_cite 生成参考文献", "research_plan action=outline 生成大纲"],
        "outline": ["research_compose 逐节成稿（结构化 claim 交给引擎排版）"],
        "writing": ["research_compose 补齐剩余小节", "research_write action=check 质检"],
        "check": ["research_audit 确定性评审打分", "research_export action=all 生成交付文件"],
        "review": ["research_export action=all 生成报告与交付文件"],
        "report": ["research_export action=export 导出 docx/pdf"],
        "export": ["完成：检查 exports/ 下的文件并交付"],
    }
    return list(mapping.get(stage, ["research_doctor action=status 查看当前状态"]))


# ---------------------------------------------------------------------------
# 参数归一化（模型传来的可能是字符串、JSON 字符串或逗号分隔）
# ---------------------------------------------------------------------------


def json_arg(value: Any, *, expect: type | tuple[type, ...] | None = None) -> Any:
    """把参数转成 Python 值；空值返回 ``None``，类型不符抛参数错误。"""
    if value is None or value == "":
        return None
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise ResearchError(
                    ARG_INVALID,
                    f"参数不是合法 JSON: {exc}",
                    hint='对象/数组参数的写法示例：{"include": {"year_from": 2020}}',
                ) from None
        else:
            parsed = text
    if expect is not None and not isinstance(parsed, expect):
        raise ResearchError(
            ARG_INVALID,
            f"参数类型不对，期望 {expect}，收到 {type(parsed).__name__}",
        )
    return parsed


def as_list(value: Any) -> list[str]:
    """数组参数既接受 JSON 数组，也接受逗号分隔字符串，便于手工调用。

    个别宿主（例如 PowerShell 5.1 调原生 exe）会吃掉参数里的双引号，
    把 ``["a","b"]`` 变成 ``[a,b]``。这里对这种情况做容错解析，
    避免一个引号问题就让整次检索失败。
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text[:1] in "[{":
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, str):
            return [parsed.strip()] if parsed.strip() else []
        # JSON 解析失败：按"列表字面量"退回手工拆分
        text = text.strip("[]")
        parts = [part.strip().strip("\"'").strip() for part in text.split(",")]
        return [part for part in parts if part]
    if "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def run_id_for(title: str) -> str:
    """按标题生成不冲突的运行 id（时间戳 + slug）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:24].strip("-")
    base = f"r-{stamp}" + (f"-{slug}" if slug else "")
    candidate = safe_run_id(base)
    root = paths().runs_dir
    suffix = 1
    while os.path.isdir(os.path.join(root, candidate)):
        suffix += 1
        candidate = safe_run_id(f"{base}-{suffix}"[:64].strip("-"))
    return candidate


def word_count(text: str) -> int:
    """中英混合字数：CJK 按字计，其它按词计（与 qa.py 口径一致）。"""
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text or ""))
    latin = len(re.findall(r"[A-Za-z0-9]+", text or ""))
    return cjk + latin
