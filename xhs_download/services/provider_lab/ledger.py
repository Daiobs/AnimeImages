"""Read-only, full-scope platform projections of persisted evaluation evidence.

No runtime discovery, provider calls, cache writes, or inferred route decisions.
Only compact observations survive the chunked database scan; asset details are
returned on demand and never included in platform lists or execution-list pages.
"""
from collections import defaultdict
from datetime import timezone
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from django.utils.dateparse import parse_datetime

from xhs_download.models import EvaluationExecution, UpstreamRoute
from xhs_download.services.quality_execution import PROVIDER_ALIASES
from xhs_download.services.upstream_routes import (
    UpstreamRouteError, canonical_source_host, default_provider_order, source_scope, validate_preferences,
)
from . import history


MAX_BYTES = 2 * 1024 * 1024
MAX_ROW_BYTES = 12 * 1024
MAX_ASSET_BYTES = 1500
MAX_SOURCE_BYTES = 2048
MAX_ROUTE_SUMMARY_BYTES = 4096
MAX_PLATFORM_PREVIEW_BYTES = 16 * 1024
PROVIDER_PREVIEW_SIZE = 10
CONTENT_TYPES = {"video", "image", "gallery", "audio", "livephoto", "multimedia", "article", "unknown"}
EVIDENCE = {"none": 0, "declared": 1, "unconfirmed": 2, "confirmed": 3}
EVIDENCE_ALIASES = {"no_valid_media": "none", "declared_only": "declared",
                    "unconfirmed_media": "unconfirmed", "confirmed_media": "confirmed"}
UNEXECUTED = {"planned", "not_executed", "not_run", "skipped", "cancelled", "canceled",
              "not_configured", "not_authorized", "not_applicable", "inapplicable"}
SCOPE = {"kind": "persisted_history_and_routes", "all_saved_records": True,
         "external_archives_included": False, "hostname_scope": "exact",
         "runtime_status": "unchecked", "timestamps": "observation_only_no_created_at_fallback",
         "observation_count": "unique_native_or_attempted_observations_excluding_unexecuted_and_offline_replays"}


def canonical_host(value):
    try:
        return canonical_source_host(value)
    except UpstreamRouteError:
        return ""


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = parse_datetime(value)
        if parsed is not None and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    return None


def positive_int(value, default, maximum=None):
    if value in (None, ""):
        return default
    if not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,10}", str(value)):
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    number = int(value)
    if number < 1 or (maximum is not None and number > maximum):
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    return number


def paging(params, total, *, prefix="", default=25, maximum=100):
    page = positive_int(params.get(prefix + "page"), 1)
    size = positive_int(params.get(prefix + "page_size"), default, maximum)
    return {"total": total, "page": page, "page_size": size, "pages": (total + size - 1) // size}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _string(value):
    return value if isinstance(value, str) else ""


def _text(value, limit=160):
    if not isinstance(value, str):
        return ""
    return str(history.safe_value(value))[:limit]


def _safe_source(value, host, *, retest=False):
    try:
        source, source_host, _ = history.source_info(value)
        if source_host != host or not canonical_host(source_host):
            return ""
        # Query-bearing sources can identify a work internally, but are not
        # offered as reusable links in this projection.
        if retest and (urlsplit(source).query or history.PRIVATE.search(urlsplit(source).path)):
            return ""
        return source
    except history.HistoryError:
        return ""


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=True).encode())


def _source_projection(value, host):
    source = _safe_source(value, host, retest=True) if host else ""
    omitted = _json_size(source) > MAX_SOURCE_BYTES
    return ("" if omitted else source), omitted


SUMMARY_FIELDS = ("source_host", "source_url", "provider_id", "state", "started_at", "finished_at",
                  "options", "replay_of", "replay_batch_key", "execution_key")
RESULT_FIELDS = ("status", "executed", "request_id", "request_attempted", "request_returned",
                 "error_code", "error_detail", "client_status", "result_kind", "assets", "cache_hit")
