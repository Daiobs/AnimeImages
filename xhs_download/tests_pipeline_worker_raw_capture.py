"""Synthetic offline boundaries: no provider request, config, or recorded claims."""
import asyncio
from concurrent.futures import wait
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from tools import upstream_match_worker as match_worker
from tools import worker_raw_sideband as wire
from tools.parsehub_provider import parsehub_xhs_image_worker as xhs_worker
from tools.provider_lab.runtime.workers import videodl_worker
from .services import upstream_matching, upstream_parsehub, worker_raw_capture as transport
from .services.pipeline import Failure, FailureKind, Work, validate_work
from .services.pipeline_shadow_capture import collect_raw, current_capture
from .services.platforms import gallerydl_generic, ytdlp_generic, xhs_parsehub_images
from .services.platforms.external_tools import run_external_command
from .services.provider_adapters.gallerydl import GalleryDlAdapter
from .services.provider_adapters.parsehub import ParseHubAdapter, ParseHubXhsAdapter
from .services.provider_adapters.videodl import VideodlAdapter
from .services.provider_adapters.ytdlp import YtDlpWorkerAdapter
from .services.provider_adapters.worker_raw import captured_fetch, decode_raw, worker_failure
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.douyin.com/video/123456789"
MEDIA = "https://cdn.example/video.mp4"
NOTE = "0123456789abcdef01234567"
XHS_SOURCE = "https://www.xiaohongshu.com/explore/" + NOTE


def native(module, name, **fields):
    obj = type(name, (), {"__module__": module})()
    vars(obj).update(fields)
    return obj


def info():
    return {"id": "123456789", "title": "Synthetic", "upstream_only": {"keep": [1, 2]},
            "formats": [{"url": MEDIA, "ext": "mp4", "height": 720},
                        {"url": "https://cdn.example/full.mp4", "ext": "mp4", "height": 1080}]}


def page():
    return {"note": {"noteDetailMap": {NOTE: {"note": {
        "noteId": NOTE, "type": "normal", "title": "Synthetic", "imageList": [
            {"urlDefault": "https://sns-img-hw.xhscdn.com/synthetic.jpg"}],
        "upstream_only": {"keep": [1, 2]},
    }}}}, "other_page_facts": {"complete": True}}


def send(request, raws):
    if "_shadow_capture_fd" not in request:
        return
    observer = wire.Observer(os.dup(request["_shadow_capture_fd"]))
    for raw in raws:
        observer(raw)
    observer.finish()


def settled(session):
    _, pending = wait(session.futures, timeout=2) if session.futures else ((), ())
    if pending:
        raise AssertionError("capture did not settle")


