"""Offline fixture gates, discovered by the default Django unittest runner."""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from xhs_download.services.provider_adapters import recordings


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "xhs_download/fixtures/provider_raw"
MODULE = "xhs_download.services.provider_adapters.recordings"
SYNTHETIC_JWT = (
    "eyJzeW50aGV0aWMiOiJ0ZXN0In0.eyJzeW50aGV0aWMiOnRydWV9."
    "c3ludGhldGljLXNpZ25hdHVyZQ"
)


def recording(raw=None, **changes):
    result = {
        "schema_version": 1, "provider": "synthetic_provider", "platform": "synthetic",
        "source_url": "https://source.example/work/1",
        "provenance": {"kind": "synthetic", "raw_kind": "json", "complete": True},
        "raw": {} if raw is None else raw,
    }
    result.update(changes)
    return result


class RecordingTests(unittest.TestCase):
    def test_bracketed_and_quoted_plain_titles_are_not_required_to_be_json(self):
        for title in ('[Official] Sample title', '{Live} Sample title',
                      '"Quoted title" - Episode 1'):
            with self.subTest(title=title):
                original = recording({"title": title})
                self.assertEqual(recordings.sanitize(original), original)
                recordings.check(original)

    def test_preserves_raw_structure_order_types_and_does_not_mutate(self):
        raw = {"items": [{"id": str(i), "height": 1080, "tracks": "unknown"}
                         for i in range(130)],
               "values": [None, False, True, 1, 0, 1.5, "", "1080P+"],
               "url": "HTTPS://CDN.example/a%20b/original.mp4?v=1&v=2&empty=#chapter"}
        original = recording(raw)
        before = deepcopy(original)
        self.assertEqual(recordings.sanitize(original), before)
        self.assertEqual(original, before)
        recordings.check(original)

    def test_fields_nested_containers_headers_and_cookie_structures(self):
        raw = {"headers": {"Cookie": "sid=synthetic-cookie; other=synthetic-other",
                           "Authorization": "Bearer synthetic-auth", "Content-Type": "video/mp4"},
               "cookies": [{"name": "sid", "value": "synthetic-cookie", "domain": "cdn.example",
                            "path": "/media", "httpOnly": True}],
               "cookie_jar": {"sid": "synthetic-sid", "other": "synthetic-other"},
               "token": {"primary": "synthetic-token", "alternates": ["synthetic-alt", 123]},
               "fields": [{"name": "access_token", "value": "synthetic-field"}]}
        result = recordings.sanitize(recording(raw))["raw"]
        self.assertEqual(result["headers"], {"Cookie": "REDACTED", "Authorization": "REDACTED",
                                             "Content-Type": "video/mp4"})
        self.assertEqual(result["cookies"][0], {**raw["cookies"][0], "value": "REDACTED"})
        self.assertEqual(result["cookie_jar"], {"sid": "REDACTED", "other": "REDACTED"})
        self.assertEqual(result["token"], {"primary": "REDACTED", "alternates": ["REDACTED", "REDACTED"]})
        self.assertEqual(result["fields"][0]["value"], "REDACTED")
        recordings.check(recording(result))

    def test_repeated_encoded_queries_nested_urls_userinfo_and_fragment(self):
        value = ("https://synthetic-user:synthetic-password@CDN.example/a%20b.mp4"
                 "?v=1&ToKeN=synthetic-one&%74oken=synthetic-two&%2573ig=synthetic-sign"
                 "&next=https%3A%2F%2Fother.example%2Fv.mp4%3Fsignature%3Dsynthetic-nested"
                 "#access_token=synthetic-fragment&chapter=2")
        expected = ("https://REDACTED:REDACTED@CDN.example/a%20b.mp4"
                    "?v=1&ToKeN=REDACTED&%74oken=REDACTED&%2573ig=REDACTED"
                    "&next=https%3A%2F%2Fother.example%2Fv.mp4%3Fsignature%3DREDACTED"
                    "#access_token=REDACTED&chapter=2")
        result = recordings.sanitize(recording({"url": value}, source_url=value))
        self.assertEqual(result["raw"]["url"], expected)
        self.assertEqual(result["source_url"], expected)
        self.assertEqual(recordings.sanitize(result), result)
        recordings.check(result)

    def test_html_embedded_urls_assignments_and_cookie_lines(self):
        raw = ('<video src="https://cdn.example/a.mp4?v=1&amp;sig=synthetic-sign"></video>'
               '<script>{"token":"synthetic-token"}</script>'
               '<p>Cookie: sid=synthetic-cookie; other=synthetic-other</p>'
               '<p>Bearer synthetic-auth</p>')
        value = recording(raw, provenance={"kind": "synthetic", "raw_kind": "html", "complete": True})
        result = recordings.sanitize(value)
        self.assertEqual(result["raw"],
                         '<video src="https://cdn.example/a.mp4?v=1&amp;sig=REDACTED"></video>'
                         '<script>{"token":"REDACTED"}</script>'
                         '<p>Cookie: REDACTED</p><p>Bearer REDACTED</p>')
        recordings.check(result)

    def test_cookie_query_does_not_consume_following_safe_parameters(self):
        url = "https://cdn.example/a.mp4?cookie=synthetic-private&v=1&v=2"
        result = recordings.sanitize(recording({"url": url, "text": "Cookie=sid=synthetic-a; b=synthetic-b"}))
        self.assertEqual(result["raw"], {
            "url": "https://cdn.example/a.mp4?cookie=REDACTED&v=1&v=2",
            "text": "Cookie=REDACTED",
        })

    def test_quoted_cookie_lines_are_rejected_without_partial_redaction(self):
        for header in ("Cookie", "Set-Cookie"):
            raw = f'{header}: arbitrary="synthetic-private"; other=synthetic-other'
            for action in (recordings.check, recordings.sanitize):
                with self.subTest(header=header, action=action.__name__):
                    with self.assertRaisesRegex(recordings.RecordingError, "UNSUPPORTED_QUOTING"):
                        action(recording(raw))

    def test_json_strings_with_escaped_quotes_preserve_structure(self):
        data = {"token": 'synthetic-prefix"synthetic-private',
                "title": 'keep "quoted" title', "items": [1, None, False]}
        raw = json.dumps(data)
        expected = {**data, "token": "REDACTED"}
        for prefix, suffix in (("", ""), ("<script>", "</script>")):
            original = recording(prefix + raw + suffix)
            with self.subTest(prefix=prefix):
                with self.assertRaises(recordings.RecordingError):
                    recordings.check(original)
                clean = recordings.sanitize(original)
                content = clean["raw"][len(prefix):len(clean["raw"]) - len(suffix) if suffix else None]
                self.assertEqual(json.loads(content), expected)
                self.assertNotIn("synthetic-private", clean["raw"])
                recordings.check(clean)

    def test_embedded_json_uses_strict_duplicate_and_number_rules(self):
        cases = (("{\"x\":1,\"x\":2}", "DUPLICATE_JSON_KEY"),
                 ("{\"x\":NaN}", "NON_FINITE_NUMBER"),
                 ("{\"x\":Infinity}", "NON_FINITE_NUMBER"),
                 ("{\"x\":-Infinity}", "NON_FINITE_NUMBER"),
                 ("{\"x\":1e999}", "NON_FINITE_NUMBER"))
        for raw, code in cases:
            for prefix, suffix in (("", ""), ("<script>", "</script>")):
                for action in (recordings.check, recordings.sanitize):
                    with self.subTest(raw=raw, prefix=prefix, action=action.__name__):
                        with self.assertRaisesRegex(recordings.RecordingError, code):
                            action(recording(prefix + raw + suffix))

    def test_header_pairs_keep_names_order_and_only_redact_values(self):
        raw = {"headers": [["Cookie", "arbitrary=synthetic-private"],
                           ["X-Api-Key", "synthetic-private-key"],
                           ["Content-Type", "video/mp4"],
                           ["Cookie", "other=synthetic-other"]]}
        original = recording(raw)
        before = deepcopy(original)
        with self.assertRaises(recordings.RecordingError):
            recordings.check(original)
        clean = recordings.sanitize(original)
        self.assertEqual(clean["raw"]["headers"], [
            ["Cookie", "REDACTED"], ["X-Api-Key", "REDACTED"],
            ["Content-Type", "video/mp4"], ["Cookie", "REDACTED"],
        ])
        self.assertEqual(original, before)
        recordings.check(clean)

    def test_nested_url_redaction_preserves_encoded_path_and_layers(self):
        nested = "https://other.example/a%2Fb.mp4?sig=synthetic-private&v=1&v=2"
        expected = nested.replace("synthetic-private", "REDACTED")
        for layers in (1, 2):
            encoded, expected_encoded = nested, expected
            for _ in range(layers):
                encoded = quote(encoded, safe="")
                expected_encoded = quote(expected_encoded, safe="")
            url = "https://cdn.example/a?next=" + encoded
            with self.subTest(layers=layers):
                clean = recordings.sanitize(recording({"url": url}))
                self.assertEqual(clean["raw"]["url"], "https://cdn.example/a?next=" + expected_encoded)
                recordings.check(clean)
                self.assertEqual(recordings.sanitize(clean), clean)
                if layers == 1:
                    decoded = parse_qs(urlsplit(clean["raw"]["url"]).query)["next"][0]
                    self.assertEqual(urlsplit(decoded).path, "/a%2Fb.mp4")

    def test_entity_encoded_html_attribute_keeps_its_json_value(self):
        class Attributes(HTMLParser):
            def handle_starttag(self, tag, attrs):
                self.attrs = dict(attrs)

        raw = ('<div data-state="{&quot;token&quot;:&quot;synthetic-private&quot;,'
               '&quot;title&quot;:&quot;keep&quot;}">x</div>')
        doc = recording(raw, provenance={"kind": "synthetic", "raw_kind": "html", "complete": True})
        with self.assertRaises(recordings.RecordingError):
            recordings.check(doc)
        clean = recordings.sanitize(doc)
        parser = Attributes()
        parser.feed(clean["raw"])
        self.assertEqual(set(parser.attrs), {"data-state"})
        self.assertEqual(json.loads(parser.attrs["data-state"]), {"token": "REDACTED", "title": "keep"})
        self.assertIn("&quot;", clean["raw"])
        recordings.check(clean)
        self.assertEqual(recordings.sanitize(clean), clean)

    def test_wrapped_assignments_encoded_text_and_other_url_userinfo(self):
        values = ["message=example?token=synthetic-secret",
                  "https%3A%2F%2Fcdn.example%2Fv.mp4%3Fsig%3Dsynthetic-secret",
                  "ftp://synthetic-user:synthetic-password@cdn.example/a.mp4",
                  "//synthetic-user@cdn.example/a.mp4#token=synthetic-secret"]
        for value in values:
            with self.subTest(), self.assertRaises(recordings.RecordingError):
                recordings.check(recording({"text": value}))
            result = recordings.sanitize(recording({"text": value}))
            self.assertNotIn("synthetic-", result["raw"]["text"])
            recordings.check(result)

    def test_independent_check_catches_fields_and_known_secret_shapes(self):
        values = [{"token": "synthetic-token"}, {"url": "https://cdn.example/a?sig=synthetic"},
                  {"unrelated": SYNTHETIC_JWT}, {"unrelated": "sk-synthetic-private-key-value"},
                  {"unrelated": "ghp_SYNTHETICabcdefghijklmnop123456"},
                  {"unrelated": "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----"},
                  {"unrelated": "Basic c3ludGhldGljOnNlY3JldA=="},
                  {"unrelated": "access_token%3Dsynthetic-encoded"}]
        for raw in values:
            with self.subTest(raw_kind=next(iter(raw))), patch.object(
                    recordings, "sanitize", side_effect=AssertionError("not a sanitizer comparison")):
                with self.assertRaises(recordings.RecordingError):
                    recordings.check(recording(raw))
            cleaned = recordings.sanitize(recording(raw))
            recordings.check(cleaned)

    def test_host_and_path_credentials_rejected_without_rewriting(self):
        values = ["https://cdn.example/token/synthetic-secret/a.mp4",
                  "https://cdn.example/a%3Ftoken%3Dsynthetic-secret",
                  "https://cdn.example/a=one/token=synthetic-secret",
                  "https://sk-synthetic-private-key.example/a.mp4",
                  "https://cdn.example/" + SYNTHETIC_JWT + "/a.mp4"]
        for url in values:
            for action in (recordings.check, recordings.sanitize):
                with self.subTest(action=action.__name__), self.assertRaisesRegex(
                        recordings.RecordingError, "CREDENTIAL_IN_HOST_OR_PATH"):
                    action(recording({"url": url}))

    def test_no_object_properties_or_custom_iterators_are_executed(self):
        class Dangerous:
            @property
            def __dict__(self):
                raise AssertionError("property evaluated")

            def __iter__(self):
                raise AssertionError("iterator evaluated")

        class CustomDict(dict):
            def items(self):
                raise AssertionError("custom method evaluated")

        for value in (Dangerous(), CustomDict(), ("tuple",)):
            with self.assertRaisesRegex(recordings.RecordingError, "NOT_JSON"):
                recordings.sanitize(recording(value))

    def test_invalid_envelopes_and_incomplete_exports_rejected(self):
        for changes in ({"schema_version": True}, {"schema_version": 2}, {"extra": True},
                        {"provider": "bad provider"}, {"source_url": "not-a-url"},
                        {"provenance": {"kind": "recorded", "raw_kind": "json", "complete": False}},
                        {"provenance": {"kind": "unknown", "raw_kind": "json", "complete": True}},
                        {"provenance": {"kind": "synthetic", "raw_kind": "object", "complete": True}},
                        {"provenance": {"kind": "synthetic", "raw_kind": "html", "complete": True}}):
            for action in (recordings.check, recordings.sanitize):
                with self.subTest(fields=list(changes)), self.assertRaises(recordings.RecordingError):
                    action(recording(**changes))

    def test_strict_json_duplicates_nonfinite_numbers_and_cycles(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.json"
            for text in ('{"duplicate":1,"duplicate":2}', '{"nested":{"x":1,"x":2}}',
                         '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
                         json.dumps(recording()).replace('"raw": {}', '"raw": 1e999')):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(recordings.RecordingError):
                    recordings.load_recording(path)
        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, float("nan"), float("inf")):
            with self.assertRaises(recordings.RecordingError):
                recordings.sanitize(recording(value))

    def test_safe_error_does_not_echo_key_value_or_parser_input(self):
        for raw in ({"synthetic-secret-token=synthetic-value": "x"}, {"token": "synthetic-value"}):
            with self.assertRaises(recordings.RecordingError) as raised:
                recordings.check(recording(raw))
            self.assertNotIn("synthetic", str(raised.exception))

    def test_committed_raw_fixture_directory(self):
        self.assertTrue(FIXTURES.is_dir())
        for path in sorted(FIXTURES.rglob("*.json")):
            with self.subTest(fixture=path.relative_to(FIXTURES).as_posix()):
                document = recordings.load_recording(path)
                self.assertEqual(path.parent.parent, FIXTURES)
                self.assertEqual(path.parent.name, document["provider"])
                recordings.check(document)

    def test_fixture_directory_gate_rejects_a_new_unsafe_sample(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "synthetic_provider"
            directory.mkdir()
            path = directory / "synthetic.json"
            path.write_text(json.dumps(recording({"token": "synthetic-secret"})), encoding="utf-8")
            with patch(__name__ + ".FIXTURES", root):
                # Call without subTest so a rejection propagates to this assertion.
                for fixture in FIXTURES.rglob("*.json"):
                    with self.assertRaises(recordings.RecordingError):
                        recordings.check(recordings.load_recording(fixture))
            path.write_text(json.dumps(recordings.sanitize(recording({"token": "synthetic-secret"}))),
                            encoding="utf-8")
            with patch(__name__ + ".FIXTURES", root):
                self.test_committed_raw_fixture_directory()


class RecordingCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", MODULE, *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=15)

    def test_real_cli_file_round_trip_and_safe_output(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic-input.json"
            target = Path(temp) / "synthetic-clean.json"
            original = recording({"url": "https://cdn.example/media.mp4?token=synthetic-private"})
            source.write_text(json.dumps(original), encoding="utf-8")
            failed = self.run_cli("check", source)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(failed.stdout, "")
            self.assertNotIn("synthetic-private", failed.stderr)
            clean = self.run_cli("sanitize", source, "--output", target)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertEqual(clean.stdout, "OK: recording sanitized\n")
            self.assertEqual(clean.stderr, "")
            self.assertEqual(json.loads(source.read_text()), original)
            self.assertEqual(recordings.load_recording(target)["raw"]["url"],
                             "https://cdn.example/media.mp4?token=REDACTED")
            checked = self.run_cli("check", target)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(checked.stdout, "OK: recording checked\n")

    def test_cli_rejects_without_output_file_or_leaking_inputs(self):
        with TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic-private-input.json"
            target = Path(temp) / "out.json"
            for contents in ('{"synthetic-secret":', '{"token":NaN}', '{"x":1,"x":2}',
                             json.dumps(recording({"url": "https://cdn.example/token/synthetic-private"}))):
                source.write_text(contents, encoding="utf-8")
                result = self.run_cli("sanitize", source, "--output", target)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("synthetic", result.stderr)
                self.assertFalse(target.exists())
            result = self.run_cli("--synthetic-secret-argument")
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("synthetic", result.stderr)
