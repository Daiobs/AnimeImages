import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import requests
from django.conf import settings
from PIL import Image

from .common import (
    HEADERS,
    MEDIA_HOST_SUFFIXES,
    XhsDownloadError,
    clean_filename,
    host_matches,
    request_get_with_retry,
    validate_external_url,
    validate_media_url,
)
from .platforms.bilibili import BILIBILI_HEADERS, BILIBILI_MEDIA_HOST_SUFFIXES
from .platforms.douyin import DOUYIN_HEADERS, DOUYIN_MEDIA_HOST_SUFFIXES


MAX_DOWNLOAD_ITEMS = 30
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 256
ORIGINAL_FORMAT = "original"


def get_media_headers(url, platform=None):
    if platform == "bilibili":
        return BILIBILI_HEADERS
    if platform == "douyin":
        return DOUYIN_HEADERS

    hostname = (urlparse(url or "").hostname or "").lower()
    if host_matches(hostname, BILIBILI_MEDIA_HOST_SUFFIXES):
        return BILIBILI_HEADERS
    if host_matches(hostname, DOUYIN_MEDIA_HOST_SUFFIXES):
        return DOUYIN_HEADERS

    return HEADERS


def normalize_save_format(save_format):
    save_format = (save_format or ORIGINAL_FORMAT).lower().strip()
    if save_format == "jpeg":
        save_format = "jpg"
    if save_format not in [ORIGINAL_FORMAT, "jpg", "png", "webp"]:
        save_format = ORIGINAL_FORMAT
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


IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}

VIDEO_CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "application/vnd.apple.mpegurl": "m3u8",
    "application/x-mpegurl": "m3u8",
}


def get_extension_from_url(url, allowed_extensions):
    suffix = Path(urlparse(url or "").path).suffix.lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    return suffix if suffix in allowed_extensions else None


def get_extension_from_content_type(content_type, mapping):
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return mapping.get(content_type)


def guess_image_extension(url, content_type=None):
    return (
        get_extension_from_content_type(content_type, IMAGE_CONTENT_TYPE_EXTENSIONS)
        or get_extension_from_url(url, set(IMAGE_CONTENT_TYPE_EXTENSIONS.values()))
        or "jpg"
    )


def guess_video_extension_from_response(url, content_type=None):
    return (
        get_extension_from_content_type(content_type, VIDEO_CONTENT_TYPE_EXTENSIONS)
        or guess_video_extension(url)
    )


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


def parse_content_length(response):
    try:
        return int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0


def validate_content_length(response, max_bytes):
    content_length = parse_content_length(response)
    if content_length and content_length > max_bytes:
        raise XhsDownloadError("资源超过允许的大小限制")


def write_limited_response(response, target, max_bytes, progress_callback=None):
    total = 0
    content_length = parse_content_length(response)

    validate_content_length(response, max_bytes)

    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")
        target.write(chunk)
        if progress_callback:
            progress_callback(total, content_length)

    return total


def read_limited_response(response, max_bytes):
    buffer = BytesIO()
    write_limited_response(response, buffer, max_bytes)
    return buffer.getvalue()


def fetch_media_response(url, headers=None, timeout=30):
    validate_media_url(url)
    return request_get_with_retry(
        url,
        headers=headers or get_media_headers(url),
        timeout=timeout,
        retries=3,
        stream=True,
        allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES,
        final_url_message="资源 URL 跳转到了不允许的下载域名",
    )


def parse_content_range_total(content_range):
    if not content_range:
        return 0
    try:
        return int(str(content_range).split("/", 1)[1])
    except (IndexError, TypeError, ValueError):
        return 0


