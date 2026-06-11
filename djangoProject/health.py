import logging
import uuid

from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET


logger = logging.getLogger(__name__)


def check_cache():
    key = f"health:ready:{uuid.uuid4().hex}"
    cache.set(key, "ok", timeout=10)
    try:
        return cache.get(key) == "ok"
    finally:
        cache.delete(key)


def check_database():
    with connections["default"].cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    return bool(row and row[0] == 1)


def get_runtime_readiness():
    components = {}
    checks = {
        "cache": check_cache,
        "database": check_database,
    }
    for name, check in checks.items():
        try:
            components[name] = bool(check())
        except Exception:
            components[name] = False
            logger.exception(
                "runtime_readiness_check_failed",
                extra={"component": name},
            )
    return {
        "ready": all(components.values()),
        "components": components,
    }


@require_GET
def liveness_view(request):
    response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def readiness_view(request):
    readiness = get_runtime_readiness()
    response = JsonResponse(
        {
            "status": "ready" if readiness["ready"] else "unavailable",
            "components": readiness["components"],
        },
        status=200 if readiness["ready"] else 503,
    )
    response["Cache-Control"] = "no-store"
    return response
