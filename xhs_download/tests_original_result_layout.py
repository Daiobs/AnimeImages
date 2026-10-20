"""Ordinary result layout consumes real dedicated-provider output, offline."""
from bs4 import BeautifulSoup
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from . import tests_douyin_format_selection as fixtures
from .views import build_result_display_context

BASE = fixtures.BASE


class OriginalResultLayoutTests(SimpleTestCase):
    def setUp(self):
        self.case = fixtures.DouyinFormatSelectionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        payload = self.case.session.post.return_value.json.return_value['data']
        payload['title'] = '真实作品标题 #图文与视频'
        payload['videos'] = [{'title': 'Video', 'streams': [
            {'url': BASE + '1080.mp4', 'qualityLabel': '1080p', 'sizeInBytes': 2800000},
            {'url': BASE + 'original.mp4', 'qualityLabel': '原画', 'sizeInBytes': 58000000},
            {'url': BASE + '1080.mp4', 'qualityLabel': '1080P'},
            {'url': BASE + '720.mp4', 'qualityLabel': '720p', 'sizeInBytes': 2000000},
            {'url': BASE + '540.mp4', 'qualityLabel': '540p', 'sizeInBytes': 2300000},
        ]}]

    def context(self):
        result = self.case.result()
        return result, build_result_display_context(result, probe_missing_video_sizes=False)

    def test_real_provider_title_raw_evidence_and_original_header(self):
        result, context = self.context()
        self.assertEqual(result.metadata['title'], '真实作品标题 #图文与视频')
        self.assertEqual(len(result.metadata['upstream_candidates']), 5)
        self.assertEqual(len(result.video_assets), 1)
        self.assertEqual(result.video_assets[0].url, BASE + 'original.mp4')
        self.case.session.post.assert_called_once()
        for staff in (False, True):
            for page in ('home', 'douyin', 'xiaohongshu', 'bilibili'):
                with self.subTest(staff=staff, page=page):
                    html = render_to_string('partials/xhs_results_region.html', {
                        **context, 'show_internal_diagnostics': staff, 'result_theme': page,
                    })
                    soup = BeautifulSoup(html, 'html.parser')
                    header = soup.select_one('.result-card-header')
                    self.assertIn('真实作品标题', header.get_text())
                    self.assertNotIn('#图文与视频', header.get_text())
                    self.assertEqual(soup.select_one('.result-caption-topics li').get_text(), '#图文与视频')
                    self.assertIn('抖音', header.get_text())
                    self.assertIn('1 个视频资源', header.get_text())
                    self.assertIsNotNone(header.select_one('#download-btn'))
                    self.assertIsNotNone(header.select_one('#select-all-btn'))
                    self.assertIsNotNone(soup.select_one('.quality-preview-tile [data-picker-cover]'))
                    self.assertIsNone(soup.select_one('.ytdlp-work'))
                    self.assertNotIn('<h3>Video</h3>', html)
                    self.assert_choices(soup, staff=staff)
                    self.assert_absent_controls(html)

    def assert_choices(self, soup, *, staff):
        choices = soup.select('[data-picker-group="video"] [data-picker-radio]')
        self.assertEqual([row['data-label'] for row in choices], ['原画', '1080p', '720p', '540p'])
        self.assertEqual(choices[1]['data-size-label'], '2.7 MiB')
        self.assertTrue(choices[0].has_attr('checked'))
        self.assertTrue(all(not row.find_parent('details') for row in choices))
        self.assertEqual(len(soup.select('[data-upstream-candidate]')), 5 if staff else 0)
        self.assertEqual(bool(soup.select_one('.ytdlp-technical')), staff)
        self.assertEqual(bool(soup.select_one('[data-upstream-candidates]')), staff)
        self.assertEqual(len({row['data-legacy-asset-id'] for row in choices}), 4)

    def assert_absent_controls(self, html):
        for label in ('原有方式保存所选资源', '复制所选链接', '不需要选择解析通道'):
            self.assertNotIn(label, html)
        self.assertNotIn('data-ytdlp-action="legacy"', html)
        self.assertNotIn('data-ytdlp-action="copy"', html)

    def test_batch_keeps_original_metadata_and_all_format_choices(self):
        result, context = self.context()
        from .services.batch_parse.results import enrich_batch_media_items
        context.update(status='success', index=0, source_text='https://www.douyin.com/video/fixture')
        enrich_batch_media_items(context)
        for staff in (False, True):
            html = render_to_string('partials/batch_result_card.html', {
                'row': context, 'show_internal_diagnostics': staff,
                'batch_download_allowed': True, 'batch_raw_links_allowed': staff,
            })
            soup = BeautifulSoup(html, 'html.parser')
            self.assertEqual(soup.select_one('.batch-result-title').get_text(), '真实作品标题')
            self.assertEqual(soup.select_one('.result-caption-topics li').get_text(), '#图文与视频')
            self.assertIsNotNone(soup.select_one('[data-picker-download-all]'))
            self.assert_choices(soup, staff=staff)
            self.assert_absent_controls(html)

    def test_candidate_only_legacy_cards_remain_visible_without_picker(self):
        for provider in ('yt_dlp_generic', 'tiantianjiexi', 'gallery_dl_generic'):
            context = {
                'status': 'success', 'image_items': [], 'video_items': [],
                'base_filename': 'legacy-result',
                'upstream_candidates': [{
                    'candidate_id': 'legacy-candidate', 'resource_token': 'opaque-token',
                    'provider_id': provider, 'media_type': 'video', 'format_label': '1080p',
                }],
            }
            for staff in (False, True):
                for template, values in (
                    ('partials/xhs_results_region.html', context),
                    ('partials/batch_result_card.html', {'row': context}),
                ):
                    with self.subTest(provider=provider, staff=staff, template=template):
                        html = render_to_string(template, {**values, 'show_internal_diagnostics': staff})
                        soup = BeautifulSoup(html, 'html.parser')
                        self.assertIsNone(soup.select_one('[data-ytdlp-picker]'))
                        self.assertIsNone(soup.select_one('.ytdlp-technical'))
                        details = soup.select_one('[data-upstream-candidates]')
                        self.assertIsNotNone(details)
                        self.assertTrue(details.has_attr('open'))
                        self.assertEqual(len(details.select('[data-upstream-candidate]')), 1)
                        for action in ('copy', 'open'):
                            button = details.select_one(f'[data-candidate-action="{action}"]')
                            self.assertIsNotNone(button)
                            self.assertFalse(button.has_attr('disabled'))

    def test_cached_candidate_placeholder_cannot_hide_existing_result_title(self):
        _, context = self.context()
        for candidate in context['upstream_candidates']:
            candidate['work'] = {**candidate['work'], 'title': 'Video'}
            candidate['work_label'] = 'Video'
        html = render_to_string('partials/xhs_results_region.html', context)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertIn('真实作品标题', soup.select_one('.result-title').get_text())
        self.assertNotIn('#图文与视频', soup.select_one('.result-title').get_text())
        self.assertEqual(soup.select_one('.result-caption-topics li').get_text(), '#图文与视频')
        self.assertIsNone(soup.select_one('.ytdlp-work'))
        self.assertEqual(len(soup.select('[data-picker-radio]')), 4)

    def test_bound_legacy_cover_keeps_original_preview_authorization(self):
        _, context = self.context()
        legacy = context['video_items'][0]
        context['proxy_image_url'] = '/media/proxy-image/'
        expected_src = '/media/proxy-image/?u=' + (legacy.get('encoded_preview_cover_url') or legacy['encoded_cover_url'])
        if legacy.get('cover_preview_provider_query'):
            expected_src += '&' + legacy['cover_preview_provider_query']
        for candidate in context['upstream_candidates']:
            candidate['work'] = {**candidate.get('work', {}), 'cover_available': False}
        for template, values in (
            ('partials/xhs_results_region.html', context),
            ('partials/batch_result_card.html', {'row': {**context, 'status': 'success'},
                                                'proxy_image_url': context['proxy_image_url']}),
        ):
            html = render_to_string(template, values)
            cover = BeautifulSoup(html, 'html.parser').select_one('[data-picker-legacy-cover]')
            self.assertIsNotNone(cover)
            self.assertEqual(cover['src'], expected_src)
            self.assertFalse(cover.has_attr('hidden'))
            self.assertFalse(cover.has_attr('data-cover-available'))

    def test_candidate_filename_uses_same_work_title_not_nested_video_placeholder(self):
        result, _ = self.context()
        from .services.service_format_selection import selected_descriptor
        row = next(row for row in result.metadata['upstream_candidates'] if row['format_label'] == '720p')
        record = self.case.record(row)
        descriptor = selected_descriptor(record)
        self.assertTrue(descriptor['filename'].startswith('真实作品标题'))
        self.assertEqual(descriptor['url'], BASE + '720.mp4')
        self.assertNotEqual(descriptor['asset_id'], result.video_assets[0].asset_id)

    def test_multiple_bound_assets_still_count_one_video_plus_unrepresented_resources(self):
        _, context = self.context()
        original = context['video_items'][0]
        asset_ids = dict.fromkeys(row['legacy_asset_id'] for row in context['upstream_candidates'])
        context['video_items'] = [{**original, 'asset_id': identity} for identity in asset_ids]
        for extra_count in (0, 1):
            if extra_count:
                context['video_items'].append({**original, 'asset_id': 'unrepresented-video'})
            html = render_to_string('partials/xhs_results_region.html', context)
            header = BeautifulSoup(html, 'html.parser').select_one('.result-card-header').get_text()
            self.assertIn(f'{1 + extra_count} 个视频资源', header)
            html = render_to_string('partials/batch_result_card.html', {'row': {**context, 'status': 'success'}})
            meta = BeautifulSoup(html, 'html.parser').select_one('.batch-meta-row').get_text()
            self.assertIn(f'视频 {1 + extra_count}', meta)
