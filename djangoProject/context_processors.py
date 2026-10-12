import os
from functools import lru_cache
from pathlib import Path

from django.conf import settings


# Inventory first-party script/style files at process startup. Keep the existing
# version scheme, but do not require every new page script to be added by hand.
_STATIC_SOURCE_ROOT = Path(__file__).resolve().parent.parent
STATIC_VERSION_FILES = (
    "static/brand/favicon.svg",
    "static/brand/favicon-32.png",
    "static/brand/apple-touch-icon.png",
    *sorted(
        path.relative_to(_STATIC_SOURCE_ROOT).as_posix()
        for directory, pattern in (("static/js", "*.js"), ("static/css", "*.css"))
        for path in (_STATIC_SOURCE_ROOT / directory).rglob(pattern)
        if path.is_file()
    ),
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
    from xhs_download.context_processors import batch_parse_visibility

    return {
        "static_asset_version": get_static_asset_version(),
        **batch_parse_visibility(request),
    }
