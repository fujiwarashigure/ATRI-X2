"""校验 ATRI-X Python 版镜像 tar 的结构与标签。

对应上游：``deploy/pack/verify-image-tar.py``（132 行）。

上游校验的是「手工拼出来的 ``scratch`` 镜像层」：它要确认层里有那个静态二进制、
权限是 0755、入口指向正确。Python 版的镜像 tar 是 **Docker 自己导出的真实产物**
（``buildx --output type=docker`` 或 ``docker build`` + ``docker save``），
所以这里改成校验「导出的东西是不是一个可用的 Python 版镜像」：

1. tar 可读、能解压出 ``manifest.json``（docker save 格式的标志）；
2. ``RepoTags`` 里含期望的 tag（防止把别的镜像 tar 当交付物）；
3. ``config`` 里的 ``Entrypoint`` 是 ``["python","-m","atri_x"]``；
4. ``Env`` 里带 ``PYTHONUNBUFFERED=1`` / ``PYTHONDONTWRITEBYTECODE=1``；
5. ``User`` 不是 root；
6. 层里能找到一个 ``atri_x/__init__.py``（确认应用代码真的进去了）。

校验**不需要 Docker**，所以在没有 Docker 的机器上也能先验包。

用法::

    python deploy/pack/verify_image_tar.py --tag atri-x:0.9.0 \\
        deploy/dist/atri-x-0.9.0-linux-amd64.tar.gz
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = Path(__file__).resolve().parents[1]
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

EXPECTED_ENTRYPOINT = ["python", "-m", "atri_x"]


class VerifyResult:
    """累计校验结果（不抛异常，最后一次汇总）。"""

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """记录一项检查结果。"""
        self.checks.append((name, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def report(self, archive: Path) -> None:
        """打印每项结果与总结。"""
        sys.stdout.write(f"校验 {archive}\n")
        for name, ok, detail in self.checks:
            mark = "ok  " if ok else "FAIL"
            suffix = f"  {detail}" if detail else ""
            sys.stdout.write(f"  [{mark}] {name}{suffix}\n")
        if self.ok:
            sys.stdout.write("verify_image_tar: passed\n")
        else:
            failed = [name for name, ok, _ in self.checks if not ok]
            sys.stderr.write(f"verify_image_tar: failed（{len(failed)} 项）：{', '.join(failed)}\n")


def open_tar(archive: Path) -> tarfile.TarFile:
    """打开（可能是 gzip 的）tar；自动识别是否压缩。"""
    with archive.open("rb") as handle:
        head = handle.read(2)
    mode = "r:gz" if head == b"\x1f\x8b" else "r:"
    return tarfile.open(archive, mode)


def load_json_member(tar: tarfile.TarFile, name: str) -> dict:
    """读 tar 里的一个 JSON 成员。"""
    member = tar.extractfile(name)
    if member is None:
        raise KeyError(name)
    return json.loads(member.read().decode("utf-8"))


def find_application_layer(tar: tarfile.TarFile, layer_paths: list[str]) -> bool:
    """在镜像层 tar 里找 ``atri_x/__init__.py``。

    ``docker save`` 的每个 layer 自己也是一个 tar（可能被 gzip 压过）。
    只扫一层内的成员名，不落盘。
    """
    for layer in layer_paths:
        handle = tar.extractfile(layer)
        if handle is None:
            continue
        payload = handle.read()
        try:
            if payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)
            with tarfile.open(fileobj=io.BytesIO(payload)) as inner:
                names = inner.getnames()
        except (tarfile.TarError, OSError, EOFError):
            continue
        if any(name.endswith("atri_x/__init__.py") for name in names):
            return True
    return False


def verify(archive: Path, tag: str) -> VerifyResult:
    """执行全部校验；返回结果对象。"""
    result = VerifyResult()
    if not archive.is_file():
        result.check("归档存在", False, str(archive))
        return result
    size_mb = archive.stat().st_size / 1048576
    result.check("归档存在", True, f"{size_mb:.1f} MiB")

    try:
        tar = open_tar(archive)
    except (tarfile.TarError, OSError) as exc:
        result.check("tar 可读", False, str(exc))
        return result
    with tar:
        try:
            manifest = load_json_member(tar, "manifest.json")
        except (KeyError, ValueError) as exc:
            result.check("manifest.json 可读", False, str(exc))
            return result
        result.check("manifest.json 可读", True, f"{len(manifest)} 个条目")
        if not manifest:
            result.check("manifest 非空", False)
            return result

        entry = manifest[0]
        config_name = entry.get("Config", "")
        layer_paths = list(entry.get("Layers") or [])

        repo_tags = list(entry.get("RepoTags") or [])
        if tag:
            result.check(
                f"RepoTags 含 {tag}",
                tag in repo_tags,
                f"实际 {repo_tags}",
            )
        else:
            result.check("RepoTags 非空", bool(repo_tags), f"实际 {repo_tags}")

        try:
            config = load_json_member(tar, config_name)
        except (KeyError, ValueError) as exc:
            result.check("config 可读", False, f"{config_name}: {exc}")
            return result
        result.check("config 可读", True, config_name)

        container = config.get("config") or config.get("Config") or {}
        entrypoint = list(container.get("Entrypoint") or [])
        result.check(
            "Entrypoint 是 python -m atri_x",
            entrypoint == EXPECTED_ENTRYPOINT,
            f"实际 {entrypoint}",
        )

        env = list(container.get("Env") or [])
        for wanted in ("PYTHONUNBUFFERED=1", "PYTHONDONTWRITEBYTECODE=1"):
            result.check(f"Env 含 {wanted}", any(item.startswith(wanted) for item in env))

        user = str(container.get("User") or "")
        result.check("以非 root 运行", user not in {"", "0", "root", "0:0"}, f"User={user or '(默认 root)'}")

        labels = container.get("Labels") or {}
        result.check(
            "带 OCI 标签",
            bool(labels.get("org.opencontainers.image.title")),
            f"version={labels.get('org.opencontainers.image.version', '?')}",
        )

        result.check("层数合理", 0 < len(layer_paths) <= 32, f"{len(layer_paths)} 层")
        result.check(
            "层里含 atri_x 应用代码",
            find_application_layer(tar, layer_paths),
            "扫描各层查找 atri_x/__init__.py",
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="verify_image_tar.py",
        description="校验 ATRI-X 镜像 tar（结构 / 标签 / 入口 / 非 root / 应用代码）",
    )
    parser.add_argument("archives", nargs="+", help="要校验的镜像 tar(.gz)")
    parser.add_argument("--tag", default="", help="期望的镜像标签，例如 atri-x:0.9.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码（全部通过才 0）。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    failed = False
    for raw in args.archives:
        result = verify(Path(raw), args.tag)
        result.report(Path(raw))
        failed = failed or not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
