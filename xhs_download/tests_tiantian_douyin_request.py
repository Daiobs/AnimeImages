"""Upstream URL regression: retain the real public controller and provider chain."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from contracts.network import offline_requests
from contracts import test_inv01_douyin as inv01
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient, TianTianJieXiApiError, TianTianJieXiGenericProvider,
    source_key_for_request, build_payload_cache_key,
)
from .services.provider_reliability import ProviderResultInvalidError

NOTE = f"https://www.douyin.com/note/{inv01.AWEME}"
GALLERY_PAYLOAD = {"code": 200, "data": {
    "title": "Contract gallery work", "videos": [],
    "images": [f"https://p3-sign.douyinpic.com/contract-photo-{i}.webp" for i in range(3)],
}}


class ChangedTianTianTransport(inv01.UpstreamTransport):
    def __init__(self):
        super().__init__()
        self.native_available = False
        self.requested_links = []
        self.reply = None
        self.short_target = inv01.SOURCE

    def answer(self, request):
        if request.url == "https://v.douyin.com/CONTRACT/":
            return inv01.fake_response(request, 302, headers={"Location": self.short_target})
        if request.url != inv01.SERVICE:
            return super().answer(request)
        body = request.body.decode() if isinstance(request.body, bytes) else request.body
        link = parse_qs(body)["link"][0]
        self.requested_links.append(link)
        self.record("tiantianjiexi")
        payload = self.reply
        if payload is None:
            payload = (inv01.ULTRA_PAYLOAD if link == inv01.SOURCE else
                       GALLERY_PAYLOAD if link == NOTE else
                       {"code": 1, "data": "", "msg": "private upstream reply"})
        return inv01.fake_response(request, payload=payload)


@override_settings(
    ALLOWED_HOSTS=["testserver"], CACHES=inv01.LOCAL_CACHE,
    ULTRA_HD_PROVIDER_ENABLED=True, ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
    DOUYIN_ULTRA_PROVIDER_ORDER="tiantianjiexi,native",
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="regression-fixture", TIANTIANJIEXI_PASSWORD="regression-fixture",
    TIANTIANJIEXI_API_BASE_URL=inv01.SERVICE, TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0, TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
)
class TianTianDouyinRequestRegressionTests(TransactionTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        ProviderRuntimeSetting.objects.create(
            singleton_key=1, douyin_ultra_mode=ProviderAccessMode.PUBLIC,
            douyin_ultra_rollout_percent=100, douyin_ultra_total_timeout_seconds=1,
        )
        self.transport = ChangedTianTianTransport()
        self.stack.enter_context(offline_requests(self.transport.send))
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root", return_value=root))
        self.addCleanup(self.transport.wait_quiet)

    def assert_public_request(self, source, expected, expected_requests=1):
        before = len(self.transport.requested_links)
        response = self.client.get(
            "/results/", {"q": source, "ultra_hd": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("has_results"), body)
        if expected == NOTE:
            self.assertIn("Contract gallery work", body["html"])
            for i in range(3):
                self.assertIn(f"contract-photo-{i}.webp", body["html"])
            self.assertNotIn(inv01.ULTRA_MEDIA, body["html"])
        else:
            self.assertIn(inv01.ULTRA_TITLE, body["html"])
            self.assertNotIn("contract-photo-", body["html"])
        self.assertEqual(self.transport.requested_links[before:], [expected] * expected_requests)

    def test_public_video_url(self):
        self.assert_public_request(inv01.SOURCE, inv01.SOURCE)

    def test_public_note_url(self):
        self.assert_public_request(NOTE, NOTE)

    def test_public_share_video_url(self):
        self.assert_public_request(f"https://www.iesdouyin.com/share/video/{inv01.AWEME}/", inv01.SOURCE)

    def test_public_share_note_url(self):
        self.assert_public_request(f"https://www.iesdouyin.com/share/note/{inv01.AWEME}/", NOTE)

    def test_public_short_video_url(self):
        self.assert_public_request("https://v.douyin.com/CONTRACT/", inv01.SOURCE)

    def test_public_short_note_url(self):
        self.transport.short_target = NOTE
        self.assert_public_request("https://v.douyin.com/CONTRACT/", NOTE)

    def test_public_modal_url_uses_video_without_type_probing(self):
        self.assert_public_request(f"https://www.douyin.com/jingxuan?modal_id={inv01.AWEME}", inv01.SOURCE)

    def test_public_aweme_id_url_uses_video_without_type_probing(self):
        self.assert_public_request(f"https://www.douyin.com/?aweme_id={inv01.AWEME}", inv01.SOURCE)

    def test_same_work_video_cache_does_not_replace_note_content(self):
        self.assert_public_request(inv01.SOURCE, inv01.SOURCE)
        self.assert_public_request(NOTE, NOTE)
        self.assert_public_request(f"https://www.iesdouyin.com/share/note/{inv01.AWEME}/", NOTE, 0)

    def test_same_work_note_cache_does_not_replace_video_content(self):
        self.assert_public_request(NOTE, NOTE)
        self.assert_public_request(inv01.SOURCE, inv01.SOURCE)
        self.assert_public_request(f"https://www.douyin.com/?modal_id={inv01.AWEME}", inv01.SOURCE, 0)

    def test_legacy_cache_without_request_kind_cannot_replace_note_content(self):
        key = source_key_for_request(inv01.SOURCE, f"douyin:{inv01.AWEME}")
        cache.set(build_payload_cache_key(key), {
            "title": inv01.ULTRA_TITLE, "videos": [{"url": inv01.ULTRA_MEDIA}],
        }, 30)
        self.assert_public_request(NOTE, NOTE)

    def test_single_flight_wait_does_not_consume_another_request_kind(self):
        provider = TianTianJieXiGenericProvider()
        key = source_key_for_request(inv01.SOURCE, f"douyin:{inv01.AWEME}")
        payload = {"_tiantian_request_kind": "video", "videos": [{"url": inv01.ULTRA_MEDIA}]}
        provider.cache_payload(key, payload)
        self.assertIsNone(provider.wait_for_cached_payload(key, 0.01, request_kind="note"))
        self.assertEqual(provider.wait_for_cached_payload(key, 0.01, request_kind="video"), payload)
        self.assertEqual(self.transport.requested_links, [])

    def test_business_code_one_is_not_a_success_or_generic_invalid_result(self):
        self.transport.reply = {"code": 1, "data": "", "msg": "private upstream reply"}
        with self.assertRaises(TianTianJieXiApiError) as caught:
            TianTianJieXiApiClient().parse_url(inv01.SOURCE)
        self.assertEqual(caught.exception.diagnostic_detail, "SERVICE_BUSINESS_CODE_1")
        self.assertNotIn("private", str(caught.exception))

    def test_non_success_code_with_media_is_still_rejected(self):
        self.transport.reply = {**inv01.ULTRA_PAYLOAD, "code": 1}
        with self.assertRaises(TianTianJieXiApiError) as caught:
            TianTianJieXiApiClient().parse_url(inv01.SOURCE)
        self.assertEqual(caught.exception.diagnostic_detail, "SERVICE_BUSINESS_CODE_1")

    def test_success_without_media_has_explicit_empty_result_detail(self):
        self.transport.reply = {"code": 200, "data": {"title": "No media"}}
        provider = TianTianJieXiGenericProvider()
        with self.assertRaises(ProviderResultInvalidError) as caught:
            provider.parse(inv01.SOURCE)
        self.assertEqual(caught.exception.diagnostic_detail, "SERVICE_EMPTY_RESULT")
        self.assertFalse(provider.failure_cache_status(source_key_for_request(inv01.SOURCE))["hit"])
