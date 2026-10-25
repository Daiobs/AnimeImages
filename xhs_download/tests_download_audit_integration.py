"""Download consumers record transfer outcomes, not descriptor issuance as success."""
import hashlib
import json
import logging
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from djangoProject.logging import RequestContextFormatter
from .models import DownloadRequestLog, RoutingDecisionLog
from .services.download_audit import download_attempts, operational_download_logs
from .services.download_handoff import issue_download_handoff_tokens


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class DownloadAuditStreamIntegrationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.item = {'type': 'image', 'url': 'https://ci.xiaohongshu.com/audit.jpg', 'size_bytes': 10}
        self.tokens = issue_download_handoff_tokens(
            item=self.item, base_filename='Audit image',
            client_identity=hashlib.sha256(b'127.0.0.1').hexdigest()[:24])
        self.upstream = Mock()
        self.upstream.headers = {'Content-Type': 'image/jpeg', 'Content-Length': '10'}
        self.upstream.url = self.item['url']
        self.upstream.iter_content.side_effect = lambda **kwargs: iter([b'first-', b'last'])

    def request_stream(self):
        with patch('xhs_download.views.fetch_media_response', return_value=self.upstream) as fetch:
            response = self.client.get(f'/media/download-stream/{self.tokens.stream_token}/')
        self.assertEqual(response.status_code, 200, getattr(response, 'content', b''))
        fetch.assert_called_once()
        return response

    def test_completed_only_after_stream_exhausted_with_actual_bytes(self):
        response = self.request_stream()
        self.assertFalse(DownloadRequestLog.objects.filter(status='success').exists())
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), b'first-')
        self.assertFalse(DownloadRequestLog.objects.filter(phase='transfer_completed').exists())
        self.assertEqual(b''.join(chunks), b'last')
        events = DownloadRequestLog.objects.filter(attempt_id=self.tokens.ticket_id)
        self.assertEqual(events.filter(phase='transfer_started').count(), 1)
        completed = events.get(phase='transfer_completed')
        self.assertEqual((completed.status, completed.actual_bytes, completed.transport),
                         ('success', 10, 'server_attachment'))
        response.close()

    def test_downstream_close_is_cancelled_and_never_counted_complete(self):
        response = self.request_stream()
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), b'first-')
        response.close()
        event = DownloadRequestLog.objects.get(attempt_id=self.tokens.ticket_id, phase='cancelled')
        self.assertEqual(event.actual_bytes, 6)
        self.assertEqual(event.error_code, 'DOWNLOAD_CONNECTION_CLOSED')
        self.assertFalse(DownloadRequestLog.objects.filter(phase='transfer_completed').exists())

    def test_route_prepare_and_stream_are_one_download_attempt(self):
        item = {'type': 'image', 'url': 'https://i0.hdslb.com/bfs/archive/audit.jpg', 'size_bytes': 10}
        payload = {'media_items': [item], 'base_filename': 'Audit image'}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Preparation must be local')):
            route = self.client.post('/media/download-images/', data=json.dumps({
                **payload, 'download_mode': 'proxy', 'route_only': True,
            }), content_type='application/json')
            self.assertEqual(route.status_code, 200, route.content)
            self.assertEqual(route.json()['download_strategy'], 'server_single_attachment')
            prepared = self.client.post('/media/download-prepare/', data=json.dumps(payload),
                                        content_type='application/json')
        self.assertEqual(prepared.status_code, 200, prepared.content)
        self.upstream.url = item['url']
        with patch('xhs_download.views.fetch_media_response', return_value=self.upstream) as fetch:
            response = self.client.get(prepared.json()['download_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'first-last')
        response.close()
        fetch.assert_called_once()
        attempts = download_attempts(DownloadRequestLog.objects.all())
        self.assertEqual(attempts.count(), 1)
        self.assertEqual((attempts.get().phase, attempts.get().actual_bytes), ('transfer_completed', 10))
        self.assertEqual(DownloadRequestLog.objects.values('attempt_id').distinct().count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertEqual(operational_download_logs(DownloadRequestLog.objects.all()).count(), 1)

    def test_single_attachment_route_preview_does_not_create_pending_download(self):
        item = {'type': 'image', 'url': 'https://i0.hdslb.com/bfs/archive/audit.jpg'}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Preview must be local')):
            response = self.client.post('/media/download-images/', data=json.dumps({
                'media_items': [item], 'download_mode': 'proxy', 'route_only': True,
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['download_strategy'], 'server_single_attachment')
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(RoutingDecisionLog.objects.exists())

    def test_stream_log_formatter_keeps_safe_progress_and_close_fields(self):
        record = logging.makeLogRecord({'msg': 'download_stream_failed', 'levelno': logging.WARNING,
            'actual_yielded_bytes': 6, 'expected_bytes': 10, 'chunk_index': 1,
            'generator_close_reason': 'downstream_consumer_closed', 'secret_url': 'https://secret.invalid'})
        output = RequestContextFormatter('%(message)s').format(record)
        self.assertIn('actual_yielded_bytes=6', output)
        self.assertIn('generator_close_reason=downstream_consumer_closed', output)
        self.assertNotIn('secret.invalid', output)
