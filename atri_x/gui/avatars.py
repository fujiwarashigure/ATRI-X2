"""群友头像：拼平台头像 URL、下载、解码缩放成统一的本地 PNG 缓存。

为什么要转成 PNG：Tk 的 ``PhotoImage`` 只认识 PNG/GIF/PPM，QQ 头像接口返回的是
**JPEG**。本仓已有纯标准库的图像编解码（:mod:`atri_x.media.imagecodec`），所以这里
直接用它「下载 → 解码 → 缩放到 96×96 → 重编码 PNG」，不引入第三方依赖。

缓存落在 ``<配置目录>/gui/avatars/`` 下，文件名是成员键的短哈希：同一成员反复下载
覆盖同一个文件，删成员时顺手删掉。
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request

from atri_x.media.imagecodec import (
    ImageCodecError,
    decode,
    encode_png,
    resize_to_fit,
)

__all__ = [
    "AVATAR_DIR",
    "AVATAR_SIZE",
    "AvatarError",
    "avatar_file",
    "avatar_url",
    "cache_avatar",
    "cache_avatar_file",
    "clear_avatar",
    "fetch_avatar_png",
]

#: 头像缓存目录名（相对 ``<配置目录>/gui/``）。
AVATAR_DIR = "avatars"

#: 落盘头像边长（像素）。界面上再按需要缩小显示。
AVATAR_SIZE = 96

#: 头像响应体上限（防止被塞一个巨型文件）。
MAX_AVATAR_BYTES = 4 * 1024 * 1024

#: 解码像素上限（防止解压炸弹）。
MAX_AVATAR_PIXELS = 16_000_000

#: 下载超时（秒）。
FETCH_TIMEOUT = 15.0

#: QQ 头像接口（``s`` 是边长，``nk`` 是 QQ 号）。
QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={uin}&s={size}"

#: 有公开头像接口的平台。
QQ_PLATFORMS = ("qqonebot", "qqofficial")


class AvatarError(Exception):
    """头像下载/解码的可预期失败（消息直接展示给用户）。"""


def avatar_url(platform: str, user_id: str, *, size: int = AVATAR_SIZE) -> str:
    """能给出头像地址的平台返回 URL，否则返回空串（Telegram / 本地 CLI 没有）。"""
    name = (platform or "").strip().lower()
    user = (user_id or "").strip()
    if name not in QQ_PLATFORMS or not user.isdigit():
        return ""
    return QQ_AVATAR_URL.format(uin=user, size=max(int(size), 40))


def fetch_avatar_png(
    url: str, *, size: int = AVATAR_SIZE, timeout: float = FETCH_TIMEOUT
) -> bytes:
    """下载头像并转成 PNG 字节；JPEG/GIF 走本仓解码器，缩放后重编码。"""
    target = (url or "").strip()
    if not target:
        raise AvatarError("这个平台没有提供头像地址")
    request = urllib.request.Request(
        target, headers={"User-Agent": "ATRI-X-GUI", "Accept": "image/*"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            data = response.read(MAX_AVATAR_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = getattr(exc, "code", "?")
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - 关不掉也不影响提示
            pass
        raise AvatarError(f"下载头像失败：HTTP {code}") from exc
    except urllib.error.URLError as exc:
        raise AvatarError(f"连不上头像地址：{getattr(exc, 'reason', exc)}") from exc
    except (TimeoutError, OSError) as exc:
        raise AvatarError(f"下载头像失败：{exc}") from exc
    if len(data) > MAX_AVATAR_BYTES:
        raise AvatarError(f"头像文件超过 {MAX_AVATAR_BYTES // 1024 // 1024} MB，已放弃")
    return to_png(data, size=size)


def to_png(data: bytes, *, size: int = AVATAR_SIZE) -> bytes:
    """任意受支持的图片字节 → 边长不超过 ``size`` 的 PNG 字节。"""
    try:
        image = decode(data, max_pixels=MAX_AVATAR_PIXELS)
        return encode_png(resize_to_fit(image, size, size))
    except ImageCodecError as exc:
        raise AvatarError(f"头像不是可识别的图片：{exc}") from exc


def cache_avatar(config_dir: str, key: str, png: bytes) -> str:
    """把 PNG 写进 ``<配置目录>/gui/avatars/<哈希>.png``，返回相对 ``gui/`` 的路径。"""
    directory = os.path.join(config_dir or "", "gui", AVATAR_DIR)
    os.makedirs(directory, exist_ok=True)
    name = _file_name(key)
    path = os.path.join(directory, name)
    temporary = path + ".tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(png)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise AvatarError(f"保存头像失败：{exc}") from exc
    return AVATAR_DIR + "/" + name


def cache_avatar_file(config_dir: str, key: str, source: str) -> str:
    """把本机图片文件转成 PNG 缓存（用户「选择本地图片」走这条路）。"""
    try:
        with open(source, "rb") as handle:
            data = handle.read(MAX_AVATAR_BYTES + 1)
    except OSError as exc:
        raise AvatarError(f"读不了这张图片：{exc}") from exc
    if len(data) > MAX_AVATAR_BYTES:
        raise AvatarError(f"图片超过 {MAX_AVATAR_BYTES // 1024 // 1024} MB，已放弃")
    return cache_avatar(config_dir, key, to_png(data))


def avatar_file(config_dir: str, relative: str) -> str:
    """相对路径 → 绝对路径（空串原样返回）。"""
    value = (relative or "").strip().replace("/", os.sep)
    if not value:
        return ""
    return os.path.join(config_dir or "", "gui", value)


def clear_avatar(config_dir: str, relative: str) -> bool:
    """删掉一个头像缓存文件；返回是否真的删了。"""
    path = avatar_file(config_dir, relative)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _file_name(key: str) -> str:
    digest = hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:16]
    return digest + ".png"
