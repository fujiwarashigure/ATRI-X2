"""漫画功能的装配层：规划模型客户端 + 平台头像适配。

为什么在 app 而不是 ``atri_x/comic``：这两件事都要碰"本进程的模型客户端表"和
"本进程的平台运行时列表"，属于装配知识；业务域（``atri_x/comic``）只认
``async plan(ctx, request) -> str`` 与 ``async avatar_url(ctx, platform, user_id)``
两个鸭子类型契约，因此可以完全离线单测。

与 ``image_rewriter`` 的对应关系：

* 两个都从 ``ModelClients`` 里挑一个模型槽（``naming`` / ``compact`` / ``chat`` /
  ``work``）并走 ``chat_stream``；
* 规划模型额外要求"只输出 JSON"，解析与校验在
  :mod:`atri_x.comic.planner`（本模块不做结构校验，避免两处规则漂移）。
"""

from __future__ import annotations

from typing import Any

from atri_x.app.dependencies import ModelClients
from atri_x.comic.characters import AVATAR_SIZE
from atri_x.comic.planner import ComicPlanRequest, build_plan_prompt
from atri_x.errors import InvalidError
from atri_x.llm import ChatRequest, LLMMessage, Role, text_segments

__all__ = [
    "COMIC_PLAN_MAX_TOKENS",
    "COMIC_PLAN_TEMPERATURE",
    "LLMComicPlanner",
    "PlatformAvatarResolver",
    "build_comic_planner",
    "comic_plan_selection",
]

#: 规划输出的上限：一页 12 格的 JSON 通常 1~2k token，留足余量。
COMIC_PLAN_MAX_TOKENS = 3000

#: 规划希望稳定、可复现，温度取低值。
COMIC_PLAN_TEMPERATURE = 0.3


class LLMComicPlanner:
    """用低成本模型做结构化分镜规划。"""

    def __init__(self, client: Any = None, model: str = "", library: Any = None) -> None:
        self.client = client
        self.model = (model or "").strip()
        self.library = library

    async def plan(self, ctx: Any, request: ComicPlanRequest) -> str:
        """一次模型调用，返回模型输出的 JSON 文本（校验由 planner 模块负责）。"""
        if self.client is None or not self.model:
            raise InvalidError("comic planning model is not configured")
        system, user = build_plan_prompt(request)
        stream = self.client.chat_stream(
            ctx,
            ChatRequest(
                model=self.model,
                messages=[
                    LLMMessage(role=Role.SYSTEM, segments=text_segments(system)),
                    LLMMessage(role=Role.USER, segments=text_segments(user)),
                ],
                temperature=COMIC_PLAN_TEMPERATURE,
                max_tokens=COMIC_PLAN_MAX_TOKENS,
            ),
        )
        out: list[str] = []
        usage = None
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            if getattr(chunk, "error", None) is not None:
                raise chunk.error
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage
            out.append(getattr(chunk, "delta_content", "") or "")
        value = "".join(out).strip()
        if not value:
            raise InvalidError("comic planner returned an empty result")
        from atri_x.agent.call_reason import side_reason
        from atri_x.llm.usage_sink import record_side_usage

        record_side_usage(reason=side_reason("comic_plan"), model=self.model, usage=usage)
        return value


def comic_plan_selection(cfg: Any) -> Any:
    """按 ``[comic].plan_model`` 解析规划模型槽（缺省跟随 work 模型）。"""
    from atri_x.config import ModelSelection
    from atri_x.storage import SessionModeChat, SessionModeWork

    mode_models = getattr(cfg, "mode_models", None) or {}
    work = mode_models.get(SessionModeWork) or ModelSelection()

    def fallback(selection: Any) -> Any:
        if not (
            str(getattr(selection, "provider", "") or "").strip()
            and str(getattr(selection, "model", "") or "").strip()
        ):
            return work
        return selection

    comic = getattr(cfg, "comic", None)
    slot = str(getattr(comic, "plan_model", "") or "").strip().lower()
    if slot == "naming":
        return fallback(getattr(cfg, "naming_model", None))
    if slot == "compact":
        return fallback(getattr(cfg, "compact_model", None))
    if slot == "chat":
        return fallback(mode_models.get(SessionModeChat))
    return work


def build_comic_planner(cfg: Any, models: ModelClients) -> Any:
    """挑规划模型槽；没配置就返回 ``None``（漫画回落到规则规划）。"""
    if cfg is None or models is None:
        return None
    selection = comic_plan_selection(cfg)
    client = (getattr(models, "by_provider", {}) or {}).get(
        getattr(selection, "provider", "")
    )
    model = str(getattr(selection, "model", "") or "").strip()
    if client is None or not model:
        return None
    return LLMComicPlanner(client, model)


class PlatformAvatarResolver:
    """按平台名在本进程的平台运行时里找头像能力。

    平台适配器的头像方法来自 ``platform.UserAvatarProvider`` 协议；没有该能力的
    平台（例如 CLI）会返回空串，业务域据此明确告知用户"没有头像"，而不是伪造。
    """

    def __init__(
        self, runtimes: list[Any] | None = None, *, size: int = AVATAR_SIZE
    ) -> None:
        self.runtimes = [item for item in (runtimes or []) if item is not None]
        self.size = int(size or 0) or AVATAR_SIZE

    def _adapter(self, platform: str) -> Any:
        wanted = (platform or "").strip()
        for runtime in self.runtimes:
            name = getattr(runtime, "name", None)
            try:
                current = name() if callable(name) else str(name or "")
            except Exception:  # noqa: BLE001 - 适配器自报名字失败就当不匹配
                continue
            if current == wanted:
                return runtime
        return None

    async def avatar_url(self, ctx: Any, platform: str, user_id: str) -> str:
        adapter = self._adapter(platform)
        if adapter is None:
            return ""
        getter = getattr(adapter, "get_user_avatar_url", None)
        if not callable(getter):
            return ""
        try:
            value = await getter(ctx, user_id, self.size)
        except Exception:  # noqa: BLE001 - 头像只是可选能力
            return ""
        return str(value or "").strip()
