"""「时间」选项卡：统一时间服务（``[time]`` / ``[time.verification]`` / ``[[time.sources]]``）。

1.0.10 起业务时间有唯一口径（:mod:`atri_x.time_service`），网络校验（SNTP / HTTPS 时间
API，默认关闭）只做观测、只写 ``/diagnostics``。这个页面把**能改的**和**能看的**都摆出来：

| 区块 | 性质 | 内容 |
|---|---|---|
| 现在 | 只读，每秒刷新 | 时区/偏移/当前时间/UTC/星期/unix/tzdata/注入开关 |
| 时区探针 | 只读，手动触发 | tzdata 是否可读 + DST 冬夏偏移是否正确（不触网） |
| ``[time]`` | 可编辑 | ``timezone`` / ``inject_context`` / ``max_sample_age_seconds`` |
| ``[time.verification]`` | 可编辑 | 开关、模式与 13 个阈值/间隔/并发参数 |
| ``[[time.sources]]`` | 可编辑（表格） | 每条来源的 id/协议/地址/分组/私网/启用/超时 |
| 后台运行状态 | 只读，来自 ``/diagnostics`` | 校验状态、最近一轮、下一轮、样本年龄、偏差、每条来源最近观测、告警 |

为什么"现在"用本地时钟算而不是问后台：这一页要回答的是"配置成这样之后，时间口径长什么
样"。所以用与后台**同一个** :class:`atri_x.time_service.TimeService`（同一份默认值、同一
份时区数据），先算给你看，再点保存；要不要让后台也生效由"重启后台服务"决定。网络校验的
**运行状态**只在后台进程里，所以那一段走 :mod:`atri_x.gui.timediagnostics`
（读 ``/diagnostics``）；来源表格与表单在 :mod:`atri_x.gui.timesources`。

保存走 :mod:`atri_x.gui.configedit` 的行级补丁：``[time]`` 与 ``[time.verification]`` 只
改这几个键（不动注释），``[[time.sources]]`` 是数组表，用
:func:`atri_x.gui.configedit.write_array_tables` 整份重写。界面校验与后台共用同一份
``TimeConfig.normalized()``，非法值**不落盘**。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.config.inspect import ConfigDecodeError
from atri_x.config.time_sections import (
    TIME_MODES,
    TimeConfig,
    TimeSourceConfig,
    TimeVerificationConfig,
)
from atri_x.gui import timeview, widgets
from atri_x.gui.configedit import ConfigEditError, patch_section, write_array_tables
from atri_x.gui.process import open_path
from atri_x.gui.tabs.base import ConfigTab, message
from atri_x.gui.timediagnostics import DiagnosticsPanel
from atri_x.gui.timesources import SourceEditor

__all__ = ["TimeTab", "TICK_MS"]

#: 「现在」区块的刷新间隔（毫秒）：一秒够看，且不占 CPU。
TICK_MS = 1000

#: ``[[time.sources]]`` 的数组表名。
SOURCES_TABLES = "time.sources"

#: ``[time.verification]`` 的数值项：(配置键, 界面名, 取值范围提示)。
_VERIFICATION_FIELDS = (
    ("interval_seconds", "校验间隔（秒）", "60–604800；实际间隔再叠加 ±jitter"),
    ("jitter_ratio", "抖动比例", "0–0.5（避免多实例同刻打同一来源）"),
    ("timeout_seconds", "单来源超时（秒）", "0.5–10"),
    ("max_concurrency", "单轮并发上限", "1–4"),
    ("max_retries", "单来源重试次数", "0–3（不含首次）"),
    ("round_deadline_seconds", "整轮 deadline（秒）", "2–60"),
    ("warn_offset_ms", "告警阈值（毫秒）", "1–3600000，且必须小于冲突阈值"),
    ("conflict_offset_ms", "冲突阈值（毫秒）", "1–86400000"),
    ("min_groups", "最少独立分组", "1–4；同一 group 只算一份证据"),
    ("max_sources", "单轮最多来源数", "1–8"),
    ("manual_min_interval_seconds", "手动校验最小间隔（秒）", "10–86400"),
    ("max_plausible_skew_seconds", "可疑偏差上限（秒）", "60–31536000"),
    ("suspend_threshold_seconds", "挂起/校时判定阈值（秒）", "1–300"),
)

#: 「后台运行状态」区块的一句话说明。
_DIAGNOSTICS_HINT = (
    "网络校验的运行状态只存在于后台进程里：这里读的是受 token 保护的只读诊断接口，"
    "界面不会替你触发校验（手动触发用 /time verify）。"
)


class TimeTab(ConfigTab):
    """统一时间服务：实时读数 + 时区探针 + 全部时间配置 + 后台校验状态。"""

    hint = "时间配置改动需要重启后台服务才生效"
    scrollable = True

    def __init__(self, parent: Any, paths: Any, *, on_restart: Any = None) -> None:
        self._clock_vars: dict[str, tk.StringVar] = {}
        self._clock_ready = False
        self._tick_job: str | None = None
        super().__init__(parent, paths, on_restart=on_restart)
        self._build()
        self.reload()

    # ---------------------------------------------------------------- 构建
    def _build(self) -> None:
        ttk.Label(
            self.body,
            text=(
                "时间口径的唯一来源是 app.toml 的 [time]：提示词里的“现在”、cron / 提醒 / "
                "静默时段的时区判定、get_current_time 工具与 /diagnostics 的时间段都取同一份。"
                "网络校验（SNTP / HTTPS 时间 API）默认关闭，只做观测、只写诊断。"
            ),
            foreground="#555555",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))

        self._build_clock_box()
        self._build_time_box()
        self._build_verification_box()
        self._build_sources_box()
        self._build_diagnostics_box()

        ttk.Label(
            self.body,
            text=timeview.tzdata_summary(),
            foreground="#8a8a8a",
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

        self.button("保存到 app.toml", self._save)
        self.button("重新载入", self.reload)
        self.button("读取后台状态", self._fetch_diagnostics)
        self.button("打开 app.toml", self._open_app_file)
        self.button("打开配置目录", self._open_config_dir)

    # -- 现在 ---------------------------------------------------------------
    def _build_clock_box(self) -> None:
        box = ttk.LabelFrame(self.body, text="现在（只读，每秒刷新；按上面的时区计算）", padding=8)
        box.pack(fill=tk.X, pady=(0, 6))
        bar = ttk.Frame(box)
        bar.pack(fill=tk.X)
        self.clock_error = ttk.Label(
            bar, text="", foreground="#b42318", wraplength=760, justify=tk.LEFT
        )
        self.clock_error.pack(side=tk.LEFT)
        ttk.Button(bar, text="刷新", command=self._refresh_clock).pack(side=tk.RIGHT)
        ttk.Button(bar, text="时区探针", command=self._probe).pack(side=tk.RIGHT, padx=(0, 6))
        self._clock_grid = ttk.Frame(box)
        self._clock_grid.pack(fill=tk.X, pady=(4, 0))
        self._probe_text = tk.Text(box, height=6, wrap=tk.WORD, font=("Microsoft YaHei UI", 9))
        self._probe_text.pack(fill=tk.X, pady=(6, 0))
        self._probe_text.configure(state=tk.DISABLED)
        self._set_text(
            self._probe_text,
            ["点上面的「时区探针」检查 tzdata 能否真的加载、DST 冬夏偏移是否正确（不触网）。"],
        )

    def _ensure_clock_rows(self, lines: list[tuple[str, str]]) -> None:
        """首次拿到数据时按字段建行（字段集合由数据层决定，界面不写死）。"""
        if self._clock_ready:
            return
        self._clock_ready = True
        for index, (label, _value) in enumerate(lines):
            row, column = divmod(index, 2)
            cell = ttk.Frame(self._clock_grid)
            cell.grid(row=row, column=column, sticky="w", padx=(0, 26), pady=1)
            ttk.Label(cell, text=label + "：", foreground="#555555").pack(side=tk.LEFT)
            variable = tk.StringVar(value="—")
            self._clock_vars[label] = variable
            ttk.Label(cell, textvariable=variable).pack(side=tk.LEFT)

    def _refresh_clock(self) -> None:
        try:
            lines = timeview.describe_clock(
                self.timezone_var.get().strip(),
                inject_context=bool(self.inject_var.get()),
            )
        except timeview.TimeViewError as exc:
            self.clock_error.configure(text=str(exc))
            return
        self.clock_error.configure(text="")
        self._ensure_clock_rows(lines)
        for label, value in lines:
            variable = self._clock_vars.get(label)
            if variable is not None:
                variable.set(value)

    def _probe(self) -> None:
        report = timeview.probe_timezone(self.timezone_var.get().strip())
        lines = [f"{label}：{value}" for label, value in timeview.probe_lines(report)]
        self._set_text(self._probe_text, lines)
        self.notify("时区探针：" + ("通过" if report.get("ok") else "失败（详见上方输出）"))

    def _tick(self) -> None:
        self._tick_job = None
        if not self.is_ready():
            return
        self._refresh_clock()
        try:
            self._tick_job = self.frame.after(TICK_MS, self._tick)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            self._tick_job = None

    def _start_tick(self) -> None:
        if self._tick_job is None:
            self._tick()

    def _stop_tick(self) -> None:
        if self._tick_job is None:
            return
        try:
            self.frame.after_cancel(self._tick_job)
        except tk.TclError:  # pragma: no cover - 窗口销毁中
            pass
        self._tick_job = None

    # -- [time] -------------------------------------------------------------
    def _build_time_box(self) -> None:
        box = ttk.LabelFrame(self.body, text="[time] 时间口径（写 app.toml）", padding=8)
        box.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(box, text="时区").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=2)
        self.timezone_var = tk.StringVar()
        self._timezone_box = widgets.dropdown(
            box, self.timezone_var, values=timeview.timezone_choices(), width=32, height=16
        )
        self._timezone_box.grid(row=0, column=1, sticky="w", pady=2)
        self._timezone_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_clock())
        ttk.Label(
            box,
            text="IANA 名 / fixed:+08:00 / system；写错保存即报错，启动也不会静默回落",
            foreground="#8a8a8a",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))

        self.inject_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box,
            text="把当前时间注入每轮系统提示（只进本次请求，不落库）",
            variable=self.inject_var,
            command=self._refresh_clock,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Label(box, text="网络样本有效期（秒）").grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=2
        )
        self.max_age_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.max_age_var, width=12).grid(
            row=2, column=1, sticky="w", pady=2
        )
        ttk.Label(
            box,
            text="60–31536000；超过这个年龄的样本判 stale（默认 21600 = 6 小时）",
            foreground="#8a8a8a",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))

    # -- [time.verification] ------------------------------------------------
    def _build_verification_box(self) -> None:
        box = ttk.LabelFrame(
            self.body, text="[time.verification] 网络校验（只观测，不接管业务时间）", padding=8
        )
        box.pack(fill=tk.X, pady=(0, 6))
        head = ttk.Frame(box)
        head.grid(row=0, column=0, columnspan=2, sticky="w")
        self.verify_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            head,
            text="启用网络时间校验（关闭时不会发起任何网络请求）",
            variable=self.verify_enabled_var,
        ).pack(side=tk.LEFT)
        ttk.Label(head, text="模式").pack(side=tk.LEFT, padx=(18, 6))
        self.verify_mode_var = tk.StringVar(value=TIME_MODES[0])
        ttk.Combobox(
            head,
            textvariable=self.verify_mode_var,
            values=list(TIME_MODES),
            state="readonly",
            width=16,
        ).pack(side=tk.LEFT)
        ttk.Label(
            head,
            text="首版只支持 verify_only（mode=takeover 会被配置层显式拒绝）",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(8, 0))

        self._verification_vars: dict[str, tk.StringVar] = {}
        for index, (key, label, hint) in enumerate(_VERIFICATION_FIELDS):
            row, column = divmod(index, 2)
            cell = ttk.Frame(box)
            cell.grid(row=row + 1, column=column, sticky="w", padx=(0, 26), pady=2)
            ttk.Label(cell, text=label).pack(side=tk.LEFT)
            variable = tk.StringVar()
            self._verification_vars[key] = variable
            ttk.Entry(cell, textvariable=variable, width=12).pack(side=tk.LEFT, padx=(6, 0))
            ttk.Label(cell, text=hint, foreground="#8a8a8a").pack(side=tk.LEFT, padx=(6, 0))

    # -- [[time.sources]] ---------------------------------------------------
    def _build_sources_box(self) -> None:
        box = ttk.LabelFrame(self.body, text="[[time.sources]] 时间来源（写 app.toml）", padding=8)
        box.pack(fill=tk.BOTH, expand=False, pady=(0, 6))
        ttk.Label(
            box,
            text=(
                "每条 [[time.sources]] 是一个网络时间来源；不同分组（group）才算独立证据，"
                "至少 min_groups 份独立证据才能判“一致”。默认不内置任何公共端点。"
            ),
            foreground="#8a8a8a",
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))
        self.sources_editor = SourceEditor(box, on_notice=self.notify)
        self.sources_editor.pack(fill=tk.BOTH, expand=True)

    # -- 后台状态 -----------------------------------------------------------
    def _build_diagnostics_box(self) -> None:
        box = ttk.LabelFrame(
            self.body, text="后台运行状态（只读，来自后台服务的 /diagnostics）", padding=8
        )
        box.pack(fill=tk.BOTH, expand=False, pady=(0, 6))
        ttk.Label(
            box, text=_DIAGNOSTICS_HINT, foreground="#8a8a8a", wraplength=960, justify=tk.LEFT
        ).pack(anchor="w", pady=(0, 6))
        self.diagnostics_panel = DiagnosticsPanel(box, self.paths, on_notice=self.notify)
        self.diagnostics_panel.pack(fill=tk.BOTH, expand=True)

    # ---------------------------------------------------------------- 载入
    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            view = timeview.load_time_config(self.require_paths())
        except timeview.TimeViewError as exc:
            self.fail(exc)
            return
        self.timezone_var.set(view.timezone)
        self.inject_var.set(view.inject_context)
        self.max_age_var.set(str(view.max_sample_age_seconds))
        self.verify_enabled_var.set(bool(view.verification.get("enabled")))
        mode = str(view.verification.get("mode") or TIME_MODES[0]).strip()
        self.verify_mode_var.set(mode if mode in TIME_MODES else TIME_MODES[0])
        for key, _label, _hint in _VERIFICATION_FIELDS:
            value = view.verification.get(key)
            self._verification_vars[key].set("" if value is None else str(value))
        self.sources_editor.load(view.sources)
        self._refresh_clock()
        self.notify("已载入 app.toml 的时间配置（改动需要重启后台服务）")

    # ---------------------------------------------------------------- 保存
    def _save(self) -> None:
        try:
            config = self._collect_config()
            changed = self._write_config(config)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.journal("保存时间服务配置（[time] / [time.verification] / [[time.sources]]）")
        # 先回读磁盘（归一化后的值可能与输入写法不同），再写状态行——
        # 顺序反过来的话 reload 的"已载入"会把"已写入"盖掉。
        self.reload()
        self.notify("已写入 app.toml；重启后台服务后生效" if changed else "配置没有变化")

    def _collect_config(self) -> TimeConfig:
        """把界面值收成 :class:`TimeConfig` 并让配置层做唯一一次校验。"""
        verification_values: dict[str, Any] = {
            "enabled": bool(self.verify_enabled_var.get()),
            "mode": self.verify_mode_var.get().strip(),
        }
        for key, label, _hint in _VERIFICATION_FIELDS:
            verification_values[key] = self._number_for(key, label)
        sources: list[TimeSourceConfig] = []
        for index, item in enumerate(self.sources_editor.values()):
            try:
                sources.append(TimeSourceConfig(**item))
            except TypeError as exc:  # pragma: no cover - 键集合由界面固定
                raise ConfigEditError(f"来源 #{index + 1} 字段不合法：{exc}") from exc
        config = TimeConfig(
            timezone=self.timezone_var.get().strip(),
            inject_context=bool(self.inject_var.get()),
            max_sample_age_seconds=_int_value(
                "网络样本有效期", self.max_age_var.get(), 21600
            ),
            verification=TimeVerificationConfig(**verification_values),
            sources=sources,
        )
        try:
            return config.normalized()
        except ConfigDecodeError as exc:
            raise ConfigEditError(str(exc)) from exc

    def _write_config(self, config: TimeConfig) -> bool:
        paths = self.require_paths()
        verification = config.verification
        changed = patch_section(
            paths.config_path,
            "time",
            {
                "timezone": config.timezone,
                "inject_context": bool(config.inject_context),
                "max_sample_age_seconds": int(config.max_sample_age_seconds),
            },
        )
        changed = patch_section(
            paths.config_path,
            "time.verification",
            {
                "enabled": bool(verification.enabled),
                "mode": verification.mode,
                "interval_seconds": int(verification.interval_seconds),
                "jitter_ratio": float(verification.jitter_ratio),
                "timeout_seconds": float(verification.timeout_seconds),
                "max_concurrency": int(verification.max_concurrency),
                "max_retries": int(verification.max_retries),
                "round_deadline_seconds": float(verification.round_deadline_seconds),
                "warn_offset_ms": int(verification.warn_offset_ms),
                "conflict_offset_ms": int(verification.conflict_offset_ms),
                "min_groups": int(verification.min_groups),
                "max_sources": int(verification.max_sources),
                "manual_min_interval_seconds": int(verification.manual_min_interval_seconds),
                "max_plausible_skew_seconds": int(verification.max_plausible_skew_seconds),
                "suspend_threshold_seconds": float(verification.suspend_threshold_seconds),
            },
        ) or changed
        changed = write_array_tables(
            paths.config_path,
            SOURCES_TABLES,
            [_source_block(source) for source in config.sources],
            after_section="time.verification",
        ) or changed
        return changed

    # ---------------------------------------------------------------- 后台状态
    def _fetch_diagnostics(self) -> None:
        self.diagnostics_panel.fetch()

    # ---------------------------------------------------------------- 数值
    def _number_for(self, key: str, label: str) -> Any:
        """按字段类型把输入框收成 int / float（空 = 用配置层默认值）。"""
        default = timeview.verification_defaults().get(key, 0)
        raw = self._verification_vars[key].get().strip()
        if isinstance(default, int):
            return _int_value(label, raw, int(default))
        return _float_value(label, raw, float(default))

    # ---------------------------------------------------------------- 其它
    def on_show(self) -> None:
        self._refresh_clock()
        self._start_tick()
        self._fetch_diagnostics()

    def on_hide(self) -> None:
        self._stop_tick()

    def _open_app_file(self) -> None:
        path = self.require_paths().config_path
        if not os.path.exists(path):
            message(self.frame, "app.toml", f"找不到文件：{path}")
            return
        if not open_path(path):
            message(self.frame, "app.toml", f"文件：{path}")

    def _open_config_dir(self) -> None:
        directory = self.require_paths().config_dir
        if not open_path(directory):
            message(self.frame, "配置目录", f"打不开目录：{directory}")

    @staticmethod
    def _set_text(widget: tk.Text, lines: list[str]) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert("1.0", "\n".join(lines))
        widget.configure(state=tk.DISABLED)


def _source_block(source: TimeSourceConfig) -> dict[str, Any]:
    """一条来源 → ``[[time.sources]]`` 块（按字段顺序写全，便于手改）。"""
    return {
        "id": source.id,
        "protocol": source.protocol,
        "host": source.host,
        "port": int(source.port),
        "url": source.url,
        "group": source.group,
        "enabled": bool(source.enabled),
        "allow_private": bool(source.allow_private),
        "max_body_bytes": int(source.max_body_bytes),
        "require_no_store": bool(source.require_no_store),
        "timeout_seconds": float(source.timeout_seconds or 0.0),
    }


def _int_value(label: str, text: str, default: int) -> int:
    """输入框 → int；空串用默认值，非法值按配置层的口吻报错。"""
    raw = (text or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigEditError(f"{label}必须是整数：{raw!r}") from None


def _float_value(label: str, text: str, default: float) -> float:
    """输入框 → float；空串用默认值，非法值按配置层的口吻报错。"""
    raw = (text or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigEditError(f"{label}必须是数字：{raw!r}") from None
