"""Persistent, non-authorizing Lab summaries. Import never executes a provider."""
from dataclasses import fields
from hashlib import sha256
import ipaddress
import json
import math
import re
import subprocess
from urllib.parse import urlsplit, parse_qsl
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from xhs_download.models import EvaluationBatch, EvaluationExecution
from .contracts import LabAssetV1, MAX_RETAINED_CANDIDATES, safe_public_text

MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_EXECUTIONS = 200
PRIVATE = re.compile(r"cookie|authorization|token|secret|password|passwd|api.?key|signature|xsec|credential|session", re.I)
UNSAFE_FIELD = re.compile(r"url|uri|href|src|header|raw|path|payload", re.I)
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
ASSET_FIELDS = {f.name for f in fields(LabAssetV1)} - {"resource_token"}
RESULT_FIELDS = {"schema_version", "request_id", "provider_id", "provider_version", "provider_scope",
                 "claimed_platform", "selected_backend", "relationships", "warnings", "license_name", "license_scope",
                 "execution_provider_id", "backend_id", "delegated_provider_id",
                 "status", "error_code",
                 "error_detail", "elapsed_ms", "executed", "result_kind", "metadata", "attempts",
                 "queue_wait_ms", "execution_ms", "candidate_count", "upstream_retention", "retention",
                 "actual_probe", "preview_and_save", "limitations", "cache_hit", "cache_status",
                 "saved_candidate_count", "request_attempted", "request_reserved", "request_returned",
                 "http_status", "reason", "unexecuted_reason", "client_status"}


class HistoryError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def identity(value, limit=200):
    if not isinstance(value, str) or len(value) > limit or not IDENTITY.fullmatch(value):
        raise HistoryError("HISTORY_IDENTITY_INVALID")
    return value


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def safe_value(value, depth=0):
    if depth > 6:
        raise HistoryError("HISTORY_TOO_DEEP")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if re.search(r"xkeysib-|xsmtpsib-|Bearer\s|BEGIN.*PRIVATE KEY", value, re.I):
            return "[redacted]"
        return safe_public_text(value, 512)
    if isinstance(value, list):
        if len(value) > 1000:
            raise HistoryError("HISTORY_LIST_TOO_LARGE")
        return [safe_value(v, depth + 1) for v in value]
    if isinstance(value, dict):
        if len(value) > 128:
            raise HistoryError("HISTORY_OBJECT_TOO_LARGE")
        return {k: safe_value(v, depth + 1) for k, v in value.items()
                if isinstance(k, str) and len(k) <= 100 and not PRIVATE.search(k) and not UNSAFE_FIELD.search(k)}
    raise HistoryError("HISTORY_VALUE_INVALID")


def source_info(value):
    if not value:
        return "", "", "MISSING"
    if not isinstance(value, str) or len(value) > 4096:
        raise HistoryError("HISTORY_SOURCE_INVALID")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                or parsed.fragment or any(PRIVATE.search(k) for k, _ in parse_qsl(parsed.query))):
            return "", "", "UNSAFE_OR_SENSITIVE_SOURCE_NOT_RETAINED"
        if host == "localhost" or host.endswith((".localhost", ".local")):
            return "", "", "NON_PUBLIC_SOURCE_NOT_RETAINED"
        try:
            if not ipaddress.ip_address(host).is_global:
                return "", "", "NON_PUBLIC_SOURCE_NOT_RETAINED"
        except ValueError:
            pass
        if any(c.isspace() for c in value) or "\\" in value or "<" in value or ">" in value:
            return "", "", "UNSAFE_SOURCE_NOT_RETAINED"
        return value, host.lower().rstrip(".").encode("idna").decode("ascii"), "RETAINED"
    except (ValueError, UnicodeError):
        raise HistoryError("HISTORY_SOURCE_INVALID") from None


