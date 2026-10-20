"""INV-01 HTTP contract (T12). Run in a checkout without .env.

    python manage.py test contracts.test_inv01_douyin --verbosity 2

The public homepage parse and continuation routes are real. Only upstream
HTTP/DNS, the TianTian account lock directory and the local KuKuTool Node
runtime are replaced. The runtime is a subprocess outside the HTTP boundary,
so it is declared absent and can never reach the real network. Each upstream
records when it was asked, relative to the start of the parse.
"""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting
from contracts.network import offline_requests


AWEME = "7669858827638845160"
SOURCE = f"https://www.douyin.com/video/{AWEME}"
SERVICE = "https://provider.contract.invalid/parse"
SIDECAR = "http://kukutool.contract.invalid"
NATIVE_MEDIA = "https://v3-web.douyinvod.com/contract-native.mp4"
ULTRA_MEDIA = "https://v3-dy-o.zjcdn.com/contract-ultra.mp4"
NATIVE_TITLE = "Contract native work"
ULTRA_TITLE = "Contract ultra work"
# Admin budget minimum; Native answers well inside it but not instantly.
BUDGET = 1.0
NATIVE_SECONDS = 0.8
LOCAL_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "inv01-contracts",
    }
}
NATIVE_AWEME = {
    "aweme_id": AWEME, "desc": NATIVE_TITLE, "author": {"nickname": "Contract"},
    "video": {
        "play_addr": {"url_list": [NATIVE_MEDIA]},
        "cover": {"url_list": ["https://p3-sign.douyinpic.com/contract-cover.webp"]},
    },
}
ULTRA_PAYLOAD = {"code": 200, "data": {
    "title": ULTRA_TITLE, "cover": "https://p3.douyinpic.com/contract-ultra.jpg",
    "videos": [{"streams": [
        {"url": ULTRA_MEDIA, "qualityLabel": "4K", "sizeInBytes": 60 * 1024 * 1024},
    ]}],
}}


def fake_response(request, status=200, *, payload=None, headers=None):
    response = requests.Response()
    response.status_code = status
    response.url = request.url
    response.request = request
    response.headers.update(headers or {})
    if payload is not None:
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
    else:
        response._content = b"" if request.method == "HEAD" else b"\0"
    response._content_consumed = True
    return response


