"""``atri_x.artist`` 服务层的单元测试（parser / render / search / store / service）。

对应 Go 测试：``internal/artist`` 下的 ``parser_test.go`` / ``render_test.go`` /
``search_test.go`` / ``store_test.go`` / ``service_test.go`` / ``metadata_store_test.go``。

约定：临时目录统一用仓库根的 ``tmp_artist_service_test/``（``.gitignore`` 已覆盖），
不用 ``tempfile``（本机 ``mkdtemp`` 权限钩子会留残留）。
"""

from __future__ import annotations

import os
import shutil
import unittest
from datetime import datetime, timezone

from atri_x.errors import (
    ErrDisabled,
    ErrForbidden,
    ErrInvalid,
    ErrNotFound,
    ForbiddenError,
    InvalidError,
    NotFoundError,
)
from atri_x.artist import (
    Artist,
    ArtistConflictError,
    Dialect,
    GenerationStore,
    Preset,
    PresetArtist,
    PresetSource,
    PresetStatus,
    ReferencedError,
    Scope,
    Service,
    SourceKind,
    Store,
    Viewer,
    compile_raw,
    extract_artists,
    extract_from_graph,
    format_weight,
    group_scope_key,
    lookup_artist,
    new_artist_id,
    new_preset_id,
    no_metadata_hint,
    render_preset,
    render_tag,
    search,
    rank,
    slugify,
    split_segments,
    unwrap_weight,
    valid_id,
)

TMP_ROOT = os.path.join(os.getcwd(), "tmp_artist_service_test")

FIXED = datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def _library_path() -> str:
    return os.path.join(TMP_ROOT, "artist_library.json")


def _new_store() -> Store:
    store = Store(_library_path())
    store.set_clock(lambda: FIXED)
    return store


def _artist(id: str, tag: str, aliases: list[str] | None = None) -> Artist:
    return Artist(id=id, canonical_tag=tag, aliases=aliases or [])


def _preset(
    id: str = "p1",
    name: str = "组合",
    scope: Scope = Scope.USER,
    scope_id: str = "u1",
    artists: list[PresetArtist] | None = None,
    raw_string: str = "",
) -> Preset:
    return Preset(
        id=id,
        name=name,
        scope=scope,
        scope_id=scope_id,
        dialect=Dialect.SD_WEIGHTED,
        artists=artists if artists is not None else [PresetArtist("sakura", 1.0)],
        raw_string=raw_string,
        source=PresetSource(type=SourceKind.TEXT),
        status=PresetStatus.APPROVED,
    )


class ParserTest(unittest.TestCase):
    def test_split_respects_depth_and_escapes(self) -> None:
        self.assertEqual(split_segments("a, b"), ["a", " b"])
        self.assertEqual(split_segments("(a, b), c"), ["(a, b)", " c"])
        self.assertEqual(split_segments("a\\,b"), ["a\\,b"])
        # 多余的右括号不能吞掉后面的内容（深度夹在 0）。
        self.assertEqual(split_segments("a), b"), ["a)", " b"])

    def test_unwrap_weight_forms(self) -> None:
        self.assertEqual(unwrap_weight("(tag:1.2)"), ("tag", 1.2, True, True))
        self.assertEqual(unwrap_weight("(tag)"), ("tag", 1.1, True, True))
        self.assertEqual(unwrap_weight("{tag}"), ("tag", 1.05, True, True))
        self.assertEqual(unwrap_weight("[tag]"), ("tag", 0.9, True, True))
        # 嵌套权重相乘。
        value, weight, weighted, ok = unwrap_weight("({tag})")
        self.assertTrue(ok)
        self.assertAlmostEqual(weight, 1.1 * 1.05, places=6)
        self.assertEqual(value, "tag")
        self.assertTrue(weighted)

    def test_unwrap_rejects_ambiguous_expressions(self) -> None:
        for raw in ("(a, b)", "(a:b:0.5)", "[a:b]", "(tag:99)", "(tag)"):
            if raw == "(tag)":
                continue
            self.assertEqual(unwrap_weight(raw)[3], False, raw)
        self.assertEqual(unwrap_weight("")[3], False)

    def test_extract_marked_artists(self) -> None:
        out = extract_artists("1girl, artist:sakura, 画师：mika, by someone")
        self.assertEqual([item.name for item in out.artists], ["sakura", "mika", "someone"])
        self.assertEqual(out.source, SourceKind.TEXT)
        self.assertTrue(out.has_original)
        self.assertIn("1girl", out.remaining)

    def test_extract_uses_resolver_for_bare_names(self) -> None:
        out = extract_artists_with_resolver()
        self.assertEqual([item.id for item in out.artists], ["sakura"])

    def test_extract_from_graph_marks_metadata(self) -> None:
        out = extract_from_graph("artist:sakura")
        self.assertEqual(out.source, SourceKind.METADATA)

    def test_duplicate_artist_warns(self) -> None:
        out = extract_artists("artist:sakura, artist:sakura")
        self.assertEqual(len(out.artists), 2)
        self.assertTrue(any("出现了多次" in text for text in out.warnings))

    def test_unparsed_tokens_are_kept_verbatim(self) -> None:
        out = extract_artists("(a, b), artist:x")
        tokens = [item for item in out.tokens if item.kind.value == "unparsed"]
        self.assertEqual([item.raw for item in tokens], ["(a, b)"])


