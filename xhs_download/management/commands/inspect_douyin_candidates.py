import json

from django.core.management.base import BaseCommand, CommandError

from ...services.diagnostics import select_best_media_candidate
from ...services.platforms.douyin_kukutool import get_kukutool_provider_health
from ...services.platforms.douyin import (
    DouyinAdapter,
    find_aweme_in_html,
    get_base_filename,
    get_video_media_candidates,
)
from ...services.provider_settings import get_platform_provider_settings


def format_quality_params(candidate):
    params = candidate.get("final_quality_params") or candidate.get("quality_params") or {}
    if not params:
        return "-"
    return ",".join(f"{key}={value}" for key, value in params.items())


class Command(BaseCommand):
    help = "Inspect Douyin video candidates from native parser sources."

    def add_arguments(self, parser):
        parser.add_argument("url", nargs="?", default="", help="Douyin URL or share text")
        parser.add_argument("--timeout", type=int, default=6, help="Probe timeout in seconds")
        parser.add_argument("--max-candidates", type=int, default=20, help="Maximum candidates to probe per source")
        parser.add_argument("--no-probe", action="store_true", help="Only list declared metadata without HEAD/Range probing")
        parser.add_argument(
            "--health",
            action="store_true",
            help="Print Douyin ultra-HD provider health without parsing a URL",
        )
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output JSON")

    def handle(self, *args, **options):
        if options["health"]:
            payload = build_provider_health_payload()
            if options["as_json"]:
                self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
                return
            self.stdout.write(f"ok: {payload['ok']}")
            self.stdout.write(f"ultra_hd_enabled: {payload['settings']['ultra_hd_enabled']}")
            self.stdout.write(f"kukutool_enabled: {payload['settings']['kukutool_enabled']}")
            self.stdout.write(f"provider_available: {payload['provider']['available']}")
            runtime = payload["provider"].get("runtime") or {}
            sidecar = payload["provider"].get("sidecar") or {}
            self.stdout.write(
                f"runtime: available={runtime.get('available')} error={runtime.get('error') or '-'}"
            )
            self.stdout.write(
                f"sidecar: configured={sidecar.get('configured')} available={sidecar.get('available')} base_url={sidecar.get('base_url') or '-'} error={sidecar.get('error') or '-'}"
            )
            return

        if not options["url"]:
            raise CommandError("URL is required unless --health is used.")

        adapter = DouyinAdapter()
        context = adapter.build_parse_context(options["url"])
        sources = []

        if context.html:
            aweme = find_aweme_in_html(context.html)
            if aweme:
                sources.append(("direct_page", aweme))

        if context.aweme_id:
            for source_name, fetcher in (
                ("mobile_feed", adapter.fetch_mobile_feed_aweme),
                ("mobile_share", adapter.fetch_mobile_share_aweme),
            ):
                try:
                    aweme = fetcher(context.aweme_id)
                except Exception as error:
                    self.stderr.write(f"{source_name} unavailable: {error}")
                    continue
                if aweme:
                    sources.append((source_name, aweme))

        if not sources:
            if context.direct_error:
                raise CommandError(str(context.direct_error))
            raise CommandError("No Douyin aweme data found.")

        payload = {
            "url": options["url"],
            "final_url": context.final_url,
            "aweme_id": context.aweme_id,
            "sources": [],
        }

        for source_name, aweme in sources:
            video = aweme.get("video") or {}
            candidates = get_video_media_candidates(video)
            selection = select_best_media_candidate(
                candidates,
                headers=adapter.headers,
                allowed_final_host_suffixes=adapter.media_host_suffixes,
                timeout=options["timeout"],
                max_candidates=options["max_candidates"],
                should_probe=not options["no_probe"],
            )
            payload["sources"].append(
                {
                    "source": source_name,
                    "aweme_id": aweme.get("aweme_id", ""),
                    "base_filename": get_base_filename(aweme),
                    "candidate_count": len(candidates),
                    "selection": selection.as_dict(),
                }
            )

        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"aweme_id: {payload['aweme_id'] or '-'}")
        self.stdout.write(f"final_url: {payload['final_url'] or '-'}")
        for source in payload["sources"]:
            selection = source["selection"]
            selected = selection["selected"] or {}
            self.stdout.write("")
            self.stdout.write(f"[{source['source']}] {source['base_filename']}")
            self.stdout.write(f"candidate_count: {source['candidate_count']}")
            self.stdout.write(f"selection_reason: {selection['reason']}")
            self.stdout.write(
                "selected: {source} {quality} declared={declared} probed={probed} host={host} params={params}".format(
                    source=selected.get("source", "-"),
                    quality=selected.get("quality_label", "-"),
                    declared=selected.get("declared_size_bytes", 0),
                    probed=selected.get("probed_size_bytes", 0),
                    host=selected.get("final_host") or selected.get("host", "-"),
                    params=format_quality_params(selected),
                )
            )
            for candidate in selection["candidates"]:
                self.stdout.write(
                    "#{index} {source} {quality} declared={declared} probed={probed} "
                    "reachable={reachable} host={host} params={params} method={method} error={error}".format(
                        index=candidate["index"],
                        source=candidate["source"],
                        quality=candidate["quality_label"],
                        declared=candidate["declared_size_bytes"],
                        probed=candidate["probed_size_bytes"],
                        reachable=candidate["reachable"],
                        host=candidate["final_host"] or candidate["host"],
                        params=format_quality_params(candidate),
                        method=candidate["probe_method"] or "-",
                        error=candidate["error"] or "-",
                    )
                )


def build_provider_health_payload():
    provider_settings = get_platform_provider_settings("douyin")
    health = get_kukutool_provider_health()
    issues = []
    if provider_settings.ultra_hd_enabled and not health.get("available"):
        issues.append({
            "code": "kukutool_not_available",
            "message": "Ultra-HD is enabled but KuKuTool is not available.",
        })
    if (
        provider_settings.kukutool_enabled
        and not provider_settings.kukutool_node_binary
        and not provider_settings.kukutool_sidecar_base_url
        and not health.get("available")
    ):
        issues.append({
            "code": "no_kukutool_runtime_configured",
            "message": "No KUKUTOOL_NODE_BINARY or KUKUTOOL_SIDECAR_BASE_URL is configured.",
        })
    return {
        "ok": not issues,
        "settings": {
            "ultra_hd_enabled": provider_settings.ultra_hd_enabled,
            "kukutool_enabled": provider_settings.kukutool_enabled,
            "provider_order": provider_settings.provider_order,
            "enabled_provider_order": provider_settings.enabled_provider_order,
            "kukutool_sidecar_configured": bool(
                provider_settings.kukutool_sidecar_base_url
            ),
            "kukutool_sidecar_base_url": (
                provider_settings.kukutool_sidecar_base_url
            ),
            "kukutool_node_binary_configured": bool(
                provider_settings.kukutool_node_binary
            ),
            "ultra_total_timeout_seconds": (
                provider_settings.ultra_total_timeout_seconds
            ),
            "kukutool_timeout_seconds": provider_settings.kukutool_timeout_seconds,
            "kukutool_sidecar_timeout_seconds": (
                provider_settings.kukutool_sidecar_timeout_seconds
            ),
        },
        "provider": health,
        "issues": issues,
    }
