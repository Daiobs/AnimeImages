"""Independent P11 storage/admin checks using controlled, local-only data."""

from datetime import date, timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from download_console.models import DownloadRuntimeSetting

from .models import (
    BatchParseRuntimeSetting,
    PipelineShadowDailyCount,
    ProviderHealthTestRun,
    ProviderHealthTestSetting,
    ProviderRuntimeSetting,
)


def admin_url(model, action, obj=None):
    return reverse(
        f"admin:{model._meta.app_label}_{model._meta.model_name}_{action}",
        args=(obj.pk,) if obj is not None else (),
    )


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ShadowStorageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = get_user_model().objects.create_superuser(
            username="shadow-storage-superuser", password="controlled-password"
        )
        cls.staff = get_user_model().objects.create_user(
            username="shadow-storage-staff", is_staff=True, password="controlled-password"
        )
        cls.nonstaff = get_user_model().objects.create_user(
            username="shadow-storage-nonstaff", password="controlled-password"
        )

    def setUp(self):
        super().setUp()
        for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("P11 tests must not use network"))
            guard.start()
            self.addCleanup(guard.stop)
        self.client.force_login(self.superuser)

    @staticmethod
    def grant(user, model, action):
        permission = Permission.objects.get(
            content_type__app_label=model._meta.app_label,
            content_type__model=model._meta.model_name,
            codename=f"{action}_{model._meta.model_name}",
        )
        user.user_permissions.add(permission)
        for name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            user.__dict__.pop(name, None)

    @staticmethod
    def request(user):
        request = RequestFactory().get("/admin/")
        request.user = user
        return request

    @staticmethod
    def make_run(**extra):
        return ProviderHealthTestRun.objects.create(
            request_id="controlled-shadow-run", test_key="health_ready", **extra
        )

    @staticmethod
    def make_count(**extra):
        values = {
            "date": date(2026, 10, 26),
            "platform": "controlled_platform",
            "provider": "controlled_provider",
            "difference_code": "CONTROLLED_DIFFERENCE",
            "count": 0,
        }
        values.update(extra)
        return PipelineShadowDailyCount.objects.create(**values)


class ShadowStorageModelTests(ShadowStorageTestCase):
    def test_count_zero_is_valid_and_negative_is_rejected_by_database(self):
        row = self.make_count(count=0)
        row.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(row).objects.filter(pk=row.pk).update(count=-1)
        row.refresh_from_db()
        self.assertEqual(row.count, 0)


