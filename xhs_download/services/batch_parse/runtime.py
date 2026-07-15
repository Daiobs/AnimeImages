import logging
import math
from dataclasses import dataclass

from ...models import BatchParseRuntimeSetting, ProviderAccessMode
from ..env_config import (
    runtime_bool as env_runtime_bool,
    runtime_float as env_runtime_float,
    runtime_int as env_runtime_int,
)
from ..provider_access import request_is_in_public_rollout
from ..rate_limits import consume_daily, consume_fixed_window, get_client_identity


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchParseBudget:
    link_count: int
    worker_count: int
    wave_count: int
    configured_total_timeout_seconds: float
    scheduling_timeout_seconds: float
    per_link_timeout_seconds: float
    theoretical_full_duration_seconds: float
    fully_schedulable_capacity: int
    fully_completable_capacity: int
    completion_grace_seconds: float
    max_response_timeout_seconds: float


def calculate_batch_parse_budget(
    *,
    link_count,
    worker_count,
    per_link_timeout_seconds,
    configured_total_timeout_seconds,
    max_scheduling_timeout_seconds=120.0,
    max_per_link_timeout_seconds=30.0,
    completion_grace_seconds=1.0,
):
    """Return deterministic scheduling capacity without reading runtime state."""
    link_count = max(0, int(link_count or 0))
    requested_workers = max(1, int(worker_count or 1))
    worker_count = min(requested_workers, link_count) if link_count else requested_workers
    configured_total_timeout = max(1.0, float(configured_total_timeout_seconds or 0))
    scheduling_timeout = min(
        configured_total_timeout,
        max(1.0, float(max_scheduling_timeout_seconds or 0)),
    )
    per_link_timeout = min(
        max(0.5, float(per_link_timeout_seconds or 0)),
        max(0.5, float(max_per_link_timeout_seconds or 0)),
    )
    completion_grace = min(
        max(0.0, float(completion_grace_seconds or 0)),
        2.0,
    )
    wave_count = math.ceil(link_count / worker_count) if link_count else 0
    theoretical_full_duration = wave_count * per_link_timeout
    response_work_duration = min(
        theoretical_full_duration,
        scheduling_timeout + per_link_timeout,
    )
    schedulable_waves = (
        min(wave_count, max(1, math.ceil(scheduling_timeout / per_link_timeout)))
        if wave_count
        else 0
    )
    completable_waves = (
        min(wave_count, math.floor(scheduling_timeout / per_link_timeout))
        if wave_count
        else 0
    )

    return BatchParseBudget(
        link_count=link_count,
        worker_count=worker_count,
        wave_count=wave_count,
        configured_total_timeout_seconds=configured_total_timeout,
        scheduling_timeout_seconds=scheduling_timeout,
        per_link_timeout_seconds=per_link_timeout,
        theoretical_full_duration_seconds=theoretical_full_duration,
        fully_schedulable_capacity=min(link_count, schedulable_waves * worker_count),
        fully_completable_capacity=min(link_count, completable_waves * worker_count),
        completion_grace_seconds=completion_grace,
        max_response_timeout_seconds=(
            response_work_duration + completion_grace if link_count else 0.0
        ),
    )


def batch_parse_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def load_batch_parse_runtime_setting():
    try:
        return BatchParseRuntimeSetting.load()
    except Exception as error:
        if type(error).__name__ != "DatabaseOperationForbidden":
            logger.warning(
                "batch_parse_runtime_setting_load_failed",
                extra={"error_type": type(error).__name__},
            )
        return None


def batch_parse_runtime_setting_or_none():
    return load_batch_parse_runtime_setting()


def batch_parse_setting_value(setting, field_name, env_name, default):
    value = getattr(setting, field_name, None) if setting is not None else None
    if value is not None and value != "":
        return value
    if isinstance(default, float):
        return env_runtime_float(env_name, default)
    return env_runtime_int(env_name, default)


def batch_parse_access_allowed(request):
    setting = batch_parse_runtime_setting_or_none()
    staff_user = batch_parse_user_is_staff(request)
    if setting is None:
        return staff_user or bool(env_runtime_bool("BATCH_PARSE_PUBLIC_ENABLED", False))
    if setting.access_mode == ProviderAccessMode.DISABLED:
        return False
    if staff_user:
        return setting.access_mode in {
            ProviderAccessMode.STAFF,
            ProviderAccessMode.PUBLIC_GRAY,
            ProviderAccessMode.PUBLIC,
        }
    if setting.access_mode == ProviderAccessMode.PUBLIC:
        return True
    if setting.access_mode == ProviderAccessMode.PUBLIC_GRAY:
        return request_is_in_public_rollout(
            "batch_parse",
            request,
            setting.rollout_percent,
        )
    return False


def batch_parse_max_links(request):
    setting = batch_parse_runtime_setting_or_none()
    global_max = max(
        1,
        int(batch_parse_setting_value(setting, "max_links", "BATCH_PARSE_MAX_LINKS", 20)),
    )
    if batch_parse_user_is_staff(request):
        staff_max = max(
            1,
            int(
                batch_parse_setting_value(
                    setting,
                    "staff_max_links",
                    "BATCH_PARSE_STAFF_MAX_LINKS",
                    global_max,
                )
            ),
        )
        return min(global_max, staff_max)
    public_max = max(
        1,
        int(
            batch_parse_setting_value(
                setting,
                "public_max_links",
                "BATCH_PARSE_PUBLIC_MAX_LINKS",
                3,
            )
        ),
    )
    return min(global_max, public_max)


