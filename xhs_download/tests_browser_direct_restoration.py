import json
from unittest.mock import patch

from django.test import Client, RequestFactory, TestCase, override_settings

from download_console.models import DownloadRuntimeSetting

from .policy.engine import apply_policy_decision_to_route
from .policy.types import PolicyDecision
from .models import (
    DownloadRequestLog,
    KukuToolDownloadDeliveryMode,
    ProviderRuntimeSetting,
)
from .services.assets import MediaAsset
from .services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    decide_download_route,
    item_can_browser_streaming_direct,
)
from .services.provider_manifest import (
    PROVIDER_MANIFEST_EXPIRED,
    PROVIDER_MANIFEST_INVALID,
    build_provider_manifest_token,
    provider_manifest_expires_at,
    provider_manifest_validation_status,
)
from .services.provider_strategy import (
    DOWNLOAD_STRATEGY_BROWSER_DIRECT,
    build_download_strategy,
)


AWEME_ID = "7611146072161047526"
MEDIA_URL = "https://v5-default.365yg.com/path/video.mp4"


def build_tiantian_item(url=MEDIA_URL, **overrides):
    item = {
        "type": "video",
        "url": url,
        "size_bytes": 320 * 1024 * 1024,
        "asset_id": f"douyin:{AWEME_ID}:video:tiantianjiexi",
        "provider": "tiantianjiexi",
        "provider_resource_id": AWEME_ID,
        "provider_cache_key": (
            f"douyin:tiantianjiexi:validated:{AWEME_ID}"
        ),
    }
    item.update(overrides)
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=item["asset_id"],
        provider=item["provider"],
        provider_resource_id=item["provider_resource_id"],
        provider_cache_key=item["provider_cache_key"],
        url=item["url"],
        media_type=item["type"],
    )
    return item


