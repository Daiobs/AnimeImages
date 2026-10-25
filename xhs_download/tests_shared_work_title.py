"""Shared naming through real normalization, candidate caching and actions."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from download_console.models import DownloadRuntimeSetting
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.platforms.kukutool_generic import KukutoolResultNormalizer
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .services.upstream_candidates import issue_candidates, resolve_candidate_record
from .services.upstream_delivery import normalize_upstream_result


TITLE = '那些再也拍不出第二张的人生照片……'
BODY = '保留完整的拍摄过程和作品说明。' * 20
CAPTION = (f'{TITLE} {TITLE} @摄影薯 #引导是出片的关键 #来拍照了 #一分钟拍照灵感 '
           '#小红书影像激励计划 #再也拍不出第二次的人')
# Text captured from the real service response; media in these tests stays synthetic.
JOINED_CAPTION = (f'{TITLE} |||{TITLE} @摄影薯 #引导是出片的关键  #来拍照了 \n'
                  '#一分钟拍照灵感 \n#小红书影像激励计划 \n#再也拍不出第二次的人生照 \n'
                  '#比美照更有杀伤力的是 \n#给摄影一分钟')
SOURCE = 'https://source.example/work/one'
MEDIA = 'https://media.example/video'
PROVIDERS = ('kukutool_generic', 'tiantianjiexi_generic')


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, YTDLP_PROVIDER_ENABLED=True,
    THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE='offline', TIANTIANJIEXI_PASSWORD='offline',
)
class SharedWorkTitleTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.enterContext(patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('unexpected network')))
        self.rule = SimpleNamespace(platform_id='fixture', risk_level='low', staff_default=True,
                                    provider_ids=PROVIDERS + ('yt_dlp_generic',),
                                    media_host_suffixes=('media.example',),
                                    image_media_host_suffixes=(), video_media_host_suffixes=())
        runtime = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.PUBLIC,
                                        third_party_allowed_public_platforms='fixture,upstream:yt_dlp_generic')
        self.enterContext(patch('xhs_download.services.provider_access.load_runtime_setting', return_value=runtime))
        self.enterContext(patch('xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url',
                                return_value=self.rule))
        self.enterContext(patch('xhs_download.views.get_download_runtime_setting',
                                return_value=DownloadRuntimeSetting(visitor_video_download_enabled=True)))
        self.requests = []

        def send(request, **kwargs):
            self.assertIn(request.url, (MEDIA + '.mp4', MEDIA + '.webm'))
            self.assertEqual(request.method, 'HEAD')
            self.requests.append((request.method, request.url))
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response.headers.update({'Content-Type': 'video/webm' if request.url.endswith('.webm') else 'video/mp4',
                                     'Content-Length': '32', 'Content-Disposition': 'inline'})
            response.raw = BytesIO(b'')
            return response

        self.enterContext(patch('requests.adapters.HTTPAdapter.send', side_effect=send))

    def normalize(self, provider, title, description='', extension='mp4'):
        payload = {'title': title, 'description': description,
                   'videos': [{'url': MEDIA + '.' + extension, 'quality': '720P', 'size': 32}]}
        before = deepcopy(payload)
        normalizer = KukutoolResultNormalizer() if provider == 'kukutool_generic' else TianTianJieXiResultNormalizer()
        result = normalizer.normalize(rule=self.rule, platform_name='Fixture', source_url=SOURCE, payload=payload)
        self.assertEqual(payload, before)
        return result, result.metadata['upstream_candidates'][0]

    def action(self, row, action='download'):
        endpoint = 'ytdlp_selected_format' if row['provider_id'] == 'yt_dlp_generic' else 'service_selected_format'
        response = self.client.post(reverse(endpoint), {
            'candidate_id': row['candidate_id'], 'resource_token': row['resource_token'], 'action': action,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['item']

    def assert_name_and_authority(self, row, title, extension):
        record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
        before = deepcopy(record)
        link = self.action(row, 'link')
        item = self.action(row)
        suffix = row.get('format_id') or row['candidate_id']
        self.assertEqual(item['filename'], f'{title}_{suffix}.{extension}')
        self.assertEqual(link['filename'], item['filename'])
        self.assertEqual(item['url'], record['url'])
        self.assertEqual(item['candidate_id'], row['candidate_id'])
        self.assertEqual(item['selected_format_id'], row.get('format_id') or '')
        self.assertEqual(item['selection_expires_at'], int(record['expires_at'] * 1000))
        self.assertEqual(resolve_candidate_record(row['resource_token'], row['candidate_id']), before)

    def test_service_explicit_title_wins_and_description_survives(self):
        description = f'{TITLE}\n{BODY}\n#摄影 #作品'
        for provider in PROVIDERS:
            for extension in ('mp4', 'webm'):
                with self.subTest(provider=provider, extension=extension):
                    result, row = self.normalize(provider, 'C# Basics (Part 1) + .NET', description, extension)
                    self.assert_name_and_authority(row, 'C# Basics (Part 1) + .NET', extension)
                    self.assertEqual(result.metadata['description'], description)
                    self.assertEqual(result.base_filename, 'C# Basics (Part 1) + .NET')

    def test_description_only_is_shortened_before_candidate_text_limits(self):
        description = f'{TITLE}\n{BODY}\n#摄影 #作品'
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, '', description)
                self.assert_name_and_authority(row, TITLE, 'mp4')
                self.assertEqual(result.base_filename, TITLE)
                self.assertEqual(result.metadata['description'], description)

    def test_combined_repeated_lead_is_short_without_discarding_caption(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, CAPTION)
                self.assert_name_and_authority(row, TITLE, 'mp4')
                self.assertEqual(result.metadata['caption'], CAPTION)

    def test_joined_repeated_caption_is_split_before_filename_sanitization(self):
        from .views import build_result_display_context
        for provider in PROVIDERS:
            for extension in ('mp4', 'webm'):
                with self.subTest(provider=provider, extension=extension):
                    result, row = self.normalize(provider, JOINED_CAPTION, extension=extension)
                    self.assert_name_and_authority(row, TITLE, extension)
                    self.assertEqual(result.base_filename, TITLE)
                    self.assertEqual(result.metadata['title'], TITLE)
                    self.assertEqual(result.metadata['caption'], JOINED_CAPTION)
                    self.assertEqual(result.metadata['description'], '@摄影薯')
                    self.assertEqual(result.metadata['topics'], [
                        '引导是出片的关键', '来拍照了', '一分钟拍照灵感', '小红书影像激励计划',
                        '再也拍不出第二次的人生照', '比美照更有杀伤力的是', '给摄影一分钟'])
                    context = build_result_display_context(result, probe_missing_video_sizes=False)
                    self.assertEqual(context['display_title'], TITLE)
                    self.assertEqual(context['display_description'], '@摄影薯')
                    self.assertEqual(context['display_topics'], result.metadata['topics'])

    def test_joined_caption_retains_body_and_does_not_split_ordinary_pipe_titles(self):
        from .services.work_caption import work_caption_metadata
        caption = f'{TITLE} |||{TITLE} 拍摄过程与感想。 #00后 #3分钟学会拍照'
        text = work_caption_metadata(caption, '单独保存的补充说明 #摄影')
        self.assertEqual(text['title'], TITLE)
        self.assertEqual(text['description'], '拍摄过程与感想。\n单独保存的补充说明')
        self.assertEqual(text['topics'], ['00后', '3分钟学会拍照', '摄影'])
        self.assertEqual(text['caption'], caption + '\n单独保存的补充说明 #摄影')
        for title in ('A ||| B', '#1 Release (2026) + C#', '主题 ||| 主题词不能当重复标题'):
            for description in ('', 'Independent body #release'):
                self.assertEqual(work_caption_metadata(title, description)['title'], title)

    def test_normal_titles_do_not_use_social_caption_word_splitting(self):
        for title in ('How to make a beautiful photograph in bright sunlight with a simple camera',
                      'C#入门教程', '#1 Release (2026) + C#', '中国国家地理 西藏特刊'):
            for provider in PROVIDERS:
                with self.subTest(title=title, provider=provider):
                    _, row = self.normalize(provider, title)
                    self.assert_name_and_authority(row, title, 'mp4')

    def test_numeric_topics_in_captions_reach_normalized_metadata_and_actions(self):
        body = BODY[:90]
        caption = f'{TITLE} {body} #00后 #3分钟学会拍照'
        for provider in PROVIDERS:
            for extension in ('mp4', 'webm'):
                for title, description in ((caption, ''), ('', caption)):
                    with self.subTest(provider=provider, extension=extension, description_only=not title):
                        result, row = self.normalize(provider, title, description, extension)
                        self.assert_name_and_authority(row, TITLE, extension)
                        self.assertEqual(result.base_filename, TITLE)
                        self.assertEqual(result.metadata['title'], TITLE)
                        self.assertEqual(result.metadata['topics'], ['00后', '3分钟学会拍照'])
                        self.assertEqual(result.metadata['caption'], caption)
                        self.assertEqual(result.metadata['description'], description or body)

    def test_numbered_ordinary_title_survives_with_and_without_independent_body(self):
        title = '#1 Release (2026) + C#'
        for provider in PROVIDERS:
            for extension in ('mp4', 'webm'):
                for description in ('', 'Independent description #00后 #3分钟学会拍照'):
                    with self.subTest(provider=provider, extension=extension, description=description):
                        result, row = self.normalize(provider, title, description, extension)
                        self.assert_name_and_authority(row, title, extension)
                        self.assertEqual(result.base_filename, title)
                        self.assertEqual(result.metadata['title'], title)
                        self.assertEqual(result.metadata['description'], description)
                        self.assertEqual(result.metadata['caption'], '\n'.join(
                            part for part in (title, description) if part))
                        self.assertEqual(result.metadata['topics'],
                                         ['00后', '3分钟学会拍照'] if description else [])

    def test_numeric_topic_caption_storage_keeps_existing_bounds(self):
        topics = ['00后', '3分钟学会拍照', '2026' + '拍' * 130] + [f'{index}分钟' for index in range(20)]
        description = f'{TITLE}\n{BODY * 20}\n' + ' '.join('#' + topic for topic in topics)
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, '', description)
                self.assert_name_and_authority(row, TITLE, 'mp4')
                self.assertEqual(result.metadata['description'], description[:2000])
                self.assertEqual(result.metadata['caption'], description[:2000])
                self.assertEqual(result.metadata['topics'], [topic[:120] for topic in topics[:16]])
                self.assertLessEqual(len(row['work_label']), 160)

    def test_old_numeric_caption_and_numbered_title_actions_preserve_authority(self):
        caption = f'{TITLE} {BODY[:90]} #00后 #3分钟学会拍照'
        ordinary_title = '#1 Release (2026) + C#'
        for provider in PROVIDERS:
            for extension in ('mp4', 'webm'):
                for label, expected in ((caption, TITLE), (ordinary_title, ordinary_title)):
                    with self.subTest(provider=provider, extension=extension, label=label):
                        row = issue_candidates([dict(url=MEDIA + '.' + extension, metadata={
                            'work_label': label, 'ext': extension, '_service_picker_issued': True,
                        }, ordinal=1, provider=provider, source_url=SOURCE, media_type='video',
                            state='candidate_only', match={'provider_id': provider})], work={
                                'title': label, 'source_host': 'source.example', 'collection': False})[0]
                        record = resolve_candidate_record(row['resource_token'], row['candidate_id'])
                        self.assertNotIn('filename_title', record)
                        with patch('django.core.signing.TimestampSigner.sign', side_effect=AssertionError('No renewal')):
                            self.assert_name_and_authority(row, expected, extension)

    def test_independent_title_is_not_reinterpreted_as_body_by_the_action(self):
        title = CAPTION
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, title, 'Independent description #作品')
                self.assert_name_and_authority(row, title[:160], 'mp4')
                self.assertEqual(result.metadata['description'], 'Independent description #作品')

    def test_stored_body_and_topics_are_bounded_after_title_derivation(self):
        description = f'{TITLE}\n{BODY * 20}\n' + ' '.join(f'#话题{index}' for index in range(100))
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, '', description)
                self.assert_name_and_authority(row, TITLE, 'mp4')
                self.assertEqual(result.metadata['description'], description[:2000])
                self.assertEqual(result.metadata['caption'], description[:2000])
                self.assertLessEqual(len(result.metadata['topics']), 16)
                self.assertLessEqual(len(row['work_label']), 160)

    def test_duplicate_title_description_is_not_repeated_in_stored_caption(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                result, row = self.normalize(provider, CAPTION, CAPTION)
                self.assert_name_and_authority(row, TITLE, 'mp4')
                self.assertEqual(result.metadata['caption'], CAPTION)
                self.assertEqual(result.metadata['description'], CAPTION)

    def test_old_normalized_payload_labels_use_untruncated_original_caption(self):
        caption = CAPTION + ' #延续话题' * 40
        for provider in PROVIDERS:
            normalizer = KukutoolResultNormalizer() if provider == 'kukutool_generic' else TianTianJieXiResultNormalizer()
            for label in (TITLE, caption, caption[:160]):
                with self.subTest(provider=provider, label_length=len(label)):
                    payload = normalizer.normalize_payload({'title': caption, 'description': caption,
                        'videos': [{'url': MEDIA + '.webm', 'quality': '720P'}]})
                    payload['_service_format_rows'] = [{**row, 'work_label': label}
                                                      for row in payload['_service_format_rows']]
                    cache.set('fixture:normalized-payload', payload, timeout=30)
                    before = deepcopy(payload)
                    result = normalizer.normalize(rule=self.rule, platform_name='Fixture', source_url=SOURCE,
                                                  payload=cache.get('fixture:normalized-payload'))
                    row = result.metadata['upstream_candidates'][0]
                    self.assert_name_and_authority(row, TITLE, 'webm')
                    self.assertEqual(result.metadata['caption'], caption)
                    self.assertEqual(cache.get('fixture:normalized-payload'), before)

    def test_author_prefix_does_not_turn_repeated_caption_into_independent_title(self):
        for provider in PROVIDERS:
            normalizer = KukutoolResultNormalizer() if provider == 'kukutool_generic' else TianTianJieXiResultNormalizer()
            for author in ('', 'Alice'):
                for title, description, expected in (
                    (CAPTION, CAPTION, TITLE),
                    ('C# Basics (Part 1)', '独立正文 #摄影', 'C# Basics (Part 1)'),
                    ('', f'{TITLE}\n{BODY} #摄影', TITLE),
                ):
                    with self.subTest(provider=provider, author=author, title=title):
                        result = normalizer.normalize(rule=self.rule, platform_name='Fixture', source_url=SOURCE,
                            payload={'author': author, 'title': title, 'description': description,
                                     'videos': [{'url': MEDIA + '.mp4', 'quality': '720P'}]})
                        prefix = 'Alice_' if author and provider == 'tiantianjiexi_generic' else ''
                        self.assertEqual(result.base_filename, prefix + expected)
                        self.assert_name_and_authority(result.metadata['upstream_candidates'][0], expected, 'mp4')
                        self.assertEqual(result.metadata['description'], description)
                        if title == description:
                            self.assertEqual(result.metadata['caption'], CAPTION)

    def test_old_issued_label_is_renamed_at_action_without_cache_mutation_or_renewal(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                row = issue_candidates([dict(url=MEDIA + '.webm', metadata={
                    'work_label': CAPTION, 'ext': 'webm', '_service_picker_issued': True,
                }, ordinal=1, provider=provider, source_url=SOURCE, media_type='video', state='candidate_only',
                    match={'provider_id': provider})], work={
                        'title': CAPTION, 'source_host': 'source.example', 'collection': False})[0]
                self.assertNotIn('filename_title', resolve_candidate_record(row['resource_token'], row['candidate_id']))
                with patch('django.core.signing.TimestampSigner.sign', side_effect=AssertionError('No renewal')):
                    self.assert_name_and_authority(row, TITLE, 'webm')

    def test_ytdlp_description_only_reaches_nonprimary_format_action(self):
        description = f'{TITLE}\n{BODY}\n#摄影'
        match = {'provider_id': 'yt_dlp_generic', 'extractor_id': 'Fixture', 'version': 'offline', 'label': 'Fixture'}
        result = normalize_upstream_result({'title': '', 'description': description, 'formats': [
            {'url': MEDIA + '.mp4', 'format_id': 'high', 'ext': 'mp4', 'vcodec': 'h264', 'acodec': 'aac', 'height': 1080},
            {'url': MEDIA + '.webm', 'format_id': 'low', 'ext': 'webm', 'vcodec': 'vp9', 'acodec': 'opus', 'height': 720},
        ]}, match=match, source_url=SOURCE)
        row = result.metadata['upstream_candidates'][1]
        self.assert_name_and_authority(row, TITLE, 'webm')
        self.assertEqual(result.metadata['description'], description)


class NativeSharedWorkTitleTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('unexpected network')))
        self.enterContext(patch('socket.getaddrinfo', side_effect=AssertionError('unexpected DNS')))

    def parse_note(self, title, description):
        note_id = '64a1b2c3d4e5f6789012abcd'
        note = {'noteId': note_id, 'type': 'video', 'title': title, 'desc': description,
                'user': {'nickname': 'Alice'},
                'video': {'media': {'stream': {'h264': [{
                    'masterUrl': 'https://sns-video-bd.xhscdn.com/stream/fixture.mp4', 'size': 32,
                }]}}}}
        payload = {'note': {'noteDetailMap': {note_id: {'note': note}}}}
        html = '<script>window.__INITIAL_STATE__=' + json.dumps(payload, ensure_ascii=False) + ';</script>'
        return XiaohongshuAdapter().parse_html(html, source_url='https://www.xiaohongshu.com/explore/' + note_id)

    def assert_native_title(self, result, expected):
        from .views import build_result_display_context, single_attachment_download_filename
        self.assertEqual(result.metadata['title'], expected[:200])
        self.assertEqual(result.base_filename, ('Alice_' + expected[:200])[:180])
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        self.assertEqual(context['base_filename'], result.base_filename)
        item = context['video_items'][0]
        self.assertEqual(single_attachment_download_filename(item, context['base_filename'],
            final_url=item['url'], content_type='video/mp4'), result.base_filename + '.mp4')
        self.assertEqual(item['url'], 'https://sns-video-bd.xhscdn.com/stream/fixture.mp4')

    def test_native_explicit_title_and_distinct_body_are_not_reinterpreted(self):
        description = f'独立正文\n{BODY}\n#摄影 #作品'
        for title in ('C# Basics (Part 1) + .NET', '#1 Release (2026) + C#', CAPTION,
                      ('A long explicit English title with C# and punctuation (Part 1) ' * 4).strip()):
            with self.subTest(title=title):
                result = self.parse_note(title, description)
                self.assert_native_title(result, title[:200].strip())
                self.assertEqual(result.metadata['description'], description)
                self.assertEqual(result.metadata['caption'], (title + '\n' + description)[:2000])

    def test_native_description_only_derives_short_title_before_body_limits(self):
        description = f'{TITLE}\n{BODY}\n#摄影 #作品'
        result = self.parse_note('', description)
        self.assert_native_title(result, TITLE)
        self.assertEqual(result.metadata['description'], description)
        self.assertEqual(result.metadata['caption'], description)
        self.assertEqual(result.metadata['topics'], ['摄影', '作品'])

    def test_native_raw_body_and_topics_remain_bounded(self):
        description = f'{TITLE}\n{BODY * 20}\n' + ' '.join('#话题' + str(index) for index in range(100))
        result = self.parse_note('', description)
        self.assert_native_title(result, TITLE)
        self.assertEqual(result.metadata['description'], description[:2000])
        self.assertEqual(result.metadata['caption'], description[:2000])
        self.assertEqual(result.metadata['topics'], ['话题' + str(index) for index in range(16)])

    def test_native_topics_only_body_survives_without_a_derived_title(self):
        result = self.parse_note('', '#摄影 #日常')
        self.assertTrue(result.video_assets)
        self.assertNotIn('title', result.metadata)
        self.assertEqual(result.metadata['description'], '#摄影 #日常')
        self.assertEqual(result.metadata['caption'], '#摄影 #日常')
        self.assertEqual(result.metadata['topics'], ['摄影', '日常'])

    def test_native_numeric_topics_keep_body_and_ordinary_numbered_title(self):
        description = f'{TITLE}\n{BODY}\n#00后 #3分钟学会拍照'
        result = self.parse_note('', description)
        self.assert_native_title(result, TITLE)
        self.assertEqual(result.metadata['description'], description)
        self.assertEqual(result.metadata['caption'], description)
        self.assertEqual(result.metadata['topics'], ['00后', '3分钟学会拍照'])
        title = '#1 Release (2026) + C#'
        for body in ('', description):
            with self.subTest(body=body):
                result = self.parse_note(title, body)
                self.assert_native_title(result, title)
                self.assertEqual(result.metadata.get('description', ''), body)
                self.assertEqual(result.metadata['caption'], '\n'.join(
                    part for part in (title, body) if part))
