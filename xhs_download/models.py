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
        verbose_name="公开灰度比例",
        help_text="仅在 Provider 设置为公开灰度时生效，0 表示不向访客开放，100 表示全部访客开放。",
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
