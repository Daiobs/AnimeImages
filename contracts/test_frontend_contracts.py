"""Public HTTP presentation/delivery contracts; no browser or provider is contacted."""

import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting
from contracts.network import offline_requests


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="contract-fixture",
    TIANTIANJIEXI_PASSWORD="not-a-real-credential",
    TIANTIANJIEXI_API_BASE_URL="https://provider.invalid/parse",
    THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO=("tiantianjiexi_generic",),
    KUKUTOOL_PROVIDER_ENABLED=False,
    YTDLP_PROVIDER_ENABLED=False,
    GALLERYDL_PROVIDER_ENABLED=False,
)
class FrontendHttpContracts(TransactionTestCase):
    # Attachment completion can close/reopen PostgreSQL connections between requests.
    def setUp(self):
        self.assertFalse((settings.BASE_DIR / ".env").exists(), "Run contracts without .env")
        cache.clear()
        self.addCleanup(cache.clear)
        ProviderRuntimeSetting.objects.update_or_create(
            pk=1,
            defaults={
                "third_party_mode": ProviderAccessMode.PUBLIC,
                "third_party_allowed_public_platforms": "tiktok",
            },
        )
        self.calls = []
        self.enterContext(offline_requests(self.send))

    def send(self, request, **kwargs):
        """Only replace the final Requests transport, after URL/auth/routing decisions."""
        self.calls.append((request.method, request.url, dict(request.headers)))
        parsed = urlsplit(request.url)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        if parsed.hostname == "provider.invalid":
            body = json.dumps({"code": 200, "data": {
                "title": "Contract short title",
                "videos": [{"streams": [
                    {"url": f"https://media.tiktokcdn.com/{height}.mp4", "quality": f"{height}P",
                     "qualityLabel": f"{height}P", "height": height, "ext": "mp4", "size": 10}
                    for height in (720, 1080)
                ]}],
            }}).encode()
            response.headers["Content-Type"] = "application/json"
        elif request.url == "https://www.bilibili.com/":
            body = b""
            response.headers["Content-Type"] = "text/html"
        elif parsed.path == "/x/web-interface/view":
            body = json.dumps({"code": 0, "data": {
                "bvid": "BV1xx411c7mD", "aid": 170001, "title": "Contract short title",
                "owner": {"name": ""}, "pages": [{"cid": 123, "page": 1}],
            }}).encode()
            response.headers["Content-Type"] = "application/json"
        elif parsed.path == "/x/player/playurl":
            body = json.dumps({"code": 0, "data": {
                "quality": 80, "accept_quality": [80], "accept_description": ["1080P"],
                "durl": [{"url": "https://media.bilivideo.com/resource", "size": 10}],
            }}).encode()
            response.headers["Content-Type"] = "application/json"
        elif parsed.hostname in {"media.bilivideo.com", "media.tiktokcdn.com"}:
            body = b"first-last"
            response.headers["Content-Type"] = "video/mp4"
        else:
            raise AssertionError(f"Unexpected outbound request: {request.method} {request.url}")
        response.headers["Content-Length"] = str(len(body))
        response.raw = io.BytesIO(body)
        response.raw._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda _: None))
        return response

    def result(self, source):
        response = self.client.get("/results/", {"q": source})
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, "html.parser")
        return response, soup

    def test_INV03_public_parse_defaults_to_highest_and_selection_binds_exact_resource(self):
        _, soup = self.result("https://www.tiktok.com/@fixture/video/1234567890123456789")
        root = soup.select_one("[data-ytdlp-picker]")
        self.assertIsNotNone(root, soup.get_text(" ", strip=True)[-2000:])
        radios = root.select("[data-picker-radio]")
        self.assertEqual(len(radios), 2)
        selected = root.select_one("[data-picker-radio][checked]")
        self.assertIn("1080", selected.parent.get_text())
        urls = []
        for radio in radios:
            response = self.client.post(root["data-selection-url"], {
                "candidate_id": radio["value"], "resource_token": radio["data-token"], "action": "download",
            }, content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content)
            item = response.json()["item"]
            self.assertEqual(item["candidate_id"], radio["value"])
            self.assertTrue(item["browser_direct_fetch_safe"])
            self.assertFalse(item["requires_merge"])
            self.assertTrue(item["filename"].startswith("Contract short title"))
            self.assertTrue(item["filename"].endswith(".mp4"))
            self.assertNotIn(".bin", item["filename"])
            urls.append(item["url"])
        self.assertEqual(set(urls), {
            "https://media.tiktokcdn.com/720.mp4", "https://media.tiktokcdn.com/1080.mp4",
        })
        self.assertNotEqual(radios[0]["data-token"], radios[1]["data-token"])
        denied = self.client.post(root["data-selection-url"], {
            "candidate_id": radios[0]["value"], "resource_token": radios[1]["data-token"], "action": "download",
        }, content_type="application/json")
        self.assertEqual(denied.status_code, 403)

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=True,
        KUKUTOOL_SIDECAR_BASE_URL="https://sidecar.invalid",
        DOUYIN_PROVIDER_ORDER=("tiantianjiexi", "native"),
    )
    def test_INV06_douyin_raw_caption_stays_out_of_nondefault_download_filename(self):
        title = "几分钟调出通透画面"
        description = (
            "先调整画面的整体曝光，再处理高光和阴影，让人物与背景之间保持自然的明暗层次。"
            "接着检查肤色和环境色，保留原本的光线方向，最后分别比较调整前后的细节。"
        )
        topics = ["摄影练习", "调色笔记"]
        caption = f"{title} {description} #摄影练习 #调色笔记"
        source = "https://www.douyin.com/video/7669858827638845160"
        media_root = "https://v3-dy-o.zjcdn.com/contract/"
        # Raw, anonymized service HTTP envelope, before normalization or caption projection.
        payload = {"code": 200, "data": {
            "title": caption,
            "cover": "https://p3.douyinpic.com/contract/cover.jpg",
            "videos": [{"streams": [
                {"url": media_root + "high.mp4", "qualityLabel": "1080P", "sizeInBytes": 3000},
                {"url": media_root + "low.mp4", "qualityLabel": "720P", "sizeInBytes": 2000},
            ]}],
        }}
        ProviderRuntimeSetting.objects.filter(pk=1).update(douyin_ultra_mode=ProviderAccessMode.PUBLIC)
        outbound = []

        def send_caption(request, **kwargs):
            if request.url == "https://sidecar.invalid/health":
                self.assertEqual(request.method, "GET")
                body = {"status": "ok", "contract_version": "douyin-source-resolver.v1",
                        "ultra_provider": {"status": "ok"}}
            else:
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url, "https://provider.invalid/parse")
                body = payload
            outbound.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers["Content-Type"] = "application/json"
            response.raw = io.BytesIO(json.dumps(body).encode())
            return response

        with patch("requests.adapters.HTTPAdapter.send", side_effect=send_caption):
            page = self.client.get("/results/", {"q": source, "ultra_hd": "1"})
            self.assertEqual(page.status_code, 200)
            soup = BeautifulSoup(page.content, "html.parser")
            heading = soup.select_one(".result-title")
            self.assertIsNotNone(heading, soup.get_text(" ", strip=True)[-2000:])
            self.assertEqual(" ".join(heading.get_text().split()), f"视频笔记 {title}")
            self.assertEqual(soup.select_one("[data-base-filename]")["data-base-filename"], title)
            details = soup.select_one("details[data-result-caption]")
            self.assertIsNotNone(details)
            self.assertFalse(details.has_attr("open"))
            self.assertEqual(details.select_one(".result-caption-description").get_text(strip=True), description)
            self.assertEqual([node.get_text(strip=True) for node in details.select("li")],
                             [f"#{topic}" for topic in topics])
            picker = soup.select_one("[data-ytdlp-picker]")
            self.assertIsNotNone(picker)
            default = picker.select_one("[data-picker-radio][checked]")
            alternatives = picker.select("[data-picker-radio]:not([checked])")
            self.assertEqual(len(alternatives), 1)
            alternate = alternatives[0]
            self.assertNotEqual(alternate["value"], default["value"])
            self.assertIn("720", alternate.parent.get_text())
            requests_before_selection = len(outbound)
            selected = self.client.post(picker["data-selection-url"], {
                "candidate_id": alternate["value"], "resource_token": alternate["data-token"],
                "action": "download",
            }, content_type="application/json")
            self.assertEqual(selected.status_code, 200, selected.content)
            item = selected.json()["item"]
            self.assertEqual(item["candidate_id"], alternate["value"])
            self.assertEqual(item["url"], media_root + "low.mp4")
            filename = item["filename"]
            self.assertTrue(filename.startswith(title + "_"), filename)
            self.assertTrue(filename.endswith(".mp4"), filename)
            self.assertLess(len(filename), len(caption))
            for excluded in (description[:12], "#", ".bin", *topics):
                self.assertNotIn(excluded, filename)
        self.assertEqual(len(outbound), requests_before_selection, "Selection must not reparse or fetch media")
        self.assertEqual(sum(request.method == "POST" for request in outbound), 1)

    def test_INV07_public_hides_technical_details_while_staff_keeps_them(self):
        source = "https://www.tiktok.com/@fixture/video/1234567890123456789"
        for role in ("anonymous", "member", "staff"):
            staff = role == "staff"
            if role != "anonymous":
                user = get_user_model().objects.create_user(f"contract-{role}", is_staff=staff)
                self.client.force_login(user)
            response, soup = self.result(source)
            root = soup.select_one("[data-ytdlp-picker]")
            self.assertIsNotNone(root, response.content[-2000:])
            candidates = [radio["value"] for radio in root.select("[data-picker-radio]")]
            self.assertTrue(candidates)
            for element in soup.select("script, style"):
                element.decompose()
            text = soup.get_text(" ", strip=True)
            technical = soup.select_one(".ytdlp-technical")
            self.assertEqual(technical is not None, staff)
            for candidate in candidates:
                self.assertNotIn(candidate, text)
            diagnostic_ids = {node["data-candidate-id"] for node in soup.select("[data-upstream-candidate]")}
            self.assertEqual(diagnostic_ids, set(candidates) if staff else set())
            self.assertEqual("tiantianjiexi_generic" in text, staff)

    def test_INV04_INV06_bilibili_attachment_contract_for_safari_and_chrome_user_agents(self):
        # UA coverage verifies server headers/bytes only, not real browser disk saving.
        user_agents = {
            "safari": "Mozilla/5.0 Version/18.0 Safari/605.1.15",
            "chrome": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36",
        }
        for browser, ua in user_agents.items():
            with self.subTest(browser=browser):
                cache.clear()
                _, soup = self.result("https://www.bilibili.com/video/BV1xx411c7mD")
                card = soup.select_one('.asset-card[data-type="video"]')
                self.assertIsNotNone(card, soup.get_text(" ", strip=True)[-1500:])
                page = soup.select_one(".xhs-download-page")
                payload = {
                    "media_items": [{"type": "video", "url": card["data-url"]}],
                    "platform_key": "bilibili",
                    "base_filename": page["data-base-filename"],
                }
                prepared = self.client.post("/media/download-prepare/", payload,
                                            content_type="application/json", HTTP_USER_AGENT=ua)
                self.assertEqual(prepared.status_code, 200, prepared.content)
                ticket = prepared.json()
                status = self.client.get(ticket["status_url"], HTTP_USER_AGENT=ua)
                self.assertEqual(status.json()["status"], "issued")
                stream = self.client.get(ticket["download_url"], HTTP_USER_AGENT=ua)
                self.assertEqual(stream.status_code, 200, getattr(stream, "content", b""))
                self.assertTrue(stream.streaming)
                self.assertEqual(stream["X-Frame-Options"], "SAMEORIGIN")
                self.assertEqual(stream["Content-Type"], "video/mp4")
                disposition = unquote(stream["Content-Disposition"])
                self.assertIn("attachment;", disposition)
                self.assertIn("Contract short title.mp4", disposition)
                self.assertNotIn(".bin", disposition)
                self.assertEqual(stream["Content-Length"], "10")
                self.assertNotEqual(self.client.get(ticket["status_url"]).json()["status"], "completed")
                self.assertEqual(b"".join(stream.streaming_content), b"first-last")
                self.assertEqual(self.client.get(ticket["status_url"]).json()["status"], "completed")
                stream.close()
                media_calls = [call for call in self.calls if "media.bilivideo.com" in call[1]]
                self.assertTrue(media_calls)
                self.assertIn("bilibili.com", media_calls[-1][2].get("Referer", ""))

    def attachment_ticket(self):
        _, soup = self.result("https://www.bilibili.com/video/BV1xx411c7mD")
        card = soup.select_one('.asset-card[data-type="video"]')
        response = self.client.post("/media/download-prepare/", {
            "media_items": [{"type": "video", "url": card["data-url"]}],
            "platform_key": "bilibili", "base_filename": "Contract short title",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_INV04_slow_attachment_keeps_status_without_renewing_download_authorization(self):
        clock = [time.time()]
        monotonic = [time.monotonic()]

        class SlowBody(io.BytesIO):
            def stream(self, amount, decode_content=True):
                for index in range(6):
                    clock[0] += 1 if index == 0 else 40
                    monotonic[0] += 1 if index == 0 else 40
                    yield b"media"

        def slow_send(request, **kwargs):
            response = self.send(request, **kwargs)
            if urlsplit(request.url).hostname == "media.bilivideo.com":
                response.raw = SlowBody()
                response.raw._connection = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda _: None))
                response.headers["Content-Length"] = "30"
            return response

        with patch("time.time", side_effect=lambda: clock[0]), \
                patch("time.monotonic", side_effect=lambda: monotonic[0]), \
                patch("requests.adapters.HTTPAdapter.send", side_effect=slow_send):
            ticket = self.attachment_ticket()
            stream = self.client.get(ticket["download_url"])
            self.assertEqual(stream.status_code, 200)
            chunks = []
            try:
                for chunk in stream.streaming_content:
                    chunks.append(chunk)
                    observed = self.client.get(ticket["status_url"])
                    self.assertEqual(observed.status_code, 200, observed.content)
                    self.assertEqual(observed.json()["status"], "streaming")
                self.assertEqual(b"".join(chunks), b"media" * 6)
                self.assertEqual(self.client.get(ticket["status_url"]).json()["status"], "completed")
                self.assertEqual(self.client.get(ticket["download_url"]).status_code, 410)
                self.assertEqual(self.client.get(ticket["status_url"], REMOTE_ADDR="127.0.0.2").status_code, 403)
                self.assertEqual(sum("media.bilivideo.com" in call[1] for call in self.calls), 1)
                clock[0] += 601
                self.assertEqual(self.client.get(ticket["status_url"]).status_code, 410)
            finally:
                stream.close()

    def test_INV04_downstream_close_is_not_an_upstream_failure(self):
        ticket = self.attachment_ticket()
        stream = self.client.get(ticket["download_url"])
        iterator = iter(stream.streaming_content)
        self.assertEqual(next(iterator), b"first-last")
        stream.close()
        observed = self.client.get(ticket["status_url"])
        self.assertEqual(observed.json()["status"], "failed")
        self.assertEqual(observed.json()["error_code"], "DOWNLOAD_CONNECTION_CLOSED")
        self.assertNotIn("上游", observed.json()["message"])
        self.assertNotIn("取消", observed.json()["message"])
        self.assertEqual(sum("media.bilivideo.com" in call[1] for call in self.calls), 1)
