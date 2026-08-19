from dataclasses import dataclass

from .xhs_page_state import XHS_TERMINAL_PAGE_STATES, XhsPageState


@dataclass(frozen=True)
class XhsFallbackPolicy:
    name: str
    page_state: str
    allow_meta_images: bool
    allow_dom_images: bool
    allow_broad_image_scan: bool
    allow_broad_video_scan: bool
    structured_image_authoritative: bool
    source_identity_verified: bool = False
    strict_current_note_dom: bool = False
    terminal_state: str = ""
    terminal_marker_detected: bool = False


def _is_terminal(value):
    return value if value in XHS_TERMINAL_PAGE_STATES else ""


def _has_verified_current_note_media(
    state,
    *,
    structured_image_urls,
    structured_video_urls,
):
    if not state.current_note_found:
        return False
    if state.post_type == "video":
        return bool(structured_video_urls)
    if state.post_type == "image":
        return bool(structured_image_urls)
    return bool(structured_image_urls or structured_video_urls)


def resolve_xhs_fallback_policy(
    state: XhsPageState,
    *,
    structured_image_urls,
    structured_video_urls,
    dom_page_state="unknown",
    page_identity_source="",
):
    current_note_terminal = _is_terminal(state.current_note_page_state)
    generic_terminal = _is_terminal(state.explicit_page_state) or _is_terminal(
        dom_page_state
    )
    verified_current_media = _has_verified_current_note_media(
        state,
        structured_image_urls=structured_image_urls,
        structured_video_urls=structured_video_urls,
    )

    terminal_state = current_note_terminal
    if not terminal_state and generic_terminal and not verified_current_media:
        terminal_state = generic_terminal
    if terminal_state:
        return XhsFallbackPolicy(
            name="terminal",
            page_state=terminal_state,
            allow_meta_images=False,
            allow_dom_images=False,
            allow_broad_image_scan=False,
            allow_broad_video_scan=False,
            structured_image_authoritative=False,
            terminal_state=terminal_state,
            terminal_marker_detected=True,
        )

    structured_image_authoritative = bool(
        state.current_note_found
        and state.post_type != "video"
        and structured_image_urls
    )
    structured_video_authoritative = bool(
        state.current_note_found
        and state.post_type != "image"
        and structured_video_urls
    )
    page_identity_verified = bool(page_identity_source)
    source_identity_mismatch = bool(
        state.source_note_id
        and state.note_candidate_found
        and not state.current_note_found
    )
    source_identity_unverified = bool(
        state.source_note_id
        and not state.current_note_found
        and not page_identity_verified
        and not source_identity_mismatch
    )
    ignored_generic_terminal = bool(generic_terminal and verified_current_media)
    suppress_broad_scan = bool(
        (state.note_candidate_found and not state.current_note_found)
        or state.other_note_candidate_found
    )

    if structured_image_authoritative or structured_video_authoritative:
        policy_name = "current_note_authoritative"
    elif source_identity_mismatch:
        policy_name = "source_identity_mismatch"
    elif source_identity_unverified:
        policy_name = "source_identity_unverified"
    elif page_identity_verified and not state.current_note_found:
        policy_name = "verified_page_fallback"
    elif suppress_broad_scan:
        policy_name = "suppress_broad_scan"
    else:
        policy_name = "allow"

    if state.current_note_found or page_identity_verified:
        page_state = "available"
    elif state.page_state != "unknown":
        page_state = state.page_state
    elif dom_page_state != "unknown":
        page_state = dom_page_state
    else:
        page_state = "unknown"

    return XhsFallbackPolicy(
        name=policy_name,
        page_state=page_state,
        allow_meta_images=(
            not structured_image_authoritative
            and not ignored_generic_terminal
            and not source_identity_mismatch
            and not source_identity_unverified
        ),
        allow_dom_images=(
            not structured_image_authoritative
            and not ignored_generic_terminal
            and not source_identity_mismatch
            and not source_identity_unverified
        ),
        allow_broad_image_scan=(
            not structured_image_authoritative
            and not suppress_broad_scan
            and not ignored_generic_terminal
            and not source_identity_mismatch
            and not source_identity_unverified
            and not (page_identity_verified and not state.current_note_found)
        ),
        allow_broad_video_scan=(
            not structured_video_authoritative
            and state.post_type != "image"
            and not suppress_broad_scan
            and not ignored_generic_terminal
            and not source_identity_mismatch
            and not source_identity_unverified
            and not (page_identity_verified and not state.current_note_found)
        ),
        structured_image_authoritative=structured_image_authoritative,
        source_identity_verified=bool(
            state.current_note_found
            or (page_identity_verified and not source_identity_mismatch)
        ),
        strict_current_note_dom=policy_name in {
            "suppress_broad_scan",
            "verified_page_fallback",
        },
        terminal_marker_detected=bool(current_note_terminal or generic_terminal),
    )