def result_summary(value):
    if not isinstance(value, dict):
        raise HistoryError("HISTORY_RESULT_INVALID")
    for key in ("status", "request_id", "provider_id", "client_status", "error_code", "error_detail"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise HistoryError("HISTORY_RESULT_INVALID")
    output = {k: safe_value(v) for k, v in value.items() if k in RESULT_FIELDS}
    assets = value.get("assets", value.get("candidates", []))
    if not isinstance(assets, list) or len(assets) > MAX_RETAINED_CANDIDATES:
        raise HistoryError("HISTORY_CANDIDATE_LIMIT")
    if any(not isinstance(a, dict) for a in assets):
        raise HistoryError("HISTORY_CANDIDATE_INVALID")
    # Keep absent fields absent: old summaries cannot acquire new track facts.
    if "assets" in value or "candidates" in value:
        output["assets"] = [{k: safe_value(v) for k, v in a.items() if k in ASSET_FIELDS} for a in assets]
    output["historical"] = True
    return output


def execution_summary(row):
    if not isinstance(row, dict):
        raise HistoryError("HISTORY_EXECUTION_INVALID")
    for key in ("replay_of", "replay_batch_key"):
        if key in row and not isinstance(row[key], str):
            raise HistoryError("HISTORY_EXECUTION_INVALID")
    source, host, source_status = source_info(row.get("source_url"))
    return {
        "execution_key": identity(row.get("execution_key")),
        "sample_key": identity(row.get("sample_key")),
        "source_url": source, "source_host": host, "source_status": source_status,
        "source_state": "available" if source else safe_value(row.get("source_state", "missing")),
        "target_id": safe_value(row.get("target_id", "")),
        "title": safe_public_text(row.get("title", ""), 300),
        "provider_id": identity(row.get("provider_id"), 80),
        "state": identity(row.get("state", "unknown"), 40),
        "started_at": safe_value(row.get("started_at")), "finished_at": safe_value(row.get("finished_at")),
        "version": safe_value(row.get("version", {})), "options": safe_value(row.get("options")),
        "replay_of": safe_value(row.get("replay_of", "")),
        "replay_batch_key": safe_value(row.get("replay_batch_key", "")),
        "result": result_summary(row.get("result", {})),
    }


@transaction.atomic
def import_batch(payload):
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1):
        raise HistoryError("HISTORY_SCHEMA_INVALID")
    batch = payload.get("batch")
    rows = payload.get("executions")
    if not isinstance(batch, dict) or not isinstance(rows, list) or len(rows) > MAX_EXECUTIONS:
        raise HistoryError("HISTORY_BATCH_INVALID")
    key = identity(batch.get("batch_key"), 150)
    metadata = safe_value(batch.get("metadata", {}))
    if not isinstance(metadata, dict):
        raise HistoryError("HISTORY_METADATA_INVALID")
    label = safe_public_text(batch.get("label", key), 200)
    obj, created_batch = EvaluationBatch.objects.get_or_create(
        batch_key="scout:" + key, defaults={"label": label, "origin": "scout", "metadata": metadata})
    if not created_batch and (obj.metadata != metadata or obj.label != label):
        raise HistoryError("HISTORY_BATCH_CONFLICT", 409)
    created = unchanged = 0
    for raw in rows:
        summary = execution_summary(raw)
        fingerprint = digest(summary)
        record, created_record = EvaluationExecution.objects.get_or_create(
            batch=obj, execution_key=summary["execution_key"],
            defaults={"summary": summary, "fingerprint": fingerprint})
        if not created_record and record.fingerprint != fingerprint:
            raise HistoryError("HISTORY_EXECUTION_CONFLICT", 409)
        if created_record:
            # Association never changes imported provenance or grants authority.
            request_id = summary["result"].get("request_id")
            if request_id:
                related = EvaluationExecution.objects.filter(batch__origin="lab",
                    summary__result__request_id=request_id,
                    summary__source_url=summary["source_url"],
                    summary__provider_id=summary["provider_id"]).first()
                if related:
                    record.application_record = related
                    record.save(update_fields=["application_record"])
            created += 1
        else:
            unchanged += 1
    return {"ok": True, "batch_id": obj.pk, "created": created, "unchanged": unchanged}


