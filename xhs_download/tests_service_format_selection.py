"""Service-native results through normalizers, issuance, HTML and action checks."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import SimpleTestCase, Client, override_settings
from django.urls import reverse

from .services.platforms.kukutool_generic import KukutoolResultNormalizer
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer
from .services.service_format_selection import build_service_picker, capability, selected_descriptor
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

    def test_existing_manifest_delivery_keeps_its_original_home_and_batch_controls(self):
        result = self.result(payload={'videos': [{'url': 'http://media.example/original.mp4'}]})
        asset = result.video_assets[0]
        self.assertTrue(asset.provider_manifest_token)
        from .views import build_result_display_context
        context = {**build_result_display_context(result, probe_missing_video_sizes=False),
                   'status': 'success', 'index': 0}
        home = BeautifulSoup(render_to_string('partials/xhs_results_region.html', context), 'html.parser')
        self.assertFalse(home.select('[data-ytdlp-picker]'))
        self.assertEqual(len(home.select('.asset-card[data-type="video"]')), 1)
        self.assertEqual(home.select_one('.asset-card')['data-provider-manifest-token'], asset.provider_manifest_token)
        batch = BeautifulSoup(render_to_string('partials/batch_result_card.html', {'row': context}), 'html.parser')
        self.assertFalse(batch.select('[data-ytdlp-picker]'))
        self.assertTrue(batch.select('[data-batch-download-item][data-type="video"]'))