class ShadowRecordAdminTests(ShadowStorageTestCase):
    def test_old_health_admin_keeps_baseline_delete_permissions(self):
        model_admin = admin.site._registry[ProviderHealthTestRun]
        self.grant(self.staff, ProviderHealthTestRun, "delete")
        for user in (self.staff, self.superuser):
            request = self.request(user)
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertEqual(model_admin.get_actions(request), {})
            self.assertEqual(model_admin.has_delete_permission(request), user.is_superuser)
        run = self.make_run(shadow_details={"controlled": True})
        response = self.client.post(admin_url(ProviderHealthTestRun, "delete", run), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProviderHealthTestRun.objects.filter(pk=run.pk).exists())

    def test_shadow_json_is_escaped_visible_and_cannot_be_changed(self):
        payload = "<script>alert('controlled')</script>"
        run = self.make_run(shadow_details={"difference_code": payload})
        url = admin_url(ProviderHealthTestRun, "change", run)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, payload)
        self.assertNotContains(response, 'name="shadow_details"')
        response = self.client.post(url, {"shadow_details": '{"forged": true}', "_save": "Save"})
        self.assertEqual(response.status_code, 403)
        run.refresh_from_db()
        self.assertEqual(run.shadow_details, {"difference_code": payload})

    def test_health_details_require_existing_view_permission(self):
        run = self.make_run(shadow_details={"controlled": True})
        url = admin_url(ProviderHealthTestRun, "change", run)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.grant(self.staff, ProviderHealthTestRun, "view")
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_new_count_admin_denies_all_mutations_even_for_superuser(self):
        row = self.make_count(count=3)
        model = type(row)
        model_admin = admin.site._registry[model]
        self.grant(self.staff, model, "view")
        self.grant(self.staff, model, "add")
        self.grant(self.staff, model, "change")
        self.grant(self.staff, model, "delete")
        for user in (self.staff, self.superuser):
            with self.subTest(user=user.username):
                request = self.request(user)
                self.assertTrue(model_admin.has_view_permission(request, row))
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request, row))
                self.assertFalse(model_admin.has_delete_permission(request, row))
                self.assertFalse(model_admin.actions)
                self.assertEqual(model_admin.get_actions(request), {})
        for action, obj in (("add", None), ("change", row), ("delete", row)):
            with self.subTest(endpoint=action):
                response = self.client.post(admin_url(model, action, obj), {"count": "99", "post": "yes"})
                self.assertEqual(response.status_code, 403)
        response = self.client.post(
            admin_url(model, "changelist"),
            {"action": "delete_selected", "_selected_action": str(row.pk), "index": "0"},
        )
        self.assertIn(response.status_code, (200, 302))
        row.refresh_from_db()
        self.assertEqual(row.count, 3)
        self.assertEqual(model.objects.count(), 1)

    def test_count_view_permission_displays_read_only_values(self):
        row = self.make_count(count=7)
        model = type(row)
        self.client.force_login(self.staff)
        for action, obj in (("changelist", None), ("change", row)):
            self.assertEqual(self.client.get(admin_url(model, action, obj)).status_code, 403)
        self.grant(self.staff, model, "view")
        self.assertEqual(self.client.get(admin_url(model, "changelist")).status_code, 200)
        response = self.client.get(admin_url(model, "change", row))
        self.assertContains(response, "CONTROLLED_DIFFERENCE")
        self.assertNotContains(response, 'name="count"')
        self.assertNotContains(response, 'name="_save"')