class UpstreamTransport:
    """Replace only Requests' final adapter transport, not provider behavior."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.events = []
        self.in_flight = 0
        self.last_activity = self.started
        self.release_ultra = threading.Event()
        self.ultra_waits = False

    def services(self):
        with self.lock:
            return [name for name, _ in self.events if name != "native"]

    def asked_at(self, name):
        with self.lock:
            return next((at for event, at in self.events if event == name), None)

    def record(self, name):
        with self.lock:
            self.events.append((name, time.monotonic() - self.started))

    def send(self, request, **kwargs):
        with self.lock:
            self.in_flight += 1
        try:
            return self.answer(request)
        finally:
            with self.lock:
                self.in_flight -= 1
                self.last_activity = time.monotonic()

    def answer(self, request):
        parsed = urlsplit(request.url)
        if parsed.hostname == "aweme.snssdk.com" and parsed.path == "/aweme/v1/feed/":
            self.record("native")
            time.sleep(NATIVE_SECONDS)
            return fake_response(request, payload={"aweme_list": [NATIVE_AWEME]})
        if request.url == SERVICE:
            assert request.method == "POST", request.method
            body = request.body.decode() if isinstance(request.body, bytes) else request.body
            assert AWEME in parse_qs(body)["link"][0], body
            self.record("tiantianjiexi")
            if self.ultra_waits:
                assert self.release_ultra.wait(10), "contract never released TianTian"
            return fake_response(request, payload=ULTRA_PAYLOAD)
        if request.url == f"{SIDECAR}/health":
            return fake_response(request, payload={
                "contract_version": "douyin-source-resolver.v1",
                "status": "ok", "ultra_provider": {"status": "ok"},
            })
        if request.url == f"{SIDECAR}/v1/ultra/resolve":
            self.record("kukutool")
            return fake_response(request, 503, payload={"message": "contract refusal"})
        if request.url in {NATIVE_MEDIA, ULTRA_MEDIA} and request.method in {"HEAD", "GET"}:
            size = 60 * 1024 * 1024 if request.url == ULTRA_MEDIA else 8 * 1024 * 1024
            if request.headers.get("Range"):
                return fake_response(request, 206, headers={
                    "Content-Type": "video/mp4", "Content-Range": f"bytes 0-0/{size}"})
            return fake_response(request, headers={
                "Content-Type": "video/mp4", "Content-Length": str(size)})
        raise AssertionError(f"Unexpected outbound HTTP request: {request.method} {request.url}")

    def wait_quiet(self, quiet=0.4, limit=10):
        # Native keeps running after a response; keep the boundary until it is done.
        self.release_ultra.set()
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            with self.lock:
                idle = not self.in_flight and time.monotonic() - self.last_activity >= quiet
            if idle:
                return
            time.sleep(0.05)
        raise AssertionError("Upstream work did not settle after the contract")


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    CACHES=LOCAL_CACHE,
    ULTRA_HD_PROVIDER_ENABLED=True,
    ULTRA_HD_PUBLIC_ACCESS_ENABLED=True,
    KUKUTOOL_PROVIDER_ENABLED=True,
    KUKUTOOL_SIDECAR_BASE_URL=SIDECAR,
    DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0,
    DOUYIN_ULTRA_PROVIDER_ORDER="tiantianjiexi,kukutool,native",
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="inv01-contract-fixture",
    TIANTIANJIEXI_PASSWORD="inv01-contract-fixture",
    TIANTIANJIEXI_API_BASE_URL=SERVICE,
    TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
)
class DouyinUltraHTTPContracts(TransactionTestCase):
    # Provider threads read admin settings on their own connections.
    def setUp(self):
        self.assertFalse((Path(settings.BASE_DIR) / ".env").exists(),
                         "Run contracts only in a checkout without .env")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        ProviderRuntimeSetting.objects.create(
            pk=1, douyin_ultra_mode=ProviderAccessMode.PUBLIC,
            douyin_ultra_rollout_percent=100,
            douyin_ultra_total_timeout_seconds=BUDGET,
        )
        self.transport = UpstreamTransport()
        self.stack.enter_context(offline_requests(self.transport.send))
        lock_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root",
            return_value=lock_root))
        self.stack.enter_context(patch.dict(
            "xhs_download.services.platforms.douyin_kukutool.KUKUTOOL_RUNTIME_STATE",
            checked=True, available=False, node_binary="", error="contract: no local runtime"))
        # Runs before the boundary closes (cleanups are last-in, first-out).
        self.addCleanup(self.transport.wait_quiet)

    def parse(self):
        self.transport.started = started = time.monotonic()
        response = self.client.get("/results/", {"q": SOURCE, "ultra_hd": "1"},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content[:1000])
        return response.json(), time.monotonic() - started

    def poll_until_settled(self, token, limit=6):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            response = self.client.post("/media/douyin-continuation/",
                                        {"continuation_token": token},
                                        content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content[:1000])
            body = response.json()
            if body.get("status") not in {"pending", "continuation_pending"}:
                return body
            time.sleep(0.2)
        self.fail("INV-01: the continuation never settled")

    def test_inv01_preferred_ultra_result_within_budget_is_shown_directly(self):
        body, elapsed = self.parse()
        self.assertNotEqual(body.get("status"), "continuation_pending", body)
        self.assertTrue(body["has_results"])
        self.assertIn(ULTRA_TITLE, body["html"])
        self.assertNotIn(NATIVE_TITLE, body["html"])
        self.assertLess(elapsed, BUDGET, "INV-01: an ultra result inside the budget is shown at once")
        self.assertEqual(self.transport.services(), ["tiantianjiexi"],
                         "INV-01: after an ultra result the other upstream work ends")

    def test_inv01_budget_expiry_shows_native_then_late_ultra_replaces_it(self):
        self.transport.ultra_waits = True
        body, elapsed = self.parse()
        native_at = self.transport.asked_at("native")
        self.assertIsNotNone(native_at, "INV-01: Native must run")
        self.assertLess(native_at, 0.3, "INV-01: Native runs from the start of the parse")
        self.assertEqual(body["status"], "continuation_pending", body)
        self.assertTrue(body["has_results"])
        self.assertIn(NATIVE_TITLE, body["html"])
        self.assertNotIn(ULTRA_TITLE, body["html"])
        self.assertLess(elapsed, BUDGET + 0.4,
                        "INV-01: Native is shown when the admin budget ends, not after it")
        self.transport.release_ultra.set()
        settled = self.poll_until_settled(body["continuation_token"])
        self.assertEqual(settled["status"], "success", settled)
        self.assertIn(ULTRA_TITLE, settled["html"])

    def test_inv01_configured_provider_order_is_followed(self):
        with override_settings(DOUYIN_ULTRA_PROVIDER_ORDER="kukutool,tiantianjiexi,native"):
            body, _ = self.parse()
        self.assertEqual(self.transport.services()[:2], ["kukutool", "tiantianjiexi"],
                         "INV-01: the configured order decides which provider goes first")
        self.assertTrue(body["has_results"])
        self.assertIn(ULTRA_TITLE, body["html"])
