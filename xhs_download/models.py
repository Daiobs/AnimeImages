from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator


class ProviderAccessMode(models.TextChoices):
    DISABLED = "disabled", "关闭"
    STAFF = "staff", "仅管理员"
    PUBLIC_GRAY = "public_gray", "公开灰度"
    PUBLIC = "public", "全站开放"


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
