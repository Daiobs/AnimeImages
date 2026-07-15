import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlparse

import requests

from ..common import XhsError, XhsParseError, XhsProviderTimeoutError
from ..env_config import runtime_int as env_runtime_int
from ..observability import sanitize_observability_text
from ..provider_access import PROVIDER_THIRD_PARTY, provider_is_allowed_for_request
from ..registry import get_adapter_for_url, parse_media_page
from ..region_routing import get_request_country_code


BATCH_DOUYIN_PROVIDER_ORDER = ("tiantianjiexi", "native")
BATCH_SKIPPED_MESSAGE = "等待执行时批次处理窗口已结束，本条未启动。"
BATCH_ITEM_TIMEOUT_MESSAGE = "本条解析超过单条时间限制。"
BATCH_PROVIDER_TIMEOUT_MESSAGE = "上游解析服务响应超时，请稍后重试。"
BATCH_PROVIDER_LABELS = {
    "tiantianjiexi": "天天解析",
    "kukutool": "KuKuTool",
    "native": "本地解析",
    "mobile_feed": "本地解析",
    "mobile_share": "本地解析",
    "native_fast_feed": "本地解析",
    "native_reference_feed": "本地解析",
    "yt_dlp_generic": "yt-dlp",
    "yt_dlp_merge": "yt-dlp 高清合并",
    "youtube_merge": "YouTube 高清合并",
    "gallery_dl_generic": "gallery-dl",
    "kukutool_generic": "KuKuTool",
}


def split_batch_links(value, max_links=None):
    max_links = max(1, int(max_links or env_runtime_int("BATCH_PARSE_MAX_LINKS", 20) or 20))
    links = []
    seen = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        links.append(line)
        if len(links) >= max_links:
            break
    return links


def build_batch_skipped_row(source_text, reason, started_at=None):
    started_at = started_at or time.monotonic()
    return {
        "status": "skipped",
        "source_text": source_text,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "error_message": sanitize_observability_text(reason),
    }


def build_batch_timeout_row(source_text, reason, started_at=None, duration_ms=None):
    started_at = started_at or time.monotonic()
    if duration_ms is None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "status": "timeout",
        "source_text": source_text,
        "duration_ms": max(0, int(duration_ms or 0)),
        "error_message": sanitize_observability_text(reason),
    }


def build_batch_failed_row(source_text, reason, started_at=None, duration_ms=None):
    started_at = started_at or time.monotonic()
    if duration_ms is None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "status": "failed",
        "source_text": source_text,
        "duration_ms": max(0, int(duration_ms or 0)),
        "error_message": sanitize_observability_text(reason),
    }


def batch_link_host(url):
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def batch_provider_display_label(metadata):
    explicit_label = str(metadata.get("provider_label") or "").strip()
    if explicit_label:
        return explicit_label
    selected_provider = str(
        metadata.get("selected_provider") or metadata.get("source_provider") or ""
    ).strip()
    return BATCH_PROVIDER_LABELS.get(selected_provider, selected_provider)


def enrich_batch_media_items(row):
    for item in row.get("video_items") or ():
        item["direct_host"] = batch_link_host(item.get("video_url") or item.get("url"))
        item["direct_url"] = item.get("video_url") or item.get("url") or ""
    for item in row.get("image_items") or ():
        item["direct_host"] = batch_link_host(item.get("url"))
        item["direct_url"] = item.get("url") or ""


def build_batch_summary(rows, *, batch_wall_duration_ms=0):
    summary = {
        "total_count": len(rows),
        "success_count": 0,
        "failed_count": 0,
        "timeout_count": 0,
        "skipped_count": 0,
        "video_count": 0,
        "image_count": 0,
        "total_duration_ms": 0,
        "batch_wall_duration_ms": max(0, int(batch_wall_duration_ms or 0)),
        "provider_counts": {},
        "platform_counts": {},
    }
    for row in rows:
        status = row.get("status") or ""
        if status == "success":
            summary["success_count"] += 1
            videos = row.get("video_items") or ()
            images = row.get("image_items") or ()
            summary["video_count"] += len(videos)
            summary["image_count"] += len(images)
            provider = row.get("provider_label") or row.get("selected_provider") or "native"
            platform = row.get("platform_name") or row.get("platform") or "未知平台"
            summary["provider_counts"][provider] = summary["provider_counts"].get(provider, 0) + 1
            summary["platform_counts"][platform] = summary["platform_counts"].get(platform, 0) + 1
        elif status == "timeout":
            summary["timeout_count"] += 1
        elif status == "skipped":
            summary["skipped_count"] += 1
        else:
            summary["failed_count"] += 1
        summary["total_duration_ms"] += int(row.get("duration_ms") or 0)
    return summary


