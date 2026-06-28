from django.conf import settings
from django.db import models


class DownloadRuntimeSetting(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
        verbose_name="单例键",
    )
    rate_limit_per_minute = models.PositiveIntegerField(
        default=0,
        verbose_name="每分钟下载请求数 / IP",
        help_text="0 表示不限制。默认值来自 DOWNLOAD_RATE_LIMIT_PER_MINUTE。",
    )
    daily_request_limit_per_ip = models.PositiveIntegerField(
        default=0,
        verbose_name="每日下载请求数 / IP",
        help_text="0 表示不限制。默认值来自 DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP。",
    )
    global_daily_request_limit = models.PositiveIntegerField(
        default=0,
        verbose_name="每日全站下载请求数",
        help_text="0 表示不限制。默认值来自 DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT。",
    )
    max_concurrent_per_ip = models.PositiveIntegerField(
        default=0,
        verbose_name="并发下载数 / IP",
        help_text="0 表示不限制。大文件下载时可适当调高。",
    )
    daily_bytes_limit_per_ip = models.PositiveBigIntegerField(
        default=0,
        verbose_name="每日下载流量 / IP",
        help_text="单位为字节，0 表示不限制。默认值来自 DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP。",
    )
    global_daily_bytes_limit = models.PositiveBigIntegerField(
        default=0,
        verbose_name="每日全站下载流量",
        help_text="单位为字节，0 表示不限制。默认值来自 DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT。",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "下载限流配置"
        verbose_name_plural = "下载限流配置"

    def __str__(self):
        return "下载限流配置"

    @classmethod
    def default_values(cls):
        return {
            "rate_limit_per_minute": settings.DOWNLOAD_RATE_LIMIT_PER_MINUTE,
            "daily_request_limit_per_ip": settings.DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP,
            "global_daily_request_limit": settings.DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT,
            "max_concurrent_per_ip": settings.DOWNLOAD_MAX_CONCURRENT_PER_IP,
            "daily_bytes_limit_per_ip": settings.DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP,
            "global_daily_bytes_limit": settings.DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT,
        }

    @classmethod
    def load(cls):
        setting, created = cls.objects.get_or_create(
            singleton_key=1,
            defaults=cls.default_values(),
        )
        return setting
