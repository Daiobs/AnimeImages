import os
from functools import lru_cache

from django.conf import settings


STATIC_VERSION_FILES = (
    "static/css/app.css",
    "static/js/app.js",
    "static/js/batch_parse.js",
    "static/js/download_reliability.js",
    "static/js/xhs_download.js",
)


@lru_cache(maxsize=1)
def production_static_asset_version():
    return os.environ.get("APP_STATIC_VERSION", "1")


def source_static_asset_version():
    mtimes = []
    for relative_path in STATIC_VERSION_FILES:
        try:
            mtimes.append(int((settings.BASE_DIR / relative_path).stat().st_mtime))
        except OSError:
            continue
    if mtimes:
        return str(max(mtimes))
    return ""


def get_static_asset_version():
    env_version = os.environ.get("APP_STATIC_VERSION")
    source_version = source_static_asset_version()
    if env_version:
        return f"{env_version}-{source_version}" if source_version else env_version

    if source_version:
        return source_version

    return production_static_asset_version()


def static_asset_version(request):
    return {
        "static_asset_version": get_static_asset_version(),
    }
