import logging

from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import UserFeedback
from .services.user_feedback import (
    FEEDBACK_MAX_PAYLOAD_BYTES,
    build_user_feedback_values,
    feedback_rate_limit_result,
)


logger = logging.getLogger(__name__)


def feedback_error(message, *, status=400, retry_after=None):
    response = JsonResponse({"ok": False, "message": message}, status=status)
    if retry_after:
        response["Retry-After"] = str(retry_after)
    return response


@require_POST
def user_feedback_submit_view(request):
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > FEEDBACK_MAX_PAYLOAD_BYTES:
        return feedback_error("反馈内容或图片过大，请精简后重试。", status=413)

    try:
        limit = feedback_rate_limit_result(request)
    except Exception:
        logger.exception(
            "user_feedback_rate_limit_unavailable",
            extra={"event": "user_feedback_rate_limit_unavailable"},
        )
    else:
        if not limit.allowed:
            return feedback_error(
                "反馈提交过于频繁，请稍后再试。",
                status=429,
                retry_after=limit.retry_after,
            )

    try:
        values = build_user_feedback_values(
            request.POST,
            image=request.FILES.get("image"),
            request_id=getattr(request, "request_id", ""),
        )
    except ValueError as exc:
        return feedback_error(str(exc))

    UserFeedback.objects.create(**values)
    return JsonResponse(
        {"ok": True, "message": "感谢反馈，我们已收到。"},
        status=201,
    )
