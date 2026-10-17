"""把 ATRI-X Python 版打包成一个 ``docker load`` 可直接载入的镜像 tar。

对应上游：``deploy/pack/build-image-tar.py``（324 行）。

上游的输入是一个**静态编译好的 Linux 二进制**（``atri-x-linux-amd64``），它手工
构造 ``scratch`` 镜像层把二进制塞进去。Python 版没有编译产物，镜像总是基于
``python:*-slim``，因此本脚本改为**基于已构建的镜像导出**，不再手工拼层：

- 首选：``docker buildx build --output type=docker,dest=...``，一次产出可
  ``docker load`` 的 tar（多架构时对每个架构各跑一次）；
- 次选：``docker build`` + ``docker save``（经典构建器，单架构）。

这样做的直接好处：不会再出现“手拼的镜像层与 Dockerfile 漂移”的问题——导出
的就是 Dockerfile 真正构建出来的东西。

用法::

    python deploy/pack/build_image_tar.py --arch amd64 --version 0.9.0 \
        --output deploy/dist/atri-x-0.9.0-linux-amd64.tar.gz
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = Path(__file__).resolve().parents[1]
_PACK = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

DOCKERFILE = "deploy/Dockerfile"


def default_version(deploy_dir: Path) -> str:
    """版本号单点来源：``deploy/VERSION``。"""
    return _lib.read_text(deploy_dir / "VERSION").strip() or "dev"


class ImageTarBuilder:
    """按架构产出一个可 ``docker load`` 的 gzip tar。"""

    def __init__(self, arch: str, version: str, output: Path, base_image: str = "") -> None:
        self.arch = arch
        self.version = version
        self.output = output
        self.base_image = base_image
        self.tag = f"atri-x:{version}-{arch}"

    def build_args(self) -> list[str]:
        """拼 ``--build-arg``（空值不传，避免把 ``FROM`` 变成 ``:``）。"""
        args = ["--build-arg", f"VERSION={self.version}"]
        if self.base_image:
            args += ["--build-arg", f"PY_BASE_IMAGE={self.base_image}"]
        return args

    def build_with_buildx(self) -> bool:
        """用 buildx 产出 docker 格式的 tar；返回是否可用/成功。"""
        if _lib.run_capture(["docker", "buildx", "version"], timeout=60.0).returncode != 0:
            return False
        raw = self.output.with_suffix(".raw.tar")
        raw.unlink(missing_ok=True)
        argv = [
            "docker",
            "buildx",
            "build",
            "--platform",
            f"linux/{self.arch}",
            *self.build_args(),
            "-f",
            DOCKERFILE,
            "-t",
            self.tag,
            "--output",
            f"type=docker,dest={raw}",
            ".",
        ]
        code = _lib.run_inherit(argv, cwd=_REPO)
        if code != 0:
            raw.unlink(missing_ok=True)
            _lib.die(f"buildx 构建 linux/{self.arch} 失败（退出码 {code}）")
        self._compress(raw)
        return True

    def build_with_classic(self) -> None:
        """经典构建器路径：``docker build`` + ``docker save``（仅本机架构）。"""
        import platform

        machine = platform.machine().lower()
        native = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
            machine, machine
        )
        if native != self.arch:
            _lib.die(
                f"没有 buildx，无法交叉构建 linux/{self.arch}（本机是 {native}）；"
                "请安装 docker buildx，或只构建本机架构"
            )
        argv = ["docker", "build", *self.build_args(), "-f", DOCKERFILE, "-t", self.tag, "."]
        code = _lib.run_inherit(argv, cwd=_REPO)
        if code != 0:
            _lib.die(f"docker build 失败（退出码 {code}）")
        raw = self.output.with_suffix(".raw.tar")
        raw.unlink(missing_ok=True)
        saved = _lib.run_capture(["docker", "save", self.tag, "-o", str(raw)], timeout=3600.0)
        if saved.returncode != 0:
            _lib.die(f"docker save 失败：{(saved.stderr or '').strip()}")
        self._compress(raw)

    def _compress(self, raw: Path) -> None:
        """把原始 tar 压成 gzip（``mtime=0`` 保证可复现）。"""
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with raw.open("rb") as src, self.output.open("wb") as dst:
            with gzip.GzipFile(fileobj=dst, mode="wb", mtime=0) as gz:
                shutil.copyfileobj(src, gz)
        raw.unlink(missing_ok=True)
        size = self.output.stat().st_size / 1048576
        _lib.log(f"已生成 {self.output}（{size:.1f} MiB，tag={self.tag}）")

    def run(self) -> int:
        """执行构建；返回进程退出码。"""
        if not _lib.has_command("docker"):
            _lib.die("未找到 docker：本脚本需要 Docker 来构建镜像 tar")
        if not self.build_with_buildx():
            _lib.warn("未检测到 buildx，退回经典构建 + docker save")
            self.build_with_classic()
        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="build_image_tar.py",
        description="构建 ATRI-X 镜像并导出为可 docker load 的 gzip tar",
    )
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"), help="目标架构")
    parser.add_argument("--version", default="", help="版本号，默认读 deploy/VERSION")
    parser.add_argument("--output", required=True, help="输出 .tar.gz 路径")
    parser.add_argument("--base-image", default="", help="覆盖基础镜像（PY_BASE_IMAGE）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    version = args.version or default_version(_DEPLOY)
    return ImageTarBuilder(args.arch, version, Path(args.output), args.base_image).run()


if __name__ == "__main__":
    raise SystemExit(main())
