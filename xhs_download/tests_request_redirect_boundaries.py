import io
import json
import os
import socket
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import urllib3
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from .services.common import (
    XhsDNSResolutionError, XhsDownloadError, XhsSecurityError, encode_url, request_get_with_retry,
    validate_public_download_url,
)
from .services.downloads import fetch_media_response
from .services.platforms.bilibili import api_get_json
from .services.platforms.douyin import DouyinAdapter, build_mobile_feed_url
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .views import proxy_image_view


class RecordedBody(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.close_count = 0
        self.read_count = 0

    def read(self, *args):
        self.read_count += 1
        return super().read(*args)

    def close(self):
        self.close_count += 1
        super().close()

    def release_conn(self):
        pass


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "request-redirect-boundary-tests",
}})
class BoundaryNetworkCase(SimpleTestCase):
    def setUp(self):
        self.sent = []
        self.responses = []
        self.routes = {}
        self.dns = {}
        self.now = 1000.0
        self.enterContext(patch("requests.adapters.HTTPAdapter.send", self.send))
        self.enterContext(patch("socket.getaddrinfo", side_effect=self.resolve))
        self.enterContext(patch("xhs_download.services.common.time.monotonic", side_effect=lambda: self.now))
        self.sleep = self.enterContext(patch("xhs_download.services.common.time.sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.now += seconds

    def resolve(self, host, port, *args, **kwargs):
        addresses = self.dns.get(host, ["8.8.8.8"])
        if isinstance(addresses, Exception):
            raise addresses
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
            for ip in addresses
        ]

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        route = self.routes[request.url]
        if isinstance(route, list):
            route = route.pop(0)
        if callable(route):
            route = route(request)
        if isinstance(route, Exception):
            raise route
        status, headers, body = route
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        response.raw = RecordedBody(body)
        message = Message()
        for key, value in headers.items():
            message[key] = value
        response.raw._original_response = SimpleNamespace(msg=message)
        response.close = Mock(wraps=response.close)
        self.responses.append(response)
        return response

    def urls(self):
        return [request.url for request, _ in self.sent]


class RequestRedirectBoundaryTests(BoundaryNetworkCase):
    def test_private_redirect_is_rejected_before_second_request(self):
        # Failed at base 35015f4: the adapter recorded the private second request.
        initial = "https://ci.xiaohongshu.com/image.jpg"
        forbidden = "http://127.0.0.1/private"
        self.routes[initial] = (302, {"Location": forbidden}, b"")
        self.routes[forbidden] = (200, {}, b"must not be requested")

        with self.assertRaises(XhsSecurityError):
            request_get_with_retry(
                initial,
                retries=1,
                stream=True,
                allowed_final_host_suffixes=("xiaohongshu.com",),
            )

        self.assertEqual([request.url for request, _ in self.sent], [initial])
        self.assertTrue(self.responses[0].raw.closed)

    def test_initial_target_validation_happens_before_send(self):
        for url in (
            "http://127.0.0.1/private", "http://[::1]/private",
            "https://localhost/", "https://a.localhost/", "ftp://public.example/file",
            "https://public.example:8080/", "https://public.example:bad/",
            "https://user:secret@public.example/", "https://public.example/a\nb",
            "https://[bad/",
        ):
            with self.subTest(url=url), self.assertRaises(XhsSecurityError):
                request_get_with_retry(url, retries=3)
        self.assertEqual(self.sent, [])
        self.sleep.assert_not_called()

    def test_public_hostname_with_nonpublic_dns_never_gets_sent(self):
        initial = "https://ci.xiaohongshu.com/start"
        target = "https://cdn.example.com/file"
        self.routes[initial] = (302, {"Location": target}, b"redirect body")
        for addresses in (
            ["10.0.0.2"], ["127.0.0.1"], ["169.254.1.1"], ["100.64.0.1"],
            ["::1"], ["fd00::1"], ["fe80::1"], ["::ffff:127.0.0.1"],
            ["8.8.8.8", "192.168.0.2"],
        ):
            with self.subTest(addresses=addresses):
                self.sent.clear()
                self.dns["cdn.example.com"] = addresses
                with self.assertRaises(XhsSecurityError):
                    request_get_with_retry(initial, retries=3)
                self.assertEqual(self.urls(), [initial])
                self.responses[-1].close.assert_called_once()
                self.assertEqual(self.responses[-1].raw.read_count, 0)
        self.sleep.assert_not_called()

    def test_dns_lookup_failure_fails_closed_without_retry(self):
        self.dns["public.example"] = socket.gaierror("fixture DNS failure")
        with self.assertRaises(XhsDNSResolutionError):
            request_get_with_retry("https://public.example/a", retries=3)
        self.assertEqual(self.sent, [])
        self.sleep.assert_not_called()

    def test_public_ipv6_dns_is_accepted(self):
        url = "https://public.example/image"
        self.dns["public.example"] = ["2606:4700:4700::1111"]
        self.routes[url] = (200, {}, b"image")
        result = request_get_with_retry(url, stream=True)
        self.assertEqual(self.urls(), [url])
        result.close()

    def test_shared_public_download_validator_rejects_cgnat_but_accepts_public(self):
        url = "https://public.example/image"
        self.dns["public.example"] = ["100.64.0.1"]
        with self.assertRaises(XhsSecurityError):
            validate_public_download_url(url)
        self.dns["public.example"] = ["8.8.8.8", "2606:4700:4700::1111"]
        self.assertEqual(validate_public_download_url(url), url)
        self.assertEqual(self.sent, [])

    def test_redirect_scheme_port_and_credentials_are_checked_before_send(self):
        initial = "https://ci.xiaohongshu.com/start"
        for target in (
            "ftp://public.example/file", "https://public.example:8080/file",
            "https://public.example:bad/file", "https://secret@public.example/file",
            "http://[::1]/file", "https://[bad/",
        ):
            with self.subTest(target=target):
                self.sent.clear()
                self.routes[initial] = (302, {"Location": target}, b"")
                with self.assertRaises(XhsSecurityError):
                    request_get_with_retry(initial, retries=3)
                self.assertEqual(self.urls(), [initial])
                self.responses[-1].close.assert_called_once()
        self.sleep.assert_not_called()

    def test_relative_redirect_statuses_and_live_final_stream(self):
        initial = "https://ci.xiaohongshu.com/start"
        final = "https://ci.xiaohongshu.com/image.jpg"
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.sent.clear()
                self.routes[initial] = (status, {"Location": "/image.jpg"}, b"not consumed")
                self.routes[final] = (200, {}, b"streamed image")
                result = request_get_with_retry(initial, stream=True)
                self.assertEqual(self.urls(), [initial, final])
                self.responses[-2].close.assert_called_once()
                self.assertEqual(self.responses[-2].raw.read_count, 0)
                self.assertEqual(result.raw.read_count, 0)
                result.close.assert_not_called()
                self.assertEqual(b"".join(result.iter_content(3)), b"streamed image")
                result.close()

    def test_final_allowlist_does_not_become_intermediate_allowlist(self):
        initial = "https://xhslink.com/a/token"
        intermediate = "https://public-relay.example/next"
        final = "https://www.xiaohongshu.com/explore/123"
        self.routes[initial] = (302, {"Location": intermediate}, b"")
        self.routes[intermediate] = (307, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"page")
        result = request_get_with_retry(initial, allowed_final_host_suffixes=("xiaohongshu.com",))
        self.assertEqual(self.urls(), [initial, intermediate, final])
        self.assertEqual(result.content, b"page")
        result.close()

    def test_rejected_final_host_is_closed_unread_and_not_retried(self):
        initial = "https://xhslink.com/a/token"
        final = "https://public-relay.example/not-a-page"
        self.routes[initial] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"not consumed")
        with self.assertRaises(XhsSecurityError):
            request_get_with_retry(initial, retries=3, allowed_final_host_suffixes=("xiaohongshu.com",))
        self.assertEqual(self.urls(), [initial, final])
        for response in self.responses:
            response.close.assert_called_once()
            self.assertEqual(response.raw.read_count, 0)
        self.sleep.assert_not_called()

    def test_missing_location_is_terminal(self):
        initial = "https://public.example/start"
        for location in (None, "", "  "):
            with self.subTest(location=location):
                self.sent.clear()
                self.routes[initial] = (302, {} if location is None else {"Location": location}, b"")
                with self.assertRaises(XhsDownloadError):
                    request_get_with_retry(initial, retries=3)
                self.assertEqual(self.urls(), [initial])
                self.responses[-1].close.assert_called_once()
        self.sleep.assert_not_called()

    def test_fragment_loop_does_not_resend_or_retry(self):
        initial = "https://public.example/start"
        self.routes[initial] = (302, {"Location": "#another-fragment"}, b"")
        with self.assertRaises(requests.TooManyRedirects):
            request_get_with_retry(initial, retries=3)
        self.assertEqual(self.urls(), [initial])
        self.responses[0].close.assert_called_once()
        self.sleep.assert_not_called()

    def test_hop_limit_is_finite_and_terminal(self):
        for index in range(4):
            self.routes[f"https://public.example/{index}"] = (302, {"Location": f"/{index + 1}"}, b"")
        with self.assertRaises(requests.TooManyRedirects):
            request_get_with_retry("https://public.example/0", retries=3, max_redirects=2)
        self.assertEqual(self.urls(), [f"https://public.example/{index}" for index in range(3)])
        for response in self.responses:
            response.close.assert_called_once()
        self.sleep.assert_not_called()

    def test_cross_origin_drops_sensitive_headers_and_cookie_jar(self):
        initial = "https://a.xiaohongshu.com/start"
        same = "https://a.xiaohongshu.com/next"
        final = "https://b.xiaohongshu.com/image"
        self.routes[initial] = (302, {"Location": same, "Set-Cookie": "server=secret; Domain=.xiaohongshu.com; Path=/"}, b"")
        self.routes[same] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"ok")
        headers = {
            "Authorization": "Bearer secret", "cOoKiE": "manual=secret",
            "Proxy-Authorization": "Basic secret", "X-Token": "secret",
            "Host": "a.xiaohongshu.com", "Referer": initial, "Origin": initial,
            "Range": "bytes=0-1023", "Accept": "image/*",
        }
        original = dict(headers)
        result = request_get_with_retry(initial, headers=headers, stream=True)
        same_headers = self.sent[1][0].headers
        self.assertEqual(same_headers["Authorization"], "Bearer secret")
        self.assertEqual(same_headers["Cookie"], "manual=secret")
        final_headers = self.sent[2][0].headers
        for key in ("Authorization", "Cookie", "Proxy-Authorization", "X-Token", "Referer", "Origin"):
            self.assertNotIn(key, final_headers)
        self.assertEqual(final_headers['Host'], 'b.xiaohongshu.com')
        self.assertEqual(final_headers["Range"], "bytes=0-1023")
        self.assertEqual(final_headers["Accept"], "image/*")
        self.assertEqual(headers, original)
        result.close()

    def test_same_origin_preserves_scoped_response_cookie(self):
        initial = "https://public.example/start"
        final = "https://public.example/next"
        self.routes[initial] = (302, {"Location": final, "Set-Cookie": "session=fixture; Path=/"}, b"")
        self.routes[final] = (200, {}, b"ok")
        result = request_get_with_retry(initial)
        self.assertEqual(self.sent[1][0].headers["Cookie"], "session=fixture")
        result.close()

    def test_cookie_jar_is_not_forwarded_to_sibling_domain(self):
        initial = "https://a.xiaohongshu.com/start"
        final = "https://b.xiaohongshu.com/end"
        self.routes[initial] = (302, {"Location": final, "Set-Cookie": "session=fixture; Domain=.xiaohongshu.com; Path=/"}, b"")
        self.routes[final] = (200, {}, b"ok")
        result = request_get_with_retry(initial)
        self.assertNotIn("Cookie", self.sent[1][0].headers)
        result.close()

    def test_https_downgrade_strips_credentials_even_on_same_host(self):
        initial, final = "https://public.example/start", "http://public.example/end"
        self.routes[initial] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"ok")
        result = request_get_with_retry(initial, headers={"Authorization": "secret", "Cookie": "secret=1"})
        self.assertNotIn("Authorization", self.sent[1][0].headers)
        self.assertNotIn("Cookie", self.sent[1][0].headers)
        result.close()

    def test_environment_ca_proxy_no_proxy_preserved_without_netrc(self):
        initial, final = "https://public.example/start", "https://bypass.example/end"
        self.routes[initial] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"ok")
        for ca_environment, expected_ca in (
            ({"REQUESTS_CA_BUNDLE": "/fixture/primary.pem", "CURL_CA_BUNDLE": "/fixture/fallback.pem"}, "/fixture/primary.pem"),
            ({"CURL_CA_BUNDLE": "/fixture/fallback.pem"}, "/fixture/fallback.pem"),
        ):
            with self.subTest(ca_environment=ca_environment):
                self.sent.clear()
                environment = {**ca_environment, "HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "bypass.example"}
                with patch.dict(os.environ, environment, clear=True), patch(
                    "requests.sessions.get_netrc_auth", side_effect=AssertionError("ambient credentials")
                ):
                    result = request_get_with_retry(initial)
                self.assertEqual(self.sent[0][1]["proxies"]["https"], environment["HTTPS_PROXY"])
                self.assertFalse(self.sent[1][1]["proxies"])
                for _, kwargs in self.sent:
                    self.assertEqual(kwargs["verify"], expected_ca)
                result.close()

    def test_network_error_has_finite_retry_and_no_final_sleep(self):
        url = "https://public.example/a"
        self.routes[url] = requests.ConnectionError("fixture failure")
        with self.assertRaises(requests.ConnectionError):
            request_get_with_retry(url, retries=3, sleep_seconds=0.25)
        self.assertEqual(self.urls(), [url] * 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.25, 0.5])

    def test_http_error_closes_unread_response_then_retries(self):
        url = "https://public.example/a"
        self.routes[url] = [(503, {}, b"not consumed"), (200, {}, b"ok")]
        result = request_get_with_retry(url, retries=2)
        self.responses[0].close.assert_called_once()
        self.assertEqual(self.responses[0].raw.read_count, 0)
        self.assertEqual(result.content, b"ok")
        result.close()

    def test_exception_attached_response_is_closed(self):
        url = "https://public.example/a"
        attached = requests.Response()
        attached.raw = RecordedBody(b"error")
        self.routes[url] = requests.ConnectionError("fixture error", response=attached)
        with self.assertRaises(requests.ConnectionError):
            request_get_with_retry(url, retries=1)
        self.assertTrue(attached.raw.closed)
        self.sleep.assert_not_called()

    def test_unexpected_body_error_closes_response_without_retry(self):
        url = "https://public.example/a"
        self.routes[url] = (200, {}, b"body")
        with patch.object(RecordedBody, "read", side_effect=ValueError("fixture body failure")):
            with self.assertRaises(ValueError):
                request_get_with_retry(url, retries=3)
        self.assertEqual(self.urls(), [url])
        self.responses[0].close.assert_called_once()
        self.sleep.assert_not_called()

    def test_buffered_body_deadline_closes_response_and_stops_retry(self):
        url = "https://public.example/a"
        self.routes[url] = (200, {}, b"a" * (128 * 1024))
        read = RecordedBody.read
        def slow_read(body, *args):
            self.advance(3)
            return read(body, *args)
        with patch.object(RecordedBody, "read", slow_read), self.assertRaises(requests.Timeout):
            request_get_with_retry(url, retries=3, deadline_monotonic=1005)
        self.assertEqual(self.urls(), [url])
        self.assertEqual(self.responses[0].raw.read_count, 2)
        self.responses[0].close.assert_called_once()
        self.sleep.assert_not_called()

    def test_deadline_shared_by_hops_and_late_response_closed(self):
        initial, final = "https://public.example/start", "https://public.example/end"
        def first(_request):
            self.advance(6)
            return 302, {"Location": final}, b""
        def last(_request):
            self.advance(5)
            return 200, {}, b"late"
        self.routes[initial], self.routes[final] = first, last
        with self.assertRaises(requests.Timeout):
            request_get_with_retry(initial, retries=3, timeout=20, deadline_monotonic=1010, stream=True)
        self.assertEqual(self.urls(), [initial, final])
        self.assertEqual([kwargs["timeout"] for _, kwargs in self.sent], [5, 2])
        for response in self.responses:
            response.close.assert_called_once()
        self.sleep.assert_not_called()

    def test_expired_deadline_does_not_resolve_or_send(self):
        with patch("socket.getaddrinfo") as dns, self.assertRaises(requests.Timeout):
            request_get_with_retry("https://public.example/a", deadline_monotonic=999)
        dns.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_dns_time_consumes_deadline_before_send(self):
        def slow_dns(*args, **kwargs):
            self.advance(6)
            return self.resolve(*args, **kwargs)
        with patch("socket.getaddrinfo", side_effect=slow_dns), self.assertRaises(requests.Timeout):
            request_get_with_retry("https://public.example/a", deadline_monotonic=1005)
        self.assertEqual(self.sent, [])

    def test_retry_does_not_reset_deadline_and_backoff_must_fit(self):
        url = "https://public.example/a"
        def failure(_request):
            self.advance(4)
            return 503, {}, b""
        self.routes[url] = failure
        with self.assertRaises(requests.Timeout):
            request_get_with_retry(url, retries=3, timeout=(8, 8), sleep_seconds=2, deadline_monotonic=1005)
        self.assertEqual(self.urls(), [url])
        self.assertEqual(self.sent[0][1]["timeout"], (2.5, 2.5))
        self.sleep.assert_not_called()

    def test_default_deadline_is_shared_across_hops(self):
        initial, final = "https://public.example/start", "https://public.example/end"
        def first(_request):
            self.advance(3)
            return 302, {"Location": final}, b""
        self.routes[initial] = first
        self.routes[final] = (200, {}, b"ok")
        result = request_get_with_retry(initial, retries=1, timeout=2)
        self.assertEqual([kwargs["timeout"] for _, kwargs in self.sent], [2, 0.5])
        result.close()


