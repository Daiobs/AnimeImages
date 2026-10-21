import io
import json
import socket
from unittest.mock import patch

import requests
import urllib3
from django.core import signing
from django.test import SimpleTestCase

from .services.common import XhsDownloadError
from .services.downloads import fetch_media_response, probe_media_prefix
from .services.provider_manifest import PROVIDER_MANIFEST_SALT, build_provider_manifest_token


class CountedBody(io.BytesIO):
    def __init__(self, body, *, fail_after=None):
        super().__init__(body)
        self.bytes_read = 0
        self.read_sizes = []
        self.fail_after = fail_after

    def read(self, size=-1):
        if size <= 0 or size > 1024:
            raise AssertionError("Unbounded media body read")
        self.read_sizes.append(size)
        if self.fail_after is not None and self.bytes_read >= self.fail_after:
            raise socket.timeout("signed URL and Cookie must not escape")
        data = super().read(size)
        self.bytes_read += len(data)
        if self.bytes_read > 1024:
            raise AssertionError("Media body budget exceeded")
        return data


class PrefixResponse(requests.Response):
    def __init__(self):
        super().__init__()
        self.was_closed = False

    @property
    def content(self):
        raise AssertionError("Full response content read")

    @property
    def text(self):
        raise AssertionError("Full response text read")

    def json(self, **kwargs):
        raise AssertionError("Full response JSON read")

    def iter_content(self, *args, **kwargs):
        raise AssertionError("Chunk iterator can overread the media budget")

    def close(self):
        self.was_closed = True
        super().close()


