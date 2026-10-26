import logging
import time

from ..assets import MediaAsset, ParseResult
from ..pipeline_shadow_capture import capture_enabled, capture_error, capture_legacy_sources
from ..douyin_ultra_attempts import (
    DouyinUltraAttemptAccountingError,
    DouyinUltraAttemptRateLimitedError,
)
from ..env_config import runtime_default_float
from ..provider_manifest import build_provider_manifest_token
from ..provider_reliability import ProviderResultInvalidError
from ..rate_limits import consume_fixed_window
from .tiantianjiexi_generic import (
    PROVIDER_ID,
    TianTianJieXiGenericProvider,
    quality_rank,
    provider_configured,
    source_key_for_identity,
)
from .xhs_page_state import normalize_xhs_note_id
from .xhs_tiantian_media_policy import (
    XhsTianTianMediaPolicyError,
    probe_xhs_tiantian_video,
    validate_xhs_tiantian_media_url,
)


logger = logging.getLogger(__name__)

XHS_TIANTIAN_ULTRA_WINDOW_DEFAULT_SECONDS = 6.0
XHS_TIANTIAN_ULTRA_WINDOW_MAX_SECONDS = 8.0
XHS_TIANTIAN_MAX_PAYLOAD_FIELDS = 32
XHS_TIANTIAN_MAX_VIDEO_CANDIDATES = 16
XHS_TIANTIAN_MAX_CANDIDATE_FIELDS = 24
XHS_TIANTIAN_MAX_URL_LENGTH = 4096
XHS_TERMINAL_PAGE_STATES = frozenset(
    {"deleted", "private", "unavailable", "login_required"}
)


class XhsUltraAttemptRateLimitedError(DouyinUltraAttemptRateLimitedError):
    pass


class XhsUltraAttemptAccountingError(DouyinUltraAttemptAccountingError):
    pass


class XhsUltraAttemptController:
    """Consumes one per-client Ultra attempt only when fresh Provider work starts."""

    def __init__(self, *, client_identity, limit, consume=consume_fixed_window):
        self.client_identity = str(client_identity or "unknown")[:256]
        self.limit = max(0, int(limit or 0))
        self.consume = consume
        self.started = False
        self.denied = False
        self.reused = False
        self.retry_after = 0
        self.cooldown_retry_after = 0
        self.preflight_reason = ""

    def reuse_cached_result(self, provider, *, reason="payload_cache"):
        del provider
        self.reused = True
        self.preflight_reason = str(reason or "payload_cache")[:80]

    def prepare_fresh_work(self, provider):
        del provider
        self.preflight_reason = "fresh_work_ready"

    def skip_fresh_work(self, provider, *, reason, retry_after=0):
        del provider
        self.preflight_reason = str(reason or "fresh_work_skipped")[:80]
        self.cooldown_retry_after = max(
            self.cooldown_retry_after,
            max(0, int(retry_after or 0)),
        )

    def begin_provider(self, provider):
        del provider
        if self.started:
            return False
        try:
            result = self.consume(
                "xhs-ultra-minute-client",
                self.client_identity,
                self.limit,
                60,
            )
        except Exception as error:
            raise XhsUltraAttemptAccountingError(
                "XHS Ultra rate accounting unavailable"
            ) from error
        if not result.allowed:
            self.denied = True
            self.retry_after = max(0, int(result.retry_after or 0))
            raise XhsUltraAttemptRateLimitedError(self.retry_after)
        self.started = True
        return True

    def record_cooldown(self, provider, retry_after):
        del provider
        self.cooldown_retry_after = max(
            self.cooldown_retry_after,
            max(0, int(retry_after or 0)),
        )


def xhs_tiantian_provider_is_available(provider=None):
    if not provider_configured():
        return False
    provider = provider or TianTianJieXiGenericProvider()
    try:
        return not provider.circuit_is_open()
    except Exception:
        return False


def build_xhs_tiantian_source_identity(note_id):
    note_id = normalize_xhs_note_id(note_id)
    if not note_id:
        raise ValueError("XHS_TIANTIAN_NOTE_ID_INVALID")
    return f"xiaohongshu:{note_id}"


