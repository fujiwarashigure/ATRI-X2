"""从品牌源图生成打包用素材（图标 / 安装向导宣传图）。

这是**开发期工具**，只在更换品牌图时才需要跑；产物已随仓库提交，构建本身不依赖 Pillow
（``packaging/build.py`` 只是把 :file:`assets/` 里的文件传给 PyInstaller / Inno Setup）。

用法::

    python packaging/make_assets.py
    python packaging/make_assets.py --icon-source D:\\brand\\atr.png --banner-source D:\\brand\\atr-big.png

源图默认取自 :file:`packaging/brand-src/`（``icon-source.png`` / ``banner-source.png``），
**已随仓库提交**，所以换台机器也能重新生成；换品牌图时把两张图覆盖进去（或用参数指定）。

产出分两个目录，**同一个源图只生成一份，避免两处漂移**：

:file:`atri_x/gui/brand/`（运行期也要用，随包分发）
``app.ico``
    10 档 Windows 图标：16/20/24/32/40/48/64/96/128/256。
    ≤32 用脸部的紧凑特写（小尺寸下才看得清），≥48 用完整原图；
    两套都来自同一张源图，只是取景不同。≤128 存 32 位 DIB+AND 掩码，256 存 PNG
    （Vista+ 约定，体积也小）。exe 图标、窗口图标、托盘图标、安装包图标共用它。
``app.png``
    256×256，Tk ``iconphoto`` 的备用素材（``iconbitmap`` 不可用时才会用到）；内容与
    ico 里 256 那一档完全一致。

:file:`packaging/assets/`（只有安装包用）
``wizard-image*.png``
    Inno Setup 7 向导左侧宣传图：比例必须是 164:314。按官方 DPI 表给 100%/200%/250%
    三档尺寸，ISCC 会挑最接近的一档，避免高 DPI 下拉伸发糊。
``wizard-small*.png``
    Inno Setup 7 向导右上角小图（58×58，1:1，带圆角透明底）。

源图要求：图标源是正方形（建议 1024×1024，带或不带 alpha 都行）；宣传图源是竖构图，
比例接近 164:314 最好，差得多也能用（脚本按目标比例居中裁切，不会变形）。
"""

from __future__ import annotations

import argparse
import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

__all__ = ["main", "write_ico", "build_icon", "build_wizard_images"]

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent

#: 运行期也要用的图标素材（窗口 / 托盘 / exe / 安装包共用同一份）。
ICON_DIR = REPO_ROOT / "atri_x" / "gui" / "brand"

#: 只有安装包需要的素材。
ASSETS_DIR = HERE / "assets"

#: 品牌源图目录（随仓库提交，保证换机器也能重新生成素材）。
BRAND_SRC_DIR = HERE / "brand-src"

#: 默认源图：图标源（正方形）与安装向导宣传图源（竖构图）。
DEFAULT_ICON_SOURCE = BRAND_SRC_DIR / "icon-source.png"
DEFAULT_BANNER_SOURCE = BRAND_SRC_DIR / "banner-source.png"

#: Inno Setup 7 左侧图像区的纵横比（官方文档：164:314）。
BANNER_RATIO = 164 / 314

#: 第 100% DPI 下的图像区尺寸（Inno Setup 7 默认设置）。
BANNER_BASE = (202, 386)

#: 图标尺寸全集；``SMALL_ICON_MAX`` 及以下用脸部特写。
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SMALL_ICON_MAX = 32

#: 脸部特写取景（相对 1024 见方的比例）：左、上、右、下。
FACE_BOX_RATIO = (0.14, 0.08, 0.76, 0.70)

#: 宣传图横向取景（0=最左，1=最右）；0.35 时脸部大致居中。
BANNER_X_FRAC = 0.35


# ---------------------------------------------------------------------------
# ICO 容器
# ---------------------------------------------------------------------------

