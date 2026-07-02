import logging
import subprocess

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Sum
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from djangoProject.health import get_runtime_readiness
from download_console.models import DownloadRuntimeSetting

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    PolicyControlRecord,
    PolicyControlState,
    PolicyLearningRun,
    PolicyOptimizationProposal,
    PolicyOptimizationStatus,
    ProviderHealthTestRun,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
    SelfHealingEvent,
)
from .feedback_loop import build_feedback, build_insights, build_suggestions
from .governance_engine import GOVERNANCE_VERSION, build_governance_v8_context
from .provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    provider_access_summary,
)


logger = logging.getLogger(__name__)

CONTROL_PLANE_VERSION = "production_control_plane_v1"
CONTROL_PLANE_CACHE_SECONDS = 45
OBSERVABILITY_PERMISSIONS = (
    "xhs_download.view_parserequestlog",
    "xhs_download.view_downloadrequestlog",
    "xhs_download.view_providerhealthtestrun",
    "xhs_download.view_provideradminauditlog",
)
PROVIDER_ROWS = (
    {
        "provider_id": PROVIDER_DOUYIN_ULTRA,
        "name": "抖音 KuKuTool 超清",
        "test_keys": ("douyin_kukutool_ultra",),
        "log_providers": ("kukutool", "kukutool_generic"),
    },
    {
        "provider_id": PROVIDER_YOUTUBE_MERGE,
        "name": "YouTube merge",
        "test_keys": ("youtube_merge",),
        "log_providers": ("youtube_merge", "yt_dlp_merge"),
    },
    {
        "provider_id": PROVIDER_THIRD_PARTY,
        "name": "通用第三方 Provider",
        "test_keys": ("third_party_ytdlp", "gallery_dl"),
        "log_providers": ("yt_dlp_generic", "gallery_dl_generic", "kukutool_generic"),
    },
)


def local_now():
    return timezone.localtime(timezone.now(), timezone.get_current_timezone())


def window_start(hours=24):
    return timezone.now() - timezone.timedelta(hours=int(hours or 24))


def day_start():
    now = timezone.localtime(timezone.now())
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def safe_section(name, builder, fallback):
    try:
        return builder()
    except Exception as error:
        logger.exception("control_plane_section_failed", extra={"section": name})
        return {
            **fallback,
            "available": False,
            "error": type(error).__name__,
        }


def format_bytes(value):
    value = int(value or 0)
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def percent(numerator, denominator):
    denominator = int(denominator or 0)
    if denominator <= 0:
        return 0
    return round((float(numerator or 0) / denominator) * 100, 1)


def ratio(numerator, denominator):
    denominator = float(denominator or 0)
    if denominator <= 0:
        return 0.0
    return round(float(numerator or 0) / denominator, 4)


