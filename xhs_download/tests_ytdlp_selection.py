"""Result selector and fresh selected-format authorization, without Provider I/O."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from download_console.models import DownloadRuntimeSetting
from tools.upstream_match_worker import compact_descriptor
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.upstream_candidates import resolve_candidate_record
from .services.upstream_delivery import normalize_upstream_result
from .services.ytdlp_selection import build_picker, capability, context_required, selected_descriptor, selected_link_descriptor


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class YtdlpSelectionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for name, result in (
            ('xhs_download.services.upstream_delivery.validate_public_download_url', None),
            ('xhs_download.services.upstream_candidates.validate_public_download_url', None),
        ):
            guard = patch(name, side_effect=lambda url: url)
            guard.start(); self.addCleanup(guard.stop)
        no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network'))
        no_network.start(); self.addCleanup(no_network.stop)
        self.match = {'provider_id': 'yt_dlp_generic', 'extractor_id': 'Fixture', 'version': '2026.6.9', 'label': 'Fixture'}

    def result(self, formats=None, **changes):
        payload = {'id': 'work-one', 'title': 'A useful work', 'uploader': 'Author', 'duration': 60,
                   'thumbnail': 'https://cdn.example.org/cover.jpg',
                   'formats': formats or [self.video('1080', 1080), self.video('720', 720)]}
        payload.update(changes)
        return normalize_upstream_result(payload, match=self.match, source_url='https://site.example.org/watch/one')

    def video(self, identity, height=1080, **changes):
        return {'format_id': identity, 'url': f'https://cdn.example.org/{identity}.mp4', 'height': height,
                'width': height * 16 // 9 if height is not None else None, 'ext': 'mp4', 'protocol': 'https', 'vcodec': 'h264', 'acodec': 'aac',
                'http_headers': {'User-Agent': 'normal agent', 'Accept': '*/*'}, **changes}

    def candidate(self, **changes):
        return self.result([self.video('one', **changes)]).metadata['upstream_candidates'][0]

    def gates(self):
        for name, value in (
            ('xhs_download.services.provider_access.load_runtime_setting', SimpleNamespace()),
            ('xhs_download.services.platforms.third_party.provider_is_enabled', True),
            ('xhs_download.services.provider_access.upstream_match_is_blocked', False),
            ('xhs_download.services.provider_access.third_party_public_access_allowed', True),
            ('xhs_download.services.provider_access.upstream_match_is_public_allowed', True),
            ('xhs_download.views.get_download_runtime_setting', SimpleNamespace()),
            ('xhs_download.views.check_download_cost_preflight', None),
            ('xhs_download.views.check_download_request_limits', ('client', None)),
        ):
            active = patch(name, return_value=value)
            active.start(); self.addCleanup(active.stop)

    def post(self, candidate, action='download', **changes):
        data = {key: candidate[key] for key in ('candidate_id', 'resource_token')}
        data.update(action=action); data.update(changes)
        return self.client.post(reverse('ytdlp_selected_format'), data, content_type='application/json')

    def test_thumbnail_survives_compaction_without_300_character_url_truncation(self):
        url = 'https://cdn.example.org/cover.jpg?x=' + 'a' * 400
        self.assertEqual(compact_descriptor({'thumbnail': url})['thumbnail'], url)
        self.assertEqual(compact_descriptor({'thumbnails': [{'url': []}, {'url': url}]})['thumbnail'], url)
        self.assertNotIn('thumbnail', compact_descriptor({'thumbnails': [{'url': []}]}))

    def test_download_expiry_preserves_candidate_lifetime_and_rejects_late_action(self):
        self.gates()
        candidate = self.candidate()
        record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        expected = int(record['expires_at'] * 1000)
        for now in (record['expires_at'] - 100, record['expires_at'] - 1):
            with patch('xhs_download.services.ytdlp_selection.time', return_value=now):
                response = self.post(candidate)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()['item']['selection_expires_at'], expected)
            self.assertEqual(response.json()['item']['url'], record['url'])
        with patch('xhs_download.services.ytdlp_selection.time', return_value=record['expires_at']):
            self.assertEqual(self.post(candidate).status_code, 409)
            copied = self.post(candidate, 'link')
            self.assertEqual(copied.status_code, 200)
            self.assertNotIn('selection_expires_at', copied.json()['item'])

    def test_missing_expiry_restricts_download_not_copy(self):
        candidate = self.candidate()
        record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        record.pop('expires_at')
        self.assertEqual(selected_link_descriptor(record)['url'], record['url'])
        with self.assertRaises(ValueError):
            selected_descriptor(record)

    def test_single_work_information_is_not_a_public_signed_url_or_header(self):
        candidates = self.result().metadata['upstream_candidates']
        work = candidates[0]['work']
        self.assertEqual(work['title'], 'A useful work')
        self.assertEqual(work['author'], 'Author')
        self.assertEqual(work['duration'], 60)
        self.assertTrue(work['cover_available'])
        self.assertNotIn('https://', json.dumps(work))
        self.assertNotIn('work', candidates[1])
        record = resolve_candidate_record(candidates[0]['resource_token'], candidates[0]['candidate_id'])
        self.assertEqual(record['work']['cover_url'], 'https://cdn.example.org/cover.jpg')

    def test_highest_direct_video_selected_but_higher_split_variant_preserved(self):
        candidates = self.result([self.video('4k', 2160, acodec='none'), self.video('1080', 1080),
                                  self.video('720', 720)]).metadata['upstream_candidates']
        picker = build_picker(candidates)
        self.assertEqual(picker['selected']['format_id'], '1080')
        self.assertEqual(len(picker['videos']), 3)
        self.assertEqual(picker['videos'][0]['format_id'], '4k')
        self.assertFalse(picker['videos'][0]['can_download'])
        self.assertIn('合并', picker['videos'][0]['limitation'])

    def test_duplicate_quality_different_fps_codec_language_are_retained(self):
        candidates = self.result([self.video('a', fps=30), self.video('b', fps=60),
                                  self.video('c', vcodec='av1'), self.video('d', language='ja')]).metadata['upstream_candidates']
        self.assertEqual({r['format_id'] for r in build_picker(candidates)['videos']}, {'a', 'b', 'c', 'd'})

    def test_native_dynamic_range_survives_issuance_and_distinguishes_equal_quality(self):
        candidates = self.result([self.video('hdr', dynamic_range='HDR10'),
                                  self.video('sdr', dynamic_range='SDR')]).metadata['upstream_candidates']
        self.assertEqual([row['dynamic_range'] for row in candidates], ['HDR10', 'SDR'])
        videos = build_picker(candidates)['videos']
        self.assertEqual([row['label'] for row in videos], ['1080p', '1080p'])
        self.assertEqual([row['variant_label'] for row in videos], ['HDR10', 'SDR'])
        self.assertEqual([(row['candidate_id'], row['resource_token']) for row in videos],
                         [(row['candidate_id'], row['resource_token']) for row in candidates])

    def test_sizes_are_estimated_only_when_the_input_says_so(self):
        candidates = self.result([self.video('exact', filesize=1024), self.video('approx', filesize_approx=2048),
                                  self.video('unknown')]).metadata['upstream_candidates']
        rows = {r['format_id']: r for r in build_picker(candidates)['videos']}
        self.assertFalse(rows['exact']['size_label'].startswith('约'))
        self.assertTrue(rows['approx']['size_label'].startswith('约'))
        self.assertEqual(rows['unknown']['size_label'], '大小未知')

    def test_audio_only_is_the_primary_result_on_non_youtube_sources(self):
        candidate = self.candidate(ext='m4a', vcodec='none', acodec='aac', abr=64, height=None,
                                   url='https://cdn.example.org/voice.m4a')
        picker = build_picker([candidate])
        self.assertTrue(picker['is_audio'])
        self.assertEqual(picker['audios'][0]['label'], '64 kbps')
        self.assertTrue(picker['selected']['can_download'])
        html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker})
        self.assertNotIn('视频格式', html)
        self.assertIn('下载音频', html)
        self.assertNotIn('Hi-Res', html)

    def test_streams_drm_insecure_context_and_unknown_are_not_downloadable(self):
        for changes in (
            {'protocol': 'm3u8_native'}, {'protocol': 'http_dash_segments'}, {'has_drm': True},
            {'url': 'http://cdn.example.org/file.mp4', 'protocol': 'http'},
            {'http_headers': {'Referer': 'https://site.example.org/'}},
            {'http_headers': {'Authorization': 'Bearer secret'}}, {'acodec': None, 'vcodec': None},
        ):
            with self.subTest(changes=changes):
                candidate = self.candidate(**changes)
                self.assertFalse(capability(candidate)['can_download'])
                with self.assertRaises(ValueError):
                    selected_descriptor(resolve_candidate_record(candidate['resource_token'], candidate['candidate_id']))

    def test_non_sensitive_preferences_do_not_become_fabricated_cookie_requirements(self):
        self.assertFalse(context_required({'User-Agent': 'x', 'Accept': '*/*'}, headers_present=True))
        self.assertTrue(context_required({}, headers_present=True))
        self.assertTrue(context_required({'Origin': 'https://site.example.org'}))
        self.assertTrue(context_required({'User-Agent': 'x'}, requires_cookie=True))
        headers = {f'x-test-{i}': 'x' for i in range(33)}
        self.assertTrue(self.candidate(http_headers=headers)['download_context_required'])

    def test_default_fetch_mode_is_optional_but_other_context_stays_restricted(self):
        for name in ('Sec-Fetch-Mode', 'sec-fetch-mode', 'SEC-FETCH-MODE'):
            candidate = self.candidate(http_headers={'User-Agent': 'fixture', name: 'navigate'})
            self.assertTrue(capability(candidate)['can_download'])
            record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
            self.assertEqual(selected_descriptor(record)['candidate_id'], candidate['candidate_id'])
        for name in ('Cookie', 'Authorization', 'Referer', 'Origin', 'Sec-Unknown'):
            self.assertTrue(context_required({name: 'private-fixture'}))
        self.assertTrue(context_required({'Sec-Fetch-Mode': 'navigate'}, requires_cookie=True))
        self.assertTrue(context_required({}, headers_present=True))

    def test_webm_audio_descriptor_preserves_container_and_explicit_tracks(self):
        self.gates()
        candidate = self.candidate(ext='webm', acodec='opus', vcodec='none',
                                   url='https://cdn.example.org/voice.webm')
        response = self.post(candidate)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['type'], 'audio')
        self.assertTrue(response.json()['item']['filename'].endswith('.webm'))
        self.assertTrue(capability(self.candidate(ext='webm', vcodec='vp9', acodec='opus'))['can_download'])
        self.assertFalse(capability(self.candidate(ext='webm', vcodec=None, acodec=None))['can_download'])

    def test_selected_links_recheck_identity_without_granting_download_authority(self):
        self.gates()
        for changes in ({'protocol': 'm3u8_native'}, {'acodec': None, 'vcodec': None},
                        {'http_headers': {'Referer': 'https://source.example.org'}}):
            candidate = self.candidate(**changes)
            response = self.post(candidate, 'link')
            self.assertEqual(response.status_code, 200, response.content)
            item = response.json()['item']
            self.assertEqual(item['candidate_id'], candidate['candidate_id'])
            self.assertEqual(item['url'], 'https://cdn.example.org/one.mp4')
            self.assertNotIn('browser_direct_fetch_safe', item)
            self.assertNotIn('headers', item)
            self.assertEqual(self.post(candidate).status_code, 409)
        with patch('xhs_download.services.platforms.third_party.provider_is_enabled', return_value=False):
            self.assertEqual(self.post(candidate, 'link').status_code, 403)
        cache.clear()
        self.assertEqual(self.post(candidate, 'link').status_code, 403)

    def test_other_providers_and_collections_keep_existing_presentation(self):
        candidate = self.candidate()
        self.assertIsNone(build_picker([{**candidate, 'provider_id': 'gallery_dl_generic'}]))
        self.assertIsNone(build_picker([{k: v for k, v in candidate.items() if k != 'work'}]))
        result = self.result(_type='playlist', entries=[{'title': 'child', **self.video('child')}],
                             upstream_scope={'bounded': True})
        self.assertNotIn('work', result.metadata['upstream_candidates'][0])

    def test_home_and_batch_keep_original_download_controls_with_selector(self):
        result = self.result()
        candidates = result.metadata['upstream_candidates']
        html = render_to_string('partials/xhs_results_region.html', {'upstream_candidates': candidates,
            'video_items': [{'video_url': 'https://cdn.example.org/old.mp4'}], 'base_filename': 'sample'})
        self.assertIn('data-ytdlp-picker', html)
        self.assertIn('id="download-btn"', html)
        self.assertIn('id="select-all-btn"', html)
        self.assertNotIn('data-url="https://cdn.example.org/old.mp4"', html)
        self.assertNotIn('src="https://cdn.example.org/', html)
        batch_context = {'row': {'status': 'success', 'upstream_candidates': candidates,
                                'video_items': [], 'image_items': [], 'base_filename': 'sample'},
                         'batch_download_allowed': True}
        batch = render_to_string('partials/batch_result_card.html', batch_context)
        self.assertIn('data-ytdlp-picker', batch)
        self.assertIn('data-picker-download-all', batch)
        denied_batch = render_to_string('partials/batch_result_card.html',
                                        {**batch_context, 'batch_download_allowed': False})
        self.assertNotIn('data-picker-download-all', denied_batch)
        for markup in (html, batch):
            soup = BeautifulSoup(markup, 'html.parser')
            self.assertFalse(soup.select('[data-ytdlp-action="download"], '
                                         '[data-ytdlp-action="copy"], [data-ytdlp-action="legacy"]'))
            for removed in ('原有方式保存所选资源', '复制所选链接', '不需要选择解析通道'):
                self.assertNotIn(removed, soup.get_text())

    def test_more_than_64_formats_remain_selectable_without_dedup_by_resolution(self):
        candidates = self.result([self.video(f'fmt-{i}', height=1080) for i in range(90)]).metadata['upstream_candidates']
        picker = build_picker(candidates)
        self.assertEqual(picker['format_count'], 90)
        html = render_to_string('partials/ytdlp_result_picker.html', {'ytdlp_picker': picker})
        self.assertEqual(html.count('data-picker-radio'), 90)
        self.assertNotIn('其他格式', html)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertEqual(len(soup.select('[data-picker-primary] [data-picker-radio]')), 90)
        self.assertFalse(soup.select('[data-picker-other]'))
        self.assertFalse(soup.select('[data-picker-option][hidden]'))

    def test_selected_action_returns_the_selected_resource_not_first_default(self):
        self.gates()
        candidates = self.result().metadata['upstream_candidates']
        first = self.post(candidates[0]); second = self.post(candidates[1])
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()['item']['url'], 'https://cdn.example.org/1080.mp4')
        self.assertEqual(second.json()['item']['url'], 'https://cdn.example.org/720.mp4')
        self.assertEqual(second.json()['item']['selected_format_id'], '720')
        self.assertEqual(second.json()['item']['candidate_id'], candidates[1]['candidate_id'])
        self.assertNotIn('headers', second.json()['item'])
        self.assertIn('no-store', second['Cache-Control'])
        self.assertFalse(second.json()['server_media_relay'])

    def test_audio_descriptor_keeps_type_extension_and_no_transcoding(self):
        self.gates()
        candidate = self.candidate(ext='m4a', acodec='aac', vcodec='none', url='https://cdn.example.org/song.m4a')
        response = self.post(candidate)
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['type'], 'audio')
        self.assertTrue(item['filename'].endswith('.m4a'))
        self.assertEqual(item['video_tracks'], 'absent')

    def test_action_requires_current_admission_even_after_parse(self):
        self.gates(); candidate = self.candidate()
        for name in ('xhs_download.services.platforms.third_party.provider_is_enabled',
                     'xhs_download.services.provider_access.upstream_match_is_public_allowed',
                     'xhs_download.services.provider_access.third_party_public_access_allowed'):
            with patch(name, return_value=False):
                self.assertEqual(self.post(candidate).status_code, 403)
        with patch('xhs_download.services.provider_access.upstream_match_is_blocked', return_value=True):
            self.assertEqual(self.post(candidate).status_code, 403)

    def test_tampering_extra_urls_and_expired_cache_are_rejected(self):
        self.gates(); candidate = self.candidate()
        self.assertEqual(self.post(candidate, url='https://wrong.example/a.mp4').status_code, 400)
        self.assertEqual(self.post(candidate, format_id='best').status_code, 400)
        self.assertEqual(self.post(candidate, candidate_id='different').status_code, 403)
        self.assertEqual(self.post(candidate, resource_token=candidate['resource_token'] + 'x').status_code, 403)
        cache.clear()
        self.assertEqual(self.post(candidate).status_code, 403)

    def test_cost_and_rate_limits_are_not_bypassed(self):
        self.gates(); candidate = self.candidate()
        with patch('xhs_download.views.check_download_cost_preflight', return_value=('LIMIT', '太大', 413)):
            self.assertEqual(self.post(candidate).status_code, 413)
        from django.http import JsonResponse
        with patch('xhs_download.views.check_download_request_limits', return_value=('client', JsonResponse({'status': 'error'}, status=429))):
            self.assertEqual(self.post(candidate).status_code, 429)

    def test_cover_is_bound_to_cached_work_and_does_not_create_download_authority(self):
        self.gates(); candidate = self.candidate()
        with patch('xhs_download.services.rate_limits.consume_fixed_window', return_value=SimpleNamespace(allowed=True)), \
                patch('xhs_download.views.check_download_request_limits') as download:
            response = self.post(candidate, 'cover')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['url'], 'https://cdn.example.org/cover.jpg')
        self.assertNotIn('item', response.json())
        download.assert_not_called()

    def test_csrf_and_method_protection(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse('ytdlp_selected_format'), {}, content_type='application/json').status_code, 403)
        self.assertEqual(self.client.get(reverse('ytdlp_selected_format')).status_code, 405)

    def test_route_preference_reordering_does_not_lose_work_card_or_cover_binding(self):
        from .services.upstream_routes import apply_format_preferences
        original = self.result()
        first = original.metadata['upstream_candidates'][0]
        reordered = apply_format_preferences(original, {'max_height': 720})
        picker = build_picker(reordered.metadata['upstream_candidates'], {'max_height': 720})
        self.assertEqual(picker['work']['title'], 'A useful work')
        self.assertEqual(picker['work']['source_host'], 'site.example.org')
        self.assertEqual(picker['first']['candidate_id'], first['candidate_id'])
        self.assertEqual(picker['selected']['format_id'], '720')
        self.assertEqual(len(picker['videos']), 2)

    def test_initial_choices_show_all_resolution_variants(self):
        candidates = self.result([self.video('a'), self.video('b', fps=60), self.video('c', 720)]).metadata['upstream_candidates']
        picker = build_picker(candidates)
        self.assertEqual(len(picker['videos']), 3)
        self.assertEqual(sum(c['initial_visible'] for c in picker['videos']), 3)
        self.assertEqual(picker['video_primary'], picker['videos'])
        self.assertEqual(picker['video_other'], [])

    def test_all_video_variants_are_visible_and_audio_stays_secondary(self):
        formats = [self.video('4k', 2160, acodec='none'), self.video('direct', 1080),
                   self.video('60fps', 1080, fps=60), self.video('av1', 1080, vcodec='av1'),
                   self.video('japanese', 1080, language='ja'),
                   self.video('hls', 1080, protocol='m3u8_native'),
                   self.video('audio', None, ext='m4a', vcodec='none', abr=128)]
        candidates = self.result(formats).metadata['upstream_candidates']
        picker = build_picker(candidates)
        self.assertEqual(picker['selected']['format_id'], '60fps')
        self.assertEqual([c['format_id'] for c in picker['video_primary']],
                         ['4k', 'direct', '60fps', 'av1', 'japanese', 'hls'])
        self.assertEqual(picker['video_other'], [])
        soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html',
                                             {'ytdlp_picker': picker}), 'html.parser')
        video = soup.select_one('[data-picker-group="video"]')
        self.assertFalse(video.select('[data-picker-other], details, [data-picker-option][hidden]'))
        self.assertEqual(len(video.select('[data-picker-primary] [data-picker-radio]')), 6)
        self.assertIn('2160p', video.get_text())
        self.assertIn('合并', video.get_text())
        projected = {c['format_id']: c for c in picker['videos']}
        self.assertIn('av1', projected['av1']['detail_label'])
        self.assertIn('ja', projected['japanese']['detail_label'])
        self.assertIn('m3u8_native', projected['hls']['detail_label'])
        self.assertIn('60 fps', projected['60fps']['detail_label'])
        self.assertFalse(video.select('[data-picker-detail]'))
        self.assertNotIn('open', soup.select_one('.ytdlp-audio-formats').attrs)
        self.assertEqual(len(soup.select('[data-picker-radio]')), len(candidates))
        self.assertCountEqual([(r['value'], r['data-token']) for r in soup.select('[data-picker-radio]')],
                              [(c['candidate_id'], c['resource_token']) for c in candidates])
        self.assertEqual(soup.select_one('[data-picker-primary] input[checked]')['value'],
                         picker['selected']['candidate_id'])

    def test_audio_tiers_are_primary_without_outer_disclosure(self):
        candidates = self.result([
            self.video('audio-high', None, ext='m4a', vcodec='none', abr=256),
            self.video('audio-alt', None, ext='webm', vcodec='none', acodec='opus', abr=256),
            self.video('audio-stream', None, ext='m4a', vcodec='none', abr=320, protocol='m3u8_native'),
        ]).metadata['upstream_candidates']
        picker = build_picker(candidates)
        self.assertTrue(picker['is_audio'])
        self.assertEqual(picker['selected']['format_id'], 'audio-high')
        soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html',
                                             {'ytdlp_picker': picker}), 'html.parser')
        self.assertIsNone(soup.select_one('.ytdlp-audio-formats'))
        self.assertEqual(len(soup.select('[data-picker-primary] [data-picker-radio]')), 1)
        other = soup.select_one('[data-picker-other]')
        self.assertNotIn('open', other.attrs)
        self.assertIn('其他格式 (2)', other.summary.get_text())
        self.assertIn('含 320 kbps', other.summary.get_text())
        self.assertEqual(len(other.select('[data-picker-radio]')), 2)

    def test_all_restricted_formats_open_with_individual_reasons(self):
        videos = [self.video('split', 2160, acodec='none'),
                  self.video('stream', protocol='m3u8_native'),
                  self.video('drm', has_drm=True),
                  self.video('context', http_headers={'Referer': 'https://site.example.org/'})]
        audio = self.video('audio-stream', None, ext='m4a', vcodec='none', protocol='m3u8_native')
        for formats in (videos, [audio], videos + [audio]):
            with self.subTest(formats=[f['format_id'] for f in formats]):
                picker = build_picker(self.result(formats).metadata['upstream_candidates'])
                self.assertTrue(picker['all_unavailable'])
                self.assertFalse(picker['selected']['can_download'])
                soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html',
                                                     {'ytdlp_picker': picker}), 'html.parser')
                self.assertEqual(len(soup.select('[data-picker-group="video"] [data-picker-primary] [data-picker-radio]')),
                                 len(picker['videos']))
                self.assertFalse(soup.select('[data-picker-group="video"] details'))
                self.assertTrue(all('open' in d.attrs for d in soup.select('details')))
                self.assertEqual(len(soup.select('[data-picker-radio]')), len(formats))
                self.assertEqual(len(soup.select('.ytdlp-format-restricted')), len(formats))
                for candidate in picker['videos'] + picker['audios']:
                    self.assertIn(candidate['limitation'], soup.get_text())
                self.assertTrue(all(radio['data-can-download'] == 'false'
                                    for radio in soup.select('[data-picker-radio]')))
                if picker['is_audio']:
                    self.assertIn('disabled', soup.select_one('[data-ytdlp-action="download"]').attrs)
                else:
                    self.assertIsNone(soup.select_one('[data-ytdlp-action="download"]'))

    def test_restricted_same_quality_does_not_displace_preferred_direct_default(self):
        candidates = self.result([self.video('split', 1080, acodec='none'), self.video('direct', 1080),
                                  self.video('preferred', 720)]).metadata['upstream_candidates']
        picker = build_picker(candidates, {'max_height': 720})
        self.assertEqual(picker['selected']['format_id'], 'preferred')
        self.assertEqual([c['format_id'] for c in picker['video_primary']], ['split', 'direct', 'preferred'])
        self.assertEqual(picker['video_other'], [])
        self.assertFalse(picker['video_primary'][0]['can_download'])

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True, YTDLP_PROVIDER_ENABLED=True)
    def test_anonymous_selected_actions_use_real_admission_and_download_policy(self):
        # Only stored settings and extractor matching are fixtures; permission
        # decisions and AnonymousUser come from the real request path.
        candidate = self.candidate()
        rule = SimpleNamespace(platform_id='upstream', risk_level='low', staff_default=True,
                               provider_ids=('yt_dlp_generic',), upstream_matches=(self.match,))
        for mode, video_enabled, download_status, link_status in (
            (ProviderAccessMode.PUBLIC, True, 200, 200),
            (ProviderAccessMode.STAFF, True, 403, 403),
            (ProviderAccessMode.PUBLIC, False, 403, 200),
        ):
            with self.subTest(mode=mode, video_enabled=video_enabled):
                runtime = ProviderRuntimeSetting(third_party_mode=mode,
                    third_party_allowed_public_platforms='upstream:yt_dlp_generic')
                download_runtime = DownloadRuntimeSetting(visitor_video_download_enabled=video_enabled)
                with patch('xhs_download.services.provider_access.load_runtime_setting', return_value=runtime), \
                        patch('xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url',
                              return_value=rule), \
                        patch('xhs_download.views.get_download_runtime_setting', return_value=download_runtime), \
                        patch('xhs_download.views.check_download_request_limits', return_value=('client', None)):
                    download = self.post(candidate)
                    link = self.post(candidate, 'link')
                self.assertTrue(download.wsgi_request.user.is_anonymous)
                self.assertTrue(link.wsgi_request.user.is_anonymous)
                self.assertEqual(download.status_code, download_status, download.content)
                self.assertEqual(link.status_code, link_status, link.content)
                if mode == ProviderAccessMode.PUBLIC and not video_enabled:
                    self.assertEqual(download.json()['error_code'], 'VISITOR_VIDEO_DOWNLOAD_DISABLED')
                    self.assertEqual(link.json()['item']['candidate_id'], candidate['candidate_id'])
                    self.assertNotIn('browser_direct_fetch_safe', link.json()['item'])

    def test_private_descriptor_revalidation_rejects_required_context_even_after_truncation(self):
        self.gates()
        headers = {'User-Agent': 'x'}
        headers.update({f'Accept-{i}': 'x' for i in range(35)})
        headers['Cookie'] = 'not-public'
        candidate = self.candidate(http_headers=headers)
        self.assertEqual(self.post(candidate).status_code, 409)

    def test_admission_service_failure_is_not_a_success_or_unhandled_exception(self):
        self.gates()
        candidate = self.candidate()
        with patch('xhs_download.services.provider_access.load_runtime_setting', side_effect=RuntimeError('private message')):
            response = self.post(candidate)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private message', response.content.decode())
