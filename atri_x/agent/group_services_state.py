"""群服务（提醒 / 投票 / 报名）的快照、开关与落库。

对应 Go：``internal/agent/group_services_state.go``。

职责：``cloneGroupReminders`` / ``cloneGroupPolls`` / ``cloneGroupSignups`` /
``setGroupServicesSnapshot`` / ``groupServicesSnapshot`` / 各 ``*ForScope`` 读写 /
``groupServicesEnabledForScope`` / ``requireGroupServices`` / ``withGroupServicesWrite`` /
``normalizeGroupServicesSnapshot`` / ``parseGroupServiceTime`` /
``formatGroupServiceTime`` / ``groupServiceDurationText`` / ``nextGroupServiceID``。

与 Go 的差异（形式，不是简化）：

* ``sync.RWMutex`` → 宿主上的 ``group_services_mu``；写路径另有一把
  ``group_services_write_mu``（Go ``groupServicesWriteMu``）。三者都指向宿主上按
  Go 字段名命名的属性，避免出现第二份互不相认的服务表。
* Go 的 ``withGroupServicesWrite(fn)`` 是"回调 + 失败回滚"；Python 侧保留同一形状，
  但回调可以是同步函数（内部同步调用，与 Go 相同）。
* 时间统一用带时区的 ``datetime``：``parse_group_service_time`` 对"无时区"的
  ``YYYY-MM-DD HH:MM:SS`` 按本地时区解释（与 Go ``time.ParseInLocation(..., time.Local)``
  同义），其余按 RFC3339 解析。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from atri_x.config import (
    GroupPollConfig,
    GroupReminderConfig,
    GroupServicesConfig,
    GroupSignupConfig,
    StateGroupServicesConfig,
)
from atri_x.errors import InvalidError

from atri_x.agent.group_policy import context_overflow_key, group_policy_for_scope, is_group_scope
from atri_x.agent.tools import rune_len, service_host, truncate_runes

__all__ = [
    "GroupServicesState",
    "check_reminder_when",
    "clone_group_polls",
    "clone_group_reminders",
    "clone_group_signups",
    "format_group_service_time",
    "group_polls_for_scope",
    "group_reminders_for_scope",
    "group_service_duration_text",
    "group_services_config",
    "group_services_enabled_for_scope",
    "group_services_snapshot",
    "group_services_state",
    "group_signups_for_scope",
    "next_group_service_id",
    "normalize_group_services_snapshot",
    "parse_group_service_time",
    "parse_relative_reminder",
    "parse_reminder_when",
    "require_group_services",
    "set_group_polls_for_scope",
    "set_group_reminders_for_scope",
    "set_group_services_snapshot",
    "set_group_signups_for_scope",
    "with_group_services_write",
]


@dataclass
class GroupServicesState:
    """群服务视图。对应 Go ``Agent.groupReminders/Polls/Signups`` + 两把锁。"""

    reminders: dict[str, list[GroupReminderConfig]] = field(default_factory=dict)
    polls: dict[str, list[GroupPollConfig]] = field(default_factory=dict)
    signups: dict[str, list[GroupSignupConfig]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)


def group_services_state(agent: Any) -> GroupServicesState:
    """取宿主上的群服务视图（缺失的属性按 Go 字段名就地创建）。"""
    host = service_host(agent)
    return GroupServicesState(
        reminders=_bucket(host, "group_reminders"),
        polls=_bucket(host, "group_polls"),
        signups=_bucket(host, "group_signups"),
        lock=_lock(host, "group_services_mu"),
        write_lock=_lock(host, "group_services_write_mu"),
    )


def _bucket(host: Any, name: str) -> dict[str, list[Any]]:
    value = getattr(host, name, None)
    if not isinstance(value, dict):
        value = {}
        _safe_set(host, name, value)
    return value


def _lock(host: Any, name: str) -> threading.Lock:
    value = getattr(host, name, None)
    if value is None:
        value = threading.Lock()
        _safe_set(host, name, value)
    return value


def _safe_set(target: Any, name: str, value: Any) -> bool:
    try:
        setattr(target, name, value)
        return True
    except AttributeError:
        return False


def group_services_config(agent: Any) -> GroupServicesConfig:
    """宿主上的群服务配置（``groupServicesCfg``）。缺失时就地建默认值。"""
    host = service_host(agent)
    config = getattr(host, "group_services_cfg", None)
    if config is None:
        services = getattr(host, "services", None)
        if isinstance(services, dict):
            candidate = services.get("group_services_config")
            config = candidate if isinstance(candidate, GroupServicesConfig) else None
    if config is None:
        config = GroupServicesConfig()
        _safe_set(host, "group_services_cfg", config)
    return config


# ---------------------------------------------------------------------------
# 拷贝
# ---------------------------------------------------------------------------
def clone_group_reminders(
    input_value: dict[str, list[GroupReminderConfig]] | None,
) -> dict[str, list[GroupReminderConfig]]:
    """对应 Go ``cloneGroupReminders``。"""
    if not input_value:
        return {}
    out: dict[str, list[GroupReminderConfig]] = {}
    for key, values in input_value.items():
        key = str(key or "").strip()
        if not key:
            continue
        out[key] = [value.copy() for value in values]
    return out


def clone_group_polls(
    input_value: dict[str, list[GroupPollConfig]] | None,
) -> dict[str, list[GroupPollConfig]]:
    """对应 Go ``cloneGroupPolls``（``options`` / ``votes`` / ``voters`` 都深拷）。"""
    if not input_value:
        return {}
    out: dict[str, list[GroupPollConfig]] = {}
    for key, values in input_value.items():
        key = str(key or "").strip()
        if not key:
            continue
        copied: list[GroupPollConfig] = []
        for value in values:
            item = value.copy()
            item.options = list(value.options or [])
            item.votes = dict(value.votes or {})
            item.voters = dict(value.voters or {})
            copied.append(item)
        out[key] = copied
    return out


def clone_group_signups(
    input_value: dict[str, list[GroupSignupConfig]] | None,
) -> dict[str, list[GroupSignupConfig]]:
    """对应 Go ``cloneGroupSignups``。"""
    if not input_value:
        return {}
    out: dict[str, list[GroupSignupConfig]] = {}
    for key, values in input_value.items():
        key = str(key or "").strip()
        if not key:
            continue
        copied: list[GroupSignupConfig] = []
        for value in values:
            item = value.copy()
            item.participants = dict(value.participants or {})
            copied.append(item)
        out[key] = copied
    return out


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def set_group_services_snapshot(
    agent: Any, snapshot: StateGroupServicesConfig | None
) -> None:
    """整表替换。对应 Go ``setGroupServicesSnapshot``。"""
    if agent is None:
        return
    state = group_services_state(agent)
    snapshot = snapshot or StateGroupServicesConfig()
    with state.lock:
        _replace(state.reminders, clone_group_reminders(snapshot.reminders))
        _replace(state.polls, clone_group_polls(snapshot.polls))
        _replace(state.signups, clone_group_signups(snapshot.signups))


def group_services_snapshot(agent: Any) -> StateGroupServicesConfig:
    """整表副本。对应 Go ``groupServicesSnapshot``。"""
    if agent is None:
        return StateGroupServicesConfig()
    state = group_services_state(agent)
    with state.lock:
        return StateGroupServicesConfig(
            reminders=clone_group_reminders(state.reminders),
            polls=clone_group_polls(state.polls),
            signups=clone_group_signups(state.signups),
        )


def _replace(target: dict[str, Any], source: dict[str, Any]) -> None:
    target.clear()
    target.update(source)


def group_reminders_for_scope(agent: Any, scope: Any) -> list[GroupReminderConfig]:
    """对应 Go ``groupRemindersForScope``。"""
    if agent is None:
        return []
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        return [value.copy() for value in state.reminders.get(key, [])]


def set_group_reminders_for_scope(
    agent: Any, scope: Any, entries: list[GroupReminderConfig] | None
) -> None:
    """对应 Go ``setGroupRemindersForScope``。"""
    if agent is None:
        return
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        if not entries:
            state.reminders.pop(key, None)
            return
        state.reminders[key] = [value.copy() for value in entries]


def group_polls_for_scope(agent: Any, scope: Any) -> list[GroupPollConfig]:
    """对应 Go ``groupPollsForScope``。"""
    if agent is None:
        return []
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        copied = clone_group_polls({key: state.polls.get(key, [])})
    return copied.get(key, [])


def set_group_polls_for_scope(
    agent: Any, scope: Any, entries: list[GroupPollConfig] | None
) -> None:
    """对应 Go ``setGroupPollsForScope``。"""
    if agent is None:
        return
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        if not entries:
            state.polls.pop(key, None)
            return
        state.polls[key] = clone_group_polls({key: entries}).get(key, [])


def group_signups_for_scope(agent: Any, scope: Any) -> list[GroupSignupConfig]:
    """对应 Go ``groupSignupsForScope``。"""
    if agent is None:
        return []
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        copied = clone_group_signups({key: state.signups.get(key, [])})
    return copied.get(key, [])


def set_group_signups_for_scope(
    agent: Any, scope: Any, entries: list[GroupSignupConfig] | None
) -> None:
    """对应 Go ``setGroupSignupsForScope``。"""
    if agent is None:
        return
    key = context_overflow_key(scope)
    state = group_services_state(agent)
    with state.lock:
        if not entries:
            state.signups.pop(key, None)
            return
        state.signups[key] = clone_group_signups({key: entries}).get(key, [])


# ---------------------------------------------------------------------------
# 开关与写事务
# ---------------------------------------------------------------------------
def group_services_enabled_for_scope(agent: Any, ctx: Any) -> bool:
    """全局开关 + 群开关。对应 Go ``groupServicesEnabledForScope``。"""
    if agent is None or not group_services_config(agent).is_enabled():
        return False
    if not is_group_scope(agent, ctx):
        return False
    return group_policy_for_scope(agent, _scope(agent, ctx)).is_services_enabled()


def require_group_services(agent: Any, ctx: Any) -> None:
    """三个前置条件的统一报错。对应 Go ``requireGroupServices``。"""
    if agent is None:
        raise InvalidError("agent is nil")
    if not group_services_config(agent).is_enabled():
        raise InvalidError("群内提醒/投票/报名已全局关闭")
    if not is_group_scope(agent, ctx):
        raise InvalidError("该功能只适用于群聊")
    if not group_policy_for_scope(agent, _scope(agent, ctx)).is_services_enabled():
        raise InvalidError("当前群已关闭提醒/投票/报名")


def with_group_services_write(agent: Any, write: Callable[[], None]) -> None:
    """一次带回滚的群服务写入。对应 Go ``withGroupServicesWrite``。

    ``write`` 抛异常 → 恢复快照并原样抛出；成功但落盘失败 → 同样恢复快照并抛出。
    """
    if agent is None:
        raise InvalidError("agent is nil")
    state = group_services_state(agent)
    with state.write_lock:
        previous = group_services_snapshot(agent)
        try:
            write()
        except BaseException:
            set_group_services_snapshot(agent, previous)
            raise
        try:
            _save_runtime_state(agent)
        except BaseException:
            set_group_services_snapshot(agent, previous)
            raise


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
def next_group_service_id(prefix: str, ids: list[str] | None) -> str:
    """下一个可用 ID（``r1`` / ``p1`` / ``s1``…）。对应 Go ``nextGroupServiceID``。"""
    used = {str(item or "").strip().lower() for item in ids or ()}
    index = 1
    while True:
        candidate = "%s%d" % (prefix, index)
        if candidate not in used:
            return candidate
        index += 1


def parse_reminder_when(raw: str, now: datetime) -> datetime:
    """解析提醒时间。对应 Go ``parseReminderWhen``。

    支持 ``10m`` / ``1h30m`` / ``2d`` / ``15:04`` / ``01-02 15:04`` /
    ``YYYY-MM-DD HH:MM[:SS]`` / ``YYYY-MM-DDTHH:MM``；``in `` 前缀会被剥掉。
    """
    text = str(raw or "").strip().lower()
    if not text:
        raise InvalidError("请指定提醒时间")
    if text.startswith("in "):
        text = text[3:].strip()
    relative = parse_relative_reminder(text, now)
    if relative is not None:
        return relative
    parsed = _parse_absolute_reminder(text, now)
    if parsed is None:
        raise InvalidError(
            "无法识别提醒时间 %r，请使用 10m、1h30m、2d、15:04 或 YYYY-MM-DD HH:MM" % raw
        )
    return parsed


def parse_relative_reminder(raw: str, now: datetime) -> datetime | None:
    """``2d3h`` / ``90m`` 之类的相对时间。对应 Go ``parseRelativeReminder``。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if "d" in text:
        days_text, _, rest = text.partition("d")
        try:
            days = int(days_text.strip())
        except ValueError:
            return None
        if days < 0:
            return None
        delta = timedelta(days=days)
        rest = rest.strip()
        if rest:
            tail = _parse_go_duration(rest)
            if tail is None:
                return None
            delta += tail
        return now + delta
    delta = _parse_go_duration(text)
    if delta is None:
        return None
    return now + delta


