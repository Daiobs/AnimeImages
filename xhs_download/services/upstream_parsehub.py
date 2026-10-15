"""Opt-in isolated ParseHub registry bridge, shared by Lab and normal parsing.

URL messages [3, url, metadata] preserve candidates independently of delivery.
Metadata-only messages [2, metadata] preserve sanitized richtext summaries.
Completeness is upstream-declared; operation restrictions never mean parse failure.
No ParseHub package imports or matching occur in the application process.
"""

import json
from pathlib import Path
import re
import subprocess

from tools.upstream_parsehub_worker import (
    COMPLETENESS, MAX_ASSETS, MAX_BYTES, PROVIDER_ID, PROVIDER_VERSION, timeout_value, validate_url,
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
                   "auth_required", "access_denied", "tls_error", "upstream_unavailable",
                   "delegation_limit", "redirect_limit", "scope_limited", "invalid_url",
                   "runtime_import_error"}
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
    try:
        validate_url(url)
        matches = _call({"action": "match", "url": url}, 10)
        if not isinstance(matches, list) or len(matches) > 100 or not all(valid_match(m) for m in matches):
            return ()
    except (ParseHubBridgeError, ValueError, TypeError):
        return ()
    return tuple(matches)


def valid_match(match):
    return (isinstance(match, dict) and set(match) == {"provider_id", "extractor_id", "label", "version"}
            and match.get("provider_id") == PROVIDER_ID and match.get("version") == PROVIDER_VERSION
            and isinstance(match.get("extractor_id"), str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", match["extractor_id"]) is not None
            and isinstance(match.get("label"), str) and len(match["label"]) <= 200)


def coverage_snapshot():
    """The fixed runtime's actual registry, not a per-site application allowlist."""
    available = False
    entries = []
    try:
        catalog = _call({"action": "catalog"}, 10)
        available = (isinstance(catalog, dict) and catalog.get("provider_id") == PROVIDER_ID
                     and catalog.get("version") == PROVIDER_VERSION
                     and isinstance(catalog.get("entries"), list) and len(catalog["entries"]) <= 100
                     and all(valid_match(m) for m in catalog["entries"]))
        entries = catalog["entries"] if available else []
    except ParseHubBridgeError:
        pass
    return {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION if available else "",
            "available": available, "entries": entries,
            "counts": {"total": len(entries), "eligible": len(entries),
                       "excluded": 0, "already_routed": 0, "other_excluded": 0}}


def parse_upstream_url(match, url, timeout_seconds=15.0):
    try:
        timeout = timeout_value(timeout_seconds)
        validate_url(url)
        if not valid_match(match):
            raise ValueError()
    except (ValueError, TypeError):
        raise ParseHubBridgeError("invalid_parse_request") from None
    payload = _call({"action": "parse", "url": url, "match": match,
                     "timeout_seconds": timeout}, timeout)
    if not isinstance(payload, list) or not payload or len(payload) > MAX_ASSETS:
        raise ParseHubBridgeError("invalid_payload")
    for row in payload:
        if not isinstance(row, list) or len(row) not in {2, 3}:
            raise ParseHubBridgeError("invalid_payload")
        if len(row) == 3 and row[0] == 3 and isinstance(row[1], str):
            try:
                validate_url(row[1])
            except (ValueError, TypeError):
                raise ParseHubBridgeError("invalid_payload") from None
            meta = row[2]
        elif len(row) == 2 and row[0] == 2:
            meta = row[1]
            if not isinstance(meta, dict) or meta.get("candidate_kind") != "richtext" or not meta.get("content_summary"):
                raise ParseHubBridgeError("invalid_payload")
        else:
            raise ParseHubBridgeError("invalid_payload")
        if not isinstance(meta, dict) or meta.get("media_completeness") not in COMPLETENESS:
            raise ParseHubBridgeError("invalid_payload")
    return payload
