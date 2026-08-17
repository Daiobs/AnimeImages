from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@override_settings(
    BATCH_PARSE_IP_INTERVAL_SECONDS=0,
    BATCH_PARSE_GLOBAL_MINUTE_LIMIT=0,
    BATCH_PARSE_DAILY_IP_LIMIT=0,
)
class GlobalSettingsPlacementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_user = get_user_model().objects.create_user(
            username="settings-placement-staff",
            password="test-password",
            is_staff=True,
        )

    def test_single_page_keeps_one_header_settings_trigger(self):
        response = self.client.get(reverse("home"))
        content = response.content.decode()
        header = content[: content.index("<main>")]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(content.count('data-bs-target="#settingsModal"'), 1)
        self.assertIn('class="btn btn-outline-secondary btn-sm nav-settings-btn"', header)
        self.assertIn("设置", header)

    def test_batch_moves_its_only_settings_trigger_to_the_header(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))
        content = response.content.decode()
        header = content[: content.index("<main>")]
        page = content[content.index("<main>") :]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(content.count('data-bs-target="#batchSettingsModal"'), 1)
        self.assertIn('data-bs-target="#batchSettingsModal"', header)
        self.assertNotIn("batch-settings-toggle", page)
        self.assertIn('id="batchSettingsModal"', page)

    def test_batch_settings_controls_remain_inside_the_parse_form(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))
        content = response.content.decode()
        form = content[
            content.index('<form method="post" class="batch-form">') :
            content.index("</form>")
        ]

        self.assertIn('name="ultra_hd" value="0"', form)
        self.assertIn('name="ultra_hd"', form)
        self.assertIn('name="youtube_merge" value="0"', form)
        self.assertIn('name="youtube_merge"', form)
        self.assertIn('id="batch-save-settings-btn"', form)
        self.assertIn('id="batch-cancel-settings-btn"', form)

    def test_header_reserves_a_leading_action_slot_for_future_feedback(self):
        source = (PROJECT_ROOT / "templates/base.html").read_text()

        leading_index = source.index("{% block nav_leading_actions %}")
        settings_index = source.index("{% block nav_actions %}")
        self.assertLess(leading_index, settings_index)
        self.assertIn("site-nav-actions", source)
