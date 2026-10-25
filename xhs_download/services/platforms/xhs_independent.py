"""Independent, request-authorized XHS service candidates with Native fallback."""

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from hashlib import sha256
from threading import Event
import time

from ..common import XhsParseError
from ..quality_execution import execute_provider, service_configured

SERVICE_ORDER = ("kukutool_generic", "tiantianjiexi_generic")


def display_resource_key(url):
    """Exact resource comparison only; never a download grant or URL alias."""
    return sha256(url.encode()).hexdigest() if isinstance(url, str) and url else ""


def _native_display_candidates(native, service, source_url):
    candidates = (service.metadata or {}).get("upstream_candidates", [])
    from .kukutool_generic import canonicalize_source_url
    from .xhs_page_state import extract_xhs_note_id, normalize_xhs_note_id
    from ..service_format_selection import SCHEMA

    metadata = native.metadata or {}
    note_id = normalize_xhs_note_id(metadata.get("note_id"))
    service_metadata = service.metadata or {}
    service_source = service_metadata.get("canonical_source_url")
    service_note_id = service_metadata.get("note_id")
    identity_signals = service_metadata.get("_source_identity_signals") or ()
    if not isinstance(identity_signals, (list, tuple)):
        identity_signals = ()
    service_note_ids = {identity for value in (*identity_signals, service_note_id)
                        if (identity := normalize_xhs_note_id(value))}
    if (getattr(native, "platform", "") != "xiaohongshu" or native.rich_media is not None
            or metadata.get("source_identity_verified") is not True or not note_id
            or not service_source
            or canonicalize_source_url(service_source) != canonicalize_source_url(source_url)
            or (service_note_id and normalize_xhs_note_id(service_note_id) != note_id)
            or any(identity != note_id for identity in service_note_ids)
            or any(identity and identity != note_id for identity in (
                extract_xhs_note_id(source_url), extract_xhs_note_id(service_source)))):
        return candidates

    videos = native.video_assets
    by_resource = {}
    for asset in videos:
        key = display_resource_key(asset.url)
        if key:
            by_resource.setdefault(key, []).append(asset)
    groups = {}
    resource_groups = {}
    for candidate in candidates:
        if (not isinstance(candidate, dict) or candidate.get("media_type") != "video"
                or candidate.get("provider_id") not in SERVICE_ORDER
                or candidate.get("provider_id") != service_metadata.get("source_provider")
                or not candidate.get("work_id") or candidate.get("work", {}).get("schema") != SCHEMA):
            continue
        group_id = (candidate["provider_id"], candidate["work_id"])
        groups.setdefault(group_id, []).append(candidate)
        key = candidate.get("resource_alias_key")
        if key in by_resource and len(by_resource[key]) == 1:
            resource_groups.setdefault(key, set()).add(group_id)

    links = {}
    for group_id, rows in groups.items():
        matches = {row.get("resource_alias_key") for row in rows
                   if resource_groups.get(row.get("resource_alias_key")) == {group_id}}
        if len(matches) == 1:
            links[group_id] = matches.pop()
        elif (not matches and len(groups) == 1 and len(videos) == 1
              and native.post_type == "video" and not native.image_assets
              and service_note_ids == {note_id}):
            # Different files share a cover only when the response confirms the
            # same work. Native keeps its own download control and authority.
            links[group_id] = display_resource_key(videos[0].url)
    if not links:
        return candidates
    displayed = []
    for row in candidates:
        key = links.get((row.get("provider_id"), row.get("work_id"))) if isinstance(row, dict) else None
        if key and row.get("media_type") == "video":
            row = {**row, "display_native_resource_key": key,
                   "display_same_resource": row.get("resource_alias_key") == key}
        displayed.append(row)
    return displayed


def authorized_services(request, source_url, *, reserve_limits=True):
    """Resolve server-side access only; URL/form options cannot grant authority."""
    from urllib.parse import urlsplit
    from django.conf import settings
    from .xiaohongshu import XiaohongshuAdapter
    from ..common import extract_url_from_text
    from ..provider_access import (
        PROVIDER_THIRD_PARTY, load_runtime_setting, provider_access_summary,
        request_user_is_staff, third_party_platform_is_blocked,
        upstream_match_is_blocked, upstream_match_is_public_allowed,
    )
    from ..rate_limits import consume_fixed_window, get_client_identity
    from ..env_config import runtime_default_int

    source = extract_url_from_text(source_url)
    if not XiaohongshuAdapter().supports_url(source):
        return ()
    config = load_runtime_setting()
    if not config or not provider_access_summary(PROVIDER_THIRD_PARTY, request, config)["request_allowed"]:
        return ()
    host = urlsplit(source).hostname
    if third_party_platform_is_blocked("xiaohongshu", host, config):
        return ()
    providers = []
    for provider in SERVICE_ORDER:
        match = {"provider_id": provider, "extractor_id": provider}
        if (not upstream_match_is_blocked(match, config)
                and (request_user_is_staff(request)
                     or upstream_match_is_public_allowed(match, host, config))
                and service_configured(provider)):
            providers.append(provider)
    if not providers:
        return ()
    if not reserve_limits:
        return tuple(providers)
    # Reuse the ordinary service limits; throttling must not disable Native.
    try:
        for scope, identity, limit in (
            ("third-party-provider-minute-ip", get_client_identity(request),
             runtime_default_int("THIRD_PARTY_RATE_LIMIT_PER_MINUTE", 10)),
            ("third-party-provider-minute-global", "all",
             settings.THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE),
        ):
            if not consume_fixed_window(scope, identity, limit, 60).allowed:
                return ()
    except Exception:
        return ()
    return tuple(providers)


