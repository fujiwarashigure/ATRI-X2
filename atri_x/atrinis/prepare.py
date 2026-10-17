"""Atrinis 事件预处理：字段归一化、校验、去重键与各派生 JSON 列。

对应 Go：``internal/atrinis/prepare.go``。

:meth:`PrepareMixin.prepare_event` 是**唯一**把"外部请求"变成"内部事件"的地方，
它负责：

1. 逐字段 trim（``version`` / ``atriwisp.name`` / ``source`` / ``id`` / ``mode`` /
   ``format`` / ``model_slot`` / ``session_mode`` / ``content``）；
2. 校验（版本白名单、atriwisp 名正则、必填项、格式/模式白名单、媒体段、
   ``created_at`` 的 RFC3339、模型槽位、会话模式）；
3. 派生出落库要用的列：``event_key``、``content_hash``、``tags_json``、
   ``requested_targets``、``resolved_targets``、``tool_declarations``、``tool_hash``。

Go 的 ``prepareEvent`` 收的是 ``Request`` 值类型，因此**不会**改动调用方的请求；
Python 侧用浅拷贝（:func:`dataclasses.replace`）复刻这一语义——只深一层，
与 Go 的值拷贝一致（切片/映射仍是共享的）。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone

from atri_x.atriena import (
    Event,
    ModeDirect,
    ModeLLM,
    ModeRecord,
    Origin,
    Request,
    Target,
    VersionV2,
    VersionV3,
)
from atri_x.atrinis.background import is_atrinis_model_slot, is_atrinis_session_mode
from atri_x.atrinis.segments import validate_media_segment
from atri_x.atrinis.store import content_hash, hash_text, trim_strings
from atri_x.errors import InvalidError
from atri_x.storage import parse_time

__all__ = [
    "ATRIWISP_NAME_PATTERN",
    "PrepareMixin",
    "normalize_targets",
    "normalized_tool_declarations",
    "unique_targets",
]

#: atriwisp 名白名单。对应 Go ``atriwispNamePattern``（``^[A-Za-z0-9_-]+$``）。
#:
#: 用 :func:`re.fullmatch` 而不是 :meth:`re.Pattern.match`：Python 的 ``$`` 会在
#: 结尾换行前匹配，而 Go 的 ``$``（无 ``(?m)``）只在文本末尾匹配。
ATRIWISP_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

_ALLOWED_FORMATS = ("text", "atriyph")
_ALLOWED_MODES = (ModeRecord, ModeDirect, ModeLLM)


def unique_targets(targets: list[Target] | None) -> list[Target]:
    """按 ``(platform, type, id)`` 去重并排序。对应 Go ``uniqueTargets``。

    排序是稳定的三关键字升序，因此调用方拿到的是**确定顺序**的目标列表——
    direct 模式确实按这个顺序发送，测试依赖它。
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[Target] = []
    for target in targets or []:
        key = (target.platform, target.type, target.id)
        if key in seen:
            continue
        seen.add(key)
        out.append(target)
    out.sort(key=lambda item: (item.platform, item.type, item.id))
    return out


def normalize_targets(targets: list[Target] | None) -> list[Target]:
    """校验并归一化目标列表。对应 Go ``normalizeTargets``。

    ``platform="all"`` 不允许带 ``type``/``id``；具体目标里 ``type`` 只允许
    ``private`` / ``group`` 且必须带 ``id``；``type`` 为空时不允许带 ``id``。
    """
    if not targets:
        raise InvalidError("targets is required")
    out: list[Target] = []
    for target in targets:
        target.platform = (target.platform or "").strip()
        target.type = (target.type or "").strip()
        target.id = (target.id or "").strip()
        if not target.platform:
            raise InvalidError("targets.platform is required")
        if target.platform == "all":
            if target.type or target.id:
                raise InvalidError("targets with platform all cannot set type or id")
            out.append(target)
            continue
        if target.type == "":
            if target.id:
                raise InvalidError("targets.id requires type")
        elif target.type in ("private", "group"):
            if not target.id:
                raise InvalidError(f"targets.id is required for {target.type} target")
        else:
            raise InvalidError(f"unsupported target type {target.type!r}")
        out.append(target)
    return unique_targets(out)


