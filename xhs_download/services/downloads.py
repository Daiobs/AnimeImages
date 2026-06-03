from pathlib import Path
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from PIL import Image

from .common import HEADERS, XhsDownloadError, clean_filename, request_get_with_retry, validate_media_url


MAX_DOWNLOAD_ITEMS = 30
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024


def normalize_save_format(save_format):
    save_format = (save_format or "jpg").lower().strip()
    if save_format == "jpeg":
        save_format = "jpg"
    if save_format not in ["jpg", "png", "webp"]:
        save_format = "jpg"
    return save_format


def normalize_quality(quality):
    try:
        quality = int(quality)
    except Exception:
        quality = 95
    return max(1, min(quality, 100))


def normalize_video_save_mode(mode):
    mode = (mode or "video").lower().strip()
    if mode not in ["video", "cover", "both"]:
        mode = "video"
    return mode


def guess_video_extension(url):
    url = (url or "").lower()
    if ".m3u8" in url:
        return "m3u8"
    if ".mov" in url:
        return "mov"
    return "mp4"


def save_image_to_buffer(image, save_format, quality):
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    buffer = BytesIO()

    if save_format == "png":
        image.save(buffer, "PNG", optimize=True)
    elif save_format == "webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(buffer, "WEBP", quality=quality, method=6)
    else:
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)

    return buffer.getvalue()


def read_limited_response(response, max_bytes):
    chunks = []
    total = 0

    for chunk in response.iter_content(chunk_size=1024 * 256):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")
        chunks.append(chunk)

    return b"".join(chunks)


def fetch_binary(url, headers=None, timeout=30, max_bytes=MAX_IMAGE_BYTES):
    validate_media_url(url)
    response = request_get_with_retry(url, headers=headers or HEADERS, timeout=timeout, retries=3, stream=True)
    with response:
        return read_limited_response(response, max_bytes=max_bytes)


def iter_download_files(media_items, base_filename, save_format="jpg", quality=95, video_save_mode="video"):
    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    failures = []

    for index, item in enumerate(media_items, start=1):
        media_type = item.get("type", "image")
        url = item.get("url")

        try:
            if not url:
                raise XhsDownloadError("资源 URL 为空")

            if media_type == "image":
                content = fetch_binary(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                image = Image.open(BytesIO(content))
                filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                yield filename, save_image_to_buffer(image, save_format, quality)

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    ext = guess_video_extension(url)
                    filename = clean_filename(f"{base_filename}_{index}.{ext}")
                    yield filename, fetch_binary(url, timeout=60, max_bytes=MAX_VIDEO_BYTES)

                if video_save_mode in ["cover", "both"]:
                    cover_url = item.get("cover_url")
                    if not cover_url:
                        raise XhsDownloadError("当前视频没有解析到封面")
                    content = fetch_binary(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                    image = Image.open(BytesIO(content))
                    filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                    yield filename, save_image_to_buffer(image, save_format, quality)

            else:
                raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

        except Exception as error:
            failures.append(f"{media_type}: {url or '-'} -> {error}")

    if failures:
        yield "failed.txt", "\n".join(failures).encode("utf-8")


def build_download_archive(media_items, base_filename, save_format="jpg", quality=95, video_save_mode="video"):
    if not media_items:
        raise XhsDownloadError("没有选择资源")
    if len(media_items) > MAX_DOWNLOAD_ITEMS:
        raise XhsDownloadError(f"一次最多下载 {MAX_DOWNLOAD_ITEMS} 个资源")

    base_filename = clean_filename(base_filename or "xhs")
    archive_buffer = BytesIO()
    saved_count = 0
    failed_count = 0

    with ZipFile(archive_buffer, "w", ZIP_DEFLATED) as archive:
        for filename, content in iter_download_files(media_items, base_filename, save_format, quality, video_save_mode):
            archive.writestr(filename, content)
            if filename == "failed.txt":
                failed_count = content.decode("utf-8").count("\n") + 1 if content else 0
            else:
                saved_count += 1

    if saved_count == 0:
        raise XhsDownloadError("没有资源保存成功")

    return {
        "filename": clean_filename(f"{base_filename}.zip"),
        "content": archive_buffer.getvalue(),
        "saved_count": saved_count,
        "failed_count": failed_count,
    }


def resolve_server_save_folder(save_dir, allow_absolute=False):
    save_dir = (save_dir or settings.XHS_DEFAULT_SERVER_SAVE_DIR or "xhs").strip()
    if not save_dir:
        save_dir = "xhs"

    candidate = Path(save_dir).expanduser()
    if candidate.is_absolute():
        if not allow_absolute:
            raise XhsDownloadError("当前环境不允许保存到绝对路径")
        return candidate

    media_root = Path(settings.MEDIA_ROOT).resolve()
    target = (media_root / candidate).resolve()
    try:
        target.relative_to(media_root)
    except ValueError as error:
        raise XhsDownloadError("保存路径不能跳出 MEDIA_ROOT") from error
    return target


def save_media_items_to_server(
    media_items,
    base_filename,
    save_dir=None,
    save_format="jpg",
    quality=95,
    video_save_mode="video",
    allow_absolute=False,
):
    if not media_items:
        raise XhsDownloadError("没有选择资源")
    if len(media_items) > MAX_DOWNLOAD_ITEMS:
        raise XhsDownloadError(f"一次最多下载 {MAX_DOWNLOAD_ITEMS} 个资源")

    save_folder = resolve_server_save_folder(save_dir, allow_absolute=allow_absolute)
    save_folder.mkdir(parents=True, exist_ok=True)

    saved_files = []
    failed_count = 0
    for filename, content in iter_download_files(media_items, base_filename, save_format, quality, video_save_mode):
        if filename == "failed.txt":
            failed_count = content.decode("utf-8").count("\n") + 1 if content else 0
            (save_folder / filename).write_bytes(content)
            continue

        target = save_folder / filename
        target.write_bytes(content)
        saved_files.append(str(target))

    if not saved_files:
        raise XhsDownloadError("没有资源保存成功")

    return {
        "save_folder": str(save_folder),
        "saved_count": len(saved_files),
        "failed_count": failed_count,
        "saved_files": saved_files,
    }