def parse_independent(url, *, native_parse, provider_ids, deadline_monotonic=None,
                      timeout_seconds=None):
    providers = tuple(pid for pid in SERVICE_ORDER if pid in provider_ids and service_configured(pid))
    started = time.monotonic()
    deadline = min(float(deadline_monotonic or started + 30),
                   started + max(0, float(30 if timeout_seconds is None else timeout_seconds)))
    if deadline <= time.monotonic():
        raise XhsParseError("小红书解析超时，请稍后再试。")
    if not providers:
        return native_parse(deadline)

    # Leave selection/display assembly time inside the existing hard deadline.
    # Short budgets scale down; long requests reserve at most 250ms.
    work_deadline = deadline - min(.25, (deadline - started) * .2)
    stop_waiting = Event()
    service_started = Event()

    def has_media(result):
        return result is not None and bool(
            result.assets or (result.metadata or {}).get("upstream_candidates"))

    def service_parse():
        for provider in providers:
            remaining = work_deadline - time.monotonic()
            # _execute_service clamps smaller timeouts to 100ms. Do not start
            # an attempt that would exceed its allotted remaining budget.
            if stop_waiting.is_set() or remaining < .1:
                break
            try:
                service_started.set()
                result = execute_provider(provider, url, timeout_seconds=min(20, remaining),
                                          service_authorized=True)
                if has_media(result):
                    return result
            except Exception:
                # Never expose signed URLs or service exceptions to the public page.
                continue
        return None

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xhs-independent")
    native = executor.submit(native_parse, work_deadline)
    service = executor.submit(service_parse)
    try:
        done, _ = wait((native, service), timeout=max(0, work_deadline - time.monotonic()))
        stop_waiting.set()
        native_result, native_error = None, None
        if native in done:
            try:
                native_result = native.result()
            except Exception as error:
                native_error = error
        if not has_media(native_result):
            native_result = None
        service_result = service.result() if service in done else None
        # Only Native's exact item snapshot can establish a LivePhoto pair.
        selected = (native_result if native_result is not None and (
                        native_result.rich_media is not None
                        or (native_result.assets and (service_result is None or not service_result.assets)))
                    else service_result or native_result)
        if selected is not None:
            metadata = dict(selected.metadata or {})
            if selected is native_result and service_result is not None:
                # Candidate-only service evidence must not remove usable Native
                # downloads or borrow their download authority/provider identity.
                metadata["upstream_candidates"] = _native_display_candidates(
                    native_result, service_result, url)
                for key in ("candidate_count", "candidate_returned_count", "candidate_retained_count",
                            "candidate_unexpanded_count", "returned_count", "retained_count",
                            "declared_count", "examined_count", "rejected_count", "unexpanded_count",
                            "truncated_count", "candidates_truncated"):
                    if key in service_result.metadata:
                        metadata[key] = service_result.metadata[key]
            if selected is service_result:
                # Image candidates alone are not evidence of an undeliverable video.
                metadata["limited_media_count"] = sum(
                    item.get("media_type") == "video"
                    for item in metadata.get("upstream_candidates", [])
                )
            return replace(selected, metadata={**metadata,
                "xhs_independent_source": "service" if selected is service_result else "native",
                "xhs_native_status": "failed" if native_error else "completed" if native in done else "deadline",
                "xhs_service_status": ("candidates" if service_result else
                    "not_started_budget" if not service_started.is_set() else
                    "unavailable" if service in done else "deadline")})
        if native_error is not None:
            raise native_error
        if native in done and service in done:
            raise XhsParseError("小红书未返回可用素材。")
        raise XhsParseError("小红书解析超时，请稍后再试。")
    finally:
        # Stopping this request's wait does not terminate an in-flight HTTP call.
        # Its own timeout/account lock remains in charge; no next service starts.
        stop_waiting.set()
        native.cancel()
        service.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
