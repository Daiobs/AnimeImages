import os
import re
import json
import time
import base64
import requests
from urllib.parse import urlparse
from pathlib import Path
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO

from django.conf import settings
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_GET

import hashlib
import mimetypes
from pathlib import Path
from django.http import FileResponse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def search_view(request):
    return render(request, "xhs_download.html")


def clean_filename(filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename.strip()
    return filename or "xhs_image"


def encode_url(url):
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8")


def decode_url(encoded):
    return base64.urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8")


def normalize_xhs_image_url(url):
    if not url:
        return None

    url = url.replace("\\u002F", "/")
    url = url.replace("\\/", "/")
    url = url.split("!")[0]
    url = url.split("?")[0]

    # notes_pre_post/xxx
    match = re.search(r"(notes_pre_post/[^/?!]+)", url)
    if match:
        image_key = match.group(1)
        return f"https://ci.xiaohongshu.com/{image_key}?imageView2/format/jpg"

    # spectrum/xxx
    match = re.search(r"(spectrum/[^/?!]+)", url)
    if match:
        image_key = match.group(1)
        return f"https://ci.xiaohongshu.com/{image_key}?imageView2/format/jpg"

    # 只有图片 key 的情况：/.../1040xxxx
    match = re.search(r"/([a-zA-Z0-9]{20,})$", url)
    if match:
        image_key = match.group(1)
        return f"https://ci.xiaohongshu.com/{image_key}?imageView2/format/jpg"

    return url


def normalize_text(text):
    if not text:
        return ""

    return (
        text
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )


def normalize_video_url(value):
    if not value:
        return None

    value = normalize_text(value).strip()

    if value.startswith("http://") or value.startswith("https://"):
        return value

    value = value.lstrip("/")
    return f"https://sns-video-bd.xhscdn.com/{value}"


def extract_video_urls(html):
    html = normalize_text(html)
    video_urls = []

    key_patterns = [
        r'"originVideoKey"\s*:\s*"([^"]+)"',
        r'"origin_video_key"\s*:\s*"([^"]+)"',
        r'"videoKey"\s*:\s*"([^"]+)"',
        r'"video_key"\s*:\s*"([^"]+)"',
    ]

    for pattern in key_patterns:
        for match in re.finditer(pattern, html):
            raw_value = normalize_text(match.group(1)).strip()

            if not raw_value:
                continue

            if raw_value.lower() in ["null", "undefined", "none"]:
                continue

            if len(raw_value) < 10:
                continue

            video_url = normalize_video_url(raw_value)

            if video_url and video_url not in video_urls:
                video_urls.append(video_url)

    url_patterns = [
        r'"masterUrl"\s*:\s*"([^"]+)"',
        r'"backupUrl"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'"video_url"\s*:\s*"([^"]+)"',
    ]

    for pattern in url_patterns:
        for match in re.finditer(pattern, html):
            raw_url = normalize_text(match.group(1)).strip()

            if not raw_url:
                continue

            if (
                    "sns-video" in raw_url
                    or ".mp4" in raw_url
                    or ".mov" in raw_url
                    or ".m3u8" in raw_url
            ):
                if raw_url not in video_urls:
                    video_urls.append(raw_url)

    return video_urls


