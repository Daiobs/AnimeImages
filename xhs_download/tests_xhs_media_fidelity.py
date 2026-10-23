import json
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from contracts.network import offline_requests

from .services.assets import ParseResult
from .services.batch_parse.results import (
    BATCH_NO_MEDIA_MESSAGE,
    build_batch_result_row,
)
from .services.common import HEADERS, XhsParseError
from .services.platforms.xiaohongshu import (
    XiaohongshuAdapter,
    normalize_video_url,
    normalize_xhs_image_url,
    normalize_xhs_preview_image_url,
)
from .services.platforms.xhs_page_state import (
    XHS_MAX_SCRIPT_CHARS,
    XHS_MAX_VIDEO_SIZE_BYTES,
    extract_xhs_note_id,
    extract_xhs_page_state,
)
from .services.upstream_public_errors import public_upstream_error_message
from .views import build_result_display_context


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
OTHER_NOTE_ID = "65b2c3d4e5f6789012abcdef"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
IMAGE_ONE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/gallery-one"
IMAGE_TWO = "https://sns-img-qc.xhscdn.com/spectrum/gallery-two"
COVER_URL = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/video-cover"
VIDEO_URL = "https://sns-video-bd.xhscdn.com/stream/video-one.mp4"
META_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/meta-image"
DOM_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/dom-image"
BROAD_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/broad-image"
CANONICAL_NOTE_HTML = f'<link rel="canonical" href="{NOTE_URL}">'
GENERIC_SHELL_FIXTURE = (
    Path(__file__).resolve().parent
    / "test_fixtures"
    / "xhs_generic_shell.html"
)


def hydration_html(payload, *, extra_html=""):
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"<html><body>{extra_html}<script>window.__INITIAL_STATE__={serialized};</script></body></html>"


def note_payload(note, *, note_id=NOTE_ID):
    return {"note": {"noteDetailMap": {note_id: {"note": note}}}}


def image_note(**overrides):
    note = {
        "noteId": NOTE_ID,
        "type": "normal",
        "title": "结构化标题",
        "user": {"nickname": "结构化作者"},
        "imageList": [
            {"urlDefault": IMAGE_ONE},
            {"urlDefault": IMAGE_TWO},
        ],
    }
    note.update(overrides)
    return note


def video_note(**overrides):
    note = {
        "noteId": NOTE_ID,
        "type": "video",
        "title": "视频标题",
        "user": {"nickname": "视频作者"},
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        {
                            "masterUrl": VIDEO_URL,
                            "size": 3 * 1024 * 1024,
                            "bitrate": 2_000_000,
                            "duration": 12_000,
                        }
                    ]
                }
            }
        },
    }
    note.update(overrides)
    return note


class XhsStructuredPageStateTests(SimpleTestCase):
    def parse_state(self, payload, *, source_url=NOTE_URL):
        soup = BeautifulSoup(hydration_html(payload), "html.parser")
        return extract_xhs_page_state(soup, source_url=source_url)

    def test_extract_note_id_accepts_known_paths_and_query(self):
        self.assertEqual(extract_xhs_note_id(NOTE_URL), NOTE_ID)
        self.assertEqual(
            extract_xhs_note_id(f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}"),
            NOTE_ID,
        )
        self.assertEqual(
            extract_xhs_note_id(f"https://www.xiaohongshu.com/?note_id={NOTE_ID}"),
            NOTE_ID,
        )
        self.assertEqual(extract_xhs_note_id("https://www.xiaohongshu.com/explore/not-a-note"), "")

    def test_valid_hydration_selects_current_note(self):
        state = self.parse_state(note_payload(image_note()))

        self.assertTrue(state.structured_state_found)
        self.assertEqual(state.note_id, NOTE_ID)
        self.assertEqual(state.title, "结构化标题")
        self.assertEqual(state.author_name, "结构化作者")
        self.assertEqual(state.post_type, "image")
        self.assertEqual(state.image_urls, (IMAGE_ONE, IMAGE_TWO))

    def test_exact_selected_note_retains_a_private_mapper_snapshot(self):
        state = self.parse_state(note_payload(image_note()))

        snapshot = state.private_selected_note_snapshot
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.image_list), 2)
        self.assertFalse(snapshot.note_level_video_present)
        snapshot_repr = repr(snapshot)
        self.assertNotIn(NOTE_ID, snapshot_repr)
        self.assertNotIn(IMAGE_ONE, snapshot_repr)
        self.assertNotIn("image_list", snapshot_repr)
        self.assertNotIn("private_selected_note_snapshot", repr(state))

    def test_private_snapshot_comes_only_from_the_exact_selected_note(self):
        payload = note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}]))
        payload["recommend"] = image_note(
            noteId=OTHER_NOTE_ID,
            imageList=[{"urlDefault": IMAGE_TWO}],
        )

        state = self.parse_state(payload)

        snapshot = state.private_selected_note_snapshot
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.image_list), 1)
        self.assertEqual(snapshot.image_list[0]["urlDefault"], IMAGE_ONE)

    def test_invalid_or_unmatched_note_has_no_private_snapshot(self):
        state = self.parse_state(
            note_payload(image_note(noteId=OTHER_NOTE_ID), note_id=OTHER_NOTE_ID)
        )

        self.assertIsNone(state.private_selected_note_snapshot)

    def test_application_json_hydration_and_balanced_string_content_are_supported(self):
        note = image_note(title='标题包含 } 和转义 " 引号')
        payload = note_payload(note)
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )

        state = extract_xhs_page_state(BeautifulSoup(html, "html.parser"), source_url=NOTE_URL)

        self.assertTrue(state.structured_state_found)
        self.assertEqual(state.title, note["title"])

    def test_valid_current_note_object_survives_invalid_unrelated_root_value(self):
        current_note = json.dumps({"note": image_note()}, ensure_ascii=False)
        html = (
            '<script>window.__INITIAL_STATE__={"global":undefined,"note":'
            f'{{"noteDetailMap":{{"{NOTE_ID}":{current_note}}}}}'
            '};</script>'
        )

        state = extract_xhs_page_state(BeautifulSoup(html, "html.parser"), source_url=NOTE_URL)

        self.assertTrue(state.structured_state_found)
        self.assertEqual(state.note_id, NOTE_ID)
        self.assertEqual(state.title, "结构化标题")

    def test_invalid_json_and_javascript_expression_are_not_executed(self):
        html = """
        <script>window.__INITIAL_STATE__={"note": undefined};</script>
        <script>window.__INITIAL_SSR_STATE__=(function(){return {"note":"bad"};})();</script>
        """
        state = extract_xhs_page_state(BeautifulSoup(html, "html.parser"), source_url=NOTE_URL)

        self.assertFalse(state.structured_state_found)
        self.assertEqual(state.note_id, NOTE_ID)

    def test_oversized_and_too_deep_payloads_are_rejected(self):
        oversized = "<script>window.__INITIAL_STATE__={\"padding\":\"" + (
            "x" * XHS_MAX_SCRIPT_CHARS
        ) + "\"};</script>"
        oversized_state = extract_xhs_page_state(
            BeautifulSoup(oversized, "html.parser"),
            source_url=NOTE_URL,
        )

        nested = {"value": "end"}
        for _index in range(40):
            nested = {"nested": nested}
        deep_state = self.parse_state(nested)

        self.assertFalse(oversized_state.structured_state_found)
        self.assertFalse(deep_state.structured_state_found)

    def test_multiple_notes_match_current_url_not_recommendation_order(self):
        payload = {
            "note": {
                "noteDetailMap": {
                    OTHER_NOTE_ID: {"note": image_note(noteId=OTHER_NOTE_ID, title="推荐内容")},
                    NOTE_ID: {"note": image_note(title="当前内容")},
                }
            }
        }

        state = self.parse_state(payload)

        self.assertEqual(state.note_id, NOTE_ID)
        self.assertEqual(state.title, "当前内容")

    def test_unmatched_current_note_does_not_select_recommendation(self):
        payload = note_payload(
            image_note(noteId=OTHER_NOTE_ID, title="推荐内容"),
            note_id=OTHER_NOTE_ID,
        )

        state = self.parse_state(payload)

        self.assertFalse(state.structured_state_found)
        self.assertTrue(state.structured_payload_found)
        self.assertEqual(state.note_id, NOTE_ID)
        self.assertEqual(state.title, "")

    def test_unknown_url_with_multiple_current_candidates_is_rejected(self):
        payload = {
            "note": {
                "noteDetailMap": {
                    NOTE_ID: {"note": image_note()},
                    OTHER_NOTE_ID: {"note": image_note(noteId=OTHER_NOTE_ID)},
                }
            }
        }

        state = self.parse_state(payload, source_url="")

        self.assertFalse(state.structured_state_found)

    def test_structured_text_is_stripped_and_bounded(self):
        state = self.parse_state(
            note_payload(
                image_note(
                    title=" <b>安全标题</b> " + ("长" * 400),
                    user={"nickname": "<i>安全作者</i>" + ("名" * 200)},
                )
            )
        )

        self.assertNotIn("<", state.title)
        self.assertNotIn("<", state.author_name)
        self.assertLessEqual(len(state.title), 200)
        self.assertLessEqual(len(state.author_name), 80)

    def test_image_info_list_prefers_default_scene_and_preserves_order(self):
        note = image_note(
            imageList=[
                {
                    "infoList": [
                        {"imageScene": "CRD_WM", "url": IMAGE_TWO},
                        {"imageScene": "WB_DFT", "url": IMAGE_ONE},
                    ]
                },
                {"urlDefault": IMAGE_TWO},
            ]
        )

        state = self.parse_state(note_payload(note))

        self.assertEqual(state.image_urls, (IMAGE_ONE, IMAGE_TWO))

    def test_video_stream_size_is_exact_only_for_valid_integer_bytes(self):
        valid_state = self.parse_state(note_payload(video_note()))
        self.assertEqual(valid_state.video_urls[0], VIDEO_URL)
        self.assertEqual(valid_state.video_size_bytes, 3 * 1024 * 1024)

        invalid_values = ("3145728", -1, 1.5, XHS_MAX_VIDEO_SIZE_BYTES + 1)
        for value in invalid_values:
            with self.subTest(value=value):
                note = video_note()
                note["video"]["media"]["stream"]["h264"][0]["size"] = value
                state = self.parse_state(note_payload(note))
                self.assertEqual(state.video_size_bytes, 0)

    def test_bitrate_and_duration_do_not_create_size_estimate(self):
        note = video_note()
        note["video"]["media"]["stream"]["h264"][0].pop("size")

        state = self.parse_state(note_payload(note))

        self.assertEqual(state.video_size_bytes, 0)

    def test_explicit_page_states_are_conservative_fixed_values(self):
        for expected in ("login_required", "private", "deleted", "unavailable"):
            with self.subTest(expected=expected):
                state = self.parse_state({"pageState": expected})
                self.assertEqual(state.page_state, expected)
                self.assertTrue(state.structured_state_found)


