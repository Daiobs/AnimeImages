import json
from pathlib import Path
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ...services.common import XhsProviderTimeoutError
from ...services.platforms.ytdlp_generic import run_ytdlp_json
from ...services.youtube_merge import (
    build_youtube_merge_plan,
    build_youtube_merge_parse_result,
    download_youtube_merge_to_temp_path,
    get_youtube_merge_health,
)
from ...services.platforms.ytdlp_merge import default_max_height


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


def build_smoke_summary(plan, *, duration_ms):
    frontend_budget_seconds = float(
        getattr(settings, "YOUTUBE_FRONTEND_TIMEOUT_SECONDS", 8.0) or 8.0
    )
    frontend_budget_ms = int(frontend_budget_seconds * 1000)
    selected_height = int((plan.get("video_format") or {}).get("height") or 0)
    complete_height = int((plan.get("complete_format") or {}).get("height") or 0)
    merge_candidate_available = bool(plan.get("available") and plan.get("requires_merge"))
    issues = []

    if not merge_candidate_available:
        issues.append(plan.get("reason") or "merge_candidate_unavailable")
    if duration_ms > frontend_budget_ms:
        issues.append("parse_exceeds_frontend_budget")
    if not plan.get("estimated_size_within_limit"):
        issues.append("estimated_size_exceeds_limit")
    if selected_height < 1080:
        issues.append("selected_height_below_1080p")

    return {
        "parse_duration_ms": duration_ms,
        "frontend_budget_ms": frontend_budget_ms,
        "within_frontend_budget": duration_ms <= frontend_budget_ms,
        "merge_candidate_available": merge_candidate_available,
        "selected_height": selected_height,
        "single_file_height": complete_height,
        "estimated_size_bytes": int(plan.get("estimated_size_bytes") or 0),
        "estimated_size_within_limit": bool(plan.get("estimated_size_within_limit")),
        "issues": tuple(issues),
        "recommended_rollout": "staff_only" if not issues else "hold",
    }


def build_download_queue_recommendation(*, duration_ms):
    frontend_budget_seconds = float(
        getattr(settings, "YOUTUBE_FRONTEND_TIMEOUT_SECONDS", 8.0) or 8.0
    )
    frontend_budget_ms = int(frontend_budget_seconds * 1000)
    queue_recommended = duration_ms > frontend_budget_ms
    return {
        "frontend_budget_ms": frontend_budget_ms,
        "sync_acceptable": not queue_recommended,
        "queue_recommended": queue_recommended,
        "reason": (
            "download_exceeds_frontend_budget"
            if queue_recommended
            else "download_within_frontend_budget"
        ),
    }


def remove_temp_download(path):
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


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
            "--download-smoke",
            action="store_true",
            help=(
                "Run the actual yt-dlp + ffmpeg merge download after planning. "
                "This can download large files and is intended for local/staff smoke tests."
            ),
        )
        parser.add_argument(
            "--keep-file",
            action="store_true",
            help="Keep the merged smoke-test file and include its path in output.",
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
            effective_max_height = int(options["max_height"] or default_max_height())
            plan = build_youtube_merge_plan(
                payload,
                max_height=effective_max_height,
                max_file_size_bytes=options["max_size"] or None,
            )
            planning_duration_ms = int((time.monotonic() - started_at) * 1000)
            download_smoke = None
            if options["download_smoke"]:
                if not plan.get("available") or not plan.get("requires_merge"):
                    raise CommandError(
                        f"YouTube merge candidate is not available: {plan.get('reason')}"
                    )
                download_started_at = time.monotonic()
                original_max_height = getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320)
                original_max_size = getattr(
                    settings,
                    "YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES",
                    500 * 1024 * 1024,
                )
                try:
                    settings.YOUTUBE_MERGE_MAX_HEIGHT = effective_max_height
                    if options["max_size"]:
                        settings.YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES = options["max_size"]
                    parse_result = build_youtube_merge_parse_result(url, payload)
                    asset = parse_result.video_assets[0]
                    temp_result = download_youtube_merge_to_temp_path(asset.to_download_item())
                finally:
                    settings.YOUTUBE_MERGE_MAX_HEIGHT = original_max_height
                    settings.YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES = original_max_size
                download_duration_ms = int((time.monotonic() - download_started_at) * 1000)
                download_smoke = {
                    "ok": True,
                    "duration_ms": download_duration_ms,
                    "size_bytes": int(temp_result.get("size") or 0),
                    "content_type": temp_result.get("content_type") or "",
                    "kept": bool(options["keep_file"]),
                    "queue": build_download_queue_recommendation(
                        duration_ms=download_duration_ms,
                    ),
                }
                if options["keep_file"]:
                    download_smoke["path"] = temp_result.get("path") or ""
                else:
                    remove_temp_download(temp_result.get("path"))
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
            "planning_duration_ms": planning_duration_ms,
            "urls_redacted": not bool(options["show_urls"]),
        }
        data["smoke"] = build_smoke_summary(plan, duration_ms=planning_duration_ms)
        data["plan"] = redact_plan_urls(
            plan,
            show_urls=bool(options["show_urls"]),
        )
        if download_smoke is not None:
            data["download_smoke"] = download_smoke
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
        self.stdout.write(f"smoke_rollout: {data['smoke']['recommended_rollout']}")
        self.stdout.write(f"smoke_issues: {', '.join(data['smoke']['issues']) or '-'}")
        if download_smoke is not None:
            self.stdout.write(f"download_smoke_ok: {download_smoke['ok']}")
            self.stdout.write(f"download_smoke_duration_ms: {download_smoke['duration_ms']}")
            self.stdout.write(f"download_smoke_size_bytes: {download_smoke['size_bytes']}")
            self.stdout.write(
                "download_smoke_queue_recommended: {recommended}".format(
                    recommended=download_smoke["queue"]["queue_recommended"],
                )
            )
            self.stdout.write(
                "download_smoke_queue_reason: {reason}".format(
                    reason=download_smoke["queue"]["reason"],
                )
            )
            if download_smoke.get("path"):
                self.stdout.write(f"download_smoke_path: {download_smoke['path']}")
