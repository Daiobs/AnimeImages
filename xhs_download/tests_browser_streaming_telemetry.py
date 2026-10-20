import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .services import browser_streaming_telemetry as telemetry


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
        distribution = telemetry.get_protocol_version_distribution()
        self.assertEqual(distribution["total"], 1)
        self.assertEqual(distribution["sample_days"], 1)
        self.assertEqual(distribution["days"][0]["versions"], [{"version": 1, "count": 1}])

    def test_versions_are_counted_for_failures_and_cancellations_only(self):
        self.assertEqual(self.post(self.payload(protocol_version=100)).status_code, 204)
        self.assertEqual(self.post(self.payload(
            protocol_version=3,
            state="cancelled",
            error_code="USER_CANCELLED",
            failure_class="NOT_APPLICABLE",
            failure_stage="USER_CANCELLED",
        )).status_code, 204)
        self.assertEqual(
            telemetry.get_protocol_version_distribution()["days"][0]["versions"],
            [{"version": 3, "count": 1}, {"version": 100, "count": 1}],
        )

    def test_invalid_protocol_versions_do_not_create_counters(self):
        for version in (0, -1, 101, True, "3", 1.5, None):
            with self.subTest(version=version):
                self.assertEqual(self.post(self.payload(protocol_version=version)).status_code, 400)
        missing = self.payload()
        del missing["protocol_version"]
        self.assertEqual(self.post(missing).status_code, 400)
        self.assertEqual(telemetry.get_protocol_version_distribution()["total"], 0)

    def test_counter_failure_preserves_terminal_log_and_204_response(self):
        with patch.object(telemetry, "cache") as failed_cache:
            failed_cache.add.side_effect = RuntimeError("cache unavailable")
            with self.assertLogs(telemetry.logger, level="INFO") as logs:
                response = self.post(self.payload())
        self.assertEqual(response.status_code, 204)
        self.assertIn("browser_streaming_terminal", "\n".join(logs.output))
        self.assertIn("browser_streaming_protocol_count_unavailable", "\n".join(logs.output))

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
        self.assertEqual(telemetry.get_protocol_version_distribution()["total"], 1)

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
        self.assertEqual(telemetry.get_protocol_version_distribution()["total"], 0)