def extract_artists_with_resolver():
    from atri_x.artist import extract_artists_with

    return extract_artists_with("sakura, 1girl", lambda name: ("sakura", True) if name == "sakura" else ("", False))


class RenderTest(unittest.TestCase):
    def test_format_weight_is_compact(self) -> None:
        self.assertEqual(format_weight(1.2), "1.2")
        self.assertEqual(format_weight(1.0), "1")
        self.assertEqual(format_weight(0.0001), "0.01")
        self.assertEqual(format_weight(99), "10")

    def test_render_tag_per_dialect(self) -> None:
        self.assertEqual(render_tag("tag", 1.0, Dialect.SD_WEIGHTED)[0], "tag")
        self.assertEqual(render_tag("tag", 1.2, Dialect.SD_WEIGHTED)[0], "(tag:1.2)")
        # NovelAI 走括号阶梯：1.05^4 ≈ 1.216，最接近 1.2 的可达值。
        self.assertEqual(render_tag("tag", 1.2, Dialect.NAI)[0], "{{{{tag}}}}")
        self.assertEqual(render_tag("tag", 0.9, Dialect.NAI)[0], "[[tag]]")
        rendered, warnings = render_tag("tag", 1.2, Dialect.TAGS)
        self.assertEqual(rendered, "tag")
        self.assertTrue(warnings)
        natural, natural_warnings = render_tag("tag", 1.2, Dialect.NATURAL)
        self.assertEqual(natural, "重点参考 tag")
        self.assertTrue(natural_warnings)

    def test_render_preset_with_slots(self) -> None:
        preset = _preset()
        rendered = render_preset(
            preset, lambda id: ("sakura_tag", True), Dialect.SD_WEIGHTED
        )
        self.assertEqual(rendered.positive, "sakura_tag")
        self.assertEqual(rendered.dialect, Dialect.SD_WEIGHTED)

    def test_render_preset_reports_missing_artist(self) -> None:
        preset = _preset()
        rendered = render_preset(preset, lambda id: ("", False), Dialect.SD_WEIGHTED)
        self.assertTrue(any("已从目录里删除" in text for text in rendered.warnings))

    def test_render_preset_natural_turns_tags_into_clause(self) -> None:
        preset = _preset(artists=[PresetArtist("a", 1.0), PresetArtist("b", 1.0)])
        tags = {"a": "alpha", "b": "beta"}
        rendered = render_preset(preset, lambda id: (tags[id], True), Dialect.NATURAL)
        self.assertEqual(rendered.positive, "画风参考：alpha、beta")


