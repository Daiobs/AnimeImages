from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    LabProviderAttemptV1,
    MAX_ASSETS,
    MAX_ATTEMPTS,
    ProviderLabError,
    safe_int,
    safe_public_text,
)
from .runtime import execute_provider_worker


VIDEODL_MODES = frozenset(
    {
        "legacy_auto",
        "specific_backend",
        "dedicated_only",
        "common_only",
        "reviewed_auto",
    }
)
BLOCKED_VIDEODL_MODES = frozenset(
    {"grabber", "grabber_only", "static_grabber", "browser_observer"}
)
BACKEND_CATEGORIES = frozenset({"dedicated", "common"})
BACKEND_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
MAX_BACKEND_IDS = 8
MAX_INVENTORY_ITEMS = 128
MAX_MATCHED_DEDICATED = 8
DEFAULT_TOTAL_TIMEOUT_SECONDS = 25.0
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 8.0
DEFAULT_DESCRIBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class VideodlExecutionOptions:
    mode: str
    backend_ids: tuple[str, ...] = ()
    backend_category: str = ""
    external_sharing_confirmed: bool = False


@dataclass(frozen=True)
class VideodlBackendDescriptor:
    backend_id: str
    backend_category: str
    available: bool
    external_sharing_required: bool
    display_label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "backend_category": self.backend_category,
            "available": self.available,
            "external_sharing_required": self.external_sharing_required,
            "display_label": self.display_label,
        }


@dataclass(frozen=True)
class VideodlControlledResult:
    result: dict[str, Any]
    resources: list[dict[str, Any]]
    attempts: tuple[LabProviderAttemptV1, ...]


class VideodlControlledError(ProviderLabError):
    def __init__(self, code: str, attempts: Sequence[LabProviderAttemptV1] = ()):
        self.attempts = tuple(attempts)[:MAX_ATTEMPTS]
        super().__init__(code)


WorkerInvoker = Callable[..., dict[str, Any]]


def parse_videodl_options(value: Any) -> VideodlExecutionOptions:
    if not isinstance(value, Mapping) or set(value) != {
        "mode",
        "backend_ids",
        "backend_category",
        "external_sharing_confirmed",
    }:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    mode = str(value.get("mode") or "")
    if mode in BLOCKED_VIDEODL_MODES:
        raise ProviderLabError("PROVIDER_POLICY_REJECTED")
    if mode not in VIDEODL_MODES:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    raw_ids = value.get("backend_ids")
    confirmation = value.get("external_sharing_confirmed")
    category = str(value.get("backend_category") or "")
    if not isinstance(raw_ids, list) or type(confirmation) is not bool:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    if len(raw_ids) > MAX_BACKEND_IDS:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    backend_ids = tuple(str(item or "") for item in raw_ids)
    if (
        len(set(backend_ids)) != len(backend_ids)
        or any(not BACKEND_ID_RE.fullmatch(item) for item in backend_ids)
    ):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")

    if mode == "legacy_auto":
        valid = not backend_ids and not category and not confirmation
    elif mode == "specific_backend":
        valid = (
            len(backend_ids) == 1
            and category in BACKEND_CATEGORIES
            and confirmation == (category == "common")
        )
    elif mode == "dedicated_only":
        valid = not backend_ids and category == "dedicated" and not confirmation
    elif mode == "common_only":
        valid = bool(backend_ids) and category == "common" and confirmation
    else:
        valid = (
            category == "common"
            and confirmation == bool(backend_ids)
        )
    if not valid:
        code = (
            "PROVIDER_POLICY_REJECTED"
            if mode in {"specific_backend", "common_only", "reviewed_auto"}
            and (category == "common" or backend_ids)
            and not confirmation
            else "PROVIDER_SCHEMA_INVALID"
        )
        raise ProviderLabError(code)
    return VideodlExecutionOptions(
        mode=mode,
        backend_ids=backend_ids,
        backend_category=category,
        external_sharing_confirmed=confirmation,
    )


