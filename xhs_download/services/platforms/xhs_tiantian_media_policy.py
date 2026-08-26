from dataclasses import dataclass
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from ..common import HEADERS, is_blocked_hostname
from ..provider_manifest import provider_manifest_is_valid


XHS_TIANTIAN_VIDEO_HOSTS = frozenset({"sns-video-hw.xhscdn.com"})
XHS_TIANTIAN_PROBE_MAX_BYTES = 4096
XHS_TIANTIAN_PROBE_MAX_REDIRECTS = 2
XHS_TIANTIAN_SOURCE_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
XHS_TIANTIAN_BINARY_VIDEO_CONTENT_TYPES = frozenset(
    {"application/octet-stream"}
)


class XhsTianTianMediaPolicyError(ValueError):
    def __init__(self, code):
        super().__init__(str(code or "XHS_TIANTIAN_MEDIA_REJECTED"))
        self.code = str(code or "XHS_TIANTIAN_MEDIA_REJECTED")


@dataclass(frozen=True)
class XhsTianTianProbeResult:
    final_url: str
    content_type: str
    bytes_read: int


def validate_xhs_tiantian_media_url(url):
    value = str(url or "").strip()
    if not value or len(value) > 4096:
        raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_URL_INVALID")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_URL_INVALID") from error

    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or hostname.endswith(".")
        or is_blocked_hostname(hostname)
        or hostname not in XHS_TIANTIAN_VIDEO_HOSTS
        or not str(parsed.path or "").strip("/")
    ):
        raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_HOST_REJECTED")
    return value


def xhs_tiantian_media_url_is_allowed(url):
    try:
        validate_xhs_tiantian_media_url(url)
    except XhsTianTianMediaPolicyError:
        return False
    return True


def xhs_tiantian_manifest_item_is_allowed(item):
    if not isinstance(item, dict):
        return False
    if str(item.get("provider") or "") != "tiantianjiexi_generic":
        return False
    if str(item.get("type") or "video").lower() != "video":
        return False
    source_key = str(item.get("provider_resource_id") or "")
    if not XHS_TIANTIAN_SOURCE_KEY_RE.fullmatch(source_key):
        return False
    if str(item.get("asset_id") or "") != (
        f"xhs:{source_key}:video:tiantianjiexi"
    ):
        return False
    if str(item.get("provider_cache_key") or "") != (
        f"third_party:tiantianjiexi_generic:{source_key}"
    ):
        return False
    if not provider_manifest_is_valid(item):
        return False
    return xhs_tiantian_media_url_is_allowed(item.get("url"))


def is_iso_bmff_video_prefix(content):
    value = bytes(content or b"")[:XHS_TIANTIAN_PROBE_MAX_BYTES]
    return len(value) >= 12 and value[4:8] == b"ftyp"


def _remaining_timeout(deadline_monotonic):
    deadline = float(deadline_monotonic or 0)
    if not deadline:
        return 3.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_DEADLINE_EXHAUSTED")
    return min(3.0, max(0.1, remaining))


def _read_probe_prefix(response):
    chunks = []
    bytes_read = 0
    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        remaining = XHS_TIANTIAN_PROBE_MAX_BYTES - bytes_read
        if remaining <= 0:
            break
        bounded = bytes(chunk[:remaining])
        chunks.append(bounded)
        bytes_read += len(bounded)
        if bytes_read >= XHS_TIANTIAN_PROBE_MAX_BYTES:
            break
    return b"".join(chunks), bytes_read


def probe_xhs_tiantian_video(
    url,
    *,
    deadline_monotonic=0,
    requester=requests.get,
):
    current_url = validate_xhs_tiantian_media_url(url)
    for redirect_index in range(XHS_TIANTIAN_PROBE_MAX_REDIRECTS + 1):
        response = None
        try:
            response = requester(
                current_url,
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.1",
                    "Range": f"bytes=0-{XHS_TIANTIAN_PROBE_MAX_BYTES - 1}",
                },
                timeout=_remaining_timeout(deadline_monotonic),
                allow_redirects=False,
                stream=True,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {301, 302, 303, 307, 308}:
                if redirect_index >= XHS_TIANTIAN_PROBE_MAX_REDIRECTS:
                    raise XhsTianTianMediaPolicyError(
                        "XHS_TIANTIAN_REDIRECT_LIMIT"
                    )
                location = str(response.headers.get("Location") or "").strip()
                current_url = validate_xhs_tiantian_media_url(
                    urljoin(current_url, location)
                )
                continue
            if status_code not in {200, 206}:
                raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_HTTP_REJECTED")

            final_url = validate_xhs_tiantian_media_url(
                str(getattr(response, "url", "") or current_url)
            )
            content_type = str(
                response.headers.get("Content-Type") or ""
            ).split(";", 1)[0].strip().lower()
            if not (
                content_type.startswith("video/")
                or content_type in XHS_TIANTIAN_BINARY_VIDEO_CONTENT_TYPES
            ):
                raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_MIME_REJECTED")
            content, bytes_read = _read_probe_prefix(response)
            if not is_iso_bmff_video_prefix(content):
                raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_FORMAT_REJECTED")
            return XhsTianTianProbeResult(
                final_url=final_url,
                content_type=content_type,
                bytes_read=bytes_read,
            )
        except requests.Timeout as error:
            raise XhsTianTianMediaPolicyError(
                "XHS_TIANTIAN_PROBE_TIMEOUT"
            ) from error
        except requests.RequestException as error:
            raise XhsTianTianMediaPolicyError(
                "XHS_TIANTIAN_PROBE_FAILED"
            ) from error
        finally:
            if response is not None:
                response.close()
    raise XhsTianTianMediaPolicyError("XHS_TIANTIAN_REDIRECT_LIMIT")
