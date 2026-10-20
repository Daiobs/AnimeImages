from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.common import XhsDownloadError, XhsParseError
from .services.downloads import (
    get_media_headers, provider_manifest_allows_public_download,
    request_public_provider_get_with_retry, validate_provider_signed_download_url,
)
from .services.platforms.bilibili import BILIBILI_HEADERS
from .services.platforms.bilibili_service_result import build_service_result, source_url_for_item
from .services.platforms.third_party import GenericThirdPartyAdapter
from .services.provider_strategy import build_download_strategy
from .services.service_format_selection import (
    capability, selected_descriptor, selected_legacy_descriptor,
)
from .services.upstream_candidates import resolve_candidate_record

SOURCE = "https://www.bilibili.com/video/BV1xx411c7mD/"
MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class BilibiliServiceResultTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        guard = patch("socket.getaddrinfo", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        validator = patch("xhs_download.services.upstream_candidates.validate_public_download_url",
                          side_effect=lambda url, *args, **kwargs: url)
        validator.start()
        self.addCleanup(validator.stop)

    def result(self, payload=None, source=SOURCE):
        return build_service_result(payload or {"video": MEDIA, "title": "Title"}, source)

    def record(self, candidate):
        return resolve_candidate_record(candidate["resource_token"], candidate["candidate_id"])

    def test_result_retains_title_identity_and_native_headers(self):
        result = self.result({"data": {"video": MEDIA, "desc": "Description title"}})
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.base_filename, "Description title")
        self.assertEqual(result.metadata["bilibili_video_id"], "BV1xx411c7mD")
        self.assertEqual(result.metadata["bilibili_page"], 1)
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "request_context")
        asset = result.video_assets[0]
        self.assertEqual(asset.provider, "tiantianjiexi_generic")
        self.assertEqual(asset.headers, BILIBILI_HEADERS)
        self.assertEqual(get_media_headers(asset.url, result.platform), BILIBILI_HEADERS)
        self.assertEqual(build_download_strategy(asset, result_platform=result.platform)["download_strategy"],
                         "server_attachment")
        self.assertTrue(provider_manifest_allows_public_download(asset.to_download_item()))
        self.assertEqual(source_url_for_item(asset.to_download_item()), SOURCE)
        self.assertIn(":BV1xx411c7mD:p1:", asset.asset_id)

    def test_title_takes_priority_and_av_is_retained(self):
        result = self.result({"video": MEDIA, "title": "Title", "desc": "Other", "aid": 123},
                             "https://www.bilibili.com/video/av123/")
        self.assertEqual(result.base_filename, "Title")
        self.assertEqual(result.metadata["bilibili_video_id"], "av123")
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "payload_identity")

    def test_explicit_identity_or_page_mismatch_rejected(self):
        for fields in ({"bvid": "BV1yy411c7mD"}, {"p": 2},
                       {"source_url": SOURCE + "?p=2"},
                       {"canonical_url": "https://www.bilibili.com/video/BV1yy411c7mD/"},
                       {"videos": [{"url": MEDIA, "bvid": "BV1yy411c7mD"}]}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.result({"video": MEDIA, **fields})

    def test_bv_and_av_evidence_only_compares_same_kind(self):
        result = self.result({"video": MEDIA, "bvid": "BV1xx411c7mD", "aid": 123})
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "payload_identity")
        result = self.result({"video": MEDIA, "bvid": "BV1xx411c7mD", "aid": 123},
                             "https://www.bilibili.com/video/av123/")
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "payload_identity")
        result = self.result({"video": MEDIA, "aid": 123})
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "request_context")
        with self.assertRaisesMessage(ValueError, "bilibili_service_identity_mismatch"):
            self.result({"video": MEDIA, "aid": 456}, "https://www.bilibili.com/video/av123/")

    def test_format_id_and_page_are_not_source_identity(self):
        result = self.result({"id": "BV1yy411c7mD", "page": 2, "videos": [{"streams": [
            {"url": MEDIA, "id": "BV1yy411c7mD", "page": 3, "p": 4},
        ]}]})
        self.assertEqual(len(result.video_assets), 1)
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "request_context")
        with self.assertRaisesMessage(ValueError, "bilibili_service_page_mismatch"):
            self.result({"data": {"video": MEDIA, "p": 2}})

    def test_invalid_sources_never_get_native_authority(self):
        for source in (SOURCE + "?p=2", "https://b23.tv/abcd",
                       "https://evil.example/video/BV1xx411c7mD/"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.result(source=source)

    def test_cover_or_audio_only_cannot_take_over_failure(self):
        for payload in ({"cover": "https://i0.hdslb.com/cover.jpg"},
                        {"images": ["https://i0.hdslb.com/cover.jpg"]},
                        {"audio": "https://upos.bilivideo.com/audio.m4a"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                build_service_result(payload, SOURCE)

    def test_all_complete_variants_receive_distinct_manifest_bindings(self):
        result = self.result({"videos": [{"streams": [
            {"url": MEDIA, "quality": "720P"},
            {"url": MEDIA.replace("video.mp4", "large.mp4"), "quality": "1080P"},
        ]}]})
        self.assertEqual(len(result.video_assets), 2)
        candidates = result.metadata["upstream_candidates"]
        self.assertEqual(len({c["work_id"] for c in candidates}), 1)
        self.assertEqual(len({a.asset_id for a in result.video_assets}), 2)
        for candidate in candidates:
            record = self.record(candidate)
            item = selected_legacy_descriptor(record)
            self.assertEqual(item["url"], record["url"])
            self.assertEqual(item["asset_id"], candidate["legacy_asset_id"])
            self.assertTrue(provider_manifest_allows_public_download(item))
            with self.assertRaisesMessage(ValueError, "service_legacy_delivery_required"):
                selected_descriptor(record)

    def test_cross_manifest_candidate_and_work_rejected(self):
        one, two = [self.result({"video": url}) for url in (MEDIA, MEDIA.replace("video.mp4", "other.mp4"))]
        record = self.record(one.metadata["upstream_candidates"][0])
        foreign = two.video_assets[0].to_download_item()
        with self.assertRaises(ValueError):
            selected_legacy_descriptor({**record, "service_legacy_item": foreign})
        item = one.video_assets[0].to_download_item()
        self.assertFalse(provider_manifest_allows_public_download(
            {**item, "provider_manifest_token": foreign["provider_manifest_token"]}))
        self.assertFalse(provider_manifest_allows_public_download({**item, "url": foreign["url"]}))
        with self.assertRaises(ValueError):
            selected_legacy_descriptor({**record, "source_url": SOURCE + "?p=2"})

    def test_expired_manifest_and_candidate_are_rejected(self):
        result = self.result()
        candidate = result.metadata["upstream_candidates"][0]
        record = self.record(candidate)
        with override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=1):
            with patch("django.core.signing.time.time", return_value=10**11):
                self.assertFalse(provider_manifest_allows_public_download(result.video_assets[0].to_download_item()))
                with self.assertRaises(ValueError):
                    selected_legacy_descriptor(record)
        with patch("xhs_download.services.upstream_candidates.time.time", return_value=10**11):
            with self.assertRaises(XhsParseError):
                self.record(candidate)

    def test_restricted_tracks_are_candidates_without_legacy_authority(self):
        for facts in ({"requires_merge": True}, {"acodec": "none"}, {"audio_tracks": "absent"},
                      {"requires_cookie": True}, {"has_drm": True}, {"protocol": "dash"},
                      {"url": MEDIA.replace(".mp4", ".m4s")}):
            with self.subTest(facts=facts):
                restricted = {"url": MEDIA, **facts}
                with self.assertRaisesMessage(ValueError, "bilibili_service_no_complete_video"):
                    self.result({"videos": [restricted]})
                result = self.result({"videos": [
                    restricted, {"url": MEDIA.replace("video.mp4", "complete.mp4")},
                ]})
                self.assertEqual(len(result.assets), 1)
                candidate = next(c for c in result.metadata["upstream_candidates"] if not c["legacy_asset_id"])
                self.assertFalse(capability(candidate)["can_download"])
                with self.assertRaises(ValueError):
                    selected_legacy_descriptor(self.record(candidate))

    def test_restricted_alias_cannot_borrow_complete_file_manifest(self):
        result = self.result({"videos": [{"streams": [
            {"url": MEDIA, "quality": "720P"}, {"url": MEDIA, "quality": "1080P", "acodec": "none"},
        ]}]})
        silent = next(c for c in result.metadata["upstream_candidates"] if c["audio_tracks"] == "absent")
        self.assertFalse(silent["legacy_asset_id"])
        self.assertFalse(capability(silent)["can_download"])

    def test_same_url_across_work_groups_keeps_independent_legacy_assets(self):
        result = self.result({"videos": [{"url": MEDIA}, {"url": MEDIA}]})
        self.assertEqual(len(result.assets), 2)
        self.assertEqual(len({a.asset_id for a in result.assets}), 2)
        self.assertTrue(all(not c["legacy_asset_id"] for c in result.metadata["upstream_candidates"]))

    def test_unreviewed_hosts_and_credentials_rejected(self):
        for url in ("https://bilivideo.com.evil.example/file.mp4",
                    "https://evil.example/file.mp4", "https://user@upos.bilivideo.com/file.mp4",
                    "http://upos.bilivideo.com:8443/file.mp4", "https://upos.bilivideo.com:8443/file.mp4"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.result({"video": url})

    def test_existing_server_request_uses_manifest_and_bilibili_headers(self):
        item = self.result().video_assets[0].to_download_item()
        response = Mock(status_code=200, url=MEDIA)
        with patch("xhs_download.services.downloads.requests.get", return_value=response) as get:
            self.assertIs(request_public_provider_get_with_retry(MEDIA, provider_item=item), response)
            self.assertEqual(get.call_args.kwargs["headers"], BILIBILI_HEADERS)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with self.assertRaises(XhsDownloadError):
            validate_provider_signed_download_url(item, MEDIA.replace("video.mp4", "other.mp4"), "rejected")

    def test_service_http_file_keeps_original_scheme_and_existing_attachment_delivery(self):
        url = MEDIA.replace("https:", "http:")
        result = self.result({"desc": "Bilibili title", "videos": [{"url": url, "streams": []}]})
        candidate = result.metadata["upstream_candidates"][0]
        item = selected_legacy_descriptor(self.record(candidate))
        self.assertEqual(item["url"], url)
        self.assertEqual(item["selection_delivery"], "legacy_manifest")
        self.assertTrue(provider_manifest_allows_public_download(item))
        with self.assertRaises(ValueError):
            selected_descriptor(self.record(candidate))

    def test_native_strategy_and_global_generic_matching_unchanged(self):
        from .services.assets import MediaAsset
        native = MediaAsset(media_type="video", platform="bilibili", url=MEDIA)
        strategy = build_download_strategy(native, result_platform="bilibili")
        self.assertEqual(strategy["download_strategy"], "browser_direct")
        self.assertEqual(strategy["fallback_strategy"], "server_attachment")
        self.assertIsNone(GenericThirdPartyAdapter().get_rule_for_url(SOURCE))
