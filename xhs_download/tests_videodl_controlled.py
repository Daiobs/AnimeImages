from __future__ import annotations

from collections import OrderedDict
import io
import json
import re
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
from xhs_download.services.provider_lab.providers import (
    VIDEODL_INVENTORY_FAILURE_TTL_SECONDS,
    VIDEODL_INVENTORY_SINGLE_FLIGHT_SECONDS,
    VIDEODL_INVENTORY_SUCCESS_TTL_SECONDS,
    VIDEODL_PIN,
    list_videodl_backend_inventory,
    videodl_inventory_cache_key,
)
from xhs_download.services.provider_lab.runtime import parse_worker_stdout
from xhs_download.services.provider_lab.videodl_control import (
    MAX_BACKEND_IDS,
    VideodlControlledError,
    execute_controlled_videodl,
    load_videodl_inventory,
    normalize_inventory,
    parse_videodl_options,
    validate_inventory_policy_coverage,
)
from xhs_download.services.provider_lab.videodl_policy import (
    COMMON_BACKEND_POLICIES,
    DEDICATED_BACKEND_POLICIES,
    REVIEWED_COMMON_BACKEND_IDS,
    REVIEWED_DEDICATED_BACKEND_IDS,
    VIDEODL_POLICY_PIN,
    VIDEODL_POLICY_REVISION,
    backend_policy,
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
        self.assertTrue(inventory[0]["registry_available"])
        self.assertTrue(inventory[-1]["registry_available"])
        self.assertNotIn("external_sharing_required", inventory[0])

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

    def test_backend_main_emits_execution_identity_without_rewriting_native_source(self):
        for category, backend_id in (("common", "GVVideoClient"), ("dedicated", "AcFunVideoClient")):
            with self.subTest(category=category):
                native = SimpleNamespace(belongto=mock.Mock(return_value=True))
                client = SimpleNamespace(parsefromurl=mock.Mock(return_value=[SimpleNamespace(
                    source="native-display-label", title="offline", ext="mp4",
                    download_url="https://media.example.org/offline.mp4",
                    with_valid_download_url=True,
                )]))
                request = {"schema_version": "1.0", "request_id": "identity-fixture",
                    "source": "https://example.org/work", "max_assets": 16, "max_output_bytes": 1000000,
                    "operation": "backend", "backend_id": backend_id, "backend_category": category}
                output = io.BytesIO()
                stdout = io.TextIOWrapper(output, encoding="utf-8", write_through=True)
                with self._videodl_modules(**{category: {backend_id: native}},
                        build_video=mock.Mock(return_value=client), build_common=mock.Mock(return_value=client)), \
                        mock.patch.object(videodl_worker, "verify_install"), \
                        mock.patch.object(sys, "argv", ["videodl_worker.py"]), \
                        mock.patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                        mock.patch.object(sys, "stdout", stdout):
                    status = videodl_worker.main()
                self.assertEqual(status, 0, output.getvalue())
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["executed_backend"], {
                    "backend_id": backend_id, "backend_category": category})
                self.assertEqual(payload["result"]["selected_backend"], "native-display-label")
                client.parsefromurl.assert_called_once_with(request["source"], request_overrides={})


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
                "registry_available": True,
                "display_label": backend_id,
            }
            for category, backend_id in items
        ]

    @staticmethod
    def _success(backend_id):
        payload = {
            "result": {
                "status": "success",
                "selected_backend": "upstream-value",
                "warnings": [],
            },
            "resources": [{"asset_id": f"asset-{backend_id}"}],
            "metrics": {"candidate_count": 1},
        }
        if backend_id != "legacy_auto":
            payload["executed_backend"] = {
                "backend_id": backend_id,
                "backend_category": "common" if backend_id in REVIEWED_COMMON_BACKEND_IDS else "dedicated",
            }
        return payload

    @classmethod
    def _complete_policy_inventory(cls, inventory=()):
        complete = cls._inventory(
            *(
                ("dedicated", backend_id)
                for backend_id in REVIEWED_DEDICATED_BACKEND_IDS
            ),
            *(
                ("common", backend_id)
                for backend_id in REVIEWED_COMMON_BACKEND_IDS
            ),
        )
        by_identity = {
            (item["backend_category"], item["backend_id"]): item
            for item in complete
        }
        for item in inventory:
            by_identity[(item["backend_category"], item["backend_id"])] = item
        return list(by_identity.values())

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
        inventory = self._complete_policy_inventory(inventory)

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
        mismatched = self._inventory(("dedicated", "BilibiliVideoClient"))
        mismatched[0]["registry_available"] = "yes"
        with self.assertRaises(ProviderLabError):
            normalize_inventory(mismatched)

    def test_exact_pin_policy_covers_all_reviewed_dedicated_backends(self):
        self.assertEqual(
            VIDEODL_POLICY_PIN,
            "6cd1de0fe1209380b183b654c65e98a629637873",
        )
        self.assertEqual(VIDEODL_POLICY_PIN, VIDEODL_PIN)
        self.assertEqual(VIDEODL_POLICY_PIN, videodl_worker.VIDEODL_PIN)
        self.assertEqual(len(REVIEWED_DEDICATED_BACKEND_IDS), 70)
        self.assertEqual(
            set(REVIEWED_DEDICATED_BACKEND_IDS),
            set(DEDICATED_BACKEND_POLICIES),
        )
        self.assertEqual(len(REVIEWED_COMMON_BACKEND_IDS), 37)
        self.assertEqual(
            set(REVIEWED_COMMON_BACKEND_IDS),
            set(COMMON_BACKEND_POLICIES),
        )
        self.assertEqual(
            sum(
                policy.controlled_state == "requires_sharing_confirmation"
                for policy in COMMON_BACKEND_POLICIES.values()
            ),
            22,
        )
        self.assertEqual(
            sum(
                policy.controlled_state == "blocked"
                for policy in COMMON_BACKEND_POLICIES.values()
            ),
            15,
        )
        self.assertEqual(
            {
                policy.controlled_state
                for policy in COMMON_BACKEND_POLICIES.values()
            },
            {"requires_sharing_confirmation", "blocked"},
        )

    def test_inventory_registry_sets_must_exactly_match_local_policy(self):
        inventory = normalize_inventory(self._complete_policy_inventory())
        validate_inventory_policy_coverage(inventory)

        missing = tuple(
            item
            for item in inventory
            if item.backend_id != "BugPkVideoClient"
        )
        with self.assertRaises(ProviderLabError) as context:
            validate_inventory_policy_coverage(missing)
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

        extra = normalize_inventory(
            self._complete_policy_inventory(
                self._inventory(("common", "FutureCommonVideoClient"))
            )
        )
        with self.assertRaises(ProviderLabError) as context:
            validate_inventory_policy_coverage(extra)
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

        with self.assertRaises(ProviderLabError) as context:
            load_videodl_inventory(
                python="python",
                worker="worker.py",
                request_id="request_registry_drift",
                invoke=lambda **kwargs: {
                    "operation": kwargs["operation"],
                    "inventory": self._inventory(
                        ("dedicated", "BilibiliVideoClient")
                    ),
                },
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

    def test_unknown_dedicated_backend_defaults_blocked(self):
        policy = backend_policy(
            "FutureUnreviewedVideoClient",
            "dedicated",
            registry_present=True,
        )
        self.assertEqual(policy.controlled_state, "blocked")
        self.assertEqual(policy.policy_reason, "UNREVIEWED_BACKEND_BLOCKED")

    def test_unknown_common_backend_defaults_blocked(self):
        policy = backend_policy(
            "FutureUnreviewedCommonVideoClient",
            "common",
            registry_present=True,
        )
        self.assertEqual(policy.controlled_state, "blocked")
        self.assertEqual(
            policy.policy_reason,
            "UNREVIEWED_COMMON_BACKEND_BLOCKED",
        )

    def test_unadapted_tls_backend_is_blocked_before_any_worker_invocation(self):
        policy = COMMON_BACKEND_POLICIES["LongZhuVideoClient"]
        self.assertEqual(policy.controlled_state, "blocked")
        self.assertEqual(
            policy.policy_reason,
            "TLS_VERIFICATION_DISABLED_BLOCKED",
        )
        calls = []
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_bugpk_blocked",
                source="https://example.com/watch",
                options=self._options(
                    "specific_backend",
                    ids=("LongZhuVideoClient",),
                    category="common",
                    confirmed=True,
                ),
                invoke=self._invoke(inventory=[], calls=calls),
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")
        self.assertEqual(context.exception.attempts, ())
        self.assertEqual(calls, [])

    def test_reviewed_auto_cannot_run_blocked_common_backend(self):
        calls = []
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_reviewed_bugpk_blocked",
                source="https://example.com/watch",
                options=self._options(
                    "reviewed_auto",
                    ids=("LongZhuVideoClient",),
                    category="common",
                    confirmed=True,
                ),
                invoke=self._invoke(inventory=[], calls=calls),
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")
        self.assertEqual(context.exception.attempts, ())
        self.assertEqual(calls, [])

    def test_guarded_common_backend_still_requires_explicit_consent(self):
        policy = COMMON_BACKEND_POLICIES['BugPkVideoClient']
        self.assertEqual(policy.controlled_state, 'requires_sharing_confirmation')
        self.assertEqual(policy.policy_reason, 'TLS_VERIFIED_SHARING_ADAPTER')
        calls = []
        with self.assertRaises(ProviderLabError):
            execute_controlled_videodl(python='python', worker='worker.py', request_id='consent',
                source='https://example.com/watch',
                options=self._options('specific_backend', ids=('BugPkVideoClient',), category='common'),
                invoke=self._invoke(inventory=[], calls=calls))
        self.assertEqual(calls, [])

    def test_guarded_tls_adapter_enforces_verification_and_denies_http(self):
        import contextlib
        import sys
        import requests
        from types import SimpleNamespace
        from tools import upstream_match_worker as guard
        from tools.provider_lab.runtime.workers.videodl_worker import run_tls_guarded, WorkerError
        from unittest.mock import Mock, patch
        prepared = requests.Request('GET', 'https://example.com/api',
            headers={'X-Forwarded-For': '1.2.3.4'}).prepare()
        client = SimpleNamespace(parsefromurl=lambda *args, **kw:
            requests.Session().send(prepared, verify=False, proxies={'https': 'http://invalid'}))
        with patch.dict(sys.modules, {'upstream_match_worker': guard}), \
                patch.object(guard, 'install_network_guard') as install, \
                patch.object(guard, 'runtime_budget', return_value=contextlib.nullcontext()), \
                patch.object(requests.Session, 'send', return_value=['fixture']) as send:
            self.assertEqual(run_tls_guarded(client, 'https://example.com/watch'), ['fixture'])
            self.assertTrue(send.call_args.kwargs['verify'])
            self.assertEqual(send.call_args.kwargs['proxies'], {})
            self.assertNotIn('X-Forwarded-For', prepared.headers)
            send.reset_mock()
            prepared.url = 'http://example.com/api'
            with self.assertRaises(WorkerError):
                run_tls_guarded(client, 'https://example.com/watch')
            send.assert_not_called()
            prepared.url = 'https://127.0.0.1/private'
            with self.assertRaises(ValueError):
                run_tls_guarded(client, 'https://example.com/watch')
            send.assert_not_called()
        install.assert_called_with(offline=False)

    def test_dedicated_external_sharing_policy_is_not_category_derived(self):
        nested = DEDICATED_BACKEND_POLICIES["C56VideoClient"]
        youtube = DEDICATED_BACKEND_POLICIES["YouTubeVideoClient"]
        ordinary = DEDICATED_BACKEND_POLICIES["BilibiliVideoClient"]
        self.assertEqual(nested.controlled_state, "requires_sharing_confirmation")
        self.assertTrue(nested.external_sharing_required)
        self.assertEqual(youtube.controlled_state, "allowed")
        self.assertFalse(youtube.external_sharing_required)
        self.assertEqual(youtube.policy_reason, "OFFICIAL_ONLY_GUARDED_ADAPTER")
        self.assertFalse(ordinary.external_sharing_required)

        inventory = normalize_inventory(
            self._inventory(
                ("dedicated", "C56VideoClient"),
                ("dedicated", "KuaishouVideoClient"),
            )
        )
        self.assertTrue(inventory[0].registry_available)
        self.assertTrue(inventory[0].controlled_available)
        self.assertFalse(inventory[1].controlled_available)
        self.assertEqual(inventory[1].controlled_state, "blocked")

    def test_embedded_provider_credential_backend_is_blocked(self):
        policy = DEDICATED_BACKEND_POLICIES["DailyMotionVideoClient"]
        self.assertEqual(policy.controlled_state, "blocked")
        self.assertEqual(
            policy.policy_reason,
            "EMBEDDED_PROVIDER_CREDENTIAL_BLOCKED",
        )

    def test_all_browser_capable_dedicated_backends_are_blocked(self):
        browser_backends = {
            item.backend_id
            for item in DEDICATED_BACKEND_POLICIES.values()
            if item.browser_execution
        }
        self.assertEqual(
            browser_backends,
            {
                "KuaishouVideoClient",
                "MGTVVideoClient",
                "XinpianchangVideoClient",
            },
        )
        self.assertTrue(
            all(
                DEDICATED_BACKEND_POLICIES[backend_id].controlled_state == "blocked"
                for backend_id in browser_backends
            )
        )

    def test_dedicated_nested_backend_requires_explicit_sharing_confirmation(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "C56VideoClient")),
            calls=calls,
        )
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_nested_unconfirmed",
                source="https://example.com/watch",
                options=self._options(
                    "specific_backend",
                    ids=("C56VideoClient",),
                    category="dedicated",
                ),
                invoke=invoke,
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")
        self.assertEqual(calls, [])

        confirmed = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_nested_confirmed",
            source="https://example.com/watch",
            options=self._options(
                "specific_backend",
                ids=("C56VideoClient",),
                category="dedicated",
                confirmed=True,
            ),
            invoke=self._invoke(
                inventory=self._inventory(("dedicated", "C56VideoClient")),
            ),
        )
        self.assertTrue(confirmed.attempts[0].external_sharing)

    def test_blocked_browser_backend_is_rejected_before_backend_worker(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "KuaishouVideoClient")),
            calls=calls,
        )
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_browser_blocked",
                source="https://example.com/watch",
                options=self._options(
                    "specific_backend",
                    ids=("KuaishouVideoClient",),
                    category="dedicated",
                ),
                invoke=invoke,
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")
        self.assertEqual(calls, [])

    def test_reviewed_auto_skips_blocked_dedicated_backend(self):
        calls = []
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_reviewed_browser_skip",
            source="https://example.com/watch",
            options=self._options("reviewed_auto", category="common"),
            invoke=self._invoke(
                inventory=self._inventory(
                    ("dedicated", "KuaishouVideoClient"),
                    ("dedicated", "BilibiliVideoClient"),
                ),
                matches=("KuaishouVideoClient", "BilibiliVideoClient"),
                calls=calls,
            ),
        )
        backend_calls = [item for item in calls if item.get("operation") == "backend"]
        self.assertEqual(
            [item["backend_id"] for item in backend_calls],
            ["BilibiliVideoClient"],
        )
        self.assertIn("policy_skipped_dedicated_backends", result.result["warnings"])

    def test_dedicated_only_policy_skips_non_allowed_matches(self):
        calls = []
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_policy_skip_only",
                source="https://example.com/watch",
                options=self._options("dedicated_only", category="dedicated"),
                invoke=self._invoke(
                    inventory=self._inventory(
                        ("dedicated", "C56VideoClient"),
                        ("dedicated", "KuaishouVideoClient"),
                    ),
                    matches=("C56VideoClient", "KuaishouVideoClient"),
                    calls=calls,
                ),
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")
        self.assertEqual(context.exception.attempts, ())
        self.assertFalse(any(item.get("operation") == "backend" for item in calls))

    def test_youtube_official_adapter_is_selected_without_aggregator_consent(self):
        calls = []
        execute_controlled_videodl(
            python="python", worker="worker.py", request_id="policy_reason",
            source="https://example.com/watch",
            options=self._options("dedicated_only", category="dedicated"),
            invoke=self._invoke(inventory=self._inventory(("dedicated", "YouTubeVideoClient")),
                                matches=("YouTubeVideoClient",), calls=calls))
        self.assertTrue(any(item.get("operation") == "backend" for item in calls))

    def test_youtube_adapter_disables_nested_services_and_installs_guard(self):
        import contextlib
        import sys
        from unittest.mock import Mock, patch
        from tools import upstream_match_worker as guard
        from tools.provider_lab.runtime.workers.videodl_worker import run_youtube_official_only
        client = Mock()
        def parse(source, request_overrides):
            self.assertEqual(source, 'https://www.youtube.com/watch?v=abcdefghijk')
            self.assertEqual(client._parsefromurlwithytdown(source), [])
            self.assertEqual(client._parsefromurlwithdownr(source), [])
            return ['official-only']
        client.parsefromurl.side_effect = parse
        with patch.dict(sys.modules, {'upstream_match_worker': guard}), \
                patch.object(guard, 'install_network_guard') as install, \
                patch.object(guard, 'runtime_budget', return_value=contextlib.nullcontext()) as budget:
            self.assertEqual(run_youtube_official_only(client, 'https://www.youtube.com/shorts/abcdefghijk'), ['official-only'])
        install.assert_called_once_with(offline=False)
        budget.assert_called_once_with(8.0)

    def test_specific_backend_runs_exactly_one_backend_without_fallback(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(
                ("dedicated", "BilibiliVideoClient"),
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
                ids=("BilibiliVideoClient",),
                category="dedicated",
            ),
            invoke=invoke,
        )
        backend_calls = [item for item in calls if item.get("operation") == "backend"]
        self.assertEqual(
            [item["backend_id"] for item in backend_calls],
            ["BilibiliVideoClient"],
        )
        self.assertEqual(result.result["selected_backend"], "BilibiliVideoClient")
        self.assertEqual(result.result["native_selected_backend"], "upstream-value")
        self.assertEqual(len(result.attempts), 1)
        self.assertTrue(result.attempts[0].selected)
        self.assertFalse(result.attempts[0].external_sharing)

    def test_backend_execution_identity_is_required_before_display_label_override(self):
        for category, backend_id in (("common", "GVVideoClient"), ("dedicated", "AcFunVideoClient")):
            expected = {"backend_id": backend_id, "backend_category": category}
            for identity in (None, {}, "GVVideoClient", {**expected, "backend_id": "AnyFetcherVideoClient"},
                    {**expected, "backend_category": "dedicated" if category == "common" else "common"},
                    {**expected, "unexpected": True}):
                with self.subTest(category=category, identity=identity):
                    payload = self._success(backend_id)
                    payload["result"]["selected_backend"] = "AnyFetcherVideoClient"
                    if identity is None:
                        payload.pop("executed_backend")
                    else:
                        payload["executed_backend"] = identity
                    with self.assertRaises(VideodlControlledError) as caught:
                        execute_controlled_videodl(python="python", worker="worker.py", request_id="mismatch",
                            source="https://example.org/work", options=self._options("specific_backend",
                                ids=(backend_id,), category=category, confirmed=category == "common"),
                            invoke=self._invoke(inventory=[], backend_results={backend_id: payload}))
                    self.assertEqual(caught.exception.attempts[0].safe_failure_code, "BACKEND_SCHEMA_INVALID")
                    self.assertFalse(caught.exception.attempts[0].selected)
                    self.assertEqual(payload["result"]["selected_backend"], "AnyFetcherVideoClient")

    def test_legacy_auto_remains_explicit_opaque_execution(self):
        calls = []
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_legacy_opaque",
            source="https://example.com/watch",
            options=self._options("legacy_auto"),
            invoke=self._invoke(inventory=[], calls=calls),
        )
        self.assertEqual(
            [item.get("operation") for item in calls],
            ["legacy_auto"],
        )
        self.assertIn("control_level_opaque_legacy", result.result["warnings"])
        self.assertEqual(result.attempts, ())

    def test_specific_unknown_or_category_mismatched_backend_fails_closed(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "BilibiliVideoClient")),
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
                ("dedicated", "BilibiliVideoClient"),
                ("dedicated", "DouyinVideoClient"),
            ),
            matches=("BilibiliVideoClient", "DouyinVideoClient"),
            backend_results={
                "BilibiliVideoClient": ProviderLabError("PROVIDER_RESULT_EMPTY"),
                "DouyinVideoClient": self._success("DouyinVideoClient"),
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
            [("BilibiliVideoClient", "empty"), ("DouyinVideoClient", "success")],
        )
        self.assertFalse(any(item.get("backend_category") == "common" for item in calls))
        self.assertFalse(any(item.get("operation") == "grabber" for item in calls))

    def test_dedicated_match_rejects_unknown_worker_backend(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "BilibiliVideoClient")),
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
            inventory=self._inventory(("dedicated", "BilibiliVideoClient")),
            matches=("BilibiliVideoClient",),
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

    def test_total_deadline_retains_completed_attempt_before_next_backend(self):
        calls = []
        current_time = [0.0]

        def invoke(**kwargs):
            calls.append(dict(kwargs))
            operation = kwargs.get("operation")
            if operation == "inventory":
                return {
                    "operation": operation,
                    "inventory": self._complete_policy_inventory(),
                }
            if operation == "match_dedicated":
                return {
                    "operation": operation,
                    "backend_ids": [
                        "BilibiliVideoClient",
                        "DouyinVideoClient",
                    ],
                }
            if kwargs.get("backend_id") == "BilibiliVideoClient":
                current_time[0] = 0.9
                raise ProviderLabError("PROVIDER_RESULT_EMPTY")
            raise AssertionError("the next backend must not be started")

        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_retained_deadline",
                source="https://example.com/watch",
                options=self._options("dedicated_only", category="dedicated"),
                total_timeout_seconds=1.0,
                invoke=invoke,
                clock=lambda: current_time[0],
            )
        self.assertEqual(context.exception.code, "PROVIDER_TIMEOUT")
        self.assertEqual(
            [(item.backend_id, item.outcome) for item in context.exception.attempts],
            [("BilibiliVideoClient", "empty")],
        )
        self.assertEqual(
            [
                item.get("backend_id")
                for item in calls
                if item.get("operation") == "backend"
            ],
            ["BilibiliVideoClient"],
        )

    def test_common_only_runs_only_selected_ids_and_marks_external_sharing(self):
        calls = []
        invoke = self._invoke(
            inventory=self._inventory(
                ("common", "AnyFetcherVideoClient"),
                ("common", "KuKuToolVideoClient"),
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
                ids=("AnyFetcherVideoClient",),
                category="common",
                confirmed=True,
            ),
            invoke=invoke,
        )
        self.assertEqual(result.attempts[0].backend_id, "AnyFetcherVideoClient")
        self.assertTrue(result.attempts[0].external_sharing)
        self.assertFalse(
            any(item.get("backend_id") == "KuKuToolVideoClient" for item in calls)
        )

    def test_specific_common_backend_still_requires_explicit_confirmation(self):
        with self.assertRaises(ProviderLabError) as context:
            self._options(
                "specific_backend",
                ids=("AnyFetcherVideoClient",),
                category="common",
            )
        self.assertEqual(context.exception.code, "PROVIDER_POLICY_REJECTED")

        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_common_confirmed",
            source="https://example.com/watch",
            options=self._options(
                "specific_backend",
                ids=("AnyFetcherVideoClient",),
                category="common",
                confirmed=True,
            ),
            invoke=self._invoke(inventory=[]),
        )
        self.assertTrue(result.attempts[0].external_sharing)

    def test_failed_common_attempt_retains_external_sharing_fact(self):
        with self.assertRaises(VideodlControlledError) as context:
            execute_controlled_videodl(
                python="python",
                worker="worker.py",
                request_id="request_common_failed",
                source="https://example.com/watch",
                options=self._options(
                    "common_only",
                    ids=("AnyFetcherVideoClient",),
                    category="common",
                    confirmed=True,
                ),
                invoke=self._invoke(
                    inventory=[],
                    backend_results={
                        "AnyFetcherVideoClient": ProviderLabError(
                            "PROVIDER_RESULT_EMPTY"
                        )
                    },
                ),
            )
        self.assertTrue(context.exception.attempts[0].external_sharing)

    def test_reviewed_auto_runs_dedicated_then_selected_common(self):
        invoke = self._invoke(
            inventory=self._inventory(
                ("dedicated", "BilibiliVideoClient"),
                ("common", "AnyFetcherVideoClient"),
            ),
            matches=("BilibiliVideoClient",),
            backend_results={
                "BilibiliVideoClient": ProviderLabError("PROVIDER_PROCESS_FAILED"),
                "AnyFetcherVideoClient": self._success("AnyFetcherVideoClient"),
            },
        )
        result = execute_controlled_videodl(
            python="python",
            worker="worker.py",
            request_id="request_reviewed",
            source="https://example.com/watch",
            options=self._options(
                "reviewed_auto",
                ids=("AnyFetcherVideoClient",),
                category="common",
                confirmed=True,
            ),
            invoke=invoke,
        )
        self.assertEqual(
            [(item.backend_id, item.outcome) for item in result.attempts],
            [
                ("BilibiliVideoClient", "failed"),
                ("AnyFetcherVideoClient", "success"),
            ],
        )
        self.assertTrue(result.attempts[-1].selected)

    def test_failure_retains_bounded_safe_attempts(self):
        invoke = self._invoke(
            inventory=self._inventory(("dedicated", "BilibiliVideoClient")),
            backend_results={
                "BilibiliVideoClient": ProviderLabError("PROVIDER_TIMEOUT"),
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
                    ids=("BilibiliVideoClient",),
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
                    "backend_id": "BilibiliVideoClient",
                    "backend_category": "dedicated",
                    "registry_available": True,
                    "controlled_state": "allowed",
                    "controlled_available": True,
                    "external_sharing_required": False,
                    "browser_execution": False,
                    "policy_reason": "EXACT_PIN_REVIEWED",
                    "display_label": "Bilibili Video Client",
                },
                {
                    "backend_id": "CommonBackend",
                    "backend_category": "common",
                    "registry_available": True,
                    "controlled_state": "requires_sharing_confirmation",
                    "controlled_available": True,
                    "external_sharing_required": True,
                    "browser_execution": False,
                    "policy_reason": "COMMON_EXTERNAL_SHARING_CONFIRMATION_REQUIRED",
                    "display_label": "Common Backend",
                },
                {
                    "backend_id": "BugPkVideoClient",
                    "backend_category": "common",
                    "registry_available": True,
                    "controlled_state": "blocked",
                    "controlled_available": False,
                    "external_sharing_required": True,
                    "browser_execution": False,
                    "policy_reason": "TLS_VERIFICATION_DISABLED_BLOCKED",
                    "display_label": "BugPk Video Client",
                },
            ],
        }
        response = self.client.get(reverse("provider_lab"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("videodl 受控执行", content)
        self.assertIn("CommonBackend", content)
        self.assertIn("BugPkVideoClient", content)
        self.assertIn("外部服务", content)
        self.assertNotIn("https://", content)
        self.assertNotIn("module path", content)
        blocked_option = re.search(
            r'<option(?=[^>]*value="BugPkVideoClient")[^>]*>',
            content,
        )
        self.assertIsNotNone(blocked_option)
        self.assertIn("disabled", blocked_option.group(0))
        blocked_checkbox = re.search(
                r'<input(?=[^>]*name="videodl_common_backends")'
                r'(?=[^>]*value="BugPkVideoClient")[^>]*>',
                content,
            )
        self.assertIsNotNone(blocked_checkbox)
        self.assertIn('disabled', blocked_checkbox.group(0))
        self.assertIn('完整 Backend 状态清单', content)

    @mock.patch("xhs_download.services.provider_lab.providers.load_videodl_inventory")
    @mock.patch("xhs_download.services.provider_lab.providers._external_python")
    def test_two_staff_page_loads_share_one_inventory_worker(
        self,
        python_mock,
        load_mock,
    ):
        python_mock.return_value = sys.executable
        load_mock.return_value = normalize_inventory(
            VideodlControlledOrchestrationTests._inventory(
                ("dedicated", "BilibiliVideoClient"),
            )
        )
        first = self.client.get(reverse("provider_lab"))
        second = self.client.get(reverse("provider_lab"))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        load_mock.assert_called_once()
        inventory_payload = first.context["videodl_backend_inventory"]
        self.assertNotIn("http", json.dumps(inventory_payload).lower())

    @mock.patch("xhs_download.services.provider_lab.providers.load_videodl_inventory")
    @mock.patch("xhs_download.services.provider_lab.providers._external_python")
    def test_inventory_cache_expiry_allows_reload(self, python_mock, load_mock):
        python_mock.return_value = sys.executable
        load_mock.return_value = normalize_inventory(
            VideodlControlledOrchestrationTests._inventory(
                ("dedicated", "BilibiliVideoClient"),
            )
        )
        first = list_videodl_backend_inventory()
        self.assertEqual(first["status"], "ready")
        cache.clear()
        second = list_videodl_backend_inventory()
        self.assertEqual(second["status"], "ready")
        self.assertEqual(load_mock.call_count, 2)
        self.assertEqual(VIDEODL_INVENTORY_SUCCESS_TTL_SECONDS, 600)

    def test_policy_revision_changes_inventory_cache_key(self):
        self.assertEqual(VIDEODL_POLICY_REVISION, "guarded-tls-v3-full-inventory")
        initial = videodl_inventory_cache_key(
            python=sys.executable,
            worker=videodl_worker.__file__,
        )
        with mock.patch(
            "xhs_download.services.provider_lab.providers.VIDEODL_POLICY_REVISION",
            "dedicated-v1-common-v2",
        ):
            changed = videodl_inventory_cache_key(
                python=sys.executable,
                worker=videodl_worker.__file__,
            )
        self.assertNotEqual(initial, changed)
        self.assertTrue(initial.startswith("provider-lab:videodl-inventory:v3:"))

    @mock.patch("xhs_download.services.provider_lab.providers.cache.set")
    @mock.patch("xhs_download.services.provider_lab.providers.load_videodl_inventory")
    @mock.patch("xhs_download.services.provider_lab.providers._external_python")
    def test_inventory_failure_uses_short_safe_cache_entry(
        self,
        python_mock,
        load_mock,
        cache_set_mock,
    ):
        python_mock.return_value = sys.executable
        load_mock.side_effect = ProviderLabError("PROVIDER_PROCESS_FAILED")
        result = list_videodl_backend_inventory()
        self.assertEqual(result, {"status": "unavailable", "items": []})
        self.assertEqual(VIDEODL_INVENTORY_FAILURE_TTL_SECONDS, 30)
        self.assertEqual(cache_set_mock.call_args.kwargs["timeout"], 30)
        self.assertNotIn("http", json.dumps(cache_set_mock.call_args.args[1]).lower())

    @mock.patch("xhs_download.services.provider_lab.providers.cache.add")
    @mock.patch("xhs_download.services.provider_lab.providers.load_videodl_inventory")
    @mock.patch("xhs_download.services.provider_lab.providers._external_python")
    def test_inventory_single_flight_does_not_start_a_second_worker(
        self,
        python_mock,
        load_mock,
        cache_add_mock,
    ):
        python_mock.return_value = sys.executable
        cache_add_mock.return_value = False
        result = list_videodl_backend_inventory()
        self.assertEqual(result, {"status": "loading", "items": []})
        self.assertEqual(VIDEODL_INVENTORY_SINGLE_FLIGHT_SECONDS, 10)
        load_mock.assert_not_called()

    @mock.patch("xhs_download.services.provider_lab.providers.load_videodl_inventory")
    def test_anonymous_and_non_staff_page_loads_do_not_start_inventory_worker(
        self,
        load_mock,
    ):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)
        non_staff = get_user_model().objects.create_user(
            username="videodl-lab-non-staff",
            password="secret",
        )
        self.client.force_login(non_staff)
        self.assertEqual(self.client.get(reverse("provider_lab")).status_code, 404)
        load_mock.assert_not_called()

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

    @mock.patch("xhs_download.services.provider_lab.runtime.worker_install_available", return_value=True)
    @mock.patch("xhs_download.services.quality_execution._python_for", return_value="/isolated/python")
    @mock.patch("xhs_download.services.provider_lab.providers.execute_controlled_videodl")
    def test_runtime_failure_retains_safe_attempts(self, execute_mock, python_mock, install_mock):
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
