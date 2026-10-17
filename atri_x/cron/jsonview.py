"""cron 领域对象的 **Go ``json.Marshal`` 形态**（Python 侧新增件）。

对应 Go：无独立文件。Go 侧这些 JSON 全部来自 ``json.Marshal`` + struct tag：

* ``internal/cron/model.go:147`` ``JobView{Job json:"job"; Metadata json:"metadata";
  Delivery json:"delivery"}``；
* ``internal/storage/storage.go:88`` ``CronJob``（**没有任何 json tag**，所以 JSON
  键就是字段名 ``ID`` / ``Name`` / … / ``UpdatedAt``；``*time.Time`` 为 nil → ``null``，
  ``time.Time`` 零值 → ``0001-01-01T00:00:00Z``）。

Python 侧这些类型是 dataclass，没有"自动按 Go 字段名序列化"的能力，而工具层
（``atri_x/tool/builtin/cron.py`` 的 ``cron_query`` / ``cron_write``）必须把**真实**
``CronJob`` 回给模型。把这一层集中在这里，避免工具层自己拼字段，也避免把 Go 的
json tag 语义散落到多处。

鸭子类型约定与 :func:`atri_x.tool.builtin.common.go_json` 一致：对象自带
``to_json()`` 时优先用它（便于测试注入视图）。
"""

from __future__ import annotations

from typing import Any

from atri_x.cron.model import JobView
from atri_x.storage.times import format_time

__all__ = ["GO_ZERO_TIME_TEXT", "cron_job_json", "job_view_json"]

#: Go ``time.Time`` 零值的 JSON 文本（``json.Marshal`` 不省略零值时间）。
GO_ZERO_TIME_TEXT = "0001-01-01T00:00:00Z"

#: ``storage.CronJob`` 的 Go JSON 键 → Python 字段名（结构体无 json tag，键即字段名）。
_CRON_JOB_JSON_FIELDS = (
    ("ID", "id"),
    ("Name", "name"),
    ("Handler", "handler"),
    ("Schedule", "schedule"),
    ("Enabled", "enabled"),
    ("Metadata", "metadata"),
    ("DeliveryState", "delivery_state"),
    ("DeliveryToken", "delivery_token"),
    ("LastRunAt", "last_run_at"),
    ("NextRunAt", "next_run_at"),
    ("RunCount", "run_count"),
    ("LastError", "last_error"),
    ("CreatedAt", "created_at"),
    ("UpdatedAt", "updated_at"),
)

#: ``storage.CronJob`` 里是 ``*time.Time`` 的字段（nil → ``null``）。
_CRON_JOB_POINTER_TIMES = ("last_run_at", "next_run_at")

#: ``storage.CronJob`` 里是 ``time.Time`` 值的字段（零值 → ``0001-01-01T00:00:00Z``）。
_CRON_JOB_VALUE_TIMES = ("created_at", "updated_at")


def cron_job_json(job: Any) -> dict[str, Any]:
    """``storage.CronJob`` → Go ``json.Marshal`` 形态。"""
    to_json = getattr(job, "to_json", None)
    if callable(to_json):
        return to_json()
    out: dict[str, Any] = {}
    for key, attr in _CRON_JOB_JSON_FIELDS:
        value = getattr(job, attr)
        if attr in _CRON_JOB_POINTER_TIMES:
            out[key] = None if value is None else _go_time(value)
        elif attr in _CRON_JOB_VALUE_TIMES:
            out[key] = _go_time(value)
        else:
            out[key] = value
    return out


def _go_time(value: Any) -> str:
    """单个时间字段的 Go JSON 文本（``None`` 视作零值）。"""
    if value is None:
        return GO_ZERO_TIME_TEXT
    return format_time(value) or GO_ZERO_TIME_TEXT


def job_view_json(view: JobView) -> dict[str, Any]:
    """``JobView`` → Go ``json.Marshal`` 形态（delivery 恒出现，无 omitempty 效果）。"""
    return {
        "job": cron_job_json(view.job),
        "metadata": _to_jsonable(view.metadata),
        "delivery": _to_jsonable(view.delivery),
    }


def _to_jsonable(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    return value
