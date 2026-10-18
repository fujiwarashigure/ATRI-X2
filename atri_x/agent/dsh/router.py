"""DSH 深度路由：任务特征打分 + 成本闸门 + 熔断。

对应文档：``devdocs/dsh-acp.md`` §6.1（"路由模块：任务特征打分 + 成本闸门 + 熔断
（``λ · cost_extra(score)`` 判定），chat 模式仍走 ATRI-X 原生低成本链路"）。

设计边界：

- **只判断"这一轮值不值得换后端"**，不碰会话、不碰预算账本 —— 预算记账在
  :mod:`atri_x.agent.dsh.route_call` 里走既有的 ``agent.begin_chat_budget`` /
  ``record_chat_usage``，因此 DSH 用量与原生用量共享同一套按用户/群/日的额度。
- **纯函数优先**：:func:`score_task` 只接收文本、返回特征与分数，可离线断言；
  :class:`DshRouter` 只持有阈值、成本参数与熔断状态。
- **不可用时静默降级**：内核缺失 / 命令找不到 / 熔断打开 / 低于阈值，一律返回
  ``use_dsh=False`` 并带上原因，调用方照常走原生链路，不报错、不影响聊天。

打分特征（都是"越像工程任务越高"）：

| 特征 | 权重 | 说明 |
|---|---|---|
| 长度 | 0.15 | 240 字符封顶 |
| 代码痕迹 | 0.20 | 代码围栏、关键字、snake/camelCase 标识符 |
| 路径/文件名 | 0.15 | ``data.json`` / ``src/a/b.py`` / ``D:\\x`` |
| 任务动词 | 0.30 | 实现/重构/调试/测试/脚本/分析… |
| 多步要求 | 0.10 | 然后/接着/分别/最后… |
| 求解型问句 | 0.10 | 为什么/怎么/原理/区别… |

寒暄（你好/在吗/谢谢…）在短句上直接扣分；最终分数夹在 ``[0, 1]``。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from atri_x.agent.dsh.command import resolve_command

__all__ = [
    "DshRouteConfig",
    "DshRouter",
    "RouteDecision",
    "TaskFeatures",
    "clamp01",
    "score_task",
]

#: 默认阈值。配置文件里的值优先（``[agent.dsh].min_score``）。
DEFAULT_MIN_SCORE = 0.40
#: 默认成本权重。
DEFAULT_LAMBDA_PENALTY = 1.0
#: 默认 DSH/原生成本比。
DEFAULT_COST_RATIO = 2.0
#: 默认成本归一化尺度（token）。
DEFAULT_COST_SCALE_TOKENS = 20000
#: 默认单轮超时（秒）。
DEFAULT_TIMEOUT_SECONDS = 180.0
#: 默认历史消息条数与字符预算。
DEFAULT_MAX_HISTORY_MESSAGES = 8
DEFAULT_PROMPT_CHAR_BUDGET = 12000
#: 默认熔断参数（连续失败次数 / 冷却秒数）。
DEFAULT_BREAKER_FAILURES = 3
DEFAULT_BREAKER_COOLDOWN = 300.0

#: 代码痕迹（子串；大小写敏感的部分按原样匹配）。
_CODE_MARKERS = (
    "```",
    "def ",
    "class ",
    "import ",
    "function(",
    "return ",
    "SELECT ",
    "Traceback",
    "Exception",
    "Error:",
    "npm ",
    "pip ",
    "git ",
    "docker ",
    "curl ",
    "});",
    "</",
)
#: 标识符嗅探：snake_case / camelCase（中文文本里出现它们通常意味着贴了代码）。
_IDENTIFIER_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b[a-z]+[A-Z][A-Za-z0-9]*\b")
#: 路径/文件名。
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s，。；、\"']+)"
    r"|(?:(?:\.{0,2}/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)"
    r"|(?:[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|json|toml|ya?ml|md|txt|go|rs|java|c|cc|cpp|h|hpp|sql|ini|log|cfg|conf|env|exe|ps1|sh|bat|html|css))",
    re.IGNORECASE,
)
#: 任务动词（模型要"动手"的信号）。
_TASK_KEYWORDS = (
    "实现",
    "写一个",
    "写个",
    "重构",
    "调试",
    "排查",
    "修复",
    "报错",
    "异常",
    "测试",
    "脚本",
    "算法",
    "优化",
    "分析",
    "设计",
    "架构",
    "部署",
    "编译",
    "翻译",
    "总结",
    "正则",
    "解析",
    "爬虫",
    "接口",
    "sql",
    "文件",
    "代码",
    "函数",
    "模块",
    "项目",
    "配置",
    "api",
)
#: 多步要求。
_STEP_MARKERS = ("然后", "接着", "第一步", "步骤", "分别", "并且", "同时", "最后", "之后")
#: 求解型问句。
_ASK_MARKERS = ("为什么", "怎么", "如何", "原理", "区别", "对比", "解释", "说明")
#: 寒暄/应答（只在短句上扣分）。
_SMALLTALK = (
    "你好",
    "您好",
    "在吗",
    "在么",
    "谢谢",
    "谢啦",
    "哈哈",
    "早上好",
    "中午好",
    "晚上好",
    "晚安",
    "嗯",
    "哦",
    "收到",
)


def clamp01(value: float) -> float:
    """把分数夹到 ``[0, 1]``。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _count(markers: Sequence[str], text: str) -> int:
    return sum(1 for marker in markers if marker and marker in text)


