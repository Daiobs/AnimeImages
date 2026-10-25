"""Safe failure mappings, without upstream messages or Provider-specific policy."""
from collections.abc import Mapping

from ..pipeline import Failure, FailureKind


def failure_for_http(status: int) -> Failure | None:
    """Map a final HTTP status; successful responses still require a valid map."""
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError("INVALID_HTTP_STATUS")
    if 200 <= status < 300:
        return None
    if status in (404, 410):
        kind = FailureKind.NOT_FOUND
    elif status == 408:
        kind = FailureKind.TIMEOUT
    elif status in (401, 403, 412, 429) or status >= 500:
        kind = FailureKind.UNAVAILABLE
    else:
        kind = FailureKind.INVALID_RESULT
    return Failure(kind, f"HTTP_{status}")


def failure_for_code(code: str | int, rules: Mapping[str, Failure]) -> Failure:
    """Map an already-confirmed business failure, never a success/malformed body.

    A family can distinguish unsupported/deleted results in its table. Unknown
    business errors remain unavailable; raw response text is never a diagnostic.
    """
    return rules.get(str(code), Failure(FailureKind.UNAVAILABLE, "UNMAPPED_UPSTREAM_CODE"))
