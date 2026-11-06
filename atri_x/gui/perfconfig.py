"""「性能 / 并发」页的数据层（无 Tk）：读写并发上限，拉后台 ``/metrics`` 实况。

分层与「在线」「时间」两页一致：数据在本模块，界面在
:mod:`atri_x.gui.tabs.performance`，于是"预设合不合理、写盘会不会顺手破坏别的键、
``/metrics`` 字段缺了会不会炸界面"都能脱离 Tk 单测。

三块内容：

* **预设**（:data:`PRESETS`）：省资源 / 标准 / 高并发。标准 = 出厂默认值，什么都不配
  就是这一套；另两套把并发收紧到单用户、或放大到多人多群；
* **读写**：``[ops]``（并发 + 排队）与 ``[platform.qqonebot]``（入站预处理池）写在
  ``app.toml``，``[image_generation]``（生图闸门）写在 ``services.toml``。界面只动这三处：
  QQ 段**只写三个池参数、绝不写 ``enabled``**（写它等于替用户打开平台），
  ``[ops].queue_wait_kinds`` 等其它键原样保留；
* **实况**：``GET http://<ATRIX_HEALTH_ADDR>/metrics``（本机回环 + Bearer 令牌）。payload
  就是 :class:`atri_x.app.ops_health.OpsMetrics` 的 JSON，``tasks`` 是
  :class:`atri_x.request.Snapshot` 的字段（``count_by_kind`` / ``queued_by_kind`` /
  ``oldest_age`` / ``active`` …），``dsh`` 是 DSH 深度路由的并发/忙拒/卡死/亲和统计
  （1.1.8 新增，旧后台缺这段时整段跳过）。这些字段带 Go 的 ``omitempty`` 语义：零值整个
  不出现，所以每个字段都必须允许缺失，缺了显示占位符而不是抛异常。

并发口径（界面文案别误导用户）：ATRI-X 的并发是 asyncio 协程，不是多进程。不同群 /
私聊的回合本来就在同一个事件循环里并行；真正串行的是**同一个群的会话**
（thread-mode group，按回合排队）。这里调的是"同时在跑多少"与"满了排多久"，既不会
用上更多 CPU 核心，也不会让单个回合变快。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from atri_x.gui import timeview
from atri_x.gui.configedit import (
    ConfigEditError,
    ConfigPaths,
    patch_section,
    read_toml,
    verify_config,
)

__all__ = [
    "FIELD_KEYS", "GROUPS", "METRICS_PATH", "PRESETS", "PRESET_HIGH", "PRESET_LOW",
    "PRESET_NORMAL", "PerfConfig", "PerfConfigError", "VALUE_MAX", "VALUE_MIN",
    "active_request_rows", "fetch_perf_metrics", "format_duration", "metrics_endpoint",
    "metrics_summary", "read_perf_config", "save_perf_config",
]

PRESET_LOW = "省资源（单用户）"
PRESET_NORMAL = "标准（默认）"
PRESET_HIGH = "高并发（多人/多群）"

#: 三个预设共用的 11 个键（也是界面表单的顺序：LLM/回合 → 生图 → QQ 入站）。
FIELD_KEYS: tuple[str, ...] = (
    "max_concurrent_turns", "max_concurrent_tools", "max_concurrent_hooks",
    "provider_max_concurrent", "queue_max_size", "queue_wait_timeout_seconds",
    "image_max_concurrent", "image_queue_size",
    "preprocess_workers", "preprocess_queue_size", "high_priority_workers",
)

#: 取值下限；0 在这些键里统一表示"不限制"或"用内置默认"。
VALUE_MIN = 0

#: 取值上限：并发不是配额，手滑多打一个 0 也没意义，校验与界面都收在 512。
VALUE_MAX = 512

PRESETS: dict[str, dict[str, int]] = {
    # 单用户桌面机：两个回合槽够用，排队容忍度低（20 秒），生图与入站各留 1~2 个槽。
    PRESET_LOW: {
        "max_concurrent_turns": 2, "max_concurrent_tools": 2, "max_concurrent_hooks": 2,
        "provider_max_concurrent": 2, "queue_max_size": 8, "queue_wait_timeout_seconds": 20,
        "image_max_concurrent": 1, "image_queue_size": 2,
        "preprocess_workers": 2, "preprocess_queue_size": 64, "high_priority_workers": 1,
    },
    # 出厂 app.toml / services.toml 的值：turns=10 面向十位以上用户；生图与入站预处理
    # 用各自的内置默认（0 = 不额外限流 / 用默认池）。
    PRESET_NORMAL: {
        "max_concurrent_turns": 10, "max_concurrent_tools": 10, "max_concurrent_hooks": 10,
        "provider_max_concurrent": 10, "queue_max_size": 20, "queue_wait_timeout_seconds": 30,
        "image_max_concurrent": 0, "image_queue_size": 0,
        "preprocess_workers": 4, "preprocess_queue_size": 256, "high_priority_workers": 1,
    },
    # 多人多群：回合 32 / 工具 32 / Hook 16。Provider 16 **低于**回合上限是有意的 ——
    # 超额只会把并发压力换成 Provider 的 429。排队 64 条等 60 秒；生图 8 槽 + 16 排队；
    # 入站预处理 8 线程 + 512 队列。
    PRESET_HIGH: {
        "max_concurrent_turns": 32, "max_concurrent_tools": 32, "max_concurrent_hooks": 16,
        "provider_max_concurrent": 16, "queue_max_size": 64, "queue_wait_timeout_seconds": 60,
        "image_max_concurrent": 8, "image_queue_size": 16,
        "preprocess_workers": 8, "preprocess_queue_size": 512, "high_priority_workers": 2,
    },
}

#: 三组键按 ``FIELD_KEYS`` 的顺序切开：``[ops]`` / ``[image_generation]`` /
#: ``[platform.qqonebot]``。保存时每组只写自己的键，别的键（``queue_wait_kinds`` 等）不动。
_OPS_KEYS, _IMAGE_KEYS, _QQ_KEYS = FIELD_KEYS[:6], FIELD_KEYS[6:8], FIELD_KEYS[8:]

#: 界面上的三组表单：``(组名, 该组的键)``。
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("LLM / 回合（app.toml 的 [ops]）", _OPS_KEYS),
    ("生图（services.toml 的 [image_generation]）", _IMAGE_KEYS),
    ("QQ 入站（app.toml 的 [platform.qqonebot]）", _QQ_KEYS),
)

#: 界面键 → ``[image_generation]`` 里的真实键名。
_IMAGE_TOML_NAMES = {"image_max_concurrent": "max_concurrent", "image_queue_size": "queue_size"}

#: 后台只读并发实况接口的路径。
METRICS_PATH = "/metrics"

#: ``0.0.0.0`` 这类"监听地址"要映射成本机能连的地址（与 :mod:`atri_x.gui.timeview` 同口径）。
_WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "*")


class PerfConfigError(Exception):
    """本页的可预期失败（消息直接展示给用户）。"""


@dataclass
class PerfConfig:
    """一张表单的全部值 + 「原来那份配置里有没有这两个小节」的提示位。

    默认值就是 :data:`PRESETS` 里的「标准」，与出厂 ``app.toml`` / ``services.toml``
    一致；配置文件缺失、读不动、某个键非法时都回落到这里。
    """

    max_concurrent_turns: int = 10
    max_concurrent_tools: int = 10
    max_concurrent_hooks: int = 10
    provider_max_concurrent: int = 10
    queue_max_size: int = 20
    queue_wait_timeout_seconds: int = 30
    #: 生图独立并发（``services.toml`` 的 ``[image_generation]``；0 = 不限制）。
    image_max_concurrent: int = 0
    image_queue_size: int = 0
    preprocess_workers: int = 4
    preprocess_queue_size: int = 256
    high_priority_workers: int = 1
    #: ``[image_generation]`` / ``[platform.qqonebot]`` 原来在不在（不在时保存会新建）。
    image_section_present: bool = False
    qq_section_present: bool = False

    def values(self) -> dict[str, int]:
        """只取 11 个数值键（保存 / 预设回填用，去掉两个提示位）。"""
        return {key: int(getattr(self, key)) for key in FIELD_KEYS}


# --- 读 --------------------------------------------------------------------
def read_perf_config(paths: ConfigPaths | None) -> PerfConfig:
    """读三个小节，逐键回落默认值。

    缺失、读不动（TOML 坏了）、类型不对、越界的键**逐个**回落到「标准」预设的值：
    一个键写错就让整张表显示 0 的话，用户会以为并发被关掉了。
    """
    if paths is None:
        return PerfConfig()
    try:
        app = read_toml(paths.config_path)
    except ConfigEditError:
        app = {}
    try:
        services = read_toml(paths.services_path) if paths.services_path else {}
    except ConfigEditError:
        services = {}

    ops, platform = _section(app, "ops"), _section(app, "platform")
    qq, image = _section(platform, "qqonebot"), _section(services, "image_generation")
    with_image = _has_section(services, "image_generation")
    if not with_image and _has_section(app, "image_generation"):
        # 旧部署会把 [image_generation] 写回 app.toml（见 assets_data/app.toml 的说明）；
        # 两份都有时以 services 为准。
        image, with_image = _section(app, "image_generation"), True

    default = PRESETS[PRESET_NORMAL]
    return PerfConfig(
        max_concurrent_turns=_number(ops, "max_concurrent_turns", default),
        max_concurrent_tools=_number(ops, "max_concurrent_tools", default),
        max_concurrent_hooks=_number(ops, "max_concurrent_hooks", default),
        provider_max_concurrent=_number(ops, "provider_max_concurrent", default),
        queue_max_size=_number(ops, "queue_max_size", default),
        queue_wait_timeout_seconds=_number(ops, "queue_wait_timeout_seconds", default),
        image_max_concurrent=_number(image, "max_concurrent", default, key="image_max_concurrent"),
        image_queue_size=_number(image, "queue_size", default, key="image_queue_size"),
        preprocess_workers=_number(qq, "preprocess_workers", default),
        preprocess_queue_size=_number(qq, "preprocess_queue_size", default),
        high_priority_workers=_number(qq, "high_priority_workers", default),
        image_section_present=with_image,
        qq_section_present=_has_section(platform, "qqonebot"),
    )


def _section(table: Any, name: str) -> Mapping[str, Any]:
    """取子表；不是表就当空表（坏配置不该让整页读不出来）。"""
    value = table.get(name) if isinstance(table, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _has_section(table: Any, name: str) -> bool:
    """该小节在文件里**存在**吗（哪怕内容为空表）。"""
    return isinstance(table, Mapping) and isinstance(table.get(name), Mapping)


def _number(
    table: Mapping[str, Any], toml_key: str, default: Mapping[str, int], *, key: str = ""
) -> int:
    """取一个非负整数键；缺失 / 非整数 / 越界都回落 ``default``（``key`` 是界面键名）。"""
    field = key or toml_key
    value = table.get(toml_key) if isinstance(table, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return int(default[field])
    if not VALUE_MIN <= value <= VALUE_MAX:
        return int(default[field])
    return value


# --- 写 --------------------------------------------------------------------
def save_perf_config(paths: ConfigPaths, values: Mapping[str, int]) -> None:
    """把一套并发参数写进三个小节，写完立刻回读校验。

    * ``app.toml`` 的 ``[ops]``：六个并发 / 排队键（``queue_wait_kinds`` 等其它键不动）；
    * ``app.toml`` 的 ``[platform.qqonebot]``：**只写** ``preprocess_workers`` /
      ``preprocess_queue_size`` / ``high_priority_workers``。小节不存在时新建，但不写
      ``enabled``，所以保存不会把 QQ 平台打开；
    * ``[image_generation]``：``max_concurrent`` / ``queue_size``。默认写 ``services.toml``，
      当前用的是旧 ``providers.toml`` 时写回 ``app.toml``（旧部署的口径）——两边都不写
      才是"用户填了却不生效"。

    越界（负数、> :data:`VALUE_MAX`）、缺键、非整数一律抛 :class:`PerfConfigError`；
    底层 :class:`ConfigEditError` 也统一换成它，界面只需要处理一种异常。
    """
    clean = _validated(values)
    if paths is None or not paths.config_path or not os.path.exists(paths.config_path):
        raise PerfConfigError("找不到 app.toml，无法保存并发配置")
    try:
        patch_section(paths.config_path, "ops", {key: clean[key] for key in _OPS_KEYS})
        patch_section(
            paths.config_path,
            "platform.qqonebot",
            {key: clean[key] for key in _QQ_KEYS},
            header_comment=(
                "「性能」页写入的入站预处理池参数。\n"
                "只写这三个键，不写 enabled —— 不会因此启用 QQ 平台。"
            ),
        )
        patch_section(
            _image_target(paths),
            "image_generation",
            {name: clean[key] for key, name in _IMAGE_TOML_NAMES.items()},
            header_comment="生图独立并发限制；0 表示不限制（由「性能」页写入）。",
        )
        verify_config(paths)
    except ConfigEditError as exc:
        raise PerfConfigError(str(exc)) from exc


def _image_target(paths: ConfigPaths) -> str:
    """``[image_generation]`` 该写哪个文件：正式 ``services.toml``，或旧部署的 ``app.toml``。

    旧的 ``providers.toml`` 不承载 ``[image_generation]``（严格解码还会把它当未知键），
    所以只在 ``services.toml`` 就位时才写它。
    """
    services = paths.services_path
    if services and os.path.exists(services) and not paths.services_is_legacy_providers:
        return services
    return paths.config_path


def _validated(values: Mapping[str, int]) -> dict[str, int]:
    """校验 11 个键（缺键 / 非整数 / 越界都抛中文错误）。"""
    missing = [key for key in FIELD_KEYS if key not in values]
    if missing:
        raise PerfConfigError("缺少这些并发参数：" + "、".join(missing))
    clean: dict[str, int] = {}
    for key in FIELD_KEYS:
        raw = values[key]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise PerfConfigError(f"{key} 必须是整数（现在是 {raw!r}）")
        if not VALUE_MIN <= raw <= VALUE_MAX:
            raise PerfConfigError(f"{key} 必须落在 {VALUE_MIN}~{VALUE_MAX} 之间（现在是 {raw}）")
        clean[key] = raw
    return clean


# --- 后台实况：``/metrics`` -------------------------------------------------
def metrics_endpoint(addr: str) -> str:
    """``127.0.0.1:32171`` → ``http://127.0.0.1:32171/metrics``（监听地址映射成本机）。"""
    text = (addr or "").strip()
    if not text:
        raise PerfConfigError(
            f"未配置后台状态接口地址（.env 的 {timeview.HEALTH_ADDR_ENV}）；"
            "桌面端启动时会自动补齐"
        )
    return timeview.local_status_endpoint(text, METRICS_PATH, PerfConfigError)


