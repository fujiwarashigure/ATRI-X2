#!/usr/bin/env python3
"""生成/核对 ``research_pack/pack.json`` 的逐文件摘要与总摘要。

``research_pack/`` 由上游 ``atri-research`` 引擎复制而来，随包只作为数据目录使用。
上游没有随包提供清单生成器，``0.2.0`` 时代的 ``digest`` 算法也无从复现；本脚本沿用
``scripts/gen_drawio_pack.py`` 已写死的同一套规则，保证清单**可复算**：

1. 文件集合与顺序直接继承 ``pack.json`` 里现有的 ``files`` 路径（不猜测收录范围）；
2. 逐文件取字节数与 sha256，拼成 ``path\\tsize\\tsha256`` 行，以 ``\\n`` 连接；
3. 对整个字符串再取 sha256 → ``digest``。

只刷新 ``files``/``file_count``/``digest``，其余字段（版本、技能清单）原样保留。

用法：
    python scripts/gen_research_pack.py            # 重新生成
    python scripts/gen_research_pack.py --check    # 只核对（过期返回 1）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: 仓库根（``scripts/gen_research_pack.py`` 的上两级）。
REPO_ROOT = Path(__file__).resolve().parents[1]
#: 随包目录。
PACK_DIR = REPO_ROOT / "research_pack"
#: 清单文件。
PACK_META = PACK_DIR / "pack.json"


def _safe(text: str) -> str:
    """控制台是 GBK：输出统一走 ASCII 兜底（与 scripts/ 其它脚本一致）。"""
    encoding = getattr(sys.stdout, "encoding", "") or "ascii"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        return text.encode("ascii", errors="replace").decode("ascii")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _listed_paths(current: dict) -> list[str]:
    """从现有清单取相对路径（``path\\tsize\\tsha256`` 的第一列）。"""
    out: list[str] = []
    for line in current.get("files") or []:
        rel = str(line).split("\t", 1)[0].strip()
        if rel:
            out.append(rel)
    return out


def build_meta(current: dict) -> dict:
    """按当前清单的路径集合重算摘要（不落盘）。"""
    lines: list[str] = []
    for rel in _listed_paths(current):
        path = PACK_DIR / Path(rel.replace("/", "\\"))
        if not path.is_file():
            raise FileNotFoundError(f"清单里的文件不存在：{rel}")
        lines.append("%s\t%d\t%s" % (rel, path.stat().st_size, _sha256(path)))
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    meta = dict(current)
    meta["digest"] = digest
    meta["file_count"] = len(lines)
    meta["files"] = lines
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/核对 research_pack/pack.json")
    parser.add_argument("--check", action="store_true", help="只核对，不写入")
    args = parser.parse_args(argv)
    try:
        current = json.loads(PACK_META.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(_safe("pack.json 读取失败：%s" % exc))
        return 1
    if not isinstance(current, dict):
        print(_safe("pack.json 不是对象"))
        return 1
    try:
        meta = build_meta(current)
    except FileNotFoundError as exc:
        print(_safe(str(exc)))
        return 1
    if args.check:
        if (
            current.get("digest") != meta["digest"]
            or current.get("file_count") != meta["file_count"]
            or current.get("files") != meta["files"]
        ):
            print(
                _safe(
                    "pack.json 已过期（digest=%s files=%s，期望 digest=%s files=%s），"
                    "请运行 python scripts/gen_research_pack.py"
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
    print(
        _safe(
            "已写入 %s（%d 个文件，digest=%s）"
            % (PACK_META, meta["file_count"], meta["digest"])
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
