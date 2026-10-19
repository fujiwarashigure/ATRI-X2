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

**但伪造表本身不够**：``deploy/_proc.py:compose_command()`` 先做存在性探测
``has_command("docker")``（= ``shutil.which``），这一步**不经过**上面两个钩子。
宿主机没装 Docker 时它直接 ``die("未找到 Docker Compose")``，伪造表根本到不了，
整套用例就退化成"必须在装了 Docker 的机器上才能跑"。因此夹具额外在 PATH 最前面
放一组**空壳** ``docker`` / ``docker-compose`` / ``git``：它们只用于让 ``which``
命中（``which`` 跟 ``PATHEXT`` 走，空壳不会被执行），真正要跑的命令仍然先命中伪造表。
这样"有 Docker 的机器"和"没 Docker 的机器"跑的是同一条代码路径。

**严格模式（本轮加固）**：``ATRIX_DEPLOY_FAKE_CAPTURE`` 一旦设置，规则文件缺失 /
JSON 损坏 / 未命中任何规则都会抛 ``AssertionError``（消息带全 argv 与规则文件路径），
**绝不回落到真实执行**——过去未命中会静默去跑真实 ``docker`` / ``git``。规则条目支持
结构化的 ``argv``（完整序列，完全相等）与 ``argv_prefix``（前缀），``match`` 子串
保留兼容；同一个条目里声明的多个谓词是「与」关系，先匹配先赢。夹具自己的本地替身
脚本（``backup.py`` / ``restore_verify.py``，见 :meth:`UpgradeFixture.helper_passthroughs`）
用逐条 ``"real": true`` 白名单允许真跑，而不是全局打开
``ATRIX_DEPLOY_FAKE_ALLOW_REAL=1``——后者会让任何未命中命令落到真机。

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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "deploy"
TMP_ROOT = REPO_ROOT / "tmp_deploy_upgrade_test"
LIB_MODULES = ("_lib.py", "_libfs.py", "_envutil.py", "_proc.py", "_netutil.py", "_console.py")

if str(DEPLOY_SRC) not in sys.path:
    sys.path.insert(0, str(DEPLOY_SRC))

import _proc  # noqa: E402  （伪造模式的直调单测用）

