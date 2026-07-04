import logging

from django.db import DatabaseError


logger = logging.getLogger(__name__)


def load_provider_runtime_setting_readonly():
    try:
        from ..models import ProviderRuntimeSetting

        return ProviderRuntimeSetting.objects.filter(singleton_key=1).first()
    except DatabaseError:
        logger.debug("provider_runtime_config_unavailable")
        return None
    except Exception as error:
        if type(error).__name__ == "DatabaseOperationForbidden":
            return None
        logger.exception("provider_runtime_config_load_failed")
        return None


def runtime_float(field_name, fallback, *, minimum=None, maximum=None, setting=None):
    if setting is None:
        setting = load_provider_runtime_setting_readonly()
    value = getattr(setting, field_name, None) if setting is not None else None
    if value is None:
        value = fallback
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(fallback)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value