def fetch_perf_metrics(paths: ConfigPaths | None, *, timeout: float = 2.5) -> dict[str, Any]:
    """拉一次后台 ``/metrics``，返回解析后的 dict（永不返回非 dict）。

    地址与令牌沿用 :func:`atri_x.gui.timeview.diagnostics_credentials`；显式禁用代理：
    要访问的是本机回环地址，走系统代理只会超时或 407。
    """
    if paths is None:
        raise PerfConfigError("找不到配置文件，读不到状态接口地址")
    addr, token = timeview.diagnostics_credentials(paths)
    if not addr:
        raise PerfConfigError(
            f"未配置后台状态接口地址（.env 的 {timeview.HEALTH_ADDR_ENV}）；"
            "桌面端启动时会自动补齐"
        )
    if not token:
        raise PerfConfigError(
            f"缺少后台状态接口令牌（.env 的 {timeview.HEALTH_TOKEN_ENV}）；"
            "桌面端启动时会自动补齐"
        )
    request = urllib.request.Request(
        metrics_endpoint(addr),
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), timeview.LocalStatusRedirectHandler())
    try:
        with opener.open(request, timeout=max(0.2, float(timeout))) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        try:
            exc.read()
        except OSError:  # 读错误体失败不该盖住真正的状态码
            pass
        finally:
            exc.close()
        if code in (401, 403):
            raise PerfConfigError(
                "状态接口鉴权失败：请检查 .env 的 ATRIX_OPS_TOKEN 与后台服务是否一致"
            ) from exc
        if code == 404:
            raise PerfConfigError(
                "后台没有注册 /metrics（旧版本，或没配 ATRIX_OPS_TOKEN）"
            ) from exc
        raise PerfConfigError(f"状态接口返回 HTTP {code}") from exc
    except urllib.error.URLError as exc:
        raise PerfConfigError(
            f"后台状态接口不可达：{getattr(exc, 'reason', exc)}（后台服务没启动？）"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise PerfConfigError(f"后台状态接口不可达：{exc}（后台服务没启动？）") from exc
    try:
        body = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerfConfigError(f"后台状态接口返回了无法解析的内容：{exc}") from exc
    if not isinstance(body, Mapping):
        raise PerfConfigError("后台 /metrics 返回的不是 JSON 对象")
    return dict(body)


# --- 实况 → 可读行 ---------------------------------------------------------
#: 固定展示的活跃 kind：``count_by_kind`` 的零值会被 omitempty 吞掉，但"现在 0 个 turn
#: 在跑"本身是有效信息。
_ALWAYS_KINDS = ("turn", "tool", "hook")


def metrics_summary(payload: Any) -> list[tuple[str, str]]:
    """``/metrics`` → ``[(标签, 值)]``。任何字段缺失 / 类型不对都不抛异常。"""
    data = payload if isinstance(payload, Mapping) else {}
    tasks, rate = _section(data, "tasks"), _section(data, "rate_limit")
    image = _section(data, "image_limit")
    rows: list[tuple[str, str]] = [
        ("版本", _text(data.get("version")) or "未知"),
        ("采集时间", _text(data.get("collected_at")) or "—"),
        ("运行时长", format_duration(data.get("uptime_seconds"))),
    ]
    rows += _kind_rows(_section(tasks, "count_by_kind"), "活跃", _ALWAYS_KINDS)
    rows += _kind_rows(_section(tasks, "queued_by_kind"), "排队", _ALWAYS_KINDS)
    rows += [
        ("并发满被拒", str(_num(tasks.get("queue_busy_rejected")))),
        ("队满被拒", str(_num(tasks.get("queue_full_rejected")))),
        ("排队超时", str(_num(tasks.get("queue_timeout_rejected")))),
        ("排队取消", str(_num(tasks.get("queue_cancel_rejected")))),
        ("已放行排队", str(_num(tasks.get("queue_admitted")))),
        ("平均排队等待", _ms_text(tasks.get("queue_average_wait_ms"))),
        ("最老排队等待", format_duration(tasks.get("oldest_queued_wait"))),
        ("最老运行中请求", format_duration(tasks.get("oldest_age"))),
        ("请求超时计数", str(_num(tasks.get("timeouts")))),
        ("生图闸门", f"active {_num(image.get('active'))} / waiting {_num(image.get('waiting'))}"),
        ("Provider 限速", _rate_limit_text(rate)),
    ]
    rows += _dsh_rows(_section(data, "dsh"))
    rows += [
        ("平台", _names_text(data.get("platforms"))),
        ("模型", _count_text(data.get("models"))),
    ]
    return rows


def _dsh_rows(dsh: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``/metrics`` 的 ``dsh`` 段 → 可读行（旧后台没有该段时一行都不加）。"""
    if not isinstance(dsh, Mapping) or not dsh:
        return []
    if not dsh.get("enabled"):
        return [("DSH 深度路由", "未启用")]
    stats = _section(dsh, "stats")
    affinity = _section(dsh, "affinity")
    credential = "可用" if dsh.get("credentialUsable") else "不可用"
    provider = _text(dsh.get("credentialProvider"))
    if provider:
        credential += f"（{provider}）"
    reason = _text(dsh.get("credentialReason"))
    if reason:
        credential += f"：{reason}"
    rows = [
        ("DSH 深度路由", "已启用（内核可用）" if dsh.get("available") else "已启用（内核不可用）"),
        ("DSH 运行状态", "运行中" if dsh.get("running") else "未拉起"),
        ("DSH 凭据协议", credential),
        (
            "DSH 并发",
            f"active {_num(dsh.get('active'))} / max {_num(dsh.get('maxConcurrent'))}"
            f"（队列等待 {format_duration(dsh.get('busyWaitSeconds'))}）",
        ),
        (
            "DSH 忙拒 / 卡死",
            f"{_num(stats.get('busy_rejected'))} / {_num(stats.get('stalled'))}",
        ),
        (
            "DSH 完成 / 失败",
            f"{_num(stats.get('completed'))} / {_num(stats.get('failed'))}",
        ),
        (
            "DSH 冷启动 / 空闲回收",
            f"{_num(stats.get('cold_starts'))} / {_num(stats.get('idle_closes'))}",
        ),
    ]
    if dsh.get("credentialGateway"):
        gateway_headers = "、".join(
            _text(name) for name in (dsh.get("gatewayHeaderNames") or []) if _text(name)
        )
        rows.append(
            (
                "DSH 网关头代理",
                ("运行中" if dsh.get("gatewayRunning") else "未启动")
                + (f"（补 {gateway_headers}）" if gateway_headers else ""),
            )
        )
    if dsh.get("affinityEnabled"):
        rows.append(
            (
                "DSH 会话亲和",
                f"{_num(affinity.get('entries'))} / {_num(affinity.get('maxEntries'))} 个"
                f"（命中 {_num(affinity.get('hits'))}）",
            )
        )
    rows.append(("DSH 预热", "已预热" if dsh.get("prewarmed") else "未预热"))
    return rows


def active_request_rows(payload: Any) -> list[dict[str, Any]]:
    """``tasks.active`` → 表格行：``kind`` / ``session_id`` / ``fair_key`` / ``running`` / ``id``。

    ``running`` 是"已运行秒数"（``started_at`` 缺失或解析不了时为 ``None``），``id`` 只留
    前 8 位：表格里放全 UUID 会把别的列挤掉，排障时按前缀够对上日志。
    """
    active = _section(payload if isinstance(payload, Mapping) else {}, "tasks").get("active")
    if not isinstance(active, (list, tuple)):
        return []
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for item in active:
        entry = item if isinstance(item, Mapping) else {}
        started = _parse_time(entry.get("started_at"))
        rows.append(
            {
                "kind": _text(entry.get("kind")),
                "session_id": _text(entry.get("session_id")),
                "fair_key": _text(entry.get("fair_key")),
                "running": None
                if started is None
                else round(max(0.0, (now - started).total_seconds()), 1),
                "id": _text(entry.get("id"))[:8],
            }
        )
    return rows


def format_duration(seconds: Any) -> str:
    """秒数 → 中文时长（``"2 小时 3 分"`` / ``"12 分 4 秒"`` / ``"7 秒"``）。"""
    total = _int_or_none(seconds)
    if total is None:
        return "—"
    hours, rest = divmod(max(0, total), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


# --- 小工具 ---------------------------------------------------------------
def _kind_rows(table: Mapping[str, Any], prefix: str, always: tuple[str, ...]) -> list[tuple[str, str]]:
    """``{kind: 计数}`` → 行；``always`` 里的 kind 缺失时也显示 0，其余按名排序。"""
    rows = [(f"{prefix} {kind}", str(_num(table.get(kind)))) for kind in always]
    rows.extend(
        (f"{prefix} {key}", str(_num(table.get(key))))
        for key in sorted(str(name) for name in table if name not in always)
    )
    return rows


def _rate_limit_text(rate: Mapping[str, Any]) -> str:
    """限速统计：区分"没配限流"（rejected=0 不代表没被拒）与"配了但零拒绝"。"""
    text = f"放行 {_num(rate.get('allowed'))} / 拒绝 {_num(rate.get('rejected'))}"
    if rate.get("configured"):
        return text + "（已配置）"
    reason = _text(rate.get("disabled_reason"))
    return text + (f"（未启用限流：{reason}）" if reason else "（未启用限流）")


def _ms_text(value: Any) -> str:
    """毫秒字段（``queue_average_wait_ms``）。"""
    number = _int_or_none(value)
    return "—" if number is None else f"{number} ms"


def _names_text(value: Any) -> str:
    """平台名之类的小列表 → ``"qqonebot、cli"``（元素是 dict 时取 name/platform/id）。"""
    if not isinstance(value, (list, tuple)) or not value:
        return "—"
    names = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("platform") or item.get("id") or "?"
        names.append(_text(item) or "?")
    return "、".join(str(name) for name in names)


def _count_text(value: Any) -> str:
    """模型列表 → ``"2 个"``（``omitempty`` 让空列表整个消失，那时是 ``"—"``）。"""
    return f"{len(value)} 个" if isinstance(value, (list, tuple, Mapping)) else "—"


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _num(value: Any) -> int:
    number = _int_or_none(value)
    return 0 if number is None else max(0, number)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
