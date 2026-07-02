import logging
import time
from dataclasses import dataclass, field

from ..models import SelfHealingEvent
from ..policy.healing_policy import (
    DEFAULT_MAX_HEALING_DEPTH,
    HEAL_ACTION_BLOCK_GRACEFULLY,
    HEAL_ACTION_DEGRADE_WAIT,
    HEAL_ACTION_FALLBACK_DIRECT,
    HEAL_ACTION_FALLBACK_PROXY,
    HEAL_ACTION_FALLBACK_ZIP,
    HEAL_ACTION_RETRY_PROVIDER,
    HEAL_ACTION_SWITCH_PROVIDER,
    decide_healing_policy,
    next_provider,
)
from .download_routing import ROUTE_BLOCKED, ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP
from .observability import request_privacy_fields, sanitize_observability_text, sanitize_observability_value
from .recovery_engine import classify_failure


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelfHealingOutcome:
    healed: bool
    action: str
    reason: str
    confidence: float
    risk: str
    failure_type: str
    trigger: str = ""
    provider: str = ""
    switched_from_provider: str = ""
    switched_to_provider: str = ""
    route_type: str = ""
    fallback_from_route: str = ""
    fallback_to_route: str = ""
    depth: int = 0
    max_depth: int = DEFAULT_MAX_HEALING_DEPTH
    duration_ms: int = 0
    error_type: str = ""
    event_id: int | None = None
    context_snapshot: dict = field(default_factory=dict)

    @property
    def provider_switched(self):
        return bool(
            self.switched_from_provider
            and self.switched_to_provider
            and self.switched_from_provider != self.switched_to_provider
        )

    def log_fields(self):
        return {
            "auto_recovery_count": 1 if self.healed else 0,
            "provider_auto_switch_count": 1 if self.provider_switched else 0,
            "failure_to_recovery_ms": self.duration_ms,
            "healing_action": self.action,
            "healing_risk": self.risk,
            "healing_confidence": self.confidence,
        }

    def metadata(self):
        return {
            **self.log_fields(),
            "healed": self.healed,
            "trigger": self.trigger,
            "failure_type": self.failure_type,
            "reason": self.reason,
            "provider": self.provider,
            "switched_from_provider": self.switched_from_provider,
            "switched_to_provider": self.switched_to_provider,
            "route_type": self.route_type,
            "fallback_from_route": self.fallback_from_route,
            "fallback_to_route": self.fallback_to_route,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "event_id": self.event_id,
        }