def batch_parse_total_timeout_seconds():
    setting = batch_parse_runtime_setting_or_none()
    return max(
        1.0,
        float(
            batch_parse_setting_value(
                setting,
                "total_timeout_seconds",
                "BATCH_PARSE_TOTAL_TIMEOUT_SECONDS",
                60.0,
            )
        ),
    )


def batch_parse_parallel_workers():
    setting = batch_parse_runtime_setting_or_none()
    return max(
        1,
        min(
            10,
            int(
                batch_parse_setting_value(
                    setting,
                    "parallel_workers",
                    "BATCH_PARSE_PARALLEL_WORKERS",
                    4,
                )
            ),
        ),
    )


def batch_parse_per_link_timeout_seconds():
    setting = batch_parse_runtime_setting_or_none()
    return max(
        0.5,
        float(
            batch_parse_setting_value(
                setting,
                "per_link_timeout_seconds",
                "BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS",
                8.0,
            )
        ),
    )


def batch_parse_effective_per_link_timeout_seconds(prefer_ultra_hd=False):
    per_link_timeout = batch_parse_per_link_timeout_seconds()
    if not prefer_ultra_hd:
        return per_link_timeout
    return max(
        per_link_timeout,
        env_runtime_float("BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS", 12.0),
    )


def build_batch_parse_budget(link_count, *, prefer_ultra_hd=False):
    return calculate_batch_parse_budget(
        link_count=link_count,
        worker_count=batch_parse_parallel_workers(),
        per_link_timeout_seconds=batch_parse_effective_per_link_timeout_seconds(
            prefer_ultra_hd=prefer_ultra_hd,
        ),
        configured_total_timeout_seconds=batch_parse_total_timeout_seconds(),
        max_scheduling_timeout_seconds=env_runtime_float(
            "BATCH_PARSE_MAX_TOTAL_TIMEOUT_SECONDS",
            120.0,
        ),
        max_per_link_timeout_seconds=env_runtime_float(
            "BATCH_PARSE_MAX_PER_LINK_TIMEOUT_SECONDS",
            30.0,
        ),
        completion_grace_seconds=env_runtime_float(
            "BATCH_PARSE_COMPLETION_GRACE_SECONDS",
            1.0,
        ),
    )


def batch_parse_rate_limit_results(request, link_count):
    if batch_parse_user_is_staff(request):
        return ()

    setting = batch_parse_runtime_setting_or_none()
    identity = get_client_identity(request)
    amount = max(1, int(link_count or 1))
    ip_interval_seconds = int(
        batch_parse_setting_value(
            setting,
            "ip_interval_seconds",
            "BATCH_PARSE_IP_INTERVAL_SECONDS",
            20,
        )
    )
    global_minute_limit = int(
        batch_parse_setting_value(
            setting,
            "global_minute_limit",
            "BATCH_PARSE_GLOBAL_MINUTE_LIMIT",
            30,
        )
    )
    daily_ip_limit = int(
        batch_parse_setting_value(
            setting,
            "daily_ip_limit",
            "BATCH_PARSE_DAILY_IP_LIMIT",
            100,
        )
    )
    return (
        (
            "ip_interval",
            consume_fixed_window(
                "batch-parse-ip-interval",
                identity,
                1 if ip_interval_seconds > 0 else 0,
                max(1, ip_interval_seconds),
                amount=1,
            ),
        ),
        (
            "global_minute",
            consume_fixed_window(
                "batch-parse-global-minute",
                "global",
                global_minute_limit,
                60,
                amount=amount,
            ),
        ),
        (
            "daily_ip",
            consume_daily(
                "batch-parse-daily-ip",
                identity,
                daily_ip_limit,
                amount=amount,
            ),
        ),
    )


def batch_parse_limit_error_message(scope, limit):
    if scope == "daily_ip":
        if limit.limit:
            return f"批量解析今日额度已用尽（{limit.current}/{limit.limit}），请明天再试。"
        return "批量解析今日额度已用尽，请明天再试。"
    if scope == "global_minute":
        retry_after = max(1, min(int(limit.retry_after or 1), 60))
        return f"当前批量解析请求较多，请约 {retry_after} 秒后再试。"
    retry_after = max(1, int(limit.retry_after or 1))
    return f"批量解析请求过于频繁，请约 {retry_after} 秒后再试。"


def batch_parse_public_ultra_hd_allowed(request):
    if batch_parse_user_is_staff(request):
        return True
    setting = batch_parse_runtime_setting_or_none()
    return bool(getattr(setting, "public_ultra_hd_enabled", False))


def batch_parse_public_youtube_merge_allowed(request):
    if batch_parse_user_is_staff(request):
        return True
    setting = batch_parse_runtime_setting_or_none()
    return bool(getattr(setting, "public_youtube_merge_enabled", False))
