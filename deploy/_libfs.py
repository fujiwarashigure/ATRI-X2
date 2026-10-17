"""文件遍历、哈希、清单校验、tar 安全解包与凭据脱敏。

对应上游：各 shell 脚本里的 ``find ... -print0``、``sha256sum -c``、
``tar -xzf``，以及 ``atri-x-watchdog.sh`` 里那一大段 ``REDACT_SED`` /
``redact_diagnostics`` / ``verify_redaction``。

从 ``_lib.py`` 拆出来（原文件 900+ 行，超过 PORTING.md §2 的 600 行上限）。

两个与上游不同的关键点，都是**有意加固**：

1. ``safe_extract_tar`` 自己做成员校验。GNU ``tar`` 会拒绝 ``..``，但 Python
   的 ``tarfile.extractall`` 默认更宽松；备份包来自生产环境，不能无条件信任。
2. 脱敏规则复用 :func:`atri_x.redact.secrets`（``PORTING.md`` §3 的唯一出处），
   保证「watchdog 脱敏」与「应用自己写日志时的脱敏」是同一套规则；只在它
   漏掉「引号内取值」那一种形态时补一条引号修复，不另立一套规则。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tarfile
from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = [
    "ArchivedPathError",
    "REDACTED",
    "REAL_REDACT",
    "SafeExtractError",
    "collect_files",
    "detect_credential_patterns",
    "detect_known_secrets",
    "extract_tar",
    "file_sha256",
    "iter_files",
    "known_secrets_from_env",
    "real_redact",
    "redact_dir",
    "safe_extract_tar",
    "sensitive_env_name",
    "verify_manifest",
]


# ============================================================
# 文件遍历与哈希
# ============================================================
def iter_files(root: str | os.PathLike[str]) -> Iterable[Path]:
    """递归产出 ``root`` 下的普通文件（跳过符号链接与目录）。

    对应 bash 里的 ``find <dir> -type f``。顺序稳定（目录与前缀均排序），
    这样清单文件与测试断言可复现。
    """
    base = Path(root)
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def collect_files(
    root: str | os.PathLike[str],
    patterns: Sequence[str],
) -> list[Path]:
    """按 ``fnmatch`` 模式收集文件（对应 ``find -name '*.db' -o -name '*.sqlite'``）。"""
    wanted = list(patterns)
    if not wanted:
        return []
    return [
        path
        for path in iter_files(root)
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in wanted)
    ]


def file_sha256(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    """流式算 sha256（备份包可能上 GB，不能整份读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_manifest(
    manifest: str | os.PathLike[str],
    base_dir: str | os.PathLike[str],
) -> tuple[bool, list[str]]:
    """校验 ``sha256sum`` 风格清单（对应 ``sha256sum -c``）。

    返回 ``(全部通过?, 失败说明列表)``。行格式 ``<64 位十六进制>  <相对路径>``；
    ``sha256sum`` 二进制模式会在路径前加 ``*``，这里与多余空白一并宽容处理。

    任何一行读不懂、文件缺失、哈希不符都记入失败列表——**不静默跳过**，
    因为“跳过校验”在恢复流程里等于“假装备份是好的”。
    """
    from _envutil import read_text

    text = read_text(manifest)
    if not text.strip():
        return False, ["清单为空或不存在"]
    base = Path(base_dir)
    failures: list[str] = []
    checked = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            failures.append(f"第 {lineno} 行格式非法：{line[:80]}")
            continue
        expected = parts[0].lower()
        rel = parts[1].strip().lstrip("*")
        target = base / rel
        if not target.is_file():
            failures.append(f"清单文件缺失：{rel}")
            continue
        actual = file_sha256(target)
        checked += 1
        if actual != expected:
            failures.append(f"校验失败：{rel}（期望 {expected[:12]}… 实际 {actual[:12]}…）")
    if checked == 0 and not failures:
        return False, ["清单里没有任何可校验的文件"]
    return not failures, failures


# ============================================================
# tar 解包（防目录穿越）
# ============================================================
class ArchivedPathError(Exception):
    """归档里出现绝对路径、``..`` 或指向目录外的链接 —— 拒绝解包。"""


class SafeExtractError(Exception):
    """解包失败（归档损坏、格式不对、I/O 错误）。"""


def safe_extract_tar(
    archive: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    *,
    mode: str = "r:gz",
) -> None:
    """解包 tar 并拒绝逃出 ``dest`` 的成员。

    对应上游的 ``tar -xzf``。两个成员都要校验：路径成员（绝对路径 / ``..``）
    与链接成员（软链/硬链指向目录外）。校验在 **解包之前** 全部完成，任何一个
    成员非法就整体失败，不留下“解了一半”的目录。
    """
    destination = Path(dest).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode) as tar:
            for member in tar.getmembers():
                name = member.name.replace("\\", "/")
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                    raise ArchivedPathError(f"归档包含绝对路径，拒绝解包：{member.name}")
                target = (destination / name).resolve()
                if target != destination and destination not in target.parents:
                    raise ArchivedPathError(f"归档成员逃出目标目录，拒绝解包：{member.name}")
                if member.issym() or member.islnk():
                    link = (target.parent / member.linkname).resolve()
                    if link != destination and destination not in link.parents:
                        raise ArchivedPathError(
                            f"归档链接指向目录外，拒绝解包：{member.name} -> {member.linkname}"
                        )
            tar.extractall(destination, filter="data")
    except (ArchivedPathError, SafeExtractError):
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SafeExtractError(f"解包失败：{archive}：{exc}") from exc


