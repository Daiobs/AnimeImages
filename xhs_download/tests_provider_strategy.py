import itertools
import re
import threading
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from io import StringIO
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import (
    BatchParseRuntimeSetting,
    ProviderAccessMode,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
)
from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError, request_get_with_retry
from .services.platforms.bilibili import api_get_json
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import (
    DouyinKukuToolProvider,
    DouyinMobileFeedProvider,
    DouyinNativeProvider,
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    DouyinUltraRaceProvider,
    build_douyin_result_providers,
)
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .services.platforms.third_party import (
    GenericThirdPartyAdapter,
    configured_third_party_provider_order,
    get_third_party_rule,
)
from .services.platforms.tiantianjiexi_generic import TianTianJieXiGenericProvider
from .services.provider_settings import ProviderSettings
from .services.batch_parse.results import (
    BATCH_PROVIDER_STAGES,
    BatchParseStageTracker,
    build_batch_result_row as service_build_batch_result_row,
    build_batch_result_rows,
    build_batch_summary,
    iter_batch_result_rows,
)
from .services.batch_parse.runtime import (
    build_batch_parse_budget,
    calculate_batch_parse_budget,
)
from .services.batch_parse.view_context import build_batch_parse_context
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_strategy import (
    DOWNLOAD_STRATEGY_BROWSER_DIRECT,
    DOWNLOAD_STRATEGY_SERVER_MERGE,
    build_download_strategy,
)
from .services.rate_limits import LimitResult
from .services.downloads import provider_manifest_allows_public_download
from .views import (
    PROVIDER_HEALTH_TESTS,
    batch_parse_limit_error_message,
    batch_parse_total_timeout_seconds,
    build_result_display_context,
    get_video_size_bytes,
    provider_health_test_by_key,
    run_provider_health_test,
    split_batch_links,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TrackingThreadPoolExecutor:
    instances = []

    def __init__(self, *args, **kwargs):
        self._executor = RealThreadPoolExecutor(*args, **kwargs)
        self._lock = threading.Lock()
        self.outstanding_count = 0
        self.max_outstanding_count = 0
        type(self).instances.append(self)

    def submit(self, *args, **kwargs):
        with self._lock:
            self.outstanding_count += 1
            self.max_outstanding_count = max(
                self.max_outstanding_count,
                self.outstanding_count,
            )
        future = self._executor.submit(*args, **kwargs)

        def mark_finished(_future):
            with self._lock:
                self.outstanding_count -= 1

        future.add_done_callback(mark_finished)
        return future

    def shutdown(self, *args, **kwargs):
        return self._executor.shutdown(*args, **kwargs)


class ProviderDownloadStrategyTests(SimpleTestCase):
    def test_tiantianjiexi_video_uses_native_handoff_without_fetch_cors(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://v5-default.365yg.com/demo.mp4",
                platform="douyin",
                provider="tiantianjiexi",
            )
        )

        self.assertEqual(
            strategy["download_strategy"],
            DOWNLOAD_STRATEGY_BROWSER_DIRECT,
        )
        self.assertEqual(strategy["fallback_strategy"], "server_attachment")
        self.assertFalse(strategy["cors_expected"])
        self.assertFalse(strategy["browser_direct_fetch_safe"])
        self.assertTrue(strategy["browser_native_handoff_capable"])

    def test_kukutool_video_native_handoff_does_not_require_fetch_cors(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://v5-default.365yg.com/demo.mp4",
                platform="douyin",
                provider="kukutool",
            )
        )

        self.assertEqual(
            strategy["download_strategy"],
            DOWNLOAD_STRATEGY_BROWSER_DIRECT,
        )
        self.assertFalse(strategy["cors_expected"])
        self.assertFalse(strategy["browser_direct_fetch_safe"])
        self.assertTrue(strategy["browser_native_handoff_capable"])

    def test_native_public_video_keeps_browser_direct_strategy(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://v5-default.365yg.com/demo.mp4",
                platform="douyin",
            )
        )

        self.assertEqual(
            strategy["download_strategy"],
            DOWNLOAD_STRATEGY_BROWSER_DIRECT,
        )
        self.assertTrue(strategy["browser_direct_fetch_safe"])

    def test_youtube_merge_requires_server_merge(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://rr1---sn.googlevideo.com/videoplayback",
                platform="third_party",
                provider="youtube_merge",
            )
        )

        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_SERVER_MERGE)
        self.assertTrue(strategy["requires_merge"])

    def test_image_prefers_browser_direct(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="image",
                url="https://ci.xiaohongshu.com/demo.jpg",
                platform="xiaohongshu",
            )
        )

        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_DIRECT)

    def test_batch_parse_minute_ip_limit_removed_from_examples_and_docs(self):
        files = (
            ".env.example.local",
            ".env.example.production",
            "docs/production-deploy-checklist.md",
        )

        for relative_path in files:
            with self.subTest(path=relative_path):
                content = (PROJECT_ROOT / relative_path).read_text()
                self.assertNotIn("BATCH_PARSE_MINUTE_IP_LIMIT", content)

    def test_batch_download_reuses_validated_route_and_prepare_contracts(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn("function executeBatchDownload(options)", content)
        self.assertIn("route_only: true", content)
        self.assertIn("prepareBatchServerDownload", content)
        self.assertIn("pollDownloadStatus", content)
        self.assertIn("downloadStartDisposition", content)
        self.assertIn('download_mode: "server_single"', content)
        self.assertIn("executeBatchCompatibilityDownload", content)
        self.assertIn("data-batch-compat-download", content)
        self.assertNotIn("fallbackDirectToPrepared: true", content)

    def test_batch_javascript_is_page_scoped_and_retry_preserves_form(self):
        global_content = (PROJECT_ROOT / "static/js/app.js").read_text()
        batch_content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertNotIn("data-retry-link", global_content)
        self.assertNotIn("batch-parse-page", global_content)
        self.assertIn("retryBatchResultCard", batch_content)
        self.assertIn("replaceBatchResultCard", batch_content)
        self.assertIn("recomputeBatchSummary", batch_content)
        self.assertIn('method: "POST"', batch_content)
        self.assertIn("card.replaceWith(replacement)", batch_content)
        self.assertNotIn("batchTextarea.value = retryButton", batch_content)
        self.assertNotIn("card_html:", batch_content)
        self.assertNotIn("signed_url", batch_content)

    def test_batch_javascript_requests_and_appends_each_completed_card(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn("runProgressiveBatchParse", content)
        self.assertIn("batchStartUrl", content)
        self.assertIn("batchItemUrl", content)
        self.assertIn("appendBatchResultCard", content)
        self.assertIn("for (let index = 0; index < totalCount; index += 1)", content)
        self.assertIn("resultList.appendChild", content)
        self.assertNotIn("batchRunToken", content[content.index("window.localStorage.setItem"):])

    def test_batch_first_preview_priority_is_limited_to_one_image_per_new_card(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()
        template = (PROJECT_ROOT / "templates/partials/batch_result_card.html").read_text()

        self.assertIn("function prioritizeFirstBatchPreview(card)", content)
        self.assertIn('card.querySelector(".batch-result-cover img")', content)
        self.assertIn('setAttribute("loading", "eager")', content)
        self.assertIn('setAttribute("fetchpriority", "high")', content)
        self.assertIn("prioritizeFirstBatchPreview(replacement)", content)
        self.assertIn(
            'batchPage.querySelector(".batch-result-card:not(.is-hidden)")',
            content,
        )
        self.assertNotIn('querySelectorAll(".batch-result-cover img")', content)
        self.assertIn('loading="lazy"', template)
        self.assertIn('decoding="async"', template)
        self.assertNotIn('loading="eager"', template)

    def test_batch_local_storage_only_contains_links(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()
        draft_block = content[content.index('window.localStorage.setItem("animeimages.batchParseDraft.v2"'):]
        draft_block = draft_block[:draft_block.index("}));")]

        self.assertIn("version: 2", draft_block)
        self.assertIn("links,", draft_block)
        self.assertNotIn("ultra_hd:", draft_block)
        self.assertNotIn("youtube_merge:", draft_block)
        self.assertNotIn("card_html", draft_block)
        self.assertNotIn("video_items", draft_block)
        self.assertNotIn("direct_url", draft_block)

    def test_shared_parse_preferences_are_page_agnostic_and_storage_scoped(self):
        preferences_content = (PROJECT_ROOT / "static/js/parse_preferences.js").read_text()
        single_content = (PROJECT_ROOT / "static/js/xhs_download.js").read_text()
        batch_content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn('"animeimages.parsePreferences.v1"', preferences_content)
        self.assertIn('"animeimages.batchParseDraft.v2"', preferences_content)
        self.assertIn('ultra_hd: "ultra_hd_enabled"', preferences_content)
        self.assertIn('youtube_merge: "youtube_merge_enabled"', preferences_content)
        self.assertIn("AnimeImagesParsePreferences", single_content)
        self.assertIn("AnimeImagesParsePreferences", batch_content)
        self.assertNotIn("animeimages.batchParseDraft.v1", batch_content)
        self.assertNotIn("provider_order", preferences_content)
        self.assertNotIn("signed_url", preferences_content)
        self.assertNotIn("manifest", preferences_content)
        self.assertNotIn("card_html", preferences_content)

    def test_batch_retry_uses_effective_available_preferences(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn("function effectiveCheckboxValue(input)", content)
        self.assertIn("input.checked && !input.disabled", content)
        self.assertIn('payload.set("ultra_hd", effectiveCheckboxValue(ultraInput)', content)
        self.assertIn('payload.set("youtube_merge", effectiveCheckboxValue(youtubeInput)', content)

    def test_single_page_keeps_download_settings_out_of_shared_preferences(self):
        content = (PROJECT_ROOT / "static/js/xhs_download.js").read_text()
        save_block = content[content.index("saveSettings({"):]
        save_block = save_block[:save_block.index("});")]

        self.assertIn("download_mode:", save_block)
        self.assertIn("save_dir:", save_block)
        self.assertIn("save_format:", save_block)
        self.assertIn("video_save_mode:", save_block)
        self.assertIn("use_file_picker:", save_block)
        self.assertNotIn("ultra_hd_enabled:", save_block)
        self.assertNotIn("youtube_merge_enabled:", save_block)
        self.assertIn("saveParsePreferences();", content)

    @patch("xhs_download.views.probe_media_size", return_value=2_800_000)
    def test_content_length_size_is_displayed_as_exact(self, _mock_probe):
        result = ParseResult(
            platform="bilibili",
            post_type="video",
            base_filename="exact-size",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://upos-sz-mirrorcos.bilivideo.com/exact.mp4",
                    platform="bilibili",
                ),
            ),
        )

        item = build_result_display_context(result)["video_items"][0]

        self.assertFalse(item["size_is_estimate"])
        self.assertEqual(item["size_label"], "2.7 MB")
        self.assertEqual(item["size_display_label"], "2.7 MB")

    def test_estimated_and_unknown_sizes_have_explicit_display_semantics(self):
        estimated_result = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="estimated-size",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://www.youtube.com/watch?v=demo",
                    platform="third_party",
                    provider="youtube_merge",
                    size_bytes=2_977_148,
                    size_is_estimate=True,
                ),
            ),
        )
        unknown_result = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="unknown-size",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://example.com/unknown.mp4",
                    platform="third_party",
                    provider="youtube_merge",
                ),
            ),
        )

        estimated_item = build_result_display_context(estimated_result)["video_items"][0]
        unknown_item = build_result_display_context(unknown_result)["video_items"][0]

        self.assertTrue(estimated_item["size_is_estimate"])
        self.assertEqual(estimated_item["size_display_label"], "预计 2.8 MB")
        self.assertFalse(unknown_item["size_is_estimate"])
        self.assertEqual(unknown_item["size_display_label"], "大小未知")

    def test_batch_and_single_result_templates_share_size_display_semantics(self):
        batch_template = (
            PROJECT_ROOT / "templates/partials/batch_result_card.html"
        ).read_text()
        single_template = (
            PROJECT_ROOT / "templates/partials/xhs_results_region.html"
        ).read_text()

        self.assertIn("item.size_display_label", batch_template)
        self.assertIn("item.size_display_label", single_template)
        self.assertIn(
            'item.size_display_label|default:item.size_label|default:"大小未知"',
            batch_template,
        )
        self.assertIn(
            'item.size_display_label|default:item.size_label|default:"大小未知"',
            single_template,
        )

    def test_size_provenance_does_not_change_download_strategy_fields(self):
        fields = {
            "media_type": "video",
            "url": "https://www.youtube.com/watch?v=demo",
            "platform": "third_party",
            "provider": "youtube_merge",
            "size_bytes": 2_977_148,
        }

        exact_strategy = build_download_strategy(MediaAsset(**fields))
        estimated_strategy = build_download_strategy(
            MediaAsset(**fields, size_is_estimate=True)
        )
        exact_merge_result = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="exact-merge-size",
            assets=(MediaAsset(**fields),),
        )
        exact_merge_item = build_result_display_context(exact_merge_result)[
            "video_items"
        ][0]

        self.assertEqual(estimated_strategy, exact_strategy)
        self.assertTrue(estimated_strategy["requires_merge"])
        self.assertEqual(exact_merge_item["size_display_label"], "2.8 MB")

    def test_short_video_rule_allows_kukutool_and_tiantianjiexi(self):
        rule = get_third_party_rule("kuaishou")

        self.assertIsNotNone(rule)
        self.assertEqual(rule.provider_ids[0], "kukutool_generic")
        self.assertIn("tiantianjiexi_generic", rule.provider_ids)
        self.assertIn("kukutool_generic", rule.provider_ids)

    def test_youtube_default_provider_order_prefers_ytdlp(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        self.assertEqual(configured_third_party_provider_order(rule)[0], "yt_dlp_generic")

    def test_short_video_default_provider_order_prefers_kukutool(self):
        for platform_id in ("tiktok", "kuaishou"):
            rule = get_third_party_rule(platform_id)
            self.assertIsNotNone(rule)
            self.assertEqual(configured_third_party_provider_order(rule)[0], "kukutool_generic")

    def test_platform_order_filters_unknown_and_rule_unsupported(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        with override_settings(
            THIRD_PARTY_PROVIDER_ORDER_YOUTUBE=(
                "unknown_provider",
                "gallery_dl_generic",
                "tiantianjiexi_generic",
                "kukutool_generic",
                "yt_dlp_generic",
            )
        ):
            self.assertEqual(
                configured_third_party_provider_order(rule),
                ("tiantianjiexi_generic", "kukutool_generic", "yt_dlp_generic"),
            )

    @override_settings(
        THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO=(
            "yt_dlp_generic",
            "kukutool_generic",
            "tiantianjiexi_generic",
            "gallery_dl_generic",
        )
    )
    def test_adapter_iter_provider_specs_uses_configured_order(self):
        rule = get_third_party_rule("kuaishou")
        adapter = GenericThirdPartyAdapter()

        provider_ids = [
            provider_id
            for provider_id, _provider, _normalizer in adapter.iter_provider_specs(
                rule,
                respect_enabled=False,
            )
        ]

        self.assertEqual(
            provider_ids,
            ["yt_dlp_generic", "kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"],
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ORDER_YOUTUBE=("kukutool_generic", "yt_dlp_generic"),
    )
    def test_youtube_provider_order_can_be_overridden(self):
        rule = get_third_party_rule("youtube")

        self.assertEqual(
            configured_third_party_provider_order(rule),
            ("kukutool_generic", "yt_dlp_generic"),
        )

    def test_split_batch_links_dedupes_and_limits(self):
        links = split_batch_links("\n".join(["https://example.com/a", "https://example.com/a"] + [
            f"https://example.com/{index}" for index in range(30)
        ]))

        self.assertEqual(len(links), 20)
        self.assertEqual(links[0], "https://example.com/a")


class BatchParseSchedulerRegressionTests(SimpleTestCase):
    @staticmethod
    def success_row(source_text, duration_ms=0):
        return {
            "status": "success",
            "source_text": source_text,
            "base_filename": source_text.rsplit("/", 1)[-1],
            "duration_ms": duration_ms,
            "video_items": (),
            "image_items": (),
        }

    def test_serial_iterator_yields_each_row_before_starting_the_next(self):
        started_links = []

        def row_builder(_request, source_text, **_kwargs):
            started_links.append(source_text)
            return self.success_row(source_text)

        started_at = monotonic()
        rows = iter_batch_result_rows(
            object(),
            ["https://example.com/1", "https://example.com/2"],
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            deadline=started_at + 1,
            response_deadline=started_at + 2,
            parallel_workers=1,
            per_link_timeout_seconds=0.5,
            scheduling_timeout_seconds=1,
            max_response_timeout_seconds=2,
            row_builder=row_builder,
        )

        first_index, first_row = next(rows)
        self.assertEqual(first_index, 0)
        self.assertEqual(first_row["source_text"], "https://example.com/1")
        self.assertEqual(started_links, ["https://example.com/1"])

        second_index, second_row = next(rows)
        self.assertEqual(second_index, 1)
        self.assertEqual(second_row["source_text"], "https://example.com/2")
        self.assertEqual(
            started_links,
            ["https://example.com/1", "https://example.com/2"],
        )
        with self.assertRaises(StopIteration):
            next(rows)

    @patch(
        "xhs_download.services.batch_parse.results.ThreadPoolExecutor",
        new=TrackingThreadPoolExecutor,
    )
    def test_scheduler_never_submits_more_than_worker_count(self):
        TrackingThreadPoolExecutor.instances.clear()

        def slow_row_builder(_request, source_text, **_kwargs):
            sleep(0.06)
            return self.success_row(source_text, duration_ms=60)

        started_at = monotonic()
        build_batch_result_rows(
            object(),
            [f"https://example.com/{index}" for index in range(5)],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 0.02,
            response_deadline=started_at + 0.2,
            parallel_workers=2,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=0.02,
            max_response_timeout_seconds=0.2,
            row_builder=slow_row_builder,
        )
        sleep(0.08)

        self.assertEqual(len(TrackingThreadPoolExecutor.instances), 1)
        self.assertLessEqual(
            TrackingThreadPoolExecutor.instances[0].max_outstanding_count,
            2,
        )

    def test_scheduler_waits_for_started_work_before_returning(self):
        state_lock = threading.Lock()
        active_count = 0

        def slow_row_builder(_request, source_text, **_kwargs):
            nonlocal active_count
            with state_lock:
                active_count += 1
            try:
                sleep(0.06)
                return self.success_row(source_text, duration_ms=60)
            finally:
                with state_lock:
                    active_count -= 1

        started_at = monotonic()
        build_batch_result_rows(
            object(),
            ["https://example.com/1", "https://example.com/2"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 0.02,
            response_deadline=started_at + 0.2,
            parallel_workers=2,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=0.02,
            max_response_timeout_seconds=0.2,
            row_builder=slow_row_builder,
        )
        with state_lock:
            active_at_return = active_count
        sleep(0.08)

        self.assertEqual(active_at_return, 0)

    def test_cooperative_workers_finish_before_response_boundary(self):
        TrackingThreadPoolExecutor.instances.clear()
        state_lock = threading.Lock()
        active_count = 0

        def cooperative_row_builder(_request, source_text, **kwargs):
            nonlocal active_count
            with state_lock:
                active_count += 1
            try:
                if source_text.endswith("/success"):
                    sleep(0.06)
                    return self.success_row(source_text, duration_ms=60)
                sleep(max(0.0, kwargs["deadline"] - monotonic()))
                return {
                    "status": "timeout",
                    "source_text": source_text,
                    "duration_ms": 100,
                    "error_message": "本条解析超过单条时间限制。",
                }
            finally:
                with state_lock:
                    active_count -= 1

        started_at = monotonic()
        with patch(
            "xhs_download.services.batch_parse.results.ThreadPoolExecutor",
            new=TrackingThreadPoolExecutor,
        ), patch(
            "xhs_download.services.batch_parse.results.logger.warning"
        ) as mock_warning:
            rows = build_batch_result_rows(
                object(),
                [
                    "https://example.com/success",
                    "https://example.com/timeout",
                    "https://example.com/skipped",
                ],
                prefer_ultra_hd=False,
                prefer_youtube_merge=False,
                deadline=started_at + 0.04,
                response_deadline=started_at + 0.25,
                parallel_workers=2,
                per_link_timeout_seconds=0.1,
                scheduling_timeout_seconds=0.04,
                max_response_timeout_seconds=0.25,
                completion_grace_seconds=0.01,
                row_builder=cooperative_row_builder,
            )

        with state_lock:
            active_at_return = active_count
        self.assertEqual(
            [row["status"] for row in rows],
            ["success", "timeout", "skipped"],
        )
        self.assertEqual(active_at_return, 0)
        self.assertEqual(
            TrackingThreadPoolExecutor.instances[0].outstanding_count,
            0,
        )
        mock_warning.assert_not_called()

    def test_uncooperative_worker_cannot_hold_response_open(self):
        TrackingThreadPoolExecutor.instances.clear()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        release_timer = threading.Timer(0.8, release_worker.set)

        def uncooperative_row_builder(_request, source_text, **kwargs):
            kwargs["stage_tracker"].update(
                "tiantianjiexi",
                provider_attempt=True,
            )
            try:
                release_worker.wait(timeout=2)
                return self.success_row(source_text)
            finally:
                worker_finished.set()

        started_at = monotonic()
        release_timer.start()
        try:
            with patch(
                "xhs_download.services.batch_parse.results.ThreadPoolExecutor",
                new=TrackingThreadPoolExecutor,
            ), patch(
                "xhs_download.services.batch_parse.results.logger.warning"
            ) as mock_warning:
                request = Mock(request_id="batch-request-123")
                source_url = (
                    "https://www.douyin.com/video/7611146072161047526"
                    "?token=must-not-be-logged"
                )
                rows = build_batch_result_rows(
                    request,
                    [
                        source_url,
                        "https://example.com/skipped-1",
                        "https://example.com/skipped-2",
                    ],
                    prefer_ultra_hd=False,
                    prefer_youtube_merge=False,
                    deadline=started_at + 0.1,
                    response_deadline=started_at + 0.2,
                    parallel_workers=1,
                    per_link_timeout_seconds=1,
                    scheduling_timeout_seconds=0.1,
                    max_response_timeout_seconds=0.2,
                    completion_grace_seconds=0.01,
                    row_builder=uncooperative_row_builder,
                )
                elapsed = monotonic() - started_at

                self.assertLess(elapsed, 0.5)
                self.assertEqual(
                    [row["status"] for row in rows],
                    ["timeout", "skipped", "skipped"],
                )
                self.assertEqual(
                    rows[0]["error_message"],
                    "本条解析超过批次最大响应时间。",
                )
                mock_warning.assert_called_once()
                self.assertEqual(
                    mock_warning.call_args.args,
                    ("batch_parse_worker_deadline_overrun",),
                )
                log_fields = mock_warning.call_args.kwargs["extra"]
                self.assertEqual(log_fields["unresolved_count"], 1)
                self.assertEqual(log_fields["worker_count"], 1)
                self.assertEqual(log_fields["link_count"], 3)
                self.assertEqual(log_fields["platform"], "douyin")
                self.assertEqual(log_fields["provider_stage"], "tiantianjiexi")
                self.assertEqual(log_fields["attempted_provider_count"], 1)
                self.assertGreaterEqual(log_fields["elapsed_ms"], 0)
                self.assertEqual(log_fields["request_id"], "batch-request-123")
                self.assertEqual(
                    set(log_fields),
                    {
                        "unresolved_count",
                        "worker_count",
                        "link_count",
                        "configured_scheduling_timeout_seconds",
                        "configured_per_link_timeout_seconds",
                        "configured_completion_grace_seconds",
                        "configured_response_timeout_seconds",
                        "platform",
                        "provider_stage",
                        "attempted_provider_count",
                        "elapsed_ms",
                        "request_id",
                    },
                )
                self.assertNotIn(source_url, repr(mock_warning.call_args))
                self.assertNotIn("must-not-be-logged", repr(mock_warning.call_args))
        finally:
            release_worker.set()
            release_timer.cancel()
            release_timer.join(timeout=1)

        self.assertTrue(worker_finished.wait(timeout=1))
        worker_cleanup_deadline = monotonic() + 1
        while (
            TrackingThreadPoolExecutor.instances[0].outstanding_count
            and monotonic() < worker_cleanup_deadline
        ):
            sleep(0.01)
        self.assertEqual(
            TrackingThreadPoolExecutor.instances[0].outstanding_count,
            0,
        )

    def test_stage_tracker_restricts_stage_and_platform_values(self):
        tracker = BatchParseStageTracker(
            platform="untrusted-platform",
            request_id="invalid request id with spaces",
        )
        tracker.start()
        tracker.update("untrusted-stage", provider_attempt=True)

        snapshot = tracker.snapshot()

        self.assertEqual(snapshot["platform"], "unknown")
        self.assertEqual(snapshot["provider_stage"], "unknown")
        self.assertEqual(snapshot["attempted_provider_count"], 0)
        self.assertEqual(snapshot["request_id"], "")
        self.assertIn(snapshot["provider_stage"], BATCH_PROVIDER_STAGES)

    def test_started_items_timeout_and_unscheduled_items_skip(self):
        def slow_row_builder(_request, source_text, **_kwargs):
            sleep(0.06)
            return self.success_row(source_text, duration_ms=60)

        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            [f"https://example.com/{index}" for index in range(4)],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 0.02,
            response_deadline=started_at + 0.2,
            parallel_workers=2,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=0.02,
            max_response_timeout_seconds=0.2,
            row_builder=slow_row_builder,
        )
        sleep(0.08)

        self.assertEqual(
            [row["status"] for row in rows],
            ["timeout", "timeout", "skipped", "skipped"],
        )

    def test_completed_success_inside_response_grace_is_preserved(self):
        def just_late_success(_request, source_text, **kwargs):
            sleep(0.03)
            kwargs["stage_tracker"].update("completed")
            return self.success_row(source_text, duration_ms=30)

        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            ["https://example.com/just-late"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 0.1,
            response_deadline=started_at + 0.1,
            parallel_workers=1,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=0.1,
            max_response_timeout_seconds=0.1,
            completion_grace_seconds=0.05,
            row_builder=just_late_success,
        )

        self.assertEqual(rows[0]["status"], "success")

    def test_completed_success_outside_response_grace_is_timeout(self):
        def too_late_success(_request, source_text, **kwargs):
            sleep(0.05)
            kwargs["stage_tracker"].update("completed")
            return self.success_row(source_text, duration_ms=50)

        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            ["https://example.com/too-late"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 0.1,
            response_deadline=started_at + 0.1,
            parallel_workers=1,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=0.1,
            max_response_timeout_seconds=0.1,
            completion_grace_seconds=0.01,
            row_builder=too_late_success,
        )

        self.assertEqual(rows[0]["status"], "timeout")

    def test_parallel_results_preserve_input_order(self):
        def out_of_order_row_builder(_request, source_text, **_kwargs):
            if source_text.endswith("/0"):
                sleep(0.03)
            return self.success_row(source_text)

        links = [f"https://example.com/{index}" for index in range(4)]
        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            links,
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 1,
            response_deadline=started_at + 2,
            parallel_workers=2,
            per_link_timeout_seconds=0.5,
            scheduling_timeout_seconds=1,
            max_response_timeout_seconds=2,
            row_builder=out_of_order_row_builder,
        )

        self.assertEqual([row["source_text"] for row in rows], links)

    def test_each_item_receives_fresh_execution_deadline_when_it_starts(self):
        observed_budgets = []

        def capture_deadline(_request, source_text, **kwargs):
            observed_budgets.append(kwargs["deadline"] - monotonic())
            if source_text.endswith("/0"):
                sleep(0.04)
            return self.success_row(source_text)

        started_at = monotonic()
        build_batch_result_rows(
            object(),
            ["https://example.com/0", "https://example.com/1"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 1,
            response_deadline=started_at + 2,
            parallel_workers=1,
            per_link_timeout_seconds=0.1,
            scheduling_timeout_seconds=1,
            max_response_timeout_seconds=2,
            row_builder=capture_deadline,
        )

        self.assertEqual(len(observed_budgets), 2)
        for budget in observed_budgets:
            self.assertGreater(budget, 0.08)
            self.assertLessEqual(budget, 0.1)

    def test_next_item_starts_only_after_worker_slot_is_released(self):
        first_finished = False
        second_observed_first_finished = []

        def ordered_row_builder(_request, source_text, **_kwargs):
            nonlocal first_finished
            if source_text.endswith("/0"):
                sleep(0.03)
                first_finished = True
            else:
                second_observed_first_finished.append(first_finished)
            return self.success_row(source_text)

        started_at = monotonic()
        build_batch_result_rows(
            object(),
            ["https://example.com/0", "https://example.com/1"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 1,
            response_deadline=started_at + 2,
            parallel_workers=1,
            per_link_timeout_seconds=0.2,
            scheduling_timeout_seconds=1,
            max_response_timeout_seconds=2,
            row_builder=ordered_row_builder,
        )

        self.assertEqual(second_observed_first_finished, [True])

    def test_one_item_timeout_does_not_change_other_success(self):
        def mixed_row_builder(_request, source_text, **_kwargs):
            if source_text.endswith("/slow"):
                sleep(0.04)
            return self.success_row(source_text)

        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            ["https://example.com/slow", "https://example.com/fast"],
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            deadline=started_at + 1,
            response_deadline=started_at + 2,
            parallel_workers=2,
            per_link_timeout_seconds=0.02,
            scheduling_timeout_seconds=1,
            max_response_timeout_seconds=2,
            row_builder=mixed_row_builder,
        )

        self.assertEqual([row["status"] for row in rows], ["timeout", "success"])

    def test_summary_counts_timeout_separately(self):
        rows = [
            self.success_row("https://example.com/ok", duration_ms=10),
            {"status": "failed", "duration_ms": 20},
            {"status": "timeout", "duration_ms": 30},
            {"status": "skipped", "duration_ms": 0},
        ]

        summary = build_batch_summary(rows, batch_wall_duration_ms=40)

        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failed_count"], 1)
        self.assertEqual(summary["timeout_count"], 1)
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["total_duration_ms"], 60)
        self.assertEqual(summary["batch_wall_duration_ms"], 40)

    def test_summary_prefers_public_provider_label(self):
        row = self.success_row("https://example.com/ok")
        row.update(
            provider_label="本地解析",
            selected_provider="mobile_feed",
            platform="douyin",
        )

        summary = build_batch_summary([row])

        self.assertEqual(summary["provider_counts"], {"本地解析": 1})

    def test_result_row_maps_provider_timeout_to_timeout(self):
        def timeout_parser(_source_text, **_kwargs):
            raise XhsProviderTimeoutError("provider token=secret timed out")

        row = service_build_batch_result_row(
            object(),
            "https://example.com/timeout",
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=1,
            parser=timeout_parser,
            display_context_builder=Mock(),
            request_country_code_builder=Mock(return_value=""),
            query_uses_third_party_provider_func=Mock(return_value=False),
            third_party_provider_is_available_func=Mock(return_value=True),
        )

        self.assertEqual(row["status"], "timeout")
        self.assertEqual(row["error_message"], "上游解析服务响应超时，请稍后重试。")
        self.assertNotIn("secret", row["error_message"])

    def test_result_row_passes_stage_callback_and_marks_completion(self):
        tracker = BatchParseStageTracker(
            platform="douyin",
            request_id="batch-request-456",
        )
        tracker.start()

        def parser(_source_text, **kwargs):
            kwargs["stage_callback"](
                "tiantianjiexi",
                provider_attempt=True,
            )
            return ParseResult(
                platform="douyin",
                post_type="video",
                base_filename="demo",
                assets=(),
            )

        row = service_build_batch_result_row(
            object(),
            "https://www.douyin.com/video/7611146072161047526",
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=1,
            parser=parser,
            display_context_builder=Mock(return_value={}),
            request_country_code_builder=Mock(return_value="CN"),
            query_uses_third_party_provider_func=Mock(return_value=False),
            third_party_provider_is_available_func=Mock(return_value=True),
            stage_tracker=tracker,
        )

        self.assertEqual(row["status"], "success")
        snapshot = tracker.snapshot()
        self.assertEqual(
            snapshot,
            {
                "platform": "douyin",
                "provider_stage": "completed",
                "attempted_provider_count": 1,
                "elapsed_ms": snapshot["elapsed_ms"],
                "request_id": "batch-request-456",
            },
        )

    def test_result_row_maps_non_timeout_error_to_failed(self):
        def failed_parser(_source_text, **_kwargs):
            raise RuntimeError("broken parser")

        row = service_build_batch_result_row(
            object(),
            "https://example.com/failed",
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=1,
            parser=failed_parser,
            display_context_builder=Mock(),
            request_country_code_builder=Mock(return_value=""),
            query_uses_third_party_provider_func=Mock(return_value=False),
            third_party_provider_is_available_func=Mock(return_value=True),
        )

        self.assertEqual(row["status"], "failed")
        self.assertIn("解析执行失败", row["error_message"])

    def test_started_row_with_expired_item_deadline_is_timeout(self):
        parser = Mock()

        row = service_build_batch_result_row(
            object(),
            "https://example.com/expired",
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=1,
            deadline=monotonic() - 1,
            parser=parser,
        )

        self.assertEqual(row["status"], "timeout")
        parser.assert_not_called()

    def test_budget_calculates_waves_and_capacity(self):
        budget = calculate_batch_parse_budget(
            link_count=10,
            worker_count=3,
            per_link_timeout_seconds=8,
            configured_total_timeout_seconds=20,
        )

        self.assertEqual(budget.wave_count, 4)
        self.assertEqual(budget.theoretical_full_duration_seconds, 32)
        self.assertEqual(budget.fully_schedulable_capacity, 9)
        self.assertEqual(budget.fully_completable_capacity, 6)
        self.assertEqual(budget.max_response_timeout_seconds, 29)

    def test_budget_uses_one_wave_when_links_fit_available_workers(self):
        single_link_budget = calculate_batch_parse_budget(
            link_count=1,
            worker_count=4,
            per_link_timeout_seconds=8,
            configured_total_timeout_seconds=60,
        )
        full_worker_wave_budget = calculate_batch_parse_budget(
            link_count=4,
            worker_count=4,
            per_link_timeout_seconds=8,
            configured_total_timeout_seconds=60,
        )

        self.assertEqual(single_link_budget.worker_count, 1)
        self.assertEqual(single_link_budget.wave_count, 1)
        self.assertEqual(single_link_budget.theoretical_full_duration_seconds, 8)
        self.assertEqual(single_link_budget.max_response_timeout_seconds, 9)
        self.assertEqual(full_worker_wave_budget.worker_count, 4)
        self.assertEqual(full_worker_wave_budget.wave_count, 1)
        self.assertEqual(full_worker_wave_budget.fully_schedulable_capacity, 4)
        self.assertEqual(full_worker_wave_budget.fully_completable_capacity, 4)
        self.assertEqual(full_worker_wave_budget.max_response_timeout_seconds, 9)

    def test_budget_long_scheduling_window_still_uses_theoretical_bound(self):
        budget = calculate_batch_parse_budget(
            link_count=6,
            worker_count=2,
            per_link_timeout_seconds=8,
            configured_total_timeout_seconds=60,
        )

        self.assertEqual(budget.wave_count, 3)
        self.assertEqual(budget.theoretical_full_duration_seconds, 24)
        self.assertEqual(budget.scheduling_timeout_seconds, 60)
        self.assertEqual(budget.max_response_timeout_seconds, 25)

    def test_budget_caps_effective_response_time(self):
        budget = calculate_batch_parse_budget(
            link_count=20,
            worker_count=4,
            per_link_timeout_seconds=999,
            configured_total_timeout_seconds=999,
            max_scheduling_timeout_seconds=120,
            max_per_link_timeout_seconds=30,
            completion_grace_seconds=99,
        )

        self.assertEqual(budget.scheduling_timeout_seconds, 120)
        self.assertEqual(budget.per_link_timeout_seconds, 30)
        self.assertEqual(budget.completion_grace_seconds, 2)
        self.assertEqual(budget.max_response_timeout_seconds, 152)

    @override_settings(
        BATCH_PARSE_PARALLEL_WORKERS=4,
        BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=8,
        BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS=1,
        BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS=12,
        BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60,
        TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2,
    )
    def test_ultra_budget_uses_serial_tiantian_lane_and_response_cap(self):
        regular_budget = build_batch_parse_budget(7, prefer_ultra_hd=False)
        ultra_budget = build_batch_parse_budget(7, prefer_ultra_hd=True)

        self.assertEqual(regular_budget.worker_count, 4)
        self.assertEqual(ultra_budget.worker_count, 1)
        self.assertEqual(ultra_budget.wave_count, 7)
        self.assertEqual(ultra_budget.theoretical_full_duration_seconds, 84)
        self.assertEqual(ultra_budget.scheduling_timeout_seconds, 54)
        self.assertEqual(ultra_budget.completion_grace_seconds, 1)
        self.assertEqual(ultra_budget.max_response_timeout_seconds, 55)

    @override_settings(
        BATCH_PARSE_PARALLEL_WORKERS=4,
        BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS=1,
        BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS=12,
        BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60,
        TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2,
    )
    def test_ultra_scheduler_serializes_tiantian_batch_requests(self):
        state_lock = threading.Lock()
        active_count = 0
        max_active_count = 0

        def concurrency_sensitive_row_builder(_request, source_text, **_kwargs):
            nonlocal active_count, max_active_count
            with state_lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
                over_limit = active_count > 1
            try:
                sleep(0.02)
                if over_limit:
                    return {
                        "status": "failed",
                        "source_text": source_text,
                        "duration_ms": 20,
                        "error_message": "provider concurrency limited",
                    }
                return self.success_row(source_text, duration_ms=20)
            finally:
                with state_lock:
                    active_count -= 1

        links = [f"https://www.douyin.com/video/{index}" for index in range(7)]
        budget = build_batch_parse_budget(len(links), prefer_ultra_hd=True)
        started_at = monotonic()
        rows = build_batch_result_rows(
            object(),
            links,
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            deadline=started_at + budget.scheduling_timeout_seconds,
            response_deadline=started_at + budget.max_response_timeout_seconds,
            parallel_workers=budget.worker_count,
            per_link_timeout_seconds=budget.per_link_timeout_seconds,
            scheduling_timeout_seconds=budget.scheduling_timeout_seconds,
            max_response_timeout_seconds=budget.max_response_timeout_seconds,
            completion_grace_seconds=budget.completion_grace_seconds,
            row_builder=concurrency_sensitive_row_builder,
        )

        self.assertEqual(max_active_count, 1)
        self.assertEqual([row["status"] for row in rows], ["success"] * 7)

    def test_view_context_passes_displayed_response_budget_to_scheduler(self):
        budget = calculate_batch_parse_budget(
            link_count=4,
            worker_count=4,
            per_link_timeout_seconds=8,
            configured_total_timeout_seconds=60,
        )
        request = RequestFactory().post(
            "/batch/",
            {
                "links": "\n".join(
                    f"https://example.com/{index}" for index in range(4)
                )
            },
        )
        request.user = Mock(is_active=True, is_staff=True)
        rows_builder = Mock(return_value=[])

        with patch(
            "xhs_download.services.batch_parse.view_context.build_batch_parse_budget",
            return_value=budget,
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_max_links",
            return_value=4,
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_rate_limit_results",
            return_value=(),
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_total_timeout_seconds",
            return_value=60,
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_access_allowed",
            return_value=True,
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_public_ultra_hd_allowed",
            return_value=False,
        ), patch(
            "xhs_download.services.batch_parse.view_context.batch_parse_public_youtube_merge_allowed",
            return_value=False,
        ), patch(
            "xhs_download.services.batch_parse.view_context.time.monotonic",
            side_effect=[100.0, 100.25],
        ):
            context = build_batch_parse_context(
                request,
                ultra_hd_available_func=Mock(return_value=False),
                youtube_merge_mode_available_func=Mock(return_value=False),
                row_builder=Mock(),
                rows_builder=rows_builder,
                strategy_matrix={},
                proxy_image_url="/proxy/image/",
            )

        scheduler_kwargs = rows_builder.call_args.kwargs
        self.assertEqual(
            scheduler_kwargs["deadline"],
            100.0 + budget.scheduling_timeout_seconds,
        )
        self.assertEqual(
            scheduler_kwargs["response_deadline"],
            100.0 + budget.max_response_timeout_seconds,
        )
        self.assertEqual(
            context["batch_budget"].max_response_timeout_seconds,
            scheduler_kwargs["response_deadline"] - 100.0,
        )


class BatchParseAdapterDeadlineTests(SimpleTestCase):
    @patch("xhs_download.services.common.requests.get")
    def test_retry_helper_does_not_start_request_after_deadline(self, mock_get):
        with self.assertRaises(requests.Timeout):
            request_get_with_retry(
                "https://example.com/resource",
                retries=2,
                deadline_monotonic=monotonic() - 1,
            )

        mock_get.assert_not_called()

    @patch("xhs_download.services.platforms.xiaohongshu.request_get_with_retry")
    def test_xiaohongshu_adapter_caps_request_to_item_deadline(self, mock_get):
        response = Mock(
            text="<html><body></body></html>",
            url="https://www.xiaohongshu.com/explore/example",
        )
        response.close = Mock()
        mock_get.return_value = response
        deadline = monotonic() + 0.5

        XiaohongshuAdapter().parse(
            "https://www.xiaohongshu.com/explore/example",
            timeout_seconds=5,
            deadline_monotonic=deadline,
        )

        self.assertLessEqual(mock_get.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(mock_get.call_args.kwargs["deadline_monotonic"], deadline)

    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_bilibili_api_caps_request_to_item_deadline(self, mock_get):
        response = Mock()
        response.json.return_value = {"code": 0, "data": {"title": "demo"}}
        response.close = Mock()
        mock_get.return_value = response
        deadline = monotonic() + 0.5

        data = api_get_json(
            "https://api.bilibili.com/x/web-interface/view",
            {"bvid": "BV1234567890"},
            deadline_monotonic=deadline,
        )

        self.assertEqual(data["title"], "demo")
        self.assertLessEqual(mock_get.call_args.kwargs["timeout"], 0.5)
        self.assertEqual(mock_get.call_args.kwargs["deadline_monotonic"], deadline)

    def test_douyin_mobile_feed_receives_item_deadline(self):
        adapter = Mock()
        adapter.fetch_mobile_feed_result.return_value = None
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7611146072161047526",
            aweme_id="7611146072161047526",
            deadline_monotonic=monotonic() + 0.5,
        )

        DouyinMobileFeedProvider().parse(adapter, context)

        kwargs = adapter.fetch_mobile_feed_result.call_args.kwargs
        self.assertLessEqual(kwargs["request_timeout"], 0.5)
        self.assertEqual(kwargs["request_deadline"], context.deadline_monotonic)

    def test_douyin_adapter_reports_fixed_provider_stages(self):
        class FakeProvider:
            def __init__(self, name, result=None):
                self.name = name
                self.result = result

            def parse(self, _adapter, _context):
                return self.result

        result = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="native",
                ),
            ),
        )
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7611146072161047526",
            aweme_id="7611146072161047526",
            deadline_monotonic=monotonic() + 1,
        )
        adapter = DouyinAdapter()
        stages = []

        def record_stage(stage, **kwargs):
            stages.append((stage, kwargs))

        with patch.object(
            adapter,
            "build_parse_context",
            return_value=context,
        ), patch.object(
            adapter,
            "get_result_providers",
            return_value=(
                FakeProvider("tiantianjiexi"),
                FakeProvider("native", result),
            ),
        ):
            parsed = adapter.parse(
                context.url,
                prefer_ultra_hd=True,
                stage_callback=record_stage,
            )

        self.assertIs(parsed, result)
        self.assertEqual(
            stages,
            [
                ("resolve", {}),
                ("tiantianjiexi", {"provider_attempt": True}),
                ("native", {"provider_attempt": True}),
            ],
        )


