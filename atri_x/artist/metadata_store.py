"""入库转码前留档的图片生成参数（按 media id 索引）。

对应 Go：``internal/artist/metadata_store.go``。

为什么需要它：媒体管线会把超过 4 MiB 或长边 ≥4096 的图转码成 JPEG，而 PNG 的
tEXt/iTXt/zTXt 与 EXIF/XMP 块正是在那一步被丢掉的。导入器把**原始字节**交给本
store，因此参数在转码之前就留了档；记录按"落库后的对象 id"归档，之后所有工具都用
这个 id 取回。

三条不可动摇的规则（与 Go 注释里的意图一致）：

* 没有生成参数的图片**不写文件**（绝大多数照片都是这种），重复导入同一份字节也
  不重写文件：只有记录真的变了才落盘。
* 内存状态只在落盘成功之后才推进：``lookup`` 看到的记录必须真的在文件里，否则
  重启后工具会回答"参数来自入库留档"，而参数其实已经没了。
* 文件损坏 / schema 高于本程序 → 报错并**保留原文件**，绝不静默当成空库。

与 Go 的差异：

* Go 的 ``sync.Mutex`` → :class:`threading.Lock`；``context.Context`` 形参保留但
  不参与判断（与 Go 一样：这里只有本地有界文件读写）。
* ``os.ReadFile`` / ``fileops.AtomicWriteFile`` → :mod:`atri_x.utils.fileops`。
* 时间统一 RFC3339（:mod:`atri_x.storage.times`），``CapturedAt`` 始终输出。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from atri_x.errors import ErrDisabled, ErrInvalid, InvalidError
from atri_x.storage.times import ZERO_TIME, format_time, parse_optional_time
from atri_x.utils import fileops

from .metadata import extract_metadata
from .types import Metadata

#: 落盘 schema 版本与解析器版本。对应 Go ``GenerationSchema`` /
#: ``GenerationCaptureVersion`` / ``MaxGenerations``。
GENERATION_SCHEMA = 1
GENERATION_CAPTURE_VERSION = 1
MAX_GENERATIONS = 10000

_DIR_MODE = 0o750
_FILE_MODE = 0o600


@dataclass
class Generation:
    """一次导入留档：对象入库前的样子 + 从原始字节里恢复出的生成参数。

    对应 Go ``Generation``（字段名照抄 struct tag）。
    """

    media_id: str = ""
    source_hash: str = ""
    source_name: str = ""
    source_mime: str = ""
    stored_mime: str = ""
    source_size: int = 0
    stored_size: int = 0
    transcoded: bool = False
    capture_version: int = 0
    captured_at: datetime = ZERO_TIME
    container: str = ""
    source: str = ""
    positive: str = ""
    negative: str = ""
    parameters: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    prompts: list[str] = field(default_factory=list)
    workflow: str = ""
    warnings: list[str] = field(default_factory=list)

    def metadata(self) -> Metadata:
        """还原成 :class:`~atri_x.artist.types.Metadata`。对应 Go ``Generation.Metadata``。

        与 ``extract_metadata`` 一样，``parameters`` / ``text`` 永远是可用的 dict，
        调用方不需要因为"数据来自哪里"而做 None 判断；返回的 map/slice 归调用方所有。
        """
        return Metadata(
            container=self.container,
            source=self.source,
            positive=self.positive,
            negative=self.negative,
            parameters=dict(self.parameters or {}),
            text=dict(self.text or {}),
            prompts=list(self.prompts or []),
            workflow=self.workflow,
            warnings=list(self.warnings or []),
        )


def _time_to_json(value: datetime | None) -> str:
    return format_time(value or ZERO_TIME)


def _time_from_json(raw: Any) -> datetime:
    if not isinstance(raw, str) or raw.strip() == "":
        return ZERO_TIME
    return parse_optional_time(raw) or ZERO_TIME


def generation_to_json(record: Generation) -> dict[str, Any]:
    """对应 Go ``Generation`` 的 struct tag 序列化。"""
    out: dict[str, Any] = {
        "media_id": record.media_id,
        "capture_version": record.capture_version,
        "captured_at": _time_to_json(record.captured_at),
    }
    for key, value in (
        ("source_hash", record.source_hash),
        ("source_name", record.source_name),
        ("source_mime", record.source_mime),
        ("stored_mime", record.stored_mime),
        ("container", record.container),
        ("source", record.source),
        ("positive", record.positive),
        ("negative", record.negative),
        ("workflow", record.workflow),
    ):
        if value:
            out[key] = value
    for key, number in (
        ("source_size", record.source_size),
        ("stored_size", record.stored_size),
    ):
        if number:
            out[key] = number
    if record.transcoded:
        out["transcoded"] = True
    if record.parameters:
        out["parameters"] = dict(record.parameters)
    if record.text:
        out["text"] = dict(record.text)
    if record.prompts:
        out["prompts"] = list(record.prompts)
    if record.warnings:
        out["warnings"] = list(record.warnings)
    return out


def generation_from_json(raw: Any) -> Generation:
    data = raw if isinstance(raw, dict) else {}
    return Generation(
        media_id=str(data.get("media_id") or ""),
        source_hash=str(data.get("source_hash") or ""),
        source_name=str(data.get("source_name") or ""),
        source_mime=str(data.get("source_mime") or ""),
        stored_mime=str(data.get("stored_mime") or ""),
        source_size=_as_int(data.get("source_size")),
        stored_size=_as_int(data.get("stored_size")),
        transcoded=bool(data.get("transcoded")),
        capture_version=_as_int(data.get("capture_version")),
        captured_at=_time_from_json(data.get("captured_at")),
        container=str(data.get("container") or ""),
        source=str(data.get("source") or ""),
        positive=str(data.get("positive") or ""),
        negative=str(data.get("negative") or ""),
        parameters=_string_map(data.get("parameters")),
        text=_string_map(data.get("text")),
        prompts=[str(item) for item in data.get("prompts") or []],
        workflow=str(data.get("workflow") or ""),
        warnings=[str(item) for item in data.get("warnings") or []],
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def normalize_generations(records: list[Generation]) -> list[Generation]:
    """丢掉再也寻址不到的记录（空 id / 重复 id）。对应 Go ``normalizeGenerations``。

    ``capture_version`` 故意不动：旧解析器写的记录必须还能被认出来，
    这样下一次 capture 才会刷新它。
    """
    out: list[Generation] = []
    seen: set[str] = set()
    for record in records or []:
        media_id = (record.media_id or "").strip()
        record.media_id = media_id
        if media_id == "" or media_id in seen:
            continue
        seen.add(media_id)
        out.append(record)
    return out


def generation_older(left: Generation, right: Generation) -> bool:
    """按"旧在前"排序；同一时刻用 media id 兜底，保证是全序。对应 Go ``generationOlder``。"""
    left_time = left.captured_at or ZERO_TIME
    right_time = right.captured_at or ZERO_TIME
    if left_time != right_time:
        return left_time < right_time
    return left.media_id < right.media_id


def _clone_generation(record: Generation) -> Generation:
    return replace(
        record,
        parameters=(
            dict(record.parameters) if record.parameters is not None else None
        ),
        text=dict(record.text) if record.text is not None else None,
        prompts=list(record.prompts or []),
        warnings=list(record.warnings or []),
    )


class GenerationStore:
    """按 media id 归档的生成参数文件存储。对应 Go ``GenerationStore``。

    实现 :class:`atri_x.media.GenerationSink` 的鸭子契约：``capture_generation`` /
    ``forget_generation``。空路径 = 未启用：读为空、写抛 :data:`atri_x.errors.ErrDisabled`。
    """

    def __init__(
        self,
        path: str = "",
        max_records: int = 0,
        now: Callable[[], datetime] | None = None,
        write: Callable[[str, bytes, int], None] | None = None,
    ) -> None:
        self.path = (path or "").strip()
        #: 记录数上限；0 表示 :data:`MAX_GENERATIONS`，负数按 0 处理。
        self.max = max_records
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._write = write or fileops.atomic_write_file
        self._mu = threading.Lock()
        self._records: list[Generation] = []
        self._loaded = False

    # -- 基础信息 ---------------------------------------------------------
    def enabled(self) -> bool:
        """对应 Go ``GenerationStore.Enabled``。"""
        return self.path != ""

    def backing_path(self) -> str:
        """对应 Go ``GenerationStore.Path``。"""
        return self.path

    def set_clock(self, now: Callable[[], datetime] | None) -> None:
        """替换时间源（测试用）。对应 Go ``SetClock``。"""
        if now is not None:
            self._now = now

    def _timestamp(self) -> datetime:
        value = self._now() if self._now is not None else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def count(self) -> int:
        """记录数（诊断用；读不到文件按 0 计）。对应 Go ``Count``。"""
        if not self.enabled():
            return 0
        with self._mu:
            try:
                self._load_locked()
            except InvalidError:
                return 0
            return len(self._records)

    # -- 读 ---------------------------------------------------------------
    def lookup(self, media_id: str) -> tuple[Generation, bool]:
        """取某个 media id 的记录。对应 Go ``Lookup``。

        没归档过 → ``(Generation(), False)``；文件读不出来 → 抛
        :class:`atri_x.errors.InvalidError`（"这张图没有参数"和"参数文件读不了"
        是两件事，后者要被翻成可见 warning）。
        """
        if not self.enabled():
            raise ErrDisabled
        key = (media_id or "").strip()
        with self._mu:
            self._load_locked()
            index = self._index_locked(key)
            if index < 0:
                return Generation(), False
            return _clone_generation(self._records[index]), True

    def _index_locked(self, media_id: str) -> int:
        for index, record in enumerate(self._records):
            if record.media_id == media_id:
                return index
        return -1

    def _limit_locked(self) -> int:
        return self.max if self.max > 0 else MAX_GENERATIONS

    def _load_locked(self) -> None:
        """读一次文件。缺失/空 = 空库；其余读不出来的一律报错并保留原文件。"""
        if self._loaded:
            return
        records: list[Generation] = []
        try:
            raw = fileops.read_file(self.path)
        except FileNotFoundError:
            raw = None
        except OSError as exc:
            raise InvalidError(f"read image generation store: {exc}") from exc
        if raw is not None:
            text = raw.text or ""
            if text.strip() != "":
                try:
                    data = json.loads(text)
                except ValueError as exc:
                    raise InvalidError(
                        "invalid: 图片生成参数记录文件已损坏，已保留原文件："
                        + str(exc)
                    ) from exc
                if data is None:
                    raise InvalidError(
                        "invalid: 图片生成参数记录文件已损坏，已保留原文件"
                    )
                if not isinstance(data, dict):
                    raise InvalidError(
                        "invalid: 图片生成参数记录文件已损坏，已保留原文件"
                    )
                schema = _as_int(data.get("schema"))
                if schema > GENERATION_SCHEMA:
                    raise InvalidError(
                        "invalid: 图片生成参数记录文件的 schema 是 "
                        f"{schema}，高于本程序支持的 {GENERATION_SCHEMA}，已拒绝读取"
                    )
                records = normalize_generations(
                    [
                        generation_from_json(item)
                        for item in data.get("records") or []
                    ]
                )
        self._records = records
        # 只有读成功才置 loaded：遇到损坏文件的调用方可以修好文件后继续用，不必重启。
        self._loaded = True

    # -- 写 ---------------------------------------------------------------
    def capture_generation(self, ctx: Any, capture: Any) -> None:
        """给一张导入的图归档生成参数。对应 Go ``CaptureGeneration``。"""
        if not self.enabled():
            raise ErrDisabled
        meta, found = extract_metadata(getattr(capture, "original", b"") or b"")
        if not found:
            # 绝大多数照片没有生成参数：对象没变化，已有记录原样保留。
            return
        media_id = (getattr(capture, "media_id", "") or "").strip()
        if media_id == "":
            # 之后没人能寻址的记录是死重量，直接拒绝而不是存下来。
            raise InvalidError("invalid: 图片生成参数记录缺少 media id")
        with self._mu:
            self._load_locked()
            record = Generation(
                media_id=media_id,
                source_hash=getattr(capture, "source_hash", "") or "",
                source_name=getattr(capture, "source_name", "") or "",
                source_mime=getattr(capture, "source_mime", "") or "",
                stored_mime=getattr(capture, "stored_mime", "") or "",
                source_size=_as_int(getattr(capture, "source_size", 0)),
                stored_size=_as_int(getattr(capture, "stored_size", 0)),
                transcoded=bool(getattr(capture, "transcoded", False)),
                capture_version=GENERATION_CAPTURE_VERSION,
                captured_at=self._timestamp(),
                container=meta.container,
                source=meta.source,
                positive=meta.positive,
                negative=meta.negative,
                parameters=dict(meta.parameters or {}),
                text=dict(meta.text or {}),
                prompts=list(meta.prompts or []),
                workflow=meta.workflow,
                warnings=list(meta.warnings or []),
            )
            index = self._index_locked(media_id)
            if index >= 0:
                current = self._records[index]
                if (
                    current.source_hash == record.source_hash
                    and current.capture_version == record.capture_version
                ):
                    # 同一份字节 + 同一个解析器：已有记录就是这次要写的东西，
                    # 保留原采集时间、不落盘（重复导入必须零成本）。
                    return
            previous = list(self._records)
            if index >= 0:
                self._records[index] = record
            else:
                self._records.append(record)
            self._evict_locked()
            try:
                self._save_locked()
            except BaseException:
                self._records = previous
                raise

    def forget_generation(self, ctx: Any, media_id: str) -> None:
        """删掉一个正在被删除的 media 对象的记录。对应 Go ``ForgetGeneration``。

        与 capture 相反：这里落盘失败**故意**保留内存里的删除——media 行已经没了，
        没人能再查回这条记录，删除会在下一次成功写入时落盘；回滚反而会让它复活。
        """
        if not self.enabled():
            raise ErrDisabled
        key = (media_id or "").strip()
        with self._mu:
            self._load_locked()
            index = self._index_locked(key)
            if index < 0:
                return
            del self._records[index]
            self._save_locked()

    def _evict_locked(self) -> None:
        """按"最旧优先"淘汰到上限内。对应 Go ``evictLocked``。"""
        limit = self._limit_locked()
        if len(self._records) <= limit:
            return
        order = sorted(
            range(len(self._records)),
            key=lambda i: (
                self._records[i].captured_at or ZERO_TIME,
                self._records[i].media_id,
            ),
        )
        drop = set(order[: len(self._records) - limit])
        self._records = [
            record
            for index, record in enumerate(self._records)
            if index not in drop
        ]

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path)
        if directory not in ("", "."):
            os.makedirs(directory, mode=_DIR_MODE, exist_ok=True)
        payload = {
            "schema": GENERATION_SCHEMA,
            "records": [generation_to_json(item) for item in self._records],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, indent="  ")
        except (TypeError, ValueError) as exc:
            raise InvalidError(f"encode image generation store: {exc}") from exc
        try:
            self._write(self.path, (text + "\n").encode("utf-8"), _FILE_MODE)
        except OSError as exc:
            raise InvalidError(f"write image generation store: {exc}") from exc


def new_generation_store(path: str) -> GenerationStore:
    """对应 Go ``NewGenerationStore``。"""
    return GenerationStore(path)


__all__ = [name for name in dir() if not name.startswith("_")]
