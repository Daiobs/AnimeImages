"""Bounded structured progress for existing external-tool download callers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


YTDLP_PROGRESS_PREFIX = "__ANIMEIMAGES_YTDLP_PROGRESS_V1__"
YTDLP_PROGRESS_TEMPLATE = (
    f"download:{YTDLP_PROGRESS_PREFIX}"
    "%(progress.status)s\t"
    "%(progress.downloaded_bytes)s\t"
    "%(progress.total_bytes)s\t"
    "%(progress.total_bytes_estimate)s\t"
    "%(progress.fragment_index)s\t"
    "%(progress.fragment_count)s"
)

MAX_PROGRESS_LINE_CHARS = 2048
MAX_PROGRESS_BYTES = 8 * 1024 * 1024 * 1024 * 1024
MAX_PROGRESS_FRAGMENTS = 1_000_000
STRUCTURED_PROGRESS_COMPLETION_RATIO = 0.95
INITIAL_PROGRESS_RATIO = 0.02
FINAL_PROGRESS_RATIO = 0.98


@dataclass(frozen=True)
class YtDlpProgressEvent:
    status: str
    downloaded_bytes: int | None
    total_bytes: int | None
    total_bytes_estimate: int | None
    fragment_index: int | None
    fragment_count: int | None


def _bounded_integer(value: str, *, maximum: int) -> int | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"na", "none", "null"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    integer = int(number)
    return integer if integer <= maximum else None


def parse_ytdlp_progress_line(line: str) -> YtDlpProgressEvent | None:
    text = str(line or "").strip(" \t\r\n")
    if len(text) > MAX_PROGRESS_LINE_CHARS:
        return None
    if not text.startswith(YTDLP_PROGRESS_PREFIX):
        return None
    payload = text[len(YTDLP_PROGRESS_PREFIX) :]
    parts = payload.split("\t")
    if len(parts) != 6 or parts[0] not in {"downloading", "finished"}:
        return None
    return YtDlpProgressEvent(
        status=parts[0],
        downloaded_bytes=_bounded_integer(parts[1], maximum=MAX_PROGRESS_BYTES),
        total_bytes=_bounded_integer(parts[2], maximum=MAX_PROGRESS_BYTES),
        total_bytes_estimate=_bounded_integer(parts[3], maximum=MAX_PROGRESS_BYTES),
        fragment_index=_bounded_integer(parts[4], maximum=MAX_PROGRESS_FRAGMENTS),
        fragment_count=_bounded_integer(parts[5], maximum=MAX_PROGRESS_FRAGMENTS),
    )


def ytdlp_progress_arguments() -> tuple[str, ...]:
    return ("--newline", "--progress-template", YTDLP_PROGRESS_TEMPLATE)


def with_ytdlp_progress_arguments(args) -> list[str]:
    result = list(args or ())
    if not result or any(
        item == "--progress-template"
        or str(item).startswith("--progress-template=")
        for item in result
    ):
        return result
    result[-1:-1] = ytdlp_progress_arguments()
    return result


def normalized_ytdlp_progress(
    event: YtDlpProgressEvent,
    *,
    estimated_size: int = 0,
) -> tuple[int, int] | None:
    estimated_size = max(0, min(int(estimated_size or 0), MAX_PROGRESS_BYTES))
    event_total = event.total_bytes or event.total_bytes_estimate or 0
    target_total = estimated_size or event_total
    if target_total <= 0:
        return None

    if event.status == "finished":
        return target_total, target_total

    if event.downloaded_bytes is not None:
        if event_total > 0 and estimated_size > 0:
            fraction = min(event.downloaded_bytes / event_total, 1.0)
            return int(target_total * fraction), target_total
        return min(event.downloaded_bytes, target_total), target_total

    if (
        event.fragment_index is not None
        and event.fragment_count
        and event.fragment_index <= event.fragment_count
    ):
        fraction = event.fragment_index / event.fragment_count
        return int(target_total * fraction), target_total
    return None


class YtDlpProgressTracker:
    """Monotonic adapter for the existing ``callback(completed, total)`` API."""

    def __init__(
        self,
        callback: Callable[[int, int], None],
        *,
        estimated_size: int = 0,
    ):
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        self.callback = callback
        self.estimated_size = max(
            0,
            min(int(estimated_size or 0), MAX_PROGRESS_BYTES),
        )
        self.completed = 0
        self.total = self.estimated_size
        self.structured_seen = False

    def emit_initial(self) -> None:
        if self.estimated_size:
            self._emit(
                max(1, int(self.estimated_size * INITIAL_PROGRESS_RATIO)),
                self.estimated_size,
            )

    def consume_line(self, line: str) -> None:
        event = parse_ytdlp_progress_line(line)
        if event is None:
            return
        normalized = normalized_ytdlp_progress(
            event,
            estimated_size=self.estimated_size,
        )
        if normalized is None:
            return
        self.structured_seen = True
        completed, total = normalized
        cap = max(1, int(total * STRUCTURED_PROGRESS_COMPLETION_RATIO))
        self._emit(min(completed, cap), total)

    def emit_directory_fallback(self, completed: int, total: int) -> None:
        if self.structured_seen:
            return
        total = self.estimated_size or max(0, int(total or 0))
        if total <= 0:
            return
        cap = max(1, int(total * STRUCTURED_PROGRESS_COMPLETION_RATIO))
        self._emit(min(max(0, int(completed or 0)), cap), total)

    def emit_finished(self, output_size: int) -> None:
        output_size = max(0, min(int(output_size or 0), MAX_PROGRESS_BYTES))
        total = max(self.estimated_size, output_size)
        if total <= 0:
            return
        completed = max(output_size, int(total * FINAL_PROGRESS_RATIO))
        self._emit(min(completed, total), total)

    def _emit(self, completed: int, total: int) -> None:
        total = max(self.total, int(total or 0))
        completed = max(self.completed, min(int(completed or 0), total))
        if total <= 0 or (completed == self.completed and total == self.total):
            return
        self.completed = completed
        self.total = total
        self.callback(completed, total)
