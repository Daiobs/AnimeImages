from unittest.mock import patch
from types import SimpleNamespace

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.assets import ParseResult
from .services.provider_lab.contracts import LabAssetV1
from .services.provider_lab.providers import parse_result_to_lab
from .services.upstream_candidates import issue_candidates, resolve_candidate_record


@override_settings(CACHES={'default': {'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class QualitySemanticTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        guard = patch('xhs_download.services.upstream_candidates.validate_public_download_url', side_effect=lambda url:url)
        guard.start()
        self.addCleanup(guard.stop)

    def convert(self, rows):
        candidates = issue_candidates([dict(url='https://cdn.example.org/media', metadata=row,
            ordinal=index,provider='yt_dlp_generic',source_url='https://example.org/work',
            media_type=row.get('media_type','video'),state='unknown') for index,row in enumerate(rows,1)])
        result = ParseResult(platform='third_party',post_type='video',base_filename='fixture',
                             assets=(),metadata={'upstream_candidates':candidates,
                             'candidates_truncated':max(0,len(rows)-len(candidates))})
        return candidates, parse_result_to_lab(result,provider_id='yt_dlp_existing',user_id=1,request_id='semantic')[0]

    def test_unknown_merge_stays_null(self):
        _, lab = self.convert([{}])
        self.assertIsNone(lab['assets'][0]['requires_merge'])
        self.assertIsNone(LabAssetV1(asset_id='unknown').as_dict()['requires_merge'])

    def test_muxed_audio_is_not_independent_audio_evidence(self):
        _, lab = self.convert([{'vcodec':'h264','acodec':'aac'}])
        self.assertIsNone(lab['assets'][0]['audio_asset_present'])

    def test_video_only_does_not_imply_merge(self):
        _, lab = self.convert([{'vcodec':'h264','acodec':'none'}])
        self.assertIsNone(lab['assets'][0]['requires_merge'])

    def test_explicit_false_true_and_role_survive(self):
        candidates, lab = self.convert([{'requires_merge':False,'audio_asset_present':True,'role':'motion'}])
        self.assertIs(candidates[0].get('requires_merge'),False)
        self.assertIs(candidates[0].get('audio_asset_present'),True)
        self.assertEqual(lab['assets'][0]['role'],'motion')
        self.assertIs(lab['assets'][0]['requires_merge'],False)
        self.assertIs(lab['assets'][0]['audio_asset_present'],True)

    def test_unrecognized_role_survives_as_description_without_authority(self):
        candidates,lab = self.convert([{'role':'live_photo_video'}])
        self.assertEqual(candidates[0]['role'],'unknown')
        self.assertEqual(candidates[0]['upstream_role'],'live_photo_video')
        self.assertEqual(lab['assets'][0]['role'],'unknown')
        self.assertEqual(lab['assets'][0]['upstream_role'],'live_photo_video')
        self.assertIsNone(lab['assets'][0]['requires_merge'])
        self.assertEqual(lab['relationships'],[])

    def test_explicit_track_and_estimate_facts_survive(self):
        candidates,lab = self.convert([{'vcodec':'h264','acodec':'aac','audio_tracks':'unknown',
            'video_tracks':'absent','size_bytes':100,'size_is_estimate':True}])
        self.assertEqual(lab['assets'][0]['audio_tracks'],'unknown')
        self.assertEqual(lab['assets'][0]['video_tracks'],'absent')
        self.assertTrue(candidates[0]['size_is_estimate'])

    def test_nonboolean_facts_stay_unknown(self):
        for value in (0,1,'false','true',[],{}):
            with self.subTest(value=value):
                candidates,lab = self.convert([{'requires_merge':value,'audio_asset_present':value}])
                self.assertIsNone(candidates[0]['requires_merge'])
                self.assertIsNone(lab['assets'][0]['audio_asset_present'])

    def test_explicit_relationships_remain_work_local_without_auto_pairing(self):
        relation = {'relationship_type':'companion_audio','source_asset_id':'v','target_asset_id':'a'}
        candidates,lab = self.convert([
            {'work_id':'one','asset_id':'v','relationships':[relation],'role':'motion'},
            {'work_id':'two','asset_id':'a','media_type':'audio','role':'audio'},
            {'work_id':'one','asset_id':'a','media_type':'audio','role':'audio'},
            {'work_id':'three','asset_id':'v','vcodec':'h264','acodec':'none'},
        ])
        self.assertEqual(lab['relationships'],[{'relationship_type':'companion_audio',
            'source_asset_id':candidates[0]['candidate_id'],'target_asset_id':candidates[2]['candidate_id']}])
        self.assertTrue(all(asset['requires_merge'] is None for asset in lab['assets']))

    def test_missing_or_ambiguous_work_local_target_does_not_pair(self):
        relation = {'relationship_type':'companion_audio','source_asset_id':'v','target_asset_id':'a'}
        for targets in ([{'work_id':'two','asset_id':'a'}],
                        [{'work_id':'one','asset_id':'a'},{'work_id':'one','asset_id':'a'}]):
            _,lab = self.convert([{'work_id':'one','asset_id':'v','relationships':[relation]},*targets])
            self.assertEqual(lab['relationships'],[])

    def test_distinct_long_work_prefixes_cannot_cross_pair(self):
        prefix = 'w' * 120
        relation = {'relationship_type':'companion_audio','source_asset_id':'v','target_asset_id':'a'}
        candidates,lab = self.convert([
            {'work_id':prefix+'one','asset_id':'v','relationships':[relation]},
            {'work_id':prefix+'two','asset_id':'a','media_type':'audio'},
        ])
        self.assertEqual(lab['relationships'],[])
        self.assertEqual([c['work_id'] for c in candidates],['',''])

    def test_changed_or_overlong_endpoint_id_is_not_truncated_into_match(self):
        for invalid in ('a'*120+'tail', ' a ', 'a\n'):
            relation = {'relationship_type':'companion_audio','source_asset_id':'v','target_asset_id':invalid}
            candidates,lab = self.convert([
                {'work_id':'one','asset_id':'v','relationships':[relation]},
                {'work_id':'one','asset_id':invalid,'media_type':'audio'},
            ])
            self.assertEqual(candidates[1]['upstream_asset_id'],'')
            self.assertEqual(lab['relationships'],[])

    def test_shared_retention_1000_does_not_raise_controlled_worker_budget(self):
        from .services.provider_lab.contracts import MAX_ASSETS, MAX_RETAINED_CANDIDATES
        from .services.upstream_candidates import MAX_CANDIDATES
        self.assertEqual(MAX_ASSETS,64)
        self.assertEqual(MAX_RETAINED_CANDIDATES,1000)
        self.assertEqual(MAX_CANDIDATES,1000)
        _,lab = self.convert([{'format_id':str(index)} for index in range(1001)])
        self.assertEqual(len(lab['assets']),1000)
        self.assertEqual(lab['metadata']['returned_count'],1000)
        self.assertEqual(lab['metadata']['retained_count'],1000)
        self.assertEqual(lab['metadata']['candidate_count'],1001)
        self.assertEqual(lab['metadata']['truncated_count'],1)
        self.assertIsNone(lab['metadata']['unexpanded_count'])
        from .services.provider_lab.resources import consume_resource_token
        for asset in lab['assets']:
            resource = consume_resource_token(token=asset['resource_token'],user_id=1,
                request_id='semantic',provider_id='yt_dlp_existing',asset_id=asset['asset_id'])
            self.assertEqual(resource.url,'https://cdn.example.org/media')

    def test_actual_default_capacity_cache_retains_350_and_1000_candidates(self):
        self.assertEqual(cache._max_entries,300)
        for count in (350,1000):
            cache.clear()
            specs = [dict(url=f'https://cdn.example.org/media/{i}',metadata={},ordinal=i,
                provider='yt_dlp_generic',source_url='https://example.org/work',media_type='video',state='unknown')
                for i in range(count)]
            with patch.object(cache,'set',wraps=cache.set) as writes:
                candidates = issue_candidates(specs)
            self.assertEqual(writes.call_count,1)
            self.assertEqual(len(candidates),count)
            for i,candidate in enumerate(candidates):
                record = resolve_candidate_record(candidate['resource_token'],candidate['candidate_id'])
                self.assertEqual(record['url'],f'https://cdn.example.org/media/{i}')

    def test_bundle_token_cannot_resolve_another_candidate(self):
        from .services.common import XhsParseError
        specs = [dict(url='https://cdn.example.org/media',metadata={},ordinal=i,provider='yt_dlp_generic',
            source_url='https://example.org/work',media_type='video',state='unknown') for i in range(2)]
        candidates = issue_candidates(specs)
        with self.assertRaises(XhsParseError):
            resolve_candidate_record(candidates[0]['resource_token'],candidates[1]['candidate_id'])

    def test_private_context_byte_budget_reports_retention_without_lab_overflow(self):
        rows = [{'format_id':str(i),'headers':{'X-One':'a'*4000,'X-Two':'b'*4000}}
                for i in range(1000)]
        candidates,lab = self.convert(rows)
        self.assertGreater(len(candidates),64)
        self.assertLess(len(candidates),1000)
        self.assertEqual(len(lab['assets']),len(candidates))
        self.assertEqual(lab['metadata']['candidate_count'],1000)
        self.assertEqual(lab['metadata']['truncated_count'],1000-len(candidates))


class WorkerSemanticTests(SimpleTestCase):
    def normalize(self, **fields):
        from tools.provider_lab.runtime.workers.videodl_worker import normalize_result
        return normalize_result([SimpleNamespace(**fields)], {'request_id':'semantic','max_assets':64})[0]['assets']

    def test_common_worker_defaults_and_nonbooleans_are_unknown(self):
        from tools.provider_lab.runtime.workers.worker_common import asset
        for facts in ({}, {'requires_merge':1,'audio_asset_present':'false'}):
            row,_ = asset(asset_id='a',url='https://cdn.example.org/video.mp4',media_type='video',role='primary',**facts)
            self.assertIsNone(row['requires_merge'])
            self.assertIsNone(row['audio_asset_present'])

    def test_ffmpeg_only_and_audio_only_do_not_imply_merge(self):
        for fields in ({'download_url':'https://cdn.example.org/video.mp4','download_with_ffmpeg':True},
                       {'audio_download_url':'https://cdn.example.org/audio.m4a'}):
            row = self.normalize(**fields)[0]
            self.assertIsNone(row['requires_merge'])
            self.assertIsNone(row['audio_asset_present'])

    def test_same_info_pair_evidence_applies_only_to_primary_video(self):
        rows = self.normalize(download_url='https://cdn.example.org/video.mp4',
            audio_download_url='https://cdn.example.org/audio.m4a',cover_url='https://cdn.example.org/cover.jpg')
        for row in rows:
            if row['role'] == 'primary':
                self.assertIs(row['requires_merge'],True)
                self.assertIs(row['audio_asset_present'],True)
            else:
                self.assertIsNone(row['requires_merge'])
                self.assertIsNone(row['audio_asset_present'])

    def test_explicit_false_overrides_pair_inference(self):
        rows = self.normalize(download_url='https://cdn.example.org/video.mp4',
            audio_download_url='https://cdn.example.org/audio.m4a',requires_merge=False,audio_asset_present=False)
        self.assertIs(rows[0]['requires_merge'],False)
        self.assertIs(rows[0]['audio_asset_present'],False)