META_FIELDS = ("identity_confirmed", "evidence_kind", "capture_status", "scope_label", "asset_count",
               "content_type", "observed_at", "archive_sha256", "baseline")


def _observation(row):
    s = {key: row.get("summary__" + key) for key in SUMMARY_FIELDS}
    r = {key: row.get("summary__result__" + key) for key in RESULT_FIELDS}
    m = {key: row.get("summary__result__metadata__" + key) for key in META_FIELDS}
    host = canonical_host(s["source_host"])
    source = _safe_source(s["source_url"], host) if host else ""
    provider = PROVIDER_ALIASES.get(_string(s["provider_id"]), _text(s["provider_id"], 80))
    native = (r["status"] == "native_observed" or m["evidence_kind"] == "native_capture_replay"
              or row.get("batch__metadata__evidence_kind") == "native_capture_replay")
    offline = not native and _string(m["evidence_kind"]) in {"offline_replay", "offline_conversion", "replay"}
    assets = r["assets"] if isinstance(r["assets"], list) else None
    media = [a for a in (assets or []) if isinstance(a, dict)
             and _string(a.get("media_type")) in {"image", "audio", "video"}
             and str(a.get("role", "")).lower() not in {"cover", "thumbnail", "poster"}]
    has_media = bool(media) if assets is not None else type(m["asset_count"]) is int and m["asset_count"] > 0
    types = {a["media_type"] for a in media}
    declared_kind = r["result_kind"] if _string(r["result_kind"]) in CONTENT_TYPES - {"unknown"} else _string(m["content_type"])
    if not types and assets is None and declared_kind in CONTENT_TYPES - {"unknown"}:
        types = {"image" if declared_kind == "gallery" else declared_kind}
    kind = "multimedia" if len(types) > 1 else next(iter(types), "unknown")
    state, status = str(s["state"] or "").lower(), str(r["status"] or "").lower()
    not_run = state in UNEXECUTED or status in UNEXECUTED or (r["executed"] is False and not native)
    uncertain = r["client_status"] == "OUTCOME_UNKNOWN" or r["error_detail"] == "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"
    success = (not native and not offline and not not_run and not uncertain
               and status == "success" and state in {"returned", "success"})
    confirmed = has_media and m["identity_confirmed"] is True and (
        (native and m["identity_confirmed"] is True
         and str(m["capture_status"] or s["state"] or "").upper() == "RETURNED")
        or (not native and not offline and not not_run and not uncertain
            and (success or (m["identity_confirmed"] is True and state in {"returned", "failed"}))))
    evidence = ("confirmed" if confirmed else "unconfirmed" if has_media
                else "declared" if _string(m["scope_label"]) in {"EXPLICIT_SUPPORT", "DECLARED_SUPPORT", "SUPPORTED"}
                or status in {"declared", "supported"} else "none")
    latest_status = ("not_executed" if not_run else "unknown" if uncertain
                     else "native_observed" if native else status if status in {
                         "success", "failed", "timeout", "unsupported", "running", "queued"}
                     else state if state in {"running", "queued"} else "unknown")
    at = timestamp(s["finished_at"]) or timestamp(s["started_at"])
    if native:
        at = timestamp(m["observed_at"]) or at
    options = _dict(s["options"])
    options = _dict(options.get("videodl_repo", options))
    route_options = {}
    if options.get("mode") == "dedicated_only":
        route_options = {"mode": "dedicated_only"}
    elif (options.get("mode") == "specific_backend"
          and isinstance(options.get("backend_category", "dedicated"), str)
          and options.get("backend_category", "dedicated") in {"dedicated", "common"}):
        backend_ids = options.get("backend_ids")
        backend = options.get("backend_id")
        if "backend_ids" in options:
            if (isinstance(backend_ids, list) and len(backend_ids) == 1
                    and isinstance(backend_ids[0], str)
                    and (backend is None or backend == backend_ids[0])):
                backend = backend_ids[0]
            else:
                backend = None
        route_options = {"mode": "specific_backend", "backend_id": backend}
        if "backend_category" in options:
            route_options["backend_category"] = options["backend_category"]
    try:
        route_backend_supported = bool(route_options)
        if route_backend_supported:
            validate_preferences(["videodl_repo"], {"videodl_repo": route_options}, {})
    except UpstreamRouteError:
        route_backend_supported = False
    observed = native or (not not_run and not offline and (r["executed"] is True
        or r["request_attempted"] is True or r["request_returned"] is True
        or state in {"returned", "success", "failed", "timeout", "sent", "running", "in_flight"}))
    return {"id": row["id"], "batch_id": row["batch_id"], "batch_key": row["batch__batch_key"],
            "application_record_id": row["application_record_id"], "host": host, "source": source,
            "provider": provider, "baseline": s["provider_id"] == "animeimages_current" or m["baseline"] is True,
            "kind": kind, "content_types": sorted(types) or ["unknown"],
            "native": native, "offline": offline, "observed": observed,
            "attempted": observed and not native and r["cache_hit"] is not True,
            "evidence": evidence, "success": success, "status": latest_status, "at": at,
            "error": _text(r["error_detail"] or r["error_code"], 160),
            "partial": has_media and status in {"failed", "timeout"}, "route_backend_supported": route_backend_supported,
            "request_id": _text(r["request_id"], 200), "archive": _text(m["archive_sha256"], 80),
            "replay_of": _text(s["replay_of"], 200), "replay_batch_key": _text(s["replay_batch_key"], 200),
            "execution_key": _text(s["execution_key"], 200),
            "evidence_kind": "native_capture_replay" if native else _text(m["evidence_kind"], 80) or "product_execution"}


