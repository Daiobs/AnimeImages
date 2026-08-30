from __future__ import annotations

from collections import OrderedDict
import io
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from tools.provider_lab.runtime.workers import videodl_worker, worker_common
from xhs_download.services.provider_lab.contracts import (
    LabProviderAttemptV1,
    LabProviderResultV1,
    ProviderLabError,
)
from xhs_download.services.provider_lab.providers import run_provider
from xhs_download.services.provider_lab.runtime import parse_worker_stdout
from xhs_download.services.provider_lab.videodl_control import (
    MAX_BACKEND_IDS,
    VideodlControlledError,
    execute_controlled_videodl,
    normalize_inventory,
    parse_videodl_options,
)


class VideodlWorkerProtocolTests(SimpleTestCase):
    def _videodl_modules(self, *, dedicated=None, common=None, build_video=None, build_common=None):
        package = ModuleType("videodl")
        package.__path__ = []
        modules = ModuleType("videodl.modules")
        modules.VideoClientBuilder = SimpleNamespace(
            REGISTERED_MODULES=OrderedDict(dedicated or {})
        )
        modules.CommonVideoClientBuilder = SimpleNamespace(
            REGISTERED_MODULES=OrderedDict(common or {})
        )
        modules.BuildVideoClient = build_video or mock.Mock()
        modules.BuildCommonVideoClient = build_common or mock.Mock()
        package.modules = modules
        return mock.patch.dict(
            sys.modules,
            {"videodl": package, "videodl.modules": modules},
        )

    def test_inventory_uses_fixed_registry_order_and_categories(self):
        with self._videodl_modules(
            dedicated={"AlphaVideoClient": object, "BetaVideoClient": object},
            common={"CommonVideoClient": object},
        ):
            inventory = videodl_worker.backend_inventory()
        self.assertEqual(
            [(item["backend_id"], item["backend_category"]) for item in inventory],
            [
                ("AlphaVideoClient", "dedicated"),
                ("BetaVideoClient", "dedicated"),
                ("CommonVideoClient", "common"),
            ],
        )
        self.assertFalse(inventory[0]["external_sharing_required"])
        self.assertTrue(inventory[-1]["external_sharing_required"])

    def test_inventory_does_not_expose_module_paths_or_class_reprs(self):
        with self._videodl_modules(dedicated={"AlphaVideoClient": object}):
            encoded = json.dumps(videodl_worker.backend_inventory())
        self.assertNotIn("module", encoded.lower())
        self.assertNotIn("<class", encoded)

    def test_matching_dedicated_backends_only_calls_belongto(self):
        matching = SimpleNamespace(belongto=mock.Mock(return_value=True))
        skipped = SimpleNamespace(belongto=mock.Mock(return_value=False))
        with self._videodl_modules(
            dedicated={"MatchingVideoClient": matching, "SkippedVideoClient": skipped}
        ):
            result = videodl_worker.matching_dedicated_backends("https://example.com/watch")
        self.assertEqual(result, ["MatchingVideoClient"])
        matching.belongto.assert_called_once()
        skipped.belongto.assert_called_once()

    def test_exact_dedicated_backend_rejects_category_mismatch_before_build(self):
        backend = SimpleNamespace(belongto=mock.Mock(return_value=True))
        build_common = mock.Mock()
        with self._videodl_modules(
            dedicated={"OnlyDedicated": backend},
            build_common=build_common,
        ):
            with self.assertRaises(videodl_worker.WorkerError) as context:
                videodl_worker.run_exact_backend(
                    "https://example.com/watch",
                    "OnlyDedicated",
                    "common",
                    "/tmp/work",
                )
        self.assertEqual(context.exception.code, "PROVIDER_SCHEMA_INVALID")
        build_common.assert_not_called()


