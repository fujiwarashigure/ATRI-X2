r"""回归测试：Atriwisp 工具的**参数编解码**必须与 Go 一致。

一次真实的 P2（见 ``verification/toolrun.md`` TR-2）。

Go 侧（``internal/toolrun/atriwisp_exec.go:43-50``）：

.. code-block:: go

    arguments := json.RawMessage(strings.TrimSpace(call.Arguments))
    if len(arguments) == 0 {
        arguments = json.RawMessage(`{}`)
    }
    payload, err := json.Marshal(atriwispToolRequest{Tool: ..., EventKey: ..., Arguments: arguments})
    if err != nil {
        return executionError(call, message, err)
    }

``json.Marshal`` 编码 ``json.RawMessage`` 时会走 ``marshalerEncoder`` →
``appendCompact(out, b, true)``：**字节非法即返回 ``*json.MarshalerError``**，于是
``executionError`` 直接结束这次调用 —— **不发出任何 HTTP 请求**。

Python 侧历史上把 ``json.loads`` 失败**回退成原始字符串**，payload 变成
``{"tool": "t", "arguments": "{not json"}``，请求照发、远端可能把畸形参数当合法输入。

同一函数还有第二处分歧：Go 用 ``appendCompact`` **原样嵌入** RawMessage 的字节
（只去掉字符串外的空白、做 HTML 转义），而旧实现 ``json.loads`` → ``json.dumps``
会重写数字（``1e2`` → ``100.0``、``1.50`` → ``1.5``）、解码 ``\uXXXX``，并且对
``1e400`` 这类溢出会写出**非法的** ``Infinity``。本文件把两件事都钉住。
"""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from atri_x.llm.llm import ToolCallRequest, segments_content_text
from atri_x.toolrun import CachedTool
from atri_x.toolrun import atriwisp_exec as awex

#: Go 那条 ``json.Marshal`` 包装错误的固定前缀（``MarshalerError.Error()``）。
GO_MARSHAL_ERROR_PREFIX = (
    "json: error calling MarshalJSON for type json.RawMessage: "
)


class _RecordingHandler(BaseHTTPRequestHandler):
    """记录**原始请求体字节**的 Atriwisp hub 替身。"""

    requests: list[bytes] = []
    response: bytes = json.dumps({"content": "ok"}).encode("utf-8")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append(self.rfile.read(length))
        data = type(self).response
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # noqa: A002
        return


@dataclass
class _Resolved:
    cached: CachedTool | None = None


