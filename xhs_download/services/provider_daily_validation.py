from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMessage, get_connection
from django.db.models import Count
from django.utils import timezone

from ..models import (
    ProviderHealthNotificationDeliveryStatus,
    ProviderHealthNotificationEvent,
    ProviderHealthTestRun,
    ProviderHealthTestSetting,
    ProviderHealthTestStatus,
    ProviderValidationNotificationStatus,
    ProviderValidationSuiteRun,
    ProviderValidationSuiteStatus,
    ProviderValidationSuiteTrigger,
)
from .env_config import (
    runtime_bool,
    runtime_float,
    runtime_int,
    runtime_list,
    runtime_str,
)
from .provider_health_tests import (
    ProviderHealthExecutionContext,
    execute_provider_health_test,
    provider_health_test_by_key,
    provider_health_test_keys,
    record_unexecuted_test,
    validate_daily_test_keys,
    validate_daily_videodl_backend_id,
)


LOCK_KEY = "provider-health:daily-validation:v1:lock"
LOCK_GRACE_SECONDS = 60
UNHEALTHY_STATUSES = frozenset(
    {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE}
)


class DailyValidationConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class NotificationEvent:
    run: ProviderHealthTestRun
    event: str
    consecutive_unhealthy: int


@dataclass(frozen=True)
class DailyValidationOutcome:
    suite: ProviderValidationSuiteRun | None
    selected_test_keys: tuple[str, ...]
    dry_run: bool = False
    retention_matched: int = 0
    retention_deleted: int = 0
    stale_recovered: int = 0

    def as_safe_dict(self) -> dict[str, Any]:
        suite = self.suite
        email_config = email_delivery_configuration()
        return {
            "status": "dry_run" if self.dry_run else (suite.status if suite else "skipped"),
            "request_id": str(getattr(suite, "request_id", "") or ""),
            "selected_test_keys": list(self.selected_test_keys),
            "total_count": int(getattr(suite, "total_count", 0) or 0),
            "success_count": int(getattr(suite, "success_count", 0) or 0),
            "failed_count": int(getattr(suite, "failed_count", 0) or 0),
            "skipped_count": int(getattr(suite, "skipped_count", 0) or 0),
            "unavailable_count": int(getattr(suite, "unavailable_count", 0) or 0),
            "notification_status": str(
                getattr(suite, "notification_status", ProviderValidationNotificationStatus.DISABLED)
            ),
            "email_delivery_env_enabled": email_config["env_enabled"],
            "email_delivery_configured": email_config["configured"],
            "retention_matched": self.retention_matched,
            "retention_deleted": self.retention_deleted,
            "stale_recovered": self.stale_recovered,
        }


def total_timeout_seconds() -> int:
    value = runtime_int("PROVIDER_DAILY_VALIDATION_TOTAL_TIMEOUT_SECONDS", 300)
    if not 30 <= value <= 900:
        raise DailyValidationConfigurationError("INVALID_TOTAL_TIMEOUT")
    return value


def email_delivery_configuration() -> dict[str, Any]:
    recipients = tuple(
        str(value or "").strip()
        for value in runtime_list("PROVIDER_VALIDATION_EMAIL_RECIPIENTS", ())
        if str(value or "").strip()
    )
    env_enabled = runtime_bool("PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED", False)
    backend = runtime_str(
        "DJANGO_EMAIL_BACKEND",
        "django.core.mail.backends.console.EmailBackend",
    )
    from_email = runtime_str("DJANGO_DEFAULT_FROM_EMAIL", "")
    use_tls = runtime_bool("DJANGO_EMAIL_USE_TLS", False)
    use_ssl = runtime_bool("DJANGO_EMAIL_USE_SSL", False)
    smtp_host_ready = not backend.endswith("smtp.EmailBackend") or bool(
        runtime_str("DJANGO_EMAIL_HOST", "")
    )
    production_backend_ready = not (
        getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG)
        and (
            backend.endswith("console.EmailBackend")
            or backend.endswith("dummy.EmailBackend")
        )
    )
    backend_valid = bool(
        backend
        and from_email
        and not (use_tls and use_ssl)
        and smtp_host_ready
        and production_backend_ready
    )
    return {
        "env_enabled": env_enabled,
        "recipients": recipients,
        "recipient_count": len(recipients),
        "backend": backend,
        "configured": bool(env_enabled and recipients and backend_valid),
    }