class VideodlControlledOrchestrationTests(SimpleTestCase):
    def _videodl_modules(self, *, dedicated=None, common=None, build_video=None, build_common=None):
        package = ModuleType("videodl")
        package.__path__ = []
        modules = ModuleType("videodl.modules")
        modules.VideoClientBuilder = SimpleNamespace(
            REGISTERED_MODULES=OrderedDict(dedicated or {})
        )
        modules.CommonVideoClientBuilder = SimpleNamespace(
            REGISTERED_MODULES=OrderedDict(common or {})
        )
        modules.BuildVideoClient = build_video or mock.Mock()
        modules.BuildCommonVideoClient = build_common or mock.Mock()
        package.modules = modules
        return mock.patch.dict(
            sys.modules,
            {"videodl": package, "videodl.modules": modules},
        )

    @staticmethod
    def _inventory(*items):
        return [
            {
                "backend_id": backend_id,
                "backend_category": category,
                "available": True,
                "external_sharing_required": category == "common",
                "display_label": backend_id,
            }
            for category, backend_id in items
        ]

    @staticmethod
    def _success(backend_id):
        return {
            "result": {
                "status": "success",
                "selected_backend": "upstream-value",
                "warnings": [],
            },
            "resources": [{"asset_id": f"asset-{backend_id}"}],
            "metrics": {"candidate_count": 1},
        }

    def _options(self, mode, ids=(), category="", confirmed=False):
        return parse_videodl_options(
            {
                "mode": mode,
                "backend_ids": list(ids),
                "backend_category": category,
                "external_sharing_confirmed": confirmed,
            }
        )

    def _invoke(self, *, inventory, matches=(), backend_results=None, calls=None):
        backend_results = dict(backend_results or {})
        calls = calls if calls is not None else []

        def invoke(**kwargs):
            calls.append(dict(kwargs))
            operation = kwargs.get("operation")
            if operation == "inventory":
                return {"operation": operation, "inventory": inventory}
            if operation == "match_dedicated":
                return {"operation": operation, "backend_ids": list(matches)}
            if operation in {"backend", "legacy_auto"}:
                outcome = backend_results.get(kwargs.get("backend_id") or "legacy_auto")
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome or self._success(kwargs.get("backend_id") or "legacy_auto")
            raise AssertionError(f"unexpected operation: {operation}")

        return invoke

    def test_options_reject_unknown_and_grabber_modes(self):
        for mode, code in (
            ("future_mode", "PROVIDER_SCHEMA_INVALID"),
            ("grabber", "PROVIDER_POLICY_REJECTED"),
        ):
            with self.subTest(mode=mode), self.assertRaises(ProviderLabError) as context:
                self._options(mode)
            self.assertEqual(context.exception.code, code)

    def test_options_reject_duplicate_and_excessive_backend_ids(self):
        with self.assertRaises(ProviderLabError):
            self._options(
                "common_only",
                ids=("CommonA", "CommonA"),
                category="common",
                confirmed=True,
            )
        with self.assertRaises(ProviderLabError):
            self._options(
                "common_only",
                ids=tuple(f"Common{index}" for index in range(MAX_BACKEND_IDS + 1)),
                category="common",
                confirmed=True,
            )

    def test_common_mode_requires_explicit_external_sharing_confirmation(self):
        with self.assertRaises(ProviderLabError) as context:
            self._options(
                "common_only",
                ids=("CommonA",),
                category="common",
                confirmed=False,
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

    def test_inventory_rejects_duplicate_or_category_mismatched_items(self):
        duplicate = self._inventory(
            ("dedicated", "ExactBackend"),
            ("dedicated", "ExactBackend"),
        )
        with self.assertRaises(ProviderLabError):
            normalize_inventory(duplicate)
        mismatched = self._inventory(("dedicated", "ExactBackend"))
        mismatched[0]["external_sharing_required"] = True
        with self.assertRaises(ProviderLabError):
            normalize_inventory(mismatched)

    def test_specific_backend_runs_exactly_one_backend_without_fallback(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(
                ("dedicated", "ExactBackend"),
                ("common", "CommonBackend"),
            ),
            calls=calls,
        )
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_specific",
            source="https://example.com/watch",
            options=self._options(
                "specific_backend",
                ids=("ExactBackend",),
                category="dedicated",
            ),
            invoke=invoke,
        )
        backend_calls = [item for item in calls if item.get("operation") == "backend"]
        self.assertEqual([item["backend_id"] for item in backend_calls], ["ExactBackend"])
        self.assertEqual(result.result["selected_backend"], "ExactBackend")
        self.assertEqual(len(result.attempts), 1)
        self.assertTrue(result.attempts[0].selected)

    def test_specific_unknown_or_category_mismatched_backend_fails_closed(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "ExactBackend")),
        )
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_unknown",
                source="https://example.com/watch",
                options=self._options(
                    "specific_backend",
                    ids=("MissingBackend",),
                    category="dedicated",
                ),
                invoke=invoke,
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

    def test_dedicated_only_preserves_failures_and_never_calls_common(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(
                ("dedicated", "FirstDedicated"),
                ("dedicated", "SecondDedicated"),
                ("common", "CommonBackend"),
            ),
            matches=("FirstDedicated", "SecondDedicated"),
            backend_results={
                "FirstDedicated": ProviderLabError("PROVIDER_RESULT_EMPTY"),
                "SecondDedicated": self._success("SecondDedicated"),
            },
            calls=calls,
        )
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_dedicated",
            source="https://example.com/watch",
            options=self._options("dedicated_only", category="dedicated"),
            invoke=invoke,
        )
        self.assertEqual(
            [(item.backend_id, item.outcome) for item in result.attempts],
            [("FirstDedicated", "empty"), ("SecondDedicated", "success")],
        )
        self.assertFalse(any(item.get("backend_category") == "common" for item in calls))
        self.assertFalse(any(item.get("operation") == "grabber" for item in calls))

    def test_dedicated_match_rejects_unknown_worker_backend(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "KnownDedicated")),
            matches=("UnknownDedicated",),
        )
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_unknown_match",
                source="https://example.com/watch",
                options=self._options("dedicated_only", category="dedicated"),
                invoke=invoke,
            )
        self.assertEqual(context.exception.code, "PROVIDER_SCHEMA_INVALID")
        self.assertEqual(context.exception.attempts, ())

    def test_expired_total_deadline_does_not_invent_an_attempt(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "ExactBackend")),
            matches=("ExactBackend",),
            calls=calls,
        )
        clock_values = iter((0.0, 0.0, 0.0, 2.0))
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_deadline",
                source="https://example.com/watch",
                options=self._options("dedicated_only", category="dedicated"),
                total_timeout_seconds=1.0,
                invoke=invoke,
                clock=lambda: next(clock_values),
            )
        self.assertEqual(context.exception.code, "PROVIDER_TIMEOUT")
        self.assertEqual(context.exception.attempts, ())
        self.assertFalse(any(item.get("operation") == "backend" for item in calls))

    def test_common_only_runs_only_selected_ids_and_marks_external_sharing(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(
                ("common", "SelectedCommon"),
                ("common", "OtherCommon"),
            ),
            calls=calls,
        )
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_common",
            source="https://example.com/watch",
            options=self._options(
                "common_only",
                ids=("SelectedCommon",),
                category="common",
                confirmed=True,
            ),
            invoke=invoke,
        )
        self.assertEqual(result.attempts[0].backend_id, "SelectedCommon")
        self.assertTrue(result.attempts[0].external_sharing)
        self.assertFalse(any(item.get("backend_id") == "OtherCommon" for item in calls))

    def test_reviewed_auto_runs_dedicated_then_selected_common(self):
        invoke = self._invoke(
            inventory=self._inventory(
                ("dedicated", "DedicatedBackend"),
                ("common", "ReviewedCommon"),
            ),
            matches=("DedicatedBackend",),
            backend_results={
                "DedicatedBackend": ProviderLabError("PROVIDER_PROCESS_FAILED"),
                "ReviewedCommon": self._success("ReviewedCommon"),
            },
        )
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_reviewed",
            source="https://example.com/watch",
            options=self._options(
                "reviewed_auto",
                ids=("ReviewedCommon",),
                category="common",
                confirmed=True,
            ),
            invoke=invoke,
        )
        self.assertEqual(
            [(item.backend_id, item.outcome) for item in result.attempts],
            [("DedicatedBackend", "failed"), ("ReviewedCommon", "success")],
        )
        self.assertTrue(result.attempts[-1].selected)

    def test_failure_retains_bounded_safe_attempts(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "ExactBackend")),
            backend_results={
                "ExactBackend": ProviderLabError("PROVIDER_TIMEOUT"),
            },
        )
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_failed",
                source="https://example.com/watch?token=private",
                options=self._options(
                    "specific_backend",
                    ids=("ExactBackend",),
                    category="dedicated",
                ),
                invoke=invoke,
            )
        error = context.exception
        self.assertEqual(error.code, "PROVIDER_TIMEOUT")
        self.assertEqual(error.attempts[0].safe_failure_code, "BACKEND_TIMEOUT")
        self.assertNotIn("https://", json.dumps(error.attempts[0].as_dict()))
        self.assertNotIn("private", json.dumps(error.attempts[0].as_dict()))

    def test_exact_dedicated_backend_does_not_run_when_source_is_unsupported(self):
        backend = SimpleNamespace(belongto=mock.Mock(return_value=False))
        build_video = mock.Mock()
        with self._videodl_modules(
            dedicated={"ExactVideoClient": backend},
            build_video=build_video,
        ):
            with self.assertRaises(videodl_worker.WorkerError) as context:
                videodl_worker.run_exact_backend(
                    "https://example.com/watch",
                    "ExactVideoClient",
                    "dedicated",
                    "/tmp/work",
                )
        self.assertEqual(context.exception.code, "PROVIDER_UNSUPPORTED")
        build_video.assert_not_called()

    def test_exact_common_backend_builds_and_parses_only_selected_backend(self):
        info = SimpleNamespace(
            with_valid_download_url=True,
            with_valid_audio_download_url=False,
        )
        client = SimpleNamespace(parsefromurl=mock.Mock(return_value=[info]))
        build_common = mock.Mock(return_value=client)
        with self._videodl_modules(
            common={"ChosenCommon": object, "OtherCommon": object},
            build_common=build_common,
        ):
            result = videodl_worker.run_exact_backend(
                "https://example.com/watch",
                "ChosenCommon",
                "common",
                "/tmp/work",
            )
        self.assertEqual(result, [info])
        self.assertEqual(build_common.call_args.args[0]["type"], "ChosenCommon")
        client.parsefromurl.assert_called_once_with(
            "https://example.com/watch",
            request_overrides={},
        )

    def test_operation_request_schema_is_closed(self):
        valid = {
            "request_id": "request_backend",
            "source": "https://example.com/watch",
            "max_assets": 64,
            "max_output_bytes": 1024 * 1024,
            "operation": "backend",
            "backend_id": "ExactVideoClient",
            "backend_category": "dedicated",
        }
        self.assertEqual(videodl_worker.validate_operation_request(valid), "backend")
        with self.assertRaises(videodl_worker.WorkerError):
            videodl_worker.validate_operation_request({**valid, "private_option": True})

    def test_shared_request_parser_keeps_old_exact_schema(self):
        payload = {
            "schema_version": "1.0",
            "request_id": "request_legacy",
            "source": "https://example.com/watch",
            "max_assets": 64,
            "max_output_bytes": 1024 * 1024,
            "operation": "legacy_auto",
        }
        with mock.patch.object(
            sys,
            "stdin",
            SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())),
        ):
            with self.assertRaises(worker_common.WorkerError):
                worker_common.parse_request()
        with mock.patch.object(
            sys,
            "stdin",
            SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())),
        ):
            parsed = worker_common.parse_request(optional_fields={"operation"})
        self.assertEqual(parsed["operation"], "legacy_auto")

    def test_runtime_accepts_only_expected_inventory_message(self):
        payload = {
            "schema_version": "1.0",
            "operation": "inventory",
            "inventory": [],
        }
        self.assertEqual(
            parse_worker_stdout(json.dumps(payload), expected_operation="inventory"),
            payload,
        )
        with self.assertRaises(ProviderLabError) as context:
            parse_worker_stdout(json.dumps(payload), expected_operation="match_dedicated")
        self.assertEqual(context.exception.code, "PROVIDER_SCHEMA_INVALID")


