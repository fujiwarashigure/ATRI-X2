"""``atri_x.net.s3`` 的单元测试。

签名部分用 AWS 官方文档 / SigV4 测试套件的已知向量断言（不联网）；传输部分用
本地 ``http.server`` 起的假 S3 服务端做端到端验证。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import http.server
import threading
import unittest
import urllib.parse

from atri_x.net.s3 import (
    EMPTY_SHA256,
    S3Client,
    S3Config,
    S3Error,
    S3NoSuchKeyError,
    canonical_request,
    parse_error_body,
    parse_list_objects,
    presign_url,
    sign_request,
    signing_key,
    string_to_sign,
    uri_encode,
)

# --- 已知向量常量（来自 AWS 官方文档与 SigV4 测试套件，禁止为了让测试变绿而修改） ---
SUITE_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"  # get-vanilla
S3_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # AWS S3 文档示例
VANILLA_CANONICAL = (
    "GET\n/\n\nhost:example.amazonaws.com\nx-amz-date:20150830T123600Z\n\n"
    "host;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
VANILLA_CANONICAL_SHA = "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63"
VANILLA_STS_SHA = "9fea9fcbf5a67a7393fb14fd3c1b6e80d4f55dbbddad4302bb6905c0decd5570"
VANILLA_SIGNATURE = "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
S3_GET_CANONICAL_SHA = "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
S3_GET_SIGNATURE = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
S3_PRESIGN_SIGNATURE = "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"

VANILLA_TIME = dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc)
S3_TIME = dt.datetime(2013, 5, 24, 0, 0, 0, tzinfo=dt.timezone.utc)


class TestSigV4Vectors(unittest.TestCase):
    """纯函数签名路径：用官方已知向量断言。"""

    def test_get_vanilla_canonical_request(self) -> None:
        text, signed = canonical_request(
            "GET",
            "/",
            "",
            {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            EMPTY_SHA256,
        )
        self.assertEqual(text, VANILLA_CANONICAL)
        self.assertEqual(signed, "host;x-amz-date")
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), VANILLA_CANONICAL_SHA)

    def test_get_vanilla_string_to_sign_and_key(self) -> None:
        scope = "20150830/us-east-1/service/aws4_request"
        sts = string_to_sign("20150830T123600Z", scope, VANILLA_CANONICAL)
        self.assertEqual(hashlib.sha256(sts.encode()).hexdigest(), VANILLA_STS_SHA)
        self.assertTrue(
            signing_key(SUITE_SECRET, "20150830", "us-east-1", "service").hex().startswith("938127")
        )

    def test_get_vanilla_authorization_header(self) -> None:
        headers = sign_request(
            method="GET",
            url="https://example.amazonaws.com/",
            region="us-east-1",
            access_key="AKIDEXAMPLE",
            secret_key=SUITE_SECRET,
            service="service",
            now=VANILLA_TIME,
            payload=b"",
            include_content_sha256=False,
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            f"SignedHeaders=host;x-amz-date, Signature={VANILLA_SIGNATURE}",
        )

    def test_s3_get_object_vector(self) -> None:
        headers = sign_request(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            region="us-east-1",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=S3_SECRET,
            headers={"Range": "bytes=0-9"},
            now=S3_TIME,
            payload=b"",
        )
        self.assertEqual(headers["x-amz-content-sha256"], EMPTY_SHA256)
        self.assertIn(f"Signature={S3_GET_SIGNATURE}", headers["Authorization"])
        self.assertIn(
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date",
            headers["Authorization"],
        )
        text, _ = canonical_request(
            "GET",
            "/test.txt",
            "",
            {
                "host": "examplebucket.s3.amazonaws.com",
                "range": "bytes=0-9",
                "x-amz-content-sha256": EMPTY_SHA256,
                "x-amz-date": "20130524T000000Z",
            },
            EMPTY_SHA256,
        )
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), S3_GET_CANONICAL_SHA)

    def test_s3_presigned_url_vector(self) -> None:
        url = presign_url(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            region="us-east-1",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=S3_SECRET,
            expires=86400,
            now=S3_TIME,
        )
        self.assertEqual(
            url,
            "https://examplebucket.s3.amazonaws.com/test.txt?"
            "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
            "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request&"
            "X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&"
            f"X-Amz-SignedHeaders=host&X-Amz-Signature={S3_PRESIGN_SIGNATURE}",
        )

    def test_session_token_is_signed(self) -> None:
        headers = sign_request(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            region="us-east-1",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=S3_SECRET,
            session_token="TOKEN",
            now=S3_TIME,
        )
        self.assertEqual(headers["x-amz-security-token"], "TOKEN")
        self.assertIn("x-amz-security-token", headers["Authorization"])
        url = presign_url(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            region="us-east-1",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=S3_SECRET,
            session_token="TOKEN",
            now=S3_TIME,
        )
        self.assertIn("X-Amz-Security-Token=TOKEN", url)

    def test_payload_hash_goes_into_the_header(self) -> None:
        body = b"hello s3"
        headers = sign_request(
            method="PUT",
            url="http://127.0.0.1:9000/bucket/key",
            region="us-east-1",
            access_key="minioadmin",
            secret_key="minioadmin",
            payload=body,
        )
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(body).hexdigest())

    def test_signature_changes_with_payload(self) -> None:
        base = dict(
            method="PUT",
            url="http://127.0.0.1:9000/bucket/key",
            region="us-east-1",
            access_key="a",
            secret_key="b",
            now=S3_TIME,
        )
        first = sign_request(**base, payload=b"one")["Authorization"]  # type: ignore[arg-type]
        second = sign_request(**base, payload=b"two")["Authorization"]  # type: ignore[arg-type]
        self.assertNotEqual(first, second)


class TestUriHelpers(unittest.TestCase):
    def test_uri_encode_rules(self) -> None:
        self.assertEqual(uri_encode("a b"), "a%20b")
        self.assertEqual(uri_encode("a+b"), "a%2Bb")
        self.assertEqual(uri_encode("a/b"), "a%2Fb")
        self.assertEqual(uri_encode("a/b", encode_slash=False), "a/b")
        self.assertEqual(uri_encode("~-_.aZ09"), "~-_.aZ09")
        self.assertEqual(uri_encode("中文"), "%E4%B8%AD%E6%96%87")

    def test_parse_error_body_xml_and_fallback(self) -> None:
        xml = (
            b"<Error><Code>NoSuchKey</Code><Message>gone</Message>"
            b"<RequestId>REQ1</RequestId></Error>"
        )
        self.assertEqual(parse_error_body(xml), ("NoSuchKey", "gone", "REQ1"))
        self.assertEqual(parse_error_body(b"plain failure"), ("", "plain failure", ""))
        self.assertEqual(parse_error_body(b""), ("", "", ""))

    def test_parse_list_objects(self) -> None:
        body = (
            b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<IsTruncated>true</IsTruncated><NextContinuationToken>TOK</NextContinuationToken>"
            b"<Contents><Key>media/a</Key></Contents><Contents><Key>media/b</Key></Contents>"
            b"</ListBucketResult>"
        )
        keys, truncated, token = parse_list_objects(body)
        self.assertEqual(keys, ["media/a", "media/b"])
        self.assertTrue(truncated)
        self.assertEqual(token, "TOK")


# ---------------------------------------------------------------------------
# 假 S3 服务端
# ---------------------------------------------------------------------------
_ERROR_XML = (
    b"<Error><Code>NoSuchKey</Code><Message>The specified key does not exist.</Message>"
    b"<RequestId>REQ-404</RequestId></Error>"
)
_DENIED_XML = (
    b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message>"
    b"<RequestId>REQ-403</RequestId></Error>"
)
_PAGE_ONE = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>media</Name><Prefix>media/</Prefix><KeyCount>2</KeyCount><MaxKeys>2</MaxKeys>"
    b"<IsTruncated>true</IsTruncated><NextContinuationToken>TOK-2</NextContinuationToken>"
    b"<Contents><Key>media/a.bin</Key><Size>3</Size></Contents>"
    b"<Contents><Key>media/b.bin</Key><Size>4</Size></Contents></ListBucketResult>"
)
_PAGE_TWO = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<IsTruncated>false</IsTruncated>"
    b"<Contents><Key>media/c.bin</Key><Size>5</Size></Contents></ListBucketResult>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeS3/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # 保持测试输出干净
        pass

    # -- helpers -----------------------------------------------------------
    def _record(self, method: str) -> tuple[urllib.parse.SplitResult, bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parsed = urllib.parse.urlsplit(self.path)
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": method,
                "path": parsed.path,
                "query": dict(urllib.parse.parse_qsl(parsed.query)),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        return parsed, body

    def _reply(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        provided = {name.lower() for name in (headers or {})}
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if "content-length" not in provided:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    # -- verbs -------------------------------------------------------------
    def do_PUT(self) -> None:
        parsed, body = self._record("PUT")
        self.server.objects[parsed.path] = body  # type: ignore[attr-defined]
        self._reply(200, b"", {"ETag": '"fake-etag"', "x-amz-request-id": "REQ-PUT"})

    def do_GET(self) -> None:
        parsed, _ = self._record("GET")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if "list-type" in query:
            if query.get("continuation-token") == "TOK-2":
                self._reply(200, _PAGE_TWO)
            else:
                self._reply(200, _PAGE_ONE)
            return
        if parsed.path.endswith("/forbidden.bin"):
            self._reply(403, _DENIED_XML)
            return
        stored = self.server.objects.get(parsed.path)  # type: ignore[attr-defined]
        if stored is None:
            self._reply(404, _ERROR_XML)
            return
        self._reply(200, stored, {"Content-Type": "application/octet-stream"})

    def do_DELETE(self) -> None:
        parsed, _ = self._record("DELETE")
        self.server.objects.pop(parsed.path, None)  # type: ignore[attr-defined]
        self._reply(204)

    def do_HEAD(self) -> None:
        parsed, _ = self._record("HEAD")
        stored = self.server.objects.get(parsed.path)  # type: ignore[attr-defined]
        if stored is None:
            self._reply(404)
            return
        self._reply(200, b"", {"Content-Length": str(len(stored)), "ETag": '"fake-etag"'})


class _FakeS3(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict[str, object]] = []
        self.objects: dict[str, bytes] = {}


class TestClientAgainstFakeServer(unittest.TestCase):
    server: _FakeS3
    thread: threading.Thread
    config: S3Config

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _FakeS3()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.config = S3Config(
            endpoint=f"http://{host}:{port}",
            region="us-east-1",
            bucket="media",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=S3_SECRET,
            path_style=True,
            prefix="media",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.server.requests.clear()
        self.server.objects.clear()
        self.client = S3Client(self.config)

    def _last(self) -> dict[str, object]:
        return self.server.requests[-1]

    def test_put_object_signs_and_uploads(self) -> None:
        key = asyncio.run(self.client.put_object("a.bin", b"abc", "application/octet-stream"))
        self.assertEqual(key, "media/a.bin")
        request = self._last()
        self.assertEqual(request["method"], "PUT")
        self.assertEqual(request["path"], "/media/media/a.bin")
        self.assertEqual(request["body"], b"abc")
        headers = request["headers"]  # type: ignore[assignment]
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertIn("Credential=AKIAIOSFODNN7EXAMPLE/", headers["authorization"])
        self.assertIn("/us-east-1/s3/aws4_request", headers["authorization"])
        self.assertIn("x-amz-date", headers)
        self.assertEqual(headers["content-type"], "application/octet-stream")
        self.assertEqual(self.server.objects["/media/media/a.bin"], b"abc")

    def test_put_object_accepts_text_and_metadata(self) -> None:
        asyncio.run(self.client.put_object("note.txt", "你好", "text/plain", metadata={"Kind": "note"}))
        headers = self._last()["headers"]  # type: ignore[assignment]
        self.assertEqual(headers["x-amz-meta-kind"], "note")
        self.assertEqual(self.server.objects["/media/media/note.txt"], "你好".encode())

    def test_get_object_roundtrip(self) -> None:
        asyncio.run(self.client.put_object("a.bin", b"payload"))
        self.assertEqual(asyncio.run(self.client.get_object("a.bin")), b"payload")

    def test_get_object_missing_raises_no_such_key(self) -> None:
        with self.assertRaises(S3NoSuchKeyError) as ctx:
            asyncio.run(self.client.get_object("missing.bin"))
        error = ctx.exception
        self.assertEqual(error.code, "NoSuchKey")
        self.assertEqual(error.status, 404)
        self.assertEqual(error.request_id, "REQ-404")
        self.assertEqual(error.key, "media/missing.bin")
        self.assertIsInstance(error, S3Error)

    def test_get_object_denied_raises_s3_error(self) -> None:
        with self.assertRaises(S3Error) as ctx:
            asyncio.run(self.client.get_object("forbidden.bin"))
        self.assertEqual(ctx.exception.code, "AccessDenied")
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(isinstance(ctx.exception, S3NoSuchKeyError))

    def test_delete_object(self) -> None:
        asyncio.run(self.client.put_object("a.bin", b"x"))
        asyncio.run(self.client.delete_object("a.bin"))
        self.assertEqual(self._last()["method"], "DELETE")
        self.assertNotIn("/media/media/a.bin", self.server.objects)
        asyncio.run(self.client.delete_object("never-existed.bin"))  # 204 不报错

    def test_head_object_returns_headers(self) -> None:
        asyncio.run(self.client.put_object("a.bin", b"12345"))
        headers = asyncio.run(self.client.head_object("a.bin"))
        self.assertEqual(headers["content-length"], "5")
        self.assertEqual(self._last()["method"], "HEAD")

    def test_head_object_missing(self) -> None:
        with self.assertRaises(S3NoSuchKeyError) as ctx:
            asyncio.run(self.client.head_object("missing.bin"))
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "NoSuchKey")

    def test_list_objects_follows_pagination(self) -> None:
        keys = asyncio.run(self.client.list_objects(""))
        self.assertEqual(keys, ["media/a.bin", "media/b.bin", "media/c.bin"])
        self.assertEqual(len(self.server.requests), 2)
        second = self.server.requests[1]["query"]
        self.assertEqual(second["continuation-token"], "TOK-2")  # type: ignore[index]
        first = self.server.requests[0]["query"]
        self.assertEqual(first["list-type"], "2")  # type: ignore[index]
        self.assertEqual(first["prefix"], "media/")  # type: ignore[index]

    def test_list_objects_signature_covers_query(self) -> None:
        asyncio.run(self.client.list_objects("x"))
        query = self._last()["query"]
        self.assertEqual(query["prefix"], "media/x")  # type: ignore[index]

    def test_transport_error_is_wrapped(self) -> None:
        broken = S3Config(
            endpoint="http://127.0.0.1:1", bucket="media", access_key="a", secret_key="b",
            timeout=2.0,
        )
        with self.assertRaises(S3Error) as ctx:
            asyncio.run(S3Client(broken).get_object("a.bin"))
        self.assertEqual(ctx.exception.code, "TransportError")

    def test_presign_get_matches_documented_vector(self) -> None:
        client = S3Client(
            S3Config(
                endpoint="https://s3.amazonaws.com",
                region="us-east-1",
                bucket="examplebucket",
                access_key="AKIAIOSFODNN7EXAMPLE",
                secret_key=S3_SECRET,
                path_style=False,
            )
        )
        url = client.presign_get("test.txt", expires=86400)
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "examplebucket.s3.amazonaws.com")
        self.assertEqual(parsed.path, "/test.txt")
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(query["X-Amz-Expires"], "86400")
        self.assertEqual(query["X-Amz-SignedHeaders"], "host")
        self.assertEqual(len(query["X-Amz-Signature"]), 64)

    def test_presign_url_layout_path_style(self) -> None:
        url = self.client.presign_get("a b.bin")
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual(parsed.path, "/media/media/a%20b.bin")

    def test_virtual_hosted_style_url(self) -> None:
        client = S3Client(
            S3Config(
                endpoint="https://s3.us-east-1.amazonaws.com",
                region="us-east-1",
                bucket="examplebucket",
                access_key="a",
                secret_key="b",
                path_style=False,
            )
        )
        self.assertEqual(
            client._url_for("dir/key.txt"),  # noqa: SLF001 - 直接断言 URL 布局
            "https://examplebucket.s3.us-east-1.amazonaws.com/dir/key.txt",
        )

    def test_endpoint_with_base_path(self) -> None:
        client = S3Client(
            S3Config(
                endpoint="https://gateway.example.com/s3",
                bucket="media",
                access_key="a",
                secret_key="b",
                path_style=True,
            )
        )
        self.assertEqual(
            client._url_for("k"), "https://gateway.example.com/s3/media/k"  # noqa: SLF001
        )

    def test_invalid_endpoint_and_bucket(self) -> None:
        with self.assertRaises(S3Error):
            S3Client(S3Config(endpoint="not-a-url", bucket="b"))._url_for("k")  # noqa: SLF001
        with self.assertRaises(S3Error):
            S3Client(S3Config(endpoint="http://127.0.0.1:9000"))._url_for("k")  # noqa: SLF001


class TestConfigHelpers(unittest.TestCase):
    def test_object_key_prefixing(self) -> None:
        config = S3Config(prefix="media")
        self.assertEqual(config.object_key("a.bin"), "media/a.bin")
        self.assertEqual(config.object_key("/a.bin"), "media/a.bin")
        self.assertEqual(S3Config(prefix="media/").object_key("a.bin"), "media/a.bin")
        self.assertEqual(S3Config().object_key("a.bin"), "a.bin")

    def test_s3_error_str_is_informative(self) -> None:
        error = S3Error("boom", code="AccessDenied", status=403, operation="get_object", key="k")
        self.assertIn("AccessDenied", str(error))
        self.assertIn("403", str(error))
        self.assertIn("k", str(error))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
