from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from download_console.models import DownloadRuntimeSetting

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderAccessMode,
    ProviderHealthTestRun,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
)
from .provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    hard_enabled_for_provider,
    parse_csv_values,
    rollout_percent_for_provider,
)


ADMIN_DISPLAY_TIME_ZONE = "Asia/Shanghai"

STATUS_LABELS = {
    "safe_for_staff": "适合 staff-only 内测",
    "safe_for_1_percent_gray": "可考虑 1% 公开灰度",
    "caution": "谨慎，建议继续观察",
    "do_not_public": "不建议公开",
}

STATUS_RANK = {
    "safe_for_1_percent_gray": 0,
    "safe_for_staff": 1,
    "caution": 2,
    "do_not_public": 3,
}


@dataclass(frozen=True)
class RolloutProviderConfig:
    provider_id: str
    name: str
    health_keys: tuple[str, ...]
    parse_query: Q
    download_query: Q


PROVIDER_CONFIGS = (
    RolloutProviderConfig(
        provider_id=PROVIDER_DOUYIN_ULTRA,
        name="抖音 KuKuTool 超高清",
        health_keys=("douyin_kukutool_ultra",),
        parse_query=(
            Q(prefer_ultra_hd=True)
            | Q(selected_provider__icontains="kukutool")
            | Q(selected_provider__icontains="ultra")
        ),
        download_query=(
            Q(provider__icontains="kukutool")
            | Q(source_platform__in=("douyin", "tiktok"))
        ),
    ),
    RolloutProviderConfig(
        provider_id=PROVIDER_YOUTUBE_MERGE,
        name="YouTube 高清合并",
        health_keys=("youtube_merge",),
        parse_query=Q(selected_provider__in=("youtube_merge", "yt_dlp_merge")),
        download_query=(
            Q(provider__in=("youtube_merge", "yt_dlp_merge"))
            | Q(source_platform="youtube")
        ),
    ),
    RolloutProviderConfig(
        provider_id=PROVIDER_THIRD_PARTY,
        name="通用第三方 Provider",
        health_keys=("third_party_ytdlp", "gallery_dl"),
        parse_query=(
            Q(source_platform="third_party")
            | Q(selected_provider__in=(
                "yt_dlp_generic",
                "gallery_dl_generic",
                "kukutool_generic",
            ))
        ),
        download_query=(
            Q(source_platform="third_party")
            | Q(provider__in=(
                "yt_dlp_generic",
                "gallery_dl_generic",
                "kukutool_generic",
            ))
        ),
    ),
)


def admin_display_timezone():
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


