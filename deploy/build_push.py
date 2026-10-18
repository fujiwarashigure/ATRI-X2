"""构建并推送 ATRI-X 镜像到阿里云 ACR（容器镜像服务）或腾讯云 TCR。

对应上游：``deploy/build-push.sh``（107 行 bash）。

用法::

    docker login registry.cn-hangzhou.aliyuncs.com
    python deploy/build_push.py registry.cn-hangzhou.aliyuncs.com/<命名空间>/atri-x:0.9.0

腾讯云示例::

    docker login ccr.ccs.tencentyun.com
    python deploy/build_push.py ccr.ccs.tencentyun.com/<命名空间>/atri-x:0.9.0

架构（可多选，逗号分隔）::

    PLATFORM=linux/amd64,linux/arm64 python deploy/build_push.py <image>

默认 ``linux/amd64``。多平台必须使用 buildx，且会直接 ``--push``
（buildx 不支持把多平台镜像同时 ``--load`` 到本地 ``docker images``）。

其他可用环境变量：
    ``ATRIX_VERSION``     覆盖版本号（默认读 ``deploy/VERSION``）
    ``PY_BASE_IMAGE``     覆盖基础镜像（国内加速 / pin digest）
    ``NODE_BASE_IMAGE``   内置 DSH 的 node 基础镜像（默认 ``node:24-bookworm-slim``）
    ``DSH_VERSION``       内置 DSH 版本（默认 ``0.2.0-rc.2``，需与 ACP 客户端匹配）
    ``DSH_NPM_REGISTRY``  内置 DSH 的 npm 源（国内加速）
    ``EXTRA_TOOLS``       追加系统命令（Python 版默认不装；内置 DSH 用的
                          libstdc++6 / ca-certificates 是固定的）
    ``APT_MIRROR``        仅当 ``EXTRA_TOOLS`` 非空时需要
    ``BUILDX_EXTRA_ARGS`` 追加给 buildx 的额外参数

有意偏差（Python 版）：
1. 上游把 ``GO_VERSION`` / ``GO_BASE_IMAGE`` / ``RUNTIME_BASE_IMAGE`` / ``GOPROXY``
   传给构建；Python 版没有编译阶段，这些参数全部由 ``PY_BASE_IMAGE`` 取代。
2. 上游无 buildx 时退回 ``DOCKER_BUILDKIT=1 docker build``，理由是 Dockerfile 用了
   ``RUN --mount``。Python 版 Dockerfile 不再需要 BuildKit 特性（没有依赖缓存挂载），
   但为了行为统一仍走同一分支，只是不再强制设置 ``DOCKER_BUILDKIT``。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

DEFAULT_PLATFORM = "linux/amd64"
DOCKERFILE = "deploy/Dockerfile"


def default_version(deploy_dir: Path) -> str:
    """版本号单点来源：``deploy/VERSION``；读不到就用 ``dev``。"""
    raw = _lib.read_text(deploy_dir / "VERSION").strip()
    return raw or "dev"


def build_args(deploy_dir: Path) -> list[str]:
    """拼 ``--build-arg`` 列表（空值一律不传，避免把 ``FROM`` 变成 ``:``）。"""
    version = _lib.env_value("ATRIX_VERSION") or default_version(deploy_dir)
    args = ["--build-arg", f"VERSION={version}"]
    for name in ("PY_BASE_IMAGE", "NODE_BASE_IMAGE", "DSH_VERSION", "DSH_NPM_REGISTRY", "EXTRA_TOOLS", "APT_MIRROR"):
        value = _lib.env_value(name)
        if value:
            args += ["--build-arg", f"{name}={value}"]
    return args


def split_extra_args(raw: str) -> list[str]:
    """按空白拆 ``BUILDX_EXTRA_ARGS``（对应上游的 ``extra_args=(${...})``）。"""
    return raw.split() if raw.strip() else []


class BuildPush:
    """一次构建 + 推送的全部状态。"""

    def __init__(self, image: str, platform: str = "") -> None:
        self.repo_root = Path(_lib.env_value("ATRIX_REPO_ROOT", str(_REPO)))
        self.deploy_dir = Path(_lib.env_value("ATRIX_DEPLOY_DIR", str(_DEPLOY)))
        self.image = image
        self.platform = platform or _lib.env_value("PLATFORM", DEFAULT_PLATFORM)

    def has_buildx(self) -> bool:
        """``docker buildx version`` 是否可用。"""
        return _lib.run_capture(["docker", "buildx", "version"], timeout=60.0).returncode == 0

    def run(self) -> int:
        """执行构建与推送；返回进程退出码。"""
        if not self.image:
            sys.stderr.write("用法: build_push.py <registry>/<namespace>/atri-x:<tag>\n")
            return 1
        if not _lib.has_command("docker"):
            sys.stderr.write("错误：未找到 docker\n")
            return 1

        version = _lib.env_value("ATRIX_VERSION") or default_version(self.deploy_dir)
        args = build_args(self.deploy_dir)
        extra = split_extra_args(_lib.env_value("BUILDX_EXTRA_ARGS"))
        _lib.log(f"构建 {self.image} (platform={self.platform}, version={version})")

        if self.has_buildx():
            argv = [
                "docker",
                "buildx",
                "build",
                "--platform",
                self.platform,
                *args,
                *extra,
                "-f",
                DOCKERFILE,
                "-t",
                self.image,
                "--push",
                ".",
            ]
            code = _lib.run_inherit(argv, cwd=self.repo_root)
            if code != 0:
                _lib.die(f"buildx 构建/推送失败（退出码 {code}）")
        else:
            if self.platform not in {"linux/amd64", "linux/arm64"}:
                sys.stderr.write(
                    f"错误：无 buildx 时 PLATFORM 只能是单一平台，当前为 {self.platform}\n"
                )
                sys.stderr.write("      请安装 docker buildx 插件，或改用 PLATFORM=linux/amd64\n")
                return 1
            _lib.warn("未检测到 buildx，退回经典构建（仅当前机器架构）")
            build_argv = [
                "docker",
                "build",
                *args,
                "-f",
                DOCKERFILE,
                "-t",
                self.image,
                ".",
            ]
            code = _lib.run_inherit(build_argv, cwd=self.repo_root)
            if code != 0:
                _lib.die(f"docker build 失败（退出码 {code}）")
            push_code = _lib.run_inherit(["docker", "push", self.image], cwd=self.repo_root)
            if push_code != 0:
                _lib.die(f"docker push 失败（退出码 {push_code}）")

        sys.stdout.write(
            "\n".join(
                [
                    f"==> 已推送：{self.image}",
                    f"服务器拉取：docker pull {self.image}",
                    f"服务器使用：在 deploy/.env 里设置 ATRIX_IMAGE={self.image}",
                    "",
                ]
            )
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="build_push.py",
        description="构建并推送 ATRI-X Python 版镜像到 ACR / TCR",
    )
    parser.add_argument("image", nargs="?", default="", help="<registry>/<namespace>/atri-x:<tag>")
    parser.add_argument("--platform", default="", help=f"默认 {DEFAULT_PLATFORM}，可用逗号分隔多平台")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    return BuildPush(args.image, args.platform).run()


if __name__ == "__main__":
    raise SystemExit(main())
