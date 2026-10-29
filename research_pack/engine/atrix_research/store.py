"""文件读写、原子落盘与运行目录存取。

所有产物都是 JSON / Markdown / 文本，统一走这里，保证：

* 写入要么成功要么不留半截文件（临时文件 + ``os.replace``）；
* 读取失败区分"不存在"与"损坏"，后者提示用户而不是静默返回空；
* 运行目录内的路径只能由白名单文件名产生，拒绝任意路径拼接。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from .errors import ARTIFACT_MISSING, ENV_INVALID, ResearchError

__all__ = [
    "RunStore",
    "artifact_entry",
    "atomic_write_json",
    "atomic_write_text",
    "now_iso",
    "read_json",
    "read_text",
    "sha256_bytes",
]

#: 运行目录里允许的工具产物名（防止通过产物名做路径穿越）。
ARTIFACTS = frozenset(
    {
        "run.json",
        "search.json",
        "library.json",
        "screening.json",
        "criteria.json",
        "decisions.json",
        "prisma.md",
        "verification.json",
        "references.json",
        "references.md",
        "outline.json",
        "outline.md",
        "draft.md",
        "qa.json",
        "qa.md",
        "reviews.json",
        "panel.json",
        "panel.md",
        "audit.json",
        "audit.md",
        "report.md",
        "fulltext.json",
        "exports",
        "fulltext",
        "notes.md",
    }
)


def now_iso() -> str:
    """本地时区的 ISO-8601 秒级时间戳（带偏移，便于人读）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write_text(path: str, text: str) -> int:
    """原子写入文本，返回字节数。父目录自动创建。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = text.encode("utf-8")
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return len(payload)


def atomic_write_json(path: str, data: Any, *, indent: int = 2) -> int:
    """原子写入 JSON（UTF-8、不转义中文、稳定缩进）。"""
    text = json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=False) + "\n"
    return atomic_write_text(path, text)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(path: str, *, default: Any = None) -> Any:
    """读 JSON；不存在返回 ``default``，损坏抛 ``ENV_INVALID``。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ResearchError(
            ENV_INVALID,
            f"文件无法解析: {path}: {exc}",
            hint=f"删除或修复 {path} 后重试。",
        ) from None


def artifact_entry(path: str, run_dir: str) -> dict[str, Any]:
    """产物清单条目：相对路径 + 字节数（不含内容，避免结果膨胀）。"""
    rel = os.path.relpath(path, run_dir).replace(os.sep, "/")
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"path": rel, "bytes": size}


class RunStore:
    """绑定到单个运行目录的存取器。

    ``run_dir`` 必须已经存在（由 :meth:`RunStore.create_at` 建立）；构造时
    做一次路径归属校验，确保运行目录在 ``runs/`` 之下。
    """

    def __init__(self, run_dir: str, runs_root: str) -> None:
        run_dir = os.path.abspath(run_dir)
        runs_root = os.path.abspath(runs_root)
        if os.path.commonpath([run_dir, runs_root]) != runs_root:
            raise ResearchError(
                ENV_INVALID,
                f"运行目录不在 runs/ 之下: {run_dir}",
                hint="运行目录由引擎管理，不要手工指定任意路径。",
            )
        self.run_dir = run_dir
        self.runs_root = runs_root

    # -- 路径 --------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return os.path.basename(self.run_dir)

    def path(self, name: str) -> str:
        if name not in ARTIFACTS:
            raise ResearchError(
                ENV_INVALID, f"未知的产物名: {name!r}", hint="使用引擎提供的产物名。"
            )
        return os.path.join(self.run_dir, name)

    def exists(self, name: str) -> bool:
        return os.path.exists(self.path(name))

    # -- 运行状态 ----------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        data = read_json(self.path("run.json"))
        if not isinstance(data, dict):
            raise ResearchError(
                ARTIFACT_MISSING,
                f"运行记录缺失或损坏: {self.run_dir}",
                hint="用 research_doctor status 查看可用运行。",
            )
        return data

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = now_iso()
        atomic_write_json(self.path("run.json"), state)

    def touch(self, stage: str | None = None) -> dict[str, Any]:
        state = self.load_state()
        if stage:
            state["stage"] = stage
        self.save_state(state)
        return state

    # -- 产物 --------------------------------------------------------------

    def write(self, name: str, data: Any) -> dict[str, Any]:
        path = self.path(name)
        if name.endswith(".json"):
            atomic_write_json(path, data)
        else:
            atomic_write_text(path, str(data))
        return artifact_entry(path, self.run_dir)

    def read(self, name: str, *, required: bool = True, default: Any = None) -> Any:
        path = self.path(name)
        if name.endswith(".json"):
            return read_json(path, default=None if required else default)
        try:
            return read_text(path)
        except FileNotFoundError:
            if required:
                raise ResearchError(
                    ARTIFACT_MISSING,
                    f"缺少产物 {name}",
                    hint="确认上一步工具已成功执行（结果里的 artifacts 字段）。",
                ) from None
            return default

    def directory(self, name: str) -> str:
        """取得（并创建）产物子目录，如 ``exports`` / ``fulltext``。"""
        path = self.path(name)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def create_at(run_dir: str, runs_root: str) -> "RunStore":
        os.makedirs(run_dir, exist_ok=True)
        return RunStore(run_dir, runs_root)