class XhsMediaFidelityAdapterTests(SimpleTestCase):
    def setUp(self):
        self.adapter = XiaohongshuAdapter()

    def parse_note(self, note, *, extra_html="", note_id=NOTE_ID):
        payload = note_payload(note, note_id=note_id)
        return self.adapter.parse_html(
            hydration_html(payload, extra_html=extra_html),
            source_url=NOTE_URL,
        )

    def test_structured_title_and_author_override_meta_and_dom(self):
        extra = """
        <meta property="og:title" content="Meta 标题">
        <meta name="author" content="Meta 作者">
        <div id="detail-title">DOM 标题</div><span class="username">DOM 作者</span>
        """

        result = self.parse_note(image_note(), extra_html=extra)

        self.assertEqual(result.metadata["title"], "结构化标题")
        self.assertEqual(result.metadata["author_name"], "结构化作者")
        self.assertEqual(result.metadata["title_source"], "structured")
        self.assertEqual(result.metadata["author_source"], "structured")
        self.assertEqual(result.base_filename, "结构化作者_结构化标题")

    def test_meta_then_dom_fallback_priority(self):
        meta_html = f"""
        {CANONICAL_NOTE_HTML}
        <meta property="og:title" content="Meta 标题 - 小红书">
        <meta name="author" content="Meta 作者">
        <meta property="og:image" content="{IMAGE_ONE}">
        <div id="detail-title">DOM 标题</div><span class="username">DOM 作者</span>
        """
        meta_result = self.adapter.parse_html(meta_html)
        self.assertEqual(meta_result.metadata["title"], "Meta 标题")
        self.assertEqual(meta_result.metadata["author_name"], "Meta 作者")
        self.assertEqual(meta_result.metadata["title_source"], "meta")

        dom_html = f"""
        {CANONICAL_NOTE_HTML}
        <div id="detail-title">DOM 标题</div><span class="username">DOM 作者</span>
        <div id="noteContainer"><img src="{IMAGE_ONE}"></div>
        """
        dom_result = self.adapter.parse_html(dom_html)
        self.assertEqual(dom_result.metadata["title"], "DOM 标题")
        self.assertEqual(dom_result.metadata["author_name"], "DOM 作者")
        self.assertEqual(dom_result.metadata["image_source"], "dom")

    def test_generic_site_title_and_author_are_not_used(self):
        html = f"""
        <meta property="og:title" content="小红书">
        <meta name="author" content="登录后查看更多">
        <meta property="og:image" content="{IMAGE_ONE}">
        """

        result = self.adapter.parse_html(html)

        self.assertNotIn("title", result.metadata)
        self.assertNotIn("author_name", result.metadata)
        self.assertEqual(result.base_filename, "xhs")

    def test_structured_gallery_is_authoritative_ordered_and_deduplicated(self):
        noise = """
        <script>const ignored = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/recommended";</script>
        <img src="https://sns-webpic-qc.xhscdn.com/avatar/user-avatar">
        """
        note = image_note(
            imageList=[
                {"urlDefault": IMAGE_TWO},
                {"urlDefault": IMAGE_ONE},
                {"urlDefault": IMAGE_TWO},
            ]
        )

        result = self.parse_note(note, extra_html=noise)

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [normalize_xhs_image_url(IMAGE_TWO), normalize_xhs_image_url(IMAGE_ONE)],
        )
        self.assertEqual(result.metadata["image_source"], "structured")

    def test_unmatched_structured_note_does_not_fall_back_to_recommendation_urls(self):
        payload = note_payload(
            image_note(noteId=OTHER_NOTE_ID, title="推荐内容"),
            note_id=OTHER_NOTE_ID,
        )

        result = self.adapter.parse_html(hydration_html(payload), source_url=NOTE_URL)

        self.assertEqual(result.assets, ())
        self.assertNotIn("title", result.metadata)

    def test_safe_fallback_still_filters_static_assets(self):
        html = f"""
        <script>
          const avatar = "https://sns-webpic-qc.xhscdn.com/avatar/profile-image";
          const logo = "https://sns-webpic-qc.xhscdn.com/static/site-logo.png";
          const note = "{IMAGE_ONE}";
        </script>
        """

        result = self.adapter.parse_html(
            hydration_html(note_payload(image_note(imageList=[])), extra_html=html)
        )

        self.assertEqual([asset.url for asset in result.image_assets], [normalize_xhs_image_url(IMAGE_ONE)])
        self.assertEqual(result.metadata["image_source"], "fallback")

    def test_protocol_relative_gallery_url_keeps_preview_variant(self):
        raw_url = "//sns-webpic-qc.xhscdn.com/notes_pre_post/protocol-image"
        result = self.parse_note(image_note(imageList=[{"urlDefault": raw_url}]))

        self.assertTrue(result.image_assets[0].url.startswith("https://"))
        self.assertEqual(
            result.image_assets[0].preview_url,
            "https://sns-webpic-qc.xhscdn.com/notes_pre_post/protocol-image",
        )

    def test_video_cover_priority_is_explicit_then_structured_image_then_meta(self):
        explicit = video_note()
        explicit["coverUrl"] = COVER_URL
        explicit_result = self.parse_note(
            explicit,
            extra_html=f'<meta property="og:image" content="{IMAGE_TWO}">',
        )
        self.assertEqual(explicit_result.video_assets[0].cover_url, normalize_xhs_image_url(COVER_URL))
        self.assertEqual(explicit_result.metadata["cover_source"], "structured")

        image_fallback = video_note(imageList=[{"urlDefault": IMAGE_ONE}])
        image_result = self.parse_note(image_fallback)
        self.assertEqual(image_result.video_assets[0].cover_url, normalize_xhs_image_url(IMAGE_ONE))

        meta_result = self.parse_note(
            video_note(),
            extra_html=f'<meta property="og:image" content="{IMAGE_TWO}">',
        )
        self.assertEqual(meta_result.video_assets[0].cover_url, normalize_xhs_image_url(IMAGE_TWO))
        self.assertEqual(meta_result.metadata["cover_source"], "meta")

        dom_result = self.parse_note(
            video_note(),
            extra_html=f'<div id="noteContainer"><img src="{IMAGE_ONE}"></div>',
        )
        self.assertEqual(dom_result.video_assets[0].cover_url, normalize_xhs_image_url(IMAGE_ONE))
        self.assertEqual(dom_result.metadata["cover_source"], "dom")

    def test_video_without_reliable_cover_still_succeeds_and_ignores_avatar(self):
        note = video_note(user={"nickname": "作者", "avatar": IMAGE_ONE})

        result = self.parse_note(note)

        self.assertEqual(len(result.video_assets), 1)
        self.assertEqual(result.video_assets[0].cover_url, "")
        self.assertEqual(result.metadata["cover_source"], "")

    def test_xhs_video_size_is_exact_known_or_unknown_without_probe(self):
        exact_result = self.parse_note(video_note())
        with patch("xhs_download.views.probe_media_size") as mock_probe:
            exact_item = build_result_display_context(exact_result)["video_items"][0]
        self.assertEqual(exact_item["size_display_label"], "3.0 MB")
        self.assertFalse(exact_item["size_is_estimate"])
        mock_probe.assert_not_called()

        unknown_note = video_note(
            video={"originVideoKey": "video-key-without-known-size-12345"},
        )
        unknown_result = self.parse_note(unknown_note)
        with patch("xhs_download.views.probe_media_size") as mock_probe:
            unknown_item = build_result_display_context(unknown_result)["video_items"][0]
        self.assertEqual(unknown_item["size_display_label"], "大小未知")
        self.assertFalse(unknown_item["size_is_estimate"])
        mock_probe.assert_not_called()

    def test_video_url_allowlist_and_m3u8_boundary(self):
        self.assertEqual(normalize_video_url(VIDEO_URL), VIDEO_URL)
        self.assertIsNone(normalize_video_url("https://example.com/video.mp4"))
        self.assertIsNone(normalize_video_url("https://sns-video-bd.xhscdn.com/live/index.m3u8"))
        self.assertIsNone(normalize_video_url("../unsafe-video-key"))

    def test_structured_image_note_ignores_recommended_video(self):
        note = image_note()
        payload = note_payload(note)
        payload["recommend"] = {
            "noteId": OTHER_NOTE_ID,
            "type": "video",
            "video": {"originVideoKey": "recommended-video-key-12345"},
        }

        result = self.adapter.parse_html(hydration_html(payload), source_url=NOTE_URL)

        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.video_assets, ())

    def test_explicit_unavailable_states_raise_safe_messages_without_internal_data(self):
        cases = {
            "login_required": "当前页面需要登录后访问",
            "private": "当前笔记不可公开访问",
            "deleted": "当前笔记已不可用",
            "unavailable": "当前笔记已不可用",
        }
        for state, expected_message in cases.items():
            with self.subTest(state=state):
                html = hydration_html({"pageState": state, "internal": "sensitive fixture data"})
                with self.assertRaisesMessage(XhsParseError, expected_message) as raised:
                    self.adapter.parse_html(html, source_url=NOTE_URL)
                self.assertNotIn("sensitive", str(raised.exception))

    def test_explicit_dom_state_marker_raises_without_fuzzy_body_matching(self):
        html = '<div class="error-page">当前笔记已删除，请返回</div>'

        with self.assertRaisesMessage(XhsParseError, "当前笔记已不可用"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_ordinary_login_word_in_title_does_not_trigger_page_state(self):
        result = self.parse_note(image_note(title="登录教程与公开素材"))

        self.assertEqual(result.metadata["page_state"], "available")
        self.assertEqual(len(result.image_assets), 2)

    def test_unknown_page_state_preserves_safe_empty_fallback(self):
        result = self.adapter.parse_html("<html><body><p>普通公开页面</p></body></html>")

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["page_state"], "unknown")

    def test_single_and_batch_display_context_share_metadata_and_cover(self):
        note = video_note()
        note["video"]["coverUrl"] = COVER_URL
        result = self.parse_note(note)

        single_context = build_result_display_context(result, probe_missing_video_sizes=True)
        batch_context = build_result_display_context(result, probe_missing_video_sizes=False)

        self.assertEqual(single_context["display_title"], "视频标题")
        self.assertEqual(single_context["display_author"], "视频作者")
        self.assertEqual(single_context["note_id"], NOTE_ID)
        self.assertEqual(
            single_context["video_items"][0]["cover_url"],
            batch_context["video_items"][0]["cover_url"],
        )

    def test_metadata_is_small_and_contains_no_media_urls_or_raw_state(self):
        result = self.parse_note(image_note())

        serialized = json.dumps(result.metadata, ensure_ascii=False)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("noteDetailMap", serialized)
        self.assertEqual(result.metadata["note_id"], NOTE_ID)
        self.assertEqual(result.metadata["page_state"], "available")


