"""「DSH」页的三个子区块：内核模型目录、状态与自检、一键配置。

宿主是 :class:`atri_x.gui.tabs.dsh.DshTab`（本模块以 mixin 形式拆出来，避免单文件
超过仓库的 600 行目标）。这里只管界面与动作，配置路径 / 状态行 / 弹窗 / 界面日志
都通过宿主提供（``tab.require_paths()`` / ``tab.notify`` / ``tab.fail`` /
``tab.journal`` / ``tab.frame`` / ``tab.on_restart``），不复制第二套。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import dshcatalog, dshconfig, dshsmoke, widgets
from atri_x.gui.configedit import ConfigEditError
from atri_x.gui.process import open_path
from atri_x.gui.tabs.base import confirm, message

__all__ = ["DshPanelsMixin"]

#: 打分试算的示例文本（输入框留空时用它）。
_SAMPLE_TEXT = "重构 utils.py，拆掉重复分支并补单元测试"


class DshPanelsMixin:
    """给 :class:`~atri_x.gui.tabs.dsh.DshTab` 提供模型目录 / 状态 / 自检区块。"""

    # -- 构建 ---------------------------------------------------------------
    def _build_catalog(self, parent: Any) -> None:
        box = ttk.LabelFrame(
            parent, text="内核模型目录（写 profiles/acp/cordis.patch.yml）", padding=8
        )
        box.pack(fill=tk.X, pady=(0, 8))
        self.model_var = tk.StringVar()
        ttk.Label(box, text="默认模型", width=12).grid(row=0, column=0, sticky="w")
        self.model_box = widgets.dropdown(
            box, self.model_var, width=34, height=12, on_open=self._refresh_model_values
        )
        self.model_box.grid(row=0, column=1, sticky="w")
        self.catalog_state_var = tk.StringVar()
        ttk.Label(
            box, textvariable=self.catalog_state_var, foreground="#555555", wraplength=940
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        columns = ("model", "window", "max")
        self.model_tree = ttk.Treeview(box, columns=columns, show="headings", height=4)
        self.model_tree.heading("model", text="模型 id（内核目录）")
        self.model_tree.heading("window", text="上下文窗口")
        self.model_tree.heading("max", text="输出上限")
        self.model_tree.column("model", width=300, stretch=False)
        self.model_tree.column("window", width=110, stretch=False)
        self.model_tree.column("max", width=110, stretch=False)
        self.model_tree.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        buttons = ttk.Frame(box)
        buttons.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="按 Provider 生成模型目录", command=self._generate_catalog).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="清空（回内核自带目录）", command=self._clear_catalog).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(buttons, text="打开 DSH 数据目录", command=self._open_dsh_dir).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(
            buttons,
            text="  模型清单取所选 Provider 的 models，上下文窗口取「模型 → 上下文窗口」页",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT)

    def _build_status(self, parent: Any) -> None:
        # 说明：这里**不用** grid + `rowconfigure(weight=1)` 把 Text 撑满——那会让
        # 「状态区底部的按钮行」被挤到 LabelFrame 可视区之外（滚动画布高度有限，
        # 表现为按钮看得见却点不到/干脆被裁掉）。状态框固定高度、按钮紧随其后。
        box = ttk.LabelFrame(parent, text="状态与自检（只读，不写配置）", padding=8)
        box.pack(fill=tk.X, pady=(10, 0))
        text_row = ttk.Frame(box)
        text_row.pack(fill=tk.X)
        self._status_text = tk.Text(text_row, height=9, wrap=tk.WORD, font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(text_row, orient=tk.VERTICAL, command=self._status_text.yview)
        self._status_text.configure(yscrollcommand=scrollbar.set, state=tk.DISABLED)
        self._status_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        buttons = ttk.Frame(box)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="刷新状态", command=self._refresh_status).pack(side=tk.LEFT)
        self.score_var = tk.StringVar(value=_SAMPLE_TEXT)
        ttk.Label(buttons, text="  打分试算", foreground="#555555").pack(side=tk.LEFT, padx=(10, 4))
        ttk.Entry(buttons, textvariable=self.score_var, width=34).pack(side=tk.LEFT)
        ttk.Button(buttons, text="试算", command=self._score_test).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            buttons,
            text="  端到端自检在底部按钮条（会拉起内核真跑一轮 prompt）",
            foreground="#8a8a8a",
        ).pack(side=tk.LEFT, padx=(10, 0))

    # -- 模型目录 -----------------------------------------------------------
    def _fill_catalog_widgets(self) -> None:
        ids = self.catalog.ids()
        self.model_box.configure(values=ids)
        current = self.settings.model_id()
        if current and current in ids:
            self.model_var.set(current)
        elif current:
            self.model_box.configure(values=[current] + ids)
            self.model_var.set(current)
        elif ids:
            self.model_var.set(self.catalog.model if self.catalog.model in ids else ids[0])
        else:
            self.model_var.set("")
        self._fill_catalog_tree()

    def _fill_catalog_tree(self) -> None:
        self.model_tree.delete(*self.model_tree.get_children())
        for item in self.catalog.models:
            self.model_tree.insert(
                "",
                tk.END,
                values=(item.id, f"{item.context_window:,}", f"{item.max_tokens:,}"),
            )
        path = dshcatalog.patch_path(self.require_paths()) if self.is_ready() else ""
        if self.catalog.present and self.catalog.models:
            text = f"补丁文件：{path}（{len(self.catalog.models)} 个模型）"
        elif path and os.path.exists(path):
            text = f"补丁文件：{path}（已清空 → 内核用自带目录）"
        else:
            text = (
                f"还没有模型目录补丁：{path}。"
                "网关的模型 id 和内核自带目录对不上时，点「按 Provider 生成模型目录」。"
            )
        self.catalog_state_var.set(text)

    def _refresh_model_values(self) -> None:
        if not self.is_ready():
            return
        self.catalog = dshcatalog.load_kernel_catalog(self.require_paths())
        self._fill_catalog_widgets()

    def _generate_catalog(self) -> None:
        """按所选 Provider 的模型清单重写内核模型目录补丁。"""
        if not self.is_ready():
            self.fail("找不到 app.toml，无法写内核模型目录")
            return
        paths = self.require_paths()
        pick = self._selected_provider() or self._auto_provider()
        if not pick:
            self.fail("没有可用的 Provider：先在「模型」页添加一个")
            return
        count = len(getattr(self._providers.get(pick), "models", []) or [])
        if not confirm(
            self.frame,
            "生成内核模型目录",
            f"将按 Provider {pick} 的 {count} 个模型重写内核模型目录补丁：\n"
            f"{dshcatalog.patch_path(paths)}\n\n旧文件会备份为 .gui.bak。继续吗？",
        ):
            return
        try:
            catalog = dshcatalog.catalog_from_provider(paths, pick, existing=self.catalog)
            changed = dshcatalog.save_kernel_catalog(paths, catalog)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.catalog = dshcatalog.load_kernel_catalog(paths)
        self._fill_catalog_widgets()
        if catalog.models:
            self.model_var.set(catalog.models[0].id)
        self._refresh_hint()
        self.journal(f"生成内核模型目录：provider={pick!r} models={catalog.ids()}")
        prefix = "已写入" if changed else "内容没有变化"
        self.notify(
            f"{prefix}内核模型目录（{len(catalog.models)} 个模型）；记得点「保存」写入默认模型"
        )

    def _clear_catalog(self) -> None:
        """清空模型目录补丁（内核回退自带目录，官方 DeepSeek 密钥场景用）。"""
        if not self.is_ready():
            return
        if not confirm(
            self.frame,
            "清空内核模型目录",
            "清空后内核使用自带模型目录（deepseek-flash 等）。\n"
            "只有用官方 DeepSeek 密钥时才需要清空；用网关（OpenCode Go 等）时清空会导致 400。\n\n继续吗？",
        ):
            return
        try:
            changed = dshcatalog.clear_kernel_catalog(self.require_paths())
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.catalog = dshcatalog.load_kernel_catalog(self.require_paths())
        self._fill_catalog_widgets()
        self._refresh_hint()
        self.journal("清空内核模型目录")
        self.notify("已清空内核模型目录" if changed else "内核模型目录本来就是空的")

    def _open_dsh_dir(self) -> None:
        if not self.is_ready():
            return
        directory = dshcatalog.dsh_home(self.require_paths())
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        if not open_path(directory):
            message(self.frame, "打开 DSH 目录", f"打不开目录：{directory}", error=True)

    # -- 一键配置 -----------------------------------------------------------
    def _auto_provider(self) -> str:
        """挑一个最可能能用的 Provider：Anthropic/官方端点 + 密钥 → 有密钥 → 第一个。"""
        if not self.is_ready():
            return ""
        paths = self.require_paths()
        candidates = list(self._providers.items())
        for name, view in candidates:
            if dshconfig.provider_is_usable_for_dsh(view) and dshconfig.provider_key_ready(
                paths, view
            ):
                return name
        for name, view in candidates:
            if dshconfig.provider_key_ready(paths, view):
                return name
        return candidates[0][0] if candidates else ""

    def _apply_recommended(self) -> None:
        """一键配置：选 Provider → 生成内核模型目录 → 写 [agent.dsh] → 询问重启。"""
        if not self.is_ready():
            self.fail("找不到 app.toml，无法保存")
            return
        paths = self.require_paths()
        pick = self._selected_provider() or self._auto_provider()
        view = self._providers.get(pick)
        if view is None:
            self.fail("没有可用的 Provider：先在「模型」页添加一个（填好 base_url 与密钥）")
            return
        protocol = "auto"
        if not dshconfig.provider_is_usable_for_dsh(view):
            suggestion = dshconfig.suggest_anthropic_base_url(view.base_url) or "（未知）"
            text = (
                f"Provider {pick} 的 base_url（{view.base_url or '未配置'}）看起来不是 "
                "Anthropic Messages 端点，内核请求会 404。\n\n"
                f"建议先把它改成：{suggestion}\n\n"
                "仍然按「强制 anthropic 协议」继续吗？（网关确实支持 Messages 协议时才选是）"
            )
            if not confirm(self.frame, "端点可能不兼容", text):
                return
            protocol = "anthropic"
        try:
            catalog = dshcatalog.catalog_from_provider(paths, pick, existing=self.catalog)
            dshcatalog.save_kernel_catalog(paths, catalog)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.catalog = dshcatalog.load_kernel_catalog(paths)
        self._fill_catalog_widgets()
        first_model = catalog.models[0].id if catalog.models else ""
        self.model_var.set(first_model)
        self.provider_var.set(pick)
        self.protocol_var.set(dshconfig.PROTOCOL_LABELS[protocol])
        self.enabled_var.set(True)
        if not self._save():
            return
        self.journal(
            f"一键配置内置 DSH：provider={pick!r} protocol={protocol} model={first_model!r}"
        )
        self.notify(f"一键配置完成：凭据用 {pick}，内核模型 {first_model}")
        message(
            self.frame,
            "一键配置完成",
            "已写入 [agent.dsh] 并生成内核模型目录（旧补丁已备份为 .gui.bak）。\n\n"
            "接下来：点「重启后台服务」让配置生效，然后发一条复杂任务试试"
            "（例如「重构 utils.py 并补测试」）。\n\n"
            "想确认真的能跑通，可以点「端到端自检」。",
        )
        if self.on_restart is not None and confirm(
            self.frame, "重启后台服务", "现在重启后台服务让配置生效吗？"
        ):
            self.on_restart()

    def _copy_suggestion(self) -> None:
        """把「同一网关的 Anthropic 地址」建议复制到剪贴板。"""
        if not self.is_ready():
            return
        pick = self._selected_provider() or self._auto_provider()
        view = self._providers.get(pick)
        if view is None:
            self.fail("先选一个 Provider")
            return
        suggestion = dshconfig.suggest_anthropic_base_url(view.base_url)
        if not suggestion:
            self.notify(f"Provider {pick} 的 base_url 已经是官方/Anthropic 端点，不需要改")
            return
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(suggestion)
        except tk.TclError:  # pragma: no cover - 剪贴板不可用
            pass
        self.notify(f"已复制建议地址：{suggestion}（到「模型」页把它填进该 Provider 的 Base URL）")

    # -- 状态 / 自检 ---------------------------------------------------------
    def on_show(self) -> None:
        """切回本页时只刷新状态，不动用户还没保存的表单输入。"""
        self._refresh_status()

    def _refresh_status(self, note: str = "") -> None:
        if not self.is_ready():
            return
        config_path = self.require_paths().config_path
        self._status_note = note
        self.notify("正在读取 DSH 状态…")
        widgets.run_async(
            self.frame, lambda: dshconfig.collect_status(config_path), self._show_status
        )

    def _show_status(self, report: Any, error: BaseException | None) -> None:
        if error is not None:
            self.fail(error)
            return
        lines = dshconfig.status_lines(report or {})
        lines.append(self._catalog_line())
        self._set_status("\n".join(lines))
        note = getattr(self, "_status_note", "")
        self._status_note = ""
        tail = "状态已刷新（只读；改配置请点「保存」）"
        self.notify(f"{note} · {tail}" if note else tail)

    def _catalog_line(self) -> str:
        """模型目录的一句话摘要（状态区最后一行）。"""
        if not self.is_ready():
            return ""
        path = dshcatalog.patch_path(self.require_paths())
        if self.catalog.present and self.catalog.models:
            return f"内核模型目录：{', '.join(self.catalog.ids())}（{path}）"
        if os.path.exists(path):
            return f"内核模型目录：已清空（{path}）→ 内核用自带目录"
        return f"内核模型目录：未生成（{path}）"

    def _score_test(self) -> None:
        """用一段文本试算路由打分（判断 min_score 设得合不合适）。"""
        if not self.is_ready():
            return
        text = (self.score_var.get() or "").strip() or _SAMPLE_TEXT
        config_path = self.require_paths().config_path
        self._append_status(f"打分试算：{text}")
        widgets.run_async(
            self.frame,
            lambda: dshconfig.collect_status(config_path, [text]),
            self._show_scores,
        )

    def _show_scores(self, report: Any, error: BaseException | None) -> None:
        if error is not None:
            self.fail(error)
            return
        for line in dshconfig.status_lines(report or {}):
            if line.startswith("打分："):
                self._append_status(line)
                return
        self._append_status("打分试算失败（状态里没有打分结果）")

    def _run_smoke(self) -> None:
        """端到端自检：真的拉起内核跑一轮 prompt。"""
        if not self.is_ready() or self._smoke_running:
            return
        config_path = self.require_paths().config_path
        if not confirm(
            self.frame,
            "端到端自检",
            "会拉起内置 DSH 内核真跑一轮 prompt（首次约 10~60 秒，会花一点 token）。\n"
            "期间可以继续看别的页，但别关窗口。继续吗？",
        ):
            return
        self._smoke_running = True
        self._smoke_button.configure(state=tk.DISABLED)
        self._append_status("端到端自检：正在拉起内核…")
        widgets.run_async(
            self.frame,
            lambda: dshsmoke.run_smoke(config_path, timeout=180.0),
            self._show_smoke,
        )

    def _show_smoke(self, result: Any, error: BaseException | None) -> None:
        self._smoke_running = False
        try:
            self._smoke_button.configure(state=tk.NORMAL)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass
        if error is not None:
            self.fail(error)
            return
        data = result if isinstance(result, dict) else {"ok": False, "error": "自检没有返回结果"}
        lines = ["端到端自检："]
        if data.get("ok"):
            lines.append("✓ 成功（stopReason=end_turn）：内置 DSH 能经当前配置跑通一轮对话")
        else:
            lines.append(
                "✗ 失败（阶段：{}）：{}".format(
                    data.get("stage") or "?",
                    data.get("error") or f"stopReason={data.get('stopReason')}",
                )
            )
        for key, label in (
            ("model", "内核模型"),
            ("answer", "回答"),
            ("elapsedSeconds", "耗时（秒）"),
            ("command", "启动命令"),
        ):
            value = data.get(key)
            if value not in (None, ""):
                lines.append(f"{label}：{value}")
        for warning in data.get("warnings") or []:
            lines.append(f"警告：{warning}")
        if data.get("gateway"):
            lines.append(f"网关头代理：{data.get('gatewayUpstream', '')}")
        tail = str(data.get("stderrTail") or "")
        if tail:
            lines.append("内核 stderr 尾部：\n" + tail)
        self._append_status("\n".join(lines))
        self.journal(
            "内置 DSH 端到端自检：{} stage={} model={} warnings={} error={}".format(
                "成功" if data.get("ok") else "失败",
                data.get("stage") or "",
                data.get("model") or "",
                len(data.get("warnings") or []),
                str(data.get("error") or data.get("stopReason") or "")[:300],
            )
        )
        if data.get("ok"):
            self.notify("端到端自检通过：内置 DSH 可用")
        else:
            self.notify("端到端自检失败：看状态区的原因（失败原因通常就是弹窗里那句）")
