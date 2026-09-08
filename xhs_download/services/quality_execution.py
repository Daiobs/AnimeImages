"""One metadata executor and candidate normalizer for Lab and ordinary routing."""

from dataclasses import replace
from pathlib import Path
import time
from uuid import uuid4

from .env_config import runtime_bool, runtime_str
from .provider_lab.contracts import ProviderLabError

PROVIDER_ALIASES = {
    'yt_dlp_existing': 'yt_dlp_generic', 'yt_dlp_generic': 'yt_dlp_generic',
    'gallery_dl_existing': 'gallery_dl_generic', 'gallery_dl_generic': 'gallery_dl_generic',
    'parsehub_repo': 'parsehub_generic', 'parsehub_generic': 'parsehub_generic',
    'videodl_repo': 'videodl_repo',
}
EXPECTED_VERSIONS = {'yt_dlp_generic':'2026.3.17', 'gallery_dl_generic':'1.32.4'}


def canonical_provider_id(provider_id):
    try:
        return PROVIDER_ALIASES[provider_id]
    except (KeyError, TypeError):
        raise ProviderLabError('PROVIDER_NOT_SELECTED') from None


def _python_for(provider_id):
    if provider_id == 'parsehub_generic':
        if not runtime_bool('PARSEHUB_UPSTREAM_ENABLED', False):
            raise ProviderLabError('PROVIDER_NOT_CONFIGURED')
        python = runtime_str('PARSEHUB_UPSTREAM_PYTHON')
        if not Path(python).is_absolute() or not Path(python).is_file():
            raise ProviderLabError('PROVIDER_NOT_CONFIGURED')
        return python
    if provider_id == 'videodl_repo':
        if not runtime_bool('VIDEODL_PROVIDER_LAB_ENABLED', False):
            raise ProviderLabError('PROVIDER_LICENSE_RESTRICTED')
        python = runtime_str('VIDEODL_PROVIDER_LAB_PYTHON', '/tmp/animeimages-provider-lab/videodl/bin/python')
        if not Path(python).is_file():
            raise ProviderLabError('PROVIDER_NOT_CONFIGURED')
        return python
    enabled = 'YTDLP_PROVIDER_ENABLED' if provider_id == 'yt_dlp_generic' else 'GALLERYDL_PROVIDER_ENABLED'
    if not runtime_bool(enabled, False):
        raise ProviderLabError('PROVIDER_NOT_CONFIGURED')
    from .upstream_matching import _executor
    try:
        command, _ = _executor(provider_id)
        return command[0]
    except Exception:
        raise ProviderLabError('PROVIDER_NOT_CONFIGURED') from None


def provider_available(provider_id):
    try:
        _python_for(canonical_provider_id(provider_id))
        return True
    except ProviderLabError:
        return False


def match_saved_videodl(source_url, options=None, *, timeout_seconds=5.0):
    """Match an explicit saved route using reviewed, isolated dedicated backends.

    This is route execution capability, never download-host authorization.
    Configuration/policy failures are explicit; a supported empty match is ().
    """
    from .provider_lab.providers import normalized_source, WORKER_ROOT
    from .provider_lab.runtime import execute_provider_worker
    from .provider_lab.videodl_control import load_videodl_inventory, MAX_MATCHED_DEDICATED
    from .provider_lab.videodl_policy import VIDEODL_POLICY_PIN, backend_policy
    source = normalized_source(source_url)
    options = {'mode':'dedicated_only'} if options is None else options
    if not isinstance(options, dict):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    selected = None
    if options != {'mode':'dedicated_only'}:
        if (set(options) != {'mode','backend_id'} or options.get('mode') != 'specific_backend'
                or not isinstance(options.get('backend_id'),str)):
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        selected = options['backend_id']
        if backend_policy(selected, 'dedicated', registry_present=True).controlled_state != 'allowed':
            raise ProviderLabError('PROVIDER_POLICY_REJECTED')
    python = _python_for('videodl_repo')
    worker = WORKER_ROOT / 'videodl_worker.py'
    request_id = uuid4().hex
    deadline = time.monotonic() + min(10.0, max(.1, float(timeout_seconds)))
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ProviderLabError('PROVIDER_TIMEOUT')
        return value
    inventory = load_videodl_inventory(python=python, worker=worker,
        request_id=request_id, timeout_seconds=remaining())
    dedicated = {item.backend_id:item for item in inventory if item.backend_category == 'dedicated'}
    if selected is not None and (selected not in dedicated or not dedicated[selected].controlled_available):
        raise ProviderLabError('PROVIDER_POLICY_REJECTED')
    payload = execute_provider_worker(python=python, worker=worker, request_id=request_id,
        source=source, timeout_seconds=remaining(), operation='match_dedicated')
    matched = payload.get('backend_ids')
    if (not isinstance(matched,list) or len(matched) > 16
            or any(not isinstance(item,str) or item not in dedicated for item in matched)):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    considered = matched if selected is not None else matched[:MAX_MATCHED_DEDICATED]
    allowed = [item for item in dict.fromkeys(considered)
               if dedicated[item].controlled_available and (selected is None or item == selected)]
    if not allowed:
        return ()
    return tuple({'provider_id':'videodl_repo', 'extractor_id':backend_id,
                  'label':'videodl dedicated', 'version':f'videodl@{VIDEODL_POLICY_PIN}'}
                 for backend_id in allowed)


