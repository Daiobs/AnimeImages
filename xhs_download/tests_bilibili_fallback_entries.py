"""Offline entrypoint integration through registry, providers and templates."""
from contextlib import ExitStack
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from download_console.models import DownloadRuntimeSetting
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .views import build_batch_result_row, build_search_results_context


SOURCE = "https://www.bilibili.com/video/BV1J4JX6PEMV/"
MEDIA = "http://upos-sz-mirrorcos.bilivideo.com/entry-video.mp4"
COVER = "https://i0.hdslb.com/entry-cover.jpg"
SERVICE = "https://service.example.invalid/parse"
PROVIDER = "tiantianjiexi_generic"


def json_response(url, payload, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    response._content_consumed = True
    return response


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="entry-fixture",
    TIANTIANJIEXI_PASSWORD="entry-fixture",
    TIANTIANJIEXI_API_BASE_URL=SERVICE,
    THIRD_PARTY_RATE_LIMIT_PER_MINUTE=10,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=100,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class BilibiliFallbackEntryTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.factory = RequestFactory()
        self.config = ProviderRuntimeSetting(
            third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="upstream:tiantianjiexi_generic",
            third_party_blocked_platforms="",
        )
        self.stack.enter_context(patch.object(
            ProviderRuntimeSetting, "load", return_value=self.config))
        self.stack.enter_context(patch.object(
            DownloadRuntimeSetting, "load",
            return_value=DownloadRuntimeSetting(visitor_video_download_enabled=True)))
        self.stack.enter_context(patch("xhs_download.views.record_parse_request_log"))
        self.limits = self.stack.enter_context(patch(
            "xhs_download.services.rate_limits.consume_fixed_window",
            return_value=SimpleNamespace(allowed=True, retry_after=0)))
        self.stack.enter_context(patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        self.stack.enter_context(patch(
            "socket.socket.connect", side_effect=AssertionError("real network forbidden")))
        directory = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root",
            return_value=Path(directory)))
        self.events = []
        self.native_success = False
        self.native = self.stack.enter_context(patch(
            "xhs_download.services.common._SingleHopSession.request",
            side_effect=self.native_http))
        self.post = self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.requests.post",
            side_effect=self.service_post))

    def native_http(self, method, url, **kwargs):
        self.assertEqual(method.upper(), "GET")
        self.assertEqual(urlsplit(url).hostname, "api.bilibili.com")
        self.assertFalse(kwargs["allow_redirects"])
        self.events.append("native")
        if not self.native_success:
            return json_response(url, {"code": -412}, status=412)
        if urlsplit(url).path == "/x/web-interface/view":
            data = {
                "bvid": "BV1J4JX6PEMV", "aid": 123, "cid": 456,
                "title": "Native entry title", "pic": COVER,
                "pages": [{"cid": 456, "page": 1, "part": "Part one"}],
            }
        elif urlsplit(url).path == "/x/player/playurl":
            data = {"quality": 80, "durl": [{"url": MEDIA, "size": 12345}]}
        else:
            raise AssertionError(f"Unexpected Native endpoint: {urlsplit(url).path}")
        return json_response(url, {"code": 0, "data": data})

    def service_post(self, url, **kwargs):
        self.events.append("service")
        self.assertEqual(url, SERVICE)
        self.assertEqual(kwargs["data"]["link"], SOURCE)
        self.assertGreater(kwargs["timeout"], 0)
        return json_response(url, {"code": 200, "data": {
            "desc": "Service entry title", "cover": COVER,
            "videos": [{"streams": [
                {"url": MEDIA, "quality": "720P", "size": 12345},
                {"url": MEDIA.replace("entry-video", "entry-large"),
                 "quality": "1080P", "size": 23456},
            ]}],
        }})

    def request(self):
        request = self.factory.get("/", {"q": SOURCE})
        request.user = AnonymousUser()
        return request

    def consume(self, entry):
        if entry == "home":
            return build_search_results_context(self.request())
        row = build_batch_result_row(
            self.request(), SOURCE, prefer_ultra_hd=False, prefer_youtube_merge=False,
            per_link_timeout_seconds=30)
        return row, 200 if row["status"] == "success" else 400

    def action(self, candidate, action):
        return self.client.post(
            reverse("service_selected_format"),
            {**{key: candidate[key] for key in ("candidate_id", "resource_token")},
             "action": action},
            content_type="application/json")

    def assert_service_components_and_actions(self, entry):
        context, status = self.consume(entry)
        self.assertEqual(status, 200, context)
        self.assertFalse(context.get("error_message"))
        self.assertEqual(context["platform"], "bilibili")
        self.assertEqual(context["base_filename"], "Service entry title")
        self.assertEqual({c["provider_id"] for c in context["upstream_candidates"]}, {PROVIDER})
        self.assertEqual(len(context["video_items"]), 2)
        expected_urls = {MEDIA, MEDIA.replace("entry-video", "entry-large")}
        self.assertEqual({item["url"] for item in context["video_items"]}, expected_urls)
        self.assertEqual({item["download_strategy"] for item in context["video_items"]},
                         {"server_attachment"})
        self.assertEqual(self.events, ["native", "service"])
        self.post.assert_called_once()
        self.native.assert_called_once()
        self.assertEqual(
            [call.args[0] for call in self.limits.call_args_list],
            ["third-party-provider-minute-ip", "third-party-provider-minute-global"])
        if entry == "home":
            html = render_to_string("partials/xhs_results_region.html", context)
        else:
            self.assertEqual(context["status"], "success")
            self.assertEqual(context["selected_provider"], PROVIDER)
            self.assertEqual(context["metadata"]["provider_fallback_count"], 1)
            html = render_to_string("partials/batch_result_card.html", {"row": context})
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(len(soup.select("[data-picker-radio]")), 2)
        self.assertEqual(len(soup.select('[data-can-legacy="true"]')), 2)
        self.assertFalse(soup.select(
            '.asset-card[data-type="video"], [data-batch-download-item][data-type="video"]'))
        candidates = context["upstream_candidates"]
        for candidate in candidates:
            copied = self.action(candidate, "link")
            self.assertEqual(copied.status_code, 200, copied.content)
            legacy = self.action(candidate, "legacy")
            self.assertEqual(legacy.status_code, 200, legacy.content)
            self.assertEqual(legacy.json()["item"]["url"], copied.json()["item"]["url"])
            self.assertIn(legacy.json()["item"]["url"], expected_urls)
            self.assertFalse(legacy.json()["item"].get("browser_upstream_bound"))
            self.assertEqual(legacy.json()["item"]["selection_delivery"], "legacy_manifest")
            browser_download = self.action(candidate, "download")
            self.assertEqual(browser_download.status_code, 409, browser_download.content)
            cover = self.action(candidate, "cover")
            self.assertEqual(cover.status_code, 200, cover.content)
            self.assertEqual(cover.json()["url"], COVER)
        self.assertEqual(self.events, ["native", "service"], "Actions must not reparse or fetch media")
        self.config.third_party_allowed_public_platforms = ""
        for action in ("link", "legacy", "cover"):
            self.assertEqual(self.action(candidates[0], action).status_code, 403)
        self.post.assert_called_once()

    def assert_native_without_service(self, entry):
        self.native_success = True
        context, status = self.consume(entry)
        self.assertEqual(status, 200, context)
        self.assertFalse(context.get("error_message"))
        self.assertEqual(context["base_filename"], "Native entry title")
        self.assertEqual(len(context["video_items"]), 1)
        self.assertFalse(context.get("upstream_candidates"))
        self.assertEqual(self.events, ["native", "native"])
        self.post.assert_not_called()
        self.limits.assert_not_called()

    def assert_unauthorized_failure_without_service(self, entry):
        self.config.third_party_allowed_public_platforms = ""
        context, status = self.consume(entry)
        if entry == "home":
            self.assertEqual(status, 200)
            self.assertTrue(context["error_message"])
        else:
            self.assertNotEqual(status, 200, context)
            self.assertEqual(context["status"], "failed")
        self.assertFalse(context.get("video_items"))
        self.assertFalse(context.get("upstream_candidates"))
        self.assertEqual(self.events, ["native"])
        self.post.assert_not_called()
        self.limits.assert_not_called()

    def test_home_native_412_reaches_service_picker_and_actions(self):
        self.assert_service_components_and_actions("home")

    def test_batch_normal_native_412_reaches_service_picker_and_actions(self):
        self.assert_service_components_and_actions("batch")

    def test_home_native_success_never_starts_service(self):
        self.assert_native_without_service("home")

    def test_batch_normal_native_success_never_starts_service(self):
        self.assert_native_without_service("batch")

    def test_home_anonymous_without_allowlist_never_starts_service(self):
        self.assert_unauthorized_failure_without_service("home")

    def test_batch_normal_anonymous_without_allowlist_never_starts_service(self):
        self.assert_unauthorized_failure_without_service("batch")