def url_or_empty(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def current_commit_hash():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=settings.BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        logger.debug("control_plane_commit_hash_unavailable", exc_info=True)
    return ""


def cache_get(key):
    try:
        return cache.get(key)
    except Exception as error:
        logger.warning("control_plane_cache_get_failed", extra={"error_type": type(error).__name__})
        return None


def cache_set(key, value, timeout=CONTROL_PLANE_CACHE_SECONDS):
    try:
        cache.set(key, value, timeout=timeout)
    except Exception as error:
        logger.warning("control_plane_cache_set_failed", extra={"error_type": type(error).__name__})


def build_system_status(request):
    readiness = get_runtime_readiness()
    return {
        "available": True,
        "environment": getattr(settings, "DJANGO_ENV", "local"),
        "debug": bool(getattr(settings, "DEBUG", False)),
        "commit_hash": current_commit_hash(),
        "current_time": local_now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "health_ready": bool(readiness.get("ready")),
        "database_ready": bool((readiness.get("components") or {}).get("database")),
        "cache_ready": bool((readiness.get("components") or {}).get("cache")),
        "components": readiness.get("components") or {},
        "request_id": str(getattr(request, "request_id", "") or ""),
        "risk_level": "normal",
    }


def build_core_metrics():
    start_today = day_start()
    start_24h = window_start(24)
    today_parse = ParseRequestLog.objects.filter(created_at__gte=start_today)
    today_download = DownloadRequestLog.objects.filter(created_at__gte=start_today)
    recent_download = DownloadRequestLog.objects.filter(created_at__gte=start_24h)
    recent_health = ProviderHealthTestRun.objects.filter(created_at__gte=start_24h)
    recent_healing = SelfHealingEvent.objects.filter(created_at__gte=start_24h)
    today_download_total = today_download.count()
    today_download_success = today_download.filter(status=ObservabilityStatus.SUCCESS).count()
    actual_bytes = today_download.aggregate(total=Sum("actual_bytes"))["total"] or 0
    blocked_count = today_download.filter(
        status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED)
    ).count()
    fallback_count = recent_download.aggregate(total=Sum("fallback_count"))["total"] or 0
    provider_failed = recent_health.exclude(status=ProviderHealthTestStatus.SUCCESS).count()
    governance_records = PolicyControlRecord.objects.filter(
        created_at__gte=start_24h,
        source=GOVERNANCE_VERSION,
    )
    runtime = DownloadRuntimeSetting.load()
    global_limit = int(runtime.global_daily_bytes_limit or 0)
    usage = ratio(actual_bytes, global_limit) if global_limit else 0
    return {
        "available": True,
        "today_parse_count": today_parse.count(),
        "today_download_count": today_download_total,
        "today_download_success_rate": percent(today_download_success, today_download_total),
        "today_actual_bytes": actual_bytes,
        "today_actual_bytes_label": format_bytes(actual_bytes),
        "today_blocked_count": blocked_count,
        "provider_failed_24h": provider_failed,
        "fallback_count_24h": fallback_count,
        "self_healing_count_24h": recent_healing.count(),
        "governance_blocked_24h": governance_records.filter(state=PolicyControlState.REJECTED).count(),
        "governance_pending_24h": governance_records.filter(state=PolicyControlState.PENDING).count(),
        "governance_approved_24h": governance_records.filter(
            state__in=(PolicyControlState.APPROVED, PolicyControlState.AUTO_APPLIED, PolicyControlState.EXECUTED)
        ).count(),
        "global_daily_bytes_limit": global_limit,
        "global_daily_bytes_usage_rate": usage,
        "global_daily_bytes_usage_label": f"{round(usage * 100, 1)}%" if global_limit else "未设置",
    }


def latest_health_for(test_keys):
    return ProviderHealthTestRun.objects.filter(test_key__in=test_keys).order_by("-created_at").first()


def provider_log_stats(provider_names):
    logs = DownloadRequestLog.objects.filter(
        created_at__gte=window_start(24),
        provider__in=provider_names,
    )
    total = logs.count()
    success = logs.filter(status=ObservabilityStatus.SUCCESS).count()
    stats = logs.aggregate(avg_duration=Avg("duration_ms"))
    latest_failed = logs.exclude(status=ObservabilityStatus.SUCCESS).order_by("-created_at").first()
    return {
        "count": total,
        "success_rate": percent(success, total),
        "average_duration_ms": round(float(stats["avg_duration"] or 0), 1),
        "recent_failure": latest_failed.error_type or latest_failed.error_code if latest_failed else "",
    }


