"""Bounded Django adapter inspired by TopicEye; see docs/admin-mail-settings.md.

TopicEye email providers: Copyright 2026 fxbin, Apache-2.0.
Rewritten for Django plain-text messages, strict response admission and safe errors.
"""
import json
import time
from email.utils import parseaddr

import requests
from django.core.mail.backends.base import BaseEmailBackend
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from .mail_channel import MailError


class EmailBackend(BaseEmailBackend):
    def __init__(self, *args, api_key="", **kwargs):
        super().__init__(*args, **kwargs)
        self._api_key = api_key

    def send_messages(self, email_messages):
        count = 0
        for message in email_messages or ():
            try:
                self._send(message)
                count += 1
            except MailError:
                if not self.fail_silently:
                    raise
        return count

    @sensitive_variables()
    def _send(self, message):
        if not all(isinstance(value, str) for value in (message.from_email, message.subject, message.body)):
            raise MailError("INVALID_MESSAGE")
        if (message.attachments or message.cc or message.bcc or message.reply_to
                or message.extra_headers or getattr(message, "alternatives", None)
                or message.content_subtype != "plain"):
            raise MailError("UNSUPPORTED_MESSAGE")
        if not self._api_key or not message.to or len(message.to) > 20:
            raise MailError("INVALID_MESSAGE")
        sender_name, sender_address = parseaddr(message.from_email)
        try:
            for value in [message.from_email, message.subject, *message.to]:
                if not isinstance(value, str) or any(c in value for c in "\r\n"):
                    raise ValidationError("header")
            validate_email(sender_address)
            for value in message.to:
                validate_email(value)
        except (ValidationError, TypeError):
            raise MailError("INVALID_MESSAGE") from None
        payload = {"sender": {"email": sender_address, "name": sender_name or "AnimeImages"},
                   "to": [{"email": value} for value in message.to],
                   "subject": message.subject, "textContent": message.body}
        if len(json.dumps(payload).encode("utf-8")) > 65536:
            raise MailError("MESSAGE_TOO_LARGE")
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.post("https://api.brevo.com/v3/smtp/email", json=payload,
                                  headers={"api-key": self._api_key, "accept": "application/json"},
                                  timeout=(5, 10), allow_redirects=False, stream=True, verify=True) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise MailError("AUTH_FAILED")
                    if status == 429:
                        raise MailError("RATE_LIMITED")
                    if status >= 500:
                        raise MailError("PROVIDER_FAILED")
                    if status != 201:
                        raise MailError("PROVIDER_REJECTED")
                    content = bytearray()
                    started = time.monotonic()
                    for chunk in response.iter_content(1):
                        content.extend(chunk)
                        if len(content) > 8192 or time.monotonic() - started > 15:
                            raise MailError("UNKNOWN")
                    try:
                        result = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise MailError("UNKNOWN") from None
                    if not isinstance(result, dict) or not isinstance(result.get("messageId"), str) or not result["messageId"].strip():
                        raise MailError("UNKNOWN")
        except requests.RequestException:
            # The server may already have accepted a POST. Never retry here.
            raise MailError("UNKNOWN") from None
