"""``packaging`` 的可离线验证部分：精准可选依赖开关 + spec 契约 + 构建环境探测。

**不跑 PyInstaller、不跑冻结产物**（评审与 Lead 都明确要求）。这里钉住的是
"静默降级会长什么样"：

* ``--with-pillow`` 只留 Pillow，不再连带 numpy/PyYAML（旧 ``--with-pillow`` 的
  三包全留语义改名为 ``--with-all-optional``）；
* ``excludes`` 的计算与 ``packaging/atri-x.spec`` 的逐包判定同源（真的执行 spec 的
  那段代码取它的取值）；spec 被回退成"三包绑死"旧版时，精准开关必须**失败**；
* 构建环境 ``import PIL`` 不可用时 ``--with-pillow`` 必须失败，只有显式
  ``--allow-missing-pillow`` 才降级成警告；
* 落给 PyInstaller 的环境变量与日志里的 excludes 一致（``--check-env`` 与
  ``run_build`` 两条路径）。

逻辑在 ``packaging/optional_deps.py``，CLI 与打印在 ``packaging/build.py``。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))

import build  # noqa: E402 - 需要先修 sys.path
import optional_deps  # noqa: E402 - 需要先修 sys.path

WORK_ROOT = REPO_ROOT / "tests" / "_tmp_packaging_build"

#: 旧的（三包绑死）spec 片段：用来证明精准开关会失败而不是静默降级。
OLD_SPEC_TEXT = 'KEEP_OPTIONAL = os.environ.get("ATRIX_PKG_KEEP_OPTIONAL") == "1"\n'
OLD_SPEC_TEXT += 'excludes = [] if KEEP_OPTIONAL else ["numpy", "PIL", "yaml"]\n'

MISSING_PILLOW = {
    "backend": "stdlib",
    "version": "",
    "pillow_installed": False,
    "reason": "ModuleNotFoundError: No module named 'PIL'",
}


def _run_cli(argv):
    """跑 ``build.main``，返回 ``(退出码, stdout, stderr)``。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build.main(argv)
    return code, out.getvalue(), err.getvalue()


def _problems(argv, spec_path):
    """``argv`` → ``(计划, 问题列表)``（走 build.py 与 CLI 完全相同的那条路径）。"""
    args = build.parse_args(argv)
    return optional_deps.plan_and_problems(args, spec_path)


class OptionalPlanTest(unittest.TestCase):
    """开关 → excludes：精准性是本任务的核心。"""

    def test_default_excludes_all_three(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args([]))
        self.assertEqual(plan["excludes"], ["numpy", "PIL", "yaml"])
        self.assertFalse(plan["keep_pillow"])
        self.assertFalse(plan["legacy_all"])

    def test_with_pillow_keeps_pillow_only(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args(["--with-pillow"]))
        self.assertTrue(plan["keep_pillow"])
        self.assertNotIn("PIL", plan["excludes"])
        # 旧语义的连带行为必须消失：numpy/PyYAML 仍然排除。
        self.assertEqual(plan["excludes"], ["numpy", "yaml"])

    def test_with_extras_keeps_extras_only(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args(["--with-extras"]))
        self.assertEqual(plan["excludes"], ["PIL"])
        self.assertFalse(plan["keep_pillow"])

    def test_both_precise_flags_keep_everything(self) -> None:
        plan = optional_deps.plan_from_args(
            build.parse_args(["--with-pillow", "--with-extras"])
        )
        self.assertEqual(plan["excludes"], [])

    def test_legacy_flag_is_the_old_all_optional_semantics(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args(["--with-all-optional"]))
        self.assertEqual(plan["excludes"], [])
        self.assertTrue(plan["keep_pillow"])
        self.assertTrue(plan["keep_extras"])
        self.assertTrue(plan["legacy_all"])

    def test_env_overrides_match_the_plan(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args(["--with-pillow"]))
        overrides = optional_deps.env_overrides(plan)
        self.assertEqual(overrides[optional_deps.KEEP_PILLOW_ENV], "1")
        self.assertEqual(overrides[optional_deps.KEEP_EXTRAS_ENV], "0")
        # 旧变量必须关掉，否则 spec 会退回"三包全留"。
        self.assertEqual(overrides[optional_deps.KEEP_OPTIONAL_ENV], "0")

    def test_plan_detail_mentions_excludes(self) -> None:
        plan = optional_deps.plan_from_args(build.parse_args(["--with-pillow"]))
        detail = optional_deps.plan_detail(plan)
        self.assertIn("Pillow=keep", detail)
        self.assertIn("numpy, yaml", detail)


