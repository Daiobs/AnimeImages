import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import BatchParseRuntimeSetting, ProviderAccessMode
from .services.assets import MediaAsset, ParseResult
from .services.provider_manifest import build_provider_manifest_token


@override_settings(
    BATCH_PARSE_PUBLIC_ENABLED=False,
    BATCH_PARSE_IP_INTERVAL_SECONDS=0,
    BATCH_PARSE_GLOBAL_MINUTE_LIMIT=0,
    BATCH_PARSE_DAILY_IP_LIMIT=0,
)
class BatchAccessVisibilityTests(TestCase):
    navigation_routes = ("home", "xhs_home", "tiktok_home", "bilibili_home")

    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create_user(
            username="batch-staff",
            password="password",
            is_staff=True,
        )
        self.admin_user = user_model.objects.create_superuser(
            username="batch-admin",
            password="password",
            email="admin@example.com",
        )

    def set_access_mode(self, mode, *, rollout_percent=0):
        return BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "access_mode": mode,
                "rollout_percent": rollout_percent,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )[0]

    def assert_navigation(self, *, visible):
        for route_name in self.navigation_routes:
            with self.subTest(route=route_name, visible=visible):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                assertion = self.assertContains if visible else self.assertNotContains
                assertion(response, 'data-nav-platform-key="batch"')

    def assert_batch_endpoints(self, *, allowed):
        responses = {
            "page": self.client.get(reverse("batch_parse")),
            "start": self.client.post(
                reverse("batch_parse_start"),
                {"links": "https://example.com/item"},
            ),
            "item": self.client.post(
                reverse("batch_parse_item"),
                {"batch_run_token": "invalid", "index": "0"},
            ),
            "retry": self.client.post(
                reverse("batch_parse_retry"),
                {"link": ""},
            ),
        }
        if allowed:
            self.assertEqual(responses["page"].status_code, 200)
            self.assertEqual(responses["start"].status_code, 200)
            self.assertEqual(responses["item"].status_code, 400)
            self.assertEqual(responses["retry"].status_code, 400)
        else:
            for name, response in responses.items():
                with self.subTest(endpoint=name):
                    self.assertEqual(response.status_code, 404)

    def test_disabled_hides_and_blocks_batch_for_anonymous_and_staff(self):
        self.set_access_mode(ProviderAccessMode.DISABLED)

        self.assert_navigation(visible=False)
        self.assert_batch_endpoints(allowed=False)

        self.client.force_login(self.staff_user)
        self.assert_navigation(visible=False)
        self.assert_batch_endpoints(allowed=False)

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(
            reverse("admin:xhs_download_batchparseruntimesetting_changelist")
        )
        self.assertRedirects(
            admin_response,
            reverse(
                "admin:xhs_download_batchparseruntimesetting_change",
                args=(BatchParseRuntimeSetting.objects.get(singleton_key=1).pk,),
            ),
            fetch_redirect_response=False,
        )

    def test_staff_mode_hides_anonymous_and_allows_staff(self):
        self.set_access_mode(ProviderAccessMode.STAFF)

        self.assert_navigation(visible=False)
        self.assert_batch_endpoints(allowed=False)

        self.client.force_login(self.staff_user)
        self.assert_navigation(visible=True)
        self.assert_batch_endpoints(allowed=True)

    def test_public_mode_allows_anonymous_and_staff(self):
        self.set_access_mode(ProviderAccessMode.PUBLIC)

        self.assert_navigation(visible=True)
        self.assert_batch_endpoints(allowed=True)

        self.client.force_login(self.staff_user)
        self.assert_navigation(visible=True)
        self.assert_batch_endpoints(allowed=True)

    def test_public_gray_uses_the_same_rollout_for_navigation_and_endpoints(self):
        self.set_access_mode(ProviderAccessMode.PUBLIC_GRAY, rollout_percent=100)

        self.assert_navigation(visible=True)
        self.assert_batch_endpoints(allowed=True)

        self.set_access_mode(ProviderAccessMode.PUBLIC_GRAY, rollout_percent=0)
        self.assert_navigation(visible=False)
        self.assert_batch_endpoints(allowed=False)

        self.client.force_login(self.staff_user)
        self.assert_navigation(visible=True)
        self.assert_batch_endpoints(allowed=True)

    @patch(
        "xhs_download.context_processors.batch_parse_access_allowed",
        side_effect=RuntimeError("database unavailable"),
    )
    def test_navigation_visibility_failure_is_closed_without_breaking_home(self, _mock_gate):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-nav-platform-key="batch"')