def normalize_inventory(value: Any) -> tuple[VideodlBackendDescriptor, ...]:
    if not isinstance(value, list) or len(value) > MAX_INVENTORY_ITEMS:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    inventory = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "backend_id",
            "backend_category",
            "available",
            "external_sharing_required",
            "display_label",
        }:
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        backend_id = str(item.get("backend_id") or "")
        category = str(item.get("backend_category") or "")
        available = item.get("available")
        sharing = item.get("external_sharing_required")
        identity = (category, backend_id)
        if (
            not BACKEND_ID_RE.fullmatch(backend_id)
            or category not in BACKEND_CATEGORIES
            or type(available) is not bool
            or type(sharing) is not bool
            or sharing != (category == "common")
            or identity in seen
        ):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        seen.add(identity)
        inventory.append(
            VideodlBackendDescriptor(
                backend_id=backend_id,
                backend_category=category,
                available=available,
                external_sharing_required=sharing,
                display_label=safe_public_text(item.get("display_label"), 80) or backend_id,
            )
        )
    if not inventory:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    return tuple(inventory)


def load_videodl_inventory(
    *,
    python: str,
    worker: str | Path,
    request_id: str,
    timeout_seconds: float = DEFAULT_DESCRIBE_TIMEOUT_SECONDS,
    invoke: WorkerInvoker = execute_provider_worker,
) -> tuple[VideodlBackendDescriptor, ...]:
    payload = invoke(
        python=python,
        worker=worker,
        request_id=request_id,
        source="",
        timeout_seconds=timeout_seconds,
        operation="inventory",
    )
    return normalize_inventory(payload.get("inventory"))


