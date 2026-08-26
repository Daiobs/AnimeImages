import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlparse

import requests

from ..common import XhsError, XhsParseError, XhsProviderTimeoutError
from ..env_config import runtime_int as env_runtime_int
from ..observability import sanitize_observability_text
from ..provider_access import PROVIDER_THIRD_PARTY, provider_is_allowed_for_request
from ..rate_limits import get_client_identity
from ..registry import get_adapter_for_url, parse_media_page
from ..region_routing import get_request_country_code
from ..platforms.xhs_tiantian_hybrid import XhsUltraAttemptController


BATCH_DOUYIN_PROVIDER_ORDER = ("tiantianjiexi", "native")
BATCH_SKIPPED_MESSAGE = "等待执行时批次处理窗口已结束，本条未启动。"
BATCH_ITEM_TIMEOUT_MESSAGE = "本条解析超过单条时间限制。"
BATCH_PROVIDER_TIMEOUT_MESSAGE = "上游解析服务响应超时，请稍后重试。"
BATCH_PUBLIC_PROVIDER_TIMEOUT_MESSAGE = "解析服务响应超时，请稍后重试。"
BATCH_RESPONSE_TIMEOUT_MESSAGE = "本条解析超过批次最大响应时间。"
BATCH_PUBLIC_FAILURE_MESSAGE = "解析执行失败，请检查链接后重试。"
BATCH_NO_MEDIA_MESSAGE = "当前链接没有解析到可用图片或视频。"
BATCH_PROVIDER_STAGES = frozenset(
    {"resolve", "tiantianjiexi", "native", "display_context", "completed", "unknown"}
)
BATCH_PROVIDER_ATTEMPT_STAGES = frozenset({"tiantianjiexi", "native"})
BATCH_PLATFORM_IDS = frozenset(
    {"douyin", "xiaohongshu", "bilibili", "third_party", "unknown"}
)
BATCH_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
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


logger = logging.getLogger(__name__)


def normalize_batch_platform(value):
    value = str(value or "").strip().lower()
    return value if value in BATCH_PLATFORM_IDS else "unknown"


def batch_platform_for_source(source_text):
    try:
        return normalize_batch_platform(get_adapter_for_url(source_text).platform_id)
    except XhsError:
        return "unknown"


def normalize_batch_request_id(value):
    value = str(value or "").strip()
    return value if BATCH_REQUEST_ID_RE.fullmatch(value) else ""


class BatchParseStageTracker:
    def __init__(self, *, platform="unknown", request_id=""):
        self._lock = threading.Lock()
        self._platform = normalize_batch_platform(platform)
        self._request_id = normalize_batch_request_id(request_id)
        self._provider_stage = "unknown"
        self._attempted_provider_count = 0
        self._started_at = 0.0

    def start(self):
        with self._lock:
            if not self._started_at:
                self._started_at = time.monotonic()

    def update(self, stage, *, provider_attempt=False):
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in BATCH_PROVIDER_STAGES:
            normalized_stage = "unknown"
        with self._lock:
            self._provider_stage = normalized_stage
            if provider_attempt and normalized_stage in BATCH_PROVIDER_ATTEMPT_STAGES:
                self._attempted_provider_count += 1

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            elapsed_ms = (
                int(max(0.0, now - self._started_at) * 1000)
                if self._started_at
                else 0
            )
            return {
                "platform": self._platform,
                "provider_stage": self._provider_stage,
                "attempted_provider_count": self._attempted_provider_count,
                "elapsed_ms": elapsed_ms,
                "request_id": self._request_id,
            }


