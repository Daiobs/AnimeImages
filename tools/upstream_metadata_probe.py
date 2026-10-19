"""Optional ISO BMFF track evidence; networking is child-only and never relays media."""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import signal
import socket
import threading
import time
from urllib.parse import urlsplit

MAX_BYTES = 128 * 1024
MAX_REQUESTS = 2
MAX_TOTAL_BYTES = MAX_BYTES * MAX_REQUESTS
MAX_SECONDS = 5.0


@dataclass(frozen=True)
class RangeRead:
    start: int
    total_size: int
    data: bytes


class ProbeRejected(Exception):
    pass


def _header(data, pos, end):
    if pos + 8 > end:
        raise ProbeRejected("incomplete_box")
    size = int.from_bytes(data[pos:pos + 4], "big")
    kind = data[pos + 4:pos + 8]
    header = 8
    if size == 1:
        if pos + 16 > end:
            raise ProbeRejected("incomplete_box")
        size = int.from_bytes(data[pos + 8:pos + 16], "big")
        header = 16
    if size < header:
        raise ProbeRejected("invalid_box_size")
    return kind, size, header


def _children(data):
    pos = 0
    count = 0
    while pos < len(data):
        count += 1
        if count > 4096:
            raise ProbeRejected("box_limit")
        kind, size, header = _header(data, pos, len(data))
        if pos + size > len(data):
            raise ProbeRejected("incomplete_box")
        yield kind, data[pos + header:pos + size]
        pos += size


def _tracks(moov):
    handlers = []
    for kind, track in _children(moov):
        if kind != b"trak":
            continue
        media = [body for name, body in _children(track) if name == b"mdia"]
        if len(media) != 1:
            raise ProbeRejected("incomplete_track_table")
        entries = [body for name, body in _children(media[0]) if name == b"hdlr"]
        if len(entries) != 1 or len(entries[0]) < 24 or entries[0][:4] != b"\0\0\0\0":
            raise ProbeRejected("incomplete_track_table")
        handlers.append(entries[0][8:12])
    if not handlers:
        raise ProbeRejected("incomplete_track_table")
    return {"video": "present" if b"vide" in handlers else "absent",
            "audio": "present" if b"soun" in handlers else "absent"}


def probe_iso_bmff_tracks(url, *, guarded_range_reader, deadline_monotonic):
    """Reader must enforce the absolute deadline inside the worker network guard.

    Returns metadata only. Store separately from original codec evidence, e.g.
    format['upstream_bmff_track_evidence']; never overwrite original codec fields.
    """
    result = dict(schema="iso_bmff_tracks_v1", video="unknown", audio="unknown",
                  complete_moov=False, reason="metadata_unavailable", requests=0, bytes_read=0)
    deadline = min(float(deadline_monotonic), time.monotonic() + MAX_SECONDS)
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment):
            raise ProbeRejected("url_rejected")
        offset = 0
        total = None
        while result["requests"] < MAX_REQUESTS:
            if time.monotonic() >= deadline:
                raise ProbeRejected("deadline")
            length = min(MAX_BYTES, MAX_TOTAL_BYTES - result["bytes_read"])
            if total is not None:
                length = min(length, total - offset)
            if length <= 0:
                raise ProbeRejected("moov_not_found")
            result["requests"] += 1
            part = guarded_range_reader(url, start=offset, length=length, deadline_monotonic=deadline)
            result["bytes_read"] += len(part.data)
            if time.monotonic() >= deadline:
                raise ProbeRejected("deadline")
            if (part.start != offset or part.total_size <= offset
                    or len(part.data) != min(length, part.total_size - offset)
                    or len(part.data) > MAX_BYTES
                    or (total is not None and part.total_size != total)):
                raise ProbeRejected("range_rejected")
            total = part.total_size
            pos = 0
            while pos < len(part.data):
                kind, size, header = _header(part.data, pos, len(part.data))
                if offset + pos + size > total:
                    raise ProbeRejected("invalid_box_size")
                if kind == b"moov":
                    if pos + size <= len(part.data):
                        tracks = _tracks(part.data[pos + header:pos + size])
                        result.update(tracks, complete_moov=True, reason="complete_track_table")
                        return result
                    if size > MAX_BYTES:
                        raise ProbeRejected("moov_over_budget")
                    offset += pos
                    break
                if pos + size > len(part.data):
                    # Advance only using a structurally parsed top-level box size.
                    offset += pos + size
                    break
                pos += size
            else:
                offset += pos
        result["reason"] = "request_budget"
    except ProbeRejected as error:
        result["reason"] = str(error)
    except Exception:
        result["reason"] = "request_failed"
    return result


def probe_in_guarded_child(url, *, deadline_monotonic):
    """Call ONLY after worker install_network_guard; hard timer requires child main thread."""
    import urllib.request
    import urllib.error
    try:
        from tools.upstream_match_worker import validate_url
    except ModuleNotFoundError as error:
        if error.name not in {'tools', 'tools.upstream_match_worker'}:
            raise
        spec = importlib.util.spec_from_file_location(
            'upstream_match_worker', Path(__file__).resolve().with_name('upstream_match_worker.py'))
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        validate_url = worker.validate_url

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("probe_requires_child_main_thread")
    if not getattr(socket.create_connection, "__qualname__", "").startswith("install_network_guard.<locals>."):
        raise RuntimeError("probe_requires_installed_child_guard")
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError("probe_requires_unoccupied_alarm")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    bytes_left = MAX_TOTAL_BYTES
    expected_total = None

    def read(url, *, start, length, deadline_monotonic):
        nonlocal bytes_left, expected_total
        if start < 0 or not 0 < length <= min(MAX_BYTES, bytes_left):
            raise ProbeRejected("range_rejected")
        validate_url(url)
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise ProbeRejected("deadline")
        request = urllib.request.Request(url, headers={
            "Range": f"bytes={start}-{start + length - 1}", "Accept-Encoding": "identity"})
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as error:
            error.close()
            raise ProbeRejected("range_rejected") from None
        with response:
            match = re.fullmatch(r"bytes ([0-9]{1,20})-([0-9]{1,20})/([0-9]{1,20})",
                                 response.headers.get("Content-Range", ""))
            if response.status != 206 or not match or response.geturl() != url:
                raise ProbeRejected("range_rejected")
            first, last, total = map(int, match.groups())
            size = last - first + 1
            content_length = response.headers.get("Content-Length")
            if (not 0 <= first <= last < total or first != start
                    or last != min(start + length, total) - 1
                    or not 0 < size <= min(length, bytes_left)
                    or (expected_total is not None and total != expected_total)
                    or (content_length is not None and (
                        not re.fullmatch(r"[0-9]{1,20}", content_length) or int(content_length) != size))
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise ProbeRejected("range_rejected")
            if time.monotonic() >= deadline_monotonic:
                raise ProbeRejected("deadline")
            # Validate and reserve the positive body budget before stdlib read():
            # HTTPResponse treats a negative amount as an unbounded read.
            bytes_left -= size
            expected_total = total
            return RangeRead(first, total, response.read(size))

    def expired(signum, frame):
        raise ProbeRejected("deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    remaining = min(MAX_SECONDS, float(deadline_monotonic) - time.monotonic())
    if remaining <= 0:
        signal.signal(signal.SIGALRM, previous)
        return probe_iso_bmff_tracks(url, guarded_range_reader=read, deadline_monotonic=deadline_monotonic)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        return probe_iso_bmff_tracks(url, guarded_range_reader=read, deadline_monotonic=deadline_monotonic)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
