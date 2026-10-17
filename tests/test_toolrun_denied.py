r"""回归测试：**被拒绝的工具调用绝不能记成成功**。

一次真实的 P1。

``runner.run`` 把确认结果的拒绝信息喂给 ``deps.record_tool_call(..., error)``，
而存储层只看 ``error is None`` 判定 ``success``。历史上 :class:`ConfirmResult`
只有 ``message`` 没有 ``error``，``_confirm`` 的四个拒绝分支（工具不可用、
原生工具缺失、需要超管、风险等级超限）都 ``return ConfirmResult(allowed=False, ...)``
**不抛异常**，于是：

* ``tool_call_records.success`` 被写成 1、``error`` 为 NULL；
* ``SuccessfulIDs`` 把这次调用当成「成功过的工具调用」，经
  ``agent/context_compact.py`` 进入压缩后的上下文；
* 模型因此以为「这个工具刚才真的执行过」，而实际上策略根本没放行。

Go 侧 ``internal/toolrun/runner.go`` 的 confirm 是 ``(ConfirmResult, error)`` 双返回，
拒绝时 error 非 nil，所以上游没有这个问题。本文件锁住修复后的不变式。
"""

from __future__ import annotations

import unittest

from atri_x.errors import ForbiddenError

from tests.test_toolrun import FakeDeps, TestRunner, run
from atri_x.toolrun import runner as runner_mod


class TestDeniedCallIsRecordedWithError(TestRunner):
    """继承 ``TestRunner`` 的脚手架（真 Manager + 假 deps），只加拒绝路径的断言。"""

    def _records(self, deps: FakeDeps) -> list:
        return getattr(deps, "records", [])

    def test_denial_without_explicit_error_still_records_one(self) -> None:
        # 最坏情况：拒绝结果连 error 都没带（例如 deps.confirm_background_tool
        # 返回的拒绝）。兜底必须补上，否则这条调用会以 success=1 落库。
        deps = FakeDeps(confirm=runner_mod.ConfirmResult(allowed=False))
        req, _ = self._req(deps)
        run(runner_mod.run(self._confirm_manager(), None, deps, req))

        records = self._records(deps)
        self.assertEqual(1, len(records), "被拒绝的调用应当被记录一次")
        _name, _risk, call_err = records[0]
        self.assertIsNotNone(
            call_err, "拒绝路径必须带 error，否则会被记成 success（P1 回归）"
        )
        self.assertIsInstance(call_err, ForbiddenError)

    def test_explicit_denial_error_is_passed_through(self) -> None:
        denial = ForbiddenError("tool_requires_superadmin")
        deps = FakeDeps(
            confirm=runner_mod.ConfirmResult(allowed=False, error=denial)
        )
        req, _ = self._req(deps)
        run(runner_mod.run(self._confirm_manager(), None, deps, req))

        records = self._records(deps)
        self.assertEqual(1, len(records))
        self.assertIs(
            records[0][2], denial, "拒绝原因必须原样传给 record_tool_call"
        )

    def test_stopped_denial_records_nothing(self) -> None:
        # 用户中断不是「一次失败的调用」，Go 也不记录。
        deps = FakeDeps(
            confirm=runner_mod.ConfirmResult(allowed=False, stopped=True)
        )
        req, _ = self._req(deps)
        result = run(runner_mod.run(self._confirm_manager(), None, deps, req))

        self.assertTrue(result.stopped)
        self.assertEqual([], self._records(deps))

    def test_allowed_call_records_no_error(self) -> None:
        # 反向对照：放行的调用不能被误记成失败。
        deps = FakeDeps()
        req, _ = self._req(deps)
        run(runner_mod.run(self._confirm_manager(), None, deps, req))
        records = self._records(deps)
        self.assertTrue(records)
        self.assertIsNone(records[0][2])


class TestConfirmResultContract(unittest.TestCase):
    def test_error_field_exists_and_defaults_to_none(self) -> None:
        result = runner_mod.ConfirmResult()
        self.assertIsNone(result.error)
        self.assertFalse(result.allowed)

    def test_error_can_carry_a_reason(self) -> None:
        boom = ForbiddenError("nope")
        self.assertIs(
            runner_mod.ConfirmResult(allowed=False, error=boom).error, boom
        )


if __name__ == "__main__":
    unittest.main()
