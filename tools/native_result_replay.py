"""Offline native-capture adapters for the real Worker and product converters.

Not a production parser. This module never loads project settings or credentials.
The CLI requires a capture and explicit source; use only a disposable process.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch


class OfflineNetworkAttempt(BaseException):
    pass


@contextmanager
def offline_network():
    def deny(*args, **kwargs):
        raise OfflineNetworkAttempt('HTTP/DNS prohibited in native replay')
    with ExitStack() as stack:
        for target in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyname_ex',
                       'socket.gethostbyaddr', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'http.client.HTTPConnection.connect', 'subprocess.Popen'):
            stack.enter_context(patch(target, side_effect=deny))
        yield


def configure_isolation():
    from pathlib import Path
    from django.conf import settings
    if settings.configured:
        raise RuntimeError('Replay CLI must not inherit application settings')
    settings.configure(
        BASE_DIR=Path(__file__).resolve().parents[1],
        ULTRA_HD_PROVIDER_ENABLED=False, KUKUTOOL_PROVIDER_ENABLED=False,
        THIRD_PARTY_PROVIDER_ENABLED=False, THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False,
        SECRET_KEY='offline-replay-only-not-an-application-key',
        INSTALLED_APPS=['django.contrib.auth', 'django.contrib.contenttypes', 'xhs_download'],
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
        CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                            'LOCATION': 'offline-native-replay'}},
        EMAIL_BACKEND='django.core.mail.backends.dummy.EmailBackend',
        USE_TZ=True, DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
        ALLOWED_HOSTS=['testserver'], ROOT_URLCONF='xhs_download.urls',
    )
    import django
    django.setup()


def restore_parsehub(value):
    # Fixed, reviewed type mapping. Never import a module named by an archive.
    from parsehub.types.media_ref import ImageRef, VideoRef, AniRef, LivePhotoRef
    from parsehub.types.result import RichTextParseResult, ImageParseResult, VideoParseResult
    from parsehub.parsers.parser.bilibili import BiliVideoParseResult
    from parsehub.parsers.parser.douyin import DouyinVideoParseResult
    from parsehub.types.platform import Platform
    types = {c.__name__: c for c in (ImageRef, VideoRef, AniRef, LivePhotoRef,
             RichTextParseResult, ImageParseResult, VideoParseResult,
             BiliVideoParseResult, DouyinVideoParseResult, Platform)}
    def restore(node):
        if isinstance(node, list):
            return [restore(item) for item in node]
        if not isinstance(node, dict):
            return node
        if '_class' not in node:
            return {key: restore(item) for key, item in node.items()}
        cls = types.get(node['_class'])
        if cls is None or set(node) != {'_class', 'fields'} or not isinstance(node['fields'], dict):
            raise ValueError('unreconstructable_provider_type')
        obj = object.__new__(cls)
        for key, item in node['fields'].items():
            if not key.isidentifier() or key.startswith('_'):
                raise ValueError('unreconstructable_provider_field')
            object.__setattr__(obj, key, restore(item))
        return obj
    return restore(value)


def worker_payload(provider, records, source):
    from tools import upstream_match_worker as worker
    records = deepcopy(records)
    if provider == 'yt_dlp_generic':
        from yt_dlp.extractor.common import InfoExtractor
        if len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError('multiple_native_stages_need_review')
        if records[0].get('_type') in {'playlist', 'multi_video', 'compat_list'}:
            raise ValueError('collection_export_not_reconstructed')
        class ReplayIE(InfoExtractor):
            _VALID_URL = r'https?://.+/(?P<id>[^/?]+)'
            def _real_extract(self, url):
                return records[0]
        return worker.parse_ytdlp(ReplayIE, source, 2)
    if provider == 'gallery_dl_generic':
        class ReplayGallery:
            pattern = r'https?://.+'
            def __init__(self, match):
                pass
            def __iter__(self):
                return iter(records)
        return worker.parse_gallery(ReplayGallery, source, 2)
    if provider == 'parsehub_generic':
        from tools.upstream_parsehub_worker import normalize_result
        if len(records) != 1:
            raise ValueError('multiple_native_stages_need_review')
        return normalize_result(restore_parsehub(records[0]), source)
    if provider == 'videodl_repo':
        from tools.provider_lab.runtime.workers.videodl_worker import normalize_result
        if len(records) != 1 or not isinstance(records[0], list):
            raise ValueError('unreconstructable_video_infos')
        infos = []
        for obj in records[0]:
            if not isinstance(obj, dict) or obj.get('_class') != 'VideoInfo':
                raise ValueError('unreconstructable_video_info_type')
            # VideoInfo's stored fields only; no constructor, properties or Backend.
            infos.append(SimpleNamespace(**obj['fields']))
        return normalize_result(infos, {'request_id': 'offline-replay', 'max_assets': 1000})
    if len(records) != 1:
        raise ValueError('multiple_native_stages_need_review')
    if provider == 'tiantianjiexi_generic':
        from xhs_download.services.platforms.tiantianjiexi_generic import (
            ensure_api_success, unwrap_api_payload, TianTianJieXiResultNormalizer)
        ensure_api_success(records[0])
        return TianTianJieXiResultNormalizer.normalize_payload(unwrap_api_payload(records[0]))
    if provider == 'kukutool_generic':
        # Capture is already after helper protocol decryption and business unwrap.
        return records[0]
    raise ValueError('unknown_provider')


@contextmanager
def isolated_boundaries():
    from xhs_download.services.common import XhsParseError
    from tools.upstream_match_worker import validate_media_url
    from django.core.cache import cache
    from django.core.cache.backends.locmem import LocMemCache
    from django.conf import settings
    if not isinstance(cache._connections['default'], LocMemCache) or settings.DATABASES['default']['NAME'] != ':memory:':
        raise RuntimeError('Replay requires isolated in-memory database and cache')
    cache.clear()  # Disposable locmem only, never the running application's cache.
    with ExitStack() as stack:
        for target in ('xhs_download.services.upstream_candidates.validate_public_download_url',
                       'xhs_download.services.upstream_delivery.validate_public_download_url'):
            stack.enter_context(patch(target, side_effect=validate_media_url))
        # No download manifests/admission. Candidate info uses test-only keys/cache.
        stack.enter_context(patch('xhs_download.services.upstream_delivery._normalize_downloadable_result',
                                  side_effect=XhsParseError('offline admission not evaluated')))
        stack.enter_context(patch('xhs_download.services.quality_execution._python_for', return_value=None))
        yield
    cache.clear()


def product_result(provider, payload, source):
    from xhs_download.services import quality_execution as execution
    match = {'provider_id': provider, 'extractor_id': provider,
             'version': execution.SERVICE_VERSIONS.get(provider, 'offline-capture'), 'label': provider}
    with ExitStack() as stack:
        if provider in execution.SERVICE_VERSIONS:
            name = 'tiantianjiexi_generic.TianTianJieXiGenericProvider' if provider == 'tiantianjiexi_generic' else 'kukutool_generic.KukutoolGenericProvider'
            stack.enter_context(patch('xhs_download.services.platforms.' + name + '.parse', return_value=payload))
        elif provider == 'videodl_repo':
            result, resources = payload
            # Execute the real Worker->payload map, replacing only Worker execution.
            stack.enter_context(patch('xhs_download.services.provider_lab.providers.run_videodl_repo',
                side_effect=lambda **kw: ({**result, 'request_id': kw['request_id']}, resources, [])))
        else:
            stack.enter_context(patch.object(execution, '_execute_upstream', return_value=(payload, match, {})))
        return execution.execute_provider(provider, source, service_authorized=True)


def replay(provider, records, source, *, payload=None):
    from xhs_download.services.provider_lab.providers import parse_result_to_lab
    from xhs_download.services.provider_lab.contracts import ProviderLabError
    from xhs_download.services.common import XhsParseError
    from xhs_download.services.platforms.tiantianjiexi_generic import TianTianJieXiApiError
    from tools.provider_lab.runtime.workers.worker_common import WorkerError
    aliases = {'yt_dlp_generic': 'yt_dlp_existing', 'gallery_dl_generic': 'gallery_dl_existing',
               'parsehub_generic': 'parsehub_repo', 'tiantianjiexi_generic': 'tiantian_existing',
               'kukutool_generic': 'kukutool_existing', 'videodl_repo': 'videodl_repo'}
    with offline_network(), isolated_boundaries():
        try:
            output = payload if payload is not None else worker_payload(provider, records, source)
            result = product_result(provider, output, source)
            lab, _ = parse_result_to_lab(result, provider_id=aliases[provider], user_id=1,
                                         request_id='offline-replay')
            def public(items):
                return [{k: v for k, v in item.items() if k not in
                         {'candidate_id', 'asset_id', 'resource_token', 'host_class'}} for item in items]
            return {'status': 'converted', 'candidates': public(result.metadata['upstream_candidates']),
                    'lab_assets': public(lab['assets']), 'download_admission': 'NOT_VERIFIED',
                    'counts': {k: result.metadata.get(k) for k in
                               ('returned_count', 'retained_count', 'rejected_count', 'unexpanded_count')}}
        except OfflineNetworkAttempt:
            raise
        except (ProviderLabError, XhsParseError, ValueError, WorkerError, TianTianJieXiApiError) as error:
            return {'status': 'conversion_rejected', 'error_class': type(error).__name__,
                    'error_code': getattr(error, 'code', ''), 'candidates': [], 'lab_assets': []}


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parsehub-payload', action='store_true')
    args = parser.parse_args()
    data = json.load(sys.stdin)
    with offline_network():
        if args.parsehub_payload:
            value = worker_payload('parsehub_generic', data['records'], data['source'])
        else:
            configure_isolation()
            value = replay(data['provider'], data['records'], data['source'])
    print(json.dumps(value, ensure_ascii=True))
