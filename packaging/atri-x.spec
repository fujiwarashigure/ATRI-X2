# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 冻结描述：把 ``atri_x`` 打成 Windows 免安装包（控制台版 + 窗口版）。

不要直接 ``pyinstaller packaging/atri-x.spec``——参数（onefile / 名称 / 图标 / 可选依赖 /
内置运行时）由 :mod:`packaging.build` 通过环境变量传进来，单独跑 spec 会拿到默认值。

环境变量
--------
``ATRIX_PKG_ONEFILE``
    ``1`` 打成单文件 exe；``0``（默认）打成 onedir 目录（启动快、杀软误报少）。
``ATRIX_PKG_NAME``
    产物名，默认 ``atri-x``。窗口版自动叫 ``<name>-gui``。
``ATRIX_PKG_ICON``
    可选 ``.ico`` 路径（两个 exe 共用）。**留空不是「没有图标」而是 PyInstaller 自带
    的默认图标**（EXE(icon=None) 时 PyInstaller 用 ``bootloader/images/icon-windowed.ico``），
    所以正常构建由 :mod:`packaging.build` 默认传 ``atri_x/gui/brand/app.ico``。
``ATRIX_PKG_GUI``
    ``0`` 时不生成窗口版 exe（只出控制台版）。
``ATRIX_PKG_RUNTIME_DIR``
    可选：要打进包里的运行时目录（一般是仓库根的 ``runtime``，内含 ``dsh-kernel``）。
    onedir 下会复制到 ``<产物>/_internal/runtime``，由 :mod:`atri_x.sidecar` 解析。
``ATRIX_PKG_KEEP_PILLOW``
    ``1`` 时不排除 Pillow（图像编解码的可选加速后端；缺失时 ``atri_x`` 走纯 Python
    兜底）。**只影响 Pillow**，不会顺带把 numpy/PyYAML 打进包。
``ATRIX_PKG_KEEP_EXTRAS``
    ``1`` 时不排除 numpy / PyYAML。``atri_x`` 运行时不 import 它们，只有被可选探测
    拖进来时才需要，因此默认排除。
``ATRIX_PKG_KEEP_OPTIONAL``
    ``1`` **等价于上面两个都置 1**（= Pillow + numpy + PyYAML 三包全留）。保留这一
    旧语义只为向后兼容老脚本；新脚本请用 ``ATRIX_PKG_KEEP_PILLOW`` /
    ``ATRIX_PKG_KEEP_EXTRAS`` 精准开关。三者都没设置时维持原默认：三包全排除。

要点
----
* **必须** ``collect_submodules("atri_x")``：``--help`` / ``doctor`` 这类路径大量用
  ``importlib.import_module`` 延迟导入（``atri_x.app``、各平台适配器等），静态分析
  看不到。
* **必须** 带上两份数据资产，否则冻结后首次启动生成不了默认配置：
  ``atri_x/config/assets_data``（默认 app.toml / SOUL.md / skills 等）与
  ``atri_x/imagegen/prompts``（生图提示词库）。
* **必须** 带上 ``tkinter``（``atri_x.gui`` 需要）：所以它**不在** excludes 里。
* onedir 下两个 EXE 共用一个 ``COLLECT``（共享 ``_internal``，磁盘只占一份）；
  onefile 只能出一个 exe，因此单文件模式只出控制台版。
