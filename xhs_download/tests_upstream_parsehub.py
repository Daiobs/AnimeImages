"""Parent-only contracts. Real registry/types execute in the isolated suite."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from tools import upstream_parsehub_worker as worker
from xhs_download.services import upstream_parsehub as bridge

SOURCE = "https://example.com/content/123?required_signature=synthetic"
MATCH = {"provider_id": worker.PROVIDER_ID, "extractor_id": "AnyRegisteredParser",
         "label": "Example", "version": worker.PROVIDER_VERSION}


@override_settings(PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON="/isolated/python")
class ServiceTests(SimpleTestCase):
    def test_actual_matching_only_in_child(self):
        before = {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")}
        with patch.object(bridge, "_call", return_value=[MATCH]) as call:
            self.assertEqual(bridge.match_upstream_url(SOURCE), (MATCH,))
        call.assert_called_once_with({"action": "match", "url": SOURCE}, 10)
        self.assertEqual(before, {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")})

    @override_settings(PARSEHUB_UPSTREAM_ENABLED=False)
    def test_disabled_never_spawns(self):
        with patch.object(bridge, "run_external_command") as run:
            self.assertEqual(bridge.match_upstream_url(SOURCE), ())
            self.assertFalse(bridge.coverage_snapshot()["available"])
            run.assert_not_called()

    def test_catalog_comes_from_child(self):
        with patch.object(bridge, "_call", return_value={"provider_id": worker.PROVIDER_ID,
                          "version": worker.PROVIDER_VERSION, "entries": [MATCH]}) as call:
            data = bridge.coverage_snapshot()
        call.assert_called_once_with({"action": "catalog"}, 10)
        self.assertEqual(data["entries"], [MATCH])
        self.assertEqual(data["counts"]["total"], 1)

    def test_invalid_catalog_and_match_are_not_trusted(self):
        for value in [None, {}, [{**MATCH, "version": "changed"}], [{**MATCH, "extractor_id": "os.system"}]]:
            with patch.object(bridge, "_call", return_value=value):
                self.assertEqual(bridge.match_upstream_url(SOURCE), ())
                self.assertFalse(bridge.coverage_snapshot()["available"])

    def test_candidate_types_and_legacy_completeness_preserved(self):
        for completeness in worker.COMPLETENESS - {"richtext"}:
            rows = [[3, "http://media.example/a.m3u8", {"media_completeness": completeness,
                     "candidate_kind": "hls", "operation_restrictions": ["insecure_transport"]}]]
            with self.subTest(completeness=completeness), patch.object(bridge, "_call", return_value=rows):
                self.assertEqual(bridge.parse_upstream_url(MATCH, SOURCE), rows)
        rows = [[2, {"media_completeness": "richtext", "candidate_kind": "richtext", "content_summary": "text"}]]
        with patch.object(bridge, "_call", return_value=rows):
            self.assertEqual(bridge.parse_upstream_url(MATCH, SOURCE), rows)

    def test_forged_match_and_private_source_fail_before_child(self):
        with patch.object(bridge, "_call") as call:
            for match, url in [({**MATCH, "provider_id": "other"}, SOURCE),
                               (MATCH, "https://127.0.0.1/private")]:
                with self.assertRaisesRegex(bridge.ParseHubBridgeError, "invalid_parse_request"):
                    bridge.parse_upstream_url(match, url)
            call.assert_not_called()

    def test_malformed_private_output_denied(self):
        for rows in [[], [[3, "https://127.0.0.1/a", {"media_completeness": "unknown"}]],
                     [[3, "file:///etc/passwd", {"media_completeness": "unknown"}]],
                     [[2, {"candidate_kind": "richtext", "media_completeness": "richtext"}]],
                     [[3, "https://media.example/a", {"media_completeness": "invented"}]]]:
            with patch.object(bridge, "_call", return_value=rows):
                with self.assertRaisesRegex(bridge.ParseHubBridgeError, "invalid_payload"):
                    bridge.parse_upstream_url(MATCH, SOURCE)

    def test_pin_and_timeout(self):
        for version, pin in [("bad", worker.PIN), (worker.VERSION, "bad")]:
            dist = SimpleNamespace(version=version, read_text=lambda _: json.dumps({"vcs_info": {"commit_id": pin}}))
            with patch.object(worker.metadata, "distribution", return_value=dist):
                with self.assertRaisesRegex(ValueError, "version_changed"):
                    worker.verify_install()
        for timeout in [True, 0, 31, float("inf"), float("nan")]:
            with self.assertRaises(ValueError):
                worker.timeout_value(timeout)

    def test_safe_worker_errors(self):
        done = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": False, "error": "secret-url", "stage": "secret"}))
        with patch.object(bridge.Path, "is_file", return_value=True), patch.object(bridge, "run_external_command", return_value=done):
            with self.assertRaisesRegex(bridge.ParseHubBridgeError, "worker_failed"):
                bridge._call({"action": "catalog"}, 1)
