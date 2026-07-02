from io import BytesIO, StringIO
from datetime import datetime, timezone as datetime_timezone
import json
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import parse_qs
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.checks import Error as CheckError, Warning as CheckWarning, run_checks
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from download_console.models import DownloadRuntimeSetting

from .services.common import (
    MEDIA_HOST_SUFFIXES,
    XhsDownloadError,
    XhsParseError,
    XhsProviderTimeoutError,
    XhsSecurityError,
    clean_filename,
    encode_url,
    request_get_with_retry,
    validate_media_url,
    validate_page_url,
    validate_public_download_url,
)
from .services.downloads import (
    MAX_IMAGE_BYTES,
    build_download_archive,
    build_download_archive_file,
    fetch_media_response,
    fetch_item_to_temp_path,
    fetch_to_temp_path,
    get_media_headers,
    normalize_quality,
    normalize_save_format,
    normalize_video_save_mode,
    provider_manifest_allows_public_download,
    public_download_error_message,
    request_public_provider_get_with_retry,
    resolve_server_save_folder,
    save_media_items_to_server,
)
from .services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    decide_download_route,
)
from .services.routing_policy_adjuster import build_routing_policy_recommendations
from .services.routing_v3 import compute_routing_score, decide_route_v3
from .services.assets import MediaAsset, ParseResult
from .services.candidates import MediaCandidate, dedupe_candidates, rank_media_candidates
from .services.diagnostics import CandidateDiagnostic, select_best_media_candidate
from .services.provider_settings import (
    ProviderSettings,
    get_platform_provider_settings,
    normalize_provider_order,
)
from .services.provider_manifest import build_provider_manifest_token
from .services.feedback_loop import (
    build_feedback,
    build_feedback_loop_context,
    build_insights,
    build_suggestions,
)
from .services.approval_gate import (
    CONTROL_AUTO_APPLY,
    CONTROL_REJECT,
    CONTROL_REQUIRE_APPROVAL,
    approve_policy_change,
    reject_policy_change,
    rollback_policy_change,
)
from .services.observability import record_download_request_log, record_provider_admin_audit_log
from .services.policy_control_layer import (
    create_controlled_policy_proposals,
    evaluate_policy_control_layer,
)
from .services.policy_risk_engine import score_policy_risk
from .services.control_convergence_engine import (
    apply_control_feedback_to_records,
    build_control_convergence_snapshot,
    find_orphan_control_decisions,
)
from .services.control_execution_tracker import record_control_execution
from .services.control_state_machine import (
    create_control_record,
    record_control_decision_for_proposal,
    transition_control_record,
)
from .services.production_intelligence import build_observability_v2_context
from .policy.healing_policy import (
    HEAL_ACTION_BLOCK_GRACEFULLY,
    HEAL_ACTION_SWITCH_PROVIDER,
    next_provider,
)
from .services.recovery_engine import (
    FAILURE_PROVIDER_TIMEOUT,
    FAILURE_PROXY_FAILED,
    FAILURE_ROUTING_FAILED,
    provider_stability_score,
    provider_stability_status,
)
from .services.self_healing_engine import SelfHealingEngine
from .policy.adaptive import build_adaptive_weight_recommendation
from .policy.engine import (
    apply_policy_decision_to_route,
    build_policy_context,
    decide_final_policy,
    evaluate_policy_shadow,
    policy_weight_diagnostics,
)
from .policy.evolution import (
    SAFETY_APPROVE,
    SAFETY_REJECT,
    SAFETY_REVIEW,
    build_policy_evolution_recommendation,
    validate_policy_suggestion,
)
from .policy.learning import build_policy_learning_snapshot, record_policy_learning_run
from .policy.optimization import (
    build_policy_optimization_queue,
    create_policy_optimization_proposal,
    evaluate_policy_drift,
)
from .policy.policies import CostPolicy, ProviderPolicy, RoutingPolicy
from .services.provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    request_rollout_bucket,
    third_party_public_access_allowed,
    third_party_public_access_summary,
)
from .models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    PolicyControlRecord,
    PolicyControlState,
    PolicyLearningRun,
    PolicyOptimizationProposal,
    PolicyOptimizationStatus,
    PolicyRuntimeMode,
    PolicyRuntimeSetting,
    ProviderAccessMode,
    ProviderAdminAuditLog,
    ProviderHealthTestSetting,
    ProviderHealthTestRun,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
    RoutingDecisionLog,
    SelfHealingEvent,
)
from .views import (
    build_provider_preview_query,
    build_search_results_context,
    limit_error_response,
    log_parse_request_event,
    third_party_provider_is_available,
    ultra_hd_is_available,
    youtube_merge_mode_is_available,
)
from .services.quality import QualityProbe, QualityProbeResult
from .services.rate_limits import (
    LimitResult,
    get_client_identity,
    get_client_ip,
    get_daily_total,
    release_concurrency,
)
from .services.region_routing import (
    get_request_country_code,
    normalize_country_code,
)
from .services.platforms import (
    BasePlatformAdapter,
    BilibiliAdapter,
    DouyinAdapter,
    GalleryDlGenericProvider,
    GenericThirdPartyAdapter,
    XiaohongshuAdapter,
    YtDlpGenericProvider,
)
from .services.platforms.external_tools import (
    effective_external_timeout_seconds,
    external_command_health,
    external_failure_cache_key,
    external_payload_cache_key,
    probe_external_command_version,
    run_external_command,
    safe_subprocess_env,
)
from .services.platforms.third_party import third_party_media_url_is_allowed
from .services.platforms.bilibili import BILIBILI_HEADERS
from .services.platforms.douyin import DOUYIN_HEADERS, extract_aweme_id, get_video_candidates
from .services.platforms.douyin_kukutool import (
    DouyinKukuToolProvider,
    build_kukutool_cache_key,
)
from .services.platforms.kukutool_generic import (
    build_failure_cache_key as build_generic_kukutool_failure_cache_key,
    build_payload_cache_key as build_generic_kukutool_payload_cache_key,
)
from .services.provider_recovery import iter_kukutool_download_recovery_urls
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinNativeEnhancedProvider,
    DouyinNativeProvider,
    DouyinTikHubProvider,
    ValidatedProviderCandidate,
    build_tikhub_validated_cache_key,
    build_tikhub_provider_response,
    build_douyin_result_providers,
    context_log_fields,
)
from .services.parser import normalize_xhs_image_url, normalize_xhs_preview_image_url, parse_xhs_html
from .services.registry import (
    PlatformRegistry,
    extract_url_from_text,
    get_adapter_for_platform,
    get_adapter_for_url,
    list_platforms,
    parse_media_page,
)


def make_image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (10, 8), (255, 0, 0)).save(buffer, "JPEG")
    return buffer.getvalue()


def make_kukutool_download_item(url, aweme_id="7647533902413173321"):
    asset_id = f"douyin:{aweme_id}:video:kukutool"
    cache_key = build_kukutool_cache_key(aweme_id)
    item = {
        "type": "video",
        "url": url,
        "asset_id": asset_id,
        "provider": "kukutool",
        "provider_resource_id": aweme_id,
        "provider_cache_key": cache_key,
    }
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=asset_id,
        provider="kukutool",
        provider_resource_id=aweme_id,
        provider_cache_key=cache_key,
        url=url,
        media_type="video",
    )
    return item


def make_third_party_download_item(
    url,
    platform="kuaishou",
    source_key="abc123",
    media_type="image",
    provider="kukutool_generic",
):
    asset_id = f"third_party:{platform}:{source_key}:{media_type}:1"
    cache_key = f"third_party:{provider}:{source_key}"
    item = {
        "type": media_type,
        "url": url,
        "asset_id": asset_id,
        "provider": provider,
        "provider_resource_id": source_key,
        "provider_cache_key": cache_key,
    }
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=asset_id,
        provider=provider,
        provider_resource_id=source_key,
        provider_cache_key=cache_key,
        url=url,
    )
    return item


class FakeStreamingResponse:
    def __init__(self, content, url="https://ci.xiaohongshu.com/a.jpg", content_type="image/jpeg"):
        self.content = content
        self.url = url
        self.headers = {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
        }
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def close(self):
        self.closed = True


class XhsUtilityTests(SimpleTestCase):
    def test_clean_filename_removes_invalid_characters(self):
        self.assertEqual(clean_filename('a/b:c*?"<>|.jpg'), "abc.jpg")

    def test_clean_filename_falls_back_when_empty(self):
        self.assertEqual(clean_filename("***"), "xhs_image")

    def test_public_download_error_message_hides_cdn_url_and_ssl_details(self):
        message = public_download_error_message(
            "HTTPSConnectionPool(host='v11-default.365yg.com', port=443): "
            "Max retries exceeded with url: /video.mp4?token=secret "
            "(Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol')))"
        )

        self.assertEqual(message, "资源下载失败：上游 CDN 连接异常，请重新解析后再试。")
        self.assertNotIn("365yg.com", message)
        self.assertNotIn("token=secret", message)

    def test_provider_stability_score_marks_unstable_provider(self):
        stable_score = provider_stability_score(
            success_rate=0.95,
            timeout_rate=0,
            failure_rate=0.05,
            average_latency_ms=500,
        )
        unstable_score = provider_stability_score(
            success_rate=0.2,
            timeout_rate=0.5,
            failure_rate=0.8,
            average_latency_ms=20000,
        )

        self.assertEqual(provider_stability_status(stable_score), "stable")
        self.assertEqual(provider_stability_status(unstable_score), "unstable")

    def test_healing_policy_provider_fallback_chain(self):
        self.assertEqual(next_provider("kukutool"), "yt_dlp_generic")
        self.assertEqual(next_provider("yt_dlp_generic"), "native")
        self.assertEqual(next_provider("native"), "fallback")

    @override_settings(KUKUTOOL_DOWNLOAD_TIMEOUT_SECONDS=7.0)
    @patch(
        "xhs_download.services.downloads.iter_kukutool_download_recovery_urls",
        return_value=["https://api-play.amemv.com/aweme/v1/play/?video_id=abc"],
    )
    @patch("xhs_download.services.downloads.fetch_to_temp_path")
    def test_kukutool_download_uses_short_timeout_and_unsigned_native_recovery(
        self,
        mock_fetch,
        _mock_recovery,
    ):
        expected = {"path": "/tmp/demo", "content_type": "video/mp4", "final_url": "native", "size": 1}
        mock_fetch.side_effect = [
            requests.exceptions.ReadTimeout("cdn timeout"),
            expected,
        ]
        item = make_kukutool_download_item("https://v11-default.365yg.com/video/demo.mp4")

        result = fetch_item_to_temp_path(item, timeout=60, expected_type="video")

        self.assertEqual(result, expected)
        first_call = mock_fetch.call_args_list[0]
        second_call = mock_fetch.call_args_list[1]
        self.assertEqual(first_call.kwargs["timeout"], 7.0)
        self.assertIsNotNone(first_call.kwargs["provider_signed"])
        self.assertEqual(second_call.args[0], "https://api-play.amemv.com/aweme/v1/play/?video_id=abc")
        self.assertEqual(second_call.kwargs["timeout"], 60)
        self.assertIsNone(second_call.kwargs["provider_signed"])

    def test_normalize_save_format_accepts_supported_formats(self):
        self.assertEqual(normalize_save_format("original"), "original")
        self.assertEqual(normalize_save_format("jpeg"), "jpg")
        self.assertEqual(normalize_save_format("png"), "png")
        self.assertEqual(normalize_save_format("webp"), "webp")

    def test_normalize_save_format_falls_back_to_original(self):
        self.assertEqual(normalize_save_format("bmp"), "original")

    def test_normalize_quality_clamps_range(self):
        self.assertEqual(normalize_quality("0"), 1)
        self.assertEqual(normalize_quality("101"), 100)
        self.assertEqual(normalize_quality("bad"), 95)

    def test_normalize_video_save_mode_falls_back_to_video(self):
        self.assertEqual(normalize_video_save_mode("both"), "both")
        self.assertEqual(normalize_video_save_mode("bad"), "video")

    def test_validate_page_url_rejects_non_xhs_hosts(self):
        with self.assertRaises(XhsSecurityError):
            validate_page_url("https://example.com/post")

    def test_validate_media_url_rejects_localhost(self):
        with self.assertRaises(XhsSecurityError):
            validate_media_url("http://127.0.0.1:8000/private")

    def test_validate_media_url_rejects_private_ip(self):
        with self.assertRaises(XhsSecurityError):
            validate_media_url("http://10.0.0.2/private")

    def test_validate_media_url_allows_douyin_redirect_hosts(self):
        validate_media_url("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")
        validate_media_url("https://v3-dy-o.zjcdn.com/video/tos/cn/path")
        validate_media_url("https://v9-default.365yg.com/video/tos/cn/path")

    def test_validate_media_url_allows_bilibili_media_hosts(self):
        validate_media_url("https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4")
        validate_media_url("https://i0.hdslb.com/bfs/archive/cover.jpg")

    def test_get_media_headers_uses_youtube_referer_for_ytimg(self):
        headers = get_media_headers("https://i.ytimg.com/vi/demo/maxresdefault.jpg")

        self.assertEqual(headers["Referer"], "https://www.youtube.com/")
        self.assertEqual(headers["Origin"], "https://www.youtube.com")

    @patch("xhs_download.services.downloads.request_public_provider_get_with_retry")
    def test_provider_signed_fetch_uses_youtube_headers_by_default(self, mock_get):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        item = {
            "type": "image",
            "url": media_url,
            "asset_id": "third_party:youtube:abc123:video:1:cover",
            "provider": "yt_dlp_generic",
            "provider_resource_id": "abc123",
            "provider_cache_key": "third_party:yt_dlp_generic:abc123",
        }
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"],
            provider=item["provider"],
            provider_resource_id=item["provider_resource_id"],
            provider_cache_key=item["provider_cache_key"],
            url=media_url,
            media_type="image",
        )

        fetch_media_response(media_url, provider_signed=item)

        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["Referer"], "https://www.youtube.com/")
        self.assertEqual(headers["Origin"], "https://www.youtube.com")

    def test_third_party_media_hosts_are_allowed_only_for_signed_provider_paths(self):
        allowed_cases = (
            ("youtube", "https://rr1---sn.googlevideo.com/videoplayback?itag=22"),
            ("youtube", "https://i.ytimg.com/vi/demo/maxresdefault.jpg"),
            ("x", "https://video.twimg.com/ext_tw_video/demo/pu/vid/720x720/video.mp4"),
            ("imgur", "https://i.imgur.com/demo.jpg"),
            ("reddit", "https://preview.redd.it/demo.jpg"),
            ("flickr", "https://live.staticflickr.com/65535/demo_o.jpg"),
            ("deviantart", "https://images-wixmp.example.wixmp.com/f/demo.jpg"),
        )

        for platform_id, media_url in allowed_cases:
            with self.subTest(platform_id=platform_id, media_url=media_url):
                self.assertTrue(third_party_media_url_is_allowed(platform_id, media_url))
                with self.assertRaises(XhsSecurityError):
                    validate_media_url(media_url)

    def test_third_party_media_host_allowlist_is_platform_scoped(self):
        self.assertFalse(
            third_party_media_url_is_allowed(
                "youtube",
                "https://i.imgur.com/demo.jpg",
            )
        )
        self.assertFalse(
            third_party_media_url_is_allowed(
                "imgur",
                "https://rr1---sn.googlevideo.com/videoplayback?itag=22",
            )
        )
        self.assertFalse(
            third_party_media_url_is_allowed(
                "reddit",
                "https://evil.example.com/demo.jpg",
            )
        )

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("10.0.0.5", 443))],
    )
    def test_validate_public_download_url_rejects_private_dns_result(self, _mock_dns):
        with self.assertRaises(XhsSecurityError):
            validate_public_download_url("https://cdn.example.com/a.jpg")

    def test_validate_public_download_url_rejects_unexpected_port(self):
        with self.assertRaises(XhsSecurityError):
            validate_public_download_url("https://cdn.example.com:8080/a.jpg")

    def test_validate_public_download_url_rejects_invalid_port(self):
        with self.assertRaises(XhsSecurityError):
            validate_public_download_url("https://cdn.example.com:notaport/a.jpg")

    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_external_command_version_probe_uses_short_cache(self, mock_run):
        cache.clear()

        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"tool 1.0\n")
            return Mock(returncode=0)

        mock_run.side_effect = fake_run
        command = ("python", "-m", "demo_tool")

        first = probe_external_command_version(command, cache_seconds=60)
        second = probe_external_command_version(command, cache_seconds=60)

        self.assertTrue(first["version_probe_ok"])
        self.assertEqual(second, first)
        mock_run.assert_called_once()

    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_external_command_version_probe_accepts_custom_version_args(self, mock_run):
        cache.clear()

        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"ffmpeg version 8.1\n")
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        result = probe_external_command_version(
            ("ffmpeg",),
            version_args=("-version",),
            cache_seconds=0,
        )

        self.assertTrue(result["version_probe_ok"])
        self.assertEqual(
            mock_run.call_args.args[0],
            ["ffmpeg", "-version"],
        )

    @override_settings(THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8)
    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_external_command_version_probe_rejects_oversized_output(self, mock_run):
        cache.clear()

        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"x" * 2048)
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        result = probe_external_command_version(
            ("python", "-m", "demo_tool"),
            cache_seconds=0,
        )

        self.assertFalse(result["version_probe_ok"])
        self.assertIn("output exceeded", result["error"])

    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.subprocess.run")
    def test_external_command_runs_in_isolated_workdir(
        self,
        mock_run,
        _mock_exists,
    ):
        def fake_run(_args, **kwargs):
            kwargs["stdout"].write(b"ok")
            return Mock(returncode=0)

        mock_run.side_effect = fake_run

        result = run_external_command(
            [".venv/bin/python", "-m", "demo_tool"],
            timeout_seconds=1,
        )

        args = mock_run.call_args.args[0]
        kwargs = mock_run.call_args.kwargs
        env = kwargs["env"]
        self.assertEqual(result.stdout, "ok")
        self.assertTrue(os.path.isabs(args[0]))
        self.assertEqual(kwargs["cwd"], env["HOME"])
        self.assertIn("xhs_external_tool_", env["HOME"])
        self.assertNotEqual(env["HOME"], safe_subprocess_env()["HOME"])
        self.assertNotIn("DJANGO_SECRET_KEY", env)
        self.assertEqual(env["YTDLP_NO_PLUGINS"], "1")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")

    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    def test_external_command_health_resolves_relative_path_to_absolute(
        self,
        _mock_access,
    ):
        with TemporaryDirectory() as temp_dir:
            binary_path = Path(temp_dir) / "bin" / "demo-tool"
            binary_path.parent.mkdir()
            binary_path.write_text("#!/bin/sh\n", encoding="utf-8")
            relative_binary = os.path.relpath(binary_path, Path.cwd())

            health = external_command_health(
                f"{relative_binary} --json",
                "demo-tool",
            )

        self.assertTrue(health["binary_configured"])
        self.assertEqual(health["executable"], relative_binary)
        self.assertEqual(health["resolved_executable"], str(binary_path))
        self.assertTrue(health["executable_found"])

    def test_safe_subprocess_env_does_not_inherit_application_secrets(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/local/bin:/usr/bin",
                "HOME": "/Users/tester",
                "LANG": "zh_CN.UTF-8",
                "DJANGO_SECRET_KEY": "django-secret",
                "TIKHUB_API_KEY": "tikhub-secret",
                "DATABASE_URL": "postgres://secret",
                "DJANGO_CACHE_URL": "redis://secret",
            },
            clear=False,
        ):
            env = safe_subprocess_env()

        self.assertEqual(
            env,
            {
                "PATH": "/usr/local/bin:/usr/bin",
                "HOME": "/Users/tester",
                "LANG": "zh_CN.UTF-8",
                "PYTHONNOUSERSITE": "1",
                "YTDLP_NO_PLUGINS": "1",
            },
        )
        self.assertNotIn("DJANGO_SECRET_KEY", env)
        self.assertNotIn("TIKHUB_API_KEY", env)
        self.assertNotIn("DATABASE_URL", env)
        self.assertNotIn("DJANGO_CACHE_URL", env)

    @override_settings(THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0)
    def test_effective_external_timeout_caps_provider_timeout(self):
        self.assertEqual(effective_external_timeout_seconds(15.0), 3.0)
        self.assertEqual(effective_external_timeout_seconds(2.0), 2.0)

    @override_settings(THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=0)
    def test_effective_external_timeout_uses_default_for_zero_frontend_budget(self):
        self.assertEqual(effective_external_timeout_seconds(15.0), 3.0)

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_third_party_download_allows_same_platform_redirect(self, mock_get, _mock_dns):
        initial_url = "https://p1.a.yximgs.com/cover.jpg"
        final_url = "https://tx2.a.yximgs.com/cover.jpg"
        provider_item = make_third_party_download_item(initial_url)
        redirect_response = Mock(
            status_code=302,
            headers={"Location": final_url},
        )
        redirect_response.close = Mock()
        final_response = Mock(
            status_code=200,
            headers={},
            url=final_url,
        )
        mock_get.side_effect = [redirect_response, final_response]

        response = request_public_provider_get_with_retry(
            initial_url,
            retries=1,
            provider_item=provider_item,
        )

        self.assertIs(response, final_response)
        self.assertEqual(mock_get.call_count, 2)
        redirect_response.close.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_third_party_download_rejects_cross_platform_redirect(self, mock_get, _mock_dns):
        initial_url = "https://p1.a.yximgs.com/cover.jpg"
        provider_item = make_third_party_download_item(initial_url)
        redirect_response = Mock(
            status_code=302,
            headers={"Location": "https://cdn.example.com/cover.jpg"},
        )
        redirect_response.close = Mock()
        mock_get.return_value = redirect_response

        with self.assertRaises(XhsDownloadError):
            request_public_provider_get_with_retry(
                initial_url,
                retries=1,
                provider_item=provider_item,
            )

        self.assertEqual(mock_get.call_count, 1)
        redirect_response.close.assert_called_once()

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_douyin_kukutool_download_allows_byte_cdn_url(self, mock_get, _mock_dns):
        initial_url = "https://v6-default.365yg.com/path/video/tos/cn/source.mp4"
        provider_item = make_kukutool_download_item(initial_url)
        response = Mock(status_code=200, headers={}, url=initial_url)
        mock_get.return_value = response

        result = request_public_provider_get_with_retry(
            initial_url,
            retries=1,
            provider_item=provider_item,
        )

        self.assertIs(result, response)

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_douyin_kukutool_download_rejects_non_byte_cdn_redirect(
        self,
        mock_get,
        _mock_dns,
    ):
        initial_url = "https://v6-default.365yg.com/path/video/tos/cn/source.mp4"
        provider_item = make_kukutool_download_item(initial_url)
        redirect_response = Mock(
            status_code=302,
            headers={"Location": "https://example.com/source.mp4"},
        )
        redirect_response.close = Mock()
        mock_get.return_value = redirect_response

        with self.assertRaisesRegex(XhsDownloadError, "Provider URL"):
            request_public_provider_get_with_retry(
                initial_url,
                retries=1,
                provider_item=provider_item,
            )

        redirect_response.close.assert_called_once()

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("198.18.0.100", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_youtube_cover_allows_provider_host_when_dns_is_reserved(
        self,
        mock_get,
        _mock_dns,
    ):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        provider_item = {
            "type": "image",
            "url": media_url,
            "asset_id": "yt_dlp_merge:youtube:abc123:video:1:h1080:cover",
            "provider": "yt_dlp_merge",
            "provider_resource_id": "abc123",
            "provider_cache_key": "yt_dlp_merge:abc123:h1080",
        }
        provider_item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=provider_item["asset_id"],
            provider=provider_item["provider"],
            provider_resource_id=provider_item["provider_resource_id"],
            provider_cache_key=provider_item["provider_cache_key"],
            url=media_url,
            media_type="image",
        )
        response = Mock(status_code=200, headers={}, url=media_url)
        mock_get.return_value = response

        result = request_public_provider_get_with_retry(
            media_url,
            retries=1,
            provider_item=provider_item,
        )

        self.assertIs(result, response)

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_youtube_cover_falls_back_when_maxres_is_missing(
        self,
        mock_get,
        _mock_dns,
    ):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        provider_item = {
            "type": "image",
            "url": media_url,
            "asset_id": "yt_dlp_merge:youtube:abc123:video:1:h1080:cover",
            "provider": "yt_dlp_merge",
            "provider_resource_id": "abc123",
            "provider_cache_key": "yt_dlp_merge:abc123:h1080",
        }
        provider_item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=provider_item["asset_id"],
            provider=provider_item["provider"],
            provider_resource_id=provider_item["provider_resource_id"],
            provider_cache_key=provider_item["provider_cache_key"],
            url=media_url,
            media_type="image",
        )
        missing_response = Mock(
            status_code=404,
            headers={},
            url=media_url,
        )
        missing_response.close = Mock()
        fallback_url = "https://i.ytimg.com/vi/demo/sddefault.jpg"
        fallback_response = Mock(status_code=200, headers={}, url=fallback_url)
        mock_get.side_effect = [missing_response, fallback_response]

        result = request_public_provider_get_with_retry(
            media_url,
            retries=1,
            provider_item=provider_item,
        )

        self.assertIs(result, fallback_response)
        self.assertEqual(mock_get.call_args_list[1].args[0], fallback_url)
        missing_response.close.assert_called_once()

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_external_provider_download_invalidates_payload_cache_on_http_error(
        self,
        mock_get,
        _mock_dns,
    ):
        source_key = "imgur-source"
        media_url = "https://i.imgur.com/demo.jpg"
        provider_item = make_third_party_download_item(
            media_url,
            platform="imgur",
            source_key=source_key,
            media_type="image",
            provider="gallery_dl_generic",
        )
        cache_key = external_payload_cache_key("gallery_dl_generic", source_key)
        cache.set(cache_key, {"title": "stale gallery payload"}, timeout=60)
        failed_response = Mock(
            status_code=403,
            headers={},
            url=media_url,
        )
        failed_response.close = Mock()
        mock_get.return_value = failed_response

        with self.assertRaises(XhsDownloadError):
            fetch_item_to_temp_path(
                provider_item,
                timeout=1,
                expected_type="image",
            )

        self.assertIsNone(cache.get(cache_key))
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(failed_response.close.call_count, 3)

    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_kukutool_generic_download_invalidates_payload_cache_on_http_error(
        self,
        mock_get,
        _mock_dns,
    ):
        source_key = "tiktok-source"
        media_url = "https://v16.tiktokcdn.com/video/demo.mp4"
        provider_item = make_third_party_download_item(
            media_url,
            platform="tiktok",
            source_key=source_key,
            media_type="video",
            provider="kukutool_generic",
        )
        payload_cache_key = build_generic_kukutool_payload_cache_key(source_key)
        failure_cache_key = build_generic_kukutool_failure_cache_key(source_key)
        cache.set(payload_cache_key, {"title": "stale kukutool payload"}, timeout=60)
        cache.set(failure_cache_key, "TimeoutError", timeout=60)
        failed_response = Mock(
            status_code=403,
            headers={},
            url=media_url,
        )
        failed_response.close = Mock()
        mock_get.return_value = failed_response

        with self.assertRaises(XhsDownloadError):
            fetch_item_to_temp_path(
                provider_item,
                timeout=1,
                expected_type="video",
            )

        self.assertIsNone(cache.get(payload_cache_key))
        self.assertIsNone(cache.get(failure_cache_key))
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(failed_response.close.call_count, 3)

    def test_dedupe_candidates_preserves_first_occurrence(self):
        candidates = dedupe_candidates([
            MediaCandidate(url="https://example.com/a.mp4", size_bytes=100),
            MediaCandidate(url="https://example.com/a.mp4", size_bytes=200),
            MediaCandidate(url="https://example.com/b.mp4", size_bytes=50),
        ])

        self.assertEqual([item.url for item in candidates], ["https://example.com/a.mp4", "https://example.com/b.mp4"])
        self.assertEqual(candidates[0].size_bytes, 100)

    def test_rank_media_candidates_prefers_larger_verified_sources(self):
        candidates = rank_media_candidates([
            MediaCandidate(url="https://example.com/low.mp4", size_bytes=1_000, width=1920, height=1080),
            MediaCandidate(url="https://example.com/high.mp4", size_bytes=20_000, width=1280, height=720),
        ])

        self.assertEqual(candidates[0].url, "https://example.com/high.mp4")

    def test_normalize_provider_order_deduplicates_names(self):
        self.assertEqual(
            normalize_provider_order("native,tikhub,native,"),
            ("native", "tikhub"),
        )

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        TIKHUB_API_KEY="secret",
    )
    def test_provider_settings_use_fixed_douyin_order_with_ultra_disabled(self):
        provider_settings = get_platform_provider_settings("douyin")

        self.assertEqual(provider_settings.provider_order, ("kukutool", "native"))
        self.assertEqual(provider_settings.enabled_provider_order, ("native",))

    def test_provider_settings_enable_tikhub_when_configured(self):
        provider_settings = ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native", "tikhub"),
            tikhub_api_key="secret",
        )

        self.assertEqual(provider_settings.enabled_provider_order, ("native", "tikhub"))

    def test_provider_settings_gate_native_enhanced_provider(self):
        provider_settings = ProviderSettings(
            ultra_hd_enabled=False,
            provider_order=("native_enhanced", "native"),
        )

        self.assertEqual(provider_settings.enabled_provider_order, ("native",))

    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_uses_head_content_length(self, mock_head):
        response = Mock()
        response.status_code = 200
        response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        response.headers = {"Content-Length": "1234", "Content-Type": "video/mp4"}
        response.close = Mock()
        mock_head.return_value = response

        result = QualityProbe().probe("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")

        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 1234)
        self.assertEqual(result.method, "HEAD")
        response.close.assert_called_once()

    @patch("xhs_download.services.quality.requests.get")
    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_falls_back_to_range_when_head_has_no_size(self, mock_head, mock_get):
        head_response = Mock()
        head_response.status_code = 200
        head_response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        head_response.headers = {"Content-Type": "video/mp4"}
        head_response.close = Mock()
        mock_head.return_value = head_response

        range_response = Mock()
        range_response.status_code = 206
        range_response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        range_response.headers = {"Content-Range": "bytes 0-0/5678", "Content-Type": "video/mp4"}
        range_response.close = Mock()
        mock_get.return_value = range_response

        result = QualityProbe().probe("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")

        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 5678)
        self.assertEqual(result.method, "RANGE")
        head_response.close.assert_called_once()
        range_response.close.assert_called_once()

    @patch("xhs_download.services.quality.requests.get")
    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_fast_mode_uses_only_range_request(self, mock_head, mock_get):
        range_response = Mock()
        range_response.status_code = 206
        range_response.url = "https://v11-default.365yg.com/video.mp4"
        range_response.headers = {
            "Content-Length": "1",
            "Content-Range": "bytes 0-0/94441025",
            "Content-Type": "video/mp4",
        }
        range_response.close = Mock()
        mock_get.return_value = range_response

        result = QualityProbe(("365yg.com",)).probe(
            "https://v11-default.365yg.com/video.mp4",
            timeout=1,
            expected_type="video",
            strategy="range_first",
            allow_fallback=False,
        )

        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 94_441_025)
        self.assertEqual(result.method, "RANGE")
        mock_head.assert_not_called()
        mock_get.assert_called_once()

    @patch("xhs_download.services.quality.requests.get")
    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_rejects_html_when_video_expected(self, mock_head, mock_get):
        head_response = Mock()
        head_response.status_code = 200
        head_response.url = "https://v3-web.douyinvod.com/video.mp4"
        head_response.headers = {"Content-Length": "1234", "Content-Type": "text/html"}
        head_response.close = Mock()
        mock_head.return_value = head_response

        range_response = Mock()
        range_response.status_code = 200
        range_response.url = "https://v3-web.douyinvod.com/video.mp4"
        range_response.headers = {"Content-Length": "1234", "Content-Type": "text/html"}
        range_response.close = Mock()
        mock_get.return_value = range_response

        result = QualityProbe().probe("https://v3-web.douyinvod.com/video.mp4", expected_type="video")

        self.assertFalse(result.reachable)
        self.assertIn("Unexpected content type", result.error)

    def test_select_best_media_candidate_prefers_probed_size(self):
        low = "https://v3-web.douyinvod.com/low.mp4"
        high = "https://v3-web.douyinvod.com/high.mp4"
        probe = Mock()
        probe.probe.side_effect = lambda url, headers=None, timeout=6, expected_type="": QualityProbeResult(
            url=url,
            final_url=url,
            reachable=True,
            size_bytes=20_000 if url == high else 2_000,
            content_type="video/mp4",
            method="HEAD",
        )

        selection = select_best_media_candidate(
            [
                MediaCandidate(url=low, size_bytes=10_000, source="declared_low"),
                MediaCandidate(url=high, size_bytes=1_000, source="declared_high"),
            ],
            quality_probe=probe,
        )

        self.assertEqual(selection.candidate.url, high)
        self.assertEqual(selection.candidate.size_bytes, 20_000)
        self.assertEqual(selection.reason, "selected_reachable_probe")

    def test_candidate_diagnostic_exposes_cdn_quality_params(self):
        diagnostic = CandidateDiagnostic(
            index=1,
            candidate=MediaCandidate(url="https://api-play.amemv.com/aweme/v1/play/?video_id=abc&ratio=540p"),
            reachable=True,
            probed_size_bytes=94_441_025,
            final_url=(
                "https://v11-default.365yg.com/video/tos/cn/path?"
                "br=48808&bt=48808&qs=13&btag=80000e00010000&cquery=106H&dy_q=1780802540"
            ),
            content_type="video/mp4",
            probe_method="HEAD",
        )

        data = diagnostic.as_dict()

        self.assertEqual(data["final_quality_params"]["br"], "48808")
        self.assertEqual(data["final_quality_params"]["bt"], "48808")
        self.assertEqual(data["final_quality_params"]["qs"], "13")
        self.assertEqual(data["final_quality_params"]["cquery"], "106H")

    @patch("xhs_download.services.common.requests.get")
    def test_request_get_with_retry_rejects_bad_final_redirect_host(self, mock_get):
        response = Mock()
        response.status_code = 200
        response.url = "http://127.0.0.1/private"
        response.close = Mock()
        mock_get.return_value = response

        with self.assertRaises(XhsSecurityError):
            request_get_with_retry(
                "https://ci.xiaohongshu.com/a",
                allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES,
                retries=1,
                sleep_seconds=0,
            )

        response.close.assert_called()

    @patch("xhs_download.services.common.time.sleep")
    @patch("xhs_download.services.common.requests.get")
    def test_request_get_with_retry_does_not_sleep_after_final_failure(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.Timeout("slow")

        with self.assertRaises(requests.Timeout):
            request_get_with_retry(
                "https://ci.xiaohongshu.com/a",
                retries=1,
            )

        mock_sleep.assert_not_called()


class RegionRoutingTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_country_code_rejects_unknown_proxy_value(self):
        self.assertEqual(normalize_country_code("cn"), "CN")
        self.assertEqual(normalize_country_code("XX"), "")
        self.assertEqual(normalize_country_code("T1"), "")

    @override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=False)
    def test_client_ip_ignores_untrusted_forwarded_header(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="203.0.113.8",
            HTTP_X_FORWARDED_FOR="198.51.100.9",
        )

        self.assertEqual(get_client_ip(request), "203.0.113.8")

    @override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=True)
    def test_client_ip_uses_first_trusted_forwarded_address(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="10.0.0.2",
            HTTP_X_FORWARDED_FOR="198.51.100.9, 10.0.0.1",
        )

        self.assertEqual(get_client_ip(request), "198.51.100.9")

    @override_settings(
        TIKHUB_TRUST_COUNTRY_HEADER=True,
        TIKHUB_COUNTRY_HEADER="HTTP_CF_IPCOUNTRY",
        TIKHUB_GEOIP_DATABASE_PATH="",
        TIKHUB_DEFAULT_COUNTRY_CODE="CN",
    )
    def test_country_routing_uses_trusted_proxy_header(self):
        request = self.factory.get("/", HTTP_CF_IPCOUNTRY="US")

        self.assertEqual(get_request_country_code(request), "US")

    @override_settings(
        TIKHUB_TRUST_COUNTRY_HEADER=False,
        TIKHUB_GEOIP_DATABASE_PATH="",
        TIKHUB_DEFAULT_COUNTRY_CODE="CN",
    )
    def test_country_routing_ignores_untrusted_proxy_header(self):
        request = self.factory.get("/", HTTP_CF_IPCOUNTRY="US")

        self.assertEqual(get_request_country_code(request), "CN")

    @override_settings(
        TIKHUB_TRUST_COUNTRY_HEADER=False,
        TIKHUB_GEOIP_DATABASE_PATH="/tmp/GeoLite2-Country.mmdb",
        TIKHUB_DEFAULT_COUNTRY_CODE="CN",
    )
    @patch(
        "xhs_download.services.region_routing.lookup_geoip_country_code",
        return_value="JP",
    )
    def test_country_routing_uses_geoip_before_default(self, _mock_lookup):
        request = self.factory.get("/", REMOTE_ADDR="8.8.8.8")

        self.assertEqual(get_request_country_code(request), "JP")


class XhsParserTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_registry_resolves_xiaohongshu_adapter(self):
        adapter = get_adapter_for_url("https://www.xiaohongshu.com/explore/abc123")

        self.assertIsInstance(adapter, XiaohongshuAdapter)

    def test_registry_resolves_xiaohongshu_short_link_adapter(self):
        adapter = get_adapter_for_url("https://xhslink.com/a/b")

        self.assertIsInstance(adapter, XiaohongshuAdapter)

    def test_registry_resolves_douyin_adapter(self):
        adapter = get_adapter_for_url("https://v.douyin.com/abc123/")

        self.assertIsInstance(adapter, DouyinAdapter)

    def test_registry_resolves_douyin_share_text(self):
        adapter = get_adapter_for_url("复制打开抖音，看看这个作品 https://v.douyin.com/abc123/ ！")

        self.assertIsInstance(adapter, DouyinAdapter)

    def test_registry_resolves_bilibili_adapter(self):
        adapter = get_adapter_for_url("https://www.bilibili.com/video/BV1xx411c7mD/")

        self.assertIsInstance(adapter, BilibiliAdapter)

    def test_registry_resolves_bilibili_short_link_adapter(self):
        adapter = get_adapter_for_url("https://b23.tv/abc123")

        self.assertIsInstance(adapter, BilibiliAdapter)

    def test_registry_resolves_generic_third_party_adapter(self):
        adapter = get_adapter_for_url("https://www.instagram.com/p/example/")

        self.assertIsInstance(adapter, GenericThirdPartyAdapter)

    def test_registry_resolves_third_party_share_text(self):
        adapter = get_adapter_for_url(
            "Open this TikTok https://www.tiktok.com/@demo/video/1234567890"
        )

        self.assertIsInstance(adapter, GenericThirdPartyAdapter)

    def test_registry_resolves_adapter_by_platform_id(self):
        adapter = get_adapter_for_platform("douyin")

        self.assertIsInstance(adapter, DouyinAdapter)

    def test_registry_resolves_third_party_rule_by_platform_id(self):
        adapter = get_adapter_for_platform("tiktok")

        self.assertIsInstance(adapter, GenericThirdPartyAdapter)

    def test_registry_lists_normalized_platform_metadata(self):
        platforms = {item["platform_id"]: item for item in list_platforms()}

        self.assertEqual(
            set(platforms),
            {"xiaohongshu", "douyin", "bilibili", "third_party"},
        )
        self.assertEqual(
            platforms["douyin"]["capabilities"]["media_types"],
            ("image", "video"),
        )
        self.assertTrue(platforms["douyin"]["capabilities"]["ultra_hd"])
        self.assertEqual(
            platforms["douyin"]["provider_policy"]["primary_provider"],
            "native",
        )
        self.assertEqual(
            platforms["douyin"]["provider_policy"]["fallback_providers"],
            ("kukutool", "tikhub"),
        )
        self.assertEqual(
            platforms["bilibili"]["provider_policy"]["provider_ids"],
            ("native",),
        )
        third_party_rules = {
            rule["platform_id"]
            for rule in platforms["third_party"]["platform_rules"]
        }
        self.assertIn("tiktok", third_party_rules)
        self.assertIn("instagram", third_party_rules)
        self.assertIn("imgur", third_party_rules)
        third_party_rules_by_id = {
            rule["platform_id"]: rule
            for rule in platforms["third_party"]["platform_rules"]
        }
        self.assertEqual(
            third_party_rules_by_id["youtube"]["provider_ids"],
            ("yt_dlp_generic", "kukutool_generic"),
        )
        self.assertEqual(
            third_party_rules_by_id["tiktok"]["provider_ids"],
            ("kukutool_generic", "yt_dlp_generic", "gallery_dl_generic"),
        )
        self.assertEqual(
            third_party_rules_by_id["imgur"]["provider_ids"],
            ("kukutool_generic", "gallery_dl_generic"),
        )
        self.assertEqual(
            platforms["third_party"]["provider_policy"]["third_party_providers"],
            (
                "kukutool_generic",
                "gallery_dl_generic",
                "yt_dlp_generic",
                "youtube_merge",
            ),
        )

    def test_registry_uses_adapter_parse_entrypoint(self):
        class FakeAdapter(BasePlatformAdapter):
            platform_id = "fake"
            page_host_suffixes = ("example.com",)
            parse_option_names = ("prefer_ultra_hd",)

            def __init__(self):
                self.received_prefer_ultra_hd = None

            def parse_url(self, url, prefer_ultra_hd=False):
                self.received_prefer_ultra_hd = prefer_ultra_hd
                return ParseResult(
                    platform=self.platform_id,
                    post_type="image",
                    base_filename="fake",
                    assets=(),
                )

        adapter = FakeAdapter()
        result = PlatformRegistry(adapters=(adapter,)).parse_url(
            "https://example.com/post/1",
            prefer_ultra_hd=True,
            client_country_code="US",
        )

        self.assertEqual(result.platform, "fake")
        self.assertTrue(adapter.received_prefer_ultra_hd)

    def test_extract_url_from_share_text_strips_trailing_punctuation(self):
        self.assertEqual(
            extract_url_from_text("复制打开 https://v.douyin.com/abc123/，"),
            "https://v.douyin.com/abc123/",
        )

    def test_extract_url_from_share_text_handles_url_prefix(self):
        self.assertEqual(
            extract_url_from_text("https://v.douyin.com/abc123/ 复制此链接，打开抖音搜索"),
            "https://v.douyin.com/abc123/",
        )

    def test_extract_aweme_id_from_douyin_urls(self):
        self.assertEqual(
            extract_aweme_id("https://www.douyin.com/video/7324602766213598501"),
            "7324602766213598501",
        )
        self.assertEqual(
            extract_aweme_id("https://www.douyin.com/?modal_id=7324602766213598501"),
            "7324602766213598501",
        )

    def test_normalize_image_url_extracts_spectrum_key(self):
        self.assertEqual(
            normalize_xhs_image_url("https://sns-img.xhscdn.com/spectrum/abc123!webp"),
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg",
        )

    def test_normalize_preview_image_url_uses_small_webp_variant(self):
        self.assertEqual(
            normalize_xhs_preview_image_url("https://sns-img.xhscdn.com/spectrum/abc123!webp"),
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/2/w/640/format/webp",
        )

    def test_parse_html_returns_image_post(self):
        html = """
        <html>
          <span class="username">user</span>
          <div id="detail-title">title</div>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/abc123!webp">
        </html>
        """

        result = parse_xhs_html(html)

        self.assertEqual(result["post_type"], "image")
        self.assertEqual(result["base_filename"], "user_title")
        self.assertEqual(result["image_urls"], ["https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg"])

    def test_xiaohongshu_adapter_returns_media_assets(self):
        html = """
        <html>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/abc123!webp">
        </html>
        """

        result = XiaohongshuAdapter().parse_html(html)

        self.assertEqual(result.platform, "xiaohongshu")
        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.image_assets[0].url, "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg")
        self.assertEqual(
            result.image_assets[0].preview_url,
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/2/w/640/format/webp",
        )

    def test_xiaohongshu_adapter_ignores_static_og_image_assets(self):
        html = """
        <html>
          <meta name="og:image" content="//picasso-static.xiaohongshu.com/fe-platform/e6214e4fbfae2cf14d634d4296916e8a5eaefdf4.png">
          <meta name="og:image" content="https://sns-img.xhscdn.com/notes_pre_post/1040g3k0321rmapod7u0g5nbcsha0baamosu1deo!webp">
        </html>
        """

        result = XiaohongshuAdapter().parse_html(html)

        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(
            result.image_assets[0].url,
            "https://ci.xiaohongshu.com/notes_pre_post/1040g3k0321rmapod7u0g5nbcsha0baamosu1deo?imageView2/format/jpg",
        )

    def test_parse_html_returns_video_post(self):
        html = """
        <html>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/cover123!webp">
          <script>{"originVideoKey":"video-key-123456789"}</script>
        </html>
        """

        result = parse_xhs_html(html)

        self.assertEqual(result["post_type"], "video")
        self.assertEqual(result["video_items"][0]["video_url"], "https://sns-video-bd.xhscdn.com/video-key-123456789")
        self.assertEqual(result["video_items"][0]["cover_url"], "https://ci.xiaohongshu.com/spectrum/cover123?imageView2/format/jpg")


class DouyinAdapterTests(SimpleTestCase):
    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    @patch("xhs_download.services.platforms.douyin.requests.get")
    def test_ultra_short_link_uses_redirect_only_and_extracts_aweme_id(self, mock_get, mock_retry_get):
        first_response = Mock()
        first_response.status_code = 302
        first_response.url = "https://v.douyin.com/example/"
        first_response.headers = {
            "Location": "https://www.douyin.com/video/7618531038729648354",
        }
        first_response.close = Mock()
        final_response = Mock()
        final_response.status_code = 200
        final_response.url = "https://www.douyin.com/video/7618531038729648354"
        final_response.headers = {}
        final_response.close = Mock()
        mock_get.side_effect = [first_response, final_response]

        context = DouyinAdapter().build_parse_context(
            "https://v.douyin.com/example/",
            fast_short_link=True,
        )

        self.assertEqual(context.aweme_id, "7618531038729648354")
        self.assertEqual(
            context.final_url,
            "https://www.douyin.com/video/7618531038729648354",
        )
        self.assertEqual(mock_get.call_count, 1)
        mock_retry_get.assert_not_called()

    @patch("xhs_download.services.platforms.douyin.requests.get")
    def test_short_link_stops_at_share_redirect_once_aweme_id_is_known(self, mock_get):
        response = Mock()
        response.status_code = 302
        response.url = "https://v.douyin.com/f_Oq8FIarA4/"
        response.headers = {
            "Location": (
                "https://www.iesdouyin.com/share/video/7430707517723905307/"
                "?region=CN"
            ),
        }
        response.close = Mock()
        mock_get.return_value = response

        final_url = DouyinAdapter().resolve_short_url(
            "https://v.douyin.com/f_Oq8FIarA4/",
        )

        self.assertEqual(
            extract_aweme_id(final_url),
            "7430707517723905307",
        )
        self.assertEqual(mock_get.call_count, 1)
        response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.requests.get")
    def test_short_link_rejects_redirect_to_untrusted_host(self, mock_get):
        response = Mock()
        response.status_code = 302
        response.url = "https://v.douyin.com/example/"
        response.headers = {"Location": "http://127.0.0.1/private"}
        response.close = Mock()
        mock_get.return_value = response

        with self.assertRaises(XhsSecurityError):
            DouyinAdapter().resolve_short_url("https://v.douyin.com/example/")

        response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_skips_direct_page_fetch_for_modal_id_without_ultra_hd(self, mock_request_get):
        expected = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="native",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v3-web.douyinvod.com/native.mp4",
                    platform="douyin",
                ),
            ),
        )

        with patch.object(DouyinAdapter, "fetch_mobile_feed_result", return_value=expected) as mock_feed:
            result = DouyinAdapter().parse_url(
                "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7539896907901062452",
                prefer_ultra_hd=False,
            )

        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/native.mp4")
        mock_feed.assert_called_once_with("7539896907901062452")
        mock_request_get.assert_not_called()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    @patch("xhs_download.services.platforms.douyin.build_douyin_result_providers")
    def test_parse_url_skips_direct_page_fetch_for_modal_id_with_ultra_hd(self, mock_provider_builder, mock_request_get):
        expected = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="ultra",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v11-default.365yg.com/ultra.mp4",
                    platform="douyin",
                    size_bytes=90_000_000,
                ),
            ),
        )

        class FakeProvider:
            def parse(self, adapter, context):
                self.context = context
                return expected

        provider = FakeProvider()
        mock_provider_builder.return_value = (provider,)

        result = DouyinAdapter().parse_url(
            "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7539896907901062452",
            prefer_ultra_hd=True,
        )

        self.assertEqual(result.video_assets[0].url, "https://v11-default.365yg.com/ultra.mp4")
        self.assertEqual(provider.context.aweme_id, "7539896907901062452")
        self.assertGreater(provider.context.deadline_monotonic, time.monotonic())
        mock_request_get.assert_not_called()

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        TIKHUB_API_KEY="",
    )
    def test_douyin_provider_builder_falls_back_to_native_provider(self):
        providers = build_douyin_result_providers()

        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], DouyinNativeProvider)

    def test_douyin_provider_builder_removes_enhanced_from_web_request_path(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native_enhanced",),
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_uses_kukutool_as_only_ultra_upstream(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("tikhub",),
            tikhub_api_key="secret",
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)
        self.assertFalse(any(isinstance(provider, DouyinTikHubProvider) for provider in providers))

    def test_douyin_provider_builder_ignores_configured_tikhub_order(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native", "tikhub"),
            tikhub_api_key="secret",
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_injects_current_kukutool_ultra_upstream(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native",),
            tikhub_api_key="secret",
        ))

        self.assertFalse(any(isinstance(provider, DouyinTikHubProvider) for provider in providers))
        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_uses_fast_native_path_without_request_switch(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub", "native_enhanced", "native"),
                tikhub_api_key="secret",
            ),
            prefer_ultra_hd=False,
        )

        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], DouyinNativeProvider)

    def test_tikhub_provider_response_schema_exposes_status_and_candidates(self):
        response = build_tikhub_provider_response(
            {
                "code": 500,
                "message": "provider failed",
                "request_id": "req-1",
                "data": {
                    "original_video_url": "https://v11-default.365yg.com/video/tos/cn/path.mp4?qs=13",
                },
            }
        )

        self.assertFalse(response.ok)
        self.assertEqual(response.code, "500")
        self.assertEqual(response.message, "provider failed")
        self.assertEqual(response.request_id, "req-1")
        self.assertEqual(len(response.candidates), 1)

    def test_tikhub_provider_response_keeps_declared_original_file_size(self):
        response = build_tikhub_provider_response(
            {
                "code": 200,
                "data": {
                    "original_video_url": "https://v5-se-jltc-cold.douyinvod.com/video.mp4",
                    "file_size": "150419221",
                    "content_type": "video/mp4",
                },
            }
        )

        self.assertEqual(response.candidates[0].size_bytes, 150_419_221)

    def test_tikhub_provider_response_reads_nested_error_detail(self):
        response = build_tikhub_provider_response(
            {
                "detail": {
                    "code": 400,
                    "message": "Request failed. Please retry.",
                    "request_id": "nested-request-id",
                }
            }
        )

        self.assertFalse(response.ok)
        self.assertEqual(response.code, "400")
        self.assertEqual(response.message, "Request failed. Please retry.")
        self.assertEqual(response.request_id, "nested-request-id")

    def test_tikhub_request_uses_aweme_id_without_redundant_share_url(self):
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
            )
        )

        params = provider.build_request_params(
            DouyinParseContext(
                url="https://www.douyin.com/user/self?modal_id=7637868943264238848",
                aweme_id="7637868943264238848",
            )
        )

        self.assertEqual(params["aweme_id"], "7637868943264238848")
        self.assertNotIn("share_url", params)

    def test_tikhub_route_uses_china_domain_for_mainland_user(self):
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_base_url="https://api.tikhub.io",
                tikhub_china_base_url="https://api.tikhub.dev",
            )
        )

        route = provider.get_request_route(
            DouyinParseContext(
                url="https://www.douyin.com/video/123",
                aweme_id="123",
                client_country_code="CN",
            )
        )

        self.assertEqual(route.region, "CN")
        self.assertEqual(route.base_url, "https://api.tikhub.dev")

    def test_tikhub_route_uses_global_domain_and_user_region_outside_china(self):
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_base_url="https://api.tikhub.io",
                tikhub_china_base_url="https://api.tikhub.dev",
            )
        )

        route = provider.get_request_route(
            DouyinParseContext(
                url="https://www.douyin.com/video/123",
                aweme_id="123",
                client_country_code="HK",
            )
        )

        self.assertEqual(route.region, "HK")
        self.assertEqual(route.base_url, "https://api.tikhub.io")

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_retries_explicit_temporary_400(self, mock_get):
        failed = Mock()
        failed.status_code = 400
        failed.json.return_value = {
            "code": 400,
            "message": "Request failed. Please retry. You won't be charged.",
        }
        failed.raise_for_status.side_effect = requests.HTTPError("temporary")
        failed.close = Mock()
        success = Mock()
        success.status_code = 200
        success.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v5-hl-mly-ov.zjcdn.com/video.mp4",
                "file_size": "182062062",
                "content_type": "application/octet-stream",
            },
        }
        success.raise_for_status = Mock()
        success.close = Mock()
        mock_get.side_effect = (failed, success)
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_max_attempts=2,
            )
        )

        data = provider.fetch_high_quality_data(
            DouyinParseContext(
                url="https://www.douyin.com/video/7636763779128216872",
                aweme_id="7636763779128216872",
                client_country_code="CN",
                deadline_monotonic=time.monotonic() + 15,
            )
        )

        self.assertEqual(data["data"]["file_size"], "182062062")
        self.assertEqual(mock_get.call_count, 2)
        self.assertTrue(mock_get.call_args_list[0].args[0].startswith("https://api.tikhub.dev/"))
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["region"], "CN")
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"]["region"], "HK")
        failed.close.assert_called_once()
        success.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_does_not_retry_non_retryable_400(self, mock_get):
        failed = Mock()
        failed.status_code = 400
        failed.json.return_value = {
            "code": 400,
            "message": "Invalid aweme_id",
        }
        failed.raise_for_status.side_effect = requests.HTTPError("invalid")
        failed.close = Mock()
        mock_get.return_value = failed
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_max_attempts=2,
            )
        )

        with self.assertRaises(requests.HTTPError):
            provider.fetch_high_quality_data(
                DouyinParseContext(
                    url="https://www.douyin.com/video/invalid",
                    aweme_id="invalid",
                    client_country_code="CN",
                )
            )

        self.assertEqual(mock_get.call_count, 1)

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_retries_network_timeout(self, mock_get):
        success = Mock()
        success.status_code = 200
        success.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v5-hl-mly-ov.zjcdn.com/video.mp4",
                "file_size": "182062062",
                "content_type": "application/octet-stream",
            },
        }
        success.raise_for_status = Mock()
        success.close = Mock()
        mock_get.side_effect = (requests.ReadTimeout("slow"), success)
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_max_attempts=2,
            )
        )

        data = provider.fetch_high_quality_data(
            DouyinParseContext(
                url="https://www.douyin.com/video/7636763779128216872",
                aweme_id="7636763779128216872",
                client_country_code="CN",
                deadline_monotonic=time.monotonic() + 15,
            )
        )

        self.assertEqual(data["data"]["file_size"], "182062062")
        self.assertEqual(mock_get.call_count, 2)
        success.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_retries_non_json_response(self, mock_get):
        invalid = Mock()
        invalid.status_code = 502
        invalid.json.side_effect = ValueError("not json")
        invalid.close = Mock()
        success = Mock()
        success.status_code = 200
        success.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v5-hl-mly-ov.zjcdn.com/video.mp4",
                "file_size": "7346662",
                "content_type": "video/mp4",
            },
        }
        success.raise_for_status = Mock()
        success.close = Mock()
        mock_get.side_effect = (invalid, success)
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_max_attempts=2,
            )
        )

        data = provider.fetch_high_quality_data(
            DouyinParseContext(
                url="https://www.douyin.com/video/7430707517723905307",
                aweme_id="7430707517723905307",
                client_country_code="CN",
                deadline_monotonic=time.monotonic() + 15,
            )
        )

        self.assertEqual(data["data"]["file_size"], "7346662")
        self.assertEqual(mock_get.call_count, 2)
        invalid.close.assert_called_once()
        success.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_retries_unusable_cn_payload_with_hk_region(self, mock_get):
        unusable = Mock()
        unusable.status_code = 200
        unusable.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v11-default.365yg.com/error",
                "file_size": "552",
                "content_type": "text/html",
            },
        }
        unusable.raise_for_status = Mock()
        unusable.close = Mock()
        success = Mock()
        success.status_code = 200
        success.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v5-hl-mly-ov.zjcdn.com/video.mp4",
                "file_size": "182062062",
                "content_type": "video/mp4",
            },
        }
        success.raise_for_status = Mock()
        success.close = Mock()
        mock_get.side_effect = (unusable, success)
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_max_attempts=2,
            )
        )

        data = provider.fetch_high_quality_data(
            DouyinParseContext(
                url="https://www.douyin.com/video/7636763779128216872",
                aweme_id="7636763779128216872",
                client_country_code="CN",
                deadline_monotonic=time.monotonic() + 15,
            )
        )

        self.assertEqual(data["data"]["file_size"], "182062062")
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["region"], "CN")
        self.assertEqual(mock_get.call_args_list[1].kwargs["params"]["region"], "HK")

    def test_tikhub_validated_cache_is_isolated_by_domain_and_region(self):
        context = DouyinParseContext(
            url="https://www.douyin.com/video/123",
            aweme_id="123",
        )

        china_key = build_tikhub_validated_cache_key(
            context,
            {"aweme_id": "123", "region": "CN"},
            "https://api.tikhub.dev",
        )
        global_key = build_tikhub_validated_cache_key(
            context,
            {"aweme_id": "123", "region": "US"},
            "https://api.tikhub.io",
        )

        self.assertNotEqual(china_key, global_key)
        self.assertIn("api.tikhub.dev:cn", china_key)
        self.assertIn("api.tikhub.io:us", global_key)

    def test_native_provider_logs_fallback_failures(self):
        class BrokenProvider:
            name = "broken"

            def parse(self, adapter, context):
                raise RuntimeError("boom")

        class FakeAdapter:
            pass

        with self.assertLogs("xhs_download.services.platforms.douyin_providers", level="INFO") as captured:
            result = DouyinNativeProvider(fallback_providers=(BrokenProvider(),)).parse(
                FakeAdapter(),
                DouyinParseContext(url="https://www.douyin.com/video/123", aweme_id="123"),
            )

        self.assertIsNone(result)
        self.assertTrue(any("douyin_provider_failed" in item for item in captured.output))

    def test_provider_log_fields_exclude_query_parameters(self):
        fields = context_log_fields(DouyinParseContext(
            url="https://www.douyin.com/video/123?token=sensitive",
            aweme_id="123",
        ))

        self.assertEqual(fields["source_host"], "www.douyin.com")
        self.assertEqual(fields["source_path"], "/video/123")
        self.assertNotIn("url", fields)

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_returns_validated_original_video_url(self, mock_get, mock_probe):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com",)

            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_title",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native.mp4",
                            platform="douyin",
                            cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                        ),
                    ),
                )

        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": (
                    "https://v11-default.365yg.com/video/tos/cn/path?"
                    "br=48808&bt=48808&qs=13"
                )
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url=response.json.return_value["data"]["original_video_url"],
            final_url=response.json.return_value["data"]["original_video_url"],
            reachable=True,
            size_bytes=94_441_025,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_region="CN",
            )
        ).parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7618531038729648354",
                aweme_id="7618531038729648354",
            ),
        )

        self.assertIn(
            result.base_filename,
            {"native_title", "douyin_7618531038729648354"},
        )
        self.assertEqual(result.video_assets[0].size_bytes, 94_441_025)
        self.assertEqual(result.video_assets[0].quality_label, "原始上传画质")
        self.assertIn("fetch_video_high_quality_play_url", mock_get.call_args.args[0])
        self.assertTrue(mock_get.call_args.args[0].startswith("https://api.tikhub.dev/"))
        self.assertEqual(mock_get.call_args.kwargs["params"]["aweme_id"], "7618531038729648354")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_returns_tikhub_even_when_native_video_is_larger(self, mock_get, mock_probe):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_1080p",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native-1080p.mp4",
                            platform="douyin",
                            size_bytes=20_000_000,
                            cover_url="https://p3-sign.douyinpic.com/native-cover.jpeg",
                        ),
                    ),
                )

            def fetch_mobile_share_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_720p",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native-720p.mp4",
                            platform="douyin",
                            size_bytes=2_500_000,
                        ),
                    ),
                )

        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v11-default.365yg.com/video/tos/cn/path?br=1200&bt=1200&qs=13",
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url=response.json.return_value["data"]["original_video_url"],
            final_url=response.json.return_value["data"]["original_video_url"],
            reachable=True,
            size_bytes=2_000_000,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
            )
        ).parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7539896907901062452",
                aweme_id="7539896907901062452",
            ),
        )

        self.assertEqual(
            result.video_assets[0].url,
            response.json.return_value["data"]["original_video_url"],
        )
        self.assertEqual(result.video_assets[0].size_bytes, 2_000_000)
        self.assertEqual(result.metadata["selected_provider"], "tikhub")

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_caches_validated_candidate(self, mock_get, mock_probe):
        cache.clear()
        response = Mock()
        response.json.return_value = {"code": 200, "data": {"original_video_url": "https://v11-default.365yg.com/a.mp4"}}
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url="https://v11-default.365yg.com/a.mp4",
            final_url="https://v11-default.365yg.com/a.mp4",
            reachable=True,
            size_bytes=90_000_000,
            content_type="video/mp4",
            method="RANGE",
        )
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_validated_cache_ttl_seconds=60,
            )
        )
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7618531038729648354",
            aweme_id="7618531038729648354",
        )
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)

        first = provider.resolve_validated_candidate(adapter, context)
        second = provider.resolve_validated_candidate(adapter, context)

        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.candidate.size_bytes, 90_000_000)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_probe.call_count, 1)

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_accepts_declared_original_size_without_cdn_probe(self, mock_get, mock_probe):
        cache.clear()
        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v5-se-jltc-cold.douyinvod.com/video.mp4",
                "file_size": "150419221",
                "content_type": "video/mp4",
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
            )
        )
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7637868943264238848",
            aweme_id="7637868943264238848",
        )
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("douyinvod.com",)

        selected = provider.resolve_validated_candidate(adapter, context)

        self.assertEqual(selected.candidate.size_bytes, 150_419_221)
        self.assertEqual(selected.content_type, "video/mp4")
        mock_probe.assert_not_called()

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_fast_path_uses_short_timeouts_and_one_range_probe(self, mock_get, mock_probe):
        cache.clear()
        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v11-default.365yg.com/a.mp4",
                "play_url": "https://v26-default.365yg.com/b.mp4",
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url="https://v11-default.365yg.com/a.mp4",
            final_url="https://v11-default.365yg.com/a.mp4",
            reachable=True,
            size_bytes=90_000_000,
            content_type="video/mp4",
            method="RANGE",
        )
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_connect_timeout_seconds=0.7,
                tikhub_read_timeout_seconds=1.5,
                tikhub_probe_timeout_seconds=1.0,
                tikhub_max_probe_candidates=1,
            )
        )
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7618531038729648354",
            aweme_id="7618531038729648354",
        )
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)

        provider.resolve_validated_candidate(adapter, context)

        self.assertEqual(mock_get.call_args.kwargs["timeout"], (0.7, 1.5))
        self.assertEqual(mock_probe.call_count, 1)
        self.assertEqual(mock_probe.call_args.kwargs["timeout"], 1.0)
        self.assertEqual(mock_probe.call_args.kwargs["strategy"], "range_first")
        self.assertFalse(mock_probe.call_args.kwargs["allow_fallback"])

    def test_tikhub_total_budget_returns_completed_native_without_waiting_for_slow_provider(self):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(
                self,
                aweme_id,
                probe_video_candidates=False,
                request_timeout=15,
                request_retries=2,
            ):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native.mp4",
                            platform="douyin",
                        ),
                    ),
                )

        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                ultra_total_timeout_seconds=0.1,
                ultra_native_timeout_seconds=0.05,
            )
        )

        def slow_tikhub(adapter, context):
            time.sleep(0.25)
            return None

        provider.resolve_validated_candidate = slow_tikhub
        started_at = time.monotonic()
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7618531038729648354",
                aweme_id="7618531038729648354",
            ),
        )
        elapsed = time.monotonic() - started_at

        self.assertEqual(result.base_filename, "native")
        self.assertLess(elapsed, 0.18)

    def test_tikhub_success_does_not_wait_for_slow_native(self):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(
                self,
                aweme_id,
                probe_video_candidates=False,
                request_timeout=15,
                request_retries=2,
            ):
                time.sleep(0.2)
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native.mp4",
                            platform="douyin",
                        ),
                    ),
                )

        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
            )
        )
        provider.resolve_validated_candidate = lambda adapter, context: ValidatedProviderCandidate(
            candidate=MediaCandidate(
                url="https://v11-default.365yg.com/ultra.mp4",
                size_bytes=90_000_000,
                quality_label="原始上传画质",
            ),
            content_type="video/mp4",
        )

        started_at = time.monotonic()
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7618531038729648354",
                aweme_id="7618531038729648354",
            ),
        )
        elapsed = time.monotonic() - started_at

        self.assertEqual(result.metadata["selected_provider"], "tikhub")
        self.assertLess(elapsed, 0.1)

    def test_tikhub_fast_native_uses_mobile_share_when_feed_has_no_assets(self):
        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(
                self,
                aweme_id,
                probe_video_candidates=False,
                request_timeout=15,
                request_retries=2,
            ):
                raise ValueError("mobile feed unavailable")

            def fetch_mobile_share_result(
                self,
                aweme_id,
                probe_video_candidates=False,
                request_timeout=15,
                request_retries=2,
            ):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="share-native",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/share.mp4",
                            platform="douyin",
                        ),
                    ),
                )

        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                ultra_total_timeout_seconds=0.2,
                ultra_native_timeout_seconds=0.15,
            )
        )
        provider.resolve_validated_candidate = lambda adapter, context: None

        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7539896907901062452",
                aweme_id="7539896907901062452",
            ),
        )

        self.assertEqual(result.base_filename, "share-native")
        self.assertEqual(
            result.video_assets[0].url,
            "https://v3-web.douyinvod.com/share.mp4",
        )

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_daily_limit_uses_shared_cache_counter(self, mock_get):
        cache.clear()
        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {"original_video_url": "https://v11-default.365yg.com/a.mp4"},
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_daily_limit=1,
            )
        )

        provider.fetch_high_quality_data(DouyinParseContext(
            url="https://www.douyin.com/video/1",
            aweme_id="1",
        ))
        with self.assertRaisesMessage(RuntimeError, "TikHub daily limit reached"):
            provider.fetch_high_quality_data(DouyinParseContext(
                url="https://www.douyin.com/video/2",
                aweme_id="2",
            ))

        self.assertEqual(mock_get.call_count, 1)

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_daily_limit_counts_each_retry_attempt(self, mock_get):
        cache.clear()
        failed = Mock()
        failed.status_code = 400
        failed.json.return_value = {
            "code": 400,
            "message": "Request failed. Please retry.",
        }
        failed.raise_for_status.side_effect = requests.HTTPError("temporary")
        failed.close = Mock()
        mock_get.return_value = failed
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_daily_limit=1,
                tikhub_max_attempts=2,
            )
        )

        with self.assertRaisesMessage(
            RuntimeError,
            "TikHub daily limit reached",
        ):
            provider.fetch_high_quality_data(
                DouyinParseContext(
                    url="https://www.douyin.com/video/1",
                    aweme_id="1",
                )
            )

        self.assertEqual(mock_get.call_count, 1)
        failed.close.assert_called_once()

    def test_native_enhanced_provider_selects_largest_video_result_across_sources(self):
        class FakeAdapter:
            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="feed",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/feed.mp4", platform="douyin", size_bytes=100),),
                )

            def fetch_mobile_share_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="share",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/share.mp4", platform="douyin", size_bytes=1000),),
                )

            def parse_html(self, html, source_url=None, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="direct",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/direct.mp4", platform="douyin", size_bytes=500),),
                )

        result = DouyinNativeEnhancedProvider().parse(
            FakeAdapter(),
            DouyinParseContext(url="https://www.douyin.com/video/123", aweme_id="123", html="<html></html>"),
        )

        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/share.mp4")

    def test_parse_html_returns_image_post_from_render_data(self):
        data = {
            "aweme_detail": {
                "aweme_id": "123",
                "desc": "图文标题",
                "author": {"nickname": "作者"},
                "images": [
                    {"url_list": ["https://p3-sign.douyinpic.com/image-1.webp"]},
                    {"download_url_list": ["https://p6-sign.douyinpic.com/image-2.webp"]},
                ],
            }
        }
        encoded = json.dumps(data, ensure_ascii=False)
        html = f'<script id="RENDER_DATA" type="application/json">{encoded}</script>'

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.base_filename, "作者_图文标题")
        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.image_assets[0].url, "https://p3-sign.douyinpic.com/image-1.webp")

    def test_parse_aweme_prefers_unwatermarked_image_url_list(self):
        result = DouyinAdapter().parse_aweme({
            "aweme_id": "7634213610894689787",
            "desc": "图文标题",
            "author": {"nickname": "作者"},
            "images": [
                {
                    "download_url_list": [
                        "https://p3-sign.douyinpic.com/tos/image~tplv-dy-water-v10:author:3000:3000:q75.webp",
                    ],
                    "url_list": [
                        "https://p3-sign.douyinpic.com/tos/image~tplv-dy-aweme-images-v2:2560:1440:q75.webp",
                    ],
                },
            ],
        })

        self.assertEqual(result.post_type, "image")
        self.assertEqual(len(result.image_assets), 1)
        self.assertIn("tplv-dy-aweme-images-v2", result.image_assets[0].url)
        self.assertNotIn("tplv-dy-water", result.image_assets[0].url)

    def test_parse_html_returns_highest_bitrate_video(self):
        data = {
            "loaderData": {
                "aweme": {
                    "aweme_id": "456",
                    "desc": "视频标题",
                    "author": {"nickname": "作者"},
                    "video": {
                        "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.webp"]},
                        "bit_rate": [
                            {
                                "bit_rate": 1000,
                                "play_addr": {"url_list": ["https://v3-web.douyinvod.com/low.mp4"]},
                            },
                            {
                                "bit_rate": 3000,
                                "play_addr": {"url_list": ["https://v3-web.douyinvod.com/high.mp4"]},
                            },
                        ],
                    },
                }
            }
        }
        html = f"""
        <script>
          window._ROUTER_DATA = {json.dumps(data, ensure_ascii=False)};
        </script>
        """

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/high.mp4")
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.webp")

    def test_parse_aweme_does_not_treat_audio_candidates_as_video(self):
        result = DouyinAdapter().parse_aweme({
            "aweme_id": "7641197695256245190",
            "desc": "图片音乐内容",
            "author": {"nickname": "作者"},
            "images": [
                {"url_list": ["https://p3-sign.douyinpic.com/image-1.webp"]},
                {"url_list": ["https://p26-sign.douyinpic.com/image-2.webp"]},
            ],
            "video": {
                "play_addr": {
                    "url_list": [
                        "https://v3-music.douyinstatic.com/video/tos/cn/path/?mime_type=audio_mp4",
                        "https://api-play.amemv.com/aweme/v1/play/music/?video_id=audio",
                        "https://lf26-music-east.douyinstatic.com/obj/ies-music-hj/audio.mp3",
                    ],
                },
            },
        })

        self.assertEqual(result.post_type, "image")
        self.assertEqual(len(result.image_assets), 2)
        self.assertFalse(result.video_assets)

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    def test_parse_aweme_enhanced_prefers_larger_probed_video(self, mock_probe):
        low = "https://v3-web.douyinvod.com/low.mp4"
        high = "https://v3-web.douyinvod.com/high.mp4"
        mock_probe.side_effect = lambda url, headers=None, timeout=6, expected_type="": QualityProbeResult(
            url=url,
            final_url=url,
            reachable=True,
            size_bytes=25_000 if url == high else 5_000,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinAdapter().parse_aweme(
            {
                "aweme_id": "456",
                "desc": "视频标题",
                "author": {"nickname": "作者"},
                "video": {
                    "bit_rate": [
                        {
                            "bit_rate": 3000,
                            "play_addr": {
                                "data_size": 12_000,
                                "url_list": [low],
                            },
                        },
                        {
                            "bit_rate": 1000,
                            "play_addr": {
                                "data_size": 1_000,
                                "url_list": [high],
                            },
                        },
                    ],
                },
            },
            probe_video_candidates=True,
        )

        self.assertEqual(result.video_assets[0].url, high)
        self.assertEqual(result.video_assets[0].size_bytes, 25_000)

    def test_parse_html_returns_video_from_mobile_share_router_data(self):
        data = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7324602766213598501",
                                "desc": "视频标题",
                                "author": {"nickname": "作者"},
                                "video": {
                                    "play_addr": {
                                        "url_list": [
                                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"
                                        ]
                                    },
                                    "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.webp"]},
                                },
                            }
                        ],
                        "status_code": 0,
                    }
                }
            }
        }
        html = f"""
        <script>
          window._ROUTER_DATA = {json.dumps(data, ensure_ascii=False)};
        </script>
        """

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(
            result.video_assets[0].url,
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.webp")

    def test_parse_aweme_prefers_browser_friendly_cover_urls(self):
        result = DouyinAdapter().parse_aweme({
            "aweme_id": "456",
            "desc": "视频标题",
            "author": {"nickname": "作者"},
            "video": {
                "cover": {
                    "url_list": [
                        "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.heic?x=1",
                        "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
                    ],
                },
                "origin_cover": {
                    "url_list": [
                        "https://p3-sign.douyinpic.com/tos/origin~tplv-dy.heic?x=1",
                    ],
                },
                "play_addr": {
                    "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"],
                },
            },
        })

        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
        )
        self.assertEqual(
            result.video_assets[0].cover_preview_url,
            "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
        )

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_falls_back_to_mobile_share_page(self, mock_get):
        mobile_data = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7324602766213598501",
                                "desc": "视频标题",
                                "author": {"nickname": "作者"},
                                "video": {
                                    "play_addr": {
                                        "url_list": [
                                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"
                                        ]
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
        mobile_response = Mock()
        mobile_response.status_code = 200
        mobile_response.url = "https://www.iesdouyin.com/share/video/7324602766213598501/?region=CN"
        mobile_response.text = f"<script>window._ROUTER_DATA = {json.dumps(mobile_data)};</script>"
        mobile_response.close = Mock()
        mock_get.side_effect = [ValueError("feed unavailable"), mobile_response]

        result = DouyinAdapter().parse_url("https://www.douyin.com/video/7324602766213598501")

        self.assertEqual(result.post_type, "video")
        self.assertEqual(len(result.video_assets), 1)
        self.assertIn("/aweme/v1/feed/", mock_get.call_args_list[0].args[0])
        self.assertIn("/share/video/7324602766213598501/", mock_get.call_args_list[1].args[0])
        mobile_response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_continues_to_note_share_when_video_share_fails(self, mock_get):
        note_data = {
            "loaderData": {
                "aweme": {
                    "aweme_id": "7324602766213598501",
                    "desc": "图文标题",
                    "author": {"nickname": "作者"},
                    "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
                }
            }
        }
        note_response = Mock()
        note_response.status_code = 200
        note_response.url = "https://www.iesdouyin.com/share/note/7324602766213598501/?region=CN"
        note_response.text = f"<script>window._ROUTER_DATA = {json.dumps(note_data)};</script>"
        note_response.close = Mock()
        mock_get.side_effect = [
            ValueError("feed unavailable"),
            ValueError("video share unavailable"),
            note_response,
        ]

        result = DouyinAdapter().parse_url("https://www.douyin.com/note/7324602766213598501")

        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.image_assets[0].url, "https://p3-sign.douyinpic.com/image.webp")
        self.assertIn("/share/video/7324602766213598501/", mock_get.call_args_list[1].args[0])
        self.assertIn("/share/note/7324602766213598501/", mock_get.call_args_list[2].args[0])
        note_response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_uses_mobile_feed_download_addr_when_available(self, mock_get):
        feed_response = Mock()
        feed_response.json.return_value = {
            "aweme_list": [
                {
                    "aweme_id": "7324602766213598501",
                    "desc": "视频标题",
                    "author": {"nickname": "作者"},
                    "video": {
                        "download_addr": {
                            "data_size": 4_395_633,
                            "height": 720,
                            "width": 720,
                            "url_list": ["https://api-play.amemv.com/aweme/v1/play/?video_id=abc&watermark=1&biz_sign=abc"],
                        },
                        "bit_rate": [
                            {
                                "bit_rate": 626067,
                                "play_addr": {
                                    "data_size": 1_270_448,
                                    "height": 576,
                                    "width": 1024,
                                    "url_list": ["https://v3-web.douyinvod.com/low.mp4"],
                                },
                            }
                        ],
                    },
                }
            ]
        }
        feed_response.close = Mock()
        mock_get.return_value = feed_response

        result = DouyinAdapter().parse_url("https://www.douyin.com/video/7324602766213598501")

        self.assertEqual(result.post_type, "video")
        self.assertEqual(
            result.video_assets[0].url,
            "https://api-play.amemv.com/aweme/v1/play/?video_id=abc&watermark=1&biz_sign=abc",
        )
        self.assertEqual(result.video_assets[0].size_bytes, 4_395_633)
        self.assertIn("/aweme/v1/feed/", mock_get.call_args_list[0].args[0])
        feed_response.close.assert_called_once()

    def test_get_video_candidates_prefers_high_quality_unwatermarked_play_url(self):
        candidates = get_video_candidates({
            "play_addr": {
                "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"]
            }
        })

        self.assertEqual(
            candidates[0],
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )
        self.assertIn(
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=1080p",
            candidates,
        )

    def test_get_video_candidates_keeps_ratio_priority_with_bitrate_items(self):
        candidates = get_video_candidates({
            "bit_rate": [
                {
                    "bit_rate": 1200000,
                    "play_addr": {
                        "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"]
                    },
                }
            ]
        })

        self.assertEqual(
            candidates[0],
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )

    def test_get_media_headers_uses_douyin_headers_for_douyin_media(self):
        self.assertIs(
            get_media_headers("https://v3-web.douyinvod.com/video.mp4"),
            DOUYIN_HEADERS,
        )


