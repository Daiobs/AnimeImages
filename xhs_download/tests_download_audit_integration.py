"""Download consumers record transfer outcomes, not descriptor issuance as success."""
import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local
from time import monotonic
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.db import DatabaseError, close_old_connections, connection, connections
from django.test import RequestFactory, TransactionTestCase, override_settings, skipUnlessDBFeature

from contracts.network import offline_requests
from djangoProject.logging import RequestContextFormatter
from .models import DownloadAuditEvent, DownloadRequestLog, PolicyControlRecord, RoutingDecisionLog
from .services.admin_console import build_admin_console_context
from .services.download_audit import download_attempts, operational_download_logs, record_transfer_audit
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
        self.assertFalse(DownloadAuditEvent.objects.filter(status='success').exists())
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), b'first-')
        self.assertFalse(DownloadAuditEvent.objects.filter(phase='transfer_completed').exists())
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertEqual(b''.join(chunks), b'last')
        events = DownloadAuditEvent.objects.filter(attempt_id=self.tokens.ticket_id)
        self.assertEqual(events.filter(phase='transfer_started').count(), 1)
        completed = events.get(phase='transfer_completed')
        self.assertEqual((completed.status, completed.actual_bytes, completed.transport),
                         ('success', 10, 'server_attachment'))
        self.assertIsNone(completed.legacy_log_id)
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertEqual(download_attempts(events).count(), 1)
        self.assertFalse(RoutingDecisionLog.objects.exists())
        response.close()
        self.assertEqual(events.filter(phase='transfer_completed').count(), 1)
        cards = {row['label']: row['value'] for row in build_admin_console_context()['cards']}
        self.assertEqual(cards['今日下载次数（去重尝试）'], 1)
        self.assertEqual(cards['今日下载实际流量（服务端已发送）'], '10 B')

    def test_downstream_close_is_cancelled_and_never_counted_complete(self):
        response = self.request_stream()
        chunks = iter(response.streaming_content)
        self.assertEqual(next(chunks), b'first-')
        response.close()
        event = DownloadAuditEvent.objects.get(attempt_id=self.tokens.ticket_id, phase='cancelled')
        self.assertEqual(event.actual_bytes, 6)
        self.assertEqual(event.status, 'cancelled')
        self.assertEqual(event.error_code, 'DOWNLOAD_CONNECTION_CLOSED')
        self.assertFalse(DownloadAuditEvent.objects.filter(phase='transfer_completed').exists())
        self.assertIsNone(event.legacy_log_id)
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(RoutingDecisionLog.objects.exists())
        self.assertFalse(operational_download_logs(DownloadRequestLog.objects.all()).exists())
        attempt = download_attempts(DownloadAuditEvent.objects.all()).get()
        self.assertEqual((attempt.phase, attempt.observed_bytes), ('cancelled', 6))

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
        attempts = download_attempts(DownloadAuditEvent.objects.all())
        self.assertEqual(attempts.count(), 1)
        self.assertEqual((attempts.get().phase, attempts.get().actual_bytes), ('transfer_completed', 10))
        self.assertEqual(DownloadAuditEvent.objects.values('attempt_id').distinct().count(), 1)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(set(DownloadRequestLog.objects.values_list('download_type', flat=True)),
                         {'single_prepare'})
        self.assertFalse(DownloadRequestLog.objects.exclude(phase='', attempt_id='', status='success').exists())
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertFalse(operational_download_logs(DownloadRequestLog.objects.all()).exists())
        linked = DownloadAuditEvent.objects.get(legacy_log_id__isnull=False)
        self.assertEqual(linked.phase, 'prepared')
        cards = {row['label']: row['value'] for row in build_admin_console_context()['cards']}
        self.assertEqual(cards['今日下载次数（去重尝试）'], 1)
        self.assertEqual(cards['今日 legacy 行数（旧口径，不计新完成率）'], 0)
        self.assertEqual(cards['今日下载实际流量（服务端已发送）'], '10 B')
        self.assertEqual(cards['今日 legacy 记录流量（未复核）'], '0 B')

    def prepare_server_attachment(self):
        item = {'type': 'image', 'url': 'https://i0.hdslb.com/bfs/archive/audit.jpg', 'size_bytes': 10}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Preparation must be local')):
            response = self.client.post('/media/download-prepare/', data=json.dumps({
                'media_items': [item], 'base_filename': 'Audit image',
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.upstream.url = item['url']
        return response.json()

    def test_preparation_without_transfer_does_not_supply_success_samples(self):
        self.prepare_server_attachment()
        attempt = download_attempts(DownloadAuditEvent.objects.all()).get()
        self.assertEqual((attempt.phase, attempt.status, attempt.actual_bytes), ('prepared', 'pending', 0))
        self.assertFalse(DownloadAuditEvent.objects.filter(phase='transfer_completed').exists())
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertFalse(operational_download_logs(DownloadRequestLog.objects.all()).exists())
        cards = {row['label']: row['value'] for row in build_admin_console_context()['cards']}
        self.assertEqual(cards['传输完成率（已观测终态，非落盘）'], '未知')
        self.assertEqual(cards['今日下载实际流量（服务端已发送）'], '0 B')

    def test_cancel_after_prepare_keeps_only_trusted_dispatch_feedback(self):
        prepared = self.prepare_server_attachment()
        with patch('xhs_download.views.fetch_media_response', return_value=self.upstream) as fetch:
            response = self.client.get(prepared['download_url'])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(next(iter(response.streaming_content)), b'first-')
            response.close()
        fetch.assert_called_once()
        attempt = download_attempts(DownloadAuditEvent.objects.all()).get()
        self.assertEqual((attempt.phase, attempt.actual_bytes), ('cancelled', 6))
        self.assertIsNone(attempt.legacy_log_id)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(DownloadRequestLog.objects.get().download_type, 'single_prepare')
        self.assertFalse(DownloadRequestLog.objects.filter(status='failed').exists())
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertFalse(operational_download_logs(DownloadRequestLog.objects.all()).exists())
        cards = {row['label']: row['value'] for row in build_admin_console_context()['cards']}
        self.assertEqual(cards['今日下载实际流量（服务端已发送）'], '6 B')

    def prepare_browser_video(self):
        item = {'type': 'video', 'url': 'https://sns-video-hw.xhscdn.com/audit.mp4', 'size_bytes': 10}
        requests_seen = []

        def send(request, **options):
            self.assertEqual((request.method, request.url), ('HEAD', item['url']))
            requests_seen.append(request.url)
            response = requests.Response()
            response.request, response.url, response.status_code = request, request.url, 200
            response._content, response._content_consumed = b'', True
            response.headers.update({'Content-Type': 'video/mp4', 'Content-Length': '10'})
            return response

        with offline_requests(send):
            response = self.client.post('/media/download-images/', data=json.dumps({
                'download_mode': 'auto', 'intent': 'download', 'media_items': [item],
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        descriptor = response.json()['items'][0]
        self.assertEqual(descriptor['url'], item['url'])
        self.assertEqual(descriptor['download_strategy'], 'browser_streaming')
        self.assertEqual(requests_seen, [item['url']])
        return descriptor

    def test_sw_http_phases_are_one_attempt_and_not_legacy_success_or_server_traffic(self):
        descriptor = self.prepare_browser_video()
        receipt = descriptor['download_audit']
        control_count = PolicyControlRecord.objects.count()
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Telemetry cannot fetch media')):
            for phase, size in (('handed_off', 0), ('transfer_started', 6), ('transfer_completed', 10),
                                ('transfer_completed', 10)):
                response = self.client.post(receipt['endpoint'], data=json.dumps({
                    'schema': receipt['schema'], 'token': receipt['token'], 'phase': phase,
                    'transport': 'browser_streaming', 'actual_bytes': size,
                }), content_type='application/json')
                self.assertEqual(response.status_code, 204, response.content)
        events = DownloadAuditEvent.objects.filter(attempt_id=receipt['attempt_id'])
        self.assertEqual(events.count(), 4)
        attempt = download_attempts(events).get()
        self.assertEqual((attempt.phase, attempt.actual_bytes, attempt.evidence_source),
                         ('transfer_completed', 10, 'client_observed'))
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertEqual(PolicyControlRecord.objects.count(), control_count)
        self.assertFalse(operational_download_logs(DownloadRequestLog.objects.all()).exists())
        cards = {row['label']: row['value'] for row in build_admin_console_context()['cards']}
        self.assertEqual(cards['今日下载次数（去重尝试）'], 1)
        self.assertEqual(cards['今日下载实际流量（服务端已发送）'], '0 B')

    def test_audit_database_failure_during_prepare_and_transfer_does_not_retry_media(self):
        with patch('xhs_download.services.observability.DownloadAuditEvent.objects.get_or_create',
                   side_effect=DatabaseError('audit storage unavailable')) as audit_write:
            prepared = self.prepare_server_attachment()
            with patch('xhs_download.views.fetch_media_response', return_value=self.upstream) as fetch:
                response = self.client.get(prepared['download_url'])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(b''.join(response.streaming_content), b'first-last')
                response.close()
            self.assertGreaterEqual(audit_write.call_count, 1)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], self.upstream.url)
        self.upstream.iter_content.assert_called_once()
        self.assertFalse(DownloadAuditEvent.objects.exists())
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_client_telemetry_database_failure_is_non_blocking_and_does_not_fetch_media(self):
        descriptor = self.prepare_browser_video()
        receipt = descriptor['download_audit']
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Telemetry cannot fetch media')):
            with patch('xhs_download.services.observability.DownloadAuditEvent.objects.get_or_create',
                       side_effect=DatabaseError('audit storage unavailable')):
                response = self.client.post(receipt['endpoint'], data=json.dumps({
                    'schema': receipt['schema'], 'token': receipt['token'], 'phase': 'transfer_completed',
                    'transport': 'browser_streaming', 'actual_bytes': 10,
                }), content_type='application/json')
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)
        self.assertEqual(DownloadAuditEvent.objects.get().phase, 'prepared')
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_existing_prepared_anchor_serializes_concurrent_dispatch_transports(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Uses PostgreSQL lock-wait evidence')
        attempt_id = 'existing-prepared-concurrent-dispatch'
        DownloadAuditEvent.objects.create(
            attempt_id=attempt_id, phase='prepared', status='pending', evidence_source='server_observed',
        )
        self.assertTrue(connection.get_autocommit())
        first_at_business_write, release_first, second_connected = Event(), Event(), Event()
        state, backend_pids = local(), {}
        create_legacy = DownloadRequestLog.objects.create

        def held_business_write(**payload):
            if state.transport == 'browser_streaming':
                first_at_business_write.set()
                if not release_first.wait(10):
                    raise AssertionError('Concurrent dispatch test did not release its first writer')
            return create_legacy(**payload)

        def dispatch(transport):
            close_old_connections()
            state.transport = transport
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    backend_pids[transport] = cursor.fetchone()[0]
                if transport == 'server_attachment':
                    second_connected.set()
                return record_transfer_audit(
                    RequestFactory().post('/media/download-prepare/'),
                    attempt_id=attempt_id, phase='handed_off', transport=transport,
                    evidence_source='server_observed', route_type='direct',
                    final_policy_route='direct', status='success', dispatch_feedback=True,
                )
            finally:
                connections.close_all()

        with patch('requests.sessions.Session.request', side_effect=AssertionError('Audit cannot fetch media')):
            with patch.object(DownloadRequestLog.objects, 'create', side_effect=held_business_write):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(dispatch, 'browser_streaming')
                    try:
                        self.assertTrue(first_at_business_write.wait(5))
                        second = pool.submit(dispatch, 'server_attachment')
                        self.assertTrue(second_connected.wait(5))
                        blocked_on_first = False
                        deadline = monotonic() + 5
                        # Distinct transports avoid the event unique constraint. Observe
                        # an actual PG wait on the already-committed prepared anchor.
                        while monotonic() < deadline and not second.done():
                            with connection.cursor() as cursor:
                                cursor.execute('SELECT %s = ANY(pg_blocking_pids(%s))', [
                                    backend_pids['browser_streaming'], backend_pids['server_attachment'],
                                ])
                                blocked_on_first = cursor.fetchone()[0]
                            if blocked_on_first:
                                break
                            release_first.wait(0.01)
                        self.assertTrue(blocked_on_first, 'Second dispatch did not wait on the first event lock')
                    finally:
                        release_first.set()
                    self.assertIsNotNone(first.result(timeout=5))
                    self.assertIsNotNone(second.result(timeout=5))

        events = DownloadAuditEvent.objects.filter(attempt_id=attempt_id)
        self.assertEqual(events.count(), 3)
        self.assertEqual(set(events.filter(phase='handed_off').values_list('transport', flat=True)),
                         {'browser_streaming', 'server_attachment'})
        self.assertEqual(download_attempts(events).count(), 1)
        self.assertEqual(events.filter(legacy_log_id__isnull=False).count(), 1)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertEqual(PolicyControlRecord.objects.count(), 1)

    def test_single_attachment_route_preview_does_not_create_pending_download(self):
        item = {'type': 'image', 'url': 'https://i0.hdslb.com/bfs/archive/audit.jpg'}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Preview must be local')):
            response = self.client.post('/media/download-images/', data=json.dumps({
                'media_items': [item], 'download_mode': 'proxy', 'route_only': True,
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['download_strategy'], 'server_single_attachment')
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(DownloadAuditEvent.objects.exists())
        self.assertFalse(RoutingDecisionLog.objects.exists())

    def test_stream_log_formatter_keeps_safe_progress_and_close_fields(self):
        record = logging.makeLogRecord({'msg': 'download_stream_failed', 'levelno': logging.WARNING,
            'actual_yielded_bytes': 6, 'expected_bytes': 10, 'chunk_index': 1,
            'generator_close_reason': 'downstream_consumer_closed', 'secret_url': 'https://secret.invalid'})
        output = RequestContextFormatter('%(message)s').format(record)
        self.assertIn('actual_yielded_bytes=6', output)
        self.assertIn('generator_close_reason=downstream_consumer_closed', output)
        self.assertNotIn('secret.invalid', output)
