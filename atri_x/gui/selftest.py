"""桌面端自检：不连服务、不建托盘，只验证 Tk 侧能构建与渲染。

用途有两个：

- ``atri-x-gui.exe --selftest``（打包后的产物级核验，见 ``packaging/build.py``）；
- ``python -m atri_x.gui.selftest``（源码树里快速确认 Tk/字体/中文渲染没坏）。

只读 GUI 代码，不写配置、不联网、不留窗口。
"""

from __future__ import annotations

import sys
import tkinter as tk

from atri_x.gui.app import WINDOW_TITLE, GuiApp

__all__ = ["run_selftest", "safe_print"]


def safe_print(text: str) -> None:
    """窗口版 exe 没有控制台（``sys.stdout`` 为 ``None``）时静默跳过。"""
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        stream.write(text + "\n")
    except (UnicodeEncodeError, OSError, ValueError):
        pass
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


def run_selftest() -> int:
    """构建界面 → 喂几帧 → 销毁；返回退出码（0 = 通过）。"""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        safe_print(f"GUI_SELFTEST_FAIL no-display: {exc}")
        return 1
    root.withdraw()
    try:
        app = GuiApp(root, settings=None, client=None, tray_enabled=False)
        app.transcript.add_user("自检：你好")
        app.transcript.stream_append("自检：这是流式")
        app.transcript.stream_append("回复")
        app.transcript.stream_finish()
        app.transcript.add_notice("自检通知", "INFO")
        app.transcript.add_error("自检错误")
        app.render_now()
        root.update_idletasks()
        root.update()
        rendered = app.transcript_view.get("1.0", tk.END)
        assert "自检" in rendered, "transcript 没有渲染出来"
        assert "ATRI" in rendered, "角色前缀缺失"
        # 逐个切页：让每页的 on_show（日志/功能页会重读文件）都真跑一次。
        for index in range(len(app.notebook.tabs())):
            app.notebook.select(index)
            root.update_idletasks()
            root.update()
        # 「工具权限」页的自检：chat / work 两个子选项卡都要渲染出工具菜单，且每行
        # 必须有激活按钮、状态和功能解释。
        # （打包产物的 ``--selftest`` 也走这里，所以冻结版少画一样东西就会被卡住。）
        toolperm = app.toolperm_tab
        assert toolperm.editors, "工具权限页没有创建 chat / work 子选项卡"
        for mode_key, editor in toolperm.editors.items():
            assert editor._rows, f"工具权限页 {mode_key} 子选项卡没有渲染出工具菜单"
            for name, row in editor._rows.items():
                assert row.badge.cget("text") in (
                    "已激活",
                    "未激活",
                    "继承全局",
                    "禁止使用",
                ), f"工具 {mode_key}/{name} 的激活状态徽标不对"
                assert str(row.button.cget("text")).strip(), f"工具 {name} 缺少激活按钮文案"
                assert str(row.summary.cget("text")).strip(), f"工具 {name} 缺少功能解释"
                assert int(row.summary.cget("wraplength")) > 0, (
                    f"工具 {name} 的功能解释没有设置换行宽度"
                )
        # 第三个子选项卡「工具参数（全局）」：每组参数都要建出面板与字段控件，
        # 并且能展开 / 收起（自检不写任何配置）。
        params = toolperm.params_editor
        assert params is not None, "工具权限页没有创建工具参数子选项卡"
        assert params.panels, "工具参数子选项卡没有渲染出参数面板"
        for key, panel in params.panels.items():
            assert panel.group.fields, f"工具参数面板 {key} 没有字段"
            rendered = len(panel._vars) + len(panel._texts)
            assert rendered == len(panel.group.fields), (
                f"工具参数面板 {key} 的控件数量不对：{rendered}"
                f" != {len(panel.group.fields)}"
            )
            panel.expand()
            root.update_idletasks()
            assert panel.expanded and panel.body.winfo_manager(), (
                f"工具参数面板 {key} 展开失败"
            )
            panel.collapse()
        assert params.footer_label.cget("text").strip(), (
            "工具参数子选项卡缺少『其余工具没有可调参数』说明"
        )
        app.notebook.select(0)
        root.update_idletasks()
        root.update()
        root.destroy()
    except Exception as exc:  # noqa: BLE001 - 自检失败要报出原因
        safe_print(f"GUI_SELFTEST_FAIL {type(exc).__name__}: {exc}")
        return 1
    safe_print(f"GUI_SELFTEST_OK tk={tk.TkVersion} title={WINDOW_TITLE}")
    return 0


if __name__ == "__main__":  # pragma: no cover - 人工自检入口
    raise SystemExit(run_selftest())
