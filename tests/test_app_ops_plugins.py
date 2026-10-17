"""运维端点回归：``/plugins/memory`` 与 ``/plugins/learning``。

真实事故（第三轮对抗性验证在**真实服务**上抓到，两轮静态/单测都没发现）：

- ``GET /plugins/memory`` → ``500 {"error":"Service.count() takes 3 positional arguments
  but 4 were given"}``；``GET /plugins/learning`` 同理（``Service.stats``）。
  根因：:mod:`atri_x.agent.memory_learning_admin` 照 Go 把 ``ctx`` 传给了**同步存储**服务，
  而本仓 ``angelmemory.Service.count(platform, scope_id)`` / ``selflearning.Service.stats``
  签名里没有 ctx（全仓另外 3 个调用点都用 2 参形状）。
- 同一个端点还有第二个问题：``Server.dispatch`` 把查询串丢掉，``request.query`` 恒空 →
  ``platform`` / ``scope_id`` 过滤静默失效（端点照回 200，但永远是空作用域的统计）。

这两条都只在"真实启动 + 真实 HTTP 请求"里才暴露，所以本文件走**真实 handler + 真实服务 +
真实 dispatch**，不搭假的 HTTP 层。
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace

from atri_x.angelmemory.service import Service as AngelMemoryService
from atri_x.app.ops_health import write_json_response
from atri_x.app.runner import _learning_admin_handler, _memory_admin_handler
from atri_x.health.server import Server, ServerOptions
from atri_x.health.state import State
from atri_x.selflearning.service import Service as SelfLearningService

TOKEN = "ops-token"
HEADERS = {"x-atrix-ops-token": TOKEN}


def _server(agent: object) -> Server:
    return Server(
        ServerOptions(
            addr="127.0.0.1:0",
            state=State(),
            extra_handlers={
                "/plugins/memory": _memory_admin_handler(agent),
                "/plugins/learning": _learning_admin_handler(agent),
            },
            extra_handler_token=TOKEN,
        )
    )


def _agent() -> object:
    # 真实服务实现 + 未装配存储（Go 的 nil 早退：返回 0 / (0, 0)）。
    return SimpleNamespace(
        angel_memory=AngelMemoryService(None),
        self_learning=SelfLearningService(None),
    )


class OpsPluginsEndpointTest(unittest.TestCase):
    def test_memory_endpoint_returns_count_not_500(self) -> None:
        response = _server(_agent()).dispatch(
            "GET", "/plugins/memory?platform=cli&scope_id=local", HEADERS
        )

        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(
            response.json(), {"platform": "cli", "scope_id": "local", "count": 0}
        )

    def test_learning_endpoint_returns_counts_not_500(self) -> None:
        response = _server(_agent()).dispatch(
            "GET", "/plugins/learning?platform=cli&scope_id=local", HEADERS
        )

        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(
            response.json(),
            {"platform": "cli", "scope_id": "local", "pending": 0, "approved": 0},
        )

    def test_query_parameters_are_not_dropped(self) -> None:
        """查询串必须传进处理函数（``r.URL.Query()``），否则过滤静默失效。"""
        response = _server(_agent()).dispatch(
            "GET", "/plugins/memory?platform=telegram&scope_id=-100123", HEADERS
        )

        body = response.json()
        self.assertEqual(body["platform"], "telegram")
        self.assertEqual(body["scope_id"], "-100123")

    def test_endpoints_require_the_ops_token(self) -> None:
        server = _server(_agent())

        self.assertEqual(server.dispatch("GET", "/plugins/memory").status, 401)

    def test_service_signatures_are_the_python_convention(self) -> None:
        """服务是同步存储：``count`` / ``stats`` 不带 ctx —— 调用方必须按这个形状调。

        这条钉住的是"哪一边是约定"：Go 把 ctx 一路穿下去，本仓在同步存储层丢掉它，
        全仓 5 个调用点里 3 个用 2 参、2 个（就是这次出事的）用 3 参。
        """
        self.assertEqual(
            list(inspect.signature(AngelMemoryService.count).parameters),
            ["self", "platform", "scope_id"],
        )
        self.assertEqual(
            list(inspect.signature(SelfLearningService.stats).parameters),
            ["self", "platform", "scope_id"],
        )


class BareAsyncHandlerTest(unittest.TestCase):
    """``extra_handlers`` 直接收下 ``async`` 处理器时，协程必须被驱动。

    真实事故（第三轮对抗性验证抓到）：``dispatch`` 只按 Go 风格同步调用，
    ``async def handler(w, r)`` 返回的协程被丢弃 —— 客户端拿到 ``200`` + **空 body**，
    进程里只剩一条 ``RuntimeWarning: coroutine ... was never awaited``。
    生产装配里这类处理器通常被 `LazyHTTPHandler` 包住，所以真实启动路径看不出来。
    """

    def _server(self) -> Server:
        async def handler(handler_ctx: object, request: object) -> None:
            write_json_response(handler_ctx, 200, {"ok": True, "path": request.path})

        return Server(
            ServerOptions(
                addr="127.0.0.1:0",
                state=State(),
                extra_handlers={"/plugins/bare": handler},
                extra_handler_token=TOKEN,
            )
        )

    def test_bare_async_handler_body_is_consumed(self) -> None:
        response = self._server().dispatch("GET", "/plugins/bare", HEADERS)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json(), {"ok": True, "path": "/plugins/bare"})

    def test_bare_async_handler_still_requires_token(self) -> None:
        self.assertEqual(self._server().dispatch("GET", "/plugins/bare").status, 401)

    def test_bare_async_handler_rejects_non_get(self) -> None:
        response = self._server().dispatch("POST", "/plugins/bare", HEADERS)
        self.assertEqual(response.status, 405)
        self.assertEqual(response.headers["Allow"], "GET")


class AsyncFunctionalHandlerTest(unittest.TestCase):
    """函数式 ``async def handler(ctx)`` 也不能把协程丢掉（同样的静默空响应）。"""

    def _server(self) -> Server:
        async def handler(ctx: object) -> object:
            return {"ok": True}

        return Server(
            ServerOptions(
                addr="127.0.0.1:0",
                state=State(),
                extra_handlers={"/plugins/async-fn": handler},
                extra_handler_token=TOKEN,
            )
        )

    def test_async_functional_handler_returns_its_value(self) -> None:
        response = self._server().dispatch("GET", "/plugins/async-fn", HEADERS)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(response.json(), {"ok": True})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