def query_uses_third_party_provider(query):
    try:
        return getattr(get_adapter_for_url(query), "platform_id", "") == "third_party"
    except XhsError:
        return False


def third_party_provider_is_available(request):
    return provider_is_allowed_for_request(PROVIDER_THIRD_PARTY, request)


def build_batch_result_row(
    request,
    source_text,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    per_link_timeout_seconds,
    deadline=None,
    parser=None,
    display_context_builder=None,
    request_country_code_builder=None,
    query_uses_third_party_provider_func=None,
    third_party_provider_is_available_func=None,
):
    started_at = time.monotonic()
    execution_deadline = (
        float(deadline)
        if deadline is not None
        else started_at + max(0.1, float(per_link_timeout_seconds or 0))
    )
    if started_at >= execution_deadline:
        return build_batch_timeout_row(
            source_text,
            BATCH_ITEM_TIMEOUT_MESSAGE,
            started_at,
        )
    try:
        parser = parser or parse_media_page
        display_context_builder = display_context_builder or _missing_display_context_builder
        request_country_code_builder = request_country_code_builder or get_request_country_code
        query_uses_third_party_provider_func = (
            query_uses_third_party_provider_func or query_uses_third_party_provider
        )
        third_party_provider_is_available_func = (
            third_party_provider_is_available_func or third_party_provider_is_available
        )

        remaining_timeout = min(
            max(0.1, float(per_link_timeout_seconds or 0)),
            max(0.0, execution_deadline - time.monotonic()),
        )
        if remaining_timeout <= 0:
            return build_batch_timeout_row(
                source_text,
                BATCH_ITEM_TIMEOUT_MESSAGE,
                started_at,
            )
        if (
            query_uses_third_party_provider_func(source_text)
            and not third_party_provider_is_available_func(request)
        ):
            raise XhsParseError("第三方通用解析暂未开放。")
        result = parser(
            source_text,
            prefer_ultra_hd=prefer_ultra_hd,
            client_country_code=(
                request_country_code_builder(request) if prefer_ultra_hd else ""
            ),
            prefer_youtube_merge=prefer_youtube_merge,
            douyin_provider_order=BATCH_DOUYIN_PROVIDER_ORDER,
            timeout_seconds=remaining_timeout,
            deadline_monotonic=execution_deadline,
        )
        display_context = display_context_builder(result, platform_key="home")
        metadata = result.metadata or {}
        selected_provider = metadata.get("selected_provider") or metadata.get("source_provider") or ""
        row = {
            "status": "success",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "provider_label": batch_provider_display_label(metadata),
            "selected_provider": selected_provider,
            **display_context,
        }
        enrich_batch_media_items(row)
        return row
    except XhsProviderTimeoutError:
        return build_batch_timeout_row(
            source_text,
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            started_at,
        )
    except requests.Timeout:
        return build_batch_timeout_row(
            source_text,
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            started_at,
        )
    except XhsError as error:
        return build_batch_failed_row(
            source_text,
            f"解析执行失败：{error}",
            started_at,
        )
    except requests.RequestException as error:
        return build_batch_failed_row(
            source_text,
            f"解析执行失败：{error}",
            started_at,
        )
    except Exception as error:
        return build_batch_failed_row(
            source_text,
            f"解析执行失败：{type(error).__name__}: {error}",
            started_at,
        )


