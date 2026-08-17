from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import UserFeedback, UserFeedbackStatus
from .services.rate_limits import LimitResult
from .services.user_feedback import (
    FEEDBACK_MAX_CONTACT_LENGTH,
    FEEDBACK_MAX_IMAGE_BYTES,
    FEEDBACK_MAX_MESSAGE_LENGTH,
    FEEDBACK_REDACTION,
    FEEDBACK_URL_REDACTION,
    build_user_feedback_values,
    sanitize_feedback_contact,
    sanitize_feedback_message,
    sanitize_feedback_page_path,
    validate_feedback_image,
)


def raster_upload(*, name="problem.png", image_format="PNG", content_type=None):
    payload = BytesIO()
    Image.new("RGB", (8, 6), color=(32, 125, 196)).save(
        payload,
        format=image_format,
    )
    image_bytes = payload.getvalue()
    mime_type = content_type or {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }[image_format]
    return SimpleUploadedFile(name, image_bytes, content_type=mime_type), image_bytes


class UserFeedbackSanitizationTests(SimpleTestCase):
    def test_page_path_always_drops_query_and_fragment(self):
        self.assertEqual(
            sanitize_feedback_page_path(
                "https://download.example/batch/?xsec_token=secret#result"
            ),
            "/batch/",
        )

    def test_message_hides_urls_and_explicit_credentials(self):
        message = sanitize_feedback_message(
            "链接 https://cdn.example/video.mp4?token=secret\n"
            "备用 cdn.example/video.mp4?signature=another-secret\n"
            "Authorization: Bearer private-token-value\n"
            "Cookie=session=private-cookie"
        )

        self.assertIn(FEEDBACK_URL_REDACTION, message)
        self.assertIn(FEEDBACK_REDACTION, message)
        self.assertNotIn("cdn.example", message)
        self.assertNotIn("another-secret", message)
        self.assertNotIn("private-token-value", message)
        self.assertNotIn("private-cookie", message)

    def test_sanitized_message_remains_within_storage_limit(self):
        message = sanitize_feedback_message("//a " * FEEDBACK_MAX_MESSAGE_LENGTH)

        self.assertLessEqual(len(message), FEEDBACK_MAX_MESSAGE_LENGTH)

    def test_contact_is_optional_single_line_text(self):
        self.assertEqual(
            sanitize_feedback_contact("  example@test.invalid\n  138 0000 0000  "),
            "example@test.invalid 138 0000 0000",
        )
        self.assertEqual(sanitize_feedback_contact(""), "")
        with self.assertRaisesMessage(ValueError, "联系方式不能超过"):
            sanitize_feedback_contact("x" * (FEEDBACK_MAX_CONTACT_LENGTH + 1))

    def test_builder_uses_server_request_id_and_bounds_metadata(self):
        values = build_user_feedback_values(
            {
                "message": "按钮在手机上看不清楚",
                "contact": "creator@example.test",
                "page_path": "/xhs/results/?q=private",
                "request_id": "client-must-not-win",
                "source_platform": "xhs",
                "viewport_width": "390",
                "viewport_height": "99999",
                "ip": "203.0.113.2",
                "user_agent": "full fingerprint",
            },
            request_id="server-request-id-1234",
        )

        self.assertEqual(values["page_path"], "/xhs/results/")
        self.assertEqual(values["request_id"], "server-request-id-1234")
        self.assertEqual(values["contact"], "creator@example.test")
        self.assertEqual(values["viewport_width"], 390)
        self.assertIsNone(values["viewport_height"])
        self.assertIsNone(values["image"])
        self.assertNotIn("ip", values)
        self.assertNotIn("user_agent", values)
        self.assertNotIn("category", values)

    def test_valid_image_is_identified_by_content_and_normalized(self):
        upload, _image_bytes = raster_upload(
            name="misleading.txt",
            image_format="WEBP",
            content_type="application/octet-stream",
        )

        validated = validate_feedback_image(upload)

        self.assertEqual(validated.name, "feedback-image.webp")
        self.assertEqual(validated.content_type, "image/webp")
        self.assertEqual(validated.tell(), 0)

    def test_non_image_upload_is_rejected(self):
        upload = SimpleUploadedFile(
            "problem.png",
            b"<html>not an image</html>",
            content_type="image/png",
        )

        with self.assertRaisesMessage(ValueError, "仅支持有效的 JPG、PNG 或 WebP"):
            validate_feedback_image(upload)


class UserFeedbackEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media_dir = TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

    def test_public_pages_render_description_image_and_contact_fields(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-bs-target="#userFeedbackModal"')
        self.assertContains(response, 'action="/feedback/"')
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'name="message"')
        self.assertContains(response, 'name="image"')
        self.assertContains(response, 'name="contact"')
        self.assertContains(response, "问题截图（可选）")
        self.assertContains(response, "选择图片或拖到这里")
        self.assertContains(response, "data-feedback-image-dropzone")
        self.assertContains(response, "data-feedback-image-preview-img")
        self.assertContains(response, "data-feedback-image-replace")
        self.assertContains(response, "data-feedback-image-remove")
        self.assertContains(response, "联系方式（可选）")
        self.assertNotContains(response, 'id="user-feedback-category"')
        self.assertNotContains(response, 'name="request_id"')
        self.assertContains(response, "js/user_feedback.js")

    def test_minimum_anonymous_submit_requires_only_description(self):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": "第二条链接没有继续解析",
                "page_path": "/batch/?debug=secret",
                "source_platform": "batch",
                "viewport_width": "1440",
                "viewport_height": "900",
            },
            REMOTE_ADDR="203.0.113.8",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"ok": True, "message": "感谢反馈，我们已收到。"})
        feedback = UserFeedback.objects.get()
        self.assertEqual(feedback.page_path, "/batch/")
        self.assertEqual(len(feedback.request_id), 32)
        self.assertEqual(feedback.source_platform, "batch")
        self.assertEqual(feedback.status, UserFeedbackStatus.NEW)
        self.assertEqual(feedback.contact, "")
        self.assertFalse(feedback.image)
        self.assertEqual(feedback.viewport_width, 1440)
        self.assertEqual(feedback.viewport_height, 900)

    def test_optional_contact_and_valid_image_are_persisted(self):
        upload, image_bytes = raster_upload(name="private-original-name.png")

        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": "页面按钮发生错位",
                "contact": "creator@example.test",
                "image": upload,
            },
            HTTP_X_REQUEST_ID="feedback-request-1234",
        )

        self.assertEqual(response.status_code, 201)
        feedback = UserFeedback.objects.get()
        self.assertEqual(feedback.contact, "creator@example.test")
        self.assertEqual(feedback.request_id, "feedback-request-1234")
        self.assertTrue(feedback.image.name.startswith("user-feedback/"))
        self.assertTrue(feedback.image.name.endswith(".png"))
        self.assertNotIn("private-original-name", feedback.image.name)
        self.assertEqual(Path(feedback.image.path).read_bytes(), image_bytes)
        self.assertNotIn(feedback.image.name, response.content.decode())

    def test_invalid_image_is_rejected_without_writing_a_file(self):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": "页面显示错误",
                "image": SimpleUploadedFile(
                    "fake.png",
                    b"<svg><script>alert(1)</script></svg>",
                    content_type="image/png",
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(UserFeedback.objects.exists())
        self.assertEqual([path for path in Path(self.media_dir.name).rglob("*") if path.is_file()], [])

    def test_oversized_image_is_rejected(self):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": "图片太大",
                "image": SimpleUploadedFile(
                    "large.png",
                    b"x" * (FEEDBACK_MAX_IMAGE_BYTES + 1),
                    content_type="image/png",
                ),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "问题截图不能超过 5 MB。")
        self.assertFalse(UserFeedback.objects.exists())

    def test_endpoint_is_csrf_protected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("user_feedback_submit"),
            {"message": "需要帮助"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserFeedback.objects.exists())

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("user_feedback_submit"))

        self.assertEqual(response.status_code, 405)

    @patch(
        "xhs_download.user_feedback_views.feedback_rate_limit_result",
        return_value=LimitResult(
            allowed=False,
            current=6,
            limit=5,
            retry_after=300,
        ),
    )
    def test_rate_limit_returns_safe_429(self, _mock_limit):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {"message": "解析失败"},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "300")
        self.assertFalse(UserFeedback.objects.exists())

    def test_message_and_contact_lengths_are_bounded(self):
        for payload in (
            {"message": "x" * (FEEDBACK_MAX_MESSAGE_LENGTH + 1)},
            {
                "message": "页面异常",
                "contact": "x" * (FEEDBACK_MAX_CONTACT_LENGTH + 1),
            },
        ):
            with self.subTest(payload_fields=tuple(payload)):
                response = self.client.post(reverse("user_feedback_submit"), payload)
                self.assertEqual(response.status_code, 400)

        self.assertFalse(UserFeedback.objects.exists())

    def test_real_fixed_window_accepts_five_then_rejects_the_sixth(self):
        statuses = []
        for index in range(6):
            response = self.client.post(
                reverse("user_feedback_submit"),
                {"message": f"反馈 {index}"},
                REMOTE_ADDR="198.51.100.22",
            )
            statuses.append(response.status_code)

        self.assertEqual(statuses, [201, 201, 201, 201, 201, 429])
        self.assertEqual(UserFeedback.objects.count(), 5)

    def test_token_like_input_is_redacted_and_not_echoed(self):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": (
                    "下载失败 https://cdn.example/video.mp4?xsec_token=url-secret\n"
                    "xsec_token=form-secret\npassword=hunter2"
                ),
                "page_path": "/results/?token=page-secret",
            },
        )

        self.assertEqual(response.status_code, 201)
        feedback = UserFeedback.objects.get()
        self.assertNotIn("url-secret", feedback.message)
        self.assertNotIn("form-secret", feedback.message)
        self.assertNotIn("hunter2", feedback.message)
        self.assertNotIn("page-secret", feedback.page_path)
        self.assertNotIn("url-secret", response.content.decode())
        self.assertNotIn("form-secret", response.content.decode())

    def test_client_request_id_is_ignored_and_invalid_platform_is_discarded(self):
        response = self.client.post(
            reverse("user_feedback_submit"),
            {
                "message": "页面提示失败",
                "request_id": "client-request-id-should-not-win",
                "source_platform": "provider-internal",
            },
            HTTP_X_REQUEST_ID="server-request-id-5678",
        )

        self.assertEqual(response.status_code, 201)
        feedback = UserFeedback.objects.get()
        self.assertEqual(feedback.request_id, "server-request-id-5678")
        self.assertEqual(feedback.source_platform, "")

    def test_feedback_table_has_no_category_ip_or_user_agent_fields(self):
        field_names = {field.name for field in UserFeedback._meta.fields}

        self.assertNotIn("category", field_names)
        self.assertNotIn("ip", field_names)
        self.assertNotIn("ip_address", field_names)
        self.assertNotIn("user_agent", field_names)


class UserFeedbackAdminTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.superuser = get_user_model().objects.create_superuser(
            username="feedback-admin",
            email="feedback-admin@example.test",
            password="local-test-password",
        )
        upload, self.image_bytes = raster_upload()
        self.feedback = UserFeedback.objects.create(
            message="解析按钮点击后没有返回结果",
            contact="admin-contact@example.test",
            image=upload,
            page_path="/results/",
            request_id="request-id-admin-1",
            source_platform="xhs",
            viewport_width=1280,
            viewport_height=800,
        )
        self.client.force_login(self.superuser)

    def test_admin_list_supports_review_fields_contact_and_image_state(self):
        response = self.client.get(
            reverse("admin:xhs_download_userfeedback_changelist"),
            {"q": "admin-contact@example.test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "解析按钮点击后没有返回结果")
        self.assertContains(response, "request-id-admin-1")
        self.assertContains(response, "处理状态")
        self.assertContains(response, "留有联系方式")
        self.assertContains(response, "包含截图")

    def test_admin_can_update_status_without_editing_submission(self):
        response = self.client.post(
            reverse(
                "admin:xhs_download_userfeedback_change",
                args=(self.feedback.pk,),
            ),
            {"status": UserFeedbackStatus.REVIEWED, "_save": "保存"},
        )

        self.assertEqual(response.status_code, 302)
        self.feedback.refresh_from_db()
        self.assertEqual(self.feedback.status, UserFeedbackStatus.REVIEWED)
        self.assertEqual(self.feedback.message, "解析按钮点击后没有返回结果")
        self.assertEqual(self.feedback.contact, "admin-contact@example.test")
        self.assertEqual(self.feedback.request_id, "request-id-admin-1")

    def test_admin_image_endpoint_is_permission_protected_and_nosniff(self):
        image_url = reverse(
            "admin:xhs_download_userfeedback_image",
            args=(self.feedback.pk,),
        )

        response = self.client.get(image_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(b"".join(response.streaming_content), self.image_bytes)

        self.client.logout()
        anonymous_response = self.client.get(image_url)
        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/admin/login/", anonymous_response["Location"])
