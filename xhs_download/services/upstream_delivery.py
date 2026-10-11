"""Short-lived authority for exact resources produced by matched upstream jobs."""

from dataclasses import replace
from hashlib import sha256
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.cache import cache

from .assets import MediaAsset, ParseResult
from .common import XhsParseError, clean_filename, validate_public_download_url
from .provider_manifest import build_provider_manifest_token, provider_manifest_is_valid
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


def upstream_manifest_item_is_allowed(item):
    if not isinstance(item, dict) or item.get('provider') not in PROVIDERS:
        return False
    match = ASSET_PATTERN.fullmatch(str(item.get('asset_id') or ''))
    if not match or not provider_manifest_is_valid(item):
        return False
    if item.get('type') != match[2] or item.get('source_platform') != 'upstream':
        return False
    try:
        record = cache.get(_key(item['asset_id']))
    except Exception:
        return False
    keys = ('asset_id', 'provider', 'provider_resource_id', 'provider_cache_key',
            'url', 'type', 'source_platform')
    return bool(isinstance(record, dict) and all(item.get(k) == record.get(k) for k in keys))


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


def normalize_upstream_result(payload, *, match, source_url):
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