def _provider_validation_email_connection(backend):
    options = {}
    if str(backend or "").endswith("smtp.EmailBackend"):
        options = {
            "host": runtime_str("DJANGO_EMAIL_HOST", ""),
            "port": runtime_int("DJANGO_EMAIL_PORT", 25),
            "username": runtime_str("DJANGO_EMAIL_HOST_USER", ""),
            "password": runtime_str("DJANGO_EMAIL_HOST_PASSWORD", ""),
            "use_tls": runtime_bool("DJANGO_EMAIL_USE_TLS", False),
            "use_ssl": runtime_bool("DJANGO_EMAIL_USE_SSL", False),
            "timeout": runtime_float("DJANGO_EMAIL_TIMEOUT_SECONDS", 10.0),
        }
    return get_connection(
        backend=backend,
        fail_silently=False,
        **options,
    )


def selected_test_keys(setting, override=None) -> tuple[str, ...]:
    raw = list(setting.daily_test_keys if override is None else override)
    try:
        return validate_daily_test_keys(raw)
    except ValueError as error:
        raise DailyValidationConfigurationError(str(error)) from error


def validate_daily_setting(setting) -> None:
    bounded_values = (
        (setting.failure_threshold, 1, 10, "INVALID_FAILURE_THRESHOLD"),
        (
            setting.notification_cooldown_hours,
            1,
            168,
            "INVALID_NOTIFICATION_COOLDOWN",
        ),
        (setting.retention_days, 7, 365, "INVALID_RETENTION_DAYS"),
    )
    for value, minimum, maximum, error_code in bounded_values:
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            raise DailyValidationConfigurationError(error_code) from None
        if not minimum <= numeric <= maximum:
            raise DailyValidationConfigurationError(error_code)
    if not validate_daily_videodl_backend_id(setting.videodl_backend_id):
        raise DailyValidationConfigurationError("INVALID_VIDEODL_BACKEND")


def recover_stale_suites(*, now=None, timeout=None) -> int:
    now = now or timezone.now()
    timeout = total_timeout_seconds() if timeout is None else int(timeout)
    cutoff = now - timezone.timedelta(seconds=timeout + LOCK_GRACE_SECONDS)
    stale = ProviderValidationSuiteRun.objects.filter(
        status=ProviderValidationSuiteStatus.RUNNING,
        started_at__lt=cutoff,
    )
    return stale.update(
        status=ProviderValidationSuiteStatus.FAILED,
        error_code="STALE_SUITE_RECOVERED",
        finished_at=now,
        duration_ms=(timeout + LOCK_GRACE_SECONDS) * 1000,
        notification_status=ProviderValidationNotificationStatus.NOT_NEEDED,
    )


def _create_skipped_suite(*, request_id, trigger, selected, actor_id, actor_username, error_code):
    now = timezone.now()
    return ProviderValidationSuiteRun.objects.create(
        request_id=request_id,
        trigger=trigger,
        status=ProviderValidationSuiteStatus.SKIPPED,
        selected_test_keys=list(selected),
        total_count=len(selected),
        skipped_count=len(selected),
        started_at=now,
        finished_at=now,
        duration_ms=0,
        error_code=error_code,
        notification_status=ProviderValidationNotificationStatus.DISABLED,
        actor_id=actor_id,
        actor_username=str(actor_username or "")[:150],
    )


def _status_counts(runs: Iterable[ProviderHealthTestRun]) -> dict[str, int]:
    counts = {
        ProviderHealthTestStatus.SUCCESS: 0,
        ProviderHealthTestStatus.FAILED: 0,
        ProviderHealthTestStatus.SKIPPED: 0,
        ProviderHealthTestStatus.UNAVAILABLE: 0,
    }
    for run in runs:
        if run.status in counts:
            counts[run.status] += 1
    return counts


