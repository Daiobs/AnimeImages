from __future__ import annotations

from collections import OrderedDict
import io
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tools.provider_lab.runtime.workers import videodl_worker, worker_common
from xhs_download.services.provider_lab.contracts import ProviderLabError
from xhs_download.services.provider_lab.runtime import parse_worker_stdout


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
