"""Service-native results through normalizers, issuance, HTML and action checks."""
import json
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core import signing
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import SimpleTestCase, Client, RequestFactory, override_settings
from django.urls import reverse

from .services.platforms.kukutool_generic import KukutoolResultNormalizer
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer
from .services.service_format_selection import build_service_picker, capability, selected_descriptor, selected_legacy_descriptor, selected_link_descriptor
from .services.upstream_candidates import resolve_candidate_record


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class ServiceFormatSelectionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for name in ('xhs_download.services.upstream_candidates.validate_public_download_url',
                     'xhs_download.services.upstream_delivery.validate_public_download_url',
                     'xhs_download.services.platforms.kukutool_generic.validate_public_url'):
            active = patch(name, side_effect=lambda url, *args, **kwargs: url)
            active.start(); self.addCleanup(active.stop)
        self.rule = SimpleNamespace(platform_id='fixture', media_host_suffixes=('media.example',),
                                    image_media_host_suffixes=(), video_media_host_suffixes=())

    def result(self, provider='kukutool_generic', payload=None):
        identity = ('40b6b3302653089b0ed07351' if provider == 'tiantianjiexi_generic'
                    else '1d723e3f60f5f7b7634cd84a')
        if payload is None:
            fixture = json.loads((Path(__file__).parent / 'fixtures/native_replay' / (identity + '.json')).read_text())
            payload = fixture['records'][0]
            if provider == 'tiantianjiexi_generic':
                payload = payload['data']
        normalizer = TianTianJieXiResultNormalizer() if provider == 'tiantianjiexi_generic' else KukutoolResultNormalizer()
        return normalizer.normalize(rule=self.rule, platform_name='Fixture',
                                    source_url='https://source.example/work/one', payload=payload)

    def gates(self, allowed=True):
        for name, value in (
            ('xhs_download.services.provider_access.load_runtime_setting', SimpleNamespace()),
            ('xhs_download.services.platforms.third_party.provider_is_enabled', True),
            ('xhs_download.services.provider_access.upstream_match_is_blocked', False),
            ('xhs_download.services.provider_access.third_party_public_access_allowed', allowed),
            ('xhs_download.services.provider_access.upstream_match_is_public_allowed', allowed),
            ('xhs_download.services.provider_access.third_party_public_access_summary', {
                'allowed': allowed, 'platform_id': 'upstream',
                'upstream_provider_ids': ('tiantianjiexi_generic', 'kukutool_generic') if allowed else ()}),
            ('xhs_download.views.get_download_runtime_setting', SimpleNamespace()),
            ('xhs_download.views.check_download_cost_preflight', None),
            ('xhs_download.views.check_download_request_limits', ('client', None)),
        ):
            active = patch(name, return_value=value)
            active.start(); self.addCleanup(active.stop)

    def post(self, candidate, action='download', **extra):
        data = {key: candidate[key] for key in ('candidate_id', 'resource_token')}
        return self.client.post(reverse('service_selected_format'),
                                {**data, 'action': action, **extra}, content_type='application/json')

    def test_native_fixtures_keep_all_labels_without_fake_resolution_or_codecs(self):
        for provider in ('tiantianjiexi_generic', 'kukutool_generic'):
            with self.subTest(provider=provider):
                candidates = self.result(provider).metadata['upstream_candidates']
                picker = build_service_picker(candidates)
                self.assertIsNotNone(picker)
                self.assertEqual([c['format_label'] for c in picker['videos']],
                                 ['2160P', '1080P+', '1080P', '720P', '540P', '480P', '360P'])
                self.assertEqual({c['provider_id'] for c in candidates}, {provider})
                for c in picker['videos']:
                    self.assertIsNone(c['height']); self.assertIsNone(c['vcodec'])
                    self.assertEqual(c['audio_tracks'], 'unknown')
                    self.assertEqual(c['can_download'], provider == 'kukutool_generic')
                html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker})
                soup = BeautifulSoup(html, 'html.parser')
                self.assertEqual(len(soup.select('[data-picker-radio]')), 7)
                self.assertEqual(soup.select_one('[data-ytdlp-picker]')['data-provider'], provider)

    def test_selected_descriptor_is_exact_and_copy_is_not_download_permission(self):
        self.gates()
        for provider in ('tiantianjiexi_generic', 'kukutool_generic'):
            rows = self.result(provider).metadata['upstream_candidates']
            videos = [r for r in rows if r['media_type'] == 'video']
            for c in (videos[0], videos[-1]):
                record = resolve_candidate_record(c['resource_token'], c['candidate_id'])
                response = self.post(c, 'link')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['item']['url'], record['url'])
                self.assertEqual(response.json()['item']['provider'], provider)
                self.assertEqual(self.post(c).status_code, 200 if provider == 'kukutool_generic' else 409)
                self.assertEqual(self.post(c, 'check').status_code, 400)

    def test_admission_and_candidate_expiry_cannot_be_borrowed(self):
        self.gates(False)
        c = self.result().metadata['upstream_candidates'][0]
        self.assertEqual(self.post(c).status_code, 403)
        self.assertEqual(self.post(c, 'link').status_code, 403)
        self.assertEqual(self.post(c, url='https://media.example/arbitrary.mp4').status_code, 400)
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('service_selected_format'),
                         {}, content_type='application/json').status_code, 403)

    def test_generic_download_expiry_preserves_original_candidate_lifetime(self):
        self.gates()
        for provider in ('tiantianjiexi_generic', 'kukutool_generic'):
            with self.subTest(provider=provider):
                row = self.result(provider, {'title': 'Work', 'videos': [
                    {'url': 'https://media.example/video.mp4', 'quality': '720P'},
                ]}).metadata['upstream_candidates'][0]
                record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
                for now in (record['expires_at'] - 100, record['expires_at'] - 1):
                    with patch('xhs_download.services.ytdlp_selection.time', return_value=now):
                        response = self.post(row)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['item']['selection_expires_at'], int(record['expires_at'] * 1000))
                    self.assertEqual(response.json()['item']['url'], record['url'])
                with patch('xhs_download.services.ytdlp_selection.time', return_value=record['expires_at']):
                    self.assertEqual(self.post(row).status_code, 409)
                    copied = self.post(row, 'link')
                    self.assertEqual(copied.status_code, 200)
                    self.assertNotIn('selection_expires_at', copied.json()['item'])

    def test_generic_copy_without_expiry_does_not_gain_download_authority(self):
        row = self.result().metadata['upstream_candidates'][0]
        record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
        record.pop('expires_at')
        self.assertEqual(selected_link_descriptor(record)['url'], record['url'])
        with self.assertRaises(ValueError):
            selected_descriptor(record)

    def test_authorized_generic_actions_share_resource_transport_without_changing_legacy(self):
        self.gates()
        self.rule.media_host_suffixes = ('media.example', '365yg.com')
        for provider in ('tiantianjiexi_generic', 'kukutool_generic'):
            for url, strategy in (('https://v26-default.365yg.com/video.mp4', 'browser_streaming'),
                                  ('https://media.example/video.mp4', 'browser_blob')):
                row = self.result(provider, {'title': 'Work', 'videos': [
                    {'url': url, 'quality': '720P'},
                ]}).metadata['upstream_candidates'][0]
                response = self.post(row)
                self.assertEqual(response.status_code, 200, response.content)
                item = response.json()['item']
                self.assertEqual(item['url'], url)
                self.assertEqual(item['download_strategy'], strategy)
                self.assertEqual(item['provider'], provider)
                self.assertNotIn('download_strategy', self.post(row, 'link').json()['item'])
                record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
                with self.assertRaisesRegex(ValueError, 'service_legacy_binding_missing'):
                    selected_legacy_descriptor(record)

    def test_expired_and_cross_candidate_tokens_are_rejected(self):
        self.gates()
        rows = self.result().metadata['upstream_candidates']
        self.assertEqual(self.post(rows[0], 'link', resource_token=rows[1]['resource_token']).status_code, 403)
        cache.clear()
        self.assertEqual(self.post(rows[0], 'link').status_code, 403)

    def test_existing_public_platform_admission_does_not_require_an_extra_upstream_allowlist(self):
        self.gates()
        c = self.result().metadata['upstream_candidates'][0]
        with patch('xhs_download.services.provider_access.third_party_public_access_summary',
                   return_value={'allowed': True, 'platform_id': 'fixture'}), patch(
                   'xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url',
                   return_value=SimpleNamespace(provider_ids=('kukutool_generic',))), patch(
                   'xhs_download.services.provider_access.upstream_match_is_public_allowed', return_value=False):
            self.assertEqual(self.post(c, 'link').status_code, 200)
        with patch('xhs_download.services.provider_access.third_party_public_access_summary',
                   return_value={'allowed': True, 'platform_id': 'upstream',
                                 'upstream_provider_ids': ('parsehub_generic',)}):
            self.assertEqual(self.post(c, 'link').status_code, 403)

    def test_sensitive_context_streams_and_missing_tracks_stay_distinct(self):
        for extra, permitted in (({}, True), ({'http_headers': {'User-Agent': 'x'}}, True),
                ({'http_headers': {'Cookie': 'not-a-real-secret'}}, False),
                ({'http_headers': {'Referer': 'https://source.example/'}}, False),
                ({'requires_merge': True}, False), ({'has_drm': True}, False),
                ({'protocol': 'm3u8_native'}, False), ({'acodec': 'none'}, False)):
            with self.subTest(extra=extra):
                result = self.result(payload={'title': 'Work', 'videos': [{'video_fullinfo': [
                    {'url': 'https://media.example/v.mp4', 'type': 'Original', **extra}]}]})
                c = result.metadata['upstream_candidates'][0]
                self.assertEqual(capability(c)['can_download'], permitted)
                if permitted:
                    record = resolve_candidate_record(c['resource_token'], c['candidate_id'])
                    self.assertEqual(selected_descriptor(record)['audio_tracks'], 'unknown')

    def test_multiple_videos_are_independent_pickers_not_quality_siblings(self):
        result = self.result(payload={'title': 'Work', 'videos': [
            {'video_fullinfo': [{'url': 'https://media.example/a.mp4', 'type': '720P'}]},
            {'video_fullinfo': [{'url': 'https://media.example/b.mp4', 'type': '1080P'}]},
        ]})
        picker = build_service_picker(result.metadata['upstream_candidates'])
        self.assertEqual(len(picker['groups']), 2)
        html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker})
        self.assertEqual(len(BeautifulSoup(html, 'html.parser').select('[data-ytdlp-picker]')), 2)

    def test_separate_audio_is_secondary_without_inventing_a_pair(self):
        payload = {'videos': [{'url': 'https://media.example/v.mp4'}],
                   'audio_url': 'https://media.example/a.m4a'}
        result = self.result(payload=payload)
        picker = build_service_picker(result.metadata['upstream_candidates'])
        html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker})
        soup = BeautifulSoup(html, 'html.parser')
        self.assertEqual(len(soup.select('details.ytdlp-audio-formats [data-ytdlp-picker]')), 1)
        self.assertEqual(len(picker['groups']), 2)
        self.assertNotEqual(picker['groups'][0]['selected']['work_id'], picker['groups'][1]['selected']['work_id'])
        audio_only = self.result(payload={'audio_url': 'https://media.example/a.m4a'})
        html = render_to_string('partials/ytdlp_result_picker.html', {
            'ytdlp_picker': build_service_picker(audio_only.metadata['upstream_candidates'])})
        self.assertEqual(len(BeautifulSoup(html, 'html.parser').select('details.ytdlp-audio-formats')), 0)

    def test_dedicated_mp4_filename_default_does_not_relabel_generic_resources(self):
        for payload, extension in (
            ({'videos': [{'url': 'https://media.example/unknown/'}]}, '.bin'),
            ({'videos': [{'url': 'https://media.example/video.webm'}]}, '.webm'),
            ({'videos': [{'url': 'https://media.example/stream.m3u8'}]}, '.m3u8'),
            ({'audios': [{'url': 'https://media.example/audio.m4a', 'extension': 'm4a'}]}, '.m4a'),
        ):
            with self.subTest(extension=extension):
                row = self.result(payload=payload).metadata['upstream_candidates'][0]
                record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
                self.assertTrue(selected_link_descriptor(record)['filename'].endswith(extension))

    def test_existing_manifest_delivery_keeps_its_original_home_and_batch_controls(self):
        result = self.result(payload={'videos': [{'url': 'http://media.example/original.mp4'}]})
        asset = result.video_assets[0]
        self.assertTrue(asset.provider_manifest_token)
        from .views import build_result_display_context
        context = {**build_result_display_context(result, probe_missing_video_sizes=False),
                   'status': 'success', 'index': 0}
        home = BeautifulSoup(render_to_string('partials/xhs_results_region.html', context), 'html.parser')
        self.assertTrue(home.select('[data-ytdlp-picker]'))
        self.assertEqual(len(home.select('.asset-card[data-type="video"]')), 1)
        self.assertEqual(home.select_one('.asset-card')['data-provider-manifest-token'], asset.provider_manifest_token)
        batch = BeautifulSoup(render_to_string('partials/batch_result_card.html', {'row': context}), 'html.parser')
        self.assertTrue(batch.select('[data-ytdlp-picker]'))
        self.assertTrue(batch.select('[data-batch-download-item][data-type="video"]'))

    def test_ordinary_normalizer_assets_do_not_hide_usable_format_choices(self):
        from .views import build_result_display_context
        from .services.batch_parse.results import enrich_batch_media_items
        result = self.result(payload={'videos': [{'video_fullinfo': [
            {'url': 'https://media.example/high.mp4', 'type': '720P'},
            {'url': 'https://media.example/low.mp4', 'type': '540P'},
        ]}]})
        self.assertTrue(result.video_assets)
        context = {**build_result_display_context(result, probe_missing_video_sizes=False),
                   'status': 'success', 'index': 0}
        enrich_batch_media_items(context)
        for template, values in (
            ('partials/xhs_results_region.html', context),
            ('partials/batch_result_card.html', {'row': context}),
        ):
            with self.subTest(template=template):
                soup = BeautifulSoup(render_to_string(template, values), 'html.parser')
                self.assertEqual(len(soup.select('[data-picker-radio]')), 2)

    def allow_fixture_manifest(self):
        self.rule.provider_ids = ('tiantianjiexi_generic', 'kukutool_generic')
        self.rule.media_types = ('video', 'image')
        active = patch('xhs_download.services.platforms.third_party.get_third_party_rule',
                       return_value=self.rule)
        active.start(); self.addCleanup(active.stop)

    def test_manifest_is_bound_only_to_its_exact_candidate_in_both_ordinary_templates(self):
        from .views import build_result_display_context
        from .services.batch_parse.results import enrich_batch_media_items
        from .services.provider_manifest import provider_manifest_is_valid
        self.allow_fixture_manifest(); self.gates()
        for provider, key in (('tiantianjiexi_generic', 'streams'), ('kukutool_generic', 'video_fullinfo')):
            result = self.result(provider, {'videos': [{key: [
                {'url': 'http://media.example/original.mp4', 'type': '720P', 'qualityLabel': '720P'},
                {'url': 'https://media.example/other.mp4', 'type': '540P', 'qualityLabel': '540P'},
            ]}]})
            rows = result.metadata['upstream_candidates']
            old = next(c for c in rows if c.get('legacy_asset_id'))
            other = next(c for c in rows if c['candidate_id'] != old['candidate_id'])
            legacy_response = self.post(old, 'legacy').json()
            self.assertIsNone(legacy_response['server_media_relay'], 'The original route has not run yet')
            descriptor = legacy_response['item']
            self.assertEqual(descriptor['selection_delivery'], 'legacy_manifest')
            self.assertNotIn('browser_upstream_bound', descriptor)
            self.assertNotIn('download_strategy', descriptor)
            self.assertTrue(provider_manifest_is_valid(descriptor))
            self.assertEqual(descriptor['asset_id'], result.video_assets[0].asset_id)
            direct = self.post(other).json()['item']
            self.assertTrue(direct['browser_upstream_bound'])
            self.assertNotIn('provider_manifest_token', direct)
            self.assertFalse(provider_manifest_is_valid({**descriptor, 'url': direct['url']}))
            self.assertEqual(self.post(other, 'link').json()['item']['url'], direct['url'])
            context = {**build_result_display_context(result, probe_missing_video_sizes=False), 'status': 'success'}
            enrich_batch_media_items(context)
            self.assertEqual(len(context['video_items']), 1, 'Do not remove the original asset')
            for template, values in (('partials/xhs_results_region.html', context),
                                     ('partials/batch_result_card.html', {'row': context})):
                soup = BeautifulSoup(render_to_string(template, values), 'html.parser')
                self.assertEqual(len(soup.select('[data-picker-radio]')), 2)
                self.assertEqual(len(soup.select('[data-can-legacy="true"]')), 1)
                self.assertEqual(soup.select_one('[data-can-legacy="true"]')['data-legacy-asset-id'],
                                 result.video_assets[0].asset_id)
                self.assertFalse(soup.select('.asset-card[data-type="video"], [data-batch-download-item][data-type="video"]'))
                self.assertNotIn(result.video_assets[0].provider_manifest_token, str(soup))

    def test_existing_download_route_rechecks_manifest_without_fetching_media(self):
        from download_console.models import DownloadRuntimeSetting
        from django.contrib.auth.models import AnonymousUser
        from .services.download_routing import decide_download_route
        self.allow_fixture_manifest(); self.gates()
        result = self.result(payload={'videos': [{'url': 'http://media.example/file.mp4'}]})
        c = result.metadata['upstream_candidates'][0]
        item = self.post(c, 'legacy').json()['item']
        runtime = DownloadRuntimeSetting(visitor_video_download_enabled=True)
        request = RequestFactory().post('/download/')
        request.user = AnonymousUser()
        with patch.object(DownloadRuntimeSetting, 'load', return_value=runtime):
            original = decide_download_route(request, [result.video_assets[0].to_download_item()], requested_mode='auto')
            selected = decide_download_route(request, [item], requested_mode='auto')
            self.assertEqual(selected['route_type'], original['route_type'])
            self.assertEqual(selected['download_strategy'], original['download_strategy'])
            self.assertEqual(selected['route_type'], 'zip', 'Keep the real old route, not the strategy hint')
            tampered = {**item, 'url': 'https://media.example/another.mp4'}
            rejected = self.client.post(reverse('media_download_prepare'),
                {'media_items': [tampered]}, content_type='application/json')
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json()['error_code'], 'PROVIDER_MANIFEST_REJECTED')

    def test_manifest_expiry_and_private_binding_mismatch_do_not_fall_back_to_another_file(self):
        self.allow_fixture_manifest(); self.gates()
        c = self.result(payload={'videos': [{'url': 'http://media.example/v.mp4'}]}).metadata['upstream_candidates'][0]
        record = resolve_candidate_record(c['resource_token'], c['candidate_id'])
        wrong = {**record, 'service_legacy_item': {**record['service_legacy_item'], 'url': 'https://media.example/wrong.mp4'}}
        with self.assertRaises(ValueError):
            selected_legacy_descriptor(wrong)
        with patch('xhs_download.services.provider_manifest.signing.loads', side_effect=signing.BadSignature('expired')):
            with self.assertRaises(ValueError):
                selected_legacy_descriptor(record)

    def test_unmatched_and_ambiguous_assets_keep_old_controls_beside_independent_pickers(self):
        from .views import build_result_display_context
        self.allow_fixture_manifest()
        payload = {'videos': [
            {'video_fullinfo': [{'url': 'https://media.example/same.mp4', 'type': '720P'}]},
            {'video_fullinfo': [{'url': 'https://media.example/same.mp4', 'type': '720P'}]},
        ], 'images': ['https://media.example/image.jpg']}
        result = self.result(payload=payload)
        self.assertFalse(any(c.get('legacy_asset_id') for c in result.metadata['upstream_candidates']))
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        soup = BeautifulSoup(render_to_string('partials/xhs_results_region.html', context), 'html.parser')
        self.assertEqual(len(soup.select('[data-ytdlp-picker]')), 2)
        self.assertEqual(len(soup.select('.asset-card[data-type="video"]')), 1)
        self.assertEqual(len(soup.select('.asset-card[data-type="image"]')), 1)
        self.assertTrue(soup.select_one('#download-btn'))
        self.assertIn('视频资源', soup.get_text())
        self.assertFalse(soup.select('[data-picker-group="video"] details'))

    def test_mixed_images_keep_their_selection_actions_when_video_is_represented(self):
        from .views import build_result_display_context
        self.allow_fixture_manifest()
        result = self.result(payload={'videos': [{'url': 'https://media.example/video.mp4'}],
                                    'images': ['https://media.example/body.jpg']})
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        self.assertEqual(len(context['image_items']), 1)
        soup = BeautifulSoup(render_to_string('partials/xhs_results_region.html', context), 'html.parser')
        self.assertEqual(len(soup.select('[data-ytdlp-picker]')), 1)
        self.assertFalse(soup.select('.asset-card[data-type="video"]'))
        self.assertEqual(len(soup.select('.asset-card[data-type="image"]')), 1)
        self.assertTrue(soup.select_one('#download-btn'))
        self.assertTrue(soup.select_one('#select-all-btn'))
        self.assertNotIn('hidden', soup.select_one('[data-legacy-download-status]').attrs)
        self.assertIn('图片资源', soup.get_text())
        self.assertFalse(soup.select('[data-ytdlp-action="copy"], [data-ytdlp-action="legacy"]'))
        for removed in ('原有方式保存所选资源', '复制所选链接', '不需要选择解析通道'):
            self.assertNotIn(removed, soup.get_text())

    def test_single_lab_or_unbound_candidate_does_not_get_legacy_authority(self):
        from .services.upstream_candidates import issue_candidate
        c = issue_candidate('http://media.example/unbound.mp4', {'work_id': 'one'}, ordinal=1,
            provider='kukutool_generic', source_url='https://source.example/work/one',
            media_type='video', state='candidate_only')
        self.assertFalse(c.get('legacy_asset_id'))
        self.assertFalse(capability(c)['can_download'])

    def test_legacy_action_keeps_anonymous_admission_and_token_checks(self):
        self.allow_fixture_manifest(); self.gates(False)
        rows = self.result(payload={'videos': [{'video_fullinfo': [
            {'url': 'http://media.example/a.mp4', 'type': '720P'},
            {'url': 'https://media.example/b.mp4', 'type': '540P'},
        ]}]}).metadata['upstream_candidates']
        self.assertTrue(rows[0]['legacy_asset_id'])
        self.assertEqual(self.post(rows[0], 'legacy').status_code, 403)
        self.assertEqual(self.post(rows[0], 'legacy', resource_token=rows[1]['resource_token']).status_code, 403)
        cache.clear()
        self.assertEqual(self.post(rows[0], 'legacy').status_code, 403)

    def test_same_resource_labels_share_only_their_exact_old_asset(self):
        self.allow_fixture_manifest(); self.gates()
        rows = self.result(payload={'videos': [{'video_fullinfo': [
            {'url': 'http://media.example/a.mp4', 'type': '720P'},
            {'url': 'http://media.example/a.mp4', 'type': 'Original'},
        ]}]}).metadata['upstream_candidates']
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['candidate_id'], rows[1]['candidate_id'])
        self.assertEqual(rows[0]['legacy_asset_id'], rows[1]['legacy_asset_id'])
        for row in rows:
            self.assertTrue(row['same_resource_labels'])
            self.assertEqual(self.post(row, 'legacy').json()['item']['url'], 'http://media.example/a.mp4')
        picker = build_service_picker(rows)
        self.assertEqual(len(picker['videos']), 2)
        self.assertTrue(all('alias_candidate_ids' not in row for row in picker['videos']))
        soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker}), 'html.parser')
        self.assertEqual(len(soup.select('[data-picker-primary] [data-picker-radio]')), 2)
        self.assertFalse(soup.select('[data-picker-other], [data-ytdlp-action="copy"], [data-ytdlp-action="legacy"]'))

    def test_generic_native_default_is_independent_of_source_first_display_order(self):
        for provider, key in (('tiantianjiexi_generic', 'streams'), ('kukutool_generic', 'video_fullinfo')):
            for scheme in ('https', 'http'):
                with self.subTest(provider=provider, scheme=scheme):
                    rows = self.result(provider, payload={'title': 'Work', 'videos': [{key: [
                        {'url': f'{scheme}://media.example/source.mp4', 'quality_label': 'source', 'size_bytes': 9000},
                        {'url': f'{scheme}://media.example/720.mp4', 'quality_label': '720p', 'size_bytes': 3000},
                        {'url': f'{scheme}://media.example/1080.mp4', 'quality_label': '1080p', 'size_bytes': 2000},
                    ]}]}).metadata['upstream_candidates']
                    before = deepcopy(rows)
                    self.assertTrue(all(row['height'] is None for row in rows))
                    presentation = build_service_picker(rows)
                    self.assertEqual([row['label'] for row in presentation['videos']], ['source', '1080p', '720p'])
                    expected = next(row for row in rows if row['format_label'] == '1080p')
                    self.assertEqual(presentation['selected']['candidate_id'], expected['candidate_id'])
                    self.assertEqual(presentation['selected']['resource_token'], expected['resource_token'])
                    self.assertEqual(rows, before)

    def test_generic_native_facts_and_explicit_preferences_still_control_default(self):
        for provider, key in (('tiantianjiexi_generic', 'streams'), ('kukutool_generic', 'video_fullinfo')):
            with self.subTest(provider=provider):
                rows = self.result(provider, payload={'videos': [{key: [
                    {'url': 'https://media.example/source.mp4', 'quality_label': 'source', 'height': 2160},
                    {'url': 'https://media.example/720.mp4', 'quality_label': '720p', 'height': 720},
                    {'url': 'https://media.example/1080.mp4', 'quality_label': '1080p', 'height': 1080},
                ]}]}).metadata['upstream_candidates']
                self.assertEqual(build_service_picker(rows)['selected']['label'], 'source')
                preferred = build_service_picker(rows, {'max_height': 720})
                self.assertEqual(preferred['selected']['label'], '720p')
                self.assertEqual([row['label'] for row in preferred['videos']], ['source', '1080p', '720p'])

    def test_native_hdr_sdr_quality_labels_and_known_variant_facts_are_not_merged(self):
        self.allow_fixture_manifest()
        for provider, key in (('tiantianjiexi_generic', 'streams'), ('kukutool_generic', 'video_fullinfo')):
            for variants in (
                [{'quality_label': '1080p HDR'}, {'quality_label': '1080P SDR'}],
                [{'quality_label': '1080p', 'fps': 30}, {'quality_label': '1080P', 'fps': 60}],
                [{'quality_label': '1080p', 'format_id': 'track-one'},
                 {'quality_label': '1080P', 'format_id': 'track-two'}],
            ):
                with self.subTest(provider=provider, variants=variants):
                    rows = self.result(provider, payload={'videos': [{key: [
                        {'url': 'http://media.example/shared.mp4', **variant} for variant in variants
                    ]}]}).metadata['upstream_candidates']
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[0]['legacy_asset_id'], rows[1]['legacy_asset_id'])
                    self.assertTrue(rows[0]['legacy_asset_id'])
                    before = deepcopy(rows)
                    videos = build_service_picker(rows)['videos']
                    self.assertEqual(len(videos), 2)
                    self.assertTrue(all('alias_candidate_ids' not in row for row in videos))
                    self.assertEqual(rows, before)

    def test_restricted_candidates_do_not_inherit_an_old_manifest_operation(self):
        self.allow_fixture_manifest()
        for extra in ({'http_headers': {'Cookie': 'synthetic-only'}},
                      {'requires_merge': True}, {'has_drm': True}, {'protocol': 'm3u8_native'}):
            with self.subTest(extra=extra):
                result = self.result(payload={'videos': [{'video_fullinfo': [
                    {'url': 'https://media.example/a.mp4', 'type': '720P', **extra},
                ]}]})
                row = result.metadata['upstream_candidates'][0]
                self.assertFalse(row.get('legacy_asset_id'))
                self.assertFalse(capability(row)['can_download'])

    def test_real_template_and_descriptor_pass_the_existing_js_identity_validator(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is required for the cross-language action contract; installed in CI')
        self.allow_fixture_manifest(); self.gates()
        for provider in ('tiantianjiexi_generic', 'kukutool_generic'):
            result = self.result(provider, {'videos': [{'url': 'http://media.example/main.mp4'}]})
            rows = result.metadata['upstream_candidates']
            html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': build_service_picker(rows)})
            radio = BeautifulSoup(html, 'html.parser').select_one('[data-can-legacy="true"]')
            selection = {'id': radio['value'], 'provider': provider, 'schema': 'service_selected_format_v1',
                         'kind': radio['data-kind'], 'canLegacy': radio['data-can-legacy'] == 'true',
                         'legacyAssetId': radio['data-legacy-asset-id'], 'formatId': radio['data-format-id']}
            self.assertEqual(selection['formatId'], '', 'Do not invent a native service format ID')
            response = self.post(rows[0], 'legacy')
            self.assertEqual(response.status_code, 200)
            script = """const fs = require('node:fs');
const picker = require('./static/js/ytdlp_result_picker.js');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const item = picker.validateItem(data.item, data.selection, 'legacy_manifest');
if (item.asset_id !== data.selection.legacyAssetId) process.exit(2);
"""
            checked = subprocess.run([node, '-e', script], input=json.dumps({
                'item': response.json()['item'], 'selection': selection}), text=True,
                capture_output=True, timeout=10, cwd=Path(__file__).resolve().parent.parent)
            self.assertEqual(checked.returncode, 0, checked.stderr)