def build_provider_status(request):
    runtime = ProviderRuntimeSetting.load()
    rows = []
    for item in PROVIDER_ROWS:
        access = provider_access_summary(item["provider_id"], request, runtime)
        last_run = latest_health_for(item["test_keys"])
        stats = provider_log_stats(item["log_providers"])
        rows.append({
            **item,
            "runtime_mode": access["effective_mode"],
            "runtime_label": access["effective_label"],
            "rollout_percent": access["rollout_percent"],
            "hard_enabled": access["hard_enabled"],
            "access_reason": access["access_reason"],
            "last_health_status": last_run.status if last_run else "",
            "last_health_status_label": last_run.get_status_display() if last_run else "暂无数据",
            "last_health_at": timezone.localtime(last_run.created_at).strftime("%Y-%m-%d %H:%M:%S") if last_run else "",
            "success_rate_24h": stats["success_rate"],
            "average_duration_ms_24h": stats["average_duration_ms"],
            "recent_failure": stats["recent_failure"] or "暂无数据",
            "settings_url": url_or_empty("provider_settings"),
            "health_url": url_or_empty("provider_health_tests"),
            "parse_logs_url": url_or_empty("parse_logs"),
        })
    return {"available": True, "rows": rows}


def route_distribution_rows(queryset):
    rows = list(
        queryset.values("route_type")
        .annotate(count=Count("id"))
        .order_by("-count", "route_type")
    )
    total = sum(row["count"] for row in rows)
    return [
        {
            "route_type": row["route_type"] or "unknown",
            "count": row["count"],
            "percent": percent(row["count"], total),
        }
        for row in rows
    ]


def build_download_cost_status():
    start_today = day_start()
    today_download = DownloadRequestLog.objects.filter(created_at__gte=start_today)
    aggregates = today_download.aggregate(
        server_bytes=Sum("server_egress_estimated_bytes"),
        browser_bytes=Sum("browser_egress_estimated_bytes"),
        actual_bytes=Sum("actual_bytes"),
        estimated_bytes=Sum("estimated_bytes"),
    )
    zip_count = today_download.filter(route_type="zip").count()
    total = today_download.count()
    big_files = list(
        today_download.filter(actual_bytes__gt=0)
        .order_by("-actual_bytes")
        .values("created_at", "download_type", "route_type", "provider", "actual_bytes")[:5]
    )
    for item in big_files:
        item["actual_bytes_label"] = format_bytes(item["actual_bytes"])
        item["created_at_label"] = timezone.localtime(item["created_at"]).strftime("%H:%M:%S")
    limit_hits = today_download.filter(
        status__in=(ObservabilityStatus.BLOCKED, ObservabilityStatus.RATE_LIMITED),
    ).values("error_code").annotate(count=Count("id")).order_by("-count")[:10]
    runtime = DownloadRuntimeSetting.load()
    runtime_summary = {
        "global_daily_bytes_limit": runtime.global_daily_bytes_limit,
        "global_daily_bytes_limit_label": format_bytes(runtime.global_daily_bytes_limit),
        "daily_bytes_limit_per_ip": runtime.daily_bytes_limit_per_ip,
        "daily_bytes_limit_per_ip_label": format_bytes(runtime.daily_bytes_limit_per_ip),
        "single_archive_bytes_limit": runtime.single_archive_bytes_limit,
        "single_archive_bytes_limit_label": format_bytes(runtime.single_archive_bytes_limit),
        "single_video_bytes_limit": runtime.single_video_bytes_limit,
        "single_video_bytes_limit_label": format_bytes(runtime.single_video_bytes_limit),
        "visitor_video_download_enabled": runtime.visitor_video_download_enabled,
        "staff_large_download_exempt": runtime.staff_large_download_exempt,
    }
    actual_bytes = aggregates["actual_bytes"] or 0
    global_limit = runtime.global_daily_bytes_limit or 0
    return {
        "available": True,
        "route_distribution": route_distribution_rows(today_download),
        "server_egress_estimated_bytes": aggregates["server_bytes"] or 0,
        "server_egress_estimated_label": format_bytes(aggregates["server_bytes"] or 0),
        "browser_egress_estimated_bytes": aggregates["browser_bytes"] or 0,
        "browser_egress_estimated_label": format_bytes(aggregates["browser_bytes"] or 0),
        "actual_bytes": actual_bytes,
        "actual_bytes_label": format_bytes(actual_bytes),
        "zip_ratio": percent(zip_count, total),
        "mobile_ratio": None,
        "mobile_ratio_label": "暂无数据",
        "limit_hits": list(limit_hits),
        "big_files": big_files,
        "runtime": runtime_summary,
        "global_usage_rate": ratio(actual_bytes, global_limit) if global_limit else 0,
        "download_settings_url": url_or_empty("admin_console"),
        "download_logs_url": url_or_empty("download_logs"),
    }


