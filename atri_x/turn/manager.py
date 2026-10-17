"""单个 Session 的 turn 阶段、原始输入、pending 追加与风险确认。

对应 Go：``internal/turn/manager.go``。

一个 Session 同时只有一个 turn。阶段由 ``Phase`` 表示；LLM 阶段可以被
``interrupt_llm`` 打断进入 ``awaiting_append_confirm``（等待用户确认是否把
补充输入合并进本轮），工具阶段可以追加 pending 输入，高风险工具调用会进入
``awaiting_risk_confirm`` 等待用户确认。

约定：

- Go 的 ``time.Duration`` → Python ``float`` 秒。
- Go 的 ``chan`` → ``asyncio.Future``（容量 1 的响应通道）与 ``asyncio.Event``
  （``waitRefresh`` 的"容量 1 非阻塞发送"语义、``appendDone`` 的 close 语义）。
- 等待类方法（``await_append_expiration`` / ``await_risk_confirmation*``）是
  异步的；其余方法是同步的，用 ``threading.Lock`` 保护状态。
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field, replace

from atri_x.llm.llm import (
    MessageSegment,
    append_segment_text,
    segments_text_only,
    text_segments,
)
from atri_x.safecontext import CancelToken

__all__ = [
    "Input",
    "Manager",
    "Part",
    "Phase",
    "PhaseAwaitAppendConfirm",
    "PhaseAwaitRiskConfirm",
    "PhaseCompact",
    "PhaseIdle",
    "PhaseLLM",
    "PhaseTool",
    "RiskConfirmation",
    "RiskConfirmationResponse",
    "Snapshot",
    "Speaker",
    "is_cancel",
    "is_confirm",
    "merge_inputs",
    "tools_string",
]


class Phase:
    """turn 阶段。对应 Go 的 ``turn.Phase`` 常量组。"""

    IDLE = "idle"
    LLM = "llm"
    TOOL = "tool"
    AWAIT_RISK_CONFIRM = "awaiting_risk_confirm"
    AWAIT_APPEND_CONFIRM = "awaiting_append_confirm"
    COMPACT = "compact"


PhaseIdle = Phase.IDLE
PhaseLLM = Phase.LLM
PhaseTool = Phase.TOOL
PhaseAwaitRiskConfirm = Phase.AWAIT_RISK_CONFIRM
PhaseAwaitAppendConfirm = Phase.AWAIT_APPEND_CONFIRM
PhaseCompact = Phase.COMPACT


@dataclass
class RiskConfirmation:
    """一次待确认的高风险工具调用。对应 Go ``turn.RiskConfirmation``。"""

    id: str = ""
    tool_name: str = ""
    arguments: str = ""
    risk: str = ""
    summary: str = ""
    detail: str = ""


@dataclass
class RiskConfirmationResponse:
    """用户对风险确认的回应。对应 Go ``turn.RiskConfirmationResponse``。"""

    confirmed: bool = False
    rejected: bool = False
    stopped: bool = False
    expired: bool = False
    confirm_all: bool = False
    confirm_tool: bool = False
    extra: str = ""
    reason: str = ""


@dataclass
class Speaker:
    """一条入站消息的发言者。

    ``actor_id`` 是用于鉴权与分组的稳定身份；``user_id`` / ``name`` 只作展示。
    """

    actor_id: str = ""
    user_id: str = ""
    name: str = ""
    role: str = ""


@dataclass
class Part:
    """合并输入里的单条原始入站消息。对应 Go ``turn.Part``。"""

    text: str = ""
    platform_text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    speaker: Speaker = field(default_factory=Speaker)


@dataclass
class Input:
    """一次入站输入。对应 Go ``turn.Input``。"""

    text: str = ""
    platform_text: str = ""
    segments: list[MessageSegment] = field(default_factory=list)
    speaker: Speaker = field(default_factory=Speaker)
    parts: list[Part] = field(default_factory=list)


@dataclass
class Snapshot:
    """某一时刻的 turn 视图。对应 Go ``turn.Snapshot``。"""

    session_id: str = ""
    phase: str = PhaseIdle
    pending_count: int = 0
    tools: dict[str, int] = field(default_factory=dict)


class _State:
    __slots__ = (
        "append_done",
        "original_input",
        "pending",
        "phase",
        "risk_confirm",
        "risk_response",
        "tools",
        "wait_refresh",
    )

    def __init__(self, phase: str, original_input: Input | None = None) -> None:
        self.phase = phase
        self.original_input = original_input or Input()
        self.pending: list[Input] = []
        self.risk_confirm: RiskConfirmation | None = None
        self.risk_response: asyncio.Future | None = None
        self.wait_refresh: asyncio.Event | None = None
        self.append_done: asyncio.Event | None = None
        self.tools: dict[str, int] = {}


def _resolve(future: asyncio.Future, value: object) -> None:
    """解析一个等待 Future（可能在其它线程里被调用）。"""
    if future.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is future.get_loop():
        future.set_result(value)
        return

    def _set() -> None:
        if not future.done():
            future.set_result(value)

    try:
        future.get_loop().call_soon_threadsafe(_set)
    except RuntimeError:  # pragma: no cover - 事件循环已关闭
        _set()


def _mirror(loop: asyncio.AbstractEventLoop, source: asyncio.Future) -> asyncio.Future:
    """给一个 Future 做可安全取消的镜像，供 ``asyncio.wait`` 使用。"""
    mirror: asyncio.Future = loop.create_future()
    source.add_done_callback(lambda _future: _resolve(mirror, None))
    return mirror


class Manager:
    """按 Session 维护 turn 状态。对应 Go ``turn.Manager``。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, _State] = {}

    # -- 压缩 ---------------------------------------------------------------

    def start_compact(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._turns:
                return False
            self._turns[session_id] = _State(PhaseCompact)
            return True

    def complete_compact(self, session_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseCompact:
                return False
            self._remove_turn_locked(session_id, turn)
            return True

    # -- LLM 阶段 -----------------------------------------------------------

    def start_llm(self, session_id: str, text: str) -> bool:
        return self.start_llm_input(session_id, text_input(text))

    def start_llm_input(self, session_id: str, input: Input) -> bool:
        with self._lock:
            if session_id in self._turns:
                return False
            self._turns[session_id] = _State(PhaseLLM, normalize_input(input))
            return True

    def interrupt_llm(self, session_id: str, text: str) -> bool:
        return self.interrupt_llm_input(session_id, text_input(text))

    def interrupt_llm_input(self, session_id: str, input: Input) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseLLM:
                return False
            turn.phase = PhaseAwaitAppendConfirm
            turn.wait_refresh = asyncio.Event()
            turn.append_done = asyncio.Event()
            _append_pending(turn, input)
            return True

    def append_pending(self, session_id: str, text: str) -> bool:
        return self.append_pending_input(session_id, text_input(text))

    def append_pending_input(self, session_id: str, input: Input) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase not in (PhaseAwaitAppendConfirm, PhaseTool):
                return False
            if _append_pending(turn, input) and turn.phase == PhaseAwaitAppendConfirm:
                _refresh_wait(turn)
            return True

    def confirm_append(self, session_id: str) -> tuple[str, bool]:
        input, ok = self.confirm_append_input(session_id)
        return input.text, ok

    def confirm_append_input(self, session_id: str) -> tuple[Input, bool]:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseAwaitAppendConfirm:
                return Input(), False
            merged = merge_inputs([turn.original_input, *turn.pending])
            self._remove_turn_locked(session_id, turn)
            return merged, True

    def cancel_append(self, session_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseAwaitAppendConfirm:
                return False
            self._remove_turn_locked(session_id, turn)
            return True

    async def await_append_expiration(self, session_id: str, timeout: float) -> bool:
        """等补充确认超时（每次追加都刷新计时）。超时后移除 turn 并返回 True。"""
        if timeout is None or timeout <= 0:
            return False
        with self._lock:
            turn = self._turns.get(session_id)
            if (
                turn is None
                or turn.phase != PhaseAwaitAppendConfirm
                or turn.append_done is None
            ):
                return False
            refresh = turn.wait_refresh
            done = turn.append_done

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if done.is_set():
                return False
            if refresh is not None and refresh.is_set():
                refresh.clear()
                deadline = loop.time() + timeout
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            waiters = [asyncio.ensure_future(done.wait())]
            if refresh is not None:
                waiters.append(asyncio.ensure_future(refresh.wait()))
            try:
                await asyncio.wait(
                    waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()

        with self._lock:
            current = self._turns.get(session_id)
            if current is turn and current.phase == PhaseAwaitAppendConfirm:
                self._remove_turn_locked(session_id, current)
                return True
            return False

    # -- 风险确认 -----------------------------------------------------------

    async def await_risk_confirmation(
        self, session_id: str, confirmation: RiskConfirmation
    ) -> tuple[RiskConfirmationResponse, bool]:
        return await self.await_risk_confirmation_context(
            None, session_id, confirmation, 0.0
        )

    async def await_risk_confirmation_context(
        self,
        ctx: CancelToken | None,
        session_id: str,
        confirmation: RiskConfirmation,
        timeout: float = 0.0,
    ) -> tuple[RiskConfirmationResponse, bool]:
        response: asyncio.Future = asyncio.get_running_loop().create_future()
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseTool:
                return RiskConfirmationResponse(stopped=True), False
            turn.phase = PhaseAwaitRiskConfirm
            turn.risk_confirm = confirmation
            turn.risk_response = response
            turn.wait_refresh = asyncio.Event()
            refresh = turn.wait_refresh

        loop = asyncio.get_running_loop()
        deadline = None if not timeout or timeout <= 0 else loop.time() + timeout
        cancel_future: asyncio.Future = loop.create_future()
        remove_callback = None
        if ctx is not None:
            remove_callback = ctx.add_callback(
                lambda: _resolve(cancel_future, "cancelled")
            )

        try:
            while True:
                if response.done():
                    result = response.result()
                    with self._lock:
                        current = self._turns.get(session_id)
                        if current is not turn:
                            return result, False
                        turn.risk_confirm = None
                        turn.risk_response = None
                        turn.wait_refresh = None
                        if result.stopped:
                            self._remove_turn_locked(session_id, turn)
                            return result, False
                        turn.phase = PhaseTool
                        return result, True
                if refresh.is_set():
                    refresh.clear()
                    if deadline is not None:
                        deadline = loop.time() + timeout
                if cancel_future.done():
                    with self._lock:
                        current = self._turns.get(session_id)
                        if current is turn and current.phase == PhaseAwaitRiskConfirm:
                            self._remove_turn_locked(session_id, current)
                    return RiskConfirmationResponse(stopped=True), False
                remaining = None
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        expired = RiskConfirmationResponse(stopped=True, expired=True)
                        with self._lock:
                            current = self._turns.get(session_id)
                            if (
                                current is turn
                                and current.phase == PhaseAwaitRiskConfirm
                            ):
                                self._remove_turn_locked(session_id, current)
                                return expired, False
                        return RiskConfirmationResponse(stopped=True), False
                waiters = [
                    _mirror(loop, response),
                    asyncio.ensure_future(refresh.wait()),
                    _mirror(loop, cancel_future),
                ]
                try:
                    await asyncio.wait(
                        waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()
        finally:
            if remove_callback is not None:
                remove_callback()
            if not cancel_future.done():
                cancel_future.cancel()

    def resolve_risk_confirmation(
        self, session_id: str, resp: RiskConfirmationResponse
    ) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if (
                turn is None
                or turn.phase != PhaseAwaitRiskConfirm
                or turn.risk_response is None
            ):
                return False
            _resolve(turn.risk_response, resp)
            return True

    def refresh_risk_confirmation(self, session_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if (
                turn is None
                or turn.phase != PhaseAwaitRiskConfirm
                or turn.risk_response is None
            ):
                return False
            _refresh_wait(turn)
            return True

    def pending_risk_confirmation(
        self, session_id: str
    ) -> tuple[RiskConfirmation, bool]:
        with self._lock:
            turn = self._turns.get(session_id)
            if (
                turn is None
                or turn.phase != PhaseAwaitRiskConfirm
                or turn.risk_confirm is None
            ):
                return RiskConfirmation(), False
            return turn.risk_confirm, True

    # -- 工具阶段 -----------------------------------------------------------

    def start_tool_phase(self, session_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase != PhaseLLM:
                return False
            turn.phase = PhaseTool
            return True

    def complete_llm(self, session_id: str) -> tuple[str, bool]:
        input, ok = self.complete_llm_input(session_id)
        return input.text, ok

    def complete_llm_input(self, session_id: str) -> tuple[Input, bool]:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase not in (
                PhaseLLM,
                PhaseTool,
                PhaseAwaitRiskConfirm,
            ):
                return Input(), False
            pending = merge_inputs(turn.pending)
            self._remove_turn_locked(session_id, turn)
            return pending, True

    def finish_request(self, session_id: str) -> None:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.phase == PhaseAwaitAppendConfirm:
                return
            self._remove_turn_locked(session_id, turn)

    def stop_session(self, session_id: str) -> None:
        with self._lock:
            self._remove_turn_locked(session_id, self._turns.get(session_id))

    def stop_all(self) -> None:
        with self._lock:
            for session_id, turn in list(self._turns.items()):
                self._remove_turn_locked(session_id, turn)

    def drain_merged(self, session_id: str) -> str:
        return self.drain_merged_input(session_id).text

    def drain_merged_input(self, session_id: str) -> Input:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or not turn.pending:
                return Input()
            merged = merge_inputs(turn.pending)
            turn.pending = []
            return merged

    def add_tool_use(self, session_id: str, name: str) -> None:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                turn = _State(PhaseTool)
                self._turns[session_id] = turn
            turn.tools[name] = turn.tools.get(name, 0) + 1

    # -- 快照 ---------------------------------------------------------------

    def snapshot(self, session_id: str) -> Snapshot:
        with self._lock:
            turn = self._turns.get(session_id)
            if turn is None:
                return Snapshot(session_id=session_id, phase=PhaseIdle)
            return _snapshot(session_id, turn)

    def snapshot_all(self) -> list[Snapshot]:
        with self._lock:
            out = [_snapshot(session_id, turn) for session_id, turn in self._turns.items()]
        out.sort(key=lambda item: item.session_id)
        return out

    # -- 内部 ---------------------------------------------------------------

    def _remove_turn_locked(self, session_id: str, turn: _State | None) -> None:
        if turn is None:
            self._turns.pop(session_id, None)
            return
        _stop_turn(turn)
        if turn.append_done is not None:
            turn.append_done.set()
            turn.append_done = None
        self._turns.pop(session_id, None)


def _append_pending(turn: _State, input: Input) -> bool:
    input = normalize_input(input)
    if input.text == "" and not input.segments:
        return False
    turn.pending.append(input)
    return True


def _refresh_wait(turn: _State | None) -> None:
    if turn is None or turn.wait_refresh is None:
        return
    turn.wait_refresh.set()


def _stop_turn(turn: _State | None) -> None:
    if turn is None or turn.phase != PhaseAwaitRiskConfirm or turn.risk_response is None:
        return
    _resolve(turn.risk_response, RiskConfirmationResponse(stopped=True))


def _snapshot(session_id: str, turn: _State) -> Snapshot:
    return Snapshot(
        session_id=session_id,
        phase=turn.phase,
        pending_count=len(turn.pending),
        tools=dict(turn.tools),
    )


def is_confirm(text: str) -> bool:
    """``$`` / ``是`` / ``y`` / ``yes`` 视为确认。"""
    return (text or "").strip().lower() in ("$", "是", "y", "yes")


def is_cancel(text: str) -> bool:
    """``取消`` / ``否`` / ``n`` / ``no`` 视为取消。"""
    return (text or "").strip().lower() in ("取消", "否", "n", "no")


def tools_string(tools: dict[str, int] | None) -> str:
    """把工具计数渲染成 ``name xN`` 列表（按名字排序）。"""
    if not tools:
        return ""
    return ", ".join(f"{name} x{tools[name]}" for name in sorted(tools))


def merge_inputs(inputs: list[Input]) -> Input:
    """合并一批入站输入，保留每条原始消息的发言者归属。

    Agent 侧的合并窗口直接调用它（对应 Go ``turn.MergeInputs``）。
    """
    clean = [normalize_input(item) for item in (inputs or [])]
    clean = [item for item in clean if item.text != "" or item.segments]
    if not clean:
        return Input()
    if len(clean) == 1:
        return clean[0]
    segments = text_segments("补充信息：")
    platform_texts: list[str] = []
    for index, item in enumerate(clean):
        segments = append_segment_text(segments, f"\n{index + 1}. ")
        segments = [*segments, *item.segments]
        platform_texts.append(item.platform_text)
    parts: list[Part] = []
    speaker = clean[0].speaker
    same_speaker = True
    for item in clean:
        if item.parts:
            parts.extend(replace(part) for part in item.parts)
        else:
            parts.append(
                Part(
                    text=item.text,
                    platform_text=item.platform_text,
                    segments=list(item.segments),
                    speaker=item.speaker,
                )
            )
        if item.speaker != speaker:
            same_speaker = False
    if not same_speaker:
        speaker = Speaker()
    return Input(
        text=segments_text_only(segments),
        platform_text=merge_text_inputs(platform_texts),
        segments=segments,
        speaker=speaker,
        parts=parts,
    )


def text_input(text: str) -> Input:
    text = (text or "").strip()
    return Input(text=text, platform_text=text, segments=text_segments(text))


def normalize_input(input: Input) -> Input:
    text = (input.text or "").strip()
    platform_text = (input.platform_text or "").strip()
    segments = [segment.clone() for segment in input.segments]
    if not segments:
        segments = text_segments(text)
    if text == "":
        text = segments_text_only(segments)
    if input.parts:
        parts = [
            replace(part, segments=[segment.clone() for segment in part.segments])
            for part in input.parts
        ]
    else:
        parts = []
    return Input(
        text=text,
        platform_text=platform_text,
        segments=segments,
        speaker=input.speaker,
        parts=parts,
    )


def merge_text_inputs(inputs: list[str]) -> str:
    clean = [(item or "").strip() for item in inputs]
    clean = [item for item in clean if item != ""]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    out = ["补充信息："]
    for index, item in enumerate(clean):
        out.append(f"\n{index + 1}. {item}")
    return "".join(out)