def _scan():
    fields = ["id", "batch_id", "application_record_id", "batch__batch_key", "batch__metadata__evidence_kind"]
    fields += ["summary__" + key for key in SUMMARY_FIELDS]
    fields += ["summary__result__" + key for key in RESULT_FIELDS]
    fields += ["summary__result__metadata__" + key for key in META_FIELDS]
    return [_observation(row) for row in EvaluationExecution.objects.order_by("pk").values(*fields).iterator(chunk_size=128)]


def _groups(rows):
    """Union explicit copies and exact call identities, never URL-only retests."""
    by_id = {r["id"]: r for r in rows}
    parents = {pk: pk for pk in by_id}

    def root(pk):
        while parents[pk] != pk:
            parents[pk] = parents[parents[pk]]
            pk = parents[pk]
        return pk

    def link(a, b):
        if b in by_id and a["host"] == by_id[b]["host"] and a["provider"] == by_id[b]["provider"]:
            if a["source"] and a["source"] == by_id[b]["source"]:
                parents[root(a["id"])] = root(b)

    identities, references = {}, {(r["batch_key"], r["execution_key"]): r["id"] for r in rows}
    for row in rows:
        link(row, row["application_record_id"])
        # Offline conversions may explicitly reference an existing capture.
        # Lab replay_of is a real retest and must never be collapsed this way.
        if row["offline"] and row["replay_of"]:
            batch = row["replay_batch_key"] or row["batch_key"]
            link(row, references.get((batch, row["replay_of"])))
        marker = ("archive", row["archive"], row["at"]) if row["native"] and row["archive"] and row["at"] else (
            ("request", row["request_id"], row["at"]) if row["request_id"] and row["at"] else None)
        if row["source"] and marker:
            key = (row["source"], row["provider"], marker)
            if key in identities:
                link(row, identities[key])
            else:
                identities[key] = row["id"]
    grouped = defaultdict(list)
    for row in rows:
        grouped[root(row["id"])].append(row)
    return list(grouped.values())


