"""Download consumers record transfer outcomes, not descriptor issuance as success."""
import hashlib
import logging
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings

from djangoProject.logging import RequestContextFormatter
from .models import DownloadRequestLog
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

    def test_stream_log_formatter_keeps_safe_progress_and_close_fields(self):
        record = logging.makeLogRecord({'msg': 'download_stream_failed', 'levelno': logging.WARNING,
            'actual_yielded_bytes': 6, 'expected_bytes': 10, 'chunk_index': 1,
            'generator_close_reason': 'downstream_consumer_closed', 'secret_url': 'https://secret.invalid'})
        output = RequestContextFormatter('%(message)s').format(record)
        self.assertIn('actual_yielded_bytes=6', output)
        self.assertIn('generator_close_reason=downstream_consumer_closed', output)
        self.assertNotIn('secret.invalid', output)