@override_settings(
    BATCH_PARSE_IP_INTERVAL_SECONDS=0,
    BATCH_PARSE_GLOBAL_MINUTE_LIMIT=0,
    BATCH_PARSE_DAILY_IP_LIMIT=0,
)
class BatchParseViewTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="staff",
            password="password",
            is_staff=True,
        )

    def test_batch_parse_page_renders_strategy_matrix(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "解析设置")
        self.assertContains(response, "batchSettingsModal")
        self.assertContains(response, "batch-settings-panel")
        self.assertContains(response, "batch-submit-spinner")
        self.assertContains(response, "高级说明：下载策略矩阵")
        self.assertContains(response, "批量解析")
        self.assertNotContains(response, "alert-warning text-start mt-3 mb-0")
        self.assertNotContains(response, "当前批量解析仅管理员可用")
        self.assertNotContains(response, "batch-option-row")
        content = response.content.decode()
        self.assertGreater(content.index("高级说明：下载策略矩阵"), content.index("batch-form"))
        self.assertGreater(content.index('name="ultra_hd"'), content.index("batch-settings-panel"))
        self.assertGreater(content.index('name="youtube_merge"'), content.index("batch-settings-panel"))
        self.assertContains(response, reverse("batch_parse_start"))
        self.assertContains(response, reverse("batch_parse_item"))
        self.assertContains(response, "data-batch-results")

    def test_batch_settings_separate_preferences_from_read_only_limits(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))
        content = response.content.decode()
        settings_panel = content[
            content.index('class="batch-settings-panel"'):
            content.index('class="modal-footer"')
        ]
        element_ids = re.findall(r'\sid="([^"]+)"', content)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<details class="batch-limits-details">', html=False)
        self.assertContains(response, "批量处理限制")
        self.assertNotIn("本次最多链接数", settings_panel)
        self.assertNotIn("同步回退响应上限", settings_panel)
        self.assertNotIn('<details class="batch-limits-details" open', content)
        self.assertIn('for="batch-ultra-hd-setting"', content)
        self.assertIn('for="batch-youtube-merge-setting"', content)
        self.assertIn('id="batch-cancel-settings-btn"', content)
        self.assertIn('id="batch-save-settings-btn"', content)
        self.assertIn("取消", content)
        self.assertEqual(len(element_ids), len(set(element_ids)))

    def test_batch_settings_use_explicit_save_and_restore_on_modal_close(self):
        content = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn("function saveBatchParsePreferences()", content)
        self.assertIn(
            'saveSettingsButton.addEventListener("click", saveBatchParsePreferences)',
            content,
        )
        self.assertIn('settingsModal.addEventListener("show.bs.modal"', content)
        self.assertIn('settingsModal.addEventListener("hidden.bs.modal"', content)
        self.assertIn("if (!settingsSaveCommitted)", content)
        self.assertIn("applyParsePreferences(parsePreferences)", content)
        self.assertNotIn("function updateSharedPreference", content)
        self.assertNotIn(
            'input.addEventListener("change", () => updateSharedPreference(input))',
            content,
        )

    def test_batch_loads_shared_preferences_before_page_script(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))
        content = response.content.decode()

        self.assertLess(
            content.index("js/parse_preferences.js?v="),
            content.index("js/batch_parse.js?v="),
        )
        self.assertContains(response, "抖音原画/超清")
        self.assertContains(response, "YouTube 最高画质合并")

    def test_batch_progress_endpoints_are_post_only_and_use_batch_access_gate(self):
        self.client.force_login(self.staff_user)
        start_get_response = self.client.get(reverse("batch_parse_start"))
        item_get_response = self.client.get(reverse("batch_parse_item"))
        self.client.logout()
        anonymous_start_response = self.client.post(
            reverse("batch_parse_start"),
            {"links": "https://example.com/1"},
        )
        anonymous_item_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": "invalid", "index": "0"},
        )

        self.assertEqual(start_get_response.status_code, 405)
        self.assertEqual(item_get_response.status_code, 405)
        self.assertEqual(anonymous_start_response.status_code, 404)
        self.assertEqual(anonymous_item_response.status_code, 404)

    def test_batch_progress_endpoints_enforce_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff_user)

        start_response = csrf_client.post(
            reverse("batch_parse_start"),
            {"links": "https://example.com/1"},
        )
        item_response = csrf_client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": "invalid", "index": "0"},
        )

        self.assertEqual(start_response.status_code, 403)
        self.assertEqual(item_response.status_code, 403)

    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_returns_each_shared_card_in_input_order(self, mock_rows):
        self.client.force_login(self.staff_user)
        mock_rows.side_effect = (
            [
                {
                    "status": "success",
                    "source_text": "https://example.com/1",
                    "base_filename": "first_video",
                    "duration_ms": 4,
                    "platform": "douyin",
                    "platform_name": "抖音",
                    "post_type": "video",
                    "provider_label": "天天解析",
                    "video_items": (),
                    "image_items": (),
                }
            ],
            [
                {
                    "status": "failed",
                    "source_text": "https://example.com/2",
                    "duration_ms": 5,
                    "error_message": "解析执行失败。",
                }
            ],
        )

        start_response = self.client.post(
            reverse("batch_parse_start"),
            {
                "links": "https://example.com/1\nhttps://example.com/2",
                "ultra_hd": "1",
            },
        )
        start_payload = start_response.json()
        first_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": start_payload["batch_run_token"], "index": "0"},
        )
        second_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": start_payload["batch_run_token"], "index": "1"},
        )

        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_payload["total_count"], 2)
        self.assertIn("first_video", first_response.json()["card_html"])
        self.assertIn("解析执行失败。", second_response.json()["card_html"])
        self.assertEqual(mock_rows.call_count, 2)
        self.assertEqual(mock_rows.call_args_list[0].args[1], ["https://example.com/1"])
        self.assertEqual(mock_rows.call_args_list[1].args[1], ["https://example.com/2"])

    def test_batch_progress_rate_limits_the_whole_batch_once(self):
        self.client.force_login(self.staff_user)

        with patch(
            "xhs_download.services.batch_parse.progress.batch_parse_rate_limit_results",
            return_value=(),
        ) as mock_limits:
            response = self.client.post(
                reverse("batch_parse_start"),
                {"links": "https://example.com/1\nhttps://example.com/2"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_limits.call_count, 1)
        self.assertEqual(mock_limits.call_args.args[1], 2)

    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_token_item_can_only_be_consumed_once(self, mock_rows):
        self.client.force_login(self.staff_user)
        mock_rows.return_value = [
            {
                "status": "failed",
                "source_text": "https://example.com/1",
                "duration_ms": 1,
                "error_message": "解析执行失败。",
            }
        ]
        start_response = self.client.post(
            reverse("batch_parse_start"),
            {"links": "https://example.com/1"},
        )
        payload = {
            "batch_run_token": start_response.json()["batch_run_token"],
            "index": "0",
        }

        first_response = self.client.post(reverse("batch_parse_item"), payload)
        duplicate_response = self.client.post(reverse("batch_parse_item"), payload)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(duplicate_response.status_code, 409)
        self.assertEqual(duplicate_response.json()["error_code"], "BATCH_ITEM_ALREADY_CONSUMED")
        mock_rows.assert_called_once()

    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_rejects_out_of_order_items(self, mock_rows):
        self.client.force_login(self.staff_user)
        mock_rows.return_value = [
            {
                "status": "failed",
                "source_text": "https://example.com/item",
                "duration_ms": 1,
                "error_message": "解析执行失败。",
            }
        ]
        start_response = self.client.post(
            reverse("batch_parse_start"),
            {"links": "https://example.com/1\nhttps://example.com/2"},
        )
        token = start_response.json()["batch_run_token"]

        early_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": token, "index": "1"},
        )

        self.assertEqual(early_response.status_code, 409)
        self.assertEqual(
            early_response.json()["error_code"],
            "BATCH_ITEM_OUT_OF_ORDER",
        )
        mock_rows.assert_not_called()

        first_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": token, "index": "0"},
        )
        second_response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": token, "index": "1"},
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(mock_rows.call_count, 2)

    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_rejects_tampered_token_before_parse(self, mock_rows):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": "tampered", "index": "0"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "INVALID_BATCH_RUN_TOKEN")
        mock_rows.assert_not_called()

    @patch(
        "xhs_download.services.batch_parse.progress.claim_batch_progress_item",
        return_value="claimed",
    )
    @patch("xhs_download.services.batch_parse.progress.load_batch_progress_token")
    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_marks_unstarted_item_skipped_after_scheduling_window(
        self,
        mock_rows,
        mock_load_token,
        _mock_claim,
    ):
        self.client.force_login(self.staff_user)
        mock_load_token.return_value = Mock(
            links=("https://example.com/late",),
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            started_at_epoch=0,
            scheduling_timeout_seconds=1,
        )

        response = self.client.post(
            reverse("batch_parse_item"),
            {"batch_run_token": "signed-token", "index": "0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "skipped")
        self.assertIn("等待执行时批次处理窗口已结束", response.json()["card_html"])
        mock_rows.assert_not_called()

    @patch("xhs_download.views.build_batch_result_rows")
    def test_batch_progress_public_item_keeps_download_and_hides_raw_tools(self, mock_rows):
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "access_mode": ProviderAccessMode.PUBLIC,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )
        signed_url = "https://v5-default.365yg.com/private.mp4?token=secret"
        mock_rows.return_value = [
            {
                "status": "success",
                "source_text": "https://example.com/1",
                "base_filename": "public_progress",
                "duration_ms": 5,
                "platform": "douyin",
                "platform_name": "抖音",
                "post_type": "video",
                "video_items": ({"direct_url": signed_url, "size_label": "1 MB"},),
                "image_items": (),
            }
        ]
        start_response = self.client.post(
            reverse("batch_parse_start"),
            {"links": "https://example.com/1"},
        )

        item_response = self.client.post(
            reverse("batch_parse_item"),
            {
                "batch_run_token": start_response.json()["batch_run_token"],
                "index": "0",
            },
        )

        self.assertEqual(item_response.status_code, 200)
        card_html = item_response.json()["card_html"]
        self.assertIn("data-batch-download", card_html)
        self.assertIn("data-url=", card_html)
        self.assertNotIn("data-batch-direct-url", card_html)
        self.assertNotIn("复制直链", card_html)
        self.assertNotIn("打开直链", card_html)

    @override_settings(
        BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=8,
        BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS=12,
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health")
    def test_batch_parse_defaults_ultra_hd_checked_when_available(self, mock_health):
        self.client.force_login(self.staff_user)
        ProviderRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={"douyin_ultra_mode": ProviderAccessMode.STAFF},
        )
        mock_health.return_value = {"available": True}

        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('type="hidden" name="ultra_hd" value="0"', content)
        self.assertRegex(content, r'name="ultra_hd"[\s\S]+value="1"[\s\S]+checked')

    @override_settings(ULTRA_HD_PROVIDER_ENABLED=True, KUKUTOOL_PROVIDER_ENABLED=True)
    @patch("xhs_download.views.get_kukutool_provider_health")
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_respects_explicit_ultra_hd_off(self, mock_parse, mock_health):
        self.client.force_login(self.staff_user)
        ProviderRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={"douyin_ultra_mode": ProviderAccessMode.STAFF},
        )
        mock_health.return_value = {"available": True}
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="normal_video",
            assets=(),
        )

        response = self.client.post(
            reverse("batch_parse"),
            {
                "links": "https://www.douyin.com/video/7611146072161047526",
                "ultra_hd": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(mock_parse.call_args.kwargs["prefer_ultra_hd"])
        self.assertContains(response, "同步回退响应上限：9.0 秒")

    @override_settings(BATCH_PARSE_TOTAL_TIMEOUT_SECONDS="")
    def test_batch_parse_total_timeout_defaults_to_sixty_seconds(self):
        self.assertEqual(batch_parse_total_timeout_seconds(), 60.0)

    @override_settings(
        BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=8,
        BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS=12,
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health")
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_ultra_hd_uses_twelve_second_per_link_floor(self, mock_parse, mock_health):
        self.client.force_login(self.staff_user)
        ProviderRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={"douyin_ultra_mode": ProviderAccessMode.STAFF},
        )
        mock_health.return_value = {"available": True}
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="ultra_video",
            assets=(),
        )

        response = self.client.post(
            reverse("batch_parse"),
            {
                "links": "https://www.douyin.com/video/7611146072161047526",
                "ultra_hd": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_parse.call_args.kwargs["prefer_ultra_hd"])
        self.assertGreater(mock_parse.call_args.kwargs["timeout_seconds"], 11.9)
        self.assertLessEqual(mock_parse.call_args.kwargs["timeout_seconds"], 12.0)
        self.assertContains(response, "12.0 秒")
        self.assertContains(response, "同步回退响应上限：13.0 秒")

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True, BATCH_PARSE_IP_INTERVAL_SECONDS=20)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_public_click_interval_uses_twenty_second_message(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="first",
            assets=(),
        )

        first = self.client.post(reverse("batch_parse"), {"links": "https://example.com/1"})
        second = self.client.post(reverse("batch_parse"), {"links": "https://example.com/2"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertContains(second, "批量解析请求过于频繁，请约")
        self.assertContains(second, "秒后再试")
        self.assertNotContains(second, "今日额度已用尽")

    @override_settings(BATCH_PARSE_IP_INTERVAL_SECONDS=20, BATCH_PARSE_DAILY_IP_LIMIT=1)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_staff_bypasses_rate_limits(self, mock_parse):
        self.client.force_login(self.staff_user)
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="staff_video",
            assets=(),
        )

        first = self.client.post(reverse("batch_parse"), {"links": "https://example.com/1"})
        second = self.client.post(reverse("batch_parse"), {"links": "https://example.com/2"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotContains(second, "批量解析请求过于频繁")
        self.assertNotContains(second, "今日额度已用尽")

    def test_batch_parse_daily_limit_message_avoids_huge_seconds(self):
        message = batch_parse_limit_error_message(
            "daily_ip",
            LimitResult(allowed=False, current=21, limit=20, retry_after=22163),
        )

        self.assertIn("今日额度已用尽", message)
        self.assertIn("21/20", message)
        self.assertNotIn("22163", message)

    def test_batch_parse_non_staff_default_not_accessible(self):
        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 404)

    @patch("xhs_download.views.parse_media_page")
    def test_batch_runtime_setting_public_mode_overrides_env(self, mock_parse):
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "access_mode": ProviderAccessMode.PUBLIC,
                "public_max_links": 2,
                "max_links": 20,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="public_video",
            assets=(),
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "\n".join(f"https://example.com/{index}" for index in range(5))},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "总链接 2")

    def test_batch_runtime_setting_disabled_blocks_staff(self):
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={"access_mode": ProviderAccessMode.DISABLED},
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 404)

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True, BATCH_PARSE_PUBLIC_MAX_LINKS=3)
    def test_batch_parse_public_mode_truncates_to_public_max(self):
        response = self.client.post(
            reverse("batch_parse"),
            {"links": "\n".join(f"https://example.com/{index}" for index in range(8))},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "总链接 3")

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_post_renders_successful_result(self, mock_parse):
        self.client.force_login(self.staff_user)
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo_video",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                    size_bytes=123456,
                    quality_label="原画",
                    cover_url="https://p3.douyinpic.com/cover.jpeg",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi"},
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "demo_video")
        self.assertContains(response, "Provider 天天解析")
        self.assertContains(response, "120.6 KB")
        self.assertContains(response, "batch-result-cover")
        self.assertContains(response, "复制直链")
        self.assertContains(response, "打开直链")
        self.assertContains(response, "高速直连")
        self.assertContains(response, "兼容下载")
        self.assertContains(response, "data-batch-direct-url")
        self.assertContains(response, "data-batch-direct-download")
        self.assertContains(response, "data-batch-download")
        self.assertContains(response, "data-batch-download-progress")
        self.assertContains(response, 'onerror="retryImage(this)"')
        self.assertNotContains(response, "v5-default.365yg.com</span>")
        self.assertNotContains(response, "质量未知")
        self.assertNotContains(response, "浏览器 Blob 优先</small>")
        self.assertNotContains(response, 'target="_blank" rel="noopener noreferrer">\n                                                                    浏览器下载')
        content = response.content.decode()
        self.assertGreater(content.index("高级说明：下载策略矩阵"), content.index("解析结果"))
        mock_parse.assert_called_once()
        call_kwargs = mock_parse.call_args.kwargs
        self.assertLessEqual(call_kwargs["timeout_seconds"], 8.0)
        self.assertGreater(call_kwargs["timeout_seconds"], 0)
        self.assertIn("deadline_monotonic", call_kwargs)
        self.assertEqual(call_kwargs["douyin_provider_order"], ("tiantianjiexi", "native"))

    @patch("xhs_download.views.probe_media_size")
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_labels_native_mobile_feed_provider_for_users(
        self,
        mock_parse,
        mock_probe_media_size,
    ):
        self.client.force_login(self.staff_user)
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="native_fallback_video",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="native",
                ),
            ),
            metadata={"selected_provider": "mobile_feed"},
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider 本地解析")
        self.assertNotContains(response, "Provider mobile_feed")
        mock_parse.assert_called_once()
        mock_probe_media_size.assert_not_called()

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_uses_parallel_workers(self, mock_parse):
        self.client.force_login(self.staff_user)
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "parallel_workers": 4,
                "total_timeout_seconds": 5,
                "per_link_timeout_seconds": 1,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )
        lock = threading.Lock()
        active_count = 0
        max_active_count = 0

        def parse_side_effect(url, **_kwargs):
            nonlocal active_count, max_active_count
            with lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
            try:
                sleep(0.05)
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename=url.rsplit("/", 1)[-1],
                    assets=(),
                )
            finally:
                with lock:
                    active_count -= 1

        mock_parse.side_effect = parse_side_effect

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "\n".join(f"https://example.com/{index}" for index in range(4))},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "成功 4")
        self.assertGreater(max_active_count, 1)

    @override_settings(BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=2)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_passes_per_link_timeout(self, mock_parse):
        self.client.force_login(self.staff_user)
        mock_parse.side_effect = RuntimeError("slow provider")

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RuntimeError")
        self.assertLessEqual(mock_parse.call_args.kwargs["timeout_seconds"], 2.0)

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_single_failure_does_not_block_other_links(self, mock_parse):
        self.client.force_login(self.staff_user)

        def parse_side_effect(url, **_kwargs):
            if "bad" in url:
                raise RuntimeError("boom")
            return ParseResult(
                platform="douyin",
                post_type="video",
                base_filename="ok_video",
                assets=(
                    MediaAsset(
                        media_type="video",
                        url="https://v5-default.365yg.com/demo.mp4",
                        platform="douyin",
                        provider="tiantianjiexi",
                    ),
                ),
            )

        mock_parse.side_effect = parse_side_effect

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/bad\nhttps://example.com/ok"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ok_video")
        self.assertContains(response, "RuntimeError")
        self.assertContains(response, "复制错误")
        self.assertContains(response, "复制原链接")
        self.assertContains(response, "重新解析此链接")
        self.assertContains(response, "成功 1")
        self.assertContains(response, "失败 1")

    @override_settings(BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=1, BATCH_PARSE_PARALLEL_WORKERS=1)
    @patch("xhs_download.views.time.monotonic")
    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_parse_total_timeout_marks_remaining_skipped(self, mock_row, mock_monotonic):
        self.client.force_login(self.staff_user)
        mock_monotonic.side_effect = itertools.chain(
            [100, 100, 102],
            itertools.repeat(102),
        )
        mock_row.return_value = {
            "status": "success",
            "source_text": "https://example.com/1",
            "base_filename": "first_video",
            "duration_ms": 0,
            "video_items": (),
            "image_items": (),
        }

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/1\nhttps://example.com/2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "first_video")
        self.assertContains(response, "等待执行时批次处理窗口已结束")

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_public_mode_renders_download_contract_without_raw_tools(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="public_video",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/private-demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                ),
            ),
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "public_video")
        self.assertNotContains(response, 'href="https://v5-default.365yg.com/private-demo.mp4"')
        self.assertNotContains(response, "复制全部成功视频直链")
        self.assertNotContains(response, "data-batch-direct-url")
        self.assertContains(response, "data-batch-download")
        self.assertContains(response, 'data-url="https://v5-default.365yg.com/private-demo.mp4"')

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_public_mode_sanitizes_error_and_hides_strategy_matrix(self, mock_parse):
        mock_parse.side_effect = RuntimeError(
            "failed https://v5-default.365yg.com/a.mp4?token=secret&signature=bad"
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "[url-redacted]")
        self.assertNotContains(response, "token=secret")
        self.assertNotContains(response, "signature=bad")
        self.assertNotContains(response, "高级说明：下载策略矩阵")


    def test_batch_retry_is_post_only_and_uses_batch_access_gate(self):
        self.client.force_login(self.staff_user)

        get_response = self.client.get(reverse("batch_parse_retry"))
        self.client.logout()
        anonymous_response = self.client.post(
            reverse("batch_parse_retry"),
            {"link": "https://example.com/1"},
        )

        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(anonymous_response.status_code, 404)

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_enforces_csrf(self, mock_row):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff_user)
        csrf_client.get(reverse("batch_parse"))
        mock_row.return_value = {
            "status": "failed",
            "source_text": "https://example.com/1",
            "duration_ms": 1,
            "error_message": "解析执行失败",
        }

        response = csrf_client.post(
            reverse("batch_parse_retry"),
            {"link": "https://example.com/1"},
        )

        self.assertEqual(response.status_code, 403)
        mock_row.assert_not_called()

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_accepts_exactly_one_link(self, mock_row):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("batch_parse_retry"),
            {"link": "https://example.com/1\nhttps://example.com/2"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "INVALID_BATCH_RETRY_LINK")
        mock_row.assert_not_called()

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_only_parses_target_and_returns_shared_card(self, mock_row):
        self.client.force_login(self.staff_user)
        mock_row.return_value = {
            "status": "success",
            "source_text": "https://example.com/target",
            "base_filename": "retried_video",
            "duration_ms": 5,
            "platform": "douyin",
            "platform_name": "抖音",
            "post_type": "video",
            "provider_label": "天天解析",
            "video_items": (),
            "image_items": (),
        }

        response = self.client.post(
            reverse("batch_parse_retry"),
            {
                "link": "https://example.com/target",
                "ultra_hd": "0",
                "youtube_merge": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "success")
        self.assertIn("partials/batch_result_card.html", [template.name for template in response.templates])
        self.assertIn("retried_video", payload["card_html"])
        self.assertIn("data-retry-link", payload["card_html"])
        mock_row.assert_called_once()
        self.assertEqual(mock_row.call_args.args[1], "https://example.com/target")

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_does_not_write_result_records(self, mock_row):
        self.client.force_login(self.staff_user)
        BatchParseRuntimeSetting.objects.get_or_create(singleton_key=1)
        ProviderRuntimeSetting.objects.get_or_create(singleton_key=1)
        mock_row.return_value = {
            "status": "failed",
            "source_text": "https://example.com/target",
            "duration_ms": 1,
            "error_message": "解析执行失败",
        }

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                reverse("batch_parse_retry"),
                {"link": "https://example.com/target"},
            )

        write_prefixes = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")
        write_queries = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith(write_prefixes)
        ]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(write_queries, [])

    @patch("xhs_download.views.youtube_merge_mode_is_available", return_value=True)
    @patch("xhs_download.views.ultra_hd_is_available", return_value=True)
    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_reuses_server_controlled_options(
        self,
        mock_row,
        _mock_ultra_available,
        _mock_youtube_available,
    ):
        self.client.force_login(self.staff_user)
        mock_row.return_value = {
            "status": "failed",
            "source_text": "https://example.com/target",
            "duration_ms": 1,
            "error_message": "解析执行失败",
        }

        response = self.client.post(
            reverse("batch_parse_retry"),
            {
                "link": "https://example.com/target",
                "ultra_hd": "1",
                "youtube_merge": "1",
                "provider_order": "kukutool,native",
            },
        )

        self.assertEqual(response.status_code, 200)
        kwargs = mock_row.call_args.kwargs
        self.assertTrue(kwargs["prefer_ultra_hd"])
        self.assertTrue(kwargs["prefer_youtube_merge"])
        self.assertNotIn("provider_order", kwargs)

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_safely_renders_non_success_states(self, mock_row):
        self.client.force_login(self.staff_user)

        for status in ("failed", "timeout", "skipped"):
            with self.subTest(status=status):
                mock_row.reset_mock()
                mock_row.return_value = {
                    "status": status,
                    "source_text": "https://example.com/target",
                    "duration_ms": 5,
                    "error_message": "安全错误信息",
                }
                response = self.client.post(
                    reverse("batch_parse_retry"),
                    {"link": "https://example.com/target"},
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], status)
                self.assertIn("安全错误信息", response.json()["card_html"])
                mock_row.assert_called_once()

    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_retry_public_response_keeps_download_and_hides_raw_tools(self, mock_row):
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "access_mode": ProviderAccessMode.PUBLIC,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )
        signed_url = "https://v5-default.365yg.com/private.mp4?token=secret"
        mock_row.return_value = {
            "status": "success",
            "source_text": "https://example.com/target",
            "base_filename": "public_retry",
            "duration_ms": 5,
            "platform": "douyin",
            "platform_name": "抖音",
            "post_type": "video",
            "video_items": ({"direct_url": signed_url, "size_label": "1 MB"},),
            "image_items": (),
        }

        response = self.client.post(
            reverse("batch_parse_retry"),
            {"link": "https://example.com/target"},
        )

        self.assertEqual(response.status_code, 200)
        card_html = response.json()["card_html"]
        self.assertIn("data-batch-download", card_html)
        self.assertIn("data-url=", card_html)
        self.assertNotIn("data-batch-direct-url", card_html)
        self.assertNotIn("复制直链", card_html)
        self.assertNotIn("打开直链", card_html)


