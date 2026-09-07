from django import forms
from django.contrib import admin, messages
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods

from .models import MailChannelSetting, ProviderHealthTestSetting
from .services.mail_channel import (
    MailError, cipher, decrypt, email_addresses, encrypt, resolve_mail_channel, safe_send_error,
)

RESULT_LABELS = {
    "SIMULATED": "本地模拟测试通过，未真实投递。",
    "ACCEPTED": "服务商已接受；不代表已进入收件箱。",
    "UNKNOWN": "发送结果未知，请先核实服务商记录，不要立即重发。",
    "SENDING": "测试发送已启动；若长时间未更新，结果未知，请勿直接重发。",
    "AUTH_FAILED": "发送失败：服务商认证未通过。",
    "RATE_LIMITED": "发送失败：服务商限流。",
    "PROVIDER_FAILED": "发送失败：服务商暂时不可用。",
    "PROVIDER_REJECTED": "发送失败：服务商拒绝请求。",
}


def allowed(user, *, test=False):
    return bool(user.is_active and user.is_staff and user.has_perm("xhs_download.change_mailchannelsetting")
                and (not test or user.has_perm("xhs_download.test_mailchannelsetting")))


class MailSettingsForm(forms.ModelForm):
    smtp_password = forms.CharField(label="SMTP 密码", required=False, max_length=4096,
                                   widget=forms.PasswordInput(render_value=False), strip=False)
    brevo_key = forms.CharField(label="Brevo API Key", required=False, max_length=4096,
                               widget=forms.PasswordInput(render_value=False), strip=False)
    clear_smtp_password = forms.BooleanField(label="清除已保存 SMTP 密码", required=False)
    clear_brevo_key = forms.BooleanField(label="清除已保存 Brevo Key", required=False)
    email_recipients = forms.CharField(label="Asset 3 告警收件人（每行一个）", required=False,
                                      max_length=6000, widget=forms.Textarea(attrs={"rows": 3}))
    revision = forms.IntegerField(widget=forms.HiddenInput)

    class Meta:
        model = MailChannelSetting
        fields = ("source", "provider", "from_email", "from_name", "smtp_host", "smtp_port",
                  "smtp_username", "smtp_security")
        labels = {"source": "配置来源", "provider": "邮件服务", "from_email": "发件地址",
                  "from_name": "发件显示名称", "smtp_host": "SMTP 主机", "smtp_port": "SMTP 端口",
                  "smtp_username": "SMTP 用户名", "smtp_security": "安全连接"}

    def clean(self):
        data = super().clean()
        for name in ("from_name", "smtp_host", "smtp_username", "smtp_password", "brevo_key"):
            if any(c in data.get(name, "") for c in "\r\n"):
                self.add_error(name, "不允许换行。")
        host = data.get("smtp_host", "")
        if host and (any(c.isspace() for c in host) or any(c in host for c in "/@?#")):
            self.add_error("smtp_host", "仅填写 SMTP 主机名或 IP。")
        if data.get("smtp_port") is not None and not 1 <= data["smtp_port"] <= 65535:
            self.add_error("smtp_port", "端口范围为 1 到 65535。")
        try:
            data["email_recipients"] = list(email_addresses(
                [s.strip() for s in data.get("email_recipients", "").splitlines() if s.strip()]))
        except forms.ValidationError:
            self.add_error("email_recipients", "请填写有效邮箱，每行一个，最多 20 个。")
        for name, stored in (("smtp_password", "smtp_password_encrypted"), ("brevo_key", "brevo_key_encrypted")):
            clear = data.get("clear_" + name)
            value = data.get(name)
            if clear and value:
                self.add_error(name, "不能同时清除和设置凭据。")
            if value or clear:
                try:
                    cipher()
                    if getattr(self.instance, stored):
                        decrypt(getattr(self.instance, stored))
                except MailError:
                    self.add_error(name, "主密钥缺失或旧凭据不可解密，未修改凭据。")
        return data


class TestMailForm(forms.Form):
    test_recipient = forms.EmailField(label="测试收件人", max_length=254)
    confirm = forms.BooleanField(label="确认发送一封测试邮件（真实通道会实际外发）")
    fingerprint = forms.CharField(widget=forms.HiddenInput, max_length=64)


