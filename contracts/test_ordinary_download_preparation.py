"""INV-03/06: a real click prepares current evidence; a preview never probes."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import time

from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django.urls import reverse

from contracts import test_backend_contracts as backend
from download_console.models import DownloadRuntimeSetting


MEDIA = backend.MEDIA


@override_settings(**backend.BackendHTTPContracts._overridden_settings)
class OrdinaryDownloadPreparationContracts(TestCase):
    setUp = backend.BackendHTTPContracts.setUp
    parse = backend.BackendHTTPContracts.parse

    def native_item(self):
        self.transport.native_outcome = 'success'
        self.transport.media_headers = {'Content-Disposition': 'attachment; filename="12345.mp4"'}
        page = self.parse()
        self.assertEqual(page.status_code, 200, page.content)
        card = BeautifulSoup(page.content, 'html.parser').select_one('[data-type="video"][data-url]')
        self.assertIsNotNone(card)
        return {'type': 'video', 'url': card['data-url'], 'source_platform': 'bilibili',
                'size_bytes': self.transport.native_size, 'filename': 'Contract native video.mp4'}

    def action(self, item, *, intent='download'):
        payload = {'media_items': [item], 'download_mode': 'browser', 'route_only': True}
        if intent:
            payload['intent'] = intent
        return self.client.post(reverse('download_images_api'), payload, content_type='application/json')

    def test_known_size_click_prepares_native_and_preserves_selected_resource(self):
        item = self.native_item()
        self.assertEqual(self.transport.media_calls, [])
        response = self.action(item)
        self.assertEqual(response.status_code, 200, response.content)
        prepared = response.json()['items'][0]
        self.assertEqual(prepared['download_strategy'], 'browser_native')
        self.assertEqual(prepared['url'], MEDIA)
        self.assertEqual(prepared['size_bytes'], item['size_bytes'])
        self.assertEqual(prepared['filename'], item['filename'])
        self.assertEqual(self.transport.media_calls, ['HEAD'])

    def test_preview_does_not_probe_then_click_consumes_fresh_descriptor(self):
        item = self.native_item()
        preview = self.action(item, intent=None)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()['items'][0]['download_strategy'], 'browser_streaming')
        self.assertEqual(self.transport.media_calls, [])
        clicked = self.action(item)
        self.assertEqual(clicked.json()['items'][0]['download_strategy'], 'browser_native')
        self.assertEqual(self.transport.media_calls, ['HEAD'])

    def test_current_evidence_reused_and_expired_evidence_refreshed(self):
        item = self.native_item()
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            first = self.action(item)
            self.assertEqual(first.status_code, 200, first.content)
            expiry = first.json()['items'][0]['native_download_expires_at']
            self.assertEqual(self.action(item).json()['items'][0]['download_strategy'], 'browser_native')
            self.assertEqual(self.transport.media_calls, ['HEAD'])
            clock[0] = expiry / 1000 + 1
            refreshed = self.action(item)
            self.assertEqual(refreshed.status_code, 200, refreshed.content)
            self.assertGreater(refreshed.json()['items'][0]['native_download_expires_at'], expiry)
        self.assertEqual(self.transport.media_calls, ['HEAD', 'HEAD'])

    def test_cost_denial_precedes_probe(self):
        item = self.native_item()
        runtime = DownloadRuntimeSetting.load()
        runtime.visitor_video_download_enabled = False
        runtime.save()
        self.assertEqual(self.action(item).status_code, 403)
        self.assertEqual(self.transport.media_calls, [])

    def test_rate_denial_precedes_missing_evidence_probe(self):
        item = self.native_item()
        runtime = DownloadRuntimeSetting.load()
        runtime.daily_request_limit_per_ip = 1
        runtime.save()
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            first = self.action(item)
            self.assertEqual(first.status_code, 200, first.content)
            # Let naturally generated evidence expire while the daily cap stays reached.
            clock[0] = first.json()['items'][0]['native_download_expires_at'] / 1000 + 1
            denied = self.action(item)
        self.assertEqual(denied.status_code, 429, denied.content)
        self.assertEqual(self.transport.media_calls, ['HEAD'])

    def service_item(self):
        self.transport.media_headers = {'Content-Disposition': 'attachment; filename="12345.mp4"'}
        page = self.parse()
        self.assertEqual(page.status_code, 200, page.content)
        option = BeautifulSoup(page.content, 'html.parser').select_one('[data-picker-radio]')
        self.assertIsNotNone(option)
        response = self.client.post(reverse('service_selected_format'), {
            'candidate_id': option['value'], 'resource_token': option['data-token'],
            'action': 'legacy',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['item']

    def test_original_manifest_expiring_during_probe_is_not_returned(self):
        with self.settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=3):
            item = self.service_item()
            clock = [time.time()]
            original_send = self.transport.send

            def advance_at_media(request, **kwargs):
                response = original_send(request, **kwargs)
                if request.url == MEDIA:
                    clock[0] += 4
                return response

            # Replace only the final HTTP boundary already used by the contract.
            from contracts.network import offline_requests
            with patch('time.time', side_effect=lambda: clock[0]), offline_requests(advance_at_media):
                response = self.action(item)
            self.assertEqual(response.status_code, 403, response.content)
            self.assertNotIn('items', response.json())
            self.assertEqual(self.transport.media_calls, ['HEAD'])

    def test_legacy_probe_without_response_evidence_keeps_consumer_authority_deadline(self):
        issued_at = 2000000000
        # Resolve module clock aliases before freezing time so lazy imports cannot retain the mock.
        with (
            self.settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=3),
            patch('xhs_download.services.upstream_delivery.time', return_value=issued_at),
            patch('xhs_download.services.service_format_selection.time', return_value=issued_at),
            patch('time.time', return_value=issued_at),
        ):
            item = self.service_item()
            self.transport.media_headers = {'Content-Type': 'text/html'}
            response = self.action({**item, 'expiresAt': (issued_at + 3600) * 1000,
                                    'selection_expires_at': (issued_at + 3600) * 1000})
        self.assertEqual(response.status_code, 200, response.content)
        prepared = response.json()['items'][0]
        self.assertEqual(prepared['download_strategy'], 'browser_streaming')
        self.assertEqual(prepared['url'], item['url'])
        self.assertEqual(prepared['asset_id'], item['asset_id'])
        self.assertNotIn('selection_schema', prepared)
        self.assertNotIn('browser_response_hosts_expires_at', prepared)
        self.assertEqual(prepared['selection_expires_at'], (issued_at + 3) * 1000)
        self.assertEqual(self.transport.media_calls, ['HEAD', 'GET'])
        script = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const protocol = require('./static/js/ordinary_media_streaming_protocol.js');
const item = JSON.parse(fs.readFileSync(0, 'utf8'));
const expiry = item.selection_expires_at;
const create = now => protocol.createTransportDescriptor(
    {...item, downloadId: 'a'.repeat(32)}, {nowEpochMs: now});
const active = create(expiry - 1);
assert.equal(active.url, item.url);
assert.throws(() => create(expiry), /DESCRIPTOR_EXPIRED/,
    'the HTTP descriptor must be rejected at its original manifest deadline');
assert.throws(() => create(expiry + 1), /DESCRIPTOR_EXPIRED/);
assert.equal(active.expiresAt, expiry);
"""
        checked = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                 input=json.dumps(prepared), text=True, capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_client_expiry_cannot_shorten_or_extend_prepared_manifest_authority(self):
        issued_at = 2000000000
        with (
            self.settings(PROVIDER_MANIFEST_MAX_AGE_SECONDS=3),
            patch('xhs_download.services.upstream_delivery.time', return_value=issued_at),
            patch('xhs_download.services.service_format_selection.time', return_value=issued_at),
            patch('time.time', return_value=issued_at),
        ):
            item = self.service_item()
            for caller_expiry in (1, (issued_at + 3600) * 1000):
                with self.subTest(caller_expiry=caller_expiry):
                    response = self.action({**item, 'expiresAt': caller_expiry,
                                            'selection_expires_at': caller_expiry})
                    self.assertEqual(response.status_code, 200, response.content)
                    prepared = response.json()['items'][0]
                    self.assertEqual(prepared['expiresAt'], (issued_at + 3) * 1000)
                    self.assertEqual(prepared['selection_expires_at'], prepared['expiresAt'])
                    self.assertEqual(prepared['native_download_expires_at'], prepared['expiresAt'])
        self.assertEqual(self.transport.media_calls, ['HEAD'])

    def test_manifest_or_source_admission_denied_without_probe(self):
        item = self.service_item()
        for denied_item in ({**item, 'url': MEDIA + '?different-resource'}, item):
            if denied_item == item:
                self.runtime.third_party_allowed_public_platforms = ''
                self.runtime.save()
            response = self.action(denied_item)
            self.assertIn(response.status_code, (400, 403), response.content)
        self.assertEqual(self.transport.media_calls, [])
