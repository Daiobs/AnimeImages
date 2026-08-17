import json

from django.apps import apps
from django.contrib.auth import SESSION_KEY, get_user_model
from django.db import connection
from django.test import Client, TestCase

from .models import ProviderAdminAuditLog, ReleaseDeployJob, SelfHealingEvent


class ProductionPolishAdminShellTests(TestCase):
    password = "local-test-password"

    def setUp(self):
        user_model = get_user_model()
        self.superuser = user_model.objects.create_superuser(
            username="local-polish-root",
            password=self.password,
        )
        self.staff = user_model.objects.create_user(
            username="local-polish-staff",
            password=self.password,
            is_staff=True,
        )

    def test_anonymous_admin_login_uses_minimal_shell(self):
        response = self.client.get("/admin/login/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "星空的高清下载小站")
        self.assertContains(response, "Admin Console")
        self.assertContains(response, "登录管理后台")
        self.assertContains(response, '<label for="id_username">用户名</label>')
        self.assertContains(response, '<label for="id_password">密码</label>')
        self.assertContains(response, 'value="登录"')
        self.assertContains(response, 'id="login-form"')
        self.assertContains(response, "返回前台")
        self.assertNotContains(response, 'class="admin-sidebar"')
        self.assertNotContains(response, 'id="user-tools"')
        self.assertNotContains(response, "Provider 管理")
        self.assertNotContains(response, "健康状态")
        self.assertNotContains(response, "退出")

    def test_django_admin_login_session_and_logout_still_work(self):
        login = self.client.post(
            "/admin/login/?next=/admin/",
            {
                "username": self.superuser.username,
                "password": self.password,
                "next": "/admin/",
            },
            follow=True,
        )

        self.assertEqual(login.status_code, 200)
        self.assertEqual(str(self.client.session[SESSION_KEY]), str(self.superuser.pk))
        self.assertContains(login, 'class="admin-sidebar"')
        self.assertContains(login, "Provider 管理")

        logout = self.client.post("/admin/logout/", follow=True)

        self.assertEqual(logout.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertNotContains(logout, 'class="admin-sidebar"')

    def test_staff_provider_settings_page_is_read_only_without_change_permission(self):
        self.client.force_login(self.staff)

        response = self.client.get("/admin/provider-settings/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "没有修改运行配置的权限")
        self.assertContains(response, 'class="provider-form-fields" disabled')
        self.assertNotContains(response, "保存 Provider 设置")

    def test_unprivileged_staff_cannot_call_sensitive_custom_admin_posts(self):
        self.client.force_login(self.staff)
        form_posts = (
            ("/admin/provider-settings/", {"douyin_ultra_mode": "public"}),
            ("/admin/policy-engine/", {"v35_mode": "public"}),
            ("/admin/policy-control/", {"action": "generate"}),
            ("/admin/release-management/", {"action": "deploy_main"}),
            ("/admin/self-healing/run/", {"provider": "kukutool"}),
            ("/admin/provider-health-tests/", {"test_key": "health_ready"}),
        )

        for path, data in form_posts:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data).status_code, 404)

        control = self.client.post(
            "/admin/provider-health/control/",
            data=json.dumps({"action": "disable"}),
            content_type="application/json",
        )
        self.assertEqual(control.status_code, 404)
        self.assertFalse(ProviderAdminAuditLog.objects.exists())
        self.assertFalse(ReleaseDeployJob.objects.exists())
        self.assertFalse(SelfHealingEvent.objects.exists())

    def test_legacy_submission_routes_are_removed_but_model_history_remains(self):
        for path in (
            "/submissions/submit/",
            "/submissions/list",
            "/submissions/detail/1/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

        self.assertTrue(apps.is_installed("submissions"))
        self.assertIn("submissions_submission", connection.introspection.table_names())
