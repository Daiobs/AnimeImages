from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
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
    SelfHealingEvent,
)
from .routing_policy_adjuster import build_routing_policy_recommendations
from .routing_v3 import build_routing_intelligence_snapshot
from .recovery_engine import provider_stability_score, provider_stability_status


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


def build_provider_stability_rows(download_queryset):
    rows = []
    provider_names = (
        download_queryset.exclude(provider="")
        .values_list("provider", flat=True)
        .distinct()
    )
    for provider in provider_names:
        provider_rows = download_queryset.filter(provider=provider)
        total = provider_rows.count()
        if total <= 0:
            continue
        success = provider_rows.filter(status=ObservabilityStatus.SUCCESS).count()
        failures = provider_rows.exclude(status=ObservabilityStatus.SUCCESS).count()
        timeouts = provider_rows.filter(
            Q(failure_type="provider_timeout")
            | Q(error_type__icontains="timeout")
            | Q(error_code__icontains="TIMEOUT")
        ).count()
        average_latency = int(provider_rows.aggregate(avg=Avg("duration_ms"))["avg"] or 0)
        success_ratio = success / total
        failure_ratio = failures / total
        timeout_ratio = timeouts / total
        score = provider_stability_score(
            success_rate=success_ratio,
            timeout_rate=timeout_ratio,
            failure_rate=failure_ratio,
            average_latency_ms=average_latency,
        )
        rows.append(
            {
                "provider": provider,
                "count": total,
                "success_rate": f"{percent(success, total)}%",
                "timeout_rate": f"{percent(timeouts, total)}%",
                "failure_rate": f"{percent(failures, total)}%",
                "average_latency_ms": average_latency,
                "stability_score": score,
                "status": provider_stability_status(score),
            }
        )
    return sorted(rows, key=lambda row: (row["stability_score"], -row["count"], row["provider"]))


def build_system_stability_panel(download_24h, download_7d):
    total_24h = download_24h.count()
    failed_24h = download_24h.exclude(status=ObservabilityStatus.SUCCESS).count()
    routing_failures = download_24h.filter(failure_type="routing_failed").count()
    provider_failures = download_24h.filter(
        Q(failure_type__startswith="provider_")
        | Q(error_type__icontains="provider")
        | Q(provider__gt="")
    ).exclude(status=ObservabilityStatus.SUCCESS).count()
    recovery_count = sum_field(download_24h, "recovery_count")
    fallback_count = sum_field(download_24h, "fallback_count")
    provider_rows = build_provider_stability_rows(download_7d)
    return {
        "summary": (
            {"label": "24h 下载失败率", "value": f"{percent(failed_24h, total_24h)}%"},
            {"label": "24h routing_failure_rate", "value": f"{percent(routing_failures, total_24h)}%"},
            {"label": "24h provider_failure_rate", "value": f"{percent(provider_failures, total_24h)}%"},
            {"label": "24h recovery_count", "value": recovery_count},
            {"label": "24h fallback_count", "value": fallback_count},
        ),
        "top_error_types": top_values(download_24h.exclude(failure_type=""), "failure_type", limit=8),
        "provider_rows": provider_rows,
        "unstable_providers": [
            row for row in provider_rows if row["status"] in {"degraded", "unstable"}
        ][:8],
    }


def build_self_healing_panel(recent_24h_start, recent_7d_start):
    healing_24h = SelfHealingEvent.objects.filter(created_at__gte=recent_24h_start)
    healing_7d = SelfHealingEvent.objects.filter(created_at__gte=recent_7d_start)
    total_24h = healing_24h.count()
    healed_24h = healing_24h.filter(healed=True).count()
    provider_switch_count = healing_24h.exclude(switched_to_provider="").count()
    average_recovery_ms = int(healing_24h.aggregate(avg=Avg("duration_ms"))["avg"] or 0)
    recent_events = [
        {
            "created_at": format_admin_datetime(event.created_at),
            "trigger": event.trigger,
            "failure_type": event.failure_type,
            "action": event.action,
            "healed": event.healed,
            "provider": event.provider,
            "switched_to_provider": event.switched_to_provider,
            "duration_ms": event.duration_ms,
            "request_id": event.request_id,
        }
        for event in healing_24h.order_by("-created_at")[:10]
    ]
    return {
        "summary": (
            {"label": "24h auto_recovery_count", "value": healed_24h},
            {"label": "24h healing_success_rate", "value": f"{percent(healed_24h, total_24h)}%"},
            {"label": "24h failure_to_recovery_time", "value": f"{average_recovery_ms} ms"},
            {"label": "24h provider_auto_switch_count", "value": provider_switch_count},
            {"label": "7d healing_events", "value": healing_7d.count()},
        ),
        "top_failure_causes": top_values(healing_24h.exclude(failure_type=""), "failure_type", limit=8),
        "top_actions": top_values(healing_24h.exclude(action=""), "action", limit=8),
        "recent_events": recent_events,
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
    system_stability = build_system_stability_panel(recent_24h_download, recent_7d_download)
    self_healing = build_self_healing_panel(recent_24h_start, recent_7d_start)
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
        "system_stability": system_stability,
        "self_healing": self_healing,
        "failure_top": top_values(recent_24h_download.exclude(status=ObservabilityStatus.SUCCESS), "error_code"),
        "large_downloads": large_download_rows,
        "blocked_rows": blocked_rows,
        "provider_top": top_values(today_download, "provider"),
        "source_platform_top": top_values(today_download, "source_platform"),
        "recent_audit": recent_audit,
    }
