import time

from .results import build_batch_result_rows, build_batch_summary, split_batch_links
from .runtime import (
    batch_parse_access_allowed,
    batch_parse_limit_error_message,
    batch_parse_max_links,
    batch_parse_public_ultra_hd_allowed,
    batch_parse_public_youtube_merge_allowed,
    batch_parse_progressive_scheduling_timeout_seconds,
    batch_parse_rate_limit_results,
    batch_parse_total_timeout_seconds,
    batch_parse_user_is_staff,
    build_batch_parse_budget,
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
    budget_prefer_ultra_hd = requested_ultra_hd and ultra_hd_available
    budget = build_batch_parse_budget(
        len(links) or max_links,
        prefer_ultra_hd=budget_prefer_ultra_hd,
    )
    progressive_scheduling_timeout_seconds = (
        batch_parse_progressive_scheduling_timeout_seconds()
    )
    rows = []
    batch_error = ""
    batch_wall_duration_ms = 0
    if request.method == "POST":
        limits = batch_parse_rate_limit_results(request, len(links) or 1)
        blocked = next(((scope, limit) for scope, limit in limits if not limit.allowed), None)
        blocked_scope, blocked_limit = blocked or (None, None)
        if blocked_limit:
            batch_error = batch_parse_limit_error_message(blocked_scope, blocked_limit)
        else:
            batch_started_at = time.monotonic()
            deadline = batch_started_at + budget.scheduling_timeout_seconds
            response_deadline = (
                batch_started_at + budget.max_response_timeout_seconds
            )
            if rows_builder is not None:
                rows = rows_builder(
                    request,
                    links,
                    prefer_ultra_hd=prefer_ultra_hd,
                    prefer_youtube_merge=prefer_youtube_merge,
                    deadline=deadline,
                    response_deadline=response_deadline,
                    budget=budget,
                )
            else:
                rows = build_batch_result_rows(
                    request,
                    links,
                    prefer_ultra_hd=prefer_ultra_hd,
                    prefer_youtube_merge=prefer_youtube_merge,
                    deadline=deadline,
                    response_deadline=response_deadline,
                    parallel_workers=budget.worker_count,
                    per_link_timeout_seconds=budget.per_link_timeout_seconds,
                    scheduling_timeout_seconds=budget.scheduling_timeout_seconds,
                    max_response_timeout_seconds=budget.max_response_timeout_seconds,
                    completion_grace_seconds=budget.completion_grace_seconds,
                    row_builder=row_builder,
                )
            batch_wall_duration_ms = int(
                (time.monotonic() - batch_started_at) * 1000
            )

    return {
        "theme_class": "theme-home",
        "page_title": "批量解析",
        "links_text": raw_links,
        "links": links,
        "rows": rows,
        "batch_summary": build_batch_summary(
            rows,
            batch_wall_duration_ms=batch_wall_duration_ms,
        ),
        "max_links": max_links,
        "batch_per_link_timeout_seconds": budget.per_link_timeout_seconds,
        "batch_total_timeout_seconds": batch_parse_total_timeout_seconds(),
        "batch_effective_total_timeout_seconds": budget.scheduling_timeout_seconds,
        "batch_progressive_scheduling_timeout_seconds": progressive_scheduling_timeout_seconds,
        "batch_progressive_max_duration_seconds": (
            progressive_scheduling_timeout_seconds
            + budget.per_link_timeout_seconds
            + budget.completion_grace_seconds
        ),
        "batch_parallel_workers": budget.worker_count,
        "batch_budget": budget,
        "batch_error": batch_error,
        "batch_staff_only": not batch_parse_access_allowed(request),
        "batch_download_allowed": batch_parse_access_allowed(request),
        "batch_raw_links_allowed": batch_parse_user_is_staff(request),
        "prefer_ultra_hd": prefer_ultra_hd,
        "batch_ultra_hd_checked": requested_ultra_hd and ultra_hd_available,
        "prefer_youtube_merge": prefer_youtube_merge,
        "batch_youtube_merge_checked": requested_youtube_merge and youtube_merge_mode_available,
        "ultra_hd_available": ultra_hd_available,
        "youtube_merge_mode_available": youtube_merge_mode_available,
        "strategy_matrix": strategy_matrix,
        "proxy_image_url": proxy_image_url,
    }
