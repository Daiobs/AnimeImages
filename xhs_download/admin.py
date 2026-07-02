from django.contrib import admin
from django.shortcuts import redirect

from .models import (
    DownloadRequestLog,
    ParseRequestLog,
    PolicyLearningRun,
    PolicyOptimizationProposal,
    PolicyRuntimeSetting,
    ProviderAdminAuditLog,
    ProviderHealthTestRun,
    ProviderRuntimeSetting,
    RoutingDecisionLog,
    SelfHealingEvent,
)


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

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return redirect("provider_settings")

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj=obj)

    def get_model_perms(self, request):
        return {}


class ReadOnlyLogAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(getattr(request.user, "is_superuser", False))


@admin.register(ParseRequestLog)
class ParseRequestLogAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "status",
        "query_host",
        "source_platform",
        "selected_provider",
        "duration_ms",
        "image_count",
        "video_count",
        "error_type",
        "request_id",
    )
    list_filter = (
        "status",
        "source_platform",
        "selected_provider",
        "is_staff",
        "error_type",
        "created_at",
    )
    search_fields = ("request_id", "query_host", "error_type")


@admin.register(DownloadRequestLog)
class DownloadRequestLogAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "status",
        "download_type",
        "item_count",
        "actual_bytes",
        "provider",
        "source_platform",
        "final_policy_route",
        "policy_confidence",
        "policy_active",
        "recovery_action",
        "healing_action",
        "fallback_to_route",
        "failure_type",
        "duration_ms",
        "request_id",
    )
    list_filter = (
        "status",
        "download_type",
        "provider",
        "source_platform",
        "final_policy_route",
        "policy_active",
        "policy_decision_source",
        "recovery_action",
        "healing_action",
        "healing_risk",
        "fallback_to_route",
        "failure_type",
        "created_at",
    )
    search_fields = ("request_id", "provider", "source_platform", "error_type", "failure_type", "healing_action")


@admin.register(RoutingDecisionLog)
class RoutingDecisionLogAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "route_type",
        "provider",
        "success",
        "estimated_server_bytes",
        "actual_server_bytes",
        "duration_ms",
        "error_code",
        "request_id",
    )
    list_filter = ("route_type", "provider", "success", "error_code", "created_at")
    search_fields = ("request_id", "provider", "error_code")


@admin.register(PolicyLearningRun)
class PolicyLearningRunAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "window_hours",
        "status",
        "download_count",
        "parse_count",
        "confidence",
        "estimated_savings_bytes",
    )
    list_filter = ("window_hours", "status", "created_at")
    search_fields = ("status",)


@admin.register(PolicyOptimizationProposal)
class PolicyOptimizationProposalAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "kind",
        "policy_area",
        "status",
        "risk",
        "safety_decision",
        "confidence",
        "sample_count",
        "automatic_execution",
        "runtime_mutated",
    )
    list_filter = (
        "status",
        "kind",
        "policy_area",
        "risk",
        "safety_decision",
        "automatic_execution",
        "runtime_mutated",
        "created_at",
    )
    search_fields = ("kind", "policy_area", "title", "signature", "drift_key", "actor_username")


@admin.register(PolicyRuntimeSetting)
class PolicyRuntimeSettingAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "v35_mode",
        "v35_rollout_percent",
        "v4_adaptive_shadow_enabled",
        "v4_auto_weights_enabled",
        "v4_adaptive_window_hours",
        "v4_adaptive_min_confidence",
        "updated_at",
    )
    readonly_fields = ("singleton_key", "updated_at")
    fields = (
        "singleton_key",
        "v35_mode",
        "v35_rollout_percent",
        "v4_adaptive_shadow_enabled",
        "v4_auto_weights_enabled",
        "v4_adaptive_window_hours",
        "v4_adaptive_min_confidence",
        "updated_at",
    )

    def has_add_permission(self, request):
        if PolicyRuntimeSetting.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(ProviderAdminAuditLog)
class ProviderAdminAuditLogAdmin(ReadOnlyLogAdmin):
    list_display = ("created_at", "actor_username", "action", "changed_fields", "request_id")
    list_filter = ("action", "actor_is_superuser", "created_at")
    search_fields = ("request_id", "actor_username", "action")


@admin.register(ProviderHealthTestRun)
class ProviderHealthTestRunAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "test_key",
        "status",
        "provider",
        "duration_ms",
        "error_type",
        "actor_username",
        "request_id",
    )
    list_filter = ("test_key", "status", "provider", "is_staff", "created_at")
    search_fields = (
        "request_id",
        "test_key",
        "provider",
        "error_type",
        "actor_username",
        "test_url_host",
    )


@admin.register(SelfHealingEvent)
class SelfHealingEventAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "trigger",
        "failure_type",
        "action",
        "healed",
        "provider",
        "switched_to_provider",
        "risk",
        "confidence",
        "duration_ms",
        "request_id",
    )
    list_filter = (
        "trigger",
        "failure_type",
        "action",
        "healed",
        "provider",
        "risk",
        "created_at",
    )
    search_fields = (
        "request_id",
        "trigger",
        "failure_type",
        "action",
        "provider",
        "switched_to_provider",
        "error_type",
    )
