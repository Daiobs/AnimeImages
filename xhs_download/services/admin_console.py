from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from download_console.models import DownloadRuntimeSetting

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderAccessMode,
    ProviderAdminAuditLog,
    ProviderHealthTestRun,
    ProviderRuntimeSetting,
)
from .routing_policy_adjuster import build_routing_policy_recommendations
from .routing_v3 import build_routing_intelligence_snapshot


ADMIN_DISPLAY_TIME_ZONE = "Asia/Shanghai"


def admin_console_timezone():
    timezone_name = str(getattr(settings, "ADMIN_DISPLAY_TIME_ZONE", ADMIN_DISPLAY_TIME_ZONE) or ADMIN_DISPLAY_TIME_ZONE)
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(ADMIN_DISPLAY_TIME_ZONE)


def format_bytes(size_bytes):
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if size_bytes <= 0:
        return "0 B"

    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def percent(numerator, denominator):
    denominator = int(denominator or 0)
    if denominator <= 0:
        return 0
    return round((int(numerator or 0) / denominator) * 100, 1)


def sum_field(queryset, field_name):
    return int(queryset.aggregate(total=Sum(field_name))["total"] or 0)


def top_values(queryset, field_name, limit=10):
    return list(
        queryset.exclude(**{field_name: ""})
        .values(field_name)
        .annotate(count=Count("id"))
        .order_by("-count", field_name)[:limit]
    )


