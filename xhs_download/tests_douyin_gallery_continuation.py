import hashlib
import json
from concurrent.futures import Future
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from .services.douyin_continuation import (
    DouyinContinuationCoordinator,
    deserialize_douyin_continuation_result,
    issue_douyin_continuation_token,
    serialize_douyin_continuation_result,
)
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import DouyinParseContext, DouyinTianTianJieXiProvider
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class DouyinGalleryContinuationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.images = [f"https://p3-sign.douyinpic.com/gallery/{i}.webp" for i in range(3)]
        self.provider = DouyinTianTianJieXiProvider(provider_settings=SimpleNamespace())

    def result(self, aweme_id="7634213610894689787"):
        payload = TianTianJieXiResultNormalizer.normalize_payload({
            "desc": "Gallery fixture #photos", "images": self.images, "videos": [],
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

    @patch("xhs_download.views.parse_media_page", side_effect=AssertionError("poll must not reparse"))
    def test_late_gallery_is_rendered_through_real_public_continuation(self, parse):
        coordinator = DouyinContinuationCoordinator(1, first_upgrade_wins=True)
        self.addCleanup(coordinator.abandon)
        future = Future()
        future.set_result(self.result())
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
        for asset in self.result().assets:
            self.assertIn(asset.asset_id, body["html"])
        self.assertNotIn("查看技术信息", body["html"])
        parse.assert_not_called()