class TianTianJieXiHealthHardeningTests(TestCase):
    def build_request(self):
        request = RequestFactory().post("/admin/provider-health-tests/run/")
        request.user = get_user_model().objects.create_superuser(
            username="admin",
            password="password",
        )
        request.request_id = "rid-health"
        return request

    def test_account_health_test_does_not_parse_video(self):
        request = self.build_request()
        item = provider_health_test_by_key("tiantianjiexi_account_status")

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone",
            TIANTIANJIEXI_PASSWORD="password",
        ):
            with patch.object(
                TianTianJieXiGenericProvider,
                "query_account_status",
                return_value={"available": True, "cached": True},
            ) as mock_status, patch.object(
                TianTianJieXiGenericProvider,
                "parse",
            ) as mock_parse:
                run = run_provider_health_test(request, item)

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)
        mock_status.assert_called_once()
        mock_parse.assert_not_called()

    def test_health_query_failure_does_not_raise_or_leak_credentials(self):
        class FailingClient:
            def query_account_status(self):
                raise RuntimeError("https://example.com/?token=secret")

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone-secret",
            TIANTIANJIEXI_PASSWORD="password-secret",
            TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=True,
        ):
            health = TianTianJieXiGenericProvider(client=FailingClient()).health()

        self.assertFalse(health["account_status_available"])
        self.assertNotIn("phone-secret", str(health))
        self.assertNotIn("password-secret", str(health))

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    )
    def test_health_query_can_be_disabled(self):
        class ExplodingClient:
            def query_account_status(self):
                raise AssertionError("should not query account")

        health = TianTianJieXiGenericProvider(client=ExplodingClient()).health()

        self.assertFalse(health["account_status_available"])

    def test_tiantianjiexi_generic_signed_provider_manifest_allowed(self):
        item = {
            "asset_id": "third_party:kuaishou:abc123:video:0",
            "provider": "tiantianjiexi_generic",
            "provider_resource_id": "abc123",
            "provider_cache_key": "third_party:tiantianjiexi_generic:abc123",
            "type": "video",
            "url": "https://tx2.a.kwimgs.com/demo.mp4",
        }
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"],
            provider=item["provider"],
            provider_resource_id=item["provider_resource_id"],
            provider_cache_key=item["provider_cache_key"],
            url=item["url"],
            media_type=item["type"],
        )

        self.assertTrue(provider_manifest_allows_public_download(item))

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.DouyinTianTianJieXiProvider.parse")
    def test_douyin_tiantianjiexi_health_validates_final_asset_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            douyin_url="https://www.douyin.com/user/self?modal_id=7611146072161047526",
        )
        asset = MediaAsset(
            media_type="video",
            url="https://v5-default.365yg.com/demo.mp4",
            platform="douyin",
            provider="tiantianjiexi",
            asset_id="douyin:7611146072161047526:video:tiantianjiexi",
            provider_resource_id="7611146072161047526",
            provider_cache_key="douyin:tiantianjiexi:validated:7611146072161047526",
        )
        asset = MediaAsset(
            **{
                **asset.__dict__,
                "provider_manifest_token": build_provider_manifest_token(
                    asset_id=asset.asset_id,
                    provider=asset.provider,
                    provider_resource_id=asset.provider_resource_id,
                    provider_cache_key=asset.provider_cache_key,
                    url=asset.url,
                    media_type=asset.media_type,
                ),
            }
        )
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(asset,),
            metadata={"selected_provider": "tiantianjiexi", "source_provider": "tiantianjiexi"},
        )

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("douyin_tiantianjiexi_ultra"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.DouyinTianTianJieXiProvider.parse")
    def test_douyin_tiantianjiexi_health_rejects_missing_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            douyin_url="https://www.douyin.com/user/self?modal_id=7611146072161047526",
        )
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi"},
        )

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("douyin_tiantianjiexi_ultra"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.UNAVAILABLE)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_third_party_tiantianjiexi_health_rejects_invalid_normalized_url(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            third_party_url="https://www.kuaishou.com/short-video/demo",
        )
        mock_parse.return_value = {
            "title": "bad",
            "videos": [{"url": "https://evil.example.com/demo.mp4", "quality": "原画"}],
        }

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("third_party_tiantianjiexi"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.FAILED)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_third_party_tiantianjiexi_health_validates_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            third_party_url="https://www.kuaishou.com/short-video/demo",
        )
        mock_parse.return_value = {
            "title": "ok",
            "videos": [{"url": "https://tx2.a.kwimgs.com/demo.mp4", "quality": "原画"}],
        }

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("third_party_tiantianjiexi"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)

    @patch("xhs_download.views.probe_media_size")
    def test_signed_tiantianjiexi_generic_video_size_does_not_probe(self, mock_probe):
        size = get_video_size_bytes(
            MediaAsset(
                media_type="video",
                platform="third_party",
                url="https://tx2.a.kwimgs.com/demo.mp4",
                provider="tiantianjiexi_generic",
            )
        )

        self.assertEqual(size, 0)
        mock_probe.assert_not_called()


