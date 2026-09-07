"""Admin mail configuration; design adapted from TopicEye (see docs/admin-mail-settings.md)."""
from dataclasses import dataclass, field
from email.utils import formataddr, parseaddr
import json
import smtplib
import socket

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import get_connection
from django.core.validators import validate_email
from django.db import connection, DatabaseError, transaction
from django.utils.crypto import salted_hmac
from django.utils.module_loading import import_string
from django.core.mail.backends.base import BaseEmailBackend
from django.views.decorators.debug import sensitive_variables

from .env_config import runtime_bool, runtime_float, runtime_int, runtime_list, runtime_str

SMTP = "django.core.mail.backends.smtp.EmailBackend"
BREVO = "xhs_download.services.brevo_email.EmailBackend"
SIMULATED = frozenset({
    "django.core.mail.backends.console.EmailBackend", "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend", "django.core.mail.backends.filebased.EmailBackend",
})


class MailError(Exception):
    pass


@sensitive_variables()
def cipher():
    try:
        return Fernet(runtime_str("MAIL_CONFIG_ENCRYPTION_KEY").encode("ascii"))
    except (ValueError, UnicodeError):
        raise MailError("ENCRYPTION_KEY_UNAVAILABLE") from None


@sensitive_variables()
def encrypt(value):
    return cipher().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


@sensitive_variables()
def decrypt(value):
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise MailError("SECRET_UNREADABLE") from None


def email_addresses(values):
    if not isinstance(values, (list, tuple)) or len(values) > 20:
        raise ValidationError("最多填写 20 个邮箱地址。")
    result = []
    for value in values:
        if not isinstance(value, str) or any(c in value for c in "\r\n"):
            raise ValidationError("邮箱地址无效。")
        value = value.strip()
        validate_email(value)
        if value not in result:
            result.append(value)
    return tuple(result)


def stored_channel():
    from xhs_download.models import MailChannelSetting
    # System checks also run before migrations. An absent table means env mode;
    # other DB failures must not silently select a different sending account.
    if MailChannelSetting._meta.db_table not in connection.introspection.table_names():
        return None
    return MailChannelSetting.objects.select_for_update().filter(singleton_key=1).first()


@dataclass(repr=False)
class MailSnapshot:
    source: str
    backend: str
    from_email: str = field(repr=False)
    recipients: tuple = field(repr=False)
    options: dict = field(repr=False)
    errors: tuple = ()
    env_enabled: bool = False
    fingerprint: str = ""

    @property
    def complete(self):
        return not self.errors

    @property
    def simulated(self):
        return self.backend in SIMULATED

    def connection(self):
        if not self.complete or not self.env_enabled:
            raise MailError("MAIL_GATE_CLOSED")
        return get_connection(self.backend, fail_silently=False, **self.options)


@sensitive_variables()
@transaction.atomic
def resolve_mail_channel():
    errors = []
    try:
        row = stored_channel()
    except DatabaseError:
        return MailSnapshot("unavailable", "", "", (), {}, ("CONFIG_STORE_UNAVAILABLE",))
    source = row.source if row else "env"
    options = {}
    recipients = ()
    backend = ""
    sender = ""
    if source == "database":
        from xhs_download.models import ProviderHealthTestSetting
        health = ProviderHealthTestSetting.objects.filter(singleton_key=1).first()
        recipients = health.email_recipients if health else []
        sender = row.from_email
        if not sender:
            errors.append("FROM_REQUIRED")
        try:
            if sender:
                validate_email(sender)
            if any(c in row.from_name for c in "\r\n"):
                raise ValidationError("name")
        except ValidationError:
            errors.append("INVALID_SENDER")
        sender = formataddr((row.from_name, sender)) if sender else ""
        if row.provider == "smtp":
            backend = SMTP
            if not row.smtp_host or not 1 <= row.smtp_port <= 65535 or row.smtp_security not in {"tls", "starttls"}:
                errors.append("SMTP_PARAMETERS_REQUIRED")
            if not row.smtp_username or not row.smtp_password_encrypted:
                errors.append("SMTP_CREDENTIALS_REQUIRED")
            try:
                password = decrypt(row.smtp_password_encrypted)
            except MailError as exc:
                password = ""
                errors.append(str(exc))
            options = dict(host=row.smtp_host, port=row.smtp_port, username=row.smtp_username,
                           password=password, use_tls=row.smtp_security == "starttls",
                           use_ssl=row.smtp_security == "tls", timeout=10)
        elif row.provider == "brevo":
            backend = BREVO
            try:
                key = decrypt(row.brevo_key_encrypted)
            except MailError as exc:
                key = ""
                errors.append(str(exc))
            if not key:
                errors.append("BREVO_KEY_REQUIRED")
            options = {"api_key": key}
        else:
            errors.append("INVALID_PROVIDER")
    elif source == "env":
        backend = runtime_str("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
        sender = runtime_str("DJANGO_DEFAULT_FROM_EMAIL")
        recipients = runtime_list("PROVIDER_VALIDATION_EMAIL_RECIPIENTS", ())
        if not sender or any(c in sender for c in "\r\n"):
            errors.append("FROM_REQUIRED")
        elif sender:
            try:
                validate_email(parseaddr(sender)[1])
            except ValidationError:
                errors.append("INVALID_SENDER")
        if backend.endswith("smtp.EmailBackend"):
            options = dict(host=runtime_str("DJANGO_EMAIL_HOST"), port=runtime_int("DJANGO_EMAIL_PORT", 25),
                           username=runtime_str("DJANGO_EMAIL_HOST_USER"), password=runtime_str("DJANGO_EMAIL_HOST_PASSWORD"),
                           use_tls=runtime_bool("DJANGO_EMAIL_USE_TLS"), use_ssl=runtime_bool("DJANGO_EMAIL_USE_SSL"),
                           timeout=runtime_float("DJANGO_EMAIL_TIMEOUT_SECONDS", 10))
            if not options["host"]:
                errors.append("SMTP_HOST_REQUIRED")
        if runtime_bool("DJANGO_EMAIL_USE_TLS") and runtime_bool("DJANGO_EMAIL_USE_SSL"):
            errors.append("TLS_CONFLICT")
    else:
        errors.append("INVALID_SOURCE")
    try:
        recipients = email_addresses(recipients)
    except ValidationError:
        recipients = ()
        errors.append("INVALID_RECIPIENTS")
    try:
        cls = import_string(backend)
        if not isinstance(cls, type) or not issubclass(cls, BaseEmailBackend):
            raise ValueError()
    except (ImportError, AttributeError, ValueError):
        errors.append("INVALID_BACKEND")
    enabled = runtime_bool("PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED")
    if enabled and getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG) and backend in SIMULATED:
        errors.append("PRODUCTION_BACKEND_REQUIRED")
    fingerprint = salted_hmac("mail-channel-v1", json.dumps(
        [source, backend, sender, recipients, options, errors, enabled, row.revision if row else 0], sort_keys=True
    ), algorithm="sha256").hexdigest()
    return MailSnapshot(source, backend, sender, recipients, options, tuple(errors), enabled, fingerprint)


def safe_send_error(exc):
    if isinstance(exc, MailError):
        code = str(exc)
        return code if code in {"UNKNOWN", "AUTH_FAILED", "RATE_LIMITED", "PROVIDER_FAILED", "PROVIDER_REJECTED"} else "SEND_FAILED"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "AUTH_FAILED"
    if isinstance(exc, (TimeoutError, socket.timeout, smtplib.SMTPServerDisconnected)):
        return "UNKNOWN"
    return "SEND_FAILED"
