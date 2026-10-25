import logging

from .middleware import get_current_request_id


PROVIDER_TERMINAL_EVENT = "provider_attempt_completed"
PROVIDER_TERMINAL_LOG_FIELDS = (
    "outcome",
    "external_call",
    "cache_hit",
    "cache_bypassed",
    "single_flight_wait",
    "continuation",
    "attempt_consumed",
    "elapsed_ms",
    "payload_age_ms",
    "retry_after_seconds",
    "candidate_count",
    "media_host",
    "expected_bytes",
    "failure_class",
)

XHS_PREVIEW_EVENT_LOG_FIELDS = {
    "xhs_preview_candidate_failed": (
        "surface",
        "source_ordinal",
        "candidate_class",
        "failure_code",
    ),
    "xhs_preview_fallback_succeeded": (
        "surface",
        "source_ordinal",
        "candidate_class",
        "attempt_count",
    ),
    "xhs_preview_candidates_exhausted": (
        "surface",
        "source_ordinal",
        "candidate_count",
        "attempted_count",
    ),
}

DOWNLOAD_STREAM_LOG_FIELDS = (
    "final_status", "expected_bytes", "actual_yielded_bytes", "chunk_index",
    "stream_elapsed_ms", "time_to_first_chunk_ms", "upstream_attempt_count",
    "exception_type", "generator_close_reason", "upstream_response_closed", "error_code",
)


class RequestContextFormatter(logging.Formatter):
    default_extra = {
        "request_id": "",
        "event": "",
        "provider": "",
        "status": "",
        "duration_ms": "",
        "source_platform": "",
        "error_type": "",
    }

    def format(self, record):
        for key, value in self.default_extra.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        if not record.request_id:
            record.request_id = get_current_request_id()
        if not record.event:
            record.event = record.getMessage()
        rendered = super().format(record)
        if record.event == PROVIDER_TERMINAL_EVENT:
            provider_fields = " ".join(
                f"{field}={getattr(record, field, '')}"
                for field in PROVIDER_TERMINAL_LOG_FIELDS
            )
            return f"{rendered} {provider_fields}"
        preview_fields = XHS_PREVIEW_EVENT_LOG_FIELDS.get(record.event)
        if record.event in {"download_stream_streaming", "download_stream_completed", "download_stream_failed"}:
            preview_fields = DOWNLOAD_STREAM_LOG_FIELDS
        if preview_fields:
            rendered_fields = " ".join(
                f"{field}={getattr(record, field, '')}"
                for field in preview_fields
            )
            return f"{rendered} {rendered_fields}"
        return rendered
