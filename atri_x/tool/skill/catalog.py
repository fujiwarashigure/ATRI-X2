"""Skill 目录：种类、记录与线程安全目录。

对应 Go：``internal/tool/skill/catalog.go``。

``Record`` 是扫描结果与工具实现之间的唯一交换结构；``Catalog`` 用
``threading.RLock`` 保护（Go 是 ``sync.RWMutex``，Python 侧目录会被 asyncio
路径与同步的 ``detail()`` 同时读，所以用可重入的线程锁）。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field, replace

from atri_x.tool.skill.agent_manifest import AgentSkillManifest
from atri_x.tool.tool import RiskSafe, Source, SourceSkillAgent, SourceSkillGo

__all__ = [
    "Catalog",
    "Kind",
    "KindAgent",
    "KindGo",
    "Record",
    "SourceForKind",
    "new_catalog",
    "source_for_kind",
]


class Kind:
    """Skill 种类。对应 Go ``Kind``。"""

    AGENT = "agent"
    GO = "go"

    ALL = (AGENT, GO)


# Go 导出常量名逐字对齐。
KindAgent = Kind.AGENT
KindGo = Kind.GO


@dataclass
class Record:
    """一条已发现的 skill。对应 Go ``Record``。

    字段名按 PORTING.md §1.4 转 snake_case；语义与 Go 一一对应。
    """

    name: str = ""
    description: str = ""
    detail: str = ""
    detail_path: str = ""
    format: str = ""
    risk: str = RiskSafe
    superadmin_only: bool = False
    tags: list[str] = field(default_factory=list)
    kind: str = Kind.AGENT
    root: str = ""
    binary_path: str = ""
    manifest: AgentSkillManifest = field(default_factory=AgentSkillManifest)
    manifest_found: bool = False
    manifest_error: str = ""


class Catalog:
    """按名字索引的 skill 目录。对应 Go ``Catalog``。"""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._records: dict[str, Record] = {}

    def replace(self, records: list[Record] | None) -> None:
        """整体替换目录，并把 root/binary 规整成绝对路径。对应 Go ``Replace``。"""
        nxt: dict[str, Record] = {}
        for record in records or []:
            if record is None or record.name == "":
                continue
            record = replace(record)
            record.root = _absolute(record.root)
            if record.binary_path != "":
                record.binary_path = _absolute(record.binary_path)
            nxt[record.name] = record
        with self._mu:
            self._records = nxt

    def get(self, name: str) -> tuple[Record | None, bool]:
        """取一条记录。对应 Go ``Catalog.Get``。"""
        with self._mu:
            record = self._records.get((name or "").strip())
            return record, record is not None

    def list(self) -> list[Record]:
        """按名字排序返回全部记录。对应 Go ``Catalog.List``。"""
        with self._mu:
            out = list(self._records.values())
        out.sort(key=lambda record: record.name)
        return out


def _absolute(path: str) -> str:
    """``filepath.Abs`` 的对应物：出错时退回 ``filepath.Clean``。"""
    try:
        return os.path.abspath(path)
    except OSError:
        return os.path.normpath(path)


def source_for_kind(kind: str) -> str:
    """Skill 种类 → 工具来源。对应 Go ``SourceForKind``。"""
    if kind == Kind.GO:
        return SourceSkillGo
    return SourceSkillAgent


#: Go 导出名 ``SourceForKind`` 的 PascalCase 别名（便于交叉检索）。
SourceForKind = source_for_kind


def new_catalog() -> Catalog:
    """对应 Go ``NewCatalog``。"""
    return Catalog()


# 便于交叉检索：Source 常量与 Go 同名。
_ = Source
