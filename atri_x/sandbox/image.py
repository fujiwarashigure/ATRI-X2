"""沙箱镜像的构建输入与版本指纹。

镜像内容 = 本包内置 ``assets/Dockerfile`` + ``egress.py``。控制面在镜像缺失或指纹
不匹配时自动执行 ``docker build``；指纹写进镜像 label（``atri-x.build``），这样
「改了代理代码但镜像还是旧的」会被发现，而不是静默用旧镜像。

冻结产物里 ``atri_x/sandbox/assets`` 会作为数据目录随包（见
``packaging/atri-x.spec``），``egress.py`` 同时以数据文件形式复制到该目录，因此
``__file__`` 相对路径在源码运行与 exe 运行下都能解析到。
"""

from __future__ import annotations

import hashlib
import os

from atri_x.sandbox.errors import SandboxUnavailableError

__all__ = ["apt_setup_source", "build_digest", "dockerfile_text", "egress_source", "image_tag_default"]

#: 镜像 label：镜像内容指纹（Dockerfile + 代理源码）。
LABEL_KEY = "atri-x.build"


def image_tag_default() -> str:
    """默认镜像名（与 :class:`atri_x.config.sandbox.SandboxConfig` 的默认值一致）。"""
    return "atri-x-sandbox:1.2.0"


def assets_dir() -> str:
    """内置资产目录（源码与冻结产物同构）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "assets")
    if os.path.isdir(candidate):
        return candidate
    # 少数冻结布局把数据放在 _internal 下的同名路径；__file__ 已指向那里。
    return candidate


def _read_asset(name: str) -> str:
    """读取内置资产文本；缺失时明确报「沙箱不可用」。"""
    path = os.path.join(assets_dir(), name)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SandboxUnavailableError(
            "沙箱镜像构建资产缺失",
            detail=f"{name}: {exc}",
        ) from exc


def dockerfile_text() -> str:
    """内置 Dockerfile 文本。"""
    return _read_asset("Dockerfile")


def apt_setup_source() -> str:
    """apt 源设置与依赖安装脚本（构建时复制进镜像）。"""
    return _read_asset("apt-setup.sh")


def egress_source() -> str:
    """出口代理源码（构建时复制进镜像 ``/opt/atri/egress.py``）。"""
    path = os.path.join(assets_dir(), "egress.py")
    if not os.path.isfile(path):
        # 源码树里代理就在包目录下（与 assets/ 同级）。
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "egress.py")
        if os.path.isfile(candidate):
            path = candidate
        else:
            raise SandboxUnavailableError(
                "沙箱出口代理源码缺失",
                detail=path,
            )
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_digest() -> str:
    """镜像内容指纹：Dockerfile + apt 脚本 + 代理源码的 SHA-256（前 16 位）。"""
    hasher = hashlib.sha256()
    hasher.update(dockerfile_text().encode("utf-8"))
    hasher.update(b"\x00--apt--\x00")
    hasher.update(apt_setup_source().encode("utf-8"))
    hasher.update(b"\x00--egress--\x00")
    hasher.update(egress_source().encode("utf-8"))
    return hasher.hexdigest()[:16]
