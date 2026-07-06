import os

from django.conf import settings


def runtime_str(name, default=""):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name, default)
    return str(value if value is not None else default).strip()


def runtime_bool(name, default=False):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def runtime_int(name, default=0):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def runtime_float(name, default=0.0):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def runtime_list(name, default=()):
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    if value is None or value == "":
        return tuple(default)
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return tuple(value or default)