def _empty_platform(host, route=None, defaults=()):
    configured = route is not None
    scope = source_scope(source_host=host) if host else {"source_host": "unknown", "applicable": False, "reason": "unknown_source"}
    applicable = scope["applicable"]
    effective = route if applicable else None
    active = bool(effective and effective["active"])
    current = {**scope, "configured": configured, "mode": "manual" if active else "default",
               "active": active, "revision": effective["revision"] if effective else 0,
               "provider_order": _bounded(effective["provider_order"]) if effective else [],
               "provider_options": _bounded(effective["provider_options"]) if effective else {},
               "format_preferences": _bounded(effective["format_preferences"]) if effective else {},
               "effective_provider_order": _bounded(effective["provider_order"]) if active else list(defaults),
               "allowed_provider_ids": list(dict.fromkeys(PROVIDER_ALIASES.values()))}
    return {"source_host": host or "unknown", "display_name": host or "Unknown source",
            "content_types": [], "providers": [], "current_route": current,
            "route_hint": "no_records", "latest_at": None, "observation_count": 0,
            "record_count": 0, "safe_source_url": "", "_rows": [], "_groups": []}


def _summarize(platform):
    rows, groups = platform.pop("_rows"), platform.pop("_groups")
    platform["record_count"] = len(rows)
    platform["content_types"] = sorted({kind for row in rows for kind in row["content_types"]})
    platform["latest_at"] = max((r["at"] for r in rows if r["at"]), default=None)
    platform["observation_count"] = sum(any(r["observed"] for r in group) for group in groups)
    providers = defaultdict(list)
    for group in groups:
        if not group[0]["baseline"] and group[0]["provider"]:
            providers[group[0]["provider"]].append(group)
    positive_kinds = defaultdict(set)
    for pid, p_groups in sorted(providers.items()):
        p_rows = [r for group in p_groups for r in group]
        # Undated records remain explicitly undated; insertion is not chronology.
        dated = [r for r in p_rows if r["at"]]
        latest = max(dated or p_rows, key=lambda r: (r["at"] or "", r["id"]))
        positives = [g for g in p_groups if any(r["evidence"] == "confirmed" for r in g)]
        draft = any(r["evidence"] == "confirmed" and not r["offline"]
                    and (pid != "videodl_repo" or r["route_backend_supported"]) for r in p_rows)
        selected = platform["current_route"]["active"] and pid in platform["current_route"]["provider_order"]
        adoptability = ("unknown_source" if platform["source_host"] == "unknown" else
                        "protected" if not platform["current_route"]["applicable"] else
                        "existing_route" if selected else "draft_candidate" if draft and pid in PROVIDER_ALIASES.values()
                        else "reference_only" if positives or any(r["evidence"] == "unconfirmed" for r in p_rows)
                        else "no_evidence")
        item = {"provider_id": pid, "evidence": max((r["evidence"] for r in p_rows), key=EVIDENCE.get),
                "positive_count": len(positives), "latest_status": latest["status"], "latest_error": latest["error"],
                "latest_at": latest["at"], "last_success_at": max((r["at"] for r in p_rows if r["success"] and r["at"]), default=None),
                "last_positive_at": max((r["at"] for r in p_rows if r["evidence"] == "confirmed" and r["at"]), default=None),
                "adoptability": adoptability, "runtime_status": "unchecked", "record_count": len(p_rows),
                "observation_count": sum(any(r["observed"] for r in g) for g in p_groups),
                "attempt_count": sum(any(r["attempted"] for r in g) for g in p_groups),
                "native_observation_count": sum(any(r["native"] for r in g) for g in p_groups),
                "partial_media_count": sum(any(r["partial"] for r in g) for g in p_groups),
                "undated_count": sum(not any(r["at"] for r in g) for g in p_groups)}
        platform["providers"].append(item)
        for g in positives:
            for r in g:
                if r["evidence"] == "confirmed":
                    for kind in r["content_types"]:
                        positive_kinds[kind].add(pid)
    positive = [p for p in platform["providers"] if p["positive_count"]]
    kinds = list(positive_kinds.values())
    conflict = len(kinds) > 1 and any(a.isdisjoint(b) for i, a in enumerate(kinds) for b in kinds[i + 1:])
    platform["route_hint"] = ("unknown_source" if platform["source_host"] == "unknown" else
        "no_records" if not rows else "content_conflict" if conflict
        else "reference_only" if positive and all(p["adoptability"] == "reference_only" for p in positive)
        else "comparable" if len(positive) > 1 else "unique_positive" if positive
        else "unconfirmed_media" if any(p["evidence"] == "unconfirmed" for p in platform["providers"])
        else "no_positive_evidence")
    return platform


