"""Compatibility exports for the former Staff-only LivePhoto builder."""

from .xhs_livephoto_builder import (
    XHS_LIVEPHOTO_STAFF_CANDIDATE_TIMEOUT_SECONDS,
    XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES,
    XHS_LIVEPHOTO_STAFF_MAX_PROBE_WORKERS,
    XHS_LIVEPHOTO_STAFF_PROVIDER,
    XHS_LIVEPHOTO_STAFF_TOTAL_TIMEOUT_SECONDS,
    XhsLivePhotoProbeEvidenceV1,
    XhsLivePhotoStaffBuildOutcome,
    XhsLivePhotoStaffPreviewError,
    build_xhs_livephoto_preview_context,
    build_xhs_livephoto_staff_result as _build_xhs_livephoto_staff_result,
    probe_xhs_livephoto_candidates,
    requests,
)


def build_xhs_livephoto_staff_result(**kwargs):
    return _build_xhs_livephoto_staff_result(**kwargs)


def build_xhs_livephoto_staff_preview_context(result):
    return build_xhs_livephoto_preview_context(result)


__all__ = (
    "XHS_LIVEPHOTO_STAFF_CANDIDATE_TIMEOUT_SECONDS",
    "XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES",
    "XHS_LIVEPHOTO_STAFF_MAX_PROBE_WORKERS",
    "XHS_LIVEPHOTO_STAFF_PROVIDER",
    "XHS_LIVEPHOTO_STAFF_TOTAL_TIMEOUT_SECONDS",
    "XhsLivePhotoProbeEvidenceV1",
    "XhsLivePhotoStaffBuildOutcome",
    "XhsLivePhotoStaffPreviewError",
    "build_xhs_livephoto_staff_preview_context",
    "build_xhs_livephoto_staff_result",
    "probe_xhs_livephoto_candidates",
)
