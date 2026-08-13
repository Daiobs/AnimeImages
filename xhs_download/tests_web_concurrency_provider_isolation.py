from pathlib import Path
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    TianTianJieXiRateLimitError,
    build_payload_cache_key,
)
from .services.platforms.kukutool_generic import source_key_for_url
from .services.rate_limits import LimitResult, acquire_concurrency, release_concurrency


ROOT_DIR = Path(__file__).resolve().parents[1]
TIANTIAN_CONCURRENCY_SCOPE = "third-party-tiantianjiexi-global"
TIANTIAN_CONCURRENCY_IDENTITY = "all"


class ProviderConcurrencyCounterTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def acquire(self, limit=2):
        return acquire_concurrency(
            TIANTIAN_CONCURRENCY_SCOPE,
            TIANTIAN_CONCURRENCY_IDENTITY,
            limit,
        )

    def release(self):
        release_concurrency(
            TIANTIAN_CONCURRENCY_SCOPE,
            TIANTIAN_CONCURRENCY_IDENTITY,
        )

    def test_effective_limit_allows_two_slots_and_rejects_the_third(self):
        first = self.acquire()
        second = self.acquire()
        third = self.acquire()

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(third.limit, 2)

        self.release()
        self.release()

    def test_release_restores_one_slot(self):
        self.assertTrue(self.acquire().allowed)
        self.assertTrue(self.acquire().allowed)
        self.assertFalse(self.acquire().allowed)

        self.release()
        self.assertTrue(self.acquire().allowed)

        self.release()
        self.release()

    def test_disabled_limit_does_not_create_a_counter(self):
        result = self.acquire(limit=0)

        self.assertTrue(result.allowed)
        self.assertEqual(result.limit, 0)


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="phone",
    TIANTIANJIEXI_PASSWORD="password",
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2,
    TIANTIANJIEXI_RETRIES=1,
)
class TianTianJieXiProviderConcurrencyTests(TestCase):
    source_url = "https://www.douyin.com/video/123456789012345678"

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_payload_cache_hit_bypasses_global_concurrency(self):
        source_key = source_key_for_url(self.source_url)
        payload = {
            "title": "cached",
            "video_url": "https://v16.tiktokcdn.com/cached.mp4",
        }
        cache.set(build_payload_cache_key(source_key), payload, timeout=60)

        with patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.acquire_concurrency",
            side_effect=AssertionError("cache hit must not reserve concurrency"),
        ):
            result = TianTianJieXiGenericProvider(client=Mock()).parse(
                self.source_url,
                timeout_seconds=1,
            )

        self.assertEqual(result, payload)

    def test_uncached_parse_reserves_configured_two_slot_limit(self):
        client = Mock()
        client.parse_url.return_value = {
            "title": "upstream",
            "video_url": "https://v16.tiktokcdn.com/upstream.mp4",
        }

        with patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.acquire_concurrency",
            return_value=LimitResult(allowed=True, current=1, limit=2),
        ) as acquire, patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.release_concurrency",
        ) as release:
            result = TianTianJieXiGenericProvider(client=client).parse(
                self.source_url,
                timeout_seconds=1,
            )

        self.assertEqual(result["title"], "upstream")
        acquire.assert_called_once_with(
            TIANTIAN_CONCURRENCY_SCOPE,
            TIANTIAN_CONCURRENCY_IDENTITY,
            2,
        )
        release.assert_called_once_with(
            TIANTIAN_CONCURRENCY_SCOPE,
            TIANTIAN_CONCURRENCY_IDENTITY,
        )

    def test_busy_global_counter_skips_upstream_and_does_not_release(self):
        client = Mock()

        with patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.acquire_concurrency",
            return_value=LimitResult(allowed=False, current=3, limit=2),
        ), patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.release_concurrency",
        ) as release:
            with self.assertRaises(TianTianJieXiRateLimitError):
                TianTianJieXiGenericProvider(client=client).parse(
                    self.source_url,
                    timeout_seconds=1,
                )

        client.parse_url.assert_not_called()
        release.assert_not_called()

    def test_upstream_failure_releases_reserved_slot(self):
        client = Mock()
        client.parse_url.side_effect = RuntimeError("controlled upstream failure")

        with patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.acquire_concurrency",
            return_value=LimitResult(allowed=True, current=1, limit=2),
        ), patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.release_concurrency",
        ) as release:
            with self.assertRaisesRegex(RuntimeError, "controlled upstream failure"):
                TianTianJieXiGenericProvider(client=client).parse(
                    self.source_url,
                    timeout_seconds=1,
                )

        release.assert_called_once_with(
            TIANTIAN_CONCURRENCY_SCOPE,
            TIANTIAN_CONCURRENCY_IDENTITY,
        )


class WebConcurrencyConfigurationTests(SimpleTestCase):
    def test_tracked_gunicorn_candidate_is_two_by_four_gthread(self):
        unit = (ROOT_DIR / "ops/systemd/animeimages.service").read_text()
        exec_start = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )

        self.assertIn("--worker-class gthread", exec_start)
        self.assertIn("--workers 2", exec_start)
        self.assertIn("--threads 4", exec_start)
        self.assertIn("--timeout 60", exec_start)
        self.assertNotIn("--timeout 600", exec_start)