def extract_tar(
    archive: str | os.PathLike[str],
    dest: str | os.PathLike[str],
    *,
    mode: str = "r:gz",
) -> None:
    """``safe_extract_tar`` 的别名（调用点语义更直白）。"""
    safe_extract_tar(archive, dest, mode=mode)


# ============================================================
# 脱敏（watchdog 诊断包用）
# ============================================================
REDACTED = "[REDACTED]"

#: 记录当前实际使用的脱敏实现；测试据此确认走的是项目规则而不是本地退回规则。
REAL_REDACT = "atri_x.redact.secrets"

# 引号内取值的凭证键（``password`` / ``token`` / ``secret`` / ``api_key`` …）。
# 这一段是**对 atri_x.redact 的兜底**：项目规则里 key=value 那条的取值分支
# （``"..."`` 或 ``[^\s,;]+``）会把收尾引号一起吃掉，于是
#     {"password": "pw-123456"}  ->  {"password": [REDACTED]
# 第二遍再跑还会继续咬掉 ``}``，违反「重复脱敏必须幂等」。
# 这里在调项目规则**之前**先把引号内取值整体换掉，引号就不会丢。
_QUOTED_CREDENTIAL = re.compile(
    r'(?i)((?:password|passwd|token|secret|api[_-]?key)[\'"]?\s*[=:]\s*[\'"])(.*?)([\'"])',
    re.DOTALL,
)

_FALLBACK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(https?://[^/\s]+/bot)\d+:[A-Za-z0-9_-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (
        re.compile(
            r"(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
        ),
        "[REDACTED]",
    ),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret"
            r"|bot[_-]?token|refresh[_-]?token|access[_-]?token|auth[_-]?token|token"
            r"|secret|password|passwd)[\"']?\s*[=:]\s*)(?:[\"'][^\"'\n]*[\"']|[^\s,;\n]+)"
        ),
        r"\1[REDACTED]",
    ),
]


def _fallback_redact(text: str) -> str:
    """``atri_x.redact`` 不可用时的本地等价规则（语义对齐，不追求逐字一致）。"""
    for pattern, replacement in _FALLBACK_RULES:
        text = pattern.sub(replacement, text)
    return text


def real_redact(text: str) -> str:
    """调用项目的 ``atri_x.redact.secrets``；不可导入时退回本地等价规则。

    退回路径是必要的：``deploy/`` 下的脚本可能在**没装 atri_x 的机器**上运行
    （例如单独拷一份 watchdog 到运维机），不能因为 import 失败就放弃脱敏。
    """
    try:
        from atri_x.redact import secrets  # 延迟导入：避免脚本启动就依赖包布局
    except Exception:  # noqa: BLE001 - 缺包 / 包损坏都退回本地规则
        return _fallback_redact(text)
    return secrets(text)


def _local_redact(text: str, limit: int = 5) -> str:
    """先补引号内取值，再按项目规则脱敏，直到结果不再变化。

    ``atri_x.redact`` 的 key=value 规则会吃掉收尾引号，所以**单遍不幂等**
    （实测 ``{"password": "pw-123456"}`` 第一遍得到 ``{"password": [REDACTED]}``，
    第二遍继续咬掉 ``}``）。上游 shell 版靠“再跑一遍 sed 必须不变”来验收幂等，
    因此这里必须返回一个**不动点**：反复应用同一组规则直到输出稳定。

    ``limit`` 是安全阀：规则集理论上可能来回振荡（例如某条规则把 ``A`` 换成
    ``B``、另一条把 ``B`` 换回 ``A``）。真出现那种情况，这里返回第 ``limit``
    遍的结果，由调用方的复检（:func:`detect_credential_patterns`）报出来，
    而不是让脚本无限循环。
    """
    current = text
    for _ in range(max(1, limit)):
        fixed = _QUOTED_CREDENTIAL.sub(lambda m: m.group(1) + REDACTED + m.group(3), current)
        candidate = real_redact(fixed)
        if candidate == current:
            return current
        current = candidate
    return current


