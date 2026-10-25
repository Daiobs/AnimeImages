"""INV-03/06/07: resource aliases and controlled names through public HTTP."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
import requests

from contracts.network import offline_requests
from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting
from xhs_download.services.upstream_candidates import resolve_candidate_record


SOURCE = 'https://www.instagram.com/p/presentation-contract/'
SERVICE = 'https://provider.contract.invalid/presentation'
FIRST = 'https://media.cdninstagram.com/first.mp4'
SECOND = 'https://media.cdninstagram.com/second.webm'


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    KUKUTOOL_GENERIC_PROVIDER_ENABLED=False,
    THIRD_PARTY_PROVIDER_ORDER_DEFAULT=('tiantianjiexi_generic',),
    TIANTIANJIEXI_PHONE='presentation-contract', TIANTIANJIEXI_PASSWORD='contract',
    TIANTIANJIEXI_API_BASE_URL=SERVICE, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0, THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=10000,
)
class SelectedPresentationContracts(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        ProviderRuntimeSetting.objects.create(pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms='instagram', third_party_blocked_platforms='')
        self.calls = []
        self.disposition = 'inline'
        self.stack.enter_context(offline_requests(self.send))
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch('xhs_download.services.platforms.tiantian_serial.lock_root', return_value=root))

    def send(self, request, **options):
        self.calls.append((request.method, request.url))
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        response._content_consumed = True
        response._content = b''
        if request.url == SERVICE:
            self.assertEqual(request.method, 'POST')
            response.headers['Content-Type'] = 'application/json'
            response._content = json.dumps({'code': 200, 'data': {
                'title': 'Window light', 'description': 'Keep the full description. #photo #video',
                'videos': [{'streams': [
                    {'url': FIRST, 'qualityLabel': '720p'},
                    {'url': FIRST, 'qualityLabel': '720P', 'sizeInBytes': 12345},
                    {'url': SECOND, 'qualityLabel': '720P', 'sizeInBytes': 23456},
                ]}],
            }}).encode()
        else:
            self.assertIn(request.url, (FIRST, SECOND))
            self.assertEqual(request.method, 'HEAD')
            extension = 'mp4' if request.url == FIRST else 'webm'
            response.headers.update({'Content-Type': 'video/' + extension,
                                     'Content-Length': '12345' if request.url == FIRST else '23456',
                                     'Content-Disposition': self.disposition})
        return response

    def parse(self):
        response = self.client.get(reverse('results'), {'q': SOURCE})
        self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertContains(response, 'Keep the full description.')
        rows = sorted(response.context['upstream_candidates'], key=lambda row: row['ordinal'])
        radios = BeautifulSoup(response.content, 'html.parser').select('[data-picker-radio]')
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(radios), 2)
        urls = {row['candidate_id']: resolve_candidate_record(row['resource_token'], row['candidate_id'])['url']
                for row in rows}
        visible = {radio['value'] for radio in radios}
        self.assertEqual({urls[identity] for identity in visible}, {FIRST, SECOND})
        self.assertEqual(len(visible & {identity for identity, url in urls.items() if url == FIRST}), 1)
        for radio in radios:
            row = next(row for row in rows if row['candidate_id'] == radio['value'])
            self.assertEqual(radio['data-token'], row['resource_token'])
        self.assertEqual(self.calls, [('POST', SERVICE)])
        return rows, radios

    def action(self, row, action):
        response = self.client.post(reverse('service_selected_format'), {
            'candidate_id': row['candidate_id'], 'resource_token': row['resource_token'], 'action': action,
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['item']

    def test_inv03_inv06_aliases_preserve_raw_authority_and_readable_selected_names(self):
        rows, _ = self.parse()
        for row in rows:
            before = deepcopy(resolve_candidate_record(row['resource_token'], row['candidate_id']))
            url = before['url']
            filename = 'Window light_2.mp4' if url == FIRST else 'Window light.webm'
            link = self.action(row, 'link')
            item = self.action(row, 'download')
            self.assertEqual(link['filename'], filename)
            self.assertEqual(item['filename'], filename)
            self.assertNotIn(row['candidate_id'], filename)
            self.assertEqual(item['url'], url)
            self.assertEqual(item['candidate_id'], row['candidate_id'])
            self.assertEqual(item['selected_format_id'], row['format_id'])
            self.assertEqual(item['selection_expires_at'], int(before['expires_at'] * 1000))
            self.assertEqual(item['download_strategy'], 'browser_streaming')
            self.assertEqual(resolve_candidate_record(row['resource_token'], row['candidate_id']), before)
        self.assertEqual(self.calls, [('POST', SERVICE), ('HEAD', SECOND), ('HEAD', FIRST)])

    def test_inv06_native_source_filename_still_qualifies_without_title_override(self):
        self.disposition = 'attachment; filename="12345.mp4"'
        rows, _ = self.parse()
        row = next(row for row in rows
                   if resolve_candidate_record(row['resource_token'], row['candidate_id'])['url'] == FIRST)
        item = self.action(row, 'download')
        self.assertEqual(item['download_strategy'], 'browser_native')
        self.assertEqual(item['url'], FIRST)
        self.assertEqual(item['filename'], 'Window light_2.mp4')
        self.assertGreater(item['native_download_expires_at'], 0)
        self.assertEqual(self.calls, [('POST', SERVICE), ('HEAD', FIRST)])
