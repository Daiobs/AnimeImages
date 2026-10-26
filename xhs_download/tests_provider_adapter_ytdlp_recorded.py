"""Complete real metadata recording, offline mapping; never fetch its media URLs."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase

from .services.pipeline import (
    Fetchable, Kind, Method, Tracks, Work, decide_delivery, fact_gaps,
    field_coverage, validate_work,
)
from .services.provider_adapters.recordings import load_recording
from .services.provider_adapters.ytdlp import YtDlpAdapter
from .tests_provider_adapters import deny_map_io


class YtdlpRecordedMappingTests(TestCase):
    def test_complete_peertube_recording_preserves_every_format_and_unknown_codec(self):
        document = load_recording(Path(__file__).parent / 'fixtures/provider_raw/yt_dlp_generic/recorded_peertube.json')
        raw = document['raw']
        original = deepcopy(raw)
        with deny_map_io():
            result = YtDlpAdapter().map(raw, document['source_url'])
        self.assertIsInstance(result, Work)
        self.assertEqual(raw, original)
        self.assertEqual(validate_work(result), ())
        self.assertEqual((result.source_url, result.work_id, result.title, result.author, result.duration_seconds),
                         (document['source_url'], raw['id'], raw['title'], 'Framasoft', 113))
        self.assertEqual((result.platform, result.provider, result.groups, result.topics), ('', '', (), ()))
        self.assertEqual(result.cover_url, raw['thumbnail'])
        self.assertEqual(result.description, raw['description'])
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual((item.kind, item.index, len(item.variants)), (Kind.VIDEO, 1, 10))
        for variant, fmt in zip(item.variants, raw['formats']):
            self.assertEqual((variant.url, variant.container, variant.height), (fmt['url'], 'mp4', fmt['height']))
            self.assertEqual(variant.width, fmt.get('width'))
            self.assertEqual(variant.fps, fmt.get('fps') or None)
            self.assertEqual(variant.bitrate, fmt['tbr'] * 1000 if fmt.get('tbr') else None)
            self.assertFalse(variant.is_original)
            self.assertFalse(variant.drm)
            self.assertEqual(variant.merge_with, '')
            self.assertEqual(variant.required_headers, ())
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertIsNone(variant.expires_at)
            self.assertEqual(variant.host_policy, ('framatube.org',))
            if fmt['protocol'] == 'https':
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
                self.assertFalse(variant.segmented)
                self.assertEqual(variant.size_bytes, fmt['filesize'])
                self.assertTrue(variant.size_exact)
                self.assertIs(decide_delivery(item.kind, variant).method, Method.BROWSER_STREAMING)
            else:
                self.assertIs(variant.tracks, Tracks.AV)
                self.assertTrue(variant.segmented)
                self.assertIsNone(variant.size_bytes)
                self.assertFalse(variant.size_exact)
                self.assertEqual(decide_delivery(item.kind, variant).reason, 'SEGMENTED_STREAM')
        self.assertEqual(item.default_variant.variant_id, '1:1080p:9')
        self.assertEqual(item.default_variant.size_bytes, 16815344)
        self.assertEqual(field_coverage(result), {key: 'yt_dlp_generic' for key in ('title', 'cover', 'quality', 'size')})
        self.assertEqual(fact_gaps(result), ('TRACKS_UNKNOWN',))
