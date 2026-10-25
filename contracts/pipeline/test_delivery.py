"""Unified pipeline §3: one rule table decides delivery from variant facts (INV-03)."""
from unittest import TestCase

from xhs_download.services.pipeline import (
    BLOB_LIMIT_BYTES,
    ChainConfig,
    Fetchable,
    Kind,
    Method,
    Tracks,
    decide_archive,
    decide_delivery,
)

from .fakes import FakeProvider, simulate, variant, video_work

T, F, U = Fetchable.TRUE, Fetchable.FALSE, Fetchable.UNKNOWN


class DeliveryRuleContract(TestCase):
    def test_rule_1_split_tracks_with_partner_merge_on_the_server(self):
        split = variant(tracks=Tracks.VIDEO_ONLY, merge_with="a1", height=2160, fetchable=T)
        delivery = decide_delivery(Kind.VIDEO, split)
        self.assertEqual((delivery.rule, delivery.method), (1, Method.SERVER_MERGE))
        self.assertTrue(delivery.counts_against_quota)

    def test_rule_2_required_headers_go_through_the_server(self):
        for kind in (Kind.VIDEO, Kind.IMAGE, Kind.AUDIO):
            with self.subTest(kind=kind):
                needs_referer = variant(required_headers=("Referer",), fetchable=T)
                delivery = decide_delivery(kind, needs_referer)
                self.assertEqual((delivery.rule, delivery.method), (2, Method.SERVER_ATTACHMENT))
                self.assertTrue(delivery.counts_against_quota)

    def test_rule_3_several_images_are_packed_by_the_browser(self):
        entries = [(Kind.IMAGE, variant(f"p{n}", container="jpg", fetchable=T)) for n in (1, 2, 3)]
        delivery = decide_archive(entries)
        self.assertEqual((delivery.rule, delivery.method, delivery.fallback),
                         (3, Method.BROWSER_ARCHIVE, None))
        self.assertFalse(delivery.counts_against_quota)

    def test_rule_3_live_photo_pairs_pack_in_the_browser_too(self):
        entries = [(Kind.IMAGE, variant("still", container="jpg", fetchable=T)),
                   (Kind.VIDEO, variant("motion", container="mov", fetchable=T))]
        self.assertEqual(decide_archive(entries).method, Method.BROWSER_ARCHIVE)

    def test_rule_3_the_server_packs_only_what_the_browser_cannot_read(self):
        cases = [
            ("headers", variant("p2", required_headers=("Cookie",), fetchable=T),
             (2, Method.SERVER_ATTACHMENT, None)),
            ("unreadable", variant("p2", fetchable=F), (3, Method.SERVER_ATTACHMENT, None)),
            ("unknown", variant("p2", fetchable=U),
             (3, Method.BROWSER_ARCHIVE, Method.SERVER_ATTACHMENT)),
        ]
        for name, second, expected in cases:
            with self.subTest(name):
                entries = [(Kind.IMAGE, variant("p1", fetchable=T)), (Kind.IMAGE, second)]
                delivery = decide_archive(entries)
                self.assertEqual((delivery.rule, delivery.method, delivery.fallback), expected)
                self.assertTrue(delivery.archive)
        entries = [(Kind.IMAGE, variant("p1", fetchable=T)), (Kind.IMAGE, variant("p2", fetchable=U))]
        self.assertIsNone(decide_archive(entries, allow_server_fallback=False).fallback)

    def test_rule_3_needs_two_items_and_no_merge(self):
        with self.assertRaises(ValueError):
            decide_archive([(Kind.IMAGE, variant())])
        split = variant("v", tracks=Tracks.VIDEO_ONLY, merge_with="a")
        delivery = decide_archive([(Kind.IMAGE, variant("p")), (Kind.VIDEO, split)])
        self.assertEqual((delivery.method, delivery.reason), (Method.NONE, "ARCHIVE_NEEDS_MERGE"))

    def test_rule_4_readable_video_and_audio_stream_in_the_browser_with_bounded_blob(self):
        for kind, tracks in ((Kind.VIDEO, Tracks.AV), (Kind.AUDIO, Tracks.AUDIO_ONLY)):
            with self.subTest(kind=kind):
                delivery = decide_delivery(kind, variant(tracks=tracks, fetchable=T))
                self.assertEqual((delivery.rule, delivery.method, delivery.fallback),
                                 (4, Method.BROWSER_STREAMING, Method.BROWSER_BLOB))
                self.assertFalse(delivery.counts_against_quota)
                self.assertFalse(delivery.fallback_counts_against_quota)
        huge = variant(fetchable=T, size_bytes=BLOB_LIMIT_BYTES + 1)
        self.assertIsNone(decide_delivery(Kind.VIDEO, huge).fallback)

    def test_rule_5_single_image_downloads_directly(self):
        for fetchable in (T, F, U):
            with self.subTest(fetchable=fetchable):
                delivery = decide_delivery(Kind.IMAGE, variant(container="jpg", fetchable=fetchable))
                self.assertEqual((delivery.rule, delivery.method), (5, Method.BROWSER_DIRECT))

    def test_rule_6_unknown_readability_tries_the_browser_then_the_switchable_server(self):
        delivery = decide_delivery(Kind.VIDEO, variant(fetchable=U))
        self.assertEqual((delivery.rule, delivery.method, delivery.fallback),
                         (6, Method.BROWSER_STREAMING, Method.SERVER_ATTACHMENT))
        self.assertFalse(delivery.counts_against_quota)
        self.assertTrue(delivery.fallback_counts_against_quota)
        switched_off = decide_delivery(Kind.VIDEO, variant(fetchable=U), allow_server_fallback=False)
        self.assertIsNone(switched_off.fallback)

    def test_rule_7_otherwise_no_download_and_a_reason(self):
        delivery = decide_delivery(Kind.VIDEO, variant(fetchable=F))
        self.assertEqual((delivery.rule, delivery.method, delivery.reason),
                         (7, Method.NONE, "BROWSER_CANNOT_READ"))

    def test_unknown_tracks_are_delivered_as_complete_files(self):
        cases = [
            (dict(fetchable=T), (4, Method.BROWSER_STREAMING)),
            (dict(fetchable=U), (6, Method.BROWSER_STREAMING)),
            (dict(fetchable=F), (7, Method.NONE)),
            (dict(fetchable=T, required_headers=("Referer",)), (2, Method.SERVER_ATTACHMENT)),
            (dict(fetchable=T, merge_with="a1"), (4, Method.BROWSER_STREAMING)),
        ]
        for facts, expected in cases:
            with self.subTest(facts=facts):
                delivery = decide_delivery(Kind.VIDEO, variant(tracks=Tracks.UNKNOWN, **facts))
                self.assertEqual((delivery.rule, delivery.method), expected)

    def test_rules_apply_in_table_order(self):
        both = variant(tracks=Tracks.VIDEO_ONLY, merge_with="a", required_headers=("Referer",))
        self.assertEqual(decide_delivery(Kind.VIDEO, both).rule, 1)
        headers_and_readable = variant(required_headers=("Referer",), fetchable=T)
        self.assertEqual(decide_delivery(Kind.VIDEO, headers_and_readable).rule, 2)


class SourceIndependenceContract(TestCase):
    """Same facts, different Provider, platform or resource ID: same delivery."""

    FACTS = [
        dict(height=1080, fetchable=T),
        dict(height=1080, fetchable=U),
        dict(height=1080, fetchable=F),
        dict(height=1080, required_headers=("Referer",), fetchable=T),
    ]

    def delivered(self, provider_name, platform, variant_id, facts):
        work = video_work(variants=(variant(variant_id, **facts),))
        config = ChainConfig(platform=platform, order=(provider_name,))
        run, _ = simulate(config, {provider_name: FakeProvider(1, work)})
        chosen = run.shown.items[0].default_variant
        return decide_delivery(run.shown.items[0].kind, chosen)

    def test_provider_platform_and_id_prefix_never_change_the_delivery(self):
        for facts in self.FACTS:
            with self.subTest(facts=facts):
                baseline = self.delivered("provider-a", "example", "native-1", facts)
                for name, platform, variant_id in (
                    ("provider-b", "example", "native-1"),
                    ("provider-a", "another", "native-1"),
                    ("provider-a", "example", "upstream:abc"),
                ):
                    self.assertEqual(self.delivered(name, platform, variant_id, facts), baseline)
