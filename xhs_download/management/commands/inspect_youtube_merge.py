import json
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ...services.common import XhsProviderTimeoutError
from ...services.platforms.ytdlp_generic import run_ytdlp_json
from ...services.youtube_merge import (
    build_youtube_merge_plan,
    get_youtube_merge_health,
)


SOURCE_URL_KEYS = {"webpage_url"}


def redacted_url(url):
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}/[redacted]"


def redact_plan_urls(value, *, show_urls=False, key=""):
    if show_urls:
        return value
    if isinstance(value, dict):
        return {
            item_key: redact_plan_urls(
                item_value,
                show_urls=show_urls,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_plan_urls(item, show_urls=show_urls)
            for item in value
        ]
    if isinstance(value, str) and key == "url":
        return redacted_url(value)
    if isinstance(value, str) and key not in SOURCE_URL_KEYS and value.startswith(("http://", "https://")):
        return redacted_url(value)
    return value


class Command(BaseCommand):
    help = "Inspect the reserved YouTube server-side merge planner."

    def add_arguments(self, parser):
        parser.add_argument(
            "url",
            nargs="?",
            default="",
            help="YouTube URL to inspect.",
        )
        parser.add_argument(
            "--binary",
            default="",
            help="yt-dlp executable command, e.g. '.venv/bin/python -m yt_dlp'",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=0.0,
            help="yt-dlp JSON timeout override in seconds.",
        )
        parser.add_argument(
            "--max-height",
            type=int,
            default=0,
            help="Maximum merged video height to plan for.",
        )
        parser.add_argument(
            "--max-size",
            type=int,
            default=0,
            help="Maximum estimated merged output bytes.",
        )
        parser.add_argument(
            "--health",
            action="store_true",
            help="Print YouTube merge runtime health without parsing a URL.",
        )
        parser.add_argument(
            "--show-urls",
            action="store_true",
            help="Include full direct stream URLs in JSON output.",
        )
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output JSON.")

    def handle(self, *args, **options):
        if options["binary"]:
            settings.YTDLP_BINARY = options["binary"]

        if options["health"]:
            payload = {
                "ok": True,
                "youtube_merge": get_youtube_merge_health(force_version_probe=True),
            }
            if options["as_json"]:
                self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            health = payload["youtube_merge"]
            self.stdout.write(f"provider: {health['provider']}")
            self.stdout.write(f"enabled: {health['enabled']}")
            self.stdout.write(f"ready: {health['ready']}")
            self.stdout.write(f"implemented: {health['implemented']}")
            self.stdout.write(f"ytdlp_ready: {health['ytdlp'].get('ready')}")
            self.stdout.write(f"ffmpeg_ready: {health['ffmpeg'].get('executable_found')}")
            return

        url = options["url"]
        if not url:
            message = "URL is required unless --health is used."
            if options["as_json"]:
                self.stdout.write(json.dumps(
                    {
                        "ok": False,
                        "error_type": "MissingUrl",
                        "error": message,
                    },
                    ensure_ascii=False,
                    indent=2,
                ))
                return
            raise CommandError(message)

        timeout_seconds = float(
            options["timeout"]
            or getattr(settings, "YOUTUBE_MERGE_TIMEOUT_SECONDS", 300.0)
            or 300.0
        )
        started_at = time.monotonic()
        try:
            payload = run_ytdlp_json(
                url,
                binary=getattr(settings, "YTDLP_BINARY", "") or "",
                timeout_seconds=timeout_seconds,
            )
            plan = build_youtube_merge_plan(
                payload,
                max_height=options["max_height"] or None,
                max_file_size_bytes=options["max_size"] or None,
            )
        except Exception as error:
            status = "timeout" if isinstance(error, XhsProviderTimeoutError) else "failed"
            data = {
                "ok": False,
                "status": status,
                "error_type": type(error).__name__,
                "error": str(error),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            }
            if options["as_json"]:
                self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
                return
            raise CommandError(f"{data['error_type']}: {data['error']}") from error

        data = {
            "ok": True,
            "source_url": url,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "plan": redact_plan_urls(
                plan,
                show_urls=bool(options["show_urls"]),
            ),
            "urls_redacted": not bool(options["show_urls"]),
        }
        if options["as_json"]:
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"title: {plan['title'] or '-'}")
        self.stdout.write(f"requires_merge: {plan['requires_merge']}")
        self.stdout.write(f"available: {plan['available']}")
        self.stdout.write(f"reason: {plan['reason']}")
        self.stdout.write(f"format_count: {plan['format_count']}")
        self.stdout.write(f"complete_height: {plan['complete_format'].get('height', 0)}")
        self.stdout.write(f"video_height: {plan['video_format'].get('height', 0)}")
        self.stdout.write(f"audio_format: {plan['audio_format'].get('format_id', '') or '-'}")
        self.stdout.write(f"estimated_size_bytes: {plan['estimated_size_bytes']}")
