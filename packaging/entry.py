"""PyInstaller 冻结入口：与 ``python -m atri_x`` 等价（另支持 ``-m <模块>`` / GUI 版）。

为什么单独一层：``python -m atri_x`` 依赖解释器的 ``-m`` 机制，冻结后没有那套机制；
PyInstaller 需要一个**可执行脚本**当分析起点。这里只做转发，保证“源码跑”和“exe 跑”
走同一个 ``main``（``atri_x/__main__.py``），不会出现两套入口各自漂移。

三种模式（同一个入口脚本，onedir 里生成两个 exe）：

1. ``atri-x.exe <args>`` → :func:`atri_x.__main__.main`（控制台版：run / service /
   config check / doctor / completion …）；
2. ``atri-x-gui.exe <args>`` → :func:`atri_x.gui.__main__.main`（窗口版：桌面端；
   靠 exe 文件名里的 ``gui`` 判定，见 :func:`is_gui_exe`）；
3. ``atri-x.exe -m <模块> [args]`` → 用 :mod:`runpy` 复刻 ``python -m``：冻结包没有
   解释器，但 ``atri_x`` 有一批按模块运行的诊断入口
   （``atri_x.platform.qq_onebot.smoke``、``atri_x.agent.dsh.smoke`` 等）。

``multiprocessing.freeze_support()`` 目前是空的保险：``atri_x`` 的运行时代码没有用
``multiprocessing``（图片隔离压缩走 ``asyncio.to_thread``，见
``atri_x/media/worker.py`` 的取舍说明），但将来若有人加回来，冻结包必须在
``if __name__ == "__main__"`` 里第一件事就调用它，否则子进程会反复重跑主程序。
"""

from __future__ import annotations

import io
import multiprocessing
import runpy
import sys

#: 允许 ``exe -m <模块>`` 与 ``exe run-module <模块>`` 两种写法。
MODULE_FLAGS = ("-m", "run-module")

#: windowed 版 exe 的文件名标记（PyInstaller 生成的 ``atri-x-gui.exe``）。
GUI_EXE_MARKER = "gui"

#: windowed 版没有控制台，``sys.stdout/stderr`` 会被置成 ``None``。
_NULL_STREAM = None


def is_gui_exe() -> bool:
    """当前是不是窗口版 exe（``atri-x-gui.exe``）。"""
    executable = getattr(sys, "executable", "") or ""
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return GUI_EXE_MARKER in name


def main(argv: list[str] | None = None) -> int:
    """按 exe 类型/``-m`` 参数转发到对应入口。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 1 and args[0] in MODULE_FLAGS:
        if len(args) < 2:
            _stderr().write("usage: atri-x -m <module> [args...]\n")
            return 2
        return run_module(args[1], args[2:])
    if is_gui_exe():
        return run_module("atri_x.gui", args)
    from atri_x.__main__ import main as app_main  # 延迟导入：--version 不拉装配层

    return int(app_main(args))


def run_module(name: str, argv: list[str]) -> int:
    """以 ``__main__`` 身份运行一个已打包模块（等价 ``python -m name``）。"""
    saved = sys.argv
    # runpy 会把 sys.argv[0] 改写成模块路径；其余参数原样透传。
    sys.argv = [name, *argv]
    try:
        runpy.run_module(name, run_name="__main__", alter_sys=True)
    except SystemExit as exc:  # 模块自己用 SystemExit 表达退出码
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        _stderr().write("%s\n" % (code,))
        return 1
    except BaseException as exc:  # noqa: BLE001 - 与 python -m 一致：异常即非零退出
        import traceback

        traceback.print_exc(file=_stderr())
        _stderr().write("atri-x: run module %s: %s\n" % (name, exc or type(exc).__name__))
        return 1
    finally:
        sys.argv = saved
    return 0


def _stderr() -> io.TextIOBase:
    """windowed exe 的 ``sys.stderr`` 是 ``None``，退到内存缓冲以免写崩。"""
    global _NULL_STREAM
    stream = getattr(sys, "stderr", None)
    if stream is not None:
        return stream
    if _NULL_STREAM is None:
        _NULL_STREAM = io.StringIO()
    return _NULL_STREAM


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
