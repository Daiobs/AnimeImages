"""Public parse/action contracts for already-admitted complete video files."""
from contextlib import ExitStack
from pathlib import Path
import json
import subprocess
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
import requests

from contracts.network import offline_requests
from xhs_download.models import ProviderAccessMode, ProviderRuntimeSetting
from xhs_download.models import DownloadAuditEvent, DownloadRequestLog


SOURCE = 'https://www.instagram.com/p/contract-video/'
MEDIA = 'https://media.cdninstagram.com/contract-video.mp4'
SECOND = 'https://media.fbcdn.net/contract-alternative.mp4'
SERVICE = 'https://provider.contract.invalid/parse'


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    KUKUTOOL_GENERIC_PROVIDER_ENABLED=False,
    THIRD_PARTY_PROVIDER_ORDER_DEFAULT=('tiantianjiexi_generic',),
    TIANTIANJIEXI_PHONE='contract', TIANTIANJIEXI_PASSWORD='contract',
    TIANTIANJIEXI_API_BASE_URL=SERVICE, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0, THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=10000,
)
class GenericBrowserDeliveryContracts(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.runtime = ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms='instagram', third_party_blocked_platforms='')
        self.disposition = 'attachment; filename="12345.mp4"'
        self.required = {}
        self.media_requests = []
        self.media_redirect = ''
        self.provider_calls = 0
        self.stack.enter_context(offline_requests(self.send))
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch('xhs_download.services.platforms.tiantian_serial.lock_root', return_value=root))

    def send(self, request, **options):
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        response._content_consumed = True
        if request.url == SERVICE:
            self.assertEqual(request.method, 'POST')
            self.provider_calls += 1
            response.headers['Content-Type'] = 'application/json'
            response._content = json.dumps({'code': 200, 'data': {
                'title': 'Public file contract',
                'http_headers': {'User-Agent': 'ordinary-default', 'Accept': '*/*',
                                 'Referer': 'https://www.instagram.com/'},
                **self.required,
                'videos': [{'streams': [
                    {'url': MEDIA, 'qualityLabel': '1080P', 'sizeInBytes': 23456},
                    {'url': SECOND, 'qualityLabel': '720P', 'sizeInBytes': 12345},
                ]}],
            }}).encode()
            return response
        if request.url in {MEDIA, SECOND}:
            self.media_requests.append((request.method, request.url, dict(request.headers)))
            if request.url == SECOND and self.media_redirect:
                response.status_code = 302
                response.headers['Location'] = self.media_redirect
                response._content = b''
                return response
            response.headers.update({'Content-Type': 'video/mp4', 'Content-Length': '12345',
                                     'Content-Disposition': self.disposition})
            # No Access-Control-Allow-Origin: native navigation is not fetch.
            response._content = b''
            return response
        raise AssertionError(f'Unexpected outbound request: {request.method} {urlsplit(request.url).hostname}')

    def parse(self):
        response = self.client.get(reverse('results'), {'q': SOURCE})
        self.assertEqual(response.status_code, 200, response.content[:1000])
        self.assertContains(response, 'Public file contract')
        radios = BeautifulSoup(response.content, 'html.parser').select('[data-picker-radio]')
        self.assertEqual(len(radios), 2, response.content[:2000])
        self.assertEqual(self.provider_calls, 1)
        return radios

    def action(self, radio, **changes):
        return self.client.post(reverse('service_selected_format'), {
            'candidate_id': radio['value'], 'resource_token': radio['data-token'],
            'action': 'download', **changes,
        }, content_type='application/json')

    def validate_browser_action(self, radio, item):
        """Consume the actual HTTP descriptor, not a parallel ideal JS fixture."""
        selection = dict(id=radio['value'], kind='video', schema='service_selected_format_v1',
                         provider='tiantianjiexi_generic')
        script = """
const fs = require('node:fs');
const picker = require('./static/js/ytdlp_result_picker.js');
const protocol = require('./static/js/ordinary_media_streaming_protocol.js');
const {item, selection} = JSON.parse(fs.readFileSync(0, 'utf8'));
picker.validateItem(item, selection);
if (item.download_strategy === 'browser_streaming') {
  const transfer = protocol.createTransportDescriptor({...item, downloadId:'1234567890abcdef1234567890abcdef',
    expectedBytes:item.size_bytes});
  if (transfer.url !== item.url) throw new Error('selected URL changed');
}
"""
        checked = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                 input=json.dumps(dict(item=item, selection=selection)),
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_inv03_normal_default_headers_issue_native_evidence_for_nondefault_outside_sw_list(self):
        radios = self.parse()
        self.assertEqual(self.media_requests, [])
        response = self.action(radios[1])
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['url'], SECOND)
        self.assertEqual(item['candidate_id'], radios[1]['value'])
        self.assertEqual(item['download_strategy'], 'browser_native')
        self.validate_browser_action(radios[1], item)
        self.assertGreater(item['native_download_expires_at'], time.time() * 1000)
        self.assertEqual([row[:2] for row in self.media_requests], [('HEAD', SECOND)])
        self.assertNotIn('headers', item)
        self.assertNotIn('http_headers', item)
        again = self.action(radios[1])
        self.assertEqual(again.json()['item']['download_strategy'], 'browser_native')
        self.assertEqual(len(self.media_requests), 1)
        self.assertEqual(self.provider_calls, 1)

    def test_inv03_same_outside_resource_without_attachment_uses_shared_sw(self):
        self.disposition = 'inline'
        radio = self.parse()[1]
        response = self.action(radio)
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['url'], SECOND)
        self.assertEqual(item['download_strategy'], 'browser_streaming')
        self.validate_browser_action(radio, item)
        self.assertNotIn('native_download_expires_at', item)
        self.assertEqual(len(self.media_requests), 1)

    def test_inv03_required_context_is_not_default_headers_or_a_browser_credential(self):
        self.required = {'required_headers': ['Referer']}
        response = self.action(self.parse()[1])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self.media_requests, [])

    def test_inv03_redirect_scope_comes_from_final_authorized_probe_not_a_cdn_family(self):
        self.media_redirect = MEDIA
        self.disposition = 'inline'
        radio = self.parse()[1]
        response = self.action(radio)
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['url'], SECOND)
        self.assertEqual(item['download_strategy'], 'browser_streaming')
        self.assertEqual(item['browser_response_hosts'], ['media.fbcdn.net', 'media.cdninstagram.com'])
        self.validate_browser_action(radio, item)
        self.assertLessEqual(item['browser_response_hosts_expires_at'], item['selection_expires_at'])
        self.assertEqual([row[:2] for row in self.media_requests], [('HEAD', SECOND), ('HEAD', MEDIA)])
        self.assertEqual(self.provider_calls, 1)

    def test_inv03_action_cannot_change_resource_or_borrow_authority(self):
        radios = self.parse()
        self.assertEqual(self.action(radios[1], url=MEDIA).status_code, 400)
        self.assertEqual(self.action(radios[1], resource_token=radios[0]['data-token']).status_code, 403)
        self.runtime.third_party_allowed_public_platforms = ''
        self.runtime.save()
        self.assertEqual(self.action(radios[1]).status_code, 403)
        self.assertEqual(self.media_requests, [])

    def test_inv06_unknown_extended_charset_does_not_borrow_fallback_filename(self):
        self.disposition = "attachment; filename=ok.mp4; filename*=X-UNKNOWN''wrong.bin"
        response = self.action(self.parse()[1])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['download_strategy'], 'browser_streaming')

    def test_inv04_authorized_selection_issues_observation_receipt_not_saved_success(self):
        radio = self.parse()[1]
        item = self.action(radio).json()['item']
        audit = item['download_audit']
        prepared = DownloadAuditEvent.objects.get(attempt_id=audit['attempt_id'])
        self.assertEqual((prepared.phase, prepared.status, prepared.actual_bytes), ('prepared', 'pending', 0))
        self.assertEqual(prepared.provider, 'tiantianjiexi_generic')
        calls_before = list(self.media_requests)
        event = dict(schema=audit['schema'], token=audit['token'], phase='handed_off',
                     transport='browser_native', actual_bytes=0)
        for _ in range(2):
            response = self.client.post(audit['endpoint'], event, content_type='application/json')
            self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(DownloadAuditEvent.objects.filter(attempt_id=audit['attempt_id']).count(), 2)
        self.assertFalse(DownloadAuditEvent.objects.filter(attempt_id=audit['attempt_id'], status='success').exists())
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertEqual(self.client.post(audit['endpoint'], {**event, 'phase': 'transfer_completed'},
                                         content_type='application/json').status_code, 400)
        self.assertEqual(self.media_requests, calls_before)
        self.assertEqual(self.provider_calls, 1)

    def test_inv04_link_preview_do_not_issue_download_receipts_and_sw_report_is_client_observed(self):
        radio = self.parse()[1]
        for action in ('link', 'open', 'preview'):
            response = self.action(radio, action=action)
            self.assertNotIn('download_audit', response.json().get('item', {}))
        self.assertEqual(DownloadRequestLog.objects.count(), 0)
        self.disposition = 'inline'
        audit = self.action(radio).json()['item']['download_audit']
        for phase in ('handed_off', 'transfer_started', 'transfer_completed'):
            response = self.client.post(audit['endpoint'], dict(
                schema=audit['schema'], token=audit['token'], phase=phase,
                transport='browser_streaming', actual_bytes=12345 if phase == 'transfer_completed' else 0),
                content_type='application/json')
            self.assertEqual(response.status_code, 204, response.content)
        done = DownloadAuditEvent.objects.get(attempt_id=audit['attempt_id'], phase='transfer_completed')
        self.assertEqual(done.evidence_source, 'client_observed')
        self.assertEqual(done.actual_bytes, 12345)
