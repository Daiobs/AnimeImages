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

    def test_same_class_collection_branches_use_native_pattern_and_runtime_limit(self):
        cls = wikimedia.WikimediaArticleExtractor
        for path in ("wiki/Example", "wiki/Category:Example", "wiki/User:Example",
                     "wiki/File:", "wiki/File%3AExample.jpg", "wiki/Special:Random",
                     "wiki/Category:Example?title=File:Example.jpg"):
            with self.subTest(path=path):
                self.assertEqual(worker.suitable(
                    self.provider, cls, "https://commons.wikimedia.org/" + path),
                    re.match(cls.pattern, "https://commons.wikimedia.org/" + path) is not None)
        self.assertTrue(worker.eligible(self.provider, wikimedia.WikimediaWikiExtractor))
        consumed = []
        def items(instance):
            for index in range(1000):
                consumed.append(index)
                yield (3, "https://example.org/image.jpg", {"index": index})
        with mock.patch.object(cls, "__iter__", items), mock.patch.object(worker, "MAX_RETAINED_CANDIDATES", 2):
            result = worker.parse_gallery(cls, "https://commons.wikimedia.org/wiki/Category:Example", 1)
        self.assertEqual(consumed, [0, 1])
        self.assertEqual(result[-1][2]["upstream_scope"]["reason"], "item_limit")
        self.assertEqual(result[-1][2]["upstream_scope"]["retained_count"], 2)
        self.assertIsNone(result[-1][2]["upstream_scope"]["unexpanded_count"])

    def test_exact_matching_handle_and_version_binding(self):
        request = {"action": "match", "provider_id": self.provider,
                   "version": importlib.metadata.version("gallery-dl"), "url": self.file_url}
        with mock.patch.object(worker, "extractor_classes", return_value=[
                wikimedia.WikimediaArticleExtractor, wikimedia.WikimediaWikiExtractor]):
            result = worker.handle(request)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["extractor_id"], "WikimediaArticleExtractor")
            self.assertEqual(result[0]["version"], request["version"])
            collection = worker.handle(dict(request, url=
                "https://commons.wikimedia.org/wiki/Category:Example"))
            self.assertEqual(collection[0]["extractor_id"], "WikimediaArticleExtractor")
            with self.assertRaisesRegex(ValueError, "version_changed"):
                worker.handle(dict(request, version="not-installed"))

    def test_existing_image_admission_and_search_exclusion(self):
        self.assertTrue(worker.eligible(self.provider, imgbb.ImgbbImageExtractor))
        self.assertTrue(worker.suitable(self.provider, imgbb.ImgbbImageExtractor,
                                        "https://ibb.co/bRGKX9bm"))
        self.assertTrue(worker.eligible(self.provider, imgbb.ImgbbAlbumExtractor))
        self.assertFalse(worker.eligible(self.provider, wikiart.WikiartImageExtractor))
        def items(instance):
            yield (3, "https://example.org/first.jpg", {})
            raise ValueError("request_limit")
        with mock.patch.object(imgbb.ImgbbAlbumExtractor, "__iter__", items):
            result = worker.parse_gallery(imgbb.ImgbbAlbumExtractor,
                                          imgbb.ImgbbAlbumExtractor.example, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2]["upstream_scope"]["reason"], "request_limit")

    def test_registry_pattern_required_not_subcategory_whitelist(self):
        for subcategory in ("article", "file", "status", "search", "category"):
            cls = type("UnreviewedExtractor", (), {"category": "unreviewed",
                                                   "subcategory": subcategory})
            self.assertFalse(worker.eligible(self.provider, cls))
            cls.pattern = r"https://example.org/(?P<id>\d+)"
            self.assertTrue(worker.eligible(self.provider, cls))


if __name__ == "__main__":
    unittest.main()
