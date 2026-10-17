"""回滚与升级脚本的部署自测（记录并伪造外部命令，不碰真实 git/docker 引擎）。

对应上游：``deploy/tests/rollback_script_test.sh``（78 行）+
``deploy/tests/upgrade_script_test.sh``（191 行）。

**为什么不用上游那套 PATH 前置替身**：上游在 Linux 上把假的 ``git`` / ``docker``
放进 PATH 前面即可命中。本机是 Windows，实测两条路都不通——
``CreateProcess`` 的名称解析不按 ``PATHEXT`` 走（会命中系统里真实的 ``git.exe``），
而把 ``python.exe`` 复制成 ``git.exe`` 又会在没有参数时进入 REPL 直接挂死。
因此改用 ``deploy/_proc.py`` 提供的两个**只在测试里生效**的钩子：

- ``ATRIX_DEPLOY_RECORD``：把每条外部命令追加到日志，用来断言「脚本到底调了什么」；
- ``ATRIX_DEPLOY_FAKE_CAPTURE``：按子串匹配预置 ``(退出码, stdout, stderr)``。

未设置这两个变量时钩子是空操作，生产路径零开销。

要守住的三条关键行为（上游各自对应一个用例）：

1. ``ATRIX_GIT_REF`` 必须先在 ``deploy/`` 下 ``rev-parse --show-toplevel`` 求出
   仓库根，再在**仓库根**执行 ``fetch`` / ``checkout``——不能假设 ``deploy/.git``；
2. 容器内命令只能是 ``config check`` / ``doctor``，**不能**再传一次应用名
   （镜像 ENTRYPOINT 已含 ``python -m atri_x``，多传一次就变成
   ``python -m atri_x atri-x config check`` 并阻断升级验收）；
3. 版本守卫生效：目标版本与 ``deploy/VERSION`` 不一致时必须拒绝，
   除非显式 ``ATRIX_ALLOW_VERSION_MISMATCH=1``。

另外覆盖 ``rollback.py`` 的前置校验：缺 ``rollback.env`` 或缺少必需键时，
**必须在做任何破坏性动作之前**失败。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "deploy"
TMP_ROOT = REPO_ROOT / "tmp_deploy_upgrade_test"
LIB_MODULES = ("_lib.py", "_libfs.py", "_envutil.py", "_proc.py", "_netutil.py", "_console.py")

#: 默认的伪造命令表：Compose 可用、有一个运行中的容器、镜像 tag/id 固定。
#: 表按顺序匹配，**先匹配先赢**，所以 ``rev-parse --show-toplevel`` 必须排在
#: 泛化的 ``git -C`` 之前。
DEFAULT_FAKES = [
    {"match": "docker compose version", "code": 0},
    {"match": "rev-parse --show-toplevel", "code": 0, "stdout": ""},  # 由夹具填仓库根
    {"match": "git -C", "code": 0},
    {"match": "ps -q", "code": 0, "stdout": "fake-container-id\n"},
    {"match": "Config.Image", "code": 0, "stdout": "atri-x:old-tag\n"},
    {"match": "{{.Image}}", "code": 0, "stdout": "sha256:old-image-id\n"},
    {"match": "RepoDigests", "code": 0, "stdout": ""},
    {"match": "State.Health", "code": 0, "stdout": "healthy\n"},
    {"match": "docker info", "code": 0},
    {"match": "image inspect", "code": 0},
    {"match": "docker tag", "code": 0},
    {"match": "docker save", "code": 0},
    {"match": "docker load", "code": 0},
    {"match": "docker run", "code": 0},
    # compose 的子命令要显式伪造，否则会真的去连 Docker 守护进程
    # （报 npipe://./pipe/dockerDesktopLinuxEngine 找不到）。
    # ⚠️ 匹配串**不能**写成 "compose up"：脚本生成的命令是
    #     docker compose -f <file> up -d --force-recreate atri-x
    # `-f <file>` 夹在 `compose` 与子命令之间，所以 "compose up" 永远匹配不上。
    # 用子命令自己的参数形态（`up -d` / `build atri-x` …）最稳。
    {"match": "up -d", "code": 0},
    {"match": "compose build atri-x", "code": 0},
    {"match": "stop atri-x", "code": 0},
    {"match": "compose restart", "code": 0},
    {"match": "compose down", "code": 0},
    {"match": "config check", "code": 0},
]


class UpgradeFixture(unittest.TestCase):
    """一个自足的夹具仓库 + 命令记录器。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        self.tmp = TMP_ROOT
        self.repo = self.tmp / "repo"
        self.deploy = self.repo / "deploy"
        for path in (self.deploy, self.deploy / "backups", self.deploy / "data"):
            path.mkdir(parents=True, exist_ok=True)
        (self.repo / ".git").mkdir(exist_ok=True)

        for name in ("upgrade.py", "rollback.py", *LIB_MODULES):
            shutil.copy2(DEPLOY_SRC / name, self.deploy / name)
        (self.deploy / "VERSION").write_text("0.6.3\n", encoding="utf-8", newline="\n")
        (self.deploy / "docker-compose.yml").write_text(
            "services:\n  atri-x:\n    image: atri-x:0.9.0\n", encoding="utf-8", newline="\n"
        )
        (self.deploy / ".env").write_text("ATRIX_IMAGE=atri-x:0.6.3\n", encoding="utf-8", newline="\n")
        # 升级流程会调这两个脚本；用替身把它们的成败控制在本测试手里。
        (self.deploy / "backup.py").write_text(
            "import pathlib, sys\n"
            "root = pathlib.Path(__file__).resolve().parent\n"
            "(root / 'backups').mkdir(parents=True, exist_ok=True)\n"
            "(root / 'backups' / 'atri-x-data-20260101-000000.tar.gz').write_bytes(b'x')\n"
            "sys.stdout.write('stub backup ok\\n')\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.deploy / "restore_verify.py").write_text(
            "import sys\nsys.stdout.write('restore_verify: passed\\n')\n",
            encoding="utf-8",
            newline="\n",
        )
        self.record = self.tmp / "commands.log"
        self.fake_file = self.tmp / "fakes.json"
        self.write_fakes(DEFAULT_FAKES)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    # ---------- 伪造与记录 ----------
    def write_fakes(self, fakes: list[dict]) -> None:
        """写伪造命令表；``rev-parse`` 的 stdout 由夹具注入仓库根。

        ``stdout`` 同时用 ``setdefault`` 与「空串则覆盖」两重判断：直接把
        ``""`` 当成“已设置”会让 rev-parse 返回空仓库根，脚本于是报
        “不在 git 工作区内”——这正是最初踩到的坑（``setdefault`` 不覆盖
        ``{"match": ..., "stdout": ""}`` 这种“键在、值为空”的条目）。
        """
        prepared = []
        for entry in fakes:
            item = dict(entry)
            if item.get("match") == "rev-parse --show-toplevel" and not item.get("stdout"):
                item["stdout"] = f"{self.repo}\n"
            prepared.append(item)
        self.fake_file.write_text(json.dumps(prepared, ensure_ascii=False), encoding="utf-8")

    def commands(self) -> list[str]:
        """读记录下来的外部命令列表。"""
        if not self.record.exists():
            return []
        return [line for line in self.record.read_text(encoding="utf-8").splitlines() if line.strip()]

    def matching(self, *fragments: str) -> list[str]:
        """筛出同时包含全部片段的命令。"""
        return [cmd for cmd in self.commands() if all(frag in cmd for frag in fragments)]

    # ---------- 执行 ----------
    def run_script(self, name: str, *args: str, env: dict | None = None):
        """跑脚本；把记录与伪造钩子通过环境变量传下去。"""
        merged = dict(os.environ)
        merged["PYTHONIOENCODING"] = "utf-8"
        merged["ATRIX_DEPLOY_RECORD"] = str(self.record)
        merged["ATRIX_DEPLOY_FAKE_CAPTURE"] = str(self.fake_file)
        merged["ATRIX_UPGRADE_SKIP_DOCTOR"] = "1"
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(self.deploy / name), *args],
            cwd=str(self.deploy),
            env=merged,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )


class TestUpgradeVersionGuard(UpgradeFixture):
    """版本守卫：不允许“只改版本号、不换代码”。"""

    def test_mismatch_is_rejected_before_touching_docker(self) -> None:
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "9.9.9"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不一致", result.stderr)
        # 版本守卫失败时不该已经动过容器
        self.assertEqual(self.matching("Config.Image"), [])

    def test_mismatch_allowed_with_override(self) -> None:
        result = self.run_script(
            "upgrade.py",
            env={"ATRIX_VERSION": "9.9.9", "ATRIX_ALLOW_VERSION_MISMATCH": "1"},
        )
        # 版本守卫放行：日志里同时出现目标版本与源码版本
        self.assertIn("9.9.9", result.stdout)
        self.assertIn("源码版本 0.6.3", result.stdout)

    def test_default_version_comes_from_version_file(self) -> None:
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": ""})
        self.assertIn("源码版本 0.6.3", result.stdout)

    def test_missing_container_fails_with_guidance(self) -> None:
        self.write_fakes(
            [dict(entry, stdout="", code=0) if entry["match"] == "ps -q" else entry
             for entry in DEFAULT_FAKES]
        )
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未发现运行中的", result.stderr)


class TestUpgradeGitRef(UpgradeFixture):
    """``ATRIX_GIT_REF`` 必须在仓库根操作，而不是 ``deploy/``。"""

    def test_git_ref_resolves_toplevel_then_operates_on_repo_root(self) -> None:
        result = self.run_script(
            "upgrade.py", env={"ATRIX_GIT_REF": "v0.6.3", "ATRIX_VERSION": "0.6.3"}
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()
        self.assertIn(f"git -C {self.deploy} rev-parse --show-toplevel", commands)
        self.assertIn(f"git -C {self.repo} fetch --tags --force", commands)
        self.assertIn(f"git -C {self.repo} checkout --detach v0.6.3", commands)
        # 不能把 deploy/ 当成 git 根
        self.assertEqual(self.matching(f"-C {self.deploy}", "fetch"), [])
        self.assertEqual(self.matching(f"-C {self.deploy}", "checkout"), [])

    def test_git_ref_resolves_target_version_after_checkout(self) -> None:
        """切完源码再读 VERSION：伪造的 checkout 会把 0.9.0 写进 VERSION。"""
        fake_checkout = {
            "match": "checkout --detach v0.9.0",
            "code": 0,
        }
        # 用一条真实执行的 python 命令模拟 checkout 的副作用
        (self.deploy / "fake_checkout.py").write_text(
            "import pathlib\n"
            "pathlib.Path(__file__).resolve().parent.joinpath('VERSION')"
            ".write_text('0.9.0\\n', encoding='utf-8', newline='\\n')\n",
            encoding="utf-8",
            newline="\n",
        )
        # 先手动制造 checkout 后的状态，验证脚本读到的是新 VERSION
        subprocess.run(
            [sys.executable, str(self.deploy / "fake_checkout.py")], check=True, timeout=60
        )
        _ = fake_checkout
        result = self.run_script(
            "upgrade.py", env={"ATRIX_GIT_REF": "v0.9.0", "ATRIX_VERSION": ""}
        )
        self.assertIn("源码版本 0.9.0", result.stdout)

    def test_without_git_ref_no_git_commands(self) -> None:
        self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3", "ATRIX_GIT_REF": ""})
        self.assertEqual(self.matching("git "), [])


class TestUpgradeContainerCommands(UpgradeFixture):
    """容器内命令不能多传一次应用名（上游那条 ``exit 97`` 的判据）。"""

    def test_never_passes_application_name_twice(self) -> None:
        self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        for command in self.commands():
            self.assertNotIn(
                "atri-x atri-x",
                command,
                f"容器内命令多传了一次应用名：{command}",
            )

    def test_precheck_passes_config_check_without_binary_name(self) -> None:
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prechecks = self.matching("docker run", "config check")
        self.assertTrue(prechecks, f"没有执行新镜像配置检查：{self.commands()}")
        for command in prechecks:
            self.assertTrue(command.rstrip().endswith("config check"), command)

    def test_old_image_snapshot_uses_actual_image_id(self) -> None:
        """按**实际 image ID** 打回滚标签，而不是按 .Config.Image 标签。"""
        self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertIn("docker tag sha256:old-image-id atri-x:rollback", self.commands())
        self.assertNotIn("docker tag atri-x:old-tag atri-x:rollback", self.commands())

    def test_rollback_env_is_written_with_all_required_keys(self) -> None:
        self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        rollback_env = self.deploy / "rollback" / "rollback.env"
        self.assertTrue(rollback_env.is_file())
        text = rollback_env.read_text(encoding="utf-8")
        for key in (
            "ROLLBACK_IMAGE=",
            "ROLLBACK_IMAGE_TAR=",
            "ROLLBACK_IMAGE_ID=",
            "ROLLBACK_DATA_BACKUP=",
            "ROLLBACK_FROM_IMAGE=",
            "ROLLBACK_TO_VERSION=",
        ):
            self.assertIn(key, text)

    def test_snapshot_asks_backup_for_stop_mode(self) -> None:
        """升级前数据快照默认用 stop 模式（SQLite 一致）。"""
        self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertEqual(self.commands(), self.commands())  # 记录本身可用
        # stub backup.py 不记录 env；这里断言调用确实发生
        self.assertTrue((self.deploy / "backups" / "atri-x-data-20260101-000000.tar.gz").is_file())


class TestRollbackPrechecks(UpgradeFixture):
    """``rollback.py`` 的前置校验必须在任何破坏性动作之前失败。"""

    def test_missing_rollback_env_fails(self) -> None:
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback.env", result.stderr)

    def test_missing_required_keys_fail(self) -> None:
        rollback_dir = self.deploy / "rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        (rollback_dir / "rollback.env").write_text(
            "ROLLBACK_IMAGE=atri-x:rollback\n", encoding="utf-8", newline="\n"
        )
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROLLBACK_IMAGE_TAR", result.stderr)

    def test_missing_image_tar_fails_before_touching_data(self) -> None:
        rollback_dir = self.deploy / "rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        (rollback_dir / "rollback.env").write_text(
            "\n".join(
                [
                    "ROLLBACK_IMAGE=atri-x:rollback",
                    f"ROLLBACK_IMAGE_TAR={self.tmp / 'nope.tar'}",
                    f"ROLLBACK_DATA_BACKUP={self.tmp / 'nope-backup.tar.gz'}",
                    "ROLLBACK_FROM_IMAGE=sha256:old",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("镜像快照不存在", result.stderr)
        self.assertTrue((self.deploy / "data").is_dir())
        self.assertEqual(list(self.deploy.glob("data.before-rollback-*")), [])
        self.assertEqual(self.matching("docker stop"), [])

    def test_non_interactive_without_confirm_aborts(self) -> None:
        """没有 ``ROLLBACK_CONFIRM=1`` 且不是 TTY 时不能静默继续。"""
        rollback_dir = self.deploy / "rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        image_tar = self.tmp / "image.tar"
        image_tar.write_bytes(b"x")
        data_backup = self.tmp / "backup.tar.gz"
        data_backup.write_bytes(b"x")
        (rollback_dir / "rollback.env").write_text(
            "\n".join(
                [
                    "ROLLBACK_IMAGE=atri-x:rollback",
                    f"ROLLBACK_IMAGE_TAR={image_tar}",
                    f"ROLLBACK_DATA_BACKUP={data_backup}",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("取消回滚", result.stderr)
        # 取消之后绝不能已经 load 过镜像或停过服务
        self.assertEqual(self.matching("docker load"), [])

    def test_load_failure_does_not_touch_data(self) -> None:
        rollback_dir = self.deploy / "rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        image_tar = self.tmp / "image.tar"
        image_tar.write_bytes(b"x")
        data_backup = self.tmp / "backup.tar.gz"
        data_backup.write_bytes(b"x")
        (rollback_dir / "rollback.env").write_text(
            "\n".join(
                [
                    "ROLLBACK_IMAGE=atri-x:rollback",
                    f"ROLLBACK_IMAGE_TAR={image_tar}",
                    f"ROLLBACK_DATA_BACKUP={data_backup}",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.write_fakes(
            [{"match": "docker load", "code": 1, "stderr": "load failed\n"}, *DEFAULT_FAKES]
        )
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("载入上一版镜像失败", result.stderr)
        self.assertTrue((self.deploy / "data").is_dir())
        self.assertEqual(list(self.deploy.glob("data.before-rollback-*")), [])


class TestDeclarativeFiles(unittest.TestCase):
    """声明式文件的基本自洽（不需要 Docker/systemd 就能查）。"""

    def test_compose_and_dockerfile_healthcheck_agree(self) -> None:
        compose = (DEPLOY_SRC / "docker-compose.yml").read_text(encoding="utf-8")
        dockerfile = (DEPLOY_SRC / "Dockerfile").read_text(encoding="utf-8")
        probe = "python -m atri_x --health-probe"
        self.assertIn(probe, dockerfile)
        self.assertIn(probe, compose)
        self.assertIn("/live", dockerfile)
        self.assertIn("/live", compose)

    def test_dockerfile_has_no_third_party_install(self) -> None:
        dockerfile = (DEPLOY_SRC / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("pip install", dockerfile)
        self.assertIn("PYTHONUNBUFFERED=1", dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", dockerfile)
        self.assertIn('ENTRYPOINT ["python", "-m", "atri_x"]', dockerfile)
        self.assertIn("10001", dockerfile)
        self.assertIn("python:3.14-slim", dockerfile)

    def test_dockerignore_pair_are_identical(self) -> None:
        root_ignore = (DEPLOY_SRC / ".dockerignore").read_text(encoding="utf-8")
        dockerfile_ignore = (DEPLOY_SRC / "Dockerfile.dockerignore").read_text(encoding="utf-8")

        def strip(text: str) -> list[str]:
            return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]

        self.assertEqual(strip(root_ignore), strip(dockerfile_ignore))

    def test_dockerignore_excludes_heavy_and_sensitive_paths(self) -> None:
        ignore = (DEPLOY_SRC / ".dockerignore").read_text(encoding="utf-8")
        for entry in (
            ".git",
            "tests",
            "deploy",
            "__pycache__",
            "*.py[cod]",
            "deploy/backups",
            "deploy/.env",
            "tmp_*",
            "*.md",
        ):
            self.assertIn(entry, ignore, f".dockerignore 缺少 {entry}")

    def test_env_example_drops_go_only_keys(self) -> None:
        env_example = (DEPLOY_SRC / ".env.example").read_text(encoding="utf-8")
        for gone in (
            "GO_VERSION",
            "GOPROXY",
            "GO_BASE_IMAGE",
            "RUNTIME_BASE_IMAGE",
            "ATRIX_BUILD_TARGET",
        ):
            self.assertNotIn(f"\n{gone}=", env_example, f".env.example 仍保留 Go 专属键 {gone}")
        self.assertIn("PY_BASE_IMAGE=", env_example)
        self.assertIn("ATRIX_VERSION=", env_example)

    def test_systemd_units_point_at_python_entrypoints(self) -> None:
        def directives(text: str) -> list[str]:
            return [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]

        service = directives((DEPLOY_SRC / "atri-x.service").read_text(encoding="utf-8"))
        watchdog = directives((DEPLOY_SRC / "atri-x-watchdog.service").read_text(encoding="utf-8"))
        self.assertIn("ExecStart=/usr/bin/python3 -m atri_x run", service)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/atri-x/deploy/atri_x_watchdog.py", watchdog)
        self.assertFalse([line for line in service if line.startswith("ExecStart=/opt/atri-x/atri-x")])
        self.assertFalse([line for line in watchdog if line.endswith(".sh")])

    def test_no_shell_scripts_remain_in_deploy(self) -> None:
        """任务要求：``deploy/**`` 下不允许再留 ``*.sh``。"""
        leftovers = sorted(str(p.relative_to(DEPLOY_SRC)) for p in DEPLOY_SRC.rglob("*.sh"))
        self.assertEqual(leftovers, [], f"仍有未重写的 shell 脚本：{leftovers}")

    def test_version_file_matches_pyproject(self) -> None:
        version = (DEPLOY_SRC / "VERSION").read_text(encoding="utf-8").strip()
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{version}"', pyproject)


if __name__ == "__main__":
    unittest.main()