@dataclass
class TaskFeatures:
    """一条用户消息的任务特征（都是计数，便于测试与解释）。"""

    chars: int = 0
    code: int = 0
    identifiers: int = 0
    paths: int = 0
    keywords: int = 0
    steps: int = 0
    asks: int = 0
    smalltalk: int = 0

    def to_dict(self) -> dict[str, int]:
        """给审计/测试用的普通字典。"""
        return {
            "chars": self.chars,
            "code": self.code,
            "identifiers": self.identifiers,
            "paths": self.paths,
            "keywords": self.keywords,
            "steps": self.steps,
            "asks": self.asks,
            "smalltalk": self.smalltalk,
        }


def score_task(text: str) -> tuple[TaskFeatures, float]:
    """给一条用户消息打分；返回 ``(特征, 分数)``。

    纯函数：不读配置、不碰网络、不看会话，因此可以直接对一组样例断言"该路由"/
    "不该路由"。
    """
    body = (text or "").strip()
    features = TaskFeatures(chars=len(body))
    if not body:
        return features, 0.0
    features.code = _count(_CODE_MARKERS, body)
    features.identifiers = len(_IDENTIFIER_RE.findall(body))
    features.paths = len(_PATH_RE.findall(body))
    features.keywords = _count(_TASK_KEYWORDS, body.lower())
    features.steps = _count(_STEP_MARKERS, body)
    features.asks = _count(_ASK_MARKERS, body)
    if len(body) <= 24:
        features.smalltalk = _count(_SMALLTALK, body)
    score = (
        0.15 * min(1.0, features.chars / 240.0)
        + 0.20 * min(1.0, (features.code + features.identifiers) / 2.0)
        + 0.15 * min(1.0, float(features.paths))
        + 0.30 * min(1.0, features.keywords / 2.0)
        + 0.10 * min(1.0, features.steps / 2.0)
        + 0.10 * min(1.0, float(features.asks))
    )
    if features.smalltalk:
        score -= 0.30 * min(1.0, float(features.smalltalk))
    return features, clamp01(score)


