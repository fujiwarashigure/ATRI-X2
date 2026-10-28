"""阿里云轻量应用服务器（Lighthouse）/ 腾讯云轻量 一键部署 ATRI-X（Python 版）。

对应上游：``deploy/pack/deploy-lighthouse.sh``（110 行 bash）。

用法（在解压后的 ``offline-<arch>/`` 目录内执行）::

    python3 deploy.py                 # 推荐：预构建镜像 + Debian/Python 运行时（功能完整）
    python3 deploy.py --load          # 直接 docker load 现成镜像 tar（完全离线）
    python3 deploy.py --load /path/to/atri-x-0.9.0-linux-amd64.tar.gz

前置：
- 已安装 Docker 和 docker compose；
- 已准备好 ``.env``（脚本会自动从 ``.env.example`` 生成并提示填写）。

上游用 ``uname -m`` 判断架构；Python 版用 :func:`platform.machine`，并额外做了
一次「包内文件名里带的是哪个架构」的交叉核对（离线包是按架构分别打的，
拿错包是常见误操作，早报错比晚报错好）。
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

_BUNDLE = Path(__file__).resolve().parent
for _candidate in (str(_BUNDLE), str(_BUNDLE.parent), str(_BUNDLE.parent.parent)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "i386": "386",
    "i686": "386",
    "aarch64": "arm64",
    "arm64": "arm64",
}
DEFAULT_VERSION = "1.1.2"
TARGET_UID = 10001
TARGET_GID = 10001


def detect_arch() -> str:
    """把 ``platform.machine()`` 归一成 amd64 / arm64。"""
    raw = platform.machine().lower()
    arch = ARCH_ALIASES.get(raw)
    if not arch:
        _lib.die(f"不支持的架构：{raw}")
    return arch


class LighthouseDeploy:
    """离线包内的一键部署流程。"""

    def __init__(self, mode: str = "build", image_tar: str = "") -> None:
        self.bundle_dir = _BUNDLE
        self.mode = mode
        self.image_tar = image_tar
        self.arch = ""
        self.version = ""
        self._compose: list[str] = []

    # ---------- 前置 ----------
    def resolve_version(self) -> None:
        """版本号优先读包内 ``VERSION``（``prepare_offline.py`` 会写进去）。"""
        raw = _lib.read_text(self.bundle_dir / "VERSION").strip()
        self.version = raw or DEFAULT_VERSION

    def check_docker(self) -> None:
        """确认 docker 守护进程在跑、compose 可用、``.env`` 已存在。"""
        if not _lib.has_command("docker"):
            _lib.die("未找到 docker，请先安装（宝塔软件商店 -> Docker管理器）")
        found = _lib.compose_command()
        if not found:
            _lib.die("未找到 docker compose / docker-compose")
        if _lib.run_capture(["docker", "info"], timeout=60.0).returncode != 0:
            _lib.die("Docker 守护进程未运行：systemctl start docker")
        self._compose = found
        _lib.log(f"Docker 可用：{' '.join(found)}")

    def ensure_env(self) -> bool:
        """没有 ``.env`` 就从示例生成并返回 False（提示填写后重跑）。"""
        env_file = self.bundle_dir / ".env"
        if env_file.is_file():
            return True
        example = self.bundle_dir / ".env.example"
        if not example.is_file():
            _lib.die(f"缺少 {example}，无法生成 .env")
        _lib.write_text(env_file, _lib.read_text(example), mode=0o600)
        _lib.warn(f"已生成 {env_file}")
        _lib.warn("请填写 DEEPSEEK_API_KEY / ATRIX_CLI_LOCAL_TOKEN / ATRINIS_HOME_TOKEN 后重新执行本脚本。")
        return False

    def prepare_data_dir(self) -> None:
        """数据目录属主改成容器内 UID/GID（bind mount 会用宿主机属主）。"""
        data = self.bundle_dir / "data"
        (data / "cache").mkdir(parents=True, exist_ok=True)
        chown = getattr(os, "chown", None)
        if callable(chown):
            try:
                for target in [data, *data.rglob("*")]:
                    chown(target, TARGET_UID, TARGET_GID)
            except OSError:
                _lib.warn(f"chown data 失败，如容器反复重启请手动执行 chown -R {TARGET_UID}:{TARGET_GID} data")
        try:
            os.chmod(data, 0o750)
        except OSError:  # pragma: no cover - Windows 上语义有限
            pass

    def compose_run(self, args: list[str], *, timeout: float = 1800.0):
        """在离线包目录里跑 ``<compose> -f docker-compose.yml <args>``。"""
        argv = _lib.compose_argv(self._compose, self.bundle_dir / "docker-compose.yml", args)
        return _lib.run_capture(argv, cwd=self.bundle_dir, timeout=timeout)

    # ---------- 获得镜像 ----------
    def find_image_tar(self) -> Path:
        """自动在包内/上级目录找对应架构的镜像 tar。"""
        name = f"atri-x-{self.version}-linux-{self.arch}.tar.gz"
        for candidate in (
            self.bundle_dir / name,
            self.bundle_dir.parent / name,
            self.bundle_dir / ".." / name,
        ):
            if candidate.is_file():
                return candidate.resolve()
        _lib.die(
            f"未找到镜像 tar，请用 --load /path/to/atri-x-{self.version}-linux-{self.arch}.tar.gz"
        )
        raise AssertionError  # pragma: no cover - die 不返回

    def acquire_image(self) -> None:
        """``--load`` 直接载入镜像 tar；否则 ``docker compose build``。"""
        if self.mode == "load":
            target = Path(self.image_tar).resolve() if self.image_tar else self.find_image_tar()
            if not target.is_file():
                _lib.die(f"镜像 tar 不存在：{target}")
            if f"linux-{self.arch}" not in target.name:
                _lib.warn(
                    f"镜像 tar 名字里的架构与当前机器（{self.arch}）不一致：{target.name}；"
                    "载入后可能无法运行"
                )
            _lib.log(f"docker load -i {target}（预构建镜像）")
            code = _lib.run_inherit(["docker", "load", "-i", str(target)], cwd=self.bundle_dir)
            if code != 0:
                _lib.die(f"docker load 失败（退出码 {code}）")
            return
        _lib.log("docker compose build（源码 + python slim 运行时）")
        result = self.compose_run(["build"])
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            _lib.die("docker compose build 失败")

    def start(self) -> None:
        """启动容器并打印结果。"""
        _lib.log("docker compose up -d")
        result = self.compose_run(["up", "-d", "--remove-orphans"])
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            _lib.die("docker compose up 失败")
        import time

        time.sleep(2)
        listed = self.compose_run(["ps"], timeout=120.0)
        sys.stdout.write(listed.stdout or "")

    def print_next_steps(self) -> None:
        """打印后续操作提示。"""
        compose = " ".join(self._compose) or "docker compose"
        lines = [
            "",
            "首次启动会在 ./data/config/atri-x/ 生成默认配置；",
            "改 TOML 后 restart，改 .env 后必须 up -d --force-recreate：",
            f"  cd {self.bundle_dir} && {compose} restart                  # 只改 TOML",
            f"  cd {self.bundle_dir} && {compose} up -d --force-recreate    # 改 .env",
            f"查看日志：cd {self.bundle_dir} && {compose} logs -f --tail=200",
            "健康检查：python3 health_probe.py        # /live；--ready 看就绪",
            "容器内验收：docker compose exec atri-x python -m atri_x doctor",
            "上线前请实际发消息、重启后确认会话仍在，并做一次备份恢复演练。",
            "",
        ]
        sys.stdout.write("\n".join(lines))

    def run(self) -> int:
        """执行部署；返回进程退出码。"""
        self.arch = detect_arch()
        _lib.log(f"服务器架构：{self.arch}")
        self.resolve_version()
        self.check_docker()
        if not self.ensure_env():
            return 1
        self.prepare_data_dir()
        self.acquire_image()
        self.start()
        self.print_next_steps()
        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="阿里云/腾讯云轻量服务器一键部署 ATRI-X（Python 版）",
    )
    parser.add_argument(
        "--load",
        nargs="?",
        const="__auto__",
        default="",
        metavar="IMAGE_TAR",
        help="直接 docker load 现成镜像 tar；不跟路径时自动查找",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    if args.load == "__auto__":
        return LighthouseDeploy(mode="load").run()
    if args.load:
        return LighthouseDeploy(mode="load", image_tar=args.load).run()
    return LighthouseDeploy(mode="build").run()


if __name__ == "__main__":
    raise SystemExit(main())
