"""Human-owned source-host preferences; never resource URLs or execution secrets."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from django.db import transaction
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

from tools.upstream_match_worker import validate_url
from ..models import UpstreamRoute, UpstreamRouteAudit
from .common import XhsParseError, extract_url_from_text, host_matches, is_blocked_hostname
from .env_config import runtime_list
from .quality_execution import PROVIDER_ALIASES


PROVIDER_IDS = tuple(dict.fromkeys(PROVIDER_ALIASES.values()))
# Request admission and runtime readiness apply independently of Human preference.
DEFAULT_PROVIDER_ORDER = ("yt_dlp_generic", "gallery_dl_generic", "parsehub_generic", "videodl_repo",
                          "tiantianjiexi_generic", "kukutool_generic")


class UpstreamRouteError(ValueError):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def source_scope(source_url):
    try:
        url = extract_url_from_text(source_url)
        validate_url(url)
        host = urlsplit(url).hostname.lower().rstrip(".").encode("idna").decode("ascii")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or is_blocked_hostname(host):
            raise ValueError()
    except (ValueError, TypeError, AttributeError, UnicodeError, XhsParseError):
        raise UpstreamRouteError("INVALID_SOURCE") from None
    from .platforms.third_party import THIRD_PARTY_PLATFORM_RULES, BLOCKED_PUBLIC_HOST_SUFFIXES
    from .registry import DEFAULT_ADAPTERS
    mature = any(rule.supports_hostname(host) for rule in THIRD_PARTY_PLATFORM_RULES) or any(
        adapter.platform_id != "third_party" and adapter.supports_url(url) for adapter in DEFAULT_ADAPTERS
    )
    blocked = host_matches(host, BLOCKED_PUBLIC_HOST_SUFFIXES)
    return {"source_host": host, "applicable": not mature and not blocked,
            "reason": "existing_route_not_overridden" if mature else "source_blocked" if blocked else ""}


def default_provider_order():
    configured = runtime_list("UPSTREAM_PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    return list(dict.fromkeys(pid for pid in configured if pid in PROVIDER_IDS))


def _snapshot(route):
    return {"revision": route.revision, "active": route.active,
            "provider_order": deepcopy(route.provider_order),
            "provider_options": deepcopy(route.provider_options),
            "format_preferences": deepcopy(route.format_preferences)}


def read_route(source_url):
    scope = source_scope(source_url)
    route = UpstreamRoute.objects.filter(source_host=scope["source_host"]).first() if scope["applicable"] else None
    state = _snapshot(route) if route else {
        "revision": 0, "active": False, "provider_order": [], "provider_options": {}, "format_preferences": {},
    }
    return {**scope, **state, "mode": "manual" if state["active"] else "default",
            "effective_provider_order": state["provider_order"] if state["active"] else default_provider_order(),
            "allowed_provider_ids": list(PROVIDER_IDS)}


def validate_preferences(provider_order, provider_options, format_preferences):
    if (not isinstance(provider_order, list) or not 1 <= len(provider_order) <= len(PROVIDER_IDS)
            or any(not isinstance(pid, str) or pid not in PROVIDER_IDS for pid in provider_order)
            or len(set(provider_order)) != len(provider_order)):
        raise UpstreamRouteError("INVALID_PROVIDER_ORDER")
    if not isinstance(provider_options, dict) or set(provider_options) - set(provider_order):
        raise UpstreamRouteError("INVALID_PROVIDER_OPTIONS")
    options = {}
    for pid, value in provider_options.items():
        if value == {}:
            continue
        if pid != "videodl_repo" or not isinstance(value, dict):
            raise UpstreamRouteError("INVALID_PROVIDER_OPTIONS")
        if value == {"mode": "dedicated_only"}:
            options[pid] = dict(value)
            continue
        if set(value) != {"mode", "backend_id"} or value["mode"] != "specific_backend":
            raise UpstreamRouteError("INVALID_PROVIDER_OPTIONS")
        from .provider_lab.videodl_policy import REVIEWED_DEDICATED_BACKEND_IDS, backend_policy
        backend = value["backend_id"]
        if (backend not in REVIEWED_DEDICATED_BACKEND_IDS
                or backend_policy(backend, "dedicated", registry_present=True).controlled_state != "allowed"):
            raise UpstreamRouteError("INVALID_BACKEND")
        options[pid] = dict(value)
    if not isinstance(format_preferences, dict) or set(format_preferences) - {"max_height", "preferred_video_codec", "preferred_container"}:
        raise UpstreamRouteError("INVALID_FORMAT_PREFERENCES")
    for key, value in format_preferences.items():
        if key == "max_height":
            valid = type(value) is int and 144 <= value <= 8640
        elif key == "preferred_video_codec":
            valid = value in ("h264", "hevc", "vp9", "av1")
        else:
            valid = value in ("mp4", "webm", "mkv")
        if not valid:
            raise UpstreamRouteError("INVALID_FORMAT_PREFERENCES")
    return list(provider_order), options, dict(format_preferences)


def write_route(source_url, *, expected_revision, actor, provider_order=None,
                provider_options=None, format_preferences=None, reset=False, reference_execution_id=None):
    scope = source_scope(source_url)
    if not scope["applicable"]:
        raise UpstreamRouteError("ROUTE_NOT_APPLICABLE", 422)
    if type(expected_revision) is not int or expected_revision < 0:
        raise UpstreamRouteError("INVALID_REVISION")
    values = ([], {}, {}) if reset else validate_preferences(
        provider_order, provider_options if provider_options is not None else {},
        format_preferences if format_preferences is not None else {},
    )
    reference = None
    if reference_execution_id is not None:
        if type(reference_execution_id) is not int:
            raise UpstreamRouteError("INVALID_HISTORY_REFERENCE")
        from xhs_download.models import EvaluationExecution
        record = EvaluationExecution.objects.filter(pk=reference_execution_id).first()
        if not record or record.summary.get("source_host") != scope["source_host"]:
            raise UpstreamRouteError("INVALID_HISTORY_REFERENCE")
        from .provider_lab.providers import list_provider_descriptors
        current = {d["execution_provider_id"]: d for d in list_provider_descriptors()
                   if d.get("execution_provider_id")}
        if any(not current.get(pid, {}).get("available") or not current.get(pid, {}).get("route_selectable")
               for pid in values[0]):
            raise UpstreamRouteError("HISTORY_PROVIDER_UNAVAILABLE", 422)
        reference = {"execution_id": record.pk, "batch_id": record.batch_id,
                     "execution_fingerprint": record.fingerprint}
    with transaction.atomic():
        route, _ = UpstreamRoute.objects.select_for_update().get_or_create(source_host=scope["source_host"])
        before = _snapshot(route)
        changed = UpstreamRoute.objects.filter(pk=route.pk, revision=expected_revision).update(
            revision=F("revision") + 1, active=not reset, provider_order=values[0],
            provider_options=values[1], format_preferences=values[2], updated_at=timezone.now(),
        )
        if changed != 1:
            raise UpstreamRouteError("REVISION_CONFLICT", 409)
        route.refresh_from_db()
        after = _snapshot(route)
        if reference:
            after["evaluation_reference"] = reference
        UpstreamRouteAudit.objects.create(
            route=route, revision=route.revision, action="reset" if reset else "save",
            before=before, after=after, actor_id=getattr(actor, "pk", None),
            actor_username=str(getattr(actor, "username", ""))[:150],
        )
    return read_route(source_url)


def route_cache_identity(route, matches):
    """Routing/executor component; resource caches must also key by source URL."""
    execution = [(m.get("provider_id"), m.get("extractor_id"), m.get("version")) for m in matches]
    payload = {key: route[key] for key in ("source_host", "revision", "effective_provider_order",
                                         "provider_options", "format_preferences")}
    return sha256(json.dumps([payload, execution], sort_keys=True).encode()).hexdigest()


def route_matches(source_url, matches):
    route = read_route(source_url)
    matches = tuple(matches)
    from .quality_execution import SERVICE_PROVIDER_IDS
    current_services = {
        (match['provider_id'], match['extractor_id'], match['version'])
        for match in saved_service_matches(source_url)
    }
    matches = tuple(match for match in matches if match.get('provider_id') not in SERVICE_PROVIDER_IDS
                    or (match.get('provider_id'), match.get('extractor_id'), match.get('version')) in current_services)
    if any(match.get("provider_id") == "videodl_repo" for match in matches):
        current = (videodl_route_availability(source_url, route)["matches"]
                   if route["applicable"] and "videodl_repo" in route["effective_provider_order"] else ())
        allowed = {(match.get("provider_id"), match.get("extractor_id"), match.get("version"))
                   for match in current}
        matches = tuple(match for match in matches if match.get("provider_id") != "videodl_repo"
                        or (match.get("provider_id"), match.get("extractor_id"), match.get("version")) in allowed)
    ordered = tuple(match for pid in route["effective_provider_order"]
                    for match in matches if match.get("provider_id") == pid)
    return ordered, {**route, "cache_identity": route_cache_identity(route, ordered)}


def saved_service_matches(source_url):
    """Match configured services in the effective route, without a remote call.

    The legacy function name is retained for callers. Default routes are eligible;
    source/access gates still decide which matches an ordinary request may use.
    """
    from .quality_execution import SERVICE_PROVIDER_IDS, service_match, provider_available
    from .provider_lab.contracts import ProviderLabError
    if not any(provider_available(provider) for provider in SERVICE_PROVIDER_IDS):
        return ()
    try:
        route = read_route(source_url)
        if not route['applicable']:
            return ()
        matches = []
        for provider in route['effective_provider_order']:
            if provider not in SERVICE_PROVIDER_IDS:
                continue
            try:
                matches.append(service_match(provider, source_url))
            except ProviderLabError:
                continue
        return tuple(matches)
    except (UpstreamRouteError, ValueError):
        return ()


def saved_videodl_matches(source_url):
    """Match the effective route against installed, reviewed dedicated backends."""
    from .quality_execution import provider_available
    if not provider_available("videodl_repo"):
        return ()
    try:
        route = read_route(source_url)
        return tuple(videodl_route_availability(source_url, route)["matches"])
    except Exception:
        # Discovery must fail closed, including unavailable migrations/runtimes.
        return ()


def videodl_route_availability(source_url, route):
    from .quality_execution import match_saved_videodl, provider_available
    from .provider_lab.contracts import ProviderLabError
    from .provider_lab.videodl_policy import backend_policy, VIDEODL_POLICY_PIN, VIDEODL_POLICY_REVISION
    if not route["applicable"] or "videodl_repo" not in route["effective_provider_order"]:
        return {"status": "not_selected", "reason": "", "matches": []}
    unavailable = {"status": "unavailable", "reason": "PROVIDER_NOT_CONFIGURED", "matches": []}
    if not provider_available("videodl_repo"):
        return unavailable
    identity = parse_cache_key(source_url, {**route, "cache_identity": route_cache_identity(route, ())}, "videodl_repo")
    key = f"{identity}:match:{VIDEODL_POLICY_REVISION}" if identity else None
    try:
        cached = cache.get(key) if key else None
    except Exception:
        cached = None
    if cached is not None:
        return cached
    options = route["provider_options"].get("videodl_repo") or {"mode": "dedicated_only"}
    try:
        matches = match_saved_videodl(source_url, options=options, timeout_seconds=5.0)
        matches = [match for match in matches if match.get("provider_id") == "videodl_repo"
                   and match.get("version") == f"videodl@{VIDEODL_POLICY_PIN}"
                   and backend_policy(match.get("extractor_id"), "dedicated").controlled_state == "allowed"
                   and (options.get("mode") != "specific_backend"
                        or match.get("extractor_id") == options.get("backend_id"))]
        status = {"status": "available" if matches else "unavailable",
                  "reason": "" if matches else "PROVIDER_UNSUPPORTED", "matches": matches}
    except ProviderLabError as error:
        reason = error.code if error.code in {"PROVIDER_NOT_CONFIGURED", "PROVIDER_LICENSE_RESTRICTED",
            "PROVIDER_POLICY_REJECTED", "PROVIDER_TIMEOUT", "PROVIDER_SCHEMA_INVALID"} else "PROVIDER_UNAVAILABLE"
        status = {**unavailable, "reason": reason}
    except Exception:
        status = {**unavailable, "reason": "PROVIDER_UNAVAILABLE"}
    if key:
        try:
            cache.set(key, status, timeout=30)
        except Exception:
            pass
    return status


def parse_cache_key(source_url, route, provider):
    """Cache only executable, admitted providers, scoped to this source and revision."""
    from .quality_execution import _python_for, SERVICE_PROVIDER_IDS
    if provider in SERVICE_PROVIDER_IDS:
        # Reuse the existing service client's cache, never an extra stale account snapshot.
        return None
    try:
        python = _python_for(provider)
        if provider in ("yt_dlp_generic", "gallery_dl_generic"):
            from .upstream_matching import _executor
            _, signature = _executor(provider)
        else:
            root = Path(__file__).resolve().parents[2]
            paths = [Path(python), Path(__file__), root / "xhs_download/services/quality_execution.py",
                     root / "tools" / ("upstream_parsehub_worker.py" if provider == "parsehub_generic"
                                       else "provider_lab/runtime/workers/videodl_worker.py")]
            if provider == "videodl_repo":
                paths.append(root / "xhs_download/services/provider_lab/videodl_policy.py")
            signature = [(str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths]
        payload = [source_url, route["cache_identity"], provider, signature]
        return "upstream:parse:v1:" + sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    except (OSError, ValueError, RuntimeError, XhsParseError):
        return None


def cached_parse_result(key, source_url, provider):
    if not key:
        return None
    from .upstream_candidates import resolve_candidate_record
    from .upstream_delivery import upstream_manifest_item_is_allowed
    try:
        result = cache.get(key)
        if result is None:
            return None
        candidates = (result.metadata or {}).get("upstream_candidates", [])
        if not result.assets and not candidates:
            return None
        for candidate in candidates:
            record = resolve_candidate_record(candidate.get("resource_token"), candidate.get("candidate_id"))
            if (record.get("source_url") != source_url or record.get("provider_id") != provider
                    or record.get("public") != candidate):
                return None
        if any(not upstream_manifest_item_is_allowed(asset.to_download_item())
               or asset.provider != provider
               or asset.provider_resource_id != sha256(source_url.encode()).hexdigest()
               for asset in result.assets):
            return None
        return deepcopy(result)
    except Exception:
        # A cache outage or expired authority is a miss, not a provider failure.
        return None


def cache_parse_result(key, result):
    if key:
        try:
            cache.set(key, deepcopy(result), timeout=60)
        except Exception:
            pass


def apply_format_preferences(result, preferences):
    """Prioritize matching video formats without dropping media or changing ordinals."""
    if not preferences:
        return result
    candidates = list((result.metadata or {}).get("upstream_candidates", []))
    prefixes = {"h264": ("h264", "avc1", "avc3"), "hevc": ("hevc", "h265", "hev1", "hvc1"),
                "vp9": ("vp9", "vp09"), "av1": ("av1", "av01")}

    def matches(candidate):
        height = candidate.get("height")
        if "max_height" in preferences and (type(height) not in (int, float)
                                              or not 0 < height <= preferences["max_height"]):
            return False
        codec = preferences.get("preferred_video_codec")
        if codec and not str(candidate.get("vcodec") or "").lower().startswith(prefixes[codec]):
            return False
        container = preferences.get("preferred_container")
        return not container or str(candidate.get("extension") or "").lower() == container

    video_slots = [i for i, candidate in enumerate(candidates) if candidate.get("media_type") == "video"]
    videos = [candidates[i] for i in video_slots]
    preferred = [candidate for candidate in videos if matches(candidate)]
    preferred.sort(key=lambda candidate: -(candidate.get("height") or 0))
    ordered = preferred + [candidate for candidate in videos if not matches(candidate)]
    for index, candidate in zip(video_slots, ordered):
        candidates[index] = candidate
    metadata = {**(result.metadata or {}), "upstream_candidates": candidates,
                "format_preference_selection": {
                    "status": "matched" if preferred else "unmatched",
                    "candidate_id": preferred[0].get("candidate_id") if preferred else None,
                    "preferences": dict(preferences),
                }}
    return replace(result, metadata=metadata)
