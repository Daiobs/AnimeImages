"""Unified pipeline §2.2/§2.3: admin-configured chains, failures, INV-01/02 in general form."""
from unittest import TestCase

from xhs_download.services.pipeline import (
    COOLDOWN_CEILING_SECONDS,
    ChainConfig,
    Completeness,
    CooldownBook,
    FailureKind,
    Policy,
    Tracks,
    Work,
)

from .fakes import FakeProvider, failure, simulate, variant, video_work


def first_success(*order, budget=5.0):
    return ChainConfig(platform="example", order=order, budget_seconds=budget)


def floor_and_upgrade(*order, floor="floor", budget=5.0, window=20.0):
    return ChainConfig(platform="example", order=order, policy=Policy.FLOOR_AND_UPGRADE,
                       budget_seconds=budget, floor_provider=floor,
                       upgrade_window_seconds=window)


class FirstSuccessContract(TestCase):
    def test_first_success_in_admin_order_wins_and_later_providers_never_start(self):
        providers = {
            "a": FakeProvider(1, video_work(720)),
            "b": FakeProvider(1, video_work(1080)),
        }
        run, timeline = simulate(first_success("a", "b"), providers)
        self.assertEqual(run.shown.provider, "a")
        self.assertEqual(run.shown.completeness, Completeness.FINAL)
        self.assertEqual(timeline.started(), ["a"])

    def test_changing_the_admin_order_changes_the_result(self):
        providers = {
            "a": FakeProvider(1, video_work(720)),
            "b": FakeProvider(1, video_work(1080)),
        }
        run, _ = simulate(first_success("b", "a"), providers)
        self.assertEqual(run.shown.provider, "b")

    def test_a_leftover_floor_setting_does_not_drop_a_provider_from_first_success(self):
        config = ChainConfig(platform="example", order=("a", "b"), floor_provider="a")
        providers = {"a": FakeProvider(1, video_work(720)), "b": FakeProvider(1, video_work())}
        run, timeline = simulate(config, providers)
        self.assertEqual((timeline.started(), run.shown.provider), (["a"], "a"))

    def test_routing_sets_platform_and_provider_not_the_adapter(self):
        returned = video_work(720, platform="adapter-guess", provider="")
        run, _ = simulate(first_success("a"), {"a": FakeProvider(1, returned)})
        self.assertEqual((run.shown.platform, run.shown.provider), ("example", "a"))

    def test_each_failure_kind_moves_on_except_not_found(self):
        for kind in ("unsupported", "unavailable", "timeout", "invalid_result"):
            with self.subTest(kind=kind):
                providers = {
                    "a": FakeProvider(1, failure(kind, "UPSTREAM_SAID_NO")),
                    "b": FakeProvider(1, video_work(720)),
                }
                run, timeline = simulate(first_success("a", "b"), providers)
                self.assertEqual(run.shown.provider, "b")
                self.assertEqual(timeline.started(), ["a", "b"])
                self.assertEqual(run.attempts[0].outcome, kind)
                self.assertEqual(run.attempts[0].code, "UPSTREAM_SAID_NO")

    def test_not_found_stops_the_chain_and_reaches_the_user(self):
        providers = {
            "a": FakeProvider(1, failure("not_found", "WORK_DELETED")),
            "b": FakeProvider(1, video_work(720)),
        }
        run, timeline = simulate(first_success("a", "b"), providers)
        self.assertIsNone(run.shown)
        self.assertEqual((run.failure.kind, run.failure.code),
                         (FailureKind.NOT_FOUND, "WORK_DELETED"))
        self.assertEqual(timeline.started(), ["a"])

    def test_unmappable_results_become_invalid_result_and_the_chain_moves_on(self):
        cases = {
            "INVALID_PLACEHOLDER_QUALITY_LABEL": video_work(
                variants=(variant("v1", quality_label="video"),)),
            "INVALID_PLACEHOLDER_TITLE": video_work(title="example"),
            "INVALID_NO_ITEMS": Work(source_url="https://example.test/w", work_id="w", items=()),
            "ADAPTER_EXCEPTION": RuntimeError("raw upstream blew up"),
            "ADAPTER_RETURNED_UNKNOWN": {"raw": "upstream json"},
        }
        for code, returned in cases.items():
            with self.subTest(code=code):
                providers = {
                    "a": FakeProvider(1, returned),
                    "b": FakeProvider(1, video_work(720)),
                }
                run, _ = simulate(first_success("a", "b"), providers)
                self.assertEqual(run.shown.provider, "b")
                self.assertEqual(run.attempts[0].outcome, "invalid_result")
                self.assertEqual(run.attempts[0].code, code)

    def test_each_provider_gets_the_remaining_budget(self):
        providers = {
            "a": FakeProvider(2, failure("unavailable")),
            "b": FakeProvider(1, video_work(720)),
        }
        _, timeline = simulate(first_success("a", "b", budget=5), providers)
        self.assertEqual(timeline.budgets(), {"a": 5, "b": 3})

    def test_exhausted_chains_report_a_safe_summary(self):
        cases = [
            ({"a": FakeProvider(1, failure("unsupported")),
              "b": FakeProvider(1, failure("unsupported"))},
             FailureKind.UNSUPPORTED, "NO_PROVIDER_SUPPORTS_URL"),
            ({"a": FakeProvider(1, failure("unsupported")),
              "b": FakeProvider(1, failure("unavailable"))},
             FailureKind.UNAVAILABLE, "ALL_PROVIDERS_FAILED"),
            ({"a": FakeProvider(9, video_work(720)),
              "b": FakeProvider(1, video_work(720))},
             FailureKind.TIMEOUT, "CHAIN_BUDGET_EXHAUSTED"),
        ]
        for providers, kind, code in cases:
            with self.subTest(code=code):
                run, _ = simulate(first_success("a", "b"), providers)
                self.assertIsNone(run.shown)
                self.assertEqual((run.failure.kind, run.failure.code), (kind, code))