def build_policy_governance_status():
    start_24h = window_start(24)
    feedback = build_feedback(window_hours=24)
    insights = build_insights(feedback)
    suggestions = build_suggestions(insights)
    latest_learning = PolicyLearningRun.objects.order_by("-created_at").first()
    pending_approvals = PolicyOptimizationProposal.objects.filter(
        status=PolicyOptimizationStatus.NEEDS_APPROVAL,
    ).count()
    governance_records = PolicyControlRecord.objects.filter(
        created_at__gte=start_24h,
        source=GOVERNANCE_VERSION,
    )
    governance_distribution = list(
        governance_records.values("control_decision")
        .annotate(count=Count("id"))
        .order_by("-count", "control_decision")
    )
    recent_events = list(
        PolicyControlRecord.objects.filter(created_at__gte=start_24h)
        .order_by("-created_at")
        .values("created_at", "policy_decision", "control_decision", "state", "source")[:5]
    )
    for event in recent_events:
        event["created_at_label"] = timezone.localtime(event["created_at"]).strftime("%H:%M:%S")
    governance_context = build_governance_v8_context(window_hours=24)
    safety = (governance_context.get("governance") or {}).get("system_safety_status") or {}
    return {
        "available": True,
        "feedback_generated_at": feedback.get("signals", {}).get("window_ended_at", ""),
        "insight_count": len(insights.get("insights") or []),
        "suggestion_count": len(suggestions.get("suggestions") or []),
        "latest_learning_status": latest_learning.status if latest_learning else "暂无数据",
        "latest_learning_at": timezone.localtime(latest_learning.created_at).strftime("%Y-%m-%d %H:%M:%S") if latest_learning else "",
        "pending_approvals": pending_approvals,
        "governance_decision_count": governance_records.count(),
        "governance_distribution": governance_distribution,
        "stability_index": safety.get("stability_index", 0),
        "stability_index_label": safety.get("stability_index_label", "暂无数据"),
        "risk_score": safety.get("risk_exposure", 0),
        "risk_label": safety.get("risk_exposure_label", "normal"),
        "recent_events": recent_events,
        "feedback_url": url_or_empty("feedback_loop"),
        "policy_url": url_or_empty("policy_engine"),
        "policy_control_url": url_or_empty("policy_control"),
        "governance_url": url_or_empty("governance_v8"),
        "rollout_url": url_or_empty("rollout_guard"),
    }


def derive_risk_level(status, metrics, provider_status, cost_status, policy_status):
    if not status.get("health_ready"):
        return "danger"
    if cost_status.get("global_usage_rate", 0) >= 0.9:
        return "danger"
    if metrics.get("today_blocked_count", 0) >= 20:
        return "warning"
    if cost_status.get("global_usage_rate", 0) >= 0.7:
        return "warning"
    failed_statuses = {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE}
    if any(row.get("last_health_status") in failed_statuses for row in provider_status.get("rows", [])):
        return "warning"
    if policy_status.get("pending_approvals", 0) > 0:
        return "warning"
    return "normal"


