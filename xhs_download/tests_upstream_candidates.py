import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.common import XhsParseError
from .services.upstream_candidates import resolve_candidate_record
from .services.upstream_delivery import normalize_upstream_result


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class UpstreamCandidateTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        for module in ('upstream_delivery', 'upstream_candidates'):
            mocked = patch(f'xhs_download.services.{module}.validate_public_download_url', side_effect=lambda u: u)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.match = dict(provider_id='yt_dlp_generic', label='Example', extractor_id='Example', version='test')

    def result(self, formats):
        return normalize_upstream_result({'title': 'Example', 'formats': formats},
            match=self.match, source_url='https://example.org/post/1')

    def test_all_formats_retained_without_fabricating_tracks(self):
        result = self.result([
            dict(url='https://cdn.example.org/a.mp4', width=1920, height=1080, acodec='aac', vcodec='h264'),
            dict(url='https://cdn.example.org/b.mp4', width=3840, height=2160, acodec='none', vcodec='hevc'),
            dict(url='https://cdn.example.org/c.m4a', acodec='aac', vcodec='none'),
            dict(url='https://cdn.example.org/d.m3u8', protocol='m3u8_native'),
            dict(url='https://cdn.example.org/e.mpd', protocol='http_dash_segments'),
            dict(url='https://cdn.example.org/f.mp4'),
            dict(url='http://cdn.example.org/g.mp4'),
        ])
        candidates = result.metadata['upstream_candidates']
        self.assertEqual(len(candidates), 7)
        self.assertEqual([c['ordinal'] for c in candidates], list(range(1, 8)))
        self.assertEqual([c['delivery_state'] for c in candidates],
            ['complete', 'silent_video', 'audio_only', 'streaming_media', 'streaming_media',
             'unknown_media_tracks', 'insecure_reference'])
        self.assertIsNone(candidates[5]['acodec'])
        self.assertIsNone(candidates[5]['height'])
        self.assertEqual(candidates[1]['height'], 2160)
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(result.assets[0].url, 'https://cdn.example.org/a.mp4')

    def test_bounded_formats_report_truncation(self):
        result = self.result([dict(url=f'https://cdn.example.org/{i}.mp4') for i in range(90)])
        self.assertEqual(len(result.metadata['upstream_candidates']), 64)
        self.assertEqual(result.metadata['candidates_truncated'], 26)
        self.assertEqual(result.assets, ())

    def test_bounded_collection_retains_partial_formats_without_download_grant(self):
        result = normalize_upstream_result({'_type': 'playlist', 'upstream_scope': {'bounded': True},
            'entries': [{'title': 'First work', 'url': 'https://cdn.example.org/a.mp4'},
                        {'title': 'Second work', 'url': 'https://cdn.example.org/b.mp4'}]},
            match=self.match, source_url='https://example.org/collection')
        self.assertEqual(result.assets, ())
        self.assertEqual([c['work_label'] for c in result.metadata['upstream_candidates']],
                         ['First work', 'Second work'])
        self.assertEqual(result.metadata['unexpanded_scope']['completion'], 'unknown')

    def test_information_has_no_raw_url_or_header_values(self):
        candidate = self.result([dict(url='https://cdn.example.org/a.mp4?signature=private',
            http_headers={'Authorization': 'Bearer private-secret'},
            format_note='https://private.example.org/token')]).metadata['upstream_candidates'][0]
        serialized = json.dumps(candidate)
        for secret in ('private-secret', 'signature=', 'private.example.org', 'cdn.example.org'):
            self.assertNotIn(secret, serialized)
        self.assertTrue(candidate['requires_context'])
        record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        self.assertEqual(record['headers']['Authorization'], 'Bearer private-secret')

    def test_expiry_tampering_and_revoked_cache(self):
        candidate = self.result([dict(url='https://cdn.example.org/a.mp4')]).metadata['upstream_candidates'][0]
        token, identity = candidate['resource_token'], candidate['candidate_id']
        self.assertEqual(resolve_candidate_record(token, identity)['public'], candidate)
        for candidate_id, supplied_token in ((identity + 'x', token), (identity, token + 'x')):
            with self.assertRaises(XhsParseError):
                resolve_candidate_record(supplied_token, candidate_id)
        cache.clear()
        with self.assertRaises(XhsParseError):
            resolve_candidate_record(token, identity)

    def test_generic_allowed_user_information_and_runtime_revocation(self):
        candidate = self.result([dict(url='https://cdn.example.org/a.mp4')]).metadata['upstream_candidates'][0]
        payload = {k: candidate[k] for k in ('candidate_id', 'resource_token')}
        with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=SimpleNamespace()), \
                patch('xhs_download.services.platforms.third_party.provider_is_enabled', return_value=True), \
                patch('xhs_download.services.provider_access.upstream_match_is_blocked', return_value=False), \
                patch('xhs_download.services.provider_access.third_party_public_access_allowed', return_value=True), \
                patch('xhs_download.services.provider_access.upstream_match_is_public_allowed', return_value=True) as allowed:
            response = self.client.post('/media/upstream-candidate/', payload, content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['url'], 'https://cdn.example.org/a.mp4')
            self.assertNotIn('provider_manifest_token', response.json())
            self.assertIn('no-store', response['Cache-Control'])
            allowed.return_value = False
            self.assertEqual(self.client.post('/media/upstream-candidate/', payload,
                content_type='application/json').status_code, 403)

    def test_csrf_required(self):
        from django.test import Client
        response = Client(enforce_csrf_checks=True).post('/media/upstream-candidate/', {}, content_type='application/json')
        self.assertEqual(response.status_code, 403)
