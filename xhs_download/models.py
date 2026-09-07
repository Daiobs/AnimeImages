import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
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


class KukuToolDownloadDeliveryMode(models.TextChoices):
    BROWSER_FIRST = "browser_first", "浏览器优先，失败后服务端"
    SERVER_STREAM = "server_stream", "服务端 attachment 流优先"
    SERVER_ONLY = "server_only", "仅服务端下载"


class PolicyRuntimeMode(models.TextChoices):
    SHADOW = "shadow", "影子观察"
    STAFF = "staff", "仅管理员启用"
    PUBLIC_GRAY = "public_gray", "公开灰度启用"
    PUBLIC = "public", "全站启用"


def runtime_default_bool(name, default=False):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def runtime_default_int(name, default=0):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def runtime_default_float(name, default=0.0):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class BatchParseRuntimeSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
        verbose_name="单例键",
    )
    access_mode = models.CharField(
        max_length=16,
        choices=ProviderAccessMode.choices,
        default=ProviderAccessMode.STAFF,
        verbose_name="开放模式",
        help_text="disabled 关闭；staff 仅管理员；public_gray 按比例公开；public 全站开放。",
    )
    rollout_percent = models.PositiveSmallIntegerField(
        default=0,
        validators=(MinValueValidator(0), MaxValueValidator(100)),
        verbose_name="公开灰度比例",
    )
    max_links = models.PositiveIntegerField(
        default=20,
        validators=(MinValueValidator(1), MaxValueValidator(200)),
        verbose_name="单次最大链接数",
    )
    staff_max_links = models.PositiveIntegerField(
        default=20,
        validators=(MinValueValidator(1), MaxValueValidator(500)),
        verbose_name="管理员单次最大链接数",
    )
    public_max_links = models.PositiveIntegerField(
        default=3,
        validators=(MinValueValidator(1), MaxValueValidator(100)),
        verbose_name="访客单次最大链接数",
    )
    per_link_timeout_seconds = models.FloatField(
        default=8.0,
        validators=(MinValueValidator(1.0), MaxValueValidator(60.0)),
        verbose_name="每条链接解析超时（秒）",
    )
    total_timeout_seconds = models.FloatField(
        default=60.0,
        validators=(MinValueValidator(1.0), MaxValueValidator(300.0)),
        verbose_name="总解析预算（秒）",
    )
    parallel_workers = models.PositiveSmallIntegerField(
        default=4,
        validators=(MinValueValidator(1), MaxValueValidator(10)),
        verbose_name="并发解析数",
        help_text="同一次批量请求中同时解析的链接数量。建议 3-5，过高可能触发上游限流。",
    )
    ip_interval_seconds = models.PositiveIntegerField(
        default=20,
        validators=(MinValueValidator(0), MaxValueValidator(3600)),
        verbose_name="访客点击间隔（秒）",
        help_text="0 表示不限制。管理员不受此限制。",
    )
    global_minute_limit = models.PositiveIntegerField(
        default=30,
        validators=(MinValueValidator(0), MaxValueValidator(10000)),
        verbose_name="全站每分钟链接额度",
        help_text="按链接数计数，0 表示不限制。管理员不受此限制。",
    )
    daily_ip_limit = models.PositiveIntegerField(
        default=100,
        validators=(MinValueValidator(0), MaxValueValidator(100000)),
        verbose_name="访客每日链接额度 / IP",
        help_text="按链接数计数，0 表示不限制。管理员不受此限制。",
    )
    public_ultra_hd_enabled = models.BooleanField(
        default=False,
        verbose_name="允许访客使用抖音原画/超清",
        help_text="管理员仍按 Provider 运行配置判断，不受此项限制。",
    )
    public_youtube_merge_enabled = models.BooleanField(
        default=False,
        verbose_name="允许访客使用 YouTube 高画质合并",
        help_text="管理员仍按 Provider 运行配置判断，不受此项限制。",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "批量解析运行配置"
        verbose_name_plural = "批量解析运行配置"

    def __str__(self):
        return "批量解析运行配置"

    @classmethod
    def default_values(cls):
        public_enabled = runtime_default_bool("BATCH_PARSE_PUBLIC_ENABLED", False)
        return {
            "access_mode": ProviderAccessMode.PUBLIC if public_enabled else ProviderAccessMode.STAFF,
            "max_links": runtime_default_int("BATCH_PARSE_MAX_LINKS", 20),
            "staff_max_links": runtime_default_int("BATCH_PARSE_STAFF_MAX_LINKS", 20),
            "public_max_links": runtime_default_int("BATCH_PARSE_PUBLIC_MAX_LINKS", 3),
            "per_link_timeout_seconds": runtime_default_float("BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS", 8.0),
            "total_timeout_seconds": runtime_default_float("BATCH_PARSE_TOTAL_TIMEOUT_SECONDS", 60.0),
            "parallel_workers": runtime_default_int("BATCH_PARSE_PARALLEL_WORKERS", 4),
            "ip_interval_seconds": runtime_default_int("BATCH_PARSE_IP_INTERVAL_SECONDS", 20),
            "global_minute_limit": runtime_default_int("BATCH_PARSE_GLOBAL_MINUTE_LIMIT", 30),
            "daily_ip_limit": runtime_default_int("BATCH_PARSE_DAILY_IP_LIMIT", 100),
        }

    @classmethod
    def load(cls):
        setting, _created = cls.objects.get_or_create(
            singleton_key=1,
            defaults=cls.default_values(),
        )
        return setting


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
    kukutool_download_delivery_mode = models.CharField(
        max_length=24,
        choices=KukuToolDownloadDeliveryMode.choices,
        default=KukuToolDownloadDeliveryMode.BROWSER_FIRST,
        verbose_name="抖音超清下载交付策略",
        help_text="browser_first 会优先交给浏览器下载管理器；server_stream 仅在直链不可用时作为服务端 attachment 兜底。",
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


class ProviderValidationSuiteTrigger(models.TextChoices):
    SCHEDULED = "scheduled", "定时任务"
    ADMIN = "admin", "后台手动"
    COMMAND = "command", "命令行"


class ProviderValidationSuiteStatus(models.TextChoices):
    RUNNING = "running", "运行中"
    SUCCESS = "success", "成功"
    DEGRADED = "degraded", "部分异常"
    FAILED = "failed", "执行失败"
    SKIPPED = "skipped", "跳过"


class ProviderValidationNotificationStatus(models.TextChoices):
    DISABLED = "disabled", "未启用"
    NOT_NEEDED = "not_needed", "无需通知"
    SENT = "sent", "已发送"
    FAILED = "failed", "发送失败"


class ProviderHealthNotificationEvent(models.TextChoices):
    NONE = "none", "无"
    FAILURE = "failure", "失败告警"
    REMINDER = "reminder", "持续失败提醒"
    RECOVERY = "recovery", "恢复通知"


class ProviderHealthNotificationDeliveryStatus(models.TextChoices):
    NONE = "none", "无"
    DISABLED = "disabled", "未启用"
    SENT = "sent", "已发送"
    FAILED = "failed", "发送失败"


def default_provider_daily_test_keys():
    return ["health_ready"]


def validate_provider_daily_test_keys(value):
    if not isinstance(value, list) or len(value) > 14:
        raise ValidationError("每日测试项必须是有界列表。")
    normalized = [str(item or "") for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValidationError("每日测试项不能重复。")
    from .services.provider_health_tests import provider_health_test_keys

    allowed = set(provider_health_test_keys())
    if any(item not in allowed for item in normalized):
        raise ValidationError("每日测试项包含未知 key。")


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


class ProviderValidationSuiteRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    request_id = models.CharField(max_length=128, unique=True)
    trigger = models.CharField(
        max_length=16,
        choices=ProviderValidationSuiteTrigger.choices,
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=ProviderValidationSuiteStatus.choices,
        default=ProviderValidationSuiteStatus.RUNNING,
        db_index=True,
    )
    selected_test_keys = models.JSONField(default=list, blank=True)
    total_count = models.PositiveSmallIntegerField(default=0)
    success_count = models.PositiveSmallIntegerField(default=0)
    failed_count = models.PositiveSmallIntegerField(default=0)
    skipped_count = models.PositiveSmallIntegerField(default=0)
    unavailable_count = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    notification_status = models.CharField(
        max_length=16,
        choices=ProviderValidationNotificationStatus.choices,
        default=ProviderValidationNotificationStatus.NOT_NEEDED,
        db_index=True,
    )
    notification_event_count = models.PositiveSmallIntegerField(default=0)
    actor_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("trigger", "created_at")),
            models.Index(fields=("status", "created_at")),
        )
        verbose_name = "Provider 每日验证批次"
        verbose_name_plural = "Provider 每日验证批次"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.trigger} {self.status}"


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
    suite_run = models.ForeignKey(
        ProviderValidationSuiteRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="test_runs",
    )
    error_code = models.CharField(max_length=80, blank=True, default="", db_index=True)
    result_fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    notification_event = models.CharField(
        max_length=16,
        choices=ProviderHealthNotificationEvent.choices,
        default=ProviderHealthNotificationEvent.NONE,
        db_index=True,
    )
    notification_delivery_status = models.CharField(
        max_length=16,
        choices=ProviderHealthNotificationDeliveryStatus.choices,
        default=ProviderHealthNotificationDeliveryStatus.NONE,
        db_index=True,
    )
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("created_at", "test_key")),
            models.Index(fields=("test_key", "status")),
            models.Index(fields=("provider", "created_at")),
            models.Index(fields=("test_key", "notification_event", "created_at")),
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
    xhs_video_url = models.TextField(blank=True, default="")
    xhs_gallery_url = models.TextField(blank=True, default="")
    bilibili_url = models.TextField(blank=True, default="")
    videodl_url = models.TextField(blank=True, default="")
    videodl_backend_id = models.CharField(max_length=80, blank=True, default="")
    daily_enabled = models.BooleanField(default=False)
    daily_test_keys = models.JSONField(
        default=default_provider_daily_test_keys,
        validators=(validate_provider_daily_test_keys,),
    )
    email_notifications_enabled = models.BooleanField(default=False)
    email_recipients = models.JSONField(default=list, blank=True)
    failure_threshold = models.PositiveSmallIntegerField(
        default=2,
        validators=(MinValueValidator(1), MaxValueValidator(10)),
    )
    notification_cooldown_hours = models.PositiveSmallIntegerField(
        default=24,
        validators=(MinValueValidator(1), MaxValueValidator(168)),
    )
    notify_recovery = models.BooleanField(default=True)
    retention_days = models.PositiveSmallIntegerField(
        default=90,
        validators=(MinValueValidator(7), MaxValueValidator(365)),
    )
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

    def clean(self):
        super().clean()
        validate_provider_daily_test_keys(self.daily_test_keys)
        backend_id = str(self.videodl_backend_id or "").strip()
        if not backend_id:
            return
        from .services.provider_health_tests import validate_daily_videodl_backend_id

        if not validate_daily_videodl_backend_id(backend_id):
            raise ValidationError(
                {"videodl_backend_id": "只允许 exact-pin policy 中明确 allowed 的 Dedicated backend。"}
            )


class MailChannelSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    source = models.CharField(max_length=12, default="env", choices=(("env", "环境配置"), ("database", "后台配置")))
    provider = models.CharField(max_length=12, default="smtp", choices=(("smtp", "SMTP"), ("brevo", "Brevo")))
    from_email = models.EmailField(blank=True)
    from_name = models.CharField(max_length=120, blank=True)
    smtp_host = models.CharField(max_length=253, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_username = models.CharField(max_length=254, blank=True)
    smtp_security = models.CharField(max_length=12, default="starttls", choices=(("starttls", "STARTTLS"), ("tls", "TLS")))
    smtp_password_encrypted = models.TextField(blank=True, editable=False)
    brevo_key_encrypted = models.TextField(blank=True, editable=False)
    revision = models.PositiveIntegerField(default=0, editable=False)
    last_test_at = models.DateTimeField(null=True, editable=False)
    last_test_result = models.CharField(max_length=40, blank=True, editable=False)
    last_test_fingerprint = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        verbose_name = "邮件通道配置"
        permissions = (("test_mailchannelsetting", "Can send a mail channel test"),)

    def __str__(self):
        return "邮件通道配置"


class UserFeedbackStatus(models.TextChoices):
    NEW = "new", "新反馈"
    REVIEWED = "reviewed", "已查看"
    RESOLVED = "resolved", "已解决"


def user_feedback_image_upload_to(_instance, filename):
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in {".jpg", ".png", ".webp"}:
        suffix = ".img"
    return f"user-feedback/{timezone.now():%Y/%m}/{uuid4().hex}{suffix}"


class UserFeedback(models.Model):
    message = models.TextField(max_length=1200, verbose_name="反馈内容")
    contact = models.CharField(
        max_length=256,
        blank=True,
        default="",
        verbose_name="联系方式",
    )
    image = models.ImageField(
        upload_to=user_feedback_image_upload_to,
        blank=True,
        max_length=255,
        verbose_name="问题截图",
    )
    page_path = models.CharField(max_length=255, verbose_name="页面路径")
    request_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
        verbose_name="关联请求 ID",
    )
    source_platform = models.CharField(
        max_length=24,
        blank=True,
        default="",
        db_index=True,
        verbose_name="来源平台",
    )
    viewport_width = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="视口宽度",
    )
    viewport_height = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="视口高度",
    )
    status = models.CharField(
        max_length=16,
        choices=UserFeedbackStatus.choices,
        default=UserFeedbackStatus.NEW,
        db_index=True,
        verbose_name="处理状态",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="提交时间",
    )

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "用户反馈"
        verbose_name_plural = "用户反馈"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} 用户反馈"


