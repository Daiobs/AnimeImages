from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import json
import requests
import threading
import time
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.core.management import call_command

from .services.platforms import tiantian_serial as serial
from .services.platforms.tiantianjiexi_generic import TianTianJieXiApiClient
from .services.provider_lab.providers import PROVIDER_REGISTRY, _failure_result
from .services.provider_lab.providers import run_provider
from .services.provider_lab.contracts import ProviderLabError


@override_settings(TIANTIANJIEXI_PHONE="serial-fixture", TIANTIANJIEXI_PASSWORD="fixture")
class TianTianAccountSerialTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patched = patch.object(serial, 'lock_root', return_value=self.root)
        patched.start()
        self.addCleanup(patched.stop)

    def test_two_callers_and_distinct_sources_never_overlap_http(self):
        active = peak = 0
        guard = threading.Lock()
        def post(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(.08)
            with guard:
                active -= 1
            response = Mock()
            response.json.return_value = {'code': 200, 'data': {'title': 'fixture'}}
            return response
        clients = [TianTianJieXiApiClient(Mock(post=post)) for _ in range(3)]
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
            with ThreadPoolExecutor(3) as pool:
                futures = [pool.submit(c.parse_url, f'https://example.com/work/{i}') for i, c in enumerate(clients)]
                for future in futures:
                    future.result(timeout=3)
        self.assertEqual(peak, 1)
        self.assertGreater(max(c.queue_wait_ms for c in clients), 0)
        self.assertTrue(all(c.execution_ms >= 80 for c in clients))

    def test_cross_process_not_just_thread_lock(self):
        script = """
import fcntl, os, sys
fd=os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('BUSY')
else:
    print('ACQUIRED')
os.close(fd)
"""
        with serial.account_slot():
            path = next(self.root.glob('*.lock'))
            first = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True, timeout=5)
            self.assertEqual(first.stdout.strip(), 'BUSY')
        second = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(second.stdout.strip(), 'ACQUIRED')

    def test_queue_timeout_and_cancel_do_not_poison_next_call(self):
        with serial.account_slot():
            with ThreadPoolExecutor(1) as pool:
                def wait():
                    with serial.account_slot(.02):
                        self.fail('overlapped')
                with self.assertRaisesRegex(serial.TianTianAdmissionDeferred, 'QUEUE_DEFERRED'):
                    pool.submit(wait).result(timeout=2)
        with self.assertRaisesRegex(serial.TianTianAdmissionDeferred, 'QUEUE_CANCELLED'):
            with serial.account_slot(cancelled=lambda: True):
                self.fail('cancelled')
        with serial.account_slot():
            pass

    def test_exception_releases_lock_and_other_work_is_independent(self):
        with self.assertRaises(ValueError):
            with serial.account_slot():
                with ThreadPoolExecutor(1) as pool:
                    self.assertEqual(pool.submit(lambda: 'other-provider').result(timeout=1), 'other-provider')
                raise ValueError('fixture')
        with serial.account_slot():
            pass

    def test_legacy_unknown_does_not_block_or_rewrite_history(self):
        with serial.account_slot():
            pass
        path = next(self.root.glob('*.lock'))
        raw = b'U\n{"operation_id":"old-operation","reason":"REQUEST_MAY_BE_SENT"}'
        path.write_bytes(raw)
        journal = path.with_suffix('.events.jsonl')
        journal.write_bytes(b'{"marker":"U","operation_id":"old-operation"}\n')
        before = path.stat()
        response = Mock()
        response.json.return_value = {'code': 200, 'data': {
            'title': 'new work', 'videos': [{'url': 'https://example.org/media.mp4'}],
        }}
        session = Mock(post=Mock(return_value=response))
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
            result = TianTianJieXiApiClient(session).parse_url('https://example.org/new-work')
        self.assertEqual(result['title'], 'new work')
        session.post.assert_called_once()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(journal.read_bytes(), b'{"marker":"U","operation_id":"old-operation"}\n')
        state = serial.inspect_account_state()
        self.assertEqual(state['legacy_state']['marker'], 'U')
        self.assertFalse(state['unknown_outcome_blocks_requests'])
        self.assertFalse(state['recovery_required'])

    def test_inspection_reports_active_lock_without_changing_it(self):
        with serial.account_slot():
            self.assertTrue(serial.inspect_account_state()['local_active'])
        self.assertFalse(serial.inspect_account_state()['local_active'])

    def test_legacy_outcome_content_never_controls_admission(self):
        with serial.account_slot():
            pass
        path = next(self.root.glob('*.lock'))
        for raw in (b'U', b'C', b'U\nnot-json', b'invalid'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with serial.account_slot():
                    with serial.account_slot():
                        pass
                self.assertEqual(path.read_bytes(), raw)
                self.assertFalse(serial.inspect_account_state()['recovery_required'])

    def test_account_state_command_is_read_only_and_has_no_recovery_action(self):
        output = StringIO()
        call_command('tiantian_account_state', stdout=output)
        state = json.loads(output.getvalue())
        self.assertEqual(state['mode'], 'serial_only')
        self.assertFalse(state['recovery_required'])
        self.assertFalse(state['lock_present'])
        self.assertEqual(list(self.root.iterdir()), [])
        absent_root = self.root / 'absent'
        with patch.object(serial, 'lock_root', return_value=absent_root):
            self.assertFalse(serial.inspect_account_state()['lock_present'])
        self.assertFalse(absent_root.exists())
        with self.assertRaises(TypeError):
            call_command('tiantian_account_state', expected_state='old-fingerprint')

    def test_pre_send_invalid_url_does_not_send_or_block_next_request(self):
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
            session=Mock()
            with override_settings(TIANTIANJIEXI_API_BASE_URL='ftp://example.org/invalid'):
                with self.assertRaises(Exception):
                    TianTianJieXiApiClient(session).parse_url('https://example.org/work')
            session.post.assert_not_called()
            response = Mock()
            response.json.return_value = {'code': 200, 'data': {
                'title': 'new work', 'videos': [{'url': 'https://example.org/media.mp4'}],
            }}
            session.post.return_value = response
            self.assertEqual(TianTianJieXiApiClient(session).parse_url('https://example.org/new-work')['title'], 'new work')
            session.post.assert_called_once()

    def test_network_failure_is_not_replayed_and_next_explicit_request_can_run(self):
        for error in (requests.Timeout, requests.ConnectionError,
                      requests.exceptions.SSLError, requests.exceptions.InvalidURL):
            with self.subTest(error=error.__name__), \
                    patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
                response = Mock()
                response.json.return_value = {'code': 200, 'data': {
                    'title': 'next work', 'videos': [{'url': 'https://example.org/media.mp4'}],
                }}
                session = Mock(post=Mock(side_effect=[error('controlled boundary'), response]))
                client = TianTianJieXiApiClient(session)
                with self.assertRaises(Exception):
                    client.parse_url('https://example.org/uncertain-work', timeout_seconds=0.2)
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(session.post.call_args.kwargs['timeout'], 0.2)
                self.assertEqual(client.parse_url('https://example.org/next-work')['title'], 'next work')
                self.assertEqual(session.post.call_count, 2)
                self.assertEqual(session.post.call_args.kwargs['data']['link'], 'https://example.org/next-work')
                self.assertEqual(serial.inspect_account_state()['legacy_state']['marker'], 'EMPTY')
                self.assertEqual(list(self.root.glob('*.events.jsonl')), [])

    def test_deferred_result_is_unexecuted_not_parse_failure(self):
        from .services.provider_reliability import classify_provider_failure, ProviderOutcome, ProviderFailureClass
        self.assertEqual(classify_provider_failure(serial.TianTianAdmissionDeferred('SERVICE_QUEUE_DEFERRED')),
                         (ProviderOutcome.SKIPPED_ADMISSION, ProviderFailureClass.NOT_APPLICABLE))
        result = _failure_result(descriptor=PROVIDER_REGISTRY['tiantian_existing'], request_id='fixture',
            elapsed_ms=10, executed=True,
            error=ProviderLabError('PROVIDER_ADMISSION_DEFERRED', detail='SERVICE_QUEUE_DEFERRED'))
        self.assertEqual(result['status'], 'skipped')
        self.assertFalse(result['executed'])

    def test_services_keep_sharing_without_per_call_fee_confirmation(self):
        for provider in ('tiantian_existing', 'kukutool_existing'):
            self.assertEqual(PROVIDER_REGISTRY[provider].requires_confirmation, ('external_sharing',))

    def test_lab_and_scout_entry_share_account_admission(self):
        # Both clients enter the same authenticated Lab run endpoint/runner.
        active = peak = 0
        guard = threading.Lock()
        def post(*args, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(active, peak)
            time.sleep(.05)
            with guard:
                active -= 1
            response = Mock()
            response.json.return_value = {'code': 200, 'data': {'title': 'fixture'}}
            return response
        def runner(**kwargs):
            TianTianJieXiApiClient(Mock(post=post)).parse_url(kwargs['source'])
            return {'status': 'success'}, []
        descriptor = replace(PROVIDER_REGISTRY['tiantian_existing'], runner=runner,
                             availability=lambda: True, enabled_setting='')
        with patch.dict(PROVIDER_REGISTRY, tiantian_existing=descriptor), \
                patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True), \
                ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(run_provider, provider_id='tiantian_existing',
                     source=f'https://example.org/{client}', user_id=1,
                     confirmations={'external_sharing': True}) for client in ('lab', 'scout')]
            self.assertTrue(all(call.result(timeout=3)['executed'] for call in calls))
        self.assertEqual(peak, 1)
