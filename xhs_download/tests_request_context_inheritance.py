"""Work/format context through normalization, private issuance and public actions."""
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
from .services.download_routing import public_route_item
from .services.provider_strategy import media_request_context
from .services.platforms.ytdlp_generic import YtDlpResultNormalizer
from .services.quality import native_attachment_expiry
from .services.request_context import request_context_fingerprint
from .services.upstream_candidates import resolve_candidate_record
from .services.upstream_delivery import normalize_upstream_result, upstream_resource_context, upstream_resource_record
from .services.ytdlp_selection import capability


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, YTDLP_PROVIDER_ENABLED=True,
)
class RequestContextInheritanceTests(SimpleTestCase):
    media_url = 'https://cdn.example.org/complete.mp4'
    source_url = 'https://site.example.org/watch/one'
    secrets = ('private-cookie', 'private-bearer', 'private-key', 'private-referrer')

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.enterContext(patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('unexpected network')))
        self.match = {'provider_id': 'yt_dlp_generic', 'extractor_id': 'Fixture',
                      'version': 'offline', 'label': 'Fixture'}
        rule = SimpleNamespace(platform_id='upstream', risk_level='low', staff_default=True,
                               provider_ids=('yt_dlp_generic',), upstream_matches=(self.match,))
        runtime = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.PUBLIC,
                                        third_party_allowed_public_platforms='upstream:yt_dlp_generic')
        self.enterContext(patch('xhs_download.services.provider_access.load_runtime_setting', return_value=runtime))
        self.enterContext(patch('xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url',
                                return_value=rule))
        self.enterContext(patch('xhs_download.views.get_download_runtime_setting',
                                return_value=DownloadRuntimeSetting(visitor_video_download_enabled=True)))
        self.media_requests = []

        def media_http(request, **kwargs):
            self.assertEqual(request.url, self.media_url)
            self.assertEqual(request.method, 'HEAD')
            self.media_requests.append(request)
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers.update({'Content-Type': 'video/mp4', 'Content-Length': '32',
                                     'Content-Disposition': 'attachment; filename="12345.mp4"'})
            response.raw = BytesIO(b'')
            return response

        self.enterContext(patch('requests.adapters.HTTPAdapter.send', side_effect=media_http))

    def normalize(self, parent=None, child=None):
        video = {'format_id': 'complete', 'url': self.media_url, 'ext': 'mp4', 'protocol': 'https',
                 'vcodec': 'h264', 'acodec': 'aac', 'height': 720, 'filesize': 32,
                 **(child or {})}
        payload = {'id': 'work-one', 'title': 'Context fixture', 'duration': 1,
                   **(parent or {}), 'formats': [video]}
        before = deepcopy(payload)
        result = normalize_upstream_result(payload, match=self.match, source_url=self.source_url)
        self.assertEqual(payload, before)
        candidate = result.metadata['upstream_candidates'][0]
        record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        self.assertEqual(record['url'], self.media_url)
        self.assertEqual(record['public']['format_id'], 'complete')
        self.assertEqual(len(result.assets), 1)
        return result, candidate, record

    def post(self, candidate, action='download'):
        return self.client.post(reverse('ytdlp_selected_format'), {
            'candidate_id': candidate['candidate_id'], 'resource_token': candidate['resource_token'],
            'action': action,
        }, content_type='application/json')

    def assert_private(self, result, *responses):
        public = json.dumps([result.metadata, public_route_item(result.assets[0].to_download_item()),
                             *(response.json() for response in responses)])
        for secret in self.secrets:
            self.assertNotIn(secret, public)
        for field in ('http_headers', 'required_headers', 'Authorization', 'Cookie'):
            self.assertNotIn('"' + field + '"', public)

    def assert_denied(self, result, candidate):
        before = len(self.media_requests)
        responses = [self.post(candidate, action) for action in ('download', 'preview', 'open')]
        self.assertEqual([response.status_code for response in responses], [409, 409, 409])
        self.assertEqual(len(self.media_requests), before, 'required context must stop before probing')
        self.assertFalse(capability(candidate)['can_download'])
        self.assertFalse(public_route_item(result.assets[0].to_download_item())['browser_direct_fetch_safe'])
        self.assert_private(result, *responses)

    def test_work_and_format_required_referer_have_the_same_action_restrictions(self):
        headers = {'Referer': 'https://site.example.org/private-referrer'}
        for on_parent in (True, False):
            with self.subTest(on_parent=on_parent):
                context = {'http_headers': headers, 'required_headers': ['Referer']}
                result, candidate, record = self.normalize(context if on_parent else {},
                                                          {} if on_parent else context)
                self.assert_denied(result, candidate)
                self.assertEqual([name.lower() for name in record['required_headers']], ['referer'])

    def test_format_headers_override_by_name_without_losing_parent_alias_headers(self):
        for parent_key in ('headers', 'http_headers'):
            for child_key in ('headers', 'http_headers'):
                with self.subTest(parent=parent_key, child=child_key):
                    parent = {parent_key: {'Cookie': 'private-cookie', 'Authorization': 'private-bearer',
                                          'Referer': 'https://site.example.org/old', 'Accept': '*/*',
                                          'X-Key': 'private-key'}, 'required_headers': ['Referer', 'X-Key']}
                    child = {child_key: {'rEfErEr': 'https://site.example.org/private-referrer',
                                         'User-Agent': 'format-agent'}, 'requires_cookie': False,
                             'required_headers': ['referer', 'User-Agent']}
                    result, candidate, record = self.normalize(parent, child)
                    self.assert_denied(result, candidate)
                    expected = {'cookie': 'private-cookie', 'authorization': 'private-bearer',
                                'referer': 'https://site.example.org/private-referrer', 'accept': '*/*',
                                'x-key': 'private-key', 'user-agent': 'format-agent'}
                    for headers in (record['headers'], result.assets[0].headers,
                                    upstream_resource_context(result.assets[0].to_download_item())['headers']):
                        self.assertEqual({key.lower(): value for key, value in headers.items()}, expected)
                        self.assertEqual(len(headers), len(expected))
                    self.assertEqual({name.lower() for name in record['required_headers']},
                                     {'referer', 'user-agent', 'x-key'})
                    self.assertTrue(record['requires_cookie'])

    def test_parent_credentials_and_custom_headers_survive_a_default_only_format(self):
        for header in ('Cookie', 'Authorization', 'X-Key'):
            with self.subTest(header=header):
                result, candidate, record = self.normalize({'headers': {header: 'private-key'}},
                                                          {'http_headers': {'User-Agent': 'format-agent'}})
                self.assert_denied(result, candidate)
                self.assertEqual(record['headers'][header], 'private-key')

    def test_both_aliases_merge_at_each_level_and_formats_do_not_mutate_work_headers(self):
        parent = {'headers': {'Cookie': 'private-cookie', 'User-Agent': 'work-generic'},
                  'http_headers': {'user-agent': 'work-http', 'Accept': '*/*'}}
        child = {'headers': {'USER-AGENT': 'format-generic', 'Referer': self.source_url},
                 'http_headers': {'user-Agent': 'format-http'}}
        result, candidate, record = self.normalize(parent, child)
        self.assert_denied(result, candidate)
        expected = {'cookie': 'private-cookie', 'user-agent': 'format-http',
                    'accept': '*/*', 'referer': self.source_url}
        self.assertEqual({key.lower(): value for key, value in record['headers'].items()}, expected)
        self.assertEqual(len(record['headers']), len(expected))

    def test_ordinary_rule_normalizer_uses_the_same_work_to_format_context(self):
        rule = SimpleNamespace(platform_id='fixture', media_host_suffixes=('cdn.example.org',))
        payload = {'title': 'Ordinary fixture', 'headers': {'Cookie': 'private-cookie', 'Accept': '*/*'},
                   'required_headers': ['Referer'], 'requires_context': True,
                   'formats': [{'format_id': 'complete', 'url': self.media_url, 'ext': 'mp4',
                                'protocol': 'https', 'vcodec': 'h264', 'acodec': 'aac', 'filesize': 32,
                                'http_headers': {'User-Agent': 'format-agent'},
                                'required_headers': ['User-Agent'], 'requires_context': False}]}
        before = deepcopy(payload)
        result = YtDlpResultNormalizer().normalize(rule=rule, platform_name='Fixture',
                                                  source_url=self.source_url, payload=payload)
        self.assertEqual(payload, before)
        asset = result.assets[0]
        self.assertEqual({key.lower(): value for key, value in asset.headers.items()},
                         {'cookie': 'private-cookie', 'accept': '*/*', 'user-agent': 'format-agent'})
        self.assertIs(asset.request_context['requires_context'], True)
        self.assertEqual(set(asset.request_context['required_headers']), {'referer', 'user-agent'})

    def test_parent_true_constraints_survive_missing_null_and_default_false_children(self):
        for key in ('requires_cookie', 'requires_context', 'ip_bound'):
            for child_value in ({}, {key: None}, {key: False}):
                with self.subTest(key=key, child=child_value):
                    result, candidate, record = self.normalize({key: True}, {
                        'http_headers': {'User-Agent': 'default-agent'}, **child_value})
                    self.assert_denied(result, candidate)
                    self.assertIs(record[key], True)
                    self.assertIs(result.assets[0].request_context[key], True)

    def test_unknown_work_flags_are_not_filled_with_format_default_false(self):
        keys = ('requires_cookie', 'requires_context', 'ip_bound')
        result, candidate, record = self.normalize(dict.fromkeys(keys), dict.fromkeys(keys, False))
        response = self.post(candidate)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['request_context_status'], 'no_known_requirement')
        self.assertIsNone(response.json()['item']['requires_cookie'])
        for key in keys:
            self.assertIsNone(record[key])
            self.assertIsNone(result.assets[0].request_context[key])

    def test_opaque_or_malformed_parent_context_cannot_be_erased_by_default_headers(self):
        for parent in ({'headers_present': True}, {'headers': 'unavailable'},
                       {'required_headers': {'Referer': True}}, {'required_headers': ['Referer', None]}):
            with self.subTest(parent=parent):
                result, candidate, _ = self.normalize(parent, {'http_headers': {'User-Agent': 'format-agent'},
                                                               'headers_present': False, 'required_headers': []})
                self.assert_denied(result, candidate)

    def test_default_headers_remain_optional_and_unknown_cookie_stays_unknown(self):
        result, candidate, record = self.normalize({
            'headers': {'Accept': '*/*', 'Referer': 'https://site.example.org/private-referrer'},
            'requires_cookie': None, 'requires_context': None, 'ip_bound': None,
        }, {'http_headers': {'User-Agent': 'format-agent'}})
        response = self.post(candidate)
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['download_strategy'], 'browser_native')
        self.assertEqual(item['request_context_status'], 'no_known_requirement')
        self.assertIsNone(item['requires_cookie'])
        self.assertIsNone(record['requires_cookie'])
        self.assertIsNone(record.get('requires_context'))
        self.assertIsNone(record.get('ip_bound'))
        expected = {'accept': '*/*', 'referer': 'https://site.example.org/private-referrer',
                    'user-agent': 'format-agent'}
        self.assertEqual({key.lower(): value for key, value in record['headers'].items()}, expected)
        self.assertEqual(len(self.media_requests), 1)
        self.assertEqual(self.media_requests[0].headers['Referer'], expected['referer'])
        self.assertEqual(item['selection_expires_at'], int(record['expires_at'] * 1000))
        self.assert_private(result, response)

    def test_ordinary_resource_record_keeps_private_admission_facts_without_renewal(self):
        result, _, _ = self.normalize({'headers': {'User-Agent': 'work-agent'}})
        item = result.assets[0].to_download_item()
        record = upstream_resource_record(item)
        self.assertEqual(record['source_url'], self.source_url)
        self.assertEqual(record['match'], self.match)
        original_expiry = record['expires_at']
        self.assertEqual(upstream_resource_record(item)['expires_at'], original_expiry)
        public = public_route_item(item)
        self.assertNotIn('source_url', public)
        self.assertNotIn('match', public)
        self.assertIsNone(upstream_resource_record({**item, 'url': 'https://cdn.example.org/other.mp4'}))
        with patch('xhs_download.services.upstream_delivery.time', return_value=original_expiry):
            self.assertIsNone(upstream_resource_record(item))

    def test_positive_evidence_from_default_context_cannot_authorize_inherited_requirements(self):
        _, candidate, default_record = self.normalize({'http_headers': {'Referer': self.source_url}})
        self.assertEqual(self.post(candidate).json()['item']['download_strategy'], 'browser_native')
        self.assertEqual(len(self.media_requests), 1)
        result, restricted, record = self.normalize({
            'http_headers': {'Referer': self.source_url}, 'required_headers': ['Referer']},
            {'http_headers': {'User-Agent': 'format-agent'}})
        self.assert_denied(result, restricted)
        self.assertNotEqual(request_context_fingerprint(default_record['headers'], **media_request_context(default_record)),
                            request_context_fingerprint(record['headers'], **media_request_context(record)))
        self.assertEqual(native_attachment_expiry(record['url'], record['headers'], **media_request_context(record)), 0)
        self.assertEqual(len(self.media_requests), 1)
