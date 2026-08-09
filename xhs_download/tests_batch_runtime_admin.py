import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import BatchParseRuntimeSettingAdmin
from .admin_batch_runtime import (
    BATCH_RUNTIME_MUTATION_FIELDS,
    PUBLIC_EXPANSION_CONFIRM_ERROR,
    STALE_RUNTIME_SETTING_ERROR,
    batch_public_expansion_reasons,
    batch_runtime_version_token,
)
from .models import BatchParseRuntimeSetting, ProviderAccessMode


class BatchRuntimeAdminTestMixin:
    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser(
            username="batch-runtime-admin",
            password="password",
            email="batch-runtime-admin@example.com",
        )
        self.client.force_login(self.superuser)

    @staticmethod
    def create_setting(access_mode=ProviderAccessMode.STAFF, **overrides):
        defaults = {
            "access_mode": access_mode,
            "rollout_percent": 0,
            "max_links": 20,
            "staff_max_links": 20,
            "public_max_links": 3,
            "per_link_timeout_seconds": 8.0,
            "total_timeout_seconds": 60.0,
            "parallel_workers": 4,
            "ip_interval_seconds": 20,
            "global_minute_limit": 30,
            "daily_ip_limit": 100,
            "public_ultra_hd_enabled": False,
            "public_youtube_merge_enabled": False,
        }
        defaults.update(overrides)
        return BatchParseRuntimeSetting.objects.create(
            singleton_key=1,
            **defaults,
        )

    @staticmethod
    def change_url(setting):
        return reverse(
            "admin:xhs_download_batchparseruntimesetting_change",
            args=(setting.pk,),
        )

    @staticmethod
    def changelist_url():
        return reverse("admin:xhs_download_batchparseruntimesetting_changelist")

    @staticmethod
    def add_url():
        return reverse("admin:xhs_download_batchparseruntimesetting_add")

    def post_payload(
        self,
        setting,
        *,
        confirmation="0",
        version=None,
        **overrides,
    ):
        data = {}
        for field_name in BATCH_RUNTIME_MUTATION_FIELDS:
            value = overrides.get(field_name, getattr(setting, field_name))
            if isinstance(value, bool):
                if value:
                    data[field_name] = "on"
            else:
                data[field_name] = str(value)
        data["runtime_version"] = (
            batch_runtime_version_token(setting) if version is None else version
        )
        if confirmation is not None:
            data["public_expansion_confirmed"] = confirmation
        data["_save"] = "保存并生效"
        return data

    def post_change(self, setting, *, follow=False, **kwargs):
        return self.client.post(
            self.change_url(setting),
            self.post_payload(setting, **kwargs),
            follow=follow,
        )