def probe_media_size(url, headers=None, timeout=6):
    validate_media_url(url)
    request_headers = headers or get_media_headers(url)

    try:
        response = requests.head(
            url,
            headers=request_headers,
            timeout=timeout,
            allow_redirects=True,
        )
        try:
            if response.status_code < 400:
                validate_external_url(
                    response.url,
                    MEDIA_HOST_SUFFIXES,
                    "资源 URL 跳转到了不允许的下载域名",
                )
                size = parse_content_length(response)
                if size:
                    return size
        finally:
            response.close()
    except Exception:
        pass

    try:
        response = requests.get(
            url,
            headers={**request_headers, "Range": "bytes=0-0"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        try:
            if response.status_code < 400:
                validate_external_url(
                    response.url,
                    MEDIA_HOST_SUFFIXES,
                    "资源 URL 跳转到了不允许的下载域名",
                )
                return (
                    parse_content_range_total(response.headers.get("Content-Range"))
                    or parse_content_length(response)
                )
        finally:
            response.close()
    except Exception:
        pass

    return 0


def fetch_to_temp_path(url, headers=None, timeout=30, max_bytes=MAX_IMAGE_BYTES, progress_callback=None):
    response = fetch_media_response(url, headers=headers, timeout=timeout)
    fd, temp_path = tempfile.mkstemp(prefix="xhs_media_")

    try:
        with os.fdopen(fd, "wb") as temp_file:
            fd = None
            with response:
                content_type = response.headers.get("Content-Type", "")
                final_url = response.url
                size = write_limited_response(
                    response,
                    temp_file,
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                )
        return {
            "path": temp_path,
            "content_type": content_type,
            "final_url": final_url,
            "size": size,
        }
    except Exception:
        response.close()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        Path(temp_path).unlink(missing_ok=True)
        raise


def fetch_binary(url, headers=None, timeout=30, max_bytes=MAX_IMAGE_BYTES):
    response = fetch_media_response(url, headers=headers, timeout=timeout)
    with response:
        return read_limited_response(response, max_bytes=max_bytes)


def iter_download_files(media_items, base_filename, save_format=ORIGINAL_FORMAT, quality=95, video_save_mode="video"):
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
                if save_format == ORIGINAL_FORMAT:
                    temp_result = fetch_to_temp_path(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        yield filename, Path(temp_result["path"]).read_bytes()
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_binary(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                    with Image.open(BytesIO(content)) as image:
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
                    if save_format == ORIGINAL_FORMAT:
                        temp_result = fetch_to_temp_path(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            yield filename, Path(temp_result["path"]).read_bytes()
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_binary(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                        with Image.open(BytesIO(content)) as image:
                            filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                            yield filename, save_image_to_buffer(image, save_format, quality)

            else:
                raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

        except Exception as error:
            failures.append(f"{media_type}: {url or '-'} -> {error}")

    if failures:
        yield "failed.txt", "\n".join(failures).encode("utf-8")


def assert_download_request(media_items):
    if not media_items:
        raise XhsDownloadError("没有选择资源")
    if len(media_items) > MAX_DOWNLOAD_ITEMS:
        raise XhsDownloadError(f"一次最多下载 {MAX_DOWNLOAD_ITEMS} 个资源")


def assert_archive_total(current_total, next_size):
    if current_total + next_size > MAX_ARCHIVE_BYTES:
        raise XhsDownloadError("本次下载资源总大小超过允许限制")


def write_archive_entry_from_temp(archive, temp_result, filename, current_total):
    assert_archive_total(current_total, temp_result["size"])
    archive.write(temp_result["path"], filename)
    return temp_result["size"]


def write_archive_entry_from_bytes(archive, filename, content, current_total):
    assert_archive_total(current_total, len(content))
    archive.writestr(filename, content)
    return len(content)


def build_download_archive_file(
    media_items,
    base_filename,
    save_format=ORIGINAL_FORMAT,
    quality=95,
    video_save_mode="video",
    progress_callback=None,
):
    assert_download_request(media_items)

    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    archive_file = tempfile.NamedTemporaryFile(prefix="xhs_archive_", suffix=".zip")
    saved_count = 0
    total_size = 0
    failures = []
    processed_count = 0
    total_items = len(media_items)

    def report_item_progress(item_index, bytes_read, content_length):
        if not progress_callback or not content_length:
            return
        item_fraction = min(bytes_read / content_length, 0.98)
        progress_callback((item_index - 1) + item_fraction, total_items)

    try:
        with ZipFile(archive_file, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            for index, item in enumerate(media_items, start=1):
                media_type = item.get("type", "image")
                url = item.get("url")

                try:
                    if not url:
                        raise XhsDownloadError("资源 URL 为空")

                    if media_type == "image":
                        if save_format == ORIGINAL_FORMAT:
                            temp_result = fetch_to_temp_path(
                                url,
                                timeout=20,
                                max_bytes=MAX_IMAGE_BYTES,
                                progress_callback=lambda done, total, item_index=index: report_item_progress(
                                    item_index,
                                    done,
                                    total,
                                ),
                            )
                            try:
                                ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                                filename = clean_filename(f"{base_filename}_{index}.{ext}")
                                total_size += write_archive_entry_from_temp(
                                    archive,
                                    temp_result,
                                    filename,
                                    total_size,
                                )
                                saved_count += 1
                            finally:
                                Path(temp_result["path"]).unlink(missing_ok=True)
                        else:
                            content = fetch_binary(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                            with Image.open(BytesIO(content)) as image:
                                filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                                converted = save_image_to_buffer(image, save_format, quality)
                            total_size += write_archive_entry_from_bytes(archive, filename, converted, total_size)
                            saved_count += 1

                    elif media_type == "video":
                        if video_save_mode in ["video", "both"]:
                            temp_result = fetch_to_temp_path(
                                url,
                                timeout=60,
                                max_bytes=MAX_VIDEO_BYTES,
                                progress_callback=lambda done, total, item_index=index: report_item_progress(
                                    item_index,
                                    done,
                                    total,
                                ),
                            )
                            try:
                                ext = guess_video_extension_from_response(
                                    temp_result["final_url"],
                                    temp_result["content_type"],
                                )
                                filename = clean_filename(f"{base_filename}_{index}.{ext}")
                                total_size += write_archive_entry_from_temp(
                                    archive,
                                    temp_result,
                                    filename,
                                    total_size,
                                )
                                saved_count += 1
                            finally:
                                Path(temp_result["path"]).unlink(missing_ok=True)

                        if video_save_mode in ["cover", "both"]:
                            cover_url = item.get("cover_url")
                            if not cover_url:
                                raise XhsDownloadError("当前视频没有解析到封面")

                            if save_format == ORIGINAL_FORMAT:
                                temp_result = fetch_to_temp_path(
                                    cover_url,
                                    timeout=20,
                                    max_bytes=MAX_IMAGE_BYTES,
                                    progress_callback=lambda done, total, item_index=index: report_item_progress(
                                        item_index,
                                        done,
                                        total,
                                    ),
                                )
                                try:
                                    ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                                    filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                                    total_size += write_archive_entry_from_temp(
                                        archive,
                                        temp_result,
                                        filename,
                                        total_size,
                                    )
                                    saved_count += 1
                                finally:
                                    Path(temp_result["path"]).unlink(missing_ok=True)
                            else:
                                content = fetch_binary(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                                with Image.open(BytesIO(content)) as image:
                                    filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                                    converted = save_image_to_buffer(image, save_format, quality)
                                total_size += write_archive_entry_from_bytes(
                                    archive,
                                    filename,
                                    converted,
                                    total_size,
                                )
                                saved_count += 1

                    else:
                        raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

                except Exception as error:
                    failures.append(f"{media_type}: {url or '-'} -> {error}")
                finally:
                    processed_count += 1
                    if progress_callback:
                        progress_callback(processed_count, total_items)

            if failures:
                archive.writestr("failed.txt", "\n".join(failures).encode("utf-8"))

        if saved_count == 0:
            detail = f"：{failures[0]}" if failures else ""
            raise XhsDownloadError(f"没有资源保存成功{detail}")

        archive_file.seek(0)
        return {
            "filename": clean_filename(f"{base_filename}.zip"),
            "file": archive_file,
            "saved_count": saved_count,
            "failed_count": len(failures),
        }

    except Exception:
        archive_file.close()
        raise


def build_download_archive(media_items, base_filename, save_format=ORIGINAL_FORMAT, quality=95, video_save_mode="video"):
    archive_file = build_download_archive_file(media_items, base_filename, save_format, quality, video_save_mode)

    try:
        content = archive_file["file"].read()
    finally:
        archive_file["file"].close()

    return {
        "filename": archive_file["filename"],
        "content": content,
        "saved_count": archive_file["saved_count"],
        "failed_count": archive_file["failed_count"],
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
    save_format=ORIGINAL_FORMAT,
    quality=95,
    video_save_mode="video",
    allow_absolute=False,
):
    assert_download_request(media_items)

    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    save_folder = resolve_server_save_folder(save_dir, allow_absolute=allow_absolute)
    save_folder.mkdir(parents=True, exist_ok=True)

    saved_files = []
    failures = []

    for index, item in enumerate(media_items, start=1):
        media_type = item.get("type", "image")
        url = item.get("url")

        try:
            if not url:
                raise XhsDownloadError("资源 URL 为空")

            if media_type == "image":
                if save_format == ORIGINAL_FORMAT:
                    temp_result = fetch_to_temp_path(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        target = save_folder / filename
                        shutil.move(temp_result["path"], target)
                        saved_files.append(str(target))
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_binary(url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                    with Image.open(BytesIO(content)) as image:
                        filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                        converted = save_image_to_buffer(image, save_format, quality)
                    target = save_folder / filename
                    target.write_bytes(converted)
                    saved_files.append(str(target))

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    temp_result = fetch_to_temp_path(url, timeout=60, max_bytes=MAX_VIDEO_BYTES)
                    try:
                        ext = guess_video_extension_from_response(
                            temp_result["final_url"],
                            temp_result["content_type"],
                        )
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        target = save_folder / filename
                        shutil.move(temp_result["path"], target)
                        saved_files.append(str(target))
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)

                if video_save_mode in ["cover", "both"]:
                    cover_url = item.get("cover_url")
                    if not cover_url:
                        raise XhsDownloadError("当前视频没有解析到封面")

                    if save_format == ORIGINAL_FORMAT:
                        temp_result = fetch_to_temp_path(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            target = save_folder / filename
                            shutil.move(temp_result["path"], target)
                            saved_files.append(str(target))
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_binary(cover_url, timeout=20, max_bytes=MAX_IMAGE_BYTES)
                        with Image.open(BytesIO(content)) as image:
                            filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                            converted = save_image_to_buffer(image, save_format, quality)
                        target = save_folder / filename
                        target.write_bytes(converted)
                        saved_files.append(str(target))

            else:
                raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

        except Exception as error:
            failures.append(f"{media_type}: {url or '-'} -> {error}")

    if failures:
        (save_folder / "failed.txt").write_text("\n".join(failures), encoding="utf-8")

    if not saved_files:
        detail = f"：{failures[0]}" if failures else ""
        raise XhsDownloadError(f"没有资源保存成功{detail}")

    return {
        "save_folder": str(save_folder),
        "saved_count": len(saved_files),
        "failed_count": len(failures),
        "saved_files": saved_files,
    }
