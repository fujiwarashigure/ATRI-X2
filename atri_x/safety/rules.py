"""规则与词表的唯一出处：内置词表 + 用户词表 + 敏感信息正则。

1.0.3 起本模块是"内置词表 + 十类敏感信息正则"；1.1.8 起加三件事：

1. **用户可编辑**：``<config_dir>/safety/wordlists/<类别>.txt`` 与内置同名文件**合并**
   （用户在前的优先），新文件名自动成为一个自定义类别（默认动作 ``block``）；
   ``<config_dir>/safety/sensitive/*.regex`` 一行一条自定义正则（``名字 = /正则/flags``）。
2. **归一化匹配**：词表匹配跑在 :func:`atri_x.safety.normalize.normalize_skeleton` 折出
   的骨架上（NFKC / 去零宽 / 去 CJK 间分隔符 / 大小写折叠），敏感信息仍在**原文**上
   跑正则（替换必须落在原文，才不会错位）。
3. **分块扫描**：``max_scan_chars`` 从"只扫前 N 字符"改成"扫前 N×4 字符、按 N 分块"，
   超长工具结果不再只检查开头一小段。

两条硬性约束（1.0.2 沿袭下来的）：

1. **幂等**：``inspect(inspect(text))`` 的文本必须与一次相同。所有替换文本里都不含会被
   自身正则再次命中的形态（无数字、无 ``@``、无 ``BEGIN``），``password`` 规则带
   ``(?!\\[已脱敏)`` 先行断言。
2. **不落原文**：命中片段只用于内存判定；审计预览走 :meth:`RuleSet.mask`，把**所有**
   敏感命中（含只 ``flag`` 的类别）与词表命中都替换掉。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from atri_x.safety.normalize import normalize_skeleton

__all__ = [
    "CATEGORY_IDS",
    "CATEGORY_LABELS",
    "CATEGORY_SEVERITIES",
    "RuleHit",
    "RuleSet",
    "SENSITIVE_DEFAULT_ACTIONS",
    "SENSITIVE_IDS",
    "SENSITIVE_LABELS",
    "SENSITIVE_REPLACEMENTS",
    "SENSITIVE_SEVERITIES",
    "WORDLIST_DIR",
    "available_wordlists",
    "load_all_wordlists",
    "load_custom_patterns",
    "load_user_wordlists",
    "load_wordlist",
    "wordlist_path",
]

#: 词表目录（包内数据文件）。
WORDLIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wordlists")

#: 八个内置词表类别，顺序固定（配置里的 ``categories`` 也按这个顺序归一）。
CATEGORY_IDS: tuple[str, ...] = (
    "hate_speech",
    "illegal_method",
    "minor_sexual",
    "privacy_violation",
    "self_harm",
    "sexual_explicit",
    "terror_extremism",
    "violence_gore",
)

CATEGORY_LABELS: dict[str, str] = {
    "hate_speech": "仇恨与歧视",
    "illegal_method": "违法犯罪方法",
    "minor_sexual": "涉及未成年人的性内容",
    "privacy_violation": "人肉开盒与隐私侵害",
    "self_harm": "自伤自杀",
    "sexual_explicit": "露骨色情",
    "terror_extremism": "恐怖主义与极端主义",
    "violence_gore": "暴力血腥与虐待",
}

CATEGORY_SEVERITIES: dict[str, str] = {
    "hate_speech": "high",
    "illegal_method": "critical",
    "minor_sexual": "critical",
    "privacy_violation": "high",
    "self_harm": "high",
    "sexual_explicit": "high",
    "terror_extremism": "critical",
    "violence_gore": "high",
}

#: 词表类别命中后的默认动作：一律整体拦截（由调用方决定替换文案）。
CATEGORY_ACTION_BLOCK = "block"

#: 敏感信息类别，顺序固定：**先结构化凭据、后 PII**（前缀规则不会被后面的规则截胡）。
SENSITIVE_IDS: tuple[str, ...] = (
    "private_key",
    "api_key",
    "jwt",
    "password",
    "id_card",
    "bank_card",
    "phone",
    "email",
    "internal_ip",
    "local_path",
)

SENSITIVE_LABELS: dict[str, str] = {
    "private_key": "PEM 私钥块",
    "api_key": "API Key / 云凭据",
    "jwt": "JWT 令牌",
    "password": "密码 / 密钥赋值",
    "id_card": "身份证号",
    "bank_card": "银行卡号",
    "phone": "手机号",
    "email": "邮箱地址",
    "internal_ip": "内网地址",
    "local_path": "本机绝对路径",
}

#: 默认动作：凭据类 ``redact``，PII 类 ``flag``（只记审计，避免工程/客服语料误杀）。
SENSITIVE_DEFAULT_ACTIONS: dict[str, str] = {
    "private_key": "redact",
    "api_key": "redact",
    "jwt": "redact",
    "password": "redact",
    "id_card": "flag",
    "bank_card": "flag",
    "phone": "flag",
    "email": "flag",
    "internal_ip": "flag",
    "local_path": "flag",
}

SENSITIVE_SEVERITIES: dict[str, str] = {
    "private_key": "critical",
    "api_key": "critical",
    "jwt": "high",
    "password": "critical",
    "id_card": "critical",
    "bank_card": "high",
    "phone": "medium",
    "email": "low",
    "internal_ip": "low",
    "local_path": "low",
}

#: 替换文本。**必须**不含会被同一类别（或任何类别）再次命中的形态。
SENSITIVE_REPLACEMENTS: dict[str, str] = {
    "private_key": "[已脱敏:私钥]",
    "api_key": "[已脱敏:API Key]",
    "jwt": "[已脱敏:JWT]",
    "password": "[已脱敏]",
    "id_card": "[已脱敏:身份证]",
    "bank_card": "[已脱敏:卡号]",
    "phone": "[已脱敏:手机号]",
    "email": "[已脱敏:邮箱]",
    "internal_ip": "[已脱敏:内网IP]",
    "local_path": "[已脱敏:本机路径]",
}

#: 敏感信息的匹配式。独立实现，不依赖 :mod:`atri_x.redact`。
_SENSITIVE_PATTERNS: dict[str, str] = {
    # 多行 PEM 块：必须最先处理，否则后续规则只遮盖其中一行。
    "private_key": (
        r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
        r"-----END[A-Z0-9 ]*PRIVATE KEY-----"
    ),
    "api_key": (
        r"(?i)(?:sk-[A-Za-z0-9_-]{16,}"
        r"|oc_sk_[A-Za-z0-9_-]{8,}"
        r"|ghp_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}"
        r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
        # Bearer 令牌归到同一类别（不新增类别，免得与 [safety].sensitive 的类别表漂移）
        r"|Bearer\s+[A-Za-z0-9._~+/=-]{20,})"
    ),
    "jwt": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    # 赋值形态。``(?!\[已脱敏)`` 是幂等兜底：二次扫描时不再啃掉文本尾巴。
    "password": (
        r"(?i)((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token"
        r"|refresh[_-]?token|auth[_-]?token|password|passwd|secret))[\"']?\s*[=:]\s*"
        r"[\"']?(?!\[已脱敏)[^\s\"'，,;]{8,}"
    ),
    "id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "bank_card": r"(?<!\d)\d{16,19}(?!\d)",
    "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "internal_ip": (
        r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
        r"\.\d{1,3}\.\d{1,3}(?![\d.])"
    ),
    "local_path": r"(?i)(?<![\w])[A-Z]:\\[^\s\"'<>|]{3,}",
}

#: ``password`` 规则有捕获组 ``\1``：替换时保留字段名，只吃掉取值。
_SENSITIVE_TEMPLATES: dict[str, str] = {"password": r"\1=[已脱敏]"}

#: 扫描窗口倍数：``max_scan_chars`` 的多少倍参与扫描（分块，见 :meth:`RuleSet.split_scan`）。
SCAN_BUDGET_FACTOR = 4

#: 分块下限（太小会让正则调用开销占比过高）。
MIN_CHUNK_CHARS = 256


@dataclass(frozen=True)
class RuleHit:
    """一处命中（只存在于内存，**不要**把它写进日志或审计）。"""

    category: str
    label: str
    action: str
    severity: str = ""
    match: str = ""


# ---------------------------------------------------------------------------
# 词表装载
# ---------------------------------------------------------------------------
def wordlist_path(name: str) -> str:
    """某个内置类别词表的绝对路径（不保证存在）。"""
    return os.path.join(WORDLIST_DIR, f"{(name or '').strip().lower()}.txt")


def _read_terms(path: str) -> list[str]:
    """读一个词表文件：一行一条、``#`` 注释、空行忽略、去重保序；读不到返回空表。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    out: list[str] = []
    for line in lines:
        term = line.strip()
        if not term or term.startswith("#") or term in out:
            continue
        out.append(term)
    return out


