import re
import warnings
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError

from .rate_limits import consume_fixed_window, get_client_identity


FEEDBACK_MAX_MESSAGE_LENGTH = 1200
FEEDBACK_MAX_CONTACT_LENGTH = 256
FEEDBACK_MAX_IMAGE_BYTES = 5 * 1024 * 1024
FEEDBACK_MAX_PAYLOAD_BYTES = FEEDBACK_MAX_IMAGE_BYTES + (128 * 1024)
FEEDBACK_RATE_LIMIT = 5
FEEDBACK_RATE_WINDOW_SECONDS = 10 * 60
FEEDBACK_REDACTION = "[敏感内容已隐藏]"
FEEDBACK_URL_REDACTION = "[链接已隐藏]"

FEEDBACK_SOURCE_PLATFORMS = frozenset(
    {
        "home",
        "xhs",
        "tiktok",
        "bilibili",
        "batch",
    }
)
FEEDBACK_ALLOWED_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
SAFE_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
URL_RE = re.compile(
    r"(?i)(?:(?:https?:)?//[^\s<>]+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}/[^\s<>?]*\?[^\s<>]+)"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?im)(?:[\"']?\b(?:"
    r"xsec_token|token|signature|sig|x-amz-signature|authorization|cookie|"
    r"password|passwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|provider[_-]?(?:credential|token|key)|"
    r"secret"
    r")\b[\"']?\s*[:=]\s*)[^\r\n]+"
)
BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_feedback_message(value):
    message = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    message = CONTROL_CHARACTER_RE.sub("", message).strip()
    message = URL_RE.sub(FEEDBACK_URL_REDACTION, message)
    message = SENSITIVE_ASSIGNMENT_RE.sub(FEEDBACK_REDACTION, message)
    message = BEARER_TOKEN_RE.sub(f"Bearer {FEEDBACK_REDACTION}", message)
    message = re.sub(r"\n{3,}", "\n\n", message).strip()
    return message[:FEEDBACK_MAX_MESSAGE_LENGTH].strip()


def sanitize_feedback_contact(value):
    contact = CONTROL_CHARACTER_RE.sub("", str(value or ""))
    contact = re.sub(r"\s+", " ", contact).strip()
    if len(contact) > FEEDBACK_MAX_CONTACT_LENGTH:
        raise ValueError(f"联系方式不能超过 {FEEDBACK_MAX_CONTACT_LENGTH} 个字符。")
    return contact


def sanitize_feedback_page_path(value):
    raw_value = CONTROL_CHARACTER_RE.sub("", str(value or "").strip())
    if not raw_value:
        return "/"

    parsed = urlsplit(raw_value)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path[:255]


def sanitize_feedback_request_id(value):
    request_id = str(value or "").strip()
    if SAFE_REQUEST_ID_RE.fullmatch(request_id):
        return request_id
    return ""


def sanitize_feedback_source_platform(value):
    source_platform = str(value or "").strip().lower()
    if source_platform in FEEDBACK_SOURCE_PLATFORMS:
        return source_platform
    return ""


def normalize_feedback_viewport_dimension(value):
    try:
        dimension = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= dimension <= 10000:
        return dimension
    return None


def validate_feedback_image(upload):
    if not upload:
        return None

    size = int(getattr(upload, "size", 0) or 0)
    if size <= 0:
        raise ValueError("上传的图片为空，请重新选择。")
    if size > FEEDBACK_MAX_IMAGE_BYTES:
        raise ValueError("问题截图不能超过 5 MB。")

    image_format = ""
    try:
        upload.seek(0)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(upload) as image:
                image_format = str(image.format or "").upper()
                image.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ) as exc:
        raise ValueError("仅支持有效的 JPG、PNG 或 WebP 图片。") from exc
    finally:
        try:
            upload.seek(0)
        except (AttributeError, OSError):
            pass

    image_metadata = FEEDBACK_ALLOWED_IMAGE_FORMATS.get(image_format)
    if image_metadata is None:
        raise ValueError("仅支持有效的 JPG、PNG 或 WebP 图片。")

    content_type, suffix = image_metadata
    upload.name = f"feedback-image{suffix}"
    upload.content_type = content_type
    return upload


def feedback_rate_limit_result(request):
    return consume_fixed_window(
        "user-feedback-submit",
        get_client_identity(request),
        FEEDBACK_RATE_LIMIT,
        FEEDBACK_RATE_WINDOW_SECONDS,
    )


def build_user_feedback_values(payload, *, image=None, request_id=""):
    raw_message = str(payload.get("message") or "")
    if len(raw_message) > FEEDBACK_MAX_MESSAGE_LENGTH:
        raise ValueError(f"反馈内容不能超过 {FEEDBACK_MAX_MESSAGE_LENGTH} 个字符。")

    message = sanitize_feedback_message(raw_message)
    if not message:
        raise ValueError("请填写反馈内容。")

    return {
        "message": message,
        "contact": sanitize_feedback_contact(payload.get("contact")),
        "image": validate_feedback_image(image),
        "page_path": sanitize_feedback_page_path(payload.get("page_path")),
        "request_id": sanitize_feedback_request_id(request_id),
        "source_platform": sanitize_feedback_source_platform(
            payload.get("source_platform")
        ),
        "viewport_width": normalize_feedback_viewport_dimension(
            payload.get("viewport_width")
        ),
        "viewport_height": normalize_feedback_viewport_dimension(
            payload.get("viewport_height")
        ),
    }
