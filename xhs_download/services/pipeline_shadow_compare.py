"""Pure comparison: raw is passed only to an adapter, never interpreted here.

The old result is evidence, not input to a mapper or a new download decision.
No probing, token issuance, cache lookup or network is performed.
"""
from dataclasses import replace
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from ..templatetags.ytdlp_picker import remaining_picker_videos, ytdlp_presentation
from .pipeline import (Failure, Work, UnknownExtension, decide_delivery, fact_gaps,
                       field_coverage, file_name, height_from_label, validate_work)
from .provider_adapters.shadow import mapper_for, provider_key
from .provider_adapters.recordings import sanitize
from .provider_strategy import build_download_strategy


def _entry(provider, codes=()):
    return {"provider": provider, "difference_codes": list(codes), "differences": [],
            "comparison_gaps": [], "field_coverage": {}, "fact_gaps": [],
            "invariants": {"checked": False, "violations": []}}


def _url_value(value):
    if not value:
        return ""
    parsed = urlsplit(value)
    # Compare exact addresses in memory; don't persist signed addresses in the
    # health record. The digest distinguishes resources without authorizing one.
    return {"host": parsed.hostname or "", "sha256": sha256(value.encode()).hexdigest()}


def _diff(entry, code, field, old, new, *, url=False):
    if old == new:
        return
    if code not in entry["difference_codes"]:
        entry["difference_codes"].append(code)
    entry["differences"].append({"field": field, "old": _url_value(old) if url else old,
                                 "new": _url_value(new) if url else new})


def _provided(work, field):
    source = work.field_sources.get(field, "")
    return bool(source and source != "request" and not source.startswith(("contract:", "request:", "adapter:")))


def _asset_rows(legacy, display):
    shown = [dict(row, kind=kind) for kind, key in (("image", "image_items"), ("video", "video_items"))
             for row in display.get(key, ())]
    rows = []
    for asset in legacy.assets:
        matches = [index for index, row in enumerate(shown) if row["kind"] == asset.media_type
                   and (row.get("video_url") or row.get("url")) == asset.url]
        matched = next((index for index in matches if asset.asset_id
                        and shown[index].get("asset_id") == asset.asset_id), next(iter(matches), None))
        row = shown.pop(matched) if matched is not None else {}
        quality = row.get("quality_label", asset.quality_label)
        rows.append({"asset": asset, "provider": asset.provider, "kind": asset.media_type,
                     "url": row.get("video_url") or row.get("url") or asset.url,
                     "quality": quality, "height": height_from_label(quality),
                     "size": row.get("size_bytes", asset.size_bytes) or None,
                     "filename": row.get("filename") or asset.filename or None,
                     "delivery": row.get("download_strategy") or build_download_strategy(
                         asset, result_platform=legacy.platform, result_metadata=legacy.metadata)["download_strategy"]})
    return rows


def _candidate_record(selected, candidates):
    provider, identity = selected.get("provider_id"), selected.get("candidate_id")
    candidates = candidates or {}
    record = candidates.get((provider, identity)) or candidates.get(identity)
    if (not isinstance(record, dict) or record.get("candidate_id") != identity
            or record.get("provider_id") != provider):
        return {}
    return record


def _legacy_rows(legacy, display, candidates):
    metadata = legacy.metadata or {}
    display = display or {}
    rows = _asset_rows(legacy, display)
    videos = display.get("video_items")
    if videos is None:
        videos = [{**row["asset"].to_download_item(), "video_url": row["url"],
                   "size_bytes": row["size"], "filename": row["filename"]}
                  for row in rows if row["kind"] == "video"]
    presentation = ytdlp_presentation(
        display.get("upstream_candidates", metadata.get("upstream_candidates", [])),
        display.get("upstream_route", metadata.get("upstream_route", {})), videos,
        display.get("display_title", metadata.get("title")), display.get("base_filename", legacy.base_filename))
    if not presentation:
        return rows
    selected_rows = []
    for group in presentation.get("groups") or [presentation]:
        selected = group["selected"]
        record = _candidate_record(selected, candidates)
        bound = [row for row in rows if selected.get("legacy_delivery") == "existing_manifest"
                 and selected.get("legacy_asset_id") and row["asset"].asset_id == selected["legacy_asset_id"]
                 and provider_key(row["provider"], legacy.platform) == provider_key(selected.get("provider_id"))
                 and (not record.get("url") or row["url"] == record["url"])]
        binding = bound[0] if len(bound) == 1 else {}
        # The selector's public choice identifies a default, not a URL grant.
        # Only its captured issuance record or exact legacy binding can supply it.
        selected_rows.append({
            "provider": selected.get("provider_id"), "kind": selected.get("media_type"),
            "url": record.get("url") or binding.get("url"),
            "quality": selected.get("label") or selected.get("format_label") or "",
            "height": selected.get("height") or height_from_label(selected.get("format_label")),
            "size": selected.get("size_bytes") or None,
            "filename": selected.get("filename") or binding.get("filename"),
            "delivery": binding.get("delivery"),
        })
    remaining = remaining_picker_videos(presentation, videos)
    for row in rows:
        if row["kind"] == "image":
            selected_rows.append(row)
        elif row["kind"] == "video":
            match = next((index for index, shown in enumerate(remaining)
                          if (shown.get("video_url") or shown.get("url")) == row["url"]
                          and (not shown.get("asset_id") or shown["asset_id"] == row["asset"].asset_id)), None)
            if match is not None:
                selected_rows.append(row)
                remaining = [*remaining[:match], *remaining[match + 1:]]
    return selected_rows


