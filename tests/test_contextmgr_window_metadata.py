r"""回归测试：``WindowResolver`` 的 **provider 上报窗口**必须同时作用于同步路径。

一次真实的 P2（见 ``verification/contextmgr.md`` CM-1）。

Go 侧 ``internal/contextmgr/window.go:21-45`` 只有**一个** ``Resolve``：

1. 进程内缓存；
2. ``ClientFor(provider).(llm.ModelMetadataProvider).ListModelMetadata(ctx)``
   —— 最权威，命中即写缓存并返回；
3. 配置里的手工 ``model_configs.<model>.context_window``；
4. ``default_context_window``；
5. ``config.DefaultContextWindow``。

而 ``internal/agent/context_usage.go``（``status`` 即 ``/context``）与
``context_policy.go``（``reachedCompactThreshold``，即 ``shouldCompact``）走的都是它。

Python 侧把这些调用点钉成了同步返回（``AgentSeams.should_compact`` 直接 ``bool()``），
于是 ``should_compact`` / ``status`` 走 :meth:`WindowResolver.resolve_sync`：它能读到
缓存与**已解析**的 provider 元数据，但不会现取（provider 适配器是协程）。本文件锁住：

* 已解析过 provider 元数据之后，同步路径与异步路径取值一致（含同一 provider 的**其它**
  模型 —— 快照是整份记下来的）；
* 装配期的 :meth:`WindowResolver.prewarm` 能让同步路径在"冷缓存"下就拿到 provider 值；
* 配置值/默认值**不会**被同步路径写进缓存，否则异步路径永远读不到 provider 上报值；
* provider 未知 / 客户端抛错 / 元数据失败一律降级，不炸 ``/context``。
"""

from __future__ import annotations

import asyncio
import unittest

from atri_x.contextmgr import budget as bg
from atri_x.contextmgr import window as wd
from atri_x.llm.llm import ModelMetadata


def run(coro):
    return asyncio.run(coro)


class FakeMetadataClient:
    """假 provider 客户端：``list_model_metadata`` 是协程（与真实适配器一致）。"""

    def __init__(self, items, calls: list[str] | None = None, error=None) -> None:
        self._items = list(items)
        self._calls = calls if calls is not None else []
        self._error = error

    async def list_model_metadata(self, ctx):
        self._calls.append("list_model_metadata")
        if self._error is not None:
            raise self._error
        return list(self._items)


class TestWindowResolverGoPriority(unittest.TestCase):
    """逐条对齐 Go ``window_test.go`` 的 ``TestWindowResolverPriority``。"""

    def _resolver(self, calls=None):
        return wd.WindowResolver(
            default_window=8192,
            manual_windows={"p/manual": 16000, "p/fallback": 12000},
            client_for=lambda provider: FakeMetadataClient(
                [ModelMetadata(id="api", context_window=32000)], calls
            ),
        )

    def test_provider_metadata_wins(self) -> None:
        self.assertEqual(32000, run(self._resolver().resolve(None, "p", "api")))

    def test_manual_window_for_other_model(self) -> None:
        self.assertEqual(16000, run(self._resolver().resolve(None, "p", "manual")))
        self.assertEqual(12000, run(self._resolver().resolve(None, "p", "fallback")))

    def test_default_window_when_metadata_has_no_match(self) -> None:
        self.assertEqual(8192, run(self._resolver().resolve(None, "p", "unknown")))

    def test_builtin_default_window_for_empty_resolver(self) -> None:
        resolver = wd.WindowResolver()
        self.assertEqual(
            bg.FALLBACK_CONTEXT_WINDOW, run(resolver.resolve(None, "p", "unknown"))
        )
        self.assertEqual(
            bg.FALLBACK_CONTEXT_WINDOW, resolver.resolve_sync("p", "unknown")
        )

    def test_default_window_is_not_cached(self) -> None:
        resolver = self._resolver()
        run(resolver.resolve(None, "p", "unknown"))
        self.assertEqual({}, resolver._cache)

    def test_manual_window_is_cached_on_async_path(self) -> None:
        # Go 是"先问元数据、没命中才用手工配置"，所以异步路径缓存 16000 是安全的。
        resolver = self._resolver()
        run(resolver.resolve(None, "p", "manual"))
        self.assertEqual({"p/manual": 16000}, resolver._cache)


