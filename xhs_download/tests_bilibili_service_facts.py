"""Native service facts must survive the real HTTP client and cache boundary."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.common import XhsDownloadError
from .services.download_handoff import issue_download_handoff_tokens, load_download_handoff
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms.bilibili_service_result import build_service_result
from .services.platforms.kukutool_generic import KukutoolResultNormalizer, source_key_for_url
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient, TianTianJieXiGenericProvider, TianTianJieXiResultNormalizer,
    build_payload_cache_key,
)
from .services.registry import parse_media_page
from .services.service_format_selection import selected_legacy_descriptor
from .services.upstream_candidates import resolve_candidate_record

SOURCE = "https://www.bilibili.com/video/BV1J4JX6PEMV/"
MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"
OTHER_MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/other.mp4"
PROVIDER = "tiantianjiexi_generic"


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="isolated-fixture", TIANTIANJIEXI_PASSWORD="fixture",
    TIANTIANJIEXI_API_BASE_URL="https://service.example.invalid/parse",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class BilibiliServiceFactsTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root", return_value=Path(folder)))
        self.stack.enter_context(patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        self.stack.enter_context(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("unexpected HTTP")))
        self.response = Mock()
        self.post = self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.requests.post",
            return_value=self.response))

    def client_payload(self, data, *, source=SOURCE, envelope=None):
        self.response.json.return_value = {"code": 200, "data": data, **(envelope or {})}
        return TianTianJieXiApiClient().parse_url(source)

    def cached_again(self, payload, source=SOURCE):
        provider = TianTianJieXiGenericProvider()
        key = source_key_for_url(source)
        provider.cache_payload(key, payload)
        cached = provider.get_cached_payload(key)
        self.assertIsNotNone(cached)
        return TianTianJieXiResultNormalizer.normalize_payload(cached)

    def test_client_and_cache_retain_identity_conflicts_without_private_response(self):
        cases = (
            ({"bvid": "BV1xx411c7mD"}, SOURCE, "identity"),
            ({"aid": 456}, "https://www.bilibili.com/video/av123/", "identity"),
            ({"bvid": "BV1J4JX6PEMV", "p": 2}, SOURCE, "page"),
            ({"p": 0}, SOURCE, "page"),
            ({"canonical_url": SOURCE + "?p=2&token=private-marker"}, SOURCE, "page"),
            ({"videos": [{"url": MEDIA, "bvid": "BV1xx411c7mD"}]}, SOURCE, "identity"),
        )
        for fields, source, reason in cases:
            with self.subTest(fields=fields):
                payload = self.client_payload(
                    {"video": MEDIA, "password": "private-marker", **fields}, source=source)
                self.assertNotIn("private-marker", repr(payload))
                for normalized in (payload, self.cached_again(payload, source),
                                   TianTianJieXiResultNormalizer.normalize_payload(dict(payload))):
                    with self.assertRaisesMessage(ValueError, f"bilibili_service_{reason}_mismatch"):
                        build_service_result(normalized, source)

    def test_outer_envelope_evidence_is_not_lost_when_data_is_unwrapped(self):
        for fields in ({"bvid": "BV1xx411c7mD"}, {"p": 2}):
            with self.subTest(fields=fields):
                payload = self.client_payload({"video": MEDIA}, envelope=fields)
                with self.assertRaises(ValueError):
                    build_service_result(self.cached_again(payload), SOURCE)

    def test_identified_video_objects_keep_part_conflicts_through_client_and_cache(self):
        for container in ("videos", "video"):
            with self.subTest(container=container):
                cache.clear()
                self.post.reset_mock()
                video = {"bvid": "BV1J4JX6PEMV", "p": 2, "url": MEDIA}
                self.response.json.return_value = {"data": {
                    container: [video] if container == "videos" else video}}
                error = XhsDownloadError("HTTP 412", http_status=412)
                with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                           side_effect=error), patch(
                               "xhs_download.services.platforms.bilibili_service_result."
                               "build_provider_manifest_token") as sign:
                    for _ in range(2):
                        with self.assertRaises(XhsDownloadError):
                            parse_media_page(SOURCE, bilibili_service_authorizer=lambda: (PROVIDER,))
                    sign.assert_not_called()
                self.post.assert_called_once()

    def test_valid_identity_reaches_manifest_descriptor_and_handoff(self):
        for fields, source in (
            ({"bvid": "BV1J4JX6PEMV", "p": 1}, SOURCE),
            ({"aid": 123, "p": 1}, "https://www.bilibili.com/video/av123/"),
            ({"bvid": "BV1J4JX6PEMV", "aid": 123}, SOURCE),
        ):
            with self.subTest(fields=fields):
                payload = self.client_payload({"video": MEDIA, "title": "Fixture", **fields}, source=source)
                result = build_service_result(self.cached_again(payload, source), source)
                self.assertEqual(result.metadata["bilibili_service_identity_basis"], "payload_identity")
                candidate = result.metadata["upstream_candidates"][0]
                record = resolve_candidate_record(candidate["resource_token"], candidate["candidate_id"])
                item = selected_legacy_descriptor(record)
                self.assertEqual(item["url"], MEDIA)
                self.assertTrue(provider_manifest_allows_public_download(item))
                tokens = issue_download_handoff_tokens(
                    item=item, base_filename=result.base_filename, client_identity="isolated")
                loaded = load_download_handoff(tokens.stream_token, client_identity="isolated")
                self.assertEqual(loaded.item["url"], MEDIA)
                self.assertEqual(loaded.item["asset_id"], item["asset_id"])

    def test_parent_media_denials_survive_client_and_cache(self):
        for facts in ({"acodec": "none"}, {"vcodec": "none"}, {"video_tracks": "absent"},
                      {"audio_tracks": "absent"}, {"protocol": "dash"}):
            with self.subTest(facts=facts):
                payload = self.client_payload({"video": MEDIA, **facts})
                for normalized in (payload, self.cached_again(payload),
                                   TianTianJieXiResultNormalizer.normalize_payload(dict(payload))):
                    row = normalized["_service_format_rows"][0]
                    for key, value in facts.items():
                        self.assertEqual(row[key], value)
                    with self.assertRaisesMessage(ValueError, "bilibili_service_no_complete_video"):
                        build_service_result(normalized, SOURCE)

    def test_real_registry_client_rejects_conflicts_before_signing(self):
        cases = ({"bvid": "BV1xx411c7mD"}, {"p": 2}, {"aid": 456},
                 {"acodec": "none"}, {"video_tracks": "absent"}, {"protocol": "dash"})
        for fields in cases:
            with self.subTest(fields=fields):
                cache.clear()
                self.post.reset_mock()
                source = "https://www.bilibili.com/video/av123/" if "aid" in fields else SOURCE
                self.response.json.return_value = {"code": 200, "data": {"video": MEDIA, **fields}}
                error = XhsDownloadError("HTTP 412", http_status=412)
                with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                           side_effect=error), patch(
                               "xhs_download.services.platforms.bilibili_service_result."
                               "build_provider_manifest_token") as sign:
                    with self.assertRaises(XhsDownloadError) as raised:
                        parse_media_page(source, bilibili_service_authorizer=lambda: (PROVIDER,))
                    self.assertIs(raised.exception, error)
                    sign.assert_not_called()
                self.post.assert_called_once()

    def test_old_normalized_cache_is_refreshed_only_for_bilibili(self):
        provider = TianTianJieXiGenericProvider()
        old = self.client_payload({"video": MEDIA})
        old.pop("_service_facts_version")
        self.post.reset_mock()
        provider.cache_payload(source_key_for_url(SOURCE), old)
        other_source = "https://www.douyin.com/video/1234567890123456789"
        other_key = source_key_for_url(other_source)
        provider.cache_payload(other_key, old)
        for stale in (old, dict(old), TianTianJieXiResultNormalizer.normalize_payload(dict(old))):
            with self.assertRaisesMessage(ValueError, "bilibili_service_facts_refresh_required"):
                build_service_result(stale, SOURCE)
        self.response.json.return_value = {"code": 200, "data": {
            "video": MEDIA, "bvid": "BV1J4JX6PEMV", "p": 1}}
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=XhsDownloadError("HTTP 412", http_status=412)):
            first = parse_media_page(SOURCE, bilibili_service_authorizer=lambda: (PROVIDER,))
            second = parse_media_page(SOURCE, bilibili_service_authorizer=lambda: (PROVIDER,))
        self.post.assert_called_once()
        self.assertEqual(first.metadata["bilibili_service_identity_basis"], "payload_identity")
        self.assertEqual(second.metadata["bilibili_service_identity_basis"], "payload_identity")
        self.assertEqual(cache.get(build_payload_cache_key(other_key)), old)
        self.assertEqual(provider.parse(other_source), old)
        self.post.assert_called_once()

    def test_refresh_cannot_sign_wrong_identity_from_new_client_response(self):
        old = self.client_payload({"video": MEDIA})
        old.pop("_service_facts_version")
        provider = TianTianJieXiGenericProvider()
        provider.cache_payload(source_key_for_url(SOURCE), old)
        self.post.reset_mock()
        self.response.json.return_value = {"code": 200, "data": {
            "video": MEDIA, "bvid": "BV1xx411c7mD"}}
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=XhsDownloadError("HTTP 412", http_status=412)), patch(
                       "xhs_download.services.platforms.bilibili_service_result."
                       "build_provider_manifest_token") as sign:
            with self.assertRaises(XhsDownloadError):
                parse_media_page(SOURCE, bilibili_service_authorizer=lambda: (PROVIDER,))
            sign.assert_not_called()
        self.post.assert_called_once()

    def test_parent_facts_apply_to_variants_but_not_independent_videos(self):
        data = {"acodec": "none", "protocol": "dash", "videos": [
            {"acodec": "none", "streams": [{"url": MEDIA}]},
            {"streams": [{"url": OTHER_MEDIA}]},
        ]}
        payload = self.client_payload(data)
        rows = {row["url"]: row for row in payload["_service_format_rows"]}
        self.assertEqual(rows[MEDIA]["acodec"], "none")
        self.assertNotIn("acodec", rows[OTHER_MEDIA])
        self.assertNotIn("protocol", rows[OTHER_MEDIA])
        result = build_service_result(self.cached_again(payload), SOURCE)
        self.assertEqual([asset.url for asset in result.assets], [OTHER_MEDIA])
        restricted = next(c for c in result.metadata["upstream_candidates"] if not c["legacy_asset_id"])
        with self.assertRaises(ValueError):
            selected_legacy_descriptor(resolve_candidate_record(
                restricted["resource_token"], restricted["candidate_id"]))

    def test_kukutool_shared_expansion_preserves_scoped_facts(self):
        for data in (
            {"video": MEDIA, "video_tracks": "absent"},
            {"videos": [{"video_tracks": "absent", "video_fullinfo": [{"url": MEDIA}]},
                        {"video_fullinfo": [{"url": OTHER_MEDIA}]}]},
        ):
            normalized = KukutoolResultNormalizer.normalize_payload(data)
            rows = {row["url"]: row for row in normalized["_service_format_rows"]}
            self.assertEqual(rows[MEDIA]["video_tracks"], "absent")
            if OTHER_MEDIA in rows:
                self.assertNotIn("video_tracks", rows[OTHER_MEDIA])
            self.assertEqual(KukutoolResultNormalizer.normalize_payload(normalized), normalized)

    def test_format_fields_are_not_mistaken_for_work_identity(self):
        payload = self.client_payload({"video": MEDIA, "id": "BV1xx411c7mD", "page": 2,
                                       "videos": [{"streams": [{"url": MEDIA, "p": 4}]}]})
        result = build_service_result(payload, SOURCE)
        self.assertEqual(result.metadata["bilibili_service_identity_basis"], "request_context")

    def test_child_media_facts_do_not_spread_to_sibling_variants(self):
        payload = self.client_payload({"videos": [{"streams": [
            {"url": MEDIA, "acodec": "none"}, {"url": OTHER_MEDIA},
        ]}]})
        rows = {row["url"]: row for row in payload["_service_format_rows"]}
        self.assertEqual(rows[MEDIA]["acodec"], "none")
        self.assertNotIn("acodec", rows[OTHER_MEDIA])
        self.assertEqual([asset.url for asset in build_service_result(payload, SOURCE).assets],
                         [OTHER_MEDIA])
