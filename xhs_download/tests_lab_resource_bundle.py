from unittest.mock import patch

from django.core.cache import cache, caches
from django.test import SimpleTestCase, override_settings

from .services.provider_lab.contracts import ProviderLabError
from .services.provider_lab import resources
from .services.provider_lab.resources import (
    RESOURCE_BUNDLE_CACHE_PREFIX, RESOURCE_MAX_USES,
    consume_resource_token, create_resource_bundle, issue_resource_token,
    store_resource_bundle,
)


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "lab-bundle-regression", "OPTIONS": {"MAX_ENTRIES": 300},
}})
class LabResourceBundleTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def issue(self, index, bundle=None):
        return issue_resource_token(
            user_id=7, request_id="offline-comparison", provider_id="yt_dlp_existing",
            asset_id=f"format-{index}", url=f"https://media.example/{index}.mp4",
            media_type="video", resource_bundle=bundle,
        )

    def consume(self, token, index, user_id=7):
        return consume_resource_token(
            token=token, user_id=user_id, request_id="offline-comparison",
            provider_id="yt_dlp_existing", asset_id=f"format-{index}",
        )

    def test_legacy_per_asset_cache_reproduces_eviction(self):
        first = self.issue(0)
        for index in range(1, 350):
            self.issue(index)
        with self.assertRaises(ProviderLabError):
            self.consume(first, 0)

    def test_full_bounded_result_survives_default_cache_capacity(self):
        bundle = create_resource_bundle()
        tokens = [self.issue(index, bundle) for index in range(1000)]
        store_resource_bundle(bundle)
        for index in (0, 63, 64, 349, 999):
            self.assertEqual(self.consume(tokens[index], index).asset_id, f"format-{index}")

    def test_binding_and_use_limit_remain_per_asset(self):
        bundle = create_resource_bundle()
        first, second = self.issue(0, bundle), self.issue(1, bundle)
        store_resource_bundle(bundle)
        for token, index, user_id in ((first, 0, 8), (first, 1, 7), (first + "x", 0, 7)):
            with self.assertRaises(ProviderLabError):
                self.consume(token, index, user_id)
        for expected in range(1, RESOURCE_MAX_USES + 1):
            self.assertEqual(self.consume(first, 0).uses, expected)
        with self.assertRaises(ProviderLabError):
            self.consume(first, 0)
        self.assertEqual(self.consume(second, 1).uses, 1)

    def test_expired_or_revoked_bundle_fails_closed(self):
        bundle = create_resource_bundle()
        token = self.issue(0, bundle)
        store_resource_bundle(bundle)
        with patch("xhs_download.services.provider_lab.resources.time.time",
                   return_value=bundle["expires_at"] + 1):
            with self.assertRaises(ProviderLabError):
                self.consume(token, 0)
        cache.delete(f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}")
        with self.assertRaises(ProviderLabError):
            self.consume(token, 0)

    def test_busy_counter_update_fails_closed_without_consuming(self):
        bundle = create_resource_bundle()
        token = self.issue(0, bundle)
        store_resource_bundle(bundle)
        key = f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}:lock"
        cache.add(key, "other-reader", timeout=5)
        with self.assertRaises(ProviderLabError):
            self.consume(token, 0)
        cache.delete(key)
        self.assertEqual(self.consume(token, 0).uses, 1)

    def test_absolute_expiry_is_not_extended_by_reads(self):
        bundle = create_resource_bundle()
        token = self.issue(0, bundle)
        store_resource_bundle(bundle)
        self.consume(token, 0)
        saved = cache.get(f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}")
        self.assertEqual(saved["expires_at"], bundle["expires_at"])
        self.assertEqual(saved["resources"][token]["expires_at"], bundle["expires_at"])

    def test_count_and_byte_budget_fail_closed(self):
        bundle = create_resource_bundle()
        bundle["resources"] = {str(i): {} for i in range(1000)}
        with self.assertRaises(ProviderLabError):
            self.issue(1000, bundle)
        bundle["resources"] = {"oversized": "x" * (2 * 1024 * 1024)}
        with self.assertRaises(ProviderLabError):
            store_resource_bundle(bundle)

    def test_delayed_reader_cannot_create_eleventh_success_after_old_lease(self):
        backend = caches['default']
        now = [1000.0]
        with patch.object(resources, 'cache', backend), \
                patch.object(resources.time, 'time', side_effect=lambda: now[0]):
            bundle = create_resource_bundle()
            token = self.issue(0, bundle)
            store_resource_bundle(bundle)
            for expected in range(1, RESOURCE_MAX_USES):
                self.assertEqual(self.consume(token, 0).uses, expected)
            key = f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}"
            original_get = backend.get
            paused = False
            competing_successes = []
            competing_errors = []

            def pause_after_snapshot(cache_key, *args, **kwargs):
                nonlocal paused
                snapshot = original_get(cache_key, *args, **kwargs)
                if cache_key == key and not paused:
                    paused = True
                    now[0] += 6
                    try:
                        competing_successes.append(self.consume(token, 0).uses)
                    except ProviderLabError as error:
                        competing_errors.append(error.code)
                return snapshot

            with patch.object(backend, 'get', side_effect=pause_after_snapshot):
                self.assertEqual(self.consume(token, 0).uses, RESOURCE_MAX_USES)
            self.assertEqual(competing_successes, [], 'A stale reader allowed an eleventh successful use')
            self.assertEqual(competing_errors, ['PROVIDER_RATE_LIMITED'])
            self.assertEqual(backend.get(key)['resources'][token]['uses'], RESOURCE_MAX_USES)
            with self.assertRaises(ProviderLabError):
                self.consume(token, 0)

    def test_reader_paused_beyond_absolute_ttl_cannot_return(self):
        backend = caches['default']
        now = [1000.0]
        with patch.object(resources, 'cache', backend), \
                patch.object(resources.time, 'time', side_effect=lambda: now[0]):
            bundle = create_resource_bundle()
            token = self.issue(0, bundle)
            store_resource_bundle(bundle)
            key = f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}"
            original_get = backend.get

            def pause_after_snapshot(cache_key, *args, **kwargs):
                snapshot = original_get(cache_key, *args, **kwargs)
                if cache_key == key:
                    now[0] = bundle['expires_at'] + 1
                return snapshot

            with patch.object(backend, 'get', side_effect=pause_after_snapshot), \
                    patch.object(backend, 'set', wraps=backend.set) as writes:
                with self.assertRaises(ProviderLabError):
                    self.consume(token, 0)
            writes.assert_not_called()

    def test_write_reaching_exact_expiry_or_later_cannot_return_descriptor(self):
        backend = caches['default']
        for offset in (0, 1):
            with self.subTest(offset=offset):
                backend.clear()
                now = [1000.0]
                with patch.object(resources, 'cache', backend), \
                        patch.object(resources.time, 'time', side_effect=lambda: now[0]):
                    bundle = create_resource_bundle()
                    token = self.issue(0, bundle)
                    store_resource_bundle(bundle)
                    key = f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}"
                    original_set = backend.set

                    def delayed_write(cache_key, *args, **kwargs):
                        if cache_key == key:
                            now[0] = bundle['expires_at'] + offset
                        return original_set(cache_key, *args, **kwargs)

                    with patch.object(backend, 'set', side_effect=delayed_write):
                        with self.assertRaises(ProviderLabError):
                            self.consume(token, 0)
                    with self.assertRaises(ProviderLabError):
                        self.consume(token, 0)
