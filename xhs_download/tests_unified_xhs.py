"""Xiaohongshu on the unified pipeline: facts, admission, live photos. No network."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from .models import PipelineRoute
from .services.pipeline import Failure, FailureKind, Kind, Work
from .services.platforms.xiaohongshu import XiaohongshuAdapter as XhsPlatform
from .services.provider_adapters.xiaohongshu import XiaohongshuMapper
from .services.unified import routes
from .services.unified.entry import with_unified
from .services.unified.presentation import present
from .services.unified.xiaohongshu import (
    GatedService, NativeOnce, is_ordinary_video, with_https_media, with_original_images, xhs_media_only,
)
from .templatetags.ytdlp_picker import remaining_picker_videos, ytdlp_presentation
from .views import build_result_display_context

ID = "64a1b2c3d4e5f6789012abcd"
SOURCE = f"https://www.xiaohongshu.com/explore/{ID}"
TOKEN = "1040g2sg31abcdefghijabcdefghijabcdefghij"
WB_DFT = f"http://sns-webpic-qc.xhscdn.com/202610271234/0123abcd/{TOKEN}!nd_dft_wlteh_webp_3"
ORIGINAL = f"https://ci.xiaohongshu.com/{TOKEN}?imageView2/format/jpg"
FIXTURES = Path(__file__).parent / "fixtures/provider_raw/xhs_native"
PLATFORM = XhsPlatform()


def gallery_raw():
    return {"note": {"noteDetailMap": {ID: {"note": {
        "noteId": ID, "type": "normal", "title": "原图测试", "imageList": [
            {"urlDefault": WB_DFT, "width": 2358, "height": 2948,
             "infoList": [{"imageScene": "WB_DFT", "url": WB_DFT}]}],
    }}}}}


def fixture(name):
    return json.loads((FIXTURES / f"synthetic_{name}.json").read_text())["raw"]


def native(raw):
    work = XiaohongshuMapper().map(deepcopy(raw), SOURCE)
    assert isinstance(work, Work), work
    return replace(with_original_images(with_https_media(work)), platform="xiaohongshu", provider="native")


class XhsFactsTests(SimpleTestCase):
    def test_photo_original_comes_first_with_the_rows_dimensions(self):
        item = native(gallery_raw()).items[0]
        original = item.variants[0]
        self.assertEqual((original.url, original.container, original.width, original.height, original.is_original),
                         (ORIGINAL, "jpg", 2358, 2948, True))
        self.assertIs(item.default_variant, original)
        self.assertTrue(all(v.url.startswith("https://") for v in item.variants))

    def test_no_original_is_invented_when_the_rule_has_none(self):
        work = native(fixture("gallery"))
        ordinary = work.items[-1]
        self.assertFalse(any(v.url.startswith("https://ci.xiaohongshu.com") for v in ordinary.variants))

    def test_live_photo_keeps_its_group_through_the_policy(self):
        work = xhs_media_only(native(fixture("gallery")))
        self.assertEqual([g.kind for g in work.groups], ["live_photo"])
        self.assertFalse(is_ordinary_video(work))
        self.assertTrue(is_ordinary_video(native(fixture("video"))))


class XhsPresentationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_gallery_photos_are_original_cards(self):
        result = present(native(gallery_raw()), platform_adapter=PLATFORM, provider_label="小红书原生",
                         fallback_extensions={"image": "jpg"})
        self.assertEqual([(a.media_type, a.url, a.ext) for a in result.assets], [("image", ORIGINAL, "jpg")])

    def test_live_motion_is_a_card_with_its_still_as_cover_not_a_picker(self):
        work = xhs_media_only(native(fixture("gallery")))
        result = present(work, platform_adapter=PLATFORM, provider_label="小红书原生",
                         fallback_extensions={"video": "mp4", "image": "jpg"})
        self.assertNotIn("unified_picker", result.metadata)
        motion = result.video_assets[0]
        self.assertEqual(motion.url, "https://sns-video-bd.xhscdn.com/motion.mp4")
        self.assertEqual(motion.cover_url, result.image_assets[0].url)
        display = build_result_display_context(result, probe_missing_video_sizes=False)
        picker = ytdlp_presentation(display["upstream_candidates"], {}, display["video_items"], "", "",
                                    display["unified_picker"])
        self.assertEqual([v["video_url"] for v in remaining_picker_videos(picker, display["video_items"])],
                         [motion.url])

    def test_pickers_leave_live_motion_cards_in_place(self):
        live = xhs_media_only(native(fixture("gallery")))
        video = native(fixture("video"))
        mixed = replace(live, items=(*live.items, replace(video.items[0], item_id="video:9", index=9)))
        result = present(mixed, platform_adapter=PLATFORM, provider_label="小红书原生",
                         fallback_extensions={"video": "mp4", "image": "jpg"})
        display = build_result_display_context(result, probe_missing_video_sizes=False)
        picker = ytdlp_presentation([], {}, display["video_items"], "", "", display["unified_picker"])
        self.assertEqual([v["video_url"] for v in remaining_picker_videos(picker, display["video_items"])],
                         ["https://sns-video-bd.xhscdn.com/motion.mp4"])

    def test_topics_show_once_beside_the_description(self):
        work = xhs_media_only(native(fixture("gallery")))
        result = present(work, platform_adapter=PLATFORM, provider_label="小红书原生",
                         fallback_extensions={"video": "mp4", "image": "jpg"})
        display = build_result_display_context(result, probe_missing_video_sizes=False)
        self.assertEqual(display["display_topics"], list(work.topics))
        self.assertTrue(work.topics)
        self.assertNotIn("#", display["display_description"])

    def test_technical_rows_explain_every_link_of_the_shown_result(self):
        from .services.unified.presentation import technical_details
        shown = native(fixture("gallery"))
        kept = xhs_media_only(shown)
        details = technical_details(shown, kept, policy="floor_and_upgrade", duration_ms=12.6,
                                    attempts=[{"provider": "native", "outcome": "success", "code": None}])
        self.assertEqual((details["policy"], details["shown"], details["duration_ms"]),
                         ("floor_and_upgrade", "native", 12))
        self.assertEqual(details["attempts"], [{"provider": "native", "outcome": "success", "code": ""}])
        rows = details["variants"]
        self.assertEqual(len(rows), sum(len(item.variants) for item in shown.items))
        self.assertTrue(all("://" not in row["host"] for row in rows))
        statuses = {row["status"] for row in rows}
        self.assertIn("展示为卡片", statuses)
        self.assertTrue(statuses <= {"展示为卡片", "未展示：同一内容的其他版本", "同一文件的镜像",
                                     "平台媒体策略未放行", "链接不是 https", "可下载"}, statuses)


class StubOnce:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def get(self, budget):
        self.calls += 1
        return self.result


class RecordingService:
    def __init__(self, outcome):
        self.outcome, self.urls = outcome, []

    def parse(self, url, budget):
        self.urls.append(url)
        return self.outcome


class XhsAdmissionTests(SimpleTestCase):
    def gate(self, native_result, service, **admitted):
        built = []

        def build(note_id, for_video):
            built.append((note_id, for_video))
            return service
        return GatedService(build, StubOnce(native_result), **admitted), built

    def test_video_note_runs_the_enhancer_on_the_canonical_note(self):
        video = native(fixture("video"))
        service = RecordingService(replace(video, provider="tiantianjiexi"))
        gate, built = self.gate(video, service, video_admitted=True, fallback_admitted=False)
        self.assertIsInstance(gate.parse(SOURCE, 5), Work)
        self.assertEqual((built, service.urls), ([(ID, True)], [SOURCE]))

    def test_gallery_never_calls_the_service(self):
        service = RecordingService(None)
        gate, built = self.gate(native(gallery_raw()), service, video_admitted=True, fallback_admitted=False)
        self.assertEqual(gate.parse(SOURCE, 5).code, "XHS_SERVICE_NOT_ADMITTED")
        self.assertEqual((built, service.urls), ([], []))

    def test_native_failure_uses_only_an_admitted_fallback(self):
        down = Failure(FailureKind.UNAVAILABLE, "XHS_UNAVAILABLE")
        gate, built = self.gate(down, RecordingService(native(gallery_raw())), video_admitted=True, fallback_admitted=False)
        self.assertEqual(gate.parse(SOURCE, 5).kind, FailureKind.UNSUPPORTED)
        gate, built = self.gate(down, RecordingService(native(gallery_raw())), video_admitted=False, fallback_admitted=True)
        self.assertIsInstance(gate.parse(SOURCE, 5), Work)
        self.assertEqual(built, [(ID, False)])

    def test_a_service_result_for_another_note_is_rejected(self):
        video = native(fixture("video"))
        other = replace(video, title="完全不同的标题", provider="tiantianjiexi")
        gate, _ = self.gate(video, RecordingService(other), video_admitted=True, fallback_admitted=False)
        self.assertEqual(gate.parse(SOURCE, 5).code, "XHS_SERVICE_TITLE_MISMATCH")

    def test_native_page_is_read_once_per_request(self):
        calls = []

        def parse(self, url, budget):
            calls.append(url)
            time.sleep(0.05)
            return XiaohongshuMapper().map(deepcopy(gallery_raw()), SOURCE)

        once, results = NativeOnce(SOURCE), []
        with patch("xhs_download.services.unified.xiaohongshu.NativeXhsAdapter.parse", parse):
            threads = [threading.Thread(target=lambda: results.append(once.get(2))) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(results[0].items[0].variants[0].url, ORIGINAL)


class XhsRouteTests(TestCase):
    def test_migration_adds_the_route_switched_off(self):
        row = PipelineRoute.objects.get(platform="xiaohongshu")
        self.assertFalse(row.enabled)
        self.assertEqual((row.policy, row.floor_provider), ("floor_and_upgrade", "native"))
        self.assertIsNone(routes.load_route("xiaohongshu"))

    def test_switch_passes_the_existing_admission_inputs(self):
        PipelineRoute.objects.filter(platform="xiaohongshu").update(enabled=True)
        controller = object()
        with patch("xhs_download.services.unified.xiaohongshu.parse", return_value="unified") as unified:
            self.assertEqual(with_unified(lambda url, **o: "legacy")(
                SOURCE, prefer_ultra_hd=True, ultra_attempt_controller=controller,
                xhs_service_provider_ids=("kukutool_generic",)), "unified")
        self.assertEqual(unified.call_args.kwargs, {"prefer_ultra_hd": True, "attempt_controller": controller,
                                                    "service_ids": ("kukutool_generic",)})
