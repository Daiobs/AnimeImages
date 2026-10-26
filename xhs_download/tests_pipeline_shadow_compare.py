"""Offline comparator counterexamples using controlled raw and legacy results."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from unittest.mock import patch

from django.test import SimpleTestCase

from .services.assets import MediaAsset, ParseResult
from .services.pipeline import Work
from .services.pipeline_shadow_capture import Capture, RawObservation
from .services.pipeline_shadow_compare import compare_capture, compare_observation, count_codes
from .services.provider_adapters.tiantian import TianTianAdapter
from .tests_provider_adapters import deny_map_io
from .templatetags.ytdlp_picker import ytdlp_presentation
from .views import build_result_display_context


PROVIDER = "tiantianjiexi_generic"
SOURCE = "https://www.douyin.com/video/6961737553342991651"
MEDIA = "https://cdn.example.org/controlled.mp4"


@contextmanager
def comparison_without_io():
    with ExitStack() as stack:
        stack.enter_context(deny_map_io())
        for target in ("django.core.cache.cache.get", "django.core.cache.cache.set",
                       "django.core.signing.dumps", "django.core.signing.loads",
                       "xhs_download.services.upstream_candidates.candidate_url"):
            stack.enter_context(patch(target, side_effect=AssertionError("COMPARISON_IO_FORBIDDEN")))
        yield


class PipelineShadowCompareTests(SimpleTestCase):
    def observation(self, videos=None):
        raw = {"code": 200, "data": {"title": "Controlled title", "videos": videos or [{
            "url": MEDIA, "qualityLabel": "1080P", "sizeInBytes": 200,
        }]}}
        return RawObservation(PROVIDER, raw, SOURCE)

    def legacy(self, assets=None, **metadata):
        return ParseResult("douyin", "video", "Controlled title", assets or (
            MediaAsset("video", MEDIA, "douyin", quality_label="1080P", size_bytes=200,
                       provider=PROVIDER, ext="mp4"),
        ), {"title": "Controlled title", **metadata})

    def compare(self, observation, legacy, **kwargs):
        before = deepcopy(observation.raw)
        with comparison_without_io():
            entry = compare_observation(observation, legacy, **kwargs)
        self.assertEqual(observation.raw, before)
        self.assertEqual(entry["invariants"], {"checked": True, "violations": []})
        return entry

    def test_display_size_is_the_old_value_when_existing_display_probe_filled_it(self):
        legacy = self.legacy()
        legacy = replace(legacy, assets=(replace(legacy.assets[0], size_bytes=0, provider="douyin_native"),))
        with patch("xhs_download.views.probe_media_size", return_value=200) as probe:
            display = build_result_display_context(legacy)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(display["video_items"][0]["size_bytes"], 200)
        entry = self.compare(self.observation(), legacy, display=display)
        self.assertNotIn("SIZE_DIFF", entry["difference_codes"])

    def test_display_filename_is_compared_when_asset_has_no_filename(self):
        legacy = self.legacy()
        legacy = replace(legacy, base_filename="Existing exported name", assets=(
            replace(legacy.assets[0], asset_id="upstream:controlled", source_platform="upstream"),
        ))
        with deny_map_io():
            display = build_result_display_context(legacy)
        self.assertEqual(legacy.assets[0].filename, "")
        self.assertEqual(display["video_items"][0]["filename"], "Existing exported name.mp4")
        entry = self.compare(self.observation(), legacy, display=display)
        self.assertIn("FILENAME_DIFF", entry["difference_codes"])
        difference = next(row for row in entry["differences"] if row["field"] == "items.1.filename")
        self.assertEqual(difference["old"], "Existing exported name.mp4")
        self.assertEqual(difference["new"], "Controlled title.mp4")

    def test_merged_result_uses_each_default_variants_field_sources(self):
        second_url = "https://cdn.example.org/second.mp4"
        observation = self.observation([
            {"url": MEDIA},
            {"url": second_url, "qualityLabel": "1080P", "sizeInBytes": 200},
        ])
        with deny_map_io():
            work = TianTianAdapter().map(observation.raw, observation.source_url)
        self.assertIsInstance(work, Work)
        first = work.items[0]
        prefix = f"items.{first.item_id}.variants.{first.default_variant.variant_id}."
        self.assertEqual(work.field_sources["size"], PROVIDER)
        self.assertEqual(work.field_sources[prefix + "size_bytes"], "contract:default")
        self.assertEqual(work.field_sources[prefix + "quality_label"], "contract:default")
        legacy = self.legacy(assets=(
            MediaAsset("video", MEDIA, "douyin", provider=PROVIDER,
                       quality_label="720P", size_bytes=100),
            MediaAsset("video", second_url, "douyin", provider=PROVIDER,
                       quality_label="720P", size_bytes=300),
        ), title="Native supplied title")
        entry = self.compare(observation, legacy, legacy_sources={
            "title": "douyin_native", "items": PROVIDER, "size": PROVIDER, "quality": PROVIDER,
        })
        self.assertIn("OLD_RESULT_MERGED", entry["difference_codes"])
        fields = {row["field"] for row in entry["differences"]}
        self.assertNotIn("title", fields)
        self.assertNotIn("items.1.size", fields)
        self.assertNotIn("items.1.quality", fields)
        self.assertIn("items.2.size", fields)
        self.assertIn("items.2.quality", fields)

    def picker_case(self):
        provider = "yt_dlp_generic"
        low, high = "https://cdn.example.org/low.mp4", "https://cdn.example.org/high.mp4"
        candidates = [{
            "provider_id": provider, "candidate_id": str(height), "work_id": "controlled",
            "media_type": "video", "protocol": "https", "extension": "mp4", "delivery_state": "complete",
            "audio_tracks": "present", "video_tracks": "present", "height": height,
            "format_label": f"{height}p", "size_bytes": size,
            "work": {"schema": "ytdlp_selected_format_v1", "title": "Controlled title"},
        } for height, size in ((720, 100), (1080, 200))]
        raw = {"id": "controlled", "title": "Controlled title", "formats": [{
            "url": url, "format_id": str(height), "ext": "mp4", "protocol": "https",
            "vcodec": "h264", "acodec": "aac", "height": height,
            "format_note": f"{height}p", "filesize": size,
        } for url, height, size in ((low, 720, 100), (high, 1080, 200))]}
        legacy = ParseResult("youtube", "video", "Controlled title", (
            MediaAsset("video", low, "youtube", provider=provider, quality_label="720P", size_bytes=100),
        ), {"title": "Controlled title"})
        display = {"display_title": "Controlled title", "base_filename": "Controlled title",
                   "upstream_candidates": candidates, "upstream_route": {},
                   "video_items": [{"video_url": low, "url": low, "size_bytes": 100}]}
        records = {candidate["candidate_id"]: {
            "candidate_id": candidate["candidate_id"], "provider_id": provider,
            "public": deepcopy(candidate), "url": url,
        } for candidate, url in zip(candidates, (low, high))}
        return RawObservation(provider, raw, "https://www.youtube.com/watch?v=controlled"), legacy, display, records

    def test_default_quality_and_size_follow_actual_picker_selection(self):
        observation, legacy, display, records = self.picker_case()
        before = deepcopy(display)
        with comparison_without_io():
            presentation = ytdlp_presentation(display["upstream_candidates"], {}, display["video_items"],
                                              "Controlled title", "Controlled title")
        self.assertEqual(presentation["selected"]["height"], 1080)
        self.assertEqual(presentation["selected"]["size_bytes"], 200)
        entry = self.compare(observation, legacy, display=display, candidates=records)
        self.assertEqual(display, before)
        self.assertNotIn("DEFAULT_URL_DIFF", entry["difference_codes"])
        self.assertNotIn("DEFAULT_QUALITY_DIFF", entry["difference_codes"])
        self.assertNotIn("SIZE_DIFF", entry["difference_codes"])

    def test_real_picker_preference_overrides_legacy_asset_default(self):
        observation, legacy, display, records = self.picker_case()
        display["upstream_route"] = {"format_preferences": {"max_height": 720}}
        legacy = replace(legacy, assets=(replace(legacy.assets[0], url=records["1080"]["url"],
                                                quality_label="1080p", size_bytes=200),))
        display["video_items"] = [{"url": records["1080"]["url"], "video_url": records["1080"]["url"],
                                   "size_bytes": 200}]
        before = deepcopy((display, records))
        entry = self.compare(observation, legacy, display=display, candidates=records)
        self.assertEqual((display, records), before)
        differences = {row["field"]: row for row in entry["differences"]}
        self.assertEqual(differences["items.1.quality"]["old"], [720, "720p"])
        self.assertEqual(differences["items.1.size"]["old"], 100)
        self.assertIn("DEFAULT_URL_DIFF", entry["difference_codes"])

    def test_missing_candidate_facts_are_gaps_not_default_asset_values(self):
        observation, legacy, display, _ = self.picker_case()
        entry = self.compare(observation, legacy, display=display)
        self.assertNotIn("DEFAULT_URL_DIFF", entry["difference_codes"])
        self.assertNotIn("FILENAME_DIFF", entry["difference_codes"])
        gaps = {row["field"]: row["code"] for row in entry["comparison_gaps"]}
        self.assertEqual(gaps["items.1.url"], "OLD_DEFAULT_URL_UNKNOWN")
        self.assertEqual(gaps["items.1.filename"], "OLD_FILENAME_UNKNOWN")
        codes = set(count_codes({"observations": [entry]}))
        self.assertNotIn(("yt_dlp_generic", "SHADOW_MATCH"), codes)
        self.assertIn(("yt_dlp_generic", "OLD_DEFAULT_URL_UNKNOWN"), codes)

    def test_candidate_record_requires_matching_provider_and_candidate_identity(self):
        observation, legacy, display, records = self.picker_case()
        for field, value in (("provider_id", PROVIDER), ("candidate_id", "other")):
            with self.subTest(field=field):
                mismatched = deepcopy(records)
                mismatched["1080"][field] = value
                entry = self.compare(observation, legacy, display=display, candidates=mismatched)
                self.assertIn("OLD_DEFAULT_URL_UNKNOWN", [gap["code"] for gap in entry["comparison_gaps"]])

    def test_service_picker_uses_exact_legacy_binding_without_candidate_lookup(self):
        _, legacy, display, records = self.picker_case()
        for candidate in display["upstream_candidates"]:
            candidate.update(provider_id=PROVIDER, service_file_eligible=True)
            candidate["work"]["schema"] = "service_selected_format_v1"
        selected = display["upstream_candidates"][1]
        selected.update(legacy_asset_id="controlled-bound-video", legacy_delivery="existing_manifest")
        high = records["1080"]["url"]
        asset = replace(legacy.assets[0], provider=PROVIDER, url=high, quality_label="1080p", size_bytes=200,
                        asset_id="controlled-bound-video", filename="Controlled title.mp4")
        legacy = replace(legacy, platform="douyin", assets=(asset,))
        display["video_items"] = [{"url": high, "video_url": high, "asset_id": asset.asset_id,
                                   "size_bytes": 200, "filename": asset.filename}]
        observation = self.observation([{"url": high, "qualityLabel": "1080p", "sizeInBytes": 200}])
        entry = self.compare(observation, legacy, display=display)
        self.assertNotIn("DEFAULT_URL_DIFF", entry["difference_codes"])
        self.assertNotIn("FILENAME_DIFF", entry["difference_codes"])
        self.assertEqual(entry["comparison_gaps"], [])
        mismatched = {"1080": {"provider_id": PROVIDER, "candidate_id": "1080", "url": MEDIA}}
        entry = self.compare(observation, legacy, display=display, candidates=mismatched)
        self.assertIn("DEFAULT_URL_DIFF", entry["difference_codes"])
        self.assertIn("OLD_FILENAME_UNKNOWN", [gap["code"] for gap in entry["comparison_gaps"]])

    def test_capture_passes_candidates_only_as_old_evidence_without_leaking_headers(self):
        observation, legacy, display, records = self.picker_case()
        for record in records.values():
            record["headers"] = {"Cookie": "controlled-private-cookie"}
            record["url"] += "?token=controlled-private-token"
        session = Capture()
        session.add(observation)
        session.observe_result(legacy, display)
        session.legacy_candidates = records
        before = deepcopy((records, observation.raw, display))
        with comparison_without_io():
            details = compare_capture(session)
        self.assertEqual((records, observation.raw, display), before)
        self.assertNotIn("controlled-private", repr(details))
        entry = details["observations"][0]
        self.assertNotIn("SHADOW_ERROR", entry["difference_codes"])
        self.assertIn("DEFAULT_URL_DIFF", entry["difference_codes"])

    def test_signed_urls_are_compared_without_persisting_raw_query_values(self):
        observation = self.observation()
        observation.raw["data"]["videos"][0]["url"] += "?token=controlled-new-secret"
        legacy = self.legacy()
        legacy = replace(legacy, assets=(replace(
            legacy.assets[0], url=MEDIA + "?token=controlled-old-secret"),))
        session = Capture()
        session.add(observation)
        session.observe_result(legacy)
        with comparison_without_io():
            details = compare_capture(session)
        serialized = repr(details)
        self.assertNotIn("controlled-new-secret", serialized)
        self.assertNotIn("controlled-old-secret", serialized)
        self.assertIn("DEFAULT_URL_DIFF", details["observations"][0]["difference_codes"])

    def test_cached_result_without_observation_never_calls_mapper(self):
        session = Capture()
        session.observe_result(self.legacy())
        with patch("xhs_download.services.pipeline_shadow_compare.mapper_for") as mapper, comparison_without_io():
            details = compare_capture(session)
        mapper.assert_not_called()
        self.assertEqual(details["observations"][0]["provider"], PROVIDER)
        self.assertEqual(details["observations"][0]["difference_codes"], ["SHADOW_NO_RAW"])