class OfflineCase(SimpleTestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        self.network.start()
        self.addCleanup(self.network.stop)


class RawSidebandTests(OfflineCase):
    def test_disabled_has_no_pipe_snapshot_future_or_payload_change(self):
        request = {"action": "parse", "url": SOURCE}
        runner = Mock(return_value="legacy")
        with patch.object(transport.os, "pipe", side_effect=AssertionError("off")), \
                patch.object(transport, "submit_with_capture", side_effect=AssertionError("off")):
            self.assertEqual(transport.run_with_capture(runner, ["worker"], request, "provider", SOURCE), "legacy")
        self.assertEqual(runner.call_args.kwargs, {"input_text": json.dumps(request)})

    def test_non_parse_operations_never_capture_even_in_session(self):
        with collect_raw() as session, patch.object(transport.os, "pipe", side_effect=AssertionError("inventory")):
            transport.run_with_capture(Mock(), ["worker"], {}, "provider", "", enabled=False)
        self.assertEqual(session.futures, [])

    def test_actual_subprocess_raw_larger_than_legacy_limit_is_not_stdout(self):
        helper = Path(wire.__file__).resolve()
        script = ("import json,sys,runpy\n"
                  "request=json.load(sys.stdin)\n"
                  f"observe=runpy.run_path({str(helper)!r})['observer'](request)\n"
                  "observe({'upstream_only':'s'*6000})\n"
                  "observe.finish()\n"
                  "print('{\"ok\":true,\"payload\":[1]}')\n")
        with collect_raw() as session:
            result = transport.run_with_capture(run_external_command, [sys.executable, "-I", "-B", "-c", script],
                {}, "provider", SOURCE, timeout_seconds=2, max_stdout_bytes=1024, max_stderr_bytes=1024)
        settled(session)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "payload": [1]})
        self.assertEqual(result.stderr, "")
        self.assertEqual(decode_raw(session.records[0].raw), {"upstream_only": "s" * 6000})

    def test_slow_reader_returns_before_future_and_retains_original_context(self):
        release, entered = Event(), Event()
        receive = transport._receive
        def slow(*args):
            entered.set()
            release.wait(2)
            receive(*args)
        runner = lambda command, **kw: (send(json.loads(kw["input_text"]), [info()]), "legacy")[1]
        try:
            with patch.object(transport, "_receive", side_effect=slow), collect_raw() as first:
                start = time.monotonic()
                result = transport.run_with_capture(runner, [], {}, "provider", SOURCE)
                elapsed = time.monotonic() - start
                self.assertEqual(result, "legacy")
                self.assertLess(elapsed, 0.1)
                self.assertTrue(entered.wait(1))
                self.assertEqual(len(first.futures), 1)
                self.assertFalse(first.futures[0].done())
            with collect_raw() as other:
                release.set()
                settled(first)
                self.assertEqual(other.records, ())
            self.assertEqual(decode_raw(first.records[0].raw), info())
        finally:
            release.set()

    def test_full_pipe_drops_shadow_without_waiting(self):
        observer = wire.Observer(100)
        observer(info())
        with patch.object(wire.os, "set_blocking"), patch.object(wire.os, "close"), \
                patch.object(wire.os, "write", side_effect=BlockingIOError) as write:
            start = time.monotonic()
            observer.finish()
            self.assertLess(time.monotonic() - start, 0.05)
        write.assert_called_once()

    def test_truncated_oversized_and_missing_sidebands_only_record_error(self):
        for value, limit in ((b'{"worker_raw_v1":[', 1000), (b"x" * 100, 10), (b"", 1000)):
            with self.subTest(value=value), patch.object(transport, "MAX_RAW_BYTES", limit), collect_raw() as session:
                def runner(command, **kwargs):
                    request = json.loads(kwargs["input_text"])
                    os.write(request["_shadow_capture_fd"], value)
                    return {"old": "unchanged"}
                self.assertEqual(transport.run_with_capture(runner, [], {}, "provider", SOURCE), {"old": "unchanged"})
            settled(session)
            self.assertEqual(session.records, ())
            self.assertEqual(session.errors, {"provider"})

    def test_pipe_allocation_failure_is_fail_open(self):
        with collect_raw() as session, patch.object(transport.os, "pipe", side_effect=OSError("secret")):
            runner = Mock(return_value="legacy")
            self.assertEqual(transport.run_with_capture(runner, [], {}, "provider", SOURCE), "legacy")
            self.assertNotIn("pass_fds", runner.call_args.kwargs)
        self.assertEqual(session.errors, {"provider"})

    def test_large_raw_short_write_reports_error_without_changing_legacy_result(self):
        release = Event()
        receive = transport._receive
        def slow(*args):
            release.wait(2)
            receive(*args)
        def runner(command, **kwargs):
            send(json.loads(kwargs["input_text"]), [{"large": "x" * (wire.MAX_BYTES // 2)}])
            return {"legacy": "complete"}
        try:
            with patch.object(transport, "_receive", side_effect=slow), collect_raw() as session:
                self.assertEqual(transport.run_with_capture(runner, [], {}, "provider", SOURCE),
                                 {"legacy": "complete"})
                self.assertFalse(session.futures[0].done())
        finally:
            release.set()
        settled(session)
        self.assertEqual(session.records, ())
        self.assertEqual(session.errors, {"provider"})

    def test_snapshot_is_complete_independent_and_never_advances_lazy_values(self):
        raw = {"timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc), "bytes": b"secret", "nested": [1]}
        encoded = {"worker_raw_v1": [wire.snapshot(raw)]}
        before = deepcopy(raw)
        raw["nested"].append(2)
        with deny_map_io():
            self.assertEqual(decode_raw(encoded), before)
        class Lazy:
            def __iter__(self):
                raise AssertionError("must not enumerate")
        observer = wire.Observer(100)
        observer({"entries": Lazy()})
        self.assertTrue(observer.failed)
        self.assertEqual(observer.records, [])

    def test_snapshot_rejects_callable_or_cycle_instead_of_partial_raw(self):
        cycle = {}
        cycle["self"] = cycle
        for raw in (cycle, {"callback": lambda: None}, {"number": float("nan")}):
            observer = wire.Observer(100)
            observer(raw)
            self.assertTrue(observer.failed)
            self.assertEqual(observer.records, [])

    def test_fetch_alone_waits_with_its_own_budget(self):
        release = Event()
        receive = transport._receive
        def slow(*args):
            release.wait(2)
            receive(*args)
        sessions = []
        def invoke():
            sessions.append(current_capture())
            return transport.run_with_capture(
                lambda cmd, **kw: send(json.loads(kw["input_text"]), [info()]), [], {}, "provider", SOURCE)
        try:
            with patch.object(transport, "_receive", side_effect=slow):
                result = captured_fetch("provider", SOURCE, 0.02, invoke, prefix="TEST")
            self.assertEqual(result, Failure(FailureKind.TIMEOUT, "TEST_TIMEOUT"))
        finally:
            release.set()
            for session in sessions:
                settled(session)


class WorkerFetchTests(OfflineCase):
    def match(self, provider):
        return {"provider_id": provider, "extractor_id": "Synthetic", "version": "pinned", "label": "Synthetic"}

    def test_ytdlp_worker_fetch_and_parse_keep_raw_formats_once(self):
        raw, legacy = info(), {"formats": [{"url": MEDIA}]}
        def boundary(command, **kwargs):
            request = json.loads(kwargs["input_text"])
            send(request, [raw])
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "payload": legacy}), "")
        adapter = YtDlpWorkerAdapter(match=self.match("yt_dlp_generic"))
        with patch.object(upstream_matching, "_context", return_value=(["worker"], "pinned", "identity")), \
                patch.object(upstream_matching, "run_external_command", side_effect=boundary) as call:
            fetched = adapter.fetch(SOURCE, 2)
            self.assertEqual(fetched, raw)
            fetched["upstream_only"]["keep"].append(3)
            self.assertEqual(raw["upstream_only"]["keep"], [1, 2])
            call.reset_mock()
            work = adapter.parse(SOURCE, 2)
            self.assertIsInstance(work, Work)
            self.assertEqual(len(work.items[0].variants), 2)
            self.assertEqual(validate_work(work), ())
            call.assert_called_once()

    def test_gallery_worker_fetch_uses_raw_messages_not_projected_rows(self):
        raw = [(2, {"post_id": "123456789", "title": "Synthetic", "upstream_only": [1]}),
               (3, MEDIA, {"post_id": "123456789", "extension": "mp4", "height": 720})]
        def boundary(command, **kwargs):
            send(json.loads(kwargs["input_text"]), raw)
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "payload": []}), "")
        adapter = GalleryDlAdapter(match=self.match("gallery_dl_generic"))
        with patch.object(upstream_matching, "_context", return_value=(["worker"], "pinned", "identity")), \
                patch.object(upstream_matching, "run_external_command", side_effect=boundary) as call:
            fetched = adapter.fetch(SOURCE, 2)
            self.assertEqual(fetched, raw)
            with deny_map_io():
                self.assertIsInstance(adapter.map(fetched, SOURCE), Work)
        call.assert_called_once()

    def test_successful_old_output_without_raw_never_refetches(self):
        for cls, provider, prefix in ((YtDlpWorkerAdapter, "yt_dlp_generic", "YTDLP"),
                                      (GalleryDlAdapter, "gallery_dl_generic", "GALLERYDL")):
            with self.subTest(provider=provider), \
                    patch.object(upstream_matching, "_context", return_value=(["worker"], "pinned", "identity")), \
                    patch.object(upstream_matching, "run_external_command", return_value=
                        subprocess.CompletedProcess([], 0, '{"ok":true,"payload":{}}', "")) as call:
                result = cls(match=self.match(provider)).fetch(SOURCE, 2)
                self.assertEqual(result, Failure(FailureKind.INVALID_RESULT, prefix + "_NO_RAW"))
                call.assert_called_once()

    def test_raw_captured_before_legacy_normalization_raises_still_reaches_mapper(self):
        for code in ("no_formats", "unsupported_result"):
            with self.subTest(code=code):
                raw = native("parsehub.types.result", "VideoParseResult", raw_url=SOURCE,
                    media=native("parsehub.types.media_ref", "VideoRef", url=MEDIA, ext="mp4"),
                    upstream_only={"preserved": True})
                match = {**self.match("parsehub_generic"), "version": upstream_parsehub.PROVIDER_VERSION}
                def boundary(command, **kwargs):
                    send(json.loads(kwargs["input_text"]), [raw])
                    return subprocess.CompletedProcess(command, 0, json.dumps({
                        "ok": False, "error": code, "stage": "parse", "error_class": "ValueError"}), "")
                with patch.object(upstream_parsehub, "runtime_bool", return_value=True), \
                        patch.object(upstream_parsehub, "runtime_str", return_value=sys.executable), \
                        patch.object(upstream_parsehub, "run_external_command", side_effect=boundary) as call:
                    adapter = ParseHubAdapter(match=match)
                    fetched = adapter.fetch(SOURCE, 2)
                    self.assertEqual(fetched["fields"]["upstream_only"], {"preserved": True})
                    with deny_map_io():
                        self.assertIsInstance(adapter.map(fetched, SOURCE), Work)
                call.assert_called_once()

    def test_exhausted_budget_never_enters_bridge(self):
        with patch.object(upstream_matching, "parse_upstream_url") as bridge:
            result = YtDlpWorkerAdapter(match=self.match("yt_dlp_generic")).fetch(SOURCE, 0)
        self.assertIs(result.kind, FailureKind.TIMEOUT)
        bridge.assert_not_called()

    def test_safe_failure_classification_contains_no_worker_text(self):
        for code, kind in (("execution_timeout", FailureKind.TIMEOUT),
                           ("auth_required", FailureKind.UNAVAILABLE),
                           ("no_formats", FailureKind.INVALID_RESULT),
                           ("extractor_mismatch", FailureKind.UNSUPPORTED)):
            result = worker_failure(SimpleNamespace(code=code), "WORKER")
            self.assertIs(result.kind, kind)
        self.assertNotIn("secret", repr(worker_failure(ValueError("Cookie=secret"), "WORKER")))

    def test_parsehub_fetch_decodes_full_native_object_without_runtime_import(self):
        raw = native("parsehub.types.result", "VideoParseResult", raw_url=SOURCE, title="Synthetic",
                     media=native("parsehub.types.media_ref", "VideoRef", url=MEDIA, ext="mp4", height=720),
                     upstream_only={"secret": "memory only"})
        match = {**self.match("parsehub_generic"), "version": upstream_parsehub.PROVIDER_VERSION}
        old = [[3, MEDIA, {"media_completeness": "unknown"}]]
        def boundary(command, **kwargs):
            send(json.loads(kwargs["input_text"]), [raw])
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "payload": old}), "")
        with patch.object(upstream_parsehub, "runtime_bool", return_value=True), \
                patch.object(upstream_parsehub, "runtime_str", return_value=sys.executable), \
                patch.object(upstream_parsehub, "run_external_command", side_effect=boundary) as call:
            result = ParseHubAdapter(match=match).parse(SOURCE, 2)
        self.assertIsInstance(result, Work)
        self.assertEqual(result.items[0].default_variant.height, 720)
        self.assertNotIn("memory only", repr(result))
        call.assert_called_once()

    def test_xhs_cache_projection_is_no_raw_and_no_worker_request(self):
        with patch.object(xhs_parsehub_images, "get_cached_external_payload", return_value={"items": [1]}), \
                patch.object(xhs_parsehub_images, "run_external_command") as run:
            result = ParseHubXhsAdapter().fetch(XHS_SOURCE, 2)
        self.assertEqual(result, Failure(FailureKind.INVALID_RESULT, "PARSEHUB_XHS_NO_RAW"))
        run.assert_not_called()

    def test_xhs_parent_bridge_preserves_payload_and_captures_whole_page(self):
        old = {"schema_version": "1.0", "provider": "parsehub_xhs_images",
               "provider_version": xhs_parsehub_images.PARSEHUB_PROVIDER_VERSION,
               "claimed_platform": "xiaohongshu", "claimed_note_id": NOTE, "post_type": "image", "items": [{}]}
        def boundary(command, **kwargs):
            send(json.loads(kwargs["input_text"]), [page()])
            return subprocess.CompletedProcess(command, 0, json.dumps(old), "")
        with patch.object(xhs_parsehub_images, "runtime_str", return_value=sys.executable), \
                patch.object(xhs_parsehub_images, "run_external_command", side_effect=boundary) as call, \
                collect_raw() as session:
            result = xhs_parsehub_images._run_worker(XHS_SOURCE, NOTE, deadline_monotonic=time.monotonic() + 2)
        settled(session)
        self.assertEqual(result, old)
        self.assertEqual(decode_raw(session.records[0].raw), page())
        with deny_map_io():
            self.assertIsInstance(ParseHubXhsAdapter().map(session.records[0].raw, XHS_SOURCE), Work)
        call.assert_called_once()

    def test_xhs_adapter_parse_uses_single_existing_worker_and_no_probe(self):
        old = {"schema_version": "1.0", "provider": "parsehub_xhs_images",
               "provider_version": xhs_parsehub_images.PARSEHUB_PROVIDER_VERSION,
               "claimed_platform": "xiaohongshu", "claimed_note_id": NOTE, "post_type": "image", "items": [{}]}
        def boundary(command, **kwargs):
            send(json.loads(kwargs["input_text"]), [page()])
            return subprocess.CompletedProcess(command, 0, json.dumps(old), "")
        with patch.object(xhs_parsehub_images, "runtime_str", return_value=sys.executable), \
                patch.object(xhs_parsehub_images, "get_cached_external_payload", return_value=None), \
                patch.object(xhs_parsehub_images, "get_cached_external_failure", return_value=None), \
                patch.object(xhs_parsehub_images, "cache"), \
                patch.object(xhs_parsehub_images, "cache_external_payload"), \
                patch.object(xhs_parsehub_images, "clear_cached_external_failure"), \
                patch.object(xhs_parsehub_images, "run_external_command", side_effect=boundary) as call:
            result = ParseHubXhsAdapter().parse(XHS_SOURCE, 2)
        self.assertIsInstance(result, Work)
        self.assertEqual(result.work_id, NOTE)
        call.assert_called_once()

    def test_videodl_explicit_backend_uses_controlled_bridge_and_actual_metadata(self):
        from .services.provider_lab import providers, runtime
        from .services.provider_lab.videodl_policy import REVIEWED_COMMON_BACKEND_IDS, REVIEWED_DEDICATED_BACKEND_IDS
        backend = "DouyinVideoClient"
        raw = native("videodl.modules.utils.data", "VideoInfo", source="display name", title="Synthetic",
                     download_url=MEDIA, raw_data={"upstream_only": [1]}, _extra={})
        requests = []
        def process(args, **kwargs):
            request = json.load(kwargs["stdin"])
            requests.append(request)
            if request["operation"] == "inventory":
                payload = {"schema_version": "1.0", "operation": "inventory", "inventory": [
                    {"backend_id": name, "backend_category": category, "registry_available": True,
                     "display_label": name} for category, names in (("dedicated", REVIEWED_DEDICATED_BACKEND_IDS),
                        ("common", REVIEWED_COMMON_BACKEND_IDS)) for name in names]}
            else:
                send(request, [[raw]])
                payload = {"schema_version": "1.0", "result": {"status": "success"}, "resources": [],
                           "metrics": {"candidate_count": 1}, "executed_backend": {
                               "backend_id": backend, "backend_category": "dedicated"}}
            kwargs["stdout"].write(json.dumps(payload).encode())
            return subprocess.CompletedProcess(args, 0)
        with patch.object(providers, "_external_python", return_value=sys.executable), \
                patch.object(runtime, "_execute_external_process", side_effect=process):
            result = VideodlAdapter(backend_id=backend, backend_category="dedicated").parse(SOURCE, 2)
        self.assertIsInstance(result, Work)
        self.assertEqual(result.field_sources["backend"], "videodl:dedicated:" + backend)
        self.assertEqual([request["operation"] for request in requests], ["inventory", "backend"])
        self.assertNotIn("_shadow_capture_fd", requests[0])
        self.assertEqual(requests[1]["backend_id"], backend)


