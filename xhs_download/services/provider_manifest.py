import logging

from django.conf import settings
from django.core import signing


logger = logging.getLogger(__name__)
PROVIDER_MANIFEST_SALT = "xhs_download.provider_manifest.v1"
PROVIDER_MANIFEST_VALID = "valid"
PROVIDER_MANIFEST_EXPIRED = "expired"
PROVIDER_MANIFEST_INVALID = "invalid"


def provider_manifest_max_age_seconds():
    return max(
        1,
        int(
            getattr(
                settings,
                "PROVIDER_MANIFEST_MAX_AGE_SECONDS",
                3600,
            )
            or 3600
        ),
    )


def build_provider_manifest_token(
    *,
    asset_id,
    provider,
    provider_resource_id,
    provider_cache_key,
    url,
    media_type="",
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
    return signing.dumps(payload, salt=PROVIDER_MANIFEST_SALT, compress=True)


def provider_manifest_validation_status(item):
    token = str(item.get("provider_manifest_token") or "")
    if not token:
        return PROVIDER_MANIFEST_INVALID
    try:
        payload = signing.loads(
            token,
            salt=PROVIDER_MANIFEST_SALT,
            max_age=provider_manifest_max_age_seconds(),
        )
    except signing.SignatureExpired:
        logger.info("provider_download_manifest_expired")
        return PROVIDER_MANIFEST_EXPIRED
    except signing.BadSignature:
        logger.warning("provider_download_manifest_signature_rejected")
        return PROVIDER_MANIFEST_INVALID

    expected = {
        "asset_id": str(item.get("asset_id") or ""),
        "provider": str(item.get("provider") or ""),
        "provider_resource_id": str(item.get("provider_resource_id") or ""),
        "provider_cache_key": str(item.get("provider_cache_key") or ""),
        "url": str(item.get("url") or ""),
    }
    if "type" in payload:
        expected["type"] = str(item.get("type") or "")
    if payload != expected:
        logger.warning("provider_download_manifest_payload_rejected")
        return PROVIDER_MANIFEST_INVALID
    return PROVIDER_MANIFEST_VALID


def provider_manifest_is_valid(item):
    return provider_manifest_validation_status(item) == PROVIDER_MANIFEST_VALID


def provider_manifest_expires_at(token):
    """Return a verified manifest expiry timestamp without exposing its payload."""
    value = str(token or "")
    if not value:
        return 0
    signer = signing.TimestampSigner(salt=PROVIDER_MANIFEST_SALT)
    try:
        signer.unsign(value)
        parts = value.rsplit(signer.sep, 2)
        if len(parts) != 3:
            return 0
        issued_at = signing.b62_decode(parts[1])
    except (signing.BadSignature, TypeError, ValueError):
        return 0
    return int(issued_at) + provider_manifest_max_age_seconds()
