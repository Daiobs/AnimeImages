import logging

from .services.batch_parse.runtime import batch_parse_access_allowed


logger = logging.getLogger(__name__)


def batch_parse_visibility(request):
    try:
        visible = batch_parse_access_allowed(request)
    except Exception as error:
        logger.warning(
            "batch_parse_navigation_visibility_failed",
            extra={"error_type": type(error).__name__},
        )
        visible = False
    return {"batch_parse_visible": bool(visible)}
