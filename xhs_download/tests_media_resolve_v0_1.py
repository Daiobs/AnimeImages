"""Media Resolve v0.1 Core conformance and supporting isolation evidence.

The twelve ``test_mr_cv_*`` methods are the accepted Core vectors.  Every
other test is explicitly labeled ``NON_CONFORMANCE_SUPPORTING_TEST`` and does
not create a thirteenth vector.
"""

from __future__ import annotations

import ast
import inspect
import os
import socket
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from unittest import mock

from .services import media_resolve_v0_1 as core
from .services.assets import MediaAsset, ParseResult
from .services.media_resolve_projection_v0_1 import project_parse_result
from .services.media_resolve_v0_1 import (
    CandidateCompleteness,
    CandidateProjection,
    DesiredMediaCapability,
    EvidenceState,
    FailureKind,
    Fallback,
    MediaCandidate,
    MediaItem,
    MediaItemsCompleteness,
    MediaResolveValidationError,
    MediaType,
    QualityPreference,
    ResolveRequest,
    ResolveResult,
    ResultOutcome,
    Retryability,
    SelectionOwner,
    SourceIdentity,
    UserAction,
    WarningCode,
    assert_exhaustive_candidate_set,
    assert_result_id_reuse_safe,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NEW_PATHS = (
    "xhs_download/services/media_resolve_v0_1.py",
    "xhs_download/services/media_resolve_projection_v0_1.py",
    "xhs_download/tests_media_resolve_v0_1.py",
    "docs/conformance/media-resolve-v0.1-prototype-evidence.md",
)


def source_identity() -> SourceIdentity:
    return SourceIdentity("synthetic-source", "fixture-item")


def candidate(candidate_id: str, **facts) -> MediaCandidate:
    return MediaCandidate(candidate_id=candidate_id, **facts)


def projection(
    *candidates: MediaCandidate,
    completeness: CandidateCompleteness = CandidateCompleteness.UNKNOWN,
    owner: SelectionOwner = SelectionOwner.CONSUMER,
    selected: str | None = None,
) -> CandidateProjection:
    return CandidateProjection(
        completeness=completeness,
        selection_owner=owner,
        candidates=tuple(candidates),
        selected_candidate_id=selected,
    )


def item(
    media_item_id: str,
    candidate_projection: CandidateProjection,
    media_type: MediaType = MediaType.VIDEO,
) -> MediaItem:
    return MediaItem(media_item_id, media_type, candidate_projection)


def resolved(
    *media_items: MediaItem,
    result_id: str = "result-fixture",
    completeness: MediaItemsCompleteness = MediaItemsCompleteness.UNKNOWN,
    warnings=(),
) -> ResolveResult:
    return ResolveResult.resolved(
        result_id=result_id,
        source_identity=source_identity(),
        media_items_completeness=completeness,
        media_items=media_items,
        warnings=warnings,
    )


def single_media_parse_result(
    *,
    locator: str = "fixture-locator-single",
    provider_marker: str = "fixture-provider-marker",
) -> ParseResult:
    return ParseResult(
        platform="synthetic-platform",
        post_type="video",
        base_filename="fixture-single",
        assets=(
            MediaAsset(
                media_type="video",
                url=locator,
                platform="synthetic-platform",
                preview_url="fixture-preview-marker",
                cover_url="fixture-cover-marker",
                cover_preview_url="fixture-cover-preview-marker",
                quality_label="Ultra",
                size_bytes=4096,
                size_is_estimate=False,
                headers={"Fixture-Header": "header-marker"},
                media_host_suffixes=("host-marker",),
                asset_id="product-asset-marker",
                provider=provider_marker,
                provider_resource_id="resource-marker",
                provider_cache_key="cache-marker",
                provider_manifest_token="manifest-marker",
            ),
        ),
        metadata={"raw_marker": "metadata-marker"},
    )


def multi_media_parse_result() -> ParseResult:
    return ParseResult(
        platform="synthetic-platform",
        post_type="multimedia",
        base_filename="fixture-multi",
        assets=(
            MediaAsset(
                media_type="image",
                url="fixture-locator-image",
                platform="synthetic-platform",
                quality_label="Original",
                size_bytes=1234,
                size_is_estimate=True,
                headers={"Fixture-Header": "image-header-marker"},
                provider="image-provider-marker",
            ),
            MediaAsset(
                media_type="video",
                url="fixture-locator-video",
                platform="synthetic-platform",
                quality_label="4K",
                size_bytes=5678,
                size_is_estimate=False,
                headers={"Fixture-Header": "video-header-marker"},
                provider="video-provider-marker",
            ),
        ),
        metadata={"layout": "must-not-project"},
    )


class MediaResolveV01CoreConformanceTests(unittest.TestCase):
    """Exactly the twelve accepted Media Resolve v0.1 Core vectors."""

    def test_mr_cv_01_supported_single_media_source(self):
        request = ResolveRequest(
            "fixture-reference-single",
            quality_preference=QualityPreference.BEST_AVAILABLE,
        )
        kwargs = dict(
            request=request,
            source_identity=source_identity(),
            result_id="result-single",
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            candidate_completeness=CandidateCompleteness.SELECTED_ONLY,
            selection_owner=SelectionOwner.RESOLVER,
        )
        first = project_parse_result(single_media_parse_result(), **kwargs)
        second = project_parse_result(single_media_parse_result(), **kwargs)

        self.assertEqual(first.outcome, ResultOutcome.RESOLVED)
        self.assertEqual(first.source_identity, source_identity())
        self.assertEqual(first.result_id, "result-single")
        self.assertEqual(first, second)
        self.assertEqual(len(first.media_items), 1)
        media_item = first.media_items[0]
        self.assertEqual(media_item.media_item_id, "item-0001")
        self.assertEqual(media_item.media_type, MediaType.VIDEO)
        self.assertEqual(len(media_item.candidate_projection.candidates), 1)
        projected = media_item.candidate_projection.candidates[0]
        self.assertEqual(projected.candidate_id, "candidate-0001-0001")
        self.assertEqual(projected.size["bytes"], 4096)
        self.assertEqual(projected.size["evidence"], EvidenceState.ASSERTED)
        self.assertIsNone(projected.dimensions)
        self.assertEqual(
            media_item.candidate_projection.selected_candidate_id,
            projected.candidate_id,
        )
        rendered = repr(first)
        for excluded in (
            "fixture-reference-single",
            "fixture-locator-single",
            "fixture-preview-marker",
            "fixture-cover-marker",
            "header-marker",
            "fixture-provider-marker",
            "resource-marker",
            "cache-marker",
            "manifest-marker",
            "host-marker",
            "metadata-marker",
            "Ultra",
        ):
            self.assertNotIn(excluded, rendered)

        assert_result_id_reuse_safe((first, second))
        different = ResolveResult.resolved(
            result_id="result-single",
            source_identity=source_identity(),
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            media_items=first.media_items,
            warnings=(WarningCode.FACTS_UNKNOWN,),
        )
        with self.assertRaises(MediaResolveValidationError):
            assert_result_id_reuse_safe((first, different))

    def test_mr_cv_02_unsupported_source(self):
        result = ResolveResult.failed(
            kind=FailureKind.UNSUPPORTED_SOURCE,
            retryability=Retryability.NOT_RETRYABLE,
            fallback=Fallback.AVAILABLE,
            user_action=UserAction.CHANGE_INPUT,
        )
        self.assertEqual(result.outcome, ResultOutcome.FAILED)
        self.assertIsNone(result.resolution)
        self.assertEqual(result.failure["kind"], FailureKind.UNSUPPORTED_SOURCE)
        self.assertEqual(
            set(result.failure),
            {"kind", "retryability", "fallback", "userAction"},
        )

    def test_mr_cv_03_no_usable_media(self):
        parse_result = ParseResult(
            "synthetic-platform",
            "image",
            "fixture-no-match",
            (
                MediaAsset(
                    "image",
                    "fixture-locator-image-only",
                    "synthetic-platform",
                ),
            ),
        )
        result = project_parse_result(
            parse_result,
            request=ResolveRequest(
                "fixture-reference-no-match",
                desired_media_capability=DesiredMediaCapability.VIDEO,
            ),
            source_identity=source_identity(),
            result_id="result-no-match",
            media_items_completeness=MediaItemsCompleteness.UNKNOWN,
            candidate_completeness=CandidateCompleteness.UNKNOWN,
            selection_owner=SelectionOwner.CONSUMER,
        )
        self.assertEqual(result.outcome, ResultOutcome.FAILED)
        self.assertEqual(result.failure["kind"], FailureKind.NO_USABLE_MEDIA)
        self.assertIsNone(result.resolution)
        unknown_result = project_parse_result(
            ParseResult(
                "synthetic-platform",
                "unknown",
                "fixture-unknown",
                (
                    MediaAsset(
                        "unclassified",
                        "fixture-locator-unknown",
                        "synthetic-platform",
                    ),
                ),
            ),
            request=ResolveRequest(
                "fixture-reference-unknown",
                desired_media_capability=DesiredMediaCapability.VIDEO,
            ),
            source_identity=source_identity(),
            result_id="result-unknown",
            media_items_completeness=MediaItemsCompleteness.UNKNOWN,
            candidate_completeness=CandidateCompleteness.UNKNOWN,
            selection_owner=SelectionOwner.CONSUMER,
        )
        self.assertEqual(unknown_result.failure["kind"], FailureKind.NO_USABLE_MEDIA)
        with self.assertRaises(MediaResolveValidationError):
            projection()

    def test_mr_cv_04_coarse_retryable_resolution_failure(self):
        result = ResolveResult.failed(
            kind=FailureKind.RESOLUTION_FAILED,
            retryability=Retryability.RETRYABLE,
            fallback=Fallback.UNKNOWN,
            user_action=UserAction.NONE,
        )
        self.assertEqual(result.failure["kind"], FailureKind.RESOLUTION_FAILED)
        self.assertEqual(result.failure["retryability"], Retryability.RETRYABLE)
        rendered = repr(result).lower()
        for prohibited in ("timeout", "provider", "exception", "quota"):
            self.assertNotIn(prohibited, rendered)

    def test_mr_cv_05_unknown_optional_physical_facts(self):
        unknown = {"evidence": EvidenceState.UNKNOWN}
        projected = candidate(
            "candidate-unknown",
            dimensions=unknown,
            codec=unknown,
            container=unknown,
            bitrate=unknown,
            size=unknown,
        )
        result = resolved(
            item("item-unknown", projection(projected)),
            warnings=(WarningCode.FACTS_UNKNOWN,),
        )
        self.assertEqual(result.outcome, ResultOutcome.RESOLVED)
        for name in ("dimensions", "codec", "container", "bitrate", "size"):
            fact = getattr(projected, name)
            self.assertEqual(dict(fact), {"evidence": EvidenceState.UNKNOWN})
        self.assertEqual(result.warnings, (WarningCode.FACTS_UNKNOWN,))

    def test_mr_cv_06_assertion_versus_measurement(self):
        asserted = candidate(
            "candidate-asserted",
            dimensions={
                "width": 3840,
                "height": 2160,
                "evidence": EvidenceState.ASSERTED,
            },
            size={"estimatedBytes": 9000, "evidence": EvidenceState.ASSERTED},
        )
        observed = candidate(
            "candidate-observed",
            size={"bytes": 9100, "evidence": EvidenceState.OBSERVED},
        )
        measured = candidate(
            "candidate-measured",
            dimensions={
                "width": 1920,
                "height": 1080,
                "evidence": EvidenceState.MEASURED,
            },
            codec={"value": "h264", "evidence": EvidenceState.MEASURED},
            container={"value": "mp4", "evidence": EvidenceState.MEASURED},
            bitrate={
                "bitsPerSecond": 8000000,
                "evidence": EvidenceState.MEASURED,
            },
            size={"bytes": 9200, "evidence": EvidenceState.MEASURED},
        )
        result = resolved(
            item(
                "item-facts",
                projection(
                    measured,
                    asserted,
                    observed,
                    completeness=CandidateCompleteness.BOUNDED,
                ),
            )
        )
        self.assertEqual(result.outcome, ResultOutcome.RESOLVED)
        self.assertEqual(asserted.dimensions["evidence"], EvidenceState.ASSERTED)
        self.assertEqual(observed.size["evidence"], EvidenceState.OBSERVED)
        self.assertEqual(measured.dimensions["evidence"], EvidenceState.MEASURED)
        self.assertNotIn("Ultra", repr(result))
        with self.assertRaises(MediaResolveValidationError):
            candidate(
                "candidate-invalid-estimate",
                size={
                    "estimatedBytes": 1,
                    "evidence": EvidenceState.MEASURED,
                },
            )

    def test_mr_cv_07_service_style_selected_only_projection(self):
        projected = candidate("candidate-service")
        candidate_projection = projection(
            projected,
            completeness=CandidateCompleteness.SELECTED_ONLY,
            owner=SelectionOwner.PROVIDER_OR_SERVICE,
            selected=projected.candidate_id,
        )
        result = resolved(item("item-service", candidate_projection))
        self.assertEqual(result.outcome, ResultOutcome.RESOLVED)
        self.assertEqual(
            candidate_projection.selection_owner,
            SelectionOwner.PROVIDER_OR_SERVICE,
        )
        self.assertEqual(candidate_projection.selected_candidate_id, projected.candidate_id)
        self.assertNotIn("url", {field.name for field in fields(MediaCandidate)})

    def test_mr_cv_08_selected_plus_bounded_alternatives(self):
        first = candidate("candidate-a")
        second = candidate("candidate-b")
        left = projection(
            second,
            first,
            completeness=CandidateCompleteness.BOUNDED,
            owner=SelectionOwner.RESOLVER,
            selected="candidate-b",
        )
        right = projection(
            first,
            second,
            completeness=CandidateCompleteness.BOUNDED,
            owner=SelectionOwner.RESOLVER,
            selected="candidate-b",
        )
        self.assertEqual(left, right)
        self.assertEqual(
            [value.candidate_id for value in left.candidates],
            ["candidate-a", "candidate-b"],
        )
        one_bounded = projection(
            first,
            completeness=CandidateCompleteness.BOUNDED,
        )
        self.assertEqual(one_bounded.completeness, CandidateCompleteness.BOUNDED)

    def test_mr_cv_09_genuinely_exhaustive_projection(self):
        complete = projection(
            candidate("candidate-b"),
            candidate("candidate-a"),
            completeness=CandidateCompleteness.EXHAUSTIVE,
        )
        assert_exhaustive_candidate_set(
            complete,
            ("candidate-a", "candidate-b"),
        )
        incomplete = projection(
            candidate("candidate-a"),
            completeness=CandidateCompleteness.EXHAUSTIVE,
        )
        with self.assertRaises(MediaResolveValidationError):
            assert_exhaustive_candidate_set(
                incomplete,
                ("candidate-a", "candidate-b"),
            )

    def test_mr_cv_10_consumer_owned_selection(self):
        consumer_projection = projection(
            candidate("candidate-consumer-a"),
            candidate("candidate-consumer-b"),
            completeness=CandidateCompleteness.BOUNDED,
            owner=SelectionOwner.CONSUMER,
        )
        result = resolved(
            item("item-consumer", consumer_projection),
            warnings=(WarningCode.PREFERENCE_NOT_APPLIED,),
        )
        self.assertIsNone(consumer_projection.selected_candidate_id)
        self.assertEqual(result.warnings, (WarningCode.PREFERENCE_NOT_APPLIED,))
        later_consumer_choice = "candidate-consumer-b"
        self.assertEqual(later_consumer_choice, "candidate-consumer-b")
        self.assertIsNone(result.media_items[0].candidate_projection.selected_candidate_id)
        with self.assertRaises(FrozenInstanceError):
            consumer_projection.selected_candidate_id = later_consumer_choice
        with self.assertRaises(MediaResolveValidationError):
            projection(
                candidate("candidate-hidden-input"),
                completeness=CandidateCompleteness.BOUNDED,
                owner=SelectionOwner.CONSUMER,
                selected="candidate-hidden-input",
            )

    def test_mr_cv_11_multi_asset_result(self):
        result = project_parse_result(
            multi_media_parse_result(),
            request=ResolveRequest("fixture-reference-multi"),
            source_identity=source_identity(),
            result_id="result-multi",
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            candidate_completeness=CandidateCompleteness.UNKNOWN,
            selection_owner=SelectionOwner.CONSUMER,
        )
        self.assertEqual(result.outcome, ResultOutcome.RESOLVED)
        self.assertEqual(
            [value.media_item_id for value in result.media_items],
            ["item-0001", "item-0002"],
        )
        self.assertEqual(
            [value.media_type for value in result.media_items],
            [MediaType.IMAGE, MediaType.VIDEO],
        )
        candidate_ids = [
            value.candidate_id
            for media_item in result.media_items
            for value in media_item.candidate_projection.candidates
        ]
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)))
        self.assertTrue(
            all(media_item.candidate_projection.candidates for media_item in result.media_items)
        )
        first_size = result.media_items[0].candidate_projection.candidates[0].size
        second_size = result.media_items[1].candidate_projection.candidates[0].size
        self.assertEqual(first_size["estimatedBytes"], 1234)
        self.assertEqual(first_size["evidence"], EvidenceState.ASSERTED)
        self.assertEqual(second_size["bytes"], 5678)
        self.assertEqual(second_size["evidence"], EvidenceState.ASSERTED)
        rendered = repr(result)
        for excluded in (
            "fixture-locator-image",
            "fixture-locator-video",
            "image-header-marker",
            "video-header-marker",
            "image-provider-marker",
            "video-provider-marker",
            "must-not-project",
            "Original",
            "4K",
        ):
            self.assertNotIn(excluded, rendered)
        for relationship_field in ("group", "layout", "relation", "rich_media"):
            self.assertNotIn(relationship_field, rendered)

        omitted = project_parse_result(
            ParseResult(
                "synthetic-platform",
                "gallery",
                "fixture-ordinary-omission",
                (
                    MediaAsset(
                        "image",
                        "fixture-locator-kept",
                        "synthetic-platform",
                    ),
                    MediaAsset("image", "", "synthetic-platform"),
                ),
            ),
            request=ResolveRequest("fixture-reference-ordinary-omission"),
            source_identity=source_identity(),
            result_id="result-ordinary-omission",
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            candidate_completeness=CandidateCompleteness.UNKNOWN,
            selection_owner=SelectionOwner.CONSUMER,
        )
        self.assertEqual(
            omitted.resolution["mediaItemsCompleteness"],
            MediaItemsCompleteness.BOUNDED,
        )
        self.assertIn(WarningCode.MEDIA_ITEMS_OMITTED, omitted.warnings)

        selected = project_parse_result(
            multi_media_parse_result(),
            request=ResolveRequest("fixture-reference-multi-selected"),
            source_identity=source_identity(),
            result_id="result-multi-selected",
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            candidate_completeness=CandidateCompleteness.SELECTED_ONLY,
            selection_owner=SelectionOwner.RESOLVER,
        )
        for media_item in selected.media_items:
            candidate_projection = media_item.candidate_projection
            self.assertEqual(len(candidate_projection.candidates), 1)
            self.assertEqual(
                candidate_projection.selected_candidate_id,
                candidate_projection.candidates[0].candidate_id,
            )

        duplicate_item = item(
            "item-duplicate-vector",
            projection(candidate("candidate-vector-one")),
        )
        with self.assertRaises(MediaResolveValidationError):
            resolved(duplicate_item, duplicate_item)
        duplicate_candidate_a = item(
            "item-vector-a",
            projection(candidate("candidate-vector-duplicate")),
        )
        duplicate_candidate_b = item(
            "item-vector-b",
            projection(candidate("candidate-vector-duplicate")),
            MediaType.IMAGE,
        )
        with self.assertRaises(MediaResolveValidationError):
            resolved(duplicate_candidate_a, duplicate_candidate_b)

        security_failure = ResolveResult.failed(
            kind=FailureKind.SECURITY_REJECTED,
            retryability=Retryability.NOT_RETRYABLE,
            fallback=Fallback.UNKNOWN,
            user_action=UserAction.NONE,
        )
        self.assertEqual(security_failure.media_items, ())

    def test_mr_cv_12_security_rejection_and_core_surface_closure(self):
        result = ResolveResult.failed(
            kind=FailureKind.SECURITY_REJECTED,
            retryability=Retryability.NOT_RETRYABLE,
            fallback=Fallback.UNKNOWN,
            user_action=UserAction.UNKNOWN,
        )
        self.assertEqual(result.outcome, ResultOutcome.FAILED)
        self.assertIsNone(result.resolution)
        self.assertEqual(result.failure["kind"], FailureKind.SECURITY_REJECTED)
        with self.assertRaises(MediaResolveValidationError):
            ResolveResult.failed(
                kind=FailureKind.SECURITY_REJECTED,
                retryability=Retryability.NOT_RETRYABLE,
                fallback=Fallback.AVAILABLE,
                user_action=UserAction.REPAIR_CREDENTIAL_ACCESS,
            )

        dataclass_names = {
            name
            for name, value in vars(core).items()
            if inspect.isclass(value) and is_dataclass(value)
        }
        self.assertEqual(
            dataclass_names,
            {
                "ResolveRequest",
                "ResolveResult",
                "SourceIdentity",
                "MediaItem",
                "CandidateProjection",
                "MediaCandidate",
            },
        )
        self.assertEqual(
            {value.name for value in fields(MediaCandidate)},
            {"candidate_id", "dimensions", "codec", "container", "bitrate", "size"},
        )
        typed_identity_values = {
            source_identity().item_id,
            resolved(
                item("item-fixture", projection(candidate("candidate-fixture"))),
                result_id="result-fixture",
            ).result_id,
            "item-fixture",
            "candidate-fixture",
        }
        self.assertEqual(len(typed_identity_values), 4)
        for prohibited in (
            "url",
            "headers",
            "cookie",
            "token",
            "provider",
            "locator",
            "handoff",
            "credential",
            "exception",
        ):
            self.assertNotIn(prohibited, repr(result).lower())