@dataclass
class DshRouteConfig:
    """``[agent.dsh]`` 的运行期形态（与配置 dataclass 解耦，便于测试构造）。"""

    enabled: bool = True
    min_score: float = DEFAULT_MIN_SCORE
    lambda_penalty: float = DEFAULT_LAMBDA_PENALTY
    cost_ratio: float = DEFAULT_COST_RATIO
    cost_scale_tokens: int = DEFAULT_COST_SCALE_TOKENS
    command: str = ""
    model: str = ""
    reasoning_effort: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES
    prompt_char_budget: int = DEFAULT_PROMPT_CHAR_BUDGET
    workspace: str = ""
    home: str = ""
    allow_permissions: bool = False
    circuit_breaker_failures: int = DEFAULT_BREAKER_FAILURES
    circuit_breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN

    @classmethod
    def from_section(cls, section: Any) -> "DshRouteConfig":
        """从 ``[agent.dsh]`` 配置段构造；``None`` / 缺字段都退回默认值。"""
        if section is None:
            return cls()

        def pick(name: str, default: Any) -> Any:
            value = getattr(section, name, None)
            if value is None:
                return default
            if isinstance(default, bool):
                return bool(value)
            if isinstance(default, int):
                return int(value)
            if isinstance(default, float):
                return float(value)
            return value

        defaults = cls()
        return cls(
            enabled=pick("enabled", defaults.enabled),
            min_score=pick("min_score", defaults.min_score),
            lambda_penalty=pick("lambda_penalty", defaults.lambda_penalty),
            cost_ratio=pick("cost_ratio", defaults.cost_ratio),
            cost_scale_tokens=pick("cost_scale_tokens", defaults.cost_scale_tokens),
            command=pick("command", defaults.command),
            model=pick("model", defaults.model),
            reasoning_effort=pick("reasoning_effort", defaults.reasoning_effort),
            timeout_seconds=pick("timeout_seconds", defaults.timeout_seconds),
            max_history_messages=pick(
                "max_history_messages", defaults.max_history_messages
            ),
            prompt_char_budget=pick("prompt_char_budget", defaults.prompt_char_budget),
            workspace=pick("workspace", defaults.workspace),
            home=pick("home", defaults.home),
            allow_permissions=pick("allow_permissions", defaults.allow_permissions),
            circuit_breaker_failures=pick(
                "circuit_breaker_failures", defaults.circuit_breaker_failures
            ),
            circuit_breaker_cooldown_seconds=pick(
                "circuit_breaker_cooldown_seconds",
                defaults.circuit_breaker_cooldown_seconds,
            ),
        )

    def cost_pressure(self, estimated_tokens: int) -> float:
        """成本压力 ``[0,1]``：``(成本比-1)/成本比 × min(1, tokens/尺度)``。

        ``cost_ratio=2`` 时上界是 0.5：短消息几乎不惩罚，长上下文才明显。这样
        ``λ · cost_pressure`` 只负责压掉"又贵又大"的调用，不代替阈值判定。
        """
        ratio = max(1.0, float(self.cost_ratio or 1.0))
        extra = (ratio - 1.0) / ratio
        scale = max(1, int(self.cost_scale_tokens or 1))
        size = min(1.0, max(0, int(estimated_tokens)) / float(scale))
        return clamp01(extra * size)


@dataclass
class RouteDecision:
    """一次路由判定的结果（``use_dsh=False`` 时 ``reason`` 说明为什么）。"""

    use_dsh: bool = False
    reason: str = ""
    score: float = 0.0
    effective_score: float = 0.0
    cost_pressure: float = 0.0
    features: TaskFeatures = field(default_factory=TaskFeatures)

    def to_dict(self) -> dict[str, Any]:
        """给审计/测试用的普通字典。"""
        return {
            "useDsh": self.use_dsh,
            "reason": self.reason,
            "score": round(self.score, 4),
            "effectiveScore": round(self.effective_score, 4),
            "costPressure": round(self.cost_pressure, 4),
            "features": self.features.to_dict(),
        }


