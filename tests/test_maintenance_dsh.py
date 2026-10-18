"""内置 DSH 内核状态目录清理（:mod:`atri_x.maintenance.dsh`）的测试。

覆盖三件事：

* 只删 ``sessions/<cwd>/<session-id>/`` 与会话缓存里的过期条目，``profiles/``、
  顶层文件、其它 ``storages/`` 内容一律保留；
* 过期判定用「目录树里最新 mtime」，新鲜会话不会被误删，空 cwd 目录会被收掉；
* ``resolve_dsh_home`` 的四种落点（配置目录默认 / 相对路径 / 绝对路径 / 无配置路径）。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from atri_x.config import AgentConfig, AgentDshConfig, AppConfig
from atri_x.maintenance.dsh import cleanup_dsh, resolve_dsh_home

from tests._maintenance_support import _AsyncCaseWithTmp


def _cutoff(days: float = 5) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


class TestCleanupDsh(_AsyncCaseWithTmp):
    def _home(self) -> str:
        return self.path("dsh-home")

    def _session(
        self, cwd: str, session_id: str, age_days: float, content: bytes = b"x" * 32
    ) -> str:
        directory = os.path.join(self._home(), "sessions", cwd, session_id)
        self.write_file(
            os.path.join(directory, "session.v4.jsonl.zstd"), content, age_days=age_days
        )
        return directory

    async def test_removes_only_expired_sessions(self) -> None:
        home = self._home()
        old = self._session("--old-ws--", "old-session", 10)
        fresh = self._session("--new-ws--", "new-session", 1)
        # 不在白名单里的东西：一律保留。
        profile = self.write_file(
            os.path.join(home, "profiles", "acp", "cordis.yml"), b"p", age_days=30
        )
        marker = self.write_file(os.path.join(home, ".anonymous-user-id"), b"id", age_days=60)

        result = await cleanup_dsh(None, home, _cutoff())

        self.assertEqual(result.sessions, 1)
        self.assertEqual(result.files, 1)
        self.assertEqual(result.bytes, 32)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(profile))
        self.assertTrue(os.path.exists(marker))
        # 旧的 cwd 桶删空后应该被收掉，新的桶因为还装着会话必须留下。
        self.assertFalse(os.path.exists(os.path.join(home, "sessions", "--old-ws--")))
        self.assertTrue(os.path.isdir(os.path.join(home, "sessions", "--new-ws--")))

    async def test_removes_empty_bucket(self) -> None:
        home = self._home()
        os.makedirs(os.path.join(home, "sessions", "--empty--"), exist_ok=True)
        await cleanup_dsh(None, home, _cutoff())
        self.assertFalse(os.path.exists(os.path.join(home, "sessions", "--empty--")))

    async def test_cleanup_cache_entries_but_keep_other_storages(self) -> None:
        home = self._home()
        stale = self.write_file(
            os.path.join(home, "storages", "session_projcache", "sessions", "old", "c.bin"),
            b"c" * 5,
            age_days=10,
        )
        fresh = self.write_file(
            os.path.join(home, "storages", "session_projcache", "sessions", "new", "c.bin"),
            b"c" * 5,
            age_days=1,
        )
        other = self.write_file(
            os.path.join(home, "storages", "runtime-state", "keep.bin"), b"k", age_days=90
        )

        result = await cleanup_dsh(None, home, _cutoff())

        self.assertEqual(result.cache_entries, 1)
        self.assertEqual(result.files, 1)
        self.assertEqual(result.bytes, 5)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(other))

    async def test_missing_or_empty_home_is_noop(self) -> None:
        for home in ("", self.path("nope"), os.path.join(self.path("file"), "x")):
            result = await cleanup_dsh(None, home, _cutoff())
            self.assertEqual((result.sessions, result.cache_entries), (0, 0))

    async def test_cancelled_ctx_raises(self) -> None:
        from atri_x.safecontext import CancelledError, with_cancel

        self._session("--ws--", "old-session", 10)
        token = with_cancel(None)
        token.cancel("test")
        with self.assertRaises(CancelledError):
            await cleanup_dsh(token, self._home(), _cutoff())

    async def test_fresh_session_with_old_empty_dir_is_kept_while_file_is_fresh(self) -> None:
        # 目录本身很老、但里面文件是新的：按「最新 mtime」判定 → 保留。
        home = self._home()
        directory = os.path.join(home, "sessions", "--ws--", "s1")
        os.makedirs(directory, exist_ok=True)
        stamp = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
        os.utime(directory, (stamp, stamp))
        self.write_file(os.path.join(directory, "session.v4.jsonl.zstd"), b"n", age_days=0)

        result = await cleanup_dsh(None, home, _cutoff())

        self.assertEqual(result.sessions, 0)
        self.assertTrue(os.path.exists(directory))


class TestResolveDshHome(_AsyncCaseWithTmp):
    def test_defaults_to_config_dir(self) -> None:
        cfg = AppConfig()
        cfg.config_path = self.path("conf", "app.toml")
        self.assertEqual(resolve_dsh_home(cfg), self.path("conf", "dsh", "home"))

    def test_relative_home_resolves_against_config_dir(self) -> None:
        cfg = AppConfig()
        cfg.config_path = self.path("conf", "app.toml")
        cfg.agent = AgentConfig(dsh=AgentDshConfig(home=os.path.join("kernel", "dsh")))
        self.assertEqual(resolve_dsh_home(cfg), self.path("conf", "kernel", "dsh"))

    def test_absolute_home_is_used_as_is(self) -> None:
        cfg = AppConfig()
        cfg.config_path = self.path("conf", "app.toml")
        absolute = self.path("elsewhere", "home")
        cfg.agent = AgentConfig(dsh=AgentDshConfig(home=absolute))
        self.assertEqual(resolve_dsh_home(cfg), os.path.abspath(absolute))

    def test_without_config_path_returns_empty(self) -> None:
        cfg = AppConfig()
        cfg.config_path = ""
        self.assertEqual(resolve_dsh_home(cfg), "")
        cfg.agent = AgentConfig(dsh=AgentDshConfig(home="relative/dsh"))
        self.assertEqual(resolve_dsh_home(cfg), "")


if __name__ == "__main__":  # pragma: no cover - 兼容直接运行
    unittest.main()
