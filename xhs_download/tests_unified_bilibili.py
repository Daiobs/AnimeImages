"""Bilibili on the unified pipeline: quality ladder, fallback admission, policy. No network."""
from copy import deepcopy
from dataclasses import replace
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from .models import PipelineRoute
from .services.pipeline import Failure, FailureKind, Work
from .services.platforms.bilibili import BilibiliAdapter as BilibiliPlatform
from .services.unified import routes
from .services.unified.bilibili import (
    AdmittedFallback, NativeAdapter, bilibili_media_only, other_qualities,
)
from .services.unified.entry import with_unified
from .services.unified.presentation import build_picker, download_item, resolve_selection

SOURCE = "https://www.bilibili.com/video/BV13x41117TL/"
HD = "http://upos-sz-mirrorhw.bilivideo.com/upgcxcode/hd.mp4?deadline=1900000000"
SD = "https://upos-sz-mirrorhw.bilivideo.com/upgcxcode/sd.mp4?deadline=1900000000"
FORMATS = [{"quality": 64, "new_description": "720P 准高清"}, {"quality": 16, "new_description": "360P 流畅"}]


def play(quality, url, size):
    return {"quality": quality, "format": "mp4", "accept_quality": [64, 16], "support_formats": FORMATS,
            "durl": [{"url": url, "size": size, "order": 1}]}


def native_raw(**extra):
    return {"view": {"code": 0, "data": {"bvid": "BV13x41117TL", "aid": 170001, "title": "统一管线 B 站",
                                         "pages": [{"page": 1, "cid": 279786, "duration": 12}]}},
            "playurl": {"code": 0, "data": play(64, HD, 5309115)},
            "selection": {"page": 1, "playurl_params": {"cid": 279786, "bvid": "BV13x41117TL", "qn": 80},
                          "page_url": SOURCE},
            **extra}


class QualityLadderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_other_quality_becomes_a_second_variant_over_https(self):
        raw = native_raw(other_playurls=[(16, {"code": 0, "data": play(16, SD, 2411724)})])
        work = NativeAdapter().map(deepcopy(raw), SOURCE)
        self.assertIsInstance(work, Work)
        variants = work.items[0].variants
        self.assertEqual([(v.url.split("/")[-1].split("?")[0], v.quality_label, v.size_bytes) for v in variants],
                         [("hd.mp4", "720P 准高清", 5309115), ("sd.mp4", "360P 流畅", 2411724)])
        self.assertTrue(all(v.url.startswith("https://") for v in variants))
        self.assertTrue(any(key.startswith("items.video:1.variants.video:1:q16:1.") for key in work.field_sources))
        picker, records, _ = build_picker(replace(work, platform="bilibili", provider="native"), {"video": "mp4"})
        self.assertEqual([c["label"] for c in picker["videos"]], ["720P 准高清", "360P 流畅"])
        choice = picker["videos"][1]
        record = resolve_selection(choice["resource_token"], choice["candidate_id"])
        self.assertEqual(record["expires_at"], 1900000000)
        self.assertEqual(download_item(record)["url"], SD)

    def test_ladder_asks_each_listed_quality_once_and_keeps_only_matching_files(self):
        calls = []

        def api(url, params, **options):
            calls.append(params["qn"])
            return play(32, SD, 1) if params["qn"] == 16 else {}

        raw = native_raw()
        raw["playurl"]["data"]["accept_quality"] = [80, 64, 16]
        with patch("xhs_download.services.platforms.bilibili.api_get_json", api), \
                patch("xhs_download.services.platforms.bilibili.anonymous_cookie_context", return_value={}):
            found = other_qualities(raw, time.monotonic() + 5)
        self.assertEqual(calls, [80, 16])
        self.assertEqual(found, [])

    def test_ladder_stops_at_the_first_error_and_never_runs_out_of_time(self):
        def api(url, params, **options):
            raise RuntimeError("412")

        with patch("xhs_download.services.platforms.bilibili.api_get_json", api), \
                patch("xhs_download.services.platforms.bilibili.anonymous_cookie_context", return_value={}):
            self.assertEqual(other_qualities(native_raw(), time.monotonic() + 5), [])
            self.assertEqual(other_qualities(native_raw(), time.monotonic() + 0.1), [])


class ServiceFallbackTests(SimpleTestCase):
    def test_only_first_part_videos_reach_the_service(self):
        calls = []
        fallback = AdmittedFallback(lambda: calls.append("authorize") or ("tiantianjiexi_generic",))
        self.assertEqual(fallback.parse(SOURCE + "?p=2", 5).code, "BILIBILI_SERVICE_SOURCE")
        self.assertEqual(calls, [])

    def test_admission_is_asked_when_the_service_would_run(self):
        self.assertEqual(AdmittedFallback(lambda: ()).parse(SOURCE, 5).code, "BILIBILI_SERVICE_NOT_ADMITTED")
        with patch("xhs_download.services.unified.bilibili.ServiceTianTian.parse",
                   return_value=Failure(FailureKind.UNAVAILABLE, "DOWN")) as service:
            AdmittedFallback(lambda: ("tiantianjiexi_generic",)).parse(SOURCE, 5)
        self.assertEqual(service.call_args.args[0], "https://www.bilibili.com/video/BV13x41117TL/")

    def test_policy_keeps_bilibili_and_its_akamai_mirrors_only(self):
        work = NativeAdapter().map(native_raw(), SOURCE)
        item = work.items[0]
        akamai = replace(item.variants[0], variant_id="video:1:9",
                         url="https://upos-hz-mirrorakam.akamaized.net/upgcxcode/hd.mp4")
        foreign = replace(item.variants[0], variant_id="video:1:8", url="https://cdn.example.test/hd.mp4")
        kept = bilibili_media_only(replace(work, items=(replace(item, variants=(*item.variants, akamai, foreign)),)))
        self.assertEqual([v.variant_id for v in kept.items[0].variants], ["video:1:1", "video:1:9"])


class BilibiliRouteTests(TestCase):
    def test_migration_adds_the_route_switched_off_with_native_first(self):
        row = PipelineRoute.objects.get(platform="bilibili")
        self.assertFalse(row.enabled)
        self.assertEqual((row.policy, row.order), ("first_success", ["native", "tiantianjiexi"]))
        self.assertIsNone(routes.load_route("bilibili"))

    def test_switch_passes_the_existing_service_authorizer(self):
        PipelineRoute.objects.filter(platform="bilibili").update(enabled=True)
        authorize = lambda: ()  # noqa: E731
        with patch("xhs_download.services.unified.bilibili.parse", return_value="unified") as unified:
            self.assertEqual(with_unified(lambda url, **o: "legacy")(SOURCE, bilibili_service_authorizer=authorize),
                             "unified")
        self.assertIs(unified.call_args.kwargs["authorize"], authorize)


class PlatformPresentationTests(SimpleTestCase):
    def test_bilibili_result_keeps_the_legacy_card_shape(self):
        from .services.unified.presentation import present
        work = replace(NativeAdapter().map(native_raw(), SOURCE), platform="bilibili", provider="native")
        result = present(work, platform_adapter=BilibiliPlatform(), provider_label="哔哩哔哩原生",
                         fallback_extensions={"video": "mp4"})
        self.assertEqual((result.platform, result.post_type, result.metadata["caption"]),
                         ("bilibili", "video", "统一管线 B 站"))
