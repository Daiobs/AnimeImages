import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from urllib.parse import urlparse

import requests

from ..common import XhsError, XhsParseError, XhsProviderTimeoutError
from ..env_config import runtime_int as env_runtime_int
from ..observability import sanitize_observability_text
from ..provider_access import PROVIDER_THIRD_PARTY, provider_is_allowed_for_request
from ..registry import get_adapter_for_url, parse_media_page
from ..region_routing import get_request_country_code


BATCH_DOUYIN_PROVIDER_ORDER = ("tiantianjiexi", "native")


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


def batch_link_host(url):
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def enrich_batch_media_items(row):
    for item in row.get("video_items") or ():
        item["direct_host"] = batch_link_host(item.get("video_url") or item.get("url"))
        item["direct_url"] = item.get("video_url") or item.get("url") or ""
    for item in row.get("image_items") or ():
        item["direct_host"] = batch_link_host(item.get("url"))
        item["direct_url"] = item.get("url") or ""


def build_batch_summary(rows):
    summary = {
        "total_count": len(rows),
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "video_count": 0,
        "image_count": 0,
        "total_duration_ms": 0,
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
            provider = row.get("selected_provider") or row.get("provider_label") or "native"
            platform = row.get("platform_name") or row.get("platform") or "未知平台"
            summary["provider_counts"][provider] = summary["provider_counts"].get(provider, 0) + 1
            summary["platform_counts"][platform] = summary["platform_counts"].get(platform, 0) + 1
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
    if deadline is not None and time.monotonic() >= deadline:
        return build_batch_skipped_row(source_text, "批量解析总预算已用尽，本条已跳过。", started_at)
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

        remaining_timeout = max(0.1, float(per_link_timeout_seconds or 0))
        if deadline is not None:
            remaining_timeout = min(
                remaining_timeout,
                max(0.0, deadline - time.monotonic()),
            )
        if remaining_timeout <= 0:
            return build_batch_skipped_row(source_text, "批量解析总预算已用尽，本条已跳过。", started_at)
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
            deadline_monotonic=(
                time.monotonic() + remaining_timeout
                if deadline is None
                else min(deadline, time.monotonic() + remaining_timeout)
            ),
        )
        display_context = display_context_builder(result, platform_key="home")
        metadata = result.metadata or {}
        row = {
            "status": "success",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "provider_label": metadata.get("provider_label") or metadata.get("selected_provider") or "",
            "selected_provider": metadata.get("selected_provider") or metadata.get("source_provider") or "",
            **display_context,
        }
        enrich_batch_media_items(row)
        return row
    except XhsProviderTimeoutError as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(str(error)),
        }
    except XhsError as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"解析失败：{error}"),
        }
    except requests.RequestException as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"请求失败：{error}"),
        }
    except Exception as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"{type(error).__name__}: {error}"),
        }


def build_batch_result_rows(
    request,
    links,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    deadline,
    parallel_workers,
    per_link_timeout_seconds,
    row_builder=None,
    row_builder_kwargs=None,
):
    if not links:
        return []

    row_builder = row_builder or build_batch_result_row
    row_builder_kwargs = row_builder_kwargs or {}
    worker_count = min(max(1, int(parallel_workers or 1)), len(links))
    per_link_timeout = per_link_timeout_seconds
    if worker_count <= 1:
        rows = []
        for link in links:
            if time.monotonic() >= deadline:
                rows.append(build_batch_skipped_row(link, "批量解析总预算已用尽，本条已跳过。"))
                continue
            rows.append(
                row_builder(
                    request,
                    link,
                    prefer_ultra_hd=prefer_ultra_hd,
                    prefer_youtube_merge=prefer_youtube_merge,
                    per_link_timeout_seconds=per_link_timeout,
                    deadline=deadline,
                    **row_builder_kwargs,
                )
            )
        return rows

    rows = [None] * len(links)
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch-parse")
    future_map = {}
    try:
        for index, link in enumerate(links):
            if time.monotonic() >= deadline:
                rows[index] = build_batch_skipped_row(link, "批量解析总预算已用尽，本条已跳过。")
                continue
            future = executor.submit(
                row_builder,
                request,
                link,
                prefer_ultra_hd=prefer_ultra_hd,
                prefer_youtube_merge=prefer_youtube_merge,
                per_link_timeout_seconds=per_link_timeout,
                deadline=deadline,
                **row_builder_kwargs,
            )
            future_map[future] = (index, link)

        remaining = max(0.0, deadline - time.monotonic())
        try:
            completed_futures = as_completed(future_map, timeout=remaining)
            for future in completed_futures:
                index, link = future_map[future]
                try:
                    rows[index] = future.result()
                except Exception as error:
                    rows[index] = {
                        "status": "failed",
                        "source_text": link,
                        "duration_ms": 0,
                        "error_message": sanitize_observability_text(f"{type(error).__name__}: {error}"),
                    }
        except FutureTimeoutError:
            pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    for index, row in enumerate(rows):
        if row is None:
            rows[index] = build_batch_skipped_row(
                links[index],
                "批量解析总预算已用尽，本条已跳过。",
            )
    return rows


def _missing_display_context_builder(*_args, **_kwargs):
    raise ValueError("display_context_builder is required")
