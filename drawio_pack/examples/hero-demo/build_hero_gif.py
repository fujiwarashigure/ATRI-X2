#!/usr/bin/env python3
"""Regenerate assets/hero-demo.gif: main.tf -> v1 diagram -> main-v2.tf -> v2 diagram.

Run from the repo root:  python3 examples/hero-demo/build_hero_gif.py
Needs: drawio CLI, ffmpeg, Pillow. Outputs are written next to this script and
the final GIF lands in assets/hero-demo.gif.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "examples" / "hero-demo"
SCRIPTS = ROOT / "skills" / "drawio-skill" / "scripts"
OUT = ROOT / "assets" / "hero-demo.gif"
W, H = 1280, 720
BG, PANEL = "#0d1117", "#161b22"
FG, COMMENT, KEYWORD, STRING = "#c9d1d9", "#8b949e", "#ff7b72", "#a5d6ff"
HOLD_S = 2.4

FONT = "/System/Library/Fonts/Menlo.ttc"
font = ImageFont.truetype(FONT, 18, index=0)
font_bold = ImageFont.truetype(FONT, 18, index=1)


def run(cmd, cwd=ROOT):
    subprocess.run(
        [str(c) for c in cmd],
        check=True,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def build_diagrams(tmp):
    for name, tf in (("v1", "main.tf"), ("v2", "main-v2.tf")):
        graph = tmp / f"{name}.json"
        run(
            [
                sys.executable,
                SCRIPTS / "tfimports.py",
                HERE / tf,
                "--direction",
                "LR",
                "-o",
                graph,
            ]
        )
        run(
            [
                sys.executable,
                SCRIPTS / "autolayout.py",
                graph,
                "-o",
                HERE / f"{name}.drawio",
            ]
        )
        run(
            [
                "drawio",
                "-x",
                "-f",
                "png",
                "--width",
                "1200",
                "-o",
                tmp / f"{name}.png",
                HERE / f"{name}.drawio",
            ]
        )


def code_frame(path, highlight_from=None):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    panel = (40, 40, W - 40, H - 40)
    d.rounded_rectangle(panel, radius=18, fill=PANEL)
    lines = path.read_text().splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if highlight_from and highlight_from in ln),
        len(lines),
    )
    per_col, line_h, col_w = 18, 23, 600
    for i, line in enumerate(lines):
        col, row = divmod(i, per_col)
        x = panel[0] + 24 + col * col_w
        y = panel[1] + 24 + row * line_h
        hi_right = min(x + col_w - 30, panel[2] - 10)
        if i >= start:
            d.rectangle((x - 8, y - 2, hi_right, y + line_h), fill="#1d3325")
            d.rectangle((x - 8, y - 2, x - 3, y + line_h), fill="#3fb950")
        words = line.split("#", 1)
        code = words[0].rstrip()
        if code.strip().startswith("resource"):
            d.text((x, y), code, font=font_bold, fill=KEYWORD)
        else:
            d.text((x, y), code, font=font, fill=FG)
            # re-color string spans crudely: anything inside double quotes
            for seg in code.split('"')[1::2]:
                pos = code.find(f'"{seg}"')
                if pos >= 0:
                    x0 = x + d.textlength(code[:pos], font=font)
                    d.text((x0, y), f'"{seg}"', font=font, fill=STRING)
        if len(words) == 2:
            xc = x + d.textlength(code, font=font)
            d.text((xc, y), "#" + words[1], font=font, fill=COMMENT)
    return img


def diagram_frame(png):
    img = Image.new("RGB", (W, H), "white")
    pic = Image.open(png).convert("RGB")
    scale = min((W - 80) / pic.width, (H - 80) / pic.height)
    pic = pic.resize((int(pic.width * scale), int(pic.height * scale)))
    img.paste(pic, ((W - pic.width) // 2, (H - pic.height) // 2))
    return img


def main():
    with tempfile.TemporaryDirectory(dir=HERE) as td:
        tmp = Path(td)
        build_diagrams(tmp)
        frames = [
            code_frame(HERE / "main.tf"),
            diagram_frame(tmp / "v1.png"),
            code_frame(HERE / "main-v2.tf", highlight_from="# v2 adds"),
            diagram_frame(tmp / "v2.png"),
        ]
        listfile = tmp / "frames.txt"
        with listfile.open("w") as f:
            for i, fr in enumerate(frames):
                p = tmp / f"frame{i}.png"
                fr.save(p)
                f.write(f"file '{p.name}'\nduration {HOLD_S}\n")
            f.write("file 'frame3.png'\n")
        palette = tmp / "palette.png"
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-i",
                listfile.name,
                "-vf",
                "palettegen",
                "-update",
                "1",
                palette.name,
            ],
            cwd=tmp,
        )
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-i",
                listfile.name,
                "-i",
                palette.name,
                "-lavfi",
                "fps=10 [x]; [x][1:v] paletteuse",
                OUT,
            ],
            cwd=tmp,
        )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
