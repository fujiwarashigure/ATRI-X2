#!/usr/bin/env python3
"""ATRI-X 的 draw.io 动作适配器（随技能目录分发，纯标准库）。

上游 drawio-skill 3.4.2 的 `scripts/diagramctl.py` 是给"能直接跑 python 的 Agent"设计的：它
把命令写进 SKILL.md，还会用 ``sys.executable + <脚本>`` 起导入器子进程。ATRI-X 的
AgentSkill 契约只有一条命令 + stdout JSON，所以这里做三件事：

1. 把「动作 + 参数」翻译成上游脚本的 argv（build / inspect / query / validate / shapes）；
2. 负责渲染：调**随包内置**的 draw.io 桌面版（``drawio_runtime/draw.io.exe``）做无头导出，
   每次导出用私有 profile 目录收容 ``APPDATA`` / ``LOCALAPPDATA`` / ``TEMP`` / ``TMP``，
   不读写宿主机上用户自己的 draw.io 配置；
3. 把结果整理成 ATRI-X 的 stdout 信封：``{"content": "...", "segments": [...]}``，
   图片/文件路径一律是**技能目录内的相对路径**（``out/xxx.png``），由宿主导入媒体中心。

冻结包里本文件由 ``atri-x.exe -m atri_x.diagram.runner --skill-dir <dir>`` 用 runpy 执行；
源码树里由当前 Python 直接执行。两种形态都**不 import atri_x**，因此不依赖仓库路径。

动作与参数说明见 ``SKILL.md``；不要改 stdout 以外的东西，``CommandTool`` 只读 stdout。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

#: 技能根目录（脚本所在目录）；宿主以它作为 cwd，媒体相对路径也以它为基准。
SKILL_ROOT = Path(__file__).resolve().parent
SCRIPTS = SKILL_ROOT / "scripts"
REFERENCES = SKILL_ROOT / "references"
OUT_DIR = SKILL_ROOT / "out"
TMP_DIR = OUT_DIR / ".tmp"
PROFILE_DIR = OUT_DIR / ".drawio-profile"

#: 内置渲染器的可执行文件名（官方解包目录是 ``draw.io.exe``）。
DRAWIO_NAMES = ("draw.io.exe", "drawio.exe", "draw.io", "drawio")
#: 渲染超时（秒）：单页 PNG 约 3 秒，多页/大图留足余量。
RENDER_TIMEOUT = 180
#: 上游脚本超时（秒）：大代码库导入可能几十秒。
SCRIPT_TIMEOUT = 300
#: 输出目录最多保留的产物数（超出按修改时间删旧）。
MAX_OUTPUTS = 60
#: 单次返回给模型的文本上限（字符）。
MAX_CONTENT_CHARS = 12000


# ---------------------------------------------------------------------------
# 输出信封
# ---------------------------------------------------------------------------


def _emit(payload: dict) -> None:
    """打印 ATRI-X 的 stdout 信封（唯一的 stdout 写入口）。"""
    text = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _emit_text(text: str, segments: list[dict] | None = None) -> None:
    """只给文本结果（无媒体）。"""
    _emit({"content": _clip(text), "segments": segments or []})


def _emit_media(text: str, segments: list[dict]) -> None:
    """文本 + 媒体段。"""
    _emit({"content": _clip(text), "segments": segments})


def _clip(text: str) -> str:
    """截断过长文本（保留头尾，避免模型上下文被一份大 JSON 撑爆）。"""
    text = (text or "").strip()
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    head = MAX_CONTENT_CHARS // 2
    tail = MAX_CONTENT_CHARS - head - 40
    return text[:head] + "\n…（已截断）…\n" + text[-tail:]


def _seg_image(path: Path) -> dict:
    """图片段（host 会导入媒体中心并让模型看到图）。"""
    return {
        "type": "image",
        "path": _rel(path),
        "name": path.name,
        "mime_type": "image/png",
    }


def _seg_file(path: Path, mime_type: str = "") -> dict:
    """文件段（.drawio / .svg / .pdf 等交付物）。"""
    segment = {"type": "file", "path": _rel(path), "name": path.name}
    if mime_type:
        segment["mime_type"] = mime_type
    return segment


def _rel(path: Path) -> str:
    """转成技能目录内的相对路径（正斜杠，拒绝越界）。"""
    resolved = path.resolve()
    try:
        return resolved.relative_to(SKILL_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"输出路径越出技能目录：{resolved}") from exc


# ---------------------------------------------------------------------------
# 环境与子进程
# ---------------------------------------------------------------------------


def _utf8_env() -> dict:
    """子进程环境：固定 UTF-8，避免 GBK 控制台把 JSON 中文打成异常。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _python_command(script: Path, args: list[str]) -> list[str]:
    """起上游 Python 脚本的命令。

    冻结包里没有 python.exe，``sys.executable`` 是 ``atri-x.exe``：必须走
    ``-m atri_x.diagram.runner --script <绝对路径>``，由运行器用 runpy 执行。
    """
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "-m",
            "atri_x.diagram.runner",
            "--script",
            str(script),
            *args,
        ]
    return [sys.executable, str(script), *args]


