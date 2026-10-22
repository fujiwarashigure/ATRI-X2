"""``atri_x.gui.icon``：品牌图标素材（窗口 / 托盘 / exe 共用）的解析与容器契约。

这里钉住两件事，它们都是「看起来没问题但实际会坏」的地方：

1. **运行时能不能找到素材**：源码运行与 PyInstaller 冻结后都靠 ``__file__`` 相对查找，
   素材缺失时必须返回空串（调用方降级），不能抛异常；
2. **ico 容器结构**：≤128 档必须是 32 位 DIB，256 档必须是 PNG——PyInstaller 的图标
   嵌入是「原样拷贝每个 ICONDIR 条目」，格式错了它不会报错，只会在 exe 里留下坏图标。
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from unittest import mock

from atri_x.gui import icon

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND = REPO_ROOT / "atri_x" / "gui" / "brand"

#: Windows 图标尺寸全集（和 ``packaging/make_assets.py`` 一致）。
EXPECTED_SIZES = {16, 20, 24, 32, 40, 48, 64, 96, 128, 256}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def ico_entries(path: Path) -> list[tuple[int, bytes]]:
    """返回 ``[(边长, 该条目裸数据前 8 字节)]``，顺便校验 ICONDIR 头。"""
    raw = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, kind) == (0, 1), f"不是 ICO 文件: reserved={reserved} type={kind}"
    entries: list[tuple[int, bytes]] = []
    offset = 6
    for _ in range(count):
        width, _height, _colors, _res, _planes, _bits, size, data_offset = struct.unpack(
            "<BBBBHHII", raw[offset : offset + 16]
        )
        offset += 16
        side = width or 256  # 256 在字节字段里记作 0
        entries.append((side, raw[data_offset : data_offset + 8]))
    return entries


def png_size(path: Path) -> tuple[int, int]:
    """从 PNG 的 IHDR 读宽高（不依赖 Pillow）。"""
    raw = path.read_bytes()
    assert raw[:8] == PNG_MAGIC, f"不是 PNG 文件: {path}"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


class BrandIconFileTest(unittest.TestCase):
    def test_ico_has_every_size(self) -> None:
        path = BRAND / "app.ico"
        self.assertTrue(
            path.is_file(), "缺少 atri_x/gui/brand/app.ico，先跑 python packaging/make_assets.py"
        )
        self.assertEqual({side for side, _ in ico_entries(path)}, EXPECTED_SIZES)

    def test_small_sizes_are_dib_and_256_is_png(self) -> None:
        entries = dict(ico_entries(BRAND / "app.ico"))
        for side in sorted(EXPECTED_SIZES - {256}):
            self.assertNotEqual(
                entries[side][:8], PNG_MAGIC, f"{side} 档应是 32 位 DIB（兼容性最好）"
            )
        self.assertEqual(entries[256][:8], PNG_MAGIC, "256 档必须是 PNG 压缩")

    def test_fallback_png_matches_ico_256_entry(self) -> None:
        self.assertEqual(png_size(BRAND / "app.png"), (256, 256))


class BrandIconResolverTest(unittest.TestCase):
    def test_resolver_points_at_existing_files(self) -> None:
        ico, png = icon.app_icon(), icon.app_icon_png()
        self.assertTrue(ico, "app_icon() 返回空串：品牌 ico 不在仓库里")
        self.assertTrue(png, "app_icon_png() 返回空串：品牌 png 不在仓库里")
        self.assertTrue(Path(ico).is_file())
        self.assertTrue(Path(png).is_file())
        self.assertTrue(icon.available())

    def test_missing_assets_degrade_to_empty_strings(self) -> None:
        """素材缺失只能是「没有图标」，不能让 GUI 起不来。"""
        empty = REPO_ROOT / "tests" / "_tmp_gui_icon" / "empty"
        with mock.patch.object(icon, "BRAND_DIR", empty):
            self.assertEqual(icon.app_icon(), "")
            self.assertEqual(icon.app_icon_png(), "")
            self.assertFalse(icon.available())


if __name__ == "__main__":
    unittest.main()