def _gap(entry, field, code):
    entry["comparison_gaps"].append({"field": field, "code": code})


def compare_observation(observation, legacy=None, *, display=None, legacy_sources=None, candidates=None):
    provider = provider_key(observation.provider)
    entry = _entry(provider)
    mapper = mapper_for(observation)
    if mapper is None:
        entry["difference_codes"].append("ADAPTER_MISSING")
        return entry
    work = mapper.map(observation.raw, observation.source_url)
    if isinstance(work, Failure):
        entry["mapping_failure"] = {"kind": work.kind.value, "diagnostic": work.code}
        if work.kind.value == "invalid_result":
            entry["difference_codes"].append("ADAPTER_INVALID_" + work.code)
        else:
            entry["difference_codes"].append("ADAPTER_FAILURE_" + work.kind.value.upper() + "_" + work.code)
        return entry
    if not isinstance(work, Work):
        entry["difference_codes"].append("ADAPTER_INVALID_TYPE")
        return entry
    work = replace(work, provider=provider, platform=getattr(legacy, "platform", "") or
                   observation.metadata.get("platform", ""))
    violations = validate_work(work)
    entry["invariants"] = {"checked": True, "violations": list(violations)}
    entry["difference_codes"].extend("ADAPTER_INVALID_" + value.upper() for value in violations)
    entry["field_coverage"] = field_coverage(work)
    entry["fact_gaps"] = list(fact_gaps(work))
    if violations:
        return entry
    defaults = [(item, item.default_variant) for item in work.items]
    decisions = [decide_delivery(item.kind, variant) for item, variant in defaults]
    if any(decision.rule == 7 for decision in decisions):
        entry["difference_codes"].append("RULE7_HIT")
    # Tests which only ask a raw client have no user-facing legacy result to
    # compare. Still report mapping, coverage and invariant checks, not a fake
    # item-count difference or a second normalization/probe.
    if legacy is None:
        entry["comparison_available"] = False
        return entry
    entry["comparison_available"] = True
    metadata = legacy.metadata or {}
    sources = legacy_sources or {}
    rows = _legacy_rows(legacy, display, candidates)
    old_providers = {provider_key(row["provider"], legacy.platform) for row in rows}
    old_providers.update(provider_key(value, legacy.platform) for value in sources.values())
    old_providers.discard("unknown")
    merged = len(old_providers) > 1
    if merged:
        entry["difference_codes"].append("OLD_RESULT_MERGED")
    def comparable(field, paths=()):
        if not merged:
            return True
        owner = sources.get(field)
        return (any(_provided(work, path) for path in paths or (field,))
                and (not owner or provider_key(owner, legacy.platform) == provider))
    if comparable("title"):
        _diff(entry, "TITLE_DIFF", "title", (display or {}).get("display_title") or
              metadata.get("title") or legacy.base_filename, work.title)
    if merged:
        rows = [row for row in rows if provider_key(row["provider"], legacy.platform) == provider]
    if comparable("items"):
        _diff(entry, "ITEM_COUNT_DIFF", "items", len(rows), len(work.items))
    # Content order is retained by both contracts. When counts differ, avoid
    # manufacturing pairwise comparisons between unrelated resources.
    if len(rows) != len(defaults) or not comparable("items"):
        return entry
    for index, (row, (item, variant), decision) in enumerate(zip(rows, defaults, decisions), 1):
        prefix = f"items.{index}."
        source_prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
        _diff(entry, "ITEM_KIND_DIFF", prefix + "kind", row["kind"], item.kind.value)
        if row["url"]:
            _diff(entry, "DEFAULT_URL_DIFF", prefix + "url", row["url"], variant.url, url=True)
        else:
            _gap(entry, prefix + "url", "OLD_DEFAULT_URL_UNKNOWN")
        if comparable("quality", (source_prefix + "height", source_prefix + "quality_label")):
            _diff(entry, "DEFAULT_QUALITY_DIFF", prefix + "quality",
                  [row["height"], row["quality"]],
                  [variant.height, variant.display_label])
        if comparable("size", (source_prefix + "size_bytes",)):
            _diff(entry, "SIZE_DIFF", prefix + "size", row["size"], variant.size_bytes)
        old_method = row["delivery"]
        if old_method:
            _diff(entry, f"DELIVERY_DIFF_{old_method.upper()}_TO_{decision.method.value.upper()}",
                  prefix + "delivery", old_method, decision.method.value)
        else:
            _gap(entry, prefix + "delivery", "OLD_DELIVERY_UNKNOWN")
        try:
            name = file_name(work, item, variant)
        except UnknownExtension:
            name = None
        # An absent legacy name is unknown, not evidence of a synthesized .bin.
        if row["filename"]:
            _diff(entry, "FILENAME_DIFF", prefix + "filename", row["filename"], name)
        else:
            _gap(entry, prefix + "filename", "OLD_FILENAME_UNKNOWN")
    return entry


