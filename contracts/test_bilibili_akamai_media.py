"""INV-03/04/05: approved Akamai resources traverse the existing public consumers."""

import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from contracts.network import offline_requests
from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting


SOURCE = "https://www.bilibili.com/video/BV1xx411c7mD/"
MEDIA = "https://upos-hz-mirrorakam.akamaized.net/video.mp4"


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="contract-fixture",
    TIANTIANJIEXI_PASSWORD="contract-fixture",
    TIANTIANJIEXI_API_BASE_URL="https://provider.invalid/parse",
    TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
)
class BilibiliAkamaiHTTPContracts(TransactionTestCase):
    def setUp(self):
        self.assertFalse((settings.BASE_DIR / ".env").exists(), "Run contracts without .env")
        cache.clear()
        self.addCleanup(cache.clear)
        self.runtime = ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="bilibili:tiantianjiexi_fallback",
        )
        self.media_url = MEDIA
        self.native_failure = False
        self.calls = []
        self.enterContext(offline_requests(self.send))

    def send(self, request, **kwargs):
        self.calls.append((request.method, request.url, dict(request.headers)))
        parsed = urlsplit(request.url)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        if request.url == "https://www.bilibili.com/":
            body = b""
            response.headers["Content-Type"] = "text/html"
        elif parsed.hostname == "api.bilibili.com" and self.native_failure:
            response.status_code = 412
            body = b'{"code": -412}'
        elif parsed.path == "/x/web-interface/nav":
            body = b'{"code": -101}'
        elif parsed.path == "/x/web-interface/view":
            body = json.dumps({"code": 0, "data": {
                "bvid": "BV1xx411c7mD", "aid": 170001, "title": "Akamai contract video",
                "pages": [{"cid": 123, "page": 1}],
            }}).encode()
        elif parsed.path == "/x/player/playurl":
            body = json.dumps({"code": 0, "data": {
                "quality": 80, "durl": [{"url": self.media_url, "size": 0}],
            }}).encode()
        elif request.url == "https://provider.invalid/parse":
            self.assertEqual(request.method, "POST")
            body = json.dumps({"code": 200, "data": {
                "title": "Akamai contract video", "videos": [{"url": self.media_url}],
            }}).encode()
        elif request.url == self.media_url:
            body = b"first-last"
            response.headers["Content-Type"] = "video/mp4"
        else:
            raise AssertionError(f"Unexpected upstream HTTP: {request.method} {request.url}")
        response.headers["Content-Length"] = str(len(body))
        response.raw = io.BytesIO(body)
        response.raw._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda _: None))
        return response

    def result_item(self):
        page = self.client.get(reverse("bilibili_results"), {"q": SOURCE})
        self.assertEqual(page.status_code, 200, page.content[:1000])
        soup = BeautifulSoup(page.content, "html.parser")
        if self.native_failure:
            option = soup.select_one('[data-picker-radio]')
            self.assertIsNotNone(option, "Expected approved service candidate in public HTML")
            selected = self.client.post(reverse("service_selected_format"), {
                "candidate_id": option["value"], "resource_token": option["data-token"], "action": "legacy",
            }, content_type="application/json")
            self.assertEqual(selected.status_code, 200, selected.content)
            return selected.json()["item"]
        card = soup.select_one('.asset-card[data-type="video"]')
        self.assertIsNotNone(card, "Expected usable Bilibili video in public HTML")
        self.assertEqual(card["data-url"], self.media_url)
        item = {"type": "video", "url": card["data-url"], "source_platform": "bilibili"}
        for key in ("provider", "provider_resource_id", "provider_cache_key", "provider_manifest_token", "asset_id"):
            value = card.get("data-" + key.replace("_", "-"))
            if value:
                item[key] = value
        return item

    def prepare(self, item):
        return self.client.post(reverse("media_download_prepare"), {
            "media_items": [item], "platform_key": "bilibili", "base_filename": "Akamai contract video",
        }, content_type="application/json")

    def assert_browser_download(self, item):
        response = self.client.post(reverse("download_images_api"), {
            "media_items": [item], "download_mode": "auto", "route_only": True, "intent": "download",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["route_type"], "direct")
        descriptor = response.json()["items"][0]
        self.assertEqual(descriptor["url"], self.media_url)
        self.assertTrue(descriptor["browser_direct_fetch_safe"])
        self.assertEqual(descriptor["download_strategy"], "browser_streaming")
        script = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const protocol = require('./static/js/ordinary_media_streaming_protocol.js');
const item = JSON.parse(fs.readFileSync(0, 'utf8'));
const descriptor = protocol.createTransportDescriptor({...item, downloadId: 'a'.repeat(32)});
assert.equal(descriptor.url, item.url);
"""
        checked = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                 input=json.dumps(descriptor), text=True, capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_native_akamai_main_url_uses_browser_route_without_invalid_handoff(self):
        for label in ("hz", "sz"):
            with self.subTest(label=label):
                cache.clear()
                self.calls.clear()
                self.media_url = MEDIA.replace("upos-hz", "upos-" + label)
                item = self.result_item()
                prepared = self.prepare(item)
                # This endpoint is server-only; INV-03 keeps approved HTTPS
                # complete videos on the existing ordinary browser route.
                self.assertEqual(prepared.status_code, 400, prepared.content)
                self.assertEqual(prepared.json()["error_code"], "DOWNLOAD_PREPARE_ROUTE_REJECTED")
                self.assert_browser_download(item)
                media_calls = [call for call in self.calls if call[1] == self.media_url]
                self.assertEqual([call[0] for call in media_calls], ["HEAD"])
                self.assertEqual(media_calls[-1][2]["Referer"], "https://www.bilibili.com/")

    def test_service_akamai_result_retains_signed_selection_and_browser_route(self):
        self.native_failure = True
        item = self.result_item()
        self.assertEqual(item["provider"], "tiantianjiexi_generic")
        self.assertTrue(item["provider_manifest_token"])
        prepared = self.prepare(item)
        self.assertEqual(prepared.status_code, 400, prepared.content)
        self.assertEqual(prepared.json()["error_code"], "DOWNLOAD_PREPARE_ROUTE_REJECTED")
        self.assert_browser_download(item)
        media_calls = [call[0] for call in self.calls if call[1] == self.media_url]
        self.assertTrue(media_calls, "Bilibili service size probe must reach the approved CDN")
        rejected = self.prepare({**item, "url": self.media_url + "?tampered"})
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertEqual(rejected.json()["error_code"], "PROVIDER_MANIFEST_REJECTED")

    def test_prepare_rejects_other_akamai_hosts_schemes_ports_and_separate_tracks(self):
        for url in (
            MEDIA.replace("upos-hz", "hz"), MEDIA.replace("upos-hz", "evil-upos-hz"),
            "https://mirrorakam.akamaized.net/video.mp4",
            MEDIA.replace(".net/", ".net.evil.com/"), "https://arbitrary.akamaized.net/video.mp4",
            MEDIA.replace("https:", "http:"), MEDIA.replace(".net/", ".net:444/"),
            MEDIA.replace(".mp4", ".m4s"), MEDIA.replace(".mp4", ".mpd"), MEDIA.replace(".mp4", ".m3u8"),
        ):
            with self.subTest(url=url):
                response = self.prepare({"type": "video", "url": url})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error_code"], "DOWNLOAD_HANDOFF_INVALID")
        self.assertEqual(self.calls, [])
