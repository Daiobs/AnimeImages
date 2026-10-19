from concurrent.futures import Future
from dataclasses import asdict, replace
from time import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.candidates import MediaCandidate
from .services.platforms.douyin_kukutool import (
    DouyinKukuToolProvider,
    build_kukutool_cache_key,
    convert_sidecar_payload_to_kukutool_payload,
    serialize_validated_candidate,
)
from .services.platforms.douyin_providers import DouyinParseContext, build_douyin_metadata_cache_key
from .services.provider_settings import ProviderSettings
from .services.quality import QualityProbeResult
from .services.service_format_selection import build_service_picker
from .services.upstream_candidates import MAX_CANDIDATES, resolve_candidate_record


AWEME_ID = "7647533902413173321"
MODULE = "xhs_download.services.platforms.douyin_kukutool"


def resource_url(name, *, expiry=0x7FFFFFFF, host="v26-default.365yg.com"):
    return f"https://{host}/signature/{expiry:08x}/video/tos/cn/bucket/{name}/"


def resource(name, *, size=7_000_000, bitrate=1_000_000, **url_options):
    return {
        "url": resource_url(name, **url_options),
        "type": name,
        "size": size,
        "bit_rate": bitrate,
    }


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
})
class DouyinKukuToolRetentionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = int(time())
        self.adapter = SimpleNamespace(
            platform_id="douyin",
            headers={},
            media_host_suffixes=("365yg.com", "douyinvod.com"),
        )
        self.context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
        )
        self.runner = Mock()
        self.probe = Mock()
        self.provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_reference_wait_seconds=0,
                kukutool_global_request_limit_per_minute=0,
            ),
            runner=self.runner,
            quality_probe=self.probe,
        )
        self.cache_key = build_kukutool_cache_key(AWEME_ID)
        for name, options in (
            (f"{MODULE}.time", {"return_value": self.now}),
            (f"{MODULE}.is_kukutool_admin_disabled", {"return_value": False}),
            ("requests.sessions.Session.request", {"side_effect": AssertionError("Unexpected HTTP")}),
            ("socket.getaddrinfo", {"side_effect": AssertionError("Unexpected DNS")}),
        ):
            active = patch(name, **options)
            active.start()
            self.addCleanup(active.stop)
        # Picker transport validation is separate from the real CDN/expiry checks.
        for name in (
            "xhs_download.services.upstream_candidates.validate_public_download_url",
            "xhs_download.services.upstream_delivery.validate_public_download_url",
        ):
            active = patch(name, side_effect=lambda url, *args, **kwargs: url)
            active.start()
            self.addCleanup(active.stop)

    def resolve(self, rows):
        self.runner.return_value = {"videos": [{"video_fullinfo": rows}]}
        return self.provider.resolve_validated_candidate(self.adapter, self.context)

    def build(self, validated):
        native = Future()
        native.set_result(None)
        return self.provider.build_result(
            self.adapter, self.context, validated, native, self.now,
        )

    def records(self, result):
        return [
            resolve_candidate_record(row["resource_token"], row["candidate_id"])
            for row in result.metadata["upstream_candidates"]
        ]

    def test_current_response_metadata_survives_only_its_validated_cache(self):
        self.adapter.media_host_suffixes += ("douyinpic.com",)
        self.runner.return_value = {
            "title": "Current work", "cover": "https://p3.douyinpic.com/current.jpg",
            "videos": [{"video_fullinfo": [resource("720P")]}],
        }
        validated = self.provider.resolve_validated_candidate(self.adapter, self.context)
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        for item in (validated, cached):
            failed_native = Future()
            failed_native.set_exception(TimeoutError("native unavailable"))
            result = self.provider.build_result(self.adapter, self.context, item, failed_native, self.now)
            self.assertEqual(result.metadata["title"], "Current work")
            self.assertEqual(result.video_assets[0].cover_url, "https://p3.douyinpic.com/current.jpg")
        self.runner.assert_called_once()
        self.assertIsNone(cache.get(build_douyin_metadata_cache_key(AWEME_ID)))

    def test_cache_hit_never_reads_separate_old_metadata_or_starts_native(self):
        self.resolve([resource("720P")])
        cache.set(build_douyin_metadata_cache_key(AWEME_ID), {
            "platform": "douyin", "post_type": "video", "base_filename": "Old title",
            "reference_url": resource_url("expired"),
            "cover_url": "https://p3.douyinpic.com/old.jpg", "metadata": {"title": "Old title"},
        }, 1800)
        with patch(f"{MODULE}.submit_provider_task", side_effect=AssertionError("No fresh task")):
            result = self.provider.parse(self.adapter, self.context)
        self.assertNotEqual(result.metadata.get("title"), "Old title")
        self.assertEqual(result.video_assets[0].cover_url, "")
        self.runner.assert_called_once()

    def test_expired_validated_metadata_does_not_decorate_new_response(self):
        self.runner.return_value = {"title": "Old title", "videos": [{"video_fullinfo": [resource("720P")]}]}
        self.provider.resolve_validated_candidate(self.adapter, self.context)
        saved = cache.get(self.cache_key)
        saved["signed_url_expires_at"] = self.now - 1
        cache.set(self.cache_key, saved, 300)
        self.runner.return_value = {"videos": [{"video_fullinfo": [resource("1080P")]}]}
        current = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertEqual(current.title, "")
        self.assertNotEqual(self.build(current).metadata.get("title"), "Old title")
        self.assertEqual(self.runner.call_count, 2)

    def test_invalid_optional_cover_does_not_discard_media(self):
        for cover in ("https://127.0.0.1/private", "https://secret@v26-default.365yg.com/private"):
            cache.delete(self.cache_key)
            self.runner.return_value = {"title": "Current", "cover": cover,
                                      "videos": [{"video_fullinfo": [resource("720P")]}]}
            current = self.provider.resolve_validated_candidate(self.adapter, self.context)
            self.assertEqual(current.cover_url, "")
            self.assertTrue(self.build(current).video_assets)

    def test_malformed_optional_title_does_not_discard_media(self):
        self.runner.return_value = {"title": "https://[broken",
                                  "videos": [{"video_fullinfo": [resource("720P")]}]}
        current = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertEqual(current.title, "")
        self.assertTrue(self.build(current).video_assets)

    def test_runner_resolve_cache_build_retains_variants_without_changing_winner(self):
        high = resource("2160P", bitrate=9_000_000)
        low = resource("1080P", size=20_000_000, bitrate=2_000_000)
        validated = self.resolve([low, high])
        self.assertEqual(validated.candidate.url, high["url"])
        self.assertEqual([c.url for c in validated.candidates], [high["url"], low["url"]])
        self.assertEqual(cache.get(self.cache_key)["candidates"],
                         [asdict(c) for c in validated.candidates])

        cached = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.candidate, validated.candidate)
        self.assertEqual(cached.candidates, validated.candidates)
        for value in (validated, cached):
            result = self.build(value)
            self.assertEqual(len(result.video_assets), 1)
            main = result.video_assets[0]
            self.assertEqual(main.url, high["url"])
            self.assertEqual(main.asset_id, f"douyin:{AWEME_ID}:video:kukutool")
            self.assertEqual(main.provider_resource_id, AWEME_ID)
            self.assertEqual(main.provider_cache_key, self.cache_key)
            self.assertTrue(main.provider_manifest_token)
            self.assertEqual(result.metadata["selected_provider"], "kukutool")
            self.assertEqual(result.metadata["kukutool_cache_hit"], value.cache_hit)
            records = self.records(result)
            self.assertEqual([record["url"] for record in records], [high["url"], low["url"]])
            self.assertEqual(len({record["candidate_id"] for record in records}), 2)
            picker = build_service_picker(result.metadata["upstream_candidates"])
            self.assertEqual(picker["selected"]["candidate_id"], records[0]["candidate_id"])
        self.runner.assert_called_once()
        self.probe.probe.assert_not_called()

    def test_sidecar_resources_survive_the_actual_runner_boundary(self):
        high = resource("2160P", bitrate=9_000_000)
        low = resource("720P")
        self.runner.return_value = convert_sidecar_payload_to_kukutool_payload({
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok",
            "best": high,
            "resources": [high, low],
        })
        validated = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertEqual([c.url for c in validated.candidates], [high["url"], low["url"]])
        self.assertEqual(len(self.records(self.build(validated))), 2)
        self.probe.probe.assert_not_called()

    def test_extra_resources_must_meet_declaration_domain_and_expiry_rules(self):
        high = resource("2160P", bitrate=9_000_000)
        low = resource("720P")
        validated = self.resolve([
            high, low,
            resource("expired", expiry=self.now - 1),
            resource("expires-soon", expiry=self.now + 30),
            resource("missing-size", size=0),
            resource("untrusted", host="media.example.com"),
            {**resource("missing-object"), "url": "https://v26-default.365yg.com/file.mp4"},
            {**resource("unsigned"), "url": "https://v26-default.365yg.com/video/tos/unsigned/"},
            {**resource("insecure"), "url": resource_url("insecure").replace("https:", "http:")},
        ])
        self.assertEqual([c.url for c in validated.candidates], [high["url"], low["url"]])
        self.assertEqual([r["url"] for r in self.records(self.build(validated))],
                         [high["url"], low["url"]])
        self.probe.probe.assert_not_called()

    def test_cache_and_build_recheck_variant_expiry_and_adapter_authority(self):
        high = resource("2160P", bitrate=9_000_000)
        short = resource("720P", expiry=self.now + 60)
        other_cdn = resource("1080P", host="v3.douyinvod.com")
        validated = self.resolve([high, short, other_cdn])
        self.assertEqual(len(validated.candidates), 3)
        self.adapter.media_host_suffixes = ("365yg.com",)
        with patch(f"{MODULE}.time", return_value=self.now + 31):
            cached = self.provider.get_cached_candidate(self.adapter, self.context)
            self.assertEqual([c.url for c in cached.candidates], [high["url"]])
            self.assertEqual([r["url"] for r in self.records(self.build(validated))], [high["url"]])
        self.probe.probe.assert_not_called()

    def test_legacy_cache_and_empty_retention_stay_single_candidate(self):
        validated = self.resolve([resource("2160P", bitrate=9_000_000), resource("720P")])
        old_data = serialize_validated_candidate(validated)
        del old_data["candidates"]
        cache.set(self.cache_key, old_data)
        cached = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.candidates, ())
        for value in (cached, replace(validated, candidates=())):
            self.assertEqual([r["url"] for r in self.records(self.build(value))],
                             [validated.candidate.url])
        self.runner.assert_called_once()
        self.probe.probe.assert_not_called()

    def test_malformed_or_untrusted_cached_variants_do_not_discard_winner(self):
        validated = self.resolve([resource("2160P", bitrate=9_000_000), resource("720P")])
        data = cache.get(self.cache_key)
        data["candidates"].extend([
            None,
            {"unexpected": "field"},
            asdict(MediaCandidate(url="https://example.com/arbitrary.mp4", size_bytes=123)),
            {"url": resource_url("bad-size"), "size_bytes": "not-a-number"},
        ])
        cache.set(self.cache_key, data)
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        self.assertEqual(cached.candidate, validated.candidate)
        self.assertEqual(cached.candidates, validated.candidates)

    def test_existing_probe_winner_retains_other_declarations_with_no_extra_probes(self):
        high = resource("2160P", size=0, bitrate=9_000_000)
        low = resource("720P")
        final_url = resource_url("verified-2160P")
        self.probe.probe.return_value = QualityProbeResult(
            url=high["url"], final_url=final_url, reachable=True,
            size_bytes=11_000_000, content_type="video/mp4", method="RANGE",
        )
        validated = self.resolve([high, low])
        self.assertEqual(validated.candidate.url, final_url)
        self.assertEqual(validated.candidate.size_bytes, 11_000_000)
        self.assertEqual([c.url for c in validated.candidates], [final_url, low["url"]])
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        self.assertEqual(cached.candidates, validated.candidates)
        self.assertEqual([r["url"] for r in self.records(self.build(cached))], [final_url, low["url"]])
        self.probe.probe.assert_called_once()
        self.assertEqual(self.probe.probe.call_args.args[0], high["url"])

    def test_probe_without_exposed_expiry_keeps_winner_as_picker_default(self):
        high = resource("2160P", size=0, bitrate=9_000_000)
        low = resource("720P")
        final_url = "https://v26-default.365yg.com/video/tos/cn/bucket/probed/"
        self.probe.probe.return_value = QualityProbeResult(
            url=high["url"], final_url=final_url, reachable=True,
            size_bytes=11_000_000, content_type="video/mp4", method="RANGE",
        )
        validated = self.resolve([high, low])
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        for value in (validated, cached):
            result = self.build(value)
            records = self.records(result)
            self.assertEqual([r["url"] for r in records], [final_url, low["url"]])
            picker = build_service_picker(result.metadata["upstream_candidates"])
            self.assertEqual(picker["selected"]["candidate_id"], records[0]["candidate_id"])
        self.probe.probe.assert_called_once()

    def test_known_probe_failures_are_not_retained_as_declared_extras(self):
        high = resource("2160P", size=0, bitrate=9_000_000)
        failed = resource("1080P", bitrate=5_000_000)
        low = resource("720P")
        self.probe.probe.side_effect = [
            QualityProbeResult(url=row["url"], reachable=False, error="unreachable")
            for row in (high, failed)
        ] + [QualityProbeResult(
            url=low["url"], final_url=low["url"], reachable=True,
            size_bytes=low["size"], content_type="video/mp4", method="RANGE",
        )]
        self.provider.provider_settings = replace(
            self.provider.provider_settings, kukutool_max_probe_candidates=3,
        )
        validated = self.resolve([high, failed, low])
        self.assertEqual([c.url for c in validated.candidates], [low["url"]])
        self.assertEqual([r["url"] for r in self.records(self.build(validated))], [low["url"]])
        self.assertEqual(self.probe.probe.call_count, 3)

    def test_same_url_labels_and_metadata_survive_resolve_cache_and_build(self):
        high = resource("2160P", bitrate=9_000_000)
        alias = {**high, "type": "1080P", "bit_rate": 1_000_000}
        other_metadata = {**alias, "size": 3_000_000}
        validated = self.resolve([high, alias, other_metadata, alias])
        self.assertEqual(validated.candidate.quality_label, "2160P")
        self.assertEqual(len(validated.candidates), 3)
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        self.assertEqual(cached.candidates, validated.candidates)
        for value in (validated, cached):
            result = self.build(value)
            rows = result.metadata["upstream_candidates"]
            self.assertEqual([row["format_label"] for row in rows], ["2160P", "1080P", "1080P"])
            self.assertTrue(all(row["same_resource_labels"] for row in rows))
            self.assertEqual(len({row["candidate_id"] for row in rows}), 3)
            self.assertEqual({r["url"] for r in self.records(result)}, {high["url"]})
            self.assertEqual(result.video_assets[0].quality_label, "2160P")
        self.probe.probe.assert_not_called()

    def test_missing_optional_size_is_retained_but_explicit_invalid_size_is_not(self):
        high = resource("2160P", bitrate=9_000_000)
        missing = resource("720P")
        del missing["size"]
        validated = self.resolve([
            high, missing, resource("negative", size=-1),
            resource("nonnumeric", size="invalid"), resource("zero", size=0),
        ])
        self.assertEqual([c.url for c in validated.candidates], [high["url"], missing["url"]])
        self.assertEqual(validated.candidates[1].size_bytes, 0)
        cached = self.provider.get_cached_candidate(self.adapter, self.context)
        self.assertEqual(cached.candidates, validated.candidates)
        self.assertEqual([r["url"] for r in self.records(self.build(cached))],
                         [high["url"], missing["url"]])
        self.probe.probe.assert_not_called()

    def test_sidecar_retains_labels_and_missing_size_without_changing_old_winner(self):
        best = resource("2160P", bitrate=9_000_000)
        alias = {**best, "type": "Original", "bit_rate": 20_000_000}
        old_winner = resource("1080P", bitrate=10_000_000)
        missing = resource("720P")
        del missing["size"]
        self.runner.return_value = convert_sidecar_payload_to_kukutool_payload({
            "contract_version": "douyin-source-resolver.v1",
            "status": "ok", "best": best,
            "resources": [alias, old_winner, missing],
        })
        validated = self.provider.resolve_validated_candidate(self.adapter, self.context)
        self.assertEqual(validated.candidate.url, old_winner["url"])
        self.assertEqual({c.quality_label for c in validated.candidates},
                         {"2160P", "Original", "1080P", "720P"})
        result = self.build(validated)
        self.assertEqual(len(self.records(result)), 4)
        self.assertEqual(result.video_assets[0].url, old_winner["url"])
        self.probe.probe.assert_not_called()

    def test_cached_variant_deserialization_is_bounded(self):
        validated = self.resolve([resource("2160P", bitrate=9_000_000), resource("720P")])
        data = cache.get(self.cache_key)
        data["candidates"] = [asdict(validated.candidate)] * MAX_CANDIDATES
        data["candidates"].append(asdict(validated.candidates[1]))
        cache.set(self.cache_key, data)
        with patch(f"{MODULE}.MediaCandidate", wraps=MediaCandidate) as constructor:
            cached = self.provider.get_cached_candidate(self.adapter, self.context)
        self.assertEqual(cached.candidates, (validated.candidate,))
        self.assertEqual(constructor.call_count, MAX_CANDIDATES + 1)
