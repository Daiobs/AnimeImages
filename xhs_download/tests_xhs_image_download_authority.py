from django.test import SimpleTestCase

from .services.platforms.xhs_image_download_authority import (
    XhsDownloadableImageAuthorityError,
    classify_xhs_image_candidate,
    validate_xhs_downloadable_image_asset,
)
from .services.provider_manifest import build_provider_manifest_token


def authority_item(*, url, extension="jpg", provider="xhs_livephoto_preview_v1"):
    asset_id = "xhs:0123456789abcdef01234567:i0:still"
    provider_resource_id = "resource-0"
    provider_cache_key = "xiaohongshu:livephoto:0"
    return {
        "type": "image",
        "url": url,
        "extension": extension,
        "asset_id": asset_id,
        "provider": provider,
        "provider_resource_id": provider_resource_id,
        "provider_cache_key": provider_cache_key,
        "provider_manifest_token": build_provider_manifest_token(
            asset_id=asset_id,
            provider=provider,
            provider_resource_id=provider_resource_id,
            provider_cache_key=provider_cache_key,
            url=url,
            media_type="image",
            source_platform="xiaohongshu",
        ),
        "source_platform": "xiaohongshu",
    }


class XhsImageDownloadAuthorityTests(SimpleTestCase):
    def test_ordinary_image_candidate_classes_remain_bounded(self):
        cases = {
            "https://ci.xiaohongshu.com/notes_pre_post/resource": "native_ci_exact",
            "https://sns-webpic-qc.xhscdn.com/path/resource": "native_sns_webpic",
            "https://sns-img-qc.xhscdn.com/path/resource": "native_sns_img",
            "https://media-v1.xhscdn.com/spectrum/resource": "native_known_resource_path",
        }
        for url, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_xhs_image_candidate(url).candidate_class,
                    expected,
                )

    def test_root_query_only_wildcard_and_lookalike_candidates_are_rejected(self):
        for url in (
            "https://sns-webpic-qc.xhscdn.com/",
            "https://sns-webpic-qc.xhscdn.com/?imageView2/format/jpg",
            "https://unreviewed.xhscdn.com/random/resource",
            "https://sns-img-qc.xhscdn.com.evil.example/path/resource",
            "ftp://sns-img-qc.xhscdn.com/path/resource",
        ):
            with self.subTest(url=url):
                self.assertIsNone(classify_xhs_image_candidate(url))

    def test_native_ordinary_authority_accepts_safe_formats_with_browser_magic_gate(self):
        for extension in ("jpg", "png", "gif", "webp", "avif", "heic"):
            with self.subTest(extension=extension):
                authority = validate_xhs_downloadable_image_asset(
                    authority_item(
                        url="https://ci.xiaohongshu.com/notes_pre_post/resource",
                        extension=extension,
                    )
                )
                self.assertEqual(authority.provider_class, "native")
                self.assertEqual(authority.candidate_class, "native_ci_exact")
                self.assertEqual(authority.extension, extension)
                self.assertTrue(authority.browser_magic_required)

    def test_parsehub_raw_authority_remains_exact_host_only(self):
        authority = validate_xhs_downloadable_image_asset(
            authority_item(
                url="https://sns-img-hw.xhscdn.com/resource-key",
                extension="heic",
                provider="parsehub_xhs_images",
            )
        )
        self.assertEqual(authority.provider_class, "parsehub_raw")
        self.assertEqual(authority.candidate_class, "parsehub_raw_exact")

    def test_manifest_tampering_url_substitution_and_unknown_format_fail_closed(self):
        valid = authority_item(
            url="https://ci.xiaohongshu.com/notes_pre_post/resource"
        )
        tampered = dict(valid, url="https://ci.xiaohongshu.com/notes_pre_post/other")
        unsupported = authority_item(
            url="https://ci.xiaohongshu.com/notes_pre_post/resource",
            extension="bmp",
        )
        unsafe = authority_item(
            url="https://unreviewed.xhscdn.com/random/resource",
        )
        for item in (tampered, unsupported, unsafe):
            with self.subTest(item=item["extension"]):
                with self.assertRaises(XhsDownloadableImageAuthorityError):
                    validate_xhs_downloadable_image_asset(item)
