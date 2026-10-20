"""Offline entrypoint integration through registry, providers and templates."""
from contextlib import ExitStack
from io import BytesIO
import json
import socket
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
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.urls import resolve, reverse

from download_console.models import DownloadRuntimeSetting
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .views import build_batch_result_row, build_search_results_context


SOURCE = "https://www.bilibili.com/video/BV1J4JX6PEMV/"
MEDIA = "http://upos-sz-mirrorcos.bilivideo.com/entry-video.mp4"
COVER = "https://i0.hdslb.com/entry-cover.jpg"
SERVICE = "https://service.example.invalid/parse"
PROVIDER = "tiantianjiexi_generic"
MP4 = (
    b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
    b"\x00\x00\x00\x0cmdat\x00\x00\x00\x00"
)


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
class BilibiliFallbackEntryTests(TransactionTestCase):
    # Closing a real stream emits request_finished and can close DB connections.
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
        real_getaddrinfo = socket.getaddrinfo

        def fixture_getaddrinfo(host, port, *args, **kwargs):
            # PostgreSQL reconnects after stream close; do not mock its DNS.
            if host in {"localhost", "127.0.0.1", "::1"}:
                return real_getaddrinfo(host, port, *args, **kwargs)
            return [(2, 1, 6, "", ("8.8.8.8", 443))]

        self.stack.enter_context(patch(
            "socket.getaddrinfo", side_effect=fixture_getaddrinfo))
        self.stack.enter_context(patch(
            "socket.socket.connect", side_effect=AssertionError("real network forbidden")))
        directory = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root",
            return_value=Path(directory)))
        self.events = []
        self.native_success = False
        self.native_incomplete = False
        self.native = self.stack.enter_context(patch(
            "xhs_download.services.common._SingleHopSession.request",
            side_effect=self.native_http))
        self.post = self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.requests.post",
            side_effect=self.service_post))
        self.media_get = self.stack.enter_context(patch(
            "xhs_download.services.downloads.requests.get",
            side_effect=self.media_http))

    def media_http(self, url, **kwargs):
        self.assertIn(url, {MEDIA, MEDIA.replace("entry-video", "entry-large")})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        self.assertGreater(kwargs["timeout"], 0)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers.update({
            "Content-Type": "video/mp4",
            "Content-Length": str(len(MP4)),
        })
        response.raw = BytesIO(MP4)
        response.raw._connection = SimpleNamespace(
            sock=SimpleNamespace(settimeout=lambda timeout: self.assertGreater(timeout, 0)))
        return response

    def native_http(self, method, url, **kwargs):
        self.assertEqual(method.upper(), "GET")
        self.assertEqual(urlsplit(url).hostname, "api.bilibili.com")
        self.assertFalse(kwargs["allow_redirects"])
        self.events.append("native")
        if not self.native_success and not self.native_incomplete:
            return json_response(url, {"code": -412}, status=412)
        if urlsplit(url).path == "/x/web-interface/view":
            data = {
                "bvid": "BV1J4JX6PEMV", "aid": 123, "cid": 456,
                "title": "Native entry title", "pic": COVER,
                "pages": [{"cid": 456, "page": 1, "part": "Part one"}],
            }
        elif urlsplit(url).path == "/x/player/playurl":
            data = {"quality": 80, "durl": [{"url": MEDIA, "size": 12345}]}
            if self.native_incomplete:
                data = {"quality": 80, "dash": {
                    "video": [{"id": 80, "height": 1080, "width": 1920,
                               "baseUrl": MEDIA.replace(".mp4", ".m4s")}],
                    "audio": [{"id": 30280, "baseUrl": MEDIA.replace(".mp4", "-audio.m4s")}],
                }}
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
        self.assertEqual(len(context["video_items"]), 1)
        expected_urls = {MEDIA, MEDIA.replace("entry-video", "entry-large")}
        self.assertEqual({item["url"] for item in context["video_items"]},
                         {MEDIA.replace("entry-video", "entry-large")})
        self.assertEqual({item["download_strategy"] for item in context["video_items"]},
                         {"server_attachment"})
        from .templatetags.ytdlp_picker import ytdlp_presentation
        presentation = ytdlp_presentation(
            context["upstream_candidates"], legacy_videos=context["video_items"])
        self.assertEqual(presentation["video_resource_count"], 1)
        self.assertEqual(presentation["legacy_video"]["url"],
                         MEDIA.replace("entry-video", "entry-large"))
        expected_events = ["native"] * (2 if self.native_incomplete else 1) + ["service"]
        self.assertEqual(self.events, expected_events)
        self.post.assert_called_once()
        self.assertEqual(self.native.call_count, expected_events.count("native"))
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
        self.assertEqual(len(soup.select('[data-picker-radio][data-can-download="false"]')), 2)
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
        self.assertEqual(self.events, expected_events, "Actions must not reparse or fetch media")
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

    def test_home_https_service_also_uses_its_existing_attachment_action(self):
        with patch(__name__ + ".MEDIA", MEDIA.replace("http:", "https:")):
            self.assert_service_components_and_actions("home")

    def test_batch_normal_native_412_reaches_service_picker_and_actions(self):
        self.assert_service_components_and_actions("batch")

    def test_home_dash_only_reaches_service_picker_and_actions(self):
        self.native_incomplete = True
        self.assert_service_components_and_actions("home")

    def test_batch_dash_only_reaches_service_picker_and_actions(self):
        self.native_incomplete = True
        self.assert_service_components_and_actions("batch")

    def test_dash_only_service_failure_preserves_specific_reason(self):
        self.native_incomplete = True
        self.post.side_effect = requests.Timeout("fixture")
        context, status = self.consume("home")
        self.assertEqual(status, 200)
        self.assertIn("分离轨媒体", context["error_message"])
        self.assertFalse(context.get("video_items"))
        self.post.assert_called_once()

    def test_dash_only_does_not_expand_anonymous_access(self):
        self.native_incomplete = True
        self.config.third_party_allowed_public_platforms = ""
        context, _ = self.consume("home")
        self.assertIn("分离轨媒体", context["error_message"])
        self.assertFalse(context.get("video_items"))
        self.post.assert_not_called()

    def test_home_native_success_never_starts_service(self):
        self.assert_native_without_service("home")

    def test_batch_normal_native_success_never_starts_service(self):
        self.assert_native_without_service("batch")

    def test_home_anonymous_without_allowlist_never_starts_service(self):
        self.assert_unauthorized_failure_without_service("home")

    def test_batch_normal_anonymous_without_allowlist_never_starts_service(self):
        self.assert_unauthorized_failure_without_service("batch")

    def selected_home_item(self):
        context, status = self.consume("home")
        self.assertEqual(status, 200, context)
        default_url = context["video_items"][0]["url"]
        selected = []
        for candidate in context["upstream_candidates"]:
            response = self.action(candidate, "legacy")
            self.assertEqual(response.status_code, 200, response.content)
            selected.append(response.json()["item"])
        item = next(item for item in selected if item["url"] != default_url)
        self.assertEqual(item["url"], MEDIA)
        self.assertEqual(item["source_platform"], "bilibili")
        self.assertEqual(item["selection_delivery"], "legacy_manifest")
        self.media_get.assert_not_called()
        return context, item

    def download_payload(self, context, item, mode="browser"):
        return {
            "media_items": [item],
            "base_filename": context["base_filename"],
            "platform_key": context["platform"],
            "download_mode": mode,
            "route_only": True,
        }

    def prepare_selected_item(self, payload):
        response = self.client.post(
            reverse("media_download_prepare"), payload,
            content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "issued")
        self.media_get.assert_not_called()
        return response.json()

    def test_home_nondefault_legacy_candidate_routes_prepares_and_streams_mp4(self):
        context, item = self.selected_home_item()
        for mode in ("browser", "auto"):
            with self.subTest(mode=mode):
                self.media_get.reset_mock()
                payload = self.download_payload(context, item, mode)
                route = self.client.post(
                    reverse("media_download_images_api"), payload,
                    content_type="application/json")
                self.assertEqual(route.status_code, 200, route.content)
                self.assertEqual(route.json()["route_type"], "proxy")
                self.assertEqual(
                    route.json()["download_strategy"], "server_single_attachment")
                self.assertTrue(route.json()["native_browser_download"])
                prepared = self.prepare_selected_item(payload)
                response = self.client.get(prepared["download_url"])
                self.assertEqual(
                    response.status_code, 200,
                    None if response.streaming else response.content)
                self.assertTrue(response.streaming)
                self.assertEqual(response["Content-Type"], "video/mp4")
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertIn(".mp4", response["Content-Disposition"])
                try:
                    self.assertEqual(b"".join(response.streaming_content), MP4)
                finally:
                    response.close()
                self.media_get.assert_called_once()
                self.assertEqual(self.media_get.call_args.args[0], item["url"])
                status = self.client.get(prepared["status_url"])
                self.assertEqual(status.status_code, 200, status.content)
                self.assertEqual(status.json()["status"], "completed")
                self.assertTrue(status.json()["stream_started"])
                self.assertEqual(status.json()["upstream_attempt_count"], 1)
        self.assertEqual(self.events, ["native", "service"])
        self.post.assert_called_once()

    def test_selected_home_item_rejects_tampering_before_route_or_prepare(self):
        context, item = self.selected_home_item()
        missing_platform = dict(item)
        missing_platform.pop("source_platform")
        variants = {
            "changed_url": {**item, "url": MEDIA.replace("entry-video", "entry-large")},
            "bad_manifest": {**item, "provider_manifest_token": "invalid"},
            "missing_platform": missing_platform,
            "wrong_platform": {**item, "source_platform": "douyin"},
        }
        for label, invalid in variants.items():
            for endpoint, mode in (
                ("media_download_images_api", "browser"),
                ("media_download_images_api", "auto"),
                ("media_download_prepare", "server_single"),
            ):
                with self.subTest(variant=label, endpoint=endpoint, mode=mode):
                    response = self.client.post(
                        reverse(endpoint), self.download_payload(context, invalid, mode),
                        content_type="application/json")
                    self.assertEqual(response.status_code, 400, response.content)
                    if endpoint == "media_download_prepare":
                        self.assertEqual(
                            response.json()["error_code"], "PROVIDER_MANIFEST_REJECTED")
                    else:
                        self.assertEqual(response.json(), {
                            "status": "error", "message": "Provider 签名资源未通过校验",
                        })
                    self.assertNotIn("download_url", response.json())
        self.media_get.assert_not_called()
        self.post.assert_called_once()

    def test_stream_revalidates_missing_or_wrong_cached_source_platform(self):
        from .services import download_handoff

        context, item = self.selected_home_item()
        for platform in (None, "douyin"):
            with self.subTest(source_platform=platform):
                prepared = self.prepare_selected_item(self.download_payload(context, item))
                token = resolve(prepared["download_url"]).kwargs["token"]
                ticket = download_handoff._load_signed_ticket(
                    token, salt=download_handoff.DOWNLOAD_HANDOFF_SALT)
                key = download_handoff.build_download_handoff_cache_key(ticket)
                record = cache.get(key)
                self.assertEqual(record["item"]["source_platform"], "bilibili")
                if platform is None:
                    record["item"].pop("source_platform")
                else:
                    record["item"]["source_platform"] = platform
                cache.set(key, record, timeout=60)
                response = self.client.get(prepared["download_url"])
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(
                    response.json()["error_code"], "PROVIDER_MANIFEST_REJECTED")
        self.media_get.assert_not_called()

    def test_public_service_permission_is_rechecked_at_route_prepare_and_stream(self):
        context, item = self.selected_home_item()
        payload = self.download_payload(context, item)
        prepared = self.prepare_selected_item(payload)
        self.config.third_party_allowed_public_platforms = ""
        for endpoint, mode in (
            ("media_download_images_api", "browser"),
            ("media_download_images_api", "auto"),
            ("media_download_prepare", "server_single"),
        ):
            with self.subTest(endpoint=endpoint, mode=mode):
                response = self.client.post(
                    reverse(endpoint), {**payload, "download_mode": mode},
                    content_type="application/json")
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()["error_code"], "PROVIDER_ACCESS_DENIED")
                self.assertNotIn("download_url", response.json())
        response = self.client.get(prepared["download_url"])
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error_code"], "PROVIDER_ACCESS_DENIED")
        self.media_get.assert_not_called()
        self.post.assert_called_once()
