from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse


class QualityCandidatePresentationTests(SimpleTestCase):
    def test_candidate_assets_participate_in_static_versioning(self):
        from djangoProject.context_processors import STATIC_VERSION_FILES
        self.assertIn('static/js/upstream_candidates.js', STATIC_VERSION_FILES)
        self.assertIn('static/css/upstream_candidates.css', STATIC_VERSION_FILES)

    def candidate(self, **changes):
        return dict(candidate_id="candidate-one", resource_token="opaque-token", media_type="video",
                    ordinal=1, provider_id="yt_dlp_generic", metadata_source="upstream_declared",
                    protocol="m3u8_native", delivery_state="manifest", **changes)

    def test_home_candidate_only_is_success_without_download_authority_or_raw_url(self):
        html = render_to_string("partials/xhs_results_region.html", {
            "upstream_candidates": [self.candidate()], "show_internal_diagnostics": False, "base_filename": "sample",
        })
        self.assertIn("已解析到 1 个候选", html)
        self.assertIn('data-candidate-action="open"', html)
        self.assertIn('data-candidate-action="copy"', html)
        self.assertIn('data-resource-token="opaque-token"', html)
        self.assertNotIn("解析失败", html)
        self.assertNotIn('id="download-btn"', html)
        self.assertNotIn('data-compare-candidate-source', html)
        self.assertNotIn('data-url=', html)
        self.assertIn("m3u8_native / manifest", html)
        self.assertIn("未知 / 未知", html)

    def test_batch_candidate_only_is_success_and_has_generic_information_actions(self):
        html = render_to_string("partials/batch_result_card.html", {"row": {
            "status": "success", "source_text": "https://example.org/post", "image_items": [],
            "video_items": [], "upstream_candidates": [self.candidate()], "base_filename": "sample",
        }, "show_internal_diagnostics": False})
        self.assertIn('data-status="success"', html)
        self.assertIn('data-candidate-action="open"', html)
        self.assertNotIn("解析失败", html)
        self.assertNotIn('data-upstream-original-open', html)

    def test_gallery_candidates_are_collapsed_and_truncation_visible(self):
        html = render_to_string("partials/upstream_candidates.html", {
            "upstream_candidates": [self.candidate()], "image_items": [object()],
            "candidates_truncated": 7,
        })
        self.assertNotIn('open>', html.split("</summary>")[0])
        self.assertIn("另有 7 项未展开", html)

    def test_metadata_is_escaped_and_not_promoted_to_verified_quality(self):
        html = render_to_string("partials/upstream_candidates.html", {
            "upstream_candidates": [self.candidate(format_label='<script>alert(1)</script>', fps=60,
                                                    size_bytes=1024, size_is_estimate=True)],
        })
        self.assertNotIn("<script>", html)
        self.assertIn("60 fps", html)
        self.assertIn("（估计）", html)
        self.assertIn("上游声明", html)

    def test_template_candidate_endpoint_matches_registered_backend(self):
        html = render_to_string("partials/upstream_candidates.html", {"upstream_candidates": [self.candidate()]})
        self.assertIn('data-candidate-url="' + reverse("upstream_candidate") + '"', html)

    def test_multiwork_label_and_unknown_completion_are_visible_outside_collapsed_candidates(self):
        html = render_to_string("partials/upstream_candidates.html", {
            "upstream_candidates": [self.candidate(work_label="作品 2 <source>")],
            "image_items": [object()], "unexpanded_scope": {"bounded": True, "completion": "unknown"},
        })
        self.assertIn("完整性未知", html.split("<details")[0])
        self.assertIn("作品 2 &lt;source&gt;", html)
        self.assertNotIn("全部媒体", html)

    def test_retained_tail_is_rendered_with_64_visible_and_separate_counts(self):
        from html.parser import HTMLParser
        class Candidates(HTMLParser):
            rows = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if "data-upstream-candidate" in attrs:
                    self.rows.append(attrs)
        candidates = [self.candidate() for _ in range(130)]
        candidates[-1]["candidate_id"] = "tail-2160"
        candidates[-1]["height"] = 2160
        html = render_to_string("partials/upstream_candidates.html", {
            "upstream_candidates": candidates, "returned_count": 130, "retained_count": 130,
            "unexpanded_count": None,
        })
        parser = Candidates(); parser.feed(html)
        self.assertEqual(len(parser.rows), 130)
        self.assertEqual(sum("hidden" not in row for row in parser.rows), 64)
        self.assertEqual(parser.rows[-1]["data-candidate-id"], "tail-2160")
        self.assertIn("返回 130 · 保留 130", html)
        self.assertIn("上游未展开 未知", html)

    def test_route_summary_is_visible_without_serializing_route_and_context_limit_is_explicit(self):
        html = render_to_string("partials/upstream_candidates.html", {
            "upstream_candidates": [self.candidate(requires_context=True)], "image_items": [object()],
            "upstream_route": {"mode": "manual", "selected_provider": "gallery_dl_generic",
                               "fallback_reason": "首选不可用", "cache_identity": "private-cache-value"},
        })
        self.assertIn("手动路线", html.split("<details")[0])
        self.assertIn("本次上游：gallery_dl_generic", html)
        self.assertIn("回退说明：首选不可用", html)
        self.assertIn("需来源请求上下文", html)
        self.assertNotIn("private-cache-value", html)
        default_html = render_to_string("partials/upstream_candidates.html", {"upstream_route": {"mode": "default"}})
        self.assertIn("默认路线", default_html)