class DirectGenericCaptureTests(OfflineCase):
    def test_gallery_adapter_uses_existing_concurrency_and_cli_boundary(self):
        raw = [[2, {"post_id": "123456789"}], [3, MEDIA, {"extension": "mp4"}]]
        with patch.object(gallerydl_generic, "get_cached_external_payload", return_value=None), \
                patch.object(gallerydl_generic, "get_cached_external_failure", return_value=None), \
                patch.object(gallerydl_generic, "cache_external_payload"), \
                patch.object(gallerydl_generic, "clear_cached_external_failure"), \
                patch.object(gallerydl_generic, "run_external_command", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps(raw), "")) as call:
            result = GalleryDlAdapter().parse(SOURCE, 2)
        self.assertIsInstance(result, Work)
        call.assert_called_once()

    def test_direct_cli_helpers_capture_native_json_without_changing_output(self):
        for module, fn, raw in ((ytdlp_generic, ytdlp_generic.run_ytdlp_json, info()),
                               (gallerydl_generic, gallerydl_generic.run_gallerydl_json,
                                [[2, {"post_id": "123456789"}], [3, MEDIA, {"extension": "mp4"}]])):
            with self.subTest(module=module.__name__), collect_raw() as session, \
                    patch.object(module, "run_external_command", return_value=
                        subprocess.CompletedProcess([], 0, json.dumps(raw), "")) as call:
                self.assertEqual(fn(SOURCE), raw)
            self.assertEqual(len(session.records), 1)
            self.assertEqual(session.records[0].raw, raw)
            call.assert_called_once()

    def test_gallery_json_lines_keep_original_native_messages(self):
        rows = [[2, {"post_id": "123456789"}], [3, MEDIA, {"extension": "mp4"}]]
        with collect_raw() as session, patch.object(gallerydl_generic, "run_external_command", return_value=
                subprocess.CompletedProcess([], 0, "\n".join(json.dumps(row) for row in rows), "")):
            self.assertEqual(gallerydl_generic.run_gallerydl_json(SOURCE), rows)
        self.assertEqual(session.records[0].raw, rows)

    def test_cached_native_json_is_observed_without_any_request(self):
        for module, cls, raw in ((ytdlp_generic, ytdlp_generic.YtDlpGenericProvider, info()),
                                (gallerydl_generic, gallerydl_generic.GalleryDlGenericProvider, [[2, {}]])):
            with self.subTest(provider=cls.__name__), collect_raw() as session, \
                    patch.object(module, "get_cached_external_payload", return_value=raw), \
                    patch.object(module, "run_external_command", side_effect=AssertionError("cache must not refetch")):
                self.assertIs(cls().parse(SOURCE), raw)
            self.assertEqual(session.records[0].raw, raw)

    def test_direct_capture_off_does_not_copy_or_observe_raw(self):
        for module, fn, raw in ((ytdlp_generic, ytdlp_generic.run_ytdlp_json, info()),
                               (gallerydl_generic, gallerydl_generic.run_gallerydl_json, [])):
            with patch.object(module, "capture_raw", side_effect=AssertionError("disabled")), \
                    patch.object(module, "run_external_command", return_value=
                        subprocess.CompletedProcess([], 0, json.dumps(raw), "")):
                self.assertEqual(fn(SOURCE), raw)


