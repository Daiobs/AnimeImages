import json
import logging

from django import forms

from .models import BatchParseRuntimeSetting, ProviderAccessMode


logger = logging.getLogger(__name__)


PUBLIC_EXPANSION_CONFIRM_VALUE = "1"
PUBLIC_EXPANSION_CONFIRM_ERROR = (
    "该修改会扩大批量解析的公网访问或公开能力。请检查风险说明后选择“确认并生效”。"
)
STALE_RUNTIME_SETTING_ERROR = "配置已被其他管理员修改，请刷新后重新确认。"

BATCH_RUNTIME_MUTATION_FIELDS = (
    "access_mode",
    "rollout_percent",
    "max_links",
    "staff_max_links",
    "public_max_links",
    "per_link_timeout_seconds",
    "total_timeout_seconds",
    "parallel_workers",
    "ip_interval_seconds",
    "global_minute_limit",
    "daily_ip_limit",
    "public_ultra_hd_enabled",
    "public_youtube_merge_enabled",
)

PUBLIC_MODE_EXPANSIONS = {
    (ProviderAccessMode.DISABLED, ProviderAccessMode.PUBLIC_GRAY),
    (ProviderAccessMode.DISABLED, ProviderAccessMode.PUBLIC),
    (ProviderAccessMode.STAFF, ProviderAccessMode.PUBLIC_GRAY),
    (ProviderAccessMode.STAFF, ProviderAccessMode.PUBLIC),
    (ProviderAccessMode.PUBLIC_GRAY, ProviderAccessMode.PUBLIC),
}


def batch_runtime_version_token(setting):
    updated_at = getattr(setting, "updated_at", None)
    if not updated_at:
        return ""
    return updated_at.isoformat(timespec="microseconds")


def batch_runtime_snapshot(setting):
    if setting is None or not getattr(setting, "pk", None):
        return None
    return {
        "access_mode": str(setting.access_mode or ProviderAccessMode.STAFF),
        "rollout_percent": int(setting.rollout_percent or 0),
        "public_ultra_hd_enabled": bool(setting.public_ultra_hd_enabled),
        "public_youtube_merge_enabled": bool(setting.public_youtube_merge_enabled),
        "updated_at": setting.updated_at,
        "version": batch_runtime_version_token(setting),
    }


def batch_runtime_risk_baseline(snapshot):
    if snapshot is not None:
        return snapshot
    return {
        "access_mode": ProviderAccessMode.STAFF,
        "rollout_percent": 0,
        "public_ultra_hd_enabled": False,
        "public_youtube_merge_enabled": False,
    }


def batch_public_expansion_reasons(previous, proposed):
    previous = batch_runtime_risk_baseline(previous)
    previous_mode = str(previous.get("access_mode") or ProviderAccessMode.STAFF)
    proposed_mode = str(proposed.get("access_mode") or ProviderAccessMode.STAFF)
    previous_rollout = int(previous.get("rollout_percent") or 0)
    proposed_rollout = int(proposed.get("rollout_percent") or 0)
    reasons = []

    if (previous_mode, proposed_mode) in PUBLIC_MODE_EXPANSIONS:
        reasons.append("access_mode_public_expansion")
    if (
        previous_mode == ProviderAccessMode.PUBLIC_GRAY
        and proposed_mode == ProviderAccessMode.PUBLIC_GRAY
        and proposed_rollout > previous_rollout
    ):
        reasons.append("public_gray_rollout_increase")
    if not previous.get("public_ultra_hd_enabled") and proposed.get(
        "public_ultra_hd_enabled"
    ):
        reasons.append("public_ultra_hd_enabled")
    if not previous.get("public_youtube_merge_enabled") and proposed.get(
        "public_youtube_merge_enabled"
    ):
        reasons.append("public_youtube_merge_enabled")
    return reasons


