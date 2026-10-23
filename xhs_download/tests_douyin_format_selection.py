"""Dedicated Douyin service -> real result consumers, no remote requests."""
import json
from io import BytesIO
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from django.core import signing
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from .services.platforms.douyin import DouyinAdapter
from .services.candidates import MediaCandidate
from .services.common import XhsDownloadError, XhsParseError
from .services.douyin_continuation import serialize_douyin_continuation_result, deserialize_douyin_continuation_result
from .services.platforms.douyin_providers import (
    DouyinParseContext, DouyinTianTianJieXiProvider,
    attach_douyin_service_picker, build_external_parse_result, douyin_caption_presentation,
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
CAPTION_TITLE = '简单几分钟调出高光比通透感'
CAPTION_DESCRIPTION = (
    '丝毫不畏惧大晴天和阴天, 找到光就是拍, 1 原图构图和打光, '
    '2 一级基础调色, 3 分离注入照片灵魂, 4 二级最终调色 5 AI辅助画面氛围'
)
CAPTION = f'{CAPTION_TITLE} {CAPTION_DESCRIPTION} #明日方舟 #摄影调色 #糕速出片指南'


class DouyinCaptionPresentationTests(SimpleTestCase):
    def test_combined_caption_keeps_description_topics_and_raw_text(self):
        text = douyin_caption_presentation(CAPTION)
        self.assertEqual(text['title'], CAPTION_TITLE)
        self.assertEqual(text['description'], CAPTION_DESCRIPTION)
        self.assertEqual(text['topics'], ['明日方舟', '摄影调色', '糕速出片指南'])
        self.assertEqual(text['caption'], CAPTION)
        self.assertTrue(text['title_derived'])

    def test_independent_title_and_description_do_not_merge(self):
        text = douyin_caption_presentation('独立的完整 标题', '另外的描述 #话题')
        self.assertEqual(text['title'], '独立的完整 标题')
        self.assertEqual(text['description'], '另外的描述')
        self.assertEqual(text['topics'], ['话题'])
        self.assertFalse(text['title_derived'])

    def test_ordinary_titles_are_not_split_on_word_spaces(self):
        for title in (
            '中国国家地理 西藏特刊',
            'How to make a beautiful photograph in bright sunlight with a simple camera',
            'iPhone 17 Pro 拍摄体验',
            'C#入门教程',
        ):
            with self.subTest(title=title):
                text = douyin_caption_presentation(title + ' #摄影')
                self.assertEqual(text['title'], title)
                self.assertEqual(text['description'], '')
        self.assertEqual(douyin_caption_presentation('原本的标题')['title'], '原本的标题')

    def test_newline_description_and_duplicate_topics(self):
        text = douyin_caption_presentation('标题\n第一段描述\n第二段描述 #摄影 #摄影')
        self.assertEqual(text['title'], '标题')
        self.assertEqual(text['description'], '第一段描述\n第二段描述')
        self.assertEqual(text['topics'], ['摄影'])

    def test_description_only_and_missing_titles(self):
        self.assertEqual(douyin_caption_presentation('', CAPTION)['title'], CAPTION_TITLE)
        for value in ('', 'https://media.example/video.mp4', '//media.example/video.mp4'):
            self.assertEqual(douyin_caption_presentation(value)['title'], '')
        self.assertEqual(douyin_caption_presentation('#只有话题')['topics'], ['只有话题'])


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class DouyinFormatSelectionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.enterContext(patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('Unexpected network request')))
        self.media_requests = []
        self.enterContext(patch('requests.adapters.HTTPAdapter.send', side_effect=self.media_metadata_http))
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

    def media_metadata_http(self, request, **kwargs):
        self.assertIn(urlsplit(request.url).hostname, {'v3-dy-o.zjcdn.com', 'v26-default.365yg.com'})
        self.assertIn(request.method, {'HEAD', 'GET'})
        if request.method == 'GET':
            self.assertEqual(request.headers.get('Range'), 'bytes=0-0')
        self.media_requests.append((request.method, request.url))
        response = requests.Response()
        response.status_code = 200 if request.method == 'HEAD' else 206
        response.url = request.url
        response.request = request
        response.headers.update({'Content-Type': 'video/mp4', 'Content-Disposition': 'inline',
                                 'Content-Length': '2000' if request.method == 'HEAD' else '1'})
        if request.method == 'GET':
            response.headers['Content-Range'] = 'bytes 0-0/2000'
        response.raw = BytesIO(b'')
        return response

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

    def test_topics_only_caption_does_not_become_download_filename(self):
        self.session.post.return_value.json.return_value['data']['title'] = '#摄影 #日常'
        result = self.result()
        row = result.metadata['upstream_candidates'][0]
        link = selected_link_descriptor(self.record(row))
        self.assertTrue(link['filename'].startswith('media_'))
        self.assertNotIn('#', link['filename'])
        record = deepcopy(self.record(row))
        record['service_legacy_item']['filename'] = '#摄影 #日常.mp4'
        self.assertEqual(selected_link_descriptor(record)['filename'], 'media.mp4')
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        self.assertEqual(context['base_filename'], 'douyin')
        self.assertEqual(context['display_topics'], ['摄影', '日常'])

    def test_caption_naming_preserves_cached_candidates_and_selected_authority(self):
        self.session.post.return_value.json.return_value['data']['title'] = CAPTION
        result = self.result()
        original_metadata = deepcopy(result.metadata)
        rows = result.metadata['upstream_candidates']
        self.assertIn(CAPTION_TITLE, result.metadata['title'])
        for row in rows[:2]:
            with self.subTest(candidate=row['candidate_id']):
                record = self.record(row)
                saved_record = deepcopy(record)
                link = selected_link_descriptor(record)
                self.assertTrue(link['filename'].startswith(CAPTION_TITLE + '_'))
                self.assertNotIn(CAPTION_DESCRIPTION[:10], link['filename'])
                self.assertNotIn('#', link['filename'])
                response = self.download_action(row)
                self.assertEqual(response.status_code, 200, response.content)
                item = response.json()['item']
                self.assertEqual(item['filename'], link['filename'])
                self.assertEqual(item['candidate_id'], row['candidate_id'])
                self.assertEqual(item['url'], record['url'])
                self.assertEqual(item['provider_manifest_token'],
                                 record['service_legacy_item']['provider_manifest_token'])
                self.assertEqual(item['selection_expires_at'], int(record['expires_at'] * 1000))
                self.assertEqual(self.record(row), saved_record)
        self.assertEqual(result.metadata, original_metadata)
        self.session.post.assert_called_once()
        self.assertEqual(self.media_requests, [('HEAD', self.record(row)['url']) for row in rows[:2]])

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

    def download_action(self, candidate, **overrides):
        with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=SimpleNamespace()), \
             patch('xhs_download.services.provider_access.provider_is_allowed_for_request', return_value=True), \
             patch('xhs_download.services.provider_settings.get_platform_provider_settings', return_value=self.settings), \
             patch('xhs_download.views.get_download_runtime_setting', return_value=SimpleNamespace()), \
             patch('xhs_download.views.check_download_cost_preflight', return_value=None), \
             patch('xhs_download.views.check_download_request_limits', return_value=('test', None)):
            return self.client.post(reverse('service_selected_format'), {
                'candidate_id': candidate['candidate_id'], 'resource_token': candidate['resource_token'],
                'action': 'download', **overrides,
            }, content_type='application/json')

    def update_cached_record(self, candidate, **changes):
        from .services.upstream_candidates import SALT, TTL
        payload = signing.loads(candidate['resource_token'], salt=SALT)
        key = 'upstream:candidate-bundle:' + payload['bundle']
        records = cache.get(key)
        records[candidate['candidate_id']].update(changes)
        cache.set(key, records, TTL)

    @override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=120)
    def test_download_expiry_uses_original_manifest_timestamp_without_resigning(self):
        issued_at = int(time()) - 80
        for provider in ('tiantianjiexi', 'kukutool'):
            with self.subTest(provider=provider):
                prefix = 'https://v26-default.365yg.com/s/7fffffff/video/tos/'
                main = MediaCandidate(url=prefix + 'main/', quality_label='1080P', size_bytes=3000)
                extra = replace(main, url=prefix + 'extra/', quality_label='720P', size_bytes=2000)
                with patch('time.time', return_value=issued_at):
                    rows = self.bridge_result(provider, (main, extra)).metadata['upstream_candidates']
                record = self.record(rows[1])
                original = record['service_legacy_item']
                for elapsed in (80, 100):
                    with self.subTest(elapsed=elapsed), patch('time.time', return_value=issued_at + elapsed), \
                         patch('django.core.signing.TimestampSigner.sign', side_effect=AssertionError('No renewal')):
                        response = self.download_action(rows[1])
                    self.assertEqual(response.status_code, 200, response.content)
                    item = response.json()['item']
                    self.assertEqual(item['download_strategy'], 'browser_streaming')
                    self.assertEqual(item['selection_expires_at'], (issued_at + 120) * 1000)
                    self.assertEqual(item['provider_manifest_token'], original['provider_manifest_token'])
                    self.assertEqual(item['url'], extra.url)
                    self.assertEqual(item['asset_id'], original['asset_id'])
                    self.assertNotEqual(item['asset_id'], self.record(rows[0])['service_legacy_item']['asset_id'])

    def test_download_expiry_is_bounded_by_candidate_record(self):
        rows = self.result().metadata['upstream_candidates']
        record = self.record(rows[1])
        response = self.download_action(rows[1])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['selection_expires_at'], int(record['expires_at'] * 1000))
        self.assertEqual(response.json()['item']['provider_manifest_token'],
                         record['service_legacy_item']['provider_manifest_token'])

    def test_extensionless_selected_video_keeps_original_mp4_naming(self):
        for provider in ('tiantianjiexi', 'kukutool'):
            with self.subTest(provider=provider):
                prefix = 'https://v26-default.365yg.com/s/7fffffff/video/tos/'
                main = MediaCandidate(url=prefix + 'main/', quality_label='1080P', size_bytes=3000)
                extra = replace(main, url=prefix + 'selected/', quality_label='540P', size_bytes=2000)
                rows = self.bridge_result(provider, (main, extra)).metadata['upstream_candidates']
                names = []
                for row, candidate in zip(rows, (main, extra)):
                    record = self.record(row)
                    response = self.download_action(row)
                    self.assertEqual(response.status_code, 200, response.content)
                    item = response.json()['item']
                    self.assertTrue(item['filename'].endswith('.mp4'), item['filename'])
                    self.assertEqual(item['filename'], selected_link_descriptor(record)['filename'])
                    self.assertEqual(item['filename'], selected_legacy_descriptor(record)['filename'])
                    self.assertEqual(item['url'], candidate.url)
                    self.assertEqual(item['provider_manifest_token'],
                                     record['service_legacy_item']['provider_manifest_token'])
                    self.assertEqual(item['size_bytes'], candidate.size_bytes)
                    self.assertFalse(record['public'].get('extension'), 'Naming is not container evidence')
                    names.append(item['filename'])
                self.assertNotEqual(*names)

    def test_explicit_video_extension_is_not_replaced_by_mp4_default(self):
        for provider in ('tiantianjiexi', 'kukutool'):
            for extension in ('mp4', 'webm', 'mov'):
                with self.subTest(provider=provider, extension=extension):
                    row = self.bridge_result(provider, (
                        MediaCandidate(url='https://v26-default.365yg.com/s/7fffffff/video/tos/video.' + extension,
                                       quality_label='720P', size_bytes=2000),
                    )).metadata['upstream_candidates'][0]
                    response = self.download_action(row)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertTrue(response.json()['item']['filename'].endswith('.' + extension))

    def test_selected_video_preserves_existing_asset_filename(self):
        context = DouyinParseContext(url=f'https://www.douyin.com/video/{AWEME}', aweme_id=AWEME)
        candidate = MediaCandidate(url=BASE + 'video/', quality_label='720P', size_bytes=2000)
        main = build_external_parse_result(DouyinAdapter(), context, candidate, provider_name='tiantianjiexi')
        main = replace(main, assets=(replace(main.video_assets[0], filename='Original title.mp4'),))
        extra = replace(candidate, url=BASE + 'another/', quality_label='540P')
        result = attach_douyin_service_picker(main, adapter=DouyinAdapter(), context=context, candidates=(candidate, extra))
        row = result.metadata['upstream_candidates'][0]
        response = self.download_action(row)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['filename'], 'Original title.mp4')
        self.assertEqual(selected_link_descriptor(self.record(row))['filename'], 'Original title.mp4')
        other = self.download_action(result.metadata['upstream_candidates'][1])
        self.assertEqual(other.status_code, 200, other.content)
        self.assertTrue(other.json()['item']['filename'].endswith('.mp4'))
        self.assertNotEqual(other.json()['item']['filename'], 'Original title.mp4')
        self.assertEqual(other.json()['item']['url'], extra.url)

    def test_download_expiry_is_bounded_by_known_cdn_expiry(self):
        expires_at = int(time()) + 90
        for provider in ('tiantianjiexi', 'kukutool'):
            with self.subTest(provider=provider):
                main = MediaCandidate(url='https://v26-default.365yg.com/s/7fffffff/video/tos/main/',
                                      quality_label='1080P', size_bytes=3000)
                extra = replace(main, url=f'https://v26-default.365yg.com/s/{expires_at:08x}/video/tos/extra/',
                                quality_label='720P')
                row = self.bridge_result(provider, (main, extra)).metadata['upstream_candidates'][1]
                response = self.download_action(row)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['item']['selection_expires_at'], expires_at * 1000)
                with patch('xhs_download.services.service_format_selection.time', return_value=expires_at):
                    response = self.download_action(row)
                self.assertEqual(response.status_code, 409)

    def test_download_action_rejects_expired_record_and_manifest_without_renewal(self):
        row = self.result().metadata['upstream_candidates'][1]
        record = self.record(row)
        with patch('django.core.signing.TimestampSigner.sign', side_effect=AssertionError('No renewal')):
            with override_settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=1), \
                 patch('time.time', return_value=time() + 2):
                self.assertEqual(self.download_action(row).status_code, 409)
            self.update_cached_record(row, expires_at=time() - 1)
            self.assertEqual(self.download_action(row).status_code, 409)
        self.assertEqual(self.record(row)['service_legacy_item']['provider_manifest_token'],
                         record['service_legacy_item']['provider_manifest_token'])
        self.assertEqual(self.media_requests, [])

    def test_download_action_rejects_cross_candidate_work_and_manifest(self):
        rows = self.result().metadata['upstream_candidates']
        row = rows[1]
        foreign = self.result('7669858827638845161').metadata['upstream_candidates'][1]
        record = self.record(row)
        original = record['service_legacy_item']
        self.assertEqual(self.download_action(row, resource_token=rows[0]['resource_token']).status_code, 403)
        self.assertEqual(self.download_action(row, resource_token=foreign['resource_token']).status_code, 403)
        for other in (rows[0], foreign):
            with self.subTest(candidate_id=other['candidate_id']):
                token = self.record(other)['service_legacy_item']['provider_manifest_token']
                self.update_cached_record(row, service_legacy_item={**original, 'provider_manifest_token': token})
                self.assertEqual(self.download_action(row).status_code, 409)
        self.assertEqual(self.media_requests, [])

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
        self.assertEqual(context['upstream_candidates'], rows)
        for staff in (False, True):
            for template, data in (('partials/xhs_results_region.html', context),
                                   ('partials/batch_result_card.html', {'row': context})):
                with self.subTest(staff=staff, template=template):
                    html = render_to_string(template, {**data, 'show_internal_diagnostics': staff})
                    soup = BeautifulSoup(html, 'html.parser')
                    self.assertEqual(len(soup.select('[data-picker-radio]')), 3)
                    self.assertEqual(len(soup.select('[data-upstream-candidate]')), 3 if staff else 0)
                    self.assertEqual(bool(soup.select_one('.ytdlp-technical')), staff)
                    self.assertEqual(bool(soup.select_one('[data-upstream-candidates]')), staff)
                    self.assertFalse(soup.select('.asset-card[data-type="video"]'))
        self.assertEqual(len(result.metadata['upstream_candidates']), 3)
        self.assertEqual(context['upstream_candidates'], rows)
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

    @override_settings(ULTRA_HD_PROVIDER_ENABLED=True, KUKUTOOL_PROVIDER_ENABLED=False,
                       THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
                       TIANTIANJIEXI_PHONE='fixture', TIANTIANJIEXI_PASSWORD='fixture')
    def test_kukutool_switch_refuses_only_kukutool_service_candidates(self):
        from .ytdlp_selection_views import _admitted
        runtime = SimpleNamespace(douyin_ultra_mode='public', douyin_ultra_rollout_percent=100,
                                  douyin_ultra_disabled_reason='')
        with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=runtime):
            for provider, admitted in (('tiantianjiexi', True), ('kukutool', False)):
                record = {'source_url': f'https://www.douyin.com/video/{AWEME}', 'provider_id': provider,
                          'dedicated_resource_id': AWEME,
                          'match': {'provider_id': provider, 'extractor_id': 'douyin:' + provider}}
                with self.subTest(provider=provider):
                    self.assertIs(_admitted(RequestFactory().post('/'), record, provider=provider, service=True),
                                  admitted)
