"""Small regressions for confirmed frontend audit findings; no network or database."""
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from djangoProject import context_processors

ROOT = Path(__file__).resolve().parents[1]
# Official, version-specific values: twbs/bootstrap v5.3.3, hugo.yml params.cdn.
BOOTSTRAP_SRI = {
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css":
        "sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js":
        "sha384-YvpcrYf0tY3lHB60NNkmXc5s9fDVZLESaAA55NDzOxhy9GkcIdslK1eN7N6jIeHz",
}


class AssetTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        url = attrs.get("src" if tag == "script" else "href")
        if tag in {"script", "link"} and url in BOOTSTRAP_SRI:
            self.assets[url] = attrs


class FrontendAuditFixTests(SimpleTestCase):
    def test_bootstrap_css_and_bundle_pin_official_integrity_and_anonymous_cors(self):
        parser = AssetTags()
        parser.feed((ROOT / "templates/base.html").read_text(encoding="utf-8"))
        self.assertEqual(set(parser.assets), set(BOOTSTRAP_SRI))
        for url, integrity in BOOTSTRAP_SRI.items():
            with self.subTest(url=url):
                self.assertEqual(parser.assets[url].get("integrity"), integrity)
                self.assertEqual(parser.assets[url].get("crossorigin"), "anonymous")

    def test_first_party_script_and_style_inventory_has_no_manual_omissions(self):
        expected = {
            path.relative_to(ROOT).as_posix()
            for directory, pattern in (("static/js", "*.js"), ("static/css", "*.css"))
            for path in (ROOT / directory).rglob(pattern)
            if path.is_file()
        }
        self.assertTrue(expected)
        self.assertTrue(expected.issubset(set(context_processors.STATIC_VERSION_FILES)))
        self.assertEqual(len(context_processors.STATIC_VERSION_FILES),
                         len(set(context_processors.STATIC_VERSION_FILES)))

    def test_first_party_template_asset_references_participate_in_versioning(self):
        pattern = re.compile(r"\{%\s*static\s+['\"]((?:js|css)/[^'\"]+\.(?:js|css))['\"]\s*%\}")
        references = set()
        for template in (ROOT / "templates").rglob("*.html"):
            references.update("static/" + path for path in pattern.findall(template.read_text(encoding="utf-8")))
        self.assertTrue(references)
        self.assertFalse(references - set(context_processors.STATIC_VERSION_FILES))

    def test_previously_omitted_scripts_change_the_version_when_updated(self):
        for relative in ("static/js/parse_preferences.js", "static/js/project_settings.js",
                         "static/js/download_reliability.js"):
            with self.subTest(path=relative), TemporaryDirectory() as directory:
                root = Path(directory)
                asset = root / relative
                asset.parent.mkdir(parents=True)
                asset.write_text("first", encoding="utf-8")
                os.utime(asset, (100, 100))
                with override_settings(BASE_DIR=root):
                    before = context_processors.source_static_asset_version()
                    asset.write_text("second", encoding="utf-8")
                    os.utime(asset, (200, 200))
                    after = context_processors.source_static_asset_version()
                self.assertEqual(before, "100")
                self.assertEqual(after, "200")

    def test_css_only_update_and_repeated_reads_keep_existing_version_semantics(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "static/css/app.css"
            asset.parent.mkdir(parents=True)
            asset.write_text("body {}", encoding="utf-8")
            os.utime(asset, (100, 100))
            with override_settings(BASE_DIR=root):
                self.assertEqual(context_processors.source_static_asset_version(), "100")
                self.assertEqual(context_processors.source_static_asset_version(), "100")
                os.utime(asset, (200, 200))
                self.assertEqual(context_processors.source_static_asset_version(), "200")

    def test_environment_release_prefix_is_preserved(self):
        with patch.dict(os.environ, {"APP_STATIC_VERSION": "release-review"}), \
                patch.object(context_processors, "source_static_asset_version", return_value="200"):
            self.assertEqual(context_processors.get_static_asset_version(), "release-review-200")

    def test_missing_source_files_still_allow_explicit_release_version(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.dict(os.environ, {"APP_STATIC_VERSION": "release-review"}):
            self.assertEqual(context_processors.source_static_asset_version(), "")
            self.assertEqual(context_processors.get_static_asset_version(), "release-review")