@override_settings(CACHES={
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "browser-streaming-protocol-distribution-tests",
    },
})
class BrowserStreamingProtocolDistributionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = datetime(2026, 10, 20, 0, 30, tzinfo=timezone.utc)

    def record_at(self, now, version):
        with patch.object(telemetry.timezone, "now", return_value=now):
            telemetry.record_protocol_version(version)

    @override_settings(TIME_ZONE="America/Los_Angeles")
    def test_utc_days_and_versions_are_separate(self):
        self.record_at(self.now - timedelta(hours=1), 1)
        self.record_at(self.now, 3)
        self.record_at(self.now, 3)
        distribution = telemetry.get_protocol_version_distribution(now=self.now)
        self.assertEqual(distribution["sample_days"], 2)
        self.assertEqual(distribution["total"], 3)
        self.assertEqual(distribution["days"][:2], [
            {"date": "2026-10-20", "total": 2, "versions": [{"version": 3, "count": 2}]},
            {"date": "2026-10-19", "total": 1, "versions": [{"version": 1, "count": 1}]},
        ])

    def test_exactly_fourteen_days_excludes_older_and_future_samples(self):
        for offset in (-1, 0, 13, 14):
            self.record_at(self.now - timedelta(days=offset), 1)
        distribution = telemetry.get_protocol_version_distribution(now=self.now)
        self.assertEqual(len(distribution["days"]), 14)
        self.assertEqual(distribution["days"][0]["date"], "2026-10-20")
        self.assertEqual(distribution["days"][-1]["date"], "2026-10-07")
        self.assertEqual(distribution["total"], 2)
        self.assertEqual(distribution["sample_days"], 2)

    def test_counter_expires_at_utc_midnight_after_fourteen_days(self):
        expiry = datetime(2026, 11, 3, tzinfo=timezone.utc)
        key = telemetry._protocol_version_key(self.now.date(), 3)
        with patch("time.time", return_value=self.now.timestamp()):
            self.record_at(self.now, 3)
        with patch("time.time", return_value=expiry.timestamp() - 1):
            self.assertEqual(cache.get(key), 1)
        with patch("time.time", return_value=expiry.timestamp()):
            self.assertIsNone(cache.get(key))

    def test_increments_do_not_extend_daily_expiration(self):
        with patch.object(telemetry, "cache") as mocked_cache:
            mocked_cache.add.return_value = False
            self.record_at(self.now, 3)
            mocked_cache.add.assert_called_once_with(
                telemetry._protocol_version_key(self.now.date(), 3),
                1,
                timeout=14 * 86400 - 1800,
            )
            mocked_cache.incr.assert_called_once()
            mocked_cache.set.assert_not_called()
            mocked_cache.touch.assert_not_called()

    def test_eviction_between_add_and_incr_is_retried_once(self):
        with patch.object(telemetry, "cache") as mocked_cache:
            mocked_cache.add.side_effect = [False, True]
            mocked_cache.incr.side_effect = ValueError("key disappeared")
            self.record_at(self.now, 3)
        self.assertEqual(mocked_cache.add.call_count, 2)
        self.assertEqual(mocked_cache.incr.call_count, 1)

    def test_parallel_events_increment_without_read_modify_write_loss(self):
        with patch.object(telemetry.timezone, "now", return_value=self.now):
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(telemetry.record_protocol_version, [3] * 64))
        self.assertEqual(telemetry.get_protocol_version_distribution(now=self.now)["total"], 64)

    def test_version_keys_and_read_window_are_bounded(self):
        with patch.object(telemetry, "cache") as mocked_cache:
            for version in (0, 101, True, "1", None):
                telemetry.record_protocol_version(version)
            mocked_cache.add.assert_not_called()
            mocked_cache.get_many.return_value = {}
            distribution = telemetry.get_protocol_version_distribution(now=self.now)
            keys = mocked_cache.get_many.call_args.args[0]
        self.assertEqual(len(keys), 1400)
        self.assertEqual(len(set(keys)), 1400)
        self.assertEqual(distribution["sample_days"], 0)
        self.assertEqual(distribution["total"], 0)
        self.assertTrue(all(not row["versions"] for row in distribution["days"]))

    def test_cache_failure_is_not_reported_as_a_zero_sample_window(self):
        with patch.object(telemetry, "cache") as failed_cache:
            failed_cache.get_many.side_effect = RuntimeError("cache unavailable")
            with self.assertLogs(telemetry.logger, level="WARNING"):
                distribution = telemetry.get_protocol_version_distribution(now=self.now)
        self.assertFalse(distribution["available"])
        self.assertEqual(distribution["days"], [])


class BrowserStreamingProtocolDashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse("ops_dashboard")
        self.user = get_user_model().objects.create_superuser(username="telemetry-ops")
        self.client.force_login(self.user)

    def test_existing_dashboard_renders_received_protocol_distribution_and_scope(self):
        telemetry.record_protocol_version(1)
        telemetry.record_protocol_version(3)
        telemetry.record_protocol_version(3)
        response = self.client.get(self.url)
        self.assertContains(response, "今日解析次数")
        self.assertContains(response, "最近 14 个 UTC 日")
        self.assertContains(response, "有样本日期：1/14")
        self.assertContains(response, "v1：1")
        self.assertContains(response, "v3：2")
        self.assertContains(response, "无样本", count=14)
        self.assertContains(response, "不等同于活跃 Worker 版本")
        self.assertContains(response, "不回填历史日志")
        self.assertContains(response, "采样量（事件）")
        self.assertContains(response, "14 天数据有缺口，不等于旧版本为零")
        self.assertContains(response, "不用于授权")
        self.assertEqual(len(response.context["protocol_version_distribution"]["days"]), 14)

    def test_empty_dashboard_does_not_claim_fourteen_days_of_samples(self):
        response = self.client.get(self.url)
        self.assertContains(response, "有样本日期：0/14")
        self.assertContains(response, "无样本", count=15)

    def test_dashboard_cache_failure_preserves_other_metrics(self):
        with patch.object(telemetry, "cache") as failed_cache:
            failed_cache.get_many.side_effect = RuntimeError("cache unavailable")
            with self.assertLogs(telemetry.logger, level="WARNING"):
                response = self.client.get(self.url)
        self.assertContains(response, "协议版本遥测缓存暂不可用")
        self.assertContains(response, "今日解析次数")
        self.assertNotContains(response, "有样本日期：0/14")