class TestWindowResolverSyncPath(unittest.TestCase):
    """CM-1 的核心：同步路径必须能读到 provider 上报值。"""

    def _resolver(self, calls=None, client_for=None):
        if client_for is None:
            client_for = lambda provider: FakeMetadataClient(  # noqa: E731
                [
                    ModelMetadata(id="m1", context_window=500000),
                    ModelMetadata(id="m2", context_window=400000),
                    ModelMetadata(id="zero", context_window=0),
                ],
                calls,
            )
        return wd.WindowResolver(
            default_window=256000,
            manual_windows={"p/m1": 100000, "p/m2": 100000, "p/manual": 30000},
            client_for=client_for,
        )

    # -- 热缓存 ------------------------------------------------------------
    def test_warm_cache_matches_async(self) -> None:
        resolver = self._resolver()
        self.assertEqual(500000, run(resolver.resolve(None, "p", "m1")))
        self.assertEqual(500000, resolver.resolve_sync("p", "m1"))

    def test_sync_sees_other_models_of_same_provider(self) -> None:
        # 只解析过 m1，但 m2 的值也在同一份元数据里 —— 同步路径也要能拿到。
        resolver = self._resolver()
        self.assertEqual(500000, run(resolver.resolve(None, "p", "m1")))
        self.assertEqual(400000, resolver.resolve_sync("p", "m2"))
        # 同步路径取到的是 provider 值，写进缓存后与异步路径一致
        self.assertEqual(400000, run(resolver.resolve(None, "p", "m2")))

    # -- 冷缓存（装配期预热） ----------------------------------------------
    def test_prewarm_fills_snapshot_without_any_resolve(self) -> None:
        calls: list[str] = []
        resolver = self._resolver(calls)
        run(resolver.prewarm(None, {"p": object()}))
        self.assertEqual(["list_model_metadata"], calls, "每个 provider 只取一次元数据")
        self.assertEqual(500000, resolver.resolve_sync("p", "m1"))
        self.assertEqual(400000, resolver.resolve_sync("p", "m2"))
        self.assertEqual(30000, resolver.resolve_sync("p", "manual"))

    def test_prewarm_accepts_iterable_of_names(self) -> None:
        resolver = self._resolver()
        run(resolver.prewarm(None, ["p"]))
        self.assertEqual(500000, resolver.resolve_sync("p", "m1"))

    def test_prewarm_skips_failing_provider(self) -> None:
        def client_for(provider):
            if provider == "bad":
                return FakeMetadataClient([], error=RuntimeError("boom"))
            return FakeMetadataClient([ModelMetadata(id="m1", context_window=500000)])

        resolver = self._resolver(client_for=client_for)
        run(resolver.prewarm(None, ["bad", "good"]))
        self.assertEqual({}, resolver._known_metadata("bad"))
        self.assertEqual(500000, resolver.resolve_sync("good", "m1"))
        # 失败的那个降级到手工/默认值，不会炸
        self.assertEqual(256000, resolver.resolve_sync("bad", "m1"))

    def test_prewarm_does_not_cache_configured_values(self) -> None:
        # 预热只填"provider 上报"快照；配置值不写缓存（否则异步路径读不到上报值）。
        resolver = self._resolver()
        run(resolver.prewarm(None, {"p": object()}))
        self.assertEqual({}, resolver._cache)

    # -- 冷缓存（没有预热过） ----------------------------------------------
    def test_cold_sync_falls_back_to_manual_then_default_then_fallback(self) -> None:
        resolver = self._resolver()
        # 手工配置优先于默认窗口
        self.assertEqual(30000, resolver.resolve_sync("p", "manual"))
        # 默认窗口
        self.assertEqual(256000, resolver.resolve_sync("p", "unknown"))
        # 连默认窗口都没有 → 内置兜底
        bare = wd.WindowResolver(manual_windows={"p/m": 7})
        self.assertEqual(7, bare.resolve_sync("p", "m"))
        self.assertEqual(bg.FALLBACK_CONTEXT_WINDOW, bare.resolve_sync("p", "missing"))

    def test_cold_sync_does_not_poison_the_cache(self) -> None:
        # 这是 CM-1 的"冻死"路径：同步路径冷读时拿到配置值，若把配置值写进缓存，
        # 之后的异步路径会命中缓存、永远读不到 provider 上报值。
        resolver = self._resolver()
        self.assertEqual(100000, resolver.resolve_sync("p", "m1"))
        self.assertEqual({}, resolver._cache, "配置值不能进缓存")
        self.assertEqual(500000, run(resolver.resolve(None, "p", "m1")))
        self.assertEqual(500000, resolver.resolve_sync("p", "m1"))

    def test_zero_window_metadata_is_ignored(self) -> None:
        resolver = self._resolver()
        run(resolver.prewarm(None, {"p": object()}))
        self.assertNotIn("zero", resolver._known_metadata("p"))
        self.assertEqual(256000, resolver.resolve_sync("p", "zero"))

    def test_clear_cache_keeps_resolved_metadata(self) -> None:
        # provider 上报值不是配置，热重载清缓存后同步路径仍应拿得到它。
        resolver = self._resolver()
        run(resolver.prewarm(None, {"p": object()}))
        resolver.clear_cache()
        self.assertEqual(400000, resolver.resolve_sync("p", "m2"))