def compare_capture(session, *, expected_provider="", platform=""):
    entries = []
    for observation in session.records:
        try:
            entries.append(compare_observation(observation, session.result, display=session.display,
                           legacy_sources=session.legacy_sources.get(id(session.result)),
                           candidates=session.legacy_candidates))
        except Exception:
            entries.append(_entry(provider_key(observation.provider), ("SHADOW_ERROR",)))
    entries.extend(_entry(provider_key(provider, platform), ("SHADOW_ERROR",))
                   for provider in sorted(session.errors))
    metadata = getattr(session.result, "metadata", None) or {}
    result_platform = platform or getattr(session.result, "platform", "")
    expected = {provider_key(value, result_platform) for value in (
        expected_provider, metadata.get("selected_provider"), metadata.get("source_provider")) if value}
    expected.update(provider_key(asset.provider, result_platform)
                    for asset in getattr(session.result, "assets", ()))
    expected.update(provider_key(value, result_platform) for value in
                    session.legacy_sources.get(id(session.result), {}).values())
    expected.discard("unknown")
    observed = {entry["provider"] for entry in entries}
    entries.extend(_entry(provider, ("SHADOW_NO_RAW",)) for provider in sorted(expected - observed))
    if not entries:
        provider = provider_key(expected_provider or metadata.get("selected_provider") or
                                metadata.get("source_provider") or "", platform or
                                getattr(session.result, "platform", ""))
        entries.append(_entry(provider, ("SHADOW_NO_RAW",)))
    # Detail retention is administrative only and uses the existing fixture
    # scrubber. Public persistence consumes only fixed dimensions/codes below.
    try:
        details = {"schema": 1, "observations": entries}
        clean = sanitize({"schema_version": 1, "provider": "shadow", "platform": "shadow",
                          "source_url": "https://shadow.example.org/", "provenance": {
                              "kind": "synthetic", "raw_kind": "json", "complete": True}, "raw": details})["raw"]
        if len(json.dumps(clean).encode()) > 256 * 1024:
            for entry in clean["observations"]:
                entry["differences"] = []
            clean["details_omitted"] = "SIZE_LIMIT"
        return clean
    except Exception:
        return {"schema": 1, "observations": [_entry("unknown", ("SHADOW_ERROR",))]}


def count_codes(details):
    for entry in details["observations"]:
        codes = set(entry["difference_codes"]) | set(entry.get("fact_gaps", ()))
        codes.update(gap["code"] for gap in entry.get("comparison_gaps", ()))
        if not codes:
            codes.add("SHADOW_MATCH" if entry.get("comparison_available") else "SHADOW_MAPPED")
        for code in sorted(codes):
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", code):
                yield entry["provider"], code