class DouyinUltraRaceSettingsTests(SimpleTestCase):
    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_race_enabled_with_both_upstreams_uses_ultra_race(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "kukutool", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertIsInstance(providers[0], DouyinUltraRaceProvider)
        self.assertIsInstance(providers[-1], DouyinNativeProvider)
        race_provider = providers[0]
        tiantianjiexi_provider = next(
            provider for provider in race_provider.providers if provider.name == "tiantianjiexi"
        )
        kukutool_provider = next(
            provider for provider in race_provider.providers if provider.name == "kukutool"
        )
        self.assertFalse(tiantianjiexi_provider.reserve_fallback_grace)
        self.assertTrue(tiantianjiexi_provider.suppress_selected_log)
        self.assertTrue(kukutool_provider.suppress_selected_log)

    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=False,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_race_disabled_respects_provider_order(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "tiantianjiexi", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinTianTianJieXiProvider)
        self.assertTrue(providers[1].reserve_fallback_grace)
        self.assertFalse(providers[1].suppress_selected_log)
        self.assertIsInstance(providers[-1], DouyinNativeProvider)

    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_provider_order_override_can_exclude_kukutool_from_batch_path(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "kukutool", "native"),
                kukutool_enabled=True,
            ),
            provider_order=("tiantianjiexi", "native"),
        )

        self.assertIsInstance(providers[0], DouyinTianTianJieXiProvider)
        self.assertFalse(providers[0].reserve_fallback_grace)
        self.assertIsInstance(providers[-1], DouyinNativeProvider)
        self.assertFalse(any(isinstance(provider, DouyinUltraRaceProvider) for provider in providers))
        self.assertFalse(any(isinstance(provider, DouyinKukuToolProvider) for provider in providers))

    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=False,
        TIANTIANJIEXI_PHONE="",
        TIANTIANJIEXI_PASSWORD="",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_unconfigured_tiantianjiexi_is_not_created(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "kukutool", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertFalse(any(isinstance(provider, DouyinTianTianJieXiProvider) for provider in providers))
        self.assertTrue(any(isinstance(provider, DouyinKukuToolProvider) for provider in providers))
        self.assertIsInstance(providers[-1], DouyinNativeProvider)


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="phone",
    TIANTIANJIEXI_PASSWORD="password",
)
class DouyinUltraRaceBudgetTests(SimpleTestCase):
    class Adapter:
        platform_id = "douyin"
        headers = {}
        media_host_suffixes = ("365yg.com", "douyinvod.com")

    class CapturingOfficialProvider:
        def __init__(self):
            self.timeout_seconds = None

        def parse(self, _url, timeout_seconds=None):
            self.timeout_seconds = timeout_seconds
            return {
                "title": "天天解析预算测试",
                "videos": [
                    {
                        "url": "https://v11-default.365yg.com/video/tos/demo.mp4",
                        "quality": "原画",
                        "size": 80 * 1024 * 1024,
                    }
                ],
            }

    class FakeRaceProvider:
        def __init__(
            self,
            name,
            *,
            delay_seconds,
            size_bytes=80 * 1024 * 1024,
            quality_label="原画",
            raises=None,
        ):
            self.name = name
            self.delay_seconds = delay_seconds
            self.size_bytes = size_bytes
            self.quality_label = quality_label
            self.raises = raises

        def parse(self, _adapter, _context):
            sleep(self.delay_seconds)
            if self.raises:
                raise self.raises
            return ParseResult(
                platform="douyin",
                post_type="video",
                base_filename=f"{self.name}_demo",
                assets=(
                    MediaAsset(
                        media_type="video",
                        url=f"https://v11-default.365yg.com/video/{self.name}.mp4",
                        platform="douyin",
                        provider=self.name,
                        quality_label=self.quality_label,
                        size_bytes=self.size_bytes,
                    ),
                ),
                metadata={
                    "selected_provider": self.name,
                    "source_provider": self.name,
                    f"{self.name}_candidate_count": 1,
                },
            )

    def provider_settings(self):
        return ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("tiantianjiexi", "kukutool", "native"),
            kukutool_enabled=True,
            ultra_total_timeout_seconds=5.0,
            ultra_kukutool_grace_seconds=1.0,
            kukutool_sidecar_timeout_seconds=3.0,
        )

    def context(self, budget_seconds=5.0):
        return DouyinParseContext(
            url="https://www.douyin.com/video/7611146072161047526?token=secret",
            final_url="https://www.douyin.com/video/7611146072161047526?token=secret",
            aweme_id="7611146072161047526",
            deadline_monotonic=monotonic() + budget_seconds,
        )

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        THIRD_PARTY_PROVIDER_ENABLED=True,
    )
    def test_race_tiantianjiexi_does_not_reserve_kukutool_grace(self):
        official_provider = self.CapturingOfficialProvider()
        provider = DouyinTianTianJieXiProvider(
            provider_settings=self.provider_settings(),
            provider=official_provider,
            reserve_fallback_grace=False,
        )

        provider.parse(self.Adapter(), self.context())

        self.assertGreaterEqual(official_provider.timeout_seconds, 4.5)

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        THIRD_PARTY_PROVIDER_ENABLED=True,
    )
    def test_sequential_tiantianjiexi_reserves_kukutool_grace(self):
        official_provider = self.CapturingOfficialProvider()
        provider = DouyinTianTianJieXiProvider(
            provider_settings=self.provider_settings(),
            provider=official_provider,
            reserve_fallback_grace=True,
        )

        provider.parse(self.Adapter(), self.context())

        self.assertLessEqual(official_provider.timeout_seconds, 2.2)

    def test_race_tiantianjiexi_wins_when_it_returns_first(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.04),
                self.FakeRaceProvider("kukutool", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "tiantianjiexi")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(result.metadata["source_provider"], "tiantianjiexi")

    def test_race_kukutool_wins_when_it_returns_strong_candidate_first(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("kukutool", delay_seconds=0.04),
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "kukutool")
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(result.metadata["source_provider"], "kukutool")

    def test_race_falls_back_to_kukutool_when_tiantianjiexi_times_out(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider(
                    "tiantianjiexi",
                    delay_seconds=0.02,
                    raises=XhsProviderTimeoutError(
                        "timeout https://example.com/video.mp4?token=secret&phone=secret"
                    ),
                ),
                self.FakeRaceProvider("kukutool", delay_seconds=0.04),
            ),
        )

        with self.assertLogs(
            "xhs_download.services.platforms.douyin_providers",
            level="INFO",
        ) as logs:
            result = provider.parse(self.Adapter(), self.context())

        output = "\n".join(logs.output)
        self.assertEqual(result.metadata["ultra_race_winner"], "kukutool")
        self.assertNotIn("https://example.com/video.mp4", output)
        self.assertNotIn("token=secret", output)
        self.assertNotIn("phone=secret", output)

    def test_race_loser_does_not_override_winner_metadata(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.04),
                self.FakeRaceProvider("kukutool", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "tiantianjiexi")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(result.video_assets[0].provider, "tiantianjiexi")


