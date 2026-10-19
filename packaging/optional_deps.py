"""可选依赖的构建计划：开关 → spec excludes、spec 契约、构建环境 PIL 探测。

职责边界（打印与命令行解析都在 ``packaging/build.py``）：

* :func:`plan` / :func:`plan_detail` —— 三个开关映射成 ``packaging/atri-x.spec``
  实际使用的 ``excludes``。开关语义与常量**只在这里定义一次**；
* :func:`spec_contract_ok` / :func:`plan_problems` —— 离线检查 spec 认不认精准
  开关（spec 若被回退成"三包绑死"的旧版，``--with-pillow`` 会静默连带
  numpy/PyYAML 进包，必须判失败）；
* :func:`backend_probe` —— 构建环境里实际生效的图像后端；``import PIL`` 的唯一
  出处仍是 ``atri_x.media.imagecodec.backend_probe``，这里不重复探测；
* :func:`pillow_env_verdict` —— ``--with-pillow`` 的构建环境判定，返回
  ``(退出码, 输出行, 错误行)``，打印留给调用方。

产物侧不做假断言（本模块不跑 PyInstaller、不解包产物）；冻结产物里到底有没有
Pillow 用部署侧**离线**命令核验::

    dist/windows/atri-x/atri-x.exe -m atri_x.media.imagecodec.backend_probe --json

环境变量契约（与 ``packaging/atri-x.spec`` 逐字对应，改一处要改两处）：

* ``ATRIX_PKG_KEEP_PILLOW=1`` → 不排除 Pillow（**只影响 Pillow**）；
* ``ATRIX_PKG_KEEP_EXTRAS=1`` → 不排除 numpy / PyYAML；
* ``ATRIX_PKG_KEEP_OPTIONAL=1`` → **等价于上面两个都置 1**（三包全留，旧语义）；
* 三者都未设置 → 三包全排除（原默认）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "BACKEND_PROBE_MODULE",
    "KEEP_EXTRAS_ENV",
    "KEEP_OPTIONAL_ENV",
    "KEEP_PILLOW_ENV",
    "OPTIONAL_PACKAGES",
    "add_cli_options",
    "backend_probe",
    "env_overrides",
    "env_report",
    "pillow_env_verdict",
    "plan",
    "plan_and_problems",
    "plan_detail",
    "plan_from_args",
    "plan_problems",
    "spec_contract_ok",
]

KEEP_PILLOW_ENV = "ATRIX_PKG_KEEP_PILLOW"
KEEP_EXTRAS_ENV = "ATRIX_PKG_KEEP_EXTRAS"
KEEP_OPTIONAL_ENV = "ATRIX_PKG_KEEP_OPTIONAL"
#: spec 里可被排除的可选第三方包（顺序与 excludes 输出一致）。
OPTIONAL_PACKAGES = ("numpy", "PIL", "yaml")
#: 产物级核验入口（离线；产物 exe 自带这个模块，见模块 docstring）。
BACKEND_PROBE_MODULE = "atri_x.media.imagecodec.backend_probe"


def add_cli_options(parser: argparse.ArgumentParser) -> None:
    """把可选依赖的开关注册到 ``parser``（文案与语义放一起，避免两边漂移）。"""
    parser.add_argument(
        "--with-pillow",
        action="store_true",
        help=(
            "bundle Pillow only (optional image codec accelerator); numpy/PyYAML stay excluded. "
            "Requires the build env to import PIL unless --allow-missing-pillow"
        ),
    )
    parser.add_argument(
        "--with-extras",
        action="store_true",
        help="also keep numpy/PyYAML (atri_x never imports them; default: excluded)",
    )
    parser.add_argument(
        "--with-all-optional",
        action="store_true",
        help="legacy semantics of the old --with-pillow: keep Pillow + numpy + PyYAML (ATRIX_PKG_KEEP_OPTIONAL=1)",
    )
    parser.add_argument(
        "--allow-missing-pillow",
        action="store_true",
        help="with --with-pillow: warn instead of failing when the build env cannot import PIL",
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="print build-environment facts (python/pyinstaller/Pillow/excludes) and exit",
    )


def plan(*, keep_pillow: bool, keep_extras: bool, legacy_all: bool = False) -> dict[str, Any]:
    """开关 → 构建计划；``excludes`` 与 spec 的逐包判定同序同名。

    ``legacy_all=True`` 是旧 ``ATRIX_PKG_KEEP_OPTIONAL=1`` 的语义：三包全留。
    """
    pillow = bool(keep_pillow or legacy_all)
    extras = bool(keep_extras or legacy_all)
    keep = {"numpy": extras, "PIL": pillow, "yaml": extras}
    return {
        "keep_pillow": pillow,
        "keep_extras": extras,
        "legacy_all": bool(legacy_all),
        "excludes": [name for name in OPTIONAL_PACKAGES if not keep[name]],
    }


def plan_from_args(args: Any) -> dict[str, Any]:
    """``argparse.Namespace`` → 计划（``packaging/build.py`` 只解析参数，映射在这里）。"""
    return plan(
        keep_pillow=bool(args.with_pillow or args.with_all_optional),
        keep_extras=bool(args.with_extras or args.with_all_optional),
        legacy_all=bool(args.with_all_optional),
    )


def plan_and_problems(args: Any, spec_path: Path) -> tuple[dict[str, Any], list[str]]:
    """``argparse.Namespace`` → ``(计划, 问题列表)``（问题空 = 可以构建）。"""
    build_plan = plan_from_args(args)
    return build_plan, plan_problems(
        build_plan,
        precise_requested=bool(args.with_pillow or args.with_extras),
        spec_path=spec_path,
    )


def env_overrides(build_plan: dict[str, Any]) -> dict[str, str]:
    """计划 → 传给 PyInstaller（由 spec 读）的三个环境变量。"""
    return {
        KEEP_PILLOW_ENV: "1" if build_plan["keep_pillow"] else "0",
        KEEP_EXTRAS_ENV: "1" if build_plan["keep_extras"] else "0",
        KEEP_OPTIONAL_ENV: "1" if build_plan["legacy_all"] else "0",
    }


def plan_detail(build_plan: dict[str, Any]) -> str:
    """一行摘要（构建日志里与 spec excludes 对账用）。"""
    return "Pillow=%s numpy/PyYAML=%s excludes=%s" % (
        "keep" if build_plan["keep_pillow"] else "exclude",
        "keep" if build_plan["keep_extras"] else "exclude",
        ", ".join(build_plan["excludes"]) or "<none>",
    )


def spec_contract_ok(spec_path: Path) -> bool:
    """spec 是否认识精准开关（纯文本检查，不 import PyInstaller）。"""
    if not spec_path.is_file():
        return False
    text = spec_path.read_text(encoding="utf-8")
    return KEEP_PILLOW_ENV in text and KEEP_EXTRAS_ENV in text


def plan_problems(
    build_plan: dict[str, Any], *, precise_requested: bool, spec_path: Path
) -> list[str]:
    """离线自检：开关、计划与 spec 契约不一致时返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    if precise_requested and not spec_contract_ok(spec_path):
        problems.append(
            "%s 不认识 %s / %s：精准开关会静默失效（--with-all-optional 仍可用）"
            % (spec_path.name, KEEP_PILLOW_ENV, KEEP_EXTRAS_ENV)
        )
    if build_plan["keep_pillow"] and "PIL" in build_plan["excludes"]:
        problems.append("PIL 被排除，但本次要求保留 Pillow（开关计划自相矛盾）")
    if build_plan["keep_extras"] and (
        "numpy" in build_plan["excludes"] or "yaml" in build_plan["excludes"]
    ):
        problems.append("numpy/PyYAML 被排除，但本次要求保留它们（开关计划自相矛盾）")
    return problems


