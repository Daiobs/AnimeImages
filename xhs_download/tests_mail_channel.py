import json
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
import requests

from .checks import check_provider_validation_email_configuration
from .models import MailChannelSetting, ProviderHealthTestSetting
from .mail_settings_views import MailSettingsForm
from .services.mail_channel import encrypt, decrypt, resolve_mail_channel, MailError, SMTP, BREVO
from .services.brevo_email import EmailBackend
from .services.provider_daily_validation import email_delivery_configuration

KEY = Fernet.generate_key().decode()
SECRET = "test-secret-never-render"


@override_settings(MAIL_CONFIG_ENCRYPTION_KEY=KEY, PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
                   DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DJANGO_DEFAULT_FROM_EMAIL="sender@example.test", PROVIDER_VALIDATION_EMAIL_RECIPIENTS=["alerts@example.test"],
                   REQUIRE_PRODUCTION_CONFIG=False)
class MailChannelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("mail-admin", "", "test-password")
        self.client.force_login(self.user)

    def data(self, **overrides):
        data = dict(action="save", revision=0, source="database", provider="smtp", from_email="sender@example.test",
                    from_name="AnimeImages", smtp_host="smtp.example.test", smtp_port=587, smtp_username="sender",
                    smtp_security="starttls", smtp_password=SECRET, email_recipients="alerts@example.test")
        data.update(overrides)
        return data

    def test_env_default_has_no_database_write(self):
        snapshot = resolve_mail_channel()
        self.assertEqual(snapshot.source, "env")
        self.assertTrue(snapshot.complete)
        self.assertEqual(MailChannelSetting.objects.count(), 0)

    def test_brevo_without_smtp_fields_preserves_smtp(self):
        self.client.post('/admin/mail-settings/', self.data())
        original = MailChannelSetting.objects.get()
        response = self.client.post('/admin/mail-settings/', dict(
            action='save', revision=1, source='database', provider='brevo',
            from_email='sender@example.test', brevo_key=SECRET,
            email_recipients='alerts@example.test'))
        self.assertEqual(response.status_code, 302)
        row = MailChannelSetting.objects.get()
        for field in ('smtp_host', 'smtp_port', 'smtp_username', 'smtp_security', 'smtp_password_encrypted'):
            self.assertEqual(getattr(row, field), getattr(original, field))
        self.assertTrue(resolve_mail_channel().complete)

    def test_env_ignores_inactive_invalid_draft_and_preserves_all_config(self):
        self.client.post('/admin/mail-settings/', self.data())
        original = MailChannelSetting.objects.get()
        response = self.client.post('/admin/mail-settings/', dict(
            action='save', revision=1, source='env', provider='invalid',
            smtp_port='invalid', from_email='invalid', email_recipients='invalid',
            clear_smtp_password='on', clear_brevo_key='on', brevo_key='bad\nkey'))
        self.assertEqual(response.status_code, 302)
        row = MailChannelSetting.objects.get()
        for field in MailSettingsForm.Meta.fields:
            if field != 'source':
                self.assertEqual(getattr(row, field), getattr(original, field))
        self.assertEqual(row.smtp_password_encrypted, original.smtp_password_encrypted)
        self.assertEqual(ProviderHealthTestSetting.objects.get().email_recipients, ['alerts@example.test'])
        self.assertFalse(ProviderHealthTestSetting.objects.get().email_notifications_enabled)
        self.assertTrue(resolve_mail_channel().env_enabled)

    def test_inactive_secret_clear_and_invalid_values_are_ignored(self):
        self.client.post('/admin/mail-settings/', self.data(provider='brevo', brevo_key=SECRET))
        old = MailChannelSetting.objects.get().brevo_key_encrypted
        response = self.client.post('/admin/mail-settings/', self.data(
            revision=1, brevo_key='bad\nkey', clear_brevo_key='on'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MailChannelSetting.objects.get().brevo_key_encrypted, old)

    def test_active_brevo_secret_error_is_not_ignored(self):
        response = self.client.post('/admin/mail-settings/', self.data(provider='brevo', brevo_key='bad\nkey'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('brevo_key', response.context['form'].errors)

    def test_brevo_ignores_invalid_smtp_and_clear(self):
        self.client.post('/admin/mail-settings/', self.data())
        original = MailChannelSetting.objects.get().smtp_password_encrypted
        response = self.client.post('/admin/mail-settings/', self.data(
            revision=1, provider='brevo', brevo_key=SECRET, smtp_port='invalid',
            smtp_host='invalid/host', smtp_security='invalid', clear_smtp_password='on'))
        self.assertEqual(response.status_code, 302)
        row = MailChannelSetting.objects.get()
        self.assertEqual(row.smtp_password_encrypted, original)
        self.assertEqual(row.smtp_host, 'smtp.example.test')
        self.assertEqual(row.smtp_port, 587)

    def test_env_minimal_post_preserves_recipients_and_both_secrets(self):
        self.client.post('/admin/mail-settings/', self.data())
        row = MailChannelSetting.objects.get()
        row.brevo_key_encrypted = encrypt(SECRET)
        row.save()
        response = self.client.post('/admin/mail-settings/', dict(action='save', revision=1, source='env'))
        self.assertEqual(response.status_code, 302)
        updated = MailChannelSetting.objects.get()
        self.assertEqual(updated.smtp_password_encrypted, row.smtp_password_encrypted)
        self.assertEqual(updated.brevo_key_encrypted, row.brevo_key_encrypted)
        self.assertEqual(ProviderHealthTestSetting.objects.get().email_recipients, ['alerts@example.test'])

    def test_incomplete_draft_cannot_test_send(self):
        self.client.post('/admin/mail-settings/', self.data(smtp_host='', smtp_password=''))
        snapshot = resolve_mail_channel()
        self.assertFalse(snapshot.complete)
        with patch('django.core.mail.EmailMessage.send') as send:
            self.client.post('/admin/mail-settings/', dict(action='test', confirm='on',
                test_recipient='test@example.test', fingerprint=snapshot.fingerprint))
            send.assert_not_called()

    def test_encryption_authenticated_and_no_secret_repr(self):
        stored = encrypt(SECRET)
        self.assertNotIn(SECRET, stored)
        self.assertEqual(decrypt(stored), SECRET)
        with override_settings(MAIL_CONFIG_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(MailError):
                decrypt(stored)
        with self.assertRaises(MailError):
            decrypt("damaged")
        self.assertNotIn("sender@example.test", repr(resolve_mail_channel()))

    def test_save_is_encrypted_and_no_automatic_send(self):
        with patch("django.core.mail.EmailMessage.send") as send:
            self.assertEqual(self.client.post("/admin/mail-settings/", self.data()).status_code, 302)
            send.assert_not_called()
        row = MailChannelSetting.objects.get()
        self.assertEqual(decrypt(row.smtp_password_encrypted), SECRET)
        self.assertNotIn(SECRET, row.smtp_password_encrypted)
        health = ProviderHealthTestSetting.objects.get()
        self.assertFalse(health.email_notifications_enabled)
        self.assertEqual(health.email_recipients, ["alerts@example.test"])
        page = self.client.get("/admin/mail-settings/")
        self.assertNotContains(page, SECRET)
        self.assertNotContains(page, row.smtp_password_encrypted)
        self.assertEqual(page["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", page["Cache-Control"])

    def test_keep_and_explicit_clear(self):
        self.client.post("/admin/mail-settings/", self.data())
        original = MailChannelSetting.objects.get().smtp_password_encrypted
        self.client.post("/admin/mail-settings/", self.data(revision=1, smtp_password=""))
        self.assertEqual(MailChannelSetting.objects.get().smtp_password_encrypted, original)
        self.client.post("/admin/mail-settings/", self.data(revision=2, smtp_password="", clear_smtp_password="on"))
        self.assertEqual(MailChannelSetting.objects.get().smtp_password_encrypted, "")

    def test_missing_or_wrong_key_preserves_ciphertext(self):
        self.client.post("/admin/mail-settings/", self.data())
        original = MailChannelSetting.objects.get().smtp_password_encrypted
        for key in ("", Fernet.generate_key().decode()):
            with override_settings(MAIL_CONFIG_ENCRYPTION_KEY=key):
                response = self.client.post("/admin/mail-settings/", self.data(revision=1))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, SECRET)
                self.assertFalse(resolve_mail_channel().complete)
                self.assertEqual(MailChannelSetting.objects.get().smtp_password_encrypted, original)

    def test_invalid_post_never_renders_secret(self):
        response = self.client.post("/admin/mail-settings/", self.data(smtp_port="invalid", brevo_key=SECRET))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SECRET)

    def test_draft_and_provider_switch_are_allowed(self):
        response = self.client.post("/admin/mail-settings/", self.data(from_email="", smtp_host="", smtp_password=""))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(resolve_mail_channel().complete)
        self.client.post("/admin/mail-settings/", self.data(revision=1, provider="brevo", smtp_host="", smtp_password="", brevo_key=SECRET))
        self.assertEqual(resolve_mail_channel().backend, BREVO)
        self.assertTrue(resolve_mail_channel().complete)

    def test_source_is_not_fieldwise_mixed(self):
        self.client.post("/admin/mail-settings/", self.data(from_email="", smtp_password=""))
        snapshot = resolve_mail_channel()
        self.assertEqual(snapshot.source, "database")
        self.assertEqual(snapshot.from_email, "")
        self.assertEqual(snapshot.options["password"], "")
        MailChannelSetting.objects.update(source="env")
        self.assertTrue(resolve_mail_channel().complete)

    def test_smtp_tls_modes(self):
        self.client.post("/admin/mail-settings/", self.data())
        snap = resolve_mail_channel()
        conn = snap.connection()
        self.assertEqual(conn.host, "smtp.example.test")
        self.assertEqual(conn.password, SECRET)
        self.assertTrue(conn.use_tls)
        self.assertFalse(conn.use_ssl)
        MailChannelSetting.objects.update(smtp_security="tls", smtp_port=465)
        conn = resolve_mail_channel().connection()
        self.assertTrue(conn.use_ssl)
        self.assertFalse(conn.use_tls)
        self.assertEqual(conn.port, 465)

    def test_env_path_without_encryption_key(self):
        with override_settings(MAIL_CONFIG_ENCRYPTION_KEY=""):
            self.assertTrue(resolve_mail_channel().complete)

    def test_snapshot_consistency_and_safe_report(self):
        self.client.post("/admin/mail-settings/", self.data())
        old = resolve_mail_channel()
        MailChannelSetting.objects.update(smtp_host="other.example.test")
        self.assertEqual(old.connection().host, "smtp.example.test")
        self.assertEqual(resolve_mail_channel().connection().host, "other.example.test")
        summary = json.dumps(email_delivery_configuration())
        self.assertNotIn(SECRET, summary)
        self.assertNotIn("alerts@example.test", summary)

    def test_permissions_and_csrf(self):
        self.client.logout()
        self.assertEqual(self.client.get("/admin/mail-settings/").status_code, 404)
        staff = get_user_model().objects.create_user("staff", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/admin/mail-settings/").status_code, 404)
        staff.user_permissions.add(Permission.objects.get(codename="change_mailchannelsetting"))
        self.assertEqual(self.client.get("/admin/mail-settings/").status_code, 200)
        self.assertEqual(self.client.post("/admin/mail-settings/", {"action": "test"}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post("/admin/mail-settings/", self.data()).status_code, 403)

    def test_test_email_uses_saved_path_and_prg_throttle(self):
        snap = resolve_mail_channel()
        payload = dict(action="test", test_recipient="test@example.test", confirm="on", fingerprint=snap.fingerprint)
        response = self.client.post("/admin/mail-settings/", payload)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["test@example.test"])
        self.assertEqual(MailChannelSetting.objects.get().last_test_result, "SIMULATED")
        self.client.get("/admin/mail-settings/")
        self.client.post("/admin/mail-settings/", payload)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ProviderHealthTestSetting.objects.count(), 0)

    def test_env_gate_blocks_test_even_superuser(self):
        with override_settings(PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=False):
            snap = resolve_mail_channel()
            with patch("django.core.mail.EmailMessage.send") as send:
                self.client.post("/admin/mail-settings/", dict(action="test", test_recipient="test@example.test", confirm="on", fingerprint=snap.fingerprint))
                send.assert_not_called()

    def test_changed_config_invalidates_test_and_stale_form(self):
        old = resolve_mail_channel()
        self.client.post("/admin/mail-settings/", self.data())
        with patch("django.core.mail.EmailMessage.send") as send:
            self.client.post("/admin/mail-settings/", dict(action="test", test_recipient="test@example.test", confirm="on", fingerprint=old.fingerprint))
            send.assert_not_called()
        response = self.client.post("/admin/mail-settings/", self.data())
        self.assertContains(response, "配置已更新")
        MailChannelSetting.objects.update(last_test_result="ACCEPTED", last_test_fingerprint=old.fingerprint)
        self.assertContains(self.client.get("/admin/mail-settings/"), "尚未验证当前配置")

    def test_smtp_error_safe_and_unknown_not_retried(self):
        import socket
        snap = resolve_mail_channel()
        with patch("django.core.mail.EmailMessage.send", side_effect=socket.timeout(SECRET)) as send:
            response = self.client.post("/admin/mail-settings/", dict(action="test", test_recipient="test@example.test", confirm="on", fingerprint=snap.fingerprint), follow=True)
            self.assertEqual(send.call_count, 1)
            self.assertNotContains(response, SECRET)
            self.assertContains(response, "发送结果未知")

    def test_fields_reject_header_injection_and_invalid_addresses(self):
        for data in (self.data(from_name="name\r\nBcc: x"), self.data(email_recipients="not-email"), self.data(smtp_port=70000), self.data(smtp_host="https://smtp.example.test")):
            self.assertFalse(MailSettingsForm(data, instance=MailChannelSetting()).is_valid())

    def test_db_system_check_ignores_unused_env_smtp(self):
        self.client.post("/admin/mail-settings/", self.data(provider="brevo", brevo_key=SECRET))
        with override_settings(DJANGO_EMAIL_HOST="", DJANGO_DEFAULT_FROM_EMAIL=""):
            self.assertEqual(check_provider_validation_email_configuration(None), [])
        MailChannelSetting.objects.update(brevo_key_encrypted="")
        self.assertTrue(check_provider_validation_email_configuration(None))

    def test_admin_database_test_and_business_share_connection(self):
        from types import SimpleNamespace
        from .services.provider_daily_validation import deliver_notifications
        self.client.post("/admin/mail-settings/", self.data())
        health = ProviderHealthTestSetting.objects.get()
        health.email_notifications_enabled = True
        health.save(update_fields=["email_notifications_enabled"])
        snapshot = resolve_mail_channel()
        observed = []
        def send(message, **kwargs):
            observed.append((message.connection.host, message.connection.password, message.from_email))
            return 1
        with patch.object(EmailMessage, "send", send):
            self.client.post("/admin/mail-settings/", dict(action="test", test_recipient="test@example.test", confirm="on", fingerprint=snapshot.fingerprint))
            with patch("xhs_download.services.provider_daily_validation._notification_subject", return_value="Alert"), patch("xhs_download.services.provider_daily_validation._notification_body", return_value="Safe body"):
                status = deliver_notifications(None, [SimpleNamespace(run=SimpleNamespace(pk=999999))], health)
        self.assertEqual(status, "sent")
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0][0], "smtp.example.test")

    def test_database_unavailable_does_not_fallback_to_env(self):
        from django.db import OperationalError
        with patch("xhs_download.services.mail_channel.stored_channel", side_effect=OperationalError("test")):
            snapshot = resolve_mail_channel()
        self.assertFalse(snapshot.complete)
        self.assertEqual(snapshot.source, "unavailable")

    def test_inactive_admin_denied(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/admin/mail-settings/").status_code, 404)


class BrevoTests(TestCase):
    def response(self, status=201, content=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(content or {"messageId": "test-id"}).encode()]
        return response

    def message(self):
        return EmailMessage("Test", "Plain text", "AnimeImages <sender@example.test>", ["to@example.test"])

    def test_success_payload_and_bounded_transport(self):
        with patch("requests.Session") as cls:
            session = cls.return_value.__enter__.return_value
            session.post.return_value = self.response()
            self.assertEqual(EmailBackend(api_key=SECRET).send_messages([self.message()]), 1)
            args, kwargs = session.post.call_args
            self.assertEqual(args[0], "https://api.brevo.com/v3/smtp/email")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(kwargs["verify"])
            self.assertEqual(kwargs["timeout"], (5, 10))
            self.assertEqual(kwargs["json"]["textContent"], "Plain text")
            self.assertEqual(session.trust_env, False)

    def test_rejection_classification(self):
        for status, code in ((200, "PROVIDER_REJECTED"), (302, "PROVIDER_REJECTED"), (401, "AUTH_FAILED"), (403, "AUTH_FAILED"), (429, "RATE_LIMITED"), (500, "PROVIDER_FAILED")):
            with self.subTest(status=status), patch("requests.Session") as cls:
                cls.return_value.__enter__.return_value.post.return_value = self.response(status)
                with self.assertRaisesRegex(MailError, code):
                    EmailBackend(api_key=SECRET).send_messages([self.message()])

    def test_timeout_unknown_no_retry(self):
        with patch("requests.Session") as cls:
            post = cls.return_value.__enter__.return_value.post
            post.side_effect = requests.Timeout(SECRET)
            with self.assertRaisesRegex(MailError, "UNKNOWN"):
                EmailBackend(api_key=SECRET).send_messages([self.message()])
            self.assertEqual(post.call_count, 1)

    def test_invalid_success_body_and_oversized_response(self):
        for content in (b"{}", b"not-json", b"x" * 8193):
            with patch("requests.Session") as cls:
                response = self.response()
                response.iter_content.return_value = [content]
                cls.return_value.__enter__.return_value.post.return_value = response
                with self.assertRaisesRegex(MailError, "UNKNOWN"):
                    EmailBackend(api_key=SECRET).send_messages([self.message()])

    def test_unsupported_messages_fail_before_network(self):
        for attribute, value in (("attachments", [("x", "x", "text/plain")]), ("cc", ["cc@example.test"]), ("extra_headers", {"X-Test": "x"}), ("content_subtype", "html")):
            message = self.message()
            setattr(message, attribute, value)
            with patch("requests.Session") as session:
                with self.assertRaisesRegex(MailError, "UNSUPPORTED_MESSAGE"):
                    EmailBackend(api_key=SECRET).send_messages([message])
                session.assert_not_called()

    def test_fail_silently_returns_actual_count(self):
        with patch("requests.Session") as cls:
            cls.return_value.__enter__.return_value.post.side_effect = [self.response(), self.response(401)]
            self.assertEqual(EmailBackend(api_key=SECRET, fail_silently=True).send_messages([self.message(), self.message()]), 1)


class MailMigrationTests(TransactionTestCase):
    def test_existing_data_reverse_reapply(self):
        old = [("xhs_download", "0025_provider_daily_validation")]
        new = [("xhs_download", "0026_mail_channel_setting")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old)
            model = executor.loader.project_state(old).apps.get_model("xhs_download", "ProviderHealthTestSetting")
            row = model.objects.create(singleton_key=1, daily_enabled=True, email_notifications_enabled=False)
            executor = MigrationExecutor(connection)
            executor.migrate(new)
            apps = executor.loader.project_state(new).apps
            saved = apps.get_model("xhs_download", "ProviderHealthTestSetting").objects.get(pk=row.pk)
            self.assertTrue(saved.daily_enabled)
            self.assertFalse(saved.email_notifications_enabled)
            self.assertEqual(saved.email_recipients, [])
            self.assertFalse(apps.get_model("xhs_download", "MailChannelSetting").objects.exists())
            executor = MigrationExecutor(connection)
            executor.migrate(old)
            self.assertNotIn("xhs_download_mailchannelsetting", connection.introspection.table_names())
        finally:
            MigrationExecutor(connection).migrate(new)
