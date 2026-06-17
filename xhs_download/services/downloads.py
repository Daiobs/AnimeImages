import os
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse
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
    validate_media_url,
    validate_public_download_url,
)
from .platforms.bilibili import BILIBILI_HEADERS, BILIBILI_MEDIA_HOST_SUFFIXES
from .platforms.douyin import DOUYIN_HEADERS, DOUYIN_MEDIA_HOST_SUFFIXES
from .provider_recovery import (
    get_kukutool_manifest_identity,
    iter_kukutool_download_recovery_urls,
)
from .provider_manifest import provider_manifest_is_valid
from .quality import (
    QualityProbe,
    content_type_matches_expected,
    parse_content_length,
)


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


def validate_content_length(response, max_bytes):
    content_length = parse_content_length(response)
    if content_length and content_length > max_bytes:
        raise XhsDownloadError("资源超过允许的大小限制")


def response_chunk_looks_like_html(chunk):
    sample = bytes(chunk[:512]).lstrip().lower()
    return sample.startswith(
        (
            b"<!doctype html",
            b"<html",
            b"<head",
            b"<body",
        )
    )


def write_limited_response(
    response,
    target,
    max_bytes,
    progress_callback=None,
    expected_type="",
):
    total = 0
    content_length = parse_content_length(response)
    first_chunk = True

    validate_content_length(response, max_bytes)

    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if not chunk:
            continue
        if (
            first_chunk
            and expected_type in {"video", "image"}
            and response_chunk_looks_like_html(chunk)
        ):
            raise XhsDownloadError("资源类型异常：HTML response body")
        first_chunk = False
        total += len(chunk)
        if total > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")
        target.write(chunk)
        if progress_callback:
            progress_callback(total, content_length)

    return total


def read_limited_response(response, max_bytes, expected_type=""):
    buffer = BytesIO()
    write_limited_response(
        response,
        buffer,
        max_bytes,
        expected_type=expected_type,
    )
    return buffer.getvalue()


def validate_response_content_type(content_type, expected_type):
    if not expected_type:
        return
    if not content_type or not content_type_matches_expected(
        content_type,
        expected_type,
    ):
        raise XhsDownloadError(
            f"资源类型异常：{content_type or 'missing Content-Type'}"
        )


def is_recoverable_provider_download_error(error):
    if isinstance(error, requests.RequestException):
        return True
    if not isinstance(error, XhsDownloadError):
        return False
    message = str(error)
    return message.startswith("HTTP ") or message.startswith("资源类型异常：")


def provider_manifest_allows_public_download(item):
    return (
        isinstance(item, dict)
        and item.get("provider") == "kukutool_generic"
        and provider_manifest_is_valid(item)
    )


def request_public_provider_get_with_retry(
    url,
    headers=None,
    timeout=30,
    retries=3,
    stream=True,
    max_redirects=5,
):
    last_error = None
    for _ in range(max(1, int(retries or 1))):
        current_url = url
        try:
            for _ in range(max_redirects + 1):
                validate_public_download_url(current_url, "Provider URL 不允许请求")
                response = requests.get(
                    current_url,
                    headers=headers or get_media_headers(current_url),
                    timeout=timeout,
                    allow_redirects=False,
                    stream=stream,
                )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    response.close()
                    if not location:
                        raise XhsDownloadError(f"HTTP {response.status_code}")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code == 200:
                    validate_public_download_url(
                        response.url,
                        "Provider URL 跳转到了不允许的地址",
                    )
                    return response
                last_error = XhsDownloadError(f"HTTP {response.status_code}")
                response.close()
                break
            else:
                last_error = XhsDownloadError("Provider URL 重定向次数过多")
        except Exception as error:
            last_error = error

    raise last_error


def fetch_media_response(url, headers=None, timeout=30, retries=3, provider_signed=False):
    if provider_signed:
        return request_public_provider_get_with_retry(
            url,
            headers=headers,
            timeout=timeout,
            retries=retries,
            stream=True,
        )
    validate_media_url(url)
    return request_get_with_retry(
        url,
        headers=headers or get_media_headers(url),
        timeout=timeout,
        retries=max(1, int(retries or 1)),
        stream=True,
        allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES,
        final_url_message="资源 URL 跳转到了不允许的下载域名",
    )


def probe_media_size(url, headers=None, timeout=6):
    result = QualityProbe().probe(url, headers=headers or get_media_headers(url), timeout=timeout)
    return result.size_bytes if result.reachable else 0


