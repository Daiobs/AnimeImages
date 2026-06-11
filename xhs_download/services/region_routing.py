import ipaddress
import logging
from functools import lru_cache
from pathlib import Path

from django.conf import settings

from .rate_limits import get_client_ip


logger = logging.getLogger(__name__)
UNKNOWN_COUNTRY_CODES = {"XX"}


def normalize_country_code(value):
    country_code = str(value or "").strip().upper()
    if (
        len(country_code) == 2
        and country_code.isalpha()
        and country_code not in UNKNOWN_COUNTRY_CODES
    ):
        return country_code
    return ""


@lru_cache(maxsize=2)
def get_geoip_reader(database_path):
    import geoip2.database

    return geoip2.database.Reader(str(Path(database_path).expanduser()))


def lookup_geoip_country_code(ip_address):
    database_path = str(
        getattr(settings, "TIKHUB_GEOIP_DATABASE_PATH", "") or ""
    ).strip()
    if not database_path:
        return ""

    try:
        address = ipaddress.ip_address(ip_address)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            return ""

        response = get_geoip_reader(database_path).country(ip_address)
        return normalize_country_code(response.country.iso_code)
    except Exception as error:
        logger.info(
            "tikhub_geoip_lookup_failed",
            extra={
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        return ""


def get_request_country_code(request):
    if getattr(settings, "TIKHUB_TRUST_COUNTRY_HEADER", False):
        header_name = (
            getattr(
                settings,
                "TIKHUB_COUNTRY_HEADER",
                "HTTP_CF_IPCOUNTRY",
            )
            or "HTTP_CF_IPCOUNTRY"
        )
        country_code = normalize_country_code(request.META.get(header_name))
        if country_code:
            return country_code

    country_code = lookup_geoip_country_code(get_client_ip(request))
    if country_code:
        return country_code

    return normalize_country_code(
        getattr(settings, "TIKHUB_DEFAULT_COUNTRY_CODE", "CN")
    ) or "CN"