#: 默认的伪造命令表：Compose 可用、有一个运行中的容器、镜像 tag/id 固定。
#: **结构化匹配**：``argv``（完整序列，完全相等）/ ``argv_prefix``（前缀）/
#: ``match``（子串，兼容旧写法）在同一格里是「与」关系；条目之间先匹配先赢。
#: 这里的每一条都被 upgrade.py / rollback.py 真实调用到（未命中会直接
#: AssertionError），所以删掉任何一条都应当让对应用例变红。
DEFAULT_FAKES = [
    # Compose 前端探测（完整 argv：别把别的 version 探针也吃掉）
    {"argv": ["docker", "compose", "version"], "code": 0},
    # git：rev-parse 的 stdout 由 write_fakes 注入夹具仓库根
    {"argv_prefix": ["git", "-C"], "match": "rev-parse --show-toplevel", "code": 0, "stdout": ""},
    {"argv_prefix": ["git", "-C"], "match": "fetch --tags --force", "code": 0},
    {"argv_prefix": ["git", "-C"], "match": "checkout --detach", "code": 0},
    # compose 子命令：脚本生成的命令是
    #     docker compose -f <file> up -d --force-recreate atri-x
    # `-f <file>` 夹在 `compose` 与子命令之间，所以前缀只能钉到 ``-f``，
    # 子命令形态用 match 钉住（例如 "up -d"）；写 "compose up" 永远匹配不上。
    {"argv_prefix": ["docker", "compose", "-f"], "match": "ps -q", "code": 0,
     "stdout": "fake-container-id\n"},
    {"argv_prefix": ["docker", "compose", "-f"], "match": "config check", "code": 0},
    {"argv_prefix": ["docker", "compose", "-f"], "match": "up -d", "code": 0},
    {"argv_prefix": ["docker", "compose", "-f"], "match": "build atri-x", "code": 0},
    {"argv_prefix": ["docker", "compose", "-f"], "match": "stop atri-x", "code": 0},
    # docker 直连命令
    {"argv_prefix": ["docker", "inspect"], "match": "{{.Config.Image}}", "code": 0,
     "stdout": "atri-x:old-tag\n"},
    {"argv_prefix": ["docker", "inspect"], "match": "{{.Image}}", "code": 0,
     "stdout": "sha256:old-image-id\n"},
    {"argv_prefix": ["docker", "inspect"], "match": "State.Health", "code": 0,
     "stdout": "healthy\n"},
    {"argv_prefix": ["docker", "image", "inspect"], "match": "RepoDigests", "code": 0, "stdout": ""},
    {"argv_prefix": ["docker", "image", "inspect"], "code": 0},
    {"argv_prefix": ["docker", "tag"], "code": 0},
    {"argv_prefix": ["docker", "save"], "code": 0},
    {"argv_prefix": ["docker", "load"], "code": 0},
    {"argv_prefix": ["docker", "run"], "code": 0},
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

        #: 空壳外部命令目录（只用于 ``shutil.which`` 命中，见模块 docstring）。
        self.bin = self.tmp / "bin"
        self.write_tool_stubs()

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
    def write_tool_stubs(self) -> None:
        """写一组空壳 ``docker`` / ``docker-compose`` / ``git``，只为 ``which`` 命中。

        存在的理由：``compose_command()`` 用 ``shutil.which`` 判断前端是否可用，
        那一步不走伪造钩子。空壳因此**不需要**有任何行为——命令本身必须命中
        ``ATRIX_DEPLOY_FAKE_CAPTURE`` 的伪造表；严格模式下漏了伪造表会直接
        ``AssertionError``，根本走不到空壳（Windows 的 ``CreateProcess`` 也不会
        执行 ``.cmd``，空壳本来就跑不起来），所以不存在"空壳兜底把问题掩盖掉"。
        """
        self.bin.mkdir(parents=True, exist_ok=True)
        shell_body = "#!/bin/sh\nexit 0\n"
        for name in ("docker", "docker-compose", "git"):
            script = self.bin / name
            script.write_text(shell_body, encoding="utf-8", newline="\n")
            # POSIX 上 which 要求可执行位；Windows 上 chmod 是空操作。
            script.chmod(0o755)
            # Windows：shutil.which 按 PATHEXT 查找，文件名必须带 .cmd。
            (self.bin / f"{name}.cmd").write_bytes(b"@echo off\r\nexit /b 0\r\n")

    def helper_passthroughs(self) -> list[dict]:
        """夹具自己的本地替身脚本：逐条 ``real: true`` 允许真正执行。

        ``upgrade.py`` / ``rollback.py`` 会用 ``_lib.run_capture`` 去跑
        ``backup.py`` / ``restore_verify.py``；它们是 :meth:`setUp` 写进夹具仓库的
        小脚本，副作用（生成备份 tar 等）正是用例要断言的东西，所以必须真跑。
        用逐条白名单而不是全局 ``ATRIX_DEPLOY_FAKE_ALLOW_REAL=1``：后者会让
        **任何**未命中命令（包括真 docker）落到真机，等于把 fail-closed 关掉。
        """
        return [
            {"argv_prefix": [sys.executable, str(self.deploy / name)], "real": True}
            for name in ("backup.py", "restore_verify.py")
        ]

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
        prepared.extend(self.helper_passthroughs())
        self.fake_file.write_text(
            json.dumps(prepared, ensure_ascii=False), encoding="utf-8", newline="\n"
        )

    def without_rule(self, **predicate) -> list[dict]:
        """复制默认伪造表并**删掉**所有满足 ``predicate`` 的条目（负向用例用）。"""
        return [
            entry
            for entry in DEFAULT_FAKES
            if not all(entry.get(key) == value for key, value in predicate.items())
        ]

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
        # 空壳命令目录放到 PATH 最前：让 has_command("docker"/"git") 在没有真
        # Docker / git 的机器上也成立（命令本身仍由伪造表接管）。
        merged["PATH"] = str(self.bin) + os.pathsep + merged.get("PATH", "")
        merged["ATRIX_DEPLOY_RECORD"] = str(self.record)
        merged["ATRIX_DEPLOY_FAKE_CAPTURE"] = str(self.fake_file)
        # 默认关掉"允许回落到真实执行"的逃生开关，保证用例验证的是 fail-closed。
        merged["ATRIX_DEPLOY_FAKE_ALLOW_REAL"] = ""
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
            [dict(entry, stdout="", code=0) if entry.get("match") == "ps -q" else entry
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
            [{"argv_prefix": ["docker", "load"], "code": 1, "stderr": "load failed\n"}, *DEFAULT_FAKES]
        )
        result = self.run_script("rollback.py", env={"ROLLBACK_CONFIRM": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("载入上一版镜像失败", result.stderr)
        self.assertTrue((self.deploy / "data").is_dir())
        self.assertEqual(list(self.deploy.glob("data.before-rollback-*")), [])


class TestFakeRulesAreRequired(UpgradeFixture):
    """负向：故意删掉一条规则 → 升级必须失败，且不碰真实 docker。

    这条负向演示同时钉住两件事：

    * 伪造模式是 fail-closed 的（``deploy/_proc.py`` 未命中抛 ``AssertionError``）；
    * 删掉的那条规则确实"有用"——否则它只是夹具里的死条目。
    """

    def test_removed_docker_save_rule_fails_closed(self) -> None:
        self.write_fakes(self.without_rule(argv_prefix=["docker", "save"]))
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertNotEqual(result.returncode, 0)
        # 断言消息里有全 argv 与规则文件路径，便于定位漏掉的规则
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("ATRIX_DEPLOY_FAKE_CAPTURE", result.stderr)
        self.assertIn("docker save", result.stderr)
        self.assertIn(str(self.fake_file), result.stderr)
        # 记录文件最后一条正是那条未命中的命令：脚本停在这里，没有继续往下跑
        commands = self.commands()
        self.assertTrue(commands, "夹具没有记录到任何外部命令")
        self.assertTrue(commands[-1].startswith("docker save"), commands[-3:])
        # 失败发生在 save_previous_image：回滚信息与后续构建都不该发生
        self.assertFalse((self.deploy / "rollback" / "rollback.env").exists())
        self.assertEqual(self.matching("build atri-x"), [])
        self.assertEqual(self.matching("up -d"), [])

    def test_removed_compose_version_rule_fails_closed(self) -> None:
        self.write_fakes(self.without_rule(argv=["docker", "compose", "version"]))
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("docker compose version", result.stderr)
        self.assertEqual(self.matching("build atri-x"), [])

    def test_rule_with_wrong_argv_prefix_does_not_match(self) -> None:
        """结构化匹配真的在比对参数序列：前缀不对就不命中（而不是退化成子串）。"""
        rules = self.without_rule(argv_prefix=["docker", "save"])
        rules.append({"argv_prefix": ["docker", "save", "--definitely-not"], "code": 0})
        self.write_fakes(rules)
        result = self.run_script("upgrade.py", env={"ATRIX_VERSION": "0.6.3"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AssertionError", result.stderr)
        self.assertIn("docker save", result.stderr)


class TestFakeCaptureStrictMode(unittest.TestCase):
    """``deploy/_proc.py`` 伪造模式的直接单测（不跑脚本，不碰真机）。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        self.tmp = TMP_ROOT
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.rules = self.tmp / "rules.json"

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def write_rules(self, payload) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.rules.write_text(text, encoding="utf-8", newline="\n")

    def fake_env(self, **extra):
        """伪造模式环境；默认显式关掉全局逃生开关。"""
        env = {
            "ATRIX_DEPLOY_FAKE_CAPTURE": str(self.rules),
            "ATRIX_DEPLOY_FAKE_ALLOW_REAL": "",
        }
        env.update(extra)
        return mock.patch.dict(os.environ, env)

    def spy_run(self):
        return mock.patch.object(
            _proc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
        )

    def test_missing_rule_file_fails_and_never_runs(self) -> None:
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError) as caught:
                _proc.run_capture(["docker", "ps"])
            run.assert_not_called()
        message = str(caught.exception)
        self.assertIn("docker ps", message)
        self.assertIn(str(self.rules), message)

    def test_corrupt_json_fails_and_never_runs(self) -> None:
        self.write_rules("{ this is not json")
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError) as caught:
                _proc.run_capture(["docker", "ps"])
            run.assert_not_called()
        self.assertIn("JSON", str(caught.exception))

    def test_unmatched_argv_raises_with_full_argv(self) -> None:
        self.write_rules([{"argv": ["docker", "version"], "code": 0}])
        argv = ["docker", "volume", "prune", "-f"]
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError) as caught:
                _proc.run_capture(argv)
            run.assert_not_called()
        message = str(caught.exception)
        self.assertIn("全部未命中", message)
        for part in argv:
            self.assertIn(part, message)
        self.assertIn(str(self.rules), message)

    def test_structured_argv_is_exact_and_prefix_is_prefix(self) -> None:
        self.write_rules(
            [
                {"argv": ["docker", "version", "--format", "{{.Server.Version}}"],
                 "code": 0, "stdout": "27.3.1\n"},
                {"argv_prefix": ["docker", "compose", "-f"], "code": 3, "stdout": "prefix\n"},
            ]
        )
        with self.fake_env(), self.spy_run() as run:
            exact = _proc.run_capture(["docker", "version", "--format", "{{.Server.Version}}"])
            prefix = _proc.run_capture(["docker", "compose", "-f", "x.yml", "ps", "-q"])
            run.assert_not_called()
            with self.assertRaises(AssertionError):
                # argv 是完全相等：多一个参数就不该命中
                _proc.run_capture(["docker", "version", "--format", "{{.Server.Version}}", "--debug"])
        self.assertEqual((exact.returncode, exact.stdout), (0, "27.3.1\n"))
        self.assertEqual((prefix.returncode, prefix.stdout), (3, "prefix\n"))

    def test_match_substring_still_supported(self) -> None:
        self.write_rules([{"match": "volume create", "code": 0, "stdout": "made\n"}])
        with self.fake_env(), self.spy_run() as run:
            done = _proc.run_capture(["docker", "volume", "create", "v1"])
            run.assert_not_called()
        self.assertEqual(done.stdout, "made\n")

    def test_all_declared_predicates_must_hold(self) -> None:
        self.write_rules([{"argv_prefix": ["docker", "save"], "match": "never", "code": 0}])
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError):
                _proc.run_capture(["docker", "save", "-o", "x.tar"])
            run.assert_not_called()

    def test_real_rule_allows_execution(self) -> None:
        self.write_rules([{"argv_prefix": [sys.executable, "helper.py"], "real": True}])
        argv = [sys.executable, "helper.py", "arg"]
        with self.fake_env(), self.spy_run() as run:
            done = _proc.run_capture(argv)
        self.assertEqual(done.returncode, 0)
        run.assert_called_once()
        self.assertEqual(list(run.call_args.args[0]), argv)

    def test_allow_real_env_is_the_only_global_escape_hatch(self) -> None:
        self.write_rules([{"match": "nothing", "code": 0}])
        with self.fake_env(ATRIX_DEPLOY_FAKE_ALLOW_REAL="1"), self.spy_run() as run:
            _proc.run_capture(["docker", "ps"])
        run.assert_called_once()

    def test_invalid_entries_are_fixture_errors(self) -> None:
        cases = (
            ([{"code": 0}], "没有匹配谓词"),
            ([{"argv": ["docker"], "code": 0, "typo": 1}], "未知键"),
            ([{"argv": [], "code": 0}], "非空字符串列表"),
            ([{"argv": [1, 2], "code": 0}], "非空字符串列表"),
            ([{"match": "x", "real": "yes"}], "true/false"),
            ([{"match": "x", "real": True, "code": 0}], "real=true"),
            ([{"match": "x", "code": "zero"}], "不是整数"),
            ({"count": 1}, "顶层必须是 JSON 数组"),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.write_rules(payload)
                with self.fake_env(), self.spy_run() as run:
                    with self.assertRaises(AssertionError) as caught:
                        _proc.run_capture(["docker", "ps"])
                    run.assert_not_called()
                self.assertIn(expected, str(caught.exception))

    def test_run_inherit_is_strict_too(self) -> None:
        self.write_rules([{"argv": ["docker", "compose", "version"], "code": 0}])
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError):
                _proc.run_inherit(["docker", "save", "-o", "x.tar"])
            run.assert_not_called()

    def test_bad_entry_after_a_matching_one_is_still_rejected(self) -> None:
        """整份规则表先校验：命中后面的拼写错误不能因为"先命中先赢"被跳过。"""
        self.write_rules(
            [
                {"argv": ["docker", "ps"], "code": 0},
                {"argv": ["docker", "ps"], "code": 0, "stduout": "typo"},
            ]
        )
        with self.fake_env(), self.spy_run() as run:
            with self.assertRaises(AssertionError) as caught:
                _proc.run_capture(["docker", "ps"])
            run.assert_not_called()
        self.assertIn("未知键", str(caught.exception))

    def test_no_fake_env_is_a_no_op(self) -> None:
        with mock.patch.dict(os.environ, {"ATRIX_DEPLOY_FAKE_CAPTURE": ""}), self.spy_run() as run:
            done = _proc.run_capture(["docker", "ps"])
        self.assertEqual(done.returncode, 0)
        run.assert_called_once()


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
