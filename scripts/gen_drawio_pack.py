#!/usr/bin/env python3
"""生成/核对 ``drawio_pack/pack.json``（随包 draw.io 技能的摘要清单）。

清单只覆盖**会装进用户配置目录的运行文件**：``skills/drawio-skill/**`` 与
``tool.json``；``tests/`` 与 ``examples/`` 只用于仓库内的一致性测试，不进清单。

摘要算法（与 `file_count`/`files` 字段一起写死，构建期核验复用同一实现）：

1. 逐文件取相对于 ``drawio_pack/`` 的正斜杠路径、字节数、sha256；
2. 按路径排序，拼成 ``path\\tsize\\tsha256`` 行，以 ``\\n`` 连接；
3. 对整个字符串再取 sha256 → ``digest``。

用法：
    python scripts/gen_drawio_pack.py            # 重新生成
    python scripts/gen_drawio_pack.py --check    # 只核对（CI/门禁用）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

#: 仓库根（``scripts/gen_drawio_pack.py`` 的上两级）。
REPO_ROOT = Path(__file__).resolve().parents[1]
#: 随包目录。
PACK_DIR = REPO_ROOT / "drawio_pack"
#: 清单文件。
PACK_META = PACK_DIR / "pack.json"
#: 清单 schema（与 ``atri_x/diagram/pack.py`` 保持一致）。
PACK_SCHEMA = "atrix-drawio/pack/1"
#: 技能目录（只有这个子目录会装进配置目录）。
SKILL_SUBDIR = Path("skills") / "drawio-skill"
#: 额外进清单的顶层文件。
EXTRA_FILES = (Path("tool.json"),)
#: 不参与摘要的目录/文件（本地运行产物、字节码缓存）。
IGNORED_DIRS = {"out", "__pycache__", ".tmp"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _safe(text: str) -> str:
    """控制台是 GBK：输出统一走 ASCII 兜底（与 scripts/ 其它脚本一致）。"""
    encoding = getattr(sys.stdout, "encoding", "") or "ascii"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        return text.encode("ascii", errors="replace").decode("ascii")


def _pack_files() -> list[Path]:
    """按固定顺序列出参与摘要的文件（绝对路径）。"""
    out: list[Path] = []
    skill_root = PACK_DIR / SKILL_SUBDIR
    for path in sorted(skill_root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(PACK_DIR).parts
        if any(part in IGNORED_DIRS for part in parts[:-1]):
            continue
        if path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        out.append(path)
    for name in EXTRA_FILES:
        candidate = PACK_DIR / name
        if candidate.is_file():
            out.append(candidate)
    return sorted(out, key=lambda item: item.relative_to(PACK_DIR).as_posix())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_meta() -> dict:
    """计算 pack.json 的全部字段（不落盘）。"""
    lines: list[str] = []
    for path in _pack_files():
        rel = path.relative_to(PACK_DIR).as_posix()
        lines.append("%s\t%d\t%s" % (rel, path.stat().st_size, _sha256(path)))
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "schema": PACK_SCHEMA,
        "skill_version": "3.4.2",
        "upstream": {
            "name": "drawio-skill",
            "version": "3.4.2",
            "homepage": "https://github.com/Agents365-ai/drawio-skill",
            "license": "MIT",
        },
        "renderer": {"name": "draw.io", "version": "32.6.1"},
        "digest": digest,
        "file_count": len(lines),
        "files": lines,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/核对 drawio_pack/pack.json")
    parser.add_argument("--check", action="store_true", help="只核对，不写入")
    args = parser.parse_args(argv)
    meta = build_meta()
    if args.check:
        try:
            current = json.loads(PACK_META.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(_safe("pack.json 读取失败：%s" % exc))
            return 1
        if (
            current.get("digest") != meta["digest"]
            or current.get("file_count") != meta["file_count"]
        ):
            print(
                _safe(
                    "pack.json 过期：digest=%s files=%s（期望 digest=%s files=%s），"
                    "请运行 python scripts/gen_drawio_pack.py"
                    % (
                        current.get("digest"),
                        current.get("file_count"),
                        meta["digest"],
                        meta["file_count"],
                    )
                )
            )
            return 1
        print(_safe("pack.json 已是最新（%d 个文件）" % meta["file_count"]))
        return 0
    PACK_META.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(_safe("已写入 %s（%d 个文件，digest=%s）" % (PACK_META, meta["file_count"], meta["digest"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
