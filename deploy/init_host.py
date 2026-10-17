"""在宝塔 / 阿里云 / 腾讯云轻量服务器上初始化 ATRI-X 部署。

对应上游：``deploy/init-host.sh``（66 行 bash）。

用法::

    python deploy/init_host.py

幂等：第一次运行会生成 ``.env`` 并提示填写；填完再跑一次即完成建镜像与启动。

有意偏差（Python 版）：
1. 上游用 ``chown -R 10001:10001`` 改数据目录属主。Python 版只在**有权限且非
   Windows** 时尝试（Windows 没有 ``os.chown``），失败只 warn——属主修复失败
   不应该让一次成功的初始化变成失败。
2. 上游 ``${COMPOSE} build`` 用 shell 展开；Python 版走 :func:`_lib.compose_argv`，
   始终显式 ``-f docker-compose.yml``。
3. 命令示例全部从 ``atri-x ...`` 改成 ``python -m atri_x ...``。
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

TARGET_UID = 10001
TARGET_GID = 10001
#: 首次启动会生成这些配置（路径相对数据目录）。
EXPECTED_CONFIGS = (
    "config/atri-x/app.toml",
    "config/atri-x/services.toml",
    "config/atri-x/state.toml",
    "config/atri-x/atrinis.toml",
)


class InitHost:
    """初始化流程：检查 Docker → 准备数据目录 → 生成 .env → 建镜像 → 启动。"""

    def __init__(self) -> None:
        self.deploy_dir = Path(_lib.env_value("ATRIX_DEPLOY_DIR", str(_DEPLOY)))
        self.data_dir = self.deploy_dir / "data"
        self.compose_file = self.deploy_dir / "docker-compose.yml"
        self.uid = _lib.env_int("ATRIX_UID", TARGET_UID)
        self.gid = _lib.env_int("ATRIX_GID", TARGET_GID)
        self._compose: list[str] = []

    # ---------- 前置检查 ----------
    def check_docker(self) -> None:
        """确认 docker 与 compose 都可用。"""
        _lib.log("检查 Docker")
        if not _lib.has_command("docker"):
            _lib.die("未找到 docker。请先在宝塔面板【软件商店 -> Docker管理器】安装 Docker。")
        found = _lib.compose_command()
        if not found:
            _lib.die("未找到 docker compose / docker-compose。请确认已安装 Compose 插件。")
        self._compose = found
        _lib.log(f"使用：{' '.join(found)}")

    def compose_run(self, args: list[str], *, timeout: float = 1800.0, env: dict | None = None):
        """在部署目录里跑 ``<compose> -f <file> <args>``。"""
        argv = _lib.compose_argv(self._compose, self.compose_file, args)
        return _lib.run_capture(argv, cwd=self.deploy_dir, env=env, timeout=timeout)

    # ---------- 数据目录 ----------
    def prepare_data_dir(self) -> None:
        """建好数据目录结构，并尽力把属主改成容器内 UID/GID。"""
        _lib.log(f"准备数据目录 {self.data_dir}")
        for relative in ("config", "run", "logs", "cache"):
            (self.data_dir / relative).mkdir(parents=True, exist_ok=True)
        self.fix_ownership()

    def fix_ownership(self) -> None:
        """尽力改属主与权限；失败只 warn（Windows 上没有 os.chown）。"""
        chown = getattr(os, "chown", None)
        if callable(chown):
            try:
                for target in [self.data_dir, *self.data_dir.rglob("*")]:
                    chown(target, self.uid, self.gid)
            except OSError as exc:
                _lib.warn(f"chown 数据目录失败（如容器反复重启请手动执行 chown -R {self.uid}:{self.gid}）：{exc}")
        for target, mode in ((self.data_dir, 0o750), (self.data_dir / "run", 0o750)):
            try:
                os.chmod(target, mode)
            except OSError:  # pragma: no cover - Windows 上 chmod 语义有限
                continue

    # ---------- .env ----------
    def ensure_env_file(self) -> bool:
        """没有 ``.env`` 就从示例生成；返回「是否已存在（可以继续）」。"""
        env_file = self.deploy_dir / ".env"
        if env_file.is_file():
            return True
        example = self.deploy_dir / ".env.example"
        if not example.is_file():
            _lib.die(f"缺少 {example}，无法生成 .env")
        _lib.write_text(env_file, _lib.read_text(example), mode=stat.S_IRUSR | stat.S_IWUSR)
        sys.stdout.write(
            "\n".join(
                [
                    "",
                    f"已生成 {env_file}",
                    "请填写其中的 API Key / token 后，重新运行：python deploy/init_host.py",
                    "",
                ]
            )
            + "\n"
        )
        return False

    # ---------- 构建与启动 ----------
    def build(self) -> None:
        """构建镜像。"""
        _lib.log("构建镜像")
        result = self.compose_run(["build"])
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            _lib.die("docker compose build 失败")

    def start(self) -> None:
        """启动容器（``--remove-orphans`` 清掉改名后遗留的容器）。"""
        _lib.log("启动容器")
        result = self.compose_run(["up", "-d", "--remove-orphans"])
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            _lib.die("docker compose up 失败")

    def print_checklist(self) -> None:
        """打印首次启动后的核对清单（内容随 Python 版调整）。"""
        compose = " ".join(self._compose) or "docker compose"
        lines = [
            "",
            "首次启动会自动生成默认配置，位置：",
            *(f"  {self.data_dir / relative}" for relative in EXPECTED_CONFIGS),
            "",
            "首次启动后请核对：",
            "  1. services.toml 的 api_key_env 与 .env 的变量名逐个对应；",
            "  2. state.toml 的 provider/model 与 services.toml 一致，且模型名真实可用；",
            "  3. app.toml 的 CLI server / OneBot / Atrinis / 生图 / security 配置符合实际接入方式；",
            f"  4. data 目录属主为 {self.uid}:{self.gid}，且不需要 root 才能写入。",
            "",
            f"只改 TOML 配置：cd {self.deploy_dir} && {compose} restart",
            f"改 .env 后必须：cd {self.deploy_dir} && {compose} up -d --remove-orphans --force-recreate",
            f"查看日志：cd {self.deploy_dir} && {compose} logs -f --tail=200",
            "健康接口：python deploy/health_probe.py   # 另有 --ready / --healthz",
            "容器内应用：python -m atri_x doctor        # 部署验收",
            "完整验收清单见 deploy/README.md 第 12 节。",
            "",
        ]
        sys.stdout.write("\n".join(lines))

    def run(self) -> int:
        """执行初始化；返回进程退出码。"""
        self.check_docker()
        self.prepare_data_dir()
        if not self.ensure_env_file():
            return 0
        self.build()
        self.start()
        self.compose_run(["ps"], timeout=120.0)
        self.print_checklist()
        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    return argparse.ArgumentParser(
        prog="init_host.py",
        description="初始化 ATRI-X 部署（数据目录 + .env + 构建 + 启动）",
    )


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    build_parser().parse_args(argv)
    return InitHost().run()


if __name__ == "__main__":
    raise SystemExit(main())
