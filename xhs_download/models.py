from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils import timezone


def local_datetime_label(value):
    if not value:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")


class ProviderAccessMode(models.TextChoices):
    DISABLED = "disabled", "关闭"
    STAFF = "staff", "仅管理员"
    PUBLIC_GRAY = "public_gray", "公开灰度"
    PUBLIC = "public", "全站开放"


class PolicyRuntimeMode(models.TextChoices):
    SHADOW = "shadow", "影子观察"
    STAFF = "staff", "仅管理员启用"
    PUBLIC_GRAY = "public_gray", "公开灰度启用"
    PUBLIC = "public", "全站启用"


class ProviderRuntimeSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
        verbose_name="单例键",
    )
    douyin_ultra_mode = models.CharField(
        max_length=16,
        choices=ProviderAccessMode.choices,
        default=ProviderAccessMode.DISABLED,
        verbose_name="抖音超高清",
    )
    youtube_merge_mode = models.CharField(
        max_length=16,
        choices=ProviderAccessMode.choices,
        default=ProviderAccessMode.DISABLED,
        verbose_name="YouTube 高清合并",
    )
    third_party_mode = models.CharField(
        max_length=16,
        choices=ProviderAccessMode.choices,
        default=ProviderAccessMode.DISABLED,
        verbose_name="通用第三方 Provider",
    )
    rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="公开灰度比例（已弃用）",
        help_text="兼容旧版本。新逻辑使用每个 Provider 独立的公开灰度比例。",
    )
    douyin_ultra_rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="抖音超高清公开灰度比例",
    )
    youtube_merge_rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="YouTube 高清合并公开灰度比例",
    )
    third_party_rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="通用第三方公开灰度比例",
    )
    douyin_ultra_disabled_reason = models.CharField(
        max_length=240,
        blank=True,
        default="",
        verbose_name="抖音超高清停用原因",
    )
    youtube_merge_disabled_reason = models.CharField(
        max_length=240,
        blank=True,
        default="",
        verbose_name="YouTube 高清合并停用原因",
    )
    third_party_disabled_reason = models.CharField(
        max_length=240,
        blank=True,
        default="",
        verbose_name="通用第三方停用原因",
    )
    third_party_allowed_public_hosts = models.TextField(
        blank=True,
        default="",
        verbose_name="第三方访客允许 Host",
        help_text="逗号或换行分隔。仅 public_gray/public 的访客访问生效。",
    )
    third_party_blocked_hosts = models.TextField(
        blank=True,
        default="",
        verbose_name="第三方阻止 Host",
        help_text="逗号或换行分隔。优先级高于允许列表。",
    )
    third_party_allowed_public_platforms = models.TextField(
        blank=True,
        default="",
        verbose_name="第三方访客允许平台",
        help_text="逗号或换行分隔 platform_id。仅 public_gray/public 的访客访问生效。",
    )
    third_party_blocked_platforms = models.TextField(
        blank=True,
        default="",
        verbose_name="第三方阻止平台",
        help_text="逗号或换行分隔 platform_id。优先级高于允许列表。",
    )
    kukutool_disabled = models.BooleanField(
        default=False,
        verbose_name="临时禁用 KuKuTool",
    )
    douyin_ultra_total_timeout_seconds = models.FloatField(
        default=5.0,
        validators=(MinValueValidator(1.0), MaxValueValidator(30.0)),
        verbose_name="抖音超清前台总预算（秒）",
        help_text="只影响抖音超清解析等待时间，不改变下载链路。",
    )
    douyin_ultra_kukutool_grace_seconds = models.FloatField(
        default=1.0,
        validators=(MinValueValidator(0.0), MaxValueValidator(10.0)),
        verbose_name="KuKuTool 近完成等待（秒）",
        help_text="native 已完成时额外等待 KuKuTool 的时间。",
    )
    kukutool_download_timeout_seconds = models.FloatField(
        default=8.0,
        validators=(MinValueValidator(1.0), MaxValueValidator(180.0)),
        verbose_name="KuKuTool 下载恢复超时（秒）",
        help_text="用于 KuKuTool 签名资源下载失败后的恢复/打包路径；单文件流式下载保持现有快速路径。",
    )
    youtube_frontend_timeout_seconds = models.FloatField(
        default=8.0,
        validators=(MinValueValidator(3.0), MaxValueValidator(60.0)),
        verbose_name="YouTube 前台解析预算（秒）",
        help_text="服务器访问 YouTube 较慢时可临时调高。",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "Provider 运行时配置"
        verbose_name_plural = "Provider 运行时配置"

    def __str__(self):
        return "Provider 运行时配置"

    @classmethod
    def load(cls):
        setting, _created = cls.objects.get_or_create(singleton_key=1)
        return setting


class PolicyRuntimeSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
        verbose_name="单例键",
    )
    v35_mode = models.CharField(
        max_length=16,
        choices=PolicyRuntimeMode.choices,
        default=PolicyRuntimeMode.SHADOW,
        verbose_name="Policy Engine v3.5 模式",
        help_text="默认影子观察；启用后 Policy Engine 可接管真实下载路由。",
    )
    v35_rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="v3.5 公开灰度比例",
        help_text="public_gray 模式下进入 Policy Engine 决策的访客比例。设置 80 即 80% Policy / 20% fallback。",
    )
    v4_adaptive_shadow_enabled = models.BooleanField(
        default=True,
        verbose_name="启用 v4 自适应影子建议",
        help_text="仅生成自适应权重建议，不自动修改真实策略。",
    )
    v4_auto_weights_enabled = models.BooleanField(
        default=False,
        verbose_name="启用 v4 自动权重",
        help_text="默认关闭。开启后 Policy Engine 会按历史反馈读取自适应权重，但不会修改 Provider 或 Cost Guard 配置。",
    )
    v4_adaptive_window_hours = models.PositiveSmallIntegerField(
        default=24,
        validators=(MinValueValidator(1), MaxValueValidator(168)),
        verbose_name="v4 反馈窗口小时数",
    )
    v4_adaptive_min_confidence = models.FloatField(
        default=0.65,
        validators=(MinValueValidator(0.0), MaxValueValidator(1.0)),
        verbose_name="v4 自动权重最低置信度",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "Policy Engine 运行时配置"
        verbose_name_plural = "Policy Engine 运行时配置"

    def __str__(self):
        return "Policy Engine 运行时配置"

    @classmethod
    def load(cls):
        setting, _created = cls.objects.get_or_create(singleton_key=1)
        return setting


class ObservabilityStatus(models.TextChoices):
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"
    BLOCKED = "blocked", "拦截"
    RATE_LIMITED = "rate_limited", "限流"


class ProviderHealthTestStatus(models.TextChoices):
    SUCCESS = "success", "成功"
    FAILED = "failed", "失败"
    SKIPPED = "skipped", "跳过"
    UNAVAILABLE = "unavailable", "不可用"


class PolicyOptimizationStatus(models.TextChoices):
    QUEUED = "queued", "已入队"
    NEEDS_APPROVAL = "needs_approval", "等待审批"
    AUTO_APPLIED = "auto_applied", "已自动应用"
    APPROVED = "approved", "已批准"
    REJECTED = "rejected", "已拒绝"
    APPLIED = "applied", "已应用"
    ROLLED_BACK = "rolled_back", "已回滚"


class PolicyControlState(models.TextChoices):
    PENDING = "pending", "等待处理"
    APPROVED = "approved", "已批准"
    REJECTED = "rejected", "已拒绝"
    AUTO_APPLIED = "auto_applied", "低风险自动通过"
    EXECUTED = "executed", "已执行"
    FAILED = "failed", "执行失败"


class ParseRequestLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    client_identity_hash = models.CharField(max_length=64, blank=True, default="")
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    user_agent_hash = models.CharField(max_length=64, blank=True, default="")
    is_staff = models.BooleanField(default=False, db_index=True)
    page_platform = models.CharField(max_length=32, blank=True, default="", db_index=True)
    query_host = models.CharField(max_length=255, blank=True, default="", db_index=True)
    source_platform = models.CharField(max_length=64, blank=True, default="", db_index=True)
    source_platform_name = models.CharField(max_length=120, blank=True, default="")
    result_platform = models.CharField(max_length=64, blank=True, default="", db_index=True)
    post_type = models.CharField(max_length=32, blank=True, default="")
    selected_provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    provider_label = models.CharField(max_length=120, blank=True, default="")
    requested_ultra_hd = models.BooleanField(default=False)
    prefer_ultra_hd = models.BooleanField(default=False)
    requested_youtube_merge = models.BooleanField(default=False)
    prefer_youtube_merge = models.BooleanField(default=False)
    status = models.CharField(
        max_length=16,
        choices=ObservabilityStatus.choices,
        default=ObservabilityStatus.SUCCESS,
        db_index=True,
    )
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    error_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    error_message_short = models.CharField(max_length=300, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    selected_provider_duration_ms = models.PositiveIntegerField(default=0)
    provider_total_duration_ms = models.PositiveIntegerField(default=0)
    provider_fallback_count = models.PositiveSmallIntegerField(default=0)
    image_count = models.PositiveSmallIntegerField(default=0)
    video_count = models.PositiveSmallIntegerField(default=0)
    runtime_mode = models.CharField(max_length=32, blank=True, default="")
    rollout_percent = models.PositiveSmallIntegerField(default=0)
    rollout_bucket = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "status")),
            models.Index(fields=("source_platform", "created_at")),
            models.Index(fields=("selected_provider", "created_at")),
        )
        verbose_name = "解析请求日志"
        verbose_name_plural = "解析请求日志"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.status} {self.request_id}"


class DownloadRequestLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    client_identity_hash = models.CharField(max_length=64, blank=True, default="")
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_staff = models.BooleanField(default=False, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=ObservabilityStatus.choices,
        default=ObservabilityStatus.SUCCESS,
        db_index=True,
    )
    download_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    item_count = models.PositiveSmallIntegerField(default=0)
    image_count = models.PositiveSmallIntegerField(default=0)
    video_count = models.PositiveSmallIntegerField(default=0)
    estimated_bytes = models.PositiveBigIntegerField(default=0)
    actual_bytes = models.PositiveBigIntegerField(default=0)
    route_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    route_reason = models.CharField(max_length=200, blank=True, default="")
    server_egress_estimated_bytes = models.PositiveBigIntegerField(default=0)
    browser_egress_estimated_bytes = models.PositiveBigIntegerField(default=0)
    provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    source_platform = models.CharField(max_length=64, blank=True, default="", db_index=True)
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    error_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    error_message_short = models.CharField(max_length=300, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    policy_cost_score = models.FloatField(default=0)
    policy_provider_score = models.FloatField(default=0)
    policy_routing_score = models.FloatField(default=0)
    final_policy_route = models.CharField(max_length=32, blank=True, default="", db_index=True)
    policy_confidence = models.FloatField(default=0)
    policy_active = models.BooleanField(default=False, db_index=True)
    policy_decision_source = models.CharField(max_length=80, blank=True, default="", db_index=True)
    policy_estimated_savings_bytes = models.BigIntegerField(default=0)
    recovery_count = models.PositiveSmallIntegerField(default=0)
    fallback_count = models.PositiveSmallIntegerField(default=0)
    recovery_action = models.CharField(max_length=80, blank=True, default="", db_index=True)
    recovery_reason = models.CharField(max_length=200, blank=True, default="")
    fallback_from_route = models.CharField(max_length=32, blank=True, default="", db_index=True)
    fallback_to_route = models.CharField(max_length=32, blank=True, default="", db_index=True)
    failure_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    auto_recovery_count = models.PositiveSmallIntegerField(default=0)
    provider_auto_switch_count = models.PositiveSmallIntegerField(default=0)
    failure_to_recovery_ms = models.PositiveIntegerField(default=0)
    healing_action = models.CharField(max_length=80, blank=True, default="", db_index=True)
    healing_risk = models.CharField(max_length=16, blank=True, default="", db_index=True)
    healing_confidence = models.FloatField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "status")),
            models.Index(fields=("download_type", "created_at")),
            models.Index(fields=("provider", "created_at")),
            models.Index(fields=("final_policy_route", "created_at")),
            models.Index(fields=("recovery_action", "created_at")),
            models.Index(fields=("fallback_to_route", "created_at")),
            models.Index(fields=("failure_type", "created_at")),
            models.Index(fields=("healing_action", "created_at")),
            models.Index(fields=("healing_risk", "created_at")),
        )
        verbose_name = "下载请求日志"
        verbose_name_plural = "下载请求日志"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.status} {self.download_type}"


class RoutingDecisionLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    route_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    estimated_server_bytes = models.PositiveBigIntegerField(default=0)
    estimated_browser_bytes = models.PositiveBigIntegerField(default=0)
    actual_server_bytes = models.PositiveBigIntegerField(default=0)
    success = models.BooleanField(default=False, db_index=True)
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("route_type", "created_at")),
            models.Index(fields=("provider", "created_at")),
            models.Index(fields=("success", "created_at")),
        )
        verbose_name = "路由决策日志"
        verbose_name_plural = "路由决策日志"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.route_type} {self.success}"


class SelfHealingEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    trigger = models.CharField(max_length=64, blank=True, default="", db_index=True)
    failure_type = models.CharField(max_length=64, blank=True, default="", db_index=True)
    action = models.CharField(max_length=80, blank=True, default="", db_index=True)
    risk = models.CharField(max_length=16, blank=True, default="", db_index=True)
    confidence = models.FloatField(default=0)
    healed = models.BooleanField(default=False, db_index=True)
    depth = models.PositiveSmallIntegerField(default=0)
    max_depth = models.PositiveSmallIntegerField(default=3)
    provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    switched_from_provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    switched_to_provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    route_type = models.CharField(max_length=32, blank=True, default="", db_index=True)
    fallback_from_route = models.CharField(max_length=32, blank=True, default="")
    fallback_to_route = models.CharField(max_length=32, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    context_snapshot = models.JSONField(default=dict, blank=True)
    client_identity_hash = models.CharField(max_length=64, blank=True, default="")
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    is_staff = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "trigger")),
            models.Index(fields=("failure_type", "created_at")),
            models.Index(fields=("action", "created_at")),
            models.Index(fields=("provider", "created_at")),
            models.Index(fields=("healed", "created_at")),
        )
        verbose_name = "Self-Healing 事件"
        verbose_name_plural = "Self-Healing 事件"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.trigger} {self.action}"


class PolicyLearningRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    window_hours = models.PositiveSmallIntegerField(default=24, db_index=True)
    window_started_at = models.DateTimeField(db_index=True)
    window_ended_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=16, blank=True, default="success", db_index=True)
    download_count = models.PositiveIntegerField(default=0)
    parse_count = models.PositiveIntegerField(default=0)
    sample_count = models.PositiveIntegerField(default=0)
    shadow_recommendation = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=0)
    estimated_savings_bytes = models.BigIntegerField(default=0)
    route_distribution = models.JSONField(default=dict, blank=True)
    provider_distribution = models.JSONField(default=dict, blank=True)
    cost_summary = models.JSONField(default=dict, blank=True)
    reason_chain = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("window_hours", "created_at")),
            models.Index(fields=("status", "created_at")),
        )
        verbose_name = "Policy 学习运行"
        verbose_name_plural = "Policy 学习运行"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.window_hours}h {self.status}"


class PolicyOptimizationProposal(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    source_version = models.CharField(max_length=16, default="v5", db_index=True)
    source_window_hours = models.PositiveSmallIntegerField(default=24, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=PolicyOptimizationStatus.choices,
        default=PolicyOptimizationStatus.QUEUED,
        db_index=True,
    )
    kind = models.CharField(max_length=80, db_index=True)
    policy_area = models.CharField(max_length=80, blank=True, default="", db_index=True)
    title = models.CharField(max_length=160, blank=True, default="")
    body = models.TextField(blank=True, default="")
    risk = models.CharField(max_length=16, blank=True, default="", db_index=True)
    risk_score = models.FloatField(default=0, db_index=True)
    impact_area = models.CharField(max_length=32, blank=True, default="", db_index=True)
    safety_decision = models.CharField(max_length=16, blank=True, default="", db_index=True)
    control_decision = models.CharField(max_length=32, blank=True, default="", db_index=True)
    confidence = models.FloatField(default=0)
    sample_count = models.PositiveIntegerField(default=0)
    proposed_change = models.JSONField(default=dict, blank=True)
    safety = models.JSONField(default=dict, blank=True)
    drift = models.JSONField(default=dict, blank=True)
    reason_chain = models.JSONField(default=list, blank=True)
    signature = models.CharField(max_length=64, db_index=True)
    drift_key = models.CharField(max_length=120, blank=True, default="", db_index=True)
    automatic_execution = models.BooleanField(default=False, db_index=True)
    runtime_mutated = models.BooleanField(default=False, db_index=True)
    application_result = models.JSONField(default=dict, blank=True)
    rollback_result = models.JSONField(default=dict, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("status", "created_at")),
            models.Index(fields=("kind", "created_at")),
            models.Index(fields=("policy_area", "created_at")),
            models.Index(fields=("drift_key", "created_at")),
            models.Index(fields=("control_decision", "created_at")),
            models.Index(fields=("impact_area", "created_at")),
        )
        verbose_name = "Policy 优化队列"
        verbose_name_plural = "Policy 优化队列"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.kind} {self.status}"


