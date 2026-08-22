from dataclasses import dataclass, field

from .rich_media import (
    RichMediaContractError,
    RichMediaContractV1,
    validate_parse_result_rich_media,
)


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
    size_is_estimate: bool = False
    headers: dict = field(default_factory=dict)
    media_host_suffixes: tuple[str, ...] = field(default_factory=tuple)
    asset_id: str = ""
    provider: str = ""
    provider_resource_id: str = ""
    provider_cache_key: str = ""
    provider_manifest_token: str = ""

    def to_download_item(self):
        item = {
            "type": self.media_type,
            "url": self.url,
        }
        if self.cover_url:
            item["cover_url"] = self.cover_url
        for key in (
            "asset_id",
            "provider",
            "provider_resource_id",
            "provider_cache_key",
            "provider_manifest_token",
        ):
            value = getattr(self, key, "")
            if value:
                item[key] = value
        return item


@dataclass(frozen=True)
class ParseResult:
    platform: str
    post_type: str
    base_filename: str
    assets: tuple[MediaAsset, ...] = field(default_factory=tuple)
    metadata: dict = field(default_factory=dict)
    rich_media: RichMediaContractV1 | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.rich_media is None:
            return
        if type(self.assets) is not tuple or any(
            type(asset) is not MediaAsset for asset in self.assets
        ):
            raise RichMediaContractError("RICH_MEDIA_RESULT_ASSET_TYPE_INVALID", "assets")
        validate_parse_result_rich_media(
            post_type=self.post_type,
            assets=self.assets,
            rich_media=self.rich_media,
        )

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
