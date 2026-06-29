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
    single_archive_bytes_limit = models.PositiveBigIntegerField(
        default=0,
        verbose_name="单次 ZIP 下载最大大小",
        help_text="单位为 bytes，0 表示不限制。用于限制单次打包下载的估算或实际 ZIP 大小。",
    )
    single_video_bytes_limit = models.PositiveBigIntegerField(
        default=0,
        verbose_name="单个视频最大大小",
        help_text="单位为 bytes，0 表示不限制。用于限制已知 size_bytes 的视频资源。",
    )
    visitor_video_download_enabled = models.BooleanField(
        default=True,
        verbose_name="允许访客下载视频",
        help_text="关闭后，非 staff 用户只能下载图片；staff 不受此项限制。",
    )
    staff_large_download_exempt = models.BooleanField(
        default=False,
        verbose_name="staff 豁免单次大文件限制",
        help_text="开启后 staff 可绕过单次 ZIP / 单视频限制，但仍不绕过每日流量限制。",
    )
    large_download_warning_bytes = models.PositiveBigIntegerField(
        default=0,
        verbose_name="大文件警告阈值",
        help_text="单位为 bytes，0 表示不提示。仅用于后台运维首页标记大文件下载。",
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
            "single_archive_bytes_limit": settings.DOWNLOAD_SINGLE_ARCHIVE_BYTES_LIMIT,
            "single_video_bytes_limit": settings.DOWNLOAD_SINGLE_VIDEO_BYTES_LIMIT,
            "visitor_video_download_enabled": settings.DOWNLOAD_VISITOR_VIDEO_DOWNLOAD_ENABLED,
            "staff_large_download_exempt": settings.DOWNLOAD_STAFF_LARGE_DOWNLOAD_EXEMPT,
            "large_download_warning_bytes": settings.DOWNLOAD_LARGE_DOWNLOAD_WARNING_BYTES,
        }

    @classmethod
    def load(cls):
        setting, created = cls.objects.get_or_create(
            singleton_key=1,
            defaults=cls.default_values(),
        )
        return setting
