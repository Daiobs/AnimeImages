import io
import socket
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.common import (
    MEDIA_HOST_SUFFIXES, XhsSecurityError, _PinnedAddressAdapter,
    validate_external_url, validate_media_url,
)
from .services.download_handoff import sanitize_download_handoff_item
from .services.downloads import fetch_media_response, get_media_headers
from .services.platforms.bilibili import BILIBILI_HEADERS, BILIBILI_MEDIA_HOST_SUFFIXES
from .services.platforms.bilibili_service_result import validate_media_url as validate_bilibili_media_url
from .services.quality import QualityProbe


MEDIA = "https://upos-hz-mirrorakam.akamaized.net/video.mp4"
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
REJECTED_URLS = (
    "https://hz-mirrorakam.akamaized.net/video.mp4",
    "https://evil-upos-hz-mirrorakam.akamaized.net/video.mp4",
    "https://upos-hz-mirrorakam.akamaized.net.evil.com/video.mp4",
    "https://arbitrary.akamaized.net/video.mp4",
    "https://mirrorakam.akamaized.net/video.mp4",
    "https://akamaized.net/video.mp4",
    "https://upos--mirrorakam.akamaized.net/video.mp4",
    "https://upos-hz.x-mirrorakam.akamaized.net/video.mp4",
    "https://upos-hz_x-mirrorakam.akamaized.net/video.mp4",
    "https://upos-HZ-mirrorakam.akamaized.net/video.mp4",
    "https://upos-hz-mirrorakam.akamaized.net./video.mp4",
    "https://child.upos-hz-mirrorakam.akamaized.net/video.mp4",
    MEDIA.replace("https:", "http:"),
    MEDIA.replace(".net/", ".net:80/"),
    MEDIA.replace(".net/", ".net:444/"),
    MEDIA.replace(".net/", ".net:invalid/"),
    MEDIA.replace(".net/", ".net:65536/"),
    MEDIA.replace(".net/", ".net:/"),
    MEDIA.replace("https://", "https://user:pass@"),
    MEDIA.replace("https://", "https://@"),
    MEDIA.replace("https://", "https://evil.example@"),
    MEDIA.replace(".net/", ".net@evil.example/"),
    MEDIA.replace("upos-hz", "upos-hz\n"),
    MEDIA.replace("upos-hz", "upos-hz\t"),
    MEDIA.replace("/video", "\\evil/video"),
    "https://127.0.0.1/video.mp4",
    "https://[::1]/video.mp4",
)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class BilibiliAkamaiPolicyTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.dns = self.enterContext(patch("socket.getaddrinfo", return_value=PUBLIC_DNS))
        self.send = self.enterContext(patch("requests.adapters.HTTPAdapter.send", side_effect=self.response))

    @staticmethod
    def response(request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers.update({"Content-Type": "video/mp4", "Content-Length": "5"})
        response.raw = io.BytesIO(b"video")
        return response

    def test_both_validators_allow_exact_lowercase_labels_and_https_port(self):
        for label in ("hz", "sz", "a", "hz-1", "0", "-"):
            for port in ("", ":443"):
                url = f"https://upos-{label}-mirrorakam.akamaized.net{port}/video.mp4?token=fixture"
                for validator in (validate_media_url, validate_bilibili_media_url):
                    with self.subTest(label=label, port=port, validator=validator.__module__):
                        self.assertEqual(validator(url), url)
        self.dns.assert_not_called()
        self.send.assert_not_called()

    def test_both_validators_reject_outside_narrow_policy(self):
        for url in REJECTED_URLS:
            for validator in (validate_media_url, validate_bilibili_media_url):
                with self.subTest(url=url, validator=validator.__module__), self.assertRaises(ValueError):
                    validator(url)
        self.dns.assert_not_called()
        self.send.assert_not_called()

    def test_existing_media_scopes_and_http_are_unchanged(self):
        for suffix in MEDIA_HOST_SUFFIXES:
            for scheme in ("http", "https"):
                url = f"{scheme}://media.{suffix}/video.mp4"
                self.assertEqual(validate_media_url(url), url)
                if suffix in BILIBILI_MEDIA_HOST_SUFFIXES:
                    self.assertEqual(validate_bilibili_media_url(url), url)
        for scope in (("xhscdn.com",), ("douyinvod.com",), ("bilibili.com",), ("evil.example",)):
            with self.subTest(scope=scope), self.assertRaises(XhsSecurityError):
                validate_external_url(MEDIA, scope)

    def test_bilibili_and_common_probe_scopes_allow_approved_host(self):
        for scope in (BILIBILI_MEDIA_HOST_SUFFIXES, MEDIA_HOST_SUFFIXES):
            for url in (MEDIA, MEDIA.replace(".net/", ".net:443/")):
                with self.subTest(scope=scope, url=url):
                    result = QualityProbe(scope).probe(url, allow_fallback=False)
                    self.assertTrue(result.reachable, result.error)
                    self.assertEqual(result.size_bytes, 5)
                    self.assertEqual(result.final_url, url)
        self.assertEqual(self.send.call_count, 4)

    def test_probe_rejects_unapproved_initial_urls_before_http(self):
        for url in REJECTED_URLS:
            with self.subTest(url=url):
                result = QualityProbe(BILIBILI_MEDIA_HOST_SUFFIXES).probe(url)
                self.assertTrue(result.security_rejected, result.error)
        self.send.assert_not_called()

    def test_existing_server_fetch_consumer_accepts_approved_url_and_final_host(self):
        response = fetch_media_response(MEDIA, retries=1)
        self.assertEqual(response.url, MEDIA)
        self.assertEqual(b"".join(response.iter_content()), b"video")
        response.close()
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.args[0].headers["Referer"], "https://www.bilibili.com/")

    def test_approved_handoff_keeps_bilibili_headers_and_blocks_separate_tracks(self):
        item = {"type": "video", "url": MEDIA}
        self.assertEqual(sanitize_download_handoff_item(item), item)
        self.assertEqual(get_media_headers(MEDIA), BILIBILI_HEADERS)
        for extension in (".m4s", ".MPD", ".m3u8"):
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                sanitize_download_handoff_item({**item, "url": MEDIA.replace(".mp4", extension)})
        self.send.assert_not_called()

    def test_public_dns_addresses_remain_pinned_with_original_tls_origin(self):
        adapter = _PinnedAddressAdapter(MEDIA, ("93.184.216.34", "8.8.8.8"))
        self.addCleanup(adapter.close)
        pool = adapter.get_connection_with_tls_context(
            requests.Request("GET", MEDIA).prepare(), verify=True, proxies={})
        self.assertEqual(pool.host, "93.184.216.34")
        self.assertEqual(pool.conn_kw["server_hostname"], "upos-hz-mirrorakam.akamaized.net")
        self.assertEqual(adapter.addresses, ("93.184.216.34", "8.8.8.8"))

    def test_private_mixed_and_nonpublic_dns_are_rejected_before_http(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "fc00::1", "0.0.0.0"):
            for public_first in (False, True):
                answers = PUBLIC_DNS[:1] if public_first else []
                self.dns.return_value = answers + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
                with self.subTest(address=address, public_first=public_first):
                    result = QualityProbe().probe(MEDIA)
                    self.assertTrue(result.security_rejected, result.error)
        self.send.assert_not_called()

    def test_redirect_to_approved_host_passes_both_scopes(self):
        start = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"

        def send(request, **kwargs):
            response = self.response(request)
            if request.url == start:
                response.status_code = 302
                response.headers["Location"] = MEDIA
            return response

        self.send.side_effect = send
        result = QualityProbe(BILIBILI_MEDIA_HOST_SUFFIXES).probe(start)
        self.assertTrue(result.reachable, result.error)
        self.assertEqual(result.final_url, MEDIA)
        self.assertEqual(self.send.call_count, 2)

    def test_redirects_do_not_expand_akamai_or_bypass_dns(self):
        for target in (
            "https://mirrorakam.akamaized.net/video.mp4", REJECTED_URLS[2], REJECTED_URLS[3],
            MEDIA.replace("https:", "http:"), MEDIA.replace(".net/", ".net:80/"),
            MEDIA.replace(".net/", ".net:444/"), "https://127.0.0.1/private",
        ):
            def send(request, **kwargs):
                response = self.response(request)
                response.status_code = 302
                response.headers["Location"] = target
                return response

            self.send.reset_mock()
            self.send.side_effect = send
            with self.subTest(target=target):
                result = QualityProbe(BILIBILI_MEDIA_HOST_SUFFIXES).probe(MEDIA)
                self.assertTrue(result.security_rejected, result.error)
            self.assertEqual(self.send.call_count, 1)

    def test_probe_rechecks_public_dns_at_redirect_before_sending(self):
        def send(request, **kwargs):
            response = self.response(request)
            response.status_code = 302
            response.headers["Location"] = MEDIA + "?next-hop"
            return response

        self.send.side_effect = send
        self.dns.side_effect = [PUBLIC_DNS, [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]]
        result = QualityProbe(BILIBILI_MEDIA_HOST_SUFFIXES).probe(MEDIA)
        self.assertTrue(result.security_rejected, result.error)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.dns.call_count, 2)