class FloorAndUpgradeContract(TestCase):
    """INV-01 written for any platform: floor from t=0, budget, late upgrade."""

    def test_preferred_result_within_budget_is_shown_and_the_floor_never_is(self):
        providers = {
            "floor": FakeProvider(0.8, video_work(720)),
            "primary": FakeProvider(3, video_work(1080)),
            "secondary": FakeProvider(1, video_work(2160)),
        }
        run, timeline = simulate(floor_and_upgrade("primary", "secondary"), providers)
        self.assertEqual(timeline.started(), ["floor", "primary"])
        self.assertEqual(timeline.shows(), [(3, "primary", "final")])
        self.assertTrue(run.done)

    def test_floor_is_shown_provisionally_at_budget_and_a_better_late_result_replaces_it(self):
        providers = {
            "floor": FakeProvider(0.8, video_work(720)),
            "primary": FakeProvider(9, video_work(1080)),
            "secondary": FakeProvider(1, video_work(2160)),
        }
        run, timeline = simulate(floor_and_upgrade("primary", "secondary"), providers)
        self.assertEqual(timeline.shows(), [(5, "floor", "provisional"), (9, "primary", "final")])
        self.assertEqual(timeline.started(), ["floor", "primary"])
        self.assertEqual(run.shown.provider, "primary")

    def test_late_result_that_is_not_better_ends_the_work_and_the_floor_becomes_final(self):
        providers = {
            "floor": FakeProvider(0.8, video_work(1080)),
            "primary": FakeProvider(9, video_work(720)),
            "secondary": FakeProvider(1, video_work(2160)),
        }
        run, timeline = simulate(floor_and_upgrade("primary", "secondary"), providers)
        self.assertEqual(timeline.shows(), [(5, "floor", "provisional")])
        self.assertEqual(timeline.started(), ["floor", "primary"])
        self.assertEqual((run.shown.provider, run.shown.completeness),
                         ("floor", Completeness.FINAL))

    def test_first_come_first_served_after_a_preferred_failure(self):
        providers = {
            "floor": FakeProvider(0.8, video_work(720)),
            "primary": FakeProvider(6, failure("unavailable")),
            "secondary": FakeProvider(2, video_work(1080)),
            "tertiary": FakeProvider(1, video_work(2160)),
        }
        run, timeline = simulate(floor_and_upgrade("primary", "secondary", "tertiary"), providers)
        self.assertEqual(timeline.started(), ["floor", "primary", "secondary"])
        self.assertEqual(timeline.budgets()["secondary"], 14)
        self.assertEqual(timeline.shows(), [(5, "floor", "provisional"), (8, "secondary", "final")])
        self.assertTrue(run.done)

    def test_floor_slower_than_budget_is_shown_when_it_arrives(self):
        providers = {
            "floor": FakeProvider(7, video_work(720)),
            "primary": FakeProvider(12, video_work(1080)),
        }
        _, timeline = simulate(floor_and_upgrade("primary"), providers)
        self.assertEqual(timeline.shows(), [(7, "floor", "provisional"), (12, "primary", "final")])

    def test_without_a_floor_result_the_first_late_result_is_shown(self):
        providers = {
            "floor": FakeProvider(1, failure("unavailable")),
            "primary": FakeProvider(9, video_work(720)),
        }
        _, timeline = simulate(floor_and_upgrade("primary"), providers)
        self.assertEqual(timeline.shows(), [(9, "primary", "final")])

    def test_floor_is_final_at_once_when_no_preferred_provider_is_left(self):
        providers = {
            "floor": FakeProvider(3, video_work(720)),
            "primary": FakeProvider(1, failure("unsupported")),
            "secondary": FakeProvider(1, failure("unavailable")),
        }
        _, timeline = simulate(floor_and_upgrade("primary", "secondary"), providers)
        self.assertEqual(timeline.shows(), [(3, "floor", "final")])

    def test_upgrade_window_end_makes_the_shown_result_final(self):
        providers = {
            "floor": FakeProvider(1, video_work(720)),
            "primary": FakeProvider(30, video_work(1080)),
        }
        run, timeline = simulate(floor_and_upgrade("primary", window=20), providers)
        self.assertEqual(timeline.shows(), [(5, "floor", "provisional")])
        self.assertEqual((run.shown.provider, run.shown.completeness),
                         ("floor", Completeness.FINAL))

    def test_not_found_before_anything_is_shown_stops_everything(self):
        providers = {
            "floor": FakeProvider(1, failure("not_found", "WORK_DELETED")),
            "primary": FakeProvider(3, video_work(1080)),
        }
        run, timeline = simulate(floor_and_upgrade("primary"), providers)
        self.assertEqual(timeline.shows(), [])
        self.assertEqual(run.failure.kind, FailureKind.NOT_FOUND)
        self.assertEqual(run.running, frozenset({"primary"}))

    def test_changing_the_admin_order_changes_who_upgrades(self):
        providers = {
            "floor": FakeProvider(0.8, video_work(720)),
            "primary": FakeProvider(9, video_work(1080)),
            "secondary": FakeProvider(8, video_work(1440)),
        }
        run, _ = simulate(floor_and_upgrade("secondary", "primary"), providers)
        self.assertEqual(run.shown.provider, "secondary")

    def test_split_track_upgrade_ranks_by_its_merged_quality(self):
        split = video_work(variants=(
            variant("v2160", height=2160, tracks=Tracks.VIDEO_ONLY, merge_with="a1"),
            variant("a1", tracks=Tracks.AUDIO_ONLY, container="m4a"),
        ))
        providers = {
            "floor": FakeProvider(0.8, video_work(1080)),
            "primary": FakeProvider(9, split),
        }
        run, _ = simulate(floor_and_upgrade("primary"), providers)
        self.assertEqual(run.shown.provider, "primary")

    def test_floor_policy_needs_a_floor_and_a_window_covering_the_budget(self):
        with self.assertRaises(ValueError):
            ChainConfig(platform="example", order=("a",), policy=Policy.FLOOR_AND_UPGRADE)
        with self.assertRaises(ValueError):
            floor_and_upgrade("a", budget=5, window=4)


