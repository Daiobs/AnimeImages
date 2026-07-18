import json
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from .services.common import XhsParseError
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
from .views import build_result_display_context


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
OTHER_NOTE_ID = "65b2c3d4e5f6789012abcdef"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
IMAGE_ONE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/gallery-one"
IMAGE_TWO = "https://sns-img-qc.xhscdn.com/spectrum/gallery-two"
COVER_URL = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/video-cover"
VIDEO_URL = "https://sns-video-bd.xhscdn.com/stream/video-one.mp4"


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

        result = self.adapter.parse_html(html)

        self.assertEqual([asset.url for asset in result.image_assets], [normalize_xhs_image_url(IMAGE_ONE)])
        self.assertEqual(result.metadata["image_source"], "fallback")

    def test_protocol_relative_gallery_url_keeps_preview_variant(self):
        raw_url = "//sns-webpic-qc.xhscdn.com/notes_pre_post/protocol-image"
        result = self.parse_note(image_note(imageList=[{"urlDefault": raw_url}]))

        self.assertTrue(result.image_assets[0].url.startswith("https://"))
        self.assertEqual(
            result.image_assets[0].preview_url,
            normalize_xhs_preview_image_url(raw_url),
        )
        self.assertIn("/w/640/format/webp", result.image_assets[0].preview_url)

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
