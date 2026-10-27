"""Step 3, Xiaohongshu switched on: the HTTP contract goes through the kernel.

Native and TianTian return recorded raw facts to the real mappers; the chain
is the kernel's ChainRun, and the existing parse path must not be called.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
import requests

from contracts.network import offline_requests
from xhs_download.models import PipelineRoute
from xhs_download.services.pipeline import ChainRun
from xhs_download.services.unified import routes

ID = "64a1b2c3d4e5f6789012abcd"
SOURCE = f"https://www.xiaohongshu.com/explore/{ID}"
TOKEN = "1040g2sg31abcdefghijabcdefghijabcdefghij"
GALLERY = {"note": {"noteDetailMap": {ID: {"note": {
    "noteId": ID, "type": "normal", "title": "原图契约", "imageList": [{
        "urlDefault": f"http://sns-webpic-qc.xhscdn.com/202610271234/0123abcd/{TOKEN}!nd_dft_wlteh_webp_3",
        "width": 2358, "height": 2948}]}}}}}
VIDEO = {"note": {"noteDetailMap": {ID: {"note": {
    "noteId": ID, "type": "video", "title": "视频契约", "video": {"media": {"stream": {"h264": [{
        "masterUrl": "http://sns-video-v4.xhscdn.com/stream/hd.mp4", "qualityType": "HD",
        "width": 720, "height": 1280, "size": 9554309, "videoCodec": "h264", "audioCodec": "aac"}]}}}}}}}}
SERVICE = {"code": 200, "data": {"title": "视频契约", "desc": "视频契约", "videos": [{"url": "https://sns-video-hw.xhscdn.com/normal.mp4", "streams": [
    {"url": "https://sns-video-hw.xhscdn.com/normal.mp4", "qualityLabel": "正常", "size": 9554309},
    {"url": "https://sns-video-hw.xhscdn.com/original", "qualityLabel": "原画", "size": 129382759}]}],
    "images": ["https://sns-img-hw.xhscdn.com/cover"]}}


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UnifiedXhsHttpContracts(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        PipelineRoute.objects.filter(platform="xiaohongshu").update(enabled=True)
        routes._cooldowns.clear()
        self.addCleanup(routes._cooldowns.clear)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline_requests(self.send))
        self.stack.enter_context(patch("xhs_download.views.parse_media_page",
                                       side_effect=AssertionError("existing parse path used")))
        self.runs = []
        self.stack.enter_context(patch("xhs_download.services.unified.runner.ChainRun",
                                       side_effect=lambda *args: self.runs.append(ChainRun(*args)) or self.runs[-1]))
        self.page = GALLERY
        self.service_calls = []
        self.stack.enter_context(patch("xhs_download.services.unified.xiaohongshu.NativeXhsAdapter.fetch",
                                       side_effect=lambda url, budget: deepcopy(self.page)))
        self.stack.enter_context(patch("xhs_download.services.unified.xiaohongshu.XhsServiceTianTian.fetch",
                                       side_effect=lambda url, budget: self.service_calls.append(url) or deepcopy(SERVICE)))
        self.stack.enter_context(patch("xhs_download.services.platforms.xhs_tiantian_hybrid.xhs_tiantian_provider_is_available",
                                       return_value=True))
        self.stack.enter_context(patch("xhs_download.views.ultra_hd_is_available", return_value=True))

    def send(self, request, **options):
        raise AssertionError("no media request is expected while parsing")

    def page_for(self, **params):
        response = self.client.get(reverse("results"), {"q": SOURCE, **params})
        self.assertEqual(response.status_code, 200, response.content[:800])
        return BeautifulSoup(response.content, "html.parser")

    def test_gallery_offers_original_photos_without_calling_the_service(self):
        soup = self.page_for(ultra_hd="1")
        self.assertEqual(len(self.runs), 1)
        cards = soup.select('.asset-card[data-type="image"]')
        self.assertEqual([c["data-url"] for c in cards], [f"https://ci.xiaohongshu.com/{TOKEN}?imageView2/format/jpg"])
        self.assertEqual(self.service_calls, [])
        self.assertIsNone(soup.select_one("[data-ytdlp-picker]"))

    def test_video_note_offers_the_services_qualities_best_first(self):
        self.page = VIDEO
        soup = self.page_for(ultra_hd="1")
        root = soup.select_one("[data-ytdlp-picker]")
        self.assertEqual(root["data-selection-schema"], "unified_selected_format_v1")
        radios = root.select('[data-picker-radio][data-kind="video"]')
        self.assertEqual([r["data-label"] for r in radios], ["原画", "正常"])
        self.assertTrue(radios[0].has_attr("checked"))
        self.assertEqual(self.service_calls, [SOURCE])

    def test_without_the_ultra_gate_the_native_stream_is_offered_over_https(self):
        self.page = VIDEO
        with patch("xhs_download.views.ultra_hd_is_available", return_value=False):
            soup = self.page_for()
        root = soup.select_one("[data-ytdlp-picker]")
        radios = root.select('[data-picker-radio][data-kind="video"]')
        self.assertEqual([r["data-label"] for r in radios], ["HD"])
        self.assertEqual(self.service_calls, [])
        link = self.client.post(reverse("service_selected_format"), json.dumps({
            "candidate_id": radios[0]["value"], "resource_token": radios[0]["data-token"], "action": "link",
        }), content_type="application/json").json()["item"]["url"]
        self.assertEqual(link, "https://sns-video-v4.xhscdn.com/stream/hd.mp4")