def build_action_recommendations(status, metrics, provider_status, cost_status, policy_status):
    actions = []
    if not status.get("health_ready"):
        actions.append({
            "level": "danger",
            "title": "优先检查运行时健康",
            "body": "请先查看 /health/ready/，确认 Redis 和 PostgreSQL 是否可用。",
            "url": url_or_empty("health_ready"),
        })
    if cost_status.get("global_usage_rate", 0) >= 0.7:
        actions.append({
            "level": "warning",
            "title": "今日全站下载流量偏高",
            "body": "建议检查 DownloadRuntimeSetting，必要时降低公开灰度或收紧大文件限制。",
            "url": cost_status.get("download_settings_url", ""),
        })
    failed_provider = next(
        (
            row for row in provider_status.get("rows", [])
            if row.get("last_health_status") in {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE}
        ),
        None,
    )
    if failed_provider:
        actions.append({
            "level": "warning",
            "title": f"{failed_provider['name']} 最近健康测试异常",
            "body": "建议运行 Provider 健康测试，并在确认前保持 staff-only 或 disabled。",
            "url": failed_provider.get("health_url", ""),
        })
    if policy_status.get("pending_approvals", 0) > 0:
        actions.append({
            "level": "warning",
            "title": "有待审批策略变更",
            "body": "请进入 Policy Control 查看待处理 approval，确认风险后再操作。",
            "url": policy_status.get("policy_control_url", ""),
        })
    if metrics.get("today_blocked_count", 0) >= 10:
        actions.append({
            "level": "warning",
            "title": "今日 blocked / rate_limited 较多",
            "body": "建议查看 Download Logs，确认 Download Cost Guard 是否过严。",
            "url": url_or_empty("download_logs"),
        })
    if not actions:
        actions.append({
            "level": "normal",
            "title": "系统状态正常",
            "body": "暂无明显风险，建议继续观察 Provider、下载流量和 governance decisions。",
            "url": "",
        })
    return {"available": True, "items": actions[:6]}


def build_control_plane_context(request):
    user = getattr(request, "user", None)
    user_key = getattr(user, "pk", "anonymous") or "anonymous"
    cache_key = f"admin:control_plane:v1:{user_key}"
    cached = cache_get(cache_key)
    if isinstance(cached, dict):
        cached["request_id"] = str(getattr(request, "request_id", "") or "")
        return cached

    status = safe_section(
        "system_status",
        lambda: build_system_status(request),
        {"health_ready": False, "database_ready": False, "cache_ready": False, "risk_level": "danger"},
    )
    metrics = safe_section("core_metrics", build_core_metrics, {})
    provider_status = safe_section("provider_status", lambda: build_provider_status(request), {"rows": []})
    cost_status = safe_section("download_cost_status", build_download_cost_status, {"route_distribution": [], "runtime": {}})
    policy_status = safe_section("policy_governance_status", build_policy_governance_status, {"recent_events": []})
    risk_level = derive_risk_level(status, metrics, provider_status, cost_status, policy_status)
    status["risk_level"] = risk_level
    actions = safe_section(
        "action_recommendations",
        lambda: build_action_recommendations(status, metrics, provider_status, cost_status, policy_status),
        {"items": [{"level": "warning", "title": "控制平面部分数据不可用", "body": "请查看服务日志定位失败模块。", "url": ""}]},
    )
    context = {
        "title": "Production Control Plane",
        "version": CONTROL_PLANE_VERSION,
        "request_id": str(getattr(request, "request_id", "") or ""),
        "system_status": status,
        "core_metrics": metrics,
        "provider_status": provider_status,
        "download_cost": cost_status,
        "policy_governance": policy_status,
        "actions": actions,
        "links": {
            "console": url_or_empty("admin_console"),
            "ops_dashboard": url_or_empty("ops_dashboard"),
            "provider_settings": url_or_empty("provider_settings"),
            "provider_health_tests": url_or_empty("provider_health_tests"),
            "rollout_guard": url_or_empty("rollout_guard"),
            "download_logs": url_or_empty("download_logs"),
            "parse_logs": url_or_empty("parse_logs"),
            "provider_audit_logs": url_or_empty("provider_audit_logs"),
        },
    }
    cache_set(cache_key, context)
    return context