def batch_runtime_effective_state(setting):
    snapshot = batch_runtime_snapshot(setting)
    if snapshot is None:
        return {
            "exists": False,
            "source": "not_created",
            "source_label": "尚未创建持久化配置",
            "source_help": "保存后将使用数据库配置；环境变量只用于初始化默认值或数据库读取失败时的兜底。",
            "access_mode": "",
            "access_mode_label": "尚未创建",
            "anonymous_state": "尚未创建",
            "staff_state": "尚未创建",
            "rollout_active": False,
            "rollout_percent": 0,
            "updated_at": None,
            "risk_access_mode": ProviderAccessMode.STAFF,
            "risk_rollout_percent": 0,
            "public_ultra_hd_enabled": False,
            "public_youtube_merge_enabled": False,
        }

    access_mode = snapshot["access_mode"]
    mode_labels = dict(ProviderAccessMode.choices)
    if access_mode == ProviderAccessMode.PUBLIC:
        anonymous_state = "允许"
    elif access_mode == ProviderAccessMode.PUBLIC_GRAY:
        anonymous_state = f"按 {snapshot['rollout_percent']}% 灰度"
    else:
        anonymous_state = "拒绝"
    staff_state = "拒绝" if access_mode == ProviderAccessMode.DISABLED else "允许"
    return {
        "exists": True,
        "source": "database",
        "source_label": "Database",
        "source_help": "当前数据库 singleton 是权威配置；环境变量不会覆盖已存在的数据库记录。",
        "access_mode": access_mode,
        "access_mode_label": mode_labels.get(access_mode, access_mode),
        "anonymous_state": anonymous_state,
        "staff_state": staff_state,
        "rollout_active": access_mode == ProviderAccessMode.PUBLIC_GRAY,
        "rollout_percent": snapshot["rollout_percent"],
        "updated_at": snapshot["updated_at"],
        "risk_access_mode": access_mode,
        "risk_rollout_percent": snapshot["rollout_percent"],
        "public_ultra_hd_enabled": snapshot["public_ultra_hd_enabled"],
        "public_youtube_merge_enabled": snapshot["public_youtube_merge_enabled"],
    }


def log_batch_runtime_admin_mutation(
    request,
    *,
    previous,
    current,
    changed_fields,
    public_expansion,
):
    user = getattr(request, "user", None)
    previous = previous or {}
    payload = {
        "event": "batch_runtime_admin_mutation",
        "actor_username": str(getattr(user, "get_username", lambda: "")() or "")[:150],
        "actor_is_superuser": bool(getattr(user, "is_superuser", False)),
        "request_id": str(getattr(request, "request_id", "") or "")[:128],
        "changed_fields": [
            field_name
            for field_name in changed_fields
            if field_name in BATCH_RUNTIME_MUTATION_FIELDS
        ],
        "previous_access_mode": str(previous.get("access_mode") or "not_created"),
        "new_access_mode": str(current.get("access_mode") or ""),
        "previous_rollout_percent": int(previous.get("rollout_percent") or 0),
        "new_rollout_percent": int(current.get("rollout_percent") or 0),
        "public_expansion": bool(public_expansion),
    }
    logger.info(
        "batch_runtime_admin_mutation %s",
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        extra={**payload, "status": "success"},
    )


class BatchParseRuntimeSettingAdminForm(forms.ModelForm):
    runtime_version = forms.CharField(required=False, widget=forms.HiddenInput)
    public_expansion_confirmed = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
        initial="0",
    )

    class Meta:
        model = BatchParseRuntimeSetting
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.persisted_snapshot = None
        self.public_expansion_reasons = []
        if not self.is_bound and getattr(self.instance, "pk", None):
            self.initial["runtime_version"] = batch_runtime_version_token(self.instance)
        if not self.is_bound:
            self.initial["public_expansion_confirmed"] = "0"

    def clean(self):
        cleaned_data = super().clean()
        instance_pk = getattr(self.instance, "pk", None)
        if instance_pk:
            current = (
                BatchParseRuntimeSetting.objects.select_for_update()
                .filter(pk=instance_pk)
                .first()
            )
            if current is None:
                raise forms.ValidationError(STALE_RUNTIME_SETTING_ERROR)
            self.persisted_snapshot = batch_runtime_snapshot(current)
            if cleaned_data.get("runtime_version") != batch_runtime_version_token(current):
                raise forms.ValidationError(STALE_RUNTIME_SETTING_ERROR)
        else:
            current = (
                BatchParseRuntimeSetting.objects.select_for_update()
                .filter(singleton_key=1)
                .first()
            )
            if current is not None:
                raise forms.ValidationError(
                    "配置已被其他管理员创建，请刷新后重新确认。"
                )

        required_risk_fields = {
            "access_mode",
            "rollout_percent",
            "public_ultra_hd_enabled",
            "public_youtube_merge_enabled",
        }
        if not required_risk_fields.issubset(cleaned_data):
            return cleaned_data
        proposed = {
            "access_mode": cleaned_data["access_mode"],
            "rollout_percent": cleaned_data["rollout_percent"],
            "public_ultra_hd_enabled": cleaned_data["public_ultra_hd_enabled"],
            "public_youtube_merge_enabled": cleaned_data[
                "public_youtube_merge_enabled"
            ],
        }
        self.public_expansion_reasons = batch_public_expansion_reasons(
            self.persisted_snapshot,
            proposed,
        )
        if (
            self.public_expansion_reasons
            and cleaned_data.get("public_expansion_confirmed")
            != PUBLIC_EXPANSION_CONFIRM_VALUE
        ):
            raise forms.ValidationError(PUBLIC_EXPANSION_CONFIRM_ERROR)
        return cleaned_data
