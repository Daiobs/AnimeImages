from time import time
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from .services.assets import MediaAsset, ParseResult
from .services.platforms.douyin import DOUYIN_HEADERS
from .services.platforms.douyin_kukutool import (
    DouyinKukuToolProvider,
    ValidatedKukuToolCandidate,
    build_kukutool_cache_key,
    cache_validated_candidate,
    get_cached_validated_candidate,
    normalize_kukutool_payload,
    parse_byte_cdn_url,
)
from .services.platforms.douyin_providers import DouyinParseContext
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
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        runner.assert_called_once()
        probe.probe.assert_not_called()

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
