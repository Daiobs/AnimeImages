"""Real pinned types run only in a child interpreter, never in Django.

CI must bootstrap a separate runtime before running this module:
  python tools/parsehub_provider/bootstrap_parsehub_provider.py /tmp/parsehub-ci
Set PARSEHUB_COMPAT_PYTHON=/tmp/parsehub-ci/bin/python and
PYTHONDONTWRITEBYTECODE=1. The bootstrap requires a Python version supported by
the pinned package and its native dependencies. Missing runtimes fail, not skip.
Local runs must also set PARSEHUB_COMPAT_PYTHON explicitly; there is no fallback.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstalledParseHubCompatibilityTests(unittest.TestCase):
    def test_real_types_in_isolated_runtime(self):
        python = os.environ.get("PARSEHUB_COMPAT_PYTHON")
        self.assertTrue(python, "Set PARSEHUB_COMPAT_PYTHON to the isolated ParseHub interpreter")
        self.assertTrue(Path(python).is_file(), "Bootstrap the isolated ParseHub CI runtime")
        before = {name for name in sys.modules if name == "parsehub" or name.startswith("parsehub.")}
        completed = subprocess.run(
            [python, "-I", "-B", str(Path(__file__).resolve()), "--isolated"],
            cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Ran 8 tests", completed.stderr)
        self.assertEqual(before, {
            name for name in sys.modules if name == "parsehub" or name.startswith("parsehub.")
        })


def isolated_suite():
    sys.path.insert(0, str(ROOT))
    from tools import upstream_parsehub_worker as worker

    worker.verify_install()
    from parsehub.types.media_ref import AniRef, ImageRef, LivePhotoRef, MediaRef, VideoRef
    from parsehub.types.result import (
        ImageParseResult, MultimediaParseResult, ParseResult,
        RichTextParseResult, VideoParseResult,
    )

    source = "https://tieba.baidu.com/p/123"
    image_url = "https://media.example/image.jpg"
    video_url = "https://media.example/video.mp4"

    class RealTypes(unittest.TestCase):
        def normalize(self, value):
            value.raw_url = source
            return worker.normalize_result(value, source)

        def test_result_classes_and_legitimate_subclasses(self):
            cases = [
                (ImageParseResult, {"photo": [ImageRef(url=image_url)]}),
                (VideoParseResult, {"video": VideoRef(url=video_url)}),
                (MultimediaParseResult, {"media": [ImageRef(url=image_url)]}),
                (RichTextParseResult, {"media": [ImageRef(url=image_url)]}),
            ]
            for base, kwargs in cases:
                for cls in (base, type("Specialized" + base.__name__, (base,), {})):
                    with self.subTest(cls=cls.__name__):
                        self.assertEqual(len(self.normalize(cls(**kwargs))), 1)

        def test_reference_subclasses_and_order(self):
            refs = [type("SpecialImage", (ImageRef,), {})(url=image_url),
                    type("SpecialVideo", (VideoRef,), {})(url=video_url),
                    type("SpecialAnimation", (AniRef,), {})(url="https://media.example/a.gif")]
            rows = self.normalize(MultimediaParseResult(media=tuple(refs)))
            self.assertEqual([row[1] for row in rows], [ref.url for ref in refs])
            self.assertEqual([row[2]["media_type"] for row in rows], ["image", "video", "image"])
            self.assertEqual(rows[1][2]["media_completeness"], "unknown")
            self.assertTrue(rows[1][2]["requires_completeness_check"])
            self.assertNotIn("acodec", rows[1][2])

        def test_animated_video_retains_other_images(self):
            for ref in [AniRef(url=video_url, ext="mp4"), AniRef(url=video_url),
                        AniRef(url="https://media.example/resource", ext=".MP4")]:
                with self.subTest(ref=ref):
                    rows = self.normalize(ImageParseResult(photo=[ImageRef(url=image_url), ref,
                                                                  ImageRef(url=image_url)]))
                    self.assertEqual([row[1] for row in rows], [image_url, ref.url, image_url])
                    self.assertEqual(rows[1][2]["mime_type"], "video/unknown")
                    self.assertEqual(rows[1][2]["extension"], "mp4")
                    self.assertTrue(rows[1][2]["requires_completeness_check"])
                    self.assertEqual(rows[0][2]["media_completeness"], "image_candidate")

        def test_richtext_explicit_media_only(self):
            markdown = "![hidden](https://media.example/hidden.jpg) <video src='https://media.example/hidden.mp4'>"
            value = RichTextParseResult(media=[ImageRef(url=image_url)], markdown_content=markdown)
            self.assertEqual([row[1] for row in self.normalize(value)], [image_url])
            with self.assertRaisesRegex(ValueError, "^no_formats$"):
                self.normalize(RichTextParseResult(markdown_content=markdown))

        def test_rejects_spoofed_result_names_and_base(self):
            for name in ("ImageParseResult", "VideoParseResult", "MultimediaParseResult", "RichTextParseResult"):
                value = type(name, (), {"media": [ImageRef(url=image_url)], "title": "fake"})()
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "^unsupported_result$"):
                    self.normalize(value)
            with self.assertRaisesRegex(ValueError, "^unsupported_result$"):
                self.normalize(ParseResult(media=ImageRef(url=image_url)))

        def test_rejects_untrusted_and_unsupported_references(self):
            refs = [MediaRef(url=image_url), LivePhotoRef(url=image_url), object(), {"url": image_url}]
            refs.extend(type(name, (), {"url": image_url, "ext": "jpg"})()
                        for name in ("ImageRef", "AniRef", "VideoRef"))
            for ref in refs:
                with self.subTest(ref=type(ref).__name__), self.assertRaisesRegex(ValueError, "^unsupported_result$"):
                    self.normalize(MultimediaParseResult(media=[ref]))

        def test_bounds_identity_and_media_validation(self):
            for value, error in [
                (ImageParseResult(), "no_formats"),
                (ImageParseResult(photo=[image_url] * 31), "item_limit"),
                (ImageParseResult(photo=["http://media.example/a.jpg"]), "insecure_media"),
                (VideoParseResult(video=VideoRef(url=image_url)), "media_type_conflict"),
                (ImageParseResult(photo=[ImageRef(url=video_url)]), "media_type_conflict"),
            ]:
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, "^" + error + "$"):
                    self.normalize(value)
            value = ImageParseResult(photo=[image_url])
            value.raw_url = source + "4"
            with self.assertRaisesRegex(ValueError, "^identity_mismatch$"):
                worker.normalize_result(value, source)

        def test_extensionless_video_remains_limited(self):
            row = self.normalize(VideoParseResult(video=VideoRef(
                url="https://media.example/resource", ext=None,
            )))[0]
            self.assertEqual(row[2]["extension"], "")
            self.assertEqual(row[2]["mime_type"], "video/unknown")
            self.assertEqual(row[2]["media_completeness"], "unknown")
            self.assertTrue(row[2]["requires_completeness_check"])

    return unittest.defaultTestLoader.loadTestsFromTestCase(RealTypes)


if __name__ == "__main__":
    if sys.argv[1:] == ["--isolated"]:
        result = unittest.TextTestRunner(verbosity=2).run(isolated_suite())
        raise SystemExit(not result.wasSuccessful())
    unittest.main()
