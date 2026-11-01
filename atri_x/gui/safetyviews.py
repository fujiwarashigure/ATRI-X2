"""「内容安全」页的三个只读/半只读子页：复核队列、审计日志、原始 TOML。

* 复核队列（``safety/review.db``）：远程审核给出 ``review`` 档、或本地词表配置成
  ``review`` 时写入的待人工处置条目。这里只做"通过 / 驳回 / 标记过期"，不改策略；
* 审计（``safety-audit.jsonl``）：每次非 ``pass`` 判定一条，**只含脱敏预览与
  sha256 指纹，不含原文**；本页只读，用于调阈值与申诉；
* 原始 TOML：给"表单里没覆盖的键"留的出口（例如阶段级策略、未来新增字段）。
  保存前用后台同一个解码器校验，写坏配置会被拒绝。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui import safetyconfig, safetyrecords
from atri_x.gui import process as gui_process
from atri_x.gui.tabs.base import confirm

__all__ = ["AuditPanel", "RawTomlPanel", "ReviewPanel"]

#: 复核状态筛选（显示名 → 值，空串 = 全部）。
_REVIEW_FILTERS = (
    ("待复核", "pending"),
    ("已通过", "approved"),
    ("已驳回", "rejected"),
    ("已过期", "expired"),
    ("全部", ""),
)

#: 审计动作筛选（显示名 → 值，空串 = 全部）。
_AUDIT_FILTERS = (
    ("全部动作", ""),
    ("拦截 block", "block"),
    ("脱敏 redact", "redact"),
    ("标记 flag", "flag"),
    ("复核 review", "review"),
)


class ReviewPanel:
    """复核队列：列表 + 详情 + 通过/驳回。"""

    def __init__(self, parent: Any, paths: Any, tab: Any) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._items: dict[str, safetyconfig.ReviewItem] = {}
        self._current = ""
        self._status = tk.StringVar(value="")
        self._stats = tk.StringVar(value="")
        self._note = tk.StringVar(value="")
        self._build()
        self.reload()

    def _build(self) -> None:
        top = ttk.Frame(self.frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="状态").pack(side=tk.LEFT)
        self._filter = ttk.Combobox(
            top,
            values=[label for label, _ in _REVIEW_FILTERS],
            state="readonly",
            width=10,
        )
        self._filter.set(_REVIEW_FILTERS[0][0])
        self._filter.pack(side=tk.LEFT, padx=(6, 6))
        self._filter.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        ttk.Button(top, text="刷新", command=self.reload).pack(side=tk.LEFT)
        ttk.Button(top, text="标记超时过期", command=self._expire).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(top, text="打开队列文件", command=self._open).pack(side=tk.RIGHT)
        ttk.Label(top, textvariable=self._stats, foreground="#555555").pack(
            side=tk.RIGHT, padx=(0, 12)
        )

        tree_box = ttk.Frame(self.frame)
        tree_box.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        tree_box.rowconfigure(0, weight=1)
        tree_box.columnconfigure(0, weight=1)
        columns = ("time", "stage", "tier", "status", "labels", "provider", "scope")
        self.tree = ttk.Treeview(tree_box, columns=columns, show="headings", height=9)
        for name, text, width in (
            ("time", "时间", 150),
            ("stage", "阶段", 80),
            ("tier", "档位", 60),
            ("status", "状态", 70),
            ("labels", "标签", 190),
            ("provider", "供应商", 80),
            ("scope", "群/用户", 150),
        ):
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tree_box, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        detail = ttk.LabelFrame(self.frame, text="详情", padding=6)
        detail.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        detail.rowconfigure(0, weight=1)
        detail.columnconfigure(0, weight=1)
        self._detail = tk.Text(detail, wrap=tk.WORD, height=7, state=tk.DISABLED)
        self._detail.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Frame(detail)
        bar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(bar, text="备注").pack(side=tk.LEFT)
        ttk.Entry(bar, textvariable=self._note, width=40).pack(side=tk.LEFT, padx=(6, 6))
        ttk.Button(bar, text="通过", command=lambda: self._resolve(True)).pack(side=tk.LEFT)
        ttk.Button(bar, text="驳回", command=lambda: self._resolve(False)).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(detail, textvariable=self._status, foreground="#8a8a8a").grid(
            row=2, column=0, sticky="w"
        )

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if self.paths is None:
            return
        settings, errors = safetyconfig.load_settings(self.paths)
        if errors:
            self._status.set("safety.toml 有错误：" + "；".join(errors))
        status = dict(_REVIEW_FILTERS).get(self._filter.get(), "pending")
        items = safetyrecords.list_review_items(self.paths, settings, status=status)
        stats = safetyrecords.review_stats(self.paths, settings)
        self._stats.set(
            f"待复核 {stats.get('pending', 0)} / 累计 {stats.get('total', 0)}"
        )
        self._items = {item.id: item for item in items}
        self.tree.delete(*self.tree.get_children())
        for item in items:
            self.tree.insert(
                "",
                tk.END,
                iid=item.id,
                values=(
                    item.created_at,
                    item.stage,
                    item.tier,
                    item.status,
                    "、".join(item.labels),
                    item.provider,
                    f"{item.platform}:{item.scope_id}" if item.platform else item.scope_id,
                ),
            )
        if self._current in self._items:
            self.tree.selection_set(self._current)
            self._show(self._items[self._current])
        else:
            self._current = ""
            self._set_detail("")
        if not items:
            self._status.set(
                "队列为空（没有命中 review 档，或复核库还没创建；远程审核的 review 档才入队）"
            )

    def _on_select(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        item = self._items.get(selection[0])
        if item is None:
            return
        self._current = item.id
        self._show(item)

    def _show(self, item: safetyconfig.ReviewItem) -> None:
        score = "" if item.score is None else f"{item.score:.2f}"
        lines = [
            f"id：{item.id}",
            f"时间：{item.created_at}    阶段：{item.stage}    档位：{item.tier}    状态：{item.status}",
            f"标签：{'、'.join(item.labels) or '（无）'}    分数：{score or '（无）'}",
            f"供应商：{item.provider or '（无）'}    请求 id：{item.request_id or '（无）'}",
            f"群/用户：{item.platform}:{item.scope_id} / {item.actor or '（未知）'}",
            f"处置：{item.action_taken or '（无）'}    原因：{item.reason or '（无）'}",
            f"预览（脱敏）：{item.preview or '（无）'}",
        ]
        if item.reviewer:
            lines.append(f"复核：{item.reviewer} @ {item.reviewed_at} · {item.note}")
        self._set_detail("\n".join(lines))
        self._status.set("选择一条后填备注，再点通过/驳回。")

    def _set_detail(self, text: str) -> None:
        self._detail.configure(state=tk.NORMAL)
        self._detail.delete("1.0", tk.END)
        self._detail.insert("1.0", text)
        self._detail.configure(state=tk.DISABLED)

    # -- 按钮 ---------------------------------------------------------------
    def _resolve(self, approved: bool) -> None:
        if not self._current:
            self.tab.notify("先选择一条待复核记录")
            return
        action = "通过" if approved else "驳回"
        if not confirm(self.frame, action, f"确定{action}这条记录吗？"):
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        ok = safetyrecords.resolve_review_item(
            self.paths,
            settings,
            self._current,
            approved=approved,
            reviewer="gui",
            note=self._note.get().strip(),
        )
        if not ok:
            self.tab.fail("更新失败：条目不存在、已经被处理或复核库不可用")
            return
        self.tab.journal(f"内容安全：复核{action} {self._current}")
        self._note.set("")
        self.reload()

    def _expire(self) -> None:
        if not confirm(self.frame, "标记过期", "把 1 小时前未处理的待复核条目标记为已过期？"):
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        count = safetyrecords.expire_review_items(
            self.paths, settings, older_than_seconds=3600
        )
        self.tab.journal(f"内容安全：标记过期 {count} 条复核记录")
        self._status.set(f"已标记过期 {count} 条")
        self.reload()

    def _open(self) -> None:
        if self.paths is None:
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        path = safetyconfig.review_db_path(self.paths, settings)
        if not gui_process.open_path(path):
            self.tab.notify(f"队列文件还不存在：{path}")


class AuditPanel:
    """审计日志尾部（只读，按动作/阶段筛选）。"""

    def __init__(self, parent: Any, paths: Any, tab: Any) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._records: list[dict[str, Any]] = []
        self._status = tk.StringVar(value="")
        self._stage = tk.StringVar(value="")
        self._build()
        self.reload()

    def _build(self) -> None:
        top = ttk.Frame(self.frame)
        top.pack(fill=tk.X)
        ttk.Label(top, text="动作").pack(side=tk.LEFT)
        self._action = ttk.Combobox(
            top, values=[label for label, _ in _AUDIT_FILTERS], state="readonly", width=12
        )
        self._action.set(_AUDIT_FILTERS[0][0])
        self._action.pack(side=tk.LEFT, padx=(6, 6))
        self._action.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        ttk.Label(top, text="阶段包含").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self._stage, width=12).pack(side=tk.LEFT, padx=(6, 6))
        ttk.Button(top, text="刷新", command=self.reload).pack(side=tk.LEFT)
        ttk.Button(top, text="打开审计文件", command=self._open).pack(side=tk.RIGHT)
        ttk.Label(top, textvariable=self._status, foreground="#555555").pack(
            side=tk.RIGHT, padx=(0, 12)
        )

        box = ttk.Frame(self.frame)
        box.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        columns = ("ts", "stage", "tier", "action", "labels", "provider", "scope", "preview")
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=16)
        for name, text, width in (
            ("ts", "时间", 150),
            ("stage", "阶段", 70),
            ("tier", "档位", 55),
            ("action", "动作", 60),
            ("labels", "标签", 150),
            ("provider", "供应商", 70),
            ("scope", "群/用户", 130),
            ("preview", "预览（已脱敏）", 260),
        ):
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        ttk.Label(
            self.frame,
            text="审计只存 sha256 指纹与脱敏预览，不含原文；pass 档不记录。删除文件不影响判定，只丢历史。",
            foreground="#8a8a8a",
            wraplength=1000,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 0))

    def reload(self) -> None:
        if self.paths is None:
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        records = safetyrecords.read_audit(self.paths, settings)
        action = dict(_AUDIT_FILTERS).get(self._action.get(), "")
        stage = self._stage.get().strip().lower()
        visible = [
            record
            for record in records
            if (not action or str(record.get("action") or "") == action)
            and (not stage or stage in str(record.get("stage") or "").lower())
        ]
        self._records = list(reversed(visible))
        self.tree.delete(*self.tree.get_children())
        for index, record in enumerate(self._records):
            labels = record.get("labels")
            if isinstance(labels, (list, tuple)):
                labels = "、".join(str(item) for item in labels)
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    str(record.get("ts") or ""),
                    str(record.get("stage") or ""),
                    str(record.get("tier") or ""),
                    str(record.get("action") or ""),
                    str(labels or ""),
                    str(record.get("provider") or ""),
                    f"{record.get('platform') or ''}:{record.get('scope') or ''}",
                    str(record.get("preview") or ""),
                ),
            )
        self._status.set(
            f"{len(self._records)} 条（最多展示最近 {safetyrecords.AUDIT_LIMIT} 条）"
        )
        if not records:
            self._status.set(
                "还没有审计记录（文件不存在，或所有判定都是 pass；远程审核开启后这里才会有 review/block）"
            )

    def _open(self) -> None:
        if self.paths is None:
            return
        settings, _errors = safetyconfig.load_settings(self.paths)
        path = safetyconfig.audit_path(self.paths, settings)
        if not gui_process.open_path(path):
            self.tab.notify(f"审计文件还不存在：{path}")


class RawTomlPanel:
    """整份 ``safety.toml`` 的文本编辑（保存前校验）。"""

    def __init__(self, parent: Any, paths: Any, tab: Any) -> None:
        self.paths = paths
        self.tab = tab
        self.frame = ttk.Frame(parent, padding=8)
        self._status = tk.StringVar(value="")
        self._build()
        self.reload()

    def _build(self) -> None:
        ttk.Label(
            self.frame,
            text=(
                "给「策略」页没覆盖的键留的出口（阶段级策略、未来新增字段）。保存前会用后台解码器校验；"
                "写坏配置会被拒绝，不会让后台服务起不来。"
            ),
            foreground="#8a8a8a",
            wraplength=1000,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))
        box = ttk.Frame(self.frame)
        box.pack(fill=tk.BOTH, expand=True)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.text = tk.Text(box, wrap=tk.NONE, undo=True)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        bar = ttk.Frame(self.frame)
        bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(bar, text="校验并保存", command=self._save).pack(side=tk.LEFT)
        ttk.Button(bar, text="重新载入", command=self.reload).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="插入默认模板", command=self._template).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(bar, textvariable=self._status, foreground="#8a8a8a").pack(
            side=tk.LEFT, padx=(12, 0)
        )

    def reload(self) -> None:
        if self.paths is None:
            return
        try:
            text = safetyconfig.safety_text(self.paths)
        except Exception as exc:  # noqa: BLE001
            self.tab.fail(exc)
            return
        if not text:
            text = "# safety.toml 尚未创建。点「插入默认模板」再保存即可启用（默认 audit 只记不拦）。\n"
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", text)
        self._status.set(
            "safety.toml 不存在" if not safetyconfig.has_safety_file(self.paths) else "已载入当前文件"
        )

    def _template(self) -> None:
        from atri_x.gui.safetyconfig import DEFAULT_TEMPLATE

        if not confirm(self.frame, "插入模板", "用默认模板覆盖编辑框内容？（尚未写盘）"):
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", DEFAULT_TEMPLATE)

    def _save(self) -> None:
        try:
            safetyconfig.save_safety_text(
                self.tab.require_paths(), self.text.get("1.0", "end-1c")
            )
        except Exception as exc:  # noqa: BLE001
            self.tab.fail(exc)
            return
        self.tab.journal("内容安全：保存 safety.toml（原始 TOML）")
        self.tab.notify("已保存；后台 ≤1 秒热加载")
        self.tab.reload()
