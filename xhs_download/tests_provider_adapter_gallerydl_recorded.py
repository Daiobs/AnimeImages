"""Replay complete historical extractor messages, without requesting their URLs."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from .services.pipeline import (
    Completeness, Fetchable, Kind, Tracks, Work, fact_gaps, field_coverage, validate_work,
)
from .services.provider_adapters.gallerydl import GalleryDlMapper
from .services.provider_adapters.recordings import load_recording
from .tests_provider_adapters import deny_map_io


class GalleryDlRecordedTests(TestCase):
    def test_complete_itaku_messages_preserve_work_and_only_emitted_file(self):
        for work_id in ("1243386", "1348332"):
            with self.subTest(work_id=work_id):
                document = load_recording(Path(__file__).parent / (
                    f"fixtures/provider_raw/gallery_dl_generic/recorded_itaku_{work_id}.json"))
                raw = document["raw"]
                original = deepcopy(raw)
                with deny_map_io():
                    result = GalleryDlMapper().map(raw, document["source_url"])
                self.assertIsInstance(result, Work)
                self.assertEqual(raw, original)
                self.assertEqual(validate_work(result), ())
                metadata, media = raw[0][-1], raw[1]
                self.assertEqual((result.source_url, result.work_id, result.title),
                                 (document["source_url"], work_id, metadata["title"]))
                self.assertEqual(result.description, metadata["description"].strip())
                self.assertEqual(result.topics, tuple(metadata["tags"]))
                self.assertEqual(result.author, metadata["owner_displayname"])
                self.assertEqual(result.cover_url, metadata["image"])
                self.assertIsNone(result.duration_seconds)
                self.assertEqual((result.platform, result.provider, result.groups), ("", "", ()))
                self.assertIs(result.completeness, Completeness.FINAL)
                self.assertEqual(len(result.items), 1)
                item = result.items[0]
                self.assertEqual((item.kind, item.index, len(item.variants)), (Kind.IMAGE, 1, 1))
                variant = item.default_variant
                self.assertEqual((variant.url, variant.container), (media[1], media[2]["extension"]))
                self.assertEqual((variant.height, variant.width, variant.fps, variant.bitrate,
                                  variant.size_bytes, variant.expires_at), (None,) * 6)
                self.assertEqual((variant.quality_label, variant.merge_with), ("", ""))
                self.assertEqual((variant.is_original, variant.size_exact, variant.drm,
                                  variant.segmented), (False,) * 4)
                self.assertEqual((variant.required_headers, variant.host_policy), ((), ("itaku.ee",)))
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
                self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(variant.request_method, "GET")
                self.assertEqual(result.field_sources[
                    f"items.{item.item_id}.variants.{variant.variant_id}.request_method"], "contract:default")
                self.assertEqual(fact_gaps(result), ())
                self.assertEqual(field_coverage(result), {
                    "title": "gallery_dl_generic", "cover": "gallery_dl_generic",
                    "quality": None, "size": None,
                })
                for field in variant.__dataclass_fields__:
                    self.assertIn(f"items.{item.item_id}.variants.{variant.variant_id}.{field}",
                                  result.field_sources)
