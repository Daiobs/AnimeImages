"""One-shot metadata-only census worker, reachable only from local CLI."""

import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from census_capture import Capture, MAX_BYTES
import upstream_match_worker as boundary


def videodl_policy():
    # Load the pure policy file without importing the Django Lab package in its venv.
    path = Path(__file__).resolve().parents[1] / 'xhs_download/services/provider_lab/videodl_policy.py'
    spec = importlib.util.spec_from_file_location('_census_videodl_policy', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.backend_policy


def authorized_videodl_selection(options):
    if not isinstance(options, dict) or set(options) != {'backend', 'category', 'external_sharing', 'policy_revision', 'pin'}:
        raise ValueError('execution_denied')
    if options['policy_revision'] != 'guarded-tls-v3-full-inventory' or options['pin'] != '6cd1de0fe1209380b183b654c65e98a629637873':
        raise ValueError('policy_version_changed')
    backend, category = options['backend'], options['category']
    policy = videodl_policy()(backend, category, registry_present=True)
    if policy.controlled_state == 'blocked' or (policy.external_sharing_required and options['external_sharing'] is not True):
        raise ValueError('execution_denied')
    return backend, category


@contextlib.contextmanager
def direct_verified_requests(errors):
    import requests
    from unittest.mock import patch
    original = requests.Session.request
    def request(session, method, url, **kwargs):
        session.trust_env = False
        kwargs.update(verify=True, proxies={})
        try:
            return original(session, method, url, **kwargs)
        except Exception as error:
            if len(errors) < 40:
                errors.append(boundary.safe_error(error, stage='videodl_http'))
            raise
    with patch.object(requests.Session, 'request', request):
        yield


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
            native_matches = [cls for cls in classes if boundary.suitable(provider, cls, source)]
            matches = [cls for cls in native_matches
                       if (cls.ie_key() not in {'TestURL', 'UnsupportedURL'} if provider == 'yt_dlp_generic'
                            else cls.__name__ not in boundary.GALLERY_EXCLUDED)]
            if not matches:
                result['status'] = 'NOT_EXECUTED' if native_matches else 'UPSTREAM_NO_MATCH'
                if native_matches:
                    result.update(matched=True, error='KNOWN_WRONG_WORK_OR_UNSUPPORTED_EXTRACTOR_EXCLUDED')
                return result
            cls = matches[0]
            result.update(matched=True, backend=boundary.entry(provider, cls, identity['version'])['extractor_id'])
            boundary.install_network_guard(offline=False)
            result['executed'] = True
            with boundary.runtime_budget(timeout) as budget:
                output = (boundary.parse_ytdlp if provider == 'yt_dlp_generic' else boundary.parse_gallery)(cls, source, timeout, observe=capture)
                if provider == 'gallery_dl_generic':
                    scopes = [item[-1]['upstream_scope'] for item in output
                              if isinstance(item[-1], dict) and 'upstream_scope' in item[-1]]
                    if scopes:
                        result['worker_iteration_scope'] = scopes[0]
                        capture.truncated = True
                        capture.omissions.append('bounded_upstream_iteration')
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
            backend_policy = videodl_policy()
            vd.verify_install()
            backend = request['backend']
            category = 'dedicated'
            if request.get('execution_options'):
                backend, category = authorized_videodl_selection(request['execution_options'])
            elif backend_policy(backend, 'dedicated', registry_present=True).controlled_state != 'allowed':
                raise ValueError('execution_denied')
            result.update(matched=True, backend=backend, mode=category+'_explicit',
                          other_modes='NOT_ATTEMPTED', executed=True)
            boundary.install_network_guard(offline=False)
            result['transport_errors'] = []
            with direct_verified_requests(result['transport_errors']), boundary.runtime_budget(timeout) as budget:
                vd.run_exact_backend(source, backend, category, str(Path.cwd()/'provider-work'), observe=capture)
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