def _consecutive_unhealthy(run: ProviderHealthTestRun) -> int:
    if run.status not in UNHEALTHY_STATUSES:
        return 0
    count = 1
    history = ProviderHealthTestRun.objects.filter(
        suite_run__isnull=False,
        test_key=run.test_key,
        pk__lt=run.pk,
    ).order_by("-created_at")
    for previous in history[:400]:
        if previous.status == ProviderHealthTestStatus.SKIPPED:
            continue
        if previous.status in UNHEALTHY_STATUSES:
            count += 1
            continue
        break
    return count


def _latest_sent_notification(run: ProviderHealthTestRun):
    return (
        ProviderHealthTestRun.objects.filter(
            suite_run__isnull=False,
            test_key=run.test_key,
            pk__lt=run.pk,
            notification_delivery_status=ProviderHealthNotificationDeliveryStatus.SENT,
        )
        .exclude(notification_event=ProviderHealthNotificationEvent.NONE)
        .order_by("-notified_at", "-created_at")
        .first()
    )


def plan_notification_events(suite, runs, setting, *, now=None) -> tuple[NotificationEvent, ...]:
    del suite
    now = now or timezone.now()
    events = []
    threshold = max(1, int(setting.failure_threshold or 2))
    cooldown = timezone.timedelta(
        hours=max(1, int(setting.notification_cooldown_hours or 24))
    )
    for run in runs:
        event = ProviderHealthNotificationEvent.NONE
        consecutive = _consecutive_unhealthy(run)
        latest_sent = _latest_sent_notification(run)
        if run.status in UNHEALTHY_STATUSES:
            if latest_sent is None:
                if consecutive >= threshold:
                    event = ProviderHealthNotificationEvent.FAILURE
            elif latest_sent.notification_event in {
                ProviderHealthNotificationEvent.FAILURE,
                ProviderHealthNotificationEvent.REMINDER,
            }:
                if latest_sent.result_fingerprint != run.result_fingerprint:
                    event = ProviderHealthNotificationEvent.FAILURE
                elif not latest_sent.notified_at or now - latest_sent.notified_at >= cooldown:
                    event = ProviderHealthNotificationEvent.REMINDER
            elif latest_sent.notification_event == ProviderHealthNotificationEvent.RECOVERY:
                if consecutive >= threshold:
                    event = ProviderHealthNotificationEvent.FAILURE
        elif (
            run.status == ProviderHealthTestStatus.SUCCESS
            and setting.notify_recovery
            and latest_sent is not None
            and latest_sent.notification_event
            in {ProviderHealthNotificationEvent.FAILURE, ProviderHealthNotificationEvent.REMINDER}
        ):
            event = ProviderHealthNotificationEvent.RECOVERY
        if event != ProviderHealthNotificationEvent.NONE:
            run.notification_event = event
            run.save(update_fields=("notification_event",))
            events.append(
                NotificationEvent(
                    run=run,
                    event=event,
                    consecutive_unhealthy=consecutive,
                )
            )
    return tuple(events)


def _notification_subject(events) -> str:
    unhealthy = sum(
        event.event in {ProviderHealthNotificationEvent.FAILURE, ProviderHealthNotificationEvent.REMINDER}
        for event in events
    )
    recovered = sum(event.event == ProviderHealthNotificationEvent.RECOVERY for event in events)
    if unhealthy and recovered:
        subject = f"[AnimeImages] Provider validation changes: {len(events)} checks"
    elif unhealthy:
        subject = f"[AnimeImages] Provider validation degraded: {unhealthy} checks"
    else:
        subject = f"[AnimeImages] Provider validation recovered: {recovered} checks"
    return subject[:180]


