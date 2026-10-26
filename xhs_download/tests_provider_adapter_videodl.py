"""Offline VideoInfo map checks; no backend, worker or delivery execution."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.provider_adapters.base import ProviderAdapter, RawMapper
from .services.provider_adapters.recordings import check, load_recording
from .services.provider_adapters.videodl import VideodlMapper
from .services.provider_adapters.service_facts import _host_policy
from .services.work_caption import _TOPIC, work_caption_presentation
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.tiktok.com/@demo/video/123"
MEDIA = "https://cdn.example/video.mp4"
FIXTURES = Path(__file__).parent / "fixtures/provider_raw/videodl"
ORIGIN = "videodl:common:ConfiguredBackend"


def native_info(**fields):
    return SimpleNamespace(source="NativeDisplayLabel", identifier="backend-local-id",
                           title="Synthetic work", download_url=MEDIA, **fields)


class VideodlMappingTests(SimpleTestCase):
    def setUp(self):
        self.mapper = VideodlMapper(backend_id="ConfiguredBackend", backend_category="common")

    def map(self, infos, source=SOURCE):
        before = deepcopy(infos)
        with deny_map_io():
            result = self.mapper.map(infos, source)
            self.assertEqual(result, self.mapper.map(infos, source))
        self.assertEqual(infos, before)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
            self.assertEqual((result.provider, result.platform, result.groups), ("", "", ()))
        return result

    def fixture(self, name):
        document = load_recording(FIXTURES / name)
        check(document)
        self.assertEqual(document["provenance"], {
            "kind": "synthetic", "raw_kind": "json", "complete": True,
        })
        self.assertTrue(all(row["_class"] == "VideoInfo" for row in document["raw"]))
        return [SimpleNamespace(**deepcopy(row["fields"])) for row in document["raw"]]

    def test_mapper_only_and_backend_selection_is_explicit(self):
        self.assertIsInstance(self.mapper, RawMapper)
        self.assertNotIsInstance(self.mapper, ProviderAdapter)
        self.assertFalse(hasattr(self.mapper, "fetch"))
        self.assertFalse(hasattr(self.mapper, "parse"))
        with self.assertRaises(TypeError):
            VideodlMapper()
        for backend, category in (("", "common"), ("https://private.example/secret", "common"),
                                  ("ConfiguredBackend", "legacy_auto")):
            with self.subTest(backend=backend, category=category), self.assertRaises(ValueError):
                VideodlMapper(backend_id=backend, backend_category=category)

    def test_actual_backend_and_native_source_remain_distinct(self):
        work = self.map([native_info()])
        self.assertEqual(work.field_sources["backend"], ORIGIN)
        self.assertEqual(work.field_sources["observations.1.source"],
                         "videodl:source:sha256:" + sha256(b"NativeDisplayLabel").hexdigest())
        self.assertEqual(work.field_sources["title"], ORIGIN)
        self.assertEqual(work.work_id, "123")
        self.assertEqual(work.field_sources["work_id"], "request:work_id")
        with self.assertRaises(FrozenInstanceError):
            work.title = "changed"
        with self.assertRaises(TypeError):
            work.field_sources["backend"] = "changed"

    def test_native_display_label_is_not_a_backend_id_or_public_text(self):
        for label in ("native-display-label", "https://private.example/?token=source-secret",
                      "display label\nAuthorization: private-source-token"):
            with self.subTest(label=label):
                info = native_info()
                info.source = label
                work = self.map([info])
                self.assertIsInstance(work, Work)
                self.assertEqual(work.field_sources["backend"], ORIGIN)
                self.assertEqual(work.field_sources["observations.1.source"],
                                 "videodl:source:sha256:" + sha256(label.encode()).hexdigest())
                self.assertNotIn(label, repr(work))

    def test_same_url_aliases_preserve_all_restrictions_in_both_orders(self):
        limited = native_info(required_headers=["Referer", "Origin"], ip_bound=True,
                              drm=True, segmented=True, request_method="POST")
        plain = native_info()
        for infos in ([limited, plain], [plain, limited]):
            with self.subTest(order=infos):
                work = self.map(infos)
                self.assertEqual(len(work.items), 2)
                for item in work.items:
                    variant = item.variants[0]
                    self.assertEqual(variant.required_headers, ("origin", "referer"))
                    self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                    self.assertTrue(variant.drm)
                    self.assertTrue(variant.segmented)
                    self.assertEqual(variant.request_method, "POST")
                    self.assertFalse(variant.deliverable)
                    for field in ("required_headers", "browser_fetchable", "drm", "segmented", "request_method"):
                        self.assertEqual(work.field_sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"], ORIGIN)

    def test_same_url_video_audio_aliases_do_not_erase_missing_context(self):
        work = self.map([native_info(
            audio_download_url=MEDIA, headers_present=True, download_with_ffmpeg=True,
            default_audio_download_headers={"Referer": "https://source.example"},
            audio_required_headers=["Origin"], audio_drm=True,
        )])
        for item in work.items:
            variant = item.variants[0]
            self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
            self.assertEqual(variant.required_headers, ("origin",))
            self.assertTrue(variant.drm)
            self.assertTrue(variant.segmented)

    def test_conflicting_alias_methods_or_tracks_fail_safely(self):
        for infos in ([native_info(method="POST"), native_info(method="PUT")],
                      [native_info(tracks="av"), native_info(tracks="video_only")]):
            with self.subTest(infos=infos):
                result = self.map(infos)
                self.assertIsInstance(result, Failure)
                self.assertIs(result.kind, FailureKind.INVALID_RESULT)

    def test_alias_expiry_keeps_the_earliest_observed_deadline(self):
        work = self.map([native_info(expires_at=2000000000), native_info(expires_at=1900000000), native_info()])
        for item in work.items:
            self.assertEqual(item.variants[0].expires_at.timestamp(), 1900000000)
            self.assertEqual(work.field_sources[f"items.{item.item_id}.variants.{item.item_id}:1.expires_at"], ORIGIN)

    def test_native_extra_storage_preserves_explicit_facts(self):
        # VideoInfo at the pinned runtime stores extension attributes in _extra.
        info = native_info(_extra={"quality": "1080P+", "required_headers": ["Referer"],
                                   "ip_bound": True, "size_bytes": 1024, "size_exact": False})
        variant = self.map([info]).items[0].variants[0]
        self.assertEqual((variant.quality_label, variant.height), ("1080P+", 1080))
        self.assertEqual(variant.required_headers, ("referer",))
        self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
        self.assertEqual((variant.size_bytes, variant.size_exact), (1024, False))

    def test_native_same_object_pairing_does_not_need_a_merge_flag_or_best_audio(self):
        first = native_info(audio_download_url="https://cdn.example/first.m4a", audio_bitrate=64000)
        second = native_info(audio_download_url="https://cdn.example/second.m4a", audio_bitrate=320000)
        second.download_url = "https://cdn.example/second.mp4"
        for infos in ([first, second], [second, first]):
            with self.subTest(order=infos):
                work = self.map(infos)
                self.assertEqual(len(work.items), 4)
                for offset, info in enumerate(infos):
                    video_item, audio_item = work.items[offset * 2:offset * 2 + 2]
                    video, audio = video_item.variants[0], audio_item.variants[0]
                    self.assertEqual((video_item.kind, audio_item.kind), (Kind.VIDEO, Kind.AUDIO))
                    self.assertEqual((video.tracks, audio.tracks), (Tracks.VIDEO_ONLY, Tracks.AUDIO_ONLY))
                    self.assertEqual((video.url, audio.url), (info.download_url, info.audio_download_url))
                    self.assertEqual(video.merge_with, audio.variant_id)
                    self.assertEqual(audio.merge_with, "")
                    self.assertIs(video_item.default_variant, video)
                    for item, variant in ((video_item, video), (audio_item, audio)):
                        self.assertEqual(work.field_sources[f"items.{item.item_id}.variants.{variant.variant_id}.tracks"], ORIGIN)
                    self.assertEqual(work.field_sources[f"items.{video_item.item_id}.variants.{video.variant_id}.merge_with"], ORIGIN)

    def test_same_video_url_native_pairs_keep_their_own_audio_partner(self):
        first = native_info(audio_download_url="https://cdn.example/first.m4a")
        second = native_info(audio_download_url="https://cdn.example/second.m4a")
        work = self.map([first, second])
        self.assertIsInstance(work, Work)
        first_video, first_audio, second_video, second_audio = [item.variants[0] for item in work.items]
        self.assertEqual(first_video.merge_with, first_audio.variant_id)
        self.assertEqual(second_video.merge_with, second_audio.variant_id)
        self.assertNotEqual(first_video.merge_with, second_video.merge_with)

    def test_audio_only_object_keeps_audio_identity_without_a_video_url(self):
        for video_url in ("", None, "file:///private/not-a-network-resource"):
            with self.subTest(video_url=video_url):
                info = native_info(audio_download_url="https://cdn.example/only.m4a", audio_size_bytes=1024)
                info.download_url = video_url
                work = self.map([info])
                self.assertIsInstance(work, Work)
                item, = work.items
                variant, = item.variants
                self.assertIs(item.kind, Kind.AUDIO)
                self.assertIs(variant.tracks, Tracks.AUDIO_ONLY)
                self.assertEqual((variant.url, variant.container, variant.size_bytes),
                                 (info.audio_download_url, "m4a", 1024))
                self.assertEqual(variant.merge_with, "")
                self.assertEqual(work.field_sources["items.1.variants.1:1.tracks"], ORIGIN)

    def test_explicit_audio_only_primary_resource_keeps_audio_identity(self):
        info = native_info(_extra={"tracks": "audio_only"}, ext="m4a")
        info.download_url = "https://cdn.example/primary-audio.m4a"
        work = self.map([info])
        self.assertIsInstance(work, Work)
        self.assertIs(work.items[0].kind, Kind.AUDIO)
        self.assertIs(work.items[0].variants[0].tracks, Tracks.AUDIO_ONLY)
        self.assertEqual(work.items[0].variants[0].merge_with, "")

    def test_separate_objects_do_not_supply_a_guessed_audio_partner(self):
        video = native_info()
        audio = native_info(audio_download_url="https://cdn.example/standalone.m4a", audio_bitrate=999999)
        audio.download_url = ""
        work = self.map([video, audio])
        self.assertIs(work.items[0].variants[0].tracks, Tracks.UNKNOWN)
        self.assertEqual(work.items[0].variants[0].merge_with, "")
        self.assertIs(work.items[1].kind, Kind.AUDIO)
        self.assertIs(work.items[1].variants[0].tracks, Tracks.AUDIO_ONLY)
        video.tracks = "video_only"
        self.assertEqual(self.map([video, audio]),
                         Failure(FailureKind.INVALID_RESULT, "VIDEODL_ITEM_WITHOUT_DEFAULT"))

    def test_same_url_alias_does_not_borrow_another_objects_merge_partner(self):
        paired = native_info(audio_download_url="https://cdn.example/paired.m4a")
        alias = native_info()
        for infos in ([paired, alias], [alias, paired]):
            with self.subTest(order=infos):
                self.assertEqual(self.map(infos),
                                 Failure(FailureKind.INVALID_RESULT, "VIDEODL_ITEM_WITHOUT_DEFAULT"))

    def test_identical_video_audio_urls_do_not_establish_independent_tracks(self):
        work = self.map([native_info(audio_download_url=MEDIA)])
        for item in work.items:
            self.assertIs(item.variants[0].tracks, Tracks.UNKNOWN)
            self.assertEqual(item.variants[0].merge_with, "")

    def test_non_native_merge_flag_is_not_a_pairing_fact(self):
        for value in (True, False):
            with self.subTest(value=value):
                info = native_info(_extra={"requires_merge": value})
                unpaired = self.map([info])
                self.assertIsInstance(unpaired, Work)
                self.assertIs(unpaired.items[0].variants[0].tracks, Tracks.UNKNOWN)
                info.audio_download_url = "https://cdn.example/paired.m4a"
                work = self.map([info])
                self.assertEqual(work.items[0].variants[0].merge_with, work.items[1].variants[0].variant_id)

    def test_contract_1_4_rejects_unpaired_video_only_without_default(self):
        for facts in ({"tracks": "video_only"}, {"vcodec": "h264", "acodec": "none"}):
            with self.subTest(facts=facts):
                result = self.map([native_info(_extra=facts)])
                self.assertEqual(result, Failure(FailureKind.INVALID_RESULT, "VIDEODL_ITEM_WITHOUT_DEFAULT"))

    def test_explicit_false_facts_are_not_default_provenance(self):
        info = native_info(size_bytes=1000, size_exact=False, drm=False, segmented=False,
                           is_original=False, required_headers=[], tracks="unknown")
        work = self.map([info])
        for field in ("size_exact", "drm", "segmented", "is_original", "required_headers", "tracks"):
            self.assertEqual(work.field_sources[f"items.1.variants.1:1.{field}"], ORIGIN, field)
        default = self.map([native_info()])
        for field in ("size_exact", "drm", "segmented", "is_original", "required_headers"):
            self.assertEqual(default.field_sources[f"items.1.variants.1:1.{field}"], "contract:default", field)
        estimate = self.map([native_info(size="1 MiB")])
        self.assertEqual(estimate.items[0].variants[0].size_bytes, 1048576)
        self.assertFalse(estimate.items[0].variants[0].size_exact)
        self.assertEqual(estimate.field_sources["items.1.variants.1:1.size_exact"], ORIGIN)

    def test_usable_media_survives_error_observations_and_warning_text(self):
        failed = native_info(err_msg="https://private.example/?token=error-secret")
        failed.download_url, failed.identifier, failed.title = "", "different-id", "failed title"
        good = native_info(err_msg="private-warning-token")
        for infos in ([failed, good], [good, failed], [good]):
            with self.subTest(infos=infos):
                work = self.map(infos, "https://source.example/work/opaque")
                self.assertIsInstance(work, Work)
                self.assertEqual(len(work.items), 1)
                self.assertEqual(work.title, good.title)
                self.assertEqual(work.work_id, good.identifier)
                self.assertNotIn("error-secret", repr(work))
                self.assertNotIn("private-warning-token", repr(work))

    def test_native_pairing_preserves_each_resources_context_and_metadata(self):
        infos = self.fixture("synthetic_video_audio.json")
        work = self.map(infos)
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        video, audio = [item.variants[0] for item in work.items]
        self.assertEqual((video.height, video.quality_label, video.size_bytes), (1080, "1080P+", 8192))
        self.assertEqual((audio.height, audio.quality_label, audio.size_bytes), (None, "", 1024))
        self.assertEqual((video.required_headers, audio.required_headers), (("origin", "referer"), ("cookie",)))
        self.assertEqual((video.tracks, audio.tracks), (Tracks.VIDEO_ONLY, Tracks.AUDIO_ONLY))
        self.assertEqual(video.merge_with, audio.variant_id)
        self.assertEqual(audio.merge_with, "")
        for item in work.items:
            variant = item.variants[0]
            prefix = f"items.{item.item_id}.variants.{variant.variant_id}"
            self.assertEqual(work.field_sources[f"{prefix}.tracks"], ORIGIN)
            self.assertEqual(work.field_sources[f"{prefix}.required_headers"], ORIGIN)
        presentation = work_caption_presentation(infos[0].title, infos[0].description)
        self.assertEqual(work.title, presentation["title"])
        self.assertEqual(work.description, _TOPIC.sub("", presentation["description"]).strip())
        self.assertEqual(work.topics, tuple(presentation["topics"]))
        self.assertEqual(work.cover_url, infos[0].cover_url)
        self.assertNotIn("do-not-project", repr(work))
        self.assertNotIn("REDACTED", repr(work))
        self.assertNotIn(infos[0].save_path, repr(work))

    def test_segmented_and_decryption_facts_are_not_executed_or_lost(self):
        work = self.map(self.fixture("synthetic_encrypted_segments.json"))
        variant = work.items[0].variants[0]
        self.assertTrue(variant.drm)
        self.assertTrue(variant.segmented)
        self.assertFalse(variant.deliverable)
        self.assertIs(work.items[0].default_variant, variant)
        self.assertNotIn("MP4DECRYPT", repr(work))

    def test_each_segmented_signal_is_sufficient_without_inventing_drm(self):
        for facts in ({"download_with_ffmpeg": True}, {"enable_nm3u8dlre": True},
                      {"segmented": True}, {"download_url": "https://cdn.example/list.m3u8"},
                      {"download_url": "https://cdn.example/list.mpd"},
                      {"download_url": "https://cdn.example/segment.m4s"}):
            with self.subTest(facts=facts):
                info = native_info()
                vars(info).update(facts)
                variant = self.map([info]).items[0].variants[0]
                self.assertTrue(variant.segmented)
                self.assertFalse(variant.drm)
                self.assertFalse(variant.deliverable)

    def test_decryption_flags_and_private_settings_only_project_drm(self):
        for facts in ({"download_with_mp4decrypt": True}, {"enable_mp4decrypt": True},
                      {"requires_decryption": True}, {"has_drm": True},
                      {"ffmpeg_settings": {"decryption_key": "private-decryption-material"}},
                      {"nm3u8dlre_settings": {"custom_keys": ["private-decryption-material"]}},
                      {"nm3u8dlre_settings": {"decryption_engine": "shaka_packager"}}):
            with self.subTest(facts=facts):
                variant = self.map([native_info(**facts)]).items[0].variants[0]
                self.assertTrue(variant.drm)
                self.assertFalse(variant.deliverable)
                self.assertNotIn("private-decryption-material", repr(variant))

    def test_aria2c_and_empty_executor_settings_are_not_segmentation_or_drm(self):
        variant = self.map([native_info(download_with_aria2c=True, ffmpeg_settings={},
                                        nm3u8dlre_settings={}, aria2c_settings={})]).items[0].variants[0]
        self.assertFalse(variant.segmented)
        self.assertFalse(variant.drm)

    def test_declared_manifest_extension_is_a_fact_even_with_an_opaque_url(self):
        for extension in ("m3u8", "mpd", "m4s", "m3u"):
            with self.subTest(extension=extension):
                info = native_info(ext=extension)
                info.download_url = "https://cdn.example/media?id=opaque"
                work = self.map([info])
                self.assertTrue(work.items[0].variants[0].segmented)
                self.assertEqual(work.field_sources["items.1.variants.1:1.segmented"], ORIGIN)

    def test_manifest_url_overrides_false_flag_with_url_provenance(self):
        info = native_info(segmented=False)
        info.download_url = "https://cdn.example/stream.m3u8"
        work = self.map([info])
        self.assertTrue(work.items[0].variants[0].segmented)
        self.assertEqual(work.field_sources["items.1.variants.1:1.segmented"], "adapter:url_extension")

    def test_literal_native_executor_option_names_remain_decryption_facts(self):
        # Native CommandModsApplier accepts literal command-option dictionary keys.
        # These are controlled constructions, not claims that a capture emitted them.
        for settings in (
            {"nm3u8dlre_settings": {"--decryption-engine": "MP4DECRYPT"}},
            {"nm3u8dlre_settings": {"--key": "private-key-material"}},
            {"ffmpeg_settings": {"-decryption_key": "private-key-material"}},
        ):
            with self.subTest(settings=settings):
                work = self.map([native_info(**settings)])
                self.assertTrue(work.items[0].variants[0].drm)
                self.assertEqual(work.field_sources["items.1.variants.1:1.drm"], ORIGIN)
                self.assertNotIn("private-key-material", repr(work))
        self.assertFalse(self.map([native_info(nm3u8dlre_settings={"--key": False})]).items[0].variants[0].drm)

    def test_only_explicit_p_labels_supply_height(self):
        for label, expected in (("1080P", 1080), ("2160P", 2160), ("1080P+", 1080),
                                ("4K", None), ("original", None), ("video", None)):
            with self.subTest(label=label):
                variant = self.map([native_info(quality=label)]).items[0].variants[0]
                self.assertEqual(variant.height, expected)
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
        variant = self.map([native_info(quality="2160P", height=720, width=1280,
                                        vcodec="h264", acodec="aac")]).items[0].variants[0]
        self.assertEqual((variant.height, variant.width, variant.tracks), (720, 1280, Tracks.AV))

    def test_required_headers_and_context_use_145_semantics(self):
        ordinary = {"Referer": "https://source.example/private", "Origin": "https://source.example"}
        plain = self.map([native_info(default_download_headers=ordinary)]).items[0].variants[0]
        self.assertEqual(plain.required_headers, ())
        self.assertIs(plain.browser_fetchable, Fetchable.UNKNOWN)
        required = self.map([native_info(default_download_headers=ordinary,
                                         required_headers=["Referer", "Origin"])]).items[0].variants[0]
        self.assertEqual(required.required_headers, ("origin", "referer"))
        for facts in ({"ip_bound": True}, {"requires_context": True}, {"headers_present": True}):
            with self.subTest(facts=facts):
                work = self.map([native_info(**facts)])
                self.assertIs(work.items[0].variants[0].browser_fetchable, Fetchable.FALSE)
                self.assertEqual(work.field_sources["items.1.variants.1:1.browser_fetchable"], ORIGIN)

    def test_private_cookie_names_do_not_leak_and_audio_context_is_separate(self):
        info = native_info(default_download_cookies={"session": "private-cookie"},
                           audio_download_url="https://cdn.example/a.m4a", audio_ip_bound=True,
                           download_with_ffmpeg=True, enable_mp4decrypt=True)
        work = self.map([info])
        video, audio = [item.variants[0] for item in work.items]
        self.assertEqual(video.required_headers, ("cookie",))
        self.assertEqual(audio.required_headers, ())
        self.assertIs(video.browser_fetchable, Fetchable.UNKNOWN)
        self.assertIs(audio.browser_fetchable, Fetchable.FALSE)
        self.assertTrue(video.segmented)
        self.assertTrue(video.drm)
        self.assertFalse(audio.segmented)
        self.assertFalse(audio.drm)
        self.assertNotIn("private-cookie", repr(work))

    def test_each_native_info_is_an_item_without_invented_variant_relationships(self):
        first, second = native_info(), native_info(quality="720P")
        second.download_url = "https://cdn.example/second.mp4"
        work = self.map([first, second])
        self.assertEqual([item.index for item in work.items], [1, 2])
        self.assertEqual([len(item.variants) for item in work.items], [1, 1])

    def test_existing_platform_host_policy_and_empty_unknown_host(self):
        for source in (SOURCE, "https://www.douyin.com/video/123",
                       "https://www.bilibili.com/video/BV13x41117TL/", "https://source.example/work/1"):
            with self.subTest(source=source):
                work = self.map([native_info()], source=source)
                expected, origin = _host_policy(source, MEDIA)
                self.assertEqual(work.items[0].variants[0].host_policy, expected)
                self.assertEqual(work.field_sources["items.1.variants.1:1.host_policy"], origin)

    def test_non_get_method_preserved_without_request_body(self):
        work = self.map([native_info(request_method="post", body="private-request-body")])
        variant = work.items[0].variants[0]
        self.assertEqual(variant.request_method, "POST")
        self.assertFalse(variant.deliverable)
        self.assertNotIn("private-request-body", repr(work))

    def test_unsafe_or_malformed_inputs_have_safe_failure_codes(self):
        for raw in (None, [], {}, [dict(source="NativeDisplayLabel", download_url=MEDIA)],
                    [None], [native_info(required_headers=[12])]):
            with self.subTest(raw=raw):
                result = self.map(raw)
                self.assertIsInstance(result, Failure)
                self.assertIs(result.kind, FailureKind.INVALID_RESULT)
                self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
        info = native_info(err_msg="https://private.example/?token=secret")
        info.download_url = ""
        result = self.map([info])
        self.assertEqual(result, Failure(FailureKind.UNAVAILABLE, "VIDEODL_ITEM_ERROR"))
        self.assertNotIn("secret", repr(result))

    def test_no_media_or_identity_is_not_reconstructed_from_cover_or_title(self):
        info = native_info(cover_url="https://cdn.example/cover.jpg")
        info.download_url = ""
        self.assertEqual(self.map([info]), Failure(FailureKind.INVALID_RESULT, "VIDEODL_NO_MEDIA"))
        info = native_info()
        del info.identifier
        self.assertEqual(self.map([info], source="https://source.example/opaque"),
                         Failure(FailureKind.INVALID_RESULT, "VIDEODL_MISSING_WORK_ID"))

    def test_validity_properties_are_not_evaluated(self):
        class Info:
            @property
            def with_valid_download_url(self):
                raise AssertionError("native property must not run")
        info = Info()
        vars(info).update(vars(native_info()))
        with deny_map_io():
            work = self.mapper.map([info], SOURCE)
        self.assertIsInstance(work, Work)
