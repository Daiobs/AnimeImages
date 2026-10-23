"""T8 HTTP contracts. Run in a checkout without .env.

    python manage.py test contracts.test_backend_contracts --verbosity 2

INV-08 requires nginx on PATH (or CONTRACT_NGINX_BINARY). All listeners use
ephemeral loopback ports; no production service or provider is contacted.
"""

from contextlib import ExitStack, contextmanager
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, LiveServerTestCase, TestCase, override_settings
from django.urls import get_resolver, reverse
from django.urls.resolvers import URLPattern, URLResolver

from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting
from contracts.network import local_address, offline_requests


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://www.bilibili.com/video/BV1J4JX6PEMV/"
MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/contract-video.mp4"
COVER = "https://i0.hdslb.com/contract-cover.jpg"
SERVICE = "https://provider.contract.invalid/parse"
NATIVE_TITLE = "Contract native video"
SERVICE_TITLE = "Contract fallback video"
ADMISSION = "bilibili:tiantianjiexi_fallback"
LOCAL_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "backend-contracts",
    }
}


def json_response(request, payload, status=200):
    response = requests.Response()
    response.status_code = status
    response.url = request.url
    response.request = request
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode()
    response._content_consumed = True
    return response


class OfflineTransport:
    """Replace only Requests' final adapter transport, not provider behavior."""

    def __init__(self):
        self.events = []
        self.bootstrap_calls = []
        self.sent_service_outcomes = []
        self.native_outcome = 412
        self.service_outcome = "success"
        self.native_size = 12345
        self.media_headers = {}
        self.media_calls = []

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        if request.url == "https://www.bilibili.com/":
            assert request.method == "GET", request.method
            self.bootstrap_calls.append(request.url)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers.update({"Content-Type": "text/html", "Content-Length": "0"})
            response._content = b""
            response._content_consumed = True
            return response
        if parsed.hostname == "api.bilibili.com":
            self.events.append("native")
            if self.native_outcome == "timeout":
                raise requests.Timeout("contract native timeout")
            if isinstance(self.native_outcome, int):
                return json_response(request, {"code": -self.native_outcome},
                                     status=self.native_outcome)
            if parsed.path == "/x/web-interface/view":
                data = {
                    "bvid": "BV1J4JX6PEMV", "aid": 123, "cid": 456,
                    "title": NATIVE_TITLE, "pic": COVER,
                    "pages": [{"cid": 456, "page": 1, "part": "Part one"}],
                }
            elif parsed.path == "/x/player/playurl":
                data = {"quality": 80, "durl": [{"url": MEDIA, "size": self.native_size}]}
                if self.native_outcome == "incomplete":
                    data = {"quality": 80, "dash": {
                        "video": [{"id": 80, "height": 1080, "width": 1920,
                                   "baseUrl": MEDIA.replace(".mp4", ".m4s")}],
                        "audio": [{"id": 30280,
                                   "baseUrl": MEDIA.replace(".mp4", "-audio.m4s")}],
                    }}
            else:
                raise AssertionError(f"Unexpected native HTTP endpoint: {request.url}")
            return json_response(request, {"code": 0, "data": data})
        if request.url == SERVICE:
            assert request.method == "POST", request.method
            body = request.body.decode() if isinstance(request.body, bytes) else request.body
            assert parse_qs(body)["link"] == [SOURCE], body
            self.events.append("tiantian")
            self.sent_service_outcomes.append(self.service_outcome)
            # Simulated sends only: timeout/disconnect cannot establish remote completion.
            if self.service_outcome == "timeout":
                raise requests.ReadTimeout("contract timeout after simulated POST send")
            if self.service_outcome == "http_failure":
                return json_response(request, {}, status=503)
            if self.service_outcome == "unknown":
                raise requests.ConnectionError(
                    "contract disconnect after simulated POST send; remote outcome unknown")
            return json_response(request, {"code": 200, "data": {
                "desc": SERVICE_TITLE, "cover": COVER,
                "videos": [{"streams": [
                    {"url": MEDIA, "quality": "1080P", "size": 12345},
                ]}],
            }})
        if request.url == MEDIA:
            self.media_calls.append(request.method)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers.update({'Content-Type': 'video/mp4', 'Content-Length': '12345', **self.media_headers})
            response._content = b''
            response._content_consumed = True
            return response
        raise AssertionError(f"Unexpected outbound HTTP request: {request.method} {request.url}")


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    CACHES=LOCAL_CACHE,
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="backend-contract-fixture",
    TIANTIANJIEXI_PASSWORD="backend-contract-fixture",
    TIANTIANJIEXI_API_BASE_URL=SERVICE,
    TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=1,
    THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=10000,
    DOWNLOAD_TRUST_X_FORWARDED_FOR=True,
    DOWNLOAD_TRUSTED_PROXY_CIDRS=("127.0.0.1/32",),
)
class BackendHTTPContracts(TestCase):
    def setUp(self):
        self.assertFalse((Path(settings.BASE_DIR) / ".env").exists(),
                         "Run contracts only in a checkout without .env")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.runtime = ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms=ADMISSION,
            third_party_blocked_platforms="",
        )
        self.transport = OfflineTransport()
        self.stack.enter_context(offline_requests(self.transport.send))
        lock_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        # The account lock is a filesystem boundary shared with daily processes.
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root",
            return_value=lock_root))

    def parse(self, client_ip="198.51.100.8", *, peer="127.0.0.1", **headers):
        return self.client.get(reverse("bilibili_results"), {"q": SOURCE},
                               REMOTE_ADDR=peer,
                               HTTP_X_FORWARDED_FOR=client_ip, **headers)

    def assert_service_result(self, response):
        self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertTrue(SERVICE_TITLE.encode() in response.content,
                        "Expected usable TianTian fallback in the public result HTML")
        self.assertIn("tiantian", self.transport.events)
        index = self.transport.events.index("tiantian")
        self.assertGreater(index, 0, "INV-05: Native must be attempted first")
        self.assertTrue(all(event == "native" for event in self.transport.events[:index]))
        self.assertEqual(self.transport.events[index:], ["tiantian"])

    def test_inv05_native_success_does_not_call_tiantian(self):
        self.transport.native_outcome = "success"
        response = self.parse()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, NATIVE_TITLE)
        self.assertNotContains(response, SERVICE_TITLE)
        self.assertGreater(len(self.transport.events), 0)
        self.assertEqual(set(self.transport.events), {"native"})

    def test_inv03_bilibili_complete_file_is_browser_first_for_native_and_service(self):
        for native_outcome in ("success", 412):
            with self.subTest(native_outcome=native_outcome):
                cache.clear()
                self.transport.events.clear()
                self.transport.native_outcome = native_outcome
                page = self.parse()
                self.assertEqual(page.status_code, 200)
                soup = BeautifulSoup(page.content, "html.parser")
                if native_outcome == "success":
                    card = soup.select_one('[data-type="video"][data-url]')
                    self.assertIsNotNone(card)
                    item = {"type": "video", "url": card["data-url"],
                            "source_platform": "bilibili"}
                    response = self.client.post(reverse("download_images_api"), {
                        "media_items": [item], "download_mode": "auto", "route_only": True,
                    }, content_type="application/json")
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()["route_type"], "direct")
                    item = response.json()["items"][0]
                else:
                    radio = soup.select_one('[data-picker-radio]')
                    self.assertIsNotNone(radio)
                    response = self.client.post(reverse("service_selected_format"), {
                        "candidate_id": radio["value"],
                        "resource_token": radio["data-token"], "action": "download",
                    }, content_type="application/json")
                    self.assertEqual(response.status_code, 200, response.content)
                    item = response.json()["item"]
                    self.assertEqual(item["source_platform"], "bilibili")
                    self.assertEqual(item["provider"], "tiantianjiexi_generic")
                    self.assertEqual(item["candidate_id"], radio["value"])
                    self.assertTrue(item["provider_manifest_token"])
                self.assertEqual(item["url"], MEDIA)
                self.assertTrue(item["browser_direct_fetch_safe"])
                self.assertEqual(item["download_strategy"], "browser_streaming")
                self.assertNotIn("server_single_attachment", str(item))

    def test_inv03_contextual_metadata_probe_does_not_enable_native_or_probe_again(self):
        self.transport.native_outcome = 'success'
        self.transport.native_size = 0
        self.transport.media_headers = {'Content-Disposition': 'attachment; filename="source.mp4"'}
        page = self.parse()
        self.assertEqual(page.status_code, 200)
        soup = BeautifulSoup(page.content, 'html.parser')
        card = soup.select_one('[data-type="video"][data-url]')
        self.assertIsNotNone(card)
        self.assertEqual(self.transport.media_calls, ['HEAD'])
        response = self.client.post(reverse('download_images_api'), {
            'media_items': [{'type': 'video', 'url': card['data-url'], 'source_platform': 'bilibili'}],
            'download_mode': 'auto', 'route_only': True,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['items'][0]
        self.assertEqual(item['download_strategy'], 'browser_streaming')
        self.assertNotIn('native_download_expires_at', item)
        self.assertEqual(self.transport.media_calls, ['HEAD'])
        self.assertEqual(item['url'], MEDIA)

    def test_inv06_public_action_uses_valid_source_filename_evidence_without_reprobe(self):
        from xhs_download.services.quality import QualityProbe

        self.transport.native_outcome = 'success'
        page = self.parse()
        self.assertEqual(page.status_code, 200)
        card = BeautifulSoup(page.content, 'html.parser').select_one('[data-type="video"][data-url]')
        self.assertIsNotNone(card)
        self.assertEqual(self.transport.media_calls, [])
        for disposition, expected in (
            ('attachment; filename="12345.mp4"', 'browser_native'),
            ("attachment; filename=wrong.bin; filename*=UTF-8''12345%2Emp4", 'browser_native'),
            ('attachment; filename="12345.bin"', 'browser_streaming'),
            ('attachment; filename="12345.webm"', 'browser_streaming'),
            ("attachment; filename=12345.mp4; filename*=UTF-8''wrong.bin", 'browser_streaming'),
            ('attachment', 'browser_streaming'),
            ("attachment; filename*=UTF-8''broken%GG.mp4", 'browser_streaming'),
        ):
            with self.subTest(disposition=disposition):
                self.transport.media_calls.clear()
                self.transport.media_headers = {'Content-Disposition': disposition}
                # Establish an existing context-free response fact through the
                # real probe, not a hand-written cache entry or patched helper.
                # The Bilibili page's own Referer probe is covered separately.
                result = QualityProbe().probe(card['data-url'], expected_type='video')
                self.assertTrue(result.reachable)
                self.assertEqual(self.transport.media_calls, ['HEAD'])
                response = self.client.post(reverse('download_images_api'), {
                    'media_items': [{'type': 'video', 'url': card['data-url'], 'source_platform': 'bilibili'}],
                    'download_mode': 'auto', 'route_only': True,
                }, content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                item = response.json()['items'][0]
                self.assertEqual(item['download_strategy'], expected)
                self.assertEqual(item['url'], MEDIA)
                if expected == 'browser_native':
                    self.assertGreater(item['native_download_expires_at'], time.time() * 1000)
                    self.assertEqual(item['fallback_strategy'], 'none')
                else:
                    self.assertNotIn('native_download_expires_at', item)
                self.assertEqual(self.transport.media_calls, ['HEAD'])

    def test_inv05_eligible_native_failures_use_admitted_tiantian(self):
        for outcome in (412, 429, 500, 503, 599, "timeout", "incomplete"):
            with self.subTest(native_outcome=outcome):
                cache.clear()
                self.transport.events.clear()
                self.transport.native_outcome = outcome
                self.assert_service_result(self.parse())

    def test_inv05_unadmitted_or_disabled_tiantian_is_never_called(self):
        for values in (
            {"third_party_allowed_public_platforms": ""},
            {"third_party_blocked_platforms": ADMISSION},
            {"third_party_mode": ProviderAccessMode.DISABLED},
        ):
            with self.subTest(configuration=values):
                self.runtime.third_party_mode = ProviderAccessMode.PUBLIC
                self.runtime.third_party_allowed_public_platforms = ADMISSION
                self.runtime.third_party_blocked_platforms = ""
                for key, value in values.items():
                    setattr(self.runtime, key, value)
                self.runtime.save()
                cache.clear()
                self.transport.events.clear()
                response = self.parse()
                self.assertFalse(SERVICE_TITLE.encode() in response.content,
                                 "INV-05: unadmitted fallback appeared in public HTML")
                self.assertIn("native", self.transport.events)
                self.assertNotIn("tiantian", self.transport.events,
                                 "INV-05: fallback requires current admission")

    def test_inv05_nonavailability_failure_does_not_call_tiantian(self):
        self.transport.native_outcome = 404
        response = self.parse()
        self.assertFalse(SERVICE_TITLE.encode() in response.content,
                         "INV-05: nonavailability failure must not trigger fallback")
        self.assertNotIn("tiantian", self.transport.events)

    @override_settings(TIANTIANJIEXI_CIRCUIT_FAILURE_THRESHOLD=1,
                       TIANTIANJIEXI_CIRCUIT_OPEN_SECONDS=3)
    def test_inv02_single_failure_timeout_unknown_recovers_without_admin_action(self):
        staff = get_user_model().objects.create_user(
            username="contract-operator", is_staff=True, is_active=True)
        operator = Client()
        operator.force_login(staff)
        for outcome in ("http_failure", "timeout", "unknown"):
            with self.subTest(provider_outcome=outcome):
                cache.clear()
                self.transport.events.clear()
                self.transport.sent_service_outcomes.clear()
                self.transport.service_outcome = outcome
                before = time.time()
                response = self.parse()
                failed_response_at = time.monotonic()
                failed_response_wall_time = time.time()
                self.assertFalse(SERVICE_TITLE.encode() in response.content,
                                 "Failed provider unexpectedly produced a public result")
                self.assertEqual(self.transport.sent_service_outcomes, [outcome],
                                 "INV-02: the failing public request must send exactly one POST")
                health = operator.get(reverse("provider_health"))
                self.assertEqual(health.status_code, 200)
                state = health.json()["third_party_tiantianjiexi"]
                self.assertTrue(state["enabled"],
                                "INV-02: one failure must not disable the provider")
                self.assertGreater(state["circuit"]["open_until"], before - 1)
                self.assertLessEqual(state["circuit"]["open_until"],
                                     failed_response_wall_time + 3,
                                     "INV-02: control plane must expose bounded cooldown")
                self.transport.service_outcome = "success"
                # Real expiry: do not clear cache or invoke an admin recovery API.
                time.sleep(max(
                    0,
                    state["circuit"]["open_until"] + 0.2 - time.time(),
                    failed_response_at + 1.2 - time.monotonic(),
                ))
                self.assertEqual(self.transport.sent_service_outcomes, [outcome],
                                 "INV-02: cooldown must not send another POST")
                self.transport.events.clear()
                self.assert_service_result(self.parse())
                self.assertEqual(self.transport.sent_service_outcomes, [outcome, "success"],
                                 "INV-02: recovery must issue one fresh successful POST")
                recovered = operator.get(reverse("provider_health")).json()
                self.assertFalse(recovered["third_party_tiantianjiexi"]["circuit"]["open"])

    def feedback(self, client_ip, *, peer="127.0.0.1", **headers):
        return self.client.post(
            reverse("user_feedback_submit"), {"message": "Contract feedback"},
            REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=client_ip, **headers)

    def test_inv09_quota_uses_rightmost_untrusted_client_not_forged_headers(self):
        for _ in range(5):
            self.assertEqual(self.feedback("198.51.100.8").status_code, 201)
        spoofed = self.feedback(
            "192.0.2.77, 198.51.100.8",
            HTTP_X_REAL_IP="192.0.2.88", HTTP_FORWARDED="for=192.0.2.99",
            HTTP_USER_AGENT="different-agent")
        self.assertEqual(spoofed.status_code, 429,
                         "INV-09: forged headers must not reset the real client's quota")
        self.assertGreater(int(spoofed["Retry-After"]), 0)
        self.assertEqual(self.feedback("198.51.100.9").status_code, 201,
                         "INV-09: separate clients must not share the proxy's quota")

    def test_inv09_untrusted_peer_cannot_choose_a_quota_by_supplying_xff(self):
        for index in range(5):
            self.assertEqual(self.feedback(f"192.0.2.{index + 1}",
                                          peer="203.0.113.8").status_code, 201)
        self.assertEqual(self.feedback("192.0.2.99", peer="203.0.113.8").status_code, 429)
        self.assertEqual(self.feedback("192.0.2.99", peer="203.0.113.9").status_code, 201)

    def test_inv09_anonymous_rollout_depends_on_client_not_proxy_or_spoofed_headers(self):
        self.runtime.third_party_mode = ProviderAccessMode.PUBLIC_GRAY
        self.runtime.third_party_rollout_percent = 50
        self.runtime.save()
        admitted = []
        for index in range(1, 17):
            real_ip = f"198.51.100.{index}"
            cache.clear()
            self.transport.events.clear()
            baseline = self.parse(real_ip)
            allowed = SERVICE_TITLE.encode() in baseline.content
            admitted.append(allowed)
            cache.clear()
            self.transport.events.clear()
            spoofed = self.parse(
                f"192.0.2.{index}, {real_ip}",
                HTTP_X_REAL_IP=f"192.0.2.{index + 30}",
                HTTP_FORWARDED=f"for=192.0.2.{index + 60}",
                HTTP_USER_AGENT=f"spoofed-agent-{index}")
            self.assertEqual(SERVICE_TITLE.encode() in spoofed.content, allowed,
                             "INV-09: anonymous rollout changed after forged headers")
            self.assertEqual("tiantian" in self.transport.events, allowed)
        self.assertEqual(set(admitted), {False, True},
                         "INV-09: distinct clients were collapsed into one proxy bucket")


class RecordingWSGIApplication:
    """Observe actual Django ingress without replacing its request handler."""

    requests = []

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        self.requests.append((environ["REQUEST_METHOD"], environ["PATH_INFO"],
                              environ.get("QUERY_STRING", "")))
        return self.application(environ, start_response)


def public_media_paths(patterns=None, prefix=""):
    paths = []
    for pattern in patterns or get_resolver().url_patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            paths.extend(public_media_paths(pattern.url_patterns, route))
        elif isinstance(pattern, URLPattern) and route.startswith("media/"):
            # Resolve every declared public media endpoint, including converters.
            kwargs = {key: "contract-missing-token" for key in pattern.pattern.converters}
            paths.append(reverse(pattern.name, kwargs=kwargs))
    return sorted(set(paths))


def ephemeral_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


@override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"],
                   CACHES=LOCAL_CACHE)
class MediaNginxHTTPContracts(LiveServerTestCase):
    static_handler = RecordingWSGIApplication

    def setUp(self):
        self.assertFalse((Path(settings.BASE_DIR) / ".env").exists(),
                         "Run contracts only in a checkout without .env")
        cache.clear()
        self.addCleanup(cache.clear)
        stack = ExitStack()
        self.addCleanup(stack.close)
        original_connect = socket.socket.connect

        def local_connect(sock, address):
            if not local_address(sock, address):
                raise AssertionError(f"External network forbidden: {address}")
            return original_connect(sock, address)

        stack.enter_context(patch("socket.socket.connect", local_connect))
        stack.enter_context(patch("requests.adapters.HTTPAdapter.send",
                                  side_effect=AssertionError("External HTTP forbidden")))

    def assert_media_request(self, port, method, path):
        RecordingWSGIApplication.requests.clear()
        status, _ = http_request(port, method, path + "?contract=reachability")
        self.assertEqual(
            RecordingWSGIApplication.requests,
            [(method, path, "contract=reachability")],
            f"INV-08: {method} {path} did not reach Django (HTTP {status})")
        self.assertLess(status, 500)

    def assert_media_ingress(self, port):
        paths = public_media_paths()
        self.assertGreaterEqual(len(paths), 15)
        for path in paths:
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    self.assert_media_request(port, method, path)

    def test_media_django_ingress_companion_not_nginx_acceptance(self):
        # Useful local evidence, deliberately separate from the nginx contract.
        self.assert_media_ingress(urlsplit(self.live_server_url).port)

    def test_inv08_every_public_media_route_reaches_real_django(self):
        with self.nginx_server() as port:
            self.assert_media_ingress(port)

    def test_inv08_countercheck_deleted_location_fails_real_http_ingress(self):
        path = "/media/service-selected-format/"
        with self.nginx_server(omitted_route=path) as port:
            for method in ("GET", "POST"):
                with self.subTest(method=method):
                    with self.assertRaisesRegex(AssertionError, "did not reach Django") as failure:
                        self.assert_media_request(port, method, path)
                    self.assertEqual(RecordingWSGIApplication.requests, [])
                    self.assertIn("HTTP 404", str(failure.exception))

    @contextmanager
    def nginx_server(self, omitted_route=None):
        nginx = os.environ.get("CONTRACT_NGINX_BINARY") or shutil.which("nginx")
        if not nginx:
            message = ("INV-08 requires an actual nginx executable; install nginx "
                       "or set CONTRACT_NGINX_BINARY (no static substitute)")
            self.assertFalse(os.environ.get("CI"), message)
            self.skipTest(message)
        helper_path = ROOT / "ops/nginx/tests/run_nginx_acceptance.py"
        spec = importlib.util.spec_from_file_location("contract_nginx_fixture", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        port, redirect_port = ephemeral_port(), ephemeral_port()
        upstream_port = urlsplit(self.live_server_url).port
        self.assertNotIn(8000, (port, redirect_port, upstream_port))
        helper.NGINX_PORT = port
        helper.REDIRECT_PORT = redirect_port

        with tempfile.TemporaryDirectory(prefix="backend-contract-nginx-") as directory:
            prefix = Path(directory)
            prefix.chmod(0o755)
            static = prefix / "static"
            static.mkdir()
            snippet = prefix / "media.conf"
            snippet_text = helper.MEDIA_SNIPPET.read_text()
            if omitted_route:
                snippet_text = helper.without_exact_location(snippet_text, omitted_route)
            snippet.write_text(snippet_text.replace(
                "127.0.0.1:8000", f"127.0.0.1:{upstream_port}"))
            config = prefix / "nginx.conf"
            config.write_text(helper._nginx_config(prefix, snippet, static).replace(
                "127.0.0.1:8000", f"127.0.0.1:{upstream_port}"))
            check = subprocess.run([nginx, "-p", directory + "/", "-c", str(config), "-t"],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(check.returncode, 0, check.stderr)
            process = subprocess.Popen(
                [nginx, "-p", directory + "/", "-c", str(config), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(100):
                    self.assertIsNone(process.poll(), "isolated nginx exited early")
                    try:
                        if http_request(port, "GET", "/health/live/")[0] == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Isolated nginx did not become ready")
                yield port
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
