from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services import upstream_parsehub as bridge
from .services.common import XhsParseError
from .services.upstream_delivery import (
    normalize_upstream_result, upstream_manifest_item_is_allowed,
)


EVIDENCE = {'schema': 'iso_bmff_tracks_v1', 'complete_moov': True,
            'audio': 'present', 'video': 'present', 'reason': 'complete_track_table'}
SOURCE = 'https://example.org/post/1'
VIDEO = 'https://cdn.example.org/video.mp4'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ParseHubVideoParityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        dns = patch('xhs_download.services.upstream_delivery.validate_public_download_url',
                    side_effect=lambda url: url)
        dns.start()
        self.addCleanup(dns.stop)
        self.match = dict(provider_id='parsehub_generic', extractor_id='Example',
                          label='example', version='test')

    def video(self, **metadata):
        return [3, VIDEO, {'extension': 'mp4', 'media_completeness': 'unknown',
                           'upstream_bmff_track_evidence': dict(EVIDENCE), **metadata}]

    def normalize(self, payload):
        return normalize_upstream_result(payload, match=self.match, source_url=SOURCE)

    def rejected(self, row):
        with patch('xhs_download.services.upstream_delivery._issue_assets') as issue:
            with self.assertRaises(XhsParseError):
                self.normalize([row])
            issue.assert_not_called()

    def test_verified_video_receives_exact_authority_without_leaking_evidence(self):
        result = self.normalize([self.video()])
        self.assertEqual(result.post_type, 'video')
        self.assertEqual(result.metadata['limited_media_count'], 0)
        asset = result.video_assets[0]
        self.assertEqual((asset.url, asset.ext, asset.headers), (VIDEO, 'mp4', {}))
        self.assertTrue(upstream_manifest_item_is_allowed(asset.to_download_item()))
        self.assertNotIn('upstream_bmff_track_evidence', asset.to_download_item())

    def test_each_verified_field_is_required_with_strict_boolean(self):
        for key, invalid in (('schema', 'v2'), ('complete_moov', 1),
                             ('audio', 'absent'), ('video', 'unknown'), ('reason', 'timeout')):
            for value in (invalid, None):
                with self.subTest(key=key, value=value):
                    self.rejected(self.video(upstream_bmff_track_evidence={**EVIDENCE, key: value}))
        for probe in (None, True, [], 'complete'):
            with self.subTest(probe=probe):
                self.rejected(self.video(upstream_bmff_track_evidence=probe))

    def test_explicit_missing_tracks_cannot_be_overridden(self):
        for key in ('acodec', 'vcodec'):
            for metadata in ({key: 'none'}, {'upstream_track_evidence': {key: 'NONE'}},
                             {key: 'none', 'upstream_track_evidence': {}}):
                with self.subTest(metadata=metadata):
                    self.rejected(self.video(**metadata))

    def test_codec_or_completeness_claim_alone_is_not_probe_evidence(self):
        self.rejected(self.video(upstream_bmff_track_evidence=None, acodec='aac',
                                 vcodec='h264', media_completeness='complete'))

    def test_evidence_is_local_to_the_message(self):
        unknown = self.video(upstream_bmff_track_evidence=None,
                             nested={'upstream_bmff_track_evidence': EVIDENCE})
        unknown[1] = 'https://cdn.example.org/unknown.mp4'
        result = self.normalize([self.video(), unknown,
                                 [3, 'https://cdn.example.org/image.png', {'extension': 'png'}]])
        self.assertEqual([a.url for a in result.video_assets], [VIDEO])
        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(result.metadata['limited_media_count'], 1)

    def test_gallery_video_remains_limited_even_with_probe(self):
        self.match['provider_id'] = 'gallery_dl_generic'
        self.rejected(self.video())

    def test_unsafe_urls_and_streaming_are_not_admitted(self):
        for url in ('http://cdn.example.org/video.mp4', 'https://u:p@cdn.example.org/video.mp4',
                    'https://cdn.example.org/video.m3u8', 'https://cdn.example.org/video.mpd'):
            with self.subTest(url=url):
                row = self.video()
                row[1] = url
                self.rejected(row)
        self.rejected(self.video(protocol='m3u8_native'))

    def test_mime_only_video_uses_safe_extension(self):
        for extension in ('', '../../bad.html', 'video/mp4', 'html'):
            with self.subTest(extension=extension):
                row = self.video(extension=extension, mime_type='video/unknown')
                row[1] = 'https://cdn.example.org/media?id=1'
                self.assertEqual(self.normalize([row]).video_assets[0].ext, 'mp4')

    def test_safe_url_extension_is_retained(self):
        row = self.video(extension='video/quicktime', mime_type='video/quicktime')
        row[1] = 'https://cdn.example.org/video.mov'
        self.assertEqual(self.normalize([row]).video_assets[0].ext, 'mov')

    def test_verified_videos_still_obey_asset_limit_and_deduplication(self):
        self.assertEqual(len(self.normalize([self.video(), self.video()]).assets), 1)
        rows = [[3, f'https://cdn.example.org/{index}.mp4', self.video()[2]]
                for index in range(31)]
        with patch('xhs_download.services.upstream_delivery._issue_assets') as issue:
            with self.assertRaises(XhsParseError):
                self.normalize(rows)
            issue.assert_not_called()

    def test_bridge_preserves_candidate_metadata_without_promoting_unknown(self):
        payload = [self.video()]
        with patch.object(bridge, 'match_url', return_value=(self.match,)), \
                patch.object(bridge, '_call', return_value=payload) as call:
            result = bridge.parse_upstream_url(self.match, SOURCE)
        call.assert_called_once()
        self.assertIs(result, payload)
        self.assertEqual(result[0][2]['media_completeness'], 'unknown')
        self.assertEqual(result[0][2]['upstream_bmff_track_evidence'], EVIDENCE)

    def test_bridge_payload_limit_stays_30(self):
        with patch.object(bridge, 'match_url', return_value=(self.match,)), \
                patch.object(bridge, '_call', return_value=[self.video()] * 31):
            with self.assertRaisesRegex(bridge.ParseHubBridgeError, 'invalid_payload'):
                bridge.parse_upstream_url(self.match, SOURCE)
