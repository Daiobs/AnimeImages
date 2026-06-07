import json

from django.core.management.base import BaseCommand, CommandError

from ...services.diagnostics import select_best_media_candidate
from ...services.platforms.douyin import (
    DouyinAdapter,
    find_aweme_in_html,
    get_base_filename,
    get_video_media_candidates,
)


def format_quality_params(candidate):
    params = candidate.get("final_quality_params") or candidate.get("quality_params") or {}
    if not params:
        return "-"
    return ",".join(f"{key}={value}" for key, value in params.items())


class Command(BaseCommand):
    help = "Inspect Douyin video candidates from native parser sources."

    def add_arguments(self, parser):
        parser.add_argument("url", help="Douyin URL or share text")
        parser.add_argument("--timeout", type=int, default=6, help="Probe timeout in seconds")
        parser.add_argument("--max-candidates", type=int, default=20, help="Maximum candidates to probe per source")
        parser.add_argument("--no-probe", action="store_true", help="Only list declared metadata without HEAD/Range probing")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output JSON")

    def handle(self, *args, **options):
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
