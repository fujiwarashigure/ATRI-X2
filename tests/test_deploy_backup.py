"""部署套件自测：``deploy/*.py`` 的备份、恢复验证、脱敏与探针。

对应上游：``deploy/tests/backup_restore_test.sh``（173 行）+
``deploy/tests/backup_restart_test.sh``（59 行）+
``deploy/tests/watchdog_redaction_test.sh``（121 行）+
``deploy/tests/restore_verify_start_test.sh`` 的启动分支。

上游用 bash + ``mktemp -d`` + ``sqlite3`` 命令行 + ``sha256sum`` 做黑盒测试。
Python 版保持**黑盒**：用 :mod:`subprocess` 跑 ``sys.executable deploy/xxx.py``，
只断言进程退出码、stdout 收尾行与产物内容，不 import 脚本内部函数——这样测试
不会因为内部重构而失效，也真实覆盖了「``python deploy/backup.py`` 从任意目录
被调起」这条运维路径。

环境约定（``PORTING.md`` §7.2）：不用 ``tempfile.mkdtemp``（本机 ACL 会让清理
抛 ``PermissionError``），统一在仓库根建 ``tmp_deploy_backup_test/``，
``tearDown`` 里 ``shutil.rmtree(ignore_errors=True)``。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "deploy"
TMP_ROOT = REPO_ROOT / "tmp_deploy_backup_test"
#: 备份/恢复脚本依赖的同级模块，必须一起拷进夹具的 deploy 目录。
LIB_MODULES = ("_lib.py", "_libfs.py", "_envutil.py", "_proc.py", "_netutil.py", "_console.py")


class DeployFixture(unittest.TestCase):
    """公共夹具：一个自足的 ``<tmp>/deploy/`` 目录 + 一份最小 data 树。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        self.tmp = TMP_ROOT
        self.deploy = self.tmp / "deploy"
        self.data = self.deploy / "data"
        self.backups = self.tmp / "backups"
        for relative in (
            "config/atri-x/characters/hero",
            "atri-x/media/ab",
            "run/atri-x",
        ):
            (self.data / relative).mkdir(parents=True, exist_ok=True)

        self.deploy.mkdir(parents=True, exist_ok=True)
        for name in ("backup.py", "restore_verify.py", "health_probe.py", "atri_x_watchdog.py", *LIB_MODULES):
            shutil.copy2(DEPLOY_SRC / name, self.deploy / name)
        (self.deploy / "docker-compose.yml").write_text(
            "services:\n  atri-x:\n    image: atri-x:0.9.0\n", encoding="utf-8", newline="\n"
        )
        self.write("config/atri-x/app.toml", 'mode = "default"\n')
        self.write("config/atri-x/providers.toml", "[providers]\n")
        self.write("config/atri-x/state.toml", "[session]\n")
        self.write("config/atri-x/characters/hero/character.toml", "hero\n")
        self.write("atri-x/media/ab/cdef0123", "media-body\n")
        self.write("run/atri-x/atri-x.pid", "999999\n")
        self.make_database(self.data / "sessions.db", media_path="/data/atri-x/media/ab/cdef0123")

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    # ---------- 夹具工具 ----------
    def write(self, relative: str, text: str) -> Path:
        """在 data 目录下写一个 UTF-8 + LF 文件。"""
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    @staticmethod
    def make_database(path: Path, *, media_path: str | None) -> None:
        """建一个带 ``media`` / ``messages`` 表的最小 SQLite 库。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE media (id TEXT, backend TEXT, local_path TEXT);")
            conn.execute("CREATE TABLE messages (id TEXT);")
            if media_path is not None:
                conn.execute(
                    "INSERT INTO media VALUES ('media:aa','local',?);", (media_path,)
                )
            conn.commit()
        finally:
            conn.close()

    def run_script(self, name: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """跑 ``python deploy/<name>``，工作目录固定在夹具仓库根。"""
        merged = dict(os.environ)
        merged["PYTHONIOENCODING"] = "utf-8"
        # 夹具里没有真实 Docker/systemd/网络依赖，全部走确定性配置。
        merged.setdefault("RESTORE_VERIFY_START", "0")
        merged.setdefault("BACKUP_VERIFY", "1")
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(self.deploy / name), *args],
            cwd=str(self.tmp),
            env=merged,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

    def latest_archive(self) -> Path:
        """取备份目录里最新的归档。"""
        archives = sorted(
            self.backups.glob("atri-x-data-*.tar.gz"), key=lambda p: p.stat().st_mtime
        )
        self.assertTrue(archives, f"没有找到备份归档：{list(self.backups.iterdir())}")
        return archives[-1]


class TestBackupHotMode(DeployFixture):
    """``BACKUP_MODE=hot``：清单与归档内容必须完全一致。"""

    def test_hot_backup_produces_manifest_matching_archive(self) -> None:
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "hot"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        archive = self.latest_archive()
        manifest = Path(str(archive) + ".manifest")
        self.assertTrue(manifest.is_file(), "没有生成 .manifest")

        from_manifest = sorted(
            line.split("  ", 1)[1].strip().lstrip("*")
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        with tarfile.open(archive) as tar:
            from_archive = sorted(
                member.name for member in tar.getmembers() if member.isfile()
            )
        self.assertEqual(from_manifest, from_archive)

    def test_hot_backup_verifies_and_reports_static_passed(self) -> None:
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "hot"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_verify: static_passed", result.stdout)

    def test_hot_backup_can_skip_verification(self) -> None:
        result = self.run_script(
            "backup.py", str(self.backups), env={"BACKUP_MODE": "hot", "BACKUP_VERIFY": "0"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("restore_verify:", result.stdout)

    def test_unknown_mode_fails(self) -> None:
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "nonsense"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未知 BACKUP_MODE", result.stderr)

    def test_missing_data_dir_fails(self) -> None:
        shutil.rmtree(self.data)
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "hot"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)


class TestBackupSqliteMode(DeployFixture):
    """``BACKUP_MODE=sqlite``：不中断服务的一致性快照 + 媒体补齐。"""

    def test_online_backup_contains_media_and_database(self) -> None:
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "sqlite"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_verify: static_passed", result.stdout)

        archive = self.latest_archive()
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertIn("data/atri-x/media/ab/cdef0123", names)
        self.assertIn("data/sessions.db", names)

    def test_online_backup_snapshot_is_readable_sqlite(self) -> None:
        """快照必须是一个能被 SQLite 打开、且内容完整的库（不只是复制文件）。"""
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "sqlite"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        extract = self.tmp / "extract-ok"
        shutil.rmtree(extract, ignore_errors=True)
        with tarfile.open(self.latest_archive()) as tar:
            tar.extractall(extract, filter="data")
        conn = sqlite3.connect(str(extract / "data" / "sessions.db"))
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            self.assertEqual(integrity, "ok")
            count = conn.execute("SELECT COUNT(*) FROM media;").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_online_backup_fails_when_database_references_missing_media(self) -> None:
        """数据库引用的本地媒体已被删除时，在线备份必须失败而不是产出残包。"""
        (self.data / "atri-x" / "media" / "ab" / "cdef0123").unlink()
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "sqlite"})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("在线备份不完整", result.stderr)

    def test_auto_mode_uses_sqlite_path_without_stopping_containers(self) -> None:
        """``auto`` 在 Python 版始终走在线快照，不尝试 docker stop。"""
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "auto"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SQLite 一致性快照", result.stdout)
        self.assertNotIn("停止 ATRI-X 容器", result.stdout)


class TestBackupPrune(DeployFixture):
    """``KEEP`` 只保留最新 N 份（连同各自的 ``.manifest``）。"""

    def test_prune_keeps_only_latest(self) -> None:
        self.backups.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            stale = self.backups / f"atri-x-data-2026010{index}-000000.tar.gz"
            stale.write_bytes(b"stale")
            Path(str(stale) + ".manifest").write_text("x\n", encoding="utf-8")

        result = self.run_script(
            "backup.py", str(self.backups), env={"BACKUP_MODE": "hot", "KEEP": "2"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        remaining = sorted(p.name for p in self.backups.glob("atri-x-data-*.tar.gz"))
        self.assertEqual(len(remaining), 2, remaining)
        for name in remaining:
            self.assertTrue((self.backups / f"{name}.manifest").is_file())


class TestRestoreVerify(DeployFixture):
    """``restore_verify.py`` 的严格模式判据与收尾状态行。"""

    def setUp(self) -> None:
        super().setUp()
        result = self.run_script("backup.py", str(self.backups), env={"BACKUP_MODE": "hot"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.archive = self.latest_archive()

    def test_strict_verification_of_fresh_backup(self) -> None:
        result = self.run_script("restore_verify.py", str(self.archive))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_verify: static_passed", result.stdout)

    def test_missing_manifest_fails_in_strict_mode(self) -> None:
        Path(str(self.archive) + ".manifest").unlink()
        result = self.run_script("restore_verify.py", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore_verify: failed", result.stderr)
        self.assertIn("缺少备份清单", result.stderr)

    def test_missing_manifest_passes_after_downgrade(self) -> None:
        Path(str(self.archive) + ".manifest").unlink()
        result = self.run_script(
            "restore_verify.py", str(self.archive), env={"RESTORE_VERIFY_STRICT": "0"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_verify: static_passed_with_skips", result.stdout)

    def test_tampered_manifest_fails(self) -> None:
        manifest = Path(str(self.archive) + ".manifest")
        tampered = manifest.read_text(encoding="utf-8") + (
            f"{'0' * 64}  data/atri-x/media/ab/cdef0123\n"
        )
        manifest.write_text(tampered, encoding="utf-8", newline="\n")
        result = self.run_script("restore_verify.py", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore_verify: failed", result.stderr)

    def test_missing_local_media_fails(self) -> None:
        broken = self.repack(lambda stage: (stage / "data/atri-x/media/ab/cdef0123").unlink())
        result = self.run_script(
            "restore_verify.py", str(broken), env={"RESTORE_VERIFY_STRICT": "0"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("本地媒体文件在备份", result.stderr)

    def test_missing_service_config_fails(self) -> None:
        broken = self.repack(
            lambda stage: (stage / "data/config/atri-x/providers.toml").unlink()
        )
        result = self.run_script(
            "restore_verify.py", str(broken), env={"RESTORE_VERIFY_STRICT": "0"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("services.toml 或 providers.toml", result.stderr)

    def test_invalid_toml_fails(self) -> None:
        broken = self.repack(
            lambda stage: (stage / "data/config/atri-x/app.toml").write_text(
                "this is [not valid toml\n", encoding="utf-8", newline="\n"
            )
        )
        result = self.run_script(
            "restore_verify.py", str(broken), env={"RESTORE_VERIFY_STRICT": "0"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("配置检查失败", result.stderr)

    def test_zero_byte_app_toml_fails(self) -> None:
        broken = self.repack(
            lambda stage: (stage / "data/config/atri-x/app.toml").write_text(
                "", encoding="utf-8", newline="\n"
            ),
            regenerate_manifest=True,
        )
        result = self.run_script("restore_verify.py", str(broken))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app.toml", result.stderr)

    def test_strict_refuses_non_data_media_paths(self) -> None:
        """``media.local_path`` 不是 ``/data/...`` 时严格模式必须拒绝。"""

        def mutate(stage: Path) -> None:
            conn = sqlite3.connect(str(stage / "data/sessions.db"))
            try:
                conn.execute(
                    "UPDATE media SET local_path='legacy/media/cdef0123' WHERE backend='local';"
                )
                conn.commit()
            finally:
                conn.close()

        broken = self.repack(mutate, regenerate_manifest=True)
        strict = self.run_script("restore_verify.py", str(broken))
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("不是 /data/... 形式", strict.stderr)

        relaxed = self.run_script(
            "restore_verify.py", str(broken), env={"RESTORE_VERIFY_STRICT": "0"}
        )
        self.assertEqual(relaxed.returncode, 0, relaxed.stdout + relaxed.stderr)
        self.assertIn("restore_verify: static_passed_with_skips", relaxed.stdout)

    def test_missing_archive_fails(self) -> None:
        result = self.run_script("restore_verify.py", str(self.tmp / "nope.tar.gz"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore_verify: failed", result.stderr)

    def test_archive_without_data_dir_fails(self) -> None:
        odd = self.tmp / "odd.tar.gz"
        with tarfile.open(odd, "w:gz") as tar:
            payload = self.tmp / "notdata.txt"
            payload.write_text("x\n", encoding="utf-8", newline="\n")
            tar.add(payload, arcname="notdata.txt")
        result = self.run_script("restore_verify.py", str(odd), env={"RESTORE_VERIFY_STRICT": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少 data/ 目录", result.stderr)

    def test_archive_with_escaping_member_is_rejected(self) -> None:
        """归档含 ``..`` 成员时必须拒绝解包（上游 ``tar`` 也会拒绝）。"""
        evil = self.tmp / "evil.tar.gz"
        payload = self.tmp / "payload.txt"
        payload.write_text("boom\n", encoding="utf-8", newline="\n")
        with tarfile.open(evil, "w:gz") as tar:
            tar.add(payload, arcname="data/../../../payload.txt")
        result = self.run_script("restore_verify.py", str(evil), env={"RESTORE_VERIFY_STRICT": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore_verify: failed", result.stderr)

    def test_start_mode_required_without_docker_fails(self) -> None:
        """``RESTORE_VERIFY_START=required`` 但镜像不可用时必须失败。"""
        result = self.run_script(
            "restore_verify.py",
            str(self.archive),
            env={
                "RESTORE_VERIFY_START": "required",
                "RESTORE_VERIFY_IMAGE": "atri-x:nonexistent-0000",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore_verify: failed", result.stderr)

    def test_start_mode_unknown_value_fails(self) -> None:
        result = self.run_script(
            "restore_verify.py", str(self.archive), env={"RESTORE_VERIFY_START": "maybe"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未知 RESTORE_VERIFY_START", result.stderr)

    def repack(self, mutate, *, regenerate_manifest: bool = False) -> Path:
        """解包 → 改内容 → 重新打包，返回新归档路径。

        ``regenerate_manifest=True`` 时同步重算清单，用来构造“清单正确但内容
        不合法”的用例（否则会被清单校验先拦下来，测不到目标分支）。
        """
        stage = self.tmp / f"stage-{abs(hash(mutate))}"
        shutil.rmtree(stage, ignore_errors=True)
        with tarfile.open(self.archive) as tar:
            tar.extractall(stage, filter="data")
        mutate(stage)
        target = self.tmp / f"repacked-{abs(hash(mutate))}.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            tar.add(stage / "data", arcname="data")
        if regenerate_manifest:
            import hashlib

            lines = []
            for path in sorted((stage / "data").rglob("*")):
                if not path.is_file():
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                lines.append(f"{digest}  data/{path.relative_to(stage / 'data').as_posix()}")
            Path(str(target) + ".manifest").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
            )
        return target


class TestHealthProbe(DeployFixture):
    """``health_probe.py``：退出码与 JSON 键名是给编排系统看的契约。"""

    def test_unreachable_endpoint_exits_one(self) -> None:
        result = self.run_script("health_probe.py", "--timeout", "1", "--quiet")
        self.assertEqual(result.returncode, 1)

    def test_json_keys_are_stable(self) -> None:
        result = self.run_script("health_probe.py", "--timeout", "1", "--json")
        self.assertEqual(result.returncode, 1)
        import json

        payload = json.loads(result.stdout)
        for key in ("url", "reachable", "status", "body", "error", "healthy"):
            self.assertIn(key, payload)
        self.assertFalse(payload["reachable"])
        self.assertEqual(payload["status"], 0)

    def test_ready_and_healthz_switch_paths(self) -> None:
        live = self.run_script("health_probe.py", "--timeout", "1", "--json")
        ready = self.run_script("health_probe.py", "--timeout", "1", "--json", "--ready")
        healthz = self.run_script("health_probe.py", "--timeout", "1", "--json", "--healthz")
        import json

        self.assertTrue(json.loads(live.stdout)["url"].endswith("/live"))
        self.assertTrue(json.loads(ready.stdout)["url"].endswith("/ready"))
        self.assertTrue(json.loads(healthz.stdout)["url"].endswith("/healthz"))

    def test_explicit_url_wins(self) -> None:
        result = self.run_script(
            "health_probe.py", "--url", "http://127.0.0.1:1/custom", "--timeout", "1", "--json"
        )
        import json

        self.assertEqual(json.loads(result.stdout)["url"], "http://127.0.0.1:1/custom")

    def test_help_exits_zero(self) -> None:
        result = self.run_script("health_probe.py", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
