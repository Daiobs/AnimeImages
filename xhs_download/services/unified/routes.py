"""Admin routes for the unified pipeline (§2.3); the chain itself is ChainRun."""
import logging
import threading

from django.db import DatabaseError

from ..pipeline import ChainConfig, CooldownBook, Policy

logger = logging.getLogger(__name__)

# Platforms whose unified path exists (entry.PLATFORM_PARSERS); a switched-on
# row for any other platform is ignored rather than routed to nothing.
IMPLEMENTED_PLATFORMS = frozenset({"douyin"})

_cooldowns = {}
_cooldowns_lock = threading.Lock()


def load_route(platform):
    """The platform's route when its switch is on; otherwise None.

    A missing table (code deployed before migrate) or any read failure means
    off, so the existing path keeps serving.
    """
    if platform not in IMPLEMENTED_PLATFORMS:
        return None
    try:
        from ...models import PipelineRoute

        return PipelineRoute.objects.filter(platform=platform, enabled=True).first()
    except DatabaseError:
        logger.debug("unified_route_unavailable platform=%s", platform)
        return None
    except Exception as error:
        if type(error).__name__ == "DatabaseOperationForbidden":
            return None
        logger.exception("unified_route_load_failed platform=%s", platform)
        return None


def chain_config(route, available):
    """ChainConfig from the admin row, keeping the Providers this request may use.

    Without its floor or without any preferred Provider, floor_and_upgrade has
    nothing to race, so the remaining Providers run first_success and each gets
    the whole window.
    """
    order = tuple(name for name in route.order if name in available)
    if not order:
        raise ValueError("no available provider")
    policy = Policy(route.policy)
    window = max(float(route.upgrade_window_seconds), float(route.budget_seconds))
    names = tuple(str(name) for name in route.platform_names or ())
    if policy is Policy.FLOOR_AND_UPGRADE and route.floor_provider in order and len(order) > 1:
        return ChainConfig(platform=route.platform, order=order, policy=policy,
                           budget_seconds=float(route.budget_seconds), floor_provider=route.floor_provider,
                           upgrade_window_seconds=window, platform_names=names)
    return ChainConfig(platform=route.platform, order=order, budget_seconds=window, platform_names=names)


def cooldowns(route):
    """Per-process cooldowns (INV-02): bounded, and cleared by one success.

    Changing the route's cooldown settings starts a fresh book.
    """
    limits = (int(route.cooldown_base_seconds), int(route.cooldown_max_seconds))
    with _cooldowns_lock:
        held = _cooldowns.get(route.platform)
        if held is None or held[0] != limits:
            held = _cooldowns[route.platform] = (limits, CooldownBook(base_seconds=limits[0], max_seconds=limits[1]))
        return held[1]
