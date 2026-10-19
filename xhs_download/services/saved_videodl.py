"""Exact-backend saved route contract; Lab confirmations are not durable grants."""

from .provider_lab.contracts import ProviderLabError
from .provider_lab.videodl_policy import VIDEODL_POLICY_PIN, VIDEODL_POLICY_REVISION, backend_policy

# Receiver review is specific to this pin. Other reviewed Lab Common backends
# remain comparison-only until their future-request receiver scope is reviewed.
COMMON_ROUTE_RECEIVERS = {
    "GVVideoClient": ("greenvideo.cc",),
    "AnyFetcherVideoClient": ("anyfetcher.com",),
}
RUNTIME_VERSION = f"videodl@{VIDEODL_POLICY_PIN}"
SHARING_SCOPE = "future_admitted_requests"


def saved_backend_descriptor(backend_id, category):
    policy = backend_policy(backend_id, category)
    recipients = COMMON_ROUTE_RECEIVERS.get(backend_id, ()) if category == "common" else ()
    allowed = (category == "dedicated" and policy.controlled_state == "allowed") or (
        category == "common" and bool(recipients)
        and policy.controlled_state in {"allowed", "requires_sharing_confirmation"})
    return {"saved_route_selectable": allowed, "saved_route_recipients": list(recipients),
            "saved_route_version": RUNTIME_VERSION}


def normalize_saved_options(value):
    if value == {"mode": "dedicated_only"}:
        return dict(value)
    if (not isinstance(value, dict) or set(value) not in (
            {"mode", "backend_id"}, {"mode", "backend_id", "backend_category"})
            or value.get("mode") != "specific_backend"
            or not isinstance(value.get("backend_id"), str)):
        raise ValueError("INVALID_PROVIDER_OPTIONS")
    category = value.get("backend_category", "dedicated")
    if not isinstance(category, str) or category not in {"dedicated", "common"} or not saved_backend_descriptor(
            value["backend_id"], category)["saved_route_selectable"]:
        raise ValueError("INVALID_BACKEND")
    return dict(value)


def confirmation_scope(host, options):
    return {"source_host": host, "backend_category": "common", "backend_id": options["backend_id"],
            "recipients": list(COMMON_ROUTE_RECEIVERS[options["backend_id"]]),
            "runtime_version": RUNTIME_VERSION, "scope": SHARING_SCOPE}


def check_saved_runtime(options, *, timeout_seconds=5.0):
    """Read installed identity/registry only; never run a parser or HTTP request."""
    from uuid import uuid4
    from .quality_execution import _python_for
    from .provider_lab.providers import WORKER_ROOT
    from .provider_lab.videodl_control import load_videodl_inventory
    inventory = load_videodl_inventory(python=_python_for("videodl_repo"),
        worker=WORKER_ROOT / "videodl_worker.py", request_id=uuid4().hex,
        timeout_seconds=timeout_seconds)
    if not any(item.backend_id == options["backend_id"]
               and item.backend_category == options.get("backend_category", "dedicated")
               and item.controlled_available for item in inventory):
        raise ProviderLabError("PROVIDER_POLICY_REJECTED")


def authorized_common_options(source_url, revision, backend_id):
    """Re-read trusted persistence. Caller-supplied options cannot grant sharing."""
    from .upstream_routes import read_route, UpstreamRouteError
    from dataclasses import replace
    try:
        route = read_route(source_url)
        stored = route["provider_options"].get("videodl_repo", {})
        grant = stored.get("sharing_authorization", {})
        options = normalize_saved_options({k: v for k, v in stored.items() if k != "sharing_authorization"})
        expected = confirmation_scope(route["source_host"], options)
        valid = (type(revision) is int and revision > 0 and route["revision"] == revision
                 and route["applicable"] and route["active"]
                 and "videodl_repo" in route["provider_order"]
                 and options.get("backend_category") == "common" and options["backend_id"] == backend_id
                 and isinstance(grant, dict) and all(grant.get(k) == v for k, v in expected.items())
                 and grant.get("revision") == revision and grant.get("policy_revision") == VIDEODL_POLICY_REVISION
                 and isinstance(grant.get("granted_at"), str) and bool(grant["granted_at"]))
    except (UpstreamRouteError, ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ProviderLabError("PROVIDER_POLICY_REJECTED")
    return replace(execution_options(options), external_sharing_confirmed=True)


def execution_options(value):
    """The single route-to-Worker parameter mapping (authorization checked separately)."""
    from .provider_lab.videodl_control import VideodlExecutionOptions
    options = normalize_saved_options(value)
    if options["mode"] == "dedicated_only":
        return VideodlExecutionOptions(mode="dedicated_only", backend_category="dedicated")
    return VideodlExecutionOptions(mode="specific_backend", backend_ids=(options["backend_id"],),
        backend_category=options.get("backend_category", "dedicated"))
