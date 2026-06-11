from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .candidates import MediaCandidate, rank_media_candidates
from .quality import QualityProbe


VISIBLE_QUERY_KEYS = {
    "a",
    "br",
    "bt",
    "btag",
    "cquery",
    "height",
    "media_type",
    "mime_type",
    "qs",
    "ratio",
    "video_id",
    "watermark",
    "width",
}


QUALITY_QUERY_KEYS = ("br", "bt", "qs", "btag", "cquery", "dy_q", "feature_id")


def get_url_host(url):
    return (urlparse(url or "").hostname or "").lower()


def redact_url(url):
    parsed = urlparse(url or "")
    redacted_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in VISIBLE_QUERY_KEYS:
            redacted_query.append((key, value))
        else:
            redacted_query.append((key, "<redacted>"))
    return urlunparse(parsed._replace(query=urlencode(redacted_query)))


def get_url_quality_params(url):
    query = dict(parse_qsl(urlparse(url or "").query, keep_blank_values=True))
    return {
        key: query.get(key, "")
        for key in QUALITY_QUERY_KEYS
        if query.get(key, "") != ""
    }


def get_url_path_tail(url, max_length=96):
    path = urlparse(url or "").path
    if len(path) <= max_length:
        return path
    return path[-max_length:]


@dataclass(frozen=True)
class CandidateDiagnostic:
    index: int
    candidate: MediaCandidate
    reachable: bool = False
    probed_size_bytes: int = 0
    final_url: str = ""
    content_type: str = ""
    probe_method: str = ""
    error: str = ""

    @property
    def effective_size_bytes(self):
        return max(self.candidate.size_bytes, self.probed_size_bytes)

    @property
    def final_host(self):
        return get_url_host(self.final_url)

    def to_effective_candidate(self):
        return replace(self.candidate, size_bytes=self.effective_size_bytes)

    def as_dict(self):
        final_url = self.final_url or self.candidate.url
        return {
            "index": self.index,
            "url": self.candidate.url,
            "redacted_url": redact_url(self.candidate.url),
            "host": get_url_host(self.candidate.url),
            "path_tail": get_url_path_tail(self.candidate.url),
            "quality_params": get_url_quality_params(self.candidate.url),
            "source": self.candidate.source,
            "quality_label": self.candidate.quality_label,
            "declared_size_bytes": self.candidate.size_bytes,
            "probed_size_bytes": self.probed_size_bytes,
            "effective_size_bytes": self.effective_size_bytes,
            "width": self.candidate.width,
            "height": self.candidate.height,
            "bit_rate": self.candidate.bit_rate,
            "quality_type": self.candidate.quality_type,
            "ratio_score": self.candidate.ratio_score,
            "reachable": self.reachable,
            "final_url": self.final_url,
            "redacted_final_url": redact_url(self.final_url),
            "final_host": self.final_host,
            "final_path_tail": get_url_path_tail(final_url),
            "final_quality_params": get_url_quality_params(final_url),
            "content_type": self.content_type,
            "probe_method": self.probe_method,
            "error": self.error,
        }


@dataclass(frozen=True)
class CandidateSelection:
    candidate: MediaCandidate | None
    diagnostics: tuple[CandidateDiagnostic, ...]
    reason: str = ""

    @property
    def selected_diagnostic(self):
        if not self.candidate:
            return None
        for diagnostic in self.diagnostics:
            if diagnostic.candidate.url == self.candidate.url:
                return diagnostic
        return None

    def as_dict(self):
        selected = self.selected_diagnostic
        return {
            "selected": selected.as_dict() if selected else None,
            "reason": self.reason,
            "candidates": [item.as_dict() for item in self.diagnostics],
        }


def diagnose_media_candidates(
    candidates,
    headers=None,
    allowed_final_host_suffixes=None,
    timeout=6,
    max_candidates=20,
    should_probe=True,
    quality_probe=None,
    expected_type="",
):
    ranked_candidates = rank_media_candidates(tuple(candidates or ()))[:max_candidates]
    if not should_probe:
        return tuple(
            CandidateDiagnostic(index=index + 1, candidate=candidate)
            for index, candidate in enumerate(ranked_candidates)
        )

    probe = quality_probe or QualityProbe(allowed_final_host_suffixes=allowed_final_host_suffixes)
    diagnostics = []
    for index, candidate in enumerate(ranked_candidates, start=1):
        try:
            result = probe.probe(candidate.url, headers=headers, timeout=timeout, expected_type=expected_type)
        except Exception as error:
            diagnostics.append(
                CandidateDiagnostic(
                    index=index,
                    candidate=candidate,
                    error=str(error),
                )
            )
            continue

        diagnostics.append(
            CandidateDiagnostic(
                index=index,
                candidate=candidate,
                reachable=result.reachable,
                probed_size_bytes=result.size_bytes,
                final_url=result.final_url,
                content_type=result.content_type,
                probe_method=result.method,
                error=result.error,
            )
        )

    return tuple(diagnostics)


def select_best_media_candidate(
    candidates,
    headers=None,
    allowed_final_host_suffixes=None,
    timeout=6,
    max_candidates=20,
    should_probe=True,
    quality_probe=None,
    expected_type="",
):
    diagnostics = diagnose_media_candidates(
        candidates,
        headers=headers,
        allowed_final_host_suffixes=allowed_final_host_suffixes,
        timeout=timeout,
        max_candidates=max_candidates,
        should_probe=should_probe,
        quality_probe=quality_probe,
        expected_type=expected_type,
    )

    reachable_candidates = [
        item.to_effective_candidate()
        for item in diagnostics
        if item.reachable and item.effective_size_bytes
    ]
    if reachable_candidates:
        return CandidateSelection(
            candidate=rank_media_candidates(reachable_candidates)[0],
            diagnostics=diagnostics,
            reason="selected_reachable_probe",
        )

    fallback_candidates = [item.to_effective_candidate() for item in diagnostics]
    if fallback_candidates:
        return CandidateSelection(
            candidate=rank_media_candidates(fallback_candidates)[0],
            diagnostics=diagnostics,
            reason="selected_declared_metadata",
        )

    return CandidateSelection(candidate=None, diagnostics=diagnostics, reason="no_candidates")
