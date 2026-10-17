"""Small configuration API for explicit Human route saves and resets."""

import json

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .services.upstream_routes import UpstreamRouteError, read_route, write_route, videodl_route_availability


def _source_selector(data):
    keys = {"source_url", "source_host"} & set(data)
    if len(keys) != 1:
        raise UpstreamRouteError("INVALID_SOURCE_SCOPE")
    key = keys.pop()
    if not isinstance(data[key], str) or not data[key]:
        raise UpstreamRouteError("INVALID_SOURCE_HOST" if key == "source_host" else "INVALID_SOURCE")
    return {key: data[key]}


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST", "DELETE"])
def upstream_routes_view(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_active and user.is_staff):
        return JsonResponse({"ok": False, "error_code": "FORBIDDEN"}, status=403)
    if request.method != "GET" and not (user.is_superuser or user.has_perm("xhs_download.change_upstreamroute")):
        return JsonResponse({"ok": False, "error_code": "CONFIGURATION_PERMISSION_REQUIRED"}, status=403)
    try:
        if request.method == "GET":
            if any(len(request.GET.getlist(key)) != 1 for key in ("source_url", "source_host")
                   if key in request.GET):
                raise UpstreamRouteError("INVALID_SOURCE_SCOPE")
            source = _source_selector(request.GET)
            route = read_route(**source)
        else:
            if len(request.body) > 8192:
                raise UpstreamRouteError("REQUEST_TOO_LARGE")
            data = json.loads(request.body)
            allowed = {"source_url", "source_host", "expected_revision"}
            if request.method == "POST":
                allowed |= {"provider_order", "provider_options", "format_preferences", "reference_execution_id",
                            "sharing_confirmation"}
            if not isinstance(data, dict) or set(data) - allowed:
                raise UpstreamRouteError("INVALID_REQUEST")
            source = _source_selector(data)
            route = write_route(
                **source, expected_revision=data.get("expected_revision"), actor=user,
                provider_order=data.get("provider_order"), provider_options=data.get("provider_options"),
                format_preferences=data.get("format_preferences"), reset=request.method == "DELETE",
                reference_execution_id=data.get("reference_execution_id"),
                sharing_confirmation=data.get("sharing_confirmation"),
            )
        host_only = "source_host" in source
        route["scope_type"] = "exact_host"
        route["source_kind"] = "host" if host_only else "url"
        route["limitations"] = ["content_type_routing_not_supported"]
        if host_only:
            route["limitations"] += ["work_reference_not_provided", "provider_availability_not_checked"]
            if "videodl_repo" in route["effective_provider_order"]:
                route["provider_availability"] = {"videodl_repo": {
                    "status": "unknown", "reason": "SOURCE_URL_REQUIRED"}}
        elif "videodl_repo" in route["provider_order"]:
            availability = videodl_route_availability(source["source_url"], route)
            route["provider_availability"] = {"videodl_repo": {
                key: availability[key] for key in ("status", "reason")}}
        return JsonResponse({"ok": True, "route": route})
    except UpstreamRouteError as error:
        return JsonResponse({"ok": False, "error_code": error.code}, status=error.status)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error_code": "INVALID_REQUEST"}, status=400)
