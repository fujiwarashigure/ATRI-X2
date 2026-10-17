#!/usr/bin/env python3
"""从上游 Go 源码机械抽取内置配置资产，生成 Python 版本。

为什么用脚本而不是手抄：``internal/config/assets.go`` 是 42.6KB 的**原始字符串
常量**（默认 ``app.toml`` / ``services.toml`` / ``state.toml`` / ``SOUL.md`` /
内置 Agent Skill 等）。手抄会引入不可见的空白与行尾差异，而
``internal/config/inspect.go`` 的 ``BuiltinAssetDrift`` 会拿内置资产与磁盘文件
逐字节比较，因此必须**保真抽取**。

抽取规则：
1. 解析 ``assets.go`` 里所有 ``const <name> = `<raw string>``` 声明。
2. 按 ``defaultConfigAssets`` 的 ``{Path: ..., Content: <name>}`` 映射，把内容写进
   ``atri_x/config/assets_data/<Path>``（保持与运行期落盘完全相同的相对路径）。
3. 生成 ``atri_x/config/assets.py``：数据清单 + 每个资产一个模块级常量 + 访问函数。

用法::

    python scripts/extract_assets.py [--upstream <上游 Go 仓库根>] [--check]

``--check`` 只比较、不写文件；发现漂移时退出码为 1。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: 上游默认位置：环境变量 ``ATRI_X_UPSTREAM`` 优先，否则取与本仓库同级的 ``ATRI-X``。
DEFAULT_UPSTREAM = Path(os.environ.get("ATRI_X_UPSTREAM") or (REPO.parent / "ATRI-X"))

# `const name = ` 后跟表达式（表达式可能由多个原始字符串用 `+` 连接，也可能
# 引用同文件里先定义的另一个 const —— `defaultServicesTOML` 就是 5 段拼接）。
CONST_DECL_RE = re.compile(r"^const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
ASSET_ENTRY_RE = re.compile(
    r"\{Path:\s*(?P<path>filepath\.Join\([^)]*\)|\"[^\"]*\")\s*,"
    r"\s*Content:\s*(?P<content>[A-Za-z_][A-Za-z0-9_]*)\s*\}"
)


def go_string_expr_to_python(expr: str) -> str:
    """把 Go 的路径表达式转成 Python。

    只处理 ``"a"``、``"a/b"``、``filepath.Join("a", "b", ...)`` 三种形态，
    因为 ``defaultConfigAssets`` / ``defaultConfigDirs`` 里只有这三种。
    """
    expr = expr.strip()
    if expr.startswith("filepath.Join("):
        inner = expr[len("filepath.Join("): -1]
        parts = re.findall(r'"([^"]*)"', inner)
        return "/".join(parts)
    m = re.fullmatch(r'"([^"]*)"', expr)
    if m:
        return m.group(1)
    raise ValueError(f"无法解析的路径表达式: {expr!r}")


def _scan_expr(text: str, pos: int) -> tuple[str, int]:
    """从 ``pos`` 起扫描一个 Go 常量表达式，返回 ``(表达式文本, 结束位置)``。

    表达式在换行处结束，除非下一非空行以 ``+`` 开头（Go 的跨行拼接写法）。
    """
    n = len(text)
    i = pos
    while i < n:
        ch = text[i]
        if ch == "`":
            j = text.find("`", i + 1)
            if j < 0:
                raise ValueError("原始字符串没有闭合反引号")
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            i = j + 1
            continue
        if ch == "\n":
            k = i + 1
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "+":
                i = k
                continue
            return text[pos:i], i
        i += 1
    return text[pos:], n


def _unescape_go(s: str) -> str:
    """解码 Go 双引号字符串里的转义。"""
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(s):
            break
        e = s[i]
        i += 1
        simple = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "a": "\a", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
        if e in simple:
            out.append(simple[e])
        elif e == "x" and i + 2 <= len(s):
            out.append(chr(int(s[i:i + 2], 16)))
            i += 2
        elif e == "u" and i + 4 <= len(s):
            out.append(chr(int(s[i:i + 4], 16)))
            i += 4
        elif e == "U" and i + 8 <= len(s):
            out.append(chr(int(s[i:i + 8], 16)))
            i += 8
        else:
            raise ValueError(f"不支持的 Go 转义: \\{e}")
    return "".join(out)


def _tokenize_expr(expr: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "+":
            i += 1
            continue
        if ch == "`":
            j = expr.find("`", i + 1)
            if j < 0:
                raise ValueError("原始字符串没有闭合反引号")
            tokens.append(expr[i + 1:j])
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and expr[j] != '"':
                if expr[j] == "\\":
                    j += 1
                j += 1
            tokens.append(_unescape_go(expr[i + 1:j]))
            i = j + 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", expr[i:])
        if m:
            tokens.append(("REF", m.group(0)))  # type: ignore[arg-type]
            i += m.end()
            continue
        raise ValueError(f"常量表达式里有无法解析的字符: {expr[i:i+20]!r}")
    return tokens


def parse_consts(text: str) -> dict[str, str]:
    """抽出全部 ``const X = <expr>`` 并求值（支持 ``+`` 拼接与常量引用）。"""
    exprs: dict[str, str] = {}
    for m in CONST_DECL_RE.finditer(text):
        expr, _ = _scan_expr(text, m.end())
        exprs[m.group(1)] = expr

    resolved: dict[str, str] = {}
    in_progress: set[str] = set()

    def resolve(name: str) -> str:
        if name in resolved:
            return resolved[name]
        if name in in_progress:
            raise ValueError(f"常量引用成环: {name}")
        if name not in exprs:
            raise ValueError(f"引用了未定义的常量: {name}")
        in_progress.add(name)
        parts: list[str] = []
        for token in _tokenize_expr(exprs[name]):
            if isinstance(token, tuple):
                parts.append(resolve(token[1]))
            else:
                parts.append(token)
        in_progress.discard(name)
        value = "".join(parts)
        resolved[name] = value
        return value

    for name in exprs:
        resolve(name)
    return resolved


def _block_after(text: str, marker: str) -> str:
    """取 ``marker`` 开始、直到下一行顶格 ``}`` 之间的块。

    不能用「第一个 ``}``」——条目自身以 ``}`` 结尾，那样会截断。
    """
    start = text.find(marker)
    if start < 0:
        return ""
    end = text.find("\n}", start)
    if end < 0:
        return text[start:]
    return text[start:end]


def parse_assets(text: str) -> list[tuple[str, str]]:
    """抽出 ``defaultConfigAssets`` 的 ``(相对路径, Go 常量名)`` 列表。"""
    block = _block_after(text, "var defaultConfigAssets")
    out: list[tuple[str, str]] = []
    for m in ASSET_ENTRY_RE.finditer(block):
        out.append((go_string_expr_to_python(m.group("path")), m.group("content")))
    return out


def parse_dirs(text: str) -> list[str]:
    block = _block_after(text, "var defaultConfigDirs")
    return [
        go_string_expr_to_python(m)
        for m in re.findall(r"filepath\.Join\([^)]*\)|\"[^\"]*\"", block)
    ]


def snake(name: str) -> str:
    """``defaultAgentSkillCreatorSkillMD`` → ``DEFAULT_AGENT_SKILL_CREATOR_SKILL_MD``。"""
    out = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    out = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", out)
    return out.upper()


HEADER = '''"""内置默认配置资产。**本文件由 ``scripts/extract_assets.py`` 生成，不要手改。**

