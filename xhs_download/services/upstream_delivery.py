"""Short-lived authority for exact resources produced by matched upstream jobs."""

from dataclasses import replace
from hashlib import sha256
from math import isfinite
import re
from time import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.cache import cache

from .assets import MediaAsset, ParseResult
from .common import XhsParseError, clean_filename, validate_public_download_url
from .provider_manifest import build_provider_manifest_token, provider_manifest_is_valid, provider_manifest_expires_at
from .provider_access import UPSTREAM_PROVIDER_IDS
from .platforms.gallerydl_generic import media_type_from_metadata, extension_from_url, VIDEO_EXTENSIONS
from .platforms.ytdlp_generic import (
    YtDlpResultNormalizer, format_url_is_direct_http,
    ytdlp_format_has_explicit_complete_video, ytdlp_format_score,
    ytdlp_size_is_estimate, integer_value,
)


PROVIDERS = frozenset(UPSTREAM_PROVIDER_IDS)
RESOURCE_TTL = 300
MAX_ASSETS = 30
ASSET_PATTERN = re.compile(r'^upstream:([a-f0-9]{32}):(image|video):([1-9][0-9]?)$')


def safe_resource_url(value):
    url = str(value or '').strip()
    try:
        parsed = urlsplit(url)
        if (not url or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url)
                or '\\' in url or parsed.scheme != 'https'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            return ''
        validate_public_download_url(url)
    except (ValueError, XhsParseError):
        return ''
    return url


def _key(asset_id):
    return 'upstream:asset:v1:' + sha256(asset_id.encode()).hexdigest()


def _authorized_resource_record(item):
    if not isinstance(item, dict) or item.get('provider') not in PROVIDERS:
        return None
    match = ASSET_PATTERN.fullmatch(str(item.get('asset_id') or ''))
    if not match or not provider_manifest_is_valid(item):
        return None
    if item.get('type') != match[2] or item.get('source_platform') != 'upstream':
        return None
    try:
        record = cache.get(_key(item['asset_id']))
    except Exception:
        return None
    keys = ('asset_id', 'provider', 'provider_resource_id', 'provider_cache_key',
            'url', 'type', 'source_platform')
    if not (isinstance(record, dict) and all(item.get(k) == record.get(k) for k in keys)
            and provider_manifest_is_valid(record)):
        return None
    if 'expires_at' in record:
        expires_at = record['expires_at']
        if (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not isfinite(expires_at) or expires_at <= time()):
            return None
    return record


def upstream_manifest_item_is_allowed(item):
    return _authorized_resource_record(item) is not None


def upstream_video_resource_binding(item):
    """Project original issuer authority; reading a route never renews it."""
    record = _authorized_resource_record(item)
    if record is None or record.get('type') != 'video':
        return {}
    binding = {key: record[key] for key in (
        'asset_id', 'provider', 'provider_resource_id', 'provider_cache_key',
        'provider_manifest_token', 'url', 'type', 'source_platform')}
    binding['selection_schema'] = 'upstream_resource_v1'
    if 'expires_at' not in record:
        binding['streaming_unsupported_reason'] = 'missing_resource_expiry'
    else:
        try:
            manifest_expires_at = provider_manifest_expires_at(record['provider_manifest_token'])
        except ValueError:
            return {}
        binding['selection_expires_at'] = int(min(
            record['expires_at'], manifest_expires_at) * 1000)
    return binding


def _issue_assets(assets, *, match, source_url):
    job_id = uuid4().hex
    source_key = sha256(source_url.encode()).hexdigest()
    result = []
    for index, asset in enumerate(assets, 1):
        asset_id = f'upstream:{job_id}:{asset.media_type}:{index}'
        fields = dict(asset_id=asset_id, provider=match['provider_id'],
                      provider_resource_id=source_key,
                      provider_cache_key=f'upstream:{job_id}', source_platform='upstream')
        token = build_provider_manifest_token(**fields, url=asset.url, media_type=asset.media_type)
        bound = replace(asset, **fields, provider_manifest_token=token)
        record = bound.to_download_item()
        if asset.media_type == 'video':
            record['expires_at'] = min(time() + RESOURCE_TTL, provider_manifest_expires_at(token))
        # Only issuer-produced exact URLs enter this cache. No client-supplied host grant.
        cache.set(_key(asset_id), record, timeout=RESOURCE_TTL)
        result.append(bound)
    return tuple(result)