def current_version():
    try:
        return {"animeimages_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=settings.BASE_DIR, timeout=2, stderr=subprocess.DEVNULL).decode().strip(),
                "tracked_changes": subprocess.run(["git", "diff", "--quiet", "HEAD"],
                    cwd=settings.BASE_DIR, timeout=2, stderr=subprocess.DEVNULL).returncode != 0}
    except (OSError, subprocess.SubprocessError):
        return {"animeimages_head": None}


def begin_execution(source, provider_id, options, replay_of=None):
    now = timezone.now()
    batch, _ = EvaluationBatch.objects.get_or_create(batch_key="lab:" + now.date().isoformat(),
        defaults={"label": "Lab " + now.date().isoformat(), "origin": "lab"})
    key = uuid4().hex
    summary = execution_summary({"execution_key": key,
        "sample_key": sha256(source.encode()).hexdigest(), "source_url": source, "provider_id": provider_id,
        "state": "unknown", "started_at": now.isoformat(), "version": current_version(),
        "options": options, "replay_of": str(replay_of or ""), "result": {}})
    return EvaluationExecution.objects.create(batch=batch, execution_key=key, summary=summary, fingerprint=digest(summary))


def finish_execution(record, result):
    summary = {**record.summary, "state": "returned", "finished_at": timezone.now().isoformat(),
               "result": result_summary(result)}
    record.summary, record.fingerprint = summary, digest(summary)
    record.save(update_fields=["summary", "fingerprint"])


def retest_allowed(record):
    from .providers import PROVIDER_REGISTRY
    s = record.summary
    if not isinstance(s, dict):
        return False
    state = s.get("state")
    if not isinstance(state, str):
        return False
    # Only known terminal or explicitly unexecuted records authorize a new attempt.
    if state.strip().lower() not in {
        "returned", "success", "failed", "timeout", "unsupported", "not_executed",
        "planned", "skipped", "cancelled", "canceled", "not_configured", "not_authorized",
    }:
        return False
    result = s.get("result", {})
    if not isinstance(result, dict):
        return False
    return bool(s.get("source_url") and s.get("provider_id") in PROVIDER_REGISTRY
                and result.get("error_detail") != "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"
                and result.get("client_status") != "OUTCOME_UNKNOWN")


def execution_dict(record):
    return {**record.summary, "id": record.pk, "origin": record.batch.origin,
            "application_record_id": record.application_record_id, "can_retest": retest_allowed(record)}


def batch_dict(batch):
    rows = list(batch.executions.values_list("summary", flat=True))
    counts = {"planned": 0, "executed": 0, "success": 0, "unsupported": 0,
              "failed": 0, "not_executed": 0, "unknown": 0}
    superseded = {r.get("replay_of") for r in rows if not r.get("replay_batch_key")
                  and isinstance(r.get("replay_of"), str)}
    for row in rows:
        if row.get("execution_key") in superseded:
            continue
        state, result = row.get("state"), row.get("result", {})
        if state in {"unknown", "sent", "reserved", "in_flight", "running"}:
            counts["unknown"] += 1
        elif state == "planned":
            counts["planned"] += 1
        elif state in {"not_executed", "skipped"}:
            counts["not_executed"] += 1
        elif result.get("executed") is False:
            counts["not_executed"] += 1
        if result.get("executed") is True:
            counts["executed"] += 1
        status = result.get("status")
        if not isinstance(status, str):
            continue
        if status in {"success", "unsupported"}:
            counts[status] += 1
        elif status in {"failed", "timeout"}:
            counts["failed"] += 1
    return {"id": batch.pk, "batch_key": batch.batch_key, "label": batch.label, "origin": batch.origin,
            "sync_status": "stored",
            "metadata": batch.metadata, "created_at": batch.created_at.isoformat(), "counts": counts,
            "target_count": len(batch.metadata["targets"]) if isinstance(batch.metadata.get("targets"), list) else None,
            "sample_count": len({r.get("sample_key") for r in rows}), "execution_count": len(rows)}
