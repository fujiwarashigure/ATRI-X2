"""ATRI-X 单机回滚：读取 ``upgrade.py`` 写入的 ``rollback.env``，校验数据快照，
载入上一版镜像快照，恢复数据并重建服务，等待 ``/ready`` 后运行 ``doctor`` 验收。

对应上游：``deploy/rollback.sh``（215 行 bash）。

用法::

    python deploy/rollback.py
    ROLLBACK_CONFIRM=1 python deploy/rollback.py   # 跳过交互确认

失败时的**兜底原则**（与上游一致，也是这个脚本最要紧的部分）：一旦回滚过程中
任何一步失败，都要把「回滚前的 data + 回滚前的镜像」恢复回去，绝不让服务停在
一个“数据是新的、镜像是旧的”或“数据被覆盖了一半”的状态。

有意偏差（Python 版）：
1. 上游用 ``chown -R 10001:10001`` 修数据卷属主。Python 版只在**有权限且非
   Windows** 时尝试（``os.chown`` 在 Windows 上不存在），失败只 warn——属主
   修复失败不应该让一次成功的回滚变成失败。
2. 上游 ``mv`` 用 shell 语义；Python 版用 :func:`shutil.move`，跨卷时自动退化为复制。
3. ``read_env_value`` 的引号处理保留，但解析逻辑复用 ``_lib.parse_env_file``。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

TARGET_UID = 10001
TARGET_GID = 10001


class Rollback:
    """一次回滚运行的全部状态。"""

    def __init__(self) -> None:
        self.deploy_dir = Path(_lib.env_value("ATRIX_DEPLOY_DIR", str(_DEPLOY)))
        self.service = _lib.env_value("ATRIX_SERVICE", "atri-x")
        self.rollback_env = self.deploy_dir / "rollback" / "rollback.env"
        self.compose_file = Path(
            _lib.env_value("ATRIX_COMPOSE_FILE", str(self.deploy_dir / "docker-compose.yml"))
        )
        self.data_dir = self.deploy_dir / "data"
        self.ready_timeout = _lib.env_int("ROLLBACK_READY_TIMEOUT", 90)
        self.confirm = _lib.env_flag("ROLLBACK_CONFIRM", False)
        self.verify_start = _lib.env_value("ROLLBACK_VERIFY_START", "required")
        self.stamp = _lib.now_stamp()
        self.data_before = self.deploy_dir / f"data.before-rollback-{self.stamp}"
        self.data_failed = self.deploy_dir / f"data.rollback-failed-{self.stamp}"
        self.stopped = False
        self._compose: list[str] = []

        self.image = ""
        self.image_tar = ""
        self.data_backup = ""
        self.from_image = ""

    # ---------- Compose ----------
    def detect_compose(self) -> None:
        """探测 Compose 前端；找不到就退出。"""
        found = _lib.compose_command()
        if not found:
            _lib.die("未找到 Docker Compose")
        self._compose = found

    def compose_run(self, args: list[str], *, timeout: float = 300.0):
        """在部署目录里跑 ``<compose> -f <file> <args>``。"""
        argv = _lib.compose_argv(self._compose, self.compose_file, args)
        return _lib.run_capture(argv, cwd=self.deploy_dir, timeout=timeout)

    def compose_up(self, image: str, *, timeout: float = 600.0) -> bool:
        """用指定镜像重建服务（``ATRIX_IMAGE`` 覆盖 compose 里的默认值）。"""
        env = dict(os.environ)
        env["ATRIX_IMAGE"] = image
        argv = _lib.compose_argv(
            self._compose, self.compose_file, ["up", "-d", "--force-recreate", self.service]
        )
        result = _lib.run_capture(argv, cwd=self.deploy_dir, env=env, timeout=timeout)
        return result.returncode == 0

    # ---------- 就绪与验收 ----------
    def wait_ready(self) -> bool:
        """轮询容器的 ``/ready``；容器已退出则立即失败。"""
        import time

        deadline = _lib.now_epoch() + self.ready_timeout
        status = ""
        while True:
            listed = self.compose_run(["ps", "-q", self.service], timeout=60.0)
            container = (listed.stdout or "").strip()
            if container:
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
                status = (inspected.stdout or "").strip()
                if status in {"exited", "dead"}:
                    _lib.warn(f"容器已退出：{status}")
                    return False
            probe = self.compose_run(
                ["exec", "-T", self.service, "python", "-m", "atri_x", "--health-probe", "--ready"],
                timeout=60.0,
            )
            if probe.returncode == 0:
                return True
            if _lib.now_epoch() >= deadline:
                _lib.warn(f"等待 /ready 超时（最后状态：{status or 'unknown'}）")
                return False
            time.sleep(2)

    def run_doctor(self) -> bool:
        """在容器里跑 ``doctor`` 验收（默认 ``--no-model``，可用环境变量覆盖）。"""
        raw = _lib.env_value("ROLLBACK_DOCTOR_ARGS", "--no-model")
        args = raw.split() if raw else ["--no-model"]
        result = self.compose_run(
            ["exec", "-T", self.service, "python", "-m", "atri_x", "doctor", *args],
            timeout=600.0,
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return result.returncode == 0

    # ---------- 属主 ----------
    @staticmethod
    def fix_ownership(path: Path) -> None:
        """尽力把恢复出来的 data 改成容器内 UID/GID。

        Windows 上没有 ``os.chown``；非 root 时也会失败。两者都只 warn：
        属主修复失败不应该让一次成功的回滚变成失败（上游同样是 ``|| true``）。
        """
        chown = getattr(os, "chown", None)
        if not callable(chown):
            return
        try:
            for target in [path, *path.rglob("*")]:
                chown(target, TARGET_UID, TARGET_GID)
        except OSError as exc:
            _lib.warn(f"chown 数据目录失败（不影响回滚结果）：{exc}")
        try:
            os.chmod(path, 0o750)
        except OSError as exc:  # pragma: no cover - 权限受限
            _lib.warn(f"chmod 数据目录失败（不影响回滚结果）：{exc}")

    # ---------- 兜底恢复 ----------
    def restore_pre_rollback_state(self, reason: str) -> None:
        """回滚失败时的兜底：把回滚前的 data 与镜像装回去。

        这个方法**一定会退出进程**（对齐上游 ``die``）：调用方不需要再判断返回
        值，避免“兜底失败后继续往下跑”把现场搞得更乱。
        """
        _lib.warn(reason)
        if self.data_before.is_dir():
            _lib.warn(f"尝试恢复回滚前 data：{self.data_before}")
            self.compose_run(["stop", self.service], timeout=180.0)
            if self.data_dir.is_dir():
                try:
                    shutil.move(str(self.data_dir), str(self.data_failed))
                except OSError as exc:
                    _lib.warn(f"无法移走当前 data 到 {self.data_failed}：{exc}")
            try:
                shutil.move(str(self.data_before), str(self.data_dir))
            except OSError as exc:
                _lib.warn(f"回滚前 data 恢复失败：{exc}；请手动检查 {self.data_before} 和 {self.data_failed}")
                _lib.die(reason)
            self.fix_ownership(self.data_dir)
            image = self.from_image or self.image
            _lib.warn(f"用回滚前镜像重建服务：{image}")
            if self.compose_up(image):
                if self.wait_ready():
                    self.stopped = False
                    _lib.warn(f"已恢复回滚前 data 和镜像 {image}；失败的恢复数据保留在 {self.data_failed}")
                else:
                    _lib.warn("已恢复回滚前 data，但服务未通过 /ready；请手动检查")
            else:
                _lib.warn(f"回滚前镜像重建失败；请手动检查 {self.data_dir} 与 {self.data_failed}")
        else:
            _lib.warn("没有可用的回滚前 data 快照；请手动检查现场")
        _lib.die(reason)

    # ---------- 主流程 ----------
    def load_rollback_env(self) -> None:
        """读 ``rollback.env`` 并校验必需键。"""
        if not self.rollback_env.is_file():
            _lib.die(f"未找到 {self.rollback_env}；请先执行 upgrade.py")
        values = _lib.parse_env_file(self.rollback_env)
        self.image = values.get("ROLLBACK_IMAGE", "")
        self.image_tar = values.get("ROLLBACK_IMAGE_TAR", "")
        self.data_backup = values.get("ROLLBACK_DATA_BACKUP", "")
        self.from_image = values.get("ROLLBACK_FROM_IMAGE", "")
        for name, value in (
            ("ROLLBACK_IMAGE", self.image),
            ("ROLLBACK_IMAGE_TAR", self.image_tar),
            ("ROLLBACK_DATA_BACKUP", self.data_backup),
        ):
            if not value:
                _lib.die(f"{self.rollback_env} 缺少必需的 {name}")

    def precheck(self) -> None:
        """检查所有前置文件都在，再动手做任何破坏性操作。"""
        self.detect_compose()
        if not self.compose_file.is_file():
            _lib.die(f"Compose 文件不存在：{self.compose_file}")
        if not Path(self.image_tar).is_file():
            _lib.die(f"镜像快照不存在：{self.image_tar}")
        if not Path(self.data_backup).is_file():
            _lib.die(f"数据快照不存在：{self.data_backup}")
        if not self.data_dir.is_dir():
            _lib.die(f"数据目录不存在：{self.data_dir}")

    def confirm_or_abort(self) -> None:
        """交互确认（``ROLLBACK_CONFIRM=1`` 时跳过）。"""
        if self.confirm:
            return
        prompt = (
            f"将回滚到 {self.image}，并用 {self.data_backup} 覆盖当前 data。输入 yes 继续："
        )
        if not sys.stdin.isatty():
            _lib.die("非交互环境且未设置 ROLLBACK_CONFIRM=1，已取消回滚")
        sys.stdout.write(prompt)
        sys.stdout.flush()
        answer = sys.stdin.readline().strip()
        if answer != "yes":
            _lib.die("已取消回滚")

    def load_image(self) -> None:
        """``docker load`` 上一版镜像快照，并确认镜像标签真的进来了。"""
        _lib.log(f"载入上一版镜像快照 {self.image_tar}")
        result = _lib.run_capture(["docker", "load", "-i", self.image_tar], timeout=1800.0)
        if result.returncode != 0:
            _lib.die(f"载入上一版镜像失败：{self.image_tar}：{(result.stderr or '').strip()}")
        if _lib.run_capture(["docker", "image", "inspect", self.image], timeout=60.0).returncode != 0:
            _lib.die(f"镜像快照中没有找到 {self.image}")

    def verify_snapshot(self) -> bool:
        """用旧镜像验证数据快照；失败时**不动**当前服务。"""
        _lib.log(f"用旧镜像验证数据快照：image={self.image} start={self.verify_start}")
        script = self.deploy_dir / "restore_verify.py"
        if not script.is_file():
            _lib.die(f"未找到 {script}，无法验证数据快照")
        env = dict(os.environ)
        env["RESTORE_VERIFY_IMAGE"] = self.image
        env["RESTORE_VERIFY_START"] = self.verify_start
        result = _lib.run_capture(
            [sys.executable, str(script), self.data_backup],
            cwd=self.deploy_dir,
            env=env,
            timeout=1800.0,
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return result.returncode == 0

    def swap_data(self) -> None:
        """把当前 data 挪走，解包数据快照到原位。"""
        _lib.log(f"恢复数据快照，回滚前 data 保留在 {self.data_before}")
        try:
            shutil.move(str(self.data_dir), str(self.data_before))
        except OSError as exc:
            _lib.die(f"无法移走当前 data：{exc}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 归档在解包前先校验成员：数据快照来自生产环境，不能无条件信任
            # （`tar.extractall` 默认比 GNU tar 宽松）。解包目标是部署目录本身，
            # 因为归档顶层就是 `data/`。
            with tarfile.open(self.data_backup, "r:gz") as tar:
                for member in tar.getmembers():
                    name = member.name.replace("\\", "/")
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise tarfile.TarError(f"归档包含非法成员路径：{member.name}")
                tar.extractall(self.deploy_dir, filter="data")
        except (tarfile.TarError, OSError) as exc:
            self.restore_pre_rollback_state(f"解包数据快照失败：{exc}")
        # 备份里可能保留旧运行的 PID 标记；回滚到尚未使用 flock 的旧版本时，
        # 这个标记会阻止旧镜像启动。恢复出的环境不应携带旧进程标记。
        (self.data_dir / "run" / "atri-x" / "atri-x.pid").unlink(missing_ok=True)
        self.fix_ownership(self.data_dir)

    def run(self) -> int:
        """执行回滚；返回进程退出码。"""
        self.load_rollback_env()
        self.precheck()
        self.confirm_or_abort()
        self.load_image()

        if not self.verify_snapshot():
            _lib.warn(
                "数据快照验证失败；不会停止或覆盖当前服务。如果这是 0.6.1 之前生成、"
                "没有 .manifest 的旧备份，可用 RESTORE_VERIFY_STRICT=0 重试"
            )
            return 1

        _lib.log(f"停止 {self.service}")
        self.compose_run(["stop", self.service], timeout=180.0)
        self.stopped = True

        self.swap_data()

        _lib.log(f"使用上一版镜像重建 {self.service}：{self.image}")
        if not self.compose_up(self.image):
            self.restore_pre_rollback_state("使用上一版镜像重建失败")
        self.stopped = False

        _lib.log(f"等待 {self.service} 通过 /ready")
        if not self.wait_ready():
            self.stopped = True
            self.restore_pre_rollback_state("回滚后服务未通过 /ready")

        _lib.log("运行回滚后 doctor 验收")
        if not self.run_doctor():
            self.stopped = True
            self.restore_pre_rollback_state("回滚后 doctor 验收失败")

        _lib.log(
            f"回滚完成：{self.image}，数据备份已恢复；回滚前 data 保留在 {self.data_before}"
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    return argparse.ArgumentParser(
        prog="rollback.py",
        description="ATRI-X 单机回滚（读取 rollback/rollback.env）",
    )


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    build_parser().parse_args(argv)
    return Rollback().run()


if __name__ == "__main__":
    raise SystemExit(main())
