"""draw.io 技能的子进程入口（技能 ``ATRIX_SKILL.toml`` 的 ``command`` 指向这里）。

两种运行形态共用本模块：

* **冻结产物**（安装版 / 便携版）：``atri-x.exe -m atri_x.diagram.runner
  --skill-dir <配置目录>/skills/agent/drawio_skill --action …``
  ——不能再依赖系统 Python，所以由 ``atri-x.exe`` 自己的 ``-m`` 转发
  （见 ``packaging/entry.py``）跑本模块；
* **源码树**：``python <配置目录>/skills/agent/drawio_skill/atrix_cli.py --action …``
  直接跑技能自带的适配器，本模块不参与。

本模块只做四件事：解析前置开关（``--skill-dir`` / ``--script``）、把标准流固定成 UTF-8、
把技能目录（或脚本所在目录）放进 ``sys.path``、用 :mod:`runpy` 执行目标脚本。
**不 import 适配器、不 import 渲染器**：适配器来自用户配置目录，导入失败必须给出可读
错误而不是让 ``atri-x.exe`` 崩在启动阶段。

``--script`` 模式是给冻结包里的**上游脚本**用的：``diagramctl.py build`` 会调
``sys.executable + <importers/xxx.py>`` 起导入器，冻结后 ``sys.executable`` 是
``atri-x.exe``，直接执行 ``.py`` 会被当成主程序参数。上游脚本已按
「``sys.frozen`` 时改走 ``-m atri_x.diagram.runner --script <绝对路径>``」打补丁
（见 ``drawio_pack/README.md``），本模块负责把这条路径真正跑起来。
"""

from __future__ import annotations

import json
import os
import runpy
import sys

__all__ = ["main", "parse_target"]

#: 前置开关（技能清单把它们放在动作参数之前）。
_SKILL_FLAGS = ("--skill-dir",)
_SCRIPT_FLAGS = ("--script",)

#: 适配器文件名（技能目录内）。
_ADAPTER_NAME = "atrix_cli.py"


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
    """输出一条机器可读错误（与适配器的统一信封同形）。"""
    print(json.dumps(payload, ensure_ascii=False))


def parse_target(argv: list[str]) -> tuple[str, str, list[str]]:
    """从参数里取出 ``--skill-dir`` / ``--script``，返回 ``(skill_dir, script, 剩余参数)``。

    只认**前置**的这两个开关（技能清单把它们放在动作参数之前）；剩余参数原样交给
    适配器或上游脚本。``--flag=value`` 与 ``--flag value`` 两种写法都接受。
    """
    skill_dir = ""
    script = ""
    rest = list(argv)
    while rest:
        head = rest[0]
        flag = ""
        for candidate in (*_SKILL_FLAGS, *_SCRIPT_FLAGS):
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
        if flag in _SKILL_FLAGS:
            skill_dir = value
        else:
            script = value
    return skill_dir.strip(), script.strip(), rest


def _run_path(path: str, argv: list[str]) -> int:
    """以 ``__main__`` 身份执行一个磁盘上的 Python 脚本，返回它的退出码。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and directory not in sys.path:
        sys.path.insert(0, directory)
    saved = sys.argv
    sys.argv = [path, *argv]
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as exc:  # 脚本自己用 SystemExit 表达退出码
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        sys.stderr.write("%s\n" % (code,))
        return 1
    except Exception as exc:  # noqa: BLE001 - 与 python -m 一致：异常即非零退出
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.stderr.write("drawio runner: %s\n" % (exc or type(exc).__name__,))
        return 1
    finally:
        sys.argv = saved
    return 0


def main(argv: list[str] | None = None) -> int:
    """运行一次 draw.io 技能调用；返回适配器/上游脚本的退出码。"""
    _force_utf8_stdio()
    skill_dir, script, rest = parse_target(list(sys.argv[1:] if argv is None else argv))
    if script:
        if not os.path.isfile(script):
            _emit(
                {
                    "content": "draw.io 运行器：脚本不存在（%s）" % script,
                    "segments": [],
                }
            )
            return 3
        return _run_path(script, rest)
    if not skill_dir:
        _emit(
            {
                "content": "draw.io 运行器：缺少 --skill-dir（技能未正确安装）",
                "segments": [],
            }
        )
        return 3
    adapter = os.path.join(skill_dir, _ADAPTER_NAME)
    if not os.path.isfile(adapter):
        _emit(
            {
                "content": "draw.io 技能未安装完整：%s 不存在（可运行 atri-x.exe -m "
                "atri_x.diagram.bootstrap --check 排查）" % adapter,
                "segments": [],
            }
        )
        return 3
    return _run_path(adapter, rest)


if __name__ == "__main__":  # pragma: no cover - 由 -m 在子进程里执行
    raise SystemExit(main())
