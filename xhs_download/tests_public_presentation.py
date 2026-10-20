from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase

from .services.common import XhsParseError
from .services.batch_parse.results import build_batch_failed_row
from .views import (
    apply_search_result_to_context,
    build_search_results_context,
    get_download_settings_context,
)


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._ignored_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            value = " ".join(str(data or "").split())
            if value:
                self.parts.append(value)


def visible_text(html):
    parser = _VisibleTextParser()
    parser.feed(html)
    return " ".join(parser.parts)


def result_display_context(*, video=True):
    video_items = []
    image_items = []
    if video:
        video_items = [
            {
                "video_url": "https://media.example/video.mp4",
                "size_display_label": "12 MB",
                "size_label": "12 MB",
                "provider": "tiantianjiexi",
                "provider_manifest_token": "opaque-test-token",
                "download_strategy": "browser_direct_provider_video",
                "strategy_label": "视频直链优先",
            }
        ]
    else:
        image_items = [
            {
                "url": "https://media.example/image.jpg",
                "provider": "tiantianjiexi",
                "provider_manifest_token": "opaque-test-token",
                "download_strategy": "browser_direct",
                "strategy_label": "图片直链优先",
                "encoded_url": "encoded-image",
            }
        ]
    return {
        "result_theme": "home",
        "platform_name": "抖音" if video else "小红书",
        "post_type": "video" if video else "image",
        "base_filename": "presentation-test",
        "display_title": "展示边界测试",
        "display_author": "测试作者",
        "note_id": "note-test",
        "metadata_source": "fixture",
        "image_items": image_items,
        "video_items": video_items,
    }


class PublicPresentationBoundaryTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def request(self, *, staff=False, authenticated=False, path="/"):
        request = self.factory.get(path)
        if staff or authenticated:
            request.user = SimpleNamespace(
                is_active=True,
                is_staff=staff,
                is_authenticated=True,
            )
        else:
            request.user = AnonymousUser()
        return request

    def single_context(self, *, staff=False, authenticated=False):
        request = self.request(staff=staff, authenticated=authenticated)
        with (
            patch("xhs_download.views.ultra_hd_is_available", return_value=True),
            patch("xhs_download.views.youtube_merge_mode_is_available", return_value=True),
        ):
            context = get_download_settings_context("home", request)
        context.update(
            {
                "image_items": [],
                "video_items": [],
                "error_message": None,
                "platform_notice": "",
                "provider_notice": "",
                "prefer_ultra_hd": False,
                "prefer_youtube_merge": False,
                "query": "",
            }
        )
        return request, context

    def batch_context(self, *, staff=False):
        request = self.request(staff=staff, path="/batch/")
        show_diagnostics = staff
        return request, {
            "max_links": 20,
            "links_text": "",
            "rows": [],
            "batch_error": "",
            "batch_per_link_timeout_seconds": 12,
            "batch_total_timeout_seconds": 60,
            "batch_progressive_scheduling_timeout_seconds": 60,
            "batch_progressive_max_duration_seconds": 73,
            "batch_parallel_workers": 4,
            "batch_budget": SimpleNamespace(
                wave_count=5,
                theoretical_full_duration_seconds=60,
                fully_schedulable_capacity=20,
                fully_completable_capacity=16,
                max_response_timeout_seconds=73,
            ),
            "batch_summary": {
                "total_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "timeout_count": 0,
                "skipped_count": 0,
                "video_count": 1,
                "image_count": 0,
                "total_duration_ms": 42,
                "batch_wall_duration_ms": 50,
                "provider_counts": {"天天解析": 1},
            },
            "batch_download_allowed": True,
            "batch_raw_links_allowed": staff,
            "show_internal_diagnostics": show_diagnostics,
            "ultra_hd_available": True,
            "youtube_merge_mode_available": True,
            "batch_ultra_hd_checked": False,
            "batch_youtube_merge_checked": False,
            "download_url": "/media/download-images/",
            "download_task_url": "",
            "download_async_tasks_enabled": False,
            "strategy_matrix": [],
            "proxy_image_url": "/media/proxy-image/",
        }

    def test_picker_technical_details_require_staff_not_just_login(self):
        for staff, authenticated in ((False, False), (False, True), (True, True)):
            request, context = self.single_context(staff=staff, authenticated=authenticated)
            self.assertEqual(context["show_internal_diagnostics"], staff)
            for provider, schema in (
                ("yt_dlp_generic", "ytdlp_selected_format_v1"),
                ("tiantianjiexi", "service_selected_format_v1"),
            ):
                result = {
                    **result_display_context(),
                    "status": "success",
                    "video_items": [],
                    "upstream_route": {"mode": "default", "selected_provider": provider},
                    "unexpanded_scope": {"bounded": True},
                    "upstream_candidates": [{
                        "candidate_id": "format-one", "resource_token": "opaque-token",
                        "provider_id": provider, "work_id": "work-one",
                        "work": {"schema": schema, "title": "展示边界测试"},
                        "media_type": "video", "format_label": "1080p",
                        "protocol": "https", "extension": "mp4", "delivery_state": "complete",
                    }],
                }
                for template, values in (
                    ("partials/xhs_results_region.html", {**context, **result}),
                    ("partials/batch_result_card.html", {**context, "row": result}),
                ):
                    with self.subTest(staff=staff, authenticated=authenticated, provider=provider,
                                      template=template):
                        html = render_to_string(template, values, request=request)
                        self.assertIn("data-ytdlp-picker", html)
                        self.assertIn("data-picker-radio", html)
                        for marker in (
                            "查看技术信息与全部原始候选", 'class="ytdlp-technical"',
                            "data-upstream-candidates", "data-upstream-route-status",
                            "data-upstream-scope-status",
                        ):
                            self.assertEqual(marker in html, staff, marker)

    def test_public_single_notice_hides_provider_while_staff_keeps_diagnostics(self):
        result = SimpleNamespace(
            platform="douyin",
            assets=(object(),),
            metadata={"selected_provider": "tiantianjiexi"},
        )
        with patch(
            "xhs_download.views.build_result_display_context",
            return_value=result_display_context(),
        ):
            _request, public_context = self.single_context(staff=False)
            apply_search_result_to_context(
                public_context,
                result,
                platform_key="home",
                prefer_ultra_hd=True,
            )
            _request, staff_context = self.single_context(staff=True)
            apply_search_result_to_context(
                staff_context,
                result,
                platform_key="home",
                prefer_ultra_hd=True,
            )

        self.assertEqual(public_context["provider_notice"], "已获取抖音原画 / 超高清资源。")
        self.assertNotIn("天天解析", public_context["provider_notice"])
        self.assertIn("天天解析", staff_context["provider_notice"])
        self.assertIn("本地解析", staff_context["provider_notice"])

    def test_public_third_party_notice_hides_implementation_names(self):
        result = SimpleNamespace(
            platform="third_party",
            metadata={
                "provider_label": "yt-dlp",
                "selected_provider": "yt_dlp_merge",
            },
        )
        with patch(
            "xhs_download.views.build_result_display_context",
            return_value=result_display_context(),
        ):
            _request, context = self.single_context(staff=False)
            apply_search_result_to_context(
                context,
                result,
                platform_key="home",
                prefer_ultra_hd=False,
            )

        self.assertEqual(context["provider_notice"], "已获取可用高清资源；下载时将合并音频和视频。")
        self.assertNotIn("yt-dlp", context["provider_notice"])
        self.assertNotIn("Provider", context["provider_notice"])

    def test_public_parse_failure_is_generic_and_staff_keeps_exact_diagnostic(self):
        def build(staff):
            request = self.request(staff=staff, path="/results/")
            request.GET = request.GET.copy()
            request.GET["q"] = "https://www.xiaohongshu.com/explore/test"
            with (
                patch(
                    "xhs_download.views.get_adapter_for_url",
                    return_value=SimpleNamespace(platform_id="xiaohongshu"),
                ),
                patch(
                    "xhs_download.views.parse_media_page",
                    side_effect=XhsParseError("tiantianjiexi cache miss at internal stage"),
                ),
                patch("xhs_download.views.ultra_hd_is_available", return_value=True),
                patch("xhs_download.views.youtube_merge_mode_is_available", return_value=True),
            ):
                return build_search_results_context(request, "home")[0]

        public_context = build(False)
        staff_context = build(True)

        self.assertEqual(public_context["error_message"], "解析失败，请检查链接后重试。")
        self.assertNotIn("tiantianjiexi", public_context["error_message"])
        self.assertIn("tiantianjiexi cache miss", staff_context["error_message"])

    def test_public_result_card_hides_strategy_but_keeps_download_contract(self):
        _request, context = self.single_context(staff=False)
        context.update(result_display_context())
        html = render_to_string("partials/xhs_results_region.html", context)
        text = visible_text(html)

        self.assertNotIn("视频直链优先", text)
        self.assertNotIn("Provider", text)
        self.assertIn('data-provider="tiantianjiexi"', html)
        self.assertIn('data-provider-manifest-token="opaque-test-token"', html)
        self.assertIn("12 MB", text)

    def test_staff_result_card_keeps_strategy_diagnostic(self):
        _request, context = self.single_context(staff=True)
        context.update(result_display_context())
        html = render_to_string("partials/xhs_results_region.html", context)

        self.assertIn("视频直链优先", visible_text(html))

    def test_public_batch_card_uses_user_status_and_generic_failure(self):
        row = build_batch_failed_row(
            "https://example.com/source",
            "ConnectionError: tiantianjiexi cache miss",
            duration_ms=42,
        )
        row.update(
            {
                "platform_name": "抖音",
                "post_type": "video",
                "provider_label": "天天解析",
                "video_items": [],
                "image_items": [],
            }
        )
        html = render_to_string(
            "partials/batch_result_card.html",
            {
                "row": row,
                "show_internal_diagnostics": False,
                "batch_download_allowed": True,
                "batch_raw_links_allowed": False,
            },
        )
        text = visible_text(html)

        self.assertIn("解析执行失败，请检查链接后重试。", text)
        self.assertNotIn("ConnectionError", text)
        self.assertNotIn("tiantianjiexi", text)
        self.assertNotIn("42ms", text)
        self.assertNotIn(" failed ", f" {text} ")

    def test_staff_batch_card_keeps_provider_status_latency_and_strategy(self):
        row = {
            "status": "success",
            "source_text": "https://example.com/source",
            "display_title": "素材",
            "base_filename": "material",
            "duration_ms": 42,
            "platform_name": "小红书",
            "post_type": "image",
            "provider_label": "天天解析",
            "video_items": [],
            "image_items": result_display_context(video=False)["image_items"],
        }
        html = render_to_string(
            "partials/batch_result_card.html",
            {
                "row": row,
                "show_internal_diagnostics": True,
                "batch_download_allowed": True,
                "batch_raw_links_allowed": True,
                "proxy_image_url": "/media/proxy-image/",
            },
        )
        text = visible_text(html)

        self.assertIn("Provider 天天解析", text)
        self.assertIn("success · 42ms", text)
        self.assertIn("图片直链优先", text)

    def test_batch_settings_and_budget_are_public_friendly_but_staff_detailed(self):
        public_request, public_context = self.batch_context(staff=False)
        staff_request, staff_context = self.batch_context(staff=True)
        public_text = visible_text(
            render_to_string("batch_parse.html", public_context, request=public_request)
        )
        staff_text = visible_text(
            render_to_string("batch_parse.html", staff_context, request=staff_request)
        )

        for hidden_label in (
            "天天解析",
            "本地解析",
            "批量处理限制",
            "单条处理最长",
            "整批响应最长",
            "并发解析数",
            "预计波次",
            "理论完整耗时",
        ):
            self.assertNotIn(hidden_label, public_text)
        self.assertIn("一次最多 20 条", public_text)
        self.assertNotIn("batch-limits-details", render_to_string(
            "batch_parse.html",
            public_context,
            request=public_request,
        ))
        self.assertIn("天天解析", staff_text)
        self.assertIn("批量处理限制", staff_text)
        self.assertIn("并发解析数", staff_text)
        self.assertIn("42ms", staff_text)

    def test_public_batch_provider_timeout_hides_upstream_implementation(self):
        from xhs_download.services.batch_parse.results import (
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            BATCH_PUBLIC_PROVIDER_TIMEOUT_MESSAGE,
            build_batch_timeout_row,
        )

        row = build_batch_timeout_row(
            "https://example.com/source",
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            duration_ms=42,
            public_reason=BATCH_PUBLIC_PROVIDER_TIMEOUT_MESSAGE,
        )
        public_html = render_to_string(
            "partials/batch_result_card.html",
            {"row": row, "show_internal_diagnostics": False},
        )
        staff_html = render_to_string(
            "partials/batch_result_card.html",
            {"row": row, "show_internal_diagnostics": True},
        )

        self.assertIn("解析服务响应超时", visible_text(public_html))
        self.assertNotIn("上游", visible_text(public_html))
        self.assertIn("上游解析服务响应超时", visible_text(staff_html))

    def test_single_settings_keep_product_claims_without_provider_copy(self):
        public_request, public_context = self.single_context(staff=False)
        staff_request, staff_context = self.single_context(staff=True)
        public_text = visible_text(
            render_to_string("xhs_download.html", public_context, request=public_request)
        )
        staff_text = visible_text(
            render_to_string("xhs_download.html", staff_context, request=staff_request)
        )

        for claim in ("公开内容可尝试解析", "部分源站内容可能有访问限制", "平台覆盖", "保持原始画质", "最高画质"):
            self.assertIn(claim, public_text)
        self.assertNotIn("无使用限制", public_text)
        for diagnostic in ("天天解析", "KuKuTool", "超高清 Provider", "本地解析"):
            self.assertNotIn(diagnostic, public_text)
            self.assertNotIn(diagnostic, staff_text)

    def test_download_route_reason_is_guarded_by_staff_presentation_flag(self):
        source = Path("static/js/xhs_download.js").read_text()

        self.assertIn('page.dataset.showInternalDiagnostics === "true"', source)
        self.assertIn("const completionLabel = showInternalDiagnostics", source)
        self.assertIn(': "正在获取高清资源"', source)
        self.assertIn('presentationText("准备文件", "服务端处理")', source)
        self.assertNotIn(
            '${routeData.route_reason || "已开始保存"}：${filenames.join("、")}',
            source,
        )

    def test_public_continuation_fallback_hides_background_execution(self):
        source = Path("xhs_download/views.py").read_text()

        self.assertIn("超高清资源仍在处理中", source)
        self.assertIn('context.get("show_internal_diagnostics")', source)