def redact_dir(
    directory: str | os.PathLike[str],
    *,
    passes: int = 3,
    extra_secrets: Sequence[str] = (),
) -> tuple[bool, list[str]]:
    """就地脱敏整个目录，直到内容不再变化（最多 ``passes`` 遍）。

    ``extra_secrets`` 是「已知凭据值」：当前进程环境里敏感变量的取值，逐个当
    字面量替换——通用规则抓不到的自定义 secret 靠这一步兜住（对应上游
    ``detect_known_secrets``，但这里直接清除而不是只报警，因为“报警了但没清”
    仍然会让诊断包带着凭据被外发）。

    返回 ``(全部成功?, 失败说明列表)``。读不出 / 写不进都算失败，绝不静默跳过。

    只写回**内容真的变了**的文件：编码损坏的二进制日志不会因为“读出来又写
    回去”而被二次破坏。
    """
    base = Path(directory)
    if not base.is_dir():
        return True, []
    failures: list[str] = []
    secrets_sorted = sorted(
        {s for s in extra_secrets if s and len(s) >= 8}, key=len, reverse=True
    )
    for path in iter_files(base):
        try:
            original = path.read_bytes()
        except OSError as exc:
            failures.append(f"无法读取：{path}：{exc}")
            continue
        text = original.decode("utf-8", errors="surrogateescape")
        # ``_local_redact`` 自身就返回不动点，所以这里一遍就够；
        # ``passes`` 保留是为了让调用方能显式收紧/放宽收敛上限。
        mutated = _local_redact(text, limit=max(1, passes))
        for secret in secrets_sorted:
            mutated = mutated.replace(secret, REDACTED)
        if mutated == text:
            continue
        try:
            path.write_bytes(mutated.encode("utf-8", errors="surrogateescape"))
        except OSError as exc:
            failures.append(f"无法写入：{path}：{exc}")
    return not failures, failures


# 独立检测用的原始凭据形态。**这些模式刻意不命中 ``[REDACTED]`` 占位**：
# 通用 ``key=value`` / ``Bearer`` / ``sk-`` 形态由脱敏规则负责，这里只用原始
# 令牌 / JWT / bot token / 带 userinfo 的 URL 做交叉验证，避免“自己验自己”。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"bot[0-9]+:[A-Za-z0-9_-]{20,}"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@[^/\s]+"),
)


def detect_credential_patterns(directory: str | os.PathLike[str]) -> list[str]:
    """扫描目录里仍像原始凭据的文件，返回命中说明列表（空 = 通过）。

    对应上游 ``detect_credential_patterns``，只读不写，是脱敏之后的第二道
    独立检查：它不复用脱敏规则，所以“脱敏规则写漏了”能在这里暴露出来。
    """
    hits: list[str] = []
    base = Path(directory)
    if not base.is_dir():
        return hits
    for path in iter_files(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in _CREDENTIAL_PATTERNS:
            match = pattern.search(text)
            if match:
                hits.append(f"疑似未脱敏内容：{path}（命中 {match.group(0)[:24]}…）")
                break
    return hits


def detect_known_secrets(
    directory: str | os.PathLike[str],
    secrets: Sequence[str],
) -> list[str]:
    """在目录里按字面量搜索已知凭据值，返回命中说明列表（空 = 通过）。

    对应上游 ``detect_known_secrets``：只看“当前进程环境里的敏感值”。
    """
    hits: list[str] = []
    base = Path(directory)
    if not base.is_dir():
        return hits
    wanted = [s for s in secrets if s and len(s) >= 8]
    if not wanted:
        return hits
    for path in iter_files(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for secret in wanted:
            if secret in text:
                hits.append(f"已知凭据值仍在文件中：{path}")
                break
    return hits


# ============================================================
# 环境里的敏感变量
# ============================================================
_SENSITIVE_SUFFIXES = (
    "_TOKEN",
    "_API_KEY",
    "_APIKEY",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIAL",
    "_KEY",
)
_SENSITIVE_EXACT = frozenset({"OPS_TOKEN", "WATCHDOG_OPS_TOKEN", "WATCHDOG_WEBHOOK_URL"})


def sensitive_env_name(name: str, patterns: Sequence[str] = ()) -> bool:
    """判断环境变量名是否像凭据（对应上游 ``detect_known_secrets`` 的 case 分支）。

    默认规则：名字本身属于 :data:`_SENSITIVE_EXACT`，或以 ``_TOKEN`` /
    ``_API_KEY`` / ``_SECRET`` / ``_PASSWORD`` / ``_KEY`` 等后缀结尾。
    ``patterns`` 用于追加自定义后缀（可写成 ``"*_PASS"`` 或 ``"_PASS"``）。
    """
    upper = name.upper()
    if upper in _SENSITIVE_EXACT:
        return True
    suffixes = list(_SENSITIVE_SUFFIXES)
    for pattern in patterns:
        cleaned = pattern.strip().upper().lstrip("*")
        if cleaned and cleaned not in suffixes:
            suffixes.append(cleaned)
    return upper.endswith(tuple(suffixes))


def known_secrets_from_env(patterns: Sequence[str] = ()) -> list[str]:
    """从当前进程环境里挑出敏感变量的取值（长度 >= 8，且不含换行）。"""
    found: list[str] = []
    for name, value in os.environ.items():
        if not sensitive_env_name(name, patterns):
            continue
        if len(value) < 8 or "\n" in value or "\r" in value:
            continue
        if value not in found:
            found.append(value)
    return found
