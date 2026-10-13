"""Real, reduced native captures through Worker -> shared candidates -> consumers."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import urllib.request
from unittest.mock import patch

from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from tools.native_result_replay import (
    OfflineNetworkAttempt, offline_network, product_result, worker_payload,
)
from tools.upstream_match_worker import validate_media_url
from .services.common import XhsParseError
from .services.provider_lab.providers import parse_result_to_lab

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'xhs_download/fixtures/native_replay'
ALIASES = {'yt_dlp_generic':'yt_dlp_existing', 'gallery_dl_generic':'gallery_dl_existing',
           'parsehub_generic':'parsehub_repo', 'videodl_repo':'videodl_repo',
           'tiantianjiexi_generic':'tiantian_existing', 'kukutool_generic':'kukutool_existing'}


@override_settings(CACHES={'default': {'BACKEND':'django.core.cache.backends.locmem.LocMemCache',
                                       'LOCATION':'native-replay-tests'}})
class NativeReplayTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ('upstream_candidates', 'upstream_delivery'):
            self.stack.enter_context(patch('xhs_download.services.' + target + '.validate_public_download_url',
                                           side_effect=validate_media_url))
        self.stack.enter_context(patch('xhs_download.services.quality_execution._python_for', return_value=None))
        self.stack.enter_context(patch('xhs_download.services.upstream_delivery._normalize_downloadable_result',
                                       side_effect=XhsParseError('offline admission not evaluated')))

    def fixture(self, uid):
        return json.loads((FIXTURES/(uid+'.json')).read_text())

    def run_fixture(self, uid):
        data = self.fixture(uid)
        original = deepcopy(data)
        if data['provider'] == 'parsehub_generic':
            python = os.environ.get('PARSEHUB_COMPAT_PYTHON')
            self.assertTrue(python and Path(python).is_file())
            proc = subprocess.run([python, str(ROOT/'tools/native_result_replay.py'), '--parsehub-payload'],
                                  input=json.dumps(data), capture_output=True, text=True, timeout=20,
                                  env={'PATH':'/usr/bin:/bin','PYTHONNOUSERSITE':'1'})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
        else:
            with offline_network():
                payload = worker_payload(data['provider'], data['records'], data['source'])
        with offline_network():
            result = product_result(data['provider'], payload, data['source'])
            lab, _ = parse_result_to_lab(result, provider_id=ALIASES[data['provider']],
                                         request_id='offline-test', user_id=1)
        self.assertEqual(data, original)
        self.assertEqual(result.assets, ())
        self.assertEqual(len(lab['assets']), len(result.metadata['upstream_candidates']))
        return result, lab, data

    def test_network_and_dns_fail_loudly_before_replay(self):
        with offline_network():
            for action in (lambda: socket.getaddrinfo('example.org', 443),
                           lambda: urllib.request.urlopen('https://example.org', timeout=1),
                           lambda: subprocess.run(['curl','https://example.org'])):
                with self.assertRaises(OfflineNetworkAttempt):
                    action()

    def test_real_richtext_body_images_and_independent_holdout(self):
        for uid, count in [('b57196ed22696db0ca991f21',30), ('2fb71c141a4a5cd6bb02efa4',4)]:
            with self.subTest(uid=uid):
                result, lab, _ = self.run_fixture(uid)
                rows = result.metadata['upstream_candidates']
                self.assertEqual(len(rows), count)
                self.assertEqual([r['ordinal'] for r in rows], list(range(1,count+1)))
                self.assertTrue(all(r['media_type']=='image' and r['role']=='gallery' for r in rows))
                self.assertTrue(all(a['requires_merge'] is None for a in lab['assets']))

    def test_real_cover_does_not_inherit_video_extension(self):
        result, lab, _ = self.run_fixture('996ddbca94d3f56df92eba8d')
        cover, video = result.metadata['upstream_candidates']
        self.assertEqual((cover['role'],cover['extension']),('cover',''))
        self.assertEqual(video['extension'],'mp4')
        self.assertEqual(lab['assets'][0]['declared_extension'],'')
        self.assertTrue(all(a['requires_merge'] is None for a in lab['assets']))

    def test_real_gallery_audio_is_not_unknown_video(self):
        result, lab, _ = self.run_fixture('3f3b2e281194564de69755e1')
        candidate = result.metadata['upstream_candidates'][0]
        self.assertEqual(candidate['media_type'],'audio')
        self.assertEqual(candidate['extension'],'m4a')
        self.assertEqual(lab['assets'][0]['media_type'],'audio')
        self.assertIsNone(candidate['requires_merge'])
        self.assertIsNone(candidate['audio_asset_present'])

    def test_real_gallery_scoped_file_metadata_not_sibling_hls(self):
        result, _, _ = self.run_fixture('5d6c88d0a3cb75de9a7b28d4')
        candidate = result.metadata['upstream_candidates'][0]
        self.assertEqual((candidate['width'],candidate['height'],candidate['duration']),(1920,1080,105))
        self.assertEqual(candidate['protocol'],'https')
        self.assertEqual(candidate['media_type'],'unknown')
        self.assertEqual(candidate['audio_tracks'],'unknown')

    def test_real_videodl_streams_across_sources_remain_candidates(self):
        for uid in ('50db251dc5c7b043b8feb342', '9e5cb53d95362646ed898dd6'):
            result, lab, _ = self.run_fixture(uid)
            streams = [r for r in result.metadata['upstream_candidates'] if r['role']=='primary']
            self.assertTrue(streams)
            self.assertTrue(all(r['protocol']=='m3u8' and r['delivery_state']=='streaming_media' for r in streams))
            self.assertTrue(all(r['requires_merge'] is None for r in streams))
            self.assertTrue(any(r['delivery_state']=='streaming_media' for r in lab['assets']))

    def test_scoped_gallery_metadata_requires_exact_resource_identity(self):
        from tools.upstream_match_worker import parse_gallery
        class Input:
            pattern = r'https?://.+'
            def __init__(self, match):
                pass
            def __iter__(self):
                return iter([[3, 'https://media.example/a', {
                    'width': 640, 'file': {'url':'https://media.example/other','width':1920,'height':1080}}]])
        with offline_network():
            rows = parse_gallery(Input, 'https://source.example/1', 2)
        self.assertEqual(rows[0][2]['width'],640)
        self.assertNotIn('height',rows[0][2])

    def test_real_ytdlp_all_formats_and_languages_reach_lab(self):
        result, lab, data = self.run_fixture('421dcc02aca139ef0727c86a')
        rows = result.metadata['upstream_candidates']
        self.assertEqual(len(rows),len(data['records'][0]['formats']))
        self.assertTrue(all(r['language']=='deu' for r in rows))
        self.assertTrue(all(r['language']=='deu' for r in lab['assets']))
        self.assertTrue(any('m3u8' in r['protocol'] for r in rows))
        self.assertFalse(result.metadata['candidates_truncated'])

    def test_real_ytdlp_bad_optional_thumbnail_does_not_erase_audio(self):
        result, lab, data = self.run_fixture('88e9b92a7c517fb33ce435a4')
        self.assertTrue(any(isinstance(t['url'],list) for t in data['records'][0]['thumbnails']))
        self.assertEqual(len(result.metadata['upstream_candidates']),1)
        self.assertEqual(result.metadata['upstream_candidates'][0]['extension'],'mp3')
        self.assertEqual(lab['assets'][0]['media_type'],'audio')
        self.assertEqual(lab['assets'][0]['audio_tracks'],'unknown')
        self.assertIsNone(lab['assets'][0]['requires_merge'])

    def test_real_tiantian_formats_and_image_order_still_retained(self):
        result, _, _ = self.run_fixture('40b6b3302653089b0ed07351')
        rows=result.metadata['upstream_candidates']
        self.assertEqual(len(rows),8)
        self.assertEqual(rows[0]['format_label'],'2160P')
        self.assertTrue(all(r['height'] is None for r in rows))
        result, _, _ = self.run_fixture('e162b21c7cd6812ff941e9cc')
        self.assertEqual([c['ordinal'] for c in result.metadata['upstream_candidates']],list(range(1,10)))

    def test_real_kukutool_root_alias_does_not_duplicate_candidate(self):
        result, _, _ = self.run_fixture('1d723e3f60f5f7b7634cd84a')
        rows=result.metadata['upstream_candidates']
        self.assertEqual(len(rows),9)
        self.assertNotIn('unknown',[r['media_type'] for r in rows])
        self.assertEqual([r['format_label'] for r in rows if r['format_label']],
                         ['360P','480P','540P','720P','1080P','1080P+','2160P'])

    def test_partial_capture_never_changes_historical_failure(self):
        from tools.provider_lab.runtime.workers.worker_common import WorkerError
        data = self.fixture('f0464caa40e3f2debb792893')
        with offline_network(), self.assertRaises(WorkerError):
            worker_payload(data['provider'],data['records'],data['source'])
        self.assertEqual(data['execution_status'],'FAILED')

    def test_shared_display_context_and_both_real_templates(self):
        from .views import apply_search_result_to_context, build_result_display_context
        from .services.batch_parse.results import build_batch_result_row
        for uid in ('b57196ed22696db0ca991f21','421dcc02aca139ef0727c86a','1d723e3f60f5f7b7634cd84a'):
            result, lab, _ = self.run_fixture(uid)
            with offline_network():
                context = {'page_platform_id':'', 'show_internal_diagnostics':False}
                apply_search_result_to_context(context, result, platform_key='home', prefer_ultra_hd=False)
                row = build_batch_result_row(RequestFactory().get('/batch/'), 'https://source.example/1',
                    prefer_ultra_hd=False, prefer_youtube_merge=False, per_link_timeout_seconds=5,
                    parser=lambda *args, **kwargs: result,
                    display_context_builder=build_result_display_context,
                    query_uses_third_party_provider_func=lambda source: False)
                self.assertEqual(row['status'],'success')
                self.assertEqual(row['upstream_candidates'],context['upstream_candidates'])
                # Test artifact must not contain usable resource actions or tokens.
                context['upstream_candidates']=[{**c,'resource_token':''} for c in context['upstream_candidates']]
                row['upstream_candidates']=context['upstream_candidates']
                home = render_to_string('partials/xhs_results_region.html',context)
                batch = render_to_string('partials/batch_result_card.html',{'row':row})
            self.assertEqual(len(context['upstream_candidates']),len(lab['assets']))
            self.assertEqual(context['returned_count'],result.metadata['returned_count'])
            self.assertEqual(context['retained_count'],result.metadata['retained_count'])
            for page in (home,batch):
                self.assertEqual(page.count('data-upstream-candidate\n'),len(lab['assets']))
                self.assertIn('disabled',page)
                self.assertNotIn('media.example',page)
                self.assertNotIn('<video',page)
                self.assertIn('返回 '+str(result.metadata['returned_count']),page)
            if uid.startswith('421'):
                self.assertIn('deu',home)
                self.assertIn('deu',batch)

    def test_synthetic_unknown_language_and_escaping_remain_safe(self):
        from .services.upstream_candidates import _candidate_record
        for value, expected in [(None,None),('',None),('deu','deu')]:
            public, _ = _candidate_record('https://media.example/a',{'language':value},ordinal=1,
                provider='yt_dlp_generic',source_url='https://source.example/1',media_type='unknown',state='candidate_only')
            self.assertEqual(public['language'],expected)
        page = render_to_string('partials/upstream_candidates.html',{'upstream_candidates':[
            {'language':'<script>alert(1)</script>'}]})
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;',page)
