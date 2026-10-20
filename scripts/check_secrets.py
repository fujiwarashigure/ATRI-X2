#!/usr/bin/env python3
"""发行前脱敏扫描：仓库里不该出现真实的个人目录、令牌密钥。

为什么单独一个脚本：``atri_x.redact`` 管的是**运行期日志脱敏**，而"发出去的仓库/发行包里
有没有夹带本机私密信息"是另一件事，只能靠静态扫描。发布前跑一次::

    python scripts/check_secrets.py            # 扫（默认扫 git 跟踪的文本文件）
    python scripts/check_secrets.py --all      # 连未跟踪文件一起扫（提交前自查）
    python scripts/check_secrets.py --json

判定口径（宁可误报，不要漏报；误报用注释 ``secrets-scan: allow`` 或允许值说明）：

- **个人目录**：``C:\\Users\\<真实用户名>`` 这类盘符 + Users + 用户名（占位符
  ``<用户>`` / ``%USERNAME%`` / ``*`` 不算）；
- **本项目令牌**：``atrix-gui-…`` / ``atrix-ops-…`` 这类随机串（文档里的占位说明不算）；
- **模型密钥**：``sk-`` 开头的长串（仓库测试里固定的假值 ``sk-abcdef…`` 不算）；
- **.env 赋值**：``ATRIX_OPS_TOKEN=…`` / ``QQONEBOT_ACCESS_TOKEN=…`` 等键后面跟了
  看起来像真值的长串（``xxx``/``test``/``smoke``/``example``/``请替换`` 这类占位不算）。

退出码：0 干净；1 有命中。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 只扫这些后缀的文本文件（其余按二进制跳过）。
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".json", ".yml", ".yaml", ".txt", ".bat", ".cmd", ".ps1",
    ".iss", ".spec", ".service", ".timer", ".conf", ".env", ".example", ".ini", ".cfg",
    ".dockerignore", ".gitignore", ".gitattributes", ".sh", ".html", ".css", ".js",
}
#: 额外按文件名纳入的文件。
TEXT_NAMES = {".env", ".env.example", "Dockerfile", "Makefile", "VERSION"}
#: 单个文件读取上限（超过按二进制跳过）。
MAX_BYTES = 2 * 1024 * 1024
#: 明显是占位的值（大小写不敏感）。
PLACEHOLDER_HINTS = (
    "xxx", "test", "smoke", "example", "sample", "placeholder", "changeme",
    "your-", "your_", "token", "secret", "password", "redacted", "<", "${", "%",
)
#: 仓库里固定使用的假密钥前缀（测试夹具，非真实 key）。
FAKE_SK_PREFIXES = ("sk-abcdefghij", "sk-1234567890")

#: (规则名, 正则, 说明)
RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "personal-home",
        re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}([A-Za-z0-9._-]{2,})"),
        "Windows 个人目录（真实用户名）",
    ),
    (
        "atrix-token",
        re.compile(r"atrix-(?:gui|ops)-[A-Za-z0-9_-]{16,}"),
        "ATRI-X 本机生成的令牌",
    ),
    (
        "sk-key",
        re.compile(r"sk-(?!abcdefghij|1234567890)[A-Za-z0-9]{24,}"),
        "模型 API Key 形状的串",
    ),
    (
        "env-assignment",
        re.compile(
            r"(?im)^\s*(?:export\s+)?("
            r"ATRIX_OPS_TOKEN|ATRIX_CLI_LOCAL_TOKEN|QQONEBOT_ACCESS_TOKEN|"
            r"DEEPSEEK_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|TELEGRAM_BOT_TOKEN"
            r")\s*=\s*([A-Za-z0-9_.\-+/=]{16,})\s*$"
        ),
        ".env 里像真值的令牌赋值",
    ),
)


def _run_git(args: list[str]) -> str:
    completed = subprocess.run(  # noqa: S603 - 固定 git 命令
        ["git", *args], cwd=str(REPO), capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def iter_files(include_untracked: bool) -> list[Path]:
    """枚举要扫描的文件（默认只扫 git 跟踪的，``--all`` 连未跟踪文本文件一起）。"""
    names: list[str] = [name for name in _run_git(["ls-files"]).splitlines() if name]
    if not names:
        names = [
            str(path.relative_to(REPO)).replace("\\", "/")
            for path in REPO.rglob("*")
            if path.is_file()
            and not any(part in (".git", "__pycache__", "dist", "build", "runtime")
                        for part in path.relative_to(REPO).parts)
        ]
    if include_untracked:
        extra = _run_git(["ls-files", "--others", "--exclude-standard"]).splitlines()
        names.extend(name for name in extra if name)
    seen: set[str] = set()
    files: list[Path] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = REPO / name
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    files.sort()
    return files


def _looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """扫描一段文本，返回 ``[(行号, 规则名, 命中片段)]``。"""
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if "secrets-scan: allow" in line:
            continue
        for name, pattern, _description in RULES:
            match = pattern.search(line)
            if match is None:
                continue
            if name == "env-assignment" and _looks_placeholder(match.group(2)):
                continue
            if name == "personal-home" and set(match.group(1)) <= set(".…*"):
                continue  # C:\Users\... 这类省略号不是真实用户名
            if name == "sk-key" and any(
                match.group(0).startswith(prefix) for prefix in FAKE_SK_PREFIXES
            ):
                continue
            snippet = match.group(0)
            if len(snippet) > 72:
                snippet = snippet[:69] + "..."
            hits.append((number, name, snippet))
    return hits


def scan(include_untracked: bool = False) -> dict[str, list[dict[str, object]]]:
    """扫描仓库，返回 ``{相对路径: [{line, rule, snippet, description}]}``。"""
    report: dict[str, list[dict[str, object]]] = {}
    description_of = {name: description for name, _pattern, description in RULES}
    for path in iter_files(include_untracked):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = scan_text(text)
        if not hits:
            continue
        relative = str(path.relative_to(REPO)).replace("\\", "/")
        report[relative] = [
            {
                "line": number,
                "rule": name,
                "description": description_of.get(name, ""),
                "snippet": snippet,
            }
            for number, name, snippet in hits
        ]
    return report


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回 0（干净）或 1（有命中）。"""
    parser = argparse.ArgumentParser(
        prog="python scripts/check_secrets.py",
        description="发行前脱敏扫描：个人目录 / 令牌 / 模型密钥",
    )
    parser.add_argument("--all", action="store_true", help="连未跟踪的文本文件一起扫")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = scan(include_untracked=bool(args.all))
    total = sum(len(items) for items in report.values())
    if args.json:
        print(json.dumps({"ok": not report, "hits": total, "files": report},
                         ensure_ascii=False, indent=2))
        return 0 if not report else 1
    if not report:
        print(f"secrets-scan: 干净（{total} 处命中，扫描范围：{'含未跟踪' if args.all else 'git 跟踪'}文件）")
        return 0
    print(f"secrets-scan: 发现 {total} 处疑似敏感信息（{len(report)} 个文件）：")
    for name, items in report.items():
        print(f"  {name}")
        for item in items:
            print(f"    L{item['line']} [{item['rule']}] {item['snippet']}  # {item['description']}")
    print("处理：删除/改成占位符；确属测试假值时用固定假值前缀（sk-abcdef… / sk-12345…）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