def snapshot():
    rows = _scan()
    routes = {r["source_host"]: r for r in UpstreamRoute.objects.values(
        "source_host", "active", "revision", "provider_order", "provider_options", "format_preferences")
        if canonical_host(r["source_host"])}
    defaults = default_provider_order()
    platforms = {host: _empty_platform(host, route, defaults) for host, route in routes.items()}
    platforms[""] = _empty_platform("")
    for row in rows:
        if row["host"] not in platforms:
            platforms[row["host"]] = _empty_platform(row["host"], routes.get(row["host"]), defaults)
        platforms[row["host"]]["_rows"].append(row)
    groups = _groups(rows)
    for group in groups:
        platforms[group[0]["host"]]["_groups"].append(group)
    return {host: _summarize(platform) for host, platform in platforms.items()}, rows


def _provider_projection(platform, *, start=0, size=PROVIDER_PREVIEW_SIZE, preferred="", preview=False):
    providers = platform["providers"]
    if preferred:
        providers = sorted(providers, key=lambda p: p["provider_id"] != preferred)
    selected = providers[start:start + size]
    projected = {**platform, "providers": selected, "provider_total": len(providers),
                 "providers_truncated": len(selected) < len(providers)}
    route = dict(platform["current_route"])
    if _json_size(route) > MAX_ROUTE_SUMMARY_BYTES:
        route["summary_truncated"] = projected["summary_truncated"] = True
        optional = ("provider_options", "format_preferences", "provider_order", "effective_provider_order")
        for key in sorted(optional, key=lambda k: _json_size(route.get(k)), reverse=True):
            if _json_size(route) <= MAX_ROUTE_SUMMARY_BYTES:
                break
            route.pop(key, None)
    projected["current_route"] = route
    if preview:
        while selected and _json_size(projected) > MAX_PLATFORM_PREVIEW_BYTES:
            selected.pop()
            projected["providers_truncated"] = True
    return projected


