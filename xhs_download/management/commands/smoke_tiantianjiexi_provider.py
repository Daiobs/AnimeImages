import json
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from xhs_download.services.registry import parse_media_page
from xhs_download.services.platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
)
from xhs_download.services.platforms.third_party import GenericThirdPartyAdapter


class Command(BaseCommand):
    help = "Smoke test TianTianJieXi provider without printing credentials or full media URLs."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Source URL to parse.")
        parser.add_argument(
            "--mode",
            choices=("api", "parse-result", "douyin-ultra"),
            default="api",
            help="Smoke mode. Defaults to api.",
        )

    def handle(self, *args, **options):
        source_url = str(options["url"] or "").strip()
        mode = options["mode"]
        if not source_url:
            raise CommandError("--url is required")

        started_at = time.monotonic()
        try:
            if mode == "api":
                output = self.run_api_mode(source_url, started_at)
            elif mode == "parse-result":
                output = self.run_parse_result_mode(source_url, started_at)
            else:
                output = self.run_douyin_ultra_mode(source_url, started_at)
        except Exception as error:
            raise CommandError(type(error).__name__) from error

        self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))

    def duration_ms(self, started_at):
        return int((time.monotonic() - started_at) * 1000)

    def run_api_mode(self, source_url, started_at):
        provider = TianTianJieXiGenericProvider()
        payload = provider.parse(source_url)

        videos = tuple(payload.get("videos") or ()) if isinstance(payload, dict) else ()
        images = tuple(payload.get("images") or ()) if isinstance(payload, dict) else ()
        first_video = videos[0] if videos else {}
        video_url = first_video.get("url") if isinstance(first_video, dict) else ""
        return {
            "enabled": bool(getattr(settings, "TIANTIANJIEXI_PROVIDER_ENABLED", False)),
            "api_configured": bool(
                getattr(settings, "TIANTIANJIEXI_PHONE", "")
                and getattr(settings, "TIANTIANJIEXI_PASSWORD", "")
            ),
            "provider": "tiantianjiexi_generic",
            "asset_count": len(videos) + len(images),
            "video_count": len(videos),
            "image_count": len(images),
            "selected_quality": (
                first_video.get("quality", "") if isinstance(first_video, dict) else ""
            ),
            "selected_video_host": urlparse(video_url or "").hostname or "",
            "duration_ms": self.duration_ms(started_at),
        }

    def run_parse_result_mode(self, source_url, started_at):
        adapter = GenericThirdPartyAdapter()
        rule = adapter.get_rule_for_url(source_url)
        if not rule:
            raise CommandError("UnsupportedPlatform")
        payload = TianTianJieXiGenericProvider().parse(source_url)
        result = TianTianJieXiResultNormalizer(
            platform_id=adapter.platform_id
        ).normalize(
            rule=rule,
            platform_name=rule.display_name,
            source_url=source_url,
            payload=payload,
        )
        first_asset = result.assets[0] if result.assets else None
        return {
            "post_type": result.post_type,
            "asset_count": len(result.assets),
            "first_asset_provider": getattr(first_asset, "provider", ""),
            "selected_quality": getattr(first_asset, "quality_label", ""),
            "provider_manifest_token_present": bool(
                getattr(first_asset, "provider_manifest_token", "")
            ),
            "selected_video_host": urlparse(
                getattr(first_asset, "url", "") or ""
            ).hostname or "",
            "duration_ms": self.duration_ms(started_at),
        }

    def run_douyin_ultra_mode(self, source_url, started_at):
        result = parse_media_page(source_url, prefer_ultra_hd=True)
        metadata = result.metadata or {}
        first_video = result.video_assets[0] if result.video_assets else None
        return {
            "result_platform": result.platform,
            "post_type": result.post_type,
            "asset_count": len(result.assets),
            "selected_provider": metadata.get("selected_provider", ""),
            "source_provider": metadata.get("source_provider", ""),
            "ultra_race_winner": metadata.get("ultra_race_winner", ""),
            "selected_quality": getattr(first_video, "quality_label", ""),
            "selected_video_host": urlparse(
                getattr(first_video, "url", "") or ""
            ).hostname or "",
            "duration_ms": self.duration_ms(started_at),
        }