def backend_probe(repo_root: Path) -> dict[str, Any]:
    """构建环境里的图像后端（``import PIL`` 唯一出处：``imagecodec.backend_probe``）。"""
    root = str(repo_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from atri_x.media.imagecodec.backend_probe import image_backend_info

        return dict(image_backend_info())
    except Exception as exc:  # noqa: BLE001 - 探测失败按"没有 Pillow"处理，由调用方决策
        return {
            "backend": "stdlib",
            "version": "",
            "pillow_installed": False,
            "reason": "%s: %s" % (type(exc).__name__, exc),
        }


def pillow_env_verdict(
    *, keep_pillow: bool, allow_missing: bool, info: dict[str, Any]
) -> tuple[int, str, str]:
    """``--with-pillow`` 的构建环境判定 → ``(退出码, 标准输出文本, 标准错误文本)``。

    构建环境里没有 PIL 时产物会静默退化成纯 Python 后端 —— 这正是要显式失败的地方；
    只有 ``--allow-missing-pillow`` 才降级成警告。文本已带换行，可直接原样写出
    （空串 = 该流没有输出）。
    """
    if not keep_pillow or info.get("pillow_installed"):
        return 0, "", ""
    reason = str(info.get("reason") or "unknown")
    if allow_missing:
        return (
            0,
            "warning     : import PIL failed (%s); artifact keeps the pure-Python codec only\n"
            % reason,
            "",
        )
    return (
        2,
        "",
        "\n".join(
            (
                "--with-pillow 需要构建环境能 import PIL，当前失败：%s" % reason,
                "install it with: %s -m pip install pillow" % sys.executable,
                "or pass --allow-missing-pillow to build without the image accelerator",
                "",
            )
        ),
    )


def env_report(
    info: dict[str, Any],
    *,
    keep_pillow: bool,
    allow_missing: bool,
    pyinstaller: str,
    artifact: str,
) -> tuple[int, str, str]:
    """``--check-env`` 的完整输出 → ``(退出码, 标准输出文本, 标准错误文本)``。

    输出顺序固定为：check-env 提示、PyInstaller、Pillow、产物路径，随后是 PIL 判定
    （缺少 PIL 且未放行时退出码为 2）。打印由 ``packaging/build.py`` 负责。
    """
    version = info.get("version") or "<not importable>"
    if not info.get("pillow_installed"):
        version += " (%s)" % (info.get("reason") or "unknown")
    code, stdout, stderr = pillow_env_verdict(
        keep_pillow=keep_pillow, allow_missing=allow_missing, info=info
    )
    head = "\n".join(
        (
            "check-env   : only probing, nothing will be built",
            "pyinstaller : %s" % pyinstaller,
            "pillow      : %s" % version,
            "artifact    : %s" % artifact,
            "",
        )
    )
    return code, head + stdout, stderr