def format_admin_datetime(value):
    if not value:
        return ""
    return timezone.localtime(value, admin_console_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def provider_mode_label(mode):
    return dict(ProviderAccessMode.choices).get(mode, mode)


def latest_health_by_key():
    latest = {}
    for run in ProviderHealthTestRun.objects.order_by("-created_at")[:100]:
        latest.setdefault(run.test_key, run)
    return [
        {
            "test_key": run.test_key,
            "status": run.status,
            "provider": run.provider,
            "duration_ms": run.duration_ms,
            "created_at": format_admin_datetime(run.created_at),
            "request_id": run.request_id,
        }
        for run in latest.values()
    ]


def build_download_runtime_summary(setting):
    return (
        ("每分钟下载请求数 / IP", setting.rate_limit_per_minute),
        ("每日下载请求数 / IP", setting.daily_request_limit_per_ip),
        ("每日全站下载请求数", setting.global_daily_request_limit),
        ("并发下载数 / IP", setting.max_concurrent_per_ip),
        ("每日下载流量 / IP", format_bytes(setting.daily_bytes_limit_per_ip)),
        ("每日全站下载流量", format_bytes(setting.global_daily_bytes_limit)),
        ("单次 ZIP 限制", format_bytes(setting.single_archive_bytes_limit) if setting.single_archive_bytes_limit else "未限制"),
        ("单视频限制", format_bytes(setting.single_video_bytes_limit) if setting.single_video_bytes_limit else "未限制"),
        ("访客视频下载", "允许" if setting.visitor_video_download_enabled else "关闭"),
        ("staff 大文件豁免", "开启" if setting.staff_large_download_exempt else "关闭"),
        ("大文件警告阈值", format_bytes(setting.large_download_warning_bytes) if setting.large_download_warning_bytes else "未设置"),
    )


def build_provider_runtime_summary(setting):
    return (
        {
            "name": "抖音 KuKuTool",
            "mode": provider_mode_label(setting.douyin_ultra_mode),
            "rollout": setting.douyin_ultra_rollout_percent,
        },
        {
            "name": "YouTube 高清合并",
            "mode": provider_mode_label(setting.youtube_merge_mode),
            "rollout": setting.youtube_merge_rollout_percent,
        },
        {
            "name": "通用第三方 Provider",
            "mode": provider_mode_label(setting.third_party_mode),
            "rollout": setting.third_party_rollout_percent,
        },
    )


def build_download_routing_summary(queryset):
    rows_by_type = {
        row["route_type"]: row
        for row in queryset.exclude(route_type="")
        .values("route_type")
        .annotate(
            count=Count("id"),
            server_egress=Sum("server_egress_estimated_bytes"),
            browser_egress=Sum("browser_egress_estimated_bytes"),
        )
        .order_by("route_type")
    }
    route_order = ("direct", "proxy", "zip", "blocked", "fallback")
    rows = []
    for route_type in route_order:
        row = rows_by_type.get(route_type, {})
        server_egress = int(row.get("server_egress") or 0)
        browser_egress = int(row.get("browser_egress") or 0)
        rows.append(
            {
                "route_type": route_type,
                "count": int(row.get("count") or 0),
                "server_egress": format_bytes(server_egress),
                "browser_egress": format_bytes(browser_egress),
            }
        )
    total_server_egress = sum_field(queryset, "server_egress_estimated_bytes")
    total_browser_egress = sum_field(queryset, "browser_egress_estimated_bytes")
    return {
        "has_data": bool(rows_by_type),
        "rows": rows,
        "total_server_egress": format_bytes(total_server_egress),
        "total_browser_egress": format_bytes(total_browser_egress),
        "server_egress_saved": format_bytes(total_browser_egress),
    }


def build_routing_intelligence_panel():
    snapshot = build_routing_intelligence_snapshot()
    rows = [
        {
            **row,
            "estimated_server_bytes_label": format_bytes(row["estimated_server_bytes"]),
            "actual_server_bytes_label": format_bytes(row["actual_server_bytes"]),
            "cost_delta_label": format_bytes(abs(row["cost_delta_bytes"])),
            "cost_delta_direction": "高于估算" if row["cost_delta_bytes"] > 0 else "低于估算",
        }
        for row in snapshot["rows"]
    ]
    return {
        **snapshot,
        "rows": rows,
        "recommendations": build_routing_policy_recommendations(snapshot),
    }


def build_admin_console_context():
    now = timezone.now()
    display_tz = admin_console_timezone()
    today_start = timezone.localtime(now, display_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_24h_start = now - timezone.timedelta(hours=24)
    recent_7d_start = now - timezone.timedelta(days=7)

    today_parse = ParseRequestLog.objects.filter(created_at__gte=today_start)
    today_download = DownloadRequestLog.objects.filter(created_at__gte=today_start)
    recent_24h_download = DownloadRequestLog.objects.filter(created_at__gte=recent_24h_start)
    recent_7d_download = DownloadRequestLog.objects.filter(created_at__gte=recent_7d_start)

    today_download_count = today_download.count()
    today_download_success = today_download.filter(status=ObservabilityStatus.SUCCESS).count()
    today_actual_bytes = sum_field(today_download, "actual_bytes")
    today_estimated_bytes = sum_field(today_download, "estimated_bytes")
    recent_24h_actual_bytes = sum_field(recent_24h_download, "actual_bytes")
    recent_7d_actual_bytes = sum_field(recent_7d_download, "actual_bytes")
    routing_summary = build_download_routing_summary(recent_24h_download)
    routing_intelligence = build_routing_intelligence_panel()
    blocked_or_limited = today_download.filter(
        status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
    ).count()

    download_setting = DownloadRuntimeSetting.load()
    provider_setting = ProviderRuntimeSetting.load()
    global_daily_limit = int(download_setting.global_daily_bytes_limit or 0)
    usage_percent = percent(today_actual_bytes, global_daily_limit)
    if global_daily_limit <= 0:
        usage_level = "unlimited"
        usage_label = "未限制"
    elif usage_percent >= 90:
        usage_level = "danger"
        usage_label = f"{usage_percent}%"
    elif usage_percent >= 70:
        usage_level = "warning"
        usage_label = f"{usage_percent}%"
    else:
        usage_level = "normal"
        usage_label = f"{usage_percent}%"

    warning_threshold = int(download_setting.large_download_warning_bytes or 0)
    large_downloads = recent_24h_download
    if warning_threshold > 0:
        large_downloads = large_downloads.filter(actual_bytes__gte=warning_threshold)
    else:
        large_downloads = large_downloads.filter(actual_bytes__gt=0)
    large_download_rows = [
        {
            "created_at": format_admin_datetime(row.created_at),
            "download_type": row.download_type,
            "provider": row.provider,
            "source_platform": row.source_platform,
            "actual_bytes": format_bytes(row.actual_bytes),
            "estimated_bytes": format_bytes(row.estimated_bytes),
            "status": row.status,
            "request_id": row.request_id,
        }
        for row in large_downloads.order_by("-actual_bytes", "-created_at")[:10]
    ]

    blocked_rows = [
        {
            "created_at": format_admin_datetime(row.created_at),
            "status": row.status,
            "error_code": row.error_code,
            "download_type": row.download_type,
            "provider": row.provider,
            "actual_bytes": format_bytes(row.actual_bytes),
            "request_id": row.request_id,
        }
        for row in recent_24h_download.filter(
            status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
        ).order_by("-created_at")[:10]
    ]

    recent_audit = [
        {
            "created_at": format_admin_datetime(row.created_at),
            "actor_username": row.actor_username,
            "action": row.action,
            "request_id": row.request_id,
        }
        for row in ProviderAdminAuditLog.objects.order_by("-created_at")[:5]
    ]

    return {
        "admin_time_zone": getattr(display_tz, "key", ADMIN_DISPLAY_TIME_ZONE),
        "cards": (
            {"label": "今日解析次数", "value": today_parse.count()},
            {"label": "今日下载次数", "value": today_download_count},
            {"label": "今日下载成功率", "value": f"{percent(today_download_success, today_download_count)}%"},
            {"label": "今日下载实际流量", "value": format_bytes(today_actual_bytes)},
            {"label": "今日下载估算流量", "value": format_bytes(today_estimated_bytes)},
            {"label": "今日下载失败数", "value": today_download.filter(status=ObservabilityStatus.FAILED).count()},
            {"label": "今日限流 / 阻止次数", "value": blocked_or_limited},
            {"label": "24h 下载实际流量", "value": format_bytes(recent_24h_actual_bytes)},
            {"label": "7 天下载实际流量", "value": format_bytes(recent_7d_actual_bytes)},
        ),
        "download_setting": download_setting,
        "download_summary": build_download_runtime_summary(download_setting),
        "provider_summary": build_provider_runtime_summary(provider_setting),
        "global_daily_limit": format_bytes(global_daily_limit) if global_daily_limit else "未限制",
        "today_actual_bytes": format_bytes(today_actual_bytes),
        "usage_percent": usage_percent,
        "usage_label": usage_label,
        "usage_level": usage_level,
        "health_runs": latest_health_by_key(),
        "routing_summary": routing_summary,
        "routing_intelligence": routing_intelligence,
        "failure_top": top_values(recent_24h_download.exclude(status=ObservabilityStatus.SUCCESS), "error_code"),
        "large_downloads": large_download_rows,
        "blocked_rows": blocked_rows,
        "provider_top": top_values(today_download, "provider"),
        "source_platform_top": top_values(today_download, "source_platform"),
        "recent_audit": recent_audit,
    }