def local_datetime(value):
    if not value:
        return ""
    return timezone.localtime(value, admin_display_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def provider_mode(provider_id, runtime_setting):
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return runtime_setting.douyin_ultra_mode
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return runtime_setting.youtube_merge_mode
    if provider_id == PROVIDER_THIRD_PARTY:
        return runtime_setting.third_party_mode
    return ProviderAccessMode.DISABLED


def provider_mode_label(mode):
    return dict(ProviderAccessMode.choices).get(mode, mode)


def at_least_status(current, minimum):
    if STATUS_RANK[minimum] > STATUS_RANK[current]:
        return minimum
    return current


def sum_field(queryset, field_name):
    return int(queryset.aggregate(total=Sum(field_name))["total"] or 0)


def latest_health_for_config(config):
    latest_by_key = []
    for key in config.health_keys:
        run = ProviderHealthTestRun.objects.filter(test_key=key).order_by("-created_at").first()
        if run:
            latest_by_key.append(run)
    if not latest_by_key:
        return {
            "status": "",
            "label": "暂无记录",
            "created_at": "",
            "details": "暂无健康测试记录",
            "has_success": False,
            "has_skipped": False,
            "has_failed": False,
        }

    statuses = {run.status for run in latest_by_key}
    if statuses & {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE}:
        status = "failed"
    elif ProviderHealthTestStatus.SKIPPED in statuses:
        status = "skipped"
    elif ProviderHealthTestStatus.SUCCESS in statuses:
        status = "success"
    else:
        status = latest_by_key[0].status

    newest = max(latest_by_key, key=lambda run: run.created_at)
    details = "；".join(
        f"{run.test_key}:{run.status}" for run in sorted(latest_by_key, key=lambda run: run.test_key)
    )
    return {
        "status": status,
        "label": status or "暂无记录",
        "created_at": local_datetime(newest.created_at),
        "details": details,
        "has_success": ProviderHealthTestStatus.SUCCESS in statuses,
        "has_skipped": ProviderHealthTestStatus.SKIPPED in statuses,
        "has_failed": bool(statuses & {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE}),
    }


def third_party_allowlist_is_empty(runtime_setting):
    hosts = parse_csv_values(runtime_setting.third_party_allowed_public_hosts)
    platforms = parse_csv_values(runtime_setting.third_party_allowed_public_platforms)
    return not hosts and not platforms


def evaluate_provider_rollout(
    *,
    config,
    runtime_mode,
    hard_enabled,
    health,
    parse_total_24h,
    parse_success_rate_24h,
    parse_failed_24h,
    avg_duration_ms_24h,
    download_blocked_24h,
    global_usage_percent,
    runtime_setting,
):
    reasons = []
    status = "safe_for_staff"

    if not hard_enabled:
        return "do_not_public", ["环境 capability gate 未开启"]

    if health["has_failed"]:
        return "do_not_public", ["最近健康测试失败 / 不可用"]

    if health["has_skipped"]:
        status = "caution"
        reasons.append("最近健康测试跳过，测试 URL 可能未配置")

    if config.provider_id == PROVIDER_THIRD_PARTY and third_party_allowlist_is_empty(runtime_setting):
        if runtime_mode in {ProviderAccessMode.PUBLIC_GRAY, ProviderAccessMode.PUBLIC}:
            return "do_not_public", ["第三方公开白名单为空"]
        status = at_least_status(status, "caution")
        reasons.append("公开灰度前需要配置第三方 allowlist")

    if parse_total_24h == 0:
        if health["has_success"]:
            status = at_least_status(status, "safe_for_staff")
            reasons.append("健康测试通过，但缺少真实流量样本，建议 staff-only")
        else:
            status = at_least_status(status, "caution")
            reasons.append("最近 24 小时没有样本")
    else:
        if avg_duration_ms_24h > 15000:
            status = at_least_status(status, "do_not_public")
            reasons.append("平均耗时过高")
        elif avg_duration_ms_24h > 8000:
            status = at_least_status(status, "caution")
            reasons.append("平均耗时偏高")

        if parse_success_rate_24h < 70:
            status = at_least_status(status, "do_not_public")
            reasons.append("24h 成功率低于 70%")
        elif parse_success_rate_24h < 90:
            status = at_least_status(status, "caution")
            reasons.append("24h 成功率不足 90%")
        elif (
            parse_total_24h >= 5
            and avg_duration_ms_24h <= 8000
            and parse_failed_24h <= 10
            and health["has_success"]
        ):
            if STATUS_RANK[status] <= STATUS_RANK["safe_for_staff"]:
                status = "safe_for_1_percent_gray"
            reasons.append("24h 成功率达标，可考虑 1% 灰度")
        else:
            status = at_least_status(status, "safe_for_staff")
            reasons.append("数据可用于 staff-only 内测，但公开灰度样本仍不足")

    if global_usage_percent >= 90:
        if runtime_mode in {ProviderAccessMode.PUBLIC_GRAY, ProviderAccessMode.PUBLIC}:
            status = at_least_status(status, "do_not_public")
        else:
            status = at_least_status(status, "caution")
        reasons.append("今日下载流量超过 90%")
    elif global_usage_percent >= 70:
        status = at_least_status(status, "caution")
        reasons.append("今日下载流量超过 70%")

    if download_blocked_24h >= 10:
        status = at_least_status(status, "caution")
        reasons.append("24h 下载阻止或限流较多")

    if runtime_mode == ProviderAccessMode.PUBLIC and status != "safe_for_1_percent_gray":
        reasons.append("当前已全站开放，但近期数据不支持该状态，建议回退")

    if not reasons:
        reasons.append("暂无明显风险，但仍建议保持观察")
    return status, reasons


def provider_parse_queryset(config, since):
    return ParseRequestLog.objects.filter(created_at__gte=since).filter(config.parse_query)


def provider_download_queryset(config, since):
    return DownloadRequestLog.objects.filter(created_at__gte=since).filter(config.download_query)


def top_error_reasons(queryset, limit=10):
    return list(
        queryset.exclude(error_code="")
        .values("error_code")
        .annotate(count=Count("id"))
        .order_by("-count", "error_code")[:limit]
    )


def build_provider_rollout_rows(runtime_setting, *, recent_24h_start, recent_7d_start, global_usage_percent):
    rows = []
    for config in PROVIDER_CONFIGS:
        parse_24h = provider_parse_queryset(config, recent_24h_start)
        download_24h = provider_download_queryset(config, recent_24h_start)
        parse_total = parse_24h.count()
        parse_success = parse_24h.filter(status=ObservabilityStatus.SUCCESS).count()
        parse_failed = parse_24h.exclude(status=ObservabilityStatus.SUCCESS).count()
        avg_duration = int(parse_24h.aggregate(avg=Avg("duration_ms"))["avg"] or 0)
        download_count = download_24h.count()
        download_actual_bytes = sum_field(download_24h, "actual_bytes")
        download_blocked = download_24h.filter(
            status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
        ).count()
        runtime_mode = provider_mode(config.provider_id, runtime_setting)
        hard_enabled = hard_enabled_for_provider(config.provider_id)
        health = latest_health_for_config(config)
        success_rate = percent(parse_success, parse_total)
        recommendation, reasons = evaluate_provider_rollout(
            config=config,
            runtime_mode=runtime_mode,
            hard_enabled=hard_enabled,
            health=health,
            parse_total_24h=parse_total,
            parse_success_rate_24h=success_rate,
            parse_failed_24h=parse_failed,
            avg_duration_ms_24h=avg_duration,
            download_blocked_24h=download_blocked,
            global_usage_percent=global_usage_percent,
            runtime_setting=runtime_setting,
        )
        rows.append({
            "provider_id": config.provider_id,
            "name": config.name,
            "hard_enabled": hard_enabled,
            "capability_label": "已开启" if hard_enabled else "未开启",
            "runtime_mode": runtime_mode,
            "runtime_mode_label": provider_mode_label(runtime_mode),
            "rollout_percent": rollout_percent_for_provider(config.provider_id, runtime_setting),
            "health_status": health["status"] or "none",
            "health_label": health["label"],
            "health_details": health["details"],
            "health_created_at": health["created_at"],
            "parse_total_24h": parse_total,
            "parse_success_rate_24h": f"{success_rate}%",
            "parse_avg_duration_ms_24h": avg_duration,
            "parse_failed_24h": parse_failed,
            "download_count_24h": download_count,
            "download_actual_bytes_24h": format_bytes(download_actual_bytes),
            "download_blocked_24h": download_blocked,
            "recommendation": recommendation,
            "recommendation_label": STATUS_LABELS[recommendation],
            "reasons": reasons,
            "parse_7d_count": provider_parse_queryset(config, recent_7d_start).count(),
            "failure_top": top_error_reasons(parse_24h.exclude(status=ObservabilityStatus.SUCCESS), 5),
        })
    return rows


def build_rollout_guard_context(request):
    now = timezone.now()
    display_tz = admin_display_timezone()
    today_start = timezone.localtime(now, display_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_24h_start = now - timezone.timedelta(hours=24)
    recent_7d_start = now - timezone.timedelta(days=7)

    today_parse = ParseRequestLog.objects.filter(created_at__gte=today_start)
    today_download = DownloadRequestLog.objects.filter(created_at__gte=today_start)
    recent_24h_parse = ParseRequestLog.objects.filter(created_at__gte=recent_24h_start)
    recent_24h_download = DownloadRequestLog.objects.filter(created_at__gte=recent_24h_start)
    recent_7d_parse = ParseRequestLog.objects.filter(created_at__gte=recent_7d_start)
    recent_7d_download = DownloadRequestLog.objects.filter(created_at__gte=recent_7d_start)

    download_setting = DownloadRuntimeSetting.load()
    runtime_setting = ProviderRuntimeSetting.load()
    today_actual_bytes = sum_field(today_download, "actual_bytes")
    global_daily_limit = int(download_setting.global_daily_bytes_limit or 0)
    global_usage_percent = percent(today_actual_bytes, global_daily_limit)
    provider_rows = build_provider_rollout_rows(
        runtime_setting,
        recent_24h_start=recent_24h_start,
        recent_7d_start=recent_7d_start,
        global_usage_percent=global_usage_percent,
    )

    return {
        "title": "放量守卫",
        "admin_time_zone": getattr(display_tz, "key", ADMIN_DISPLAY_TIME_ZONE),
        "cards": (
            {"label": "今日解析次数", "value": today_parse.count()},
            {"label": "今日下载次数", "value": today_download.count()},
            {"label": "今日下载实际流量", "value": format_bytes(today_actual_bytes)},
            {
                "label": "今日全站下载流量使用率",
                "value": "未限制" if global_daily_limit <= 0 else f"{global_usage_percent}%",
            },
            {"label": "24h 解析失败数", "value": recent_24h_parse.exclude(status=ObservabilityStatus.SUCCESS).count()},
            {
                "label": "24h 下载阻止/限流",
                "value": recent_24h_download.filter(
                    status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
                ).count(),
            },
            {"label": "7 天解析总数", "value": recent_7d_parse.count()},
            {"label": "7 天下载实际流量", "value": format_bytes(sum_field(recent_7d_download, "actual_bytes"))},
        ),
        "global_daily_limit": format_bytes(global_daily_limit) if global_daily_limit else "未限制",
        "today_actual_bytes": format_bytes(today_actual_bytes),
        "global_usage_percent": global_usage_percent,
        "provider_rows": provider_rows,
        "failure_top": top_error_reasons(recent_24h_parse.exclude(status=ObservabilityStatus.SUCCESS), 10),
        "download_risk": {
            "blocked_or_limited_24h": recent_24h_download.filter(
                status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
            ).count(),
            "actual_bytes_24h": format_bytes(sum_field(recent_24h_download, "actual_bytes")),
            "global_limit": format_bytes(global_daily_limit) if global_daily_limit else "未限制",
            "usage_label": "未限制" if global_daily_limit <= 0 else f"{global_usage_percent}%",
        },
        "links": {
            "provider_settings": reverse("provider_settings"),
            "provider_health_tests": reverse("provider_health_tests"),
            "parse_logs": reverse("parse_logs"),
            "download_logs": reverse("download_logs"),
            "admin_console": reverse("admin_console"),
            "ops_dashboard": reverse("ops_dashboard"),
        },
    }
