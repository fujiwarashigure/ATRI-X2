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

from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

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
    # 品牌图标：窗口图标（Tk iconbitmap）与托盘图标（LoadImageW）都要在运行期读文件，
    # 所以不能只嵌进 exe 资源。素材由 packaging/make_assets.py 生成。
    (os.path.join(REPO_ROOT, "atri_x", "gui", "brand"), "atri_x/gui/brand"),
]

#: 可选内置运行时（DSH 内核等）：整目录复制到 ``runtime/``。
if RUNTIME_DIR and os.path.isdir(RUNTIME_DIR):
    datas.append((RUNTIME_DIR, "runtime"))

#: 运行时零第三方依赖：这些包只有在本机装了、且被可选导入探测到时才会被拖进来。
#: ``tkinter`` 不能排除——窗口版要用。``PIL`` 与 numpy/PyYAML 分开判定：
#: ``--with-pillow``（``ATRIX_PKG_KEEP_PILLOW=1``）只保留 Pillow。
excludes = [
    name
    for name, keep in (("numpy", KEEP_EXTRAS), ("PIL", KEEP_PILLOW), ("yaml", KEEP_EXTRAS))
    if not keep
]

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
