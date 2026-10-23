"""G3: final safe attachments qualify the original authorized selection."""
import time
from unittest.mock import patch

import requests

from contracts import test_generic_browser_delivery as generic
from xhs_download.services.upstream_candidates import TTL


FINAL = 'https://independent-media.example/opaque-video'


class RedirectNativeDeliveryContracts(generic.GenericBrowserDeliveryContracts):
    def setUp(self):
        super().setUp()
        self.final_status = 200
        self.final_headers = {}
        self.final_observed = None

    def send(self, request, **options):
        if request.url != FINAL:
            return super().send(request, **options)
        self.media_requests.append((request.method, request.url, dict(request.headers)))
        self.assertIn(request.method, {'HEAD', 'GET'})
        if request.method == 'GET':
            self.assertEqual(request.headers.get('Range'), 'bytes=0-0')
        if self.final_observed:
            self.final_observed()
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, self.final_status
        response.headers.update({'Content-Type': 'video/mp4', 'Content-Length': '12345',
                                 'Content-Disposition': self.disposition, **self.final_headers})
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        response._content_consumed = True
        response._content = b''
        return response

    def test_cross_host_attachment_without_cors_preserves_selected_identity(self):
        self.media_redirect = FINAL
        radio = self.parse()[1]
        preview = self.action(radio, action='link')
        self.assertEqual(preview.status_code, 200, preview.content)
        original = preview.json()['item']
        self.assertEqual(self.media_requests, [])
        response = self.action(radio)
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertEqual(item['download_strategy'], 'browser_native')
        self.assertEqual(item['url'], generic.SECOND)
        self.assertEqual(item['candidate_id'], radio['value'])
        for key in ('candidate_id', 'url', 'selected_format_id', 'filename'):
            self.assertEqual(item[key], original[key])
        self.assertEqual(item['size_bytes'], 12345)
        self.assertLessEqual(item['native_download_expires_at'], item['selection_expires_at'])
        self.assertNotIn(FINAL, str(item))
        self.validate_browser_action(radio, item)
        self.assertEqual([row[:2] for row in self.media_requests],
                         [('HEAD', generic.SECOND), ('HEAD', FINAL)])
        again = self.action(radio).json()['item']
        self.assertEqual(again['native_download_expires_at'], item['native_download_expires_at'])
        self.assertEqual(again['selection_expires_at'], item['selection_expires_at'])
        self.assertEqual(len(self.media_requests), 2)
        self.assertEqual(self.provider_calls, 1)

    def test_redirected_nonattachments_use_existing_streaming_path(self):
        self.media_redirect = FINAL
        now = time.time()
        for index, disposition in enumerate(('inline', 'attachment; filename=12345.bin',
                                            'attachment; filename=12345.webm', 'attachment',
                                            'attachment; filename="../12345.mp4"')):
            with self.subTest(disposition=disposition):
                self.disposition = disposition
                # Advance past the previous response evidence, not candidate authority.
                now += 61
                calls = len(self.media_requests)
                with patch('time.time', return_value=now), patch.object(
                        generic, 'SOURCE', f'https://www.instagram.com/p/redirect-name-{index}/'):
                    self.provider_calls = 0
                    radio = self.parse()[1]
                    response = self.action(radio)
                self.assertEqual(response.status_code, 200, response.content)
                item = response.json()['item']
                self.assertEqual(item['url'], generic.SECOND)
                self.assertEqual(item['download_strategy'], 'browser_streaming')
                self.assertNotIn('native_download_expires_at', item)
                self.assertEqual(item['browser_response_hosts'], ['media.fbcdn.net', 'independent-media.example'])
                self.validate_browser_action(radio, item)
                self.assertEqual(len(self.media_requests), calls + 2)

    def test_private_redirect_is_rejected_before_request_and_does_not_issue_native(self):
        self.media_redirect = 'https://127.0.0.1/private'
        response = self.action(self.parse()[1])
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()['item']
        self.assertNotEqual(item['download_strategy'], 'browser_native')
        self.assertNotIn('native_download_expires_at', item)
        self.assertNotIn('browser_response_hosts', item)
        self.assertEqual([row[:2] for row in self.media_requests], [('HEAD', generic.SECOND)])

    def test_required_context_cannot_borrow_existing_redirect_evidence(self):
        self.media_redirect = FINAL
        response = self.action(self.parse()[1])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['item']['download_strategy'], 'browser_native')
        calls = len(self.media_requests)
        self.required = {'required_headers': ['Referer']}
        self.provider_calls = 0
        # The provider reply has changed; a fresh admitted parse must preserve it.
        with patch.object(generic, 'SOURCE', 'https://www.instagram.com/p/required-context/'):
            response = self.action(self.parse()[1])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(len(self.media_requests), calls)

    def test_redirect_evidence_is_capped_by_original_candidate_expiry(self):
        self.media_redirect = FINAL
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            radio = self.parse()[1]
            expiry = int((clock[0] + TTL) * 1000)
            clock[0] = expiry / 1000 - 5
            response = self.action(radio)
            self.assertEqual(response.status_code, 200, response.content)
            item = response.json()['item']
            self.assertEqual(item['download_strategy'], 'browser_native')
            self.assertEqual(item['native_download_expires_at'], expiry)
            self.assertEqual(item['selection_expires_at'], expiry)
            self.assertEqual(item['url'], generic.SECOND)
            clock[0] = expiry / 1000 + 1
            expired = self.action(radio)
            self.assertIn(expired.status_code, (403, 409))
            self.assertNotIn('item', expired.json())
            self.assertEqual(len(self.media_requests), 2)

    def test_expired_redirect_evidence_is_replaced_by_current_inline_response(self):
        self.media_redirect = FINAL
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            radio = self.parse()[1]
            response = self.action(radio)
            self.assertEqual(response.status_code, 200, response.content)
            original = response.json()['item']
            self.assertEqual(original['download_strategy'], 'browser_native')
            clock[0] = original['native_download_expires_at'] / 1000 + 1
            self.disposition = 'inline'
            response = self.action(radio)
            self.assertEqual(response.status_code, 200, response.content)
            item = response.json()['item']
            self.assertEqual(item['download_strategy'], 'browser_streaming')
            self.assertNotIn('native_download_expires_at', item)
            for key in ('url', 'candidate_id', 'selected_format_id', 'selection_expires_at'):
                self.assertEqual(item[key], original[key])
            self.assertEqual(len(self.media_requests), 4)
            self.assertEqual(self.action(radio).json()['item']['download_strategy'], 'browser_streaming')
            self.assertEqual(len(self.media_requests), 4)
            self.assertEqual(self.provider_calls, 1)

    def test_authorization_expiring_during_redirect_probe_cannot_issue_descriptor(self):
        self.media_redirect = FINAL
        clock = [time.time()]
        with patch('time.time', side_effect=lambda: clock[0]):
            radio = self.parse()[1]

            def expire_authority():
                clock[0] += 301

            self.final_observed = expire_authority
            response = self.action(radio)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertNotIn('item', response.json())
        self.assertEqual([row[:2] for row in self.media_requests],
                         [('HEAD', generic.SECOND), ('HEAD', FINAL)])