class BrowserDirectCapabilityTests(TestCase):
    def setUp(self):
        ProviderRuntimeSetting.load()
        DownloadRuntimeSetting.load()
        self.request = RequestFactory().post("/media/download-images/")

    def test_tiantian_strategy_enables_streaming_without_blob_fetch(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url=MEDIA_URL,
                platform="douyin",
                provider="tiantianjiexi",
            )
        )

        self.assertEqual(
            strategy["download_strategy"],
            DOWNLOAD_STRATEGY_BROWSER_DIRECT,
        )
        self.assertTrue(strategy["browser_native_handoff_capable"])
        self.assertTrue(strategy["browser_streaming_direct_capable"])
        self.assertFalse(strategy["browser_direct_fetch_safe"])
        self.assertFalse(strategy["cors_expected"])

    def test_validated_tiantian_asset_routes_to_filesystem_stream(self):
        route = decide_download_route(
            self.request,
            [build_tiantian_item()],
            requested_mode="auto",
        )

        self.assertEqual(route["route_type"], ROUTE_DIRECT)
        self.assertEqual(
            route["download_strategy"],
            "browser_direct_provider_video",
        )
        self.assertEqual(route["server_egress_estimated_bytes"], 0)
        self.assertGreater(route["browser_egress_estimated_bytes"], 0)
        self.assertTrue(route["items"][0]["browser_native_handoff_safe"])
        self.assertTrue(route["items"][0]["browser_streaming_direct_safe"])
        self.assertFalse(route["items"][0]["browser_direct_fetch_safe"])
        self.assertEqual(
            route["items"][0]["browser_direct_mode"],
            "filesystem_stream",
        )

    def test_validated_tiantian_asset_rejects_server_media_modes(self):
        for requested_mode in ("proxy", "server_single", "zip"):
            with self.subTest(requested_mode=requested_mode):
                route = decide_download_route(
                    self.request,
                    [build_tiantian_item()],
                    requested_mode=requested_mode,
                )

                self.assertEqual(route["route_type"], ROUTE_BLOCKED)
                self.assertEqual(
                    route["download_strategy"],
                    "browser_direct_relay_blocked",
                )
                self.assertEqual(
                    route["error_code"],
                    "BROWSER_DIRECT_RELAY_NOT_ALLOWED",
                )
                self.assertEqual(route["server_egress_estimated_bytes"], 0)

    def test_active_policy_cannot_move_direct_video_to_server_data_plane(self):
        item = build_tiantian_item()
        direct_route = decide_download_route(
            self.request,
            [item],
            requested_mode="auto",
        )

        for policy_route in (ROUTE_PROXY, ROUTE_ZIP):
            with self.subTest(policy_route=policy_route), patch(
                "xhs_download.policy.engine.decide_final_policy",
                return_value=PolicyDecision(
                    allow_download=True,
                    route=policy_route,
                    provider="tiantianjiexi",
                    cost_level="high",
                    confidence=1.0,
                    active=True,
                ),
            ):
                route = apply_policy_decision_to_route(
                    self.request,
                    [item],
                    direct_route,
                    requested_mode="auto",
                )

            self.assertEqual(route["route_type"], ROUTE_DIRECT)
            self.assertEqual(
                route["download_strategy"],
                "browser_direct_provider_video",
            )
            self.assertEqual(route["server_egress_estimated_bytes"], 0)

    def test_manifest_identity_and_url_tampering_are_rejected(self):
        valid = build_tiantian_item()
        for changes in (
            {"url": "https://v5-default.365yg.com/path/other.mp4"},
            {"provider": "kukutool"},
            {"provider_resource_id": "7611146072161047527"},
            {"provider_cache_key": "douyin:tiantianjiexi:validated:other"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(
                    item_can_browser_streaming_direct({**valid, **changes})
                )

    def test_signed_unexpected_host_and_private_ip_are_rejected(self):
        for url in (
            "https://media.example.com/path/video.mp4",
            "http://127.0.0.1/path/video.mp4",
            "http://10.0.0.1/path/video.mp4",
        ):
            with self.subTest(url=url):
                self.assertFalse(
                    item_can_browser_streaming_direct(
                        build_tiantian_item(url)
                    )
                )

    def test_cookie_and_merge_requirements_block_native_direct(self):
        self.assertFalse(
            item_can_browser_streaming_direct(
                build_tiantian_item(requires_cookie=True)
            )
        )
        self.assertFalse(
            item_can_browser_streaming_direct(
                build_tiantian_item(requires_merge=True)
            )
        )

    def test_kukutool_server_only_remains_an_explicit_relay_constraint(self):
        setting = ProviderRuntimeSetting.load()
        setting.kukutool_download_delivery_mode = (
            KukuToolDownloadDeliveryMode.SERVER_ONLY
        )
        setting.save(
            update_fields=["kukutool_download_delivery_mode", "updated_at"]
        )
        item = build_tiantian_item(
            provider="kukutool",
            asset_id=f"douyin:{AWEME_ID}:video:kukutool",
            provider_cache_key=f"douyin:kukutool:validated:{AWEME_ID}",
        )

        route = decide_download_route(
            self.request,
            [item],
            requested_mode="auto",
        )

        self.assertNotEqual(route["route_type"], ROUTE_DIRECT)


class BrowserDirectRouteViewTests(TestCase):
    def setUp(self):
        ProviderRuntimeSetting.load()
        DownloadRuntimeSetting.load()
        self.client = Client()

    def route_payload(self, item=None):
        return {
            "media_items": [item or build_tiantian_item()],
            "base_filename": "douyin-direct",
            "download_mode": "auto",
            "route_only": True,
        }

    @patch("xhs_download.views.build_single_attachment_response")
    @patch("xhs_download.views.fetch_media_response")
    def test_direct_route_never_opens_or_prepares_media_body(
        self,
        mock_fetch,
        mock_build,
    ):
        response = self.client.post(
            "/media/download-images/",
            data=json.dumps(self.route_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route_type"], ROUTE_DIRECT)
        mock_fetch.assert_not_called()
        mock_build.assert_not_called()

    def test_direct_route_records_selection_without_download_completion(self):
        response = self.client.post(
            "/media/download-images/",
            data=json.dumps(self.route_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        log = DownloadRequestLog.objects.get()
        self.assertEqual(log.download_type, "direct_route_selected")
        self.assertEqual(log.route_type, ROUTE_DIRECT)
        self.assertEqual(log.actual_bytes, 0)

    @patch("xhs_download.views.issue_download_handoff_tokens")
    def test_prepare_rejects_direct_video_without_issuing_server_handoff(
        self,
        mock_issue,
    ):
        response = self.client.post(
            "/media/download-prepare/",
            data=json.dumps(self.route_payload()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error_code"],
            "BROWSER_DIRECT_RELAY_NOT_ALLOWED",
        )
        self.assertNotIn("download_url", response.json())
        self.assertNotIn("status_url", response.json())
        mock_issue.assert_not_called()

    @patch("xhs_download.views.build_single_attachment_response")
    @patch("xhs_download.views.build_download_archive_file")
    def test_server_modes_cannot_transfer_direct_video_body(
        self,
        mock_archive,
        mock_attachment,
    ):
        for download_mode in ("proxy", "server_single", "zip"):
            payload = self.route_payload()
            payload.update({
                "download_mode": download_mode,
                "route_only": False,
            })
            with self.subTest(download_mode=download_mode):
                response = self.client.post(
                    "/media/download-images/",
                    data=json.dumps(payload),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["error_code"],
                    "BROWSER_DIRECT_RELAY_NOT_ALLOWED",
                )

        mock_archive.assert_not_called()
        mock_attachment.assert_not_called()

    def test_tampered_route_request_is_rejected_before_direct_handoff(self):
        item = build_tiantian_item()
        item["url"] = "https://v5-default.365yg.com/path/tampered.mp4"

        with patch("xhs_download.views.fetch_media_response") as mock_fetch:
            response = self.client.post(
                "/media/download-images/",
                data=json.dumps(self.route_payload(item)),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error_code"],
            "PROVIDER_MANIFEST_REJECTED",
        )
        mock_fetch.assert_not_called()
        self.assertNotIn("tampered.mp4", response.content.decode())

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=1)
    def test_expired_manifest_has_distinct_reparse_response(self):
        with patch(
            "xhs_download.services.provider_manifest.signing.time.time",
            return_value=1000,
        ):
            item = build_tiantian_item()

        with patch(
            "xhs_download.services.provider_manifest.signing.time.time",
            return_value=1002,
        ):
            route_response = self.client.post(
                "/media/download-images/",
                data=json.dumps(self.route_payload(item)),
                content_type="application/json",
            )
            prepare_response = self.client.post(
                "/media/download-prepare/",
                data=json.dumps(self.route_payload(item)),
                content_type="application/json",
            )

        for response in (route_response, prepare_response):
            self.assertEqual(response.status_code, 410)
            self.assertEqual(
                response.json()["error_code"],
                "PROVIDER_MANIFEST_EXPIRED",
            )
            self.assertEqual(
                response.json()["message"],
                "解析结果已过期，请重新解析后下载。",
            )
            self.assertNotContains(response, MEDIA_URL, status_code=410)

    def test_route_only_and_normal_auto_use_the_same_direct_decision(self):
        item = build_tiantian_item()
        route_only = self.client.post(
            "/media/download-images/",
            data=json.dumps(self.route_payload(item)),
            content_type="application/json",
        )
        normal = self.client.post(
            "/media/download-images/",
            data=json.dumps(
                {
                    **self.route_payload(item),
                    "route_only": False,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(route_only.status_code, 200)
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(route_only.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(normal.json()["route_type"], ROUTE_DIRECT)
        self.assertEqual(
            route_only.json()["download_strategy"],
            normal.json()["download_strategy"],
        )


class ProviderManifestStatusTests(TestCase):
    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=120)
    def test_verified_manifest_exposes_only_its_expiry_timestamp(self):
        with patch(
            "xhs_download.services.provider_manifest.signing.time.time",
            return_value=1000,
        ):
            item = build_tiantian_item()

        self.assertEqual(
            provider_manifest_expires_at(item["provider_manifest_token"]),
            1120,
        )
        self.assertEqual(provider_manifest_expires_at("forged"), 0)

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=1)
    def test_timestamp_expiry_is_not_classified_as_tampering(self):
        with patch(
            "xhs_download.services.provider_manifest.signing.time.time",
            return_value=1000,
        ):
            item = build_tiantian_item()
        with patch(
            "xhs_download.services.provider_manifest.signing.time.time",
            return_value=1002,
        ):
            status = provider_manifest_validation_status(item)

        self.assertEqual(status, PROVIDER_MANIFEST_EXPIRED)

    def test_forged_manifest_remains_invalid(self):
        item = build_tiantian_item()
        item["provider_manifest_token"] = "forged"

        self.assertEqual(
            provider_manifest_validation_status(item),
            PROVIDER_MANIFEST_INVALID,
        )