def build_xhs_tiantian_request_url(note_id):
    note_id = normalize_xhs_note_id(note_id)
    if not note_id:
        raise ValueError("XHS_TIANTIAN_NOTE_ID_INVALID")
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def _normalize_correspondence_text(value):
    return "".join(
        character.lower()
        for character in str(value or "")[:300]
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def xhs_tiantian_titles_correspond(native_title, provider_title):
    native = _normalize_correspondence_text(native_title)
    provider = _normalize_correspondence_text(provider_title)
    if not provider:
        return True
    if not native:
        return False
    return native == provider or native in provider or provider in native


def _safe_identity_signals(payload):
    signals = payload.get("_source_identity_signals") or ()
    if not isinstance(signals, (list, tuple)) or len(signals) > 8:
        raise ProviderResultInvalidError("XHS Provider identity structure invalid")
    normalized = []
    for value in signals:
        note_id = normalize_xhs_note_id(value)
        if not note_id:
            raise ProviderResultInvalidError("XHS Provider identity invalid")
        if note_id not in normalized:
            normalized.append(note_id)
    return tuple(normalized)


def validate_xhs_tiantian_payload(payload, *, note_id, native_title=""):
    if not isinstance(payload, dict) or len(payload) > XHS_TIANTIAN_MAX_PAYLOAD_FIELDS:
        raise ProviderResultInvalidError("XHS Provider payload invalid")
    videos = payload.get("videos") or ()
    if (
        not isinstance(videos, (list, tuple))
        or not videos
        or len(videos) > XHS_TIANTIAN_MAX_VIDEO_CANDIDATES
    ):
        raise ProviderResultInvalidError("XHS Provider video candidates invalid")

    public_video_count = 0
    for candidate in videos:
        if not isinstance(candidate, dict) or len(candidate) > XHS_TIANTIAN_MAX_CANDIDATE_FIELDS:
            raise ProviderResultInvalidError("XHS Provider candidate invalid")
        candidate_url = str(candidate.get("url") or "").strip()
        if not candidate_url or len(candidate_url) > XHS_TIANTIAN_MAX_URL_LENGTH:
            continue
        if candidate_url.startswith("https://"):
            public_video_count += 1
    if not public_video_count:
        raise ProviderResultInvalidError("XHS Provider did not return HTTPS video")

    normalized_note_id = normalize_xhs_note_id(note_id)
    signals = _safe_identity_signals(payload)
    if any(signal != normalized_note_id for signal in signals):
        raise ProviderResultInvalidError("XHS Provider identity mismatch")
    if not xhs_tiantian_titles_correspond(native_title, payload.get("title")):
        raise ProviderResultInvalidError("XHS Provider title mismatch")
    return True


def select_xhs_tiantian_video_candidate(payload):
    videos = tuple(payload.get("videos") or ())
    ranked = []
    for index, candidate in enumerate(videos):
        if not isinstance(candidate, dict):
            continue
        candidate_url = str(candidate.get("url") or "").strip()
        if not candidate_url.startswith("https://"):
            continue
        try:
            size_bytes = max(0, int(candidate.get("size") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        quality_label = str(candidate.get("quality") or "原画").strip()[:80]
        ranked.append(
            (
                quality_rank(quality_label),
                size_bytes,
                -index,
                {
                    "url": candidate_url,
                    "quality": quality_label or "原画",
                    "size": size_bytes,
                },
            )
        )
    if not ranked:
        raise ProviderResultInvalidError("XHS Provider video candidate missing")
    return max(ranked, key=lambda item: item[:3])[3]


def xhs_tiantian_native_result_is_eligible(result):
    metadata = getattr(result, "metadata", None) or {}
    note_id = normalize_xhs_note_id(metadata.get("note_id"))
    return bool(
        getattr(result, "platform", "") == "xiaohongshu"
        and getattr(result, "post_type", "") == "video"
        and getattr(result, "rich_media", None) is None
        and len(getattr(result, "video_assets", ())) == 1
        and str(result.video_assets[0].url or "").startswith(("http://", "https://"))
        and note_id
        and metadata.get("source_identity_verified") is True
        and str(metadata.get("page_state") or "") not in XHS_TERMINAL_PAGE_STATES
    )


def _stage(stage_callback, value, *, provider_attempt=False):
    if not callable(stage_callback):
        return
    try:
        stage_callback(value, provider_attempt=provider_attempt)
    except TypeError:
        stage_callback(value)


def _fallback_result(native_result, *, reason, attempt_controller=None, duration_ms=0):
    metadata = dict(native_result.metadata or {})
    metadata.update(
        {
            "selected_provider": "native",
            "xhs_tiantian_hybrid_status": "fallback",
            "xhs_tiantian_fallback_reason": str(reason or "unavailable")[:80],
            "xhs_tiantian_provider_duration_ms": max(0, int(duration_ms or 0)),
            "selected_provider_duration_ms": max(0, int(duration_ms or 0)),
            "provider_total_duration_ms": max(0, int(duration_ms or 0)),
            "provider_fallback_count": 1,
            "xhs_tiantian_external_call": bool(
                getattr(attempt_controller, "started", False)
            ),
            "xhs_tiantian_retry_after_seconds": max(
                int(getattr(attempt_controller, "retry_after", 0) or 0),
                int(getattr(attempt_controller, "cooldown_retry_after", 0) or 0),
            ),
            "xhs_tiantian_full_server_media_bytes": 0,
        }
    )
    return ParseResult(
        platform=native_result.platform,
        post_type=native_result.post_type,
        base_filename=native_result.base_filename,
        assets=native_result.assets,
        metadata=metadata,
    )


def _failure_reason(error, attempt_controller):
    if getattr(attempt_controller, "denied", False):
        return "rate_limit"
    reason = str(getattr(attempt_controller, "preflight_reason", "") or "")
    if reason in {"circuit_open", "failure_cooldown", "provider_busy", "provider_rate_limit"}:
        return reason
    code = str(getattr(error, "code", "") or "")
    if code.startswith("XHS_TIANTIAN_HOST") or code.startswith("XHS_TIANTIAN_URL"):
        return "host_reject"
    if code.startswith("XHS_TIANTIAN_MIME") or code.startswith("XHS_TIANTIAN_FORMAT"):
        return "format_reject"
    if "identity" in str(error).lower() or "title mismatch" in str(error).lower():
        return "identity_mismatch"
    if "timeout" in type(error).__name__.lower() or "timeout" in str(error).lower():
        return "timeout"
    if isinstance(error, ProviderResultInvalidError):
        return "invalid_result"
    return "provider_unavailable"


def enhance_xhs_ordinary_video(
    native_result,
    *,
    deadline_monotonic=0,
    attempt_controller=None,
    stage_callback=None,
    provider=None,
    probe_func=probe_xhs_tiantian_video,
):
    if not xhs_tiantian_native_result_is_eligible(native_result):
        return native_result
    provider = provider or TianTianJieXiGenericProvider()
    if not xhs_tiantian_provider_is_available(provider):
        return _fallback_result(
            native_result,
            reason="circuit_open" if provider_configured() else "not_configured",
            attempt_controller=attempt_controller,
        )

    started_at = time.monotonic()
    provider_window = min(
        XHS_TIANTIAN_ULTRA_WINDOW_MAX_SECONDS,
        max(
            0.1,
            runtime_default_float(
                "XHS_TIANTIAN_ULTRA_WINDOW_SECONDS",
                XHS_TIANTIAN_ULTRA_WINDOW_DEFAULT_SECONDS,
            ),
        ),
    )
    provider_deadline = started_at + provider_window
    if float(deadline_monotonic or 0):
        provider_deadline = min(provider_deadline, float(deadline_monotonic))
    if provider_deadline <= started_at:
        return _fallback_result(
            native_result,
            reason="timeout",
            attempt_controller=attempt_controller,
        )

    metadata = native_result.metadata or {}
    note_id = normalize_xhs_note_id(metadata.get("note_id"))
    source_identity = build_xhs_tiantian_source_identity(note_id)
    request_url = build_xhs_tiantian_request_url(note_id)
    _stage(stage_callback, "tiantianjiexi", provider_attempt=True)
    try:
        payload = provider.parse(
            request_url,
            timeout_seconds=provider_window,
            deadline_monotonic=provider_deadline,
            attempt_controller=attempt_controller,
            source_identity=source_identity,
            payload_validator=lambda value: validate_xhs_tiantian_payload(
                value,
                note_id=note_id,
                native_title=metadata.get("title"),
            ),
        )
        validate_xhs_tiantian_payload(
            payload,
            note_id=note_id,
            native_title=metadata.get("title"),
        )
        selected = select_xhs_tiantian_video_candidate(payload)
        validate_xhs_tiantian_media_url(selected["url"])
        probe = probe_func(
            selected["url"],
            deadline_monotonic=provider_deadline,
        )
        source_key = source_key_for_identity(source_identity)
        asset_id = f"xhs:{source_key}:video:tiantianjiexi"
        provider_cache_key = f"third_party:{PROVIDER_ID}:{source_key}"
        native_asset = native_result.video_assets[0]
        provider_asset = MediaAsset(
            media_type="video",
            url=probe.final_url,
            platform="xiaohongshu",
            cover_url=native_asset.cover_url,
            cover_preview_url=native_asset.cover_preview_url,
            filename=native_asset.filename,
            ext=native_asset.ext,
            quality_label=selected["quality"],
            size_bytes=selected["size"],
            size_is_estimate=False,
            asset_id=asset_id,
            provider=PROVIDER_ID,
            provider_resource_id=source_key,
            provider_cache_key=provider_cache_key,
            provider_manifest_token=build_provider_manifest_token(
                asset_id=asset_id,
                provider=PROVIDER_ID,
                provider_resource_id=source_key,
                provider_cache_key=provider_cache_key,
                url=probe.final_url,
                media_type="video",
            ),
        )
        provider_duration_ms = int((time.monotonic() - started_at) * 1000)
        result_metadata = dict(metadata)
        result_metadata.update(
            {
                "selected_provider": PROVIDER_ID,
                "source_provider": PROVIDER_ID,
                "provider_label": "天天解析",
                "candidate_count": len(payload.get("videos") or ()),
                "xhs_tiantian_hybrid_status": "success",
                "xhs_tiantian_correspondence": "NATIVE_REQUEST_BOUND_ENHANCER",
                "xhs_tiantian_provider_identity_signal": (
                    "EXPLICIT_MATCH"
                    if payload.get("_source_identity_signals")
                    else "REQUEST_BOUND_ONLY"
                ),
                "xhs_tiantian_provider_duration_ms": provider_duration_ms,
                "selected_provider_duration_ms": provider_duration_ms,
                "provider_total_duration_ms": provider_duration_ms,
                "provider_fallback_count": 0,
                "provider_window_started_after_resolution": True,
                "provider_budget_ms": int(provider_window * 1000),
                "xhs_tiantian_external_call": bool(
                    getattr(attempt_controller, "started", False)
                ),
                "xhs_tiantian_cache_reused": bool(
                    getattr(attempt_controller, "reused", False)
                ),
                "xhs_tiantian_bounded_probe_bytes": int(probe.bytes_read),
                "xhs_tiantian_full_server_media_bytes": 0,
            }
        )
        result = ParseResult(
            platform=native_result.platform,
            post_type=native_result.post_type,
            base_filename=native_result.base_filename,
            assets=(provider_asset,),
            metadata=result_metadata,
        )
        if capture_enabled():
            try:
                sources = {"items": PROVIDER_ID}
                if metadata.get("title"):
                    sources["title"] = "xhs_native"
                if native_asset.cover_url:
                    sources["cover"] = "xhs_native"
                if selected["quality"]:
                    sources["quality"] = PROVIDER_ID
                if selected["size"]:
                    sources["size"] = PROVIDER_ID
                capture_legacy_sources(result, sources)
            except Exception:
                capture_error(PROVIDER_ID)
        return result
    except Exception as error:
        reason = _failure_reason(error, attempt_controller)
        logger.info(
            "xhs_tiantian_hybrid_native_fallback",
            extra={
                "reason": reason,
                "provider_started": bool(
                    getattr(attempt_controller, "started", False)
                ),
            },
        )
        _stage(stage_callback, "native", provider_attempt=False)
        return _fallback_result(
            native_result,
            reason=reason,
            attempt_controller=attempt_controller,
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
