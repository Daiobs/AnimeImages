from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from contracts.network import offline_requests
from contracts.test_backend_contracts import OfflineTransport, SOURCE, MEDIA, ADMISSION, SERVICE
from contracts.test_inv01_douyin import (
    AWEME, NATIVE_AWEME, NATIVE_MEDIA, ULTRA_PAYLOAD, ULTRA_MEDIA,
    SOURCE as DOUYIN_SOURCE,
)
from .models import ProviderAccessMode, ProviderHealthTestSetting, ProviderRuntimeSetting
from .services.platforms.bilibili import clear_anonymous_context_cache
from .services.provider_health_tests import (
    ProviderHealthExecutionContext, execute_provider_health_test, provider_health_test_by_key,
)
from .tests_xhs_media_fidelity import (
    NOTE_URL, IMAGE_ONE, IMAGE_TWO, COVER_URL, VIDEO_URL,
    hydration_html, note_payload, image_note, video_note,
)


class CountedBody(BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.bytes_read = 0

    def read(self, size=-1, **kwargs):
        content = super().read(size)
        self.bytes_read += len(content)
        return content


class SmokeTransport(OfflineTransport):
    def __init__(self):
        super().__init__()
        self.native_outcome = "success"
        self.note = image_note()
        self.notes_by_url = {}
        self.media_requests = []
        self.bodies = []
        self.parse_requests = []
        self.media_status = 206
        self.service_size_missing = False
        self.ultra_payload = json.loads(json.dumps(ULTRA_PAYLOAD))

    def answer(self, request, *, body=b"", status=200, content_type="text/html"):
        response = requests.Response()
        response.status_code = status
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = content_type
        response.raw = CountedBody(body)
        return response

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        if request.url in {MEDIA, ULTRA_MEDIA, NATIVE_MEDIA, IMAGE_ONE, IMAGE_TWO, COVER_URL, VIDEO_URL}:
            self.media_requests.append(request)
            kind = "image/jpeg" if request.url in {IMAGE_ONE, IMAGE_TWO, COVER_URL} else "video/mp4"
            response = self.answer(request, body=b"\x00\x00\x00\x18ftypisom" + b"x" * 8192,
                                   status=self.media_status, content_type=kind)
            response.headers["Content-Range"] = "bytes 0-1023/8204"
            response.headers["Content-Length"] = "1024"
            self.bodies.append(response.raw)
            return response
        self.parse_requests.append(request)
        if parsed.hostname == "www.xiaohongshu.com":
            note = self.notes_by_url.get(request.url, self.note)
            return self.answer(request, body=hydration_html(note_payload(note, note_id=note["noteId"])).encode())
        if parsed.hostname == "aweme.snssdk.com":
            return self.answer(request, body=json.dumps({"aweme_list": [NATIVE_AWEME]}).encode(),
                               content_type="application/json")
        if request.url == SERVICE and AWEME in str(request.body):
            return self.answer(request, body=json.dumps(self.ultra_payload).encode(), content_type="application/json")
        response = super().send(request, **kwargs)
        if request.url == SERVICE and self.service_size_missing:
            payload = response.json()
            payload["data"]["videos"][0]["streams"][0].pop("size", None)
            response._content = json.dumps(payload).encode()
        return response


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "smoke-execution"}},
    ALLOWED_HOSTS=["testserver", "localhost"],
    ULTRA_HD_PROVIDER_ENABLED=True, ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
    KUKUTOOL_PROVIDER_ENABLED=False, DOUYIN_ULTRA_PROVIDER_ORDER="tiantianjiexi,native",
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="smoke-offline", TIANTIANJIEXI_PASSWORD="smoke-offline",
    TIANTIANJIEXI_API_BASE_URL=SERVICE, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
)
class DownloadSmokeExecutionTests(TransactionTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        clear_anonymous_context_cache()
        self.addCleanup(cache.clear)
        self.addCleanup(clear_anonymous_context_cache)
        self.runtime = ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms=ADMISSION,
            douyin_ultra_mode=ProviderAccessMode.PUBLIC, douyin_ultra_rollout_percent=100,
        )
        self.setting = ProviderHealthTestSetting.objects.create(
            pk=1, xhs_gallery_url=NOTE_URL, xhs_video_url=NOTE_URL,
            bilibili_url=SOURCE, douyin_url=DOUYIN_SOURCE,
        )
        self.transport = SmokeTransport()
        self.stack.enter_context(offline_requests(self.transport.send))
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("xhs_download.services.platforms.tiantian_serial.lock_root", return_value=root))

    def execute(self, key):
        context = ProviderHealthExecutionContext(
            request_id="offline-download-smoke", is_staff=True, trigger="daily",
            test_setting=self.setting, runtime_setting=self.runtime,
        )
        return execute_provider_health_test(context, provider_health_test_by_key(key))

    def assert_prefix(self, run, url):
        self.assertEqual(run.status, "success", run.download_smoke_details)
        details = run.download_smoke_details
        self.assertEqual(details["access_scope"], "anonymous_policy")
        self.assertEqual(details["download"]["request_count"], 1)
        self.assertEqual(details["download"]["bytes_received"], 1024)
        self.assertEqual(len(self.transport.media_requests), 1)
        self.assertEqual(self.transport.media_requests[0].url, url)
        self.assertEqual(self.transport.media_requests[0].headers["Range"], "bytes=0-1023")
        self.assertEqual(sum(body.bytes_read for body in self.transport.bodies), 1024)
        self.assertTrue(all(body.closed for body in self.transport.bodies))
        self.assertNotIn(url, json.dumps(details))
        self.assertEqual(set(details["fields"]), {"title", "cover", "quality", "size"})

    def test_xhs_gallery_parses_real_page_and_fetches_only_first_image(self):
        run = self.execute("download_smoke_xhs_gallery")
        self.assert_prefix(run, IMAGE_ONE)
        self.assertTrue(run.download_smoke_details["fields"]["title"])
        self.assertTrue(run.download_smoke_details["fields"]["cover"])
        self.assertFalse(run.download_smoke_details["fields"]["size"])

    def test_xhs_video_does_not_run_display_size_probe(self):
        self.transport.note = video_note(imageList=[{"urlDefault": COVER_URL}])
        run = self.execute("download_smoke_xhs_video")
        self.assert_prefix(run, VIDEO_URL)

    def test_bilibili_native_headers_and_fields(self):
        run = self.execute("download_smoke_bilibili")
        self.assert_prefix(run, MEDIA)
        self.assertEqual(run.provider, "native")
        self.assertTrue(all(run.download_smoke_details["fields"].values()))
        self.assertIn("bilibili.com", self.transport.media_requests[0].headers["Referer"])
        self.assertNotIn("Cookie", self.transport.media_requests[0].headers)

    def test_bilibili_uses_existing_admitted_fallback_and_selection_manifest(self):
        self.transport.native_outcome = 412
        run = self.execute("download_smoke_bilibili")
        self.assert_prefix(run, MEDIA)
        self.assertEqual(run.provider, "tiantianjiexi_generic")
        self.assertEqual(self.transport.events[-1], "tiantian")
        self.assertTrue(all(event == "native" for event in self.transport.events[:-1]))

    def test_bilibili_missing_size_stays_missing_without_extra_head(self):
        self.transport.native_outcome = 412
        self.transport.service_size_missing = True
        run = self.execute("download_smoke_bilibili")
        self.assert_prefix(run, MEDIA)
        self.assertFalse(run.download_smoke_details["fields"]["size"])

    def test_douyin_uses_current_ultra_default_and_exact_selected_resource(self):
        self.transport.ultra_payload["data"]["videos"][0]["streams"].insert(0, {
            "url": ULTRA_MEDIA.replace("ultra", "lower"), "qualityLabel": "720P", "sizeInBytes": 10240,
        })
        run = self.execute("download_smoke_douyin")
        self.assert_prefix(run, ULTRA_MEDIA)
        self.assertEqual(run.provider, "tiantianjiexi")
        self.assertTrue(run.download_smoke_details["fields"]["quality"])
        self.assertTrue(run.download_smoke_details["fields"]["size"])

    def test_daily_audit_staff_flag_does_not_grant_fallback(self):
        self.runtime.third_party_allowed_public_platforms = ""
        self.runtime.save()
        self.transport.native_outcome = 412
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertNotIn("tiantian", self.transport.events)
        self.assertEqual(self.transport.media_requests, [])
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 0)
        self.assertEqual(run.error_code, "PARSE_HTTP_412")

    def test_disabled_visitor_download_preserves_fields_and_makes_no_media_request(self):
        from download_console.models import DownloadRuntimeSetting
        runtime = DownloadRuntimeSetting.load()
        runtime.visitor_video_download_enabled = False
        runtime.save()
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        self.assertEqual(run.download_smoke_details["stage"], "admission")
        self.assertTrue(all(run.download_smoke_details["fields"].values()))
        self.assertEqual(self.transport.media_requests, [])

    def test_exhausted_download_quota_blocks_before_media_fetch(self):
        from download_console.models import DownloadRuntimeSetting
        from .services.rate_limits import consume_daily
        runtime = DownloadRuntimeSetting.load()
        runtime.global_daily_request_limit = 1
        runtime.save()
        consume_daily("download-requests-global", "all", 1)
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "DOWNLOAD_DAILY_REQUEST_LIMIT_GLOBAL")
        self.assertEqual(run.download_smoke_details["stage"], "admission")
        self.assertEqual(self.transport.media_requests, [])

    def test_exhausted_download_bytes_blocks_before_media_fetch(self):
        from download_console.models import DownloadRuntimeSetting
        from .services.rate_limits import consume_daily
        runtime = DownloadRuntimeSetting.load()
        runtime.global_daily_bytes_limit = 1024
        runtime.save()
        consume_daily("download-bytes-global", "all", 1024, amount=1024)
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL")
        self.assertEqual(self.transport.media_requests, [])

    def test_download_failure_keeps_parsed_coverage_and_never_reparses(self):
        self.transport.media_status = 403
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertTrue(all(run.download_smoke_details["fields"].values()))
        self.assertEqual(run.download_smoke_details["download"]["http_status"], 403)
        self.assertEqual(len(self.transport.media_requests), 1)
        views = [r for r in self.transport.parse_requests if urlsplit(r.url).path == "/x/web-interface/view"]
        self.assertEqual(len(views), 1)

    def test_byte_accounting_failure_is_not_reported_as_success_or_retried(self):
        with patch("xhs_download.views.record_download_archive_bytes", side_effect=OSError("offline cache failure")):
            run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "QUOTA_ACCOUNTING_FAILED")
        self.assertEqual(run.download_smoke_details["download"]["bytes_received"], 1024)
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 1)
        self.assertEqual(len(self.transport.media_requests), 1)
        self.assertTrue(all(body.closed for body in self.transport.bodies))

    def test_missing_sample_does_not_parse_or_fetch(self):
        self.setting.bilibili_url = ""
        run = self.execute("download_smoke_bilibili")
        self.assertEqual(run.status, "skipped")
        self.assertEqual(self.transport.parse_requests + self.transport.media_requests, [])

    def test_real_daily_runner_persists_all_four_optional_checks_without_email(self):
        from download_console.models import DownloadRuntimeSetting
        from .services.provider_daily_validation import run_daily_provider_validation, build_daily_validation_report
        from .services.rate_limits import get_daily_total
        download_setting = DownloadRuntimeSetting.load()
        download_setting.global_daily_bytes_limit = 8192
        download_setting.global_daily_request_limit = 10
        download_setting.save()
        video_id = "65b2c3d4e5f6789012abcdef"
        video_source = f"https://www.xiaohongshu.com/explore/{video_id}"
        self.transport.notes_by_url[video_source] = video_note(noteId=video_id, imageList=[{"urlDefault": COVER_URL}])
        self.setting.xhs_video_url = video_source
        self.setting.daily_enabled = True
        self.setting.daily_test_keys = [
            "download_smoke_xhs_gallery", "download_smoke_xhs_video",
            "download_smoke_douyin", "download_smoke_bilibili",
        ]
        self.setting.save()
        outcome = run_daily_provider_validation(no_email=True)
        self.assertEqual(outcome.suite.status, "success")
        self.assertEqual(outcome.suite.success_count, 4)
        self.assertEqual(len(self.transport.media_requests), 4)
        self.assertEqual([r.url for r in self.transport.media_requests], [IMAGE_ONE, VIDEO_URL, ULTRA_MEDIA, MEDIA])
        self.assertEqual(sum(body.bytes_read for body in self.transport.bodies), 4096)
        self.assertEqual(get_daily_total("download-bytes-global", "all"), 4096)
        self.assertEqual(get_daily_total("download-requests-global", "all"), 4)
        self.assertEqual(outcome.suite.test_runs.count(), 4)
        report = build_daily_validation_report(self.setting)
        self.assertEqual(report["active_failing_count"], 0)
        self.assertEqual(report["selected_test_count"], 4)
        for run in outcome.suite.test_runs.all():
            self.assertTrue(run.download_smoke_details["fields"]["title"])
            self.assertTrue(run.download_smoke_report_lines)
        self.setting.refresh_from_db()
        self.assertEqual(len(self.setting.daily_test_keys), 4)
