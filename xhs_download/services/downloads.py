from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

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


def build_download_archive(media_items, base_filename, save_format="jpg", quality=95, video_save_mode="video"):
    if not media_items:
        raise XhsDownloadError("没有选择资源")
    if len(media_items) > MAX_DOWNLOAD_ITEMS:
        raise XhsDownloadError(f"一次最多下载 {MAX_DOWNLOAD_ITEMS} 个资源")

    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    archive_buffer = BytesIO()
    saved_count = 0
    failures = []

    with ZipFile(archive_buffer, "w", ZIP_DEFLATED) as archive:
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
                    archive.writestr(filename, save_image_to_buffer(image, save_format, quality))
                    saved_count += 1

                elif media_type == "video":
                    if video_save_mode in ["video", "both"]:
                        ext = guess_video_extension(url)
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        archive.writestr(filename, fetch_binary(url, timeout=60, max_bytes=MAX_VIDEO_BYTES))
                        saved_count += 1

                    if video_save_mode in ["cover", "both"]:
                        cover_url = item.get("cover_url")
                        if not cover_url:
                            raise XhsDownloadError("当前视频没有解析到封面")
                        content = fetch_binary(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                        image = Image.open(BytesIO(content))
                        filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                        archive.writestr(filename, save_image_to_buffer(image, save_format, quality))
                        saved_count += 1
                else:
                    raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

            except Exception as error:
                failures.append(f"{media_type}: {url or '-'} -> {error}")

        if failures:
            archive.writestr("failed.txt", "\n".join(failures))

    if saved_count == 0:
        raise XhsDownloadError("没有资源保存成功")

    return {
        "filename": clean_filename(f"{base_filename}.zip"),
        "content": archive_buffer.getvalue(),
        "saved_count": saved_count,
        "failed_count": len(failures),
    }
