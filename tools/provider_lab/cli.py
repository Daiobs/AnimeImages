#!/usr/bin/env python3
"""Offline commands for source lifecycle and Provider capability fixtures."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from tools.rich_media_contract.validate_contract import (
    ContractRejected,
    load_document,
    validate_document,
)
from xhs_download.services.source_lifecycle import (
    SourceInputKind,
    SourceLifecycleError,
    SourceParameterPolicy,
    build_source_resolution,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "source_cases.json"
DEFAULT_DELTA = PROJECT_ROOT / "docs" / "research" / "parsehub" / "existing_provider_delta.json"
ALLOWED_ACTIONS = {
    "ADOPT_NOW",
    "ADOPT_DORMANT",
    "REFERENCE_ONLY",
    "ALREADY_STRONGER",
    "SECURITY_REVIEW_REQUIRED",
    "DEMAND_DRIVEN",
    "REJECT_DIRECT",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _policy(value: Any) -> SourceParameterPolicy:
    if not isinstance(value, dict):
        raise ValueError("fixture policy must be an object")
    allowed = {
        "retain_for_parse",
        "retain_for_identity",
        "remove_after_parse",
        "always_drop",
    }
    if set(value) != allowed:
        raise ValueError("fixture policy fields are invalid")
    return SourceParameterPolicy(
        retain_for_parse=tuple(value["retain_for_parse"]),
        retain_for_identity=tuple(value["retain_for_identity"]),
        remove_after_parse=tuple(value["remove_after_parse"]),
        always_drop=tuple(value["always_drop"]),
    )


def _source_case_result(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case.get("case_id") or "")
    if not case_id or len(case_id) > 64:
        return {"case_id": "invalid_case", "outcome": "FAIL", "code": "FIXTURE_INVALID"}
    expected = case.get("expected")
    if not isinstance(expected, dict):
        return {"case_id": case_id, "outcome": "FAIL", "code": "FIXTURE_INVALID"}
    try:
        resolution = build_source_resolution(
            platform_id=case["platform_id"],
            input_kind=SourceInputKind(case["input_kind"]),
            input_value=case["input_value"],
            resolved_url=case.get("resolved_url") or "",
            canonical_identity=case["canonical_identity"],
            parameter_policy=_policy(case["parameter_policy"]),
            resolution_duration_ms=int(case.get("resolution_duration_ms") or 0),
        )
    except (KeyError, TypeError, ValueError, SourceLifecycleError) as error:
        code = error.code if isinstance(error, SourceLifecycleError) else "FIXTURE_INVALID"
        expected_code = expected.get("error_code")
        return {
            "case_id": case_id,
            "outcome": "PASS" if expected_code == code else "FAIL",
            "code": code,
        }

    safe = resolution.to_safe_diagnostic().to_structural_dict()
    expected_safe = expected.get("safe")
    if expected.get("error_code") or not isinstance(expected_safe, dict):
        return {"case_id": case_id, "outcome": "FAIL", "code": "EXPECTED_REJECTION"}
    matches = all(safe.get(key) == value for key, value in expected_safe.items())
    return {
        "case_id": case_id,
        "outcome": "PASS" if matches else "FAIL",
        "code": "OK" if matches else "SAFE_DIAGNOSTIC_MISMATCH",
    }


def validate_source_fixtures(path: Path = DEFAULT_SOURCE_FIXTURE) -> dict[str, Any]:
    document = _load_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != "1.0":
        raise ValueError("source fixture document is invalid")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("source fixture cases are invalid")
    results = [_source_case_result(case) for case in cases if isinstance(case, dict)]
    passed = sum(item["outcome"] == "PASS" for item in results)
    return {
        "status": "PASS" if passed == len(cases) == len(results) else "FAIL",
        "total": len(cases),
        "passed": passed,
        "failed_case_ids": [
            item["case_id"] for item in results if item["outcome"] != "PASS"
        ],
        "accepted": sum(
            item["code"] == "OK" and item["outcome"] == "PASS" for item in results
        ),
        "rejected": sum(
            item["code"] != "OK" and item["outcome"] == "PASS" for item in results
        ),
    }


def inventory() -> dict[str, Any]:
    return {
        "lab": "animeimages_provider_lab_v1",
        "network_enabled": False,
        "parsehub_imported": False,
        "production_environment_access": False,
        "commands": [
            "inventory",
            "validate-source-fixture",
            "redact-source",
            "compare-provider-capability",
            "validate-rich-media",
            "summarize-delta",
        ],
    }


def redact_source(args: argparse.Namespace) -> dict[str, Any]:
    policy_document = json.loads(args.parameter_policy)
    resolution = build_source_resolution(
        platform_id=args.platform_id,
        input_kind=SourceInputKind(args.input_kind),
        input_value=args.source,
        resolved_url=args.resolved_url or "",
        canonical_identity=args.canonical_identity,
        parameter_policy=_policy(policy_document),
        resolution_duration_ms=args.resolution_duration_ms,
    )
    return resolution.to_safe_diagnostic().to_structural_dict()


def _load_delta(path: Path) -> dict[str, Any]:
    document = _load_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != "1.0":
        raise ValueError("capability delta document is invalid")
    capabilities = document.get("capabilities")
    if not isinstance(capabilities, list):
        raise ValueError("capability delta list is invalid")
    for item in capabilities:
        if not isinstance(item, dict) or item.get("recommended_action") not in ALLOWED_ACTIONS:
            raise ValueError("capability delta action is invalid")
    return document


def compare_provider_capability(path: Path, capability_id: str) -> dict[str, Any]:
    document = _load_delta(path)
    item = next(
        (
            capability
            for capability in document["capabilities"]
            if capability.get("capability_id") == capability_id
        ),
        None,
    )
    if item is None:
        raise ValueError("capability was not found")
    return {
        "capability_id": item["capability_id"],
        "recommended_action": item["recommended_action"],
        "selected_pr": item["selected_pr"],
        "status": item["status"],
        "adoption_value": item["adoption_value"],
    }


def summarize_delta(path: Path) -> dict[str, Any]:
    document = _load_delta(path)
    action_counts = Counter(
        item["recommended_action"] for item in document["capabilities"]
    )
    status_counts = Counter(item["status"] for item in document["capabilities"])
    return {
        "capability_count": len(document["capabilities"]),
        "action_counts": dict(sorted(action_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
    }


def validate_rich_media(paths: list[Path]) -> dict[str, Any]:
    results = []
    for index, path in enumerate(paths):
        try:
            result = validate_document(load_document(path))
        except ContractRejected as error:
            result = error.result
        results.append(
            {
                "case": index,
                "status": result.status,
                "code": result.code,
                "path": result.path,
            }
        )
    return {
        "status": "PASS" if all(item["status"] == "VALID" for item in results) else "FAIL",
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AnimeImages offline Provider Lab v1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory")

    source_fixture = subparsers.add_parser("validate-source-fixture")
    source_fixture.add_argument("path", nargs="?", type=Path, default=DEFAULT_SOURCE_FIXTURE)

    redact = subparsers.add_parser("redact-source")
    redact.add_argument("--platform-id", required=True)
    redact.add_argument(
        "--input-kind",
        choices=[item.value for item in SourceInputKind],
        required=True,
    )
    redact.add_argument("--source", required=True)
    redact.add_argument("--resolved-url", default="")
    redact.add_argument("--canonical-identity", required=True)
    redact.add_argument(
        "--parameter-policy",
        required=True,
        help="JSON object containing the four closed parameter-policy lists",
    )
    redact.add_argument("--resolution-duration-ms", type=int, default=0)

    compare = subparsers.add_parser("compare-provider-capability")
    compare.add_argument("capability_id")
    compare.add_argument("--delta", type=Path, default=DEFAULT_DELTA)

    validate_media = subparsers.add_parser("validate-rich-media")
    validate_media.add_argument("documents", nargs="+", type=Path)

    summary = subparsers.add_parser("summarize-delta")
    summary.add_argument("--delta", type=Path, default=DEFAULT_DELTA)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory()
        elif args.command == "validate-source-fixture":
            result = validate_source_fixtures(args.path)
        elif args.command == "redact-source":
            result = redact_source(args)
        elif args.command == "compare-provider-capability":
            result = compare_provider_capability(args.delta, args.capability_id)
        elif args.command == "validate-rich-media":
            result = validate_rich_media(args.documents)
        else:
            result = summarize_delta(args.delta)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, SourceLifecycleError) as error:
        code = error.code if isinstance(error, SourceLifecycleError) else "INPUT_REJECTED"
        print(json.dumps({"status": "FAIL", "code": code}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result.get("status") != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
