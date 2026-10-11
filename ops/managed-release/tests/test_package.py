"""Artifact packaging uses committed objects, including when replace refs exist."""

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[3]


class PackageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "scripts").mkdir()
        (self.root / "ops/managed-release").mkdir(parents=True)
        shutil.copyfile(SOURCE / "scripts/package_managed_release.py", self.root / "scripts/package_managed_release.py")
        for path in ("ops/managed-release/install.py", "ops/managed-release/animeimages-consumer-probe",
                     "ops/managed-release/animeimages-managed-release.sudoers", "scripts/check_deploy_readability.py"):
            (self.root / path).write_text("reviewed\n")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic packaging fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.output = self.root.parent / (self.root.name + ".tar")
        self.addCleanup(lambda: self.output.unlink(missing_ok=True))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True, stderr=subprocess.DEVNULL)

    def package(self):
        return subprocess.run([sys.executable, "-I", "-B", str(self.root / "scripts/package_managed_release.py"),
                               "--output", str(self.output)], text=True, capture_output=True)

    def test_committed_bundle_and_digest_ignore_replace_objects(self):
        install = self.root / "ops/managed-release/install.py"
        install.write_text("unreviewed replacement\n")
        self.git("add", ".")
        self.git("commit", "-m", "replacement")
        replacement = self.git("rev-parse", "HEAD").strip()
        self.git("reset", "--hard", self.sha)
        self.git("replace", self.sha, replacement)
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tool=" + self.sha, result.stdout)
        self.assertIn("sha256=" + hashlib.sha256(self.output.read_bytes()).hexdigest(), result.stdout)
        with tarfile.open(self.output) as archive:
            self.assertEqual(set(archive.getnames()), {"install.py", "animeimages-consumer-probe",
                             "animeimages-managed-release.sudoers", "check_deploy_readability.py", "TOOL_SHA"})
            self.assertEqual(archive.extractfile("install.py").read(), b"reviewed\n")
            self.assertEqual(archive.extractfile("TOOL_SHA").read(), (self.sha + "\n").encode())
            for item in archive.getmembers():
                self.assertTrue(item.isfile())
                self.assertEqual((item.uid, item.gid, item.mode), (0, 0, 0o600))

    def test_dirty_checkout_refuses_before_creating_archive(self):
        (self.root / "untracked").write_text("unfinished\n")
        self.assertNotEqual(self.package().returncode, 0)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_not_overwritten(self):
        self.output.write_bytes(b"keep existing artifact")
        self.assertNotEqual(self.package().returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"keep existing artifact")


if __name__ == "__main__":
    unittest.main()