def check_reminder_when(when_text: str, text: str, config: GroupServicesConfig, now: datetime) -> datetime:
    """提醒创建的前置校验，返回生效的 ``due_at``。对应 Go ``ReminderCreate`` 前半段。"""
    body = str(text or "").strip()
    if not body:
        raise InvalidError("提醒内容不能为空")
    if rune_len(body) > config.max_text_runes:
        raise InvalidError("提醒内容超过 %d 个字符" % config.max_text_runes)
    due_at = parse_reminder_when(when_text, now)
    if due_at < now + MIN_REMINDER_DELAY:
        raise InvalidError("提醒时间至少要在 %s 之后" % group_service_duration_text(MIN_REMINDER_DELAY))
    if due_at > now + timedelta(days=config.max_reminder_days):
        raise InvalidError("提醒时间不能超过 %d 天" % config.max_reminder_days)
    return due_at


#: 创建提醒的最小提前量。对应 Go ``minReminderDelay``。
MIN_REMINDER_DELAY = timedelta(seconds=10)

#: 提醒派发轮询间隔。对应 Go ``groupServiceTickEvery``。
GROUP_SERVICE_TICK_SECONDS = 30.0


def parse_group_service_time(value: str) -> tuple[datetime | None, bool]:
    """解析落库时间。对应 Go ``parseGroupServiceTime``。"""
    text = str(value or "").strip()
    if not text:
        return None, False
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed, True
    if parsed is not None:
        # RFC3339 无时区：Go 的 ParseInLocation 按本地时区解释。
        return parsed.astimezone(), True
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, False
    return naive.astimezone(), True