def _videodl_payload(source, *, backend=None, options=None, timeout_seconds=20.0):
    from .provider_lab.providers import run_videodl_repo
    from .provider_lab.videodl_control import VideodlExecutionOptions
    from .provider_lab.videodl_policy import VIDEODL_POLICY_PIN
    if backend is not None and options is not None:
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    if backend is not None:
        # Saved noncommercial routes may select a reviewed dedicated backend;
        # service-sharing confirmation remains a separate per-request decision.
        if not isinstance(backend, str):
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        options = VideodlExecutionOptions(mode='specific_backend', backend_ids=(backend,),
                                         backend_category='dedicated')
    if options is None or options == {'mode':'dedicated_only'}:
        options = VideodlExecutionOptions(mode='dedicated_only', backend_category='dedicated')
    elif isinstance(options, dict):
        if set(options) != {'mode','backend_id'} or options.get('mode') != 'specific_backend':
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        options = VideodlExecutionOptions(mode='specific_backend',
            backend_ids=(options['backend_id'],), backend_category='dedicated')
    elif not isinstance(options, VideodlExecutionOptions):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    request_id = uuid4().hex
    result, resources, attempts = run_videodl_repo(
        source=source, request_id=request_id, user_id=0, options=options,
        timeout_seconds=timeout_seconds)
    if (result.get('provider_id') != 'videodl_repo' or result.get('request_id') != request_id
            or result.get('provider_version') != f'videodl@{VIDEODL_POLICY_PIN}'
            or result.get('status') != 'success'):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    by_id = {resource.get('asset_id'): resource for resource in resources if isinstance(resource, dict)}
    if len(by_id) != len(resources):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    rows = []
    for asset in result.get('assets') or ():
        record = by_id.get(asset.get('asset_id'))
        if not record:
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        rows.append([3, record.get('url'), {
            **asset, 'media_type':asset.get('media_type'), 'extension':asset.get('declared_extension'),
            'duration':asset.get('duration_seconds'), 'title':result.get('metadata',{}).get('title'),
            'headers_present':record.get('headers_present') is True,
            'headers':record.get('headers') or {},
            'requires_cookie':record.get('requires_cookie') is True,
        }])
    match = {'provider_id':'videodl_repo', 'version':result.get('provider_version',''),
             'extractor_id':result.get('selected_backend') or 'videodl', 'label':'videodl'}
    return rows, match, {'attempts':[attempt.as_dict() for attempt in attempts],
                          'warnings':result.get('warnings') or []}


def _select_match(matches, provider, expected_match=None):
    if expected_match is not None and (
            not isinstance(expected_match, dict) or expected_match.get('provider_id') != provider
            or any(not isinstance(expected_match.get(key), str) or not expected_match[key]
                   for key in ('extractor_id', 'version'))):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    if not matches:
        raise ProviderLabError('PROVIDER_UNSUPPORTED')
    if expected_match is None:
        return matches[0]
    for match in matches:
        if isinstance(match, dict) and all(match.get(key) == expected_match[key]
                                          for key in ('provider_id', 'extractor_id', 'version')):
            return match
    raise ProviderLabError('PROVIDER_UNSUPPORTED')


def _execute_upstream(provider, source, timeout_seconds, *, expected_match=None):
    """Use the product worker transport and installed registries, not Lab parsers."""
    from . import upstream_matching, upstream_parsehub
    deadline = time.monotonic() + min(30.0, max(.1, float(timeout_seconds)))
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ProviderLabError('PROVIDER_TIMEOUT')
        return value
    try:
        if provider == 'parsehub_generic':
            matches = upstream_parsehub._call({'action':'match','url':source}, remaining())
            if not isinstance(matches,list) or not all(upstream_parsehub.valid_match(m) for m in matches):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            match = _select_match(matches, provider, expected_match)
            payload = upstream_parsehub.parse_upstream_url(match, source, remaining())
        else:
            command, version, _ = upstream_matching._context(provider)
            if version != EXPECTED_VERSIONS[provider]:
                raise ProviderLabError('PROVIDER_NOT_CONFIGURED')
            request = {'provider_id':provider,'version':version,'url':source,'allow_mature':True}
            matches = upstream_matching._call(command, {**request,'action':'match'}, remaining())
            if not isinstance(matches,list):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            match = _select_match(matches, provider, expected_match)
            if (not isinstance(match,dict) or match.get('provider_id') != provider
                    or match.get('version') != version):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            payload = upstream_matching._call(command, {**request,'action':'parse',
                'extractor_id':match['extractor_id'],'timeout_seconds':remaining()},remaining())
        return payload, match, {}
    except (upstream_matching.UpstreamMatchingError, upstream_parsehub.ParseHubBridgeError) as error:
        code = error.code
        lab_code = ('PROVIDER_TIMEOUT' if 'timeout' in code else
                    'PROVIDER_NOT_CONFIGURED' if code in {'disabled','unavailable','executor_unavailable','version_changed'} else
                    'PROVIDER_UNSUPPORTED' if code == 'extractor_mismatch' else
                    'PROVIDER_RESULT_EMPTY' if code == 'no_formats' else 'PROVIDER_PROCESS_FAILED')
        raise ProviderLabError(lab_code) from None


