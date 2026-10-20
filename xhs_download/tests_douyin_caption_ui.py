from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase

from .services.assets import MediaAsset, ParseResult
from .services.common import clean_filename
from .views import (
    build_batch_result_row,
    build_result_display_context,
    build_search_results_context,
    search_results_view,
)


TITLE = "简单几分钟调出高光比通透感"
DESCRIPTION = "先调整画面的整体曝光，再处理高光和阴影，让人物与背景之间保持自然的明暗层次。" * 2
CAPTION = f"{TITLE} {DESCRIPTION} #摄影 #调色"
SOURCE = "https://www.douyin.com/video/1234567890123456789"


class DouyinCaptionUITests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        for target in ("requests.sessions.Session.request", "socket.create_connection"):
            blocker = patch(target, side_effect=AssertionError("UI tests must not use the network"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def request(self, *, partial=False, staff=False, authenticated=False, mobile=False):
        request = self.factory.get(
            "/results/", {"q": SOURCE, **({"_partial": "1"} if partial else {})},
            HTTP_USER_AGENT="iPhone" if mobile else "Desktop",
        )
        request.user = SimpleNamespace(
            is_active=True, is_staff=staff, is_authenticated=staff or authenticated,
        )
        return request

    def result(self, title=CAPTION, description="", *, picker=False):
        asset = MediaAsset(
            media_type="video", platform="douyin",
            url="https://v3-dy-o.zjcdn.com/video.mp4",
            size_bytes=1024, asset_id="douyin:1234567890123456789:video:native",
            provider="native", provider_resource_id="1234567890123456789",
            provider_manifest_token="unchanged-manifest", filename="original.mp4",
        )
        metadata = {"title": title, "description": description, "author": "原作者"}
        if picker:
            metadata["upstream_candidates"] = [{
                "candidate_id": "candidate-one", "resource_token": "unchanged-token",
                "provider_id": "tiantianjiexi", "work_id": "work-one",
                "work": {"schema": "service_selected_format_v1", "title": title},
                "work_label": title, "media_type": "video", "format_label": "720p",
                "protocol": "https", "extension": "mp4", "delivery_state": "candidate_only",
                "service_file_eligible": True, "legacy_asset_id": asset.asset_id,
                "legacy_delivery": "existing_manifest",
            }]
        return ParseResult(
            platform="douyin", post_type="video", base_filename="原作者_" + title,
            assets=(asset,), metadata=metadata,
        )

    def assert_caption(self, html, *, batch=False, title=TITLE, description=DESCRIPTION):
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.select_one(".batch-result-title" if batch else ".result-title")
        self.assertIn(title, heading.get_text())
        self.assertNotIn(description, heading.get_text())
        details = soup.select_one("details[data-result-caption]")
        self.assertIsNotNone(details)
        self.assertEqual(details.select_one("summary").get_text(), "作品描述与话题")
        self.assertFalse(details.has_attr("open"))
        self.assertIsNone(details.find_parent("details", class_="ytdlp-technical"))
        self.assertEqual(details.select_one(".result-caption-description").get_text(), description)
        self.assertEqual([node.get_text() for node in details.select("li")], ["#摄影", "#调色"])
        return soup

    def test_normal_and_ajax_homepage_project_caption_for_all_users_and_viewports(self):
        result = self.result()
        with (
            patch("xhs_download.views.parse_media_page", return_value=result),
            patch("xhs_download.views.ultra_hd_is_available", return_value=False),
            patch("xhs_download.views.youtube_merge_mode_is_available", return_value=False),
            patch("xhs_download.views.log_parse_request_event"),
        ):
            for partial in (False, True):
                for staff, authenticated in ((False, False), (False, True), (True, True)):
                    for mobile in (False, True):
                        with self.subTest(partial=partial, staff=staff, authenticated=authenticated, mobile=mobile):
                            response = search_results_view(self.request(
                                partial=partial, staff=staff, authenticated=authenticated, mobile=mobile))
                            self.assertEqual(response.status_code, 200)
                            if partial:
                                payload = json.loads(response.content)
                                self.assertEqual(payload["base_filename"], TITLE)
                                self.assertEqual(payload["status"], "success")
                                html = payload["html"]
                            else:
                                html = response.content.decode()
                                page = BeautifulSoup(html, "html.parser").select_one("[data-base-filename]")
                                self.assertEqual(page["data-base-filename"], TITLE)
                            self.assert_caption(html)

    def test_batch_uses_shared_projection_without_changing_source_or_download_identity(self):
        result = self.result()
        with patch("xhs_download.views.parse_media_page", return_value=result):
            row = build_batch_result_row(
                self.request(), SOURCE, prefer_ultra_hd=False, prefer_youtube_merge=False,
                per_link_timeout_seconds=12,
            )
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["source_text"], SOURCE)
        self.assertEqual(row["base_filename"], TITLE)
        self.assertEqual(row["display_title"], TITLE)
        self.assertEqual(row["display_description"], DESCRIPTION)
        self.assertEqual(row["display_topics"], ["摄影", "调色"])
        html = render_to_string("partials/batch_result_card.html", {"row": row, "batch_download_allowed": True})
        soup = self.assert_caption(html, batch=True)
        self.assertEqual(soup.select_one("[data-batch-download-item]")["data-download-filename"], TITLE + "_1.mp4")
        self.assertEqual(row["video_items"][0]["provider_manifest_token"], "unchanged-manifest")
        self.assertEqual(row["video_items"][0]["url"], result.assets[0].url)

    def test_picker_rendering_keeps_raw_result_and_candidate_records_unchanged(self):
        result = self.result(picker=True)
        original = deepcopy(result)
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        self.assertEqual(context["base_filename"], TITLE)
        self.assertEqual(context["display_title"], TITLE)
        for template, values in (
            ("partials/xhs_results_region.html", context),
            ("partials/batch_result_card.html", {"row": {**context, "status": "success"}}),
        ):
            for staff in (False, True):
                with self.subTest(template=template, staff=staff):
                    html = render_to_string(template, {**values, "show_internal_diagnostics": staff})
                    soup = self.assert_caption(html, batch="batch" in template)
                    self.assertIsNotNone(soup.select_one("[data-ytdlp-picker]"))
                    self.assertEqual(bool(soup.select_one(".ytdlp-technical")), staff)
                    self.assertEqual(soup.select_one("[data-picker-radio]")["data-token"], "unchanged-token")
        self.assertEqual(result, original)
        self.assertEqual(result.metadata["title"], CAPTION)
        self.assertEqual(result.base_filename, "原作者_" + CAPTION)
        self.assertEqual(result.assets[0].filename, "original.mp4")
        self.assertEqual(result.metadata["upstream_candidates"][0]["work"]["title"], CAPTION)

    def test_explicit_description_and_topics_are_escaped_in_both_templates(self):
        title = "A <small> title"
        description = "<script>alert('x')</script>\n第二段 & 内容"
        context = build_result_display_context(
            self.result(title, description + " #<img>"), probe_missing_video_sizes=False,
        )
        self.assertEqual(context["display_title"], title)
        self.assertEqual(context["base_filename"], clean_filename(title))
        for template, values in (
            ("partials/xhs_results_region.html", context),
            ("partials/batch_result_card.html", {"row": {**context, "status": "success"}}),
        ):
            with self.subTest(template=template):
                html = render_to_string(template, values)
                soup = BeautifulSoup(html, "html.parser")
                details = soup.select_one("[data-result-caption]")
                self.assertEqual(details.select_one(".result-caption-description").get_text(), description)
                self.assertEqual(details.select_one("li").get_text(), "#<img>")
                self.assertFalse(details.select("script, img"))
                self.assertIn("&lt;script&gt;", html)
                self.assertIn("&lt;small&gt;", html)

    def test_empty_details_are_hidden_and_invalid_title_keeps_filename_fallback(self):
        for title in ("A short English title", "https://example.com/not-a-title"):
            result = replace(self.result(title), base_filename="douyin_fallback")
            context = build_result_display_context(result, probe_missing_video_sizes=False)
            self.assertEqual(context["base_filename"], "douyin_fallback")
            for template, values in (
                ("partials/xhs_results_region.html", context),
                ("partials/batch_result_card.html", {"row": {**context, "status": "success"}}),
            ):
                self.assertNotIn("data-result-caption", render_to_string(template, values))
        missing = replace(self.result(""), base_filename="")
        self.assertEqual(build_result_display_context(missing, probe_missing_video_sizes=False)["base_filename"], "")
        fallback = replace(missing, base_filename="Existing short title")
        self.assertEqual(build_result_display_context(fallback, probe_missing_video_sizes=False)["display_title"],
                         "Existing short title")
        independent = self.result("独立标题", "单独保存的作品描述")
        self.assertEqual(build_result_display_context(
            independent, probe_missing_video_sizes=False)["base_filename"], independent.base_filename)

    def test_non_douyin_projection_is_unchanged(self):
        with patch("xhs_download.views.douyin_caption_presentation", side_effect=AssertionError("Douyin only")):
            for platform in ("xiaohongshu", "bilibili", "third_party"):
                result = replace(self.result("Other title", "Other description"), platform=platform)
                context = build_result_display_context(result, probe_missing_video_sizes=False)
                self.assertEqual(context["base_filename"], result.base_filename)
                self.assertEqual(context["display_title"], "Other title" if platform == "xiaohongshu" else "")
                self.assertEqual(context["display_description"], "")
                self.assertEqual(context["display_topics"], [])

    def test_empty_search_context_initializes_caption_fields(self):
        request = self.request()
        request.GET = request.GET.copy()
        request.GET["q"] = ""
        context, status = build_search_results_context(request)
        self.assertEqual(status, 200)
        self.assertEqual(context["display_description"], "")
        self.assertEqual(context["display_topics"], [])
