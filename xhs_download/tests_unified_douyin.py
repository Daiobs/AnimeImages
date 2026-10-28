"""Douyin on the unified pipeline: facts, chain, page, selection. No network."""
import json
import threading
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core import signing
from django.core.cache import cache
import requests
from django.db import DatabaseError
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from .models import PipelineRoute
from .services.pipeline import CooldownBook, Failure, FailureKind, Item, Kind, Variant, Work
from .services.platforms.douyin import DouyinAdapter as DouyinPlatform
from .services.unified import routes
from .services.unified.douyin import (
    NativeAdapter, ServiceTianTianAdapter, douyin_media_only, failure_error,
)
from .services.unified.entry import with_unified
from .services.unified.presentation import (
    SCHEMA, build_picker, download_item, link_item, picker_with_covers, present, resolve_selection,
)
from .services.unified.runner import run_chain
from .services.common import XhsSecurityError
from .services.unified.transport import browser_readable_link
from .views import build_result_display_context

AWEME = "7634213610894689787"
SOURCE = f"https://www.douyin.com/video/{AWEME}"
WATERMARK = "https://v5-gz-a.douyinvod.com/obj/watermark.mp4"
NATIVE_RAW = {
    "aweme_id": AWEME, "desc": "测试作品 #话题", "author": {"nickname": "作者"},
    "video": {
        "duration": 9100,
        "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.jpeg"]},
        "play_addr": {"url_list": ["https://v11-colds.douyinvod.com/obj/root.mp4"],
                      "width": 576, "height": 1024, "data_size": 1601315},
        "download_addr": {"url_list": [WATERMARK], "width": 720, "height": 720, "data_size": 2507813},
        "bit_rate": [
            {"bit_rate": 652156, "play_addr": {
                "url_list": ["https://v11-cold-src.douyinvod.com/obj/720.mp4",
                             "https://v11-colds.douyinvod.com/obj/720.mp4"],
                "width": 720, "height": 1280, "data_size": 741828}},
            {"bit_rate": 375626, "play_addr": {
                "url_list": ["https://v11-cold-src.douyinvod.com/obj/540.mp4"],
                "width": 540, "height": 960, "data_size": 427275}},
        ],
    },
}
TIANTIAN_RAW = {"code": 200, "data": {
    "aweme_id": AWEME, "title": "测试作品",
    "videos": [{"url": "https://v26-qingtao.douyinvod.com/obj/original?mime_type=video_mp4",
                "streams": [{"url": "https://v11-qingtao.douyinvod.com/obj/original?mime_type=video_mp4"}]}],
    "images": ["https://p5-sign.douyinpic.com/obj/cover.webp"],
}}
PLATFORM = DouyinPlatform()


def native_work():
    work = NativeAdapter().map(deepcopy(NATIVE_RAW), SOURCE)
    assert isinstance(work, Work), work
    return replace(work, platform="douyin", provider="native")


def gallery_work():
    images = tuple(Item(f"image:{index}", Kind.IMAGE, index, (Variant(
        f"image:{index}:1", f"https://p3-sign.douyinpic.com/obj/{index}.jpeg", container="jpeg"),))
        for index in (1, 2, 3))
    return Work(SOURCE, AWEME, images, platform="douyin", provider="tiantianjiexi", title="图文作品")


def route(**fields):
    values = dict(platform="douyin", order=["tiantianjiexi", "kukutool", "native"], policy="floor_and_upgrade",
                  budget_seconds=1.0, floor_provider="native", upgrade_window_seconds=1.0,
                  platform_names=["抖音", "douyin"], cooldown_base_seconds=10, cooldown_max_seconds=300)
    values.update(fields)
    return SimpleNamespace(**values)


