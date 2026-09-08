"""Offline admission checks against installed gallery-dl extractor semantics."""

import importlib.metadata
import re
import unittest
from unittest import mock

from gallery_dl.extractor import imgbb, wikimedia, wikiart

from tools import upstream_match_worker as worker


class GalleryCompatibilityTests(unittest.TestCase):
    provider = "gallery_dl_generic"
    file_url = "https://commons.wikimedia.org/wiki/File:Example.jpg"

    def test_file_namespace_matches_native_single_title_query(self):
        cls = wikimedia.WikimediaArticleExtractor
        self.assertTrue(worker.eligible(self.provider, cls))
        for path in ("wiki/File:Example.jpg", "index.php/File:Example.jpg",
                     "wiki/file:Example.jpg", "wiki/File:Example%20image.jpg"):
            url = "https://commons.wikimedia.org/" + path
            with self.subTest(path=path):
                self.assertTrue(worker.suitable(self.provider, cls, url))
                instance = cls(re.match(cls.pattern, url))
                self.assertEqual(instance.prefix, "file")
                self.assertEqual(instance.params(), {"titles": instance.path})

    def test_same_class_collection_branches_remain_unmatched(self):
        cls = wikimedia.WikimediaArticleExtractor
        for path in ("wiki/Example", "wiki/Category:Example", "wiki/User:Example",
                     "wiki/File:", "wiki/File%3AExample.jpg", "wiki/Special:Random",
                     "wiki/Category:Example?title=File:Example.jpg"):
            with self.subTest(path=path):
                self.assertFalse(worker.suitable(
                    self.provider, cls, "https://commons.wikimedia.org/" + path))
        self.assertFalse(worker.eligible(self.provider, wikimedia.WikimediaWikiExtractor))

    def test_exact_matching_handle_and_version_binding(self):
        request = {"action": "match", "provider_id": self.provider,
                   "version": importlib.metadata.version("gallery-dl"), "url": self.file_url}
        with mock.patch.object(worker, "extractor_classes", return_value=[
                wikimedia.WikimediaArticleExtractor, wikimedia.WikimediaWikiExtractor]):
            result = worker.handle(request)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["extractor_id"], "WikimediaArticleExtractor")
            self.assertEqual(result[0]["version"], request["version"])
            self.assertEqual(worker.handle(dict(request, url=
                "https://commons.wikimedia.org/wiki/Category:Example")), [])
            with self.assertRaisesRegex(ValueError, "version_changed"):
                worker.handle(dict(request, version="not-installed"))

    def test_existing_image_admission_and_search_exclusion(self):
        self.assertTrue(worker.eligible(self.provider, imgbb.ImgbbImageExtractor))
        self.assertTrue(worker.suitable(self.provider, imgbb.ImgbbImageExtractor,
                                        "https://ibb.co/bRGKX9bm"))
        self.assertFalse(worker.eligible(self.provider, imgbb.ImgbbAlbumExtractor))
        self.assertFalse(worker.eligible(self.provider, wikiart.WikiartImageExtractor))

    def test_no_blanket_article_or_status_admission(self):
        for subcategory in ("article", "file", "status", "search", "category"):
            cls = type("UnreviewedExtractor", (), {"category": "unreviewed",
                                                   "subcategory": subcategory})
            self.assertFalse(worker.eligible(self.provider, cls))


if __name__ == "__main__":
    unittest.main()
