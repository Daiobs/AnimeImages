import json
from io import StringIO
from threading import Event
from time import monotonic, sleep, time
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError
from .services.platforms.douyin import DOUYIN_HEADERS
from .services.platforms.douyin_kukutool import (
    DouyinKukuToolProvider,
    ValidatedKukuToolCandidate,
    build_kukutool_cache_key,
    build_kukutool_profile_cache_key,
    build_kukutool_single_flight_key,
    build_kukutool_safe_env,
    cache_validated_candidate,
    convert_sidecar_payload_to_kukutool_payload,
    find_node_runtime,
    get_cached_validated_candidate,
    get_kukutool_circuit_status,
    normalize_kukutool_payload,
    parse_byte_cdn_url,
    record_kukutool_failure,
    record_kukutool_success,
    run_kukutool_helper,
    run_kukutool_sidecar,
    run_kukutool_upstream,
    run_kukutool_url_helper,
)
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    cache_douyin_metadata,
)
from .services.provider_settings import ProviderSettings
from .services.quality import QualityProbeResult


AWEME_ID = "7647533902413173321"
LOW_URL = (
    "https://v11-default.365yg.com/a/6a2bd115/"
    "video/tos/cn/bucket/normal/?br=2996&qs=0"
)
ULTRA_URL = (
    "https://v26-default.365yg.com/b/7fffffff/"
    "video/tos/cn/bucket/source/?br=53031&qs=13"
)