class SearchTest(unittest.TestCase):
    def test_search_requires_all_terms(self) -> None:
        artists = [_artist("sakura", "sakura_paint")]
        self.assertEqual(len(search(artists, [], "sakura", 10)), 1)
        self.assertEqual(search(artists, [], "sakura missing", 10), [])

    def test_rank_keeps_partial_matches(self) -> None:
        artists = [_artist("sakura", "sakura_paint")]
        matches = rank(artists, [], "sakura missing", 10)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].kind, "artist")

    def test_empty_query_lists_everything(self) -> None:
        artists = [_artist("a", "alpha"), _artist("b", "beta")]
        self.assertEqual(len(search(artists, [], "", 10)), 2)

    def test_preset_scoring_uses_artist_tags(self) -> None:
        artists = [_artist("sakura", "sakura_paint")]
        preset = _preset(artists=[PresetArtist("sakura", 1.0)])
        matches = search(artists, [preset], "sakura_paint", 10)
        kinds = {item.kind for item in matches}
        self.assertIn("preset", kinds)

    def test_limit_is_clamped(self) -> None:
        artists = [_artist(f"a{index}", f"alpha{index}") for index in range(60)]
        self.assertEqual(len(search(artists, [], "", 999)), 50)
        self.assertEqual(len(search(artists, [], "", 0)), 10)


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_disabled_store(self) -> None:
        store = Store("")
        self.assertFalse(store.enabled())
        with self.assertRaises(DisabledError := type(ErrDisabled)):
            store.library()

    def test_put_and_read_artist(self) -> None:
        store = _new_store()
        saved = store.put_artist(_artist("sakura", "sakura_paint"))
        self.assertEqual(saved.created_at, FIXED)
        item, found = store.artist("sakura")
        self.assertTrue(found)
        self.assertEqual(item.canonical_tag, "sakura_paint")

    def test_library_round_trip_on_disk(self) -> None:
        store = _new_store()
        store.put_artist(_artist("sakura", "sakura_paint"))
        store.upsert_preset(_preset(), -1)
        reloaded = _new_store()
        library = reloaded.library()
        self.assertEqual(len(library.artists), 1)
        self.assertEqual(len(library.presets), 1)
        self.assertEqual(library.schema, 1)
        # 每次写入都会推进 revision。
        self.assertEqual(library.revision, 2)

    def test_lookup_by_tag_alias_and_id(self) -> None:
        store = _new_store()
        store.put_artist(_artist("sakura", "sakura_paint", aliases=["Sakura"]))
        for name in ("sakura_paint", "SAKURA", "sakura"):
            _item, found = store.lookup(name)
            self.assertTrue(found, name)

    def test_artist_limit_and_validation(self) -> None:
        store = _new_store()
        with self.assertRaises(InvalidError):
            store.put_artist(_artist("bad id", "tag"))
        self.assertFalse(valid_id("a/b"))

    def test_delete_artist_refused_while_referenced(self) -> None:
        store = _new_store()
        store.put_artist(_artist("sakura", "sakura_paint"))
        store.upsert_preset(_preset(), -1)
        with self.assertRaises(ReferencedError) as raised:
            store.delete_artist("sakura")
        self.assertEqual(raised.exception.presets, ["p1"])
        with self.assertRaises(NotFoundError):
            store.delete_artist("missing")

    def test_preset_revision_conflict(self) -> None:
        store = _new_store()
        store.upsert_preset(_preset(), -1)
        with self.assertRaises(ArtistConflictError) as raised:
            store.upsert_preset(_preset(name="改名"), 99)
        self.assertEqual(raised.exception.expected_revision, 99)
        self.assertEqual(raised.exception.current_revision, 1)
        updated = store.upsert_preset(_preset(name="改名"), 1)
        self.assertEqual(updated.revision, 2)

    def test_preset_scope_is_immutable(self) -> None:
        store = _new_store()
        store.upsert_preset(_preset(scope=Scope.USER, scope_id="u1"), -1)
        with self.assertRaises(ForbiddenError):
            store.upsert_preset(_preset(scope=Scope.GROUP, scope_id="qq/g1"), 1)

    def test_delete_preset_checks_revision(self) -> None:
        store = _new_store()
        store.upsert_preset(_preset(), -1)
        with self.assertRaises(ArtistConflictError):
            store.delete_preset("p1", 5)
        removed = store.delete_preset("p1", 1)
        self.assertEqual(removed.id, "p1")
        with self.assertRaises(NotFoundError):
            store.delete_preset("p1", -1)

    def test_corrupt_library_reports_instead_of_resetting(self) -> None:
        os.makedirs(TMP_ROOT, exist_ok=True)
        with open(_library_path(), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("{not json")
        store = _new_store()
        with self.assertRaises(InvalidError):
            store.library()

    def test_resolver_maps_names_to_ids(self) -> None:
        store = _new_store()
        store.put_artist(_artist("sakura", "sakura_paint"))
        resolve = store.resolver()
        self.assertEqual(resolve("Sakura"), ("sakura", True))
        self.assertEqual(resolve("nope"), ("", False))


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.store = _new_store()
        self.store.put_artist(_artist("sakura", "sakura_paint"))
        self.service = Service(self.store)

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def test_visibility_rules(self) -> None:
        self.store.upsert_preset(_preset("global", scope=Scope.GLOBAL, scope_id=""), -1)
        self.store.upsert_preset(_preset("mine", scope=Scope.USER, scope_id="u1"), -1)
        self.store.upsert_preset(_preset("other", scope=Scope.USER, scope_id="u2"), -1)
        self.store.upsert_preset(
            _preset("group", scope=Scope.GROUP, scope_id=group_scope_key("qq", "g1")), -1
        )
        viewer = Viewer(actor_id="u1", platform="qq", scope_id="g1")
        ids = {item.id for item in self.service.visible_presets(viewer)}
        self.assertEqual(ids, {"global", "mine", "group"})
        # 超级管理员也看不到别人的个人组合。
        admin = Viewer(actor_id="root", platform="qq", scope_id="g1", superadmin=True)
        self.assertNotIn("other", {item.id for item in self.service.visible_presets(admin)})

    def test_save_recomputes_scope_id(self) -> None:
        viewer = Viewer(actor_id="u1", platform="qq", scope_id="g1")
        saved = self.service.save(
            viewer, _preset("p1", scope=Scope.USER, scope_id="someone-else"), -1
        )
        self.assertEqual(saved.scope_id, "u1")
        group_preset = _preset("p2", scope=Scope.GROUP, scope_id="")
        saved_group = self.service.save(viewer, group_preset, -1)
        self.assertEqual(saved_group.scope_id, "qq/g1")

    def test_save_global_requires_superadmin(self) -> None:
        viewer = Viewer(actor_id="u1", platform="qq", scope_id="g1")
        with self.assertRaises(ForbiddenError):
            self.service.save(viewer, _preset("g", scope=Scope.GLOBAL, scope_id=""), -1)

    def test_save_group_without_group_context(self) -> None:
        viewer = Viewer(actor_id="u1")
        with self.assertRaises(ForbiddenError):
            self.service.save(viewer, _preset("g", scope=Scope.GROUP, scope_id=""), -1)

    def test_preset_visibility_and_delete(self) -> None:
        self.store.upsert_preset(_preset("p1", scope=Scope.USER, scope_id="u2"), -1)
        viewer = Viewer(actor_id="u1")
        with self.assertRaises(ForbiddenError):
            self.service.preset(viewer, "p1")
        with self.assertRaises(NotFoundError):
            self.service.preset(viewer, "missing")

    def test_compile_preset_renders_dialect(self) -> None:
        viewer = Viewer(actor_id="u1")
        self.store.upsert_preset(_preset("p1", scope=Scope.USER, scope_id="u1"), -1)
        compiled = self.service.compile(viewer, "p1", "", Dialect.SD_WEIGHTED)
        self.assertEqual(compiled.preset_id, "p1")
        self.assertEqual(compiled.positive, "sakura_paint")
        self.assertEqual(compiled.dialect, Dialect.SD_WEIGHTED)

    def test_compile_raw_preserves_unparsed(self) -> None:
        compiled = compile_raw("(a, b), {sakura}", Dialect.SD_WEIGHTED, None)
        # ``(a, b)`` 是拒绝解释的标签组，逐字保留；``{sakura}`` 是 x1.05 的加权标签。
        self.assertEqual(compiled.positive, "(a, b), (sakura:1.05)")
        self.assertEqual(compiled.original, "(a, b), {sakura}")

    def test_compile_rejects_empty_and_unknown_dialect(self) -> None:
        viewer = Viewer(actor_id="u1")
        with self.assertRaises(InvalidError):
            self.service.compile(viewer, "", "", Dialect.SD_WEIGHTED)

        class _BogusDialect:
            def valid(self) -> bool:
                return False

        with self.assertRaises(InvalidError):
            self.service.compile(viewer, "", "x", _BogusDialect())  # type: ignore[arg-type]

    def test_recommend_only_approved(self) -> None:
        viewer = Viewer(actor_id="u1")
        approved = _preset("ok", scope=Scope.USER, scope_id="u1", raw_string="sakura")
        pending = _preset("later", scope=Scope.USER, scope_id="u1", raw_string="sakura")
        pending.status = PresetStatus.PENDING
        self.store.upsert_preset(approved, -1)
        self.store.upsert_preset(pending, -1)
        ids = {item.id for item in self.service.recommend(viewer, "sakura", 5)}
        self.assertEqual(ids, {"ok"})

    def test_extract_text_uses_catalog(self) -> None:
        out = self.service.extract_text("sakura_paint, 1girl")
        self.assertEqual([item.id for item in out.artists], ["sakura"])

    def test_disabled_service(self) -> None:
        service = Service(Store(""))
        with self.assertRaises(type(ErrDisabled)):
            service.visible_presets(Viewer(actor_id="u1"))

    def test_ids_are_deterministic_and_safe(self) -> None:
        self.assertEqual(new_artist_id("Sakura Paint"), "sakura-paint")
        self.assertEqual(new_artist_id("樱花"), new_artist_id("樱花"))
        self.assertTrue(new_artist_id("樱花").startswith("artist-"))
        preset_id = new_preset_id("我的组合")
        self.assertTrue(preset_id.startswith("preset-"))
        self.assertEqual(slugify("A B/C"), "a-b-c")

    def test_no_metadata_hint_mentions_jpeg_specifically(self) -> None:
        self.assertIn("JPEG", no_metadata_hint("image/jpeg"))
        self.assertIn("PNG", no_metadata_hint("image/png"))


class GenerationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.path = os.path.join(TMP_ROOT, "generations.json")

    def tearDown(self) -> None:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    def _store(self) -> GenerationStore:
        store = GenerationStore(self.path)
        store.set_clock(lambda: FIXED)
        return store

    def test_capture_ignores_images_without_metadata(self) -> None:
        store = self._store()

        class Capture:
            media_id = "media:abc"
            original = b"not an image"

        store.capture_generation(None, Capture())
        self.assertEqual(store.count(), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_capture_without_metadata_is_a_no_op(self) -> None:
        # Go 的顺序：先看字节里有没有生成参数，没有就直接返回——此时连 media id
        # 都不检查（没有参数要留档，缺 id 也无所谓）。
        store = self._store()

        class Capture:
            media_id = ""
            original = b"whatever"

        store.capture_generation(None, Capture())
        self.assertEqual(store.count(), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_disabled_store(self) -> None:
        store = GenerationStore("")
        self.assertFalse(store.enabled())
        self.assertEqual(store.count(), 0)
        with self.assertRaises(type(ErrDisabled)):
            store.lookup("media:abc")

    def test_lookup_missing_record(self) -> None:
        store = self._store()
        _record, found = store.lookup("media:missing")
        self.assertFalse(found)

    def test_corrupt_file_is_refused_and_kept(self) -> None:
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("null")
        store = self._store()
        with self.assertRaises(InvalidError):
            store.lookup("media:abc")
        self.assertTrue(os.path.exists(self.path))

    def test_schema_from_the_future_is_refused(self) -> None:
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"schema": 99, "records": []}')
        store = self._store()
        with self.assertRaises(InvalidError):
            store.lookup("media:abc")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