class DouyinFactsTests(SimpleTestCase):
    def test_native_drops_watermarked_download_copy(self):
        urls = [variant.url for item in native_work().items for variant in item.variants]
        self.assertNotIn(WATERMARK, urls)
        self.assertIn("https://v11-cold-src.douyinvod.com/obj/720.mp4", urls)

    def test_native_keeps_watermarked_copy_when_it_is_all_there_is(self):
        raw = deepcopy(NATIVE_RAW)
        raw["video"] = {"download_addr": raw["video"]["download_addr"]}
        work = NativeAdapter().map(raw, SOURCE)
        self.assertEqual([v.url for v in work.items[0].variants], [WATERMARK])

    def test_service_video_images_are_the_cover_and_unlabeled_video_is_original(self):
        work = ServiceTianTianAdapter().map(deepcopy(TIANTIAN_RAW), SOURCE)
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO])
        self.assertEqual(work.cover_url, "https://p5-sign.douyinpic.com/obj/cover.webp")
        self.assertTrue(all(variant.is_original for variant in work.items[0].variants))
        self.assertFalse(any(key.startswith("items.image:") for key in work.field_sources))

    def test_service_gallery_keeps_photos(self):
        raw = deepcopy(TIANTIAN_RAW)
        raw["data"].pop("videos")
        raw["data"]["images"] = [f"https://p5-sign.douyinpic.com/obj/{index}.webp" for index in (1, 2)]
        work = ServiceTianTianAdapter().map(raw, SOURCE)
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE])

    def test_native_cover_is_one_the_browser_can_show(self):
        raw = deepcopy(NATIVE_RAW)
        raw["video"]["cover"] = {"url_list": ["https://p3-sign.douyinpic.com/obj/c.heic",
                                              "https://p3-sign.douyinpic.com/obj/c.jpeg"]}
        work = NativeAdapter().map(raw, SOURCE)
        self.assertEqual(work.cover_url, "https://p3-sign.douyinpic.com/obj/c.jpeg")
        self.assertEqual(work.field_sources["cover_url"], "adapter:douyin_browser_cover")

    def test_media_outside_douyin_policy_is_dropped(self):
        work = native_work()
        foreign = replace(work.items[0], variants=(
            replace(work.items[0].variants[0], url="https://cdn.example.test/a.mp4"),) + work.items[0].variants[1:])
        kept = douyin_media_only(replace(work, items=(foreign,) + work.items[1:]))
        self.assertNotIn("https://cdn.example.test/a.mp4", [v.url for i in kept.items for v in i.variants])

    def test_failures_become_page_errors_with_codes(self):
        self.assertEqual(failure_error(Failure(FailureKind.NOT_FOUND, "X_GONE")).diagnostic_detail, "X_GONE")
        self.assertIn("超时", str(failure_error(Failure(FailureKind.TIMEOUT, "CHAIN_WINDOW_EXHAUSTED"))))


class FakeAdapter:
    def __init__(self, delay, outcome):
        self.delay, self.outcome = delay, outcome

    def parse(self, source_url, budget):
        time.sleep(self.delay)
        return self.outcome


class RunnerTests(SimpleTestCase):
    def run_route(self, adapters, **fields):
        config = routes.chain_config(route(**fields), adapters)
        return run_chain(config, adapters, {name: SOURCE for name in adapters},
                         CooldownBook(base_seconds=10, max_seconds=300))

    def test_preferred_result_within_budget_is_shown_even_when_floor_is_first(self):
        service = replace(native_work(), title="service")
        run = self.run_route({"native": FakeAdapter(0.01, native_work()),
                              "tiantianjiexi": FakeAdapter(0.1, service)})
        self.assertEqual((run.shown.provider, run.shown.title), ("tiantianjiexi", "service"))

    def test_floor_is_shown_when_preferred_fails(self):
        run = self.run_route({"native": FakeAdapter(0.05, native_work()),
                              "tiantianjiexi": FakeAdapter(0.01, Failure(FailureKind.UNAVAILABLE, "DOWN"))})
        self.assertEqual(run.shown.provider, "native")
        self.assertEqual([(a.provider, a.outcome) for a in run.attempts],
                         [("tiantianjiexi", "unavailable"), ("native", "success")])

    def test_floor_is_shown_at_the_window_when_preferred_is_slow(self):
        started = time.monotonic()
        run = self.run_route({"native": FakeAdapter(0.01, native_work()),
                              "tiantianjiexi": FakeAdapter(2.0, native_work())},
                             budget_seconds=1.0, upgrade_window_seconds=1.0)
        self.assertEqual(run.shown.provider, "native")
        self.assertLess(time.monotonic() - started, 1.9)

    def test_all_failing_reports_the_chain_failure(self):
        run = self.run_route({"native": FakeAdapter(0.01, Failure(FailureKind.UNAVAILABLE, "A")),
                              "tiantianjiexi": FakeAdapter(0.01, Failure(FailureKind.UNAVAILABLE, "B"))})
        self.assertIsNone(run.shown)
        self.assertEqual(run.failure.code, "ALL_PROVIDERS_FAILED")

    def test_native_alone_runs_first_success_with_the_whole_window(self):
        config = routes.chain_config(route(budget_seconds=2.0, upgrade_window_seconds=5.0), {"native": None})
        self.assertEqual((config.policy.value, config.order, config.budget_seconds), ("first_success", ("native",), 5.0))

    def test_exception_in_adapter_is_an_invalid_result(self):
        class Broken:
            def parse(self, source_url, budget):
                raise RuntimeError("boom")
        run = self.run_route({"native": FakeAdapter(0.01, native_work()), "tiantianjiexi": Broken()})
        self.assertEqual(run.shown.provider, "native")
        self.assertIn(("tiantianjiexi", "invalid_result", "ADAPTER_EXCEPTION"),
                      [(a.provider, a.outcome, a.code) for a in run.attempts])


class PresentationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def picker(self, work=None):
        picker, records, defaults = build_picker(work or native_work(), {"video": "mp4"})
        return picker, records, defaults

    def test_one_option_per_file_best_first_and_default_selected(self):
        picker, _records, defaults = self.picker()
        labels = [(c["label"], c["size_bytes"]) for c in picker["videos"]]
        self.assertEqual(labels, [("720p", 741828), ("576p", 1601315), ("540p", 427275)])
        self.assertTrue(picker["videos"][0]["selected"])
        self.assertEqual(defaults[0][1].height, 1280)
        self.assertEqual(picker["selection_url"], reverse("service_selected_format"))

    def test_records_keep_every_mirror_of_the_file(self):
        picker, records, _ = self.picker()
        record = records[picker["videos"][0]["candidate_id"]]
        self.assertEqual(record["mirrors"], ["https://v11-cold-src.douyinvod.com/obj/720.mp4",
                                             "https://v11-colds.douyinvod.com/obj/720.mp4"])

    def test_download_item_is_an_ordinary_browser_video(self):
        picker, _records, _defaults = self.picker()
        choice = picker["videos"][0]
        item = download_item(resolve_selection(choice["resource_token"], choice["candidate_id"]))
        self.assertEqual(item["url"], "https://v11-cold-src.douyinvod.com/obj/720.mp4")
        self.assertEqual((item["unified_schema"], item["selection_delivery"]), (SCHEMA, "ordinary"))
        self.assertNotIn("selection_schema", item)
        self.assertNotIn("browser_upstream_bound", item)
        # A Provider name on a download item means a manifest the server checks.
        self.assertNotIn("provider", item)
        self.assertEqual(item["unified_provider"], "native")
        self.assertTrue(item["filename"].endswith(".mp4"))
        self.assertLessEqual(len(item["filename"].rsplit(".", 1)[0]), 80)
        self.assertEqual(link_item(resolve_selection(choice["resource_token"], choice["candidate_id"]))["provider"],
                         "unified")

    def test_segmented_variant_is_listed_without_download(self):
        work = native_work()
        item = work.items[0]
        segmented = replace(item.variants[0], segmented=True, url="https://v11-colds.douyinvod.com/obj/a.m3u8")
        picker, records, _ = self.picker(replace(work, items=(replace(item, variants=(segmented,)),)))
        choice = picker["videos"][0]
        self.assertFalse(choice["can_download"])
        self.assertIn("分片", choice["limitation"])
        with self.assertRaises(ValueError):
            download_item(resolve_selection(choice["resource_token"], choice["candidate_id"]))

    def test_references_are_checked(self):
        picker, _records, _ = self.picker()
        choice = picker["videos"][0]
        with self.assertRaises(ValueError):
            resolve_selection(choice["resource_token"], picker["videos"][1]["candidate_id"])
        with self.assertRaises(signing.BadSignature):
            resolve_selection(choice["resource_token"] + "x", choice["candidate_id"])
        cache.clear()
        with self.assertRaises(ValueError):
            resolve_selection(choice["resource_token"], choice["candidate_id"])

    def test_present_gallery_uses_image_cards_and_no_picker(self):
        result = present(gallery_work(), platform_adapter=PLATFORM, provider_label="天天解析",
                         fallback_extensions={"image": "jpg"})
        self.assertEqual((result.post_type, len(result.image_assets)), ("image", 3))
        self.assertNotIn("unified_picker", result.metadata)

    def test_present_video_keeps_default_card_and_caption(self):
        result = present(native_work(), platform_adapter=PLATFORM, provider_label="抖音原生",
                         fallback_extensions={"video": "mp4"})
        self.assertEqual(result.post_type, "video")
        self.assertEqual([a.url for a in result.video_assets], ["https://v11-cold-src.douyinvod.com/obj/720.mp4"])
        self.assertEqual(result.metadata["description"], "#话题")
        display = picker_with_covers(result.metadata["unified_picker"], [{"cover_url": "c"}])
        self.assertEqual(display["cover_video"], {"cover_url": "c"})
        self.assertIsNone(result.metadata["unified_picker"]["cover_video"])


