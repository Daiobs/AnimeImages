"""Step 3, Bilibili switched on: the HTTP contract goes through the kernel.

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

from contracts.network import offline_requests
from xhs_download.models import PipelineRoute
from xhs_download.services.pipeline import ChainRun, Failure, FailureKind
from xhs_download.services.unified import routes

SOURCE = "https://www.bilibili.com/video/BV13x41117TL/"
HD = "https://upos-sz-mirrorhw.bilivideo.com/upgcxcode/hd.mp4"
SD = "https://upos-sz-mirrorhw.bilivideo.com/upgcxcode/sd.mp4"
FALLBACK = "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/fallback.mp4"
FORMATS = [{"quality": 64, "new_description": "720P 准高清"}, {"quality": 16, "new_description": "360P 流畅"}]


def play(quality, url, size):
    return {"quality": quality, "format": "mp4", "accept_quality": [64, 16], "support_formats": FORMATS,
            "durl": [{"url": url, "size": size, "order": 1}]}


NATIVE = {"view": {"code": 0, "data": {"bvid": "BV13x41117TL", "aid": 170001, "title": "契约 B 站",
                                        "pages": [{"page": 1, "cid": 279786, "duration": 12}]}},
          "playurl": {"code": 0, "data": play(64, HD, 5309115)},
          "selection": {"page": 1, "playurl_params": {"cid": 279786, "bvid": "BV13x41117TL", "qn": 80},
                        "page_url": SOURCE},
          "other_playurls": [(16, {"code": 0, "data": play(16, SD, 2411724)})]}
SERVICE = {"code": 200, "data": {"bvid": "BV13x41117TL", "title": "契约 B 站", "videos": [{"url": FALLBACK}],
                                 "images": ["https://i1.hdslb.com/bfs/archive/cover.jpg"]}}


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UnifiedBilibiliHttpContracts(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        PipelineRoute.objects.filter(platform="bilibili").update(enabled=True)
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
        self.native = deepcopy(NATIVE)
        self.stack.enter_context(patch("xhs_download.services.unified.bilibili.NativeAdapter.fetch",
                                       side_effect=lambda url, budget: deepcopy(self.native)))
        self.service_calls = []
        self.stack.enter_context(patch("xhs_download.services.unified.bilibili.ServiceTianTian.fetch",
                                       side_effect=lambda url, budget: self.service_calls.append(url) or deepcopy(SERVICE)))
        self.admitted = ()
        self.stack.enter_context(patch("xhs_download.services.platforms.bilibili_fallback.authorized_services",
                                       side_effect=lambda *args, **kwargs: self.admitted))

    def send(self, request, **options):
        raise AssertionError("no media request is expected while parsing")

    def radios(self):
        response = self.client.get(reverse("results"), {"q": SOURCE})
        self.assertEqual(response.status_code, 200, response.content[:800])
        root = BeautifulSoup(response.content, "html.parser").select_one("[data-ytdlp-picker]")
        self.assertEqual(root["data-selection-schema"], "unified_selected_format_v1")
        return root.select('[data-picker-radio][data-kind="video"]')

    def link(self, radio):
        return self.client.post(reverse("service_selected_format"), json.dumps({
            "candidate_id": radio["value"], "resource_token": radio["data-token"], "action": "link",
        }), content_type="application/json").json()["item"]["url"]

    def test_native_qualities_are_offered_best_first(self):
        radios = self.radios()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual([r["data-label"] for r in radios], ["720P 准高清", "360P 流畅"])
        self.assertTrue(radios[0].has_attr("checked"))
        self.assertEqual(self.link(radios[1]), SD)
        self.assertEqual(self.service_calls, [])

    def test_native_failure_uses_an_admitted_service_for_the_same_video(self):
        self.native = {"view": {"code": -412, "message": "请求被拦截"}}
        self.admitted = ("tiantianjiexi_generic",)
        radios = self.radios()
        self.assertEqual(self.link(radios[0]), FALLBACK)
        self.assertEqual(self.service_calls, [SOURCE])

    def test_without_admission_a_native_failure_is_an_error_page(self):
        self.native = {"view": {"code": -412, "message": "请求被拦截"}}
        response = self.client.get(reverse("results"), {"q": SOURCE})
        self.assertEqual(self.service_calls, [])
        self.assertIsNone(BeautifulSoup(response.content, "html.parser").select_one("[data-ytdlp-picker]"))