class ReleaseDeployJobStatus(models.TextChoices):
    PENDING = "pending", "等待执行"
    RUNNING = "running", "执行中"
    SUCCEEDED = "succeeded", "成功"
    FAILED = "failed", "失败"
    CANCELLED = "cancelled", "已取消"


class ReleaseDeployTargetType(models.TextChoices):
    MAIN = "main", "main"
    TAG = "tag", "Tag"
    COMMIT = "commit", "Commit"


class ReleaseDeployJob(models.Model):
    execution_slot = models.PositiveSmallIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ReleaseDeployJobStatus.choices,
        default=ReleaseDeployJobStatus.PENDING,
        db_index=True,
    )
    target_type = models.CharField(
        max_length=16,
        choices=ReleaseDeployTargetType.choices,
        default=ReleaseDeployTargetType.MAIN,
        db_index=True,
    )
    target_ref = models.CharField(max_length=160, db_index=True)
    target_sha = models.CharField(max_length=64, blank=True, default="", db_index=True)
    request_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    actor_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    actor_username = models.CharField(max_length=150, blank=True, default="", db_index=True)
    actor_is_superuser = models.BooleanField(default=False)
    command_plan = models.JSONField(default=dict, blank=True)
    stdout_tail = models.TextField(blank=True, default="")
    stderr_tail = models.TextField(blank=True, default="")
    error_message = models.CharField(max_length=300, blank=True, default="")
    health_check_status = models.CharField(max_length=32, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-created_at",)
        indexes = (
            models.Index(fields=("status", "created_at")),
            models.Index(fields=("target_type", "target_ref")),
            models.Index(fields=("actor_username", "created_at")),
        )
        constraints = (
            models.CheckConstraint(
                condition=models.Q(execution_slot=1),
                name="release_deploy_slot_is_one",
            ),
            models.UniqueConstraint(
                fields=("execution_slot",),
                condition=models.Q(status__in=("pending", "running")),
                name="uniq_active_release_deploy_slot",
            ),
        )
        verbose_name = "发布部署任务"
        verbose_name_plural = "发布部署任务"

    def __str__(self):
        return f"{local_datetime_label(self.created_at)} {self.target_type}:{self.target_ref} {self.status}"
