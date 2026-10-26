"""Offline smoke tests: replace only HTTP sends; parse and save real fixtures."""
from collections import deque
import contextlib
from html import escape
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import zipfile

from django.test import SimpleTestCase
from PIL import Image
import requests

from tools import production_smoke as smoke


SECRET = "private-candidate-manifest-continuation-sentinel"
HOME = '<section class="xhs-download-page" data-ultra-hd-available="true" data-youtube-merge-available="true" data-download-async-enabled="true"></section><input name="csrfmiddlewaretoken" value="masked-csrf"><input name="ultra_hd" value="1"><input name="youtube_merge" value="0">'


def response(body, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = body if isinstance(body, bytes) else (json.dumps(body).encode() if isinstance(body, dict) else body.encode())
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


def media(body, length=True):
    return response(body, headers={"Content-Length": str(len(body))} if length else {})


def picker(legacy=False, provider="tiantianjiexi", schema="service_selected_format_v1"):
    endpoint = "ytdlp" if schema == "ytdlp_selected_format_v1" else "service"
    def radio(value, checked):
        return (f'<input data-picker-radio data-kind="video" value="{value}" data-token="{SECRET}" '
                f'data-format-id="{value}" data-label="1080p" data-legacy-asset-id="asset-1" '
                f'data-can-download="{str(not legacy).lower()}" data-can-legacy="true" {"checked" if checked else ""}>')
    return (f'<section data-ytdlp-picker data-provider="{provider}" data-selection-schema="{schema}" '
            f'data-selection-url="/media/{endpoint}-selected-format/">{radio("low", False)}{radio("highest", True)}</section>')


def item(legacy=False, provider="tiantianjiexi", schema="service_selected_format_v1"):
    return {"type": "video", "url": "https://cdn.example/video.mp4?token=" + SECRET,
            "filename": "title.mp4", "candidate_id": "highest", "selected_format_id": "highest",
            "selection_schema": schema, "provider": provider, "asset_id": "asset-1",
            "provider_resource_id": "resource-1", "provider_cache_key": "cache-1", "provider_manifest_token": SECRET,
            "source_platform": "bilibili" if legacy else "douyin", "size_bytes": 10,
            "selection_delivery": "legacy_manifest" if legacy else "direct",
            "browser_upstream_bound": not legacy, "browser_direct_fetch_safe": not legacy,
            "requires_merge": False, "requires_cookie": False, "download_strategy": "server_attachment" if legacy else "browser_streaming",
            "selection_expires_at": int((time.time() + 300) * 1000)}


def card(index, kind="image", **extra):
    values = {"type": kind, "url": f"https://cdn.example/{index}.png?token={SECRET}", "source-platform": "xiaohongshu",
              "download-strategy": "browser_direct", "fallback-strategy": "none", "browser-direct-fetch-safe": "true",
              "display-ordinal": str(index), **extra}
    return '<div class="asset-card" ' + ' '.join(f'data-{k}="{escape(str(v))}"' for k, v in values.items()) + '></div>'


def parsed(html, **extra):
    return {"status": "success", "html": html, "has_results": True, "base_filename": "example", **extra}


class Wire:
    """Script requests.Session.send, leaving request preparation and cookies real."""
    def __init__(self, steps, journal_path):
        self.steps, self.requests = deque(steps), []
        self.journal_path = journal_path

    def factory(self):
        wire = self

        class Session(requests.Session):
            def send(self, request, **kwargs):
                # Every wire attempt must already have a durable, secret-free record.
                entries = wire.journal_path.read_text().splitlines()
                assert len(entries) == len(wire.requests) + 1
                assert SECRET not in "\n".join(entries)
                assert kwargs.get("allow_redirects") is False
                assert self.trust_env is False
                wire.requests.append(request)
                method, path, outcome = wire.steps.popleft()
                assert (request.method, urlsplit(request.url).path) == (method, path)
                if isinstance(outcome, Exception):
                    raise outcome
                if path == "/":
                    self.cookies.set("csrftoken", "cookie-csrf", domain="site.example", path="/")
                if outcome.headers.get("Set-Cookie"):
                    self.cookies.set("cdn-secret", SECRET, domain="cdn.example", path="/")
                outcome.request, outcome.url = request, request.url
                return outcome

        return Session()


class ProductionSmokeTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.assets = tempfile.TemporaryDirectory()
        cls.ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        cls.video = None
        if Path(ffmpeg).is_file() and Path(cls.ffprobe).is_file():
            path = Path(cls.assets.name) / "fixture.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=32x24:rate=5",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000", "-t", "0.4",
                            "-c:v", "mpeg4", "-c:a", "aac", str(path)], check=True, capture_output=True, timeout=30)
            cls.video = path.read_bytes()
        image = io.BytesIO()
        Image.new("RGB", (3, 2), "red").save(image, format="PNG")
        cls.png = image.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = smoke.create_round(Path(self.temp.name) / "private", "test-round")
        self.journal = smoke.Journal(self.directory)
        self.addCleanup(self.journal.file.close)

    def runner(self, steps):
        wire = Wire([("GET", "/", response(HOME)), *steps], self.directory / "attempts.jsonl")
        http = smoke.PublicHTTP("https://site.example", self.journal, session_factory=wire.factory)
        self.addCleanup(http.close)
        runner = smoke.SmokeRunner(http, self.directory, ffprobe=self.ffprobe, sleep=lambda _: None)
        runner.bootstrap()
        return runner, wire

    def need_video(self):
        if self.video is None:
            self.skipTest("local ffmpeg/ffprobe fixture tools unavailable")

    def test_checked_default_not_first_and_csrf_no_cookie_to_media(self):
        self.need_video()
        redirect = response(b"", 302, {"Location": "https://other.example/final.mp4", "Set-Cookie": "cdn-secret=" + SECRET})
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", redirect), ("GET", "/final.mp4", media(self.video)),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["status"], "passed", result)
        request = wire.requests[2]
        self.assertEqual(json.loads(request.body), {"candidate_id": "highest", "resource_token": SECRET, "action": "download"})
        self.assertEqual(request.headers["X-CSRFToken"], "masked-csrf")
        self.assertIn("csrftoken=cookie-csrf", request.headers["Cookie"])
        self.assertEqual(request.headers["Origin"], "https://site.example")
        for request in wire.requests[3:]:
            for header in ("Cookie", "Authorization", "X-CSRFToken", "Referer", "Origin"):
                self.assertNotIn(header, request.headers)
        self.assertEqual(result["download"]["video_tracks"], 1)
        self.assertEqual(result["download"]["audio_tracks"], 1)
        self.assertEqual(result["parse_attempts"], 1)
        self.assertFalse(wire.steps)

    def test_bilibili_legacy_keeps_original_manifest_for_prepare(self):
        self.need_video()
        original = item(legacy=True, provider="tiantianjiexi_generic")
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker(True, "tiantianjiexi_generic")))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": original})),
            ("POST", "/media/download-images/", response({"status": "routed", "route_type": "proxy",
                "download_strategy": "server_single_attachment", "items": [{"url": original["url"]}]})),
            ("POST", "/media/download-prepare/", response({"ok": True, "status": "issued", "expires_in_seconds": 60,
                "download_url": "/media/download-stream/" + SECRET + "/", "status_url": "/media/download-status/private-status/"})),
            ("GET", "/media/download-stream/" + SECRET + "/", media(self.video)),
            ("GET", "/media/download-status/private-status/", response({"status": "completed"})),
        ])
        result = runner.run_sample("bilibili", dict(smoke.SAMPLES)["bilibili"])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(json.loads(wire.requests[2].body)["action"], "legacy")
        self.assertEqual(json.loads(wire.requests[4].body)["media_items"], [original])
        self.assertEqual(result["transport"], "http_server_attachment")
        self.assertTrue(all(urlsplit(r.url).hostname == "site.example" for r in wire.requests))
        self.assertNotIn(SECRET, (self.directory / "bilibili/summary.json").read_text())

    def test_youtube_reads_home_hidden_default_not_availability(self):
        runner, wire = self.runner([("GET", "/results/", response({"status": "success", "has_results": False}))])
        result = runner.run_sample("youtube", dict(smoke.SAMPLES)["youtube"])
        self.assertEqual(parse_qs(urlsplit(wire.requests[1].url).query)["youtube_merge"], ["0"])
        self.assertEqual(result["status"], "failed")

    def test_whole_image_group_crc_formats_and_no_server_zip(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(card(1) + card(2)))),
            ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(self.png)),
        ])
        result = runner.run_sample("xhs_images", dict(smoke.SAMPLES)["xhs_images"])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual((result["saved_count"], result["failed_count"]), (2, 0))
        self.assertEqual([r["format"] for r in result["images"]], ["PNG", "PNG"])
        with zipfile.ZipFile(self.directory / "xhs_images/images.zip") as zipped:
            self.assertEqual(zipped.namelist(), ["image_01.png", "image_02.png"])
            self.assertIsNone(zipped.testzip())
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in zipped.infolist()))
        self.assertEqual(len(wire.requests), 4)

    def test_image_error_container_is_not_saved_as_html_or_full_pass(self):
        runner, _ = self.runner([
            ("GET", "/results/", response(parsed(card(1) + card(2)))),
            ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(b"<html>" + SECRET.encode())),
        ])
        result = runner.run_sample("xhs_images", dict(smoke.SAMPLES)["xhs_images"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["images"][1]["error_code"], "WRONG_MEDIA_CONTAINER")
        self.assertFalse((self.directory / "xhs_images/image_02.media").exists())
        with zipfile.ZipFile(self.directory / "xhs_images/images.zip") as zipped:
            self.assertIn("failed.txt", zipped.namelist())
            self.assertNotIn(SECRET, zipped.read("failed.txt").decode())

    def test_missing_length_not_pass_even_with_valid_video(self):
        self.need_video()
        runner, _ = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", media(self.video, length=False)),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["status"], "insufficient_evidence", result)
        self.assertEqual(result["download"]["width"], 32)

    def test_unknown_parse_not_retried_next_sample_runs_and_errors_redacted(self):
        runner, wire = self.runner([
            ("GET", "/results/", requests.ReadTimeout("https://secret.example/" + SECRET)),
            ("GET", "/results/", response({"status": "error", "error_code": SECRET, "message": SECRET}, status=403)),
        ])
        first = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        second = runner.run_sample("youtube", dict(smoke.SAMPLES)["youtube"])
        self.assertEqual(first["error_code"], "HTTP_OUTCOME_UNKNOWN")
        self.assertEqual(second["error_code"], "CONTROL_HTTP_FAILED")
        self.assertEqual([first["parse_attempts"], second["parse_attempts"]], [1, 1])
        self.assertEqual(len(wire.requests), 3)
        for path in self.directory.rglob("*.json*"):
            self.assertNotIn(SECRET, path.read_text())

    def test_unknown_stream_not_retried(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", requests.ReadTimeout(SECRET)),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["error_code"], "HTTP_OUTCOME_UNKNOWN", result)
        self.assertEqual(result["media_http_attempts"], 1)
        self.assertEqual(len(wire.requests), 4)

    def test_expired_selection_never_fetches_media(self):
        expired = item()
        expired["selection_expires_at"] = 1
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": expired})),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["error_code"], "SELECTION_EXPIRED")
        self.assertEqual(result["media_http_attempts"], 0)
        self.assertFalse(wire.steps)

    def test_continuation_polls_without_reparse(self):
        self.need_video()
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed("", status="continuation_pending", has_results=False,
                continuation_token=SECRET, poll_after_ms=500, expires_in_seconds=15))),
            ("POST", "/media/douyin-continuation/", response({"status": "continuation_pending", "poll_after_ms": 500})),
            ("POST", "/media/douyin-continuation/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", media(self.video)),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["parse_attempts"], 1)
        self.assertEqual(len([r for r in wire.requests if urlsplit(r.url).path == "/media/douyin-continuation/"]), 2)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_expired_continuation_does_not_reparse(self):
        runner, wire = self.runner([("GET", "/results/", response(parsed("", status="continuation_pending",
            has_results=False, continuation_token=SECRET, expires_in_seconds=0)))])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["error_code"], "CONTINUATION_NO_RESULT")
        self.assertEqual(len(wire.requests), 2)

    def test_cross_origin_descriptor_rejected_before_csrf_send(self):
        html = picker().replace("/media/service-selected-format/", "https://evil.example/media/service-selected-format/")
        runner, wire = self.runner([("GET", "/results/", response(parsed(html)))])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["error_code"], "CROSS_ORIGIN_CONTROL_URL")
        self.assertEqual(len(wire.requests), 2)

    def test_private_round_refuses_resume_and_dry_run_has_no_http(self):
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "attempts.jsonl").stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(smoke.SmokeError, "ROUND_EXISTS_NO_RESUME"):
            smoke.create_round(self.directory.parent, self.directory.name)
        with mock.patch.object(requests.Session, "request", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(["--dry-run"]), 0)
            self.assertEqual(smoke.main(["--list"]), 0)
            self.assertEqual(smoke.main([]), 0)

    def run_cli_round(self, name, steps, *, exclude=False, ffprobe=None):
        target = self.directory.parent / name
        wire = Wire([("GET", "/", response(HOME)), *steps], target / "attempts.jsonl")
        http_class = smoke.PublicHTTP
        args = ["--run", "--base-url", "https://site.example", "--round", name,
                "--output", str(self.directory.parent), "--ffprobe", ffprobe or sys.executable]
        if exclude:
            args.append("--youtube-not-public")
        with mock.patch.object(smoke, "PublicHTTP", side_effect=lambda base, journal:
                               http_class(base, journal, session_factory=wire.factory)), contextlib.redirect_stdout(io.StringIO()):
            code = smoke.main(args)
        self.assertFalse(wire.steps)
        return code, json.loads((target / "report.json").read_text()), wire

    def test_cli_browser_first_bilibili_and_native_descriptors_are_http_evidence_only(self):
        self.need_video()
        for entry in ("card", "picker"):
            for strategy in ("browser_native", "browser_streaming"):
                with self.subTest(entry=entry, strategy=strategy):
                    native = {"download_strategy": "browser_native",
                              "native_download_expires_at": int((time.time() + 300) * 1000)}
                    ordinary = smoke.card_item(smoke.BeautifulSoup(card(1, "video"), "html.parser").div)
                    bili = item(provider="tiantianjiexi_generic") if entry == "picker" else dict(ordinary)
                    bili.update(source_platform="bilibili", **native)
                    bili["download_strategy"] = strategy
                    bili_html = picker(provider="tiantianjiexi_generic") if entry == "picker" else card(
                        1, "video", **{"source-platform": "bilibili"})
                    steps = [
                        ("GET", "/results/", response(parsed(card(1) + card(2)))),
                        ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(self.png)),
                        ("GET", "/results/", response(parsed(card(1, "video")))),
                        ("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                            "download_strategy": "browser_direct_provider_video", "items": [{**ordinary, **native}]})),
                        ("GET", "/1.png", media(self.video)),
                        ("GET", "/results/", response(parsed(picker()))),
                        ("POST", "/media/service-selected-format/", response({"status": "success", "item": {**item(), **native}})),
                        ("GET", "/video.mp4", media(self.video)),
                        ("GET", "/results/", response(parsed(bili_html))),
                    ]
                    if entry == "picker":
                        steps.append(("POST", "/media/service-selected-format/", response({"status": "success", "item": bili})))
                    else:
                        steps.append(("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                            "download_strategy": "browser_direct_provider_video", "items": [bili]})))
                    steps.append(("GET", urlsplit(bili["url"]).path, media(self.video)))
                    code, report, wire = self.run_cli_round("browser-" + entry + "-" + strategy, steps,
                                                           exclude=True, ffprobe=self.ffprobe)
                    self.assertEqual(code, 0, report)
                    self.assertTrue(report["passed"])
                    self.assertEqual(report["scope"], "anonymous_http_smoke")
                    for key in ("browser_native_verified", "browser_sw_verified", "browser_zip_verified", "ui_auto_replace_verified"):
                        self.assertIs(report[key], False)
                    for result in report["samples"]:
                        self.assertEqual(result["parse_attempts"], 1)
                        self.assertEqual(result["scope"], "anonymous_http_smoke")
                        self.assertIs(result["browser_native_verified"], False)
                        self.assertIs(result["browser_sw_verified"], False)
                    for result in report["samples"][1:]:
                        self.assertEqual(result["transport"], "http_direct_media")
                        self.assertEqual(result["media_http_attempts"], 1)
                        self.assertEqual(result["saved_count"], 1)
                    self.assertEqual(report["samples"][-1]["policies"][-1]["download_strategy"], strategy)
                    for request in wire.requests:
                        path = urlsplit(request.url).path
                        self.assertNotIn(path, ("/media/download-prepare/", "/media/download-tasks/"))
                        if path == "/media/download-images/":
                            payload = json.loads(request.body)
                            self.assertEqual(payload["intent"], "download")
                            self.assertIs(payload["route_only"], True)
                            self.assertEqual(payload["download_mode"], "browser")
                    self.assertNotIn(SECRET, json.dumps(report))

    def test_ordinary_preparation_denial_never_fetches_media_or_retries(self):
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(card(1, "video")))),
            ("POST", "/media/download-images/", response({"status": "error"}, status=403)),
        ])
        result = runner.run_sample("bilibili", dict(smoke.SAMPLES)["bilibili"])
        self.assertEqual(result["error_code"], "CONTROL_HTTP_FAILED")
        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))
        self.assertEqual(json.loads(wire.requests[-1].body)["intent"], "download")
        self.assertFalse(wire.steps)

    def test_nullable_cookie_descriptor_requires_authoritative_browser_safety(self):
        self.need_video()
        for strategy in ("browser_native", "browser_streaming"):
            for index, (cookie, safe, accepted) in enumerate(((None, True, True), (True, True, False), (None, False, False))):
                with self.subTest(strategy=strategy, requires_cookie=cookie, safe=safe):
                    selected = {**item(), "download_strategy": strategy, "requires_cookie": cookie,
                                "browser_direct_fetch_safe": safe,
                                "native_download_expires_at": int((time.time() + 300) * 1000)}
                    steps = [("GET", "/results/", response({"status": "error"}, status=403)) for _ in range(3)]
                    steps.extend([
                        ("GET", "/results/", response(parsed(picker()))),
                        ("POST", "/media/service-selected-format/", response({"status": "success", "item": selected})),
                    ])
                    if accepted:
                        steps.append(("GET", "/video.mp4", media(self.video)))
                    _, report, _ = self.run_cli_round("nullable-" + strategy + "-" + str(index), steps,
                                                      exclude=True, ffprobe=self.ffprobe)
                    result = report["samples"][-1]
                    self.assertEqual(result["parse_attempts"], 1)
                    self.assertEqual(result["media_http_attempts"], int(accepted))
                    if accepted:
                        self.assertEqual(result["status"], "passed", result)
                        self.assertEqual(result["saved_count"], 1)
                        self.assertEqual(result["transport"], "http_direct_media")
                        self.assertIs(result["browser_native_verified"], False)
                        self.assertIs(result["browser_sw_verified"], False)
                    else:
                        self.assertEqual(result["error_code"], "DIRECT_BINDING_MISMATCH")

    def test_ordinary_route_accepts_only_empty_optional_identity_projection(self):
        self.need_video()
        original = smoke.card_item(smoke.BeautifulSoup(card(1, "video"), "html.parser").div)
        for index, identity in enumerate(({}, {"asset_id": None, "provider": None}, {"asset_id": "", "provider": ""})):
            with self.subTest(identity=identity):
                # public_route_item omits absent/empty identity fields from ordinary cards.
                projected = {"url": original["url"], "type": "video", "browser_direct_fetch_safe": True,
                             "requires_merge": False, "download_strategy": "browser_streaming", **identity}
                steps = [("GET", "/results/", response({"status": "error"}, status=403)) for _ in range(3)]
                steps.extend([
                    ("GET", "/results/", response(parsed(card(1, "video")))),
                    ("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                        "download_strategy": "browser_direct_provider_video", "items": [projected]})),
                    ("GET", "/1.png", media(self.video)),
                ])
                _, report, wire = self.run_cli_round("empty-identity-" + str(index), steps,
                                                     exclude=True, ffprobe=self.ffprobe)
                result = report["samples"][-1]
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 1))
                sent = json.loads(wire.requests[-2].body)["media_items"][0]
                self.assertEqual((sent["asset_id"], sent["provider"]), ("", ""))

    def test_route_identity_normalization_keeps_resource_and_nonempty_bindings_strict(self):
        cases = [({}, {"url": "https://cdn.example/other.mp4"}),
                 ({}, {"type": "audio"}),
                 ({}, {"asset_id": "unexpected-id"}),
                 ({}, {"provider": "unexpected-provider"}),
                 ({}, {"asset_id": 0}),
                 ({}, {"provider": False})]
        for key in ("asset_id", "provider"):
            for value in (None, "", "changed"):
                cases.append(({key.replace("_", "-"): "original"}, {key: value}))
        for index, (attributes, changes) in enumerate(cases):
            with self.subTest(attributes=attributes, changes=changes):
                html = card(1, "video", **attributes)
                original = smoke.card_item(smoke.BeautifulSoup(html, "html.parser").div)
                steps = [("GET", "/results/", response({"status": "error"}, status=403)) for _ in range(3)]
                steps.extend([
                    ("GET", "/results/", response(parsed(html))),
                    ("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                        "download_strategy": "browser_direct_provider_video", "items": [{**original, **changes}]})),
                ])
                _, report, _ = self.run_cli_round("strict-identity-" + str(index), steps, exclude=True)
                result = report["samples"][-1]
                self.assertEqual(result["error_code"], "ROUTE_BINDING_MISMATCH")
                self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))

    def test_native_descriptor_requires_live_expiry_on_picker_and_card(self):
        for entry in ("card", "picker"):
            for expiry in (None, 1, True, "9999999999999"):
                with self.subTest(entry=entry, expiry=expiry):
                    ordinary = smoke.card_item(smoke.BeautifulSoup(card(1, "video"), "html.parser").div)
                    selected = item(provider="tiantianjiexi_generic") if entry == "picker" else ordinary
                    selected["download_strategy"] = "browser_native"
                    if expiry is not None:
                        selected["native_download_expires_at"] = expiry
                    steps = [("GET", "/results/", response({"status": "error"}, status=403)) for _ in range(3)]
                    steps.append(("GET", "/results/", response(parsed(
                        picker(provider="tiantianjiexi_generic") if entry == "picker" else card(1, "video")))))
                    if entry == "picker":
                        steps.append(("POST", "/media/service-selected-format/", response({"status": "success", "item": selected})))
                    else:
                        steps.append(("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                            "download_strategy": "browser_direct_provider_video", "items": [selected]})))
                    code, report, _ = self.run_cli_round("expiry-" + entry + "-" + str(expiry), steps, exclude=True)
                    self.assertEqual(code, 1)
                    result = report["samples"][-1]
                    self.assertEqual(result["error_code"], "NATIVE_DESCRIPTOR_EXPIRED")
                    self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 0))

    def test_prepared_route_authority_and_policy_are_checked_before_media(self):
        cases = [({"selection_expires_at": 1}, "SELECTION_EXPIRED"),
                 ({"expiresAt": 1}, "SELECTION_EXPIRED"),
                 ({"download_strategy": "future_unknown_strategy"}, "UNRECOGNIZED_ROUTE_POLICY"),
                 ({"requires_cookie": True}, "DIRECT_ROUTE_UNAUTHORIZED"),
                 ({"requires_merge": True}, "DIRECT_ROUTE_UNAUTHORIZED")]
        for index, (changes, expected) in enumerate(cases):
            with self.subTest(changes=changes):
                ordinary = smoke.card_item(smoke.BeautifulSoup(card(1, "video"), "html.parser").div)
                steps = [("GET", "/results/", response({"status": "error"}, status=403)) for _ in range(3)]
                steps.extend([
                    ("GET", "/results/", response(parsed(card(1, "video")))),
                    ("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                        "download_strategy": "browser_direct_provider_video", "items": [{**ordinary, **changes}]})),
                ])
                code, report, _ = self.run_cli_round("route-authority-" + str(index), steps, exclude=True)
                self.assertEqual(code, 1)
                self.assertEqual(report["samples"][-1]["error_code"], expected)
                self.assertEqual(report["samples"][-1]["media_http_attempts"], 0)

    def test_unknown_native_http_result_never_retries_or_switches_transport(self):
        native = {**item(), "download_strategy": "browser_native",
                  "native_download_expires_at": int((time.time() + 300) * 1000)}
        runner, wire = self.runner([
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": native})),
            ("GET", "/video.mp4", requests.ReadTimeout(SECRET)),
        ])
        result = runner.run_sample("douyin", dict(smoke.SAMPLES)["douyin"])
        self.assertEqual(result["error_code"], "HTTP_OUTCOME_UNKNOWN", result)
        self.assertEqual(result["transport"], "http_direct_media")
        self.assertEqual((result["parse_attempts"], result["media_http_attempts"]), (1, 1))
        self.assertFalse(wire.steps)

    def test_default_still_parses_youtube_and_403_never_auto_excludes(self):
        for exclude, count in ((False, 5), (True, 4)):
            with self.subTest(exclude=exclude):
                steps = [("GET", "/results/", response({"status": "error",
                          "error_code": "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED"}, status=403)) for _ in range(count)]
                code, report, wire = self.run_cli_round("denied-" + str(exclude), steps, exclude=exclude)
                queries = [parse_qs(urlsplit(r.url).query)["q"][0] for r in wire.requests[1:]]
                self.assertEqual(dict(smoke.SAMPLES)["youtube"] in queries, not exclude)
                self.assertEqual(len(report["required_samples"]), count)
                self.assertEqual(len(report["excluded"]), int(exclude))
                self.assertTrue(all(s["status"] == "failed" for s in report["samples"]))
                self.assertFalse(report["passed"])
                self.assertEqual(code, 1)

    def test_explicit_youtube_exclusion_allows_only_four_verified_required_samples(self):
        self.need_video()
        native_url = "https://cdn.example/native.mp4"
        original = item(legacy=True, provider="tiantianjiexi_generic")
        steps = [
            ("GET", "/results/", response(parsed(card(1) + card(2)))),
            ("GET", "/1.png", media(self.png)), ("GET", "/2.png", media(self.png)),
            ("GET", "/results/", response(parsed(card(1, "video", url=native_url)))),
            ("POST", "/media/download-images/", response({"status": "routed", "route_type": "direct",
                "download_strategy": "browser_direct_provider_video", "items": [{"type": "video", "url": native_url,
                "provider": "", "asset_id": "", "browser_direct_fetch_safe": True}]})),
            ("GET", "/native.mp4", media(self.video)),
            ("GET", "/results/", response(parsed(picker()))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": item()})),
            ("GET", "/video.mp4", media(self.video)),
            ("GET", "/results/", response(parsed(picker(True, "tiantianjiexi_generic")))),
            ("POST", "/media/service-selected-format/", response({"status": "success", "item": original})),
            ("POST", "/media/download-images/", response({"status": "routed", "route_type": "proxy",
                "download_strategy": "server_single_attachment", "items": [{"url": original["url"]}]})),
            ("POST", "/media/download-prepare/", response({"ok": True, "status": "issued", "expires_in_seconds": 60,
                "download_url": "/media/download-stream/private-stream/", "status_url": "/media/download-status/private-status/"})),
            ("GET", "/media/download-stream/private-stream/", media(self.video)),
            ("GET", "/media/download-status/private-status/", response({"status": "completed"})),
        ]
        code, report, wire = self.run_cli_round("four-required", steps, exclude=True, ffprobe=self.ffprobe)
        self.assertEqual(code, 0, report)
        self.assertTrue(report["passed"])
        self.assertEqual(report["required_samples"], [name for name, _ in smoke.SAMPLES if name != "youtube"])
        self.assertEqual(len(report["samples"]), 4)
        excluded = report["excluded"][0]
        self.assertEqual((excluded["sample"], excluded["status"], excluded["parse_attempts"]), ("youtube", "excluded", 0))
        self.assertEqual(excluded["reason_code"], "OPERATOR_CONFIRMED_NOT_PUBLIC")
        self.assertIn("--youtube-not-public", excluded["reason"])
        parses = [r for r in wire.requests if urlsplit(r.url).path == "/results/"]
        self.assertEqual(len(parses), 4)
        self.assertFalse(any("youtube.com" in parse_qs(urlsplit(r.url).query)["q"][0] for r in parses))

    def test_dry_run_exposes_explicit_exclusion_and_keeps_fixed_link(self):
        for flags, count in (([], 5), (["--youtube-not-public"], 4)):
            output = io.StringIO()
            with mock.patch.object(requests.Session, "request", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(output):
                self.assertEqual(smoke.main(["--dry-run", *flags]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["samples"]["youtube"], dict(smoke.SAMPLES)["youtube"])
            self.assertEqual(len(plan["required_samples"]), count)
            self.assertEqual(plan["youtube_not_public"], bool(flags))
            self.assertEqual(len(plan["excluded"]), int(bool(flags)))
            self.assertEqual(plan["network_requests"], 0)

    def test_length_mismatch_and_wrong_video_container(self):
        for body, declared, code in [(b"<html>" + SECRET.encode(), 20, "WRONG_MEDIA_CONTAINER"),
                                     (b"\x00\x00\x00\x18ftypisom" + b"data", 99, "CONTENT_LENGTH_MISMATCH")]:
            with self.subTest(code=code):
                # Receive tests use the same request boundary, without parsing doubles.
                wire = Wire([("GET", "/bad.mp4", response(body, headers={"Content-Length": str(declared)}))],
                            self.directory / "attempts.jsonl")
                wire.requests = [None] * sum(self.journal.counts.values())
                http = smoke.PublicHTTP("https://site.example", self.journal, session_factory=wire.factory)
                try:
                    saved = smoke.receive(http, "https://cdn.example/bad.mp4", self.directory / (code + ".media"), "video")
                finally:
                    http.close()
                self.assertEqual(saved["error_code"], code)
