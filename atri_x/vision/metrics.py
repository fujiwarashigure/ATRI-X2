"""视觉服务的轻量观测接口。

对应 Go：``internal/vision/metrics.go``。

实现方必须并发安全，且**永不**记录图像字节、base64、API key、media ID 或
缓存键——这些标签会进入 ``/metrics``，属于不可外泄的数据。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Metrics", "NoopMetrics", "Snapshotter"]


@runtime_checkable
class Metrics(Protocol):
    """接收服务观测事件的接口。对应 Go ``Metrics``。"""

    def cache_lookup(self, result: str) -> None:
        """记录 ``"hit"`` / ``"negative_hit"`` / ``"miss"``。"""
        ...

    def coalesced(self) -> None:
        """记录一次由在途同 key 任务服务的请求。"""
        ...

    def job_started(self) -> None:
        """记录一个唯一的真实上游任务。"""
        ...

    def duration(self, stage: str, seconds: float) -> None:
        """记录阶段耗时（秒）：``"preprocess"`` / ``"upstream"`` / ``"total"``。"""
        ...

    def error(self, error_class: str) -> None:
        """记录安全错误类别，如 ``"timeout"`` / ``"canceled"`` / ``"http_429"``。"""
        ...


class NoopMetrics:
    """丢弃所有观测。对应 Go ``NoopMetrics``。"""

    def cache_lookup(self, result: str) -> None:  # noqa: ARG002 - 契约签名
        return None

    def coalesced(self) -> None:
        return None

    def job_started(self) -> None:
        return None

    def duration(self, stage: str, seconds: float) -> None:  # noqa: ARG002
        return None

    def error(self, error_class: str) -> None:  # noqa: ARG002
        return None


@runtime_checkable
class Snapshotter(Protocol):
    """能回报自身计数的 Metrics。对应 Go ``Snapshotter``。

    进程内 :class:`atri_x.vision.counters.Counters` 实现它；服务通过
    :meth:`atri_x.vision.service.Service.stats` 暴露出去。
    """

    def metrics_snapshot(self) -> object: ...
