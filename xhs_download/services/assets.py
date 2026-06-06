from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaAsset:
    media_type: str
    url: str
    platform: str
    preview_url: str = ""
    cover_url: str = ""
    cover_preview_url: str = ""
    filename: str = ""
    ext: str = ""
    quality_label: str = "原始文件"
    size_bytes: int = 0
    headers: dict = field(default_factory=dict)
    media_host_suffixes: tuple[str, ...] = field(default_factory=tuple)

    def to_download_item(self):
        item = {
            "type": self.media_type,
            "url": self.url,
        }
        if self.cover_url:
            item["cover_url"] = self.cover_url
        return item


@dataclass(frozen=True)
class ParseResult:
    platform: str
    post_type: str
    base_filename: str
    assets: tuple[MediaAsset, ...] = field(default_factory=tuple)

    @property
    def image_assets(self):
        return tuple(asset for asset in self.assets if asset.media_type == "image")

    @property
    def video_assets(self):
        return tuple(asset for asset in self.assets if asset.media_type == "video")

    def to_legacy_dict(self):
        return {
            "platform": self.platform,
            "post_type": self.post_type,
            "base_filename": self.base_filename,
            "image_urls": [asset.url for asset in self.image_assets],
            "video_items": [
                {
                    "video_url": asset.url,
                    "cover_url": asset.cover_url or None,
                }
                for asset in self.video_assets
            ],
        }
