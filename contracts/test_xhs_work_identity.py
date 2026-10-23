"""XHS work identity through public result and Batch HTTP controllers.

Only the final HTTP/DNS transport is substituted. Native parsing, admission,
generic service normalization, payload caching and result selection stay real.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
import requests

from contracts.network import offline_requests
from xhs_download.models import (
    BatchParseRuntimeSetting, ProviderAccessMode, ProviderRuntimeSetting,
)


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
OTHER_ID = "65b2c3d4e5f6789012abcdef"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
SHORT_URL = "https://xhslink.cn/o/contract-work"
SERVICE = "https://provider.contract.invalid/xhs-parse"
PROMO_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/site-promotion"
NATIVE_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/current-work"
NATIVE_VIDEO = "https://sns-video-bd.xhscdn.com/stream/current-work.mp4"
SERVICE_VIDEO = "https://sns-video-hw.xhscdn.com/stream/service-work.mp4"
SERVICE_WEBM = "https://sns-video-hw.xhscdn.com/stream/service-work.webm"
SHELL_TITLE = "Site promotion, not a work"
SHELL_AUTHOR = "Site promotion account"
WORK_TITLE = "Window light"


def note_html(*, note_id=NOTE_ID, media_type="image", title=WORK_TITLE, description=""):
    note = {"noteId": note_id, "type": "normal", "title": title,
            "desc": description, "user": {"nickname": "Work author"},
            "imageList": [{"urlDefault": NATIVE_IMAGE}]}
    if media_type == "video":
        note.update(type="video", imageList=[], video={"media": {"stream": {
            "h264": [{"masterUrl": NATIVE_VIDEO, "size": 12345}],
        }}})
    payload = {"note": {"currentNoteId": note_id,
                        "noteDetailMap": {note_id: {"note": note}}}}
    return "<script>window.__INITIAL_STATE__=" + json.dumps(payload) + ";</script>"


def shell_html(kind="all"):
    # No unavailable/login wording: HTTP 200 and valid CDN URLs are insufficient.
    fragments = {
        "meta": f'<meta property="og:image" content="{PROMO_IMAGE}">',
        "dom": f'<div class="swiper-slide"><img src="{PROMO_IMAGE}"></div>',
        "script": '<script>window.siteCampaign=' + json.dumps({"image": PROMO_IMAGE}) + ';</script>',
    }
    return (f'<html><head><meta property="og:title" content="{SHELL_TITLE}">'
            f'<meta name="author" content="{SHELL_AUTHOR}"></head><body>'
            + ("".join(fragments.values()) if kind == "all" else fragments[kind])
            + "</body></html>")


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                        "LOCATION": "xhs-work-identity-contracts"}},
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    KUKUTOOL_GENERIC_PROVIDER_ENABLED=False, KUKUTOOL_PROVIDER_ENABLED=False,
    ULTRA_HD_PROVIDER_ENABLED=False,
    TIANTIANJIEXI_PHONE="xhs-work-identity-contract", TIANTIANJIEXI_PASSWORD="contract",
    TIANTIANJIEXI_API_BASE_URL=SERVICE, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000, THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=10000,
)
class XhsWorkIdentityHTTPContracts(TransactionTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.runtime = ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="upstream:tiantianjiexi_generic",
            third_party_blocked_platforms="",
        )
        BatchParseRuntimeSetting.objects.create(
            pk=1, access_mode=ProviderAccessMode.PUBLIC, ip_interval_seconds=0,
            daily_ip_limit=1000,
        )
        self.html = shell_html()
        self.redirect_to = ""
        self.service_title = "Service work"
        self.service_description = ""
        self.service_media = SERVICE_VIDEO
        self.service_failure = ""
        self.provider_calls = []
        self.page_calls = []
        self.media_calls = []
        self.unexpected = []
        self.stack.enter_context(offline_requests(self.send))
        lock_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root", return_value=lock_root))
        self.addCleanup(lambda: self.assertEqual(self.unexpected, []))

    def send(self, request, **options):
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        response._content_consumed = True
        response.encoding = "utf-8"
        response._content = b""
        if request.url == SERVICE:
            self.assertEqual(request.method, "POST")
            self.provider_calls.append(request)
            if self.service_failure == "timeout":
                raise requests.Timeout("offline service timeout")
            if self.service_failure == "http":
                response.status_code = 503
                return response
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps({"code": 200, "data": {
                "title": self.service_title, "description": self.service_description,
                "author": "Service author",
                "videos": [{"streams": [{"url": self.service_media,
                    "qualityLabel": "1080P", "sizeInBytes": 12345}]}],
            }}).encode()
            return response
        if request.url in {SHORT_URL, NOTE_URL}:
            self.assertEqual(request.method, "GET")
            self.page_calls.append(request.url)
            if request.url == SHORT_URL and self.redirect_to:
                response.status_code = 302
                response.headers["Location"] = self.redirect_to
            else:
                response.headers["Content-Type"] = "text/html; charset=utf-8"
                response._content = self.html.encode()
            return response
        if request.url in {NATIVE_VIDEO, SERVICE_VIDEO, SERVICE_WEBM}:
            self.assertEqual(request.method, "HEAD")
            self.media_calls.append((request.method, request.url))
            response.headers.update({"Content-Type": "video/webm" if request.url.endswith(".webm") else "video/mp4",
                                     "Content-Length": "12345", "Content-Disposition": "inline"})
            return response
        self.unexpected.append((request.method, urlsplit(request.url).hostname))
        raise AssertionError("Unexpected external HTTP request")

    def forbid_service(self):
        self.runtime.third_party_allowed_public_platforms = ""
        self.runtime.save()

    def parse(self, entry="results", source=SHORT_URL):
        if entry == "batch":
            response = self.client.post(reverse("batch_parse"), {"links": source, "ultra_hd": "0"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["batch_error"])
            self.assertEqual(len(response.context["rows"]), 1)
            return response, response.context["rows"][0]
        response = self.client.get(reverse("results"), {"q": source, "ultra_hd": "0"})
        self.assertEqual(response.status_code, 200)
        return response, response.context

    def assert_failed(self, response, context, entry):
        for key in ("image_items", "video_items", "upstream_candidates"):
            self.assertEqual(len(context.get(key) or []), 0, key)
        if entry == "batch":
            self.assertEqual(context["status"], "failed")
            summary = response.context["batch_summary"]
            self.assertEqual((summary["success_count"], summary["failed_count"]), (0, 1))
            self.assertEqual((summary["image_count"], summary["video_count"]), (0, 0))
        else:
            self.assertTrue(context["error_message"])
        self.assertFalse(BeautifulSoup(response.content, "html.parser").select("[data-picker-radio]"))
        self.assertNotIn(PROMO_IMAGE, response.content.decode())

    def assert_clean_service(self, response, context, entry):
        self.assertFalse(context.get("image_items"))
        self.assertFalse(context.get("video_items"))
        candidates = context["upstream_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["work_label"], self.service_title)
        radios = BeautifulSoup(response.content, "html.parser").select("[data-picker-radio]")
        self.assertEqual(len(radios), 1)
        for polluted in (SHELL_TITLE, SHELL_AUTHOR, PROMO_IMAGE):
            self.assertNotIn(polluted, response.content.decode())
            self.assertNotIn(polluted, str(context.get("base_filename", "")))
        action = self.client.post(reverse("service_selected_format"), {
            "candidate_id": radios[0]["value"], "resource_token": radios[0]["data-token"],
            "action": "download",
        }, content_type="application/json")
        self.assertEqual(action.status_code, 200, action.content)
        self.assertEqual(action.json()["item"]["url"], self.service_media)
        if entry == "batch":
            self.assertEqual(context["status"], "success")
            self.assertEqual(response.context["batch_summary"]["success_count"], 1)
            self.assertEqual(response.context["batch_summary"]["image_count"], 0)
        return action.json()["item"]

    def test_no_id_shell_meta_dom_and_script_each_fail_in_results_and_batch(self):
        self.forbid_service()
        for entry in ("results", "batch"):
            for kind in ("meta", "dom", "script", "all"):
                with self.subTest(entry=entry, media_source=kind):
                    self.html = shell_html(kind)
                    response, context = self.parse(entry)
                    self.assert_failed(response, context, entry)
        self.assertEqual(self.provider_calls, [])

    def test_invalid_native_uses_real_authorized_generic_service_without_pollution(self):
        for entry in ("results", "batch"):
            with self.subTest(entry=entry):
                cache.clear()
                response, context = self.parse(entry)
                self.assert_clean_service(response, context, entry)
        self.assertEqual(len(self.provider_calls), 2)

    def test_service_payload_cache_hit_stays_clean_without_another_provider_call(self):
        for entry in ("results", "batch"):
            with self.subTest(entry=entry):
                cache.clear()
                before = len(self.provider_calls)
                pages = len(self.page_calls)
                for _ in range(2):
                    response, context = self.parse(entry)
                    self.assert_clean_service(response, context, entry)
                    self.assertEqual(len(self.provider_calls), before + 1)
                self.assertEqual(len(self.page_calls), pages + 2)

    def test_invalid_native_and_unadmitted_service_fail_without_provider_request(self):
        self.forbid_service()
        for entry in ("results", "batch"):
            with self.subTest(entry=entry):
                response, context = self.parse(entry)
                self.assert_failed(response, context, entry)
        self.assertEqual(self.provider_calls, [])

    def test_invalid_native_and_service_failure_do_not_fill_results_with_site_media(self):
        for entry in ("results", "batch"):
            for failure in ("http", "timeout"):
                with self.subTest(entry=entry, failure=failure):
                    cache.clear()
                    before = len(self.provider_calls)
                    self.service_failure = failure
                    response, context = self.parse(entry)
                    self.assert_failed(response, context, entry)
                    self.assertEqual(len(self.provider_calls), before + 1)

    def test_valid_native_video_survives_service_http_failure_and_timeout(self):
        self.html = note_html(media_type="video")
        for entry in ("results", "batch"):
            for failure in ("http", "timeout"):
                with self.subTest(entry=entry, failure=failure):
                    cache.clear()
                    self.service_failure = failure
                    before = len(self.provider_calls)
                    response, context = self.parse(entry, NOTE_URL)
                    self.assertEqual(len(context["video_items"]), 1)
                    self.assertEqual(context["video_items"][0]["video_url"], NATIVE_VIDEO)
                    self.assertFalse(context.get("upstream_candidates"))
                    self.assertContains(response, WORK_TITLE)
                    self.assertEqual(len(self.provider_calls), before + 1)
                    if entry == "batch":
                        self.assertEqual(context["status"], "success")

    def test_valid_short_redirect_and_unique_current_work_without_url_id_remain_supported(self):
        self.forbid_service()
        self.html = note_html()
        for entry in ("results", "batch"):
            for redirect in ("", NOTE_URL):
                with self.subTest(entry=entry, redirect=bool(redirect)):
                    self.redirect_to = redirect
                    response, context = self.parse(entry)
                    self.assertEqual(len(context["image_items"]), 1)
                    self.assertContains(response, "current-work")
                    self.assertContains(response, WORK_TITLE)
                    if entry == "batch":
                        self.assertEqual(context["status"], "success")
        self.assertIn(NOTE_URL, self.page_calls)
        self.assertEqual(self.provider_calls, [])

    def test_canonical_work_identity_preserves_restricted_meta_and_current_dom_fallback(self):
        self.forbid_service()
        for entry in ("results", "batch"):
            for media in (f'<meta property="og:image" content="{NATIVE_IMAGE}">',
                          f'<div id="noteContainer"><div class="swiper-slide"><img src="{NATIVE_IMAGE}"></div></div>'):
                with self.subTest(entry=entry, media=media):
                    self.html = f'<link rel="canonical" href="{NOTE_URL}">' + media
                    response, context = self.parse(entry)
                    self.assertEqual(len(context["image_items"]), 1)
                    self.assertContains(response, "current-work")

    def test_explicit_requested_id_cannot_be_replaced_by_another_current_work(self):
        self.forbid_service()
        self.html = note_html(note_id=OTHER_ID)
        for entry in ("results", "batch"):
            with self.subTest(entry=entry):
                response, context = self.parse(entry, NOTE_URL)
                self.assert_failed(response, context, entry)

    def test_recommendation_and_conflicting_work_maps_cannot_supply_missing_identity(self):
        self.forbid_service()
        note = {"type": "normal", "title": "Recommended work",
                "imageList": [{"urlDefault": PROMO_IMAGE}]}
        for payload in ({"recommend": [{"noteId": NOTE_ID, **note}]},
                        {"note": {"noteDetailMap": {key: {"note": {"noteId": key, **note}}
                                                    for key in (NOTE_ID, OTHER_ID)}}}):
            self.html = "<script>window.__INITIAL_STATE__=" + json.dumps(payload) + ";</script>"
            for entry in ("results", "batch"):
                with self.subTest(entry=entry, payload=payload):
                    response, context = self.parse(entry)
                    self.assert_failed(response, context, entry)

    def test_inv06_native_card_filename_reaches_normal_download_action(self):
        self.forbid_service()
        caption = WORK_TITLE + "\n" + WORK_TITLE + "\nA quiet afternoon beside the window.\n#portrait #light"
        for entry in ("results", "batch"):
            for title in (WORK_TITLE, "", caption):
                with self.subTest(entry=entry, title=title):
                    self.html = note_html(media_type="video", title=title, description=caption)
                    response, context = self.parse(entry, NOTE_URL)
                    soup = BeautifulSoup(response.content, "html.parser")
                    card = soup.select_one(
                        '[data-type="video"][data-url][data-download-filename]')
                    self.assertIsNotNone(card)
                    filename = card["data-download-filename"]
                    if entry == "results" and not filename:
                        # The ordinary homepage consumer uses this page-level fallback.
                        filename = soup.select_one("[data-base-filename]")["data-base-filename"] + ".mp4"
                    self.assertEqual(filename.count(WORK_TITLE), 1)
                    self.assertNotIn("quiet afternoon", filename)
                    self.assertNotIn("#portrait", filename)
                    self.assertTrue(filename.endswith(".mp4"), filename)
                    item = {"type": "video", "url": card["data-url"],
                            "source_platform": "xiaohongshu", "size_bytes": 12345,
                            "filename": filename}
                    downloaded = self.client.post(reverse("download_images_api"), {
                        "media_items": [item], "download_mode": "browser",
                        "route_only": True, "intent": "download",
                    }, content_type="application/json")
                    self.assertEqual(downloaded.status_code, 200, downloaded.content)
                    prepared = downloaded.json()["items"][0]
                    self.assertEqual(prepared["filename"], filename)
                    self.assertEqual(prepared["url"], NATIVE_VIDEO)
                    self.assertEqual(prepared["download_strategy"], "browser_streaming")
        self.assertEqual(self.provider_calls, [])

    def test_inv06_service_action_short_title_preserves_mp4_webm_and_selection_authority(self):
        self.html = "<html><body></body></html>"
        caption = WORK_TITLE + "\n" + WORK_TITLE + "\nA quiet afternoon beside the window.\n#portrait #light"
        for entry in ("results", "batch"):
            for media in (SERVICE_VIDEO, SERVICE_WEBM):
                with self.subTest(entry=entry, media=media):
                    cache.clear()
                    self.service_title = caption
                    self.service_description = caption
                    self.service_media = media
                    before = len(self.provider_calls)
                    response, context = self.parse(entry)
                    self.assertFalse(context.get("image_items"))
                    radio = BeautifulSoup(response.content, "html.parser").select_one("[data-picker-radio]")
                    self.assertIsNotNone(radio)
                    candidate = next(row for row in context["upstream_candidates"]
                                     if row["candidate_id"] == radio["value"])
                    payload = {"candidate_id": radio["value"], "resource_token": radio["data-token"],
                               "action": "download"}
                    first = self.client.post(reverse("service_selected_format"), payload,
                                             content_type="application/json")
                    self.assertEqual(first.status_code, 200, first.content)
                    item = first.json()["item"]
                    suffix = candidate.get("format_id") or candidate["candidate_id"]
                    extension = media.rsplit(".", 1)[1]
                    self.assertEqual(item["filename"], f"{WORK_TITLE}_{suffix}.{extension}")
                    self.assertEqual(item["url"], media)
                    self.assertEqual(item["candidate_id"], radio["value"])
                    self.assertEqual(item["selected_format_id"], candidate.get("format_id") or "")
                    self.assertEqual(item["download_strategy"], "browser_streaming")
                    again = self.client.post(reverse("service_selected_format"), payload,
                                             content_type="application/json")
                    self.assertEqual(again.status_code, 200, again.content)
                    for key in ("filename", "url", "candidate_id", "selected_format_id", "selection_expires_at"):
                        self.assertEqual(again.json()["item"][key], item[key])
                    self.assertEqual(len(self.provider_calls), before + 1)
