"""Admin-configured Provider chains (docs/architecture/unified-pipeline.md §2.3).

ChainRun is the only implementation of the routing policies. It never calls a
Provider or sleeps: a scheduler starts the Providers it names, reports each
outcome and the passing of time, and shows what it returns. The same events in
the same order always give the same decisions.
"""
from dataclasses import dataclass, replace
from enum import Enum

from .failures import Failure, FailureKind
from .model import Completeness, Work, validate_work


class Policy(str, Enum):
    FIRST_SUCCESS = "first_success"
    FLOOR_AND_UPGRADE = "floor_and_upgrade"


# INV-02: whatever the admin configures, no cooldown outlasts this.
COOLDOWN_CEILING_SECONDS = 3600


@dataclass(frozen=True)
class ChainConfig:
    platform: str
    order: tuple[str, ...]
    policy: Policy = Policy.FIRST_SUCCESS
    budget_seconds: float = 5.0
    floor_provider: str = ""
    upgrade_window_seconds: float = 0.0
    platform_names: tuple[str, ...] = ()

    def __post_init__(self):
        if self.budget_seconds <= 0:
            raise ValueError("budget_seconds must be positive")
        if len(set(self.order)) != len(self.order):
            raise ValueError("order lists a provider twice")
        if self.policy is Policy.FLOOR_AND_UPGRADE:
            if not self.floor_provider:
                raise ValueError("floor_and_upgrade needs floor_provider")
            if self.upgrade_window_seconds < self.budget_seconds:
                raise ValueError("upgrade_window_seconds must cover budget_seconds")


class CooldownBook:
    """Bounded cooldown after `unavailable`; expiry needs no admin action."""

    def __init__(self, *, base_seconds, max_seconds):
        self.max_seconds = min(max_seconds, COOLDOWN_CEILING_SECONDS)
        self.base_seconds = min(base_seconds, self.max_seconds)
        self._streak = {}
        self._until = {}

    def record_unavailable(self, provider, now):
        streak = self._streak.get(provider, 0) + 1
        self._streak[provider] = streak
        seconds = min(self.base_seconds * 2 ** min(streak - 1, 32), self.max_seconds)
        self._until[provider] = now + seconds
        return seconds

    def record_success(self, provider):
        self._streak.pop(provider, None)
        self._until.pop(provider, None)

    def cooling(self, provider, now):
        return self._until.get(provider, 0) > now

    def snapshot(self, now):
        """What the control plane shows: provider -> cooldown end."""
        return {provider: until for provider, until in self._until.items() if until > now}


@dataclass(frozen=True)
class Start:
    provider: str
    budget_seconds: float


@dataclass(frozen=True)
class Attempt:
    provider: str
    outcome: str  # "success", "cooling_down" or a FailureKind value
    code: str = ""


@dataclass(frozen=True)
class Step:
    start: tuple[Start, ...] = ()
    show: Work | None = None
    done: bool = False
    failure: Failure | None = None


def invoke(provider, source_url, budget_seconds):
    """Call one adapter; an exception becomes invalid_result."""
    try:
        outcome = provider.parse(source_url, budget_seconds)
    except Exception:
        return Failure(FailureKind.INVALID_RESULT, "ADAPTER_EXCEPTION")
    return outcome


