"""Dedicated Douyin service -> real result consumers, no remote requests."""
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from .services.platforms.douyin import DouyinAdapter
from .services.candidates import MediaCandidate
from .services.common import XhsDownloadError, XhsParseError
from .services.douyin_continuation import serialize_douyin_continuation_result, deserialize_douyin_continuation_result
from .services.platforms.douyin_providers import (
    DouyinParseContext, DouyinTianTianJieXiProvider,
    attach_douyin_service_picker, build_external_parse_result,
)
from .services.platforms.tiantianjiexi_generic import TianTianJieXiApiClient, TianTianJieXiGenericProvider
from .services.service_format_selection import (
    build_service_picker, capability, selected_descriptor,
    selected_legacy_descriptor, selected_link_descriptor,
)
from .services.upstream_candidates import resolve_candidate_record
from .services.downloads import (
    provider_manifest_allows_public_download, request_public_provider_get_with_retry,
    validate_provider_signed_download_url,
)
from .services.provider_recovery import get_kukutool_manifest_identity, get_tiantianjiexi_manifest_identity
from .views import build_result_display_context

AWEME = '7669858827638845160'
BASE = 'https://v3-dy-o.zjcdn.com/fixture/'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class DouyinFormatSelectionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for target in ('requests.sessions.Session.request', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Unexpected network request'))
            guard.start()
            self.addCleanup(guard.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, replacement in (
            ('xhs_download.services.platforms.tiantian_serial.lock_root', lambda: Path(tmp.name)),
            ('xhs_download.services.platforms.tiantian_serial.runtime_str', lambda *a: 'fixture-account'),
            ('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', lambda: True),
            ('xhs_download.services.platforms.tiantianjiexi_generic.provider_configured', lambda: True),
            ('xhs_download.services.platforms.tiantianjiexi_generic.runtime_str',
             lambda name, *a: 'https://service.example/parse' if name.endswith('BASE_URL') else 'fixture'),
            ('xhs_download.services.upstream_candidates.validate_public_download_url', lambda url: url),
            ('xhs_download.services.upstream_delivery.validate_public_download_url', lambda url: url),
        ):
            p = patch(target, side_effect=replacement); p.start(); self.addCleanup(p.stop)
        self.settings = SimpleNamespace(provider_is_enabled=lambda name: True,
                                       ultra_total_timeout_seconds=6, enabled_provider_order=('tiantianjiexi',))
        self.session = Mock()
        self.session.post.return_value.json.return_value = {'code': 200, 'data': {
            'title': 'Fixture work', 'cover': 'https://p3.douyinpic.com/fixture.jpg',
            'videos': [{'streams': [
                {'url': BASE + 'high.mp4', 'qualityLabel': '1080P', 'sizeInBytes': 3000},
                {'url': BASE + 'low.mp4', 'qualityLabel': '720P', 'sizeInBytes': 2000},
                {'url': BASE + 'low.mp4', 'qualityLabel': '流畅', 'sizeInBytes': 2000},
            ]}],
        }}

    def result(self, aweme=AWEME, race=False):
        context = DouyinParseContext(url=f'https://www.douyin.com/video/{aweme}', aweme_id=aweme, prefer_ultra_hd=True)
        provider = DouyinTianTianJieXiProvider(provider_settings=self.settings,
            provider=TianTianJieXiGenericProvider(client=TianTianJieXiApiClient(session=self.session)),
            reserve_fallback_grace=False)
        if race:
            from .services.assets import MediaAsset, ParseResult
            from .services.platforms.douyin_providers import DouyinUltraRaceProvider
            context.direct_result = ParseResult(platform='douyin', post_type='video', base_filename='native',
                assets=(MediaAsset(media_type='video', platform='douyin', url=BASE + 'native.mp4'),))
            return DouyinUltraRaceProvider(provider_settings=self.settings, providers=(provider,)).parse(DouyinAdapter(), context)
        return provider.parse(DouyinAdapter(), context)

    def test_real_race_keeps_selected_main_and_full_format_projection(self):
        self.session.post.return_value.json.return_value['data']['videos'][0]['streams'][0]['sizeInBytes'] = 60 * 1024 * 1024
        result = self.result(race=True)
        self.assertEqual(result.metadata['ultra_race_winner'], 'tiantianjiexi')
        self.assertEqual(result.video_assets[0].url, BASE + 'high.mp4')
        self.assertEqual(len(result.metadata['upstream_candidates']), 3)
        self.session.post.assert_called_once()

    def bridge_result(self, provider, candidates, *, payload=None, missing_manifest=False):
        context = DouyinParseContext(url=f'https://www.douyin.com/video/{AWEME}', aweme_id=AWEME)
        main = build_external_parse_result(DouyinAdapter(), context, candidates[0], provider_name=provider)
        if missing_manifest:
            main = replace(main, assets=(replace(main.video_assets[0], provider_manifest_token=''),))
        return attach_douyin_service_picker(
            main, adapter=DouyinAdapter(), context=context, candidates=tuple(candidates), payload=payload,
        )

    def record(self, candidate):
        return resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])

    def test_same_url_aliases_keep_exact_default_and_real_main_binding(self):
        winner = MediaCandidate(url=BASE + 'main.mp4', quality_label='Original', size_bytes=9000000)
        alias = replace(winner, quality_label='2160P', size_bytes=8000000)
        other_metadata = replace(winner, size_bytes=7000000)
        context = DouyinParseContext(url=f'https://www.douyin.com/video/{AWEME}', aweme_id=AWEME)
        original = build_external_parse_result(DouyinAdapter(), context, winner, provider_name='tiantianjiexi')
        result = attach_douyin_service_picker(
            original, adapter=DouyinAdapter(), context=context,
            candidates=(alias, other_metadata, winner),
        )
        rows = result.metadata['upstream_candidates']
        self.assertIs(result.video_assets[0], original.video_assets[0])
        self.assertEqual([r['dedicated_default'] for r in rows], [False, False, True])
        selected = build_service_picker(rows)['selected']
        self.assertEqual(selected['candidate_id'], rows[2]['candidate_id'])
        self.assertEqual(selected['format_label'], 'Original')
        self.assertEqual(selected['size_bytes'], 9000000)
        for row in rows:
            binding = self.record(row)['service_legacy_item']
            self.assertEqual(binding['size_bytes'], 9000000)
            self.assertEqual(binding['provider_manifest_token'], original.video_assets[0].provider_manifest_token)

    def test_projection_does_not_resolve_dns_but_actions_recheck_public_addresses(self):
        from .services.common import XhsSecurityError
        with patch('xhs_download.services.upstream_candidates.validate_public_download_url',
                   side_effect=AssertionError('projection must not resolve DNS')), \
             patch('xhs_download.services.upstream_delivery.validate_public_download_url',
                   side_effect=AssertionError('projection must not resolve cover DNS')):
            rows = self.result().metadata['upstream_candidates']
            result = self.bridge_result('tiantianjiexi', (
                MediaCandidate(url=BASE + 'cached.mp4', quality_label='Original', size_bytes=1000),
            ))
            restored = deserialize_douyin_continuation_result(serialize_douyin_continuation_result(result))
            self.assertEqual(len(restored.metadata['upstream_candidates']), 1)
        record = self.record(rows[1])
        with patch('xhs_download.services.upstream_candidates.validate_public_download_url',
                   side_effect=XhsSecurityError('private address')) as validate:
            for action in (selected_link_descriptor, selected_descriptor, selected_legacy_descriptor):
                with self.assertRaises(ValueError):
                    action(record)
            self.assertEqual(validate.call_count, 3)
            with self.assertRaises(XhsParseError):
                self.record(rows[1])
        with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=SimpleNamespace()), \
             patch('xhs_download.services.provider_access.provider_is_allowed_for_request', return_value=True), \
             patch('xhs_download.services.provider_settings.get_platform_provider_settings', return_value=self.settings), \
             patch('xhs_download.services.upstream_delivery.validate_public_download_url',
                   side_effect=XhsSecurityError('private cover address')) as validate:
            response = self.client.post(reverse('service_selected_format'), {
                'candidate_id': rows[0]['candidate_id'], 'resource_token': rows[0]['resource_token'],
                'action': 'cover',
            }, content_type='application/json')
            self.assertEqual(response.status_code, 409)
            validate.assert_called_once()
        self.session.post.assert_called_once()

    def test_missing_or_ambiguous_manifest_disables_dedicated_download_but_not_copy(self):
        winner = MediaCandidate(url=BASE + 'main.mp4', quality_label='Original', size_bytes=9000000)
        alias = replace(winner, quality_label='2160P')
        for provider in ('tiantianjiexi', 'kukutool'):
            if provider == 'kukutool':
                winner = replace(winner, url='https://v26-default.365yg.com/s/7fffffff/video/tos/main/')
                alias = replace(alias, url=winner.url)
            for missing in (True, False):
                with self.subTest(provider=provider, missing_manifest=missing):
                    payload = None if missing else {'_service_format_rows': [
                        {'url': winner.url, 'quality_label': winner.quality_label, 'work_id': 'one'},
                        {'url': alias.url, 'quality_label': alias.quality_label, 'work_id': 'two'},
                    ]}
                    result = self.bridge_result(provider, (winner, alias), payload=payload, missing_manifest=missing)
                    for row in result.metadata['upstream_candidates']:
                        self.assertFalse(capability(row)['can_download'])
                        self.assertFalse(capability(row)['can_preview'])
                        self.assertEqual(row['legacy_asset_id'], '')
                        record = self.record(row)
                        self.assertEqual(selected_link_descriptor(record)['url'], winner.url)
                        with self.assertRaises(ValueError):
                            selected_descriptor(record)
                        with self.assertRaises(ValueError):
                            selected_legacy_descriptor(record)
                        self.assertTrue(capability({**row, 'provider_id': 'kukutool_generic'})['can_download'])

    def test_optional_continuation_cache_failure_keeps_valid_primary(self):
        result = self.result()
        payload = serialize_douyin_continuation_result(result)
        for error in (ConnectionError('cache unavailable'), RuntimeError('backend error')):
            with self.subTest(error=type(error).__name__), \
                 patch('xhs_download.services.upstream_candidates.resolve_candidate_record', side_effect=error) as resolve, \
                 patch('xhs_download.services.upstream_candidates.cache.set') as write:
                restored = deserialize_douyin_continuation_result(payload)
                self.assertEqual(restored.video_assets[0].url, result.video_assets[0].url)
                self.assertEqual(restored.video_assets[0].provider_manifest_token,
                                 result.video_assets[0].provider_manifest_token)
                self.assertNotIn('upstream_candidates', restored.metadata)
                resolve.assert_called_once()
                write.assert_not_called()
        self.session.post.assert_called_once()

    def test_continuation_drops_expired_and_cross_work_refs_without_renewal(self):
        result = self.result()
        payload = serialize_douyin_continuation_result(result)
        row = result.metadata['upstream_candidates'][0]
        foreign = {**self.record(row), 'dedicated_resource_id': '7669858827638845161'}
        with patch('xhs_download.services.upstream_candidates.resolve_candidate_record',
                   side_effect=[XhsParseError('expired'), foreign, XhsParseError('expired')]), \
             patch('xhs_download.services.upstream_candidates.cache.set') as write:
            restored = deserialize_douyin_continuation_result(payload)
        self.assertEqual(restored.video_assets[0].url, result.video_assets[0].url)
        self.assertNotIn('upstream_candidates', restored.metadata)
        write.assert_not_called()
        self.session.post.assert_called_once()

    def test_extra_format_legacy_transport_accepts_only_exact_signed_url_without_recovery(self):
        for provider, identity in (('tiantianjiexi', get_tiantianjiexi_manifest_identity),
                                   ('kukutool', get_kukutool_manifest_identity)):
            with self.subTest(provider=provider):
                prefix = BASE if provider == 'tiantianjiexi' else 'https://v26-default.365yg.com/s/7fffffff/video/tos/'
                main = MediaCandidate(url=prefix + 'main/', quality_label='Original', size_bytes=9000000)
                extra = replace(main, url=prefix + 'extra/', quality_label='720P')
                result = self.bridge_result(provider, (main, extra))
                rows = result.metadata['upstream_candidates']
                primary_item = selected_legacy_descriptor(self.record(rows[0]))
                item = selected_legacy_descriptor(self.record(rows[1]))
                self.assertIsNotNone(identity(primary_item))
                self.assertIsNone(identity(item))
                self.assertEqual(validate_provider_signed_download_url(item, item['url'], 'invalid'), item['url'])
                for invalid in (prefix + 'other/', 'https://example.com/video.mp4'):
                    with self.assertRaises(XhsDownloadError):
                        validate_provider_signed_download_url(item, invalid, 'invalid')
                response = Mock(status_code=200, url=item['url'])
                with patch('xhs_download.services.downloads.requests.get', return_value=response) as get:
                    self.assertIs(request_public_provider_get_with_retry(
                        item['url'], provider_item=item, retries=1,
                    ), response)
                    get.assert_called_once()
                redirect = Mock(status_code=302, headers={'Location': prefix + 'other/'})
                with patch('xhs_download.services.downloads.requests.get', return_value=redirect) as get:
                    with self.assertRaises(XhsDownloadError):
                        request_public_provider_get_with_retry(item['url'], provider_item=item, retries=1)
                    get.assert_called_once()

    def test_known_cdn_expiry_rejects_extra_download_without_renewing_or_breaking_copy(self):
        now = int(time())
        for provider, identity in (('tiantianjiexi', get_tiantianjiexi_manifest_identity),
                                   ('kukutool', get_kukutool_manifest_identity)):
            with self.subTest(provider=provider), patch('xhs_download.services.provider_manifest.time', return_value=now):
                main = MediaCandidate(url='https://v26-default.365yg.com/s/7fffffff/video/tos/main/',
                                      quality_label='Original', size_bytes=9000000)
                extra = replace(main, url=f'https://v26-default.365yg.com/s/{now + 60:08x}/video/tos/extra/',
                                quality_label='720P')
                result = self.bridge_result(provider, (main, extra))
                rows = result.metadata['upstream_candidates']
                primary_item = selected_legacy_descriptor(self.record(rows[0]))
                record = self.record(rows[1])
                item = selected_descriptor(record)
                self.assertTrue(provider_manifest_allows_public_download(item))
                for elapsed in (30, 61):
                    with self.subTest(elapsed=elapsed), \
                         patch('xhs_download.services.provider_manifest.time', return_value=now + elapsed), \
                         patch('xhs_download.services.upstream_candidates.cache.set') as write, \
                         patch('xhs_download.services.downloads.requests.get') as get:
                        current = self.record(rows[1])
                        self.assertFalse(provider_manifest_allows_public_download(item))
                        with self.assertRaises(ValueError):
                            selected_descriptor(current)
                        with self.assertRaises(ValueError):
                            selected_legacy_descriptor(current)
                        with self.assertRaises(XhsDownloadError):
                            request_public_provider_get_with_retry(item['url'], provider_item=item, retries=1)
                        self.assertEqual(selected_link_descriptor(current)['url'], extra.url)
                        self.assertIsNotNone(identity(primary_item))
                        self.assertTrue(provider_manifest_allows_public_download(primary_item))
                        write.assert_not_called()
                        get.assert_not_called()

    def test_expired_manifest_does_not_remove_copy_capability(self):
        result = self.result()
        row = result.metadata['upstream_candidates'][1]
        with override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=1), \
             patch('time.time', return_value=time() + 2):
            record = self.record(row)
            with self.assertRaises(ValueError):
                selected_descriptor(record)
            self.assertEqual(selected_link_descriptor(record)['url'], BASE + 'low.mp4')

    def test_real_api_normalization_dedicated_provider_and_both_templates(self):
        result = self.result()
        rows = result.metadata['upstream_candidates']
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(result.video_assets), 1)
        self.assertEqual(result.video_assets[0].url, BASE + 'high.mp4')
        self.assertEqual({row['provider_id'] for row in rows}, {'tiantianjiexi'})
        self.assertEqual(len({resolve_candidate_record(r['resource_token'], r['candidate_id'])['url'] for r in rows}), 2)
        self.session.post.assert_called_once()
        context = {**build_result_display_context(result, probe_missing_video_sizes=False), 'status': 'success'}
        for template, data in (('partials/xhs_results_region.html', context),
                               ('partials/batch_result_card.html', {'row': context})):
            soup = BeautifulSoup(render_to_string(template, data), 'html.parser')
            self.assertEqual(len(soup.select('[data-picker-radio]')), 3)
            self.assertFalse(soup.select('.asset-card[data-type="video"]'))
        self.assertEqual(build_service_picker(rows)['selected']['candidate_id'], rows[0]['candidate_id'])

    def test_nonprimary_manifest_exact_identity_and_cache_roundtrip(self):
        result = self.result()
        from .services.douyin_continuation import serialize_douyin_continuation_result, deserialize_douyin_continuation_result
        encoded = serialize_douyin_continuation_result(result)
        cache.set('test-result', json.loads(json.dumps(encoded)), 60)
        restored = deserialize_douyin_continuation_result(cache.get('test-result'))
        rows = restored.metadata['upstream_candidates']
        self.assertEqual(len(rows), 3)
        old = {k: v for k, v in encoded.items() if k != 'format_refs'}
        self.assertNotIn('upstream_candidates', deserialize_douyin_continuation_result(old).metadata)
        items = [selected_descriptor(resolve_candidate_record(r['resource_token'], r['candidate_id'])) for r in rows]
        self.assertEqual(items[1]['url'], BASE + 'low.mp4')
        self.assertEqual(items[1]['provider_resource_id'], AWEME)
        self.assertNotEqual(items[0]['asset_id'], items[1]['asset_id'])
        self.assertEqual(items[1]['asset_id'], items[2]['asset_id'])
        self.assertNotEqual(items[0]['filename'], items[1]['filename'])
        for item in items:
            self.assertTrue(provider_manifest_allows_public_download(item))
        self.assertFalse(provider_manifest_allows_public_download({**items[1], 'provider_manifest_token': items[0]['provider_manifest_token']}))
        self.assertFalse(provider_manifest_allows_public_download({**items[1], 'url': BASE + 'other.mp4'}))
        second = self.result('7669858827638845161').metadata['upstream_candidates'][1]
        other = selected_descriptor(resolve_candidate_record(second['resource_token'], second['candidate_id']))
        self.assertNotEqual(other['asset_id'], items[1]['asset_id'])
        self.assertFalse(provider_manifest_allows_public_download({**items[1], 'provider_manifest_token': other['provider_manifest_token']}))

    def test_current_admission_expiry_cross_token_and_no_reparse(self):
        rows = self.result().metadata['upstream_candidates']
        c = rows[1]
        body = {k: c[k] for k in ('candidate_id', 'resource_token')}
        with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=SimpleNamespace()), \
             patch('xhs_download.services.provider_access.provider_is_allowed_for_request', return_value=True) as admit, \
             patch('xhs_download.services.provider_settings.get_platform_provider_settings', return_value=self.settings), \
             patch('xhs_download.views.get_download_runtime_setting', return_value=SimpleNamespace()), \
             patch('xhs_download.views.check_download_cost_preflight', return_value=None), \
             patch('xhs_download.views.check_download_request_limits', return_value=('test', None)):
            for action in ('link', 'download', 'legacy'):
                response = self.client.post(reverse('service_selected_format'), {**body, 'action': action}, content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['item']['url'], BASE + 'low.mp4')
            admit.return_value = False
            self.assertEqual(self.client.post(reverse('service_selected_format'), {**body, 'action': 'download'}, content_type='application/json').status_code, 403)
            admit.return_value = True
            self.assertEqual(self.client.post(reverse('service_selected_format'), {**body, 'resource_token': rows[0]['resource_token'], 'action': 'link'}, content_type='application/json').status_code, 403)
            cache.clear()
            self.assertEqual(self.client.post(reverse('service_selected_format'), {**body, 'action': 'link'}, content_type='application/json').status_code, 403)
        self.session.post.assert_called_once()
