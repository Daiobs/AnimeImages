"""Offline regression for the original note's three-image, zero-video shape."""
from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import json
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import signing
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from PIL import Image

from tools.native_result_replay import offline_network
from .services.downloads import (
    fetch_item_to_temp_path,
    iter_download_files,
    provider_manifest_allows_public_download,
    validate_provider_signed_download_url,
)
from .services.common import XhsDownloadError
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import DouyinParseContext, DouyinTianTianJieXiProvider
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient,
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
    build_payload_cache_key,
    source_key_for_request,
)
from .services.provider_manifest import (
    PROVIDER_MANIFEST_SALT,
    build_provider_manifest_token,
    douyin_tiantian_image_asset_id,
)
from .services.provider_recovery import get_tiantianjiexi_manifest_identity
from .services.provider_reliability import ProviderResultInvalidError


AWEME = "7634213610894689787"
SOURCE = f"https://www.douyin.com/note/{AWEME}"
IMAGE_URLS = tuple(f"https://p3-sign.douyinpic.com/gallery-fixture/{index}.webp" for index in range(1, 4))


@override_settings(
    SECRET_KEY="gallery-test-only-key",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="gallery-test-account",
    TIANTIANJIEXI_PASSWORD="gallery-test-not-a-credential",
    TIANTIANJIEXI_API_BASE_URL="https://provider.example/parse",
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS=120,
    TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=0,
    PROVIDER_MANIFEST_MAX_AGE_SECONDS=3600,
)
class DouyinTianTianGalleryTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline_network())
        cache.clear()
        self.addCleanup(cache.clear)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root", return_value=Path(folder),
        ))
        self.reference = self.stack.enter_context(patch(
            "xhs_download.services.platforms.douyin_providers.get_reference_result",
            side_effect=AssertionError("Gallery must not fetch video metadata"),
        ))
        self.session = Mock()
        self.raw = {"code": 200, "data": {
            "desc": "Gallery fixture #photos", "images": list(IMAGE_URLS), "videos": [],
        }}
        self.session.post.return_value.json.side_effect = lambda: deepcopy(self.raw)
        self.generic = TianTianJieXiGenericProvider(client=TianTianJieXiApiClient(session=self.session))
        self.provider = DouyinTianTianJieXiProvider(
            provider_settings=SimpleNamespace(
                provider_is_enabled=lambda name: True, ultra_total_timeout_seconds=5,
                enabled_provider_order=("tiantianjiexi",),
            ),
            provider=self.generic, reserve_fallback_grace=False,
        )
        self.adapter = DouyinAdapter()

    def context(self):
        return DouyinParseContext(url=SOURCE, final_url=SOURCE, aweme_id=AWEME, prefer_ultra_hd=True)

    def result(self):
        return self.provider.parse(self.adapter, self.context())

    def item(self):
        return self.result().image_assets[0].to_download_item()

    def signed(self, item, **changes):
        item = {**item, **changes}
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"], provider=item["provider"],
            provider_resource_id=item["provider_resource_id"], provider_cache_key=item["provider_cache_key"],
            url=item["url"], media_type=item["type"], source_platform=item.get("source_platform", ""),
        )
        return item

    def test_original_note_shape_maps_three_ordered_signed_images(self):
        normalized = TianTianJieXiResultNormalizer.normalize_payload(self.raw["data"])
        self.assertEqual(normalized["videos"], ())
        self.assertEqual(normalized["images"], IMAGE_URLS)
        self.assertFalse(self.provider.build_candidates(normalized, self.adapter))
        self.assertTrue(self.provider.payload_has_media(normalized, self.adapter))
        result = self.result()
        self.assertEqual((result.platform, result.post_type), ("douyin", "image"))
        self.assertEqual([asset.url for asset in result.assets], list(IMAGE_URLS))
        self.assertEqual(len({asset.asset_id for asset in result.assets}), 3)
        self.assertEqual(result.base_filename, "Gallery fixture")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(result.metadata["tiantianjiexi_candidate_count"], 3)
        for asset in result.assets:
            item = asset.to_download_item()
            self.assertEqual(asset.asset_id, douyin_tiantian_image_asset_id(AWEME, asset.url))
            self.assertEqual(asset.provider_resource_id, AWEME)
            self.assertEqual(asset.provider, "tiantianjiexi")
            self.assertEqual(asset.provider_cache_key, f"douyin:tiantianjiexi:validated:{AWEME}")
            self.assertTrue(provider_manifest_allows_public_download(item))
            signed = signing.loads(asset.provider_manifest_token, salt=PROVIDER_MANIFEST_SALT)
            self.assertEqual((signed["type"], signed["source_platform"]), ("image", "douyin"))
            self.assertIsNone(get_tiantianjiexi_manifest_identity(item))
        self.session.post.assert_called_once()
        self.reference.assert_not_called()

    def test_parse_cache_and_cached_result_keep_gallery_without_another_request(self):
        first = self.result()
        second = self.result()
        cached = self.provider.cached_result(self.adapter, self.context())
        for result in (second, cached):
            self.assertEqual([(asset.url, asset.asset_id) for asset in result.assets],
                             [(asset.url, asset.asset_id) for asset in first.assets])
            self.assertTrue(all(provider_manifest_allows_public_download(asset.to_download_item()) for asset in result.assets))
            self.assertEqual(result.post_type, "image")
        self.assertTrue(cached.metadata["tiantianjiexi_cache_hit"])
        self.session.post.assert_called_once()

    def test_deduplicates_images_without_reordering_or_changing_identity(self):
        self.raw["data"]["images"] = [IMAGE_URLS[2], IMAGE_URLS[0], IMAGE_URLS[2], IMAGE_URLS[1]]
        result = self.result()
        self.assertEqual([asset.url for asset in result.assets], [IMAGE_URLS[2], IMAGE_URLS[0], IMAGE_URLS[1]])
        self.assertEqual(result.assets[1].asset_id, douyin_tiantian_image_asset_id(AWEME, IMAGE_URLS[0]))

    def test_malicious_or_expired_images_never_become_valid_payloads(self):
        invalid = [
            "https://attacker.example/image.webp", "https://douyinpic.com.attacker.example/image.webp",
            "http://127.0.0.1/image.webp", "http://[::1]/image.webp", "file:///tmp/image.webp",
            "https://user:password@p3.douyinpic.com/image.webp", "https://p3.douyinpic.com:8000/image.webp",
            "https://p3.douyinpic.com:invalid/image.webp", "https://p3.douyinpic.com./image.webp",
            IMAGE_URLS[0] + "?x-expires=1", IMAGE_URLS[0] + "?x-expires=0",
            IMAGE_URLS[0] + "?x-expires=", IMAGE_URLS[0] + "?x-expires=invalid",
            f"https://v3-dy-o.zjcdn.com/{int(time.time()) - 60:08x}/video/image.webp",
            "", None, {"url": IMAGE_URLS[0]},
        ]
        for url in invalid:
            with self.subTest(url=url):
                payload = {"videos": [], "images": [url]}
                self.assertFalse(self.provider.payload_has_media(payload, self.adapter))
                with self.assertRaises(ProviderResultInvalidError):
                    self.generic.validate_payload(payload, lambda value: self.provider.payload_has_media(value, self.adapter))

    def test_invalid_only_payload_does_not_enter_success_cache(self):
        self.raw["data"]["images"] = ["https://attacker.example/image.webp"]
        with self.assertRaises(ProviderResultInvalidError):
            self.result()
        key = source_key_for_request(SOURCE, self.context().source_identity)
        self.assertIsNone(cache.get(build_payload_cache_key(key)))

    def test_invalid_image_is_omitted_but_other_images_survive(self):
        self.raw["data"]["images"] = [IMAGE_URLS[0], "http://127.0.0.1/x", IMAGE_URLS[2]]
        self.assertEqual([asset.url for asset in self.result().assets], [IMAGE_URLS[0], IMAGE_URLS[2]])

    def test_video_candidates_keep_existing_video_and_do_not_promote_cover_images(self):
        self.reference.side_effect = None
        self.reference.return_value = None
        self.raw["data"]["videos"] = [{"url": "https://v3-dy-o.zjcdn.com/fixture/video.mp4", "quality": "1080P", "size": 3000}]
        with patch("xhs_download.services.platforms.douyin_providers.attach_douyin_service_picker", side_effect=lambda result, **kwargs: result):
            result = self.result()
        self.assertEqual(result.post_type, "video")
        self.assertEqual(len(result.video_assets), 1)
        self.assertFalse(result.image_assets)
        self.assertEqual(result.assets[0].asset_id, f"douyin:{AWEME}:video:tiantianjiexi")
        self.assertTrue(provider_manifest_allows_public_download(result.assets[0].to_download_item()))

    def test_rejected_videos_do_not_promote_response_images_to_gallery(self):
        for video_url in (
            "https://attacker.example/video.mp4",
            f"https://v3-dy-o.zjcdn.com/{int(time.time()) - 60:08x}/video/fixture.mp4",
        ):
            with self.subTest(video_url=video_url):
                cache.clear()
                self.raw["data"]["videos"] = [{"url": video_url, "quality": "1080P"}]
                payload = TianTianJieXiResultNormalizer.normalize_payload(self.raw["data"])
                self.assertTrue(payload["videos"])
                self.assertFalse(self.provider.build_candidates(payload, self.adapter))
                self.assertFalse(self.provider.build_image_urls(payload, self.adapter))
                with self.assertRaises(ProviderResultInvalidError):
                    self.result()
                key = source_key_for_request(SOURCE, self.context().source_identity)
                self.assertIsNone(cache.get(build_payload_cache_key(key)))

    def test_manifest_rejects_cross_image_work_type_provider_and_platform_tampering(self):
        items = [asset.to_download_item() for asset in self.result().assets]
        first, second = items[:2]
        for changes in (
            {"url": second["url"]}, {"asset_id": second["asset_id"]},
            {"provider_manifest_token": second["provider_manifest_token"]},
            {"url": second["url"], "asset_id": second["asset_id"]},
            {"provider_resource_id": "7634213610894689788"}, {"type": "video"},
            {"provider": "tiantianjiexi_generic"}, {"source_platform": "third_party"},
            {"provider_cache_key": "third_party:tiantianjiexi:gallery"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(provider_manifest_allows_public_download({**first, **changes}))
        for changes in (
            {"asset_id": second["asset_id"]}, {"type": "video"},
            {"source_platform": "third_party"}, {"asset_id": f"douyin:{AWEME}:video:tiantianjiexi"},
        ):
            with self.subTest(resigned=changes):
                self.assertFalse(provider_manifest_allows_public_download(self.signed(first, **changes)))

    def test_image_cannot_borrow_video_manifest_even_with_the_same_url(self):
        item = self.item()
        video = self.signed(item, type="video", asset_id=f"douyin:{AWEME}:video:tiantianjiexi")
        self.assertFalse(provider_manifest_allows_public_download({**video, "type": "image"}))

    def test_image_url_and_manifest_expiration_are_rechecked_at_download_and_cache_read(self):
        now = int(time.time())
        self.raw["data"]["images"] = [IMAGE_URLS[0] + f"?x-expires={now + 120}"]
        item = self.item()
        self.assertTrue(provider_manifest_allows_public_download(item))
        with patch("xhs_download.services.downloads.time.time", return_value=now + 90):
            self.assertFalse(provider_manifest_allows_public_download(item))
            self.assertIsNone(self.provider.cached_result(self.adapter, self.context()))
        plain = self.signed(item, url=IMAGE_URLS[0], asset_id=douyin_tiantian_image_asset_id(AWEME, IMAGE_URLS[0]))
        with patch("django.core.signing.time.time", return_value=time.time() + 3601):
            self.assertFalse(provider_manifest_allows_public_download(plain))

    def test_signed_download_does_not_redirect_to_another_image(self):
        item = self.item()
        self.assertEqual(validate_provider_signed_download_url(item, item["url"], "blocked"), item["url"])
        with self.assertRaises(XhsDownloadError):
            validate_provider_signed_download_url(item, IMAGE_URLS[1], "blocked")

    def test_invalid_gallery_manifest_cannot_fall_back_to_unsigned_download(self):
        item = self.item()
        for changes in ({"url": IMAGE_URLS[1]}, {"type": "video"}, {"provider_manifest_token": ""}):
            with self.subTest(changes=changes), patch("xhs_download.services.downloads.fetch_to_temp_path") as fetch:
                with self.assertRaisesMessage(XhsDownloadError, "Provider"):
                    fetch_item_to_temp_path({**item, **changes}, expected_type="image")
                fetch.assert_not_called()

    def test_403_does_not_refresh_gallery_into_a_video(self):
        item = self.item()
        with patch("xhs_download.services.downloads.fetch_to_temp_path", side_effect=XhsDownloadError("HTTP 403")) as fetch, \
                patch("xhs_download.services.downloads.refresh_tiantianjiexi_download_item") as refresh:
            with self.assertRaisesMessage(XhsDownloadError, "HTTP 403"):
                fetch_item_to_temp_path(item, expected_type="image")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["provider_signed"], item)
        refresh.assert_not_called()

    def test_existing_image_download_consumer_keeps_order_bytes_and_real_extension(self):
        result = self.result()
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "green").save(buffer, format="PNG")
        content = buffer.getvalue()
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        paths = []

        def downloaded(url, **kwargs):
            self.assertEqual(kwargs["expected_type"], "image")
            self.assertTrue(provider_manifest_allows_public_download(kwargs["provider_signed"]))
            path = Path(folder) / f"{len(paths)}.part"
            path.write_bytes(content)
            paths.append(path)
            return {"path": str(path), "url": url, "content_type": "image/png", "size_bytes": len(content)}

        with patch("xhs_download.services.downloads.fetch_to_temp_path", side_effect=downloaded):
            files = list(iter_download_files([asset.to_download_item() for asset in result.assets], result.base_filename))
        self.assertEqual([name for name, _ in files], [f"Gallery fixture_{index}.png" for index in range(1, 4)])
        self.assertTrue(all(data == content for _, data in files))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_home_batch_display_and_daily_default_consume_gallery_assets(self):
        from .services.provider_download_smoke import default_resource
        from .views import build_result_display_context
        result = self.result()
        for surface in ("single", "batch"):
            display = build_result_display_context(result, probe_missing_video_sizes=False, preview_surface=surface)
            self.assertEqual([item["url"] for item in display["image_items"]], list(IMAGE_URLS))
            self.assertFalse(display["video_items"])
            self.assertTrue(all(provider_manifest_allows_public_download(item) for item in display["image_items"]))
        item, asset, selected = default_resource(result, RequestFactory().get("/"))
        self.assertEqual(item["url"], IMAGE_URLS[0])
        self.assertEqual(asset, result.assets[0])
        self.assertFalse(selected)

    def test_historical_sanitized_tiantian_gallery_shape_is_reusable(self):
        fixture = Path(__file__).parent / "fixtures/native_replay/e162b21c7cd6812ff941e9cc.json"
        data = json.loads(fixture.read_text())["records"][0]["data"]
        normalized = TianTianJieXiResultNormalizer.normalize_payload(data)
        self.assertFalse(normalized["videos"])
        self.assertEqual(len(normalized["images"]), 9)
        # Historical example hosts are not current Douyin download authority.
        self.assertFalse(self.provider.payload_has_media(normalized, self.adapter))