对应 Go：``internal/config/assets.go``。

资产正文放在同目录 :file:`assets_data/` 下，**相对路径与运行期落盘路径完全一致**
（因为 ``BuiltinAssetDrift`` 会逐字节比较内置资产与磁盘文件）。修改上游后重新运行::

    python scripts/extract_assets.py

目录布局来源：Go 的 ``defaultConfigAssets`` / ``defaultConfigDirs``。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "assets_data"

#: 上游 Go 包名，保留以便交叉检索。
UPSTREAM_GO_CONST = "{upstream}"


@dataclass(frozen=True)
class DefaultAsset:
    """一条内置资产。对应 Go ``defaultAsset``。"""

    #: 相对配置目录的落盘路径（POSIX 分隔符）
    path: str
    #: 对应的 Go 常量名（仅用于交叉检索）
    go_const: str

    def content(self) -> str:
        """读取资产正文（UTF-8，LF）。"""
        return (DATA_DIR / self.path).read_text(encoding="utf-8")


#: 默认配置目录。对应 Go ``defaultConfigDirs``。
DEFAULT_CONFIG_DIRS: tuple[str, ...] = {dirs}


#: 默认配置资产清单。对应 Go ``defaultConfigAssets``。
DEFAULT_CONFIG_ASSETS: tuple[DefaultAsset, ...] = (
{assets}
)