class WorkerObservationTests(OfflineCase):
    def test_gallery_observe_preserves_native_metadata_before_clipping(self):
        from gallery_dl.extractor.message import Message
        raw = [(Message.Directory, {"post_id": "123456789", "upstream_only": {"keep": True}}),
               (Message.Url, MEDIA, {"post_id": "123456789", "extension": "mp4", "upstream_only": [1]})]
        class SyntheticExtractor:
            pattern = r"https://example.org/1"
            def __init__(self, match):
                pass
            def __iter__(self):
                return iter(deepcopy(raw))
        observer = wire.Observer(100)
        with patch.object(match_worker, "extractor_classes", return_value=[SyntheticExtractor]):
            old = match_worker.parse_gallery(SyntheticExtractor, "https://example.org/1", 2)
            new = match_worker.parse_gallery(SyntheticExtractor, "https://example.org/1", 2, observe=observer)
        self.assertEqual(old, new)
        self.assertEqual(decode_raw({"worker_raw_v1": observer.records}, many=True), raw)
        self.assertNotIn("upstream_only", old[0][-1])

    def test_parsehub_observe_precedes_real_normalizer_with_no_extra_parse(self):
        from tools import upstream_parsehub_worker as worker
        modules = {name: ModuleType(name) for name in ("parsehub", "parsehub.types", "parsehub.types.media_ref",
                   "parsehub.types.result", "parsehub.utils", "parsehub.utils.helpers")}
        base = type("MediaRef", (), {"__module__": "parsehub.types.media_ref"})
        media_module = modules["parsehub.types.media_ref"]
        media_module.MediaRef = base
        for name in ("AniRef", "ImageRef", "LivePhotoRef", "VideoRef"):
            setattr(media_module, name, type(name, (base,), {"__module__": "parsehub.types.media_ref"}))
        result_base = type("ParseResult", (), {"__module__": "parsehub.types.result"})
        modules["parsehub.types.result"].ParseResult = result_base
        modules["parsehub.types.result"].RichTextParseResult = type("RichTextParseResult", (result_base,), {})
        modules["parsehub.utils.helpers"].SecretCookie = lambda value: value
        result = type("VideoParseResult", (result_base,), {"__module__": "parsehub.types.result"})()
        media = media_module.VideoRef()
        vars(media).update(url=MEDIA, ext="mp4")
        vars(result).update(raw_url=SOURCE, title="Synthetic", content="", media=[media], upstream_only=[1])
        calls = []
        class SyntheticParser:
            def __init__(self, **kwargs):
                pass
            async def parse(self, url):
                calls.append(url)
                return result
        observer = wire.Observer(100)
        with patch.dict(sys.modules, modules), patch.object(worker, "execution_boundary", return_value=nullcontext()):
            old = asyncio.run(worker.parse(SOURCE, 2, SyntheticParser))
            new = asyncio.run(worker.parse(SOURCE, 2, SyntheticParser, observe=observer))
        self.assertEqual(old, new)
        self.assertEqual(calls, [SOURCE, SOURCE])
        self.assertEqual(decode_raw({"worker_raw_v1": observer.records})["fields"]["upstream_only"], [1])

    def test_ytdlp_eager_collection_keeps_existing_execution_and_limits(self):
        from yt_dlp.extractor.common import InfoExtractor
        class SyntheticIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>1)"
            def _real_extract(self, url):
                return {"_type": "playlist", "id": "collection", "entries": [
                    {"id": "child", "title": "Synthetic", "url": MEDIA, "ext": "mp4"}]}
        observer = wire.Observer(100)
        old = match_worker.parse_ytdlp(SyntheticIE, "https://example.org/1", 2)
        new = match_worker.parse_ytdlp(SyntheticIE, "https://example.org/1", 2, observe=observer)
        self.assertEqual(new, old)
        self.assertFalse(observer.failed)
        root = decode_raw({"worker_raw_v1": observer.records})
        self.assertEqual(root["entries"][0]["url"], MEDIA)

    def test_xhs_observe_keeps_complete_page_before_projection(self):
        raw = page()
        class XHSAPI:
            def __init__(self, **kwargs):
                pass
            async def __fetch_html(self, url):
                return "synthetic html"
            async def __extract_data(self, html):
                return raw
            def get_trace_id(self, url):
                return "synthetic.jpg"
        modules = {name: ModuleType(name) for name in ("parsehub", "parsehub.provider_api", "parsehub.provider_api.xhs")}
        modules["parsehub.provider_api.xhs"].XHSAPI = XHSAPI
        request = {"source_url": XHS_SOURCE, "expected_note_id": NOTE, "max_items": 64}
        observer = wire.Observer(100)
        with patch.dict(sys.modules, modules):
            old = asyncio.run(xhs_worker._extract(request))
            new = asyncio.run(xhs_worker._extract(request, observe=observer))
        self.assertEqual(old, new)
        self.assertEqual(decode_raw({"worker_raw_v1": observer.records}), raw)
        self.assertNotIn("other_page_facts", old)

    def test_videodl_observation_precedes_validity_filter(self):
        good = native("videodl.modules.utils.data", "VideoInfo", source="native", download_url=MEDIA,
                      with_valid_download_url=True)
        invalid = native("videodl.modules.utils.data", "VideoInfo", source="native", err_msg="no resource",
                         with_valid_download_url=False, with_valid_audio_download_url=False)
        modules = {name: ModuleType(name) for name in ("videodl", "videodl.modules")}
        module = modules["videodl.modules"]
        module.VideoClientBuilder = SimpleNamespace(REGISTERED_MODULES={"SyntheticClient": SimpleNamespace(belongto=lambda url: True)})
        module.CommonVideoClientBuilder = SimpleNamespace(REGISTERED_MODULES={})
        module.BuildVideoClient = Mock(return_value=SimpleNamespace(parsefromurl=Mock(return_value=[good, invalid])))
        module.BuildCommonVideoClient = Mock(side_effect=AssertionError("no fallback"))
        observer = wire.Observer(100)
        with patch.dict(sys.modules, modules):
            old = videodl_worker.run_exact_backend(SOURCE, "SyntheticClient", "dedicated", "/unused")
            new = videodl_worker.run_exact_backend(SOURCE, "SyntheticClient", "dedicated", "/unused", observe=observer)
        self.assertEqual(new, old)
        captured = decode_raw({"worker_raw_v1": observer.records}, objects=True)
        self.assertEqual(len(captured), 2)
        self.assertEqual(vars(captured[1]), vars(invalid))
