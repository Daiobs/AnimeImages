from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


IMAGE_FORMATS = {
    "jpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "gif": ("gif", "image/gif"),
    "webp": ("webp", "image/webp"),
    "avif": ("avif", "image/avif"),
}

CONTENT_TYPE_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
}

URL_EXTENSION_FORMATS = {
    "jpeg": "jpeg",
    "jpg": "jpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    "avif": "avif",
}

GENERIC_BINARY_CONTENT_TYPES = {
    "application/binary",
    "application/download",
    "application/octet-stream",
    "binary/octet-stream",
}


@dataclass(frozen=True)
class ImageFormatResolution:
    format: str = ""
    extension: str = ""
    mime_type: str = ""
    confidence: str = ""
    error_code: str = ""
    metadata_mismatch: bool = False

    @property
    def valid(self):
        return bool(self.format and self.extension and self.mime_type)


def normalize_image_content_type(value):
    return str(value or "").split(";", 1)[0].strip().lower()


def prefix_looks_like_non_image(value):
    prefix = bytes(value or b"")[:512].lstrip().lower()
    return prefix.startswith(
        (
            b"<!doctype html",
            b"<html",
            b"<head",
            b"<body",
            b"<?xml",
            b"<svg",
            b"{",
            b"[",
        )
    )


def detect_image_format_from_prefix(value):
    prefix = bytes(value or b"")
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "webp"
    if len(prefix) >= 16 and prefix[4:8] == b"ftyp":
        box_size = int.from_bytes(prefix[:4], "big")
        if box_size >= 16:
            box_end = min(len(prefix), box_size)
            brands = [prefix[8:12]]
            brands.extend(
                prefix[index:index + 4]
                for index in range(16, box_end, 4)
                if len(prefix[index:index + 4]) == 4
            )
            if any(brand in {b"avif", b"avis"} for brand in brands):
                return "avif"
    return ""


def image_format_from_url(url):
    suffix = Path(urlparse(str(url or "")).path).suffix.lower().lstrip(".")
    return URL_EXTENSION_FORMATS.get(suffix, "")


def image_format_resolution(format_name, confidence, *, metadata_mismatch=False):
    extension, mime_type = IMAGE_FORMATS[format_name]
    return ImageFormatResolution(
        format=format_name,
        extension=extension,
        mime_type=mime_type,
        confidence=confidence,
        metadata_mismatch=metadata_mismatch,
    )


def resolve_image_format(prefix=b"", *, content_type="", url=""):
    prefix = bytes(prefix or b"")
    declared_content_type = normalize_image_content_type(content_type)
    declared_format = CONTENT_TYPE_FORMATS.get(declared_content_type, "")
    magic_format = detect_image_format_from_prefix(prefix)

    if magic_format:
        return image_format_resolution(
            magic_format,
            "magic",
            metadata_mismatch=bool(
                declared_format and declared_format != magic_format
            ),
        )

    if prefix_looks_like_non_image(prefix):
        return ImageFormatResolution(error_code="IMAGE_CONTENT_INVALID")

    if declared_format:
        return image_format_resolution(declared_format, "content_type")

    if declared_content_type and (
        declared_content_type.startswith("text/")
        or declared_content_type in {
            "application/json",
            "application/xml",
            "image/svg+xml",
        }
    ):
        return ImageFormatResolution(error_code="IMAGE_CONTENT_INVALID")

    url_format = image_format_from_url(url)
    if url_format:
        return image_format_resolution(url_format, "url_suffix")

    return ImageFormatResolution(error_code="IMAGE_FORMAT_UNDETERMINED")