class ChainRun:
    def __init__(self, config, cooldowns):
        self.config = config
        self.cooldowns = cooldowns
        self.attempts = []
        self.shown = None
        self.done = False
        self.failure = None
        self._started_at = None
        floor = config.floor_provider if config.policy is Policy.FLOOR_AND_UPGRADE else None
        self._pending = [p for p in config.order if p != floor]
        self._running = set()
        self._floor_work = None
        self._floor_settled = config.policy is Policy.FIRST_SUCCESS
        self._budget_expired = False
        self._out_of_time = False

    @property
    def _floor(self):
        return self.config.policy is Policy.FLOOR_AND_UPGRADE

    @property
    def running(self):
        """Providers still working; a scheduler cancels them once done."""
        return frozenset(self._running)

    @property
    def next_deadline(self):
        if self.done or self._started_at is None:
            return None
        if self._floor and self._budget_expired:
            return self._started_at + self.config.upgrade_window_seconds
        return self._started_at + self.config.budget_seconds

    def start(self, now):
        self._started_at = now
        starts = []
        if self._floor:
            floor = self.config.floor_provider
            if self.cooldowns.cooling(floor, now):
                self.attempts.append(Attempt(floor, "cooling_down"))
                self._floor_settled = True
            else:
                self._running.add(floor)
                starts.append(Start(floor, self.config.upgrade_window_seconds))
        starts.extend(self._start_next(now))
        if not starts:
            return self._finish(self._exhausted_failure())
        return Step(start=tuple(starts))

    def on_outcome(self, provider, outcome, now):
        outcome = self._normalize(provider, outcome)
        if isinstance(outcome, Work):
            self.cooldowns.record_success(provider)
        elif outcome.kind is FailureKind.UNAVAILABLE:
            self.cooldowns.record_unavailable(provider, now)
        if self.done or provider not in self._running:
            return Step()
        self._running.discard(provider)
        if isinstance(outcome, Work):
            self.attempts.append(Attempt(provider, "success"))
        else:
            self.attempts.append(Attempt(provider, outcome.kind.value, outcome.code))
        if self._floor and provider == self.config.floor_provider:
            return self._on_floor(outcome, now)
        return self._on_preferred(outcome, now)

    def on_time(self, now):
        if self.done or self._started_at is None:
            return Step()
        elapsed = now - self._started_at
        if not self._floor:
            if elapsed >= self.config.budget_seconds:
                return self._finish(Failure(FailureKind.TIMEOUT, "CHAIN_BUDGET_EXHAUSTED"))
            return Step()
        if elapsed >= self.config.upgrade_window_seconds:
            if self.shown is not None or self._floor_work is not None:
                return self._settle_on_floor()
            return self._finish(Failure(FailureKind.TIMEOUT, "CHAIN_WINDOW_EXHAUSTED"))
        if not self._budget_expired and elapsed >= self.config.budget_seconds:
            self._budget_expired = True
            if self._floor_work is not None:
                return self._show(self._floor_work, Completeness.PROVISIONAL)
        return Step()

    def _on_floor(self, outcome, now):
        self._floor_settled = True
        if isinstance(outcome, Work):
            self._floor_work = outcome
            if not self._pending and not self._running:
                return self._settle_on_floor()
            if self._budget_expired and self.shown is None:
                return self._show(outcome, Completeness.PROVISIONAL)
            return Step()
        if outcome.kind is FailureKind.NOT_FOUND:
            return self._finish(outcome)
        if not self._pending and not self._running:
            return self._finish(self._exhausted_failure())
        return Step()

    def _on_preferred(self, outcome, now):
        if isinstance(outcome, Work):
            if self.shown is not None and outcome.rank_key() <= self.shown.rank_key():
                # First come, first served: the first preferred result ends the
                # chain even when it does not beat what the page already shows.
                return self._settle_on_floor()
            return self._show(outcome, Completeness.FINAL, done=True)
        if outcome.kind is FailureKind.NOT_FOUND:
            if self._floor_work is not None:
                return self._settle_on_floor()
            return self._finish(outcome)
        starts = self._start_next(now)
        if starts:
            return Step(start=starts)
        if self._floor_work is not None:
            return self._settle_on_floor()
        if not self._floor_settled:
            return Step()
        return self._finish(self._exhausted_failure())

    def _start_next(self, now):
        remaining = self._remaining(now)
        while self._pending and remaining > 0:
            provider = self._pending.pop(0)
            if self.cooldowns.cooling(provider, now):
                self.attempts.append(Attempt(provider, "cooling_down"))
                continue
            self._running.add(provider)
            return (Start(provider, remaining),)
        if self._pending:
            self._out_of_time = True
            self._pending.clear()
        return ()

    def _remaining(self, now):
        limit = self.config.upgrade_window_seconds if self._floor else self.config.budget_seconds
        return limit - (now - self._started_at)

    def _normalize(self, provider, outcome):
        if isinstance(outcome, Failure):
            return outcome
        if not isinstance(outcome, Work):
            return Failure(FailureKind.INVALID_RESULT, "ADAPTER_RETURNED_UNKNOWN")
        work = replace(outcome, platform=self.config.platform, provider=provider)
        violations = validate_work(work, platform_names=self.config.platform_names)
        if violations:
            return Failure(FailureKind.INVALID_RESULT, f"INVALID_{violations[0].upper()}")
        return work

    def _show(self, work, completeness, *, done=False):
        self.shown = replace(work, completeness=completeness)
        if done:
            self.done = True
        return Step(show=self.shown, done=self.done)

    def _settle_on_floor(self):
        if self.shown is None:
            return self._show(self._floor_work, Completeness.FINAL, done=True)
        self.shown = replace(self.shown, completeness=Completeness.FINAL)
        self.done = True
        return Step(done=True)

    def _finish(self, failure):
        self.done = True
        self.failure = failure
        return Step(done=True, failure=failure)

    def _exhausted_failure(self):
        if self._out_of_time:
            return Failure(FailureKind.TIMEOUT, "CHAIN_BUDGET_EXHAUSTED")
        ran = [a for a in self.attempts if a.outcome != "cooling_down"]
        if not ran:
            return Failure(FailureKind.UNAVAILABLE, "ALL_PROVIDERS_COOLING")
        if all(a.outcome == FailureKind.UNSUPPORTED.value for a in ran):
            return Failure(FailureKind.UNSUPPORTED, "NO_PROVIDER_SUPPORTS_URL")
        return Failure(FailureKind.UNAVAILABLE, "ALL_PROVIDERS_FAILED")
