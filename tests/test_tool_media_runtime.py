"""``atri_x/tool/media_runtime.py`` 的单元测试。

对应 Go 测试：``internal/tool/media_runtime_test.go``。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import unittest

from atri_x.errors import InvalidError
from atri_x.media import Input
from atri_x.storage import Media, MediaReference
from atri_x.tool.media_runtime import (
    MEDIA_INPUTS_DIR,
    MediaCall,
    MediaInput,
    MediaRuntime,
    media_filename,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_tool_media_runtime_test")

MEDIA_A = "media:" + "a" * 64
MEDIA_B = "media:" + "b" * 64


class FakeManager:
    """最小 ``media.Manager`` 假实现（只覆盖 MediaCall 用到的面）。"""

    def __init__(self, payload: bytes = b"payload") -> None:
        self.payload = payload
        self.references: list[MediaReference] = []
        self.removed: list[MediaReference] = []
        self.opened: list[str] = []
        self.imported: list[tuple[int, Input]] = []
        self.metadata_error: BaseException | None = None

    async def metadata(self, ctx, id: str) -> Media:
        del ctx
        if self.metadata_error is not None:
            raise self.metadata_error
        return Media(id=id, name="sample.PNG", mime_type="image/png", size=len(self.payload))

    def add_reference(self, reference: MediaReference) -> None:
        self.references.append(reference)

    def remove_reference(self, reference: MediaReference) -> None:
        self.removed.append(reference)

    async def open(self, ctx, id: str) -> tuple[io.BytesIO, Media]:
        del ctx
        self.opened.append(id)
        return io.BytesIO(self.payload), await self.metadata(None, id)

    async def import_reader(self, ctx, reader, size: int, spec: Input) -> Media:
        del ctx
        self.imported.append((size, spec))
        return Media(id=MEDIA_B, name=spec.name, mime_type=spec.mime_type, size=size)


def _run(coro):
    return asyncio.run(coro)


class MediaInputTest(unittest.TestCase):
    """``MediaInput`` 的字段校验。"""

    def test_accepts_single_media_key(self) -> None:
        parsed = MediaInput.from_dict({"media": MEDIA_A})
        self.assertEqual(parsed.media_id, MEDIA_A)
        self.assertEqual(parsed.to_json(), {"media": MEDIA_A})

    def test_accepts_json_text(self) -> None:
        parsed = MediaInput.from_dict(json.dumps({"media": MEDIA_A}))
        self.assertEqual(parsed.media_id, MEDIA_A)

    def test_rejects_unknown_field(self) -> None:
        with self.assertRaises(InvalidError) as ctx:
            MediaInput.from_dict({"media": MEDIA_A, "path": "/etc/passwd"})
        self.assertIn("path", str(ctx.exception))

    def test_rejects_invalid_id(self) -> None:
        for bad in ("", "media:zz", "x" * 70, MEDIA_A.upper(), None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidError):
                    MediaInput.from_dict({"media": bad})

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(InvalidError):
            MediaInput.from_dict([1, 2])

    def test_prefix_matches_go_constant(self) -> None:
        from atri_x.media import IDPrefix

        self.assertEqual(IDPrefix, "media:")


class MediaFilenameTest(unittest.TestCase):
    """``media_filename`` 的扩展名清洗。"""

    def test_keeps_safe_extension(self) -> None:
        name = media_filename(Media(id=MEDIA_A, name="a.PNG"))
        self.assertEqual(name, "a" * 64 + ".PNG")

    def test_drops_unsafe_extension(self) -> None:
        self.assertEqual(media_filename(Media(id=MEDIA_A, name="a.p/ng")), "a" * 64)
        self.assertEqual(media_filename(Media(id=MEDIA_A, name="a.")), "a" * 64)
        self.assertEqual(media_filename(Media(id=MEDIA_A, name="noext")), "a" * 64)


class MediaCallTest(unittest.TestCase):
    """``MediaCall`` 的引用、导出与导入。"""

    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _runtime(self) -> tuple[MediaRuntime, FakeManager]:
        center = FakeManager()
        return MediaRuntime(center=center, sandbox_root=TMP_ROOT), center

    def test_retain_adds_reference_once(self) -> None:
        runtime, center = self._runtime()
        call = runtime.new_call()

        async def scenario() -> None:
            first = await call.retain(None, MEDIA_A)
            second = await call.retain(None, MEDIA_A)
            self.assertEqual(first.id, MEDIA_A)
            self.assertEqual(second.id, MEDIA_A)

        _run(scenario())
        self.assertEqual(len(center.references), 1)
        ref = center.references[0]
        self.assertEqual(ref.media_id, MEDIA_A)
        self.assertEqual(ref.owner_type, "skill")
        self.assertEqual(ref.purpose, "temporary")
        self.assertEqual(ref.owner_id, call.owner)
        self.assertEqual(len(call.owner), 26)

    def test_retain_requires_center(self) -> None:
        call = MediaRuntime().new_call()
        with self.assertRaises(InvalidError):
            _run(call.retain(None, MEDIA_A))

    def test_retain_rejects_invalid_id(self) -> None:
        runtime, _center = self._runtime()
        with self.assertRaises(InvalidError):
            _run(runtime.new_call().retain(None, "media:bad"))

    def test_close_removes_references_and_workspace(self) -> None:
        runtime, center = self._runtime()
        call = runtime.new_call()

        async def scenario() -> str:
            await call.retain(None, MEDIA_A)
            relative = call.workspace(TMP_ROOT)
            self.assertTrue(os.path.isdir(os.path.join(TMP_ROOT, relative)))
            await call.close()
            return relative

        relative = _run(scenario())
        self.assertEqual(len(center.removed), 1)
        self.assertFalse(os.path.exists(os.path.join(TMP_ROOT, relative)))
        self.assertEqual(call.refs, {})

    def test_export_writes_private_file(self) -> None:
        runtime, _center = self._runtime()
        call = runtime.new_call()

        async def scenario() -> tuple[str, Media]:
            return await call.export(None, MEDIA_A, TMP_ROOT)

        relative, metadata = _run(scenario())
        self.assertEqual(metadata.id, MEDIA_A)
        self.assertTrue(relative.startswith(".media-call-"))
        with open(os.path.join(TMP_ROOT, relative), "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        _run(call.close())

    def test_cached_export_reuses_and_refreshes_mtime(self) -> None:
        runtime, center = self._runtime()
        call = runtime.new_call()

        async def scenario() -> tuple[str, str, float]:
            first = await call.cached_export(None, MEDIA_A)
            os.utime(first, (1, 1))
            stale = os.stat(first).st_mtime
            second = await call.cached_export(None, MEDIA_A)
            return first, second, os.stat(second).st_mtime, stale

        first, second, mtime, stale = _run(scenario())
        self.assertEqual(first, second)
        self.assertEqual(len(center.opened), 1)
        self.assertGreater(mtime, stale)
        self.assertEqual(
            os.path.basename(first), "a" * 64 + ".PNG"
        )
        self.assertIn(MEDIA_INPUTS_DIR, first)
        self.assertNotIn("\\", os.path.dirname(first))
        with open(first.replace("/", os.sep), "rb") as handle:
            self.assertEqual(handle.read(), b"payload")
        _run(call.close())

    def test_cached_export_requires_sandbox(self) -> None:
        # 与 Go 一致：先 Retain（引用已加上）再检查 SandboxRoot，因此抛错时
        # 引用仍然存在，由调用方的 close() 负责回收。
        center = FakeManager()
        call = MediaRuntime(center=center).new_call()
        with self.assertRaises(InvalidError):
            _run(call.cached_export(None, MEDIA_A))
        self.assertEqual(len(center.references), 1)

    def test_cached_export_rejects_directory_in_cache(self) -> None:
        runtime, _center = self._runtime()
        os.makedirs(
            os.path.join(TMP_ROOT, MEDIA_INPUTS_DIR, "a" * 64 + ".PNG"),
            exist_ok=True,
        )
        call = runtime.new_call()
        with self.assertRaises(InvalidError):
            _run(call.cached_export(None, MEDIA_A))

    def test_import_reads_relative_regular_file(self) -> None:
        runtime, center = self._runtime()
        target = os.path.join(TMP_ROOT, "out.png")
        with open(target, "wb") as handle:
            handle.write(b"12345")
        call = runtime.new_call()

        async def scenario() -> Media:
            return await call.import_(None, TMP_ROOT, "out.png", None)

        media = _run(scenario())
        self.assertEqual(media.name, "out.png")
        self.assertEqual(center.imported[0][0], 5)
        self.assertEqual(center.imported[0][1].name, "out.png")

    def test_import_rejects_escaping_paths(self) -> None:
        runtime, _center = self._runtime()
        call = runtime.new_call()
        for bad in ("../out.png", "/abs/out.png", "C:/x.png", "a/../../b.png"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidError):
                    _run(call.import_(None, TMP_ROOT, bad, None))

    def test_import_rejects_missing_or_directory(self) -> None:
        runtime, _center = self._runtime()
        os.makedirs(os.path.join(TMP_ROOT, "sub"), exist_ok=True)
        call = runtime.new_call()
        with self.assertRaises(InvalidError):
            _run(call.import_(None, TMP_ROOT, "missing.png", None))
        with self.assertRaises(InvalidError):
            _run(call.import_(None, TMP_ROOT, "sub", None))

    def test_import_requires_center(self) -> None:
        call = MediaRuntime().new_call()
        with self.assertRaises(InvalidError):
            _run(call.import_(None, TMP_ROOT, "out.png", Input()))

    def test_close_records_non_fatal_failures(self) -> None:
        class BadCenter(FakeManager):
            def remove_reference(self, reference: MediaReference) -> None:
                raise RuntimeError("boom")

        call = MediaRuntime(center=BadCenter(), sandbox_root=TMP_ROOT).new_call()

        async def scenario() -> None:
            await call.retain(None, MEDIA_A)
            await call.close()

        _run(scenario())
        self.assertEqual(len(call.failures), 1)
        self.assertEqual(len(call.refs), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
