"""ATRI-X 单机升级：先校验当前配置、做一致性数据快照和上一版镜像快照，
再用新镜像做配置兼容性检查，通过后重建服务。

对应上游：``deploy/upgrade.sh``（204 行 bash）。

用法::

    python deploy/upgrade.py            # 使用 deploy/VERSION 作为新版本
    ATRIX_VERSION=0.9.0 python deploy/upgrade.py

与 Python 版应用的对接差异（重要，见 ``deploy/README.md``）：
上游把容器内命令写成 ``atri-x config check`` / ``atri-x doctor``（Go 二进制名）。
Python 版镜像 ENTRYPOINT 已是 ``python -m atri_x``，因此容器内命令必须是
``python -m atri_x config check`` —— **不要再传一次 ``atri-x``**，否则会变成
``python -m atri_x atri-x config check`` 并阻断升级验收（上游踩过同形的坑）。

有意偏差（Python 版）：
1. 没有 ``go-runtime`` 构建目标，``docker compose build`` 不带 ``ATRIX_BUILD_TARGET``。
2. 数据快照调用 ``deploy/backup.py``、恢复验证调用 ``deploy/restore_verify.py``，
   两者都用 ``sys.executable`` 调起（不依赖 PATH 里的 ``python``）。
3. 上游用 ``ls -1t backups/*.tar.gz | head -1`` 找最新备份；Python 版按 mtime
   排序取最新，并且只认本脚本**刚刚**产生的那一份（比对调用前后集合），
   避免并发备份把别的归档当成自己的快照。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

ROLLBACK_TAG = "atri-x:rollback"
CONTAINER_PYTHON = ["python", "-m", "atri_x"]


class Upgrade:
    """一次升级运行的全部状态。"""

    def __init__(self) -> None:
        self.deploy_dir = Path(_lib.env_value("ATRIX_DEPLOY_DIR", str(_DEPLOY)))
        self.service = _lib.env_value("ATRIX_SERVICE", "atri-x")
        self.rollback_dir = self.deploy_dir / "rollback"
        self.compose_file = Path(
            _lib.env_value("ATRIX_COMPOSE_FILE", str(self.deploy_dir / "docker-compose.yml"))
        )
        self.requested_version = _lib.env_value("ATRIX_VERSION")
        self.source_version = ""
        self.new_version = ""
        self.image_tag = ""
        self.precheck_dir: Path | None = None
        self._compose: list[str] = []
        self.current_image = ""
        self.current_image_id = ""
        self.current_image_digest = ""
        self.backup_file = ""

    # ---------- Compose ----------
    def detect_compose(self) -> None:
        """探测 Compose 前端；找不到就退出。"""
        found = _lib.compose_command()
        if not found:
            _lib.die("未找到 Docker Compose")
        self._compose = found

    def compose_run(self, args: list[str], *, timeout: float = 600.0, env: dict | None = None):
        """在部署目录里跑 ``<compose> -f <file> <args>``。"""
        argv = _lib.compose_argv(self._compose, self.compose_file, args)
        return _lib.run_capture(argv, cwd=self.deploy_dir, env=env, timeout=timeout)

    # ---------- 源码标签 ----------
    def switch_git_ref(self) -> None:
        """``ATRIX_GIT_REF`` 存在时先切源码标签，再校验源码版本。

        顺序很重要：先切换源码，再解析未显式指定的目标版本；否则
        ``ATRIX_GIT_REF`` 指向新标签但用户没设置 ``ATRIX_VERSION`` 时，
        ``NEW_VERSION`` 仍停留在旧源码版本而被守卫拒绝。

        仓库根必须用 ``git -C <deploy_dir> rev-parse --show-toplevel`` 求出来：
        正常布局是 ``仓库/.git`` + ``仓库/deploy/upgrade.py``，用 ``deploy/.git``
        判断会失效；``rev-parse`` 同时兼容 worktree 与子模块布局。
        """
        ref = _lib.env_value("ATRIX_GIT_REF")
        if not ref:
            return
        if not _lib.has_command("git"):
            _lib.die("设置了 ATRIX_GIT_REF，但未找到 git")
        result = _lib.run_capture(
            ["git", "-C", str(self.deploy_dir), "rev-parse", "--show-toplevel"], timeout=60.0
        )
        repo_root = (result.stdout or "").strip()
        if result.returncode != 0 or not repo_root:
            _lib.die(f"设置了 ATRIX_GIT_REF，但 {self.deploy_dir} 不在 git 工作区内")
        _lib.log(f"切换源码到 {ref}")
        if _lib.run_capture(["git", "-C", repo_root, "fetch", "--tags", "--force"], timeout=600.0).returncode != 0:
            _lib.die("git fetch 失败")
        checkout = _lib.run_capture(
            ["git", "-C", repo_root, "checkout", "--detach", ref], timeout=600.0
        )
        if checkout.returncode != 0:
            _lib.die(f"git checkout {ref} 失败：{(checkout.stderr or '').strip()}")

    def resolve_versions(self) -> None:
        """解析源码版本与目标版本，并做版本一致性守卫。"""
        version_file = self.deploy_dir / "VERSION"
        self.source_version = _lib.read_text(version_file).strip()
        if not self.source_version:
            _lib.die(f"无法读取源码版本：{version_file}")
        self.new_version = self.requested_version or self.source_version
        self.image_tag = _lib.env_value("ATRIX_IMAGE", f"atri-x:{self.new_version}")
        allow_mismatch = _lib.env_flag("ATRIX_ALLOW_VERSION_MISMATCH", False)
        if self.new_version != self.source_version and not allow_mismatch:
            _lib.die(
                f"目标版本 {self.new_version} 与当前源码 deploy/VERSION={self.source_version} 不一致："
                "本脚本只构建当前工作区的代码，不会自动切换。请先 "
                f"git fetch --tags && git checkout v{self.new_version}"
                f"（或设置 ATRIX_GIT_REF=v{self.new_version}）；"
                "确实只想改版本号时设置 ATRIX_ALLOW_VERSION_MISMATCH=1。"
            )

    # ---------- 当前状态 ----------
    def inspect_current(self) -> None:
        """读当前容器 / 镜像信息；读不到就退出（不能盲升级）。"""
        listed = self.compose_run(["ps", "-q", self.service], timeout=60.0)
        container_id = (listed.stdout or "").strip()
        if not container_id:
            _lib.die(
                f"未发现运行中的 {self.service} 容器；请先启动一次，"
                "或使用 docker compose up -d 后重试"
            )
        image = _lib.run_capture(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container_id], timeout=60.0
        )
        self.current_image = (image.stdout or "").strip()
        if not self.current_image:
            _lib.die(f"无法读取当前 {self.service} 镜像标签")
        image_id = _lib.run_capture(
            ["docker", "inspect", "--format", "{{.Image}}", container_id], timeout=60.0
        )
        self.current_image_id = (image_id.stdout or "").strip()
        if not self.current_image_id:
            _lib.die(f"无法读取当前 {self.service} 镜像 ID")
        digest = _lib.run_capture(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}",
                self.current_image_id,
            ],
            timeout=60.0,
        )
        self.current_image_digest = (digest.stdout or "").strip()
        _lib.log(f"当前镜像：{self.current_image}（id={self.current_image_id}）")
        _lib.log(f"新镜像：{self.image_tag}（源码版本 {self.source_version}）")

    # ---------- 备份与快照 ----------
    def config_check_current(self) -> None:
        """升级前在**当前**容器里做配置检查（用旧版本解析旧配置）。"""
        _lib.log("升级前配置检查")
        result = self.compose_run(
            ["exec", "-T", self.service, *CONTAINER_PYTHON, "config", "check"], timeout=300.0
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        if result.returncode != 0:
            _lib.die("升级前配置检查失败")

    def data_snapshot(self) -> None:
        """调 ``backup.py`` 做升级前数据快照，并立刻用当前镜像验证可恢复。"""
        mode = _lib.env_value("UPGRADE_BACKUP_MODE", "stop")
        _lib.log(f"升级前数据快照（BACKUP_MODE={mode}）")
        before = {p.name for p in (self.deploy_dir / "backups").glob("atri-x-data-*.tar.gz")}
        env = dict(os.environ)
        env["BACKUP_MODE"] = mode
        result = _lib.run_capture(
            [sys.executable, str(self.deploy_dir / "backup.py")],
            cwd=self.deploy_dir,
            env=env,
            timeout=3600.0,
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        if result.returncode != 0:
            _lib.die("升级前数据快照失败")

        after = sorted(
            (p for p in (self.deploy_dir / "backups").glob("atri-x-data-*.tar.gz") if p.name not in before),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not after:
            _lib.die("未找到刚生成的备份文件")
        self.backup_file = str(after[0])

        _lib.log("验证备份可被当前实际镜像恢复")
        script = self.deploy_dir / "restore_verify.py"
        if not script.is_file():
            _lib.die(f"未找到 {script}")
        verify_env = dict(os.environ)
        verify_env["RESTORE_VERIFY_IMAGE"] = self.current_image_id
        verify_env["RESTORE_VERIFY_START"] = "required"
        verified = _lib.run_capture(
            [sys.executable, str(script), self.backup_file],
            cwd=self.deploy_dir,
            env=verify_env,
            timeout=1800.0,
        )
        sys.stdout.write(verified.stdout or "")
        sys.stderr.write(verified.stderr or "")
        if verified.returncode != 0:
            _lib.die(f"升级前数据快照无法被当前实际镜像 {self.current_image_id} 恢复")

    def save_previous_image(self) -> None:
        """把当前**实际镜像 ID** 打上回滚标签并存成 tar，写 ``rollback.env``。"""
        _lib.log(f"保存上一版实际镜像快照（id={self.current_image_id}）")
        self.rollback_dir.mkdir(parents=True, exist_ok=True)
        if _lib.run_capture(["docker", "tag", self.current_image_id, ROLLBACK_TAG], timeout=120.0).returncode != 0:
            _lib.die(f"无法给镜像 {self.current_image_id} 打回滚标签 {ROLLBACK_TAG}")
        tar_path = self.rollback_dir / "previous-image.tar"
        saved = _lib.run_capture(
            ["docker", "save", ROLLBACK_TAG, "-o", str(tar_path)], timeout=3600.0
        )
        if saved.returncode != 0:
            _lib.die(f"docker save 失败：{(saved.stderr or '').strip()}")

        # 按**实际 image ID** 保存，而不是按 .Config.Image 标签：标签可能在
        # 升级过程中被新构建覆盖，ID 不会。
        _lib.write_env_file(
            self.rollback_dir / "rollback.env",
            {
                "ROLLBACK_IMAGE": ROLLBACK_TAG,
                "ROLLBACK_IMAGE_TAR": str(tar_path),
                "ROLLBACK_IMAGE_ID": self.current_image_id,
                "ROLLBACK_IMAGE_DIGEST": self.current_image_digest,
                "ROLLBACK_DATA_BACKUP": self.backup_file,
                "ROLLBACK_FROM_IMAGE": self.current_image_id,
                "ROLLBACK_FROM_VERSION": self.current_image,
                "ROLLBACK_TO_VERSION": self.new_version,
            },
        )
        _lib.log(f"回滚信息已写入 {self.rollback_dir / 'rollback.env'}")

    # ---------- 构建与预检 ----------
    def build_new_image(self) -> None:
        """构建新镜像。"""
        _lib.log(f"构建新镜像 {self.image_tag}")
        env = dict(os.environ)
        env["ATRIX_VERSION"] = self.new_version
        env["ATRIX_IMAGE"] = self.image_tag
        result = self.compose_run(["build", self.service], timeout=7200.0, env=env)
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            _lib.die(f"构建新镜像失败：{self.image_tag}")

    def precheck_new_image(self) -> None:
        """用新镜像做配置兼容性检查（默认在 data 的隔离副本上跑）。"""
        _lib.log("用新镜像做配置兼容性检查（隔离数据副本）")
        isolated = _lib.env_flag("UPGRADE_PRECHECK_ISOLATED", True)
        if isolated:
            self.precheck_dir = self.deploy_dir / f".upgrade-check-{os.getpid()}"
            shutil.rmtree(self.precheck_dir, ignore_errors=True)
            (self.precheck_dir / "data").mkdir(parents=True, exist_ok=True)
            source = self.deploy_dir / "data"
            if source.is_dir():
                for item in source.iterdir():
                    target = self.precheck_dir / "data" / item.name
                    if item.is_dir():
                        shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, target)
            precheck_data = self.precheck_dir / "data"
        else:
            _lib.warn("UPGRADE_PRECHECK_ISOLATED=0：新镜像配置检查直接挂载生产 data，可能修改生产数据")
            precheck_data = self.deploy_dir / "data"

        uid_gid = self._caller_uid_gid()
        # 镜像 ENTRYPOINT 已是 `python -m atri_x`，这里只能传 "config check"；
        # 再传一次 `atri-x` 会变成 `python -m atri_x atri-x config check`。
        argv = [
            "docker",
            "run",
            "--rm",
            "--user",
            uid_gid,
            "-v",
            f"{_lib.host_path(precheck_data)}:/data",
            "-e",
            "XDG_CONFIG_HOME=/data/config",
            "-e",
            "XDG_DATA_HOME=/data",
            "-e",
            "XDG_RUNTIME_DIR=/data/run",
            self.image_tag,
            "config",
            "check",
        ]
        result = _lib.run_capture(argv, timeout=600.0)
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        if result.returncode != 0:
            _lib.die("新镜像配置检查失败，已取消升级")

    @staticmethod
    def _caller_uid_gid() -> str:
        """用调用者 UID/GID 跑容器，避免 root 写出的临时 data 让宿主用户清不掉。"""
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        return f"{getuid() if callable(getuid) else 0}:{getgid() if callable(getgid) else 0}"

    # ---------- 重建与验收 ----------
    def recreate(self) -> None:
        """用新镜像重建服务。"""
        _lib.log(f"重建 {self.service}")
        env = dict(os.environ)
        env["ATRIX_VERSION"] = self.new_version
        env["ATRIX_IMAGE"] = self.image_tag
        result = self.compose_run(
            ["up", "-d", "--force-recreate", self.service], timeout=900.0, env=env
        )
        if result.returncode != 0:
            sys.stdout.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            self.rollback_hint()
            _lib.die(f"重建 {self.service} 失败")

    def service_health(self) -> str:
        """读容器健康状态（没有 healthcheck 时读 ``State.Status``）。"""
        listed = self.compose_run(["ps", "-q", self.service], timeout=60.0)
        container = (listed.stdout or "").strip()
        if not container:
            return ""
        inspected = _lib.run_capture(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container,
            ],
            timeout=30.0,
        )
        return (inspected.stdout or "").strip()

    def wait_healthy(self) -> bool:
        """等 healthcheck 变 healthy（最多 40 次 × 3s，与上游一致）。"""
        import time

        status = ""
        for _ in range(40):
            status = self.service_health()
            if status == "healthy":
                return True
            if status in {"unhealthy", "exited"}:
                break
            time.sleep(3)
        _lib.warn(f"升级后 {self.service} 未通过健康检查（最后状态：{status or 'unknown'}）")
        return False

    def run_doctor(self) -> bool:
        """在容器里跑 ``doctor`` 验收。"""
        if _lib.env_flag("ATRIX_UPGRADE_SKIP_DOCTOR", False):
            _lib.warn("ATRIX_UPGRADE_SKIP_DOCTOR=1，跳过升级后 doctor 验收")
            return True
        args = ["--e2e"] if _lib.env_flag("ATRIX_UPGRADE_E2E", False) else ["--no-model"]
        _lib.log(f"运行部署验收：python -m atri_x doctor {' '.join(args)}")
        result = self.compose_run(
            ["exec", "-T", self.service, *CONTAINER_PYTHON, "doctor", *args], timeout=1800.0
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return result.returncode == 0

    def rollback_hint(self) -> None:
        """提示回滚命令（失败路径上一定会调用）。"""
        _lib.warn(f"如需回滚：python {self.deploy_dir / 'rollback.py'}")

    def cleanup(self) -> None:
        """删除预检用的隔离数据副本。"""
        if self.precheck_dir is not None:
            shutil.rmtree(self.precheck_dir, ignore_errors=True)
            self.precheck_dir = None

    # ---------- 主流程 ----------
    def run(self) -> int:
        """执行升级；返回进程退出码。"""
        self.detect_compose()
        self.rollback_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.switch_git_ref()
            self.resolve_versions()
            self.inspect_current()
            self.config_check_current()
            self.data_snapshot()
            self.save_previous_image()
            self.build_new_image()
            self.precheck_new_image()
            self.recreate()
            if not self.wait_healthy():
                self.rollback_hint()
                return 1
            _lib.log(f"{self.service} 健康检查通过")
            if not self.run_doctor():
                _lib.warn("升级后验收失败")
                self.rollback_hint()
                return 1
            _lib.log(f"升级完成并通过验收。回滚命令：python {self.deploy_dir / 'rollback.py'}")
            return 0
        finally:
            self.cleanup()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    return argparse.ArgumentParser(
        prog="upgrade.py",
        description="ATRI-X 单机升级（快照 → 构建 → 预检 → 重建 → 验收）",
    )


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    build_parser().parse_args(argv)
    return Upgrade().run()


if __name__ == "__main__":
    raise SystemExit(main())