class PolicyControlRecord(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    proposal = models.ForeignKey(
        PolicyOptimizationProposal,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="control_records",
    )
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    policy_decision = models.CharField(max_length=80, blank=True, default="", db_index=True)
    policy_snapshot = models.JSONField(default=dict, blank=True)
    control_decision = models.CharField(max_length=32, blank=True, default="", db_index=True)
    state = models.CharField(
        max_length=32,
        choices=PolicyControlState.choices,
        default=PolicyControlState.PENDING,
        db_index=True,
    )
    execution_result = models.CharField(max_length=32, blank=True, default="", db_index=True)
    execution_snapshot = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    route = models.CharField(max_length=32, blank=True, default="", db_index=True)
    cost = models.PositiveBigIntegerField(default=0)
    control_confidence = models.FloatField(default=0)
    feedback_summary = models.JSONField(default=dict, blank=True)
    failure_pattern = models.CharField(max_length=160, blank=True, default="", db_index=True)
    drift_score = models.FloatField(default=0)
    consistency_score = models.FloatField(default=1)
    transition_history = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=64, blank=True, default="", db_index=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("state", "created_at")),
            models.Index(fields=("control_decision", "created_at")),
            models.Index(fields=("policy_decision", "created_at")),
            models.Index(fields=("provider", "created_at")),
            models.Index(fields=("route", "created_at")),
            models.Index(fields=("failure_pattern", "created_at")),
        )
        verbose_name = "Policy 控制闭环记录"
        verbose_name_plural = "Policy 控制闭环记录"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.state} {self.policy_decision or self.control_decision}"


class ProviderAdminAuditLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    actor_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)
    actor_is_superuser = models.BooleanField(default=False)
    action = models.CharField(max_length=80, db_index=True)
    changed_fields = models.JSONField(default=list, blank=True)
    before_snapshot = models.JSONField(default=dict, blank=True)
    after_snapshot = models.JSONField(default=dict, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    user_agent_hash = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "action")),
            models.Index(fields=("actor_username", "created_at")),
        )
        verbose_name = "Provider 操作审计"
        verbose_name_plural = "Provider 操作审计"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.actor_username} {self.action}"


class ProviderHealthTestRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=128, db_index=True)
    test_key = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=ProviderHealthTestStatus.choices,
        default=ProviderHealthTestStatus.SKIPPED,
        db_index=True,
    )
    provider = models.CharField(max_length=80, blank=True, default="", db_index=True)
    duration_ms = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=120, blank=True, default="", db_index=True)
    error_message_short = models.CharField(max_length=300, blank=True, default="")
    runtime_mode = models.CharField(max_length=32, blank=True, default="")
    hard_enabled = models.BooleanField(default=False, db_index=True)
    is_staff = models.BooleanField(default=False, db_index=True)
    actor_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)
    test_url_host = models.CharField(max_length=255, blank=True, default="", db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "test_key")),
            models.Index(fields=("test_key", "status")),
            models.Index(fields=("provider", "created_at")),
        )
        verbose_name = "Provider 健康测试记录"
        verbose_name_plural = "Provider 健康测试记录"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.test_key} {self.status}"


class ProviderHealthTestSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    douyin_url = models.TextField(blank=True, default="")
    youtube_url = models.TextField(blank=True, default="")
    third_party_url = models.TextField(blank=True, default="")
    gallerydl_url = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Provider 健康测试配置"
        verbose_name_plural = "Provider 健康测试配置"

    @classmethod
    def load(cls):
        setting, _created = cls.objects.get_or_create(singleton_key=1)
        return setting

    def __str__(self):
        return "Provider 健康测试配置"