* 入口 ``entry.py`` 按 exe 文件名分流：``atri-x.exe`` 走 CLI，``atri-x-gui.exe`` 走 GUI。
"""

import os
import sys

REPO_ROOT = os.path.dirname(SPECPATH)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from PyInstaller.utils.hooks import collect_data_files  # noqa: E402
from PyInstaller.utils.hooks import collect_submodules  # noqa: E402
from PyInstaller.utils.hooks import copy_metadata  # noqa: E402

NAME = os.environ.get("ATRIX_PKG_NAME") or "atri-x"
ONEFILE = os.environ.get("ATRIX_PKG_ONEFILE") == "1"
KEEP_OPTIONAL = os.environ.get("ATRIX_PKG_KEEP_OPTIONAL") == "1"
#: 逐包判定（``KEEP_OPTIONAL`` 是"三包全留"的旧语义，向后兼容）。
KEEP_PILLOW = KEEP_OPTIONAL or os.environ.get("ATRIX_PKG_KEEP_PILLOW") == "1"
KEEP_EXTRAS = KEEP_OPTIONAL or os.environ.get("ATRIX_PKG_KEEP_EXTRAS") == "1"
WITH_GUI = os.environ.get("ATRIX_PKG_GUI", "1") != "0"
ICON = os.environ.get("ATRIX_PKG_ICON") or None
RUNTIME_DIR = (os.environ.get("ATRIX_PKG_RUNTIME_DIR") or "").strip()

#: 延迟导入的模块静态分析找不到，必须整包收集。
hiddenimports = collect_submodules("atri_x")

#: 运行期数据资产；路径映射必须与源码布局一致（``__file__`` 相对查找）。
datas = [
    (os.path.join(REPO_ROOT, "atri_x", "config", "assets_data"), "atri_x/config/assets_data"),
    (os.path.join(REPO_ROOT, "atri_x", "imagegen", "prompts"), "atri_x/imagegen/prompts"),
    # 内容安全词表：``atri_x/safety/rules.py`` 按包内相对路径读 ``wordlists/*.txt``。
    # 不收进来时冻结产物里 8 类词表全空，过滤会静默变成"只脱敏敏感信息"。
    (
        os.path.join(REPO_ROOT, "atri_x", "safety", "wordlists"),
        "atri_x/safety/wordlists",
    ),
    # 品牌图标：窗口图标（Tk iconbitmap）与托盘图标（LoadImageW）都要在运行期读文件，
    # 所以不能只嵌进 exe 资源。素材由 packaging/make_assets.py 生成。
    (os.path.join(REPO_ROOT, "atri_x", "gui", "brand"), "atri_x/gui/brand"),
]

#: 可选内置运行时（DSH 内核等）：整目录复制到 ``runtime/``。
if RUNTIME_DIR and os.path.isdir(RUNTIME_DIR):
    datas.append((RUNTIME_DIR, "runtime"))

#: 运行时零第三方**代码**依赖：这些包只有在本机装了、且被可选导入探测到时才会被拖进来。
#: ``tkinter`` 不能排除——窗口版要用。``PIL`` 与 numpy/PyYAML 分开判定：
#: ``--with-pillow``（``ATRIX_PKG_KEEP_PILLOW=1``）只保留 Pillow。
#:
#: 注意：这一段必须留在下面的 tzdata 收集段**之前**——测试会执行到收集段标记处，用它
#: 断言 excludes 的真实取值（见 ``tests/test_packaging_build.py``）。
excludes = [
    name
    for name, keep in (("numpy", KEEP_EXTRAS), ("PIL", KEEP_PILLOW), ("yaml", KEEP_EXTRAS))
    if not keep
]

# ===========================================================================
# TZDATA-COLLECT-BEGIN（测试只执行到这一行之前；见 tests/test_packaging_build.py）
#
# IANA 时区数据（**硬依赖**，1.0.10 起）
#
# Windows 没有系统 zoneinfo（``zoneinfo.TZPATH`` 为空），``[time].timezone`` 的 IANA 名
# 只能靠 PyPI 的 ``tzdata`` 包。历史上它是被 pyinstaller-hooks-contrib 的
# ``hook-tzdata.py`` **偶然**收进来的（构建机装了就有、没装就静默丢），这里改成
# 显式收集 + 构建期断言：收集不到 ``Asia/Shanghai`` 直接让构建失败。
# ===========================================================================
try:
    TZDATA_VERSION = __import__("importlib.metadata", fromlist=["version"]).version("tzdata")
except Exception as exc:  # noqa: BLE001 - 缺包就是构建错误
    raise SystemExit(
        "tzdata is required to build ATRI-X for Windows (IANA timezone data). "
        "Install it with: %s -m pip install tzdata  (cause: %s: %s)"
        % (sys.executable, type(exc).__name__, exc)
    )

#: ``tzdata.zoneinfo`` 下的全部数据文件 + 顶层 ``zones`` / ``zone.tab`` 等。
tzdata_datas = collect_data_files("tzdata")
tzdata_datas += copy_metadata("tzdata")
datas.extend(tzdata_datas)

#: ``zoneinfo`` 运行期用 ``importlib.resources.files("tzdata")`` 找数据，因此包本身
#: 必须被冻结进 PYZ（数据文件之外还要能 import）。
hiddenimports += collect_submodules("tzdata")

#: 构建期断言：数据必须在 TOC 里。``Asia/Shanghai`` 是默认时区，缺了就等于功能不可用。
_tz_sources = {str(src).replace("\\", "/") for src, _dest in tzdata_datas}
if not any(path.endswith("tzdata/zoneinfo/Asia/Shanghai") for path in _tz_sources):
    raise SystemExit(
        "tzdata collection failed: tzdata/zoneinfo/Asia/Shanghai is not in the bundle "
        "(collected %d tzdata file(s), version=%s)"
        % (len(tzdata_datas), TZDATA_VERSION)
    )
print("[atri-x.spec] tzdata %s: %d data file(s) collected" % (TZDATA_VERSION, len(tzdata_datas)))

# ===========================================================================
# 随包研究引擎（科研研究助理，1.1.3 起）
#
# 引擎本体**不进 PYZ**：它是 ``research_pack/engine`` 里的**数据文件**，首次启动由
# ``atri_x.research.bootstrap`` 复制到用户配置目录，再由
# ``atri-x.exe -m atri_x.research.runner`` 从磁盘加载（见 ``atri_x/research/runner.py``）。
# 这样换引擎版本不需要重打包，也不会出现"PYZ 里一份旧引擎把配置目录的新引擎盖掉"。
#
# 但引擎用到的**可选第三方依赖**必须以 PYZ 模块的形式在冻结进程里可导入，否则
# PDF 导出 / PDF 全文抽取会静默降级成"缺库"。这里按构建机是否装了它们来收集：
# 构建机没装 = 产物降级（引擎会显式声明 degraded），不算构建失败。
# ===========================================================================
RESEARCH_PACK_DIR = os.path.join(REPO_ROOT, "research_pack")
_research_meta = os.path.join(RESEARCH_PACK_DIR, "pack.json")
if not os.path.isfile(_research_meta):
    raise SystemExit(
        "research_pack/ is required to build ATRI-X (missing %s).\n"
        "它是 1.1.3 起的随包研究引擎与技能数据，由仓库根 research_pack/ 提供；"
        "被误删时构建应当失败，而不是出一个没有研究助理的包。" % _research_meta
    )
datas.append((RESEARCH_PACK_DIR, "research_pack"))

_research_deps: list[str] = []
for _optional in ("reportlab", "pypdf"):
    try:
        __import__(_optional)
    except Exception:  # noqa: BLE001 - 构建机没装就降级，不阻断构建
        continue
    hiddenimports += collect_submodules(_optional)
    datas += collect_data_files(_optional)
    _research_deps.append(_optional)
print("[atri-x.spec] research_pack: engine=%s deps=%s" % (
    "yes",
    ",".join(_research_deps) or "<none: PDF degrades>",
))

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
    collected = [exe]
    if WITH_GUI:
        gui_exe = EXE(
            pyz,
            a.scripts,
            [],
            exclude_binaries=True,
            name=NAME + "-gui",
            debug=False,
            bootloader_ignore_signals=False,
            strip=False,
            upx=False,
            console=False,
            disable_windowed_traceback=False,
            argv_emulation=False,
            target_arch=None,
            codesign_identity=None,
            entitlements_file=None,
            icon=ICON,
        )
        collected.append(gui_exe)
    coll = COLLECT(
        *collected,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=NAME,
    )
