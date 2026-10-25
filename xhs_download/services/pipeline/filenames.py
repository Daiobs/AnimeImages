"""Download filenames (docs/architecture/unified-pipeline.md §4, INV-06)."""
import re

MAX_BASE_LENGTH = 80

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")
_EXTENSION = re.compile(r"[a-z0-9]{1,5}")

EXTENSION_ALIASES = {"jpeg": "jpg", "jpe": "jpg", "quicktime": "mov", "mpeg4": "mp4"}
PLACEHOLDER_EXTENSIONS = frozenset({"bin", "dat", "tmp", "file", "unknown", "octet"})
CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-flv": "flv",
    "video/mp2t": "ts",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "image/avif": "avif",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/webm": "webm",
}


class UnknownExtension(ValueError):
    pass


def sanitize(text):
    text = _SPACES.sub(" ", _ILLEGAL.sub(" ", text or "")).strip(" .")
    return text[:MAX_BASE_LENGTH].rstrip(" .")


def base_name(work):
    """Short title; without one, platform_workid. Description and topics never."""
    return sanitize(work.title) or sanitize(f"{work.platform}_{work.work_id}")


def item_numbers(work):
    """item_id -> number from 1 by index; a group shares one number.

    A work with a single unit (one item, or one live photo) gets no numbers.
    """
    group_of = {
        item_id: position
        for position, group in enumerate(work.groups)
        for item_id in group.item_ids
    }
    units = {}
    numbers = {}
    for item in sorted(work.items, key=lambda item: item.index):
        unit = ("group", group_of[item.item_id]) if item.item_id in group_of else ("item", item.item_id)
        numbers[item.item_id] = units.setdefault(unit, len(units) + 1)
    if len(units) == 1:
        return {item_id: None for item_id in numbers}
    return numbers


def _clean_extension(value):
    value = EXTENSION_ALIASES.get(value, value)
    if value in PLACEHOLDER_EXTENSIONS or not _EXTENSION.fullmatch(value):
        return ""
    return value


def extension(variant, content_type=""):
    """Real extension from the container, else the response Content-Type."""
    found = _clean_extension((variant.container or "").strip().lstrip(".").lower())
    if found:
        return found
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    found = CONTENT_TYPE_EXTENSIONS.get(media_type, "")
    if found:
        return found
    raise UnknownExtension("no real extension for this variant")


def file_name(work, item, variant, content_type=""):
    number = item_numbers(work)[item.item_id]
    stem = base_name(work)
    if number is not None:
        stem = f"{stem}_{number}"
    return f"{stem}.{extension(variant, content_type)}"


def archive_name(work):
    return f"{base_name(work)}.zip"
