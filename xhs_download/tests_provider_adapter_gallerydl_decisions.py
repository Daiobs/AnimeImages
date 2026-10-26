"""Bounded P4 regressions for unified-pipeline section 9 decisions.

Synthetic snapshots, grounded in installed gallery-dl 1.32.4 source:
extractor/artstation.py (same-num preview), pinterest.py (ytdl HLS),
imgur.py (per-image album directories), uploadir.py (POST body), and
exception.HttpError (quoted status). No gallery_dl import or live capture.
Optional tracks/quality facts below are explicit test inputs, not claims
that those extractors emit them. Metadata is copied at each message boundary.
"""
from copy import deepcopy
from unittest import TestCase

from xhs_download.services.pipeline import (
    Failure, FailureKind, Kind, Method, Tracks, Work, decide_delivery,
    validate_work,
)
from xhs_download.services.provider_adapters.gallerydl import GalleryDlMapper
from xhs_download.tests_provider_adapters import deny_map_io


SOURCE = "https://source.example/work/synthetic"
MEDIA = "https://cdn.example/media.mp4"


def stream(directory, *resources):
    return [[2, "", deepcopy(directory)], *[
        [3, url, deepcopy(metadata)] for url, metadata in resources
    ]]


class GalleryDlDecisionRegressionTests(TestCase):
    def map_raw(self, raw, source_url=SOURCE):
        before = deepcopy(raw)
        with deny_map_io():
            result = GalleryDlMapper().map(raw, source_url)
        self.assertEqual(raw, before, "mapping changed native input")
        return result

    def map_work(self, raw, source_url=SOURCE):
        result = self.map_raw(raw, source_url)
        self.assertIsInstance(result, Work)
        self.assertEqual(validate_work(result), ())
        return result

    def assert_failure(self, raw, kind, source_url=SOURCE):
        result = self.map_raw(raw, source_url)
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, kind)
        self.assertEqual(set(vars(result)), {"kind", "code"})
        self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
        return result

    def test_artstation_same_num_video_and_preview_are_two_items(self):
        metadata = {
            "id": 123, "num": 1,
            "asset": {"id": 456, "has_embedded_player": True, "has_image": True},
        }
        preview = "https://cdn.example/preview.jpg"
        work = self.map_work(stream(metadata,
            (MEDIA, {**metadata, "extension": "mp4"}),
            (preview, {**metadata, "extension": "jpg"}),
        ))
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.IMAGE])
        self.assertEqual([len(item.variants) for item in work.items], [1, 1])
        self.assertEqual([item.default_variant.url for item in work.items], [MEDIA, preview])
        self.assertEqual(len({item.item_id for item in work.items}), 2)

    def test_same_kind_same_identity_still_groups_variants(self):
        for kind, extension in ((Kind.IMAGE, "jpg"), (Kind.VIDEO, "mp4")):
            with self.subTest(kind=kind):
                urls = [f"https://cdn.example/{name}.{extension}" for name in ("low", "high")]
                work = self.map_work(stream({"id": 123},
                    (urls[0], {"id": 123, "num": 1, "extension": extension, "height": 720}),
                    (urls[1], {"id": 123, "num": 1, "extension": extension, "height": 1080}),
                ))
                self.assertEqual(len(work.items), 1)
                item = work.items[0]
                self.assertIs(item.kind, kind)
                self.assertEqual([v.url for v in item.variants], urls)
                self.assertEqual(item.default_variant.url, urls[1])

    def test_pinterest_native_ytdl_hls_is_retained_as_segmented(self):
        url = "https://cdn.example/a.m3u8"
        metadata = {"id": "pin-1", "num": 1, "extension": "mp4", "_ytdl_manifest": "hls"}
        work = self.map_work(stream({"id": "pin-1"}, ("ytdl:" + url, metadata)))
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertIs(item.kind, Kind.VIDEO)
        self.assertEqual(len(item.variants), 1)
        variant = item.variants[0]
        self.assertEqual(variant.url, url)
        self.assertTrue(variant.segmented)
        delivery = decide_delivery(item.kind, variant)
        self.assertEqual((delivery.method, delivery.reason), (Method.NONE, "SEGMENTED_STREAM"))

    def test_ytdl_webpage_without_manifest_remains_invalid(self):
        raw = stream({"id": 123},
            ("ytdl:https://source.example/watch/123", {"id": 123, "extension": "mp4"}),
        )
        self.assert_failure(raw, FailureKind.INVALID_RESULT)

    def test_url_album_must_match_directory_album(self):
        raw = stream({"id": "image-1", "album": {"id": "A"}},
            ("https://cdn.example/first.jpg", {"id": "image-1", "album": {"id": "B"}}),
        )
        self.assert_failure(raw, FailureKind.INVALID_RESULT)

    def test_same_album_accepts_distinct_per_image_directories(self):
        raw = []
        urls = []
        for num in (1, 2):
            metadata = {"id": f"image-{num}", "num": num,
                        "album": {"id": "A", "title": "Album title"}}
            url = f"https://cdn.example/{num}.jpg"
            urls.append(url)
            raw.extend(stream(metadata, (url, metadata)))
        work = self.map_work(raw)
        self.assertEqual((work.work_id, work.title), ("A", "Album title"))
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE])
        self.assertEqual([item.default_variant.url for item in work.items], urls)

    def test_native_quoted_http_status_maps_without_disclosing_url(self):
        cases = (
            (404, "Not Found", FailureKind.NOT_FOUND),
            (412, "Precondition Failed", FailureKind.UNAVAILABLE),
            (429, "Too Many Requests", FailureKind.UNAVAILABLE),
            (500, "Internal Server Error", FailureKind.UNAVAILABLE),
            (503, "Service Unavailable", FailureKind.UNAVAILABLE),
            (599, "Upstream Error", FailureKind.UNAVAILABLE),
            (408, "Request Timeout", FailureKind.TIMEOUT),
        )
        secret_url = "https://source.example/private?token=synthetic-secret"
        for status, reason, kind in cases:
            with self.subTest(status=status):
                message = f"'{status} {reason}' for '{secret_url}'"
                result = self.assert_failure([[-1, {"error": "HttpError", "message": message}]], kind)
                self.assertNotIn("synthetic-secret", repr(result))
                self.assertNotIn(secret_url, repr(result))
                self.assertNotIn(message, repr(result))

    def test_malformed_http_message_is_unavailable_without_secret_disclosure(self):
        for message in (None, "", "synthetic-secret", {"token": "synthetic-secret"},
                        "invalid response for 'https://source.example/404?token=synthetic-secret'"):
            with self.subTest(message=message):
                result = self.assert_failure(
                    [[-1, {"error": "HttpError", "message": message}]], FailureKind.UNAVAILABLE,
                )
                self.assertNotIn("synthetic-secret", repr(result))
                self.assertNotIn("source.example", repr(result))

    def test_d03_post_is_retained_without_body_and_same_url_get_stays_preferred(self):
        for method in ("POST", "post"):
            with self.subTest(method=method):
                raw = stream({"id": "upload-1"},
                    (MEDIA, {"id": "upload-1", "num": 1, "height": 2160,
                             "_http_method": method,
                             "_http_headers": {"X-Request-Token": "synthetic-header-secret"},
                             "_http_data": {"authenticity_token": "synthetic-body-secret",
                                            "upload_id": "synthetic-body-upload"}}),
                    (MEDIA, {"id": "upload-1", "num": 1, "height": 720}),
                )
                work = self.map_work(raw)
                for private in ("_http_data", "authenticity_token", "synthetic-body-secret",
                                "synthetic-body-upload", "synthetic-header-secret"):
                    self.assertNotIn(private, repr(work))
                self.assertEqual(len(work.items), 1)
                item = work.items[0]
                self.assertEqual(len(item.variants), 2)
                post, get = item.variants
                self.assertEqual((post.request_method, get.request_method), ("POST", "GET"))
                self.assertEqual((post.url, get.url), (MEDIA, MEDIA))
                self.assertEqual(post.required_headers, ("x-request-token",))
                self.assertEqual(get.required_headers, ())
                self.assertEqual(item.default_variant, get)
                self.assertEqual(work.field_sources[
                    f"items.{item.item_id}.variants.{post.variant_id}.request_method"], "gallery_dl_generic")
                delivery = decide_delivery(item.kind, post)
                self.assertEqual((delivery.method, delivery.reason), (Method.NONE, "UNSUPPORTED_REQUEST"))
                self.assertNotEqual(decide_delivery(item.kind, get).method, Method.NONE)

    def test_d03_absent_method_defaults_to_get_with_contract_source(self):
        work = self.map_work(stream({"id": 123}, (MEDIA, {"id": 123})))
        item = work.items[0]
        variant = item.default_variant
        self.assertEqual(variant.request_method, "GET")
        self.assertEqual(work.field_sources[
            f"items.{item.item_id}.variants.{variant.variant_id}.request_method"], "contract:default")

    def test_d04_video_only_remains_unpaired_beside_complete_variant_and_audio(self):
        work = self.map_work(stream({"id": 123},
            ("https://cdn.example/video-only.mp4", {"num": 1, "tracks": "video_only", "height": 2160}),
            ("https://cdn.example/complete.mp4", {"num": 1, "tracks": "av", "height": 720}),
            ("https://cdn.example/audio.m4a", {"num": 2, "tracks": "audio_only"}),
        ))
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        item = work.items[0]
        self.assertEqual(len(item.variants), 2)
        video_only, complete = item.variants
        self.assertIs(video_only.tracks, Tracks.VIDEO_ONLY)
        self.assertIs(complete.tracks, Tracks.AV)
        self.assertEqual(item.default_variant, complete)
        self.assertTrue(all(v.merge_with == "" for i in work.items for v in i.variants))
        self.assertEqual(work.groups, ())
        delivery = decide_delivery(item.kind, video_only)
        self.assertEqual((delivery.method, delivery.reason), (Method.NONE, "NEEDS_AUDIO_MERGE"))
        self.assertNotEqual(decide_delivery(item.kind, complete).method, Method.NONE)

    def test_itaku_source_id_mismatch_is_invalid(self):
        metadata = {"id": 1348332, "title": "Synthetic image", "extension": "png"}
        raw = stream(metadata, ("https://cdn.example/image.png", metadata))
        self.assert_failure(raw, FailureKind.INVALID_RESULT, "https://itaku.ee/images/1243386")
        work = self.map_work(raw, "https://itaku.ee/images/1348332")
        self.assertEqual(work.work_id, "1348332")

    def test_album_does_not_fall_back_to_child_image_title(self):
        for album in ({"id": "A"}, {"id": "A", "title": ""}):
            with self.subTest(album=album):
                metadata = {"id": "image-1", "num": 1, "album": album,
                            "title": "Child image title"}
                work = self.map_work(stream(metadata, ("https://cdn.example/child.jpg", metadata)))
                self.assertEqual((work.work_id, work.title), ("A", ""))
                self.assertNotIn("title", work.field_sources)
