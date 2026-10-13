"""Explicit local, metadata-only census entry; never registered as a Web API."""

import contextlib
import json
import logging
import re
from pathlib import Path
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tools.census_capture import Capture, MAX_BYTES
from xhs_download.services.quality_execution import _python_for, EXPECTED_VERSIONS

AUTHORIZATION = 'UPSTREAM_CAPABILITY_CENSUS_PHASE1_V1'
PROVIDERS = ('yt_dlp_generic', 'gallery_dl_generic', 'parsehub_generic', 'videodl_repo',
             'tiantianjiexi_generic', 'kukutool_generic')


def collect(provider, source, timeout=60):
    from xhs_download.services.common import validate_public_url
    from tools.upstream_match_worker import validate_url
    from xhs_download.services.platforms.external_tools import run_external_command
    validate_url(source)
    validate_public_url(source)
    python = _python_for(provider)
    capture = Capture()
    result = {'executed': False, 'matched': True, 'cache_hit': False,
              'application_compatibility': 'NOT_EVALUATED', 'internal_http': 'UNKNOWN'}
    started = time.monotonic()
    try:
        if provider in {'yt_dlp_generic', 'gallery_dl_generic', 'parsehub_generic'}:
            completed = run_external_command(
                [python, '-I', '-B', str(Path(settings.BASE_DIR) / 'tools/upstream_census_worker.py')],
                input_text=json.dumps({'provider': provider, 'url': source, 'timeout': timeout,
                    'version': EXPECTED_VERSIONS.get(provider, '')}), timeout_seconds=timeout+5,
                max_stdout_bytes=MAX_BYTES, max_stderr_bytes=65536)
            result = json.loads(completed.stdout)
            if completed.returncode:
                raise ValueError('worker_exit')
            return result
        if provider == 'tiantianjiexi_generic':
            import requests
            from urllib.parse import urlsplit
            from xhs_download.services.platforms.tiantianjiexi_generic import TianTianJieXiApiClient
            class Session(requests.Session):
                def send(self, request, **kwargs):
                    validate_public_url(request.url)
                    if urlsplit(request.url).hostname != expected_host:
                        raise ValueError('service_redirect_host_changed')
                    kwargs['verify'] = True
                    return super().send(request, **kwargs)
            session = Session()
            session.trust_env = False
            client = TianTianJieXiApiClient(session=session, observe=capture)
            capture.secrets = (client.phone, client.password)
            expected_host = urlsplit(client.base_url).hostname
            result.update(executed=True, capture_source='API business response')
            try:
                client.parse_url(source, timeout_seconds=timeout)
            finally:
                result.update(queue_wait_ms=getattr(client, 'queue_wait_ms', 0),
                              execution_ms=getattr(client, 'execution_ms', 0))
                session.close()
        elif provider == 'kukutool_generic':
            from xhs_download.services.platforms.douyin_kukutool import run_kukutool_url_helper
            result.update(executed=True, capture_source='API decrypted business response')
            capture(run_kukutool_url_helper(source, timeout_seconds=timeout))
        elif provider == 'videodl_repo':
            from xhs_download.services.provider_lab.providers import WORKER_ROOT
            from xhs_download.services.provider_lab.videodl_control import (
                load_videodl_inventory)
            from xhs_download.services.provider_lab.runtime import execute_provider_worker
            from uuid import uuid4
            kwargs = dict(python=python, worker=WORKER_ROOT/'videodl_worker.py',
                          request_id=uuid4().hex, timeout_seconds=5)
            inventory = load_videodl_inventory(**kwargs)
            matched = execute_provider_worker(**kwargs, source=source, operation='match_dedicated')['backend_ids']
            allowed = {item.backend_id for item in inventory if item.controlled_state == 'allowed'
                       and item.backend_category == 'dedicated'}
            selected = next((name for name in matched if name in allowed), None)
            result.update(mode='dedicated_only', other_modes='NOT_ATTEMPTED', matched=bool(matched))
            if selected is None:
                result.update(status='NOT_EXECUTED', error='POLICY_LIMITED' if matched else 'NO_DEDICATED_MATCH')
                return result
            completed = run_external_command(
                [python, '-I', '-B', str(Path(settings.BASE_DIR)/'tools/upstream_census_worker.py')],
                input_text=json.dumps({'provider': provider, 'url': source, 'backend': selected,
                                      'timeout': timeout}), timeout_seconds=timeout+5,
                max_stdout_bytes=MAX_BYTES, max_stderr_bytes=65536)
            return json.loads(completed.stdout)
        result['status'] = 'RETURNED'
    except Exception as error:
        from xhs_download.services.platforms.tiantian_serial import TianTianAdmissionDeferred
        from xhs_download.services.platforms.tiantianjiexi_generic import (
            TianTianJieXiUncertainError, TianTianJieXiUnsupportedError,
            TianTianJieXiAuthError, TianTianJieXiRateLimitError)
        from tools.upstream_match_worker import safe_error
        code = getattr(error, 'code', None) or getattr(error, 'diagnostic_detail', None) or safe_error(error)['error']
        safe_code = str(code) if re.fullmatch(r'[A-Za-z0-9_:-]{1,100}', str(code)) else 'PROVIDER_ERROR_REDACTED'
        result.update(status='FAILED', error=safe_code, error_class=type(error).__name__)
        if hasattr(error, 'attempts'):
            result['internal_attempt_record_count'] = len(error.attempts)
        if isinstance(error, (subprocess.TimeoutExpired, TimeoutError)) or 'Timeout' in type(error).__name__:
            result['status'] = 'OUTCOME_UNKNOWN'
        if isinstance(error, TianTianJieXiUncertainError):
            result['status'] = 'OUTCOME_UNKNOWN'
        if isinstance(error, TianTianJieXiUnsupportedError):
            result['upstream_semantic'] = 'BUSINESS_UNSUPPORTED'
        if isinstance(error, (TianTianJieXiAuthError, TianTianJieXiRateLimitError)):
            result['pause_provider'] = True
        if isinstance(error, TianTianAdmissionDeferred):
            result.update(status='NOT_EXECUTED', executed=False, error=error.reason,
                          queue_wait_ms=error.queue_wait_ms)
    result.setdefault('execution_ms', round((time.monotonic()-started)*1000))
    result['observation'] = capture.summary()
    return result


