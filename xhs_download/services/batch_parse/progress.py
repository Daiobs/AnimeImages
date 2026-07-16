import hashlib
import secrets
import time
from dataclasses import dataclass

from django.core import signing
from django.core.cache import cache
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.urls import reverse

from ..rate_limits import get_client_identity
from .results import build_batch_skipped_row, split_batch_links
from .runtime import (
    batch_parse_effective_per_link_timeout_seconds,
    batch_parse_limit_error_message,
    batch_parse_max_links,
    batch_parse_progressive_scheduling_timeout_seconds,
    batch_parse_public_ultra_hd_allowed,
    batch_parse_public_youtube_merge_allowed,
    batch_parse_rate_limit_results,
    batch_parse_user_is_staff,
    build_batch_parse_budget,
)


BATCH_PROGRESS_TOKEN_SALT = "xhs_download.batch_parse.progress.v1"
BATCH_PROGRESS_TOKEN_MAX_AGE_SECONDS = 10 * 60
BATCH_PROGRESS_ITEM_CLAIMED = "claimed"
BATCH_PROGRESS_ITEM_DUPLICATE = "duplicate"
BATCH_PROGRESS_ITEM_OUT_OF_ORDER = "out_of_order"


class BatchProgressTokenError(ValueError):
    pass


@dataclass(frozen=True)
class BatchProgressRun:
    links: tuple[str, ...]
    prefer_ultra_hd: bool
    prefer_youtube_merge: bool
    started_at_epoch: float
    scheduling_timeout_seconds: float


def create_batch_progress_token(
    links,
    *,
    prefer_ultra_hd,
    prefer_youtube_merge,
    client_identity,
    scheduling_timeout_seconds,
):
    return signing.dumps(
        {
            "links": list(links),
            "prefer_ultra_hd": bool(prefer_ultra_hd),
            "prefer_youtube_merge": bool(prefer_youtube_merge),
            "client_identity": str(client_identity or ""),
            "started_at_epoch": time.time(),
            "scheduling_timeout_seconds": float(scheduling_timeout_seconds),
            "nonce": secrets.token_urlsafe(12),
        },
        salt=BATCH_PROGRESS_TOKEN_SALT,
        compress=True,
    )


