from contextlib import ExitStack
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from contracts.network import offline_requests
from contracts.test_backend_contracts import OfflineTransport, SOURCE, MEDIA, ADMISSION, SERVICE
from contracts.test_inv01_douyin import (
    AWEME, NATIVE_AWEME, NATIVE_MEDIA, ULTRA_PAYLOAD, ULTRA_MEDIA,
    ULTRA_TITLE, NATIVE_TITLE,
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


DOUYIN_IMAGE_ONE = "https://p3-sign.douyinpic.com/smoke-first.jpg"
DOUYIN_IMAGE_TWO = "https://p3-sign.douyinpic.com/smoke-second.jpg"
XHS_SERVICE_AUDIO = "https://sns-video-bd.xhscdn.com/smoke-audio.mp3"
XHS_SERVICE_HLS = "https://sns-video-bd.xhscdn.com/smoke-stream.m3u8"
DOUYIN_GALLERY = {
    "aweme_id": AWEME, "desc": "Smoke gallery work",
    "author": {"nickname": "Contract"},
    "images": [{"url_list": [DOUYIN_IMAGE_ONE]}, {"url_list": [DOUYIN_IMAGE_TWO]}],
}


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
        self.douyin_aweme = json.loads(json.dumps(NATIVE_AWEME))
        self.xhs_service_payload = None
        self.ultra_delay = 0
        self.ultra_status = 200
        self.ultra_finished = threading.Event()
        self.ultra_finished.set()

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
        image_urls = {IMAGE_ONE, IMAGE_TWO, COVER_URL, DOUYIN_IMAGE_ONE, DOUYIN_IMAGE_TWO}
        if request.url in {MEDIA, ULTRA_MEDIA, NATIVE_MEDIA, VIDEO_URL,
                           XHS_SERVICE_AUDIO, XHS_SERVICE_HLS} | image_urls:
            self.media_requests.append(request)
            kind = "image/jpeg" if request.url in image_urls else "video/mp4"
            if request.url == XHS_SERVICE_AUDIO:
                kind = "audio/mpeg"
            elif request.url == XHS_SERVICE_HLS:
                kind = "application/vnd.apple.mpegurl"
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
            return self.answer(request, body=json.dumps({"aweme_list": [self.douyin_aweme]}).encode(),
                               content_type="application/json")
        if parsed.hostname in {"www.iesdouyin.com", "www.douyin.com"}:
            return self.answer(request, body=b"<html><body>No media</body></html>")
        if request.url == SERVICE and AWEME in str(request.body):
            self.ultra_finished.clear()
            try:
                time.sleep(self.ultra_delay)
                return self.answer(request, body=json.dumps(self.ultra_payload).encode(),
                                   status=self.ultra_status, content_type="application/json")
            finally:
                self.ultra_finished.set()
        if request.url == SERVICE and self.xhs_service_payload is not None:
            return self.answer(request, body=json.dumps(self.xhs_service_payload).encode(),
                               content_type="application/json")
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

    @override_settings(TIANTIANJIEXI_PROVIDER_ENABLED=False)
    def test_douyin_gallery_is_a_user_result_and_downloads_only_first_image(self):
        self.transport.douyin_aweme = DOUYIN_GALLERY
        run = self.execute("download_smoke_douyin")
        self.assert_prefix(run, DOUYIN_IMAGE_ONE)
        self.assertEqual(run.provider, "mobile_feed")
        self.assertEqual(run.download_smoke_details.get("media_type"), "image")
        self.assertEqual(run.download_smoke_details["fields"], {
            "title": True, "cover": True, "quality": False, "size": False,
        })
        self.assertEqual(len(self.transport.parse_requests), 1)

    def test_sample_name_does_not_override_actual_image_assets(self):
        run = self.execute("download_smoke_xhs_video")
        self.assert_prefix(run, IMAGE_ONE)
        self.assertEqual(run.download_smoke_details["media_type"], "image")

    def test_candidate_only_service_images_do_not_gain_download_authority(self):
        self.runtime.third_party_allowed_public_platforms = "upstream:tiantianjiexi_generic"
        self.runtime.save()
        self.transport.note = image_note(imageList=[])
        self.transport.xhs_service_payload = {"code": 200, "data": {
            "title": "TianTian image work", "images": [IMAGE_ONE, IMAGE_TWO],
        }}
        response = self.client.get("/results/", {"q": NOTE_URL},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        html = response.json()["html"]
        self.assertNotIn('id="download-btn"', html)
        self.assertNotIn('data-type="image"', html)
        run = self.execute("download_smoke_xhs_video")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "SMOKE_RESOURCE_MISSING")
        self.assertEqual(run.download_smoke_details["provider"], "")
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 0)
        self.assertEqual(self.transport.media_requests, [])
        self.assertEqual(sum(request.url == SERVICE for request in self.transport.parse_requests), 1)

    def configure_xhs_service(self, payload, *, native_images=False):
        self.runtime.third_party_allowed_public_platforms = "upstream:tiantianjiexi_generic"
        self.runtime.save()
        self.transport.note = image_note() if native_images else image_note(imageList=[])
        self.transport.xhs_service_payload = {"code": 200, "data": payload}

    def public_xhs_selection(self):
        response = self.client.get("/results/", {"q": NOTE_URL},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content[:1000])
        soup = BeautifulSoup(response.json()["html"], "html.parser")
        picker = soup.select_one("[data-ytdlp-picker]")
        self.assertIsNotNone(picker)
        selected = picker.select_one("[data-picker-radio][checked]")
        self.assertIsNotNone(selected)
        return soup, picker, selected

    def assert_browser_only_xhs_selection_is_skipped(self, payload, media_type, url):
        self.configure_xhs_service(payload)
        _, picker, selected = self.public_xhs_selection()
        self.assertEqual(selected["data-kind"], media_type)
        self.assertEqual(selected["data-can-download"], "true")
        response = self.client.post(picker["data-selection-url"], {
            "candidate_id": selected["value"], "resource_token": selected["data-token"],
            "action": "download",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:1000])
        descriptor = response.json()["item"]
        self.assertEqual(descriptor["url"], url)
        self.assertEqual(descriptor["type"], media_type)
        self.assertEqual(descriptor["candidate_id"], selected["value"])
        self.assertEqual(descriptor["provider"], "tiantianjiexi_generic")
        self.assertTrue(descriptor["asset_id"].startswith("upstream:selection:"))
        if media_type == 'video':
            self.assertEqual([request.method for request in self.transport.media_requests], ['HEAD'])
            self.assertEqual(self.transport.media_requests[0].url, url)
        else:
            self.assertEqual(self.transport.media_requests, [])

        # Daily must obtain a fresh result rather than reuse the public parse cache.
        cache.clear()
        self.transport.parse_requests.clear()
        self.transport.media_requests.clear()
        self.transport.bodies.clear()
        run = self.execute("download_smoke_xhs_video")
        self.assertEqual(run.status, "skipped", run.download_smoke_details)
        self.assertEqual(run.error_code, "SMOKE_BROWSER_ONLY_RESOURCE")
        self.assertEqual(run.provider, "tiantianjiexi_generic")
        details = run.download_smoke_details
        self.assertEqual(details["access_scope"], "anonymous_policy")
        self.assertEqual(details["media_type"], media_type)
        self.assertEqual(details["stage"], "admission")
        self.assertEqual(details["download"]["status"], "not_run")
        self.assertEqual(details["download"]["error_code"], "SMOKE_BROWSER_ONLY_RESOURCE")
        self.assertEqual(details["download"]["request_count"], 0)
        self.assertEqual(details["download"]["bytes_received"], 0)
        self.assertEqual(self.transport.media_requests, [])
        self.assertEqual(self.transport.bodies, [])
        self.assertEqual(sum(request.url == SERVICE for request in self.transport.parse_requests), 1)
        self.assertNotIn(url, json.dumps(details))

    def test_browser_only_service_video_action_succeeds_but_daily_skips_without_fetch(self):
        self.assert_browser_only_xhs_selection_is_skipped({
            "title": "Service video work",
            "videos": [{"url": VIDEO_URL, "quality": "1080p"}],
        }, "video", VIDEO_URL)

    def test_audio_service_action_succeeds_and_daily_preserves_type_when_skipping(self):
        self.assert_browser_only_xhs_selection_is_skipped({
            "title": "Service audio work", "audio_url": XHS_SERVICE_AUDIO,
        }, "audio", XHS_SERVICE_AUDIO)

    def test_unavailable_hls_candidate_does_not_block_native_first_image(self):
        self.configure_xhs_service({
            "title": "Service stream candidate",
            "videos": [{"url": XHS_SERVICE_HLS, "quality": "1080p"}],
        }, native_images=True)
        soup, picker, selected = self.public_xhs_selection()
        self.assertEqual(selected["data-can-download"], "false")
        self.assertEqual(selected["data-can-legacy"], "false")
        images = soup.select('.asset-card[data-type="image"]')
        self.assertEqual([image["data-url"] for image in images], [IMAGE_ONE, IMAGE_TWO])
        response = self.client.post(picker["data-selection-url"], {
            "candidate_id": selected["value"], "resource_token": selected["data-token"],
            "action": "download",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 409, response.content[:1000])
        self.assertEqual(self.transport.media_requests, [])

        cache.clear()
        self.transport.parse_requests.clear()
        run = self.execute("download_smoke_xhs_gallery")
        self.assert_prefix(run, IMAGE_ONE)
        self.assertEqual(run.download_smoke_details["media_type"], "image")
        self.assertEqual(run.provider, "native")
        self.assertEqual(sum(request.url == SERVICE for request in self.transport.parse_requests), 1)

    def test_browser_only_service_video_still_obeys_visitor_download_denial(self):
        from download_console.models import DownloadRuntimeSetting
        self.configure_xhs_service({
            "title": "Service video work",
            "videos": [{"url": VIDEO_URL, "quality": "1080p"}],
        })
        runtime = DownloadRuntimeSetting.load()
        runtime.visitor_video_download_enabled = False
        runtime.save()
        _, picker, selected = self.public_xhs_selection()
        self.assertEqual(selected["data-can-download"], "true")
        response = self.client.post(picker["data-selection-url"], {
            "candidate_id": selected["value"], "resource_token": selected["data-token"],
            "action": "download",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 403, response.content[:1000])
        self.assertEqual(response.json()["error_code"], "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        run = self.execute("download_smoke_xhs_video")
        self.assertEqual(run.status, "failed", run.download_smoke_details)
        self.assertEqual(run.error_code, "VISITOR_VIDEO_DOWNLOAD_DISABLED")
        self.assertEqual(run.download_smoke_details["stage"], "admission")
        self.assertEqual(run.download_smoke_details["media_type"], "video")
        self.assertEqual(run.download_smoke_details["download"]["status"], "not_run")
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 0)
        self.assertEqual(run.download_smoke_details["download"]["bytes_received"], 0)
        self.assertEqual(self.transport.media_requests, [])

    def test_wrong_platform_sample_is_rejected_before_any_network(self):
        self.setting.douyin_url = SOURCE
        run = self.execute("download_smoke_douyin")
        self.assertEqual(run.error_code, "SAMPLE_URL_POLICY_REJECTED")
        self.assertEqual(self.transport.parse_requests, [])
        self.assertEqual(self.transport.media_requests, [])

    @override_settings(TIANTIANJIEXI_PROVIDER_ENABLED=False)
    def test_douyin_no_media_does_not_invent_a_selected_native_provider(self):
        self.transport.douyin_aweme = {"aweme_id": AWEME, "images": []}
        run = self.execute("download_smoke_douyin")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.download_smoke_details["provider"], "")
        self.assertNotEqual(run.provider, "native")
        self.assertFalse(run.download_smoke_details.get("media_type"))
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 0)
        self.assertEqual(self.transport.media_requests, [])

    def defer_douyin_service(self, *, native=True, status=200, delay=1.6):
        self.runtime.douyin_ultra_total_timeout_seconds = 1.0
        self.runtime.save()
        self.transport.ultra_delay = delay
        self.transport.ultra_status = status
        if not native:
            self.transport.douyin_aweme = {"aweme_id": AWEME, "images": []}
        # Keep the final HTTP boundary installed until background provider work settles.
        self.addCleanup(self.wait_for_deferred_service)

    def wait_for_deferred_service(self):
        self.assertTrue(self.transport.ultra_finished.wait(self.transport.ultra_delay + 2),
                        "deferred HTTP never completed")
        time.sleep(0.2)

    def assert_douyin_not_replayed(self):
        urls = [request.url for request in self.transport.parse_requests]
        self.assertEqual(urls.count(SERVICE), 1, urls)
        native = [url for url in urls if urlsplit(url).hostname == "aweme.snssdk.com"]
        self.assertEqual(len(native), 1, urls)

    def poll_public_continuation(self, token):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.post("/media/douyin-continuation/",
                                        {"continuation_token": token},
                                        content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content[:1000])
            body = response.json()
            if body["status"] != "continuation_pending":
                return body
            time.sleep(0.2)
        self.fail("public continuation did not settle")

    def assert_public_and_daily_select_late_service(self, *, native):
        self.defer_douyin_service(native=native)
        response = self.client.get("/results/", {"q": DOUYIN_SOURCE, "ultra_hd": "1"},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content[:1000])
        initial = response.json()
        self.assertEqual(initial["status"], "continuation_pending", initial)
        self.assertEqual(initial["has_results"], native, initial)
        if native:
            self.assertIn(NATIVE_TITLE, initial["html"])
        self.assertNotIn(ULTRA_TITLE, initial["html"])
        settled = self.poll_public_continuation(initial["continuation_token"])
        self.assertEqual(settled["status"], "success", settled)
        self.assertIn(ULTRA_TITLE, settled["html"])
        self.assert_douyin_not_replayed()
        self.wait_for_deferred_service()

        # Repeat the same HTTP fixture as a fresh Daily parse, not a payload-cache hit.
        cache.clear()
        self.transport.parse_requests.clear()
        self.transport.media_requests.clear()
        self.transport.bodies.clear()
        from .services import provider_download_smoke
        with patch.object(provider_download_smoke, "parse_sample",
                          wraps=provider_download_smoke.parse_sample) as parse:
            run = self.execute("download_smoke_douyin")
        self.assertEqual(parse.call_count, 1)
        self.assert_prefix(run, ULTRA_MEDIA)
        self.assertEqual(run.provider, "tiantianjiexi")
        self.assertEqual(run.download_smoke_details.get("media_type"), "video")
        self.assertTrue(all(run.download_smoke_details["fields"].values()))
        continuation = run.download_smoke_details.get("continuation", {})
        self.assertEqual(continuation.get("status"), "success")
        self.assertEqual(continuation.get("error_code"), "")
        self.assertGreater(continuation.get("poll_count", 0), 0)
        self.assert_douyin_not_replayed()

    def test_public_and_daily_empty_initial_result_both_select_late_service(self):
        self.assert_public_and_daily_select_late_service(native=False)

    def test_public_and_daily_native_initial_result_both_upgrade_to_late_service(self):
        self.assert_public_and_daily_select_late_service(native=True)

    def test_daily_keeps_native_fallback_when_continuation_fails(self):
        self.defer_douyin_service(status=503)
        from .services import provider_download_smoke
        with patch.object(provider_download_smoke, "parse_sample",
                          wraps=provider_download_smoke.parse_sample) as parse:
            run = self.execute("download_smoke_douyin")
        self.assertEqual(parse.call_count, 1)
        self.assertTrue(self.transport.ultra_finished.is_set(),
                        "Daily must consume continuation before keeping Native")
        self.assert_prefix(run, NATIVE_MEDIA)
        self.assertEqual(run.provider, "mobile_feed")
        self.assertEqual(run.download_smoke_details.get("media_type"), "video")
        self.assertTrue(run.download_smoke_details["fields"]["title"])
        self.assertTrue(run.download_smoke_details["fields"]["cover"])
        continuation = run.download_smoke_details.get("continuation", {})
        self.assertEqual(continuation.get("status"), "failed")
        self.assertEqual(continuation.get("error_code"), "DOUYIN_CONTINUATION_FAILED")
        self.assertGreater(continuation.get("poll_count", 0), 0)
        self.assert_douyin_not_replayed()

    def test_daily_keeps_native_fallback_when_continuation_naturally_expires(self):
        from .services import provider_download_smoke
        from .services.douyin_continuation import (
            DOUYIN_CONTINUATION_TTL_SECONDS, DOUYIN_CONTINUATION_RESULT_GRACE_SECONDS,
        )
        delay = DOUYIN_CONTINUATION_TTL_SECONDS + DOUYIN_CONTINUATION_RESULT_GRACE_SECONDS + 2
        self.defer_douyin_service(delay=delay)
        with patch.object(provider_download_smoke, "parse_sample",
                          wraps=provider_download_smoke.parse_sample) as parse:
            run = self.execute("download_smoke_douyin")
        self.assertEqual(parse.call_count, 1)
        self.assert_prefix(run, NATIVE_MEDIA)
        self.assertEqual(run.provider, "mobile_feed")
        continuation = run.download_smoke_details.get("continuation", {})
        self.assertIn(continuation.get("status"), {"timeout", "failed"})
        self.assertIn(continuation.get("error_code"), {
            "DOUYIN_CONTINUATION_EXPIRED", "DOUYIN_CONTINUATION_NOT_FOUND",
            "DOUYIN_CONTINUATION_TIMEOUT",
        })
        self.assertGreater(continuation.get("poll_count", 0), 0)
        self.assert_douyin_not_replayed()

    def test_daily_empty_fallback_preserves_terminal_continuation_failure(self):
        self.defer_douyin_service(native=False, status=503)
        run = self.execute("download_smoke_douyin")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "DOUYIN_CONTINUATION_FAILED")
        self.assertEqual(run.download_smoke_details["provider"], "")
        self.assertEqual(run.download_smoke_details["download"]["request_count"], 0)
        self.assertEqual(self.transport.media_requests, [])
        continuation = run.download_smoke_details.get("continuation", {})
        self.assertEqual(continuation.get("status"), "failed")
        self.assertEqual(continuation.get("error_code"), "DOUYIN_CONTINUATION_FAILED")
        self.assertGreater(continuation.get("poll_count", 0), 0)
        self.assert_douyin_not_replayed()

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
