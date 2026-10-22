"""品牌图标素材的运行时解析。

素材正文放在同目录 :file:`brand/` 下（:file:`app.ico` / :file:`app.png`），由
``packaging/make_assets.py`` 从品牌源图生成，**源码运行与 PyInstaller 冻结后走同一套
``__file__`` 相对查找**（和 :mod:`atri_x.config.assets` 的做法一致）。

为什么运行时还要带一份 .ico：Windows 上 Tk 的窗口图标不会自动继承 exe 的资源图标
（默认是 Tk 自己的羽毛图标），托盘的 ``LoadImageW`` 也必须给一个真实文件路径。
exe 图标、窗口图标、托盘图标、安装包图标因此共用这同一份 ``app.ico``。

素材缺失（例如有人裁剪了发行目录）时各函数返回空串，调用方负责降级——图标不是
启动的必要条件，不该因为找不到图就起不来。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BRAND_DIR", "app_icon", "app_icon_png", "available"]

#: 品牌素材目录。冻结后是 ``<_internal>/atri_x/gui/brand``。
BRAND_DIR = Path(__file__).resolve().parent / "brand"

#: 多尺寸图标（Windows ICO，10 档）。
ICON_NAME = "app.ico"

#: 备用 PNG（Tk ``iconbitmap`` 不可用时走 ``iconphoto``）。
PNG_NAME = "app.png"


def app_icon() -> str:
    """返回 ``app.ico`` 的绝对路径；文件不存在时返回空串。"""
    path = BRAND_DIR / ICON_NAME
    return str(path) if path.is_file() else ""


def app_icon_png() -> str:
    """返回 ``app.png`` 的绝对路径；文件不存在时返回空串。"""
    path = BRAND_DIR / PNG_NAME
    return str(path) if path.is_file() else ""


def available() -> bool:
    """是否至少有一个可用的图标素材。"""
    return bool(app_icon() or app_icon_png())
