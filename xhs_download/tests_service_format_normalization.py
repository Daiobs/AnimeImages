"""Native service shapes through normalizers and the shared execution boundary."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import pickle
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.platforms.kukutool_generic import (
    KukutoolResultNormalizer, ServiceFormatPayload,
)
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiResultNormalizer, unwrap_api_payload,
)
from .services.quality_execution import execute_provider
from .services.upstream_candidates import resolve_candidate_record


FIXTURES = Path(__file__).parent / 'fixtures' / 'native_replay'
SOURCE = 'https://source.example/work/1'
URL = 'https://media.example/video.mp4'
NORMALIZERS = {
    'tiantianjiexi_generic': TianTianJieXiResultNormalizer,
    'kukutool_generic': KukutoolResultNormalizer,
}


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                     'LOCATION': 'service-format-normalization'}})
class ServiceFormatNormalizationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('xhs_download.services.quality_execution._python_for', return_value=None))
        self.rule = SimpleNamespace(platform_id='fixture', media_host_suffixes=('media.example',))

    def fixture(self, provider):
        uid = ('40b6b3302653089b0ed07351' if provider == 'tiantianjiexi_generic'
               else '1d723e3f60f5f7b7634cd84a')
        data = json.loads((FIXTURES / (uid + '.json')).read_text())['records'][0]
        return unwrap_api_payload(data) if provider == 'tiantianjiexi_generic' else data

    def normalize(self, provider, native):
        cls = NORMALIZERS[provider]
        payload = cls.normalize_payload(native)
        return cls().normalize(rule=self.rule, platform_name='Fixture', source_url=SOURCE,
                               payload=payload)

    def execute(self, provider, native):
        cls = ('TianTianJieXiGenericProvider' if provider == 'tiantianjiexi_generic'
               else 'KukutoolGenericProvider')
        # The transport boundary is replaced; normalization, candidate issuance and
        # the shared executor are the production implementations.
        with patch(f'xhs_download.services.platforms.{provider}.{cls}.parse', return_value=native):
            return execute_provider(provider, SOURCE, service_authorized=True)

    def test_native_fixtures_preserve_seven_labels_and_exact_sizes(self):
        for provider, cls in NORMALIZERS.items():
            with self.subTest(provider=provider):
                native = self.fixture(provider)
                original = deepcopy(native)
                payload = cls.normalize_payload(native)
                rows = [r for r in payload['_service_format_rows'] if r['media_type'] == 'video']
                self.assertEqual(len(rows), 7)
                self.assertEqual({r['quality_label'] for r in rows},
                                 {'360P', '480P', '540P', '720P', '1080P', '1080P+', '2160P'})
                self.assertEqual({r['work_id'] for r in rows}, {'native-video-1'})
                self.assertEqual(next(r['size_bytes'] for r in rows if r['quality_label'] == '2160P'), 4428698300)
                self.assertTrue(all(r['quality_label_source'] == 'upstream' for r in rows))
                self.assertTrue(all('height' not in r and 'vcodec' not in r for r in rows))
                self.assertEqual(native, original)
                self.assertIs(cls.normalize_payload(payload), payload)
                restored = pickle.loads(pickle.dumps(payload))
                self.assertIsInstance(restored, ServiceFormatPayload)
                self.assertEqual(cls.normalize_payload(restored), payload)

    def test_both_production_entries_issue_all_formats_and_keep_old_assets(self):
        for provider in NORMALIZERS:
            native = self.fixture(provider)
            for entry in (self.normalize, self.execute):
                with self.subTest(provider=provider, entry=entry.__name__):
                    result = entry(provider, native)
                    rows = result.metadata['upstream_candidates']
                    self.assertEqual(len([r for r in rows if r['media_type'] == 'video']), 7)
                    self.assertTrue(any(r['media_type'] == 'image' for r in rows))
                    self.assertEqual({r['provider_id'] for r in rows}, {provider})
                    for row in rows:
                        record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
                        self.assertEqual(record['provider_id'], provider)
                    if entry == self.normalize:
                        self.assertEqual(len(result.video_assets), 1)
                        # Kukutool's image is explicitly its cover. TianTian's
                        # images entry has no cover identity and remains an image.
                        self.assertEqual(len(result.image_assets), 1 if provider == 'tiantianjiexi_generic' else 0)
                        self.assertTrue(result.assets[0].provider_manifest_token)
                    else:
                        self.assertEqual(result.assets, ())

    def test_same_url_labels_and_same_label_urls_survive(self):
        for provider, cls in NORMALIZERS.items():
            key = 'streams' if provider == 'tiantianjiexi_generic' else 'video_fullinfo'
            native = {'title': 'A work', 'url': URL, 'videos': [{'url': URL, key: [
                {'url': URL, 'quality_label': '1080P', 'size': 100},
                {'url': URL, 'quality_label': 'original', 'size': 100},
                {'url': URL + '?variant=2', 'quality_label': '1080P', 'size': 200},
            ]}]}
            rows = cls.normalize_payload(native)['_service_format_rows']
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({r['url'] for r in rows}), 2)
            self.assertEqual({r['work_label'] for r in rows}, {'A work'})

    def test_missing_parent_url_and_multiple_videos_keep_group_boundaries(self):
        for provider, cls in NORMALIZERS.items():
            key = 'streams' if provider == 'tiantianjiexi_generic' else 'video_fullinfo'
            native = {'title': 'Collection', 'videos': [
                {key: [{'url': URL, 'quality_label': '720P'}]},
                {key: [{'url': URL + '?part=2', 'quality_label': '1080P'}]},
            ]}
            rows = cls.normalize_payload(native)['_service_format_rows']
            self.assertEqual({r['work_id'] for r in rows}, {'native-video-1', 'native-video-2'})
            self.assertEqual({r['work_label'] for r in rows}, {'Collection (1)', 'Collection (2)'})

    def test_flat_videos_are_independent_not_quality_variants(self):
        for cls in NORMALIZERS.values():
            rows = cls.normalize_payload({'videos': [{'url': URL}, {'url': URL + '?v=2'}]})['_service_format_rows']
            self.assertEqual({r['work_id'] for r in rows}, {'native-video-1', 'native-video-2'})

    def test_root_variants_do_not_merge_with_first_video_container(self):
        for provider, cls in NORMALIZERS.items():
            key = 'streams' if provider == 'tiantianjiexi_generic' else 'video_fullinfo'
            rows = cls.normalize_payload({'videos': [{key: [{'url': URL, 'quality_label': '720P'}]}],
                                         key: [{'url': URL + '?other=1', 'quality_label': '1080P'}]})['_service_format_rows']
            self.assertEqual({r['work_id'] for r in rows}, {'native-video-1', 'native-video-root'})
            self.assertEqual(len(rows), 2)

    def test_mixed_body_images_are_assets_but_explicit_cover_is_not_gallery(self):
        from .views import build_result_display_context
        from django.template.loader import render_to_string
        from bs4 import BeautifulSoup
        cover = 'https://media.example/cover.jpg'
        body = ['https://media.example/body-b.jpg', 'https://media.example/body-a.jpg']
        for provider in NORMALIZERS:
            result = self.normalize(provider, {'title': 'Mixed work', 'cover': cover,
                                    'videos': [{'url': URL}], 'images': [cover, *body]})
            self.assertEqual([asset.url for asset in result.image_assets], body)
            self.assertTrue(all(asset.provider_manifest_token for asset in result.image_assets))
            self.assertEqual(len(result.video_assets), 1)
            candidates = result.metadata['upstream_candidates']
            self.assertEqual(sum(c['media_type'] == 'image' for c in candidates), 3)
            context = build_result_display_context(result)
            self.assertEqual(len(context['image_items']), 2)
            home = BeautifulSoup(render_to_string('partials/xhs_results_region.html', context), 'html.parser')
            self.assertIn(body[0], str(home))
            self.assertIn(body[1], str(home))
            batch = BeautifulSoup(render_to_string('partials/batch_result_card.html',
                {'row': {**context, 'status': 'success', 'post_type': 'video', 'index': 0}}), 'html.parser')
            self.assertEqual(len(batch.select('[data-batch-download-item][data-type="image"]')), 2)

    def test_restricted_mixed_image_stays_candidate_without_new_legacy_download_grant(self):
        for provider in NORMALIZERS:
            result = self.normalize(provider, {'videos': [{'url': URL}],
                'images': ['https://media.example/body.jpg'], 'requires_context': True})
            self.assertEqual(result.image_assets, ())
            images = [c for c in result.metadata['upstream_candidates'] if c['media_type'] == 'image']
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0]['download_context_required'])

    def test_explicit_facts_headers_and_estimated_size_are_not_invented_or_dropped(self):
        for cls in NORMALIZERS.values():
            native = {'videos': [{'url': URL, 'qualityLabel': '1080P+', 'size': '1.5 MB',
                                  'headers': {'Referer': 'https://source.example/'},
                                  'requires_merge': True, 'vcodec': 'none', 'language': 'ja'}]}
            row = cls.normalize_payload(native)['_service_format_rows'][0]
            self.assertEqual(row['size_bytes'], 1572864)
            self.assertTrue(row['size_is_estimate'])
            self.assertEqual(row['headers'], native['videos'][0]['headers'])
            self.assertTrue(row['requires_merge'])
            self.assertEqual(row['vcodec'], 'none')
            self.assertEqual(row['language'], 'ja')
            self.assertNotIn('height', row)
            self.assertNotIn('acodec', row)

    def test_native_private_field_cannot_spoof_internal_rows(self):
        for cls in NORMALIZERS.values():
            rows = cls.normalize_payload({'videos': [{'url': URL}], '_service_format_rows': [
                {'url': 'https://other.example/forged.mp4', 'work_id': 'forged'}]})['_service_format_rows']
            self.assertEqual([r['url'] for r in rows], [URL])
            self.assertEqual(rows[0]['work_id'], 'native-video-1')

    def test_nested_variants_keep_parent_context_requirements(self):
        for provider, cls in NORMALIZERS.items():
            key = 'streams' if provider == 'tiantianjiexi_generic' else 'video_fullinfo'
            payload = cls.normalize_payload({'headers': {'Origin': 'https://source.example'}, 'videos': [
                {'requires_cookie': True, 'headers': {'Referer': SOURCE}, key: [
                    {'url': URL, 'headers': {'Accept': '*/*'}, 'requires_cookie': False}]}]})
            row = payload['_service_format_rows'][0]
            self.assertEqual(row['headers'], {'Origin': 'https://source.example', 'Referer': SOURCE, 'Accept': '*/*'})
            self.assertTrue(row['requires_cookie'])

    def test_mixed_header_aliases_and_parent_restrictions_reject_download(self):
        from .services.service_format_selection import capability, selected_descriptor
        restrictions = (
            {'headers': {'Referer': SOURCE}},
            {'headers': {'Cookie': 'synthetic=not-a-real-account'}},
            {'http_headers': {'Authorization': 'synthetic-only'}},
            {'requires_cookie': True}, {'requires_context': True},
            {'headers_present': True}, {'headers': 'unrecoverable-header-shape'},
            {'has_drm': True}, {'requires_merge': True},
        )
        for provider in NORMALIZERS:
            key = 'streams' if provider == 'tiantianjiexi_generic' else 'video_fullinfo'
            for restriction in restrictions:
                with self.subTest(provider=provider, restriction=tuple(restriction)):
                    native = {**restriction, 'videos': [{key: [
                        {'url': URL, 'http_headers': {'Accept': '*/*'},
                         'requires_context': False, 'requires_cookie': False,
                         'has_drm': False, 'requires_merge': False}]}]}
                    result = self.execute(provider, native)
                    candidate = result.metadata['upstream_candidates'][0]
                    self.assertFalse(capability(candidate)['can_download'])
                    record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
                    with self.assertRaises(ValueError):
                        selected_descriptor(record)

    def test_root_audio_retains_context_and_cannot_become_unrestricted(self):
        from .services.service_format_selection import capability
        for provider in NORMALIZERS:
            result = self.execute(provider, {
                'title': 'Audio work', 'audio_url': 'https://media.example/audio.m4a',
                'headers': {'Referer': SOURCE}, 'requires_context': True})
            candidate = result.metadata['upstream_candidates'][0]
            record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
            self.assertEqual(candidate['media_type'], 'audio')
            self.assertEqual(record['headers']['Referer'], SOURCE)
            self.assertFalse(capability(candidate)['can_download'])

    def test_ordinary_pure_audio_issues_candidates_without_legacy_assets(self):
        from .services.service_format_selection import build_service_picker, selected_descriptor
        url = 'https://media.example/audio.m4a'
        for provider in NORMALIZERS:
            with self.subTest(provider=provider):
                result = self.normalize(provider, {'title': 'Audio work', 'audio_url': url})
                self.assertEqual(result.post_type, 'audio')
                self.assertEqual(result.assets, ())
                candidates = result.metadata['upstream_candidates']
                self.assertEqual(len(candidates), 1)
                candidate = candidates[0]
                self.assertEqual(candidate['media_type'], 'audio')
                self.assertEqual(candidate['provider_id'], provider)
                self.assertTrue(build_service_picker(candidates)['is_audio'])
                record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
                descriptor = selected_descriptor(record)
                self.assertEqual(descriptor['url'], url)
                self.assertTrue(descriptor['service_audio_file'])
                self.assertFalse(descriptor.get('provider_manifest_token'))

    def test_ordinary_pure_audio_keeps_host_and_context_restrictions(self):
        from .services.service_format_selection import selected_descriptor
        for provider in NORMALIZERS:
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError):
                    self.normalize(provider, {'audio_url': 'https://other.example/audio.m4a'})
                result = self.normalize(provider, {'audio_url': 'https://media.example/audio.m4a',
                                                   'requires_context': True, 'headers': {'Referer': SOURCE}})
                self.assertEqual(result.assets, ())
                candidate = result.metadata['upstream_candidates'][0]
                record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
                self.assertEqual(record['headers']['Referer'], SOURCE)
                with self.assertRaises(ValueError):
                    selected_descriptor(record)

    def test_tiantian_business_envelope_does_not_drop_root_context(self):
        native = unwrap_api_payload({'code': 200, 'headers': {'Referer': SOURCE},
                                     'has_drm': True, 'data': {'title': 'Work', 'videos': [
                                         {'streams': [{'url': URL, 'http_headers': {'Accept': '*/*'}}]}]}})
        row = TianTianJieXiResultNormalizer.normalize_payload(native)['_service_format_rows'][0]
        self.assertEqual(row['http_headers'], {'Referer': SOURCE, 'Accept': '*/*'})
        self.assertTrue(row['has_drm'])

    def test_ordinary_candidate_issuance_keeps_existing_host_restrictions(self):
        for provider in NORMALIZERS:
            result = self.normalize(provider, {'videos': [
                {'url': URL}, {'url': 'https://other.example/not-granted.mp4'}]})
            self.assertEqual(len(result.metadata['upstream_candidates']), 1)
            self.assertEqual(result.assets[0].url, URL)

    def test_images_remain_ordered_and_do_not_become_video_variants(self):
        urls = ['https://media.example/b.jpg', 'https://media.example/a.jpg']
        for provider, cls in NORMALIZERS.items():
            native = {'images': urls, 'title': 'Gallery'}
            rows = cls.normalize_payload(native)['_service_format_rows']
            self.assertEqual([r['url'] for r in rows], urls)
            self.assertTrue(all(r['media_type'] == 'image' and not r.get('work_id') for r in rows))
            result = self.normalize(provider, native)
            self.assertEqual([a.url for a in result.image_assets], urls)
