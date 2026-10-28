"""P5 fetch checks through the existing client/provider and final HTTP boundary."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.pipeline import Failure, FailureKind
from .services.platforms import tiantian_serial
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient,
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
    build_payload_cache_key,
    build_single_flight_key,
    source_key_for_request,
    unwrap_api_payload,
)
from .services.provider_adapters.tiantian import TianTianAdapter


SOURCE_URL = "https://www.tiktok.com/@demo/video/123"
MEDIA_URL = "https://v16.tiktokcdn.com/demo-1080.mp4"
MODULE = "xhs_download.services.platforms.tiantianjiexi_generic"


def raw_video():
    return {
        "code": 200,
        "message": "success",
        "data": {
            "id": "123",
            "title": "demo video",
            "video_url": MEDIA_URL,
            "quality": "1080P",
            "upstream_only": {"nested": ["retained", {"value": 7}]},
        },
        "request_id": "synthetic-fetch-evidence",
    }


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_API_BASE_URL="https://service.example/ajax/detail.php",
    TIANTIANJIEXI_PHONE="fetch-fixture-phone",
    TIANTIANJIEXI_PASSWORD="fetch-fixture-password",
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_TIMEOUT_SECONDS=5,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=20,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2,
    TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS=120,
    TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=8,
    TIANTIANJIEXI_CIRCUIT_FAILURE_THRESHOLD=5,
    TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS=60,
    TIANTIANJIEXI_CIRCUIT_OPEN_SECONDS=120,
)
class TianTianAdapterFetchTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(override_settings(CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"tiantian-adapter-fetch-{uuid4().hex}",
            },
        }))
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(tiantian_serial, "lock_root", return_value=Path(directory)))
        self.session = self.enterContext(requests.Session())
        self.post = self.enterContext(patch.object(self.session, "post"))
        self.raw = raw_video()
        self.response = Mock(spec=requests.Response)
        self.response.status_code = 200
        self.response.json.return_value = self.raw
        self.post.return_value = self.response
        self.adapter = TianTianAdapter(session=self.session)
        self.source_key = source_key_for_request(SOURCE_URL)

    def normalized(self):
        return TianTianJieXiResultNormalizer.normalize_payload(unwrap_api_payload(self.raw))

    def assert_failure(self, result, kind, code=None):
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, kind)
        self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
        if code is not None:
            self.assertEqual(result.code, code)
        for private in (SOURCE_URL, "fetch-fixture-password", "upstream-secret"):
            self.assertNotIn(private, repr(result))

    def test_client_observes_raw_without_changing_legacy_normalized_return(self):
        seen = []
        expected_raw = deepcopy(self.raw)
        expected_normalized = self.normalized()
        plain = TianTianJieXiApiClient(session=self.session).parse_url(SOURCE_URL, timeout_seconds=1)
        observed = TianTianJieXiApiClient(
            session=self.session, observe=lambda payload: seen.append(deepcopy(payload)),
        ).parse_url(SOURCE_URL, timeout_seconds=1)

        self.assertEqual(seen, [expected_raw])
        self.assertEqual(plain, expected_normalized)
        self.assertEqual(observed, plain)
        self.assertEqual(self.raw, expected_raw)
        self.assertNotIn("data", observed)
        self.assertEqual(self.post.call_count, 2)

    def test_fetch_returns_deepcopied_raw_not_the_normalized_cached_payload(self):
        before = deepcopy(self.raw)
        expected_normalized = self.normalized()

        result = self.adapter.fetch(SOURCE_URL, budget=0.75)

        self.assertEqual(result, before)
        self.assertEqual(self.raw, before)
        self.assertIsNot(result, self.raw)
        self.assertIsNot(result["data"]["upstream_only"], self.raw["data"]["upstream_only"])
        self.assertEqual(cache.get(build_payload_cache_key(self.source_key)), expected_normalized)
        self.assertNotEqual(result, expected_normalized)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args, ("https://service.example/ajax/detail.php",))
        self.assertEqual(kwargs["data"], {
            "phone": "fetch-fixture-phone", "password": "fetch-fixture-password", "link": SOURCE_URL,
        })
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 0.75)
        result["data"]["upstream_only"]["nested"][1]["value"] = 99
        self.assertEqual(self.raw, before)

    def test_valid_normalized_cache_is_not_reconstructed_or_refetched(self):
        normalized = self.normalized()
        TianTianJieXiGenericProvider().cache_payload(self.source_key, normalized)

        with self.assertLogs("xhs_download.provider_reliability", level="INFO") as logs:
            result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.INVALID_RESULT, "TIANTIAN_NO_RAW")
        self.post.assert_not_called()
        self.assertEqual(cache.get(build_payload_cache_key(self.source_key)), normalized)
        self.assertEqual(logs.records[-1].outcome, "SUCCESS_PAYLOAD_CACHE")

    def test_single_flight_cached_result_does_not_trigger_http_for_raw(self):
        normalized = self.normalized()
        lock_key = build_single_flight_key(self.source_key)
        cache.set(lock_key, "other-owner", timeout=10)

        def publish_during_poll(_seconds):
            TianTianJieXiGenericProvider().cache_payload(self.source_key, normalized)

        with patch(f"{MODULE}.time.sleep", side_effect=publish_during_poll) as sleep, \
                self.assertLogs("xhs_download.provider_reliability", level="INFO") as logs:
            result = self.adapter.fetch(SOURCE_URL, budget=1)

        sleep.assert_called_once()
        self.assert_failure(result, FailureKind.INVALID_RESULT, "TIANTIAN_NO_RAW")
        self.post.assert_not_called()
        self.assertEqual(cache.get(lock_key), "other-owner")
        self.assertEqual(logs.records[-1].outcome, "SUCCESS_SINGLE_FLIGHT")
        self.assertTrue(logs.records[-1].single_flight_wait)

    @override_settings(TIANTIANJIEXI_RETRIES=3)
    def test_timeout_is_safe_and_does_not_add_a_retry(self):
        self.post.side_effect = requests.Timeout(f"{SOURCE_URL}?token=upstream-secret")

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.TIMEOUT, "TIANTIAN_TIMEOUT")
        self.post.assert_called_once()

    def test_http_412_is_unavailable_without_response_details(self):
        self.response.status_code = 412
        self.response.raise_for_status.side_effect = requests.HTTPError(
            f"{SOURCE_URL}?token=upstream-secret", response=self.response,
        )

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.UNAVAILABLE, "HTTP_412")
        self.response.json.assert_not_called()
        self.post.assert_called_once()

    def test_invalid_json_is_invalid_result_not_upstream_text(self):
        for error in (
            ValueError("upstream-secret"),
            requests.exceptions.JSONDecodeError("upstream-secret", "not-json", 0),
        ):
            with self.subTest(error=type(error).__name__):
                self.response.json.side_effect = error
                result = self.adapter.fetch(f"{SOURCE_URL}?case={type(error).__name__}", budget=1)
                self.assert_failure(result, FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_JSON")
        self.assertEqual(self.post.call_count, 2)

    def test_business_1001_is_a_safe_unavailable_failure(self):
        self.response.json.return_value = {"code": 1001, "msg": "upstream-secret", "data": {}}

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.UNAVAILABLE, "TIANTIAN_SERVICE_ERROR")
        self.post.assert_called_once()

    def test_business_1_is_no_media_for_this_work_not_an_outage(self):
        self.response.json.return_value = {"code": 1, "msg": "upstream-secret", "data": {}}

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.INVALID_RESULT, "TIANTIAN_NO_MEDIA")

    def test_success_without_media_returns_invalid_result_instead_of_raising(self):
        self.response.json.return_value = {"code": 200, "data": {}}

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.INVALID_RESULT)
        self.post.assert_called_once()

    def test_non_object_json_returns_invalid_result_not_unavailable(self):
        self.response.json.return_value = []

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.INVALID_RESULT)
        self.post.assert_called_once()

    @override_settings(TIANTIANJIEXI_RETRIES=2)
    def test_retry_failure_does_not_return_the_previous_observed_response(self):
        self.response.json.return_value = {"code": 1001, "msg": "upstream-secret", "data": {}}
        self.post.side_effect = [self.response, requests.Timeout("upstream-secret")]

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.TIMEOUT, "TIANTIAN_TIMEOUT")
        self.assertEqual(self.post.call_count, 2)

    def test_parse_with_exhausted_or_nonfinite_budget_does_not_fetch(self):
        with patch.object(self.adapter, "fetch", wraps=self.adapter.fetch) as fetch:
            for budget in (0, -1, float("nan"), float("inf"), -float("inf")):
                with self.subTest(budget=budget):
                    result = self.adapter.parse(SOURCE_URL, budget=budget)
                    self.assert_failure(result, FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
            fetch.assert_not_called()
        self.post.assert_not_called()

    def test_repeated_parse_does_not_request_again_to_reconstruct_raw(self):
        first = self.adapter.fetch(SOURCE_URL, budget=1)
        second = self.adapter.parse(SOURCE_URL, budget=1)
        third = TianTianAdapter(session=self.session).parse(SOURCE_URL, budget=1)

        self.assertEqual(first, self.raw)
        self.assert_failure(second, FailureKind.INVALID_RESULT, "TIANTIAN_NO_RAW")
        self.assert_failure(third, FailureKind.INVALID_RESULT, "TIANTIAN_NO_RAW")
        self.post.assert_called_once()
