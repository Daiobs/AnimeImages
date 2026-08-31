from django.contrib import admin, messages
from django.http import FileResponse, Http404
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html

from .admin_batch_runtime import (
    BATCH_RUNTIME_MUTATION_FIELDS,
    BatchParseRuntimeSettingAdminForm,
    batch_runtime_effective_state,
    batch_runtime_snapshot,
    log_batch_runtime_admin_mutation,
)
from .models import (
    BatchParseRuntimeSetting,
    DownloadRequestLog,
    ParseRequestLog,
    PolicyControlRecord,
    PolicyLearningRun,
    PolicyOptimizationProposal,
    PolicyRuntimeSetting,
    ProviderAdminAuditLog,
    ProviderHealthTestRun,
    ProviderRuntimeSetting,
    ProviderValidationSuiteRun,
    ReleaseDeployJob,
    RoutingDecisionLog,
    SelfHealingEvent,
    UserFeedback,
)


@admin.register(BatchParseRuntimeSetting)
class BatchParseRuntimeSettingAdmin(admin.ModelAdmin):
    form = BatchParseRuntimeSettingAdminForm
    change_form_template = "admin/xhs_download/batchparseruntimesetting/change_form.html"
    actions = None
    save_as = False
    list_display = (
        "__str__",
        "access_mode",
        "rollout_percent",
        "max_links",
        "staff_max_links",
        "public_max_links",
        "parallel_workers",
        "ip_interval_seconds",
        "daily_ip_limit",
        "updated_at",
    )
    readonly_fields = ("singleton_key", "updated_at")
    fields = (
        "singleton_key",
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
        "updated_at",
        "runtime_version",
        "public_expansion_confirmed",
    )

    def has_add_permission(self, request):
        if BatchParseRuntimeSetting.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def add_view(self, request, form_url="", extra_context=None):
        # The global navigation context normally calls the runtime loader. On the
        # singleton's add page that would create the row during a GET request.
        request._skip_batch_runtime_visibility_load = True
        return super().add_view(request, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        setting = BatchParseRuntimeSetting.objects.filter(singleton_key=1).first()
        if setting is not None:
            return redirect(
                "admin:xhs_download_batchparseruntimesetting_change",
                setting.pk,
            )
        return redirect("admin:xhs_download_batchparseruntimesetting_add")

    def render_change_form(
        self,
        request,
        context,
        add=False,
        change=False,
        form_url="",
        obj=None,
    ):
        saved_setting = None
        object_id = context.get("object_id")
        if object_id:
            saved_setting = BatchParseRuntimeSetting.objects.filter(pk=object_id).first()
        elif request.method == "POST":
            saved_setting = BatchParseRuntimeSetting.objects.filter(singleton_key=1).first()
        context["batch_runtime_effective_state"] = batch_runtime_effective_state(
            saved_setting
        )
        context["batch_runtime_unpersisted_post"] = request.method == "POST"
        context["show_delete"] = False
        context["show_save_and_add_another"] = False
        context["show_save_and_continue"] = False
        return super().render_change_form(
            request,
            context,
            add=add,
            change=change,
            form_url=form_url,
            obj=obj,
        )

    def save_model(self, request, obj, form, change):
        previous = getattr(form, "persisted_snapshot", None)
        super().save_model(request, obj, form, change)
        obj.refresh_from_db()
        current = batch_runtime_snapshot(obj)
        if previous is None:
            changed_fields = list(BATCH_RUNTIME_MUTATION_FIELDS)
        else:
            changed_fields = [
                field_name
                for field_name in form.changed_data
                if field_name in BATCH_RUNTIME_MUTATION_FIELDS
            ]
        log_batch_runtime_admin_mutation(
            request,
            previous=previous,
            current=current,
            changed_fields=changed_fields,
            public_expansion=bool(
                getattr(form, "public_expansion_reasons", ())
            ),
        )

    def response_add(self, request, obj, post_url_continue=None):
        self.message_user(request, "批量解析运行配置已保存并生效。", messages.SUCCESS)
        return redirect(
            "admin:xhs_download_batchparseruntimesetting_change",
            obj.pk,
        )

    def response_change(self, request, obj):
        self.message_user(request, "批量解析运行配置已保存并生效。", messages.SUCCESS)
        return redirect(
            "admin:xhs_download_batchparseruntimesetting_change",
            obj.pk,
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


@admin.register(UserFeedback)
class UserFeedbackAdmin(admin.ModelAdmin):
    actions = None
    list_display = (
        "created_at",
        "status",
        "message_preview",
        "contact_provided",
        "image_provided",
        "page_path",
        "source_platform",
        "request_id_preview",
    )
    list_editable = ("status",)
    list_filter = ("status", "source_platform", "created_at")
    search_fields = ("message", "contact", "request_id", "page_path")
    ordering = ("-created_at",)
    fields = (
        "status",
        "message",
        "contact",
        "image_link",
        "page_path",
        "request_id",
        "source_platform",
        "viewport_width",
        "viewport_height",
        "created_at",
    )
    readonly_fields = (
        "message",
        "contact",
        "image_link",
        "page_path",
        "request_id",
        "source_platform",
        "viewport_width",
        "viewport_height",
        "created_at",
    )

    @admin.display(description="反馈摘要")
    def message_preview(self, obj):
        message = str(obj.message or "").replace("\n", " ")
        return message if len(message) <= 80 else f"{message[:77]}..."

    @admin.display(boolean=True, description="留有联系方式")
    def contact_provided(self, obj):
        return bool(str(obj.contact or "").strip())

    @admin.display(boolean=True, description="包含截图")
    def image_provided(self, obj):
        return bool(obj.image)

    @admin.display(description="问题截图")
    def image_link(self, obj):
        if not obj or not obj.pk or not obj.image:
            return "未上传"
        image_url = reverse(
            "admin:xhs_download_userfeedback_image",
            args=(obj.pk,),
        )
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">查看上传图片</a>',
            image_url,
        )

    @admin.display(description="关联请求 ID", ordering="request_id")
    def request_id_preview(self, obj):
        request_id = str(obj.request_id or "")
        return request_id if len(request_id) <= 20 else f"{request_id[:17]}..."

    def get_urls(self):
        custom_urls = [
            path(
                "<path:object_id>/image/",
                self.admin_site.admin_view(self.image_view),
                name="xhs_download_userfeedback_image",
            ),
        ]
        return custom_urls + super().get_urls()

    def image_view(self, request, object_id):
        feedback = self.get_object(request, object_id)
        if (
            feedback is None
            or not feedback.image
            or not self.has_view_or_change_permission(request, feedback)
        ):
            raise Http404

        suffix = str(feedback.image.name or "").lower().rsplit(".", 1)[-1]
        content_type = {
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(suffix)
        if content_type is None:
            raise Http404

        try:
            image_file = feedback.image.storage.open(feedback.image.name, "rb")
        except (FileNotFoundError, OSError):
            raise Http404

        response = FileResponse(image_file, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="feedback-image.{suffix}"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
        "risk_score",
        "impact_area",
        "control_decision",
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
        "impact_area",
        "control_decision",
        "safety_decision",
        "automatic_execution",
        "runtime_mutated",
        "created_at",
    )
    search_fields = ("kind", "policy_area", "title", "signature", "drift_key", "actor_username")


@admin.register(PolicyControlRecord)
class PolicyControlRecordAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "state",
        "policy_decision",
        "control_decision",
        "execution_result",
        "route",
        "provider",
        "cost",
        "control_confidence",
        "failure_pattern",
        "request_id",
    )
    list_filter = (
        "state",
        "control_decision",
        "policy_decision",
        "execution_result",
        "route",
        "provider",
        "failure_pattern",
        "source",
        "created_at",
    )
    search_fields = ("request_id", "policy_decision", "control_decision", "provider", "route", "failure_pattern")


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
        "suite_run",
        "error_code",
        "notification_event",
        "notification_delivery_status",
        "error_type",
        "actor_username",
        "request_id",
    )
    list_filter = (
        "test_key",
        "status",
        "provider",
        "notification_event",
        "notification_delivery_status",
        "is_staff",
        "created_at",
    )
    search_fields = (
        "request_id",
        "test_key",
        "provider",
        "error_type",
        "actor_username",
        "test_url_host",
        "error_code",
    )


@admin.register(ProviderValidationSuiteRun)
class ProviderValidationSuiteRunAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "trigger",
        "status",
        "total_count",
        "success_count",
        "failed_count",
        "skipped_count",
        "unavailable_count",
        "duration_ms",
        "notification_status",
        "request_id",
    )
    list_filter = ("trigger", "status", "notification_status", "created_at")
    search_fields = ("request_id", "error_code", "actor_username")


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


@admin.register(ReleaseDeployJob)
class ReleaseDeployJobAdmin(ReadOnlyLogAdmin):
    list_display = (
        "created_at",
        "status",
        "target_type",
        "target_ref",
        "target_sha",
        "actor_username",
        "finished_at",
    )
    list_filter = ("status", "target_type", "actor_is_superuser", "created_at")
    search_fields = ("target_ref", "target_sha", "actor_username", "request_id", "error_message")
