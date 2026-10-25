"""Fetch/map composition for unified-pipeline sections 2.1 and 9."""
from abc import ABC, abstractmethod
import math
import time
from typing import Generic, Protocol, TypeVar, runtime_checkable

from ..pipeline import Failure, FailureKind, Work


Raw = TypeVar("Raw")


@runtime_checkable
class RawMapper(Protocol[Raw]):
    name: str
    platforms: tuple[str, ...]

    def map(self, raw: Raw, source_url: str) -> Work | Failure: ...


@runtime_checkable
class ProviderAdapter(RawMapper[Raw], Protocol[Raw]):
    def fetch(self, source_url: str, budget: float) -> Raw | Failure: ...

    def parse(self, source_url: str, budget: float) -> Work | Failure: ...


class Adapter(ABC, Generic[Raw]):
    """Family implementations own fetch and pure map; the kernel owns decisions.

    ``fetch`` reuses an existing client and its bounded timeout, without retries
    or new scheduling here. ``map`` must have no I/O or clock dependency. Callers
    use the existing kernel ``invoke`` to contain unexpected exceptions.
    """

    name: str
    platforms: tuple[str, ...]

    @abstractmethod
    def fetch(self, source_url: str, budget: float) -> Raw | Failure:
        """Return raw upstream facts, never a legacy result/candidate/token."""

    @abstractmethod
    def map(self, raw: Raw, source_url: str) -> Work | Failure:
        """Map facts only; routing fills provider and platform on the Work."""

    def parse(self, source_url: str, budget: float) -> Work | Failure:
        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        deadline = time.monotonic() + budget
        raw = self.fetch(source_url, budget)
        if time.monotonic() >= deadline:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        if isinstance(raw, Failure):
            return raw
        outcome = self.map(raw, source_url)
        if time.monotonic() >= deadline:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        if not isinstance(outcome, (Work, Failure)):
            return Failure(FailureKind.INVALID_RESULT, "ADAPTER_INVALID_OUTPUT")
        return outcome
