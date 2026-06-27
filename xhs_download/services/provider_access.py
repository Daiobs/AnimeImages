import logging

from django.conf import settings

from ..models import ProviderAccessMode, ProviderRuntimeSetting


logger = logging.getLogger(__name__)

PROVIDER_DOUYIN_ULTRA = "douyin_ultra"
PROVIDER_YOUTUBE_MERGE = "youtube_merge"
PROVIDER_THIRD_PARTY = "third_party"


def request_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def load_runtime_setting():
    try:
        return ProviderRuntimeSetting.load()
    except Exception as error:
        if type(error).__name__ != "DatabaseOperationForbidden":
            logger.exception("provider_runtime_setting_load_failed")
        return None


def mode_label(mode):
    return dict(ProviderAccessMode.choices).get(mode, mode)


def default_mode_for_provider(provider_id):
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return (
            ProviderAccessMode.PUBLIC
            if bool(getattr(settings, "ULTRA_HD_PUBLIC_ACCESS_ENABLED", False))
            else ProviderAccessMode.DISABLED
        )
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        if not bool(getattr(settings, "YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED", False)):
            return ProviderAccessMode.DISABLED
        return (
            ProviderAccessMode.STAFF
            if bool(getattr(settings, "YOUTUBE_MERGE_STAFF_ONLY", True))
            else ProviderAccessMode.PUBLIC
        )
    if provider_id == PROVIDER_THIRD_PARTY:
        if not bool(getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False)):
            return ProviderAccessMode.DISABLED
        return (
            ProviderAccessMode.STAFF
            if bool(getattr(settings, "THIRD_PARTY_STAFF_ONLY", True))
            else ProviderAccessMode.PUBLIC
        )
    return ProviderAccessMode.DISABLED


def configured_mode_for_provider(provider_id, runtime_setting=None):
    runtime_setting = runtime_setting or load_runtime_setting()
    if runtime_setting is None:
        return ProviderAccessMode.DEFAULT
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return runtime_setting.douyin_ultra_mode
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return runtime_setting.youtube_merge_mode
    if provider_id == PROVIDER_THIRD_PARTY:
        return runtime_setting.third_party_mode
    return ProviderAccessMode.DEFAULT


def hard_enabled_for_provider(provider_id):
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return bool(
            getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
            and getattr(settings, "KUKUTOOL_PROVIDER_ENABLED", False)
        )
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return bool(getattr(settings, "YOUTUBE_MERGE_PROVIDER_ENABLED", False))
    if provider_id == PROVIDER_THIRD_PARTY:
        return bool(getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False))
    return False


def effective_mode_for_provider(provider_id, runtime_setting=None):
    configured_mode = configured_mode_for_provider(provider_id, runtime_setting)
    if configured_mode == ProviderAccessMode.DEFAULT:
        return default_mode_for_provider(provider_id)
    return configured_mode


def provider_access_summary(provider_id, request=None, runtime_setting=None):
    configured_mode = configured_mode_for_provider(provider_id, runtime_setting)
    default_mode = default_mode_for_provider(provider_id)
    effective_mode = (
        default_mode
        if configured_mode == ProviderAccessMode.DEFAULT
        else configured_mode
    )
    hard_enabled = hard_enabled_for_provider(provider_id)
    staff_user = request_user_is_staff(request) if request is not None else False
    public_allowed = hard_enabled and effective_mode == ProviderAccessMode.PUBLIC
    staff_allowed = hard_enabled and effective_mode in {
        ProviderAccessMode.PUBLIC,
        ProviderAccessMode.STAFF,
    }
    request_allowed = public_allowed or (
        hard_enabled
        and effective_mode == ProviderAccessMode.STAFF
        and staff_user
    )
    return {
        "provider_id": provider_id,
        "hard_enabled": hard_enabled,
        "configured_mode": configured_mode,
        "configured_label": mode_label(configured_mode),
        "default_mode": default_mode,
        "default_label": mode_label(default_mode),
        "effective_mode": effective_mode,
        "effective_label": mode_label(effective_mode),
        "public_allowed": public_allowed,
        "staff_allowed": staff_allowed,
        "request_allowed": request_allowed,
        "request_is_staff": staff_user,
    }


def provider_is_allowed_for_request(provider_id, request=None):
    return provider_access_summary(provider_id, request)["request_allowed"]
