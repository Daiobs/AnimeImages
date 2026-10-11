from hashlib import sha256
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.common import XhsParseError
from .services.upstream_delivery import (
    normalize_upstream_result, upstream_manifest_item_is_allowed, video_evidence_state, UpstreamMediaError,
)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class UpstreamDeliveryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.dns = patch('xhs_download.services.upstream_delivery.validate_public_download_url', side_effect=lambda u: u)
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.match = dict(provider_id='gallery_dl_generic', extractor_id='ExampleImage', label='example:image', version='test')

    def image_result(self):
        return normalize_upstream_result([[3, 'https://cdn.example.org/original.png', {'extension': 'png'}]],
                                         match=self.match, source_url='https://example.org/post/1')

    def test_exact_resource_issued(self):
        result = self.image_result()
        self.assertEqual(result.platform, 'third_party')
        self.assertTrue(upstream_manifest_item_is_allowed(result.assets[0].to_download_item()))

    def test_tampered_fields_rejected(self):
        item = self.image_result().assets[0].to_download_item()
        for key in ('url', 'provider', 'provider_resource_id', 'provider_cache_key', 'asset_id', 'source_platform', 'type'):
            with self.subTest(key=key):
                self.assertFalse(upstream_manifest_item_is_allowed({**item, key: 'forged'}))

    def test_signature_required(self):
        item = self.image_result().assets[0].to_download_item()
        item.pop('provider_manifest_token')
        self.assertFalse(upstream_manifest_item_is_allowed(item))

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=60)
    def test_signature_expires_with_cached_resource_still_present(self):
        # Replace signing's clock object, not shared time.time used by the cache.
        with patch('django.core.signing.time') as clock:
            clock.time.return_value = 1700000000
            item = self.image_result().assets[0].to_download_item()
            key = 'upstream:asset:v1:' + sha256(item['asset_id'].encode()).hexdigest()
            record = cache.get(key)
            self.assertIsInstance(record, dict)
            self.assertTrue(upstream_manifest_item_is_allowed(item))
            clock.time.return_value = 1700000060
            self.assertTrue(upstream_manifest_item_is_allowed(item))
            clock.time.return_value = 1700000061
            self.assertEqual(cache.get(key), record)
            self.assertFalse(upstream_manifest_item_is_allowed(item))

    def test_tampered_signature_rejected_with_cached_resource_present(self):
        item = self.image_result().assets[0].to_download_item()
        self.assertTrue(upstream_manifest_item_is_allowed(item))
        forged = {**item, 'provider_manifest_token': item['provider_manifest_token'] + 'tampered'}
        self.assertFalse(upstream_manifest_item_is_allowed(forged))
        self.assertTrue(upstream_manifest_item_is_allowed(item))

    def test_cache_eviction_fails_closed(self):
        item = self.image_result().assets[0].to_download_item()
        cache.clear()
        self.assertFalse(upstream_manifest_item_is_allowed(item))

    def test_metadata_urls_do_not_become_assets(self):
        payload = [[3, 'https://cdn.example.org/original.png', {'extension': 'png', 'avatar': 'https://cdn.example.org/avatar.png'}]]
        result = normalize_upstream_result(payload, match=self.match, source_url='https://example.org/post/1')
        self.assertEqual(len(result.assets), 1)
        self.assertTrue(result.assets[0].url.endswith('/original.png'))

    def test_queue_not_normalized(self):
        with self.assertRaises(XhsParseError):
            normalize_upstream_result([[6, 'https://example.org/user', {}]], match=self.match, source_url='https://example.org/post/1')

    def test_resource_limit_does_not_silently_truncate_gallery(self):
        payload = [[3, f'https://cdn.example.org/{index}.png', {'extension': 'png'}]
                   for index in range(31)]
        result = normalize_upstream_result(payload, match=self.match, source_url='https://example.org/post/1')
        self.assertEqual(len(result.metadata['upstream_candidates']), 31)
        self.assertEqual(len(result.assets), 30)
        self.assertEqual(result.metadata['limited_media_count'], 1)

    def test_credentials_and_non_https_rejected(self):
        for url in ('file:///tmp/media.png', 'https://user:pass@cdn.example.org/file.png',
                    'https://cdn.example.org:8443/file.png'):
            with self.subTest(url=url), self.assertRaises(XhsParseError):
                normalize_upstream_result([[3, url, {'extension': 'png'}]], match=self.match, source_url='https://example.org/post/1')

    def test_video_must_contain_audio_and_video(self):
        match = {**self.match, 'provider_id': 'yt_dlp_generic'}
        for payload in ({'_type': 'playlist', 'entries': []},):
            with self.assertRaises(XhsParseError):
                normalize_upstream_result(payload, match=match, source_url='https://example.org/post/1')

    def test_complete_video_uses_existing_format_selection(self):
        payload = {'title': 'Example', 'formats': [{'url': 'https://cdn.example.org/video.mp4', 'vcodec': 'h264', 'acodec': 'aac', 'protocol': 'https', 'height': 720}]}
        result = normalize_upstream_result(payload, match={**self.match, 'provider_id': 'yt_dlp_generic'}, source_url='https://example.org/post/1')
        self.assertEqual(len(result.video_assets), 1)
        self.assertTrue(upstream_manifest_item_is_allowed(result.assets[0].to_download_item()))

    def test_unknown_tracks_are_not_reported_as_explicit_split(self):
        payload = {'url': 'https://cdn.example.org/video.mp4', 'ext': 'mp4'}
        self.assertEqual(video_evidence_state(payload), 'unknown_media_tracks')
        result = normalize_upstream_result(payload, match={**self.match, 'provider_id': 'yt_dlp_generic'},
                                          source_url='https://example.org/post/1')
        self.assertEqual(result.metadata['upstream_candidates'][0]['delivery_state'], 'unknown_media_tracks')
        self.assertEqual(result.assets, ())

    def test_distinct_track_and_streaming_evidence(self):
        base = {'url': 'https://cdn.example.org/media.mp4'}
        for fields, expected in (
            ({'acodec': 'aac', 'vcodec': 'h264'}, 'complete'),
            ({'acodec': 'none', 'vcodec': 'h264'}, 'separate_media_tracks'),
            ({'acodec': 'aac', 'vcodec': 'none'}, 'separate_media_tracks'),
            ({'protocol': 'm3u8_native'}, 'streaming_media'),
            ({'protocol': 'http_dash_segments'}, 'streaming_media'),
        ):
            with self.subTest(fields=fields):
                self.assertEqual(video_evidence_state({**base, **fields}), expected)
        self.assertEqual(video_evidence_state({}), 'no_formats')

    def test_derived_track_defaults_do_not_override_extractor_unknown(self):
        item = {'url': 'https://cdn.example.org/media.mp4', 'acodec': 'aac', 'vcodec': 'h264',
                'upstream_track_evidence': {'acodec': None, 'vcodec': None}}
        self.assertEqual(video_evidence_state(item), 'unknown_media_tracks')

    def test_bounded_bmff_evidence_confirms_unknown_but_not_explicit_split(self):
        item = {'url': 'https://cdn.example.org/media.mp4',
                'upstream_track_evidence': {'acodec': None, 'vcodec': None},
                'upstream_bmff_track_evidence': {'schema': 'iso_bmff_tracks_v1',
                    'complete_moov': True, 'audio': 'present', 'video': 'present',
                    'reason': 'complete_track_table'}}
        self.assertEqual(video_evidence_state(item), 'complete')
        item['upstream_track_evidence']['acodec'] = 'none'
        self.assertEqual(video_evidence_state(item), 'separate_media_tracks')

    def test_parsehub_image_gets_same_exact_authority(self):
        result = normalize_upstream_result(
            [[3, 'https://cdn.example.org/image.png', {'extension': 'png', 'media_completeness': 'image_candidate'}]],
            match={**self.match, 'provider_id': 'parsehub_generic'}, source_url='https://example.org/post/1')
        self.assertTrue(upstream_manifest_item_is_allowed(result.assets[0].to_download_item()))

    def test_parsehub_unknown_video_does_not_receive_authority(self):
        with patch(
                'xhs_download.services.upstream_delivery._issue_assets') as issue:
            result = normalize_upstream_result(
                [[3, 'https://cdn.example.org/video.mp4', {'extension': 'mp4', 'media_completeness': 'unknown'}]],
                match={**self.match, 'provider_id': 'parsehub_generic'}, source_url='https://example.org/post/1')
        issue.assert_not_called()
        self.assertEqual(result.assets, ())
        self.assertEqual(len(result.metadata['upstream_candidates']), 1)

    def test_mixed_unknown_video_does_not_discard_valid_image(self):
        result = normalize_upstream_result(
            [[3, 'https://cdn.example.org/video.mp4', {'extension': 'mp4'}],
             [3, 'https://cdn.example.org/image.png', {'extension': 'png'}]],
            match={**self.match, 'provider_id': 'parsehub_generic'}, source_url='https://example.org/post/1')
        self.assertEqual(len(result.image_assets), 1)
        self.assertEqual(len(result.video_assets), 0)
        self.assertEqual(result.metadata['limited_media_count'], 1)

    def test_independent_parse_authorities(self):
        first = self.image_result().assets[0]
        second = self.image_result().assets[0]
        self.assertNotEqual(first.asset_id, second.asset_id)
        mixed = {**first.to_download_item(), 'provider_cache_key': second.provider_cache_key}
        self.assertFalse(upstream_manifest_item_is_allowed(mixed))

    def test_mature_urls_never_enter_dynamic_third_party_matcher(self):
        from .services.platforms.third_party import GenericThirdPartyAdapter
        with patch('xhs_download.services.upstream_matching.match_upstream_url') as matcher:
            for url in ('https://www.xiaohongshu.com/explore/example', 'https://v.douyin.com/example/',
                        'https://www.bilibili.com/video/BVexample'):
                self.assertIsNone(GenericThirdPartyAdapter().get_rule_for_url(url))
            matcher.assert_not_called()

    def test_legacy_diagnostics_cannot_execute_dynamic_matches(self):
        from types import SimpleNamespace
        from .services.platforms.third_party import GenericThirdPartyAdapter
        rule = SimpleNamespace(upstream_matches=(self.match,))
        self.assertEqual(list(GenericThirdPartyAdapter().iter_provider_specs(rule)), [])
