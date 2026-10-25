"""Bounded display projection using real issuance, with all network forbidden."""
from copy import deepcopy
from unittest.mock import patch

from django.core.cache import cache
from django.template import Context, Template
from django.test import SimpleTestCase, override_settings

from .services.candidates import MediaCandidate
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import (
    DouyinParseContext, attach_douyin_service_picker, build_external_parse_result,
)
from .services.service_format_selection import build_service_picker
from .services.ytdlp_selection import build_picker
from .templatetags.ytdlp_picker import remaining_picker_videos, ytdlp_presentation


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class QualityPresentationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for target in ('requests.sessions.Session.request', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Unexpected network request'))
            guard.start()
            self.addCleanup(guard.stop)

    def result(self, variants=None):
        base = 'https://v3-dy-o.zjcdn.com/fixture/'
        variants = variants or [('main', '原画', 55000000), ('hd', '1080p', 0),
                                ('hd', '1080P', 2700000), ('sd', '720p', 2000000),
                                ('low', '540p', 2300000)]
        candidates = tuple(MediaCandidate(url=f'{base}{name}.mp4', quality_label=label, size_bytes=size)
                           for name, label, size in variants)
        adapter = DouyinAdapter()
        context = DouyinParseContext(url='https://www.douyin.com/video/7123456789012345678',
                                    aweme_id='7123456789012345678')
        original = build_external_parse_result(adapter, context, candidates[0], provider_name='tiantianjiexi',
                                               base_filename='真实作品 #日常')
        return attach_douyin_service_picker(original, adapter=adapter, context=context, candidates=candidates)

    def test_five_native_rows_project_four_exact_resources_without_mutation(self):
        result = self.result()
        rows = result.metadata['upstream_candidates']
        before = deepcopy(rows)
        presentation = build_service_picker(rows)
        self.assertEqual([row['label'] for row in presentation['videos']], ['原画', '1080p', '720p', '540p'])
        self.assertEqual(presentation['format_count'], 4)
        self.assertEqual(presentation['video_group_count'], 1)
        self.assertEqual(presentation['video_primary'], presentation['videos'])
        self.assertEqual(presentation['video_other'], [])
        hd = presentation['videos'][1]
        self.assertEqual(hd['size_bytes'], 2700000)
        self.assertEqual(hd['candidate_id'], rows[2]['candidate_id'])
        self.assertEqual(hd['resource_token'], rows[2]['resource_token'])
        self.assertEqual(hd['alias_candidate_ids'], [rows[1]['candidate_id'], rows[2]['candidate_id']])
        self.assertEqual(rows, before)
        self.assertEqual(len(rows), 5)
        self.assertEqual(presentation['selected']['candidate_id'], rows[0]['candidate_id'])
        self.assertFalse(any('url' in row for row in presentation['videos']))

    def test_known_size_can_inform_default_alias_without_replacing_its_identity(self):
        rows = self.result([('main', '1080p', 0), ('main', '1080P', 2000)]).metadata['upstream_candidates']
        selected = build_service_picker(rows)['selected']
        self.assertEqual(selected['candidate_id'], rows[0]['candidate_id'])
        self.assertEqual(selected['resource_token'], rows[0]['resource_token'])
        self.assertEqual(selected['label'], '1080p')
        self.assertEqual(selected['size_bytes'], 2000)
        self.assertIsNone(rows[0]['size_bytes'])

    def test_conflicting_known_sizes_remain_separate_including_unknown_alias(self):
        rows = self.result([('main', '1080p', 9000), ('main', '1080P', 8000),
                            ('main', '1080p', 0)]).metadata['upstream_candidates']
        presentation = build_service_picker(rows)
        self.assertEqual(len(presentation['videos']), 3)
        self.assertEqual(presentation['selected']['size_bytes'], 9000)

    def test_different_quality_labels_never_share_a_display_identity_or_size(self):
        for labels in (('原画', '1080P'), ('原画', 'source'), ('original', 'source'),
                       ('1080p', '720p'), ('720P', '流畅'), ('1080p HDR', '1080P SDR')):
            with self.subTest(labels=labels):
                rows = self.result([('main', labels[0], 0), ('main', labels[1], 2000)]).metadata['upstream_candidates']
                before = deepcopy(rows)
                presentation = build_service_picker(rows)
                self.assertEqual(len(presentation['videos']), 2)
                self.assertEqual(presentation['selected']['candidate_id'], rows[0]['candidate_id'])
                self.assertIsNone(presentation['selected']['size_bytes'])
                self.assertTrue(all('alias_candidate_ids' not in row for row in presentation['videos']))
                self.assertCountEqual([(row['candidate_id'], row['resource_token']) for row in presentation['videos']],
                                      [(row['candidate_id'], row['resource_token']) for row in rows])
                self.assertEqual(rows, before)

    def test_case_only_aliases_use_legacy_binding_for_old_cached_candidates(self):
        for labels in (('720p', '720P'), ('1080p+', '1080P+'), ('2160p', '2160P'), ('Source', 'SOURCE')):
            with self.subTest(labels=labels):
                rows = self.result([('main', labels[0], 0), ('main', labels[1], 2000)]).metadata['upstream_candidates']
                rows = [{key: value for key, value in row.items() if key != 'resource_alias_key'} for row in rows]
                before = deepcopy(rows)
                presentation = build_service_picker(rows)
                self.assertEqual(len(presentation['videos']), 1)
                self.assertEqual(presentation['selected']['candidate_id'], rows[0]['candidate_id'])
                self.assertEqual(presentation['selected']['resource_token'], rows[0]['resource_token'])
                self.assertEqual(presentation['selected']['size_bytes'], 2000)
                self.assertEqual(rows, before)

    def test_numeric_plus_source_and_unknown_stable_order_is_not_size_order(self):
        variants = [('main', '720P', 100), ('unknown1', '高清', 99999999),
                    ('hd', '1080P', 200), ('source', 'source', 50),
                    ('hdplus', '1080P+', 1), ('unknown2', '流畅', 999), ('low', '540P', 9000)]
        rows = self.result(variants).metadata['upstream_candidates']
        presentation = build_service_picker(rows)
        self.assertEqual([row['label'] for row in presentation['videos']],
                         ['source', '1080p+', '1080p', '720p', '540p', '高清', '流畅'])
        self.assertEqual(presentation['selected']['candidate_id'], rows[0]['candidate_id'])
        self.assertTrue(all(row['height'] is None for row in presentation['videos']))

    def test_same_bound_url_different_media_or_context_facts_never_merge(self):
        original = self.result([('main', '1080p', 100), ('main', '1080P', 100)]).metadata['upstream_candidates']
        for difference in ({'fps': 60}, {'vcodec': 'hevc'}, {'language': 'ja'},
                           {'dynamic_range': 'HDR10'}, {'audio_asset_present': True},
                           {'format_id': 'different-native-format'}, {'audio_bitrate': 256},
                           {'upstream_asset_id': 'different-native-asset'},
                           {'relationships': [{'source_asset_id': 'video', 'target_asset_id': 'audio',
                                               'relationship_type': 'audio_for'}]},
                           {'protocol': 'm3u8_native'}, {'audio_tracks': 'absent'},
                           {'download_context_required': True}, {'requires_context': True},
                           {'headers_present': True}, {'requires_cookie': True}, {'has_drm': True},
                           {'legacy_delivery': ''}, {'legacy_asset_id': ''}):
            with self.subTest(difference=difference):
                rows = [original[0], {**original[1], **difference}]
                self.assertEqual(len(build_service_picker(rows)['videos']), 2)

    def test_distinct_resources_and_work_groups_do_not_merge(self):
        rows = self.result([('main', '1080p', 100), ('other', '1080P', 100)]).metadata['upstream_candidates']
        self.assertEqual(len(build_service_picker(rows)['videos']), 2)
        same = self.result([('main', '1080p', 100), ('main', '1080P', 100)]).metadata['upstream_candidates']
        presentation = build_service_picker([same[0], {**same[1], 'work_id': 'other-work'}])
        self.assertEqual(presentation['video_group_count'], 2)
        self.assertEqual([len(group['videos']) for group in presentation['groups']], [1, 1])
        self.assertIsNone(build_service_picker([same[0], {**same[1], 'provider_id': 'kukutool'}]))

    def test_legacy_preview_uses_any_exact_group_binding_and_keeps_remaining_contract(self):
        result = self.result()
        rows = result.metadata['upstream_candidates']
        legacy = result.video_assets[0].to_download_item()
        unrelated = {**legacy, 'asset_id': 'different-resource'}
        presentation = ytdlp_presentation(rows, legacy_videos=[unrelated, legacy])
        self.assertIs(presentation['legacy_video'], legacy)
        self.assertEqual(remaining_picker_videos(presentation, [unrelated, legacy]), [unrelated])
        self.assertIsNone(ytdlp_presentation(rows, legacy_videos=[unrelated])['legacy_video'])
        self.assertEqual(remaining_picker_videos(None, [legacy]), [legacy])

    def test_context_title_is_escaped_and_not_borrowed_by_other_work_groups(self):
        rows = self.result().metadata['upstream_candidates']
        rows = [{**row, 'work': {**row['work'], 'title': 'Video'}} for row in rows]
        title = '真实作品 <片段> #日常'
        presentation = ytdlp_presentation(rows, display_title=title)
        self.assertEqual(presentation['work']['title'], title)
        self.assertEqual(rows[0]['work']['title'], 'Video')
        rendered = Template('{% load ytdlp_picker %}{% ytdlp_presentation rows display_title=title as p %}{{ p.work.title }}').render(
            Context({'rows': rows, 'title': title}))
        self.assertIn('&lt;片段&gt;', rendered)
        multiple = ytdlp_presentation([rows[0], {**rows[1], 'work_id': 'other'}], display_title=title)
        self.assertEqual([group['work']['title'] for group in multiple['groups']], ['Video', 'Video'])
        self.assertEqual(ytdlp_presentation(rows, base_filename='文件标题')['work']['title'], '文件标题')
        self.assertEqual(ytdlp_presentation(rows)['work']['title'], 'Video')

    def test_ytdlp_default_checks_and_audio_tiers_are_unchanged(self):
        def row(identity, **extra):
            return {'candidate_id': identity, 'provider_id': 'yt_dlp_generic', 'media_type': 'video',
                    'work_id': 'one', 'work': {'schema': 'ytdlp_selected_format_v1', 'title': 'Work'},
                    'height': 1080, 'protocol': 'https', 'extension': 'mp4', 'delivery_state': 'complete',
                    'audio_tracks': 'present', 'video_tracks': 'present', **extra}
        rows = [row('normal'), row('fast', fps=60), row('unknown', height=None,
                delivery_state='unknown_media_tracks'), row('stream', height=2160, protocol='m3u8_native')]
        presentation = build_picker(rows)
        self.assertEqual(presentation['selected']['candidate_id'], 'fast')
        self.assertEqual(len(presentation['video_primary']), 4)
        self.assertEqual(presentation['video_other'], [])
        self.assertTrue(next(item for item in presentation['videos'] if item['candidate_id'] == 'unknown')['can_check'])
        self.assertFalse(presentation['videos'][0]['can_download'])
        audios = [row(identity, media_type='audio', height=None, video_tracks='absent',
                      extension='m4a', delivery_state='audio_only', audio_bitrate=256)
                  for identity in ('audio-main', 'audio-alt')]
        audio = build_picker(audios)
        self.assertTrue(audio['is_audio'])
        self.assertEqual(audio['video_group_count'], 0)
        self.assertEqual(len(audio['audio_primary']), 1)
        self.assertEqual(len(audio['audio_other']), 1)

    def test_repeated_quality_labels_describe_only_known_variant_differences(self):
        rows = self.result([('main', '1080p', 100), ('other', '1080P', 100),
                            ('third', '1080p', 100), ('low', '720p', 50)]).metadata['upstream_candidates']
        first = {'fps': 30, 'vcodec': 'h264', 'acodec': 'aac', 'language': 'en',
                 'extension': 'mp4', 'protocol': 'https'}
        second = {'fps': 60, 'vcodec': 'av1', 'acodec': 'opus', 'language': 'ja',
                  'extension': 'webm', 'protocol': 'm3u8_native'}
        rows = [{**rows[0], **first}, {**rows[1], **second}, {**rows[2], **first}, rows[3]]
        before = deepcopy(rows)
        videos = build_service_picker(rows)['videos']
        self.assertEqual(len(videos), 4)
        self.assertEqual(videos[0]['variant_label'], '30 fps · h264 · aac · en · MP4 · https · 版本 1')
        self.assertEqual(videos[1]['variant_label'], '60 fps · av1 · opus · ja · WEBM · m3u8_native')
        self.assertEqual(videos[2]['variant_label'], '30 fps · h264 · aac · en · MP4 · https · 版本 3')
        self.assertEqual(videos[3]['variant_label'], '')
        self.assertEqual(rows, before)

    def test_unobservable_variants_have_stable_ordinals_without_unknown_clutter(self):
        rows = self.result([('main', '1080p', 100), ('other', '1080P', 100)]).metadata['upstream_candidates']
        rows[0]['fps'] = 60
        rows[1]['vcodec'] = 'unknown'
        videos = build_service_picker(rows)['videos']
        self.assertEqual([row['variant_label'] for row in videos], ['版本 1', '版本 2'])
        self.assertEqual([row['candidate_id'] for row in videos], [row['candidate_id'] for row in rows])
