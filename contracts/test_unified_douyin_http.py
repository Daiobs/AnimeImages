"""Step 3, Douyin switched on: the HTTP contract goes through the kernel.

The results page and the selection endpoint are reached over HTTP. Providers
are fakes that return raw upstream facts to the real mappers; the chain is the
kernel's ChainRun, and the existing parse path must not be called.
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
from xhs_download.services.pipeline import ChainRun, Failure, FailureKind
from xhs_download.services.unified import routes
from xhs_download.services.unified.douyin import NativeAdapter, ServiceTianTianAdapter

AWEME = "7634213610894689787"
SOURCE = f"https://www.douyin.com/video/{AWEME}"
NO_CORS = "https://v11-cold-src.douyinvod.com/obj/720.mp4"
CORS = "https://v11-colds.douyinvod.com/obj/720.mp4"
NATIVE_RAW = {"aweme_id": AWEME, "desc": "契约作品 #话题", "video": {
    "duration": 9100,
    "bit_rate": [
        {"bit_rate": 652156, "play_addr": {"url_list": [NO_CORS, CORS],
                                           "width": 720, "height": 1280, "data_size": 741828}},
        {"bit_rate": 375626, "play_addr": {"url_list": ["https://v11-colds.douyinvod.com/obj/540.mp4"],
                                           "width": 540, "height": 960, "data_size": 427275}},
    ],
    "download_addr": {"url_list": ["https://v5-gz-a.douyinvod.com/obj/watermark.mp4"],
                      "width": 720, "height": 720, "data_size": 2507813},
}}
SERVICE_RAW = {"code": 200, "data": {"aweme_id": AWEME, "title": "契约作品",
                                     "videos": [{"url": "https://v26-qingtao.douyinvod.com/obj/original.mp4"}]}}


class RawFake:
    """A Provider whose fetch returns recorded raw facts; map is the real one."""

    def __init__(self, raw):
        self.raw = raw

    def fetch(self, source_url, budget):
        return deepcopy(self.raw) if not isinstance(self.raw, Failure) else self.raw


class FakeNative(RawFake, NativeAdapter):
    pass


class FakeService(RawFake, ServiceTianTianAdapter):
    pass


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UnifiedDouyinHttpContracts(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        PipelineRoute.objects.filter(platform="douyin").update(enabled=True)
        # Cooldowns are per process (INV-02); each contract starts without any.
        routes._cooldowns.clear()
        self.addCleanup(routes._cooldowns.clear)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.media_requests = []
        self.stack.enter_context(offline_requests(self.send))
        self.stack.enter_context(patch("xhs_download.views.parse_media_page",
                                       side_effect=AssertionError("existing parse path used")))
        self.runs = []
        self.stack.enter_context(patch("xhs_download.services.unified.runner.ChainRun",
                                       side_effect=lambda *args: self.runs.append(ChainRun(*args)) or self.runs[-1]))
        self.adapters = {"native": FakeNative(NATIVE_RAW)}
        self.stack.enter_context(patch("xhs_download.services.unified.douyin.build_adapters",
                                       side_effect=lambda *args, **kwargs: dict(self.adapters)))

    def send(self, request, **options):
        self.media_requests.append((request.method, request.url, request.headers.get("Origin"),
                                    request.headers.get("Range")))
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 206
        response._content_consumed, response._content = True, b"\0"
        response.headers.update({"Content-Type": "video/mp4", "Content-Range": "bytes 0-0/741828"})
        if request.url != NO_CORS:
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    def picker(self, **params):
        response = self.client.get(reverse("results"), {"q": SOURCE, **params})
        self.assertEqual(response.status_code, 200, response.content[:800])
        return BeautifulSoup(response.content, "html.parser").select_one("[data-ytdlp-picker]")

    def select(self, radio, action):
        return self.client.post(reverse("service_selected_format"), json.dumps({
            "candidate_id": radio["value"], "resource_token": radio["data-token"], "action": action,
        }), content_type="application/json")

    def test_native_qualities_are_offered_and_saved_from_a_browser_readable_mirror(self):
        root = self.picker()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(root["data-selection-schema"], "unified_selected_format_v1")
        radios = root.select('[data-picker-radio][data-kind="video"]')
        self.assertEqual([r["data-label"] for r in radios], ["720p", "540p"])
        self.assertEqual([r.has_attr("checked") for r in radios], [True, False])
        with patch("xhs_download.services.provider_strategy.prepare_browser_transport", side_effect=lambda item: item):
            response = self.select(radios[0], "download")
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()["item"]
        self.assertEqual((item["url"], item["selection_delivery"], item["type"]), (CORS, "ordinary", "video"))
        self.assertNotIn("selection_schema", item)
        self.assertEqual([(m, u, o, r) for m, u, o, r in self.media_requests],
                         [("GET", NO_CORS, "http://testserver", "bytes=0-0"),
                          ("GET", CORS, "http://testserver", "bytes=0-0")])

    def test_preferred_service_wins_and_watermarked_copy_is_never_offered(self):
        self.adapters["tiantianjiexi"] = FakeService(SERVICE_RAW)
        root = self.picker(ultra_hd="1")
        radios = root.select('[data-picker-radio][data-kind="video"]')
        self.assertEqual([r["data-label"] for r in radios], ["原画"])
        self.assertEqual([(a.provider, a.outcome) for a in self.runs[0].attempts][-1], ("tiantianjiexi", "success"))
        link = self.select(radios[0], "link").json()["item"]["url"]
        self.assertEqual(link, "https://v26-qingtao.douyinvod.com/obj/original.mp4")

    def test_preferred_failure_shows_the_native_floor_and_cools_the_service_down(self):
        self.adapters["tiantianjiexi"] = FakeService(Failure(FailureKind.UNAVAILABLE, "SERVICE_DOWN"))
        root = self.picker(ultra_hd="1")
        labels = [r["data-label"] for r in root.select('[data-picker-radio][data-kind="video"]')]
        self.assertEqual(labels, ["720p", "540p"])
        self.adapters["tiantianjiexi"] = FakeService(SERVICE_RAW)
        self.picker(ultra_hd="1")
        self.assertEqual([(a.provider, a.outcome) for a in self.runs[1].attempts][0], ("tiantianjiexi", "cooling_down"))

    def test_switched_off_platform_stays_on_the_existing_path(self):
        PipelineRoute.objects.filter(platform="douyin").update(enabled=False)
        with self.assertRaises(AssertionError):
            self.client.get(reverse("results"), {"q": SOURCE})
        self.assertEqual(self.runs, [])
