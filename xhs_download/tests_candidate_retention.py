"""Offline retention regressions: display limits must not erase parse evidence."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.core import signing
from django.core.cache import cache

from .services import upstream_candidates, upstream_delivery, quality_execution


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class CandidateRetentionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        for module in (upstream_candidates, upstream_delivery):
            guard = patch.object(module, 'validate_public_download_url', side_effect=lambda value: value)
            guard.start()
            self.addCleanup(guard.stop)
        admission = patch.object(upstream_delivery, '_normalize_downloadable_result',
                                 return_value=SimpleNamespace(assets=()))
        admission.start()
        self.addCleanup(admission.stop)
        self.match = {'provider_id': 'yt_dlp_generic', 'extractor_id': 'Fixture', 'version': 'test'}
        self.source = 'https://example.org/post/1'

    def formats(self, count=80):
        formats = [{'url': f'https://media.example/{i}.mp4', 'format_id': str(i),
                    'height': 720, 'vcodec': 'h264', 'acodec': 'aac'} for i in range(count)]
        formats[-3].update(height=2160, vcodec='av1')
        formats[-2].update(vcodec='none', acodec='opus')
        formats[-1].update(vcodec=None, acodec=None)
        return formats

    def normalize(self, payload, provider='yt_dlp_generic'):
        return upstream_delivery.normalize_upstream_result(
            payload, match={**self.match, 'provider_id': provider}, source_url=self.source)

    def test_tail_quality_audio_unknown_are_retained(self):
        rows = self.normalize({'formats': self.formats()}).metadata['upstream_candidates']
        self.assertEqual(len(rows), 80)
        self.assertEqual([r['format_id'] for r in rows], [str(i) for i in range(80)])
        self.assertEqual(rows[-3]['height'], 2160)
        self.assertEqual(rows[-3]['vcodec'], 'av1')
        self.assertEqual(rows[-2]['media_type'], 'audio')
        self.assertIsNone(rows[-1]['acodec'])

    def test_images_after_display_and_old_worker_limit_are_retained(self):
        result = self.normalize([[3, f'https://media.example/{i}.jpg',
                                  {'extension': 'jpg', 'width': i + 1}] for i in range(120)],
                                'gallery_dl_generic')
        self.assertEqual(len(result.metadata['upstream_candidates']), 120)

    def test_nested_playlist_media_retained_without_download_admission(self):
        video = {'formats': self.formats(), 'title': 'work'}
        payload = {'_type': 'playlist', 'upstream_scope': {'bounded': True}, 'entries': [
            {'_type': 'playlist', 'upstream_scope': {'bounded': True}, 'entries': [video]}]}
        result = self.normalize(payload)
        self.assertEqual(len(result.metadata['upstream_candidates']), 80)
        self.assertEqual(result.assets, ())

    def test_videodl_tail_retained(self):
        payload = [[3, item['url'], {**item, 'media_type': 'video'}] for item in self.formats()]
        result = quality_execution._normalize_videodl_candidates(
            payload, {**self.match, 'provider_id': 'videodl_repo'}, self.source)
        self.assertEqual(len(result.metadata['upstream_candidates']), 80)
        self.assertEqual(result.metadata['upstream_candidates'][-3]['height'], 2160)

    def test_hardcap_reports_returned_retained_and_unexpanded(self):
        result = self.normalize({'formats': self.formats(1005)})
        self.assertEqual(len(result.metadata['upstream_candidates']), 1000)
        self.assertEqual(result.metadata['candidate_returned_count'], 1005)
        self.assertEqual(result.metadata['candidate_retained_count'], 1000)
        self.assertEqual(result.metadata['candidate_unexpanded_count'], 5)

    def test_thousand_real_cache_records_survive_lab_mapping(self):
        from .services.provider_lab.providers import parse_result_to_lab
        from .services.provider_lab.resources import consume_resource_token
        result = self.normalize({'formats': self.formats(1000)})
        public, _ = parse_result_to_lab(result, provider_id='yt_dlp_existing',
                                       user_id=1, request_id='retention-cache')
        self.assertEqual(len(public['assets']), 1000)
        for index in (0, 500, 999):
            candidate = result.metadata['upstream_candidates'][index]
            record = upstream_candidates.resolve_candidate_record(
                candidate['resource_token'], candidate['candidate_id'])
            self.assertEqual(record['public']['format_id'], str(index))
            asset = public['assets'][index]
            resource = consume_resource_token(token=asset['resource_token'], user_id=1,
                request_id='retention-cache', provider_id='yt_dlp_existing', asset_id=asset['asset_id'])
            self.assertTrue(resource.url.endswith(f'/{index}.mp4'))
        self.assertLess(len(cache._cache), 20)

    def test_rejected_counts_and_exact_long_url(self):
        formats = self.formats(4)
        exact = 'https://media.example/item?signature=' + 'x' * 5000
        formats[0]['url'] = exact
        formats[1]['url'] = 'file:///not-a-network-resource'
        result = self.normalize({'formats': formats})
        meta = result.metadata
        self.assertEqual((meta['declared_count'], meta['examined_count'], meta['retained_count'],
                          meta['rejected_count'], meta['unexpanded_count']), (4, 4, 3, 1, 0))
        candidate = meta['upstream_candidates'][0]
        record = upstream_candidates.resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        self.assertEqual(record['url'], exact)

    def test_shared_service_work_counts_toward_exact_cache_budget(self):
        specs = [dict(url=f'https://media.example/{i}.mp4', metadata={
            'work_id': 'one', '_service_picker_issued': True,
        }, ordinal=i + 1, provider='kukutool_generic', source_url=self.source,
            media_type='video', state='candidate_only') for i in range(3)]
        work = {'title': 'Work', 'author': 'Author', 'cover_url': 'https://media.example/cover.jpg'}

        def issue(count, budget):
            with patch.object(upstream_candidates, 'MAX_CANDIDATE_BUNDLE_BYTES', budget), \
                    patch.object(upstream_candidates.time, 'time', return_value=1700000000):
                with patch.object(cache, 'set', wraps=cache.set) as writes:
                    rows = upstream_candidates.issue_candidates(specs[:count], work=work)
                if rows:
                    self.assertEqual(writes.call_count, 1)
                    self.assertEqual(writes.call_args.kwargs['timeout'], upstream_candidates.TTL)
                    stored = writes.call_args.args[1]
                    size = len(json.dumps(stored, ensure_ascii=True, separators=(',', ':')).encode())
                    self.assertLessEqual(size, budget)
                    return rows, size
                writes.assert_not_called()
                return rows, 0

        _, one_size = issue(1, upstream_candidates.MAX_CANDIDATE_BUNDLE_BYTES)
        _, two_size = issue(2, upstream_candidates.MAX_CANDIDATE_BUNDLE_BYTES)
        self.assertEqual(len(issue(3, one_size - 1)[0]), 0)
        self.assertEqual(len(issue(3, one_size)[0]), 1)
        self.assertEqual(len(issue(3, two_size - 1)[0]), 1)
        self.assertEqual(len(issue(3, two_size)[0]), 2)

    def test_missing_shared_work_rejects_instead_of_borrowing_another_record(self):
        from .services.common import XhsParseError
        row = upstream_candidates.issue_candidates([dict(url='https://media.example/one.mp4',
            metadata={'work_id': 'one', '_service_picker_issued': True}, ordinal=1,
            provider='kukutool_generic', source_url=self.source, media_type='video', state='candidate_only')],
            work={'title': 'Work'})[0]
        payload = signing.loads(row['resource_token'], salt=upstream_candidates.SALT)
        key = 'upstream:candidate-bundle:' + payload['bundle']
        stored = cache.get(key)
        stored.pop('_shared_work')
        cache.set(key, stored, timeout=upstream_candidates.TTL)
        with self.assertRaises(XhsParseError):
            upstream_candidates.resolve_candidate_record(row['resource_token'], row['candidate_id'])