class SelfHealingEngine:
    def __init__(self, request=None, *, context=None, max_depth=DEFAULT_MAX_HEALING_DEPTH):
        self.request = request
        self.context = dict(context or {})
        self.max_depth = max(1, int(max_depth or DEFAULT_MAX_HEALING_DEPTH))

    def detect_failure(self, *, route_result=None, provider="", error=None, error_code=""):
        route_result = route_result or {}
        return {
            "route_type": route_result.get("route_type", ""),
            "provider": provider or route_result.get("provider", ""),
            "error": error,
            "error_code": error_code or route_result.get("error_code", ""),
        }

    def classify_failure(self, *, route_type="", provider="", error=None, error_code=""):
        return classify_failure(
            error,
            error_code=error_code,
            route_type=route_type,
            provider=provider,
        )

    def decide_recovery_action(
        self,
        *,
        failure_type,
        route_type="",
        provider="",
        depth=0,
        stability_score=None,
    ):
        return decide_healing_policy(
            failure_type=failure_type,
            route_type=route_type,
            provider=provider,
            depth=depth,
            stability_score=stability_score,
        )

    def execute_recovery(
        self,
        *,
        policy,
        provider="",
        route_type="",
        retry_callable=None,
        provider_switcher=None,
    ):
        switched_to = ""
        healed = policy.can_heal

        if policy.action == HEAL_ACTION_RETRY_PROVIDER and retry_callable:
            try:
                retry_callable()
                healed = True
            except Exception:
                healed = False

        if policy.action == HEAL_ACTION_SWITCH_PROVIDER:
            switched_to = next_provider(provider)
            if provider_switcher and switched_to:
                try:
                    provider_switcher(switched_to)
                    healed = True
                except Exception:
                    healed = False
            else:
                healed = bool(switched_to)

        if policy.action in {
            HEAL_ACTION_FALLBACK_ZIP,
            HEAL_ACTION_FALLBACK_PROXY,
            HEAL_ACTION_FALLBACK_DIRECT,
        }:
            healed = True

        if policy.action in {HEAL_ACTION_BLOCK_GRACEFULLY, HEAL_ACTION_DEGRADE_WAIT}:
            healed = False

        return healed, switched_to

    def record_healing_event(self, outcome):
        try:
            privacy = request_privacy_fields(self.request)
            event = SelfHealingEvent.objects.create(
                request_id=privacy["request_id"],
                client_identity_hash=privacy["client_identity_hash"],
                ip_hash=privacy["ip_hash"],
                is_staff=privacy["is_staff"],
                trigger=outcome.trigger[:64],
                failure_type=outcome.failure_type[:64],
                action=outcome.action[:80],
                risk=outcome.risk[:16],
                confidence=float(outcome.confidence or 0),
                healed=bool(outcome.healed),
                depth=max(0, int(outcome.depth or 0)),
                max_depth=max(1, int(outcome.max_depth or self.max_depth)),
                provider=outcome.provider[:80],
                switched_from_provider=outcome.switched_from_provider[:80],
                switched_to_provider=outcome.switched_to_provider[:80],
                route_type=outcome.route_type[:32],
                fallback_from_route=outcome.fallback_from_route[:32],
                fallback_to_route=outcome.fallback_to_route[:32],
                duration_ms=max(0, int(outcome.duration_ms or 0)),
                error_type=outcome.error_type[:120],
                reason=sanitize_observability_text(outcome.reason, limit=300),
                context_snapshot=sanitize_observability_value(outcome.context_snapshot),
            )
            return event
        except Exception:
            logger.exception("self_healing_event_write_failed")
            return None

    def handle_failure(
        self,
        *,
        trigger,
        route_result=None,
        provider="",
        error=None,
        error_code="",
        failure_type="",
        depth=0,
        stability_score=None,
        retry_callable=None,
        provider_switcher=None,
        force_healed=None,
    ):
        started_at = time.monotonic()
        detection = self.detect_failure(
            route_result=route_result,
            provider=provider,
            error=error,
            error_code=error_code,
        )
        route_type = detection["route_type"]
        provider = str(detection["provider"] or "")
        failure_type = str(failure_type or "") or self.classify_failure(
            route_type=route_type,
            provider=provider,
            error=error,
            error_code=detection["error_code"],
        )
        policy = self.decide_recovery_action(
            failure_type=failure_type,
            route_type=route_type,
            provider=provider,
            depth=depth,
            stability_score=stability_score,
        )
        healed, switched_to = self.execute_recovery(
            policy=policy,
            provider=provider,
            route_type=route_type,
            retry_callable=retry_callable,
            provider_switcher=provider_switcher,
        )
        if force_healed is not None:
            healed = bool(force_healed)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        fallback_from = route_type if policy.fallback_to_route else ""
        fallback_to = policy.fallback_to_route
        if policy.action == HEAL_ACTION_FALLBACK_ZIP:
            fallback_from = route_type or "routing"
            fallback_to = ROUTE_ZIP
        elif policy.action == HEAL_ACTION_FALLBACK_PROXY:
            fallback_from = route_type or ROUTE_ZIP
            fallback_to = ROUTE_PROXY
        elif policy.action == HEAL_ACTION_FALLBACK_DIRECT:
            fallback_from = route_type or ROUTE_PROXY
            fallback_to = ROUTE_DIRECT
        elif policy.action in {HEAL_ACTION_BLOCK_GRACEFULLY, HEAL_ACTION_DEGRADE_WAIT}:
            fallback_to = ROUTE_BLOCKED

        outcome = SelfHealingOutcome(
            healed=bool(healed),
            action=policy.action,
            reason=policy.reason,
            confidence=policy.confidence,
            risk=policy.risk,
            failure_type=failure_type,
            trigger=str(trigger or ""),
            provider=provider,
            switched_from_provider=provider if switched_to else "",
            switched_to_provider=switched_to,
            route_type=route_type,
            fallback_from_route=fallback_from,
            fallback_to_route=fallback_to,
            depth=max(0, int(depth or 0)),
            max_depth=self.max_depth,
            duration_ms=duration_ms,
            error_type=type(error).__name__ if error is not None else "",
            context_snapshot=self.context,
        )
        event = self.record_healing_event(outcome)
        if event is not None:
            outcome = SelfHealingOutcome(
                **{
                    **outcome.__dict__,
                    "event_id": event.pk,
                }
            )
        logger.info(
            "self_healing_event",
            extra={
                "trigger": outcome.trigger,
                "failure_type": outcome.failure_type,
                "action": outcome.action,
                "healed": outcome.healed,
                "provider": outcome.provider,
                "switched_to_provider": outcome.switched_to_provider,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome


def attach_healing_to_route_info(route_info, outcome):
    route_info = dict(route_info or {})
    route_info["healing"] = outcome.metadata()
    return route_info