class TianTianJieXiSmokeCommandTests(TestCase):
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_smoke_api_mode_hides_full_url_and_credentials(self, mock_parse):
        mock_parse.return_value = {
            "videos": [{"url": "https://tx2.a.kwimgs.com/path/video.mp4?token=secret", "quality": "原画"}],
            "images": [],
        }
        out = StringIO()

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone-secret",
            TIANTIANJIEXI_PASSWORD="password-secret",
        ):
            call_command(
                "smoke_tiantianjiexi_provider",
                "--url",
                "https://www.kuaishou.com/short-video/demo",
                "--mode",
                "api",
                stdout=out,
            )

        output = out.getvalue()
        self.assertIn('"selected_video_host": "tx2.a.kwimgs.com"', output)
        self.assertNotIn("phone-secret", output)
        self.assertNotIn("password-secret", output)
        self.assertNotIn("path/video.mp4", output)
        self.assertNotIn("token=secret", output)

    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_smoke_parse_result_mode_generates_parse_result(self, mock_parse):
        mock_parse.return_value = {
            "title": "ok",
            "videos": [{"url": "https://tx2.a.kwimgs.com/demo.mp4", "quality": "原画"}],
        }
        out = StringIO()

        call_command(
            "smoke_tiantianjiexi_provider",
            "--url",
            "https://www.kuaishou.com/short-video/demo",
            "--mode",
            "parse-result",
            stdout=out,
        )

        output = out.getvalue()
        self.assertIn('"post_type": "video"', output)
        self.assertIn('"first_asset_provider": "tiantianjiexi_generic"', output)
        self.assertIn('"provider_manifest_token_present": true', output)

    @patch("xhs_download.management.commands.smoke_tiantianjiexi_provider.parse_media_page")
    def test_smoke_douyin_ultra_mode_outputs_selected_provider(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                    quality_label="原画",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi", "ultra_race_winner": "tiantianjiexi"},
        )
        out = StringIO()

        call_command(
            "smoke_tiantianjiexi_provider",
            "--url",
            "https://www.douyin.com/user/self?modal_id=7611146072161047526",
            "--mode",
            "douyin-ultra",
            stdout=out,
        )

        output = out.getvalue()
        self.assertIn('"selected_provider": "tiantianjiexi"', output)
        self.assertIn('"ultra_race_winner": "tiantianjiexi"', output)