class DshRouter:
    """持有阈值、成本参数、可用性与熔断状态的进程内路由器。

    生命周期与 Agent 绑定（``agent.services['dsh_route']``）；熔断状态是**进程内**
    的，重启即清零 —— 没有跨重启的持久化语义，避免一次坏配置把路由永久关死。
    """

    def __init__(
        self,
        config: DshRouteConfig | None = None,
        *,
        resolve: Any = resolve_command,
        clock: Any = time.monotonic,
    ) -> None:
        self.config = config or DshRouteConfig()
        self._resolve = resolve
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._notified = False
        self._command: Sequence[str] | str | None = None
        self._available: bool | None = None

    # -- 可用性 ---------------------------------------------------------------
    def available(self, *, refresh: bool = False) -> bool:
        """DSH 是否可用（只解析一次命令；``refresh=True`` 强制重查）。"""
        with self._lock:
            if self._available is not None and not refresh:
                return self._available
            try:
                command = self._resolve(self.config.command)
            except Exception:  # noqa: BLE001 - 解析失败按不可用处理
                command = None
            self._command = command
            self._available = bool(command)
            return self._available

    def command(self) -> Sequence[str] | str | None:
        """上次解析出的启动命令（不可用时 ``None``）。"""
        if self._available is None:
            self.available()
        return self._command

    # -- 熔断 -----------------------------------------------------------------
    def breaker_open(self) -> bool:
        """熔断是否处于打开状态（冷却到期自动放行一次探测）。"""
        with self._lock:
            if not self._open_until:
                return False
            if self._clock() < self._open_until:
                return True
            # 冷却到期：清零并进入半开（允许重试，失败会立即重新打开）。
            self._open_until = 0.0
            self._failures = 0
            return False

    def breaker_state(self) -> dict[str, Any]:
        """熔断状态快照（doctor/测试用）。"""
        with self._lock:
            opened = bool(self._open_until and self._clock() < self._open_until)
            return {
                "failures": self._failures,
                "open": opened,
                "openUntil": round(self._open_until, 3),
            }

    def record_success(self) -> None:
        """成功一次：清零失败计数并复位"已提示"。"""
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._notified = False

    def record_failure(self) -> bool:
        """失败一次；返回是否「刚刚打开熔断」（调用方据此只提示一次）。"""
        with self._lock:
            self._failures += 1
            threshold = max(1, int(self.config.circuit_breaker_failures or 1))
            if self._failures >= threshold and not self._open_until:
                self._open_until = self._clock() + max(
                    1.0, float(self.config.circuit_breaker_cooldown_seconds or 1.0)
                )
                return True
            return False

    def note_notified(self) -> bool:
        """标记「本轮冷却期已经提示过用户」；返回是否是第一次。"""
        with self._lock:
            if self._notified:
                return False
            self._notified = True
            return True

    # -- 判定 -----------------------------------------------------------------
    def decide(self, text: str, *, estimated_tokens: int = 0) -> RouteDecision:
        """判断这一轮是否交给 DSH；**任何**异常/缺件都返回 ``use_dsh=False``。"""
        features, score = score_task(text)
        decision = RouteDecision(features=features, score=score, effective_score=score)
        if not self.config.enabled:
            decision.reason = "disabled"
            return decision
        if self.breaker_open():
            decision.reason = "breaker_open"
            return decision
        if not self.available():
            decision.reason = "unavailable"
            return decision
        threshold = max(0.0, min(1.0, float(self.config.min_score)))
        if score < threshold:
            decision.reason = "below_threshold"
            return decision
        pressure = self.config.cost_pressure(estimated_tokens)
        effective = clamp01(score - float(self.config.lambda_penalty) * pressure)
        decision.cost_pressure = pressure
        decision.effective_score = effective
        if effective < threshold:
            decision.reason = "cost_gate"
            return decision
        decision.use_dsh = True
        decision.reason = "routed"
        return decision

    def describe(self) -> dict[str, Any]:
        """完整状态快照（doctor / 测试）。"""
        return {
            "enabled": self.config.enabled,
            "available": self.available(),
            "command": _command_text(self._command),
            "minScore": self.config.min_score,
            "lambdaPenalty": self.config.lambda_penalty,
            "costRatio": self.config.cost_ratio,
            "model": self.config.model,
            "breaker": self.breaker_state(),
        }


def _command_text(command: Sequence[str] | str | None) -> str:
    if not command:
        return ""
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)
