"""Complete historical raw archives, plus an optional offline Worker parity check."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from unittest import TestCase, skipUnless
from urllib.parse import parse_qs, urlsplit

from .services.pipeline import Fetchable, Kind, Tracks, Variant, Work, validate_work
from .services.provider_adapters.parsehub import MAX_BYTES, ParseHubMapper, _object, _richtext_media
from .services.provider_adapters.recordings import check, load_recording
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/provider_raw/parsehub_generic"
RUNTIME = os.environ.get("PARSEHUB_COMPAT_PYTHON")

_WORKER_PROBE = r'''
import json, socket, sys
def offline(*args, **kwargs):
    raise AssertionError("NETWORK_FORBIDDEN")
socket.create_connection = socket.getaddrinfo = offline
socket.socket.connect = socket.socket.connect_ex = socket.socket.sendto = offline
from tools.upstream_match_worker import install_network_guard
from tools.upstream_parsehub_worker import MAX_BYTES, explicit_richtext_media
from parsehub.types import media_ref
from parsehub.types.result import RichTextParseResult
install_network_guard(offline=True)

data = json.load(sys.stdin)
fields = data["raw"]["fields"]
result = RichTextParseResult.__new__(RichTextParseResult)
vars(result).update(fields)
media = []
for raw in fields["media"]:
    cls = getattr(media_ref, raw["_class"])
    item = cls.__new__(cls)
    vars(item).update(raw["fields"])
    media.append(item)
ordered, seen = explicit_richtext_media(result, media, fields["raw_url"])
print(json.dumps({"limit": MAX_BYTES, "media": [
    [type(item).__name__, item.url, item.ext, item.width, item.height] for item in ordered
]}))
'''


class ParseHubRecordedTests(TestCase):
    def recording(self, name):
        data = load_recording(FIXTURES / ("recorded_" + name + ".json"))
        check(data)
        self.assertEqual(data["provenance"], {"kind": "recorded", "raw_kind": "json", "complete": True})
        before = deepcopy(data["raw"])
        with deny_map_io():
            work = ParseHubMapper().map(data["raw"], data["source_url"])
        self.assertEqual(data["raw"], before)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual((work.provider, work.platform, work.author, work.groups), ("", "", "", ()))
        self.assertEqual(work.source_url, data["source_url"])
        self.assertEqual(work.field_sources["source_url"], "request")
        self.assertEqual(work.field_sources["work_id"], "parsehub_generic:raw_url")
        self.assertEqual(work.field_sources["author"], "contract:default")
        # MediaRef duration is item-level; do not invent an aggregate work duration.
        self.assertIsNone(work.duration_seconds)
        self.assertEqual(work.field_sources["duration_seconds"], "contract:default")
        for item in work.items:
            for variant in item.variants:
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
                self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(variant.required_headers, ())
                self.assertEqual(variant.request_method, "GET")
                self.assertFalse(variant.drm)
                self.assertFalse(variant.segmented)
                self.assertIsNone(variant.size_bytes)
                self.assertFalse(variant.size_exact)
                self.assertFalse(variant.is_original)
                self.assertEqual(variant.quality_label, "")
                self.assertEqual(variant.merge_with, "")
                self.assertIsNone(variant.fps)
                self.assertIsNone(variant.bitrate)
                self.assertEqual(set(self.sources(work, item)), set(Variant.__dataclass_fields__))
                sources = self.sources(work, item)
                for field in ("tracks", "browser_fetchable"):
                    self.assertEqual(sources[field], "contract:unknown")
                for field in ("size_bytes", "size_exact", "is_original", "fps", "bitrate", "quality_label",
                              "required_headers", "request_method", "drm", "segmented", "merge_with"):
                    self.assertEqual(sources[field], "contract:default")
        return data, work

    def sources(self, work, item):
        prefix = f"items.{item.item_id}.variants.{item.variants[0].variant_id}."
        return {key[len(prefix):]: value for key, value in work.field_sources.items() if key.startswith(prefix)}

    def test_native_bili_complete_archive_and_deadline(self):
        data, work = self.recording("bilibili_native")
        self.assertEqual(data["raw"]["_class"], "BiliVideoParseResult")
        fields = data["raw"]["fields"]
        raw = fields["media"]["fields"]
        self.assertEqual(work.work_id, "BV13x41117TL")
        self.assertEqual(work.title, fields["title"])
        self.assertEqual(work.description, fields["content"])
        self.assertEqual(work.topics, ())
        self.assertEqual(work.cover_url, raw["thumb_url"])
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        variant = item.default_variant
        self.assertIs(item.kind, Kind.VIDEO)
        self.assertEqual((variant.url, variant.width, variant.height, variant.container), (raw["url"], 640, 360, "mp4"))
        self.assertEqual(variant.expires_at, datetime.fromtimestamp(1791874874, timezone.utc))
        self.assertEqual(parse_qs(urlsplit(variant.url).query)["upsig"], ["REDACTED"])
        sources = self.sources(work, item)
        self.assertEqual(sources["expires_at"], "adapter:url_deadline")
        self.assertEqual(sources["host_policy"], "existing:BILIBILI_MEDIA_HOST_SUFFIXES")
        for field in ("url", "container", "width", "height"):
            self.assertEqual(sources[field], "parsehub_generic")
        for field in ("title", "description", "cover_url", "quality"):
            self.assertEqual(work.field_sources[field], "parsehub_generic")

    def test_native_douyin_complete_archive(self):
        data, work = self.recording("douyin_native")
        self.assertEqual(data["raw"]["_class"], "DouyinVideoParseResult")
        fields = data["raw"]["fields"]
        raw = fields["media"]["fields"]
        self.assertEqual(work.work_id, "6961737553342991651")
        self.assertEqual(work.title, " ".join(fields["title"].split()))
        self.assertEqual((work.description, work.topics), ("", ()))
        self.assertEqual(work.cover_url, raw["thumb_url"])
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        variant = item.default_variant
        self.assertIs(item.kind, Kind.VIDEO)
        self.assertEqual((variant.url, variant.width, variant.height, variant.container), (raw["url"], 576, 1024, "mp4"))
        self.assertIsNone(variant.expires_at)
        self.assertEqual(self.sources(work, item)["host_policy"], "existing:DOUYIN_MEDIA_HOST_SUFFIXES")
        self.assertEqual(self.sources(work, item)["expires_at"], "contract:default")
        self.assertEqual(parse_qs(urlsplit(work.cover_url).query)["x-signature"], ["REDACTED"])

    def test_both_richtext_archives_preserve_text_and_all_body_images(self):
        for name, count in (("weixin_richtext_short", 4), ("weixin_richtext_long", 30)):
            with self.subTest(name=name):
                data, work = self.recording(name)
                fields = data["raw"]["fields"]
                self.assertEqual(fields["media"], [])
                self.assertEqual(len(work.items), count)
                self.assertEqual(work.work_id, urlsplit(fields["raw_url"]).path.rsplit("/", 1)[-1])
                self.assertEqual(work.description, fields["content"].strip())
                self.assertEqual(work.title, " ".join(fields["title"].split()))
                self.assertEqual((work.cover_url, work.topics), ("", ()))
                self.assertNotIn("markdown_content", repr(work))
                self.assertNotIn("<img", repr(work))
                for item in work.items:
                    self.assertIs(item.kind, Kind.IMAGE)
                    self.assertIsNone(item.default_variant.width)
                    self.assertIsNone(item.default_variant.height)
                    # A query wx_fmt does not declare an actual path extension.
                    self.assertEqual(item.default_variant.container, "")
                    self.assertEqual(self.sources(work, item)["container"], "contract:default")

    def test_recordings_have_no_unredacted_upsig(self):
        count = 0
        for path in FIXTURES.glob("recorded_*.json"):
            text = path.read_text()
            for value in re.findall(r"[?&]upsig=([^&\s\"<>]+)", text, re.I):
                count += 1
                self.assertEqual(value, "REDACTED")
            check(load_recording(path))
        self.assertEqual(count, 1)

    @skipUnless(RUNTIME, "Set PARSEHUB_COMPAT_PYTHON for the pinned offline Worker")
    def test_same_recordings_match_worker_richtext_media(self):
        for name in ("weixin_richtext_short", "weixin_richtext_long"):
            with self.subTest(name=name):
                data, work = self.recording(name)
                process = subprocess.run([RUNTIME, "-B", "-c", _WORKER_PROBE], input=json.dumps(data),
                                         text=True, capture_output=True, timeout=30,
                                         cwd=Path(__file__).resolve().parent.parent)
                self.assertEqual(process.returncode, 0, process.stderr)
                expected = json.loads(process.stdout)
                self.assertEqual(expected["limit"], MAX_BYTES)
                fields = data["raw"]["fields"]
                with deny_map_io():
                    mapped = _richtext_media(fields, fields["media"], fields["raw_url"])
                actual = [[cls, row["url"], row.get("ext"), row.get("width", 0), row.get("height", 0)]
                          for cls, row in map(_object, mapped)]
                self.assertEqual(actual, expected["media"])
                self.assertEqual([item.default_variant.url for item in work.items],
                                 [row[1] for row in expected["media"]])
