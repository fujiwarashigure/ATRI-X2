"""``tests/test_tool_skill*.py`` 共用的夹具：临时 skill 树与假媒体运行时。

放在独立模块里，让两个测试文件都不必重复样板；同时把 PORTING.md §7.2 的
「临时目录建在仓库根下」约定集中在一处。

* :func:`make_agent_skill` / :func:`make_go_skill` 造出扫描器认识的目录布局；
* :class:`FakeMediaRuntime` / :class:`FakeMediaCall` 是
  ``atri_x.tool.media_runtime``（另一位实现者负责）的假替身，只实现
  skill 包鸭子类型依赖的 ``export`` / ``workspace`` / ``import_`` / ``retain``
  / ``close``。
"""

from __future__ import annotations

import os
import shutil
import unittest
from dataclasses import dataclass, field
from typing import Any

from atri_x.atriyph import SKILL_FILE_NAME

__all__ = [
    "CALLABLE_TOML",
    "DOC_ONLY_TOML",
    "ECHO_SCRIPT",
    "REPO_ROOT",
    "TMP_ROOT",
    "VALID_ATRIYPH",
    "FakeMedia",
    "FakeMediaCall",
    "FakeMediaRuntime",
    "SkillTestBase",
    "make_agent_skill",
    "make_go_skill",
    "write_text",
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP_ROOT = os.path.join(REPO_ROOT, "tmp_skill_test")

VALID_ATRIYPH = "#skill demo - 演示 skill\n** risk low\n"

DOC_ONLY_TOML = """
risk = "high"
superadmin_only = true
"""

CALLABLE_TOML = """
risk = "medium"
superadmin_only = false
tags = ["doc", "doc"]
command = ["python", "echo_args.py"]
timeout_seconds = 10
expose_root = true

parameters = '''
{
  "type": "object",
  "required": ["input"],
  "properties": {
    "input": {"type": "string"},
    "count": {"type": "integer"},
    "flag": {"type": "boolean"},
    "items": {"type": "array", "items": {"type": "string"}},
    "nested": {"type": "object", "properties": {"blob": {"type": "media"}}},
    "media_in": {"type": "media"}
  }
}
'''

[args]
input = "--input"
count = "--count"
flag = "--flag"
items = "--items"
media_in = "--media"
"""

ECHO_SCRIPT = """import json
import sys

print(json.dumps({"content": "args=" + " ".join(sys.argv[1:])}, ensure_ascii=False))
"""


def write_text(path: str, text: str) -> None:
    """写 UTF-8 文本（``newline="\\n"``，不经 PowerShell）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def make_agent_skill(root: str, name: str, atriyph: str = "", toml: str = "") -> str:
    """造一个 ``agent/<name>/`` skill 目录。"""
    directory = os.path.join(root, "agent", name)
    os.makedirs(directory, exist_ok=True)
    write_text(
        os.path.join(directory, SKILL_FILE_NAME), atriyph or f"#skill {name} - {name}\n"
    )
    if toml:
        write_text(os.path.join(directory, "ATRIX_SKILL.toml"), toml)
    write_text(os.path.join(directory, "echo_args.py"), ECHO_SCRIPT)
    return directory


def make_go_skill(root: str, name: str) -> str:
    """造一个 ``go/<name>/`` skill 目录（含同名可执行文件桩）。"""
    directory = os.path.join(root, "go", name)
    os.makedirs(directory, exist_ok=True)
    write_text(
        os.path.join(directory, SKILL_FILE_NAME),
        f"#skill {name} - {name}\n** risk medium\n",
    )
    binary = os.path.join(directory, name + (".exe" if os.name == "nt" else ""))
    write_text(binary, "stub")
    if os.name != "nt":
        os.chmod(binary, 0o755)
    return directory


@dataclass
class FakeMedia:
    """最小媒体元数据（对应 ``atri_x.storage.Media`` 里用到的字段）。"""

    id: str = ""
    name: str = ""
    mime_type: str = ""
    size: int = 0


@dataclass
class FakeMediaCall:
    """媒体交接的假实现。对应 Go ``tool.MediaCall``。"""

    exported: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    closed: bool = False
    payload: bytes = b"media-bytes"

    def export(self, ctx: Any, media_id: str, base: str) -> tuple[str, FakeMedia]:
        del ctx
        relative = os.path.join("media", media_id.replace(":", "_") + ".bin")
        full = os.path.join(base, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(self.payload)
        self.exported.append(media_id)
        return relative, FakeMedia(
            id=media_id,
            name="x.bin",
            mime_type="application/octet-stream",
            size=len(self.payload),
        )

    def workspace(self, base: str) -> str:
        return os.path.join(base, "workspace")

    def import_(self, ctx: Any, base: str, path: str, input_: Any) -> FakeMedia:
        del ctx, base
        self.imported.append(path)
        return FakeMedia(
            id="media:" + "c" * 64,
            name=getattr(input_, "name", "") or os.path.basename(path),
            mime_type=getattr(input_, "mime_type", "") or "image/png",
            size=7,
        )

    def retain(self, ctx: Any, media_id: str) -> FakeMedia:
        del ctx
        self.retained.append(media_id)
        return FakeMedia(id=media_id, name="kept.bin", mime_type="image/png", size=7)

    async def close(self) -> None:
        self.closed = True


class FakeMediaRuntime:
    """``MediaRuntime`` 的假实现。对应 Go ``tool.MediaRuntime``。"""

    def __init__(self) -> None:
        self.calls: list[FakeMediaCall] = []

    def new_call(self) -> FakeMediaCall:
        call = FakeMediaCall()
        self.calls.append(call)
        return call


class SkillTestBase(unittest.TestCase):
    """在仓库根下建 ``tmp_skill_test/`` 并自动清理。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.root = TMP_ROOT

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