def _notification_body(suite, events) -> str:
    names = {
        item["key"]: item["name"]
        for item in (provider_health_test_by_key(key) for key in provider_health_test_keys())
        if item
    }
    lines = [
        "AnimeImages Provider validation",
        f"request_id: {suite.request_id}",
        f"started_at: {suite.started_at.isoformat()}",
        f"suite_status: {suite.status}",
        (
            "counts: "
            f"success={suite.success_count} failed={suite.failed_count} "
            f"skipped={suite.skipped_count} unavailable={suite.unavailable_count}"
        ),
        "",
    ]
    for event in events:
        run = event.run
        lines.extend(
            (
                f"test_key: {run.test_key}",
                f"display_name: {names.get(run.test_key, run.test_key)}",
                f"provider: {run.provider}",
                f"status: {run.status}",
                f"error_code: {run.error_code or '-'}",
                f"consecutive_unhealthy: {event.consecutive_unhealthy}",
                f"event: {event.event}",
                f"test_host: {run.test_url_host or '-'}",
                "",
            )
        )
    report_url = runtime_str("PROVIDER_VALIDATION_ADMIN_REPORT_URL", "")
    parsed_report_url = urlparse(report_url) if report_url else None
    if (
        parsed_report_url
        and parsed_report_url.scheme in {"http", "https"}
        and parsed_report_url.hostname
        and not parsed_report_url.username
        and not parsed_report_url.password
        and not parsed_report_url.query
        and not parsed_report_url.fragment
    ):
        lines.append(f"admin_report: {report_url}")
    return "\n".join(lines)[:12000]


def deliver_notifications(suite, events, setting, *, no_email=False, now=None) -> str:
    now = now or timezone.now()
    if not events:
        return ProviderValidationNotificationStatus.NOT_NEEDED
    config = email_delivery_configuration()
    gate_open = bool(
        not no_email
        and setting.email_notifications_enabled
        and config["configured"]
    )
    if not gate_open:
        ProviderHealthTestRun.objects.filter(pk__in=[event.run.pk for event in events]).update(
            notification_delivery_status=ProviderHealthNotificationDeliveryStatus.DISABLED
        )
        return ProviderValidationNotificationStatus.DISABLED
    try:
        sent_count = EmailMessage(
            subject=_notification_subject(events),
            body=_notification_body(suite, events),
            from_email=runtime_str("DJANGO_DEFAULT_FROM_EMAIL", ""),
            to=list(config["recipients"]),
            connection=_provider_validation_email_connection(config["backend"]),
        ).send(fail_silently=False)
        if sent_count != 1:
            raise RuntimeError("EMAIL_DELIVERY_NOT_CONFIRMED")
    except Exception:
        ProviderHealthTestRun.objects.filter(pk__in=[event.run.pk for event in events]).update(
            notification_delivery_status=ProviderHealthNotificationDeliveryStatus.FAILED
        )
        return ProviderValidationNotificationStatus.FAILED
    ProviderHealthTestRun.objects.filter(pk__in=[event.run.pk for event in events]).update(
        notification_delivery_status=ProviderHealthNotificationDeliveryStatus.SENT,
        notified_at=now,
    )
    return ProviderValidationNotificationStatus.SENT


def prune_validation_history(setting, *, current_suite=None, now=None) -> tuple[int, int]:
    now = now or timezone.now()
    cutoff = now - timezone.timedelta(days=max(7, int(setting.retention_days or 90)))
    queryset = ProviderValidationSuiteRun.objects.filter(created_at__lt=cutoff).exclude(
        status=ProviderValidationSuiteStatus.RUNNING
    )
    if current_suite is not None:
        queryset = queryset.exclude(pk=current_suite.pk)
    matched = queryset.count()
    deleted, details = queryset.delete()
    deleted_suites = int(
        details.get("xhs_download.ProviderValidationSuiteRun", 0)
    )
    return matched, deleted_suites


def _complete_suite(
    suite,
    runs,
    *,
    started_monotonic,
    deadline_exceeded=False,
    clock=time.monotonic,
):
    counts = _status_counts(runs)
    degraded = bool(
        counts[ProviderHealthTestStatus.FAILED]
        or counts[ProviderHealthTestStatus.UNAVAILABLE]
        or deadline_exceeded
    )
    suite.status = (
        ProviderValidationSuiteStatus.DEGRADED
        if degraded
        else ProviderValidationSuiteStatus.SUCCESS
    )
    suite.total_count = len(runs)
    suite.success_count = counts[ProviderHealthTestStatus.SUCCESS]
    suite.failed_count = counts[ProviderHealthTestStatus.FAILED]
    suite.skipped_count = counts[ProviderHealthTestStatus.SKIPPED]
    suite.unavailable_count = counts[ProviderHealthTestStatus.UNAVAILABLE]
    suite.duration_ms = max(0, round((clock() - started_monotonic) * 1000))
    suite.finished_at = timezone.now()
    if deadline_exceeded:
        suite.error_code = "SUITE_DEADLINE_EXCEEDED"
    suite.save(
        update_fields=(
            "status",
            "total_count",
            "success_count",
            "failed_count",
            "skipped_count",
            "unavailable_count",
            "duration_ms",
            "finished_at",
            "error_code",
        )
    )


