"""Adapter lookup and legacy Provider aliases for the observation-only runner."""
from .bilibili import BilibiliMapper
from .douyin import DouyinMapper
from .gallerydl import GalleryDlMapper
from .kukutool import KukutoolAdapter
from .parsehub import ParseHubMapper, ParseHubXhsMapper
from .tiantian import TianTianAdapter
from .videodl import VideodlMapper
from .xiaohongshu import XiaohongshuMapper
from .ytdlp import YtDlpAdapter


MAPPERS = {mapper.name: mapper for mapper in (
    BilibiliMapper, DouyinMapper, GalleryDlMapper, KukutoolAdapter,
    ParseHubMapper, ParseHubXhsMapper, TianTianAdapter, VideodlMapper,
    XiaohongshuMapper, YtDlpAdapter,
)}
ALIASES = {
    "tiantianjiexi": "tiantianjiexi_generic", "kukutool": "kukutool_generic",
    "youtube_merge": "yt_dlp_generic", "yt_dlp_merge": "yt_dlp_generic",
    "mobile_feed": "douyin_native", "mobile_share": "douyin_native",
}
NATIVE = {"bilibili": "bilibili_native", "douyin": "douyin_native", "xiaohongshu": "xhs_native"}


def provider_key(value, platform=""):
    value = ALIASES.get(value, value)
    if value in MAPPERS:
        return value
    if value in ("", "native"):
        return NATIVE.get(platform, "unknown")
    return "unknown"


def mapper_for(observation):
    factory = MAPPERS.get(provider_key(observation.provider))
    if factory is VideodlMapper:
        return factory(backend_id=observation.metadata.get("backend_id"),
                       backend_category=observation.metadata.get("backend_category"))
    return factory() if factory else None