class SpecContractTest(unittest.TestCase):
    """spec 契约：精准开关只对认识它们的 spec 生效。"""

    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        cls.old_spec = WORK_ROOT / "old.spec"
        cls.old_spec.write_text(OLD_SPEC_TEXT, encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)

    def _spec_excludes(self, **env_overrides: str) -> list[str]:
        """真的执行 spec 里 ``excludes`` 那段（剥掉 PyInstaller 依赖），取它的取值。

        只执行到 ``a = Analysis(`` 之前，并在 ``TZDATA-BLOCK`` 标记处截断（那里会真的
        收集 tzdata、缺数据时 ``SystemExit``；那些行为由
        ``tests/test_packaging_tzdata.py`` 与真实构建覆盖）。``collect_submodules`` 用替身。
        这样断言的是 spec **实际算出来的** excludes，而不是"spec 里出现过某个变量名"。
        """
        text = build.SPEC_PATH.read_text(encoding="utf-8")
        head = text.partition("TZDATA-COLLECT-BEGIN")[0]
        # 只剥掉 PyInstaller 的 import 语句（不能按关键字过滤整行：模块 docstring 的
        # 首行里也有 "PyInstaller"，删掉它会把 docstring 变成代码）。
        source = "\n".join(
            line
            for line in head.splitlines()
            if not line.startswith(("import PyInstaller", "from PyInstaller"))
        )
        namespace: dict[str, object] = {
            "__name__": "atri-x.spec",
            "SPECPATH": str(build.SPEC_PATH.parent),
            "collect_submodules": lambda _name: [],
        }
        clean = {
            key: value
            for key, value in os.environ.items()
            if key
            not in (
                optional_deps.KEEP_PILLOW_ENV,
                optional_deps.KEEP_EXTRAS_ENV,
                optional_deps.KEEP_OPTIONAL_ENV,
            )
        }
        clean.update(env_overrides)
        with mock.patch.dict(os.environ, clean, clear=True):
            exec(compile(source, str(build.SPEC_PATH), "exec"), namespace)  # noqa: S102
        return list(namespace["excludes"])  # type: ignore[arg-type]

    def test_spec_default_excludes_everything(self) -> None:
        self.assertEqual(self._spec_excludes(), ["numpy", "PIL", "yaml"])

    def test_spec_keep_pillow_excludes_only_extras(self) -> None:
        self.assertEqual(
            self._spec_excludes(**{optional_deps.KEEP_PILLOW_ENV: "1"}), ["numpy", "yaml"]
        )

    def test_spec_keep_extras_excludes_only_pillow(self) -> None:
        self.assertEqual(
            self._spec_excludes(**{optional_deps.KEEP_EXTRAS_ENV: "1"}), ["PIL"]
        )

    def test_spec_keep_optional_keeps_all_three(self) -> None:
        """向后兼容的显式等价关系：``KEEP_OPTIONAL=1`` == 三包全留。"""
        self.assertEqual(self._spec_excludes(**{optional_deps.KEEP_OPTIONAL_ENV: "1"}), [])

    def test_spec_and_build_plan_agree_on_every_combination(self) -> None:
        combos = (
            (["--with-pillow"], {optional_deps.KEEP_PILLOW_ENV: "1"}),
            (["--with-extras"], {optional_deps.KEEP_EXTRAS_ENV: "1"}),
            (
                ["--with-pillow", "--with-extras"],
                {optional_deps.KEEP_PILLOW_ENV: "1", optional_deps.KEEP_EXTRAS_ENV: "1"},
            ),
            (["--with-all-optional"], {optional_deps.KEEP_OPTIONAL_ENV: "1"}),
            ([], {}),
        )
        for argv, env in combos:
            with self.subTest(argv=argv):
                plan = optional_deps.plan_from_args(build.parse_args(argv))
                self.assertEqual(plan["excludes"], self._spec_excludes(**env))

    def test_repo_spec_understands_the_precise_switches(self) -> None:
        self.assertTrue(optional_deps.spec_contract_ok(build.SPEC_PATH))
        text = build.SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn(optional_deps.KEEP_PILLOW_ENV, text)
        self.assertIn(optional_deps.KEEP_EXTRAS_ENV, text)
        # 旧变量仍要兼容：KEEP_OPTIONAL=1 等价于"三包全留"。
        self.assertIn(optional_deps.KEEP_OPTIONAL_ENV, text)

    def test_old_spec_rejects_precise_switches(self) -> None:
        self.assertFalse(optional_deps.spec_contract_ok(self.old_spec))
        _plan, problems = _problems(["--with-pillow"], self.old_spec)
        self.assertTrue(problems)
        self.assertIn(optional_deps.KEEP_PILLOW_ENV, problems[0])

    def test_old_spec_still_supports_the_legacy_flag(self) -> None:
        _plan, problems = _problems(["--with-all-optional"], self.old_spec)
        self.assertEqual(problems, [])