class TestWindowResolverDegradation(unittest.TestCase):
    """provider 未知 / 客户端异常时的降级（Go：``ClientFor`` 返回 nil，断言失败继续走）。"""

    def test_client_for_raising_unknown_provider(self) -> None:
        def client_for(provider):
            raise RuntimeError(f"client not found for provider {provider!r}")

        resolver = wd.WindowResolver(
            default_window=8192, manual_windows={"p/m": 999}, client_for=client_for
        )
        self.assertEqual(999, run(resolver.resolve(None, "p", "m")))
        self.assertEqual(8192, run(resolver.resolve(None, "p", "other")))
        self.assertEqual(999, resolver.resolve_sync("p", "m"))
        self.assertEqual(8192, resolver.resolve_sync("p", "other"))

    def test_client_for_returning_none(self) -> None:
        resolver = wd.WindowResolver(default_window=8192, client_for=lambda provider: None)
        self.assertEqual(8192, run(resolver.resolve(None, "p", "m")))

    def test_client_without_metadata_support(self) -> None:
        class Bare:
            pass

        resolver = wd.WindowResolver(default_window=8192, client_for=lambda provider: Bare())
        self.assertEqual(8192, run(resolver.resolve(None, "p", "m")))

    def test_metadata_error_degrades(self) -> None:
        resolver = wd.WindowResolver(
            default_window=8192,
            manual_windows={"p/m": 999},
            client_for=lambda provider: FakeMetadataClient([], error=RuntimeError("boom")),
        )
        self.assertEqual(999, run(resolver.resolve(None, "p", "m")))
        self.assertEqual({}, resolver._known_metadata("p"))

    def test_no_client_at_all(self) -> None:
        resolver = wd.WindowResolver(default_window=8192)
        self.assertEqual(8192, run(resolver.resolve(None, "p", "m")))
        self.assertEqual(8192, resolver.resolve_sync("p", "m"))
        self.assertIsNone(run(resolver.prewarm(None, ["p"])))
        self.assertEqual({}, resolver._known_metadata("p"))


if __name__ == "__main__":
    unittest.main()
