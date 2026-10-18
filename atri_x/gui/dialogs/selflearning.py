"""「自学习设置」子窗口：编辑 ``app.toml`` 的 ``[self_learning]``。

自学习是「表达 / 黑话学习层」：只有 review 通过的内容才会注入提示词。主「进阶」页
只放一个总开关，这里放开保留期、候选门槛与各处的字符/条数上限——都是**数值限制**
和**是否参与**的问题，不涉及提示词正文。

0 / 留空一律表示「用内置默认」（读取与清键都按这个口径），避免把某个默认值在界面
上写死成配置文件里的一行。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from atri_x.gui.configedit import ConfigEditError, patch_section, read_toml
from atri_x.gui.dialogs.common import Dialog

__all__ = ["SelfLearningDialog"]

#: 数值项：(配置键, 界面名, 说明)。
_INT_FIELDS = (
    ("retention_days", "保留天数", "学习数据的保留期；0 表示不按时间清理"),
    ("min_count", "候选最低出现次数", "至少被提到多少次才进入 review"),
    ("min_users", "候选最低用户数", "至少多少个不同用户说过才进入 review（1 = 允许单人复读）"),
    ("max_meaning_runes", "含义最大字符数", "单条候选含义的最大长度"),
    ("max_context_runes", "注入上下文上限", "每轮最多注入多少字符的学习上下文"),
    ("max_observation_runes", "单条观察上限", "单条观察的最大字符数"),
    ("max_observations_per_scope", "单会话观察条数上限", "单个会话最多保留多少条观察"),
    ("max_observation_writes_per_minute", "每分钟写入上限", "每个会话每分钟最多写入多少条观察"),
    ("max_mine_chars", "单次挖掘字符上限", "单次挖掘最多扫描多少字符"),
    ("mine_timeout_seconds", "挖掘超时（秒）", "单次挖掘的超时时间"),
)


class SelfLearningDialog(Dialog):
    """自学习参数编辑窗口。"""

    title = "自学习设置（app.toml [self_learning]）"
    geometry = "680x560"

    def build(self) -> None:
        top = ttk.Frame(self.body)
        top.pack(fill=tk.X)
        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            top, text="启用自学习（表达 / 黑话学习层）", variable=self.enabled_var
        ).pack(anchor="w")
        ttk.Label(
            self.body,
            text=(
                "只有 review 通过的候选才会进入提示词；这里是门槛与配额。"
                "数值留空或 0 表示使用内置默认值。保存后需要重启后台服务生效。"
            ),
            foreground="#555555",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 8))

        form = ttk.Frame(self.body)
        form.pack(fill=tk.BOTH, expand=True)
        form.columnconfigure(1, weight=1)
        self.value_vars: dict[str, tk.StringVar] = {}
        for row, (key, label, hint) in enumerate(_INT_FIELDS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="nw", pady=3)
            variable = tk.StringVar()
            self.value_vars[key] = variable
            ttk.Entry(form, textvariable=variable, width=12).grid(
                row=row, column=1, sticky="w", pady=3
            )
            ttk.Label(form, text=hint, foreground="#8a8a8a", wraplength=380).grid(
                row=row, column=2, sticky="w", padx=(8, 0)
            )

        self.button("保存", self._save)
        self.button("重新载入", self.reload)

    # -- 数据 ---------------------------------------------------------------
    def reload(self) -> None:
        if not self.is_ready():
            return
        try:
            data = read_toml(self.require_paths().config_path)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        section = data.get("self_learning")
        section = section if isinstance(section, dict) else {}
        enabled = section.get("enabled")
        self.enabled_var.set(True if enabled is None else bool(enabled))
        for key, _label, _hint in _INT_FIELDS:
            value = section.get(key)
            self.value_vars[key].set("" if value in (None, "") else str(value))
        self.notify("已载入 self_learning（改动需要重启后台服务）")

    def _save(self) -> None:
        try:
            values: dict[str, Any] = {"enabled": bool(self.enabled_var.get())}
            for key, _label, _hint in _INT_FIELDS:
                raw = self.value_vars[key].get().strip()
                if not raw:
                    values[key] = None
                    continue
                try:
                    number = int(raw)
                except ValueError as exc:
                    raise ConfigEditError(f"{key} 必须是整数：{raw!r}") from exc
                if number < 0:
                    raise ConfigEditError(f"{key} 不能是负数：{number}")
                values[key] = number
            patch_section(self.require_paths().config_path, "self_learning", values)
        except ConfigEditError as exc:
            self.fail(exc)
            return
        self.saved("已保存 [self_learning]；重启后台服务后生效")
