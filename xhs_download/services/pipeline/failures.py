"""The five Provider failure kinds (docs/architecture/unified-pipeline.md §2.2)."""
from dataclasses import dataclass
from enum import Enum
import re


class FailureKind(str, Enum):
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESULT = "invalid_result"


# Diagnostic codes are safe to log and show to staff: no URL, cookie or key fits.
SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    code: str

    def __post_init__(self):
        if not isinstance(self.kind, FailureKind):
            raise ValueError("unknown failure kind")
        if not SAFE_CODE.fullmatch(self.code or ""):
            raise ValueError("diagnostic code must match [A-Z][A-Z0-9_]{0,63}")