class PillowBuildEnvTest(unittest.TestCase):
    """构建环境的 ``import PIL`` 检查：默认失败，显式开关才降级。"""

    def _verdict(self, argv, info):
        args = build.parse_args(argv)
        return optional_deps.pillow_env_verdict(
            keep_pillow=bool(args.with_pillow or args.with_all_optional),
            allow_missing=bool(args.allow_missing_pillow),
            info=info,
        )

    def test_installed_pillow_passes(self) -> None:
        info = {"backend": "pillow", "version": "12.3.0", "pillow_installed": True}
        code, stdout, stderr = self._verdict(["--with-pillow"], info)
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_missing_pillow_fails_by_default(self) -> None:
        code, stdout, stderr = self._verdict(["--with-pillow"], MISSING_PILLOW)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("import PIL", stderr)
        self.assertIn("--allow-missing-pillow", stderr)

    def test_allow_missing_pillow_warns_instead_of_failing(self) -> None:
        code, stdout, stderr = self._verdict(
            ["--with-pillow", "--allow-missing-pillow"], MISSING_PILLOW
        )
        self.assertEqual(code, 0)
        self.assertIn("pure-Python", stdout)
        self.assertEqual(stderr, "")

    def test_no_flag_ignores_a_missing_pillow(self) -> None:
        code, stdout, stderr = self._verdict([], MISSING_PILLOW)
        self.assertEqual((code, stdout, stderr), (0, "", ""))

    def test_legacy_flag_also_checks_the_build_env(self) -> None:
        code, _stdout, stderr = self._verdict(["--with-all-optional"], MISSING_PILLOW)
        self.assertEqual(code, 2)
        self.assertIn("import PIL", stderr)


class CheckEnvCliTest(unittest.TestCase):
    """``--check-env``：不构建也能拿到环境结论（CI 可用来门禁）。"""

    def test_check_env_prints_facts_without_building(self) -> None:
        with (
            mock.patch.object(build, "pyinstaller_version", return_value="9.9.9"),
            mock.patch.object(optional_deps, "backend_probe", return_value=MISSING_PILLOW),
            mock.patch.object(build, "run_build") as runner,
        ):
            code, out, _err = _run_cli(["--check-env", "--with-pillow", "--allow-missing-pillow"])
        self.assertEqual(code, 0)
        self.assertIn("pyinstaller : 9.9.9", out)
        self.assertIn("only probing", out)
        self.assertIn("numpy, yaml", out)  # --with-pillow 的 excludes 计划
        runner.assert_not_called()

    def test_check_env_fails_when_pillow_is_missing(self) -> None:
        with (
            mock.patch.object(build, "pyinstaller_version", return_value="9.9.9"),
            mock.patch.object(optional_deps, "backend_probe", return_value=MISSING_PILLOW),
        ):
            code, _out, err = _run_cli(["--check-env", "--with-pillow"])
        self.assertEqual(code, 2)
        self.assertIn("import PIL", err)

    def test_build_path_fails_before_pyinstaller_when_pillow_is_missing(self) -> None:
        """真构建路径：发现 PIL 缺失时必须在调用 PyInstaller 之前退出。

        真构建路径会先要求 draw.io 渲染器（``packaging/vendor/drawio`` 不入库，CI 上
        必然没有），这里把它打桩成"存在"，否则用例结果会随构建机是否放了渲染器漂移，
        测的就不是「PIL 校验早于 PyInstaller」了。
        """
        with (
            mock.patch.object(build, "resolve_drawio_dir", return_value=Path("/fake/drawio")),
            mock.patch.object(build, "pyinstaller_version", return_value="9.9.9"),
            mock.patch.object(optional_deps, "backend_probe", return_value=MISSING_PILLOW),
            mock.patch.object(build, "run_build") as runner,
        ):
            code, _out, err = _run_cli(["--with-pillow", "--no-verify"])
        self.assertEqual(code, 2)
        self.assertIn("import PIL", err)
        runner.assert_not_called()