def _remaining_timeout(deadline: float, cap: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining < 0.2:
        raise VideodlControlledError("PROVIDER_TIMEOUT")
    return min(cap, remaining)


def _attempt_failure(error: ProviderLabError) -> tuple[str, str]:
    return {
        "PROVIDER_UNSUPPORTED": ("unsupported", "BACKEND_UNSUPPORTED"),
        "PROVIDER_RESULT_EMPTY": ("empty", "BACKEND_EMPTY"),
        "PROVIDER_TIMEOUT": ("timeout", "BACKEND_TIMEOUT"),
        "PROVIDER_SCHEMA_INVALID": ("failed", "BACKEND_SCHEMA_INVALID"),
        "PROVIDER_INVALID_JSON": ("failed", "BACKEND_SCHEMA_INVALID"),
        "PROVIDER_OUTPUT_TOO_LARGE": ("failed", "BACKEND_SCHEMA_INVALID"),
        "PROVIDER_POLICY_REJECTED": ("policy_rejected", "BACKEND_POLICY_REJECTED"),
    }.get(error.code, ("failed", "BACKEND_PROCESS_FAILED"))


def _final_error_code(attempts: Sequence[LabProviderAttemptV1]) -> str:
    outcomes = {attempt.outcome for attempt in attempts}
    if "timeout" in outcomes:
        return "PROVIDER_TIMEOUT"
    if outcomes and outcomes <= {"unsupported"}:
        return "PROVIDER_UNSUPPORTED"
    if outcomes and outcomes <= {"empty", "unsupported"} and "empty" in outcomes:
        return "PROVIDER_RESULT_EMPTY"
    return "PROVIDER_PROCESS_FAILED"


def execute_controlled_videodl(
    *,
    python: str,
    worker: str | Path,
    request_id: str,
    source: str,
    options: VideodlExecutionOptions,
    total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    attempt_timeout_seconds: float = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
    invoke: WorkerInvoker = execute_provider_worker,
    clock: Callable[[], float] = time.monotonic,
) -> VideodlControlledResult:
    started_at = clock()
    deadline = started_at + min(60.0, max(1.0, float(total_timeout_seconds)))
    if options.mode == "legacy_auto":
        payload = invoke(
            python=python,
            worker=worker,
            request_id=request_id,
            source=source,
            timeout_seconds=_remaining_timeout(deadline, attempt_timeout_seconds, clock),
            operation="legacy_auto",
        )
        result = payload.get("result")
        resources = payload.get("resources")
        if not isinstance(result, dict) or not isinstance(resources, list):
            raise VideodlControlledError("PROVIDER_SCHEMA_INVALID")
        result["warnings"] = list(result.get("warnings") or ()) + [
            "control_level_opaque_legacy"
        ]
        return VideodlControlledResult(result=result, resources=resources, attempts=())

    inventory = load_videodl_inventory(
        python=python,
        worker=worker,
        request_id=request_id,
        timeout_seconds=_remaining_timeout(deadline, DEFAULT_DESCRIBE_TIMEOUT_SECONDS, clock),
        invoke=invoke,
    )
    available = {
        (item.backend_category, item.backend_id): item
        for item in inventory
        if item.available
    }
    for backend_id in options.backend_ids:
        if (options.backend_category, backend_id) not in available:
            raise VideodlControlledError("PROVIDER_POLICY_REJECTED")

    dedicated_ids: list[str] = []
    common_ids: list[str] = []
    if options.mode == "specific_backend":
        if options.backend_category == "dedicated":
            dedicated_ids = list(options.backend_ids)
        else:
            common_ids = list(options.backend_ids)
    elif options.mode in {"dedicated_only", "reviewed_auto"}:
        payload = invoke(
            python=python,
            worker=worker,
            request_id=request_id,
            source=source,
            timeout_seconds=_remaining_timeout(deadline, DEFAULT_DESCRIBE_TIMEOUT_SECONDS, clock),
            operation="match_dedicated",
        )
        matched = payload.get("backend_ids")
        if not isinstance(matched, list):
            raise VideodlControlledError("PROVIDER_SCHEMA_INVALID")
        for backend_id in matched[:MAX_MATCHED_DEDICATED]:
            if not isinstance(backend_id, str) or (
                "dedicated",
                backend_id,
            ) not in available:
                raise VideodlControlledError("PROVIDER_SCHEMA_INVALID")
            if backend_id not in dedicated_ids:
                dedicated_ids.append(backend_id)
    if options.mode in {"common_only", "reviewed_auto"}:
        common_ids = list(options.backend_ids)

    attempts: list[LabProviderAttemptV1] = []

    def run_attempt(backend_id: str, category: str) -> VideodlControlledResult | None:
        if len(attempts) >= MAX_ATTEMPTS:
            return None
        timeout_seconds = _remaining_timeout(deadline, attempt_timeout_seconds, clock)
        attempt_started = clock()
        try:
            payload = invoke(
                python=python,
                worker=worker,
                request_id=request_id,
                source=source,
                timeout_seconds=timeout_seconds,
                operation="backend",
                backend_id=backend_id,
                backend_category=category,
            )
            result = payload.get("result")
            resources = payload.get("resources")
            metrics = payload.get("metrics")
            candidate_count = (
                safe_int(metrics.get("candidate_count"))
                if isinstance(metrics, Mapping)
                else 0
            )
            if (
                not isinstance(result, dict)
                or not isinstance(resources, list)
                or not 1 <= candidate_count <= MAX_ASSETS
            ):
                raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        except ProviderLabError as error:
            outcome, failure_code = _attempt_failure(error)
            attempts.append(
                LabProviderAttemptV1(
                    ordinal=len(attempts) + 1,
                    backend_id=backend_id,
                    backend_category=category,
                    outcome=outcome,
                    elapsed_ms=int((clock() - attempt_started) * 1000),
                    safe_failure_code=failure_code,
                    external_sharing=category == "common",
                )
            )
            return None
        attempts.append(
            LabProviderAttemptV1(
                ordinal=len(attempts) + 1,
                backend_id=backend_id,
                backend_category=category,
                outcome="success",
                elapsed_ms=int((clock() - attempt_started) * 1000),
                candidate_count=candidate_count,
                selected=True,
                external_sharing=category == "common",
            )
        )
        result["selected_backend"] = backend_id
        result["warnings"] = list(result.get("warnings") or ()) + [
            f"controlled_mode_{options.mode}"
        ]
        return VideodlControlledResult(
            result=result,
            resources=resources,
            attempts=tuple(attempts),
        )

    for backend_id in dedicated_ids:
        result = run_attempt(backend_id, "dedicated")
        if result is not None:
            return result
    for backend_id in common_ids:
        result = run_attempt(backend_id, "common")
        if result is not None:
            return result

    if not attempts:
        code = (
            "PROVIDER_UNSUPPORTED"
            if options.mode in {"dedicated_only", "reviewed_auto"}
            else "PROVIDER_POLICY_REJECTED"
        )
        raise VideodlControlledError(code)
    raise VideodlControlledError(_final_error_code(attempts), attempts)


def public_inventory(items: Sequence[VideodlBackendDescriptor]) -> list[dict[str, Any]]:
    return [item.as_dict() for item in tuple(items)[:MAX_INVENTORY_ITEMS]]