def _dib_bytes(img: Image.Image) -> bytes:
    """把 RGBA 图编码成 ICO 内嵌的 32 位 DIB（自底向上 BGRA + 1bpp AND 掩码）。"""
    width, height = img.size
    pixels = img.load()
    assert pixels is not None

    rows: list[bytes] = []
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    body = b"".join(rows)

    stride = ((width + 31) // 32) * 4
    mask_rows: list[bytes] = []
    for y in range(height - 1, -1, -1):
        bits = bytearray(stride)
        for x in range(width):
            if pixels[x, y][3] < 128:
                bits[x >> 3] |= 0x80 >> (x & 7)
        mask_rows.append(bytes(bits))
    mask = b"".join(mask_rows)

    # biHeight 是「高度 ×2」，因为后面还跟着 AND 掩码。
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, len(body) + len(mask), 0, 0, 0, 0
    )
    return header + body + mask


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def write_ico(path: Path, images: dict[int, Image.Image]) -> None:
    """写多尺寸 ``.ico``；``images`` 是 ``{边长: RGBA 图}``，尺寸需与实际像素一致。"""
    if not images:
        raise ValueError("images 不能为空")
    for size, img in images.items():
        if img.size != (size, size):
            raise ValueError(f"{size} 这一档的图像实际是 {img.size}")

    payloads: list[tuple[int, bytes]] = []
    for size in sorted(images):
        img = images[size].convert("RGBA")
        # 256 用 PNG（Vista+ 约定，体积小）；更小的档位用 DIB，兼容性最好。
        payloads.append((size, _png_bytes(img) if size >= 256 else _dib_bytes(img)))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    directory = bytearray()
    for size, data in payloads:
        field = 0 if size >= 256 else size  # 256 在字节字段里记作 0
        directory += struct.pack(
            "<BBBBHHII", field, field, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    path.write_bytes(header + bytes(directory) + b"".join(d for _s, d in payloads))


# ---------------------------------------------------------------------------
# 取景
# ---------------------------------------------------------------------------

def _square(img: Image.Image) -> Image.Image:
    """裁成正方形（居中）；源图已经是正方形时原样返回。"""
    width, height = img.size
    if width == height:
        return img
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def _crop_to_ratio(img: Image.Image, ratio: float, x_frac: float) -> Image.Image:
    """按目标纵横比裁切；``x_frac`` 是裁切框在可移动范围内的位置（0=最左，1=最右）。"""
    width, height = img.size
    if width / height > ratio:  # 太宽 → 裁两侧
        new_width = int(round(height * ratio))
        span = width - new_width
        left = int(round(span * max(0.0, min(1.0, x_frac))))
        return img.crop((left, 0, left + new_width, height))
    new_height = int(round(width / ratio))
    span = height - new_height
    top = int(round(span * max(0.0, min(1.0, x_frac))))
    return img.crop((0, top, width, top + new_height))


def _face_crop(icon: Image.Image) -> Image.Image:
    width, height = icon.size
    l, t, r, b = FACE_BOX_RATIO
    return icon.crop(
        (int(width * l), int(height * t), int(width * r), int(height * b))
    )


def _rounded(img: Image.Image, radius_frac: float = 0.2) -> Image.Image:
    """加圆角透明底（用于向导右上角小图）。"""
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    radius = int(round(size * radius_frac))
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    return img.resize(size, Image.LANCZOS)


# ---------------------------------------------------------------------------
# 素材构建
# ---------------------------------------------------------------------------

def build_icon(source: Image.Image) -> dict[int, Image.Image]:
    """生成 ``{边长: 图}``：小尺寸用脸部特写，大尺寸用完整原图。"""
    full = _square(source.convert("RGBA"))
    face = _face_crop(full)
    out: dict[int, Image.Image] = {}
    for size in ICON_SIZES:
        base = face if size <= SMALL_ICON_MAX else full
        out[size] = base.resize((size, size), Image.LANCZOS)
    return out


def build_wizard_images(source: Image.Image) -> dict[str, Image.Image]:
    """生成安装向导的左宣传图与小图（宣传图比例固定 164:314）。"""
    crop = _crop_to_ratio(source.convert("RGBA"), BANNER_RATIO, BANNER_X_FRAC)
    base_w, base_h = BANNER_BASE
    images = {
        "wizard-image.png": _resize(crop, (base_w, base_h)),
        "wizard-image-2x.png": _resize(crop, (base_w * 2, base_h * 2)),
        # Inno 文档里 250% DPI 的图像区是 534x1022。
        "wizard-image-250.png": _resize(crop, (534, 1022)),
    }
    # 小图区在 Inno 7 里是 58x58（标准 DPI）；再给一档 2 倍图。
    # 用脸部特写（和图标的小尺寸档一致），58px 下整图会糊成一团。
    small = _rounded(_face_crop(_square(source.convert("RGBA"))))
    images["wizard-small.png"] = _resize(small, (58, 58))
    images["wizard-small-2x.png"] = _resize(small, (116, 116))
    return images


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python packaging/make_assets.py",
        description="从品牌源图生成 packaging/assets/ 下的图标与安装向导素材",
    )
    parser.add_argument(
        "--icon-source", default=str(DEFAULT_ICON_SOURCE), help="图标源图（正方形）"
    )
    parser.add_argument(
        "--banner-source", default=str(DEFAULT_BANNER_SOURCE), help="安装向导宣传图源图（竖构图）"
    )
    parser.add_argument(
        "--out",
        default=str(ASSETS_DIR),
        help="安装向导素材输出目录（默认 packaging/assets）",
    )
    parser.add_argument(
        "--icon-out",
        default=str(ICON_DIR),
        help="图标素材输出目录（默认 atri_x/gui/brand）",
    )
    parser.add_argument(
        "--icon-variant",
        choices=("hybrid", "full", "face"),
        default="hybrid",
        help="hybrid=小尺寸用脸部特写（默认）；full=全部整图；face=全部特写",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    icon_path = Path(args.icon_source)
    banner_path = Path(args.banner_source)
    for path in (icon_path, banner_path):
        if not path.is_file():
            print(f"ERROR: 源图不存在: {path}", file=sys.stderr)
            return 2

    out = Path(args.out)
    icon_out = Path(args.icon_out)
    out.mkdir(parents=True, exist_ok=True)
    icon_out.mkdir(parents=True, exist_ok=True)

    icon_src = Image.open(icon_path)
    banner_src = Image.open(banner_path)
    print(f"icon source   : {icon_path} {icon_src.size} {icon_src.mode}")
    print(f"banner source : {banner_path} {banner_src.size} {banner_src.mode}")

    full = _square(icon_src.convert("RGBA"))
    face = _face_crop(full)
    if args.icon_variant == "full":
        images = {s: full.resize((s, s), Image.LANCZOS) for s in ICON_SIZES}
    elif args.icon_variant == "face":
        images = {s: face.resize((s, s), Image.LANCZOS) for s in ICON_SIZES}
    else:
        images = build_icon(icon_src)
    write_ico(icon_out / "app.ico", images)
    # 和 ico 里 256 那一档保持一致，避免两条路径显示成两张不同的图。
    images[256].save(icon_out / "app.png")

    for name, img in build_wizard_images(banner_src).items():
        img.save(out / name)

    print(f"\n图标素材: {icon_out}")
    for path in sorted(p for p in icon_out.iterdir() if p.is_file()):
        print(f"  {path.name:24s} {path.stat().st_size:>9,} B")
    print(f"\n安装包素材: {out}")
    for path in sorted(p for p in out.iterdir() if p.is_file()):
        print(f"  {path.name:24s} {path.stat().st_size:>9,} B")
    print(f"\n图标档位: {', '.join(str(s) for s in ICON_SIZES)}（≤{SMALL_ICON_MAX} 用脸部特写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