class XhsFallbackAuthorityAndTerminalTests(SimpleTestCase):
    def setUp(self):
        self.adapter = XiaohongshuAdapter()

    def parse_payload(self, payload, *, extra_html=""):
        return self.adapter.parse_html(
            hydration_html(payload, extra_html=extra_html),
            source_url=NOTE_URL,
        )

    def note_without_media(self, **overrides):
        note = {
            "noteId": NOTE_ID,
            "type": "normal",
            "title": "仅元数据标题",
            "user": {"nickname": "仅元数据作者"},
        }
        note.update(overrides)
        return note

    def other_note_payload(self, note=None):
        return note_payload(
            note or image_note(noteId=OTHER_NOTE_ID, title="推荐标题"),
            note_id=OTHER_NOTE_ID,
        )

    def test_no_url_id_shell_rejects_every_unbound_media_surface(self):
        uuid_image = "https://ci.xiaohongshu.com/7e7b7932-92e7-4c41-9a53-ce76a06f8ecf"
        surfaces = (
            GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8"),
            f'<meta property="og:image" content="{uuid_image}">',
            f'<div id="noteContainer"><img src="{uuid_image}"></div>',
            f'<div class="swiper-slide"><img src="{uuid_image}"></div>',
            f'<script>const images = ["{uuid_image}"];</script>',
            '<script>{"originVideoKey":"unbound-video-key-1234567890"}</script>',
        )
        for source in (None, "https://xhslink.cn/o/opaque", "https://www.xiaohongshu.com/explore"):
            for html in surfaces:
                with self.subTest(source=source, html=html):
                    result = self.adapter.parse_html(html, source_url=source)
                    self.assertEqual(result.assets, ())
                    self.assertEqual(result.metadata["page_state"], "unknown")
                    self.assertFalse(result.metadata["source_identity_verified"])

    def test_no_url_id_shell_is_empty_for_single_display_and_failed_for_batch(self):
        source = "https://xhslink.cn/o/opaque"
        html = GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8")
        result = self.adapter.parse_html(html, source_url=source)
        context = build_result_display_context(result, platform_key="home")
        row = build_batch_result_row(
            Mock(), source, prefer_ultra_hd=False, prefer_youtube_merge=False,
            per_link_timeout_seconds=2,
            parser=lambda url, **kwargs: self.adapter.parse_html(html, source_url=url),
            display_context_builder=build_result_display_context,
            request_country_code_builder=lambda request: "",
            query_uses_third_party_provider_func=lambda url: False,
            third_party_provider_is_available_func=lambda request: True,
        )
        self.assertEqual(context["image_items"], [])
        self.assertEqual(context["video_items"], [])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_message"], BATCH_NO_MEDIA_MESSAGE)

    def test_no_url_id_unique_current_structure_preserves_images_and_video(self):
        for note in (image_note(), video_note()):
            with self.subTest(kind=note["type"]):
                result = self.adapter.parse_html(
                    hydration_html(note_payload(note)), source_url="https://xhslink.cn/o/opaque")
                self.assertTrue(result.assets)
                self.assertEqual(result.metadata["note_id"], NOTE_ID)
                self.assertTrue(result.metadata["current_note_found"])
                self.assertTrue(result.metadata["source_identity_verified"])

    def test_no_url_id_unique_current_structure_preserves_fallbacks(self):
        for media, expected_source, post_type in (
            (f'<meta property="og:image" content="{META_IMAGE}">', "meta", "image"),
            (f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>', "dom", "image"),
            (f'<script>const image = "{BROAD_IMAGE}";</script>', "fallback", "image"),
            ('<script>{"originVideoKey":"current-video-key-1234567890"}</script>', "fallback", "video"),
        ):
            with self.subTest(source=expected_source, post_type=post_type):
                note = self.note_without_media(type=post_type)
                result = self.adapter.parse_html(
                    hydration_html(note_payload(note), extra_html=media)
                )
                self.assertEqual(len(result.assets), 1)
                self.assertEqual(result.post_type, post_type)
                self.assertEqual(result.metadata[f"{post_type}_source"], expected_source)
                self.assertTrue(result.metadata["source_identity_verified"])

    def test_no_url_id_current_note_without_media_cannot_scan_recommendation(self):
        for recommendation in (
            image_note(noteId=OTHER_NOTE_ID),
            video_note(noteId=OTHER_NOTE_ID),
        ):
            with self.subTest(kind=recommendation["type"]):
                payload = note_payload(self.note_without_media(type=recommendation["type"]))
                payload["recommendations"] = [recommendation]
                result = self.adapter.parse_html(hydration_html(payload))
                self.assertEqual(result.assets, ())
                self.assertTrue(result.metadata["current_note_found"])
                self.assertTrue(result.metadata["other_note_candidate_found"])

    def test_no_url_id_consistent_page_identity_allows_scoped_fallback(self):
        for identity in (
            CANONICAL_NOTE_HTML,
            f'<meta property="og:url" content="{NOTE_URL}">',
            f'<div id="noteContainer" data-note-id="{NOTE_ID}"></div>',
            CANONICAL_NOTE_HTML + f'<meta property="og:url" content="{NOTE_URL}">',
        ):
            for media, expected_source in (
                (f'<meta property="og:image" content="{META_IMAGE}">', "meta"),
                (f'<div class="note-content"><img src="{DOM_IMAGE}"></div>', "dom"),
            ):
                with self.subTest(identity=identity, source=expected_source):
                    html = identity + media + GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8")
                    result = self.adapter.parse_html(html, source_url="https://xhslink.cn/o/opaque")
                    self.assertEqual(len(result.image_assets), 1)
                    self.assertEqual(result.metadata["note_id"], NOTE_ID)
                    self.assertTrue(result.metadata["source_identity_verified"])
                    self.assertEqual(result.metadata["image_source"], expected_source)
                    self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")

    def test_no_url_id_page_identity_does_not_admit_page_level_or_script_media(self):
        for media in (
            GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8"),
            f'<script>const image = "{BROAD_IMAGE}";</script>',
            '<script>{"originVideoKey":"unbound-video-key-1234567890"}</script>',
        ):
            with self.subTest(media=media):
                result = self.adapter.parse_html(CANONICAL_NOTE_HTML + media)
                self.assertEqual(result.assets, ())
                self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")

    def test_page_identity_conflicts_reject_structured_and_fallback_media(self):
        other_url = f"https://www.xiaohongshu.com/explore/{OTHER_NOTE_ID}"
        conflicts = (
            CANONICAL_NOTE_HTML + f'<meta property="og:url" content="{other_url}">',
            CANONICAL_NOTE_HTML + f'<div id="noteContainer" data-note-id="{OTHER_NOTE_ID}"></div>',
            f'<link rel="canonical" href="{other_url}">',
        )
        for source in (None, "https://xhslink.cn/o/opaque", NOTE_URL):
            for identity in conflicts:
                with self.subTest(source=source, identity=identity):
                    html = hydration_html(note_payload(image_note()), extra_html=identity)
                    result = self.adapter.parse_html(html, source_url=source)
                    self.assertEqual(result.assets, ())
                    self.assertFalse(result.metadata["source_identity_verified"])
                    self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")
        html = conflicts[0] + f'<meta property="og:image" content="{META_IMAGE}">'
        self.assertEqual(self.adapter.parse_html(html).assets, ())

    def test_foreign_page_identity_cannot_authorize_xhs_meta_images(self):
        for value in (f"https://example.com/explore/{NOTE_ID}", f"https://www.xiaohongshu.com.evil/explore/{NOTE_ID}"):
            with self.subTest(value=value):
                html = f'<link rel="canonical" href="{value}"><meta property="og:image" content="{META_IMAGE}">'
                self.assertEqual(self.adapter.parse_html(html).assets, ())

    def test_no_page_identity_recommendations_or_multiple_current_notes_are_not_selected(self):
        for payload in (
            {"recommend": [image_note()]},
            {"note": {"noteDetailMap": {
                NOTE_ID: {"note": image_note()},
                OTHER_NOTE_ID: {"note": image_note(noteId=OTHER_NOTE_ID)},
            }}},
        ):
            with self.subTest(payload=payload):
                html = hydration_html(payload, extra_html=f'<meta property="og:image" content="{META_IMAGE}">')
                result = self.adapter.parse_html(html)
                self.assertEqual(result.assets, ())
                self.assertEqual(result.metadata["fallback_policy"], "source_identity_unverified")

    def test_page_identity_does_not_turn_recommendations_into_current_work(self):
        for payload in (
            {"recommend": [image_note()]},
            {"feeds": [image_note()]},
            {"currentNote": image_note(noteId=OTHER_NOTE_ID), "recommend": [image_note()]},
        ):
            with self.subTest(payload=payload):
                html = hydration_html(payload, extra_html=CANONICAL_NOTE_HTML
                    + f'<meta property="og:image" content="{META_IMAGE}">')
                result = self.adapter.parse_html(html, source_url="https://xhslink.cn/o/opaque")
                self.assertEqual(result.assets, ())
                self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

    def test_verified_page_identity_selects_only_matching_note_from_multiple_records(self):
        for note in (image_note(), video_note()):
            with self.subTest(kind=note["type"]):
                payload = {"note": {"noteDetailMap": {
                    OTHER_NOTE_ID: {"note": image_note(noteId=OTHER_NOTE_ID,
                        title="Other work", imageList=[{"urlDefault": BROAD_IMAGE}])},
                    NOTE_ID: {"note": note},
                }}}
                html = hydration_html(payload, extra_html=CANONICAL_NOTE_HTML
                    + f'<meta property="og:image" content="{META_IMAGE}">')
                state = extract_xhs_page_state(BeautifulSoup(html, "html.parser"),
                                              source_url="https://xhslink.cn/o/opaque")
                self.assertEqual(state.note_id, NOTE_ID)
                self.assertEqual(state.page_note_id, NOTE_ID)
                self.assertEqual(state.source_note_id, "")
                self.assertTrue(state.current_note_found)
                self.assertFalse(state.page_identity_conflict)
                self.assertIsNone(state.private_selected_note_snapshot)
                result = self.adapter.parse_html(html, source_url="https://xhslink.cn/o/opaque")
                expected = ([normalize_video_url(VIDEO_URL)] if note["type"] == "video" else
                            [normalize_xhs_image_url(IMAGE_ONE), normalize_xhs_image_url(IMAGE_TWO)])
                self.assertEqual([asset.url for asset in result.assets], expected)
                self.assertEqual(result.metadata["fallback_policy"], "current_note_authoritative")
                self.assertTrue(result.metadata["source_identity_verified"])

    def test_page_target_mismatch_is_distinct_from_missing_or_conflicting_identity(self):
        html = hydration_html(self.other_note_payload(), extra_html=CANONICAL_NOTE_HTML
                              + f'<meta property="og:image" content="{META_IMAGE}">')
        state = extract_xhs_page_state(BeautifulSoup(html, "html.parser"))
        self.assertFalse(state.current_note_found)
        self.assertFalse(state.page_identity_conflict)
        self.assertEqual(state.page_note_id, NOTE_ID)
        result = self.adapter.parse_html(html)
        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

        other_url = f"https://www.xiaohongshu.com/explore/{OTHER_NOTE_ID}"
        conflict = extract_xhs_page_state(BeautifulSoup(
            f'<link rel="canonical" href="{other_url}">' + hydration_html(note_payload(image_note())),
            "html.parser"), source_url=NOTE_URL)
        self.assertTrue(conflict.page_identity_conflict)
        self.assertFalse(conflict.current_note_found)

    def test_short_link_redirect_to_matching_work_uses_actual_http_boundary(self):
        short = "https://xhslink.cn/o/opaque"
        calls = []

        def send(request, **kwargs):
            calls.append(request.url)
            response = requests.Response()
            response.url = request.url
            response.status_code = 302 if request.url == short else 200
            if request.url == short:
                response.headers["Location"] = NOTE_URL
                response._content = b""
            else:
                self.assertEqual(request.url, NOTE_URL)
                response._content = hydration_html(note_payload(image_note())).encode()
            response.raw = BytesIO(response._content)
            return response

        with offline_requests(send):
            result = self.adapter.parse_url(short)
        self.assertEqual(calls, [short, NOTE_URL])
        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.metadata["note_id"], NOTE_ID)
        self.assertTrue(result.metadata["source_identity_verified"])

    def test_short_link_shell_reports_unknown_content_not_deleted_or_login(self):
        def send(request, **kwargs):
            response = requests.Response()
            response.url = request.url
            response.status_code = 200
            response._content = GENERIC_SHELL_FIXTURE.read_bytes()
            response.raw = BytesIO(response._content)
            return response

        with offline_requests(send), self.assertRaisesMessage(
            XhsParseError, "未读取到该链接的有效作品内容"
        ) as raised:
            self.adapter.parse_url("https://xhslink.cn/o/opaque")
        self.assertEqual(
            public_upstream_error_message(
                raised.exception, is_upstream=False, fallback="解析失败，请检查链接后重试。"
            ),
            "未读取到该链接的有效作品内容。",
        )

    def test_short_link_shell_batch_preserves_fixed_unknown_content_message(self):
        def send(request, **kwargs):
            response = requests.Response()
            response.url = request.url
            response.status_code = 200
            response._content = GENERIC_SHELL_FIXTURE.read_bytes()
            response.raw = BytesIO(response._content)
            return response

        with offline_requests(send):
            row = build_batch_result_row(
                Mock(), "https://xhslink.cn/o/opaque",
                prefer_ultra_hd=False, prefer_youtube_merge=False,
                per_link_timeout_seconds=2,
                parser=lambda url, **kwargs: self.adapter.parse_url(url),
                display_context_builder=build_result_display_context,
                request_country_code_builder=lambda request: "",
                query_uses_third_party_provider_func=lambda url: False,
                third_party_provider_is_available_func=lambda request: True,
            )
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["public_error_message"], "未读取到该链接的有效作品内容。")

    def test_explicit_note_id_rejects_generic_shell_media(self):
        source_url = (
            "https://www.xiaohongshu.com/explore/"
            "69453774000000001f00dbb5"
        )
        result = self.adapter.parse_html(
            GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8"),
            source_url=source_url,
        )

        self.assertEqual(result.assets, ())
        self.assertFalse(result.metadata["current_note_found"])
        self.assertFalse(result.metadata["note_candidate_found"])
        self.assertEqual(
            result.metadata["fallback_policy"],
            "source_identity_unverified",
        )

    def test_generic_shell_is_empty_for_single_display_and_failed_for_batch(self):
        source_url = (
            "https://www.xiaohongshu.com/explore/"
            "69453774000000001f00dbb5"
        )
        result = self.adapter.parse_html(
            GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8"),
            source_url=source_url,
        )

        single_context = build_result_display_context(result, platform_key="home")
        batch_row = build_batch_result_row(
            Mock(),
            source_url,
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=2,
            parser=lambda *_args, **_kwargs: result,
            display_context_builder=build_result_display_context,
            request_country_code_builder=lambda _request: "",
            query_uses_third_party_provider_func=lambda _source: False,
            third_party_provider_is_available_func=lambda _request: True,
        )

        self.assertEqual(single_context["image_items"], [])
        self.assertEqual(single_context["video_items"], [])
        self.assertEqual(batch_row["status"], "failed")
        self.assertEqual(batch_row["error_message"], BATCH_NO_MEDIA_MESSAGE)
        self.assertEqual(batch_row["public_error_message"], BATCH_NO_MEDIA_MESSAGE)

    def test_verified_canonical_identity_allows_meta_fallback(self):
        html = f'''
        <link rel="canonical" href="{NOTE_URL}">
        <meta property="og:image" content="{META_IMAGE}">
        '''

        result = self.adapter.parse_html(html, source_url=NOTE_URL)

        self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")
        self.assertEqual(result.metadata["image_source"], "meta")
        self.assertEqual(result.image_assets[0].url, normalize_xhs_image_url(META_IMAGE))

    def test_verified_page_identity_allows_strict_current_note_dom(self):
        html = f'''
        <meta property="og:url" content="{NOTE_URL}">
        <div id="noteContainer"><img src="{DOM_IMAGE}"></div>
        '''

        result = self.adapter.parse_html(html, source_url=NOTE_URL)

        self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")
        self.assertEqual(result.metadata["image_source"], "dom")
        self.assertEqual(result.image_assets[0].url, normalize_xhs_image_url(DOM_IMAGE))

    def test_current_note_container_identity_allows_strict_dom_fallback(self):
        html = (
            f'<div id="noteContainer" data-note-id="{NOTE_ID}">'
            f'<img src="{DOM_IMAGE}"></div>'
        )

        result = self.adapter.parse_html(html, source_url=NOTE_URL)

        self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")
        self.assertEqual(result.metadata["image_source"], "dom")

    def test_verified_page_identity_still_rejects_broad_shell_scan(self):
        html = CANONICAL_NOTE_HTML + GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8")

        result = self.adapter.parse_html(html, source_url=NOTE_URL)

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "verified_page_fallback")

    def test_mismatched_note_rejects_meta_and_strict_dom(self):
        for fallback_html in (
            f'<meta property="og:image" content="{META_IMAGE}">',
            f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>',
        ):
            with self.subTest(fallback_html=fallback_html):
                result = self.parse_payload(
                    self.other_note_payload(),
                    extra_html=fallback_html,
                )

                self.assertEqual(result.assets, ())
                self.assertEqual(
                    result.metadata["fallback_policy"],
                    "source_identity_mismatch",
                )

    @patch("xhs_download.services.platforms.xiaohongshu.request_get_with_retry")
    def test_parse_url_preserves_requested_note_identity_after_generic_redirect(
        self,
        mock_get,
    ):
        response = Mock()
        response.url = "https://www.xiaohongshu.com/explore"
        response.text = GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8")
        mock_get.return_value = response

        with self.assertRaisesMessage(XhsParseError, "未读取到该链接的有效作品内容"):
            self.adapter.parse_url(NOTE_URL)
        response.close.assert_called_once()

    @patch("xhs_download.services.platforms.xiaohongshu.request_get_with_retry")
    def test_parse_url_uses_document_accept_without_changing_media_headers(self, mock_get):
        original_headers = HEADERS.copy()
        response = Mock()
        response.url = NOTE_URL
        response.text = hydration_html(note_payload(image_note()))
        mock_get.return_value = response

        self.adapter.parse_url(NOTE_URL)

        mock_get.assert_called_once()
        page_headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(
            page_headers["Accept"],
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        )
        self.assertIsNot(page_headers, HEADERS)
        self.assertEqual(HEADERS, original_headers)
        self.assertEqual(
            HEADERS["Accept"],
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        )
        self.assertEqual(
            {key: value for key, value in page_headers.items() if key != "Accept"},
            {key: value for key, value in HEADERS.items() if key != "Accept"},
        )
        response.close.assert_called_once()

    @patch("xhs_download.services.platforms.xiaohongshu.request_get_with_retry")
    def test_login_redirect_is_terminal_even_without_dom_login_markers(self, mock_get):
        for source_url in ("https://xhslink.cn/o/synthetic", NOTE_URL):
            for login_path in ("/login", "/login/?redirect=synthetic"):
                with self.subTest(source_url=source_url, login_path=login_path):
                    response = Mock()
                    response.url = "https://www.xiaohongshu.com" + login_path
                    response.text = GENERIC_SHELL_FIXTURE.read_text(encoding="utf-8")
                    mock_get.return_value = response
                    empty_result = ParseResult(
                        platform="xiaohongshu", post_type="image", base_filename="synthetic", assets=(),
                    )
                    with patch.object(self.adapter, "parse_html", return_value=empty_result) as parse_html:
                        with self.assertRaisesMessage(XhsParseError, "当前页面需要登录后访问"):
                            self.adapter.parse_url(source_url, prefer_ultra_hd=True)
                        parse_html.assert_not_called()
                    response.close.assert_called_once()

    def test_unrelated_json_ld_without_note_id_rejects_unbound_images(self):
        html = f'''
        <script type="application/ld+json">
          {{"@context":"https://schema.org","@type":"WebSite"}}
        </script>
        <script>const fallback = "{BROAD_IMAGE}";</script>
        '''

        result = self.adapter.parse_html(html)

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_unverified")

    def test_unrelated_application_json_without_note_id_rejects_unbound_images(self):
        config = {
            "siteConfig": {
                "title": "站点配置",
                "author": "站点",
                "type": "website",
                "images": ["https://example.com/site-logo.png"],
            }
        }
        html = (
            '<script type="application/json">'
            + json.dumps(config, ensure_ascii=False)
            + "</script>"
            + f'<script>const fallback = "{BROAD_IMAGE}";</script>'
        )

        result = self.adapter.parse_html(html)

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["image_source"], "")
        self.assertFalse(result.metadata["note_candidate_found"])

    def test_unrelated_hydration_without_note_id_rejects_unbound_images(self):
        payload = {
            "siteConfig": {
                "title": "配置标题",
                "author": "配置作者",
                "type": "website",
                "images": ["https://example.com/static.png"],
            }
        }

        result = self.adapter.parse_html(
            hydration_html(
                payload,
                extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_unverified")

    def test_unrelated_json_without_note_id_rejects_unbound_video(self):
        html = '''
        <script type="application/json">{"siteConfig":{"theme":"light"}}</script>
        <script>{"originVideoKey":"legacy-video-key-1234567890"}</script>
        '''

        result = self.adapter.parse_html(html)

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["video_source"], "")

    def test_current_note_without_media_uses_meta_image(self):
        result = self.parse_payload(
            note_payload(self.note_without_media()),
            extra_html=f'<meta property="og:image" content="{META_IMAGE}">',
        )

        self.assertEqual(result.metadata["image_source"], "meta")
        self.assertEqual(result.image_assets[0].url, normalize_xhs_image_url(META_IMAGE))

    def test_current_note_without_media_uses_current_dom_image(self):
        result = self.parse_payload(
            note_payload(self.note_without_media()),
            extra_html=f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>',
        )

        self.assertEqual(result.metadata["image_source"], "dom")
        self.assertEqual(result.image_assets[0].url, normalize_xhs_image_url(DOM_IMAGE))

    def test_current_note_without_media_uses_safe_broad_image(self):
        result = self.parse_payload(
            note_payload(self.note_without_media()),
            extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
        )

        self.assertEqual(result.metadata["image_source"], "fallback")
        self.assertEqual(result.image_assets[0].url, normalize_xhs_image_url(BROAD_IMAGE))

    def test_empty_structured_image_list_allows_fallback(self):
        note = self.note_without_media(imageList=[])
        state = extract_xhs_page_state(
            BeautifulSoup(hydration_html(note_payload(note)), "html.parser"),
            source_url=NOTE_URL,
        )
        result = self.parse_payload(
            note_payload(note),
            extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
        )

        self.assertTrue(state.structured_image_list_present)
        self.assertEqual(state.image_urls, ())
        self.assertEqual(result.metadata["image_source"], "fallback")

    def test_invalid_structured_image_list_allows_fallback(self):
        note = self.note_without_media(
            imageList=[
                {"urlDefault": "https://sns-webpic-qc.xhscdn.com/"},
                {"urlDefault": "https://sns-webpic-qc.xhscdn.com/avatar/user"},
                {"urlDefault": "https://example.com/not-xhs.jpg"},
            ]
        )

        result = self.parse_payload(
            note_payload(note),
            extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
        )

        self.assertEqual(result.metadata["image_source"], "fallback")
        self.assertFalse(result.metadata["structured_image_authoritative"])

    def test_valid_structured_gallery_does_not_append_meta_image(self):
        result = self.parse_payload(
            note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}])),
            extra_html=f'<meta property="og:image" content="{META_IMAGE}">',
        )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [IMAGE_ONE],
        )

    def test_valid_structured_gallery_does_not_append_dom_image(self):
        result = self.parse_payload(
            note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}])),
            extra_html=f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>',
        )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [IMAGE_ONE],
        )

    def test_valid_structured_gallery_does_not_append_broad_noise(self):
        result = self.parse_payload(
            note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}])),
            extra_html=f'<script>const noise = "{BROAD_IMAGE}";</script>',
        )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [IMAGE_ONE],
        )
        self.assertTrue(result.metadata["structured_image_authoritative"])

    def test_structured_gallery_deduplicates_after_normalization(self):
        result = self.parse_payload(
            note_payload(
                image_note(
                    imageList=[
                        {"urlDefault": IMAGE_ONE},
                        {"urlDefault": f"{IMAGE_ONE}?token=fixture"},
                        {"urlDefault": IMAGE_TWO},
                    ]
                )
            )
        )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [normalize_xhs_image_url(IMAGE_ONE), normalize_xhs_image_url(IMAGE_TWO)],
        )

    def test_mismatched_note_rejects_explicit_page_meta_despite_canonical(self):
        result = self.parse_payload(
            self.other_note_payload(),
            extra_html=(
                CANONICAL_NOTE_HTML
                + f'<meta property="og:image" content="{META_IMAGE}">'
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["image_source"], "")
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")
        self.assertFalse(result.metadata["source_identity_verified"])
        self.assertNotIn("推荐标题", result.base_filename)

    def test_mismatched_note_rejects_strict_dom_despite_canonical(self):
        result = self.parse_payload(
            self.other_note_payload(),
            extra_html=(
                CANONICAL_NOTE_HTML
                + f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>'
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["image_source"], "")
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

    def test_mismatched_note_rejects_page_level_swiper_recommendation(self):
        result = self.parse_payload(
            self.other_note_payload(),
            extra_html=(
                CANONICAL_NOTE_HTML
                + f'<div class="swiper-slide"><img src="{DOM_IMAGE}"></div>'
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

    def test_mismatched_note_rejects_page_level_carousel_recommendation(self):
        result = self.parse_payload(
            self.other_note_payload(),
            extra_html=(
                CANONICAL_NOTE_HTML
                + f'<div class="carousel-container"><img src="{DOM_IMAGE}"></div>'
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

    def test_mismatched_note_rejects_swiper_scoped_to_note_container(self):
        result = self.parse_payload(
            self.other_note_payload(),
            extra_html=(
                CANONICAL_NOTE_HTML
                + '<div id="noteContainer"><div class="swiper-slide">'
                f'<img src="{DOM_IMAGE}"></div></div>'
            ),
        )

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["image_source"], "")
        self.assertEqual(result.metadata["fallback_policy"], "source_identity_mismatch")

    def test_unrelated_json_rejects_unbound_page_level_swiper(self):
        html = f'''
        <script type="application/json">{{"siteConfig":{{"theme":"light"}}}}</script>
        <div class="swiper-slide"><img src="{DOM_IMAGE}"></div>
        '''

        result = self.adapter.parse_html(html)

        self.assertEqual(result.metadata["fallback_policy"], "source_identity_unverified")
        self.assertEqual(result.metadata["image_source"], "")
        self.assertEqual(result.assets, ())

    def test_mismatched_note_suppresses_broad_recommendation_image(self):
        result = self.parse_payload(self.other_note_payload())

        self.assertEqual(result.assets, ())
        self.assertEqual(
            result.metadata["fallback_policy"],
            "source_identity_mismatch",
        )
        self.assertNotIn("title", result.metadata)

    def test_mismatched_note_suppresses_recommendation_video(self):
        recommendation = video_note(noteId=OTHER_NOTE_ID, title="推荐视频")

        result = self.parse_payload(self.other_note_payload(recommendation))

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["video_source"], "")

    def test_login_terminal_precedes_meta_image(self):
        html = hydration_html(
            {"pageState": "login_required"},
            extra_html=f'<meta property="og:image" content="{META_IMAGE}">',
        )

        with self.assertRaisesMessage(XhsParseError, "当前页面需要登录后访问"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_login_terminal_precedes_current_dom_image(self):
        html = hydration_html(
            {"pageState": "login_required"},
            extra_html=f'<div id="noteContainer"><img src="{DOM_IMAGE}"></div>',
        )

        with self.assertRaisesMessage(XhsParseError, "当前页面需要登录后访问"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_login_terminal_precedes_broad_image(self):
        html = hydration_html(
            {"pageState": "login_required"},
            extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
        )

        with self.assertRaisesMessage(XhsParseError, "当前页面需要登录后访问"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_login_terminal_precedes_generic_carousel_and_swiper_images(self):
        html = hydration_html(
            {"pageState": "login_required"},
            extra_html=(
                f'<div class="carousel-container"><img src="{META_IMAGE}"></div>'
                f'<div class="swiper-slide"><img src="{DOM_IMAGE}"></div>'
            ),
        )

        with self.assertRaisesMessage(XhsParseError, "当前页面需要登录后访问"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_private_terminal_precedes_meta_image(self):
        html = hydration_html(
            {"pageState": "private"},
            extra_html=f'<meta property="og:image" content="{META_IMAGE}">',
        )

        with self.assertRaisesMessage(XhsParseError, "当前笔记不可公开访问"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_deleted_terminal_precedes_dom_image(self):
        html = f'''
        <div class="error-page">当前笔记已删除，请返回</div>
        <div id="noteContainer"><img src="{DOM_IMAGE}"></div>
        '''

        with self.assertRaisesMessage(XhsParseError, "当前笔记已不可用"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_unavailable_terminal_precedes_broad_image(self):
        html = hydration_html(
            {"pageState": "unavailable"},
            extra_html=f'<script>const fallback = "{BROAD_IMAGE}";</script>',
        )

        with self.assertRaisesMessage(XhsParseError, "当前笔记已不可用"):
            self.adapter.parse_html(html, source_url=NOTE_URL)

    def test_generic_terminal_does_not_kill_verified_current_note_media(self):
        payload = note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}]))
        payload["pageState"] = "login_required"

        result = self.parse_payload(payload)

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(result.metadata["page_state"], "available")
        self.assertTrue(result.metadata["terminal_page_state"])

    def test_structured_gallery_remains_authoritative_over_generic_carousel(self):
        result = self.parse_payload(
            note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}])),
            extra_html=(
                f'<div class="carousel-container"><img src="{DOM_IMAGE}"></div>'
            ),
        )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [IMAGE_ONE],
        )
        self.assertEqual(
            result.metadata["fallback_policy"],
            "current_note_authoritative",
        )

    def test_dom_terminal_does_not_kill_verified_current_note_media(self):
        result = self.parse_payload(
            note_payload(image_note(imageList=[{"urlDefault": IMAGE_ONE}])),
            extra_html='<div class="error-page">当前笔记已删除，请返回</div>',
        )

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(result.metadata["page_state"], "available")
        self.assertTrue(result.metadata["terminal_page_state"])

    def test_current_note_private_state_wins_over_valid_media(self):
        note = image_note(
            pageState="private",
            imageList=[{"urlDefault": IMAGE_ONE}],
        )

        with self.assertRaisesMessage(XhsParseError, "当前笔记不可公开访问"):
            self.parse_payload(note_payload(note))

    def test_other_note_media_cannot_bypass_terminal_state(self):
        payload = self.other_note_payload()
        payload["pageState"] = "private"

        with self.assertRaisesMessage(XhsParseError, "当前笔记不可公开访问"):
            self.parse_payload(payload)

    def test_ordinary_deleted_word_in_title_is_not_terminal(self):
        result = self.parse_payload(note_payload(image_note(title="删除照片的方法")))

        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.metadata["page_state"], "available")

    def test_ordinary_private_word_in_title_is_not_terminal(self):
        result = self.parse_payload(note_payload(image_note(title="私密空间摄影")))

        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.metadata["page_state"], "available")

    def test_matched_video_without_structured_url_uses_safe_video_fallback(self):
        note = self.note_without_media(type="video")
        state = extract_xhs_page_state(
            BeautifulSoup(hydration_html(note_payload(note)), "html.parser"),
            source_url=NOTE_URL,
        )

        result = self.parse_payload(
            note_payload(note),
            extra_html='<script>{"originVideoKey":"current-video-key-1234567890"}</script>',
        )

        self.assertEqual(len(result.video_assets), 1)
        self.assertEqual(result.metadata["video_source"], "fallback")
        self.assertFalse(state.structured_video_present)

    def test_matched_video_does_not_scan_other_note_recommendation(self):
        payload = note_payload(self.note_without_media(type="video"))
        payload["recommend"] = video_note(
            noteId=OTHER_NOTE_ID,
            title="推荐视频",
        )

        result = self.parse_payload(payload)

        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["fallback_policy"], "suppress_broad_scan")

    def test_metadata_exposes_only_small_policy_facts(self):
        result = self.parse_payload(note_payload(image_note()))

        self.assertTrue(result.metadata["current_note_found"])
        self.assertTrue(result.metadata["note_candidate_found"])
        self.assertEqual(
            result.metadata["fallback_policy"],
            "current_note_authoritative",
        )
        self.assertTrue(result.metadata["structured_image_authoritative"])
        serialized = json.dumps(result.metadata, ensure_ascii=False)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("noteDetailMap", serialized)

    def test_unrelated_payload_remains_diagnostic_not_authoritative(self):
        html = f'''
        <script type="application/json">{{"siteConfig":{{"theme":"light"}}}}</script>
        <script>const fallback = "{BROAD_IMAGE}";</script>
        '''

        result = self.adapter.parse_html(html, source_url=NOTE_URL)

        self.assertTrue(result.metadata["structured_payload_found"])
        self.assertFalse(result.metadata["current_note_found"])
        self.assertFalse(result.metadata["note_candidate_found"])
        self.assertEqual(
            result.metadata["fallback_policy"],
            "source_identity_unverified",
        )
        self.assertFalse(result.metadata["source_identity_verified"])
