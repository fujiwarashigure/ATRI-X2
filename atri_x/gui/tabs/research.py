"""「研究」选项卡：查看科研助理的运行与产物（只读）。

设计取舍：

* **不依赖引擎包**：直接读 ``<配置目录>/research/runs/*/run.json`` 与目录大小，
  引擎没装好、venv 没建好时这一页仍然能显示"环境未就绪"，方便排障；
* **不改数据**：没有删除/重跑按钮，避免在 GUI 里绕过工具的风险与审计口径；
* 展示三层信息：运行列表（左）→ 选中运行的摘要（右上）→ 产物与实际文件（右下），
  双击运行行等价于"打开该运行的目录"。
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

__all__ = ["ResearchTab"]

_STAGES = {
    "init": "已立项",
    "search": "已检索",
    "screen": "已筛选",
    "verify": "已核验",
    "cite": "已生成引用",
    "outline": "已出大纲",
    "writing": "写作中",
    "check": "已质检",
    "review": "已评审",
    "report": "已出报告",
    "export": "已导出",
}

_ARTIFACTS = (
    ("run.json", "运行记录"),
    ("search.json", "检索记录"),
    ("library.json", "文献库"),
    ("criteria.json", "纳排标准"),
    ("screening.json", "筛选结果"),
    ("verification.json", "引文核验"),
    ("references.json", "参考文献"),
    ("outline.json", "写作大纲"),
    ("draft.md", "论文正文"),
    ("qa.json", "质检结果"),
    ("audit.json", "确定性评审"),
    ("panel.json", "评审聚合"),
    ("report.md", "研究报告"),
    ("exports", "导出目录"),
    ("fulltext", "全文目录"),
)


def research_home() -> str:
    """与 ``/research`` 命令、引擎共用同一套路径解析规则（唯一出处见 ``atri_x.research.paths``）。"""
    from atri_x.research.paths import research_home as resolve_home

    return resolve_home()


def _load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


class ResearchTab:
    """科研运行浏览器。"""

    def __init__(self, parent: Any, paths: Any = None) -> None:
        del paths  # 路径由 research_home() 统一解析，不依赖 ConfigPaths 的字段
        self.frame = ttk.Frame(parent)
        self.home = research_home()
        self._runs: list[dict[str, Any]] = []
        self._build()
        self.refresh()

    # -- 构建 --------------------------------------------------------------

    def _build(self) -> None:
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(toolbar, text="科研助理运行与产物（只读）").pack(side=tk.LEFT)
        self.home_var = tk.StringVar(value=self.home)
        ttk.Label(toolbar, textvariable=self.home_var, foreground="#666666").pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="打开目录", command=self._open_home).pack(
            side=tk.RIGHT, padx=4
        )

        panes = ttk.Panedwindow(self.frame, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(panes)
        self.tree = ttk.Treeview(
            left,
            columns=("stage", "title", "updated"),
            show="tree headings",
            height=12,
        )
        self.tree.heading("#0", text="运行 id")
        self.tree.column("#0", width=250, stretch=False)
        self.tree.heading("stage", text="阶段")
        self.tree.column("stage", width=90, stretch=False)
        self.tree.heading("title", text="题目")
        self.tree.column("title", width=220)
        self.tree.heading("updated", text="更新")
        self.tree.column("updated", width=130, stretch=False)
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected())
        self.tree.bind("<Double-1>", lambda _event: self._open_selected_dir())
        panes.add(left, weight=3)

        right = ttk.Frame(panes)
        buttons = ttk.Frame(right)
        buttons.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(buttons, text="打开该运行目录", command=self._open_selected_dir).pack(
            side=tk.LEFT
        )
        ttk.Button(buttons, text="打开报告", command=self._open_report).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(buttons, text="打开导出目录", command=self._open_exports).pack(
            side=tk.LEFT
        )
        self.text = tk.Text(right, wrap=tk.WORD, height=16)
        text_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set, state=tk.DISABLED)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        panes.add(right, weight=4)

    # -- 数据 --------------------------------------------------------------

    def refresh(self) -> None:
        self.home = research_home()
        self.home_var.set(self.home)
        runs = []
        runs_dir = os.path.join(self.home, "runs")
        if os.path.isdir(runs_dir):
            for name in os.listdir(runs_dir):
                state = _load(os.path.join(runs_dir, name, "run.json"))
                if not isinstance(state, dict):
                    continue
                runs.append(
                    {
                        "id": state.get("id") or name,
                        "title": str(state.get("title") or ""),
                        "stage": str(state.get("stage") or ""),
                        "updated_at": str(state.get("updated_at") or ""),
                        "dir": os.path.join(runs_dir, name),
                        "state": state,
                    }
                )
        runs.sort(key=lambda item: item["updated_at"], reverse=True)
        self._runs = runs
        for item in self.tree.get_children():
            self.tree.delete(item)
        for run in runs:
            self.tree.insert(
                "",
                tk.END,
                iid=run["id"],
                text=run["id"],
                values=(
                    _STAGES.get(run["stage"], run["stage"] or "未知"),
                    run["title"][:60],
                    run["updated_at"].replace("T", " ")[:16],
                ),
            )
        if runs:
            self.tree.selection_set(runs[0]["id"])
            self._show_selected()
        else:
            self._set_text(self._empty_message())

    def _empty_message(self) -> str:
        lines = [f"研究数据目录：{self.home}", ""]
        if not os.path.isdir(self.home):
            lines += [
                "该目录还不存在，说明科研助理还没被使用过。",
                "",
                "让助理在对话里开始一次研究（或先运行安装器），",
                "完成后这里会显示每次运行的阶段、产物与导出文件。",
            ]
        else:
            lines += [
                "还没有运行记录。",
                "",
                "在对话里说「帮我查一下 X 的研究现状」即可创建第一次运行。",
            ]
        return "\n".join(lines)

    # -- 展示 --------------------------------------------------------------

    def _selected(self) -> dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            return None
        run_id = selection[0]
        for run in self._runs:
            if run["id"] == run_id:
                return run
        return None

    def _show_selected(self) -> None:
        run = self._selected()
        if run is None:
            return
        state = run["state"]
        lines = [
            f"运行：{run['id']}",
            f"题目：{state.get('title', '')}",
            f"研究问题：{state.get('question', '')}",
            f"阶段：{_STAGES.get(run['stage'], run['stage'] or '未知')}"
            f" · 语言：{state.get('language', '')} · 引用样式：{state.get('style', '')}"
            f" · 目标字数：{state.get('target_words', '')}",
        ]
        counts = state.get("counts") or {}
        if counts:
            pairs = []
            for key, label in (
                ("identified", "检索命中"),
                ("deduped", "去重后"),
                ("included", "纳入"),
                ("verified_ratio", "核验通过率"),
                ("qa_score", "质检得分"),
                ("draft_words", "正文字数"),
            ):
                if key in counts:
                    pairs.append(f"{label} {counts[key]}")
            if pairs:
                lines.append("计数：" + "，".join(pairs))
        lines += ["", f"目录：{run['dir']}", "", "产物："]
        found_any = False
        for name, label in _ARTIFACTS:
            path = os.path.join(run["dir"], name)
            if os.path.isdir(path):
                files = [
                    item
                    for item in sorted(os.listdir(path))
                    if os.path.isfile(os.path.join(path, item))
                ]
                detail = "、".join(files[:8]) + ("…" if len(files) > 8 else "")
                lines.append(f"  · {label}：{detail or '（空）'}")
                found_any = True
            elif os.path.isfile(path):
                lines.append(f"  · {label}：{_human_size(os.path.getsize(path))}")
                found_any = True
        if not found_any:
            lines.append("  （暂无）")
        lines += [
            "",
            "提示：写作/核验等步骤在对话里由 research_* 工具完成；",
            "这一页只负责查看，避免在界面里绕过工具的审计口径。",
        ]
        self._set_text("\n".join(lines))

    def _set_text(self, content: str) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", content)
        self.text.configure(state=tk.DISABLED)

    # -- 动作 --------------------------------------------------------------

    def _open(self, path: str) -> None:
        if not os.path.exists(path):
            messagebox.showinfo("科研助理", f"路径不存在：\n{path}", parent=self.frame)
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", path])
        except Exception as exc:  # noqa: BLE001 - 打不开目录不该打断界面
            messagebox.showerror(
                "科研助理", f"打开失败：{type(exc).__name__}: {exc}", parent=self.frame
            )

    def _open_home(self) -> None:
        self._open(self.home)

    def _open_selected_dir(self) -> None:
        run = self._selected()
        if run is not None:
            self._open(run["dir"])

    def _open_report(self) -> None:
        run = self._selected()
        if run is None:
            return
        self._open(os.path.join(run["dir"], "report.md"))

    def _open_exports(self) -> None:
        run = self._selected()
        if run is None:
            return
        self._open(os.path.join(run["dir"], "exports"))

    # -- 兼容主窗口的懒加载钩子 -------------------------------------------

    def on_hidden(self) -> None:
        """主窗口切走该页时可能调用；本页没有需要暂停的定时器。"""

    def reload(self) -> None:
        """主窗口"重新载入"语义下的等价动作。"""
        self.refresh()
