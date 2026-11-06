"""「内容安全」页的数据层（无 Tk）：读写 ``safety.toml``、词表、复核队列与审计。

这一层刻意不碰 tkinter，方便 `tests/test_gui_safety.py` 在无显示环境里直接断言：

* **策略文件**：``<配置目录>/safety.toml``。文件不存在 = 功能未启用（与后台
  :func:`atri_x.safety.settings.load_safety_settings` 的语义一致）；界面上的
  「创建默认配置」写一份带注释、默认只记不拦（``mode = "audit"``）的模板；
* **词表**：``<配置目录>/safety/wordlists/<类别>.txt``（一行一条）与
  ``<配置目录>/safety/sensitive/*.regex``（``名字 = /正则/flags``）；
* **复核队列**：``<配置目录>/safety/review.db``（与后台
  :class:`atri_x.safety.review_queue.ReviewQueue` 同一张表；本页只读不建库，
  库不存在时返回空，避免"打开界面就少一个文件"）；
* **审计**：``<配置目录>/safety-audit.jsonl``（每行一条 JSON，**不含原文**）。

保存策略沿用 :mod:`atri_x.gui.configedit` 的行级补丁：只改本页负责的键，
不重写整份文件、不丢注释。写完立刻用后台同一个解码器回读一遍，坏配置直接
抛 :class:`ConfigEditError` 给界面显示，而不是等重启后台服务才炸。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_toml,
    write_text_file,
)
from atri_x.safety import (
    SafetySettings,
    local_policy,
    load_safety_settings,
    safety_dirs,
    safety_file_path,
)
from atri_x.safety.settings import DEFAULT_AUDIT_FILE_NAME, DEFAULT_SAFETY_FILE
from atri_x.safety.rules import RuleSet, load_custom_patterns, load_user_wordlists

__all__ = [
    "DEFAULT_TEMPLATE",
    "LocalScanResult",
    "SafetyOverview",
    "WordlistFile",
    "audit_path",
    "create_default_safety_file",
    "create_list_file",
    "file_terms",
    "has_safety_file",
    "list_wordlist_files",
    "load_settings",
    "local_scan",
    "overview",
    "read_text_file",
    "review_db_path",
    "safety_path",
    "safety_text",
    "save_safety_sections",
    "save_safety_text",
    "scopes_dir",
    "sensitive_dir",
    "wordlists_dir",
    "write_list_file",
]

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def safety_path(paths: ConfigPaths) -> str:
    """``<配置目录>/safety.toml`` 的绝对路径（不保证存在）。"""
    return safety_file_path(paths.config_dir)


def has_safety_file(paths: ConfigPaths) -> bool:
    """策略文件是否存在（不存在 = 内容安全未启用，行为同 1.1.7）。"""
    return os.path.isfile(safety_path(paths))


def _dirs(paths: ConfigPaths, settings: SafetySettings) -> dict[str, str]:
    return safety_dirs(paths.config_dir, settings)


def wordlists_dir(paths: ConfigPaths, settings: SafetySettings) -> str:
    """用户词表目录（``[local].wordlist_dir`` 解析后的绝对路径）。"""
    return _dirs(paths, settings)["wordlists"]


def sensitive_dir(paths: ConfigPaths, settings: SafetySettings) -> str:
    """用户自定义敏感信息正则目录（``[local].sensitive_dir``）。"""
    return _dirs(paths, settings)["sensitive"]


def scopes_dir(paths: ConfigPaths, settings: SafetySettings) -> str:
    """按群 / 按工具策略目录（``safety/scopes/*.toml``，固定位置）。"""
    return _dirs(paths, settings)["scopes"]


def review_db_path(paths: ConfigPaths, settings: SafetySettings) -> str:
    """复核队列 SQLite 文件（``safety/review.db``）。"""
    return _dirs(paths, settings)["review_db"]


def audit_path(paths: ConfigPaths, settings: SafetySettings) -> str:
    """审计日志绝对路径；``[local].audit_path`` 留空时落配置根目录。"""
    configured = _dirs(paths, settings)["audit"]
    if configured:
        return configured
    return os.path.join(paths.config_dir, DEFAULT_AUDIT_FILE_NAME)


# ---------------------------------------------------------------------------
# 策略文件
# ---------------------------------------------------------------------------
def load_settings(paths: ConfigPaths) -> tuple[SafetySettings, list[str]]:
    """读 ``safety.toml``（缺文件 = 全默认、无错误）。"""
    return load_safety_settings(safety_path(paths))


def safety_text(paths: ConfigPaths) -> str:
    """读整份 ``safety.toml`` 原文（文件不存在返回空串）。"""
    return read_text_file(safety_path(paths))


def save_safety_text(paths: ConfigPaths, text: str) -> bool:
    """整份保存 ``safety.toml``（高级编辑用）；保存前用后台解码器校验。"""
    path = safety_path(paths)
    _validate_text(text, path)
    return write_text_file(path, text)


def save_safety_sections(
    paths: ConfigPaths, sections: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """按小节写策略（行级补丁），返回被改动的文件路径列表。

    ``sections`` 形如 ``{"local": {"enabled": True}, "remote": {...}}``；值为
    ``None`` 表示删除该键。写完整份文件用 :func:`save_safety_text`。
    """
    path = safety_path(paths)
    _validate_sections(path, sections)
    changed: list[str] = []
    for section, values in sections.items():
        clean = dict(values)
        if not clean:
            continue
        try:
            if patch_section(path, section, clean):
                changed.append(path)
        except ConfigEditError:
            raise
        except Exception as exc:  # noqa: BLE001 - 值类型不对时给出可读错误
            raise ConfigEditError(f"写入 [{section}] 失败：{exc}") from exc
    _validate_file(path)
    return changed


def _validate_sections(path: str, sections: Mapping[str, Mapping[str, Any]]) -> None:
    """写盘**之前**把"现有文件 + 本次改动"合起来解码一遍。

    先写后验会把非法值留在文件里（用户在界面上看到的是保存失败，但磁盘已经脏了）；
    这里先合并在内存里验证，类型错误直接拒绝，文件一个字节都不动。
    """
    merged = read_toml(path)
    for section, values in sections.items():
        node: dict[str, Any] = merged
        for part in section.split("."):
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        for key, value in values.items():
            if value is None:
                node.pop(key, None)
            else:
                node[key] = value
    unknown: list[str] = []
    try:
        SafetySettings.from_dict(merged, source=DEFAULT_SAFETY_FILE, unknown=unknown)
    except Exception as exc:  # noqa: BLE001 - 解码失败直接拒绝写入
        raise ConfigEditError(f"策略值不合法：{exc}") from exc


def _validate_text(text: str, path: str) -> None:
    """校验待写入的 TOML 文本能否被后台解码（写盘前）。"""
    if not text.strip():
        raise ConfigEditError("内容为空：safety.toml 至少要保留一个可解析的小节")
    import tomllib

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigEditError(f"{os.path.basename(path)} 不是合法的 TOML：{exc}") from exc
    errors = _decode_errors(text)
    if errors:
        raise ConfigEditError("；".join(errors))


def _validate_file(path: str) -> None:
    """回读整份文件：后台服务按当前代码能解码才允许保存成功。"""
    _settings, errors = load_safety_settings(path)
    if errors:
        raise ConfigEditError("；".join(errors))


def _decode_errors(text: str) -> list[str]:
    """用后台同一套解码器验证文本（``strict=False``：未知键忽略，类型错报错）。"""
    import tomllib

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [str(exc)]
    unknown: list[str] = []
    try:
        SafetySettings.from_dict(data, source=DEFAULT_SAFETY_FILE, unknown=unknown)
    except Exception as exc:  # noqa: BLE001 - 解码失败要报给界面
        return [str(exc)]
    return []


#: 默认策略模板：默认**只记不拦**（``mode = "audit"``），远程层关闭。
#: 用户确认要拦时把 ``mode`` 改成 ``enforce`` 即可；删除文件即整体回退。
DEFAULT_TEMPLATE = """\
# ATRI-X 内容安全策略（1.1.8）
# 保存后后台 ≤1 秒热加载；删除本文件 = 回退到 1.1.7 的 [safety] 行为。
# 默认只记录不拦截（audit）。确认词表/规则符合预期后把 mode 改成 "enforce"。
version = 1

[local]
enabled = true
mode = "audit"
normalize = true
max_scan_chars = 8000
wordlist_dir = "safety/wordlists"
sensitive_dir = "safety/sensitive"
audit_path = ""
audit_preview_chars = 120
block_image_output = false
block_file_output = false
strip_tool_images = false

[remote]
# 云厂商审核默认关闭；两家凭据都从 .env / 系统环境变量读，不写进本文件。
enabled = false
providers = ["tencent", "aliyun"]
quorum = "any_block"
timeout_ms = 800
retries = 1
fail_mode = "open"
fail_close_scopes = []
cache_ttl_seconds = 600
redact_before_submit = true

[tiers]
local_wordlist = "block"
local_sensitive_redact = "redact"
remote_block = "block"
remote_review = "review"
inbound_review = "pass_and_queue"
outbound_review = "send_and_queue"

[media]
images = "sync"
audio = "async"
video = "async"
image_max_bytes = 4194304
image_base64_max_bytes = 1048576
skip_actors = []

[cost]
daily_budget_cny = 0.0
on_budget_exceeded = "local_only"
text_price_cny_per_10k = 22.0
"""


def create_default_safety_file(
    paths: ConfigPaths, *, text: str = DEFAULT_TEMPLATE
) -> str:
    """写一份默认 ``safety.toml``（已存在时**不覆盖**，直接报错让用户先看）。"""
    path = safety_path(paths)
    if os.path.isfile(path):
        raise ConfigEditError(f"{DEFAULT_SAFETY_FILE} 已存在，请直接编辑或先备份")
    _validate_text(text, path)
    write_text_file(path, text)
    return path


# ---------------------------------------------------------------------------
# 词表 / 正则文件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WordlistFile:
    """一个用户词表（或自定义正则）文件。"""

    name: str
    path: str
    kind: str  # "wordlist" | "regex"
    entries: int


def read_text_file(path: str) -> str:
    """读一个 UTF-8 文本文件（不存在返回空串）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ConfigEditError(f"读取 {path} 失败：{exc}") from exc


def create_list_file(directory: str, name: str, text: str) -> str:
    """新建单个词表文件；拒绝路径与覆盖，独占创建防止同名竞态。"""
    if not name or name in (".", "..") or any(char in name for char in '/\\\\:'):
        raise ConfigEditError("文件名不能包含路径或盘符")
    root = os.path.realpath(directory)
    path = os.path.join(root, name)
    if os.path.commonpath((root, os.path.realpath(path))) != root:
        raise ConfigEditError("词表必须位于指定目录内")
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise ConfigEditError(f"新建词表失败：{exc}") from exc
    return path


def write_list_file(path: str, text: str) -> bool:
    """写一个词表 / 正则文件（自动建父目录；内容没变不落盘）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ConfigEditError(f"创建目录 {parent} 失败：{exc}") from exc
    return write_text_file(path, text)


def file_terms(path: str) -> list[str]:
    """词表文件里的有效词条（``#`` 注释与空行忽略，去重保序）。"""
    out: list[str] = []
    for line in read_text_file(path).splitlines():
        term = line.strip()
        if not term or term.startswith("#") or term in out:
            continue
        out.append(term)
    return out


def list_wordlist_files(paths: ConfigPaths, settings: SafetySettings) -> list[WordlistFile]:
    """列出用户词表目录（``.txt``）与敏感信息正则目录（``.regex``）。"""
    out: list[WordlistFile] = []
    for directory, suffix, kind in (
        (wordlists_dir(paths, settings), ".txt", "wordlist"),
        (sensitive_dir(paths, settings), ".regex", "regex"),
    ):
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            if not filename.lower().endswith(suffix):
                continue
            path = os.path.join(directory, filename)
            if not os.path.isfile(path):
                continue
            count = len(file_terms(path)) if kind == "wordlist" else _regex_count(path)
            out.append(WordlistFile(name=filename, path=path, kind=kind, entries=count))
    return out


def _regex_count(path: str) -> int:
    count = 0
    for line in read_text_file(path).splitlines():
        text = line.strip()
        if text and not text.startswith("#") and "=" in text:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 本地规则试扫（离线自检）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LocalScanResult:
    """一次本地试扫的结果（只用于界面自检，不写审计）。"""

    text: str
    hits: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]
    enabled: bool
    mode: str

    @property
    def blocked(self) -> bool:
        return any(hit.get("action") == "block" for hit in self.hits)


def local_scan(
    paths: ConfigPaths, settings: SafetySettings, text: str
) -> LocalScanResult:
    """用当前策略的**本地规则层**试扫一段文本（不联网、不写审计）。"""
    policy = local_policy(settings)
    rules = RuleSet(
        policy,
        extra_terms=load_user_wordlists(wordlists_dir(paths, settings)),
        custom_patterns=load_custom_patterns(sensitive_dir(paths, settings)),
        normalize=settings.local.normalize,
    )
    processed, hits = rules.inspect(text or "")
    rows = tuple(
        {
            "category": hit.category,
            "label": hit.label,
            "action": hit.action,
            "severity": hit.severity,
            "match": hit.match,
        }
        for hit in hits
    )
    return LocalScanResult(
        text=processed,
        hits=rows,
        warnings=tuple(rules.warnings),
        enabled=bool(settings.local.enabled),
        mode=settings.local.mode,
    )


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SafetyOverview:
    """「内容安全」页顶部的状态摘要。"""

    file_exists: bool
    local_enabled: bool
    local_mode: str
    remote_enabled: bool
    providers: tuple[str, ...]
    quorum: str
    fail_mode: str
    review_pending: int
    review_total: int
    audit_records: int
    last_audit_at: str
    wordlist_files: int
    wordlist_terms: int
    errors: tuple[str, ...]


def overview(paths: ConfigPaths) -> SafetyOverview:
    """汇总当前配置与数据文件状态（缺文件全部按默认值展示）。"""
    from atri_x.gui import safetyrecords
    from atri_x.safety.review_queue import STATUS_PENDING

    settings, errors = load_settings(paths)
    records = (
        safetyrecords.read_audit(paths, settings, limit=1) if has_safety_file(paths) else []
    )
    last_at = str(records[-1].get("ts") or "") if records else ""
    stats = safetyrecords.review_stats(paths, settings)
    files = list_wordlist_files(paths, settings)
    return SafetyOverview(
        file_exists=has_safety_file(paths),
        local_enabled=bool(settings.local.enabled),
        local_mode=str(settings.local.mode or "audit"),
        remote_enabled=bool(settings.remote.enabled),
        providers=tuple(str(item) for item in settings.remote.providers),
        quorum=str(settings.remote.quorum or ""),
        fail_mode=str(settings.remote.fail_mode or "open"),
        review_pending=int(stats.get(STATUS_PENDING, 0)),
        review_total=int(stats.get("total", 0)),
        audit_records=_audit_count(paths, settings),
        last_audit_at=last_at,
        wordlist_files=len(files),
        wordlist_terms=sum(item.entries for item in files if item.kind == "wordlist"),
        errors=tuple(errors),
    )


def _audit_count(paths: ConfigPaths, settings: SafetySettings) -> int:
    path = audit_path(paths, settings)
    if not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 供界面下拉框用的常量
# ---------------------------------------------------------------------------
#: 本地模式下拉项（值 → 中文说明）。
LOCAL_MODES: tuple[tuple[str, str], ...] = (
    ("off", "off：完全关闭"),
    ("audit", "audit：只记录不拦截"),
    ("enforce", "enforce：真正拦截"),
)

#: 档位映射项（值 → 中文说明）。
TIER_ACTIONS: tuple[tuple[str, str], ...] = (
    ("block", "block：拦截"),
    ("review", "review：进人工复核"),
    ("pass", "pass：放行"),
)

#: 复核处置项。
REVIEW_ACTIONS: tuple[tuple[str, str], ...] = (
    ("pass_and_queue", "照发 + 入复核队列"),
    ("hold", "先压住，等复核"),
    ("send_and_queue", "照发 + 入队列（发送侧）"),
)

#: 阶段远程策略项。
STAGE_POLICY_CHOICES: tuple[tuple[str, str], ...] = (
    ("sync", "sync：同步送审"),
    ("cache_only", "cache_only：只用缓存"),
    ("local_only", "local_only：只走本地"),
    ("off", "off：不送审"),
)

#: 失败策略项。
FAIL_MODES: tuple[tuple[str, str], ...] = (
    ("open", "open：放行 + 告警（推荐默认）"),
    ("closed", "closed：拦截（高敏群）"),
)

#: 云厂商多供应商仲裁项。
QUORUMS: tuple[tuple[str, str], ...] = (
    ("any_block", "any_block：任一家拦截就拦"),
    ("both_pass", "both_pass：两家都放行才放行"),
    ("primary_only", "primary_only：只看第一家"),
)

#: 允许写入 ``[remote.stages]`` 的阶段名（与后台 STAGES 一致）。
KNOWN_STAGES: tuple[str, ...] = (
    "inbound",
    "llm",
    "send",
    "tool",
    "image_prompt",
    "image",
    "audio",
    "video",
)

#: 阶段名 → 中文名（界面上更好读）。
STAGE_LABELS: dict[str, str] = {
    "inbound": "入站消息",
    "llm": "模型输出",
    "send": "发送出口",
    "tool": "工具结果",
    "image_prompt": "生图提示词",
    "image": "图片",
    "audio": "语音",
    "video": "视频",
}
