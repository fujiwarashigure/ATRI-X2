"""「在线」选项卡：一条线看住「后台服务 → 协议端 → QQ 平台」三段，并提供上下线开关。

为什么需要这一页：QQ 机器人"看起来在跑"和"真的在线"是两件事 —— 后台服务活着、
NapCat 端口在听、OneBot 连接真的连上了，任何一环断掉现象都只是"群里不回话"。
这里把三段状态并排显示，并把「启用/停用 QQ 平台 + 重启后台服务」做成一个开关：

- **连接（上线）**：校验 ``[platform.qqonebot]`` 的 ws 地址与 token → 写
  ``enabled = true`` → 重启后台服务 → 轮询 ``/healthz`` 直到平台状态变成已连接；
- **断开（下线）**：写 ``enabled = false`` → 重启后台服务。

状态数据来自后台服务的 ``/healthz``（见 :mod:`atri_x.gui.linkstate` 与
:mod:`atri_x.health.server`），**不是**猜日志；协议端端口用一次 TCP 探测，
完整的一次性验证仍然用 ``atri-x.exe -m atri_x.platform.qq_onebot.smoke``。

发行包自带协议端环境（``qqproto\\``）时，本页还能一键「启动协议端」并直接打开带
令牌的 NapCat WebUI（扫码登录）；令牌与端口由 :mod:`atri_x.gui.qqproto` 在启动时
自动生成。外部 NapCat 用户看到的仍是普通端口探测。

本页不改 ``.env`` 里的密钥，只改 ``app.toml`` 的一行 ``enabled``。
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Any

from atri_x.gui import linkstate, process, qqproto, service, widgets
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.tabs.base import ConfigTab, confirm

__all__ = ["LinkTab"]

#: 状态自动刷新间隔（毫秒）。健康接口在本机回环，2 秒一次开销可以忽略。
REFRESH_INTERVAL_MS = 2000

#: 部署提示：内置协议端后，换机器只需要带「程序目录 + 配置目录」。
DEPLOY_HINT = (
    "换电脑部署（照顺序）：① 复制本程序目录（便携版/安装版都自带 NapCat + 便携 QQ，"
    "首次启动自动生成令牌与端口）；② 复制配置目录（%APPDATA%\\ATRI-X，"
    "或用便携版 start-portable.bat 时在包内 data\\ 下）→ 新机器上先启动桌面端 → "
    "在本页点「启动协议端」扫码登录 → 点「连接（上线）」。"
)


class LinkTab(ConfigTab):
    """后台服务 / 协议端 / QQ 平台三段状态 + 连接开关。"""

    hint = ""

    def __init__(
        self,
        parent: Any,
        paths: Any,
        *,
        settings: service.GuiSettings | None = None,
        on_restart: Any = None,
        can_control_service: bool = True,
    ) -> None:
        super().__init__(parent, paths, on_restart=on_restart)
        self.settings = settings
        self.can_control_service = bool(can_control_service and settings is not None)
        self._proto: qqproto.QqProtoSetup | None = (
            settings.qqproto if settings is not None else None
        )
        self._visible = False
        self._inflight = False
        self._busy = False
        self._job: str | None = None
        self._cfg = linkstate.PlatformConfig()
        self._report: linkstate.HealthReport | None = None
        self._health_error = ""
        self._probe: linkstate.EndpointProbe | None = None
        self._build()
        self.reload()

    # ---------------------------------------------------------------- 构建界面

    def _build(self) -> None:
        self._vars: dict[str, tk.StringVar] = {
            "service_state": tk.StringVar(value="…"),
            "service_detail": tk.StringVar(value=""),
            "service_messages": tk.StringVar(value=""),
            "platform_config": tk.StringVar(value=""),
            "platform_state": tk.StringVar(value=""),
            "platform_detail": tk.StringVar(value=""),
            "endpoint_state": tk.StringVar(value="…"),
            "endpoint_detail": tk.StringVar(value=""),
            "endpoint_env": tk.StringVar(value=""),
            "models": tk.StringVar(value=""),
        }
        grid = ttk.Frame(self.body)
        grid.pack(fill=tk.BOTH, expand=True)
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="link")

        # 后台服务
        card = self._card(grid, "后台服务（atri-x service run）", 0, 0, padx=(0, 6), pady=(0, 6))
        self._label(card, "service_state", row=0, bold=True)
        self._label(card, "service_detail", row=1, wrap=300)
        self._label(card, "service_messages", row=2, wrap=300)

        # QQ 平台
        card = self._card(grid, "QQ 平台（OneBot v11）", 0, 1, padx=(0, 6), pady=(0, 6))
        self._label(card, "platform_config", row=0, wrap=320)
        self._platform_state_label = self._label(card, "platform_state", row=1, bold=True)
        self._label(card, "platform_detail", row=2, wrap=320)
        actions = ttk.Frame(card)
        actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._connect_button = ttk.Button(actions, text="连接（上线）", command=self._toggle_online)
        self._connect_button.pack(side=tk.LEFT)

        # 协议端
        card = self._card(grid, "协议端（NapCat）", 0, 2, pady=(0, 6))
        self._label(card, "endpoint_state", row=0, bold=True)
        self._label(card, "endpoint_detail", row=1, wrap=280)
        self._label(card, "endpoint_env", row=2, wrap=280)
        actions = ttk.Frame(card)
        actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._probe_button = ttk.Button(actions, text="检测端口", command=self._probe_now)
        self._probe_button.pack(side=tk.LEFT, padx=(0, 6))
        self._start_button = ttk.Button(actions, text="启动协议端", command=self._start_protocol)
        self._start_button.pack(side=tk.LEFT, padx=(0, 6))
        if self._proto is None or not self._proto.available:
            self._start_button.configure(state="disabled")
        self._webui_button = ttk.Button(actions, text="打开 WebUI", command=self._open_webui)
        self._webui_button.pack(side=tk.LEFT)

        # 模型 + 提示
        card = self._card(grid, "模型与就绪状态", 1, 0, pady=(0, 6))
        card.grid(columnspan=3, sticky="ew")
        self._label(card, "models", row=0, wrap=980)

        hint = ttk.Label(
            grid,
            text=DEPLOY_HINT,
            foreground="#555555",
            wraplength=980,
            justify=tk.LEFT,
        )
        hint.grid(row=2, column=0, columnspan=3, sticky="w")

        self.button("立即刷新", self.refresh_now)
        self._copy_button = self.button("复制连接信息", self._copy_info)

    def _card(self, parent: Any, title: str, row: int, column: int, **grid: Any) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.grid(row=row, column=column, sticky="nsew", **grid)
        return frame

    def _label(
        self, parent: Any, key: str, *, row: int, bold: bool = False, wrap: int = 300
    ) -> ttk.Label:
        options: dict[str, Any] = {
            "textvariable": self._vars[key],
            "wraplength": wrap,
            "justify": tk.LEFT,
        }
        if bold:
            options["font"] = ("Microsoft YaHei UI", 10, "bold")
        label = ttk.Label(parent, **options)
        label.grid(row=row, column=0, sticky="w", pady=(2, 0))
        return label

    # ---------------------------------------------------------------- 生命周期

    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            self._cfg = linkstate.read_platform_config(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._render_service()
        self._render_platform()
        self._render_endpoint()

    def on_show(self) -> None:
        self._visible = True
        self.reload()
        self._schedule(0)

    def on_hide(self) -> None:
        self._visible = False
        self._cancel()

    # ---------------------------------------------------------------- 轮询

    def refresh_now(self) -> None:
        """立即刷新一次（不等自动轮询）。"""
        self._refresh()

    def _schedule(self, delay: int | None = None) -> None:
        self._cancel()
        if not self._visible:
            return
        interval = REFRESH_INTERVAL_MS if delay is None else max(0, int(delay))
        try:
            self._job = self.frame.after(interval, self._refresh)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            self._job = None

    def _cancel(self) -> None:
        if self._job is None:
            return
        try:
            self.frame.after_cancel(self._job)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        self._job = None

    def _refresh(self) -> None:
        self._job = None
        if self._inflight:
            self._schedule()
            return
        self._inflight = True
        cfg = self._cfg
        settings = self.settings
        previous_probe = self._probe

        def work() -> tuple[Any, Any, str]:
            report = None
            error = ""
            if settings is not None and settings.health_addr:
                try:
                    report = linkstate.fetch_health(settings.health_addr, settings.health_token)
                except linkstate.LinkError as exc:
                    error = str(exc)
            # 只在"还没确认连上"时做 TCP 探测：连上以后每分钟几次握手只会刷协议端日志。
            entry = linkstate.platform_of(report) if report is not None else None
            connected = bool(entry is not None and entry.connected)
            probe = previous_probe
            if cfg.ws_url and (probe is None or not connected):
                probe = linkstate.probe_endpoint(cfg.ws_url)
            return probe, report, error

        widgets.run_async(self.frame, work, self._refresh_done)

    def _refresh_done(self, value: Any, error: BaseException | None) -> None:
        self._inflight = False
        if error is not None:
            self._health_error = f"状态采集失败：{error}"
            self._render_service()
            self._schedule()
            return
        probe, report, health_error = value if isinstance(value, tuple) else (None, None, "")
        self._probe = probe
        self._report = report
        self._health_error = health_error
        self._render_service()
        self._render_platform()
        self._render_endpoint()
        self._render_models()
        self._schedule()

    # ---------------------------------------------------------------- 渲染

    def _render_service(self) -> None:
        pid = process.read_service_pid()
        alive = bool(pid > 0 and process.is_process_alive(pid))
        report = self._report
        if alive:
            state = f"运行中（PID {pid}）"
        elif report is not None:
            # 状态接口通 = 进程在跑，只是不是这个界面拉起的（pid 记录只由界面写）。
            state = "运行中（非本界面启动）"
        else:
            state = "未运行"
        self._vars["service_state"].set(state)
        parts: list[str] = []
        if report is not None:
            if report.version:
                parts.append(f"版本 {report.version}")
            parts.append(f"已运行 {linkstate.format_uptime(report.uptime_seconds)}")
            if report.http_status == 503:
                parts.append(f"状态 {report.status or 'not_ready'}")
        elif self._health_error:
            parts.append(self._health_error)
        elif self.settings is None or not self.settings.health_addr:
            parts.append("状态接口未配置（重启桌面端会自动补齐）")
        else:
            parts.append("状态接口不可达（后台服务可能刚重启）")
        self._vars["service_detail"].set(" · ".join(parts))
        if report is None:
            self._vars["service_messages"].set("")
            return
        text = f"消息 成功 {report.messages_ok} / 失败 {report.messages_failed}"
        last = linkstate.format_clock(report.last_message_at)
        if last:
            text += f" · 最近一条 {last}"
        self._vars["service_messages"].set(text)

    def _render_platform(self) -> None:
        cfg = self._cfg
        report = self._report
        entry = linkstate.platform_of(report) if report is not None else None
        self._vars["platform_state"].set(
            linkstate.describe_platform(entry, enabled=cfg.enabled, service_known=report is not None)
        )
        if entry is not None and entry.connected:
            color = "#1a7f37"
        elif cfg.enabled:
            color = "#b42318"
        else:
            color = "#555555"
        try:
            self._platform_state_label.configure(foreground=color)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

        parts = [f"配置：{'已启用' if cfg.enabled else '未启用'}"]
        if cfg.ws_url:
            parts.append(f"协议端 {cfg.ws_url}")
        if cfg.trigger_keywords:
            parts.append("唤醒词 " + "、".join(cfg.trigger_keywords))
        detail = " · ".join(parts)
        if entry is not None:
            extra = [f"连接次数 {entry.connect_count}"]
            connected_at = linkstate.format_clock(entry.last_connected_at)
            if connected_at:
                extra.append(f"最近上线 {connected_at}")
            if not entry.connected:
                disconnected_at = linkstate.format_clock(entry.last_disconnected_at)
                if disconnected_at:
                    extra.append(f"最近断开 {disconnected_at}")
            if entry.last_error:
                extra.append(f"错误：{entry.last_error}")
            detail += "\n" + " · ".join(extra)
        self._vars["platform_detail"].set(detail)
        self._vars["platform_config"].set(
            f"协议端 {cfg.ws_url}" if cfg.ws_url else "还没配协议端地址（见「平台」页）"
        )
        try:
            self._connect_button.configure(text="断开（下线）" if cfg.enabled else "连接（上线）")
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass

    def _render_endpoint(self) -> None:
        cfg = self._cfg
        proto = self._proto
        if proto is not None:
            if proto.available:
                self._vars["endpoint_env"].set(f"内置协议端：{proto.env_dir}")
            else:
                self._vars["endpoint_env"].set(f"内置协议端：不可用（{proto.reason}）")
        else:
            self._vars["endpoint_env"].set("协议端：外部自备（本程序没有内置环境）")
        if not cfg.ws_url:
            self._vars["endpoint_state"].set("未配置")
            self._vars["endpoint_detail"].set("到「平台」页填 [platform.qqonebot].ws_url 后再回来。")
            return
        probe = self._probe
        if probe is None:
            self._vars["endpoint_state"].set("未检测")
            self._vars["endpoint_detail"].set(cfg.ws_url)
            return
        self._vars["endpoint_state"].set("已监听" if probe.reachable else "未监听")
        detail = probe.detail
        if not probe.reachable and proto is not None and proto.available:
            detail += "\n协议端没起？点「启动协议端」再检测。"
        self._vars["endpoint_detail"].set(detail)

    def _render_models(self) -> None:
        report = self._report
        if report is None:
            self._vars["models"].set("（等待后台服务状态接口）")
            return
        if report.models:
            text = " · ".join(linkstate.describe_model(item) for item in report.models)
        else:
            text = "（还没有 provider 健康记录；发一条消息后出现）"
        checks = [
            f"{key}={report.checks[key]}"
            for key in ("process", "scheduler")
            if key in (report.checks or {})
        ]
        if checks:
            text += "\n就绪：" + " · ".join(checks)
        self._vars["models"].set(text)

    # ---------------------------------------------------------------- 动作

    def _toggle_online(self) -> None:
        if self._busy:
            return
        try:
            paths = self.require_paths()
        except ConfigEditError as exc:
            self.fail(exc)
            return
        try:
            cfg = linkstate.read_platform_config(paths.config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return

        target = not cfg.enabled
        if target:
            blocker = linkstate.connect_blocker(cfg, paths.env_path)
            if blocker:
                self.fail(blocker)
                return
            probe = linkstate.probe_endpoint(cfg.ws_url, timeout=1.0)
            if not probe.reachable and not confirm(
                self.frame,
                "协议端未监听",
                f"{probe.detail}。\n\nNapCat 没启动时后台会一直重连、机器人不会上线。"
                "仍要现在启用吗？",
            ):
                return
            if not confirm(
                self.frame,
                "连接 QQ",
                "将启用 [platform.qqonebot] 并重启后台服务，QQ 机器人会上线。继续？",
            ):
                return
        elif not confirm(
            self.frame,
            "断开 QQ",
            "将停用 [platform.qqonebot] 并重启后台服务，QQ 机器人会下线。继续？",
        ):
            return

        try:
            linkstate.set_platform_enabled(paths.config_path, target)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._cfg = cfg = linkstate.PlatformConfig(
            section_exists=True,
            enabled=target,
            ws_url=cfg.ws_url,
            token_env=cfg.token_env,
            trigger_keywords=cfg.trigger_keywords,
            send_file_mode=cfg.send_file_mode,
        )
        self.journal(f"在线页：platform.qqonebot.enabled -> {str(target).lower()}")
        self._render_platform()
        if not self.can_control_service:
            self.notify("已写入配置；桌面端没有托管后台服务，请手动重启后生效")
            self._refresh()
            return
        self._restart("连接 QQ" if target else "断开 QQ")

    def _restart(self, why: str) -> None:
        settings = self.settings
        if settings is None:
            self.notify("没有后台服务配置，无法重启")
            return
        self._set_busy(True)
        self.notify(f"正在重启后台服务（{why}）…")
        widgets.run_async(
            self.frame,
            lambda: service.restart_service(settings),
            self._restart_done,
        )

    def _restart_done(self, value: Any, error: BaseException | None) -> None:
        self._set_busy(False)
        if error is not None:
            self.fail(f"重启后台服务失败：{error}")
            return
        ok, reason = value if isinstance(value, tuple) and len(value) == 2 else (False, "未知结果")
        if not ok:
            self.fail(f"后台服务未就绪：{reason}")
            return
        self.journal("在线页：重启后台服务完成")
        self.notify("后台服务已就绪，等待 QQ 上线…")
        self._schedule(400)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        buttons = [self._connect_button, self._probe_button, self._webui_button, self._copy_button]
        if self._proto is not None and self._proto.available:
            buttons.append(self._start_button)
        for widget in buttons:
            try:
                widget.configure(state=state)
            except tk.TclError:  # pragma: no cover - 窗口销毁中
                pass

    def _probe_now(self) -> None:
        cfg = self._cfg
        if not cfg.ws_url:
            self.fail("还没有配置协议端 WS 地址")
            return
        self._vars["endpoint_state"].set("检测中…")
        widgets.run_async(
            self.frame,
            lambda: linkstate.probe_endpoint(cfg.ws_url),
            self._probe_done,
        )

    def _probe_done(self, value: Any, error: BaseException | None) -> None:
        if error is not None:
            self.fail(f"检测失败：{error}")
            return
        self._probe = value
        self._render_endpoint()
        self.notify(value.detail)

    def _start_protocol(self) -> None:
        """启动内置协议端（新控制台窗口；关掉窗口等于下线）。"""
        proto = self._proto
        if proto is None or not proto.available:
            self.fail(proto.reason if proto is not None else "本程序没有内置协议端环境")
            return
        ok, detail = qqproto.launch_background(proto)
        if not ok:
            self.fail(detail)
            return
        self.journal("在线页：启动内置 NapCat 协议端")
        self.notify(detail)
        self._schedule(3000)

    def _open_webui(self) -> None:
        proto = self._proto
        if proto is not None and proto.available and proto.webui_url:
            url = proto.webui_url
            label = "内置 NapCat WebUI（登录二维码见页面）"
        else:
            url = "http://127.0.0.1:6099/webui"
            label = "NapCat WebUI"
        try:
            opened = webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - 打不开浏览器是可展示的失败
            self.fail(f"打不开浏览器：{exc}")
            return
        self.journal(f"在线页：打开 {label}")
        if not opened:
            self.notify(f"没能自动打开浏览器，请手动访问 {url}")

    def _copy_info(self) -> None:
        try:
            paths = self.require_paths()
            secret = linkstate.secret_state(self._cfg, paths.env_path)
        except ConfigEditError:
            secret = "?"
        proto = self._proto
        lines = [
            "ATRI-X QQ 接入信息",
            f"后台服务 WS：{self.settings.url if self.settings else '（未知）'}",
            f"OneBot 协议端：{self._cfg.ws_url or '（未配置）'}",
            f"OneBot 令牌：{'已写入 .env' if secret == 'set' else secret}",
            f"NapCat WebUI：{(proto.webui_url.rsplit('?', 1)[0] if proto and proto.webui_url else 'http://127.0.0.1:6099/webui')}",
        ]
        if proto is not None:
            lines.insert(
                1,
                f"协议端环境：{'内置 ' + proto.env_dir if proto.available else '外部自备（' + proto.reason + '）'}",
            )
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append("\n".join(lines))
        except tk.TclError:  # pragma: no cover - 剪贴板不可用
            self.fail("剪贴板不可用")
            return
        self.journal("在线页：复制连接信息")
        self.notify("连接信息已复制到剪贴板")
