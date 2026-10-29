"""研究引擎的子进程入口（技能 ``ATRIX_SKILL.toml`` 的 ``command`` 指向这里）。

两种运行形态共用本模块：

* **冻结产物**（安装版 / 便携版）：``atri-x.exe -m atri_x.research.runner --home <home> <tool> …``
  ——不能再依赖系统 Python，所以由 ``atri-x.exe`` 自己的 ``-m`` 转发（见
  ``packaging/entry.py``）跑本模块；
* **源码树**：``python <home>/engine/run.py --home <home> <tool> …`` 直接跑引擎自带入口，
  本模块不参与。

本模块只做三件事：解析 ``--home``、把 ``<home>/engine`` 放进 ``sys.path``、把剩下的
参数交给 ``atrix_research.cli.main``。**不 import LLM、不 import 网络层**，也刻意不在
模块顶层 import 引擎——引擎在磁盘上（用户配置目录），导入失败必须给出可读错误而不是
让 ``atri-x.exe`` 崩在启动阶段。
"""

from __future__ import annotations

import json
import os
import sys

from atri_x.research.paths import research_home

__all__ = ["main", "parse_target"]

#: ``--home`` / ``--config-dir`` 的等价写法（技能清单只用 ``--home``）。
_HOME_FLAGS = ("--home",)
_CONFIG_FLAGS = ("--config-dir",)


def _force_utf8_stdio() -> None:
    """统一标准流编码（Windows 控制台 cp936 会把 JSON 里的非 GBK 字符打成异常）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _emit(payload: dict[str, object]) -> None:
    """输出一条机器可读错误（与引擎的统一信封同形：``ok`` + ``error``）。"""
    print(json.dumps(payload, ensure_ascii=False))


def parse_target(argv: list[str]) -> tuple[str, str, list[str]]:
    """从参数里取出 ``--home`` / ``--config-dir``，返回 ``(home, config_dir, 剩余参数)``。

    只认**前置**的这两个开关（技能清单把它们放在工具名之前）；剩余参数原样交给引擎。
    """
    home = ""
    config_dir = ""
    rest = list(argv)
    while rest:
        head = rest[0]
        flag = ""
        for candidate in (*_HOME_FLAGS, *_CONFIG_FLAGS):
            if head == candidate or head.startswith(candidate + "="):
                flag = candidate
                break
        if not flag:
            break
        if head == flag:
            if len(rest) < 2:
                break
            value = rest[1]
            rest = rest[2:]
        else:
            value = head[len(flag) + 1 :]
            rest = rest[1:]
        if flag in _HOME_FLAGS:
            home = value
        else:
            config_dir = value
    resolved = home.strip() or research_home(config_dir)
    return os.path.abspath(resolved), config_dir, rest


def main(argv: list[str] | None = None) -> int:
    """运行一次引擎工具调用；返回引擎的退出码。"""
    _force_utf8_stdio()
    home, _config_dir, rest = parse_target(list(sys.argv[1:] if argv is None else argv))
    engine_dir = os.path.join(home, "engine")
    if not os.path.isfile(os.path.join(engine_dir, "atrix_research", "__init__.py")):
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "ENGINE_MISSING",
                    "message": "研究引擎未安装（首次启动应自动安装；可运行 atri-x.exe -m atri_x.research.bootstrap --check 排查）",
                    "home": home,
                },
            }
        )
        return 3
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    try:
        from atrix_research.cli import main as cli_main
    except Exception as exc:  # noqa: BLE001 - 引擎源码损坏时给出可读错误而不是栈
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "ENGINE_IMPORT_FAILED",
                    "message": f"{type(exc).__name__}: {exc}",
                    "home": home,
                },
            }
        )
        return 3
    return int(cli_main(["--home", home, *rest]))


if __name__ == "__main__":  # pragma: no cover - 由 -m 在子进程里执行
    raise SystemExit(main())
