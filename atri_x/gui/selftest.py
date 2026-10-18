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