class RealAdapterStreamTests(SimpleTestCase):
    def test_final_stream_remains_readable_after_session_closes_real_adapters(self):
        payload = b"synthetic streamed payload"
        body = RecordedBody(payload)

        def local_response(pool, method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertFalse(kwargs["redirect"])
            self.assertFalse(kwargs["preload_content"])
            return urllib3.response.HTTPResponse(
                body=body, status=200, headers={"Content-Type": "image/png"},
                preload_content=False, pool=pool,
            )

        close_adapter = requests.adapters.HTTPAdapter.close
        with (
            patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]),
            patch("socket.socket.connect", side_effect=AssertionError("real network forbidden")),
            patch("urllib3.connectionpool.HTTPSConnectionPool.urlopen", autospec=True, side_effect=local_response) as transport,
            patch("requests.adapters.HTTPAdapter.close", autospec=True, side_effect=close_adapter) as closed,
        ):
            response = request_get_with_retry("https://public.example/image", stream=True)
            self.assertGreaterEqual(closed.call_count, 2)
            self.assertEqual(body.read_count, 0)
            self.assertFalse(body.closed)
            self.assertEqual(b"".join(response.iter_content(5)), payload)
            response.close()
            transport.assert_called_once()


class RedirectConsumerTests(BoundaryNetworkCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def proxy(self, url):
        request = RequestFactory().get("/media/proxy-image/", {"u": encode_url(url)})
        request.user = AnonymousUser()
        return proxy_image_view(request)

    def test_image_proxy_blocks_private_dns_redirect_at_network_boundary(self):
        initial, target = "https://ci.xiaohongshu.com/a.jpg", "https://a.xhscdn.com/private"
        self.routes[initial] = (302, {"Location": target}, b"")
        self.dns["a.xhscdn.com"] = ["192.168.0.2"]
        response = self.proxy(initial)
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(self.urls(), [initial])
        self.responses[0].close.assert_called_once()

    def test_image_proxy_serves_relative_redirect_image_and_closes_final(self):
        initial, final = "https://ci.xiaohongshu.com/a.jpg", "https://ci.xiaohongshu.com/b.jpg"
        content = b"\x89PNG\r\n\x1a\n" + b"fixture"
        self.routes[initial] = (302, {"Location": "/b.jpg"}, b"")
        self.routes[final] = (200, {"Content-Type": "image/png"}, content)
        response = self.proxy(initial)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response.content, content)
        self.assertEqual(self.urls(), [initial, final])
        for upstream in self.responses:
            upstream.close.assert_called_once()

    def test_media_consumer_preserves_initial_and_final_host_rules(self):
        with self.assertRaises(XhsSecurityError):
            fetch_media_response("https://not-media.example/a.jpg")
        self.assertEqual(self.sent, [])
        initial, final = "https://ci.xiaohongshu.com/a.jpg", "https://not-media.example/a.jpg"
        self.routes[initial] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {}, b"rejected final")
        with self.assertRaises(XhsSecurityError):
            fetch_media_response(initial)
        self.assertEqual(self.urls(), [initial, final])
        self.assertEqual(self.responses[-1].raw.read_count, 0)

    def test_media_consumer_returns_live_stream_through_public_intermediate(self):
        initial = "https://aweme.snssdk.com/aweme/v1/play/?video_id=fixture"
        intermediate, final = "https://public-relay.example/next", "https://v3.zjcdn.com/video.mp4"
        self.routes[initial] = (302, {"Location": intermediate}, b"")
        self.routes[intermediate] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {"Content-Type": "video/mp4"}, b"synthetic video bytes")
        response = fetch_media_response(initial)
        self.assertEqual(self.urls(), [initial, intermediate, final])
        self.assertEqual(response.raw.read_count, 0)
        response.close.assert_not_called()
        self.assertEqual(b"".join(response.iter_content(4)), b"synthetic video bytes")
        response.close()

    def test_xhs_shortlink_multihop_parses_page_and_closes_responses(self):
        initial, intermediate = "https://xhslink.com/a/fixture", "https://xhslink.com/next"
        final = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
        self.routes[initial] = (302, {"Location": "/next"}, b"")
        self.routes[intermediate] = (302, {"Location": final}, b"")
        self.routes[final] = (200, {"Content-Type": "text/html"}, b"<html><head><meta property='og:title' content='Fixture note'></head><body></body></html>")
        result = XiaohongshuAdapter().parse(initial)
        self.assertEqual(result.platform, "xiaohongshu")
        self.assertEqual(self.urls(), [initial, intermediate, final])
        for response in self.responses:
            response.close.assert_called_once()

    def test_xhs_page_rejects_private_redirect_without_retry(self):
        initial = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
        self.routes[initial] = (302, {"Location": "https://local-resolver.example/page"}, b"")
        self.dns["local-resolver.example"] = ["10.0.0.2"]
        with self.assertRaises(XhsSecurityError):
            XiaohongshuAdapter().parse(initial)
        self.assertEqual(self.urls(), [initial])

    def test_bilibili_api_consumes_json_after_relative_redirect(self):
        initial = "https://api.bilibili.com/x/web-interface/view?bvid=fixture"
        final = "https://api.bilibili.com/x/result"
        self.routes[initial] = (307, {"Location": "/x/result"}, b"")
        self.routes[final] = (200, {"Content-Type": "application/json"}, b'{"code":0,"data":{"title":"fixture"}}')
        self.assertEqual(api_get_json(initial.split("?")[0], {"bvid": "fixture"}), {"title": "fixture"})
        self.assertEqual(self.urls(), [initial, final])
        self.responses[-1].close.assert_called_once()

    def test_bilibili_api_rejects_private_dns_before_send(self):
        initial = "https://api.bilibili.com/x/web-interface/view"
        self.dns["api.bilibili.com"] = ["10.0.0.2"]
        with self.assertRaises(XhsSecurityError):
            api_get_json(initial, {})
        self.assertEqual(self.sent, [])

    def test_douyin_mobile_feed_consumes_json_and_closes(self):
        aweme_id = "1234567890"
        initial = build_mobile_feed_url(aweme_id)
        final = "https://aweme.snssdk.com/result"
        self.routes[initial] = (302, {"Location": "/result"}, b"")
        self.routes[final] = (200, {}, json.dumps({"aweme_list": [{"aweme_id": aweme_id, "desc": "fixture", "video": {}}]}).encode())
        result = DouyinAdapter().fetch_mobile_feed_aweme(aweme_id)
        self.assertEqual(result["aweme_id"], aweme_id)
        self.assertEqual(self.urls(), [initial, final])
        self.responses[-1].close.assert_called_once()

    def test_douyin_direct_page_records_security_rejection_without_second_send(self):
        initial = "https://www.douyin.com/discover"
        self.routes[initial] = (302, {"Location": "http://127.0.0.1/private"}, b"")
        context = DouyinAdapter().build_parse_context(initial)
        self.assertIsInstance(context.direct_error, XhsSecurityError)
        self.assertEqual(self.urls(), [initial])
