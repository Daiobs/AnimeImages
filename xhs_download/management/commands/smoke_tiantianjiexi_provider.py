import json
import time
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from xhs_download.services.platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
)


class Command(BaseCommand):
    help = "Smoke test TianTianJieXi provider without printing credentials or full media URLs."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Source URL to parse.")

    def handle(self, *args, **options):
        source_url = str(options["url"] or "").strip()
        if not source_url:
            raise CommandError("--url is required")

        provider = TianTianJieXiGenericProvider()
        started_at = time.monotonic()
        try:
            payload = provider.parse(source_url)
        except Exception as error:
            raise CommandError(f"{type(error).__name__}: {error}") from error

        duration_ms = int((time.monotonic() - started_at) * 1000)
        videos = tuple(payload.get("videos") or ()) if isinstance(payload, dict) else ()
        images = tuple(payload.get("images") or ()) if isinstance(payload, dict) else ()
        first_video = videos[0] if videos else {}
        video_url = first_video.get("url") if isinstance(first_video, dict) else ""
        output = {
            "enabled": True,
            "api_configured": True,
            "provider": "tiantianjiexi_generic",
            "asset_count": len(videos) + len(images),
            "video_count": len(videos),
            "image_count": len(images),
            "selected_quality": (
                first_video.get("quality", "") if isinstance(first_video, dict) else ""
            ),
            "selected_video_host": urlparse(video_url or "").hostname or "",
            "duration_ms": duration_ms,
        }
        self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
