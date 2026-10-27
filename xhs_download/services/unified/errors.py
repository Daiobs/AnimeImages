"""Page errors for a chain that ended without a result."""
from ..common import XhsParseError, XhsProviderTimeoutError
from ..pipeline import FailureKind


class UnifiedParseError(XhsParseError):
    def __init__(self, message, code):
        super().__init__(message)
        self.diagnostic_detail = code


class UnifiedTimeoutError(XhsProviderTimeoutError):
    def __init__(self, message, code):
        super().__init__(message)
        self.diagnostic_detail = code


def failure_error(failure, platform_name):
    code = getattr(failure, "code", "") or "CHAIN_NOT_FINISHED"
    if failure is None or failure.kind is FailureKind.TIMEOUT:
        return UnifiedTimeoutError(f"{platform_name}解析超时，请稍后重试。", code)
    if failure.kind is FailureKind.NOT_FOUND:
        return UnifiedParseError("作品不存在、已删除或当前不可见。", code)
    if failure.kind is FailureKind.UNSUPPORTED:
        return UnifiedParseError(f"暂不支持该{platform_name}链接。", code)
    return UnifiedParseError(f"{platform_name}解析失败，请稍后重试。", code)