def platforms_payload(params):
    query = params.get("q", "").strip().lower()
    kind, pid = params.get("content_type", ""), params.get("provider_id", "")
    evidence, route_state, sort = params.get("evidence", ""), params.get("route_state", ""), params.get("sort") or "latest"
    evidence = EVIDENCE_ALIASES.get(evidence, evidence)
    if (len(query) > 200 or kind and kind not in CONTENT_TYPES or evidence and evidence not in EVIDENCE
            or route_state not in {"", "manual", "default", "protected", "configured", "no_records"}
            or sort not in {"host", "name", "latest", "-latest", "oldest", "evidence", "observations", "-observations"}
            or len(pid) > 80):
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    pid = PROVIDER_ALIASES.get(pid, pid)
    platforms, _ = snapshot()
    unknown = platforms.pop("")
    records = sum(p["record_count"] for p in platforms.values()) + unknown["record_count"]
    observations = sum(p["observation_count"] for p in platforms.values()) + unknown["observation_count"]
    rows = []
    for p in platforms.values():
        if query and query not in p["source_host"] and query not in p["display_name"].lower():
            continue
        filter_kind = "image" if kind == "gallery" else kind
        if filter_kind and filter_kind not in p["content_types"] and not (
                filter_kind == "multimedia" and len(set(p["content_types"]) - {"unknown"}) > 1):
            continue
        if (pid or evidence) and not any((not pid or x["provider_id"] == pid)
                and (not evidence or x["evidence"] == evidence) for x in p["providers"]):
            continue
        if route_state in {"manual", "default"} and p["current_route"]["mode"] != route_state:
            continue
        if route_state == "default" and not p["current_route"]["applicable"]:
            continue
        if route_state == "protected" and p["current_route"]["applicable"]:
            continue
        if route_state == "configured" and not p["current_route"]["configured"]:
            continue
        if route_state == "no_records" and p["record_count"]:
            continue
        rows.append(p)
    rows.sort(key=lambda p: p["source_host"])
    if sort in {"latest", "-latest", "oldest"}:
        dated = [p for p in rows if p["latest_at"]]
        dated.sort(key=lambda p: p["latest_at"], reverse=sort != "oldest")
        rows = dated + [p for p in rows if not p["latest_at"]]
    elif sort.endswith("observations"):
        rows.sort(key=lambda p: p["observation_count"], reverse=sort.startswith("-"))
    elif sort == "evidence":
        rows.sort(key=lambda p: max((EVIDENCE[x["evidence"]] for x in p["providers"]), default=0), reverse=True)
    page = paging(params, len(rows))
    start = (page["page"] - 1) * page["page_size"]
    previews = [_provider_projection(p, preferred=pid, preview=True) for p in rows[start:start + page["page_size"]]]
    return bounded_payload({"ok": True, "platforms": previews, **page,
            "unknown_source_count": unknown["record_count"], "record_count": records,
            "unique_observation_count": observations, "scope": dict(SCOPE),
            "filtered_record_count": sum(p["record_count"] for p in rows),
            "filtered_observation_count": sum(p["observation_count"] for p in rows)})


def bounded_payload(payload):
    if _json_size(payload) > MAX_BYTES:
        raise history.HistoryError("HISTORY_RESPONSE_TOO_LARGE", 413)
    return payload


def _bounded(value, depth=0, budget=None):
    """A small, defensive display projection, also for older pre-sanitized rows."""
    if budget is None:
        budget = [64]
    budget[0] -= 1
    if depth > 4 or budget[0] < 0:
        return None
    if isinstance(value, str):
        return _text(value, 512)
    if value is None or type(value) in {bool, int, float}:
        return history.safe_value(value)
    if isinstance(value, list):
        return [_bounded(v, depth + 1, budget) for v in value[:32] if budget[0] > 0]
    if isinstance(value, dict):
        return {k: _bounded(v, depth + 1, budget) for k, v in list(value.items())[:32]
                if isinstance(k, str) and len(k) <= 100 and not history.PRIVATE.search(k)
                and not history.UNSAFE_FIELD.search(k) and budget[0] > 0}
    return None


def _trim_execution_row(row):
    if _json_size(row) <= MAX_ROW_BYTES:
        return
    row["summary_truncated"] = True
    for key in ("version", "options"):
        row.pop(key, None)
    asset_count_present = "asset_count" in row["result"]
    row["result"] = {k: v if type(v) is bool else _text(v, 160)
                     for k, v in row["result"].items()
                     if k in {"status", "executed", "error_code", "error_detail", "result_kind"}
                     and (isinstance(v, str) or type(v) is bool)}
    if asset_count_present:
        row["result"]["asset_count"] = row["asset_count"]
    row["batch"]["metadata"] = {}
    # Legacy scalar fields can together exceed the budget even after metadata
    # is removed. Retain identity, counts and comparison keys; omit display text.
    optional = ("title", "target_id", "source_status", "source_state", "replay_of",
                "replay_batch_key", "sample_key", "execution_key")
    for key in sorted(optional, key=lambda k: _json_size(row.get(k)), reverse=True):
        if _json_size(row) <= MAX_ROW_BYTES:
            break
        row.pop(key, None)
    for key in ("error_detail", "error_code", "result_kind"):
        if _json_size(row) <= MAX_ROW_BYTES:
            break
        row["result"].pop(key, None)
    for key in ("label", "batch_key", "origin"):
        if _json_size(row) <= MAX_ROW_BYTES:
            break
        row["batch"].pop(key, None)


