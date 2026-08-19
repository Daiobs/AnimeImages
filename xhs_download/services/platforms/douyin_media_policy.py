from urllib.parse import urlparse

from ..common import XhsSecurityError, host_matches, is_blocked_hostname


DOUYIN_MEDIA_HOST_SUFFIXES = (
    "amemv.com",
    "snssdk.com",
    "douyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinstatic.com",
    "byteimg.com",
    "bytetos.com",
    "365yg.com",
    "zjcdn.com",
)
DOUYIN_REVIEWED_EXACT_MEDIA_HOSTS = frozenset(
    {"v3-dy-a-x.ixigua.com"}
)


def douyin_media_host_policy(hostname, *, existing_suffixes=None):
    hostname = str(hostname or "").strip().lower()
    if not hostname or hostname.endswith(".") or is_blocked_hostname(hostname):
        return ""
    suffixes = (
        DOUYIN_MEDIA_HOST_SUFFIXES
        if existing_suffixes is None
        else tuple(existing_suffixes)
    )
    if host_matches(hostname, suffixes):
        return "existing"
    if hostname in DOUYIN_REVIEWED_EXACT_MEDIA_HOSTS:
        return "ixigua_exact"
    return ""


def is_allowed_douyin_media_hostname(hostname, *, existing_suffixes=None):
    return bool(
        douyin_media_host_policy(
            hostname,
            existing_suffixes=existing_suffixes,
        )
    )


def validate_douyin_media_url(
    url,
    message="资源 URL 不在允许的抖音媒体域名内",
    *,
    existing_suffixes=None,
):
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise XhsSecurityError(message)

    policy = douyin_media_host_policy(
        parsed.hostname,
        existing_suffixes=existing_suffixes,
    )
    if not policy:
        raise XhsSecurityError(message)
    if policy == "ixigua_exact":
        try:
            port = parsed.port
        except ValueError as error:
            raise XhsSecurityError(message) from error
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise XhsSecurityError(message)
    return url
