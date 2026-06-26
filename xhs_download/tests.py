from io import BytesIO
import json
import os
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import parse_qs
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests
from django.core.cache import cache
from django.core.checks import run_checks
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from PIL import Image

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
    fetch_item_to_temp_path,
    fetch_to_temp_path,
    get_media_headers,
    normalize_quality,
    normalize_save_format,
    normalize_video_save_mode,
    provider_manifest_allows_public_download,
    request_public_provider_get_with_retry,
    resolve_server_save_folder,
    save_media_items_to_server,
)
from .services.assets import MediaAsset, ParseResult
from .services.candidates import MediaCandidate, dedupe_candidates, rank_media_candidates
from .services.diagnostics import CandidateDiagnostic, select_best_media_candidate
from .services.provider_settings import (
    ProviderSettings,
    get_platform_provider_settings,
    normalize_provider_order,
)
from .services.provider_manifest import build_provider_manifest_token
from .views import build_provider_preview_query
from .services.quality import QualityProbe, QualityProbeResult
from .services.rate_limits import (
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
            ("kukutool_generic", "gallery_dl_generic", "yt_dlp_generic"),
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
    )
    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_generic_provider_download_allows_public_url(
        self,
        mock_get,
        _mock_getaddrinfo,
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
    )
    @patch(
        "xhs_download.services.common.socket.getaddrinfo",
        return_value=[(None, None, None, "", ("93.184.216.34", 443))],
    )
    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_generic_provider_video_cover_download_allows_public_url(
        self,
        mock_get,
        _mock_getaddrinfo,
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


class XhsDownloadViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_home_route_renders_download_page(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "星空的高清下载小站")
        self.assertContains(response, "theme-home")
        self.assertContains(response, "nav-settings-btn")
        self.assertContains(response, "css/app.css?v=")
        self.assertContains(response, "js/xhs_download.js?v=")
        self.assertContains(response, "data-download-task-url")

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
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}],"base_filename":"base"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["X-Saved-Count"], "1")

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
                data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
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
        payload = '{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}'

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
        self.assertEqual(mock_archive.call_count, 1)
        self.assertEqual(get_daily_total("download-requests-global", "all"), 1)

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=False)
    def test_download_task_endpoint_is_disabled_in_production_mode(self):
        response = Client().post(
            "/media/download-tasks/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("同步下载接口", response.json()["message"])

    def test_server_save_mode_is_forbidden_when_disabled(self):
        with override_settings(XHS_ALLOW_SERVER_SAVE=False):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)

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


class RuntimeHealthViewTests(SimpleTestCase):
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
        YTDLP_BINARY="/usr/bin/python -m missing_yt_dlp_module",
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
    )
    def test_system_check_allows_private_third_party_without_enabled_provider(self):
        issue_ids = {issue.id for issue in run_checks()}

        self.assertNotIn("xhs_download.E007", issue_ids)


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
        summary = response.json()["third_party_summary"]
        self.assertFalse(summary["enabled"])
        self.assertFalse(summary["public_access_enabled"])
        self.assertFalse(summary["configured"])
        self.assertFalse(summary["public_ready"])
        self.assertEqual(summary["enabled_provider_ids"], [])
        self.assertEqual(summary["ready_provider_ids"], [])
        self.assertIn("provider_states", summary)
        self.assertIn("kukutool_generic", summary["provider_states"])
        self.assertEqual(summary["issue"], "")

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
        self.assertEqual(summary["enabled_provider_ids"], ["kukutool_generic"])
        self.assertEqual(summary["ready_provider_ids"], ["kukutool_generic"])
        self.assertTrue(summary["provider_states"]["kukutool_generic"]["enabled"])
        self.assertTrue(summary["provider_states"]["kukutool_generic"]["ready"])
        self.assertEqual(summary["issue"], "")

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

    def test_provider_control_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            "/admin/provider-health/control/",
            data=json.dumps({"action": "disable"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