def _asset_projection(asset):
    item = {k: _bounded(v) for k, v in _dict(asset).items() if k in history.ASSET_FIELDS}
    identity = {"asset_id": (120, 768), "media_type": (32, 192), "role": (32, 192)}
    for key, (limit, byte_limit) in identity.items():
        if key not in item:
            continue
        original = item[key]
        if isinstance(original, str):
            item[key] = _text(original, limit)
            while _json_size(item[key]) > byte_limit:
                item[key] = item[key][:-1]
            if item[key] != original:
                item["summary_truncated"] = True
        else:
            del item[key]
            item["summary_truncated"] = True
    if _json_size(item) > MAX_ASSET_BYTES:
        item["summary_truncated"] = True
        optional = set(item) - set(identity) - {"summary_truncated"}
        for key in sorted(optional, key=lambda k: (-_json_size(item[k]), k)):
            if _json_size(item) <= MAX_ASSET_BYTES:
                break
            del item[key]
    return item


def execution_row(record, *, full=False, params=None):
    s = _dict(record.summary)
    result = _dict(s.get("result"))
    stored_assets = result.get("assets", result.get("candidates"))
    assets_known = isinstance(stored_assets, list)
    assets = stored_assets if assets_known else []
    host = canonical_host(s.get("source_host"))
    safe_source, source_omitted = _source_projection(s.get("source_url"), host)
    source = _safe_source(s.get("source_url"), host) if host else ""
    projected = {"id": record.pk, "batch_id": record.batch_id, "batch__batch_key": record.batch.batch_key,
                 "application_record_id": record.application_record_id,
                 "batch__metadata__evidence_kind": _dict(record.batch.metadata).get("evidence_kind")}
    projected.update({"summary__" + key: s.get(key) for key in SUMMARY_FIELDS})
    projected.update({"summary__result__" + key: result.get(key) for key in RESULT_FIELDS})
    projected.update({"summary__result__metadata__" + key: _dict(result.get("metadata")).get(key) for key in META_FIELDS})
    observation = _observation(projected)
    row = {key: _bounded(s[key]) if key in {"version", "options"}
        else _text(s[key], {"provider_id": 80, "state": 40}.get(key, 300))
        for key in ("execution_key", "sample_key", "provider_id", "state", "title",
        "source_status", "source_state", "target_id", "version", "options", "replay_of", "replay_batch_key") if key in s}
    row.update(id=record.pk, source_host=host or "unknown", source_url=safe_source,
        started_at=timestamp(s.get("started_at")), finished_at=timestamp(s.get("finished_at")),
        application_record_id=record.application_record_id, origin=_text(record.batch.origin, 16),
        can_retest=bool(safe_source and history.retest_allowed(record)), asset_count=len(assets) if assets_known else None,
        content_types=observation["content_types"], evidence_kind=observation["evidence_kind"],
        latest_status=observation["status"], evidence=observation["evidence"],
        work_key=sha256(source.encode()).hexdigest() if source else "", can_compare_work=bool(source),
        source_projection_omitted=source_omitted,
        assets_omitted=not full and assets_known and bool(assets), batch={"id": record.batch_id, "batch_key": _text(record.batch.batch_key, 160),
            "label": _text(record.batch.label, 200), "origin": _text(record.batch.origin, 16),
            "created_at": record.batch.created_at.isoformat(), "metadata": {
                k: _bounded(v) for k, v in _dict(record.batch.metadata).items() if k in {"version", "evidence_kind"}}})
    fields = history.RESULT_FIELDS if full else {"status", "executed", "error_code", "error_detail", "client_status",
        "provider_id", "result_kind", "provider_version", "selected_backend", "backend_id", "metadata"}
    row["result"] = {k: _bounded(v) for k, v in result.items() if k in fields}
    if not full:
        row["result"]["asset_count"] = row["asset_count"]
    if source_omitted:
        row["summary_truncated"] = True
    # Bound each row independently, so even a huge legacy record cannot stop pagination.
    _trim_execution_row(row)
    if full and assets_known:
        asset_page = paging(params or {}, len(assets), prefix="asset_", default=100, maximum=1000)
        start = (asset_page["page"] - 1) * asset_page["page_size"]
        selected = [_asset_projection(asset) for asset in assets[start:start + asset_page["page_size"]]]
        row["result"]["assets"] = selected
        row["assets_omitted"] = len(selected) < len(assets)
        row["asset_paging"] = {**asset_page, "returned": len(selected)}
    elif full and not assets_known:
        row["asset_paging"] = None
        row["assets_omitted"] = False
    return row