def load_wordlist(name: str) -> list[str]:
    """读一个内置词表文件（缺失/不可读时返回空列表，词表缺一份不该让链路失败）。"""
    return _read_terms(wordlist_path(name))


def available_wordlists() -> tuple[str, ...]:
    """包内实际存在的词表类别（按 :data:`CATEGORY_IDS` 的顺序）。"""
    return tuple(name for name in CATEGORY_IDS if os.path.isfile(wordlist_path(name)))


def load_all_wordlists() -> dict[str, list[str]]:
    """一次读全部内置词表。"""
    return {name: load_wordlist(name) for name in CATEGORY_IDS}


def load_user_wordlists(directory: str) -> dict[str, list[str]]:
    """读用户词表目录：``<类别>.txt``，一行一条。

    目录不存在/不可读 → 空字典（用户不建这个目录就等于"不覆盖"）。
    ``#`` 注释与空行忽略；文件名（去 ``.txt``）就是类别名；新类别自动生效。
    """
    base = (directory or "").strip()
    if not base or not os.path.isdir(base):
        return {}
    out: dict[str, list[str]] = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return {}
    for filename in names:
        if not filename.lower().endswith(".txt"):
            continue
        category = filename[: -len(".txt")].strip().lower()
        if not category:
            continue
        terms = _read_terms(os.path.join(base, filename))
        if terms:
            out[category] = terms
    return out