def parse_xhs_page(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("请输入正确的小红书链接")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=15,
        allow_redirects=True
    )
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    username_tag = soup.find("span", class_="username")
    title_tag = soup.find("div", {"id": "detail-title"})

    username = username_tag.get_text(strip=True) if username_tag else ""
    title = title_tag.get_text(strip=True) if title_tag else ""

    base_filename = clean_filename(f"{username}_{title}") if username or title else "xhs"

    video_urls = extract_video_urls(html)

    meta_image_urls = []

    # 第一优先级：只从 meta og:image 取
    for tag in soup.find_all("meta"):
        content = tag.get("content", "")
        name = tag.get("name", "")
        property_name = tag.get("property", "")

        if ("og:image" in name or "og:image" in property_name) and content:
            img_url = normalize_xhs_image_url(content)
            if img_url and img_url not in meta_image_urls:
                meta_image_urls.append(img_url)

    # 视频笔记：og:image 只作为封面，不进入 image_items
    if video_urls:
        cover_url = meta_image_urls[0] if meta_image_urls else None

        return {
            "post_type": "video",
            "base_filename": base_filename,
            "image_urls": [],
            "video_items": [
                {
                    "video_url": video_urls[0],
                    "cover_url": cover_url,
                }
            ],
        }

    # 图片笔记：如果 meta 里已经有图，就直接返回，不再扫整个 HTML
    if meta_image_urls:
        return {
            "post_type": "image",
            "base_filename": base_filename,
            "image_urls": meta_image_urls,
            "video_items": [],
        }

    # 兜底：只有 meta 没图时，才扫 HTML
    fallback_image_urls = []
    html_normalized = normalize_text(html)
    candidates = re.findall(r'https?://[^\s"\'<>]+', html_normalized)

    for item in candidates:
        if any(key in item for key in [
            "sns-webpic",
            "ci.xiaohongshu.com",
            "notes_pre_post",
        ]):
            img_url = normalize_xhs_image_url(item)

            if img_url and img_url not in fallback_image_urls:
                fallback_image_urls.append(img_url)

    return {
        "post_type": "image",
        "base_filename": base_filename,
        "image_urls": fallback_image_urls,
        "video_items": [],
    }


def search_results_view(request):
    query = request.GET.get("q", "").strip()

    context = {
        "query": query,
        "post_type": "",
        "image_items": [],
        "video_items": [],
        "error_message": None,
        "base_filename": "",
    }

    if not query:
        return render(request, "xhs_download.html", context)

    try:
        result = parse_xhs_page(query)

        context["post_type"] = result["post_type"]
        context["base_filename"] = result["base_filename"]

        context["image_items"] = [
            {
                "type": "image",
                "url": url,
                "encoded_url": encode_url(url),
            }
            for url in result["image_urls"]
        ]

        context["video_items"] = [
            {
                "type": "video",
                "video_url": item["video_url"],
                "cover_url": item["cover_url"],
                "encoded_cover_url": encode_url(item["cover_url"]) if item["cover_url"] else "",
            }
            for item in result["video_items"]
        ]

        if not context["image_items"] and not context["video_items"]:
            context["error_message"] = "没有解析到图片或视频。"

    except Exception as e:
        context["error_message"] = f"解析失败：{e}"

    return render(request, "xhs_download.html", context)


