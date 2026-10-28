"""Synthetic Native page inputs. Every map call forbids I/O and mutation."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Variant, Work, validate_work
from .services.provider_adapters import ProviderAdapter, RawMapper
from .services.provider_adapters.recordings import check, load_recording
from .services.provider_adapters.xiaohongshu import XiaohongshuMapper, MAX_HTML_CHARS, MAX_DOM_DEPTH, MAX_DOM_NODES
from .tests_provider_adapters import deny_map_io


ID = "64a1b2c3d4e5f6789012abcd"
OTHER = "65b2c3d4e5f6789012abcdef"
SOURCE = "https://www.xiaohongshu.com/explore/" + ID
IMAGE = "http://sns-webpic-qc.xhscdn.com/image.jpg?x=1&y=2"
VIDEO = "https://sns-video-bd.xhscdn.com/video.mp4"
PREFIX = "xhs_native:page_state.note.noteDetailMap.<note_id>.note"
FIXTURES = Path(__file__).parent / "fixtures/provider_raw/xhs_native"


def fixture(name):
    document = load_recording(FIXTURES / ("synthetic_" + name + ".json"))
    check(document)
    assert document["provenance"]["kind"] == "synthetic"
    return document["raw"]


def state(note=None, key=ID):
    return {"note": {"noteDetailMap": {key: {"note": note if note is not None else {
        "noteId": ID, "type": "normal", "imageList": [{"urlDefault": IMAGE}],
    }}}}}


def current(raw):
    return raw["note"]["noteDetailMap"][ID]["note"]


def html(body, *, canonical=SOURCE, script=None):
    return (f'<html><head><link rel="canonical" href="{canonical}"></head><body>'
            + body + ("<script>window.__INITIAL_STATE__=" + json.dumps(script) + ";</script>" if script else "")
            + "</body></html>")


class XiaohongshuMappingTests(TestCase):
    def map(self, raw, source=SOURCE):
        before = deepcopy(raw)
        with deny_map_io():
            result = XiaohongshuMapper().map(raw, source)
        self.assertEqual(raw, before)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
            self.assertEqual((result.platform, result.provider), ("", ""))
            for field in Work.__dataclass_fields__:
                if field != "field_sources":
                    self.assertTrue(result.field_sources[field], field)
            for item in result.items:
                self.assertIn(item.default_variant, item.variants)
                for field in ("item_id", "kind", "index", "variants", "default_variant"):
                    self.assertTrue(result.field_sources[f"items.{item.item_id}.{field}"])
                for variant in item.variants:
                    for field in Variant.__dataclass_fields__:
                        self.assertTrue(self.origin(result, item, variant, field), field)
            with self.assertRaises(TypeError):
                result.field_sources["work_id"] = "mutated"
        return result

    def origin(self, work, item, variant, field):
        return work.field_sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"]

    def failure(self, raw, code, kind=FailureKind.INVALID_RESULT, source=SOURCE):
        self.assertEqual(self.map(raw, source), Failure(kind, code))

    def test_structured_media_is_not_reordered_or_enriched_by_dom(self):
        raw = fixture("gallery")
        before = self.map(raw)
        body = '<div class="note-content"><img src="https://sns-webpic-qc.xhscdn.com/ordinary.heic"><img src="https://example.test/unrelated.jpg"></div>'
        work = self.map({"page_state": raw, "html": html(body)})
        self.assertIsInstance(work, Work)
        self.assertEqual(work.items, before.items)
        self.assertEqual(work.groups, before.groups)

    def test_dom_body_leaves_out_the_date_and_location_line(self):
        raw = state({"noteId": ID, "type": "normal", "title": "Known current note", "imageList": [{"urlDefault": IMAGE}]})
        body = ('<div class="note-content"><div id="detail-title" class="title">Known current note</div>'
                '<div id="detail-desc" class="desc"><span class="note-text">Body text #tag</span></div>'
                '<div class="bottom-container"><span class="date">2天前 四川</span></div></div>')
        work = self.map({"page_state": raw, "html": html(body)})
        self.assertIsInstance(work, Work)
        self.assertIn("Body text", work.description)
        self.assertNotIn("四川", work.description)
        self.assertNotIn("2天前", work.description)

    def test_dom_fallback_inherits_current_note_media_constraints(self):
        raw = state({"noteId": ID, "type": "normal", "title": "Known current note", "requires_cookie": True, "ip_bound": True})
        work = self.map({"page_state": raw, "html": html('<div class="note-content"><img src="' + IMAGE + '"></div>')})
        self.assertIsInstance(work, Work)
        variant = work.items[0].default_variant
        self.assertEqual(variant.required_headers, ("cookie",))
        self.assertIs(variant.browser_fetchable, Fetchable.FALSE)

    def test_only_raw_mapper_no_old_projection_or_helpers(self):
        mapper = XiaohongshuMapper()
        self.assertIsInstance(mapper, RawMapper)
        self.assertNotIsInstance(mapper, ProviderAdapter)
        self.assertFalse(hasattr(mapper, "fetch"))
        self.assertFalse(hasattr(mapper, "parse"))
        with patch("xhs_download.services.platforms.xiaohongshu.XiaohongshuAdapter.parse_html", side_effect=AssertionError("old projection")), \
             patch("xhs_download.services.platforms.xhs_page_state._select_current_note", side_effect=AssertionError("unverified selection")), \
             patch("xhs_download.services.platforms.xiaohongshu.normalize_xhs_image_url", side_effect=AssertionError("URL rewrite")), \
             patch("xhs_download.services.platforms.xiaohongshu.resolve_xhs_fallback_policy", side_effect=AssertionError("old policy")):
            self.assertIsInstance(self.map(state()), Work)
        for raw in ({"assets": [], "metadata": {}}, {"image_urls": [IMAGE]}, {"items": []}, None):
            self.failure(raw, "XHS_NOT_RAW_PAGE")

    def test_gallery_native_order_live_pair_and_complete_variants(self):
        work = self.map(fixture("gallery"))
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.VIDEO, Kind.IMAGE])
        self.assertEqual([len(item.variants) for item in work.items], [3, 2, 1])
        self.assertEqual(work.groups[0].item_ids, ("image:1", "video:2"))
        self.assertEqual(work.groups[0].kind, "live_photo")
        self.assertEqual((work.work_id, work.title, work.author, work.description, work.topics),
                         (ID, "Native gallery", "Synthetic creator", "First paragraph\nSecond paragraph", ("travel", "plain")))
        self.assertEqual(work.field_sources["work_id"], PREFIX + ".noteId")
        for field, suffix in (("title", ".title"), ("description", ".desc"), ("topics", ".desc"), ("author", ".user.nickname")):
            self.assertEqual(work.field_sources[field], PREFIX + suffix)
        image, motion, _ = work.items
        self.assertEqual((image.default_variant.width, image.default_variant.height, image.default_variant.is_original), (2400, 1600, True))
        self.assertEqual((motion.default_variant.width, motion.default_variant.height, motion.default_variant.fps,
                          motion.default_variant.size_bytes, motion.default_variant.size_exact), (1080, 1920, 30, 12345, True))
        self.assertIsNone(motion.default_variant.bitrate)
        self.assertIs(motion.default_variant.tracks, Tracks.UNKNOWN)
        self.assertEqual(motion.default_variant.merge_with, "")

    def test_scalar_sources_and_explicit_false_not_default(self):
        raw = state()
        row = current(raw)["imageList"][0]
        row.update(width=100, height=200, fps=24, size=123, size_exact=False,
                   drm=False, segmented=False, is_original=False, required_headers=[], request_method="GET")
        work = self.map(raw)
        item, variant = work.items[0], work.items[0].default_variant
        path = PREFIX + ".imageList[0]"
        for field in ("width", "height", "fps", "size_exact", "drm", "segmented", "is_original"):
            self.assertEqual(self.origin(work, item, variant, field), path + "." + field)
        self.assertEqual(self.origin(work, item, variant, "size_bytes"), path + ".size")
        self.assertEqual(self.origin(work, item, variant, "required_headers"), path)
        self.assertEqual(self.origin(work, item, variant, "request_method"), path)
        self.assertEqual(variant.host_policy, ("xiaohongshu.com", "xhscdn.com"))
        self.assertEqual(self.origin(work, item, variant, "host_policy"), "existing:XHS_MEDIA_HOST_SUFFIXES")
        self.assertEqual(variant.container, "jpg")
        self.assertEqual(self.origin(work, item, variant, "container"), "adapter:url_extension")
        self.assertEqual(variant.url, IMAGE)

    def test_missing_defaults_remain_missing(self):
        work = self.map(state())
        item, variant = work.items[0], work.items[0].default_variant
        self.assertEqual((work.title, work.description, work.author, work.cover_url, work.groups), ("", "", "", "", ()))
        self.assertIsNone(work.duration_seconds)
        for field in ("width", "height", "fps", "bitrate", "size_bytes", "expires_at"):
            self.assertIsNone(getattr(variant, field))
            self.assertEqual(self.origin(work, item, variant, field), "contract:default")
        self.assertIs(variant.tracks, Tracks.UNKNOWN)
        self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
        self.assertEqual((variant.required_headers, variant.drm, variant.segmented, variant.is_original, variant.size_exact), ((), False, False, False, False))

    def test_preview_dimensions_never_borrow_root(self):
        raw = state()
        current(raw)["imageList"][0].update(width=3000, height=2000, infoList=[
            {"url": IMAGE}, {"url": "https://sns-webpic-qc.xhscdn.com/preview.webp"}])
        variants = self.map(raw).items[0].variants
        self.assertEqual([(v.width, v.height) for v in variants], [(3000, 2000), (3000, 2000), (None, None)])

    def test_native_url_pre_retained_without_root_dimensions(self):
        raw = state()
        row = current(raw)["imageList"][0]
        row.update(width=3000, height=2000, urlPre="http://sns-webpic-qc.xhscdn.com/preview.webp")
        variants = self.map(raw).items[0].variants
        self.assertEqual([v.url for v in variants], [IMAGE, row["urlPre"]])
        self.assertEqual((variants[1].width, variants[1].height), (None, None))
        del row["urlDefault"]
        self.assertEqual(self.map(raw).items[0].default_variant.url, row["urlPre"])

    def test_consumer_origin_key_is_not_expanded_but_actual_url_is_retained(self):
        raw = state({"noteId": ID, "type": "video", "video": {"consumer": {"originVideoKey": "synthetic-bare-key"}}})
        self.failure(raw, "XHS_NO_MEDIA")
        current(raw)["video"]["consumer"]["originVideoKey"] = VIDEO
        self.assertEqual(self.map(raw).items[0].default_variant.url, VIDEO)

    def test_valid_json_recommendation_map_is_not_promoted_by_target_slice(self):
        raw = {"recommendations": {ID: {"note": {"noteId": ID, "type": "normal", "imageList": [{"urlDefault": IMAGE}]}}}}
        self.failure(html("", script=raw), "XHS_NO_MEDIA")
        script = '<script>window.__INITIAL_STATE__={"missing":undefined,"recommendations":' + json.dumps(raw["recommendations"]) + '};</script>'
        self.failure(script, "XHS_NO_MEDIA")

    def test_repeated_image_list_positions_are_not_deduplicated(self):
        raw = state()
        current(raw)["imageList"] *= 2
        work = self.map(raw)
        self.assertEqual(len(work.items), 2)
        self.assertEqual([item.index for item in work.items], [1, 2])

    def test_video_image_list_is_cover_not_gallery(self):
        work = self.map(fixture("video"))
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO])
        self.assertEqual(len(work.items[0].variants), 3)
        self.assertEqual(work.cover_url, "http://sns-webpic-qc.xhscdn.com/cover.webp")
        self.assertEqual(work.field_sources["cover_url"], PREFIX + ".imageList[0].urlDefault")
        self.assertEqual(work.items[0].default_variant.height, 1080)
        self.assertEqual([(v.width, v.height) for v in work.items[0].variants], [(1280, 720), (None, 1080), (None, None)])
        self.assertIsNone(work.duration_seconds)
        self.assertTrue(all(v.bitrate is None and not v.required_headers for v in work.items[0].variants))

    def test_explicit_duration_units_and_missing_units(self):
        for fields, expected in (({"duration": 12000}, None), ({"duration_ms": 12500}, 12.5), ({"duration_seconds": 12.5}, 12.5)):
            raw = fixture("video")
            video = current(raw["page_state"])["video"]
            video.pop("duration")
            video.update(fields)
            self.assertEqual(self.map(raw).duration_seconds, expected)

    def test_live_flag_requires_same_item_not_parallel_lists(self):
        raw = fixture("gallery")
        row = current(raw)["imageList"][0]
        row["livePhoto"] = False
        work = self.map(raw)
        self.assertEqual(work.groups, ())
        self.assertEqual(len(work.items), 3)
        row.pop("stream")
        row["livePhoto"] = True
        self.assertEqual(self.map(raw).groups, ())

    def test_alias_restrictions_union_but_metadata_stays_local(self):
        raw = state()
        row = current(raw)["imageList"][0]
        row.update(infoList=[{"url": IMAGE, "width": 100, "drm": True, "request_method": "POST", "expires_at": 2000000000},
                             {"url": IMAGE, "width": 200, "segmented": True, "expires_at": 1900000000}])
        work = self.map(raw)
        self.assertEqual([v.width for v in work.items[0].variants], [None, 100, 200])
        for variant in work.items[0].variants:
            self.assertTrue(variant.drm)
            self.assertTrue(variant.segmented)
            self.assertEqual(variant.request_method, "POST")
            self.assertEqual(variant.expires_at, datetime.fromtimestamp(1900000000, timezone.utc))

    def test_alias_missing_context_is_not_completed_by_another_observation(self):
        for aliases in ([{"urlDefault": IMAGE, "headers_present": True, "headers": {"X-Access": None}},
                         {"urlDefault": IMAGE, "headers": {"Referer": SOURCE}}],
                        [{"urlDefault": IMAGE, "headers": {"Referer": SOURCE}},
                         {"urlDefault": IMAGE, "headers_present": True, "headers": {"X-Access": ""}}]):
            raw = state()
            current(raw)["imageList"] = aliases
            work = self.map(raw)
            self.assertTrue(all(item.default_variant.browser_fetchable is Fetchable.FALSE for item in work.items))

    def test_root_url_context_does_not_leak_to_preview_or_motion(self):
        raw = fixture("gallery")
        current(raw)["imageList"][0].update(required_headers=["X-Still"], ip_bound=True)
        work = self.map(raw)
        self.assertEqual(work.items[0].variants[0].required_headers, ("x-still",))
        self.assertEqual(work.items[0].variants[1].required_headers, ("x-still",))
        self.assertEqual(work.items[0].variants[2].required_headers, ())
        self.assertTrue(all(v.required_headers == () and v.browser_fetchable is Fetchable.UNKNOWN for v in work.items[1].variants))

    def test_headers_customary_explicit_credentials_and_no_values(self):
        cases = [({"headers": {"Referer": SOURCE, "Origin": SOURCE, "User-Agent": "plain"}}, ()),
                 ({"headers": {"Referer": SOURCE}, "required_headers": ["Referer", "Origin"]}, ("origin", "referer")),
                 ({"headers": {"Cookie": "secret", "X-Access": "secret"}}, ("cookie", "x-access"))]
        for fields, expected in cases:
            raw = state()
            current(raw)["imageList"][0].update(fields)
            work = self.map(raw)
            self.assertEqual(work.items[0].default_variant.required_headers, expected)
            self.assertNotIn("secret", repr(work))

    def test_ip_and_missing_context_false(self):
        for fields in ({"ip_bound": True}, {"requires_context": True}, {"headers_present": True},
                       {"headers_present": True, "headers": {"X-Access": None}},
                       {"headers_present": True, "headers": {"X-Access": ""}}):
            raw = state()
            current(raw).update(fields)
            self.assertIs(self.map(raw).items[0].default_variant.browser_fetchable, Fetchable.FALSE)

    def test_segment_paths_and_unknown_tracks(self):
        for suffix in ("m3u8", "mpd", "m4s"):
            raw = state({"noteId": ID, "type": "video", "video": {"media": {"stream": {"h264": [{"masterUrl": VIDEO + "." + suffix}]}}}})
            work = self.map(raw)
            self.assertTrue(work.items[0].default_variant.segmented)
            self.assertIs(work.items[0].default_variant.tracks, Tracks.UNKNOWN)

    def test_unpaired_tracks_are_not_guessed(self):
        raw = fixture("video")
        streams = current(raw["page_state"])["video"]["media"]["stream"]
        for variants in streams.values():
            for variant in variants:
                variant["tracks"] = "video_only"
        self.failure(raw, "XHS_ITEM_WITHOUT_DEFAULT")
        streams["h264"][0]["tracks"] = "av"
        work = self.map(raw)
        self.assertIs(work.items[0].default_variant.tracks, Tracks.AV)
        self.assertTrue(all(v.merge_with == "" for v in work.items[0].variants))

    def test_identity_key_vs_note_id_mismatch_both_directions(self):
        for key, value, source in ((OTHER, ID, SOURCE), (ID, OTHER, SOURCE.replace(ID, OTHER)), (ID, OTHER, SOURCE)):
            raw = state({"noteId": value, "type": "normal", "imageList": [{"urlDefault": IMAGE}]}, key)
            self.failure(raw, "XHS_IDENTITY_MISMATCH", source=source)
            self.failure(html('<div class="note-content"><img src="' + IMAGE + '"></div>', canonical=source, script=raw),
                         "XHS_IDENTITY_MISMATCH", source=source)

    def test_matching_other_notes_not_merged_and_request_not_provider_identity(self):
        raw = state()
        raw["note"]["noteDetailMap"][OTHER] = {"note": {"noteId": OTHER, "title": "Other", "type": "normal", "imageList": [{"urlDefault": VIDEO}]}}
        self.assertEqual(len(self.map(raw).items), 1)
        current(raw).pop("noteId")
        self.assertTrue(self.map(raw).field_sources["work_id"].endswith(":map_key"))

    def test_short_source_actual_page_and_conflicting_final_identity(self):
        work = self.map({"page_state": state(), "page_url": SOURCE}, "https://xhslink.cn/o/opaque")
        self.assertEqual(work.source_url, SOURCE)
        self.assertEqual(work.field_sources["source_url"], "execution:page_url")
        self.failure({"page_state": state(), "page_url": SOURCE.replace(ID, OTHER)}, "XHS_IDENTITY_MISMATCH")
        self.failure(html('<div class="note-content"><img src="' + IMAGE + '"></div>', canonical=SOURCE.replace(ID, OTHER)), "XHS_IDENTITY_MISMATCH")

    def test_richtext_current_body_text_images_and_no_html_saved(self):
        work = self.map(fixture("richtext"))
        self.assertEqual((work.title, work.author, work.description, work.topics),
                         ("Native body", "Synthetic author", "First paragraph.\nSecond paragraph", ("travel",)))
        self.assertEqual([item.default_variant.url for item in work.items],
                         ["http://sns-webpic-qc.xhscdn.com/body.jpg?x=1&y=2", "https://www.xiaohongshu.com/body-two.webp"])
        self.assertTrue(work.field_sources["work_id"].startswith("xhs_native:html."))
        for unwanted in ("<div", "Navigation", "avatar.jpg", "recommend.jpg", "script text"):
            self.assertNotIn(unwanted, repr(work))

    def test_richtext_deduplicates_only_dom_media_preserves_order(self):
        body = '<div class="note-content"><img data-original="' + IMAGE + '"><img src="https://example.test/two.webp"><img src="' + IMAGE + '"></div>'
        work = self.map(html(body))
        self.assertEqual([item.default_variant.url for item in work.items], [IMAGE, "https://example.test/two.webp"])

    def test_dom_requires_identity_and_bounded_current_body(self):
        self.failure('<div class="note-content"><p>Unbound</p><img src="' + IMAGE + '"></div>', "XHS_NO_MEDIA")
        self.failure(html('<div id="noteContainer"><p>Outer body</p><img src="' + IMAGE + '"></div>'), "XHS_NO_MEDIA")
        body = '<div class="note-content"><img src="' + IMAGE + '"></div>'
        self.failure(html(body + body), "XHS_AMBIGUOUS_BODY")
        self.failure(" " * (MAX_HTML_CHARS + 1), "XHS_HTML_LIMIT")
        self.failure("<div>" * (MAX_DOM_DEPTH + 1) + "</div>" * (MAX_DOM_DEPTH + 1), "XHS_DOM_LIMIT")
        self.failure("<br>" * MAX_DOM_NODES, "XHS_DOM_LIMIT")

    def test_html_state_and_undefined_sibling_preserve_full_scope(self):
        raw = state()
        current(raw)["title"] = "Literal undefined string"
        self.assertEqual(self.map(html("", script=raw)).items, self.map(raw).items)
        entry = json.dumps(raw["note"]["noteDetailMap"][ID])
        scripted = '<script>window.__INITIAL_STATE__={"broken":undefined,"noteDetailMap":{"' + ID + '":' + entry + '}};</script>'
        work = self.map(scripted)
        self.assertIsInstance(work, Work)
        self.assertEqual(work.title, "Literal undefined string")

    def test_private_deleted_login_and_http_failures(self):
        for status, kind in (("private", FailureKind.NOT_FOUND), ("deleted", FailureKind.NOT_FOUND),
                             ("login_required", FailureKind.UNAVAILABLE), ("unavailable", FailureKind.UNAVAILABLE)):
            raw = state()
            current(raw)["pageState"] = status
            self.failure(raw, "XHS_" + status.upper(), kind)
        for status, kind in ((404, FailureKind.NOT_FOUND), (429, FailureKind.UNAVAILABLE), (408, FailureKind.TIMEOUT)):
            self.failure({"html": "", "http_status": status}, "HTTP_" + str(status), kind)

    def test_malformed_resources_fail_without_dropping_siblings(self):
        for bad in (None, 1, [], "wrong"):
            raw = state()
            current(raw)["imageList"].append(bad)
            self.failure(raw, "XHS_INVALID_IMAGE")
        self.failure(state({"noteId": ID, "type": "normal", "imageList": [{}]}), "XHS_IMAGE_WITHOUT_URL")
        self.failure({"html": "", "page_state": []}, "XHS_INVALID_PAGE_STATE")
        self.failure({"html": "", "assets": []}, "XHS_NOT_RAW_PAGE")

    def test_topics_empty_title_and_unknown_container_do_not_drop_media(self):
        raw = state()
        current(raw).update(title="", desc="Body before #native topic[话题]# after")
        current(raw)["imageList"][0]["urlDefault"] = "http://sns-webpic-qc.xhscdn.com/opaque?imageView2/format/jpg"
        work = self.map(raw)
        self.assertEqual(work.title, "")
        self.assertEqual(work.description, "Body before  after")
        self.assertEqual(work.topics, ("native topic",))
        self.assertEqual(len(work.items), 1)
        self.assertEqual(work.items[0].default_variant.container, "")
