r"""自动会话命名的**调用契约**回归测试。

这个文件存在的原因是：一次真实的 P0。

``NamingMixin.generate_title`` 过去这样调用标题生成器::

    result = self.title_gen.generate_title(messages)

而唯一的生产实现是 :meth:`atri_x.agent.title.TitleGenerator.generate_title`，
签名是 ``async def generate_title(self, ctx, messages)``。后果：

* 每次命名在绑参阶段就抛 ``TypeError``，被 ``except Exception`` 吞掉并记成
  ``llm_error`` —— 模型生成的标题**永远不落库**，3 次失败后回落到「首条消息截断」；
* 现有测试用的是**同步、只收 messages** 的假实现，所以整个套件全绿也照不出来。

因此这里锁住两件事：

1. ``naming.generate_title`` 能正确驱动**真实签名**（``async (ctx, messages)``）；
2. 真实 ``TitleGenerator`` 的签名本身没有漂移（有人改签名时这个测试先炸）。
"""

from __future__ import annotations

import inspect
import unittest

from atri_x.session.naming import _call_title_generator  # noqa: PLC2701 - 契约测试


class _Result:
    def __init__(self, raw_title: str) -> None:
        self.raw_title = raw_title


class _AsyncGenerator:
    """真实形状：``async def generate_title(ctx, messages)``。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def generate_title(self, ctx, messages):  # noqa: ANN001, ANN201
        self.calls.append((ctx, messages))
        return _Result("异步标题")


class _SyncOneArgGenerator:
    """旧测试替身形状：同步、只收 messages。"""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def generate_title(self, messages):  # noqa: ANN001, ANN201
        self.calls.append(messages)
        return _Result("同步标题")


class _BoomGenerator:
    async def generate_title(self, ctx, messages):  # noqa: ANN001, ANN201
        raise RuntimeError("llm exploded")


class TestCallTitleGenerator(unittest.TestCase):
    def test_real_shape_is_awaited_and_receives_ctx(self) -> None:
        generator = _AsyncGenerator()
        marker = object()
        result = _call_title_generator(generator, marker, ["m1"])
        self.assertEqual(result.raw_title, "异步标题")
        self.assertEqual(len(generator.calls), 1)
        self.assertIs(generator.calls[0][0], marker)
        self.assertEqual(generator.calls[0][1], ["m1"])

    def test_sync_one_arg_shape_is_supported(self) -> None:
        generator = _SyncOneArgGenerator()
        result = _call_title_generator(generator, object(), ["m1"])
        self.assertEqual(result.raw_title, "同步标题")
        self.assertEqual(generator.calls, [["m1"]])

    def test_generator_exception_propagates(self) -> None:
        with self.assertRaises(RuntimeError):
            _call_title_generator(_BoomGenerator(), None, ["m1"])

    def test_none_ctx_is_allowed(self) -> None:
        generator = _AsyncGenerator()
        self.assertEqual(
            _call_title_generator(generator, None, []).raw_title, "异步标题"
        )
        self.assertIsNone(generator.calls[0][0])


class TestRealTitleGeneratorContract(unittest.TestCase):
    """真实实现的签名不得漂移。"""

    def test_signature_is_async_ctx_messages(self) -> None:
        from atri_x.agent.title import TitleGenerator

        function = TitleGenerator.generate_title
        self.assertTrue(
            inspect.iscoroutinefunction(function),
            "TitleGenerator.generate_title 必须仍是协程函数",
        )
        names = list(inspect.signature(function).parameters)
        self.assertEqual(names[:3], ["self", "ctx", "messages"])

    def test_async_implementation_is_accepted_by_the_bridge(self) -> None:
        # 直接用真实类的方法（不构造实例，避免依赖 LLM 客户端）。
        from atri_x.agent.title import TitleGenerator

        function = TitleGenerator.generate_title
        positional = [
            parameter
            for parameter in inspect.signature(function).parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        # _call_title_generator 用「>= 2」判定是否传 ctx；self 已绑定所以这里应为 2。
        self.assertGreaterEqual(len(positional), 2)

    def test_naming_mixin_uses_the_bridge(self) -> None:
        import atri_x.session.naming as naming

        source = inspect.getsource(naming.NamingMixin.generate_title)
        self.assertIn("_call_title_generator", source)
        self.assertNotIn("self.title_gen.generate_title(messages)", source)


if __name__ == "__main__":
    unittest.main()
