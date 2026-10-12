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

    def response_fields(self, **overrides):
        fields = {
            "error_code": "UPSTREAM_MEDIA_INVALID",
            "failure_class": "NOT_APPLICABLE",
            "failure_stage": "HTTP_RESPONSE_REJECTED",
            "response_reject_reason": "FINAL_HOST_REJECTED",
            "http_status": 200,
            "content_type_class": "VIDEO",
            "final_media_host": "unknown",
        }
        fields.update(overrides)
        return fields

    def redirect_fields(self, **overrides):
        fields = {
            "initial_media_host": "v11-default.365yg.com",
            "initial_media_family": "DOUYIN_365YG",
            "observed_final_host": "unreviewed.example",
            "observed_final_host_class": "UNREVIEWED_PUBLIC",
            "redirected": True,
        }
        fields.update(overrides)
        return fields

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

    def test_existing_xhs_stream_hosts_accept_cancel_telemetry(self):
        for host in ("sns-video-hw.xhscdn.com", "sns-video-bd.xhscdn.com", "sns-video-v6.xhscdn.com"):
            with self.subTest(host=host):
                response = self.post(self.payload(
                    media_host=host, state="cancelled", error_code="DOWNLOAD_MANAGER_CANCELLED",
                    failure_class="NOT_APPLICABLE", failure_stage="DOWNLOAD_MANAGER_CANCELLED",
                ))
                self.assertEqual(response.status_code, 204)
        for host in ("sns-video-zl.xhscdn.com", "sns-video-v0.xhscdn.com", "sns-video-hw.xhscdn.com.evil.test"):
            with self.subTest(host=host):
                self.assertEqual(self.post(self.payload(media_host=host)).status_code, 400)

    def test_existing_xhs_family_is_preserved_in_rejection_observation(self):
        for host, family in (("sns-video-hw.xhscdn.com", "XHS_TIANTIAN_HW_EXACT"),
                             ("sns-video-v6.xhscdn.com", "XHS_NATIVE_REVIEWED")):
            with self.subTest(host=host):
                payload = self.payload(media_host=host, **self.response_fields(), **self.redirect_fields(
                    initial_media_host=host, initial_media_family=family))
                self.assertEqual(self.post(payload).status_code, 204)
                payload["initial_media_family"] = "DOUYIN_365YG"
                self.assertEqual(self.post(payload).status_code, 400)

    def test_new_response_rejection_fields_are_accepted_and_sanitized(self):
        payload = self.payload(
            error_code="UPSTREAM_MEDIA_INVALID",
            failure_class="NOT_APPLICABLE",
            failure_stage="HTTP_RESPONSE_REJECTED",
            response_reject_reason="HTTP_STATUS_REJECTED",
            http_status=403,
            content_type_class="OTHER",
            final_media_host="v11-default.365yg.com",
        )
        with self.assertLogs("xhs_download.browser_streaming_telemetry", level="INFO") as logs:
            response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        serialized = "\n".join(logs.output)
        self.assertIn('"response_reject_reason":"HTTP_STATUS_REJECTED"', serialized)
        self.assertIn('"http_status":403', serialized)

    def test_source_bound_final_observation_is_logged_without_url_data(self):
        payload = self.payload(
            **self.response_fields(),
            **self.redirect_fields(),
        )
        with self.assertLogs("xhs_download.browser_streaming_telemetry", level="INFO") as logs:
            response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        serialized = "\n".join(logs.output)
        self.assertIn('"initial_media_family":"DOUYIN_365YG"', serialized)
        self.assertIn('"observed_final_host":"unreviewed.example"', serialized)
        self.assertIn('"observed_final_host_class":"UNREVIEWED_PUBLIC"', serialized)
        self.assertIn('"redirected":true', serialized)
        self.assertNotIn("/media/", serialized)
        self.assertNotIn("token", serialized.lower())

    def test_reviewed_bdcgslb_final_host_requires_a_365yg_initial_family(self):
        accepted = self.post(
            self.payload(
                **self.response_fields(final_media_host="random.edge.bdcgslb.com"),
                **self.redirect_fields(
                    observed_final_host="random.edge.bdcgslb.com",
                    observed_final_host_class="REVIEWED_FINAL_ONLY",
                ),
            )
        )
        self.assertEqual(accepted.status_code, 204)

        ixigua_mismatch = self.post(
            self.payload(
                media_host="v3-dy-a-x.ixigua.com",
                **self.response_fields(final_media_host="random.edge.bdcgslb.com"),
                **self.redirect_fields(
                    initial_media_host="v3-dy-a-x.ixigua.com",
                    initial_media_family="DOUYIN_IXIGUA_EXACT",
                    observed_final_host="random.edge.bdcgslb.com",
                    observed_final_host_class="REVIEWED_FINAL_ONLY",
                ),
            )
        )
        self.assertEqual(ixigua_mismatch.status_code, 400)

        ixigua_observation_only = self.post(
            self.payload(
                media_host="v3-dy-a-x.ixigua.com",
                **self.response_fields(final_media_host="unknown"),
                **self.redirect_fields(
                    initial_media_host="v3-dy-a-x.ixigua.com",
                    initial_media_family="DOUYIN_IXIGUA_EXACT",
                    observed_final_host="random.edge.bdcgslb.com",
                    observed_final_host_class="REVIEWED_FINAL_ONLY",
                ),
            )
        )
        self.assertEqual(ixigua_observation_only.status_code, 204)

        mismatched_observation = self.post(
            self.payload(
                **self.response_fields(final_media_host="random.edge.bdcgslb.com"),
                **self.redirect_fields(
                    observed_final_host="different.edge.bdcgslb.com",
                    observed_final_host_class="REVIEWED_FINAL_ONLY",
                ),
            )
        )
        self.assertEqual(mismatched_observation.status_code, 400)

        missing_redirect = self.post(
            self.payload(
                **self.response_fields(final_media_host="random.edge.bdcgslb.com"),
                **self.redirect_fields(
                    observed_final_host="random.edge.bdcgslb.com",
                    observed_final_host_class="REVIEWED_FINAL_ONLY",
                    redirected=False,
                ),
            )
        )
        self.assertEqual(missing_redirect.status_code, 400)

    def test_final_observation_group_is_all_or_nothing_and_source_consistent(self):
        complete = self.redirect_fields()
        for field in complete:
            with self.subTest(missing=field):
                cache.clear()
                partial = {key: value for key, value in complete.items() if key != field}
                response = self.post(
                    self.payload(**self.response_fields(), **partial)
                )
                self.assertEqual(response.status_code, 400)

        inconsistent = (
            {"initial_media_host": "v3-web.douyinvod.com"},
            {"initial_media_family": "DOUYIN_DOUYINVOD"},
            {"observed_final_host_class": "INITIAL_SOURCE"},
            {"redirected": "true"},
        )
        for overrides in inconsistent:
            with self.subTest(overrides=overrides):
                cache.clear()
                response = self.post(
                    self.payload(
                        **self.response_fields(),
                        **self.redirect_fields(**overrides),
                    )
                )
                self.assertEqual(response.status_code, 400)

    def test_observed_final_host_rejects_urls_tokens_ip_loopback_and_trailing_dot(self):
        unsafe_values = (
            "https://unreviewed.example/media.mp4?token=secret",
            "unreviewed.example/path",
            "unreviewed.example?token=secret",
            "user@unreviewed.example",
            "127.0.0.1",
            "localhost",
            "unreviewed.example.",
        )
        for observed_host in unsafe_values:
            with self.subTest(observed_host=observed_host):
                cache.clear()
                response = self.post(
                    self.payload(
                        **self.response_fields(),
                        **self.redirect_fields(observed_final_host=observed_host),
                    )
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotContains(response, observed_host, status_code=400)

    def test_invalid_response_rejection_fields_are_rejected(self):
        cases = (
            {"response_reject_reason": "RAW_HTTP_ERROR"},
            {"http_status": 99},
            {"http_status": 600},
            {"http_status": "403"},
            {"content_type_class": "TEXT_HTML"},
            {"final_media_host": "attacker.example"},
        )
        base = {
            "response_reject_reason": "HTTP_STATUS_REJECTED",
            "http_status": 403,
            "content_type_class": "OTHER",
            "final_media_host": "unknown",
        }
        for invalid in cases:
            with self.subTest(invalid=invalid):
                cache.clear()
                response = self.post(self.payload(**{**base, **invalid}))
                self.assertEqual(response.status_code, 400)

    def test_response_rejection_fields_must_be_sent_as_one_group(self):
        for field, value in (
            ("response_reject_reason", "HTTP_STATUS_REJECTED"),
            ("http_status", 403),
            ("content_type_class", "OTHER"),
            ("final_media_host", "unknown"),
        ):
            with self.subTest(field=field):
                cache.clear()
                response = self.post(self.payload(**{field: value}))
                self.assertEqual(response.status_code, 400)

    def test_response_rejection_fields_require_matching_terminal_contract(self):
        response_fields = {
            "response_reject_reason": "HTTP_STATUS_REJECTED",
            "http_status": 403,
            "content_type_class": "VIDEO",
            "final_media_host": "v11-default.365yg.com",
        }
        cases = (
            {"state": "cancelled"},
            {"error_code": "UPSTREAM_FETCH_FAILED"},
            {"failure_stage": "STREAM_BODY_FAILURE"},
        )

        for mismatch in cases:
            with self.subTest(mismatch=mismatch):
                cache.clear()
                response = self.post(
                    self.payload(**response_fields, **mismatch)
                )
                self.assertEqual(response.status_code, 400)

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

        direct_final_only = self.post(
            self.payload(media_host="random.edge.bdcgslb.com")
        )
        self.assertEqual(direct_final_only.status_code, 400)

    def test_reviewed_ixigua_media_hostname_is_accepted_without_url_fields(self):
        with self.assertLogs(
            "xhs_download.browser_streaming_telemetry",
            level="INFO",
        ) as logs:
            response = self.post(
                self.payload(media_host="v3-dy-a-x.ixigua.com")
            )

        self.assertEqual(response.status_code, 204)
        serialized = "\n".join(logs.output)
        self.assertIn('"media_host":"v3-dy-a-x.ixigua.com"', serialized)
        self.assertNotIn("/media/", serialized)

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
        with self.assertNumQueries(0):
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