@override_settings(
    UPSTREAM_PROVIDER_LAB_ENABLED=True,
    VIDEODL_PROVIDER_LAB_ENABLED=True,
)
class VideodlProviderLabIntegrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="videodl-lab-staff",
            password="secret",
            is_staff=True,
        )
        self.client.force_login(self.user)

    @mock.patch("xhs_download.provider_lab_views.list_videodl_backend_inventory")
    def test_staff_page_renders_bounded_inventory_controls_without_urls(self, inventory_mock):
        inventory_mock.return_value = {
            "status": "ready",
            "items": [
                {
                    "backend_id": "DedicatedBackend",
                    "backend_category": "dedicated",
                    "available": True,
                    "external_sharing_required": False,
                    "display_label": "Dedicated Backend",
                },
                {
                    "backend_id": "CommonBackend",
                    "backend_category": "common",
                    "available": True,
                    "external_sharing_required": True,
                    "display_label": "Common Backend",
                },
            ],
        }
        response = self.client.get(reverse("provider_lab"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("videodl 受控执行", content)
        self.assertIn("CommonBackend", content)
        self.assertIn("外部 aggregator", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("module path", content)

    @mock.patch("xhs_download.provider_lab_views.run_provider")
    def test_view_passes_closed_videodl_options_to_runtime(self, run_mock):
        run_mock.return_value = {"schema_version": "1.0", "status": "success"}
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {
                    "source": "https://example.com/watch",
                    "provider_id": "videodl_repo",
                    "options": {
                        "mode": "specific_backend",
                        "backend_ids": ["ExactBackend"],
                        "backend_category": "dedicated",
                        "external_sharing_confirmed": False,
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        options = run_mock.call_args.kwargs["options"]
        self.assertEqual(options.mode, "specific_backend")
        self.assertEqual(options.backend_ids, ("ExactBackend",))

    @mock.patch("xhs_download.provider_lab_views.run_provider")
    def test_old_request_remains_legacy_compatible(self, run_mock):
        run_mock.return_value = {"schema_version": "1.0", "status": "success"}
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {
                    "source": "https://example.com/watch",
                    "provider_id": "videodl_repo",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(run_mock.call_args.kwargs["options"])

    def test_other_provider_rejects_videodl_options(self):
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {
                    "source": "https://example.com/watch",
                    "provider_id": "animeimages_current",
                    "options": {
                        "mode": "legacy_auto",
                        "backend_ids": [],
                        "backend_category": "",
                        "external_sharing_confirmed": False,
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "PROVIDER_SCHEMA_INVALID")

    def test_common_without_confirmation_is_policy_rejected(self):
        response = self.client.post(
            reverse("provider_lab_run"),
            data=json.dumps(
                {
                    "source": "https://example.com/watch",
                    "provider_id": "videodl_repo",
                    "options": {
                        "mode": "common_only",
                        "backend_ids": ["CommonBackend"],
                        "backend_category": "common",
                        "external_sharing_confirmed": False,
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "PROVIDER_POLICY_REJECTED")

    @mock.patch("xhs_download.services.provider_lab.providers.execute_controlled_videodl")
    def test_runtime_failure_retains_safe_attempts(self, execute_mock):
        attempt = LabProviderAttemptV1(
            ordinal=1,
            backend_id="ExactBackend",
            backend_category="dedicated",
            outcome="timeout",
            elapsed_ms=8000,
            safe_failure_code="BACKEND_TIMEOUT",
        )
        execute_mock.side_effect = VideodlControlledError(
            "PROVIDER_TIMEOUT",
            (attempt,),
        )
        result = run_provider(
            provider_id="videodl_repo",
            source="https://example.com/watch",
            user_id=self.user.pk,
            options=parse_videodl_options(
                {
                    "mode": "specific_backend",
                    "backend_ids": ["ExactBackend"],
                    "backend_category": "dedicated",
                    "external_sharing_confirmed": False,
                }
            ),
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["attempts"][0]["safe_failure_code"], "BACKEND_TIMEOUT")

    def test_contract_allows_at_most_one_selected_attempt(self):
        attempts = tuple(
            LabProviderAttemptV1(
                ordinal=index,
                backend_id=f"Backend{index}",
                backend_category="dedicated",
                outcome="success",
                selected=True,
            )
            for index in (1, 2)
        )
        payload = LabProviderResultV1(
            request_id="request_attempts",
            provider_id="videodl_repo",
            provider_version="videodl@test",
            provider_scope="lab_only_noncommercial",
            status="success",
            attempts=attempts,
        ).as_dict()
        self.assertEqual(sum(item["selected"] for item in payload["attempts"]), 1)