class RunBuildEnvTest(unittest.TestCase):
    """落给 PyInstaller 的环境变量必须与日志里的 excludes 一致。"""

    @classmethod
    def setUpClass(cls) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(WORK_ROOT, ignore_errors=True)

    def _capture(self, argv):
        seen: dict[str, object] = {}

        def fake_run(cmd, env=None, cwd=None, **kwargs):  # noqa: ANN001 - 替身
            seen["cmd"] = cmd
            seen["env"] = dict(env or {})
            return SimpleNamespace(returncode=0)

        args = build.parse_args(
            argv + ["--dist", str(WORK_ROOT / "dist"), "--work", str(WORK_ROOT / "work")]
        )
        out = io.StringIO()
        with mock.patch.object(build.subprocess, "run", fake_run), contextlib.redirect_stdout(out):
            code = build.run_build(
                args,
                "atri-x",
                with_kernel=False,
                with_gui=False,
                plan=optional_deps.plan_from_args(args),
            )
        return code, seen, out.getvalue()

    def test_with_pillow_sets_only_the_pillow_env(self) -> None:
        code, seen, out = self._capture(["--with-pillow"])
        self.assertEqual(code, 0)
        env = seen["env"]
        self.assertEqual(env[optional_deps.KEEP_PILLOW_ENV], "1")
        self.assertEqual(env[optional_deps.KEEP_EXTRAS_ENV], "0")
        # 旧变量必须关掉，否则 spec 会退回"三包全留"。
        self.assertEqual(env[optional_deps.KEEP_OPTIONAL_ENV], "0")
        self.assertIn("excludes    : numpy, yaml", out)
        self.assertIn("pillow=True", out)

    def test_legacy_flag_sets_keep_optional(self) -> None:
        _code, seen, out = self._capture(["--with-all-optional"])
        env = seen["env"]
        self.assertEqual(env[optional_deps.KEEP_OPTIONAL_ENV], "1")
        self.assertEqual(env[optional_deps.KEEP_PILLOW_ENV], "1")
        self.assertEqual(env[optional_deps.KEEP_EXTRAS_ENV], "1")
        self.assertIn("excludes    : <none>", out)

    def test_default_excludes_everything(self) -> None:
        _code, seen, out = self._capture([])
        env = seen["env"]
        self.assertEqual(
            (
                env[optional_deps.KEEP_PILLOW_ENV],
                env[optional_deps.KEEP_EXTRAS_ENV],
                env[optional_deps.KEEP_OPTIONAL_ENV],
            ),
            ("0", "0", "0"),
        )
        self.assertIn("excludes    : numpy, PIL, yaml", out)

    def test_probe_module_is_documented_in_help(self) -> None:
        """部署侧核验命令必须写在 ``--help`` / 模块 docstring 里。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = build.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--with-pillow", out.getvalue())
        self.assertIn("--with-all-optional", out.getvalue())
        self.assertIn("--check-env", out.getvalue())
        self.assertIn(optional_deps.BACKEND_PROBE_MODULE, build.__doc__ or "")
        self.assertIn(optional_deps.BACKEND_PROBE_MODULE, optional_deps.__doc__ or "")


class VerifyCapabilityLineTest(unittest.TestCase):
    """产物侧只打印离线核验命令，不做假的"验证过 pillow"断言。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dist = WORK_ROOT / "dist" / "atri-x"
        cls.dist.mkdir(parents=True, exist_ok=True)
        cls.artifact = cls.dist / "atri-x.exe"
        cls.artifact.write_bytes(b"MZ-fake")

    def _verify(self, *, with_pillow: bool):
        def fake_run_exe(artifact, argv, timeout, scratch_home=False):  # noqa: ANN001 - 替身
            del artifact, timeout, scratch_home
            stdout = ""
            if argv == ["--version"]:
                stdout = "atri-x 9.9.9"
            elif argv == ["config", "check"]:
                stdout = "config OK"
            elif argv[:2] == ["-m", "atri_x.app.time_check"]:
                stdout = '{"ok": true, "zones": 598}'
            return {"code": 0, "stdout": stdout, "stderr": ""}

        # service_smoke 要真的起进程、监听端口，假 exe（b"MZ-fake"）跑不了；这里只测
        # pillow 能力行的输出，所以把冒烟替掉（冒烟本身见下一个用例）。
        def fake_service_smoke(artifact, timeout=180.0):  # noqa: ANN001 - 替身
            del artifact, timeout
            return 0

        out = io.StringIO()
        with (
            mock.patch.object(build, "run_exe", fake_run_exe),
            mock.patch.object(build, "service_smoke", fake_service_smoke),
            contextlib.redirect_stdout(out),
        ):
            # 这里只测 pillow 能力行：随包研究引擎的产物级核验见
            # ``test_missing_research_pack_fails_verify``（假 exe 没有 research_pack）。
            code = build.verify(
                self.artifact, "atri-x", with_pillow=with_pillow, with_research=False
            )
        return code, out.getvalue()

    def test_requested_pillow_prints_the_offline_check_command(self) -> None:
        code, out = self._verify(with_pillow=True)
        self.assertEqual(code, 0)
        self.assertIn(optional_deps.BACKEND_PROBE_MODULE, out)
        self.assertIn("offline check", out)
        self.assertIn("--json", out)

    def test_excluded_pillow_says_so(self) -> None:
        code, out = self._verify(with_pillow=False)
        self.assertEqual(code, 0)
        self.assertIn("pure-Python image codec only", out)

    def test_service_smoke_failure_fails_verify(self) -> None:
        """后台服务起不来时 verify 必须返回 1（1.0.5 就是漏了这条检查）。"""

        def fake_run_exe(artifact, argv, timeout, scratch_home=False):  # noqa: ANN001
            del artifact, timeout, scratch_home
            if argv == ["--version"]:
                stdout = "atri-x 9.9.9"
            elif argv[:2] == ["-m", "atri_x.app.time_check"]:
                stdout = '{"ok": true, "zones": 598}'
            else:
                stdout = "config OK"
            return {"code": 0, "stdout": stdout, "stderr": ""}

        def failing_service_smoke(artifact, timeout=180.0):  # noqa: ANN001
            del artifact, timeout
            return 1

        out = io.StringIO()
        with (
            mock.patch.object(build, "run_exe", fake_run_exe),
            mock.patch.object(build, "service_smoke", failing_service_smoke),
            contextlib.redirect_stdout(out),
        ):
            code = build.verify(
                self.artifact, "atri-x", with_pillow=False, with_research=False
            )
        self.assertEqual(code, 1)

    def test_missing_research_pack_fails_verify(self) -> None:
        """随包引擎缺失/装不上时 verify 必须返回 1（1.1.3 起是硬要求）。

        假 exe 跑不出 research bootstrap 的 JSON，正好模拟"spec 漏收 research_pack"。
        """

        def fake_run_exe(artifact, argv, timeout, scratch_home=False):  # noqa: ANN001
            del artifact, timeout, scratch_home
            stdout = ""
            if argv == ["--version"]:
                stdout = "atri-x 9.9.9"
            elif argv == ["config", "check"]:
                stdout = "config OK"
            elif argv[:2] == ["-m", "atri_x.app.time_check"]:
                stdout = '{"ok": true, "zones": 598}'
            return {"code": 0, "stdout": stdout, "stderr": ""}

        def fake_service_smoke(artifact, timeout=180.0):  # noqa: ANN001
            del artifact, timeout
            return 0

        out = io.StringIO()
        with (
            mock.patch.object(build, "run_exe", fake_run_exe),
            mock.patch.object(build, "service_smoke", fake_service_smoke),
            contextlib.redirect_stdout(out),
        ):
            code = build.verify(self.artifact, "atri-x", with_pillow=False)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