class BoundedCooldownContract(TestCase):
    """INV-02: unavailable cools a Provider down for a bounded, visible time."""

    def test_repeated_failures_never_exceed_the_configured_or_hard_ceiling(self):
        book = CooldownBook(base_seconds=30, max_seconds=300)
        seconds = [book.record_unavailable("a", now=0) for _ in range(40)]
        self.assertEqual(seconds[:4], [30, 60, 120, 240])
        self.assertEqual(max(seconds), 300)
        greedy = CooldownBook(base_seconds=30, max_seconds=10 ** 9)
        self.assertLessEqual(max(greedy.record_unavailable("a", now=0) for _ in range(40)),
                             COOLDOWN_CEILING_SECONDS)

    def test_cooling_provider_is_skipped_visibly_then_retried_after_expiry_without_admin(self):
        book = CooldownBook(base_seconds=30, max_seconds=300)
        providers = {
            "a": FakeProvider(1, failure("unavailable")),
            "b": FakeProvider(1, video_work(720)),
        }
        simulate(first_success("a", "b"), providers, book)
        self.assertEqual(book.snapshot(now=10), {"a": 31})

        run, timeline = simulate(first_success("a", "b"), providers, book, start_at=10)
        self.assertEqual(timeline.started(), ["b"])
        self.assertEqual(run.attempts[0].outcome, "cooling_down")

        providers["a"].outcome = video_work(720)
        run, timeline = simulate(first_success("a", "b"), providers, book, start_at=31)
        self.assertEqual(timeline.started(), ["a"])
        self.assertEqual(run.shown.provider, "a")
        self.assertEqual(book.snapshot(now=31), {})

    def test_only_unavailable_starts_a_cooldown(self):
        for kind in ("unsupported", "timeout", "invalid_result"):
            with self.subTest(kind=kind):
                book = CooldownBook(base_seconds=30, max_seconds=300)
                providers = {
                    "a": FakeProvider(1, failure(kind)),
                    "b": FakeProvider(1, video_work(720)),
                }
                simulate(first_success("a", "b"), providers, book)
                self.assertEqual(book.snapshot(now=1), {})

    def test_all_providers_cooling_is_reported_not_hidden(self):
        book = CooldownBook(base_seconds=30, max_seconds=300)
        book.record_unavailable("a", now=0)
        run, timeline = simulate(first_success("a"), {"a": FakeProvider(1, video_work())},
                                 book, start_at=1)
        self.assertEqual(timeline.started(), [])
        self.assertEqual(run.failure.code, "ALL_PROVIDERS_COOLING")