#: 自定义正则行：``名字 = /正则/flags``（名字允许中文等非空白字符）。
_CUSTOM_PATTERN_RE = re.compile(
    r"^\s*(?P<name>[^\s=/]{1,40})\s*=\s*/(?P<body>.+)/(?P<flags>[a-z]*)\s*$"
)

_FLAG_BITS: dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
}


def load_custom_patterns(directory: str) -> tuple[tuple[str, str, str], ...]:
    """读用户自定义敏感信息正则目录（``*.regex``）。

    返回 ``((名字, 正则源码, 替换文本), ...)``；非法行/非法正则**整条跳过**（不出现在返回
    里，调用方据此告警）。替换文本固定为 ``[已脱敏:<名字>]``（不含可再次命中的形态）。
    """
    base = (directory or "").strip()
    if not base or not os.path.isdir(base):
        return ()
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ()
    for filename in names:
        if not filename.lower().endswith(".regex"):
            continue
        path = os.path.join(base, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            matched = _CUSTOM_PATTERN_RE.match(text)
            if not matched:
                continue
            name = matched.group("name").strip().lower()
            if not name or name in seen:
                continue
            body = matched.group("body")
            flags = 0
            for char in matched.group("flags"):
                flags |= _FLAG_BITS.get(char, 0)
            try:
                re.compile(body, flags)
            except re.error:
                continue
            seen.add(name)
            out.append((name, body, f"[已脱敏:{name}]"))
    return tuple(out)


# ---------------------------------------------------------------------------
# 编译后的规则集
# ---------------------------------------------------------------------------
def _escape_terms(terms: Iterable[str]) -> str:
    """把词表编成"任一词命中"的交替式（逐词转义，避免词表里的符号变成正则）。"""
    return "|".join(re.escape(term) for term in terms if term)


def _compile(pattern: str, flags: int = 0) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


@dataclass(frozen=True)
class _CategoryRule:
    id: str
    label: str
    severity: str
    action: str
    terms: tuple[str, ...]
    regex: re.Pattern[str] | None


@dataclass(frozen=True)
class _SensitiveRule:
    id: str
    label: str
    severity: str
    replacement: str
    template: str
    regex: re.Pattern[str] | None


class RuleSet:
    """按一份策略编译好的规则集。构造一次，之后只读。

    ``policy`` 是鸭子类型：只用到 ``enabled_categories()`` / ``enabled_sensitive()``
    / ``action_for()`` / ``allowlist`` / ``max_scan_chars`` 这几处。

    1.1.8 新增的可选参数（默认值保持 1.1.7 行为）：

    * ``extra_terms`` —— 用户词表 ``{类别: [词条, ...]}``，与内置合并（用户在前）；
    * ``custom_patterns`` —— 用户自定义正则 ``((名字, 源码, 替换), ...)``；
    * ``normalize`` —— 词表匹配是否跑归一化骨架（由 ``[local].normalize`` 控制）。
    """

    def __init__(
        self,
        policy: Any,
        *,
        extra_terms: Mapping[str, Sequence[str]] | None = None,
        custom_patterns: Sequence[tuple[str, str, str]] = (),
        normalize: bool = True,
    ) -> None:
        self.allowlist: tuple[str, ...] = tuple(getattr(policy, "allowlist", ()) or ())
        self.max_scan_chars = int(getattr(policy, "max_scan_chars", 8000) or 8000)
        self.normalize = bool(normalize)
        self.warnings: list[str] = []
        self._actions: dict[str, str] = {}
        self.categories: list[_CategoryRule] = []
        builtin = dict(load_all_wordlists())
        user = {str(k).strip().lower(): list(v) for k, v in (extra_terms or {}).items()}
        enabled = list(policy.enabled_categories())
        for name in user:
            if name not in enabled:
                enabled.append(name)
        for name in enabled:
            terms: list[str] = []
            for term in list(user.get(name, ())) + list(builtin.get(name, ())):
                cleaned = str(term).strip()
                if cleaned and cleaned not in terms:
                    terms.append(cleaned)
            if not terms:
                self.warnings.append(f"词表为空或缺失，已跳过：{name}")
                continue
            normalized = self._normalize_terms(terms)
            regex = _compile(_escape_terms(normalized))
            if regex is None:
                self.warnings.append(f"词表无法编译，已跳过：{name}")
                continue
            self.categories.append(
                _CategoryRule(
                    id=name,
                    label=CATEGORY_LABELS.get(name, name),
                    severity=CATEGORY_SEVERITIES.get(name, "high"),
                    action=CATEGORY_ACTION_BLOCK,
                    terms=tuple(terms),
                    regex=regex,
                )
            )
        self.sensitive: list[_SensitiveRule] = []
        for name in policy.enabled_sensitive():
            pattern = _SENSITIVE_PATTERNS.get(name, "")
            regex = _compile(pattern) if pattern else None
            if regex is None:
                self.warnings.append(f"敏感信息规则缺失或非法，已跳过：{name}")
                continue
            action_for = getattr(policy, "action_for", None)
            action = action_for(name) if callable(action_for) else ""
            self._actions[name] = action if action in ("flag", "redact", "block") else (
                SENSITIVE_DEFAULT_ACTIONS.get(name, "redact")
            )
            self.sensitive.append(
                _SensitiveRule(
                    id=name,
                    label=SENSITIVE_LABELS.get(name, name),
                    severity=SENSITIVE_SEVERITIES.get(name, "medium"),
                    replacement=SENSITIVE_REPLACEMENTS.get(name, "[已脱敏]"),
                    template=_SENSITIVE_TEMPLATES.get(name, ""),
                    regex=regex,
                )
            )
        for name, source, replacement in custom_patterns:
            regex = _compile(source)
            if regex is None:
                self.warnings.append(f"自定义正则无法编译，已跳过：{name}")
                continue
            self._actions[name] = "redact"
            self.sensitive.append(
                _SensitiveRule(
                    id=name,
                    label=name,
                    severity="medium",
                    replacement=replacement,
                    template="",
                    regex=regex,
                )
            )

    # -- 内部 ---------------------------------------------------------------
    def _normalize_terms(self, terms: Iterable[str]) -> list[str]:
        if not self.normalize:
            return [term for term in terms if term]
        out: list[str] = []
        for term in terms:
            skeleton = normalize_skeleton(term)
            if skeleton and skeleton not in out:
                out.append(skeleton)
        return out

    # -- 匹配 ---------------------------------------------------------------
    def allowlisted(self, value: str) -> bool:
        """命中豁免：命中片段**包含**白名单里的某一条就放过这一处。"""
        return any(entry and entry in value for entry in self.allowlist)

    def inspect(self, text: str) -> tuple[str, list[RuleHit]]:
        """返回 ``(处理后的文本, 命中列表)``；是否采用由调用方按 mode 决定。

        ``处理后的文本`` 已应用 ``redact`` 类替换；``block`` 不替换正文
        （由调用方给整段替换文案）。
        """
        original = text or ""
        if not original:
            return original, []
        head, tail = self.split_scan(original)
        out = head
        hits: list[RuleHit] = []
        # 1) 词表类：命中即 block（不替换正文，留给调用方的统一文案）。
        skeleton = normalize_skeleton(out) if self.normalize else out
        for rule in self.categories:
            if rule.regex is None:
                continue
            matched = [
                m.group(0)
                for m in rule.regex.finditer(skeleton)
                if not self.allowlisted(m.group(0))
            ]
            if not matched:
                continue
            hits.append(
                RuleHit(
                    category=rule.id,
                    label=rule.label,
                    action=rule.action,
                    severity=rule.severity,
                    match=matched[0],
                )
            )
        # 2) 敏感信息：按类别动作 redact / flag / block（在**原文**上跑，替换位置才准）。
        for rule in self.sensitive:
            if rule.regex is None:
                continue
            effective = self._action_for(rule)
            found = [
                m.group(0) for m in rule.regex.finditer(out) if not self.allowlisted(m.group(0))
            ]
            if not found:
                continue
            hits.append(
                RuleHit(
                    category=rule.id,
                    label=rule.label,
                    action=effective,
                    severity=rule.severity,
                    match=found[0],
                )
            )
            if effective == "redact":
                out = self._redact(rule, out)
        return out + tail, hits

    def mask(self, text: str) -> str:
        """把所有命中（含只 ``flag`` 的类别）都替换掉，供审计预览使用。"""
        original = text or ""
        if not original:
            return original
        out = original
        for rule in self.categories:
            if rule.regex is not None:
                out = rule.regex.sub("[已过滤]", out)
        for rule in self.sensitive:
            if rule.regex is None:
                continue
            replacement = rule.template or rule.replacement
            out = rule.regex.sub(replacement, out)
        return out

    def split_scan(self, text: str) -> tuple[str, str]:
        """按扫描预算切出"要扫描的头"与"原样保留的尾"。

        1.1.8 起预算从 ``max_scan_chars`` 放宽到 ``max_scan_chars × 4``
        （``SCAN_BUDGET_FACTOR``）：旧实现只扫前 8000 字符，实测 2 万字符的工具结果
        有 6 成完全没检查。截断只影响**是否扫描**，不影响文本：尾部原样拼回，绝不丢字符。
        """
        limit = self.max_scan_chars * SCAN_BUDGET_FACTOR
        if limit <= 0 or len(text) <= limit:
            return text, ""
        return text[:limit], text[limit:]

    def scan_chunks(self, text: str) -> list[str]:
        """把"要扫描的头"按 ``max_scan_chars`` 切成多块（供排障与统计用）。"""
        head, _tail = self.split_scan(text)
        size = max(MIN_CHUNK_CHARS, self.max_scan_chars)
        if len(head) <= size:
            return [head] if head else []
        return [head[i : i + size] for i in range(0, len(head), size)]

    # -- 动作 ---------------------------------------------------------------
    def _action_for(self, rule: _SensitiveRule) -> str:
        return self._actions.get(rule.id, SENSITIVE_DEFAULT_ACTIONS.get(rule.id, "redact"))

    def action_for(self, category: str) -> str:
        """某个敏感类别的生效动作（供引擎裁决层查询）。"""
        name = (category or "").strip().lower()
        return self._actions.get(name, SENSITIVE_DEFAULT_ACTIONS.get(name, "redact"))

    def _redact(self, rule: _SensitiveRule, text: str) -> str:
        def _sub(match: re.Match[str]) -> str:
            if self.allowlisted(match.group(0)):
                return match.group(0)
            return match.expand(rule.template) if rule.template else rule.replacement

        return rule.regex.sub(_sub, text)