class BatchRuntimeSingletonAdminTests(BatchRuntimeAdminTestMixin, TestCase):
    def test_changelist_redirects_to_the_singleton_change_form(self):
        setting = self.create_setting()

        response = self.client.get(self.changelist_url())

        self.assertRedirects(
            response,
            self.change_url(setting),
            fetch_redirect_response=False,
        )

    def test_changelist_response_never_renders_an_action_selector(self):
        self.create_setting()

        response = self.client.get(self.changelist_url(), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '_selected_action')

    def test_admin_actions_are_disabled(self):
        model_admin = BatchParseRuntimeSettingAdmin(
            BatchParseRuntimeSetting,
            AdminSite(),
        )
        request = RequestFactory().get(self.changelist_url())
        request.user = self.superuser

        self.assertIsNone(model_admin.actions)
        self.assertEqual(model_admin.get_actions(request), {})

    def test_delete_permission_is_denied(self):
        setting = self.create_setting()
        model_admin = admin.site._registry[BatchParseRuntimeSetting]
        request = RequestFactory().get("/")
        request.user = self.superuser

        self.assertFalse(model_admin.has_delete_permission(request, setting))

    def test_delete_endpoint_is_denied(self):
        setting = self.create_setting()
        delete_url = reverse(
            "admin:xhs_download_batchparseruntimesetting_delete",
            args=(setting.pk,),
        )

        response = self.client.get(delete_url)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(BatchParseRuntimeSetting.objects.filter(pk=setting.pk).exists())

    def test_add_is_denied_when_singleton_exists(self):
        self.create_setting()
        model_admin = admin.site._registry[BatchParseRuntimeSetting]
        request = RequestFactory().get(self.add_url())
        request.user = self.superuser

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertEqual(self.client.get(self.add_url()).status_code, 403)

    def test_empty_changelist_redirects_to_add_without_creating_a_row(self):
        response = self.client.get(self.changelist_url())

        self.assertRedirects(
            response,
            self.add_url(),
            fetch_redirect_response=False,
        )
        self.assertEqual(BatchParseRuntimeSetting.objects.count(), 0)

    def test_add_form_get_does_not_trigger_runtime_get_or_create(self):
        response = self.client.get(self.add_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(BatchParseRuntimeSetting.objects.count(), 0)
        self.assertContains(response, "尚未创建持久化配置")

    def test_non_risky_add_creates_the_singleton_and_redirects_to_change(self):
        unsaved = BatchParseRuntimeSetting()

        response = self.client.post(
            self.add_url(),
            self.post_payload(unsaved, version=""),
        )

        setting = BatchParseRuntimeSetting.objects.get(singleton_key=1)
        self.assertRedirects(
            response,
            self.change_url(setting),
            fetch_redirect_response=False,
        )

    @patch("xhs_download.admin_batch_runtime.logger.info")
    def test_initial_creation_audits_all_runtime_fields(self, info_mock):
        unsaved = BatchParseRuntimeSetting()

        self.client.post(
            self.add_url(),
            self.post_payload(unsaved, version=""),
        )

        payload = json.loads(info_mock.call_args.args[1])
        self.assertEqual(payload["changed_fields"], list(BATCH_RUNTIME_MUTATION_FIELDS))


class BatchRuntimeEffectiveStateTests(BatchRuntimeAdminTestMixin, TestCase):
    def get_change(self, mode, **overrides):
        setting = self.create_setting(mode, **overrides)
        return setting, self.client.get(self.change_url(setting))

    def test_public_state_shows_saved_effective_database_values(self):
        setting, response = self.get_change(ProviderAccessMode.PUBLIC)

        self.assertContains(response, "当前生效状态")
        self.assertContains(response, "全站开放")
        self.assertContains(response, "<code>public</code>", html=True)
        self.assertContains(response, "Database")
        self.assertContains(response, '<span>匿名访客</span>\n                    <strong>允许</strong>', html=True)
        self.assertContains(response, '<span>Staff</span>\n                    <strong>允许</strong>', html=True)
        self.assertContains(
            response,
            timezone.localtime(setting.updated_at).strftime("%Y-%m-%d %H:%M:%S"),
        )

    def test_staff_state_shows_anonymous_denied_and_staff_allowed(self):
        _setting, response = self.get_change(ProviderAccessMode.STAFF)

        self.assertContains(response, "仅管理员")
        self.assertContains(response, '<code>staff</code>', html=True)
        self.assertContains(response, "拒绝")
        self.assertContains(response, "允许")

    def test_public_gray_state_shows_rollout_percentage(self):
        _setting, response = self.get_change(
            ProviderAccessMode.PUBLIC_GRAY,
            rollout_percent=37,
        )

        self.assertContains(response, "公开灰度")
        self.assertContains(response, '<code>public_gray</code>', html=True)
        self.assertContains(response, "按 37% 灰度")
        self.assertContains(response, "37%")

    def test_disabled_state_shows_anonymous_and_staff_denied(self):
        _setting, response = self.get_change(ProviderAccessMode.DISABLED)

        self.assertContains(response, "关闭")
        self.assertContains(response, '<code>disabled</code>', html=True)
        self.assertContains(response, "拒绝", count=2)

    def test_invalid_risky_post_keeps_saved_state_separate_from_form_draft(self):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        response = self.post_change(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-saved-access-mode="staff"')
        self.assertContains(response, 'data-server-unpersisted="true"')
        self.assertContains(response, 'value="public" selected')
        setting.refresh_from_db()
        self.assertEqual(setting.access_mode, ProviderAccessMode.STAFF)

    def test_successful_save_refreshes_effective_state_and_success_message(self):
        setting = self.create_setting(max_links=20)

        response = self.post_change(setting, max_links=18, follow=True)

        self.assertContains(response, "配置已保存并生效")
        self.assertContains(response, 'data-saved-access-mode="staff"')
        setting.refresh_from_db()
        self.assertEqual(setting.max_links, 18)

    def test_change_form_uses_save_and_effective_primary_action(self):
        _setting, response = self.get_change(ProviderAccessMode.STAFF)

        self.assertContains(response, 'value="保存并生效"')
        self.assertContains(response, "有未保存的更改")
        self.assertContains(response, "batch_runtime_admin.js")


class BatchRuntimePublicExpansionTests(BatchRuntimeAdminTestMixin, TestCase):
    def assert_rejected(self, setting, **overrides):
        original = {
            field_name: getattr(setting, field_name)
            for field_name in BATCH_RUNTIME_MUTATION_FIELDS
        }
        response = self.post_change(setting, **overrides)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, PUBLIC_EXPANSION_CONFIRM_ERROR)
        setting.refresh_from_db()
        for field_name, value in original.items():
            self.assertEqual(getattr(setting, field_name), value)
        return response

    def test_ordinary_limit_change_saves_without_confirmation(self):
        setting = self.create_setting(max_links=20)

        response = self.post_change(setting, max_links=18)

        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.max_links, 18)

    def test_staff_to_public_requires_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        self.assert_rejected(setting, access_mode=ProviderAccessMode.PUBLIC)

    def test_staff_to_public_saves_with_exact_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        response = self.post_change(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
            confirmation="1",
        )

        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.access_mode, ProviderAccessMode.PUBLIC)

    def test_disabled_to_public_gray_requires_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.DISABLED)

        self.assert_rejected(
            setting,
            access_mode=ProviderAccessMode.PUBLIC_GRAY,
            rollout_percent=10,
        )

    def test_disabled_to_public_requires_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.DISABLED)

        self.assert_rejected(setting, access_mode=ProviderAccessMode.PUBLIC)

    def test_public_gray_to_public_requires_confirmation(self):
        setting = self.create_setting(
            ProviderAccessMode.PUBLIC_GRAY,
            rollout_percent=25,
        )

        self.assert_rejected(setting, access_mode=ProviderAccessMode.PUBLIC)

    def test_public_gray_rollout_increase_requires_confirmation(self):
        setting = self.create_setting(
            ProviderAccessMode.PUBLIC_GRAY,
            rollout_percent=25,
        )

        self.assert_rejected(setting, rollout_percent=100)

    def test_public_gray_rollout_decrease_saves_without_confirmation(self):
        setting = self.create_setting(
            ProviderAccessMode.PUBLIC_GRAY,
            rollout_percent=100,
        )

        response = self.post_change(setting, rollout_percent=20)

        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.rollout_percent, 20)

    def test_public_to_staff_saves_without_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.PUBLIC)

        response = self.post_change(setting, access_mode=ProviderAccessMode.STAFF)

        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.access_mode, ProviderAccessMode.STAFF)

    def test_public_to_disabled_saves_without_confirmation(self):
        setting = self.create_setting(ProviderAccessMode.PUBLIC)

        response = self.post_change(
            setting,
            access_mode=ProviderAccessMode.DISABLED,
        )

        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertEqual(setting.access_mode, ProviderAccessMode.DISABLED)

    def test_enabling_public_ultra_hd_requires_confirmation(self):
        setting = self.create_setting(public_ultra_hd_enabled=False)

        self.assert_rejected(setting, public_ultra_hd_enabled=True)

    def test_enabling_public_youtube_merge_requires_confirmation(self):
        setting = self.create_setting(public_youtube_merge_enabled=False)

        self.assert_rejected(setting, public_youtube_merge_enabled=True)

    def test_non_exact_confirmation_marker_is_rejected(self):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        self.assert_rejected(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
            confirmation="true",
        )

    def test_missing_confirmation_marker_is_rejected(self):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        self.assert_rejected(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
            confirmation=None,
        )

    def test_public_expansion_reason_set_is_limited_to_fixed_server_values(self):
        reasons = batch_public_expansion_reasons(
            {
                "access_mode": ProviderAccessMode.STAFF,
                "rollout_percent": 0,
                "public_ultra_hd_enabled": False,
                "public_youtube_merge_enabled": False,
            },
            {
                "access_mode": ProviderAccessMode.PUBLIC,
                "rollout_percent": 100,
                "public_ultra_hd_enabled": True,
                "public_youtube_merge_enabled": True,
            },
        )

        self.assertEqual(
            reasons,
            [
                "access_mode_public_expansion",
                "public_ultra_hd_enabled",
                "public_youtube_merge_enabled",
            ],
        )


