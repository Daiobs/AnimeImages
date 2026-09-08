"""Opt-in product ParseHub bridge. Integration owns admission and delivery.

Set PARSEHUB_UPSTREAM_ENABLED and PARSEHUB_UPSTREAM_PYTHON independently of XHS.
parse_upstream_url returns gallery-style [3, url, metadata] messages. Videos carry
media_completeness=unknown and MUST NOT be admitted as complete without evidence.
"""

import json
from pathlib import Path
import re
import subprocess

from tools.upstream_parsehub_worker import (
    MAX_BYTES, PARSERS, PROVIDER_ID, PROVIDER_VERSION, match_url, timeout_value,
)
from .platforms.external_tools import run_external_command, ExternalCommandOutputTooLarge
from .env_config import runtime_bool, runtime_str

WORKER = Path(__file__).resolve().parents[2] / "tools/upstream_parsehub_worker.py"


class ParseHubBridgeError(RuntimeError):
    def __init__(self, code, stage="parent", error_class=""):
        super().__init__(code)
        self.code, self.stage, self.error_class = code, stage, error_class


def _call(request, timeout):
    if not runtime_bool("PARSEHUB_UPSTREAM_ENABLED", False):
        raise ParseHubBridgeError("disabled")
    python = Path(runtime_str("PARSEHUB_UPSTREAM_PYTHON"))
    if not python.is_absolute() or not python.is_file():
        raise ParseHubBridgeError("unavailable")
    try:
        result = run_external_command(
            [str(python), "-I", "-B", str(WORKER)], input_text=json.dumps(request),
            timeout_seconds=timeout, max_stdout_bytes=MAX_BYTES, max_stderr_bytes=65536)
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ParseHubBridgeError("worker_timeout") from None
    except ExternalCommandOutputTooLarge:
        raise ParseHubBridgeError("output_limit") from None
    except (OSError, ValueError):
        raise ParseHubBridgeError("worker_failed") from None
    if not isinstance(data, dict) or result.returncode or data.get("ok") is not True:
        allowed = {"version_changed", "unavailable", "network_timeout", "network_denied",
                   "execution_guard", "extractor_mismatch", "identity_mismatch", "no_formats",
                   "output_limit", "request_limit", "insecure_media", "unsupported_result", "media_type_conflict",
                   "auth_required", "access_denied", "tls_error", "upstream_unavailable"}
        code = data.get("error") if isinstance(data, dict) else None
        stage = data.get("stage") if isinstance(data, dict) else None
        error_class = data.get("error_class", "") if isinstance(data, dict) else ""
        if not isinstance(error_class, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_class):
            error_class = ""
        raise ParseHubBridgeError(code if code in allowed else "worker_failed",
                                  stage if stage in {"request", "import", "parse"} else "worker",
                                  error_class)
    return data.get("payload")


def match_upstream_url(url):
    matches = match_url(url)
    if not matches:
        return ()
    try:
        identity = _call({"action": "identity"}, 5)
        if identity != {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION}:
            return ()
    except ParseHubBridgeError:
        return ()
    return matches


def coverage_snapshot():
    """Return reviewed bridge entries only after the exact runtime handshake.

    Counts describe this bridge's catalog, not ParseHub's full platform inventory
    or successful live parses. The caller may append this dict to its snapshots.
    """
    available = False
    try:
        available = _call({"action": "identity"}, 5) == {
            "provider_id": PROVIDER_ID, "version": PROVIDER_VERSION}
    except ParseHubBridgeError:
        pass
    entries = [
        {"provider_id": PROVIDER_ID, "extractor_id": extractor,
         "label": label, "version": PROVIDER_VERSION}
        for extractor, (_, label) in PARSERS.items()
    ] if available else []
    return {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION if available else "",
            "available": available, "entries": entries,
            "counts": {"total": len(entries), "eligible": len(entries),
                       "excluded": 0, "already_routed": 0, "other_excluded": 0}}


def parse_upstream_url(match, url, timeout_seconds=15.0):
    try:
        timeout = timeout_value(timeout_seconds)
        if (match,) != match_url(url):
            raise ValueError()
    except (ValueError, TypeError):
        raise ParseHubBridgeError("invalid_parse_request") from None
    payload = _call({"action": "parse", "url": url, "match": match,
                     "timeout_seconds": timeout}, timeout)
    if not isinstance(payload, list) or not payload or len(payload) > 30:
        raise ParseHubBridgeError("invalid_payload")
    for row in payload:
        if (not isinstance(row, list) or len(row) != 3 or row[0] != 3 or
                not isinstance(row[1], str) or not isinstance(row[2], dict) or
                row[2].get("media_completeness") not in {"unknown", "image_candidate"}):
            raise ParseHubBridgeError("invalid_payload")
    return payload
