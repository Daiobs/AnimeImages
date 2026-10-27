"""Offline platform-matrix coverage at the file, CLI, and HTTP send boundaries."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit
import zipfile

from PIL import Image
import requests

from tools import production_smoke as smoke
from xhs_download.tests_production_smoke import (
    HOME, SECRET, Wire, card, image_item, image_route, item, media, parsed, picker, response,
)


SOURCE = "https://www.flickr.com/photos/private-photo?token=" + SECRET


def upstream_card(index, **extra):
    return card(index, **{"asset-id": f"upstream:image-{index}", "provider": "gallery_dl_generic", **extra})


def upstream_image_item(index, **extra):
    return image_item(index, **{"asset_id": f"upstream:image-{index}", "provider": "gallery_dl_generic",
                                "browser_upstream_bound": True, **extra})


class OfflineMatrixCase(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.round_number = 0
        image = io.BytesIO()
        Image.new("RGB", (3, 2), "red").save(image, format="PNG")
        self.png = image.getvalue()
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "requests.adapters.HTTPAdapter.send"):
            guard = mock.patch(target, side_effect=AssertionError("real network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def samples_file(self, samples=None, *, document=None, mode=0o600):
        path = self.root / "samples.json"
        if document is None:
            document = {"version": 1, "samples": samples or [self.sample()]}
        if path.exists():
            path.chmod(0o600)
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(mode)
        return path

    @staticmethod
    def sample(**changes):
        return {"id": "foo", "platform": "flickr", "kind": "image", "url": SOURCE, **changes}

    def new_round(self):
        self.round_number += 1
        directory = smoke.create_round(self.root / "rounds", "round-" + str(self.round_number))
        journal = smoke.Journal(directory)
        self.addCleanup(journal.file.close)
        return directory, journal

    def runner(self, steps, *, base="https://site.example"):
        directory, journal = self.new_round()
        wire = Wire([("GET", "/", response(HOME)), *steps], directory / "attempts.jsonl")
        http = smoke.PublicHTTP(base, journal, session_factory=wire.factory)
        self.addCleanup(http.close)
        runner = smoke.SmokeRunner(http, directory, ffprobe=sys.executable, sleep=lambda _: None)
        runner.bootstrap()
        return runner, wire

    def assert_private_report(self, directory, result):
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SOURCE, json.dumps(result))
        for path in directory.rglob("*.json*"):
            contents = path.read_text()
            self.assertNotIn(SECRET, contents, str(path))
            self.assertNotIn("?token=", contents, str(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @contextlib.contextmanager
    def adapter_runner(self, steps, *, base="https://site.example"):
        directory, journal = self.new_round()
        wire = Wire([("GET", "/", response(HOME)), *steps], directory / "attempts.jsonl")
        scripted = wire.factory()
        scripted.trust_env = False
        self.addCleanup(scripted.close)

        def send(adapter, request, **kwargs):
            self.assertEqual(adapter.max_retries.total, 0)
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["timeout"], (15, 120))
            self.assertFalse(kwargs.get("proxies"))
            return scripted.send(request, allow_redirects=False, **kwargs)

        # Session.request/send stay real; only the last adapter boundary is replaced.
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", new=send):
            http = smoke.PublicHTTP(base, journal)
            self.addCleanup(http.close)
            self.assertFalse(http.site.trust_env)
            self.assertFalse(http.media.trust_env)
            runner = smoke.SmokeRunner(http, directory, ffprobe=sys.executable, sleep=lambda _: None)
            runner.bootstrap()
            yield runner, wire


class ProductionSmokeSamplesFileTests(OfflineMatrixCase):
    def test_private_v1_file_preserves_sample_order_and_optional_limit(self):
        rows = [self.sample(), self.sample(id="album", kind="gallery", max_bytes=1),
                self.sample(id="clip", kind="video", max_bytes=smoke.VIDEO_LIMIT)]
        self.assertEqual(smoke.load_samples(self.samples_file(rows)), rows)

    def test_rejects_invalid_envelopes_and_unknown_root_keys(self):
        valid = {"version": 1, "samples": [self.sample()]}
        documents = [[], "not-an-object", {}, {"samples": valid["samples"]},
                     {"version": 1}, {**valid, SECRET: True},
                     *[{**valid, "version": value} for value in (True, 0, 2, "1", 1.0)],
                     *[{**valid, "samples": value} for value in ([], {}, "samples", None)]]
        for document in documents:
            with self.subTest(document=document):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file(document=document))

    def test_rejects_missing_fields_unknown_sample_keys_and_duplicate_ids(self):
        rows = [[{k: v for k, v in self.sample().items() if k != missing}]
                for missing in ("id", "platform", "kind", "url")]
        rows.extend([[self.sample(**{SECRET: "unexpected"})],
                     [self.sample(), self.sample(platform="douyin", kind="gallery")],
                     [None], ["not-an-object"], [[]]])
        for samples in rows:
            with self.subTest(samples=samples):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file(document={"version": 1, "samples": samples}))

    def test_rejects_invalid_kinds_and_non_scalar_kinds(self):
        for kind in ("photo", "IMAGE", "", None, True, 1, [], {}):
            with self.subTest(kind=kind):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file([self.sample(kind=kind)]))

    def test_limit_must_be_a_positive_integer_within_video_limit(self):
        for limit in (0, -1, True, False, "1024", 1.5, None, [], smoke.VIDEO_LIMIT + 1):
            with self.subTest(limit=limit):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file([self.sample(max_bytes=limit)]))

    def test_sample_urls_must_remain_public_https(self):
        urls = ["http://cdn.example/photo.png", "http://127.0.0.1:8000/photo.png",
                "https://127.0.0.1/photo.png", "https://localhost/photo.png",
                "https://192.168.1.2/photo.png", "https://[::1]/photo.png",
                "https://user:password@cdn.example/photo.png",
                "https://cdn.example:8000/photo.png", "https://cdn.example/photo.png#token",
                "https://cdn.example/has space", "", None, 7, {}]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file([self.sample(url=url)]))

    def test_ids_cannot_escape_the_round_directory(self):
        for sample_id in ("../outside", "/tmp/outside", "foo/bar", "", None, 4):
            with self.subTest(sample_id=sample_id):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file([self.sample(id=sample_id)]))

    def test_permissions_must_be_exactly_0600(self):
        for mode in (0o400, 0o604, 0o640, 0o644, 0o660, 0o700):
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(smoke.SmokeError):
                    smoke.load_samples(self.samples_file(mode=mode))

    def test_symlink_is_rejected_even_when_target_is_private(self):
        path = self.samples_file()
        link = self.root / "linked-samples.json"
        link.symlink_to(path)
        with self.assertRaises((smoke.SmokeError, OSError)):
            smoke.load_samples(link)

    def test_malformed_json_and_unknown_keys_have_secret_free_cli_errors(self):
        path = self.samples_file()
        for body in ('{"version": 1, "samples": [' + SECRET,
                     json.dumps({"version": 1, "samples": [self.sample(**{SECRET: SOURCE})]})):
            with self.subTest(body=body):
                path.write_text(body, encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(smoke.main(["--samples-file", str(path), "--dry-run"]), 2)
                self.assertNotIn(SECRET, output.getvalue())
                self.assertNotIn(SOURCE, output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())


class ProductionSmokeMatrixCLITests(OfflineMatrixCase):
    def test_dry_run_modes_emit_metadata_without_urls_or_network_or_output_files(self):
        rows = [self.sample(max_bytes=32), self.sample(id="album", kind="gallery", max_bytes=128)]
        path = self.samples_file(rows)
        destination = self.root / "must-not-exist"
        for mode in ([], ["--dry-run"], ["--dryrun"], ["--list"]):
            with self.subTest(mode=mode):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), mock.patch.object(requests.Session, "request") as request:
                    code = smoke.main([*mode, "--samples-file", str(path), "--max-file-bytes", "64",
                                       "--output", str(destination)])
                self.assertEqual(code, 0)
                request.assert_not_called()
                plan = json.loads(output.getvalue())
                self.assertEqual(plan["network_requests"], 0)
                self.assertEqual(plan["required_samples"], ["foo", "album"])
                self.assertEqual([{key: row[key] for key in ("id", "platform", "kind", "source_host")}
                                  for row in plan["samples"]], [
                    {"id": "foo", "platform": "flickr", "kind": "image", "source_host": "www.flickr.com"},
                    {"id": "album", "platform": "flickr", "kind": "gallery", "source_host": "www.flickr.com"},
                ])
                self.assertNotIn(SECRET, output.getvalue())
                self.assertNotIn("https://", output.getvalue())
                self.assertNotIn('"url"', output.getvalue())
                self.assertFalse(destination.exists())

    def test_invalid_cli_limit_fails_before_any_http(self):
        path = self.samples_file()
        for limit in (0, -1, smoke.VIDEO_LIMIT + 1):
            with self.subTest(limit=limit), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(smoke.main(["--samples-file", str(path), "--max-file-bytes", str(limit)]), 2)

    def test_cli_applies_the_smaller_file_or_global_budget_to_real_image_flow(self):
        for number, (file_limit, cli_limit, passed) in enumerate((
                (64, 1024, False), (1024, 64, False), (len(self.png), len(self.png), True))):
            with self.subTest(file_limit=file_limit, cli_limit=cli_limit):
                path = self.samples_file([self.sample(max_bytes=file_limit)])
                target = self.root / "cli-rounds" / str(number)
                body = media(self.png)
                if not passed:
                    body.iter_content = mock.Mock(side_effect=AssertionError("oversized body must not be read"))
                wire = Wire([("GET", "/", response(HOME)),
                             ("GET", "/results/", response(parsed(card(1)))),
                             ("GET", "/1.png", body)], target / "attempts.jsonl")
                http_class = smoke.PublicHTTP
                output = io.StringIO()
                with mock.patch.object(smoke, "PublicHTTP", side_effect=lambda base, journal:
                                       http_class(base, journal, session_factory=wire.factory)), contextlib.redirect_stdout(output):
                    code = smoke.main(["--run", "--samples-file", str(path),
                                       "--max-file-bytes", str(cli_limit), "--base-url", "http://127.0.0.1:8000",
                                       "--output", str(target.parent), "--round", target.name,
                                       "--ffprobe", sys.executable])
                report = json.loads((target / "report.json").read_text())
                self.assertEqual(code, 0 if passed else 1, report)
                self.assertIs(report["passed"], passed)
                self.assertEqual(report["required_samples"], ["foo"])
                self.assertFalse(wire.steps)
                self.assertEqual(len(wire.requests), 3)
                self.assertEqual(parse_qs(urlsplit(wire.requests[1].url).query)["q"], [SOURCE])
                result = report["samples"][0]
                self.assertEqual((result["platform"], result["kind"]), ("flickr", "image"))
                self.assertEqual(result["max_file_bytes"], min(file_limit, cli_limit))
                if not passed:
                    body.iter_content.assert_not_called()
                    self.assertEqual(result["images"][0]["error_code"], "MEDIA_SIZE_LIMIT")
                    self.assertFalse((target / "foo/images.zip").exists())
                self.assertNotIn(SECRET, output.getvalue())
                self.assert_private_report(target, report)

    def test_image_only_and_gallery_rounds_do_not_require_ffprobe(self):
        missing_probe = self.root / "missing-ffprobe"
        self.assertFalse(missing_probe.exists())
        for kind, indices in (("image", (1,)), ("gallery", (1, 2))):
            with self.subTest(kind=kind):
                path = self.samples_file([self.sample(kind=kind)])
                target = self.root / "without-probe" / kind
                wire = Wire([
                    ("GET", "/", response(HOME)),
                    ("GET", "/results/", response(parsed("".join(card(i) for i in indices)))),
                    *[("GET", f"/{i}.png", media(self.png)) for i in indices],
                ], target / "attempts.jsonl")
                http_class = smoke.PublicHTTP
                with mock.patch.object(smoke, "PublicHTTP", side_effect=lambda base, journal:
                                       http_class(base, journal, session_factory=wire.factory)), \
                        mock.patch.object(smoke.subprocess, "run", side_effect=AssertionError("no probe needed")) as probe, \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = smoke.main(["--run", "--samples-file", str(path),
                                       "--base-url", "http://127.0.0.1:8000", "--output", str(target.parent),
                                       "--round", kind, "--ffprobe", str(missing_probe)])
                self.assertEqual(code, 0)
                probe.assert_not_called()
                report = json.loads((target / "report.json").read_text())
                self.assertTrue(report["passed"], report)
                self.assertEqual(report["samples"][0]["saved_count"], len(indices))
                self.assertEqual(len(wire.requests), 2 + len(indices))
                self.assertFalse(wire.steps)
                self.assert_private_report(target, report)

    def test_round_containing_video_still_requires_ffprobe_before_http(self):
        for kinds in (("video",), ("image", "video")):
            with self.subTest(kinds=kinds):
                path = self.samples_file([self.sample(id=kind, kind=kind) for kind in kinds])
                destination = self.root / "must-not-be-created"
                output = io.StringIO()
                with mock.patch.object(requests.Session, "request") as request, contextlib.redirect_stdout(output):
                    code = smoke.main(["--run", "--samples-file", str(path),
                                       "--base-url", "https://site.example", "--output", str(destination),
                                       "--ffprobe", str(self.root / "missing-ffprobe")])
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue().strip(), "FFPROBE_UNAVAILABLE")
                request.assert_not_called()
                self.assertFalse(destination.exists())


class ProductionSmokeMatrixBehaviorTests(OfflineMatrixCase):
    def test_single_image_uses_image_flow_and_records_public_metadata(self):
        runner, wire = self.runner([("GET", "/results/", response(parsed(card(1, **{"source-platform": "flickr"})))),
                                    ("GET", "/1.png", media(self.png))])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual((result["expected_count"], result["saved_count"], result["failed_count"]), (1, 1, 0))
        self.assertEqual((result["provider"], result["media_hosts"], result["download_route"]),
                         ("not_exposed", ["cdn.example"], "browser_direct"))
        self.assertEqual(result["failure_stage"], "")
        self.assertEqual((result["platform"], result["kind"], result["source_host"]),
                         ("flickr", "image", "www.flickr.com"))
        self.assertEqual(result["images"][0]["format"], "PNG")
        self.assertEqual(result["transport"], "http_direct_image")
        self.assertEqual((runner.directory / "foo/image_01.media").read_bytes(), self.png)
        self.assertFalse((runner.directory / "foo/images.zip").exists())
        self.assertNotIn("archive", result)
        self.assertEqual([urlsplit(request.url).path for request in wire.requests],
                         ["/", "/results/", "/1.png"])
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_explicit_douyin_gallery_ignores_name_and_video_picker(self):
        html = (card(1, **{"source-platform": "douyin"}) +
                card(2, **{"source-platform": "douyin"}) + card(3, "video") + picker())
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(html, selected_provider="tiantianjiexi"))),
            ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(self.png)),
        ])
        result = runner.run_sample("douyin", "https://www.douyin.com/note/123?token=" + SECRET,
                                   kind="gallery", platform="douyin")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual((result["saved_count"], result["expected_count"]), (2, 2))
        self.assertEqual(result["provider"], "tiantianjiexi")
        self.assertEqual(result["media_hosts"], ["cdn.example"])
        self.assertEqual([urlsplit(request.url).path for request in wire.requests],
                         ["/", "/results/", "/1.png", "/2.png"])
        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 2))
        self.assertEqual(result["transport"], "http_images_local_store_zip")
        with zipfile.ZipFile(runner.directory / "douyin/images.zip") as archive:
            self.assertEqual(archive.namelist(), ["image_01.png", "image_02.png"])
            self.assertTrue(all(archive.read(name) == self.png for name in archive.namelist()))
            self.assertIsNone(archive.testzip())
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_single_image_kind_does_not_silently_accept_a_gallery(self):
        runner, wire = self.runner([("GET", "/results/", response(parsed(card(1) + card(2))))])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "EXPECTED_SINGLE_IMAGE")
        self.assertEqual(result["failure_stage"], "selection")
        self.assertEqual(result["media_http_attempts"], 0)
        self.assertEqual(len(wire.requests), 2)
        self.assertFalse(wire.steps)

    def test_gallery_rejects_one_image_before_authorization_or_media(self):
        for kind in ("gallery", None):
            with self.subTest(kind=kind):
                runner, wire = self.runner([("GET", "/results/", response(parsed(card(1))))])
                result = runner.run_sample("xhs_images", SOURCE, kind=kind)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], "EXPECTED_IMAGE_GALLERY")
                self.assertEqual(result["failure_stage"], "selection")
                self.assertEqual(result["media_http_attempts"], 0)
                self.assertEqual(len(wire.requests), 2)
                self.assertFalse(wire.steps)

    def test_ordinary_images_do_not_require_browser_fetch_safety_or_route(self):
        for kind, indices in (("image", (1,)), ("gallery", (1, 2))):
            for safe in ("false", None):
                with self.subTest(kind=kind, safe=safe):
                    html = "".join(card(i, **{"source-platform": "flickr", "browser-direct-fetch-safe": "false"})
                                   for i in indices)
                    if safe is None:
                        html = html.replace(' data-browser-direct-fetch-safe="false"', "")
                    runner, wire = self.runner([
                        ("GET", "/results/", response(parsed(html))),
                        *[("GET", f"/{i}.png", media(self.png)) for i in indices],
                    ])
                    result = runner.run_sample("foo", SOURCE, kind=kind)
                    self.assertEqual(result["status"], "passed", result)
                    self.assertEqual((result["saved_count"], result["media_http_attempts"]), (len(indices), len(indices)))
                    self.assertEqual([urlsplit(request.url).path for request in wire.requests],
                                     ["/", "/results/", *[f"/{i}.png" for i in indices]])
                    self.assertFalse(wire.steps)

    def test_ordinary_image_blocked_cookie_or_merge_flags_reject_before_any_media(self):
        cases = [({"download-strategy": "blocked_by_download_cost_guard"}, "DOWNLOAD_POLICY_BLOCKED"),
                 ({"requires-cookie": "true"}, "IMAGE_CONTEXT_REQUIRED"),
                 ({"requires-merge": "true"}, "IMAGE_CONTEXT_REQUIRED")]
        for kind in ("image", "gallery"):
            for attributes, expected in cases:
                with self.subTest(kind=kind, attributes=attributes):
                    html = (card(1) if kind == "gallery" else "") + card(2, **attributes)
                    runner, wire = self.runner([("GET", "/results/", response(parsed(html)))])
                    result = runner.run_sample("foo", SOURCE, kind=kind)
                    self.assertEqual(result["error_code"], expected, result)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["media_http_attempts"], 0)
                    self.assertEqual(len(wire.requests), 2)
                    self.assertFalse(wire.steps)

    def test_mixed_upstream_gallery_route_accepts_empty_optional_identity(self):
        for identity in ({}, {"asset_id": None, "provider": None}, {"asset_id": "", "provider": ""}):
            with self.subTest(identity=identity):
                runner, wire = self.runner([
                    ("GET", "/results/", response(parsed(card(1) + upstream_card(2)))),
                    image_route(image_item(1, browser_upstream_bound=True, **identity), upstream_image_item(2)),
                    ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(self.png)),
                ])
                result = runner.run_sample("foo", SOURCE, kind="gallery")
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual((result["saved_count"], result["media_http_attempts"]), (2, 2))
                request = json.loads(wire.requests[2].body)
                self.assertEqual(request["intent"], "download")
                self.assertIs(request["route_only"], True)
                self.assertEqual(request["download_mode"], "browser")
                original = request["media_items"][0]
                self.assertEqual((original["asset_id"], original["provider"]), ("", ""))
                self.assertEqual(request["media_items"][1]["asset_id"], "upstream:image-2")
                self.assertEqual([urlsplit(request.url).path for request in wire.requests],
                                 ["/", "/results/", "/media/download-images/", "/1.png", "/2.png"])
                self.assertFalse(wire.steps)

    def test_image_route_binds_resource_and_nonempty_identity(self):
        cases = [({"url": "https://cdn.example/other.png"}, None),
                 ({"type": "video"}, None), ({}, "url"), ({}, "type")]
        for key in ("asset_id", "provider"):
            cases.extend(({key: value}, None) for value in (None, "", "changed", 0, False))
            cases.append(({}, key))
        for changes, missing in cases:
            with self.subTest(changes=changes, missing=missing):
                projected = upstream_image_item(1, **changes)
                if missing:
                    projected.pop(missing, None)
                runner, wire = self.runner([
                    ("GET", "/results/", response(parsed(upstream_card(1)))),
                    image_route(projected),
                ])
                result = runner.run_sample("foo", SOURCE, kind="image")
                self.assertEqual(result["error_code"], "IMAGE_BINDING_MISMATCH", result)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["media_http_attempts"], 0)
                self.assertEqual(len(wire.requests), 3)
                self.assertFalse(wire.steps)

    def test_upstream_images_require_authoritative_bound_flag(self):
        for binding in ({}, {"browser_upstream_bound": False}, {"browser_upstream_bound": None},
                        {"browser_upstream_bound": "true"}, {"browser_upstream_bound": True}):
            with self.subTest(binding=binding):
                projected = upstream_image_item(1)
                projected.pop("browser_upstream_bound")
                projected.update(binding)
                steps = [
                    ("GET", "/results/", response(parsed(upstream_card(1)))),
                    image_route(projected),
                ]
                allowed = binding.get("browser_upstream_bound") is True
                if allowed:
                    steps.append(("GET", "/1.png", media(self.png)))
                runner, wire = self.runner(steps)
                result = runner.run_sample("foo", SOURCE, kind="image")
                self.assertEqual(result["status"], "passed" if allowed else "failed", result)
                self.assertEqual(result["media_http_attempts"], int(allowed))
                if not allowed:
                    self.assertEqual(result["error_code"], "IMAGE_BINDING_MISMATCH")
                self.assertFalse(wire.steps)

    def test_image_authorization_uses_bound_context_and_validates_the_whole_group(self):
        contexts = [{"browser_direct_fetch_safe": value} for value in (False, None, "true", 1)]
        contexts.extend([{"requires_cookie": True}, {"requires_merge": True}])
        for context in contexts:
            with self.subTest(context=context):
                runner, wire = self.runner([
                    ("GET", "/results/", response(parsed(upstream_card(1) + upstream_card(2)))),
                    image_route(upstream_image_item(1), upstream_image_item(2, **context)),
                ])
                result = runner.run_sample("album", SOURCE, kind="gallery")
                self.assertEqual(result["error_code"], "IMAGE_CONTEXT_REQUIRED", result)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_stage"], "route")
                self.assertEqual(result["media_http_attempts"], 0)
                self.assertEqual(len(wire.requests), 3)
                self.assertFalse(wire.steps)

    def test_image_missing_bound_safety_is_not_inferred_from_original_card(self):
        projected = upstream_image_item(1)
        projected.pop("browser_direct_fetch_safe")
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(upstream_card(1)))), image_route(projected),
        ])
        result = runner.run_sample("foo", SOURCE, kind="image")
        self.assertEqual(result["error_code"], "IMAGE_CONTEXT_REQUIRED", result)
        self.assertEqual(result["media_http_attempts"], 0)
        self.assertFalse(wire.steps)

    def test_image_download_consumes_authorized_context_not_stale_card_flags(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(upstream_card(1, **{
                "requires-cookie": "true", "requires-merge": "true", "browser-direct-fetch-safe": "false"})))),
            image_route(upstream_image_item(1)),
            ("GET", "/1.png", media(self.png)),
        ])
        result = runner.run_sample("foo", SOURCE, kind="image")
        self.assertEqual(result["status"], "passed", result)
        original = json.loads(wire.requests[2].body)["media_items"][0]
        self.assertIs(original["requires_cookie"], True)
        self.assertIs(original["requires_merge"], True)
        self.assertIs(original["browser_direct_fetch_safe"], False)
        self.assertEqual((runner.directory / "foo/image_01.media").read_bytes(), self.png)
        self.assertEqual(result["media_http_attempts"], 1)
        self.assertFalse(wire.steps)

    def test_blocked_image_route_or_bound_policy_never_fetches_media(self):
        for location in ("route", "item"):
            for policy in ({"route_type": "blocked"}, {"download_strategy": "blocked_by_download_cost_guard"}):
                with self.subTest(location=location, policy=policy):
                    routed = (image_route(upstream_image_item(1), **policy) if location == "route"
                              else image_route(upstream_image_item(1, **policy)))
                    runner, wire = self.runner([("GET", "/results/", response(parsed(upstream_card(1)))), routed])
                    result = runner.run_sample("foo", SOURCE, kind="image")
                    self.assertEqual(result["error_code"], "DOWNLOAD_POLICY_BLOCKED", result)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["media_http_attempts"], 0)
                    if location == "route":
                        for key, value in policy.items():
                            self.assertEqual(result["download_route" if key == "download_strategy" else key], value)
                    self.assertEqual(len(wire.requests), 3)
                    self.assertFalse(wire.steps)

    def test_gallery_route_cannot_omit_duplicate_or_reorder_images(self):
        for projected in ([], [upstream_image_item(1)], [upstream_image_item(1), upstream_image_item(1)],
                          [upstream_image_item(2), upstream_image_item(1)]):
            with self.subTest(projected=projected):
                runner, wire = self.runner([
                    ("GET", "/results/", response(parsed(upstream_card(1) + upstream_card(2)))),
                    image_route(*projected),
                ])
                result = runner.run_sample("album", SOURCE, kind="gallery")
                self.assertEqual(result["error_code"], "IMAGE_ROUTE_REJECTED" if len(projected) != 2 else "IMAGE_BINDING_MISMATCH")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["media_http_attempts"], 0)
                self.assertEqual(len(wire.requests), 3)
                self.assertFalse(wire.steps)

    def test_unknown_provider_value_is_not_copied_to_reports(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(card(1, provider=SECRET), selected_provider=SECRET))),
            ("GET", "/1.png", media(self.png)),
        ])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["provider"], "not_exposed")
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_prepare_400_preserves_descriptor_host_route_and_separate_remote_code(self):
        original = item(legacy=True)
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker(legacy=True)))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": original})),
            ("POST", "/media/download-images/", response({"status": "routed", "route_type": "proxy",
                "download_strategy": "server_single_attachment", "items": [{"url": original["url"]}]})),
            ("POST", "/media/download-prepare/", response({"status": "error",
                "error_code": "SERVICE_BUSINESS_CODE_400", "message": SOURCE}, status=400)),
        ])
        result = runner.run_sample("clip", SOURCE, kind="video", platform="flickr")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "CONTROL_HTTP_FAILED")
        self.assertEqual(result["remote_error_code"], "SERVICE_BUSINESS_CODE_400")
        self.assertEqual(result["failure_stage"], "prepare")
        self.assertEqual(result["media_hosts"], ["cdn.example"])
        self.assertEqual(result["download_route"], "server_single_attachment")
        self.assertEqual(result["route_type"], "proxy")
        lengths = result["handoff_item_string_lengths"]
        self.assertEqual(lengths["url"], len(original["url"]))
        self.assertEqual(lengths["provider_manifest_token"], len(SECRET))
        self.assertTrue(all(type(length) is int for length in lengths.values()))
        self.assertEqual(result["provider"], "tiantianjiexi")
        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))
        self.assertEqual(json.loads(wire.requests[-1].body)["download_mode"], "server_single")
        self.assertEqual(len(wire.requests), 5)
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_remote_parse_code_and_message_are_redacted_without_losing_stage(self):
        runner, wire = self.runner([("GET", "/results/", response({"status": "error",
                                    "error_code": SECRET, "message": SOURCE}, status=403))])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["error_code"], "CONTROL_HTTP_FAILED")
        self.assertEqual(result["remote_error_code"], "REMOTE_ERROR_REDACTED")
        self.assertEqual(result["failure_stage"], "parse")
        self.assertEqual(result["media_hosts"], [])
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_header_only_admission_failure_is_preserved_without_media_request(self):
        code = "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED"
        runner, wire = self.runner([("GET", "/results/", response({"status": "error"},
            status=403, headers={"X-Error-Code": code}))])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["remote_error_code"], code)
        self.assertEqual(result["failure_stage"], "parse")
        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_unknown_header_error_is_redacted_and_body_error_has_precedence(self):
        for body, expected in (({}, "REMOTE_ERROR_REDACTED"),
                               ({"error_code": "PROVIDER_UNAVAILABLE"}, "PROVIDER_UNAVAILABLE")):
            with self.subTest(expected=expected):
                runner, wire = self.runner([("GET", "/results/", response(body,
                    status=403, headers={"X-Error-Code": SECRET}))])
                result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
                self.assertEqual(result["remote_error_code"], expected)
                self.assertEqual(result["media_http_attempts"], 0)
                self.assertFalse(wire.steps)
                self.assert_private_report(runner.directory, result)

    def test_known_continuation_failure_codes_remain_safe_diagnostics(self):
        for code in ("DOUYIN_CONTINUATION_FAILED", "DOUYIN_CONTINUATION_RESULT_REJECTED"):
            self.assertEqual(smoke.remote_summary({"error_code": code})["error_code"], code)

    def test_corrupt_image_reports_container_stage_not_parse_or_prepare(self):
        corrupt = b"\x89PNG\r\n\x1a\n" + SECRET.encode()
        runner, wire = self.runner([("GET", "/results/", response(parsed(card(1)))),
                                    ("GET", "/1.png", media(corrupt))])
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_stage"], "container")
        self.assertEqual(result["images"][0]["error_code"], "INVALID_IMAGE")
        self.assertFalse((runner.directory / "foo/images.zip").exists())
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_video_size_header_rejects_before_reading_any_body(self):
        body = response(b"", headers={"Content-Length": "65"})
        body.iter_content = mock.Mock(side_effect=AssertionError("oversized body must not be read"))
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", body),
        ])
        result = runner.run_sample("clip", SOURCE, kind="video", platform="flickr", max_bytes=64)
        self.assertEqual(result["error_code"], "MEDIA_SIZE_LIMIT")
        self.assertEqual(result["failure_stage"], "media")
        self.assertEqual(result["download_route"], "browser_streaming")
        self.assertEqual(result["media_hosts"], ["cdn.example"])
        body.iter_content.assert_not_called()
        self.assertFalse((runner.directory / "clip/video.media").exists())
        self.assertEqual(result["media_http_attempts"], 1)
        self.assertFalse(wire.steps)

    def test_unknown_length_stream_stops_at_limit_without_draining_or_retrying(self):
        consumed = []

        def chunks():
            consumed.append("first")
            yield self.png
            consumed.append("overflow")
            yield b"x" * 16
            raise AssertionError("remaining file must not be consumed")

        body = response(b"")
        body.iter_content = mock.Mock(return_value=chunks())
        runner, wire = self.runner([("GET", "/results/", response(parsed(card(1)))),
                                    ("GET", "/1.png", body)])
        maximum = len(self.png) + 8
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr", max_bytes=maximum)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_stage"], "media")
        self.assertEqual(result["images"][0]["error_code"], "MEDIA_SIZE_LIMIT")
        self.assertEqual(consumed, ["first", "overflow"])
        body.iter_content.assert_called_once()
        self.assertLessEqual((runner.directory / "foo/image_01.media").stat().st_size, maximum)
        self.assertFalse((runner.directory / "foo/images.zip").exists())
        self.assertEqual(result["media_http_attempts"], 1)
        self.assertFalse(wire.steps)


class ProductionSmokeMatrixHTTPTests(OfflineMatrixCase):
    def test_image_authorization_denial_or_unknown_outcome_never_retries_or_fetches_media(self):
        for base in ("http://127.0.0.1:8000", "https://site.example"):
            for outcome, expected in (
                    (response({"status": "error", "error_code": "PROVIDER_ACCESS_DENIED"}, status=403), "CONTROL_HTTP_FAILED"),
                    (response({"status": "error", "error_code": "PROVIDER_UNAVAILABLE"}, status=503), "CONTROL_HTTP_FAILED"),
                    (requests.ReadTimeout(SECRET), "HTTP_OUTCOME_UNKNOWN"),
                    (requests.ConnectionError(SECRET), "HTTP_OUTCOME_UNKNOWN")):
                with self.subTest(base=base, expected=expected, outcome=type(outcome).__name__):
                    with self.adapter_runner([
                        ("GET", "/results/", response(parsed(upstream_card(1)))),
                        ("POST", "/media/download-images/", outcome),
                    ], base=base) as (runner, wire):
                        result = runner.run_sample("foo", SOURCE, kind="image")
                        self.assertEqual(result["status"], "failed", result)
                        self.assertEqual(result["error_code"], expected)
                        self.assertEqual(result["failure_stage"], "route")
                        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))
                        self.assertEqual(len(wire.requests), 3)
                        self.assertEqual([request.url.split("?")[0] for request in wire.requests],
                                         [base + "/", base + "/results/", base + "/media/download-images/"])
                        request = json.loads(wire.requests[2].body)
                        self.assertEqual(request["intent"], "download")
                        self.assertIs(request["route_only"], True)
                        self.assertEqual(request["download_mode"], "browser")
                        self.assertFalse(wire.steps)
                        self.assert_private_report(runner.directory, result)

    def test_local_base_can_send_controls_but_not_local_or_http_media(self):
        runner, wire = self.runner([], base="http://127.0.0.1:8000")
        self.assertEqual(wire.requests[0].url, "http://127.0.0.1:8000/")
        for url in ("http://127.0.0.1:8000/file.png", "https://127.0.0.1/file.png",
                    "http://localhost:8000/file.png", "https://localhost/file.png",
                    "https://[::1]/file.png", "http://cdn.example/file.png"):
            with self.subTest(url=url), self.assertRaises(smoke.SmokeError):
                runner.http.send("GET", url, "media", media=True)
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(sum(runner.http.journal.counts.values()), 1)

    def test_local_base_does_not_authorize_cross_origin_controls(self):
        runner, wire = self.runner([], base="http://127.0.0.1:8000")
        for url in ("https://other.example/media/download-prepare/",
                    "http://127.0.0.1:8001/media/download-prepare/",
                    "http://localhost:8000/media/download-prepare/"):
            with self.subTest(url=url), self.assertRaises(smoke.SmokeError):
                runner.http.control("POST", url, "prepare", data={"token": SECRET})
        self.assertEqual(len(wire.requests), 1)

    def test_media_redirect_cannot_use_the_local_base_exception(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(card(1)))),
            ("GET", "/1.png", response(b"", status=302,
                                       headers={"Location": "http://127.0.0.1:8000/private?token=" + SECRET})),
        ], base="http://127.0.0.1:8000")
        result = runner.run_sample("foo", SOURCE, kind="image", platform="flickr")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_stage"], "media")
        self.assertEqual(result["media_http_attempts"], 1)
        self.assertEqual(len(wire.requests), 3)
        self.assertFalse(wire.steps)
        self.assert_private_report(runner.directory, result)

    def test_real_requests_adapter_does_not_retry_parse_errors_and_next_sample_runs(self):
        steps = [
            ("GET", "/results/", requests.ReadTimeout(SOURCE)),
            ("GET", "/results/", requests.ConnectionError(SECRET)),
            ("GET", "/results/", response({"status": "error", "error_code": "PROVIDER_UNAVAILABLE"}, status=503)),
            ("GET", "/results/", response(parsed(card(1)))),
            ("GET", "/1.png", media(self.png)),
        ]
        with self.adapter_runner(steps, base="http://127.0.0.1:8000") as (runner, wire):
            for name, expected in (("timeout", "HTTP_OUTCOME_UNKNOWN"),
                                   ("connection", "HTTP_OUTCOME_UNKNOWN"), ("unavailable", "CONTROL_HTTP_FAILED")):
                result = runner.run_sample(name, SOURCE, kind="image", platform="flickr")
                self.assertEqual(result["error_code"], expected, result)
                self.assertEqual(result["failure_stage"], "parse")
                self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))
            later = runner.run_sample("later", SOURCE, kind="image", platform="flickr")
            self.assertEqual(later["status"], "passed", later)
            self.assertEqual(len(wire.requests), 6)
            self.assertTrue(all(isinstance(request, requests.PreparedRequest) for request in wire.requests))
            self.assertFalse(wire.steps)
            self.assert_private_report(runner.directory, later)

    def test_real_requests_adapter_does_not_retry_media_or_switch_transport(self):
        steps = [
            ("GET", "/results/", response(parsed(card(1)))),
            ("GET", "/1.png", requests.ReadTimeout(SECRET)),
            ("GET", "/results/", response(parsed(card(1)))),
            ("GET", "/1.png", response(b"", status=503)),
            ("GET", "/results/", response(parsed(card(1)))),
            ("GET", "/1.png", media(self.png)),
        ]
        with self.adapter_runner(steps) as (runner, wire):
            for name, expected in (("timeout", "HTTP_OUTCOME_UNKNOWN"), ("unavailable", "MEDIA_HTTP_FAILED")):
                result = runner.run_sample(name, SOURCE, kind="image", platform="flickr")
                self.assertEqual(result["images"][0]["error_code"], expected, result)
                self.assertEqual(result["failure_stage"], "media")
                self.assertEqual(result["transport"], "http_direct_image")
                self.assertNotIn("archive", result)
                self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 1))
            later = runner.run_sample("later", SOURCE, kind="image", platform="flickr")
            self.assertEqual(later["status"], "passed", later)
            self.assertEqual(len(wire.requests), 7)
            self.assertFalse(wire.steps)
            for request in wire.requests:
                if urlsplit(request.url).hostname == "cdn.example":
                    for header in ("Authorization", "Cookie", "X-CSRFToken", "Origin", "Referer"):
                        self.assertNotIn(header, request.headers)
            self.assert_private_report(runner.directory, later)
