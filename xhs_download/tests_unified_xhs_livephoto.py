"""Xiaohongshu live photos on the unified pipeline: the admin's three save modes. No network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import time

from django.core import signing
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from .models import PipelineRoute
from .services.pipeline import Kind, Work
from .services.platforms.xhs_livephoto_builder import build_xhs_livephoto_preview_context
from .services.platforms.xiaohongshu import XiaohongshuAdapter as XhsPlatform
from .services.provider_adapters.xiaohongshu import XiaohongshuMapper
from .services.unified.presentation import present
from .services.unified.xhs_livephoto import as_live_photos, live_photo_mode, without_motions
from .services.unified.xiaohongshu import with_https_media, with_original_images, xhs_media_only
from .services.xhs_livephoto_pair_handoff import (
    XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
    XHS_LIVEPHOTO_PAIR_TTL_SECONDS,
    XhsLivePhotoPairHandoffError,
    _cache_key,
    load_xhs_livephoto_pair_descriptor,
)
from .views import build_video_item

ID = "64a1b2c3d4e5f6789012abcd"
SOURCE = f"https://www.xiaohongshu.com/explore/{ID}"
TOKENS = ("1040g2sg31abcdefghijabcdefghijabcdefghij", "1040g2sg31bbcdefghijabcdefghijabcdefghij")
ORIGINALS = tuple(f"https://ci.xiaohongshu.com/{token}?imageView2/format/jpg" for token in TOKENS)
MASTER = "https://sns-video-v4.xhscdn.com/stream/live/1.mp4"
BACKUP = "https://sns-bak-v1.xhscdn.com/stream/live/1.mp4"
H264 = {"h265": [], "h264": [{"masterUrl": MASTER.replace("https:", "http:"), "backupUrls": [BACKUP],
                               "size": 385650}]}
PLATFORM = XhsPlatform()


def live_raw(stream=H264):
    stills = [f"http://sns-webpic-qc.xhscdn.com/202610271234/0123abcd/{token}!nd_dft_wlteh_webp_3"
              for token in TOKENS]
    return {"note": {"noteDetailMap": {ID: {"note": {"noteId": ID, "type": "normal", "title": "实况测试", "imageList": [
        {"urlDefault": stills[0], "width": 1080, "height": 1440, "livePhoto": True, "stream": deepcopy(stream)},
        {"urlDefault": stills[1], "width": 1080, "height": 1440, "livePhoto": False}]}}}}}


def live_work(stream=H264):
    work = XiaohongshuMapper().map(live_raw(stream), SOURCE)
    assert isinstance(work, Work), work
    return xhs_media_only(replace(with_original_images(with_https_media(work)),
                                  platform="xiaohongshu", provider="native"))


def separate(work):
    return present(work, platform_adapter=PLATFORM, provider_label="小红书原生",
                   fallback_extensions={"video": "mp4", "image": "jpg"})


def motion_item(work):
    return next(item for item in work.items if item.kind is Kind.VIDEO)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LivePhotoModeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_native_names_each_stream_rows_codec(self):
        work = live_work()
        motion = motion_item(work)
        source = work.field_sources[f"items.{motion.item_id}.variants.{motion.variants[0].variant_id}.url"]
        self.assertIn(".stream.h264[0].masterUrl", source)

    def test_pairs_take_the_existing_livephoto_shape(self):
        work = live_work()
        result = as_live_photos(work, separate(work))
        self.assertEqual(result.post_type, "multimedia")
        self.assertEqual([(a.media_type, a.asset_id.split(":", 2)[2]) for a in result.assets],
                         [("image", "i0:still"), ("video", "i0:motion"), ("image", "i1:still")])
        self.assertEqual([a.url for a in result.image_assets], list(ORIGINALS))
        self.assertEqual(result.video_assets[0].url, MASTER)
        self.assertEqual({a.provider for a in result.assets}, {"xhs_livephoto_preview_v1"})
        self.assertEqual(len(result.rich_media.groups), 1)
        items = result.metadata["xhs_livephoto_items"]
        self.assertEqual([(i["kind"], i["pair_download_status"]) for i in items],
                         [("live_photo", "ready"), ("regular_image", "not_applicable")])
        self.assertEqual(result.metadata["xhs_live_photo_mode"], "livephoto")

    def test_the_existing_cards_hand_each_pair_to_its_own_client(self):
        work = live_work()
        result = as_live_photos(work, separate(work))
        context = build_xhs_livephoto_preview_context(result, client_identity="client-a")
        live, regular = context["items"]
        self.assertTrue(live["pair_token"] and live["motion_token"])
        self.assertEqual((regular["kind"], regular["pair_token"]), ("regular_image", ""))
        descriptor = load_xhs_livephoto_pair_descriptor(live["pair_token"], client_identity="client-a")
        self.assertEqual((descriptor.still["url"], descriptor.motion["url"]), (ORIGINALS[0], MASTER))
        self.assertTrue(descriptor.archive_filename.endswith("_01_livephoto.zip"))
        with self.assertRaises(XhsLivePhotoPairHandoffError) as other:
            load_xhs_livephoto_pair_descriptor(live["pair_token"], client_identity="client-b")
        self.assertEqual(other.exception.error_code, "XHS_LIVEPHOTO_PAIR_CLIENT_MISMATCH")

    def test_a_motion_the_livephoto_feature_has_not_reviewed_keeps_the_note_separate(self):
        h265_only = {"h264": [], "h265": [{"masterUrl": MASTER}]}
        unverified_host = {"h264": [{"masterUrl": "https://sns-video-bd.xhscdn.com/live.mp4"}]}
        for stream in (h265_only, unverified_host):
            with self.subTest(stream=next(key for key, rows in stream.items() if rows)):
                work = live_work(stream)
                self.assertEqual([g.kind for g in work.groups], ["live_photo"])
                self.assertIsNone(as_live_photos(work, separate(work)))

    def test_still_only_drops_the_motions_and_keeps_every_photo(self):
        work = without_motions(live_work())
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE])
        self.assertEqual(work.groups, ())
        self.assertEqual([a.url for a in separate(work).assets], list(ORIGINALS))

    def test_separate_numbers_each_motion_after_its_photo(self):
        result = separate(live_work())
        self.assertEqual(result.metadata["video_display_ordinals"], {MASTER: 1})
        card = build_video_item(result.video_assets[0], result, probe_missing_size=False)
        self.assertEqual((card["display_ordinal"], card["filename"]), (1, f"{result.base_filename}_01.mp4"))

    def test_tokens_last_thirty_minutes_and_never_past_the_media_links(self):
        work = live_work()
        motion = motion_item(work)
        soon = datetime.fromtimestamp(time.time() + 60, tz=timezone.utc)
        expiring = replace(work, items=tuple(
            replace(item, variants=tuple(replace(v, expires_at=soon) for v in item.variants))
            if item.item_id == motion.item_id else item for item in work.items))
        for shown, ceiling in ((work, time.time() + XHS_LIVEPHOTO_PAIR_TTL_SECONDS), (expiring, soon.timestamp())):
            result = as_live_photos(shown, separate(shown))
            token = build_xhs_livephoto_preview_context(result, client_identity="client-a")["items"][0]["pair_token"]
            ticket = signing.loads(token, salt=XHS_LIVEPHOTO_PAIR_TOKEN_SALT)["ticket_id"]
            self.assertAlmostEqual(cache.get(_cache_key(ticket))["expires_at"], ceiling, delta=5)
        self.assertEqual(XHS_LIVEPHOTO_PAIR_TTL_SECONDS, 1800)

    def test_unknown_modes_read_as_livephoto(self):
        self.assertEqual(live_photo_mode(PipelineRoute(live_photo_mode="")), "livephoto")
        self.assertEqual(live_photo_mode(PipelineRoute(live_photo_mode="separate")), "separate")


class LivePhotoRouteTests(TestCase):
    def test_migration_starts_every_route_on_livephoto(self):
        self.assertEqual(set(PipelineRoute.objects.values_list("live_photo_mode", flat=True)), {"livephoto"})