class UpstreamMediaError(XhsParseError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _has_complete_bmff_tracks(probe):
    return (isinstance(probe, dict) and probe.get('schema') == 'iso_bmff_tracks_v1'
            and probe.get('complete_moov') is True and probe.get('audio') == 'present'
            and probe.get('video') == 'present' and probe.get('reason') == 'complete_track_table')


def video_evidence_state(item):
    if not isinstance(item, dict) or not item.get('url'):
        return 'no_formats'
    if not format_url_is_direct_http(item):
        return 'streaming_media'
    evidence = item.get('upstream_track_evidence', item)
    if not isinstance(evidence, dict):
        return 'unknown_media_tracks'
    if any(str(evidence.get(key) or '').lower() == 'none' for key in ('acodec', 'vcodec')):
        return 'separate_media_tracks'
    if ytdlp_format_has_explicit_complete_video(evidence):
        return 'complete'
    probe = item.get('upstream_bmff_track_evidence')
    if _has_complete_bmff_tracks(probe):
        return 'complete'
    return 'unknown_media_tracks'


class _UnboundVideoNormalizer(YtDlpResultNormalizer):
    def build_provider_asset_fields(self, *args, **kwargs):
        return {}

    def collect_video_formats(self, payload, allowed_suffixes, platform_id):
        formats = payload.get('formats') or [payload]
        candidates = []
        states = set()
        for item in formats:
            state = video_evidence_state(item)
            states.add(state)
            if state != 'complete':
                continue
            url = safe_resource_url(item.get('url'))
            if not url:
                continue
            candidates.append(dict(
                url=url, quality_label=str(item.get('format_note') or item.get('format_id') or 'video'),
                size_bytes=integer_value(item.get('filesize') or item.get('filesize_approx')),
                size_is_estimate=ytdlp_size_is_estimate(item), score=ytdlp_format_score(item)))
        if not candidates:
            for code in ('unknown_media_tracks', 'streaming_media', 'separate_media_tracks'):
                if code in states:
                    raise UpstreamMediaError(code)
            raise UpstreamMediaError('no_formats')
        return sorted(candidates, key=lambda item: item['score'], reverse=True)


def _normalize_downloadable_result(payload, *, match, source_url):
    provider = match.get('provider_id')
    limited_media_count = 0
    if provider not in PROVIDERS:
        raise XhsParseError('扩展解析器不可用。')
    if provider == 'yt_dlp_generic':
        if not isinstance(payload, dict) or payload.get('_type', 'video') not in ('video', None):
            raise XhsParseError('扩展解析只接受单条内容，不支持账号或播放列表。')
        formats = payload.get('formats') or []
        if not isinstance(formats, list) or len(formats) > 1000:
            raise XhsParseError('上游返回的格式数量超出限制。')
        # Admit only actual format/thumbnail candidates, never arbitrary metadata URLs.
        urls = [payload.get('url'), payload.get('thumbnail')]
        urls += [item.get('url') for item in formats if isinstance(item, dict)]
        hosts = set()
        for url in urls:
            try:
                host = urlsplit(str(url or '')).hostname
            except ValueError:
                continue
            if host and host not in hosts and safe_resource_url(url):
                hosts.add(host)
        rule = SimpleNamespace(platform_id='upstream', media_host_suffixes=tuple(hosts))
        try:
            normalized = _UnboundVideoNormalizer().normalize(
                rule=rule, platform_name=match['label'], source_url=source_url, payload=payload)
        except UpstreamMediaError:
            raise
        except ValueError as error:
            raise XhsParseError('解析结果没有可直接保存的完整视频；分离音视频或流媒体暂不支持。') from error
        formats_by_url = {f.get('url'): f for f in formats if isinstance(f, dict)}
        assets = tuple(replace(a, ext=str(formats_by_url.get(a.url, payload).get('ext')
                                         or extension_from_url(a.url) or 'mp4').lower())
                       for a in normalized.assets)
        title = normalized.base_filename
    else:
        if not isinstance(payload, list) or len(payload) > MAX_ASSETS + 10:
            raise XhsParseError('上游图片结果超出单条内容限制。')
        assets = []
        title = match['label']
        seen = set()
        for message in payload:
            # gallery-dl URL messages are direct item evidence; do not recurse metadata.
            if not isinstance(message, (tuple, list)) or len(message) != 3 or message[0] != 3:
                continue
            url, metadata = message[1:]
            if not isinstance(metadata, dict):
                continue
            media_type = media_type_from_metadata(metadata, url)
            if media_type == 'video':
                # Evidence belongs to this message, never a nested or sibling URL.
                candidate = {**metadata, 'url': url}
                explicit_none = any(str(metadata.get(key) or '').lower() == 'none'
                                    for key in ('acodec', 'vcodec'))
                if (provider != 'parsehub_generic' or explicit_none
                        or not _has_complete_bmff_tracks(metadata.get('upstream_bmff_track_evidence'))
                        or video_evidence_state(candidate) != 'complete'):
                    limited_media_count += 1
                    continue
            if media_type not in ('image', 'video') or url in seen:
                continue
            url = safe_resource_url(url)
            if not url:
                continue
            seen.add(url)
            title = str(metadata.get('title') or metadata.get('filename') or title)
            ext = str(metadata.get('extension') or extension_from_url(url)).lower().lstrip('.')
            if media_type == 'video' and f'.{ext}' not in VIDEO_EXTENSIONS:
                url_ext = extension_from_url(url)
                ext = url_ext if f'.{url_ext}' in VIDEO_EXTENSIONS else 'mp4'
            assets.append(MediaAsset(media_type=media_type, url=url, platform='third_party',
                                     preview_url=url if media_type == 'image' else '', ext=ext))
            if len(assets) > MAX_ASSETS:
                raise XhsParseError('上游图片结果超出单条内容限制。')
    checked = []
    for asset in assets:
        if not safe_resource_url(asset.url):
            continue
        cover = safe_resource_url(asset.cover_url) if asset.cover_url else ''
        checked.append(replace(asset, cover_url=cover, cover_preview_url=cover, headers={}))
    if not checked:
        if limited_media_count:
            raise UpstreamMediaError('unknown_media_tracks')
        raise XhsParseError('解析完成，但没有符合公开单条资源要求的图片或完整视频。')
    return ParseResult(
        platform='third_party', post_type='video' if any(a.media_type == 'video' for a in checked) else 'image',
        base_filename=clean_filename(title)[:180] or 'media',
        assets=_issue_assets(checked, match=match, source_url=source_url),
        metadata=dict(source_platform='upstream', source_platform_name=match['label'],
                      selected_provider=provider, source_provider=provider, provider_label=match['label'],
                      upstream_extractor=match['extractor_id'], upstream_version=match['version'],
                      canonical_source_url=source_url, coverage_status='matched_attemptable',
                      limited_media_count=limited_media_count,
                      delivery_limitation='浏览器跨域或来源限制可能阻止保存；不会自动转为服务器下载。'))


def normalize_upstream_result(payload, *, match, source_url):
    """Preserve bounded valid candidates even when no download asset is admitted."""
    from .upstream_candidates import MAX_CANDIDATES, candidate_url, issue_candidates
    from .provider_lab.contracts import safe_public_text

    provider = match.get('provider_id')
    if provider not in PROVIDERS:
        raise XhsParseError('扩展解析器不可用。')
    title = match.get('label') or 'media'
    author = ''
    rows = []
    scope = {}
    unexpanded_unknown = False
    collection = False
    if provider == 'yt_dlp_generic':
        if not isinstance(payload, dict):
            raise XhsParseError('扩展解析只接受单条内容。')
        if isinstance(payload.get('upstream_scope'), dict):
            scope = payload['upstream_scope']
        collection = payload.get('_type') in {'playlist', 'multi_video', 'compat_list'}
        if collection:
            scope = payload.get('upstream_scope') or {}
            if scope.get('bounded') is not True or not isinstance(payload.get('entries'), list):
                raise XhsParseError('集合结果未提供有界范围。')
        elif payload.get('_type', 'video') not in {'video', None}:
            raise XhsParseError('扩展解析只接受有界媒体结果。')
        title = payload.get('title') or title
        author = payload.get('uploader') or payload.get('creator') or ''
        input_count = 0
        nodes = 0

        def visit(node, depth=0):
            nonlocal nodes, input_count, unexpanded_unknown, scope
            if nodes >= 101 or depth >= 16:
                unexpanded_unknown = True
                return
            nodes += 1
            if not isinstance(node, dict):
                return
            node_scope = node.get('upstream_scope') or {}
            if node_scope:
                scope = node_scope
            if node.get('_type') in {'playlist', 'multi_video', 'compat_list'}:
                entries = node.get('entries')
                if node_scope.get('bounded') is not True or not isinstance(entries, list):
                    unexpanded_unknown = True
                    return
                if node_scope.get('completion') != 'complete':
                    unexpanded_unknown = True
                for child in entries:
                    if nodes >= 101:
                        unexpanded_unknown = True
                        break
                    visit(child, depth + 1)
                return
            if node.get('_type', 'video') not in {'video', None}:
                unexpanded_unknown = True
                return
            formats = node.get('formats') or ([node] if node.get('url') else [])
            if not isinstance(formats, list):
                raise XhsParseError('上游格式结构无效。')
            total = node_scope.get('format_total')
            input_count += max(len(formats), total if type(total) is int and 0 <= total <= 1000000 else 0)
            for item in formats[:max(0, MAX_CANDIDATES - len(rows))]:
                if isinstance(item, dict):
                    rows.append((item.get('url'), {
                        'duration': node.get('duration'), 'work_label': node.get('title'), **item}))

        visit(payload)
    else:
        if not isinstance(payload, list):
            raise XhsParseError('上游媒体结构无效。')
        rows = [(row[1], row[2]) for row in payload
                if isinstance(row, (tuple, list)) and len(row) == 3 and row[0] == 3
                and isinstance(row[2], dict)]
        input_count = len(rows)
        rows = rows[:MAX_CANDIDATES]
    candidates = []
    admitted_rows = []
    rejected = 0
    examined = 0
    for url, metadata in rows:
        if len(candidates) >= MAX_CANDIDATES:
            break
        examined += 1
        if isinstance(metadata.get('upstream_scope'), dict):
            scope = metadata['upstream_scope']
            if scope.get('unexpanded_count') is None and scope.get('completion') != 'complete':
                unexpanded_unknown = True
        total = metadata.get('candidate_total')
        if metadata.get('candidates_truncated') is True and type(total) is int and 0 <= total <= 1000000:
            input_count = max(input_count, total)
        evidence = metadata.get('upstream_track_evidence')
        if isinstance(evidence, dict):
            metadata = {**metadata, **{key: ('none' if str(metadata.get(key) or '').lower() == 'none'
                else evidence.get(key)) for key in ('acodec', 'vcodec')}}
        safe_url = candidate_url(url, validator=validate_public_download_url)
        if not safe_url:
            rejected += 1
            continue
        title = metadata.get('title') or title
        author = metadata.get('author') or metadata.get('uploader') or author
        media_type = str(metadata.get('media_type') or metadata.get('type') or '')
        if media_type not in {'image', 'video', 'audio'}:
            media_type = media_type_from_metadata(metadata, url)
        if not media_type and (
                str(metadata.get('extension') or metadata.get('ext') or '').lower().lstrip('.')
                in {'m4a', 'mp3', 'aac', 'ogg', 'opus', 'flac', 'wav'}
                or str(metadata.get('mime_type') or metadata.get('mime') or '').lower().startswith('audio/')):
            media_type = 'audio'
        if provider == 'yt_dlp_generic':
            if metadata.get('vcodec') == 'none':
                media_type = 'audio'
            elif media_type not in {'image', 'audio'}:
                media_type = 'video'
        if media_type not in {'image', 'video', 'audio'}:
            media_type = 'unknown'
        state = 'image' if media_type == 'image' else video_evidence_state({**metadata, 'url': url})
        if state == 'complete' and _has_complete_bmff_tracks(metadata.get('upstream_bmff_track_evidence')):
            metadata = {**metadata, 'audio_tracks': 'present', 'video_tracks': 'present'}
        if media_type == 'audio':
            state = 'audio_only'
        elif metadata.get('acodec') == 'none' and metadata.get('vcodec') not in {None, '', 'none'}:
            state = 'silent_video'
        if urlsplit(url).scheme == 'http':
            state = 'insecure_reference'
        candidates.append(dict(url=safe_url, metadata=metadata, ordinal=len(candidates) + 1,
                                           provider=provider, source_url=source_url,
                                           media_type=media_type, state=state, match=match))
        admitted_rows.append((safe_url, metadata))
    if not candidates:
        raise XhsParseError('上游没有返回有效的公网媒体候选。')
    work = None
    if provider == 'yt_dlp_generic' and not collection:
        work = {'title': str(title), 'author': str(author), 'duration': payload.get('duration'),
                'source_host': urlsplit(source_url).hostname or '', 'collection': False,
                'cover_url': safe_resource_url(payload.get('thumbnail'))}
    candidates = issue_candidates(candidates, work=work)
    assets = ()
    if provider == 'yt_dlp_generic':
        downloadable_payload = {**payload, 'formats': [dict(meta, url=url) for url, meta in admitted_rows]}
    else:
        downloadable_payload = [[3, url, meta] for url, meta in admitted_rows[:MAX_ASSETS]]
    if not collection:
        try:
            assets = _normalize_downloadable_result(downloadable_payload, match=match, source_url=source_url).assets
        except XhsParseError:
            # Admission failure limits actions; it does not erase a successful parse.
            pass
    return ParseResult(
        platform='third_party', post_type=('video' if any(c['media_type'] == 'video' for c in candidates)
                                          else 'audio' if all(c['media_type'] == 'audio' for c in candidates)
                                          else 'image'),
        base_filename=clean_filename(str(title))[:180] or 'media', assets=assets,
        metadata=dict(
            source_platform='upstream', source_platform_name=match.get('label', provider),
            title=safe_public_text(title, 300), author_name=safe_public_text(author, 200),
            selected_provider=provider, source_provider=provider, provider_label=match.get('label', provider),
            upstream_extractor=match.get('extractor_id', ''), upstream_version=match.get('version', ''),
            canonical_source_url=source_url, coverage_status='matched_attemptable',
            upstream_candidates=candidates, candidates_truncated=max(0, input_count - len(candidates) - rejected),
            candidate_count=input_count, candidate_returned_count=input_count,
            candidate_retained_count=len(candidates),
            candidate_unexpanded_count=None if unexpanded_unknown else max(0, input_count - len(candidates) - rejected),
            returned_count=input_count, retained_count=len(candidates),
            declared_count=input_count, examined_count=examined, rejected_count=rejected,
            unexpanded_count=None if unexpanded_unknown else max(0, input_count - len(candidates) - rejected),
            truncated_count=max(0, input_count - len(candidates) - rejected),
            unexpanded_scope={'bounded': True, 'completion': 'unknown'} if scope else {},
            rejected_candidate_count=rejected, limited_media_count=max(0, len(candidates) - len(assets)),
            delivery_limitation='解析候选已保留；预览、自动保存、合流和归档能力单独判断。',
        ),
    )
