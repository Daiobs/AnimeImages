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

    def assert_shared_settings_component(
        self,
        response,
        *,
        download_location_visible,
    ):
        content = response.content.decode()
        header = content[: content.index("<main>")]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(content.count('id="projectSettingsModal"'), 1)
        self.assertEqual(content.count('data-bs-target="#projectSettingsModal"'), 1)
        self.assertIn('data-bs-target="#projectSettingsModal"', header)
        self.assertIn('id="project-ultra-hd-setting"', content)
        self.assertIn('id="project-youtube-merge-setting"', content)
        if download_location_visible:
            self.assertIn('value="browser_default"', content)
            self.assertIn('value="ask_when_supported"', content)
        else:
            self.assertNotIn('name="project-download-location"', content)
            self.assertNotIn("浏览器保存位置", content)
        self.assertNotIn('id="settingsModal"', content)
        self.assertNotIn('id="batchSettingsModal"', content)

    def test_all_single_parser_pages_share_one_project_settings_component(self):
        for route_name in ("home", "xhs_home", "tiktok_home", "bilibili_home"):
            with self.subTest(route_name=route_name):
                self.assert_shared_settings_component(
                    self.client.get(reverse(route_name)),
                    download_location_visible=False,
                )

    def test_staff_single_parser_can_control_browser_save_location(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("home"))

        self.assert_shared_settings_component(
            response,
            download_location_visible=True,
        )
        self.assertContains(
            response,
            'data-download-location-setting-available="true"',
        )

    def test_public_single_parser_enforces_browser_default_location(self):
        response = self.client.get(reverse("home"))

        self.assertContains(
            response,
            'data-download-location-setting-available="false"',
        )
        source = (PROJECT_ROOT / "static/js/xhs_download.js").read_text()
        self.assertIn("downloadLocationSettingAvailable", source)
        self.assertIn(
            "downloadLocationSettingAvailable\n"
            '            && projectSettings.download.location_mode === "ask_when_supported"',
            source,
        )

    def test_batch_uses_the_same_project_settings_component(self):
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("batch_parse"))

        self.assert_shared_settings_component(
            response,
            download_location_visible=True,
        )
        content = response.content.decode()
        self.assertNotIn("batch-settings-toggle", content)
        self.assertNotIn("batch-settings-panel", content)

    def test_batch_effective_preferences_remain_hidden_form_fields(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))
        content = response.content.decode()
        form = content[
            content.index('<form method="post" class="batch-form">') :
            content.index("</form>")
        ]

        self.assertIn('id="batch-ultra-hd-request-input"', form)
        self.assertIn('name="ultra_hd"', form)
        self.assertIn('id="batch-youtube-merge-request-input"', form)
        self.assertIn('name="youtube_merge"', form)
        self.assertNotIn("project-save-settings-btn", form)
        self.assertNotIn("project-cancel-settings-btn", form)

    def test_project_settings_only_exposes_user_preferences(self):
        source = (
            PROJECT_ROOT / "templates/partials/project_settings_modal.html"
        ).read_text()

        self.assertIn("浏览器保存位置", source)
        self.assertIn("解析质量", source)
        self.assertIn("抖音 / 小红书原画与超清", source)
        self.assertIn("YouTube 最高画质合并", source)
        for forbidden in (
            "下载处理方式",
            "智能下载",
            "Server Save",
            "服务端保存路径",
            "MEDIA_ROOT",
            "save_dir",
            "Provider",
            "runtime",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_settings_commit_only_from_the_explicit_save_action(self):
        source = (PROJECT_ROOT / "static/js/project_settings.js").read_text()

        self.assertEqual(source.count("settingsApi.updateProjectSettings("), 1)
        save_listener = source.index('saveButton.addEventListener("click"')
        update_call = source.index("settingsApi.updateProjectSettings(")
        self.assertGreater(update_call, save_listener)
        self.assertIn('modal.addEventListener("show.bs.modal"', source)
        self.assertIn('modal.addEventListener("hidden.bs.modal"', source)
        self.assertIn("if (!saveCommitted)", source)
        self.assertNotIn("localStorage", source)

    def test_settings_changes_are_deferred_during_active_operations(self):
        single_source = (PROJECT_ROOT / "static/js/xhs_download.js").read_text()
        batch_source = (PROJECT_ROOT / "static/js/batch_parse.js").read_text()

        self.assertIn("pendingProjectSettings", single_source)
        self.assertIn("!activeParseController && !downloadInProgress", single_source)
        self.assertIn("applyPendingProjectSettings();", single_source)
        self.assertIn("pendingProjectSettings", batch_source)
        self.assertIn("batchOperationInProgress()", batch_source)
        self.assertIn("applyPendingProjectSettings();", batch_source)

    def test_single_parse_submit_uses_effective_project_settings_fields(self):
        source = (PROJECT_ROOT / "static/js/xhs_download.js").read_text()
        submit_start = source.index("async function handleSearchSubmit")
        submit_end = source.index("applyProjectSettings(projectSettings);", submit_start)
        submit_handler = source[submit_start:submit_end]

        self.assertIn("new FormData(searchForm)", submit_handler)
        self.assertNotIn("updateUltraHdRequestValue", source)
        self.assertNotIn("updateYoutubeMergeRequestValue", source)

    def test_shared_assets_load_before_page_specific_scripts(self):
        self.client.force_login(self.staff_user)
        content = self.client.get(reverse("batch_parse")).content.decode()

        preferences_index = content.index("js/parse_preferences.js?v=")
        settings_index = content.index("js/project_settings.js?v=")
        batch_index = content.index("js/batch_parse.js?v=")
        self.assertLess(preferences_index, settings_index)
        self.assertLess(settings_index, batch_index)

    def test_header_keeps_feedback_before_the_rightmost_settings_action(self):
        source = (PROJECT_ROOT / "templates/base.html").read_text()

        leading_index = source.index("{% block nav_leading_actions %}")
        settings_index = source.index("{% block nav_actions %}")
        self.assertLess(leading_index, settings_index)
        self.assertIn("site-nav-actions", source)
