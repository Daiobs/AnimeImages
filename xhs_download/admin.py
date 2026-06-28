from django.contrib import admin
from django.shortcuts import redirect

from .models import ProviderRuntimeSetting


@admin.register(ProviderRuntimeSetting)
class ProviderRuntimeSettingAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "douyin_ultra_mode",
        "youtube_merge_mode",
        "third_party_mode",
        "douyin_ultra_rollout_percent",
        "youtube_merge_rollout_percent",
        "third_party_rollout_percent",
        "kukutool_disabled",
        "updated_at",
    )
    readonly_fields = ("singleton_key", "updated_at")
    fields = (
        "singleton_key",
        "douyin_ultra_mode",
        "douyin_ultra_rollout_percent",
        "douyin_ultra_disabled_reason",
        "youtube_merge_mode",
        "youtube_merge_rollout_percent",
        "youtube_merge_disabled_reason",
        "third_party_mode",
        "third_party_rollout_percent",
        "third_party_disabled_reason",
        "third_party_allowed_public_platforms",
        "third_party_allowed_public_hosts",
        "third_party_blocked_platforms",
        "third_party_blocked_hosts",
        "kukutool_disabled",
        "updated_at",
    )

    def has_add_permission(self, request):
        if ProviderRuntimeSetting.objects.exists():
            return False
        return super().has_add_permission(request)

    def changelist_view(self, request, extra_context=None):
        return redirect("provider_settings")