class MediaResolveV01SupportingTests(unittest.TestCase):
    """NON_CONFORMANCE_SUPPORTING_TEST — isolation and negative evidence."""

    suite_label = "NON_CONFORMANCE_SUPPORTING_TEST"

    def test_non_conformance_supporting_test_duplicate_result_alias_rejection(self):
        baseline = resolved(
            item("item-alias", projection(candidate("candidate-alias"))),
            result_id="result-alias",
        )
        changed = resolved(
            item("item-alias", projection(candidate("candidate-alias"))),
            result_id="result-alias",
            warnings=(WarningCode.FACTS_UNKNOWN,),
        )
        with self.assertRaises(MediaResolveValidationError):
            assert_result_id_reuse_safe((baseline, changed))

    def test_non_conformance_supporting_test_duplicate_media_item_id_rejection(self):
        duplicated = item("item-duplicate", projection(candidate("candidate-one")))
        another = item("item-duplicate", projection(candidate("candidate-two")))
        with self.assertRaises(MediaResolveValidationError):
            resolved(duplicated, another)

    def test_non_conformance_supporting_test_duplicate_candidate_id_rejection(self):
        first = item("item-one", projection(candidate("candidate-duplicate")))
        second = item(
            "item-two",
            projection(candidate("candidate-duplicate")),
            MediaType.IMAGE,
        )
        with self.assertRaises(MediaResolveValidationError):
            resolved(first, second)

    def test_non_conformance_supporting_test_hidden_consumer_selection_rejection(self):
        with self.assertRaises(MediaResolveValidationError):
            projection(
                candidate("candidate-consumer"),
                completeness=CandidateCompleteness.BOUNDED,
                owner=SelectionOwner.CONSUMER,
                selected="candidate-consumer",
            )

    def test_non_conformance_supporting_test_locator_provider_id_derivation_rejection(self):
        for invalid_id in (
            "locator/segment",
            "candidate?signature=fixture",
            "candidate&credential=fixture",
            "candidate id",
        ):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(MediaResolveValidationError):
                    candidate(invalid_id)

        request = ResolveRequest("fixture-reference-id-independence")
        kwargs = dict(
            request=request,
            source_identity=source_identity(),
            result_id="result-id-independence",
            media_items_completeness=MediaItemsCompleteness.EXHAUSTIVE,
            candidate_completeness=CandidateCompleteness.UNKNOWN,
            selection_owner=SelectionOwner.CONSUMER,
        )
        first = project_parse_result(
            single_media_parse_result(
                locator="fixture-locator-one",
                provider_marker="provider-marker-one",
            ),
            **kwargs,
        )
        second = project_parse_result(
            single_media_parse_result(
                locator="fixture-locator-two",
                provider_marker="provider-marker-two",
            ),
            **kwargs,
        )
        self.assertEqual(
            first.media_items[0].candidate_projection.candidates[0].candidate_id,
            second.media_items[0].candidate_projection.candidates[0].candidate_id,
        )

    def test_non_conformance_supporting_test_production_import_boundary(self):
        production_files = {
            PROJECT_ROOT / "djangoProject" / "urls.py",
            PROJECT_ROOT / "djangoProject" / "asgi.py",
            PROJECT_ROOT / "djangoProject" / "wsgi.py",
            PROJECT_ROOT / "xhs_download" / "urls.py",
            PROJECT_ROOT / "xhs_download" / "views.py",
            PROJECT_ROOT / "xhs_download" / "admin.py",
            PROJECT_ROOT / "xhs_download" / "apps.py",
            PROJECT_ROOT / "xhs_download" / "services" / "registry.py",
            PROJECT_ROOT / "xhs_download" / "services" / "parser.py",
            PROJECT_ROOT / "static" / "js" / "ordinary_media_download_sw.js",
        }
        production_files.update(
            (PROJECT_ROOT / "xhs_download" / "management" / "commands").glob("*.py")
        )
        production_files.update((PROJECT_ROOT / "ops").rglob("*"))
        needles = ("media_resolve_v0_1", "media_resolve_projection_v0_1")
        references = []
        for path in sorted(production_files):
            if not path.is_file() or path.suffix.lower() not in {
                ".py",
                ".js",
                ".sh",
                ".service",
                ".md",
                ".json",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                references.append(path.relative_to(PROJECT_ROOT).as_posix())
        self.assertEqual(references, [])

    def test_non_conformance_supporting_test_import_has_no_network_database_or_registry(self):
        script = r'''
import importlib.abc
import socket
import sys

class BlockedImport(importlib.abc.MetaPathFinder):
    BLOCKED = ("django", "redis", "psycopg", "requests", "httpx")
    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.BLOCKED or fullname.startswith(tuple(value + "." for value in self.BLOCKED)):
            raise AssertionError("blocked dependency import: " + fullname)
        return None

def network_forbidden(*args, **kwargs):
    raise AssertionError("network attempted")

sys.meta_path.insert(0, BlockedImport())
socket.socket = network_forbidden
socket.create_connection = network_forbidden
import xhs_download.services.media_resolve_v0_1
import xhs_download.services.media_resolve_projection_v0_1
assert "xhs_download.services.registry" not in sys.modules
assert not any(name.startswith("django") for name in sys.modules)
'''
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_non_conformance_supporting_test_static_import_surface(self):
        forbidden_roots = {
            "django",
            "redis",
            "psycopg",
            "requests",
            "httpx",
            "socket",
        }
        forbidden_fragments = {
            "settings",
            "registry",
            "provider",
            "delivery",
            "downloads",
        }
        violations = []
        for relative_path in NEW_PATHS[:2]:
            path = PROJECT_ROOT / relative_path
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    module = name.lower()
                    root = module.split(".", 1)[0]
                    if root in forbidden_roots or any(
                        fragment in module for fragment in forbidden_fragments
                    ):
                        violations.append((relative_path, name))
        self.assertEqual(violations, [])

    def test_non_conformance_supporting_test_no_network_or_persistent_write(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("network or persistent write attempted")

        with mock.patch.object(socket, "socket", side_effect=forbidden), mock.patch.object(
            socket,
            "create_connection",
            side_effect=forbidden,
        ), mock.patch("builtins.open", side_effect=forbidden), mock.patch.object(
            Path,
            "write_text",
            side_effect=forbidden,
        ), mock.patch.object(Path, "write_bytes", side_effect=forbidden):
            result = project_parse_result(
                single_media_parse_result(),
                request=ResolveRequest("fixture-reference-pure"),
                source_identity=source_identity(),
                result_id="result-pure",
                media_items_completeness=MediaItemsCompleteness.UNKNOWN,
                candidate_completeness=CandidateCompleteness.UNKNOWN,
                selection_owner=SelectionOwner.CONSUMER,
            )
        self.assertEqual(result.outcome, ResultOutcome.RESOLVED)

    def test_non_conformance_supporting_test_deletion_only_path_inventory(self):
        self.assertEqual(
            NEW_PATHS,
            (
                "xhs_download/services/media_resolve_v0_1.py",
                "xhs_download/services/media_resolve_projection_v0_1.py",
                "xhs_download/tests_media_resolve_v0_1.py",
                "docs/conformance/media-resolve-v0.1-prototype-evidence.md",
            ),
        )
        self.assertTrue(all((PROJECT_ROOT / path).is_file() for path in NEW_PATHS))
