from hashlib import sha256
import json
from time import time
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsDownloadError
from .services.download_routing import decide_download_route, route_payload
from .services.downloads import (
    build_download_archive_file, build_provider_signed_cover_item,
    fetch_item_to_temp_path, provider_manifest_allows_public_download,
    request_public_provider_get_with_retry,
)
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_strategy import build_download_strategy


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class UpstreamDownloadTests(SimpleTestCase):
    def setUp(self):
        self.item = dict(
            asset_id="upstream:" + "a" * 32 + ":video:1", type="video",
            provider="yt_dlp_generic", provider_resource_id="source",
            provider_cache_key="upstream:job", source_platform="upstream",
            url="https://media.example.org/video.mp4",
        )
        self.item["provider_manifest_token"] = build_provider_manifest_token(
            **{k: v for k, v in self.item.items() if k != "type"}, media_type="video",
        )
        key = "upstream:asset:v1:" + sha256(self.item["asset_id"].encode()).hexdigest()
        cache.set(key, {**self.item, 'expires_at': time() + 300}, 300)
        self.addCleanup(cache.delete, key)
        self.cache_key = key
        self.request = RequestFactory().post("/")
        runtime = SimpleNamespace(visitor_video_download_enabled=True,
                                  single_video_bytes_limit=0, single_archive_bytes_limit=0)
        self.runtime_patch = patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load", return_value=runtime)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)

    def test_route_revalidates_binding_and_exports_server_flag(self):
        route = decide_download_route(self.request, [self.item])
        self.assertEqual(route["route_type"], "direct")
        self.assertEqual(route["server_egress_estimated_bytes"], 0)
        self.assertIs(route["items"][0]["browser_upstream_bound"], True)
        self.assertTrue(provider_manifest_allows_public_download(self.item))
        for field, value in (("url", "https://other.example.org/a.mp4"),
                             ("source_platform", "other"),
                             ("provider_manifest_token", "invalid")):
            forged = {**self.item, field: value, "browser_upstream_bound": True}
            route = decide_download_route(self.request, [forged])
            self.assertEqual(route["route_type"], "blocked")
            self.assertIs(route["items"][0]["browser_upstream_bound"], False)
        cache.delete(self.cache_key)
        self.assertFalse(provider_manifest_allows_public_download(self.item))

    def issued_video(self, provider='yt_dlp_generic'):
        from .services.upstream_delivery import _issue_assets, _key

        asset = MediaAsset(media_type='video', platform='third_party',
                           url='https://v26-default.365yg.com/video.mp4')
        issued = _issue_assets((asset,), match={'provider_id': provider},
                               source_url='https://source.example/work/one')[0].to_download_item()
        self.addCleanup(cache.delete, _key(issued['asset_id']))
        return issued

    def test_issued_video_route_keeps_original_identity_and_expiry_across_providers(self):
        from .services.upstream_delivery import _key

        now = int(time())
        for provider in ('yt_dlp_generic', 'parsehub_generic'):
            with self.subTest(provider=provider), patch('time.time', return_value=now), \
                    patch('xhs_download.services.upstream_delivery.time', return_value=now):
                item = self.issued_video(provider)
            record = cache.get(_key(item['asset_id']))
            self.assertEqual(record['expires_at'], now + 300)
            for elapsed in (0, 200, 299):
                with patch('time.time', return_value=now + elapsed), \
                        patch('xhs_download.services.upstream_delivery.time', return_value=now + elapsed), \
                        patch('django.core.signing.TimestampSigner.sign', side_effect=AssertionError('No renewal')), \
                        patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
                    route = decide_download_route(self.request, [item])
                self.assertEqual(route['route_type'], 'direct')
                public = route['items'][0]
                self.assertEqual(public['selection_schema'], 'upstream_resource_v1')
                self.assertEqual(public['selection_expires_at'], (now + 300) * 1000)
                self.assertEqual(public['download_strategy'], 'browser_streaming')
                for key in ('asset_id', 'provider', 'provider_resource_id', 'provider_cache_key',
                            'provider_manifest_token', 'url'):
                    self.assertEqual(public[key], item[key])
                self.assertNotIn('candidate_id', public)
                self.assertNotIn('selected_format_id', public)
            with patch('xhs_download.services.upstream_delivery.time', return_value=now + 300):
                self.assertEqual(decide_download_route(self.request, [item])['route_type'], 'blocked')

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=120)
    def test_resource_expiry_is_bounded_by_original_manifest_not_route_time(self):
        now = int(time())
        with patch('time.time', return_value=now), \
                patch('xhs_download.services.upstream_delivery.time', return_value=now):
            item = self.issued_video()
        with patch('time.time', return_value=now + 90):
            public = decide_download_route(self.request, [item])['items'][0]
        self.assertEqual(public['selection_expires_at'], (now + 120) * 1000)
        with patch('time.time', return_value=now + 121):
            self.assertEqual(decide_download_route(self.request, [item])['route_type'], 'blocked')

    def test_old_cache_without_expiry_has_no_streaming_grant_or_fabricated_expiry(self):
        from .services.upstream_delivery import _key

        item = self.issued_video()
        key = _key(item['asset_id'])
        record = cache.get(key)
        record.pop('expires_at')
        cache.set(key, record, 300)
        for forged in (item, {**item, 'selection_expires_at': int((time() + 9999) * 1000),
                             'browser_direct_fetch_safe': True, 'browser_upstream_bound': True}):
            public = decide_download_route(self.request, [forged])['items'][0]
            self.assertEqual(public['selection_schema'], 'upstream_resource_v1')
            self.assertEqual(public['streaming_unsupported_reason'], 'missing_resource_expiry')
            self.assertNotIn('selection_expires_at', public)
            self.assertNotIn('download_strategy', public)
            self.assertIs(public['browser_direct_fetch_safe'], False)
            self.assertIs(public['browser_upstream_bound'], False)

    def test_server_modes_and_mixed_selections_are_blocked(self):
        for mode in ("proxy", "server_single", "zip", "stream", "server"):
            route = decide_download_route(self.request, [self.item], requested_mode=mode)
            self.assertEqual(route["route_type"], "blocked", mode)
        route = decide_download_route(self.request, [self.item, {"type": "image", "url": "https://example.org/a.jpg"}])
        self.assertEqual(route["route_type"], "blocked")

    def test_policy_cannot_restore_server_egress(self):
        for route_type in ("proxy", "zip", "fallback"):
            route = route_payload(route_type=route_type, route_reason="policy",
                                  download_strategy="server_zip", media_items=[self.item], estimated_bytes=500)
            self.assertEqual(route["route_type"], "blocked")
            self.assertEqual(route["server_egress_estimated_bytes"], 0)

    def test_image_binding_uses_browser_archive(self):
        image = {**self.item, "type": "image", "provider": "gallery_dl_generic",
                 "asset_id": "upstream:" + "b" * 32 + ":image:1",
                 "url": "https://media.example.org/a.jpg"}
        image["provider_manifest_token"] = build_provider_manifest_token(
            **{k: v for k, v in image.items() if k not in {"type", "provider_manifest_token"}}, media_type="image")
        key = "upstream:asset:v1:" + sha256(image["asset_id"].encode()).hexdigest()
        cache.set(key, image, 300)
        self.addCleanup(cache.delete, key)
        for items in ([image], [image, image]):
            route = decide_download_route(self.request, items, requested_mode="browser")
            self.assertEqual(route["download_strategy"], "browser_direct_images")
            self.assertTrue(all(item["browser_upstream_bound"] for item in route["items"]))

    def test_prepare_endpoint_rejects_before_issuing_handoff(self):
        from .views import download_prepare_api

        request = RequestFactory().post("/download/prepare/", json.dumps({"media_items": [self.item]}),
                                        content_type="application/json")
        request._dont_enforce_csrf_checks = True
        with patch("xhs_download.views.get_download_runtime_setting"), \
                patch("xhs_download.views.check_download_cost_preflight", return_value=None), \
                patch("xhs_download.views.issue_download_handoff_tokens") as issue:
            response = download_prepare_api(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error_code"], "DOWNLOAD_PREPARE_ROUTE_REJECTED")
        issue.assert_not_called()

    def test_direct_post_zip_is_rejected_without_downloading(self):
        from .views import download_images_api

        request = RequestFactory().post("/download/", json.dumps({"media_items": [self.item], "download_mode": "zip"}),
                                        content_type="application/json")
        request._dont_enforce_csrf_checks = True
        with patch("xhs_download.views.zip_download_route_info", return_value={}), \
                patch("xhs_download.views.get_download_runtime_setting"), \
                patch("xhs_download.views.check_download_cost_preflight", return_value=None), \
                patch("xhs_download.views.log_download_request_event"), \
                patch("xhs_download.views.safe_routing_decision", side_effect=lambda request, items, callback, **kw: callback()), \
                patch("xhs_download.views.apply_policy_decision_to_route", side_effect=lambda request, items, route, **kw: route), \
                patch("xhs_download.views.build_download_archive_file") as archive, \
                patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")) as network:
            response = download_images_api(request)
        self.assertEqual(response.status_code, 403)
        archive.assert_not_called()
        network.assert_not_called()

    def test_public_result_preserves_exact_source_binding_without_granting_route_flag(self):
        from .views import build_image_item, build_video_item

        for media_type, builder in (("image", build_image_item), ("video", build_video_item)):
            fields = {k: v for k, v in self.item.items() if k != "type"}
            fields["asset_id"] = "upstream:" + "a" * 32 + f":{media_type}:1"
            fields["provider_manifest_token"] = build_provider_manifest_token(
                **{k: v for k, v in fields.items() if k != "provider_manifest_token"}, media_type=media_type)
            asset = MediaAsset(media_type=media_type, platform="third_party", **fields)
            result = ParseResult(platform="third_party", post_type=media_type, base_filename="test", assets=(asset,))
            with patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")) as network:
                public = builder(asset, result)
            self.assertEqual(public["source_platform"], "upstream")
            self.assertEqual(public["provider_manifest_token"], fields["provider_manifest_token"])
            self.assertNotIn("browser_upstream_bound", public)
            self.assertFalse(public["browser_direct_fetch_safe"])
            network.assert_not_called()

    def test_server_save_rejects_entire_selection_before_filesystem_side_effects(self):
        from .services.downloads import save_media_items_to_server

        ordinary = {"type": "image", "url": "https://example.org/image.jpg"}
        for upstream in (self.item, {**self.item, "provider": "", "provider_manifest_token": ""},
                         {**self.item, "type": "image", "asset_id": "upstream:invalid:image:1"}):
            for items in ([upstream], [ordinary, upstream], [upstream, ordinary]):
                with self.subTest(items=items):
                    folder = Mock()
                    with patch("xhs_download.services.downloads.resolve_server_save_folder", return_value=folder) as resolve, \
                            patch("xhs_download.services.downloads.fetch_item_to_temp_path") as fetch_item, \
                            patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")) as network:
                        with self.assertRaises(XhsDownloadError):
                            save_media_items_to_server(items, "test")
                    resolve.assert_not_called()
                    fetch_item.assert_not_called()
                    network.assert_not_called()
                    folder.mkdir.assert_not_called()

    def test_server_save_post_rejects_before_mode_fallback_or_preparation(self):
        from .views import download_images_api

        ordinary = {"type": "image", "url": "https://example.org/image.jpg"}
        for upstream in (self.item, {**self.item, "provider": "", "provider_manifest_token": ""}):
            for items in ([upstream], [ordinary, upstream], [upstream, ordinary]):
                for server_enabled in (True, False):
                    request = RequestFactory().post("/download/", json.dumps({
                        "media_items": items, "download_mode": "server",
                    }), content_type="application/json")
                    request._dont_enforce_csrf_checks = True
                    with patch("xhs_download.views.server_save_mode_is_enabled", return_value=server_enabled) as enabled, \
                            patch("xhs_download.views.zip_download_route_info") as prepare, \
                            patch("xhs_download.views.save_media_items_to_server") as save, \
                            patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")) as network:
                        response = download_images_api(request)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(json.loads(response.content)["error_code"], "UPSTREAM_BROWSER_DOWNLOAD_REQUIRED")
                    enabled.assert_not_called()
                    prepare.assert_not_called()
                    save.assert_not_called()
                    network.assert_not_called()

    def test_no_server_relay_even_with_expired_or_missing_provider(self):
        for item in (self.item, {**self.item, "provider": "", "provider_manifest_token": ""}):
            with self.assertRaises(XhsDownloadError):
                fetch_item_to_temp_path(item)
            with self.assertRaises(XhsDownloadError):
                build_download_archive_file([item], "test")
            with self.assertRaises(XhsDownloadError):
                request_public_provider_get_with_retry(item["url"], provider_item=item)
        self.assertIsNone(build_provider_signed_cover_item(self.item, "https://example.org/cover.jpg"))

    def test_strategy_is_prefix_scoped_and_has_no_server_fallback(self):
        for media_type in ("video", "image"):
            asset = MediaAsset(media_type=media_type, url=self.item["url"], platform="third_party",
                               asset_id=f"upstream:job:{media_type}:1", provider="yt_dlp_generic")
            strategy = build_download_strategy(asset)
            self.assertEqual(strategy["fallback_strategy"], "none")
            self.assertEqual(strategy["strategy_label"], "下载")
            self.assertFalse(strategy["browser_direct_fetch_safe"])
        legacy = MediaAsset(media_type="video", url=self.item["url"], platform="third_party", provider="yt_dlp_generic")
        self.assertEqual(build_download_strategy(legacy)["download_strategy"], "server_attachment")
