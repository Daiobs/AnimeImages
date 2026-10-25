"""Unified pipeline kernel (docs/architecture/unified-pipeline.md).

Not wired into any request path yet: Step 2 maps Providers onto it and
compares it with the current paths; Step 3 switches each platform over.
"""
from .chain import (
    COOLDOWN_CEILING_SECONDS,
    Attempt,
    ChainConfig,
    ChainRun,
    CooldownBook,
    Policy,
    Start,
    Step,
    invoke,
)
from .delivery import BLOB_LIMIT_BYTES, Delivery, Method, decide_archive, decide_delivery
from .failures import Failure, FailureKind
from .filenames import (
    MAX_BASE_LENGTH,
    UnknownExtension,
    archive_name,
    base_name,
    extension,
    file_name,
    item_numbers,
)
from .model import (
    DEFAULT_QUALITY_LABEL,
    Completeness,
    Fetchable,
    Group,
    Item,
    Kind,
    Tracks,
    Variant,
    Work,
    fact_gaps,
    field_coverage,
    height_from_label,
    validate_work,
)
