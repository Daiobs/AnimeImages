from django.contrib import admin

from .models import DownloadRuntimeSetting


@admin.register(DownloadRuntimeSetting)
class DownloadRuntimeSettingAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "rate_limit_per_minute",
        "daily_request_limit_per_ip",
        "global_daily_request_limit",
        "max_concurrent_per_ip",
        "daily_bytes_limit_per_ip",
        "global_daily_bytes_limit",
        "updated_at",
    )
    readonly_fields = ("singleton_key", "updated_at")
    fields = (
        "singleton_key",
        "rate_limit_per_minute",
        "daily_request_limit_per_ip",
        "global_daily_request_limit",
        "max_concurrent_per_ip",
        "daily_bytes_limit_per_ip",
        "global_daily_bytes_limit",
        "updated_at",
    )

    def has_add_permission(self, request):
        if DownloadRuntimeSetting.objects.exists():
            return False
        return super().has_add_permission(request)