def _run(script: Path, args: list[str], timeout: int = SCRIPT_TIMEOUT) -> tuple[int, str, str]:
    """跑一个上游脚本，返回 ``(退出码, stdout, stderr)``；超时按失败返回。"""
    command = _python_command(script, args)
    try:
        proc = subprocess.run(
            command,
            cwd=str(SKILL_ROOT),
            env=_utf8_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"上游脚本超时（>{timeout}s）：{script.name}"
    except OSError as exc:
        return 125, "", f"无法启动上游脚本：{exc}"
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, stdout, stderr


def _parse_json_output(stdout: str) -> dict:
    """解析上游脚本的 JSON 输出（它们可能先打日志、后跟 ``score:`` 之类的尾行）。"""
    text = (stdout or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        return value if isinstance(value, dict) else {"value": value}
    return {}


def _tail(text: str, limit: int = 1200) -> str:
    """取尾部文本用于错误消息（上游报错通常最后几行最有用）。"""
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return "\n".join(lines[-12:])[:limit]


# ---------------------------------------------------------------------------
# 产物目录与命名
# ---------------------------------------------------------------------------


def _prepare_dirs() -> None:
    """建输出目录（媒体导入要求路径真实存在且都在技能目录内）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)


def _prune_outputs() -> None:
    """只保留最近的 ``MAX_OUTPUTS`` 个产物，避免配置目录无限膨胀。"""
    try:
        files = [
            path
            for path in OUT_DIR.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ]
    except OSError:
        return
    if len(files) <= MAX_OUTPUTS:
        return
    files.sort(key=lambda path: path.stat().st_mtime)
    for path in files[: len(files) - MAX_OUTPUTS]:
        try:
            path.unlink()
        except OSError:
            pass


def _safe_name(value: str, default: str = "diagram") -> str:
    """把模型给的名字收成安全文件名（不含目录分隔符/控制字符）。"""
    name = (value or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", name).strip(".-")
    return (name or default)[:60]


def _unique_path(base: Path) -> Path:
    """同名产物加短后缀，避免覆盖上一次结果。"""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    return base.with_name(f"{stem}-{int(time.time())}{suffix}")


def _temp_file(suffix: str) -> Path:
    """临时输入文件（放在技能目录内，宿主允许读）。"""
    _prepare_dirs()
    return TMP_DIR / f"in-{uuid.uuid4().hex[:12]}{suffix}"


# ---------------------------------------------------------------------------
# 渲染器定位与无头导出
# ---------------------------------------------------------------------------


def _drawio_candidates() -> list[Path]:
    """内置/本机 draw.io 可执行文件的候选路径（先内置、后兜底）。"""
    candidates: list[Path] = []
    override = (os.environ.get("ATRIX_DRAWIO") or "").strip()
    if override:
        candidates.append(Path(override))
    meipass = getattr(sys, "_MEIPASS", "") or ""
    if meipass:
        candidates.append(Path(meipass) / "drawio_runtime")
    executable = Path(sys.executable).resolve()
    candidates.append(executable.parent / "drawio_runtime")
    candidates.append(executable.parent / "_internal" / "drawio_runtime")
    for root in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", "")):
        root = (root or "").strip()
        if root:
            candidates.append(Path(root) / "Programs" / "draw.io")
            candidates.append(Path(root) / "draw.io")
    return candidates


def _exe_in(path: Path) -> str:
    """文件本身或目录里的 draw.io 可执行文件。"""
    if path.is_file():
        return str(path)
    if not path.is_dir():
        return ""
    for name in DRAWIO_NAMES:
        candidate = path / name
        if candidate.is_file():
            return str(candidate)
    return ""


def resolve_drawio(explicit: str = "") -> str:
    """定位 draw.io 可执行文件；找不到返回空串。"""
    explicit = (explicit or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    for candidate in _drawio_candidates():
        found = _exe_in(candidate)
        if found:
            return found
    for name in ("draw.io", "drawio", "draw.io.exe", "drawio.exe"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _profile_env() -> dict:
    """渲染子进程环境：把用户数据目录收容到技能目录下的私有 profile。"""
    env = dict(os.environ)
    profile = PROFILE_DIR
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        target = profile / key.lower()
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    env["DRAWIO_DISABLE_UPDATE"] = "true"
    return env


def _kill_tree(pid: int) -> None:
    """Windows 上强杀整个进程树（draw.io 会派生 GPU/crashpad 子进程）。"""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:  # pragma: no cover - Windows 是唯一交付平台
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def drawio_version(exe: str) -> str:
    """读 draw.io 版本号（``-V`` 输出形如 ``32.6.1``）；失败返回空串。"""
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-V"],
            cwd=str(SKILL_ROOT),
            env=_profile_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    return text.splitlines()[0].strip() if text else ""


def render(
    source: Path,
    output: Path,
    *,
    fmt: str = "png",
    exe: str = "",
    scale: str = "",
    width: str = "",
    height: str = "",
    border: str = "",
    transparent: bool = False,
    pages: str = "",
) -> tuple[bool, str]:
    """用内置 draw.io 无头导出一张图；返回 ``(是否成功, 失败原因/提示)``。

    实测（32.6.1）：导出模式**不显示任何窗口**，8 秒内退出；不写用户目录；
    ``APPDATA`` 等重定向后照常工作。缺 ``ffmpeg.dll`` 会让渲染进程崩溃，
    所以内置目录必须整目录随包（见 ``docs/diagram.md`` 的体积说明）。
    """
    exe = resolve_drawio(exe)
    if not exe:
        return False, "未找到 draw.io 渲染器（内置 drawio_runtime 缺失；可在配置里设 ATRIX_DRAWIO 指向 draw.io.exe）"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        exe,
        "-x",
        "-f",
        fmt,
        "--disable-update",
        "--disable-acceleration",
    ]
    if scale:
        command.extend(["-s", str(scale)])
    if width:
        command.extend(["--width", str(width)])
    if height:
        command.extend(["--height", str(height)])
    if border:
        command.extend(["-b", str(border)])
    if transparent:
        command.append("-t")
    if pages:
        if pages == "all":
            command.append("-a")
        elif "-" in pages:
            command.extend(["-g", pages])
        else:
            command.extend(["-p", pages])
    command.extend(["-o", str(output), str(source)])

    proc = subprocess.Popen(
        command,
        cwd=str(OUT_DIR),
        env=_profile_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=RENDER_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.communicate()
        return False, f"draw.io 渲染超时（>{RENDER_TIMEOUT}s）"
    out_text = (stdout or b"").decode("utf-8", errors="replace")
    err_text = (stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        detail = _tail(out_text + "\n" + err_text)
        return False, f"draw.io 渲染失败（exit={proc.returncode}）：{detail or '无输出'}"
    return True, ""


# ---------------------------------------------------------------------------
# 动作实现
# ---------------------------------------------------------------------------


def _read_content(content: str) -> str:
    """统一换行与去 BOM；拒绝空内容。"""
    text = (content or "").replace("\r\n", "\n").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("content 为空")
    return text


def _write_input(content: str, suffix: str) -> Path:
    """把内联内容落成临时文件（上游脚本只吃路径）。"""
    path = _temp_file(suffix)
    path.write_text(_read_content(content), encoding="utf-8", newline="\n")
    return path


def _detect_source_type(content: str) -> tuple[str, str]:
    """内联内容 → ``(source_type, 临时文件后缀)``。"""
    stripped = content.lstrip()
    if stripped.startswith("<"):
        return "drawio", ".drawio"
    try:
        data = json.loads(stripped)
    except ValueError:
        raise ValueError("无法识别 content 类型：XML 需以 < 开头，IR/graph 需是 JSON")
    if isinstance(data, dict) and data.get("schema") == "drawio-skill/diagram-ir/v1":
        return "ir", ".json"
    return "graph", ".json"


def _build_drawio(
    source: Path,
    output: Path,
    *,
    source_type: str = "auto",
    group: bool = False,
    direction: str = "",
    views: str = "",
    title: str = "",
) -> tuple[bool, dict, str]:
    """调上游 ``diagramctl.py build`` 生成 .drawio，返回 ``(成功, 摘要, 失败原因)``。"""
    args = ["build", str(source), "-o", str(output), "--from", source_type or "auto"]
    if group:
        args.append("--group")
    if direction:
        args.extend(["--direction", direction])
    if views:
        args.extend(["--views", views])
    if title:
        args.extend(["--title", title])
    args.extend(["--ir-output", str(output.with_suffix(".ir.json"))])
    code, stdout, stderr = _run(SCRIPTS / "diagramctl.py", args)
    if code != 0 or not output.is_file():
        return False, {}, f"build 失败（exit={code}）：{_tail(stderr or stdout)}"
    return True, _parse_json_output(stdout), ""


def _validate(drawio_path: Path) -> tuple[dict, str]:
    """跑上游结构校验，返回 ``(结果 JSON, 原始输出)``（score 只在文本行里）。"""
    code, stdout, stderr = _run(
        SCRIPTS / "validate.py", [str(drawio_path), "--json", "--score"]
    )
    payload = _parse_json_output(stdout)
    if payload and "score" not in payload:
        match = re.search(r"score:\s*([0-9.]+)", stdout or "")
        if match:
            payload["score"] = match.group(1)
    if code != 0 and not payload:
        return {}, f"validate 失败（exit={code}）：{_tail(stderr or stdout)}"
    return payload, stdout


def _render_and_collect(
    drawio_path: Path,
    *,
    fmt: str,
    exe: str,
    output_stem: str,
    scale: str,
    width: str,
    height: str,
    border: str,
    transparent: bool,
    pages: str,
) -> tuple[list[dict], list[str], str]:
    """按需渲染并整理媒体段，返回 ``(segments, 额外文本行, 失败原因)``。"""
    if fmt == "drawio":
        return [_seg_file(drawio_path, "application/x-drawio")], [], ""
    suffix = "." + fmt
    target = _unique_path(OUT_DIR / f"{output_stem}{suffix}")
    ok, reason = render(
        drawio_path,
        target,
        fmt=fmt,
        exe=exe,
        scale=scale,
        width=width,
        height=height,
        border=border,
        transparent=transparent,
        pages=pages,
    )
    if not ok:
        return [_seg_file(drawio_path, "application/x-drawio")], [f"渲染未成功，仅返回 .drawio：{reason}"], reason
    segments = [_seg_image(target)] if fmt == "png" else [_seg_file(target)]
    return segments, [f"已导出 {target.name}（{target.stat().st_size // 1024} KB）"], ""


def _action_doctor(args: argparse.Namespace) -> int:
    """环境自检：版本、渲染器、可选依赖、技能目录。永不抛异常。"""
    exe = resolve_drawio(args.drawio_exe)
    version = drawio_version(exe)
    references = sorted(path.name for path in REFERENCES.glob("*.md")) if REFERENCES.is_dir() else []
    payload = {
        "skill_dir": str(SKILL_ROOT),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "drawio_exe": exe,
        "drawio_version": version,
        "render_available": bool(version),
        "graphviz": bool(shutil.which("dot")),
        "scripts": len(list(SCRIPTS.glob("*.py"))) if SCRIPTS.is_dir() else 0,
        "references": references,
        "actions": sorted(ACTIONS),
    }
    _emit_text("draw.io 技能自检：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _action_create(args: argparse.Namespace) -> int:
    """从内联 IR/XML/graph 或文件生成 .drawio，并按需导出图片。"""
    _prepare_dirs()
    content = (args.content or "").strip()
    source_text = (args.source or "").strip()
    source_type = (args.source_type or "auto").strip()
    if not content and not source_text:
        _emit_text("create 需要 content（内联 IR/XML/JSON）或 source（文件路径）之一。")
        return 0
    temp_input: Path | None = None
    if content:
        detected, suffix = _detect_source_type(content)
        if source_type in ("", "auto"):
            source_type = detected
        if detected == "drawio" and source_type in ("drawio", "xml", "auto"):
            source = _write_input(content, ".drawio")
            temp_input = source
        else:
            source = _write_input(content, suffix)
            temp_input = source
    else:
        source = Path(source_text)
        if not source.is_absolute():
            source = SKILL_ROOT / source
        if not source.is_file():
            _emit_text(f"source 不存在：{source}")
            return 0
    stem = _safe_name(args.output or "diagram")
    if source.suffix.lower() == ".drawio" and source_type in ("drawio", "xml", "auto"):
        target = _unique_path(OUT_DIR / f"{stem}.drawio")
        shutil.copyfile(source, target)
        summary = {"format": "drawio", "nodes": None, "edges": None}
    else:
        target = _unique_path(OUT_DIR / f"{stem}.drawio")
        ok, summary, reason = _build_drawio(
            source,
            target,
            source_type=source_type or "auto",
            group=bool(args.group),
            direction=args.direction or "",
            views=args.views or "",
            title=args.title or "",
        )
        if not ok:
            _emit_text(reason)
            return 0
    validate_result, _raw = _validate(target)
    lines = [f"已生成 .drawio：{_rel(target)}"]
    if summary.get("nodes") is not None:
        lines.append(f"节点 {summary.get('nodes')} / 边 {summary.get('edges')}")
    if validate_result:
        lines.append(
            "结构校验：%s error / %s warning（score=%s）"
            % (
                validate_result.get("errors", "?"),
                validate_result.get("warnings", "?"),
                validate_result.get("score", "?"),
            )
        )
    segments: list[dict] = []
    fmt = (args.format or "png").strip()
    extra: list[str] = []
    if fmt and fmt not in ("drawio", "none"):
        segments, extra, _reason = _render_and_collect(
            target,
            fmt=fmt,
            exe=args.drawio_exe,
            output_stem=stem,
            scale=args.scale or "",
            width=str(args.width or ""),
            height=str(args.height or ""),
            border=str(args.border or ""),
            transparent=bool(args.transparent),
            pages=args.pages or "",
        )
    if not segments:
        segments = [_seg_file(target, "application/x-drawio")]
    _emit_media("\n".join(lines + extra), segments)
    if temp_input is not None:
        temp_input.unlink(missing_ok=True)
    return 0


def _ext_for_source_type(source_type: str, content: str) -> str:
    """内联内容写临时文件时用的扩展名（导入器按扩展名/文件头判定）。"""
    mapping = {
        "terraform": ".tf",
        "sql": ".sql",
        "openapi": ".json" if content.lstrip().startswith("{") else ".yaml",
        "asyncapi": ".json" if content.lstrip().startswith("{") else ".yaml",
        "proto": ".proto",
        "graphql": ".graphql",
        "kubernetes": ".yaml",
        "k8s": ".yaml",
        "compose": ".yaml",
        "ci": ".yml",
        "ir": ".json",
        "graph": ".json",
        "drawio": ".drawio",
    }
    return mapping.get(source_type, ".txt")


def _action_build(args: argparse.Namespace) -> int:
    """从代码/基础设施/API/数据源生成 .drawio（支持一期全部导入器）。"""
    _prepare_dirs()
    content = (args.content or "").strip()
    source_text = (args.source or "").strip()
    source_type = (args.source_type or "auto").strip() or "auto"
    temp_input: Path | None = None
    if content and source_type in ("auto", "", "ir", "graph", "drawio"):
        detected, _suffix = _detect_source_type(content)
        if source_type in ("auto", ""):
            source_type = detected
        temp_input = _write_input(content, _ext_for_source_type(source_type, content))
        source = temp_input
    elif content:
        temp_input = _write_input(content, _ext_for_source_type(source_type, content))
        source = temp_input
    elif source_text:
        source = Path(source_text)
        if not source.is_absolute():
            source = SKILL_ROOT / source
        if not source.exists():
            _emit_text(f"source 不存在：{source}")
            return 0
    else:
        _emit_text("build 需要 source（文件/目录路径）或 content（内联源码）之一。")
        return 0

    stem = _safe_name(args.output or "diagram")
    target = _unique_path(OUT_DIR / f"{stem}.drawio")
    ok, summary, reason = _build_drawio(
        source,
        target,
        source_type=source_type,
        group=bool(args.group),
        direction=args.direction or "",
        views=args.views or "",
        title=args.title or "",
    )
    if not ok:
        _emit_text(reason)
        if temp_input is not None:
            temp_input.unlink(missing_ok=True)
        return 0
    validate_result, _raw = _validate(target)
    lines = [
        f"已从 {source_type} 源生成 .drawio：{_rel(target)}",
    ]
    if summary.get("nodes") is not None:
        lines.append(f"节点 {summary.get('nodes')} / 边 {summary.get('edges')}")
    if summary.get("views"):
        lines.append("视图：" + ", ".join(str(item) for item in summary.get("views") or []))
    if validate_result:
        lines.append(
            "结构校验：%s error / %s warning（score=%s）"
            % (
                validate_result.get("errors", "?"),
                validate_result.get("warnings", "?"),
                validate_result.get("score", "?"),
            )
        )
    fmt = (args.format or "png").strip()
    extra: list[str] = []
    segments: list[dict] = []
    if fmt and fmt not in ("drawio", "none"):
        segments, extra, _reason = _render_and_collect(
            target,
            fmt=fmt,
            exe=args.drawio_exe,
            output_stem=stem,
            scale=args.scale or "",
            width=str(args.width or ""),
            height=str(args.height or ""),
            border=str(args.border or ""),
            transparent=bool(args.transparent),
            pages=args.pages or "",
        )
    if not segments:
        segments = [_seg_file(target, "application/x-drawio")]
    _emit_media("\n".join(lines + extra), segments)
    if temp_input is not None:
        temp_input.unlink(missing_ok=True)
    return 0


def _resolve_diagram_arg(args: argparse.Namespace) -> Path | None:
    """取动作输入图：``source`` 优先；否则 ``content`` 写临时 .drawio。"""
    source_text = (args.source or "").strip()
    if source_text:
        path = Path(source_text)
        if not path.is_absolute():
            path = SKILL_ROOT / path
        return path if path.is_file() else None
    content = (args.content or "").strip()
    if content:
        return _write_input(content, ".drawio")
    return None


def _action_export(args: argparse.Namespace) -> int:
    """把已有 .drawio 导出成 PNG/SVG/PDF（内置 draw.io 无头渲染）。"""
    _prepare_dirs()
    path = _resolve_diagram_arg(args)
    if path is None:
        _emit_text("export 需要 source（.drawio 路径）或 content（.drawio XML）之一。")
        return 0
    fmt = (args.format or "png").strip().lower()
    if fmt not in ("png", "svg", "pdf"):
        _emit_text("format 只支持 png / svg / pdf。")
        return 0
    stem = _safe_name(args.output or path.stem or "diagram")
    segments, extra, reason = _render_and_collect(
        path,
        fmt=fmt,
        exe=args.drawio_exe,
        output_stem=stem,
        scale=args.scale or "",
        width=str(args.width or ""),
        height=str(args.height or ""),
        border=str(args.border or ""),
        transparent=bool(args.transparent),
        pages=args.pages or "",
    )
    if reason or not segments:
        _emit_media(_clip("导出失败：" + (reason or "未知原因")), segments)
        return 0
    first = segments[0].get("path") or ""
    _emit_media("\n".join([f"已导出 {fmt.upper()}：{first}", *extra]), segments)
    return 0


def _action_validate(args: argparse.Namespace) -> int:
    """结构校验 .drawio：返回 error/warning/score 与问题清单。"""
    path = _resolve_diagram_arg(args)
    if path is None:
        _emit_text("validate 需要 source（.drawio 路径）或 content（.drawio XML）之一。")
        return 0
    result, raw = _validate(path)
    if not result:
        _emit_text(raw or "校验没有返回结果。")
        return 0
    lines = [
        "校验结果：%s error / %s warning（score=%s）"
        % (result.get("errors", "?"), result.get("warnings", "?"), result.get("score", "?")),
    ]
    findings = result.get("findings") or result.get("issues") or result.get("problems") or []
    if isinstance(findings, list):
        for item in findings[:20]:
            if isinstance(item, dict):
                lines.append(
                    "- [%s] %s" % (item.get("level") or item.get("severity") or "info", item.get("message") or item)
                )
            else:
                lines.append("- " + str(item))
    _emit_text("\n".join(lines))
    return 0


def _action_inspect(args: argparse.Namespace) -> int:
    """摘要：节点/边/页面/无障碍文本（上游 inspect）。"""
    path = _resolve_diagram_arg(args)
    if path is None:
        _emit_text("inspect 需要 source（.drawio 或 IR JSON 路径）之一。")
        return 0
    out = TMP_DIR / f"inspect-{uuid.uuid4().hex[:8]}.json"
    code, stdout, stderr = _run(SCRIPTS / "diagramctl.py", ["inspect", str(path)])
    payload = _parse_json_output(stdout)
    if code != 0 or not payload:
        _emit_text(f"inspect 失败（exit={code}）：{_tail(stderr or stdout)}")
        return 0
    _emit_text("inspect 结果：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _action_query(args: argparse.Namespace) -> int:
    """语义查询：按 kind/owner/boundary 过滤，或找两节点间的有向路径。"""
    path = _resolve_diagram_arg(args)
    if path is None:
        _emit_text("query 需要 source（.drawio 或 IR JSON 路径）之一。")
        return 0
    argv = ["query", str(path)]
    if args.query_kind:
        argv.extend(["--kind", args.query_kind])
    if args.query_owner:
        argv.extend(["--owner", args.query_owner])
    if args.from_node:
        argv.extend(["--from", args.from_node])
    if args.to_node:
        argv.extend(["--to", args.to_node])
    code, stdout, stderr = _run(SCRIPTS / "diagramctl.py", argv)
    payload = _parse_json_output(stdout)
    if code != 0 or not payload:
        _emit_text(f"query 失败（exit={code}）：{_tail(stderr or stdout)}")
        return 0
    _emit_text("query 结果：\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _action_shapes(args: argparse.Namespace) -> int:
    """官方形状检索：拿 draw.io 官方 style 字符串，避免模型臆造形状名。"""
    keyword = (args.keyword or args.content or "").strip()
    if not keyword:
        _emit_text('shapes 需要 keyword，例如 "aws lambda" / "uml actor"。')
        return 0
    code, stdout, stderr = _run(
        SCRIPTS / "shapesearch.py", [keyword, "--json", "--limit", "12"]
    )
    payload = _parse_json_output(stdout)
    if code != 0 or not payload:
        _emit_text(f"shapes 检索失败（exit={code}）：{_tail(stderr or stdout)}")
        return 0
    results = payload.get("results")
    if not isinstance(results, list):
        results = payload.get("value") if isinstance(payload.get("value"), list) else []
    lines = [f"shape 检索：{keyword}（{len(results)} 条）"]
    for item in results[:12]:
        if isinstance(item, dict):
            lines.append("- %s：%s" % (item.get("name") or item.get("title") or "?", item.get("style") or ""))
    if len(lines) == 1:
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    _emit_text("\n".join(lines))
    return 0


def _action_docs(args: argparse.Namespace) -> int:
    """读取技能内置参考文档（references/*.md）：先列目录，再按名字读正文。"""
    if not REFERENCES.is_dir():
        _emit_text("技能缺少 references/ 目录。")
        return 0
    doc = (args.doc or "").strip()
    if not doc:
        lines = ["可用参考文档（docs 动作 + doc=<文件名> 读取正文）："]
        for path in sorted(REFERENCES.glob("*.md")):
            lines.append("- " + path.name)
        _emit_text("\n".join(lines))
        return 0
    name = doc if doc.endswith(".md") else doc + ".md"
    target = (REFERENCES / name).resolve()
    try:
        rel = target.relative_to(REFERENCES.resolve())
    except ValueError:
        _emit_text("doc 只能取 references/ 下的文件名。")
        return 0
    if not target.is_file():
        _emit_text(f"没有这篇参考文档：{rel.as_posix()}（先不带 doc 调 docs 看列表）")
        return 0
    text = target.read_text(encoding="utf-8", errors="replace")
    _emit_text(f"# {rel.as_posix()}\n\n{text}")
    return 0


ACTIONS = {
    "doctor": _action_doctor,
    "create": _action_create,
    "build": _action_build,
    "export": _action_export,
    "validate": _action_validate,
    "inspect": _action_inspect,
    "query": _action_query,
    "shapes": _action_shapes,
    "docs": _action_docs,
}


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def _bool_text(value: str) -> bool:
    """``CommandTool`` 把 boolean 参数渲染成字符串 ``true`` / ``false``。"""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    """命令行：与 ``tool.json`` 的 ``[args]`` 映射逐项对应。"""
    parser = argparse.ArgumentParser(
        prog="atrix_cli.py",
        description="ATRI-X draw.io 动作适配器（由 ATRIX_SKILL.toml 调用）",
    )
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS))
    parser.add_argument("--source", default="", help="源文件/目录路径")
    parser.add_argument("--content", default="", help="内联内容（XML / IR JSON / SQL / OpenAPI…）")
    parser.add_argument("--source-type", default="auto", help="源类型（auto/ir/graph/terraform/sql/openapi…）")
    parser.add_argument("--format", default="png", help="输出格式：drawio/png/svg/pdf/html")
    parser.add_argument("--output", default="", help="输出文件名（不含扩展名）")
    parser.add_argument("--title", default="", help="图标题")
    parser.add_argument("--scale", default="", help="导出缩放倍数，如 2")
    parser.add_argument("--width", default=None, type=int, help="导出宽度（像素）")
    parser.add_argument("--height", default=None, type=int, help="导出高度（像素）")
    parser.add_argument("--border", default=None, type=int, help="图边框宽度（像素）")
    parser.add_argument("--transparent", default="false", type=_bool_text, help="透明背景")
    parser.add_argument("--pages", default="", help="页码：all / 3 / 1-3")
    parser.add_argument("--group", default="false", type=_bool_text, help="按目录/模块分组")
    parser.add_argument("--direction", default="", choices=["", "TB", "LR"], help="布局方向")
    parser.add_argument("--views", default="", help="多视图：executive,system,deployment,dataflow,security")
    parser.add_argument("--keyword", default="", help="形状/文档检索关键词")
    parser.add_argument("--doc", default="", help="references/ 下的文档文件名")
    parser.add_argument("--query-kind", default="", help="query：节点 kind")
    parser.add_argument("--query-owner", default="", help="query：owner")
    parser.add_argument("--from-node", default="", help="query：起点节点 id")
    parser.add_argument("--to-node", default="", help="query：终点节点 id")
    parser.add_argument("--drawio-exe", default="", help="draw.io 可执行文件（默认用内置渲染器）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """动作入口；所有可预期的失败都变成可读文本（退出码 0），不向模型抛栈。"""
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse 的参数错误：保持非零退出
        return int(exc.code or 2)
    _prepare_dirs()
    _prune_outputs()
    action = ACTIONS[args.action]
    try:
        return int(action(args) or 0)
    except ValueError as exc:
        _emit_text(f"参数错误：{exc}")
        return 0
    except OSError as exc:
        _emit_text(f"文件操作失败：{exc}")
        return 0
    except Exception as exc:  # noqa: BLE001 - 适配器不能让宿主看到栈
        _emit_text(f"{args.action} 执行失败：{type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    _prepare_dirs()
    raise SystemExit(main())
