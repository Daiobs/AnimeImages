"""Small configuration API for explicit Human route saves and resets."""

import json

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from .services.upstream_routes import UpstreamRouteError, read_route, write_route, videodl_route_availability


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
            source_url = request.GET.get("source_url", "")
            route = read_route(source_url)
        else:
            if len(request.body) > 8192:
                raise UpstreamRouteError("REQUEST_TOO_LARGE")
            data = json.loads(request.body)
            allowed = {"source_url", "expected_revision"}
            if request.method == "POST":
                allowed |= {"provider_order", "provider_options", "format_preferences"}
            if not isinstance(data, dict) or set(data) - allowed:
                raise UpstreamRouteError("INVALID_REQUEST")
            source_url = data.get("source_url", "")
            route = write_route(
                source_url, expected_revision=data.get("expected_revision"), actor=user,
                provider_order=data.get("provider_order"), provider_options=data.get("provider_options"),
                format_preferences=data.get("format_preferences"), reset=request.method == "DELETE",
            )
        if "videodl_repo" in route["provider_order"]:
            availability = videodl_route_availability(source_url, route)
            route["provider_availability"] = {"videodl_repo": {
                key: availability[key] for key in ("status", "reason")}}
        return JsonResponse({"ok": True, "route": route})
    except UpstreamRouteError as error:
        return JsonResponse({"ok": False, "error_code": error.code}, status=error.status)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error_code": "INVALID_REQUEST"}, status=400)