class BatchRuntimeConcurrencyAndAuditTests(BatchRuntimeAdminTestMixin, TestCase):
    def test_stale_form_is_rejected(self):
        setting = self.create_setting(max_links=20)
        stale_version = batch_runtime_version_token(setting)
        BatchParseRuntimeSetting.objects.filter(pk=setting.pk).update(
            updated_at=timezone.now() + timedelta(seconds=1),
        )

        response = self.post_change(
            setting,
            version=stale_version,
            max_links=18,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_RUNTIME_SETTING_ERROR)

    def test_stale_form_does_not_overwrite_newer_database_values(self):
        setting = self.create_setting(max_links=20)
        stale_version = batch_runtime_version_token(setting)
        BatchParseRuntimeSetting.objects.filter(pk=setting.pk).update(
            max_links=12,
            access_mode=ProviderAccessMode.DISABLED,
            updated_at=timezone.now() + timedelta(seconds=1),
        )

        self.post_change(
            setting,
            version=stale_version,
            max_links=18,
            access_mode=ProviderAccessMode.STAFF,
        )

        setting.refresh_from_db()
        self.assertEqual(setting.max_links, 12)
        self.assertEqual(setting.access_mode, ProviderAccessMode.DISABLED)

    @patch("xhs_download.admin_batch_runtime.logger.info")
    def test_successful_save_emits_structured_mutation_audit(self, info_mock):
        setting = self.create_setting(max_links=20)

        response = self.post_change(setting, max_links=18)

        self.assertEqual(response.status_code, 302)
        info_mock.assert_called_once()
        payload = json.loads(info_mock.call_args.args[1])
        self.assertEqual(payload["event"], "batch_runtime_admin_mutation")
        self.assertEqual(payload["actor_username"], self.superuser.username)
        self.assertTrue(payload["actor_is_superuser"])
        self.assertEqual(payload["changed_fields"], ["max_links"])
        self.assertEqual(payload["previous_access_mode"], ProviderAccessMode.STAFF)
        self.assertEqual(payload["new_access_mode"], ProviderAccessMode.STAFF)
        self.assertFalse(payload["public_expansion"])

    @patch("xhs_download.admin_batch_runtime.logger.info")
    def test_rejected_save_does_not_emit_success_audit(self, info_mock):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        response = self.post_change(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
        )

        self.assertEqual(response.status_code, 200)
        info_mock.assert_not_called()

    @patch("xhs_download.admin_batch_runtime.logger.info")
    def test_audit_payload_contains_only_allowlisted_non_sensitive_keys(self, info_mock):
        setting = self.create_setting(max_links=20)

        self.post_change(setting, max_links=19)

        payload = json.loads(info_mock.call_args.args[1])
        self.assertEqual(
            set(payload),
            {
                "event",
                "actor_username",
                "actor_is_superuser",
                "request_id",
                "changed_fields",
                "previous_access_mode",
                "new_access_mode",
                "previous_rollout_percent",
                "new_rollout_percent",
                "public_expansion",
            },
        )
        serialized = json.dumps(payload).lower()
        for forbidden in (
            "password",
            "cookie",
            "csrf",
            "sessionid",
            "signed_url",
            ".env",
        ):
            self.assertNotIn(forbidden, serialized)

    @patch("xhs_download.admin_batch_runtime.logger.info")
    def test_audit_changed_fields_excludes_confirmation_and_version(self, info_mock):
        setting = self.create_setting(ProviderAccessMode.STAFF)

        self.post_change(
            setting,
            access_mode=ProviderAccessMode.PUBLIC,
            confirmation="1",
        )

        payload = json.loads(info_mock.call_args.args[1])
        self.assertEqual(payload["changed_fields"], ["access_mode"])
        self.assertNotIn("runtime_version", payload["changed_fields"])
        self.assertNotIn("public_expansion_confirmed", payload["changed_fields"])
        self.assertTrue(payload["public_expansion"])
