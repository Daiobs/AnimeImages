"""Five reduced historical observations, not complete recordings or live acceptance."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.pipeline.model import Completeness
from .services.provider_adapters.videodl import VideodlMapper
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/native_replay"
CASES = {
    "0151880293e8f4148ded3846": ("HuanQiuVideoClient", False, "mp4", "http://media.example/asset-1.mp4", ""),
    "f0464caa40e3f2debb792893": ("WeiboVideoClient", None, "", "", ""),
    "50db251dc5c7b043b8feb342": ("AcFunVideoClient", True, "mp4", "https://media.example/asset-2.m3u8", "https://media.example/asset-1.jpeg"),
    "9e5cb53d95362646ed898dd6": ("FoxNewsVideoClient", True, "", "https://media.example/asset-2.m3u8", "https://media.example/asset-1.jpg"),
    "996ddbca94d3f56df92eba8d": ("MeipaiVideoClient", False, "mp4", "http://media.example/asset-2.mp4", "https://media.example/asset-1"),
}


class VideodlReducedReplayTests(SimpleTestCase):
    def test_existing_five_reduced_observations_without_reconstructing_omitted_fields(self):
        # Exact backend was not captured. This is synthetic invocation context,
        # intentionally different from the historical native source label.
        mapper = VideodlMapper(backend_id="ReplayConfiguredBackend", backend_category="common")
        origin = "videodl:common:ReplayConfiguredBackend"
        for key, (native_source, segmented, container, media, cover) in CASES.items():
            with self.subTest(archive=key, source=native_source):
                archive = json.loads((FIXTURES / f"{key}.json").read_text())
                original = deepcopy(archive)
                self.assertEqual(archive["provider"], "videodl_repo")
                self.assertEqual(archive["limitations"], [
                    "URLs and prose replaced; secret markers retained; unused native internals omitted",
                    "No current resource authorization or delivery claim",
                ])
                self.assertNotIn("provenance", archive)
                self.assertEqual(len(archive["records"]), 1)
                infos = []
                for record in archive["records"][0]:
                    self.assertEqual(record["_class"], "VideoInfo")
                    # NoneType is the only marker that determines the full value.
                    fields = {name: None if value == {"_redacted": "NoneType"} else deepcopy(value)
                              for name, value in record["fields"].items()}
                    infos.append(SimpleNamespace(**fields))
                before = deepcopy(infos)
                with deny_map_io():
                    work = mapper.map(infos, archive["source"])
                self.assertEqual(infos, before)
                self.assertEqual(archive, original)
                if segmented is None:
                    self.assertEqual(archive["execution_status"], "FAILED")
                    self.assertEqual(work, Failure(FailureKind.UNAVAILABLE, "VIDEODL_ITEM_ERROR"))
                    continue
                self.assertEqual(archive["execution_status"], "RETURNED")
                self.assertIsInstance(work, Work)
                self.assertEqual(validate_work(work), ())
                self.assertEqual(len(work.items), 1)
                item = work.items[0]
                variant, = item.variants
                self.assertEqual(vars(item), {"item_id": "1", "kind": Kind.VIDEO, "index": 1, "variants": (variant,)})
                self.assertEqual(vars(variant), {
                    "variant_id": "1:1", "url": media, "container": container,
                    "tracks": Tracks.UNKNOWN, "height": None, "width": None, "fps": None,
                    "bitrate": None, "quality_label": "", "is_original": False,
                    "size_bytes": None, "size_exact": False, "merge_with": "",
                    "required_headers": (), "browser_fetchable": Fetchable.UNKNOWN,
                    "drm": False, "segmented": segmented, "request_method": "GET",
                    "expires_at": None, "host_policy": ("media.example",),
                })
                expected_sources = {
                    "source_url": "request", "work_id": origin, "backend": origin,
                    "items": origin, "title": origin, "description": "contract:default",
                    "topics": "contract:default", "author": "contract:default",
                    "cover_url": origin if cover else "contract:default",
                    "duration_seconds": "contract:default", "platform": "contract:default",
                    "provider": "contract:default", "groups": "contract:default",
                    "completeness": "contract:default",
                    "observations.1.source": "videodl:source:sha256:" + sha256(native_source.encode()).hexdigest(),
                }
                if cover:
                    expected_sources["cover"] = origin
                variant_sources = {
                    "variant_id": "adapter:resource_identity", "url": origin,
                    "container": origin if container else "contract:default",
                    "tracks": "contract:unknown", "height": "contract:default",
                    "width": "contract:default", "fps": "contract:default",
                    "bitrate": "contract:default", "quality_label": "contract:default",
                    "is_original": "contract:default", "size_bytes": "contract:default",
                    "size_exact": "contract:default", "merge_with": "contract:default",
                    "required_headers": "contract:default", "browser_fetchable": "contract:unknown",
                    "drm": "contract:default", "segmented": origin if segmented else "contract:default",
                    "request_method": "contract:default", "expires_at": "contract:default",
                    "host_policy": "adapter:exact_url_host",
                }
                expected_sources.update({f"items.1.variants.1:1.{field}": source
                                         for field, source in variant_sources.items()})
                self.assertEqual(dict(work.field_sources), expected_sources)
                self.assertEqual(vars(work), {
                    "source_url": "https://source.example/work/1", "work_id": "redacted-text",
                    "items": (item,), "platform": "", "title": "redacted-text",
                    "description": "", "topics": (), "author": "", "cover_url": cover,
                    "duration_seconds": None, "groups": (), "provider": "",
                    "completeness": Completeness.FINAL, "field_sources": expected_sources,
                })