class ShadowSettingAdminTests(ShadowStorageTestCase):
    @staticmethod
    def setting_snapshot(setting):
        return ProviderHealthTestSetting.objects.values().get(pk=setting.pk)

    def test_initial_add_uses_change_permission_ignores_hidden_fields_and_is_singleton(self):
        self.client.force_login(self.staff)
        url = admin_url(ProviderHealthTestSetting, "add")
        payload = {
            "public_shadow_enabled": "on", "_save": "Save", "singleton_key": "9",
            "daily_enabled": "on", "douyin_url": "https://example.test/controlled-injected",
            "videodl_backend_id": "UnknownBackend", "daily_test_keys": '["unknown"]',
            "email_recipients": '["controlled-injected@example.test"]',
        }
        self.grant(self.staff, ProviderHealthTestSetting, "view")
        self.assertEqual(self.client.post(url, payload).status_code, 403)
        self.grant(self.staff, ProviderHealthTestSetting, "add")
        self.assertEqual(self.client.post(url, payload).status_code, 403)
        self.assertFalse(ProviderHealthTestSetting.objects.exists())

        self.staff.user_permissions.clear()
        self.grant(self.staff, ProviderHealthTestSetting, "change")
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        setting = ProviderHealthTestSetting.objects.get()
        self.assertEqual(setting.singleton_key, 1)
        self.assertTrue(setting.public_shadow_enabled)
        defaults = ProviderHealthTestSetting()
        for field in ProviderHealthTestSetting._meta.concrete_fields:
            if field.name not in {"id", "public_shadow_enabled", "updated_at"}:
                self.assertEqual(getattr(setting, field.name), getattr(defaults, field.name), field.name)

        self.assertEqual(self.client.post(url, {"_save": "Save"}).status_code, 403)
        self.assertEqual(ProviderHealthTestSetting.objects.count(), 1)
        setting.refresh_from_db()
        self.assertTrue(setting.public_shadow_enabled)

    def test_form_exposes_only_new_toggle_and_read_only_updated_at(self):
        setting = ProviderHealthTestSetting.objects.create(douyin_url="https://example.test/controlled-hidden")
        model_admin = admin.site._registry[ProviderHealthTestSetting]
        request = self.request(self.superuser)
        self.assertEqual(set(model_admin.get_fields(request, setting)), {"public_shadow_enabled", "updated_at"})
        self.assertIn("updated_at", model_admin.get_readonly_fields(request, setting))
        form_class = model_admin.get_form(request, setting)
        self.assertEqual(set(form_class.base_fields), {"public_shadow_enabled"})
        response = self.client.get(admin_url(ProviderHealthTestSetting, "change", setting))
        self.assertContains(response, 'name="public_shadow_enabled"')
        self.assertNotContains(response, 'name="updated_at"')
        self.assertNotContains(response, setting.douyin_url)

    def test_invalid_hidden_backend_does_not_block_toggle_or_weaken_model_clean(self):
        setting = ProviderHealthTestSetting.objects.create(videodl_backend_id="UnknownBackend")
        with self.assertRaises(ValidationError) as error:
            setting.full_clean()
        self.assertIn("videodl_backend_id", error.exception.message_dict)
        response = self.client.post(
            admin_url(ProviderHealthTestSetting, "change", setting),
            {"public_shadow_enabled": "on", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertTrue(setting.public_shadow_enabled)
        self.assertEqual(setting.videodl_backend_id, "UnknownBackend")
        with self.assertRaises(ValidationError):
            setting.full_clean()

    def test_injected_post_fields_cannot_change_old_configuration(self):
        setting = ProviderHealthTestSetting.objects.create(
            douyin_url="https://example.test/controlled-original",
            daily_test_keys=["health_ready"],
            email_recipients=["controlled-original@example.test"],
        )
        before = self.setting_snapshot(setting)
        response = self.client.post(
            admin_url(ProviderHealthTestSetting, "change", setting),
            {
                "public_shadow_enabled": "on", "_save": "Save", "singleton_key": "2",
                "douyin_url": "https://example.test/controlled-injected", "daily_enabled": "on",
                "daily_test_keys": '["unknown"]', "videodl_backend_id": "UnknownBackend",
                "email_recipients": '["controlled-injected@example.test"]',
                "updated_at": "2000-01-01T00:00:00Z",
            },
        )
        self.assertEqual(response.status_code, 302)
        after = self.setting_snapshot(setting)
        self.assertTrue(after["public_shadow_enabled"])
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        for name in before.keys() - {"public_shadow_enabled", "updated_at"}:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(ProviderHealthTestSetting.objects.count(), 1)

    def test_stale_admin_object_saves_only_toggle_and_timestamp(self):
        setting = ProviderHealthTestSetting.objects.create()
        stale = ProviderHealthTestSetting.objects.get(pk=setting.pk)
        ProviderHealthTestSetting.objects.filter(pk=setting.pk).update(
            daily_enabled=True, email_recipients=["controlled-concurrent@example.test"],
            douyin_url="https://example.test/controlled-concurrent", videodl_backend_id="UnknownBackend",
            updated_at=timezone.now() - timedelta(days=1),
        )
        before = self.setting_snapshot(setting)
        model_admin = admin.site._registry[ProviderHealthTestSetting]
        original_save = ProviderHealthTestSetting.save
        with patch.object(model_admin, "get_object", return_value=stale), patch.object(
            ProviderHealthTestSetting, "save", autospec=True, side_effect=original_save
        ) as save:
            response = self.client.post(
                admin_url(ProviderHealthTestSetting, "change", setting),
                {"public_shadow_enabled": "on", "_save": "Save"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(set(save.call_args.kwargs["update_fields"]), {"public_shadow_enabled", "updated_at"})
        after = self.setting_snapshot(setting)
        self.assertTrue(after["public_shadow_enabled"])
        self.assertGreater(after["updated_at"], before["updated_at"])
        for name in before.keys() - {"public_shadow_enabled", "updated_at"}:
            self.assertEqual(after[name], before[name], name)

    def test_view_permission_cannot_post_and_change_permission_can_toggle_both_ways(self):
        setting = ProviderHealthTestSetting.objects.create()
        url = admin_url(ProviderHealthTestSetting, "change", setting)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"public_shadow_enabled": "on"}).status_code, 403)
        self.grant(self.staff, ProviderHealthTestSetting, "view")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="public_shadow_enabled"')
        self.assertEqual(self.client.post(url, {"public_shadow_enabled": "on"}).status_code, 403)
        setting.refresh_from_db()
        self.assertFalse(setting.public_shadow_enabled)
        self.grant(self.staff, ProviderHealthTestSetting, "change")
        for payload, expected in (({"public_shadow_enabled": "on"}, True), ({}, False)):
            response = self.client.post(url, {"_save": "Save", **payload})
            self.assertEqual(response.status_code, 302)
            setting.refresh_from_db()
            self.assertIs(setting.public_shadow_enabled, expected)

    def test_anonymous_and_nonstaff_cannot_access_or_change_toggle(self):
        setting = ProviderHealthTestSetting.objects.create()
        url = admin_url(ProviderHealthTestSetting, "change", setting)
        self.grant(self.nonstaff, ProviderHealthTestSetting, "view")
        self.grant(self.nonstaff, ProviderHealthTestSetting, "change")
        client = Client()
        for user in (None, self.nonstaff):
            if user is not None:
                client.force_login(user)
            with self.subTest(user=user):
                for response in (client.get(url), client.post(url, {"public_shadow_enabled": "on"})):
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("/login/", response["Location"])
        setting.refresh_from_db()
        self.assertFalse(setting.public_shadow_enabled)

    def test_toggle_post_enforces_csrf(self):
        setting = ProviderHealthTestSetting.objects.create()
        url = admin_url(ProviderHealthTestSetting, "change", setting)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.superuser)
        self.assertEqual(client.post(url, {"public_shadow_enabled": "on"}).status_code, 403)
        setting.refresh_from_db()
        self.assertFalse(setting.public_shadow_enabled)
        self.assertEqual(client.get(url).status_code, 200)
        response = client.post(url, {
            "public_shadow_enabled": "on", "_save": "Save",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        })
        self.assertEqual(response.status_code, 302)
        setting.refresh_from_db()
        self.assertTrue(setting.public_shadow_enabled)

    def test_new_admin_gets_do_not_initialize_any_runtime_or_shadow_rows(self):
        count_model = PipelineShadowDailyCount
        models = (ProviderHealthTestSetting, BatchParseRuntimeSetting, ProviderRuntimeSetting,
                  DownloadRuntimeSetting, ProviderHealthTestRun, count_model)
        for model in models:
            self.assertEqual(model.objects.count(), 0, model.__name__)
        for url in (
            admin_url(ProviderHealthTestSetting, "changelist"),
            admin_url(ProviderHealthTestSetting, "add"),
            admin_url(count_model, "changelist"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url, follow=True)
                self.assertEqual(response.status_code, 200)
                for model in models:
                    self.assertEqual(model.objects.count(), 0, model.__name__)

    def test_existing_setting_get_leaves_configuration_and_batch_uninitialized(self):
        setting = ProviderHealthTestSetting.objects.create(videodl_backend_id="UnknownBackend")
        before = self.setting_snapshot(setting)
        for action, obj in (("changelist", None), ("change", setting)):
            response = self.client.get(admin_url(ProviderHealthTestSetting, action, obj), follow=True)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.setting_snapshot(setting), before)
        self.assertFalse(BatchParseRuntimeSetting.objects.exists())
