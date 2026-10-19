import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ops.nginx.tests.run_nginx_acceptance import REQUIRED_MEDIA_PATHS, without_exact_location


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = PROJECT_ROOT / "ops" / "nginx" / "tools" / "check_nginx_drift.py"
SPEC = importlib.util.spec_from_file_location("check_nginx_drift", MODULE_PATH)
drift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drift)


class NginxDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = drift.SITE_TEMPLATE_PATH.read_text()
        cls.site = drift.render_site_template(cls.template, "192.0.2.10")
        cls.snippet = drift.MEDIA_SNIPPET_PATH.read_text()

    def test_canonical_baseline_passes(self):
        drift.validate_baseline(self.site, self.snippet)

    def test_deleting_any_required_exact_media_location_is_rejected(self):
        for path in (*REQUIRED_MEDIA_PATHS, drift.TELEMETRY_PATH):
            with self.subTest(path=path):
                changed = without_exact_location(self.snippet, path)
                self.assertNotIn(("=", path), {item.args for item in drift.parse_nginx(changed)})
                with self.assertRaises(drift.ConfigDriftError):
                    drift.validate_baseline(self.site, changed)

    def test_new_media_locations_cannot_be_prefixes(self):
        for path in REQUIRED_MEDIA_PATHS:
            with self.subTest(path=path):
                changed = self.snippet.replace(f"location = {path} {{", f"location ^~ {path} {{", 1)
                with self.assertRaises(drift.ConfigDriftError):
                    drift.validate_baseline(self.site, changed)

    def test_each_new_media_location_requires_upstream_and_trusted_header_shape(self):
        for path in REQUIRED_MEDIA_PATHS:
            start = self.snippet.index(f"location = {path} {{")
            end = self.snippet.index("}", start) + 1
            block = self.snippet[start:end]
            for original, replacement in (
                ("proxy_pass http://127.0.0.1:8000;", ""),
                ("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                 "proxy_set_header X-Forwarded-For $http_x_forwarded_for;"),
                ("proxy_set_header X-Request-ID $request_id;", ""),
            ):
                with self.subTest(path=path, directive=original):
                    changed = self.snippet[:start] + block.replace(original, replacement, 1) + self.snippet[end:]
                    with self.assertRaises(drift.ConfigDriftError):
                        drift.validate_baseline(self.site, changed)

    def test_telemetry_location_must_be_exact(self):
        changed = self.snippet.replace(
            "location = /media/browser-download-telemetry/ {",
            "location /media/browser-download-telemetry/ {",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, changed)

    def test_duplicate_telemetry_location_is_rejected(self):
        duplicate = """
location = /media/browser-download-telemetry/ {
    proxy_pass http://127.0.0.1:8000;
}
"""
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, self.snippet + duplicate)

    def test_missing_telemetry_location_is_rejected(self):
        changed = self.snippet.replace(
            "/media/browser-download-telemetry/",
            "/media/browser-download-telemetry-missing/",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, changed)

    def test_telemetry_proxy_headers_are_required(self):
        start = self.snippet.index("location = /media/browser-download-telemetry/ {")
        changed = self.snippet[:start] + self.snippet[start:].replace(
            "    proxy_set_header X-Request-ID $request_id;\n",
            "",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, changed)

    def test_telemetry_proxy_header_values_are_exact(self):
        start = self.snippet.index("location = /media/browser-download-telemetry/ {")
        changed = self.snippet[:start] + self.snippet[start:].replace(
            "proxy_set_header X-Request-ID $request_id;",
            "proxy_set_header X-Request-ID $http_x_request_id;",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, changed)

    def test_broad_media_deny_is_required(self):
        changed = self.site.replace("location /media/ {", "location /blocked-media/ {", 1)
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_generic_media_proxy_is_rejected(self):
        changed = self.site.replace(
            "    location /media/ {\n        return 404;\n    }",
            "    location /media/ {\n        proxy_pass http://127.0.0.1:8000;\n    }",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_additional_regex_media_location_is_rejected(self):
        rogue = """
    location ~ ^/media/rogue/ {
        proxy_pass http://127.0.0.1:8000;
    }
"""
        changed = self.site.replace(
            "    location /media/ {\n",
            rogue + "\n    location /media/ {\n",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_additional_snippet_media_location_is_rejected(self):
        rogue = """
location /media/rogue/ {
    proxy_pass http://127.0.0.1:8000;
}
"""
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, self.snippet + rogue)

    def test_provider_proxy_target_is_rejected(self):
        changed = self.site.replace(
            "proxy_pass http://127.0.0.1:8000;",
            "proxy_pass https://provider.example.test;",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_proxy_pass_with_extra_argument_is_rejected(self):
        changed = self.snippet.replace(
            "proxy_pass http://127.0.0.1:8000;",
            "proxy_pass http://127.0.0.1:8000 unexpected;",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(self.site, changed)

    def test_media_include_must_precede_broad_deny(self):
        include = "    include /etc/nginx/snippets/animeimages-v1-media.conf;\n\n"
        changed = self.site.replace(include, "", 1)
        broad = "    location /media/ {\n        return 404;\n    }\n"
        changed = changed.replace(broad, broad + "\n" + include.rstrip("\n") + "\n", 1)
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_static_alias_is_required(self):
        changed = self.site.replace(
            "alias /srv/animeimages/staticfiles/;",
            "alias /tmp/other-static/;",
            1,
        )
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_application_root_proxy_is_required_for_health_and_service_worker(self):
        changed = self.site.replace("    location / {", "    location /app/ {", 1)
        with self.assertRaises(drift.ConfigDriftError):
            drift.validate_baseline(changed, self.snippet)

    def test_malformed_baseline_is_invalid_input(self):
        with self.assertRaises(drift.ConfigInputError):
            drift.parse_nginx("server { location / { return 200; }")

    def test_site_template_renders_valid_ip(self):
        rendered = drift.render_site_template(self.template, "198.51.100.20")
        self.assertNotIn(drift.SITE_PLACEHOLDER, rendered)
        self.assertEqual(rendered.count("198.51.100.20"), 2)

    def test_site_template_rejects_invalid_ip(self):
        with self.assertRaises(drift.ConfigInputError):
            drift.render_site_template(self.template, "not-an-ip")

    def test_exact_comparison_detects_hash_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            live_path = Path(directory) / "live.conf"
            canonical = self.snippet.encode()
            live_path.write_bytes(canonical)
            drift.compare_exact(canonical, live_path, "test")
            live_path.write_bytes(canonical + b"\n# drift\n")
            with self.assertRaises(drift.ConfigDriftError):
                drift.compare_exact(canonical, live_path, "test")

    def test_effective_dump_sections_are_validated(self):
        effective = (
            "# configuration file /etc/nginx/sites-enabled/animeimages:\n"
            + self.site
            + "\n# configuration file /etc/nginx/snippets/animeimages-v1-media.conf:\n"
            + self.snippet
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nginx-effective.txt"
            path.write_text(effective)
            drift.validate_effective_dump(path)

    def test_manifest_declares_only_required_managed_files(self):
        manifest = drift.load_manifest()
        self.assertEqual(len(manifest["managed_files"]), 2)
        self.assertTrue(all(item["required"] for item in manifest["managed_files"]))

    def test_cli_self_check_returns_zero(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(drift.run([]), 0)

    def test_cli_drift_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            live_path = Path(directory) / "live.conf"
            live_path.write_text(self.snippet + "\n# drift\n")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(drift.run(["--live-snippet", str(live_path)]), 1)

    def test_cli_unreadable_input_returns_two(self):
        missing = PROJECT_ROOT / "ops" / "nginx" / "tests" / "missing-live.conf"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(drift.run(["--live-snippet", str(missing)]), 2)


if __name__ == "__main__":
    unittest.main()
