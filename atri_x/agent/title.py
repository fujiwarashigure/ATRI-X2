"""会话标题生成器。

对应 Go：``internal/agent/title.go``。

Go 的 ``sync.RWMutex`` → :class:`threading.RLock`（本类全是同步小操作）。
``llm.LLM.ChatStream(ctx, req)`` 在 Python 侧是**异步生成器**，因此
:meth:`TitleGenerator.generate_title` 也是协程。
"""

from __future__ import annotations

import threading
from typing import Any

from atri_x.llm import ChatRequest, LLMMessage, Role, text_segments
from atri_x.redact import summarize
from atri_x.session import TitleResult
from atri_x.storage import RoleAssistant, RoleUser
from atri_x.agent.errors import MAX_USER_ERROR_RUNES

__all__ = ["TitleGenerator", "title_prompt"]


class TitleGenerator:
    """按"专用命名模型优先、主模型兜底"的顺序生成标题。

    对应 Go ``titleGenerator``。字段名与 Go 一致（``primary`` /
    ``primary_model`` / ``naming`` / ``naming_model`` / ``naming_source``）。
    """

    def __init__(
        self,
        primary: Any = None,
        primary_model: str = "",
        naming: Any = None,
        naming_model: str = "",
        naming_source: str = "",
        logger: Any = None,
    ) -> None:
        self._mu = threading.RLock()
        self.primary = primary
        self.primary_model = primary_model
        self.naming = naming
        self.naming_model = naming_model
        self.naming_source = naming_source
        self.logger = logger

    async def generate_title(self, ctx: Any, messages: list[Any]) -> TitleResult:
        """对应 Go ``titleGenerator.GenerateTitle``。"""
        with self._mu:
            naming, naming_model, naming_source = (
                self.naming,
                self.naming_model,
                self.naming_source,
            )
            primary, primary_model = self.primary, self.primary_model
            logger = self.logger
        if naming is not None and naming_model:
            try:
                title = await self._generate(ctx, naming, naming_model, messages)
                return TitleResult(raw_title=title)
            except Exception as exc:  # noqa: BLE001 - 失败要回退主模型
                if logger is not None and not _is_cancellation(exc):
                    # 专用命名模型的失败不能被回退掩盖，诊断留在这里。
                    logger.warn(
                        "session naming model failed",
                        event="session_naming_failed",
                        provider=naming_source,
                        model=naming_model,
                        error=summarize(str(exc), MAX_USER_ERROR_RUNES),
                    )
        if primary is None or not primary_model:
            raise RuntimeError("no title model available")
        title = await self._generate(ctx, primary, primary_model, messages)
        return TitleResult(raw_title=title)

    async def _generate(
        self, ctx: Any, client: Any, model: str, messages: list[Any]
    ) -> str:
        req = ChatRequest(
            model=model,
            messages=[
                LLMMessage(
                    role=Role.SYSTEM,
                    segments=text_segments(
                        "你是会话命名助手。请根据对话内容生成一个简短中文标题，只输出标题，不要解释。"
                    ),
                ),
                LLMMessage(role=Role.USER, segments=text_segments(title_prompt(messages))),
            ],
            # Go 侧是 ``MaxTokens: 32``；实测 opencode-go/deepseek-v4.1-flash 在这个
            # 上限下会把 32 个 token 全花在正文之前（``finish_reason=length``、
            # ``delta_content`` 为空），自动命名因此永远失败。标题本身很短，
            # 放宽到 256 不会带来可感知的成本。
            max_tokens=256,
        )
        chunks: list[str] = []
        stream = client.chat_stream(ctx, req)
        if hasattr(stream, "__aiter__"):
            async for chunk in stream:
                if chunk.error is not None:
                    raise chunk.error
                chunks.append(chunk.delta_content or "")
        else:  # pragma: no cover - 同步假实现（测试便利）
            for chunk in stream:
                if chunk.error is not None:
                    raise chunk.error
                chunks.append(chunk.delta_content or "")
        return "".join(chunks)

    # -- 外部注入（对应 Go 的 setPrimary / setNaming / setLogger）-------------

    def set_primary(self, client: Any, model: str) -> None:
        with self._mu:
            self.primary = client
            self.primary_model = model

    def set_naming(self, client: Any, provider: str, model: str) -> None:
        with self._mu:
            self.naming = client
            self.naming_source = provider
            self.naming_model = model

    def set_logger(self, logger: Any) -> None:
        with self._mu:
            self.logger = logger


def title_prompt(messages: list[Any]) -> str:
    """把会话消息渲染成命名提示词。对应 Go ``titlePrompt``。"""
    parts: list[str] = ["请为下面这段会话生成一个不超过20个中文字符的标题。\n\n"]
    for message in messages:
        content = getattr(message, "content", "") or ""
        if not content.strip():
            continue
        role = getattr(message, "role", "")
        if role == RoleUser:
            parts.append("用户：")
        elif role == RoleAssistant:
            parts.append("助手：")
        else:
            continue
        parts.append(content)
        parts.append("\n")
    return "".join(parts)


def _is_cancellation(err: BaseException) -> bool:
    from atri_x.agent.errors import is_cancellation

    return is_cancellation(err)