def summarize_stage_snapshots(snapshots):
    if not snapshots:
        return BatchParseStageTracker().snapshot()

    def common_value(key, fallback):
        values = {snapshot.get(key, fallback) for snapshot in snapshots}
        return values.pop() if len(values) == 1 else fallback

    return {
        "platform": common_value("platform", "unknown"),
        "provider_stage": common_value("provider_stage", "unknown"),
        "attempted_provider_count": sum(
            int(snapshot.get("attempted_provider_count") or 0)
            for snapshot in snapshots
        ),
        "elapsed_ms": max(
            int(snapshot.get("elapsed_ms") or 0)
            for snapshot in snapshots
        ),
        "request_id": common_value("request_id", ""),
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
    safe_reason = sanitize_observability_text(reason)
    return {
        "status": "skipped",
        "source_text": source_text,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "error_message": safe_reason,
        "public_error_message": safe_reason,
    }


def build_batch_timeout_row(
    source_text,
    reason,
    started_at=None,
    duration_ms=None,
    public_reason=None,
):
    started_at = started_at or time.monotonic()
    if duration_ms is None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
    safe_reason = sanitize_observability_text(reason)
    return {
        "status": "timeout",
        "source_text": source_text,
        "duration_ms": max(0, int(duration_ms or 0)),
        "error_message": safe_reason,
        "public_error_message": sanitize_observability_text(
            public_reason if public_reason is not None else reason
        ),
    }


def build_batch_failed_row(
    source_text,
    reason,
    started_at=None,
    duration_ms=None,
    public_reason=BATCH_PUBLIC_FAILURE_MESSAGE,
):
    started_at = started_at or time.monotonic()
    if duration_ms is None:
        duration_ms = int((time.monotonic() - started_at) * 1000)
    return {
        "status": "failed",
        "source_text": source_text,
        "duration_ms": max(0, int(duration_ms or 0)),
        "error_message": sanitize_observability_text(reason),
        "public_error_message": sanitize_observability_text(public_reason),
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
    stage_tracker=None,
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
        if stage_tracker is not None:
            stage_tracker.update("resolve")
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
        parser_kwargs = {
            "prefer_ultra_hd": prefer_ultra_hd,
            "client_country_code": (
                request_country_code_builder(request) if prefer_ultra_hd else ""
            ),
            "prefer_youtube_merge": prefer_youtube_merge,
            "douyin_provider_order": BATCH_DOUYIN_PROVIDER_ORDER,
            "timeout_seconds": remaining_timeout,
            "deadline_monotonic": execution_deadline,
        }
        if prefer_ultra_hd and batch_platform_for_source(source_text) == "xiaohongshu":
            parser_kwargs["ultra_attempt_controller"] = XhsUltraAttemptController(
                client_identity=get_client_identity(request),
                limit=env_runtime_int("ULTRA_HD_RATE_LIMIT_PER_MINUTE", 10),
            )
        if stage_tracker is not None:
            parser_kwargs["stage_callback"] = stage_tracker.update
        result = parser(
            source_text,
            **parser_kwargs,
        )
        if stage_tracker is not None:
            stage_tracker.update("display_context")
        display_context = display_context_builder(result, platform_key="home")
        if not (
            display_context.get("video_items")
            or display_context.get("image_items")
        ):
            return build_batch_failed_row(
                source_text,
                BATCH_NO_MEDIA_MESSAGE,
                started_at,
                public_reason=BATCH_NO_MEDIA_MESSAGE,
            )
        metadata = result.metadata or {}
        selected_provider = metadata.get("selected_provider") or metadata.get("source_provider") or ""
        provider_notice = ""
        if result.platform == "xiaohongshu" and prefer_ultra_hd:
            if metadata.get("xhs_tiantian_hybrid_status") == "success":
                provider_notice = "已获取小红书原画 / 超高清资源。"
            elif metadata.get("xhs_tiantian_hybrid_status") == "fallback":
                provider_notice = "超高清解析暂不可用，已返回普通清晰度。"
        row = {
            "status": "success",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "provider_label": batch_provider_display_label(metadata),
            "selected_provider": selected_provider,
            "provider_notice": provider_notice,
            **display_context,
        }
        enrich_batch_media_items(row)
        if stage_tracker is not None:
            stage_tracker.update("completed")
        return row
    except XhsProviderTimeoutError:
        return build_batch_timeout_row(
            source_text,
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            started_at,
            public_reason=BATCH_PUBLIC_PROVIDER_TIMEOUT_MESSAGE,
        )
    except requests.Timeout:
        return build_batch_timeout_row(
            source_text,
            BATCH_PROVIDER_TIMEOUT_MESSAGE,
            started_at,
            public_reason=BATCH_PUBLIC_PROVIDER_TIMEOUT_MESSAGE,
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
    response_deadline,
    completion_grace_seconds,
    row_builder_kwargs,
    stage_tracker,
):
    started_at = time.monotonic()
    stage_tracker.start()
    stage_tracker.update("resolve")
    response_deadline = float(response_deadline)
    if started_at >= response_deadline:
        return build_batch_timeout_row(
            source_text,
            BATCH_RESPONSE_TIMEOUT_MESSAGE,
            started_at,
        )
    execution_deadline = min(
        started_at + max(0.001, float(per_link_timeout_seconds or 0)),
        response_deadline,
    )
    try:
        row = row_builder(
            request,
            source_text,
            prefer_ultra_hd=prefer_ultra_hd,
            prefer_youtube_merge=prefer_youtube_merge,
            per_link_timeout_seconds=per_link_timeout_seconds,
            deadline=execution_deadline,
            stage_tracker=stage_tracker,
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
    completed_within_grace = (
        row.get("status") == "success"
        and stage_tracker.snapshot().get("provider_stage") == "completed"
        and finished_at
        <= min(
            response_deadline,
            execution_deadline
            + min(max(0.0, float(completion_grace_seconds or 0)), 2.0),
        )
    )
    if (
        finished_at >= execution_deadline
        and row.get("status") != "timeout"
        and not completed_within_grace
    ):
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


def iter_batch_result_rows(
    request,
    links,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    deadline,
    response_deadline,
    parallel_workers,
    per_link_timeout_seconds,
    scheduling_timeout_seconds,
    max_response_timeout_seconds,
    completion_grace_seconds=1.0,
    row_builder=None,
    row_builder_kwargs=None,
):
    """Schedule rows within explicit scheduling and HTTP response deadlines.

    Batch-reachable providers should finish cooperatively by their item deadline.
    Python cannot safely kill a running thread, so a worker that overruns the final
    response boundary is logged and detached from the request's blocking path.
    """
    if not links:
        return

    row_builder = row_builder or build_batch_result_row
    row_builder_kwargs = row_builder_kwargs or {}
    worker_count = min(max(1, int(parallel_workers or 1)), len(links))
    per_link_timeout = max(0.001, float(per_link_timeout_seconds or 0))
    completion_grace = min(max(0.0, float(completion_grace_seconds or 0)), 2.0)
    scheduling_deadline = float(deadline)
    response_deadline = float(response_deadline)
    scheduling_timeout = max(0.0, float(scheduling_timeout_seconds or 0))
    max_response_timeout = max(0.0, float(max_response_timeout_seconds or 0))
    rows = [None] * len(links)
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch-parse")
    active_futures = {}
    next_index = 0
    next_emit_index = 0
    shutdown_wait = False

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
            now = time.monotonic()
            if now >= scheduling_deadline or now >= response_deadline:
                mark_remaining_skipped()
                return
            index = next_index
            link = links[index]
            next_index += 1
            stage_tracker = BatchParseStageTracker(
                platform=batch_platform_for_source(link),
                request_id=getattr(request, "request_id", ""),
            )
            future = executor.submit(
                _execute_batch_result_row,
                row_builder,
                request,
                link,
                prefer_ultra_hd=prefer_ultra_hd,
                prefer_youtube_merge=prefer_youtube_merge,
                per_link_timeout_seconds=per_link_timeout,
                response_deadline=response_deadline,
                completion_grace_seconds=completion_grace,
                row_builder_kwargs=row_builder_kwargs,
                stage_tracker=stage_tracker,
            )
            active_futures[future] = (index, link, stage_tracker)

    def collect_future(future):
        index, link, _stage_tracker = active_futures.pop(future)
        try:
            rows[index] = future.result()
        except Exception as error:
            rows[index] = build_batch_failed_row(
                link,
                f"解析执行失败：{type(error).__name__}: {error}",
            )

    def emit_ready_rows():
        nonlocal next_emit_index
        while next_emit_index < len(rows) and rows[next_emit_index] is not None:
            index = next_emit_index
            next_emit_index += 1
            yield index, rows[index]

    def stop_at_response_deadline():
        mark_remaining_skipped()
        unresolved_count = 0
        unresolved_snapshots = []
        for future in tuple(active_futures):
            index, link, stage_tracker = active_futures[future]
            if future.done():
                collect_future(future)
                continue
            if future.cancel():
                active_futures.pop(future)
                rows[index] = build_batch_skipped_row(
                    link,
                    BATCH_SKIPPED_MESSAGE,
                )
                continue
            if future.done():
                collect_future(future)
                continue
            active_futures.pop(future)
            rows[index] = build_batch_timeout_row(
                link,
                BATCH_RESPONSE_TIMEOUT_MESSAGE,
                duration_ms=int(max_response_timeout * 1000),
            )
            unresolved_count += 1
            unresolved_snapshots.append(stage_tracker.snapshot())

        if unresolved_count:
            stage_diagnostics = summarize_stage_snapshots(unresolved_snapshots)
            logger.warning(
                "batch_parse_worker_deadline_overrun",
                extra={
                    "unresolved_count": unresolved_count,
                    "worker_count": worker_count,
                    "link_count": len(links),
                    "configured_scheduling_timeout_seconds": scheduling_timeout,
                    "configured_per_link_timeout_seconds": per_link_timeout,
                    "configured_completion_grace_seconds": completion_grace,
                    "configured_response_timeout_seconds": max_response_timeout,
                    **stage_diagnostics,
                },
            )
        return unresolved_count

    try:
        submit_available()
        while active_futures:
            now = time.monotonic()
            if now >= response_deadline:
                unresolved_count = stop_at_response_deadline()
                shutdown_wait = unresolved_count == 0
                break
            if next_index < len(links) and now >= scheduling_deadline:
                mark_remaining_skipped()

            remaining_response_time = max(0.0, response_deadline - now)
            wait_timeout = remaining_response_time
            if next_index < len(links):
                wait_timeout = min(
                    wait_timeout,
                    max(0.0, scheduling_deadline - now),
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
                collect_future(future)
            yield from emit_ready_rows()
            if next_index < len(links):
                submit_available()
        else:
            shutdown_wait = True
    finally:
        # Only a deadline overrun or scheduler exception takes the non-blocking path.
        executor.shutdown(wait=shutdown_wait, cancel_futures=True)

    for index, row in enumerate(rows):
        if row is None:
            rows[index] = build_batch_skipped_row(
                links[index],
                BATCH_SKIPPED_MESSAGE,
            )
    yield from emit_ready_rows()


def build_batch_result_rows(request, links, **kwargs):
    """Collect the progressive scheduler output for synchronous form fallback."""
    rows = [None] * len(links)
    for index, row in iter_batch_result_rows(request, links, **kwargs):
        rows[index] = row
    return [
        row
        if row is not None
        else build_batch_skipped_row(links[index], BATCH_SKIPPED_MESSAGE)
        for index, row in enumerate(rows)
    ]


def _missing_display_context_builder(*_args, **_kwargs):
    raise ValueError("display_context_builder is required")
