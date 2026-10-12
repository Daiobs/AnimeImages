"""Public upstream errors are fixed product copy, never extractor output."""

UPSTREAM_PUBLIC_MESSAGES = {
    "UPSTREAM_AUTH_REQUIRED": "该公开链接的上游解析需要登录；本入口不读取账号凭据。",
    "UPSTREAM_ACCESS_DENIED": "上游拒绝访问该内容，可能存在地区或访问限制。",
    "UPSTREAM_EXECUTION_TIMEOUT": "上游解析超时，请稍后再试。",
    "UPSTREAM_NETWORK_TIMEOUT": "上游网络连接超时，请稍后再试。",
    "UPSTREAM_PLAYLIST_DENIED": "本入口仅接受单条内容，不支持账号或播放列表。",
    "UPSTREAM_DELEGATION_DENIED": "该链接需要额外的上游跳转解析，当前暂不支持。",
    "UPSTREAM_UNSUPPORTED_RESULT": "解析结果没有可交付的图片或完整视频，可能为分离音视频或流媒体。",
    "UPSTREAM_NO_FORMATS": "上游没有返回当前可保存的媒体格式。",
    "UPSTREAM_UNKNOWN_MEDIA_TRACKS": "上游未提供足够的音视频轨道信息，暂时无法确认这是完整视频。",
    "UPSTREAM_SEPARATE_MEDIA_TRACKS": "上游仅提供分离音轨或视频轨，本入口暂不支持合流。",
    "UPSTREAM_STREAMING_MEDIA": "上游仅提供 HLS/DASH 流媒体，本入口暂不支持保存该格式。",
    "UPSTREAM_TLS_ERROR": "连接上游时证书验证失败；安全校验未被关闭。",
    "UPSTREAM_NETWORK_DENIED": "上游访问目标未通过网络安全检查。",
    "UPSTREAM_IDENTITY_MISMATCH": "上游页面与请求的作品身份不一致，未采用该结果。",
    "UPSTREAM_UNAVAILABLE": "当前上游不可用，请稍后重试。",
}
_LEGACY_MESSAGES = {
    "扩展解析失败：" + message: message for message in UPSTREAM_PUBLIC_MESSAGES.values()
}
_LEGACY_MESSAGES["匹配的扩展解析器当前未启用或不可执行。"] = "匹配的扩展解析器当前未启用或不可执行。"
_LEGACY_MESSAGES["扩展解析超时，请稍后再试。"] = UPSTREAM_PUBLIC_MESSAGES["UPSTREAM_EXECUTION_TIMEOUT"]


def public_upstream_error_message(error, *, is_upstream, fallback):
    if not is_upstream:
        return fallback
    code = getattr(error, "error_code", None)
    message = UPSTREAM_PUBLIC_MESSAGES.get(code) if isinstance(code, str) else None
    if message is None:
        message = _LEGACY_MESSAGES.get(str(error))
    return "扩展解析失败：" + (message or UPSTREAM_PUBLIC_MESSAGES["UPSTREAM_UNAVAILABLE"])