def platform_payload(params):
    value = params.get("source_host", "")
    host = "" if value == "unknown" else canonical_host(value)
    if not host and value != "unknown":
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    platforms, rows = snapshot()
    if host not in platforms:
        raise history.HistoryError("HISTORY_NOT_FOUND", 404)
    selected = [r for r in rows if r["host"] == host]
    work_id = params.get("work_id")
    if work_id:
        work_id = positive_int(work_id, None)
        reference = next((r for r in selected if r["id"] == work_id), None)
        if reference is None:
            raise history.HistoryError("HISTORY_NOT_FOUND", 404)
        if not reference["source"]:
            raise history.HistoryError("HISTORY_SOURCE_UNAVAILABLE", 422)
        selected = [r for r in selected if r["source"] == reference["source"]]
    selected.sort(key=lambda r: (r["at"] or "", r["id"]), reverse=True)
    page = paging(params, len(selected))
    start = (page["page"] - 1) * page["page_size"]
    ids = [r["id"] for r in selected[start:start + page["page_size"]]]
    records = {r.pk: r for r in EvaluationExecution.objects.filter(pk__in=ids).select_related("batch")}
    executions = [execution_row(records[pk]) for pk in ids if pk in records]
    platform = platforms[host]
    provider_page = paging(params, len(platform["providers"]), prefix="provider_", default=20, maximum=50)
    provider_start = (provider_page["page"] - 1) * provider_page["page_size"]
    platform = _provider_projection(platform, start=provider_start, size=provider_page["page_size"])
    provider_page.update(returned=len(platform["providers"]),
        has_next=provider_page["page"] < provider_page["pages"], has_previous=provider_page["page"] > 1,
        next_page=provider_page["page"] + 1 if provider_page["page"] < provider_page["pages"] else None)
    platform["source_projection_omitted"] = False
    for r in selected:
        safe_source, source_omitted = _source_projection(r["source"], host)
        if source_omitted:
            platform["source_projection_omitted"] = True
            platform["summary_truncated"] = True
        if safe_source:
            platform["safe_source_url"] = safe_source
            break
    page.update(returned=len(executions), has_next=page["page"] < page["pages"],
                has_previous=page["page"] > 1, next_page=page["page"] + 1 if page["page"] < page["pages"] else None)
    return bounded_payload({"ok": True, "platform": platform, "executions": executions, "paging": page, **page,
            "provider_paging": provider_page,
            "scope": {**SCOPE, "work_id": work_id or None, "comparison": "exact_retained_source" if work_id else "hostname"}})


def execution_payload(params):
    execution_id = positive_int(params.get("execution_id"), None)
    record = EvaluationExecution.objects.filter(pk=execution_id).select_related("batch").first()
    if record is None:
        raise history.HistoryError("HISTORY_NOT_FOUND", 404)
    return bounded_payload({"ok": True, "execution": execution_row(record, full=True, params=params), "scope": dict(SCOPE)})
