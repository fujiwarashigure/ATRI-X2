"""路径解析、运行目录布局与可配置限额。

目录布局（全部在 ATRI-X 的配置根下，和 app.toml 同级，不进 ATRI-X 安装目录，
升级 ATRI-X 不会丢研究数据）::

    <APPADATA>/ATRI-X/research/
        settings.json           # 限额与默认参数（用户可改）
        engine/                 # 本引擎副本（安装器写入）
        venv/                   # 独立虚拟环境（安装器创建）
        runs/<run_id>/          # 一次研究一个目录
            run.json
            library.json  search.json  screening.json  verification.json
            references.json  outline.json  draft.md  qa.json
            reviews.json  panel.json  report.md
            exports/  fulltext/  cache/
        cache/http/             # 跨运行共享的 HTTP 缓存

历史兼容：``ATRIX_RESEARCH_HOME`` 可整体改道（测试与多实例用）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import ARG_INVALID, ENV_INVALID, ResearchError

__all__ = [
    "DEFAULT_SETTINGS",
    "ResearchPaths",
    "ResearchSettings",
    "app_config_dir",
    "default_home",
    "load_settings",
    "paths",
    "reset_cache",
    "resolve_run_id",
    "safe_run_id",
]

APP_DIR_NAME = "ATRI-X"
XDG_APP_DIR_NAME = "atri-x"
ENV_HOME = "ATRIX_RESEARCH_HOME"
ENV_MAILTO = "ATRIX_RESEARCH_MAILTO"

_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

#: settings.json 缺失时的默认值。写进文件后就是用户可改的显式配置。
DEFAULT_SETTINGS: dict[str, Any] = {
    "schema": "atrix-research/settings/1",
    "contact_mailto": "",
    "http": {
        # 单次 HTTP 超时（秒）
        "timeout_seconds": 20,
        # 同一主机两次请求的最小间隔（秒），对公共 API 保持礼貌
        "min_interval_seconds": 0.35,
        # 缓存有效期（天）；0 表示不读缓存但仍写入
        "cache_ttl_days": 14,
        # 失败重试次数（仅对 429 / 5xx / 网络错误）
        "retries": 2,
    },
    "search": {
        "default_sources": ["openalex", "crossref", "arxiv"],
        "per_source_limit": 20,
        "max_total_candidates": 240,
    },
    "screen": {
        "max_fulltext_bytes": 8 * 1024 * 1024,
    },
    "verify": {
        "title_similarity_pass": 0.90,
        "title_similarity_warn": 0.75,
        "min_verified_ratio": 0.90,
    },
    "cite": {
        "default_style": "gbt7714",
    },
    "panel": {
        "scale": 5,
        "weights": {
            "originality": 0.22,
            "rigor": 0.26,
            "clarity": 0.16,
            "significance": 0.22,
            "ethics": 0.14,
        },
        "thresholds": {"accept": 4.20, "minor_revision": 3.50, "major_revision": 2.60},
        "disagreement_warn": 1.0,
    },
    "limits": {
        # 单次安装保留的运行数上限（超出时最旧的被拒绝创建，而不是静默删除）
        "max_runs": 200,
        # 单个运行目录允许的最大字节数
        "max_run_bytes": 128 * 1024 * 1024,
        # 单次检索写入 library 的最大条数
        "max_library_items": 500,
    },
}


def _user_config_dir() -> str:
    """``os.UserConfigDir`` 的等价实现（与 ATRI-X 保持一致）。"""
    if os.name == "nt":
        return (os.environ.get("AppData") or os.environ.get("APPDATA") or "").strip()
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return xdg
    home = (os.environ.get("HOME") or "").strip()
    return os.path.join(home, ".config") if home else ""


def app_config_dir() -> str:
    """ATRI-X 配置目录：Windows 为 ``%APPDATA%/ATRI-X``，其它平台按 XDG。"""
    base = _user_config_dir()
    name = APP_DIR_NAME if os.name == "nt" else XDG_APP_DIR_NAME
    if not base:
        # 极端环境（无 APPDATA/HOME）下退到当前目录，保证工具仍可运行并给出明确提示
        return os.path.abspath(name)
    return os.path.join(base, name)


def default_home() -> str:
    """研究数据根目录；``ATRIX_RESEARCH_HOME`` 优先。"""
    override = (os.environ.get(ENV_HOME) or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(app_config_dir(), "research")


@dataclass(frozen=True)
class ResearchPaths:
    """一次进程内使用的路径集合；``frozen`` 防止各工具半路改道。"""

    home: str

    @property
    def engine_dir(self) -> str:
        return os.path.join(self.home, "engine")

    @property
    def venv_dir(self) -> str:
        return os.path.join(self.home, "venv")

    @property
    def runs_dir(self) -> str:
        return os.path.join(self.home, "runs")

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.home, "cache")

    @property
    def http_cache_dir(self) -> str:
        return os.path.join(self.cache_dir, "http")

    @property
    def settings_path(self) -> str:
        return os.path.join(self.home, "settings.json")

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, safe_run_id(run_id))

    def ensure(self) -> "ResearchPaths":
        for directory in (self.home, self.runs_dir, self.http_cache_dir):
            os.makedirs(directory, exist_ok=True)
        return self


_CURRENT: ResearchPaths | None = None


def paths(home: str | None = None) -> ResearchPaths:
    """返回（并缓存）当前路径集合。"""
    global _CURRENT
    if home:
        return ResearchPaths(os.path.abspath(home))
    if _CURRENT is None:
        _CURRENT = ResearchPaths(default_home())
    return _CURRENT


def reset_cache() -> None:
    """清掉进程内缓存的路径（测试用）。"""
    global _CURRENT
    _CURRENT = None


def safe_run_id(value: str) -> str:
    """校验运行 id。

    运行 id 会拼进文件路径，必须白名单校验，拒绝 ``..``、路径分隔符与
    大小写混写（Windows 大小写不敏感，混写会产生"两个名字一个目录"）。
    """
    text = (value or "").strip().lower()
    if not _RUN_ID_RE.match(text):
        raise ResearchError(
            ARG_INVALID,
            f"非法的运行 id: {value!r}",
            hint="运行 id 只能包含 a-z、0-9 和连字符，长度 3-64；用 research_doctor 的 status 列出可用 id。",
        )
    return text


def resolve_run_id(value: str) -> str:
    """把 ``latest`` / 空值解析成具体运行 id。"""
    text = (value or "").strip().lower()
    if text and text != "latest":
        return safe_run_id(text)
    runs = list_runs()
    if not runs:
        raise ResearchError(
            "RUN_NOT_FOUND",
            "还没有任何研究运行记录",
            hint="先调用 research_plan 创建运行（action=init）。",
        )
    return runs[0]["id"]


def list_runs() -> list[dict[str, Any]]:
    """按更新时间倒序列出运行摘要（只读 run.json，坏文件跳过）。"""
    root = paths().runs_dir
    if not os.path.isdir(root):
        return []
    out: list[dict[str, Any]] = []
    for name in os.listdir(root):
        if not _RUN_ID_RE.match(name):
            continue
        state_path = os.path.join(root, name, "run.json")
        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": data.get("id", name),
                "title": data.get("title", ""),
                "question": data.get("question", ""),
                "stage": data.get("stage", ""),
                "language": data.get("language", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "dir": os.path.join(root, name),
            }
        )
    out.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    return out


@dataclass
class ResearchSettings:
    """settings.json 的只读视图；缺项按默认值补齐（前向兼容）。"""

    data: dict[str, Any] = field(default_factory=dict)
    origin: str = "default"

    def section(self, name: str) -> dict[str, Any]:
        base = DEFAULT_SETTINGS.get(name, {})
        merged = dict(base) if isinstance(base, dict) else {}
        current = self.data.get(name)
        if isinstance(current, dict):
            merged.update(current)
        return merged

    @property
    def mailto(self) -> str:
        value = (self.data.get("contact_mailto") or os.environ.get(ENV_MAILTO) or "").strip()
        return value


def load_settings() -> ResearchSettings:
    """读取 settings.json；不存在时返回默认值（不写盘，安装器负责落盘）。"""
    path = paths().settings_path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return ResearchSettings(dict(DEFAULT_SETTINGS), "default")
    except (OSError, ValueError) as exc:
        raise ResearchError(
            ENV_INVALID,
            f"settings.json 无法解析: {exc}",
            hint=f"修好或删除 {path}（删除后按默认值运行）。",
        ) from None
    if not isinstance(data, dict):
        raise ResearchError(
            ENV_INVALID,
            "settings.json 顶层必须是对象",
            hint=f"检查 {path}，应为 {{\"schema\": ..., \"http\": {{...}}}}。",
        )
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return ResearchSettings(merged, path)
