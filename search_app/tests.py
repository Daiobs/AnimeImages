from pathlib import Path
from io import StringIO
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import Client, SimpleTestCase, override_settings
from PIL import Image

from .image_library import initialize_image_library


class ImageLibraryInitializationTests(SimpleTestCase):
    def create_image(self, path, size=(40, 20), color=(255, 0, 0)):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path)

    def test_initialize_image_library_generates_square_padded_image(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "static" / "images"
            source = root / "原神" / "纳西妲" / "站" / "source.jpg"
            self.create_image(source)

            result = initialize_image_library(root)
            output = root / "原神" / "纳西妲" / "padded" / "站" / "source.jpg"

            self.assertEqual(result.scanned, 1)
            self.assertEqual(result.generated, 1)
            self.assertTrue(output.exists())

            with Image.open(output) as padded:
                self.assertEqual(padded.size, (40, 40))

    def test_initialize_image_library_skips_existing_padded_inputs(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "static" / "images"
            source = root / "单人IP" / "春日野穹" / "source.jpg"
            padded_source = root / "单人IP" / "春日野穹" / "padded" / "old.jpg"
            self.create_image(source)
            self.create_image(padded_source)

            result = initialize_image_library(root)

            self.assertEqual(result.scanned, 1)
            self.assertEqual(result.generated, 1)
            self.assertFalse((root / "单人IP" / "春日野穹" / "padded" / "padded" / "old.jpg").exists())

    def test_management_command_dry_run_does_not_write_files(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "static" / "images"
            source = root / "单人IP" / "初音未来" / "source.jpg"
            self.create_image(source)

            call_command("init_image_library", images_root=str(root), dry_run=True, stdout=StringIO())

            self.assertFalse((root / "单人IP" / "初音未来" / "padded" / "source.jpg").exists())


class SearchViewTests(SimpleTestCase):
    def create_image_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-real-image-for-template-only")

    def test_search_prefers_padded_images(self):
        with TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            padded = base_dir / "static" / "images" / "单人IP" / "春日野穹" / "padded" / "a.jpg"
            raw = base_dir / "static" / "images" / "单人IP" / "春日野穹" / "raw.jpg"
            self.create_image_file(padded)
            self.create_image_file(raw)

            with override_settings(BASE_DIR=base_dir):
                response = Client().get("/results/", {"q": "春日野穹"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/static/images/单人IP/春日野穹/padded/a.jpg")
        self.assertNotContains(response, "/static/images/单人IP/春日野穹/raw.jpg")

    def test_search_supports_folder_tabs_from_padded_subfolders(self):
        with TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            image = base_dir / "static" / "images" / "原神" / "纳西妲" / "padded" / "站" / "a.jpg"
            self.create_image_file(image)

            with override_settings(BASE_DIR=base_dir):
                response = Client().get("/results/", {"q": "纳西妲", "folder": "站"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/static/images/原神/纳西妲/padded/站/a.jpg")

    def test_path_like_query_returns_empty_results(self):
        response = Client().get("/results/", {"q": "../secret"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "没有找到相关图片。")