def _execute_batch_result_row(
    row_builder,
    request,
    source_text,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    per_link_timeout_seconds,
    row_builder_kwargs,
):
    started_at = time.monotonic()
    execution_deadline = started_at + max(
        0.001,
        float(per_link_timeout_seconds or 0),
    )
    try:
        row = row_builder(
            request,
            source_text,
            prefer_ultra_hd=prefer_ultra_hd,
            prefer_youtube_merge=prefer_youtube_merge,
            per_link_timeout_seconds=per_link_timeout_seconds,
            deadline=execution_deadline,
            **row_builder_kwargs,
        )
    except Exception as error:
        return build_batch_failed_row(
            source_text,
            f"解析执行失败：{type(error).__name__}: {error}",
            started_at,
        )

    finished_at = time.monotonic()
    if not isinstance(row, dict):
        return build_batch_failed_row(
            source_text,
            "解析执行失败：返回结果格式无效。",
            started_at,
        )
    if finished_at >= execution_deadline and row.get("status") != "timeout":
        return build_batch_timeout_row(
            source_text,
            BATCH_ITEM_TIMEOUT_MESSAGE,
            started_at,
            duration_ms=int((finished_at - started_at) * 1000),
        )
    if row.get("status") == "skipped":
        return build_batch_timeout_row(
            source_text,
            BATCH_ITEM_TIMEOUT_MESSAGE,
            started_at,
            duration_ms=int((finished_at - started_at) * 1000),
        )
    row.setdefault("source_text", source_text)
    row.setdefault("duration_ms", int((finished_at - started_at) * 1000))
    return row


def build_batch_result_rows(
    request,
    links,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    deadline,
    parallel_workers,
    per_link_timeout_seconds,
    completion_grace_seconds=1.0,
    row_builder=None,
    row_builder_kwargs=None,
):
    if not links:
        return []

    row_builder = row_builder or build_batch_result_row
    row_builder_kwargs = row_builder_kwargs or {}
    worker_count = min(max(1, int(parallel_workers or 1)), len(links))
    per_link_timeout = max(0.001, float(per_link_timeout_seconds or 0))
    completion_grace = min(max(0.0, float(completion_grace_seconds or 0)), 2.0)
    response_deadline = float(deadline) + per_link_timeout + completion_grace
    rows = [None] * len(links)
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch-parse")
    active_futures = {}
    next_index = 0

    def mark_remaining_skipped():
        nonlocal next_index
        while next_index < len(links):
            rows[next_index] = build_batch_skipped_row(
                links[next_index],
                BATCH_SKIPPED_MESSAGE,
            )
            next_index += 1

    def submit_available():
        nonlocal next_index
        while len(active_futures) < worker_count and next_index < len(links):
            if time.monotonic() >= deadline:
                mark_remaining_skipped()
                return
            index = next_index
            link = links[index]
            next_index += 1
            future = executor.submit(
                _execute_batch_result_row,
                row_builder,
                request,
                link,
                prefer_ultra_hd=prefer_ultra_hd,
                prefer_youtube_merge=prefer_youtube_merge,
                per_link_timeout_seconds=per_link_timeout,
                row_builder_kwargs=row_builder_kwargs,
            )
            active_futures[future] = (index, link)

    try:
        submit_available()
        while active_futures:
            now = time.monotonic()
            if next_index < len(links) and now >= deadline:
                mark_remaining_skipped()

            remaining_response_time = max(0.0, response_deadline - now)
            if remaining_response_time <= 0:
                completed, _pending = wait(
                    tuple(active_futures),
                    return_when=FIRST_COMPLETED,
                )
            else:
                wait_timeout = remaining_response_time
                if next_index < len(links):
                    wait_timeout = min(
                        wait_timeout,
                        max(0.0, float(deadline) - now),
                    )
                if wait_timeout <= 0:
                    continue
                completed, _pending = wait(
                    tuple(active_futures),
                    timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue

            for future in completed:
                index, link = active_futures.pop(future)
                try:
                    rows[index] = future.result()
                except Exception as error:
                    rows[index] = build_batch_failed_row(
                        link,
                        f"解析执行失败：{type(error).__name__}: {error}",
                    )
            if next_index < len(links):
                submit_available()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for index, row in enumerate(rows):
        if row is None:
            rows[index] = build_batch_skipped_row(
                links[index],
                BATCH_SKIPPED_MESSAGE,
            )
    return rows


def _missing_display_context_builder(*_args, **_kwargs):
    raise ValueError("display_context_builder is required")
