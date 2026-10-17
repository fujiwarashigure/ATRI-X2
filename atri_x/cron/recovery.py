"""平台连接跟踪、过期 once 扫描与补发入口。对应 Go：``internal/cron/recovery.go``。

两件事：

1. **历史数据迁移**（:meth:`RecoveryMixin.migrate_legacy_delivery_state`）：把老版本写在
   metadata ``delivery`` 段里的投递数据搬到 ``delivery_state`` 列，**在调度器把它当成
   新一轮任务之前**；已完整投递的条件禁用，未完整的等平台连接补发。
2. **补发**：只由**平台连接**触发（:meth:`RecoveryMixin.notify_platform_connected`）；
   扫描已过期的 ``once`` 任务，读**最新任务配置**，沿用旧报告继续补发，**不重新执行 LLM**。
   补发失败会在 cli 上通知超管，但不会形成周期重试。
"""

from __future__ import annotations

from typing import Any

from atri_x.cron.delivery import (
    delivery_output_ids,
    ensure_delivery_output_state,
    ensure_delivery_target_state,
)
from atri_x.cron.errors import join_errors
from atri_x.cron.model import (
    METADATA_KIND,
    USER_HANDLER_NAME,
    CronDeliveryState,
    DeliveryDelivered,
    LegacyCronDeliveryMetadata,
    Metadata,
    ScheduleMode,
    contains_string,
    decode_legacy_delivery_metadata,
    decode_metadata,
    encode_delivery_state,
    has_legacy_delivery_metadata,
    parse_run_at,
)
from atri_x.errors import AtriXError
from atri_x.storage.ids import new_id
from atri_x.storage.models import CronJob

__all__ = ["RecoveryMixin"]