def format_group_service_time(value: datetime) -> str:
    """落库格式（RFC3339、秒精度）。对应 Go ``formatGroupServiceTime``。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def group_service_duration_text(value: timedelta) -> str:
    """时长文案。对应 Go ``groupServiceDurationText``（秒精度）。"""
    seconds = max(0, int(round(value.total_seconds())))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    out = ""
    if hours:
        out += "%dh" % hours
    if minutes:
        out += "%dm" % minutes
    if secs or not out:
        out += "%ds" % secs
    return out


# ---------------------------------------------------------------------------
# 载入时的归一化
# ---------------------------------------------------------------------------
def normalize_group_services_snapshot(
    input_value: StateGroupServicesConfig | None, config: GroupServicesConfig
) -> StateGroupServicesConfig:
    """``state.toml`` → 内存：截断超限字段、丢掉过期与无效条目。

    对应 Go ``normalizeGroupServicesSnapshot``。
    """
    config = (config or GroupServicesConfig()).normalized()
    source = input_value or StateGroupServicesConfig()
    out = StateGroupServicesConfig()
    now = datetime.now().astimezone()
    for key, values in (source.reminders or {}).items():
        key = str(key or "").strip()
        if not key:
            continue
        clean: list[GroupReminderConfig] = []
        for value in values:
            item = value.copy()
            item.id = str(item.id or "").strip()
            item.text = truncate_runes(str(item.text or "").strip(), config.max_text_runes)
            item.due_at = str(item.due_at or "").strip()
            item.status = str(item.status or "").strip()
            if not item.id or not item.text or not item.due_at:
                continue
            if item.status in ("sent", "skipped"):
                at, ok = parse_group_service_time(item.due_at)
                if ok and at is not None and now - at > timedelta(hours=24):
                    continue
            clean.append(item)
            if len(clean) >= config.max_reminders_per_scope:
                break
        if clean:
            out.reminders[key] = clean
    for key, values in (source.polls or {}).items():
        key = str(key or "").strip()
        if not key:
            continue
        clean_polls: list[GroupPollConfig] = []
        for value in values:
            item = value.copy()
            item.id = str(item.id or "").strip()
            item.question = truncate_runes(str(item.question or "").strip(), config.max_text_runes)
            item.status = str(item.status or "").strip()
            options = list(item.options or [])
            if len(options) > config.max_poll_options:
                options = options[: config.max_poll_options]
            item.options = [truncate_runes(str(opt or "").strip(), config.max_text_runes) for opt in options]
            if not item.id or not item.question or len(item.options) < 2:
                continue
            if item.status == "closed":
                at, ok = parse_group_service_time(item.created_at)
                if ok and at is not None and now - at > timedelta(days=7):
                    continue
            clean_polls.append(item)
            if len(clean_polls) >= config.max_polls_per_scope:
                break
        if clean_polls:
            out.polls[key] = clean_polls
    for key, values in (source.signups or {}).items():
        key = str(key or "").strip()
        if not key:
            continue
        clean_signups: list[GroupSignupConfig] = []
        for value in values:
            item = value.copy()
            item.id = str(item.id or "").strip()
            item.title = truncate_runes(str(item.title or "").strip(), config.max_text_runes)
            item.status = str(item.status or "").strip()
            if item.capacity < 0:
                item.capacity = 0
            if item.capacity > config.max_signup_capacity:
                item.capacity = config.max_signup_capacity
            if not item.id or not item.title:
                continue
            if item.status == "closed":
                at, ok = parse_group_service_time(item.created_at)
                if ok and at is not None and now - at > timedelta(days=7):
                    continue
            clean_signups.append(item)
            if len(clean_signups) >= config.max_signups_per_scope:
                break
        if clean_signups:
            out.signups[key] = clean_signups
    return out


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _scope(agent: Any, ctx: Any) -> Any:
    helper = getattr(agent, "scope", None)
    if helper is not None:
        return helper(ctx)
    from atri_x.agent.identity import scope as identity_scope

    return identity_scope(agent, ctx)


def _save_runtime_state(agent: Any) -> None:
    helper = getattr(agent, "save_runtime_state", None)
    if helper is not None:
        helper()


def _parse_go_duration(value: str) -> timedelta | None:
    """Go ``time.ParseDuration`` 的等价物（``ns`` / ``us`` / ``ms`` / ``s`` / ``m`` / ``h``）。"""
    text = str(value or "").strip()
    if not text:
        return None
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    if not text:
        return None
    units = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "μs": 1e-6,
        "ms": 1e-3,
        "s": 1.0,
        "m": 60.0,
        "h": 3600.0,
    }
    total = 0.0
    number = ""
    index = 0
    matched = False
    while index < len(text):
        char = text[index]
        if char.isdigit() or char == ".":
            number += char
            index += 1
            continue
        unit = None
        for candidate in ("ns", "us", "µs", "μs", "ms", "s", "m", "h"):
            if text.startswith(candidate, index):
                unit = candidate
                break
        if unit is None or not number:
            return None
        try:
            total += float(number) * units[unit]
        except ValueError:
            return None
        matched = True
        number = ""
        index += len(unit)
    if number or not matched:
        return None
    return timedelta(seconds=sign * total)


def _parse_absolute_reminder(text: str, now: datetime) -> datetime | None:
    """绝对时间：``15:04`` / ``01-02 15:04`` / ``YYYY-MM-DD HH:MM[:SS]``。"""
    local = now.astimezone()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        candidate = local.replace(
            hour=parsed.hour,
            minute=parsed.minute,
            second=parsed.second,
            microsecond=0,
        )
        if candidate <= local:
            candidate = candidate + timedelta(days=1)
        return candidate
    try:
        parsed = datetime.strptime("%d-%s" % (local.year, text), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.astimezone()