@override_settings(
    BATCH_PARSE_PUBLIC_ENABLED=False,
    BATCH_PARSE_IP_INTERVAL_SECONDS=0,
    BATCH_PARSE_GLOBAL_MINUTE_LIMIT=0,
    BATCH_PARSE_DAILY_IP_LIMIT=0,
)
class BatchPublicDownloadTemplateTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="batch-download-staff",
            password="password",
            is_staff=True,
        )
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={
                "access_mode": ProviderAccessMode.PUBLIC,
                "ip_interval_seconds": 0,
                "global_minute_limit": 0,
                "daily_ip_limit": 0,
            },
        )

    @staticmethod
    def parse_result():
        return ParseResult(
            platform="xiaohongshu",
            post_type="mixed",
            base_filename="batch_public_demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://media.example.com/video.mp4",
                    platform="xiaohongshu",
                    size_bytes=1024,
                ),
                MediaAsset(
                    media_type="image",
                    url="https://media.example.com/image.jpg",
                    platform="xiaohongshu",
                ),
            ),
            metadata={"title": "Public Batch"},
        )

    @patch("xhs_download.views.parse_media_page")
    def test_public_anonymous_can_download_video_and_image_without_raw_tools(self, mock_parse):
        mock_parse.return_value = self.parse_result()

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/source"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "batch-browser-download-btn", count=2)
        self.assertContains(response, "data-batch-download-item", count=2)
        self.assertContains(response, "data-batch-download-route-url")
        self.assertContains(response, "data-batch-download-prepare-url")
        self.assertContains(response, "js/download_reliability.js")
        self.assertNotContains(response, "复制直链")
        self.assertNotContains(response, "打开直链")
        self.assertNotContains(response, "展开链接")
        self.assertNotContains(response, "复制全部成功视频直链")
        self.assertNotContains(response, "links.txt")
        self.assertNotContains(response, "aria2.txt")
        self.assertNotContains(response, "data-batch-direct-url")
        self.assertNotContains(response, "高级说明：下载策略矩阵")

    @patch("xhs_download.views.parse_media_page")
    def test_staff_keeps_download_and_raw_link_tools(self, mock_parse):
        mock_parse.return_value = self.parse_result()
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/source"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "batch-browser-download-btn", count=2)
        self.assertContains(response, "复制直链")
        self.assertContains(response, "打开直链")
        self.assertContains(response, "展开链接")
        self.assertContains(response, "复制全部成功视频直链")
        self.assertContains(response, "links.txt")
        self.assertContains(response, "aria2.txt")
        self.assertContains(response, "data-batch-direct-url")
        self.assertContains(response, "高级说明：下载策略矩阵")

    @patch("xhs_download.views.build_batch_result_row")
    def test_public_retry_card_keeps_download_but_not_raw_tools(self, mock_row):
        mock_row.return_value = {
            "status": "success",
            "source_text": "https://example.com/source",
            "base_filename": "retry_public",
            "duration_ms": 1,
            "platform": "xiaohongshu",
            "platform_name": "小红书",
            "post_type": "image",
            "video_items": (),
            "image_items": ({
                "url": "https://media.example.com/image.jpg",
                "direct_url": "https://media.example.com/image.jpg",
                "encoded_url": "",
                "encoded_preview_url": "",
                "preview_provider_query": "",
                "download_strategy": "browser_direct",
            },),
        }

        response = self.client.post(
            reverse("batch_parse_retry"),
            {"link": "https://example.com/source"},
        )

        self.assertEqual(response.status_code, 200)
        card_html = response.json()["card_html"]
        self.assertIn("data-batch-download", card_html)
        self.assertNotIn("复制直链", card_html)
        self.assertNotIn("打开直链", card_html)
        self.assertNotIn("data-batch-direct-url", card_html)

    def test_public_provider_item_uses_validated_direct_with_explicit_relay_fallback(self):
        aweme_id = "123456789012345678"
        item = {
            "type": "video",
            "url": "https://v3-dy-o.zjcdn.com/path/video.mp4",
            "asset_id": f"douyin:{aweme_id}:video:tiantianjiexi",
            "provider": "tiantianjiexi",
            "provider_resource_id": aweme_id,
            "provider_cache_key": f"douyin:tiantianjiexi:validated:{aweme_id}",
        }
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"],
            provider=item["provider"],
            provider_resource_id=item["provider_resource_id"],
            provider_cache_key=item["provider_cache_key"],
            url=item["url"],
            media_type=item["type"],
        )
        payload = {
            "media_items": [item],
            "base_filename": "batch-provider",
            "download_mode": "auto",
            "route_only": True,
        }

        route_response = self.client.post(
            reverse("media_download_images_api"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        prepare_response = self.client.post(
            reverse("media_download_prepare"),
            data=json.dumps({**payload, "download_mode": "server_single"}),
            content_type="application/json",
        )

        self.assertEqual(route_response.status_code, 200)
        self.assertEqual(route_response.json()["route_type"], "direct")
        self.assertEqual(
            route_response.json()["download_strategy"],
            "browser_direct_provider_video",
        )
        self.assertTrue(
            route_response.json()["items"][0][
                "browser_native_handoff_safe"
            ]
        )
        self.assertFalse(
            route_response.json()["items"][0][
                "browser_direct_fetch_safe"
            ]
        )
        self.assertEqual(prepare_response.status_code, 200)
        self.assertEqual(prepare_response.json()["status"], "issued")
        self.assertTrue(prepare_response.json()["download_url"].startswith(
            "/media/download-stream/"
        ))
        self.assertTrue(prepare_response.json()["status_url"].startswith(
            "/media/download-status/"
        ))

    def test_public_download_rejects_tampered_provider_manifest_safely(self):
        provider_url = "https://media.example.com/provider-video.mp4?token=test-secret"
        item = {
            "type": "video",
            "url": provider_url,
            "asset_id": "douyin:demo:video:tiantianjiexi",
            "provider": "tiantianjiexi",
            "provider_resource_id": "demo",
            "provider_cache_key": "douyin:tiantianjiexi:validated:demo",
            "provider_manifest_token": "tampered",
        }
        payload = {
            "media_items": [item],
            "base_filename": "batch-provider",
            "download_mode": "auto",
            "route_only": True,
        }

        route_response = self.client.post(
            reverse("media_download_images_api"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        prepare_response = self.client.post(
            reverse("media_download_prepare"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(route_response.status_code, 400)
        self.assertEqual(prepare_response.status_code, 400)
        self.assertEqual(
            prepare_response.json()["error_code"],
            "PROVIDER_MANIFEST_REJECTED",
        )
        self.assertNotIn(provider_url, route_response.content.decode())
        self.assertNotIn(provider_url, prepare_response.content.decode())
        self.assertNotIn("test-secret", route_response.content.decode())
        self.assertNotIn("test-secret", prepare_response.content.decode())
