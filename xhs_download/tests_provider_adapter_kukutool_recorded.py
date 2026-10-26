"""Complete KuKuTool archive replay and explicitly synthetic raw sidecar inputs."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from .services.pipeline import (
    Completeness, Fetchable, Kind, Tracks, Variant, Work,
    fact_gaps, field_coverage, validate_work,
)
from .services.platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from .services.provider_adapters.kukutool import KukutoolAdapter
from .services.provider_adapters.recordings import check, load_recording, sanitize
from .services.work_caption import _TOPIC, work_caption_presentation
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/provider_raw/kukutool_generic"
NAME = "kukutool_generic"
CASES = {
    "recorded_acfun.json": "recorded",
    "recorded_weibo_gallery.json": "recorded",
    "synthetic_sidecar_best.json": "synthetic",
    "synthetic_sidecar_resources.json": "synthetic",
    "synthetic_generic_audio.json": "synthetic",
}


class KukutoolRecordedMappingTests(TestCase):
    def replay(self, filename):
        document = load_recording(FIXTURES / filename)
        check(document)
        self.assertEqual(document["provider"], NAME)
        self.assertEqual(document["provenance"], {
            "kind": CASES[filename], "raw_kind": "json", "complete": True,
        })
        raw = document["raw"]
        original = deepcopy(raw)
        self.assertNotIn("_sidecar", raw)
        self.assertNotIn("_retained_video_fullinfo", raw)
        self.assertFalse(any(key.startswith("_service_") for key in raw))
        adapter = KukutoolAdapter()
        self.assertEqual(adapter.name, NAME)
        with deny_map_io():
            work = adapter.map(raw, document["source_url"])
            repeated = adapter.map(raw, document["source_url"])
        self.assertEqual(raw, original)
        self.assertEqual(work, repeated)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        with self.assertRaises(FrozenInstanceError):
            work.title = "changed"
        with self.assertRaises(TypeError):
            work.field_sources["title"] = "changed"
        for item in work.items:
            with self.assertRaises(FrozenInstanceError):
                item.index = 99
            for variant in item.variants:
                with self.assertRaises(FrozenInstanceError):
                    variant.height = 999
        return document, work

    def assert_work(self, document, work, work_id):
        raw = document["raw"]
        presentation = work_caption_presentation(raw.get("title", ""), "")
        self.assertEqual(work.source_url, document["source_url"])
        self.assertEqual(work.work_id, work_id)
        self.assertEqual((work.platform, work.provider), ("", ""))
        self.assertEqual(work.title, presentation["title"])
        self.assertEqual(work.description, _TOPIC.sub("", presentation["description"]).strip())
        self.assertEqual(work.topics, tuple(presentation["topics"]))
        self.assertEqual(work.author, "")
        self.assertEqual(work.cover_url, raw.get("cover", ""))
        self.assertIsNone(work.duration_seconds)
        self.assertIs(work.completeness, Completeness.FINAL)
        self.assertEqual(work.groups, ())
        self.assertEqual([item.index for item in work.items], list(range(1, len(work.items) + 1)))
        self.assertEqual(work.field_sources["source_url"], "request")
        self.assertEqual(work.field_sources["work_id"], "request:work_id")
        self.assertEqual(work.field_sources["items"], NAME)
        for field, present in (
            ("title", work.title), ("description", work.description),
            ("topics", work.topics), ("cover", work.cover_url),
        ):
            self.assertEqual(work.field_sources.get(field), NAME if present else None)
        self.assertNotIn("author", work.field_sources)
        self.assertNotIn("duration_seconds", work.field_sources)

    def assert_variant(self, work, item, variant, *, url, container="", label="",
                       height=None, size=None, bitrate=None, original=False,
                       headers=(), douyin=False):
        self.assertEqual(variant.url, url)
        self.assertEqual(variant.container, container)
        self.assertEqual(variant.quality_label, label)
        self.assertEqual(variant.height, height)
        self.assertIsNone(variant.width)
        self.assertIsNone(variant.fps)
        self.assertEqual(variant.bitrate, bitrate)
        self.assertEqual(variant.size_bytes, size)
        self.assertEqual(variant.size_exact, size is not None)
        self.assertEqual(variant.is_original, original)
        self.assertIs(variant.tracks, Tracks.UNKNOWN)
        self.assertEqual(variant.merge_with, "")
        self.assertEqual(variant.required_headers, headers)
        self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
        self.assertFalse(variant.drm)
        self.assertFalse(variant.segmented)
        self.assertEqual(variant.request_method, "GET")
        self.assertIsNone(variant.expires_at)
        self.assertEqual(variant.host_policy, tuple(DOUYIN_MEDIA_HOST_SUFFIXES)
                         if douyin else ())
        prefix = f"items.{item.item_id}.variants.{variant.variant_id}"
        for field in Variant.__dataclass_fields__:
            self.assertIn(f"{prefix}.{field}", work.field_sources)
        for field, present in (
            ("url", True), ("quality_label", bool(label)), ("height", height is not None),
            ("size_bytes", size is not None), ("size_exact", size is not None),
            ("bitrate", bitrate is not None), ("is_original", original),
            ("required_headers", bool(headers)),
        ):
            self.assertEqual(work.field_sources[f"{prefix}.{field}"],
                             NAME if present else "contract:default")
        for field in ("tracks", "browser_fetchable"):
            self.assertEqual(work.field_sources[f"{prefix}.{field}"], "contract:unknown")
        self.assertEqual(work.field_sources[f"{prefix}.container"],
                         "adapter:url_extension" if container else "contract:default")
        self.assertEqual(work.field_sources[f"{prefix}.host_policy"],
                         "existing:DOUYIN_MEDIA_HOST_SUFFIXES" if douyin else
                         "contract:empty_list:any_public_https_host_passing_per_hop_checks")
        self.assertEqual(work.field_sources[f"{prefix}.request_method"], "contract:default")

    def test_recorded_acfun_preserves_all_seven_quality_variants_and_still(self):
        document, work = self.replay("recorded_acfun.json")
        raw = document["raw"]
        self.assertEqual(set(raw), {"title", "type", "cover", "url", "videos", "pics"})
        self.assertEqual(raw["type"], "video")
        self.assert_work(document, work, "ac37303684")
        self.assertEqual(len(work.items), 2)
        video, still = work.items
        self.assertIs(video.kind, Kind.VIDEO)
        self.assertIs(still.kind, Kind.IMAGE)
        self.assertEqual(len(video.variants), 7)
        rows = raw["videos"][0]["video_fullinfo"]
        heights = (360, 480, 540, 720, 1080, 1080, 2160)
        sizes = (256504206, 441223813, 441223813, 940515424, 1454895518, 2705867906, 4428698300)
        for variant, row, height, size in zip(video.variants, rows, heights, sizes):
            self.assertEqual(set(row), {"url", "size", "type"})
            self.assertEqual(row["size"], size)
            self.assert_variant(work, video, variant, url=row["url"], container="mp4",
                                label=row["type"], height=height, size=size)
        self.assertEqual(raw["url"], rows[-1]["url"])
        self.assertEqual(raw["videos"][0]["url"], rows[-1]["url"])
        self.assertEqual(urlsplit(rows[1]["url"]).path, urlsplit(rows[2]["url"]).path)
        self.assertIs(video.default_variant, video.variants[-1])
        self.assertEqual(len(still.variants), 1)
        self.assertEqual(raw["pics"], [raw["cover"]])
        self.assert_variant(work, still, still.variants[0], url=raw["pics"][0], container="jpeg")
        self.assertIs(still.default_variant, still.variants[0])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": NAME, "size": NAME})

    def test_recorded_weibo_preserves_nine_images_without_format_or_cover_invention(self):
        document, work = self.replay("recorded_weibo_gallery.json")
        raw = document["raw"]
        self.assertEqual(set(raw), {"title", "type", "videos", "pics"})
        self.assertEqual(raw["type"], "images")
        self.assertEqual(raw["videos"], [])
        self.assert_work(document, work, "RkWevk3D3")
        self.assertEqual(len(work.items), 9)
        # Token redaction collapses URLs, not the nine observed image positions.
        self.assertEqual(len(set(raw["pics"])), 1)
        for item, url in zip(work.items, raw["pics"]):
            self.assertIs(item.kind, Kind.IMAGE)
            self.assertEqual(len(item.variants), 1)
            self.assertEqual(parse_qs(urlsplit(url).query)["token"], ["REDACTED"])
            self.assert_variant(work, item, item.variants[0], url=url)
            self.assertIs(item.default_variant, item.variants[0])
        self.assertEqual(fact_gaps(work), ())
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": None, "quality": None, "size": None})

    def test_synthetic_sidecar_best_keeps_final_url_without_inventing_original(self):
        document, work = self.replay("synthetic_sidecar_best.json")
        self.assert_work(document, work, "7000000000000000001")
        best = document["raw"]["best"]
        self.assertEqual(len(work.items), 1)
        video, = work.items
        self.assertIs(video.kind, Kind.VIDEO)
        self.assertEqual(len(video.variants), 1)
        self.assert_variant(work, video, video.variants[0], url=best["final_url"],
                            label="\u8d85\u9ad8\u6e05", size=70481987, bitrate=53031000, douyin=True)
        self.assertIs(video.default_variant, video.variants[0])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), {"title": None, "cover": None, "quality": NAME, "size": NAME})

    def test_synthetic_sidecar_resources_preserve_facts_and_choose_original(self):
        document, work = self.replay("synthetic_sidecar_resources.json")
        self.assert_work(document, work, "7000000000000000002")
        raw = document["raw"]
        self.assertEqual(len(work.items), 1)
        video, = work.items
        self.assertIs(video.kind, Kind.VIDEO)
        self.assertEqual(len(video.variants), 4)
        expected = (
            {"label": "4K", "size": 4000000},
            {"label": "1080P+", "height": 1080, "size": 2000000, "bitrate": 3000000},
            {"label": "\u539f\u753b", "size": 5000000, "original": True, "headers": ("x-fixture-token",)},
            {"label": "\u8d85\u9ad8\u6e05\u6e90"},
        )
        for variant, row, facts in zip(video.variants, raw["resources"], expected):
            self.assert_variant(work, video, variant, url=row["url"], container="mp4", douyin=True, **facts)
        self.assertIs(video.default_variant, video.variants[2])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": NAME, "size": NAME})
        self.assertNotIn("REDACTED", repr(work))
        self.assertNotIn("Synthetic fixture client", repr(work))

    def test_synthetic_audio_arrays_remain_separate_from_video_variants(self):
        document, work = self.replay("synthetic_generic_audio.json")
        self.assert_work(document, work, "7000000000000000003")
        raw = document["raw"]
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO, Kind.AUDIO])
        self.assertEqual([len(item.variants) for item in work.items], [1, 1, 1])
        video, audio, alternate = work.items
        self.assert_variant(work, video, video.variants[0], url=raw["url"], container="mp4",
                            label="720P", height=720, size=1000000, douyin=True)
        self.assert_variant(work, audio, audio.variants[0], url=raw["audios"][0], container="mp3", douyin=True)
        self.assert_variant(work, alternate, alternate.variants[0], url=raw["audios"][1]["url"],
                            container="m4a", size=120000, douyin=True)
        for item in work.items:
            self.assertIs(item.default_variant, item.variants[0])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": NAME, "size": NAME})

    def test_fixture_corpus_is_complete_checked_and_explicitly_classified(self):
        self.assertEqual({path.name for path in FIXTURES.glob("*.json")}, set(CASES))
        for name in CASES:
            with self.subTest(filename=name):
                document = load_recording(FIXTURES / name)
                before = deepcopy(document)
                check(document)
                self.assertEqual(sanitize(document), document)
                self.assertEqual(document, before)
                self.assertEqual(document["provenance"]["kind"], CASES[name])
