import struct
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from .models import BatchParseRuntimeSetting, ProviderAccessMode


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE_NAME = "DokiTool"


class BrowserIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        BatchParseRuntimeSetting.objects.update_or_create(
            singleton_key=1,
            defaults={"access_mode": ProviderAccessMode.PUBLIC},
        )

    def assert_site_metadata(self, response, *, title, description):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"<title>{title}</title>", html=True)
        self.assertContains(response, f'<meta name="description" content="{description}">', html=True)
        self.assertContains(response, f'<meta property="og:title" content="{title}">', html=True)
        self.assertContains(response, f'<meta property="og:description" content="{description}">', html=True)
        self.assertContains(response, '<meta property="og:type" content="website">', html=True)
        self.assertContains(response, f'<meta property="og:site_name" content="{SITE_NAME}">', html=True)
        self.assertContains(response, '<meta name="theme-color" content="#6253d6">', html=True)
        self.assertContains(response, 'rel="icon" type="image/svg+xml"')
        self.assertContains(response, 'rel="icon" type="image/png" sizes="32x32"')
        self.assertContains(response, 'rel="apple-touch-icon" sizes="180x180"')
        self.assertNotContains(response, 'property="og:image"')

    def test_home_uses_product_identity_and_minimal_metadata(self):
        self.assert_site_metadata(
            self.client.get(reverse("home")),
            title=SITE_NAME,
            description="无需登录解析去水印，支持视频、图片、批量无水印保存",
        )

    def test_platform_pages_use_natural_product_titles(self):
        expected = {
            "xhs_home": "小红书高清下载 · DokiTool",
            "tiktok_home": "抖音高清下载 · DokiTool",
            "bilibili_home": "哔哩哔哩高清下载 · DokiTool",
        }

        for route_name, title in expected.items():
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f"<title>{title}</title>", html=True)
                self.assertContains(response, f'<meta property="og:title" content="{title}">', html=True)

    def test_batch_uses_product_title_and_batch_description(self):
        self.assert_site_metadata(
            self.client.get(reverse("batch_parse")),
            title="批量解析 · DokiTool",
            description="一行一个链接，逐条解析并保存高清图片与视频素材。",
        )

    def test_admin_login_uses_the_same_local_icons(self):
        response = self.client.get("/admin/login/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rel="icon" type="image/svg+xml"')
        self.assertContains(response, 'rel="icon" type="image/png" sizes="32x32"')
        self.assertContains(response, 'rel="apple-touch-icon" sizes="180x180"')
        self.assertContains(response, '<meta name="theme-color" content="#6253d6">', html=True)

    def test_favicon_root_redirects_to_the_local_png(self):
        response = self.client.get(reverse("favicon"))

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/static/brand/favicon-32.png")

    def test_icon_assets_are_local_and_have_expected_dimensions(self):
        svg = (PROJECT_ROOT / "static/brand/favicon.svg").read_text()
        self.assertIn('viewBox="0 0 64 64"', svg)
        self.assertNotIn("http://", svg.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", svg)

        expected_dimensions = {
            "favicon-32.png": (32, 32),
            "apple-touch-icon.png": (180, 180),
        }
        for filename, dimensions in expected_dimensions.items():
            with self.subTest(filename=filename):
                content = (PROJECT_ROOT / "static/brand" / filename).read_bytes()
                self.assertEqual(content[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", content[16:24]), dimensions)
