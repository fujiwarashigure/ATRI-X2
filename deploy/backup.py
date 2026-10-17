"""ATRI-X 数据备份，可在宝塔【计划任务】中每天执行。

对应上游：``deploy/backup.sh``（318 行 bash）。

按下列顺序选择一致性备份方式（``BACKUP_MODE=auto`` 时的默认行为）：

1. ``sqlite``：对所有 SQLite 数据库做**在线一致性快照**（不中断服务），
   其余文件直接归档；
2. ``stop``：短暂停止 ATRI-X 容器后打包，再自动启动并等 healthcheck 就绪；
3. ``hot``：回退到热打包，并明确警告可能不一致。

用法::

    python deploy/backup.py [备份目录]
    BACKUP_MODE=stop python deploy/backup.py [备份目录]   # 强制短暂停机备份
    BACKUP_MODE=hot  python deploy/backup.py [备份目录]   # 明确接受热打包风险

默认备份到 ``<deploy>/backups``，保留最近 14 份（``KEEP`` 可覆盖）。

有意偏差（Python 版，详见 ``deploy/README.md``）：
1. 上游用 ``sqlite3(1)`` 命令行 + ``.backup``，**宿主机没装 sqlite3 就退到停机备份**。
   Python 版用标准库 :mod:`sqlite3` 的在线备份 API，语义相同（同样的 SQLite
   一致性快照），但**不需要外部命令**，所以在精简运维机上也能走“不中断服务”的路径。
2. 上游校验清单用 ``sha256sum``，Python 版用 :mod:`hashlib`，因此清单校验不再
   依赖宿主机有没有 coreutils。
3. 上游用 ``tar --exclude`` 复制非数据库文件、用 ``tar -czf`` 打包；Python 版用
   :mod:`shutil` 与 :mod:`tarfile`，排除规则逐条对齐（见 :data:`EXCLUDE_SUFFIXES`）。
4. 归档内路径顺序由 :func:`_lib.iter_files` 的排序决定（确定性），上游
   ``find`` 的顺序不保证；清单与归档内容始终一致，恢复验证不受影响。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

#: 被当作 SQLite 数据库的文件名后缀（与上游 ``find -name`` 一致）。
DB_SUFFIXES = ("*.db", "*.sqlite", "*.sqlite3")
#: 非数据库文件复制时要排除的 SQLite 旁路文件（与上游 ``tar --exclude`` 一致）。
EXCLUDE_SUFFIXES = ("-wal", "-shm", "-journal")
DEFAULT_KEEP = 14
ARCHIVE_STEM = "atri-x-data-"


class Backup:
    """一次备份运行的全部状态。"""

    def __init__(self, backup_dir: str = "") -> None:
        self.deploy_dir = Path(_lib.env_value("ATRIX_DEPLOY_DIR", str(_DEPLOY)))
        self.data_dir = Path(_lib.env_value("ATRIX_DATA_DIR", str(self.deploy_dir / "data")))
        self.backup_dir = Path(backup_dir) if backup_dir else Path(
            _lib.env_value("ATRIX_BACKUP_DIR", str(self.deploy_dir / "backups"))
        )
        self.keep = _lib.env_int("KEEP", DEFAULT_KEEP)
        self.mode = _lib.env_value("BACKUP_MODE", "auto").lower()
        self.verify = _lib.env_flag("BACKUP_VERIFY", True)
        self.compose_file = Path(
            _lib.env_value("ATRIX_COMPOSE_FILE", str(self.deploy_dir / "docker-compose.yml"))
        )
        self.service = _lib.env_value("ATRIX_SERVICE", "atri-x")
        self.ready_timeout = _lib.env_int("BACKUP_RESTART_READY_TIMEOUT", 60)
        self.stamp = _lib.now_stamp()
        self.archive = self.backup_dir / f"{ARCHIVE_STEM}{self.stamp}.tar.gz"
        self.stage: Path | None = None
        self.stopped = False
        self.missing_media = 0
        self._compose: list[str] | None = None

    # ---------- Compose ----------
    def compose(self) -> list[str] | None:
        """缓存 Compose 前端探测结果。"""
        if self._compose is None:
            self._compose = _lib.compose_command() or []
        return self._compose or None

    def compose_run(self, args: list[str], *, timeout: float = 300.0):
        """在部署目录里跑 ``<compose> -f <file> <args>``。

        始终用显式 compose 文件、并在部署目录下执行：脚本可能从 cron 或其他
        目录被调用，不这样做会命中另一个 Compose 项目或丢失 ``.env``。
        """
        compose = self.compose()
        if not compose:
            return None
        argv = _lib.compose_argv(compose, self.compose_file, args)
        return _lib.run_capture(argv, cwd=self.deploy_dir, timeout=timeout)

    # ---------- 模式选择 ----------
    def resolve_mode(self) -> str:
        """把 ``auto`` / 非法输入归一成 ``sqlite`` / ``stop`` / ``hot``。"""
        mode = self.mode
        if mode == "auto":
            # Python 版总是具备 SQLite 在线备份能力，因此 auto 直接走不中断服务的路径。
            return "sqlite"
        if mode in {"sqlite", "stop", "hot"}:
            return mode
        _lib.die(f"未知 BACKUP_MODE={self.mode}（可用 auto / sqlite / stop / hot）")
        return ""  # pragma: no cover - die 不返回

    # ---------- SQLite 一致性快照 ----------
    @staticmethod
    def sqlite_snapshot(source: Path, target: Path) -> None:
        """用 :mod:`sqlite3` 在线备份 API 做一致性快照。

        对应上游的 ``sqlite3 <db> ".backup '<name>'"``：源库在 WAL 模式下也
        能拿到一致快照，且不阻塞写入方。``source_uri`` 用只读模式打开，
        避免备份过程意外创建 ``-wal`` 文件。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", *EXCLUDE_SUFFIXES):
            stale = Path(str(target) + suffix) if suffix else target
            if stale.exists():
                stale.unlink()
        uri = f"file:{source.as_posix()}?mode=ro"
        source_conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            target_conn = sqlite3.connect(str(target), timeout=30.0)
            try:
                source_conn.backup(target_conn)
                target_conn.commit()
            finally:
                target_conn.close()
        finally:
            source_conn.close()

    def databases(self, root: Path) -> list[Path]:
        """列出 ``root`` 下所有 SQLite 数据库文件。"""
        return _lib.collect_files(root, DB_SUFFIXES)

    def relative_to_data(self, path: Path) -> str:
        """把数据库绝对路径转成相对 ``data/`` 的 POSIX 相对路径。"""
        return path.relative_to(self.data_dir).as_posix()

    # ---------- 各模式 ----------
    def run_sqlite_mode(self) -> None:
        """先对全部 SQLite 做快照，再复制非数据库文件，最后补齐媒体引用。"""
        if self.stage is None:  # pragma: no cover - run() 总会先建 stage
            _lib.die("内部错误：暂存目录未初始化")
        staged_data = self.stage / "data"
        staged_data.mkdir(parents=True, exist_ok=True)

        # 顺序很重要：先数据库快照再复制媒体。反过来（先媒体后数据库）会让
        # 数据库引用到归档里不存在的媒体；而快照之后新导入的图片会被补齐
        # 步骤带进来。
        _lib.log("先做 SQLite 一致性快照")
        for database in self.databases(self.data_dir):
            rel = self.relative_to_data(database)
            target = staged_data / rel
            _lib.log(f"SQLite 一致性备份：{rel}")
            try:
                self.sqlite_snapshot(database, target)
            except sqlite3.Error as exc:
                _lib.die(f"SQLite 一致性备份失败：{database}：{exc}")

        _lib.log("复制快照时间点之后的非数据库文件（媒体、配置、日志等）")
        self._copy_non_databases(staged_data)

        _lib.log("按数据库里的本地媒体引用补齐文件")
        self._reconcile_media(staged_data)
        if self.missing_media > 0:
            _lib.die(
                f"在线备份不完整：{self.missing_media} 个数据库引用的本地媒体已不存在；"
                "如需可回滚快照请改用 BACKUP_MODE=stop"
            )

        # 旁路文件绝不能进归档：它们和主库不是同一时刻的快照。
        for stale in _lib.collect_files(staged_data, [f"*{suffix}" for suffix in EXCLUDE_SUFFIXES]):
            stale.unlink(missing_ok=True)

    def _copy_non_databases(self, staged_data: Path) -> None:
        """复制 ``data/`` 下所有非数据库文件（排除 WAL/SHM/journal）。"""
        import fnmatch

        for path in _lib.iter_files(self.data_dir):
            name = path.name
            if any(fnmatch.fnmatch(name, pattern) for pattern in DB_SUFFIXES):
                continue
            if any(name.endswith(suffix) for suffix in EXCLUDE_SUFFIXES):
                continue
            target = staged_data / path.relative_to(self.data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, target)
            except OSError as exc:
                _lib.die(f"复制非数据库文件失败：{path}：{exc}")

    def _reconcile_media(self, staged_data: Path) -> None:
        """按数据库里的 ``media.local_path`` 补齐缺失的本地媒体文件。

        数据库快照与媒体复制之间仍可能跨过“删除”窗口。源文件已经被清理时
        无法产出引用完整的在线备份，直接失败（与上游一致）。
        """
        for database in self.databases(staged_data):
            try:
                conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            except sqlite3.Error as exc:
                _lib.die(f"查询 media 表失败：{database}：{exc}")
            try:
                has_media = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='media';"
                ).fetchone()[0]
                if not has_media:
                    continue
                rows = conn.execute(
                    "SELECT local_path FROM media WHERE backend='local' "
                    "AND local_path IS NOT NULL AND local_path<>'';"
                ).fetchall()
            except sqlite3.Error as exc:
                _lib.die(f"读取本地媒体引用失败：{database}：{exc}")
            finally:
                conn.close()
            for (local_path,) in rows:
                text = str(local_path or "")
                if not text:
                    continue
                rel = text[len("/data/") :] if text.startswith("/data/") else text.lstrip("/")
                staged = staged_data / rel
                if staged.is_file():
                    continue
                source = self.data_dir / rel
                if source.is_file():
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, staged)
                    _lib.log(f"补齐媒体引用：{text}")
                else:
                    _lib.warn(
                        f"数据库 {database.name} 引用的本地媒体在源 data 中已不存在：{text}"
                    )
                    self.missing_media += 1

    def maybe_stop(self) -> str:
        """``stop`` 模式：尽量停容器；停不掉就退回 ``hot``。"""
        container_id = self.compose_run(["ps", "-q", self.service], timeout=60.0)
        if container_id is None or not (container_id.stdout or "").strip():
            _lib.log("未发现运行中的 ATRI-X 容器，直接按冷数据打包")
            return "stop"
        _lib.log("短暂停止 ATRI-X 容器，确保 SQLite WAL 一致")
        result = self.compose_run(["stop", self.service], timeout=180.0)
        if result is not None and result.returncode == 0:
            self.stopped = True
            return "stop"
        _lib.warn("停止容器失败，改用热打包")
        return "hot"

    # ---------- 清单 ----------
    def write_manifest(self) -> int:
        """从**归档内容本身**生成 sha256 清单，返回写入的条目数。

        清单必须描述归档里真实存在的内容，而不是仍在变化的生产 ``data`` 目录，
        否则清单和归档可能对不上。这里解压到临时目录后再计算。

        路径写成 ``data/...`` 形式（相对归档根），这样
        ``restore_verify.py`` 能在解包后的临时目录里直接按清单校验。
        """
        manifest = Path(str(self.archive) + ".manifest")
        stage = self.backup_dir / f".atri-x-manifest-{os.getpid()}"
        shutil.rmtree(stage, ignore_errors=True)
        lines: list[str] = []
        try:
            _lib.safe_extract_tar(self.archive, stage)
            entries = sorted(
                _lib.iter_files(stage),
                key=lambda p: p.relative_to(stage).as_posix(),
            )
            for path in entries:
                rel = path.relative_to(stage).as_posix()
                lines.append(f"{_lib.file_sha256(path)}  {rel}")
            _lib.write_text(manifest, "\n".join(lines) + ("\n" if lines else ""))
        except (tarfile.TarError, _lib.SafeExtractError, _lib.ArchivedPathError, OSError) as exc:
            _lib.die(f"备份清单生成失败，已中止：{self.archive}：{exc}")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        if not lines:
            _lib.warn(f"备份清单为空，归档里没有任何数据文件：{self.archive}")
        _lib.log(f"备份清单：{manifest}（{len(lines)} 个文件）")
        return len(lines)

    def prune_old(self) -> None:
        """只保留最新 ``KEEP`` 份归档（连同各自的 ``.manifest``）。"""
        _lib.log(f"清理超过 {self.keep} 份的旧备份")
        archives = sorted(
            self.backup_dir.glob(f"{ARCHIVE_STEM}*.tar.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in archives[self.keep :]:
            stale.unlink(missing_ok=True)
            Path(str(stale) + ".manifest").unlink(missing_ok=True)

    # ---------- 主流程 ----------
    def run(self) -> int:
        """执行一次备份；返回进程退出码。"""
        if not self.data_dir.is_dir():
            _lib.die(f"错误：{self.data_dir} 不存在")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        mode = self.resolve_mode()

        self.stage = self.backup_dir / f".atri-x-backup-{os.getpid()}"
        shutil.rmtree(self.stage, ignore_errors=True)
        (self.stage / "data").mkdir(parents=True, exist_ok=True)

        try:
            if mode == "sqlite":
                self.run_sqlite_mode()
                _lib.log(f"打包 {self.archive}")
                self._pack(self.stage / "data")
            elif mode == "stop":
                mode = self.maybe_stop()
                if mode == "hot":
                    _lib.warn("SQLite WAL 模式下热打包可能不一致，生产环境不建议")
                _lib.log(f"打包 {self.archive}")
                self._pack(self.data_dir, warn_hot=mode == "hot")
            else:
                _lib.warn(
                    f"热打包 {self.data_dir} -> {self.archive}（SQLite WAL 下可能不一致）"
                )
                self._pack(self.data_dir, warn_hot=True)

            if not self.archive.is_file():
                _lib.die("未生成备份文件")
            self.write_manifest()
            if self.verify and not self._run_restore_verify():
                _lib.warn(f"备份恢复验证失败：{self.archive}")
                _lib.warn("保留该备份用于排查；确认恢复流程前不要清理旧备份")
                return 1
            self.prune_old()
            _lib.log(f"完成：{self.archive}")
            return 0
        finally:
            if self.stopped:
                self._restart_container()
            if self.stage is not None:
                shutil.rmtree(self.stage, ignore_errors=True)

    def _pack(self, source: Path, *, warn_hot: bool = False) -> None:
        """把 ``data/`` 打进 ``<archive>``（归档内顶层目录名固定为 ``data``）。"""
        if warn_hot:
            _lib.warn("热打包可能在 SQLite WAL 下不一致")
        try:
            with tarfile.open(self.archive, "w:gz") as tar:
                tar.add(source, arcname="data", recursive=True)
        except (tarfile.TarError, OSError) as exc:
            _lib.die(f"打包失败：{self.archive}：{exc}")

    def _run_restore_verify(self) -> bool:
        """调用 ``deploy/restore_verify.py`` 做隔离恢复验证。

        输出**始终**转发到本进程的 stdout/stderr：调用方（cron、计划任务、升级
        脚本）靠 ``restore_verify: static_passed`` 这一行判断结果，吞掉它就等于
        把可观测性丢了。失败时返回 False，由 :meth:`run` 决定退出码。
        """
        script = self.deploy_dir / "restore_verify.py"
        if not script.is_file():
            _lib.warn("未找到 restore_verify.py，跳过恢复验证")
            return True
        _lib.log("在隔离目录中执行恢复验证")
        result = _lib.run_capture(
            [sys.executable, str(script), str(self.archive)],
            cwd=self.deploy_dir,
            timeout=1800.0,
        )
        sys.stdout.write(result.stdout or "")
        sys.stderr.write(result.stderr or "")
        return result.returncode == 0

    def _restart_container(self) -> None:
        """停机备份之后把容器拉起来，并等 healthcheck 就绪。

        “服务没有恢复”必须作为整体失败返回，否则 cron / 计划任务会把一次
        备份标记为成功，即使 ATRI-X 仍处于停止状态。
        """
        _lib.log("恢复 ATRI-X 容器")
        result = self.compose_run(["up", "-d", "--remove-orphans"], timeout=600.0)
        compose = self.compose() or ["docker", "compose"]
        if result is None or result.returncode != 0:
            _lib.warn(
                f"容器恢复失败，请手动执行：cd {self.deploy_dir} && "
                f"{' '.join(compose)} -f {self.compose_file} up -d --remove-orphans"
            )
            raise SystemExit(1)
        if not self.wait_for_container_ready():
            _lib.warn(
                f"容器已启动但未在 {self.ready_timeout}s 内就绪，请检查："
                f"cd {self.deploy_dir} && {' '.join(compose)} -f {self.compose_file} logs {self.service}"
            )
            raise SystemExit(1)

    def wait_for_container_ready(self) -> bool:
        """等 Docker healthcheck 判定就绪。

        没有 healthcheck 的服务只要求 ``running``；等待超时或 ``unhealthy``
        都返回失败（与上游 ``wait_for_container_ready`` 一致）。
        """
        import time

        deadline = _lib.now_epoch() + self.ready_timeout
        status = ""
        while True:
            listed = self.compose_run(["ps", "-q", self.service], timeout=60.0)
            container = (listed.stdout or "").strip() if listed else ""
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
                if status in {"healthy", "running"}:
                    return True
                if status in {"unhealthy", "exited", "dead", "created"}:
                    _lib.warn(f"ATRI-X 容器状态异常：{status}")
                    return False
            if _lib.now_epoch() >= deadline:
                _lib.warn(f"等待 ATRI-X 就绪超时（最后状态：{status or 'unknown'}）")
                return False
            time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="ATRI-X 数据备份（SQLite 一致性快照 / 停机 / 热打包）",
    )
    parser.add_argument("backup_dir", nargs="?", default="", help="备份输出目录，默认 <deploy>/backups")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    return Backup(args.backup_dir).run()


if __name__ == "__main__":
    raise SystemExit(main())