def normalized_tool_declarations(tools: list | None) -> str:
    """工具声明的规范化 JSON（空列表返回空串）。对应 Go ``normalizedToolDeclarations``。"""
    from atri_x.atriena.types import tool_declaration_to_json

    if not tools:
        return ""
    return _marshal([tool_declaration_to_json(item) for item in tools])


def _marshal(value: object) -> str:
    """按 Go ``json.Marshal`` 的紧凑口径序列化（无空格、不转义非 ASCII）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class PrepareMixin:
    """事件预处理。对应 Go ``prepare.go`` 里的 ``Service`` 方法。"""

    def prepare_event(self, origin: Origin, req: Request) -> Event:
        """校验并预处理一次请求。对应 Go ``prepareEvent``（返回 error 时抛异常）。

        不会修改传入的 ``req``（Go 的值语义）。
        """
        req = replace(req, atriwisp=replace(req.atriwisp))
        req.version = (req.version or "").strip()
        req.atriwisp.name = (req.atriwisp.name or "").strip()
        req.source = (req.source or "").strip()
        req.id = (req.id or "").strip()
        req.mode = (req.mode or "").strip()
        req.format = (req.format or "").strip()
        req.model_slot = (req.model_slot or "").strip()
        req.session_mode = (req.session_mode or "").strip()
        req.content = (req.content or "").strip()

        if req.version not in (VersionV2, VersionV3):
            raise InvalidError(f"unsupported Atriena version {req.version!r}")
        if not req.atriwisp.name:
            raise InvalidError("atriwisp.name is required")
        if ATRIWISP_NAME_PATTERN.fullmatch(req.atriwisp.name) is None:
            raise InvalidError("atriwisp.name must use only letters, digits, _ or -")
        if not req.source:
            raise InvalidError("source is required")
        if not req.id:
            raise InvalidError("id is required")
        if not req.content and not req.segments and not req.calls:
            raise InvalidError("content, segments or calls is required")
        if not req.format:
            req.format = "text"
        if req.format not in _ALLOWED_FORMATS:
            raise InvalidError(f"unsupported format {req.format!r}")
        if req.mode not in _ALLOWED_MODES:
            raise InvalidError(f"unsupported mode {req.mode!r}")
        if req.mode == ModeLLM and not req.content:
            raise InvalidError("content is required for llm mode")
        if req.model_slot and not is_atrinis_model_slot(req.model_slot):
            raise InvalidError(f"unsupported model_slot {req.model_slot!r}")
        if req.session_mode and not is_atrinis_session_mode(req.session_mode):
            raise InvalidError(f"unsupported session_mode {req.session_mode!r}")
        for index, segment in enumerate(req.segments or []):
            try:
                validate_media_segment(segment)
            except InvalidError as exc:
                raise InvalidError(f"segment {index}: {exc}") from exc

        created_at = _now()
        raw_created_at = (req.created_at or "").strip()
        if raw_created_at:
            try:
                created_at = parse_time(raw_created_at)
            except ValueError as exc:
                raise InvalidError(f"created_at must be RFC3339: {exc}") from exc

        tags_json = _marshal(trim_strings(req.atriwisp.tags))
        targets = normalize_targets(req.targets)
        req.targets = targets
        requested_targets = _marshal([target.to_json() for target in targets])
        resolved = self.resolve_targets(req)
        resolved_targets = _marshal([target.to_json() for target in resolved])
        tool_declarations = normalized_tool_declarations(req.tools)

        return Event(
            request=req,
            origin=origin,
            event_key=f"{req.atriwisp.name}/{req.source}/{req.id}",
            content_hash=content_hash(req),
            tool_declarations=tool_declarations,
            tool_hash=hash_text(tool_declarations),
            tags_json=tags_json,
            requested_targets=requested_targets,
            resolved_targets=resolved_targets,
            created_at=created_at,
            received_at=_now(),
        )


def _now() -> datetime:
    """当前时间（UTC aware）。对应 Go ``time.Now()``。

    PORTING.md §1.7 要求时间一律带时区，故这里固定用 UTC 而不是本地时区。
    """
    return datetime.now(timezone.utc)
