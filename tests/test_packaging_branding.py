"""打包侧的品牌素材接线：spec 收集、Inno 指令、build.py 默认图标。

这些断言都是「少一行就静默退化成默认图标」的东西——不写测试的话，下次有人删掉
``datas`` 里的一行或 iss 里的一条指令，构建仍然会成功，只是图又没了。
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING = REPO_ROOT / "packaging"
if str(PACKAGING) not in sys.path:
    sys.path.insert(0, str(PACKAGING))

import build  # noqa: E402 - 需要先修 sys.path

SPEC = PACKAGING / "atri-x.spec"
ISS = PACKAGING / "atri-x.iss"
BRAND_ICO = REPO_ROOT / "atri_x" / "gui" / "brand" / "app.ico"

#: Inno 左侧图像区的纵横比（官方文档：画面恒为 164:314）。
BANNER_RATIO = 164 / 314


def png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"不是 PNG 文件: {path}"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


def iss_value(text: str, directive: str) -> str:
    """取 iss 里 ``directive=...`` 的原始值（忽略注释行）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(";") or not stripped.lower().startswith(directive.lower() + "="):
            continue
        return stripped.split("=", 1)[1].strip()
    return ""


def iss_path(value: str) -> Path:
    """把 iss 里的相对路径解析成真实文件路径。

    ``.iss`` 是 Windows 安装包脚本，路径一律用反斜杠（``assets\\wizard-image.png``、
    ``..\\atri_x\\gui\\brand\\app.ico``）。POSIX 上反斜杠不是分隔符，直接
    ``PACKAGING / value`` 会得到一个不存在的文件名（CI 的 ubuntu 分片就是这样红
    的）。这里统一把 ``\\`` 换成 ``/`` 再拼——同一份 iss 在任何平台上都指向同一个
    文件，测试也就与宿主平台无关了。
    """
    return (PACKAGING / value.strip().replace("\\", "/")).resolve()


class SpecBrandTest(unittest.TestCase):
    def test_brand_dir_is_collected_as_data(self) -> None:
        """窗口图标与托盘图标都要在运行期读文件，所以必须进 datas。"""
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("atri_x/gui/brand", text)
        self.assertIn('"gui", "brand"', text)


class BuildDefaultIconTest(unittest.TestCase):
    def test_default_icon_points_at_the_brand_ico(self) -> None:
        self.assertEqual(build.DEFAULT_ICON, BRAND_ICO)
        self.assertTrue(build.DEFAULT_ICON.is_file())


class InstallerBrandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ISS.read_text(encoding="utf-8")

    def test_setup_icon_is_set(self) -> None:
        value = iss_value(self.text, "SetupIconFile")
        self.assertTrue(value, "atri-x.iss 没写 SetupIconFile：安装包会用 Inno 默认图标")
        path = iss_path(value)
        self.assertTrue(path.is_file(), f"SetupIconFile 指向的文件不存在: {path}")
        self.assertEqual(path, BRAND_ICO.resolve())

    def test_wizard_images_are_set_and_exist(self) -> None:
        for directive in ("WizardImageFile", "WizardSmallImageFile"):
            value = iss_value(self.text, directive)
            self.assertTrue(value, f"atri-x.iss 没写 {directive}")
            for item in value.split(","):
                path = iss_path(item)
                self.assertTrue(path.is_file(), f"{directive} 指向的文件不存在: {path}")

    def test_welcome_page_is_enabled(self) -> None:
        """左侧宣传图只在欢迎页 / 完成页显示，Inno 6/7 默认跳过欢迎页。"""
        self.assertEqual(iss_value(self.text, "DisableWelcomePage").lower(), "no")

    def test_banner_assets_keep_the_required_aspect(self) -> None:
        value = iss_value(self.text, "WizardImageFile")
        for item in value.split(","):
            path = iss_path(item)
            width, height = png_size(path)
            self.assertAlmostEqual(
                width / height, BANNER_RATIO, delta=0.01, msg=f"{path.name} 比例不是 164:314"
            )

    def test_small_image_is_square(self) -> None:
        value = iss_value(self.text, "WizardSmallImageFile")
        for item in value.split(","):
            path = iss_path(item)
            width, height = png_size(path)
            self.assertEqual(width, height, f"{path.name} 不是正方形")


if __name__ == "__main__":
    unittest.main()