def placeholder_image_response():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
        <rect width="100%" height="100%" fill="#f1f3f5"/>
        <text x="50%" y="45%" dominant-baseline="middle" text-anchor="middle"
              font-size="18" fill="#999">
            图片加载失败
        </text>
        <text x="50%" y="58%" dominant-baseline="middle" text-anchor="middle"
              font-size="14" fill="#bbb">
            请稍后重试
        </text>
    </svg>
    """
    return HttpResponse(svg, content_type="image/svg+xml")


@require_GET
def proxy_image_view(request):
    """
    图片代理：
    - 不保存到本地
    - 不写数据库
    - 网络差时自动重试
    - 失败时返回占位图，而不是 404 裂图
    """
    encoded_url = request.GET.get("u")

    if not encoded_url:
        return placeholder_image_response()

    try:
        image_url = decode_url(encoded_url)
    except Exception:
        return placeholder_image_response()

    try:
        response = request_get_with_retry(
            image_url,
            headers=HEADERS,
            timeout=15,
            retries=3
        )

        content_type = response.headers.get("Content-Type", "image/jpeg")

        if "image" not in content_type.lower():
            return placeholder_image_response()

        result = HttpResponse(response.content, content_type=content_type)

        # 不在服务器缓存；允许浏览器短时间缓存，减少重复请求
        result["Cache-Control"] = "private, max-age=300"

        return result

    except Exception as e:
        print("缩略图代理失败：", image_url, e)
        return placeholder_image_response()


def normalize_save_format(save_format):
    """
    统一保存格式
    """
    save_format = (save_format or "jpg").lower().strip()

    if save_format == "jpeg":
        save_format = "jpg"

    if save_format not in ["jpg", "png", "webp"]:
        save_format = "jpg"

    return save_format


def normalize_quality(quality):
    """
    限制图片质量范围：1 - 100
    """
    try:
        quality = int(quality)
    except Exception:
        quality = 95

    return max(1, min(quality, 100))


def normalize_video_save_mode(mode):
    """
    video：仅保存视频
    cover：仅保存封面
    both：视频和封面都保存
    """
    mode = (mode or "video").lower().strip()

    if mode not in ["video", "cover", "both"]:
        mode = "video"

    return mode


def get_save_folder(save_dir):
    """
    保存目录规则：

    1. 如果前端传 xhs，则保存到 MEDIA_ROOT/xhs
    2. 如果前端传 E:\\xxx\\xxx，则保存到该绝对路径
    """
    save_dir = (save_dir or "xhs").strip()

    if os.path.isabs(save_dir):
        save_folder = save_dir
    else:
        save_folder = os.path.join(settings.MEDIA_ROOT, save_dir)

    os.makedirs(save_folder, exist_ok=True)
    return save_folder


def get_public_media_url(save_path):
    """
    如果文件保存在 MEDIA_ROOT 下面，则返回可访问的 media URL。
    如果保存到了 E 盘等外部目录，则返回 None。
    """
    try:
        media_root = Path(settings.MEDIA_ROOT).resolve()
        file_path = Path(save_path).resolve()
        relative_path = file_path.relative_to(media_root)

        return settings.MEDIA_URL + str(relative_path).replace("\\", "/")
    except Exception:
        return None


def save_image_by_format(image, save_path, save_format, quality):
    """
    按用户设置保存图片
    """
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)

    if save_format == "png":
        image.save(save_path, "PNG", optimize=True)

    elif save_format == "webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        image.save(
            save_path,
            "WEBP",
            quality=quality,
            method=6
        )

    else:
        # JPG 不支持透明通道，必须转 RGB
        if image.mode != "RGB":
            image = image.convert("RGB")

        image.save(
            save_path,
            "JPEG",
            quality=quality,
            optimize=True,
            progressive=True
        )


def guess_video_extension(url):
    """
    根据视频 URL 粗略判断保存扩展名。
    小红书大多数视频可以直接保存为 mp4。
    """
    url = (url or "").lower()

    if ".m3u8" in url:
        return "m3u8"

    if ".mov" in url:
        return "mov"

    if ".mp4" in url:
        return "mp4"

    return "mp4"


def download_binary_file(url, save_path):
    """
    下载视频/二进制文件。
    不经过 Pillow，不做图片格式转换。
    """
    if not url:
        raise ValueError("视频 URL 为空")

    video_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }

    with requests.get(
            url,
            headers=video_headers,
            timeout=60,
            allow_redirects=True,
            stream=True
    ) as response:
        response.raise_for_status()

        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


@require_POST
def download_images_api(request):
    try:
        data = json.loads(request.body.decode("utf-8"))

        media_items = data.get("media_items", [])

        # 兼容旧前端：如果还传 image_urls，也能下载
        image_urls = data.get("image_urls", [])
        if image_urls and not media_items:
            media_items = [
                {
                    "type": "image",
                    "url": url
                }
                for url in image_urls
            ]

        base_filename = clean_filename(data.get("base_filename", "xhs"))
        save_dir = data.get("save_dir", "xhs")
        save_format = normalize_save_format(data.get("save_format", "jpg"))
        quality = normalize_quality(data.get("quality", 95))
        video_save_mode = normalize_video_save_mode(data.get("video_save_mode", "video"))
        if not media_items:
            return JsonResponse({
                "status": "error",
                "message": "没有选择资源"
            }, status=400)

        save_folder = get_save_folder(save_dir)

        saved_files = []
        failed_files = []

        for index, item in enumerate(media_items, start=1):
            media_type = item.get("type", "image")
            url = item.get("url")

            try:
                if not url:
                    raise ValueError("资源 URL 为空")

                # 图片
                if media_type == "image":
                    # response = requests.get(
                    #     url,
                    #     headers=HEADERS,
                    #     timeout=20,
                    #     allow_redirects=True
                    # )
                    response = request_get_with_retry(
                        url,
                        headers=HEADERS,
                        timeout=20,
                        retries=3
                    )
                    response.raise_for_status()

                    image = Image.open(BytesIO(response.content))

                    filename = clean_filename(
                        f"{base_filename}_{index}.{save_format}"
                    )
                    save_path = os.path.join(save_folder, filename)

                    save_image_by_format(
                        image=image,
                        save_path=save_path,
                        save_format=save_format,
                        quality=quality
                    )

                    saved_files.append({
                        "type": "image",
                        "filename": filename,
                        "path": save_path,
                        "url": get_public_media_url(save_path),
                    })

                # 视频
                elif media_type == "video":
                    video_url = item.get("url")
                    cover_url = item.get("cover_url")

                    saved_item = {
                        "type": "video",
                        "files": [],
                    }

                    # 1. 保存视频
                    if video_save_mode in ["video", "both"]:
                        if not video_url:
                            raise ValueError("视频 URL 为空")

                        ext = guess_video_extension(video_url)

                        video_filename = clean_filename(
                            f"{base_filename}_{index}.{ext}"
                        )
                        video_path = os.path.join(save_folder, video_filename)

                        download_binary_file(video_url, video_path)

                        saved_item["files"].append({
                            "kind": "video",
                            "filename": video_filename,
                            "path": video_path,
                            "url": get_public_media_url(video_path),
                        })

                    # 2. 保存封面
                    if video_save_mode in ["cover", "both"]:
                        if not cover_url:
                            raise ValueError("当前视频没有解析到封面，无法只下载封面")

                        cover_response = requests.get(
                            cover_url,
                            headers=HEADERS,
                            timeout=20,
                            allow_redirects=True
                        )
                        cover_response.raise_for_status()

                        cover_image = Image.open(BytesIO(cover_response.content))

                        cover_filename = clean_filename(
                            f"{base_filename}_{index}_cover.{save_format}"
                        )
                        cover_path = os.path.join(save_folder, cover_filename)

                        save_image_by_format(
                            image=cover_image,
                            save_path=cover_path,
                            save_format=save_format,
                            quality=quality
                        )

                        saved_item["files"].append({
                            "kind": "cover",
                            "filename": cover_filename,
                            "path": cover_path,
                            "url": get_public_media_url(cover_path),
                        })

                    if not saved_item["files"]:
                        raise ValueError("没有选择要保存的视频内容")

                    saved_files.append(saved_item)

                else:
                    raise ValueError(f"暂不支持的资源类型：{media_type}")

                time.sleep(0.2)

            except Exception as e:
                failed_files.append({
                    "type": media_type,
                    "url": url,
                    "error": str(e),
                })

        return JsonResponse({
            "status": "success" if saved_files else "error",
            "message": f"成功保存 {len(saved_files)} 个资源，失败 {len(failed_files)} 个",
            "save_folder": save_folder,
            "save_format": save_format,
            "quality": quality,
            "video_save_mode": video_save_mode,
            "saved_files": saved_files,
            "failed_files": failed_files,
        })

    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)


def request_get_with_retry(url, headers=None, timeout=15, retries=3, sleep_seconds=0.8):
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers or HEADERS,
                timeout=timeout,
                allow_redirects=True
            )

            if response.status_code == 200:
                return response

            last_error = Exception(f"HTTP {response.status_code}")

        except Exception as e:
            last_error = e

        time.sleep(sleep_seconds * (attempt + 1))

    raise last_error


def get_cache_dir():
    cache_dir = Path(settings.MEDIA_ROOT) / "xhs_thumb_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_cache_key(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def find_cached_image(url):
    cache_dir = get_cache_dir()
    cache_key = get_cache_key(url)
    files = list(cache_dir.glob(f"{cache_key}.*"))

    if files:
        return files[0]

    return None


def get_extension_from_content_type(content_type):
    content_type = (content_type or "image/jpeg").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type)

    if ext in [".jpe", None]:
        ext = ".jpg"

    return ext


def request_get_with_retry(url, headers=None, timeout=15, retries=3, sleep_seconds=0.8):
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers or HEADERS,
                timeout=timeout,
                allow_redirects=True
            )

            if response.status_code == 200:
                return response

            last_error = Exception(f"HTTP {response.status_code}")

        except Exception as e:
            last_error = e

        time.sleep(sleep_seconds * (attempt + 1))

    raise last_error


def placeholder_image_response():
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
        <rect width="100%" height="100%" fill="#f1f3f5"/>
        <text x="50%" y="45%" dominant-baseline="middle" text-anchor="middle"
              font-size="18" fill="#999">
            图片加载失败
        </text>
        <text x="50%" y="58%" dominant-baseline="middle" text-anchor="middle"
              font-size="14" fill="#bbb">
            请稍后刷新重试
        </text>
    </svg>
    """
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Cache-Control"] = "no-store"
    return response
