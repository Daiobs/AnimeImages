import time

from .results import build_batch_result_rows, build_batch_summary, split_batch_links
from .runtime import (
    batch_parse_access_allowed,
    batch_parse_effective_per_link_timeout_seconds,
    batch_parse_limit_error_message,
    batch_parse_max_links,
    batch_parse_parallel_workers,
    batch_parse_public_ultra_hd_allowed,
    batch_parse_public_youtube_merge_allowed,
    batch_parse_rate_limit_results,
    batch_parse_total_timeout_seconds,
    batch_parse_user_is_staff,
)


def request_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_batch_parse_context(
    request,
    *,
    ultra_hd_available_func,
    youtube_merge_mode_available_func,
    row_builder,
    rows_builder=None,
    strategy_matrix,
    proxy_image_url,
):
    raw_links = request.POST.get("links", "") if request.method == "POST" else ""
    ultra_hd_available = (
        batch_parse_public_ultra_hd_allowed(request)
        and ultra_hd_available_func(request)
    )
    youtube_merge_mode_available = (
        batch_parse_public_youtube_merge_allowed(request)
        and youtube_merge_mode_available_func(request)
    )
    requested_ultra_hd = (
        request_flag(request.POST.get("ultra_hd"))
        if request.method == "POST"
        else ultra_hd_available
    )
    requested_youtube_merge = (
        request_flag(request.POST.get("youtube_merge"))
        if request.method == "POST"
        else False
    )
    prefer_ultra_hd = (
        request.method == "POST"
        and requested_ultra_hd
        and ultra_hd_available
    )
    prefer_youtube_merge = (
        request.method == "POST"
        and requested_youtube_merge
        and youtube_merge_mode_available
    )
    max_links = batch_parse_max_links(request)
    links = split_batch_links(raw_links, max_links=max_links)
    rows = []
    batch_error = ""
    if request.method == "POST":
        limits = batch_parse_rate_limit_results(request, len(links) or 1)
        blocked = next(((scope, limit) for scope, limit in limits if not limit.allowed), None)
        blocked_scope, blocked_limit = blocked or (None, None)
        if blocked_limit:
            batch_error = batch_parse_limit_error_message(blocked_scope, blocked_limit)
        else:
            deadline = time.monotonic() + batch_parse_total_timeout_seconds()
            per_link_timeout_seconds = batch_parse_effective_per_link_timeout_seconds(
                prefer_ultra_hd=prefer_ultra_hd,
            )
            if rows_builder is not None:
                rows = rows_builder(
                    request,
                    links,
                    prefer_ultra_hd=prefer_ultra_hd,
                    prefer_youtube_merge=prefer_youtube_merge,
                    deadline=deadline,
                )
            else:
                rows = build_batch_result_rows(
                    request,
                    links,
                    prefer_ultra_hd=prefer_ultra_hd,
                    prefer_youtube_merge=prefer_youtube_merge,
                    deadline=deadline,
                    parallel_workers=batch_parse_parallel_workers(),
                    per_link_timeout_seconds=per_link_timeout_seconds,
                    row_builder=row_builder,
                )

    return {
        "theme_class": "theme-home",
        "page_title": "批量解析",
        "links_text": raw_links,
        "links": links,
        "rows": rows,
        "batch_summary": build_batch_summary(rows),
        "max_links": max_links,
        "batch_per_link_timeout_seconds": batch_parse_effective_per_link_timeout_seconds(
            prefer_ultra_hd=prefer_ultra_hd,
        ),
        "batch_total_timeout_seconds": batch_parse_total_timeout_seconds(),
        "batch_parallel_workers": batch_parse_parallel_workers(),
        "batch_error": batch_error,
        "batch_staff_only": not batch_parse_access_allowed(request),
        "batch_direct_links_allowed": batch_parse_user_is_staff(request),
        "prefer_ultra_hd": prefer_ultra_hd,
        "batch_ultra_hd_checked": requested_ultra_hd and ultra_hd_available,
        "prefer_youtube_merge": prefer_youtube_merge,
        "batch_youtube_merge_checked": requested_youtube_merge and youtube_merge_mode_available,
        "ultra_hd_available": ultra_hd_available,
        "youtube_merge_mode_available": youtube_merge_mode_available,
        "strategy_matrix": strategy_matrix,
        "proxy_image_url": proxy_image_url,
    }
