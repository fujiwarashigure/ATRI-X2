"""「在线」选项卡：一条线看住「后台服务 → 协议端 → QQ 平台」三段，并提供上下线开关。

为什么需要这一页：QQ 机器人"看起来在跑"和"真的在线"是两件事 —— 后台服务活着、
协议端端口在听、OneBot 连接真的连上了、QQ 真的登录了，任何一环断掉现象都只是
"群里不回话"。这里把四段状态并排显示，并把「启用/停用 QQ 平台 + 重启后台服务」
做成一个开关：

- **连接（上线）**：校验 ``[platform.qqonebot]`` 的 ws 地址与 token → 写
  ``enabled = true`` → 重启后台服务 → 轮询 ``/healthz`` 直到平台状态变成已连接；
- **断开（下线）**：写 ``enabled = false`` → 重启后台服务。

**上线通知**（1.1.3 修订）：QQ 平台卡片上还有「私聊 / 群聊」两个独立复选框 + 群号
输入框（写 ``app.toml`` 的 ``[connect_notice]``）——私聊发给 ``[security.superadmins]``
里的超管，群聊发给列出的 QQ 群；两个开关互不影响。改完同样要重启后台服务。

**四维状态**（这是本页的核心语义，别再合并成一个"在线/离线"）：

| 维度 | 数据源 |
|---|---|
| 进程运行 | 本程序托管的 PID 记录（只认自己启动的；外置显示"外部自备"） |
| OneBot 连通 | ``/healthz`` 的 ``platforms[].connected``（= WebSocket 通了） |
| QQ 登录 | ``/healthz`` 的 ``platforms[].login_ok``（= ``get_login_info`` 成功；**端口在听不等于登录**） |
| ATRI 平台接入 | ``[platform.qqonebot].enabled`` + 上面的连通状态 |

状态数据来自后台服务的 ``/healthz``（见 :mod:`atri_x.gui.linkstate` 与
:mod:`atri_x.health.server`），**不是**猜日志；协议端端口用一次 TCP 探测，
完整的一次性验证仍然用 ``atri-x.exe -m atri_x.platform.qq_onebot.smoke``。

协议端可以是用户自备的 SnowLuma（1.1.0 起新装默认，外置接入）或内置/外置的 NapCat：
启动/停止/WebUI 入口统一走 :mod:`atri_x.gui.proto_manager`，停止**只停本程序启动的
PID 树**，不按进程名杀。发行包内置 NapCat 时，本页会提示"可在「平台」页切过去用"。
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Any

from atri_x.gui import linkstate, process, proto_manager, service, widgets
from atri_x.gui.configedit import ConfigEditError, patch_section
from atri_x.gui.proto_setup import ProtoSetup
from atri_x.gui.tabs.base import ConfigTab, confirm
from atri_x.platform.qq_onebot.backend import detect_backend

__all__ = ["LinkTab", "REFRESH_INTERVAL_MS"]

#: 状态自动刷新间隔（毫秒）。健康接口在本机回环，2 秒一次开销可以忽略。
REFRESH_INTERVAL_MS = 2000

#: 部署提示：默认 SnowLuma（外置）与内置 NapCat 两条路都要说清楚。
DEPLOY_HINT = (
    "换电脑部署（照顺序）：① 复制本程序目录（内置 NapCat 的发行版自带 NapCat + 便携 QQ，"
    "首次启动自动生成令牌与端口）；② 复制配置目录（%APPDATA%\\ATRI-X，"
    "或用便携版 start-portable.bat 时在包内 data\\ 下）→ 新机器上先启动桌面端。"
    "用 SnowLuma（1.1.0 起新装默认，外置接入）：先自行安装 SnowLuma，"
    "在「平台」页确认协议端是 SnowLuma，再回本页启动并连接上线；"
    "用内置 NapCat：在「平台」页把协议端切成 NapCat、重启桌面端，"
    "再回本页点「启动协议端」扫码登录 → 点「连接（上线）」。"
)


def _split_keywords(text: str) -> list[str]:
    """中英文逗号（兼容顿号）分隔的唤醒词列表；去空、去重、保序。"""
    words: list[str] = []
    for chunk in (text or "").replace("，", ",").replace("、", ",").split(","):
        word = chunk.strip()
        if word and word not in words:
            words.append(word)
    return words


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
        self._proto: ProtoSetup | None = (
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
        wake_row = ttk.Frame(card)
        wake_row.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(wake_row, text="唤醒词").pack(side=tk.LEFT)
        self._wake_var = tk.StringVar()
        self._wake_entry = ttk.Entry(wake_row, textvariable=self._wake_var, width=18)
        self._wake_entry.pack(side=tk.LEFT, padx=(4, 4))
        self._wake_button = ttk.Button(wake_row, text="保存唤醒词", command=self._save_wake_words)
        self._wake_button.pack(side=tk.LEFT)
        ttk.Label(
            card,
            text="逗号分隔；群里 @机器人、或消息以唤醒词开头（如「atri 你好」）都会叫醒。"
            "留空 = 只认 @机器人 与回复。改完重启后台服务生效。",
            foreground="#8a8a8a",
            wraplength=320,
            justify=tk.LEFT,
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))
        # 上线通知：私聊 / 群聊各自独立开关（写 app.toml [connect_notice]，1.1.3 修订）。
        notice_row = ttk.Frame(card)
        notice_row.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(notice_row, text="上线通知").pack(side=tk.LEFT)
        self._notice_private_var = tk.BooleanVar(value=True)
        self._notice_private_box = ttk.Checkbutton(
            notice_row, text="私聊", variable=self._notice_private_var
        )
        self._notice_private_box.pack(side=tk.LEFT, padx=(4, 6))
        self._notice_group_var = tk.BooleanVar(value=True)
        self._notice_group_box = ttk.Checkbutton(
            notice_row, text="群聊", variable=self._notice_group_var
        )
        self._notice_group_box.pack(side=tk.LEFT, padx=(0, 6))
        self._notice_groups_var = tk.StringVar()
        self._notice_groups_entry = ttk.Entry(
            notice_row, textvariable=self._notice_groups_var, width=14
        )
        self._notice_groups_entry.pack(side=tk.LEFT, padx=(0, 4))
        self._notice_button = ttk.Button(
            notice_row, text="保存", command=self._save_connect_notice
        )
        self._notice_button.pack(side=tk.LEFT)
        ttk.Label(
            card,
            text="私聊/群聊互不影响；群号逗号分隔（群聊开关打开但群号留空 = 不发群通知）。"
            "改完重启后台服务生效。",
            foreground="#8a8a8a",
            wraplength=320,
            justify=tk.LEFT,
        ).grid(row=6, column=0, sticky="w", pady=(4, 0))
        actions = ttk.Frame(card)
        actions.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self._connect_button = ttk.Button(actions, text="连接（上线）", command=self._toggle_online)
        self._connect_button.pack(side=tk.LEFT)

        # 协议端
        proto_title = (
            f"协议端（{self._proto.name}）" if self._proto is not None else "协议端（外部自备）"
        )
        card = self._card(grid, proto_title, 0, 2, pady=(0, 6))
        self._label(card, "endpoint_state", row=0, bold=True)
        self._label(card, "endpoint_detail", row=1, wrap=280)
        self._label(card, "endpoint_env", row=2, wrap=280)
        actions = ttk.Frame(card)
        actions.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self._probe_button = ttk.Button(actions, text="检测端口", command=self._probe_now)
        self._probe_button.pack(side=tk.LEFT, padx=(0, 6))
        self._start_button = ttk.Button(actions, text="启动协议端", command=self._start_protocol)
        self._start_button.pack(side=tk.LEFT, padx=(0, 6))
        self._stop_button = ttk.Button(actions, text="停止协议端", command=self._stop_protocol)
        self._stop_button.pack(side=tk.LEFT, padx=(0, 6))
        self._webui_button = ttk.Button(actions, text="打开 WebUI", command=self._open_webui)
        self._webui_button.pack(side=tk.LEFT)
        if self._proto is None or not self._proto.available:
            self._start_button.configure(state="disabled")

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
        self._sync_notice_entries(self._cfg)
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
        self._sync_wake_entry(cfg)
        entry = linkstate.platform_of(report) if report is not None else None
        self._vars["platform_state"].set(
            linkstate.describe_platform(entry, enabled=cfg.enabled, service_known=report is not None)
        )
        if entry is not None and entry.connected and entry.login_ok:
            color = "#1a7f37"
        elif entry is not None and entry.connected:
            # 连上了但 QQ 没登录（或还没握手）：不能显示成"可用"的绿色。
            color = "#b26a00"
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
            if entry.backend:
                extra.append(f"协议端 {entry.backend}")
            if entry.login_checked:
                extra.append("QQ 登录：" + ("已登录" if entry.login_ok else "未登录"))
            if entry.app_name:
                detected = detect_backend(entry.app_name)
                if detected and entry.backend and detected != entry.backend:
                    extra.append(
                        f"后端不一致：配置 {entry.backend}，实际连上 {detected}（{entry.app_name}）"
                    )
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
        """四维状态：进程 / OneBot 连通 / QQ 登录 / ATRI 接入（**互不代替**）。"""
        cfg = self._cfg
        proto = self._proto
        entry = linkstate.platform_of(self._report) if self._report is not None else None
        self._vars["endpoint_env"].set(self._describe_proto_env(proto))
        if not cfg.ws_url:
            self._vars["endpoint_state"].set("未配置")
            self._vars["endpoint_detail"].set("到「平台」页填 [platform.qqonebot].ws_url 后再回来。")
            return

        process_text = self._describe_process(proto)
        connect_text = "OneBot：已连通" if entry is not None and entry.connected else "OneBot：未连通"
        if entry is None:
            connect_text = "OneBot：未知（后台服务状态未取到）"
        login_text = self._describe_login(entry)
        atri_text = "ATRI 接入：{}".format("已启用" if cfg.enabled else "未启用")
        probe = self._probe
        if probe is None:
            port_text = "端口：未检测"
        else:
            port_text = "端口：" + ("可达（仅说明有进程在听，不代表 QQ 已登录）" if probe.reachable else "不可达")

        # 主状态：先看"能不能收消息"，再看卡在哪一层。
        if entry is not None and entry.connected and entry.login_checked and not entry.login_ok:
            head = "协议端已连上，但 QQ 未登录"
        elif entry is not None and entry.connected and entry.login_ok:
            head = "QQ 已登录（可用）"
        elif entry is not None and entry.connected:
            head = "OneBot 已连通（登录态未确认）"
        elif probe is not None and probe.reachable:
            head = "端口在听，但 OneBot 未连通"
        else:
            head = "协议端未就绪"
        self._vars["endpoint_state"].set(head)
        detail_lines = [
            f"进程：{process_text}",
            connect_text,
            login_text,
            atri_text,
            port_text,
        ]
        if probe is not None and not probe.reachable:
            detail_lines.append(probe.detail)
        if proto is not None and not proto.available and proto.reason:
            detail_lines.append(proto.reason)
        elif proto is not None and proto.needs_consent:
            detail_lines.append("SnowLuma 还没完成首启（同意 EULA/隐私 + 设 WebUI 密码）")
        elif not probe or not probe.reachable:
            detail_lines.append("协议端没起？点「启动协议端」再检测。")
        self._vars["endpoint_detail"].set("\n".join(detail_lines))

    def _describe_process(self, proto: ProtoSetup | None) -> str:
        """"进程运行"这一维：只认本程序托管的 PID，外置一律显示未托管。"""
        if proto is None or not proto.env_dir:
            return "外部自备（未托管）"
        try:
            info = proto_manager.status(proto)
        except Exception:  # noqa: BLE001 - 状态查询失败不该让页面崩
            return "未知"
        if info.get("managed"):
            return f"运行中（本程序托管，PID {info.get('pid')}）"
        reason = str(info.get("reason") or "")
        if reason:
            return f"未运行或未托管（{reason}）"
        return "未运行（外置协议端请自行确认）"

    def _describe_login(self, entry: Any) -> str:
        """``QQ 登录``这一维：``login_checked`` 为假表示后台还没握过手。"""
        if entry is None:
            return "QQ 登录：未知（后台服务状态未取到）"
        if not entry.login_checked:
            return "QQ 登录：未确认（后台服务还没握手；端口在听不等于已登录）"
        if entry.login_ok:
            who = entry.nickname or "机器人"
            account = f"（{entry.self_id}）" if entry.self_id else ""
            return f"QQ 登录：已登录 {who}{account}"
        return "QQ 登录：未登录"

    def _describe_proto_env(self, proto: ProtoSetup | None) -> str:
        """协议端环境一行说明（后端名 + 目录/不可用原因）。"""
        if proto is None:
            return "协议端：外部自备（本程序没有内置环境）"
        if not proto.available:
            return f"{proto.name}：不可用（{proto.reason}）"
        where = "外部自备" if proto.external else "内置"
        return f"{proto.name}：{where}（{proto.env_dir}）"

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
                f"{probe.detail}。\n\n协议端没启动时后台会一直重连、机器人不会上线。"
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
            self._cfg = linkstate.read_platform_config(paths.config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal(f"在线页：platform.qqonebot.enabled -> {str(target).lower()}")
        self._render_platform()
        if not self.can_control_service:
            self.notify("已写入配置；桌面端没有托管后台服务，请手动重启后生效")
            self._refresh()
            return
        self._restart("连接 QQ" if target else "断开 QQ")

    def _save_wake_words(self) -> None:
        """把「唤醒词」输入框写进 ``[platform.qqonebot].trigger_keywords``。"""
        try:
            paths = self.require_paths()
            words = _split_keywords(self._wake_var.get())
            if not words and not confirm(
                self.frame,
                "清空唤醒词",
                "清空后群里只能靠 @机器人 或回复机器人消息叫醒。继续？",
            ):
                return
            patch_section(
                paths.config_path,
                "platform.qqonebot",
                {"trigger_keywords": words or None},
            )
            self._cfg = linkstate.read_platform_config(paths.config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._sync_wake_entry(self._cfg)
        self._render_platform()
        self.journal(
            "在线页：保存唤醒词 " + ("、".join(words) if words else "（清空，只认 @ 与回复）")
        )
        self.notify(
            f"已保存唤醒词：{'、'.join(words)}；重启后台服务后生效"
            if words
            else "已清空唤醒词；重启后台服务后生效"
        )

    def _sync_notice_entries(self, cfg: linkstate.PlatformConfig) -> None:
        """把配置里的上线通知开关填进控件（用户正在输入群号时不覆盖文本框）。"""
        self._notice_private_var.set(bool(cfg.connect_private))
        self._notice_group_var.set(bool(cfg.connect_group))
        try:
            focused = self.frame.focus_get()
        except (KeyError, tk.TclError):  # pragma: no cover - 窗口销毁中
            focused = None
        if focused is self._notice_groups_entry:
            return
        text = "、".join(cfg.connect_groups)
        if self._notice_groups_var.get() != text:
            self._notice_groups_var.set(text)

    def _save_connect_notice(self) -> None:
        """把「上线通知」的私聊/群聊开关与群号写进 app.toml ``[connect_notice]``。"""
        try:
            paths = self.require_paths()
            linkstate.set_connect_notice(
                paths.config_path,
                private=bool(self._notice_private_var.get()),
                group=bool(self._notice_group_var.get()),
                groups=_split_keywords(self._notice_groups_var.get()),
            )
            self._cfg = linkstate.read_platform_config(paths.config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self._sync_notice_entries(self._cfg)
        groups = "、".join(self._cfg.connect_groups) or "（空）"
        self.journal(
            "在线页：上线通知 private="
            + str(self._cfg.connect_private).lower()
            + " group="
            + str(self._cfg.connect_group).lower()
            + " groups="
            + groups
        )
        self.notify("已保存上线通知设置；重启后台服务后生效")

    def _sync_wake_entry(self, cfg: linkstate.PlatformConfig) -> None:
        """把配置里的唤醒词填进输入框（用户正在输入时不覆盖）。"""
        try:
            focused = self.frame.focus_get()
        except (KeyError, tk.TclError):  # pragma: no cover - 窗口销毁中
            focused = None
        if focused is self._wake_entry:
            return
        text = "、".join(cfg.trigger_keywords)
        if self._wake_var.get() != text:
            self._wake_var.set(text)

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
        buttons = [
            self._connect_button,
            self._probe_button,
            self._webui_button,
            self._copy_button,
            self._wake_button,
            self._stop_button,
        ]
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
        """启动协议端（新控制台窗口；关掉窗口等于下线）。"""
        proto = self._proto
        if proto is None or not proto.available:
            self.fail(proto.reason if proto is not None else "本程序没有可用的协议端环境")
            return
        ok, detail = proto_manager.launch(proto)
        if not ok:
            self.fail(detail)
            return
        self.journal(f"在线页：启动 {proto.name} 协议端")
        self.notify(detail)
        self._schedule(3000)

    def _stop_protocol(self) -> None:
        """停止**本程序启动的**协议端进程树（不按进程名杀，不碰外置进程）。"""
        proto = self._proto
        if proto is None or not proto.env_dir:
            self.fail("没有可停止的托管协议端")
            return
        ok, detail = proto_manager.stop(proto)
        self.journal(f"在线页：停止 {proto.name} 协议端 -> {detail}")
        if ok:
            self.notify(detail)
        else:
            self.fail(detail)
        self._render_endpoint()

    def _open_webui(self) -> None:
        proto = self._proto
        url = proto_manager.webui_url(proto) if proto is not None else ""
        if not url:
            self.fail(
                proto.reason if proto is not None and proto.reason else "还没有可用的协议端 WebUI 地址"
            )
            return
        label = f"{proto.name} WebUI" if proto is not None else "协议端 WebUI"
        if proto is not None and proto.backend == "snowluma":
            label += "（账号密码登录；首启密码只在它自己的控制台里显示）"
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
        proto_name = proto.name if proto is not None else "协议端"
        lines = [
            "ATRI-X QQ 接入信息",
            f"后台服务 WS：{self.settings.url if self.settings else '（未知）'}",
            f"协议端类型：{proto_name}",
            f"OneBot 协议端：{self._cfg.ws_url or '（未配置）'}",
            f"OneBot 令牌：{'已写入 .env' if secret == 'set' else secret}",
            f"{proto_name} WebUI：{proto_manager.webui_url(proto) if proto else '（未配置）'}",
        ]
        if proto is not None:
            lines.insert(
                1,
                f"协议端环境：{'外部自备 ' + proto.env_dir if proto.external else ('内置 ' + proto.env_dir if proto.available else '不可用（' + proto.reason + '）')}",
            )
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append("\n".join(lines))
        except tk.TclError:  # pragma: no cover - 剪贴板不可用
            self.fail("剪贴板不可用")
            return
        self.journal("在线页：复制连接信息")
        self.notify("连接信息已复制到剪贴板")