class TestAtriwispArguments(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}/tool"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.response = json.dumps({"content": "ok"}).encode("utf-8")

    # -- 脚手架 -------------------------------------------------------------
    def _resolved(self, endpoint: str | None = None, event_key: str = "ev") -> _Resolved:
        return _Resolved(
            cached=CachedTool(
                name="t",
                canonical_name="atriwisp_h_t",
                endpoint=self.endpoint if endpoint is None else endpoint,
                event_key=event_key,
            )
        )

    def _call(self, arguments: str, **kwargs):
        return awex.execute_atriwisp_tool(
            None,
            ToolCallRequest(name="t", id="1", arguments=arguments),
            self._resolved(**kwargs),
        )

    def _body(self) -> bytes:
        self.assertEqual(1, len(_RecordingHandler.requests), "应当恰好发出一次请求")
        return _RecordingHandler.requests[0]

    # -- 非法 JSON：本地失败、不发请求 --------------------------------------
    def test_invalid_json_fails_locally_without_request(self) -> None:
        message, result, err = self._call("{not json")
        self.assertIsNone(result)
        self.assertIsInstance(err, ValueError)
        self.assertEqual([], _RecordingHandler.requests, "非法参数绝不能发出请求")
        self.assertIn("tool call t failed", segments_content_text(message.segments))

    def test_error_text_uses_go_marshaler_error_shape(self) -> None:
        _message, _result, err = self._call("{not json")
        self.assertTrue(
            str(err).startswith(GO_MARSHAL_ERROR_PREFIX),
            f"错误文本外层必须与 Go 的 MarshalerError 一致，实际：{err}",
        )
        # 内层是语法细节（Go 用 scanner 的 SyntaxError，Python 用解码器文案）。
        self.assertTrue(str(err)[len(GO_MARSHAL_ERROR_PREFIX) :].strip())

    def test_trailing_garbage_is_invalid(self) -> None:
        _message, result, err = self._call('{"a":1} trailing')
        self.assertIsNone(result)
        self.assertIsInstance(err, ValueError)
        self.assertEqual([], _RecordingHandler.requests)

    def test_nan_and_infinity_are_rejected_like_go(self) -> None:
        # Go 的 scanner 只认 RFC 7159 字面量；Python 的 json 默认接受这三个。
        for arguments in ("NaN", "Infinity", "-Infinity", '{"a": NaN}'):
            with self.subTest(arguments=arguments):
                _RecordingHandler.requests = []
                _message, result, err = self._call(arguments)
                self.assertIsNone(result)
                self.assertIsInstance(err, ValueError)
                self.assertEqual([], _RecordingHandler.requests)

    def test_invalid_json_wins_over_missing_endpoint(self) -> None:
        # Go 的顺序是「先 Marshal（内含校验）再建请求」，所以非法参数先报；
        # 缺 endpoint 是 Python 侧的额外判空，不能抢在 Marshal 之前。
        _message, result, err = self._call("{not json", endpoint="")
        self.assertIsNone(result)
        self.assertIn("MarshalJSON", str(err))
        self.assertNotIn("endpoint", str(err))

    # -- 合法 JSON：按原文嵌入 ---------------------------------------------
    def test_raw_lexemes_are_preserved(self) -> None:
        arguments = (
            '{"n": 1.50, "e": 1e2, "s": "\\u0041", '
            '"big": 123456789012345678901234567890, "huge": 1e400}'
        )
        _message, result, err = self._call(arguments)
        self.assertIsNone(err)
        self.assertEqual("ok", result.content)
        body = self._body().decode("utf-8")
        # Go 用 RawMessage：数字与转义按模型给的字节原样进 payload（只压掉空白）
        self.assertIn(
            '"arguments":{"n":1.50,"e":1e2,"s":"\\u0041",'
            '"big":123456789012345678901234567890,"huge":1e400}',
            body,
        )
        self.assertNotIn("Infinity", body, "1e400 不能被重写成非法的 Infinity")
        self.assertNotIn("100.0", body, "1e2 不能被重写成 100.0")
        self.assertNotIn('"A"', body, r"\u0041 不能被解码成 A")

    def test_whitespace_outside_strings_is_stripped(self) -> None:
        _message, _result, err = self._call('{ "a" : [ 1 , 2 ] , "b" : "  keep  " }')
        self.assertIsNone(err)
        self.assertIn(
            '"arguments":{"a":[1,2],"b":"  keep  "}',
            self._body().decode("utf-8"),
            "字符串外的空白必须压掉（Go appendCompact），字符串内的原样保留",
        )

    def test_html_escaping_matches_go_append_compact(self) -> None:
        _message, _result, err = self._call('{"a":"<&>"}')
        self.assertIsNone(err)
        self.assertIn(
            '"arguments":{"a":"\\u003c\\u0026\\u003e"}',
            self._body().decode("utf-8"),
            "Go 的 appendCompact 以 escapeHTML=true 转义 < > &",
        )

    def test_line_separator_is_escaped(self) -> None:
        _message, _result, err = self._call('{"a":"x\u2028y"}')
        self.assertIsNone(err)
        self.assertIn('"arguments":{"a":"x\\u2028y"}', self._body().decode("utf-8"))

    def test_escaped_quote_does_not_end_the_string(self) -> None:
        _message, _result, err = self._call('{"a":"q\\"  \\"z", "b" : 1}')
        self.assertIsNone(err)
        self.assertIn('"arguments":{"a":"q\\"  \\"z","b":1}', self._body().decode("utf-8"))

    def test_key_order_matches_go_struct(self) -> None:
        _message, _result, err = self._call('{"a":1}')
        self.assertIsNone(err)
        self.assertTrue(
            self._body().decode("utf-8").startswith('{"tool":"t","event_key":"ev","arguments":'),
            "字段顺序必须与 Go 结构体声明一致：tool → event_key → arguments",
        )

    def test_event_key_omitted_when_empty(self) -> None:
        _message, _result, err = self._call('{"a":1}', event_key="")
        self.assertIsNone(err)
        self.assertTrue(self._body().decode("utf-8").startswith('{"tool":"t","arguments":'))

    def test_blank_arguments_become_empty_object(self) -> None:
        _message, _result, err = self._call("   ")
        self.assertIsNone(err)
        self.assertIn('"arguments":{}', self._body().decode("utf-8"))

    def test_scalar_arguments_are_embedded_raw(self) -> None:
        # Go 的 RawMessage 不要求是对象：``123`` 原样嵌入仍是合法 JSON。
        _message, _result, err = self._call("123")
        self.assertIsNone(err)
        self.assertIn('"arguments":123', self._body().decode("utf-8"))

    def test_payload_is_exactly_one_json_object(self) -> None:
        _message, _result, err = self._call('{"a":1}')
        self.assertIsNone(err)
        parsed = json.loads(self._body().decode("utf-8"))
        self.assertEqual({"tool": "t", "event_key": "ev", "arguments": {"a": 1}}, parsed)


if __name__ == "__main__":
    unittest.main()