class BilibiliAdapterTests(SimpleTestCase):
    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_parse_url_returns_video_asset_from_public_apis(self, mock_get):
        view_response = Mock()
        view_response.json.return_value = {
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "title": "测试标题",
                "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 62131, "page": 1, "part": "P1"}],
            },
        }
        view_response.close = Mock()

        play_response = Mock()
        play_response.json.return_value = {
            "code": 0,
            "data": {
                "quality": 80,
                "durl": [
                    {
                        "order": 2,
                        "size": 100,
                        "url": "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/low.mp4",
                    },
                    {
                        "order": 1,
                        "size": 200,
                        "url": "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/high.mp4",
                    },
                ],
            },
        }
        play_response.close = Mock()
        mock_get.side_effect = [view_response, play_response]

        result = BilibiliAdapter().parse_url("https://www.bilibili.com/video/BV1xx411c7mD/")

        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.base_filename, "作者_测试标题")
        self.assertEqual(len(result.video_assets), 2)
        self.assertEqual(
            result.video_assets[0].url,
            "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/high.mp4",
        )
        self.assertEqual(
            result.video_assets[1].url,
            "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/low.mp4",
        )
        self.assertEqual(result.video_assets[0].size_bytes, 200)
        self.assertEqual(result.video_assets[1].size_bytes, 100)
        self.assertEqual(result.video_assets[0].cover_url, "https://i0.hdslb.com/bfs/archive/cover.jpg")
        self.assertIn("x/web-interface/view?bvid=BV1xx411c7mD", mock_get.call_args_list[0].args[0])
        self.assertIn("x/player/playurl?", mock_get.call_args_list[1].args[0])

    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_parse_url_resolves_b23_short_link(self, mock_get):
        resolve_response = Mock()
        resolve_response.url = "https://www.bilibili.com/video/BV1xx411c7mD/?p=1"
        resolve_response.close = Mock()

        view_response = Mock()
        view_response.json.return_value = {
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "title": "测试标题",
                "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 62131, "page": 1, "part": "P1"}],
            },
        }
        view_response.close = Mock()

        play_response = Mock()
        play_response.json.return_value = {
            "code": 0,
            "data": {
                "quality": 64,
                "durl": [{"size": 100, "url": "//upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4"}],
            },
        }
        play_response.close = Mock()
        mock_get.side_effect = [resolve_response, view_response, play_response]

        result = BilibiliAdapter().parse_url("https://b23.tv/abc123")

        self.assertEqual(result.video_assets[0].url, "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4")
        self.assertEqual(result.video_assets[0].cover_url, "https://i0.hdslb.com/bfs/archive/cover.jpg")
        self.assertEqual(mock_get.call_args_list[0].args[0], "https://b23.tv/abc123")

    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_parse_url_encodes_unicode_referer_for_api_headers(self, mock_get):
        view_response = Mock()
        view_response.json.return_value = {
            "code": 0,
            "data": {
                "bvid": "BV1REjd6xEZf",
                "aid": 2,
                "title": "智能体副业还能做吗",
                "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 62131, "page": 1, "part": "P1"}],
            },
        }
        view_response.close = Mock()

        play_response = Mock()
        play_response.json.return_value = {
            "code": 0,
            "data": {
                "quality": 80,
                "durl": [
                    {
                        "size": 100,
                        "url": "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4",
                    }
                ],
            },
        }
        play_response.close = Mock()
        mock_get.side_effect = [view_response, play_response]

        BilibiliAdapter().parse_url(
            "https://www.bilibili.com/video/BV1REjd6xEZf/?title_encode=智能体副业还能做吗？2026 年真实情况"
        )

        for call in mock_get.call_args_list:
            referer = call.kwargs["headers"]["Referer"]
            self.assertNotIn("智能体", referer)
            referer.encode("latin-1")
        self.assertIn("%E6%99%BA%E8%83%BD%E4%BD%93", mock_get.call_args_list[0].kwargs["headers"]["Referer"])

    def test_get_media_headers_uses_bilibili_headers_for_bilibili_media(self):
        self.assertIs(
            get_media_headers("https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4"),
            BILIBILI_HEADERS,
        )


