import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.assets import ParseResult
from .services import quality_execution as execution
from .services.provider_lab.contracts import LabAssetV1, ProviderLabError
from .services.provider_lab.providers import parse_result_to_lab, run_provider, PROVIDER_REGISTRY
from .services.provider_lab.resources import consume_resource_token


SOURCE = 'https://example.org/post/123?access=retained%2B'
URL = 'https://cdn.example.org/media.mp4?signature=private'


@override_settings(CACHES={'default': {'BACKEND':'django.core.cache.backends.locmem.LocMemCache'}})
class SharedQualityExecutionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        for target in ('xhs_download.services.upstream_candidates.validate_public_download_url',
                       'xhs_download.services.upstream_delivery.validate_public_download_url'):
            mock = patch(target, side_effect=lambda url: url)
            mock.start()
            self.addCleanup(mock.stop)

    def response(self, provider, payload, **extra):
        return payload, {'provider_id':provider, 'extractor_id':'installed',
                'version':'test', 'label':'test'}, extra

    def execute(self, provider, payload, **extra):
        canonical = execution.canonical_provider_id(provider)
        with patch.object(execution, '_python_for', return_value='/isolated/python'), \
                patch.object(execution, '_execute_upstream', return_value=self.response(canonical,payload,**extra)) as worker:
            result = execution.execute_provider(provider,SOURCE,timeout_seconds=12)
        worker.assert_called_once()
        worker.assert_called_once_with(canonical, SOURCE, 12, expected_match=None)
        return result

    def test_gallery_is_registered_and_uses_shared_isolated_execution(self):
        self.assertIn('gallery_dl_existing',PROVIDER_REGISTRY)
        result=self.execute('gallery_dl_existing',[[3,'http://cdn.example.org/image.jpg',{'extension':'jpg'}]])
        self.assertIsInstance(result,ParseResult)
        self.assertEqual(len(result.metadata['upstream_candidates']),1)
        self.assertEqual(result.metadata['upstream_candidates'][0]['delivery_state'],'insecure_reference')
        self.assertEqual(result.assets,())

    def test_same_url_formats_split_audio_stream_and_unknown_are_not_lost(self):
        formats=[
            {'url':URL,'format_id':'1080','height':1080,'vcodec':'h264','acodec':'aac'},
            {'url':URL,'format_id':'2160','height':2160,'fps':60,'vcodec':'vp9','acodec':'none','filesize_approx':1000},
            {'url':'https://cdn.example.org/audio.m4a','vcodec':'none','acodec':'aac'},
            {'url':'https://cdn.example.org/stream.m3u8','protocol':'m3u8_native'},
        ]
        result=self.execute('yt_dlp_existing',{'title':'test','formats':formats})
        lab,_=parse_result_to_lab(result,provider_id='yt_dlp_existing',user_id=7,request_id='fixture-request')
        assets=lab['assets']
        self.assertEqual(len(assets),4)
        self.assertEqual(assets[1]['height'],2160)
        self.assertEqual(assets[1]['fps'],60)
        self.assertEqual(assets[1]['acodec'],'none')
        self.assertEqual(assets[1]['audio_tracks'],'absent')
        self.assertIsNone(assets[1]['requires_merge'])
        self.assertTrue(assets[1]['size_is_estimate'])
        self.assertEqual(assets[2]['media_type'],'audio')
        self.assertEqual(assets[3]['audio_tracks'],'unknown')
        self.assertEqual(assets[3]['video_tracks'],'unknown')
        self.assertIsNone(assets[3]['requires_merge'])
        self.assertIsNone(assets[3]['fps'])
        self.assertNotIn('signature',json.dumps(lab))
        urls=[]
        for asset in assets[:2]:
            resource=consume_resource_token(token=asset['resource_token'],user_id=7,
                request_id='fixture-request',provider_id='yt_dlp_existing',asset_id=asset['asset_id'])
            urls.append(resource.url)
        self.assertEqual(urls,[URL,URL])

    def test_formats_beyond_display_limit_remain_retained(self):
        result=self.execute('yt_dlp_existing',{'formats':[
            {'url':URL,'format_id':str(index),'height':index+1} for index in range(70)]})
        lab,_=parse_result_to_lab(result,provider_id='yt_dlp_existing',user_id=1,request_id='many-formats')
        self.assertEqual(len(lab['assets']),70)
        self.assertEqual(lab['metadata']['candidate_count'],70)
        self.assertEqual(lab['metadata']['truncated_count'],0)
        self.assertFalse(lab['metadata']['truncated'])

    def test_parsehub_runner_does_not_import_parsehub_in_parent(self):
        before={name for name in sys.modules if name=='parsehub' or name.startswith('parsehub.')}
        result=self.execute('parsehub_repo',[[3,URL,{'media_type':'video','extension':'mp4'}]])
        descriptor = replace(PROVIDER_REGISTRY['parsehub_repo'], enabled_setting='', availability=lambda: True)
        with patch.dict(PROVIDER_REGISTRY, {'parsehub_repo': descriptor}), \
                patch.object(execution,'execute_provider',return_value=result) as shared:
            lab=run_provider(provider_id='parsehub_repo',source=SOURCE,user_id=1)
        self.assertEqual(lab['status'],'success')
        self.assertEqual(len(lab['assets']),1)
        shared.assert_called_once()
        with patch.dict(PROVIDER_REGISTRY, {'parsehub_repo': replace(descriptor, availability=lambda: False)}), \
                patch.object(execution,'execute_provider') as blocked:
            unavailable = run_provider(provider_id='parsehub_repo',source=SOURCE,user_id=1)
        self.assertEqual(unavailable['status'], 'not_configured')
        self.assertFalse(unavailable['executed'])
        blocked.assert_not_called()
        self.assertEqual(before,{name for name in sys.modules if name=='parsehub' or name.startswith('parsehub.')})

    def test_provider_identity_mismatch_rejected(self):
        with patch.object(execution,'_python_for',return_value='/isolated/python'), \
                patch('xhs_download.services.upstream_matching._context',return_value=(['python'],'2026.3.17','fixture')), \
                patch('xhs_download.services.upstream_matching._call',return_value=[{'provider_id':'parsehub_generic','version':'2026.3.17'}]):
            with self.assertRaises(ProviderLabError):
                execution.execute_provider('yt_dlp_existing',SOURCE)

    def test_expected_match_selects_admitted_second_extractor_not_blocked_first(self):
        first = {'provider_id':'yt_dlp_generic','extractor_id':'Blocked','version':'2026.3.17','label':'fixture'}
        admitted = {**first,'extractor_id':'Admitted'}
        payload = {'formats':[{'url':URL}]}
        with patch.object(execution,'_python_for'), \
                patch('xhs_download.services.upstream_matching._context',return_value=(['python'],'2026.3.17','fixture')), \
                patch('xhs_download.services.upstream_matching._call',side_effect=[[first, admitted],payload]) as worker:
            execution.execute_provider('yt_dlp_generic',SOURCE,expected_match=admitted)
        self.assertEqual(worker.call_args_list[1].args[1]['extractor_id'],'Admitted')

    def test_expected_match_missing_or_version_changed_never_parses(self):
        admitted = {'provider_id':'yt_dlp_generic','extractor_id':'Admitted','version':'2026.3.17'}
        for fresh in ([{**admitted,'extractor_id':'Blocked'}], [{**admitted,'version':'changed'}]):
            with self.subTest(fresh=fresh), \
                    patch('xhs_download.services.upstream_matching._context',return_value=(['python'],'2026.3.17','fixture')), \
                    patch('xhs_download.services.upstream_matching._call',return_value=fresh) as worker:
                with self.assertRaises(ProviderLabError):
                    execution._execute_upstream('yt_dlp_generic',SOURCE,10,expected_match=admitted)
                worker.assert_called_once()

    def test_parsehub_expected_match_selects_exact_fresh_identity(self):
        from .services import upstream_parsehub
        admitted = {'provider_id':'parsehub_generic','extractor_id':'Admitted','version':'pinned','label':'fixture'}
        first = {**admitted,'extractor_id':'Blocked'}
        with patch.object(upstream_parsehub,'valid_match',return_value=True), \
                patch.object(upstream_parsehub,'_call',return_value=[first,admitted]), \
                patch.object(upstream_parsehub,'parse_upstream_url',return_value=[]) as parse:
            execution._execute_upstream('parsehub_generic',SOURCE,10,expected_match=admitted)
        self.assertEqual(parse.call_args.args[0],admitted)

    def test_worker_rejection_keeps_safe_reason_for_product_and_lab(self):
        from .services.upstream_parsehub import ParseHubBridgeError
        from .services.upstream_matching import UpstreamMatchingError
        for provider, target, error_type in (
            ('parsehub_generic', 'xhs_download.services.upstream_parsehub._call', ParseHubBridgeError),
            ('yt_dlp_generic', 'xhs_download.services.upstream_matching._context', UpstreamMatchingError),
        ):
            for code in ('access_denied', 'auth_required', 'tls_error', 'identity_mismatch'):
                with self.subTest(provider=provider, code=code), patch(target, side_effect=error_type(code)):
                    with self.assertRaises(ProviderLabError) as caught:
                        execution._execute_upstream(provider, SOURCE, 10)
                    self.assertEqual(caught.exception.detail, 'UPSTREAM_' + code.upper())
            with patch(target, side_effect=error_type('https://private.example/?token=secret')):
                with self.assertRaises(ProviderLabError) as caught:
                    execution._execute_upstream(provider, SOURCE, 10)
                self.assertEqual(caught.exception.detail, '')

    def test_unknown_metadata_has_explicit_unknown_not_missing_tracks(self):
        asset=LabAssetV1(asset_id='a').as_dict()
        self.assertEqual(asset['audio_tracks'],'unknown')
        self.assertEqual(asset['video_tracks'],'unknown')
        self.assertIsNone(asset['fps'])
        self.assertIsNone(asset['bitrate'])

    def test_context_flags_block_public_resource_url_without_exposing_headers(self):
        from .services.provider_lab.resources import public_resource_descriptor
        for flags in ({'headers_present':True}, {'requires_cookie':True}, {'requires_context':True}):
            with self.subTest(flags=flags):
                candidate = {'candidate_id':'context', 'resource_token':'private-token',
                             'media_type':'video', **flags}
                result = ParseResult(platform='third_party',post_type='video',base_filename='context',
                                     assets=(),metadata={'upstream_candidates':[candidate]})
                with patch('xhs_download.services.upstream_candidates.resolve_candidate_record',
                           return_value={'url':URL}):
                    lab,_ = parse_result_to_lab(result,provider_id='yt_dlp_existing',
                                                user_id=7,request_id='context-test')
                asset = lab['assets'][0]
                resource = consume_resource_token(token=asset['resource_token'],user_id=7,
                    request_id='context-test',provider_id='yt_dlp_existing',asset_id=asset['asset_id'])
                public = public_resource_descriptor(resource)
                self.assertFalse(public['inspectable'])
                self.assertNotIn('url',public)
                self.assertNotIn('signature',json.dumps(lab))

    def test_installed_gallery_runs_in_real_isolated_worker_without_network(self):
        root=Path(__file__).resolve().parents[1]
        fixture=f'''
import sys
sys.path.insert(0, {str(root)!r})
from tools import upstream_match_worker as worker
from gallery_dl.extractor.message import Message
class InstalledGalleryFixture:
    category = 'youtube'
    subcategory = 'image'
    pattern = r'https://example\\.org/.*'
    def __init__(self, match):
        pass
    def __iter__(self):
        yield Message.Url, 'https://cdn.example.org/original.png', {{'extension':'png','width':2160}}
worker.extractor_classes=lambda provider: [InstalledGalleryFixture]
worker.validate_url=lambda url: url
worker.main()
'''
        with tempfile.TemporaryDirectory() as temp:
            worker=Path(temp)/'fixture.py'
            worker.write_text(fixture)
            with patch('xhs_download.services.upstream_matching._context',
                       return_value=([sys.executable,'-I','-B',str(worker)],'1.32.4','fixture')):
                payload, match, _ = execution._execute_upstream('gallery_dl_generic', SOURCE, 10)
        self.assertEqual(match['provider_id'],'gallery_dl_generic')
        self.assertEqual(payload[0][2]['width'],2160)

    @override_settings(PARSEHUB_UPSTREAM_ENABLED=False)
    def test_parsehub_disabled_gate_prevents_execution(self):
        with patch.object(execution, '_execute_upstream') as worker:
            with self.assertRaises(ProviderLabError):
                execution.execute_provider('parsehub_repo', SOURCE)
        worker.assert_not_called()

    def test_worker_version_pin_prevents_parse(self):
        with patch('xhs_download.services.upstream_matching._context',return_value=(['python'],'unreviewed','fixture')), \
                patch('xhs_download.services.upstream_matching._call') as worker:
            with self.assertRaises(ProviderLabError):
                execution._execute_upstream('gallery_dl_generic', SOURCE, 10)
        worker.assert_not_called()

    def test_videodl_is_canonical_candidate_only_not_download_authority(self):
        from .services.provider_access import UPSTREAM_PROVIDER_IDS
        payload = [[3, URL, {'media_type':'video','vcodec':'h264','acodec':'aac'}]]
        match = {'provider_id':'videodl_repo','extractor_id':'fixture','version':'pinned'}
        with patch.object(execution,'_python_for'), \
                patch.object(execution,'_videodl_payload',return_value=(payload,match,{})) as worker, \
                patch('xhs_download.services.upstream_delivery.normalize_upstream_result') as delivery:
            result = execution.execute_provider('videodl_repo',SOURCE,timeout_seconds=7)
        self.assertNotIn('videodl_repo',UPSTREAM_PROVIDER_IDS)
        delivery.assert_not_called()
        self.assertEqual(result.assets,())
        self.assertEqual(result.metadata['upstream_candidates'][0]['provider_id'],'videodl_repo')
        self.assertEqual(worker.call_args.kwargs['timeout_seconds'],7)

    def test_saved_videodl_matches_only_actual_controlled_dedicated_backend(self):
        inventory = [SimpleNamespace(backend_id='Fixture',backend_category='dedicated',controlled_available=True)]
        with patch.object(execution,'_python_for',return_value='/isolated/python'), \
                patch('xhs_download.services.provider_lab.videodl_control.load_videodl_inventory',return_value=inventory), \
                patch('xhs_download.services.provider_lab.runtime.execute_provider_worker',return_value={'backend_ids':['Fixture']}) as worker:
            matches = execution.match_saved_videodl(SOURCE)
        self.assertEqual(matches[0]['provider_id'],'videodl_repo')
        self.assertEqual(matches[0]['extractor_id'],'Fixture')
        self.assertEqual(worker.call_args.kwargs['operation'],'match_dedicated')
        self.assertEqual(worker.call_args.kwargs['source'],SOURCE)
        self.assertLessEqual(worker.call_args.kwargs['timeout_seconds'],5)

    def test_saved_videodl_returns_concrete_backend_ids_in_bounded_match_order(self):
        inventory = [SimpleNamespace(backend_id=f'Fixture{i}', backend_category='dedicated',
                                     controlled_available=True) for i in range(10)]
        ids = [item.backend_id for item in inventory]
        with patch.object(execution,'_python_for',return_value='/isolated/python'), \
                patch('xhs_download.services.provider_lab.videodl_control.load_videodl_inventory',return_value=inventory), \
                patch('xhs_download.services.provider_lab.runtime.execute_provider_worker',return_value={'backend_ids':ids}):
            matches = execution.match_saved_videodl(SOURCE)
        self.assertEqual([match['extractor_id'] for match in matches], ids[:8])

    def test_saved_videodl_empty_blocked_and_unknown_matches_are_not_admitted(self):
        inventory = [SimpleNamespace(backend_id='Blocked',backend_category='dedicated',controlled_available=False)]
        with patch.object(execution,'_python_for',return_value='/isolated/python'), \
                patch('xhs_download.services.provider_lab.videodl_control.load_videodl_inventory',return_value=inventory), \
                patch('xhs_download.services.provider_lab.runtime.execute_provider_worker') as worker:
            for ids in ([],['Blocked']):
                worker.return_value = {'backend_ids':ids}
                self.assertEqual(execution.match_saved_videodl(SOURCE),())
            for ids in (['Unknown'],[True],['Blocked']*17):
                worker.return_value = {'backend_ids':ids}
                with self.assertRaises(ProviderLabError):
                    execution.match_saved_videodl(SOURCE)

    def test_saved_videodl_common_or_legacy_options_rejected_before_worker(self):
        with patch.object(execution,'_python_for') as worker:
            for options in ({'mode':'legacy_auto'}, {'mode':'common_only'}, {'mode':'dedicated_only','external_sharing_confirmed':True}):
                with self.assertRaises(ProviderLabError):
                    execution.match_saved_videodl(SOURCE,options)
        worker.assert_not_called()
