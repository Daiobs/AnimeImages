import os
from functools import lru_cache

from django.conf import settings


STATIC_VERSION_FILES = (
    "static/css/app.css",
    "static/js/app.js",
    "static/js/xhs_download.js",
)


@lru_cache(maxsize=1)
def production_static_asset_version():
    return os.environ.get("APP_STATIC_VERSION", "1")


def get_static_asset_version():
    env_version = os.environ.get("APP_STATIC_VERSION")
    if env_version:
        return env_version

    if settings.DEBUG:
        mtimes = []
        for relative_path in STATIC_VERSION_FILES:
            try:
                mtimes.append(int((settings.BASE_DIR / relative_path).stat().st_mtime))
            except OSError:
                continue
        if mtimes:
            return str(max(mtimes))

    return production_static_asset_version()


def static_asset_version(request):
    return {
        "static_asset_version": get_static_asset_version(),
    }
