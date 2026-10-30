"""漫画业务服务（编排入口）：取材 → 人物 → 规划 → 编译 → 生成 → 记录。

这是"薄业务层"：HTTP 客户端、并发限流器、媒体中心、内容安全检查、角色库、
聊天历史全部由装配层注入（``atri_x/app/tool_runtime.py`` 与
``atri_x/tool/builtin/register.py`` 拿的是**和普通生图同一批对象**），所以：

* 不复制生图 HTTP 客户端（用的就是 ``imagegen.Client``）；
* 与 ``image_generate`` 共用 profile 与并发限流；
* 生图 profile 的 ``superadmin_only`` 在这里重新校验，普通用户不能借
  ``comic_render`` 绕过限制；
* 生图 profile 的 ``supports_reference`` 没开时直接拒绝，而不是静默丢参考图。

失败恢复（P1 的关键）：

* 生成/入库/投递是**三个独立状态**；投递只能记 ``delivery=requested``，成功回执
  由 Agent 层负责，这里不伪造；
* 请求超时等"可能已在服务端生成"的错误记为 ``state=unknown``，**不自动重试**；
* 记录里保存计划版本 + 指纹 + 完整提示词 + 参考图顺序，可解释"这张图怎么来的"；
* 已有 ``task_id`` 的重发直接用记录的 MediaID，不再调用生图接口。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from atri_x.comic.characters import (
    CharacterResolveRequest,
    NoAvatarResolver,
    resolve_comic_characters,
)
from atri_x.comic.context import collect_comic_context
from atri_x.comic.models import (
    ComicError,
    ComicRenderError,
    ComicStoreError,
    is_maybe_unknown_error,
    new_task_id,
)
from atri_x.comic.options import (
    DEFAULT_PANEL_COUNT,
    MAX_PANEL_COUNT,
    ComicOptions,
    PlanOutcome,
    PlanRequest,
    RenderOutcome,
    RenderRequest,
    comic_options_from,
    dedupe_warnings,
)
from atri_x.comic.planner import (
    ComicPlanRequest,
    parse_plan_response,
    rules_plan,
)
from atri_x.comic.prompt import build_comic_prompt
from atri_x.comic.records import ComicRecord
from atri_x.comic.references import character_presets, load_references
from atri_x.comic.store import ComicStore
from atri_x.imagegen import ProfileFrom, Request
from atri_x.media import Input as MediaInput
from atri_x.media import MediaReference, is_cancelled
from atri_x.safety import guard_from_context, scope_from_context
from atri_x.storage import now

__all__ = [
    "DEFAULT_PANEL_COUNT",
    "MAX_PANEL_COUNT",
    "ComicOptions",
    "ComicService",
    "PlanOutcome",
    "PlanRequest",
    "RenderOutcome",
    "RenderRequest",
    "comic_options_from",
]

#: 生成结果的落盘扩展名（与 mime 对应；生图服务一般只回 png/jpeg/webp）。
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}
#: 生成文件名里只允许安全字符（``safe_file_name`` 的轻量版，够这份用途）。
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class ComicService:
    """漫画业务编排。所有依赖都是鸭子类型，方便测试注入假实现。"""

    def __init__(
        self,
        options: ComicOptions,
        *,
        store: ComicStore,
        history: Any = None,
        characters: Any = None,
        media: Any = None,
        profiles: dict[str, Any] | None = None,
        default_profile: str = "",
        limiter: Any = None,
        planner: Any = None,
        avatar_resolver: Any = None,
        logger: Any = None,
    ) -> None:
        self.options = options
        self.store = store
        self.history = history
        self.characters = characters
        self.media = media
        self.profiles = dict(profiles or {})
        self.default_profile = (default_profile or "").strip()
        self.limiter = limiter
        self.planner = planner
        self.avatar_resolver = avatar_resolver if avatar_resolver is not None else NoAvatarResolver()
        self.logger = logger

    # -- 查询 ---------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.options.enabled and self.profiles)

    def available_planners(self) -> list[str]:
        modes = ["rules"]
        if self.planner is not None:
            modes.append("llm")
        return modes

    # -- 规划 ---------------------------------------------------------------
    async def plan(self, ctx: Any, request: PlanRequest, *, viewer: Any = None) -> PlanOutcome:
        if not self.enabled():
            raise ComicError(
                "漫画功能未启用：需要 [image_generation] 有可用 profile，且 [comic].enabled=true。"
            )
        context = collect_comic_context(
            self.history,
            ctx,
            anchor_message_id=request.anchor_message_id,
            query=request.context_query,
            limit=request.context_limit or self.options.context_limit,
        )
        resolved = await resolve_comic_characters(
            ctx,
            context=context,
            request=CharacterResolveRequest(
                mode=request.character_mode,
                character_ids=list(request.character_ids or []),
                reference_images=list(request.reference_images or []),
                max_characters=self.options.max_characters,
            ),
            store=self.characters,
            viewer=viewer,
            avatar_resolver=self.avatar_resolver,
        )
        panel_count = int(request.panel_count or 0) or self.options.default_panel_count
        if panel_count > self.options.max_panels:
            raise ComicError(
                f"格数最多 {self.options.max_panels} 格（收到 {panel_count}）；"
                "一页里太多格会让画面和台词都糊掉。"
            )
        plan_request = ComicPlanRequest(
            context=context,
            characters=resolved.characters,
            panel_count=panel_count,
            language=(request.language or self.options.language),
            layout=(request.layout or self.options.layout),
            title_hint=request.title_hint,
            instructions=request.instructions,
            max_scene_runes=self.options.max_scene_runes,
            max_dialogue_runes=self.options.max_dialogue_runes,
        )
        warnings = list(resolved.warnings)
        plan = await self._make_plan(ctx, plan_request, warnings)
        plan.warnings = dedupe_warnings([*warnings, *plan.warnings, *context.warnings])
        path = self.store.save_plan(plan)
        return PlanOutcome(plan=plan, plan_path=path, warnings=list(plan.warnings))

    async def _make_plan(
        self, ctx: Any, request: ComicPlanRequest, warnings: list[str]
    ) -> Any:
        mode = (self.options.planner or "auto").strip().lower()
        use_llm = mode in ("auto", "llm") and self.planner is not None
        if mode == "llm" and self.planner is None:
            raise ComicError(
                "配置要求 [comic].planner=llm，但没有可用的规划模型；"
                "请配置 chat/work 模型，或把 planner 改成 auto/rules。"
            )
        if use_llm:
            try:
                text = await self.planner.plan(ctx, request)
                return parse_plan_response(request, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if mode == "llm":
                    raise ComicError(f"模型规划失败：{exc}") from exc
                warnings.append(f"模型规划失败，已改用规则规划：{exc}")
        return rules_plan(request)

    # -- 生成 ---------------------------------------------------------------
    async def render(
        self, ctx: Any, request: RenderRequest, *, viewer: Any = None
    ) -> RenderOutcome:
        if not self.enabled():
            raise ComicError("漫画功能未启用。")
        plan = self.store.load_plan(request.plan_id, request.revision)
        latest = self.store.latest_revision(request.plan_id)
        if request.revision and request.revision != latest:
            raise ComicRenderError(
                f"计划 {plan.plan_id} 已更新到第 {latest} 版（请求的是第 {request.revision} 版）；"
                "请先用 comic_plan 重新预览确认。"
            )
        if request.task_id:
            return await self._resend(ctx, request, plan)

        profile_name, profile = self._select_profile(ctx, request.profile, viewer)
        cfg = profile.config
        client = profile.client
        enabled = getattr(client, "enabled", None)
        if callable(enabled) and not enabled():
            raise ComicRenderError("生图服务未启用：请检查 [image_generation] 的 API Key。")
        warnings: list[str] = []
        reference_characters, references = await load_references(
            ctx,
            plan,
            cfg,
            viewer,
            warnings,
            media=self.media,
            characters=self.characters,
            download=not request.dry_run,
        )
        size = (request.size or str(getattr(cfg, "size", "") or "")).strip()
        quality = (request.quality or str(getattr(cfg, "quality", "") or "")).strip()
        prompt = build_comic_prompt(
            plan,
            reference_characters=reference_characters,
            global_preset=str(getattr(cfg, "preset_prompt", "") or ""),
            character_presets=character_presets(
                ctx, plan, viewer, warnings, characters=self.characters
            ),
            extra_negative=str(getattr(cfg, "negative_prompt", "") or ""),
            negative_mode=str(getattr(cfg, "negative_prompt_mode", "") or "field"),
            max_runes=int(self.options.max_prompt_runes or 0),
        )
        warnings.extend(prompt.warnings)
        task_id = new_task_id()
        record = ComicRecord(
            task_id=task_id,
            plan_id=plan.plan_id,
            plan_revision=plan.revision,
            plan_fingerprint=plan.fingerprint(),
            profile=profile_name,
            model=str(getattr(cfg, "model", "") or ""),
            size=size,
            quality=quality,
            state="rendering",
            generation="pending",
            storage="pending",
            prompt=prompt.positive,
            negative=prompt.negative,
            reference_descriptors=[
                f"{item.display_name}:{item.reference.describe()}"
                for item in reference_characters
            ],
            warnings=list(warnings),
        )
        record.validate()
        if request.dry_run:
            record.state = "succeeded"
            record.generation = "ok"
            record.storage = "failed"
            record.error = "dry_run：只编译，未调用生图接口。"
            return RenderOutcome(
                plan=plan,
                record=record,
                prompt=prompt.positive,
                negative=prompt.negative,
                warnings=warnings,
                dry_run=True,
            )

        # 先落一条 rendering 记录：进程崩了也能查到"发起过什么"。
        self.store.save_record(record)
        filtered = self._filter_prompt(ctx, prompt.positive, prompt.negative)
        if filtered is None:
            record.state = "failed"
            record.generation = "failed"
            record.storage = "failed"
            record.error = "内容安全策略拒绝：漫画提示词不合规，未发起生成请求。"
            self.store.save_record(record)
            raise ComicRenderError(record.error)
        prompt.positive, prompt.negative = filtered
        record.prompt, record.negative = prompt.positive, prompt.negative
        if is_cancelled(ctx):
            record.state = "failed"
            record.generation = "failed"
            record.storage = "failed"
            record.error = "漫画生成已取消。"
            self.store.save_record(record)
            raise ComicRenderError(record.error)

        try:
            result = await self._generate(ctx, client, Request(
                prompt=prompt.positive,
                negative_prompt=prompt.negative,
                size=size,
                quality=quality,
                references=references,
            ))
        except asyncio.CancelledError:
            record.state = "failed"
            record.generation = "failed"
            record.storage = "failed"
            record.error = "漫画生成已取消。"
            self.store.save_record(record)
            raise
        except BaseException as exc:  # noqa: BLE001 - 统一落记录再翻成用户可读错误
            record.attempts += 1
            if is_maybe_unknown_error(exc):
                record.state = "unknown"
                record.generation = "unknown"
                record.storage = "failed"
                record.error = (
                    "生图请求超时/连接中断，服务端可能已经生成："
                    "已标记为结果未知，不会自动重试（避免重复扣费）。"
                )
                self.store.save_record(record)
                raise ComicRenderError(record.error) from exc
            record.state = "failed"
            record.generation = "failed"
            record.storage = "failed"
            record.error = f"生图失败：{exc}"
            self.store.save_record(record)
            raise ComicRenderError(record.error) from exc

        record.attempts += 1
        record.generation = "ok"
        record.revised_prompt = str(getattr(result, "revised_prompt", "") or "")
        record.model = str(getattr(result, "model", "") or record.model)
        stored = await self._store_media(ctx, record, result)
        if stored is None:
            raise ComicRenderError(record.error or "图片入库失败。")
        send = self.options.send_by_default if request.send is None else bool(request.send)
        record.state = "succeeded"
        record.delivery = "requested" if send else "not_requested"
        record.storage = "ok"
        record.warnings = dedupe_warnings(record.warnings)
        self.store.save_record(record)
        media_id = record.media_id
        if not self._allow_output(ctx):
            record.state = "failed"
            record.error = "内容安全策略在图片出口拦截了这张图。"
            self.store.save_record(record)
            raise ComicRenderError(record.error)
        return RenderOutcome(
            plan=plan,
            record=record,
            media_id=media_id,
            media_name=record.media_name,
            mime_type=record.mime_type,
            prompt=prompt.positive,
            negative=prompt.negative,
            revised_prompt=record.revised_prompt,
            warnings=list(record.warnings),
            dry_run=False,
        )

    async def _resend(self, ctx: Any, request: RenderRequest, plan: Any) -> RenderOutcome:
        record = self.store.load_record(request.task_id)
        if record is None:
            raise ComicRenderError(f"没有找到生成任务 {request.task_id}。")
        if record.plan_id != plan.plan_id:
            raise ComicRenderError(
                f"任务 {record.task_id} 属于计划 {record.plan_id}，与请求的计划 {plan.plan_id} 不一致。"
            )
        # 重发显示的是**当时那一版**计划的标题/分镜，不是最新版的（避免图文不匹配）。
        try:
            plan = self.store.load_plan(record.plan_id, record.plan_revision)
        except ComicStoreError:
            pass
        record = self.store.recover_interrupted(record)
        if not record.media_id:
            self.store.save_record(record)
            raise ComicRenderError(
                "这个任务没有可重发的图片（生成未成功）。"
                "如果状态是结果未知，请人工确认后再决定是否重画。"
            )
        record.attempts += 1
        record.delivery = "requested"
        record.state = "succeeded"
        self.store.save_record(record)
        return RenderOutcome(
            plan=plan,
            record=record,
            media_id=record.media_id,
            media_name=record.media_name,
            mime_type=record.mime_type,
            prompt=record.prompt,
            negative=record.negative,
            revised_prompt=record.revised_prompt,
            warnings=[f"重发已有图片（{record.media_id}），没有重新调用生图接口。"],
            reused=True,
        )

    # -- 依赖交互 -----------------------------------------------------------
    def _select_profile(
        self, ctx: Any, requested: str, viewer: Any
    ) -> tuple[str, Any]:
        name = (requested or "").strip() or ProfileFrom(ctx) or self.default_profile
        profile = self.profiles.get(name)
        if profile is None:
            raise ComicRenderError("没有配置生图 profile：" + name)
        cfg = profile.config
        if (
            name != self.default_profile
            and getattr(cfg, "superadmin_only", False)
            and not bool(getattr(viewer, "superadmin", False))
        ):
            raise ComicRenderError("生图 profile " + name + " 仅超级管理员可用。")
        return name, profile

    async def _generate(self, ctx: Any, client: Any, request: Any) -> Any:
        if self.limiter is not None:
            release = await self.limiter.acquire(ctx)
            try:
                return await client.generate(ctx, request)
            finally:
                release()
        return await client.generate(ctx, request)

    async def _store_media(self, ctx: Any, record: ComicRecord, result: Any) -> Any:
        if self.media is None:
            record.state = "failed"
            record.generation = "ok"
            record.storage = "failed"
            record.error = "Media Center 未配置，生成结果无法入库。"
            self.store.save_record(record)
            return None
        stamp = now().strftime("%Y%m%d-%H%M%S")
        mime_type = str(getattr(result, "mime_type", "") or "image/png")
        extension = _EXT_BY_MIME.get(mime_type.lower(), ".png")
        name = _SAFE_NAME.sub("_", f"comic-{stamp}-{record.task_id[:8]}{extension}")
        data = bytes(getattr(result, "data", b"") or b"")
        try:
            stored = await self.media.import_reader(
                ctx, data, len(data), MediaInput(name=name, mime_type=mime_type)
            )
        except Exception as exc:  # noqa: BLE001
            record.state = "failed"
            record.storage = "failed"
            record.error = f"图片入库失败：{exc}"
            self.store.save_record(record)
            return None
        record.media_id = str(getattr(stored, "id", "") or "")
        record.media_name = str(getattr(stored, "name", "") or name)
        record.mime_type = mime_type
        record.storage = "ok"
        add_reference = getattr(self.media, "add_reference", None)
        if callable(add_reference):
            try:
                add_reference(
                    MediaReference(
                        media_id=record.media_id,
                        owner_type="comic",
                        owner_id=record.task_id,
                        purpose="output",
                    )
                )
            except Exception:  # noqa: BLE001 - 引用登记失败不致命
                record.warnings = dedupe_warnings(
                    [*record.warnings, "登记媒体引用失败；清理策略可能提前回收这张图。"]
                )
        return stored

    # -- 安全检查 -----------------------------------------------------------
    def _filter_prompt(
        self, ctx: Any, positive: str, negative: str
    ) -> tuple[str, str] | None:
        guard = guard_from_context(ctx)
        if guard is None:
            return positive, negative
        scope = scope_from_context(ctx)
        positive_text, positive_decision = guard.filter_text(
            positive, stage="image_prompt", scope=scope, meta={"tool": "comic_render"}
        )
        negative_text, negative_decision = guard.filter_text(
            negative, stage="image_prompt", scope=scope, meta={"tool": "comic_render"}
        )
        blocked = [
            decision
            for decision in (positive_decision, negative_decision)
            if decision.action == "block" and guard.enforcing
        ]
        if blocked:
            return None
        return positive_text, negative_text

    def _allow_output(self, ctx: Any) -> bool:
        guard = guard_from_context(ctx)
        if guard is None:
            return True
        decision = guard.allow_media_output(
            "image", stage="tool", scope=scope_from_context(ctx), meta={"tool": "comic_render"}
        )
        return bool(decision.allowed)