class Command(BaseCommand):
    help = 'Local-only bounded upstream census capture; outputs private JSON, no media download.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--authorization', required=True)
        parser.add_argument('--describe', action='store_true')

    def handle(self, *args, **options):
        if options['authorization'] != AUTHORIZATION:
            raise CommandError('Explicit census task authorization required')
        logging.disable(logging.CRITICAL)
        if options['describe']:
            data = {}
            for provider in PROVIDERS:
                try:
                    python = _python_for(provider)
                    data[provider] = {'configured': True, 'python': python,
                                      'version': EXPECTED_VERSIONS.get(provider, '')}
                    if provider == 'parsehub_generic':
                        from tools.upstream_parsehub_worker import PIN, VERSION
                        data[provider].update(pin=PIN, version=VERSION)
                    if provider == 'videodl_repo':
                        from tools.provider_lab.runtime.workers.videodl_worker import VIDEODL_PIN
                        data[provider].update(pin=VIDEODL_PIN, version='0.9.1')
                    if provider in {'tiantianjiexi_generic', 'kukutool_generic'}:
                        data[provider]['version'] = 'application_client_at_frozen_head; remote_service_unversioned'
                except Exception:
                    data[provider] = {'configured': False}
            self.stdout.write(json.dumps(data))
            return
        request = json.loads(sys.stdin.read(16385))
        if (not isinstance(request, dict) or set(request) != {'provider', 'url', 'timeout'}
                or request['provider'] not in PROVIDERS or type(request['timeout']) not in (int, float)
                or not .1 <= request['timeout'] <= 60):
            raise CommandError('Invalid census request')
        # Suppress third-party stdout and exceptions; raw observations only leave via private JSON.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                result = collect(request['provider'], request['url'], request['timeout'])
            except Exception as error:
                result = {'status': 'NOT_EXECUTED', 'executed': False,
                          'error': getattr(error, 'code', 'ADMISSION_FAILED'),
                          'error_class': type(error).__name__, 'application_compatibility': 'NOT_EVALUATED'}
        self.stdout.write(json.dumps(result, ensure_ascii=True))
