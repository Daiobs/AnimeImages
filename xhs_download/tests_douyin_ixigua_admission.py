from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .services.assets import MediaAsset, ParseResult
from .services.common import (
    XhsDownloadError,
    XhsSecurityError,
    validate_external_url,
)
from .services.downloads import (
    get_media_headers,
    request_public_provider_get_with_retry,
    tiantianjiexi_douyin_manifest_is_allowed,
    validate_response_content_type,
)
from .services.download_routing import (
    item_can_browser_direct_video,
    single_video_can_server_proxy_download,
)
from .services.douyin_continuation import (
    deserialize_douyin_continuation_result,
    serialize_douyin_continuation_result,
)
from .services.platforms.douyin import DOUYIN_HEADERS
from .services.platforms.douyin_media_policy import (
    douyin_media_host_policy,
    validate_douyin_media_url,
)
from .services.platforms.douyin_providers import (
    DouyinTianTianJieXiProvider,
)
from .services.provider_manifest import build_provider_manifest_token


AWEME_ID = "7667796432904990115"
IXIGUA_MEDIA_URL = (
    "https://v3-dy-a-x.ixigua.com/media/controlled.mp4?fixture=fake"
)


def make_tiantianjiexi_item(url=IXIGUA_MEDIA_URL):
    asset_id = f"douyin:{AWEME_ID}:video:tiantianjiexi"
    cache_key = f"douyin:tiantianjiexi:validated:{AWEME_ID}"
    item = {
        "type": "video",
        "url": url,
        "asset_id": asset_id,
        "provider": "tiantianjiexi",
        "provider_resource_id": AWEME_ID,
        "provider_cache_key": cache_key,
    }
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=asset_id,
        provider="tiantianjiexi",
        provider_resource_id=AWEME_ID,
        provider_cache_key=cache_key,
        url=url,
        media_type="video",
    )
    return item


class DouyinIxiguaMediaPolicyTests(SimpleTestCase):
    def test_reviewed_exact_host_and_existing_douyin_cdn_hosts_are_allowed(self):
        for url, expected_policy in (
            (IXIGUA_MEDIA_URL, "ixigua_exact"),
            ("https://v11-default.365yg.com/video.mp4", "existing"),
            ("https://v3-web.douyinvod.com/video.mp4", "existing"),
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_douyin_media_url(url), url)
                self.assertEqual(
                    douyin_media_host_policy(url.split("/", 3)[2]),
                    expected_policy,
                )

    def test_exact_host_match_is_case_insensitive_without_widening_scope(self):
        mixed_case = "https://V3-DY-A-X.IXIGUA.COM/media/video.mp4"

        self.assertEqual(validate_douyin_media_url(mixed_case), mixed_case)
        for rejected in (
            "https://ixigua.com/media/video.mp4",
            "https://www.ixigua.com/media/video.mp4",
            "https://evil.v3-dy-a-x.ixigua.com/media/video.mp4",
            "https://v3-dy-a-x.ixigua.com.evil.example/media/video.mp4",
            "https://evilixigua.com/media/video.mp4",
            "https://ixigua-example.com/media/video.mp4",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(XhsSecurityError):
                    validate_douyin_media_url(rejected)

    def test_exact_host_rejects_protocol_userinfo_trailing_dot_and_port_bypasses(self):
        for rejected in (
            "http://v3-dy-a-x.ixigua.com/media/video.mp4",
            "https://user@v3-dy-a-x.ixigua.com/media/video.mp4",
            "https://user:password@v3-dy-a-x.ixigua.com/media/video.mp4",
            "https://v3-dy-a-x.ixigua.com./media/video.mp4",
            "https://v3-dy-a-x.ixigua.com:444/media/video.mp4",
            "https://v3-dy-a-x.ixigua.com:notaport/media/video.mp4",
            "https://127.0.0.1/media/video.mp4",
            "https://[::1]/media/video.mp4",
            "not-a-url",
        ):
            with self.subTest(rejected=rejected):
                with self.assertRaises(XhsSecurityError):
                    validate_douyin_media_url(rejected)

    def test_generic_external_policy_does_not_gain_ixigua_access(self):
        with self.assertRaises(XhsSecurityError):
            validate_external_url(
                IXIGUA_MEDIA_URL,
                ("googlevideo.com",),
            )


class DouyinIxiguaProviderAdmissionTests(SimpleTestCase):
    def setUp(self):
        self.adapter = SimpleNamespace(
            media_host_suffixes=("365yg.com", "douyinvod.com"),
        )
        self.provider = DouyinTianTianJieXiProvider(
            provider_settings=SimpleNamespace(),
        )

    def test_tiantian_provider_accepts_reviewed_exact_host_candidate(self):
        candidates = self.provider.build_candidates(
            {
                "videos": [
                    {
                        "url": IXIGUA_MEDIA_URL,
                        "quality": "原画",
                        "size": 110_691_390,
                    }
                ]
            },
            self.adapter,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].url, IXIGUA_MEDIA_URL)
        self.assertEqual(candidates[0].quality_label, "原画")

    def test_tiantian_provider_rejects_ixigua_lookalike_candidate(self):
        candidates = self.provider.build_candidates(
            {
                "videos": [
                    {
                        "url": (
                            "https://v3-dy-a-x.ixigua.com.evil.example/"
                            "media/video.mp4"
                        ),
                        "quality": "原画",
                    }
                ]
            },
            self.adapter,
        )

        self.assertEqual(candidates, ())

    def test_signed_manifest_and_douyin_headers_accept_reviewed_host(self):
        item = make_tiantianjiexi_item()

        self.assertTrue(tiantianjiexi_douyin_manifest_is_allowed(item))
        self.assertIs(get_media_headers(IXIGUA_MEDIA_URL), DOUYIN_HEADERS)
        self.assertTrue(item_can_browser_direct_video(item))
        self.assertFalse(single_video_can_server_proxy_download(item))

    def test_continuation_round_trip_preserves_reviewed_host(self):
        item = make_tiantianjiexi_item()
        result = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="controlled",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=IXIGUA_MEDIA_URL,
                    platform="douyin",
                    provider="tiantianjiexi",
                    asset_id=item["asset_id"],
                    provider_resource_id=item["provider_resource_id"],
                    provider_cache_key=item["provider_cache_key"],
                    provider_manifest_token=item["provider_manifest_token"],
                ),
            ),
        )

        restored = deserialize_douyin_continuation_result(
            serialize_douyin_continuation_result(result)
        )

        self.assertEqual(restored.video_assets[0].url, IXIGUA_MEDIA_URL)

    @patch("xhs_download.services.downloads.requests.get")
    def test_signed_request_rejects_redirect_to_untrusted_host(self, mock_get):
        response = Mock(
            status_code=302,
            headers={"Location": "https://attacker.example/video.mp4"},
        )
        response.close = Mock()
        mock_get.return_value = response

        with self.assertRaises(XhsDownloadError):
            request_public_provider_get_with_retry(
                IXIGUA_MEDIA_URL,
                retries=1,
                provider_item=make_tiantianjiexi_item(),
            )

        mock_get.assert_called_once()
        response.close.assert_called_once()

    def test_html_and_json_media_types_keep_existing_rejection_gate(self):
        for content_type in ("text/html", "application/json"):
            with self.subTest(content_type=content_type):
                with self.assertRaises(XhsDownloadError):
                    validate_response_content_type(content_type, "video")
