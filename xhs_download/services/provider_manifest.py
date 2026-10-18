import logging
import hashlib
from time import time

from django.conf import settings
from django.core import signing


logger = logging.getLogger(__name__)
PROVIDER_MANIFEST_SALT = "xhs_download.provider_manifest.v1"


def douyin_format_asset_id(aweme_id, provider, url):
    digest = hashlib.sha256(str(url).encode()).hexdigest()[:24]
    return f"douyin:{aweme_id}:video:{provider}:format:{digest}"


def douyin_format_manifest_is_valid(item):
    """Extra formats retain work identity but cannot borrow primary recovery."""
    provider = item.get('provider')
    aweme_id = str(item.get('provider_resource_id') or '')
    if (provider not in {'tiantianjiexi', 'kukutool'} or not aweme_id.isdigit()
            or item.get('type') != 'video' or item.get('source_platform') != 'douyin'
            or item.get('provider_cache_key') != f'douyin:{provider}:validated:{aweme_id}'
            or item.get('asset_id') != douyin_format_asset_id(aweme_id, provider, item.get('url'))
            or not provider_manifest_is_valid(item)):
        return False
    from .platforms.douyin_media_policy import validate_douyin_media_url
    from .platforms.douyin_kukutool import is_byte_cdn_url, parse_byte_cdn_url
    try:
        validate_douyin_media_url(item.get('url'))
        metadata = parse_byte_cdn_url(item.get('url'))
        if metadata['expires_at'] and metadata['expires_at'] <= int(time()) + 30:
            return False
        if provider == 'kukutool':
            return bool(is_byte_cdn_url(item.get('url'))
                        and metadata['object_key'] and metadata['expires_at'])
    except (TypeError, ValueError):
        return False
    return True


def build_provider_manifest_token(
    *,
    asset_id,
    provider,
    provider_resource_id,
    provider_cache_key,
    url,
    media_type="",
    source_platform="",
):
    payload = {
        "asset_id": str(asset_id or ""),
        "provider": str(provider or ""),
        "provider_resource_id": str(provider_resource_id or ""),
        "provider_cache_key": str(provider_cache_key or ""),
        "url": str(url or ""),
    }
    if media_type:
        payload["type"] = str(media_type or "")
    if source_platform:
        payload["source_platform"] = str(source_platform or "")
    return signing.dumps(payload, salt=PROVIDER_MANIFEST_SALT, compress=True)


def provider_manifest_max_age_seconds():
    return max(1, int(getattr(settings, "PROVIDER_MANIFEST_MAX_AGE_SECONDS", 3600) or 3600))


def provider_manifest_expires_at(token):
    """Read the original authenticated timestamp without issuing a new token."""
    signer = signing.TimestampSigner(salt=PROVIDER_MANIFEST_SALT)
    max_age = provider_manifest_max_age_seconds()
    try:
        signer.unsign(token, max_age=max_age)
    except signing.BadSignature as error:
        raise ValueError("provider_manifest_invalid") from error
    issued_at = signing.b62_decode(token.rsplit(signer.sep, 2)[1])
    return issued_at + max_age


def provider_manifest_is_valid(item):
    token = str(item.get("provider_manifest_token") or "")
    if not token:
        return False
    try:
        payload = signing.loads(
            token,
            salt=PROVIDER_MANIFEST_SALT,
            max_age=provider_manifest_max_age_seconds(),
        )
    except signing.BadSignature:
        logger.warning("provider_download_manifest_signature_rejected")
        return False

    expected = {
        "asset_id": str(item.get("asset_id") or ""),
        "provider": str(item.get("provider") or ""),
        "provider_resource_id": str(item.get("provider_resource_id") or ""),
        "provider_cache_key": str(item.get("provider_cache_key") or ""),
        "url": str(item.get("url") or ""),
    }
    if "type" in payload:
        expected["type"] = str(item.get("type") or "")
    if "source_platform" in payload:
        expected["source_platform"] = str(item.get("source_platform") or "")
    return payload == expected
