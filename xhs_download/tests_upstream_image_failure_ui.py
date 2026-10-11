"""Image link actions reuse existing server-rendered permission gates."""

from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class Actions(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-batch-image-link-action" in attrs:
            self.links.append((tag, attrs))


class UpstreamImageFailureUiTests(SimpleTestCase):
    def setUp(self):
        self.url = "https://media.example/image.jpg?signature=synthetic"
        self.item = {"type": "image", "url": self.url, "source_platform": "upstream",
                     "asset_id": "upstream:job:image:1", "card_preview_url": self.url,
                     "lightbox_preview_url": self.url}
        self.row = {"status": "success", "platform": "third_party", "image_items": [self.item],
                    "video_items": [], "base_filename": "synthetic"}

    def test_existing_raw_permission_is_required_even_when_download_is_allowed(self):
        for download in (True, False):
            html = render_to_string("partials/batch_result_card.html", {
                "row": self.row, "batch_download_allowed": download, "batch_raw_links_allowed": False,
            })
            self.assertEqual(Actions(html).links, [])
            self.assertNotIn("复制直链", html)
            self.assertNotIn("打开直链", html)
            self.assertIn('data-status="success"', html)

    def test_authorized_actions_are_not_duplicated_or_marked_as_downloads(self):
        html = render_to_string("partials/batch_result_card.html", {
            "row": self.row, "batch_download_allowed": True, "batch_raw_links_allowed": True,
        })
        actions = Actions(html).links
        self.assertEqual(len(actions), 2)
        button, link = actions
        self.assertEqual(button[0], "button")
        self.assertEqual(button[1]["data-batch-image-link-action"], "copy")
        self.assertEqual(link[0], "button")
        self.assertEqual(link[1]["data-batch-image-link-action"], "open")
        for _, attrs in actions:
            self.assertNotIn("href", attrs)
            self.assertNotIn("data-copy-text", attrs)
            self.assertNotIn("download", attrs)
            self.assertNotIn("data-batch-image-download", attrs)
        self.assertEqual(html.count("复制直链"), 1)
        self.assertEqual(html.count("打开直链"), 1)
        self.assertIn('data-status="success"', html)

    def test_homepage_does_not_gain_new_raw_link_actions(self):
        html = render_to_string("partials/xhs_results_region.html", {
            "image_items": [self.item], "base_filename": "synthetic",
        })
        self.assertEqual(Actions(html).links, [])
        self.assertNotIn('href="' + self.url, html)
        self.assertNotIn("复制直链", html)
        self.assertNotIn('crossorigin="anonymous"', html)
        self.assertIn('referrerpolicy="no-referrer"', html)
        self.assertIn("共解析到 1 张图片", html)

    def test_homepage_open_action_requires_existing_diagnostics_permission(self):
        for allowed in (True, False):
            html = render_to_string("partials/xhs_results_region.html", {
                "image_items": [self.item], "show_internal_diagnostics": allowed, "base_filename": "synthetic",
            })
            self.assertEqual(html.count("data-upstream-image-open"), int(allowed))
            self.assertNotIn('href="' + self.url, html)
            self.assertNotIn("data-copy-text", html)
