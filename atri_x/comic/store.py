"""漫画计划与生成记录的持久化（P0-8 / P1）。

为什么是文件而不是 SQLite 表：这份数据是**每页漫画一份自包含快照**（计划 +
一条生成记录），没有跨表 join、没有并发写热点；仓库里同类"小型自包含业务数据"
（画师串库、长记忆目录）也是文件。好处是升级不涉及数据库迁移，坏处是要自己保证
原子写与路径安全，这两件事本模块都做了：

* 所有 ID 先过白名单正则，再用 ``os.path.join``——不接受 ``..`` / 绝对路径；
* 写入先写 ``.tmp`` 再 ``os.replace``，崩溃最多留下一个临时文件；
* 读取前检查文件大小上限，损坏/不可解析一律报错而**不覆盖**。

目录布局::

    <配置目录>/comic/plans/<plan_id>.json        # 一份计划含全部 revision
    <配置目录>/comic/records/<task_id>.json      # 一次生成任务
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from atri_x.comic.models import (
    ComicDataError,
    ComicPlan,
    ComicStoreError,
)
from atri_x.comic.records import ComicRecord

__all__ = [
    "MAX_JSON_BYTES",
    "PLANS_DIR",
    "RECORDS_DIR",
    "ComicStore",
    "comic_root",
]

#: 单个 JSON 文件读取上限（正常计划几 KB；超过基本就是坏了）。
MAX_JSON_BYTES = 4 << 20

PLANS_DIR = "plans"
RECORDS_DIR = "records"

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def comic_root(config_dir: str) -> str:
    """漫画数据根目录：``<配置目录>/comic``。"""
    return os.path.join(str(config_dir or "").strip(), "comic")


def _check_id(value: Any, what: str) -> str:
    text = str(value or "").strip()
    if not text or not _ID_PATTERN.match(text):
        raise ComicStoreError(f"{what} 不合法：{value!r}")
    return text


class ComicStore:
    """计划与记录的只读/只写仓储（同步文件 IO，量级很小）。"""

    def __init__(self, root: str, logger: Any = None) -> None:
        self.root = os.path.abspath(str(root or "").strip() or ".")
        self.logger = logger
        self.last_error = ""

    # -- 路径 ---------------------------------------------------------------
    @property
    def plans_dir(self) -> str:
        return os.path.join(self.root, PLANS_DIR)

    @property
    def records_dir(self) -> str:
        return os.path.join(self.root, RECORDS_DIR)

    def plan_path(self, plan_id: str) -> str:
        return os.path.join(self.plans_dir, _check_id(plan_id, "计划 ID") + ".json")

    def record_path(self, task_id: str) -> str:
        return os.path.join(self.records_dir, _check_id(task_id, "任务 ID") + ".json")

    # -- 通用 IO ------------------------------------------------------------
    def _read_json(self, path: str) -> Any:
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ComicStoreError(f"读取文件失败：{exc}") from exc
        if size > MAX_JSON_BYTES:
            raise ComicStoreError(f"文件过大（{size} 字节），拒绝加载：{os.path.basename(path)}")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            raise ComicStoreError(f"文件损坏或不是合法 JSON：{os.path.basename(path)}") from exc

    def _write_json(self, path: str, payload: Any) -> str:
        directory = os.path.dirname(path)
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, path)
        except OSError as exc:
            raise ComicStoreError(f"写入文件失败：{exc}") from exc
        return path

    # -- 计划 ---------------------------------------------------------------
    def save_plan(self, plan: ComicPlan) -> str:
        """保存一个计划版本；同一 ``plan_id`` 的版本累积在一份文件里。"""
        plan.validate()
        path = self.plan_path(plan.plan_id)
        payload = self._load_plan_file(plan.plan_id)
        versions = payload.get("plans")
        if not isinstance(versions, dict):
            versions = {}
        key = str(plan.revision)
        versions[key] = plan.to_dict()
        latest = max(int(item) for item in versions) if versions else plan.revision
        payload = {
            "schema_version": plan.schema_version,
            "plan_id": plan.plan_id,
            "latest_revision": latest,
            "plans": versions,
        }
        return self._write_json(path, payload)

    def _load_plan_file(self, plan_id: str) -> dict[str, Any]:
        path = self.plan_path(plan_id)
        if not os.path.exists(path):
            return {}
        raw = self._read_json(path)
        if not isinstance(raw, dict):
            raise ComicStoreError(f"计划文件格式不对：{os.path.basename(path)}")
        return raw

    def load_plan(self, plan_id: str, revision: int = 0) -> ComicPlan:
        """读一个计划版本；``revision=0`` 读最新版本。"""
        raw = self._load_plan_file(plan_id)
        versions = raw.get("plans")
        if not isinstance(versions, dict) or not versions:
            raise ComicStoreError(f"计划 {plan_id} 不存在（可能已过期或没保存成功）。")
        wanted = int(revision or 0) or int(raw.get("latest_revision") or 0)
        if wanted <= 0:
            raise ComicStoreError(f"计划 {plan_id} 没有可读版本。")
        payload = versions.get(str(wanted))
        if not isinstance(payload, dict):
            raise ComicStoreError(
                f"计划 {plan_id} 没有第 {wanted} 版（当前最新 {raw.get('latest_revision')}）。"
            )
        try:
            plan = ComicPlan.from_dict(payload)
            plan.validate()
        except ComicDataError as exc:
            raise ComicStoreError(f"计划 {plan_id} 第 {wanted} 版结构损坏：{exc}") from exc
        return plan

    def latest_revision(self, plan_id: str) -> int:
        raw = self._load_plan_file(plan_id)
        return int(raw.get("latest_revision") or 0)

    # -- 生成记录 -----------------------------------------------------------
    def save_record(self, record: ComicRecord) -> str:
        record.validate()
        return self._write_json(self.record_path(record.task_id), record.to_dict())

    def load_record(self, task_id: str) -> ComicRecord | None:
        path = self.record_path(task_id)
        if not os.path.exists(path):
            return None
        raw = self._read_json(path)
        try:
            record = ComicRecord.from_dict(raw)
        except ComicDataError as exc:
            raise ComicStoreError(f"生成记录 {task_id} 结构损坏：{exc}") from exc
        return record

    def recover_interrupted(self, record: ComicRecord) -> ComicRecord:
        """重启后把卡在 ``rendering`` 的记录标成"结果未知"，**不**假装成功。

        调用方拿到返回的记录后应重新保存（本函数不落盘，保持纯函数式，方便测试）。
        """
        if record.state != "rendering":
            return record
        record.state = "unknown"
        record.generation = "unknown"
        if record.storage == "pending":
            record.storage = "failed"
        record.error = (
            "任务在生成过程中中断（进程重启或超时）；生图服务端可能已经生成，"
            "为避免重复扣费不会自动重试。"
        )
        return record
