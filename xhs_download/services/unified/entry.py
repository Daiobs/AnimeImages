"""Where a parse request chooses between the unified path and the existing one."""
from functools import wraps

from ..common import XhsError
from . import routes


def route_for_url(url):
    """The switched-on route for the URL's platform, or None."""
    from ..registry import get_adapter_for_url

    try:
        platform = str(getattr(get_adapter_for_url(url), "platform_id", "") or "")
    except XhsError:
        return None
    return routes.load_route(platform)


def _douyin(url, route, **options):
    from .douyin import parse

    return parse(url, route, prefer_ultra_hd=bool(options.get("prefer_ultra_hd")),
                 attempt_controller=options.get("ultra_attempt_controller"))


def _xiaohongshu(url, route, **options):
    from .xiaohongshu import parse

    return parse(url, route, prefer_ultra_hd=bool(options.get("prefer_ultra_hd")),
                 attempt_controller=options.get("ultra_attempt_controller"),
                 service_ids=options.get("xhs_service_provider_ids") or ())


def _bilibili(url, route, **options):
    from .bilibili import parse

    return parse(url, route, authorize=options.get("bilibili_service_authorizer"))


# Each platform's Providers and facts live in its own module; this only registers them.
PLATFORM_PARSERS = {"douyin": _douyin, "xiaohongshu": _xiaohongshu, "bilibili": _bilibili}


def parse_unified(url, route, **options):
    return PLATFORM_PARSERS[route.platform](url, route, **options)


def with_unified(parser):
    """``parser`` unless the link's platform has switched to the unified path."""
    @wraps(parser)
    def parse(url, **options):
        route = route_for_url(url)
        if route is None:
            return parser(url, **options)
        return parse_unified(url, route, **options)
    return parse