def run_daily_provider_validation(
    *,
    force=False,
    no_email=False,
    test_keys_override=None,
    dry_run=False,
    trigger=ProviderValidationSuiteTrigger.SCHEDULED,
    actor_id=None,
    actor_username="",
    execute_test: Callable = execute_provider_health_test,
    clock: Callable[[], float] = time.monotonic,
) -> DailyValidationOutcome:
    if dry_run:
        setting = (
            ProviderHealthTestSetting.objects.filter(singleton_key=1).first()
            or ProviderHealthTestSetting()
        )
    else:
        setting = ProviderHealthTestSetting.load()
    selected = selected_test_keys(setting, test_keys_override)
    validate_daily_setting(setting)
    email_delivery_configuration()
    total_timeout = total_timeout_seconds()
    if dry_run:
        return DailyValidationOutcome(
            suite=None,
            selected_test_keys=selected,
            dry_run=True,
        )

    request_id = uuid4().hex
    stale_recovered = recover_stale_suites(timeout=total_timeout)
    if not setting.daily_enabled and not force:
        suite = _create_skipped_suite(
            request_id=request_id,
            trigger=trigger,
            selected=selected,
            actor_id=actor_id,
            actor_username=actor_username,
            error_code="DAILY_VALIDATION_DISABLED",
        )
        return DailyValidationOutcome(
            suite=suite,
            selected_test_keys=selected,
            stale_recovered=stale_recovered,
        )
    if not selected:
        suite = _create_skipped_suite(
            request_id=request_id,
            trigger=trigger,
            selected=selected,
            actor_id=actor_id,
            actor_username=actor_username,
            error_code="NO_TESTS_SELECTED",
        )
        return DailyValidationOutcome(
            suite=suite,
            selected_test_keys=selected,
            stale_recovered=stale_recovered,
        )

    lock_value = uuid4().hex
    if not cache.add(LOCK_KEY, lock_value, timeout=total_timeout + LOCK_GRACE_SECONDS):
        suite = _create_skipped_suite(
            request_id=request_id,
            trigger=trigger,
            selected=selected,
            actor_id=actor_id,
            actor_username=actor_username,
            error_code="SINGLE_FLIGHT_ACTIVE",
        )
        return DailyValidationOutcome(
            suite=suite,
            selected_test_keys=selected,
            stale_recovered=stale_recovered,
        )

    suite = None
    started_monotonic = clock()
    retention_matched = 0
    retention_deleted = 0
    try:
        suite = ProviderValidationSuiteRun.objects.create(
            request_id=request_id,
            trigger=trigger,
            status=ProviderValidationSuiteStatus.RUNNING,
            selected_test_keys=list(selected),
            actor_id=actor_id,
            actor_username=str(actor_username or "")[:150],
        )
        context = ProviderHealthExecutionContext(
            request_id=request_id,
            actor_id=actor_id,
            actor_username=str(actor_username or "")[:150],
            is_staff=True,
            trigger=trigger,
            suite_run=suite,
            test_setting=setting,
        )
        deadline = started_monotonic + total_timeout
        runs = []
        deadline_exceeded = False
        for key in selected:
            item = provider_health_test_by_key(key)
            if item is None:
                raise DailyValidationConfigurationError("UNKNOWN_DAILY_TEST_KEY")
            if clock() >= deadline:
                deadline_exceeded = True
                runs.append(
                    record_unexecuted_test(
                        context,
                        item,
                        error_code="SUITE_DEADLINE_EXCEEDED",
                    )
                )
                continue
            runs.append(execute_test(context, item))
        _complete_suite(
            suite,
            runs,
            started_monotonic=started_monotonic,
            deadline_exceeded=deadline_exceeded,
            clock=clock,
        )
        events = plan_notification_events(suite, runs, setting)
        suite.notification_event_count = len(events)
        suite.notification_status = deliver_notifications(
            suite,
            events,
            setting,
            no_email=no_email,
        )
        suite.save(
            update_fields=("notification_event_count", "notification_status")
        )
        retention_matched, retention_deleted = prune_validation_history(
            setting,
            current_suite=suite,
        )
    except Exception:
        if suite is not None:
            suite.status = ProviderValidationSuiteStatus.FAILED
            suite.error_code = "SUITE_ORCHESTRATION_FAILED"
            suite.finished_at = timezone.now()
            suite.duration_ms = max(
                0, round((clock() - started_monotonic) * 1000)
            )
            suite.save(
                update_fields=("status", "error_code", "finished_at", "duration_ms")
            )
        raise
    finally:
        if cache.get(LOCK_KEY) == lock_value:
            cache.delete(LOCK_KEY)
    return DailyValidationOutcome(
        suite=suite,
        selected_test_keys=selected,
        retention_matched=retention_matched,
        retention_deleted=retention_deleted,
        stale_recovered=stale_recovered,
    )