class DouyinKukuToolTests(SimpleTestCase):
    def tearDown(self):
        cache.clear()

    def test_payload_selects_highest_bitrate_byte_cdn_resource(self):
        resources = normalize_kukutool_payload({
            "videos": [{
                "video_fullinfo": [
                    {"type": "1080p", "size": 4_001_412, "url": LOW_URL},
                    {"type": "超高清", "size": 70_481_987, "url": ULTRA_URL},
                ],
            }],
        })

        self.assertEqual(resources[0].url, ULTRA_URL)
        self.assertEqual(resources[0].bit_rate, 53_031_000)
        self.assertEqual(resources[0].size_bytes, 70_481_987)

    def test_payload_rejects_non_byte_cdn_resource(self):
        resources = normalize_kukutool_payload({
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": "https://example.com/source.mp4?br=53031",
                }],
            }],
        })

        self.assertEqual(resources, ())

    def test_sidecar_payload_converts_to_existing_kukutool_shape(self):
        final_url = (
            "https://v26-default.365yg.com/final/7fffffff/"
            "video/tos/cn/bucket/source-final/"
        )
        payload = convert_sidecar_payload_to_kukutool_payload({
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "cached": True,
            "cache": {"hit": True, "coalesced": False},
            "timing": {"stage": "cache_hit", "total_ms": 12},
            "best": {
                "type": "超高清",
                "size": 70_481_987,
                "url": ULTRA_URL,
                "final_url": final_url,
                "bitrate": 53_031_000,
                "quality_score": 13,
                "object_key": "video/tos/cn/bucket/source",
            },
        })

        resources = normalize_kukutool_payload(payload)

        self.assertEqual(resources[0].url, final_url)
        self.assertEqual(resources[0].size_bytes, 70_481_987)
        self.assertEqual(resources[0].bit_rate, 53_031_000)
        self.assertEqual(resources[0].quality_score, 13)
        self.assertEqual(payload["_sidecar"]["timing"]["stage"], "cache_hit")

    @patch("xhs_download.services.platforms.douyin_kukutool.requests.post")
    def test_run_kukutool_sidecar_uses_stable_contract(self, mock_post):
        response = Mock(
            ok=True,
            status_code=200,
            content=b"{}",
        )
        response.json.return_value = {
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "best": {
                "type": "超高清",
                "size": 70_481_987,
                "url": ULTRA_URL,
            },
        }
        mock_post.return_value = response

        payload = run_kukutool_sidecar(
            AWEME_ID,
            base_url="http://127.0.0.1:8787",
            timeout_seconds=1,
        )

        self.assertEqual(normalize_kukutool_payload(payload)[0].url, ULTRA_URL)
        self.assertEqual(
            mock_post.call_args.args[0],
            "http://127.0.0.1:8787/v1/ultra/resolve",
        )
        self.assertFalse(mock_post.call_args.kwargs["allow_redirects"])

    @patch("xhs_download.services.platforms.douyin_kukutool.run_kukutool_helper")
    @patch("xhs_download.services.platforms.douyin_kukutool.requests.post")
    def test_run_kukutool_upstream_prefers_sidecar(self, mock_post, mock_helper):
        response = Mock(
            ok=True,
            status_code=200,
            content=b"{}",
        )
        response.json.return_value = {
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "best": {
                "type": "超高清",
                "size": 70_481_987,
                "url": ULTRA_URL,
            },
        }
        mock_post.return_value = response

        payload = run_kukutool_upstream(
            AWEME_ID,
            sidecar_base_url="http://127.0.0.1:8787",
            sidecar_timeout_seconds=1,
            timeout_seconds=5,
        )

        self.assertEqual(normalize_kukutool_payload(payload)[0].url, ULTRA_URL)
        mock_helper.assert_not_called()

    @patch("xhs_download.services.platforms.douyin_kukutool.requests.get")
    def test_sidecar_health_does_not_follow_redirects(self, mock_get):
        from .services.platforms.douyin_kukutool import get_kukutool_sidecar_status

        response = Mock(
            ok=True,
            status_code=200,
            content=b"{}",
        )
        response.json.return_value = {
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "ultra_provider": {"status": "ok"},
        }
        mock_get.return_value = response

        status = get_kukutool_sidecar_status(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_sidecar_base_url="http://127.0.0.1:8787",
            ),
            force=True,
        )

        self.assertTrue(status["available"])
        self.assertFalse(mock_get.call_args.kwargs["allow_redirects"])

    @patch("xhs_download.management.commands.inspect_douyin_candidates.get_kukutool_provider_health")
    def test_inspect_douyin_candidates_health_outputs_json(self, mock_health):
        mock_health.return_value = {
            "provider": "kukutool",
            "available": True,
            "runtime": {"available": False, "error": "provider disabled"},
            "sidecar": {
                "configured": True,
                "available": True,
                "base_url": "http://127.0.0.1:8787",
                "error": "",
            },
            "circuit": {"open": False},
        }
        output = StringIO()

        call_command("inspect_douyin_candidates", "--health", "--json", stdout=output)
        payload = json.loads(output.getvalue())

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["provider"]["available"])
        self.assertIn("settings", payload)
        self.assertIn("issues", payload)

    def test_inspect_douyin_candidates_requires_url_without_health(self):
        with self.assertRaisesRegex(CommandError, "URL is required"):
            call_command("inspect_douyin_candidates")

    @patch("xhs_download.services.platforms.douyin_kukutool.run_kukutool_helper")
    @patch("xhs_download.services.platforms.douyin_kukutool.run_kukutool_sidecar")
    def test_run_kukutool_upstream_fallback_uses_remaining_budget(
        self,
        mock_sidecar,
        mock_helper,
    ):
        mock_sidecar.side_effect = OSError("sidecar unavailable")
        mock_helper.return_value = {
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": ULTRA_URL,
                }],
            }],
        }

        payload = run_kukutool_upstream(
            AWEME_ID,
            sidecar_base_url="http://127.0.0.1:8787",
            sidecar_timeout_seconds=0.2,
            timeout_seconds=1.0,
        )

        self.assertEqual(normalize_kukutool_payload(payload)[0].url, ULTRA_URL)
        self.assertLessEqual(mock_helper.call_args.kwargs["timeout_seconds"], 1.0)
        self.assertGreater(mock_helper.call_args.kwargs["timeout_seconds"], 0)

    @patch("xhs_download.services.platforms.douyin_kukutool.run_kukutool_helper")
    @patch("xhs_download.services.platforms.douyin_kukutool.run_kukutool_sidecar")
    @patch("xhs_download.services.platforms.douyin_kukutool.monotonic")
    def test_run_kukutool_upstream_does_not_exceed_budget_after_sidecar_timeout(
        self,
        mock_monotonic,
        mock_sidecar,
        mock_helper,
    ):
        mock_monotonic.side_effect = [10.0, 11.1, 11.1]
        mock_sidecar.side_effect = OSError("sidecar timed out")

        with self.assertRaisesRegex(OSError, "sidecar timed out"):
            run_kukutool_upstream(
                AWEME_ID,
                sidecar_base_url="http://127.0.0.1:8787",
                sidecar_timeout_seconds=1.0,
                timeout_seconds=1.0,
            )

        mock_helper.assert_not_called()

    def test_payload_deduplicates_rotating_urls_by_tos_object_key(self):
        second_url = (
            "https://v9-default.365yg.com/another/7ffffffe/"
            "video/tos/cn/bucket/source/?br=60000&qs=13"
        )
        resources = normalize_kukutool_payload({
            "videos": [{
                "video_fullinfo": [
                    {"type": "超高清", "size": 70_481_987, "url": ULTRA_URL},
                    {"type": "Original", "size": 80_000_000, "url": second_url},
                ],
            }],
        })

        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].url, second_url)

    def test_parse_byte_cdn_url_extracts_stable_object_and_expiry(self):
        metadata = parse_byte_cdn_url(ULTRA_URL)

        self.assertEqual(metadata["object_key"], "video/tos/cn/bucket/source")
        self.assertEqual(metadata["expires_at"], int("7fffffff", 16))

    def test_expired_signed_candidate_is_removed_from_cache(self):
        cache_key = build_kukutool_cache_key(AWEME_ID)
        validated = ValidatedKukuToolCandidate(
            candidate=normalize_kukutool_payload({
                "videos": [{
                    "video_fullinfo": [{
                        "type": "超高清",
                        "size": 70_481_987,
                        "url": ULTRA_URL,
                    }],
                }],
            })[0].to_candidate(),
            final_url=ULTRA_URL,
            content_type="video/mp4",
            object_key="video/tos/cn/bucket/source",
            signed_url_expires_at=int(time()) + 20,
        )

        cache_validated_candidate(cache_key, validated, 300)

        self.assertIsNone(
            get_cached_validated_candidate(cache_key, ("365yg.com",))
        )

    def test_cached_candidate_without_object_key_is_rejected(self):
        cache_key = build_kukutool_cache_key(AWEME_ID)
        cache.set(
            cache_key,
            {
                "url": ULTRA_URL,
                "size_bytes": 70_481_987,
                "quality_label": "超高清",
                "signed_url_expires_at": int(time()) + 3600,
            },
            timeout=300,
        )

        self.assertIsNone(
            get_cached_validated_candidate(cache_key, ("365yg.com",))
        )

    def test_provider_uses_verified_kukutool_result_without_size_comparison(self):
        payload = {
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 7_000_000,
                    "url": ULTRA_URL,
                }],
            }],
        }
        runner = Mock(return_value=payload)
        probe = Mock()
        probe.probe.return_value = QualityProbeResult(
            url=ULTRA_URL,
            final_url=ULTRA_URL,
            reachable=True,
            size_bytes=7_000_000,
            content_type="video/mp4",
            method="RANGE",
        )

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
                            size_bytes=20_000_000,
                            cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                        ),
                    ),
                )

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
            ),
            runner=runner,
            quality_probe=probe,
        )
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(result.video_assets[0].url, ULTRA_URL)
        self.assertEqual(result.video_assets[0].size_bytes, 7_000_000)
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.jpeg")
        self.assertEqual(result.video_assets[0].provider, "kukutool")
        self.assertEqual(result.video_assets[0].provider_resource_id, AWEME_ID)
        self.assertEqual(
            result.video_assets[0].provider_cache_key,
            build_kukutool_cache_key(AWEME_ID),
        )
        self.assertTrue(result.video_assets[0].provider_manifest_token)
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        runner.assert_called_once()
        probe.probe.assert_not_called()

    @patch("xhs_download.services.platforms.douyin_kukutool.requests.post")
    def test_provider_can_use_sidecar_without_node_helper(self, mock_post):
        response = Mock(
            ok=True,
            status_code=200,
            content=b"{}",
        )
        response.json.return_value = {
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "cached": False,
            "timing": {"stage": "verified", "total_ms": 100},
            "best": {
                "type": "超高清",
                "size": 70_481_987,
                "url": ULTRA_URL,
            },
        }
        mock_post.return_value = response

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(self, _aweme_id, **_options):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native.mp4",
                            platform="douyin",
                            cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                        ),
                    ),
                )

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_sidecar_base_url="http://127.0.0.1:8787",
            ),
        )
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(result.video_assets[0].url, ULTRA_URL)
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(
            result.metadata["fallback_reason"],
            "kukutool_validated",
        )
        mock_post.assert_called()

    def test_provider_probes_when_preferred_resource_has_no_declared_size(self):
        payload = {
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 0,
                    "url": ULTRA_URL,
                }],
            }],
        }
        runner = Mock(return_value=payload)
        probe = Mock()
        probe.probe.return_value = QualityProbeResult(
            url=ULTRA_URL,
            final_url=ULTRA_URL,
            reachable=True,
            size_bytes=70_481_987,
            content_type="video/mp4",
            method="RANGE",
        )
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
            ),
            runner=runner,
            quality_probe=probe,
        )

        selected = provider.resolve_validated_candidate(
            adapter,
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(selected.candidate.size_bytes, 70_481_987)
        self.assertEqual(probe.probe.call_count, 1)

    def test_provider_probes_when_signed_expiry_is_not_exposed(self):
        unsigned_url = (
            "https://v26-default.365yg.com/token/not-an-expiry/"
            "video/tos/cn/bucket/source/?br=53031&qs=13"
        )
        runner = Mock(return_value={
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": unsigned_url,
                }],
            }],
        })
        probe = Mock()
        probe.probe.return_value = QualityProbeResult(
            url=unsigned_url,
            final_url=unsigned_url,
            reachable=True,
            size_bytes=70_481_987,
            content_type="video/mp4",
            method="RANGE",
        )
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
            ),
            runner=runner,
            quality_probe=probe,
        )

        selected = provider.resolve_validated_candidate(
            adapter,
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(selected.candidate.url, unsigned_url)
        self.assertEqual(probe.probe.call_count, 1)

    def test_provider_falls_back_to_native_when_upstream_fails(self):
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

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
            ),
            runner=Mock(side_effect=OSError("upstream unavailable")),
        )
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/native.mp4")
        self.assertEqual(result.metadata["selected_provider"], "native")
        self.assertEqual(result.metadata["fallback_reason"], "kukutool_unavailable")

    def test_provider_falls_back_to_native_when_process_capacity_is_full(self):
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

        semaphore = Mock()
        semaphore.acquire.return_value = False
        runner = Mock()
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
            ),
            runner=runner,
        )

        with patch(
            "xhs_download.services.platforms.douyin_kukutool.get_kukutool_semaphore",
            return_value=semaphore,
        ):
            result = provider.parse(
                FakeAdapter(),
                DouyinParseContext(
                    url=f"https://www.douyin.com/video/{AWEME_ID}",
                    aweme_id=AWEME_ID,
                ),
            )

        self.assertEqual(result.metadata["selected_provider"], "native")
        runner.assert_not_called()

    def test_deadline_returns_native_while_kukutool_finishes_cache_in_background(self):
        payload = {
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": ULTRA_URL,
                }],
            }],
        }

        def slow_runner(*_args, **_kwargs):
            sleep(0.15)
            return payload

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(self, _aweme_id, **_options):
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

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                ultra_total_timeout_seconds=0.05,
                ultra_native_timeout_seconds=0.05,
                kukutool_timeout_seconds=1.0,
                kukutool_global_max_concurrency=0,
                kukutool_failure_cache_ttl_seconds=0,
                kukutool_reference_wait_seconds=0,
            ),
            runner=slow_runner,
        )
        started_at = monotonic()
        result = provider.parse(
            FakeAdapter(),
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertLess(monotonic() - started_at, 0.14)
        self.assertEqual(result.metadata["selected_provider"], "native")
        self.assertEqual(result.metadata["fallback_reason"], "kukutool_deadline")
        self.assertTrue(result.metadata["kukutool_background_refresh"])

        cache_key = build_kukutool_cache_key(AWEME_ID)
        for _ in range(20):
            if cache.get(cache_key):
                break
            sleep(0.02)
        self.assertIsNotNone(cache.get(cache_key))

    def test_deadline_raises_retryable_error_when_both_paths_are_still_running(self):
        def slow_runner(*_args, **_kwargs):
            sleep(0.15)
            return {"videos": []}

        class SlowAdapter:
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(self, _aweme_id, **_options):
                sleep(0.15)
                return None

            def fetch_mobile_share_result(self, _aweme_id, **_options):
                sleep(0.15)
                return None

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                ultra_total_timeout_seconds=0.05,
                ultra_native_timeout_seconds=0.05,
                kukutool_timeout_seconds=1.0,
                kukutool_global_max_concurrency=0,
                kukutool_failure_cache_ttl_seconds=0,
            ),
            runner=slow_runner,
        )

        with self.assertRaisesRegex(XhsProviderTimeoutError, "请稍后重试"):
            provider.parse(
                SlowAdapter(),
                DouyinParseContext(
                    url=f"https://www.douyin.com/video/{AWEME_ID}",
                    aweme_id=AWEME_ID,
                ),
            )
        sleep(0.2)

    def test_local_single_flight_reuses_running_kukutool_future(self):
        release_runner = Event()
        runner = Mock()

        def wait_for_release(*_args, **_kwargs):
            release_runner.wait(timeout=1)
            return {"videos": []}

        runner.side_effect = wait_for_release
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_timeout_seconds=1.0,
                kukutool_global_max_concurrency=0,
                kukutool_failure_cache_ttl_seconds=0,
            ),
            runner=runner,
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
        )

        first_future, first_state = provider.get_or_start_kukutool_future(
            adapter,
            context,
        )
        second_future, second_state = provider.get_or_start_kukutool_future(
            adapter,
            context,
        )

        self.assertEqual(first_state, "started")
        self.assertEqual(second_state, "local_single_flight")
        self.assertIs(first_future, second_future)
        release_runner.set()
        first_future.result(timeout=1)
        runner.assert_called_once()

    def test_remote_single_flight_does_not_start_duplicate_upstream(self):
        aweme_id = "7647533902413173322"
        cache.add(
            build_kukutool_single_flight_key(aweme_id),
            "another-worker",
            timeout=30,
        )
        runner = Mock()
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_global_max_concurrency=0,
                kukutool_failure_cache_ttl_seconds=0,
            ),
            runner=runner,
        )

        future, state = provider.get_or_start_kukutool_future(
            adapter,
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{aweme_id}",
                aweme_id=aweme_id,
            ),
        )

        self.assertIsNone(future)
        self.assertEqual(state, "remote_single_flight")
        runner.assert_not_called()

    def test_hot_candidate_and_metadata_cache_skip_native_reference_wait(self):
        cache_douyin_metadata(
            AWEME_ID,
            ParseResult(
                platform="douyin",
                post_type="video",
                base_filename="cached-title",
                assets=(
                    MediaAsset(
                        media_type="video",
                        url="https://v3-web.douyinvod.com/native.mp4",
                        platform="douyin",
                        cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                    ),
                ),
            ),
        )
        cache_validated_candidate(
            build_kukutool_cache_key(AWEME_ID),
            ValidatedKukuToolCandidate(
                candidate=normalize_kukutool_payload({
                    "videos": [{
                        "video_fullinfo": [{
                            "type": "超高清",
                            "size": 70_481_987,
                            "url": ULTRA_URL,
                        }],
                    }],
                })[0].to_candidate(),
                final_url=ULTRA_URL,
                content_type="video/mp4",
                object_key="video/tos/cn/bucket/source",
                signed_url_expires_at=int(time()) + 3600,
            ),
            300,
        )
        adapter = Mock()
        adapter.platform_id = "douyin"
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com", "douyinvod.com")
        adapter.fetch_mobile_feed_result.side_effect = AssertionError(
            "native should not start on a complete hot cache hit"
        )
        runner = Mock()
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_reference_wait_seconds=0.5,
            ),
            runner=runner,
        )

        started_at = monotonic()
        result = provider.parse(
            adapter,
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertLess(monotonic() - started_at, 0.2)
        self.assertEqual(result.base_filename, "cached-title")
        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://p3-sign.douyinpic.com/cover.jpeg",
        )
        adapter.fetch_mobile_feed_result.assert_not_called()
        runner.assert_not_called()

    def test_native_metadata_cache_keeps_title_and_author(self):
        adapter = Mock()
        adapter.fetch_mobile_feed_result.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="作者_标题",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v3-web.douyinvod.com/native.mp4",
                    platform="douyin",
                    cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                ),
            ),
            metadata={
                "aweme_id": AWEME_ID,
                "author": "作者",
                "title": "标题",
            },
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
        )

        from .services.platforms.douyin_providers import (
            get_cached_douyin_metadata,
            get_fast_native_result,
        )

        get_fast_native_result(adapter, context)
        cached = get_cached_douyin_metadata(AWEME_ID)

        self.assertEqual(cached.metadata["author"], "作者")
        self.assertEqual(cached.metadata["title"], "标题")

    def test_safe_child_environment_excludes_django_secrets(self):
        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C.UTF-8",
                "DJANGO_SECRET_KEY": "do-not-forward",
                "DATABASE_URL": "postgres://secret",
            },
            clear=True,
        ):
            child_env = build_kukutool_safe_env()

        self.assertEqual(
            child_env,
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C.UTF-8",
            },
        )

    def test_safe_child_environment_keeps_windows_runtime_vars(self):
        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C.UTF-8",
                "SystemRoot": "C:\\Windows",
                "WINDIR": "C:\\Windows",
                "DJANGO_SECRET_KEY": "do-not-forward",
            },
            clear=True,
        ):
            child_env = build_kukutool_safe_env()

        self.assertEqual(child_env["SystemRoot"], "C:\\Windows")
        self.assertEqual(child_env["WINDIR"], "C:\\Windows")
        self.assertNotIn("DJANGO_SECRET_KEY", child_env)

    def test_circuit_opens_after_threshold_and_success_resets_it(self):
        provider_settings = ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("kukutool", "native"),
            kukutool_circuit_failure_threshold=2,
            kukutool_circuit_window_seconds=60,
            kukutool_circuit_open_seconds=120,
        )

        record_kukutool_failure(provider_settings)
        self.assertFalse(get_kukutool_circuit_status()["open"])
        record_kukutool_failure(provider_settings)
        self.assertTrue(get_kukutool_circuit_status()["open"])

        record_kukutool_success(provider_settings)
        self.assertFalse(get_kukutool_circuit_status()["open"])

    def test_global_request_limit_blocks_second_cold_upstream_call(self):
        runner = Mock(return_value={
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": ULTRA_URL,
                }],
            }],
        })
        adapter = Mock()
        adapter.headers = DOUYIN_HEADERS
        adapter.media_host_suffixes = ("365yg.com",)
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_global_request_limit_per_minute=1,
            ),
            runner=runner,
        )

        first = provider.resolve_validated_candidate(
            adapter,
            DouyinParseContext(
                url=f"https://www.douyin.com/video/{AWEME_ID}",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertIsNotNone(first)
        with self.assertRaisesRegex(RuntimeError, "global request limit"):
            provider.resolve_validated_candidate(
                adapter,
                DouyinParseContext(
                    url="https://www.douyin.com/video/7647533902413173399",
                    aweme_id="7647533902413173399",
                ),
            )
        runner.assert_called_once()

    @patch("xhs_download.services.platforms.douyin_kukutool.subprocess.run")
    @patch("xhs_download.services.platforms.douyin_kukutool.shutil.which")
    def test_production_runtime_discovery_does_not_search_local_fallbacks(
        self,
        mock_which,
        mock_run,
    ):
        self.assertEqual(
            find_node_runtime("", allow_development_fallbacks=False),
            "",
        )
        mock_which.assert_not_called()
        mock_run.assert_not_called()

    @patch("xhs_download.services.platforms.douyin_kukutool.subprocess.run")
    def test_runtime_probe_uses_minimal_child_environment(self, mock_run):
        mock_run.return_value = Mock(returncode=0)

        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "LANG": "C.UTF-8",
                "DJANGO_SECRET_KEY": "do-not-forward",
            },
            clear=True,
        ):
            selected = find_node_runtime(
                "/usr/bin/node",
                allow_development_fallbacks=False,
            )

        self.assertEqual(selected, "/usr/bin/node")
        child_env = mock_run.call_args.kwargs["env"]
        self.assertNotIn("DJANGO_SECRET_KEY", child_env)
        self.assertEqual(child_env["PATH"], "/usr/bin")

    @patch(
        "xhs_download.services.platforms.douyin_kukutool.get_kukutool_runtime_status",
        return_value={
            "checked": True,
            "available": True,
            "node_binary": "/usr/bin/node",
            "error": "",
            "checked_at": 1,
        },
    )
    @patch("xhs_download.services.platforms.douyin_kukutool.subprocess.run")
    def test_helper_caches_successful_profile_for_future_node_processes(
        self,
        mock_run,
        _mock_runtime,
    ):
        profile = {
            "id": "profile-1",
            "authRoute": "auth-profile",
            "authResponseFields": {"key": "key-field", "seed": "seed-field"},
            "parseRequestFields": {
                "key": "request-key",
                "payload": "request-payload",
                "iv": "request-iv",
                "version": "request-version",
            },
        }
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"result": {"videos": []}, "profile": profile}),
            stderr="",
        )

        result = run_kukutool_helper(
            AWEME_ID,
            timeout_seconds=1,
            profile_cache_ttl_seconds=60,
        )

        self.assertEqual(result, {"videos": []})
        self.assertEqual(mock_run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(mock_run.call_args.kwargs["errors"], "replace")
        self.assertEqual(
            cache.get(build_kukutool_profile_cache_key()),
            profile,
        )

    @patch(
        "xhs_download.services.platforms.douyin_kukutool.get_kukutool_runtime_status",
        return_value={
            "checked": True,
            "available": True,
            "node_binary": "/usr/bin/node",
            "error": "",
            "checked_at": 1,
        },
    )
    @patch("xhs_download.services.platforms.douyin_kukutool.subprocess.run")
    def test_url_helper_passes_public_url_to_node_process(
        self,
        mock_run,
        _mock_runtime,
    ):
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"result": {"pics": ["https://cdn.example.com/a.jpg"]}}),
            stderr="",
        )

        result = run_kukutool_url_helper(
            "https://www.instagram.com/p/example/",
            timeout_seconds=1,
        )

        self.assertEqual(result, {"pics": ["https://cdn.example.com/a.jpg"]})
        self.assertEqual(
            mock_run.call_args.args[0][2],
            "https://www.instagram.com/p/example/",
        )
