"""Saved Common API -> fresh Django process -> pages -> real Worker contracts.

Offline synthetic package identity is explicit; these tests do not attest the
installed upstream source/pin or availability of the actual external receivers.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from django.test import SimpleTestCase


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "xhs_download/fixtures/saved_common/process_driver.py"


class SavedCommonProcessTests(SimpleTestCase):
    def test_saved_common_api_survives_restart_and_preserves_staff_admission(self):
        for backend in ("GVVideoClient", "AnyFetcherVideoClient"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(prefix="saved-common-") as directory:
                # Deliberately do not inherit deployment credentials, proxy or DB settings.
                env = {"PATH": os.defpath, "HOME": directory, "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                    "DJANGO_SETTINGS_MODULE": "djangoProject.settings", "DJANGO_ENV": "local",
                    "DJANGO_DEBUG": "True", "DJANGO_ALLOWED_HOSTS": "testserver,localhost",
                    "DJANGO_SECRET_KEY": "saved-common-offline-fixture-not-a-production-secret",
                    "DJANGO_DATABASE_URL": "sqlite:///" + str(Path(directory) / "isolated.sqlite3"),
                    "DJANGO_CACHE_URL": "", "DJANGO_CACHE_KEY_PREFIX": "saved-common-offline",
                    "DJANGO_LOG_LEVEL": "ERROR", "DJANGO_SECURE_SSL_REDIRECT": "False",
                    "DJANGO_SESSION_COOKIE_SECURE": "False", "DJANGO_CSRF_COOKIE_SECURE": "False",
                    "VIDEODL_PROVIDER_LAB_ENABLED": "True", "VIDEODL_PROVIDER_LAB_PYTHON": sys.executable,
                    "THIRD_PARTY_PROVIDER_ENABLED": "True", "UPSTREAM_PROVIDER_ORDER": "videodl_repo",
                    "YTDLP_PROVIDER_ENABLED": "False", "GALLERYDL_PROVIDER_ENABLED": "False",
                    "YTDLP_BINARY": str(Path(directory) / "unconfigured-yt-dlp"),
                    "GALLERYDL_BINARY": str(Path(directory) / "unconfigured-gallery-dl"),
                    "PARSEHUB_UPSTREAM_ENABLED": "False", "KUKUTOOL_PROVIDER_ENABLED": "False",
                    "ULTRA_HD_PROVIDER_ENABLED": "False", "YOUTUBE_MERGE_PROVIDER_ENABLED": "False"}
                results = []
                for phase in ("writer", "reader"):
                    done = subprocess.run([sys.executable, "-B", str(DRIVER), phase, backend],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
                    self.assertEqual(done.returncode, 0, f"{phase}: {done.stdout}\n{done.stderr}")
                    lines = [line.removeprefix("SAVED_COMMON_RESULT ") for line in done.stdout.splitlines()
                             if line.startswith("SAVED_COMMON_RESULT ")]
                    self.assertEqual(len(lines), 1, done.stdout)
                    results.append(json.loads(lines[0]))
                writer, reader = results
                self.assertNotEqual(writer["pid"], reader["pid"])
                self.assertEqual(writer["fixture_identity"], reader["fixture_identity"])
                self.assertTrue(writer["auth_csrf_checked"])
                self.assertTrue(reader["anonymous_denied"])
                self.assertTrue(reader["revoked_revision_rejected"])
                self.assertEqual(reader["native_parses"], 2)
                self.assertGreater(reader["home_candidates"], 0)
