import logging

from django.conf import settings
from django.core import signing


logger = logging.getLogger(__name__)
PROVIDER_MANIFEST_SALT = "xhs_download.provider_manifest.v1"


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


def provider_manifest_is_valid(item):
    token = str(item.get("provider_manifest_token") or "")
    if not token:
        return False
    try:
        payload = signing.loads(
            token,
            salt=PROVIDER_MANIFEST_SALT,
            max_age=max(
                1,
                int(
                    getattr(
                        settings,
                        "PROVIDER_MANIFEST_MAX_AGE_SECONDS",
                        3600,
                    )
                    or 3600
                ),
            ),
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
