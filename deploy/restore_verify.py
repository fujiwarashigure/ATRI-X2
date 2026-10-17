"""在隔离临时目录恢复一份 ATRI-X 备份，并验证数据库、角色素材与本地媒体
是否能一起被读取。

对应上游：``deploy/restore-verify.sh``（403 行 bash）。

该脚本**不接触生产 data 目录**，可安全地由 ``backup.py`` 调用，也可以手动对
已有备份做恢复演练。

用法::

    python deploy/restore_verify.py /path/to/atri-x-data-YYYYmmdd-HHMMSS.tar.gz

输出约定（调用方靠最后一行判断结果，**这一段是契约，不要改**）：:

    restore_verify: passed                    # 静态完整性 + 真实隔离启动都通过
    restore_verify: static_passed              # 静态完整性通过，但启动验收被关闭/跳过
    restore_verify: static_passed_with_skips   # 非严格模式静态完整性通过，但有跳过项
    restore_verify: failed                     # 失败（写 stderr，退出码 1）

有意偏差（Python 版）：
1. 上游依赖 ``sha256sum`` / ``sqlite3`` 两个外部命令，缺了就得降级或在严格模式
   下直接失败。Python 版的 :mod:`hashlib` 与 :mod:`sqlite3` 是标准库，**永远
   可用**，因此严格模式不再有“工具缺失”这种跳过项；判据因此更硬。
2. 上游靠 ``command -v cygpath`` 处理 Windows 路径，Python 版用 :func:`_lib.host_path`。
3. 上游用外部 ``python3 -c`` 内嵌脚本来解析 TOML；Python 版直接用 :mod:`tomllib`。
   配置引用检查的**判据逐条对齐**（见 :func:`check_toml_references`）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEPLOY = Path(__file__).resolve().parent
for _candidate in (str(_REPO), str(_DEPLOY)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import _lib  # noqa: E402

DB_SUFFIXES = ("*.db", "*.sqlite", "*.sqlite3")
#: 结果状态常量（与最终输出行一一对应）。
PASSED = "passed"
SKIPPED = "skipped"
FAILED = "failed"


class RestoreVerifier:
    """一次恢复验证的完整状态与流程。"""

    def __init__(self, archive: str) -> None:
        self.archive = self._absolute(archive)
        self.strict = _lib.env_flag("RESTORE_VERIFY_STRICT", True)
        self.checks = {
            "manifest": SKIPPED,
            "toml": SKIPPED,
            "database": SKIPPED,
            "media_paths": SKIPPED,
            "start": SKIPPED,
        }
        self.characters = "missing"
        self.media = "missing"
        self.failures: list[str] = []
        self.tmp_dir: Path | None = None
        self.verify_container = ""

    @staticmethod
    def _absolute(path: str) -> Path:
        """转绝对路径。

        调用方可能传相对目录（例如 ``backups``），而后续清单校验会在临时目录里
        执行，相对归档路径会失效。
        """
        return Path(path).resolve()

    # ---------- 准备工作 ----------
    def extract(self) -> Path:
        """解压到隔离临时目录，返回 ``data/`` 路径。

        ``PORTING.md`` §7.2：本机 ``tempfile.mkdtemp`` 会以 0o700 建目录、清理
        时抛 ``PermissionError``，所以这里用 :mod:`tempfile` 的 **TemporaryDirectory
        之外** 的路径策略：显式建到系统临时目录下、名字里带进程号，
        由 :meth:`cleanup` 用 ``shutil.rmtree(ignore_errors=True)`` 收尾。
        """
        base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        target = base / f"atri-x-restore-verify-{os.getpid()}"
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = target
        _lib.log(f"解压备份到隔离目录 {target}")
        try:
            _lib.safe_extract_tar(self.archive, target)
        except (_lib.SafeExtractError, _lib.ArchivedPathError) as exc:
            self.die(f"解压备份失败：{exc}")
        return target / "data"

    def cleanup(self) -> None:
        """删除临时目录与验收容器（**永不抛异常**，收尾不能掩盖真实失败）。"""
        if self.verify_container and _lib.has_command("docker"):
            _lib.run_capture(["docker", "rm", "-f", self.verify_container], timeout=60.0)
            self.verify_container = ""
        if self.tmp_dir is not None:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            self.tmp_dir = None

    def die(self, message: str) -> None:
        """把失败统一成 ``restore_verify: failed`` + 退出码 1。"""
        _lib.warn(message)
        sys.stderr.write("restore_verify: failed\n")
        raise SystemExit(1)

    # ---------- 各检查 ----------
    def check_manifest(self) -> None:
        """校验 ``<archive>.manifest`` 里的 sha256。

        严格模式下**拒绝跳过**：没有清单、清单不是 sha256sum 格式、或校验不通过
        都直接失败。上游把“缺少 sha256sum 命令”也算严格失败项；Python 版不存在
        这个前提，所以只保留“清单本身有问题”这一类。
        """
        manifest = Path(str(self.archive) + ".manifest")
        if not manifest.is_file() or manifest.stat().st_size == 0:
            if self.strict:
                self.die(
                    f"缺少备份清单 {manifest}，严格模式拒绝跳过文件完整性校验；"
                    "确认不需要时可用 RESTORE_VERIFY_STRICT=0 降级"
                )
            _lib.warn("备份没有清单，跳过文件级校验")
            return
        _lib.log(f"校验备份清单 {manifest}")
        ok, failures = _lib.verify_manifest(manifest, self.tmp_dir or Path("."))
        if not ok:
            for failure in failures[:20]:
                _lib.warn(failure)
            self.die(f"备份清单校验失败：{manifest}")
        self.checks["manifest"] = PASSED

    def check_toml_references(self, config_dir: Path, data_dir: Path) -> None:
        """解析所有 ``*.toml`` 并校验配置引用关系（判据逐条对齐上游）。

        必需项：
        - ``app.toml`` 可解析且非空；
        - ``services.toml`` 或 ``providers.toml`` 至少一个存在且非空；
        - ``app.toml`` 的 ``config_files.services``/``providers`` 指向的文件存在且非空；
        - ``config_files.state`` 不能与 app/services/providers/atrinis/tool_tags 共用同一文件。
        """
        bad: list[str] = []
        for path in sorted(config_dir.glob("*.toml")):
            try:
                with path.open("rb") as handle:
                    tomllib.load(handle)
            except (tomllib.TOMLDecodeError, OSError) as exc:
                bad.append(f"{path.name}: {exc}")

        app: dict = {}
        try:
            with (config_dir / "app.toml").open("rb") as handle:
                loaded = tomllib.load(handle)
            app = loaded if isinstance(loaded, dict) else {}
        except (tomllib.TOMLDecodeError, OSError) as exc:
            bad.append(f"app.toml: {exc}")

        config_files = app.get("config_files") or {}
        if not isinstance(config_files, dict):
            config_files = {}
        services = str(config_files.get("services") or "")
        providers = str(config_files.get("providers") or "")
        state = str(config_files.get("state") or "")

        def normalize(value: str) -> Path:
            """容器内 ``/data/...`` 映射回恢复出的 data 目录。"""
            if value == "/data":
                return Path(os.path.normpath(str(data_dir)))
            if value.startswith("/data/"):
                return Path(os.path.normpath(str(data_dir / value[len("/data/") :])))
            if os.path.isabs(value):
                return Path(os.path.normpath(value))
            return Path(os.path.normpath(str(config_dir / value)))

        service_path = normalize(services) if services else None
        provider_path = normalize(providers) if providers else None
        if services:
            if service_path is None or not service_path.is_file() or service_path.stat().st_size == 0:
                bad.append(f"config_files.services 指向的文件不存在或为空: {service_path}")
        elif providers:
            if provider_path is None or not provider_path.is_file() or provider_path.stat().st_size == 0:
                bad.append(f"config_files.providers 指向的文件不存在或为空: {provider_path}")
        else:
            default_provider = normalize("providers.toml")
            if not default_provider.is_file() or default_provider.stat().st_size == 0:
                bad.append(
                    "app.toml 未配置 config_files.services/providers，且默认 providers.toml 不存在或为空"
                )

        if state:
            shared = {"app": normalize("app.toml")}
            if services and service_path is not None:
                shared["services"] = service_path
            if providers and provider_path is not None:
                shared["providers"] = provider_path
            for key in ("atrinis", "tool_tags"):
                value = str(config_files.get(key) or "")
                if value:
                    shared[key] = normalize(value)
            state_path = normalize(state)
            for name, shared_path in shared.items():
                if state_path == shared_path:
                    bad.append(f"config_files.state 不能与 {name} 共用同一个文件: {state_path}")

        if bad:
            self.die("配置检查失败: " + "; ".join(bad))
        self.checks["toml"] = PASSED
        _lib.log("配置解析与引用检查通过")

    def check_databases(self, data_dir: Path) -> list[Path]:
        """对所有数据库做 ``PRAGMA integrity_check`` 并确认有表结构。"""
        databases = _lib.collect_files(data_dir, DB_SUFFIXES)
        if not databases:
            self.die("备份中没有任何 SQLite 数据库文件，不能视为可恢复数据")
        for database in databases:
            integrity = self._scalar(database, "PRAGMA integrity_check;")
            if integrity != "ok":
                self.die(f"SQLite 完整性校验失败：{database}：{integrity}")
            table_count = self._scalar(
                database, "SELECT COUNT(*) FROM sqlite_master WHERE type='table';"
            )
            try:
                count = int(table_count)
            except ValueError:
                self.die(f"无法读取数据库表结构：{database}：{table_count}")
            if count <= 0:
                self.die(f"数据库 {database} 中没有任何表，可能不是有效的 ATRI-X 数据库")
        _lib.log(f"SQLite integrity_check = ok（{len(databases)} 个数据库）")
        self.checks["database"] = PASSED
        return databases

    @staticmethod
    def _scalar(database: Path, sql: str) -> str:
        """用只读连接执行一条返回单值的 SQL；失败返回错误文本。"""
        try:
            conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return f"{exc}"
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.Error as exc:
            return f"{exc}"
        finally:
            conn.close()
        return "" if row is None else str(row[0])

    def check_media_paths(self, databases: list[Path], data_dir: Path) -> None:
        """确保数据库记录的本地媒体文件在备份里真实存在。

        容器内路径形如 ``/data/atri-x/media/<xx>/<rest>``，备份中的对应位置是
        ``<data_dir>/atri-x/media/...``；严格模式只接受这一种映射，非 ``/data/...``
        形式一律拒绝（上游同样如此）。
        """
        media_dir = next(
            (p for p in sorted(data_dir.rglob("media")) if p.is_dir()), None
        )
        applicable = False
        fallback = False
        for database in databases:
            has_media = self._scalar(
                database, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='media';"
            )
            if has_media != "1":
                continue
            local_count = self._scalar(
                database, "SELECT COUNT(*) FROM media WHERE backend='local';"
            )
            if local_count in {"", "0"}:
                continue
            applicable = True
            if media_dir is None:
                self.die(f"数据库 {database} 记录了 {local_count} 个本地媒体，但备份中没有 media/ 目录")
            rows = self._scalar_rows(
                database,
                "SELECT local_path FROM media WHERE backend='local' "
                "AND local_path IS NOT NULL AND local_path<>'';",
            )
            missing = 0
            for local_path in rows:
                if local_path.startswith("/data/"):
                    if not (data_dir / local_path[len("/data/") :]).is_file():
                        missing += 1
                    continue
                if self.strict:
                    self.die(
                        f"数据库 {database.name} 中有 media.local_path 不是 /data/... 形式"
                        f"（{local_path}），严格模式拒绝按文件名兜底；"
                        "确认不需要时可用 RESTORE_VERIFY_STRICT=0 降级"
                    )
                fallback = True
                if not any(p.name == Path(local_path).name for p in _lib.iter_files(media_dir)):
                    missing += 1
            if missing > 0:
                self.die(f"数据库 {database} 有 {missing} 个本地媒体文件在备份 media/ 中找不到")
            _lib.log(f"本地媒体引用检查通过：{database.name}（{local_count} 条）")

        if not applicable or not fallback:
            self.checks["media_paths"] = PASSED
        else:
            _lib.warn("部分 media.local_path 不是 /data/... 形式，媒体路径检查记为 skipped")
            self.checks["media_paths"] = SKIPPED

    @staticmethod
    def _scalar_rows(database: Path, sql: str) -> list[str]:
        """执行一条返回单列的 SQL，返回字符串列表（失败返回空列表）。"""
        try:
            conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        try:
            return [str(row[0] or "") for row in conn.execute(sql).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def check_start(self, data_dir: Path) -> None:
        """可选的隔离启动验收：真实镜像 + 恢复数据，``--network none`` 起一次。"""
        mode = _lib.env_value("RESTORE_VERIFY_START", "auto").strip().lower()
        if mode in {"1", "true", "yes", "require", "required"}:
            mode = "required"
        elif mode in {"0", "false", "no", "off"}:
            mode = "off"
        elif mode != "auto":
            self.die(f"未知 RESTORE_VERIFY_START={mode}（可用 auto / 1 / 0 / required）")
        if mode == "off":
            return

        image = _lib.env_value("RESTORE_VERIFY_IMAGE")
        service = _lib.env_value("ATRIX_SERVICE", "atri-x")
        if not image:
            inspected = _lib.run_capture(
                ["docker", "inspect", "--format", "{{.Config.Image}}", service], timeout=30.0
            )
            image = (inspected.stdout or "").strip()
        if not _lib.has_command("docker"):
            if mode == "required":
                self.die(f"RESTORE_VERIFY_START={mode} 但未找到 docker")
            _lib.warn("未找到 docker，跳过隔离启动验收")
            return
        if not image:
            if mode == "required":
                self.die(f"RESTORE_VERIFY_START={mode} 但未找到可用镜像；请设置 RESTORE_VERIFY_IMAGE")
            _lib.warn("未找到可用的 ATRI-X 镜像，跳过隔离启动验收")
            return
        if _lib.run_capture(["docker", "image", "inspect", image], timeout=60.0).returncode != 0:
            if mode == "required":
                self.die(f"RESTORE_VERIFY_START={mode} 但镜像不存在：{image}")
            _lib.warn(f"镜像不存在，跳过隔离启动验收：{image}")
            return

        self._run_isolated(image, data_dir)

    def _run_isolated(self, image: str, data_dir: Path) -> None:
        """真起一个容器跑应用，等它的 ``/ready``；这是唯一的“真实验收”环节。"""
        import time

        self.verify_container = f"atri-x-restore-verify-{os.getpid()}"
        timeout = _lib.env_int("RESTORE_VERIFY_START_TIMEOUT", 45)
        deadline = _lib.now_epoch() + timeout
        uid_gid = self._caller_uid_gid()
        _lib.log(f"启动隔离实例做恢复验收（network=none，镜像 {image}）")
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            self.verify_container,
            "--network",
            "none",
            "--user",
            uid_gid,
            "-v",
            f"{_lib.host_path(data_dir)}:/data",
            "-e",
            "XDG_CONFIG_HOME=/data/config",
            "-e",
            "XDG_DATA_HOME=/data",
            "-e",
            "XDG_RUNTIME_DIR=/data/run",
            image,
            "run",
        ]
        result = _lib.run_capture(argv, timeout=180.0)
        if result.returncode != 0:
            self.die(f"隔离实例启动失败：{image}：{(result.stderr or '').strip()}")
        while True:
            state = _lib.run_capture(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    self.verify_container,
                ],
                timeout=30.0,
            )
            status = (state.stdout or "").strip()
            if status in {"exited", "dead"}:
                self.die(f"隔离实例提前退出（{status}）")
            probe = _lib.run_capture(
                [
                    "docker",
                    "exec",
                    self.verify_container,
                    "python",
                    "-m",
                    "atri_x",
                    "--health-probe",
                    "--ready",
                ],
                timeout=30.0,
            )
            if probe.returncode == 0:
                self.checks["start"] = PASSED
                _lib.log("隔离实例 /ready 通过")
                return
            if _lib.now_epoch() >= deadline:
                self.die(f"等待隔离实例 /ready 超时（最后状态：{status or 'unknown'}）")
            time.sleep(1)

    @staticmethod
    def _caller_uid_gid() -> str:
        """用调用者 UID/GID 跑容器，避免 root 写出的临时 data 让宿主用户清不掉。"""
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        uid = getuid() if callable(getuid) else 0
        gid = getgid() if callable(getgid) else 0
        return f"{uid}:{gid}"

    # ---------- 主流程 ----------
    def run(self) -> int:
        """执行全部检查并打印结果；返回进程退出码。"""
        if not self.archive.is_file():
            self.die(f"备份文件不存在：{self.archive}")
        try:
            data_dir = self.extract()
            self.check_manifest()
            if not data_dir.is_dir():
                self.die("备份中缺少 data/ 目录，无法恢复")

            # 备份可能来自仍在运行的实例，归档里会保留旧 PID 标记。恢复环境没有
            # 对应进程；旧版本会把这个标记当成“已有服务运行”，导致隔离启动失败。
            marker = data_dir / "run" / "atri-x" / "atri-x.pid"
            if marker.is_file():
                _lib.log(f"移除恢复数据中的旧服务 PID 标记：{marker}")
                marker.unlink(missing_ok=True)

            config_dir = None
            for candidate in (data_dir / "config" / "atri-x", data_dir / "config"):
                if (candidate / "app.toml").is_file():
                    config_dir = candidate
                    break
            if config_dir is None:
                self.die("备份中找不到 config/atri-x/app.toml，配置无法恢复")
            if (config_dir / "app.toml").stat().st_size == 0:
                self.die("缺少必需的主配置 app.toml（或文件为空），恢复后无法启动")
            has_service_config = any(
                (config_dir / name).is_file() and (config_dir / name).stat().st_size > 0
                for name in ("services.toml", "providers.toml")
            )
            if not has_service_config:
                self.die("缺少必需的服务配置 services.toml 或 providers.toml（或文件为空），恢复后无法启动")
            if not (config_dir / "state.toml").is_file():
                _lib.warn("缺少配置文件 state.toml，将使用内置默认值")

            self.check_toml_references(config_dir, data_dir)

            for candidate in (config_dir / "characters", data_dir / "config" / "atri-x" / "characters"):
                if candidate.is_dir():
                    count = sum(1 for _ in _lib.iter_files(candidate))
                    self.characters = "present"
                    _lib.log(f"角色素材目录存在：{count} 个文件")
                    break
            else:
                _lib.warn("备份中没有 characters/ 角色素材目录（可能未启用角色库）")

            media_dir = next((p for p in data_dir.rglob("media") if p.is_dir()), None)
            if media_dir is not None:
                count = sum(1 for _ in _lib.iter_files(media_dir))
                self.media = "present"
                _lib.log(f"媒体目录存在：{count} 个文件")
            else:
                _lib.warn("备份中没有 media/ 目录（可能尚未导入媒体）")

            databases = self.check_databases(data_dir)
            self.check_media_paths(databases, data_dir)
            self.check_start(data_dir)

            _lib.log(
                "恢复验证结果：manifest={manifest} toml={toml} database={database} "
                "media_paths={media_paths} start={start} characters={characters} media={media}".format(
                    characters=self.characters, media=self.media, **self.checks
                )
            )
            static_ok = all(
                self.checks[key] == PASSED
                for key in ("manifest", "toml", "database", "media_paths")
            )
            if static_ok and self.checks["start"] == PASSED:
                sys.stdout.write("restore_verify: passed\n")
            elif static_ok:
                sys.stdout.write("restore_verify: static_passed\n")
            else:
                sys.stdout.write("restore_verify: static_passed_with_skips\n")
            return 0
        finally:
            self.cleanup()


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="restore_verify.py",
        description="在隔离目录恢复 ATRI-X 备份并验证可恢复性",
    )
    parser.add_argument("archive", nargs="?", default="", help="备份 tar.gz 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：返回进程退出码。"""
    _lib.console()
    args = build_parser().parse_args(argv)
    if not args.archive:
        _lib.warn("用法：python deploy/restore_verify.py /path/to/atri-x-data-*.tar.gz")
        sys.stderr.write("restore_verify: failed\n")
        return 1
    return RestoreVerifier(args.archive).run()


if __name__ == "__main__":
    raise SystemExit(main())