class FakeResponse:
    def __init__(self, status, headers):
        self.status_code, self.headers, self.closed = status, headers, False

    def close(self):
        self.closed = True


class TransportTests(SimpleTestCase):
    RECORD = {"url": "https://a.douyinvod.com/x.mp4",
              "mirrors": ["https://a.douyinvod.com/x.mp4", "https://b.douyinvod.com/x.mp4"]}

    def test_first_mirror_the_origin_may_read_wins(self):
        seen = []

        def request(url, **options):
            seen.append((url, options["headers"]["Origin"], options["headers"]["Range"]))
            cors = {} if url.startswith("https://a.") else {"Access-Control-Allow-Origin": "*"}
            return FakeResponse(206, {**cors, "Content-Range": "bytes 0-0/60099047"})

        self.assertEqual(browser_readable_link(self.RECORD, "https://download.example", request=request),
                         ("https://b.douyinvod.com/x.mp4", 60099047))
        self.assertEqual([url for url, *_ in seen], self.RECORD["mirrors"])
        self.assertEqual({(origin, rng) for _url, origin, rng in seen}, {("https://download.example", "bytes=0-0")})

    def test_no_readable_mirror_or_failures_give_none(self):
        def request(url, **options):
            if url.startswith("https://a."):
                raise OSError("down")
            return FakeResponse(206, {"Access-Control-Allow-Origin": "https://other.example"})

        with self.assertLogs("xhs_download.services.unified.transport", level="INFO") as logs:
            self.assertIsNone(browser_readable_link(self.RECORD, "https://download.example", request=request))
        self.assertIn("result=refused links=a.douyinvod.com:OSError,b.douyinvod.com:206:no-cors", logs.output[0])
        self.assertNotIn("https://", logs.output[0])

    def test_a_link_the_check_cannot_reach_is_left_to_the_browser(self):
        def request(url, **options):
            if url.startswith("https://a."):
                raise requests.Timeout("slow")
            return FakeResponse(206, {"Content-Range": "bytes 0-0/72993146"})

        self.assertEqual(browser_readable_link(self.RECORD, "https://download.example", request=request),
                         ("https://a.douyinvod.com/x.mp4", 0))

    def test_a_link_the_server_refuses_to_request_is_never_handed_over(self):
        def request(url, **options):
            raise XhsSecurityError("refused")

        self.assertIsNone(browser_readable_link(self.RECORD, "https://download.example", request=request))


class ResultPageTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_result_region_renders_the_picker_instead_of_video_cards(self):
        result = present(native_work(), platform_adapter=PLATFORM, provider_label="抖音原生",
                         fallback_extensions={"video": "mp4"})
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        html = render_to_string("partials/xhs_results_region.html", {**context, "proxy_image_url": "/media/proxy-image/"})
        soup = BeautifulSoup(html, "html.parser")
        root = soup.select_one("[data-ytdlp-picker]")
        self.assertEqual(root["data-selection-schema"], SCHEMA)
        self.assertEqual(root["data-selection-url"], reverse("service_selected_format"))
        self.assertEqual(len(root.select('[data-picker-radio][data-kind="video"]')), 3)
        self.assertEqual(len(root.select("[data-picker-radio][checked]")), 1)
        cards = [c for c in soup.select('.asset-card[data-type="video"]') if not c.find_parent(attrs={"data-ytdlp-picker": True})]
        self.assertEqual(cards, [])


class RouteSwitchTests(TestCase):
    def test_migration_adds_douyin_route_switched_off(self):
        row = PipelineRoute.objects.get(platform="douyin")
        self.assertFalse(row.enabled)
        self.assertEqual((row.policy, row.floor_provider, row.order[0]), ("floor_and_upgrade", "native", "tiantianjiexi"))
        self.assertIsNone(routes.load_route("douyin"))

    def test_switch_routes_douyin_links_only(self):
        PipelineRoute.objects.filter(platform="douyin").update(enabled=True)
        calls = []

        def legacy(url, **options):
            calls.append(("legacy", url))
            return "legacy"

        with patch("xhs_download.services.unified.douyin.parse", return_value="unified") as unified:
            self.assertEqual(with_unified(legacy)(SOURCE, prefer_ultra_hd=True), "unified")
            self.assertEqual(with_unified(legacy)("https://www.bilibili.com/video/BV1xx411c7mD"), "legacy")
        self.assertTrue(unified.call_args.kwargs["prefer_ultra_hd"])

    def test_unreadable_route_table_means_existing_path(self):
        with patch.object(PipelineRoute.objects, "filter", side_effect=DatabaseError("missing")):
            self.assertIsNone(routes.load_route("douyin"))


class SelectionEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        PipelineRoute.objects.filter(platform="douyin").update(enabled=True)
        picker, _records, _ = build_picker(native_work(), {"video": "mp4"})
        self.choice = picker["videos"][0]
        self.url = reverse("service_selected_format")

    def post(self, action, token=None):
        body = {"candidate_id": self.choice["candidate_id"],
                "resource_token": token or self.choice["resource_token"], "action": action}
        return self.client.post(self.url, json.dumps(body), content_type="application/json")

    def test_download_returns_the_ordinary_item_on_a_readable_mirror(self):
        mirror = "https://v11-colds.douyinvod.com/obj/720.mp4"
        with patch("xhs_download.services.provider_strategy.prepare_browser_transport", side_effect=lambda item: item), \
                patch("xhs_download.services.unified.transport.browser_readable_link",
                      return_value=(mirror, 741828)) as readable:
            response = self.post("download")
        self.assertEqual(response.status_code, 200, response.content)
        item = response.json()["item"]
        self.assertEqual((item["candidate_id"], item["selection_delivery"], item["url"]),
                         (self.choice["candidate_id"], "ordinary", mirror))
        self.assertEqual(readable.call_args.args[1], "http://testserver")
        self.assertEqual(response.json()["server_media_relay"], False)

    def test_download_without_a_readable_mirror_is_refused(self):
        with patch("xhs_download.services.unified.transport.browser_readable_link", return_value=None):
            response = self.post("download")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "SELECTED_FORMAT_BROWSER_UNREADABLE")

    def test_link_returns_the_variant_url(self):
        response = self.post("link")
        self.assertEqual(response.json()["item"]["url"], "https://v11-cold-src.douyinvod.com/obj/720.mp4")

    def test_switching_off_retires_issued_pickers(self):
        PipelineRoute.objects.filter(platform="douyin").update(enabled=False)
        self.assertEqual(self.post("link").status_code, 403)

    def test_other_tokens_still_reach_the_existing_endpoint(self):
        response = self.post("link", token=signing.dumps({"id": "x", "bundle": "y"}, salt="upstream-candidate-info-v1"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("格式信息已失效", response.json()["message"])


class CooldownSettingsTests(SimpleTestCase):
    def setUp(self):
        routes._cooldowns.clear()
        self.addCleanup(routes._cooldowns.clear)

    def test_route_settings_bound_the_book_and_a_change_starts_fresh(self):
        book = routes.cooldowns(route(cooldown_base_seconds=5, cooldown_max_seconds=20))
        self.assertEqual(book.record_unavailable("tiantianjiexi", 0), 5)
        self.assertEqual(book.record_unavailable("tiantianjiexi", 0), 10)
        self.assertIs(routes.cooldowns(route(cooldown_base_seconds=5, cooldown_max_seconds=20)), book)
        fresh = routes.cooldowns(route(cooldown_base_seconds=5, cooldown_max_seconds=8))
        self.assertIsNot(fresh, book)
        self.assertFalse(fresh.cooling("tiantianjiexi", 1))


class ReviewedHostTests(SimpleTestCase):
    """Production, 2026-10-28: TianTian answered from the reviewed exact host and
    the page dropped the only video, because presentation re-checked a suffix list."""

    def setUp(self):
        cache.clear()

    def test_reviewed_exact_douyin_host_reaches_the_picker(self):
        raw = deepcopy(TIANTIAN_RAW)
        raw["data"]["videos"] = [{"url": "https://v3-dy-a-x.ixigua.com/obj/original?mime_type=video_mp4"}]
        work = replace(ServiceTianTianAdapter().map(raw, SOURCE), platform="douyin", provider="tiantianjiexi")
        kept = douyin_media_only(work)
        result = present(kept, platform_adapter=PLATFORM, provider_label="天天解析",
                         fallback_extensions={"video": "mp4"})
        self.assertIsNotNone(result)
        choice = result.metadata["unified_picker"]["videos"][0]
        self.assertEqual(resolve_selection(choice["resource_token"], choice["candidate_id"])["url"],
                         "https://v3-dy-a-x.ixigua.com/obj/original?mime_type=video_mp4")

    def test_hosts_outside_the_douyin_policy_still_never_reach_the_page(self):
        raw = deepcopy(TIANTIAN_RAW)
        raw["data"]["videos"] = [{"url": "https://v3-dy-a-x.ixigua.example/obj/original.mp4"}]
        work = replace(ServiceTianTianAdapter().map(raw, SOURCE), platform="douyin", provider="tiantianjiexi")
        self.assertEqual(douyin_media_only(work).items, ())


class EnrichTests(SimpleTestCase):
    def test_sizes_are_measured_for_files_without_one_and_unknown_stays_unknown(self):
        from dataclasses import replace
        from .services.unified.enrich import with_measured_sizes
        work = native_work()
        item = work.items[0]
        unsized = tuple(replace(v, size_bytes=None, size_exact=False) for v in item.variants)
        work = replace(work, items=(replace(item, variants=unsized),))
        answered = unsized[0].url

        def request(url, **options):
            self.assertEqual(options["headers"]["Range"], "bytes=0-0")
            if url != answered:
                raise requests.Timeout("slow")
            return FakeResponse(206, {"Content-Range": "bytes 0-0/72993146"})

        measured = with_measured_sizes(work, request=request)
        sizes = {v.url: (v.size_bytes, v.size_exact) for v in measured.items[0].variants}
        self.assertEqual(sizes[answered], (72993146, True))
        self.assertTrue(all(size is None for url, (size, _exact) in sizes.items() if url != answered))
        sized = native_work()
        self.assertIs(with_measured_sizes(sized, request=None), sized)  # nothing to measure, no request

    def test_floor_qualities_join_only_a_single_quality_winner(self):
        from dataclasses import replace
        from .services.unified.enrich import with_floor_qualities
        floor = replace(native_work(), provider="native")
        winner_item = replace(floor.items[0], variants=(replace(floor.items[0].variants[0], variant_id="video:1:o",
                              url="https://v26-qingtao.douyinvod.com/obj/original.mp4", quality_label="原画",
                              is_original=True, size_bytes=None, size_exact=False),))
        winner = replace(floor, provider="tiantianjiexi", items=(winner_item,))
        merged = with_floor_qualities(winner, floor)
        urls = [v.url for v in merged.items[0].variants]
        self.assertEqual(urls[0], "https://v26-qingtao.douyinvod.com/obj/original.mp4")
        self.assertEqual(set(urls[1:]), {v.url for v in floor.items[0].variants})
        self.assertTrue(all(v.variant_id.startswith("native:") for v in merged.items[0].variants[1:]))
        self.assertIs(with_floor_qualities(floor, floor), floor)
        several = replace(floor, provider="kukutool")
        self.assertIs(with_floor_qualities(several, replace(winner, provider="native")), several)


    def test_quality_tiers_keep_the_largest_file_per_tier_and_drop_other_heights(self):
        from dataclasses import replace
        from .services.unified.enrich import with_quality_tiers
        base = native_work()
        template = base.items[0].variants[0]

        def file(name, width, height, size, **extra):
            return replace(template, variant_id=f"video:1:{name}", url=f"https://v26.douyinvod.com/{name}.mp4",
                           width=width, height=height, size_bytes=size, size_exact=True, quality_label="", **extra)

        variants = (file("o", None, None, 72993146, is_original=True), file("a", 1280, 720, 4699750),
                    file("b", 1280, 720, 10695000), file("c", 1024, 576, 4100000), file("d", 960, 540, 2800000),
                    file("e", 640, 360, 900000))
        work, hidden = with_quality_tiers(replace(base, items=(replace(base.items[0], variants=variants),)))
        # 576p joins the 540p tier and, being larger, is the file kept there.
        self.assertEqual([v.variant_id for v in work.items[0].variants], ["video:1:o", "video:1:b", "video:1:c"])
        self.assertEqual(hidden, {"https://v26.douyinvod.com/a.mp4": "未展示：同一清晰度已有更大的文件",
                                  "https://v26.douyinvod.com/d.mp4": "未展示：同一清晰度已有更大的文件",
                                  "https://v26.douyinvod.com/e.mp4": "未展示：不在清晰度档位内"})
