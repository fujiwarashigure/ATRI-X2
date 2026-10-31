"""桌面端的断线自愈接线层：把 :mod:`atri_x.gui.supervisor` 接到 :class:`GuiApp` 上。

为什么单独一层：``atri_x/gui/app.py`` 已在单文件行数棘轮基线里（只允许变短），而断线自愈
需要在主循环里 tick、在连接状态事件里记一笔、在状态栏里临时显示「正在自动重启后台服务…」。
所以用**子类覆盖**这几个方法（``super()`` 保留原行为），把新增逻辑关在这个小模块里，
``app.py`` 本身一行不动，全量自检/单测直接构造的 :class:`GuiApp` 默认行为也保持不变。

安全边界在 :mod:`atri_x.gui.supervisor` 里实现（只对回环地址、`--no-service` 不启用、
用户显式停服务后暂停）；本模块只负责「什么时候问看护对象、结果怎么展示」。
"""

from __future__ import annotations

from typing import Any

from atri_x.gui import journal
from atri_x.gui.app import GuiApp
from atri_x.gui.supervisor import (
    RecoveryOutcome,
    ServiceSupervisor,
    is_loopback_url,
    service_recovery,
)

__all__ = ["RecoveringGuiApp"]


class RecoveringGuiApp(GuiApp):
    """在 :class:`GuiApp` 上叠加「后台服务断线自愈」。

    只有真正的桌面端入口（:mod:`atri_x.gui.__main__`）用它；``--no-service`` 时
    ``allow_auto_recover=False``，不会构造看护对象。
    """

    def __init__(self, *args: Any, allow_auto_recover: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._supervisor: ServiceSupervisor | None = None
        settings = self.settings
        # 只对本机回环的后台服务启用：连远端服务器时绝不替对方重启进程。
        if allow_auto_recover and settings is not None and is_loopback_url(settings.url):
            self._supervisor = ServiceSupervisor(service_recovery(settings))

    # ---------------------------------------------------------------- 覆盖点

    def _poll(self) -> None:
        """主循环：先处理原窗口逻辑，再驱动一次自愈（不阻塞 Tk）。"""
        super()._poll()
        self._poll_service_recovery()

    def _handle_state(self, state: str, detail: str) -> None:
        super()._handle_state(state, detail)
        supervisor = self._supervisor
        if supervisor is not None:
            supervisor.note_state(state)

    def _update_status(self) -> None:
        """状态栏：自动恢复进行中时优先显示「正在自动重启后台服务…」。

        WS 客户端每 2 秒会再报一次「已断开」，直接改 ``_status_text`` 会被覆盖，所以
        在渲染层按 ``in_flight`` 覆盖一次标签。
        """
        supervisor = self._supervisor
        in_flight = supervisor is not None and supervisor.in_flight
        label = "● 正在自动重启后台服务…" if in_flight else self._status_text
        if self.status_label.cget("text") != label:
            self.status_label.configure(text=label)
        runtime = self._runtime_text or ""
        if self.runtime_label.cget("text") != runtime:
            self.runtime_label.configure(text=runtime)
        self.features_tab.set_status(label)

    def quit_app(self, *, stop_service: bool) -> None:
        """退出前先停看护：否则「退出并停止服务」会被自愈当成掉线又拉起来。"""
        if stop_service:
            self.suspend_auto_recover()
        super().quit_app(stop_service=stop_service)

    # ---------------------------------------------------------------- 自愈接口

    def _poll_service_recovery(self) -> None:
        """驱动状态机并消费后台恢复结果（每个 :data:`POLL_INTERVAL_MS` 调一次）。"""
        supervisor = self._supervisor
        if supervisor is None:
            return
        supervisor.tick()
        for outcome in supervisor.take_outcomes():
            self._handle_recovery_outcome(outcome)

    def _handle_recovery_outcome(self, outcome: RecoveryOutcome) -> None:
        supervisor = self._supervisor
        config_dir = self._tray_config_dir()
        if outcome.ok:
            self.service_owned = True
            journal.record(config_dir, f"后台服务已自动恢复（第 {outcome.attempt} 次尝试）")
            self._append_notice("后台服务已自动恢复，正在重新连接…")
            return
        journal.record(
            config_dir, f"后台服务自动恢复失败（第 {outcome.attempt} 次）：{outcome.detail}"
        )
        if outcome.attempt == 1:
            self._append_notice(
                f"检测到后台服务已断开，正在自动重启（第 1 次）：{outcome.detail}"
            )
        if supervisor is not None and supervisor.exhausted:
            self._append_notice(
                "自动恢复后台服务失败（已重试到上限）。请从「服务 → 重启后台服务」手动恢复，"
                "或打开后台日志排查。"
            )

    def suspend_auto_recover(self) -> None:
        """暂停断线自愈：用户显式停止/重启服务、或退出程序时调用。"""
        supervisor = self._supervisor
        if supervisor is not None:
            supervisor.suspend()

    def resume_auto_recover(self) -> None:
        """恢复断线自愈并重新计时：用户手动启动/重启服务（成功或失败）后调用。"""
        supervisor = self._supervisor
        if supervisor is not None:
            supervisor.resume()