class XhsDownloadArchiveTests(SimpleTestCase):
    def test_kukutool_download_failure_refreshes_once_then_uses_new_url(self):
        item = make_kukutool_download_item(
            "https://v26-default.365yg.com/a/7fffffff/video/tos/cn/source"
        )
        refreshed_url = (
            "https://v9-default.365yg.com/b/7fffffff/video/tos/cn/source"
        )
        temp_file = NamedTemporaryFile(delete=False)
        temp_file.close()
        result = {
            "path": temp_file.name,
            "content_type": "video/mp4",
            "final_url": refreshed_url,
            "size": 1,
        }

        with (
            patch(
                "xhs_download.services.downloads.fetch_to_temp_path",
                side_effect=[XhsDownloadError("HTTP 403"), result],
            ) as mock_fetch,
            patch(
                "xhs_download.services.downloads.iter_kukutool_download_recovery_urls",
                return_value=[refreshed_url],
            ),
        ):
            selected = fetch_item_to_temp_path(
                item,
                expected_type="video",
            )

        Path(temp_file.name).unlink(missing_ok=True)
        self.assertEqual(selected["final_url"], refreshed_url)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(mock_fetch.call_args_list[0].kwargs["retries"], 1)
        self.assertEqual(mock_fetch.call_args_list[1].kwargs["retries"], 1)

    def test_kukutool_refresh_failure_falls_back_to_native_once(self):
        item = make_kukutool_download_item(
            "https://v26-default.365yg.com/a/7fffffff/video/tos/cn/source"
        )
        refreshed_url = (
            "https://v9-default.365yg.com/b/7fffffff/video/tos/cn/source"
        )
        native_url = "https://v3-web.douyinvod.com/native.mp4"
        temp_file = NamedTemporaryFile(delete=False)
        temp_file.close()
        result = {
            "path": temp_file.name,
            "content_type": "video/mp4",
            "final_url": native_url,
            "size": 1,
        }

        with (
            patch(
                "xhs_download.services.downloads.fetch_to_temp_path",
                side_effect=[
                    XhsDownloadError("HTTP 403"),
                    XhsDownloadError("HTTP 404"),
                    result,
                ],
            ) as mock_fetch,
            patch(
                "xhs_download.services.downloads.iter_kukutool_download_recovery_urls",
                return_value=[refreshed_url, native_url],
            ),
        ):
            selected = fetch_item_to_temp_path(
                item,
                expected_type="video",
            )

        Path(temp_file.name).unlink(missing_ok=True)
        self.assertEqual(selected["final_url"], native_url)
        self.assertEqual(mock_fetch.call_count, 3)

    def test_local_download_limit_error_does_not_refresh_provider(self):
        item = make_kukutool_download_item(
            "https://v26-default.365yg.com/a/7fffffff/video/tos/cn/source"
        )

        with (
            patch(
                "xhs_download.services.downloads.fetch_to_temp_path",
                side_effect=XhsDownloadError("资源超过允许的大小限制"),
            ),
            patch(
                "xhs_download.services.downloads.iter_kukutool_download_recovery_urls"
            ) as mock_recovery,
        ):
            with self.assertRaisesRegex(XhsDownloadError, "大小限制"):
                fetch_item_to_temp_path(item, expected_type="video")

        mock_recovery.assert_not_called()

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_video_download_rejects_html_error_page(self, mock_get):
        response = FakeStreamingResponse(
            b"<html>blocked</html>",
            url="https://v26-default.365yg.com/error",
            content_type="text/html",
        )
        mock_get.return_value = response

        with self.assertRaisesRegex(XhsDownloadError, "资源类型异常"):
            fetch_to_temp_path(
                "https://v26-default.365yg.com/error",
                expected_type="video",
            )

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_video_download_rejects_missing_content_type(self, mock_get):
        response = FakeStreamingResponse(
            b"not-a-video",
            url="https://v26-default.365yg.com/error",
            content_type="",
        )
        mock_get.return_value = response

        with self.assertRaisesRegex(XhsDownloadError, "missing Content-Type"):
            fetch_to_temp_path(
                "https://v26-default.365yg.com/error",
                expected_type="video",
            )

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_video_download_rejects_html_body_with_binary_content_type(
        self,
        mock_get,
    ):
        response = FakeStreamingResponse(
            b"<!doctype html><html>blocked</html>",
            url="https://v26-default.365yg.com/error",
            content_type="application/octet-stream",
        )
        mock_get.return_value = response

        with self.assertRaisesRegex(XhsDownloadError, "HTML response body"):
            fetch_to_temp_path(
                "https://v26-default.365yg.com/error",
                expected_type="video",
            )

    @patch("xhs_download.services.downloads.fetch_to_temp_path")
    def test_signed_external_video_download_failure_clears_payload_and_failure_cache(
        self,
        mock_fetch,
    ):
        media_url = "https://rr1---sn.googlevideo.com/videoplayback?itag=22"
        asset_id = "third_party:youtube:abc123:video:1"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
            media_type="video",
        )
        item = {
            "type": "video",
            "url": media_url,
            "asset_id": asset_id,
            "provider": "yt_dlp_generic",
            "provider_resource_id": provider_resource_id,
            "provider_cache_key": provider_cache_key,
            "provider_manifest_token": token,
        }
        payload_cache_key = external_payload_cache_key(
            "yt_dlp_generic",
            provider_resource_id,
        )
        failure_cache_key = external_failure_cache_key(
            "yt_dlp_generic",
            provider_resource_id,
        )
        cache.set(payload_cache_key, {"title": "stale ytdlp payload"}, timeout=60)
        cache.set(failure_cache_key, "TimeoutError", timeout=60)
        mock_fetch.side_effect = XhsDownloadError("HTTP 403")

        with self.assertRaisesRegex(XhsDownloadError, "HTTP 403"):
            fetch_item_to_temp_path(item, expected_type="video")

        self.assertIsNone(cache.get(payload_cache_key))
        self.assertIsNone(cache.get(failure_cache_key))
        self.assertTrue(mock_fetch.call_args.kwargs["provider_signed"])

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_kukutool_recovery_invalidates_exact_cache_before_refresh(self):
        aweme_id = "7647533902413173321"
        cache_key = build_kukutool_cache_key(aweme_id)
        cache.set(cache_key, {"url": "stale"}, timeout=300)
        refreshed_url = (
            "https://v9-default.365yg.com/b/7fffffff/video/tos/cn/source"
        )

        def assert_invalidated(*_args, **_kwargs):
            self.assertIsNone(cache.get(cache_key))
            return Mock(candidate=Mock(url=refreshed_url))

        item = make_kukutool_download_item(
            "https://v26-default.365yg.com/a/7fffffff/video/tos/cn/source",
            aweme_id=aweme_id,
        )
        with (
            patch(
                "xhs_download.services.provider_recovery."
                "DouyinKukuToolProvider.refresh_for_download",
                side_effect=assert_invalidated,
            ),
            patch(
                "xhs_download.services.provider_recovery.get_fast_native_result"
            ) as mock_native,
        ):
            recovery = iter_kukutool_download_recovery_urls(item)
            self.assertEqual(next(recovery), refreshed_url)

        mock_native.assert_not_called()

    def test_tampered_kukutool_manifest_cannot_trigger_provider_refresh(self):
        item = make_kukutool_download_item(
            "https://v26-default.365yg.com/a/7fffffff/video/tos/cn/source"
        )
        item["provider_resource_id"] = "7647533902413173399"

        self.assertEqual(
            list(iter_kukutool_download_recovery_urls(item) or ()),
            [],
        )

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_build_download_archive_keeps_original_image_bytes_by_default(self, mock_get):
        raw_content = b"raw-webp-content"
        response = Mock()
        response.headers = {"Content-Type": "image/webp", "Content-Length": str(len(raw_content))}
        response.url = "https://sns-img.xhscdn.com/spectrum/a.webp"
        response.iter_content.return_value = [raw_content]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        archive = build_download_archive(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
            base_filename="base",
        )

        with ZipFile(BytesIO(archive["content"])) as zip_file:
            self.assertEqual(zip_file.read("base_1.webp"), raw_content)

        self.assertEqual(archive["saved_count"], 1)
        self.assertEqual(archive["failed_count"], 0)

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_build_download_archive_returns_zip(self, mock_get):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.iter_content.return_value = [make_image_bytes()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        archive = build_download_archive(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
            base_filename="base",
            save_format="jpg",
        )

        with ZipFile(BytesIO(archive["content"])) as zip_file:
            self.assertIn("base_1.jpg", zip_file.namelist())

        self.assertEqual(archive["saved_count"], 1)
        self.assertEqual(archive["failed_count"], 0)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_build_download_archive_accepts_signed_ytdlp_video_url(
        self,
        mock_get,
        _mock_getaddrinfo,
    ):
        media_url = "https://rr1---sn.googlevideo.com/videoplayback?itag=18"
        content = b"video-bytes"
        response = FakeStreamingResponse(
            content,
            url=media_url,
            content_type="video/mp4",
        )
        response.status_code = 200
        mock_get.return_value = response
        result = GenericThirdPartyAdapter(
            provider=Mock(parse=Mock(side_effect=RuntimeError("kukutool failed"))),
            ytdlp_provider=Mock(parse=Mock(return_value={
                "title": "yt archive demo",
                "formats": [
                    {
                        "url": media_url,
                        "protocol": "https",
                        "ext": "mp4",
                        "vcodec": "avc1.4d401f",
                        "acodec": "mp4a.40.2",
                        "height": 360,
                        "filesize": len(content),
                    }
                ],
            })),
        ).parse_url("https://www.youtube.com/watch?v=demo")
        item = result.video_assets[0].to_download_item()

        archive = build_download_archive(
            media_items=[item],
            base_filename=result.base_filename,
        )

        with ZipFile(BytesIO(archive["content"])) as zip_file:
            self.assertEqual(zip_file.read("yt archive demo_1.mp4"), content)
        self.assertEqual(archive["saved_count"], 1)
        self.assertEqual(archive["failed_count"], 0)

    def test_resolve_server_save_folder_rejects_absolute_when_disabled(self):
        absolute_path = Path(Path.cwd().anchor) / "tmp" / "xhs"
        with self.assertRaisesMessage(Exception, "当前环境不允许保存到绝对路径"):
            resolve_server_save_folder(str(absolute_path), allow_absolute=False)

    def test_resolve_server_save_folder_keeps_relative_inside_media_root(self):
        with TemporaryDirectory() as tmp_dir:
            with override_settings(MEDIA_ROOT=Path(tmp_dir) / "media", XHS_DEFAULT_SERVER_SAVE_DIR="xhs"):
                folder = resolve_server_save_folder("xhs", allow_absolute=False)

        self.assertEqual(folder, (Path(tmp_dir) / "media" / "xhs").resolve())

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_generic_provider_download_allows_public_url(
        self,
        mock_get,
    ):
        content = make_image_bytes()
        media_url = "https://scontent.cdninstagram.com/image.jpg"
        response = FakeStreamingResponse(
            content,
            url=media_url,
            content_type="image/jpeg",
        )
        response.status_code = 200
        mock_get.return_value = response
        result = GenericThirdPartyAdapter(
            runner=Mock(return_value={
                "title": "image post",
                "pics": [media_url],
            })
        ).parse_url("https://www.instagram.com/p/example/")
        item = result.image_assets[0].to_download_item()

        with patch(
            "xhs_download.services.common.socket.getaddrinfo",
            return_value=[(None, None, None, "", ("93.184.216.34", 443))],
        ):
            temp_result = fetch_item_to_temp_path(
                item,
                timeout=1,
                max_bytes=MAX_IMAGE_BYTES,
                expected_type="image",
            )

        try:
            self.assertEqual(Path(temp_result["path"]).read_bytes(), content)
            self.assertEqual(temp_result["final_url"], media_url)
        finally:
            Path(temp_result["path"]).unlink(missing_ok=True)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_generic_provider_video_cover_download_allows_public_url(
        self,
        mock_get,
    ):
        cover_bytes = make_image_bytes()
        video_url = "https://scontent.cdninstagram.com/video.mp4"
        cover_url = "https://scontent.cdninstagram.com/cover.jpg"
        response = FakeStreamingResponse(
            cover_bytes,
            url=cover_url,
            content_type="image/jpeg",
        )
        response.status_code = 200
        mock_get.return_value = response
        result = GenericThirdPartyAdapter(
            runner=Mock(return_value={
                "title": "video post",
                "cover": cover_url,
                "videos": [video_url],
            })
        ).parse_url("https://www.instagram.com/p/video-cover-example/")
        item = result.video_assets[0].to_download_item()

        with patch(
            "xhs_download.services.common.socket.getaddrinfo",
            return_value=[(None, None, None, "", ("93.184.216.34", 443))],
        ):
            archive = build_download_archive_file(
                [item],
                base_filename="base",
                video_save_mode="cover",
            )

        try:
            with ZipFile(archive["file"]) as zip_file:
                self.assertEqual(zip_file.read("base_1_cover.jpg"), cover_bytes)
            self.assertEqual(archive["saved_count"], 1)
            mock_get.assert_called_once()
            self.assertEqual(mock_get.call_args.args[0], cover_url)
        finally:
            archive["file"].close()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_signed_generic_provider_url_override_does_not_reuse_original_manifest(self):
        media_url = "https://scontent.cdninstagram.com/image.jpg"
        result = GenericThirdPartyAdapter(
            runner=Mock(return_value={
                "title": "image post",
                "pics": [media_url],
            })
        ).parse_url("https://www.instagram.com/p/example/")
        item = result.image_assets[0].to_download_item()

        with self.assertRaises(XhsSecurityError):
            fetch_item_to_temp_path(
                item,
                url="https://evil.example.com/image.jpg",
                timeout=1,
                max_bytes=MAX_IMAGE_BYTES,
                expected_type="image",
            )

    def test_resolve_server_save_folder_rejects_relative_escape(self):
        with TemporaryDirectory() as tmp_dir:
            with override_settings(MEDIA_ROOT=Path(tmp_dir) / "media", XHS_DEFAULT_SERVER_SAVE_DIR="xhs"):
                with self.assertRaisesMessage(Exception, "保存路径不能跳出 MEDIA_ROOT"):
                    resolve_server_save_folder("../outside", allow_absolute=False)

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_save_media_items_to_server_writes_file_when_allowed(self, mock_get):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.iter_content.return_value = [make_image_bytes()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        with TemporaryDirectory() as tmp_dir:
            result = save_media_items_to_server(
                media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
                base_filename="base",
                save_dir=str(Path(tmp_dir) / "xhs"),
                save_format="jpg",
                allow_absolute=True,
            )

            self.assertEqual(result["saved_count"], 1)
            self.assertTrue(Path(result["saved_files"][0]).exists())


class XhsDownloadViewTests(TestCase):
    def setUp(self):
        cache.clear()
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.youtube_merge_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_platforms = (
            "youtube,kuaishou,tiktok,flickr,imgur"
        )
        setting.save(
            update_fields=[
                "third_party_mode",
                "youtube_merge_mode",
                "third_party_allowed_public_platforms",
                "updated_at",
            ]
        )

    def test_home_route_renders_download_page(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "星空的高清下载小站")
        self.assertContains(response, "theme-home")
        self.assertContains(response, "nav-settings-btn")
        self.assertContains(response, "css/app.css?v=")
        self.assertContains(response, "js/xhs_download.js?v=")
        self.assertContains(response, "data-download-task-url")

    @override_settings(DEBUG=False, XHS_ALLOW_SERVER_SAVE=True, XHS_ENABLE_SERVER_SAVE_MODE=False)
    def test_production_page_hides_server_save_mode(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="server"')
        self.assertNotContains(response, "开发保存模式：保存到服务端路径")

    @override_settings(DEBUG=True, XHS_ALLOW_SERVER_SAVE=True, XHS_ENABLE_SERVER_SAVE_MODE=True)
    def test_debug_page_can_show_server_save_mode(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="server"')

    def test_download_page_marks_provider_notice_as_result_content(self):
        html = render_to_string(
            "xhs_download.html",
            {
                "theme_class": "theme-home",
                "provider_notice": "本次由 yt-dlp 第三方解析。",
                "image_items": [],
                "video_items": [],
                "download_async_tasks_enabled": False,
            },
        )

        self.assertIn("xhs-download-page theme-home has-results", html)
        self.assertIn("本次由 yt-dlp 第三方解析。", html)

    @override_settings(
        PREVIEW_RATE_LIMIT_PER_MINUTE=1,
        PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=0,
        PREVIEW_DAILY_BYTES_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_applies_ip_rate_limit(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(make_image_bytes())
        params = {
            "u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg"),
        }

        first = Client().get("/media/proxy-image/", params)
        second = Client().get("/media/proxy-image/", params)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertEqual(mock_fetch.call_count, 1)

    @override_settings(
        PREVIEW_RATE_LIMIT_PER_MINUTE=0,
        PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=0,
        PREVIEW_DAILY_BYTES_LIMIT_PER_IP=1,
        PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_enforces_daily_byte_limit(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(make_image_bytes())

        response = Client().get(
            "/media/proxy-image/",
            {"u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg")},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            get_daily_total(
                "preview-bytes-ip",
                get_client_identity(response.wsgi_request),
            ),
            len(make_image_bytes()),
        )

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_allows_signed_generic_provider_public_url(self, mock_fetch):
        media_url = "https://p1.a.yximgs.com/cover.jpg"
        asset_id = "third_party:kuaishou:abc123:video:1:cover"
        provider_cache_key = "third_party:kukutool_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="kukutool_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        mock_fetch.return_value = FakeStreamingResponse(
            make_image_bytes(),
            url=media_url,
            content_type="image/jpeg",
        )

        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url(media_url),
                "asset_id": asset_id,
                "provider": "kukutool_generic",
                "provider_resource_id": provider_resource_id,
                "provider_cache_key": provider_cache_key,
                "provider_manifest_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertTrue(mock_fetch.call_args.kwargs["provider_signed"])

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_allows_signed_ytdlp_youtube_cover_url(self, mock_fetch):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1:cover"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        mock_fetch.return_value = FakeStreamingResponse(
            make_image_bytes(),
            url=media_url,
            content_type="image/jpeg",
        )

        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url(media_url),
                "asset_id": asset_id,
                "provider": "yt_dlp_generic",
                "provider_resource_id": provider_resource_id,
                "provider_cache_key": provider_cache_key,
                "provider_manifest_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertTrue(mock_fetch.call_args.kwargs["provider_signed"])

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_rejects_tampered_signed_provider_url(self, mock_fetch):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1:cover"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url("https://evil.example.com/cover.jpg"),
                "asset_id": asset_id,
                "provider": "yt_dlp_generic",
                "provider_resource_id": provider_resource_id,
                "provider_cache_key": provider_cache_key,
                "provider_manifest_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        mock_fetch.assert_not_called()

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_invalidates_signed_external_payload_cache_on_non_image_response(
        self,
        mock_fetch,
    ):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1:cover"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        cache_key = external_payload_cache_key("yt_dlp_generic", provider_resource_id)
        failure_cache_key = external_failure_cache_key(
            "yt_dlp_generic",
            provider_resource_id,
        )
        cache.set(cache_key, {"title": "stale ytdlp payload"}, timeout=60)
        cache.set(failure_cache_key, "TimeoutError", timeout=60)
        mock_fetch.return_value = FakeStreamingResponse(
            b"<html>blocked</html>",
            url=media_url,
            content_type="text/html",
        )

        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url(media_url),
                "asset_id": asset_id,
                "provider": "yt_dlp_generic",
                "provider_resource_id": provider_resource_id,
                "provider_cache_key": provider_cache_key,
                "provider_manifest_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIsNone(cache.get(cache_key))
        self.assertIsNone(cache.get(failure_cache_key))

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_invalidates_signed_external_payload_cache_on_fetch_error(
        self,
        mock_fetch,
    ):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1:cover"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        cache_key = external_payload_cache_key("yt_dlp_generic", provider_resource_id)
        failure_cache_key = external_failure_cache_key(
            "yt_dlp_generic",
            provider_resource_id,
        )
        cache.set(cache_key, {"title": "stale ytdlp payload"}, timeout=60)
        cache.set(failure_cache_key, "TimeoutError", timeout=60)
        mock_fetch.side_effect = XhsDownloadError("HTTP 403")

        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url(media_url),
                "asset_id": asset_id,
                "provider": "yt_dlp_generic",
                "provider_resource_id": provider_resource_id,
                "provider_cache_key": provider_cache_key,
                "provider_manifest_token": token,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIsNone(cache.get(cache_key))
        self.assertIsNone(cache.get(failure_cache_key))

    def test_provider_preview_query_supports_signed_ytdlp_cover(self):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset = MediaAsset(
            media_type="video",
            url="https://rr1---sn.googlevideo.com/videoplayback?itag=22",
            platform="third_party",
            cover_url=media_url,
            provider="yt_dlp_generic",
            asset_id="third_party:youtube:abc123:video:1",
            provider_resource_id="abc123",
            provider_cache_key="third_party:yt_dlp_generic:abc123",
        )

        query = build_provider_preview_query(asset, media_url, "cover")
        params = {key: values[0] for key, values in parse_qs(query).items()}

        self.assertEqual(params["provider"], "yt_dlp_generic")
        self.assertEqual(params["type"], "image")
        self.assertEqual(params["asset_id"], "third_party:youtube:abc123:video:1:cover")
        self.assertTrue(
            provider_manifest_allows_public_download({
                **params,
                "url": media_url,
            })
        )

    def test_provider_preview_query_supports_signed_ytdlp_merge_cover(self):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset = MediaAsset(
            media_type="video",
            url="https://www.youtube.com/watch?v=demo",
            platform="third_party",
            cover_url=media_url,
            provider="yt_dlp_merge",
            asset_id="yt_dlp_merge:youtube:abc123:video:1:h1080",
            provider_resource_id="abc123",
            provider_cache_key="yt_dlp_merge:abc123:h1080",
        )

        query = build_provider_preview_query(asset, media_url, "cover")
        params = {key: values[0] for key, values in parse_qs(query).items()}

        self.assertEqual(params["provider"], "yt_dlp_merge")
        self.assertEqual(params["type"], "image")
        self.assertEqual(
            params["asset_id"],
            "yt_dlp_merge:youtube:abc123:video:1:h1080:cover",
        )
        self.assertTrue(
            provider_manifest_allows_public_download({
                **params,
                "url": media_url,
            })
        )
        self.assertFalse(
            provider_manifest_allows_public_download({
                **params,
                "url": "https://evil.example.com/cover.jpg",
            })
        )

    def test_provider_manifest_allows_signed_douyin_kukutool_video(self):
        media_url = "https://v26-default.365yg.com/path/video/tos/cn/source.mp4"
        item = make_kukutool_download_item(media_url)

        self.assertTrue(provider_manifest_allows_public_download(item))
        self.assertFalse(
            provider_manifest_allows_public_download({
                **item,
                "url": "https://evil.example.com/source.mp4",
            })
        )

    def test_provider_preview_query_marks_image_asset_type(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset = MediaAsset(
            media_type="image",
            url=media_url,
            platform="third_party",
            provider="gallery_dl_generic",
            asset_id="third_party:imgur:abc123:image:1",
            provider_resource_id="abc123",
            provider_cache_key="third_party:gallery_dl_generic:abc123",
        )

        query = build_provider_preview_query(asset, media_url)
        params = {key: values[0] for key, values in parse_qs(query).items()}

        self.assertEqual(params["type"], "image")
        self.assertEqual(params["asset_id"], "third_party:imgur:abc123:image:1")
        self.assertTrue(
            provider_manifest_allows_public_download({
                **params,
                "url": media_url,
            })
        )

    def test_provider_preview_query_rejects_disallowed_provider_cover_host(self):
        asset = MediaAsset(
            media_type="video",
            url="https://rr1---sn.googlevideo.com/videoplayback?itag=22",
            platform="third_party",
            provider="yt_dlp_generic",
            asset_id="third_party:youtube:abc123:video:1",
            provider_resource_id="abc123",
            provider_cache_key="third_party:yt_dlp_generic:abc123",
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            query = build_provider_preview_query(
                asset,
                "https://evil.example.com/cover.jpg",
                "cover",
            )

        self.assertEqual(query, "")
        mock_warning.assert_called_once()

    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_does_not_allow_unsigned_youtube_cover_url(self, mock_fetch):
        response = Client().get(
            "/media/proxy-image/",
            {
                "u": encode_url(
                    "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        mock_fetch.assert_not_called()

    def test_provider_manifest_rechecks_third_party_media_host(self):
        media_url = "https://evil.example.com/image.jpg"
        asset_id = "third_party:instagram:abc123:image:1"
        provider_cache_key = "third_party:kukutool_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="kukutool_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "kukutool_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_allows_signed_ytdlp_public_url(self):
        media_url = "https://rr1---sn.googlevideo.com/videoplayback?itag=22"
        asset_id = "third_party:youtube:abc123:video:1"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        self.assertTrue(
            provider_manifest_allows_public_download(
                {
                    "url": media_url,
                    "asset_id": asset_id,
                    "provider": "yt_dlp_generic",
                    "provider_resource_id": provider_resource_id,
                    "provider_cache_key": provider_cache_key,
                    "provider_manifest_token": token,
                }
            )
        )

    def test_provider_manifest_rejects_youtube_video_asset_on_cover_host(self):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
            media_type="video",
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "type": "video",
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "yt_dlp_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_allows_signed_gallerydl_public_url(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset_id = "third_party:imgur:abc123:image:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        self.assertTrue(
            provider_manifest_allows_public_download(
                {
                    "url": media_url,
                    "asset_id": asset_id,
                    "provider": "gallery_dl_generic",
                    "provider_resource_id": provider_resource_id,
                    "provider_cache_key": provider_cache_key,
                    "provider_manifest_token": token,
                }
            )
        )

    def test_provider_manifest_allows_signed_gallerydl_video_url(self):
        media_url = "https://i.imgur.com/demo.mp4"
        asset_id = "third_party:imgur:abc123:video:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        self.assertTrue(
            provider_manifest_allows_public_download(
                {
                    "url": media_url,
                    "asset_id": asset_id,
                    "provider": "gallery_dl_generic",
                    "provider_resource_id": provider_resource_id,
                    "provider_cache_key": provider_cache_key,
                    "provider_manifest_token": token,
                }
            )
        )

    def test_provider_manifest_rejects_third_party_type_mismatch(self):
        media_url = "https://i.imgur.com/demo.mp4"
        asset_id = "third_party:imgur:abc123:video:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "type": "image",
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_rejects_third_party_resource_id_mismatch(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset_id = "third_party:imgur:abc123:image:1"
        provider_cache_key = "third_party:gallery_dl_generic:other123"
        provider_resource_id = "other123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "type": "image",
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_rejects_third_party_cache_key_mismatch(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset_id = "third_party:imgur:abc123:image:1"
        provider_cache_key = "third_party:gallery_dl_generic:other123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "type": "image",
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_binds_type_when_token_declares_it(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset_id = "third_party:imgur:abc123:image:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
            media_type="image",
        )
        base_item = {
            "url": media_url,
            "asset_id": asset_id,
            "provider": "gallery_dl_generic",
            "provider_resource_id": provider_resource_id,
            "provider_cache_key": provider_cache_key,
            "provider_manifest_token": token,
        }

        self.assertTrue(
            provider_manifest_allows_public_download({
                **base_item,
                "type": "image",
            })
        )
        self.assertFalse(provider_manifest_allows_public_download(base_item))
        self.assertFalse(
            provider_manifest_allows_public_download({
                **base_item,
                "type": "video",
            })
        )

    def test_provider_manifest_allows_third_party_video_cover_as_image(self):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:video:1:cover"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        self.assertTrue(
            provider_manifest_allows_public_download(
                {
                    "type": "image",
                    "url": media_url,
                    "asset_id": asset_id,
                    "provider": "yt_dlp_generic",
                    "provider_resource_id": provider_resource_id,
                    "provider_cache_key": provider_cache_key,
                    "provider_manifest_token": token,
                }
            )
        )

    def test_provider_manifest_rejects_unknown_third_party_asset_suffix(self):
        media_url = "https://i.imgur.com/demo.jpg"
        asset_id = "third_party:imgur:abc123:image:1:original"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "type": "image",
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_rejects_disallowed_provider_for_platform(self):
        media_url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        asset_id = "third_party:youtube:abc123:image:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    def test_provider_manifest_rejects_disallowed_media_type_for_platform(self):
        media_url = "https://live.staticflickr.com/demo.mp4"
        asset_id = "third_party:flickr:abc123:video:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )

        with patch("xhs_download.services.downloads.logger.warning") as mock_warning:
            self.assertFalse(
                provider_manifest_allows_public_download(
                    {
                        "url": media_url,
                        "asset_id": asset_id,
                        "provider": "gallery_dl_generic",
                        "provider_resource_id": provider_resource_id,
                        "provider_cache_key": provider_cache_key,
                        "provider_manifest_token": token,
                    }
                )
            )

        mock_warning.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic."
        "YtDlpGenericProvider.parse"
    )
    @patch(
        "xhs_download.services.platforms.kukutool_generic."
        "KukutoolGenericProvider.parse"
    )
    def test_results_ajax_uses_ytdlp_fallback_for_generic_video(
        self,
        mock_kukutool_parse,
        mock_ytdlp_parse,
    ):
        mock_kukutool_parse.side_effect = RuntimeError("kukutool failed")
        mock_ytdlp_parse.return_value = {
            "title": "tiktok page demo",
            "thumbnail": "https://p16.tiktokcdn.com/cover.jpg",
            "formats": [
                {
                    "url": "https://v16.tiktokcdn.com/video/demo.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "vcodec": "avc1.4d401f",
                    "acodec": "mp4a.40.2",
                    "height": 720,
                    "filesize": 300,
                    "format_note": "720p",
                }
            ],
        }

        response = Client().get(
            "/results/",
            {"q": "https://www.tiktok.com/@demo/video/123"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["has_results"])
        self.assertIn("result-card", payload["html"])
        self.assertIn("TikTok", payload["html"])
        self.assertIn("yt-dlp", payload["html"])
        self.assertIn('data-provider="yt_dlp_generic"', payload["html"])
        mock_kukutool_parse.assert_called_once()
        mock_ytdlp_parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=True,
    )
    @patch(
        "xhs_download.services.platforms.gallerydl_generic."
        "GalleryDlGenericProvider.parse"
    )
    def test_results_ajax_uses_gallerydl_for_generic_image_platform(
        self,
        mock_gallerydl_parse,
    ):
        mock_gallerydl_parse.return_value = [
            [
                2,
                "https://live.staticflickr.com/demo/photo.jpg",
                {
                    "title": "flickr page demo",
                    "filename": "photo",
                    "extension": "jpg",
                    "filesize": 1234,
                },
            ]
        ]

        response = Client().get(
            "/results/",
            {"q": "https://www.flickr.com/photos/nasa2explore/53566933692"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["has_results"])
        self.assertIn("result-card", payload["html"])
        self.assertIn("Flickr", payload["html"])
        self.assertIn("gallery-dl", payload["html"])
        self.assertIn('data-provider="gallery_dl_generic"', payload["html"])
        self.assertIn("provider_manifest_token", payload["html"])
        mock_gallerydl_parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.probe_media_size")
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_probe_signed_third_party_video_size(
        self,
        mock_parse,
        mock_probe_size,
    ):
        media_url = "https://hwmov.a.kwimgs.com/demo.mp4"
        cover_url = "https://p1.a.yximgs.com/cover.jpg"
        asset_id = "third_party:kuaishou:abc123:video:1"
        provider_cache_key = "third_party:kukutool_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="kukutool_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="kuaishou",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=media_url,
                    platform="third_party",
                    cover_url=cover_url,
                    cover_preview_url=cover_url,
                    provider="kukutool_generic",
                    asset_id=asset_id,
                    provider_resource_id=provider_resource_id,
                    provider_cache_key=provider_cache_key,
                    provider_manifest_token=token,
                ),
            ),
            metadata={"source_platform": "kuaishou"},
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"},
        )

        self.assertEqual(response.status_code, 200)
        mock_probe_size.assert_not_called()
        self.assertContains(response, 'data-provider="kukutool_generic"')
        self.assertContains(response, "provider_manifest_token")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.probe_media_size")
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_probe_signed_ytdlp_video_size(
        self,
        mock_parse,
        mock_probe_size,
    ):
        media_url = "https://rr1---sn.googlevideo.com/videoplayback?itag=22"
        asset_id = "third_party:youtube:abc123:video:1"
        provider_cache_key = "third_party:yt_dlp_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="yt_dlp_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=media_url,
                    platform="third_party",
                    provider="yt_dlp_generic",
                    asset_id=asset_id,
                    provider_resource_id=provider_resource_id,
                    provider_cache_key=provider_cache_key,
                    provider_manifest_token=token,
                ),
            ),
            metadata={"source_platform": "youtube"},
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )

        self.assertEqual(response.status_code, 200)
        mock_probe_size.assert_not_called()
        self.assertContains(response, 'data-provider="yt_dlp_generic"')
        self.assertContains(response, "data-provider-manifest-token")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.probe_media_size")
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_probe_signed_gallerydl_video_size(
        self,
        mock_parse,
        mock_probe_size,
    ):
        media_url = "https://i.imgur.com/demo.mp4"
        asset_id = "third_party:imgur:abc123:video:1"
        provider_cache_key = "third_party:gallery_dl_generic:abc123"
        provider_resource_id = "abc123"
        token = build_provider_manifest_token(
            asset_id=asset_id,
            provider="gallery_dl_generic",
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=media_url,
        )
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="imgur",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=media_url,
                    platform="third_party",
                    provider="gallery_dl_generic",
                    asset_id=asset_id,
                    provider_resource_id=provider_resource_id,
                    provider_cache_key=provider_cache_key,
                    provider_manifest_token=token,
                ),
            ),
            metadata={"source_platform": "imgur"},
        )

        response = Client().get(
            "/results/",
            {"q": "https://imgur.com/gallery/demo"},
        )

        self.assertEqual(response.status_code, 200)
        mock_probe_size.assert_not_called()
        self.assertContains(response, 'data-provider="gallery_dl_generic"')
        self.assertContains(response, "data-provider-manifest-token")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_uses_third_party_source_platform_notice(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="kuaishou",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://hwmov.a.kwimgs.com/demo.mp4",
                    platform="third_party",
                    provider="kukutool_generic",
                ),
            ),
            metadata={
                "source_platform": "kuaishou",
                "source_platform_name": "快手",
                "provider_label": "KuKuTool",
            },
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "快手")
        self.assertContains(response, "KuKuTool 第三方解析")
        self.assertNotContains(response, ">third_party<")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health", return_value={"available": True})
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_show_douyin_ultra_notice_for_youtube(
        self,
        mock_parse,
        _mock_health,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(),
            metadata={
                "source_platform": "youtube",
                "source_platform_name": "YouTube",
                "selected_provider": "yt_dlp_merge",
            },
        )

        response = Client().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "ultra_hd": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=True,
            client_country_code="CN",
        )
        self.assertContains(response, "yt-dlp 高清合并")
        self.assertNotContains(response, "超高清 Provider 目前仅用于抖音视频")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health", return_value={"available": True})
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_show_douyin_ultra_notice_for_xhs(
        self,
        mock_parse,
        _mock_health,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        mock_parse.return_value = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="xhs-demo",
            assets=(
                MediaAsset(
                    media_type="image",
                    url="https://ci.xiaohongshu.com/a.jpg",
                    platform="xiaohongshu",
                ),
            ),
        )

        response = Client().get(
            "/results/",
            {
                "q": "https://www.xiaohongshu.com/explore/demo",
                "ultra_hd": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "小红书")
        self.assertNotContains(response, "超高清 Provider 目前仅用于抖音视频")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health", return_value={"available": True})
    @patch("xhs_download.views.parse_media_page")
    def test_results_does_not_show_douyin_ultra_notice_for_bilibili(
        self,
        mock_parse,
        _mock_health,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        mock_parse.return_value = ParseResult(
            platform="bilibili",
            post_type="video",
            base_filename="bilibili-demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4",
                    platform="bilibili",
                    size_bytes=1024 * 1024,
                ),
            ),
        )

        response = Client().get(
            "/results/",
            {
                "q": "https://www.bilibili.com/video/BV1xx411c7mD/",
                "ultra_hd": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "哔哩哔哩")
        self.assertNotContains(response, "超高清 Provider 目前仅用于抖音视频")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=False,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_blocks_third_party_when_public_access_disabled(self, mock_parse):
        response = Client().get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )

        self.assertEqual(response.status_code, 403)
        mock_parse.assert_not_called()
        self.assertContains(response, "第三方通用解析暂未开放", status_code=403)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=True,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_blocks_third_party_for_anonymous_when_staff_only_enabled(
        self,
        mock_parse,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=demo"},
        )
        request.user = Mock(is_active=False, is_staff=False)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 403)
        self.assertEqual(context["error_message"], "第三方通用解析暂未向该用户开放。")
        self.assertFalse(third_party_provider_is_available(request))
        mock_parse.assert_not_called()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=True,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_allows_third_party_for_staff_when_staff_only_enabled(
        self,
        mock_parse,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["third_party_mode", "updated_at"])
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="image",
            base_filename="youtube",
            assets=(),
            metadata={"source_platform": "youtube"},
        )
        request = RequestFactory().get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=demo"},
        )
        request.user = Mock(is_active=True, is_staff=True)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertTrue(third_party_provider_is_available(request))
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=False,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_rate_limits_third_party_provider_requests(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="kuaishou",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://hwmov.a.kwimgs.com/demo.mp4",
                    platform="third_party",
                    provider="kukutool_generic",
                ),
            ),
            metadata={"source_platform": "kuaishou", "source_platform_name": "快手"},
        )

        first = Client().get(
            "/results/",
            {"q": "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"},
        )
        second = Client().get(
            "/results/",
            {"q": "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertEqual(mock_parse.call_count, 1)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_RATE_LIMIT_PER_MINUTE=0,
        THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=1,
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_global_rate_limits_third_party_provider_requests(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://rr1---sn.googlevideo.com/videoplayback?demo=1",
                    platform="third_party",
                    provider="yt_dlp_generic",
                ),
            ),
            metadata={"source_platform": "youtube", "source_platform_name": "YouTube"},
        )

        first = Client(REMOTE_ADDR="192.0.2.10").get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=demo"},
        )
        second = Client(REMOTE_ADDR="192.0.2.11").get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=demo"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertContains(second, "第三方解析当前排队请求较多", status_code=429)
        self.assertIn("Retry-After", second)
        self.assertEqual(mock_parse.call_count, 1)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch(
        "xhs_download.views.parse_media_page",
        side_effect=XhsProviderTimeoutError(
            "第三方解析正在后台处理中，请 3-5 秒后重试同一链接。"
        ),
    )
    def test_results_shows_third_party_background_processing_without_failure_prefix(
        self,
        mock_parse,
    ):
        response = Client().get(
            "/results/",
            {"q": "https://www.kuaishou.com/short-video/3xvxtmcc2r22c6i"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "第三方解析正在后台处理中")
        self.assertNotContains(response, "解析失败：第三方解析正在后台处理中")
        mock_parse.assert_called_once()

    @patch(
        "xhs_download.views.check_preview_request_limits",
        side_effect=RuntimeError("redis unavailable"),
    )
    def test_preview_proxy_fails_closed_when_cache_is_unavailable(self, _mock_limits):
        with patch("xhs_download.views.logger.exception") as mock_log_exception:
            response = Client().get(
                "/media/proxy-image/",
                {"u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg")},
            )

        self.assertEqual(response.status_code, 503)
        mock_log_exception.assert_called_once_with("Preview rate-limit cache failed")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        TIKHUB_API_KEY="secret",
    )
    @patch(
        "xhs_download.views.get_kukutool_provider_health",
        return_value={"available": True},
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_passes_ultra_hd_request_flag(self, mock_parse, _mock_health):
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get("/results/", {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"})

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.douyin.com/video/123",
            prefer_ultra_hd=True,
            client_country_code="CN",
        )
        self.assertContains(response, "本次优先使用 KuKuTool 超高清上游")
        self.assertContains(response, 'id="ultra-hd-request-input"')
        self.assertContains(response, 'value="1"')

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health")
    def test_page_does_not_probe_ultra_hd_health_when_provider_is_disabled(
        self,
        mock_health,
    ):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="ultra-hd-setting"')
        self.assertContains(response, "当前环境未启用 KuKuTool 超高清上游")
        mock_health.assert_not_called()

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=False,
    )
    @patch("xhs_download.views.get_kukutool_provider_health")
    def test_page_does_not_probe_ultra_hd_health_when_kukutool_is_disabled(
        self,
        mock_health,
    ):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="ultra-hd-setting"')
        self.assertContains(response, "当前环境未启用 KuKuTool 超高清上游")
        mock_health.assert_not_called()

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        TIKHUB_API_KEY="secret",
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_defaults_to_local_provider_without_ultra_hd_flag(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get("/results/", {"q": "https://www.douyin.com/video/123"})

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with("https://www.douyin.com/video/123", prefer_ultra_hd=False)
        self.assertContains(response, "feature-card", count=4)
        self.assertContains(response, "无需登录解析去水印，支持视频、图片、批量无水印保存")
        self.assertContains(response, 'placeholder="请将复制的链接粘贴到这里"')
        self.assertContains(response, "paste-link-icon-btn")
        self.assertContains(response, 'id="ultra-hd-request-input"')
        self.assertContains(response, 'id="ultra-hd-setting"')
        self.assertContains(response, "解析质量")
        self.assertContains(response, "默认关闭，关闭时只使用本地解析")
        self.assertNotContains(response, "hero-provider-row")
        self.assertContains(response, "无需登录/注册")
        self.assertContains(response, "打开页面即可解析链接，无使用限制。")
        self.assertContains(response, "原始文件保存")
        self.assertContains(response, "去除水印，保持原始画质。")
        self.assertContains(response, 'action="/results/"')
        self.assertContains(response, 'href="/xhs/"')
        self.assertContains(response, "小红书")
        self.assertContains(response, 'href="/tiktok/"')
        self.assertContains(response, "抖音")
        self.assertContains(response, 'href="/bilibili/"')
        self.assertContains(response, "哔哩哔哩")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True,
        YOUTUBE_MERGE_STAFF_ONLY=False,
        YTDLP_BINARY="/usr/bin/python -m yt_dlp",
        YOUTUBE_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
    )
    @patch(
        "xhs_download.services.youtube_merge.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "ok",
            "error": "",
        },
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "2026.03.17",
            "error": "",
        },
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    @patch("xhs_download.views.parse_media_page")
    def test_results_passes_youtube_merge_request_flag(
        self,
        mock_parse,
        _mock_access,
        _mock_exists,
        _mock_ytdlp_probe,
        _mock_ffmpeg_probe,
    ):
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(),
            metadata={"source_platform": "youtube"},
        )

        response = Client().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=False,
            prefer_youtube_merge=True,
        )
        self.assertContains(response, 'id="youtube-merge-request-input"')
        self.assertContains(response, 'value="1"')

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.parse_media_page")
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_results_passes_youtube_merge_for_staff_when_staff_only_enabled(
        self,
        mock_health,
        mock_parse,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["youtube_merge_mode", "updated_at"])
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": True,
            "staff_only": True,
        }
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(),
            metadata={
                "source_platform": "youtube",
                "selected_provider": "youtube_merge",
            },
        )
        request = RequestFactory().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )
        request.user = Mock(is_active=True, is_staff=True)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertTrue(context["prefer_youtube_merge"])
        self.assertEqual(context["provider_notice"], "本次使用 YouTube 最高画质任务；下载时将在服务端合并音频和视频，文件可能更大。")
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=False,
            prefer_youtube_merge=True,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.parse_media_page")
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_results_does_not_pass_youtube_merge_for_anonymous_when_staff_only_enabled(
        self,
        mock_health,
        mock_parse,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["youtube_merge_mode", "updated_at"])
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": True,
            "staff_only": True,
        }
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(),
            metadata={
                "source_platform": "youtube",
                "selected_provider": "yt_dlp_merge",
            },
        )
        request = RequestFactory().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )
        request.user = Mock(is_active=False, is_staff=False)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertFalse(context["prefer_youtube_merge"])
        self.assertEqual(context["provider_notice"], "本次使用 yt-dlp 高清合并；默认优先 1080p，下载时将在服务端合并音频和视频。")
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=False,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
    )
    @patch("xhs_download.views.parse_media_page")
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_results_does_not_pass_youtube_merge_for_staff_when_public_access_disabled(
        self,
        mock_health,
        mock_parse,
    ):
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": False,
            "staff_only": True,
        }
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="video",
            base_filename="youtube",
            assets=(),
            metadata={
                "source_platform": "youtube",
                "selected_provider": "yt_dlp_merge",
            },
        )
        request = RequestFactory().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )
        request.user = Mock(is_active=True, is_staff=True)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertFalse(context["prefer_youtube_merge"])
        mock_parse.assert_called_once_with(
            "https://www.youtube.com/watch?v=demo",
            prefer_ultra_hd=False,
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.logger.info")
    @patch("xhs_download.views.parse_media_page")
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_results_logs_parse_completion_with_provider_timing(
        self,
        mock_health,
        mock_parse,
        mock_log_info,
    ):
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": True,
            "staff_only": False,
        }
        mock_parse.return_value = ParseResult(
            platform="third_party",
            post_type="image",
            base_filename="youtube",
            assets=(
                MediaAsset(
                    media_type="image",
                    url="https://i.ytimg.com/vi/demo/hqdefault.jpg",
                    platform="third_party",
                ),
            ),
            metadata={
                "source_platform": "youtube",
                "selected_provider": "youtube_merge",
                "selected_provider_duration_ms": 120,
                "provider_total_duration_ms": 180,
                "provider_fallback_count": 0,
            },
        )
        request = RequestFactory().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )
        request.user = Mock(is_active=True, is_staff=False)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertEqual(len(context["image_items"]), 1)
        event, = mock_log_info.call_args.args
        extra = mock_log_info.call_args.kwargs["extra"]
        self.assertEqual(event, "media_parse_completed")
        self.assertEqual(extra["query_host"], "www.youtube.com")
        self.assertTrue(extra["requested_youtube_merge"])
        self.assertTrue(extra["prefer_youtube_merge"])
        self.assertEqual(extra["platform"], "third_party")
        self.assertEqual(extra["source_platform"], "youtube")
        self.assertEqual(extra["selected_provider"], "youtube_merge")
        self.assertEqual(extra["selected_provider_duration_ms"], 120)
        self.assertEqual(extra["provider_total_duration_ms"], 180)
        self.assertEqual(extra["image_count"], 1)
        self.assertEqual(extra["video_count"], 0)
        self.assertGreaterEqual(extra["duration_ms"], 0)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
    )
    @patch("xhs_download.views.logger.info")
    @patch("xhs_download.views.parse_media_page")
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_results_logs_parse_failure_with_flags_and_error_type(
        self,
        mock_health,
        mock_parse,
        mock_log_info,
    ):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["youtube_merge_mode", "updated_at"])
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": True,
            "staff_only": True,
        }
        mock_parse.side_effect = XhsParseError("provider exploded")
        request = RequestFactory().get(
            "/results/",
            {
                "q": "https://www.youtube.com/watch?v=demo",
                "youtube_merge": "1",
            },
        )
        request.user = Mock(is_active=False, is_staff=False)

        context, status_code = build_search_results_context(request, "home")

        self.assertEqual(status_code, 200)
        self.assertIn("解析失败", context["error_message"])
        event, = mock_log_info.call_args.args
        extra = mock_log_info.call_args.kwargs["extra"]
        self.assertEqual(event, "media_parse_failed")
        self.assertEqual(extra["query_host"], "www.youtube.com")
        self.assertTrue(extra["requested_youtube_merge"])
        self.assertFalse(extra["prefer_youtube_merge"])
        self.assertEqual(extra["error_type"], "XhsParseError")
        self.assertIn("provider exploded", extra["error"])
        self.assertGreaterEqual(extra["duration_ms"], 0)

    @patch("xhs_download.views.get_youtube_merge_health")
    def test_youtube_merge_mode_respects_staff_only(self, mock_health):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["youtube_merge_mode", "updated_at"])
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": True,
            "staff_only": True,
        }
        request = RequestFactory().get("/")

        request.user = Mock(is_active=True, is_staff=False)
        self.assertFalse(youtube_merge_mode_is_available(request))

        request.user = Mock(is_active=True, is_staff=True)
        self.assertTrue(youtube_merge_mode_is_available(request))

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=False)
    @patch("xhs_download.views.get_youtube_merge_health")
    def test_youtube_merge_mode_requires_provider_capability(self, mock_health):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["youtube_merge_mode", "updated_at"])
        mock_health.return_value = {
            "implemented": True,
            "ready": True,
            "public_access_enabled": False,
            "staff_only": False,
        }
        request = RequestFactory().get("/")
        request.user = Mock(is_active=True, is_staff=True)

        self.assertFalse(youtube_merge_mode_is_available(request))

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=False,
        TIKHUB_API_KEY="secret",
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_ignore_ultra_hd_when_public_access_is_disabled(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.douyin.com/video/123",
            prefer_ultra_hd=False,
        )
        self.assertContains(response, "当前环境未向访客开放超高清 Provider")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        KUKUTOOL_NODE_BINARY="/missing/node",
    )
    @patch(
        "xhs_download.views.get_kukutool_provider_health",
        return_value={"available": False},
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_ignore_ultra_hd_when_node_runtime_is_unavailable(
        self,
        mock_parse,
        _mock_health,
    ):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.douyin.com/video/123",
            prefer_ultra_hd=False,
        )
        self.assertContains(response, "当前环境未向访客开放超高清 Provider")

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=False)
    def test_page_omits_async_task_url_when_tasks_are_disabled(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-download-task-url=""')
        self.assertContains(response, 'data-download-async-enabled="false"')

    def test_xhs_route_uses_xhs_results_action(self):
        response = Client().get("/xhs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-xhs")
        self.assertContains(response, 'action="/xhs/results/"')

    def test_tiktok_route_uses_tiktok_results_action(self):
        response = Client().get("/tiktok/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-tiktok")
        self.assertContains(response, 'action="/tiktok/results/"')

    def test_bilibili_route_uses_bilibili_results_action(self):
        response = Client().get("/bilibili/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-bilibili")
        self.assertContains(response, 'action="/bilibili/results/"')
        self.assertContains(response, "粘贴哔哩哔哩视频链接")

    @patch("xhs_download.views.parse_media_page")
    def test_home_results_keep_home_result_theme(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get("/results/", {"q": "https://v.douyin.com/abc123/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "result-theme-home")
        self.assertContains(response, "抖音")
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_xhs_route_can_parse_douyin_link(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get("/xhs/results/", {"q": "https://v.douyin.com/abc123/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "你粘贴的是抖音链接，已自动使用对应解析器。")
        self.assertContains(response, "抖音")
        self.assertContains(response, "result-theme-tiktok")
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_results_ajax_returns_partial_html_without_full_page_navigation(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get(
            "/tiktok/results/",
            {"q": "https://v.douyin.com/abc123/"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["has_results"])
        self.assertEqual(payload["base_filename"], "作者_标题")
        self.assertIn("result-theme-tiktok", payload["html"])
        self.assertNotIn("<!DOCTYPE html>", payload["html"])
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_results_ajax_separates_error_content_from_media_results(self, mock_parse):
        mock_parse.side_effect = XhsParseError("没有解析到图片或视频")

        response = Client().get(
            "/tiktok/results/",
            {"q": "https://v.douyin.com/abc123/"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["has_results"])
        self.assertTrue(payload["has_content"])
        self.assertIn("解析失败：没有解析到图片或视频", payload["message"])
        self.assertIn("alert-danger", payload["html"])

    @patch("xhs_download.views.parse_media_page")
    def test_results_ajax_returns_target_page_for_cross_platform_link(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get(
            "/xhs/results/",
            {"q": "https://v.douyin.com/abc123/"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["target_page"]["key"], "tiktok")
        self.assertEqual(payload["target_page"]["home_url"], "/tiktok/")
        self.assertEqual(payload["target_page"]["theme_class"], "theme-tiktok")
        self.assertEqual(payload["target_page"]["parse_action_url"], "/tiktok/results/")

    @patch("xhs_download.views.parse_media_page")
    def test_bilibili_results_render_video_asset(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="bilibili",
            post_type="video",
            base_filename="作者_标题",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4",
                    platform="bilibili",
                    cover_url="https://i0.hdslb.com/bfs/archive/cover.jpg",
                    cover_preview_url="https://i0.hdslb.com/bfs/archive/cover.jpg",
                    size_bytes=1024 * 1024,
                ),
            ),
        )

        response = Client().get("/bilibili/results/", {"q": "https://www.bilibili.com/video/BV1xx411c7mD/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "视频笔记")
        self.assertContains(response, "哔哩哔哩")
        self.assertContains(response, "已解析到 1 个视频资源")
        self.assertContains(response, "1.0 MB")
        self.assertContains(response, "download-progress-wrap")
        mock_parse.assert_called_once_with("https://www.bilibili.com/video/BV1xx411c7mD/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_mobile_results_page_shows_download_location_warning(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="作者_标题",
            assets=(
                MediaAsset(
                    media_type="image",
                    url="https://ci.xiaohongshu.com/a.jpg",
                    platform="xiaohongshu",
                ),
            ),
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.xiaohongshu.com/explore/demo"},
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "手机端建议单张图片或单个视频保存")
        self.assertContains(response, "系统“文件”App")
        self.assertNotContains(response, "自动保存到相册")

    @patch("xhs_download.views.build_download_archive_file")
    def test_download_endpoint_returns_zip_response(self, mock_archive):
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [
                    {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                    {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
                ],
                "base_filename": "base",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["X-Saved-Count"], "1")

    def test_download_route_single_image_direct(self):
        request = RequestFactory().post("/media/download-images/")
        route = decide_download_route(
            request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 123}],
            requested_mode="auto",
        )

        self.assertEqual(route["route_type"], ROUTE_DIRECT)
        self.assertEqual(route["browser_egress_estimated_bytes"], 123)
        self.assertEqual(route["server_egress_estimated_bytes"], 0)

    def test_download_route_many_images_uses_zip(self):
        request = RequestFactory().post("/media/download-images/")
        items = [
            {"type": "image", "url": f"https://ci.xiaohongshu.com/{index}.jpg", "size_bytes": 10}
            for index in range(13)
        ]

        route = decide_download_route(request, items, requested_mode="auto")

        self.assertEqual(route["route_type"], ROUTE_ZIP)
        self.assertEqual(route["server_egress_estimated_bytes"], 130)

    def test_download_route_nine_images_uses_zip(self):
        request = RequestFactory().post("/media/download-images/")
        items = [
            {"type": "image", "url": f"https://ci.xiaohongshu.com/{index}.jpg", "size_bytes": 10}
            for index in range(9)
        ]

        route = decide_download_route(request, items, requested_mode="auto")

        self.assertEqual(route["route_type"], ROUTE_ZIP)
        self.assertEqual(route["server_egress_estimated_bytes"], 90)

    def test_download_route_single_video_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        route = decide_download_route(
            request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4", "size_bytes": 456}],
            requested_mode="auto",
        )

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 456)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_browser_single_video_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        route = decide_download_route(
            request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4", "size_bytes": 456}],
            requested_mode="browser",
        )

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 456)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_proxy_single_video_forces_server_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        route = decide_download_route(
            request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4", "size_bytes": 456}],
            requested_mode="proxy",
        )

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 456)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_provider_resources_use_zip(self):
        request = RequestFactory().post("/media/download-images/")
        youtube_route = decide_download_route(
            request,
            [{
                "type": "video",
                "url": "https://example.com/youtube.mp4",
                "provider": "youtube_merge",
                "size_bytes": 1000,
            }],
            requested_mode="auto",
        )
        kukutool_route = decide_download_route(
            request,
            [{
                "type": "video",
                "url": "https://v6-default.365yg.com/a.mp4",
                "provider": "kukutool",
                "size_bytes": 2000,
            }],
            requested_mode="auto",
        )

        self.assertEqual(youtube_route["route_type"], ROUTE_ZIP)
        self.assertEqual(kukutool_route["route_type"], ROUTE_ZIP)

    def test_download_route_signed_kukutool_cdn_video_uses_server_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        item = make_kukutool_download_item("https://v6-default.365yg.com/a.mp4")
        item["size_bytes"] = 2000

        route = decide_download_route(request, [item], requested_mode="auto")

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 2000)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_browser_signed_kukutool_cdn_video_uses_server_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        item = make_kukutool_download_item("https://v6-default.365yg.com/a.mp4")
        item["size_bytes"] = 2000

        route = decide_download_route(request, [item], requested_mode="browser")

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 2000)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_proxy_signed_kukutool_cdn_video_forces_server_attachment(self):
        request = RequestFactory().post("/media/download-images/")
        item = make_kukutool_download_item("https://v6-default.365yg.com/a.mp4")
        item["size_bytes"] = 2000

        route = decide_download_route(request, [item], requested_mode="proxy")

        self.assertEqual(route["route_type"], ROUTE_PROXY)
        self.assertEqual(route["download_strategy"], "server_single_attachment")
        self.assertEqual(route["server_egress_estimated_bytes"], 2000)
        self.assertEqual(route["browser_egress_estimated_bytes"], 0)

    def test_download_route_mixed_items_use_zip(self):
        request = RequestFactory().post("/media/download-images/")
        route = decide_download_route(
            request,
            [
                {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                {"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4"},
            ],
            requested_mode="auto",
        )

        self.assertEqual(route["route_type"], ROUTE_ZIP)

    def test_download_route_visitor_video_disabled_blocks(self):
        setting = DownloadRuntimeSetting.load()
        setting.visitor_video_download_enabled = False
        setting.save(update_fields=["visitor_video_download_enabled", "updated_at"])
        request = RequestFactory().post("/media/download-images/")

        route = decide_download_route(
            request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4", "size_bytes": 789}],
            requested_mode="auto",
        )

        self.assertEqual(route["route_type"], ROUTE_BLOCKED)
        self.assertEqual(route["error_code"], "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        self.assertEqual(route["estimated_bytes"], 789)

    def test_route_v3_accuracy_inherits_v2_rules(self):
        request = RequestFactory().post("/media/download-images/")

        image_route = decide_route_v3(
            request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"}],
            context={"requested_mode": "auto"},
        )
        video_route = decide_route_v3(
            request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4"}],
            context={"requested_mode": "auto"},
        )
        mixed_route = decide_route_v3(
            request,
            [
                {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                {"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4"},
            ],
            context={"requested_mode": "auto"},
        )

        self.assertEqual(image_route["route_type"], ROUTE_DIRECT)
        self.assertEqual(video_route["route_type"], ROUTE_PROXY)
        self.assertEqual(mixed_route["route_type"], ROUTE_ZIP)
        self.assertEqual(image_route["strategy_version"], "v3")
        self.assertIn("estimated_cost", image_route)

    def test_route_v3_cost_estimation_handles_actual_delta(self):
        RoutingDecisionLog.objects.create(
            request_id="rid-cost",
            route_type=ROUTE_ZIP,
            provider="native",
            estimated_server_bytes=100,
            actual_server_bytes=250,
            success=True,
            duration_ms=100,
        )

        score = compute_routing_score(
            ROUTE_ZIP,
            RoutingDecisionLog.objects.filter(route_type=ROUTE_ZIP),
        )

        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 1)

    def test_route_v3_feedback_loop_writes_log_and_scores(self):
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["strategy_version"], "v3")
        self.assertIn("routing_score", response.json())
        feedback = RoutingDecisionLog.objects.get()
        self.assertEqual(feedback.route_type, ROUTE_DIRECT)
        self.assertTrue(feedback.success)
        self.assertEqual(feedback.estimated_browser_bytes, 321)

    def test_routing_policy_adjuster_returns_recommendations(self):
        for index in range(3):
            RoutingDecisionLog.objects.create(
                request_id=f"rid-direct-{index}",
                route_type=ROUTE_DIRECT,
                provider="native",
                estimated_browser_bytes=10,
                success=True,
                duration_ms=100,
            )

        recommendations = build_routing_policy_recommendations()

        self.assertTrue(recommendations)
        self.assertIn("DIRECT", recommendations[0])


    def test_download_auto_direct_returns_json_and_writes_route_log(self):
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "routed")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(response["Content-Type"], "application/json")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.browser_egress_estimated_bytes, 321)
        self.assertNotIn("https://", log.route_reason)

    @override_settings(
        DOWNLOAD_RATE_LIMIT_PER_MINUTE=1,
        DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT=1,
        DOWNLOAD_MAX_CONCURRENT_PER_IP=1,
        DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    def test_download_auto_direct_does_not_consume_server_download_limits(self):
        cache.clear()
        DownloadRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "rate_limit_per_minute": 1,
                "daily_request_limit_per_ip": 0,
                "global_daily_request_limit": 1,
                "max_concurrent_per_ip": 1,
                "daily_bytes_limit_per_ip": 0,
                "global_daily_bytes_limit": 0,
            },
        )
        payload = json.dumps({
            "download_mode": "auto",
            "media_items": [{
                "type": "image",
                "url": "https://ci.xiaohongshu.com/a.jpg",
                "size_bytes": 321,
            }],
        })
        client = Client()

        first = client.post("/media/download-images/", data=payload, content_type="application/json")
        second = client.post("/media/download-images/", data=payload, content_type="application/json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(second.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(get_daily_total("download-requests-global", "all"), 0)
        self.assertEqual(get_daily_total("download-requests-ip", get_client_identity(first.wsgi_request)), 0)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_auto_video_returns_server_single_file(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url="https://sns-video-hw.xhscdn.com/a.mp4",
            content_type="video/mp4",
        )
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/a.mp4",
                    "size_bytes": 654,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        self.assertEqual(mock_fetch.call_count, 1)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 654)
        self.assertEqual(log.browser_egress_estimated_bytes, 0)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_proxy_video_returns_single_file_response(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url="https://sns-video-hw.xhscdn.com/a.mp4",
            content_type="video/mp4",
        )
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/a.mp4",
                    "size_bytes": 654,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 654)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_single_video_ssl_error_returns_short_error(self, mock_fetch):
        long_error = (
            "HTTPSConnectionPool(host='v11-default.365yg.com', port=443): "
            "Max retries exceeded with url: /video/tos/cn/demo.mp4?token=secret "
            "(Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol')))"
        )
        mock_fetch.side_effect = requests.exceptions.SSLError(long_error)

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/a.mp4",
                    "size_bytes": 654,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["message"], "单文件下载失败，请重新解析后再试。")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.route_type, ROUTE_BLOCKED)
        self.assertEqual(log.error_code, "PROXY_FAILED_RECOVERY_BLOCKED")
        self.assertEqual(log.fallback_from_route, ROUTE_PROXY)
        self.assertEqual(log.fallback_to_route, ROUTE_BLOCKED)
        self.assertEqual(log.failure_type, FAILURE_PROXY_FAILED)
        self.assertNotIn("365yg.com", log.error_message_short)
        self.assertNotIn("token=secret", log.error_message_short)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_signed_kukutool_video_uses_server_single_file(self, mock_fetch):
        item = make_kukutool_download_item("https://v11-default.365yg.com/video/demo.mp4")
        item["size_bytes"] = 2000
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url=item["url"],
            content_type="video/mp4",
        )

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [item],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 2000)
        self.assertEqual(log.browser_egress_estimated_bytes, 0)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_proxy_signed_kukutool_video_returns_single_file_response(self, mock_fetch):
        item = make_kukutool_download_item("https://v11-default.365yg.com/video/demo.mp4")
        item["size_bytes"] = 2000
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url=item["url"],
            content_type="video/mp4",
        )

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [item],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 2000)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_proxy_single_video_returns_single_file_response(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url="https://sns-video-hw.xhscdn.com/a.mp4",
            content_type="video/mp4",
        )
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/a.mp4",
                    "size_bytes": 654,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 654)

    @patch("xhs_download.views.fetch_media_response")
    def test_download_browser_single_image_returns_single_file_response(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(
            b"image-content",
            url="https://ci.xiaohongshu.com/a.jpg",
            content_type="image/jpeg",
        )
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "browser",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"image-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.server_egress_estimated_bytes, 321)

    def test_download_auto_blocked_returns_error_header_and_route_log(self):
        setting = DownloadRuntimeSetting.load()
        setting.visitor_video_download_enabled = False
        setting.save(update_fields=["visitor_video_download_enabled", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/a.mp4",
                    "size_bytes": 100,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["X-Error-Code"], "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_BLOCKED)
        self.assertEqual(log.estimated_bytes, 100)

    @patch("xhs_download.views.build_download_archive_file")
    def test_download_auto_zip_still_uses_zip_flow(self, mock_archive):
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 2,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [
                    {"type": "image", "url": f"https://ci.xiaohongshu.com/{index}.jpg"}
                    for index in range(13)
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_ZIP)

    @patch("xhs_download.views.build_download_archive_file")
    @patch("xhs_download.views.decide_download_route", side_effect=RuntimeError("routing exploded"))
    def test_routing_safe_wrapper_falls_back_to_zip_without_500(self, _mock_route, mock_archive):
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 100,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_ZIP)
        self.assertEqual(log.recovery_count, 1)
        self.assertEqual(log.fallback_count, 1)
        self.assertEqual(log.fallback_from_route, "routing")
        self.assertEqual(log.fallback_to_route, ROUTE_ZIP)
        self.assertEqual(log.failure_type, FAILURE_ROUTING_FAILED)
        self.assertEqual(log.auto_recovery_count, 1)
        self.assertEqual(log.healing_action, "fallback_zip")
        self.assertEqual(SelfHealingEvent.objects.count(), 1)
        event = SelfHealingEvent.objects.get()
        self.assertEqual(event.failure_type, FAILURE_ROUTING_FAILED)
        self.assertEqual(event.action, "fallback_zip")
        self.assertTrue(event.healed)

    def test_self_healing_engine_switches_provider_and_records_event(self):
        request = RequestFactory().get("/media/download-images/")
        request.request_id = "rid-heal-provider"
        switched = []

        outcome = SelfHealingEngine(request).handle_failure(
            trigger="provider_exception",
            route_result={"provider": "kukutool"},
            provider="kukutool",
            error=XhsProviderTimeoutError("provider timeout"),
            depth=1,
            provider_switcher=switched.append,
        )

        self.assertTrue(outcome.healed)
        self.assertEqual(outcome.action, HEAL_ACTION_SWITCH_PROVIDER)
        self.assertEqual(outcome.switched_from_provider, "kukutool")
        self.assertEqual(outcome.switched_to_provider, "yt_dlp_generic")
        self.assertEqual(switched, ["yt_dlp_generic"])
        event = SelfHealingEvent.objects.get()
        self.assertEqual(event.request_id, "rid-heal-provider")
        self.assertEqual(event.action, HEAL_ACTION_SWITCH_PROVIDER)
        self.assertEqual(event.switched_to_provider, "yt_dlp_generic")

    def test_self_healing_engine_blocks_after_max_depth(self):
        request = RequestFactory().get("/media/download-images/")
        request.request_id = "rid-heal-depth"

        outcome = SelfHealingEngine(request).handle_failure(
            trigger="provider_exception",
            route_result={"provider": "kukutool"},
            provider="kukutool",
            error=XhsProviderTimeoutError("provider timeout"),
            depth=3,
        )

        self.assertFalse(outcome.healed)
        self.assertEqual(outcome.action, HEAL_ACTION_BLOCK_GRACEFULLY)
        self.assertEqual(outcome.fallback_to_route, ROUTE_BLOCKED)
        self.assertEqual(SelfHealingEvent.objects.get().depth, 3)

    @patch("xhs_download.views.fetch_media_response")
    @patch("xhs_download.views.build_download_archive_file")
    def test_provider_timeout_during_zip_falls_back_to_proxy(self, mock_archive, mock_fetch):
        item = make_kukutool_download_item("https://v11-default.365yg.com/video/demo.mp4")
        item["size_bytes"] = 2000
        mock_archive.side_effect = XhsProviderTimeoutError("provider timeout")
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url=item["url"],
            content_type="video/mp4",
        )

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "zip",
                "media_items": [item],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "single_file")
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.recovery_count, 1)
        self.assertEqual(log.fallback_count, 1)
        self.assertEqual(log.fallback_from_route, ROUTE_ZIP)
        self.assertEqual(log.fallback_to_route, ROUTE_PROXY)
        self.assertEqual(log.failure_type, FAILURE_PROVIDER_TIMEOUT)
        self.assertEqual(log.auto_recovery_count, 1)
        self.assertEqual(log.provider_auto_switch_count, 1)
        self.assertEqual(log.healing_action, HEAL_ACTION_SWITCH_PROVIDER)
        event = SelfHealingEvent.objects.get()
        self.assertEqual(event.failure_type, FAILURE_PROVIDER_TIMEOUT)
        self.assertEqual(event.switched_from_provider, "kukutool")
        self.assertEqual(event.switched_to_provider, "yt_dlp_generic")

    @patch("xhs_download.views.fetch_media_response")
    @patch("xhs_download.views.build_download_archive_file")
    def test_zip_failure_falls_back_to_proxy_for_single_image(self, mock_archive, mock_fetch):
        mock_archive.side_effect = XhsDownloadError("zip failed")
        mock_fetch.return_value = FakeStreamingResponse(
            b"image-content",
            url="https://ci.xiaohongshu.com/a.jpg",
            content_type="image/jpeg",
        )

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "zip",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 100,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_PROXY)
        self.assertEqual(log.recovery_count, 1)
        self.assertEqual(log.fallback_count, 1)
        self.assertEqual(log.fallback_from_route, ROUTE_ZIP)
        self.assertEqual(log.fallback_to_route, ROUTE_PROXY)
        self.assertEqual(log.auto_recovery_count, 1)
        self.assertEqual(log.healing_action, "fallback_proxy")
        self.assertEqual(SelfHealingEvent.objects.get().action, "fallback_proxy")

    @patch("xhs_download.views.fetch_media_response")
    def test_proxy_failure_blocks_when_direct_fallback_is_not_safe(self, mock_fetch):
        mock_fetch.side_effect = XhsDownloadError("proxy failed")

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 100,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.route_type, ROUTE_BLOCKED)
        self.assertEqual(log.recovery_count, 1)
        self.assertEqual(log.fallback_count, 1)
        self.assertEqual(log.fallback_from_route, ROUTE_PROXY)
        self.assertEqual(log.fallback_to_route, ROUTE_BLOCKED)
        self.assertEqual(log.failure_type, FAILURE_PROXY_FAILED)
        self.assertEqual(log.auto_recovery_count, 0)
        self.assertEqual(log.healing_action, "fallback_direct")
        self.assertFalse(SelfHealingEvent.objects.get().healed)

    def test_download_runtime_setting_cost_guard_defaults_to_unlimited(self):
        setting = DownloadRuntimeSetting.load()

        self.assertEqual(setting.single_archive_bytes_limit, 0)
        self.assertEqual(setting.single_video_bytes_limit, 0)
        self.assertTrue(setting.visitor_video_download_enabled)
        self.assertFalse(setting.staff_large_download_exempt)
        self.assertEqual(setting.large_download_warning_bytes, 100 * 1024 * 1024)

    def test_download_endpoint_blocks_visitor_video_when_disabled(self):
        setting = DownloadRuntimeSetting.load()
        setting.visitor_video_download_enabled = False
        setting.save(update_fields=["visitor_video_download_enabled", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/demo.mp4",
                    "size_bytes": 1024,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["X-Error-Code"], "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.error_code, "VISITOR_VIDEO_DOWNLOAD_DISABLED")

    @patch("xhs_download.views.fetch_media_response")
    @patch("xhs_download.views.build_download_archive_file")
    def test_staff_can_download_video_single_file_when_visitor_video_disabled(self, mock_archive, mock_fetch):
        from django.contrib.auth import get_user_model

        setting = DownloadRuntimeSetting.load()
        setting.visitor_video_download_enabled = False
        setting.save(update_fields=["visitor_video_download_enabled", "updated_at"])
        user = get_user_model().objects.create_user(username="download-staff", is_staff=True)
        client = Client()
        client.force_login(user)
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 1,
            "failed_count": 0,
        }
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url="https://sns-video-hw.xhscdn.com/demo.mp4",
            content_type="video/mp4",
        )

        response = client.post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/demo.mp4",
                    "size_bytes": 1024,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        self.assertEqual(mock_archive.call_count, 0)
        self.assertEqual(mock_fetch.call_count, 1)

    def test_download_endpoint_blocks_single_video_size_limit(self):
        setting = DownloadRuntimeSetting.load()
        setting.single_video_bytes_limit = 100
        setting.save(update_fields=["single_video_bytes_limit", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/demo.mp4",
                    "size_bytes": 101,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["X-Error-Code"], "SINGLE_VIDEO_SIZE_LIMIT")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.error_code, "SINGLE_VIDEO_SIZE_LIMIT")
        self.assertEqual(log.estimated_bytes, 101)

    def test_download_endpoint_blocks_estimated_archive_size_limit(self):
        setting = DownloadRuntimeSetting.load()
        setting.single_archive_bytes_limit = 100
        setting.save(update_fields=["single_archive_bytes_limit", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [
                    {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 60},
                    {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg", "size_bytes": 41},
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["X-Error-Code"], "SINGLE_ARCHIVE_SIZE_LIMIT")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.error_code, "SINGLE_ARCHIVE_SIZE_LIMIT")
        self.assertEqual(log.estimated_bytes, 101)

    @patch("xhs_download.views.build_download_archive_file")
    def test_download_endpoint_blocks_actual_archive_size_limit(self, mock_archive):
        setting = DownloadRuntimeSetting.load()
        setting.single_archive_bytes_limit = 10
        setting.save(update_fields=["single_archive_bytes_limit", "updated_at"])
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"this archive is too large"),
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [
                    {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                    {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["X-Error-Code"], "SINGLE_ARCHIVE_SIZE_LIMIT")
        self.assertEqual(mock_archive.call_count, 1)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.error_code, "SINGLE_ARCHIVE_SIZE_LIMIT")
        self.assertGreater(log.actual_bytes, 10)

    @patch("xhs_download.views.fetch_media_response")
    @patch("xhs_download.views.build_download_archive_file")
    def test_staff_large_download_exempt_allows_single_video_file(self, mock_archive, mock_fetch):
        from django.contrib.auth import get_user_model

        setting = DownloadRuntimeSetting.load()
        setting.single_archive_bytes_limit = 10
        setting.single_video_bytes_limit = 10
        setting.staff_large_download_exempt = True
        setting.save(update_fields=[
            "single_archive_bytes_limit",
            "single_video_bytes_limit",
            "staff_large_download_exempt",
            "updated_at",
        ])
        user = get_user_model().objects.create_user(username="large-staff", is_staff=True)
        client = Client()
        client.force_login(user)
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"this archive is allowed for staff"),
            "saved_count": 1,
            "failed_count": 0,
        }
        mock_fetch.return_value = FakeStreamingResponse(
            b"video-content",
            url="https://sns-video-hw.xhscdn.com/demo.mp4",
            content_type="video/mp4",
        )

        response = client.post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [{
                    "type": "video",
                    "url": "https://sns-video-hw.xhscdn.com/demo.mp4",
                    "size_bytes": 1000,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "video/mp4")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"video-content")
        self.assertEqual(mock_archive.call_count, 0)
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("xhs_download.views.build_download_archive_file")
    def test_staff_large_download_exempt_does_not_bypass_global_daily_bytes(self, mock_archive):
        from django.contrib.auth import get_user_model

        setting = DownloadRuntimeSetting.load()
        setting.single_archive_bytes_limit = 10
        setting.staff_large_download_exempt = True
        setting.daily_bytes_limit_per_ip = 0
        setting.global_daily_bytes_limit = 1
        setting.save(update_fields=[
            "single_archive_bytes_limit",
            "staff_large_download_exempt",
            "daily_bytes_limit_per_ip",
            "global_daily_bytes_limit",
            "updated_at",
        ])
        user = get_user_model().objects.create_user(username="daily-staff", is_staff=True)
        client = Client()
        client.force_login(user)
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"too much for global daily bytes"),
            "saved_count": 1,
            "failed_count": 0,
        }

        response = client.post(
            "/media/download-images/",
            data=json.dumps({
                "media_items": [
                    {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                    {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-Error-Code"], "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.RATE_LIMITED)
        self.assertEqual(log.error_code, "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL")

    def test_download_endpoint_rejects_invalid_json(self):
        response = Client().post(
            "/media/download-images/",
            data="{bad",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_download_endpoint_requires_csrf_when_checks_are_enabled(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/media/download-images/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(DOWNLOAD_MAX_CONCURRENT_PER_IP=1)
    @patch("xhs_download.views.acquire_concurrency", side_effect=RuntimeError("redis unavailable"))
    def test_download_endpoint_fails_closed_when_concurrency_cache_is_unavailable(self, _mock_acquire):
        with patch("xhs_download.views.logger.exception") as mock_log_exception:
            response = Client().post(
                "/media/download-images/",
                data=json.dumps({
                    "media_items": [
                        {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                        {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
                    ],
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("下载保护服务暂时不可用", response.json()["message"])
        mock_log_exception.assert_called_once_with("Download concurrency cache failed")

    @patch("xhs_download.services.rate_limits.cache.decr", side_effect=RuntimeError("redis unavailable"))
    def test_concurrency_release_cache_failure_does_not_escape(self, _mock_decr):
        with self.assertLogs("xhs_download.services.rate_limits", level="ERROR"):
            release_concurrency("download-concurrent", "client")

    @override_settings(
        DOWNLOAD_RATE_LIMIT_PER_MINUTE=1,
        DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT=1,
        DOWNLOAD_MAX_CONCURRENT_PER_IP=0,
        DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.build_download_archive_file")
    def test_download_endpoint_applies_ip_rate_limit(self, mock_archive):
        DownloadRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "rate_limit_per_minute": 1,
                "daily_request_limit_per_ip": 0,
                "global_daily_request_limit": 1,
                "max_concurrent_per_ip": 0,
                "daily_bytes_limit_per_ip": 0,
                "global_daily_bytes_limit": 0,
            },
        )
        mock_archive.side_effect = [
            {
                "filename": "first.zip",
                "file": BytesIO(b"first"),
                "saved_count": 1,
                "failed_count": 0,
            },
            {
                "filename": "second.zip",
                "file": BytesIO(b"second"),
                "saved_count": 1,
                "failed_count": 0,
            },
        ]
        payload = json.dumps({
            "media_items": [
                {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
            ],
        })

        first = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )
        second = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertEqual(second["X-Error-Code"], "DOWNLOAD_MINUTE_RATE_LIMIT")
        self.assertEqual(mock_archive.call_count, 1)
        self.assertEqual(get_daily_total("download-requests-global", "all"), 1)

    @override_settings(
        DOWNLOAD_RATE_LIMIT_PER_MINUTE=0,
        DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT=0,
        DOWNLOAD_MAX_CONCURRENT_PER_IP=0,
        DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.build_download_archive_file")
    def test_download_runtime_setting_overrides_env_defaults(self, mock_archive):
        DownloadRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "rate_limit_per_minute": 1,
                "daily_request_limit_per_ip": 0,
                "global_daily_request_limit": 0,
                "max_concurrent_per_ip": 0,
                "daily_bytes_limit_per_ip": 0,
                "global_daily_bytes_limit": 0,
            },
        )
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 1,
            "failed_count": 0,
        }
        payload = json.dumps({
            "media_items": [
                {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"},
                {"type": "image", "url": "https://ci.xiaohongshu.com/b.jpg"},
            ],
        })

        first = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )
        second = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second["X-Error-Code"], "DOWNLOAD_MINUTE_RATE_LIMIT")

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=False)
    def test_download_task_endpoint_is_disabled_in_production_mode(self):
        response = Client().post(
            "/media/download-tasks/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("同步下载接口", response.json()["message"])

    @patch("xhs_download.views.save_media_items_to_server")
    def test_production_server_save_mode_falls_back_to_auto_download(self, mock_save):
        with override_settings(DEBUG=False, XHS_ALLOW_SERVER_SAVE=True, XHS_ENABLE_SERVER_SAVE_MODE=False):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a.jpg"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "routed")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(mock_save.call_count, 0)

    @patch("xhs_download.views.save_media_items_to_server")
    def test_debug_server_save_mode_without_hard_gate_falls_back_to_auto_download(self, mock_save):
        with override_settings(DEBUG=True, XHS_ALLOW_SERVER_SAVE=True, XHS_ENABLE_SERVER_SAVE_MODE=False):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a.jpg"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "routed")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(mock_save.call_count, 0)

    @patch("xhs_download.views.save_media_items_to_server")
    def test_debug_server_save_mode_can_save_to_server(self, mock_save):
        mock_save.return_value = {
            "saved_count": 1,
            "failed_count": 0,
            "saved_files": ["xhs/a.jpg"],
            "failed_files": [],
        }

        with override_settings(DEBUG=True, XHS_ALLOW_SERVER_SAVE=True, XHS_ENABLE_SERVER_SAVE_MODE=True):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a.jpg"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertIn("服务端保存完成", response.json()["message"])
        self.assertEqual(mock_save.call_count, 1)

    @patch("xhs_download.services.downloads.fetch_media_response")
    def test_archive_progress_updates_during_large_file_download(self, mock_fetch):
        content = b"a" * (1024 * 300)
        mock_fetch.return_value = FakeStreamingResponse(content)
        progress_events = []

        archive = build_download_archive_file(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"}],
            base_filename="base",
            progress_callback=lambda completed, total: progress_events.append((completed, total)),
        )

        try:
            self.assertTrue(any(0 < completed < 1 for completed, _ in progress_events))
            self.assertEqual(progress_events[-1], (1, 1))
        finally:
            archive["file"].close()

    @patch("xhs_download.services.downloads.fetch_media_response")
    def test_archive_progress_uses_item_size_when_content_length_missing(self, mock_fetch):
        content = b"a" * (1024 * 300)
        response = FakeStreamingResponse(content, content_type="video/mp4")
        response.headers.pop("Content-Length", None)
        mock_fetch.return_value = response
        progress_events = []

        archive = build_download_archive_file(
            media_items=[{
                "type": "video",
                "url": "https://v11-default.365yg.com/video/demo.mp4",
                "size_bytes": len(content),
            }],
            base_filename="base",
            progress_callback=lambda completed, total: progress_events.append((completed, total)),
        )

        try:
            self.assertTrue(any(0 < completed < 1 for completed, _ in progress_events))
            self.assertEqual(progress_events[-1], (1, 1))
        finally:
            archive["file"].close()

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=True)
    @patch("xhs_download.services.download_tasks.build_download_archive_file")
    def test_download_task_flow_returns_progress_and_file(self, mock_archive):
        archive_file = NamedTemporaryFile(prefix="test_archive_", suffix=".zip")
        archive_file.write(b"zip-content")
        archive_file.seek(0)
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": archive_file,
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-tasks/",
            data=json.dumps({
                "media_items": [{"type": "image", "url": "https://ci.xiaohongshu.com/a"}],
                "base_filename": "base",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")

        status_payload = None
        for _ in range(20):
            status_response = Client().get(payload["status_url"])
            self.assertEqual(status_response.status_code, 200)
            status_payload = status_response.json()
            if status_payload["status"] == "completed":
                break
            time.sleep(0.02)

        self.assertEqual(status_payload["status"], "completed")
        self.assertEqual(status_payload["progress"], 100)
        self.assertEqual(status_payload["saved_count"], 1)

        file_response = Client().get(status_payload["file_url"])
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response["Content-Type"], "application/zip")
        self.assertEqual(b"".join(file_response.streaming_content), b"zip-content")


class PolicyEngineEvolutionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.request = RequestFactory().post("/media/download-images/")

    def test_cost_provider_routing_policies_return_standard_results(self):
        context = build_policy_context(
            self.request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"}],
            requested_mode="auto",
        )

        cost = CostPolicy().evaluate(context)
        provider = ProviderPolicy().evaluate(context)
        routing = RoutingPolicy().evaluate(context)

        self.assertEqual(cost.policy, "cost")
        self.assertEqual(provider.policy, "provider")
        self.assertEqual(routing.policy, "routing")
        self.assertIn(cost.route, {ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, ROUTE_BLOCKED})
        self.assertGreaterEqual(cost.score, 0)
        self.assertLessEqual(cost.score, 1)
        self.assertGreaterEqual(provider.confidence, 0)
        self.assertLessEqual(provider.confidence, 1)
        self.assertEqual(routing.route, ROUTE_DIRECT)

    def test_policy_conflict_resolution_prefers_provider_server_constraint(self):
        context = build_policy_context(
            self.request,
            [{
                "type": "video",
                "url": "https://example.com/video.mp4",
                "provider": "youtube_merge",
                "size_bytes": 800_000_000,
            }],
            requested_mode="auto",
        )

        decision = decide_final_policy(context)

        self.assertEqual(decision.route, ROUTE_ZIP)
        self.assertEqual(set(decision.policy_scores.keys()), {"cost", "provider", "routing"})
        self.assertIn("provider-requires-server-validation-or-merge", decision.reason_chain)

    @override_settings(POLICY_ENGINE_MODE="shadow", POLICY_ENGINE_V35_ENABLED=False)
    def test_policy_shadow_fallback_does_not_change_existing_download_route(self):
        route_info = decide_download_route(
            self.request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}],
            requested_mode="auto",
        )
        shadow = evaluate_policy_shadow(
            self.request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}],
            route_info=route_info,
            requested_mode="auto",
        )

        self.assertEqual(route_info["route_type"], ROUTE_DIRECT)
        self.assertEqual(shadow["fallback_route"], ROUTE_DIRECT)
        self.assertFalse(shadow["active"])
        self.assertEqual(shadow["decision_source"], "routing_v2_fallback_with_policy_shadow")

    def test_policy_runtime_default_shadow_does_not_override_route(self):
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}
        route_info = decide_download_route(self.request, [item], requested_mode="browser")

        updated = apply_policy_decision_to_route(
            self.request,
            [item],
            route_info,
            requested_mode="browser",
        )

        self.assertEqual(route_info["route_type"], ROUTE_PROXY)
        self.assertEqual(updated["route_type"], ROUTE_PROXY)
        self.assertEqual(updated["final_policy_route"], ROUTE_DIRECT)
        self.assertFalse(updated["policy_active"])
        self.assertEqual(
            updated["policy_decision_source"],
            "routing_v2_fallback_with_policy_shadow",
        )

    def test_policy_v35_public_mode_can_override_executable_route(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC
        setting.save(update_fields=["v35_mode", "updated_at"])
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}
        route_info = decide_download_route(self.request, [item], requested_mode="browser")

        updated = apply_policy_decision_to_route(
            self.request,
            [item],
            route_info,
            requested_mode="browser",
        )

        self.assertEqual(route_info["route_type"], ROUTE_PROXY)
        self.assertEqual(updated["route_type"], ROUTE_DIRECT)
        self.assertEqual(updated["download_strategy"], "browser_direct")
        self.assertTrue(updated["policy_active"])
        self.assertEqual(updated["policy_decision_source"], "policy_v3_5")
        self.assertEqual(updated["strategy_version"], "v3.5")

    def test_policy_v35_staff_mode_requires_staff(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.STAFF
        setting.save(update_fields=["v35_mode", "updated_at"])
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}
        anonymous_route = decide_download_route(self.request, [item], requested_mode="browser")

        anonymous_result = apply_policy_decision_to_route(
            self.request,
            [item],
            anonymous_route,
            requested_mode="browser",
        )

        staff_request = RequestFactory().post("/media/download-images/")
        staff_request.user = Mock(
            is_active=True,
            is_staff=True,
            is_superuser=False,
            is_authenticated=True,
            pk=1,
        )
        staff_route = decide_download_route(staff_request, [item], requested_mode="browser")
        staff_result = apply_policy_decision_to_route(
            staff_request,
            [item],
            staff_route,
            requested_mode="browser",
        )

        self.assertEqual(anonymous_result["route_type"], ROUTE_PROXY)
        self.assertFalse(anonymous_result["policy_active"])
        self.assertEqual(staff_result["route_type"], ROUTE_DIRECT)
        self.assertTrue(staff_result["policy_active"])

    def test_policy_v35_public_gray_supports_80_20_fallback_gate(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC_GRAY
        setting.v35_rollout_percent = 0
        setting.save(update_fields=["v35_mode", "v35_rollout_percent", "updated_at"])
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}
        route_info = decide_download_route(self.request, [item], requested_mode="browser")

        fallback_result = apply_policy_decision_to_route(
            self.request,
            [item],
            route_info,
            requested_mode="browser",
        )

        setting.v35_rollout_percent = 100
        setting.save(update_fields=["v35_rollout_percent", "updated_at"])
        active_result = apply_policy_decision_to_route(
            self.request,
            [item],
            route_info,
            requested_mode="browser",
        )

        self.assertEqual(fallback_result["route_type"], ROUTE_PROXY)
        self.assertFalse(fallback_result["policy_active"])
        self.assertEqual(active_result["route_type"], ROUTE_DIRECT)
        self.assertTrue(active_result["policy_active"])
        self.assertEqual(active_result["policy_decision"]["rollout_percent"], 100)

    def test_policy_shadow_fields_are_recorded_on_download_log(self):
        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "auto",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.final_policy_route, ROUTE_DIRECT)
        self.assertGreater(log.policy_cost_score, 0)
        self.assertGreater(log.policy_provider_score, 0)
        self.assertGreater(log.policy_routing_score, 0)
        self.assertGreaterEqual(log.policy_confidence, 0)
        self.assertFalse(log.policy_active)
        self.assertEqual(log.policy_decision_source, "routing_v2_fallback_with_policy_shadow")

    def test_policy_v35_active_download_records_feedback_fields(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC
        setting.save(update_fields=["v35_mode", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "browser",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.final_policy_route, ROUTE_DIRECT)
        self.assertTrue(log.policy_active)
        self.assertEqual(log.policy_decision_source, "policy_v3_5")
        self.assertGreaterEqual(log.policy_estimated_savings_bytes, 0)

    def test_policy_v35_active_can_override_explicit_zip_mode(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC
        setting.save(update_fields=["v35_mode", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "zip",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.final_policy_route, ROUTE_DIRECT)
        self.assertTrue(log.policy_active)
        self.assertEqual(log.policy_decision_source, "policy_v3_5")

    def test_policy_v35_active_can_override_explicit_proxy_mode(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC
        setting.save(update_fields=["v35_mode", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "proxy",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.final_policy_route, ROUTE_DIRECT)
        self.assertTrue(log.policy_active)
        self.assertEqual(log.policy_decision_source, "policy_v3_5")

    def test_policy_v35_active_can_override_explicit_server_single_mode(self):
        setting = PolicyRuntimeSetting.load()
        setting.v35_mode = PolicyRuntimeMode.PUBLIC
        setting.save(update_fields=["v35_mode", "updated_at"])

        response = Client().post(
            "/media/download-images/",
            data=json.dumps({
                "download_mode": "server_single",
                "media_items": [{
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/a.jpg",
                    "size_bytes": 321,
                }],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.final_policy_route, ROUTE_DIRECT)
        self.assertTrue(log.policy_active)
        self.assertEqual(log.policy_decision_source, "policy_v3_5")

    def test_cost_guard_still_blocks_unsafe_request(self):
        setting = DownloadRuntimeSetting.load()
        setting.visitor_video_download_enabled = False
        setting.save(update_fields=["visitor_video_download_enabled", "updated_at"])
        policy_setting = PolicyRuntimeSetting.load()
        policy_setting.v35_mode = PolicyRuntimeMode.PUBLIC
        policy_setting.save(update_fields=["v35_mode", "updated_at"])
        context = build_policy_context(
            self.request,
            [{"type": "video", "url": "https://sns-video-hw.xhscdn.com/a.mp4"}],
            requested_mode="auto",
        )

        decision = decide_final_policy(context)

        self.assertFalse(decision.allow_download)
        self.assertEqual(decision.route, ROUTE_BLOCKED)
        self.assertIn("当前环境暂不允许访客下载视频资源。", decision.reason_chain)

    def test_policy_learning_run_standardizes_shadow_recommendation(self):
        DownloadRequestLog.objects.create(
            request_id="rid-policy",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            item_count=1,
            route_type=ROUTE_ZIP,
            provider="native",
            actual_bytes=100,
            server_egress_estimated_bytes=80,
        )
        ParseRequestLog.objects.create(
            request_id="rid-parse-policy",
            status=ObservabilityStatus.SUCCESS,
            selected_provider="native",
            result_platform="xiaohongshu",
        )

        snapshot = build_policy_learning_snapshot(window_hours=24)
        run = record_policy_learning_run(window_hours=24)

        self.assertEqual(snapshot["window_hours"], 24)
        self.assertIn("shadow_recommendation", snapshot)
        self.assertIn("confidence", snapshot)
        self.assertIn("estimated_savings_bytes", snapshot)
        self.assertEqual(PolicyLearningRun.objects.count(), 1)
        self.assertEqual(run.window_hours, 24)
        self.assertGreaterEqual(run.sample_count, 2)

    def test_policy_v4_adaptive_weights_are_shadow_only(self):
        DownloadRequestLog.objects.create(
            request_id="rid-policy-v4",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            route_type=ROUTE_ZIP,
            provider="yt_dlp_merge",
            actual_bytes=500_000_000,
            server_egress_estimated_bytes=100_000_000,
        )

        recommendation = build_adaptive_weight_recommendation(window_hours=24)

        self.assertEqual(set(recommendation["weights"]), {"cost", "provider", "routing"})
        self.assertAlmostEqual(sum(recommendation["weights"].values()), 1.0, places=3)
        self.assertIn("policy-v4-shadow-adaptive-weights", recommendation["reason_chain"])

    def test_policy_v4_auto_weights_default_to_static(self):
        DownloadRequestLog.objects.create(
            request_id="rid-policy-v4-static",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            route_type=ROUTE_ZIP,
            final_policy_route=ROUTE_ZIP,
            provider="native",
            actual_bytes=500_000_000,
            server_egress_estimated_bytes=1,
        )

        diagnostics = policy_weight_diagnostics()

        self.assertEqual(diagnostics["source"], "static")
        self.assertAlmostEqual(diagnostics["weights"]["cost"], 0.4)
        self.assertAlmostEqual(diagnostics["weights"]["provider"], 0.4)
        self.assertAlmostEqual(diagnostics["weights"]["routing"], 0.2)

    def test_policy_v4_auto_weights_can_feed_policy_decision(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_auto_weights_enabled = True
        setting.v4_adaptive_min_confidence = 0.1
        setting.v4_adaptive_window_hours = 24
        setting.save(update_fields=[
            "v4_auto_weights_enabled",
            "v4_adaptive_min_confidence",
            "v4_adaptive_window_hours",
            "updated_at",
        ])
        DownloadRequestLog.objects.create(
            request_id="rid-policy-v4-active",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            route_type=ROUTE_ZIP,
            final_policy_route=ROUTE_ZIP,
            provider="native",
            actual_bytes=500_000_000,
            server_egress_estimated_bytes=1,
        )
        context = build_policy_context(
            self.request,
            [{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"}],
            requested_mode="auto",
        )

        decision = decide_final_policy(context)

        self.assertEqual(decision.weight_source, "policy_v4_adaptive")
        self.assertIn("server-cost-delta-positive", decision.reason_chain)
        self.assertGreater(decision.policy_weights["cost"], 0.4)
        self.assertGreaterEqual(decision.weight_confidence, 0.1)

    def test_policy_v4_auto_weights_low_confidence_falls_back_static(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_auto_weights_enabled = True
        setting.v4_adaptive_min_confidence = 0.99
        setting.save(update_fields=[
            "v4_auto_weights_enabled",
            "v4_adaptive_min_confidence",
            "updated_at",
        ])

        diagnostics = policy_weight_diagnostics()

        self.assertEqual(diagnostics["source"], "static_adaptive_confidence_too_low")
        self.assertAlmostEqual(diagnostics["weights"]["cost"], 0.4)
        self.assertLess(diagnostics["confidence"], 0.99)

    def test_policy_v4_evolution_generates_review_suggestion_without_execution(self):
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}
        before = decide_download_route(self.request, [item], requested_mode="auto")

        recommendation = build_policy_evolution_recommendation(window_hours=24)

        after = decide_download_route(self.request, [item], requested_mode="auto")
        self.assertEqual(before["route_type"], after["route_type"])
        self.assertEqual(recommendation["version"], "v4")
        self.assertEqual(recommendation["mode"], "recommendation_only")
        self.assertFalse(recommendation["automatic_execution"])
        self.assertFalse(recommendation["production_behavior_changed"])
        self.assertEqual(recommendation["suggestions"][0]["safety"]["decision"], SAFETY_REVIEW)
        self.assertIn(
            "insufficient-feedback-sample",
            recommendation["suggestions"][0]["safety"]["reason_chain"],
        )

    def test_policy_v4_safety_guard_can_approve_low_risk_adaptive_suggestion(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_adaptive_min_confidence = 0.1
        setting.save(update_fields=["v4_adaptive_min_confidence", "updated_at"])
        for index in range(8):
            DownloadRequestLog.objects.create(
                request_id=f"rid-v4-approve-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="auto",
                route_type=ROUTE_DIRECT,
                final_policy_route=ROUTE_DIRECT,
                provider="native",
                estimated_bytes=1000,
                actual_bytes=900,
                browser_egress_estimated_bytes=1000,
            )

        recommendation = build_policy_evolution_recommendation(
            window_hours=24,
            runtime_setting=setting,
        )

        suggestion = recommendation["suggestions"][0]
        self.assertEqual(suggestion["safety"]["decision"], SAFETY_APPROVE)
        self.assertEqual(suggestion["safety"]["risk"], "low")
        self.assertFalse(suggestion["automatic_execution"])
        self.assertEqual(recommendation["safety_summary"][SAFETY_APPROVE], 1)

    def test_policy_v4_safety_guard_rejects_automatic_execution(self):
        safety = validate_policy_suggestion({
            "kind": "adaptive_policy_weights",
            "automatic_execution": True,
            "proposed_change": {
                "weights": {"cost": 0.4, "provider": 0.4, "routing": 0.2},
            },
        })

        self.assertEqual(safety["decision"], SAFETY_REJECT)
        self.assertEqual(safety["risk"], "high")
        self.assertIn("automatic-execution-is-not-allowed-in-v4", safety["reason_chain"])

    def test_policy_v5_queue_auto_applies_low_risk_without_runtime_mutation_by_default(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_adaptive_min_confidence = 0.1
        setting.v4_auto_weights_enabled = False
        setting.save(update_fields=[
            "v4_adaptive_min_confidence",
            "v4_auto_weights_enabled",
            "updated_at",
        ])
        for index in range(8):
            DownloadRequestLog.objects.create(
                request_id=f"rid-v5-auto-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="auto",
                route_type=ROUTE_DIRECT,
                final_policy_route=ROUTE_DIRECT,
                provider="native",
                estimated_bytes=1000,
                actual_bytes=900,
                browser_egress_estimated_bytes=1000,
            )

        result = build_policy_optimization_queue(
            window_hours=24,
            auto_apply_low_risk=True,
            allow_runtime_mutation=False,
        )

        proposal = PolicyOptimizationProposal.objects.get()
        setting.refresh_from_db()
        self.assertEqual(result["version"], "v5")
        self.assertEqual(result["auto_applied_count"], 1)
        self.assertEqual(proposal.status, PolicyOptimizationStatus.AUTO_APPLIED)
        self.assertTrue(proposal.automatic_execution)
        self.assertFalse(proposal.runtime_mutated)
        self.assertFalse(setting.v4_auto_weights_enabled)
        self.assertFalse(result["production_behavior_changed"])

    def test_policy_v5_queue_can_mutate_runtime_when_explicitly_allowed(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_adaptive_min_confidence = 0.1
        setting.v4_auto_weights_enabled = False
        setting.save(update_fields=[
            "v4_adaptive_min_confidence",
            "v4_auto_weights_enabled",
            "updated_at",
        ])
        for index in range(8):
            DownloadRequestLog.objects.create(
                request_id=f"rid-v5-mutate-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="auto",
                route_type=ROUTE_DIRECT,
                final_policy_route=ROUTE_DIRECT,
                provider="native",
                estimated_bytes=1000,
                actual_bytes=900,
                browser_egress_estimated_bytes=1000,
            )

        result = build_policy_optimization_queue(
            window_hours=24,
            auto_apply_low_risk=True,
            allow_runtime_mutation=True,
        )

        proposal = PolicyOptimizationProposal.objects.get()
        setting.refresh_from_db()
        self.assertEqual(proposal.status, PolicyOptimizationStatus.AUTO_APPLIED)
        self.assertTrue(proposal.runtime_mutated)
        self.assertTrue(setting.v4_auto_weights_enabled)
        self.assertTrue(result["production_behavior_changed"])

    def test_policy_v5_queue_requires_approval_when_auto_apply_disabled(self):
        suggestion = {
            "kind": "adaptive_policy_weights",
            "policy_area": "policy_weights",
            "title": "调整 Policy Engine 权重",
            "body": "low risk",
            "proposed_change": {
                "weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19},
                "delta": {"cost": 0.02, "provider": -0.01, "routing": -0.01},
            },
            "confidence": 0.8,
            "sample_count": 20,
            "reason_chain": ["test-low-risk"],
            "automatic_execution": False,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        }

        proposal = create_policy_optimization_proposal(
            suggestion,
            auto_apply_low_risk=False,
        )

        self.assertEqual(proposal.status, PolicyOptimizationStatus.NEEDS_APPROVAL)
        self.assertFalse(proposal.automatic_execution)

    def test_policy_v5_drift_control_forces_human_review(self):
        for index in range(3):
            PolicyOptimizationProposal.objects.create(
                source_version="v5",
                status=PolicyOptimizationStatus.AUTO_APPLIED,
                kind="adaptive_policy_weights",
                policy_area="policy_weights",
                title="existing",
                risk="low",
                safety_decision=SAFETY_APPROVE,
                confidence=0.8,
                sample_count=20,
                proposed_change={"weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19}},
                safety={"decision": SAFETY_APPROVE, "risk": "low"},
                drift={},
                signature=f"{index}".zfill(64),
                drift_key="policy_weights:adaptive_policy_weights",
                automatic_execution=True,
            )
        suggestion = {
            "kind": "adaptive_policy_weights",
            "policy_area": "policy_weights",
            "title": "调整 Policy Engine 权重",
            "body": "low risk",
            "proposed_change": {
                "weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19},
                "delta": {"cost": 0.02, "provider": -0.01, "routing": -0.01},
            },
            "confidence": 0.8,
            "sample_count": 20,
            "reason_chain": ["test-drift"],
            "automatic_execution": False,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        }

        drift = evaluate_policy_drift(suggestion)
        proposal = create_policy_optimization_proposal(suggestion)

        self.assertEqual(drift["decision"], SAFETY_REVIEW)
        self.assertEqual(proposal.status, PolicyOptimizationStatus.NEEDS_APPROVAL)
        self.assertIn("policy-drift-threshold", proposal.drift["reason"])

    def test_policy_v5_queue_rejects_high_risk_suggestion(self):
        suggestion = {
            "kind": "adaptive_policy_weights",
            "policy_area": "policy_weights",
            "title": "unsafe",
            "body": "unsafe",
            "proposed_change": {
                "weights": {"cost": 0.4, "provider": 0.4, "routing": 0.2},
            },
            "confidence": 1.0,
            "sample_count": 20,
            "automatic_execution": True,
            "safety": {"decision": SAFETY_REJECT, "risk": "high"},
        }

        proposal = create_policy_optimization_proposal(suggestion)

        self.assertEqual(proposal.status, PolicyOptimizationStatus.REJECTED)
        self.assertFalse(proposal.automatic_execution)

    def test_policy_control_risk_scoring_classifies_impact_areas(self):
        provider_risk = score_policy_risk({
            "kind": "provider_priority_change",
            "policy_area": "provider",
            "proposed_change": {"provider": "kukutool"},
            "sample_count": 20,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        })
        routing_risk = score_policy_risk({
            "kind": "routing_weight_change",
            "policy_area": "routing",
            "proposed_change": {
                "delta": {"cost": 0.02, "provider": -0.01, "routing": -0.01},
            },
            "sample_count": 20,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        })
        cost_risk = score_policy_risk({
            "kind": "cost_limit_adjustment",
            "policy_area": "cost",
            "proposed_change": {"estimated_cost_bytes": 1024 * 1024 * 1024},
            "sample_count": 20,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        })

        self.assertEqual(provider_risk["impact_area"], "provider")
        self.assertEqual(provider_risk["risk_level"], "high")
        self.assertEqual(routing_risk["impact_area"], "routing")
        self.assertEqual(routing_risk["risk_level"], "medium")
        self.assertEqual(cost_risk["impact_area"], "cost")
        self.assertEqual(cost_risk["risk_level"], "high")

    def test_policy_control_layer_decides_auto_apply_approval_and_reject(self):
        low_risk = {
            "kind": "adaptive_policy_weights",
            "policy_area": "policy_weights",
            "title": "low",
            "proposed_change": {
                "weights": {"cost": 0.41, "provider": 0.39, "routing": 0.2},
                "delta": {"cost": 0.01, "provider": -0.01, "routing": 0},
            },
            "sample_count": 20,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        }
        provider_change = {
            "kind": "provider_priority_change",
            "policy_area": "provider",
            "title": "provider",
            "proposed_change": {"provider": "kukutool"},
            "sample_count": 20,
            "safety": {"decision": SAFETY_APPROVE, "risk": "low"},
        }
        unsafe = {
            "kind": "adaptive_policy_weights",
            "policy_area": "policy_weights",
            "title": "unsafe",
            "proposed_change": {},
            "automatic_execution": True,
            "sample_count": 20,
            "safety": {"decision": SAFETY_REJECT, "risk": "high"},
        }

        low_result = evaluate_policy_control_layer([low_risk])
        provider_result = evaluate_policy_control_layer([provider_change])
        unsafe_result = evaluate_policy_control_layer([unsafe])

        self.assertEqual(low_result["decision"], CONTROL_AUTO_APPLY)
        self.assertEqual(provider_result["decision"], CONTROL_REQUIRE_APPROVAL)
        self.assertEqual(unsafe_result["decision"], CONTROL_REJECT)

    def test_policy_control_generates_proposals_without_runtime_mutation(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_adaptive_min_confidence = 0.1
        setting.v4_auto_weights_enabled = False
        setting.save(update_fields=[
            "v4_adaptive_min_confidence",
            "v4_auto_weights_enabled",
            "updated_at",
        ])
        for index in range(8):
            DownloadRequestLog.objects.create(
                request_id=f"rid-control-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="auto",
                route_type=ROUTE_DIRECT,
                final_policy_route=ROUTE_DIRECT,
                provider="native",
                estimated_bytes=1000,
                actual_bytes=900,
                browser_egress_estimated_bytes=1000,
            )

        result = create_controlled_policy_proposals(window_hours=24)

        proposal = PolicyOptimizationProposal.objects.get()
        setting.refresh_from_db()
        self.assertEqual(result["proposal_count"], 1)
        self.assertEqual(proposal.control_decision, CONTROL_AUTO_APPLY)
        self.assertEqual(proposal.status, PolicyOptimizationStatus.AUTO_APPLIED)
        self.assertFalse(proposal.runtime_mutated)
        self.assertFalse(setting.v4_auto_weights_enabled)
        self.assertFalse(proposal.application_result["behavior_changed"])

    def test_policy_approval_flow_and_rollback(self):
        proposal = PolicyOptimizationProposal.objects.create(
            source_version="v5",
            status=PolicyOptimizationStatus.NEEDS_APPROVAL,
            kind="adaptive_policy_weights",
            policy_area="policy_weights",
            title="review",
            risk="medium",
            risk_score=0.5,
            impact_area="routing",
            control_decision=CONTROL_REQUIRE_APPROVAL,
            safety_decision=SAFETY_REVIEW,
            confidence=0.8,
            sample_count=20,
            proposed_change={"weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19}},
            safety={"decision": SAFETY_REVIEW, "risk": "medium"},
            drift={},
            signature="a" * 64,
            drift_key="policy_weights:adaptive_policy_weights",
        )

        approved = approve_policy_change(proposal, actor_username="admin")
        proposal.refresh_from_db()
        self.assertTrue(approved["ok"])
        self.assertEqual(proposal.status, PolicyOptimizationStatus.APPROVED)
        self.assertEqual(proposal.actor_username, "admin")

        rejected = reject_policy_change(proposal, actor_username="admin", reason="not-needed")
        proposal.refresh_from_db()
        self.assertTrue(rejected["ok"])
        self.assertEqual(proposal.status, PolicyOptimizationStatus.REJECTED)

    def test_policy_control_rollback_restores_runtime_setting(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_auto_weights_enabled = True
        setting.v4_adaptive_shadow_enabled = True
        setting.save(update_fields=[
            "v4_auto_weights_enabled",
            "v4_adaptive_shadow_enabled",
            "updated_at",
        ])
        proposal = PolicyOptimizationProposal.objects.create(
            source_version="v5",
            status=PolicyOptimizationStatus.APPROVED,
            kind="adaptive_policy_weights",
            policy_area="policy_weights",
            title="applied",
            risk="low",
            risk_score=0.2,
            impact_area="routing",
            control_decision=CONTROL_AUTO_APPLY,
            safety_decision=SAFETY_APPROVE,
            confidence=0.8,
            sample_count=20,
            proposed_change={"weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19}},
            safety={"decision": SAFETY_APPROVE, "risk": "low"},
            drift={},
            signature="b" * 64,
            drift_key="policy_weights:adaptive_policy_weights",
            runtime_mutated=True,
            application_result={
                "before": {
                    "v4_auto_weights_enabled": False,
                    "v4_adaptive_shadow_enabled": True,
                },
                "after": {
                    "v4_auto_weights_enabled": True,
                    "v4_adaptive_shadow_enabled": True,
                },
            },
        )

        result = rollback_policy_change(proposal, actor_username="admin")

        proposal.refresh_from_db()
        setting.refresh_from_db()
        self.assertTrue(result["behavior_changed"])
        self.assertFalse(setting.v4_auto_weights_enabled)
        self.assertEqual(proposal.status, PolicyOptimizationStatus.ROLLED_BACK)
        self.assertFalse(proposal.runtime_mutated)

    def test_policy_control_admin_page_requires_permission_and_superuser_can_generate(self):
        response = Client().get("/admin/policy-control/")
        self.assertEqual(response.status_code, 404)

        user = User.objects.create_superuser(
            username="policy-admin",
            email="policy@example.com",
            password="pw",
        )
        client = Client()
        client.force_login(user)

        get_response = client.get("/admin/policy-control/")
        self.assertEqual(get_response.status_code, 200)
        post_response = client.post("/admin/policy-control/", {"action": "generate", "window": "24"})
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response["Location"], "/admin/policy-control/")

    def test_control_state_machine_transition_correctness(self):
        record = create_control_record(
            policy_decision=ROUTE_DIRECT,
            control_decision=CONTROL_REQUIRE_APPROVAL,
            source="test",
        )

        self.assertEqual(record.state, PolicyControlState.PENDING)
        transition_control_record(record, PolicyControlState.APPROVED, reason="approved")
        record.refresh_from_db()
        self.assertEqual(record.state, PolicyControlState.APPROVED)
        transition_control_record(record, PolicyControlState.EXECUTED, reason="executed")
        record.refresh_from_db()
        self.assertEqual(record.state, PolicyControlState.EXECUTED)
        self.assertGreaterEqual(len(record.transition_history), 3)
        with self.assertRaises(ValueError):
            transition_control_record(record, PolicyControlState.APPROVED, reason="invalid")

    def test_control_approval_execution_feedback_loop(self):
        proposal = PolicyOptimizationProposal.objects.create(
            source_version="v5",
            status=PolicyOptimizationStatus.NEEDS_APPROVAL,
            kind="adaptive_policy_weights",
            policy_area="policy_weights",
            title="review",
            risk="medium",
            risk_score=0.5,
            impact_area="routing",
            control_decision=CONTROL_REQUIRE_APPROVAL,
            safety_decision=SAFETY_REVIEW,
            confidence=0.8,
            sample_count=20,
            proposed_change={"weights": {"cost": 0.42, "provider": 0.39, "routing": 0.19}},
            safety={"decision": SAFETY_REVIEW, "risk": "medium"},
            drift={},
            signature="c" * 64,
            drift_key="policy_weights:adaptive_policy_weights",
        )
        control_record = record_control_decision_for_proposal(
            proposal,
            risk={"risk_score": 0.5, "risk_level": "medium"},
            gate={"reason": "human-approval-required"},
        )

        self.assertEqual(control_record.state, PolicyControlState.PENDING)
        approve_policy_change(proposal, actor_username="admin")
        control_record.refresh_from_db()
        self.assertEqual(control_record.state, PolicyControlState.APPROVED)

        executed = record_control_execution(
            proposal=proposal,
            request_id="rid-control-loop",
            policy_decision=ROUTE_DIRECT,
            control_decision=CONTROL_REQUIRE_APPROVAL,
            status=ObservabilityStatus.SUCCESS,
            provider="native",
            route=ROUTE_DIRECT,
            cost=900,
            policy_confidence=0.8,
        )
        self.assertEqual(executed.id, control_record.id)
        self.assertEqual(executed.state, PolicyControlState.EXECUTED)

        feedback = apply_control_feedback_to_records(window_hours=24)
        executed.refresh_from_db()
        self.assertEqual(feedback["updated"], 1)
        self.assertEqual(executed.feedback_summary["route_success_rate"], 1.0)
        self.assertGreater(executed.control_confidence, 0)

    def test_control_execution_cannot_bypass_pending_approval(self):
        proposal = PolicyOptimizationProposal.objects.create(
            source_version="v5",
            status=PolicyOptimizationStatus.NEEDS_APPROVAL,
            kind="provider_priority_change",
            policy_area="provider",
            title="provider review",
            risk="high",
            risk_score=0.7,
            impact_area="provider",
            control_decision=CONTROL_REQUIRE_APPROVAL,
            safety_decision=SAFETY_REVIEW,
            confidence=0.8,
            sample_count=20,
            proposed_change={"provider": "kukutool"},
            safety={"decision": SAFETY_REVIEW, "risk": "medium"},
            drift={},
            signature="d" * 64,
            drift_key="provider:provider_priority_change",
        )
        record_control_decision_for_proposal(
            proposal,
            risk={"risk_score": 0.7, "risk_level": "high"},
            gate={"reason": "human-approval-required"},
        )

        with self.assertRaises(ValueError):
            record_control_execution(
                proposal=proposal,
                request_id="rid-control-bypass",
                policy_decision=ROUTE_DIRECT,
                control_decision=CONTROL_REQUIRE_APPROVAL,
                status=ObservabilityStatus.SUCCESS,
                provider="kukutool",
                route=ROUTE_DIRECT,
                cost=1000,
            )

    def test_control_failure_execution_tracking(self):
        record = record_control_execution(
            request_id="rid-control-failure",
            policy_decision=ROUTE_PROXY,
            control_decision="download_policy_observed",
            status=ObservabilityStatus.FAILED,
            provider="kukutool",
            route=ROUTE_PROXY,
            cost=1234,
            error_type="TimeoutError",
            failure_type="provider_timeout",
        )

        self.assertEqual(record.state, PolicyControlState.FAILED)
        self.assertEqual(record.execution_result, "failed")
        self.assertIn("provider_timeout", record.failure_pattern)

    def test_control_convergence_detects_policy_drift(self):
        record_control_execution(
            request_id="rid-control-drift",
            policy_decision=ROUTE_DIRECT,
            control_decision="policy_v5",
            status=ObservabilityStatus.SUCCESS,
            provider="native",
            route=ROUTE_ZIP,
            cost=5000,
            policy_confidence=0.9,
        )

        snapshot = build_control_convergence_snapshot(window_hours=24)

        self.assertEqual(snapshot["total_records"], 1)
        self.assertLess(snapshot["control_drift_score"], 1.0)
        self.assertEqual(snapshot["policy_consistency_score"], 1.0)
        self.assertTrue(snapshot["suggestions"])

    def test_control_no_orphan_decision_after_control_generation(self):
        setting = PolicyRuntimeSetting.load()
        setting.v4_adaptive_min_confidence = 0.1
        setting.v4_auto_weights_enabled = False
        setting.save(update_fields=[
            "v4_adaptive_min_confidence",
            "v4_auto_weights_enabled",
            "updated_at",
        ])
        for index in range(8):
            DownloadRequestLog.objects.create(
                request_id=f"rid-control-no-orphan-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="auto",
                route_type=ROUTE_DIRECT,
                final_policy_route=ROUTE_DIRECT,
                provider="native",
                estimated_bytes=1000,
                actual_bytes=900,
                browser_egress_estimated_bytes=1000,
            )

        result = create_controlled_policy_proposals(window_hours=24)

        self.assertEqual(result["proposal_count"], 1)
        self.assertEqual(PolicyControlRecord.objects.count(), 1)
        self.assertEqual(find_orphan_control_decisions(window_hours=24), [])

    def test_control_convergence_admin_page_requires_permission(self):
        response = Client().get("/admin/control-convergence/")
        self.assertEqual(response.status_code, 404)

        user = User.objects.create_superuser(
            username="control-admin",
            email="control@example.com",
            password="pw",
        )
        client = Client()
        client.force_login(user)

        response = client.get("/admin/control-convergence/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Control Convergence")

    def test_observability_v2_metrics_are_computed_from_policy_feedback(self):
        DownloadRequestLog.objects.create(
            request_id="rid-intel-direct",
            status=ObservabilityStatus.SUCCESS,
            download_type="auto",
            route_type=ROUTE_DIRECT,
            final_policy_route=ROUTE_DIRECT,
            policy_cost_score=0.9,
            policy_provider_score=0.8,
            policy_routing_score=0.7,
            policy_confidence=0.8,
            provider="native",
            estimated_bytes=1000,
            actual_bytes=900,
            browser_egress_estimated_bytes=1000,
        )
        DownloadRequestLog.objects.create(
            request_id="rid-intel-conflict",
            status=ObservabilityStatus.FAILED,
            download_type="zip",
            route_type=ROUTE_ZIP,
            final_policy_route=ROUTE_DIRECT,
            policy_cost_score=0.4,
            policy_provider_score=0.5,
            policy_routing_score=0.6,
            policy_confidence=0.5,
            provider="yt_dlp_generic",
            estimated_bytes=1000,
            actual_bytes=500,
            server_egress_estimated_bytes=1000,
            fallback_count=1,
            failure_type=FAILURE_PROVIDER_TIMEOUT,
        )
        DownloadRequestLog.objects.create(
            request_id="rid-intel-proxy",
            status=ObservabilityStatus.SUCCESS,
            download_type="proxy",
            route_type=ROUTE_PROXY,
            final_policy_route=ROUTE_PROXY,
            policy_cost_score=0.7,
            policy_provider_score=0.6,
            policy_routing_score=0.8,
            policy_confidence=0.72,
            provider="yt_dlp_generic",
            estimated_bytes=1000,
            actual_bytes=1000,
            server_egress_estimated_bytes=1000,
        )

        context = build_observability_v2_context(window_hours=24)

        self.assertEqual(context["download_count"], 3)
        self.assertAlmostEqual(context["metrics"]["policy_conflict_rate"], 1 / 3, places=3)
        self.assertAlmostEqual(context["metrics"]["fallback_frequency"], 1 / 3, places=3)
        self.assertAlmostEqual(context["metrics"]["provider_reliability"], 2 / 3, places=3)
        self.assertAlmostEqual(context["metrics"]["routing_efficiency"], 0.4444, places=3)
        self.assertAlmostEqual(context["metrics"]["cost_accuracy"], 0.8, places=3)
        self.assertFalse(context["recommendation"]["automatic_execution"])
        self.assertEqual(context["recommendation"]["mode"], "recommendation_only")
        self.assertEqual(context["policy"]["policy_count"], 3)
        self.assertEqual(context["policy"]["policy_conflict_rate_label"], "33.3%")
        self.assertTrue(
            any(
                row["provider"] == "yt_dlp_generic"
                and row["failure_type"] == FAILURE_PROVIDER_TIMEOUT
                for row in context["provider"]["failure_heatmap"]
            )
        )

    def test_feedback_loop_generates_signals_insights_and_suggestions(self):
        DownloadRequestLog.objects.create(
            request_id="rid-feedback-direct",
            status=ObservabilityStatus.SUCCESS,
            download_type="auto",
            route_type=ROUTE_DIRECT,
            provider="native",
            estimated_bytes=1000,
            actual_bytes=900,
            browser_egress_estimated_bytes=1000,
        )
        DownloadRequestLog.objects.create(
            request_id="rid-feedback-provider-fail",
            status=ObservabilityStatus.FAILED,
            download_type="zip",
            route_type=ROUTE_ZIP,
            provider="yt_dlp_generic",
            estimated_bytes=1000,
            actual_bytes=500,
            server_egress_estimated_bytes=1000,
            fallback_count=1,
            failure_type=FAILURE_PROVIDER_TIMEOUT,
        )
        ParseRequestLog.objects.create(
            request_id="rid-feedback-parse-fail",
            status=ObservabilityStatus.FAILED,
            selected_provider="yt_dlp_generic",
            error_type="ProviderParseError",
        )
        ProviderHealthTestRun.objects.create(
            request_id="rid-feedback-health-fail",
            test_key="third_party_ytdlp",
            status=ProviderHealthTestStatus.FAILED,
            provider="yt_dlp_generic",
            error_type="ProviderHealthTimeout",
            duration_ms=9000,
        )
        RoutingDecisionLog.objects.create(
            request_id="rid-feedback-route",
            route_type=ROUTE_ZIP,
            provider="yt_dlp_generic",
            success=False,
            duration_ms=5000,
        )

        feedback = build_feedback(window_hours=24)
        insights = build_insights(feedback)
        suggestions = build_suggestions(insights)

        self.assertEqual(feedback["provider"], "all")
        self.assertEqual(feedback["signals"]["download_count"], 2)
        self.assertEqual(feedback["signals"]["parse_count"], 1)
        self.assertEqual(feedback["signals"]["health_count"], 1)
        self.assertAlmostEqual(feedback["signals"]["failure_rate"], 0.75)
        self.assertEqual(feedback["routes"][ROUTE_ZIP]["download_count"], 1)
        self.assertEqual(feedback["routes"][ROUTE_ZIP]["fallback_rate_label"], "100.0%")
        self.assertTrue(
            any(
                row["provider"] == "yt_dlp_generic"
                and row["failure_rate_label"] == "100.0%"
                for row in feedback["signals"]["provider_signals"]
            )
        )
        self.assertTrue(
            any(
                row["failure_type"] == FAILURE_PROVIDER_TIMEOUT
                for row in feedback["signals"]["failures"]
            )
        )

        insight_kinds = {row["kind"] for row in insights["insights"]}
        self.assertIn("failure_increase", insight_kinds)
        self.assertIn("provider_degraded", insight_kinds)
        self.assertIn("route_inefficiency", insight_kinds)
        self.assertTrue(suggestions["suggestions"])
        self.assertTrue(
            all(not row["automatic_execution"] for row in suggestions["suggestions"])
        )

    def test_feedback_loop_does_not_change_download_routing_behavior(self):
        item = {"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg", "size_bytes": 321}

        before = decide_download_route(self.request, [item], requested_mode="auto")
        context = build_feedback_loop_context(window_hours=24)
        after = decide_download_route(self.request, [item], requested_mode="auto")

        self.assertEqual(before["route_type"], after["route_type"])
        self.assertEqual(before["download_strategy"], after["download_strategy"])
        self.assertEqual(context["suggestion"]["suggestions"][0]["automatic_execution"], False)


class RuntimeHealthViewTests(SimpleTestCase):
    @override_settings(
        DEBUG=False,
        ALLOWED_HOSTS=["download.example.com"],
        SECURE_SSL_REDIRECT=True,
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    )
    def test_production_security_settings_are_available(self):
        from django.conf import settings as django_settings

        self.assertFalse(django_settings.DEBUG)
        self.assertIn("download.example.com", django_settings.ALLOWED_HOSTS)
        self.assertTrue(django_settings.SECURE_SSL_REDIRECT)
        self.assertTrue(django_settings.SESSION_COOKIE_SECURE)
        self.assertTrue(django_settings.CSRF_COOKIE_SECURE)
        self.assertEqual(
            django_settings.SECURE_PROXY_SSL_HEADER,
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )

    def test_liveness_does_not_depend_on_external_services(self):
        response = Client().get("/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_provider_health_is_hidden_from_anonymous_users(self):
        response = Client().get("/admin/provider-health/")

        self.assertEqual(response.status_code, 404)

    @patch("djangoProject.health.get_runtime_readiness")
    def test_readiness_reports_available_components(self, mock_readiness):
        mock_readiness.return_value = {
            "ready": True,
            "components": {"cache": True, "database": True},
        }

        response = Client().get("/health/ready/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            response.json()["components"],
            {"cache": True, "database": True},
        )

    @patch("djangoProject.health.get_runtime_readiness")
    def test_readiness_returns_503_without_exposing_error_details(self, mock_readiness):
        mock_readiness.return_value = {
            "ready": False,
            "components": {"cache": False, "database": True},
        }

        response = Client().get("/health/ready/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")
        self.assertNotIn("error", response.json())


class RuntimeHealthCheckTests(TestCase):
    def test_runtime_readiness_checks_cache_and_database(self):
        from djangoProject.health import get_runtime_readiness

        readiness = get_runtime_readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(
            readiness["components"],
            {"cache": True, "database": True},
        )

    @override_settings(DJANGO_CACHE_URL="")
    @patch("xhs_download.checks.importlib.util.find_spec")
    def test_system_check_does_not_require_redis_package_without_redis_cache(
        self,
        mock_find_spec,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertNotIn("xhs_download.E008", issue_ids)
        mock_find_spec.assert_not_called()

    @override_settings(DJANGO_CACHE_URL="redis://127.0.0.1:6379/0")
    @patch("xhs_download.checks.importlib.util.find_spec", return_value=None)
    def test_system_check_reports_missing_redis_package_for_redis_cache(
        self,
        mock_find_spec,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E008", issue_ids)
        mock_find_spec.assert_called_once_with("redis")

    @override_settings(DJANGO_CACHE_URL="redis://127.0.0.1:6379/0")
    @patch("xhs_download.checks.importlib.util.find_spec", return_value=object())
    def test_system_check_accepts_installed_redis_package_for_redis_cache(
        self,
        mock_find_spec,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertNotIn("xhs_download.E008", issue_ids)
        mock_find_spec.assert_called_once_with("redis")

    @override_settings(REQUIRE_PRODUCTION_CONFIG=True, DEBUG=False)
    @patch("xhs_download.services.platforms.douyin_kukutool.shutil.which")
    @patch("xhs_download.services.platforms.douyin_kukutool.subprocess.run")
    def test_find_node_runtime_skips_development_fallbacks_in_production(
        self,
        mock_run,
        mock_which,
    ):
        from .services.platforms.douyin_kukutool import find_node_runtime

        self.assertEqual(find_node_runtime(""), "")
        mock_which.assert_not_called()
        mock_run.assert_not_called()

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/path/that/does/not/exist/yt-dlp",
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    def test_system_check_reports_missing_ytdlp_executable(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E004", issue_ids)

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/path/that/does/not/exist/yt-dlp",
        PROVIDER_STRICT_CAPABILITY_CHECKS=False,
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    def test_provider_dependency_check_defaults_to_warning(self):
        issues = {issue.id: issue for issue in run_checks()}

        self.assertIsInstance(issues["xhs_download.E004"], CheckWarning)

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/path/that/does/not/exist/yt-dlp",
        PROVIDER_STRICT_CAPABILITY_CHECKS=True,
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    def test_provider_dependency_check_can_be_strict_error(self):
        issues = {issue.id: issue for issue in run_checks()}

        self.assertIsInstance(issues["xhs_download.E004"], CheckError)

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/usr/bin/python -m missing_yt_dlp_module",
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": False,
            "version_output": "",
            "error": "No module named missing_yt_dlp_module",
        },
    )
    def test_system_check_reports_ytdlp_version_probe_failure(
        self,
        mock_probe,
        _mock_access,
        _mock_exists,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E004", issue_ids)
        mock_probe.assert_called_once()

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="/usr/bin/python -m yt_dlp",
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "2026.03.17",
            "error": "",
        },
    )
    def test_system_check_accepts_ytdlp_version_probe_success(
        self,
        mock_probe,
        _mock_access,
        _mock_exists,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertNotIn("xhs_download.E004", issue_ids)
        mock_probe.assert_called_once()

    @override_settings(
        YTDLP_PROVIDER_ENABLED=False,
        YTDLP_BINARY="/usr/bin/python -m yt_dlp",
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    @patch("xhs_download.services.platforms.ytdlp_generic.probe_external_command_version")
    def test_ytdlp_health_does_not_probe_when_provider_is_disabled(
        self,
        mock_probe,
        _mock_access,
        _mock_exists,
    ):
        health = YtDlpGenericProvider().health()

        self.assertFalse(health["enabled"])
        self.assertTrue(health["ready"])
        self.assertIsNone(health["version_probe_ok"])
        mock_probe.assert_not_called()

    @override_settings(
        YTDLP_PROVIDER_ENABLED=False,
        YTDLP_BINARY="",
    )
    @patch("xhs_download.services.platforms.external_tools.shutil.which", return_value=None)
    @patch("xhs_download.services.platforms.ytdlp_generic.probe_external_command_version")
    def test_ytdlp_health_not_ready_when_disabled_binary_is_missing(
        self,
        mock_probe,
        _mock_which,
    ):
        health = YtDlpGenericProvider().health()

        self.assertFalse(health["enabled"])
        self.assertFalse(health["ready"])
        self.assertFalse(health["executable_found"])
        self.assertIsNone(health["version_probe_ok"])
        mock_probe.assert_not_called()

    @override_settings(
        GALLERYDL_PROVIDER_ENABLED=True,
        GALLERYDL_BINARY="/usr/bin/python -m gallery_dl",
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    @patch(
        "xhs_download.services.platforms.gallerydl_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": False,
            "version_output": "",
            "error": "No module named gallery_dl",
        },
    )
    def test_gallerydl_health_reports_not_ready_when_version_probe_fails(
        self,
        mock_probe,
        _mock_access,
        _mock_exists,
    ):
        health = GalleryDlGenericProvider().health()

        self.assertTrue(health["enabled"])
        self.assertFalse(health["ready"])
        self.assertFalse(health["version_probe_ok"])
        self.assertEqual(health["version_probe_error"], "No module named gallery_dl")
        mock_probe.assert_called_once()

    @override_settings(
        YTDLP_PROVIDER_ENABLED=True,
        YTDLP_BINARY="",
        REQUIRE_PRODUCTION_CONFIG=True,
    )
    def test_system_check_requires_explicit_ytdlp_binary_in_production(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E003", issue_ids)

    @override_settings(
        GALLERYDL_PROVIDER_ENABLED=True,
        GALLERYDL_BINARY="",
        REQUIRE_PRODUCTION_CONFIG=True,
    )
    def test_system_check_requires_explicit_gallerydl_binary_in_production(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E005", issue_ids)

    @override_settings(
        GALLERYDL_PROVIDER_ENABLED=True,
        GALLERYDL_BINARY="/path/that/does/not/exist/gallery-dl",
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    def test_system_check_reports_missing_gallerydl_executable(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E006", issue_ids)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
    )
    def test_system_check_reports_public_third_party_without_enabled_provider(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E007", issue_ids)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
    )
    def test_system_check_requires_concrete_provider_when_capability_is_enabled(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E007", issue_ids)

    @override_settings(
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True,
        YTDLP_BINARY="/usr/bin/python -m yt_dlp",
        YOUTUBE_MERGE_FFMPEG_BINARY="/usr/bin/ffmpeg",
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    @patch(
        "xhs_download.services.youtube_merge.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "ok",
            "error": "",
        },
    )
    @patch(
        "xhs_download.services.platforms.ytdlp_generic.probe_external_command_version",
        return_value={
            "version_probe_ok": True,
            "version_output": "2026.03.17",
            "error": "",
        },
    )
    @patch("xhs_download.services.platforms.external_tools.os.path.exists", return_value=True)
    @patch("xhs_download.services.platforms.external_tools.os.access", return_value=True)
    def test_system_check_allows_public_youtube_merge_when_runtime_is_ready(
        self,
        _mock_access,
        _mock_exists,
        _mock_ytdlp_probe,
        _mock_ffmpeg_probe,
    ):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertNotIn("xhs_download.E013", issue_ids)
        self.assertNotIn("xhs_download.E010", issue_ids)
        self.assertNotIn("xhs_download.E012", issue_ids)

    @override_settings(
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=False,
        YOUTUBE_MERGE_REQUIRE_FFMPEG=True,
        YTDLP_BINARY="",
        YOUTUBE_MERGE_FFMPEG_BINARY="",
        REQUIRE_PRODUCTION_CONFIG=True,
    )
    def test_system_check_requires_explicit_youtube_merge_binaries_in_production(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertIn("xhs_download.E009", issue_ids)
        self.assertIn("xhs_download.E011", issue_ids)


class ProviderControlViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="provider-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(self.user)

    def test_staff_can_disable_and_enable_kukutool(self):
        disabled = self.client.post(
            "/admin/provider-health/control/",
            data=json.dumps({"action": "disable"}),
            content_type="application/json",
        )
        health = self.client.get("/admin/provider-health/")
        enabled = self.client.post(
            "/admin/provider-health/control/",
            data=json.dumps({"action": "enable"}),
            content_type="application/json",
        )

        self.assertEqual(disabled.status_code, 200)
        self.assertTrue(disabled.json()["admin_disabled"])
        self.assertTrue(health.json()["admin_disabled"])
        self.assertEqual(enabled.status_code, 200)
        self.assertFalse(enabled.json()["admin_disabled"])
        self.assertIn("third_party_summary", disabled.json())
        self.assertIn("third_party_summary", enabled.json())

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=False,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
        YOUTUBE_MERGE_PROVIDER_ENABLED=False,
        YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=False,
    )
    def test_provider_health_includes_third_party_diagnostics(self):
        source_key = "abc123"
        cache.set(f"third-party:kukutool:failure:{source_key}", "TimeoutError")

        response = self.client.get(
            "/admin/provider-health/",
            {"source_key": source_key},
        )

        self.assertEqual(response.status_code, 200)
        third_party = response.json()["third_party"]
        self.assertEqual(third_party["provider"], "kukutool_generic")
        self.assertEqual(third_party["failure_cache"]["source_key"], source_key)
        self.assertTrue(third_party["failure_cache"]["hit"])
        self.assertEqual(third_party["failure_cache"]["reason"], "TimeoutError")
        self.assertIn("circuit", third_party)
        self.assertIn("single_flight", third_party)
        gallerydl = response.json()["third_party_gallerydl"]
        self.assertEqual(gallerydl["provider"], "gallery_dl_generic")
        self.assertIn("max_items", gallerydl)
        self.assertIn("output_max_bytes", gallerydl)
        self.assertIn("max_pending_tasks", gallerydl)
        self.assertIn("payload_cache_ttl_seconds", gallerydl)
        self.assertIn("failure_cache_ttl_seconds", gallerydl)
        self.assertIn("executable_found", gallerydl)
        self.assertIn("ready", gallerydl)
        self.assertIn("version_probe_ok", gallerydl)
        self.assertIn("version_output", gallerydl)
        self.assertIn("version_probe_error", gallerydl)
        ytdlp = response.json()["third_party_ytdlp"]
        self.assertEqual(ytdlp["provider"], "yt_dlp_generic")
        self.assertIn("output_max_bytes", ytdlp)
        self.assertIn("max_pending_tasks", ytdlp)
        self.assertIn("executor_workers", ytdlp)
        self.assertIn("payload_cache_ttl_seconds", ytdlp)
        self.assertIn("failure_cache_ttl_seconds", ytdlp)
        self.assertIn("executable_found", ytdlp)
        self.assertIn("ready", ytdlp)
        self.assertIn("version_probe_ok", ytdlp)
        self.assertIn("version_output", ytdlp)
        self.assertIn("version_probe_error", ytdlp)
        youtube_merge = response.json()["youtube_merge"]
        self.assertEqual(youtube_merge["provider"], "youtube_merge")
        self.assertTrue(youtube_merge["implemented"])
        self.assertFalse(youtube_merge["enabled"])
        self.assertFalse(youtube_merge["public_ready"])
        self.assertIn("ffmpeg", youtube_merge)
        self.assertIn("ytdlp", youtube_merge)
        summary = response.json()["third_party_summary"]
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["public_access_enabled"])
        self.assertFalse(summary["staff_only"])
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["public_ready"])
        self.assertFalse(summary["staff_ready"])
        self.assertEqual(summary["enabled_provider_ids"], [])
        self.assertEqual(summary["ready_provider_ids"], [])
        self.assertIn("provider_states", summary)
        self.assertIn("kukutool_generic", summary["provider_states"])
        self.assertEqual(summary["issue"], "")
        self.assertEqual(summary["rollout"]["status"], "disabled")
        self.assertEqual(
            summary["rollout"]["reason"],
            "third_party_entrypoint_disabled",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=False,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_summary_keeps_provider_readiness_when_entry_is_disabled(self):
        response = self.client.get("/admin/provider-health/")

        self.assertEqual(response.status_code, 200)
        summary = response.json()["third_party_summary"]
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["public_ready"])
        self.assertFalse(summary["staff_ready"])
        self.assertEqual(summary["enabled_provider_ids"], ["kukutool_generic"])
        self.assertEqual(summary["ready_provider_ids"], ["kukutool_generic"])
        self.assertTrue(summary["provider_states"]["kukutool_generic"]["enabled"])
        self.assertTrue(summary["provider_states"]["kukutool_generic"]["ready"])
        self.assertEqual(summary["issue"], "")
        self.assertEqual(summary["rollout"]["status"], "disabled")
        self.assertEqual(
            summary["rollout"]["reason"],
            "third_party_entrypoint_disabled",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_reports_disabled_third_party_kukutool_provider(self):
        response = self.client.get("/admin/provider-health/")

        self.assertEqual(response.status_code, 200)
        third_party = response.json()["third_party"]
        self.assertFalse(third_party["enabled"])
        self.assertFalse(third_party["provider_enabled"])

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_reports_third_party_summary_issue(self):
        response = self.client.get("/admin/provider-health/")

        self.assertEqual(response.status_code, 200)
        summary = response.json()["third_party_summary"]
        self.assertTrue(summary["enabled"])
        self.assertTrue(summary["public_access_enabled"])
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["public_ready"])
        self.assertEqual(summary["enabled_provider_ids"], [])
        self.assertEqual(summary["ready_provider_ids"], [])
        self.assertEqual(summary["issue"], "no_provider_enabled")
        self.assertEqual(summary["rollout"]["status"], "hold")
        self.assertEqual(summary["rollout"]["reason"], "no_provider_enabled")

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=True,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_summary_ignores_legacy_staff_only_env_gate(self):
        response = self.client.get("/admin/provider-health/")

        self.assertEqual(response.status_code, 200)
        summary = response.json()["third_party_summary"]
        self.assertTrue(summary["enabled"])
        self.assertTrue(summary["public_access_enabled"])
        self.assertFalse(summary["staff_only"])
        self.assertTrue(summary["public_ready"])
        self.assertTrue(summary["staff_ready"])
        self.assertEqual(summary["enabled_provider_ids"], ["kukutool_generic"])
        self.assertEqual(summary["ready_provider_ids"], ["kukutool_generic"])
        self.assertEqual(summary["rollout"]["status"], "public_candidate")
        self.assertEqual(
            summary["rollout"]["reason"],
            "ready_but_requires_manual_public_approval",
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True,
        THIRD_PARTY_STAFF_ONLY=False,
        THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
        YTDLP_PROVIDER_ENABLED=False,
        GALLERYDL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_summary_marks_public_candidate_rollout(self):
        response = self.client.get("/admin/provider-health/")

        self.assertEqual(response.status_code, 200)
        summary = response.json()["third_party_summary"]
        self.assertTrue(summary["public_ready"])
        self.assertTrue(summary["staff_ready"])
        self.assertEqual(summary["rollout"]["status"], "public_candidate")
        self.assertEqual(
            summary["rollout"]["reason"],
            "ready_but_requires_manual_public_approval",
        )

    def test_provider_control_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            "/admin/provider-health/control/",
            data=json.dumps({"action": "disable"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)


class ProviderSettingsAdminViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="provider-settings-admin",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(self.user)

    def test_provider_settings_requires_staff_user(self):
        response = Client().get("/admin/provider-settings/")

        self.assertEqual(response.status_code, 404)

    def test_staff_can_view_provider_settings_dashboard(self):
        response = self.client.get("/admin/provider-settings/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Provider 管理后台")
        self.assertContains(response, "抖音超高清")
        self.assertContains(response, "YouTube 高清合并")
        self.assertContains(response, "通用第三方 Provider")
        self.assertContains(response, "建议先使用 staff 完成 smoke test")
        self.assertContains(response, "第三方 allowlist 为空")
        self.assertContains(response, "public_gray")

    def test_staff_can_update_provider_access_modes(self):
        response = self.client.post(
            "/admin/provider-settings/",
            data={
                "douyin_ultra_mode": ProviderAccessMode.STAFF,
                "youtube_merge_mode": ProviderAccessMode.DISABLED,
                "third_party_mode": ProviderAccessMode.PUBLIC,
                "douyin_ultra_rollout_percent": "5",
                "youtube_merge_rollout_percent": "42",
                "third_party_rollout_percent": "9",
                "douyin_ultra_disabled_reason": "upstream watch",
                "youtube_merge_disabled_reason": "queue watch",
                "third_party_disabled_reason": "smoke only",
                "third_party_allowed_public_platforms": "youtube",
                "third_party_allowed_public_hosts": "youtube.com",
                "third_party_blocked_platforms": "reddit",
                "third_party_blocked_hosts": "youtube.com.evil.com",
                "kukutool_disabled": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        setting = ProviderRuntimeSetting.load()
        self.assertEqual(setting.douyin_ultra_mode, ProviderAccessMode.STAFF)
        self.assertEqual(setting.youtube_merge_mode, ProviderAccessMode.DISABLED)
        self.assertEqual(setting.third_party_mode, ProviderAccessMode.PUBLIC)
        self.assertEqual(setting.douyin_ultra_rollout_percent, 5)
        self.assertEqual(setting.youtube_merge_rollout_percent, 42)
        self.assertEqual(setting.third_party_rollout_percent, 9)
        self.assertEqual(setting.douyin_ultra_disabled_reason, "upstream watch")
        self.assertEqual(setting.youtube_merge_disabled_reason, "queue watch")
        self.assertEqual(setting.third_party_disabled_reason, "smoke only")
        self.assertEqual(setting.third_party_allowed_public_platforms, "youtube")
        self.assertEqual(setting.third_party_allowed_public_hosts, "youtube.com")
        self.assertEqual(setting.third_party_blocked_platforms, "reddit")
        self.assertEqual(setting.third_party_blocked_hosts, "youtube.com.evil.com")
        self.assertTrue(setting.kukutool_disabled)

    def test_provider_runtime_defaults_are_conservative(self):
        setting = ProviderRuntimeSetting.load()

        self.assertEqual(setting.douyin_ultra_mode, ProviderAccessMode.DISABLED)
        self.assertEqual(setting.youtube_merge_mode, ProviderAccessMode.DISABLED)
        self.assertEqual(setting.third_party_mode, ProviderAccessMode.DISABLED)
        self.assertEqual(setting.rollout_percent, 0)
        self.assertEqual(setting.douyin_ultra_rollout_percent, 0)
        self.assertEqual(setting.youtube_merge_rollout_percent, 0)
        self.assertEqual(setting.third_party_rollout_percent, 0)
        self.assertEqual(setting.third_party_allowed_public_platforms, "")
        self.assertEqual(setting.third_party_allowed_public_hosts, "")
        self.assertEqual(setting.third_party_blocked_platforms, "")
        self.assertEqual(setting.third_party_blocked_hosts, "")

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_capability_enabled_admin_disabled_is_not_available(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.DISABLED
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False)

        self.assertFalse(third_party_provider_is_available(request))

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_capability_enabled_admin_staff_allows_staff_only(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")

        request.user = Mock(is_active=False, is_staff=False)
        self.assertFalse(third_party_provider_is_available(request))

        request.user = Mock(is_active=True, is_staff=True)
        self.assertTrue(third_party_provider_is_available(request))

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_capability_enabled_public_gray_zero_blocks_visitor(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.third_party_rollout_percent = 0
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_rollout_percent",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False)

        self.assertFalse(third_party_provider_is_available(request))

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_capability_enabled_public_gray_hundred_allows_visitor(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.third_party_rollout_percent = 100
        setting.save(
            update_fields=["third_party_mode", "third_party_rollout_percent", "updated_at"]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False)

        self.assertTrue(third_party_provider_is_available(request))

    @override_settings(
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_provider_rollout_percent_is_independent_per_provider(self):
        setting = ProviderRuntimeSetting.load()
        setting.youtube_merge_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.third_party_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.youtube_merge_rollout_percent = 100
        setting.third_party_rollout_percent = 0
        setting.douyin_ultra_rollout_percent = 0
        setting.save(
            update_fields=[
                "youtube_merge_mode",
                "third_party_mode",
                "douyin_ultra_mode",
                "youtube_merge_rollout_percent",
                "third_party_rollout_percent",
                "douyin_ultra_rollout_percent",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False)

        with patch(
            "xhs_download.views.get_youtube_merge_health",
            return_value={"implemented": True, "ready": True},
        ):
            self.assertTrue(youtube_merge_mode_is_available(request))
        self.assertFalse(third_party_provider_is_available(request))
        with patch("xhs_download.views.get_kukutool_provider_health", return_value={"available": True}):
            self.assertFalse(ultra_hd_is_available(request))

    def test_rollout_bucket_is_stable_and_provider_specific(self):
        request = RequestFactory().get("/", HTTP_USER_AGENT="pytest")
        request.user = Mock(is_active=False, is_staff=False, is_authenticated=False)

        youtube_bucket = request_rollout_bucket(PROVIDER_YOUTUBE_MERGE, request)
        self.assertEqual(
            youtube_bucket,
            request_rollout_bucket(PROVIDER_YOUTUBE_MERGE, request),
        )
        self.assertNotEqual(
            youtube_bucket,
            request_rollout_bucket(PROVIDER_THIRD_PARTY, request),
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_public_requires_allowlist(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False, is_authenticated=False)

        summary = third_party_public_access_summary(
            "https://www.youtube.com/watch?v=demo",
            request,
        )

        self.assertFalse(summary["allowed"])
        self.assertEqual(summary["error_code"], "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED")

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_allowed_platform_allows_public_visitor(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_platforms = "youtube"
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_platforms",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False, is_authenticated=False)

        self.assertTrue(
            third_party_public_access_allowed(
                "https://www.youtube.com/watch?v=demo",
                request,
            )
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_allowed_host_uses_strict_suffix_boundary(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_hosts = "youtube.com"
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_hosts",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False, is_authenticated=False)

        self.assertTrue(
            third_party_public_access_allowed(
                "https://www.youtube.com/watch?v=demo",
                request,
            )
        )
        self.assertFalse(
            third_party_public_access_allowed(
                "https://youtube.com.evil.com/watch?v=demo",
                request,
            )
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_blocklist_wins_over_allowlist(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_platforms = "youtube,reddit"
        setting.third_party_blocked_platforms = "reddit"
        setting.third_party_blocked_hosts = "youtube.com"
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_platforms",
                "third_party_blocked_platforms",
                "third_party_blocked_hosts",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False, is_authenticated=False)

        self.assertFalse(
            third_party_public_access_allowed(
                "https://www.youtube.com/watch?v=demo",
                request,
            )
        )
        self.assertFalse(
            third_party_public_access_allowed(
                "https://www.reddit.com/r/pics/comments/demo",
                request,
            )
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_sensitive_staff_smoke_but_public_requires_allowlist(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")
        request.user = Mock(is_active=True, is_staff=True, is_authenticated=True)

        self.assertTrue(
            third_party_public_access_allowed(
                "https://www.instagram.com/p/demo",
                request,
            )
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_built_in_adult_host_is_rejected_for_staff_by_default(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.third_party_allowed_public_hosts = "onlyfans.com"
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_hosts",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=True, is_staff=True, is_authenticated=True)

        summary = third_party_public_access_summary(
            "https://onlyfans.com/demo",
            request,
        )

        self.assertFalse(summary["allowed"])
        self.assertEqual(summary["error_code"], "THIRD_PARTY_PLATFORM_BLOCKED")

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_capability_enabled_admin_public_allows_visitor(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")
        request.user = Mock(is_active=False, is_staff=False)

        self.assertTrue(third_party_provider_is_available(request))

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=False)
    def test_capability_disabled_admin_public_is_still_unavailable(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_platforms = "youtube"
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_platforms",
                "updated_at",
            ]
        )
        request = RequestFactory().get("/")
        request.user = Mock(is_active=True, is_staff=True)

        self.assertFalse(third_party_provider_is_available(request))
        self.assertFalse(
            third_party_public_access_allowed(
                "https://www.youtube.com/watch?v=demo",
                request,
            )
        )

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=False,
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.get_kukutool_provider_health")
    def test_douyin_ultra_staff_only_allows_staff_but_not_anonymous(self, mock_health):
        mock_health.return_value = {"available": True}
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        request = RequestFactory().get("/")

        request.user = Mock(is_active=False, is_staff=False)
        self.assertFalse(ultra_hd_is_available(request))

        request.user = Mock(is_active=True, is_staff=True)
        self.assertTrue(ultra_hd_is_available(request))

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False,
        THIRD_PARTY_STAFF_ONLY=True,
    )
    def test_third_party_staff_only_runtime_mode_allows_staff(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.STAFF
        setting.save(update_fields=["third_party_mode", "updated_at"])
        request = RequestFactory().get("/")

        request.user = Mock(is_active=False, is_staff=False)
        self.assertFalse(third_party_provider_is_available(request))

        request.user = Mock(is_active=True, is_staff=True)
        self.assertTrue(third_party_provider_is_available(request))


class ProviderHealthTestPanelTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        cache.clear()
        self.staff = get_user_model().objects.create_user(
            username="health-staff",
            password="test-password",
            is_staff=True,
        )
        self.superuser = get_user_model().objects.create_superuser(
            username="health-root",
            password="test-password",
        )

    def grant_health_permission(self, user, codename):
        permission = Permission.objects.get(
            content_type__app_label="xhs_download",
            codename=codename,
        )
        user.user_permissions.add(permission)
        for cache_name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            if hasattr(user, cache_name):
                delattr(user, cache_name)

    def test_provider_health_tests_page_permissions(self):
        staff_client = Client()
        staff_client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/provider-health-tests/").status_code, 404)
        self.assertEqual(staff_client.get("/admin/provider-health-tests/").status_code, 404)
        self.assertContains(super_client.get("/admin/provider-health-tests/"), "Provider 健康测试")

        self.grant_health_permission(self.staff, "view_providerhealthtestrun")
        self.assertContains(staff_client.get("/admin/provider-health-tests/"), "下载限流配置")

    @patch("xhs_download.views.get_runtime_readiness", return_value={"ready": True, "components": {"database": True, "cache": True}})
    def test_provider_health_tests_execute_permissions(self, _mock_readiness):
        staff_client = Client()
        staff_client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        blocked = staff_client.post("/admin/provider-health-tests/", data={"test_key": "health_ready"})
        self.assertEqual(blocked.status_code, 404)

        ok = super_client.post("/admin/provider-health-tests/", data={"test_key": "health_ready"})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.get().status, ProviderHealthTestStatus.SUCCESS)

        ProviderHealthTestRun.objects.all().delete()
        self.grant_health_permission(self.staff, "add_providerhealthtestrun")
        permitted = staff_client.post("/admin/provider-health-tests/", data={"test_key": "health_ready"})
        self.assertEqual(permitted.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.get().actor_username, "health-staff")

    @patch("xhs_download.views.get_runtime_readiness", return_value={"ready": True, "components": {"database": True, "cache": True}})
    def test_provider_health_tests_run_all_records_each_test(self, _mock_readiness):
        staff_client = Client()
        staff_client.force_login(self.staff)

        blocked = staff_client.post("/admin/provider-health-tests/", data={"action": "run_all_tests"})
        self.assertEqual(blocked.status_code, 404)

        client = Client()
        client.force_login(self.superuser)

        response = client.post("/admin/provider-health-tests/", data={"action": "run_all_tests"})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.count(), 6)
        statuses = dict(
            ProviderHealthTestRun.objects.values_list("test_key", "status")
        )
        self.assertEqual(statuses["health_ready"], ProviderHealthTestStatus.SUCCESS)
        self.assertEqual(statuses["douyin_native_parse"], ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(statuses["douyin_kukutool_ultra"], ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(statuses["youtube_merge"], ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(statuses["third_party_ytdlp"], ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(statuses["gallery_dl"], ProviderHealthTestStatus.SKIPPED)

    @patch("xhs_download.views.get_runtime_readiness", return_value={"ready": True, "components": {"database": True, "cache": True}})
    def test_provider_health_tests_ajax_post_renders_panel_without_redirect(self, _mock_readiness):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "health_ready"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)
        self.assertContains(response, "Runtime Readiness 测试完成：成功")
        self.assertContains(response, "health_ready")
        self.assertEqual(ProviderHealthTestRun.objects.get().status, ProviderHealthTestStatus.SUCCESS)

    @patch("xhs_download.views.get_runtime_readiness", return_value={"ready": True, "components": {"database": True, "cache": True}})
    def test_provider_health_tests_ajax_run_all_renders_panel_without_redirect(self, _mock_readiness):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"action": "run_all_tests"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response)
        self.assertContains(response, "Provider 一键测试完成")
        self.assertEqual(ProviderHealthTestRun.objects.count(), 6)

    def test_provider_health_test_empty_url_returns_skipped(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "douyin_native_parse"},
        )

        self.assertEqual(response.status_code, 302)
        run = ProviderHealthTestRun.objects.get()
        self.assertEqual(run.status, ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(run.error_type, "TestUrlMissing")

    @override_settings(YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080)
    @patch("xhs_download.views.build_youtube_merge_plan", return_value={"available": True})
    @patch("xhs_download.views.YtDlpGenericProvider.parse", return_value={"title": "Demo"})
    @patch(
        "xhs_download.views.get_youtube_merge_health",
        return_value={"implemented": True, "ready": True, "timeout_seconds": 10},
    )
    def test_youtube_health_test_uses_default_1080p_merge_target(
        self,
        _mock_health,
        _mock_parse,
        mock_plan,
    ):
        setting = ProviderHealthTestSetting.load()
        setting.youtube_url = "https://www.youtube.com/watch?v=dt-m5GT2rSc"
        setting.save()
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "youtube_merge"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.get().status, ProviderHealthTestStatus.SUCCESS)
        self.assertEqual(mock_plan.call_args.kwargs["max_height"], 1080)

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        KUKUTOOL_PROVIDER_ENABLED=False,
    )
    def test_provider_health_test_capability_disabled_is_unavailable(self):
        setting = ProviderHealthTestSetting.load()
        setting.douyin_url = "https://www.douyin.com/video/123"
        setting.save()
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "douyin_kukutool_ultra"},
        )

        self.assertEqual(response.status_code, 302)
        run = ProviderHealthTestRun.objects.get()
        self.assertEqual(run.status, ProviderHealthTestStatus.UNAVAILABLE)
        self.assertEqual(run.error_type, "CapabilityDisabled")

    @patch(
        "xhs_download.views.parse_media_page",
        side_effect=XhsParseError(
            "失败 https://cdn.example.com/a.mp4?token=secret provider_manifest_token=abc"
        ),
    )
    def test_provider_health_test_error_is_sanitized(self, _mock_parse):
        setting = ProviderHealthTestSetting.load()
        setting.douyin_url = "https://www.douyin.com/video/123?token=secret"
        setting.save()
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "douyin_native_parse"},
        )

        self.assertEqual(response.status_code, 302)
        run = ProviderHealthTestRun.objects.get()
        self.assertEqual(run.status, ProviderHealthTestStatus.FAILED)
        self.assertEqual(run.test_url_host, "www.douyin.com")
        self.assertIn("[url-redacted]", run.error_message_short)
        self.assertNotIn("cdn.example.com", run.error_message_short)
        self.assertNotIn("token=secret", run.error_message_short)
        self.assertNotIn("provider_manifest_token=abc", run.error_message_short)

    def test_provider_health_test_urls_are_saved_in_database(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={
                "action": "save_test_urls",
                "douyin_url": "https://www.douyin.com/video/123",
                "youtube_url": "https://www.youtube.com/watch?v=abc",
                "third_party_url": "https://www.youtube.com/watch?v=third",
                "gallerydl_url": "https://imgur.com/a/demo",
            },
        )

        self.assertEqual(response.status_code, 302)
        setting = ProviderHealthTestSetting.load()
        self.assertEqual(setting.douyin_url, "https://www.douyin.com/video/123")
        self.assertEqual(setting.gallerydl_url, "https://imgur.com/a/demo")

        page = client.get("/admin/provider-health-tests/")
        self.assertContains(page, "www.douyin.com")
        self.assertContains(page, "www.youtube.com")
        self.assertContains(page, "imgur.com")
        self.assertContains(page, "运行全部测试")
        self.assertContains(page, "health-running")
        self.assertContains(page, 'form.getAttribute("action")')
        self.assertContains(page, "healthRequestInFlight")
        self.assertNotContains(page, "fetch(form.action")

    def test_provider_health_test_url_save_clears_invalid_urls(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={
                "action": "save_test_urls",
                "douyin_url": "not-a-url",
                "youtube_url": "ftp://example.com/video",
                "third_party_url": "https://www.youtube.com/watch?v=third",
                "gallerydl_url": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        setting = ProviderHealthTestSetting.load()
        self.assertEqual(setting.douyin_url, "")
        self.assertEqual(setting.youtube_url, "")
        self.assertEqual(setting.third_party_url, "https://www.youtube.com/watch?v=third")
        self.assertEqual(setting.gallerydl_url, "")

    def test_provider_health_test_url_save_requires_change_permission(self):
        staff_client = Client()
        staff_client.force_login(self.staff)

        blocked = staff_client.post(
            "/admin/provider-health-tests/",
            data={"action": "save_test_urls", "douyin_url": "https://www.douyin.com/video/123"},
        )
        self.assertEqual(blocked.status_code, 404)
        self.assertEqual(ProviderHealthTestSetting.load().douyin_url, "")

        self.grant_health_permission(self.staff, "change_providerhealthtestsetting")
        permitted = staff_client.post(
            "/admin/provider-health-tests/",
            data={"action": "save_test_urls", "douyin_url": "https://www.douyin.com/video/123"},
        )
        self.assertEqual(permitted.status_code, 302)
        self.assertEqual(ProviderHealthTestSetting.load().douyin_url, "https://www.douyin.com/video/123")

    @patch("xhs_download.views.get_runtime_readiness", return_value={"ready": True, "components": {"database": True, "cache": True}})
    def test_provider_health_test_records_request_id(self, _mock_readiness):
        client = Client()
        client.force_login(self.superuser)

        response = client.post(
            "/admin/provider-health-tests/",
            data={"test_key": "health_ready"},
            HTTP_X_REQUEST_ID="health-test-request-1",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.get().request_id, "health-test-request-1")

    def test_provider_health_tests_page_shows_recent_records(self):
        ProviderHealthTestRun.objects.create(
            request_id="rid-health",
            test_key="health_ready",
            status=ProviderHealthTestStatus.SUCCESS,
            provider="runtime",
            actor_username="health-root",
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/provider-health-tests/")

        self.assertContains(response, "health_ready")
        self.assertContains(response, "rid-health")

    def test_provider_health_tests_recent_records_are_paginated(self):
        for index in range(55):
            ProviderHealthTestRun.objects.create(
                request_id=f"rid-health-{index}",
                test_key="health_ready",
                status=ProviderHealthTestStatus.SUCCESS,
                provider="runtime",
                actor_username="health-root",
            )
        client = Client()
        client.force_login(self.superuser)

        first_page = client.get("/admin/provider-health-tests/")
        second_page = client.get("/admin/provider-health-tests/", {"page": "2"})

        self.assertEqual(first_page.context["recent_runs_page"].paginator.count, 55)
        self.assertEqual(len(first_page.context["recent_runs"]), 50)
        self.assertContains(first_page, "共 55 条")
        self.assertContains(first_page, "下一页")
        self.assertEqual(second_page.context["recent_runs_page"].number, 2)
        self.assertEqual(len(second_page.context["recent_runs"]), 5)

    def test_provider_health_tests_show_admin_display_timezone(self):
        run = ProviderHealthTestRun.objects.create(
            request_id="rid-timezone",
            test_key="health_ready",
            status=ProviderHealthTestStatus.SUCCESS,
            provider="runtime",
            actor_username="health-root",
        )
        ProviderHealthTestRun.objects.filter(pk=run.pk).update(
            created_at=datetime(2026, 6, 28, 16, 5, 6, tzinfo=datetime_timezone.utc),
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/provider-health-tests/")

        self.assertContains(response, "2026-06-29 00:05:06")
        self.assertContains(response, "Asia/Shanghai")

    def test_provider_health_tests_page_shows_download_runtime_summary(self):
        DownloadRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "rate_limit_per_minute": 9,
                "daily_request_limit_per_ip": 90,
                "global_daily_request_limit": 900,
                "max_concurrent_per_ip": 2,
                "daily_bytes_limit_per_ip": 1024,
                "global_daily_bytes_limit": 2048,
            },
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/provider-health-tests/")

        self.assertContains(response, "每分钟下载请求数 / IP")
        self.assertContains(response, "DownloadRuntimeSetting")
        self.assertContains(response, "已初始化")
        self.assertContains(response, "9")
        self.assertContains(response, "2.0 KB")

    def test_provider_health_run_admin_is_read_only_and_superuser_can_delete(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import ProviderHealthTestRunAdmin

        request = RequestFactory().get("/")
        request.user = self.staff
        super_request = RequestFactory().get("/")
        super_request.user = self.superuser
        health_admin = ProviderHealthTestRunAdmin(ProviderHealthTestRun, AdminSite())

        self.assertFalse(health_admin.has_add_permission(request))
        self.assertFalse(health_admin.has_change_permission(request))
        self.assertFalse(health_admin.has_delete_permission(request))
        self.assertTrue(health_admin.has_delete_permission(super_request))

    def test_provider_health_tests_appears_in_admin_home(self):
        from django.contrib import admin

        request = RequestFactory().get("/")
        request.user = self.superuser

        app_list = admin.site.get_app_list(request)
        models_by_app = {
            app["app_label"]: {model["object_name"] for model in app["models"]}
            for app in app_list
        }

        self.assertIn("ProviderHealthTestPanelProxy", models_by_app["provider_console"])

    def test_provider_health_tests_admin_entry_redirects_to_panel(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/provider_console/providerhealthtestpanelproxy/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/provider-health-tests/")


class AdminObservabilityMvpTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        cache.clear()
        self.staff = get_user_model().objects.create_user(
            username="ops-staff",
            password="test-password",
            is_staff=True,
        )
        self.superuser = get_user_model().objects.create_superuser(
            username="ops-root",
            password="test-password",
        )

    def grant_xhs_view_permission(self, user, codename):
        permission = Permission.objects.get(
            content_type__app_label="xhs_download",
            codename=codename,
        )
        user.user_permissions.add(permission)
        for cache_name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            if hasattr(user, cache_name):
                delattr(user, cache_name)

    def test_request_id_middleware_generates_and_reuses_request_id(self):
        generated = Client().get("/")
        reused = Client().get("/", HTTP_X_REQUEST_ID="nginx-request-123")

        self.assertIn("X-Request-ID", generated)
        self.assertEqual(reused["X-Request-ID"], "nginx-request-123")

    @patch("xhs_download.views.parse_media_page")
    def test_parse_success_writes_parse_request_log(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="image",
                    url="https://ci.xiaohongshu.com/demo.jpg",
                    platform="xiaohongshu",
                ),
            ),
            metadata={"selected_provider": "native", "source_platform": "xiaohongshu"},
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.xiaohongshu.com/explore/demo"},
        )

        self.assertEqual(response.status_code, 200)
        log = ParseRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.SUCCESS)
        self.assertEqual(log.query_host, "www.xiaohongshu.com")
        self.assertEqual(log.selected_provider, "native")
        self.assertEqual(log.image_count, 1)
        self.assertEqual(len(log.ip_hash), 64)

    @patch("xhs_download.views.parse_media_page", side_effect=XhsParseError("bad link"))
    def test_parse_failure_writes_parse_request_log(self, _mock_parse):
        response = Client().get("/results/", {"q": "https://www.xiaohongshu.com/explore/fail"})

        self.assertEqual(response.status_code, 200)
        log = ParseRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.FAILED)
        self.assertEqual(log.error_type, "XhsParseError")
        self.assertIn("bad link", log.error_message_short)

    def test_observability_error_text_is_redacted_in_database_and_logger_extra(self):
        request = RequestFactory().post("/xhs/results/")
        request.user = self.staff
        request.request_id = "rid-redact"
        error = XhsParseError(
            "下载失败 https://cdn.example.com/a.mp4?token=secret&signature=abc "
            "provider_manifest_token=very-secret"
        )

        with patch("xhs_download.views.logger.info") as mock_info:
            log_parse_request_event(
                request,
                "media_parse_failed",
                query="https://www.xiaohongshu.com/explore/demo",
                platform_key="xhs",
                requested_ultra_hd=False,
                prefer_ultra_hd=False,
                requested_youtube_merge=False,
                prefer_youtube_merge=False,
                duration_ms=12,
                error=error,
                error_code="PROVIDER_TIMEOUT",
            )

        log = ParseRequestLog.objects.get(request_id="rid-redact")
        self.assertIn("[url-redacted]", log.error_message_short)
        self.assertNotIn("cdn.example.com/a.mp4", log.error_message_short)
        self.assertNotIn("very-secret", log.error_message_short)
        self.assertNotIn("token=secret", log.error_message_short)
        logged_error = mock_info.call_args.kwargs["extra"]["error"]
        self.assertIn("[url-redacted]", logged_error)
        self.assertNotIn("cdn.example.com/a.mp4", logged_error)
        self.assertNotIn("very-secret", logged_error)

    def test_observability_keeps_plain_chinese_error_text(self):
        request = RequestFactory().post("/media/download-images/")
        request.user = self.staff
        request.request_id = "rid-plain-error"

        record_download_request_log(
            request,
            download_type="zip",
            status=ObservabilityStatus.FAILED,
            error_message_short="第三方解析超时，请稍后再试",
        )

        log = DownloadRequestLog.objects.get(request_id="rid-plain-error")
        self.assertEqual(log.error_message_short, "第三方解析超时，请稍后再试")

    def test_provider_admin_audit_snapshot_redacts_sensitive_text(self):
        request = RequestFactory().post("/admin/provider-settings/")
        request.user = self.superuser
        request.request_id = "rid-audit-redact"

        record_provider_admin_audit_log(
            request,
            action="provider_runtime_update",
            before_snapshot={
                "third_party_allowed_public_hosts": (
                    "https://cdn.example.com/a.mp4?auth_key=secret"
                ),
            },
            after_snapshot={
                "third_party_allowed_public_hosts": (
                    "provider_manifest_token=very-secret"
                ),
            },
        )

        audit = ProviderAdminAuditLog.objects.get(request_id="rid-audit-redact")
        self.assertEqual(audit.changed_fields, ["third_party_allowed_public_hosts"])
        self.assertEqual(
            audit.before_snapshot["third_party_allowed_public_hosts"],
            "[url-redacted]",
        )
        self.assertEqual(
            audit.after_snapshot["third_party_allowed_public_hosts"],
            "provider_manifest_token=[redacted]",
        )

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_third_party_allowlist_miss_writes_blocked_log(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC
        setting.third_party_allowed_public_platforms = ""
        setting.save(
            update_fields=[
                "third_party_mode",
                "third_party_allowed_public_platforms",
                "updated_at",
            ]
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.youtube.com/watch?v=demo"},
        )

        self.assertEqual(response.status_code, 403)
        log = ParseRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.BLOCKED)
        self.assertEqual(log.error_code, "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        ULTRA_HD_RATE_LIMIT_PER_MINUTE=1,
    )
    @patch("xhs_download.views.get_kukutool_provider_health", return_value={"available": True})
    @patch("xhs_download.views.consume_fixed_window")
    def test_parse_rate_limit_writes_rate_limited_log(self, mock_limit, _mock_health):
        setting = ProviderRuntimeSetting.load()
        setting.douyin_ultra_mode = ProviderAccessMode.PUBLIC
        setting.save(update_fields=["douyin_ultra_mode", "updated_at"])
        mock_limit.return_value = Mock(allowed=False, retry_after=30)

        response = Client().get(
            "/tiktok/results/",
            {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"},
        )

        self.assertEqual(response.status_code, 429)
        log = ParseRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.RATE_LIMITED)
        self.assertEqual(log.error_code, "RATE_LIMITED")

    @patch("xhs_download.services.observability.ParseRequestLog.objects.create", side_effect=RuntimeError("db down"))
    @patch("xhs_download.views.parse_media_page")
    def test_parse_log_write_failure_does_not_break_response(self, mock_parse, _mock_create):
        mock_parse.return_value = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="demo",
            assets=(),
            metadata={},
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.xiaohongshu.com/explore/demo"},
        )

        self.assertEqual(response.status_code, 200)

    @patch("xhs_download.views.build_download_archive_file")
    def test_zip_download_success_writes_download_log(self, mock_archive):
        tmp = NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.write(b"zip-bytes")
        tmp.flush()
        tmp.seek(0)
        mock_archive.return_value = {
            "file": tmp,
            "filename": "demo.zip",
            "saved_count": 1,
            "failed_count": 0,
        }
        payload = {
            "media_items": [
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo.jpg"},
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo-2.jpg"},
            ],
            "base_filename": "demo",
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.SUCCESS)
        self.assertEqual(log.download_type, "zip")
        self.assertEqual(log.item_count, 2)
        self.assertGreater(log.actual_bytes, 0)

    @override_settings(DOWNLOAD_RATE_LIMIT_PER_MINUTE=1)
    @patch("xhs_download.views.check_download_request_limits")
    def test_download_rate_limit_writes_download_log(self, mock_limits):
        mock_limits.return_value = (
            "identity",
            limit_error_response(
                "limited",
                retry_after=1,
                error_code="DOWNLOAD_MINUTE_RATE_LIMIT",
            ),
        )
        payload = {
            "media_items": [
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo.jpg"},
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo-2.jpg"},
            ],
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 429)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.RATE_LIMITED)
        self.assertEqual(log.error_code, "DOWNLOAD_MINUTE_RATE_LIMIT")

    @patch("xhs_download.views.check_download_request_limits")
    @patch("xhs_download.views.acquire_concurrency")
    def test_download_concurrency_limit_writes_specific_download_log(
        self,
        mock_acquire,
        mock_limits,
    ):
        mock_limits.return_value = ("identity", None)
        mock_acquire.return_value = LimitResult(
            allowed=False,
            current=2,
            limit=1,
        )
        payload = {
            "media_items": [
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo.jpg"},
                {"type": "image", "url": "https://ci.xiaohongshu.com/demo-2.jpg"},
            ],
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["X-Error-Code"], "DOWNLOAD_CONCURRENCY_LIMIT")
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.RATE_LIMITED)
        self.assertEqual(log.error_code, "DOWNLOAD_CONCURRENCY_LIMIT")

    def test_provider_manifest_failure_writes_download_log(self):
        payload = {
            "media_items": [
                {
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/demo.jpg",
                    "asset_id": "cover",
                    "provider": "yt_dlp_generic",
                    "provider_resource_id": "abc",
                    "provider_cache_key": "third_party:yt_dlp_generic:abc",
                    "provider_manifest_token": "bad-token",
                }
            ],
        }

        response = Client().post(
            "/media/download-images/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.status, ObservabilityStatus.FAILED)
        self.assertEqual(log.error_code, "PROVIDER_MANIFEST_REJECTED")

    def test_provider_settings_update_writes_audit_log(self):
        client = Client()
        client.force_login(self.staff)

        response = client.post(
            "/admin/provider-settings/",
            data={
                "douyin_ultra_mode": ProviderAccessMode.STAFF,
                "youtube_merge_mode": ProviderAccessMode.DISABLED,
                "third_party_mode": ProviderAccessMode.DISABLED,
                "douyin_ultra_rollout_percent": "0",
                "youtube_merge_rollout_percent": "0",
                "third_party_rollout_percent": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        audit = ProviderAdminAuditLog.objects.get()
        self.assertEqual(audit.actor_username, "ops-staff")
        self.assertEqual(audit.action, "provider_runtime_update")
        self.assertIn("douyin_ultra_mode", audit.changed_fields)
        self.assertEqual(audit.before_snapshot["douyin_ultra_mode"], ProviderAccessMode.DISABLED)
        self.assertEqual(audit.after_snapshot["douyin_ultra_mode"], ProviderAccessMode.STAFF)

    def test_ops_dashboard_access_and_aggregation(self):
        ParseRequestLog.objects.create(
            request_id="rid1",
            status=ObservabilityStatus.SUCCESS,
            source_platform="xiaohongshu",
            selected_provider="native",
            duration_ms=100,
        )
        ParseRequestLog.objects.create(
            request_id="rid2",
            status=ObservabilityStatus.FAILED,
            error_code="PROVIDER_TIMEOUT",
            duration_ms=300,
        )
        DownloadRequestLog.objects.create(
            request_id="rid3",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            actual_bytes=1024,
        )
        staff_client = Client()
        staff_client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        anonymous = Client().get("/admin/ops-dashboard/")
        staff_response = staff_client.get("/admin/ops-dashboard/")
        super_response = super_client.get("/admin/ops-dashboard/")
        self.grant_xhs_view_permission(self.staff, "view_parserequestlog")
        permitted_response = staff_client.get("/admin/ops-dashboard/")

        self.assertEqual(anonymous.status_code, 404)
        self.assertEqual(staff_response.status_code, 404)
        self.assertContains(super_response, "运营看板")
        self.assertContains(permitted_response, "今日解析次数")
        self.assertContains(permitted_response, "PROVIDER_TIMEOUT")

    def test_observability_v2_access_and_metrics_render(self):
        DownloadRequestLog.objects.create(
            request_id="rid-intel-page",
            status=ObservabilityStatus.SUCCESS,
            download_type="auto",
            route_type=ROUTE_DIRECT,
            final_policy_route=ROUTE_DIRECT,
            policy_confidence=0.8,
            provider="native",
            estimated_bytes=1024,
            actual_bytes=1024,
            browser_egress_estimated_bytes=1024,
        )
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/observability-v2/").status_code, 404)
        self.assertEqual(client.get("/admin/observability-v2/").status_code, 404)
        self.assertContains(super_client.get("/admin/observability-v2/"), "Observability v2")
        self.assertContains(super_client.get("/admin/observability-v2/"), "policy_conflict_rate")
        self.assertContains(super_client.get("/admin/observability-v2/"), "recommendation_only")

        self.grant_xhs_view_permission(self.staff, "view_downloadrequestlog")
        permitted_response = client.get("/admin/observability-v2/")

        self.assertContains(permitted_response, "Observability v2")
        self.assertContains(permitted_response, "routing_efficiency")
        self.assertNotContains(permitted_response, "rid-intel-page")

    def test_feedback_loop_page_access_and_render(self):
        DownloadRequestLog.objects.create(
            request_id="rid-feedback-page",
            status=ObservabilityStatus.FAILED,
            download_type="zip",
            route_type=ROUTE_ZIP,
            provider="yt_dlp_generic",
            fallback_count=1,
            failure_type=FAILURE_PROVIDER_TIMEOUT,
        )
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/feedback-loop/").status_code, 404)
        self.assertEqual(client.get("/admin/feedback-loop/").status_code, 404)
        self.assertContains(super_client.get("/admin/feedback-loop/"), "Feedback Loop MVP")
        self.assertContains(super_client.get("/admin/feedback-loop/"), "Provider Trend")
        self.assertContains(super_client.get("/admin/feedback-loop/"), "Policy Suggestions")

        self.grant_xhs_view_permission(self.staff, "view_downloadrequestlog")
        permitted_response = client.get("/admin/feedback-loop/")

        self.assertContains(permitted_response, "Feedback Loop MVP")
        self.assertContains(permitted_response, "yt_dlp_generic")
        self.assertNotContains(permitted_response, "rid-feedback-page")

    def test_policy_engine_page_requires_specific_permission(self):
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/policy-engine/").status_code, 404)
        self.assertEqual(client.get("/admin/policy-engine/").status_code, 404)
        self.assertContains(super_client.get("/admin/policy-engine/"), "Policy Engine 决策中心")

        self.grant_xhs_view_permission(self.staff, "view_policyruntimesetting")
        response = client.get("/admin/policy-engine/")

        self.assertContains(response, "Policy Engine 决策中心")
        self.assertContains(response, "当前账号只有查看权限")
        self.assertContains(response, "v3.5 模式")

    def test_policy_engine_page_updates_runtime_with_change_permission(self):
        client = Client()
        client.force_login(self.staff)
        self.grant_xhs_view_permission(self.staff, "change_policyruntimesetting")

        response = client.post(
            "/admin/policy-engine/",
            data={
                "v35_mode": PolicyRuntimeMode.PUBLIC_GRAY,
                "v35_rollout_percent": "80",
                "v4_adaptive_shadow_enabled": "1",
                "v4_auto_weights_enabled": "1",
                "v4_adaptive_window_hours": "168",
                "v4_adaptive_min_confidence": "0.5",
            },
        )

        self.assertEqual(response.status_code, 302)
        setting = PolicyRuntimeSetting.load()
        self.assertEqual(setting.v35_mode, PolicyRuntimeMode.PUBLIC_GRAY)
        self.assertEqual(setting.v35_rollout_percent, 80)
        self.assertTrue(setting.v4_auto_weights_enabled)
        self.assertEqual(setting.v4_adaptive_window_hours, 168)
        self.assertEqual(setting.v4_adaptive_min_confidence, 0.5)
        audit = ProviderAdminAuditLog.objects.get(action="policy_runtime_update")
        self.assertIn("v35_mode", audit.changed_fields)
        self.assertEqual(audit.after_snapshot["v35_rollout_percent"], 80)

    def test_policy_engine_page_shows_feedback_rows(self):
        DownloadRequestLog.objects.create(
            request_id="rid-policy-page",
            status=ObservabilityStatus.SUCCESS,
            download_type="auto",
            route_type=ROUTE_DIRECT,
            final_policy_route=ROUTE_DIRECT,
            policy_active=True,
            provider="native",
            actual_bytes=1024,
            duration_ms=12,
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/policy-engine/")

        self.assertContains(response, "最近 Policy 决策反馈")
        self.assertContains(response, "rid-policy-page")
        self.assertContains(response, "1.0 KB")

    def test_observability_log_pages_require_specific_view_permissions(self):
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/parse-logs/").status_code, 404)
        self.assertEqual(client.get("/admin/parse-logs/").status_code, 404)
        self.assertEqual(client.get("/admin/download-logs/").status_code, 404)
        self.assertEqual(client.get("/admin/provider-audit-logs/").status_code, 404)
        self.assertContains(super_client.get("/admin/parse-logs/"), "解析日志")

        self.grant_xhs_view_permission(self.staff, "view_parserequestlog")
        self.assertContains(client.get("/admin/parse-logs/"), "解析日志")
        self.grant_xhs_view_permission(self.staff, "view_downloadrequestlog")
        self.assertContains(client.get("/admin/download-logs/"), "下载日志")
        self.grant_xhs_view_permission(self.staff, "view_provideradminauditlog")
        self.assertContains(client.get("/admin/provider-audit-logs/"), "Provider 审计")

    def test_download_logs_page_shows_and_filters_route_type(self):
        DownloadRequestLog.objects.create(
            request_id="rid-direct",
            status=ObservabilityStatus.SUCCESS,
            download_type="direct",
            route_type="direct",
        )
        DownloadRequestLog.objects.create(
            request_id="rid-zip",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            route_type="zip",
        )
        client = Client()
        client.force_login(self.staff)
        self.grant_xhs_view_permission(self.staff, "view_downloadrequestlog")

        response = client.get("/admin/download-logs/", {"route_type": "direct"})

        self.assertContains(response, "route_type")
        self.assertContains(response, "direct")
        self.assertContains(response, "rid-direct")
        self.assertNotContains(response, "rid-zip")

    def test_admin_console_permissions(self):
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/console/").status_code, 404)
        self.assertEqual(client.get("/admin/console/").status_code, 404)
        self.assertContains(super_client.get("/admin/console/"), "运维首页")

        self.grant_xhs_view_permission(self.staff, "view_downloadrequestlog")
        self.assertContains(client.get("/admin/console/"), "今日下载次数")

    def test_self_healing_run_permissions_and_manual_event(self):
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/self-healing/run/").status_code, 404)
        self.assertEqual(client.get("/admin/self-healing/run/").status_code, 404)
        self.assertContains(super_client.get("/admin/self-healing/run/"), "Self-Healing")

        self.grant_xhs_view_permission(self.staff, "view_selfhealingevent")
        self.assertContains(client.get("/admin/self-healing/run/"), "Self-Healing")
        self.assertEqual(
            client.post("/admin/self-healing/run/", data={"provider": "kukutool"}).status_code,
            404,
        )

        self.grant_xhs_view_permission(self.staff, "add_selfhealingevent")
        response = client.post("/admin/self-healing/run/", data={"provider": "kukutool"})

        self.assertEqual(response.status_code, 302)
        event = SelfHealingEvent.objects.get()
        self.assertEqual(event.trigger, "manual_diagnostics")
        self.assertEqual(event.provider, "kukutool")
        self.assertEqual(event.action, HEAL_ACTION_SWITCH_PROVIDER)
        self.assertEqual(event.switched_to_provider, "yt_dlp_generic")

    def test_admin_console_empty_data_renders(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/console/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "今日下载次数")
        self.assertContains(response, "暂无失败记录")
        self.assertContains(response, "暂无大文件下载记录")
        self.assertContains(response, "暂无路由数据")
        self.assertContains(response, "暂无 Routing v3 反馈数据")
        self.assertContains(response, "Self-Healing Dashboard")

    def test_admin_console_shows_download_metrics_and_failure_top(self):
        DownloadRequestLog.objects.create(
            request_id="rid-download-success",
            status=ObservabilityStatus.SUCCESS,
            download_type="zip",
            route_type="zip",
            route_reason="多资源需要统一打包下载",
            server_egress_estimated_bytes=4096,
            provider="yt_dlp_generic",
            source_platform="youtube",
            estimated_bytes=4096,
            actual_bytes=2048,
        )
        DownloadRequestLog.objects.create(
            request_id="rid-download-failed",
            status=ObservabilityStatus.BLOCKED,
            download_type="zip",
            route_type="blocked",
            route_reason="下载请求被成本策略阻止",
            provider="kukutool",
            source_platform="douyin",
            estimated_bytes=8192,
            actual_bytes=4096,
            error_code="SINGLE_ARCHIVE_SIZE_LIMIT",
            failure_type=FAILURE_PROVIDER_TIMEOUT,
            recovery_count=1,
            fallback_count=1,
            recovery_action="zip_to_proxy",
            fallback_from_route=ROUTE_ZIP,
            fallback_to_route=ROUTE_PROXY,
            duration_ms=16000,
        )
        DownloadRequestLog.objects.create(
            request_id="rid-download-direct",
            status=ObservabilityStatus.SUCCESS,
            download_type="direct",
            route_type="direct",
            route_reason="图片资源优先浏览器直链下载",
            browser_egress_estimated_bytes=2048,
            estimated_bytes=2048,
        )
        RoutingDecisionLog.objects.create(
            request_id="rid-route-direct",
            route_type="direct",
            provider="native",
            estimated_browser_bytes=2048,
            actual_server_bytes=0,
            success=True,
            duration_ms=120,
        )
        RoutingDecisionLog.objects.create(
            request_id="rid-route-zip",
            route_type="zip",
            provider="yt_dlp_generic",
            estimated_server_bytes=4096,
            actual_server_bytes=6144,
            success=True,
            duration_ms=900,
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/console/")

        self.assertContains(response, "今日下载次数")
        self.assertContains(response, "3")
        self.assertContains(response, "今日下载实际流量")
        self.assertContains(response, "6.0 KB")
        self.assertContains(response, "SINGLE_ARCHIVE_SIZE_LIMIT")
        self.assertContains(response, "yt_dlp_generic")
        self.assertContains(response, "douyin")
        self.assertContains(response, "最近 24h 下载路由")
        self.assertContains(response, "direct")
        self.assertContains(response, "zip")
        self.assertContains(response, "服务器出网节省估算")
        self.assertContains(response, "Routing Intelligence Panel")
        self.assertContains(response, "routing_efficiency_score")
        self.assertContains(response, "cost_accuracy_score")
        self.assertContains(response, "策略建议")
        self.assertContains(response, "System Stability Panel")
        self.assertContains(response, "24h recovery_count")
        self.assertContains(response, "24h fallback_count")
        self.assertContains(response, FAILURE_PROVIDER_TIMEOUT)
        self.assertContains(response, "Provider Stability Score")
        self.assertContains(response, "unstable")

    def test_rollout_guard_permissions(self):
        client = Client()
        client.force_login(self.staff)
        super_client = Client()
        super_client.force_login(self.superuser)

        self.assertEqual(Client().get("/admin/rollout-guard/").status_code, 404)
        self.assertEqual(client.get("/admin/rollout-guard/").status_code, 404)
        self.assertContains(super_client.get("/admin/rollout-guard/"), "放量守卫")

        self.grant_xhs_view_permission(self.staff, "view_parserequestlog")
        self.assertContains(client.get("/admin/rollout-guard/"), "Provider 放量建议")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        YOUTUBE_MERGE_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
    )
    def test_rollout_guard_empty_data_renders(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "抖音 KuKuTool 超高清")
        self.assertContains(response, "YouTube 高清合并")
        self.assertContains(response, "通用第三方 Provider")
        self.assertContains(response, "谨慎，建议继续观察")

    @override_settings(ULTRA_HD_PROVIDER_ENABLED=False, KUKUTOOL_PROVIDER_ENABLED=True)
    def test_rollout_guard_capability_disabled_is_do_not_public(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "环境 capability gate 未开启")
        self.assertContains(response, "不建议公开")

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=True)
    def test_rollout_guard_health_failed_is_do_not_public(self):
        ProviderHealthTestRun.objects.create(
            request_id="rid-health-failed",
            test_key="youtube_merge",
            status=ProviderHealthTestStatus.FAILED,
            provider="youtube_merge",
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "最近健康测试失败 / 不可用")
        self.assertContains(response, "不建议公开")

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=True)
    def test_rollout_guard_health_skipped_is_caution(self):
        ProviderHealthTestRun.objects.create(
            request_id="rid-health-skipped",
            test_key="youtube_merge",
            status=ProviderHealthTestStatus.SKIPPED,
            provider="youtube_merge",
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "最近健康测试跳过")
        self.assertContains(response, "谨慎，建议继续观察")

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=True)
    def test_rollout_guard_success_rate_allows_one_percent_gray(self):
        ProviderHealthTestRun.objects.create(
            request_id="rid-health-success",
            test_key="youtube_merge",
            status=ProviderHealthTestStatus.SUCCESS,
            provider="youtube_merge",
        )
        for index in range(5):
            ParseRequestLog.objects.create(
                request_id=f"rid-youtube-success-{index}",
                status=ObservabilityStatus.SUCCESS,
                selected_provider="youtube_merge",
                duration_ms=500,
            )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "可考虑 1% 公开灰度")
        self.assertContains(response, "24h 成功率达标")

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=True)
    def test_rollout_guard_low_success_rate_is_do_not_public(self):
        ProviderHealthTestRun.objects.create(
            request_id="rid-health-success-low",
            test_key="youtube_merge",
            status=ProviderHealthTestStatus.SUCCESS,
            provider="youtube_merge",
        )
        for index in range(2):
            ParseRequestLog.objects.create(
                request_id=f"rid-youtube-ok-{index}",
                status=ObservabilityStatus.SUCCESS,
                selected_provider="youtube_merge",
                duration_ms=500,
            )
        for index in range(3):
            ParseRequestLog.objects.create(
                request_id=f"rid-youtube-failed-{index}",
                status=ObservabilityStatus.FAILED,
                selected_provider="youtube_merge",
                duration_ms=500,
                error_code="PROVIDER_FAILED",
            )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "24h 成功率低于 70%")
        self.assertContains(response, "不建议公开")

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_rollout_guard_third_party_public_gray_requires_allowlist(self):
        setting = ProviderRuntimeSetting.load()
        setting.third_party_mode = ProviderAccessMode.PUBLIC_GRAY
        setting.third_party_rollout_percent = 1
        setting.third_party_allowed_public_hosts = ""
        setting.third_party_allowed_public_platforms = ""
        setting.save(update_fields=[
            "third_party_mode",
            "third_party_rollout_percent",
            "third_party_allowed_public_hosts",
            "third_party_allowed_public_platforms",
            "updated_at",
        ])
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "第三方公开白名单为空")
        self.assertContains(response, "不建议公开")

    @override_settings(YOUTUBE_MERGE_PROVIDER_ENABLED=True)
    def test_rollout_guard_download_usage_over_ninety_blocks_public_gray(self):
        provider_setting = ProviderRuntimeSetting.load()
        provider_setting.youtube_merge_mode = ProviderAccessMode.PUBLIC_GRAY
        provider_setting.youtube_merge_rollout_percent = 1
        provider_setting.save(update_fields=[
            "youtube_merge_mode",
            "youtube_merge_rollout_percent",
            "updated_at",
        ])
        download_setting = DownloadRuntimeSetting.load()
        download_setting.global_daily_bytes_limit = 1000
        download_setting.save(update_fields=["global_daily_bytes_limit", "updated_at"])
        ProviderHealthTestRun.objects.create(
            request_id="rid-health-usage",
            test_key="youtube_merge",
            status=ProviderHealthTestStatus.SUCCESS,
            provider="youtube_merge",
        )
        for index in range(5):
            ParseRequestLog.objects.create(
                request_id=f"rid-youtube-usage-{index}",
                status=ObservabilityStatus.SUCCESS,
                selected_provider="youtube_merge",
                duration_ms=500,
            )
        DownloadRequestLog.objects.create(
            request_id="rid-download-usage",
            status=ObservabilityStatus.SUCCESS,
            provider="youtube_merge",
            source_platform="youtube",
            actual_bytes=950,
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "今日下载流量超过 90%")
        self.assertContains(response, "不建议公开")

    def test_rollout_guard_page_contains_operational_links(self):
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/rollout-guard/")

        self.assertContains(response, "Provider 设置")
        self.assertContains(response, "Provider 测试")
        self.assertContains(response, "解析日志")
        self.assertContains(response, "下载日志")
        self.assertContains(response, "运维首页")

    def test_observability_log_pages_are_paginated(self):
        for index in range(55):
            ParseRequestLog.objects.create(
                request_id=f"rid-parse-{index}",
                status=ObservabilityStatus.SUCCESS,
                query_host="example.com",
            )
            DownloadRequestLog.objects.create(
                request_id=f"rid-download-{index}",
                status=ObservabilityStatus.SUCCESS,
                download_type="zip",
            )
            ProviderAdminAuditLog.objects.create(
                request_id=f"rid-audit-{index}",
                actor_username="ops-root",
                action="provider_runtime_update",
            )
        client = Client()
        client.force_login(self.superuser)

        parse_response = client.get("/admin/parse-logs/")
        download_response = client.get("/admin/download-logs/")
        audit_response = client.get("/admin/provider-audit-logs/")

        self.assertEqual(parse_response.context["page_obj"].paginator.count, 55)
        self.assertEqual(len(parse_response.context["page_obj"]), 50)
        self.assertContains(parse_response, "共 55 条")
        self.assertContains(parse_response, "下一页")
        self.assertEqual(download_response.context["page_obj"].paginator.count, 55)
        self.assertEqual(audit_response.context["page_obj"].paginator.count, 55)

    def test_observability_log_pagination_preserves_filters(self):
        for index in range(55):
            ParseRequestLog.objects.create(
                request_id=f"rid-keep-{index}",
                status=ObservabilityStatus.SUCCESS,
                query_host="example.com",
            )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/parse-logs/", {"q": "rid-keep"})

        self.assertContains(response, "q=rid-keep&page=2")

    def test_observability_log_pages_show_admin_display_timezone(self):
        log = ParseRequestLog.objects.create(
            request_id="rid-parse-timezone",
            status=ObservabilityStatus.SUCCESS,
            query_host="example.com",
        )
        ParseRequestLog.objects.filter(pk=log.pk).update(
            created_at=datetime(2026, 6, 28, 16, 5, 6, tzinfo=datetime_timezone.utc),
        )
        client = Client()
        client.force_login(self.superuser)

        response = client.get("/admin/parse-logs/")

        self.assertContains(response, "2026-06-29 00:05:06")

    def test_observability_admin_models_are_read_only_for_staff_and_runtime_is_changeable(self):
        from django.contrib import admin
        from django.contrib.admin.sites import AdminSite

        from .admin import ProviderAdminAuditLogAdmin, ProviderRuntimeSettingAdmin

        request = RequestFactory().get("/")
        request.user = self.staff
        super_request = RequestFactory().get("/")
        super_request.user = self.superuser
        audit_admin = ProviderAdminAuditLogAdmin(ProviderAdminAuditLog, AdminSite())
        runtime_admin = ProviderRuntimeSettingAdmin(ProviderRuntimeSetting, AdminSite())

        self.assertFalse(audit_admin.has_add_permission(request))
        self.assertFalse(audit_admin.has_change_permission(request))
        self.assertFalse(audit_admin.has_delete_permission(request))
        self.assertTrue(audit_admin.has_delete_permission(super_request))
        self.assertFalse(runtime_admin.has_change_permission(request))
        self.assertTrue(runtime_admin.has_change_permission(super_request))

        app_list = admin.site.get_app_list(super_request)
        models_by_app = {
            app["app_label"]: {model["object_name"] for model in app["models"]}
            for app in app_list
        }
        self.assertIn("ProviderRuntimeSettingProxy", models_by_app["provider_console"])
        self.assertIn("ProviderHealthTestPanelProxy", models_by_app["provider_console"])
        self.assertIn("DownloadRuntimeSetting", models_by_app["download_console"])
        self.assertNotIn("ProviderRuntimeSetting", models_by_app.get("xhs_download", set()))
        self.assertNotIn("DownloadRuntimeSetting", models_by_app.get("xhs_download", set()))

    @override_settings(
        DOWNLOAD_RATE_LIMIT_PER_MINUTE=7,
        DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP=70,
        DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT=700,
        DOWNLOAD_MAX_CONCURRENT_PER_IP=3,
        DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP=1024,
        DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT=2048,
        DOWNLOAD_SINGLE_ARCHIVE_BYTES_LIMIT=4096,
        DOWNLOAD_SINGLE_VIDEO_BYTES_LIMIT=512,
        DOWNLOAD_VISITOR_VIDEO_DOWNLOAD_ENABLED=False,
        DOWNLOAD_STAFF_LARGE_DOWNLOAD_EXEMPT=True,
        DOWNLOAD_LARGE_DOWNLOAD_WARNING_BYTES=256,
    )
    def test_download_runtime_setting_initializes_from_environment_defaults(self):
        setting = DownloadRuntimeSetting.load()

        self.assertEqual(setting.rate_limit_per_minute, 7)
        self.assertEqual(setting.daily_request_limit_per_ip, 70)
        self.assertEqual(setting.global_daily_request_limit, 700)
        self.assertEqual(setting.max_concurrent_per_ip, 3)
        self.assertEqual(setting.daily_bytes_limit_per_ip, 1024)
        self.assertEqual(setting.global_daily_bytes_limit, 2048)
        self.assertEqual(setting.single_archive_bytes_limit, 4096)
        self.assertEqual(setting.single_video_bytes_limit, 512)
        self.assertFalse(setting.visitor_video_download_enabled)
        self.assertTrue(setting.staff_large_download_exempt)
        self.assertEqual(setting.large_download_warning_bytes, 256)

    def test_prune_observability_logs_dry_run_and_delete_keeps_audit_logs(self):
        parse_log = ParseRequestLog.objects.create(request_id="old-parse")
        download_log = DownloadRequestLog.objects.create(request_id="old-download")
        audit_log = ProviderAdminAuditLog.objects.create(
            request_id="old-audit",
            action="provider_runtime_update",
        )
        old_time = timezone.now() - timezone.timedelta(days=45)
        ParseRequestLog.objects.filter(pk=parse_log.pk).update(created_at=old_time)
        DownloadRequestLog.objects.filter(pk=download_log.pk).update(created_at=old_time)
        ProviderAdminAuditLog.objects.filter(pk=audit_log.pk).update(created_at=old_time)

        out = StringIO()
        call_command("prune_observability_logs", "--days", "30", "--dry-run", stdout=out)
        self.assertEqual(ParseRequestLog.objects.count(), 1)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)

        call_command("prune_observability_logs", "--days", "30", stdout=StringIO())

        self.assertEqual(ParseRequestLog.objects.count(), 0)
        self.assertEqual(DownloadRequestLog.objects.count(), 0)
        self.assertEqual(ProviderAdminAuditLog.objects.count(), 1)
