from .platforms.xiaohongshu import (
    XiaohongshuAdapter,
    extract_video_urls,
    normalize_video_url,
    normalize_xhs_image_url,
    normalize_xhs_preview_image_url,
)


_xhs_adapter = XiaohongshuAdapter()


def parse_xhs_html(html):
    return _xhs_adapter.parse_html(html).to_legacy_dict()


def parse_xhs_page(url):
    return _xhs_adapter.parse_url(url).to_legacy_dict()