def _normalize_videodl_candidates(payload, match, source):
    """Candidate-only admission never creates a download manifest or host grant."""
    from .assets import ParseResult
    from .upstream_candidates import MAX_CANDIDATES, candidate_url, issue_candidates
    candidates = []
    rejected = 0
    examined = 0
    for _, url, metadata in payload:
        if len(candidates) >= MAX_CANDIDATES:
            break
        examined += 1
        url = candidate_url(url)
        if not url:
            rejected += 1
            continue
        candidates.append(dict(url=url, metadata=metadata, ordinal=len(candidates)+1,
            provider='videodl_repo',source_url=source,match=match,media_type=metadata.get('media_type','unknown'),
            state='separate_media_tracks' if metadata.get('requires_merge') else 'candidate_only'))
    if not candidates:
        raise ProviderLabError('PROVIDER_RESULT_EMPTY')
    candidates = issue_candidates(candidates)
    from .provider_lab.contracts import safe_public_text
    title = safe_public_text(payload[0][2].get('title') or 'videodl', 300)
    return ParseResult(platform='third_party',post_type='video' if any(c['media_type']=='video' for c in candidates) else 'image',
        base_filename='videodl',assets=(),metadata={
            'title':title,'upstream_candidates':candidates,'source_provider':'videodl_repo',
            'selected_provider':'videodl_repo','source_platform':'upstream','canonical_source_url':source,
            'upstream_extractor':match['extractor_id'],'upstream_version':match['version'],
            'candidates_truncated':max(0,len(payload)-len(candidates)-rejected),
            'candidate_count':len(payload), 'candidate_returned_count':len(payload),
            'candidate_retained_count':len(candidates),
            'candidate_unexpanded_count':max(0,len(payload)-len(candidates)-rejected),
            'returned_count':len(payload), 'retained_count':len(candidates),
            'declared_count':len(payload), 'examined_count':examined, 'rejected_count':rejected,
            'unexpanded_count':max(0,len(payload)-len(candidates)-rejected),
            'truncated_count':max(0,len(payload)-len(candidates)-rejected),
            'rejected_candidate_count':rejected,'limited_media_count':len(candidates),
        })


def execute_provider(provider_id, source_url, *, timeout_seconds=20.0, backend=None, options=None,
                     expected_match=None):
    """Execute only the requested provider, independent of saved route preference.

    Results are successful when candidates exist, including unknown/separate
    tracks and streams. This function never selects a winner or saves a route.
    """
    from .provider_lab.providers import normalized_source
    from .upstream_delivery import normalize_upstream_result
    provider = canonical_provider_id(provider_id)
    source = normalized_source(source_url)
    _python_for(provider)
    if provider == 'videodl_repo':
        if expected_match is not None:
            from .provider_lab.videodl_policy import VIDEODL_POLICY_PIN
            _select_match([expected_match], provider, expected_match)
            if (expected_match['version'] != f'videodl@{VIDEODL_POLICY_PIN}'
                    or options is not None or backend not in (None, expected_match['extractor_id'])):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            backend = expected_match['extractor_id']
        payload, match, execution = _videodl_payload(source, backend=backend, options=options,
                                                   timeout_seconds=timeout_seconds)
        if expected_match is not None:
            _select_match([match], provider, expected_match)
    else:
        if backend is not None or options is not None:
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        payload, match, execution = _execute_upstream(provider,source,timeout_seconds,
                                                      expected_match=expected_match)
    result = (_normalize_videodl_candidates(payload,match,source) if provider == 'videodl_repo' else
              normalize_upstream_result(payload, match=match, source_url=source))
    metadata = dict(result.metadata)
    metadata.update({key:execution[key] for key in ('warnings','unexpanded_scope','attempts') if key in execution})
    metadata['truncated'] = bool(execution.get('truncated') or metadata.get('candidates_truncated'))
    upstream_count = execution.get('upstream_format_count')
    if isinstance(upstream_count, int):
        metadata['candidate_count'] = upstream_count
        metadata['candidates_truncated'] = max(metadata.get('candidates_truncated',0),
            upstream_count - len(metadata.get('upstream_candidates') or ()))
    return replace(result, metadata=metadata)