@sensitive_post_parameters("smtp_password", "brevo_key", "email_recipients", "test_recipient")
@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
@sensitive_variables()
def mail_settings(request):
    if not allowed(request.user):
        raise Http404
    row = MailChannelSetting.objects.filter(singleton_key=1).first() or MailChannelSetting()
    health = ProviderHealthTestSetting.objects.filter(singleton_key=1).first()
    form = MailSettingsForm(instance=row, initial={"revision": row.revision,
        "email_recipients": "\n".join(health.email_recipients if health else [])})
    snapshot = resolve_mail_channel()
    test_form = TestMailForm(initial={"fingerprint": snapshot.fingerprint})
    if request.method == "POST" and request.POST.get("action") == "save":
        with transaction.atomic():
            row, _ = MailChannelSetting.objects.get_or_create(singleton_key=1)
            row = MailChannelSetting.objects.select_for_update().get(pk=row.pk)
            form = MailSettingsForm(request.POST, instance=row)
            if form.is_valid():
                if form.cleaned_data["revision"] != row.revision:
                    form.add_error(None, "配置已更新，请刷新后重试。")
                else:
                    for name, stored in (("smtp_password", "smtp_password_encrypted"), ("brevo_key", "brevo_key_encrypted")):
                        if form.cleaned_data["clear_" + name]:
                            setattr(row, stored, "")
                        elif form.cleaned_data[name]:
                            setattr(row, stored, encrypt(form.cleaned_data[name]))
                    row.revision += 1
                    row.last_test_result = ""
                    row.last_test_fingerprint = ""
                    row.save()
                    health, _ = ProviderHealthTestSetting.objects.get_or_create(singleton_key=1)
                    health.email_recipients = form.cleaned_data["email_recipients"]
                    health.save(update_fields=["email_recipients", "updated_at"])
                    messages.success(request, "邮件配置已保存；未发送邮件，未开启业务通知。")
                    return redirect("mail_settings")
    elif request.method == "POST" and request.POST.get("action") == "test":
        if not allowed(request.user, test=True):
            raise Http404
        test_form = TestMailForm(request.POST)
        if test_form.is_valid():
            if test_form.cleaned_data["fingerprint"] != snapshot.fingerprint:
                messages.error(request, "配置已变化，请刷新并确认已保存的配置。")
            elif not snapshot.complete or not snapshot.env_enabled:
                messages.error(request, "配置不完整或外发被环境开关关闭。")
            else:
                row, _ = MailChannelSetting.objects.get_or_create(singleton_key=1)
                now = timezone.now()
                claimed = MailChannelSetting.objects.filter(pk=row.pk).filter(
                    Q(last_test_at__isnull=True) | Q(last_test_at__lt=now-timezone.timedelta(seconds=60))
                ).update(last_test_at=now, last_test_result="SENDING", last_test_fingerprint=snapshot.fingerprint)
                if not claimed:
                    messages.error(request, "测试过于频繁，请至少等待 60 秒。")
                else:
                    result = "SEND_FAILED"
                    try:
                        sent = EmailMessage(subject="AnimeImages CANARY", body="此为邮件通道验证，不是故障或恢复通知。",
                            from_email=snapshot.from_email, to=[test_form.cleaned_data["test_recipient"]],
                            connection=snapshot.connection()).send(fail_silently=False)
                        result = ("SIMULATED" if snapshot.simulated else "ACCEPTED") if sent == 1 else "SEND_FAILED"
                    except Exception as exc:
                        result = safe_send_error(exc)
                    MailChannelSetting.objects.filter(pk=row.pk, last_test_at=now,
                        last_test_fingerprint=snapshot.fingerprint).update(last_test_result=result)
                    messages.info(request, RESULT_LABELS.get(result, "发送失败，请检查配置。"))
            return redirect("mail_settings")
    try:
        cipher()
        for value in (row.smtp_password_encrypted, row.brevo_key_encrypted):
            decrypt(value)
        key_ready = True
    except MailError:
        key_ready = False
    context = {**admin.site.each_context(request), "title": "邮件设置", "form": form, "test_form": test_form,
        "mail_complete": snapshot.complete, "mail_errors": snapshot.errors, "mail_source": snapshot.source,
        "env_gate": snapshot.env_enabled, "db_gate": bool(health and health.email_notifications_enabled),
        "recipient_count": len(snapshot.recipients), "key_ready": key_ready,
        "smtp_secret_set": bool(row.smtp_password_encrypted), "brevo_secret_set": bool(row.brevo_key_encrypted),
        "can_test": allowed(request.user, test=True), "last_test_at": row.last_test_at,
        "last_test_result": RESULT_LABELS.get(row.last_test_result, "发送失败。") if row.last_test_result and row.last_test_fingerprint == snapshot.fingerprint else "尚未验证当前配置"}
    response = render(request, "admin/mail_settings.html", context)
    response["X-Content-Type-Options"] = "nosniff"
    return response