class DownloadSmokeTransportTests(SimpleTestCase):
    initial = "https://first.bilivideo.com/video.mp4?signature=private-token"
    final = "https://last.bilivideo.com/video.mp4?signature=other-private-token"
    signed_url = "https://p1.a.yximgs.com/cover.jpg?signature=private-token"

    def setUp(self):
        self.routes = {}
        self.sent = []
        self.responses = []
        self.bodies = []
        self.dns_calls = []
        self.dns_error = None
        self.enterContext(patch("requests.adapters.HTTPAdapter.send", self.send))
        self.enterContext(patch("socket.getaddrinfo", self.resolve))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))

    def tearDown(self):
        self.assertTrue(all(response.was_closed for response in self.responses))
        self.assertTrue(all(body.closed for body in self.bodies))
        self.assertLessEqual(sum(body.bytes_read for body in self.bodies), 1024)

    def resolve(self, host, port, *args, **kwargs):
        self.dns_calls.append((host, port))
        if self.dns_error is not None:
            raise self.dns_error
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    def send(self, request, **kwargs):
        self.sent.append((request, kwargs))
        route = self.routes[request.url]
        if isinstance(route, Exception):
            raise route
        status, headers, body = route
        response = PrefixResponse()
        response.status_code = status
        response.headers.update(headers)
        response.url = request.url
        response.request = request
        body = body if isinstance(body, CountedBody) else CountedBody(body)
        response.raw = urllib3.response.HTTPResponse(
            body=body, headers=headers, status=status, preload_content=False,
            decode_content=False,
        )
        self.bodies.append(body)
        self.responses.append(response)
        return response

    def media(self, url=None, *, status=206, headers=None, body=None):
        self.routes[url or self.initial] = (
            status,
            {"Content-Type": "video/mp4", "Content-Range": "bytes 0-1023/10000", **(headers or {})},
            body if body is not None else b"\x00\x00\x00\x18ftypisom" + b"x" * 20000,
        )

    def signed_item(self, url=None, **updates):
        item = {
            "type": "image", "url": url or self.signed_url,
            "provider": "kukutool_generic", "asset_id": "third_party:kuaishou:abc123:image:1",
            "provider_resource_id": "abc123", "provider_cache_key": "third_party:kukutool_generic:abc123",
            **updates,
        }
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"], provider=item["provider"],
            provider_resource_id=item["provider_resource_id"],
            provider_cache_key=item["provider_cache_key"], url=item["url"], media_type=item["type"],
        )
        return item

    def probe(self, item=None, **kwargs):
        result = probe_media_prefix(item or {"url": self.initial, "type": "video"}, **kwargs)
        self.assertEqual(set(result), {
            "status", "error_code", "http_status", "final_host", "final_port", "redirect_count",
            "referer_lost", "content_type", "range_supported", "bytes_received", "request_count",
        })
        self.assertEqual(result["request_count"], len(self.sent))
        self.assertEqual(result["bytes_received"], sum(body.bytes_read for body in self.bodies))
        serialized = json.dumps(result)
        for secret in ("private-token", "signature=", "Cookie", "https://", "Set-Cookie"):
            self.assertNotIn(secret, serialized)
        for request, options in self.sent:
            self.assertEqual(request.method, "GET")
            self.assertTrue(options["stream"])
            self.assertIsNot(options["verify"], False)
            self.assertLessEqual(options["timeout"], kwargs.get("timeout", 10))
        return result

    def test_206_reads_exact_prefix_once(self):
        self.media(headers={"Content-Type": "video/mp4; secret=private-token"})
        result = self.probe()
        self.assertEqual(result, {
            "status": "success", "error_code": "", "http_status": 206,
            "final_host": "first.bilivideo.com", "final_port": 443,
            "redirect_count": 0, "referer_lost": False, "content_type": "video/mp4",
            "range_supported": True, "bytes_received": 1024, "request_count": 1,
        })
        self.assertEqual(self.sent[0][0].headers["Range"], "bytes=0-1023")
        self.assertEqual(self.sent[0][0].headers["Host"], "first.bilivideo.com")
        self.assertEqual(self.dns_calls, [("first.bilivideo.com", 443)])

    def test_200_ignoring_range_still_reads_only_1024(self):
        self.media(status=200, headers={"Content-Length": "999999999", "Accept-Ranges": "bytes"})
        result = self.probe()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_received"], 1024)
        self.assertFalse(result["range_supported"])
        self.assertEqual(len(self.sent), 1)

    def test_custom_smaller_budget_changes_range_and_body_cap(self):
        self.media(status=200)
        result = self.probe(max_bytes=23)
        self.assertEqual(result["bytes_received"], 23)
        self.assertEqual(self.sent[0][0].headers["Range"], "bytes=0-22")

    def test_larger_requested_budget_is_still_capped_at_1024(self):
        self.media()
        result = self.probe(max_bytes=1000000)
        self.assertEqual(result["bytes_received"], 1024)
        self.assertEqual(self.sent[0][0].headers["Range"], "bytes=0-1023")

    def test_short_nonempty_media_prefix_is_success(self):
        self.media(status=200, body=b"media")
        result = self.probe()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_received"], 5)

    def test_redirect_chain_preserves_exact_counts_and_never_reads_redirect_bodies(self):
        middle = "https://first.bilivideo.com/next.mp4"
        self.routes[self.initial] = (302, {"Location": middle}, b"r" * 100000)
        self.routes[middle] = (307, {"Location": self.final}, b"r" * 100000)
        self.media(self.final)
        result = self.probe()
        self.assertEqual([r.url for r, _ in self.sent], [self.initial, middle, self.final])
        self.assertEqual(result["request_count"], 3)
        self.assertEqual(result["redirect_count"], 2)
        self.assertTrue(result["referer_lost"])
        self.assertEqual(result["final_host"], "last.bilivideo.com")
        self.assertEqual([body.bytes_read for body in self.bodies], [0, 0, 1024])
        self.assertIn("Referer", self.sent[1][0].headers)
        self.assertNotIn("Referer", self.sent[2][0].headers)
        self.assertTrue(all(r.headers["Range"] == "bytes=0-1023" for r, _ in self.sent))

    def test_cross_origin_referer_loss_is_retained_on_403_without_retry(self):
        self.routes[self.initial] = (302, {"Location": self.final}, b"r" * 100000)
        self.routes[self.final] = (403, {"Content-Type": "text/html"}, b"private failure body")
        result = self.probe()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "HTTP_403")
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["content_type"], "text/html")
        self.assertTrue(result["referer_lost"])
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["bytes_received"], 0)

    def test_blocked_initial_port_sends_nothing(self):
        for port in (0, 8443):
            with self.subTest(port=port):
                result = self.probe({"url": f"https://first.bilivideo.com:{port}/video.mp4", "type": "video"})
                self.assertEqual(result["error_code"], "PORT_POLICY_DENIED")
                self.assertEqual(result["final_port"], port)
                self.assertIsNone(result["http_status"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.dns_calls, [])

    def test_blocked_redirect_port_preserves_last_status_and_denied_target(self):
        self.routes[self.initial] = (302, {"Location": "https://last.bilivideo.com:8443/file"}, b"x" * 10000)
        result = self.probe()
        self.assertEqual(result["error_code"], "PORT_POLICY_DENIED")
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(result["final_host"], "last.bilivideo.com")
        self.assertEqual(result["final_port"], 8443)
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["request_count"], 1)

    def test_timeout_counts_one_send_and_never_retries(self):
        self.routes[self.initial] = requests.ReadTimeout("https://secret.invalid/?Cookie=secret")
        result = self.probe(timeout=0.5)
        self.assertEqual(result["error_code"], "TIMEOUT")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["final_host"], "first.bilivideo.com")
        self.assertIsNone(result["http_status"])

    def test_partial_body_timeout_keeps_consumed_byte_count(self):
        self.media(body=CountedBody(b"x" * 2000, fail_after=31))
        result = self.probe()
        self.assertEqual(result["error_code"], "TIMEOUT")
        self.assertEqual(result["bytes_received"], 31)
        self.assertEqual(result["http_status"], 206)
        self.assertEqual(result["request_count"], 1)

    def test_temporary_dns_failure_is_not_retried(self):
        self.dns_error = socket.gaierror(socket.EAI_AGAIN, "temporary DNS")
        result = self.probe()
        self.assertEqual(result["error_code"], "DNS_ERROR")
        self.assertEqual(result["request_count"], 0)
        self.assertEqual(len(self.dns_calls), 1)

    def test_private_ip_redirect_is_rejected_before_send(self):
        self.routes[self.initial] = (302, {"Location": "https://127.0.0.1/private"}, b"unread")
        result = self.probe()
        self.assertEqual(result["error_code"], "URL_POLICY_DENIED")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["final_host"], "127.0.0.1")

    def test_redirect_loop_does_not_retry(self):
        self.routes[self.initial] = (302, {"Location": self.initial}, b"unread")
        result = self.probe()
        self.assertEqual(result["error_code"], "REDIRECT_LIMIT")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["redirect_count"], 1)

    def test_redirect_limit_counts_responses_not_history(self):
        for index in range(7):
            url = self.initial if index == 0 else f"https://first.bilivideo.com/{index}"
            self.routes[url] = (302, {"Location": f"https://first.bilivideo.com/{index + 1}"}, b"unread")
        result = self.probe()
        self.assertEqual(result["error_code"], "REDIRECT_LIMIT")
        self.assertEqual(result["request_count"], 6)
        self.assertEqual(result["redirect_count"], 6)
        self.assertFalse(result["referer_lost"])
        self.assertTrue(all(not r.history for r in self.responses))

    def test_declared_html_is_rejected_without_reading(self):
        self.media(headers={"Content-Type": "text/html"})
        result = self.probe()
        self.assertEqual(result["error_code"], "CONTENT_TYPE_MISMATCH")
        self.assertEqual(result["bytes_received"], 0)

    def test_html_disguised_as_media_is_not_success(self):
        self.media(body=b" \n<!DOCTYPE html><html>error</html>")
        result = self.probe()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "HTML_RESPONSE")

    def test_empty_body_is_not_success(self):
        self.media(body=b"")
        self.assertEqual(self.probe()["error_code"], "EMPTY_BODY")

    def test_encoded_body_is_not_decoded_or_overread(self):
        self.media(headers={"Content-Encoding": "gzip"})
        result = self.probe()
        self.assertEqual(result["error_code"], "UNSUPPORTED_CONTENT_ENCODING")
        self.assertEqual(result["bytes_received"], 0)

    def test_invalid_content_range_does_not_claim_support(self):
        for value in ("", "bytes 1-1024/2000", "bytes 0-1024/2000", "bytes 0-1023/20", "nonsense"):
            with self.subTest(content_range=value):
                self.media(headers={"Content-Range": value}, body=b"m")
                result = probe_media_prefix({"url": self.initial, "type": "video"})
                self.assertEqual(result["status"], "success")
                self.assertFalse(result["range_supported"])

    def test_signed_206_uses_manifest_and_existing_headers(self):
        self.media(self.signed_url, headers={"Content-Type": "image/jpeg"})
        result = self.probe(self.signed_item())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_received"], 1024)
        self.assertEqual(self.sent[0][0].headers["Referer"], "https://www.xiaohongshu.com/")

    def test_signed_redirect_retains_referer_and_does_not_consume_redirect_body(self):
        target = "https://p2.a.yximgs.com/cover.jpg"
        self.routes[self.signed_url] = (302, {"Location": target, "Set-Cookie": "secret=private-token"}, b"r" * 10000)
        self.routes[target] = (403, {"Content-Type": "text/html"}, b"unread")
        result = self.probe(self.signed_item())
        self.assertEqual(result["error_code"], "HTTP_403")
        self.assertFalse(result["referer_lost"])
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(self.sent[0][0].headers["Referer"], self.sent[1][0].headers["Referer"])
        self.assertNotIn("Cookie", self.sent[1][0].headers)

    def test_signed_redirect_port_is_rejected_without_second_send(self):
        self.routes[self.signed_url] = (302, {"Location": "https://p2.a.yximgs.com:8443/cover.jpg"}, b"unread")
        result = self.probe(self.signed_item())
        self.assertEqual(result["error_code"], "PORT_POLICY_DENIED")
        self.assertEqual(result["final_port"], 8443)
        self.assertEqual(result["http_status"], 302)
        self.assertEqual(result["request_count"], 1)

    def test_signed_redirect_outside_manifest_policy_is_rejected(self):
        self.routes[self.signed_url] = (302, {"Location": self.final}, b"unread")
        result = self.probe(self.signed_item())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["final_host"], "last.bilivideo.com")

    def test_signed_timeout_is_not_retried(self):
        self.routes[self.signed_url] = requests.ConnectTimeout("private-token")
        result = self.probe(self.signed_item())
        self.assertEqual(result["error_code"], "TIMEOUT")
        self.assertEqual(result["request_count"], 1)

    def test_recoverable_signed_provider_403_does_not_refresh_or_retry(self):
        url = "https://v6.douyinvod.com/video.mp4?signature=private-token"
        item = self.signed_item(
            url, type="video", provider="tiantianjiexi",
            asset_id="douyin:7647533902413173321:video:tiantianjiexi",
            provider_resource_id="7647533902413173321",
            provider_cache_key="douyin:tiantianjiexi:validated:7647533902413173321",
        )
        self.routes[url] = (403, {"Content-Type": "text/html"}, b"unread")
        result = self.probe(item)
        self.assertEqual(result["error_code"], "HTTP_403")
        self.assertEqual(result["request_count"], 1)

    def test_youtube_cover_404_does_not_try_fallback_candidates(self):
        url = "https://i.ytimg.com/vi/demo/maxresdefault.jpg"
        item = self.signed_item(
            url, provider="yt_dlp_merge", asset_id="yt_dlp_merge:youtube:abc123:video:1:h1080:cover",
            provider_cache_key="yt_dlp_merge:abc123:h1080",
        )
        self.routes[url] = (404, {}, b"unread")
        result = self.probe(item)
        self.assertEqual(result["error_code"], "HTTP_404")
        self.assertEqual(result["request_count"], 1)

    def test_invalid_manifest_cannot_fall_back_to_native_allowlisted_url(self):
        item = self.signed_item(
            self.initial, type="video", provider="tiantianjiexi",
            asset_id="douyin:123:video:tiantianjiexi", provider_resource_id="123",
            provider_cache_key="douyin:tiantianjiexi:validated:123",
        )
        item["provider_manifest_token"] = "invalid"
        result = self.probe(item)
        self.assertEqual(result["error_code"], "MANIFEST_REJECTED")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.dns_calls, [])

    def test_expired_manifest_cannot_fall_back_to_native_allowlisted_url(self):
        class ExpiredSigner(signing.TimestampSigner):
            def timestamp(self):
                return "1"

        item = self.signed_item(self.initial)
        payload = signing.loads(item["provider_manifest_token"], salt=PROVIDER_MANIFEST_SALT)
        item["provider_manifest_token"] = ExpiredSigner(salt=PROVIDER_MANIFEST_SALT).sign_object(payload)
        with self.assertRaises(signing.SignatureExpired):
            signing.loads(item["provider_manifest_token"], salt=PROVIDER_MANIFEST_SALT, max_age=3600)
        result = self.probe(item)
        self.assertEqual(result["error_code"], "MANIFEST_REJECTED")
        self.assertEqual(self.sent, [])

    def test_incomplete_provider_identity_is_rejected(self):
        for field in ("provider", "provider_resource_id", "provider_cache_key", "provider_manifest_token"):
            with self.subTest(field=field):
                result = self.probe({"url": self.initial, "type": "video", field: "bound"})
                self.assertEqual(result["error_code"], "MANIFEST_REJECTED")
        self.assertEqual(self.sent, [])

    def test_native_asset_identifier_does_not_require_provider_manifest(self):
        url = "https://ci.xhscdn.com/image.jpg"
        self.media(url, headers={"Content-Type": "image/jpeg"})
        result = self.probe({"url": url, "type": "image", "asset_id": "xhs:note:image:1"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["bytes_received"], 1024)
        self.assertEqual(result["request_count"], 1)

    def test_browser_only_upstream_descriptor_is_not_bypassed(self):
        result = self.probe({"url": self.initial, "type": "video", "asset_id": "upstream:asset"})
        self.assertEqual(result["error_code"], "MEDIA_POLICY_DENIED")
        self.assertEqual(self.sent, [])

    def test_existing_fetch_does_not_start_accepting_206_by_default(self):
        self.media()
        with self.assertRaises(XhsDownloadError):
            fetch_media_response(self.initial, retries=1)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("Range", self.sent[0][0].headers)

    def test_existing_signed_fetch_does_not_start_accepting_206_by_default(self):
        self.media(self.signed_url, headers={"Content-Type": "image/jpeg"})
        with self.assertRaises(XhsDownloadError):
            fetch_media_response(self.signed_url, provider_signed=self.signed_item(), retries=1)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("Range", self.sent[0][0].headers)