class RecoveryMixin:
    """``internal/cron/recovery.go`` 的方法集合（由 :class:`~atri_x.cron.service.Service` 组合）。"""

    async def migrate_legacy_delivery_state(self, ctx: Any) -> None:
        """迁移历史投递数据。对应 Go ``MigrateLegacyDeliveryState``。

        只处理"还没跑过、没有 next_run_at、没有 delivery_state/token"的启用任务，
        因此天然幂等；CAS 的 ``expected_token=""`` 保证并发下只有一方迁移成功。
        """
        jobs = self.cron_jobs().list_enabled()
        for job in jobs:
            if (
                job.handler != USER_HANDLER_NAME
                or job.run_count == 0
                or job.next_run_at is not None
                or job.delivery_state
                or job.delivery_token
            ):
                continue
            try:
                meta = decode_metadata(job.metadata)
            except Exception:  # noqa: BLE001
                continue
            if meta.schedule.mode != ScheduleMode.ONCE:
                continue
            try:
                legacy = decode_legacy_delivery_metadata(job.metadata)
            except Exception:  # noqa: BLE001
                continue
            if not has_legacy_delivery_metadata(legacy):
                continue

            state = self.legacy_delivery_state(meta, legacy)
            try:
                encoded = encode_delivery_state(state)
            except Exception as exc:  # noqa: BLE001
                raise AtriXError(f"encode legacy cron delivery {job.name}: {exc}") from exc
            try:
                swapped = self.cron_jobs().compare_and_swap_delivery(
                    job.id, "", state.run_id, encoded
                )
            except Exception as exc:  # noqa: BLE001
                raise AtriXError(f"migrate legacy cron delivery {job.name}: {exc}") from exc
            if not swapped:
                continue
            self.log_info("cron legacy delivery migrated", **self.cron_log_attrs(job.name, meta))
            if self.delivery_complete(meta, state):
                err = await self.disable_completed_delivery(ctx, job.name, state.run_id)
                if err is not None:
                    raise AtriXError(
                        f"disable migrated cron delivery {job.name}: {err}"
                    ) from err

    def legacy_delivery_state(
        self, meta: Metadata, legacy: LegacyCronDeliveryMetadata
    ) -> CronDeliveryState:
        """把历史投递数据折算成新的投递状态。对应 Go ``legacyDeliveryState``。"""
        state = CronDeliveryState(
            run_id=new_id(),
            report_ready=True,
            task_completed=legacy.completed,
            report=legacy.report,
            report_segments=list(legacy.report_segments),
            report_session_id=legacy.report_session_id,
            report_message_id=legacy.report_message_id,
        )
        output_ids = delivery_output_ids(state)
        for target in self.resolve_delivery_targets(meta, ""):
            if not contains_string(legacy.delivered_platforms, target.platform):
                continue
            target_state = ensure_delivery_target_state(state, target.key)
            for output_id in output_ids:
                ensure_delivery_output_state(target_state, output_id).status = DeliveryDelivered
        return state

    async def run_missed_once(self, ctx: Any) -> None:
        """对所有已知连接平台各扫一次补发。对应 Go ``RunMissedOnce``。"""
        for platform_name in self.connected_platform_names():
            await self.run_missed_once_for_platform(ctx, platform_name)

    async def notify_platform_connected(self, ctx: Any, platform_name: str) -> None:
        """记录平台连接并立刻为该平台补发。对应 Go ``NotifyPlatformConnected``。"""
        name = (platform_name or "").strip()
        if not name:
            return
        self.connected_platforms[name] = True
        self.log_info("cron platform connected", platform=name)
        await self.run_missed_once_for_platform(ctx, name)

    async def run_missed_once_for_platform(self, ctx: Any, platform_name: str) -> None:
        """扫描某个平台上已过期的一次性任务并补发。对应 Go ``runMissedOnceForPlatform``。

        列表用初读的 job 做粗筛，真正投递前**重新读一遍最新配置**（名字、run_at、目标平台、
        是否仍启用都可能被改过），并在同一把 per-job 闸门里完成投递。
        """
        jobs = self.cron_jobs().list_enabled()
        for job in jobs:
            try:
                meta = decode_metadata(job.metadata)
            except Exception:  # noqa: BLE001
                continue
            if meta.kind != METADATA_KIND or meta.schedule.mode != ScheduleMode.ONCE:
                continue
            try:
                run_at = parse_run_at(meta.schedule.run_at, self.location)
            except Exception:  # noqa: BLE001
                continue
            if run_at > self.now() or not contains_string(
                self.target_platform_names(meta), platform_name
            ):
                continue
            async with self._delivery_gate(job.name):
                try:
                    latest = self.cron_jobs().get_by_name(job.name)
                except Exception:  # noqa: BLE001
                    continue
                if not latest.enabled:
                    continue
                try:
                    latest_meta = decode_metadata(latest.metadata)
                    latest_run_at = parse_run_at(latest_meta.schedule.run_at, self.location)
                except Exception:  # noqa: BLE001
                    continue
                if (
                    latest_meta.kind != METADATA_KIND
                    or latest_meta.schedule.mode != ScheduleMode.ONCE
                    or latest_run_at > self.now()
                    or not contains_string(
                        self.target_platform_names(latest_meta), platform_name
                    )
                ):
                    continue
                self.audit_event(
                    "cron.missed_delivery_started",
                    **self.cron_audit_attrs(job.name, meta, platform=platform_name),
                )
                self.log_info(
                    "cron missed delivery started",
                    **self.cron_log_attrs(job.name, meta, platform=platform_name),
                )
                deliver_err = await self.deliver_missed_once(
                    ctx, latest, latest_meta, platform_name
                )
            if deliver_err is not None:
                self.audit_event(
                    "cron.missed_delivery_failed",
                    **self.cron_audit_attrs(
                        job.name, latest_meta, platform=platform_name, error=str(deliver_err)
                    ),
                )
                self.log_warn(
                    "missed cron run failed",
                    job=job.name,
                    platform=platform_name,
                    error=str(deliver_err),
                )
                await self.send_to_platforms(
                    ctx,
                    job.name,
                    ["cli"],
                    f"cron 补跑失败：{job.name}\n错误：{deliver_err}",
                )
                continue
            self.audit_event(
                "cron.missed_delivery_completed",
                **self.cron_audit_attrs(job.name, latest_meta, platform=platform_name),
            )
            self.log_info(
                "cron missed delivery completed",
                **self.cron_log_attrs(job.name, latest_meta, platform=platform_name),
            )

    async def deliver_missed_once(
        self, ctx: Any, job: CronJob, meta: Metadata, platform_name: str
    ) -> BaseException | None:
        """为单个平台补发一次。对应 Go ``deliverMissedOnce``。

        复用 :meth:`~atri_x.cron.execution.ExecutionMixin.prepare_delivery`：
        报告已就绪时不会再执行 LLM，只把报告投给还没收到的目标。
        """
        job, state, prepare_err = await self.prepare_delivery(ctx, job, meta)
        if not state.report_ready:
            return prepare_err
        deliver_err = await self.deliver_prepared(ctx, job, meta, state, platform_name, True)
        return join_errors(prepare_err, deliver_err)

    def connected_platform_names(self) -> list[str]:
        """已连接平台名（已排序）。对应 Go ``connectedPlatformNames``。"""
        return sorted(self.connected_platforms)