def load_batch_progress_token(token, *, client_identity):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=BATCH_PROGRESS_TOKEN_SALT,
            max_age=BATCH_PROGRESS_TOKEN_MAX_AGE_SECONDS,
        )
    except (signing.BadSignature, signing.SignatureExpired) as error:
        raise BatchProgressTokenError("批量解析任务已失效，请重新提交。") from error

    if not isinstance(payload, dict):
        raise BatchProgressTokenError("批量解析任务格式无效，请重新提交。")
    links = payload.get("links")
    if (
        not isinstance(links, list)
        or not links
        or not all(isinstance(link, str) and link.strip() for link in links)
    ):
        raise BatchProgressTokenError("批量解析任务格式无效，请重新提交。")
    if not secrets.compare_digest(
        str(payload.get("client_identity") or ""),
        str(client_identity or ""),
    ):
        raise BatchProgressTokenError("批量解析任务与当前访问来源不匹配。")
    try:
        started_at_epoch = float(payload["started_at_epoch"])
        scheduling_timeout_seconds = max(
            1.0,
            float(payload["scheduling_timeout_seconds"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BatchProgressTokenError("批量解析任务格式无效，请重新提交。") from error

    return BatchProgressRun(
        links=tuple(link.strip() for link in links),
        prefer_ultra_hd=bool(payload.get("prefer_ultra_hd")),
        prefer_youtube_merge=bool(payload.get("prefer_youtube_merge")),
        started_at_epoch=started_at_epoch,
        scheduling_timeout_seconds=scheduling_timeout_seconds,
    )


def _batch_progress_cache_key(token, index, state):
    token_digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:32]
    return f"batch-progress:{token_digest}:{int(index)}:{state}"


def claim_batch_progress_item(token, index):
    index = int(index)
    if index > 0 and not cache.get(
        _batch_progress_cache_key(token, index - 1, "complete")
    ):
        return BATCH_PROGRESS_ITEM_OUT_OF_ORDER
    claimed = cache.add(
        _batch_progress_cache_key(token, index, "claimed"),
        1,
        timeout=BATCH_PROGRESS_TOKEN_MAX_AGE_SECONDS + 60,
    )
    return BATCH_PROGRESS_ITEM_CLAIMED if claimed else BATCH_PROGRESS_ITEM_DUPLICATE


def complete_batch_progress_item(token, index):
    cache.set(
        _batch_progress_cache_key(token, index, "complete"),
        1,
        timeout=BATCH_PROGRESS_TOKEN_MAX_AGE_SECONDS + 60,
    )


def _request_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _json_response(payload, *, status=200, retry_after=0):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    if retry_after:
        response["Retry-After"] = str(max(1, int(retry_after)))
    return response


def build_batch_progress_start_response(
    request,
    *,
    ultra_hd_available_func,
    youtube_merge_mode_available_func,
):
    links = split_batch_links(
        request.POST.get("links", ""),
        max_links=batch_parse_max_links(request),
    )
    if not links:
        return _json_response(
            {
                "ok": False,
                "error_code": "INVALID_BATCH_LINKS",
                "message": "请至少输入一条有效链接。",
            },
            status=400,
        )

    limits = batch_parse_rate_limit_results(request, len(links))
    blocked = next(((scope, limit) for scope, limit in limits if not limit.allowed), None)
    if blocked:
        blocked_scope, blocked_limit = blocked
        return _json_response(
            {
                "ok": False,
                "error_code": "BATCH_START_RATE_LIMITED",
                "message": batch_parse_limit_error_message(
                    blocked_scope,
                    blocked_limit,
                ),
            },
            status=429,
            retry_after=blocked_limit.retry_after,
        )

    ultra_hd_available = (
        batch_parse_public_ultra_hd_allowed(request)
        and ultra_hd_available_func(request)
    )
    youtube_merge_available = (
        batch_parse_public_youtube_merge_allowed(request)
        and youtube_merge_mode_available_func(request)
    )
    prefer_ultra_hd = _request_flag(request.POST.get("ultra_hd")) and ultra_hd_available
    prefer_youtube_merge = (
        _request_flag(request.POST.get("youtube_merge"))
        and youtube_merge_available
    )
    scheduling_timeout_seconds = (
        batch_parse_progressive_scheduling_timeout_seconds()
    )
    token = create_batch_progress_token(
        links,
        prefer_ultra_hd=prefer_ultra_hd,
        prefer_youtube_merge=prefer_youtube_merge,
        client_identity=get_client_identity(request),
        scheduling_timeout_seconds=scheduling_timeout_seconds,
    )
    return _json_response(
        {
            "ok": True,
            "batch_run_token": token,
            "total_count": len(links),
            "scheduling_timeout_seconds": scheduling_timeout_seconds,
            "per_link_timeout_seconds": batch_parse_effective_per_link_timeout_seconds(
                prefer_ultra_hd=prefer_ultra_hd,
            ),
        }
    )


def build_batch_progress_item_response(
    request,
    *,
    ultra_hd_available_func,
    youtube_merge_mode_available_func,
    rows_builder,
):
    token = str(request.POST.get("batch_run_token") or "")
    try:
        index = int(request.POST.get("index", ""))
    except (TypeError, ValueError):
        index = -1
    try:
        run = load_batch_progress_token(
            token,
            client_identity=get_client_identity(request),
        )
    except BatchProgressTokenError as error:
        return _json_response(
            {
                "ok": False,
                "error_code": "INVALID_BATCH_RUN_TOKEN",
                "message": str(error),
            },
            status=400,
        )
    if index < 0 or index >= len(run.links):
        return _json_response(
            {
                "ok": False,
                "error_code": "INVALID_BATCH_ITEM_INDEX",
                "message": "批量解析条目序号无效。",
            },
            status=400,
        )
    claim_result = claim_batch_progress_item(token, index)
    if claim_result == BATCH_PROGRESS_ITEM_OUT_OF_ORDER:
        return _json_response(
            {
                "ok": False,
                "error_code": "BATCH_ITEM_OUT_OF_ORDER",
                "message": "请等待上一条链接解析完成。",
            },
            status=409,
        )
    if claim_result != BATCH_PROGRESS_ITEM_CLAIMED:
        return _json_response(
            {
                "ok": False,
                "error_code": "BATCH_ITEM_ALREADY_CONSUMED",
                "message": "本条批量解析请求已经执行，请勿重复提交。",
            },
            status=409,
        )

    source_text = run.links[index]
    item_started_at = time.monotonic()
    if time.time() >= run.started_at_epoch + run.scheduling_timeout_seconds:
        row = build_batch_skipped_row(
            source_text,
            "等待执行时批次处理窗口已结束，本条未启动。",
        )
    else:
        prefer_ultra_hd = (
            run.prefer_ultra_hd
            and batch_parse_public_ultra_hd_allowed(request)
            and ultra_hd_available_func(request)
        )
        prefer_youtube_merge = (
            run.prefer_youtube_merge
            and batch_parse_public_youtube_merge_allowed(request)
            and youtube_merge_mode_available_func(request)
        )
        budget = build_batch_parse_budget(
            1,
            prefer_ultra_hd=prefer_ultra_hd,
        )
        rows = rows_builder(
            request,
            [source_text],
            prefer_ultra_hd=prefer_ultra_hd,
            prefer_youtube_merge=prefer_youtube_merge,
            deadline=item_started_at + budget.scheduling_timeout_seconds,
            response_deadline=item_started_at + budget.max_response_timeout_seconds,
            budget=budget,
        )
        row = rows[0]

    item_wall_duration_ms = int((time.monotonic() - item_started_at) * 1000)
    card_html = render_to_string(
        "partials/batch_result_card.html",
        {
            "row": row,
            "batch_direct_links_allowed": batch_parse_user_is_staff(request),
            "proxy_image_url": reverse("media_proxy_image"),
        },
        request=request,
    )
    complete_batch_progress_item(token, index)
    return _json_response(
        {
            "ok": True,
            "index": index,
            "status": row.get("status") or "failed",
            "card_html": card_html,
            "item_wall_duration_ms": item_wall_duration_ms,
        }
    )
