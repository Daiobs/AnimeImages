from django.contrib import admin
from django.shortcuts import redirect

from xhs_download.models import ProviderRuntimeSetting

from .models import ProviderHealthTestPanelProxy, ProviderRuntimeSettingProxy


@admin.register(ProviderRuntimeSettingProxy)
class ProviderRuntimeSettingProxyAdmin(admin.ModelAdmin):
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

    def has_add_permission(self, request):
        if ProviderRuntimeSetting.objects.exists():
            return False
        return super().has_add_permission(request)

    def changelist_view(self, request, extra_context=None):
        return redirect("provider_settings")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return redirect("provider_settings")


@admin.register(ProviderHealthTestPanelProxy)
class ProviderHealthTestPanelProxyAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        return redirect("provider_health_tests")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        user = request.user
        return bool(
            getattr(user, "is_active", False)
            and getattr(user, "is_staff", False)
            and (
                getattr(user, "is_superuser", False)
                or user.has_perm("xhs_download.view_providerhealthtestrun")
            )
        )

    def get_model_perms(self, request):
        if self.has_view_permission(request):
            return {"view": True}
        return {}
