"""One-shot metadata-only census worker, reachable only from local CLI."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from census_capture import Capture, MAX_BYTES
import upstream_match_worker as boundary


def collect(request, capture):
    provider, source = request['provider'], boundary.validate_url(request['url'])
    timeout = min(60.0, max(.1, float(request.get('timeout', 60))))
    result = {'executed': False, 'matched': False, 'cache_hit': False,
              'application_compatibility': 'NOT_EVALUATED', 'capture_source': 'Provider native object export'}
    budget = None
    try:
        if provider in boundary.PROVIDERS:
            identity = boundary.package_identity(provider)
            if identity['version'] != request['version']:
                raise ValueError('version_changed')
            classes = boundary.extractor_classes(provider)
            # Census admits native Generic but still excludes synthetic / known wrong-work paths.
            matches = [cls for cls in classes if boundary.suitable(provider, cls, source)
                       and (cls.ie_key() not in {'TestURL', 'UnsupportedURL'} if provider == 'yt_dlp_generic'
                            else cls.__name__ not in boundary.GALLERY_EXCLUDED)]
            if not matches:
                result['status'] = 'UPSTREAM_NO_MATCH'
                return result
            cls = matches[0]
            result.update(matched=True, backend=boundary.entry(provider, cls, identity['version'])['extractor_id'])
            boundary.install_network_guard(offline=False)
            result['executed'] = True
            with boundary.runtime_budget(timeout) as budget:
                (boundary.parse_ytdlp if provider == 'yt_dlp_generic' else boundary.parse_gallery)(cls, source, timeout, observe=capture)
            result['internal_http'] = budget['requests']
            result['network_bytes'] = budget['bytes']
        elif provider == 'parsehub_generic':
            import upstream_parsehub_worker as ph
            ph.verify_install()
            matches = ph.match_url(source)
            if not matches:
                result['status'] = 'UPSTREAM_NO_MATCH'
                return result
            cls = ph.import_parser(matches[0]['extractor_id'])
            result.update(matched=True, backend=cls.__name__)
            budget = ph.Budget(timeout)
            ph.install_execution_guard(budget)
            result['executed'] = True
            asyncio.run(ph.parse(source, timeout, cls, budget, observe=capture))
            result['internal_http'] = 'UNKNOWN'
            result['network_bytes'] = budget.wire_bytes
            result['delegations'] = budget.delegations
        elif provider == 'videodl_repo':
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from tools.provider_lab.runtime.workers import videodl_worker as vd
            from xhs_download.services.provider_lab.videodl_policy import backend_policy
            vd.verify_install()
            backend = request['backend']
            if backend_policy(backend, 'dedicated', registry_present=True).controlled_state != 'allowed':
                raise ValueError('execution_denied')
            result.update(matched=True, backend=backend, mode='dedicated_only',
                          other_modes='NOT_ATTEMPTED', executed=True)
            boundary.install_network_guard(offline=False)
            with boundary.runtime_budget(timeout) as budget:
                vd.run_exact_backend(source, backend, 'dedicated', str(Path.cwd()/'provider-work'), observe=capture)
        else:
            raise ValueError('invalid_request')
        result['status'] = 'RETURNED'
    except Exception as error:
        failure = boundary.safe_error(error)
        result.update(status='FAILED', error=failure['error'], stage=failure['stage'],
                      error_class=failure['error_class'])
        if isinstance(error, TimeoutError):
            result['status'] = 'OUTCOME_UNKNOWN'
    finally:
        if provider in boundary.PROVIDERS:
            result['provider_attempts'] = capture.attempts
        elif provider == 'parsehub_generic' and budget is not None:
            result['provider_attempts'] = int(result['executed']) + budget.delegations
        if isinstance(budget, dict):
            result.update(internal_http=budget['requests'], network_bytes=budget['bytes'])
        elif budget is not None:
            result.update(internal_http=budget.requests, network_bytes=budget.wire_bytes,
                          delegations=budget.delegations)
    return result


def main():
    request = json.loads(sys.stdin.read(16385))
    for key in tuple(os.environ):
        if key not in {'PATH', 'HOME', 'LANG', 'TMPDIR'}:
            os.environ.pop(key, None)
    os.environ['YTDLP_NO_PLUGINS'] = '1'
    os.environ['PYCRYPTODOME_DISABLE_GMP'] = '1'
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (65, 65))
    capture = Capture()
    start = time.monotonic()
    with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        result = collect(request, capture)
    result.update(observation=capture.summary(), execution_ms=round((time.monotonic()-start)*1000))
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
