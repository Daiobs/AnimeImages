from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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

    def test_unknown_remote_outcome_is_not_automatically_retried(self):
        with serial.account_slot():
            serial.request_in_flight(True)
        with self.assertRaisesRegex(serial.TianTianAdmissionDeferred, 'PREVIOUS_OUTCOME_UNKNOWN'):
            with serial.account_slot():
                self.fail('unknown is not permission to overlap')

    def test_recovery_keeps_inode_and_old_unknown_evidence(self):
        with serial.account_slot():
            serial.request_in_flight(True)
        before = serial.inspect_or_recover()
        operation = before['metadata']['operation_id']
        restored = serial.inspect_or_recover(expected_state=before['state_fingerprint'],
            evidence='human_risk_acceptance', reference='phase1b-human-conversation')
        self.assertTrue(restored['recovered'])
        after = serial.inspect_or_recover()
        self.assertEqual(after['inode'], before['inode'])
        self.assertEqual(after['marker'], 'C')
        self.assertEqual(after['metadata']['previous_operation_id'], operation)
        self.assertEqual(after['metadata']['old_outcome'], 'UNKNOWN_NOT_REPLAYED')
        with serial.account_slot():
            serial.request_in_flight(True)
            serial.request_in_flight(False)
        self.assertEqual(len(next(self.root.glob('*.events.jsonl')).read_text().splitlines()),4)

    def test_active_lock_and_stale_recovery_cannot_clear_new_state(self):
        with serial.account_slot():
            serial.request_in_flight(True)
            with self.assertRaisesRegex(serial.TianTianAdmissionDeferred,'ACCOUNT_ACTIVE'):
                serial.inspect_or_recover()
        before=serial.inspect_or_recover()
        serial.inspect_or_recover(expected_state=before['state_fingerprint'],
            evidence='human_risk_acceptance',reference='first')
        with serial.account_slot():
            serial.request_in_flight(True)
        with self.assertRaisesRegex(serial.TianTianAdmissionDeferred,'STATE_CHANGED'):
            serial.inspect_or_recover(expected_state=before['state_fingerprint'],
                evidence='human_risk_acceptance',reference='stale')
        self.assertEqual(serial.inspect_or_recover()['marker'],'U')

    def test_legacy_unknown_and_clear_are_compatible(self):
        with serial.account_slot(): pass
        path=next(self.root.glob('*.lock'))
        path.write_bytes(b'U')
        before=serial.inspect_or_recover()
        self.assertTrue(before['legacy_unattributed'])
        with self.assertRaisesRegex(serial.TianTianAdmissionDeferred,'PREVIOUS_OUTCOME_UNKNOWN'):
            with serial.account_slot(): self.fail('legacy U')
        serial.inspect_or_recover(expected_state=before['state_fingerprint'],
            evidence='human_risk_acceptance',reference='legacy-human')
        path.write_bytes(b'C')
        with serial.account_slot(): pass

    def test_pre_send_invalid_url_releases_but_timeout_stays_unknown(self):
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
            session=Mock()
            with override_settings(TIANTIANJIEXI_API_BASE_URL='ftp://example.org/invalid'):
                with self.assertRaises(Exception):
                    TianTianJieXiApiClient(session).parse_url('https://example.org/work')
            session.post.assert_not_called()
            self.assertEqual(serial.inspect_or_recover()['metadata']['reason'],'LOCAL_REQUEST_NOT_SENT')
            with self.assertRaises(Exception):
                TianTianJieXiApiClient(Mock(post=Mock(side_effect=requests.Timeout()))).parse_url('https://example.org/work')
            self.assertEqual(serial.inspect_or_recover()['marker'],'U')

    def test_invalid_redirect_after_dispatch_stays_unknown(self):
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
            with self.assertRaises(Exception):
                TianTianJieXiApiClient(Mock(post=Mock(side_effect=requests.exceptions.InvalidURL()))).parse_url('https://example.org/work')
        self.assertEqual(serial.inspect_or_recover()['marker'],'U')

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