def default_config_asset(path: str) -> DefaultAsset | None:
    """按落盘路径查一条内置资产。"""
    for asset in DEFAULT_CONFIG_ASSETS:
        if asset.path == path:
            return asset
    return None


def ensure_platform_defaults(config_dir: Path) -> None:
    """在 ``config_dir`` 下创建默认目录与缺失的默认资产。

    对应 Go ``EnsurePlatformDefaults``（Go 侧还负责解析平台默认配置路径，
    那部分在 ``atri_x.config.loader`` 里）。
    """
    for rel in DEFAULT_CONFIG_DIRS:
        (config_dir / rel).mkdir(parents=True, exist_ok=True)
    for asset in DEFAULT_CONFIG_ASSETS:
        target = config_dir / asset.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(asset.content(), encoding="utf-8", newline="\\n")


def copy_asset_tree(dest: Path) -> None:
    """把整个资产树复制到 ``dest``（用于离线打包/备份脚本）。"""
    shutil.copytree(DATA_DIR, dest, dirs_exist_ok=True)
'''


def build_python(
    upstream_rel: str, dirs: list[str], assets: list[tuple[str, str]]
) -> str:
    dirs_repr = (
        "()"
        if not dirs
        else "(\n" + "".join(f'    "{d}",\n' for d in dirs) + ")"
    )
    asset_lines = []
    for path, go_const in assets:
        asset_lines.append(f'    DefaultAsset(path="{path}", go_const="{go_const}"),')
    assets_repr = "\n".join(asset_lines)
    return HEADER.format(upstream=upstream_rel, dirs=dirs_repr, assets=assets_repr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--upstream", default=str(DEFAULT_UPSTREAM), help="上游 ATRI-X（Go）仓库根")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args(argv)

    upstream = Path(args.upstream)
    assets_go = upstream / "internal" / "config" / "assets.go"
    if not assets_go.is_file():
        print(f"找不到上游文件: {assets_go}", file=sys.stderr)
        return 2

    text = assets_go.read_text(encoding="utf-8")
    consts = parse_consts(text)
    assets = parse_assets(text)
    dirs = parse_dirs(text)
    if not assets:
        print("没有从 defaultConfigAssets 解析出任何资产", file=sys.stderr)
        return 2

    missing = [name for _, name in assets if name not in consts]
    if missing:
        print(f"以下常量名在 assets.go 里没有对应的 const 原始字符串: {missing}", file=sys.stderr)
        return 2

    extra = sorted(set(consts) - {name for _, name in assets})
    if extra:
        print(f"提示：assets.go 里还有 {len(extra)} 个未进入 defaultConfigAssets 的常量: {extra}")

    data_dir = REPO / "atri_x" / "config" / "assets_data"
    changed: list[str] = []
    for path, name in assets:
        target = data_dir / path
        content = consts[name]
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            continue
        changed.append(path)
        if not args.check:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

    py_target = REPO / "atri_x" / "config" / "assets.py"
    py_source = build_python("internal/config/assets.go", dirs, assets)
    py_changed = (not py_target.is_file()) or py_target.read_text(encoding="utf-8") != py_source
    if py_changed and not args.check:
        py_target.parent.mkdir(parents=True, exist_ok=True)
        py_target.write_text(py_source, encoding="utf-8", newline="\n")

    total_bytes = sum(len(consts[name].encode("utf-8")) for _, name in assets)
    print(f"资产数: {len(assets)}  总字节: {total_bytes}  默认目录: {len(dirs)}")
    if args.check:
        if changed or py_changed:
            print(f"漂移: 数据文件 {changed}  assets.py 需更新={py_changed}", file=sys.stderr)
            return 1
        print("无漂移")
        return 0
    print(f"已写入 {len(changed)} 个数据文件到 {data_dir}" + (f"（变化: {changed}）" if changed else ""))
    print(f"assets.py 已更新: {py_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
