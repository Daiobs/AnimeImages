import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse


class BrowserStreamingTelemetryEndpointTests(TestCase):
    maxDiff = None

    def setUp(self):
        cache.clear()
        self.url = reverse("media_browser_download_telemetry")

    def payload(self, **overrides):
        payload = {
            "schema_version": 1,
            "telemetry_id": "11111111111111111111111111111111",
            "download_id": "22222222222222222222222222222222",
            "provider": "tiantianjiexi",
            "media_host": "v11-default.365yg.com",
            "transport": "browser_streaming",
            "state": "failed",
            "error_code": "UPSTREAM_FETCH_FAILED",
            "failure_class": "FETCH_REJECTED_TYPE_ERROR",
            "failure_stage": "FETCH_REJECTED_BEFORE_RESPONSE",
            "streaming_started": True,
            "expected_bytes": 110691390,
            "received_bytes": 0,
            "total_bytes": 110691390,
            "registration_to_fetch_ms": 14,
            "fetch_reject_ms": 901,
            "time_to_first_chunk_ms": 0,
            "stream_duration_ms": 901,
            "navigator_online": True,
            "page_visibility": "visible",
            "worker_controller_state": "activated",
            "worker_active_state": "activated",
            "worker_waiting": False,
            "worker_installing": False,
            "protocol_version": 1,
            "browser_family": "chromium",
        }
        payload.update(overrides)
        return payload

    def post(self, payload, **extra):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def test_valid_terminal_failure_is_logged_as_client_observed_metadata(self):
        with self.assertLogs("xhs_download.browser_streaming_telemetry", level="INFO") as logs:
            response = self.post(self.payload())

        self.assertEqual(response.status_code, 204)
        serialized = "\n".join(logs.output)
        self.assertIn("browser_streaming_terminal", serialized)
        self.assertIn('"client_observed":true', serialized)
        self.assertIn('"media_host":"v11-default.365yg.com"', serialized)
        self.assertNotIn("path/video.mp4", serialized)
        self.assertNotIn("super-secret", serialized)

    def test_unknown_fields_are_rejected_without_logging_their_values(self):
        payload = self.payload(
            url="https://v11-default.365yg.com/path/video.mp4?token=super-secret"
        )
        with self.assertNoLogs("xhs_download.browser_streaming_telemetry", level="INFO"):
            response = self.post(payload)
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, "super-secret", status_code=400)

    def test_query_and_token_like_fields_are_rejected(self):
        for field in ("query", "token", "signed_url"):
            cache.clear()
            response = self.post(self.payload(**{field: "super-secret"}))
            self.assertEqual(response.status_code, 400)
            self.assertNotContains(response, "super-secret", status_code=400)

    @override_settings(BROWSER_DOWNLOAD_TELEMETRY_MAX_PAYLOAD_BYTES=1024)
    def test_oversized_payload_is_rejected(self):
        response = self.client.post(
            self.url,
            data=b"{" + (b"x" * 2048) + b"}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error_code"], "TELEMETRY_PAYLOAD_TOO_LARGE")

    def test_invalid_download_id_is_rejected(self):
        response = self.post(self.payload(download_id="not-an-opaque-id"))
        self.assertEqual(response.status_code, 400)
    def test_invalid_media_hostname_is_rejected(self):
        response = self.post(self.payload(media_host="attacker.example"))
        self.assertEqual(response.status_code, 400)

    def test_unknown_failure_class_is_rejected(self):
        response = self.post(self.payload(failure_class="CORS_FAILURE"))
        self.assertEqual(response.status_code, 400)

    def test_non_json_and_get_requests_are_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = self.client.post(self.url, data="not-json", content_type="text/plain")
        self.assertEqual(response.status_code, 415)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        denied = client.post(
            self.url,
            data=json.dumps(self.payload()),
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 403)

        client.get(reverse("home"))
        token = client.cookies["csrftoken"].value
        allowed = client.post(
            self.url,
            data=json.dumps(self.payload()),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(allowed.status_code, 204)

    @override_settings(BROWSER_DOWNLOAD_TELEMETRY_RATE_LIMIT_PER_MINUTE=1)
    def test_rate_limit_is_independent_and_enforced(self):
        self.assertEqual(self.post(self.payload()).status_code, 204)
        second = self.post(self.payload(telemetry_id="33333333333333333333333333333333"))
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error_code"], "TELEMETRY_RATE_LIMITED")
        self.assertGreaterEqual(int(second["Retry-After"]), 1)

    @patch("xhs_download.views.requests.get")
    @patch("xhs_download.views.parse_media_page")
    def test_endpoint_never_calls_provider_or_fetches_media(self, parse_media_page, requests_get):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 204)
        parse_media_page.assert_not_called()
        requests_get.assert_not_called()

    def test_completion_events_are_not_admitted_in_failure_v1(self):
        response = self.post(
            self.payload(
                state="completed",
                error_code="",
                failure_class="NOT_APPLICABLE",
                failure_stage="PRE_START_FAILURE",
            )
        )
        self.assertEqual(response.status_code, 400)