def test_daily_state(test_key, *, selected=False) -> dict[str, Any]:
    runs = ProviderHealthTestRun.objects.filter(
        suite_run__isnull=False,
        test_key=test_key,
    ).order_by("-created_at")
    latest = runs.first()
    last_success = runs.filter(status=ProviderHealthTestStatus.SUCCESS).first()
    last_unhealthy = runs.filter(status__in=UNHEALTHY_STATUSES).first()
    consecutive = _consecutive_unhealthy(latest) if latest else 0
    return {
        "test_key": test_key,
        "display_name": (provider_health_test_by_key(test_key) or {}).get("name", test_key),
        "selected": selected,
        "latest": latest,
        "last_success_at": getattr(last_success, "created_at", None),
        "last_unhealthy_at": getattr(last_unhealthy, "created_at", None),
        "consecutive_unhealthy": consecutive,
    }


def build_daily_validation_report(setting=None) -> dict[str, Any]:
    setting = setting or ProviderHealthTestSetting.load()
    try:
        selected = selected_test_keys(setting)
    except DailyValidationConfigurationError:
        selected = ()
    states = [
        test_daily_state(key, selected=key in selected)
        for key in provider_health_test_keys()
        if key in selected
    ]
    latest_scheduled = ProviderValidationSuiteRun.objects.filter(
        trigger=ProviderValidationSuiteTrigger.SCHEDULED
    ).first()
    latest_notification = (
        ProviderValidationSuiteRun.objects.exclude(
            notification_status=ProviderValidationNotificationStatus.NOT_NEEDED
        ).first()
    )
    now = timezone.now()
    trends = {}
    for days in (7, 30):
        queryset = ProviderValidationSuiteRun.objects.filter(
            created_at__gte=now - timezone.timedelta(days=days)
        )
        trends[days] = {
            row["status"]: row["count"]
            for row in queryset.values("status").annotate(count=Count("id"))
        }
    return {
        "selected_test_keys": selected,
        "selected_test_count": len(selected),
        "test_states": states,
        "active_failing_count": sum(
            bool(state["latest"] and state["latest"].status in UNHEALTHY_STATUSES)
            for state in states
        ),
        "latest_scheduled_suite": latest_scheduled,
        "latest_notification_suite": latest_notification,
        "email_delivery": email_delivery_configuration(),
        "trends": trends,
        "trend_7": trends[7],
        "trend_30": trends[30],
    }


__all__ = [
    "DailyValidationConfigurationError",
    "DailyValidationOutcome",
    "build_daily_validation_report",
    "deliver_notifications",
    "email_delivery_configuration",
    "plan_notification_events",
    "prune_validation_history",
    "recover_stale_suites",
    "run_daily_provider_validation",
    "selected_test_keys",
    "total_timeout_seconds",
    "validate_daily_setting",
]
