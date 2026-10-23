import hashlib
import json
import time
from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from .services.douyin_continuation import (
    DOUYIN_CONTINUATION_MAX_CACHE_BYTES,
    DouyinContinuationCoordinator,
    build_douyin_continuation_cache_key,
    deserialize_douyin_continuation_result,
    issue_douyin_continuation_token,
    serialize_douyin_continuation_result,
)
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import DouyinParseContext, DouyinTianTianJieXiProvider
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer
from .views import build_result_display_context


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class DouyinGalleryContinuationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.images = [f"https://p3-sign.douyinpic.com/gallery/{i}.webp" for i in range(3)]
        self.provider = DouyinTianTianJieXiProvider(provider_settings=SimpleNamespace())

    def result(self, aweme_id="7634213610894689787", *, title="", description="Gallery fixture #photos"):
        payload = TianTianJieXiResultNormalizer.normalize_payload({
            "title": title, "desc": description, "images": self.images, "videos": [],
        })
        source = f"https://www.douyin.com/note/{aweme_id}"
        return self.provider.result_from_payload(
            DouyinAdapter(), DouyinParseContext(url=source, final_url=source, aweme_id=aweme_id),
            payload, [], allow_network=False,
        )

    def test_cached_gallery_keeps_every_signed_resource_and_title(self):
        original = self.result()
        restored = deserialize_douyin_continuation_result(serialize_douyin_continuation_result(original))
        self.assertEqual(restored.post_type, "image")
        self.assertEqual(restored.base_filename, original.base_filename)
        self.assertEqual(restored.metadata["title"], original.metadata["title"])
        self.assertEqual([asset.url for asset in restored.assets], self.images)
        self.assertEqual([asset.preview_url for asset in restored.assets], self.images)
        for before, after in zip(original.assets, restored.assets):
            self.assertEqual(after.to_download_item(), before.to_download_item())
            self.assertTrue(provider_manifest_allows_public_download(after.to_download_item()))

    def test_gallery_caption_is_unchanged_after_continuation(self):
        for title, description in (
            ("Gallery title", "Full gallery caption #photos"),
            ("", "Gallery fixture #photos"),
            ("Gallery title", ""),
        ):
            with self.subTest(title=title, description=description):
                original = self.result(title=title, description=description)
                restored = deserialize_douyin_continuation_result(
                    serialize_douyin_continuation_result(original)
                )
                before = build_result_display_context(original, probe_missing_video_sizes=False)
                after = build_result_display_context(restored, probe_missing_video_sizes=False)
                for key in ("display_title", "display_description", "display_topics", "base_filename"):
                    self.assertEqual(after[key], before[key], key)
                self.assertEqual(
                    [asset.to_download_item() for asset in restored.assets],
                    [asset.to_download_item() for asset in original.assets],
                )

    def test_continuation_description_is_bounded_without_changing_source(self):
        description = "\u56fe" * 2500
        for title in ("Gallery title", ""):
            with self.subTest(title=title):
                original = self.result(title=title, description=description)
                payload = serialize_douyin_continuation_result(original)
                restored = deserialize_douyin_continuation_result(payload)
                self.assertEqual(restored.metadata.get("description"), description[:2000])
                self.assertEqual(original.metadata["description"], description)
                self.assertLessEqual(
                    len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")),
                    DOUYIN_CONTINUATION_MAX_CACHE_BYTES,
                )

    def test_titleless_long_gallery_description_and_tail_topic_survive_continuation(self):
        details = "\u56fe\u6587\u6b63\u6587" * 150
        description = f"Gallery lead\n{details} #tailtopic"
        self.assertGreater(len(description), 200)
        self.assertLessEqual(len(description), 2000)
        original = self.result(description=description)
        restored = deserialize_douyin_continuation_result(
            serialize_douyin_continuation_result(original)
        )
        self.assertEqual(original.metadata["title"], "")
        self.assertEqual(restored.metadata["title"], "")
        self.assertEqual(original.metadata["description"], description)
        self.assertEqual(restored.metadata["description"], description)
        before = build_result_display_context(original, probe_missing_video_sizes=False)
        after = build_result_display_context(restored, probe_missing_video_sizes=False)
        self.assertEqual(after["display_title"], "Gallery lead")
        self.assertEqual(after["base_filename"], "Gallery lead")
        self.assertIn(details, after["display_description"])
        self.assertEqual(after["display_topics"], ["tailtopic"])
        for key in ("display_title", "display_description", "display_topics", "base_filename"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(
            [asset.to_download_item() for asset in restored.assets],
            [asset.to_download_item() for asset in original.assets],
        )

    def test_large_gallery_caption_fits_complete_cache_record_without_losing_images(self):
        self.images = [f"https://p3-sign.douyinpic.com/gallery/{i}.webp" for i in range(24)]
        base = serialize_douyin_continuation_result(self.result(title="Gallery title", description=""))
        room = DOUYIN_CONTINUATION_MAX_CACHE_BYTES - len(
            json.dumps(base, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        )
        for title, length in (("Gallery title", 2000), ("Gallery title", (room - 30) // 6), ("", 2000)):
            with self.subTest(title=title, description_length=length):
                description = "\u56fe" * length
                original = self.result(title=title, description=description)
                coordinator = DouyinContinuationCoordinator(1, first_upgrade_wins=True)
                self.addCleanup(coordinator.abandon)
                future = Future()
                future.set_result(original)
                coordinator.capture_future(
                    future, provider_name="tiantianjiexi", score_result=lambda result: (0, 0),
                    is_strong_result=lambda result: False, prepare_result=lambda result: result,
                )
                record = cache.get(build_douyin_continuation_cache_key(coordinator.task_id))
                self.assertEqual(record["status"], "success")
                self.assertTrue(coordinator.available)
                self.assertLessEqual(
                    len(json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8")),
                    DOUYIN_CONTINUATION_MAX_CACHE_BYTES,
                )
                restored = deserialize_douyin_continuation_result(record["result"])
                self.assertTrue(restored.metadata["description"])
                self.assertTrue(description.startswith(restored.metadata["description"]))
                self.assertLess(len(restored.metadata["description"]), len(description))
                self.assertEqual(original.metadata["description"], description)
                self.assertEqual(
                    [asset.to_download_item() for asset in restored.assets],
                    [asset.to_download_item() for asset in original.assets],
                )

    def test_cache_cannot_swap_image_tokens_types_or_works(self):
        payload = serialize_douyin_continuation_result(self.result())
        other = serialize_douyin_continuation_result(self.result("7634213610894689788"))
        for mutation in ("token", "type", "source", "work"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(payload)
                if mutation == "token":
                    changed["assets"][1]["provider_manifest_token"] = changed["assets"][0]["provider_manifest_token"]
                elif mutation == "type":
                    changed["post_type"] = "video"
                    changed["asset"] = changed["assets"][0]
                    changed["asset"]["media_type"] = "video"
                elif mutation == "source":
                    changed["assets"][0]["source_platform"] = "bilibili"
                else:
                    changed["assets"][1] = other["assets"][1]
                with self.assertRaises(ValueError):
                    deserialize_douyin_continuation_result(changed)

    def test_expired_image_authorization_is_not_renewed_by_continuation(self):
        with patch("django.core.signing.time.time", return_value=1000):
            payload = serialize_douyin_continuation_result(self.result())
        with self.assertRaises(ValueError):
            deserialize_douyin_continuation_result(payload)

    def test_image_url_expiry_is_rechecked_even_when_manifest_is_current(self):
        now = int(time.time())
        self.images = [url + f"?x-expires={now + 40}" for url in self.images]
        payload = serialize_douyin_continuation_result(self.result())
        with patch("xhs_download.services.downloads.time.time", return_value=now + 11):
            with self.assertRaisesRegex(ValueError, "expired"):
                deserialize_douyin_continuation_result(payload)

    @patch("xhs_download.views.parse_media_page", side_effect=AssertionError("poll must not reparse"))
    def test_late_gallery_is_rendered_through_real_public_continuation(self, parse):
        coordinator = DouyinContinuationCoordinator(1, first_upgrade_wins=True)
        self.addCleanup(coordinator.abandon)
        future = Future()
        original = self.result(title="Gallery title", description="Full gallery caption #photos <b>detail</b>")
        future.set_result(original)
        coordinator.capture_future(
            future, provider_name="tiantianjiexi", score_result=lambda result: (0, 0),
            is_strong_result=lambda result: False, prepare_result=lambda result: result,
        )
        token = issue_douyin_continuation_token(
            task_id=coordinator.task_id,
            client_identity=hashlib.sha256(b"127.0.0.1").hexdigest()[:24], platform_key="home",
        )
        response = self.client.post(
            "/media/douyin-continuation/", data=json.dumps({"continuation_token": token}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertTrue(body["has_results"])
        self.assertIn("3", body["html"])
        self.assertIn("Gallery title", body["html"])
        self.assertIn("Full gallery caption", body["html"])
        self.assertIn("#photos", body["html"])
        self.assertIn("&lt;b&gt;detail&lt;/b&gt;", body["html"])
        self.assertNotIn("<b>detail</b>", body["html"])
        for asset in original.assets:
            self.assertIn(asset.asset_id, body["html"])
        self.assertNotIn("查看技术信息", body["html"])
        parse.assert_not_called()