def fetch_to_temp_path(
    url,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    progress_callback=None,
    expected_type="",
    retries=3,
    provider_signed=False,
):
    response = fetch_media_response(
        url,
        headers=headers,
        timeout=timeout,
        retries=retries,
        provider_signed=provider_signed,
    )
    fd, temp_path = tempfile.mkstemp(prefix="xhs_media_")

    try:
        with os.fdopen(fd, "wb") as temp_file:
            fd = None
            with response:
                content_type = response.headers.get("Content-Type", "")
                validate_response_content_type(content_type, expected_type)
                final_url = response.url
                size = write_limited_response(
                    response,
                    temp_file,
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    expected_type=expected_type,
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


def fetch_item_to_temp_path(
    item,
    url=None,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    progress_callback=None,
    expected_type="",
):
    initial_url = url or item.get("url")
    recovery_enabled = bool(get_kukutool_manifest_identity(item))
    provider_signed = provider_manifest_allows_public_download(item)
    attempted_urls = set()
    candidates = [initial_url]
    recovery_urls = None
    last_error = None

    while candidates or recovery_urls is not None:
        if not candidates:
            try:
                candidates.append(next(recovery_urls))
            except StopIteration:
                recovery_urls = None
                continue
        candidate_url = candidates.pop(0)
        if not candidate_url or candidate_url in attempted_urls:
            continue
        attempted_urls.add(candidate_url)
        try:
            return fetch_to_temp_path(
                candidate_url,
                headers=headers or get_media_headers(candidate_url),
                timeout=timeout,
                max_bytes=max_bytes,
                progress_callback=progress_callback,
                expected_type=expected_type,
                retries=1 if recovery_enabled else 3,
                provider_signed=provider_signed,
            )
        except Exception as error:
            last_error = error
            if (
                recovery_enabled
                and recovery_urls is None
                and len(attempted_urls) == 1
                and is_recoverable_provider_download_error(error)
            ):
                recovery_urls = iter(
                    iter_kukutool_download_recovery_urls(item) or ()
                )

    raise last_error or XhsDownloadError("资源下载失败")


def fetch_binary(
    url,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    expected_type="",
    retries=3,
):
    response = fetch_media_response(
        url,
        headers=headers,
        timeout=timeout,
        retries=retries,
    )
    with response:
        content_type = response.headers.get("Content-Type", "")
        validate_response_content_type(content_type, expected_type)
        return read_limited_response(
            response,
            max_bytes=max_bytes,
            expected_type=expected_type,
        )


def fetch_item_binary(
    item,
    url=None,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    expected_type="",
):
    temp_result = fetch_item_to_temp_path(
        item,
        url=url,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        expected_type=expected_type,
    )
    try:
        return Path(temp_result["path"]).read_bytes()
    finally:
        Path(temp_result["path"]).unlink(missing_ok=True)


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
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        yield filename, Path(temp_result["path"]).read_bytes()
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_item_binary(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    with Image.open(BytesIO(content)) as image:
                        filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                        yield filename, save_image_to_buffer(image, save_format, quality)

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    ext = guess_video_extension(url)
                    filename = clean_filename(f"{base_filename}_{index}.{ext}")
                    yield filename, fetch_item_binary(
                        item,
                        timeout=60,
                        max_bytes=MAX_VIDEO_BYTES,
                        expected_type="video",
                    )

                if video_save_mode in ["cover", "both"]:
                    cover_url = item.get("cover_url")
                    if not cover_url:
                        raise XhsDownloadError("当前视频没有解析到封面")
                    if save_format == ORIGINAL_FORMAT:
                        temp_result = fetch_to_temp_path(
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                            expected_type="image",
                        )
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            yield filename, Path(temp_result["path"]).read_bytes()
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_binary(
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                            expected_type="image",
                        )
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
                            temp_result = fetch_item_to_temp_path(
                                item,
                                timeout=20,
                                max_bytes=MAX_IMAGE_BYTES,
                                expected_type="image",
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
                            content = fetch_item_binary(
                                item,
                                timeout=20,
                                max_bytes=MAX_IMAGE_BYTES,
                                expected_type="image",
                            )
                            with Image.open(BytesIO(content)) as image:
                                filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                                converted = save_image_to_buffer(image, save_format, quality)
                            total_size += write_archive_entry_from_bytes(archive, filename, converted, total_size)
                            saved_count += 1

                    elif media_type == "video":
                        if video_save_mode in ["video", "both"]:
                            temp_result = fetch_item_to_temp_path(
                                item,
                                timeout=60,
                                max_bytes=MAX_VIDEO_BYTES,
                                expected_type="video",
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
                                    expected_type="image",
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
                                content = fetch_binary(
                                    cover_url,
                                    timeout=20,
                                    max_bytes=MAX_IMAGE_BYTES,
                                    expected_type="image",
                                )
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
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        target = save_folder / filename
                        shutil.move(temp_result["path"], target)
                        saved_files.append(str(target))
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_item_binary(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    with Image.open(BytesIO(content)) as image:
                        filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                        converted = save_image_to_buffer(image, save_format, quality)
                    target = save_folder / filename
                    target.write_bytes(converted)
                    saved_files.append(str(target))

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=60,
                        max_bytes=MAX_VIDEO_BYTES,
                        expected_type="video",
                    )
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
                        temp_result = fetch_to_temp_path(
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                            expected_type="image",
                        )
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            target = save_folder / filename
                            shutil.move(temp_result["path"], target)
                            saved_files.append(str(target))
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_binary(
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                            expected_type="image",
                        )
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
